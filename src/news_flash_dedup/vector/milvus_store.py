"""P19 真 Milvus/ES 双写仓储（UAT 接入层）。

20:3x 主窗口亲笔落位：草案审定自 `log/temp/p19-uat-wiring-proposal.md` §三
（T3 子窗口起草，主窗口审 diff 后采纳；★1/2/3/5 裁定见 目标文档.md 决策日志 D9）。

按 05:28 批复 + 修订 1 + T1/T2 真机教训：
- 隔离集合 `p19_<run_uuid>_<space_id>`（Milvus 集合名仅允许 [A-Za-z0-9_]——连字符
  非法，P12 `news_dedup_replay_...` 先例；★1 主窗口裁定追认）；
- 隔离 ES 索引 `p19-batch-<run_uuid>-news-dedup-(items|control)-v1-YYYY.MM.DD`；
- 八字段 Schema（无 dynamic/正文/secret）；Strong 一致读；
  写确认（upsert_count==1 + Strong get 回读逐字段相等）后才 CAS ready（INV-1）；
- P19 仅 pending→ready/failed 翻转（INV-6 独占权）；非法翻转拒绝零写入；
- 孤儿向量丢弃并记孔洞，不声称完整（RECALL_INCOMPLETE，INV-5）；
- Stable ID 幂等 upsert + 状态回读支撑半写补偿/重启恢复（N12）。

E 批 M-09/F1（log\\temp\\e-batch-design.md §二，G 门③定裁）：写入侧向量
租约代次闸——ES 主记录 `task_lease.vector` 为租约权威（代次单调，释放
不清零，不借 ES 版本冒充代次）；写/翻闸校验不过=拒读拒写零副作用；
写确认日志 `diagnostics.vector.write_confirmation` 与 ready 翻转同源
（INV-6 族扩展）；采用面 dim/metric 必验等（同维异 metric/异构同名
集合一律拒接管）。
"""

from __future__ import annotations

import hashlib
import math
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

from news_flash_dedup.es_client import assert_test_environment
from news_flash_dedup.milvus_client import (
    assert_test_milvus_environment,
    partition_name,
)
from news_flash_dedup.recall.vector_space import EmbeddingSpace
from news_flash_dedup.recall.vector_store import (
    VECTOR_FIELDS,
    VectorIdentityConflict,
    VectorWriteUnknown,
    prepare_vector_row,
    vector_index_params,
    vector_row_fingerprint,
    vector_schema,
)
from news_flash_dedup.vector.coordinator import P19VectorError


P19_UAT_ENV_FLAG = "P19_CONFIRM_UAT"
P19_COLLECTION_PATTERN = re.compile(r"^p19_[a-z0-9]{6,16}_s_[0-9a-f]{40}$")
P19_INDEX_PATTERN = re.compile(
    r"^p19-batch-[a-z0-9]{6,16}-news-dedup-(items|control)-v1-\d{4}\.\d{2}\.\d{2}$"
)


class P19UATGatesNotOpen(RuntimeError):
    """`P19_CONFIRM_UAT=1` 缺失或环境门未满足。"""


class P19CollectionNameInvalid(ValueError):
    """隔离集合名不匹配 `p19_<run>_<space_id>` 硬正则闸门。"""


class P19IndexNameInvalid(ValueError):
    """隔离 ES 索引名不匹配 `p19-batch-<run>-...` 硬正则闸门。"""


class VectorStateTransitionError(ValueError):
    """非法 vector_state 翻转（目标态词表外 / 源态非 pending）——拒绝零写入（INV-6）。"""


class VectorStateCASConflict(RuntimeError):
    """vector_state CAS 版本竞争冲突——写入未发生，状态不动。"""


class P19CollectionDimensionMismatch(ValueError):
    """V1：采用既有集合 embedding dim 与空间声明不符（缺键/型错/异值同拒）。"""


class P19CollectionMetricMismatch(ValueError):
    """V1：采用既有集合 embedding 索引 metric/index_type 与声明不符（缺键同拒）。"""


class VectorLeaseStale(RuntimeError):
    """M-09：调用方租约与 ES 权威 `task_lease.vector` 现行代次不符——拒读拒写。"""


class VectorRowStale(VectorWriteUnknown):
    """M-09：向量行指纹与写确认日志不符（或日志缺席/代次陈旧）——族谱⊂VectorWriteUnknown。"""


