# -*- coding: utf-8 -*-
"""H-02(b) H2-5（E 批，log\\temp\\e-batch-design.md §1.2）：vector 覆盖证明基
写侧推进器 + L3 对账器。

- 推进器（VectorFrontierAdvancer）：批次写确认毕→已证 ready 序集经
  `advance_prepared_frontier`（H2-1 同件，W-R4 语义——无证明不跳洞、单调
  不回退）推进 vector_frontier→日控制文档 `checkpoint.vector_prepared`
  CAS 更新（prepare.py:380-430 同工艺）；快照代际 schema_version 显式。
- 对账器（VectorReconciler）：(reconciled_through, vector_frontier] 区间
  逐 seq 查证——无行且无权威 → foreign_proof_count+=1（仅外国证明加护
  场景）；有行但无现行代次写确认日志 → record_hole(stale_generation_row)
  + hole_count+=1；reconciled_through 推进到 vector_frontier；对账故障
  显式记账不静默（E 批 R2/R5 对冲）。
- 双独立实现纪律（E 批 R1 对冲②）：本模块（推进/对账写侧）与
  recall/vector_store.py VectorFrontierProvider（读侧）除
  advance_prepared_frontier 求值函数外不共享任何状态。

无现役消费点（live 装配根接入前 additive 新件；读侧红测 G10-G12 钉
快照读取契约，本模块按同一契约产出快照）。
"""
from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from news_flash_dedup.admission import CONTROL_INDEX

from .prepare import advance_prepared_frontier
from .vector_store import VectorFrontierProvider

SNAPSHOT_SCHEMA = VectorFrontierProvider.SNAPSHOT_SCHEMA   # "vector-prepared-v1"
SNAPSHOT_KEY = VectorFrontierProvider.SNAPSHOT_KEY         # "vector_prepared"


class VectorFrontierConflict(RuntimeError):
    """vector 前沿快照 CAS 冲突超界——放弃本轮不掩盖（PrepareConflict 同形）。"""


