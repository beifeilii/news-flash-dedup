"""五路候选 RRF 排序及冻结比较计划。"""

from __future__ import annotations

import math
from dataclasses import dataclass
from fractions import Fraction
from typing import Iterable

from .models import ChannelResult, RecallCandidate, RecallRequest


FUSION_VERSION = "rrf_v1_k60_30_10"
MAIN_CHANNELS = ("hash", "near", "bm25", "embedding", "entity")
CHANNEL_LIMITS = {"near": 40, "bm25": 40, "embedding": 40, "entity": 30}


@dataclass(frozen=True)
class ChannelHit:
    channel: str
    rank: int
    score: float
    visible_seq: int | None
    query_version: str
    subpaths: tuple[str, ...]


@dataclass(frozen=True)
class FusedCandidate:
    candidate: RecallCandidate
    channel_hits: tuple[ChannelHit, ...]
    rrf_score: Fraction
    hash_protected: bool
    selected_for_pair_check: bool
    incremental_required: bool


@dataclass(frozen=True)
class RecallGap:
    channel: str
    reason: str


@dataclass(frozen=True)
class ChannelAudit:
    channel: str
    status: str
    query_version: str | None
    candidate_count: int
    pages: int
    topk_excluded: int
    visible_seq: int | None
    prepared_seq: int | None
    coverage_complete: bool
    truncated_buckets: tuple[str, ...]
    error_code: str | None
    # 窗口W2Fβ（WB2-L1 条58）：additive 两字段——elapsed_ms=通道自报耗时
    # （CHANNEL_MISSING 等未计量来源为 None）；truncate_reason=截断原因独立
    # 字段（status=="truncated" 时镜像 error_code，否则 None；error_code
    # 既有面不动，截断原因不再只能混 error_code 读取）。
    elapsed_ms: float | None = None
    truncate_reason: str | None = None


@dataclass(frozen=True)
class RecallPlan:
    version: str
    merged_top30: tuple[FusedCandidate, ...]
    pair_top10: tuple[FusedCandidate, ...]
    hash_protected: tuple[FusedCandidate, ...]
    incremental_required: tuple[FusedCandidate, ...]
    required: tuple[FusedCandidate, ...]
    recall_gaps: tuple[RecallGap, ...]
    planning_excluded: int
    channel_audits: tuple[ChannelAudit, ...]

    @property
    def recall_incomplete(self) -> bool:
        return bool(self.recall_gaps)


@dataclass(frozen=True)
class CompletionReport:
    unfinished_record_ids: tuple[str, ...]
    budget_exhausted: bool
    recall_incomplete: bool


def _validate_candidate(request: RecallRequest, candidate: RecallCandidate) -> None:
    if (not candidate.record_id or not candidate.item_id or
            candidate.record_id == request.record_id or
            candidate.item_id == request.item_id or
            type(candidate.arrival_seq) is not int or
            not 0 < candidate.arrival_seq < request.arrival_seq or
            not isinstance(candidate.text, str) or
            type(candidate.score) not in (float, int) or
            not math.isfinite(candidate.score)):
        raise ValueError("candidate identity, order or score is invalid")


def _same_authority(left: RecallCandidate, right: RecallCandidate) -> bool:
    return (left.item_id == right.item_id and
            left.arrival_seq == right.arrival_seq and
            left.text == right.text)


def _gap(request: RecallRequest, response: ChannelResult) -> str | None:
    if response.status == "complete":
        if response.error_code:
            return response.error_code
        if not response.coverage_complete:
            return "COVERAGE_UNPROVEN"
        floor = request.arrival_seq - 1
        if (type(request.visible_seq) is not int or
                type(response.visible_seq) is not int or
                min(request.visible_seq, response.visible_seq) < floor):
            return "VISIBLE_FRONTIER_UNPROVEN"
        if response.channel in ("hash", "near", "entity"):
            if (type(request.prepared_seq) is not int or
                    type(response.prepared_seq) is not int or
                    min(request.prepared_seq, response.prepared_seq) < floor):
                return "PREPARED_FRONTIER_UNPROVEN"
        if response.channel == "embedding":
            if (type(response.prepared_seq) is not int or
                    response.prepared_seq < floor):
                return "VECTOR_FRONTIER_UNPROVEN"
        return None
    if (response.channel == "entity" and response.status == "not_applicable" and
            response.error_code is None and not response.candidates):
        return None
    return response.error_code or response.status.upper()


