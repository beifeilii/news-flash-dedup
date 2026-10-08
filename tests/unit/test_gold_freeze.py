"""全部人物和文本均为合成测试夹具，不代表历史材料的人工标注。"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from hashlib import sha256

import pytest

from news_flash_dedup.gold import assign_splits, build_families
from news_flash_dedup.gold.materials import MaterialCatalog, MaterialFile, MaterialRow


def _catalog():
    texts = (
        "fixture-only-body-alpha-871",
        "fixture-only-body-beta-562",
        "fixture-only-body-gamma-394",
        "fixture-only-body-delta-028",
    )
    rows = tuple(
        MaterialRow(
            row_key=f"fixture:重复:{i + 2}:single",
            material_id="fixture",
            sheet_name="重复",
            excel_row=i + 2,
            side="single",
            raw_id=str(i + 1),
            raw_text=text,
            raw_hash=sha256(text.encode()).hexdigest(),
            legacy_group="1" if i < 3 else "2",
            legacy_label="重复",
            legacy_note=None,
            hidden=False,
        )
        for i, text in enumerate(texts)
    )
    return MaterialCatalog(
        materials=(MaterialFile("fixture", 4, ("fixture.xlsx",), (("重复", 4),)),),
        rows=rows,
        negative_pairs=(),
    )


def _review(a, b, label):
    from news_flash_dedup.gold.freeze import (
        Adjudication, EvidenceSpan, IndependentAnnotation, PairReview,
    )

    evidence_a = (EvidenceSpan(0, 1, a.raw_text[0], "fact"),)
    evidence_b = (EvidenceSpan(0, 1, b.raw_text[0], "fact"),)
    annotations = tuple(
        IndependentAnnotation(
            annotator_id=f"fixture-ann-{slot}",
            label=label,
            reason="fixture only",
            evidence_a=evidence_a,
            evidence_b=evidence_b,
            rule_version="fixture-r1",
            status="submitted",
            submitted_at=datetime(2026, 9, 22, 15, slot, tzinfo=timezone.utc),
        )
        for slot in (1, 2)
    )
    adjudication = Adjudication(
        reviewer_alg="fixture-alg",
        reviewer_data="fixture-data",
        gold_label=label,
        gold_reason="fixture only",
        evidence_a=evidence_a,
        evidence_b=evidence_b,
        adjudication_version="fixture-a1",
        status="final",
        adjudicated_at=datetime(2026, 9, 22, 16, tzinfo=timezone.utc),
    )
    return PairReview(a.row_key, b.row_key, annotations, adjudication)


def _inputs(*, include_d=False):
    from news_flash_dedup.gold.freeze import QueryAssignment, ReplayItem

    catalog = _catalog()
    a, b, c, d = catalog.rows
    graph = build_families(catalog)
    splits = assign_splits(graph, seed="fixture-split")
    ordered = (a, b, d, c) if include_d else (a, b, c)
    replay = tuple(
        ReplayItem(
            row_key=row.row_key,
            scope_id="scope-x",
            business_date="2026-09-22",
            arrival_seq=i + 1,
            order_provenance="synthetic",
            synthetic_plan_id="fixture-order-v1",
        )
        for i, row in enumerate(ordered)
    )
    assignments = tuple(
        QueryAssignment(row.row_key, "M", "fixture clear", not include_d or row != c)
        for row in (a, b, c)
    )
    reviews = (_review(a, b, "重复"), _review(b, c, "重复"), _review(a, c, "不重复"))
    return catalog, graph, splits, replay, assignments, reviews


def _freeze(catalog, graph, splits, replay, assignments, reviews, *, exclusions=()):
    from news_flash_dedup.gold.freeze import freeze_dataset

    return freeze_dataset(
        catalog=catalog,
        graph=graph,
        splits=splits,
        split_seed="fixture-split",
        reviews=reviews,
        replay=replay,
        assignments=assignments,
        exclusions=exclusions,
        rule_version="fixture-r1",
        frozen_at=datetime(2026, 9, 23, tzinfo=timezone.utc),
        first_run_at=datetime(2026, 9, 24, tzinfo=timezone.utc),
    )


def test_freeze_keeps_direct_nontransitive_labels_and_fixed_m_denominators():
    from news_flash_dedup.gold.freeze import inspect_members

    catalog, graph, splits, replay, assignments, reviews = _inputs()
    frozen = _freeze(catalog, graph, splits, replay, assignments, reviews)
    a, b, c, _ = catalog.rows

    assert frozen.dataset_version.startswith("ds1-")
    assert frozen.manifest["counts"]["M"] == 3
    assert frozen.manifest["counts"]["H"] == 0
    assert frozen.manifest["counts"]["q_plus"] == 2
    assert frozen.manifest["queries"][c.row_key]["g_i"] == [b.row_key]
    assert frozen.manifest["queries"][c.row_key]["h_i"] == [a.row_key, b.row_key]
    mutable_view = frozen.manifest
    mutable_view["queries"][c.row_key]["g_i"].clear()
    assert frozen.manifest["queries"][c.row_key]["g_i"] == [b.row_key]
    audit = inspect_members(frozen, c.row_key, (b.row_key, a.row_key), decision="重复")
    assert audit.correct == (b.row_key,)
    assert audit.known_wrong == (a.row_key,)
    assert not audit.member_label_pass
    boundary = inspect_members(frozen, c.row_key, (), decision="边界case/疑难case")
    assert boundary.query_set == "M" and boundary.m_positive_member_gate_failed
    assert frozen.manifest["counts"]["q_plus"] == 2
    assert all(row.raw_text not in frozen.manifest_json for row in catalog.rows)
    assert "raw_text" not in str(frozen.manifest)


def test_unknown_returned_member_stays_unknown_and_does_not_become_negative():
    from news_flash_dedup.gold.freeze import Exclusion, inspect_members

    catalog, graph, splits, replay, assignments, reviews = _inputs(include_d=True)
    d = catalog.rows[3]
    c = catalog.rows[2]
    frozen = _freeze(
        catalog, graph, splits, replay, assignments, reviews,
        exclusions=(Exclusion(d.row_key, "fixture query excluded before run"),),
    )
    audit = inspect_members(frozen, c.row_key, (d.row_key,), decision="重复")
    assert audit.unknown == (d.row_key,)
    assert audit.known_wrong == ()
    assert not audit.member_label_pass
    assert frozen.manifest["queries"][c.row_key]["unknown_prior"] == [d.row_key]


def test_strict_list_may_return_subset_of_two_direct_gold_members():
    from news_flash_dedup.gold.freeze import inspect_members

    catalog, graph, splits, replay, assignments, reviews = _inputs()
    a, b, c, _ = catalog.rows
    two_positive = reviews[:2] + (_review(a, c, "重复"),)
    partial_coverage = tuple(
        replace(item, full_coverage=False) if item.row_key == c.row_key else item
        for item in assignments
    )
    frozen = _freeze(catalog, graph, splits, replay, partial_coverage, two_positive)
    assert frozen.manifest["queries"][c.row_key]["g_i"] == [a.row_key, b.row_key]
    audit = inspect_members(frozen, c.row_key, (b.row_key,), decision="重复")
    assert audit.member_label_pass
    assert not audit.m_positive_member_gate_failed


def test_freeze_rejects_unresolved_reviews_and_uncovered_clear_negative_query():
    catalog, graph, splits, replay, assignments, reviews = _inputs()
    unresolved = replace(reviews[0], adjudication=replace(reviews[0].adjudication, status="pending"))
    with pytest.raises(ValueError, match="final adjudication"):
        _freeze(catalog, graph, splits, replay, assignments, (unresolved,) + reviews[1:])

    incomplete = tuple(
        replace(item, full_coverage=False) if item.row_key == catalog.rows[0].row_key else item
        for item in assignments
    )
    with pytest.raises(ValueError, match="clear negative query"):
        _freeze(catalog, graph, splits, replay, incomplete, reviews)


def test_freeze_rejects_cross_family_pair_and_fake_real_time():
    catalog, graph, splits, replay, assignments, reviews = _inputs()
    a, _, _, d = catalog.rows
    with pytest.raises(ValueError, match="family"):
        _freeze(catalog, graph, splits, replay, assignments, reviews + (_review(a, d, "不重复"),))

    claimed_real = tuple(replace(item, order_provenance="real", synthetic_plan_id=None) for item in replay)
    with pytest.raises(ValueError, match="source_timestamp"):
        _freeze(catalog, graph, splits, claimed_real, assignments, reviews)


def test_freeze_rejects_empty_review_set_and_bad_original_evidence():
    catalog, graph, splits, replay, assignments, reviews = _inputs()
    with pytest.raises(ValueError, match="reviewed pair"):
        _freeze(catalog, graph, splits, replay, assignments, ())
    invalid_span = replace(reviews[0].annotations[0].evidence_a[0], quote="not original")
    invalid_annotation = replace(reviews[0].annotations[0], evidence_a=(invalid_span,))
    invalid_review = replace(reviews[0], annotations=(invalid_annotation, reviews[0].annotations[1]))
    with pytest.raises(ValueError, match="original body"):
        _freeze(catalog, graph, splits, replay, assignments, (invalid_review,) + reviews[1:])


def test_freeze_requires_two_reviewers_and_adjudication_before_freeze():
    catalog, graph, splits, replay, assignments, reviews = _inputs()
    same_reviewer = replace(reviews[0].adjudication, reviewer_data="fixture-alg")
    with pytest.raises(ValueError, match="distinct reviewers"):
        _freeze(
            catalog, graph, splits, replay, assignments,
            (replace(reviews[0], adjudication=same_reviewer),) + reviews[1:],
        )
    late = replace(reviews[0].adjudication, adjudicated_at=datetime(2026, 9, 24, tzinfo=timezone.utc))
    with pytest.raises(ValueError, match="after freeze"):
        _freeze(
            catalog, graph, splits, replay, assignments,
            (replace(reviews[0], adjudication=late),) + reviews[1:],
        )


def test_original_evidence_offsets_count_unicode_codepoints():
    catalog, _, _, replay, assignments, _ = _inputs()
    a, b, c, d = catalog.rows
    changed_text = "😀" + a.raw_text
    a = replace(a, raw_text=changed_text, raw_hash=sha256(changed_text.encode()).hexdigest())
    catalog = replace(catalog, rows=(a, b, c, d))
    graph = build_families(catalog)
    splits = assign_splits(graph, seed="fixture-split")
    reviews = (_review(a, b, "重复"), _review(b, c, "重复"), _review(a, c, "不重复"))
    frozen = _freeze(catalog, graph, splits, replay, assignments, reviews)
    assert frozen.manifest["counts"]["pairs"] == 3


def test_real_replay_timestamp_must_follow_arrival_sequence():
    catalog, graph, splits, replay, assignments, reviews = _inputs()
    real = tuple(
        replace(
            item,
            order_provenance="real",
            synthetic_plan_id=None,
            source_timestamp=datetime(2026, 9, 22, 12, 4 - i, tzinfo=timezone.utc),
        )
        for i, item in enumerate(replay)
    )
    with pytest.raises(ValueError, match="timestamp order"):
        _freeze(catalog, graph, splits, real, assignments, reviews)


def test_real_replay_cannot_use_arrival_after_freeze_and_cross_day_has_no_prior():
    catalog, graph, splits, replay, assignments, reviews = _inputs()
    late = tuple(
        replace(
            item,
            order_provenance="real",
            synthetic_plan_id=None,
            source_timestamp=datetime(2026, 9, 24, 12, i, tzinfo=timezone.utc),
        )
        for i, item in enumerate(replay)
    )
    with pytest.raises(ValueError, match="after freeze"):
        _freeze(catalog, graph, splits, late, assignments, reviews)

    a, b, c, _ = catalog.rows
    across_days = (
        replay[0],
        replace(replay[1], business_date="2026-09-23", arrival_seq=1),
        replace(replay[2], business_date="2026-09-23", arrival_seq=2),
    )
    frozen = _freeze(catalog, graph, splits, across_days, assignments, reviews)
    assert frozen.manifest["queries"][b.row_key]["h_i"] == []
    assert frozen.manifest["queries"][c.row_key]["h_i"] == [b.row_key]
    assert frozen.manifest["queries"][c.row_key]["g_i"] == [b.row_key]
    assert frozen.manifest["counts"]["q_plus"] == 1


def test_freeze_version_changes_for_label_or_order_and_pending_queue_has_no_gold():
    from news_flash_dedup.gold.freeze import pending_pair_queue

    catalog, graph, splits, replay, assignments, reviews = _inputs()
    original = _freeze(catalog, graph, splits, replay, assignments, reviews)
    changed_label = _review(catalog.rows[0], catalog.rows[2], "边界case/疑难case")
    changed_assignments = tuple(
        replace(item, full_coverage=False) if item.row_key == catalog.rows[2].row_key else item
        for item in assignments
    )
    changed = _freeze(
        catalog, graph, splits, replay, changed_assignments, reviews[:2] + (changed_label,),
    )
    assert original.dataset_version != changed.dataset_version
    swapped = (replay[0], replace(replay[2], arrival_seq=2), replace(replay[1], arrival_seq=3))
    reordered = _freeze(catalog, graph, splits, swapped, assignments, reviews)
    assert reordered.dataset_version != original.dataset_version
    queue = pending_pair_queue(catalog, ((catalog.rows[0].row_key, catalog.rows[1].row_key),))
    assert len(queue) == 1
    assert queue[0]["status"] == "pending"
    assert "gold_label" not in str(queue) and "raw_text" not in str(queue)


def test_pair_id_is_stable_under_reversed_pair_direction():
    from news_flash_dedup.gold.freeze import pending_pair_queue

    catalog, graph, splits, replay, assignments, reviews = _inputs()
    a, b, _, _ = catalog.rows
    forward_queue = pending_pair_queue(catalog, ((a.row_key, b.row_key),))
    reverse_queue = pending_pair_queue(catalog, ((b.row_key, a.row_key),))
    assert forward_queue[0]["pair_id"] == reverse_queue[0]["pair_id"]
    forward = _freeze(catalog, graph, splits, replay, assignments, reviews)
    reverse_review = _review(b, a, "重复")
    reverse = _freeze(catalog, graph, splits, replay, assignments, (reverse_review,) + reviews[1:])
    assert forward.manifest["pairs"][0]["pair_id"] == reverse.manifest["pairs"][0]["pair_id"]