def _utc_iso(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("clock must be timezone aware")
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z")


def _snapshot_of(source: Mapping) -> dict | None:
    checkpoint = source.get("checkpoint")
    snapshot = checkpoint.get(SNAPSHOT_KEY) if isinstance(checkpoint, dict) else None
    return dict(snapshot) if isinstance(snapshot, dict) else None


def _int_field(snapshot: dict | None, name: str) -> int:
    if not isinstance(snapshot, dict):
        return 0
    value = snapshot.get(name)
    return value if type(value) is int and value >= 0 else 0


@dataclass(frozen=True)
class VectorFrontierAdvanceReport:
    """一轮推进报告（停滞可观测：generation/advanced_at 入快照，E 批 R2 对冲）。"""
    scope_id: str
    business_date: str
    space_id: str
    vector_frontier: int
    max_ready_seen: int
    generation: int
    advanced: bool


@dataclass(frozen=True)
class VectorReconcileReport:
    """一轮对账报告（孔洞/外国证明计数显式记账，不静默）。"""
    scope_id: str
    business_date: str
    space_id: str
    reconciled_through: int
    holes_opened: int
    foreign_proofs: int


class VectorFrontierAdvancer:
    """vector_prepared 快照推进器（prepare.py:380-430 CAS 工艺同形）。

    store：ElasticsearchBatchStore 同形（get/create/replace/is_conflict）。
    """

    def __init__(self, store: Any, *, max_cas_attempts: int = 8,
                 clock: Callable[[], datetime] | None = None) -> None:
        if max_cas_attempts < 1:
            raise ValueError("max_cas_attempts must be positive")
        self._store = store
        self.max_cas_attempts = max_cas_attempts
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _day_body(self, scope_id: str, business_date: str, space_id: str,
                  now: str) -> dict:
        return {
            "kind": "day", "scope_id": scope_id, "business_date": business_date,
            "updated_at": now,
            "checkpoint": {SNAPSHOT_KEY: {
                "schema_version": SNAPSHOT_SCHEMA,
                "scope_id": scope_id, "business_date": business_date,
                "space_id": space_id,
                "vector_frontier": 0, "max_ready_seen": 0,
                "reconciled_through": 0, "hole_count": 0,
                "foreign_proof_count": 0, "generation": 0,
                "advanced_at": now,
            }},
        }

    def advance(self, scope_id: str, business_date: str, space_id: str,
                ready_seqs: Iterable[int], *,
                foreign_proofs: Iterable[int] | None = None,
                hole_count: int | None = None) -> VectorFrontierAdvanceReport:
        """推进 vector_frontier（无证明不跳洞、单调不回退）。

        ready_seqs=本批写确认已证 arrival_seq 集（调用方代次过滤后供给）；
        foreign_proofs=已证他域/日序号（跳过凭证明，W-R4 同语义）；
        hole_count=L3 对账开孔洞计数透传（None=沿用已存快照值）。
        """
        ready = {int(seq) for seq in ready_seqs}
        foreign = None if foreign_proofs is None else {int(s) for s in foreign_proofs}
        day_key = VectorFrontierProvider.day_key(scope_id, business_date)
        now = _utc_iso(self.clock())
        for _attempt in range(self.max_cas_attempts):
            day = self._store.get(CONTROL_INDEX, day_key)
            if day is None:
                try:
                    self._store.create(CONTROL_INDEX, day_key,
                                       self._day_body(scope_id, business_date,
                                                      space_id, now))
                except Exception as error:
                    if not self._store.is_conflict(error):
                        raise
                day = self._store.get(CONTROL_INDEX, day_key)
                if day is None:
                    raise VectorFrontierConflict(
                        "vector frontier day control creation is unconfirmed")
            source = day["source"]
            snapshot = _snapshot_of(source)
            if snapshot is not None and snapshot.get("space_id") not in (
                    None, space_id):
                # F3 混空间：既有快照属他空间——fail-closed 不并写。
                raise VectorFrontierConflict(
                    "vector frontier snapshot belongs to another space")
            stored = _int_field(snapshot, "vector_frontier")
            frontier = advance_prepared_frontier(stored, ready, foreign=foreign)
            max_ready_seen = max(
                [_int_field(snapshot, "max_ready_seen"), *ready] or [0])
            holes = (_int_field(snapshot, "hole_count")
                     if hole_count is None else int(hole_count))
            generation = _int_field(snapshot, "generation") + 1
            checkpoint = source.get("checkpoint")
            new_checkpoint = {
                **(checkpoint if isinstance(checkpoint, dict) else {}),
                SNAPSHOT_KEY: {
                    "schema_version": SNAPSHOT_SCHEMA,
                    "scope_id": scope_id, "business_date": business_date,
                    "space_id": space_id,
                    "vector_frontier": frontier,
                    "max_ready_seen": max_ready_seen,
                    "reconciled_through": _int_field(snapshot,
                                                     "reconciled_through"),
                    "hole_count": holes,
                    "foreign_proof_count": _int_field(snapshot,
                                                      "foreign_proof_count"),
                    "generation": generation,
                    "advanced_at": now,
                },
            }
            updated = {**source, "checkpoint": new_checkpoint, "updated_at": now}
            try:
                self._store.replace(CONTROL_INDEX, day_key, updated,
                                    day["seq_no"], day["primary_term"])
            except Exception as error:
                if self._store.is_conflict(error):
                    continue
                raise
            return VectorFrontierAdvanceReport(
                scope_id=scope_id, business_date=business_date,
                space_id=space_id, vector_frontier=frontier,
                max_ready_seen=max_ready_seen, generation=generation,
                advanced=frontier != stored)
        raise VectorFrontierConflict(
            "vector frontier snapshot CAS contention exceeded")


class VectorReconciler:
    """L3 对账器：(reconciled_through, vector_frontier] 逐 seq 查证并推进。

    store：ElasticsearchBatchStore 同形。row_for_seq(seq)→(row, authority)
    证据供给端口（调用方实查 Milvus/ES）：(None, None)=无行且无权威（外国
    证明加护场景）；(row, authority)=在场对拍。record_hole 孔洞落账端口
    （RealMilvusP19Store.record_hole 同形）。
    """

    def __init__(self, store: Any, *, max_cas_attempts: int = 8,
                 clock: Callable[[], datetime] | None = None) -> None:
        if max_cas_attempts < 1:
            raise ValueError("max_cas_attempts must be positive")
        self._store = store
        self.max_cas_attempts = max_cas_attempts
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def reconcile(self, scope_id: str, business_date: str, space_id: str, *,
                  row_for_seq: Callable[[int], tuple[Any, Any]],
                  journal_confirmed: Callable[[int, Any, Any], bool],
                  record_hole: Callable[[int], None]) -> VectorReconcileReport:
        """区间查证：无行且无权威→foreign+1；有行无现行代次日志→孔洞+1。"""
        day_key = VectorFrontierProvider.day_key(scope_id, business_date)
        now = _utc_iso(self.clock())
        holes_opened = 0
        foreign_proofs = 0
        for _attempt in range(self.max_cas_attempts):
            day = self._store.get(CONTROL_INDEX, day_key)
            if day is None:
                # 无快照=无已证前沿，对账零对象（显式记账一轮零推进）。
                return VectorReconcileReport(
                    scope_id=scope_id, business_date=business_date,
                    space_id=space_id, reconciled_through=0,
                    holes_opened=0, foreign_proofs=0)
            source = day["source"]
            snapshot = _snapshot_of(source)
            if snapshot is None or snapshot.get("space_id") != space_id:
                raise VectorFrontierConflict(
                    "vector frontier snapshot is absent or foreign to this space")
            frontier = _int_field(snapshot, "vector_frontier")
            start = _int_field(snapshot, "reconciled_through")
            if _attempt == 0:
                # 区间查证只在首轮执行（CAS 重试见到的前沿单调不回退，
                # 已查序号不重复查证——与 prepare.py:352-353 提示语义同向）。
                for seq in range(start + 1, frontier + 1):
                    row, authority = row_for_seq(seq)
                    if row is None and authority is None:
                        foreign_proofs += 1      # 外国证明加护场景
                        continue
                    if not journal_confirmed(seq, row, authority):
                        record_hole(seq)         # stale_generation_row 落账
                        holes_opened += 1
            checkpoint = source.get("checkpoint")
            new_checkpoint = {
                **(checkpoint if isinstance(checkpoint, dict) else {}),
                SNAPSHOT_KEY: {
                    **snapshot,
                    "reconciled_through": frontier,
                    "hole_count": _int_field(snapshot, "hole_count") + holes_opened,
                    "foreign_proof_count": (
                        _int_field(snapshot, "foreign_proof_count")
                        + foreign_proofs),
                    "advanced_at": snapshot.get("advanced_at", now),
                },
            }
            updated = {**source, "checkpoint": new_checkpoint, "updated_at": now}
            try:
                self._store.replace(CONTROL_INDEX, day_key, updated,
                                    day["seq_no"], day["primary_term"])
            except Exception as error:
                if self._store.is_conflict(error):
                    continue
                raise
            return VectorReconcileReport(
                scope_id=scope_id, business_date=business_date,
                space_id=space_id, reconciled_through=frontier,
                holes_opened=holes_opened, foreign_proofs=foreign_proofs)
        raise VectorFrontierConflict(
            "vector reconcile snapshot CAS contention exceeded")


__all__ = [
    "SNAPSHOT_KEY",
    "SNAPSHOT_SCHEMA",
    "VectorFrontierAdvanceReport",
    "VectorFrontierAdvancer",
    "VectorFrontierConflict",
    "VectorReconcileReport",
    "VectorReconciler",
]
