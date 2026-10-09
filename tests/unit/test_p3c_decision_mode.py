# -*- coding: utf-8 -*-
"""提交三：公共理由、观测指标、灰度开关（语义权威改造 3/3）验收测试。

方案：log/快讯去重_判官与证明层改造_可执行技术方案.md §5.3
（2026-10-10，分支 p3-semantic-authority）。

钉值面：
1. 灰度开关 DEDUP_JUDGE_DECISION_MODE 默认 legacy_proof_gate（缺席/空串
   回旧口径；**非法非空值→ValueError 明确报错不静默**，整改令二）——
   生效结论回到提交一前证明闸语义（证据诊断重升否决项：无引文→
   EVIDENCE_UNBOUND、passed=false→MV_REJECTED、结论对拍冲突→
   CONCLUSION_CONTRADICTS、无轴 ND→doubtful 降级、分歧/存疑→
   SUBJECT_UNRESOLVED）；
2. legacy 模式下同一份判官结果**离线**并行计算 semantic 结论记入
   comparison（不增加 LLM 调用——双序调用计数恒为 2）；
3. 观测指标 judge.semantic.* / judge.order_disagree / judge.evidence.warn /
   judge.evidence.fallback_full_text / judge.evidence.rule.P_* /
   judge.legacy_vs_new.changed 挂 DecideOutcome.judge_diagnostics +
   ProcessingBudget 轻量计数钩子，绝不进公共五字段；
4. 公共理由：判官源=整改令五固定措辞（禁用"已验证/已逐一核验"）；
   规则链源=最早重复/首个冲突对已清洗 detail（清洗=去 URL/折叠空白/
   ≤300 字），无可用 detail 才落固定兜底；边界理由保持主 issue
   detail 不变；
5. 显式 semantic_authority（env 或 judge_decision_mode 参数）=提交一
   口径直签；参数优先于 env。
"""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest

from news_flash_dedup.compare import aggregate as aggregate_module
from news_flash_dedup.compare.aggregate import CoverageStatus, FrozenRecallPlan
from news_flash_dedup.compare.pair_compare import PairResult, UNRESOLVED_CODES
from news_flash_dedup.decide import judge_pair
from news_flash_dedup.decide import service as decide_service
from news_flash_dedup.lib import run_manifest as rm
from news_flash_dedup.runtime_budget import (
    ProcessingBudget,
    RuntimeBudgetConfig,
)


# ---------------------------------------------------------------- 夹具（p1b/p3sem 同形）

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
# 提交一修复（2026-10-10 整改令三）：重复样本=同主体无硬冲突；判官源
# 不重复样本=同主体不同事件（机器五族轴不在场——有轴的对一律被
# compare/core_conflict 前置层拦截，判官零调用，见 test_p3fix_core_conflict）。
DUP_H = "甲公司9月24日公告营收100万元。"
DUP_C = "甲公司公告：9月24日营收100万元。"
ND2_H = "甲公司公告回购股份。"
ND2_C = "甲公司发布半年度财报。"


def _history(text=DUP_H, subject="甲公司"):
    h = _ctx(H_ID, "item-A", text, 1)
    h["facts"] = [_fact(H_ID, subject)]
    return h


def _current(text=DUP_C, subject="甲公司"):
    c = _ctx(C_ID, "item-C", text, 3)
    c["facts"] = [_fact(C_ID, subject)]
    return c


# ---------------------------------------------------------------- double 假证明（p3sem 同型）

_DOUBLE_MODEL = "judge-double-v1"
_DOUBLE_PROMPT_SHA = hashlib.sha256(b"double-prompt").hexdigest()
_DOUBLE_POLICY = "policy-v1"


def _make_proof(ctx, *, verdict, mv_passed=True, rules_triggered=(),
                with_falsification=True, unbound_quotes=False,
                falsification=None,
                reason="双序测试理由。"):
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
        # 提交一修复：证伪结构可由调用方按文本对实义覆盖（dimension/
        # evidence/relation 必须与文本真实差异一致，不挂伪证伪）。
        proof["falsification"] = falsification or {
            "dimension": "subject",
            "evidence_a": {"text": text_a[0:2], "offset_start": 0, "offset_end": 2},
            "evidence_b": {"text": text_b[0:2], "offset_start": 0, "offset_end": 2},
            "relation": "双侧主体不同，证伪同一事实。",
        }
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


