"""B5/E2 S4 写入真链编排（设计 §3.2）：chunk → embedding → upsert → CAS。

真链流程（record 四元组 + 正文 + 真 space）：
1. 分块  real_chunk_text(text, qwen_bpe_tokenizer)（chunking_version 新身份
   tokens512_overlap64_bpe_v1）；空文本 → () → P19VectorError（既有口径
   coordinator.py:44-45）→ vector_state=failed（诚实失败，INV-1）；
   单 chunk >6000 字符 → 客户端护栏截断嵌入输入（chunk 原文区间不动；
   截断事件入台账，mabc _truncate 先例）。
2. 嵌入  client.embed_documents（分批 ≤10 保序；磁盘缓存命中零 API）；
   终败 → vector_state=failed + EmbeddingApiError 传播（嵌入是纯函数无
   部分态，重试安全；重试预算记录职责在调用方/worker 层，本片无 worker）；
   畸形 → VectorWriteUnknown 传播且**不翻状态**（确认未知，fail-closed，
   与 milvus_store 全件同口径）。
3. 写入  store.upsert_chunks（既有执法全继承：ES 权威四元组对拍、
   upsert_count==1 + Strong 回读逐字段相等、同主键同内容 reused /
   异内容 VectorIdentityConflict）。R3-H4-② upsert 前置源态门禁：
   源态非 pending（failed/ready）→ VectorStateTransitionError 拒绝零写入
   （孤儿 chunk fail-closed——旧序先写后 CAS 拒，chunk 已落无主记录认领）；
   源态 None（权威缺席）放行至 upsert 既有 VectorWriteUnknown 面（缺席即
   拒写，本无孤儿面）；pending→ready 并发翻转残余由 upsert 幂等 + CAS
   源态门禁收口（INV-6 单写者模型下可容忍，在案）。
4. 翻转  cas_vector_state(to="ready")（INV-6：源态 pending 门禁既有）；
   半写恢复：重放即 reused/created 幂等（N12），嵌入缓存使重放零 API 成本。

INV-6 独占权不变：pending 由创建者写，本编排仅 pending→ready/failed；
INV-1 次序不变：逐 chunk 写确认后才 CAS ready；嵌入阶段失败绝不翻 ready。
fake 轨 upsert_vectors 本体不动（coordinator.py，M21 演进仅新增调用方）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from news_flash_dedup.recall.vector_space import EmbeddingSpace
from news_flash_dedup.vector.coordinator import P19VectorError
from news_flash_dedup.vector.embedding_client import EmbeddingApiError, EmbeddingClient
from news_flash_dedup.vector.milvus_store import VectorStateTransitionError
from news_flash_dedup.vector.qwen_bpe import QwenBpeTokenizer, real_chunk_text


@dataclass(frozen=True)
class IngestReport:
    """单记录写入真链报告（随 shadow/UAT/回填台账落盘）。"""
    record_id: str
    chunk_ids: tuple[int, ...]
    outcomes: tuple[tuple[str, str], ...]     # (vector_id, "created"|"reused")
    state: str                                # "ready"
    truncated_chunk_ids: tuple[int, ...]      # 6000 字护栏截断事件
    tokens_used: int                          # 本记录服务端 usage 计账
    cache_hits: int                           # 本记录磁盘缓存命中数
    chunking_version: str


class RealVectorPipeline:
    """写入真链编排器（§3.2；store 为 RealMilvusP19Store 鸭式接口）。

    INV-4 防线：client.dimension / space.dimension / store 空间三者一致性
    装配期断言——空间不符即拒，不待到写径才炸。
    """

    def __init__(self, store: Any, client: EmbeddingClient,
                 space: EmbeddingSpace, *,
                 tokenizer: QwenBpeTokenizer) -> None:
        if client.dimension != space.dimension:
            raise ValueError(
                f"client dimension {client.dimension} differs from space "
                f"dimension {space.dimension} (INV-4 防混)")
        store_space = getattr(store, "space", None)
        if store_space is None:
            store_config = getattr(store, "config", None)
            store_space = getattr(store_config, "space", None)
        if store_space is not None and store_space.space_id != space.space_id:
            raise ValueError(
                "store space differs from pipeline space (INV-4 防混)")
        self._store = store
        self._client = client
        self._space = space
        self._tokenizer = tokenizer

    def ingest_record(self, *, record_id: str, text: str, scope_id: str,
                      arrival_seq: int) -> IngestReport:
        """单记录真链：分块 → 嵌入 → upsert → CAS ready（失败语义 §3.2/§2.5）。"""
        chunks = real_chunk_text(text, self._tokenizer)
        if not chunks:
            # 空文本 = 输入缺陷（非确认问题）→ 诚实失败（既有口径）
            self._store.cas_vector_state(record_id, to="failed")
            raise P19VectorError("chunks must be non-empty")
        truncated = tuple(
            chunk.chunk_id for chunk in chunks
            if len(chunk.text) > self._client.max_input_chars)
        usage_before = self._client.usage_snapshot()
        try:
            vectors = self._client.embed_documents(
                [chunk.text for chunk in chunks])
        except EmbeddingApiError:
            # 嵌入终败 = 确定的任务失败（重试安全、无部分态）→ failed
            self._store.cas_vector_state(record_id, to="failed")
            raise
        # VectorWriteUnknown（畸形/漂移）不经此捕获——不翻状态传播（确认未知）
        # R3-H4-② upsert 前置源态门禁：非 pending 拒绝零写入（孤儿 chunk
        # fail-closed，与 cas_vector_state 同源词表 VectorStateTransitionError）；
        # 源态 None（权威缺席）放行至 upsert 既有缺席拒写面（无孤儿面）。
        source_state = self._store.get_vector_state(record_id)
        if source_state is not None and source_state != "pending":
            raise VectorStateTransitionError(
                f"vector pipeline upsert requires source=pending; got "
                f"{source_state!r} (orphan-chunk fail-closed, R3-H4-②)"
            )
        outcomes = self._store.upsert_chunks(
            record_id=record_id,
            chunks=[(chunk.chunk_id, list(vector))
                    for chunk, vector in zip(chunks, vectors)],
            scope_id=scope_id, arrival_seq=arrival_seq)
        self._store.cas_vector_state(record_id, to="ready")
        usage_after = self._client.usage_snapshot()
        return IngestReport(
            record_id=record_id,
            chunk_ids=tuple(chunk.chunk_id for chunk in chunks),
            outcomes=tuple(outcomes),
            state="ready",
            truncated_chunk_ids=truncated,
            tokens_used=(usage_after["total_tokens"]
                         - usage_before["total_tokens"]),
            cache_hits=(usage_after["cache_hits"] - usage_before["cache_hits"]),
            chunking_version=chunks[0].chunking_version,
        )


__all__ = ["IngestReport", "RealVectorPipeline"]