@dataclass(frozen=True)
class VectorLease:
    """M-09：调用方持有的向量写入租约（权威=ES 主记录 task_lease.vector）。

    代次语义（10 §6.4 L325）：generation 单调 +1，释放不清零；
    lease_until=aware ISO8601（None 仅出现于释放态权威面，持有态必非 None）。
    """
    owner_id: str
    generation: int
    lease_until: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.owner_id, str) or not self.owner_id:
            raise ValueError("owner_id must be a non-empty str")
        if type(self.generation) is not int or self.generation < 1:
            raise ValueError("generation must be int >= 1")
        if self.lease_until is not None:
            parsed = datetime.fromisoformat(
                str(self.lease_until).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                raise ValueError("lease_until must be timezone aware")


def assert_p19_uat_open(env: Mapping[str, str] | None = None) -> None:
    """P19 闸门：`P19_CONFIRM_UAT=1` 必须设置；ES/Milvus 双环境闸通过。"""
    env = env if env is not None else os.environ
    if env.get(P19_UAT_ENV_FLAG) != "1":
        raise P19UATGatesNotOpen(
            f"P19 requires {P19_UAT_ENV_FLAG}=1; got {env.get(P19_UAT_ENV_FLAG)!r}"
        )
    assert_test_environment(env=dict(env))
    assert_test_milvus_environment(env=dict(env))


def build_p19_collection_name(run_uuid: str, space_id: str) -> str:
    """P19 隔离集合名：p19_<run>_<space_id>（Milvus 命名仅 [A-Za-z0-9_]）。"""
    if not re.fullmatch(r"[a-z0-9]{6,16}", run_uuid):
        raise ValueError("run_uuid must be a generated lowercase token")
    if not re.fullmatch(r"s_[0-9a-f]{40}", space_id):
        raise ValueError("embedding_space_id is invalid")
    return f"p19_{run_uuid}_{space_id}"


def validate_p19_collection_name(collection_name: str) -> None:
    """集合名必须严格匹配硬正则；保留集合与跨代前缀一律拒收。"""
    if not P19_COLLECTION_PATTERN.fullmatch(collection_name):
        raise P19CollectionNameInvalid(
            f"collection {collection_name!r} does not match p19_<run>_s_<40hex>"
        )


def validate_p19_index_name(index_name: str) -> None:
    """ES 索引名必须严格匹配硬正则（p19-batch 专属前缀，不借 P18）。"""
    if not P19_INDEX_PATTERN.fullmatch(index_name):
        raise P19IndexNameInvalid(
            f"index {index_name!r} does not match "
            f"p19-batch-<run>-news-dedup-(items|control)-v1-YYYY.MM.DD"
        )


@dataclass(frozen=True)
class RealMilvusP19Config:
    """P19 真双写接入配置（空间标识=不可变配置摘要，§2.1）。"""
    run_uuid: str
    business_date: date
    space: EmbeddingSpace
    collection_name: str = field(init=False)
    items_index: str = field(init=False)
    control_index: str = field(init=False)

    def __post_init__(self) -> None:
        collection = build_p19_collection_name(self.run_uuid, self.space.space_id)
        validate_p19_collection_name(collection)
        dotted = self.business_date.strftime("%Y.%m.%d")
        items = f"p19-batch-{self.run_uuid}-news-dedup-items-v1-{dotted}"
        control = f"p19-batch-{self.run_uuid}-news-dedup-control-v1-{dotted}"
        validate_p19_index_name(items)
        validate_p19_index_name(control)
        object.__setattr__(self, "collection_name", collection)
        object.__setattr__(self, "items_index", items)
        object.__setattr__(self, "control_index", control)


class RealMilvusP19Store:
    """P19 真 Milvus/ES 双写仓储（UAT 接入）。

    双写顺序执法 = ES 权威主记录存在 + 四元组（record_id/scope_id/business_date/
    arrival_seq）身份对拍（主记录先落 ES）；状态机门禁集中于 cas_vector_state——
    ready 后重放经 Stable ID 身份对拍为 reused 幂等零改写（N12 恢复语义依赖此）。
    """

    # M-09：租约代次闸配置位——类级默认 None=未配置（现役行为逐字节不变，
    # 绿守卫钉 V4c）；live 装配根必须显式注入权威读端口（E 批 R2 对冲：
    # 配置漏接=live 无闸，装配期硬断言）。
    lease_authority: Any = None

    def __init__(self, milvus: Any, es: Any, config: RealMilvusP19Config, *,
                 lease_authority: Any = None) -> None:
        assert_p19_uat_open()
        self.milvus = milvus
        self.es = es
        self.config = config
        if lease_authority is not None:
            self.lease_authority = lease_authority
        # 约束 a 类比：集合显式创建/核验（不凭重读冒充新 owner，§5.2）
        self._ensure_collection()
        # 约束 a：ES 集群 action.auto_create_index=-*，显式幂等建索引（19:16 先例）
        self._ensure_indices()

    # ---------- 生命周期 ----------

    def _ensure_collection(self) -> None:
        name = self.config.collection_name
        adopt_existing = self.milvus.has_collection(collection_name=name)
        if not adopt_existing:
            try:
                self.milvus.create_collection(
                    collection_name=name,
                    schema=vector_schema(self.config.space),
                    index_params=vector_index_params(),
                )
            except Exception as error:
                # 窗口W2Fβ（WA3b 条66 TOCTOU）：并发实例先建——create 抛错后
                # 复检存在即落回 describe 核验路径决定接管/拒收；仍不存在
                # = 创建确认未知（fail-closed，不冒充已建）。
                if not self.milvus.has_collection(collection_name=name):
                    raise VectorWriteUnknown(
                        "collection creation confirmation is unknown"
                    ) from error
                adopt_existing = True
        if adopt_existing:
            description = self.milvus.describe_collection(collection_name=name)
            # 窗口Z2（外部审计三轮确认）：describe 回包畸形（非 Mapping /
            # 缺 fields / fields 非 list / 字段缺 name）原以裸 KeyError/
            # TypeError 逃逸——包 VectorWriteUnknown（读确认未知，fail-closed，
            # 与仓内 vector_store 错误分类口径一致；确认异构仍走下方
            # P19CollectionNameInvalid，两态分立）。
            fields = (
                description.get("fields")
                if isinstance(description, Mapping) else None
            )
            if not isinstance(fields, list):
                raise VectorWriteUnknown(
                    "collection schema description is unknown"
                )
            names = set()
            for f in fields:
                if not isinstance(f, Mapping) or not isinstance(f.get("name"), str):
                    raise VectorWriteUnknown(
                        "collection schema description is unknown"
                    )
                names.add(f["name"])
            if (names != set(VECTOR_FIELDS)
                    or description.get("enable_dynamic_field") is not False):
                raise P19CollectionNameInvalid(
                    f"existing collection {name!r} schema differs; refusing to adopt"
                )  # §5.2：异构同名集合不接管（模型升级新建集合）
            # M-09/F1（E 批 V1-V3）：dim/metric 必验等——同 dim 异 metric
            # （COSINE vs L2 分数口径不可比）与异构同名集合一律拒接管；
            # 键缺席/型错=fail-closed 拒（describe 回包键位 drift 不放行）。
            self._assert_collection_embedding_dim(fields)
            self._assert_collection_embedding_metric(name)
        partition = partition_name(self.config.business_date.isoformat())
        if partition not in self.milvus.list_partitions(collection_name=name):
            try:
                self.milvus.create_partition(collection_name=name,
                                             partition_name=partition)
            except Exception as error:
                # W3F（W3c-F-2，同类不同步收口，对齐 66② collection/indices
                # TOCTOU 复检=任务书对码区"直接尝试+已存在并入语义"向）：
                # 并发实例先建分区——create 抛错后复检在场即并入复用
                # （"集合已存在→复用"先例同向）；仍不在场 = 创建确认未知
                # （fail-closed，VectorWriteUnknown 保链，不冒充已建）。
                # list_partitions 预检/复检读失败裸逃逸与 66②
                # has_collection/indices.exists 预检/复检同形（读失败=
                # 原异常冒出，不包装——对齐不扩大）。
                if partition not in self.milvus.list_partitions(
                        collection_name=name):
                    raise VectorWriteUnknown(
                        "partition creation confirmation is unknown"
                    ) from error

    def _ensure_indices(self) -> None:
        for name in (self.config.items_index, self.config.control_index):
            if not self.es.indices.exists(index=name):
                try:
                    self.es.indices.create(index=name)
                except Exception as error:
                    # 窗口W2Fβ（WA3b 条66 TOCTOU）：并发先建→复检存在即视为
                    # 已建；仍不存在 = 创建确认未知（fail-closed）。
                    if not self.es.indices.exists(index=name):
                        raise VectorWriteUnknown(
                            "index creation confirmation is unknown"
                        ) from error

    # ---------- M-09 采用面 dim/metric 核验（V1-V3） ----------

    def _assert_collection_embedding_dim(self, fields: list) -> None:
        """embedding 字段 params.dim 必验等；缺键/型错/异值→DimensionMismatch。"""
        embedding = None
        for f in fields:
            if f.get("name") == "embedding":
                embedding = f
                break
        params = embedding.get("params") if isinstance(embedding, Mapping) else None
        dim = params.get("dim") if isinstance(params, Mapping) else None
        if type(dim) is not int or dim != self.config.space.dimension:
            raise P19CollectionDimensionMismatch(
                f"collection embedding dim is unconfirmed or differs: "
                f"{dim!r} != {self.config.space.dimension!r}"
            )

    def _assert_collection_embedding_metric(self, name: str) -> None:
        """embedding 字段索引 metric_type==COSINE 且 index_type==FLAT 必验等。"""
        found = False
        for index_name in self.milvus.list_indexes(collection_name=name):
            description = self.milvus.describe_index(
                collection_name=name, index_name=index_name)
            if not isinstance(description, Mapping):
                raise P19CollectionMetricMismatch(
                    "collection index description is unknown")
            if description.get("field_name") != "embedding":
                continue
            found = True
            if (description.get("index_type") != "FLAT"
                    or description.get("metric_type") != "COSINE"):
                raise P19CollectionMetricMismatch(
                    f"collection embedding index differs: "
                    f"index_type={description.get('index_type')!r} "
                    f"metric_type={description.get('metric_type')!r}")
        if not found:
            raise P19CollectionMetricMismatch(
                "collection embedding index is absent")

    # ---------- M-09 租约代次闸（V4-V7） ----------

    @staticmethod
    def _vector_lease_state(source: Mapping) -> Mapping:
        """权威面 task_lease.vector 子字段（缺席/异形→空映射=无租约）。"""
        task_lease = source.get("task_lease")
        vector = task_lease.get("vector") if isinstance(task_lease, Mapping) else None
        return vector if isinstance(vector, Mapping) else {}

    @staticmethod
    def _lease_until_active(lease_until: Any) -> bool:
        """aware ISO 且晚于现在=活跃；naive/畸形/过期=False（fail-closed）。"""
        if not isinstance(lease_until, str) or not lease_until:
            return False
        try:
            parsed = datetime.fromisoformat(lease_until.replace("Z", "+00:00"))
        except ValueError:
            return False
        if parsed.tzinfo is None:
            return False
        return parsed > datetime.now(timezone.utc)

    def _assert_lease_current(self, record_id: str, lease: Any, *,
                              source: Mapping) -> Mapping:
        """持有租约与权威逐字段相等（owner_id+generation）且未过期——否则拒。

        10 §6.4 L329：不借 ES 版本冒充代次——核验显式读 task_lease.vector
        子字段，与 _seq_no/_primary_term（仅 CAS 条件用途）语义分立。
        """
        if not isinstance(lease, VectorLease):
            raise VectorLeaseStale(
                f"vector lease is required for {record_id!r} "
                "(lease_authority configured)")
        vector = self._vector_lease_state(source)
        if (vector.get("owner_id") != lease.owner_id
                or vector.get("generation") != lease.generation):
            raise VectorLeaseStale(
                f"vector lease stale for {record_id!r}: "
                f"authority=(owner={vector.get('owner_id')!r}, "
                f"generation={vector.get('generation')!r}) vs "
                f"held=(owner={lease.owner_id!r}, "
                f"generation={lease.generation!r})")
        if not self._lease_until_active(vector.get("lease_until")):
            raise VectorLeaseStale(
                f"vector lease expired or released for {record_id!r}")
        return vector

    def _journal_write_confirmation(self, record_id: str, *,
                                    rows: list[dict], lease: VectorLease,
                                    authority: Mapping) -> None:
        """upsert 读回确认后落 diagnostics.vector.write_confirmation（H2-6）。

        同租约代次经 ES CAS（if_seq_no/if_primary_term，:433-438 同纪律）
        原子落主记录——日志与权威读数同源，不存在半写窗（E 批 R3 对冲）。
        rows=本次 upsert 全部读回确认行（指纹=prepare_vector_row 出行规范形）。
        """
        source = authority["source"]
        diagnostics = source.get("diagnostics")
        diagnostics = dict(diagnostics) if isinstance(diagnostics, Mapping) else {}
        vector_diag = diagnostics.get("vector")
        vector_diag = dict(vector_diag) if isinstance(vector_diag, Mapping) else {}
        vector_diag["write_confirmation"] = {
            "generation": lease.generation,
            "owner_id": lease.owner_id,
            "confirmed_at": datetime.now(timezone.utc)
                .isoformat(timespec="seconds").replace("+00:00", "Z"),
            "rows": {row["vector_id"]: vector_row_fingerprint(row)
                     for row in rows},
        }
        diagnostics["vector"] = vector_diag
        body = dict(source, diagnostics=diagnostics)
        from elasticsearch import ConflictError

        try:
            self.es.index(
                index=self.config.items_index, id=record_id, document=body,
                if_seq_no=authority["seq_no"],
                if_primary_term=authority["primary_term"],
                refresh="wait_for",
            )
        except ConflictError as exc:
            raise VectorStateCASConflict(
                f"write-confirmation journal CAS conflict for {record_id!r}"
            ) from exc

    def claim_vector_lease(self, record_id: str, *, owner_id: str,
                           ttl_seconds: float) -> VectorLease:
        """租约领取/接管：ES 权威代次单调 +1 CAS 落 task_lease.vector。

        现役活跃租约（他主未过期）→ VectorLeaseStale 拒；CAS 竞争→
        VectorStateCASConflict（写入未发生）。
        """
        current = self.es_authority_source(record_id)
        if current is None:
            raise VectorWriteUnknown("ES authority record is absent")
        source = current["source"]
        vector = self._vector_lease_state(source)
        if vector.get("owner_id") and self._lease_until_active(
                vector.get("lease_until")):
            raise VectorLeaseStale(
                f"vector lease actively held by {vector.get('owner_id')!r}")
        old = vector.get("generation")
        new_generation = (old if type(old) is int else 0) + 1
        lease_until = (datetime.now(timezone.utc)
                       + timedelta(seconds=float(ttl_seconds))
                       ).isoformat(timespec="seconds").replace("+00:00", "Z")
        task_lease = source.get("task_lease")
        task_lease = dict(task_lease) if isinstance(task_lease, Mapping) else {}
        task_lease["vector"] = {
            "owner_id": owner_id,
            "generation": new_generation,
            "lease_until": lease_until,
        }
        body = dict(source, task_lease=task_lease)
        from elasticsearch import ConflictError

        try:
            self.es.index(
                index=self.config.items_index, id=record_id, document=body,
                if_seq_no=current["seq_no"],
                if_primary_term=current["primary_term"],
                refresh="wait_for",
            )
        except ConflictError as exc:
            raise VectorStateCASConflict(
                f"vector lease claim CAS conflict for {record_id!r}"
            ) from exc
        return VectorLease(owner_id=owner_id, generation=new_generation,
                           lease_until=lease_until)

    def release_vector_lease(self, record_id: str, lease: VectorLease) -> None:
        """租约释放：owner/lease_until 置 None，generation 保留（10 §6.4 L325）。"""
        current = self.es_authority_source(record_id)
        if current is None:
            raise VectorWriteUnknown("ES authority record is absent")
        source = current["source"]
        vector = self._vector_lease_state(source)
        if (vector.get("owner_id") != lease.owner_id
                or vector.get("generation") != lease.generation):
            raise VectorLeaseStale(
                f"release requires the current lease for {record_id!r}")
        task_lease = source.get("task_lease")
        task_lease = dict(task_lease) if isinstance(task_lease, Mapping) else {}
        task_lease["vector"] = {
            "owner_id": None,
            "generation": lease.generation,      # 代次不清零（红测 V6 钉）
            "lease_until": None,
        }
        body = dict(source, task_lease=task_lease)
        from elasticsearch import ConflictError

        try:
            self.es.index(
                index=self.config.items_index, id=record_id, document=body,
                if_seq_no=current["seq_no"],
                if_primary_term=current["primary_term"],
                refresh="wait_for",
            )
        except ConflictError as exc:
            raise VectorStateCASConflict(
                f"vector lease release CAS conflict for {record_id!r}"
            ) from exc

    def _assert_row_confirmed(self, row: Mapping) -> None:
        """M-09 读闸（V5）：行必须被现行代次写确认日志覆盖且指纹相等。

        日志缺席（未确认行）/日志代次≠权威现行代次（陈旧行）/指纹不符
        （半写残留）→ VectorRowStale——陈旧行不冒充存在（INV-5 不静默）。
        """
        record_id = row.get("record_id")
        current = self.es_authority_source(record_id) if isinstance(
            record_id, str) else None
        if current is None:
            raise VectorRowStale("vector row authority is absent")
        source = current["source"]
        diagnostics = source.get("diagnostics")
        vector_diag = (diagnostics.get("vector")
                       if isinstance(diagnostics, Mapping) else None)
        journal = (vector_diag.get("write_confirmation")
                   if isinstance(vector_diag, Mapping) else None)
        rows = journal.get("rows") if isinstance(journal, Mapping) else None
        if not isinstance(rows, Mapping):
            raise VectorRowStale("vector write confirmation is absent")
        vector = self._vector_lease_state(source)
        if journal.get("generation") != vector.get("generation"):
            raise VectorRowStale(
                "vector write confirmation generation is stale")
        expected = rows.get(row.get("vector_id"))
        if not isinstance(expected, str) or expected != vector_row_fingerprint(row):
            raise VectorRowStale(
                "vector row differs from write confirmation")

    # ---------- ES 权威回读（双写顺序的对拍锚） ----------

    def es_authority_source(self, record_id: str) -> dict | None:
        """实时 GET items 主记录；None = 不存在。返回 {source, seq_no, primary_term}。"""
        from elasticsearch import NotFoundError

        try:
            response = self.es.get(index=self.config.items_index, id=record_id,
                                   realtime=True)
        except NotFoundError:
            return None
        # 窗口W1Fγ（W1b 确认项，Z2 同口径续修）：ES GET 回包畸形——缺
        # _source/_seq_no/_primary_term（裸 KeyError）、版本号非数值（裸
        # TypeError/ValueError）、_source 非 Mapping（下游裸 AttributeError）
        # ——包 VectorWriteUnknown（读确认未知，fail-closed；确认不存在
        # 仍走上方 None，两态分立）。
        try:
            source = response["_source"]
            seq_no = int(response["_seq_no"])
            primary_term = int(response["_primary_term"])
        except (KeyError, TypeError, ValueError) as error:
            raise VectorWriteUnknown(
                "ES authority response is unknown"
            ) from error
        if not isinstance(source, Mapping):
            raise VectorWriteUnknown("ES authority response is unknown")
        return {
            "source": source,
            "seq_no": seq_no,
            "primary_term": primary_term,
        }

    def get_vector_state(self, record_id: str) -> str | None:
        """实时回读 vector_state（约束 d 类比：翻转以回读为准）。"""
        authority = self.es_authority_source(record_id)
        return None if authority is None else authority["source"].get("vector_state")

    # ---------- 双写原语（INV-1/INV-2 + N12） ----------

    def _read_vector_row(self, vector_id: str) -> dict | None:
        """Strong 一致按主键读向量行；None = 不存在（M-09 写确认读回内径）。"""
        try:
            result = self.milvus.get(
                collection_name=self.config.collection_name, ids=[vector_id],
                output_fields=sorted(VECTOR_FIELDS), consistency_level="Strong",
            )
        except Exception as error:
            # 窗口W2Fβ（WA3b 条66 写路径裸逃逸①，W1Fγ 同类延伸）：
            # get 抛错原裸逃逸——包 VectorWriteUnknown（读确认未知，
            # fail-closed，与 recall/vector_store.py:272-278 同口径——
            # W2 修复波 2 (a)-2 锚勘正）。
            raise VectorWriteUnknown(
                "vector read confirmation is unknown"
            ) from error
        if not isinstance(result, list) or len(result) > 1:
            raise VectorWriteUnknown("vector read result is invalid")
        return result[0] if result else None

    def get_vector_row(self, vector_id: str) -> dict | None:
        """Strong 一致按主键读向量行；None = 不存在。

        M-09（E 批 V5）：lease_authority 已配置时，返回前过写确认读闸——
        行必须被现行代次 write_confirmation 日志覆盖且指纹相等，否则
        VectorRowStale（陈旧/未确认行不冒充存在）。upsert 写确认内径走
        `_read_vector_row`（日志产出中，不入读闸）。
        """
        row = self._read_vector_row(vector_id)
        if row is not None and self.lease_authority is not None:
            self._assert_row_confirmed(row)
        return row

    def upsert_chunks(self, *, record_id: str,
                      chunks: list[tuple[int, list[float]]],
                      scope_id: str, arrival_seq: int,
                      lease: VectorLease | None = None) -> list[tuple[str, str]]:
        """逐 chunk upsert；返回 [(vector_id, "created"|"reused"), ...]（保序）。

        前置（双写顺序 + 身份对拍）：ES 权威主记录必须存在且四元组一致；
        缺失 → VectorWriteUnknown；不一致 → VectorIdentityConflict。
        写确认（约束 c 类比）：upsert_count==1 且 Strong get 回读逐字段相等，
        否则 VectorWriteUnknown——确认前绝不上抛成功（INV-1）。
        幂等（约束 b 类比）：同 vector_id 同内容 → "reused" 零改写；
        同 vector_id 异内容 → VectorIdentityConflict（INV-2 不可覆盖）。
        """
        if not chunks:
            raise P19VectorError("chunks must be non-empty")
        business_date = self.config.business_date.isoformat()
        authority = self.es_authority_source(record_id)
        if authority is None:
            raise VectorWriteUnknown("ES authority record is absent")
        source = authority["source"]
        if (source.get("record_id") != record_id
                or source.get("scope_id") != scope_id
                or source.get("business_date") != business_date
                or source.get("arrival_seq") != arrival_seq):
            raise VectorIdentityConflict(
                "ES authority differs from vector write intent"
            )
        # M-09（E 批 V4）：租约代次闸——拒闸先于任何 Milvus/ES 写副作用
        # （红测 V4 钉 milvus.upsert_calls==0 且 es 零 index 调用）。
        if self.lease_authority is not None:
            self._assert_lease_current(record_id, lease, source=source)
        report: list[tuple[str, str]] = []
        confirmed_rows: list[dict] = []
        for chunk_id, embedding in chunks:
            row = prepare_vector_row(record_id, scope_id, business_date, arrival_seq,
                                     self.config.space, chunk_id, list(embedding))
            existing = self._read_vector_row(row["vector_id"])
            if existing is not None:
                if existing != row:
                    raise VectorIdentityConflict(
                        "same vector_id has a different artifact"
                    )
                confirmed_rows.append(row)
                report.append((row["vector_id"], "reused"))
                continue
            try:
                outcome = self.milvus.upsert(
                    collection_name=self.config.collection_name, data=row,
                    partition_name=partition_name(business_date),
                )
            except Exception as error:
                # 窗口W2Fβ（WA3b 条66 写路径裸逃逸②，W1Fγ 同类延伸）：
                # upsert 抛错原裸逃逸——包 VectorWriteUnknown（写确认未知，
                # fail-closed，与 recall/vector_store.py:285-291 同口径——
                # W2 修复波 2 (a)-2 锚勘正）。
                raise VectorWriteUnknown(
                    "vector upsert confirmation is unknown"
                ) from error
            if not isinstance(outcome, Mapping) or outcome.get("upsert_count") != 1:
                raise VectorWriteUnknown("vector upsert count is unconfirmed")
            if self._read_vector_row(row["vector_id"]) != row:
                raise VectorWriteUnknown("vector readback confirmation failed")
            confirmed_rows.append(row)
            report.append((row["vector_id"], "created"))
        # M-09/H2-6（E 批 V4b）：读回逐字段确认后，同租约代次落写确认日志
        # （CAS 原子落主记录——日志-ready 同源，红测 V7 联动钉）。
        if self.lease_authority is not None:
            self._journal_write_confirmation(
                record_id, rows=confirmed_rows, lease=lease, authority=authority)
        return report

    def search_strong(self, *, query_vector: list[float], scope_id: str,
                      business_date: str,
                      arrival_seq_before: int | None = None,
                      limit: int = 80) -> list[dict]:
        """Strong 一致 ANN 检索（写后立即可见的演示锚）。

        标量过滤：scope_id/business_date/embedding_space_id 等值（+ 可选
        arrival_seq 上界）；分区 bd_YYYYMMDD。返回
        [{record_id, chunk_id, arrival_seq, score, vector_id}, ...]。
        """
        # 窗口W2Fβ（WA3b-M1 条63）：入参正则闸镜像 recall/vector_store.py
        # :173-174（prepare_vector_row scope 正则）与 :310-313（search
        # SCOPE_INVALID）同款——scope_id/business_date 未校验直拼
        # filter = 注入面（W2 修复波 2 (a)-2 锚勘正）；
        # 闸在表达式构造之前（输入缺陷=ValueError，不混入读确认未知的
        # VectorWriteUnknown），非法入参零调用 milvus.search。
        if not re.fullmatch(r"[a-z0-9_-]{1,128}", scope_id):
            raise ValueError("scope_id must be a trusted scalar identifier")
        partition_name(business_date)   # 规范日闸前置（真层同款校验）
        expression = (
            f'scope_id == "{scope_id}" and '
            f'business_date == "{business_date}" and '
            f'embedding_space_id == "{self.config.space.space_id}"'
        )
        if arrival_seq_before is not None:
            expression += f" and arrival_seq < {int(arrival_seq_before)}"
        try:
            response = self.milvus.search(
                collection_name=self.config.collection_name,
                data=[list(query_vector)], filter=expression, limit=limit,
                output_fields=["record_id", "chunk_id", "arrival_seq", "scope_id",
                               "business_date", "embedding_space_id"],
                partition_names=[partition_name(business_date)],
                anns_field="embedding",
                search_params={"metric_type": "COSINE", "params": {}},
                consistency_level="Strong",
            )
            if (not isinstance(response, list) or len(response) != 1
                    or not isinstance(response[0], list)
                    or len(response[0]) > limit):
                raise ValueError("Milvus response is incomplete")
        except Exception as error:
            raise VectorWriteUnknown("Milvus search confirmation is unknown") from error
        hits: list[dict] = []
        for hit in response[0]:
            entity = hit.get("entity")
            if not isinstance(entity, Mapping):
                raise VectorWriteUnknown("Milvus hit entity is missing")
            record_id = entity.get("record_id")
            chunk_id = entity.get("chunk_id")
            if not isinstance(record_id, str) or type(chunk_id) is not int:
                raise VectorWriteUnknown("Milvus hit identity is invalid")
            # 窗口Z2（外部审计三轮确认）：distance 非 (int,float) 或非有限
            # 原以裸 TypeError/ValueError 逃逸（float(None)/float("x")）——
            # 包 VectorWriteUnknown（vector_store.py:376 type+isfinite 同口径——
            # W2 修复波 2 (a)-2 锚勘正）。
            distance = hit.get("distance")
            if type(distance) not in (int, float) or not math.isfinite(distance):
                raise VectorWriteUnknown("Milvus hit distance is invalid")
            hits.append({
                "record_id": record_id,
                "chunk_id": chunk_id,
                "arrival_seq": entity.get("arrival_seq"),
                "score": float(distance),
                "vector_id": hit.get("vector_id", hit.get("id")),
            })
        return hits

    def cas_vector_state(self, record_id: str, *, to: str,
                         lease: VectorLease | None = None) -> None:
        """P19 翻转 vector_state（pending→ready/failed，INV-6 独占权）。

        两道门禁：① 目标态词表 {ready, failed}（ready→pending 结构性不可达）；
        ② 源态必须 pending（failed→ready / ready→failed / ready→ready 皆拒）。
        拒绝零写入；ES 版本竞争 → VectorStateCASConflict，状态不动。

        M-09/H2-6（E 批 V7）：lease_authority 已配置时加两道门禁——
        ③ 持有租约与权威现行代次相符（V4 同闸）；④ pending→ready 翻转
        前置核验 write_confirmation 日志在场且代次==租约代次（日志缺席
        =未确认不冒充 ready，VectorStateTransitionError 零写入）；CAS body
        原样携带日志（日志-状态同源）。
        """
        if to not in ("ready", "failed"):
            raise VectorStateTransitionError(
                f"to must be 'ready' or 'failed'; got {to!r}"
            )
        current = self.es_authority_source(record_id)
        if current is None:
            raise VectorWriteUnknown("ES authority record is absent")
        state = current["source"].get("vector_state")
        if state != "pending":
            raise VectorStateTransitionError(
                f"vector_state flip requires source=pending; got {state!r}"
            )
        if self.lease_authority is not None:
            self._assert_lease_current(record_id, lease,
                                       source=current["source"])
            if to == "ready":
                diagnostics = current["source"].get("diagnostics")
                vector_diag = (diagnostics.get("vector")
                               if isinstance(diagnostics, Mapping) else None)
                journal = (vector_diag.get("write_confirmation")
                           if isinstance(vector_diag, Mapping) else None)
                if (not isinstance(journal, Mapping)
                        or journal.get("generation") != lease.generation):
                    raise VectorStateTransitionError(
                        "ready flip requires write confirmation journal at "
                        "the current lease generation (zero writes)")
        body = dict(current["source"], vector_state=to)
        from elasticsearch import ConflictError

        try:
            self.es.index(
                index=self.config.items_index, id=record_id, document=body,
                if_seq_no=current["seq_no"],
                if_primary_term=current["primary_term"],
                refresh="wait_for",
            )
        except ConflictError as exc:
            raise VectorStateCASConflict(
                f"vector_state CAS conflict for {record_id!r} (state untouched)"
            ) from exc

    # ---------- 孔洞台账（§6/INV-5：丢弃并记孔洞，不声称完整） ----------

    def filter_hits_by_es_authority(self, hits: list[dict], *, scope_id: str,
                                    business_date: str) -> tuple[list[dict], list[dict]]:
        """Milvus 命中逐条回查 ES 权威：缺失/不匹配 → 丢弃 + 记孔洞（幂等）。

        返回 (kept, holes)；孤儿判定两类：ES_MISSING / AUTHORITY_MISMATCH
        （scope_id/business_date/arrival_seq 四元组对拍；embedding_space_id 由
        Milvus 侧 filter + 集合名内嵌 space_id 承担，★2 主窗口裁定不补字段）。
        """
        kept: list[dict] = []
        holes: list[dict] = []
        for hit in hits:
            # 窗口W1Fγ（W1b 确认项）：hit 溯源 Milvus search 回包（search_strong
            # 归一化产物，非本函数构造）——畸形（非 Mapping / record_id 缺失
            # 或非 str）原以裸 KeyError/TypeError 逃逸或静默错记孔洞——包
            # VectorWriteUnknown（与 search_strong hit identity 同口径）。
            record_id = hit.get("record_id") if isinstance(hit, Mapping) else None
            if not isinstance(record_id, str):
                raise VectorWriteUnknown("Milvus hit identity is invalid")
            authority = self.es_authority_source(record_id)
            if authority is None:
                self.record_hole(record_id=record_id, reason="ES_MISSING",
                                 scope_id=scope_id, business_date=business_date)
                holes.append({"record_id": record_id, "reason": "ES_MISSING"})
                continue
            source = authority["source"]
            if (source.get("record_id") != record_id
                    or source.get("scope_id") != scope_id
                    or source.get("business_date") != business_date
                    or source.get("arrival_seq") != hit.get("arrival_seq")):
                self.record_hole(record_id=record_id, reason="AUTHORITY_MISMATCH",
                                 scope_id=scope_id, business_date=business_date)
                holes.append({"record_id": record_id,
                              "reason": "AUTHORITY_MISMATCH"})
                continue
            if self.lease_authority is not None:
                # M-09（E 批 V5 扩展）：租约代次读闸——命中行须被现行代次
                # 写确认日志覆盖（日志缺席/代次陈旧/行未登记 → 剔除+孔洞
                # stale_generation_row；复用本次权威回查读数，不新增往返）。
                diagnostics = source.get("diagnostics")
                vector_diag = (diagnostics.get("vector")
                               if isinstance(diagnostics, Mapping) else None)
                journal = (vector_diag.get("write_confirmation")
                           if isinstance(vector_diag, Mapping) else None)
                journal_rows = (journal.get("rows")
                                if isinstance(journal, Mapping) else None)
                vector_lease = self._vector_lease_state(source)
                hit_vector_id = hit.get("vector_id")
                if (not isinstance(journal_rows, Mapping)
                        or journal.get("generation")
                            != vector_lease.get("generation")
                        or (isinstance(hit_vector_id, str)
                            and hit_vector_id not in journal_rows)):
                    self.record_hole(record_id=record_id,
                                     reason="stale_generation_row",
                                     scope_id=scope_id,
                                     business_date=business_date)
                    holes.append({"record_id": record_id,
                                  "reason": "stale_generation_row"})
                    continue
            kept.append(hit)
        return kept, holes

    def record_hole(self, *, record_id: str, reason: str, scope_id: str,
                    business_date: str) -> str:
        """孔洞落 control 索引（op_type=create 幂等；同 hole_id 重记不重复）。"""
        hole_id = hashlib.sha256(
            f"{record_id}|{reason}|{scope_id}|{business_date}".encode("utf-8")
        ).hexdigest()
        doc = {
            "hole_id": hole_id, "record_id": record_id, "reason": reason,
            "scope_id": scope_id, "business_date": business_date,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        }
        from elasticsearch import ConflictError

        try:
            self.es.create(index=self.config.control_index, id=hole_id,
                           document=doc, refresh="wait_for")
        except ConflictError:
            pass  # 幂等：已记
        return hole_id

    def partition_hole_count(self, business_date: str) -> int:
        """H-02(b) H2-7（E 批）：本日分区孔洞计数。

        作用域=本 store 自身 control 索引台账（collection 前缀孔洞——run
        隔离的控制索引内），按 business_date 过滤计数；复用 holes() 读径
        （台账 200 封顶与本口径一致，不新增查询形态）。
        """
        partition_name(business_date)      # 规范日闸（ISO 日期才入计数）
        return sum(1 for hole in self.holes()
                   if hole.get("business_date") == business_date)

    def holes(self) -> list[dict]:
        """孔洞台账全量回读（UAT 规模 size=200 封顶）。"""
        response = self.es.search(index=self.config.control_index,
                                  query={"match_all": {}}, size=200)
        # 窗口W1Fγ（W1b 确认项）：ES search 回包畸形——response/hits 容器非
        # Mapping（裸 AttributeError）、内层非 list、逐条 hit 非 Mapping 或
        # 缺 _source（裸 TypeError/KeyError）、_source 非 Mapping（静默放行
        # 垃圾进台账）——包 VectorWriteUnknown（台账读确认未知，fail-closed）。
        try:
            container = response.get("hits", {})
        except AttributeError as error:
            raise VectorWriteUnknown("ES holes response is unknown") from error
        inner = (
            container.get("hits", []) if isinstance(container, Mapping) else None
        )
        if not isinstance(inner, list):
            raise VectorWriteUnknown("ES holes response is unknown")
        sources: list[dict] = []
        for hit in inner:
            source = hit.get("_source") if isinstance(hit, Mapping) else None
            if not isinstance(source, Mapping):
                raise VectorWriteUnknown("ES holes response is unknown")
            sources.append(source)
        return sources

    def recall_completeness(self) -> tuple[bool, str | None]:
        """孔洞非空 → (False, "RECALL_INCOMPLETE")——不声称完整、不缩小掩盖。

        口径限定（R9 外部审核 F8d 注记，主窗口 06:3x）：本函数仅覆盖**孔洞
        台账**维度——"无孔洞"≠"召回完整"（ES 已写而 Milvus 未写的记录不在
        此维度）；实现与主窗口审定稿 p19-uat-wiring-proposal.md L463-467
        逐字一致。vector_pending 等其他 RECALL_INCOMPLETE 信号由决策层并列
        判定（log/P19-设计-WIP.md L126），不收敛于本函数。
        """
        if self.holes():
            return False, "RECALL_INCOMPLETE"
        return True, None

    # ---------- UAT 运维（公共标尺 3/4） ----------

    def snapshot_collections(self) -> list[str]:
        return sorted(self.milvus.list_collections())

    def cleanup_plan(self) -> dict:
        """dry-run：仅本 run 前缀（Milvus `p19_<run>_` + ES `p19-batch-<run>-`）。"""
        try:
            collection_listing = self.milvus.list_collections()
        except Exception as error:
            # 窗口W2Fβ（WA3b 条66）：清单读确认未知不裸逃逸（fail-closed）。
            raise VectorWriteUnknown(
                "collection listing confirmation is unknown"
            ) from error
        if (not isinstance(collection_listing, list) or
                any(not isinstance(name, str) for name in collection_listing)):
            raise VectorWriteUnknown("collection listing confirmation is unknown")
        collections = sorted(name for name in collection_listing
                             if name.startswith(f"p19_{self.config.run_uuid}_"))
        try:
            response = self.es.cat.indices(format="json", h="index")
        except Exception as error:
            raise VectorWriteUnknown(
                "ES index listing confirmation is unknown"
            ) from error
        # 窗口W2Fβ（WA3b 条66，原 L497 裸解析）：回包必须 list[Mapping]、
        # index 键为 str（缺键按 "" 容忍=旧兼容面）；异型不再裸
        # AttributeError/TypeError，统一读确认未知（fail-closed）。
        items = getattr(response, "body", response)
        if not isinstance(items, list):
            raise VectorWriteUnknown("ES index listing confirmation is unknown")
        names: list[str] = []
        for item in items:
            if not isinstance(item, Mapping):
                raise VectorWriteUnknown(
                    "ES index listing confirmation is unknown"
                )
            value = item.get("index", "")
            if not isinstance(value, str):
                raise VectorWriteUnknown(
                    "ES index listing confirmation is unknown"
                )
            names.append(value)
        indices = sorted(name for name in names
                         if name.startswith(f"p19-batch-{self.config.run_uuid}-"))
        return {"collections": collections, "indices": indices}

    def cleanup(self) -> dict:
        """执行清场：仅 drop/delete cleanup_plan 列出的本 run 前缀对象。"""
        plan = self.cleanup_plan()
        for name in plan["collections"]:
            if self.milvus.has_collection(collection_name=name):
                self.milvus.drop_collection(collection_name=name)
        for name in plan["indices"]:
            if self.es.indices.exists(index=name):
                self.es.indices.delete(index=name)
        return plan


__all__ = [
    "P19CollectionDimensionMismatch",
    "P19CollectionMetricMismatch",
    "P19CollectionNameInvalid",
    "P19IndexNameInvalid",
    "P19UATGatesNotOpen",
    "P19_COLLECTION_PATTERN",
    "P19_INDEX_PATTERN",
    "P19_UAT_ENV_FLAG",
    "RealMilvusP19Config",
    "RealMilvusP19Store",
    "VectorLease",
    "VectorLeaseStale",
    "VectorRowStale",
    "VectorStateCASConflict",
    "VectorStateTransitionError",
    "assert_p19_uat_open",
    "build_p19_collection_name",
    "validate_p19_collection_name",
    "validate_p19_index_name",
]
