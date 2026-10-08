"""B+ 第三步核心件②：逐对比对结果缓存（2026-10-08 主窗口）。

设计依据（吞吐方案-最终方案 v3 §三.1）：逐对比对是纯函数——结果只由
两侧文本与判定输入版本链决定，与查询时刻、可见状态、其他候选无关
（三方评审代码级实证：判定栈全程不读 decision_watermark）。因此缓存
永不会"过时"，唯一失效维度=版本链变化——键必须覆盖影响比对结果的
全部输入版本（facts/字典/normalizer/pipeline 全链，调用侧装配单源）。

并发：预检池多线程读写——dict 操作加锁；命中/未命中计数供 P4/P6 仪表化
（差集率与缓存命中是验收对拍的两个观测面）。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class PairCacheKey:
    """缓存键：两侧内容哈希 + 全版本链（缺一字段=另一个键）。"""

    history_raw_hash: str
    current_raw_hash: str
    version_chain: tuple[str, ...]   # facts/dict/normalizer/pipeline 全链


class PairResultCache:
    """线程安全的逐对结果缓存（进程内；持久化=生产化后续项）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: dict[PairCacheKey, Any] = {}
        self._hits = 0
        self._misses = 0

    def get(self, key: PairCacheKey) -> tuple[bool, Any]:
        """(True, result) 命中 / (False, None) 未命中。"""
        with self._lock:
            if key in self._data:
                self._hits += 1
                return True, self._data[key]
            self._misses += 1
            return False, None

    def put(self, key: PairCacheKey, result: Any) -> None:
        with self._lock:
            self._data[key] = result

    def get_or_compute(self, key: PairCacheKey, compute) -> Any:
        """命中即返；未命中现算并写入（计算在锁外——比对是贵活，
        不阻塞他线程；同键并发重复算可接受，结果时不变必然同值）。"""
        hit, value = self.get(key)
        if hit:
            return value
        value = compute()
        self.put(key, value)
        return value

    @property
    def stats(self) -> dict[str, int]:
        with self._lock:
            return {"hits": self._hits, "misses": self._misses,
                    "size": len(self._data)}


__all__ = ["PairCacheKey", "PairResultCache"]
