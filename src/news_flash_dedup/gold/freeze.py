"""人工逐对结果的结构性冻结与合法前序校验；不从旧表生成金标。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from hashlib import sha256
from typing import Iterable

from .artifacts import build_manifest
from .families import FamilyGraph
from .materials import MaterialCatalog, MaterialRow


LABELS = frozenset({"重复", "不重复", "边界case/疑难case"})


@dataclass(frozen=True)
class EvidenceSpan:
    start: int
    end: int
    quote: str
    field_path: str


@dataclass(frozen=True)
class IndependentAnnotation:
    annotator_id: str
    label: str
    reason: str
    evidence_a: tuple[EvidenceSpan, ...]
    evidence_b: tuple[EvidenceSpan, ...]
    rule_version: str
    status: str
    submitted_at: datetime


@dataclass(frozen=True)
class Adjudication:
    reviewer_alg: str
    reviewer_data: str
    gold_label: str
    gold_reason: str
    evidence_a: tuple[EvidenceSpan, ...]
    evidence_b: tuple[EvidenceSpan, ...]
    adjudication_version: str
    status: str
    adjudicated_at: datetime


@dataclass(frozen=True)
class PairReview:
    a_row_key: str
    b_row_key: str
    annotations: tuple[IndependentAnnotation, ...]
    adjudication: Adjudication


@dataclass(frozen=True)
class ReplayItem:
    row_key: str
    scope_id: str
    business_date: str
    arrival_seq: int
    order_provenance: str
    source_timestamp: datetime | None = None
    synthetic_plan_id: str | None = None


@dataclass(frozen=True)
class QueryAssignment:
    row_key: str
    set_kind: str
    reason: str
    full_coverage: bool


@dataclass(frozen=True)
class Exclusion:
    row_key: str
    reason: str


@dataclass(frozen=True)
class FrozenDataset:
    dataset_version: str
    manifest_json: str

    @property
    def manifest(self) -> dict:
        """每次返回独立视图，调用方不能改写已冻结的内部快照。"""
        return json.loads(self.manifest_json)


@dataclass(frozen=True)
class MemberAudit:
    query_set: str
    correct: tuple[str, ...]
    known_wrong: tuple[str, ...]
    unknown: tuple[str, ...]
    illegal: tuple[str, ...]
    member_label_pass: bool
    m_positive_member_gate_failed: bool


def _digest(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _pair_key(a: str, b: str) -> tuple[str, str]:
    return tuple(sorted((a, b)))


def _pair_id(a: str, b: str) -> str:
    return _digest("\n".join(_pair_key(a, b)))


def _evidence_digest(spans: tuple[EvidenceSpan, ...], row: MaterialRow) -> str:
    if not spans:
        raise ValueError("both sides need original-text evidence")
    records = []
    for span in spans:
        if (
            not isinstance(span.start, int)
            or not isinstance(span.end, int)
            or span.start < 0
            or span.start >= span.end
            or span.end > len(row.raw_text)
            or row.raw_text[span.start:span.end] != span.quote
            or not span.field_path.strip()
        ):
            raise ValueError("evidence span must match original body")
        records.append({
            "start": span.start,
            "end": span.end,
            "quote_hash": _digest(span.quote),
            "field_path": span.field_path,
        })
    return _digest(_canonical(records))


def _review_record(
    review: PairReview,
    rows: dict[str, MaterialRow],
    graph: FamilyGraph,
    rule_version: str,
    frozen_at: datetime,
) -> dict:
    a = rows.get(review.a_row_key)
    b = rows.get(review.b_row_key)
    if a is None or b is None or a.row_key == b.row_key:
        raise ValueError("review must reference two distinct known material rows")
    if graph.family_by_row[a.row_key] != graph.family_by_row[b.row_key]:
        raise ValueError("review pair endpoints must be in one isolation family")
    if len(review.annotations) != 2:
        raise ValueError("review requires two independent annotations")
    annotators = [item.annotator_id for item in review.annotations]
    if any(not item.strip() for item in annotators) or len(set(annotators)) != 2:
        raise ValueError("review requires distinct annotator IDs")
    annotation_records = []
    submitted_times = []
    for item in review.annotations:
        if (
            item.status != "submitted"
            or item.label not in LABELS
            or item.rule_version != rule_version
            or not item.reason.strip()
        ):
            raise ValueError("annotation is unresolved or uses another rule version")
        if not _aware(item.submitted_at) or item.submitted_at > frozen_at:
            raise ValueError("annotation submitted after freeze or without timezone")
        submitted_times.append(item.submitted_at)
        annotation_records.append({
            "annotator_id": item.annotator_id,
            "label": item.label,
            "reason_sha256": _digest(item.reason),
            "evidence_a_sha256": _evidence_digest(item.evidence_a, a),
            "evidence_b_sha256": _evidence_digest(item.evidence_b, b),
            "submitted_at": item.submitted_at.isoformat(),
        })
    decision = review.adjudication
    if (
        decision.status != "final"
        or decision.gold_label not in LABELS
        or not decision.gold_reason.strip()
        or not decision.adjudication_version.strip()
        or not decision.reviewer_alg.strip()
        or not decision.reviewer_data.strip()
    ):
        raise ValueError("review requires final adjudication with reason and reviewers")
    if decision.reviewer_alg == decision.reviewer_data:
        raise ValueError("adjudication requires distinct reviewers")
    if (
        not _aware(decision.adjudicated_at)
        or decision.adjudicated_at > frozen_at
        or decision.adjudicated_at < max(submitted_times)
    ):
        raise ValueError("adjudication completed after freeze or before both annotations")
    return {
        "pair_id": _pair_id(a.row_key, b.row_key),
        "a_row_key": a.row_key,
        "b_row_key": b.row_key,
        "gold_label": decision.gold_label,
        "gold_reason_sha256": _digest(decision.gold_reason),
        "legacy_label_a": a.legacy_label,
        "legacy_label_b": b.legacy_label,
        "annotations": sorted(annotation_records, key=lambda item: item["annotator_id"]),
        "reviewer_alg": decision.reviewer_alg,
        "reviewer_data": decision.reviewer_data,
        "adjudication_version": decision.adjudication_version,
        "adjudicated_at": decision.adjudicated_at.isoformat(),
        "adjudication_evidence_a_sha256": _evidence_digest(decision.evidence_a, a),
        "adjudication_evidence_b_sha256": _evidence_digest(decision.evidence_b, b),
    }


def _aware(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() is not None


def _replay_record(item: ReplayItem, row_keys: set[str]) -> dict:
    if item.row_key not in row_keys or not item.scope_id.strip():
        raise ValueError("replay row or scope is unknown")
    try:
        parsed = date.fromisoformat(item.business_date)
    except ValueError as exc:
        raise ValueError("business_date must be ISO date") from exc
    if parsed.isoformat() != item.business_date:
        raise ValueError("business_date must be canonical ISO date")
    if not isinstance(item.arrival_seq, int) or item.arrival_seq < 1:
        raise ValueError("arrival_seq must be a positive integer")
    if item.order_provenance == "real":
        if item.source_timestamp is None or not _aware(item.source_timestamp) or item.synthetic_plan_id:
            raise ValueError("real order requires aware source_timestamp and no synthetic plan")
    elif item.order_provenance == "synthetic":
        if item.source_timestamp is not None or not item.synthetic_plan_id:
            raise ValueError("synthetic order requires explicit plan ID and no source_timestamp")
    else:
        raise ValueError("order_provenance must be real or synthetic")
    return {
        "row_key": item.row_key,
        "scope_id": item.scope_id,
        "business_date": item.business_date,
        "arrival_seq": item.arrival_seq,
        "order_provenance": item.order_provenance,
        "source_timestamp": item.source_timestamp.isoformat() if item.source_timestamp else None,
        "synthetic_plan_id": item.synthetic_plan_id,
    }


def freeze_dataset(
    *,
    catalog: MaterialCatalog,
    graph: FamilyGraph,
    splits: dict[str, str],
    split_seed: str,
    reviews: Iterable[PairReview],
    replay: Iterable[ReplayItem],
    assignments: Iterable[QueryAssignment],
    exclusions: Iterable[Exclusion],
    rule_version: str,
    frozen_at: datetime,
    first_run_at: datetime | None = None,
) -> FrozenDataset:
    """校验外部人工结果，冻结无正文的直接关系与前序集合。"""
    if not rule_version.strip() or not _aware(frozen_at):
        raise ValueError("rule_version and aware frozen_at are required")
    if first_run_at is not None and (not _aware(first_run_at) or first_run_at <= frozen_at):
        raise ValueError("freeze must precede first run")
    isolation = build_manifest(catalog, graph, splits, split_seed=split_seed)
    row_by_key = {row.row_key: row for row in catalog.rows}
    if len(row_by_key) != len(catalog.rows):
        raise ValueError("material row keys must be unique")

    pair_records: dict[tuple[str, str], dict] = {}
    for review in reviews:
        record = _review_record(review, row_by_key, graph, rule_version, frozen_at)
        key = _pair_key(review.a_row_key, review.b_row_key)
        if key in pair_records:
            raise ValueError("duplicate unordered review pair")
        pair_records[key] = record
    if not pair_records:
        raise ValueError("freeze requires at least one reviewed pair")

    replay_records = [_replay_record(item, set(row_by_key)) for item in replay]
    replay_by_row = {item["row_key"]: item for item in replay_records}
    if len(replay_by_row) != len(replay_records) or not replay_records:
        raise ValueError("replay requires distinct nonempty row keys")
    if len({(item["scope_id"], item["business_date"], item["arrival_seq"]) for item in replay_records}) != len(replay_records):
        raise ValueError("arrival_seq must be unique within scope and day")
    if len({item["order_provenance"] for item in replay_records}) != 1:
        raise ValueError("real and synthetic replay orders must be separate")
    replay_records.sort(key=lambda item: (item["scope_id"], item["business_date"], item["arrival_seq"]))
    if replay_records[0]["order_provenance"] == "real":
        last_time_by_scope_day: dict[tuple[str, str], datetime] = {}
        for item in replay_records:
            scope_day = (item["scope_id"], item["business_date"])
            stamp = datetime.fromisoformat(item["source_timestamp"])
            if stamp > frozen_at:
                raise ValueError("real source_timestamp cannot be after freeze")
            if scope_day in last_time_by_scope_day and stamp < last_time_by_scope_day[scope_day]:
                raise ValueError("real timestamp order conflicts with arrival_seq")
            last_time_by_scope_day[scope_day] = stamp

    assignments_by_row = {item.row_key: item for item in assignments}
    exclusions_by_row = {item.row_key: item for item in exclusions}
    if (
        len(assignments_by_row) + len(exclusions_by_row) != len(replay_records)
        or set(assignments_by_row) & set(exclusions_by_row)
        or set(assignments_by_row) | set(exclusions_by_row) != set(replay_by_row)
    ):
        raise ValueError("every replay row needs one M/H assignment or predeclared exclusion")
    if any(item.set_kind not in {"M", "H"} or not item.reason.strip() for item in assignments_by_row.values()):
        raise ValueError("query assignment needs M/H and a predeclared reason")
    if any(not item.reason.strip() for item in exclusions_by_row.values()):
        raise ValueError("exclusion needs a predeclared reason")

    queries: dict[str, dict] = {}
    for item in replay_records:
        assignment = assignments_by_row.get(item["row_key"])
        if assignment is None:
            continue
        prior = [
            earlier["row_key"] for earlier in replay_records
            if earlier["scope_id"] == item["scope_id"]
            and earlier["business_date"] == item["business_date"]
            and earlier["arrival_seq"] < item["arrival_seq"]
        ]
        positive = []
        unknown = []
        for prior_key in prior:
            decision = pair_records.get(_pair_key(item["row_key"], prior_key))
            if decision is None or decision["gold_label"] == "边界case/疑难case":
                unknown.append(prior_key)
            elif decision["gold_label"] == "重复":
                positive.append(prior_key)
        if assignment.full_coverage and unknown:
            raise ValueError("full coverage claim contains unknown or boundary relation")
        if assignment.set_kind == "M" and not positive and (not assignment.full_coverage or unknown):
            raise ValueError("clear negative query requires complete direct-pair coverage")
        queries[item["row_key"]] = {
            "set_kind": assignment.set_kind,
            "reason_sha256": _digest(assignment.reason),
            "full_coverage": assignment.full_coverage,
            "h_i": prior,
            "g_i": positive,
            "unknown_prior": unknown,
        }

    manifest = {
        "schema_version": "1",
        "isolation_manifest_sha256": _digest(_canonical(isolation)),
        "split_seed": split_seed,
        "rule_version": rule_version,
        "frozen_at": frozen_at.isoformat(),
        "first_run_at": first_run_at.isoformat() if first_run_at else None,
        "order_provenance": replay_records[0]["order_provenance"],
        "replay": replay_records,
        "pairs": sorted(pair_records.values(), key=lambda item: item["pair_id"]),
        "queries": dict(sorted(queries.items())),
        "exclusions": [
            {"row_key": key, "reason_sha256": _digest(item.reason)}
            for key, item in sorted(exclusions_by_row.items())
        ],
        "counts": {
            "M": sum(item["set_kind"] == "M" for item in queries.values()),
            "H": sum(item["set_kind"] == "H" for item in queries.values()),
            "q_plus": sum(item["set_kind"] == "M" and bool(item["g_i"]) for item in queries.values()),
            "exhaustive_M": sum(item["set_kind"] == "M" and item["full_coverage"] for item in queries.values()),
            "pairs": len(pair_records),
            "excluded_queries": len(exclusions_by_row),
        },
    }
    dataset_version = "ds1-" + _digest(_canonical(manifest))
    manifest["dataset_version"] = dataset_version
    return FrozenDataset(dataset_version, _canonical(manifest))


def inspect_members(
    frozen: FrozenDataset,
    query_row_key: str,
    returned_row_keys: Iterable[str],
    *,
    decision: str,
) -> MemberAudit:
    """仅核成员标签子集门；完整 S 还需五字段合同、顺序和逐成员证据。"""
    if decision not in LABELS | {"failed"}:
        raise ValueError("invalid prediction decision")
    manifest = frozen.manifest
    query = manifest["queries"].get(query_row_key)
    if query is None:
        raise ValueError("query was not frozen into M or H")
    labels = {
        _pair_key(item["a_row_key"], item["b_row_key"]): item["gold_label"]
        for item in manifest["pairs"]
    }
    prior = set(query["h_i"])
    correct: list[str] = []
    known_wrong: list[str] = []
    unknown: list[str] = []
    illegal: list[str] = []
    seen: set[str] = set()
    for key in returned_row_keys:
        if key in seen or key not in prior:
            illegal.append(key)
        else:
            label = labels.get(_pair_key(query_row_key, key))
            if label == "重复":
                correct.append(key)
            elif label == "不重复":
                known_wrong.append(key)
            else:
                unknown.append(key)
        seen.add(key)
    member_label_pass = decision == "重复" and bool(correct) and not (known_wrong or unknown or illegal)
    return MemberAudit(
        query_set=query["set_kind"],
        correct=tuple(correct),
        known_wrong=tuple(known_wrong),
        unknown=tuple(unknown),
        illegal=tuple(illegal),
        member_label_pass=member_label_pass,
        m_positive_member_gate_failed=(
            query["set_kind"] == "M" and bool(query["g_i"]) and not member_label_pass
        ),
    )


def pending_pair_queue(
    catalog: MaterialCatalog,
    pairs: Iterable[tuple[str, str]],
) -> list[dict[str, str]]:
    """只接受显式获准文本对，返回不含正文和标签的待审清单。"""
    known = {row.row_key for row in catalog.rows}
    queue = []
    seen: set[tuple[str, str]] = set()
    for a, b in pairs:
        key = _pair_key(a, b)
        if a not in known or b not in known or a == b or key in seen:
            raise ValueError("pending pair must be distinct, known and unique")
        seen.add(key)
        queue.append({"pair_id": _pair_id(a, b), "a_row_key": a, "b_row_key": b, "status": "pending"})
    return queue
