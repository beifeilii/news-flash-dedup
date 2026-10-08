"""P1 联调装配包：真件 judge_callable 接线（2026-10-09，施工窗 P1-a，
合流树 p1-integrated）。

依据（冻结件）：
- 接口合同 log/temp/p1-interface-contract-v1.md §一-§五；
- 判定宪章 policy_v2（机检语义口径）；
- 工程正文 log/判定链改造最终方案-Codex-20261008.md §3.1/§3.2。

覆盖（decide_for_task 级，真件+真开关；LLM 边界罐装，机检/适配/聚合全真）：
- 双序判重复+机验过 → 签发 FACT_EQUIVALENT（件级重复，真证据入列）；
- 双序不重复+机检证伪轴 → VERIFIED_CONFLICT（dimension 映射钉）；
- 空口不重复（无机检轴）→ 合同 §二 降级不签（JUDGE_DOUBTFUL 未决进人工）；
- 机验拦截（P_NUMERIC）→ MV_REJECTED 未决进人工；
- 超时（真件慢 LLM+真线程硬超时）→ TIMEOUT 未决；
- 预算尽（真 ProcessingBudget 越软界）→ BUDGET_EXCEEDED 未决；
- 开关关=逐字节老行为（IN_CHAIN 关判官零调用；PROOF 关→装配返 None=
  未注入 fail-closed 未决）；
- 适配器直射钉：validate_proof 录取线过/预算异常映射/failure·invalid
  verdict/cache_key 合同公式/time→stage·polarity→event 维度映射；
- 装配透传钉：commit_one 显式注入生效+env 关未注入 fail-closed。
"""

from __future__ import annotations

import hashlib
import json
import time
from types import SimpleNamespace

import pytest

from news_flash_dedup.commit.coordinator import CommitContext, commit_one
from news_flash_dedup.commit.fake_store import FakeCommitStore
from news_flash_dedup.decide import judge_adapter, judge_pair
from news_flash_dedup.decide import judge_proof as jp
from news_flash_dedup.decide import llm_residual as lr
from news_flash_dedup.decide import service as decide_service
from news_flash_dedup.runtime_budget import (
    ProcessingBudget, RuntimeBudgetConfig,
)


# ---------------------------------------------------------------- 夹具（P1-b 同形）

def _ctx(record_id, item_id, text, arrival_seq, **kw):
    return {
        "record_id": record_id, "item_id": item_id, "text": text,
        "raw_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "scope_id": "default", "business_date": "2026-09-26",
        "arrival_seq": arrival_seq, "pipeline_version": "dedup_v1",
        **kw,
    }


def _fact(record_id, subject):
    """最小合法事实夹具（P1-b 同形；文本首字=subject[0]，证据 offset [0,1)）。"""
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


def _history(text, subject="甲公司"):
    h = _ctx(H_ID, "item-A", text, 1)
    h["facts"] = [_fact(H_ID, subject)]
    return h


def _current(text, subject="乙公司"):
    c = _ctx(C_ID, "item-C", text, 3)
    c["facts"] = [_fact(C_ID, subject)]
    return c


# ---------------------------------------------------------------- 真件（LLM 边界罐装）

class _MockLLM:
    """按 (文本A, 文本B) 路由罐装判定（P1-a 同形）；sleep_s 模拟慢调用。"""

    def __init__(self, responses, sleep_s=0.0):
        self.responses = responses
        self.sleep_s = sleep_s
        self.calls = []

    def __call__(self, model, system, user, timeout_s=None):
        self.calls.append(user)
        if self.sleep_s:
            time.sleep(self.sleep_s)
        a = user.split("【文本A】\n", 1)[1].split("\n\n【文本B】\n", 1)[0]
        b = user.split("\n\n【文本B】\n", 1)[1]
        return self.responses[(a, b)], 0.01


def _jjson(decision, ea, eb, *, na=(), nb=(), ta=(), tb=(),
           ncon="一致", tcon="一致"):
    return json.dumps({
        "decision": decision, "reason": "r",
        "evidence_a": list(ea), "evidence_b": list(eb),
        "numeric_check": {"conclusion": ncon, "numbers_a": list(na),
                          "numbers_b": list(nb)},
        "time_check": {"conclusion": tcon, "times_a": list(ta),
                       "times_b": list(tb)},
    }, ensure_ascii=False)


