# -*- coding: utf-8 -*-
"""P1c 运行时装配接线钉（api/assemble.py + lib/run_manifest.py 登记面）。

钉面（任务书逐条）：
- 开关族钉：双装配开关缺席=OFF（wire 返原对象/None——影子态口径"接线
  但默认不启用"）；真值集（"1"/"true"/"yes"/"on" 大小写不敏感+strip）；
  其余值一律关（fail-closed 默认关）；
- env 解析钉：对池窗 1..24、微批批大小窗 1..10、窗口 20..50ms、单对
  超时正数窗——越窗/非数值 ValueError（报文含 env 名，可对账）；
- 判官执行器接线钉（全罐装传输）：Wired 捆扎面 execute → 每对恰 2 路
  call_fn（ab/ba 双序）；render 单对合同（禁拼对结构钉：每次只见单个
  请求）；哨兵占位误用即 RuntimeError 响报；OFF spec 构造即拒（半接线
  僵尸防）；spec 旋钮随行钉（对池值/单对硬超时释槽位）；
- Embedding 写径接线钉（全 fake client）：OFF=同一对象原样返回（写径
  逐字节现役）；ON=读径/台账/护栏直通（dimension/usage_snapshot/
  embed_query 零触写径）；写径经网关合批（满批即发+余量超时即发，不凑
  批硬等）+位次保序；跨调用方合批钉（假钟冻结=无 deadline 批，满批
  结构性合流）；**写路径真链等价钉**（RealVectorPipeline 直连 vs 包装
  ——IngestReport 全字段+store 终态+落行一致）；三态失败映射钉
  （EmbeddingApiError→pipeline 翻 failed 照旧；VectorWriteUnknown→
  不翻状态照旧；批级失败逐条回落不死全批）；未知优先于终败传播钉；
  close 拒新提交钉；
- KNOWN_SWITCHES 登记钉（六项入册+快照值可溯）。

零成本纪律：判官传输=罐装函数；嵌入客户端=罐装计数器（无真 HTTP/缓存
/预算盘）；FakeP19Store 同 test_p19_real_pipeline.py:104-144 鸭式形态；
tokenizer=qwen_bpe 本地词表（仓内 vendor 文件，零网络）。零真 LLM/ES/
Milvus。
"""

from __future__ import annotations

import threading
import time

import pytest

from news_flash_dedup.api import assemble as asm
from news_flash_dedup.decide.judge_pair_executor import (
    DualOrderOutcome,
    JudgePairRequest,
    STATUS_ERROR,
    STATUS_OK,
    STATUS_TIMEOUT,
)
from news_flash_dedup.lib import run_manifest as rm
from news_flash_dedup.recall.vector_space import EmbeddingSpace
from news_flash_dedup.recall.vector_store import VectorWriteUnknown
from news_flash_dedup.vector import pipeline as vp
from news_flash_dedup.vector import qwen_bpe as qb
from news_flash_dedup.vector.embedding_client import EmbeddingApiError

DIM = 4


def _space():
    """小维度假空间（test_p19_real_pipeline.py:52-56 同形）。"""
    return EmbeddingSpace("mock_embedding", "fixture_v1", "codepoint_v1", "none",
                          "COSINE", DIM, "plain_v1", "plain_v1",
                          "tokens512_overlap64_bpe_v1", "p09_v1")


# ---------------------------------------------------------------- 罐装件

