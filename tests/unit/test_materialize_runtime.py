"""P0-T2 单测：物化重试与熔断（确定性注入时钟/随机源）。

钉面（施工简报 P0-T2）：
- 参数钉：窗 20/失败≥5/比率≥50%/开 30s/半开探测 2/退避上限 60s/基数 0.5；
- 熔断状态机：trip→open 30s→half_open→2 连续探测成功→closed；
  探测失败→重开（open_count+1）；
- 熔断等待不消耗活动尝试；UNKNOWN_WRITE 零计数即时读回；
- 重试耗尽≠永久失败（无升级终态通路）；
- 协调器接线：开路期物化零 ES 触达/零水位推进/零墓碑面，受理照常
  ACCEPTED；瞬态失败计入熔断窗→5 连失败开路→MaterializeDeferred。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from news_flash_dedup.admission import AdmissionUnknown
from news_flash_dedup.batch_admission import BatchAdmissionCoordinator, BatchLimits
from news_flash_dedup.materialize_failure import (
    FailureClass,
    MaterializeFailure,
    SystemicOutageError,
    classify_exception,
)
from news_flash_dedup.materialize_runtime import (
    BREAKER_CLOSED,
    BREAKER_HALF_OPEN,
    BREAKER_OPEN,
    BreakerDecision,
    CircuitBreaker,
    MaterializeDeferred,
    MaterializeRetryPolicy,
    MaterializeRetryRuntime,
    RetryState,
    retry_delay_seconds,
)
from test_admission import MemoryStore, request
from test_batch_admission import BatchMemoryStore, coordinator


NOW = datetime(2026, 9, 24, 0, 0, tzinfo=timezone.utc)


class _Clock:
    """注入时钟（秒域浮点；构造时基 0）。"""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _transient(message: str = "connection refused") -> MaterializeFailure:
    return MaterializeFailure(
        failure_class=FailureClass.TRANSIENT_INFRA,
        error_code="transport", detail=message)


def _unknown(message: str = "bulk confirmation is unknown") -> MaterializeFailure:
    return MaterializeFailure(
        failure_class=FailureClass.UNKNOWN_WRITE,
        error_code="unconfirmed_outcome", detail=message)


# ---------- 参数钉 ----------

def test_policy_pins_p0_defaults():
    policy = MaterializeRetryPolicy()
    assert policy.active_attempt_limit == 5
    assert policy.retry_base_seconds == 0.5
    assert policy.retry_max_seconds == 60.0            # 简报口径：最长退避 60 秒
    assert policy.breaker_window_requests == 20
    assert policy.breaker_min_failures == 5
    assert policy.breaker_failure_ratio == 0.5
    assert policy.breaker_open_seconds == 30.0
    assert policy.breaker_half_open_probes == 2


@pytest.mark.parametrize("kwargs", [
    {"active_attempt_limit": 0},
    {"breaker_window_requests": 4},                    # 窗 < 最小失败数
    {"breaker_min_failures": 0},
    {"breaker_half_open_probes": 0},
    {"retry_base_seconds": 0.0},
    {"retry_base_seconds": 61.0},                      # base > max 非法
    {"retry_max_seconds": 0.0},
    {"breaker_open_seconds": 0.0},
    {"breaker_failure_ratio": 0.0},
    {"breaker_failure_ratio": 1.5},
])
def test_policy_rejects_invalid_shapes(kwargs):
    with pytest.raises(ValueError):
        MaterializeRetryPolicy(**kwargs)


# ---------- 全抖动退避 ----------

def test_retry_delay_full_jitter_bounds():
    policy = MaterializeRetryPolicy()
    assert retry_delay_seconds(1, policy=policy, random_value=0.0) == 0.0
    assert retry_delay_seconds(1, policy=policy,
                                random_value=0.5) == pytest.approx(0.25)
    assert retry_delay_seconds(3, policy=policy,
                                random_value=0.999) < 2.0     # ceiling=2.0
    # 上限钳制：0.5·2^(n-1) ≥ 60 之后恒 60。
    for attempt in (8, 12, 20):
        assert retry_delay_seconds(attempt, policy=policy,
                                    random_value=0.999) <= 60.0
    assert retry_delay_seconds(20, policy=policy,
                               random_value=1.0 - 1e-9) == pytest.approx(
        60.0, rel=1e-6)


@pytest.mark.parametrize("attempt,random_value", [
    (0, 0.5), (-1, 0.5), ("1", 0.5), (1, 1.0), (1, -0.1),
])
def test_retry_delay_rejects_invalid_inputs(attempt, random_value):
    with pytest.raises(ValueError):
        retry_delay_seconds(attempt, policy=MaterializeRetryPolicy(),
                            random_value=random_value)


# ---------- 熔断状态机 ----------

def _breaker(clock: _Clock) -> CircuitBreaker:
    return CircuitBreaker(MaterializeRetryPolicy(), clock=clock)


def test_breaker_trips_at_five_failures_full_ratio():
    clock = _Clock(100.0)
    breaker = _breaker(clock)
    for _ in range(4):
        breaker.record_failure()
    assert breaker.decision().state == BREAKER_CLOSED
    breaker.record_failure()                            # 第 5 连续失败
    decision = breaker.decision()
    assert decision.state == BREAKER_OPEN
    assert decision.allowed is False
    assert decision.retry_after_seconds == pytest.approx(30.0)
    assert breaker.open_count == 1


def test_breaker_trips_at_half_ratio_boundary():
    # 5 成功 + 5 失败 = 10 样本 50% ≥ 50% → 开。
    clock = _Clock()
    breaker = _breaker(clock)
    for _ in range(5):
        breaker.record_success()
    for _ in range(4):
        breaker.record_failure()
    assert breaker.decision().state == BREAKER_CLOSED
    breaker.record_failure()
    assert breaker.decision().state == BREAKER_OPEN


def test_breaker_stays_closed_below_threshold_or_ratio():
    clock = _Clock()
    breaker = _breaker(clock)
    for _ in range(10):
        breaker.record_success()
    for _ in range(4):
        breaker.record_failure()                        # 4/14 ≈ 28.6%
    assert breaker.decision().state == BREAKER_CLOSED
    # 窗滑动稀释：20 成功后 5 失败 → 15S+5F=25% < 50% 不开。
    breaker2 = _breaker(_Clock())
    for _ in range(20):
        breaker2.record_success()
    for _ in range(5):
        breaker2.record_failure()
    assert breaker2.decision().state == BREAKER_CLOSED


def test_breaker_open_holds_thirty_seconds_then_half_open():
    clock = _Clock(0.0)
    breaker = _breaker(clock)
    for _ in range(5):
        breaker.record_failure()
    clock.advance(29.0)
    decision = breaker.decision()
    assert decision.state == BREAKER_OPEN
    assert decision.allowed is False
    assert decision.retry_after_seconds == pytest.approx(1.0)
    clock.advance(1.0)                                  # 期满
    decision = breaker.decision()
    assert decision.state == BREAKER_HALF_OPEN
    assert decision.allowed is True                     # 本次尝试即探测


def test_breaker_two_consecutive_probes_close():
    clock = _Clock()
    breaker = _breaker(clock)
    for _ in range(5):
        breaker.record_failure()
    clock.advance(30.0)
    breaker.decision()                                  # → half_open
    breaker.record_success()                            # 探测 1
    assert breaker.state == BREAKER_HALF_OPEN
    breaker.record_success()                            # 探测 2：连续
    assert breaker.state == BREAKER_CLOSED
    # 恢复后窗已清：单次失败不再即时跳闸。
    breaker.record_failure()
    assert breaker.state == BREAKER_CLOSED


def test_breaker_probe_failure_reopens_with_fresh_hold():
    clock = _Clock()
    breaker = _breaker(clock)
    for _ in range(5):
        breaker.record_failure()
    clock.advance(30.0)
    breaker.decision()                                  # → half_open
    breaker.record_success()                            # 探测 1 成功
    breaker.record_failure()                            # 探测 2 失败 → 重开
    assert breaker.state == BREAKER_OPEN
    assert breaker.open_count == 2
    clock.advance(29.0)
    assert breaker.decision().state == BREAKER_OPEN
    clock.advance(1.0)
    assert breaker.decision().state == BREAKER_HALF_OPEN


def test_breaker_success_while_open_is_ignored_for_window():
    # 闸为咨询面：open 期间漏入的成功不稀释也不闭路（状态只由探测路径闭）。
    clock = _Clock()
    breaker = _breaker(clock)
    for _ in range(5):
        breaker.record_failure()
    breaker.record_success()
    assert breaker.state == BREAKER_OPEN


# ---------- RetryState / 运行时门面 ----------

def test_retry_state_definite_failure_accounting():
    clock = _Clock(50.0)
    runtime = MaterializeRetryRuntime(clock=clock,
                                      random_source=lambda: 0.5)
    state = runtime.record_entry_failure(17, _transient())
    assert state.active_attempts == 1
    assert state.first_failed_at == 50.0
    assert state.last_failure_class is FailureClass.TRANSIENT_INFRA
    # 全抖动（随机 0.5）：首次 ceiling=0.5 → delay 0.25 → 50.25。
    assert state.next_retry_at == pytest.approx(50.25)
    clock.advance(5.0)
    runtime.record_entry_failure(17, _transient())
    state = runtime.entry_state(17)
    assert state.active_attempts == 2
    assert state.first_failed_at == 50.0                # 首失败时刻不变
    assert state.last_failed_at == 55.0


def test_retry_state_unknown_write_zero_attempts_immediate_readback():
    clock = _Clock(10.0)
    runtime = MaterializeRetryRuntime(clock=clock,
                                      random_source=lambda: 0.5)
    for _ in range(10):
        runtime.record_entry_failure(17, _unknown())
    state = runtime.entry_state(17)
    assert state.active_attempts == 0                   # 读回优先：零计数
    assert state.next_retry_at == 10.0                  # 立即读回
    assert runtime.entry_exhausted(17) is False


def test_breaker_wait_does_not_consume_active_attempts():
    clock = _Clock(0.0)
    runtime = MaterializeRetryRuntime(clock=clock,
                                      random_source=lambda: 0.0)
    runtime.record_entry_failure(17, _transient())
    runtime.record_entry_failure(17, _transient())
    runtime.record_entry_failure(17, _transient())
    before = runtime.entry_state(17).active_attempts
    # 熔断开路 30 秒（等待期）：无任何新失败样本——条目账不动。
    for _ in range(5):
        runtime.record_request_failure()
    assert runtime.breaker.state == BREAKER_OPEN
    clock.advance(30.0)
    assert runtime.entry_state(17).active_attempts == before
    # 熔断等待不把 unknown 转永久：读回仍合法。
    runtime.record_entry_failure(17, _unknown())
    assert runtime.entry_state(17).active_attempts == before
    assert runtime.entry_state(17).last_failure_class is FailureClass.UNKNOWN_WRITE


def test_retry_exhaustion_is_not_permanent_failure():
    clock = _Clock()
    runtime = MaterializeRetryRuntime(clock=clock,
                                      random_source=lambda: 0.0)
    for _ in range(5):                                  # 活动尝试上限 5
        runtime.record_entry_failure(17, _transient())
    assert runtime.entry_exhausted(17) is True
    # 耗尽≠永久：后续仍可读回/重试记账（无任何终态升级 API）。
    state = runtime.record_entry_failure(17, _unknown())
    assert state.active_attempts == 5                   # unknown 不再加
    assert state.next_retry_at == clock.now
    runtime.record_entry_success(17)
    assert runtime.entry_state(17) is None
    assert runtime.entry_exhausted(17) is False


def test_entry_state_returns_copy_not_alias():
    clock = _Clock()
    runtime = MaterializeRetryRuntime(clock=clock,
                                      random_source=lambda: 0.0)
    runtime.record_entry_failure(3, _transient())
    view = runtime.entry_state(3)
    view.active_attempts = 99                           # 改副本不得改账
    assert runtime.entry_state(3).active_attempts == 1


# ---------- MaterializeDeferred ----------

def test_materialize_deferred_carries_breaker_state_and_classifies_systemic():
    deferred = MaterializeDeferred(
        "materialization deferred: breaker open",
        state=BREAKER_OPEN, retry_after_seconds=12.5)
    assert isinstance(deferred, SystemicOutageError)
    assert deferred.breaker_state == BREAKER_OPEN
    assert deferred.retry_after_seconds == 12.5
    failure = classify_exception(deferred)
    assert failure.failure_class is FailureClass.SYSTEMIC_OUTAGE
    assert failure.may_tombstone is False


def test_runtime_guard_attempt_raises_only_when_disallowed():
    clock = _Clock()
    runtime = MaterializeRetryRuntime(clock=clock,
                                      random_source=lambda: 0.0)
    runtime.guard_attempt()                             # closed：放行
    for _ in range(5):
        runtime.record_request_failure()
    with pytest.raises(MaterializeDeferred) as caught:
        runtime.guard_attempt()
    assert caught.value.breaker_state == BREAKER_OPEN
    assert caught.value.retry_after_seconds == pytest.approx(30.0)
    assert isinstance(caught.value, SystemicOutageError)


# ---------- 协调器接线（熔断期间只持久化 ACCEPTED） ----------

class _FailingBulk(BatchMemoryStore):
    """瞬态断网形态：bulk 恒失败（连接拒绝）。"""

    def bulk_create(self, documents):
        self.bulk_calls += 1
        raise OSError("connection refused during bulk")


def _wired(store, clock: _Clock) -> BatchAdmissionCoordinator:
    runtime = MaterializeRetryRuntime(clock=clock,
                                      random_source=lambda: 0.0)
    wall = {"at": NOW + timedelta(seconds=clock.now)}
    return BatchAdmissionCoordinator(
        store, owner_id="writer-t2", owner_isolated=lambda: True,
        limits=BatchLimits(), clock=lambda: wall["at"],
        retry_runtime=runtime)


def test_open_breaker_blocks_materialization_with_zero_es_touch():
    clock = _Clock()
    store = _FailingBulk()
    service = _wired(store, clock)
    service.accept_batch([request()])
    for _ in range(5):                                  # 制造 5 连瞬态失败开路
        with pytest.raises(AdmissionUnknown):
            service.materialize_prefix()
    assert store.bulk_calls == 5
    head_before = store.get("news-dedup-control-v1", "batch_admission_head")
    with pytest.raises(MaterializeDeferred):
        service.materialize_prefix()
    # 零 ES 触达：无新 bulk、头文档逐字节不变（水位 0、pending 仍在）。
    assert store.bulk_calls == 5
    head_after = store.get("news-dedup-control-v1", "batch_admission_head")
    assert head_after == head_before
    assert head_after["source"]["last_materialized_seq"] == 0
    assert len(head_after["source"]["pending"]["batches"]) == 1


def test_open_breaker_still_admits_new_items_as_accepted_only():
    clock = _Clock()
    store = _FailingBulk()
    service = _wired(store, clock)
    service.accept_batch([request(request_id="1-1", item_id="1-1")])
    for _ in range(5):
        with pytest.raises(AdmissionUnknown):
            service.materialize_prefix()
    assert store.bulk_calls == 5
    # 熔断开路期：受理照常（头文档 CAS 不经闸）——新件只持久化 ACCEPTED。
    receipt = service.accept_batch([request(request_id="2-1", item_id="2-1")])[0]
    assert receipt.arrival_seq == 2
    head = store.get("news-dedup-control-v1", "batch_admission_head")["source"]
    assert head["last_allocated_seq"] == 2
    assert head["last_materialized_seq"] == 0           # 水位不推进
    assert len(head["pending"]["batches"]) == 2
    # 物化仍被闸：不开 bulk、不产墓碑面（T3 前无墓碑通路——钉 bulk_calls）。
    with pytest.raises(MaterializeDeferred):
        service.materialize_prefix()
    assert store.bulk_calls == 5


def test_transient_bulk_failures_trip_breaker_through_coordinator():
    clock = _Clock()
    store = _FailingBulk()
    service = _wired(store, clock)
    service.accept_batch([request()])
    for _ in range(5):
        with pytest.raises(AdmissionUnknown):
            service.materialize_prefix()
    assert service.retry_runtime.breaker.window.count(True) == 0   # 开路即清窗
    assert service.retry_runtime.breaker.state == BREAKER_OPEN
    assert service.retry_runtime.breaker.open_count == 1
    # 既有行为保链：瞬态失败仍按 AdmissionUnknown 上抛（调用方重试语义不变）。
    with pytest.raises(MaterializeDeferred):
        service.materialize_prefix()


def test_recovered_materialization_closes_breaker_after_two_probe_bulks():
    clock = _Clock()

    class _RecoverableBulk(BatchMemoryStore):
        fail = True

        def bulk_create(self, documents):
            self.bulk_calls += 1
            if self.fail:
                raise OSError("connection refused during bulk")
            return super().bulk_create(documents)

    store = _RecoverableBulk()
    service = _wired(store, clock)
    service.accept_batch([request(request_id="1-1", item_id="1-1")])
    service.accept_batch([request(request_id="2-1", item_id="2-1")])
    for _ in range(5):
        with pytest.raises(AdmissionUnknown):
            service.materialize_prefix()
    assert service.retry_runtime.breaker.state == BREAKER_OPEN

    store.fail = False                                  # 故障恢复
    clock.advance(31.0)                                 # 开路期满 → half_open
    # 一次 prefix 物化（前两批一次 bulk：探测 1 成功）。
    assert service.materialize_prefix() == 2
    assert service.retry_runtime.breaker.state == BREAKER_HALF_OPEN
    # 第三批：探测 2 连续成功 → closed。
    service.accept_batch([request(request_id="3-1", item_id="3-1")])
    assert service.materialize_prefix() == 3
    assert service.retry_runtime.breaker.state == BREAKER_CLOSED
    head = store.get("news-dedup-control-v1", "batch_admission_head")["source"]
    assert head["last_materialized_seq"] == 3           # 恢复后水位补进
    assert head["pending"]["batches"] == []


def test_retry_runtime_none_default_keeps_legacy_behavior():
    # 无 retry_runtime：物化路径不触闸（构造面零 diff——现役调用形态不变）。
    service = coordinator(BatchMemoryStore())
    assert service.retry_runtime is None
    service.accept_batch([request()])
    assert service.materialize_prefix() == 1