def _adjudicate_direct(judge, h_text=DUP_H, c_text=DUP_C, **kw):
    pair = SimpleNamespace(
        pair_id="pair-p3c-direct", history_record_id=H_ID,
        current_record_id=C_ID, history_item_id="item-A",
        current_item_id="item-C")
    pctx = judge_pair.build_pair_context(pair, history_text=h_text,
                                         current_text=c_text)
    return judge_pair.adjudicate_pair(judge, pctx, **kw)


def _decide(judge, h_text=DUP_H, c_text=DUP_C, **kw):
    return decide_service.decide_for_task(
        _history(h_text), [], current=_current(c_text), judge_callable=judge,
        judge_in_chain=True, coverage_complete=True, **kw)


def _budget():
    return ProcessingBudget.derive(
        accepted_at_mono=0.0,
        config=RuntimeBudgetConfig(
            max_processing_s=15.0, prepare_soft_s=10.0, commit_target_s=1.0,
            channel_timeout_s=0.3, per_call_timeout_s=3.0,
            max_model_calls=20, shared_append_budget=1),
        clock_mono=lambda: 0.0)


# ============================================================ 1. 开关默认/读值

def test_mode_default_is_legacy_proof_gate(monkeypatch):
    """§5.3 + 整改令二：缺席/空串默认 legacy_proof_gate（fail-closed 回旧）；
    **非法非空值→ValueError 明确报错，不得静默回退**（env 与显式参数两路）。"""
    monkeypatch.delenv(judge_pair.JUDGE_DECISION_MODE_ENV, raising=False)
    assert judge_pair.judge_decision_mode() == "legacy_proof_gate"
    assert judge_pair.DEFAULT_JUDGE_DECISION_MODE == "legacy_proof_gate"
    monkeypatch.setenv(judge_pair.JUDGE_DECISION_MODE_ENV, "")
    assert judge_pair.judge_decision_mode() == "legacy_proof_gate"
    monkeypatch.setenv(judge_pair.JUDGE_DECISION_MODE_ENV, "banana")
    with pytest.raises(ValueError, match="DEDUP_JUDGE_DECISION_MODE"):
        judge_pair.judge_decision_mode()
    judge = _judge(lambda ctx: _make_proof(ctx, verdict="duplicate"))
    with pytest.raises(ValueError):
        _adjudicate_direct(judge)                    # 非法 env 启动即报
    monkeypatch.delenv(judge_pair.JUDGE_DECISION_MODE_ENV, raising=False)
    with pytest.raises(ValueError):
        _adjudicate_direct(judge, decision_mode="banana")   # 非法实参同罪
    monkeypatch.setenv(judge_pair.JUDGE_DECISION_MODE_ENV,
                       "semantic_authority")
    assert judge_pair.judge_decision_mode() == "semantic_authority"


def test_switch_registered_in_run_manifest():
    """§5.3：灰度开关登记入 KNOWN_SWITCHES（审计清单如实快照）。"""
    assert "DEDUP_JUDGE_DECISION_MODE" in rm.KNOWN_SWITCHES


# ============================================================ 2. legacy 生效口径 + 离线对照

def test_legacy_restores_veto_with_offline_comparison(monkeypatch):
    """默认 legacy：duplicate+mv passed=false → 旧否决（EVIDENCE_INVALID
    边界）；同一份结果离线算 semantic=equivalent 记 comparison；双序
    调用恒 2 次（不增 LLM 调用）。"""
    monkeypatch.delenv(judge_pair.JUDGE_DECISION_MODE_ENV, raising=False)
    judge = _judge(lambda ctx: _make_proof(ctx, verdict="duplicate",
                                           mv_passed=False,
                                           rules_triggered=["P_OFFSET"]))
    judged = _adjudicate_direct(judge)
    assert judge.calls == ["ab", "ba"]                # 不增 LLM 调用
    assert judged.decision_mode == "legacy_proof_gate"
    assert judged.outcome == "unresolved"
    assert judged.code == "EVIDENCE_INVALID"
    assert judged.failure_reason == judge_pair.MV_REJECTED
    cmp = judged.comparison
    assert cmp is not None
    assert cmp["legacy_outcome"] == "unresolved"
    assert cmp["semantic_outcome"] == "equivalent"
    assert cmp["semantic_code"] == "JUDGE_EQUIVALENT"
    assert cmp["changed"] is True


