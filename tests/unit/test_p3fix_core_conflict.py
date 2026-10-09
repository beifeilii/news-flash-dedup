# -*- coding: utf-8 -*-
"""提交一修复：硬冲突前置层 + 模式开关 + 审计持久化 + 理由纠偏验收测试。

依据：独立复审整改令（2026-10-10，老板批准转发，分支
p3-semantic-authority）——
一、compare/core_conflict.py 确定性硬冲突前置层：规则比较→硬冲突前置
   →硬冲突直接不重复并跳过判官→无硬冲突才进判官双序合并；
二、DEDUP_JUDGE_DECISION_MODE 默认 legacy_proof_gate、非法值明确报错
   （开关行为钉在 test_p3c_decision_mode.py）；
三、错误测试样本修正 + 硬冲突正例（判官零调用）+ 反过度拦截反例
   （主窗附加纪律 1：反例是命根子——任一 false positive=把真重复冤杀）；
四、判官诊断结构化持久化（JudgePairOutcome→PairResult→AuditRecord 端到端，
   不进公共五字段，完整原文回退显式降质标记）；
五、公共理由按来源固定措辞（禁"已验证""已逐一核验"）。
主窗附加纪律 2：默认 legacy_proof_gate 下判官裁决面行为与 2d0d418 基线
   全等（§7 回归钉）。
"""

from __future__ import annotations

import hashlib
import time
from types import SimpleNamespace

import pytest

from news_flash_dedup.commit.coordinator import (
    CommitContext,
    build_commit_write_plan,
)
from news_flash_dedup.compare import core_conflict as cc
from news_flash_dedup.compare.pair_compare import UNRESOLVED_CODES
from news_flash_dedup.decide import audit as audit_module
from news_flash_dedup.decide import judge_adapter
from news_flash_dedup.decide import judge_pair
from news_flash_dedup.decide import judge_proof
from news_flash_dedup.decide import judge_version_config as jvc
from news_flash_dedup.decide import llm_residual as lr
from news_flash_dedup.decide import service as decide_service
from news_flash_dedup.lib import run_manifest as rm


# ---------------------------------------------------------------- 夹具（p3sem 同形）

def _ctx(record_id, item_id, text, arrival_seq, **kw):
    return {
        "record_id": record_id, "item_id": item_id, "text": text,
        "raw_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "scope_id": "default", "business_date": "2026-09-26",
        "arrival_seq": arrival_seq, "pipeline_version": "dedup_v1",
        **kw,
    }


def _fact(record_id, subject):
    ev = [{"record_id": record_id, "field": "x",
           "quote": subject[0], "start": 0, "end": 1}]
    return {
        "fact_id": "f1", "evidence": ev,
        "fact_type": {"status": "present", "raw_value": "回购", "evidence": ev},
        "subject": {"status": "present", "raw_value": subject, "evidence": ev},
        "event_state": {
            "predicate": {"status": "present", "raw_value": "回购", "evidence": ev},
            "polarity": {"status": "present", "raw_value": "回购", "evidence": ev},
            "modality": {"status": "missing", "raw_value": None, "evidence": []},
            "attribution": {"status": "missing", "raw_value": None, "evidence": []}},
        "time": {"expression": {"status": "missing", "raw_value": None, "evidence": []},
                 "stage": {"status": "missing", "raw_value": None, "evidence": []},
                 "anchor": {"status": "missing", "raw_value": None, "evidence": []}},
        "key_object": {"status": "missing", "raw_value": None, "evidence": []},
        "numerics": [],
    }


H_ID, C_ID = "a" * 64, "c" * 64


def _history(text, subject):
    h = _ctx(H_ID, "item-A", text, 1)
    h["facts"] = [_fact(H_ID, subject)]
    return h


def _current(text, subject):
    c = _ctx(C_ID, "item-C", text, 3)
    c["facts"] = [_fact(C_ID, subject)]
    return c


def _explode(ctx):
    """判官零调用反证：前置层若未拦截，本 callable 被调用即炸 →
    JUDGE_EXCEPTION 未决边界（断言"不重复"即失败）。"""
    raise AssertionError("硬冲突对不得进判官")


def _decide(h_text, h_subject, c_text, c_subject, judge, **kw):
    return decide_service.decide_for_task(
        _history(h_text, h_subject), [],
        current=_current(c_text, c_subject),
        judge_callable=judge, judge_in_chain=True,
        coverage_complete=True, **kw)


# ---------------------------------------------------------------- double 假证明（p3c 同型）

_DOUBLE_MODEL = "judge-double-v1"
_DOUBLE_PROMPT_SHA = hashlib.sha256(b"double-prompt").hexdigest()
_DOUBLE_POLICY = "policy-v1"


