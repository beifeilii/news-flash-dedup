"""api/host.py 组装根（B4 施工窗；R154 裁定 P-Q8 并入；设计=
log\设计-真链路接线包.md §三-F4 + log\设计-P21接线包.md §三-Q4）。

组装序（单源接线，fail-closed）：
1. 模式闸解析（recall.service.mode_from_environment 单源；未知值
   RecallModeInvalid 冒泡拒装）；
2. F2 水位读侧 + F2 准备驱动 + F1 召回服务 + 注册序扫描 + F0 worker
   （live 缺 commit_store / shadow 缺 artifact_sink → ValueError，
   worker 构造面同纪律——协议自检 persist/es_store.py:556-558 先行）；
3. Q2 查询端口（ElasticsearchBatchStore 语义）+ Q3 就绪聚合（默认探针或
   显式注入）+ Q1 查询端点 app；
4. 受理面三件套（admission_port/context_provider/max_http_bytes）要么全注
   要么全缺——部分注入 → ValueError（防半接线受理面裸奔）。

前缀闸族纪律：index_prefix 必须 p01-batch- 前缀（batch 命名空间现役闸）；
持久化/集群触达闸族（DEPLOY_ENV+CONFIRM）不在本层越权——RealESP18Store 等
真层件自带闸（es_client.py:48-53），本层只接线不复制闸逻辑。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from news_flash_dedup.admission import BUSINESS_ZONE, CONTROL_INDEX
from news_flash_dedup.batch_admission import HEAD_ID
from news_flash_dedup.batch_es_store import ElasticsearchBatchStore
from news_flash_dedup.decide.judge_version_config import default_judge_version
from news_flash_dedup.es_admission_schema import day_index
from news_flash_dedup.product.readiness import ReadinessPort
from news_flash_dedup.product.task_query import TaskQueryPort
from news_flash_dedup.recall.prepare import (
    ElasticsearchWatermarkProvider,
    PrepareWorker,
)
from news_flash_dedup.recall.service import (
    NullVectorSearcher,
    RecallService,
    mode_from_environment,
)
from news_flash_dedup.recall.worker import (
    DedupWorker,
    ElasticsearchRegistrationScanner,
)

from .http import create_app
from .query import create_query_app

_PREFIX_GATE = "p01-batch-"
# R3-M4 五通道词表统一（readiness.py:27 _CHANNELS 同源锚；旧四面缺 embedding
# 致 recall_chain_unavailable:embedding 成词表死码）：
# - hash/near/bm25：ES 词法通道——实查日索引在场；
# - embedding：向量通道——实查 Milvus 集合在场（经 vector_searcher 内仓取达）；
# - entity：实体通道——实查日索引 + 准备产物（日控制 checkpoint 快照）在场。
_DAY_INDEX_CHANNELS = ("hash", "near", "bm25")
_PREPARED_SNAPSHOT_KEY = "recall_prepared"   # prepare.py _SNAPSHOT_KEY 同源锚（F2④）


@dataclass(frozen=True)
class HostAssembly:
    """组装产物（只读句柄面）。"""
    mode: str
    query_app: Any
    readiness_port: ReadinessPort
    query_port: TaskQueryPort
    worker: DedupWorker
    admission_app: Any
    recall_service: RecallService
    prepare_worker: PrepareWorker
    watermark_provider: ElasticsearchWatermarkProvider


def _truthy(value: Any) -> bool:
    return bool(value() if callable(value) else value)


def default_probes(*, es_client: Any, index_prefix: str, mode: str,
                   clock: Callable[[], datetime],
                   vector_searcher: Any = None,
                   collector_alive: Any = None,
                   delivery_route_configured: Any = None,
                   watermark_provider: Any = None,
                   business_date: str | None = None,
                   scope_id: str = "default",
                   ) -> dict[str, Callable[[], str | None]]:
    """默认探针组（§三-Q4 装配面）。

    fixed：es_control_read（HEAD 文档实时 GET 可读）+ collector_alive +
    delivery_route_configured（后两者缺省注入→fail-closed 报码，不默认绿）；
    shadow/live：+ 五通道链路实查（R3-M4：hash/near/bm25 查日索引在场、
    embedding 查 Milvus 集合在场、entity 查日索引+准备产物在场）+ 水位可读
    （scope_id 注入域，不再硬编码 "default"）+ 向量缺口（shadow→degraded
    embedding_space_unconfirmed；live→fatal vector_channel_unavailable——
    §七-7.2 分册语义）。
    """
    store = ElasticsearchBatchStore(es_client, index_prefix=index_prefix)

    def es_control_read() -> str | None:
        try:
            head = store.get(CONTROL_INDEX, HEAD_ID)
        except Exception:
            return "es_control_read_failed"
        return None if head is not None else "es_control_read_failed"

    def collector() -> str | None:
        if collector_alive is None:
            return "collector_not_running"      # 缺省注入→fail-closed
        return None if _truthy(collector_alive) else "collector_not_running"

    def delivery_route() -> str | None:
        if delivery_route_configured is None:
            return "delivery_route_unconfigured"
        return (None if _truthy(delivery_route_configured)
                else "delivery_route_unconfigured")

    probes: dict[str, Callable[[], str | None]] = {
        "es_control_read": es_control_read,
        "collector": collector,
        "delivery_route": delivery_route,
    }
    if mode == "fixed":
        return probes

    if business_date is None:
        now = clock()
        if now.tzinfo is None:
            raise ValueError("clock must be timezone aware")
        business_date = now.astimezone(BUSINESS_ZONE).date().isoformat()
    physical_items = index_prefix + day_index(business_date)

    for channel in _DAY_INDEX_CHANNELS:
        def chain(channel: str = channel) -> str | None:
            # hash/near/bm25 词法通道实查：日索引在场（R3-M4）
            try:
                exists = bool(es_client.indices.exists(index=physical_items))
            except Exception:
                exists = False
            return (None if exists
                    else f"recall_chain_unavailable:{channel}")
        probes[f"recall_chain_{channel}"] = chain

    def embedding_chain() -> str | None:
        # embedding 通道实查：Milvus 集合在场（R3-M4，五通道死码消除）。
        # 集合句柄经 vector_searcher 内仓取达（MilvusVectorStore.milvus /
        # collection_name 为公开面；store 公开属性优先、_store 私有名兜底）；
        # 未接线/鸭式无仓 searcher → None——链缺口归 vector_gap 探针所有
        # （embedding_space_unconfirmed/vector_channel_unavailable 两码不混，
        # 不重复报码）。
        vstore = getattr(vector_searcher, "store", None)
        if vstore is None:
            vstore = getattr(vector_searcher, "_store", None)
        milvus = getattr(vstore, "milvus", None)
        collection = getattr(vstore, "collection_name", None)
        if milvus is None or not collection:
            return None
        try:
            present = bool(milvus.has_collection(collection_name=collection))
        except Exception:
            present = False
        return None if present else "recall_chain_unavailable:embedding"
    probes["recall_chain_embedding"] = embedding_chain

    def entity_chain() -> str | None:
        # entity 通道实查：日索引在场 + 准备产物在场（R3-M4）——准备产物 =
        # 日控制文档 checkpoint.recall_prepared 快照（F2④ 准备前沿产物，
        # entity_ids 由 PrepareWorker 写入日文档后快照推进）。
        try:
            if not bool(es_client.indices.exists(index=physical_items)):
                return "recall_chain_unavailable:entity"
            day = store.get(
                CONTROL_INDEX,
                ElasticsearchWatermarkProvider.day_key(scope_id, business_date))
        except Exception:
            return "recall_chain_unavailable:entity"
        source = day["source"] if day is not None else None
        checkpoint = source.get("checkpoint") if isinstance(source, Mapping) else None
        snapshot = (checkpoint.get(_PREPARED_SNAPSHOT_KEY)
                    if isinstance(checkpoint, Mapping) else None)
        return (None if isinstance(snapshot, Mapping)
                else "recall_chain_unavailable:entity")
    probes["recall_chain_entity"] = entity_chain

    def watermark() -> str | None:
        if watermark_provider is None:
            return "watermark_unreadable"
        try:
            watermark_provider.visible_seq(scope_id, business_date)
            watermark_provider.prepared_seq(scope_id, business_date)
        except Exception:
            return "watermark_unreadable"
        return None
    probes["watermark"] = watermark

    def vector_gap() -> str | None:
        real = (vector_searcher is not None
                and not isinstance(vector_searcher, NullVectorSearcher))
        if real:
            return None
        return ("embedding_space_unconfirmed" if mode == "shadow"
                else "vector_channel_unavailable")
    probes["vector_gap"] = vector_gap
    return probes


def create_host(*, es_client: Any, index_prefix: str, scope_id: str = "default",
                status_authenticated: Callable | None,
                admission_port: Any = None, context_provider: Any = None,
                max_http_bytes: Any = None, env: Mapping[str, str] | None = None,
                clock: Callable[[], datetime] | None = None,
                vector_searcher: Any = None, fact_supply: Any = None,
                commit_store: Any = None, artifact_sink: Any = None,
                probes: Mapping[str, Callable] | None = None,
                collector_alive: Any = None,
                delivery_route_configured: Any = None,
                max_stale_retries: int = 3,
                dictionary: Any = None,
                dictionary_version: str = "dict_v1",
                debug_router: Any = None) -> HostAssembly:
    """组装根：模式闸→召回链四件→查询两件→端点 app→（可选）受理 app。

    debug_router（log\\debug-endpoint\\方案设计-v1.md §D1）：可选预制只读
    日志查看 router（api/debug.py create_debug_router 产物）；None→不挂
    /debug（现状不变），注入→挂到 query_app。本层只接线不复制闸逻辑——
    token/日志源校验归 create_debug_router 工厂所有。
    """
    if es_client is None:
        raise ValueError("es_client must be explicitly injected")
    if not isinstance(index_prefix, str) or not index_prefix.startswith(
            _PREFIX_GATE):
        raise ValueError("index_prefix must carry the p01-batch- gate prefix")
    if status_authenticated is None or not callable(status_authenticated):
        raise ValueError("status_authenticated must be explicitly injected")
    if not scope_id:
        raise ValueError("scope_id must be non-empty")
    clock = clock or (lambda: datetime.now(timezone.utc))
    mode = mode_from_environment(env)               # 模式闸单源解析
    # 终审复审补丁（唯一权威单源传递）：判官版本配置在组合根创建一次
    # （default_judge_version 按 DEDUP_JUDGE_DECISION_MODE 分发），同一
    # JudgeVersionConfig 一路传给 DedupWorker（影子腿预装配+生效腿
    # commit_one→decide_for_task），禁止下游再各自读环境变量。
    judge_version_config = default_judge_version(env)

    watermark_provider = ElasticsearchWatermarkProvider(es_client, index_prefix)
    prepare_worker = PrepareWorker(es_client, index_prefix, clock=clock)
    recall_service = RecallService(
        es_client, index_prefix, watermark_provider=watermark_provider,
        vector_searcher=vector_searcher, fact_supply=fact_supply, clock=clock)
    scanner = ElasticsearchRegistrationScanner(es_client, index_prefix)
    worker = DedupWorker(                           # live/shadow 依赖校验在内
        mode=mode, recall_service=recall_service,
        prepare_worker=prepare_worker,
        watermark_provider=watermark_provider, scanner=scanner,
        commit_store=commit_store, artifact_sink=artifact_sink, clock=clock,
        max_stale_retries=max_stale_retries,
        dictionary=dictionary, dictionary_version=dictionary_version,
        judge_version_config=judge_version_config)

    store = ElasticsearchBatchStore(es_client, index_prefix=index_prefix)
    query_port = TaskQueryPort(store, scope_id=scope_id, clock=clock)
    if probes is None:
        probes = default_probes(
            es_client=es_client, index_prefix=index_prefix, mode=mode,
            clock=clock, vector_searcher=vector_searcher,
            collector_alive=collector_alive,
            delivery_route_configured=delivery_route_configured,
            watermark_provider=watermark_provider, scope_id=scope_id)
    readiness_port = ReadinessPort(probes, mode=mode)
    query_app = create_query_app(
        query_port=query_port, readiness_port=readiness_port,
        status_authenticated=status_authenticated)
    if debug_router is not None:
        query_app.include_router(debug_router)

    admission_injected = (admission_port is not None,
                          context_provider is not None,
                          max_http_bytes is not None)
    if any(admission_injected) and not all(admission_injected):
        raise ValueError(
            "admission wiring is all-or-nothing: admission_port, "
            "context_provider and max_http_bytes must be injected together")
    admission_app = (create_app(admission_port=admission_port,
                                context_provider=context_provider,
                                max_http_bytes=max_http_bytes)
                     if all(admission_injected) else None)

    return HostAssembly(
        mode=mode, query_app=query_app, readiness_port=readiness_port,
        query_port=query_port, worker=worker, admission_app=admission_app,
        recall_service=recall_service, prepare_worker=prepare_worker,
        watermark_provider=watermark_provider)


__all__ = ["HostAssembly", "create_host", "default_probes"]