def test_legacy_bare_not_duplicate_downgrades_to_doubtful(monkeypatch):
    """默认 legacy：无 falsification 的双序 not_duplicate → 旧 §二 降级
    doubtful（SUBJECT_UNRESOLVED 边界）；semantic 侧=conflict 离线对照。
    文本对=同主体不同事件（机器五族轴不在场），判官裸签 ND 业务成立。"""
    monkeypatch.delenv(judge_pair.JUDGE_DECISION_MODE_ENV, raising=False)
    judge = _judge(lambda ctx: _make_proof(ctx, verdict="not_duplicate",
                                           with_falsification=False))
    judged = _adjudicate_direct(judge, ND2_H, ND2_C)
    assert judged.outcome == "unresolved"
    assert judged.code == "SUBJECT_UNRESOLVED"
    assert judged.failure_reason == judge_pair.JUDGE_DOUBTFUL
    assert judged.comparison["semantic_outcome"] == "conflict"
    assert judged.comparison["semantic_code"] == "JUDGE_NON_DUPLICATE"
    assert judged.comparison["changed"] is True


def test_legacy_signed_paths_use_legacy_codes(monkeypatch):
    """默认 legacy：干净双序 duplicate → 重复/FACT_EQUIVALENT（旧码旧
    文案）；干净双序 ND+falsification → 不重复/VERIFIED_CONFLICT。
    两侧 semantic 对照码不同 → changed=True 全记录。"""
    monkeypatch.delenv(judge_pair.JUDGE_DECISION_MODE_ENV, raising=False)
    judge_dup = _judge(lambda ctx: _make_proof(ctx, verdict="duplicate"))
    judged_dup = _adjudicate_direct(judge_dup)
    assert judged_dup.outcome == "equivalent"
    assert judged_dup.code == "FACT_EQUIVALENT"
    assert "机器验 gate 通过" in judged_dup.detail
    assert judged_dup.comparison["semantic_code"] == "JUDGE_EQUIVALENT"
    assert judged_dup.comparison["changed"] is True
    # ND 带轴：文本对=同主体、事实时间明确不同（机检 time 轴真实存在；
    # 证伪结构与文本差异一致，不挂伪证伪）
    t_h, t_c = "甲公司9月24日公告营收100万元。", "甲公司9月25日公告营收100万元。"

    def _fals(ctx):
        ta, tb = ctx["text_a"], ctx["text_b"]
        da, db = ("9月24日", "9月25日") if "9月24日" in ta else ("9月25日", "9月24日")
        return {"dimension": "time",
                "evidence_a": {"text": da, "offset_start": ta.find(da),
                               "offset_end": ta.find(da) + len(da)},
                "evidence_b": {"text": db, "offset_start": tb.find(db),
                               "offset_end": tb.find(db) + len(db)},
                "relation": "事实时间不同，证伪同一事实。"}

    judge_nd = _judge(lambda ctx: _make_proof(
        ctx, verdict="not_duplicate", falsification=_fals(ctx)))
    judged_nd = _adjudicate_direct(judge_nd, t_h, t_c)
    assert judged_nd.outcome == "conflict"
    assert judged_nd.code == "VERIFIED_CONFLICT"
    assert judged_nd.comparison["semantic_code"] == "JUDGE_NON_DUPLICATE"
    assert judged_nd.comparison["changed"] is True