def _make_proof(ctx, *, verdict, mv_passed=True, rules_triggered=(),
                with_falsification=False, unbound_quotes=False,
                falsification=None, reason="双序测试理由。"):
    text_a, text_b = ctx["text_a"], ctx["text_b"]
    sha_a = hashlib.sha256(text_a.encode("utf-8")).hexdigest()
    sha_b = hashlib.sha256(text_b.encode("utf-8")).hexdigest()
    if unbound_quotes:
        quotes_a = [{"text": "不存在的片段", "offset_start": 0, "offset_end": 2,
                     "role": "subject"}]
        quotes_b = [{"text": "也不存在的片段", "offset_start": 0, "offset_end": 2,
                     "role": "subject"}]
    else:
        quotes_a = [{"text": text_a[0:4], "offset_start": 0, "offset_end": 4,
                     "role": "subject"}]
        quotes_b = [{"text": text_b[0:4], "offset_start": 0, "offset_end": 4,
                     "role": "subject"}]
    proof = {
        "pair_id": ctx["pair_id"],
        "item_a_id": ctx["item_a_id"],
        "item_b_id": ctx["item_b_id"],
        "text_a_sha256": sha_a,
        "text_b_sha256": sha_b,
        "order": ctx["order"],
        "verdict": verdict,
        "reason": reason,
        "quotes_a": quotes_a,
        "quotes_b": quotes_b,
        "numeric_check": {"conclusion": "一致", "details": []},
        "time_check": {"conclusion": "一致", "anchors_a": [], "anchors_b": []},
        "machine_verify": {"mode": "audit",
                           "rules_triggered": list(rules_triggered),
                           "passed": mv_passed},
        "model_version": _DOUBLE_MODEL,
        "prompt_sha256": _DOUBLE_PROMPT_SHA,
        "policy_version": _DOUBLE_POLICY,
        "judged_at": "2026-10-10T00:00:00+00:00",
    }
    if verdict == "not_duplicate" and with_falsification:
        proof["falsification"] = falsification
    proof["cache_key"] = judge_pair.compute_cache_key(
        _DOUBLE_MODEL, _DOUBLE_PROMPT_SHA, _DOUBLE_POLICY,
        ctx["order"], sha_a, sha_b)
    return proof


def _judge(handler):
    calls = []

    def _callable(ctx):
        calls.append(ctx["order"])
        return handler(ctx)

    _callable.calls = calls
    return _callable


def _adjudicate_direct(judge, h_text, c_text, **kw):
    pair = SimpleNamespace(
        pair_id="pair-p3fix-direct", history_record_id=H_ID,
        current_record_id=C_ID, history_item_id="item-A",
        current_item_id="item-C")
    pctx = judge_pair.build_pair_context(pair, history_text=h_text,
                                         current_text=c_text)
    return judge_pair.adjudicate_pair(judge, pctx, **kw)


# ============================================================ 3-A. 硬冲突正例（判官零调用）
# 整改令三：每种硬冲突至少一条正例——主体明确不同（代码互斥/主体名互斥）、
# 数值不同、时间不同、开盘/收盘、上涨/下跌、高于/低于、单位美元/点、
# 数值修订后有效值不同。默认 legacy 模式下同样前置拦截（模式无关）。

_HARD_CONFLICT_CASES = [
    # (case_id, h_text, c_text, type, h_quote, c_quote, 公共理由)
    ("subject-code",
     "甲公司（600001）公告营收100万元。", "甲公司（600002）公告营收100万元。",
     "subject", "600001", "600002",
     "两条快讯的核心主体不同，属于同一槽位明确冲突，因此判定为不重复。"),
    ("subject-name",  # 整改令三点名样本：易天股份（300812）/名臣健康
     "易天股份（300812）主力净流入5亿。", "名臣健康主力净流入5亿。",
     "subject", "易天股份", "名臣健康",
     "两条快讯的核心主体不同，属于同一槽位明确冲突，因此判定为不重复。"),
    ("numeric",  # 整改令三点名样本：787.8万/789.8万 同槽位数值冲突
     "甲公司9月24日公告营收787.8万元。", "甲公司9月24日公告营收789.8万元。",
     "numeric", "787.8万", "789.8万",
     "两条快讯的核心数值不同，属于同一槽位明确冲突，因此判定为不重复。"),
    ("unit",
     "甲公司9月24日报价6612.30美元。", "甲公司9月24日报价6612.30点。",
     "unit", "6612.30美元", "6612.30点",
     "两条快讯同槽位数值的单位不同，属于同一槽位明确冲突，因此判定为不重复。"),
    ("time",
     "甲公司9月24日公告营收100万元。", "甲公司9月25日公告营收100万元。",
     "time", "9月24日", "9月25日",
     "两条快讯的事实时间不同，属于同一槽位明确冲突，因此判定为不重复。"),
    ("stage",
     "甲公司开盘价100元。", "甲公司收盘价100元。",
     "stage", "开盘", "收盘",
     "两条快讯的时间阶段不同，属于同一槽位明确冲突，因此判定为不重复。"),
    ("polarity-rise-fall",
     "甲公司股价上涨5%。", "甲公司股价下跌5%。",
     "polarity", "上涨", "下跌",
     "两条快讯的方向相反，属于同一槽位明确冲突，因此判定为不重复。"),
    ("polarity-above-below",
     "甲公司业绩高于预期。", "甲公司业绩低于预期。",
     "polarity", "高于", "低于",
     "两条快讯的方向相反，属于同一槽位明确冲突，因此判定为不重复。"),
    ("revision",
     "甲公司公告净利177.9万元。", "甲公司公告：净利由177.9万修正为177.5万。",
     "revision", "177.9万", "177.5万",
     "两条快讯为修订关系且修订后最新有效值不同，属于同一槽位明确冲突，因此判定为不重复。"),
]


