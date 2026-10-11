"""P1a-T4 容量 4096+429+manifest 钉（卡3.5；验收钉 6.3 全谱；零成本内存面）。

设计锚 = log\\P1a-预备设计-2026-10-11.md §1.4（队列容量与"429 先于分号"）：

- 深度口径=全库非终态任务文档数（跨业务日累计；ACTIVE 真值对账）；
- 近似计数器（enqueue/终态增减）+周期 ES count 对账：分歧取 ES 值+
  告警；对账期间以近似值闸门 fail-closed（宁可多拒）；
- 三档 4096/3277/3891：warning 仅指标/critical 升级告警+最老任务年龄
  联动/hard 拒收 AdmissionCapacityExceeded("durable_queue_capacity")；
- 闸序铁律（钉 6.3）：429 时 last_allocated_seq 与队内最大 arrival_seq
  均不变（realtime GET 对拍）；同身份队满重试不 429（复用先于容量闸）；
  排空后新身份正常分号；
- 端口面 reason 集扩：durable_queue_capacity→Backpressure 子类（429+
  Retry-After 建议）；collector queue_depth_face 预拒收+档位观测
  （未接线=零行为 diff）；
- 头文档过渡字段（last_materialized_seq 别名+pending 空壳）写面摘除：
  新头不落；旧形头经游标写点 CAS 剥离收敛。

灌队世界态构造：`_seed_queue`（头文档游标形+work 任务文档族，等价于
"已受理未物化"的既成世界——T3/T8 全前沿钉同型种子工艺）。
"""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone

import pytest

from news_flash_dedup.admission import (
    AdmissionRequest, BUSINESS_ZONE, CONTROL_INDEX, _digest, _utc,
)
from news_flash_dedup.api.contract import Backpressure, IngressContext, submit
from news_flash_dedup.batch_admission import (
    ADMISSION_CONFIG_VERSION, AdmissionCapacityExceeded,
    BatchAdmissionCoordinator, BatchLimits, DURABLE_QUEUE_CAPACITY,
    DURABLE_QUEUE_CRITICAL_DEPTH, DURABLE_QUEUE_WARNING_DEPTH,
    DurableQueueDepth, HEAD_ID, queue_depth_tier, _new_work_item,
)
from news_flash_dedup.batch_collector import (
    BatchAdmissionCollector, CollectorRejected,
)
from news_flash_dedup.product.admission_port import (
    CollectorAdmissionPort, DurableQueueBackpressure,
    suggest_retry_after_seconds,
)
from news_flash_dedup.recovery_metrics import recovery_metrics
from news_flash_dedup.work_queue import enqueue_work_item

from test_admission import request
from test_batch_admission import BatchMemoryStore


NOW = datetime(2026, 9, 24, 0, 0, tzinfo=timezone.utc)
BUSINESS_DATE = "2026-09-24"
EXPIRY = datetime.combine(
    NOW.astimezone(BUSINESS_ZONE).date() + timedelta(days=7),
    time.min, BUSINESS_ZONE)
WORK_PREFIX = "news-dedup-work-v1-"


def head_source(store):
    return store.get(CONTROL_INDEX, HEAD_ID)["source"]


def work_docs(store):
    return [key for index, key in store.docs if index.startswith(WORK_PREFIX)]


def seed_item(seq, *, accepted_at=NOW):
    return _new_work_item(
        request(request_id=f"{seq}-1", item_id=f"{seq}-1",
                text=f"正文{seq}"),
        _digest(["default", f"{seq}-1"]), seq, accepted_at,
        BUSINESS_DATE, EXPIRY)