def test_legacy_unbound_quotes_veto_evidence_unbound(monkeypatch):
    """默认 legacy：引文全失效 → EVIDENCE_UNBOUND 否决（旧引文闸）。"""
    monkeypatch.delenv(judge_pair.JUDGE_DECISION_MODE_ENV, raising=False)
    judge = _judge(lambda ctx: _make_proof(ctx, verdict="duplicate",
                                           unbound_quotes=True))
    judged = _adjudicate_direct(judge)
    assert judged.outcome == "unresolved"
    assert judged.code == "EVIDENCE_INVALID"
    assert judged.failure_reason == judge_pair.EVIDENCE_UNBOUND
    assert judged.comparison["semantic_outcome"] == "equivalent"


# ============================================================ 3. semantic 直签 + 参数优先

def test_semantic_mode_via_env_signs_with_warnings(monkeypatch):
    """显式 semantic_authority（env）→ 提交一口径直签（诊断不改判）。"""
    monkeypatch.setenv(judge_pair.JUDGE_DECISION_MODE_ENV,
                       "semantic_authority")
    judge = _judge(lambda ctx: _make_proof(ctx, verdict="duplicate",
                                           mv_passed=False))
    judged = _adjudicate_direct(judge)
    assert judged.decision_mode == "semantic_authority"
    assert judged.outcome == "equivalent"
    assert judged.code == "JUDGE_EQUIVALENT"
    assert judged.comparison is None


def test_param_overrides_env_both_directions(monkeypatch):
    """judge_decision_mode 参数优先于 env（双向）。"""
    monkeypatch.setenv(judge_pair.JUDGE_DECISION_MODE_ENV,
                       "legacy_proof_gate")
    judge = _judge(lambda ctx: _make_proof(ctx, verdict="duplicate",
                                           mv_passed=False))
    out = _decide(judge, judge_decision_mode="semantic_authority")
    assert out.decision == "重复"
    assert out.internal_code == "JUDGE_EQUIVALENT"
    monkeypatch.setenv(judge_pair.JUDGE_DECISION_MODE_ENV,
                       "semantic_authority")
    judge2 = _judge(lambda ctx: _make_proof(ctx, verdict="duplicate",
                                            mv_passed=False))
    out2 = _decide(judge2, judge_decision_mode="legacy_proof_gate")
    assert out2.decision == "边界case/疑难case"
    assert out2.internal_code == "EVIDENCE_INVALID"


# ============================================================ 4. 观测指标

def test_metrics_semantic_and_legacy_changed(monkeypatch):
    """§5.3 指标：legacy 默认下 semantic 结论照计 + 新旧差异计数 +
    P_* 规则计数 + 证据告警计数；计数挂 judge_diagnostics，不进公共
    五字段。"""
    monkeypatch.delenv(judge_pair.JUDGE_DECISION_MODE_ENV, raising=False)
    judge = _judge(lambda ctx: _make_proof(ctx, verdict="duplicate",
                                           mv_passed=False,
                                           rules_triggered=["P_OFFSET"]))
    out = _decide(judge)
    assert out.decision == "边界case/疑难case"      # legacy 生效面
    d = out.judge_diagnostics
    assert d["judge.semantic.duplicate"] == 1       # semantic 离线照计
    assert d["judge.legacy_vs_new.changed"] == 1
    assert d["judge.evidence.warn"] == 1
    assert d["judge.evidence.rule.P_OFFSET"] == 1
    public = out.to_public_dict()
    assert set(public) == {"item_id", "text", "decision",
                           "duplicate_ids", "reason"}   # 五字段封闭
    assert "judge_diagnostics" not in json.dumps(public, ensure_ascii=False)


def test_metrics_order_disagree_and_fallback(monkeypatch):
    """judge.order_disagree（semantic 侧双序分歧）与
    judge.evidence.fallback_full_text（完整原文回退）计数。"""
    monkeypatch.setenv(judge_pair.JUDGE_DECISION_MODE_ENV,
                       "semantic_authority")

    def _mixed(ctx):
        if ctx["order"] == "ab":
            return _make_proof(ctx, verdict="duplicate")
        return _make_proof(ctx, verdict="not_duplicate",
                           with_falsification=False)   # 同主体文本不挂伪证伪

    out = _decide(_judge(_mixed))
    assert out.decision == "边界case/疑难case"
    assert out.internal_code == "JUDGE_UNCERTAIN"
    d = out.judge_diagnostics
    assert d["judge.order_disagree"] == 1
    assert d["judge.semantic.unresolved"] == 1
    assert "judge.legacy_vs_new.changed" not in d   # semantic 模式无对照

    judge_fb = _judge(lambda ctx: _make_proof(ctx, verdict="duplicate",
                                              unbound_quotes=True))
    out_fb = _decide(judge_fb)
    assert out_fb.decision == "重复"
    assert out_fb.judge_diagnostics[
        "judge.evidence.fallback_full_text"] == 1


