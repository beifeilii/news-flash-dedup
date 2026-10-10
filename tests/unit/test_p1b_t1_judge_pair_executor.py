"""P1b 任务一 · 判官候选对并发执行器单元钉（decide/judge_pair_executor.py）。

钉值面（任务书逐条）：
- 并发上限钉：pair_concurrency 工人池上限（屏障定钉：恰 N 对同时在判，
  少则屏障超时、多则不可能——工人只有 N 个）；
- 限流钉：HTTP 总并发独立信号量（http_concurrency 与对池两把尺子分开：
  对池 2 对 × 双序 4 路 HTTP，HTTP 上限 2 恰收口；峰值台账直钉）；
- 失败/超时隔离钉：坏对（抛错/慢死）只死自己，池内其他对全 ok；超时释
  等待槽位不连坐后续对；
- 背压钉：in-flight 窗口（工人+等位槽）满 → 拒新（status=rejected、
  台账计数，不抛错不连坐）；
- 顺序对应钉：结果序恒等输入序（乱序完成也不乱回）；
- 双序钉：每对恰 2 路 HTTP（AB/BA），call_fn 所见 kwargs 逐次恰为
  单对单序 render 产物（调日志集合对拍=禁拼对的结构钉）；单序失败
  不连坐另一序；combine 自定义装配；
- 构造校验钉：20/24/40 默认钉 + 越窗值 ValueError。

全部 mock 传输层（pair_fn/call_fn 为罐装函数），零真 LLM/零真 HTTP。
"""

from __future__ import annotations

import threading
import time

import pytest

from news_flash_dedup.decide.judge_pair_executor import (
    DEFAULT_HTTP_CONCURRENCY,
    DEFAULT_MAX_PAIR_CONCURRENCY,
    DEFAULT_PAIR_CONCURRENCY,
    DualOrderOutcome,
    JudgePairExecutor,
    JudgePairRequest,
    STATUS_ERROR,
    STATUS_OK,
    STATUS_REJECTED,
    STATUS_TIMEOUT,
)


def _req(i: int) -> JudgePairRequest:
    return JudgePairRequest(pair_id=f"p{i}", text_history=f"历史{i}",
                            text_current=f"现文{i}")


def _requests(n: int) -> list[JudgePairRequest]:
    return [_req(i) for i in range(n)]


# ---------------------------------------------------------------- 默认值 / 构造校验

def test_defaults_pinned_to_task_brief():
    # 任务书钉值：20 / 24 / 40
    assert DEFAULT_PAIR_CONCURRENCY == 20
    assert DEFAULT_MAX_PAIR_CONCURRENCY == 24
    assert DEFAULT_HTTP_CONCURRENCY == 40
    executor = JudgePairExecutor(lambda r: None)
    assert executor.pair_concurrency == 20
    assert executor.max_pair_concurrency == 24
    assert executor.http_concurrency == 40
    assert executor.window is None            # 缺省无界窗口
    assert executor.ledger.snapshot() == {
        "submitted": 0, "rejected": 0, "completed": 0, "timeouts": 0,
        "errors": 0, "pairs_peak": 0, "http_peak": 0}


def test_constructor_validation_fail_closed():
    def good(_request: JudgePairRequest) -> None:
        return None

    with pytest.raises(TypeError):
        JudgePairExecutor("not-callable")
    with pytest.raises(ValueError):
        JudgePairExecutor(good, pair_concurrency=0)
    with pytest.raises(ValueError):
        JudgePairExecutor(good, pair_concurrency=5, max_pair_concurrency=4)
    with pytest.raises(ValueError):
        JudgePairExecutor(good, max_pair_concurrency=0)
    with pytest.raises(ValueError):
        JudgePairExecutor(good, http_concurrency=0)
    with pytest.raises(ValueError):
        JudgePairExecutor(good, pair_timeout_s=0)
    with pytest.raises(ValueError):
        JudgePairExecutor(good, pair_timeout_s=-1.0)
    with pytest.raises(ValueError):
        JudgePairExecutor(good, queue_capacity=-1)
    # 合法边界：1..max 与超时/窗口取值均可
    JudgePairExecutor(good, pair_concurrency=1, max_pair_concurrency=1,
                      http_concurrency=1, pair_timeout_s=0.05,
                      queue_capacity=0)