def _seed_queue(store, count, *, owner="writer-1", old_form=False,
                accepted_at=NOW):
    """灌队世界态：游标形头（cursor=count）+count 件 ACTIVE 任务文档。

    ``old_form=True`` 落 T2/T3 期过渡字段（别名+pending 空壳）——旧形
    头剥离收敛钉的种子。
    """
    body = {
        "kind": "admission", "owner_id": owner,
        "last_allocated_seq": count, "last_terminal": 0,
        "last_decision_seq": 0, "config_version": ADMISSION_CONFIG_VERSION,
        "checkpoint": {}, "updated_at": _utc(NOW),
    }
    if old_form:
        body["last_materialized_seq"] = 0
        body["pending"] = {"version": "batch-v1", "batches": []}
    store.create(CONTROL_INDEX, HEAD_ID, body)
    for seq in range(1, count + 1):
        enqueue_work_item(store, seed_item(seq, accepted_at=accepted_at))
    return store


def durable(store, owner="writer-1", when=NOW, durable_queue=None):
    clock = when if callable(when) else (lambda: when)
    return BatchAdmissionCoordinator(
        store, owner_id=owner, owner_isolated=lambda: True,
        limits=BatchLimits(), clock=clock,
        use_durable_queue=True, durable_queue=durable_queue,
    )


def _admission_request(request_id, text="正文"):
    return AdmissionRequest(
        scope_id="default", request_id=request_id, item_id=request_id,
        text=text, received_at=NOW, schema_version="1", pipeline_version="v1",
        embedding_space_id="v3", delivery_route_ref="audit-route-v1",
        trace_id=f"trace-{request_id}",
    )


# ---------- 纯函数：三档分界（蓝图 §1.4 常数钉） ----------


@pytest.mark.parametrize("depth,expected", [
    (0, "normal"), (3276, "normal"),
    (3277, "warning"), (3890, "warning"),
    (3891, "critical"), (4095, "critical"),
    (4096, "hard"), (5000, "hard"),
])
def test_queue_depth_tier_boundaries(depth, expected):
    assert queue_depth_tier(depth) == expected


@pytest.mark.parametrize("kwargs", [
    {"depth": -1},
    {"capacity": 4096, "warning_depth": 4000, "critical_depth": 3891},  # w>c
    {"capacity": 3890, "warning_depth": 3277, "critical_depth": 3891},  # c>cap
])
def test_queue_depth_tier_rejects_invalid_tiers(kwargs):
    with pytest.raises(ValueError):
        queue_depth_tier(kwargs.pop("depth", 0), **kwargs)


def test_capacity_constants_pinned():
    """4096/3277/3891 单源常量钉（manifest v4 同源登记——漂移即此处红）。"""
    assert DURABLE_QUEUE_CAPACITY == 4096
    assert DURABLE_QUEUE_WARNING_DEPTH == 3277
    assert DURABLE_QUEUE_CRITICAL_DEPTH == 3891
    assert ADMISSION_CONFIG_VERSION == 1


# ---------- DurableQueueDepth 单元面 ----------


def test_counter_enqueue_terminal_floor_and_metrics():
    counter = DurableQueueDepth()
    assert counter.depth() == 0 and counter.tier() == "normal"
    counter.on_enqueue(8)
    assert counter.depth() == 8
    counter.on_terminal(3)
    assert counter.depth() == 5
    counter.on_terminal(99)                  # 下限 0（终态多计不穿负）
    assert counter.depth() == 0
    assert counter.on_enqueue(0) is None      # 零值合法
    metrics = counter.metrics()
    assert metrics == {"depth": 0, "tier": "normal", "capacity": 4096,
                       "reconciliations": 0, "drift_events": 0,
                       "last_drift": None}
    with pytest.raises(ValueError):
        counter.on_enqueue(-1)
    with pytest.raises(ValueError):
        counter.on_terminal(-1)
    with pytest.raises(ValueError):
        DurableQueueDepth(reconcile_interval_seconds=0)
    with pytest.raises(ValueError):
        DurableQueueDepth(capacity=4, warning_depth=5, critical_depth=5)


