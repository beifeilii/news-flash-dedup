"""P0-T3 单条墓碑（物化终态域：schema+确定性 ID+持久化读回+迟到补正）。

铁律（施工简报 P0-T3 + 设计稿 §5.3）：
- 墓碑唯一入口=``error_class`` 为 **item_permanent**（schema 层 Literal
  钉死——网络/未知写入/系统级故障在结构上就进不了墓碑）；
- 墓碑 ID 由 ``scope_id + business_date + arrival_seq`` 确定生成（重放
  对拍，不重复创建第二个死亡件）；
- **持久化+读回确认（confirmed）之后水位才许跳过**——确认未知=可重试
  的暂时态，绝不产出跳洞凭据；
- 重放对拍口径：身份字段（scope_id/business_date/arrival_seq/record_id/
  item_id/raw_hash/failed_stage/pipeline_version）不一致=IDENTITY_CORRUPTION
  （IdentityConflict 承载→T1 分类器归 identity_corruption：停止自动推进
  +报警，不重试不墓碑升级）；账务字段（attempt_count/首尾失败时刻/
  error_code/error_summary/retryable/recorded_at）不参拍——首证为准，
  墓碑不可变（不 update）；
- 迟到成功补正（场景 C 确定规则）：墓碑后迟到的主记录**不删除、不复活、
  不二次物化**——CAS 翻 ``task_state="tombstoned"``（prepare/worker 扫描
  词表 accepted/running 均不含该值→自然跳过），墓碑证据不动，他件水位
  不触碰。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from .admission import IdentityConflict, _digest
from .es_admission_schema import day_index


TOMBSTONE_TASK_STATE = "tombstoned"
# 重放对拍的身份字段（分歧=身份损坏）；账务字段（attempt_count/时刻/
# error_code/error_summary/retryable/recorded_at/late_success_at）不参拍。
_IDENTITY_FIELDS = (
    "scope_id", "business_date", "arrival_seq", "record_id", "item_id",
    "raw_hash", "failed_stage", "pipeline_version",
)
# 迟到主记录对拍字段（与受理物化主记录体共用键面）。
_MAIN_RECORD_IDENTITY_FIELDS = (
    "scope_id", "business_date", "arrival_seq", "record_id", "item_id",
    "raw_hash",
)
_CAS_RETRY_LIMIT = 8
# admission._utc 同款 canonical 形（微秒+Z）——admission_schema.py 钉在案。
_UTC_TIMESTAMP_PATTERN = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$"


class TombstonePersistUnknown(RuntimeError):
    """墓碑持久化确认未知（create/读回传输未证）——可重试暂时态，
    **绝不**当作已确认（水位不得跳洞）。"""


class TombstoneStore(Protocol):
    """墓碑持久化协议（BatchStore/MemoryStore/ElasticsearchBatchStore
    结构满足：get/create/replace/is_conflict）。"""

    def get(self, index: str, key: str) -> dict | None: ...
    def create(self, index: str, key: str, body: dict) -> None: ...
    def replace(self, index: str, key: str, body: dict,
                seq_no: int, primary_term: int) -> None: ...
    def is_conflict(self, error: Exception) -> bool: ...


class MaterializationTombstoneV1(BaseModel):
    """单条墓碑（extra=forbid strict；error_class Literal=item_permanent）。

    字段口径=施工简报 T3 十字段 + 身份锚（scope_id/business_date/
    record_id）+ 证据位（error_summary/recorded_at/pipeline_version）
    + 迟到补正位（late_success_at，默认 None）。
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    scope_id: str = Field(min_length=1)
    business_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    arrival_seq: int = Field(ge=1)
    record_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    item_id: str = Field(min_length=1)
    raw_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    failed_stage: str = Field(min_length=1)
    error_class: Literal["item_permanent"] = "item_permanent"
    error_code: str = Field(min_length=1)
    error_summary: str = Field(max_length=300, default="")
    attempt_count: int = Field(ge=1)
    first_failed_at: str = Field(pattern=_UTC_TIMESTAMP_PATTERN)
    last_failed_at: str = Field(pattern=_UTC_TIMESTAMP_PATTERN)
    retryable: bool
    pipeline_version: str = Field(min_length=1)
    recorded_at: str = Field(pattern=_UTC_TIMESTAMP_PATTERN)
    late_success_at: str | None = Field(default=None,
                                        pattern=_UTC_TIMESTAMP_PATTERN)


