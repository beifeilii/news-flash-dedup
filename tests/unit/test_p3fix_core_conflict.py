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

独立终审整改令（P0 六项+P1 三项，2026-10-10，本文件收口）——
P0-1 模式闸：硬冲突前置仅 semantic_authority 执行；legacy 全等回归钉
   （§7 专项：前置层零调用+判官照常+公共五字段基线全等）；
P0-2 主体收窄：删除"文首取段首4字"回退，主体硬冲突只保留双侧代码
   互斥（§3-A subject-code 正例+§3-B-bis 特朗普反例钉）；
P0-3 四族摘除：极性/单位/时间/阶段从 _DETECTORS 摘除（§3-B-bis 三枚
   反例钉 a/b/c+§3-C 函数级语义钉备日后槽位对齐版回归）；
P0-4 revision 摘除：前值修订交判官（§3-B-bis 反例钉）；
P0-5 fail-open：逐检测器 try/except+judge.core_conflict.detector_error
   诊断计数+放行给判官（§3-B-bis 两钉）；
P0-6 数值族保留加审：787.8万/789.8万 正例（判官零调用）+3286000股/
   328.6万股 同值不同写法反例（§3-A/§3-B-bis）；
P1-1 单源传递：JudgeVersionConfig 在 decide_for_task 入口创建一次，
   adapter/manifest/audit 三处版本一致（§9 钉测：env 空+显式实参
   semantic_authority→三层 v6/policy_v4——一期 v6-lite 起映射 v6）；
P1-2 diagnostics_hash：五诊断字段规范化哈希入 AuditRecord，payload_hash
   原样不动（§10 钉测）。
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
from news_flash_dedup.commit.fake_store import FakeCommitStore
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
# 整改令三：每种硬冲突至少一条正例。终审 P0 收口后现役硬冲突族=主体
# （P0-2 收窄：仅双侧明确证券代码互斥）+数值（P0-6 保留：剔数骨架
# 同槽位判据）；unit/time/stage/polarity/revision 五族已摘除（正例改钉
# 函数级语义备回归，见 §3-C）。前置拦截仅 semantic_authority 模式生效
# （P0-1 模式闸；legacy 全等回归钉见 §7）。

_HARD_CONFLICT_CASES = [
    # (case_id, h_text, c_text, type, h_quote, c_quote, 公共理由)
    ("subject-code",
     "甲公司（600001）公告营收100万元。", "甲公司（600002）公告营收100万元。",
     "subject", "600001", "600002",
     "两条快讯的核心主体不同，属于同一槽位明确冲突，因此判定为不重复。"),
    ("numeric",  # 整改令三点名样本：787.8万/789.8万 同槽位数值冲突
     "甲公司9月24日公告营收787.8万元。", "甲公司9月24日公告营收789.8万元。",
     "numeric", "787.8万", "789.8万",
     "两条快讯的核心数值不同，属于同一槽位明确冲突，因此判定为不重复。"),
    # 终审 P0-6 正例钉（无日期裸数值对——同槽位判据不依赖日期词）
    ("numeric-bare",
     "甲公司营收787.8万元。", "甲公司营收789.8万元。",
     "numeric", "787.8万", "789.8万",
     "两条快讯的核心数值不同，属于同一槽位明确冲突，因此判定为不重复。"),
]


@pytest.mark.parametrize(
    "case_id,h_text,c_text,ctype,h_quote,c_quote,reason",
    _HARD_CONFLICT_CASES,
    ids=[c[0] for c in _HARD_CONFLICT_CASES])
def test_hard_conflict_skips_judge_signs_verified_conflict(
        monkeypatch, case_id, h_text, c_text, ctype, h_quote, c_quote,
        reason):
    """整改令一/三+终审 P0-1/P0-6：semantic_authority 下核心硬冲突→直接
    不重复（VERIFIED_CONFLICT）+判官零调用（爆炸 callable 反证）+双侧
    原文证据绑真 offset+按冲突类型的固定公共理由（整改令五）+审计冲突件
    basis=CORE_CONFLICT。legacy_proof_gate 下不拦截（模式闸，§7 专项钉）。"""
    _semantic(monkeypatch)
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
    # 判官未参与（前置层直判），但生效模式留痕（终审复审第三轮：
    # 拦截对 judge_decision_mode=semantic_authority——模式即判定成因）
    assert pair.judge_decision_mode == "semantic_authority"
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


# ============================================================ 3-B-bis. 终审反例钉（P0-2/P0-3/P0-4/P0-6）
# 独立终审整改令反例：每枚钉"不得硬冲突、判官被调用"（fp=0 铁律——
# 硬冲突误判=把真重复冤杀成不重复）。

def test_p0_2_counterexample_subject_prefix_truncation(monkeypatch):
    """终审 P0-2 反例钉："特朗普表示将结束战争。/特朗普总统表示将结束
    战争。"——原"文首取段首4字"回退截成"特朗普表"/"特朗普总"误判主体
    不同；名称比对整族降级后（无代码=不判主体冲突）不得硬冲突，判官
    被调用。"""
    _semantic(monkeypatch)
    judge = _judge(lambda ctx: _make_proof(ctx, verdict="duplicate"))
    out = _decide("特朗普表示将结束战争。", "特朗普",
                  "特朗普总统表示将结束战争。", "特朗普总统", judge)
    assert judge.calls == ["ab", "ba"]          # 判官被调用（不硬冲突）
    assert "judge.core_conflict.intercepted" not in out.judge_diagnostics
    assert out.decision == "重复"                # 判官签发重复（真重复不被冤杀）


def test_p0_3a_counterexample_polarity_one_side_supplement(monkeypatch):
    """终审 P0-3 反例钉 a)："甲指数上涨，乙指数下跌。/甲指数上涨。"——
    单方补充另一指数方向，非同槽位极性冲突；极性族停用+数值骨架不同
    →不硬冲突，判官被调用。"""
    _semantic(monkeypatch)
    judge = _judge(lambda ctx: _make_proof(ctx, verdict="duplicate"))
    out = _decide("甲指数上涨，乙指数下跌。", "甲指数",
                  "甲指数上涨。", "甲指数", judge)
    assert judge.calls == ["ab", "ba"]
    assert "judge.core_conflict.intercepted" not in out.judge_diagnostics
    assert out.decision == "重复"


def test_p0_3b_counterexample_unit_cross_slot(monkeypatch):
    """终审 P0-3 反例钉 b)："甲公司目标价100美元，涨幅1%。/甲公司指数
    报100点，涨幅1%。"——同值挂不同单位词=跨槽位（目标价 vs 指数位），
    不是同槽位单位冲突；单位族停用+数值剔数骨架不同→不硬冲突，判官
    被调用。"""
    _semantic(monkeypatch)
    judge = _judge(lambda ctx: _make_proof(ctx, verdict="not_duplicate",
                                           with_falsification=False))
    out = _decide("甲公司目标价100美元，涨幅1%。", "甲公司",
                  "甲公司指数报100点，涨幅1%。", "甲公司", judge)
    assert judge.calls == ["ab", "ba"]
    assert "judge.core_conflict.intercepted" not in out.judge_diagnostics
    assert out.decision == "不重复"               # 判官语义裁决（非前置抢判）
    assert out.internal_code == "JUDGE_NON_DUPLICATE"


