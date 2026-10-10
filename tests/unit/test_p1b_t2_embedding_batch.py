"""P1b 任务二 · Embedding 真批量微批网关单元钉（vector/embedding_batch.py）。

钉值面（任务书+主窗裁定逐条）：
- 构造校验钉：batch_size 窗 1..10（端点硬顶裁定，16~32 作废）缺省 8；
  max_wait 窗 0.020~0.050s 缺省 0.030；越界/非 int/非有限 → ValueError；
- 批量组装钉：满批即发（假钟冻结仍发=到量不等钟）；切批 9→[4][4][1]；
  余量以剩余最早条重新起窗；
- 超时即发钉（不凑批等待）：pending 不足一批 → max_wait 截止按**现有量**
  发（3 条就发 3 条，绝不为凑 4 硬等）；deadline 锚=最早未决条；
- 失败逐条回落钉：批调用失败（异常/响应形态不符）→ 逐条 [单文本] 重发
  ——好条成活、坏条只死该条（一批坏一条不死全批）；
- 顺序对应钉：逐位 outcome 位次=输入序；乱序完成/多调用方并发同池共批
  槽位回填不串号；
- 严格变体钉：strict/embed 失败即抛 EmbeddingBatchError（.failures 位次
  →异常，不吞错冒充）；空输入/非 str/关闭后拒新。

全部 mock 传输层（batch_call_fn 罐装），零真 LLM/零真网络。假钟
（FakeClock）控制 deadline 时序——满批路径不依赖钟，超时路径靠推进假钟
确定性触发。
"""

from __future__ import annotations

import threading
import time

import pytest

from news_flash_dedup.vector.embedding_batch import (
    DEFAULT_BATCH_SIZE,
    DEFAULT_MAX_WAIT_S,
    EmbeddingBatchConfig,
    EmbeddingBatchError,
    EmbeddingBatchMalformed,
    EmbeddingBatcher,
    MAX_BATCH_SIZE,
    MIN_BATCH_SIZE,
)


# ---------------------------------------------------------------- 测试工装

class FakeClock:
    """假钟：deadline 计算确定性可控（flusher 以 4ms 真时长哨醒拾钟）。"""

    def __init__(self, start: float = 100.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, delta: float) -> None:
        self.now += delta


def _vec(text: str) -> list[float]:
    """确定性逐文本向量（t-<i> → [i, i/2]）：顺序对应钉的身份锚。"""
    index = int(text.split("-")[1])
    return [float(index), float(index) / 2.0]


def _texts(n: int, start: int = 0) -> list[str]:
    return [f"t-{i}" for i in range(start, start + n)]