@pytest.mark.parametrize(
    "case_id,h_text,c_text,ctype,h_quote,c_quote,reason",
    _HARD_CONFLICT_CASES,
    ids=[c[0] for c in _HARD_CONFLICT_CASES])
def test_hard_conflict_skips_judge_signs_verified_conflict(
        monkeypatch, case_id, h_text, c_text, ctype, h_quote, c_quote,
        reason):
    """整改令一/三：核心硬冲突→直接不重复（VERIFIED_CONFLICT）+判官零调用
    （爆炸 callable 反证）+双侧原文证据绑真 offset+按冲突类型的固定公共
    理由（整改令五）+审计冲突件 basis=CORE_CONFLICT。默认 legacy 模式下
    同样拦截（前置层先于模式分流）。"""
    monkeypatch.delenv(judge_pair.JUDGE_DECISION_MODE_ENV, raising=False)
    h_subject = h_text.lstrip("*ST").split("（")[0][:4]
    c_subject = c_text.lstrip("*ST").split("（")[0][:4]
    out = _decide(h_text, h_subject, c_text, c_subject, _explode)
    assert out.decision == "不重复", case_id
    assert out.duplicate_ids == ()
    assert out.internal_code == "VERIFIED_CONFLICT"
    pair = out.pair_results[0]
    assert pair.outcome == "conflict" and pair.code == "VERIFIED_CONFLICT"
    assert len(pair.verified_conflicts) == 1
    conflict = pair.verified_conflicts[0]
    assert conflict.field_path == f"core_conflict.{ctype}"
    assert conflict.basis == "CORE_CONFLICT"
    assert conflict.history_evidence.quote == h_quote
    assert conflict.current_evidence.quote == c_quote
    # 双侧证据可回指原文（整改令一条件 4：两侧都能绑定原文证据）
    h_ev, c_ev = conflict.history_evidence, conflict.current_evidence
    assert h_text[h_ev.start:h_ev.end] == h_quote
    assert c_text[c_ev.start:c_ev.end] == c_quote
    assert out.reason == reason                    # 整改令五固定措辞
    assert "已验证" not in out.reason and "已逐一核验" not in out.reason
    assert pair.judge_decision_mode == ""          # 判官未参与（前置层直判）
    # 观测指标同源消费（提交三指标面+本层拦截计数）：判官零调用但拦截
    # 计数照记，且绝不进公共五字段
    diag = out.judge_diagnostics
    assert diag["judge.core_conflict.intercepted"] == 1
    assert diag[f"judge.core_conflict.{ctype}"] == 1
    import json as _json
    assert "core_conflict" not in _json.dumps(
        out.to_public_dict(), ensure_ascii=False)
    # 公共五字段封闭
    public = out.to_public_dict()
    assert set(public) == {"item_id", "text", "decision",
                           "duplicate_ids", "reason"}
    for code in ("VERIFIED_CONFLICT", "JUDGE_NON_DUPLICATE", "CORE_CONFLICT"):
        assert code not in public["reason"]


def test_hard_conflict_module_output_shape():
    """整改令一：core_conflict 输出至少含 has_conflict/conflict_type/
    field_path/human_reason/history_evidence/current_evidence 六字段。"""
    hit = cc.detect_core_conflict(
        "甲公司9月24日公告营收787.8万元。", "甲公司9月24日公告营收789.8万元。",
        history_record_id=H_ID, current_record_id=C_ID)
    assert hit.has_conflict is True
    assert hit.conflict_type == "numeric"
    assert hit.field_path == "core_conflict.numeric"
    assert hit.human_reason
    assert hit.history_evidence is not None
    assert hit.current_evidence is not None
    miss = cc.detect_core_conflict(
        "甲公司9月24日公告营收100万元。", "甲公司公告：9月24日营收100万元。",
        history_record_id=H_ID, current_record_id=C_ID)
    assert miss.has_conflict is False
    assert miss.conflict_type == "" and miss.field_path == ""
    assert miss.history_evidence is None and miss.current_evidence is None


# ============================================================ 3-B. 反过度拦截反例（命根子）
# 整改令三：单方缺数值/缺时间/缺主体不触发；信息补充无矛盾不触发；不同
# 指标/角色数值不错位比较；别名/简称可无损归一不误判。反例一律进判官
# （调用计数==2），结论由判官业务正确签发。

