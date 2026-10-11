# -*- coding: utf-8 -*-
"""P0-T7 红测：恢复指标聚合 + 故障注入钩子 + Scenario A/B/C 域级模拟。

出处=主窗令（drift 报告批复版 T7 项）：
- 指标五项：tombstone 计数/死亡分布/水位跳洞/熔断开启/still_processing
  对账恒等式（accepted == ready + tombstone + still_processing——四计数
  三存储面，静息态失衡=数据不一致需人工）；
- 故障注入四钩子：指定序号永死/60 秒级断连/未知写入/迟到成功；
- Scenario A/B/C 域级模拟（受理物化域，head last_materialized_seq）：
  - A：60 秒断连→熔断开启（仅 ACCEPTED 持久化、水位不推进、零墓碑）
    →窗尽+半开探测→恢复→水位跨全——决策侧水位天然跳洞（主窗令：不碰）；
  - B：未知写入 409→读回对拍自愈（丢失回执=零二次物化）/未确认=重试
    不定永久（零墓碑）；
  - C：单条永死入墓（批内 1 坏件不杀整批、水位跨 READY+TOMBSTONE）+
    迟到成功→墓碑权威补正（CAS 翻 tombstoned，不删除不复活不二次物化，
    墓态件不进召回候选）。

base=ElasticsearchBatchStore+FakeESClient（真实逐条解析面形态——
故障注入器条件挂接其上；墓簿扫描走 ⑧同一证据源 scan 面）。
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from news_flash_dedup.admission import CONTROL_INDEX, REQUEST_INDEX
from news_flash_dedup.batch_admission import (
    BatchAdmissionCoordinator, BatchLimits, HEAD_ID,
)
from news_flash_dedup.batch_es_store import ElasticsearchBatchStore
from news_flash_dedup.es_admission_schema import day_index
from news_flash_dedup.materialize_runtime import (
    MaterializeDeferred, MaterializeRetryPolicy, MaterializeRetryRuntime,
)
from news_flash_dedup.materialize_terminal import (
    MaterializationTombstoneV1,
    reconcile_late_materialization,
    scan_terminal_tombstones,
)
from news_flash_dedup.recovery_faults import RecoveryFaultInjector
from news_flash_dedup.recovery_metrics import recovery_metrics

from b4_fake_es import FakeESClient
from test_admission import request
from test_batch_admission import NOW

PREFIX = "p01-batch-t7-"
DAY = "2026-09-24"
SCOPE = "default"
ITEMS = day_index(DAY)


class FakeClock:
    """注入时钟（浮点秒域——断连窗/熔断计时共用）。"""

    def __init__(self, start: float = 0.0) -> None:
        self.now = float(start)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _tombstone(seq: int, *, scope_id: str = SCOPE,
               business_date: str = DAY) -> MaterializationTombstoneV1:
    return MaterializationTombstoneV1(
        scope_id=scope_id, business_date=business_date, arrival_seq=seq,
        record_id=f"{seq:064x}", item_id=f"t7-{seq}",
        raw_hash=f"{seq + 1:064x}",
        failed_stage="materialize_bulk", error_class="item_permanent",
        error_code="http_400", error_summary="injected permanent rejection",
        attempt_count=1, first_failed_at="2026-09-24T01:00:00.000000Z",
        last_failed_at="2026-09-24T01:00:00.000000Z", retryable=False,
        pipeline_version="v1", recorded_at="2026-09-24T01:00:00.000000Z",
    )


def _port(client: FakeESClient):
    """⑧同一终态证据源端口（物理前缀经端口包装）。"""
    return lambda index, body: client.search(index=PREFIX + index, body=body)


def _stack(clock=None):
    client = FakeESClient()
    base = ElasticsearchBatchStore(client, index_prefix=PREFIX)
    injector = RecoveryFaultInjector(base, clock=clock)
    return client, base, injector


def _service(store, runtime=None):
    return BatchAdmissionCoordinator(
        store, owner_id="writer-1", owner_isolated=lambda: True,
        limits=BatchLimits(), clock=lambda: NOW, retry_runtime=runtime,
    )


def _accept(service, count, prefix="s"):
    return service.accept_batch([
        request(request_id=f"{prefix}{n}-1", item_id=f"{prefix}{n}-1")
        for n in range(1, count + 1)])


def _head(base) -> dict:
    return base.get(CONTROL_INDEX, HEAD_ID)["source"]


def _metrics(base, client, runtime=None) -> dict:
    return recovery_metrics(
        _head(base), scan_terminal_tombstones(_port(client), SCOPE, DAY),
        breaker_opens=runtime.breaker.open_count if runtime is not None else 0,
    )


def _recall_candidate_ids(client: FakeESClient) -> list[str]:
    """召回登记扫描（recall/worker.py:253 同形 filter）候选集。"""
    hits = client.search(index=PREFIX + ITEMS, body={"query": {"bool": {
        "filter": [{"term": {"scope_id": SCOPE}},
                   {"terms": {"task_state": ["accepted", "running"]}}]}}})
    return [hit["_id"] for hit in hits["hits"]["hits"]]


# ---------- ⑧墓簿全文扫描面：scan_terminal_tombstones ----------

def test_scan_tombstones_enumerates_validated_entries():
    client, base, _ = _stack()
    from news_flash_dedup.materialize_terminal import persist_tombstone
    persist_tombstone(base, _tombstone(3))
    persist_tombstone(base, _tombstone(7))
    persist_tombstone(base, _tombstone(5, scope_id="other"))      # 他域
    persist_tombstone(base, _tombstone(9, business_date="2026-09-25"))
    entries = scan_terminal_tombstones(_port(client), SCOPE, DAY)
    assert [t.arrival_seq for t in entries] == [3, 7]
    assert entries[0].error_class == "item_permanent"
    assert entries[0].record_id == f"{3:064x}"


class _StatusError(RuntimeError):
    def __init__(self, status: int) -> None:
        super().__init__(f"http {status}")
        self.status_code = status


def test_scan_tombstones_404_empty_other_errors_raise():
    def _raising(status: int):
        def search(index, body):
            raise _StatusError(status)
        return search

    assert scan_terminal_tombstones(_raising(404), SCOPE, DAY) == []
    with pytest.raises(RuntimeError):
        scan_terminal_tombstones(_raising(503), SCOPE, DAY)


def test_scan_tombstones_malformed_entries_are_skipped():
    # 畸形条目（缺字段/非映射源/非映射条目）不中断聚合——按不识别跳过。
    response = {"hits": {"hits": [
        {"_id": "a", "_source": {"scope_id": SCOPE, "arrival_seq": 5}},
        {"_id": "b", "_source": "not-a-dict"},
        {"_id": "c", "not_source": {}},
        "not-a-hit",
    ]}}
    assert scan_terminal_tombstones(
        lambda index, body: response, SCOPE, DAY) == []


# ---------- 指标聚合纯函数：recovery_metrics ----------

def _head_dict(allocated, materialized, pending=(), quarantine=None):
    batches = [{"entries": [{"arrival_seq": seq} for seq in batch]}
               for batch in pending]
    head = {"last_allocated_seq": allocated,
            "last_materialized_seq": materialized,
            "pending": {"version": "batch-v1", "batches": batches}}
    if quarantine is not None:
        head["checkpoint"] = {"batch_quarantine": quarantine}
    return head


def test_metrics_happy_path_with_tombstone_skip():
    metrics = recovery_metrics(_head_dict(10, 10), [_tombstone(3)])
    assert metrics["accepted"] == 10
    assert metrics["ready"] == 9
    assert metrics["tombstone_count"] == 1
    assert metrics["still_processing"] == 0
    assert metrics["accounting_balanced"] is True
    assert metrics["watermark_gap_skips"] == {
        "count": 1, "seqs": [3], "tombstone": 1, "foreign": 0}
    assert metrics["death_distribution"]["by_error_code"] == {"http_400": 1}
    assert metrics["death_distribution"]["by_failed_stage"] == {
        "materialize_bulk": 1}
    assert metrics["death_distribution"]["by_attempt_count"] == {1: 1}
    assert metrics["breaker_opens"] == 0


def test_metrics_mid_flight_transition_is_visible_imbalance():
    # 墓碑已落（7）、批未收缩（6..10 在 pending）：恒等式失衡=中间态可见
    # （四计数三存储面——失衡即信号，非构造恒真）。
    metrics = recovery_metrics(
        _head_dict(10, 5, pending=[[6, 7, 8, 9, 10]]), [_tombstone(7)])
    assert (metrics["ready"], metrics["tombstone_count"],
            metrics["still_processing"]) == (5, 1, 5)
    assert metrics["accounting_balanced"] is False


def test_metrics_quarantine_counts_in_still_processing():
    # 隔离批序号区间（1..8）计入 still_processing——过期隔离静息态恒等。
    quarantine = {"first_seq": 1, "last_seq": 8, "reason": "logical_expiry"}
    metrics = recovery_metrics(_head_dict(8, 0, quarantine=quarantine), [])
    assert (metrics["still_processing"], metrics["quarantined_items"]) == (8, 8)
    assert metrics["accounting_balanced"] is True


def test_metrics_foreign_gap_skips_split_and_window_filter():
    metrics = recovery_metrics(
        _head_dict(10, 6), [_tombstone(2)], foreign_seqs=[4, 99])
    # 外国序号按写路径所有权计入 ready（不因读侧证明扣除）；
    # 99 > accepted 窗口外不计。
    assert metrics["ready"] == 5
    assert metrics["watermark_gap_skips"] == {
        "count": 2, "seqs": [2, 4], "tombstone": 1, "foreign": 1}


def test_metrics_window_and_duplicate_tombstones_counted_once():
    metrics = recovery_metrics(
        _head_dict(10, 10), [_tombstone(3), _tombstone(3), _tombstone(12)])
    assert metrics["tombstone_count"] == 1
    assert metrics["watermark_gap_skips"]["seqs"] == [3]


def test_metrics_rejects_malformed_inputs():
    for head in (None, {}, {"last_allocated_seq": 1},
                 {"last_allocated_seq": "x", "last_materialized_seq": 0},
                 {"last_allocated_seq": 1, "last_materialized_seq": 0,
                  "pending": "no"},
                 {"last_allocated_seq": 1, "last_materialized_seq": 0,
                  "pending": {"version": 1, "batches": "no"}},
                 {"last_allocated_seq": 1, "last_materialized_seq": 0,
                  "pending": {"batches": [{"entries": "no"}]}},
                 {"last_allocated_seq": 1, "last_materialized_seq": 0,
                  "pending": {"batches": []},
                  "checkpoint": {"batch_quarantine": {"first_seq": 2}}},
                 {"last_allocated_seq": 1, "last_materialized_seq": 0,
                  "pending": {"batches": []},
                  "checkpoint": {"batch_quarantine": {
                      "first_seq": 5, "last_seq": 4}}}):
        with pytest.raises(ValueError):
            recovery_metrics(head, [])
    with pytest.raises(ValueError):
        recovery_metrics(_head_dict(1, 0), [], breaker_opens=-1)
    with pytest.raises(ValueError):
        recovery_metrics(_head_dict(1, 0), [], breaker_opens="3")
    with pytest.raises(ValueError):
        recovery_metrics(_head_dict(1, 0), [{"not": "a model"}])


# ---------- 故障注入钩子：装配/校验面 ----------

def test_injector_attaches_classified_face_conditionally():
    # 条件实例挂接（probe FaultStore 同型承重机制）：base 无逐条解析面
    # →注入器不暴露该面（coordinator getattr 探针得 None → legacy 路径）。
    from test_batch_admission import BatchMemoryStore

    bare = RecoveryFaultInjector(BatchMemoryStore())
    assert callable(getattr(bare, "bulk_create_classified", None)) is False
    client, base, _ = _stack()
    attached = RecoveryFaultInjector(base)
    assert callable(getattr(attached, "bulk_create_classified", None)) is True


def test_injector_arm_validation_and_late_delivery_idempotency():
    client, base, injector = _stack()
    with pytest.raises(ValueError):
        injector.arm_permanent_death(["x"])
    with pytest.raises(ValueError):
        injector.arm_permanent_death([0])
    with pytest.raises(ValueError):
        injector.arm_unknown_write([None])
    with pytest.raises(ValueError):
        injector.arm_late_success([-1])
    with pytest.raises(ValueError):
        injector.arm_disconnect(0)
    with pytest.raises(ValueError):
        RecoveryFaultInjector(base).arm_disconnect(60)   # 无注入时钟
    with pytest.raises(ValueError):
        injector.deliver_late_success(0)
    assert injector.deliver_late_success(1) is False     # 无待投递


# ---------- Scenario A：60 秒断连 → 熔断 → 恢复 ----------

def test_scenario_a_disconnect_opens_breaker_then_recovers():
    """60 秒断连窗：受理照常持久化、水位不推进、零墓碑；熔断开（仅
    ACCEPTED）；窗尽+半开探测→物化恢复→水位跨全。决策侧水位天然跳洞
    （主窗令：不碰——本域零改动零断言）。"""
    clock = FakeClock(0.0)
    policy = MaterializeRetryPolicy(
        active_attempt_limit=5, breaker_window_requests=6,
        breaker_min_failures=3, breaker_failure_ratio=0.5,
        breaker_open_seconds=30.0, breaker_half_open_probes=1,
    )
    runtime = MaterializeRetryRuntime(
        policy=policy, clock=clock, random_source=lambda: 0.0)
    client, base, injector = _stack(clock=clock)
    service = _service(injector, runtime=runtime)
    _accept(service, 8, prefix="a")
    injector.arm_disconnect(60.0)

    from news_flash_dedup.admission import AdmissionUnknown
    for step in range(3):                      # t=0/10/20：传输级拒 → 未知
        with pytest.raises(AdmissionUnknown):
            service.materialize_prefix()
        if step < 2:
            clock.advance(10)
    clock.advance(5)                            # t=25：熔断开（3 失败/6 窗）
    with pytest.raises(MaterializeDeferred):
        service.materialize_prefix()            # 闸拒——零 ES 触达
    # 熔断开路期证据：受理事实在、水位零、pending 全量、零墓碑。
    assert injector.calls["bulk"] == 3
    assert injector.calls["disconnect_refused"] == 3
    head = _head(base)
    assert (head["last_allocated_seq"], head["last_materialized_seq"]) == (8, 0)
    assert len(head["pending"]["batches"][0]["entries"]) == 8
    mid = _metrics(base, client, runtime)
    assert mid["breaker_opens"] == 1 and mid["tombstone_count"] == 0
    assert (mid["ready"], mid["still_processing"]) == (0, 8)
    assert mid["accounting_balanced"] is True

    clock.advance(36)                           # t=61：断连窗尽+熔断期满
    assert service.materialize_prefix() == 8
    assert runtime.breaker.state == "closed"    # 半开探测成功 → 闭合
    assert runtime.breaker.open_count == 1
    final = _metrics(base, client, runtime)
    assert (final["accepted"], final["ready"],
            final["still_processing"]) == (8, 8, 0)
    assert final["accounting_balanced"] is True
    assert final["tombstone_count"] == 0
    assert final["watermark_gap_skips"]["count"] == 0
    assert final["breaker_opens"] == 1


# ---------- Scenario B：未知写入（409 → 读回对拍） ----------

def test_scenario_b_lost_ack_readback_self_heals_zero_rematerialization():
    """丢失回执（写入真实成功+409）：读回身份对拍收口——零二次物化。"""
    from news_flash_dedup.admission import AdmissionUnknown

    client, base, injector = _stack()
    service = _service(injector)
    receipts = _accept(service, 4, prefix="b")
    injector.arm_unknown_write([2], written=True)
    assert service.materialize_prefix() == 4
    assert injector.calls["bulk"] == 1           # 一次 bulk 收口（零重物化）
    head = _head(base)
    assert head["last_materialized_seq"] == 4
    assert head["pending"]["batches"] == []
    # item 文档真实在场（丢失回执自愈证据）：
    assert base.get(REQUEST_INDEX, "item:" + receipts[1].record_id) is not None
    metrics = _metrics(base, client)
    assert (metrics["tombstone_count"], metrics["ready"]) == (0, 4)
    assert metrics["accounting_balanced"] is True
    assert metrics["watermark_gap_skips"]["count"] == 0


def test_scenario_b_unconfirmed_unknown_retries_without_tombstone():
    """未确认（读回未命中）：保持重试——未知绝不定永久（零墓碑）。"""
    from news_flash_dedup.admission import AdmissionUnknown

    client, base, injector = _stack()
    service = _service(injector)
    _accept(service, 4, prefix="u")
    injector.arm_unknown_write([3], written=False)
    with pytest.raises(AdmissionUnknown):
        service.materialize_prefix()
    mid = _metrics(base, client)
    assert mid["tombstone_count"] == 0           # 未知≠永久（零墓碑）
    assert (mid["ready"], mid["still_processing"]) == (0, 4)
    assert mid["accounting_balanced"] is True
    injector.disarm()
    assert service.materialize_prefix() == 4     # 重试收敛（409 自愈）
    assert injector.calls["bulk"] == 2
    final = _metrics(base, client)
    assert (final["ready"], final["still_processing"]) == (4, 0)
    assert final["accounting_balanced"] is True


# ---------- Scenario C：单条永死 + 迟到成功（墓碑权威） ----------

def test_scenario_c_pure_death_late_success_tombstone_authoritative():
    """纯死形态入墓（主记录不写）→迟到成功落库→场景 C 补正：
    CAS 翻 tombstoned、墓证不动、零二次物化、墓态件不进召回候选。"""
    client, base, injector = _stack()
    service = _service(injector)
    receipts = _accept(service, 5, prefix="c")
    dead = receipts[2]                           # arrival_seq=3
    injector.arm_late_success([3])
    assert service.materialize_prefix() == 5     # 1 坏件不杀整批
    assert _head(base)["pending"]["batches"] == []

    entries = scan_terminal_tombstones(_port(client), SCOPE, DAY)
    assert [t.arrival_seq for t in entries] == [3]
    tomb = entries[0]
    assert (tomb.error_code, tomb.failed_stage, tomb.attempt_count) == (
        "http_400", "materialize_bulk", 1)
    assert tomb.retryable is False
    assert base.get(ITEMS, dead.record_id) is None      # 纯死：主记录未落
    metrics = _metrics(base, client)
    assert (metrics["tombstone_count"], metrics["ready"],
            metrics["still_processing"]) == (1, 4, 0)
    assert metrics["accounting_balanced"] is True
    assert metrics["watermark_gap_skips"] == {
        "count": 1, "seqs": [3], "tombstone": 1, "foreign": 0}
    assert metrics["death_distribution"]["by_error_code"] == {"http_400": 1}
    bulk_after_death = injector.calls["bulk"]

    # 迟到成功：失联重试最终抵达（主记录迟到落库）。
    assert injector.deliver_late_success(3) is True
    landed = base.get(ITEMS, dead.record_id)["source"]
    assert landed["task_state"] == "accepted"
    assert injector.calls["bulk"] == bulk_after_death     # 零二次物化

    # 场景 C 补正（墓碑权威）：CAS 翻 tombstoned，不删除不复活。
    assert reconcile_late_materialization(
        injector, tomb, main_index=ITEMS) == "corrected"
    assert base.get(ITEMS, dead.record_id)["source"]["task_state"] == "tombstoned"
    assert injector.deliver_late_success(3) is False     # 幂等（不重复投递）
    assert scan_terminal_tombstones(_port(client), SCOPE, DAY)[0] == tomb
    assert _metrics(base, client)["tombstone_count"] == 1

    # 墓态件不进召回候选（worker 扫描词表结构性缺席）。
    candidates = _recall_candidate_ids(client)
    assert dead.record_id not in candidates
    live_ids = {receipt.record_id for receipt in receipts
                if receipt.arrival_seq != 3}
    assert live_ids <= set(candidates)


def test_scenario_c_mixed_form_reconciles_in_batch():
    """混合形态（主记录真实写入+单条 400 永久）：同批即补正——墓碑权威，
    墓态件不进召回候选，批内其余照常。"""
    client, base, injector = _stack()
    service = _service(injector)
    receipts = _accept(service, 5, prefix="m")
    dead = receipts[2]
    injector.arm_permanent_death([3])           # main_record=True 混合形态
    assert service.materialize_prefix() == 5
    assert injector.calls["bulk"] == 1
    record = base.get(ITEMS, dead.record_id)["source"]
    assert record["task_state"] == "tombstoned"  # 同批即时补正
    metrics = _metrics(base, client)
    assert (metrics["tombstone_count"], metrics["ready"]) == (1, 4)
    assert metrics["accounting_balanced"] is True
    candidates = _recall_candidate_ids(client)
    assert dead.record_id not in candidates
    live_ids = {receipt.record_id for receipt in receipts
                if receipt.arrival_seq != 3}
    assert live_ids <= set(candidates)