def test_p0_3c_counterexample_time_info_supplement(monkeypatch):
    """终审 P0-3 反例钉 c)："9月10日甲公司营收100万。/9月10日甲公司
    营收100万，当日股价涨1%。"——当日股价补充=信息补充，非时间冲突；
    时间族停用+数值 token 数不等→不硬冲突，判官被调用。"""
    _semantic(monkeypatch)
    judge = _judge(lambda ctx: _make_proof(ctx, verdict="duplicate"))
    out = _decide("9月10日甲公司营收100万。", "9月10日甲公司",
                  "9月10日甲公司营收100万，当日股价涨1%。", "9月10日甲公司",
                  judge)
    assert judge.calls == ["ab", "ba"]
    assert "judge.core_conflict.intercepted" not in out.judge_diagnostics
    assert out.decision == "重复"


def test_p0_4_counterexample_revision_goes_to_judge(monkeypatch):
    """终审 P0-4 反例钉："前值为177.9万人。/前值由177.9万人修正为
    177.5万人。"——v5 提示词 R8 已定前值修订在产品确认前一律存疑，
    硬冲突层不得抢判不重复；修订族摘除后不硬冲突，进判官。"""
    _semantic(monkeypatch)
    judge = _judge(lambda ctx: _make_proof(ctx, verdict="duplicate"))
    out = _decide("前值为177.9万人。", "前值",
                  "前值由177.9万人修正为177.5万人。", "前值", judge)
    assert judge.calls == ["ab", "ba"]          # 进判官（不硬冲突）
    assert "judge.core_conflict.revision" not in out.judge_diagnostics
    assert out.decision == "重复"


def test_p0_6_counterexample_same_value_different_notation_bare(monkeypatch):
    """终审 P0-6 反例钉："3286000股，占比5%。/328.6万股。"——同值不同
    写法+单方补充（占比）：数值 token 数不等（2 vs 1）不判同槽位冲突
    →不硬冲突，判官被调用；与 §3-A numeric-bare 正例共同钉数值族
    判据的两翼。"""
    _semantic(monkeypatch)
    judge = _judge(lambda ctx: _make_proof(ctx, verdict="duplicate"))
    out = _decide("3286000股，占比5%。", "3286000股",
                  "328.6万股。", "328.6万股", judge)
    assert judge.calls == ["ab", "ba"]
    assert "judge.core_conflict.intercepted" not in out.judge_diagnostics
    assert out.decision == "重复"


def test_p0_5_detector_exception_fails_open_to_judge(monkeypatch):
    """终审 P0-5 fail-open：任一检测器异常→记诊断计数
    （judge.core_conflict.detector_error）+放行给判官，绝不中断判定
    （后续检测器照常执行；判官照常被调用并签发）。"""
    _semantic(monkeypatch)

    def _boom(text_a, text_b, id_a, id_b):
        raise RuntimeError("检测器炸了（fail-open 钉）")

    # 前置一个必炸检测器+现役检测器：异常计数后继续跑现役族，全放行
    monkeypatch.setattr(cc, "_DETECTORS", (_boom, cc._detect_subject))
    judge = _judge(lambda ctx: _make_proof(ctx, verdict="duplicate"))
    out = _decide("甲公司9月24日公告营收100万元。", "甲公司",
                  "甲公司公告：9月24日营收100万元。", "甲公司", judge)
    assert out.judge_diagnostics["judge.core_conflict.detector_error"] == 1
    assert judge.calls == ["ab", "ba"]          # 放行给判官（双序实调）
    assert out.decision == "重复"                # 判定未中断，正常签发
    assert out.internal_code == "JUDGE_EQUIVALENT"


def test_p0_5_detector_exception_without_callback_still_fail_open(monkeypatch):
    """终审 P0-5 兜底形态：on_detector_error 未注入（None）时异常同样
    放行（不向上抛、不中断），返回 NO_CONFLICT。"""
    def _boom(text_a, text_b, id_a, id_b):
        raise RuntimeError("无回调 fail-open 钉")

    monkeypatch.setattr(cc, "_DETECTORS", (_boom,))
    hit = cc.detect_core_conflict(
        "甲公司公告营收100万元。", "甲公司公告营收100万元。",
        history_record_id=H_ID, current_record_id=C_ID)
    assert hit is cc.NO_CONFLICT
    assert hit.has_conflict is False


# ------------------------------------------------ 3-B-ter. 终审复审第三轮钉测
# 硬冲突前置拦截路径审计模式留痕：拦截对 PairResult/AuditRecord 的
# judge_decision_mode 必须记生效模式（legacy 不走此层，模式即判定成因
# 的一部分）。全假件零真 API（爆炸 callable 反证判官零调用）。

def test_review3_hard_conflict_intercept_records_semantic_mode(monkeypatch):
    """复审第三轮钉测①：semantic_authority 正常生产配置（固定映射
    config）命中硬冲突（787.8万股 vs 789.8万股类）→判官零调用（爆炸
    callable 反证）+PairResult.judge_decision_mode=="semantic_authority"
    +AuditRecord 同值。"""
    _semantic(monkeypatch)
    h_text = "甲公司9月24日公告营收787.8万元。"
    c_text = "甲公司9月24日公告营收789.8万元。"
    out = _decide(h_text, "甲公司", c_text, "甲公司", _explode)
    assert out.decision == "不重复"
    assert out.internal_code == "VERIFIED_CONFLICT"
    pair = out.pair_results[0]
    # 拦截对模式留痕（复审第三轮核心断言——原为空串）
    assert pair.judge_decision_mode == "semantic_authority"
    record = audit_module.build_audit_record(pair)
    assert record.judge_decision_mode == "semantic_authority"
    assert out.judge_version_config.decision_mode == "semantic_authority"
    # 端到端：commit 写入计划审计文档同值留痕
    ctx = CommitContext(
        scope_id="default", business_date="2026-09-26", arrival_seq=3,
        current=_current(c_text, "甲公司"),
        candidates=(_history(h_text, "甲公司"),),
        visible_seq=10, prepared_seq=10, coverage_complete=True)
    plan = build_commit_write_plan(ctx, out, audit_complete=True)
    assert plan.audit_batch.records[0].judge_decision_mode == \
        "semantic_authority"


def test_review3_replay_intercept_records_explicit_mode(monkeypatch):
    """复审第三轮钉测②：allow_prompt_direct=True+judge_v2 config+
    env=semantic_authority 命中硬冲突→PairResult 与 AuditRecord 均记
    "semantic_authority"（回放通道生效合并模式显式留痕——env 单源链
    解析值，非空串；版本面仍 config 单源 judge_v2）。"""
    monkeypatch.setenv(judge_pair.JUDGE_DECISION_MODE_ENV,
                       "semantic_authority")
    replay_vc = jvc.judge_version_for_prompt("judge_v2")
    assert replay_vc.decision_mode == ""
    h_text = "甲公司营收787.8万元。"
    c_text = "甲公司营收789.8万元。"
    out = decide_service.decide_for_task(
        _history(h_text, "甲公司"), [],
        current=_current(c_text, "甲公司"),
        judge_callable=_explode, judge_in_chain=True, coverage_complete=True,
        judge_version_config=replay_vc, allow_prompt_direct=True)
    assert out.decision == "不重复"
    assert out.internal_code == "VERIFIED_CONFLICT"
    pair = out.pair_results[0]
    # 拦截对留生效合并模式（回放通道 env 解析值——显式非空串）
    assert pair.judge_decision_mode == "semantic_authority"
    record = audit_module.build_audit_record(pair)
    assert record.judge_decision_mode == "semantic_authority"
    # 版本面仍 config 单源（v2 提示词身份不漂移）
    assert out.judge_version_config is replay_vc
    assert out.judge_version_config.prompt_version == "judge_v2"