def _semantic(monkeypatch):
    monkeypatch.setenv(judge_pair.JUDGE_DECISION_MODE_ENV,
                       judge_pair.MODE_SEMANTIC_AUTHORITY)


def test_counterexample_one_side_numeric_missing(monkeypatch):
    """反例①：单方缺数值→不触发数值冲突（缺失≠冲突）→判官判重复。"""
    _semantic(monkeypatch)
    judge = _judge(lambda ctx: _make_proof(ctx, verdict="duplicate"))
    out = _decide("甲公司公告营收100万元。", "甲公司",
                  "甲公司公告营收增长。", "甲公司", judge)
    assert judge.calls == ["ab", "ba"]               # 判官实跑（未被拦截）
    assert out.decision == "重复"
    assert out.internal_code == "JUDGE_EQUIVALENT"


def test_counterexample_one_side_time_missing(monkeypatch):
    """反例②：单方缺时间→不触发时间冲突（宪章时间缺失例外）→判官判重复。"""
    _semantic(monkeypatch)
    judge = _judge(lambda ctx: _make_proof(ctx, verdict="duplicate"))
    out = _decide("甲公司9月24日公告营收100万元。", "甲公司",
                  "甲公司公告营收100万元。", "甲公司", judge)
    assert judge.calls == ["ab", "ba"]
    assert out.decision == "重复"


def test_counterexample_one_side_subject_generic(monkeypatch):
    """反例③：一方只写"公司股票"（泛指自指）→不按主体冲突处理
    （=P_SUBJECT_MISSING 告警通道）→判官判重复。"""
    _semantic(monkeypatch)
    judge = _judge(lambda ctx: _make_proof(ctx, verdict="duplicate"))
    out = _decide("*ST清越（600146）主力净流入5亿。", "*ST清越",
                  "公司股票主力净流入5亿。", "公司股票", judge)
    assert judge.calls == ["ab", "ba"]
    assert out.decision == "重复"


def test_counterexample_info_supplement_no_contradiction(monkeypatch):
    """反例④：信息补充已有槽位无矛盾（多报一条净利）→不触发→判官判重复。"""
    _semantic(monkeypatch)
    judge = _judge(lambda ctx: _make_proof(ctx, verdict="duplicate"))
    out = _decide("甲公司公告营收100万元。", "甲公司",
                  "甲公司公告营收100万元，净利60万元。", "甲公司", judge)
    assert judge.calls == ["ab", "ba"]
    assert out.decision == "重复"


def test_counterexample_cross_role_numerics_not_compared(monkeypatch):
    """反例⑤：不同指标/角色的数值不错位比较（营收100万 vs 净利100万）
    →不触发数值冲突→判官语义判不重复（业务正确：指标不同）。"""
    _semantic(monkeypatch)
    judge = _judge(lambda ctx: _make_proof(ctx, verdict="not_duplicate"))
    out = _decide("甲公司公告营收100万元。", "甲公司",
                  "甲公司公告净利100万元。", "甲公司", judge)
    assert judge.calls == ["ab", "ba"]
    assert out.decision == "不重复"
    assert out.internal_code == "JUDGE_NON_DUPLICATE"


def test_counterexample_alias_containment_not_misjudged(monkeypatch):
    """反例⑥：主体别名/简称可无损归一（易天股份 vs 易天、*ST清越 vs
    清越股份）→不得误判主体冲突→判官判重复。"""
    _semantic(monkeypatch)
    judge = _judge(lambda ctx: _make_proof(ctx, verdict="duplicate"))
    out = _decide("易天股份主力净流入5亿。", "易天股份",
                  "易天主力净流入5亿。", "易天", judge)
    assert judge.calls == ["ab", "ba"]
    assert out.decision == "重复"
    judge2 = _judge(lambda ctx: _make_proof(ctx, verdict="duplicate"))
    out2 = _decide("*ST清越主力净流入5亿。", "*ST清越",
                   "清越股份主力净流入5亿。", "清越股份", judge2)
    assert judge2.calls == ["ab", "ba"]
    assert out2.decision == "重复"


def test_counterexample_same_value_different_notation(monkeypatch):
    """反例⑦（补员）：同值不同写法不冤杀——3286000股=328.6万股（另侧
    多报占比=信息补充）；每10股派3元=每股派0.3元（token 数不等不错位）；
    否定词族不对称（并未拉低/未拉低）不按极性冲突拦截。"""
    _semantic(monkeypatch)
    judge = _judge(lambda ctx: _make_proof(ctx, verdict="duplicate"))
    out = _decide("甲公司9月24日减持3286000股，占总股本1.2%。", "甲公司",
                  "甲公司9月24日减持328.6万股。", "甲公司", judge)
    assert judge.calls == ["ab", "ba"]
    assert out.decision == "重复"
    judge2 = _judge(lambda ctx: _make_proof(ctx, verdict="duplicate"))
    out2 = _decide("甲公司每10股派3元。", "甲公司",
                   "甲公司每股派0.3元。", "甲公司", judge2)
    assert judge2.calls == ["ab", "ba"]
    assert out2.decision == "重复"
    judge3 = _judge(lambda ctx: _make_proof(ctx, verdict="duplicate"))
    out3 = _decide("公司表示并未拉低股价。", "公司",
                   "公司表示未拉低股价。", "公司", judge3)
    assert judge3.calls == ["ab", "ba"]
    assert out3.decision == "重复"


