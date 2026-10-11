from __future__ import annotations

from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from threading import Barrier, Event, Thread
from time import monotonic

import pytest

from news_flash_dedup import batch_admission
from news_flash_dedup.admission import (
    AdmissionConflict, AdmissionReceipt, AdmissionUnknown, IdentityConflict,
)
from news_flash_dedup.batch_admission import BatchAdmissionCoordinator, BatchLimits
from news_flash_dedup.batch_collector import BatchAdmissionCollector, _request_size
from test_admission import request
from test_batch_admission import BatchMemoryStore, NOW


class CountingStore(BatchMemoryStore):
    def __init__(self):
        super().__init__()
        self.admission_cas = 0
        self.replace_calls = 0

    def replace(self, index, key, body, seq_no, primary_term):
        self.replace_calls += 1
        if (key == "batch_admission_head" and body["last_allocated_seq"] >
                self.docs[(index, key)]["source"]["last_allocated_seq"]):
            self.admission_cas += 1
        return super().replace(index, key, body, seq_no, primary_term)


class BlockingStore(CountingStore):
    def __init__(self, stage="before_cas"):
        super().__init__()
        self.stage = stage
        self.entered = Event()
        self.release = Event()

    def replace(self, index, key, body, seq_no, primary_term):
        if self.stage == "after_cas":
            super().replace(index, key, body, seq_no, primary_term)
        self.entered.set()
        assert self.release.wait(2)
        if self.stage == "before_cas":
            return super().replace(index, key, body, seq_no, primary_term)


class PausedCollector(BatchAdmissionCollector):
    def __init__(self, *args, **kwargs):
        self.start_worker = Event()
        super().__init__(*args, **kwargs)

    def _run(self):
        assert self.start_worker.wait(2)
        super()._run()


class RecordingCoordinator:
    def __init__(self, service, action=None):
        self.service = service
        self.limits = service.limits
        self.action = action
        self.calls = []
        self.entered = Event()
        self.release = Event()

    def accept_batch(self, requests):
        self.calls.append(list(requests))
        if self.action is not None:
            return self.action(self, requests)
        return self.service.accept_batch(requests)


class Call:
    def __init__(self, collector, value, gate=None):
        self.result = None
        self.done = Event()

        def run():
            try:
                if gate is not None:
                    gate.wait(timeout=1)
                self.result = collector.accept(value)
            except BaseException as error:
                self.result = error
            finally:
                self.done.set()

        self.thread = Thread(target=run, daemon=True)
        self.thread.start()

    def get(self):
        assert self.done.wait(1), "accept did not finish within test bound"
        return self.result


@pytest.fixture
def harness():
    collectors, calls, releases = [], [], []

    class Harness:
        def collector(self, service, *, paused=False, configured=True, **kwargs):
            if configured:
                kwargs.setdefault("request_timeout_seconds", 0.5)
                kwargs.setdefault("close_timeout_seconds", 0.03)
            cls = PausedCollector if paused else BatchAdmissionCollector
            collector = cls(service, **kwargs)
            collectors.append(collector)
            if paused:
                releases.append(collector.start_worker)
            return collector

        def call(self, collector, value=None, gate=None):
            call = Call(collector, value or request(), gate)
            calls.append(call)
            return call

        def release_on_exit(self, event):
            releases.append(event)

        def queue(self, collector, values):
            result = []
            for count, value in enumerate(values, 1):
                result.append(self.call(collector, value))
                with collector._condition:
                    assert collector._condition.wait_for(
                        lambda: len(collector._waiting) == count, timeout=0.5,
                    )
            return result

    yield Harness()
    for event in releases:
        event.set()
    for collector in collectors:
        collector.close()
        collector._thread.join(1)
        assert not collector._thread.is_alive()
    for call in calls:
        call.thread.join(1)
        assert not call.thread.is_alive()


def _service(store, limits=None):
    return BatchAdmissionCoordinator(
        store, owner_id="writer-1", owner_isolated=lambda: True,
        limits=limits or BatchLimits(), clock=lambda: NOW,
    )


def _head(store):
    return store.get("news-dedup-control-v1", "batch_admission_head")["source"]


def _assert_rejected(result, reason):
    assert isinstance(result, AdmissionConflict)
    assert type(result).__name__ == "CollectorRejected"
    assert result.reason == reason


def test_eight_independent_accept_calls_share_one_confirmed_cas(harness):
    store = CountingStore()
    collector = harness.collector(_service(store), paused=True, configured=False)
    gate = Barrier(8)
    calls = [harness.call(collector, request(request_id=f"{n}-1", item_id=f"{n}-1"), gate)
             for n in range(8)]
    with collector._condition:
        assert collector._condition.wait_for(lambda: len(collector._waiting) == 8, timeout=0.5)
    collector.start_worker.set()
    receipts = [call.get() for call in calls]
    assert sorted(receipt.arrival_seq for receipt in receipts) == list(range(1, 9))
    assert store.admission_cas == 1
    assert store.bulk_calls == 0


