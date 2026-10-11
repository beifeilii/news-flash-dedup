"""P1a-T1 任务文档 schema（队列持久化：独立 ES 任务文档族）。

蓝图 = log\\P1a-预备设计-2026-10-11.md §1.1（逐字段定稿）+ §一铁律四条。
字段分五组（33 字段全 strict，extra=forbid）：

- **身份锚**（17 字段，与 ``PendingAdmissionV1`` 同源字段，冻结不改语义）：
  scope_id/request_id/item_id/record_id/business_fingerprint/business_date/
  arrival_seq/expires_at/received_at/accepted_at/schema_version/
  pipeline_version/embedding_space_id/delivery_route_ref/trace_id + 载荷
  text/raw_hash——**入队后不可变**（重试复用首次身份；按重试日重算身份=
  IDENTITY_CORRUPTION 级错误，蓝图 1.5 跨日恢复钉）；
- **生命周期**：task_state（七态）/materialize_state（物化域子状态）/
  retry_count/next_retry_at/terminal_reason/result（P0-T2 RetryState 的
  持久化迁移宿主——崩溃丢账/接管后重试预算错算的修复面）；
- **租约**（resilience-design §8.2 四件之三）：lease_owner/
  lease_generation/lease_expires_at；
- **失败证据**（有界，正文不入）：first_failed_at/last_failed_at/
  last_failure_class/last_error_code/last_error_summary(≤300)；
- **审计**：enqueued_at/updated_at。

铁律（蓝图 §一，逐条进验收）：
1. **正文只住一处**：入队后任务文档持正本（text），物化成功后主记录持
   正本，任务文档 ``result`` 只存结论不存正文；墓碑与任务终态记录均
   不存正文（T3 纪律沿用）；
2. **首证冻结**：身份锚入队后不可变（重放对拍口径与现役 ``_matches``/
   ``_from_mappings`` 一致——对拍字段集见 ``FROZEN_FIELDS``）；
3. ``task_state`` 终态只有 ready/tombstoned/expired；retry_wait/leased/
   compensating 不是终态，水位不得越过（``TERMINAL_TASK_STATES``）；
4. 入队/翻态全部走确定性 ID create/CAS（幂等重放=对拍，身份分歧=
   IDENTITY_CORRUPTION——T1/T3 分类口径原样执法）。

裁定登记（蓝图 §七遗留项，施工窗定）：
- ``lease_epoch_history`` 有界环形**不设**（蓝图标可选）：代次审计由
  ``lease_generation`` 单调值承载，字段面最小化；
- 状态值取小写串（与现役 items 域 task_state 词表 "accepted"/"running"/
  "tombstoned" 同风格——str 值直接序列化进 ES keyword；T3 先例
  ``TOMBSTONE_TASK_STATE="tombstoned"``）。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# admission._utc / admission_schema.py 同款 canonical 形（微秒+Z）。
_UTC_TIMESTAMP_PATTERN = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$"

# ---------- 任务状态值域（蓝图 1.1；终态三+非终态四） ----------

WORK_TASK_PENDING = "pending"
WORK_TASK_LEASED = "leased"
WORK_TASK_RETRY_WAIT = "retry_wait"
WORK_TASK_COMPENSATING = "compensating"
WORK_TASK_READY = "ready"
WORK_TASK_TOMBSTONED = "tombstoned"
WORK_TASK_EXPIRED = "expired"

#: 终态集合：水位可越过（T5 终态证据推进的合法跳洞凭据面）。
TERMINAL_TASK_STATES = frozenset({
    WORK_TASK_READY, WORK_TASK_TOMBSTONED, WORK_TASK_EXPIRED,
})
#: 非终态（活动）集合：队列深度口径（蓝图 1.4——跨业务日累计）。
ACTIVE_TASK_STATES = frozenset({
    WORK_TASK_PENDING, WORK_TASK_LEASED, WORK_TASK_RETRY_WAIT,
    WORK_TASK_COMPENSATING,
})

# ---------- 物化域子状态（resilience-design §5.1 对齐） ----------

MATERIALIZE_ACCEPTED = "accepted"
MATERIALIZE_MATERIALIZING = "materializing"
MATERIALIZE_RETRY_WAIT = "retry_wait"
MATERIALIZE_READY = "ready"
MATERIALIZE_TOMBSTONE = "tombstone"
MATERIALIZE_EXPIRED = "expired"

# last_failure_class 值域 = P0-T1 FailureClass 五类（str 枚举值）。
# 与 materialize_failure.FailureClass 的同步钉在 test_work_queue.py
# （逐值比对，防漂移）。
_FAILURE_CLASS_LITERAL = Literal[
    "transient_infra", "unknown_write", "item_permanent",
    "identity_corruption", "systemic_outage",
]


class WorkResultV1(BaseModel):
    """终态快照（五字段公共输出合同的镜像）。

    主记录（items 索引 ``result``）仍是输出合同**权威正本**；本件只作
    任务查询/对账面（蓝图 1.1 result 字段口径）。decision/duplicate_ids/
    reason 三键与 item_mapping().result 的子映射同名同语义；record_id/
    arrival_seq 为任务对账锚。P1a-T2 施工勘误：物化收口（``ready``）早于
    决策产出——``result`` 在 ready 时**可空**（``None``），结论镜像由
    commit 域在决策落锤后补（P1a-T3 接线）；对账读取者按 ``None`` 容忍。
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    decision: str = Field(min_length=1)
    duplicate_ids: list[str] = Field(default_factory=list)
    reason: str = Field(default="")
    record_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    arrival_seq: int = Field(ge=1)


