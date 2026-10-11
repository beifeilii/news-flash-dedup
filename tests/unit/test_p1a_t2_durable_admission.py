"""P1a-T2 受理切换钉（use_durable_queue=True 双写模式；零成本内存面）。

设计锚 = log\\P1a-预备设计-2026-10-11.md §1.2（头文档游标化）/§1.4
（受理闸序铁律：复用→容量→分号→enqueue→CAS）：

- 头文档形态：首次受理即建**游标形**（last_terminal/last_decision_seq/
  config_version；P1a-T4 起过渡别名/pending 空壳不再写——旧形头读面
  宽容）；混合形态（非空 pending+游标标记）fail-closed；
- 闸序：同身份复用永不 429（容量闸前）；容量=durable 档
  （P1a-T4 注入式小容量；ACTIVE 真值对账）；分号=最小空闲
  （孤儿占用集回填崩溃窗缺口）；
- enqueue：T1 确定性 ID 原语（幂等重放对拍）；
- CAS：游标推进；冲突重入以复用面收口（legacy 重试轮同型）；
- 形态卫兵：legacy 受理不认领游标头；durable 受理不认领 legacy 头
  （改形唯一合法通道=迁移器 P1a-T6）；
- 物化 claim 循环=P1a-T3（本卡 materialize_oldest 在游标头上为 no-op）。
"""

from __future__ import annotations

import hashlib
from datetime import datetime, time, timedelta, timezone

import pytest

from news_flash_dedup.admission import (
    AdmissionConflict, AdmissionRequest, AdmissionUnknown, BUSINESS_ZONE,
    CONTROL_INDEX, IdentityConflict, _digest, _utc,
)
from news_flash_dedup.batch_admission import (
    AdmissionCapacityExceeded, DurableQueueDepth, HEAD_ID,
    BatchAdmissionCoordinator, BatchLimits, _new_work_item,
)
from news_flash_dedup.batch_es_store import ElasticsearchBatchStore
from news_flash_dedup.work_queue import (
    WorkItemV1, claim_work_item, complete_work_item, enqueue_work_item,
    load_work_item, work_index, work_item_id,
)

from b4_fake_es import FakeESClient
from test_admission import MemoryStore, request
from test_batch_admission import BatchMemoryStore, coordinator


NOW = datetime(2026, 9, 24, 0, 0, tzinfo=timezone.utc)
BUSINESS_DATE = "2026-09-24"


def other_scope_request(scope_id="other", request_id="100-1",
                        item_id="100-1"):
    return AdmissionRequest(
        scope_id=scope_id, request_id=request_id, item_id=item_id,
        text="甲公司发布新产品。",
        received_at=datetime(2026, 9, 23, 15, 59, tzinfo=timezone.utc),
        schema_version="1", pipeline_version="v1", embedding_space_id="v3",
        delivery_route_ref="audit-route-v1", trace_id="trace-first",
    )


def durable(store, owner="writer-1", limits=None, when=NOW, isolated=True,
            durable_queue=None):
    return BatchAdmissionCoordinator(
        store, owner_id=owner, owner_isolated=lambda: isolated,
        limits=limits or BatchLimits(), clock=lambda: when,
        use_durable_queue=True, durable_queue=durable_queue,
    )


def head_source(store):
    return store.get(CONTROL_INDEX, HEAD_ID)["source"]


def work_docs(store):
    return [(index, key) for index, key in store.docs
            if index.startswith("news-dedup-work-v1-")]


def first_work_source(store):
    assert len(work_docs(store)) == 1
    index, key = work_docs(store)[0]
    return index, key, store.get(index, key)["source"]


# ---------- 头文档游标形态 ----------


def test_first_durable_accept_creates_cursor_head():
    store = BatchMemoryStore()
    receipts = durable(store).accept_batch([request()])
    assert receipts[0].arrival_seq == 1
    source = head_source(store)
    assert source["kind"] == "admission"
    assert source["owner_id"] == "writer-1"
    assert source["last_allocated_seq"] == 1
    assert source["last_terminal"] == 0
    assert source["last_decision_seq"] == 0
    assert source["config_version"] == 1
    # P1a-T4：过渡兼容面摘除——新头不落 last_materialized_seq 别名与
    # pending 空壳（旧形头经游标写点 CAS 收敛新形；读面两形宽容）。
    assert "last_materialized_seq" not in source
    assert "pending" not in source
    assert isinstance(source["checkpoint"], dict)
    assert "updated_at" in source