def test_request_pair_id_is_correlation_key():
    with pytest.raises(ValueError):
        JudgePairRequest(pair_id="", text_history="a", text_current="b")


# ---------------------------------------------------------------- 并发上限钉

def test_pair_concurrency_cap_barrier_pinned():
    """屏障定钉：pair_concurrency=3 → 恰 3 对同时在判（少于 3 屏障超时
    翻车为单对 error；多于 3 结构不可能——工人只有 3 个）。12 对全部 ok。"""
    concurrency = 3
    barrier = threading.Barrier(concurrency, timeout=5.0)

    def pair_fn(request: JudgePairRequest):
        barrier.wait()
        return f"ok:{request.pair_id}"

    executor = JudgePairExecutor(pair_fn, pair_concurrency=concurrency,
                                 max_pair_concurrency=4)
    results = executor.execute(_requests(12))
    assert [r.status for r in results] == [STATUS_OK] * 12
    assert [r.value for r in results] == [f"ok:p{i}" for i in range(12)]
    assert executor.ledger.pairs_peak == concurrency
    assert executor.ledger.snapshot()["submitted"] == 12


def test_window_is_workers_plus_queue_capacity():
    """窗口语义钉：window = pair_concurrency + queue_capacity。"""
    executor = JudgePairExecutor(
        lambda r: None, pair_concurrency=7, queue_capacity=5)
    assert executor.window == 12
    executor = JudgePairExecutor(
        lambda r: None, pair_concurrency=7, queue_capacity=0)
    assert executor.window == 7


# ---------------------------------------------------------------- 限流钉（HTTP 独立信号量）