# ============================================================ 3-C. 已摘除族函数级语义钉（终审 P0-3/P0-4）
# 四族+revision 已从 _DETECTORS 摘除（槽位归属未证前停用，交判官）；
# 函数与语义钉保留备日后槽位对齐版回归——本节直射函数级（不经
# detect_core_conflict），证明函数判据未漂移；复位入 _DETECTORS 前以
# 本节为语义基准。

_RETIRED_POSITIVE_CASES = [
    ("unit", cc._detect_unit,
     "甲公司9月24日报价6612.30美元。", "甲公司9月24日报价6612.30点。",
     "unit"),
    ("time", cc._detect_time,
     "甲公司9月24日公告营收100万元。", "甲公司9月25日公告营收100万元。",
     "time"),
    ("stage", cc._detect_stage,
     "甲公司开盘价100元。", "甲公司收盘价100元。", "stage"),
    ("polarity", cc._detect_polarity,
     "甲公司股价上涨5%。", "甲公司股价下跌5%。", "polarity"),
    ("revision", cc._detect_revision,
     "甲公司公告净利177.9万元。", "甲公司公告：净利由177.9万修正为177.5万。",
     "revision"),
]


@pytest.mark.parametrize(
    "case_id,fn,h_text,c_text,ctype", _RETIRED_POSITIVE_CASES,
    ids=[c[0] for c in _RETIRED_POSITIVE_CASES])
def test_retired_detector_family_semantics_pinned(
        case_id, fn, h_text, c_text, ctype):
    """终审 P0-3/P0-4：已摘除族函数语义钉（函数保留在案）——直射调用
    仍按原判据命中（含双侧证据绑真 offset）；复位前判据不漂移。"""
    hit = fn(h_text, c_text, H_ID, C_ID)
    assert hit is not None, case_id
    assert hit.has_conflict is True
    assert hit.conflict_type == ctype
    assert hit.field_path == f"core_conflict.{ctype}"
    assert h_text[hit.history_evidence.start:
                  hit.history_evidence.end] == hit.history_evidence.quote
    assert c_text[hit.current_evidence.start:
                  hit.current_evidence.end] == hit.current_evidence.quote


def test_retired_families_absent_from_active_detectors():
    """终审 P0-3/P0-4：五族确实不在 _DETECTORS——原五族正例样本经
    detect_core_conflict 一律 NO_CONFLICT（停用，交判官），与函数级
    语义钉双面互证（函数在案≠现役）。"""
    assert cc._detect_unit not in cc._DETECTORS
    assert cc._detect_time not in cc._DETECTORS
    assert cc._detect_stage not in cc._DETECTORS
    assert cc._detect_polarity not in cc._DETECTORS
    assert cc._detect_revision not in cc._DETECTORS
    for h_text, c_text in (
            ("甲公司9月24日报价6612.30美元。", "甲公司9月24日报价6612.30点。"),
            ("甲公司9月24日公告营收100万元。", "甲公司9月25日公告营收100万元。"),
            ("甲公司开盘价100元。", "甲公司收盘价100元。"),
            ("甲公司股价上涨5%。", "甲公司股价下跌5%。"),
            ("甲公司公告净利177.9万元。",
             "甲公司公告：净利由177.9万修正为177.5万。")):
        hit = cc.detect_core_conflict(
            h_text, c_text, history_record_id=H_ID, current_record_id=C_ID)
        assert hit.has_conflict is False, (h_text, c_text)


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


# —— 终审 P0-1：legacy 全等回归钉（模式闸专项） ——

def test_p0_1_legacy_mode_hard_conflict_layer_fully_disabled(monkeypatch):
    """终审 P0-1 legacy 全等回归钉：legacy_proof_gate（env 缺席默认）下
    硬冲突前置层**整层不执行**——detect_core_conflict 零调用（间谍反证）、
    硬冲突对照常进判官（双序实调）、判官结论即终态，端到端公共五字段
    与基线 2d0d418 判定完全一致（基线无前置拦截层，判官双序重复→
    FACT_EQUIVALENT/基线文案；双序证伪不重复→VERIFIED_CONFLICT/基线
    文案）；前置拦截计数为零。"""
    monkeypatch.delenv(judge_pair.JUDGE_DECISION_MODE_ENV, raising=False)
    spy: list = []

    def _spy_detect(*args, **kwargs):
        spy.append(1)                     # 任何调用都是模式闸失效
        return cc.NO_CONFLICT

    monkeypatch.setattr(decide_service.core_conflict_module,
                        "detect_core_conflict", _spy_detect)
    # 硬冲突对（semantic 下会被前置层拦截的 numeric 同槽位对）
    num_h, num_c = "甲公司9月24日公告营收787.8万元。", "甲公司9月24日公告营收789.8万元。"
    # ① 判官双序重复 → 基线：重复/FACT_EQUIVALENT/基线文案公共理由
    judge = _judge(lambda ctx: _make_proof(ctx, verdict="duplicate"))
    out = _decide(num_h, "甲公司", num_c, "甲公司", judge)
    assert spy == []                      # 前置层零调用（模式闸关死）
    assert judge.calls == ["ab", "ba"]    # 判官照常被调用（基线行为）
    assert out.decision == "重复"
    assert out.duplicate_ids == ("item-A",)
    assert out.internal_code == "FACT_EQUIVALENT"
    assert out.pair_results[0].code == "FACT_EQUIVALENT"
    assert out.reason == (
        "判官双序一致判定同一事实，引文已绑定双侧原文且机器验 gate 通过。")
    assert out.to_public_dict() == {
        "item_id": "item-C", "text": num_c, "decision": "重复",
        "duplicate_ids": ["item-A"],
        "reason": "判官双序一致判定同一事实，引文已绑定双侧原文且机器验 gate 通过。"}
    assert "judge.core_conflict.intercepted" not in out.judge_diagnostics
    # ② 硬冲突主体对（代码互斥）在 legacy 下同样进判官：判官双序重复
    # →基线重复（证明语义收窄只影响 semantic 面，不渗入 legacy 面）
    sub_h, sub_c = "甲公司（600001）公告营收100万元。", "甲公司（600002）公告营收100万元。"
    judge2 = _judge(lambda ctx: _make_proof(ctx, verdict="duplicate"))
    out2 = _decide(sub_h, "甲公司", sub_c, "甲公司", judge2)
    assert spy == []
    assert judge2.calls == ["ab", "ba"]
    assert out2.decision == "重复"
    assert out2.internal_code == "FACT_EQUIVALENT"


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
# 版本装配单源化 + RunManifest 如实登记生效版本 + 模式分发（一期
# v6-lite 起 semantic_authority→judge_v6+policy_v4）

def test_version_config_prompt_policy_mapping():
    """复审条 1：统一版本配置对象 prompt→policy 映射——judge_v1/v2/v3→
    policy_v2（各自原 policy=judge_proof 证明门控宪章版）；judge_v5→
    policy_v3；judge_v6→policy_v4（一期 v6-lite 登记）；未注册 prompt/
    未登记映射一律 fail-closed。"""
    for v in ("judge_v1", "judge_v2", "judge_v3"):
        vc = jvc.judge_version_for_prompt(v)
        assert vc.policy_version == "policy_v2"
        assert vc.policy_version == judge_proof.PROOF_POLICY_VERSION
        assert vc.prompt_sha256 == lr.judge_prompt_for_version(v)[1]
        assert vc.decision_mode == ""
    vc5 = jvc.judge_version_for_prompt("judge_v5")
    assert vc5.policy_version == "policy_v3"
    vc6 = jvc.judge_version_for_prompt("judge_v6")
    assert vc6.policy_version == "policy_v4"
    with pytest.raises(ValueError):                 # 未注册 prompt
        jvc.judge_version_for_prompt("judge_v4")