def tombstone_id(scope_id: str, business_date: str, arrival_seq: int) -> str:
    """确定性墓碑 ID：``sha256(scope_id, business_date, arrival_seq)``。"""
    if not isinstance(scope_id, str) or not scope_id:
        raise ValueError("scope_id must be a non-empty str")
    if not isinstance(business_date, str) or not business_date:
        raise ValueError("business_date must be a non-empty str")
    if type(arrival_seq) is not int or arrival_seq < 1:
        raise ValueError("arrival_seq must be a positive int")
    return _digest([scope_id, business_date, arrival_seq])


def tombstone_index(business_date: str) -> str:
    """墓碑逻辑索引名（``day_index`` kind 形态复用：tombstones）。"""
    return day_index(business_date, "tombstones")


@dataclass(frozen=True)
class TombstonePersistResult:
    """墓碑持久化结果（confirmed=True 才是水位跳洞凭据）。"""

    tombstone_id: str
    index: str
    confirmed: bool
    created: bool              # True=本次新建；False=幂等重放对拍一致
    existing_source: dict | None = None   # 重放读回证据（对拍口径快照）


def _divergences(existing: dict | None, body: dict) -> list[str]:
    """身份字段逐名对拍（账务字段不参拍——首证为准）。"""
    if not isinstance(existing, dict):
        return ["<source>"]
    return [name for name in _IDENTITY_FIELDS
            if existing.get(name) != body.get(name)]


def persist_tombstone(store: TombstoneStore,
                      tombstone: MaterializationTombstoneV1,
                      ) -> TombstonePersistResult:
    """持久化单条墓碑：create → 实时读回确认；冲突走对拍幂等。

    - create 成功 → GET 读回：身份一致 → confirmed/created；读回缺失/
      读失败 → TombstonePersistUnknown（重试；**不得**据 create 即跳洞）；
    - create 冲突 → GET 对拍：身份一致 → confirmed/created=False（幂等，
      首证为准不更新）；身份分歧 → IdentityConflict（IDENTITY_CORRUPTION）；
      读回缺失/读失败 → TombstonePersistUnknown；
    - create 其他异常 → TombstonePersistUnknown（from error 保链）。
    """
    index = tombstone_index(tombstone.business_date)
    key = tombstone_id(tombstone.scope_id, tombstone.business_date,
                       tombstone.arrival_seq)
    body = tombstone.model_dump()
    created = False
    try:
        store.create(index, key, body)
        created = True
    except Exception as error:
        if not store.is_conflict(error):
            raise TombstonePersistUnknown(
                "tombstone create is unknown") from error
        # 冲突：重放形态——实时读回对拍（首证为准）。
        try:
            existing = store.get(index, key)
        except Exception as read_error:
            raise TombstonePersistUnknown(
                "tombstone conflict readback is unknown") from read_error
        diverged = _divergences(
            existing.get("source") if existing else None, body)
        if diverged:
            raise IdentityConflict(
                "tombstone replay diverges on identity fields: "
                + ",".join(diverged))
        return TombstonePersistResult(
            tombstone_id=key, index=index, confirmed=True, created=False,
            existing_source=(existing["source"] if existing else None))
    # create 成功后读回确认（confirmed 才是跳洞凭据）。
    try:
        readback = store.get(index, key)
    except Exception as read_error:
        raise TombstonePersistUnknown(
            "tombstone readback confirmation is unknown") from read_error
    if readback is None:
        raise TombstonePersistUnknown(
            "tombstone readback did not confirm persistence")
    diverged = _divergences(readback.get("source"), body)
    if diverged:
        raise IdentityConflict(
            "tombstone readback diverges on identity fields: "
            + ",".join(diverged))
    return TombstonePersistResult(
        tombstone_id=key, index=index, confirmed=True, created=True)


# ---------- 迟到成功补正（场景 C 确定规则） ----------