def _switches_on(monkeypatch):
    """真开关：DEDUP_JUDGE_PROOF（真件装配闸）+ DEDUP_JUDGE_IN_CHAIN（主链闸）。"""
    monkeypatch.setenv(jp.JUDGE_PROOF_ENV, "1")
    monkeypatch.setenv(judge_pair.JUDGE_IN_CHAIN_ENV, "1")


def _real_callable(responses, *, budget=None, sleep_s=0.0):
    """真件 judge_callable：真 adapter+真 judge_proof 核验+真装配闸
    （仅 LLM 边界罐装）。"""
    cfg = lr.ResidualJudgeConfig(mv_mode=lr.MV_AUDIT)
    judge = lr.SyncResidualJudge(
        cfg, call_fn=_MockLLM(responses, sleep_s=sleep_s), budget=budget)
    cb = judge_adapter.build_judge_callable(judge=judge)
    assert cb is not None
    return cb


# 文本对（机检口径设计：duplicate 绿/ND 无轴/ND 数值冲突/ND 主体轴）
DUP_H = "甲公司9月24日公告营收100万元。"
DUP_C = "乙公司9月24日公告营收100万元。"
ND0_H = "甲公司预期业绩增长。"
ND0_C = "乙公司预期业绩增长。"
NUM_H = "甲公司9月24日公告营收787.8万元。"
NUM_C = "乙公司9月24日公告营收789.8万元。"
SUB_H = "甲公司（600001）主力净流入5亿。"
SUB_C = "乙公司（002919）主力净流入5亿。"

DUP_RESP = {
    (DUP_H, DUP_C): _jjson("重复", ("9月24日公告营收100万元",),
                           ("9月24日公告营收100万元",),
                           na=("100万",), nb=("100万",),
                           ta=("9月24日",), tb=("9月24日",)),
    (DUP_C, DUP_H): _jjson("重复", ("9月24日公告营收100万元",),
                           ("9月24日公告营收100万元",),
                           na=("100万",), nb=("100万",),
                           ta=("9月24日",), tb=("9月24日",)),
}
ND0_RESP = {
    (ND0_H, ND0_C): _jjson("不重复", ("预期业绩增长",), ("预期业绩增长",),
                           ncon="无关键数值", tcon="均无时间"),
    (ND0_C, ND0_H): _jjson("不重复", ("预期业绩增长",), ("预期业绩增长",),
                           ncon="无关键数值", tcon="均无时间"),
}
NUM_RESP = {
    (NUM_H, NUM_C): _jjson("重复", ("9月24日公告营收787.8万元",),
                           ("9月24日公告营收789.8万元",),
                           na=("787.8万",), nb=("789.8万",),
                           ta=("9月24日",), tb=("9月24日",),
                           ncon="一致"),
    (NUM_C, NUM_H): _jjson("重复", ("9月24日公告营收789.8万元",),
                           ("9月24日公告营收787.8万元",),
                           na=("789.8万",), nb=("787.8万",),
                           ta=("9月24日",), tb=("9月24日",),
                           ncon="一致"),
}
SUB_RESP = {
    (SUB_H, SUB_C): _jjson("不重复", ("主力净流入5亿",), ("主力净流入5亿",),
                           na=("5亿",), nb=("5亿",),
                           ncon="一致", tcon="均无时间"),
    (SUB_C, SUB_H): _jjson("不重复", ("主力净流入5亿",), ("主力净流入5亿",),
                           na=("5亿",), nb=("5亿",),
                           ncon="一致", tcon="均无时间"),
}


# ---------------------------------------------------------------- 联调红测（decide_for_task 级）

def test_real_judge_signs_duplicate_fact_equivalent(monkeypatch):
    """双序判重复+机验过（真件+真开关）→ 签发 FACT_EQUIVALENT（件级重复，
    对级真证据入列，duplicate_ids 只收已证件）。"""
    _switches_on(monkeypatch)
    cb = _real_callable(DUP_RESP)
    out = decide_service.decide_for_task(
        _history(DUP_H), [], current=_current(DUP_C), judge_callable=cb,
        coverage_complete=True)
    assert out.decision == "重复"
    assert out.duplicate_ids == ("item-A",)
    assert out.internal_code == "FACT_EQUIVALENT"
    pair = out.pair_results[0]
    assert pair.outcome == "equivalent" and pair.code == "FACT_EQUIVALENT"
    assert len(pair.used_evidence) > 0
    quotes = {e.quote for e in pair.used_evidence}
    assert "9月24日公告营收100万元" in quotes     # 真引文（非空壳）