def test_version_config_mode_dispatch_and_env_default():
    """复审条 3：judge_v5 不得无条件默认——模式分发表 legacy_proof_gate
    （默认）→judge_v1+policy_v2；semantic_authority→judge_v6+policy_v4
    （一期 v6-lite 起映射 v6，2026-10-11 分支 p3-v6-phase1；此前为
    judge_v5+policy_v3）；非法模式 fail-closed；缺省读 env（缺席/空串=
    legacy）。"""
    vc_legacy = jvc.judge_version_for_mode(judge_pair.MODE_LEGACY_PROOF_GATE)
    assert (vc_legacy.prompt_version, vc_legacy.policy_version) == (
        "judge_v1", "policy_v2")
    assert vc_legacy.decision_mode == "legacy_proof_gate"
    vc_sem = jvc.judge_version_for_mode(judge_pair.MODE_SEMANTIC_AUTHORITY)
    assert (vc_sem.prompt_version, vc_sem.policy_version) == (
        "judge_v6", "policy_v4")
    assert vc_sem.decision_mode == "semantic_authority"
    with pytest.raises(ValueError):
        jvc.judge_version_for_mode("banana")
    # 缺省 env 分发（缺席=legacy 默认；空串同 legacy）
    assert jvc.default_judge_version({}).prompt_version == "judge_v1"
    assert jvc.default_judge_version(
        {"DEDUP_JUDGE_DECISION_MODE": ""}).prompt_version == "judge_v1"
    assert jvc.default_judge_version(
        {"DEDUP_JUDGE_DECISION_MODE": "semantic_authority"}
    ).prompt_version == "judge_v6"
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
            ("semantic_authority", "judge_v6", "policy_v4")):
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


# ============================================================ 9. 终审 P1-1 单源传递钉测
# JudgeVersionConfig 在 decide_for_task 入口创建一次，依次传给判官装配
# （build_judge_callable/proof_for_order）、decide 服务、cache key 素材、
# run manifest 登记与审计记录——修掉"显式 judge_decision_mode 实参与
# adapter/manifest 各读环境变量"的分裂（显式实参语义权威）。

def test_p1_1_single_source_version_config_explicit_arg(monkeypatch):
    """终审 P1-1 钉测：env 空+显式实参 semantic_authority → decide_for_task
    入口创建 JudgeVersionConfig 一次（judge_v6/policy_v4——一期 v6-lite 起
    semantic 映射 v6），adapter/manifest/audit 三处版本一致。判官全假件
    零真 API（装配间谍+罐装 call_fn；真装配件被间谍替换的段不触 LLM）。"""
    import json as _json
    monkeypatch.delenv(judge_pair.JUDGE_DECISION_MODE_ENV, raising=False)

    # （a）入口创建一次并传给判官装配：装配间谍捕获同一 vc（返回 None
    # =未注入 fail-closed，绝不触真 LLM）
    captured = {}

    def _spy_build(*, judge=None, config=None, budget=None,
                   environ=None, version_config=None):
        captured["version_config"] = version_config
        return None

    monkeypatch.setattr(decide_service.judge_adapter_module,
                        "build_judge_callable", _spy_build)
    t_h, t_c = "甲公司9月24日公告营收100万元。", "甲公司公告：9月24日营收100万元。"
    history = _history(t_h, "甲公司")
    current = _current(t_c, "甲公司")
    out = decide_service.decide_for_task(
        history, [], current=current, judge_callable=None,
        judge_in_chain=True, coverage_complete=True,
        judge_decision_mode="semantic_authority")
    vc = out.judge_version_config
    assert vc is not None
    assert (vc.prompt_version, vc.policy_version, vc.decision_mode) == (
        "judge_v6", "policy_v4", "semantic_authority")
    assert captured["version_config"] is vc       # 判官装配收到入口同一 vc
    assert out.decision == "边界case/疑难case"     # 未注入 fail-closed（不冒签）
    # 内部审计面外露，绝不进公共五字段
    assert "judge_version_config" not in out.to_public_dict()
    monkeypatch.undo()                            # 恢复真装配件（env 复原）

    # （b）adapter：同一 vc 装配罐装真件（call_fn 罐装，零真 API）→
    # 证明 prompt_sha256/policy/cache key 素材全部同源自 v6/policy_v4
    resp = _json.dumps({
        "decision": "重复", "reason": "r",
        "evidence_a": ["100万元"], "evidence_b": ["100万元"],
        "numeric_check": {"conclusion": "一致", "numbers_a": ["100万"],
                          "numbers_b": ["100万"]},
        "time_check": {"conclusion": "一致", "times_a": ["9月24日"],
                       "times_b": ["9月24日"]},
    }, ensure_ascii=False)
    cfg = judge_adapter.default_judge_config(None, version_config=vc)
    assert cfg.prompt_version == "judge_v6"       # adapter 不再回落 env 读 v1
    judge = lr.SyncResidualJudge(
        lr.ResidualJudgeConfig(prompt_version=cfg.prompt_version),
        call_fn=lambda model, system, user, timeout_s=None: (resp, 0.01))
    cb = judge_adapter.build_judge_callable(
        judge=judge, version_config=vc,
        environ={"DEDUP_JUDGE_PROOF": "1"})
    assert cb is not None
    ctx = {"pair_id": "p-p11", "order": "ab",
           "item_a_id": "item-A", "item_b_id": "item-C",
           "record_a_id": H_ID, "record_b_id": C_ID,
           "text_a": t_h, "text_b": t_c}
    proof = cb(ctx)
    assert proof["prompt_sha256"] == vc.prompt_sha256   # v6 提示词摘要
    assert proof["policy_version"] == vc.policy_version == "policy_v4"
    assert proof["cache_key"] == judge_pair.compute_cache_key(
        proof["model_version"], vc.prompt_sha256, vc.policy_version,
        "ab", proof["text_a_sha256"], proof["text_b_sha256"])

    # （c）manifest：登记同一 vc（env 空不回落 env 读值）
    manifest = rm.build_run_manifest(
        inputs=("rec-a",), embedding_space="fake_space",
        code_git_sha=_GIT_SHA0, judge_version_config=vc)
    assert manifest.prompt_version == "judge_v6" == vc.prompt_version
    assert manifest.prompt_sha256 == vc.prompt_sha256
    assert manifest.policy_version == "policy_v4" == vc.policy_version

    # （d）audit：对级 PairResult→AuditRecord 的 judge_decision_mode 与
    # 入口 vc 同源（semantic_authority）；mode→版本映射单源自证
    # （jvc.judge_version_for_mode(audit mode) == 入口 vc）
    judge2 = _judge(lambda ctx: _make_proof(ctx, verdict="duplicate"))
    out2 = decide_service.decide_for_task(
        _history(t_h, "甲公司"), [], current=_current(t_c, "甲公司"),
        judge_callable=judge2, judge_in_chain=True, coverage_complete=True,
        judge_decision_mode="semantic_authority")
    assert out2.decision == "重复"
    assert out2.judge_version_config == vc
    assert out2.pair_results[0].judge_decision_mode == \
        vc.decision_mode == "semantic_authority"
    ctx2 = CommitContext(
        scope_id="default", business_date="2026-09-26", arrival_seq=3,
        current=current, candidates=(history,), visible_seq=10,
        prepared_seq=10, coverage_complete=True)
    plan = build_commit_write_plan(ctx2, out2, audit_complete=True)
    record = plan.audit_batch.records[0]
    assert record.judge_decision_mode == vc.decision_mode
    assert jvc.judge_version_for_mode(
        record.judge_decision_mode) == vc        # audit mode→版本单源一致