def test_counter_reconcile_semantics():
    counter = DurableQueueDepth(reconcile_interval_seconds=30)
    t0 = NOW
    assert counter.due(t0) is True           # 首对账=立即
    counter.on_enqueue(5)                    # 近似值与真值一致
    assert counter.reconcile(5, now=t0) == "consistent"
    assert counter.depth() == 5 and counter.drift_events == 0
    assert counter.due(t0 + timedelta(seconds=29)) is False   # 周期内
    assert counter.due(t0 + timedelta(seconds=30)) is True    # 周期到
    # 漂移低注入：真值 7 高于近似值 5（绕过计数器的入队——崩溃漂移
    # /接管残账形态）；分歧取 ES 真值+drift 留痕。
    assert counter.reconcile(7, now=t0 + timedelta(seconds=30)) == "drifted"
    assert counter.depth() == 7              # 分歧取 ES 真值
    assert counter.drift_events == 1 and counter.last_drift == 2
    assert counter.reconciliations == 2
    with pytest.raises(ValueError):
        counter.reconcile(-1, now=t0)        # 畸形真值拒收


def test_admission_capacity_exceeded_durable_attrs():
    error = AdmissionCapacityExceeded(
        "durable_queue_capacity", depth=4100, capacity=4096)
    assert error.reason == "durable_queue_capacity"
    assert error.depth == 4100 and error.capacity == 4096
    assert "durable_queue_capacity" in str(error)
    with pytest.raises(ValueError):
        AdmissionCapacityExceeded("durable_queue_capacity")   # 缺观测属性
    with pytest.raises(ValueError):
        AdmissionCapacityExceeded("durable_queue_capacity",
                                  depth=1, capacity=0)
    with pytest.raises(ValueError):
        AdmissionCapacityExceeded("log_item_limit", depth=1, capacity=2)
    with pytest.raises(ValueError):
        AdmissionCapacityExceeded("no_such_reason")


# ---------- 闸序铁律（验收钉 6.3 全谱） ----------


def test_hard_capacity_fill_4096_rejects_with_zero_side_effects():
    # 灌队 4096（真档常数，懒建缺省计数器）：首次受理对账收口 ES 真值
    # → hard 档 → 新身份 429；失败时游标与队内最大 arrival_seq 均不变
    # （realtime GET 对拍——钉 6.3）。
    store = BatchMemoryStore()
    _seed_queue(store, 4096)
    service = durable(store)                 # 缺省计数器=4096 档
    before_cursor = head_source(store)["last_allocated_seq"]
    before_docs = sorted(store.docs)
    with pytest.raises(AdmissionCapacityExceeded,
                       match="durable_queue_capacity") as caught:
        service.accept_batch([_admission_request("new-1")])
    assert caught.value.depth == 4096 and caught.value.capacity == 4096
    # 零副作用：头游标不变；队内文档集逐键不变（含最大 arrival_seq）。
    assert head_source(store)["last_allocated_seq"] == before_cursor == 4096
    assert sorted(store.docs) == before_docs
    arrivals = [store.get(index, key)["source"]["arrival_seq"]
                for index, key in before_docs if index.startswith(WORK_PREFIX)]
    assert max(arrivals) == 4096 and len(arrivals) == 4096
    # 对账观测面：hard 档+最老任务年龄联动（种子 accepted_at=NOW=时钟）。
    # 懒建计数器首对账自 0 收口真值=bootstrap 漂移事件（审计留痕语义）。
    metrics = service.durable_queue_metrics()
    assert metrics["tier"] == "hard" and metrics["depth"] == 4096
    assert metrics["oldest_age_seconds"] == 0.0
    assert metrics["reconciliations"] == 1 and metrics["drift_events"] == 1
    assert metrics["last_drift"] == 4096


def test_replay_of_accepted_identity_never_429_at_full_queue():
    # 钉 6.3：队满（hard 档）时同身份重试照常复用（复用路径先于容量闸
    # ——早返不触发对账/闸门）。
    store = BatchMemoryStore()
    _seed_queue(store, 4096)
    service = durable(store)
    receipt = service.accept_batch([
        request(request_id="1-1", item_id="1-1", text="正文1")])[0]
    assert receipt.reused is True and receipt.arrival_seq == 1