def test_real_judge_signs_conflict_with_subject_axis(monkeypatch):
    """双序不重复+机检主体轴 → VERIFIED_CONFLICT（件级不重复；
    dimension=subject 直钉，双侧证伪引文绑真 offset）。"""
    _switches_on(monkeypatch)
    cb = _real_callable(SUB_RESP)
    out = decide_service.decide_for_task(
        _history(SUB_H), [], current=_current(SUB_C), judge_callable=cb,
        coverage_complete=True)
    assert out.decision == "不重复"
    pair = out.pair_results[0]
    assert pair.outcome == "conflict" and pair.code == "VERIFIED_CONFLICT"
    assert len(pair.verified_conflicts) == 1
    conflict = pair.verified_conflicts[0]
    assert conflict.field_path == "judge_falsification.subject"
    assert conflict.history_evidence.quote == "600001"
    assert conflict.current_evidence.quote == "002919"


def test_real_judge_empty_not_duplicate_downgraded(monkeypatch):
    """空口不重复（判官双序 不重复 但机检无证伪轴）→ 合同 §二 降级不签：
    JUDGE_DOUBTFUL 未决进人工，不得入"有效排除"。"""
    _switches_on(monkeypatch)
    cb = _real_callable(ND0_RESP)
    out = decide_service.decide_for_task(
        _history(ND0_H), [], current=_current(ND0_C), judge_callable=cb,
        coverage_complete=True)
    assert out.decision == "边界case/疑难case"
    assert out.duplicate_ids == ()
    pair = out.pair_results[0]
    assert pair.outcome == "unresolved"
    assert pair.code == "SUBJECT_UNRESOLVED"
    assert judge_pair.JUDGE_DOUBTFUL in pair.detail


def test_real_judge_machine_numeric_conflict_rejected(monkeypatch):
    """机验拦截（判官双序 重复 声称数值一致，机抽 787.8万/789.8万 双向
    差异=P_NUMERIC）→ MV_REJECTED 未决进人工（EVIDENCE_INVALID 码）。"""
    _switches_on(monkeypatch)
    cb = _real_callable(NUM_RESP)
    out = decide_service.decide_for_task(
        _history(NUM_H), [], current=_current(NUM_C), judge_callable=cb,
        coverage_complete=True)
    assert out.decision == "边界case/疑难case"
    pair = out.pair_results[0]
    assert pair.outcome == "unresolved"
    assert pair.code == "EVIDENCE_INVALID"
    assert judge_pair.MV_REJECTED in pair.detail


def test_real_judge_timeout_unresolved(monkeypatch):
    """超时（真件慢 LLM + adjudicate_pair 真线程硬超时）→ TIMEOUT 未决
    （DEPENDENCY_TIMEOUT 码）。"""
    _switches_on(monkeypatch)
    cb = _real_callable(DUP_RESP, sleep_s=2.0)
    out = decide_service.decide_for_task(
        _history(DUP_H), [], current=_current(DUP_C), judge_callable=cb,
        judge_timeout_s=0.2, coverage_complete=True)
    assert out.decision == "边界case/疑难case"
    pair = out.pair_results[0]
    assert pair.outcome == "unresolved"
    assert pair.code == "DEPENDENCY_TIMEOUT"
    assert judge_pair.TIMEOUT in pair.detail


