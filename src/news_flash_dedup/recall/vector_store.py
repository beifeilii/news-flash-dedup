"""P12 隔离 Milvus 向量索引及 ES 权威记录回查。"""

from __future__ import annotations

import hashlib
import json
import math
import re
import struct
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from news_flash_dedup.es_admission_schema import day_index
from news_flash_dedup.milvus_client import partition_name

from .es_gateway import candidate_eligible, elapsed_ms_since, response_body
from .models import ChannelResult, RecallCandidate, RecallRequest
from .vector_space import EmbeddingSpace, validate_vector, vector_id


VECTOR_QUERY_VERSION = "embedding_v1"
VECTOR_FIELDS = frozenset({
    "vector_id", "record_id", "scope_id", "business_date", "arrival_seq",
    "embedding_space_id", "chunk_id", "embedding",
})

# B5/E2 §4.5 闸扩展（F1 修复，设计指定唯一既有改动面）：集合/ES 前缀二态闭集
# 词表——现役 replay 形态或 P19 形态；两族均维持 endswith(space.space_id) 执法。
# P19 形态正则为 vector/milvus_store.py:46-49 P19_COLLECTION_PATTERN /
# P19_INDEX_PATTERN 前缀段的同源镜像——不得反向 import vector/milvus_store.py
# （其已 import 本模块，milvus_store.py:34-41，循环 import 结构性禁止）；
# 镜像等价由红测钉⑥（tests/unit/test_p19_gate_extension.py）对拍执法。
_REPLAY_COLLECTION_FORM = re.compile(
    r"news_dedup_replay_[a-z0-9]{6,16}_s_[0-9a-f]{40}\Z")
_P19_COLLECTION_FORM = re.compile(r"p19_[a-z0-9]{6,16}_s_[0-9a-f]{40}\Z")
_P01_ES_PREFIX_FORM = re.compile(r"p01-batch-[A-Za-z0-9-]+-\Z")
_P19_ES_PREFIX_FORM = re.compile(r"p19-batch-[a-z0-9]{6,16}-\Z")


class VectorIdentityConflict(ValueError):
    pass


class VectorWriteUnknown(RuntimeError):
    pass


def vector_row_fingerprint(row: Mapping) -> str:
    """M-09/H-02 共享件（E 批 H2-3）：向量行写确认指纹（纯函数单源）。

    规范形（红测 G2 钉）：`"sha256:" + sha256(json.dumps(row, sort_keys=True,
    separators=(",", ":"), ensure_ascii=False).encode("utf-8"))`——指纹口径与
    prepare_vector_row 出行规范形绑定；VECTOR_FIELDS 词表变更时本口径同步
    （E 批 R4 对冲）。
    """
    canonical = json.dumps(row, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False).encode("utf-8")
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