# ============================================================ 4. 诊断持久化端到端

def test_diagnostics_persist_pair_to_audit_record(monkeypatch):
    """整改令四：JudgePairOutcome→PairResult→AuditRecord 端到端——
    evidence_status/evidence_warnings/machine_findings/
    full_text_fallback_used/judge_decision_mode 结构化落对级结果并进
    持久化审计文档；绝不进公共五字段；payload_hash 规范化集不含诊断
    字段（审计哈希稳定）。"""
    _semantic(monkeypatch)
    judge = _judge(lambda ctx: _make_proof(
        ctx, verdict="duplicate", mv_passed=False,
        rules_triggered=("P_OFFSET",)))
    history = _history("甲公司9月24日公告营收100万元。", "甲公司")
    current = _current("甲公司公告：9月24日营收100万元。", "甲公司")
    out = decide_service.decide_for_task(
        history, [], current=current, judge_callable=judge,
        judge_in_chain=True, coverage_complete=True)
    assert out.decision == "重复"
    pair = out.pair_results[0]                       # 对级结构化字段
    assert pair.evidence_status == "warn"
    assert "P_OFFSET" in pair.machine_findings
    assert judge_pair.MACHINE_VERIFY_REJECTED in pair.evidence_warnings
    assert pair.full_text_fallback_used is False
    assert pair.judge_decision_mode == "semantic_authority"
    public = out.to_public_dict()                    # 公共五字段封闭
    assert set(public) == {"item_id", "text", "decision",
                           "duplicate_ids", "reason"}
    import json as _json
    blob = _json.dumps(public, ensure_ascii=False)
    for key in ("evidence_status", "evidence_warnings", "machine_findings",
                "full_text_fallback_used", "judge_decision_mode",
                "P_OFFSET"):
        assert key not in blob
    # 持久化审计文档（commit 写入计划同源）
    ctx = CommitContext(
        scope_id="default", business_date="2026-09-26", arrival_seq=3,
        current=current, candidates=(history,), visible_seq=10,
        prepared_seq=10, coverage_complete=True)
    plan = build_commit_write_plan(ctx, out, audit_complete=True)
    record = plan.audit_batch.records[0]
    assert record.evidence_status == "warn"
    assert "P_OFFSET" in record.machine_findings
    assert judge_pair.MACHINE_VERIFY_REJECTED in record.evidence_warnings
    assert record.full_text_fallback_used is False
    assert record.judge_decision_mode == "semantic_authority"
    doc = record.to_doc()                            # 持久化文档实形
    for key in ("evidence_status", "evidence_warnings", "machine_findings",
                "full_text_fallback_used", "judge_decision_mode"):
        assert key in doc
    # payload_hash 稳定性：同核心字段、不同诊断 → 同哈希（诊断不进
    # 规范化集，既有审计重放一致性不动）
    record_default_diag = audit_module.build_audit_record(
        pair.__class__(**{**pair.__dict__,
                          "evidence_status": "pass", "evidence_warnings": (),
                          "machine_findings": (),
                          "full_text_fallback_used": False,
                          "judge_decision_mode": ""}))
    assert record.payload_hash == record_default_diag.payload_hash


def test_full_text_fallback_flag_persisted_explicitly(monkeypatch):
    """整改令四：完整原文回退显式标记（full_text_fallback_used=True）——
    不与精确引文同质量级，对级与审计文档同标。"""
    _semantic(monkeypatch)
    judge = _judge(lambda ctx: _make_proof(ctx, verdict="duplicate",
                                           unbound_quotes=True))
    history = _history("甲公司9月24日公告营收100万元。", "甲公司")
    current = _current("甲公司公告：9月24日营收100万元。", "甲公司")
    out = decide_service.decide_for_task(
        history, [], current=current, judge_callable=judge,
        judge_in_chain=True, coverage_complete=True)
    assert out.decision == "重复"
    pair = out.pair_results[0]
    assert pair.full_text_fallback_used is True
    assert pair.evidence_status == "fail"            # 双侧均无可绑定引文
    assert judge_pair.EVIDENCE_FALLBACK_FULL_TEXT in pair.evidence_warnings
    ctx = CommitContext(
        scope_id="default", business_date="2026-09-26", arrival_seq=3,
        current=current, candidates=(history,), visible_seq=10,
        prepared_seq=10, coverage_complete=True)
    plan = build_commit_write_plan(ctx, out, audit_complete=True)
    record = plan.audit_batch.records[0]
    assert record.full_text_fallback_used is True
    assert judge_pair.EVIDENCE_FALLBACK_FULL_TEXT in record.evidence_warnings