def test_real_judge_budget_exhausted_unresolved(monkeypatch):
    """预算尽（真 ProcessingBudget 越 prepare_soft→judge 抛预算字面→
    adapter 映射 JudgeBudgetExceeded）→ BUDGET_EXCEEDED 未决
    （CANDIDATE_BUDGET_EXHAUSTED 码）。"""
    _switches_on(monkeypatch)
    cfg = RuntimeBudgetConfig(
        max_processing_s=100.0, prepare_soft_s=10.0, commit_target_s=1.0,
        channel_timeout_s=0.3, per_call_timeout_s=3.0, max_model_calls=20,
        shared_append_budget=0)
    budget = ProcessingBudget(accepted_at_mono=0.0, config=cfg,
                              clock_mono=lambda: 50.0)   # 50>10=越软界
    cb = _real_callable(DUP_RESP, budget=budget)
    out = decide_service.decide_for_task(
        _history(DUP_H), [], current=_current(DUP_C), judge_callable=cb,
        coverage_complete=True)
    assert out.decision == "边界case/疑难case"
    pair = out.pair_results[0]
    assert pair.outcome == "unresolved"
    assert pair.code == "CANDIDATE_BUDGET_EXHAUSTED"
    assert judge_pair.BUDGET_EXCEEDED in pair.detail


def test_switch_off_byte_identical_old_behavior(monkeypatch):
    """开关关=逐字节老行为：DEDUP_JUDGE_IN_CHAIN 缺席（PROOF 开+真件
    注入也不进主链）→ 判官零调用，结果与无注入逐字段同构。"""
    monkeypatch.setenv(jp.JUDGE_PROOF_ENV, "1")
    monkeypatch.delenv(judge_pair.JUDGE_IN_CHAIN_ENV, raising=False)
    cb = _real_callable(DUP_RESP)
    out = decide_service.decide_for_task(
        _history(DUP_H), [], current=_current(DUP_C), judge_callable=cb,
        coverage_complete=True)
    out_plain = decide_service.decide_for_task(
        _history(DUP_H), [], current=_current(DUP_C),
        coverage_complete=True)
    assert out.to_public_dict() == out_plain.to_public_dict()
    assert out.internal_code == out_plain.internal_code
    assert dict(out.pair_codes) == dict(out_plain.pair_codes)
    assert out.unresolved_fields == out_plain.unresolved_fields


def test_proof_switch_off_means_not_injected(monkeypatch):
    """DEDUP_JUDGE_PROOF 关 → build_judge_callable 返 None（未注入）；
    主链开+未注入 → JUDGE_NOT_INJECTED fail-closed 未决进人工。"""
    monkeypatch.delenv(jp.JUDGE_PROOF_ENV, raising=False)
    monkeypatch.setenv(judge_pair.JUDGE_IN_CHAIN_ENV, "1")
    assert judge_adapter.build_judge_callable() is None
    out = decide_service.decide_for_task(
        _history(DUP_H), [], current=_current(DUP_C), judge_callable=None,
        coverage_complete=True)
    assert out.decision == "边界case/疑难case"
    pair = out.pair_results[0]
    assert pair.outcome == "unresolved"
    assert judge_pair.JUDGE_NOT_INJECTED in pair.detail


# ---------------------------------------------------------------- 适配器直射钉（合同 §一 形态）

def _order_ctx(pair_id, text_h, text_c, order="ab"):
    pair = SimpleNamespace(
        pair_id=pair_id, history_record_id=H_ID, current_record_id=C_ID,
        history_item_id="item-A", current_item_id="item-C")
    base = judge_pair.build_pair_context(pair, history_text=text_h,
                                         current_text=text_c)
    return judge_pair._order_context(base, order)


def test_adapter_proof_passes_validate_and_cache_key(monkeypatch):
    """适配器产物过 validate_proof 录取线（duplicate）；cache_key=合同
    §一 公式重算；quotes/三态结论/machine_verify 段全合规。"""
    _switches_on(monkeypatch)
    cb = _real_callable(DUP_RESP)
    ctx = _order_ctx("p1", DUP_H, DUP_C, "ab")
    proof = cb(ctx)
    validated = judge_pair.validate_proof(proof, ctx)
    assert validated["verdict"] == "duplicate"
    assert proof["policy_version"] == "policy_v2"
    assert proof["machine_verify"] == {"mode": "gate", "rules_triggered": [],
                                       "passed": True}
    assert proof["numeric_check"]["conclusion"] == "一致"
    assert proof["time_check"]["conclusion"] == "一致"
    assert proof["time_check"]["anchors_a"] == ["924"]
    expected_key = judge_pair.compute_cache_key(
        proof["model_version"], proof["prompt_sha256"],
        proof["policy_version"], "ab",
        proof["text_a_sha256"], proof["text_b_sha256"])
    assert proof["cache_key"] == expected_key
    assert proof["item_a_id"] == "item-A" and proof["item_b_id"] == "item-C"
    assert proof["judged_at"]                            # ISO 非空（合同必填）