def test_single_accept_flushes_at_wait_bound_without_packing_input():
    store = CountingStore()
    with BatchAdmissionCollector(_service(store), max_wait_seconds=0.03) as collector:
        started = monotonic()
        receipt = collector.accept(request())
        elapsed = monotonic() - started
    assert receipt.arrival_seq == 1
    # 窗口 Z3 宽区间改真断言（机制钉，取代旧 0.02<=elapsed<0.5 固定宽窗）：
    # 1) 下界钉"不早于等待界"——flush 由 _select 的 condition.wait(remaining)
    #    在 queued_at+max_wait_seconds 后触发（src batch_collector.py:211-216），
    #    最早不早于配置界；容 Windows 计时粒度（~15.6ms）取 0.02 容差；
    # 2) 上界钉配置界+调度余量（0.03+0.2，取代旧 0.5 上界）；
    # 3) 对照机制钉：0.09 等待界实测显著更慢（delta≥0.04=0.06 标称差−双程
    #    抖动余量），证明 elapsed 真实跟踪配置界而非巧合落入固定宽窗。
    assert elapsed >= 0.03 - 0.02, (
        f"flush 早于等待界：elapsed={elapsed:.4f} < 0.03-0.02（机制破坏）")
    assert elapsed < 0.03 + 0.2, (
        f"flush 远超等待界+调度余量：elapsed={elapsed:.4f} >= 0.23")
    store_slow = CountingStore()
    with BatchAdmissionCollector(_service(store_slow), max_wait_seconds=0.09) as slow:
        started_slow = monotonic()
        receipt_slow = slow.accept(request())
        elapsed_slow = monotonic() - started_slow
    assert receipt_slow.arrival_seq == 1
    assert elapsed_slow - elapsed >= 0.04, (
        f"等待界未真实生效：0.09 界 elapsed={elapsed_slow:.4f} 与 0.03 界 "
        f"elapsed={elapsed:.4f} 差 {elapsed_slow - elapsed:.4f} < 0.04")


def test_collector_rejects_oversized_request_without_allocating_sequence():
    store = CountingStore()
    with BatchAdmissionCollector(_service(store), max_wait_seconds=0.01,
                                 max_batch_bytes=200) as collector:
        with pytest.raises(AdmissionConflict) as caught:
            collector.accept(request(text="正文" * 100))
    _assert_rejected(caught.value, "request_too_large")
    assert store.get("news-dedup-control-v1", "batch_admission_head") is None


@pytest.mark.parametrize("conflict", ["text", "item", "request", "within_batch"])
def test_mixed_conflict_only_fails_its_own_call_and_preserves_fifo(harness, conflict):
    store = CountingStore()
    service = _service(store)
    if conflict != "within_batch":
        service.accept_batch([request()])
    first = request(request_id="x", item_id="x")
    bad = {"text": request(text="不同正文"), "item": request(item_id="other"),
           "request": request(request_id="other"),
           "within_batch": replace(first, text="不同正文")}[conflict]
    values = [first, bad, request(request_id="z", item_id="z")]
    recording = RecordingCoordinator(service)
    collector = harness.collector(recording, paused=True, configured=False)
    calls = harness.queue(collector, values)
    collector.start_worker.set()
    results = [call.get() for call in calls]
    assert isinstance(results[0], AdmissionReceipt)
    assert isinstance(results[1], IdentityConflict)
    assert isinstance(results[2], AdmissionReceipt)
    first_seq = 1 if conflict == "within_batch" else 2
    assert [results[n].arrival_seq for n in (0, 2)] == [first_seq, first_seq + 1]
    assert recording.calls == [values, *[[value] for value in values]]
    assert _head(store)["last_allocated_seq"] == first_seq + 1
    retry = collector.accept(replace(first, trace_id="retry-trace", pipeline_version="v2"))
    assert retry == results[0] and retry.reused


def test_fifo_receipt_positions_and_pending_materialized_retries(harness):
    store = CountingStore()
    service = _service(store)
    original = service.accept_batch([request()])[0]
    service.materialize_oldest()
    pending_request = request(request_id="pending", item_id="pending")
    pending = service.accept_batch([pending_request])[0]
    fresh = request(request_id="fresh", item_id="fresh")
    values = [replace(request(), trace_id="new", pipeline_version="v2"),
              pending_request, fresh, fresh, request(request_id="last", item_id="last")]
    collector = harness.collector(service, paused=True)
    calls = harness.queue(collector, values)
    collector.start_worker.set()
    results = [call.get() for call in calls]
    assert [r.arrival_seq for r in results] == [1, 2, 3, 3, 4]
    assert [r.reused for r in results] == [True, True, False, True, False]
    assert results[0] == original and results[1] == pending
    assert _head(store)["last_allocated_seq"] == 4


def test_unknown_batch_is_not_split_or_followed_by_more_admission(harness):
    def unknown(recording, values):
        raise AdmissionUnknown("CAS acknowledgement lost")

    recording = RecordingCoordinator(_service(CountingStore()), unknown)
    collector = harness.collector(recording, paused=True)
    values = [request(request_id=str(n), item_id=str(n)) for n in range(3)]
    calls = harness.queue(collector, values)
    collector.start_worker.set()
    assert all(isinstance(call.get(), AdmissionUnknown) for call in calls)
    assert recording.calls == [values]
    with pytest.raises(AdmissionConflict):
        collector.accept(request(request_id="new", item_id="new"))
    assert collector.snapshot().failed


def test_unknown_during_split_stops_remaining_retries(harness):
    def action(recording, values):
        if len(recording.calls) == 1:
            raise IdentityConflict("pre-CAS identity conflict")
        raise AdmissionUnknown("CAS unconfirmed")

    recording = RecordingCoordinator(_service(CountingStore()), action)
    collector = harness.collector(recording, paused=True)
    values = [request(request_id=str(n), item_id=str(n)) for n in range(3)]
    calls = harness.queue(collector, values)
    collector.start_worker.set()
    assert all(isinstance(call.get(), AdmissionUnknown) for call in calls)
    assert recording.calls == [values, [values[0]]]