def test_drain_frees_capacity_then_new_identity_gets_sequence():
    # 钉 6.3：排空（claim 循环终态出账→计数器归零）后新身份正常分号。
    store = BatchMemoryStore()
    _seed_queue(store, 8)
    queue = DurableQueueDepth(capacity=8, warning_depth=1, critical_depth=6,
                              reconcile_interval_seconds=30)
    service = durable(store, durable_queue=queue)
    with pytest.raises(AdmissionCapacityExceeded,
                       match="durable_queue_capacity"):
        service.accept_batch([_admission_request("new-1")])
    assert service.recover() == 8            # claim 循环排空
    assert head_source(store)["last_terminal"] == 8
    assert service.durable_queue_metrics()["depth"] == 0   # 终态出账
    receipt = service.accept_batch([_admission_request("after-1")])[0]
    assert receipt.reused is False and receipt.arrival_seq == 9


def test_reconcile_takes_es_truth_on_counter_drift():
    # 对账漂移注入：计数器虚高（种子 4）→ 首次受理对账收口 ES 真值 0
    # →受理通过；drift 计数留痕（对账期近似值闸门 fail-closed 的收敛面）。
    store = BatchMemoryStore()
    queue = DurableQueueDepth(reconcile_interval_seconds=30)
    queue.on_enqueue(4)                      # 模拟崩溃漂移/接管残账
    service = durable(store, durable_queue=queue)
    receipt = service.accept_batch([_admission_request("fresh-1")])[0]
    assert receipt.arrival_seq == 1
    assert queue.reconciliations == 1 and queue.drift_events == 1
    assert queue.last_drift == -4 and queue.depth() == 1


def test_critical_tier_observes_oldest_task_age():
    # critical 档联动观测（最老任务年龄）：对账点观测——种子 3 件
    # accepted_at=NOW，时钟 NOW+31 受理 2 件新身份 → 对账收口 ES=3
    # （critical，oldest_age_seconds=31）→ 闸门 3+2>4 拒（计数器保持
    # 3=critical——观测面与闸门同点一致）。
    store = BatchMemoryStore()
    _seed_queue(store, 3)
    queue = DurableQueueDepth(capacity=4, warning_depth=1, critical_depth=3,
                              reconcile_interval_seconds=30)
    clock = {"now": NOW + timedelta(seconds=31)}
    service = durable(store, when=lambda: clock["now"],
                      durable_queue=queue)
    with pytest.raises(AdmissionCapacityExceeded,
                       match="durable_queue_capacity"):
        service.accept_batch([_admission_request("one-1"),
                              _admission_request("one-2")])
    metrics = service.durable_queue_metrics()
    assert metrics["tier"] == "critical" and metrics["depth"] == 3
    assert metrics["oldest_age_seconds"] == pytest.approx(31.0, abs=0.5)
    # normal 档：空队首受理 → 无年龄观测（None）。
    store2 = BatchMemoryStore()
    service2 = durable(store2)
    service2.accept_batch([_admission_request("a-1")])
    assert service2.durable_queue_metrics()["oldest_age_seconds"] is None


def test_legacy_side_keeps_max_log_items_gate():
    # 双写窗 False 侧零漂：legacy 容量口径=max_log_items（4096 档只
    # 替换 durable 侧）；计数器面恒 None。
    store = BatchMemoryStore()
    service = BatchAdmissionCoordinator(
        store, owner_id="writer-1", owner_isolated=lambda: True,
        limits=BatchLimits(max_log_items=2), clock=lambda: NOW)
    assert service.durable_queue is None
    assert service.durable_queue_metrics() is None
    assert service.durable_queue_depth() is None
    service.accept_batch([request(request_id="1-1", item_id="1-1",
                                  text="正文1")])
    with pytest.raises(AdmissionCapacityExceeded, match="log_item_limit"):
        service.accept_batch([
            request(request_id=f"{n}-1", item_id=f"{n}-1", text=f"正文{n}")
            for n in (2, 3)])


# ---------- 头文档过渡字段摘除（写面） ----------