def test_p1_1_version_config_param_beats_env(monkeypatch):
    """终审 P1-1 补钉：env=semantic_authority+显式实参 legacy_proof_gate
    →入口 vc=judge_v1/policy_v2（显式实参语义权威，双向对拍 §9 主钉）。"""
    monkeypatch.setenv(judge_pair.JUDGE_DECISION_MODE_ENV, "semantic_authority")
    out = decide_service.decide_for_task(
        _history("甲公司公告回购股份。", "甲公司"), [],
        current=_current("甲公司发布半年度财报。", "甲公司"),
        judge_callable=_judge(lambda ctx: _make_proof(
            ctx, verdict="not_duplicate", with_falsification=False)),
        judge_in_chain=True, coverage_complete=True,
        judge_decision_mode="legacy_proof_gate")
    vc = out.judge_version_config
    assert (vc.prompt_version, vc.policy_version, vc.decision_mode) == (
        "judge_v1", "policy_v2", "legacy_proof_gate")


# ============================================================ 9-bis. 终审复审补丁钉测
# P1-1 未核销小补丁（唯一权威，冲突即错）：JudgeVersionConfig 为唯一权威
# ——同传 mode+config 冲突报错；config 内部固定映射不匹配报错；v1 判官
# 配 v5 config 报错；完全一致配置通过。判官全假件零真 API。

def test_review_mode_config_conflict_raises(monkeypatch):
    """复审钉测①：judge_decision_mode=semantic_authority 与
    judge_version_config=legacy(v1) 同传 → ValueError（唯一权威，冲突
    即错，禁静默选边——防"实际合并模式=新、提示词/policy=旧"架空）。"""
    monkeypatch.delenv(judge_pair.JUDGE_DECISION_MODE_ENV, raising=False)
    legacy_vc = jvc.judge_version_for_mode("legacy_proof_gate")
    with pytest.raises(ValueError, match="不一致"):
        decide_service.decide_for_task(
            _history("甲公司公告回购股份。", "甲公司"), [],
            current=_current("甲公司发布半年度财报。", "甲公司"),
            judge_callable=_judge(lambda ctx: _make_proof(
                ctx, verdict="duplicate")),
            judge_in_chain=True, coverage_complete=True,
            judge_decision_mode="semantic_authority",
            judge_version_config=legacy_vc)
    # 反向同理：legacy 模式配 semantic v5 config → 报错
    semantic_vc = jvc.judge_version_for_mode("semantic_authority")
    with pytest.raises(ValueError, match="不一致"):
        decide_service.decide_for_task(
            _history("甲公司公告回购股份。", "甲公司"), [],
            current=_current("甲公司发布半年度财报。", "甲公司"),
            judge_callable=_judge(lambda ctx: _make_proof(
                ctx, verdict="duplicate")),
            judge_in_chain=True, coverage_complete=True,
            judge_decision_mode="legacy_proof_gate",
            judge_version_config=semantic_vc)


def test_review_config_internal_fixed_mapping_mismatch_raises():
    """复审钉测②：JudgeVersionConfig 构造时固定映射校验——
    legacy_proof_gate→judge_v1+policy_v2、semantic_authority→
    judge_v6+policy_v4（一期 v6-lite 起映射 v6）；mode/prompt/policy
    任一维不匹配即 ValueError（fail-closed，绝不静默）。prompt_sha256
    一律用注册表真实哈希（全量校验后占位哈希在构造器即拒——见 §9-ter
    专项钉）。"""
    sha_v1 = lr.judge_prompt_for_version("judge_v1")[1]
    sha_v5 = lr.judge_prompt_for_version("judge_v5")[1]
    # 语义模式配 v1 提示词 → 拒
    with pytest.raises(ValueError, match="固定映射冲突"):
        jvc.JudgeVersionConfig(
            prompt_version="judge_v1",
            prompt_sha256=sha_v1,
            policy_version="policy_v2",
            decision_mode="semantic_authority")
    # 语义模式配 policy_v2（提示词对）→ 拒（全量校验 ③ 先拦：v5 提示词
    # 固定策略=policy_v3，不待模式映射即 fail-closed）
    with pytest.raises(ValueError, match="固定策略不符"):
        jvc.JudgeVersionConfig(
            prompt_version="judge_v5",
            prompt_sha256=sha_v5,
            policy_version="policy_v2",
            decision_mode="semantic_authority")
    # legacy 模式配 v5 提示词 → 拒
    with pytest.raises(ValueError, match="固定映射冲突"):
        jvc.JudgeVersionConfig(
            prompt_version="judge_v5",
            prompt_sha256=sha_v5,
            policy_version="policy_v3",
            decision_mode="legacy_proof_gate")
    # 语义模式配 judge_v5+policy_v3（v5 自洽但模式现值=v6）→ 拒
    # （一期 v6-lite：semantic 映射 v6，v5 仅 prompt 直解回放通道可达）
    with pytest.raises(ValueError, match="固定映射冲突"):
        jvc.JudgeVersionConfig(
            prompt_version="judge_v5",
            prompt_sha256=sha_v5,
            policy_version="policy_v3",
            decision_mode="semantic_authority")
    # 非法模式字面量 → 拒（构造器同闸报非法模式）
    with pytest.raises(ValueError, match="非法"):
        jvc.JudgeVersionConfig(
            prompt_version="judge_v1",
            prompt_sha256=sha_v1,
            policy_version="policy_v2",
            decision_mode="banana")
    # decision_mode=空串（按 prompt 直解路径，回放/考试通道）不受固定
    # 映射约束——v2/v3 提示词配注册表真实哈希合法在案
    sha_v2 = lr.judge_prompt_for_version("judge_v2")[1]
    ok = jvc.JudgeVersionConfig(
        prompt_version="judge_v2",
        prompt_sha256=sha_v2,
        policy_version="policy_v2")
    assert ok.decision_mode == ""
    assert ok.prompt_sha256 == sha_v2


