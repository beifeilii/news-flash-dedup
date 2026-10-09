"""P1-b 判官进主链 + 提交前最终聚合（2026-10-09，施工窗 P1-b）。

依据（冻结件）：
- 接口合同 log/temp/p1-interface-contract-v1.md §一-§五；
- 工程正文 log/判定链改造最终方案-Codex-20261008.md §3.1/§3.2。

提交一（2026-10-10，分支 p3-semantic-authority，方案
log/快讯去重_判官与证明层改造_可执行技术方案.md §4.2/§5.1）口径更新：
- 判官双序签 duplicate → 重复（**JUDGE_EQUIVALENT**——语义权威码，
  不冒充机器已验证的 FACT_EQUIVALENT）；
- 双序签 not_duplicate → 不重复（**JUDGE_NON_DUPLICATE**——不再强制
  机器证伪轴；无轴记 P_NO_AXIS 审计告警，不降级）；
- 证明层一票否决解除：机验 passed=false / 引文回指失败 / 结论对拍
  "不一致" 全部降为证据诊断告警（不再未决）；双序分歧/存疑 → 未决
  （**JUDGE_UNCERTAIN**）；
- 合同级硬失败保留：换文复用/身份错绑/结构非法/超时/预算/异常/
  verdict=failure·invalid → 仍 fail-closed 未决进人工。

覆盖：
- 开关 DEDUP_JUDGE_IN_CHAIN 默认关=老行为逐字节（判官零调用、结果同构）；
- 红测群（合同 §五红线）：required 非空而 pair_results 空→不得签不重复；
  候选漏判→不得签不重复；预算耗尽/超时→未决进人工；
- 提交前唯一聚合：终聚合 DecideOutcome 才进 commit 写入计划；初聚合不写
  主记录（commit 侧只见终态）；commit_one 接线点只读核对（env 开+未注入
  判官 → fail-closed 边界，不冒签）。

证明件 = 合同合规的假证明（_make_proof 测试 double，照合同 §一 schema
逐字段构造含 cache_key 公式重算 + 提交一 audit 语义 machine_verify +
reason 字段）；P1-a 真件合流时零改动替换注入件。
"""

from __future__ import annotations

import hashlib
import time
from types import SimpleNamespace

import pytest

from news_flash_dedup.compare import aggregate as aggregate_module
from news_flash_dedup.compare.aggregate import CoverageStatus, FrozenRecallPlan
from news_flash_dedup.compare.pair_compare import PairResult
from news_flash_dedup.commit.coordinator import (
    CommitContext,
    build_commit_write_plan,
    commit_one,
)
from news_flash_dedup.commit.fake_store import FakeCommitStore
from news_flash_dedup.decide import judge_pair
from news_flash_dedup.decide import service as decide_service


# ---------------------------------------------------------------- 夹具

def _ctx(record_id, item_id, text, arrival_seq, **kw):
    return {
        "record_id": record_id, "item_id": item_id, "text": text,
        "raw_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "scope_id": "default", "business_date": "2026-09-26",
        "arrival_seq": arrival_seq, "pipeline_version": "dedup_v1",
        **kw,
    }


def _fact(record_id, subject):
    """最小合法事实夹具（test_p17_implementation._evidence_for 同形，
    subject 参数化）；文本首字必须等于 subject[0]（证据 offset [0,1)）。"""
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


H_ID, C_ID, D_ID = "a" * 64, "c" * 64, "b" * 64
H_TEXT, C1_TEXT, CUR_TEXT = "甲公司完成回购。", "丙公司完成回购。", "乙公司完成回购。"


def _history():
    h = _ctx(H_ID, "item-A", H_TEXT, 1)
    h["facts"] = [_fact(H_ID, "甲公司")]
    return h


def _candidate1():
    c = _ctx(D_ID, "item-B", C1_TEXT, 2)
    c["facts"] = [_fact(D_ID, "丙公司")]
    return c


def _current():
    c = _ctx(C_ID, "item-C", CUR_TEXT, 3)
    c["facts"] = [_fact(C_ID, "乙公司")]
    return c


# ---------------------------------------------------------------- 合同合规假证明 double

_DOUBLE_MODEL = "judge-double-v1"
_DOUBLE_PROMPT_SHA = hashlib.sha256(b"double-prompt").hexdigest()
_DOUBLE_POLICY = "policy-v1"


