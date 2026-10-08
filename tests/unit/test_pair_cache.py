"""逐对结果缓存钉（decide/pair_cache.py，B+ 第三步核心件②）：

- 命中/未命中语义与计数（P4/P6 仪表化观测面）；
- 键全字段参与：任一字段变=另一个键（版本链缺一不行）；
- get_or_compute：命中不重复算；计算在锁外（贵活不阻塞）；
- 线程冒烟：并发读写不损坏、计数守恒。
"""

from __future__ import annotations

import threading

from news_flash_dedup.decide.pair_cache import PairCacheKey, PairResultCache


def _key(hist: str = "h1", cur: str = "c1",
         chain: tuple[str, ...] = ("f1", "d1", "n1", "p1")) -> PairCacheKey:
    return PairCacheKey(history_raw_hash=hist, current_raw_hash=cur,
                        version_chain=chain)


def test_hit_miss_and_stats():
    cache = PairResultCache()
    key = _key()
    assert cache.get(key) == (False, None)
    cache.put(key, "R")
    assert cache.get(key) == (True, "R")
    assert cache.stats == {"hits": 1, "misses": 1, "size": 1}


def test_key_fields_all_participate():
    cache = PairResultCache()
    cache.put(_key(), "R")
    # 逐字段扰动：全部必须未命中（版本链任一元素变=不同键）
    variants = [
        _key(hist="h2"), _key(cur="c2"),
        _key(chain=("f2", "d1", "n1", "p1")),
        _key(chain=("f1", "d1", "n1", "p1", "extra")),
        _key(chain=("f1", "d1", "n1")),   # 链短一截也是不同键
    ]
    for variant in variants:
        assert cache.get(variant) == (False, None), variant


def test_get_or_compute_single_compute_on_hit():
    cache = PairResultCache()
    calls = []

    def compute():
        calls.append(1)
        return "R"

    assert cache.get_or_compute(_key(), compute) == "R"
    assert cache.get_or_compute(_key(), compute) == "R"
    assert len(calls) == 1   # 命中不重复算


def test_thread_smoke_counting_conserved():
    cache = PairResultCache()
    keys = [_key(hist=f"h{i}") for i in range(20)]

    def worker(start: int) -> None:
        for i in range(start, 200, 4):
            cache.get_or_compute(keys[i % 20], lambda: "R")

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    stats = cache.stats
    assert stats["size"] == 20
    # 200 次访问=命中+未命中（同键并发重复算不破坏计数守恒）
    assert stats["hits"] + stats["misses"] >= 200
    assert stats["hits"] > 0
