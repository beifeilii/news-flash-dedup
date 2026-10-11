"""P0-T2 物化重试与熔断（重试预算+熔断器；时钟/随机源全注入）。

口径（施工简报 P0-T2；设计稿 §6.1 参数，退避上限以简报 60s 为准）：

- 熔断：20 请求窗内失败≥5 且失败率≥50% → 开（30 秒）→ 半开（2 个
  探测连续成功→恢复；探测失败→重开）；
- 退避：全抖动 ``uniform[0, min(60s, 0.5s·2^n)]``；
- 熔断期间新件只持久化 ACCEPTED、不推进水位、不产墓碑（受理路径不经
  熔断闸——闸只挂物化写路径）；
- 活动尝试只统计真正发出且得到**确定失败**的请求（瞬态/系统级）；
  UNKNOWN_WRITE 走读回不计数；等待熔断恢复期间不消耗活动尝试；
- **重试耗尽≠永久失败**（本模块无任何把重试状态升级为墓簿/终态的
  通路——墓碑唯一入口=分类为 ITEM_PERMANENT，见 T3/T4）。

纪律：域层零全局 sleep——只产出下一动作时刻（next_retry_at/
retry_after_seconds），等待由编排层拥有（计划 Task 2 Step 3 同款）。
"""

from __future__ import annotations

import random
from collections import deque
from dataclasses import dataclass, replace
from typing import Callable

from .materialize_failure import (
    FailureClass,
    MaterializeFailure,
    SystemicOutageError,
)


BREAKER_CLOSED = "closed"
BREAKER_OPEN = "open"
BREAKER_HALF_OPEN = "half_open"


@dataclass(frozen=True)
class MaterializeRetryPolicy:
    """P0-T2 参数钉（简报口径单源；构造即校验）。"""

    active_attempt_limit: int = 5
    retry_base_seconds: float = 0.5
    retry_max_seconds: float = 60.0          # 简报口径：最长退避 60 秒
    breaker_window_requests: int = 20
    breaker_min_failures: int = 5
    breaker_failure_ratio: float = 0.5
    breaker_open_seconds: float = 30.0
    breaker_half_open_probes: int = 2

    def __post_init__(self) -> None:
        if (type(self.active_attempt_limit) is not int
                or self.active_attempt_limit < 1):
            raise ValueError("active_attempt_limit must be a positive int")
        if (type(self.breaker_window_requests) is not int
                or self.breaker_window_requests < self.breaker_min_failures):
            raise ValueError(
                "breaker window must be an int >= breaker_min_failures")
        if (type(self.breaker_min_failures) is not int
                or self.breaker_min_failures < 1):
            raise ValueError("breaker_min_failures must be a positive int")
        if (type(self.breaker_half_open_probes) is not int
                or self.breaker_half_open_probes < 1):
            raise ValueError("breaker_half_open_probes must be a positive int")
        if not 0 < self.retry_base_seconds <= self.retry_max_seconds:
            raise ValueError("retry delay bounds must satisfy 0 < base <= max")
        if not 0 < self.breaker_open_seconds:
            raise ValueError("breaker_open_seconds must be positive")
        if not 0 < self.breaker_failure_ratio <= 1:
            raise ValueError("breaker_failure_ratio must be in (0, 1]")


def retry_delay_seconds(attempt: int, *, policy: MaterializeRetryPolicy,
                        random_value: float) -> float:
    """全抖动退避：``random_value · min(cap, base·2^(attempt-1))``。

    attempt 为 1 基（第 n 次确定失败后的下一次尝试等待）；随机值由调用
    方注入（``[0, 1)``），域内不触全局 random（确定性测试前提）。
    """
    if type(attempt) is not int or attempt < 1:
        raise ValueError("attempt must be a positive int (1-based)")
    if not 0.0 <= random_value < 1.0:
        raise ValueError("random_value must be in [0, 1)")
    ceiling = min(policy.retry_max_seconds,
                  policy.retry_base_seconds * (2 ** (attempt - 1)))
    return random_value * ceiling