def _make_proof(ctx, *, verdict, mv_passed=True, rules_triggered=(),
                numeric_conclusion="一致",
                time_conclusion="一致", with_falsification=True,
                unbound_quotes=False, stolen_binding=False,
                reason="双序测试理由：双侧所述为同一事实。"):
    """照合同 §一 schema 构造 VerifiedJudgeProof（dict）。

    ctx = judge_pair._order_context 产物（含 order/text_a/text_b/双侧 id 与 sha）。
    默认全合规；各关键字构造一类不合规形态供红测。
    提交一：machine_verify 为 audit 语义（mode="audit"，passed 兼容保留、
    消费侧不得改判）；proof 携 reason（§5.1 文件 B-1）。
    """
    text_a, text_b = ctx["text_a"], ctx["text_b"]
    sha_a = hashlib.sha256(text_a.encode("utf-8")).hexdigest()
    sha_b = hashlib.sha256(text_b.encode("utf-8")).hexdigest()
    if stolen_binding:  # 换文复用：绑定别的正文
        sha_a = hashlib.sha256("别人的正文甲".encode("utf-8")).hexdigest()
        sha_b = hashlib.sha256("别人的正文乙".encode("utf-8")).hexdigest()
    if unbound_quotes:  # offset 回指失败：引文不是原文切片
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
        "numeric_check": {"conclusion": numeric_conclusion, "details": []},
        "time_check": {"conclusion": time_conclusion,
                       "anchors_a": [], "anchors_b": []},
        "machine_verify": {"mode": "audit",
                           "rules_triggered": list(rules_triggered),
                           "passed": mv_passed},
        "model_version": _DOUBLE_MODEL,
        "prompt_sha256": _DOUBLE_PROMPT_SHA,
        "policy_version": _DOUBLE_POLICY,
        "judged_at": "2026-10-09T00:00:00+00:00",
    }
    if verdict == "not_duplicate" and with_falsification:
        proof["falsification"] = {
            "dimension": "subject",
            "evidence_a": {"text": text_a[0:2], "offset_start": 0, "offset_end": 2},
            "evidence_b": {"text": text_b[0:2], "offset_start": 0, "offset_end": 2},
            "relation": "双侧主体不同（甲/乙/丙互异），证伪同一事实。",
        }
    proof["cache_key"] = judge_pair.compute_cache_key(
        _DOUBLE_MODEL, _DOUBLE_PROMPT_SHA, _DOUBLE_POLICY,
        ctx["order"], sha_a, sha_b)
    return proof


def _judge(handler):
    """包装判官 handler 为 judge_callable 并记录调用序列。"""
    calls = []

    def _callable(ctx):
        calls.append({"pair_id": ctx["pair_id"], "order": ctx["order"],
                      "history_record_id": ctx["history_record_id"]})
        return handler(ctx)

    _callable.calls = calls
    return _callable


def _sign_duplicate(ctx):
    return _make_proof(ctx, verdict="duplicate")


def _sign_conflict(ctx):
    return _make_proof(ctx, verdict="not_duplicate")


def _sign_doubtful(ctx):
    return _make_proof(ctx, verdict="doubtful")


def _boundary_kwargs(**over):
    kw = dict(coverage_complete=True)
    kw.update(over)
    return kw


# ---------------------------------------------------------------- A. 开关纪律

def test_switch_off_default_env_unset_judge_never_called(monkeypatch):
    """开关默认关（env 缺席）：注入判官也零调用，结果=老行为。"""
    monkeypatch.delenv(judge_pair.JUDGE_IN_CHAIN_ENV, raising=False)
    judge = _judge(_sign_duplicate)
    out = decide_service.decide_for_task(
        _history(), [], current=_current(), judge_callable=judge,
        **_boundary_kwargs())
    assert judge.calls == []
    assert out.decision == "边界case/疑难case"
    assert out.duplicate_ids == ()


def test_switch_off_param_explicit_and_byte_identical(monkeypatch):
    """显式 judge_in_chain=False 与 env 缺席默认形态逐字段同构（老行为逐字节）。"""
    monkeypatch.delenv(judge_pair.JUDGE_IN_CHAIN_ENV, raising=False)
    out_default = decide_service.decide_for_task(
        _history(), [], current=_current(), **_boundary_kwargs())
    out_off = decide_service.decide_for_task(
        _history(), [], current=_current(), judge_in_chain=False,
        judge_callable=_judge(_sign_duplicate), **_boundary_kwargs())
    assert out_default.to_public_dict() == out_off.to_public_dict()
    assert out_default.internal_code == out_off.internal_code
    assert dict(out_default.pair_codes) == dict(out_off.pair_codes)
    assert out_default.unresolved_fields == out_off.unresolved_fields