def test_pair_diagnostics_defaults_when_judge_untouched():
    """整改令四兜底：判官未参与的对（开关关）→对级诊断字段保持默认
    （pass/空/False/""），绝不伪造判官痕迹。"""
    out = decide_service.decide_for_task(
        _history("甲公司9月24日公告营收100万元。", "甲公司"), [],
        current=_current("甲公司公告：9月24日营收100万元。", "甲公司"),
        coverage_complete=True)
    pair = out.pair_results[0]
    assert pair.evidence_status == "pass"
    assert pair.evidence_warnings == ()
    assert pair.machine_findings == ()
    assert pair.full_text_fallback_used is False
    assert pair.judge_decision_mode == ""


# ============================================================ 7. 默认模式基线全等回归钉
# 主窗附加纪律 2：默认 legacy_proof_gate（env 缺席）下判官裁决面行为与
# 2d0d418 基线逐字全等（码/文案/未决映射）。硬冲突前置层是整改令新增
# 行为（本节不含硬冲突夹具——拦截行为由 §3-A 专钉）。

def test_baseline_default_mode_clean_duplicate(monkeypatch):
    """基线全等①：默认模式干净双序 duplicate → FACT_EQUIVALENT + 基线
    文案逐字（服务级+适配器级双钉）。"""
    monkeypatch.delenv(judge_pair.JUDGE_DECISION_MODE_ENV, raising=False)
    judge = _judge(lambda ctx: _make_proof(ctx, verdict="duplicate"))
    judged = _adjudicate_direct(
        judge, "甲公司9月24日公告营收100万元。", "甲公司公告：9月24日营收100万元。")
    assert judged.outcome == "equivalent"
    assert judged.code == "FACT_EQUIVALENT"
    assert judged.detail == (
        "判官双序一致判定同一事实，引文已绑定双侧原文且机器验 gate 通过。")
    out = _decide("甲公司9月24日公告营收100万元。", "甲公司",
                  "甲公司公告：9月24日营收100万元。", "甲公司", judge)
    assert out.decision == "重复"
    assert out.internal_code == "FACT_EQUIVALENT"
    assert out.pair_results[0].code == "FACT_EQUIVALENT"


def test_baseline_default_mode_nd_with_falsification(monkeypatch):
    """基线全等②：默认模式双序 ND+合格证伪轴 → VERIFIED_CONFLICT +
    基线文案（适配器级——服务级真轴对已由前置层接管）。"""
    monkeypatch.delenv(judge_pair.JUDGE_DECISION_MODE_ENV, raising=False)
    t_h, t_c = "甲公司9月24日公告营收100万元。", "甲公司9月25日公告营收100万元。"

    def _fals(ctx):
        ta, tb = ctx["text_a"], ctx["text_b"]
        da = "9月24日" if "9月24日" in ta else "9月25日"
        db = "9月25日" if da == "9月24日" else "9月24日"
        return {"dimension": "time",
                "evidence_a": {"text": da, "offset_start": ta.find(da),
                               "offset_end": ta.find(da) + len(da)},
                "evidence_b": {"text": db, "offset_start": tb.find(db),
                               "offset_end": tb.find(db) + len(db)},
                "relation": "事实时间不同，证伪同一事实。"}

    judge = _judge(lambda ctx: _make_proof(
        ctx, verdict="not_duplicate", with_falsification=True,
        falsification=_fals(ctx)))
    judged = _adjudicate_direct(judge, t_h, t_c)
    assert judged.outcome == "conflict"
    assert judged.code == "VERIFIED_CONFLICT"
    assert judged.detail == (
        "判官双序一致证伪同一事实（维度 time），"
        "双侧证伪引文已绑定原文且机器验 gate 通过。")


