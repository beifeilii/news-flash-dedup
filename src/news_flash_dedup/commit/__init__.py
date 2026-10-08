"""P17 commit 包：提交协调骨架与接口（待 UAT）。"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from . import es_store
from . import fake_store
from .coordinator import (
    CommitContext,
    CommitOneError,
    CommitOutcome,
    commit_one,
)
from .es_store import (
    P17IndexPrefixInvalid,
    P17UATGatesNotOpen,
    RealESCommitStore,
    RealESConfig,
    assert_p17_uat_open,
    build_p17_index_prefix,
    validate_p17_index_name,
)
from .fake_store import FakeCommitStore, FakeMainRecord


@dataclass(frozen=True)
class DecideResultStale:
    """P17 03:57 §6.1 / 03:52 §4.2：工程层失败留接口，不吞异常。

    触发场景：
    - prepared_seq < arrival_seq - 1（准备快照过期）
    - lexical_watermark < arrival_seq - 1（BM25 屏障过期）
    - 主记录 CAS 失败（前缀 owner 失效 / CAS 冲突 / 准备快照已换代）
    """
    code: str
    detail: str
    arrival_seq: int
    scope_id: str
    business_date: str
    occurred_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def __str__(self) -> str:
        return f"DECIDE_RESULT_STALE[{self.code}] arrival_seq={self.arrival_seq} {self.detail}"


@dataclass(frozen=True)
class WatermarkSnapshot:
    """P12 coverage 提供的双水位快照（语义对应 ES control.admission_head.pending）。"""
    scope_id: str
    business_date: str
    last_materialized_seq: int
    lexical_watermark: int
    prepared_seq: int
    owner_id: str
    owner_generation: int


def is_watermark_complete(wm: WatermarkSnapshot, arrival_seq: int) -> tuple[bool, str]:
    """P17-INV-7：双水位必须真实覆盖 `arrival_seq - 1`，否则视为不完整。"""
    floor = arrival_seq - 1
    if wm.prepared_seq < floor:
        return False, f"prepared_seq {wm.prepared_seq} < {floor}"
    if wm.lexical_watermark < floor:
        return False, f"lexical_watermark {wm.lexical_watermark} < {floor}"
    return True, ""


def re_freeze_required(history: Mapping, raw_candidates: Iterable[Mapping]
                        ) -> dict[str, Mapping]:
    """P17 03:57 修订 §6.3：增量补查候选 ∪ history 按 arrival_seq 升序重新冻结。"""
    required: dict[str, Mapping] = {history["record_id"]: history}
    for cand in raw_candidates:
        required[cand["record_id"]] = cand
    return dict(sorted(
        required.items(),
        key=lambda kv: (kv[1]["arrival_seq"], kv[1]["record_id"]),
    ))


def incremental_query_window(arrival_seq: int, budget: int = 200) -> tuple[int, int]:
    """增量补查窗口：[arrival_seq - budget, arrival_seq - 1]（含两端）。"""
    floor = max(1, arrival_seq - budget)
    return floor, arrival_seq - 1


@dataclass(frozen=True)
class CommitPlan:
    """P17 提交计划（一次 commit_one 的输入）。"""
    scope_id: str
    business_date: str
    arrival_seq: int
    current_record_id: str
    candidates: tuple[Mapping, ...]
    watermark: WatermarkSnapshot


@dataclass(frozen=True)
class CommitResult:
    """P17 提交结果。"""
    arrival_seq: int
    scope_id: str
    business_date: str
    decision: str
    duplicate_ids: tuple[str, ...]
    audit_complete: bool
    audit_index: str
    commit_state: str
    stale_detail: str = ""


__all__ = [
    "CommitContext",
    "CommitOneError",
    "CommitOutcome",
    "CommitPlan",
    "CommitResult",
    "DecideResultStale",
    "FakeCommitStore",
    "FakeMainRecord",
    "P17IndexPrefixInvalid",
    "P17UATGatesNotOpen",
    "RealESCommitStore",
    "RealESConfig",
    "WatermarkSnapshot",
    "assert_p17_uat_open",
    "build_p17_index_prefix",
    "commit_one",
    "es_store",
    "fake_store",
    "incremental_query_window",
    "is_watermark_complete",
    "re_freeze_required",
    "validate_p17_index_name",
]