def test_switch_on_via_env(monkeypatch):
    """env DEDUP_JUDGE_IN_CHAIN=1 → 判官进主链生效。"""
    monkeypatch.setenv(judge_pair.JUDGE_IN_CHAIN_ENV, "1")
    judge = _judge(_sign_duplicate)
    out = decide_service.decide_for_task(
        _history(), [], current=_current(), judge_callable=judge,
        **_boundary_kwargs())
    assert judge.calls != []
    assert out.decision == "重复"


def test_switch_param_overrides_env(monkeypatch):
    """显式 judge_in_chain=False 覆盖 env=1（参数优先）。"""
    monkeypatch.setenv(judge_pair.JUDGE_IN_CHAIN_ENV, "1")
    judge = _judge(_sign_duplicate)
    out = decide_service.decide_for_task(
        _history(), [], current=_current(), judge_callable=judge,
        judge_in_chain=False, **_boundary_kwargs())
    assert judge.calls == []
    assert out.decision == "边界case/疑难case"


@pytest.mark.parametrize("value,expected", [
    ("1", True), ("true", True), ("TRUE", True), ("yes", True), ("on", True),
    ("0", False), ("false", False), ("off", False), ("", False), ("2", False),
])
def test_switch_value_parsing(value, expected):
    assert judge_pair.judge_in_chain_enabled(
        {judge_pair.JUDGE_IN_CHAIN_ENV: value}) is expected


# ---------------------------------------------------------------- B. 签发路径（合同 §四/§五）

def test_judge_signs_duplicate_final_decision():
    """双序 duplicate → equivalent → 件级重复（提交一：JUDGE_EQUIVALENT
    语义权威码）；duplicate_ids 只收已证件；对级 PairResult 携真证据。"""
    judge = _judge(_sign_duplicate)
    out = decide_service.decide_for_task(
        _history(), [], current=_current(), judge_callable=judge,
        judge_in_chain=True, **_boundary_kwargs())
    assert out.decision == "重复"
    assert out.duplicate_ids == ("item-A",)
    assert out.internal_code == "JUDGE_EQUIVALENT"
    assert out.pair_codes[H_ID] == "JUDGE_EQUIVALENT"
    final_pair = out.pair_results[0]
    assert final_pair.outcome == "equivalent"
    assert final_pair.code == "JUDGE_EQUIVALENT"
    assert len(final_pair.used_evidence) > 0
    for code in ("EXACT_TEXT_MATCH", "LOSSLESS_TEXT_MATCH", "FACT_EQUIVALENT",
                 "JUDGE_EQUIVALENT", "JUDGE_NON_DUPLICATE", "JUDGE_UNCERTAIN"):
        assert code not in out.reason


def test_judge_signs_conflict_final_decision():
    """双序 not_duplicate+合格 falsification → conflict → 件级不重复
    （提交一：JUDGE_NON_DUPLICATE；falsification 仍构造审计冲突件）。"""
    judge = _judge(_sign_conflict)
    out = decide_service.decide_for_task(
        _history(), [], current=_current(), judge_callable=judge,
        judge_in_chain=True, **_boundary_kwargs())
    assert out.decision == "不重复"
    assert out.duplicate_ids == ()
    assert out.internal_code == "JUDGE_NON_DUPLICATE"
    final_pair = out.pair_results[0]
    assert final_pair.outcome == "conflict"
    assert final_pair.code == "JUDGE_NON_DUPLICATE"
    assert len(final_pair.verified_conflicts) == 1
    conflict = final_pair.verified_conflicts[0]
    assert conflict.history_evidence.record_id == H_ID
    assert conflict.current_evidence.record_id == C_ID
    assert conflict.history_evidence.quote == H_TEXT[0:2]