@pytest.mark.parametrize("mutation", [
    {"last_terminal": 2},                    # terminal > allocated
    {"last_decision_seq": 1},                # decision > terminal
    {"config_version": 0},                   # 纪元必须 ≥1
    {"last_materialized_seq": 1},            # 别名与游标漂移
    {"pending": {"version": "batch-v1", "batches": [{"x": 1}]}},  # 混合形态
])
def test_cursor_head_validation_rejects_drift(mutation):
    base = {
        "kind": "admission", "owner_id": "writer-1",
        "last_allocated_seq": 1, "last_terminal": 0,
        "last_decision_seq": 0, "config_version": 1,
        "last_materialized_seq": 0,
        "pending": {"version": "batch-v1", "batches": []},
        "checkpoint": {}, "updated_at": "2026-09-24T00:00:00.000000Z",
    }
    with pytest.raises(AdmissionConflict, match="batch log validation failed"):
        BatchAdmissionCoordinator._validate_log({**base, **mutation})


# ---------- 受理→任务文档入队 ----------


def test_durable_accept_enqueues_work_docs_with_contiguous_sequences():
    store = BatchMemoryStore()
    receipts = durable(store).accept_batch([
        request(request_id=f"{n}-1", item_id=f"{n}-1", text=f"正文{n}")
        for n in (1, 2, 3)
    ])
    assert [receipt.arrival_seq for receipt in receipts] == [1, 2, 3]
    assert head_source(store)["last_allocated_seq"] == 3
    docs = work_docs(store)
    assert len(docs) == 3
    for receipt in receipts:
        item = load_work_item(store, "default", BUSINESS_DATE,
                              receipt.arrival_seq)
        assert item.task_state == "pending"
        assert item.materialize_state == "accepted"
        assert item.request_id == receipt.request_id
        assert item.record_id == receipt.record_id
        assert item.expires_at == receipt.expires_at
        assert item.raw_hash == hashlib.sha256(
            ("正文%d" % receipt.arrival_seq).encode("utf-8")).hexdigest()


def test_work_doc_coordinates_are_deterministic():
    store = BatchMemoryStore()
    durable(store).accept_batch([request()])
    index, key, source = first_work_source(store)
    assert index == work_index(BUSINESS_DATE)
    assert key == work_item_id("default", BUSINESS_DATE, 1)
    # 33 字段 strict 契约：存储正本即合法任务文档。
    assert WorkItemV1.model_validate(source).arrival_seq == 1


def test_durable_reuse_returns_reused_receipt_without_second_doc():
    store = BatchMemoryStore()
    service = durable(store)
    first, same = service.accept_batch([request(), request()])
    assert first.arrival_seq == same.arrival_seq == 1
    assert first.reused is False and same.reused is True
    replay = service.accept_batch([request()])[0]
    assert replay.reused is True and replay.arrival_seq == 1
    assert len(work_docs(store)) == 1
    assert head_source(store)["last_allocated_seq"] == 1


def test_durable_cross_binding_is_identity_conflict():
    store = BatchMemoryStore()
    service = durable(store)
    service.accept_batch([request()])
    with pytest.raises(IdentityConflict):
        service.accept_batch([request(item_id="other")])
    with pytest.raises(IdentityConflict):
        service.accept_batch([request(request_id="other")])
    with pytest.raises(IdentityConflict):
        service.accept_batch([request(text="乙公司发布三季度财报。")])


def test_durable_lookup_is_scope_coupled():
    # 他域同名 request_id 不得误判复用（检索面 scope 过滤钉）。
    store = BatchMemoryStore()
    service = durable(store)
    service.accept_batch([request()])
    receipt = service.accept_batch([other_scope_request()])[0]
    assert receipt.reused is False and receipt.arrival_seq == 2


# ---------- 容量闸（复用先于容量；P1a-T4 4096 档注入式小容量） ----------