@dataclass
class RetryState:
    """单条目重试账（可变；活动尝试=真正发出且得到确定失败的请求）。

    - ``active_attempts``：确定失败计数（瞬态/永久/损坏/系统级）；
      UNKNOWN_WRITE 不计数（读回优先）；熔断等待不计数；
    - ``next_retry_at``：下一次允许尝试的时刻（秒，注入时钟域）；
      UNKNOWN_WRITE 置为当前时刻（立即读回）；
    - 重试耗尽（``active_attempts`` 达上限）**不是**终态——本结构不
      承载任何永久化语义。
    """

    active_attempts: int = 0
    first_failed_at: float | None = None
    last_failed_at: float | None = None
    last_failure_class: FailureClass | None = None
    next_retry_at: float | None = None

    def record_definite_failure(self, *, now: float, delay: float,
                                failure_class: FailureClass) -> None:
        if self.first_failed_at is None:
            self.first_failed_at = now
        self.active_attempts += 1
        self.last_failed_at = now
        self.last_failure_class = failure_class
        self.next_retry_at = now + delay

    def record_unknown(self, *, now: float) -> None:
        if self.first_failed_at is None:
            self.first_failed_at = now
        self.last_failed_at = now
        self.last_failure_class = FailureClass.UNKNOWN_WRITE
        if self.next_retry_at is None:
            self.next_retry_at = now


@dataclass(frozen=True)
class BreakerDecision:
    """熔断闸判定（编排层消费；allowed=False 时 retry_after>0）。"""

    state: str
    allowed: bool
    retry_after_seconds: float


class MaterializeDeferred(SystemicOutageError):
    """熔断开启期物化延后（受理事实不动、水位不推进、不产墓碑）。

    基类 SystemicOutageError：经 classify_exception 归 SYSTEMIC_OUTAGE
    （不重试计数、不墓碑）；携带 retry_after_seconds 供编排层等待。
    """

    def __init__(self, message: str, *, state: str,
                 retry_after_seconds: float) -> None:
        super().__init__(message)
        self.breaker_state = state
        self.retry_after_seconds = retry_after_seconds


class CircuitBreaker:
    """物化熔断器（滚动请求窗；调用方持锁——单飞域，无内锁）。

    状态机：closed →（窗内失败≥min 且比率≥ratio）→ open →（open_seconds
    后）→ half_open →（连续 probe 成功≥half_open_probes）→ closed；
    half_open 探测失败 → open（重开计时，open_count+1）。开/关均清窗
    （恢复后陈旧样本不得即时再跳闸）。
    """

    def __init__(self, policy: MaterializeRetryPolicy, *,
                 clock: Callable[[], float]) -> None:
        self._policy = policy
        self._clock = clock
        self._window: deque[bool] = deque(maxlen=policy.breaker_window_requests)
        self._state = BREAKER_CLOSED
        self._opened_at: float | None = None
        self._probe_successes = 0
        self._open_count = 0

    @property
    def state(self) -> str:
        return self._state

    @property
    def open_count(self) -> int:
        return self._open_count

    @property
    def window(self) -> tuple[bool, ...]:
        """观测面（True=失败样本；测试/指标只读）。"""
        return tuple(self._window)

    def decision(self, *, now: float | None = None) -> BreakerDecision:
        now = self._clock() if now is None else now
        if self._state == BREAKER_CLOSED:
            return BreakerDecision(BREAKER_CLOSED, True, 0.0)
        if self._state == BREAKER_OPEN:
            elapsed = now - (self._opened_at or 0.0)
            remaining = self._policy.breaker_open_seconds - elapsed
            if remaining > 0:
                return BreakerDecision(BREAKER_OPEN, False, remaining)
            self._state = BREAKER_HALF_OPEN
            self._probe_successes = 0
        # open 已期满 → half_open：本次尝试即探测（允许）。
        return BreakerDecision(BREAKER_HALF_OPEN, True, 0.0)

    def record_success(self, *, now: float | None = None) -> None:
        now = self._clock() if now is None else now
        if self._state == BREAKER_HALF_OPEN:
            self._probe_successes += 1
            if self._probe_successes >= self._policy.breaker_half_open_probes:
                self._close()
            return
        if self._state == BREAKER_CLOSED:
            self._window.append(False)

    def record_failure(self, *, now: float | None = None) -> None:
        now = self._clock() if now is None else now
        if self._state == BREAKER_HALF_OPEN:
            # 探测失败：立即重开（新一轮 open_seconds）。
            self._open(now)
            return
        if self._state == BREAKER_OPEN:
            return                                  # 已开：无新样本语义
        self._window.append(True)
        failures = sum(self._window)
        if (failures >= self._policy.breaker_min_failures
                and failures / len(self._window) >=
                self._policy.breaker_failure_ratio):
            self._open(now)

    def _open(self, now: float) -> None:
        self._state = BREAKER_OPEN
        self._opened_at = now
        self._open_count += 1
        self._probe_successes = 0
        self._window.clear()

    def _close(self) -> None:
        self._state = BREAKER_CLOSED
        self._opened_at = None
        self._probe_successes = 0
        self._window.clear()