def test_judge_called_in_candidate_order_two_orders_each():
    """按候选序对未决对逐对调用；每对双序（ab 后 ba）各一次。"""
    judge = _judge(_sign_doubtful)
    out = decide_service.decide_for_task(
        _history(), [_candidate1()], current=_current(),
        judge_callable=judge, judge_in_chain=True, **_boundary_kwargs())
    seq = [(c["history_record_id"], c["order"]) for c in judge.calls]
    assert seq == [(H_ID, "ab"), (H_ID, "ba"), (D_ID, "ab"), (D_ID, "ba")]
    assert out.decision == "边界case/疑难case"   # 双序存疑 → 未决进人工
    assert out.internal_code == "JUDGE_UNCERTAIN"  # 提交一：判官已跑而存疑
    for pair in out.pair_results:
        assert pair.outcome == "unresolved"
        assert pair.code == "JUDGE_UNCERTAIN"
        assert judge_pair.JUDGE_DOUBTFUL in pair.detail


def test_initial_non_boundary_skips_judge():
    """初聚合非边界（同文 EXACT 快路已签重复）时不调判官（判官只补未决对）。
    夹具形态对齐 test_p17 test_decide_same_nondefault_scope_still_duplicates：
    _complete_fact + 时间槽 verified_missing → EXACT 证书路径。"""
    from test_e_h04_facts_projection import _complete_fact
    history = _history()
    current = _ctx(C_ID, "item-C", H_TEXT, 3)
    history["facts"] = [_complete_fact(H_ID, H_TEXT)]
    current["facts"] = [_complete_fact(C_ID, H_TEXT)]
    for facts in (history["facts"], current["facts"]):
        facts[0]["time"]["expression"] = {
            "status": "missing", "raw_value": None, "evidence": [],
            "verified_missing": True}
    judge = _judge(_sign_conflict)
    out = decide_service.decide_for_task(
        history, [], current=current, judge_callable=judge,
        judge_in_chain=True, **_boundary_kwargs())
    assert out.decision == "重复"
    assert judge.calls == []


# ---------------------------------------------------------------- C. 红测群：失败/未决映射（合同 §二/§三 + 提交一 §4.2）

def _adjudicate(handler, *, h_text=H_TEXT, c_text=CUR_TEXT):
    """直跑判官适配层（诊断面断言用）：返 JudgePairOutcome。"""
    pair = SimpleNamespace(
        pair_id="pair-direct-1", history_record_id=H_ID,
        current_record_id=C_ID, history_item_id="item-A",
        current_item_id="item-C")
    pctx = judge_pair.build_pair_context(pair, history_text=h_text,
                                         current_text=c_text)
    return judge_pair.adjudicate_pair(_judge(handler), pctx)


def test_bare_not_duplicate_signs_with_p_no_axis_warning():
    """提交一 §4.2：双序 not_duplicate + 无 falsification → **不重复**
    （JUDGE_NON_DUPLICATE），P_NO_AXIS 只记证据充分性告警、不再降级。"""
    judge = _judge(lambda ctx: _make_proof(ctx, verdict="not_duplicate",
                                           with_falsification=False))
    out = decide_service.decide_for_task(
        _history(), [], current=_current(), judge_callable=judge,
        judge_in_chain=True, **_boundary_kwargs())
    assert out.decision == "不重复"
    assert out.duplicate_ids == ()
    assert out.pair_results[0].outcome == "conflict"
    assert out.pair_results[0].code == "JUDGE_NON_DUPLICATE"
    judged = _adjudicate(lambda ctx: _make_proof(
        ctx, verdict="not_duplicate", with_falsification=False))
    assert judged.outcome == "conflict"
    assert "P_NO_AXIS" in judged.evidence_warnings
    assert judged.verified_conflicts == ()        # 无轴不构造审计冲突件


def test_red_order_disagree_unresolved():
    """合同 §三 ORDER_DISAGREE（双序分歧）→ 未决进人工
    （提交一：JUDGE_UNCERTAIN 码）。"""
    def _handler(ctx):
        return _make_proof(ctx, verdict=(
            "duplicate" if ctx["order"] == "ab" else "not_duplicate"))
    out = decide_service.decide_for_task(
        _history(), [], current=_current(), judge_callable=_judge(_handler),
        judge_in_chain=True, **_boundary_kwargs())
    assert out.decision == "边界case/疑难case"
    assert out.internal_code == "JUDGE_UNCERTAIN"
    assert out.pair_results[0].code == "JUDGE_UNCERTAIN"
    assert judge_pair.ORDER_DISAGREE in out.pair_results[0].detail