def test_adapter_falsification_dimension_mapping(monkeypatch):
    """维度映射钉：subject→subject（合同原生）；time→stage、polarity→event
    （宪章族最近邻，合同四维闭表）。"""
    _switches_on(monkeypatch)
    # subject 轴（合同原生维）
    cb = _real_callable(SUB_RESP)
    ctx = _order_ctx("p1", SUB_H, SUB_C, "ab")
    proof = cb(ctx)
    assert proof["verdict"] == "not_duplicate"
    validated = judge_pair.validate_proof(proof, ctx)
    assert validated["falsification"]["dimension"] == "subject"
    assert validated["falsification"]["evidence_a"]["text"] == "600001"
    # time 轴 → 合同 stage（宪章 §二-3 时间/阶段同族）
    t_h, t_c = "甲公司3月4日公告投产。", "乙公司3月5日公告投产。"
    resp = {
        (t_h, t_c): _jjson("不重复", ("3月4日公告投产",), ("3月5日公告投产",),
                           ncon="无关键数值", tcon="不一致",
                           ta=("3月4日",), tb=("3月5日",)),
        (t_c, t_h): _jjson("不重复", ("3月5日公告投产",), ("3月4日公告投产",),
                           ncon="无关键数值", tcon="不一致",
                           ta=("3月5日",), tb=("3月4日",)),
    }
    cb2 = _real_callable(resp)
    ctx2 = _order_ctx("p2", t_h, t_c, "ab")
    proof2 = cb2(ctx2)
    assert proof2["falsification"]["dimension"] == "stage"
    assert proof2["falsification"]["evidence_a"]["text"] == "3月4日"
    assert proof2["time_check"]["conclusion"] == "不一致"
    # polarity 轴 → 合同 event（宪章 §二-5 方向冲突=事件族）
    p_h, p_c = "甲公司主力净流入5亿。", "乙公司主力净流出5亿。"
    resp3 = {
        (p_h, p_c): _jjson("不重复", ("净流入5亿",), ("净流出5亿",),
                           na=("5亿",), nb=("5亿",),
                           ncon="一致", tcon="均无时间"),
        (p_c, p_h): _jjson("不重复", ("净流出5亿",), ("净流入5亿",),
                           na=("5亿",), nb=("5亿",),
                           ncon="一致", tcon="均无时间"),
    }
    cb3 = _real_callable(resp3)
    ctx3 = _order_ctx("p3", p_h, p_c, "ab")
    proof3 = cb3(ctx3)
    assert proof3["falsification"]["dimension"] == "event"
    assert proof3["falsification"]["evidence_a"]["text"] == "净流入"


def test_adapter_verdict_failure_and_invalid(monkeypatch):
    """败状映射：LLM 调用失败→verdict=failure；JSON 非法→verdict=invalid
    （合同五态，validate_proof 收）。"""
    _switches_on(monkeypatch)

    def _boom(model, system, user, timeout_s=None):
        raise ConnectionError("mock net down")

    cfg = lr.ResidualJudgeConfig(mv_mode=lr.MV_AUDIT, max_retries=0)
    judge = lr.SyncResidualJudge(cfg, call_fn=_boom)
    cb = judge_adapter.build_judge_callable(judge=judge)
    ctx = _order_ctx("p1", DUP_H, DUP_C, "ab")
    proof = cb(ctx)
    assert proof["verdict"] == "failure"
    validated = judge_pair.validate_proof(proof, ctx)
    assert validated["verdict"] == "failure"

    cb2 = _real_callable({(DUP_H, DUP_C): "not-json{{{"})
    proof2 = cb2(ctx)
    assert proof2["verdict"] == "invalid"
    assert judge_pair.validate_proof(proof2, ctx)["verdict"] == "invalid"