class MaterializeRetryRuntime:
    """T2 运行时门面：熔断闸 + 请求级样本 + 条目级重试账。

    - ``guard_attempt``：物化写路径入口闸（开路→MaterializeDeferred）；
    - ``record_request_failure/success``：请求级（一次 bulk/读回往返）
      熔断窗样本——**仅失败/成功的确定结果计入**（调用方按
      ``failure.participates_in_breaker`` 过滤后调用失败面）；
    - ``record_entry_failure/success``：条目级重试账（active attempts、
      next_retry_at）；
    - 时钟/随机源注入；单飞域调用方持锁（协调器 RLock），无内锁。
    """

    def __init__(self, *,
                 policy: MaterializeRetryPolicy | None = None,
                 clock: Callable[[], float],
                 random_source: Callable[[], float] | None = None) -> None:
        self.policy = (policy if policy is not None
                       else MaterializeRetryPolicy())
        self._clock = clock
        self._random = (random_source if random_source is not None
                        else random.random)
        self.breaker = CircuitBreaker(self.policy, clock=clock)
        self._entries: dict[int, RetryState] = {}

    # ---- 请求级（熔断窗样本） ----

    def breaker_decision(self) -> BreakerDecision:
        return self.breaker.decision()

    def guard_attempt(self) -> None:
        decision = self.breaker.decision()
        if not decision.allowed:
            raise MaterializeDeferred(
                f"materialization deferred: breaker {decision.state} "
                f"(retry after {decision.retry_after_seconds:.1f}s)",
                state=decision.state,
                retry_after_seconds=decision.retry_after_seconds)

    def record_request_failure(self) -> None:
        self.breaker.record_failure()

    def record_request_success(self) -> None:
        self.breaker.record_success()

    # ---- 条目级（重试账） ----

    def next_delay(self, attempt: int) -> float:
        """P1a-T3：任务文档退避取值面（条目账迁入 work 文档后，defer
        写侧的公共延迟计算——policy/随机源单源注入，域外不触私有态）。"""
        return retry_delay_seconds(
            attempt, policy=self.policy, random_value=self._random())

    def record_entry_failure(self, arrival_seq: int,
                             failure: MaterializeFailure) -> RetryState:
        """条目失败入账：确定失败计数+全抖动退避；未知写入零计数即时读回。"""
        state = self._entries.get(arrival_seq)
        if state is None:
            state = RetryState()
            self._entries[arrival_seq] = state
        now = self._clock()
        if failure.failure_class is FailureClass.UNKNOWN_WRITE:
            state.record_unknown(now=now)
        else:
            attempt = state.active_attempts + 1
            delay = retry_delay_seconds(
                attempt, policy=self.policy, random_value=self._random())
            state.record_definite_failure(
                now=now, delay=delay,
                failure_class=failure.failure_class)
        return state

    def record_entry_success(self, arrival_seq: int) -> RetryState | None:
        """条目终态成功出账（返回被清账的快照；无账=None）。"""
        return self._entries.pop(arrival_seq, None)

    def entry_state(self, arrival_seq: int) -> RetryState | None:
        """观测面（副本——外部不得经别名改账）。"""
        state = self._entries.get(arrival_seq)
        return None if state is None else replace(state)

    def entry_retry_after(self, arrival_seq: int) -> float | None:
        state = self._entries.get(arrival_seq)
        return None if state is None else state.next_retry_at

    def entry_exhausted(self, arrival_seq: int) -> bool:
        """活动尝试达上限（≠永久失败——读回/读回后重试仍合法）。"""
        state = self._entries.get(arrival_seq)
        return (state is not None
                and state.active_attempts >= self.policy.active_attempt_limit)


__all__ = [
    "BREAKER_CLOSED",
    "BREAKER_HALF_OPEN",
    "BREAKER_OPEN",
    "BreakerDecision",
    "CircuitBreaker",
    "MaterializeDeferred",
    "MaterializeRetryPolicy",
    "MaterializeRetryRuntime",
    "RetryState",
    "retry_delay_seconds",
]