@pytest.mark.parametrize("stage", ["before_cas", "after_cas"])
def test_store_block_timeout_is_unknown_but_waiting_can_be_atomically_withdrawn(harness, stage):
    store = BlockingStore(stage)
    harness.release_on_exit(store.release)
    collector = harness.collector(_service(store), max_wait_seconds=0.005,
                                  request_timeout_seconds=0.06)
    active = harness.call(collector)
    assert store.entered.wait(0.5)
    assert isinstance(active.get(), AdmissionUnknown)
    snap = collector.snapshot()
    assert snap.inflight_items == 1 and snap.worker_alive
    queued = harness.call(collector, request(request_id="queued", item_id="queued"))
    _assert_rejected(queued.get(), "queue_timeout")
    assert collector.snapshot().waiting_items == 0
    assert collector.snapshot().inflight_items == 1
    store.release.set()
    collector.close()
    collector._thread.join(1)
    assert _head(store)["last_allocated_seq"] == 1
    assert collector.snapshot().inflight_items == 0
    assert isinstance(active.result, AdmissionUnknown)


@pytest.mark.parametrize("quota", ["items", "bytes"])
def test_capacity_includes_active_and_waiting_even_after_caller_timeout(harness, quota):
    store = BlockingStore()
    harness.release_on_exit(store.release)
    size = _request_size(request())
    kwargs = {"max_queue_items": 2} if quota == "items" else {"max_queue_bytes": 2 * size}
    collector = harness.collector(_service(store), max_wait_seconds=0.005,
                                  request_timeout_seconds=0.12, **kwargs)
    active = harness.call(collector)
    assert store.entered.wait(0.5)
    assert isinstance(active.get(), AdmissionUnknown)
    queued = harness.call(collector)
    with collector._condition:
        assert collector._condition.wait_for(lambda: len(collector._waiting) == 1, timeout=0.5)
    with pytest.raises(AdmissionConflict) as caught:
        collector.accept(request())
    _assert_rejected(caught.value, "queue_full")
    snap = collector.snapshot()
    assert snap.waiting_items == snap.inflight_items == 1
    assert snap.waiting_bytes == snap.inflight_bytes == size
    assert snap.peak_total_items == 2 and snap.peak_total_bytes == 2 * size
    collector.close()
    _assert_rejected(queued.get(), "closed")


def test_close_is_bounded_wakes_both_states_and_does_not_claim_isolation(harness):
    store = BlockingStore()
    harness.release_on_exit(store.release)
    collector = harness.collector(_service(store), max_wait_seconds=0.005)
    active = harness.call(collector)
    assert store.entered.wait(0.5)
    queued = harness.call(collector, request(request_id="queued", item_id="queued"))
    with collector._condition:
        assert collector._condition.wait_for(lambda: len(collector._waiting) == 1, timeout=0.5)
    started = monotonic()
    collector.close()
    assert monotonic() - started < 0.3
    assert isinstance(active.get(), AdmissionUnknown)
    _assert_rejected(queued.get(), "closed")
    snap = collector.snapshot()
    assert snap.closed and snap.worker_alive and snap.inflight_items == 1
    with pytest.raises(AdmissionConflict) as caught:
        collector.accept(request())
    _assert_rejected(caught.value, "closed")
    store.release.set()
    collector._thread.join(1)
    assert not collector.snapshot().worker_alive
    assert _head(store)["last_allocated_seq"] == 1
    assert isinstance(active.result, AdmissionUnknown)
    collector.close()


def test_timeout_before_selection_is_definite_and_never_calls_coordinator(harness):
    recording = RecordingCoordinator(_service(CountingStore()))
    collector = harness.collector(recording, paused=True, request_timeout_seconds=0.02)
    _assert_rejected(harness.call(collector).get(), "queue_timeout")
    collector.start_worker.set()
    collector.close()
    assert recording.calls == []


def test_close_before_selection_never_flushes_waiting_requests(harness):
    recording = RecordingCoordinator(_service(CountingStore()))
    collector = harness.collector(recording, paused=True)
    calls = harness.queue(collector, [request(), request()])
    collector.close()
    assert all(isinstance(call.get(), AdmissionConflict) for call in calls)
    collector.start_worker.set()
    collector._thread.join(1)
    assert recording.calls == []


@pytest.mark.parametrize("error", [OSError("read failed"), SystemExit("worker failed"),
                                   AdmissionConflict("owner or durable state differs")])
def test_worker_failure_wakes_active_and_waiting_without_reopening(harness, error):
    def fail(recording, values):
        recording.entered.set()
        assert recording.release.wait(2)
        raise error

    recording = RecordingCoordinator(_service(CountingStore()), fail)
    harness.release_on_exit(recording.release)
    collector = harness.collector(recording, max_wait_seconds=0.005)
    active = harness.call(collector)
    assert recording.entered.wait(0.5)
    queued = harness.call(collector)
    with collector._condition:
        assert collector._condition.wait_for(lambda: len(collector._waiting) == 1, timeout=0.5)
    recording.release.set()
    result = active.get()
    if isinstance(error, AdmissionConflict):
        assert result is error
    else:
        assert isinstance(result, AdmissionUnknown)
        assert result.__cause__ is error
    _assert_rejected(queued.get(), "worker_failed")
    collector._thread.join(0.5)
    assert collector.snapshot().failed and not collector.snapshot().worker_alive
    with pytest.raises(AdmissionConflict) as caught:
        collector.accept(request())
    _assert_rejected(caught.value, "worker_failed")
    assert len(recording.calls) == 1


