"""B5/E2 S4 召回激活：查询编码层 + 真 VectorSearcher 装配（设计 §4.1/§4.6）。

本片文件面单件（目标文档.md L182 台账 R136：B4 禁触 embedding_query.py）。

接缝 = VectorSearcher 端口签名（B1 过闸稿 §2.4 L140 / §三-F1 L153④）：
鸭式协议 search(request, query_vector) -> ChannelResult，由 B4 侧召回编排器
RecallService 构造注入；B4 过渡态 NullVectorSearcher.search(request, None)
同型调用面——query_vector=None 时本片经 QueryEmbedder 供给真向量（
「真 MilvusVectorStore+真 query_vector 供给，经 B5 文件 recall/
embedding_query.py」过闸稿 §2.4 L140 全形）。装配零改动面 =
编排器/准备/worker/decide/commit（R164 回锚）。

失败语义（§2.5 查询径 + §4.6 错误码分立）：
- 嵌入 API 终败 → ChannelResult("embedding","unavailable",(),"embedding_v1",
  error_code="EMBEDDING_QUERY_FAILED")——不裸抛（对齐 vector_store.py:317-339
  错误面）；与过渡态 SPACE_UNCONFIRMED（未接线）语义分立，两码不混；
- 响应畸形/维度漂移 → 同上 unavailable + EMBEDDING_QUERY_FAILED；
- 空文本：查询编码层不前置拦截——直接委托 store 既有 not_applicable/
  EMPTY_BODY 分支（vector_store.py:317-320，空文本不耗 API）；
- 铁律：任何失败路径不产出零向量/均值向量冒充成功。

对称编码（§4.1）：query 与 document 同一编码路径（root 09 §6.4 L424：
托管 v3 不使用 query 指令；N11 §三 query/document 一致性 ✅）。
prepared_seq 直传（vector_store.py:338 既有；fusion.py:123-126
VECTOR_FRONTIER_UNPROVEN 依赖）。

（W-R3c 散项 h 锚勘误：本 docstring vector_store.py 三处锚原 192-202
错误面 / 180-183 EMPTY_BODY / 311 prepared_seq 直传系 W2Fβ 系演进后
漂移，已勘正为 198-220 / 198-201 / 329；E 批 H-02(b) 向 vector_store.py
增件（VectorFrontierProvider 等）致再漂移，EW 实现窗裁定书 B1 准机械锚
更新——再勘正为 298-320 / 298-301 / 446：行号锚以动态内容钉守
（tests/unit/test_winr3c_repairs.py h 族）；fusion.py:123-126 锚实测
仍准不动。W2 ⑩①-5 预算接线向 vector_store.py search 签名/timeout
透传增件致三锚再漂移、F-4 包络窄修（A+B）再移 1-5 行——同 B1 机械
锚先例再勘正为 302-324 / 302-305 / 455，内容钉语义逐字不动。
P2 确定性破序（2026-10-09）向 vector_store.py 模块头/检索环增件致
三锚再漂移（+15/+15/+9）——同 B1 先例再勘正为 317-339 / 317-320 /
338，内容钉语义仍逐字不动。）
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from news_flash_dedup.recall.models import ChannelResult, RecallRequest
from news_flash_dedup.recall.vector_space import EmbeddingSpace
from news_flash_dedup.recall.vector_store import (
    VECTOR_QUERY_VERSION,
    MilvusVectorStore,
    VectorWriteUnknown,
)
from news_flash_dedup.vector.embedding_client import (
    EmbeddingApiError,
    EmbeddingClient,
)


def _elapsed_ms_since(started: datetime, now: datetime) -> float:
    return round((now - started).total_seconds() * 1000.0, 3)


class QueryEmbedder:
    """查询编码器（§4.1）：对称编码 + 客户端 LRU/可选磁盘层。

    INV-4 防线：装配期断言 client.dimension == space.dimension——空间与
    客户端维度不符即拒（防混空间地基）。
    """

    def __init__(self, client: EmbeddingClient, space: EmbeddingSpace) -> None:
        if client.dimension != space.dimension:
            raise ValueError(
                f"client dimension {client.dimension} differs from space "
                f"dimension {space.dimension} (INV-4 防混)")
        self._client = client
        self._space = space

    @property
    def space(self) -> EmbeddingSpace:
        return self._space

    def embed_query(self, text: str) -> list[float]:
        """query 编码 = document 同一编码路径（对称方案，无 query 指令）。"""
        return self._client.embed_query(text)


class RealVectorSearcher:
    """真 VectorSearcher 装配（§4.6 端口本体）：闸扩展后真 MilvusVectorStore
    + QueryEmbedder 供给 query_vector。

    端口形态：search(request, query_vector) -> ChannelResult（鸭式，与 B4
    NullVectorSearcher 同型）；query_vector 非 None 时直传 store（调用方
    预供向量形态），None 时本片经 QueryEmbedder 现供。
    """

    def __init__(self, store: MilvusVectorStore, embedder: QueryEmbedder, *,
                 clock: Callable[[], datetime] | None = None) -> None:
        if store.space.space_id != embedder.space.space_id:
            raise ValueError(
                "store space differs from embedder space (INV-4 防混)")
        self._store = store
        self._embedder = embedder
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def search(self, request: RecallRequest,
               query_vector: list[float] | tuple[float, ...] | None = None
               ) -> ChannelResult:
        if query_vector is None:
            if (not request.text.strip()
                    or request.embedding_space_id != self._store.space.space_id):
                # 不前置拦截、不浪费嵌入：空文本 / 空间未确认均委托 store 既有
                # 分支自产语义（EMPTY_BODY / SPACE_UNCONFIRMED，store 先行闸
                # 不消费向量，零 API）——SPACE_UNCONFIRMED（未接线过渡态）与
                # EMBEDDING_QUERY_FAILED（接线后故障）两码不混（§4.6）。
                return self._store.search(request, [])
            started = self._clock()
            try:
                query_vector = self._embedder.embed_query(request.text)
            except (EmbeddingApiError, VectorWriteUnknown):
                # §2.5 查询径：终败/畸形同映射 unavailable +
                # EMBEDDING_QUERY_FAILED——不裸抛；与 SPACE_UNCONFIRMED
                # （未接线过渡态）语义分立。
                return ChannelResult(
                    "embedding", "unavailable", (), VECTOR_QUERY_VERSION,
                    visible_seq=request.visible_seq,
                    error_code="EMBEDDING_QUERY_FAILED",
                    prepared_seq=request.prepared_seq,
                    elapsed_ms=_elapsed_ms_since(started, self._clock()))
        return self._store.search(request, list(query_vector))


def build_real_vector_searcher(*, client: EmbeddingClient,
                               space: EmbeddingSpace,
                               store: MilvusVectorStore | None = None,
                               milvus: Any = None, es: Any = None,
                               collection_name: str | None = None,
                               es_index_prefix: str | None = None,
                               clock: Callable[[], datetime] | None = None,
                               max_search_limit: int = 640,
                               frontier_provider: Any = None) -> RealVectorSearcher:
    """装配真 VectorSearcher（§4.6）：闸扩展后真 store + 真 query_vector 供给。

    两形态：store 直给（测试/预装），或 milvus+es+collection_name+
    es_index_prefix 现构（生产/UAT 装配根）；集合名/ES 前缀经闸扩展后
    二态闭集词表执法（p19 族接纳，§4.5）。

    H-02(b)（E 批 H2-8）：frontier_provider 装配根透传（RecallService
    watermark_provider 风格）——现构形态注入 MilvusVectorStore 覆盖证明
    基求值；store 直给形态下 provider 属 store 侧既有装配，本函数不二传。
    """
    if store is None:
        if milvus is None or es is None or not collection_name or not es_index_prefix:
            raise ValueError(
                "store or (milvus, es, collection_name, es_index_prefix) required")
        store = MilvusVectorStore(milvus, es, collection_name, es_index_prefix,
                                  space, clock=clock,
                                  max_search_limit=max_search_limit,
                                  frontier_provider=frontier_provider)
    return RealVectorSearcher(store, QueryEmbedder(client, space), clock=clock)


__all__ = [
    "QueryEmbedder",
    "RealVectorSearcher",
    "build_real_vector_searcher",
]
