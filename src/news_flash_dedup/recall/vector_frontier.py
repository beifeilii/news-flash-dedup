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

2026-10-09（P0-a 修订二，主窗口修订令 / log\判定链改造技术设计书-v1-1009.md
§②）：首个消费点落地——vector/milvus_store.py upsert_chunks 写确认尾段
经 COVERAGE_FRONTIER_ENV 开关（默认关）驱动本模块推进器；读侧
fail-closed 求值语义（vector_store.py:430-447）不变。

P0-T5 全前沿令（主窗口墓碑全前沿链扩充⑤⑧）：推进器/对账器各增设
terminal_evidence 墓簿端口（None=未注入，行为逐字节不变）——注入后
advance 自取本域日墓证（缺向量序号=确定性终态，vector 前沿不因缺向量
永久停滞；墓碑不要求 embedding 生成）；对账区间墓证序号跳过且**不
record_hole**（缺向量≠孤儿语义分立），tombstone_proofs 分计不算外国；
端口契约=materialize_terminal.scan_terminal_tombstone_seqs 同源读面
（materialize/prepare/vector 各前沿共读同一终态证据源，不伪装成新闻
记录）。
"""
from __future__ import annotations

import os
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from news_flash_dedup.admission import CONTROL_INDEX

from .prepare import advance_prepared_frontier
from .vector_store import VectorFrontierProvider

SNAPSHOT_SCHEMA = VectorFrontierProvider.SNAPSHOT_SCHEMA   # "vector-prepared-v1"
SNAPSHOT_KEY = VectorFrontierProvider.SNAPSHOT_KEY         # "vector_prepared"


# 2026-10-09（P0-a 修订二，主窗口修订令）：覆盖闸 frontier 写侧接线开关
# DEDUP_COVERAGE_FRONTIER——默认关=现役逐字节（vector_prepared 快照零写者，
# 读侧恒 unproven 形态不动）；开=写侧真件在 Milvus 写入 ack 后推进。
COVERAGE_FRONTIER_ENV = "DEDUP_COVERAGE_FRONTIER"


def coverage_frontier_enabled(env: Mapping[str, str] | None = None) -> bool:
    """开关解析：精确 "1"=开；缺省/空串/其余一律关（fail-closed 默认关，
    "true"/大小写变体等不放大——service.py 模式闸 mode_from_environment
    同型纪律）。"""
    source = os.environ if env is None else env
    return source.get(COVERAGE_FRONTIER_ENV, "") == "1"


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
    # P0-T5 全前沿令⑤：本轮凭墓簿跳过的终态序号计数（观测/对账面）。
    tombstone_proofs: int = 0


@dataclass(frozen=True)
class VectorReconcileReport:
    """一轮对账报告（孔洞/外国证明/墓证计数显式记账，不静默）。"""
    scope_id: str
    business_date: str
    space_id: str
    reconciled_through: int
    holes_opened: int
    foreign_proofs: int
    # P0-T5 全前沿令⑤：区间内墓簿终态序号计数（跳过不孔洞、不算外国）。
    tombstone_proofs: int = 0


class VectorFrontierAdvancer:
    """vector_prepared 快照推进器（prepare.py:380-430 CAS 工艺同形）。

    store：ElasticsearchBatchStore 同形（get/create/replace/is_conflict）。
    """

    def __init__(self, store: Any, *, max_cas_attempts: int = 8,
                 clock: Callable[[], datetime] | None = None,
                 terminal_evidence: Callable[[str, str], Iterable[int]]
                 | None = None) -> None:
        if max_cas_attempts < 1:
            raise ValueError("max_cas_attempts must be positive")
        self._store = store
        self.max_cas_attempts = max_cas_attempts
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        # P0-T5 全前沿令⑤⑧（主窗口扩充令）：墓簿终态证据端口——注入后
        # 每次 advance 自取本域日墓证（vector 前沿不因缺向量永久停滞；
        # 墓碑不要求 embedding 生成）；None=未注入（现役行为逐字节不变，
        # additive 纪律）。端口契约=materialize_terminal.scan_terminal_
        # tombstone_seqs 同源读面（各前沿共读同一终态证据源）。
        self.terminal_evidence = terminal_evidence

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
                "foreign_proof_count": 0, "tombstone_proof_count": 0,
                "generation": 0,
                "advanced_at": now,
            }},
        }

    def advance(self, scope_id: str, business_date: str, space_id: str,
                ready_seqs: Iterable[int], *,
                foreign_proofs: Iterable[int] | None = None,
                hole_count: int | None = None,
                terminal_proofs: Iterable[int] | None = None) -> VectorFrontierAdvanceReport:
        """推进 vector_frontier（无证明不跳洞、单调不回退）。

        ready_seqs=本批写确认已证 arrival_seq 集（调用方代次过滤后供给）；
        foreign_proofs=已证他域/日序号（跳过凭证明，W-R4 同语义）；
        hole_count=L3 对账开孔洞计数透传（None=沿用已存快照值）；
        terminal_proofs=已证墓碑终态序号（P0-T5 全前沿令⑤：本域本日
        item_permanent 确定性已处理——缺向量序号的合法跳洞凭据；
        None=无显式墓证输入；注入 terminal_evidence 端口时另自取合并）。
        """
        ready = {int(seq) for seq in ready_seqs}
        foreign = None if foreign_proofs is None else {int(s) for s in foreign_proofs}
        terminal = (None if terminal_proofs is None
                    else {int(s) for s in terminal_proofs})
        if self.terminal_evidence is not None:
            evidence = {int(s) for s in self.terminal_evidence(
                scope_id, business_date)}
            terminal = evidence if terminal is None else terminal | evidence
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
            frontier = advance_prepared_frontier(stored, ready, foreign=foreign,
                                                  terminal=terminal)
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
                    # 墓证累计（有界审计位；快照谱系 hole_count/foreign 同
                    # 款累计口径，调用方 journal 重导确定性可复核）。
                    "tombstone_proof_count": (
                        _int_field(snapshot, "tombstone_proof_count")
                        + sum(1 for seq in (terminal or ())
                              if stored < seq <= frontier)),
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
                advanced=frontier != stored,
                tombstone_proofs=sum(1 for seq in (terminal or ())
                                     if stored < seq <= frontier))
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
                 clock: Callable[[], datetime] | None = None,
                 terminal_evidence: Callable[[str, str], Iterable[int]]
                 | None = None) -> None:
        if max_cas_attempts < 1:
            raise ValueError("max_cas_attempts must be positive")
        self._store = store
        self.max_cas_attempts = max_cas_attempts
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        # P0-T5 全前沿令⑤⑧：墓簿终态证据端口（None=未注入，现役行为
        # 逐字节不变）。注入后区间内墓证序号跳过——**不 record_hole**
        # （缺向量=确定性终态而非孤儿），tombstone_proofs 分计不算外国。
        self.terminal_evidence = terminal_evidence

    def reconcile(self, scope_id: str, business_date: str, space_id: str, *,
                  row_for_seq: Callable[[int], tuple[Any, Any]],
                  journal_confirmed: Callable[[int, Any, Any], bool],
                  record_hole: Callable[[int], None]) -> VectorReconcileReport:
        """区间查证：墓证序号跳过（不孔洞）；无行且无权威→foreign+1；
        有行无现行代次日志→孔洞+1。"""
        day_key = VectorFrontierProvider.day_key(scope_id, business_date)
        now = _utc_iso(self.clock())
        holes_opened = 0
        foreign_proofs = 0
        tombstone_proofs = 0
        terminal = None
        if self.terminal_evidence is not None:
            terminal = {int(s) for s in self.terminal_evidence(
                scope_id, business_date)}
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
                    if terminal is not None and seq in terminal:
                        # P0-T5 全前沿令⑤：墓证序号=确定性终态（item_
                        # permanent 入墓，向量结构性缺席）——跳过且**不落
                        # 孔洞**（孤儿=应有向量而行不在场；墓证=本就不该
                        # 有向量，两语义分立），tombstone_proofs 分计。
                        tombstone_proofs += 1
                        continue
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
                    "tombstone_proof_count": (
                        _int_field(snapshot, "tombstone_proof_count")
                        + tombstone_proofs),
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
                holes_opened=holes_opened, foreign_proofs=foreign_proofs,
                tombstone_proofs=tombstone_proofs)
        raise VectorFrontierConflict(
            "vector reconcile snapshot CAS contention exceeded")


__all__ = [
    "COVERAGE_FRONTIER_ENV",
    "SNAPSHOT_KEY",
    "SNAPSHOT_SCHEMA",
    "VectorFrontierAdvanceReport",
    "VectorFrontierAdvancer",
    "VectorFrontierConflict",
    "VectorReconcileReport",
    "VectorReconciler",
    "coverage_frontier_enabled",
]
