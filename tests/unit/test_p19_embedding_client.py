"""B5/E2 S1 红测（设计 §2 模型网关 + §7.2 ①–④⑧）：vector/embedding_client.py。

红测清单映射（log\设计-真Embedding接线包.md v4 R164）：
- ① 缓存键含 model+dimension+preprocessing（跨 model/维度/preprocessing 键互不相同）；
  分目录布局 cache_root/<model>/<dimension>/<preprocessing>/<cache_key>.json（R149 裁定候选 A）；
  条目 JSON 只存向量+最小元数据、不存正文。
- ② 响应畸形五型 → §2.5 映射：data 数≠input 数 / 维度漂移（+canary 事件）/ 非有限 float /
  空向量 → VectorWriteUnknown("embedding response is unknown")；缺 usage → 保守上界估值
  计入预算（闸不旁路）+ 连续 3 批告警事件。
- ③ 429 退避序列（min(60, 2·2^attempt)+jitter，最多 6 次）与终败 → EmbeddingApiError；
  连接类线性 0.5·(attempt+1)、3 次终败；HTTP 4xx 非 429 不重试直接终败。
- ④ 查询径失败语义由 S4 钉（recall/embedding_query.py）；本件钉客户端层：
  embed_query 终败 → EmbeddingApiError 裸抛给查询编码层（不产出零向量冒充成功）。
- ⑧ 缓存作废规程（R13）：漂移事件 → 按目录整批删除（目录列举可枚举）+ 同进程 LRU
  显式清空钉；作废事件可入台账。
- R14 日预算闸：5M tok/日默认 + 计数器落盘 + 启动加载 + 业务日翻转重置 +
  落盘失败 fail-closed（重启不可绕闸）；超闸 → EmbeddingBudgetExceeded（fail-closed，
  不静默降级、不旁路）。
- 铁律：任何失败路径不得产出零向量/均值向量冒充成功（INV-1 嵌入域同构）。
- key 纪律：DEDUP_EMBEDDING_API_KEY 优先 / DEDUP_EMBEDDING_KEY_FILE 备选现读；
  掩码形态 N11 文档款 sk-xxxx...xxxx；证据不落明文。
- §7.1 延续钉：fake_store 模块不引入 embedding_client（真客户端不得泄入 fake 模块）。
"""

from __future__ import annotations

import json
import math
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from news_flash_dedup.recall.vector_store import VectorWriteUnknown
from news_flash_dedup.vector import embedding_client as ec


# ---------- 测试夹具 ----------

DIM = 4  # 单元层用小维度；真轨 1024 由 UAT/shadow 钉（客户端维度由 config 承载）

CN = timezone(timedelta(hours=8))
T0 = datetime(2026, 9, 29, 10, 0, tzinfo=CN)


def _payload(vectors, *, usage=True, tokens=11):
    # 真 API 响应形态（W-R3a 呈裁钉：DashScope/OpenAI 兼容响应恒带 index
    # 逐位对位；R3-H4-① 起客户端逐项核验 index 与批次位置恒等）
    data = [{"embedding": list(v), "index": i} for i, v in enumerate(vectors)]
    body = {"data": data}
    if usage:
        body["usage"] = {"total_tokens": tokens}
    return body


def _vec(seed=1.0):
    return [float(seed), 0.5, 0.25, 0.125]


class FakeTransport:
    """可编程 transport：签名同真实调用（base_url/model/api_key/texts/timeout_s）。"""

    def __init__(self):
        self.calls = []
        self.script = []  # 每次调用弹一个：payload dict 或 Exception 实例

    def push(self, item):
        self.script.append(item)
        return self

    def __call__(self, *, base_url, model, api_key, texts, timeout_s):
        self.calls.append({"base_url": base_url, "model": model,
                           "api_key": api_key, "texts": list(texts),
                           "timeout_s": timeout_s})
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _client(tmp_path, transport, *, budget=5_000_000, clock=None, sleep=None,
            rng=None, cache_root=None, env=None, query_disk=False):
    config = ec.EmbeddingClientConfig(
        base_url="https://dashscope.example/v1", model="text-embedding-v3",
        dimension=DIM, cache_root=cache_root if cache_root is not None else tmp_path / "cache",
        daily_token_budget=budget, query_disk_cache_enabled=query_disk,
    )
    ticks = {"now": clock or (lambda: T0)}
    return ec.EmbeddingClient(
        config,
        transport_fn=transport,
        env=env if env is not None else {"DEDUP_EMBEDDING_API_KEY": "sk-testkey1234567890abcd"},
        clock=ticks["now"],
        sleep=sleep or (lambda _seconds: None),
        rng=rng or (lambda: 0.0),
    )


