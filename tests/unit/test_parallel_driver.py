"""并行编排核钉（recall/parallel_driver.py，B+ 第三步组装件）：

- 直用道：差集空 ∧ 覆盖未变 → 草稿复用（零补算零重聚合）；
- 重算道：缺对补算（缓存优先）/掉出退出/存活保留 → aggregate 重聚合
  （coverage 取新鲜值）；
- 缓存时不变性：同一缺对二次定稿命中缓存（补算口零调用）；
- 覆盖口径：recall_gaps 在场 → False（缺口语义不新增）。
"""

from __future__ import annotations

from types import SimpleNamespace

from news_flash_dedup.decide.pair_cache import PairResultCache
from news_flash_dedup.recall.fusion import FusedCandidate, RecallPlan
from news_flash_dedup.recall.models import RecallCandidate
from news_flash_dedup.recall.parallel_driver import (
    PrecheckPacket,
    finalize_decision,
)


def _cand(rid: str, seq: int) -> RecallCandidate:
    return RecallCandidate(
        record_id=rid, item_id=f"it-{rid}", arrival_seq=seq,
        text=f"text-{rid}", subpaths=(), score=1.0, query_version="v1")


def _fused(rid: str, seq: int) -> FusedCandidate:
    return FusedCandidate(
        candidate=_cand(rid, seq), channel_hits=(), rrf_score=0,
        hash_protected=False, selected_for_pair_check=True,
        incremental_required=False)


def _plan(ids, *, gaps=(), coverage=True):
    audits = (SimpleNamespace(coverage_complete=coverage),) if not gaps else ()
    return RecallPlan(
        version="v1", merged_top30=(), pair_top10=(), hash_protected=(),
        incremental_required=(),
        required=tuple(_fused(rid, seq) for rid, seq in ids),
        recall_gaps=tuple(gaps), planning_excluded=0,
        channel_audits=audits)


def _pair(rid: str) -> SimpleNamespace:
    return SimpleNamespace(history_record_id=rid, verdict=f"pair-{rid}")


def _packet(ids=("a",), *, coverage=True) -> PrecheckPacket:
    plan = _plan(tuple((rid, i + 1) for i, rid in enumerate(ids)))
    decide = SimpleNamespace(
        decision="草稿", pair_results=tuple(_pair(rid) for rid in ids))
    return PrecheckPacket(
        plan=plan, request=None,
        current={"record_id": "cur", "raw_hash": "cur-hash"},
        candidates=tuple(ids), coverage=coverage, decide=decide)


_CHAIN = ("f1", "d1", "n1", "p1")


def _aggregate_recorder(out):
    def aggregate(current, frozen_plan, pair_results, *, coverage_complete):
        out["pairs"] = sorted(p.history_record_id for p in pair_results)
        out["coverage"] = coverage_complete
        return SimpleNamespace(decision="重聚合", pair_results=pair_results)
    return aggregate


def test_identical_reuses_draft_without_recompute():
    packet = _packet(("a", "b"))
    fresh = _plan((("a", 1), ("b", 2)))
    called = []
    decision = finalize_decision(
        packet, commit_request=None, fresh_plan=fresh,
        decide_pair=lambda h, c: called.append(h),
        aggregate=_aggregate_recorder({}), pair_cache=PairResultCache(),
        version_chain=_CHAIN, candidate_lookup=lambda rid: {})
    assert decision.recomputed is False
    assert decision.decide is packet.decide     # 草稿直用
    assert called == []                          # 零补算


def test_coverage_change_triggers_reaggregate_with_fresh_coverage():
    packet = _packet(("a",), coverage=True)
    fresh = _plan((("a", 1),), gaps=("gap",))   # 新鲜覆盖降级
    out = {}
    decision = finalize_decision(
        packet, commit_request=None, fresh_plan=fresh,
        decide_pair=lambda h, c: None,
        aggregate=_aggregate_recorder(out), pair_cache=PairResultCache(),
        version_chain=_CHAIN, candidate_lookup=lambda rid: {})
    assert decision.recomputed is True
    assert out == {"pairs": ["a"], "coverage": False}


def test_missing_computed_dropped_excluded_survivors_kept():
    packet = _packet(("a", "b"))                 # 预检 a,b
    fresh = _plan((("a", 1), ("c", 3)))          # 提交时刻：b 掉出 c 新入
    out = {}
    computed = []

    def decide_pair(history, current):
        computed.append(history["record_id"])
        return _pair(history["record_id"])

    lookup = {"c": {"record_id": "c", "raw_hash": "c-hash"}}
    decision = finalize_decision(
        packet, commit_request=None, fresh_plan=fresh,
        decide_pair=decide_pair,
        aggregate=_aggregate_recorder(out), pair_cache=PairResultCache(),
        version_chain=_CHAIN, candidate_lookup=lookup.__getitem__)
    assert decision.diff.missing == ("c",) and decision.diff.dropped == ("b",)
    assert computed == ["c"]                     # 只补缺对
    assert out["pairs"] == ["a", "c"]            # b 退出、a 存活、c 新算


def test_missing_pair_second_finalize_hits_cache():
    packet = _packet(("a",))
    fresh = _plan((("a", 1), ("c", 3)))
    cache = PairResultCache()
    computed = []

    def decide_pair(history, current):
        computed.append(1)
        return _pair(history["record_id"])

    lookup = {"c": {"record_id": "c", "raw_hash": "c-hash"}}
    for _ in range(2):
        finalize_decision(
            packet, commit_request=None, fresh_plan=fresh,
            decide_pair=decide_pair, aggregate=_aggregate_recorder({}),
            pair_cache=cache, version_chain=_CHAIN,
            candidate_lookup=lookup.__getitem__)
    assert len(computed) == 1                    # 第二次命中缓存零补算
    assert cache.stats["hits"] == 1