class WorkItemV1(BaseModel):
    """独立任务文档（33 字段 strict；extra=forbid）。

    构造即校验字段值域（模式/长度/ge）；状态机不变量（租约三元组/
    retry_wait 到期时刻/终态理由）由 :func:`validate_work_item_invariants`
    承载——CAS 写边界与测试共用同一执法面。
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    # ---- 身份锚（入队后冻结——重试复用首次身份） ----
    scope_id: str = Field(min_length=1)
    request_id: str = Field(min_length=1)
    item_id: str = Field(min_length=1)
    record_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    business_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    business_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    arrival_seq: int = Field(ge=1)
    expires_at: str = Field(pattern=_UTC_TIMESTAMP_PATTERN)
    received_at: str = Field(pattern=_UTC_TIMESTAMP_PATTERN)
    accepted_at: str = Field(pattern=_UTC_TIMESTAMP_PATTERN)
    schema_version: str = Field(min_length=1)
    pipeline_version: str = Field(min_length=1)
    embedding_space_id: str = Field(min_length=1)
    delivery_route_ref: str = Field(min_length=1)
    trace_id: str | None = None

    # ---- 载荷（迁移前唯一存放点=头文档 pending；迁移后=本字段） ----
    text: str
    raw_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    # ---- 生命周期 ----
    task_state: Literal[
        "pending", "leased", "retry_wait", "compensating",
        "ready", "tombstoned", "expired",
    ] = WORK_TASK_PENDING
    materialize_state: Literal[
        "accepted", "materializing", "retry_wait",
        "ready", "tombstone", "expired",
    ] = MATERIALIZE_ACCEPTED
    retry_count: int = Field(default=0, ge=0)
    next_retry_at: str | None = Field(
        default=None, pattern=_UTC_TIMESTAMP_PATTERN)
    #: 终态原因：tombstoned 时=item_permanent 的 error_code（T1 分类）；
    #: expired 时=logical_expiry；ready 正常终态=空串。
    terminal_reason: str = Field(default="")
    result: WorkResultV1 | None = None

    # ---- 租约（resilience-design §8.2；generation 单调不回退） ----
    lease_owner: str | None = None
    lease_generation: int = Field(default=0, ge=0)
    lease_expires_at: str | None = Field(
        default=None, pattern=_UTC_TIMESTAMP_PATTERN)

    # ---- 失败证据（有界，正文不入） ----
    first_failed_at: str | None = Field(
        default=None, pattern=_UTC_TIMESTAMP_PATTERN)
    last_failed_at: str | None = Field(
        default=None, pattern=_UTC_TIMESTAMP_PATTERN)
    last_failure_class: _FAILURE_CLASS_LITERAL | None = None
    last_error_code: str = Field(default="")
    last_error_summary: str = Field(default="", max_length=300)

    # ---- 审计 ----
    enqueued_at: str = Field(pattern=_UTC_TIMESTAMP_PATTERN)
    updated_at: str = Field(pattern=_UTC_TIMESTAMP_PATTERN)


#: 首证冻结字段集（幂等重放对拍面——蓝图铁律 2）。
#: 入队后不可变的身份锚全集：身份 15 字段+载荷 2 字段；生命周期/租约/
#: 失败证据/审计四组不参拍（对拍口径与 T3 墓碑 _IDENTITY_FIELDS 同型：
#: 身份参拍、账务不参拍）。
FROZEN_FIELDS = (
    "scope_id", "request_id", "item_id", "record_id",
    "business_fingerprint", "business_date", "arrival_seq", "expires_at",
    "received_at", "accepted_at", "schema_version", "pipeline_version",
    "embedding_space_id", "delivery_route_ref", "trace_id",
    "text", "raw_hash",
)


def validate_work_item_invariants(item: WorkItemV1) -> None:
    """状态机不变量（schema 表达不了的条件约束——写边界执法面）。

    - ``leased``：租约三元组齐全（owner 非空+expires_at 非空+
      generation ≥ 1——generation 0 = 从未持有租约）；
    - ``retry_wait``：``next_retry_at`` 必在（退避到期时刻）；
    - 终态三态：``tombstoned``/``expired`` 必带 ``terminal_reason``
      （错误码/logical_expiry）且不带 ``result`` 快照；``ready`` 不带
      理由（``result`` 可空——P1a-T2 勘误：物化收口早于决策产出，
      结论镜像由 commit 域后补，见 WorkResultV1 注记）；终态时租约
      owner/expires_at 已清、``next_retry_at`` 已清
      （``lease_generation`` 保留作 fencing 证据，单调不回退）。

    违例 = ValueError（调用方包装为状态冲突 fail-closed，不静默落库）。
    """
    state = item.task_state
    if state == WORK_TASK_LEASED:
        if (not item.lease_owner or item.lease_expires_at is None
                or item.lease_generation < 1):
            raise ValueError(
                "leased work item requires lease owner, expiry and "
                "generation >= 1")
    elif state == WORK_TASK_RETRY_WAIT:
        if item.next_retry_at is None:
            raise ValueError("retry_wait work item requires next_retry_at")
    elif state in TERMINAL_TASK_STATES:
        if item.lease_owner is not None or item.lease_expires_at is not None:
            raise ValueError("terminal work item must not hold a live lease")
        if item.next_retry_at is not None:
            raise ValueError("terminal work item must not carry next_retry_at")
        if state == WORK_TASK_READY:
            # P1a-T2 施工勘误（蓝图 1.1 result 口径复核）：result=五字段公共
            # 输出合同的**镜像面**（决策结论由 decide/commit 域产出——物化
            # 收口时决策未产，result 可空）；"终态输出恰一份"由主记录权威
            # 正本承载，本字段只作任务对账面。ready 禁带 terminal_reason
            # （墓/过期理由只属 tombstoned/expired）不变。
            if item.terminal_reason:
                raise ValueError("ready work item must not carry terminal_reason")
        else:
            if not item.terminal_reason:
                raise ValueError(
                    f"{state} work item requires terminal_reason")
            if item.result is not None:
                raise ValueError(
                    f"{state} work item must not carry result snapshot")


__all__ = [
    "ACTIVE_TASK_STATES",
    "FROZEN_FIELDS",
    "MATERIALIZE_ACCEPTED",
    "MATERIALIZE_EXPIRED",
    "MATERIALIZE_MATERIALIZING",
    "MATERIALIZE_READY",
    "MATERIALIZE_RETRY_WAIT",
    "MATERIALIZE_TOMBSTONE",
    "TERMINAL_TASK_STATES",
    "WORK_TASK_COMPENSATING",
    "WORK_TASK_EXPIRED",
    "WORK_TASK_LEASED",
    "WORK_TASK_PENDING",
    "WORK_TASK_READY",
    "WORK_TASK_RETRY_WAIT",
    "WORK_TASK_TOMBSTONED",
    "WorkItemV1",
    "WorkResultV1",
    "validate_work_item_invariants",
]