def test_review_v1_judge_with_semantic_config_raises(monkeypatch):
    """复审钉测③：v1 判官（显式 judge.config.prompt_version=judge_v1）
    配 semantic version_config（一期 v6-lite 起为 v6/policy_v4）→
    build_judge_callable 装配即 ValueError（判官实际 v1、证明可记成
    v6/policy_v4 的架空形态在装配层拦死）；proof_for_order 对实际生效
    配置再对拍一次（双闸）。"""
    import json as _json
    monkeypatch.delenv(judge_pair.JUDGE_DECISION_MODE_ENV, raising=False)
    semantic_vc = jvc.judge_version_for_mode("semantic_authority")
    resp = _json.dumps({
        "decision": "重复", "reason": "r",
        "evidence_a": ["100万元"], "evidence_b": ["100万元"],
        "numeric_check": {"conclusion": "一致", "numbers_a": ["100万"],
                          "numbers_b": ["100万"]},
        "time_check": {"conclusion": "一致", "times_a": ["9月24日"],
                       "times_b": ["9月24日"]},
    }, ensure_ascii=False)
    # v1 判官（罐装 call_fn，零真 API）
    v1_judge = lr.SyncResidualJudge(
        lr.ResidualJudgeConfig(prompt_version="judge_v1"),
        call_fn=lambda model, system, user, timeout_s=None: (resp, 0.01))
    with pytest.raises(ValueError, match="不一致"):
        judge_adapter.build_judge_callable(
            judge=v1_judge, version_config=semantic_vc,
            environ={"DEDUP_JUDGE_PROOF": "1"})
    # 显式 config 实参同闸：v1 ResidualJudgeConfig 配 semantic vc → 拒
    with pytest.raises(ValueError, match="不一致"):
        judge_adapter.build_judge_callable(
            config=lr.ResidualJudgeConfig(prompt_version="judge_v1"),
            version_config=semantic_vc,
            environ={"DEDUP_JUDGE_PROOF": "1"})
    # proof_for_order 直射双闸：v1 判官+semantic vc（v6）→ 拒（证明
    # 元数据与 cache_key 必须记判官实际生效版本）
    ctx = {"pair_id": "p-v1v5", "order": "ab",
           "item_a_id": "item-A", "item_b_id": "item-C",
           "record_a_id": H_ID, "record_b_id": C_ID,
           "text_a": "甲公司公告营收100万元。",
           "text_b": "甲公司公告称，营收为100万元。"}
    with pytest.raises(ValueError, match="实际生效"):
        judge_adapter.proof_for_order(v1_judge, ctx, version_config=semantic_vc)


def test_review_consistent_config_passes_end_to_end(monkeypatch):
    """复审钉测④：完全一致配置通过——mode+config 同传一致（semantic
    authority+vc(v6/policy_v4，一期 v6-lite 起映射 v6)）→ 入口接受单源
    config；v6 判官+同源 v6 config → 装配/证明/cache_key 全同源（判官
    罐装零真 API）；v1 判官+同源 v1 config → 同过。coordinator/worker
    预装配同配置一路传入（装配与判定同源）。"""
    import json as _json
    monkeypatch.delenv(judge_pair.JUDGE_DECISION_MODE_ENV, raising=False)
    semantic_vc = jvc.judge_version_for_mode("semantic_authority")
    t_h, t_c = "甲公司9月24日公告营收100万元。", "甲公司公告：9月24日营收100万元。"

    # （i）入口同传一致 → 接受，vc 即单源（装配间谍捕获同一 vc）
    captured = {}

    def _spy_build(*, judge=None, config=None, budget=None,
                   environ=None, version_config=None):
        captured["version_config"] = version_config
        return None

    monkeypatch.setattr(decide_service.judge_adapter_module,
                        "build_judge_callable", _spy_build)
    out = decide_service.decide_for_task(
        _history(t_h, "甲公司"), [], current=_current(t_c, "甲公司"),
        judge_callable=None, judge_in_chain=True, coverage_complete=True,
        judge_decision_mode="semantic_authority",
        judge_version_config=semantic_vc)
    assert out.judge_version_config is semantic_vc
    assert captured["version_config"] is semantic_vc
    monkeypatch.undo()

    # （ii）v6 判官+同源 v6 config → 装配过、证明四维+cache_key 同源
    resp = _json.dumps({
        "decision": "重复", "reason": "r",
        "evidence_a": ["100万元"], "evidence_b": ["100万元"],
        "numeric_check": {"conclusion": "一致", "numbers_a": ["100万"],
                          "numbers_b": ["100万"]},
        "time_check": {"conclusion": "一致", "times_a": ["9月24日"],
                       "times_b": ["9月24日"]},
    }, ensure_ascii=False)
    v6_judge = lr.SyncResidualJudge(
        lr.ResidualJudgeConfig(prompt_version="judge_v6"),
        call_fn=lambda model, system, user, timeout_s=None: (resp, 0.01))
    cb = judge_adapter.build_judge_callable(
        judge=v6_judge, version_config=semantic_vc,
        environ={"DEDUP_JUDGE_PROOF": "1"})
    assert cb is not None
    ctx = {"pair_id": "p-v6ok", "order": "ab",
           "item_a_id": "item-A", "item_b_id": "item-C",
           "record_a_id": H_ID, "record_b_id": C_ID,
           "text_a": t_h, "text_b": t_c}
    proof = cb(ctx)
    assert proof["prompt_sha256"] == semantic_vc.prompt_sha256
    assert proof["policy_version"] == semantic_vc.policy_version
    assert proof["cache_key"] == judge_pair.compute_cache_key(
        proof["model_version"], semantic_vc.prompt_sha256,
        semantic_vc.policy_version,
        "ab", proof["text_a_sha256"], proof["text_b_sha256"])

    # （iii）v1 判官+同源 v1 config（legacy_vc）→ 装配/证明同过同源
    legacy_vc = jvc.judge_version_for_mode("legacy_proof_gate")
    v1_judge = lr.SyncResidualJudge(
        lr.ResidualJudgeConfig(prompt_version="judge_v1"),
        call_fn=lambda model, system, user, timeout_s=None: (resp, 0.01))
    cb1 = judge_adapter.build_judge_callable(
        judge=v1_judge, version_config=legacy_vc,
        environ={"DEDUP_JUDGE_PROOF": "1"})
    assert cb1 is not None
    proof1 = cb1(ctx)
    assert proof1["prompt_sha256"] == legacy_vc.prompt_sha256
    assert proof1["policy_version"] == legacy_vc.policy_version

    # （iv）coordinator 预装配单源：commit_one 传 judge_version_config
    # （装配间谍捕获）+服务入口收到同一对象（间谍 decide_for_task）
    asm_captured = {}

    def _spy_asm_build(*, judge=None, config=None, budget=None,
                       environ=None, version_config=None):
        asm_captured["version_config"] = version_config
        return None

    svc_captured = {}

    def _spy_decide(*args, **kwargs):
        svc_captured["config"] = kwargs.get("judge_version_config")
        return out    # 复用（i）终态（重复），装配/入参面已断言即可

    monkeypatch.setattr(decide_service.judge_adapter_module,
                        "build_judge_callable", _spy_asm_build)
    monkeypatch.setattr(decide_service, "decide_for_task", _spy_decide)
    from news_flash_dedup.commit.coordinator import (
        CommitContext as _CC, commit_one as _commit_one)
    store = FakeCommitStore()
    _ctx_kw = dict(
        scope_id="default", business_date="2026-09-26", arrival_seq=3,
        current=_current(t_c, "甲公司"),
        candidates=(_history(t_h, "甲公司"),),
        visible_seq=10, prepared_seq=10, coverage_complete=True)
    outcome = _commit_one(_CC(**_ctx_kw), store, audit_complete=True,
                          judge_version_config=semantic_vc)
    assert asm_captured["version_config"] is semantic_vc
    assert svc_captured["config"] is semantic_vc
    assert outcome.state == "committed"
    monkeypatch.undo()

    # （v）worker 预装配同型：shadow_decide 传同一配置（装配/服务同源）
    from news_flash_dedup.recall import worker as recall_worker
    shadow_asm: dict = {}

    def _spy_shadow_asm(*, judge=None, config=None, budget=None,
                        environ=None, version_config=None):
        shadow_asm["version_config"] = version_config
        return None

    shadow_svc: dict = {}

    def _spy_shadow_decide(*args, **kwargs):
        shadow_svc["config"] = kwargs.get("judge_version_config")
        return out

    monkeypatch.setattr(recall_worker.judge_adapter_module,
                        "build_judge_callable", _spy_shadow_asm)
    monkeypatch.setattr(recall_worker.decide_service, "decide_for_task",
                        _spy_shadow_decide)
    shadow_out = recall_worker.shadow_decide(
        _current(t_c, "甲公司"), (_history(t_h, "甲公司"),),
        coverage_complete=True, visible_seq=10, prepared_seq=10,
        judge_version_config=semantic_vc)
    assert shadow_asm["version_config"] is semantic_vc
    assert shadow_svc["config"] is semantic_vc
    assert shadow_out is out