@dataclass(frozen=True)
class VectorCoverageProof:
    """H-02(b) H2-3：vector 覆盖证明基（类型化，空间/快照代际显式）。

    vector_frontier=L2 已证连续前沿（advance_prepared_frontier 同件推进）；
    hole_count=L3 对账开孔洞计数（0=无孔）。两维缺一直 False（F4/F5）。
    """
    scope_id: str
    business_date: str
    space_id: str
    vector_frontier: int
    hole_count: int
    schema_version: str

    def __post_init__(self) -> None:
        for name in ("scope_id", "business_date", "space_id", "schema_version"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise ValueError(name + " must be a nonempty str")
        if type(self.vector_frontier) is not int or self.vector_frontier < 0:
            raise ValueError("vector_frontier must be a nonnegative int")
        if type(self.hole_count) is not int or self.hole_count < 0:
            raise ValueError("hole_count must be a nonnegative int")


class VectorFrontierProvider:
    """H-02(b) H2-3 生产读侧：实时 GET 日控制文档 checkpoint.vector_prepared
    快照 → 类型化 VectorCoverageProof。

    fail-closed（F12）：日文档缺席/快照缺席/畸形/schema_version 未知/
    空间不符（F3 混空间）/ES 不可达 → None（不猜、不传播异常）；
    水位语义同 prepare.py:107-137——每次实时 GET，不缓存（G12 钉）。
    """

    SNAPSHOT_KEY = "vector_prepared"
    SNAPSHOT_SCHEMA = "vector-prepared-v1"

    def __init__(self, store: Any, index_prefix: str) -> None:
        # store：ElasticsearchBatchStore 最小鸭式（get(index, doc_id) 返回
        # {"_source": ...} 形原始文档；缺失→None）。
        self._store = store
        self.index_prefix = index_prefix

    @staticmethod
    def day_key(scope_id: str, business_date: str) -> str:
        return f"day:{scope_id}:{business_date}"   # prepare.py:117-119 同键形

    def vector_frontier(self, scope_id: str, business_date: str,
                        space_id: str) -> VectorCoverageProof | None:
        from news_flash_dedup.admission import CONTROL_INDEX

        try:
            doc = self._store.get(
                CONTROL_INDEX, self.day_key(scope_id, business_date))
        except Exception:
            return None                                # F12：读不可达→None
        if not isinstance(doc, Mapping):
            return None
        source = doc.get("_source")
        if not isinstance(source, Mapping):
            return None
        checkpoint = source.get("checkpoint")
        snapshot = (checkpoint.get(self.SNAPSHOT_KEY)
                    if isinstance(checkpoint, Mapping) else None)
        if not isinstance(snapshot, Mapping):
            return None
        if snapshot.get("schema_version") != self.SNAPSHOT_SCHEMA:
            return None                                # F2：代际未知→None
        if snapshot.get("space_id") != space_id:
            return None                                # F3：混空间→None
        frontier = snapshot.get("vector_frontier")
        holes = snapshot.get("hole_count")
        if (type(frontier) is not int or type(holes) is not int
                or frontier < 0 or holes < 0):
            return None                                # 型错/负值→None（G11 钉+F-4 F12 包络）
        return VectorCoverageProof(
            scope_id=scope_id, business_date=business_date, space_id=space_id,
            vector_frontier=frontier, hole_count=holes,
            schema_version=self.SNAPSHOT_SCHEMA)


def vector_schema(space: EmbeddingSpace):
    from pymilvus import DataType, MilvusClient

    schema = MilvusClient.create_schema(auto_id=False, enable_dynamic_field=False)
    schema.add_field("vector_id", DataType.VARCHAR, is_primary=True,
                     auto_id=False, max_length=96)
    schema.add_field("record_id", DataType.VARCHAR, max_length=64)
    schema.add_field("scope_id", DataType.VARCHAR, max_length=128)
    schema.add_field("business_date", DataType.VARCHAR, max_length=10)
    schema.add_field("arrival_seq", DataType.INT64)
    schema.add_field("embedding_space_id", DataType.VARCHAR, max_length=64)
    schema.add_field("chunk_id", DataType.INT64)
    schema.add_field("embedding", DataType.FLOAT_VECTOR, dim=space.dimension)
    return schema


def vector_index_params():
    from pymilvus import MilvusClient

    params = MilvusClient.prepare_index_params()
    params.add_index(field_name="embedding", index_type="FLAT", metric_type="COSINE")
    return params


def prepare_vector_row(record_id: str, scope_id: str, business_date: str,
                       arrival_seq: int, space: EmbeddingSpace, chunk_id: int,
                       embedding: list[float]) -> dict:
    key = vector_id(record_id, chunk_id)
    partition_name(business_date)
    if not re.fullmatch(r"[a-z0-9_-]{1,128}", scope_id):
        raise ValueError("scope_id must be a trusted scalar identifier")
    if type(arrival_seq) is not int or arrival_seq < 1:
        raise ValueError("arrival_seq must be positive")
    validated = validate_vector(embedding, space)
    try:
        packed = [struct.unpack("<f", struct.pack("<f", value))[0]
                  for value in validated]
    except OverflowError as error:
        raise ValueError("embedding exceeds float32 range") from error
    if not any(value != 0 for value in packed):
        raise ValueError("embedding becomes zero in float32")
    return {
        "vector_id": key, "record_id": record_id, "scope_id": scope_id,
        "business_date": business_date, "arrival_seq": arrival_seq,
        "embedding_space_id": space.space_id, "chunk_id": chunk_id,
        "embedding": packed,
    }


class MilvusVectorStore:
    def __init__(self, milvus: Any, es: Any, collection_name: str,
                 es_index_prefix: str, space: EmbeddingSpace, *,
                 clock: Callable[[], datetime] | None = None,
                 max_search_limit: int = 640,
                 frontier_provider: Any = None) -> None:
        # 集合名闸：二态闭集词表（§4.5-1）——replay 形态或 P19 形态；
        # endswith(space_id) 执法两族同型；fail-closed 保持（同类型同风格，
        # 保留集合名/跨代前缀/第三形态一律拒收——闭集扩展非通配放开）。
        if (not (_REPLAY_COLLECTION_FORM.fullmatch(collection_name)
                 or _P19_COLLECTION_FORM.fullmatch(collection_name))
                or not collection_name.endswith(space.space_id)):
            raise ValueError("collection must be an isolated replay of this space")
        # ES 前缀闸：二态闭集词表（§4.5-2）——p01-batch- 或 p19-batch-<run>-。
        if not (_P01_ES_PREFIX_FORM.fullmatch(es_index_prefix)
                or _P19_ES_PREFIX_FORM.fullmatch(es_index_prefix)):
            raise ValueError("ES index prefix must be isolated")
        if max_search_limit < 80:
            raise ValueError("search limit must cover the initial 80 chunks")
        self.milvus = milvus
        self.es = es
        self.collection_name = collection_name
        self.es_index_prefix = es_index_prefix
        self.space = space
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.max_search_limit = max_search_limit
        # H-02(b)（E 批 H2-3 接线点）：证明基供源；None=无证明（coverage
        # 恒 False，过渡态语义逐字节不变，绿守卫 G4 钉）。
        self.frontier_provider = frontier_provider

    def _es_source(self, record_id: str, business_date: str) -> Mapping | None:
        index = self.es_index_prefix + day_index(business_date)
        result = response_body(self.es.get(index=index, id=record_id, realtime=True))
        if result.get("_index") != index or result.get("_id") != record_id:
            raise ValueError("ES readback identity differs")
        if result.get("found") is False and "_source" not in result:
            return None
        source = result.get("_source")
        if result.get("found") is not True or not isinstance(source, Mapping):
            raise ValueError("ES readback is incomplete")
        return source

    def upsert(self, row: dict) -> str:
        if set(row) != VECTOR_FIELDS:
            raise ValueError("Milvus row must have exactly eight fields")
        expected = prepare_vector_row(
            row["record_id"], row["scope_id"], row["business_date"],
            row["arrival_seq"], self.space, row["chunk_id"], row["embedding"],
        )
        if row != expected:
            raise VectorIdentityConflict("row differs from its stable identity or space")
        now = self.clock()
        if now.tzinfo is None:
            raise ValueError("service clock must be timezone aware")
        try:
            source = self._es_source(row["record_id"], row["business_date"])
        except Exception:
            raise VectorWriteUnknown("ES authority read is unknown") from None
        if source is None:
            raise VectorWriteUnknown("ES authority record is absent")
        if (source.get("record_id") != row["record_id"] or
                source.get("scope_id") != row["scope_id"] or
                source.get("business_date") != row["business_date"] or
                source.get("arrival_seq") != row["arrival_seq"] or
                source.get("embedding_space_id") != row["embedding_space_id"]):
            raise VectorIdentityConflict("ES authority differs from vector row")
        expiry = source.get("expires_at")
        if not isinstance(expiry, str):
            raise VectorWriteUnknown("ES authority is expired or invalid")
        try:
            # 窗口W2Fβ（WA3b 条66）：replace("Z") 定点化——仅尾 Z 是时区指示
            # 符，替换到 +00:00；串内其他位置的 Z 不再被误改写，畸形串交由
            # fromisoformat 拒绝（fail-closed 不变）。
            parsed = expiry[:-1] + "+00:00" if expiry.endswith("Z") else expiry
            expires_at = datetime.fromisoformat(parsed)
        except ValueError:
            raise VectorWriteUnknown("ES authority is expired or invalid") from None
        if expires_at.tzinfo is None or now >= expires_at:
            raise VectorWriteUnknown("ES authority is expired or invalid")
        try:
            existing = self.milvus.get(
                collection_name=self.collection_name, ids=[row["vector_id"]],
                output_fields=list(VECTOR_FIELDS), consistency_level="Strong",
            )
        except Exception:
            raise VectorWriteUnknown("vector read confirmation is unknown") from None
        if not isinstance(existing, list) or len(existing) > 1:
            raise VectorWriteUnknown("vector read result is invalid")
        if existing:
            if existing[0] != row:
                raise VectorIdentityConflict("same vector_id has a different artifact")
            return "reused"
        try:
            result = self.milvus.upsert(
                collection_name=self.collection_name, data=row,
                partition_name=partition_name(row["business_date"]),
            )
        except Exception:
            raise VectorWriteUnknown("vector upsert confirmation is unknown") from None
        if not isinstance(result, Mapping) or result.get("upsert_count") != 1:
            raise VectorWriteUnknown("vector upsert count is unconfirmed")
        return "created"

    def search(self, request: RecallRequest,
               query_vector: list[float], *,
               timeout_s: float | None = None) -> ChannelResult:
        # W2 ⑩①-5（N43 挂账清偿）：timeout_s 仅在场时透传 Milvus search
        # timeout kw（None=零传参，现役逐字节）。
        started = self.clock()
        if not request.text.strip():
            return ChannelResult("embedding", "not_applicable", (),
                                 VECTOR_QUERY_VERSION, error_code="EMPTY_BODY",
                                 elapsed_ms=elapsed_ms_since(started, self.clock()))
        if request.embedding_space_id != self.space.space_id:
            return ChannelResult("embedding", "unavailable", (),
                                 VECTOR_QUERY_VERSION, error_code="SPACE_UNCONFIRMED",
                                 elapsed_ms=elapsed_ms_since(started, self.clock()))
        if not re.fullmatch(r"[a-z0-9_-]{1,128}", request.scope_id):
            return ChannelResult("embedding", "unavailable", (),
                                 VECTOR_QUERY_VERSION, error_code="SCOPE_INVALID",
                                 elapsed_ms=elapsed_ms_since(started, self.clock()))
        try:
            vector = list(validate_vector(query_vector, self.space))
        except ValueError:
            # 窗口W2Fβ（WA3b 条66，原 L184 错误面对齐）：非法查询向量与
            # 通道其余输入拒收同形——返回 unavailable+error_code，不裸抛。
            return ChannelResult("embedding", "unavailable", (),
                                 VECTOR_QUERY_VERSION,
                                 visible_seq=request.visible_seq,
                                 error_code="EMBEDDING_INVALID",
                                 prepared_seq=request.prepared_seq,
                                 elapsed_ms=elapsed_ms_since(started, self.clock()))
        now = self.clock()
        if now.tzinfo is None:
            raise ValueError("service clock must be timezone aware")
        expression = (
            f'scope_id == "{request.scope_id}" and '
            f'business_date == "{request.business_date}" and '
            f'arrival_seq < {request.arrival_seq} and '
            f'embedding_space_id == "{self.space.space_id}"'
        )
        by_record: dict[str, dict] = {}
        limit = 80
        pages = 0
        status = "complete"
        error_code = None
        while True:
            try:
                response = self.milvus.search(
                    collection_name=self.collection_name,
                    data=[vector], filter=expression, limit=limit,
                    output_fields=["record_id", "scope_id", "business_date",
                                   "arrival_seq", "embedding_space_id", "chunk_id"],
                    partition_names=[partition_name(request.business_date)],
                    anns_field="embedding",
                    search_params={"metric_type": "COSINE", "params": {}},
                    consistency_level="Strong",
                    **({"timeout": timeout_s} if timeout_s is not None else {}),
                )
                if (not isinstance(response, list) or len(response) != 1 or
                        not isinstance(response[0], list) or len(response[0]) > limit):
                    raise ValueError("Milvus response is incomplete")
            except Exception:
                status, error_code = "unavailable", "MILVUS_SEARCH_FAILED"
                break
            pages += 1
            hits = response[0]
            try:
                for hit in hits:
                    if not isinstance(hit, Mapping):
                        raise ValueError("Milvus hit is invalid")
                    entity = hit.get("entity")
                    if not isinstance(entity, Mapping):
                        raise ValueError("Milvus hit entity is missing")
                    record_id = entity.get("record_id")
                    chunk_id = entity.get("chunk_id")
                    score = hit.get("distance")
                    returned_id = hit.get("vector_id", hit.get("id"))
                    if (not isinstance(record_id, str) or
                            type(chunk_id) is not int or
                            returned_id != vector_id(record_id, chunk_id) or
                            ("id" in hit and "vector_id" in hit and
                             hit["id"] != hit["vector_id"]) or
                            type(score) not in (int, float) or not math.isfinite(score)):
                        raise ValueError("Milvus hit identity or score differs")
                    if (entity.get("scope_id") != request.scope_id or
                            entity.get("business_date") != request.business_date or
                            entity.get("embedding_space_id") != self.space.space_id or
                            type(entity.get("arrival_seq")) is not int or
                            not 0 < entity["arrival_seq"] < request.arrival_seq or
                            record_id == request.record_id):
                        raise ValueError("Milvus scalar filter did not hold")
                    current = by_record.setdefault(record_id, {
                        "score": float(score), "chunk_ids": set(),
                    })
                    current["score"] = max(current["score"], float(score))
                    current["chunk_ids"].add(chunk_id)
            except Exception:
                status, error_code = "unavailable", "MILVUS_HIT_INVALID"
                break
            if len(by_record) >= 40 or len(hits) < limit:
                break
            if limit >= self.max_search_limit:
                status, error_code = "truncated", "VECTOR_CHUNK_LIMIT"
                break
            limit = min(limit * 2, self.max_search_limit)

        candidates: list[RecallCandidate] = []
        orphan = False
        for record_id, match in by_record.items():
            try:
                source = self._es_source(record_id, request.business_date)
                if source is None:
                    orphan = True
                    continue
                if not candidate_eligible(source, request, self.clock()):
                    continue
                if (source.get("record_id") != record_id or
                        source.get("embedding_space_id") != self.space.space_id or
                        not isinstance(source.get("text"), str)):
                    orphan = True
                    continue
                candidates.append(RecallCandidate(
                    record_id, source["item_id"], source["arrival_seq"], source["text"],
                    ("embedding",), match["score"], VECTOR_QUERY_VERSION,
                    tuple(sorted(match["chunk_ids"])),
                ))
            except Exception:
                orphan = True
        candidates.sort(key=lambda item: (-item.score, item.arrival_seq, item.record_id))
        # 窗口W2Fβ（WA3b 条66，原 L282-283）：孤儿信号在截断态同样降级——
        # truncated 不吞 ORPHAN_VECTOR（向量命中无 ES 权威=已知不完整）。
        if orphan and status in ("complete", "truncated"):
            status, error_code = "unavailable", "ORPHAN_VECTOR"
        # H-02(b)（E 批 H2-2 求值式，五条件缺一不可——证明基错误置 True=
        # 假完整=fp 向高危，fail-closed 不猜）：complete 态 ∧ 无孤儿 ∧
        # 类型化证明在场 ∧ 代际/空间符 ∧ 前沿覆盖 arrival_seq-1 ∧ 孔洞=0。
        coverage_complete = False
        if status == "complete" and not orphan and self.frontier_provider is not None:
            # F-4 B 修（EWΔ 复审）：provider 调用移入 try（原在 try 外，
            # except 增 ValueError 亦够不着——设计 f4_4 语义钉"provider
            # ValueError→降级不穿透"结构性要求）；判型/构造任何异常=证明
            # 不成立（fail-closed 方向恒安全），五条件求值面不变。
            try:
                proof = self.frontier_provider.vector_frontier(
                    request.scope_id, request.business_date, self.space.space_id)
                coverage_complete = (
                    isinstance(proof, VectorCoverageProof)
                    and proof.schema_version == VectorFrontierProvider.SNAPSHOT_SCHEMA
                    and proof.space_id == self.space.space_id
                    and proof.vector_frontier >= request.arrival_seq - 1
                    and proof.hole_count == 0
                )
            except (TypeError, AttributeError, ValueError):
                coverage_complete = False
        return ChannelResult(
            "embedding", status, tuple(candidates[:40]), VECTOR_QUERY_VERSION,
            pages=pages, visible_seq=request.visible_seq, error_code=error_code,
            coverage_complete=coverage_complete,
            topk_excluded=max(0, len(candidates) - 40),
            # 窗口W2Fβ（WB2-M3 条54）：prepared_seq 直传（融合
            # VECTOR_FRONTIER_UNPROVEN 报告路径依赖；None 直传不捏造）。
            prepared_seq=request.prepared_seq,
            elapsed_ms=elapsed_ms_since(started, self.clock()),
        )