def test_baseline_default_mode_unresolved_mappings(monkeypatch):
    """基线全等③：默认模式未决映射逐项——裸 ND→doubtful/SUBJECT_
    UNRESOLVED；分歧→SUBJECT_UNRESOLVED；超时→DEPENDENCY_TIMEOUT；
    换文绑定→EVIDENCE_INVALID；mv passed=false→EVIDENCE_INVALID；
    未注入→SUBJECT_UNRESOLVED。"""
    monkeypatch.delenv(judge_pair.JUDGE_DECISION_MODE_ENV, raising=False)
    h, c = "甲公司公告回购股份。", "甲公司发布半年度财报。"
    # 裸 ND → doubtful
    judged = _adjudicate_direct(_judge(lambda ctx: _make_proof(
        ctx, verdict="not_duplicate")), h, c)
    assert judged.outcome == "unresolved"
    assert judged.code == "SUBJECT_UNRESOLVED"
    assert judged.failure_reason == judge_pair.JUDGE_DOUBTFUL
    # 裸 ND 参与的分歧 → 无轴侧先降级 doubtful（基线 §二 旧口径），
    # 归因 JUDGE_DOUBTFUL 而非 ORDER_DISAGREE
    judged2 = _adjudicate_direct(_judge(lambda ctx: _make_proof(
        ctx, verdict=("duplicate" if ctx["order"] == "ab"
                      else "not_duplicate"))), h, c)
    assert judged2.code == "SUBJECT_UNRESOLVED"
    assert judged2.failure_reason == judge_pair.JUDGE_DOUBTFUL
    # 带真轴 ND 参与的分歧 → ORDER_DISAGREE（时间差文本 + time 维证伪）
    t_h, t_c = "甲公司9月24日公告营收100万元。", "甲公司9月25日公告营收100万元。"

    def _disagree(ctx):
        if ctx["order"] == "ab":
            return _make_proof(ctx, verdict="duplicate")
        ta, tb = ctx["text_a"], ctx["text_b"]
        da = "9月24日" if "9月24日" in ta else "9月25日"
        db = "9月25日" if da == "9月24日" else "9月24日"
        return _make_proof(
            ctx, verdict="not_duplicate", with_falsification=True,
            falsification={
                "dimension": "time",
                "evidence_a": {"text": da, "offset_start": ta.find(da),
                               "offset_end": ta.find(da) + len(da)},
                "evidence_b": {"text": db, "offset_start": tb.find(db),
                               "offset_end": tb.find(db) + len(db)},
                "relation": "事实时间不同，证伪同一事实。"})
    judged2b = _adjudicate_direct(_judge(_disagree), t_h, t_c)
    assert judged2b.code == "SUBJECT_UNRESOLVED"
    assert judged2b.failure_reason == judge_pair.ORDER_DISAGREE
    # 超时 → DEPENDENCY_TIMEOUT
    def _slow(ctx):
        time.sleep(1.0)
        return _make_proof(ctx, verdict="duplicate")
    judged3 = _adjudicate_direct(_judge(_slow), h, c, timeout_s=0.2)
    assert judged3.code == "DEPENDENCY_TIMEOUT"
    assert judged3.failure_reason == judge_pair.TIMEOUT
    # 换文绑定 → EVIDENCE_INVALID
    def _stolen(ctx):
        proof = _make_proof(ctx, verdict="duplicate")
        sha_a = hashlib.sha256("别人的正文甲".encode("utf-8")).hexdigest()
        sha_b = hashlib.sha256("别人的正文乙".encode("utf-8")).hexdigest()
        proof["text_a_sha256"] = sha_a
        proof["text_b_sha256"] = sha_b
        proof["cache_key"] = judge_pair.compute_cache_key(
            _DOUBLE_MODEL, _DOUBLE_PROMPT_SHA, _DOUBLE_POLICY,
            ctx["order"], sha_a, sha_b)
        return proof
    judged4 = _adjudicate_direct(_judge(_stolen), h, c)
    assert judged4.code == "EVIDENCE_INVALID"
    assert judged4.failure_reason == judge_pair.INVALID_OUTPUT
    # mv passed=false duplicate → EVIDENCE_INVALID（旧否决）
    judged5 = _adjudicate_direct(_judge(lambda ctx: _make_proof(
        ctx, verdict="duplicate", mv_passed=False,
        rules_triggered=("P_OFFSET",))), h, c)
    assert judged5.code == "EVIDENCE_INVALID"
    assert judged5.failure_reason == judge_pair.MV_REJECTED
    # 未注入 → SUBJECT_UNRESOLVED
    judged6 = _adjudicate_direct(None, h, c)
    assert judged6.code == "SUBJECT_UNRESOLVED"
    assert judged6.failure_reason == judge_pair.JUDGE_NOT_INJECTED


# ============================================================ 8. 提交二复审条 1/2/3
# 版本装配单源化 + RunManifest 如实登记生效版本 + judge_v5 随模式分发

def test_version_config_prompt_policy_mapping():
    """复审条 1：统一版本配置对象 prompt→policy 映射——judge_v1/v2/v3→
    policy_v2（各自原 policy=judge_proof 证明门控宪章版）；judge_v5→
    policy_v3；未注册 prompt/未登记映射一律 fail-closed。"""
    for v in ("judge_v1", "judge_v2", "judge_v3"):
        vc = jvc.judge_version_for_prompt(v)
        assert vc.policy_version == "policy_v2"
        assert vc.policy_version == judge_proof.PROOF_POLICY_VERSION
        assert vc.prompt_sha256 == lr.judge_prompt_for_version(v)[1]
        assert vc.decision_mode == ""
    vc5 = jvc.judge_version_for_prompt("judge_v5")
    assert vc5.policy_version == "policy_v3"
    with pytest.raises(ValueError):                 # 未注册 prompt
        jvc.judge_version_for_prompt("judge_v4")


