"""P16-C 五字段汇总 aggregate 实施测试（对应 03:09 验收标尺）。

验收标尺：
1. decision 严格耦合（重复 ↔ 非空；其余两类 []）
2. arrival_seq 升序去重、首命中不 break（INV-4）、非传递扩组（L01）
3. 非法成员拒绝门六形态（自身/未来/异域/跨日/未规划/错绑，INV-12/L08）
4. 聚合不返回 failed（INV-8）；五字段封闭校验（INV-9）；reason 非空无内部码
5. 红绿双落盘（沿用版本化命名）+ 全量单测落盘
"""

from __future__ import annotations

import pytest

from news_flash_dedup.compare import aggregate as aggregate_module
from news_flash_dedup.compare.pair_compare import (
    EQUIVALENT_CODES,
    PairIssue,
    PairResult,
    UNRESOLVED_CODES,
)


def _ctx(*, record_id, item_id, text="t", raw_hash="h", scope_id="default",
         business_date="2026-09-26", arrival_seq=1, pipeline_version="dedup_v1"):
    return {"record_id": record_id, "item_id": item_id, "text": text,
            "raw_hash": raw_hash, "scope_id": scope_id,
            "business_date": business_date, "arrival_seq": arrival_seq,
            "pipeline_version": pipeline_version}


def _pair(history, current, *, outcome="equivalent", code="FACT_EQUIVALENT",
          detail="stub"):
    return PairResult(
        pair_id=f"{history['record_id']}|{current['record_id']}",
        history_record_id=history["record_id"],
        current_record_id=current["record_id"],
        history_item_id=history["item_id"],
        current_item_id=current["item_id"],
        history_arrival_seq=history["arrival_seq"],
        current_arrival_seq=current["arrival_seq"],
        history_raw_hash=history["raw_hash"],
        current_raw_hash=current["raw_hash"],
        pipeline_version="dedup_v1",
        outcome=outcome,
        code=code,
        detail=detail,
        aligned_facts=(),
        verified_conflicts=(),
        unresolved_fields=(),
        used_evidence=(),
        budget_at=0,
    )


def _coverage(complete=True):
    return aggregate_module.CoverageStatus(visible_seq=10, prepared_seq=10, complete=complete)


# ---------- 模块入口闸 ----------

def test_module_exposes_aggregate():
    assert hasattr(aggregate_module, "aggregate"), \
        "aggregate module must expose aggregate()"
    assert hasattr(aggregate_module, "AggregateOutcome")
    assert hasattr(aggregate_module, "FrozenRecallPlan")
    assert hasattr(aggregate_module, "CoverageStatus")


# ---------- 标尺 1：decision 严格耦合 ----------

def test_decision_duplicate_requires_nonempty_ids():
    history = _ctx(record_id="a" * 64, item_id="item-A", arrival_seq=1)
    current = _ctx(record_id="b" * 64, item_id="item-B", arrival_seq=2)
    plan = {"version": "rrf_v1", "required": {history["record_id"]: history}}
    out = aggregate_module.aggregate(
        current, plan, [_pair(history, current)],
        coverage=_coverage(True),
    )
    assert out.decision == "重复"
    assert out.duplicate_ids == ("item-A",)


def test_decision_not_duplicate_requires_empty_ids():
    history = _ctx(record_id="a" * 64, item_id="item-A", arrival_seq=1)
    current = _ctx(record_id="b" * 64, item_id="item-B", arrival_seq=2)
    plan = {"version": "rrf_v1", "required": {history["record_id"]: history}}
    out = aggregate_module.aggregate(
        current, plan, [_pair(history, current, outcome="conflict",
                              code="VERIFIED_CONFLICT", detail="差异")],
        coverage=_coverage(True),
    )
    assert out.decision == "不重复"
    assert out.duplicate_ids == ()


# ---------- 标尺 2：arrival_seq 升序去重 + 首命中不 break + 非传递 ----------

def test_lists_sorted_by_arrival_seq_and_deduped():
    history_a = _ctx(record_id="a" * 64, item_id="item-A", arrival_seq=1)
    history_d = _ctx(record_id="b" * 64, item_id="item-D", arrival_seq=2)
    current = _ctx(record_id="c" * 64, item_id="item-C", arrival_seq=3)
    plan = {"version": "rrf_v1", "required": {
        history_a["record_id"]: history_a,
        history_d["record_id"]: history_d,
    }}
    out = aggregate_module.aggregate(
        current, plan,
        [_pair(history_d, current), _pair(history_d, current),  # 重复 D
         _pair(history_a, current)],
        coverage=_coverage(True),
    )
    assert out.duplicate_ids == ("item-A", "item-D")