class FakeEmbedClient:
    """罐装嵌入客户端（鸭面四件+embed_query；剧本：fail_multi/fail_always）。

    fail_multi：多文本调用抛此错（单文本照常）——批级瞬断回落剧本；
    fail_always：一切调用抛此错——终败/未知剧本。
    """

    def __init__(self, *, dim=DIM, max_input_chars=6000):
        self.dimension = dim
        self.max_input_chars = max_input_chars
        self.calls = []            # 每次 embed_documents 的 texts 快照
        self.usage = {"total_tokens": 0, "api_calls": 0, "cache_hits": 0}
        self.fail_multi = None
        self.fail_always = None

    def usage_snapshot(self):
        return dict(self.usage)

    def embed_query(self, text):
        return [0.25] * self.dimension

    def embed_documents(self, texts):
        texts = list(texts)
        self.calls.append(texts)
        if self.fail_always is not None:
            raise self.fail_always
        if len(texts) > 1 and self.fail_multi is not None:
            raise self.fail_multi
        self.usage["total_tokens"] += len(texts)
        self.usage["api_calls"] += 1
        return [[float(len(t)), 0.5, 0.25, 0.125] for t in texts]


class ScriptedEmbedClient(FakeEmbedClient):
    """逐调用剧本客户端（calls_script 逐次消费：None=成功/异常实例=抛）。"""

    def __init__(self, script):
        super().__init__()
        self.script = list(script)

    def embed_documents(self, texts):
        texts = list(texts)
        self.calls.append(texts)
        item = self.script.pop(0) if self.script else None
        if item is not None:
            raise item
        self.usage["total_tokens"] += len(texts)
        self.usage["api_calls"] += 1
        return [[float(len(t)), 0.5, 0.25, 0.125] for t in texts]


class FakeP19Store:
    """真仓储接口面鸭式替身（test_p19_real_pipeline.py:104-144 同形精简）。"""

    def __init__(self, space, *, pending=True):
        self.space = space
        self.rows = {}
        self.states = {}
        self.cas_log = []
        self._default = "pending" if pending else None

    def get_vector_state(self, record_id):
        return self.states.get(record_id, self._default)

    def upsert_chunks(self, *, record_id, chunks, scope_id, arrival_seq):
        report = []
        for chunk_id, embedding in chunks:
            key = f"{record_id}_{chunk_id}"
            if key in self.rows:
                report.append((key, "reused"))
            else:
                self.rows[key] = (record_id, chunk_id, tuple(embedding),
                                  scope_id, arrival_seq)
                report.append((key, "created"))
        return report

    def cas_vector_state(self, record_id, *, to):
        if to not in ("ready", "failed"):
            raise ValueError("to must be ready|failed")
        if self.get_vector_state(record_id) != "pending":
            raise ValueError("source state must be pending")
        self.states[record_id] = to
        self.cas_log.append((record_id, to))


_ON_VALUES = ("1", "true", "YES", "On", " 1 ")
_OFF_VALUES = ("", "0", "2", "banana", "off", "false")


# ---------------------------------------------------------------- 开关族 / env 解析

def test_both_switches_default_off():
    """影子态口径钉：缺席 env=双 OFF=缺省 spec（20/None；8/30ms）。"""
    assert asm.judge_executor_spec_from_env({}) == asm.JudgeExecutorSpec(
        enabled=False, pair_concurrency=20, pair_timeout_s=None)
    assert asm.embedding_micro_batch_spec_from_env({}) == (
        asm.EmbeddingMicroBatchSpec(
            enabled=False, batch_size=8, max_wait_s=0.030))


def test_switch_on_values_and_case_insensitivity():
    """真值集钉（judge_pair.py:67 同款）：strip+lower 后 ∈ {1,true,yes,on}。"""
    for raw in _ON_VALUES:
        env = {asm.JUDGE_PAIR_EXECUTOR_ENV: raw,
               asm.EMBEDDING_MICRO_BATCH_ENV: raw}
        assert asm.judge_executor_spec_from_env(env).enabled is True
        assert asm.embedding_micro_batch_spec_from_env(env).enabled is True
    for raw in _OFF_VALUES:
        env = {asm.JUDGE_PAIR_EXECUTOR_ENV: raw,
               asm.EMBEDDING_MICRO_BATCH_ENV: raw}
        assert asm.judge_executor_spec_from_env(env).enabled is False
        assert asm.embedding_micro_batch_spec_from_env(env).enabled is False