def test_metrics_legacy_disagree_counts_semantic_side(monkeypatch):
    """legacy 模式下 judge.order_disagree 仍按 semantic 侧（离线对照）计。"""
    monkeypatch.delenv(judge_pair.JUDGE_DECISION_MODE_ENV, raising=False)

    def _mixed(ctx):
        if ctx["order"] == "ab":
            return _make_proof(ctx, verdict="duplicate")
        return _make_proof(ctx, verdict="not_duplicate",
                           with_falsification=False)

    out = _decide(_judge(_mixed))
    assert out.decision == "边界case/疑难case"
    assert out.internal_code == "SUBJECT_UNRESOLVED"     # legacy 码
    d = out.judge_diagnostics
    assert d["judge.order_disagree"] == 1                # semantic 侧分歧
    assert d["judge.legacy_vs_new.changed"] == 1         # 码分流差异


def test_budget_counter_hook(monkeypatch):
    """§5.3：ProcessingBudget 轻量计数钩子同步计数（双通道同源）。"""
    monkeypatch.setenv(judge_pair.JUDGE_DECISION_MODE_ENV,
                       "semantic_authority")
    budget = _budget()
    judge = _judge(lambda ctx: _make_proof(ctx, verdict="duplicate"))
    out = decide_service.decide_for_task(
        _history(), [], current=_current(), judge_callable=judge,
        judge_in_chain=True, coverage_complete=True, budget=budget)
    assert out.decision == "重复"
    snap = budget.counter_snapshot()
    assert snap["judge.semantic.duplicate"] == 1
    assert snap["judge.semantic.duplicate"] == out.judge_diagnostics[
        "judge.semantic.duplicate"]


# ============================================================ 5. 公共理由（§5.3 + 整改令五）

# 整改令五：判官源公共理由=固定措辞；规则链兜底文案去"已验证/已逐一核验"。
_FIXED_DUP_REASON = "两条快讯经规则链路比对核心要素一致，因此判定为重复。"
_FIXED_CONFLICT_REASON = "两条快讯经规则链路比对存在核心要素冲突，因此判定为不重复。"


def test_public_reason_from_judge_duplicate_detail(monkeypatch):
    """重复：公共 reason=判官双序重复固定措辞（整改令五：能力内声明，
    模型理由只留存 proofs 审计件，不进公共面）。"""
    monkeypatch.setenv(judge_pair.JUDGE_DECISION_MODE_ENV,
                       "semantic_authority")
    judge = _judge(lambda ctx: _make_proof(
        ctx, verdict="duplicate", reason="营收与日期完全一致。"))
    out = _decide(judge)
    assert out.decision == "重复"
    assert out.reason == judge_pair.JUDGE_DUPLICATE_REASON
    assert out.reason == (
        "判官双序一致认为两条快讯描述同一核心事实，因此判定为重复。")
    assert "已验证" not in out.reason and "已逐一核验" not in out.reason


def test_public_reason_cap_300_and_url_stripped(monkeypatch):
    """理由清洗：URL 剥除 + 上限 300 字符（规则链 detail 携带的链接/
    超长文本绝不进公共面；判官源理由已是固定措辞，本钉守规则链通道）。"""
    long_detail = ("已验证至少一条充分冲突：numerics.value。详见 "
                   "http://evil.example.com/report " + "长" * 400)
    out = _aggregate_with(_agg_pair(outcome="conflict",
                                    code="VERIFIED_CONFLICT",
                                    detail=long_detail))
    assert out.decision == "不重复"
    assert len(out.reason) <= 300
    assert "http" not in out.reason
    assert "evil.example.com" not in out.reason