def test_machine_verify_rejected_now_audit_only_still_signs():
    """提交一（解除一票否决）：双序 duplicate + machine_verify.passed=false
    （携 P_* 触发）→ 仍签重复（JUDGE_EQUIVALENT），MACHINE_VERIFY_REJECTED
    与 P_* 只进证据诊断。"""
    judge = _judge(lambda ctx: _make_proof(ctx, verdict="duplicate",
                                           mv_passed=False,
                                           rules_triggered=("P_OFFSET",)))
    out = decide_service.decide_for_task(
        _history(), [], current=_current(), judge_callable=judge,
        judge_in_chain=True, **_boundary_kwargs())
    assert out.decision == "重复"
    assert out.duplicate_ids == ("item-A",)
    assert out.pair_results[0].code == "JUDGE_EQUIVALENT"
    judged = _adjudicate(lambda ctx: _make_proof(
        ctx, verdict="duplicate", mv_passed=False,
        rules_triggered=("P_OFFSET",)))
    assert judged.outcome == "equivalent"
    assert judge_pair.MACHINE_VERIFY_REJECTED in judged.evidence_warnings
    assert "P_OFFSET" in judged.machine_findings


def test_red_judge_timeout_unresolved():
    """合同 §三 TIMEOUT（判官超时）→ 未决进人工（fail-closed）。"""
    def _slow(ctx):
        time.sleep(1.5)
        return _make_proof(ctx, verdict="duplicate")
    out = decide_service.decide_for_task(
        _history(), [], current=_current(), judge_callable=_judge(_slow),
        judge_in_chain=True, judge_timeout_s=0.2, **_boundary_kwargs())
    assert out.decision == "边界case/疑难case"
    assert out.internal_code == "DEPENDENCY_TIMEOUT"
    assert judge_pair.TIMEOUT in out.pair_results[0].detail


def test_red_judge_budget_exceeded_unresolved():
    """合同 §三 BUDGET_EXCEEDED（预算耗尽）→ 未决进人工。"""
    def _budget(ctx):
        raise judge_pair.JudgeBudgetExceeded("预算耗尽")
    out = decide_service.decide_for_task(
        _history(), [], current=_current(), judge_callable=_judge(_budget),
        judge_in_chain=True, **_boundary_kwargs())
    assert out.decision == "边界case/疑难case"
    assert out.internal_code == "CANDIDATE_BUDGET_EXHAUSTED"
    assert judge_pair.BUDGET_EXCEEDED in out.pair_results[0].detail


def test_red_judge_exception_unresolved():
    """判官任意异常 → fail-closed 未决（绝不冒签）。"""
    def _boom(ctx):
        raise RuntimeError("判官内部崩溃")
    out = decide_service.decide_for_task(
        _history(), [], current=_current(), judge_callable=_judge(_boom),
        judge_in_chain=True, **_boundary_kwargs())
    assert out.decision == "边界case/疑难case"
    assert judge_pair.JUDGE_EXCEPTION in out.pair_results[0].detail


def test_red_judge_not_injected_fail_closed():
    """默认实现：开关开但未注入 judge_callable → fail-closed 未决。"""
    out = decide_service.decide_for_task(
        _history(), [], current=_current(), judge_callable=None,
        judge_in_chain=True, **_boundary_kwargs())
    assert out.decision == "边界case/疑难case"
    assert out.internal_code == "SUBJECT_UNRESOLVED"
    assert judge_pair.JUDGE_NOT_INJECTED in out.pair_results[0].detail


def test_evidence_unbound_now_full_text_fallback_still_signs():
    """提交一（解除一票否决 + §5.1 修改点 3）：双序 duplicate 但引文
    offset 全部回指失败 → 仍签重复；EVIDENCE_UNBOUND 告警 + 完整原文
    回退证据（quote=该侧完整正文、start=0、end=len，可回指）。"""
    judge = _judge(lambda ctx: _make_proof(ctx, verdict="duplicate",
                                           unbound_quotes=True))
    out = decide_service.decide_for_task(
        _history(), [], current=_current(), judge_callable=judge,
        judge_in_chain=True, **_boundary_kwargs())
    assert out.decision == "重复"
    assert out.duplicate_ids == ("item-A",)
    assert out.pair_results[0].code == "JUDGE_EQUIVALENT"
    judged = _adjudicate(lambda ctx: _make_proof(
        ctx, verdict="duplicate", unbound_quotes=True))
    assert judged.outcome == "equivalent"
    assert judge_pair.EVIDENCE_UNBOUND in judged.evidence_warnings
    assert judge_pair.EVIDENCE_FALLBACK_FULL_TEXT in judged.evidence_warnings
    fallback = {e.record_id: e for e in judged.used_evidence
                if e.start == 0 and e.end == len(e.quote)}
    assert fallback[H_ID].quote == H_TEXT
    assert fallback[C_ID].quote == CUR_TEXT
    assert H_TEXT[0:len(H_TEXT)] == fallback[H_ID].quote  # 审计可回指