def test_version_config_mode_dispatch_and_env_default():
    """复审条 3：judge_v5 不得无条件默认——模式分发表 legacy_proof_gate
    （默认）→judge_v1+policy_v2；semantic_authority→judge_v5+policy_v3；
    非法模式 fail-closed；缺省读 env（缺席/空串=legacy）。"""
    vc_legacy = jvc.judge_version_for_mode(judge_pair.MODE_LEGACY_PROOF_GATE)
    assert (vc_legacy.prompt_version, vc_legacy.policy_version) == (
        "judge_v1", "policy_v2")
    assert vc_legacy.decision_mode == "legacy_proof_gate"
    vc_sem = jvc.judge_version_for_mode(judge_pair.MODE_SEMANTIC_AUTHORITY)
    assert (vc_sem.prompt_version, vc_sem.policy_version) == (
        "judge_v5", "policy_v3")
    assert vc_sem.decision_mode == "semantic_authority"
    with pytest.raises(ValueError):
        jvc.judge_version_for_mode("banana")
    # 缺省 env 分发（缺席=legacy 默认；空串同 legacy）
    assert jvc.default_judge_version({}).prompt_version == "judge_v1"
    assert jvc.default_judge_version(
        {"DEDUP_JUDGE_DECISION_MODE": ""}).prompt_version == "judge_v1"
    assert jvc.default_judge_version(
        {"DEDUP_JUDGE_DECISION_MODE": "semantic_authority"}
    ).prompt_version == "judge_v5"
    with pytest.raises(ValueError):                 # 非法 env 同罪不静默
        jvc.default_judge_version({"DEDUP_JUDGE_DECISION_MODE": "banana"})


_GIT_SHA0 = "0" * 40


def test_adapter_effective_versions_match_manifest_records(monkeypatch):
    """复审条 2 钉测：adapter 实际版本与 manifest 记录一致——同一环境下
    judge_adapter.default_judge_config（装配默认）、罐装真件证明的
    prompt_sha256/policy_version/cache_key 素材、build_run_manifest 缺省
    登记三者同源相等（legacy 与 semantic 双环境各验）。"""
    import json as _json

    t_h, t_c = "甲公司9月24日公告营收100万元。", "甲公司公告称，9月24日营收为100万元。"
    resp = _json.dumps({
        "decision": "重复", "reason": "r",
        "evidence_a": ["100万元"], "evidence_b": ["100万元"],
        "numeric_check": {"conclusion": "一致", "numbers_a": ["100万"],
                          "numbers_b": ["100万"]},
        "time_check": {"conclusion": "一致", "times_a": ["9月24日"],
                       "times_b": ["9月24日"]},
    }, ensure_ascii=False)

    for mode, want_prompt, want_policy in (
            ("legacy_proof_gate", "judge_v1", "policy_v2"),
            ("semantic_authority", "judge_v5", "policy_v3")):
        env = {"DEDUP_JUDGE_PROOF": "1", "DEDUP_JUDGE_DECISION_MODE": mode}
        vc = jvc.default_judge_version(env)
        # ① adapter 装配默认配置同源（条 1/3）
        cfg = judge_adapter.default_judge_config(env)
        assert cfg.prompt_version == want_prompt == vc.prompt_version
        assert cfg.mv_mode == lr.MV_AUDIT
        # ② manifest 缺省登记同源（条 2：如实记录实际生效版本）
        manifest = rm.build_run_manifest(
            inputs=("rec-a",), embedding_space="fake_space",
            code_git_sha=_GIT_SHA0, env=env)
        assert manifest.prompt_version == vc.prompt_version
        assert manifest.prompt_sha256 == vc.prompt_sha256
        assert manifest.policy_version == vc.policy_version == want_policy
        # ③ 罐装真件证明四维同源（adapter 实跑产物==manifest 记录）
        judge = lr.SyncResidualJudge(
            lr.ResidualJudgeConfig(prompt_version=cfg.prompt_version),
            call_fn=lambda model, system, user, timeout_s=None: (resp, 0.01))
        cb = judge_adapter.build_judge_callable(judge=judge, environ=env)
        assert cb is not None
        ctx = {
            "pair_id": "p-ver", "order": "ab",
            "item_a_id": "item-A", "item_b_id": "item-C",
            "record_a_id": H_ID, "record_b_id": C_ID,
            "text_a": t_h, "text_b": t_c}
        proof = cb(ctx)
        assert proof["prompt_sha256"] == vc.prompt_sha256 == \
            manifest.prompt_sha256
        assert proof["policy_version"] == vc.policy_version == \
            manifest.policy_version
        assert proof["cache_key"] == judge_pair.compute_cache_key(
            proof["model_version"], vc.prompt_sha256, vc.policy_version,
            "ab", proof["text_a_sha256"], proof["text_b_sha256"])