# ============================================================ 9-ter. 终审复审第二轮钉测
# P1-1 复审第二轮三条缺口（全量校验+manifest 防覆盖+空模式生产隔离）：
# 判官全假件零真 API（罐装双序假证明/装配间谍，无任何真 LLM 调用）。

def test_review2_correct_mode_wrong_sha_rejected():
    """复审第二轮钉测①：正确模式+错 SHA 拒——semantic_authority 模式
    配 judge_v6+policy_v4（模式/策略全对，一期 v6-lite 起映射 v6）但
    prompt_sha256 为全零/错值 → 构造即 ValueError（全量校验 ②：哈希=
    注册表真实值，占位哈希不得冒充合法配置）。"""
    sha_v6 = lr.judge_prompt_for_version("judge_v6")[1]
    # 全零哈希（复审复现件）
    with pytest.raises(ValueError, match="注册表真实哈希不符"):
        jvc.JudgeVersionConfig(
            prompt_version="judge_v6",
            prompt_sha256="0" * 64,
            policy_version="policy_v4",
            decision_mode="semantic_authority")
    # 错值哈希（非全零但不同于注册表）
    wrong_sha = ("f" if sha_v6[0] != "f" else "e") + sha_v6[1:]
    with pytest.raises(ValueError, match="注册表真实哈希不符"):
        jvc.JudgeVersionConfig(
            prompt_version="judge_v6",
            prompt_sha256=wrong_sha,
            policy_version="policy_v4",
            decision_mode="semantic_authority")
    # 真实哈希+全对 → 过（对照）
    ok = jvc.JudgeVersionConfig(
        prompt_version="judge_v6",
        prompt_sha256=sha_v6,
        policy_version="policy_v4",
        decision_mode="semantic_authority")
    assert ok.prompt_sha256 == sha_v6


def test_review2_empty_mode_wrong_policy_or_sha_rejected():
    """复审第二轮钉测②：空模式+错 policy/SHA 拒——decision_mode=""
    只豁免"模式↔提示词映射"，不豁免哈希与策略校验；空模式带错
    policy（prompt→固定策略不符）或全零/错哈希 → 构造即 ValueError；
    未注册 prompt（v4）同样 fail-closed。"""
    sha_v2 = lr.judge_prompt_for_version("judge_v2")[1]
    # 空模式+全零哈希 → 拒（复审复现件：全零哈希冒充合法配置）
    with pytest.raises(ValueError, match="注册表真实哈希不符"):
        jvc.JudgeVersionConfig(
            prompt_version="judge_v2",
            prompt_sha256="0" * 64,
            policy_version="policy_v2")
    # 空模式+错 policy（v2 提示词配 policy_v3）→ 拒
    with pytest.raises(ValueError, match="固定策略不符"):
        jvc.JudgeVersionConfig(
            prompt_version="judge_v2",
            prompt_sha256=sha_v2,
            policy_version="policy_v3")
    # 空模式+未注册 prompt（judge_v4）→ 拒（注册表 fail-closed）
    with pytest.raises(ValueError, match="未知"):
        jvc.JudgeVersionConfig(
            prompt_version="judge_v4",
            prompt_sha256=sha_v2,
            policy_version="policy_v2")
    # 空模式+全对 → 过（对照：真实哈希+固定策略）
    ok = jvc.JudgeVersionConfig(
        prompt_version="judge_v2",
        prompt_sha256=sha_v2,
        policy_version="policy_v2")
    assert ok.decision_mode == ""


def test_review2_manifest_explicit_conflict_with_config_rejected():
    """复审第二轮钉测③：manifest 显式字段与 config 冲突拒——
    build_run_manifest 传 judge_version_config 后，显式 prompt_version/
    prompt_sha256/policy_version 只允许不传或与 config 完全一致；任何
    不一致 → ValueError（防覆盖：不得"传 v5 config 却显式登记 v1"）。"""
    semantic_vc = jvc.judge_version_for_mode("semantic_authority")
    kw = dict(inputs=("rec-a",), embedding_space="fake_space",
              code_git_sha=_GIT_SHA0)
    # 三维逐一冲突 → 各自拒绝
    with pytest.raises(ValueError, match="不一致"):
        rm.build_run_manifest(
            prompt_version="judge_v1",            # 冲突：config=v5
            judge_version_config=semantic_vc, **kw)
    with pytest.raises(ValueError, match="不一致"):
        rm.build_run_manifest(
            prompt_sha256="0" * 64,                # 冲突：config=真实 v5 sha
            judge_version_config=semantic_vc, **kw)
    with pytest.raises(ValueError, match="不一致"):
        rm.build_run_manifest(
            policy_version="policy_v2",            # 冲突：config=policy_v3
            judge_version_config=semantic_vc, **kw)
    # 与 config 完全一致的显式实参 → 放行（显式确认语义）
    m_eq = rm.build_run_manifest(
        prompt_version=semantic_vc.prompt_version,
        prompt_sha256=semantic_vc.prompt_sha256,
        policy_version=semantic_vc.policy_version,
        judge_version_config=semantic_vc, **kw)
    assert m_eq.prompt_version == semantic_vc.prompt_version
    assert m_eq.prompt_sha256 == semantic_vc.prompt_sha256
    assert m_eq.policy_version == semantic_vc.policy_version
    # 不传显式 → 从 config 同源（原 §9 行为不破）
    m_src = rm.build_run_manifest(
        judge_version_config=semantic_vc, **kw)
    assert m_src.prompt_version == semantic_vc.prompt_version
    assert m_src.prompt_sha256 == semantic_vc.prompt_sha256
    assert m_src.policy_version == semantic_vc.policy_version
    # config 缺席时显式实参路径原样（回放走单独回放接口的语义面）
    m_plain = rm.build_run_manifest(
        prompt_version="judge_v1", code_git_sha=_GIT_SHA0,
        inputs=("rec-a",), embedding_space="fake_space")
    assert m_plain.prompt_version == "judge_v1"


def test_review2_empty_mode_config_rejected_in_production(monkeypatch):
    """复审第二轮钉测④：生产入口收空模式 config 拒/显式回放模式放行
    ——decide_for_task 收 decision_mode="" 的 config（按 prompt 直解、
    v2 提示词形态）：allow_prompt_direct 缺省 False → ValueError（生产
    拒绝）；显式传 True（回放/考试工具专属）→ 放行。"""
    monkeypatch.delenv(judge_pair.JUDGE_DECISION_MODE_ENV, raising=False)
    replay_vc = jvc.judge_version_for_prompt("judge_v2")
    assert replay_vc.decision_mode == ""
    # 生产入口（缺省 flag=False）→ 拒
    with pytest.raises(ValueError, match="拒绝空模式"):
        decide_service.decide_for_task(
            _history("甲公司公告回购股份。", "甲公司"), [],
            current=_current("甲公司发布半年度财报。", "甲公司"),
            judge_callable=_judge(lambda ctx: _make_proof(
                ctx, verdict="duplicate")),
            judge_in_chain=True, coverage_complete=True,
            judge_version_config=replay_vc)
    # 显式回放模式（flag=True）→ 放行
    out = decide_service.decide_for_task(
        _history("甲公司公告回购股份。", "甲公司"), [],
        current=_current("甲公司发布半年度财报。", "甲公司"),
        judge_callable=_judge(lambda ctx: _make_proof(
            ctx, verdict="duplicate")),
        judge_in_chain=True, coverage_complete=True,
        judge_version_config=replay_vc,
        allow_prompt_direct=True)
    assert out.decision == "重复"
    assert out.judge_version_config is replay_vc