def test_non_transitive_no_synthesized_facts():
    """L01：C 与 A 冲突、与 B 等价 → 列表=[B]，A 不传递入 C。"""
    ctx_a = _ctx(record_id="a" * 64, item_id="item-A", arrival_seq=1)
    ctx_b = _ctx(record_id="b" * 64, item_id="item-B", arrival_seq=2)
    ctx_c = _ctx(record_id="c" * 64, item_id="item-C", arrival_seq=3)
    plan = {"version": "rrf_v1", "required": {
        ctx_a["record_id"]: ctx_a,
        ctx_b["record_id"]: ctx_b,
    }}
    out = aggregate_module.aggregate(
        ctx_c, plan,
        [_pair(ctx_a, ctx_c, outcome="conflict",
               code="VERIFIED_CONFLICT", detail="100 vs 101"),
         _pair(ctx_b, ctx_c)],
        coverage=_coverage(True),
    )
    assert out.decision == "重复"
    assert out.duplicate_ids == ("item-B",)


def test_first_hit_continues_through_required():
    """L02：首命中后不 break；后续候选继续处理。"""
    ctx_a = _ctx(record_id="a" * 64, item_id="item-A", arrival_seq=1)
    ctx_d = _ctx(record_id="b" * 64, item_id="item-D", arrival_seq=2)
    ctx_c = _ctx(record_id="c" * 64, item_id="item-C", arrival_seq=3)
    plan = {"version": "rrf_v1", "required": {
        ctx_a["record_id"]: ctx_a,
        ctx_d["record_id"]: ctx_d,
    }}
    out = aggregate_module.aggregate(
        ctx_c, plan,
        [_pair(ctx_d, ctx_c), _pair(ctx_a, ctx_c)],   # 输入顺序：D 在前
        coverage=_coverage(True),
    )
    assert out.duplicate_ids == ("item-A", "item-D")   # 按 arrival_seq 升序


def test_single_conflict_does_not_end_whole():
    """L03：A=C、D=E；输出 [D]。"""
    ctx_a = _ctx(record_id="a" * 64, item_id="item-A", arrival_seq=1)
    ctx_d = _ctx(record_id="b" * 64, item_id="item-D", arrival_seq=2)
    ctx_c = _ctx(record_id="c" * 64, item_id="item-C", arrival_seq=3)
    plan = {"version": "rrf_v1", "required": {
        ctx_a["record_id"]: ctx_a,
        ctx_d["record_id"]: ctx_d,
    }}
    out = aggregate_module.aggregate(
        ctx_c, plan,
        [_pair(ctx_a, ctx_c, outcome="conflict",
               code="VERIFIED_CONFLICT", detail="差异"),
         _pair(ctx_d, ctx_c)],
        coverage=_coverage(True),
    )
    assert out.decision == "重复"
    assert out.duplicate_ids == ("item-D",)


def test_other_failure_does_not_unconfirm_duplicate():
    """L07：A=E、D=U；A 仍输出，A 在列表。"""
    ctx_a = _ctx(record_id="a" * 64, item_id="item-A", arrival_seq=1)
    ctx_d = _ctx(record_id="b" * 64, item_id="item-D", arrival_seq=2)
    ctx_c = _ctx(record_id="c" * 64, item_id="item-C", arrival_seq=3)
    plan = {"version": "rrf_v1", "required": {
        ctx_a["record_id"]: ctx_a,
        ctx_d["record_id"]: ctx_d,
    }}
    out = aggregate_module.aggregate(
        ctx_c, plan,
        [_pair(ctx_a, ctx_c),
         _pair(ctx_d, ctx_c, outcome="unresolved",
               code="CANDIDATE_BUDGET_EXHAUSTED", detail="预算耗尽")],
        coverage=_coverage(True),
    )
    assert out.decision == "重复"
    assert out.duplicate_ids == ("item-A",)


# ---------- 标尺 3：非法成员拒绝门六形态 ----------

def test_reject_self_record_id():
    ctx_a = _ctx(record_id="a" * 64, item_id="item-A", arrival_seq=1)
    ctx_c = _ctx(record_id="a" * 64, item_id="item-C", arrival_seq=2)  # 同 record_id
    plan = {"version": "rrf_v1", "required": {ctx_a["record_id"]: ctx_a}}
    with pytest.raises(aggregate_module.IllegalMemberError):
        aggregate_module.aggregate(ctx_c, plan, [_pair(ctx_a, ctx_c)],
                                    coverage=_coverage(True))