def test_http_semaphore_cap_direct_pinned():
    """HTTP 信号量独立执法：8 线程并发 http_call，上限 2 恰收口（屏障 2）。"""
    http_concurrency = 2
    barrier = threading.Barrier(http_concurrency, timeout=5.0)
    executor = JudgePairExecutor(lambda r: None,
                                 http_concurrency=http_concurrency)

    def transport(index: int) -> int:
        barrier.wait()
        return index

    outcomes: list[int] = []
    threads = [
        threading.Thread(target=lambda i=i: outcomes.append(
            executor.http_call(transport, i)))
        for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(outcomes) == list(range(8))
    assert executor.ledger.http_peak == http_concurrency


def test_http_window_context_releases_slot():
    seen: list[str] = []
    executor = JudgePairExecutor(lambda r: None, http_concurrency=1)
    with executor.http_window():
        seen.append("in")
    # 窗口退出后信号量可再进（上锁未泄）
    assert executor.http_call(lambda: "again") == "again"
    assert seen == ["in"]


def test_dual_order_http_capped_below_pair_pool():
    """两把尺子分开钉：对池 2 对并发 × 每对 2 路 HTTP = 潜在 4 路；
    http_concurrency=2 收口——HTTP 峰值恰 2，调用总数恰 4（2 对 × AB/BA）。"""
    pair_concurrency, http_concurrency = 2, 2
    barrier = threading.Barrier(http_concurrency, timeout=5.0)
    calls: list[dict] = []
    calls_lock = threading.Lock()

    def call_fn(*, model, system, user, order):
        barrier.wait()
        with calls_lock:
            calls.append({"model": model, "system": system, "user": user,
                          "order": order})
        return f"content:{order}:{len(user)}"

    executor = JudgePairExecutor(lambda r: None,
                                 pair_concurrency=pair_concurrency,
                                 http_concurrency=http_concurrency)
    render = (lambda request, order: {
        "model": "m1", "system": "sys", "user": f"{request.pair_id}:{order}",
        "order": order})
    pair_fn = executor.dual_order_pair_fn(call_fn, render=render)
    results = executor.execute(_requests(2), pair_fn=pair_fn)
    assert [r.status for r in results] == [STATUS_OK, STATUS_OK]
    assert len(calls) == 4                       # 每对恰 2 路 HTTP
    assert executor.ledger.http_peak == http_concurrency
    # 每对 ab/ba 双序值一一对应（DualOrderOutcome 缺省 combine）
    for result in results:
        outcome = result.value
        assert isinstance(outcome, DualOrderOutcome)
        assert outcome.ab.error is None and outcome.ba.error is None
        assert outcome.ab.value.startswith("content:ab:")
        assert outcome.ba.value.startswith("content:ba:")


# ---------------------------------------------------------------- 失败 / 超时隔离钉

def test_error_isolation_single_pair_fails_others_ok():
    def pair_fn(request: JudgePairRequest):
        if request.pair_id == "p2":
            raise RuntimeError("boom: 判官调用失败")
        return f"ok:{request.pair_id}"

    executor = JudgePairExecutor(pair_fn, pair_concurrency=3)
    results = executor.execute(_requests(6))
    assert results[2].status == STATUS_ERROR
    assert "boom" in results[2].error
    others = [r for i, r in enumerate(results) if i != 2]
    assert all(r.status == STATUS_OK for r in others)
    assert all(r.value for r in others)
    assert executor.ledger.snapshot()["errors"] == 1
    assert executor.ledger.snapshot()["completed"] == 5


def test_timeout_isolation_slow_pair_does_not_poison_pool():
    """超时钉：慢对（0.4s > 0.1s 超时）单列 timeout；快对全部 ok；
    超时后等待槽位回收——后续排队对照常执行（不连坐）。"""
    def pair_fn(request: JudgePairRequest):
        if request.pair_id == "p0":
            time.sleep(0.4)                     # 慢死对
        return f"ok:{request.pair_id}"

    executor = JudgePairExecutor(pair_fn, pair_concurrency=1,
                                 pair_timeout_s=0.1)
    results = executor.execute(_requests(5))
    assert results[0].status == STATUS_TIMEOUT
    assert "超时" in results[0].error
    for result in results[1:]:
        assert result.status == STATUS_OK       # 后续对不受连坐
        assert result.value.startswith("ok:")
    snapshot = executor.ledger.snapshot()
    assert snapshot["timeouts"] == 1
    assert snapshot["completed"] == 4


# ---------------------------------------------------------------- 背压钉

def test_backpressure_window_full_rejects_and_counts():
    """背压钉：pair_concurrency=1 + queue_capacity=0 → 窗口=1；首对占窗，
    其余 4 对拒新（status=rejected + 台账计数），放行后首对正常完成。"""
    release = threading.Event()
    entered = threading.Event()

    def pair_fn(request: JudgePairRequest):
        entered.set()
        assert release.wait(timeout=5.0)
        return f"ok:{request.pair_id}"

    executor = JudgePairExecutor(pair_fn, pair_concurrency=1,
                                 queue_capacity=0)

    # 交付前先钩住首对进入（避免提交循环快于工人取活造成偶然拒新窗口）
    results: list = []

    def run() -> None:
        results.extend(executor.execute(_requests(5)))

    runner = threading.Thread(target=run, daemon=True)
    runner.start()
    assert entered.wait(timeout=5.0)            # 首对已占窗
    time.sleep(0.05)                            # 让提交循环走完
    release.set()
    runner.join(timeout=5.0)
    assert not runner.is_alive()

    assert results[0].status == STATUS_OK
    assert results[0].value == "ok:p0"
    rejected = results[1:]
    assert all(r.status == STATUS_REJECTED for r in rejected)
    assert [r.pair_id for r in rejected] == ["p1", "p2", "p3", "p4"]
    assert all("拒新" in (r.error or "") for r in rejected)
    snapshot = executor.ledger.snapshot()
    assert snapshot["submitted"] == 1
    assert snapshot["rejected"] == 4
    assert snapshot["completed"] == 1


def test_backpressure_queue_capacity_extends_window():
    """等位槽钉：queue_capacity=1 → 窗口=2（1 在判 + 1 等位），5 对中
    2 受理、3 拒新。"""
    release = threading.Event()
    entered = threading.Event()

    def pair_fn(request: JudgePairRequest):
        entered.set()
        assert release.wait(timeout=5.0)
        return f"ok:{request.pair_id}"

    executor = JudgePairExecutor(pair_fn, pair_concurrency=1,
                                 queue_capacity=1)
    results: list = []

    def run() -> None:
        results.extend(executor.execute(_requests(5)))

    runner = threading.Thread(target=run, daemon=True)
    runner.start()
    assert entered.wait(timeout=5.0)
    time.sleep(0.05)
    release.set()
    runner.join(timeout=5.0)
    assert [r.status for r in results[:2]] == [STATUS_OK, STATUS_OK]
    assert all(r.status == STATUS_REJECTED for r in results[2:])
    assert executor.ledger.snapshot()["rejected"] == 3


# ---------------------------------------------------------------- 顺序对应钉

def test_results_preserve_input_order_under_out_of_order_completion():
    """乱序完成钉：按 pair_id 决定耗时（p0 最慢），完成序乱，回序恒等。"""
    def pair_fn(request: JudgePairRequest):
        index = int(request.pair_id[1:])
        time.sleep(0.02 * (7 - index))          # p0 慢 → p7 快：完成序倒挂
        return f"ok:{request.pair_id}"

    executor = JudgePairExecutor(pair_fn, pair_concurrency=8)
    requests = _requests(8)
    results = executor.execute(reversed(requests))
    assert [r.pair_id for r in results] == [
        f"p{i}" for i in reversed(range(8))]
    assert [r.value for r in results] == [
        f"ok:p{i}" for i in reversed(range(8))]


def test_execute_empty_input_returns_empty():
    executor = JudgePairExecutor(lambda r: None)
    assert executor.execute([]) == []
    assert executor.ledger.snapshot()["submitted"] == 0


# ---------------------------------------------------------------- 双序工厂钉（每对 2 路 HTTP + 禁拼对）

def test_dual_order_two_http_per_pair_and_no_pair_merging():
    """每对恰 2 路 HTTP（AB/BA）；call_fn 所见 user 逐次恰为**单对单序**
    render 产物——调用日志集合与期望集恰等（多拼/漏拼/串对皆不等）。"""
    calls: list[tuple[str, str]] = []           # (pair_id, order)
    lock = threading.Lock()

    def render(request: JudgePairRequest, order: str) -> dict:
        # 单对单序提示词组装（业务侧权）：user 只含本对双侧正文
        return {"model": "m1", "system": "sys",
                "user": (f"{order}|{request.pair_id}|"
                         f"{request.text_history}|{request.text_current}")}

    def call_fn(*, model, system, user) -> str:
        order, pair_id, _, _ = user.split("|", 3)
        with lock:
            calls.append((pair_id, order))
        return f"{pair_id}:{order}"

    executor = JudgePairExecutor(lambda r: None, pair_concurrency=4,
                                  http_concurrency=4)
    pair_fn = executor.dual_order_pair_fn(call_fn, render=render)
    requests = _requests(6)
    results = executor.execute(requests, pair_fn=pair_fn)
    assert [r.status for r in results] == [STATUS_OK] * 6
    # 12 路调用 = 6 对 × 2 序，恰无多余（拼对会少调、串对会重复）
    assert len(calls) == 12
    assert set(calls) == {(f"p{i}", order)
                          for i in range(6) for order in ("ab", "ba")}
    for result in results:
        outcome = result.value
        assert outcome.ab.value == f"{result.pair_id}:ab"
        assert outcome.ba.value == f"{result.pair_id}:ba"
    assert executor.ledger.snapshot()["completed"] == 6


def test_dual_order_render_sees_exactly_one_request_and_order():
    seen: list[tuple[str, str]] = []

    def render(request: JudgePairRequest, order: str) -> dict:
        seen.append((request.pair_id, order))
        return {"user": f"{request.pair_id}:{order}"}

    executor = JudgePairExecutor(lambda r: None, pair_concurrency=1)
    pair_fn = executor.dual_order_pair_fn(
        lambda **kw: kw["user"], render=render)
    executor.execute(_requests(3), pair_fn=pair_fn)
    assert sorted(seen) == [(f"p{i}", order)
                            for i in range(3) for order in ("ab", "ba")]


def test_dual_order_one_order_failure_does_not_poison_other():
    """单序失败不连坐另一序：ba 抛错 → ab 仍 ok；error=原始异常实例。"""
    def render(request: JudgePairRequest, order: str) -> dict:
        return {"user": f"{request.pair_id}:{order}"}

    def call_fn(*, user) -> str:
        if user.endswith(":ba"):
            raise RuntimeError("ba 序传输失败")
        return f"content:{user}"

    executor = JudgePairExecutor(lambda r: None, pair_concurrency=2)
    pair_fn = executor.dual_order_pair_fn(call_fn, render=render)
    results = executor.execute(_requests(2), pair_fn=pair_fn)
    for result in results:
        assert result.status == STATUS_OK        # 对级成功（序级失败在 outcome）
        outcome: DualOrderOutcome = result.value
        assert outcome.ab.error is None
        assert outcome.ab.value == f"content:{result.pair_id}:ab"
        assert isinstance(outcome.ba.error, RuntimeError)
        assert str(outcome.ba.error) == "ba 序传输失败"


def test_dual_order_custom_combine():
    def render(request: JudgePairRequest, order: str) -> dict:
        return {"user": f"{request.pair_id}:{order}"}

    def combine(ab, ba) -> dict:
        return {"pair": f"{ab.value}/{ba.value}",
                "ab_ok": ab.error is None, "ba_ok": ba.error is None}

    executor = JudgePairExecutor(lambda r: None, pair_concurrency=1)
    pair_fn = executor.dual_order_pair_fn(
        lambda **kw: kw["user"], render=render, combine=combine)
    results = executor.execute(_requests(2), pair_fn=pair_fn)
    assert [r.value for r in results] == [
        {"pair": "p0:ab/p0:ba", "ab_ok": True, "ba_ok": True},
        {"pair": "p1:ab/p1:ba", "ab_ok": True, "ba_ok": True},
    ]


def test_dual_order_factory_validation():
    executor = JudgePairExecutor(lambda r: None)

    def render(_request: JudgePairRequest, _order: str) -> dict:
        return {}

    with pytest.raises(TypeError):
        executor.dual_order_pair_fn("not-callable", render=render)
    with pytest.raises(TypeError):
        executor.dual_order_pair_fn(lambda **kw: None, render="not-callable")
    with pytest.raises(TypeError):
        executor.dual_order_pair_fn(lambda **kw: None, render=render,
                                    combine="not-callable")


# ---------------------------------------------------------------- 复入守卫 / 错误摘要

def test_execute_reentrancy_guard():
    entered = threading.Event()
    release = threading.Event()

    def pair_fn(request: JudgePairRequest):
        entered.set()
        assert release.wait(timeout=5.0)
        return "ok"

    executor = JudgePairExecutor(pair_fn, pair_concurrency=1)
    first: list = []
    runner = threading.Thread(
        target=lambda: first.append(executor.execute([_req(0)])),
        daemon=True)
    runner.start()
    assert entered.wait(timeout=5.0)            # 首次执行占住
    with pytest.raises(RuntimeError):
        executor.execute([_req(1)])             # 复入即拒
    release.set()
    runner.join(timeout=5.0)
    assert first[0][0].status == STATUS_OK


def test_error_snippet_truncated_to_300_chars():
    marker = "x" * 2000

    def pair_fn(_request: JudgePairRequest):
        raise RuntimeError(marker)

    executor = JudgePairExecutor(pair_fn, pair_concurrency=1)
    results = executor.execute([_req(0)])
    assert results[0].status == STATUS_ERROR
    assert len(results[0].error) == 300          # 家法同款 300 字截断
    assert results[0].error == "x" * 300         # str(RuntimeError)=纯消息