# ---------- ① 缓存键与分目录布局（§2.3 + R149 候选 A） ----------

def test_cache_key_contains_model_dimension_preprocessing():
    k1 = ec.cache_key("text-embedding-v3", 1024, "p09_v1", "文本甲")
    # 跨 model / 维度 / preprocessing / 文本四族互不相同
    assert k1 != ec.cache_key("text-embedding-v4", 1024, "p09_v1", "文本甲")
    assert k1 != ec.cache_key("text-embedding-v3", 512, "p09_v1", "文本甲")
    assert k1 != ec.cache_key("text-embedding-v3", 1024, "p09_v2", "文本甲")
    assert k1 != ec.cache_key("text-embedding-v3", 1024, "p09_v1", "文本乙")
    assert isinstance(k1, str) and len(k1) == 64
    int(k1, 16)  # hex


def test_disk_cache_layout_candidate_a_and_no_text_in_entry(tmp_path):
    transport = FakeTransport().push(_payload([_vec()]))
    client = _client(tmp_path, transport)
    client.embed_documents(["文本甲"])
    base = tmp_path / "cache"
    entry = base / "text-embedding-v3" / str(DIM) / "p09_v1"
    files = list(entry.glob("*.json"))
    assert len(files) == 1  # cache_root/<model>/<dimension>/<preprocessing>/<cache_key>.json
    assert files[0].stem == ec.cache_key("text-embedding-v3", DIM, "p09_v1", "文本甲")
    body = json.loads(files[0].read_text(encoding="utf-8"))
    assert body["vector"] == _vec()
    assert "文本甲" not in files[0].read_text(encoding="utf-8")  # 不存正文
    assert set(body) <= {"vector", "created_at"}  # 最小元数据


def test_disk_cache_hit_skips_api_and_budget(tmp_path):
    transport = FakeTransport().push(_payload([_vec()], tokens=11))
    client = _client(tmp_path, transport)
    assert client.embed_documents(["文本甲"]) == [_vec()]
    assert client.embed_documents(["文本甲"]) == [_vec()]
    assert len(transport.calls) == 1  # 第二发缓存命中零 API
    snap = client.usage_snapshot()
    assert snap["total_tokens"] == 11 and snap["api_calls"] == 1
    assert snap["cache_hits"] == 1


def test_truncation_guard_6000_chars_and_cache_key_on_truncated(tmp_path):
    long_text = "文" * 7000
    transport = FakeTransport().push(_payload([_vec()]))
    client = _client(tmp_path, transport)
    client.embed_documents([long_text])
    sent = transport.calls[0]["texts"][0]
    assert len(sent) == 6000  # §2.2 护栏：截断仅作用嵌入输入
    assert transport.calls[0]["texts"][0] == "文" * 6000
    key = ec.cache_key("text-embedding-v3", DIM, "p09_v1", "文" * 6000)
    assert (tmp_path / "cache" / "text-embedding-v3" / str(DIM) / "p09_v1"
            / f"{key}.json").exists()
    assert any(a["type"] == "input_truncated" for a in client.alarms)


def test_batching_respects_batch_size_10_and_order(tmp_path):
    vectors = [_vec(float(i)) for i in range(1, 24)]
    transport = FakeTransport()
    for start in range(0, 23, 10):
        transport.push(_payload(vectors[start:start + 10]))
    client = _client(tmp_path, transport)
    texts = [f"文本{i}" for i in range(23)]
    assert client.embed_documents(texts) == vectors
    assert [len(c["texts"]) for c in transport.calls] == [10, 10, 3]  # ≤10 硬上限
    assert [t for c in transport.calls for t in c["texts"]] == texts  # 保序