@pytest.mark.parametrize("response", [[], [None, None], None])
def test_invalid_receipt_response_notifies_everyone_and_stops_worker(harness, response):
    recording = RecordingCoordinator(_service(CountingStore()), lambda _, values: response)
    collector = harness.collector(recording, paused=True)
    calls = harness.queue(collector, [request(), request()])
    collector.start_worker.set()
    assert all(isinstance(call.get(), AdmissionUnknown) for call in calls)
    collector._thread.join(0.5)
    assert collector.snapshot().failed
    assert len(recording.calls) == 1


def test_exception_outside_coordinator_call_wakes_waiting(harness, monkeypatch):
    collector = harness.collector(_service(CountingStore()), paused=True)
    calls = harness.queue(collector, [request(), request()])

    def fail():
        raise RuntimeError("selection failed")

    monkeypatch.setattr(collector, "_select", fail)
    collector.start_worker.set()
    for call in calls:
        _assert_rejected(call.get(), "worker_failed")
    collector._thread.join(0.5)
    assert collector.snapshot().failed


@pytest.mark.parametrize("rejection", ["identity", "capacity"])
def test_timeout_during_conflict_prevents_late_split(harness, rejection):
    def conflict(recording, values):
        recording.entered.set()
        assert recording.release.wait(2)
        if rejection == "capacity":
            raise batch_admission.AdmissionCapacityExceeded("log_item_limit")
        raise IdentityConflict("pre-CAS identity conflict")

    recording = RecordingCoordinator(_service(CountingStore()), conflict)
    harness.release_on_exit(recording.release)
    collector = harness.collector(recording, paused=True, request_timeout_seconds=0.08,
                                  max_wait_seconds=0.005)
    values = [request(), request(text="不同正文")]
    calls = harness.queue(collector, values)
    collector.start_worker.set()
    assert recording.entered.wait(0.5)
    assert all(isinstance(call.get(), AdmissionUnknown) for call in calls)
    recording.release.set()
    collector._thread.join(0.5)
    assert recording.calls == [values]
    assert collector.snapshot().failed


def test_snapshot_is_immutable_aggregate_and_batch_bytes_bound_flush(harness):
    store = CountingStore()
    size = _request_size(request())
    collector = harness.collector(_service(store), paused=True, max_batch_bytes=2 * size)
    calls = harness.queue(collector, [request(), request(), request()])
    before = collector.snapshot()
    assert before.waiting_items == before.peak_waiting_items == 3
    assert before.waiting_bytes == before.peak_waiting_bytes == 3 * size
    # D25 钉值（三轮审计 C-18 族：>=0 系恒真式）：paused 收集器快照与
    # 入队背靠背，oldest_waiting_seconds 必为非负小值——钉区间防负值/
    # 异常大值静默漂移
    assert 0 <= before.oldest_waiting_seconds < 30
    with pytest.raises(FrozenInstanceError):
        before.waiting_items = 9
    collector.start_worker.set()
    assert all(isinstance(call.get(), AdmissionReceipt) for call in calls)
    collector.close()
    after = collector.snapshot()
    assert before.waiting_items == 3
    assert after.waiting_items == after.inflight_items == 0
    assert after.oldest_waiting_seconds == 0
    assert after.peak_inflight_items == 2 and after.peak_inflight_bytes == 2 * size
    assert after.flushed_batches == 2 and after.flushed_items == 3
    assert after.flushed_bytes == 3 * size
    assert after.last_batch_items == 1 and after.last_batch_bytes == size
    assert after.max_batch_items == 2 and after.max_batch_bytes == 2 * size
    assert after.queue_wait_count == 3
    assert after.queue_wait_seconds_total >= after.queue_wait_seconds_max > 0
    assert not any(word in name for name in vars(after) for word in ("request_id", "text", "trace"))


def test_one_timed_out_member_blocks_retry_of_entire_pre_cas_conflict(harness):
    def conflict(recording, values):
        recording.entered.set()
        assert recording.release.wait(2)
        raise IdentityConflict("pre-CAS identity conflict")

    recording = RecordingCoordinator(_service(CountingStore()), conflict)
    harness.release_on_exit(recording.release)
    collector = harness.collector(recording, paused=True, max_wait_seconds=0.005)
    first = harness.queue(collector, [request()])[0]
    collector.request_timeout_seconds = 0.04
    second = harness.call(collector, request(text="不同正文"))
    with collector._condition:
        assert collector._condition.wait_for(lambda: len(collector._waiting) == 2, timeout=0.5)
    collector.start_worker.set()
    assert recording.entered.wait(0.5)
    assert isinstance(second.get(), AdmissionUnknown)
    assert not first.done.is_set()
    recording.release.set()
    assert isinstance(first.get(), AdmissionUnknown)
    assert len(recording.calls) == 1