def test_public_reason_from_judge_non_duplicate_detail(monkeypatch):
    """不重复（JUDGE_NON_DUPLICATE，判官双序无机器证伪轴）：公共 reason=
    整改令五固定措辞（不冒称确定性规则冲突，证据质量信息指引审计）。"""
    monkeypatch.setenv(judge_pair.JUDGE_DECISION_MODE_ENV,
                       "semantic_authority")
    judge = _judge(lambda ctx: _make_proof(
        ctx, verdict="not_duplicate", with_falsification=False,
        reason="事件完全不同。"))
    out = _decide(judge, ND2_H, ND2_C)
    assert out.decision == "不重复"
    assert out.internal_code == "JUDGE_NON_DUPLICATE"
    assert out.reason == judge_pair.JUDGE_NON_DUPLICATE_REASON
    assert out.reason == (
        "判官双序一致判定为不重复；未形成确定性规则冲突，"
        "相关证据质量信息已记录审计。")
    assert "已验证" not in out.reason and "已逐一核验" not in out.reason


def _agg_pair(*, outcome, code, detail):
    return PairResult(
        pair_id="pair-agg-1", history_record_id=H_ID, current_record_id=C_ID,
        history_item_id="item-A", current_item_id="item-C",
        history_arrival_seq=1, current_arrival_seq=3,
        history_raw_hash=_history()["raw_hash"],
        current_raw_hash=_current()["raw_hash"],
        pipeline_version="dedup_v1",
        outcome=outcome, code=code, detail=detail,
        aligned_facts=(), verified_conflicts=(), unresolved_fields=(),
        used_evidence=())


def _aggregate_with(pair):
    current = _current()
    plan = FrozenRecallPlan(version="rrf_v1_k60_30_10",
                            required={H_ID: _history()})
    return aggregate_module.aggregate(
        current, plan, [pair],
        coverage=CoverageStatus(visible_seq=10, prepared_seq=10,
                                complete=True))


def test_public_reason_from_rule_conflict_detail():
    """不重复（规则硬冲突）：公共 reason=首个冲突对 detail 清洗版。"""
    detail = "已验证至少一条充分冲突：numerics.value（numeric-conflict-test）。"
    out = _aggregate_with(_agg_pair(outcome="conflict",
                                    code="VERIFIED_CONFLICT", detail=detail))
    assert out.decision == "不重复"
    assert out.internal_code == "VERIFIED_CONFLICT"
    assert out.reason == detail
    assert out.reason != _FIXED_CONFLICT_REASON


def test_public_reason_from_rule_equivalent_detail():
    """重复（规则链等价）：公共 reason=最早重复对 detail 清洗版。"""
    detail = "主体和核心事件一致，差异属于已允许的表达或信息差异。"
    out = _aggregate_with(_agg_pair(outcome="equivalent",
                                    code="FACT_EQUIVALENT", detail=detail))
    assert out.decision == "重复"
    assert out.reason == detail


def test_public_reason_fallback_when_detail_empty():
    """无可用 detail（清洗后为空）→ 固定兜底文案不变。"""
    out = _aggregate_with(_agg_pair(outcome="conflict",
                                    code="VERIFIED_CONFLICT", detail=""))
    assert out.decision == "不重复"
    assert out.reason == _FIXED_CONFLICT_REASON
    out_dup = _aggregate_with(_agg_pair(outcome="equivalent",
                                        code="FACT_EQUIVALENT", detail=""))
    assert out_dup.decision == "重复"
    assert out_dup.reason == _FIXED_DUP_REASON


def test_boundary_reason_unchanged_primary_issue(monkeypatch):
    """边界：公共理由保持现有主 issue 理由（判官未决 detail 原样透传）。"""
    monkeypatch.setenv(judge_pair.JUDGE_DECISION_MODE_ENV,
                       "semantic_authority")

    def _mixed(ctx):
        if ctx["order"] == "ab":
            return _make_proof(ctx, verdict="duplicate")
        return _make_proof(ctx, verdict="not_duplicate")

    out = _decide(_judge(_mixed))
    assert out.decision == "边界case/疑难case"
    assert out.reason == f"判官未决进人工（{judge_pair.ORDER_DISAGREE}）。"
