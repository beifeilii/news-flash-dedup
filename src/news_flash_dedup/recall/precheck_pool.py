"""B+ 第三步核心件③：并行预检池——保序并发执行器（2026-10-08 主窗口）。

语义（吞吐方案-最终方案 v3 §三.1 + 外部终审竞态口径）：
- 并发：N 件的准备后相位（召回+逐对比对，只读）同时在飞——吞吐来源；
- 保序：完成顺序无论如何，**交付给提交相位的顺序恒为提交（arrival_seq）
  顺序**——"判定结果与并行度无关"在执行层的结构性保证（提交相位按序
  重构候选集时，比它早的必然已提交可见，与串行版逐字节同前提）；
- 故障：任一件失败=该件携带异常交付（预检池不吞错、不阻塞后继——失败件
  由提交相位按现役"failed 不推进"语义收容，其余件不受影响）。

本模块只做并发编排（通用内核，不绑业务类型）：提交方按序 submit，
收获方按提交序 drain——内部按完成度缓冲。等价于"并行 map + 保序收集"。
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class PoolItem:
    """一件预检产出：sequence=提交序（arrival_seq）；result 与 error
    恰一非 None（error 携带异常对象，由提交相位收容处置）。"""

    sequence: int
    result: Any = None
    error: BaseException | None = None


class PrecheckPool:
    """保序并发执行器：按提交序收货，与完成序/并行度无关。"""

    def __init__(self, workers: int, fn: Callable[[Any], Any]) -> None:
        if workers < 1:
            raise ValueError("workers must be >= 1")
        self._fn = fn
        self._executor = ThreadPoolExecutor(max_workers=workers)
        self._lock = threading.Lock()
        self._done: dict[int, PoolItem] = {}   # 完成缓冲（乱序到达）
        self._next_emit: int | None = None     # 下一个待交付序号
        self._closed = False

    def submit(self, sequence: int, payload: Any) -> None:
        """按 arrival_seq 序提交（调用侧=受理序单源）。首个 submit 锚定
        交付起点；序号必须严格递增（受理序违例=接线错误 fail-closed）。"""
        with self._lock:
            if self._closed:
                raise RuntimeError("submit after close")
            if self._next_emit is None:
                self._next_emit = sequence
            elif sequence < self._next_emit:
                raise ValueError(
                    f"sequence regressed: {sequence} < {self._next_emit}")
        # 线程池提交与回调注册必须在锁外：future 可能在 add_done_callback
        # 前已完成（回调立即在当前线程执行并取锁）——锁内注册=自死锁。
        future = self._executor.submit(self._fn, payload)

        def _capture(fut, seq=sequence):
            try:
                item = PoolItem(sequence=seq, result=fut.result())
            except BaseException as exc:  # 异常随件交付，不阻塞后继
                item = PoolItem(sequence=seq, error=exc)
            with self._lock:
                self._done[seq] = item

        future.add_done_callback(_capture)

    def drain_one(self, *, timeout: float | None = None) -> PoolItem | None:
        """按提交序交付下一件；尚未完成 → None（非阻塞语义由调用侧
        轮询/等待策略决定；timeout 保留给提交相位的闸门等待）。"""
        del timeout  # 首版非阻塞；提交相位自带闸门循环
        with self._lock:
            if self._next_emit is None:
                return None
            item = self._done.pop(self._next_emit, None)
            if item is None:
                return None
            self._next_emit += 1
            return item

    def pending_count(self) -> int:
        """已提交未交付（含在飞+缓冲）——闸门/背压观测面。"""
        with self._lock:
            return len(self._done)

    def close(self) -> None:
        with self._lock:
            self._closed = True
        self._executor.shutdown(wait=True)


__all__ = ["PoolItem", "PrecheckPool"]