def test_new_head_omits_and_accept_strips_transitional_fields():
    # 旧形头（别名+pending 空壳）经受理 CAS 剥离收敛新形。
    store = BatchMemoryStore()
    _seed_queue(store, 0, old_form=True)
    assert "last_materialized_seq" in head_source(store)
    service = durable(store)
    service.accept_batch([_admission_request("fresh-1")])
    source = head_source(store)
    assert source["last_allocated_seq"] == 1
    assert "last_materialized_seq" not in source
    assert "pending" not in source
    # 收敛后读面照常（校验宽容两形）。
    receipt = service.accept_batch([
        request(request_id="fresh-1", item_id="fresh-1", text="正文")])[0]
    assert receipt.reused is True


def test_terminal_advance_strips_transitional_fields():
    # 终态推进写点同剥离：旧形头+1 件在库 → 物化后字段消失。
    store = BatchMemoryStore()
    _seed_queue(store, 1, old_form=True)
    service = durable(store)
    assert service.materialize_oldest() == 1
    source = head_source(store)
    assert source["last_terminal"] == 1
    assert "last_materialized_seq" not in source
    assert "pending" not in source


def test_recovery_metrics_reads_pendingless_cursor_head():
    # 新形游标头（无 pending 键）指标兼容：在途账=active 注入。
    head = {
        "kind": "admission", "owner_id": "writer-1",
        "last_allocated_seq": 5, "last_terminal": 3,
        "last_decision_seq": 3, "config_version": 1,
        "checkpoint": {},
    }
    metrics = recovery_metrics(head, [], active_work_items=2)
    assert metrics["accepted"] == 5 and metrics["ready"] == 3
    assert metrics["still_processing"] == 2
    # legacy 头缺 pending 仍 fail-closed（形态残缺=指标无意义）。
    legacy = {"kind": "admission", "owner_id": "writer-1",
              "last_allocated_seq": 1, "last_materialized_seq": 0}
    with pytest.raises(ValueError, match="pending"):
        recovery_metrics(legacy, [])


# ---------- 端口面：429+Retry-After 建议 ----------


def test_suggest_retry_after_seconds():
    assert suggest_retry_after_seconds(4090, 4096) == 1   # 逼近硬档
    assert suggest_retry_after_seconds(4096, 4096) == 2   # 硬档满
    assert suggest_retry_after_seconds(0, 4096) == 1
    with pytest.raises(ValueError):
        suggest_retry_after_seconds(-1, 4096)
    with pytest.raises(ValueError):
        suggest_retry_after_seconds(1, 0)


def test_port_maps_durable_capacity_to_backpressure_with_retry_after():
    # 真实链路：collector→协调器容量闸→端口 DurableQueueBackpressure
    # （Backpressure 子类——合同层 429 映射零改动）。
    store = BatchMemoryStore()
    queue = DurableQueueDepth(capacity=1, warning_depth=1, critical_depth=1,
                              reconcile_interval_seconds=30)
    service = durable(store, durable_queue=queue)
    collector = BatchAdmissionCollector(
        service, max_wait_seconds=0.005, max_queue_items=8,
        max_queue_bytes=4 * 1024, request_timeout_seconds=0.5,
        close_timeout_seconds=0.05)
    port = CollectorAdmissionPort(collector)
    try:
        receipt = port.accept(_admission_request("1-1"))
        assert receipt.arrival_seq == 1
        with pytest.raises(DurableQueueBackpressure) as caught:
            port.accept(_admission_request("2-1"))
        error = caught.value
        assert isinstance(error, Backpressure)
        assert error.depth == 1 and error.capacity == 1
        assert error.retry_after_seconds == 2   # depth≥capacity=硬档满
        assert "depth=1" in str(error) and "capacity=1" in str(error)
        # 队满重试不 429：复用身份经端口照常回执（协调器闸早返）。
        replay = port.accept(_admission_request("1-1"))
        assert replay.reused is True
    finally:
        collector.close()


