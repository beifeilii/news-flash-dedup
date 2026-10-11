"""P1a-T3 物化 claim 循环钉（durable 双写模式 True 侧；零成本内存面）。

设计锚 = log\\P1a-预备设计-2026-10-11.md §1.3（租约/fencing）+§3.3
（claim 驱动物化）+任务卡 3.4（P0 场景 A/B/C 任务形态复跑/崩溃接管
矩阵钉 6.2 单元面）：

- claim→四文档 bulk→逐条收口（409 读回/瞬态 defer/永久 terminalize）
  →last_terminal 终态证据推进（含过渡别名平移）；
- T2 条目账任务文档化（retry_count/next_retry_at/失败证据五件；
  全新协调器续驱=重启零漂移）；
- 熔断闸挂 claim 循环（开路=零 ES 触达/受理照常/水位不推进）；
- 逻辑到期→EXPIRED 终态（不重物化/单调序号保留）；
- 1 坏件不杀整轮（item_permanent 入墓+他件照常收口，水位跨
  READY+TOMBSTONE 推进）；
- fencing：A 崩溃→B 接管（generation 再+1）→A 复活三路写必败
  WorkLeaseLost；
- 账式恒等：accepted == ready + tombstone + still_processing
  （recovery_metrics 游标头对账）；
- 形态卫兵：legacy 头文档进 claim 循环=AdmissionConflict。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from news_flash_dedup.admission import (
    AdmissionConflict, AdmissionUnknown, CONTROL_INDEX, IdentityConflict,
)
from news_flash_dedup.batch_admission import (
    HEAD_ID, BatchAdmissionCoordinator, BatchLimits, _pending_view)
from news_flash_dedup.batch_es_store import ElasticsearchBatchStore
from news_flash_dedup.materialize_runtime import (
    MaterializeDeferred, MaterializeRetryRuntime)
from news_flash_dedup.materialize_terminal import (
    load_tombstone, reconcile_late_materialization)
from news_flash_dedup.recovery_metrics import recovery_metrics
from news_flash_dedup.work_queue import (
    WorkLeaseLost, claim_work_item, complete_work_item, defer_work_item,
    load_work_item, terminalize_work_item)

from b4_fake_es import FakeESClient
from test_admission import request
from test_batch_admission import (
    BatchMemoryStore, ClassifiedBatchMemoryStore, coordinator)
from test_p0_t7_faults_metrics import FakeClock


NOW = datetime(2026, 9, 24, 0, 0, tzinfo=timezone.utc)
BUSINESS_DATE = "2026-09-24"


def durable(store, owner="writer-1", when=NOW, runtime=None):
    return BatchAdmissionCoordinator(
        store, owner_id=owner, owner_isolated=lambda: True,
        limits=BatchLimits(), clock=lambda: when,
        retry_runtime=runtime, use_durable_queue=True,
    )


def head_source(store):
    return store.get(CONTROL_INDEX, HEAD_ID)["source"]


def accept(service, count, prefix=""):
    return service.accept_batch([
        request(request_id=f"{prefix}{n}-1", item_id=f"{prefix}{n}-1",
                text=f"正文{prefix}{n}")
        for n in range(1, count + 1)
    ])


# ---------- 基础链：claim→四文档→ready→last_terminal ----------


def test_claim_loop_materializes_all_four_documents_per_item():
    store = ClassifiedBatchMemoryStore()
    service = durable(store)
    accept(service, 3)
    assert service.recover() == 3
    source = head_source(store)
    assert source["last_terminal"] == 3
    # P1a-T4：过渡别名摘除（游标写点剥旧形字段；新头不落）。
    assert "last_materialized_seq" not in source
    assert "pending" not in source
    for seq in (1, 2, 3):
        item = load_work_item(store, "default", BUSINESS_DATE, seq)
        assert item.task_state == "ready"
        assert item.materialize_state == "ready"
        assert item.result is None                   # 占位——commit 域镜像
        main = store.get("news-dedup-items-v1-2026.09.24", item.record_id)
        assert main["source"]["task_state"] == "accepted"
        assert main["source"]["arrival_seq"] == seq
        seq_map = store.get("news-dedup-requests-v1", "seq:" + str(seq))
        assert seq_map["source"]["kind"] == "seq"
        assert seq_map["source"]["record_id"] == item.record_id


def test_recover_is_bounded_round_drain():
    store = ClassifiedBatchMemoryStore()
    service = durable(store)
    accept(service, 8)
    assert service.recover() == 8
    assert service.recover() == 8          # 幂等排空
    assert service.materialize_oldest() == 8
    assert service.materialize_prefix() == 8


def test_legacy_bulk_store_path_materializes_without_classified_face():
    # 无 bulk_create_classified 的 store（BatchMemoryStore）：整批
    # bulk_create+全量读回对拍收口（行为等价，逐条分类面缺席）。
    store = BatchMemoryStore()
    service = durable(store)
    accept(service, 2)
    assert service.recover() == 2
    assert load_work_item(
        store, "default", BUSINESS_DATE, 1).task_state == "ready"


def test_claim_round_bounds_at_32_items_per_round():
    store = ClassifiedBatchMemoryStore()
    service = durable(store)
    for start in range(0, 40, 8):          # 5 批×8（批件上限）
        accept(service, 8, prefix=f"{start}-")
    first = service.materialize_oldest()   # 单轮 ≤32 件
    assert first == 32
    assert service.recover() == 40         # 排空驱动收敛


# ---------- 瞬态 defer（T2 条目账任务文档化） ----------


def test_transient_failure_defers_with_persisted_retry_account():
    store = ClassifiedBatchMemoryStore()
    store.item_statuses = {0: 503}         # 件 1 主记录文档位 → 瞬态
    runtime = MaterializeRetryRuntime(
        clock=lambda: 0.0, random_source=lambda: 0.5)
    service = durable(store, runtime=runtime)
    accept(service, 1)
    assert service.recover() == 0          # 未收口（retry_wait 在案）
    item = load_work_item(store, "default", BUSINESS_DATE, 1)
    assert item.task_state == "retry_wait"
    assert item.retry_count == 1
    assert item.next_retry_at is not None
    assert item.last_failure_class == "transient_infra"
    assert item.last_error_code == "http_503"
    assert item.first_failed_at is not None
    assert item.lease_owner is None and item.lease_expires_at is None
    # 退避到期前 claim=waiting（零写观察面）
    claim = claim_work_item(store, "default", BUSINESS_DATE, 1,
                            owner_id="writer-1", now=NOW)
    assert claim.outcome == "waiting"
    # 到期后重试成功（失败账不清零——审计位保留）
    store.item_statuses = {}
    later = durable(store, when=NOW + timedelta(seconds=60))
    assert later.recover() == 1
    item = load_work_item(store, "default", BUSINESS_DATE, 1)
    assert item.task_state == "ready"
    assert item.retry_count == 1


def test_defer_account_survives_coordinator_restart():
    # T2 条目账进程内存 dict→任务文档字段：全新协调器（新 runtime）读账
    # 续驱，重启零漂移。
    store = ClassifiedBatchMemoryStore()
    store.item_statuses = {0: 503}
    service = durable(store)
    accept(service, 1)
    assert service.recover() == 0
    store.item_statuses = {}
    fresh = durable(store, when=NOW + timedelta(seconds=60))
    assert fresh.recover() == 1
    assert load_work_item(
        store, "default", BUSINESS_DATE, 1).task_state == "ready"


# ---------- UNKNOWN_WRITE 读回收口 ----------


def test_unknown_write_resolves_by_readback():
    # 写入真实发生+回执丢失（409）：读回对拍自愈收口，零重物化。
    store = ClassifiedBatchMemoryStore()
    service = durable(store)
    accept(service, 1)
    item = load_work_item(store, "default", BUSINESS_DATE, 1)
    main_index, main_key, main_body = BatchAdmissionCoordinator._documents(
        [_pending_view(item)])[0]
    store.create(main_index, main_key, main_body)    # 预落主记录（丢回执）
    store.item_statuses = {0: 409}                   # 回执丢失形态
    assert service.recover() == 1
    reloaded = load_work_item(store, "default", BUSINESS_DATE, 1)
    assert reloaded.task_state == "ready"
    assert reloaded.retry_count == 0                 # 未知零计数


def test_unknown_write_unresolved_defers_zero_count_immediate():
    store = ClassifiedBatchMemoryStore()
    store.item_statuses = {0: 409}         # 未知写且读回未命中
    service = durable(store)
    accept(service, 1)
    assert service.recover() == 0
    item = load_work_item(store, "default", BUSINESS_DATE, 1)
    assert item.task_state == "retry_wait"
    assert item.retry_count == 0           # 零计数
    assert item.next_retry_at is not None  # 立即读回（当前时刻）
    store.item_statuses = {}
    later = durable(store, when=NOW + timedelta(seconds=1))
    assert later.recover() == 1


def test_readback_divergence_is_identity_corruption_fail_closed():
    store = ClassifiedBatchMemoryStore()
    service = durable(store)
    accept(service, 1)
    item = load_work_item(store, "default", BUSINESS_DATE, 1)
    divergent = {
        "scope_id": "default", "request_id": "1-1", "item_id": "1-1",
        "record_id": item.record_id, "schema_version": "1",
        "pipeline_version": "v1", "embedding_space_id": "v3",
        "business_date": BUSINESS_DATE, "arrival_seq": 1,
        "received_at": "2026-09-23T15:59:00.000000Z",
        "accepted_at": "2026-09-24T00:00:00.000000Z",
        "expires_at": "2026-10-01T00:00:00.000000Z",
        "text": "被篡改的正文", "raw_hash": "0" * 64,
        "task_state": "accepted", "delivery_state": "not_ready",
        "result": None,
        "diagnostics": {"delivery": {"route_ref": "audit-route-v1",
                                     "trace_id": "trace-first"}},
    }
    store.create("news-dedup-items-v1-2026.09.24",
                 item.record_id, divergent)
    store.item_statuses = {0: 409}         # 未知写 → 读回对拍撞分歧
    with pytest.raises(IdentityConflict):
        service.recover()
    # fail-closed：任务停在持约态（不 defer 不 complete——结构损坏直抛）
    assert load_work_item(
        store, "default", BUSINESS_DATE, 1).task_state == "leased"


# ---------- item_permanent 入墓（1 坏件不杀整轮） ----------


def test_permanent_failure_tombstones_one_item_not_the_round():
    store = ClassifiedBatchMemoryStore()
    # 件 1 四文档位 0..3；件 2 主记录=位 4、request=5、item=6。
    # 注入位 6（item 映射文档 400）——镜像 T7 注入器同型（混合形态：
    # 主记录照写+墓碑权威补正翻 tombstoned）。
    store.item_statuses = {6: 400}
    service = durable(store)
    accept(service, 2)
    assert service.recover() == 2
    first = load_work_item(store, "default", BUSINESS_DATE, 1)
    second = load_work_item(store, "default", BUSINESS_DATE, 2)
    assert first.task_state == "ready"      # 1 坏件不杀整轮
    assert second.task_state == "tombstoned"
    assert second.terminal_reason == "http_400"
    # 墓碑在案（on-demand：raw_hash 与任务文档同源）
    tomb = load_tombstone(store, "default", BUSINESS_DATE, 2)
    assert tomb is not None and tomb.error_code == "http_400"
    assert tomb.raw_hash == second.raw_hash
    # 混合形态补正：主记录已落且被墓碑权威翻 tombstoned
    main = store.get("news-dedup-items-v1-2026.09.24", second.record_id)
    assert main["source"]["task_state"] == "tombstoned"
    # 水位跨 READY+TOMBSTONE 推进
    assert head_source(store)["last_terminal"] == 2


# ---------- 逻辑到期 → EXPIRED ----------


def test_logical_expiry_terminalizes_without_rematerialization():
    store = ClassifiedBatchMemoryStore()
    service = durable(store)
    accept(service, 1)
    # 受理即过期（D+7 之外；expires=2026-09-30T16:00Z 业务区零点）
    week_later = NOW + timedelta(days=7, seconds=1)
    expired_service = durable(store, when=week_later)
    assert expired_service.recover() == 1
    item = load_work_item(store, "default", BUSINESS_DATE, 1)
    assert item.task_state == "expired"
    assert item.terminal_reason == "logical_expiry"
    # 不重物化：主记录零落
    assert store.get(
        "news-dedup-items-v1-2026.09.24", item.record_id) is None
    assert head_source(store)["last_terminal"] == 1    # 单调序号保留


# ---------- 熔断闸（受理照常/零水位推进） ----------


def test_breaker_open_defers_materialization_but_accepts():
    store = ClassifiedBatchMemoryStore()
    clock = FakeClock(0.0)
    runtime = MaterializeRetryRuntime(
        clock=clock, random_source=lambda: 0.0)
    service = durable(store, runtime=runtime)
    accept(service, 1)
    # 人为开闸：窗内 5 失败（比率 100% ≥ 50%）
    for _ in range(5):
        runtime.record_request_failure()
    assert runtime.breaker.state == "open"
    assert runtime.breaker.open_count >= 1
    with pytest.raises(MaterializeDeferred):
        service.materialize_oldest()
    # 受理照常（闸只挂物化写路径）
    service.accept_batch([
        request(request_id="2-1", item_id="2-1", text="正文2")])
    # 开路期零 ES 触达（物化面直接拒启——水位不动）
    assert head_source(store)["last_terminal"] == 0
    # 开路期满（30s）→ 半开探测 → 物化照常收敛
    clock.advance(31.0)
    assert service.recover() == 2


# ---------- fencing 崩溃接管（钉 6.2 单元面） ----------


def test_crash_takeover_fencing_old_owner_writes_fail():
    from news_flash_dedup.materialize_failure import (
        FailureClass, MaterializeFailure)

    store = ClassifiedBatchMemoryStore()
    service = durable(store)
    accept(service, 1)
    # A 持约后崩溃（租约 60s）
    claim_a = claim_work_item(store, "default", BUSINESS_DATE, 1,
                              owner_id="writer-a", now=NOW)
    assert claim_a.outcome == "claimed"
    # B 于租约到期后接管（claim 顺带 recover——generation 再+1；
    # 过期租约接管路径的 outcome="claimed"，代次连续性为准据）
    later = NOW + timedelta(seconds=61)
    claim_b = claim_work_item(store, "default", BUSINESS_DATE, 1,
                              owner_id="writer-b", now=later)
    assert claim_b.outcome == "claimed"
    assert claim_b.lease_generation == claim_a.lease_generation + 1
    # A 复活三路写全部必败（WorkLeaseLost——僵尸放弃）
    with pytest.raises(WorkLeaseLost):
        complete_work_item(
            store, "default", BUSINESS_DATE, 1, result=None,
            owner_id="writer-a", lease_generation=claim_a.lease_generation,
            now=later)
    failure = MaterializeFailure(
        failure_class=FailureClass.TRANSIENT_INFRA, error_code="http_503",
        detail="connection timed out")
    with pytest.raises(WorkLeaseLost):
        defer_work_item(
            store, "default", BUSINESS_DATE, 1, failure=failure,
            delay_seconds=1.0, definite=True, owner_id="writer-a",
            lease_generation=claim_a.lease_generation, now=later)
    with pytest.raises(WorkLeaseLost):
        terminalize_work_item(
            store, "default", BUSINESS_DATE, 1, task_state="tombstoned",
            terminal_reason="http_400", owner_id="writer-a",
            lease_generation=claim_a.lease_generation, now=later)


def test_claim_loop_recovers_expired_lease_mid_round():
    # 崩溃残约：A 崩溃后受理协调器（writer-1）claim 顺带接管续驱。
    store = ClassifiedBatchMemoryStore()
    service = durable(store, owner="writer-1")
    accept(service, 1)
    claim_work_item(store, "default", BUSINESS_DATE, 1,
                    owner_id="writer-a", now=NOW)
    later = NOW + timedelta(seconds=61)
    service_b = durable(store, owner="writer-1", when=later)
    assert service_b.recover() == 1
    item = load_work_item(store, "default", BUSINESS_DATE, 1)
    assert item.task_state == "ready"


# ---------- P0 场景 A/B/C 任务形态（T7 注入器复跑） ----------


def test_scenario_a_disconnect_unknown_then_recover():
    # 场景 A（断连）：bulk 请求级 OSError → AdmissionUnknown；断连期
    # 零水位；窗过+租约到期后接管收敛。
    from news_flash_dedup.recovery_faults import RecoveryFaultInjector

    clock = FakeClock(0.0)
    base = ElasticsearchBatchStore(
        FakeESClient(), index_prefix="p01-batch-t3disc-")
    store = RecoveryFaultInjector(base, clock=clock)
    store.arm_disconnect(30.0)
    service = durable(store)
    accept(service, 2)
    with pytest.raises(AdmissionUnknown):
        service.recover()
    assert head_source(store)["last_terminal"] == 0    # 断连期零水位
    assert store.calls["disconnect_refused"] >= 1
    clock.advance(31.0)                                # 断连窗过
    later = durable(store, when=NOW + timedelta(seconds=61))
    assert later.recover() == 2
    assert head_source(store)["last_terminal"] == 2


def test_scenario_b_permanent_death_tombstones_and_advances():
    # 场景 B（单件永久死）：T7 注入器 item 文档 400 → 墓碑+tombstoned+
    # 他件照常（水位跨墓推进）。
    from news_flash_dedup.recovery_faults import RecoveryFaultInjector

    base = ElasticsearchBatchStore(
        FakeESClient(), index_prefix="p01-batch-t3death-")
    store = RecoveryFaultInjector(base)
    store.arm_permanent_death([2])
    service = durable(store)
    accept(service, 3)
    assert service.recover() == 3
    states = [load_work_item(store, "default", BUSINESS_DATE, n).task_state
              for n in (1, 2, 3)]
    assert states == ["ready", "tombstoned", "ready"]
    tomb = load_tombstone(store, "default", BUSINESS_DATE, 2)
    assert tomb is not None and tomb.error_code == "http_400"
    # 混合形态：主记录照写且被墓碑权威补正翻 tombstoned
    item = load_work_item(store, "default", BUSINESS_DATE, 2)
    main = base.get("news-dedup-items-v1-2026.09.24", item.record_id)
    assert main["source"]["task_state"] == "tombstoned"
    assert head_source(store)["last_terminal"] == 3


def test_scenario_c_late_success_correction_wins_tombstone_authority():
    # 场景 C（迟到成功）：纯死形态入墓后主记录迟到落库——墓碑权威补正
    # 翻 tombstoned（不删除不复活不二次物化）。
    from news_flash_dedup.recovery_faults import RecoveryFaultInjector

    base = ElasticsearchBatchStore(
        FakeESClient(), index_prefix="p01-batch-t3late-")
    store = RecoveryFaultInjector(base)
    store.arm_late_success([1])
    service = durable(store)
    accept(service, 2)
    assert service.recover() == 2
    item = load_work_item(store, "default", BUSINESS_DATE, 1)
    assert item.task_state == "tombstoned"
    main_index = "news-dedup-items-v1-2026.09.24"
    assert base.get(main_index, item.record_id) is None   # 纯死形态
    assert store.deliver_late_success(1) is True
    landed = base.get(main_index, item.record_id)
    assert landed["source"]["task_state"] == "accepted"
    tomb = load_tombstone(store, "default", BUSINESS_DATE, 1)
    assert reconcile_late_materialization(
        base, tomb, main_index=main_index) == "corrected"
    assert base.get(main_index, item.record_id)[
        "source"]["task_state"] == "tombstoned"


# ---------- 账式恒等（recovery_metrics 游标头对账） ----------


def test_accounting_identity_balances_on_cursor_head():
    store = ClassifiedBatchMemoryStore()
    store.item_statuses = {6: 400}         # 件 2 永久死（item 映射位）
    service = durable(store)
    accept(service, 2)
    assert service.recover() == 2
    metrics = recovery_metrics(head_source(store), [
        load_tombstone(store, "default", BUSINESS_DATE, 2)],
        active_work_items=0)
    assert metrics["accepted"] == 2
    assert metrics["ready"] == 1
    assert metrics["tombstone_count"] == 1
    assert metrics["still_processing"] == 0
    assert metrics["accounting_balanced"] is True
    # in-flight 中段对账
    store2 = ClassifiedBatchMemoryStore()
    svc = durable(store2)
    accept(svc, 2)
    mid = recovery_metrics(head_source(store2), [], active_work_items=2)
    assert mid["accepted"] == 2
    assert mid["still_processing"] == 2
    assert mid["accounting_balanced"] is True


# ---------- commit 腿 last_decision_seq 镜像（蓝图 1.2.1/七-3） ----------


def test_mirror_decision_seq_advances_cursor_head_monotonically():
    from news_flash_dedup.batch_admission import mirror_decision_seq

    store = ClassifiedBatchMemoryStore()
    service = durable(store)
    accept(service, 3)
    assert mirror_decision_seq(
        store, "default", BUSINESS_DATE, 2, clock=lambda: NOW) == "advanced"
    source = head_source(store)
    assert source["last_decision_seq"] == 2
    assert source["last_terminal"] == 0        # 镜像零触碰物化游标
    # 重放幂等（同值）+ 单调（回退）
    assert mirror_decision_seq(
        store, "default", BUSINESS_DATE, 2, clock=lambda: NOW) == "current"
    assert mirror_decision_seq(
        store, "default", BUSINESS_DATE, 1, clock=lambda: NOW) == "current"
    assert mirror_decision_seq(
        store, "default", BUSINESS_DATE, 5, clock=lambda: NOW) == "advanced"
    assert head_source(store)["last_decision_seq"] == 5


def test_mirror_decision_seq_legacy_head_and_absent_are_noops():
    from news_flash_dedup.batch_admission import mirror_decision_seq

    legacy = BatchMemoryStore()
    coordinator(legacy).accept_batch([request()])
    assert mirror_decision_seq(
        legacy, "default", BUSINESS_DATE, 5,
        clock=lambda: NOW) == "legacy_form"
    assert "last_decision_seq" not in legacy.get(
        CONTROL_INDEX, HEAD_ID)["source"]        # legacy 头零污染
    assert mirror_decision_seq(
        ClassifiedBatchMemoryStore(), "default", BUSINESS_DATE, 5,
        clock=lambda: NOW) == "absent"           # 无头不代建


def test_mirror_decision_seq_lag_tolerated_on_cas_contention():
    from news_flash_dedup.batch_admission import mirror_decision_seq
    from test_admission import Conflict

    class _ContendedReplace:
        def __init__(self, inner):
            self._inner = inner

        def get(self, index, key):
            return self._inner.get(index, key)

        def create(self, index, key, body):
            return self._inner.create(index, key, body)

        def replace(self, index, key, body, seq_no, primary_term):
            raise Conflict()                     # 持续竞争超界

        def is_conflict(self, error):
            return isinstance(error, Conflict)

    store = ClassifiedBatchMemoryStore()
    service = durable(store)
    accept(service, 1)
    contended = _ContendedReplace(store)
    assert mirror_decision_seq(
        contended, "default", BUSINESS_DATE, 9,
        clock=lambda: NOW) == "lagged"           # 滞后容忍（不抛）
    assert head_source(store)["last_decision_seq"] == 0


# ---------- 形态卫兵 ----------


def test_claim_round_refuses_legacy_form_head():
    store = BatchMemoryStore()
    coordinator(store).accept_batch([request()])
    with pytest.raises(AdmissionConflict, match="not in cursor form"):
        durable(store).materialize_oldest()
