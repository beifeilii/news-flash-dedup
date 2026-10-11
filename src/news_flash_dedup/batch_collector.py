"""单条受理入口的有界等待队列；批次 CAS 确认后才返回受理凭据。"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field
from math import isfinite
from threading import Condition, Event, Thread, TIMEOUT_MAX, current_thread
from time import monotonic

from .admission import (
    AdmissionConflict, AdmissionReceipt, AdmissionRequest, AdmissionUnknown, IdentityConflict,
)
from .batch_admission import (
    AdmissionCapacityExceeded, BatchAdmissionCoordinator, DurableQueueDepth,
    _bytes,
)

_log = logging.getLogger(__name__)


class CollectorRejected(AdmissionConflict):
    """尚未选中且确定未送交协调器的本地拒收。"""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__("batch collector rejected: " + reason)


@dataclass(frozen=True)
class CollectorSnapshot:
    waiting_items: int
    waiting_bytes: int
    inflight_items: int
    inflight_bytes: int
    peak_waiting_items: int
    peak_waiting_bytes: int
    peak_inflight_items: int
    peak_inflight_bytes: int
    peak_total_items: int
    peak_total_bytes: int
    oldest_waiting_seconds: float
    # flushed_* 为"尝试"计数（accept_batch 确认前递增；语义钉见 __init__，W2 批41）
    flushed_batches: int
    flushed_items: int
    flushed_bytes: int
    last_batch_items: int
    last_batch_bytes: int
    max_batch_items: int
    max_batch_bytes: int
    queue_wait_count: int
    queue_wait_seconds_total: float
    queue_wait_seconds_max: float
    closed: bool
    failed: bool
    worker_alive: bool
    flushed_signal: int
    # W-A 族③(b)-2（A1-005）：reset 重建次数（additive 尾字段，审计连续
    # 性口径延伸——重建可见；默认 0 现役构造点零回归）。
    resets: int = 0
    # P0-T6 队列告警层越层计数（additive 尾字段；越层一次计一，回落复位
    # 不清零——审计连续口径，默认 0 既有观察者零回归）。
    depth_warnings: int = 0
    # P1a-T4 durable 队列深度联动观测（additive 尾字段；queue_depth_face
    # 未接线恒 None/0——既有观察者零回归）。durable_tier_events=档位
    # 迁入 warning/critical/hard 的累计次数（审计连续口径）。
    durable_depth: int | None = None
    durable_tier: str | None = None
    durable_tier_events: int = 0


@dataclass(eq=False)
class _Waiting:
    request: AdmissionRequest
    size: int
    queued_at: float
    ready: Event = field(default_factory=Event)
    selected: bool = False
    receipt: AdmissionReceipt | None = None
    error: Exception | None = None


def _request_size(request: AdmissionRequest) -> int:
    # 预留持久条目的时间、摘要与键值开销。
    payload = {
        "scope_id": request.scope_id,
        "request_id": request.request_id,
        "item_id": request.item_id,
        "text": request.text,
        "delivery_route_ref": request.delivery_route_ref,
        "trace_id": request.trace_id,
        "schema_version": request.schema_version,
        "pipeline_version": request.pipeline_version,
        "embedding_space_id": request.embedding_space_id,
    }
    return len(_bytes(payload)) + 1024


class BatchAdmissionCollector:
    def __init__(
        self, coordinator: BatchAdmissionCoordinator, *,
        max_wait_seconds: float = 0.02,
        max_batch_bytes: int | None = None,
        max_queue_items: int = 512,
        max_queue_bytes: int = 4 * 1024 * 1024,
        warning_depth: int = 400,
        request_timeout_seconds: float = 5,
        close_timeout_seconds: float = 5,
        queue_depth_face: Callable[[], DurableQueueDepth | None] | None = None,
    ) -> None:
        # P0-T6 参数分层（域层默认值，主窗令）：
        # - max_wait_seconds 0.1→0.02（更快合批节奏——短等待摊薄单条时延）；
        # - 队列两梯：warning_depth=400（告警层，越层告警一次）+
        #   max_queue_items=512（硬层，queue_full 拒收）——与 durable log
        #   硬容量（BatchLimits.max_log_items=512）同梯，内存缓冲与持久
        #   缓冲对齐；
        # - 告警层高于硬层时惰性（永不触达，不视为配置错误——fail-soft，
        #   既有小容量构造点零回归）。
        if not 0 < max_wait_seconds <= 0.1:
            raise ValueError("batch wait must be in (0, 100ms]")
        if any(not isfinite(value) or not 0 < value <= TIMEOUT_MAX
               for value in (request_timeout_seconds, close_timeout_seconds)):
            raise ValueError("collector timeouts must be positive and within platform bounds")
        if warning_depth < 1:
            raise ValueError("warning depth must be positive")
        self.coordinator = coordinator
        self.max_wait_seconds = max_wait_seconds
        self.warning_depth = warning_depth
        self.max_batch_bytes = min(
            coordinator.limits.max_batch_bytes if max_batch_bytes is None else max_batch_bytes,
            coordinator.limits.max_batch_bytes,
        )
        if min(self.max_batch_bytes, max_queue_items, max_queue_bytes,
               coordinator.limits.max_batch_items) <= 0:
            raise ValueError("collector limits must be positive")
        self.max_queue_items = max_queue_items
        self.max_queue_bytes = max_queue_bytes
        self.request_timeout_seconds = request_timeout_seconds
        self.close_timeout_seconds = close_timeout_seconds
        self._condition = Condition()
        self._waiting: deque[_Waiting] = deque()
        self._waiting_bytes = 0
        self._inflight: list[_Waiting] = []
        self._inflight_bytes = 0
        self._peak_waiting_items = self._peak_waiting_bytes = 0
        self._peak_inflight_items = self._peak_inflight_bytes = 0
        self._peak_total_items = self._peak_total_bytes = 0
        # W2 批41（F7 计数语义钉）：flushed_* 计"尝试"非"成功"——_invoke 在
        # coordinator.accept_batch 网络往返**之前**递增；失败/unknown 批次已计入。
        # 已确认受理语义须对拍 store 侧持久证据，不得把本计数当成功受理数。
        self._flushed_batches = self._flushed_items = self._flushed_bytes = 0
        self._last_batch_items = self._last_batch_bytes = 0
        self._max_batch_items = self._max_batch_bytes = 0
        self._queue_wait_count = 0
        self._queue_wait_seconds_total = self._queue_wait_seconds_max = 0.0
        self._closed = False
        self._failed = False
        # W-A 族③(b)-1（A1-005）：reset 重建计数（审计连续——不清零）。
        self._resets = 0
        # P0-T6 队列告警层（warning_depth）越层计量：_note_depth_warning
        # 越层告警一次并置位；回落（下一 accept 观察到低于告警层）复位。
        self._depth_warned = False
        self._depth_warnings = 0
        # P1a-T4 容量数据源换队深度（蓝图 §1.4）：queue_depth_face 返回
        # 协调器深度计数器（durable 档位）——hard 档本地快拒 queue_full
        # （省一轮 ES 往返；复用身份在该窗同样被拒=既有内存 queue_full
        # 同型取舍，协调器闸"已受理身份永不 429"铁律不受影响），warning/
        # critical 档位观测入 snapshot（additive 尾字段）。未接线（缺省
        # None）=零行为 diff；内存等待队列档（512/4MB+告警 400）独立
        # 保留。观测面 fail-soft：face 异常按 None（权威闸在协调器锁内）。
        self._queue_depth_face = queue_depth_face
        self._durable_depth: int | None = None
        self._durable_tier: str | None = None
        self._durable_tier_events = 0
        self._durable_tier_last: str | None = None
        # 异步物化器订阅：每个成功 flush（202 已确认）的批次触发一次，
        # 物化器无需再以固定间隔轮询；仍未消除的事件由后续兜底短超时捕获。
        self.batch_flushed = Event()
        self._flushed_signal = 0
        self._thread = Thread(target=self._run, name="batch-admission-collector", daemon=True)
        self._thread.start()

    def _peaks(self) -> None:
        self._peak_waiting_items = max(self._peak_waiting_items, len(self._waiting))
        self._peak_waiting_bytes = max(self._peak_waiting_bytes, self._waiting_bytes)
        self._peak_inflight_items = max(self._peak_inflight_items, len(self._inflight))
        self._peak_inflight_bytes = max(self._peak_inflight_bytes, self._inflight_bytes)
        self._peak_total_items = max(
            self._peak_total_items, len(self._waiting) + len(self._inflight),
        )
        self._peak_total_bytes = max(
            self._peak_total_bytes, self._waiting_bytes + self._inflight_bytes,
        )

    def _queue_elapsed(self, item: _Waiting) -> None:
        elapsed = max(0.0, monotonic() - item.queued_at)
        self._queue_wait_count += 1
        self._queue_wait_seconds_total += elapsed
        self._queue_wait_seconds_max = max(self._queue_wait_seconds_max, elapsed)

    def _note_depth_warning(self) -> None:
        """P0-T6 队列告警层（越层一次；回落复位——须持 condition 锁调用）。

        深度=waiting+inflight（与 queue_full 硬层同一口径）。越层告警一次
        （深度回落后再次越层重新告警）；计量入 snapshot.depth_warnings
        （有界审计位，回落复位**不**清零）。告警层高于硬层时惰性（永不
        触达——fail-soft，小容量构造点零回归）。
        """
        depth = len(self._waiting) + len(self._inflight)
        if depth >= self.warning_depth:
            if not self._depth_warned:
                self._depth_warned = True
                self._depth_warnings += 1
                _log.warning(
                    "collector queue depth %d reached warning tier "
                    "(warning_depth=%d, hard capacity=%d)",
                    depth, self.warning_depth, self.max_queue_items)
        else:
            self._depth_warned = False

    def _observe_durable_tier(self) -> None:
        """durable 深度档位观测+hard 档本地快拒（须持 condition 锁调用）。

        只读计数器（int 读原子）——权威闸门在协调器受理锁内
        ``_durable_depth``；本面为预拒收/观测性质（省一轮 ES 往返）。
        face 异常按 None 处理（fail-soft：观测面不得反噬受理）。
        """
        counter = None
        try:
            counter = self._queue_depth_face()
        except Exception:
            counter = None
        if counter is None:
            return
        depth = counter.depth()
        tier = counter.tier()
        self._durable_depth, self._durable_tier = depth, tier
        if tier != self._durable_tier_last:
            self._durable_tier_last = tier
            if tier != "normal":
                self._durable_tier_events += 1
                _log.warning(
                    "collector observed durable queue tier %s (depth=%d)",
                    tier, depth)
        if tier == "hard":
            raise CollectorRejected("queue_full")

    def _finish(self, item: _Waiting, *, receipt: AdmissionReceipt | None = None,
                error: Exception | None = None) -> None:
        # 迟到的确认不能改写调用者已经拿到的 unknown。
        if not item.ready.is_set():
            item.receipt = receipt
            item.error = error
            item.ready.set()

    def _reject_waiting(self, reason: str) -> None:
        while self._waiting:
            item = self._waiting.popleft()
            self._waiting_bytes -= item.size
            self._queue_elapsed(item)
            self._finish(item, error=CollectorRejected(reason))

    def accept(self, request: AdmissionRequest) -> AdmissionReceipt:
        started = monotonic()
        deadline = started + self.request_timeout_seconds
        size = _request_size(request)
        if size > self.max_batch_bytes:
            raise CollectorRejected("request_too_large")
        waiting = _Waiting(request=request, size=size, queued_at=started)
        with self._condition:
            if self._closed:
                raise CollectorRejected("worker_failed" if self._failed else "closed")
            if self._queue_depth_face is not None:
                self._observe_durable_tier()
            if (len(self._waiting) + len(self._inflight) >= self.max_queue_items or
                    self._waiting_bytes + self._inflight_bytes + size > self.max_queue_bytes):
                raise CollectorRejected("queue_full")
            self._waiting.append(waiting)
            self._waiting_bytes += size
            self._peaks()
            self._note_depth_warning()
            self._condition.notify_all()
        waiting.ready.wait(max(0.0, deadline - monotonic()))
        with self._condition:
            if not waiting.ready.is_set():
                if not waiting.selected:
                    self._waiting.remove(waiting)
                    self._waiting_bytes -= size
                    self._queue_elapsed(waiting)
                    self._finish(waiting, error=CollectorRejected("queue_timeout"))
                else:
                    self._finish(waiting, error=AdmissionUnknown("selected request timed out"))
                self._condition.notify_all()
            if waiting.error is not None:
                raise waiting.error
            assert waiting.receipt is not None
            return waiting.receipt

    def _select(self) -> list[_Waiting]:
        with self._condition:
            while not self._closed:
                if not self._waiting:
                    self._condition.wait()
                    continue
                selected: list[_Waiting] = []
                size = 0
                for item in self._waiting:
                    if (len(selected) == self.coordinator.limits.max_batch_items or
                            size + item.size > self.max_batch_bytes):
                        break
                    selected.append(item)
                    size += item.size
                remaining = self._waiting[0].queued_at + self.max_wait_seconds - monotonic()
                if (len(selected) < self.coordinator.limits.max_batch_items and
                        len(selected) == len(self._waiting) and remaining > 0):
                    # 撤队会改变队首，唤醒后必须重新选取。
                    self._condition.wait(remaining)
                    continue
                for item in selected:
                    item.selected = True
                    self._inflight.append(item)
                    self._inflight_bytes += item.size
                    self._waiting.popleft()
                    self._waiting_bytes -= item.size
                    self._queue_elapsed(item)
                self._peaks()
                return selected
            return []

    def _invoke(self, selected: list[_Waiting]) -> list[AdmissionReceipt]:
        with self._condition:
            if (self._closed or any(item.ready.is_set() for item in selected) or
                    any(isinstance(item.error, AdmissionUnknown) for item in self._inflight)):
                raise AdmissionUnknown("selected batch stopped before further admission")
            size = sum(item.size for item in selected)
            self._flushed_batches += 1
            self._flushed_items += len(selected)
            self._flushed_bytes += size
            self._last_batch_items, self._last_batch_bytes = len(selected), size
            self._max_batch_items = max(self._max_batch_items, len(selected))
            self._max_batch_bytes = max(self._max_batch_bytes, size)
        receipts = self.coordinator.accept_batch([item.request for item in selected])
        if (not isinstance(receipts, list) or len(receipts) != len(selected) or
                not all(isinstance(receipt, AdmissionReceipt) for receipt in receipts)):
            raise AdmissionUnknown("batch receipt response is invalid")
        # 触发异步物化器；多次 flush 只递增计数，由物化器自己清。
        # W2 批79（L-8）：计数递增与 Event set 移入 condition 锁——snapshot 在锁内
        # 读 _flushed_signal，锁外变更可致观察者见到"计数已增/事件未置"中间态
        # （探针 log/temp/winw2fd-probe-collector-signal.json 实证旧码锁外调用）。
        with self._condition:
            self._flushed_signal += 1
            self.batch_flushed.set()
        return receipts

    def _flush(self, selected: list[_Waiting]) -> None:
        try:
            receipts = self._invoke(selected)
        except (IdentityConflict, AdmissionCapacityExceeded) as error:
            if len(selected) == 1:
                with self._condition:
                    self._finish(selected[0], error=error)
                return
            # 仅这两类证明本次整批未 CAS；每项最多重提一次，保留 FIFO 和原身份。
            # 容量不足不能抹去混批中已有受理，重提仍由协调器返回原 receipt。
            for item in selected:
                try:
                    receipt = self._invoke([item])[0]
                except (IdentityConflict, AdmissionCapacityExceeded) as rejection:
                    with self._condition:
                        self._finish(item, error=rejection)
                else:
                    with self._condition:
                        self._finish(item, receipt=receipt)
            return
        with self._condition:
            for item, receipt in zip(selected, receipts):
                self._finish(item, receipt=receipt)

    def _run(self) -> None:
        try:
            while True:
                selected = self._select()
                if not selected:
                    return
                self._flush(selected)
                with self._condition:
                    self._inflight.clear()
                    selected.clear()
                    self._inflight_bytes = 0
                    self._condition.notify_all()
        except BaseException as error:
            if isinstance(error, (AdmissionConflict, AdmissionUnknown)):
                outcome = error
            else:
                outcome = AdmissionUnknown("batch collector worker failed")
                outcome.__cause__ = error
            with self._condition:
                self._failed = self._closed = True
                self._reject_waiting("worker_failed")
                for item in self._inflight:
                    self._finish(item, error=outcome)
                self._condition.notify_all()
        finally:
            with self._condition:
                self._inflight.clear()
                self._inflight_bytes = 0
                self._condition.notify_all()

    def snapshot(self) -> CollectorSnapshot:
        with self._condition:
            oldest = max(0.0, monotonic() - self._waiting[0].queued_at) if self._waiting else 0.0
            return CollectorSnapshot(
                waiting_items=len(self._waiting), waiting_bytes=self._waiting_bytes,
                inflight_items=len(self._inflight), inflight_bytes=self._inflight_bytes,
                peak_waiting_items=self._peak_waiting_items,
                peak_waiting_bytes=self._peak_waiting_bytes,
                peak_inflight_items=self._peak_inflight_items,
                peak_inflight_bytes=self._peak_inflight_bytes,
                peak_total_items=self._peak_total_items, peak_total_bytes=self._peak_total_bytes,
                oldest_waiting_seconds=oldest,
                flushed_batches=self._flushed_batches, flushed_items=self._flushed_items,
                flushed_bytes=self._flushed_bytes,
                last_batch_items=self._last_batch_items, last_batch_bytes=self._last_batch_bytes,
                max_batch_items=self._max_batch_items, max_batch_bytes=self._max_batch_bytes,
                queue_wait_count=self._queue_wait_count,
                queue_wait_seconds_total=self._queue_wait_seconds_total,
                queue_wait_seconds_max=self._queue_wait_seconds_max,
                closed=self._closed, failed=self._failed, worker_alive=self._thread.is_alive(),
                flushed_signal=self._flushed_signal, resets=self._resets,
                depth_warnings=self._depth_warnings,
                durable_depth=self._durable_depth,
                durable_tier=self._durable_tier,
                durable_tier_events=self._durable_tier_events,
            )

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._reject_waiting("closed")
            for item in self._inflight:
                self._finish(item, error=AdmissionUnknown("collector closed with selected request"))
            self._condition.notify_all()
        # 线程存活不等于写权隔离，迟到写入仍须同身份对账。
        if current_thread() is not self._thread:
            self._thread.join(self.close_timeout_seconds)

    def reset(self) -> None:
        """W-A 族③(b)（A1-005）：unknown 停机后的显式重建。

        前置（fail-closed）：`_closed` 为 False → ValueError（活着的收集器
        reset=编程错误）；worker 线程仍活 → ValueError（inflight 可能仍在
        写，禁双 worker）；`_failed=False` 的正常 close 同样允许（语义=
        重建，不限于故障）。动作：清 _failed/_closed、_resets+1（审计
        连续——重建次数可见）、log.warning 留痕、新 worker 线程启动。

        语义钉：等待队列/inflight 在停机时已全量拒绝（unknown 语义不重放
        ——"迟到确认不改写 unknown"纪律）；flushed_*/_peak_*/_queue_wait_*
        计数保留不清零（:118-120 审计连续性口径延伸）。调用方纪律：accept
        收 CollectorRejected("worker_failed") 后由上层决定 reset 或放弃
        ——reset 不自动发生（自动重建会把未知确认状态批静默重放）。
        """
        with self._condition:
            if not self._closed:
                raise ValueError("collector reset requires closed state")
            if self._thread.is_alive():
                raise ValueError("collector reset requires a dead worker thread")
            self._failed = False
            self._closed = False
            self._resets += 1
            _log.warning("batch admission collector reset (#%d)", self._resets)
            self._thread = Thread(target=self._run,
                                  name="batch-admission-collector", daemon=True)
            self._thread.start()

    def __enter__(self) -> BatchAdmissionCollector:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