def freeze_recall_plan(request: RecallRequest,
                       responses: Iterable[ChannelResult], *,
                       incremental: Iterable[RecallCandidate] = ()) -> RecallPlan:
    by_channel: dict[str, ChannelResult] = {}
    for response in responses:
        if response.channel not in MAIN_CHANNELS or response.channel in by_channel:
            raise ValueError("unknown or duplicate recall channel")
        if (response.status not in
                ("complete", "truncated", "unavailable", "not_applicable")):
            raise ValueError("invalid recall status")
        if len(response.candidates) > CHANNEL_LIMITS.get(response.channel, math.inf):
            raise ValueError("channel candidate limit exceeded")
        by_channel[response.channel] = response

    authority: dict[str, RecallCandidate] = {}
    hits: dict[str, list[ChannelHit]] = {}
    hash_ids: set[str] = set()
    gaps: list[RecallGap] = []
    audits: list[ChannelAudit] = []
    for channel in MAIN_CHANNELS:
        response = by_channel.get(channel)
        if response is None:
            gaps.append(RecallGap(channel, "CHANNEL_MISSING"))
            audits.append(ChannelAudit(channel, "unavailable", None, 0, 0, 0,
                                       None, None, False, (), "CHANNEL_MISSING"))
            continue
        audits.append(ChannelAudit(
            channel, response.status, response.query_version,
            len(response.candidates), response.pages, response.topk_excluded,
            response.visible_seq, response.prepared_seq,
            response.coverage_complete, response.truncated_buckets,
            response.error_code,
            elapsed_ms=response.elapsed_ms,
            truncate_reason=(response.error_code
                             if response.status == "truncated" else None),
        ))
        reason = _gap(request, response)
        if reason is not None:
            gaps.append(RecallGap(channel, reason))
        seen_in_channel: set[str] = set()
        for rank, candidate in enumerate(response.candidates, 1):
            _validate_candidate(request, candidate)
            if candidate.query_version != response.query_version:
                raise ValueError("candidate query version differs from channel")
            prior = authority.setdefault(candidate.record_id, candidate)
            if not _same_authority(prior, candidate):
                raise ValueError("cross-channel record identity conflict")
            if channel == "hash":
                hash_ids.add(candidate.record_id)
            if candidate.record_id in seen_in_channel:
                continue
            seen_in_channel.add(candidate.record_id)
            hits.setdefault(candidate.record_id, []).append(ChannelHit(
                channel, rank, float(candidate.score), response.visible_seq,
                response.query_version, candidate.subpaths,
            ))

    incremental_ids: set[str] = set()
    for candidate in incremental:
        _validate_candidate(request, candidate)
        prior = authority.setdefault(candidate.record_id, candidate)
        if not _same_authority(prior, candidate):
            raise ValueError("incremental record identity conflict")
        incremental_ids.add(candidate.record_id)

    ranked_ids = sorted(
        hits,
        key=lambda record_id: (
            -sum((Fraction(1, 60 + hit.rank) for hit in hits[record_id]),
                 Fraction(0)),
            authority[record_id].arrival_seq, record_id,
        ),
    )
    # M-12/L 窗口P：[:30][:10] 冗余死切片——前缀 10 即前缀 30 的前 10，等价收敛。
    selected_ids = set(ranked_ids[:10])
    all_items = {
        record_id: FusedCandidate(
            candidate, tuple(hits.get(record_id, ())),
            sum((Fraction(1, 60 + hit.rank)
                 for hit in hits.get(record_id, ())), Fraction(0)),
            record_id in hash_ids, record_id in selected_ids,
            record_id in incremental_ids,
        )
        for record_id, candidate in authority.items()
    }
    merged = tuple(all_items[record_id] for record_id in ranked_ids[:30])
    selected = merged[:10]
    by_arrival = lambda record_id: (authority[record_id].arrival_seq, record_id)
    protected = tuple(all_items[record_id]
                      for record_id in sorted(hash_ids, key=by_arrival))
    incremental_required = tuple(all_items[record_id]
                                 for record_id in sorted(incremental_ids,
                                                         key=by_arrival))
    required: list[FusedCandidate] = []
    required_ids: set[str] = set()
    for item in (*selected, *protected, *incremental_required):
        record_id = item.candidate.record_id
        if record_id not in required_ids:
            required.append(item)
            required_ids.add(record_id)
    return RecallPlan(
        FUSION_VERSION, merged, selected, protected, incremental_required,
        tuple(required), tuple(gaps), max(0, len(ranked_ids) - 30),
        tuple(audits),
    )


def plan_completion(plan: RecallPlan,
                    completed_record_ids: Iterable[str]) -> CompletionReport:
    completed = set(completed_record_ids)
    required_ids = {item.candidate.record_id for item in plan.required}
    if not completed <= required_ids:
        raise ValueError("completion refers to a record outside the frozen plan")
    unfinished = tuple(item.candidate.record_id for item in plan.required
                       if item.candidate.record_id not in completed)
    return CompletionReport(unfinished, bool(unfinished), plan.recall_incomplete)