@pytest.mark.parametrize("extra", [False, True])
def test_wrong_receipt_count_after_real_cas_remains_unknown_without_retry(harness, extra):
    store = CountingStore()

    def action(recording, values):
        receipts = recording.service.accept_batch(values)
        return receipts + receipts[:1] if extra else receipts[:1]

    recording = RecordingCoordinator(_service(store), action)
    collector = harness.collector(recording, paused=True)
    calls = harness.queue(collector, [request(), request(request_id="next", item_id="next")])
    collector.start_worker.set()
    assert all(isinstance(call.get(), AdmissionUnknown) for call in calls)
    collector._thread.join(0.5)
    assert _head(store)["last_allocated_seq"] == 2
    assert len(recording.calls) == 1 and collector.snapshot().failed


def test_unknown_mid_split_preserves_earlier_confirmed_receipt(harness):
    store = CountingStore()

    def action(recording, values):
        if len(recording.calls) == 1:
            raise IdentityConflict("pre-CAS identity conflict")
        if len(recording.calls) == 3:
            raise AdmissionUnknown("CAS unconfirmed")
        return recording.service.accept_batch(values)

    recording = RecordingCoordinator(_service(store), action)
    collector = harness.collector(recording, paused=True)
    values = [request(request_id=str(n), item_id=str(n)) for n in range(3)]
    calls = harness.queue(collector, values)
    collector.start_worker.set()
    results = [call.get() for call in calls]
    assert isinstance(results[0], AdmissionReceipt) and results[0].arrival_seq == 1
    assert all(isinstance(result, AdmissionUnknown) for result in results[1:])
    assert recording.calls == [values, [values[0]], [values[1]]]
    assert _head(store)["last_allocated_seq"] == 1


def test_withdrawn_head_does_not_leave_stale_selection(harness):
    # P0-T6：域层默认 max_wait 0.1→0.02 后，本钉（撤队/陈旧选取语义）
    # 以显式 max_wait_seconds=0.1 保留原前提（单件滞留>30ms 才撤队）——
    # 既有小容量/旧行为构造点同型纪律。
    recording = RecordingCoordinator(_service(CountingStore()))
    collector = harness.collector(recording, max_wait_seconds=0.1,
                                  request_timeout_seconds=0.03)
    first = harness.call(collector)
    with collector._condition:
        assert collector._condition.wait_for(lambda: len(collector._waiting) == 1, timeout=0.5)
    collector.request_timeout_seconds = 0.5
    second = harness.call(collector, request(request_id="next", item_id="next"))
    _assert_rejected(first.get(), "queue_timeout")
    receipt = second.get()
    assert isinstance(receipt, AdmissionReceipt) and receipt.request_id == "next"
    assert receipt.arrival_seq == 1
    assert len(recording.calls) == 1 and len(recording.calls[0]) == 1


@pytest.mark.parametrize("field", ["request_timeout_seconds", "close_timeout_seconds"])
@pytest.mark.parametrize("value", [0, -1, float("inf"), float("nan"), 1e100])
def test_timeouts_must_be_positive_and_finite(field, value):
    with pytest.raises(ValueError):
        with BatchAdmissionCollector(_service(CountingStore()), **{field: value}):
            pass


def _assert_capacity(result, reason):
    assert isinstance(result, batch_admission.AdmissionCapacityExceeded)
    assert not isinstance(result, (IdentityConflict, AdmissionUnknown))
    assert result.reason == reason


@pytest.mark.parametrize("quota,reason", [
    ("items", "log_item_limit"), ("bytes", "log_byte_limit"),
])
def test_full_durable_log_recovers_on_same_collector(harness, quota, reason):
    # P0-T6：域层默认 max_log_items 64→512 后，本钉（满日志边界+恢复）
    # 以显式 limits=64 保留原边界语义（既有小容量构造点同型纪律）。
    store = CountingStore()
    service = _service(store, limits=BatchLimits(max_log_items=64))
    values = [request(request_id=f"{n:03}", item_id=f"{n:03}") for n in range(65)]
    count = 64 if quota == "items" else 8
    originals = []
    for start in range(0, count, 8):
        originals.extend(service.accept_batch(values[start:start + 8]))
    if quota == "bytes":
        # Freeze the exact full-log byte boundary before starting the collector.
        service.limits = replace(service.limits, max_log_bytes=len(
            batch_admission._bytes(_head(store)["pending"]["batches"])))
    limits = service.limits
    recording = RecordingCoordinator(service)
    collector = harness.collector(recording, max_wait_seconds=0.005)
    before, cas = deepcopy(store.docs), store.replace_calls
    with pytest.raises(AdmissionConflict) as caught:
        collector.accept(values[count])
    assert store.docs == before and store.replace_calls == cas
    assert recording.calls == [[values[count]]]
    assert service.materialize_oldest() == 8
    recovered = collector.accept(values[count])
    assert recovered.arrival_seq == count + 1 and not recovered.reused
    _assert_capacity(caught.value, reason)
    assert not collector.snapshot().closed and not collector.snapshot().failed
    assert collector.snapshot().worker_alive
    assert collector.coordinator is recording and recording.service is service
    assert service.limits == limits
    assert store.admission_cas == count // 8 + 1
    assert store.replace_calls == cas + 2  # one prefix shrink, one new admission
    assert _head(store)["last_allocated_seq"] == count + 1
    assert _head(store)["last_materialized_seq"] == 8
    for value, original in zip(values, originals):
        reused = service.reconcile(value)
        assert reused == original and reused.reused