def test_reject_future_arrival_seq():
    ctx_a = _ctx(record_id="a" * 64, item_id="item-A", arrival_seq=5)  # 未来
    ctx_c = _ctx(record_id="c" * 64, item_id="item-C", arrival_seq=2)
    plan = {"version": "rrf_v1", "required": {ctx_a["record_id"]: ctx_a}}
    # 窗口K 收紧（D30 M-14）：原 raises 元组含顶类 Exception 等价裸收；
    # 实测抛 AggregateError（identity/hash 绑定详述 arrival_seq 倒挂）。
    with pytest.raises(aggregate_module.AggregateError, match="arrival_seq"):
        aggregate_module.aggregate(ctx_c, plan, [_pair(ctx_a, ctx_c)],
                                    coverage=_coverage(True))


def test_reject_cross_scope():
    ctx_a = _ctx(record_id="a" * 64, item_id="item-A", arrival_seq=1,
                 scope_id="other")
    ctx_c = _ctx(record_id="c" * 64, item_id="item-C", arrival_seq=2)
    plan = {"version": "rrf_v1", "required": {ctx_a["record_id"]: ctx_a}}
    with pytest.raises(aggregate_module.IllegalMemberError):
        aggregate_module.aggregate(ctx_c, plan, [_pair(ctx_a, ctx_c)],
                                    coverage=_coverage(True))


def test_reject_cross_business_date():
    ctx_a = _ctx(record_id="a" * 64, item_id="item-A", arrival_seq=1,
                 business_date="2026-09-27")
    ctx_c = _ctx(record_id="c" * 64, item_id="item-C", arrival_seq=2,
                 business_date="2026-09-26")
    plan = {"version": "rrf_v1", "required": {ctx_a["record_id"]: ctx_a}}
    with pytest.raises(aggregate_module.IllegalMemberError):
        aggregate_module.aggregate(ctx_c, plan, [_pair(ctx_a, ctx_c)],
                                    coverage=_coverage(True))


def test_reject_member_not_in_required():
    ctx_a = _ctx(record_id="a" * 64, item_id="item-A", arrival_seq=1)
    ctx_orphan = _ctx(record_id="x" * 64, item_id="item-X", arrival_seq=2)
    ctx_c = _ctx(record_id="c" * 64, item_id="item-C", arrival_seq=3)
    plan = {"version": "rrf_v1", "required": {ctx_a["record_id"]: ctx_a}}  # 不含 orphan
    with pytest.raises(aggregate_module.IllegalMemberError):
        aggregate_module.aggregate(ctx_c, plan, [_pair(ctx_orphan, ctx_c)],
                                    coverage=_coverage(True))


def test_reject_mismatched_item_id_binding():
    """同一 history 但 pair 中 item_id 与 required 不一致 → PairBindingError。"""
    ctx_a = _ctx(record_id="a" * 64, item_id="item-A", arrival_seq=1)
    ctx_c = _ctx(record_id="c" * 64, item_id="item-C", arrival_seq=2)
    plan = {"version": "rrf_v1", "required": {ctx_a["record_id"]: ctx_a}}
    pair = _pair(ctx_a, ctx_c)
    bad_pair = PairResult(
        pair_id=pair.pair_id,
        history_record_id=pair.history_record_id,
        current_record_id=pair.current_record_id,
        history_item_id="item-A-other",  # 错绑
        current_item_id=pair.current_item_id,
        history_arrival_seq=pair.history_arrival_seq,
        current_arrival_seq=pair.current_arrival_seq,
        history_raw_hash=pair.history_raw_hash,
        current_raw_hash=pair.current_raw_hash,
        pipeline_version=pair.pipeline_version,
        outcome=pair.outcome,
        code=pair.code,
        detail=pair.detail,
        aligned_facts=(),
        verified_conflicts=(),
        unresolved_fields=(),
        used_evidence=(),
        budget_at=0,
    )
    # 窗口K 收紧（D30 M-14）：原裸 Exception；实测 AggregateError（identity/hash 错绑）。
    with pytest.raises(aggregate_module.AggregateError,
                       match="identity/hash does not match"):
        aggregate_module.aggregate(ctx_c, plan, [bad_pair],
                                    coverage=_coverage(True))


# ---------- 标尺 4：五字段封闭校验 + reason 无内部码 ----------