def _main_record_divergences(source: dict | None,
                             tombstone: MaterializationTombstoneV1) -> list[str]:
    if not isinstance(source, dict):
        return ["<source>"]
    reference = tombstone.model_dump()
    return [name for name in _MAIN_RECORD_IDENTITY_FIELDS
            if source.get(name) != reference.get(name)]


def reconcile_late_materialization(
        store: TombstoneStore, tombstone: MaterializationTombstoneV1,
        *, main_index: str) -> str:
    """墓碑后迟到主记录的确定性补正（不删除/不复活/不二次物化）。

    规则（场景 C）：
    - 主记录缺失 → ``"absent"``（无迟到写，无需补正）；
    - 主记录身份与墓碑不一致 → IdentityConflict（IDENTITY_CORRUPTION）；
    - 主记录 ``task_state`` 已为 ``tombstoned`` → ``"already_corrected"``；
    - 否则 → CAS 翻 ``task_state="tombstoned"`` → ``"corrected"``。
      CAS 冲突重读有界（8 次）：他方已翻→already_corrected；仍争用→
      AdmissionUnknown 同族（调用方重试，幂等收敛）。

    水位零触碰（不覆盖他件水位）；墓碑证据零改动。
    """
    key = tombstone.record_id
    current = store.get(main_index, key)
    if current is None:
        return "absent"
    source = current["source"]
    diverged = _main_record_divergences(source, tombstone)
    if diverged:
        raise IdentityConflict(
            "late materialization diverges from tombstone identity: "
            + ",".join(diverged))
    if source.get("task_state") == TOMBSTONE_TASK_STATE:
        return "already_corrected"
    updated = {**source, "task_state": TOMBSTONE_TASK_STATE}
    for _ in range(_CAS_RETRY_LIMIT):
        try:
            store.replace(main_index, key, updated,
                          current["seq_no"], current["primary_term"])
        except Exception as error:
            if not store.is_conflict(error):
                raise
            current = store.get(main_index, key)
            if current is None:
                return "absent"
            source = current["source"]
            diverged = _main_record_divergences(source, tombstone)
            if diverged:
                raise IdentityConflict(
                    "late materialization diverges from tombstone identity: "
                    + ",".join(diverged))
            if source.get("task_state") == TOMBSTONE_TASK_STATE:
                return "already_corrected"
            updated = {**source, "task_state": TOMBSTONE_TASK_STATE}
            continue
        return "corrected"
    raise TombstonePersistUnknown(
        "late materialization correction CAS contention exceeded retry bound")


def load_tombstone(store: TombstoneStore, scope_id: str, business_date: str,
                   arrival_seq: int) -> MaterializationTombstoneV1 | None:
    """读回某序号的墓碑（读侧查询面；读失败按 fail-closed 上抛包装）。"""
    index = tombstone_index(business_date)
    key = tombstone_id(scope_id, business_date, arrival_seq)
    try:
        document = store.get(index, key)
    except Exception as error:
        raise TombstonePersistUnknown(
            "tombstone read is unknown") from error
    if document is None:
        return None
    return MaterializationTombstoneV1.model_validate(document["source"])


def tombstone_exists_confirmed(store: TombstoneStore, scope_id: str,
                               business_date: str, arrival_seq: int) -> bool:
    """已持久化墓碑存在性速查（T5 跳洞凭据读面；读未知=False fail-closed）。"""
    try:
        return load_tombstone(store, scope_id, business_date,
                              arrival_seq) is not None
    except TombstonePersistUnknown:
        return False


# ---------- P0-T5 全前沿令：终态证据共读面（⑧同一证据源） ----------