def test_judge_concurrency_window_fail_closed():
    """对池窗钉：1..24（模块钉值窗）；越窗/非整数 ValueError（报文含 env 名）。"""
    env = {asm.JUDGE_PAIR_CONCURRENCY_ENV: "24"}
    assert asm.judge_executor_spec_from_env(env).pair_concurrency == 24
    env = {asm.JUDGE_PAIR_CONCURRENCY_ENV: "3"}
    assert asm.judge_executor_spec_from_env(env).pair_concurrency == 3
    for bad in ("0", "25", "-1", "3.5", "abc", "二十"):
        with pytest.raises(ValueError, match=asm.JUDGE_PAIR_CONCURRENCY_ENV):
            asm.judge_executor_spec_from_env(
                {asm.JUDGE_PAIR_CONCURRENCY_ENV: bad})


def test_judge_timeout_env_parse():
    """单对硬超时钉：正浮点秒；缺席=None（不包裹——模块缺省）。"""
    env = {asm.JUDGE_PAIR_TIMEOUT_ENV: "1.5"}
    assert asm.judge_executor_spec_from_env(env).pair_timeout_s == 1.5
    assert asm.judge_executor_spec_from_env({}).pair_timeout_s is None
    for bad in ("0", "-1", "abc", "nan", "inf"):
        with pytest.raises(ValueError, match=asm.JUDGE_PAIR_TIMEOUT_ENV):
            asm.judge_executor_spec_from_env({asm.JUDGE_PAIR_TIMEOUT_ENV: bad})


def test_embedding_batch_size_window():
    """微批批大小窗钉：1..10（DashScope 端点硬顶）；越窗/非整数 ValueError。"""
    for ok in ("1", "8", "10"):
        spec = asm.embedding_micro_batch_spec_from_env(
            {asm.EMBEDDING_BATCH_SIZE_ENV: ok})
        assert spec.batch_size == int(ok)
    for bad in ("0", "11", "abc", "8.5"):
        with pytest.raises(ValueError, match=asm.EMBEDDING_BATCH_SIZE_ENV):
            asm.embedding_micro_batch_spec_from_env(
                {asm.EMBEDDING_BATCH_SIZE_ENV: bad})


def test_embedding_max_wait_window_ms():
    """微批窗口钉：20..50ms（毫秒入参/秒面换算）；越窗/非数值 ValueError。"""
    spec = asm.embedding_micro_batch_spec_from_env(
        {asm.EMBEDDING_BATCH_MAX_WAIT_MS_ENV: "20"})
    assert spec.max_wait_s == pytest.approx(0.020)
    spec = asm.embedding_micro_batch_spec_from_env(
        {asm.EMBEDDING_BATCH_MAX_WAIT_MS_ENV: "50"})
    assert spec.max_wait_s == pytest.approx(0.050)
    for bad in ("19", "51", "0", "abc"):
        with pytest.raises(ValueError, match=asm.EMBEDDING_BATCH_MAX_WAIT_MS_ENV):
            asm.embedding_micro_batch_spec_from_env(
                {asm.EMBEDDING_BATCH_MAX_WAIT_MS_ENV: bad})


# ---------------------------------------------------------------- 判官执行器接线（全罐装）

def _transport_log():
    calls = []

    def call_fn(**kwargs):
        calls.append(kwargs)
        return "content-tail", 0.001

    return call_fn, calls


def _render_log():
    seen = []

    def render(request, order):
        seen.append((request.pair_id, order))
        return {"system": "sys", "user": f"{request.text_history}|"
                                         f"{request.text_current}|{order}"}

    return render, seen


def _requests(n):
    return [JudgePairRequest(pair_id=f"p{i}", text_history=f"历史{i}",
                             text_current=f"现文{i}") for i in range(n)]