@pytest.mark.parametrize("quota,reason", [
    ("items", "log_item_limit"), ("bytes", "log_byte_limit"),
])
@pytest.mark.parametrize("new_first", [False, True])
def test_capacity_mixed_retries_preserve_original_receipts_and_fifo(harness, quota, reason, new_first):
    store = CountingStore()
    service = _service(store)
    old = request(request_id="old", item_id="old")
    pending = request(request_id="log", item_id="log")
    fresh = request(request_id="new", item_id="new")
    materialized_receipt = service.accept_batch([old])[0]
    service.materialize_oldest()
    pending_receipt = service.accept_batch([pending])[0]
    limits = {"max_log_items": 1} if quota == "items" else {
        "max_log_bytes": len(batch_admission._bytes(_head(store)["pending"]["batches"]))}
    service.limits = replace(service.limits, **limits)
    retries = [replace(old, pipeline_version="v2", trace_id="retry"),
               replace(pending, pipeline_version="v2", trace_id="retry")]
    values = [fresh, *retries] if new_first else [*retries, fresh]
    before, cas = deepcopy(store.docs), store.replace_calls
    recording = RecordingCoordinator(service)
    collector = harness.collector(recording, paused=True)
    calls = harness.queue(collector, values)
    collector.start_worker.set()
    results = [call.get() for call in calls]
    reused = results[1:] if new_first else results[:2]
    assert reused == [materialized_receipt, pending_receipt]
    assert all(result.reused for result in reused)
    _assert_capacity(results[0 if new_first else 2], reason)
    assert recording.calls == [values, *[[value] for value in values]]
    assert store.docs == before and store.replace_calls == cas
    assert not collector.snapshot().closed
    assert service.materialize_oldest() == 2
    recovered = collector.accept(fresh)
    assert recovered.arrival_seq == 3 and not recovered.reused
    assert collector.accept(retries[0]) == materialized_receipt
    assert collector.accept(retries[1]) == pending_receipt
    assert store.admission_cas == 3 and store.replace_calls == cas + 2
    assert _head(store)["last_allocated_seq"] == 3
    assert service.limits == replace(BatchLimits(), **limits)


@pytest.mark.parametrize("quota,reason", [
    ("items", "log_item_limit"), ("bytes", "log_byte_limit"),
])
def test_capacity_split_accepts_only_fifo_prefix_that_fits(harness, quota, reason):
    store = CountingStore()
    service = _service(store)
    first = request(request_id="one", item_id="one")
    original = service.accept_batch([first])[0]
    log_bytes = len(batch_admission._bytes(_head(store)["pending"]["batches"]))
    service.materialize_oldest()
    limits = {"max_log_items": 1} if quota == "items" else {"max_log_bytes": log_bytes}
    service.limits = replace(service.limits, **limits)
    second = request(request_id="two", item_id="two")
    third = request(request_id="end", item_id="end")
    values = [second, third, second, first]
    recording = RecordingCoordinator(service)
    collector = harness.collector(recording, paused=True)
    calls = harness.queue(collector, values)
    collector.start_worker.set()
    results = [call.get() for call in calls]
    assert isinstance(results[0], AdmissionReceipt) and results[0].arrival_seq == 2
    assert not results[0].reused
    _assert_capacity(results[1], reason)
    assert results[2] == results[0] and results[2].reused
    assert results[3] == original and results[3].reused
    assert recording.calls == [values, *[[value] for value in values]]
    assert store.admission_cas == 2 and store.replace_calls == 3
    assert service.materialize_oldest() == 2
    assert collector.accept(third).arrival_seq == 3
    assert store.admission_cas == 3


@pytest.mark.parametrize("fault", ["owner_isolation", "owner", "read", "bad_log", "quarantine", "unknown"])
def test_full_log_does_not_mask_fatal_state_as_capacity(harness, fault, monkeypatch):
    store = CountingStore()
    service = _service(store, BatchLimits(max_log_items=1))
    service.accept_batch([request()])
    head = store.docs[("news-dedup-control-v1", "batch_admission_head")]["source"]
    if fault == "owner_isolation":
        service.owner_isolated = lambda: False
    elif fault == "owner":
        head["owner_id"] = "other-writer"
    elif fault == "bad_log":
        head["pending"]["batches"][0]["digest"] = "invalid"
    elif fault == "quarantine":
        head["checkpoint"]["batch_quarantine"] = {"reason": "permanent_bulk_error"}
    elif fault == "unknown":
        service._unknown_cas = True
    else:
        def fail(*_):
            raise OSError("realtime read unavailable")
        monkeypatch.setattr(store, "get", fail)
    before, cas = deepcopy(store.docs), store.replace_calls
    recording = RecordingCoordinator(service)
    collector = harness.collector(recording, paused=True)
    values = [request(), request(request_id="new", item_id="new")]
    calls = harness.queue(collector, values)
    collector.start_worker.set()
    results = [call.get() for call in calls]
    expected = AdmissionUnknown if fault == "read" else AdmissionConflict
    assert all(type(result) is expected for result in results)
    collector._thread.join(0.5)
    assert collector.snapshot().closed and collector.snapshot().failed
    assert not collector.snapshot().worker_alive
    with pytest.raises(AdmissionConflict) as caught:
        collector.accept(request())
    _assert_rejected(caught.value, "worker_failed")
    assert recording.calls == [values]
    assert store.docs == before and store.replace_calls == cas


