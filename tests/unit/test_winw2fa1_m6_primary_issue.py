"""W2Fα1 条目55（WB2-M6 / WA1a-D6 同案）：aggregate 分支 4 主码对齐 _primary_issue。

aggregate.py 分支 4（not coverage.complete）原取 issues[0]，与分支 5 的
_primary_issue（_BOUNDARY_PRIORITY 优先序）两制不对码——reason=冻结五字段
之一，探针先行量化金标触发面：回放 coverage.complete 恒 True 分支 4 零触发
（log\temp\winw2fa1-probe-m6-branch4.json：calls=395 incomplete=0 branch4=0），
惰性对齐零翻转安全。
"""

from __future__ import annotations

from news_flash_dedup.compare import aggregate as aggregate_module
from news_flash_dedup.compare.pair_compare import PairResult


def _ctx(*, record_id, item_id, text="t", raw_hash="h", scope_id="default",
         business_date="2026-09-26", arrival_seq=1, pipeline_version="dedup_v1"):
    return {"record_id": record_id, "item_id": item_id, "text": text,
            "raw_hash": raw_hash, "scope_id": scope_id,
            "business_date": business_date, "arrival_seq": arrival_seq,
            "pipeline_version": pipeline_version}


def _pair(history, current, *, outcome="unresolved", code="FACT_INCOMPLETE",
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


def _coverage(complete):
    return aggregate_module.CoverageStatus(visible_seq=10, prepared_seq=10,
                                           complete=complete)


# ---------- 红测（修复前分支 4 取 issues[0]） ----------

def test_m6_branch4_uses_primary_issue_priority():
    """覆盖不完整且 issues=[FACT_INCOMPLETE, NUMERIC_ALIGNMENT_FAILED]：
    修复后按 _BOUNDARY_PRIORITY 取 NUMERIC_ALIGNMENT_FAILED（rank 6<9），
    修复前取 issues[0]=FACT_INCOMPLETE（两制不对码）。"""
    h1 = _ctx(record_id="a" * 64, item_id="item-A", arrival_seq=1)
    h2 = _ctx(record_id="b" * 64, item_id="item-B", arrival_seq=2)
    current = _ctx(record_id="c" * 64, item_id="item-C", arrival_seq=3)
    plan = {"version": "rrf_v1", "required": {
        h1["record_id"]: h1, h2["record_id"]: h2}}
    out = aggregate_module.aggregate(
        current, plan,
        [_pair(h1, current, code="FACT_INCOMPLETE", detail="先到的低优先码"),
         _pair(h2, current, code="NUMERIC_ALIGNMENT_FAILED",
               detail="后到的优先码")],
        coverage=_coverage(False))
    assert out.decision == "边界case/疑难case"
    assert out.internal_code == "NUMERIC_ALIGNMENT_FAILED"
    assert out.reason == "后到的优先码"


def test_m6_branch4_single_issue_unchanged():
    """前后均绿守卫：单 issue 时 issues[0]≡_primary_issue，行为不变。"""
    h1 = _ctx(record_id="a" * 64, item_id="item-A", arrival_seq=1)
    current = _ctx(record_id="c" * 64, item_id="item-C", arrival_seq=3)
    plan = {"version": "rrf_v1", "required": {h1["record_id"]: h1}}
    out = aggregate_module.aggregate(
        current, plan,
        [_pair(h1, current, code="RULE_UNCOVERED", detail="未覆盖")],
        coverage=_coverage(False))
    assert out.decision == "边界case/疑难case"
    assert out.internal_code == "RULE_UNCOVERED"


def test_m6_branch4_empty_issues_recall_incomplete_unchanged():
    """前后均绿守卫：零 issue + 覆盖不完整 → RECALL_INCOMPLETE 兜底不变。"""
    h1 = _ctx(record_id="a" * 64, item_id="item-A", arrival_seq=1)
    current = _ctx(record_id="c" * 64, item_id="item-C", arrival_seq=3)
    plan = {"version": "rrf_v1", "required": {h1["record_id"]: h1}}
    out = aggregate_module.aggregate(
        current, plan,
        [_pair(h1, current, outcome="conflict", code="VERIFIED_CONFLICT",
               detail="差异")],
        coverage=_coverage(False))
    assert out.decision == "边界case/疑难case"
    assert out.internal_code == "RECALL_INCOMPLETE"


def test_m6_branch5_priority_already_aligned_unchanged():
    """前后均绿守卫：分支 5（覆盖完整+issues）现役 _primary_issue 不动。"""
    h1 = _ctx(record_id="a" * 64, item_id="item-A", arrival_seq=1)
    h2 = _ctx(record_id="b" * 64, item_id="item-B", arrival_seq=2)
    current = _ctx(record_id="c" * 64, item_id="item-C", arrival_seq=3)
    plan = {"version": "rrf_v1", "required": {
        h1["record_id"]: h1, h2["record_id"]: h2}}
    out = aggregate_module.aggregate(
        current, plan,
        [_pair(h1, current, code="FACT_INCOMPLETE", detail="先到的低优先码"),
         _pair(h2, current, code="NUMERIC_ALIGNMENT_FAILED",
               detail="后到的优先码")],
        coverage=_coverage(True))
    assert out.decision == "边界case/疑难case"
    assert out.internal_code == "NUMERIC_ALIGNMENT_FAILED"