def test_wire_judge_pair_executor_off_returns_none():
    """OFF（缺省）→ None：判定链零感知（点头 B 口径装配侧落点）。"""
    assert asm.wire_judge_pair_executor(
        {}, call_fn=lambda **k: ("", 0.0), render=lambda r, o: {}) is None
    assert asm.wire_judge_pair_executor(
        {asm.JUDGE_PAIR_EXECUTOR_ENV: "0"},
        call_fn=lambda **k: ("", 0.0), render=lambda r, o: {}) is None


def test_wired_executor_dual_order_two_calls_per_pair():
    """双序钉：每对恰 2 路 call_fn（ab/ba）；render 单对合同（禁拼对）。"""
    call_fn, calls = _transport_log()
    render, seen = _render_log()
    wired = asm.wire_judge_pair_executor(
        {asm.JUDGE_PAIR_EXECUTOR_ENV: "1"}, call_fn=call_fn, render=render)
    assert wired is not None
    results = wired.execute(_requests(5))
    assert [r.status for r in results] == [STATUS_OK] * 5
    assert len(calls) == 10                       # 5 对 × 双序 2 路
    assert all("system" in kwargs and "user" in kwargs for kwargs in calls)
    pair_orders: dict[str, set] = {}
    for pair_id, order in seen:
        pair_orders.setdefault(pair_id, set()).add(order)
    assert all(orders == {"ab", "ba"} for orders in pair_orders.values())
    for result in results:                        # 缺省 combine → DualOrderOutcome
        assert isinstance(result.value, DualOrderOutcome)
    assert wired.ledger.snapshot()["completed"] == 5


def test_wired_executor_placeholder_misuse_raises_loudly():
    """哨兵占位钉：裸 executor.execute（无 pair_fn 覆盖）=误用即响——
    占位 RuntimeError 被执行器逐对异常收容（不连坐设计）为响亮
    status=error+纪律报文，绝不静默半接线。"""
    call_fn, _ = _transport_log()
    render, _ = _render_log()
    wired = asm.wire_judge_pair_executor(
        {asm.JUDGE_PAIR_EXECUTOR_ENV: "1"}, call_fn=call_fn, render=render)
    results = wired.executor.execute(_requests(1))
    assert results[0].status == STATUS_ERROR
    assert "P1c 装配纪律" in (results[0].error or "")
    assert wired.ledger.snapshot()["errors"] == 1


def test_build_rejects_disabled_spec_and_bad_callables():
    """fail-closed 构造钉：OFF spec 构造即拒；注入面非可调用即拒。"""
    with pytest.raises(ValueError, match="enabled=False"):
        asm.build_judge_pair_executor(
            asm.JudgeExecutorSpec(enabled=False),
            call_fn=lambda **k: None, render=lambda r, o: {})
    on = asm.JudgeExecutorSpec(enabled=True)
    with pytest.raises(TypeError):
        asm.build_judge_pair_executor(
            on, call_fn="not-callable", render=lambda r, o: {})
    with pytest.raises(TypeError):
        asm.build_judge_pair_executor(
            on, call_fn=lambda **k: None, render="not-callable")
    with pytest.raises(TypeError):
        asm.build_judge_pair_executor(
            on, call_fn=lambda **k: None, render=lambda r, o: {},
            combine="not-callable")


def test_wired_executor_spec_knobs_flow():
    """旋钮随行钉：对池值经 env→spec→executor；spec 回显在捆扎面。"""
    call_fn, _ = _transport_log()
    render, _ = _render_log()
    wired = asm.wire_judge_pair_executor(
        {asm.JUDGE_PAIR_EXECUTOR_ENV: "1",
         asm.JUDGE_PAIR_CONCURRENCY_ENV: "3"},
        call_fn=call_fn, render=render)
    assert wired.spec.pair_concurrency == 3
    assert wired.executor.pair_concurrency == 3
    assert wired.executor.max_pair_concurrency == 24   # 模块钉值不越