# ---------- ② 响应畸形五型 → §2.5 映射 ----------

def test_malformed_data_count_mismatch_write_unknown(tmp_path):
    transport = FakeTransport().push(_payload([_vec(), _vec()]))  # 1 入 2 出
    client = _client(tmp_path, transport)
    with pytest.raises(VectorWriteUnknown, match="(?i)embedding response is unknown"):
        client.embed_documents(["文本甲"])


def test_malformed_dimension_drift_write_unknown_plus_canary(tmp_path):
    transport = FakeTransport().push(_payload([[1.0, 0.5]]))  # 维度 2 ≠ 4
    client = _client(tmp_path, transport)
    with pytest.raises(VectorWriteUnknown, match="(?i)embedding response is unknown"):
        client.embed_documents(["文本甲"])
    assert any(a["type"] == "dimension_drift" for a in client.alarms)  # R1/R2 canary 事件


@pytest.mark.parametrize("bad", [[0.0, 0.0, 0.0, 0.0], [1.0, float("nan"), 0.3, 0.4],
                                 [1.0, float("inf"), 0.3, 0.4], "not-a-list"])
def test_malformed_vector_values_write_unknown(tmp_path, bad):
    transport = FakeTransport().push(_payload([bad]))
    client = _client(tmp_path, transport)
    with pytest.raises(VectorWriteUnknown, match="(?i)embedding response is unknown"):
        client.embed_documents(["文本甲"])


def test_malformed_payload_not_mapping_write_unknown(tmp_path):
    transport = FakeTransport().push(["not", "a", "mapping"])
    client = _client(tmp_path, transport)
    with pytest.raises(VectorWriteUnknown, match="(?i)embedding response is unknown"):
        client.embed_documents(["文本甲"])


def test_no_zero_vector_forgery_on_any_failure(tmp_path):
    # 铁律：失败路径不得产出零向量/均值向量冒充成功（INV-1 同构）
    transport = FakeTransport()
    for _ in range(3):
        transport.push(ec.EmbeddingConnectionFault("boom"))  # 连接类 3 次终败
    client = _client(tmp_path, transport)
    with pytest.raises(ec.EmbeddingApiError):
        client.embed_documents(["文本甲"])
    assert transport.calls  # 已尝试
    assert not list((tmp_path / "cache").rglob("*.json"))  # 失败不落缓存


def test_missing_usage_conservative_estimate_counts_into_budget(tmp_path):
    # 缺 usage → 字符数→token 保守上界估值（UTF-8 字节数）计闸不旁路
    transport = FakeTransport().push(_payload([_vec()], usage=False))
    client = _client(tmp_path, transport)
    client.embed_documents(["文本甲乙"])  # 4 字 × 3 字节 = 12 tok 上界估值
    snap = client.usage_snapshot()
    assert snap["total_tokens"] == len("文本甲乙".encode("utf-8")) == 12
    assert snap["estimated_usage_events"] == 1


def test_missing_usage_streak_three_batches_alarm(tmp_path):
    transport = FakeTransport()
    for _ in range(3):
        transport.push(_payload([_vec()], usage=False))
    client = _client(tmp_path, transport)
    for i in range(3):
        client.embed_documents([f"文本{i}"])
    assert any(a["type"] == "usage_missing_streak" for a in client.alarms)


# ---------- ③ 429/连接退避与终败（§2.4） ----------

def test_429_backoff_sequence_and_terminal_failure(tmp_path):
    sleeps = []
    transport = FakeTransport()
    for _ in range(6):
        transport.push(ec.EmbeddingRateLimited("429"))
    client = _client(tmp_path, transport, sleep=sleeps.append, rng=lambda: 0.5)
    with pytest.raises(ec.EmbeddingApiError, match="(?i)rate|429|exceed"):
        client.embed_documents(["文本甲"])
    assert len(transport.calls) == 6  # 最多 6 次
    expected = [min(60.0, 2.0 * (2 ** a)) + 0.5 for a in range(5)]
    assert sleeps == expected  # min(60, 2·2^attempt)+jitter(0,1)