@pytest.mark.parametrize("fault", ["unknown_before", "unknown_after", "owner"])
def test_capacity_split_stops_at_first_noncapacity_failure(harness, fault, monkeypatch):
    store = CountingStore()
    service = _service(store, BatchLimits(max_log_items=1))
    old = request(request_id="old", item_id="old")
    original = service.accept_batch([old])[0]
    service.materialize_oldest()

    def action(recording, values):
        if len(recording.calls) == 3:
            if fault == "owner":
                service.owner_isolated = lambda: False
            elif fault == "unknown_after":
                def lost_ack(*_):
                    raise OSError("CAS acknowledgement lost")
                monkeypatch.setattr(store, "after_write", lost_ack)
            else:
                def unconfirmed(*_):
                    raise OSError("CAS still in flight")
                monkeypatch.setattr(store, "replace", unconfirmed)
        return service.accept_batch(values)

    recording = RecordingCoordinator(service, action)
    collector = harness.collector(recording, paused=True)
    values = [old, request(request_id="one", item_id="one"),
              request(request_id="two", item_id="two")]
    calls = harness.queue(collector, values)
    collector.start_worker.set()
    results = [call.get() for call in calls]
    assert results[0] == original and results[0].reused
    expected = AdmissionConflict if fault == "owner" else AdmissionUnknown
    assert all(type(result) is expected for result in results[1:])
    assert recording.calls == [values, [old], [values[1]]]
    collector._thread.join(0.5)
    assert collector.snapshot().failed and collector.snapshot().closed
    assert _head(store)["last_allocated_seq"] == (2 if fault == "unknown_after" else 1)
    assert store.admission_cas == (2 if fault == "unknown_after" else 1)
    assert service.reconcile(old) == original
    if fault == "unknown_after":
        confirmed = service.reconcile(values[1])
        assert confirmed.arrival_seq == 2 and confirmed.reused
    with pytest.raises(AdmissionConflict) as caught:
        collector.accept(values[2])
    _assert_rejected(caught.value, "worker_failed")


def test_flush_signal_increments_and_sets_event_on_success(harness):
    store = CountingStore()
    collector = harness.collector(_service(store), paused=True, configured=False)
    values = [request(request_id=f"{n}-1", item_id=f"{n}-1") for n in range(5)]
    calls = harness.queue(collector, values)
    collector.start_worker.set()
    results = [call.get() for call in calls]
    assert all(isinstance(r, AdmissionReceipt) for r in results)
    snap = collector.snapshot()
    assert snap.flushed_batches == 1
    assert snap.flushed_signal == 1
    assert collector.batch_flushed.is_set()


def test_each_successful_flush_signals_once_per_batch(harness):
    store = CountingStore()
    collector = harness.collector(_service(store), paused=True, configured=False)
    # 4 个请求/批 → 2 批 flush；每批单独触发信号一次。
    batch_one = [request(request_id=f"{n}-1", item_id=f"{n}-1") for n in range(3)]
    calls = harness.queue(collector, batch_one)
    collector.start_worker.set()
    assert all(isinstance(call.get(), AdmissionReceipt) for call in calls)
    first_signal = collector.snapshot().flushed_signal
    assert first_signal == 1
    # 关闭并以新 collector 触发第二批独立信号。
    collector.close()
    collector2 = harness.collector(_service(store), paused=True, configured=False)
    calls2 = harness.queue(collector2, [request(request_id="4-1", item_id="4-1")])
    collector2.start_worker.set()
    assert isinstance(calls2[0].get(), AdmissionReceipt)
    assert collector2.snapshot().flushed_signal == 1


def test_failed_invocation_does_not_increment_signal(harness):
    def explode(recording, values):
        raise OSError("downstream lost")

    recording = RecordingCoordinator(_service(CountingStore()), explode)
    collector = harness.collector(recording, paused=True)
    calls = harness.queue(collector, [request()])
    collector.start_worker.set()
    assert isinstance(calls[0].get(), AdmissionUnknown)
    snap = collector.snapshot()
    assert snap.flushed_signal == 0
    assert snap.failed
    assert not collector.batch_flushed.is_set()


def test_event_signal_is_safe_under_sustained_throughput(harness):
    """16 个请求入队后启动 worker；flush 数与 batch 数一致、信号随之递增。"""
    store = CountingStore()
    collector = harness.collector(_service(store), paused=True, configured=False)
    calls = []
    for round_index in range(4):
        for n in range(4):
            value = request(request_id=f"{round_index}-{n}", item_id=f"{round_index}-{n}")
            calls.append(harness.call(collector, value))
    with collector._condition:
        assert collector._condition.wait_for(lambda: len(collector._waiting) == 16, timeout=1.5)
    assert collector.snapshot().flushed_signal == 0
    collector.start_worker.set()
    for call in calls:
        assert isinstance(call.get(), AdmissionReceipt)
    # W2Fε 收紧（WC1 F1）：机制推导钉——16 请求全部预入队后 worker 才启动，
    # _select 每批取满 max_batch_items=8 ⇒ 恰 2 次 _invoke；batch_collector.py
    # :253 每次成功 _invoke 恰递增 1 ⇒ signal==2（09-29 W3e F5① 回锚：原写
    # :245 系登记时点锚漂移）。P01 微测原始意图（log
    # \P01-异步物化聚合窗口微测.md："事件触发/快照字段/吞吐"四项，_invoke 成功
    # 后递增并 set）=信号与成功 flush 一一对应，"事件信号安全"与"批次数精确"
    # 两读法在 ==2 上合一。红能力：退化逐条 flush ⇒ signal=16≠2 即红；
    # 丢信号 ⇒ <2 即红。原 1<=signal<=16 上界 16 构造恒真（永不失败）已拆除。
    signal = collector.snapshot().flushed_signal
    assert signal == 2
    assert collector.batch_flushed.is_set()