def test_capacity_gate_counts_active_depth_across_days():
    # P1a-T4：容量档=durable_queue_capacity（注入小容量 2；T2 期
    # max_log_items 占位口径已替换——失败零副作用/复用永不 429 不变）。
    store = BatchMemoryStore()
    queue = DurableQueueDepth(capacity=2, warning_depth=1, critical_depth=1,
                              reconcile_interval_seconds=30)
    service = durable(store, durable_queue=queue)
    with pytest.raises(AdmissionCapacityExceeded,
                       match="durable_queue_capacity"):
        service.accept_batch([
            request(request_id=f"{n}-1", item_id=f"{n}-1", text=f"正文{n}")
            for n in (1, 2, 3)])
    # 失败零副作用：游标与队内文档均不变。
    assert head_source(store)["last_allocated_seq"] == 0
    assert work_docs(store) == []
    service.accept_batch([
        request(request_id="1-1", item_id="1-1", text="正文1"),
        request(request_id="2-1", item_id="2-1", text="正文2")])
    # 已受理身份永不 429：纯复用在容量打满时照常通过。
    replay = service.accept_batch([request(request_id="1-1", item_id="1-1",
                                           text="正文1")])[0]
    assert replay.reused is True
    with pytest.raises(AdmissionCapacityExceeded,
                       match="durable_queue_capacity") as caught:
        service.accept_batch([request(request_id="3-1", item_id="3-1",
                                      text="正文3")])
    assert caught.value.depth == 2 and caught.value.capacity == 2


def test_capacity_depth_excludes_terminal_tasks():
    # 终态出账（ACTIVE 真值口径不变）+ P1a-T4 对账收口：绕过协调器的
    # 终态转换（直调原语）→ 计数器漂移偏高 → 时钟推进越过对账周期后
    # 下次受理以 ES 真值收口（drift 收敛=对账分歧取 ES 值）。
    store = BatchMemoryStore()
    queue = DurableQueueDepth(capacity=1, warning_depth=1, critical_depth=1,
                              reconcile_interval_seconds=30)
    clock = {"now": NOW}
    service = BatchAdmissionCoordinator(
        store, owner_id="writer-1", owner_isolated=lambda: True,
        limits=BatchLimits(), clock=lambda: clock["now"],
        use_durable_queue=True, durable_queue=queue)
    service.accept_batch([request()])
    claim = claim_work_item(store, "default", BUSINESS_DATE, 1,
                            owner_id="writer-1", now=NOW)
    complete_work_item(store, "default", BUSINESS_DATE, 1, result=None,
                       owner_id="writer-1",
                       lease_generation=claim.lease_generation, now=NOW)
    # 近似计数器仍=1（终态不经协调器）→ 对账期近似值闸门 fail-closed
    # （宁可多拒：同刻重试照拒）。
    with pytest.raises(AdmissionCapacityExceeded,
                       match="durable_queue_capacity"):
        service.accept_batch([
            request(request_id="8-1", item_id="8-1", text="正文八")])
    # 越过对账周期 → ES 真值（终态出账=0）收口，新身份正常分号。
    clock["now"] = NOW + timedelta(seconds=31)
    receipt = service.accept_batch([
        request(request_id="9-1", item_id="9-1", text="正文九")])[0]
    assert receipt.arrival_seq == 2 and receipt.reused is False
    assert queue.reconciliations == 2 and queue.drift_events == 1


def test_batch_byte_limit_rejects_oversized_item():
    store = BatchMemoryStore()
    service = durable(store, limits=BatchLimits(max_batch_bytes=1024))
    with pytest.raises(AdmissionCapacityExceeded, match="batch_byte_limit"):
        service.accept_batch([request(text="超" * 2048)])
    assert work_docs(store) == []


# ---------- 孤儿（enqueue→CAS 崩溃窗残档）：占用集+收养 ----------


def orphan_item(seq=1, *, request_id="100-1", item_id="100-1",
                business_date=BUSINESS_DATE):
    day = NOW.astimezone(BUSINESS_ZONE).date()
    expiry = datetime.combine(day + timedelta(days=7), time.min, BUSINESS_ZONE)
    return _new_work_item(
        request(request_id=request_id, item_id=item_id),
        _digest(["default", item_id]), seq, NOW,
        business_date, expiry)