def test_adapter_budget_raises_contract_exception(monkeypatch):
    """预算字面错误 → JudgeBudgetExceeded（合同 BUDGET_EXCEEDED 异常通道）。"""
    _switches_on(monkeypatch)
    cfg = RuntimeBudgetConfig(
        max_processing_s=100.0, prepare_soft_s=10.0, commit_target_s=1.0,
        channel_timeout_s=0.3, per_call_timeout_s=3.0, max_model_calls=20,
        shared_append_budget=0)
    budget = ProcessingBudget(accepted_at_mono=0.0, config=cfg,
                              clock_mono=lambda: 50.0)
    cb = _real_callable(DUP_RESP, budget=budget)
    ctx = _order_ctx("p1", DUP_H, DUP_C, "ab")
    with pytest.raises(judge_pair.JudgeBudgetExceeded):
        cb(ctx)


def test_adapter_empty_not_duplicate_carries_no_falsification(monkeypatch):
    """无机检轴的不重复：proof 不携 falsification、machine_verify.passed=
    true（P_NO_AXIS 不入机验闸）→ 合同 §二 降级 doubtful（非硬失败）。"""
    _switches_on(monkeypatch)
    cb = _real_callable(ND0_RESP)
    ctx = _order_ctx("p1", ND0_H, ND0_C, "ab")
    proof = cb(ctx)
    assert proof["verdict"] == "not_duplicate"
    assert "falsification" not in proof
    assert proof["machine_verify"]["passed"] is True
    validated = judge_pair.validate_proof(proof, ctx)
    assert validated["downgrade_not_duplicate"] is True


def test_adapter_numeric_mv_rejection_shape(monkeypatch):
    """机验拦截形态：P_NUMERIC 入 rules_triggered、passed=false →
    validate_proof 抛 MV_REJECTED（硬失败通道）。"""
    _switches_on(monkeypatch)
    cb = _real_callable(NUM_RESP)
    ctx = _order_ctx("p1", NUM_H, NUM_C, "ab")
    proof = cb(ctx)
    assert proof["machine_verify"]["passed"] is False
    assert "P_NUMERIC" in proof["machine_verify"]["rules_triggered"]
    assert proof["numeric_check"]["conclusion"] == "不一致"
    with pytest.raises(judge_pair.JudgeProofError) as excinfo:
        judge_pair.validate_proof(proof, ctx)
    assert str(excinfo.value).startswith(judge_pair.MV_REJECTED)


# ---------------------------------------------------------------- 装配透传钉（commit_one）

def test_commit_one_explicit_judge_injection_signs(monkeypatch):
    """commit_one 装配透传：显式注入真件（参数路径）→ 终聚合重复入主记录。"""
    _switches_on(monkeypatch)
    cb = _real_callable(DUP_RESP)
    store = FakeCommitStore()
    ctx = CommitContext(
        scope_id="default", business_date="2026-09-26", arrival_seq=3,
        current=_current(DUP_C), candidates=(_history(DUP_H),),
        visible_seq=10, prepared_seq=10, coverage_complete=True)
    outcome = commit_one(ctx, store, audit_complete=True,
                         judge_callable=cb)
    assert outcome.state == "committed"
    assert outcome.decide_outcome.decision == "重复"
    record = store.main_records[C_ID]
    assert record.decision == "重复"
    assert record.duplicate_ids == ("item-A",)


def test_commit_one_env_proof_off_fail_closed(monkeypatch):
    """commit_one 未显式注入+env 装配闸关（PROOF 缺席、IN_CHAIN 开）
    → 真件不装配（None）→ fail-closed 边界进人工，绝不冒签。"""
    monkeypatch.delenv(jp.JUDGE_PROOF_ENV, raising=False)
    monkeypatch.setenv(judge_pair.JUDGE_IN_CHAIN_ENV, "1")
    store = FakeCommitStore()
    ctx = CommitContext(
        scope_id="default", business_date="2026-09-26", arrival_seq=3,
        current=_current(DUP_C), candidates=(_history(DUP_H),),
        visible_seq=10, prepared_seq=10, coverage_complete=True)
    outcome = commit_one(ctx, store, audit_complete=True)
    assert outcome.state == "committed"
    assert outcome.decide_outcome.decision == "边界case/疑难case"
    record = store.main_records[C_ID]
    assert record.decision == "边界case/疑难case"
    assert record.duplicate_ids == ()