# ---------- P0-T6 参数分层（域层默认值+行为钉，主窗令） ----------

def test_t6_module_resolution_stays_within_this_repo():
    """P0-T6 解析钉：news_flash_dedup 模块必须解析到本仓（收集期曾有
    集成测试模块把外来主产品树 src 插到 sys.path[0]，本仓首次导入的
    batch_collector 解析到外来旧副本——T6 改动首次暴露该隐性毒化；
    test_text_normalize_uat.py 已回锚仓相对）。全套跑（integration 先
    于 unit 收集/执行）下此钉保持红/绿敏感。"""
    from pathlib import Path

    import news_flash_dedup.batch_collector as module

    repo_root = Path(__file__).resolve().parents[2]
    resolved = Path(module.__file__).resolve()
    assert repo_root == resolved.parents[2]


def test_t6_collector_domain_defaults():
    """P0-T6：collector 域层默认——max_wait 0.02（原 0.1）/队列硬层 512
    （原 64）/告警层 400；与 durable log 硬容量（BatchLimits.max_log_
    items=512）同梯（内存缓冲与持久缓冲对齐）。"""
    import inspect

    parameters = inspect.signature(BatchAdmissionCollector.__init__).parameters
    assert parameters["max_wait_seconds"].default == 0.02
    assert parameters["max_queue_items"].default == 512
    assert parameters["warning_depth"].default == 400
    assert parameters["max_queue_bytes"].default == 4 * 1024 * 1024


def test_t6_queue_warning_tier_crosses_once(harness, caplog):
    """P0-T6 告警层行为钉：深度达 warning_depth 越层告警一次；层上继续
    入队不重复告警（深度未回落）；计量入 snapshot.depth_warnings。"""
    import logging

    store = CountingStore()
    collector = harness.collector(
        _service(store), paused=True, configured=False,
        max_queue_items=10, warning_depth=3, max_wait_seconds=0.05)
    with caplog.at_level(logging.WARNING,
                         logger="news_flash_dedup.batch_collector"):
        for n in range(3):
            harness.call(collector,
                         request(request_id=f"w{n}-1", item_id=f"w{n}-1"))
        with collector._condition:
            assert collector._condition.wait_for(
                lambda: len(collector._waiting) == 3, timeout=1.0)
        crossing = [r for r in caplog.records if "warning tier" in r.getMessage()]
        assert len(crossing) == 1                     # 越层恰一次
        assert collector.snapshot().depth_warnings == 1
        harness.call(collector, request(request_id="w3-1", item_id="w3-1"))
        with collector._condition:
            assert collector._condition.wait_for(
                lambda: len(collector._waiting) == 4, timeout=1.0)
        assert len([r for r in caplog.records
                    if "warning tier" in r.getMessage()]) == 1   # 层上不重复
        assert collector.snapshot().depth_warnings == 1


def test_t6_depth_warning_gauge_reset_and_recross(harness, caplog):
    """P0-T6 gauge 语义钉（锁内直驱，harness 同款内部探针纪律）：回落
    复位——再次越层重新告警；depth_warnings 计数回落不清零（审计连续）。"""
    import logging

    from news_flash_dedup.batch_collector import _Waiting

    store = CountingStore()
    collector = harness.collector(
        _service(store), paused=True, configured=False,
        max_queue_items=10, warning_depth=3, max_wait_seconds=0.05)

    def _fill(count):
        for _ in range(count):
            collector._waiting.append(
                _Waiting(request=request(), size=100, queued_at=monotonic()))

    with caplog.at_level(logging.WARNING,
                         logger="news_flash_dedup.batch_collector"):
        with collector._condition:
            _fill(3)
            collector._note_depth_warning()          # 越层 → 告警 1
            collector._note_depth_warning()          # 层上重复评估：不重复
            collector._waiting.clear()
            collector._note_depth_warning()          # 回落：复位（零告警）
            _fill(3)
            collector._note_depth_warning()          # 再越层 → 告警 2
        warnings = [r for r in caplog.records if "warning tier" in r.getMessage()]
        assert len(warnings) == 2
        assert collector.snapshot().depth_warnings == 2


def test_t6_warning_tier_above_hard_cap_is_inert(harness, caplog):
    """P0-T6 fail-soft 钉：告警层高于硬层（既有小容量构造点形态）→ 惰性
    （永不告警、不配置报错）；硬层 queue_full 拒收照旧。"""
    import logging

    store = CountingStore()
    collector = harness.collector(
        _service(store), paused=True, configured=False,
        max_queue_items=2, warning_depth=400, max_wait_seconds=0.005)
    with caplog.at_level(logging.WARNING,
                         logger="news_flash_dedup.batch_collector"):
        calls = [harness.call(collector,
                              request(request_id=f"i{n}-1", item_id=f"i{n}-1"))
                 for n in range(2)]
        with collector._condition:
            assert collector._condition.wait_for(
                lambda: len(collector._waiting) == 2, timeout=1.0)
        overflow = harness.call(collector,
                                request(request_id="i2-1", item_id="i2-1"))
        _assert_rejected(overflow.get(), "queue_full")   # 硬层先于告警层
        assert collector.snapshot().depth_warnings == 0  # 告警层惰性
        assert not [r for r in caplog.records
                    if "warning tier" in r.getMessage()]