def test_429_eventually_succeeds_within_retry_budget(tmp_path):
    sleeps = []
    transport = FakeTransport()
    transport.push(ec.EmbeddingRateLimited("429")).push(ec.EmbeddingRateLimited("429"))
    transport.push(_payload([_vec()], tokens=7))
    client = _client(tmp_path, transport, sleep=sleeps.append)
    assert client.embed_documents(["文本甲"]) == [_vec()]
    assert len(transport.calls) == 3


def test_connection_fault_linear_backoff_three_attempts(tmp_path):
    sleeps = []
    transport = FakeTransport()
    for _ in range(3):
        transport.push(ec.EmbeddingConnectionFault("conn reset"))
    client = _client(tmp_path, transport, sleep=sleeps.append)
    with pytest.raises(ec.EmbeddingApiError, match="(?i)connect|timeout|fault"):
        client.embed_documents(["文本甲"])
    assert len(transport.calls) == 3  # 连接类 3 次
    assert sleeps == [0.5 * (a + 1) for a in range(2)]  # 线性 0.5·(attempt+1)


def test_http_4xx_non_429_terminal_no_retry(tmp_path):
    transport = FakeTransport().push(ec.EmbeddingHttpTerminal(401, "unauthorized"))
    client = _client(tmp_path, transport)
    with pytest.raises(ec.EmbeddingApiError, match="(?i)401|terminal|http"):
        client.embed_documents(["文本甲"])
    assert len(transport.calls) == 1  # 确定性失败不重试


# ---------- ④ embed_query 客户端层（LRU + 终败裸抛） ----------

def test_embed_query_lru_hit_and_eviction(tmp_path):
    transport = FakeTransport()
    for _ in range(514):
        transport.push(_payload([_vec()]))
    client = _client(tmp_path, transport)
    client.embed_query("查询甲")
    client.embed_query("查询甲")
    assert len(transport.calls) == 1  # LRU 命中零 API
    for i in range(512):
        client.embed_query(f"查询{i}")  # 填满 512 后挤出最久未用（查询甲被挤出）
    assert len(transport.calls) == 513
    client.embed_query("查询甲")  # 已被挤出 → 再调 API
    assert len(transport.calls) == 514
    # 钉容量语义：LRU 规模 ≤512
    assert client.query_cache_size() <= 512


def test_embed_query_terminal_failure_raises_no_forgery(tmp_path):
    transport = FakeTransport()
    for _ in range(3):
        transport.push(ec.EmbeddingConnectionFault("down"))
    client = _client(tmp_path, transport)
    with pytest.raises(ec.EmbeddingApiError):
        client.embed_query("查询甲")


# ---------- ⑧ 缓存作废规程（R13：删目录整树 + LRU 显式清空） ----------

def test_invalidate_deletes_directory_tree_and_clears_lru(tmp_path):
    transport = FakeTransport()
    transport.push(_payload([_vec(), _vec()]))   # 写入径一批两文
    transport.push(_payload([_vec()]))           # 查询径单文
    client = _client(tmp_path, transport)
    client.embed_documents(["文本甲", "文本乙"])
    client.embed_query("查询甲")
    assert client.query_cache_size() == 1
    model_dir = tmp_path / "cache" / "text-embedding-v3"
    assert model_dir.exists()
    event = client.invalidate_cache()  # 默认本 model 整树
    assert not model_dir.exists()  # 目录列举可枚举 → 删目录整树
    assert client.query_cache_size() == 0  # 同进程 LRU 显式清空
    assert event["deleted_dirs"] and any("text-embedding-v3" in d for d in event["deleted_dirs"])
    assert event["lru_cleared"] == 1


