"""B5/E2 S4 红测（设计 §3 写入真链 + §4.1/4.6 召回激活）：vector/pipeline.py
+ recall/embedding_query.py。

红测清单映射（log\设计-真Embedding接线包.md v4 R164）：
- 写入真链（§3.2）：record 四元组+正文 → real_chunk_text（BPE，chunking 新身份）
  → embed_documents（分批保序/缓存命中零 API）→ upsert_chunks（既有执法继承）
  → cas ready；空文本 → P19VectorError + vector_state=failed（诚实失败）；
  嵌入终败 → failed + EmbeddingApiError 传播（重试安全）；响应畸形 →
  VectorWriteUnknown 传播且**不翻状态**（确认未知，fail-closed）；
  6000 字护栏：截断仅作用嵌入输入、chunk 原文区间不动、截断事件入台账；
  半写恢复重放：嵌入缓存命中零 API + upsert reused 幂等（N12）。
- 查询编码层（§4.1）：QueryEmbedder 对称编码（query=document 同一路径）；
  失败 → unavailable + EMBEDDING_QUERY_FAILED 不裸抛；空文本不前置拦截
  （store 既有 not_applicable/EMPTY_BODY）；SPACE_UNCONFIRMED 语义分立
  （未接线 vs 接线后故障两码不混）。
- VectorSearcher 端口（§4.6，B1 过闸稿 §2.4 L140）：鸭式
  search(request, query_vector)->ChannelResult；query_vector=None 时本片
  经 QueryEmbedder 供给（B4 NullVectorSearcher.search(request, None) 同型
  调用面）；prepared_seq 直传（fusion.py:123-126 依赖）；VECTOR_QUERY_VERSION
  维持 embedding_v1 不升。
- INV-4 防线：装配期 client.dimension 与 space.dimension 不符即拒。
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from news_flash_dedup.recall.models import ChannelResult, RecallCandidate, RecallRequest
from news_flash_dedup.recall.vector_space import EmbeddingSpace
from news_flash_dedup.recall.vector_store import (
    VECTOR_QUERY_VERSION,
    MilvusVectorStore,
    VectorWriteUnknown,
    prepare_vector_row,
)
from news_flash_dedup.vector import embedding_client as ec
from news_flash_dedup.vector import pipeline as vp
from news_flash_dedup.vector import qwen_bpe as qb
from news_flash_dedup.vector.coordinator import P19VectorError
from news_flash_dedup.recall import embedding_query as eq


NOW = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)
DAY = "2026-09-29"
DIM = 4


def _space():
    """小维度假空间（单元层；真 1024 空间由 UAT/shadow 钉）。"""
    return EmbeddingSpace("mock_embedding", "fixture_v1", "codepoint_v1", "none",
                          "COSINE", DIM, "plain_v1", "plain_v1",
                          "tokens512_overlap64_bpe_v1", "p09_v1")


def _rid(seed: str) -> str:
    return hashlib.sha256(f"b5s4-{seed}".encode("utf-8")).hexdigest()


def _vec(seed: float = 1.0):
    return [float(seed), 0.5, 0.25, 0.125]


class FakeTransport:
    def __init__(self):
        self.calls = []
        self.script = []

    def push(self, item):
        self.script.append(item)
        return self

    def __call__(self, *, base_url, model, api_key, texts, timeout_s):
        self.calls.append(list(texts))
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _payload(vectors, tokens=9):
    # 真 API 响应形态（W-R3a 呈裁钉：响应恒带 index 逐位对位；R3-H4-① 起
    # 客户端逐项核验 index 与批次位置恒等）
    return {"data": [{"embedding": list(v), "index": i}
                     for i, v in enumerate(vectors)],
            "usage": {"total_tokens": tokens}}


def _client(tmp_path, transport):
    config = ec.EmbeddingClientConfig(
        base_url="https://dashscope.example/v1", model="text-embedding-v3",
        dimension=DIM, cache_root=tmp_path / "cache", daily_token_budget=5_000_000)
    return ec.EmbeddingClient(
        config, transport_fn=transport,
        env={"DEDUP_EMBEDDING_API_KEY": "sk-testkey1234567890abcd"},
        clock=lambda: NOW, sleep=lambda s: None, rng=lambda: 0.0)


# ---------- 写入侧 fake 仓储（RealMilvusP19Store 鸭式替身） ----------

class FakeP19Store:
    """真仓储接口面：upsert_chunks / cas_vector_state / get_vector_state。"""

    def __init__(self, space, *, pending=True):
        self.space = space
        self.rows = {}
        self.states = {}
        self.pending = set()
        self.upsert_calls = []
        if pending:
            self._pending_default = True
        else:
            self._pending_default = False

    def get_vector_state(self, record_id):
        return self.states.get(record_id, "pending" if self._pending_default else None)

    def upsert_chunks(self, *, record_id, chunks, scope_id, arrival_seq):
        if self.get_vector_state(record_id) is None:
            raise VectorWriteUnknown("ES authority record is absent")
        report = []
        for chunk_id, embedding in chunks:
            key = f"{record_id}_{chunk_id}"
            row = (record_id, chunk_id, tuple(float(v) for v in embedding),
                   scope_id, arrival_seq)
            if key in self.rows:
                assert self.rows[key] == row, "same vector_id different artifact"
                report.append((key, "reused"))
            else:
                self.rows[key] = row
                report.append((key, "created"))
        self.upsert_calls.append({"record_id": record_id, "n": len(chunks)})
        return report

    def cas_vector_state(self, record_id, *, to):
        if to not in ("ready", "failed"):
            raise ValueError("to must be ready|failed")
        if self.get_vector_state(record_id) != "pending":
            raise ValueError("source state must be pending")
        self.states[record_id] = to


# ---------- 写入真链（§3.2） ----------

def test_ingest_happy_path_chunk_embed_upsert_cas_ready(tmp_path):
    transport = FakeTransport().push(_payload([_vec(1.0)]))
    client = _client(tmp_path, transport)
    space = _space()
    store = FakeP19Store(space)
    pipe = vp.RealVectorPipeline(store, client, space, tokenizer=qb.QwenBpeTokenizer())
    rid = _rid("happy")
    report = pipe.ingest_record(record_id=rid, text="中国人民银行宣布降准。",
                                scope_id="default", arrival_seq=7)
    assert report.state == "ready"
    assert store.get_vector_state(rid) == "ready"
    assert report.chunk_ids == (0,)          # 短文本单 chunk（BPE 实测 6 token）
    assert all(outcome in ("created", "reused") for _, outcome in report.outcomes)
    assert [vid for vid, _ in report.outcomes] == [
        f"{rid}_{cid}" for cid in report.chunk_ids]
    assert report.truncated_chunk_ids == ()
    assert report.tokens_used == 9
    assert report.chunking_version == "tokens512_overlap64_bpe_v1"


def test_ingest_empty_text_p19_error_and_failed(tmp_path):
    transport = FakeTransport()
    client = _client(tmp_path, transport)
    store = FakeP19Store(_space())
    pipe = vp.RealVectorPipeline(store, client, _space(),
                                 tokenizer=qb.QwenBpeTokenizer())
    rid = _rid("empty")
    with pytest.raises(P19VectorError, match="(?i)chunks must be non-empty"):
        pipe.ingest_record(record_id=rid, text="   \n ", scope_id="default",
                           arrival_seq=1)
    assert store.get_vector_state(rid) == "failed"  # 诚实失败（INV-1）
    assert transport.calls == []                    # 零 API


def test_ingest_api_terminal_failure_failed_then_raise(tmp_path):
    transport = FakeTransport()
    for _ in range(3):
        transport.push(ec.EmbeddingConnectionFault("down"))
    client = _client(tmp_path, transport)
    store = FakeP19Store(_space())
    pipe = vp.RealVectorPipeline(store, client, _space(),
                                 tokenizer=qb.QwenBpeTokenizer())
    rid = _rid("apifail")
    with pytest.raises(ec.EmbeddingApiError):
        pipe.ingest_record(record_id=rid, text="正文。", scope_id="default",
                           arrival_seq=1)
    assert store.get_vector_state(rid) == "failed"  # 终败 → failed（重试安全）
    assert store.rows == {}                          # 零写入


def test_ingest_malformed_response_unknown_no_state_flip(tmp_path):
    transport = FakeTransport().push({"data": [{"embedding": [1.0, 2.0]}]})
    client = _client(tmp_path, transport)
    store = FakeP19Store(_space())
    pipe = vp.RealVectorPipeline(store, client, _space(),
                                 tokenizer=qb.QwenBpeTokenizer())
    rid = _rid("malformed")
    with pytest.raises(VectorWriteUnknown, match="(?i)embedding response is unknown"):
        pipe.ingest_record(record_id=rid, text="正文。", scope_id="default",
                           arrival_seq=1)
    assert store.get_vector_state(rid) == "pending"  # 确认未知：不翻状态（fail-closed）


class _WideSpanTokenizer:
    """宽 span 桩（100 字符/token）：构造单 chunk >6000 字符的护栏场景。"""

    def token_spans(self, text):
        return tuple((i, min(i + 100, len(text))) for i in range(0, len(text), 100))


def test_ingest_truncation_guard_embeds_truncated_only(tmp_path):
    long_text = "文" * 6500 + "。"           # 66 宽 token → 单 chunk 6501 字符
    transport = FakeTransport().push(_payload([_vec()]))
    client = _client(tmp_path, transport)
    store = FakeP19Store(_space())
    tokenizer = _WideSpanTokenizer()
    pipe = vp.RealVectorPipeline(store, client, _space(), tokenizer=tokenizer)
    rid = _rid("truncate")
    report = pipe.ingest_record(record_id=rid, text=long_text,
                                scope_id="default", arrival_seq=1)
    assert report.truncated_chunk_ids == (0,)          # 截断事件入台账
    assert len(transport.calls[0][0]) == 6000          # 截断仅作用嵌入输入
    chunks = qb.real_chunk_text(long_text, tokenizer)
    assert chunks[0].text == long_text                  # chunk 原文区间不动


def test_ingest_half_write_replay_zero_api_reused(tmp_path):
    transport = FakeTransport().push(_payload([_vec()]))
    client = _client(tmp_path, transport)
    space = _space()
    store = FakeP19Store(space)
    tokenizer = qb.QwenBpeTokenizer()
    pipe = vp.RealVectorPipeline(store, client, space, tokenizer=tokenizer)
    rid = _rid("replay")
    # 首次：嵌入成功 + upsert 成功，但 CAS 前中断（模拟：手动只跑前两步）
    chunks = qb.real_chunk_text("中国人民银行宣布降准。", tokenizer)
    vectors = client.embed_documents([c.text for c in chunks])
    store.upsert_chunks(record_id=rid,
                        chunks=[(c.chunk_id, v) for c, v in zip(chunks, vectors)],
                        scope_id="default", arrival_seq=3)
    assert store.get_vector_state(rid) == "pending"     # 半写态
    # 重放：嵌入缓存命中零 API + upsert reused + CAS ready
    report = pipe.ingest_record(record_id=rid, text="中国人民银行宣布降准。",
                                scope_id="default", arrival_seq=3)
    assert report.state == "ready"
    assert all(outcome == "reused" for _, outcome in report.outcomes)
    assert len(transport.calls) == 1                    # 重放零 API（缓存命中）


def test_pipeline_space_dimension_mismatch_rejected(tmp_path):
    transport = FakeTransport()
    client = _client(tmp_path, transport)  # dimension=4
    other = EmbeddingSpace("mock_embedding", "fixture_v1", "codepoint_v1", "none",
                           "COSINE", 8, "plain_v1", "plain_v1",
                           "tokens512_overlap64_bpe_v1", "p09_v1")
    with pytest.raises(ValueError, match="(?i)dimension|space"):
        vp.RealVectorPipeline(FakeP19Store(other), client, other,
                              tokenizer=qb.QwenBpeTokenizer())


# ---------- 召回侧 fake 双倍体（MilvusVectorStore 既有形态） ----------

class FakeMilvus:
    def __init__(self):
        self.search_rows = []

    def search(self, **kwargs):
        return [self.search_rows[: kwargs["limit"]]]


class FakeES:
    def __init__(self, docs, prefix):
        self.docs = {d["record_id"]: d for d in docs}
        self.prefix = prefix

    def get(self, *, index, id, realtime):
        assert index.startswith(self.prefix)
        if id not in self.docs:
            return {"_index": index, "_id": id, "found": False}
        return {"_index": index, "_id": id, "found": True,
                "_source": self.docs[id]}


def _doc(space, record_id, item_id, seq):
    return {"scope_id": "default", "business_date": DAY, "record_id": record_id,
            "item_id": item_id, "arrival_seq": seq, "text": "中国人民银行宣布降准",
            "embedding_space_id": space.space_id,
            "expires_at": (NOW + timedelta(days=1)).isoformat()}


def _hit(space, record_id, chunk_id, seq):
    return {"id": f"{record_id}_{chunk_id}", "distance": 0.98,
            "entity": {"record_id": record_id, "scope_id": "default",
                       "business_date": DAY, "arrival_seq": seq,
                       "embedding_space_id": space.space_id, "chunk_id": chunk_id}}


def _searcher(tmp_path, transport, *, docs=(), hits=()):
    client = _client(tmp_path, transport)
    space = _space()
    collection = f"p19_b5s4unit_{space.space_id}"
    prefix = "p19-batch-b5s4unit-"
    store = MilvusVectorStore(FakeMilvus(), FakeES(docs, prefix), collection,
                              prefix, space, clock=lambda: NOW)
    store.milvus.search_rows = list(hits)
    assembly = eq.build_real_vector_searcher(
        client=client, space=space, store=store)
    return assembly, store, transport, space


def _request(space, text="中国人民银行宣布降准", **overrides):
    base = dict(scope_id="default", business_date=DAY, record_id=_rid("current"),
                item_id="9-1", arrival_seq=5, text=text, visible_seq=4,
                prepared_seq=4, embedding_space_id=space.space_id)
    base.update(overrides)
    return RecallRequest(**base)


# ---------- 查询编码层 + 真 VectorSearcher 装配（§4.1/§4.6） ----------

def test_searcher_success_embeds_query_and_searches(tmp_path):
    rid = _rid("cand")
    space_holder = {}
    transport = FakeTransport().push(_payload([_vec()]))
    client = _client(tmp_path, transport)
    space = _space()
    doc = _doc(space, rid, "9-0", 4)
    store = MilvusVectorStore(FakeMilvus(), FakeES([doc], "p19-batch-b5s4unit-"),
                              f"p19_b5s4unit_{space.space_id}",
                              "p19-batch-b5s4unit-", space, clock=lambda: NOW)
    store.milvus.search_rows = [_hit(space, rid, 0, 4)]
    searcher = eq.RealVectorSearcher(store, eq.QueryEmbedder(client, space))
    result = searcher.search(_request(space), None)   # B4 端口同型调用
    assert result.status == "complete"
    assert [c.record_id for c in result.candidates] == [rid]
    assert result.query_version == VECTOR_QUERY_VERSION == "embedding_v1"  # 不升
    assert result.prepared_seq == 4                   # prepared_seq 直传
    assert len(transport.calls) == 1                  # 查询向量由本片供给
    assert transport.calls[0] == ["中国人民银行宣布降准"]  # 对称编码同文


def test_searcher_explicit_query_vector_passthrough(tmp_path):
    space = _space()
    store = MilvusVectorStore(FakeMilvus(), FakeES([], "p19-batch-b5s4unit-"),
                              f"p19_b5s4unit_{space.space_id}",
                              "p19-batch-b5s4unit-", space, clock=lambda: NOW)
    transport = FakeTransport()
    searcher = eq.RealVectorSearcher(store, eq.QueryEmbedder(_client(tmp_path, transport), space))
    result = searcher.search(_request(space), _vec())  # 显式向量直传 store
    assert result.status == "complete"
    assert transport.calls == []                       # 不再嵌入


def test_searcher_api_failure_unavailable_not_raised(tmp_path):
    transport = FakeTransport()
    for _ in range(3):
        transport.push(ec.EmbeddingConnectionFault("down"))
    searcher, store, transport2, space = _searcher(tmp_path, transport)
    result = searcher.search(_request(space), None)
    assert result.channel == "embedding"
    assert result.status == "unavailable"
    assert result.candidates == ()
    assert result.error_code == "EMBEDDING_QUERY_FAILED"  # §2.5 查询径映射
    assert result.query_version == "embedding_v1"
    assert result.visible_seq == 4 and result.prepared_seq == 4
    assert result.elapsed_ms is not None


def test_searcher_malformed_response_unavailable_not_raised(tmp_path):
    transport = FakeTransport().push({"data": [{"embedding": [1.0, 2.0]}]})
    searcher, _, _, space = _searcher(tmp_path, transport)
    result = searcher.search(_request(space), None)
    assert result.status == "unavailable"
    assert result.error_code == "EMBEDDING_QUERY_FAILED"  # 畸形同码不裸抛


def test_searcher_budget_exceeded_unavailable(tmp_path):
    transport = FakeTransport().push(_payload([_vec()], tokens=10))
    client = _client(tmp_path, transport)
    # 用满预算后再查询 → 超闸 fail-closed → unavailable（不静默降级）
    client._budget_used = 5_000_000
    searcher = eq.RealVectorSearcher(
        MilvusVectorStore(FakeMilvus(), FakeES([], "p19-batch-b5s4unit-"),
                          f"p19_b5s4unit_{_space().space_id}",
                          "p19-batch-b5s4unit-", _space(), clock=lambda: NOW),
        eq.QueryEmbedder(client, _space()))
    result = searcher.search(_request(_space()), None)
    assert result.status == "unavailable"
    assert result.error_code == "EMBEDDING_QUERY_FAILED"
    assert transport.calls == []  # 超闸零调用


def test_searcher_empty_text_store_empty_body_not_preintercepted(tmp_path):
    transport = FakeTransport()
    searcher, _, _, space = _searcher(tmp_path, transport)
    result = searcher.search(_request(space, text="  \n "), None)
    assert result.status == "not_applicable"
    assert result.error_code == "EMPTY_BODY"     # store 既有语义（不前置拦截）
    assert transport.calls == []                  # 空文本零嵌入调用


def test_searcher_space_unconfirmed_distinct_from_query_failed(tmp_path):
    transport = FakeTransport()
    searcher, _, _, space = _searcher(tmp_path, transport)
    request = _request(space, embedding_space_id="s_" + "0" * 40)
    result = searcher.search(request, None)
    assert result.status == "unavailable"
    assert result.error_code == "SPACE_UNCONFIRMED"  # 两码不混（§4.6）
    assert transport.calls == []                      # 空间未确认不嵌入


def test_query_embedder_dimension_mismatch_rejected(tmp_path):
    transport = FakeTransport()
    client = _client(tmp_path, transport)
    other = EmbeddingSpace("mock_embedding", "fixture_v1", "codepoint_v1", "none",
                           "COSINE", 8, "plain_v1", "plain_v1",
                           "tokens512_overlap64_bpe_v1", "p09_v1")
    with pytest.raises(ValueError, match="(?i)dimension|space"):
        eq.QueryEmbedder(client, other)


def test_query_embedder_lru_second_call_zero_api(tmp_path):
    transport = FakeTransport().push(_payload([_vec()]))
    client = _client(tmp_path, transport)
    embedder = eq.QueryEmbedder(client, _space())
    assert embedder.embed_query("中国人民银行宣布降准") == _vec()
    assert embedder.embed_query("中国人民银行宣布降准") == _vec()
    assert len(transport.calls) == 1