def test_review2_replay_merge_mode_explicit_and_auditable(monkeypatch):
    """复审第二轮钉测⑤：回放 v2/v3 时实际合并模式显式稳定可审计
    ——allow_prompt_direct=True 放行空模式 config 后，生效合并模式经
    标准单源链显式确定（env→默认 legacy_proof_gate），写入
    active_decision_mode 并流经 PairResult→AuditRecord（端到端 commit
    写入计划），不得留空串；env=semantic_authority 时同样显式稳定
    （可复现、可审计）。"""
    monkeypatch.delenv(judge_pair.JUDGE_DECISION_MODE_ENV, raising=False)
    t_h, t_c = "甲公司9月24日公告营收100万元。", "甲公司公告：9月24日营收100万元。"
    replay_vc = jvc.judge_version_for_prompt("judge_v2")

    # （i）env 缺席 → 显式确定 legacy_proof_gate（默认），审计留痕
    judge = _judge(lambda ctx: _make_proof(ctx, verdict="duplicate"))
    history = _history(t_h, "甲公司")
    current = _current(t_c, "甲公司")
    out = decide_service.decide_for_task(
        history, [], current=current, judge_callable=judge,
        judge_in_chain=True, coverage_complete=True,
        judge_version_config=replay_vc, allow_prompt_direct=True)
    assert out.judge_version_config is replay_vc
    pair = out.pair_results[0]
    assert pair.judge_decision_mode == "legacy_proof_gate"   # 显式非空
    record = audit_module.build_audit_record(pair)
    assert record.judge_decision_mode == "legacy_proof_gate"
    assert "diagnostics_hash" in record.to_doc()
    # 端到端：commit 写入计划审计文档同样留痕（稳定可审计）
    ctx = CommitContext(
        scope_id="default", business_date="2026-09-26", arrival_seq=3,
        current=current, candidates=(history,), visible_seq=10,
        prepared_seq=10, coverage_complete=True)
    plan = build_commit_write_plan(ctx, out, audit_complete=True)
    plan_record = plan.audit_batch.records[0]
    assert plan_record.judge_decision_mode == "legacy_proof_gate"

    # （ii）env=semantic_authority → 显式确定 semantic_authority（可复现）
    monkeypatch.setenv(judge_pair.JUDGE_DECISION_MODE_ENV,
                       "semantic_authority")
    judge_sem = _judge(lambda ctx: _make_proof(ctx, verdict="duplicate"))
    out_sem = decide_service.decide_for_task(
        history, [], current=current, judge_callable=judge_sem,
        judge_in_chain=True, coverage_complete=True,
        judge_version_config=replay_vc, allow_prompt_direct=True)
    pair_sem = out_sem.pair_results[0]
    assert pair_sem.judge_decision_mode == "semantic_authority"
    record_sem = audit_module.build_audit_record(pair_sem)
    assert record_sem.judge_decision_mode == "semantic_authority"
    assert (record_sem.diagnostics_hash != record.diagnostics_hash
            )   # 审计模式不同→诊断哈希可区分（可审计面稳定分账）

    # （iii）版本面仍以 config 为单源（v2 提示词身份不漂移）
    assert out_sem.judge_version_config is replay_vc
    assert out_sem.judge_version_config.prompt_version == "judge_v2"


# ============================================================ 10. 终审 P1-2 diagnostics_hash 钉测

def test_p1_2_diagnostics_hash_changes_and_payload_hash_stable(monkeypatch):
    """终审 P1-2 钉测：AuditRecord.diagnostics_hash=五诊断字段
    （evidence_status/evidence_warnings/machine_findings/
    full_text_fallback_used/judge_decision_mode）规范化哈希——五字段
    任一变化→hash 变；payload_hash 原样保留不动（历史兼容）；规范化=
    warnings/findings 集合语义（排序去序敏感）。"""
    _semantic(monkeypatch)
    judge = _judge(lambda ctx: _make_proof(ctx, verdict="duplicate",
                                           mv_passed=False,
                                           rules_triggered=("P_OFFSET",)))
    history = _history("甲公司9月24日公告营收100万元。", "甲公司")
    current = _current("甲公司公告：9月24日营收100万元。", "甲公司")
    out = decide_service.decide_for_task(
        history, [], current=current, judge_callable=judge,
        judge_in_chain=True, coverage_complete=True)
    pair = out.pair_results[0]
    base = audit_module.build_audit_record(pair)
    assert base.diagnostics_hash                    # 自动计算非空（64 hex）
    assert len(base.diagnostics_hash) == 64
    assert base.payload_hash
    # 持久化文档实含（to_doc 序列化面）
    assert "diagnostics_hash" in base.to_doc()
    # 公共五字段绝不携诊断哈希
    assert "diagnostics_hash" not in out.to_public_dict()

    # 五字段逐一变异：diagnostics_hash 必变、payload_hash 必不变
    mutations = [
        {"evidence_status": "pass"},
        {"evidence_warnings": ()},
        {"machine_findings": ()},
        {"full_text_fallback_used": True},
        {"judge_decision_mode": "legacy_proof_gate"},
    ]
    for mut in mutations:
        mutated = audit_module.build_audit_record(
            pair.__class__(**{**pair.__dict__, **mut}))
        assert mutated.diagnostics_hash != base.diagnostics_hash, mut
        assert mutated.payload_hash == base.payload_hash, mut

    # 规范化：warnings/findings 排序（集合语义，顺序无关）→ 同 hash
    two = pair.__class__(**{**pair.__dict__,
                            "evidence_warnings": ("W_SECOND", "W_FIRST"),
                            "machine_findings": ("M_SECOND", "M_FIRST")})
    two_swapped = pair.__class__(
        **{**pair.__dict__,
           "evidence_warnings": ("W_FIRST", "W_SECOND"),
           "machine_findings": ("M_FIRST", "M_SECOND")})
    rec_two = audit_module.build_audit_record(two)
    rec_two_swapped = audit_module.build_audit_record(two_swapped)
    assert rec_two.diagnostics_hash == rec_two_swapped.diagnostics_hash
    assert rec_two.diagnostics_hash != base.diagnostics_hash
    assert rec_two.payload_hash == base.payload_hash

    # 端到端：commit 写入计划的审计文档实含 diagnostics_hash（§4 同源）
    ctx = CommitContext(
        scope_id="default", business_date="2026-09-26", arrival_seq=3,
        current=current, candidates=(history,), visible_seq=10,
        prepared_seq=10, coverage_complete=True)
    plan = build_commit_write_plan(ctx, out, audit_complete=True)
    record = plan.audit_batch.records[0]
    assert record.diagnostics_hash == base.diagnostics_hash
    assert "diagnostics_hash" in record.to_doc()