def test_contract_maps_durable_backpressure_to_429():
    # 合同层兜底：DurableQueueBackpressure 是 Backpressure 子类 →
    # except Backpressure→429（现役映射零改动）。
    class _Rejecting:
        def accept(self, request):
            raise DurableQueueBackpressure(4096, 4096)

    context = IngressContext(
        scope_id="default", received_at=NOW, schema_version="1",
        pipeline_version="v1", embedding_space_id="none",
        delivery_route_ref="route-v1", max_text_codepoints=20000,
        max_http_bytes=256 * 1024)
    payload = {"traceId": "t-1", "requestId": "1-1", "articleId": None,
               "rewrittenId": "123", "integrationId": None,
               "source": {"title": "", "body": "", "pageDate": "2026-09-24"},
               "rewrite": {"title": "", "body": "正文"}}
    response = submit(payload, context, _Rejecting())
    assert response.status == 429


# ---------- collector 容量数据源换队深度 ----------


def test_collector_hard_tier_fast_rejects_and_observes_tier():
    # 接线 face：hard 档本地快拒 queue_full（零 ES 往返）+档位观测入
    # snapshot（additive 尾字段）。
    store = BatchMemoryStore()
    queue = DurableQueueDepth(capacity=2, warning_depth=1, critical_depth=1,
                              reconcile_interval_seconds=30)
    service = durable(store, durable_queue=queue)
    service.accept_batch([_admission_request("1-1")])
    service.accept_batch([_admission_request("2-1")])   # depth=2=hard
    collector = BatchAdmissionCollector(
        service, max_wait_seconds=0.005, max_queue_items=8,
        max_queue_bytes=4 * 1024, request_timeout_seconds=0.5,
        close_timeout_seconds=0.05,
        queue_depth_face=service.durable_queue_depth)
    try:
        with pytest.raises(CollectorRejected, match="queue_full"):
            collector.accept(_admission_request("3-1"))
        snapshot = collector.snapshot()
        assert snapshot.durable_depth == 2
        assert snapshot.durable_tier == "hard"
        assert snapshot.durable_tier_events == 1
    finally:
        collector.close()


def test_collector_observes_warning_tier_without_reject():
    store = BatchMemoryStore()
    queue = DurableQueueDepth(capacity=4, warning_depth=1, critical_depth=3,
                              reconcile_interval_seconds=30)
    service = durable(store, durable_queue=queue)
    service.accept_batch([_admission_request("1-1")])
    service.accept_batch([_admission_request("2-1")])   # depth=2=warning
    collector = BatchAdmissionCollector(
        service, max_wait_seconds=0.005, request_timeout_seconds=0.5,
        close_timeout_seconds=0.05,
        queue_depth_face=service.durable_queue_depth)
    try:
        receipt = collector.accept(_admission_request("3-1"))
        assert receipt.arrival_seq == 3               # warning 档不拒
        snapshot = collector.snapshot()
        assert snapshot.durable_tier == "warning"
        assert snapshot.durable_tier_events == 1
    finally:
        collector.close()


def test_collector_face_failure_is_fail_soft_and_default_unwired():
    # face 异常按 None（观测面不反噬受理）；未接线=零行为 diff（尾字段
    # 恒 None/0——既有观察者零回归）。
    def _boom():
        raise RuntimeError("depth face unavailable")

    store = BatchMemoryStore()
    service = durable(store)
    collector = BatchAdmissionCollector(
        service, max_wait_seconds=0.005, request_timeout_seconds=0.5,
        close_timeout_seconds=0.05, queue_depth_face=_boom)
    try:
        receipt = collector.accept(_admission_request("1-1"))
        assert receipt.arrival_seq == 1               # fail-soft 照常受理
    finally:
        collector.close()
    store2 = BatchMemoryStore()
    service2 = durable(store2)
    collector2 = BatchAdmissionCollector(
        service2, max_wait_seconds=0.005, request_timeout_seconds=0.5,
        close_timeout_seconds=0.05)
    try:
        assert collector2.accept(_admission_request("1-1")).arrival_seq == 1
        snapshot = collector2.snapshot()
        assert snapshot.durable_depth is None
        assert snapshot.durable_tier is None
        assert snapshot.durable_tier_events == 0
    finally:
        collector2.close()