def test_no_failed_decision_value():
    """L10：aggregate 不返回 failed；输出 decision ∈ {重复,不重复,边界case/疑难case}。"""
    history = _ctx(record_id="a" * 64, item_id="item-A", arrival_seq=1)
    current = _ctx(record_id="b" * 64, item_id="item-B", arrival_seq=2)
    plan = {"version": "rrf_v1", "required": {history["record_id"]: history}}
    out = aggregate_module.aggregate(
        current, plan, [_pair(history, current)],
        coverage=_coverage(True),
    )
    assert out.decision in {"重复", "不重复", "边界case/疑难case"}
    assert out.decision != "failed"


def test_reason_contains_no_internal_code():
    history = _ctx(record_id="a" * 64, item_id="item-A", arrival_seq=1)
    current = _ctx(record_id="b" * 64, item_id="item-B", arrival_seq=2)
    plan = {"version": "rrf_v1", "required": {history["record_id"]: history}}
    out = aggregate_module.aggregate(
        current, plan, [_pair(history, current)],
        coverage=_coverage(True),
    )
    for code in EQUIVALENT_CODES | UNRESOLVED_CODES | {"VERIFIED_CONFLICT"}:
        assert code not in out.reason, \
            f"public reason {out.reason!r} must not contain internal code {code!r}"


def test_empty_item_id_rejected():
    history = _ctx(record_id="a" * 64, item_id="item-A", arrival_seq=1)
    current = _ctx(record_id="c" * 64, item_id="", arrival_seq=2)
    plan = {"version": "rrf_v1", "required": {history["record_id"]: history}}
    with pytest.raises(aggregate_module.AggregateError):
        aggregate_module.aggregate(current, plan, [_pair(history, current)],
                                    coverage=_coverage(True))


def test_empty_text_rejected():
    history = _ctx(record_id="a" * 64, item_id="item-A", arrival_seq=1)
    current = _ctx(record_id="c" * 64, item_id="item-C", text="", arrival_seq=2)
    plan = {"version": "rrf_v1", "required": {history["record_id"]: history}}
    with pytest.raises(aggregate_module.AggregateError):
        aggregate_module.aggregate(current, plan, [_pair(history, current)],
                                    coverage=_coverage(True))


def test_decision_coupling_invariant_enforced():
    """耦合不变量：duplicate_ids 非空 ⇔ decision='重复'。"""
    history = _ctx(record_id="a" * 64, item_id="item-A", arrival_seq=1)
    current = _ctx(record_id="c" * 64, item_id="item-C", arrival_seq=2)
    plan = {"version": "rrf_v1", "required": {history["record_id"]: history}}
    # 制造一个 conflict + equivalent 同对的异常组合
    out = aggregate_module.aggregate(
        current, plan,
        [_pair(history, current, outcome="conflict",
               code="VERIFIED_CONFLICT", detail="差异")],
        coverage=_coverage(True),
    )
    assert out.decision == "不重复"
    assert out.duplicate_ids == ()
    assert len(out.duplicate_ids) == 0


# ---------- INV-12 双水位 ----------

def test_incomplete_waterfalls_marks_recall_incomplete():
    """双水位未真正覆盖：no duplicates + coverage incomplete → 边界 + RECALL_INCOMPLETE。
    若有 duplicates 则按 L07 不被撤销（重复依然有效）。"""
    history = _ctx(record_id="a" * 64, item_id="item-A", arrival_seq=1)
    current = _ctx(record_id="b" * 64, item_id="item-B", arrival_seq=5)
    plan = {"version": "rrf_v1", "required": {history["record_id"]: history}}
    # 无 pair 进入 → 完全空 required 视图
    out = aggregate_module.aggregate(
        current, plan, [],
        coverage=aggregate_module.CoverageStatus(
            visible_seq=2, prepared_seq=2, complete=False),
    )
    assert out.decision == "边界case/疑难case"
    assert out.duplicate_ids == ()
    assert out.internal_code == "RECALL_INCOMPLETE"


def test_incomplete_waterfalls_does_not_unconfirm_duplicate():
    """L07 + INV-12：双水位未覆盖时，已有的等价重复仍生效 → 重复。"""
    history = _ctx(record_id="a" * 64, item_id="item-A", arrival_seq=1)
    current = _ctx(record_id="b" * 64, item_id="item-B", arrival_seq=5)
    plan = {"version": "rrf_v1", "required": {history["record_id"]: history}}
    out = aggregate_module.aggregate(
        current, plan, [_pair(history, current)],
        coverage=aggregate_module.CoverageStatus(
            visible_seq=2, prepared_seq=2, complete=False),
    )
    assert out.decision == "重复"
    assert out.duplicate_ids == ("item-A",)