"""P1a-T6 迁移器钉（卡3.7；验收钉 6.5；蓝图 §3.2/§3.3）。

世界态构造=**真实 legacy 生产者**（legacy 协调器 accept_batch 写头
pending 全塞形）→ 迁移器两阶段搬 → 游标世界 → 回退演练全流程。

钉谱（6.5）：
- 空迁移 no-op（头缺席/已游标两态零写）；
- 完整搬（Phase A 建档+Phase B 备份+CAS 切换；头字段逐值：last_terminal
  =旧 last_materialized_seq 值不变、last_decision_seq=0 初值、
  config_version=1、checkpoint.migration 全息）；
- **迁移前后 arrival_seq 不变**（不重号不跳号——确定性 ID+序号透传）；
- Phase 0 排空（drain-first：残件清零后 Phase A 零建档+账式证据入档）；
- 混合态续跑+中断重跑幂等（Phase A 建档中断→重跑 replayed 收敛）；
- digest/身份分歧停+人工不吞（预置分歧任务文档→MigrationConflict，
  头不切——fail-closed 停在 Phase A）；
- 回退演练 Fake ES 全流程一次：逆向 CAS 精确快照恢复→durable 形态
  卫兵拒受理（无双写无复活）→legacy 路径排空收敛（任务文档不删，
  409→读回对拍自愈）；
- 纪元执法（epoch 校验/回退不冒领他纪元/缺备份拒）。
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from news_flash_dedup.admission import AdmissionRequest
from news_flash_dedup.batch_admission import (
    AdmissionConflict, BatchAdmissionCoordinator, BatchLimits, CONTROL_INDEX,
    HEAD_ID,
)
from news_flash_dedup.work_queue import work_index, work_item_id
from news_flash_dedup.work_queue.migration import (
    MIGRATION_BACKUP_PREFIX, MigrationConflict, MigrationUnknown,
    migrate_batch_head, rollback_batch_head,
)

from test_admission import request
from test_batch_admission import BatchMemoryStore


NOW = datetime(2026, 9, 25, 0, 0, tzinfo=timezone.utc)
BUSINESS_DATE = "2026-09-25"      # NOW 的 Asia/Shanghai 业务日
EPOCH = 20261011


def _legacy(store, when=NOW):
    return BatchAdmissionCoordinator(
        store, owner_id="writer-1", owner_isolated=lambda: True,
        limits=BatchLimits(), clock=lambda: when)


def _durable(store, when=NOW):
    return BatchAdmissionCoordinator(
        store, owner_id="writer-1", owner_isolated=lambda: True,
        limits=BatchLimits(), clock=lambda: when,
        use_durable_queue=True)


def _admission(request_id, text="正文"):
    return AdmissionRequest(
        scope_id="default", request_id=request_id, item_id=request_id,
        text=text, received_at=NOW, schema_version="1", pipeline_version="v1",
        embedding_space_id="v3", delivery_route_ref="audit-route-v1",
        trace_id=f"trace-{request_id}",
    )


def _seed_legacy(store, count):
    """真实 legacy 生产者：accept_batch 写头 pending 全塞形。"""
    service = _legacy(store)
    receipts = []
    for n in range(1, count + 1):
        receipts.extend(service.accept_batch(
            [request(request_id=f"{n}-1", item_id=f"{n}-1",
                     text=f"正文{n}")]))
    return receipts


def _head_source(store):
    return store.get(CONTROL_INDEX, HEAD_ID)["source"]


def _work_source(store, seq, business_date=BUSINESS_DATE):
    return store.get(work_index(business_date),
                     work_item_id("default", business_date, seq))["source"]


# ---------- 空迁移 no-op（钉 6.5） ----------


def test_empty_migration_is_noop_for_absent_and_cursor_heads():
    # 头缺席：零写零报告副作用。
    store = BatchMemoryStore()
    report = migrate_batch_head(
        store, owner_id="writer-1", owner_isolated=lambda: True,
        clock=lambda: NOW, epoch=EPOCH)
    assert report.phase == "absent" and report.head_form == "absent"
    assert report.moved == 0 and report.switched is False
    assert store.get(CONTROL_INDEX, HEAD_ID) is None
    # 已游标形（durable 受理建头）：no-op（幂等重跑安全）。
    store2 = BatchMemoryStore()
    _durable(store2).accept_batch([_admission("1-1")])
    before = _head_source(store2)
    report2 = migrate_batch_head(
        store2, owner_id="writer-1", owner_isolated=lambda: True,
        clock=lambda: NOW, epoch=EPOCH)
    assert report2.phase == "already-migrated"
    assert report2.moved == 0 and report2.switched is False
    assert _head_source(store2) == before      # 零写（逐键对拍）


# ---------- 完整搬：Phase A 建档 + Phase B 切换（钉 6.5） ----------


def test_full_migration_preserves_arrival_seq_and_switches_head():
    store = BatchMemoryStore()
    _seed_legacy(store, 3)
    legacy_head = _head_source(store)
    assert "last_terminal" not in legacy_head           # legacy 全塞形
    assert legacy_head["last_materialized_seq"] == 0
    report = migrate_batch_head(
        store, owner_id="writer-1", owner_isolated=lambda: True,
        clock=lambda: NOW, epoch=EPOCH, drain=False)
    assert report.phase == "migrated" and report.switched is True
    assert report.moved == 3 and report.replayed == 0
    assert report.first_seq == 1 and report.last_seq == 3
    assert report.backup_id == f"{MIGRATION_BACKUP_PREFIX}{EPOCH}"
    # 头切换：游标形逐字段（值不变+初值+纪元）。
    head = _head_source(store)
    assert "pending" not in head and "last_materialized_seq" not in head
    assert head["last_allocated_seq"] == legacy_head["last_allocated_seq"] == 3
    assert head["last_terminal"] == 0                    # =旧 materialized 值
    assert head["last_decision_seq"] == 0                # 决策水位初值
    assert head["config_version"] == 1
    migration_note = head["checkpoint"]["migration"]
    assert migration_note["epoch"] == EPOCH and migration_note["moved"] == 3
    assert migration_note["switched_at"]                 # 切换时刻入档
    # 备份文档在场（全量快照）。
    backup = store.get(CONTROL_INDEX, report.backup_id)["source"]
    snapshot = backup["checkpoint"]["migration_backup"]["snapshot"]
    assert snapshot == legacy_head                       # 逐字节快照
    # 迁移前后 arrival_seq 不变（不重号不跳号）+身份冻结字段全等。
    for seq in (1, 2, 3):
        source = _work_source(store, seq)
        assert source["arrival_seq"] == seq
        assert source["business_date"] == BUSINESS_DATE
        assert source["text"] == f"正文{seq}"
        assert source["task_state"] == "pending"
        assert source["record_id"] == legacy_head["pending"]["batches"][
            seq - 1]["entries"][0]["record_id"]
    # 游标世界照常受理+同身份复用（迁移件=在册任务；同文同身份
    # ——request 助手与 _seed_legacy 同 text 才构成合法复用）。
    durable = _durable(store)
    receipt = durable.accept_batch(
        [request(request_id="1-1", item_id="1-1", text="正文1")])[0]
    assert receipt.reused is True and receipt.arrival_seq == 1


def test_phase0_drains_before_copy():
    # 排空优先：drain=True → legacy 物化循环清 pending → Phase A 零建档
    # → last_terminal 直收物化面；账式证据入档（still_pending=0）。
    store = BatchMemoryStore()
    _seed_legacy(store, 2)
    report = migrate_batch_head(
        store, owner_id="writer-1", owner_isolated=lambda: True,
        clock=lambda: NOW, epoch=EPOCH, drain=True)
    assert report.phase == "migrated" and report.switched is True
    assert report.moved == 0 and report.pending_entries == 0
    assert report.drain == {"accepted": 2, "materialized": 2,
                            "still_pending": 0, "quarantined": 0}
    assert _head_source(store)["last_terminal"] == 2
    # 物化主记录在位（legacy 物化循环产出）。
    receipt = _durable(store).accept_batch(
        [request(request_id="1-1", item_id="1-1", text="正文1")])[0]
    assert receipt.reused is True and receipt.arrival_seq == 1


# ---------- 混合态续跑+中断重跑幂等（钉 6.5） ----------


class _InterruptingStore(BatchMemoryStore):
    """第 k 次 work 索引 create 后注入故障（模拟迁移中断）。"""

    def __init__(self, inner, fail_after):
        super().__init__()
        self._inner = inner
        self._fail_after = fail_after
        self._work_creates = 0

    def create(self, index, key, body):
        if index.startswith("news-dedup-work-v1-"):
            self._work_creates += 1
            if self._work_creates > self._fail_after:
                raise RuntimeError("synthetic interruption")
        return self._inner.create(index, key, body)

    # 基类同名方法逐件显式代理（本壳自有 docs 恒空；__getattr__ 兜
    # 不了基类已定义面——正常查找先命中基类实现）。
    def get(self, index, key):
        return self._inner.get(index, key)

    def replace(self, index, key, body, seq_no, primary_term):
        return self._inner.replace(index, key, body, seq_no, primary_term)

    def mget(self, keys):
        return self._inner.mget(keys)

    def search(self, index, body):
        return self._inner.search(index, body)


def test_interrupted_rerun_is_idempotent():
    store = BatchMemoryStore()
    _seed_legacy(store, 3)
    legacy_head = _head_source(store)
    # 中断：第 2 件建档后注入故障 → MigrationUnknown（可重跑）。
    interrupted = _InterruptingStore(store, fail_after=1)
    with pytest.raises(MigrationUnknown):
        migrate_batch_head(interrupted, owner_id="writer-1",
                           owner_isolated=lambda: True, clock=lambda: NOW,
                           epoch=EPOCH, drain=False)
    # 头不切（Phase A 无头写——fail-closed 中间态安全）。
    assert _head_source(store) == legacy_head
    assert _work_source(store, 1)["arrival_seq"] == 1    # 部分建档在位
    assert store.get(work_index(BUSINESS_DATE),
                     work_item_id("default", BUSINESS_DATE, 2)) is None
    # 重跑：确定性 ID 对拍 → 1 replayed + 2 moved → 完整收敛。
    report = migrate_batch_head(
        store, owner_id="writer-1", owner_isolated=lambda: True,
        clock=lambda: NOW, epoch=EPOCH, drain=False)
    assert report.moved == 2 and report.replayed == 1
    assert report.switched is True
    for seq in (1, 2, 3):                                # arrival_seq 不变
        assert _work_source(store, seq)["arrival_seq"] == seq


def test_identity_divergence_stops_for_human_review():
    # 预置分歧任务文档（同确定性 ID，text 冻结字段漂移）→ Phase A
    # 对拍分歧 → MigrationConflict 停+人工不吞；头不切。
    store = BatchMemoryStore()
    _seed_legacy(store, 2)
    from news_flash_dedup.work_queue.migration import _work_item_from_entry
    entry = _head_source(store)["pending"]["batches"][0]["entries"][0]
    diverged = _work_item_from_entry({
        **entry, "text": "被篡改的正文"})
    store.create(work_index(entry["business_date"]),
                 work_item_id("default", entry["business_date"],
                              entry["arrival_seq"]),
                 diverged.model_dump(mode="json"))
    legacy_head = _head_source(store)
    with pytest.raises(MigrationConflict) as caught:
        migrate_batch_head(store, owner_id="writer-1",
                           owner_isolated=lambda: True, clock=lambda: NOW,
                           epoch=EPOCH, drain=False)
    assert "identity diverges" in str(caught.value)
    assert _head_source(store) == legacy_head            # 头不切（停推进）
    assert store.get(CONTROL_INDEX,
                     f"{MIGRATION_BACKUP_PREFIX}{EPOCH}") is None  # 零备份


# ---------- 回退演练：Fake ES 全流程一次（钉 6.5；蓝图 §3.3） ----------


def test_rollback_drill_restores_legacy_and_drains_without_revival():
    store = BatchMemoryStore()
    _seed_legacy(store, 3)
    legacy_head = _head_source(store)
    report = migrate_batch_head(
        store, owner_id="writer-1", owner_isolated=lambda: True,
        clock=lambda: NOW, epoch=EPOCH, drain=False)
    assert report.switched is True and report.moved == 3
    # 回退（逆向 CAS：精确快照恢复）。
    result = rollback_batch_head(store, epoch=EPOCH, clock=lambda: NOW)
    assert result["restored"] is True and result["pending_entries"] == 3
    assert result["last_materialized_seq"] == 0
    assert result["last_allocated_seq"] == 3
    restored = _head_source(store)
    for key, value in legacy_head.items():               # 精确恢复（含 pending）
        assert restored[key] == value, key
    assert "last_terminal" not in restored               # 游标字段随形态退役
    # 无复活：durable 形态卫兵拒受理（回退后零双写）。
    with pytest.raises(AdmissionConflict, match="requires migration"):
        _durable(store).accept_batch([_admission("9-9")])
    # 任务文档不删：已建档 3 件留场（幂等面——同 identity 同主记录）。
    for seq in (1, 2, 3):
        assert _work_source(store, seq)["arrival_seq"] == seq
    # 旧路径排空收敛：legacy 受理照常（同文复用）+物化循环清 pending。
    legacy = _legacy(store)
    receipt = legacy.accept_batch(
        [request(request_id="1-1", item_id="1-1", text="正文1")])[0]
    assert receipt.reused is True and receipt.arrival_seq == 1
    assert legacy.materialize_prefix() == 3
    drained = _head_source(store)
    assert drained["pending"]["batches"] == []
    assert drained["last_materialized_seq"] == 3
    # 备份文档保留（回退后再迁移=新纪元：头已被排空改动，旧纪元快照
    # 不再适用——同纪元重放=备份分歧拒（纪元纪律））。排空后 pending
    # 已空 → Phase A 零件零重放（任务文档留场=已物化主记录在册，无需
    # 再搬）；新备份入档（排空后快照）+头切游标（terminal=3 物化面）。
    with pytest.raises(MigrationConflict, match="backup diverges"):
        migrate_batch_head(store, owner_id="writer-1",
                           owner_isolated=lambda: True, clock=lambda: NOW,
                           epoch=EPOCH, drain=False)
    report2 = migrate_batch_head(
        store, owner_id="writer-1", owner_isolated=lambda: True,
        clock=lambda: NOW, epoch=EPOCH + 1, drain=False)
    assert report2.phase == "migrated"
    assert report2.moved == 0 and report2.replayed == 0
    assert report2.pending_entries == 0
    assert _head_source(store)["last_terminal"] == 3
    assert report2.backup_id == f"{MIGRATION_BACKUP_PREFIX}{EPOCH + 1}"


def test_rollback_guards_epoch_and_forms():
    store = BatchMemoryStore()
    _seed_legacy(store, 1)
    migrate_batch_head(store, owner_id="writer-1",
                       owner_isolated=lambda: True, clock=lambda: NOW,
                       epoch=EPOCH, drain=False)
    # 纪元不匹配：不冒领别人的纪元（形态/纪元检查先于备份读取）。
    with pytest.raises(MigrationConflict, match="epoch differs"):
        rollback_batch_head(store, epoch=EPOCH + 1, clock=lambda: NOW)
    # 备份缺席（纪元对但备份被人工删除）：拒。
    del store.docs[(CONTROL_INDEX, f"{MIGRATION_BACKUP_PREFIX}{EPOCH}")]
    with pytest.raises(MigrationConflict, match="backup is absent"):
        rollback_batch_head(store, epoch=EPOCH, clock=lambda: NOW)
    # legacy 形头（未迁移）：拒（回退只对游标形有意义）。
    store2 = BatchMemoryStore()
    _seed_legacy(store2, 1)
    with pytest.raises(MigrationConflict, match="not in cursor form"):
        rollback_batch_head(store2, epoch=EPOCH, clock=lambda: NOW)


def test_epoch_and_form_validation():
    store = BatchMemoryStore()
    with pytest.raises(ValueError):
        migrate_batch_head(store, owner_id="w", owner_isolated=lambda: True,
                           clock=lambda: NOW, epoch=0)
    with pytest.raises(ValueError):
        rollback_batch_head(store, epoch=-1, clock=lambda: NOW)