def test_invalidate_scoped_subdirectory(tmp_path):
    transport = FakeTransport().push(_payload([_vec()]))
    config = ec.EmbeddingClientConfig(
        base_url="https://dashscope.example/v1", model="text-embedding-v3",
        dimension=DIM, cache_root=tmp_path / "cache", daily_token_budget=5_000_000)
    client = ec.EmbeddingClient(config, transport_fn=transport,
                                env={"DEDUP_EMBEDDING_API_KEY": "sk-testkey1234567890abcd"},
                                clock=lambda: T0, sleep=lambda s: None, rng=lambda: 0.0)
    client.embed_documents(["文本甲"])
    leaf = tmp_path / "cache" / "text-embedding-v3" / str(DIM) / "p09_v1"
    assert leaf.exists()
    client.invalidate_cache(dimension=DIM, preprocessing="p09_v1")
    assert not leaf.exists()
    assert (tmp_path / "cache" / "text-embedding-v3").exists()  # 只删子树


# ---------- R14 日预算闸（5M 默认/落盘/启动加载/日翻转/落盘失败 fail-closed） ----------

def test_budget_default_5m_and_exceeded_fail_closed(tmp_path):
    transport = FakeTransport().push(_payload([_vec()], tokens=100))
    client = _client(tmp_path, transport, budget=100)
    client.embed_documents(["文本甲"])  # 用满 100
    with pytest.raises(ec.EmbeddingBudgetExceeded):
        client.embed_documents(["文本乙"])
    assert len(transport.calls) == 1  # 超闸 fail-closed：不再发起 API
    assert any(a["type"] == "budget_exceeded" for a in client.alarms)


def test_budget_counter_persisted_and_loaded_on_restart(tmp_path):
    transport = FakeTransport().push(_payload([_vec()], tokens=100))
    cache_root = tmp_path / "cache"
    client = _client(tmp_path, transport, cache_root=cache_root, budget=1000)
    client.embed_documents(["文本甲"])
    counter_file = cache_root / "budget_counter.json"
    body = json.loads(counter_file.read_text(encoding="utf-8"))
    assert body["used_tokens"] == 100
    assert body["business_date"] == "2026-09-29"
    # 重启加载：新实例同 counter 路径 → 已用量恢复（重启不可绕闸）
    transport2 = FakeTransport().push(_payload([_vec(2.0)], tokens=950))
    client2 = _client(tmp_path, transport2, cache_root=cache_root, budget=1000)
    client2.embed_documents(["文本乙"])
    snap2 = client2.usage_snapshot()
    assert snap2["total_tokens"] == 950           # 进程期累计
    assert snap2["budget_used_tokens"] == 100 + 950  # 计数器跨进程恢复
    with pytest.raises(ec.EmbeddingBudgetExceeded):
        client2.embed_documents(["文本丙"])
    assert len(transport2.calls) == 1


def test_budget_day_rollover_resets_counter(tmp_path):
    day1 = datetime(2026, 9, 29, 23, 59, tzinfo=CN)
    day2 = datetime(2026, 9, 30, 0, 30, tzinfo=CN)
    now = {"t": day1}
    transport = FakeTransport()
    transport.push(_payload([_vec()], tokens=900))
    transport.push(_payload([_vec()], tokens=50))
    cache_root = tmp_path / "cache"
    client = _client(tmp_path, transport, cache_root=cache_root, budget=1000,
                     clock=lambda: now["t"])
    client.embed_documents(["文本甲"])
    now["t"] = day2  # 业务日翻转（UTC+8）
    client.embed_documents(["文本乙"])  # 若未翻转将超闸
    snap = client.usage_snapshot()
    assert snap["total_tokens"] == 950            # 进程期累计不重置
    assert snap["budget_used_tokens"] == 50       # 日闸计数器新日重置
    body = json.loads((cache_root / "budget_counter.json").read_text(encoding="utf-8"))
    assert body["business_date"] == "2026-09-30" and body["used_tokens"] == 50