def test_conclusion_contradicts_now_audit_only_still_signs():
    """提交一（解除一票否决）：双序 duplicate 但机侧数值结论"不一致"
    → 仍签重复；CONCLUSION_CONTRADICTS 只进证据诊断。"""
    judge = _judge(lambda ctx: _make_proof(ctx, verdict="duplicate",
                                           numeric_conclusion="不一致"))
    out = decide_service.decide_for_task(
        _history(), [], current=_current(), judge_callable=judge,
        judge_in_chain=True, **_boundary_kwargs())
    assert out.decision == "重复"
    assert out.pair_results[0].code == "JUDGE_EQUIVALENT"
    judged = _adjudicate(lambda ctx: _make_proof(
        ctx, verdict="duplicate", numeric_conclusion="不一致"))
    assert judged.outcome == "equivalent"
    assert judge_pair.CONCLUSION_CONTRADICTS in judged.evidence_warnings


def test_red_stolen_proof_rebinding_rejected():
    """换文复用：证明绑定的正文 sha 与上下文不符 → INVALID_OUTPUT 未决。"""
    judge = _judge(lambda ctx: _make_proof(ctx, verdict="duplicate",
                                           stolen_binding=True))
    out = decide_service.decide_for_task(
        _history(), [], current=_current(), judge_callable=judge,
        judge_in_chain=True, **_boundary_kwargs())
    assert out.decision == "边界case/疑难case"
    assert out.internal_code == "EVIDENCE_INVALID"
    assert judge_pair.INVALID_OUTPUT in out.pair_results[0].detail


def test_red_verdict_failure_unresolved():
    """证明 verdict=failure → 未决单列（不冒充存疑也不冒签）。"""
    judge = _judge(lambda ctx: _make_proof(ctx, verdict="failure"))
    out = decide_service.decide_for_task(
        _history(), [], current=_current(), judge_callable=judge,
        judge_in_chain=True, **_boundary_kwargs())
    assert out.decision == "边界case/疑难case"
    assert judge_pair.JUDGE_FAILURE in out.pair_results[0].detail


# ---------------------------------------------------------------- D. 聚合 strict 红测（合同 §五红线）

def _plan_with(*ctxs):
    return FrozenRecallPlan(
        version="rrf_v1_k60_30_10",
        required={c["record_id"]: c for c in ctxs})


def _conflict_pair(history_ctx, current_ctx):
    return PairResult(
        pair_id="pair-test-1",
        history_record_id=history_ctx["record_id"],
        current_record_id=current_ctx["record_id"],
        history_item_id=history_ctx["item_id"],
        current_item_id=current_ctx["item_id"],
        history_arrival_seq=history_ctx["arrival_seq"],
        current_arrival_seq=current_ctx["arrival_seq"],
        history_raw_hash=history_ctx["raw_hash"],
        current_raw_hash=current_ctx["raw_hash"],
        pipeline_version="dedup_v1",
        outcome="conflict", code="VERIFIED_CONFLICT", detail="已证伪。",
        aligned_facts=(), verified_conflicts=(), unresolved_fields=(),
        used_evidence=())


def test_red_strict_required_nonempty_pairs_empty_never_signs_not_duplicate():
    """合同 §五红线①：strict 口径 required 非空而 pair_results 空 → 不得签
    不重复（FACT_INCOMPLETE 落边界）。"""
    history = _ctx(H_ID, "item-A", H_TEXT, 1)
    current = _current()
    out = aggregate_module.aggregate(
        current, _plan_with(history), [],
        coverage=CoverageStatus(visible_seq=10, prepared_seq=10, complete=True),
        strict_required_coverage=True)
    assert out.decision == "边界case/疑难case"
    assert out.internal_code == "FACT_INCOMPLETE"
    assert out.duplicate_ids == ()


