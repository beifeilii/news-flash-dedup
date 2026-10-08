"""并行预检池钉（recall/precheck_pool.py，B+ 第三步核心件③）：

- 保序：乱序完成 → 按提交序交付（并行度无关性的执行层保证）；
- 异常随件：失败件携带异常交付、不阻塞后继（提交相位收容）；
- 序号纪律：提交序回退/关闭后提交 → fail-closed；
- 并发真实性：慢件不挡快件完成，但出口仍按序。
"""

from __future__ import annotations

import time

import pytest

from news_flash_dedup.recall.precheck_pool import PrecheckPool


def test_ordered_delivery_despite_scrambled_completion():
    # 序号小的故意更慢——完成序必然乱，交付序必须仍是 1,2,3
    pool = PrecheckPool(workers=3,
                        fn=lambda p: (time.sleep(p), p)[1])
    pool.submit(1, 0.06)   # 最慢
    pool.submit(2, 0.02)
    pool.submit(3, 0.001)  # 最快
    out = []
    for _ in range(3):
        while True:
            item = pool.drain_one()
            if item is not None:
                out.append(item)
                break
            time.sleep(0.005)
    assert [i.sequence for i in out] == [1, 2, 3]
    assert [i.result for i in out] == [0.06, 0.02, 0.001]
    pool.close()


def test_error_rides_the_item_and_does_not_block():
    def fn(payload):
        if payload == "boom":
            raise RuntimeError("预检失败样例")
        return payload.upper()

    pool = PrecheckPool(workers=2, fn=fn)
    pool.submit(1, "boom")
    pool.submit(2, "ok")
    first, second = [], []
    while len(first) + len(second) < 2:
        item = pool.drain_one()
        if item is not None:
            (first if item.sequence == 1 else second).append(item)
        else:
            time.sleep(0.005)
    assert isinstance(first[0].error, RuntimeError)
    assert first[0].result is None
    assert second[0].result == "OK" and second[0].error is None
    pool.close()


def test_sequence_regression_refused():
    pool = PrecheckPool(workers=1, fn=lambda p: p)
    pool.submit(5, "a")
    with pytest.raises(ValueError):
        pool.submit(4, "b")
    pool.close()


def test_submit_after_close_refused():
    pool = PrecheckPool(workers=1, fn=lambda p: p)
    pool.close()
    with pytest.raises(RuntimeError):
        pool.submit(1, "a")


def test_workers_validation():
    with pytest.raises(ValueError):
        PrecheckPool(workers=0, fn=lambda p: p)


def test_concurrency_is_real():
    # 两件各睡 0.05s：并发总耗时应显著小于串行 0.1s
    pool = PrecheckPool(workers=2, fn=lambda p: (time.sleep(0.05), p)[1])
    start = time.monotonic()
    pool.submit(1, "a")
    pool.submit(2, "b")
    while pool.drain_one() is None:
        time.sleep(0.002)
    while pool.drain_one() is None:
        time.sleep(0.002)
    elapsed = time.monotonic() - start
    assert elapsed < 0.09
    pool.close()