def test_budget_persist_failure_fail_closed(tmp_path):
    transport = FakeTransport().push(_payload([_vec()], tokens=10))
    cache_root = tmp_path / "cache"
    client = _client(tmp_path, transport, cache_root=cache_root)
    # 落盘失败注入：counter 路径指向不可写形态（目录占用文件名）
    counter_file = cache_root / "budget_counter.json"
    counter_file.mkdir(parents=True)  # 同名目录 → 写文件必败
    with pytest.raises(ec.EmbeddingBudgetExceeded, match="(?i)persist|counter"):
        client.embed_documents(["文本甲"])
    # 此后一切调用 fail-closed（不可继续裸跑）
    with pytest.raises(ec.EmbeddingBudgetExceeded):
        client.embed_documents(["文本乙"])
    assert len(transport.calls) == 1


def test_budget_persistence_required_by_default(tmp_path):
    config = ec.EmbeddingClientConfig(
        base_url="https://dashscope.example/v1", model="text-embedding-v3",
        dimension=DIM, cache_root=None, daily_token_budget=1000)
    with pytest.raises(ValueError, match="(?i)persist|budget|path"):
        ec.EmbeddingClient(config, transport_fn=FakeTransport(),
                           env={"DEDUP_EMBEDDING_API_KEY": "sk-x"},
                           clock=lambda: T0, sleep=lambda s: None, rng=lambda: 0.0)


# ---------- key 纪律（§2.2：环境引用、禁抄值、掩码） ----------

def test_key_resolution_priority_and_file_fallback(tmp_path, monkeypatch):
    key_file = tmp_path / "k.env"
    key_file.write_text("# comment\nQWEN_API_KEY='sk-filekey000011112222'\n",
                        encoding="utf-8")
    # 文件去单引号兜底 + 现读不落地
    assert ec.resolve_api_key({"DEDUP_EMBEDDING_KEY_FILE": str(key_file)}) == "sk-filekey000011112222"
    # 环境变量优先
    assert ec.resolve_api_key({"DEDUP_EMBEDDING_API_KEY": "sk-envkey",
                               "DEDUP_EMBEDDING_KEY_FILE": str(key_file)}) == "sk-envkey"
    assert ec.resolve_api_key({}) is None


def test_key_masked_in_form_sk_xxxx_dotdotdot_xxxx():
    assert ec.mask_api_key("sk-b1b6abcdefgh918a") == "sk-b1b...918a"  # N11 文档形态
    assert ec.mask_api_key("short") == "***"
    assert ec.mask_api_key(None) == "***"


def test_missing_key_fail_closed_at_call(tmp_path):
    transport = FakeTransport()
    client = _client(tmp_path, transport, env={})
    with pytest.raises(ec.EmbeddingApiError, match="(?i)key"):
        client.embed_documents(["文本甲"])
    assert transport.calls == []  # 缺 key 零调用


def test_transport_call_shape_openai_compatible(tmp_path):
    transport = FakeTransport().push(_payload([_vec()]))
    client = _client(tmp_path, transport)
    client.embed_documents(["文本甲"])
    call = transport.calls[0]
    assert call["base_url"] == "https://dashscope.example/v1"
    assert call["model"] == "text-embedding-v3"
    assert call["api_key"] == "sk-testkey1234567890abcd"
    assert call["timeout_s"] == 30


# ---------- §7.1 延续钉：fake 模块不引入真客户端 ----------

def test_fake_store_does_not_import_embedding_client():
    import inspect

    import news_flash_dedup.vector.fake_store as fs
    for attr in ("embedding_client", "EmbeddingClient", "qwen_bpe", "pipeline",
                 "urllib", "OpenAI"):
        assert attr not in dir(fs), f"fake_store 不应引入 {attr!r}"
    source = inspect.getsource(fs)
    for token in ("embedding_client", "qwen_bpe", "urllib", "openai"):
        assert token not in source


# ---------- 并发闸形态（ingest=2 / query=8 先例注册） ----------

def test_semaphore_limits_registered():
    assert ec.INGEST_CONCURRENCY == 2
    assert ec.QUERY_CONCURRENCY == 8
    config = ec.EmbeddingClientConfig(
        base_url="b", model="m", dimension=DIM, cache_root=None,
        daily_token_budget=1)
    assert config.batch_size <= 10
    assert config.max_input_chars == 6000