def test_wired_executor_pair_timeout_discards_slow_pair():
    """单对硬超时钉：pair_timeout_s 到点→status=timeout、结果丢弃不连坐。"""
    def slow_call(**kwargs):
        time.sleep(0.3)
        return "never", 0.3

    render, _ = _render_log()
    wired = asm.wire_judge_pair_executor(
        {asm.JUDGE_PAIR_EXECUTOR_ENV: "1",
         asm.JUDGE_PAIR_TIMEOUT_ENV: "0.05"},
        call_fn=slow_call, render=render)
    results = wired.execute(_requests(1))
    assert results[0].status == STATUS_TIMEOUT
    assert results[0].value is None
    assert wired.ledger.snapshot()["timeouts"] == 1


# ---------------------------------------------------------------- Embedding 写径接线（全 fake）

def test_wire_off_returns_same_object():
    """OFF=同一对象原样返回（写径逐字节现役——零包装零行为差）。"""
    client = FakeEmbedClient()
    assert asm.wire_embedding_micro_batch(client, {}) is client
    assert asm.wire_embedding_micro_batch(
        client, {asm.EMBEDDING_MICRO_BATCH_ENV: "0"}) is client
    assert asm.wire_embedding_micro_batch(
        client, {asm.EMBEDDING_MICRO_BATCH_ENV: "banana"}) is client


def test_wrapped_client_proxies_read_faces():
    """直通面钉：dimension/max_input_chars/usage_snapshot/embed_query 代理
    被包 client；读径/台账零触写径（inner.calls 空）。"""
    inner = FakeEmbedClient()
    wrapped = asm.wire_embedding_micro_batch(
        inner, {asm.EMBEDDING_MICRO_BATCH_ENV: "1"})
    assert isinstance(wrapped, asm.BatchedEmbeddingClient)
    assert wrapped.dimension == DIM
    assert wrapped.max_input_chars == 6000
    assert wrapped.embed_query("查询文本") == [0.25] * DIM
    assert wrapped.usage_snapshot() == {
        "total_tokens": 0, "api_calls": 0, "cache_hits": 0}
    assert inner.calls == []


def test_embed_documents_full_batch_then_deadline_batch():
    """合批钉（真钟）：满批 8 即发 + 余量 4 超时即发（不凑批硬等）；保序。"""
    inner = FakeEmbedClient()
    wrapped = asm.wire_embedding_micro_batch(
        inner, {asm.EMBEDDING_MICRO_BATCH_ENV: "1",
                asm.EMBEDDING_BATCH_SIZE_ENV: "8"})
    texts = [f"文本条目{i}" for i in range(12)]
    vectors = wrapped.embed_documents(texts)
    assert vectors == [[float(len(t)), 0.5, 0.25, 0.125] for t in texts]
    assert [len(call) for call in inner.calls] == [8, 4]
    snapshot = wrapped.batcher.ledger.snapshot()
    assert snapshot["full_batches"] == 1
    assert snapshot["deadline_batches"] == 1


def test_cross_caller_coalescing_full_batch():
    """跨调用方合批钉（假钟冻结=无 deadline 批，满批结构性合流）。

    两路各 4 条并发提交 → 合 1 批 8 条（满批即发）；逐位槽位回填按
    调用方各自保序不串号。join 带超时=接线坏时快败不挂死。
    """
    inner = FakeEmbedClient()
    wrapped = asm.wire_embedding_micro_batch(
        inner, {asm.EMBEDDING_MICRO_BATCH_ENV: "1",
                asm.EMBEDDING_BATCH_SIZE_ENV: "8"},
        clock=lambda: 0.0)
    results: dict[str, list] = {}

    def submit(tag):
        texts = [f"{tag}-{i}" for i in range(4)]
        results[tag] = wrapped.embed_documents(texts)

    threads = [threading.Thread(target=submit, args=(tag,))
               for tag in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10.0)
    assert all(not thread.is_alive() for thread in threads)
    assert [len(call) for call in inner.calls] == [8]
    assert wrapped.batcher.ledger.snapshot()["full_batches"] == 1
    assert results["a"] == [[3.0, 0.5, 0.25, 0.125]] * 4
    assert results["b"] == [[3.0, 0.5, 0.25, 0.125]] * 4