def test_orphan_identity_is_reused_and_cursor_adopted():
    store = BatchMemoryStore()
    enqueue_work_item(store, orphan_item(seq=1))   # 崩溃窗残档：序>游标
    receipt = durable(store).accept_batch([request()])[0]
    # 复用面回执（首证为准）+ 收养 CAS 归账（不重复分号）。
    assert receipt.reused is True and receipt.arrival_seq == 1
    assert head_source(store)["last_allocated_seq"] == 1
    assert len(work_docs(store)) == 1


def test_orphan_seq_is_occupied_not_reassigned():
    store = BatchMemoryStore()
    enqueue_work_item(store, orphan_item(seq=1, request_id="999-9",
                                         item_id="999-9"))
    receipt = durable(store).accept_batch([request()])[0]
    # 最小空闲分号：跳过孤儿占用的 1 号，新件取 2；游标收养覆盖孤儿顶。
    assert receipt.arrival_seq == 2 and receipt.reused is False
    assert head_source(store)["last_allocated_seq"] == 2
    assert load_work_item(store, "default", BUSINESS_DATE, 1).request_id == "999-9"


def test_orphan_lookup_spans_business_dates():
    # work 族检索跨业务日（通配索引形态）；他日孤儿同身复用。
    store = BatchMemoryStore()
    enqueue_work_item(store, orphan_item(seq=1, business_date="2026-09-23"))
    receipt = durable(store).accept_batch([request()])[0]
    assert receipt.reused is True and receipt.arrival_seq == 1
    assert head_source(store)["last_allocated_seq"] == 1


# ---------- CAS 竞争：冲突重入以复用面收口 ----------


def test_cas_contention_converges_via_work_reuse():
    class ContendedStore(BatchMemoryStore):
        def __init__(self):
            super().__init__()
            self.bumped = False

        def replace(self, index, key, body, seq_no, primary_term):
            if not self.bumped and (index, key) == (CONTROL_INDEX, HEAD_ID):
                # 并发写者先行改头（合法平移：仅动 updated_at）——
                # 本方持旧 seq_no 的 CAS 必败，重入循环。
                self.bumped = True
                current = self.docs[(index, key)]
                current["source"] = {
                    **current["source"],
                    "updated_at": "2026-09-24T00:00:00.500000Z"}
                current["seq_no"] += 1
            return super().replace(index, key, body, seq_no, primary_term)

    store = ContendedStore()
    service = durable(store)
    receipts = service.accept_batch([request()])
    # 重入轮：已入队任务经复用面收口+收养——不重复分号（与 legacy
    # CAS 重试轮 pending-hit 语义同型：回执 reused=True）。
    assert receipts[0].arrival_seq == 1
    assert head_source(store)["last_allocated_seq"] == 1
    assert len(work_docs(store)) == 1


# ---------- 形态卫兵（双写窗互不认领） ----------


def test_legacy_admission_refuses_cursor_form_head():
    store = BatchMemoryStore()
    durable(store).accept_batch([request()])
    with pytest.raises(AdmissionConflict, match="cursor form"):
        coordinator(store).accept_batch([request()])


def test_durable_admission_refuses_legacy_form_head():
    store = BatchMemoryStore()
    coordinator(store).accept_batch([request()])
    with pytest.raises(AdmissionConflict, match="not in cursor form"):
        durable(store).accept_batch([request()])


def test_release_quarantine_refuses_cursor_form_head():
    store = BatchMemoryStore()
    service = durable(store)
    service.accept_batch([request()])
    with pytest.raises(AdmissionConflict, match="cursor-form"):
        service.release_quarantine(operator="operator-1", disposition="drain")


# ---------- 检索面 fail-closed / 恢复面 ----------


def test_store_without_search_face_fails_closed():
    class NoSearchStore(BatchMemoryStore):
        search = None

    with pytest.raises(AdmissionConflict, match="search-capable"):
        durable(NoSearchStore()).accept_batch([request()])


def test_reconcile_finds_pending_work_without_materialization():
    store = BatchMemoryStore()
    service = durable(store)
    service.accept_batch([request()])
    receipt = service.reconcile(request())
    assert receipt.arrival_seq == 1 and receipt.reused is True
    with pytest.raises(AdmissionUnknown):
        service.reconcile(request(request_id="404-1", item_id="404-1"))


