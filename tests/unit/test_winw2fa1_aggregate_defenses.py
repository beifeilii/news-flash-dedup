"""W2Fα1 条目67（WB3 发现#3）：aggregate 三处防御缺席补齐（直调红测）。

三防御（当前编排不可达，防御纵深；三腿预期零翻转）：
1. 计划内（frozen_plan.required）成员无对级结果 → FACT_INCOMPLETE 记账循环
   （漏判不可静默判"不重复"）；
2. pair.outcome=="conflict" 必须携 CONFLICT_CODES 内码（VERIFIED_CONFLICT），
   与 equivalent/unresolved 两分支既有白名单闸同纪律；
3. arrival_seq 全局唯一断言：不同 history 记录共享同一 arrival_seq = 到达序
   损坏 → AggregateError（同一记录的重复对行=既有去重语义，不受影响）。
"""

from __future__ import annotations

import pytest

from news_flash_dedup.compare import aggregate as aggregate_module
from news_flash_dedup.compare.aggregate import AggregateError
from news_flash_dedup.compare.pair_compare import PairResult


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
        outcome=outcome, code=code, detail=detail,
        aligned_facts=(), verified_conflicts=(), unresolved_fields=(),
        used_evidence=(), budget_at=0)


def _coverage(complete=True):
    return aggregate_module.CoverageStatus(visible_seq=10, prepared_seq=10,
                                           complete=complete)


# ---------- 防御 1：计划内无 pair → FACT_INCOMPLETE 记账 ----------

def test_uncovered_required_member_books_fact_incomplete():
    """required 两成员只来 1 对（conflict）——修复前判"不重复"（漏判静默）；
    修复后 uncovered 成员记 FACT_INCOMPLETE → 边界。"""
    h1 = _ctx(record_id="a" * 64, item_id="item-A", arrival_seq=1)
    h2 = _ctx(record_id="b" * 64, item_id="item-B", arrival_seq=2)
    current = _ctx(record_id="c" * 64, item_id="item-C", arrival_seq=3)
    plan = {"version": "rrf_v1", "required": {
        h1["record_id"]: h1, h2["record_id"]: h2}}
    out = aggregate_module.aggregate(
        current, plan,
        [_pair(h1, current, outcome="conflict", code="VERIFIED_CONFLICT",
               detail="差异")],
        coverage=_coverage(True))
    assert out.decision == "边界case/疑难case"
    assert out.internal_code == "FACT_INCOMPLETE"


def test_uncovered_required_member_with_unresolved_pair_keeps_boundary():
    """uncovered 记账与既有未决 issue 并存时仍落边界（优先序执法）。"""
    h1 = _ctx(record_id="a" * 64, item_id="item-A", arrival_seq=1)
    h2 = _ctx(record_id="b" * 64, item_id="item-B", arrival_seq=2)
    current = _ctx(record_id="c" * 64, item_id="item-C", arrival_seq=3)
    plan = {"version": "rrf_v1", "required": {
        h1["record_id"]: h1, h2["record_id"]: h2}}
    out = aggregate_module.aggregate(
        current, plan,
        [_pair(h1, current, outcome="unresolved", code="RULE_UNCOVERED",
               detail="未覆盖")],
        coverage=_coverage(True))
    assert out.decision == "边界case/疑难case"
    # W-R3b（R3-M7-b）：集合断言不咬优先序——改优先序等值钉：
    # _BOUNDARY_PRIORITY 内 RULE_UNCOVERED（rank 8）先于 FACT_INCOMPLETE
    # （rank 9），双 issue 并存时主码必须落 RULE_UNCOVERED。
    assert out.internal_code == "RULE_UNCOVERED"


# ---------- 防御 2：conflict 必须携 CONFLICT_CODES 内码 ----------

def test_conflict_outcome_requires_conflict_code():
    """outcome=conflict 但 code∉CONFLICT_CODES → AggregateError（修复前静默入列）。"""
    h1 = _ctx(record_id="a" * 64, item_id="item-A", arrival_seq=1)
    current = _ctx(record_id="c" * 64, item_id="item-C", arrival_seq=3)
    plan = {"version": "rrf_v1", "required": {h1["record_id"]: h1}}
    with pytest.raises(AggregateError):
        aggregate_module.aggregate(
            current, plan,
            [_pair(h1, current, outcome="conflict", code="FACT_EQUIVALENT",
                   detail="错码")],
            coverage=_coverage(True))


# ---------- 防御 3：arrival_seq 全局唯一（跨记录） ----------

def test_distinct_records_sharing_arrival_seq_rejected():
    """两个不同 history 记录携同一 arrival_seq → AggregateError（修复前放行）。"""
    h1 = _ctx(record_id="a" * 64, item_id="item-A", arrival_seq=1)
    h2 = _ctx(record_id="b" * 64, item_id="item-B", arrival_seq=1)   # 同 seq 异记录
    current = _ctx(record_id="c" * 64, item_id="item-C", arrival_seq=3)
    plan = {"version": "rrf_v1", "required": {
        h1["record_id"]: h1, h2["record_id"]: h2}}
    with pytest.raises(AggregateError):
        aggregate_module.aggregate(
            current, plan,
            [_pair(h1, current, outcome="conflict", code="VERIFIED_CONFLICT",
                   detail="差异1"),
             _pair(h2, current, outcome="conflict", code="VERIFIED_CONFLICT",
                   detail="差异2")],
            coverage=_coverage(True))


# ---------- 前后均绿守卫（现役语义零漂移） ----------

def test_same_record_duplicate_pair_rows_still_deduped():
    """同一 history 记录的重复对行=既有去重语义（防御 3 不误伤）。"""
    h1 = _ctx(record_id="a" * 64, item_id="item-A", arrival_seq=1)
    current = _ctx(record_id="c" * 64, item_id="item-C", arrival_seq=3)
    plan = {"version": "rrf_v1", "required": {h1["record_id"]: h1}}
    out = aggregate_module.aggregate(
        current, plan,
        [_pair(h1, current), _pair(h1, current)],
        coverage=_coverage(True))
    assert out.decision == "重复"
    assert out.duplicate_ids == ("item-A",)


def test_verified_conflict_pair_still_not_duplicate():
    """合法 conflict 对（VERIFIED_CONFLICT）→ 不重复（防御 2 不误伤）。"""
    h1 = _ctx(record_id="a" * 64, item_id="item-A", arrival_seq=1)
    current = _ctx(record_id="c" * 64, item_id="item-C", arrival_seq=3)
    plan = {"version": "rrf_v1", "required": {h1["record_id"]: h1}}
    out = aggregate_module.aggregate(
        current, plan,
        [_pair(h1, current, outcome="conflict", code="VERIFIED_CONFLICT",
               detail="差异")],
        coverage=_coverage(True))
    assert out.decision == "不重复"
    assert out.internal_code == "VERIFIED_CONFLICT"


def test_fully_covered_plan_no_new_issue():
    """计划全覆盖 + 覆盖完整 + 无 issue → 不重复 NO_DUPLICATE_FOUND（防御 1 不误伤）。"""
    h1 = _ctx(record_id="a" * 64, item_id="item-A", arrival_seq=1)
    current = _ctx(record_id="c" * 64, item_id="item-C", arrival_seq=3)
    plan = {"version": "rrf_v1", "required": {h1["record_id"]: h1}}
    out = aggregate_module.aggregate(
        current, plan,
        [_pair(h1, current, outcome="conflict", code="VERIFIED_CONFLICT",
               detail="差异")],
        coverage=_coverage(True))
    assert out.decision == "不重复"