def test_write_path_pipeline_report_and_state_equivalence():
    """**写路径真链等价钉**：RealVectorPipeline 直连 vs 微批包装——
    IngestReport 全字段（含 tokens_used/cache_hits 台账增量口径）+ store
    终态（ready）+ 落行内容一致。"""
    space = _space()
    tokenizer = qb.QwenBpeTokenizer()
    text = "中国人民银行宣布降准0.5个百分点，释放长期资金约1万亿元。"

    direct_store = FakeP19Store(space)
    report_direct = vp.RealVectorPipeline(
        direct_store, FakeEmbedClient(), space,
        tokenizer=tokenizer).ingest_record(
            record_id="r1", text=text, scope_id="default", arrival_seq=1)

    wired_store = FakeP19Store(space)
    wrapped = asm.wire_embedding_micro_batch(
        FakeEmbedClient(), {asm.EMBEDDING_MICRO_BATCH_ENV: "1"})
    report_wired = vp.RealVectorPipeline(
        wired_store, wrapped, space, tokenizer=tokenizer).ingest_record(
            record_id="r1", text=text, scope_id="default", arrival_seq=1)

    assert report_direct == report_wired
    assert direct_store.states == wired_store.states == {"r1": "ready"}
    assert direct_store.rows == wired_store.rows


def test_write_path_terminal_failure_flips_failed_both_paths():
    """三态钉（终败）：EmbeddingApiError → 包装路径照旧翻 failed+传播
    （pipeline.py:96-99 口径经包装层保形）。"""
    space = _space()
    tokenizer = qb.QwenBpeTokenizer()
    text = "工商银行发布年度业绩报告。"

    direct_store = FakeP19Store(space)
    boom_direct = FakeEmbedClient()
    boom_direct.fail_always = EmbeddingApiError("terminal")
    with pytest.raises(EmbeddingApiError):
        vp.RealVectorPipeline(direct_store, boom_direct, space,
                              tokenizer=tokenizer).ingest_record(
                              record_id="r1", text=text, scope_id="default",
                              arrival_seq=1)
    assert direct_store.get_vector_state("r1") == "failed"

    wired_store = FakeP19Store(space)
    boom = FakeEmbedClient()
    boom.fail_always = EmbeddingApiError("terminal")
    wrapped = asm.wire_embedding_micro_batch(
        boom, {asm.EMBEDDING_MICRO_BATCH_ENV: "1"})
    with pytest.raises(EmbeddingApiError):
        vp.RealVectorPipeline(wired_store, wrapped, space,
                              tokenizer=tokenizer).ingest_record(
                              record_id="r1", text=text, scope_id="default",
                              arrival_seq=1)
    assert wired_store.get_vector_state("r1") == "failed"
    # 批级失败→逐条回落（1 批+1 单发）→全败→首错原样
    assert [len(call) for call in boom.calls] == [1, 1]


def test_write_path_unknown_failure_no_state_flip_both_paths():
    """三态钉（确认未知）：VectorWriteUnknown → 不翻状态照旧传播
    （pipeline.py:100-103 口径经包装层保形）。"""
    space = _space()
    tokenizer = qb.QwenBpeTokenizer()
    text = "农业银行发布季度经营数据。"

    direct_store = FakeP19Store(space)
    unknown_direct = FakeEmbedClient()
    unknown_direct.fail_always = VectorWriteUnknown(
        "embedding response is unknown")
    with pytest.raises(VectorWriteUnknown):
        vp.RealVectorPipeline(direct_store, unknown_direct, space,
                              tokenizer=tokenizer).ingest_record(
                              record_id="r1", text=text, scope_id="default",
                              arrival_seq=1)
    assert direct_store.get_vector_state("r1") == "pending"

    wired_store = FakeP19Store(space)
    unknown = FakeEmbedClient()
    unknown.fail_always = VectorWriteUnknown(
        "embedding response is unknown")
    wrapped = asm.wire_embedding_micro_batch(
        unknown, {asm.EMBEDDING_MICRO_BATCH_ENV: "1"})
    with pytest.raises(VectorWriteUnknown):
        vp.RealVectorPipeline(wired_store, wrapped, space,
                              tokenizer=tokenizer).ingest_record(
                              record_id="r1", text=text, scope_id="default",
                              arrival_seq=1)
    assert wired_store.get_vector_state("r1") == "pending"