def test_materialize_drains_via_claim_loop():
    # P1a-T3 落地后：materialize_oldest=单轮 claim（完成→last_terminal
    # 推进+别名平移）；recover=排空驱动（T2 期占位钉随新世界更新）。
    store = BatchMemoryStore()
    service = durable(store)
    service.accept_batch([
        request(request_id=f"{n}-1", item_id=f"{n}-1", text=f"正文{n}")
        for n in (1, 2)])
    assert service.materialize_oldest() == 2
    source = head_source(store)
    assert source["last_terminal"] == 2
    # P1a-T4：别名不再平移写（过渡字段摘除——游标写点剥旧形字段）。
    assert "last_materialized_seq" not in source
    assert service.recover() == 2


# ---------- recovery_metrics 游标形头兼容 ----------


def _cursor_head_source(allocated, terminal, *, quarantine=None):
    return {
        "kind": "admission", "owner_id": "writer-1",
        "last_allocated_seq": allocated, "last_terminal": terminal,
        "last_decision_seq": terminal, "config_version": 1,
        "last_materialized_seq": terminal,
        "pending": {"version": "batch-v1", "batches": []},
        "checkpoint": ({} if quarantine is None
                       else {"batch_quarantine": quarantine}),
    }


def test_recovery_metrics_reads_cursor_head_with_active_count():
    from news_flash_dedup.recovery_metrics import recovery_metrics

    metrics = recovery_metrics(
        _cursor_head_source(allocated=3, terminal=2),
        [], active_work_items=1)
    assert metrics["accepted"] == 3
    assert metrics["ready"] == 2
    assert metrics["still_processing"] == 1
    assert metrics["accounting_balanced"] is True


def test_recovery_metrics_cursor_head_requires_active_count():
    from news_flash_dedup.recovery_metrics import recovery_metrics

    with pytest.raises(ValueError, match="active_work_items"):
        recovery_metrics(_cursor_head_source(allocated=3, terminal=0), [])


def test_recovery_metrics_legacy_head_ignores_active_count():
    from news_flash_dedup.recovery_metrics import recovery_metrics

    legacy_head = {
        "kind": "admission", "owner_id": "writer-1",
        "last_allocated_seq": 1, "last_materialized_seq": 0,
        "pending": {"version": "batch-v1", "batches": []},
        "checkpoint": {},
    }
    metrics = recovery_metrics(legacy_head, [], active_work_items=7)
    assert metrics["still_processing"] == 0


# ---------- prepare ②路：work 任务文档族登记查证（frozen-path 触点） ----------


def _proof_worker():
    from news_flash_dedup.recall.prepare import PrepareWorker

    client = FakeESClient()
    prefix = "p01-batch-t2proof-"
    store = ElasticsearchBatchStore(client, index_prefix=prefix)
    return PrepareWorker(client, prefix), store


def test_prepare_foreign_proofs_read_work_index_across_days():
    # P1a-T2 ②路替换钉：未物化段登记证明=work 任务文档族检索（原头文档
    # pending 查证面退役）；他域/他日登记 → foreign；本分区登记 → 不证。
    worker, store = _proof_worker()
    day = NOW.astimezone(BUSINESS_ZONE).date()
    expiry = datetime.combine(day + timedelta(days=7), time.min, BUSINESS_ZONE)
    foreign = _new_work_item(
        other_scope_request(request_id="f-1", item_id="f-1"),
        _digest(["other", "f-1"]), 1, NOW, "2026-09-23", expiry)
    own = _new_work_item(
        request(request_id="o-1", item_id="o-1"),
        _digest(["default", "o-1"]), 2, NOW, "2026-09-24", expiry)
    enqueue_work_item(store, foreign)
    enqueue_work_item(store, own)
    proven = worker._scan_foreign_proofs("default", "2026-09-24", [1, 2],
                                         None)
    assert proven == {1}


def test_prepare_foreign_proofs_stop_closed_on_absent_registration():
    # 登记缺席（无任务文档）→ 不证（fail-closed 停摆不猜）。
    worker, _store = _proof_worker()
    assert worker._scan_foreign_proofs("default", "2026-09-24", [7],
                                       None) == set()
