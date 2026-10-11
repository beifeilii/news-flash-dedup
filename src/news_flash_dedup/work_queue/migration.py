"""P1a-T6 迁移器：legacy 头文档 pending 全塞 → 任务文档+游标形（卡3.7）。

蓝图 = log\\P1a-预备设计-2026-10-11.md §3.2/§3.3（推荐方案=排空优先
+一次性两阶段搬，可恢复、可回退）：

- **Phase 0 排空**（drain-first）：legacy 物化循环跑至头文档 pending 空
  （或仅剩 RETRY_WAIT/熔断等待残件——不强制清零）；排空证据=验收账式
  快照（accepted/materialized/still_pending/隔离件数 逐值入档）；
- **Phase A 幂等建档**（可中断重跑）：对 pending 内每条 entry——
  copy→任务文档（确定性 ``work_item_id(scope,business_date,arrival_seq)``
  =sha256 三元组）→ ``enqueue_work_item``（create→实时读回对拍冻结字段
  全等；409=已存在→对拍幂等续跑；分歧=``IdentityConflict`` 迁移器停+
  人工裁决不吞；未知=``MigrationUnknown`` 可中断重跑）。无 CAS 收缩、
  无头文档写——任意中断重跑天然幂等（确定性 ID）；
- **Phase B 单次 CAS 切换**（原子无中间态）：
  1. 备份：旧头文档全量快照写 ``admission_head_backup_<epoch>``（control
     索引 checkpoint 槽内，≤4MB=现役 max_log_bytes 同界；保留至排空
     验收通过后清理）；
  2. CAS 头文档：``pending``→摘除、``last_materialized_seq``→
     ``last_terminal``（值不变）、+``last_decision_seq``（=0 现役决策
     水位初值——legacy 头无决策水位字段，镜像写入面随 commit 域推进）、
     +``config_version``=1、``checkpoint.migration``={epoch,搬运条数,
     排空证据引用,切换时刻}。

回退（§3.3 单点开关，不复活不双写）：``rollback_batch_head``——逆向 CAS
把备份快照**精确**写回头文档（pending/last_materialized_seq 随快照恢复；
游标三字段随形态一并退役）。回退后：durable 侧形态卫兵
（"batch log is not in cursor form; … requires migration"）拒受理=
零双写零复活；任务文档不删（已终态留场幂等；未终态与恢复后 pending 同
确定性 ID 同键——两路径同 identity 指向同主记录，物化对拍一致即幂等，
不一致=IDENTITY_CORRUPTION 停+人工）；legacy 路径照常排空收敛。

导入边界：本模块**不进** ``work_queue.__init__``（``batch_admission``
模块级 import work_queue——进 __init__ 即环）；函数级 import
``batch_admission`` 消费其 ``_validate_log``/常量。使用面：
``from news_flash_dedup.work_queue.migration import migrate_batch_head``。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from ..admission import _utc
from .es_store import enqueue_work_item
from .schema import WorkItemV1

#: 备份文档 id 前缀（epoch 后缀=回退点名锚）。
MIGRATION_BACKUP_PREFIX = "admission_head_backup_"

#: 备份快照字节上界（蓝图 §3.2"≤4MB 现役 max_log_bytes 内"）。
MAX_HEAD_SNAPSHOT_BYTES = 4 * 1024 * 1024

#: Phase 0 物化循环轮数上界（每轮批级推进；RETRY_WAIT/熔断残件不消耗轮）。
_DRAIN_MAX_ROUNDS = 64


class MigrationConflict(Exception):
    """迁移结构性冲突（fail-closed：停+人工裁决，不吞）。"""

    reason: str

    def __init__(self, reason: str):
        super().__init__(f"batch head migration conflict: {reason}")
        self.reason = reason


class MigrationUnknown(Exception):
    """迁移确认未知（传输/读写层面）：可中断重跑（幂等收敛），不冒充成功。"""


@dataclass(frozen=True)
class MigrationReport:
    """迁移结果快照（审计/钉 6.5 对拍面）。"""

    epoch: int
    phase: str                     # "absent"|"already-migrated"|"migrated"
    head_form: str                 # "absent"|"cursor"|"legacy"
    moved: int                     # Phase A 本轮新建任务文档数
    replayed: int                  # Phase A 幂等重放（已存在+对拍一致）数
    pending_entries: int           # 切换前 pending 残件数（Phase 0 后）
    switched: bool                 # Phase B 是否执行了 CAS 切换
    backup_id: str | None
    first_seq: int | None
    last_seq: int | None
    drain: dict | None             # Phase 0 排空证据（账式快照）

    def to_dict(self) -> dict:
        return {
            "epoch": self.epoch, "phase": self.phase,
            "head_form": self.head_form, "moved": self.moved,
            "replayed": self.replayed, "pending_entries": self.pending_entries,
            "switched": self.switched, "backup_id": self.backup_id,
            "first_seq": self.first_seq, "last_seq": self.last_seq,
            "drain": self.drain,
        }


def _head_document(store: Any) -> dict | None:
    from ..batch_admission import CONTROL_INDEX, HEAD_ID

    head = store.get(CONTROL_INDEX, HEAD_ID)
    if head is None:
        return None
    if (not isinstance(head.get("source"), dict) or
            type(head.get("seq_no")) is not int or
            type(head.get("primary_term")) is not int):
        raise MigrationUnknown("batch head readback is malformed")
    return head


def _legacy_entries(source: dict) -> list[dict]:
    """legacy pending 批信封摊平（arrival_seq 升序全量；形态先执法）。"""
    from ..batch_admission import BatchAdmissionCoordinator

    BatchAdmissionCoordinator._validate_log(source)
    if "last_terminal" in source:
        raise MigrationConflict("head is already in cursor form")
    entries: list[dict] = []
    for batch in source["pending"]["batches"]:
        entries.extend(batch["entries"])
    entries.sort(key=lambda entry: entry["arrival_seq"])
    return entries


def _work_item_from_entry(entry: dict) -> WorkItemV1:
    """pending entry → 任务文档（身份锚逐字段同源；``_new_work_item``
    的 entry 形孪生——payload/raw_hash/生命周期缺省同口径）。"""
    import hashlib

    return WorkItemV1(
        scope_id=entry["scope_id"],
        request_id=entry["request_id"],
        item_id=entry["item_id"],
        record_id=entry["record_id"],
        business_fingerprint=entry["business_fingerprint"],
        business_date=entry["business_date"],
        arrival_seq=entry["arrival_seq"],
        expires_at=entry["expires_at"],
        received_at=entry["received_at"],
        accepted_at=entry["accepted_at"],
        schema_version=entry["schema_version"],
        pipeline_version=entry["pipeline_version"],
        embedding_space_id=entry["embedding_space_id"],
        delivery_route_ref=entry["delivery_route_ref"],
        trace_id=entry["trace_id"],
        text=entry["text"],
        raw_hash=hashlib.sha256(entry["text"].encode("utf-8")).hexdigest(),
        enqueued_at=entry["accepted_at"],
        updated_at=entry["accepted_at"],
    )


def drain_evidence(source: dict) -> dict:
    """排空证据=验收账式快照（蓝图 Phase 0：逐值入档）。"""
    entries = [entry for batch in source["pending"]["batches"]
               for entry in batch["entries"]]
    checkpoint = source.get("checkpoint", {})
    return {
        "accepted": source["last_allocated_seq"],
        "materialized": source["last_materialized_seq"],
        "still_pending": len(entries),
        "quarantined": len(checkpoint.get("batch_quarantine", [])
                           if isinstance(checkpoint, dict) else []),
    }


def drain_legacy_pending(store: Any, *, owner_id: str,
                         owner_isolated: Callable[[], bool],
                         clock: Callable[[], datetime],
                         limits: Any = None) -> dict:
    """Phase 0 排空：legacy 物化循环跑到 pending 空/无推进。

    返回排空证据快照；``AdmissionUnknown`` 包为 ``MigrationUnknown``
    （排空可中断重跑）。头文档形态卫兵：游标形=无需排空（直接返回
    游标账式——Phase 0 幂等）。
    """
    from ..batch_admission import (AdmissionUnknown, BatchAdmissionCoordinator,
                                   BatchLimits)

    head = _head_document(store)
    if head is None:
        return {"accepted": 0, "materialized": 0, "still_pending": 0,
                "quarantined": 0}
    source = head["source"]
    if "last_terminal" in source:
        # 已迁移：游标账式（terminal=物化面，pending 面恒空）。
        return {"accepted": source["last_allocated_seq"],
                "materialized": source["last_terminal"],
                "still_pending": 0, "quarantined": 0}
    coordinator = BatchAdmissionCoordinator(
        store, owner_id=owner_id, owner_isolated=owner_isolated,
        limits=limits if limits is not None else BatchLimits(), clock=clock)
    for _ in range(_DRAIN_MAX_ROUNDS):
        entries = [entry for batch in source["pending"]["batches"]
                   for entry in batch["entries"]]
        if not entries:
            break
        try:
            coordinator.materialize_prefix()
        except AdmissionUnknown as error:
            raise MigrationUnknown(
                "legacy drain round is unconfirmed") from error
        head = _head_document(store)
        source = head["source"]
    return drain_evidence(source)


def _backup_head(store: Any, source: dict, *, owner_id: str,
                 epoch: int) -> str:
    """Phase B-1 备份：全量快照写备份文档（幂等重放对拍）。"""
    from ..batch_admission import CONTROL_INDEX

    backup_id = f"{MIGRATION_BACKUP_PREFIX}{epoch}"
    body = {
        "kind": "admission", "owner_id": owner_id,
        "checkpoint": {"migration_backup": {"epoch": epoch,
                                            "snapshot": source}},
    }
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    if len(payload) > MAX_HEAD_SNAPSHOT_BYTES:
        raise MigrationConflict(
            "head snapshot exceeds the 4MB backup bound")
    try:
        store.create(CONTROL_INDEX, backup_id, body)
        return backup_id
    except Exception as error:
        if not store.is_conflict(error):
            raise MigrationUnknown("head backup create is unknown") from error
    # 409=已存在：读回对拍（幂等重跑——快照必须逐字节同源）。
    existing = store.get(CONTROL_INDEX, backup_id)
    if existing is None:
        raise MigrationUnknown("head backup conflict readback is unknown")
    snapshot = (existing.get("source", {}).get("checkpoint", {})
                .get("migration_backup", {}).get("snapshot"))
    if snapshot != source:
        raise MigrationConflict(
            "existing head backup diverges from current snapshot")
    return backup_id


def _switch_head(store: Any, head: dict, *, epoch: int, moved: int,
                 drain: dict | None, clock: Callable[[], datetime]) -> bool:
    """Phase B-2 单次 CAS 切换（返回是否本调用执行了切换；已游标=幂等
    返回 False）。"""
    from ..batch_admission import CONTROL_INDEX, HEAD_ID

    source = head["source"]
    if "last_terminal" in source:
        return False
    backup_id = f"{MIGRATION_BACKUP_PREFIX}{epoch}"
    _backup_head(store, source, owner_id=source["owner_id"], epoch=epoch)
    updated = {
        "kind": "admission", "owner_id": source["owner_id"],
        "last_allocated_seq": source["last_allocated_seq"],
        "last_terminal": source["last_materialized_seq"],   # 值不变
        "last_decision_seq": 0,                              # 决策水位初值
        "config_version": 1,
        "checkpoint": {
            **source.get("checkpoint", {}),
            "migration": {"epoch": epoch, "moved": moved, "drain": drain,
                          "switched_at": _utc(clock())},
        },
        "updated_at": _utc(clock()),
    }
    try:
        store.replace(CONTROL_INDEX, HEAD_ID, updated,
                      head["seq_no"], head["primary_term"])
    except Exception as error:
        if store.is_conflict(error):
            # 竞争：重读重判（他方已切=幂等收口；未切=重试一轮）。
            current = _head_document(store)
            if current is not None and "last_terminal" in current["source"]:
                return False
            raise MigrationConflict(
                "head switch CAS lost contention") from error
        raise MigrationUnknown("head switch CAS is unknown") from error
    return True


def migrate_batch_head(store: Any, *, owner_id: str,
                       owner_isolated: Callable[[], bool],
                       clock: Callable[[], datetime], epoch: int,
                       drain: bool = True, limits: Any = None,
                       ) -> MigrationReport:
    """两阶段迁移入口（Phase 0 排空→A 幂等建档→B 单次 CAS 切换）。

    幂等三态：头缺席/已游标形→no-op 报告；legacy 形→完整搬（任意中断
    重跑收敛——Phase A 确定性 ID+Phase B 备份对拍）。身份分歧
    （``IdentityConflict``）→ ``MigrationConflict`` 停+人工（不吞）。
    """
    from ..batch_admission import AdmissionUnknown, IdentityConflict
    from .es_store import WorkPersistUnknown

    if type(epoch) is not int or epoch < 1:
        raise ValueError("epoch must be a positive int")
    head = _head_document(store)
    if head is None:
        return MigrationReport(
            epoch=epoch, phase="absent", head_form="absent",
            moved=0, replayed=0, pending_entries=0, switched=False,
            backup_id=None, first_seq=None, last_seq=None, drain=None)
    source = head["source"]
    if "last_terminal" in source:
        return MigrationReport(
            epoch=epoch, phase="already-migrated", head_form="cursor",
            moved=0, replayed=0, pending_entries=0, switched=False,
            backup_id=None, first_seq=None, last_seq=None, drain=None)
    # Phase 0（可选；残件=0 时零轮直过）。
    evidence = (drain_legacy_pending(store, owner_id=owner_id,
                                     owner_isolated=owner_isolated,
                                     clock=clock, limits=limits)
                if drain else None)
    head = _head_document(store)
    source = head["source"]
    if "last_terminal" in source:
        # 排空期间他方已切（并发迁移者）：幂等收口。
        return MigrationReport(
            epoch=epoch, phase="already-migrated", head_form="cursor",
            moved=0, replayed=0, pending_entries=0, switched=False,
            backup_id=None, first_seq=None, last_seq=None, drain=evidence)
    # Phase A：幂等建档（无头写——中断重跑天然收敛）。
    entries = _legacy_entries(source)
    moved = 0
    replayed = 0
    for entry in entries:
        item = _work_item_from_entry(entry)
        try:
            result = enqueue_work_item(store, item)
        except IdentityConflict as error:
            raise MigrationConflict(
                f"work item identity diverges at arrival_seq "
                f"{entry['arrival_seq']}") from error
        except (AdmissionUnknown, WorkPersistUnknown) as error:
            raise MigrationUnknown(
                "work item copy is unconfirmed") from error
        if result.created:
            moved += 1
        else:
            replayed += 1
    # Phase B：备份+单次 CAS 切换。
    switched = _switch_head(store, head, epoch=epoch, moved=moved,
                            drain=evidence, clock=clock)
    first_seq = entries[0]["arrival_seq"] if entries else None
    last_seq = entries[-1]["arrival_seq"] if entries else None
    return MigrationReport(
        epoch=epoch, phase="migrated" if switched else "already-migrated",
        head_form="cursor" if switched else "cursor",
        moved=moved, replayed=replayed, pending_entries=len(entries),
        switched=switched,
        backup_id=f"{MIGRATION_BACKUP_PREFIX}{epoch}" if switched else None,
        first_seq=first_seq, last_seq=last_seq, drain=evidence)


def rollback_batch_head(store: Any, *, epoch: int,
                        clock: Callable[[], datetime]) -> dict:
    """回退（§3.3 逆向 CAS，纯数据操作）：备份快照精确写回头文档。

    - 备份缺席/头非游标形/``checkpoint.migration.epoch`` 不匹配 →
      ``MigrationConflict``（不冒领别人的纪元）；
    - 回退后头=legacy 全塞形（快照逐字节）：durable 侧形态卫兵拒受理
      （零双写零复活）；legacy 物化循环照常排空收敛；任务文档不删
      （幂等语义见模块 docstring）。
    """
    from ..batch_admission import CONTROL_INDEX, HEAD_ID

    if type(epoch) is not int or epoch < 1:
        raise ValueError("epoch must be a positive int")
    head = _head_document(store)
    if head is None:
        raise MigrationConflict("batch head is absent at rollback")
    source = head["source"]
    if "last_terminal" not in source:
        raise MigrationConflict("batch head is not in cursor form")
    migration = (source.get("checkpoint", {}).get("migration", {})
                 if isinstance(source.get("checkpoint"), dict) else {})
    if migration.get("epoch") != epoch:
        raise MigrationConflict(
            "head migration epoch differs from rollback epoch")
    backup_id = f"{MIGRATION_BACKUP_PREFIX}{epoch}"
    backup = store.get(CONTROL_INDEX, backup_id)
    if backup is None:
        raise MigrationConflict("head backup is absent for the epoch")
    snapshot = (backup.get("source", {}).get("checkpoint", {})
                .get("migration_backup", {}).get("snapshot"))
    if not isinstance(snapshot, dict):
        raise MigrationUnknown("head backup snapshot is malformed")
    restored = {**snapshot, "updated_at": _utc(clock())}
    try:
        store.replace(CONTROL_INDEX, HEAD_ID, restored,
                      head["seq_no"], head["primary_term"])
    except Exception as error:
        if store.is_conflict(error):
            raise MigrationConflict(
                "rollback CAS lost contention") from error
        raise MigrationUnknown("rollback CAS is unknown") from error
    entries = [entry for batch in snapshot["pending"]["batches"]
               for entry in batch["entries"]]
    return {
        "epoch": epoch, "backup_id": backup_id, "restored": True,
        "pending_entries": len(entries),
        "last_materialized_seq": snapshot["last_materialized_seq"],
        "last_allocated_seq": snapshot["last_allocated_seq"],
    }


__all__ = [
    "MAX_HEAD_SNAPSHOT_BYTES",
    "MIGRATION_BACKUP_PREFIX",
    "MigrationConflict",
    "MigrationReport",
    "MigrationUnknown",
    "drain_evidence",
    "drain_legacy_pending",
    "migrate_batch_head",
    "rollback_batch_head",
]