def test_batch_failure_falls_back_per_item_good_items_survive():
    """回落钉：批级瞬断（多文本抛 EmbeddingApiError）→ 逐条单发全成
    ——一批坏一条不死全批（embedding_batch.py:329-348 写径实效）。"""
    inner = FakeEmbedClient()
    inner.fail_multi = EmbeddingApiError("batch-level transient")
    wrapped = asm.wire_embedding_micro_batch(
        inner, {asm.EMBEDDING_MICRO_BATCH_ENV: "1"})
    texts = [f"条目{i}" for i in range(3)]
    vectors = wrapped.embed_documents(texts)
    assert vectors == [[float(len(t)), 0.5, 0.25, 0.125] for t in texts]
    assert [len(call) for call in inner.calls] == [3, 1, 1, 1]
    snapshot = wrapped.batcher.ledger.snapshot()
    assert snapshot["fallback_batches"] == 1
    assert snapshot["item_failures"] == 0


def test_unknown_error_takes_priority_over_terminal_in_mapping():
    """未知优先钉：批级终败→回落中一条确认未知 → 未知原样传播（不把
    未知冒充已知终败——fail-closed 方向）。"""
    unknown = VectorWriteUnknown("embedding response is unknown")
    script = [EmbeddingApiError("batch-level"), None, unknown]
    inner = ScriptedEmbedClient(script)
    wrapped = asm.wire_embedding_micro_batch(
        inner, {asm.EMBEDDING_MICRO_BATCH_ENV: "1"})
    with pytest.raises(VectorWriteUnknown):
        wrapped.embed_documents(["甲文", "乙文"])
    assert [len(call) for call in inner.calls] == [2, 1, 1]


def test_close_refuses_new_submissions():
    """close 钉：停 flusher 后拒新提交（在判条目照发完再退的收口纪律）。"""
    inner = FakeEmbedClient()
    wrapped = asm.wire_embedding_micro_batch(
        inner, {asm.EMBEDDING_MICRO_BATCH_ENV: "1"})
    wrapped.embed_documents(["先发一条"])            # 懒启 flusher
    wrapped.close()
    with pytest.raises(RuntimeError, match="已关闭"):
        wrapped.embed_documents(["再来一条"])


# ---------------------------------------------------------------- KNOWN_SWITCHES 登记面

def test_p1c_switches_registered_in_known_switches():
    """登记钉：六项 P1c env 入册（「新增开关须显式登记入本表」规矩）；
    快照面值可溯（缺席记空串）。"""
    names = (
        asm.JUDGE_PAIR_EXECUTOR_ENV,
        asm.JUDGE_PAIR_CONCURRENCY_ENV,
        asm.JUDGE_PAIR_TIMEOUT_ENV,
        asm.EMBEDDING_MICRO_BATCH_ENV,
        asm.EMBEDDING_BATCH_SIZE_ENV,
        asm.EMBEDDING_BATCH_MAX_WAIT_MS_ENV,
    )
    for name in names:
        assert name in rm.KNOWN_SWITCHES
    snapshot = dict(rm.snapshot_switch_state(
        {asm.EMBEDDING_MICRO_BATCH_ENV: "1"}))
    assert snapshot[asm.EMBEDDING_MICRO_BATCH_ENV] == "1"
    assert snapshot[asm.JUDGE_PAIR_EXECUTOR_ENV] == ""   # 缺席=空串（未设置）
