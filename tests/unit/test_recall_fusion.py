"""P13 五路 RRF、Hash 保护及固定比较计划。"""

from dataclasses import replace
from fractions import Fraction

import pytest

from news_flash_dedup.recall.fusion import freeze_recall_plan, plan_completion
from news_flash_dedup.recall.models import ChannelResult, RecallCandidate, RecallRequest


REQUEST = RecallRequest("default", "2026-09-26", "z" * 64,
                        "current", 100, "甲公司完成回购",
                        visible_seq=99, prepared_seq=99)
CHANNELS = ("hash", "near", "bm25", "embedding", "entity")


def candidate(number, channel="bm25", *, text="甲公司完成回购"):
    return RecallCandidate(f"{number:064x}", f"item-{number}", number,
                           text, (channel,), 1.0, channel + "_v1")


def response(channel, candidates=(), *, status="complete", coverage=True,
             error_code=None, visible_seq=99, prepared_seq=99):
    return ChannelResult(channel, status, tuple(candidates), channel + "_v1",
                         visible_seq=visible_seq, prepared_seq=prepared_seq,
                         coverage_complete=coverage, error_code=error_code)


def five(**overrides):
    return tuple(overrides.get(channel, response(channel)) for channel in CHANNELS)


def test_rrf_uses_main_channels_once_and_stable_rank_not_raw_scores():
    first = candidate(1)
    second = candidate(2)
    near_first = replace(first, subpaths=("simhash", "minhash"),
                         score=0.1, query_version="near_v1")
    near_second = replace(second, subpaths=("simhash",),
                          score=0.99, query_version="near_v1")
    plan = freeze_recall_plan(REQUEST, five(
        near=response("near", [near_second, near_first]),
        bm25=response("bm25", [first, second]),
    ))
    assert [item.candidate.record_id for item in plan.merged_top30] == [
        first.record_id, second.record_id]
    assert plan.merged_top30[0].rrf_score == Fraction(1, 62) + Fraction(1, 61)
    assert len(plan.merged_top30[0].channel_hits) == 2
    assert {hit.channel for hit in plan.merged_top30[0].channel_hits} == {
        "near", "bm25"}


def test_hash_outside_regular_30_and_10_is_still_required():
    shared = [candidate(number) for number in range(1, 36)]
    hash_only = candidate(90, "hash")
    plan = freeze_recall_plan(REQUEST, five(
        near=response("near", [replace(item, subpaths=("simhash",),
                                       query_version="near_v1")
                               for item in shared]),
        bm25=response("bm25", shared),
        hash=response("hash", [hash_only]),
    ))
    assert len(plan.merged_top30) == 30
    assert len(plan.pair_top10) == 10
    assert hash_only.record_id not in {item.candidate.record_id
                                       for item in plan.merged_top30}
    assert [item.candidate.record_id for item in plan.hash_protected] == [
        hash_only.record_id]
    assert len(plan.required) == 11
    assert hash_only.record_id in {item.candidate.record_id for item in plan.required}
    assert plan.planning_excluded == 6
    report = plan_completion(plan, [item.candidate.record_id
                                    for item in plan.pair_top10])
    assert report.unfinished_record_ids == (hash_only.record_id,)
    assert report.budget_exhausted is True


def test_incomplete_channel_preserves_reliable_other_candidates_and_gap():
    reliable = candidate(1, "hash")
    plan = freeze_recall_plan(REQUEST, five(
        hash=response("hash", [reliable]),
        embedding=response("embedding", status="unavailable", coverage=False,
                           error_code="MILVUS_SEARCH_FAILED"),
    ))
    assert [item.candidate.record_id for item in plan.required] == [
        reliable.record_id]
    assert plan.recall_incomplete is True
    assert any(gap.channel == "embedding" for gap in plan.recall_gaps)
    assert plan_completion(plan, [reliable.record_id]).budget_exhausted is False


def test_complete_traversal_without_coverage_is_still_a_gap():
    plan = freeze_recall_plan(REQUEST, five(
        embedding=response("embedding", coverage=False)))
    assert plan.recall_incomplete is True
    assert [(gap.channel, gap.reason) for gap in plan.recall_gaps] == [
        ("embedding", "COVERAGE_UNPROVEN")]


def test_proven_entity_not_applicable_only_when_no_error():
    complete = freeze_recall_plan(REQUEST, five(
        entity=response("entity", status="not_applicable", coverage=False)))
    assert complete.recall_incomplete is False
    failed = freeze_recall_plan(REQUEST, five(
        entity=response("entity", status="not_applicable", coverage=False,
                        error_code="ENTITY_UNRESOLVED")))
    assert failed.recall_incomplete is True


def test_cross_channel_record_identity_mismatch_rejected():
    first = candidate(1)
    with pytest.raises(ValueError, match="identity"):
        freeze_recall_plan(REQUEST, five(
            bm25=response("bm25", [first]),
            near=response("near", [replace(first, text="另一正文",
                                           query_version="near_v1")]),
        ))


def test_registered_incremental_is_required_without_changing_regular_metrics():
    normal = candidate(1)
    extra = candidate(2, "incremental")
    plan = freeze_recall_plan(REQUEST, five(
        bm25=response("bm25", [normal])), incremental=[extra, normal])
    assert [item.candidate.record_id for item in plan.merged_top30] == [
        normal.record_id]
    assert [item.candidate.record_id for item in plan.required] == [
        normal.record_id, extra.record_id]
    assert {item.candidate.record_id for item in plan.incremental_required} == {
        normal.record_id, extra.record_id}
    assert plan_completion(plan, [normal.record_id]).unfinished_record_ids == (
        extra.record_id,)


def test_missing_channel_cannot_become_complete_empty():
    plan = freeze_recall_plan(REQUEST, [response("hash")])
    assert plan.recall_incomplete is True
    assert {gap.channel for gap in plan.recall_gaps} == set(CHANNELS) - {"hash"}


def test_claimed_coverage_without_current_visible_or_prepared_frontier_is_a_gap():
    plan = freeze_recall_plan(REQUEST, five(
        hash=response("hash", visible_seq=None),
        near=response("near", prepared_seq=None),
        bm25=response("bm25", visible_seq=1),
    ))
    assert {gap.channel for gap in plan.recall_gaps} >= {
        "hash", "near", "bm25"}


def test_channel_truncation_and_versions_remain_in_plan_audit():
    plan = freeze_recall_plan(REQUEST, five(
        near=replace(response("near", status="truncated", coverage=False),
                     pages=4, topk_excluded=7,
                     truncated_buckets=("simhash:0",)),
    ))
    near_audit = next(item for item in plan.channel_audits
                      if item.channel == "near")
    assert near_audit.status == "truncated"
    assert near_audit.pages == 4
    assert near_audit.topk_excluded == 7
    assert near_audit.truncated_buckets == ("simhash:0",)
    assert near_audit.query_version == "near_v1"
