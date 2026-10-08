"""P10 通道间统一的候选和覆盖工件。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Literal


ChannelStatus = Literal["complete", "truncated", "unavailable", "not_applicable"]


@dataclass(frozen=True)
class RecallRequest:
    scope_id: str
    business_date: str
    record_id: str
    item_id: str
    arrival_seq: int
    text: str = field(repr=False)
    visible_seq: int | None = None
    outer_trim_approved: bool = False
    prepared_seq: int | None = None
    embedding_space_id: str | None = None

    def __post_init__(self) -> None:
        if not all(isinstance(value, str) and value for value in
                   (self.scope_id, self.record_id, self.item_id)):
            raise ValueError("scope and record identities must be nonempty")
        if date.fromisoformat(self.business_date).isoformat() != self.business_date:
            raise ValueError("business_date must be canonical")
        if type(self.arrival_seq) is not int or self.arrival_seq < 1:
            raise ValueError("arrival_seq must be positive")
        if not isinstance(self.text, str):
            raise TypeError("text must be str")
        if not isinstance(self.outer_trim_approved, bool):
            raise TypeError("outer_trim_approved must be bool")
        for name, frontier in (("visible_seq", self.visible_seq),
                               ("prepared_seq", self.prepared_seq)):
            if frontier is not None and (type(frontier) is not int or frontier < 0):
                raise ValueError(name + " must be a nonnegative certified frontier")


@dataclass(frozen=True)
class RecallCandidate:
    record_id: str
    item_id: str
    arrival_seq: int
    text: str = field(repr=False)
    subpaths: tuple[str, ...]
    score: float
    query_version: str
    chunk_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class ChannelResult:
    channel: str
    status: ChannelStatus
    candidates: tuple[RecallCandidate, ...]
    query_version: str
    pages: int = 0
    visible_seq: int | None = None
    error_code: str | None = None
    truncated_buckets: tuple[str, ...] = ()
    coverage_complete: bool = False
    topk_excluded: int = 0
    prepared_seq: int | None = None
    # 窗口W2Fβ（WB2-L1 条58）：通道自报耗时（毫秒，service clock 两次读数差；
    # additive——未计量来源保持 None，不冒充已计量）。
    elapsed_ms: float | None = None