def test_red_legacy_same_shape_still_signs_not_duplicate_frozen():
    """对照：旧路径（strict 默认关）同形态保持 L11/T045 冻结契约"∅→不重复"
    逐字节不动（test_p16_d_matrix 钉死口径不变）。"""
    history = _ctx(H_ID, "item-A", H_TEXT, 1)
    current = _current()
    out = aggregate_module.aggregate(
        current, _plan_with(history), [],
        coverage=CoverageStatus(visible_seq=10, prepared_seq=10, complete=True))
    assert out.decision == "不重复"
    assert out.internal_code == "NO_DUPLICATE_FOUND"


def test_red_strict_missing_candidate_never_signs_not_duplicate():
    """合同 §五红线②：候选漏判（required 两员、对级只见一员）→ 不得签
    不重复（strict 口径同样 FACT_INCOMPLETE 落边界）。"""
    h1 = _ctx(H_ID, "item-A", H_TEXT, 1)
    h2 = _ctx(D_ID, "item-B", C1_TEXT, 2)
    current = _current()
    pair = _conflict_pair(h1, current)
    out = aggregate_module.aggregate(
        current, _plan_with(h1, h2), [pair],
        coverage=CoverageStatus(visible_seq=10, prepared_seq=10, complete=True),
        strict_required_coverage=True)
    assert out.decision == "边界case/疑难case"
    assert out.internal_code == "FACT_INCOMPLETE"
    assert out.duplicate_ids == ()


# ---------------------------------------------------------------- E. 提交前唯一聚合（合同 §五 + ④）

def test_final_aggregation_is_what_enters_commit_write_plan():
    """④：初聚合（边界）只用于定位未决对不写主记录；判官签发后的终聚合
    （重复）才进 commit 写入计划——主记录五字段与审计证据同源于终态。"""
    history, current = _history(), _current()
    decide = decide_service.decide_for_task(
        history, [], current=current, judge_callable=_judge(_sign_duplicate),
        judge_in_chain=True, coverage_complete=True)
    assert decide.decision == "重复"   # 返回的即终聚合
    ctx = CommitContext(
        scope_id="default", business_date="2026-09-26", arrival_seq=3,
        current=current, candidates=(history,), visible_seq=10,
        prepared_seq=10, coverage_complete=True)
    plan = build_commit_write_plan(ctx, decide, audit_complete=True)
    assert plan.main_record.decision == "重复"
    assert plan.main_record.duplicate_ids == ("item-A",)
    assert plan.main_record.internal_code == "JUDGE_EQUIVALENT"  # 提交一新码
    bases = {record.basis for record in plan.audit_batch.records}
    assert bases == {"JUDGE_EQUIVALENT"}   # 审计 basis=终态对级码，非初态未决码
    # 审计证据段实填判官证明引文（非空壳）
    record = plan.audit_batch.records[0]
    assert record.history_evidence["quote"] == H_TEXT[0:4]


def test_commit_one_env_on_without_judge_fail_closed_boundary(monkeypatch):
    """commit_one 接线点只读核对（coordinator.py 零改动）：env 开时
    commit_one→decide_for_task 走新路径，判官未注入（装配层职责外）
    → fail-closed 边界进人工，绝不冒签重复/不重复。"""
    monkeypatch.setenv(judge_pair.JUDGE_IN_CHAIN_ENV, "1")
    store = FakeCommitStore()
    history, current = _history(), _current()
    ctx = CommitContext(
        scope_id="default", business_date="2026-09-26", arrival_seq=3,
        current=current, candidates=(history,), visible_seq=10,
        prepared_seq=10, coverage_complete=True)
    outcome = commit_one(ctx, store, audit_complete=True)
    assert outcome.state == "committed"
    assert outcome.decide_outcome.decision == "边界case/疑难case"
    record = store.main_records[current["record_id"]]
    assert record.decision == "边界case/疑难case"
    assert record.duplicate_ids == ()
    assert judge_pair.JUDGE_NOT_INJECTED in "".join(
        outcome.decide_outcome.unresolved_fields) or any(
        judge_pair.JUDGE_NOT_INJECTED in p.detail
        for p in outcome.decide_outcome.pair_results)