class RecordingTransport:
    """罐装传输：记录每次调用 texts；script 逐次弹出（Exception→抛出，
    其余→作为整个响应返回）；script 空 → 按 _vec 逐文本返回。"""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.script: list = []

    def push(self, item) -> "RecordingTransport":
        self.script.append(item)
        return self

    def __call__(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        if self.script:
            item = self.script.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        return [_vec(t) for t in texts]


def _batcher(transport, *, batch_size=4, max_wait_s=0.020, clock=None):
    return EmbeddingBatcher(
        transport, config=EmbeddingBatchConfig(batch_size=batch_size,
                                               max_wait_s=max_wait_s),
        clock=clock or FakeClock())


def _run_async(batcher, fn):
    """后台线程跑一次调用（超时路径需主线程推进假钟解锁）；box 收结果
    或异常（收 BaseException 供断言面）。"""
    box: dict = {}

    def run() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:   # noqa: BLE001 — 收容给断言面
            box["error"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, box


def _wait_calls(transport, count: int, timeout: float = 5.0) -> None:
    stop = time.monotonic() + timeout
    while time.monotonic() < stop and len(transport.calls) < count:
        time.sleep(0.002)


# ---------------------------------------------------------------- 构造校验钉

def test_config_defaults_pinned():
    assert MIN_BATCH_SIZE == 1 and MAX_BATCH_SIZE == 10   # 端点硬顶裁定
    assert DEFAULT_BATCH_SIZE == 8
    assert DEFAULT_MAX_WAIT_S == 0.030                    # 20~50ms 窗中值
    config = EmbeddingBatchConfig()
    assert config.batch_size == 8
    assert config.max_wait_s == 0.030


def test_config_validation_fail_closed():
    with pytest.raises(ValueError):
        EmbeddingBatchConfig(batch_size=0)
    with pytest.raises(ValueError):
        EmbeddingBatchConfig(batch_size=11)               # 端点硬顶 10
    with pytest.raises(ValueError):
        EmbeddingBatchConfig(batch_size=True)             # bool 冒充 int
    with pytest.raises(ValueError):
        EmbeddingBatchConfig(batch_size="8")
    with pytest.raises(ValueError):
        EmbeddingBatchConfig(max_wait_s=0.019)            # 20ms 下沿外
    with pytest.raises(ValueError):
        EmbeddingBatchConfig(max_wait_s=0.051)            # 50ms 上沿外
    with pytest.raises(ValueError):
        EmbeddingBatchConfig(max_wait_s=float("nan"))
    with pytest.raises(ValueError):
        EmbeddingBatchConfig(max_wait_s=float("inf"))
    with pytest.raises(ValueError):
        EmbeddingBatchConfig(max_wait_s=True)             # bool 冒充 float
    # 合法边界全开
    EmbeddingBatchConfig(batch_size=1, max_wait_s=0.020)
    EmbeddingBatchConfig(batch_size=10, max_wait_s=0.050)


def test_batcher_requires_callable_transport():
    with pytest.raises(TypeError):
        EmbeddingBatcher("not-callable")
    batcher = EmbeddingBatcher(lambda texts: [[0.0]] * len(texts))
    assert batcher.batch_size == DEFAULT_BATCH_SIZE
    assert batcher.max_wait_s == DEFAULT_MAX_WAIT_S
    batcher.close()


# ---------------------------------------------------------------- 批量组装钉

def test_full_batch_flushes_immediately_even_with_frozen_clock():
    """满批即发钉：假钟冻结（任何等钟路径永不触发）下恰 batch_size 条
    提交 → 立即单批发出——证明满批发不等 max_wait。"""
    clock = FakeClock()
    transport = RecordingTransport()
    batcher = _batcher(transport, batch_size=8, clock=clock)
    texts = _texts(8)
    outcomes = batcher.embed_documents(texts)
    assert all(o.ok for o in outcomes)
    assert transport.calls == [texts]                     # 单批全量
    snapshot = batcher.ledger.snapshot()
    assert snapshot["full_batches"] == 1
    assert snapshot["deadline_batches"] == 0
    assert snapshot["batches_sent"] == 1
    batcher.close()


def test_batch_assembly_splits_and_leftover_gets_fresh_window():
    """切批钉：9 条 batch_size=4 → [4][4] 满批即发 + [1] 超时即发；
    结果按序对应；余量条以剩余最早条重新起窗（推进假钟即发）。"""
    clock = FakeClock()
    transport = RecordingTransport()
    batcher = _batcher(transport, batch_size=4, clock=clock)
    texts = _texts(9)
    thread, box = _run_async(batcher, lambda: batcher.embed_documents(texts))
    _wait_calls(transport, 2)                             # 满批两发
    assert transport.calls == [texts[0:4], texts[4:8]]    # [4][4] 已发
    assert thread.is_alive()                              # 余量 1 条在等窗
    clock.advance(0.030)                                  # 越余量截止 → 即发
    _wait_calls(transport, 3)
    thread.join(timeout=2.0)
    assert not thread.is_alive()
    assert transport.calls == [texts[0:4], texts[4:8], texts[8:9]]
    assert [o.vector for o in box["value"]] == [_vec(t) for t in texts]
    snapshot = batcher.ledger.snapshot()
    assert snapshot["full_batches"] == 2
    assert snapshot["deadline_batches"] == 1
    batcher.close()


def test_single_call_embed_goes_through_microbatch():
    """单条便捷口：经微批机制（孤条按 max_wait 截止单发 [单文本]）。"""
    clock = FakeClock()
    transport = RecordingTransport()
    batcher = _batcher(transport, batch_size=4, clock=clock)
    thread, box = _run_async(batcher, lambda: batcher.embed("t-7"))
    time.sleep(0.05)                                      # 孤条起窗
    assert transport.calls == []                          # 未到截止不发
    clock.advance(0.030)                                  # 越截止 → 单发
    thread.join(timeout=2.0)
    assert not thread.is_alive()
    assert transport.calls == [["t-7"]]
    assert box["value"] == _vec("t-7")
    batcher.close()


# ---------------------------------------------------------------- 超时即发钉（不凑批等待）

def test_deadline_flushes_current_quantity_not_waiting_to_fill():
    """不凑批钉：3 条 pending（batch_size=4 永不凑满）→ max_wait 截止按
    现有量 3 条即发（单批恰 3 条）。"""
    clock = FakeClock()
    transport = RecordingTransport()
    batcher = _batcher(transport, batch_size=4, clock=clock)
    texts = _texts(3)
    thread, box = _run_async(batcher, lambda: batcher.embed_documents(texts))
    time.sleep(0.05)                                      # 确保已提交（t0 起窗）
    assert transport.calls == []                          # 未到截止不发
    clock.advance(0.030)                                  # 越过 t0+0.020 截止
    thread.join(timeout=2.0)
    assert not thread.is_alive()
    assert transport.calls == [texts]                     # 按现有量：恰 3 条
    assert batcher.ledger.snapshot()["deadline_batches"] == 1
    assert [o.vector for o in box["value"]] == [_vec(t) for t in texts]
    batcher.close()


def test_deadline_anchored_to_oldest_pending_entry():
    """deadline 锚钉：最早条 t0 起窗；后到条并入同批；越 t0+max_wait 全量发
    （若锚在新条上，本推进量不足以触发，线程必超时败测）。"""
    clock = FakeClock()
    transport = RecordingTransport()
    batcher = _batcher(transport, batch_size=10, max_wait_s=0.050,
                      clock=clock)
    first = _texts(2)
    thread_a, box_a = _run_async(
        batcher, lambda: batcher.embed_documents(first))
    time.sleep(0.05)                                      # 2 条 t0=100.0 起窗
    clock.advance(0.030)                                  # t=100.030
    thread_b, box_b = _run_async(
        batcher, lambda: batcher.embed_documents(["t-99"]))  # 新条 t=100.030
    time.sleep(0.05)
    clock.advance(0.031)                                  # t=100.061 > 100.050
    thread_a.join(timeout=2.0)
    thread_b.join(timeout=2.0)
    assert not thread_a.is_alive() and not thread_b.is_alive()
    # 全量 3 条一批（锚在最早条 t0：一次截止一起发）
    assert transport.calls == [first + ["t-99"]]
    assert [o.vector for o in box_a["value"]] == [_vec(t) for t in first]
    assert box_b["value"][0].vector == _vec("t-99")
    batcher.close()


# ---------------------------------------------------------------- 失败逐条回落钉

def test_batch_failure_falls_back_per_item_and_all_good_survive():
    """批失败回落钉：批调用抛错 → 逐条 [单文本] 重发；好条全成活；
    单发调用逐条恰为 [该条]（不再拼批）。"""
    clock = FakeClock()
    transport = RecordingTransport()
    transport.push(RuntimeError("batch dead"))
    batcher = _batcher(transport, batch_size=4, clock=clock)
    texts = _texts(3)
    thread, box = _run_async(batcher, lambda: batcher.embed_documents(texts))
    time.sleep(0.05)
    clock.advance(0.030)
    thread.join(timeout=2.0)
    assert not thread.is_alive()
    # 首发批（3 条）失败 → 逐条单发恰 3 次，每次恰 1 条
    assert transport.calls[0] == texts
    assert transport.calls[1:] == [[texts[0]], [texts[1]], [texts[2]]]
    assert [o.vector for o in box["value"]] == [_vec(t) for t in texts]
    snapshot = batcher.ledger.snapshot()
    assert snapshot["batch_failures"] == 1
    assert snapshot["fallback_batches"] == 1
    assert snapshot["fallback_single_calls"] == 3
    assert snapshot["item_failures"] == 0
    assert snapshot["batches_sent"] == 0                  # 批级零冒充成功
    batcher.close()


def test_one_bad_item_does_not_kill_the_batch():
    """一批坏一条不死全批钉：批失败回落后仅坏条死（error=原始异常实例），
    其余条照常成活。"""
    clock = FakeClock()
    transport = RecordingTransport()
    transport.push(RuntimeError("batch dead"))            # 批调用死
    transport.push([_vec("t-0")])                         # 单发好（整响应）
    transport.push(ValueError("bad item"))                # 单发死
    transport.push([_vec("t-2")])                         # 单发好
    batcher = _batcher(transport, batch_size=4, clock=clock)
    texts = _texts(3)
    thread, box = _run_async(batcher, lambda: batcher.embed_documents(texts))
    time.sleep(0.05)
    clock.advance(0.030)
    thread.join(timeout=2.0)
    outcomes = box["value"]
    assert outcomes[0].ok and outcomes[0].vector == _vec(texts[0])
    assert not outcomes[1].ok
    assert isinstance(outcomes[1].error, ValueError)
    assert str(outcomes[1].error) == "bad item"
    assert outcomes[2].ok and outcomes[2].vector == _vec(texts[2])
    assert batcher.ledger.snapshot()["item_failures"] == 1
    batcher.close()


def test_malformed_batch_response_falls_back_per_item():
    """响应形态不符（长度≠input）→ EmbeddingBatchMalformed → 逐条回落
    （确认面不冒充成功）；单发好条成活。"""
    clock = FakeClock()
    transport = RecordingTransport()
    transport.push([[0.0], [0.0]])                        # 3 条只回 2 项：畸形
    batcher = _batcher(transport, batch_size=4, clock=clock)
    texts = _texts(3)
    thread, box = _run_async(batcher, lambda: batcher.embed_documents(texts))
    time.sleep(0.05)
    clock.advance(0.030)
    thread.join(timeout=2.0)
    assert all(o.ok for o in box["value"])
    assert [o.vector for o in box["value"]] == [_vec(t) for t in texts]
    snapshot = batcher.ledger.snapshot()
    assert snapshot["malformed_batches"] == 1
    assert snapshot["batch_failures"] == 1
    assert snapshot["fallback_single_calls"] == 3
    batcher.close()


def test_malformed_single_fallback_item_error_is_typed():
    """回落单发也畸形 → 该条 error=EmbeddingBatchMalformed（不冒充）。"""
    clock = FakeClock()
    transport = RecordingTransport()
    transport.push(RuntimeError("batch dead"))
    transport.push([[0.0], [0.0]])                        # 单发回 2 项：畸形
    batcher = _batcher(transport, batch_size=4, clock=clock)
    thread, box = _run_async(batcher, lambda: batcher.embed_documents(["t-0"]))
    time.sleep(0.05)
    clock.advance(0.030)
    thread.join(timeout=2.0)
    outcome = box["value"][0]
    assert not outcome.ok
    assert isinstance(outcome.error, EmbeddingBatchMalformed)
    assert batcher.ledger.snapshot()["item_failures"] == 1
    batcher.close()


# ---------------------------------------------------------------- 顺序对应钉

def test_out_of_order_completion_keeps_positional_mapping():
    """乱序完成钉：后提交批先完成（快批超车慢批），两调用方结果仍按各自
    输入序一一对应（_vec 身份锚分家对拍）。"""
    clock = FakeClock()

    def transport(texts: list[str]) -> list[list[float]]:
        slow = "t-0" in texts                             # 慢批（先提交）
        time.sleep(0.05 if slow else 0.005)
        return [_vec(t) for t in texts]

    batcher = _batcher(transport, batch_size=4, clock=clock)
    results: dict[str, list] = {}

    def run(tag: str, texts: list[str]) -> None:
        results[tag] = batcher.embed_documents(texts)

    slow_texts, fast_texts = _texts(4), _texts(4, start=10)
    thread_slow = threading.Thread(target=run, args=("slow", slow_texts),
                                   daemon=True)
    thread_slow.start()
    time.sleep(0.01)                                      # 慢批先入池
    thread_fast = threading.Thread(target=run, args=("fast", fast_texts),
                                   daemon=True)
    thread_fast.start()
    thread_fast.join(timeout=5.0)                         # 快批先完成（超车）
    assert not thread_fast.is_alive()
    thread_slow.join(timeout=5.0)
    assert not thread_slow.is_alive()
    assert [o.vector for o in results["slow"]] == [_vec(t) for t in slow_texts]
    assert [o.vector for o in results["fast"]] == [_vec(t) for t in fast_texts]
    batcher.close()


def test_concurrent_callers_share_pool_without_cross_talk():
    """多调用方并发钉：两线程同池共批（合批不串号），各自结果按各自输入
    序一一对应；池内 8 条恰被覆盖一次。"""
    clock = FakeClock()
    calls: list[list[str]] = []
    lock = threading.Lock()

    def transport(texts: list[str]) -> list[list[float]]:
        with lock:
            calls.append(list(texts))
        time.sleep(0.01)                                  # 制造交错
        return [_vec(t) for t in texts]

    batcher = _batcher(transport, batch_size=8, clock=clock)
    texts_a = _texts(4, start=0)
    texts_b = _texts(4, start=10)
    results: dict[str, list] = {}

    def run(tag: str, texts: list[str]) -> None:
        results[tag] = batcher.embed_documents(texts)

    threads = [
        threading.Thread(target=run, args=("a", texts_a), daemon=True),
        threading.Thread(target=run, args=("b", texts_b), daemon=True),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5.0)
    assert all(not thread.is_alive() for thread in threads)
    # 全部 8 条恰被覆盖一次（合批/两批皆可，不丢不重不串）
    flattened = sorted(t for call in calls for t in call)
    assert flattened == sorted(texts_a + texts_b)
    assert [o.vector for o in results["a"]] == [_vec(t) for t in texts_a]
    assert [o.vector for o in results["b"]] == [_vec(t) for t in texts_b]
    batcher.close()


# ---------------------------------------------------------------- 严格变体 / 输入纪律 / 生命周期

def test_strict_raises_with_positional_failures():
    """严格变体钉：坏条 → EmbeddingBatchError，.failures 位次→原始异常。"""
    clock = FakeClock()
    transport = RecordingTransport()
    transport.push(RuntimeError("batch dead"))
    transport.push([_vec("t-0")])                         # 单发好
    transport.push(ValueError("bad item"))                # 单发死
    batcher = _batcher(transport, batch_size=4, clock=clock)
    thread, box = _run_async(
        batcher, lambda: batcher.embed_documents_strict(["t-0", "t-1"]))
    time.sleep(0.05)
    clock.advance(0.030)
    thread.join(timeout=2.0)
    error = box.get("error")
    assert isinstance(error, EmbeddingBatchError)
    assert set(error.failures) == {1}                     # 位次=输入序
    assert isinstance(error.failures[1], ValueError)
    assert str(error.failures[1]) == "bad item"
    batcher.close()


def test_strict_and_embed_success_paths():
    clock = FakeClock()
    transport = RecordingTransport()
    batcher = _batcher(transport, batch_size=2, clock=clock)
    vectors = batcher.embed_documents_strict(_texts(2))   # 满批即发
    assert vectors == [_vec("t-0"), _vec("t-1")]
    thread, box = _run_async(batcher, lambda: batcher.embed("t-0"))
    time.sleep(0.05)
    clock.advance(0.030)                                  # 孤条截止发
    thread.join(timeout=2.0)
    assert not thread.is_alive()
    assert box["value"] == _vec("t-0")
    batcher.close()


def test_input_validation_and_closed_rejection():
    batcher = _batcher(RecordingTransport())
    with pytest.raises(ValueError):
        batcher.embed_documents([])
    with pytest.raises(TypeError):
        batcher.embed_documents(["ok", 42])
    with pytest.raises(TypeError):
        batcher.embed(42)
    batcher.close()
    with pytest.raises(RuntimeError):
        batcher.embed_documents(["after-close"])           # 关闭拒新


def test_context_manager_closes():
    with EmbeddingBatcher(
            lambda texts: [_vec(t) for t in texts],
            config=EmbeddingBatchConfig(batch_size=2)) as batcher:
        assert batcher.embed_documents_strict(_texts(2)) == \
            [_vec("t-0"), _vec("t-1")]
    with pytest.raises(RuntimeError):
        batcher.embed_documents(["late"])