def scan_terminal_tombstone_seqs(search: "Callable[[str, dict], dict]",
                                 scope_id: str, business_date: str, *,
                                 scan_size: int = 10000) -> set[int]:
    """墓簿终态序号枚举（P0-T5 主窗口扩充令：各前沿共读的**同一终态证据源**）。

    - ``search``=只读检索端口（``search(index=..., body=...)`` 形态；逻辑
      索引名由本函数按 ``tombstone_index(business_date)`` 派生，物理前缀
      由调用方包装进端口——各面前缀闸各自现役执法）；
    - 枚举本 ``scope_id``+``business_date`` 的已持久化墓碑 ``arrival_seq``
      （升序单页，``scan_size`` 上界——超出保守不证：前沿停于首个未证
      孔洞，工作量有界，孔洞纪律安全向）；
    - 索引缺席（404 形态）=当日无墓簿 → 空集（无证据≠证据缺失，fail-closed
      不证不猜）；检索端口其他异常**上抛**（前沿停摆可见，不静默吞错）；
      回包/条目形态不识别 → 该条目不计（不证）。

    与 ``_scan_foreign_proofs``（他域/日登记证明）分立：墓证=**本域本日**
    序号的确定性终态（item_permanent 入墓），非外国序号——两证明源可并
    存，均可作跳洞凭据（``advance_prepared_frontier`` 的 ``foreign`` 与
    ``terminal`` 两参分立，账务分计）。
    """
    index = tombstone_index(business_date)
    body = {
        "size": scan_size,
        "track_total_hits": True,
        "query": {"bool": {"filter": [
            {"term": {"scope_id": scope_id}},
        ]}},
        "sort": [{"arrival_seq": "asc"}],
        "_source": ["arrival_seq"],
    }
    try:
        result = search(index=index, body=body)
    except Exception as error:
        status = getattr(error, "status_code", None)
        if type(status) is int and status == 404:
            return set()          # 无墓簿=无终态证据（fail-closed 不证）
        raise
    hits = (result.get("hits", {}) if isinstance(result, dict) else {})
    found = hits.get("hits", []) if isinstance(hits, dict) else None
    if not isinstance(found, list):
        return set()
    seqs: set[int] = set()
    for hit in found:
        source = hit.get("_source") if isinstance(hit, dict) else None
        seq = source.get("arrival_seq") if isinstance(source, dict) else None
        if type(seq) is int and seq >= 1:
            seqs.add(seq)
    return seqs


def scan_terminal_tombstones(search: "Callable[[str, dict], dict]",
                             scope_id: str, business_date: str, *,
                             scan_size: int = 10000,
                             ) -> list[MaterializationTombstoneV1]:
    """墓簿终态条目枚举（P0-T7 指标面：死亡分布/计数的数据源）。

    与 :func:`scan_terminal_tombstone_seqs` 同源同纪律（⑧同一证据源——
    ``search`` 只读检索端口、404=空表 fail-closed、其他异常上抛、条目形态
    不识别不计），返回**已验证**的 ``MaterializationTombstoneV1`` 列表
    （``arrival_seq`` 升序；schema 漂移条目按不识别跳过——指标聚合不因
    单条畸形中断）。两扫描面分立：序号面（前沿跳洞凭据）零 diff 保持；
    本面为指标/对账聚合取全文。``_source`` 不投影（默认全文——分布字段
    全集）。
    """
    index = tombstone_index(business_date)
    body = {
        "size": scan_size,
        "track_total_hits": True,
        "query": {"bool": {"filter": [
            {"term": {"scope_id": scope_id}},
        ]}},
        "sort": [{"arrival_seq": "asc"}],
    }
    try:
        result = search(index=index, body=body)
    except Exception as error:
        status = getattr(error, "status_code", None)
        if type(status) is int and status == 404:
            return []           # 无墓簿=无终态证据（fail-closed 不证）
        raise
    hits = (result.get("hits", {}) if isinstance(result, dict) else {})
    found = hits.get("hits", []) if isinstance(hits, dict) else None
    if not isinstance(found, list):
        return []
    entries: list[MaterializationTombstoneV1] = []
    for hit in found:
        source = hit.get("_source") if isinstance(hit, dict) else None
        if not isinstance(source, dict):
            continue            # 形态不识别→不计（同序号面纪律）
        try:
            entries.append(MaterializationTombstoneV1.model_validate(source))
        except Exception:
            continue            # schema 漂移条目按不识别处理
    return entries


__all__ = [
    "TOMBSTONE_TASK_STATE",
    "MaterializationTombstoneV1",
    "TombstonePersistResult",
    "TombstonePersistUnknown",
    "TombstoneStore",
    "load_tombstone",
    "persist_tombstone",
    "reconcile_late_materialization",
    "scan_terminal_tombstone_seqs",
    "scan_terminal_tombstones",
    "tombstone_exists_confirmed",
    "tombstone_id",
    "tombstone_index",
]
