"""隔离 G0 原型：有界多条受理日志、批次 CAS 与异步物化。

P1a-T2（蓝图 log\\P1a-预备设计-2026-10-11.md §1.2/1.4）：受理切换为
任务文档入队（work 索引确定性 ID），头文档瘦身为**纯游标形态**
（last_allocated_seq/last_terminal/last_decision_seq/config_version），
pending 全塞形态退役（迁移器=P1a-T6 域）；物化改任务级租约 claim
循环（work_queue 六原语——批前缀收缩语义退役）。

过渡兼容面（P1a-T2 起）：头文档曾平移写 ``last_materialized_seq``
（=``last_terminal`` 同值别名）与 ``pending`` 空壳。**P1a-T4 起写面摘除**
——新头不落两字段；旧形头（本分支 T2/T3 期产物）经游标写点 CAS
顺带剥离（``_cursor_write``——形态判定只认 ``last_terminal``，剥过渡
字段不构成改形，改形唯一合法通道仍是迁移器 T6）；读面
（``_validate_cursor_head``/prepare 兼容读/recovery_metrics）对两形
均宽容（旧形别名须与游标同值）。
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, replace
from datetime import datetime, time, timedelta, timezone
from threading import RLock
from typing import Callable, Literal, Protocol

from .admission import (
    AdmissionConflict, AdmissionReceipt, AdmissionRequest, AdmissionUnknown,
    BUSINESS_ZONE, CONTROL_INDEX, REQUEST_INDEX, _digest, _receipt, _utc,
    IdentityConflict,
)
from .admission_schema import PendingAdmissionV1
from .materialize_failure import (
    FailureClass, classify_exception, failure_from_outcome)
from .materialize_runtime import (
    MaterializeRetryRuntime, retry_delay_seconds)
from .materialize_terminal import (
    MaterializationTombstoneV1, TombstonePersistUnknown, persist_tombstone,
    reconcile_late_materialization)
from .work_queue import (
    ACTIVE_TASK_STATES, TERMINAL_TASK_STATES, WORK_INDEX_PATTERN,
    WORK_TASK_EXPIRED, WORK_TASK_LEASED, WORK_TASK_TOMBSTONED, WorkItemV1,
    WorkLeaseLost, WorkPersistUnknown, claim_work_item, complete_work_item,
    defer_work_item, enqueue_work_item, terminalize_work_item,
    work_item_id)


HEAD_ID = "batch_admission_head"
LOG_VERSION = "batch-v1"
_log = logging.getLogger(__name__)
# P1a-T4：头文档 config_version 单源（建头写 1；T6 迁移器提升纪元）。
# run_manifest v4 实态字段同源引用（跑批可回溯到受理配置纪元）。
ADMISSION_CONFIG_VERSION = 1
MATERIALIZE_PREFIX_BATCHES = 4
# P1a-T2：claim 循环每轮任务上界（原批前缀 32 件口径平移）。
MATERIALIZE_PREFIX_ITEMS = 32
# W2 批41（F7 常量化，原魔法数 4）：每条目物化四文档=主记录+request/item/seq
# 三映射，与 _documents() 的 documents.extend 结构一一对应；改结构必须同步本常量。
_DOCUMENTS_PER_ENTRY = 4
# P1a-T2：受理复用检索的非终态任务词表（ACTIVE 四态；终态复用走
# request/item 映射读回——materialized 权威面）。
_WORK_LOOKUP_STATES = sorted(ACTIVE_TASK_STATES)
# P1a-T3：终态证据扫描词表（READY/TOMBSTONED/EXPIRED——last_terminal
# 连续前缀推进的合法跳洞凭据面，T5 终态证据同口径）。
_WORK_TERMINAL_STATES = sorted(TERMINAL_TASK_STATES)
# P1a-T2：孤儿探针分号护栏页（enqueue-before-CAS 崩溃窗残档枚举上界；
# 超出即 fail-closed 拒受理——人工裁决，不猜）。
_ORPHAN_PROBE_SIZE = 64
# P1a-T3：claim 循环每轮终态推进证据扫描页（连续前缀页式枚举上界）。
_TERMINAL_SCAN_PAGE = 256


class AdmissionCapacityExceeded(AdmissionConflict):
    """本次整批受理 CAS 前容量不足；不否认其中已有身份的受理事实。

    适配层须先于 AdmissionConflict 捕获并映射429，而非身份冲突409；
    单条 batch_byte_limit 可按既有配置映射413。不得用于写入未知。

    P1a-T4：``durable_queue_capacity`` 档（蓝图 §1.4 硬层 4096）携带
    ``depth``/``capacity`` 观测属性（合同层 Retry-After 建议的取值面）。
    """

    def __init__(self, reason: Literal["log_item_limit", "batch_byte_limit",
                                       "log_byte_limit",
                                       "durable_queue_capacity"],
                 *, depth: int | None = None,
                 capacity: int | None = None) -> None:
        if reason not in ("log_item_limit", "batch_byte_limit",
                          "log_byte_limit", "durable_queue_capacity"):
            raise ValueError("invalid batch capacity reason")
        self.reason = reason
        if reason == "durable_queue_capacity":
            if type(depth) is not int or depth < 0 or type(capacity) is not int \
                    or capacity < 1:
                raise ValueError(
                    "durable_queue_capacity requires depth and capacity")
        else:
            if depth is not None or capacity is not None:
                raise ValueError(
                    "depth and capacity are durable_queue_capacity only")
        self.depth = depth
        self.capacity = capacity
        super().__init__("batch admission capacity exceeded: " + reason)


# P1a-T4 容量三档（蓝图 §1.4：深度口径=全库非终态任务文档数，跨业务日
# 累计；warning 仅指标/critical 升级告警+最老任务年龄联动/hard 拒收）。
DURABLE_QUEUE_CAPACITY = 4096
DURABLE_QUEUE_WARNING_DEPTH = 3277          # ≈80%
DURABLE_QUEUE_CRITICAL_DEPTH = 3891         # ≈95%
_DEPTH_TIERS = ("normal", "warning", "critical", "hard")


def queue_depth_tier(depth: int, *, capacity: int = DURABLE_QUEUE_CAPACITY,
                     warning_depth: int = DURABLE_QUEUE_WARNING_DEPTH,
                     critical_depth: int = DURABLE_QUEUE_CRITICAL_DEPTH
                     ) -> str:
    """深度分档纯函数（hard=拒收档；normal/warning/critical=观测档）。"""
    if type(depth) is not int or depth < 0:
        raise ValueError("depth must be a nonnegative int")
    if not 0 < warning_depth <= critical_depth <= capacity:
        raise ValueError("depth tiers must satisfy 0 < warning <= critical "
                         "<= capacity")
    if depth >= capacity:
        return "hard"
    if depth >= critical_depth:
        return "critical"
    if depth >= warning_depth:
        return "warning"
    return "normal"


class DurableQueueDepth:
    """P1a-T4 进程内近似深度计数器（enqueue/终态增减+周期 ES 对账）。

    蓝图 §1.4 计数实现（shadow-queue 计划 Task 3 原文口径）：不做每请求
    全量扫描；近似值闸门 fail-closed（对账窗内宁可多拒 429，不可超分
    序号）；对账分歧取 ES 真值并记漂移事件。单写者纪律（owner_id CAS）
    下本计数器与真值的分歧源=崩溃漂移/接管残账——对账周期收口。

    域层零全局时钟：``due``/``reconcile`` 由调用方注入时刻；线程安全
    由协调器受理锁承载（单飞域同 RetryRuntime 先例）。
    """

    def __init__(self, *, reconcile_interval_seconds: float = 30.0,
                 capacity: int = DURABLE_QUEUE_CAPACITY,
                 warning_depth: int = DURABLE_QUEUE_WARNING_DEPTH,
                 critical_depth: int = DURABLE_QUEUE_CRITICAL_DEPTH) -> None:
        if not 0 < reconcile_interval_seconds:
            raise ValueError("reconcile interval must be positive")
        queue_depth_tier(0, capacity=capacity, warning_depth=warning_depth,
                         critical_depth=critical_depth)   # 档位参数即校验
        self.reconcile_interval_seconds = float(reconcile_interval_seconds)
        self.capacity = capacity
        self.warning_depth = warning_depth
        self.critical_depth = critical_depth
        self._approximate = 0
        self._last_reconcile_at: datetime | None = None
        self.reconciliations = 0
        self.drift_events = 0
        self.last_drift: int | None = None

    def depth(self) -> int:
        """当前近似深度（闸门取值面）。"""
        return self._approximate

    def tier(self) -> str:
        return queue_depth_tier(
            self._approximate, capacity=self.capacity,
            warning_depth=self.warning_depth,
            critical_depth=self.critical_depth)

    def on_enqueue(self, count: int = 1) -> None:
        if type(count) is not int or count < 0:
            raise ValueError("enqueue count must be a nonnegative int")
        self._approximate += count

    def on_terminal(self, count: int = 1) -> None:
        if type(count) is not int or count < 0:
            raise ValueError("terminal count must be a nonnegative int")
        self._approximate = max(0, self._approximate - count)

    def due(self, now: datetime) -> bool:
        """对账到期判定（首对账=立即；此后按周期）。"""
        if self._last_reconcile_at is None:
            return True
        return (now - self._last_reconcile_at).total_seconds() >= \
            self.reconcile_interval_seconds

    def reconcile(self, es_value: int, *, now: datetime) -> str:
        """ES 真值对账（分歧取 ES 值+漂移计数；一致=零写）。"""
        if type(es_value) is not int or es_value < 0:
            raise ValueError("es_value must be a nonnegative int")
        self._last_reconcile_at = now
        self.reconciliations += 1
        if es_value == self._approximate:
            return "consistent"
        self.drift_events += 1
        self.last_drift = es_value - self._approximate
        self._approximate = es_value       # 分歧取 ES 真值（蓝图 §1.4）
        return "drifted"

    def metrics(self) -> dict:
        """观测面（快照副本；缺省埋点=配线层消费）。"""
        return {
            "depth": self._approximate, "tier": self.tier(),
            "capacity": self.capacity,
            "reconciliations": self.reconciliations,
            "drift_events": self.drift_events,
            "last_drift": self.last_drift,
        }


class BatchStore(Protocol):
    def get(self, index: str, key: str) -> dict | None: ...
    def mget(self, keys: list[tuple[str, str]]) -> list[dict | None]: ...
    def create(self, index: str, key: str, body: dict) -> None: ...
    def replace(self, index: str, key: str, body: dict, seq_no: int, primary_term: int) -> None: ...
    def bulk_create(self, documents: list[tuple[str, str, dict]]) -> None: ...
    def is_conflict(self, error: Exception) -> bool: ...


@dataclass(frozen=True)
class BatchLimits:
    # P0-T6 参数分层（域层默认值，主窗令）：durable log 硬容量 64→512——
    # 与 collector 队列同梯（告警层 400 / 硬层 512，batch_collector.py），
    # 受理突发余量与物化吞吐对齐；装配面显式传参不受默认值影响。
    max_batch_items: int = 8
    max_batch_bytes: int = 512 * 1024
    max_log_items: int = 512
    max_log_bytes: int = 4 * 1024 * 1024

    def __post_init__(self) -> None:
        if min(self.max_batch_items, self.max_batch_bytes,
               self.max_log_items, self.max_log_bytes) <= 0:
            raise ValueError("batch limits must be positive")


def _bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def _batch_digest(entries: list[dict]) -> str:
    # P1a-T2 起仅迁移器（P1a-T6 Phase A 对拍）与旧形态测试消费。
    return hashlib.sha256(_bytes(entries)).hexdigest()


def _keys(scope_id: str, request_id: str, item_id: str) -> tuple[str, str, str]:
    record_id = _digest([scope_id, item_id])
    return record_id, "request:" + _digest([scope_id, request_id]), "item:" + record_id


def pending_registration(source: dict, arrival_seq: int) -> tuple[str, str] | None:
    """登记证明查询面（W-R4 最小加法，09 §3.4「只有登记证明属于其他域/日
    才可跳过；缺号未知须恢复，不能猜不存在」）：头文档 pending 受理日志内
    给定全局序号的 (scope_id, business_date) 登记归属。

    P1a-T2 起头文档 pending=恒空过渡壳 → 本面恒返 None（语义正确：
    未登记）；work 任务文档查询面（``recall/prepare._scan_foreign_proofs``
    ②路顶替）P1a-T3 落地后本面退役。读侧替换前现役消费点零改动。
    """
    if not isinstance(source, dict) or type(arrival_seq) is not int:
        return None
    pending = source.get("pending")
    if not isinstance(pending, dict) or pending.get("version") != LOG_VERSION:
        return None
    batches = pending.get("batches")
    if not isinstance(batches, list):
        return None
    for batch in batches:
        entries = batch.get("entries") if isinstance(batch, dict) else None
        if not isinstance(entries, list):
            return None
        for entry in entries:
            if not isinstance(entry, dict):
                return None
            if entry.get("arrival_seq") != arrival_seq:
                continue
            scope_id = entry.get("scope_id")
            business_date = entry.get("business_date")
            if (isinstance(scope_id, str) and scope_id and
                    isinstance(business_date, str) and business_date):
                return scope_id, business_date
            return None
    return None


_PENDING_FIELDS = (
    "scope_id", "request_id", "item_id", "record_id", "business_fingerprint",
    "text", "delivery_route_ref", "trace_id", "received_at", "accepted_at",
    "business_date", "expires_at", "schema_version", "pipeline_version",
    "embedding_space_id", "arrival_seq",
)


def _pending_view(item: WorkItemV1) -> dict:
    """任务文档 → PendingAdmissionV1 兼容视图（回执构造面）。

    ``_receipt`` 经 ``PendingAdmissionV1.model_validate``（extra=forbid），
    任务文档的 33 字段全量 dump 会撞禁携带——本视图按 16 字段身份锚
    全集投影（值域同源，零改写）。
    """
    dumped = item.model_dump(mode="json")
    return {key: dumped[key] for key in _PENDING_FIELDS}


def _parse_utc(timestamp: str) -> datetime:
    """任务文档 canonical 时刻解析（work 检索/租约同款 Z 形）。"""
    return datetime.fromisoformat(timestamp.replace("Z", "+00:00"))


def mirror_decision_seq(store, scope_id: str, business_date: str,
                        arrival_seq: int, *, clock: Callable[[], datetime],
                        _rounds: int = 8) -> str:
    """P1a-T3：commit 腿决策游标镜像写入（蓝图 1.2.1/七-3 裁定）。

    头文档 ``last_decision_seq`` 由 commit 腿在其自身 CAS 之后幂等推进
    （单调、滞后容忍、重放幂等），**只作任务对账/游标可见面，不新增
    任何读判路径**；commit 域控制文档（decision_watermark_seq 权威面）
    保持现役不动。

    返回码表（全路径不抛——可见面永不阻塞 commit 腿）：
    - ``"advanced"``：本次镜像推进到位；
    - ``"current"``：重放/回退（幂等单调 no-op）；
    - ``"legacy_form"``：legacy 头（无 ``last_terminal``）——双写窗不
      认领，T6 迁移后自然消失；
    - ``"absent"``：头文档缺席（受理写者负责建头；镜像不代建）；
    - ``"lagged"``：CAS 竞争超界/写未知——滞后容忍（下次 commit 腿
      重放自然补齐）。

    ``business_date``/``scope_id`` 仅为镜像面签名完备性保留（头文档是
    全域单写者文档，非分区键）；镜像不校验值域归属（单调闸自足）。
    """
    if type(arrival_seq) is not int or arrival_seq < 0:
        return "current"           # 畸形输入按 no-op 可见面处理
    for _ in range(_rounds):
        try:
            head = store.get(CONTROL_INDEX, HEAD_ID)
        except Exception:
            return "lagged"
        if head is None:
            return "absent"
        source = head["source"]
        if "last_terminal" not in source:
            return "legacy_form"
        current = source.get("last_decision_seq", 0)
        if type(current) is not int or arrival_seq <= current:
            return "current"
        updated = {**_cursor_write(source), "last_decision_seq": arrival_seq,
                   "updated_at": _utc(clock())}
        try:
            store.replace(CONTROL_INDEX, HEAD_ID, updated,
                          head["seq_no"], head["primary_term"])
        except Exception as error:
            if store.is_conflict(error):
                continue
            return "lagged"
        return "advanced"
    return "lagged"


def _cursor_write(source: dict) -> dict:
    """游标形头文档 CAS 写基（P1a-T4）：浅拷贝并剥过渡字段。

    剥 ``last_materialized_seq``/``pending``（T2/T3 期过渡兼容面）——
    形态判定只认 ``last_terminal`` 在位，剥过渡字段不构成改形（改形
    唯一合法通道=迁移器 T6）；旧形头经任一游标写点 CAS 后即收敛为新
    形。``_validate_cursor_head`` 读面对两形均宽容。
    """
    updated = dict(source)
    updated.pop("last_materialized_seq", None)
    updated.pop("pending", None)
    return updated


def _new_work_item(request: AdmissionRequest, record_id: str, arrival_seq: int,
                   accepted_at: datetime, business_date: str,
                   expires_at: datetime) -> WorkItemV1:
    """新受理任务文档（P1a-T2：PendingAdmissionV1 身份锚全集+生命周期缺省）。

    身份字段=现役 pending 条目逐字段同源（首证冻结口径不变）；新增载荷
    ``raw_hash``（sha256(text)）与生命周期缺省（``pending``/``accepted``/
    零账零约零证据+enqueued_at=updated_at=accepted_at）。构造即经
    ``WorkItemV1`` strict 校验（值域漂移=构造期即拒）。
    """
    return WorkItemV1(
        scope_id=request.scope_id,
        request_id=request.request_id,
        item_id=request.item_id,
        record_id=record_id,
        business_fingerprint=_digest({
            "item_id": request.item_id, "text": request.text}),
        business_date=business_date,
        arrival_seq=arrival_seq,
        expires_at=_utc(expires_at),
        received_at=_utc(request.received_at),
        accepted_at=_utc(accepted_at),
        schema_version=request.schema_version,
        pipeline_version=request.pipeline_version,
        embedding_space_id=request.embedding_space_id,
        delivery_route_ref=request.delivery_route_ref,
        trace_id=request.trace_id,
        text=request.text,
        raw_hash=hashlib.sha256(request.text.encode("utf-8")).hexdigest(),
        enqueued_at=_utc(accepted_at),
        updated_at=_utc(accepted_at),
    )


class BatchAdmissionCoordinator:
    def __init__(
        self, store: BatchStore, *, owner_id: str,
        owner_isolated: Callable[[], bool], limits: BatchLimits,
        clock: Callable[[], datetime],
        retry_runtime: MaterializeRetryRuntime | None = None,
        use_durable_queue: bool = False,
        durable_queue: DurableQueueDepth | None = None,
    ) -> None:
        self.store = store
        self.owner_id = owner_id
        self.owner_isolated = owner_isolated
        self.limits = limits
        self.clock = clock
        # P0-T2：可选熔断/重试运行时（None=现役行为逐字节零 diff）。
        # 闸只挂物化写路径（materialize_*）；accept_batch 不经闸——熔断
        # 期间新件照常持久化 ACCEPTED（受理头文档 CAS 不受熔断约束）。
        self.retry_runtime = retry_runtime
        # P1a-T2 双写模式（蓝图 §1.4；追加 keyword-only，缺省 False）：
        # False=受理走现役头文档 pending 全塞路径（逐字节零 diff——
        # T8 前沿链与既有 ~80 件行为钉全部原样绿）；True=受理切换任务
        # 文档入队（work 索引）+头文档纯游标形态。物化 claim 循环=P1a-T3；
        # 全量翻转（摘 legacy 分支）=P1a-T6。两形态头文档互不认领
        # （accept 入口 form 卫兵 fail-closed；迁移器=T6 唯一合法改形者）。
        self.use_durable_queue = use_durable_queue
        # P1a-T4 容量计数器（蓝图 §1.4）：durable 侧近似深度+周期 ES 对账
        # +三档观测。显式注入=小容量测试/装配层调档；缺省懒建 4096 档；
        # legacy 侧恒 None（False 路径容量口径=max_log_items 不变）。
        # 线程面：写点（reconcile/on_enqueue/on_terminal）恒持受理锁；
        # collector 预拒收面跨线程只读 depth()（int 读原子，观测值性质
        # ——权威闸门在锁内 _durable_depth）。
        self.durable_queue = durable_queue if durable_queue is not None else (
            DurableQueueDepth() if use_durable_queue else None)
        self._depth_oldest_age: float | None = None
        self._lock = RLock()
        self._unknown_cas = False

    def _retry_guard_attempt(self) -> None:
        """T2 熔断闸：开路即拒启动物化尝试（零 ES 触达、零水位、零墓碑）。"""
        if self.retry_runtime is not None:
            self.retry_runtime.guard_attempt()

    def _note_retry_failure(self, error: BaseException) -> None:
        """物化写路径失败入熔断窗（仅参与类：瞬态/系统级；未知写入除外）。"""
        if self.retry_runtime is None:
            return
        failure = classify_exception(error)
        if failure.participates_in_breaker:
            self.retry_runtime.record_request_failure()

    def _note_retry_success(self) -> None:
        """物化写路径成功入熔断窗（真发生 bulk 往返的调用点才记）。"""
        if self.retry_runtime is not None:
            self.retry_runtime.record_request_success()

    # ---------- P0-T4：bulk 逐条结果解析 + 未知读回 + 批内单条隔离 ----------

    def _classified_bulk(self):
        """store 提供逐条解析面（``bulk_create_classified``）时走分类收口。"""
        method = getattr(self.store, "bulk_create_classified", None)
        return method if callable(method) else None

    def _note_entry_item_failure(self, entry: dict, failure) -> None:
        """P0-T4 单条失败入账：熔断窗样本（参与类）+条目重试账（未知零计数）。"""
        if self.retry_runtime is None:
            return
        self.retry_runtime.record_entry_failure(entry["arrival_seq"], failure)
        if failure.participates_in_breaker:
            self.retry_runtime.record_request_failure()

    def _note_entry_item_success(self, entry: dict) -> None:
        """P0-T4 单条收口成功出账（清条目重试账；无账=no-op）。"""
        if self.retry_runtime is not None:
            self.retry_runtime.record_entry_success(entry["arrival_seq"])

    @staticmethod
    def _epoch_utc(epoch: float) -> str:
        """runtime 侧 epoch 秒 → admission._utc 同款 canonical 微秒形。"""
        return datetime.fromtimestamp(epoch, timezone.utc).isoformat(
            timespec="microseconds").replace("+00:00", "Z")

    def _entry_tombstone(self, entry: dict, failure) -> MaterializationTombstoneV1:
        """确认 item_permanent 的条目 → 单条墓碑（T3 schema；正文不进墓）。"""
        attempts = 1
        first_failed = _utc(self.clock())
        if self.retry_runtime is not None:
            state = self.retry_runtime.entry_state(entry["arrival_seq"])
            if state is not None:
                attempts = max(1, state.active_attempts + 1)
                if state.first_failed_at is not None:
                    first_failed = self._epoch_utc(state.first_failed_at)
        return MaterializationTombstoneV1(
            scope_id=entry["scope_id"], business_date=entry["business_date"],
            arrival_seq=entry["arrival_seq"], record_id=entry["record_id"],
            item_id=entry["item_id"],
            raw_hash=hashlib.sha256(entry["text"].encode("utf-8")).hexdigest(),
            failed_stage="materialize_bulk", error_class="item_permanent",
            error_code=failure.error_code, error_summary=failure.detail,
            attempt_count=attempts, first_failed_at=first_failed,
            last_failed_at=_utc(self.clock()), retryable=False,
            pipeline_version=entry["pipeline_version"],
            recorded_at=_utc(self.clock()),
        )

    def _terminalize_entry(self, entry: dict, failure) -> None:
        """P0-T4 单条隔离：条目入墓（持久化+读回确认，T3）；批内其余照常。

        同批已落的主记录（混合形态：主记录 201+映射 4xx）按场景 C 确定规则
        补正——CAS 翻 ``task_state="tombstoned"``（prepare/worker 扫描词表
        均不含该值→自然跳过），不删除不复活，墓碑证据与批内他件零触碰。
        """
        tombstone = self._entry_tombstone(entry, failure)
        try:
            persist_tombstone(self.store, tombstone)
        except TombstonePersistUnknown as error:
            self._note_retry_failure(error)
            raise AdmissionUnknown(
                "tombstone persistence is unconfirmed") from error
        main_index = "news-dedup-items-v1-" + entry["business_date"].replace("-", ".")
        try:
            reconcile_late_materialization(
                self.store, tombstone, main_index=main_index)
        except TombstonePersistUnknown as error:
            self._note_retry_failure(error)
            raise AdmissionUnknown(
                "late materialization correction is unconfirmed") from error
        # IdentityConflict 直接上抛（T1 分类 IDENTITY_CORRUPTION：
        # 停止自动推进+报警，不重试不隔离）。

    def _compare_materialized(self, expected: dict, actual_source: dict,
                              culprit: dict) -> None:
        """读回对拍（既有 deterministic 内容对拍口径；分歧=整批隔离）。"""
        if any(actual_source.get(key) != value for key, value in expected.items()
               if key not in {"task_state", "delivery_state", "result", "diagnostics"}):
            self._quarantine(culprit, "deterministic_content_conflict")
        if "diagnostics" in expected:
            expected_delivery = expected["diagnostics"]["delivery"]
            diagnostics = actual_source.get("diagnostics")
            delivery = diagnostics.get("delivery") if isinstance(diagnostics, dict) else None
            if not isinstance(delivery, dict) or any(
                    delivery.get(key) != value for key, value in expected_delivery.items()
            ):
                self._quarantine(culprit, "deterministic_content_conflict")

    def _resolve_classified_bulk(self, method, documents: list,
                                 entries: list, entry_batches: list,
                                 *, batches: list, unlocated_reason: str) -> list:
        """P0-T4 逐条结果收口：未知(409)读回对拍、瞬态整体重试、单条永久入墓。

        返回墓碑化条目序号列表（观测/对账面）。铁律：
        - 只有 item_permanent 入墓（网络/未知/系统级结构性进不了墓）；
        - 1 坏件不杀整批：其余条目照常收口，水位跨 READY+TOMBSTONE 推进；
        - 任一条目未收口（瞬态/读回未命中/墓簿未确认）→ AdmissionUnknown
          （幂等重试：已落文档走 409→读回对拍自愈）；
        - 读回身份分歧 → 既有整批隔离（deterministic_content_conflict）；
        - 请求级 PermanentBulkError（不可定位）→ 既有 unlocated 隔离纪律。
        """
        from .batch_es_store import PermanentBulkError

        for batch in batches:
            self._quarantine_if_expired(batch)
        try:
            outcomes = method(documents)
        except Exception as error:
            self._note_retry_failure(error)
            for batch in batches:
                self._quarantine_if_expired(batch)
            if isinstance(error, PermanentBulkError):
                self._quarantine(batches[0], unlocated_reason)
            raise AdmissionUnknown(
                "bulk materialization confirmation is unknown") from error
        for batch in batches:
            self._quarantine_if_expired(batch)
        # 未知写入（409 形态）：实时 mget 读回身份对拍收口。
        unknown_positions = [outcome.position for outcome in outcomes
                             if outcome.failure_class is
                             FailureClass.UNKNOWN_WRITE]
        resolved_positions: set = set()
        if unknown_positions:
            keys = [(documents[position][0], documents[position][1])
                    for position in unknown_positions]
            try:
                found = self._mget(keys)
            except Exception as error:
                self._note_retry_failure(error)
                for batch in batches:
                    self._quarantine_if_expired(batch)
                raise
            finally:
                for batch in batches:
                    self._quarantine_if_expired(batch)
            for position, actual in zip(unknown_positions, found):
                culprit = entry_batches[position // _DOCUMENTS_PER_ENTRY]
                if actual is None or not isinstance(actual, dict) or \
                        not isinstance(actual.get("source"), dict):
                    continue            # 未命中=未确认（重试收敛，不定永久）
                self._compare_materialized(
                    documents[position][2], actual["source"], culprit)
                resolved_positions.add(position)
        # 逐条目收口：永久入墓；瞬态/未收口=可重试；其余 READY。
        tombstoned: list = []
        for entry_index, entry in enumerate(entries):
            outcome_slice = outcomes[
                entry_index * _DOCUMENTS_PER_ENTRY:
                (entry_index + 1) * _DOCUMENTS_PER_ENTRY]
            culprit = entry_batches[entry_index]
            permanent = [outcome for outcome in outcome_slice
                         if outcome.failure_class is FailureClass.ITEM_PERMANENT]
            if permanent:
                self._quarantine_if_expired(culprit)
                self._terminalize_entry(entry, failure_from_outcome(permanent[0]))
                self._note_entry_item_success(entry)   # 终态出账（清重试账）
                tombstoned.append(entry["arrival_seq"])
                continue
            transient = [outcome for outcome in outcome_slice
                         if outcome.failure_class is FailureClass.TRANSIENT_INFRA]
            unresolved = [outcome for outcome in outcome_slice
                          if outcome.failure_class is
                          FailureClass.UNKNOWN_WRITE and
                          outcome.position not in resolved_positions]
            if transient or unresolved:
                failure = failure_from_outcome((transient or unresolved)[0])
                self._note_entry_item_failure(entry, failure)
                raise AdmissionUnknown(
                    "materialized item confirmation is unknown")
            self._note_entry_item_success(entry)
        return tombstoned

    def _active(self) -> None:
        if not self.owner_isolated():
            raise AdmissionConflict("old writer isolation not confirmed")
        if self._unknown_cas:
            raise AdmissionConflict("unknown batch CAS blocks further sequence allocation")

    def _read(self, index: str, key: str) -> dict | None:
        try:
            return self.store.get(index, key)
        except Exception as error:
            raise AdmissionUnknown("realtime document read is unconfirmed") from error

    def _mget(self, keys: list[tuple[str, str]]) -> list[dict | None]:
        try:
            found = self.store.mget(keys)
        except Exception as error:
            raise AdmissionUnknown("realtime mget is unconfirmed") from error
        if not isinstance(found, list) or len(found) != len(keys):
            raise AdmissionUnknown("realtime mget is incomplete")
        return found

    @staticmethod
    def _timestamp(value: str) -> datetime:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None:
            raise ValueError("timestamp must be timezone aware")
        return result

    @classmethod
    def _validate_log(cls, source: dict) -> None:
        # P1a-T2 双形态执法（迁移窗）：头文档二选一——
        # - 游标形态（P1a 蓝图 §1.2）：``last_terminal`` 在案（形态标记），
        #   pending=空壳/缺席；**混合形态（非空 pending+游标标记）即拒**
        #   ——改形唯一合法通道=迁移器（P1a-T6）；
        # - legacy 全塞形态（现役）：批信封/digest/序号边界全量执法
        #   （P0 语义逐字节保留——双写窗 False 侧的行为钉）。
        # enabled=false 文档必须恢复时重新校验，不能只信写入时的 Schema。
        try:
            if "last_terminal" in source:
                cls._validate_cursor_head(source)
                return
            cls._validate_legacy_head(source)
        except (KeyError, TypeError, ValueError, AttributeError, OverflowError) as error:
            raise AdmissionConflict("batch log validation failed") from error

    @classmethod
    def _validate_cursor_head(cls, source: dict) -> None:
        """游标形态校验（P1a-T2；蓝图 §1.2 头文档字段合同）。"""
        allocated = source["last_allocated_seq"]
        terminal = source["last_terminal"]
        if (source["kind"] != "admission" or
                not isinstance(source["owner_id"], str) or not source["owner_id"] or
                type(allocated) is not int or type(terminal) is not int or
                not 0 <= terminal <= allocated <= (1 << 53) - 1 or
                not isinstance(source.get("checkpoint", {}), dict)):
            raise ValueError("invalid head")
        decision = source["last_decision_seq"]
        if type(decision) is not int or not 0 <= decision <= terminal:
            raise ValueError("invalid decision cursor")
        config = source["config_version"]
        if type(config) is not int or config < 1:
            raise ValueError("invalid config epoch")
        pending = source.get("pending")
        if pending is not None:
            # 过渡空壳（G0/旧读面形态兼容）：批内容一律拒收——
            # 旧形态非空头文档必须先走迁移器（P1a-T6），不猜不吞。
            if (set(pending) != {"version", "batches"} or
                    pending["version"] != LOG_VERSION or
                    pending["batches"] != []):
                raise ValueError("legacy pending log must be migrated")
        alias = source.get("last_materialized_seq")
        if alias is not None and (type(alias) is not int or alias != terminal):
            # 过渡别名与游标漂移=写入面 bug（两值必须同 CAS 平移）。
            raise ValueError("legacy materialized alias diverges")

    @classmethod
    def _validate_legacy_head(cls, source: dict) -> None:
        """legacy 全塞形态校验（P0 语义逐字节保留；双写窗 False 侧）。"""
        allocated = source["last_allocated_seq"]
        materialized = source["last_materialized_seq"]
        if (source["kind"] != "admission" or
                not isinstance(source["owner_id"], str) or not source["owner_id"] or
                type(allocated) is not int or type(materialized) is not int or
                not 0 <= materialized <= allocated <= (1 << 53) - 1 or
                not isinstance(source.get("checkpoint", {}), dict)):
            raise ValueError("invalid head")
        pending = source["pending"]
        if (set(pending) != {"version", "batches"} or pending["version"] != LOG_VERSION or
                not isinstance(pending["batches"], list)):
            raise ValueError("invalid pending")
        sequence = materialized
        batch_ids: set[str] = set()
        requests: set[tuple[str, str]] = set()
        items: set[tuple[str, str]] = set()
        for batch in pending["batches"]:
            if set(batch) != {"batch_id", "digest", "entries"}:
                raise ValueError("invalid batch envelope")
            batch_id = batch["batch_id"]
            if (not isinstance(batch_id, str) or len(batch_id) != 64 or
                    any(char not in "0123456789abcdef" for char in batch_id) or
                    batch_id in batch_ids):
                raise ValueError("invalid batch identity")
            batch_ids.add(batch_id)
            entries = batch["entries"]
            if not isinstance(entries, list) or not entries or _batch_digest(entries) != batch["digest"]:
                raise ValueError("invalid batch digest")
            for entry in entries:
                PendingAdmissionV1.model_validate(entry)
                sequence += 1
                request_key = (entry["scope_id"], entry["request_id"])
                item_key = (entry["scope_id"], entry["item_id"])
                accepted = cls._timestamp(entry["accepted_at"])
                cls._timestamp(entry["received_at"])
                day = accepted.astimezone(BUSINESS_ZONE).date()
                expiry = datetime.combine(day + timedelta(days=7), time.min, BUSINESS_ZONE)
                if (entry["arrival_seq"] != sequence or request_key in requests or item_key in items or
                        entry["record_id"] != _digest([entry["scope_id"], entry["item_id"]]) or
                        entry["business_fingerprint"] != _digest({
                            "item_id": entry["item_id"], "text": entry["text"]}) or
                        entry["business_date"] != day.isoformat() or
                        cls._timestamp(entry["expires_at"]) != expiry):
                    raise ValueError("invalid pending identity or sequence")
                requests.add(request_key)
                items.add(item_key)
        if sequence != allocated:
            raise ValueError("invalid sequence boundary")

    @staticmethod
    def _unquarantined(head: dict) -> None:
        if head["source"].get("checkpoint", {}).get("batch_quarantine"):
            raise AdmissionConflict("batch log is quarantined")

    def _head(self) -> dict:
        head = self._read(CONTROL_INDEX, HEAD_ID)
        if head is None:
            if self.use_durable_queue:
                body = {
                    "kind": "admission", "owner_id": self.owner_id,
                    "last_allocated_seq": 0, "last_terminal": 0,
                    "last_decision_seq": 0,
                    "config_version": ADMISSION_CONFIG_VERSION,
                    "checkpoint": {}, "updated_at": _utc(self.clock()),
                }
            else:
                body = {
                    "kind": "admission", "owner_id": self.owner_id,
                    "last_allocated_seq": 0, "last_materialized_seq": 0,
                    "pending": {"version": LOG_VERSION, "batches": []},
                    "checkpoint": {}, "updated_at": _utc(self.clock()),
                }
            try:
                self.store.create(CONTROL_INDEX, HEAD_ID, body)
            except Exception as error:
                if not self.store.is_conflict(error):
                    self._unknown_cas = True
                    raise AdmissionUnknown("batch head creation unconfirmed") from error
            head = self._read(CONTROL_INDEX, HEAD_ID)
        if head is None:
            raise AdmissionUnknown("batch head is absent after creation")
        source = head["source"]
        self._validate_log(source)
        if source["owner_id"] != self.owner_id:
            raise AdmissionConflict("batch admission owner differs")
        return head

    def _live(self, expires_at: str) -> None:
        try:
            expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        except (AttributeError, ValueError) as error:
            raise AdmissionConflict("invalid logical expiry") from error
        if expiry.tzinfo is None or self.clock() >= expiry:
            raise AdmissionConflict("task has passed its logical expiry")

    def _quarantine_if_expired(self, batch: dict) -> None:
        if any(self.clock() >= self._timestamp(entry["expires_at"]) for entry in batch["entries"]):
            self._quarantine(batch, "logical_expiry")

    def _quarantine(self, batch: dict, reason: str) -> None:
        for _ in range(8):
            if not self.owner_isolated():
                raise AdmissionConflict("old writer isolation not confirmed")
            head = self._head()
            queued = head["source"]["pending"]["batches"]
            if not any(current == batch for current in queued):
                raise AdmissionConflict("batch changed before quarantine")
            self._unquarantined(head)
            checkpoint = head["source"].get("checkpoint", {})
            quarantine = {
                "batch_id": batch["batch_id"], "digest": batch["digest"],
                "first_seq": batch["entries"][0]["arrival_seq"],
                "last_seq": batch["entries"][-1]["arrival_seq"],
                "reason": reason, "recorded_at": _utc(self.clock()),
            }
            updated = {**head["source"],
                       "checkpoint": {**checkpoint, "batch_quarantine": quarantine},
                       "updated_at": _utc(self.clock())}
            try:
                self.store.replace(CONTROL_INDEX, HEAD_ID, updated,
                                   head["seq_no"], head["primary_term"])
            except Exception as error:
                if self.store.is_conflict(error):
                    continue
                self._unknown_cas = True
                raise AdmissionUnknown("batch quarantine confirmation is unknown") from error
            message = "expired batch is quarantined" if reason == "logical_expiry" else "batch is quarantined"
            raise AdmissionConflict(message)
        raise AdmissionConflict("batch quarantine CAS contention exceeded retry bound")

    def release_quarantine(self, *, operator: str, disposition: str,
                           expected_batch_id: str | None = None) -> dict:
        """W-A 族③(a)（A1-007）：quarantine 解除通道（10 号文 L459 批面形态）。

        三面齐：权限面（operator 署名强制+owner_isolated 单活证明+点名
        expected_batch_id 防误清）｜条件面（disposition 二态闭集——drain=
        过期隔离规范通道：清槽 pending 头批、last_materialized_seq 推进至
        批末序（保留单调序号、allocated 不动、过期正文不重新物化，唯一
        写入=控制文档 CAS）+无正文 lifecycle_cleanup 检查点；retry=非
        过期原因根因已除：仅清旗保留批次，logical_expiry 闸死拒）｜审计
        落账面（checkpoint.quarantine_release=署名/处置/时刻/被解除记录
        全快照，返回值供运维日志外发）。CAS 8 次有界重试（:553-578 同
        先例）；非冲突异常 _unknown_cas=True+AdmissionUnknown（:277-278
        同纪律）。
        """
        if not isinstance(operator, str) or not operator:
            raise ValueError("operator must be a non-empty str")
        if disposition not in ("drain", "retry"):
            raise ValueError(
                f"disposition must be 'drain' or 'retry'; got {disposition!r}")
        with self._lock:
            for _ in range(8):
                if not self.owner_isolated():
                    raise AdmissionConflict("old writer isolation not confirmed")
                head = self._head()
                source = head["source"]
                if "last_terminal" in source:
                    # P1a-T2 形态卫兵：release_quarantine 是 pending 全塞
                    # 世界的运维面——游标形头文档上无批可清，且本面的
                    # pending 重建写会毒化过渡空壳（fail-closed 拒）。
                    # P1a-T3/T5 以任务终态面重建到期运维通道。
                    raise AdmissionConflict(
                        "quarantine release is not defined for cursor-form "
                        "batch logs")
                checkpoint = source.get("checkpoint", {})
                quarantine = checkpoint.get("batch_quarantine")
                if not quarantine:
                    raise AdmissionConflict("no batch quarantine to release")
                if (expected_batch_id is not None
                        and expected_batch_id != quarantine["batch_id"]):
                    raise AdmissionConflict(
                        "quarantined batch differs from named batch")
                now = _utc(self.clock())
                new_checkpoint = {key: value for key, value in checkpoint.items()
                                  if key != "batch_quarantine"}
                if disposition == "drain":
                    batches = source["pending"]["batches"]
                    if (not batches
                            or batches[0]["batch_id"] != quarantine["batch_id"]):
                        raise AdmissionConflict(
                            "quarantined batch is not the pending head")
                    if (source["last_materialized_seq"]
                            != quarantine["first_seq"] - 1):
                        raise AdmissionConflict(
                            "materialized sequence prefix is not contiguous")
                    new_checkpoint["lifecycle_cleanup"] = {
                        "kind": "quarantine_drain",
                        "batch_id": quarantine["batch_id"],
                        "digest": quarantine["digest"],
                        "first_seq": quarantine["first_seq"],
                        "last_seq": quarantine["last_seq"],
                        "cleaned_at": now, "operator": operator,
                    }
                    new_pending = {"version": LOG_VERSION,
                                   "batches": list(batches[1:])}
                    new_materialized = quarantine["last_seq"]
                else:
                    if quarantine["reason"] == "logical_expiry":
                        raise AdmissionConflict(
                            "expired batch must be drained, not retried")
                    new_pending = source["pending"]
                    new_materialized = source["last_materialized_seq"]
                release = {
                    "operator": operator, "disposition": disposition,
                    "released_at": now, "quarantine": dict(quarantine),
                }
                new_checkpoint["quarantine_release"] = release
                updated = {**source, "pending": new_pending,
                           "last_materialized_seq": new_materialized,
                           "checkpoint": new_checkpoint, "updated_at": now}
                try:
                    self.store.replace(CONTROL_INDEX, HEAD_ID, updated,
                                       head["seq_no"], head["primary_term"])
                except Exception as error:
                    if self.store.is_conflict(error):
                        continue
                    self._unknown_cas = True
                    raise AdmissionUnknown(
                        "release quarantine confirmation is unknown") from error
                return release
            raise AdmissionConflict(
                "release quarantine CAS contention exceeded retry bound")

    def _matches(self, request: AdmissionRequest, entry: dict) -> AdmissionReceipt:
        self._live(entry["expires_at"])
        if (entry["scope_id"] != request.scope_id or
                entry["request_id"] != request.request_id or
                entry["item_id"] != request.item_id or
                entry["business_fingerprint"] != _digest({
                    "item_id": request.item_id, "text": request.text
                }) or entry["text"] != request.text):
            raise IdentityConflict("request or item identity already binds other content")
        # W2 修复波 2 (a)-7（A1-010）：__dict__ 展开→dataclasses.replace
        # （与 admission.py:196 同型；逐字段语义等价——receipt_reuse 卫测钉）。
        return replace(_receipt(entry), reused=True)

    def _from_mappings(self, request: AdmissionRequest, request_map: dict, item_map: dict) -> AdmissionReceipt:
        try:
            return self._checked_mappings(request, request_map, item_map)
        except IdentityConflict:
            raise
        except (KeyError, TypeError, ValueError, AttributeError, OverflowError) as error:
            raise AdmissionConflict("materialized registration is invalid") from error

    def _checked_mappings(self, request: AdmissionRequest, request_map: dict, item_map: dict) -> AdmissionReceipt:
        source = request_map["source"]
        self._live(source["expires_at"])
        if source != {**item_map["source"], "kind": "request"}:
            raise AdmissionConflict("request and item registration disagree")
        record_id, _, _ = _keys(request.scope_id, request.request_id, request.item_id)
        target = "news-dedup-items-v1-" + source["business_date"].replace("-", ".")
        if (source.get("kind") != "request" or item_map["source"].get("kind") != "item" or
                source["record_id"] != record_id or source["target_index"] != target):
            raise AdmissionConflict("materialized registration target differs")
        if (source["scope_id"] != request.scope_id or source["request_id"] != request.request_id or
                source["item_id"] != request.item_id or
                source["business_fingerprint"] != _digest({
                    "item_id": request.item_id, "text": request.text
                })):
            raise IdentityConflict("materialized identity or text differs")
        if type(source["arrival_seq"]) is not int or not 1 <= source["arrival_seq"] <= (1 << 53) - 1:
            raise AdmissionConflict("sequence registration is invalid")
        seq_map = self._read(REQUEST_INDEX, "seq:" + str(source["arrival_seq"]))
        if (seq_map is None or seq_map["source"].get("kind") != "seq" or
                source != {**seq_map["source"], "kind": "request"}):
            raise AdmissionConflict("sequence registration is absent or inconsistent")
        main = self._read(source["target_index"], source["record_id"])
        if main is None:
            raise AdmissionConflict("materialized mapping has no main record")
        item = main["source"]
        if any(item.get(key) != source[key] for key in (
            "scope_id", "request_id", "item_id", "record_id", "business_date",
            "arrival_seq", "expires_at",
        )) or item.get("text") != request.text:
            raise AdmissionConflict("materialized main identity differs")
        if item.get("raw_hash") != hashlib.sha256(request.text.encode("utf-8")).hexdigest():
            raise AdmissionConflict("materialized raw hash differs")
        if not all(isinstance(item.get(key), str) and item[key] for key in (
            "schema_version", "pipeline_version", "embedding_space_id", "accepted_at", "received_at",
        )):
            raise AdmissionConflict("materialized version or time is invalid")
        delivery = item.get("diagnostics", {}).get("delivery", {})
        if not isinstance(delivery, dict):
            raise AdmissionConflict("frozen delivery diagnostics are invalid")
        route = delivery.get("route_ref")
        trace_id = delivery.get("trace_id")
        if (not isinstance(route, str) or not route or
                (trace_id is not None and not isinstance(trace_id, str))):
            raise AdmissionConflict("frozen delivery route or trace is invalid")
        accepted = self._timestamp(item["accepted_at"])
        self._timestamp(item["received_at"])
        day = accepted.astimezone(BUSINESS_ZONE).date()
        expiry = datetime.combine(day + timedelta(days=7), time.min, BUSINESS_ZONE)
        if source["business_date"] != day.isoformat() or self._timestamp(source["expires_at"]) != expiry:
            raise AdmissionConflict("materialized date or expiry differs")
        self._live(source["expires_at"])
        return AdmissionReceipt(
            source["scope_id"], source["request_id"], source["item_id"],
            source["record_id"], source["arrival_seq"], source["business_date"],
            item["accepted_at"], source["expires_at"], item["schema_version"],
            item["pipeline_version"], item["embedding_space_id"],
            route, trace_id=trace_id, reused=True,
        )

    # ---------- P1a-T2：work 索引检索/分号助手（受理闸序共用面） ----------

    def _store_search(self, index: str, body: dict) -> dict:
        """store duck 检索面（MemoryStore/FakeESClient/ElasticsearchBatchStore
        均提供 search；缺席=装配错误 fail-closed）。404=空结果（索引未建）。"""
        search = getattr(self.store, "search", None)
        if not callable(search):
            raise AdmissionConflict(
                "work queue lookup requires a search-capable store")
        try:
            result = search(index, body)
        except Exception as error:
            if getattr(error, "status_code", None) == 404:
                return {"hits": {"total": {"value": 0, "relation": "eq"},
                                 "hits": []}}
            raise AdmissionUnknown("work index lookup is unknown") from error
        if not isinstance(result, dict):
            raise AdmissionConflict("work index lookup response is malformed")
        return result

    def _work_lookup(self, requests: list[AdmissionRequest]) -> dict:
        """同身份未终态任务检索（①复用前置面；work 索引跨日通配）。

        语义=现役 pending 复用查询的宿主平移：命中即"受理在案"（首证
        冻结字段对拍在 ``_matches`` 收口）；request/item 双键分歧=身份
        绑定冲突（现役 by_request/by_item 对拍同型）。终态任务不经本面
        （request/item 映射读回=materialized 权威复用面）。页满=可能
        截断：漏检会把在案身份当新件重复分号——fail-closed 拒受理。
        """
        if not requests:
            return {}
        scopes = sorted({request.scope_id for request in requests})
        request_ids = [request.request_id for request in requests]
        item_ids = [request.item_id for request in requests]
        size = max(64, 8 * len(requests))
        result = self._store_search(WORK_INDEX_PATTERN, {
            "size": size, "track_total_hits": True,
            "query": {"bool": {
                "filter": [
                    {"terms": {"scope_id": scopes}},
                    {"terms": {"task_state": _WORK_LOOKUP_STATES}},
                ],
                "should": [
                    {"terms": {"request_id": request_ids}},
                    {"terms": {"item_id": item_ids}},
                ],
                "minimum_should_match": 1,
            }},
            "sort": [{"arrival_seq": "asc"}],
        })
        found = result.get("hits", {}).get("hits", [])
        if not isinstance(found, list):
            raise AdmissionConflict("work item lookup response is malformed")
        if len(found) >= size:
            raise AdmissionUnknown("work item lookup is truncated")
        request_targets = {(request.scope_id, request.request_id)
                           for request in requests}
        item_targets = {(request.scope_id, request.item_id)
                        for request in requests}
        by_request: dict[tuple[str, str], dict] = {}
        by_item: dict[tuple[str, str], dict] = {}
        for hit in found:
            if not isinstance(hit, dict):
                continue
            source = hit.get("_source")
            if not isinstance(source, dict):
                continue
            # strict 契约执法（schema 漂移=fail-closed）+PendingAdmissionV1
            # 兼容视图投影（_matches/_receipt 消费面同形态）。
            try:
                item = WorkItemV1.model_validate(source)
            except Exception as error:
                raise AdmissionConflict(
                    "work item lookup hit is schema invalid") from error
            view = _pending_view(item)
            request_key = (view["scope_id"], view["request_id"])
            item_key = (view["scope_id"], view["item_id"])
            if request_key in request_targets:
                previous = by_request.setdefault(request_key, view)
                if previous != view:
                    raise IdentityConflict(
                        "request identity binds multiple pending work items")
            if item_key in item_targets:
                previous = by_item.setdefault(item_key, view)
                if previous != view:
                    raise IdentityConflict(
                        "item identity binds multiple pending work items")
        lookup: dict[tuple, dict] = {}
        for key, value in by_request.items():
            lookup[("req", *key)] = value
        for key, value in by_item.items():
            lookup[("item", *key)] = value
        return lookup

    def _active_depth(self) -> int:
        """队列深度（ACTIVE 任务真值计数，跨业务日——对账取值面）。"""
        result = self._store_search(WORK_INDEX_PATTERN, {
            "size": 0, "track_total_hits": True,
            "query": {"bool": {"filter": [
                {"terms": {"task_state": _WORK_LOOKUP_STATES}},
            ]}},
        })
        total = result.get("hits", {}).get("total", {})
        if (not isinstance(total, dict) or total.get("relation") != "eq" or
                type(total.get("value")) is not int or total["value"] < 0):
            raise AdmissionUnknown("queue depth count is not exact")
        return total["value"]

    # ---------- P1a-T4：深度计数器对账+三档观测（容量闸取值面） ----------

    def durable_queue_depth(self) -> DurableQueueDepth | None:
        """深度计数器观测面（collector 预拒收/告警配线消费；legacy=None）。"""
        return self.durable_queue

    def durable_queue_metrics(self) -> dict | None:
        """容量观测快照（tier/对账次数/漂移计数/最老任务年龄）。"""
        counter = self.durable_queue
        if counter is None:
            return None
        metrics = counter.metrics()
        metrics["oldest_age_seconds"] = self._depth_oldest_age
        return metrics

    def _durable_depth(self, now: datetime) -> int:
        """容量闸深度取值：对账到期→ES 真值收口+档位观测；否则近似值。

        对账期间以近似值闸门（蓝图 §1.4 fail-closed——宁可多拒 429，
        不可超分序号）；分歧取 ES 真值并告警。调用方持受理锁。
        """
        counter = self.durable_queue
        if not counter.due(now):
            return counter.depth()
        before = counter.depth()
        es_value = self._active_depth()
        outcome = counter.reconcile(es_value, now=now)
        if outcome == "drifted":
            _log.warning(
                "durable queue depth reconciled to ES truth "
                "(approximate=%d, es=%d)", before, es_value)
        self._observe_depth_tier(now)
        return counter.depth()

    def _observe_depth_tier(self, now: datetime) -> None:
        """三档观测（warning=指标日志；critical/hard=升级告警+最老任务年龄联动）。"""
        counter = self.durable_queue
        tier = counter.tier()
        age = None
        if tier != "normal":
            age = self._oldest_active_age_seconds(now)
        self._depth_oldest_age = age
        if tier == "warning":
            _log.warning("durable queue depth %d entered warning tier "
                         "(capacity=%d)", counter.depth(), counter.capacity)
        elif tier == "critical":
            _log.warning("durable queue depth %d entered critical tier "
                         "(capacity=%d, oldest_age_seconds=%s)",
                         counter.depth(), counter.capacity, age)
        elif tier == "hard":
            _log.warning("durable queue depth %d at hard capacity "
                         "(capacity=%d, oldest_age_seconds=%s)",
                         counter.depth(), counter.capacity, age)

    def _oldest_active_age_seconds(self, now: datetime) -> float | None:
        """最老非终态任务年龄（critical 档联动观测；fail-soft 不阻塞受理）。

        arrival_seq 全局单调（单写者分配面）→ 升序首件≈最老受理件；
        观测值性质，检索失败/形态不识别=None（告警面不得反噬受理闸）。
        """
        try:
            hits = self._scan_work_states(
                _WORK_LOOKUP_STATES, 0, (1 << 53) - 1,
                size=1, projection=["accepted_at"])
            if not hits:
                return None
            accepted = hits[0].get("_source", {}).get("accepted_at")
            if not isinstance(accepted, str):
                return None
            return max(0.0, (now - _parse_utc(accepted)).total_seconds())
        except Exception:
            return None

    def _probe_orphans(self, cursor: int) -> tuple[int, set[int]]:
        """孤儿探针：返回（最大序, 占用序集合）——最小空闲分配的占用集。

        崩溃窗残档=work 文档序>头文档游标（enqueue 已落、CAS 未行）。
        合法活动序号恒 ≤ cursor（claim 循环只处理 ≤ cursor 任务；
        单写者纪律下本探针在途竞写不可能——受理锁内调用）。
        """
        result = self._store_search(WORK_INDEX_PATTERN, {
            "size": _ORPHAN_PROBE_SIZE, "track_total_hits": True,
            "query": {"bool": {"filter": [
                {"range": {"arrival_seq": {"gt": cursor}}},
            ]}},
            "sort": [{"arrival_seq": "desc"}],
            "_source": ["arrival_seq"],
        })
        found = result.get("hits", {}).get("hits", [])
        if not isinstance(found, list):
            raise AdmissionConflict("orphan probe response is malformed")
        if len(found) >= _ORPHAN_PROBE_SIZE:
            raise AdmissionUnknown("orphan residue exceeds probe budget")
        occupied: set[int] = set()
        maximum = 0
        for hit in found:
            seq = hit.get("_source", {}).get("arrival_seq")
            if type(seq) is not int or seq < 1:
                raise AdmissionConflict("orphan probe hit is malformed")
            occupied.add(seq)
            maximum = max(maximum, seq)
        return maximum, occupied

    @staticmethod
    def _next_seqs(cursor: int, occupied: set[int], count: int) -> list[int]:
        """最小空闲序号分配（占用集=孤儿探针读回；防同序异身份双档、
        回填崩溃窗缺号——序号不跳不重铁律的分配面执法）。"""
        seqs: list[int] = []
        candidate = cursor + 1
        while len(seqs) < count:
            if candidate not in occupied:
                seqs.append(candidate)
            candidate += 1
            if candidate > (1 << 53) - 1:
                raise AdmissionConflict("sequence space exhausted")
        return seqs

    def _adopt_cursor(self, target: int) -> None:
        """孤儿收养 CAS（last_allocated_seq→target；单调只升，幂等重入）。"""
        for _ in range(8):
            if not self.owner_isolated():
                raise AdmissionConflict("old writer isolation not confirmed")
            latest = self._head()
            if latest["source"]["last_allocated_seq"] >= target:
                return
            self._unquarantined(latest)
            updated = {
                **_cursor_write(latest["source"]),
                "last_allocated_seq": target,
                "updated_at": _utc(self.clock()),
            }
            try:
                self.store.replace(CONTROL_INDEX, HEAD_ID, updated,
                                   latest["seq_no"], latest["primary_term"])
            except Exception as error:
                if self.store.is_conflict(error):
                    continue
                self._unknown_cas = True
                raise AdmissionUnknown(
                    "orphan adoption CAS confirmation is unknown") from error
            return
        raise AdmissionConflict("orphan adoption CAS contention exceeded retry bound")

    def accept_batch(self, requests: list[AdmissionRequest]) -> list[AdmissionReceipt]:
        if not requests or len(requests) > self.limits.max_batch_items:
            raise AdmissionConflict("batch item limit exceeded")
        with self._lock:
            if self.use_durable_queue:
                return self._accept_durable(requests)
            return self._accept_legacy(requests)

    def _accept_legacy(self, requests: list[AdmissionRequest]) -> list[AdmissionReceipt]:
        """现役受理路径（P0 语义逐字节保留；``use_durable_queue=False``）。

        形态卫兵：游标形头文档归 P1a 写者/迁移器（P1a-T6）管——legacy
        受理不认领（fail-closed；防 pending 全塞写回毒化游标头文档）。
        """
        with self._lock:
            self._active()
            for _ in range(8):
                head = self._head()
                self._unquarantined(head)
                if "last_terminal" in head["source"]:
                    raise AdmissionConflict(
                        "batch log is in cursor form; legacy admission refused")
                batches = head["source"]["pending"]["batches"]
                for batch in batches:
                    self._quarantine_if_expired(batch)
                outstanding = [entry for batch in batches for entry in batch["entries"]]
                by_request = {(entry["scope_id"], entry["request_id"]): entry
                              for entry in outstanding}
                by_item = {(entry["scope_id"], entry["item_id"]): entry
                           for entry in outstanding}
                key_pairs = [_keys(r.scope_id, r.request_id, r.item_id) for r in requests]
                lookup_keys = [(REQUEST_INDEX, key) for _, req_key, item_key in key_pairs
                               for key in (req_key, item_key)]
                mappings = self._mget(lookup_keys)
                results: list[AdmissionReceipt] = []
                new_entries: list[dict] = []
                accepted_at = self.clock()
                day = accepted_at.astimezone(BUSINESS_ZONE).date()
                expires_at = datetime.combine(day + timedelta(days=7), time.min, BUSINESS_ZONE)
                for offset, request in enumerate(requests):
                    record_id, _, _ = key_pairs[offset]
                    req_map, item_map = mappings[2 * offset:2 * offset + 2]
                    req_pending = by_request.get((request.scope_id, request.request_id))
                    item_pending = by_item.get((request.scope_id, request.item_id))
                    if req_pending is not None or item_pending is not None:
                        if req_pending is None or item_pending is None or req_pending != item_pending:
                            raise IdentityConflict("request/item pending binding differs")
                        results.append(self._matches(request, req_pending))
                        continue
                    if req_map is not None and (
                        req_map["source"]["request_id"] != request.request_id or
                        req_map["source"]["item_id"] != request.item_id
                    ):
                        raise IdentityConflict("request identity binds another item")
                    if item_map is not None and (
                        item_map["source"]["item_id"] != request.item_id or
                        item_map["source"]["request_id"] != request.request_id
                    ):
                        raise IdentityConflict("item identity binds another request")
                    if req_map is not None or item_map is not None:
                        if req_map is None or item_map is None:
                            raise AdmissionConflict("materialized registration is incomplete")
                        results.append(self._from_mappings(request, req_map, item_map))
                        continue
                    entry = PendingAdmissionV1.model_validate({
                        "scope_id": request.scope_id,
                        "request_id": request.request_id, "item_id": request.item_id,
                        "record_id": record_id,
                        "business_fingerprint": _digest({
                            "item_id": request.item_id, "text": request.text
                        }),
                        "text": request.text, "delivery_route_ref": request.delivery_route_ref,
                        "trace_id": request.trace_id, "received_at": _utc(request.received_at),
                        "accepted_at": _utc(accepted_at), "business_date": day.isoformat(),
                        "expires_at": _utc(expires_at),
                        "schema_version": request.schema_version,
                        "pipeline_version": request.pipeline_version,
                        "embedding_space_id": request.embedding_space_id,
                        "arrival_seq": head["source"]["last_allocated_seq"] + len(new_entries) + 1,
                    }).model_dump()
                    by_request[(request.scope_id, request.request_id)] = entry
                    by_item[(request.scope_id, request.item_id)] = entry
                    new_entries.append(entry)
                    results.append(_receipt(entry))
                self._active()
                for batch in batches:
                    self._quarantine_if_expired(batch)
                for receipt in results:
                    self._live(receipt.expires_at)
                if not new_entries:
                    return results
                if len(outstanding) + len(new_entries) > self.limits.max_log_items:
                    raise AdmissionCapacityExceeded("log_item_limit")
                if len(_bytes(new_entries)) > self.limits.max_batch_bytes:
                    raise AdmissionCapacityExceeded("batch_byte_limit")
                envelope = {
                    "batch_id": _digest([self.owner_id, new_entries[0]["arrival_seq"]]),
                    "digest": _batch_digest(new_entries), "entries": new_entries,
                }
                proposed = [*batches, envelope]
                if len(_bytes(proposed)) > self.limits.max_log_bytes:
                    raise AdmissionCapacityExceeded("log_byte_limit")
                updated = {
                    **head["source"],
                    "last_allocated_seq": new_entries[-1]["arrival_seq"],
                    "pending": {"version": LOG_VERSION, "batches": proposed},
                    "updated_at": _utc(self.clock()),
                }
                try:
                    self.store.replace(CONTROL_INDEX, HEAD_ID, updated,
                                       head["seq_no"], head["primary_term"])
                except Exception as error:
                    if self.store.is_conflict(error):
                        # CAS 冲突意味着 head 已被人改写，必须重读。
                        self._active()
                        continue
                    self._unknown_cas = True
                    raise AdmissionUnknown("batch CAS confirmation is unknown") from error
                # CAS 已确认；出站到期只否认完整输出，不撤销新条目的受理事实。
                try:
                    for receipt in results:
                        self._live(receipt.expires_at)
                except AdmissionConflict as error:
                    raise AdmissionUnknown("batch CAS confirmed but receipt expiry prevents complete response") from error
                return results
            raise AdmissionConflict("batch CAS contention exceeded retry bound")

    def _accept_durable(self, requests: list[AdmissionRequest]) -> list[AdmissionReceipt]:
        """P1a-T2 受理切换（蓝图 1.4 闸序铁律；``use_durable_queue=True``）：
        ①同身份复用（任务文档+映射）→②容量闸→③分号→④enqueue→⑤CAS 游标。

        - ①复用：work 索引未终态任务（pending 面）+request/item 映射
          （materialized 面）双查——已受理身份永不 429、永不重复分号
          （复用先于容量闸，现役 :682-687 语义平移）；孤儿（序>游标，
          enqueue→CAS 崩溃窗残档）经收养 CAS 归账后才发回执；
        - ②容量闸：队列深度（近似计数器+周期 ES 对账，跨业务日）+新增>
          容量硬档 → AdmissionCapacityExceeded("durable_queue_capacity")
          ——**先于任何分号/CAS**（失败时 last_allocated_seq 与队内最大
          arrival_seq 均不变；对账期近似值闸门 fail-closed）；
        - ③分号：最小空闲序号（孤儿占用集回填崩溃窗缺口——防同序异
          身份双档，防永久缺号）；
        - ④enqueue：T1 原语逐条 create→读回（确定性 ID 幂等；create 面
          不经 bulk 注入钩——断连场景受理照常持久化，Scenario A 平移）；
        - ⑤CAS：游标推进（冲突→重入循环：①work 复用面把本轮已入队任务
          按已受理收口+收养，不重复分号）；CAS 确认后出站到期只否认完整
          输出（现役 :758-763 平移）。

        形态卫兵：legacy 全塞形头文档不受理（迁移器=P1a-T6 唯一合法
        改形通道，不猜不吞）。
        """
        with self._lock:
            self._active()
            for _ in range(8):
                head = self._head()
                self._unquarantined(head)
                if "last_terminal" not in head["source"]:
                    raise AdmissionConflict(
                        "batch log is not in cursor form; durable queue "
                        "admission requires migration")
                cursor = head["source"]["last_allocated_seq"]
                work_hits = self._work_lookup(requests)
                key_pairs = [_keys(r.scope_id, r.request_id, r.item_id) for r in requests]
                lookup_keys = [(REQUEST_INDEX, key) for _, req_key, item_key in key_pairs
                               for key in (req_key, item_key)]
                mappings = self._mget(lookup_keys)
                results: list[AdmissionReceipt | None] = []
                fresh_positions: list[int] = []
                fresh: list[AdmissionRequest] = []
                fresh_records: list[str] = []
                # 批内合并（现役 in-batch coalescing 平移）：fresh 身份
                # 本地索引表——批内同 request_id/item_id 复用首件（不重复
                # 分号/入队），跨绑定分歧=IdentityConflict。
                local_by_request: dict[tuple[str, str], int] = {}
                local_by_item: dict[tuple[str, str], int] = {}
                reuse_positions: dict[int, int] = {}
                work_hit_max = 0
                for offset, request in enumerate(requests):
                    record_id, _, _ = key_pairs[offset]
                    req_map, item_map = mappings[2 * offset:2 * offset + 2]
                    work_req = work_hits.get(("req", request.scope_id, request.request_id))
                    work_item = work_hits.get(("item", request.scope_id, request.item_id))
                    if work_req is not None or work_item is not None:
                        if work_req is None or work_item is None or work_req != work_item:
                            raise IdentityConflict("request/item pending binding differs")
                        results.append(self._matches(request, work_req))
                        work_hit_max = max(work_hit_max, work_req["arrival_seq"])
                        continue
                    if req_map is not None and (
                        req_map["source"]["request_id"] != request.request_id or
                        req_map["source"]["item_id"] != request.item_id
                    ):
                        raise IdentityConflict("request identity binds another item")
                    if item_map is not None and (
                        item_map["source"]["item_id"] != request.item_id or
                        item_map["source"]["request_id"] != request.request_id
                    ):
                        raise IdentityConflict("item identity binds another request")
                    if req_map is not None or item_map is not None:
                        if req_map is None or item_map is None:
                            raise AdmissionConflict("materialized registration is incomplete")
                        results.append(self._from_mappings(request, req_map, item_map))
                        continue
                    local_request = local_by_request.get((request.scope_id, request.request_id))
                    local_item = local_by_item.get((request.scope_id, request.item_id))
                    if local_request is not None or local_item is not None:
                        if (local_request is None or local_item is None or
                                local_request != local_item):
                            raise IdentityConflict("request/item pending binding differs")
                        # 五字段对拍（_matches 同语义；键已由索引表匹配，
                        # text 即剩余分歧面——指纹由 item_id+text 派生）。
                        if fresh[local_request].text != request.text:
                            raise IdentityConflict(
                                "request or item identity already binds other content")
                        results.append(None)
                        reuse_positions[len(results) - 1] = local_request
                        continue
                    results.append(None)
                    fresh_positions.append(len(results) - 1)
                    fresh.append(request)
                    fresh_records.append(record_id)
                    local_by_request[(request.scope_id, request.request_id)] = len(fresh) - 1
                    local_by_item[(request.scope_id, request.item_id)] = len(fresh) - 1
                self._active()
                for receipt in results:
                    if receipt is not None:
                        self._live(receipt.expires_at)
                if not fresh:
                    # 全复用：孤儿收养（序>游标）归账后才发回执；无孤儿=
                    # 零写快路径（空批 no-op 语义保留——现役 :729-730 平移）。
                    if work_hit_max > cursor:
                        self._adopt_cursor(work_hit_max)
                    return results
                # ② 容量闸（任何分号/CAS 之前——失败零游标增量；蓝图
                # §1.4 铁律：4096 硬档替换 T2 期 max_log_items 占位口径，
                # 近似计数器闸门+对账期 fail-closed；复用路径已在上方
                # 早返，已受理身份永不 429）。
                depth = self._durable_depth(self.clock())
                if depth + len(fresh) > self.durable_queue.capacity:
                    raise AdmissionCapacityExceeded(
                        "durable_queue_capacity",
                        depth=depth, capacity=self.durable_queue.capacity)
                # ③ 分号基线：孤儿探针+最小空闲分配。
                orphan_max, occupied = self._probe_orphans(cursor)
                accepted_at = self.clock()
                day = accepted_at.astimezone(BUSINESS_ZONE).date()
                expires_at = datetime.combine(day + timedelta(days=7), time.min, BUSINESS_ZONE)
                seqs = self._next_seqs(cursor, occupied, len(fresh))
                new_items: list[WorkItemV1] = []
                for request, record_id, arrival in zip(fresh, fresh_records, seqs):
                    new_items.append(_new_work_item(
                        request, record_id, arrival, accepted_at,
                        day.isoformat(), expires_at))
                # 入队文档大小防御（413 语义平移；log_byte_limit 随 pending
                # 全塞形态退役——头文档恒 O(1)，其保护的 4MB 约束不复存在）。
                for item in new_items:
                    if len(_bytes(item.model_dump(mode="json"))) > self.limits.max_batch_bytes:
                        raise AdmissionCapacityExceeded("batch_byte_limit")
                # ④ enqueue（T1 原语：create→读回对拍；确定性 ID 幂等）。
                for item in new_items:
                    try:
                        enqueue_work_item(self.store, item)
                    except WorkPersistUnknown as error:
                        self._note_retry_failure(error)
                        raise AdmissionUnknown(
                            "work enqueue confirmation is unknown") from error
                    except AdmissionConflict as error:
                        self._note_retry_failure(error)
                        raise
                # 入队即真值在库：计数器随 enqueue 增加（CAS 冲突重入轮
                # 复用面不再重复分号——计数一致；中途异常的部分入队由
                # 下次对账周期收口，偏差方向=少计，闸门保守性由 ES 真值
                # 周期对账兜底）。
                self.durable_queue.on_enqueue(len(new_items))
                for position, item in zip(fresh_positions, new_items):
                    results[position] = _receipt(_pending_view(item))
                for position, fresh_index in reuse_positions.items():
                    results[position] = replace(
                        _receipt(_pending_view(new_items[fresh_index])),
                        reused=True)
                # ⑤ CAS 游标（收养并入同一写：覆盖孤儿顶与复用孤儿最大序；
                # 游标写基剥过渡字段——旧形头首次重写即收敛新形）。
                new_max = max([*seqs, orphan_max, work_hit_max])
                updated = {
                    **_cursor_write(head["source"]),
                    "last_allocated_seq": new_max,
                    "updated_at": _utc(self.clock()),
                }
                try:
                    self.store.replace(CONTROL_INDEX, HEAD_ID, updated,
                                       head["seq_no"], head["primary_term"])
                except Exception as error:
                    if self.store.is_conflict(error):
                        # CAS 冲突：头文档已被改写（物化线程推进 last_terminal
                        # 等）——重入循环：①work 复用面把本轮已入队任务按
                        # 已受理收口（收养 CAS），不重复分号。
                        self._active()
                        continue
                    self._unknown_cas = True
                    raise AdmissionUnknown("batch CAS confirmation is unknown") from error
                # CAS 已确认；出站到期只否认完整输出，不撤销新条目的受理事实。
                try:
                    for receipt in results:
                        self._live(receipt.expires_at)
                except AdmissionConflict as error:
                    raise AdmissionUnknown("batch CAS confirmed but receipt expiry prevents complete response") from error
                return results
            raise AdmissionConflict("batch CAS contention exceeded retry bound")

    # ---------- P1a-T3：物化 claim 驱动循环（durable 专属；legacy 路径零触） ----------

    def _cursor_head(self, *, label: str) -> dict:
        """读头文档并做游标形态卫兵（claim 循环/终态推进共用面）。"""
        head = self._head()
        self._unquarantined(head)
        if "last_terminal" not in head["source"]:
            raise AdmissionConflict(
                f"batch log is not in cursor form; {label} requires migration")
        return head

    def _scan_work_states(self, states: list[str], low: int, high: int,
                          *, size: int, projection: list[str] | None
                          ) -> list[dict]:
        """work 族检索（claim 候选/终态证据两消费面；升序页式）。"""
        body: dict = {
            "size": size,
            "track_total_hits": True,
            "query": {"bool": {"filter": [
                {"terms": {"task_state": states}},
                {"range": {"arrival_seq": {"gt": low, "lte": high}}},
            ]}},
            "sort": [{"arrival_seq": "asc"}],
        }
        if projection is not None:
            body["_source"] = projection
        result = self._store_search(WORK_INDEX_PATTERN, body)
        found = result.get("hits", {}).get("hits", [])
        if not isinstance(found, list):
            raise AdmissionConflict("work scan response is malformed")
        return found

    def _claim_round(self) -> int:
        """一轮 claim 循环：扫描 ACTIVE 候选→逐件 claim→物化→收口。

        返回本轮**新达终态**件数（complete/tombstoned/expired；0=无可
        推进——busy/waiting 占位或已排空）。调用方持锁；熔断开路→
        MaterializeDeferred 上抛（受理面不经闸——闸只挂物化写路径）。

        P0 场景语义平移（tombstone/defer/读回三路）：
        - ITEM_PERMANENT → 墓碑（persist_tombstone+迟到补正）+任务
          ``tombstoned`` 终态（1 坏件不杀整轮：其余件照常收口）；
        - TRANSIENT_INFRA → ``retry_wait``（T2 条目账任务文档化：
          retry_count/next_retry_at/失败证据五件 CAS 写回——进程内存账
          退役，重启零漂移；退避=runtime.next_delay(retry_count+1)）；
        - UNKNOWN_WRITE → 读回对拍收口（409 命中一致=已落；未命中=
          ``retry_wait`` 零计数即时读回）；
        - 逻辑到期 → ``expired`` 终态（不重物化/单调序号保留/无正文
          检查点——批隔离退役为结构损坏专用后的任务级到期闸）。
        """
        if not self.owner_isolated():
            raise AdmissionConflict("old writer isolation not confirmed")
        self._retry_guard_attempt()
        head = self._cursor_head(label="claim round")
        source = head["source"]
        terminal = source["last_terminal"]
        allocated = source["last_allocated_seq"]
        if terminal >= allocated:
            return 0
        hits = self._scan_work_states(
            _WORK_LOOKUP_STATES, terminal, allocated,
            size=MATERIALIZE_PREFIX_ITEMS, projection=None)
        now = self.clock()
        processed = 0
        claimed: list[WorkItemV1] = []
        claims: list = []
        for hit in hits:
            candidate = hit.get("_source")
            if not isinstance(candidate, dict):
                raise AdmissionConflict("claim scan hit is malformed")
            try:
                item = WorkItemV1.model_validate(candidate)
            except Exception as error:
                raise AdmissionConflict(
                    "claim scan hit is schema invalid") from error
            # 到期闸先行（未持约活态直收 EXPIRED——不重物化；活租约跳过
            # 等其持有者收口/租约到期接管后收口）。
            if now >= _parse_utc(item.expires_at) and (
                    item.task_state != WORK_TASK_LEASED):
                self._expire_work(item)
                processed += 1
                continue
            claim = claim_work_item(
                self.store, item.scope_id, item.business_date,
                item.arrival_seq, owner_id=self.owner_id, now=now)
            if claim.outcome in ("busy", "waiting", "terminal"):
                continue
            if claim.outcome == "absent":
                # 序≤allocated 而任务文档缺席=结构性缺口（enqueue 确认
                # 先于 CAS，缺席即数据不一致）——fail-closed 不猜。
                raise AdmissionConflict(
                    "work item is absent for an allocated sequence")
            item = claim.item
            if now >= _parse_utc(item.expires_at):
                self._terminalize_fenced(
                    item, WORK_TASK_EXPIRED, "logical_expiry",
                    claim.lease_generation, now)
                processed += 1
                continue
            claimed.append(item)
            claims.append(claim)
        if claimed:
            processed += self._materialize_claimed(claimed, claims, now)
        return processed

    def _materialize_claimed(self, items: list[WorkItemV1], claims: list,
                             now) -> int:
        """claimed 批一次 bulk 四文档物化+逐件收口（P0-T4 语义任务化平移）。"""
        documents = self._documents([_pending_view(item) for item in items])
        classified = self._classified_bulk()
        if classified is not None:
            return self._resolve_durable_classified(
                classified, documents, items, claims, now)
        return self._resolve_durable_bulk(documents, items, claims, now)

    def _resolve_durable_classified(self, method, documents, items, claims,
                                    now) -> int:
        """分类逐件收口（409 读回对拍/瞬态 defer/永久入墓；结构损坏直抛）。"""
        try:
            outcomes = method(documents)
        except Exception as error:
            self._note_retry_failure(error)
            raise AdmissionUnknown(
                "bulk materialization confirmation is unknown") from error
        self._note_retry_success()
        resolved_positions = self._readback_unknowns(documents, outcomes)
        completed = 0
        for index, (item, claim) in enumerate(zip(items, claims)):
            slice_ = outcomes[index * _DOCUMENTS_PER_ENTRY:
                               (index + 1) * _DOCUMENTS_PER_ENTRY]
            permanent = [outcome for outcome in slice_
                         if outcome.failure_class is FailureClass.ITEM_PERMANENT]
            if permanent:
                failure = failure_from_outcome(permanent[0])
                self._tombstone_work(item, claim, failure, now)
                completed += 1
                continue
            transient = [outcome for outcome in slice_
                          if outcome.failure_class is
                          FailureClass.TRANSIENT_INFRA]
            unresolved = [outcome for outcome in slice_
                          if outcome.failure_class is
                          FailureClass.UNKNOWN_WRITE and
                          outcome.position not in resolved_positions]
            if transient or unresolved:
                failure = failure_from_outcome((transient or unresolved)[0])
                self._defer_work(item, claim, failure, now)
                continue
            self._complete_work(item, claim, now)
            completed += 1
        return completed

    def _readback_unknowns(self, documents, outcomes) -> set[int]:
        """UNKNOWN_WRITE 读回对拍（命中一致=已落确认；分歧=IdentityConflict
        ——结构损坏 fail-closed 直抛，批隔离面退役后无整批隔离语义）。"""
        unknown_positions = [outcome.position for outcome in outcomes
                             if outcome.failure_class is
                             FailureClass.UNKNOWN_WRITE]
        resolved: set[int] = set()
        if not unknown_positions:
            return resolved
        keys = [(documents[position][0], documents[position][1])
                for position in unknown_positions]
        try:
            found = self._mget(keys)
        except Exception as error:
            self._note_retry_failure(error)
            raise
        for position, actual in zip(unknown_positions, found):
            expected = documents[position][2]
            if (actual is None or not isinstance(actual, dict) or
                    not isinstance(actual.get("source"), dict)):
                continue            # 未命中=未确认（读回重试收敛，不定永久）
            source = actual["source"]
            if any(source.get(key) != value
                   for key, value in expected.items()
                   if key not in {"task_state", "delivery_state",
                                  "result", "diagnostics"}):
                raise IdentityConflict(
                    "materialized content diverges on readback")
            resolved.add(position)
        return resolved

    def _resolve_durable_bulk(self, documents, items, claims, now) -> int:
        """legacy bulk 面（无逐条解析 store）：整批 create+全量读回对拍。"""
        try:
            self.store.bulk_create(documents)
        except Exception as error:
            self._note_retry_failure(error)
            raise AdmissionUnknown(
                "bulk materialization confirmation is unknown") from error
        self._note_retry_success()
        try:
            found = self._mget([(index, key) for index, key, _ in documents])
        except Exception as error:
            self._note_retry_failure(error)
            raise
        for ((_, _, expected), actual) in zip(documents, found):
            if actual is None or not isinstance(actual, dict) or \
                    not isinstance(actual.get("source"), dict):
                raise AdmissionUnknown(
                    "materialized document is not yet confirmed")
            source = actual["source"]
            if any(source.get(key) != value
                   for key, value in expected.items()
                   if key not in {"task_state", "delivery_state",
                                  "result", "diagnostics"}):
                raise IdentityConflict(
                    "materialized content diverges on readback")
        completed = 0
        for item, claim in zip(items, claims):
            self._complete_work(item, claim, now)
            completed += 1
        return completed

    def _defer_work(self, item: WorkItemV1, claim, failure, now) -> None:
        """瞬态/未确认失败落账：任务文档 retry_wait（T2 条目账 CAS 写回）。"""
        if (self.retry_runtime is not None
                and failure.participates_in_breaker):
            self.retry_runtime.record_request_failure()
        definite = failure.failure_class is not FailureClass.UNKNOWN_WRITE
        delay = 0.0
        if definite:
            attempt = item.retry_count + 1
            delay = (self.retry_runtime.next_delay(attempt)
                     if self.retry_runtime is not None else 0.0)
        try:
            defer_work_item(
                self.store, item.scope_id, item.business_date,
                item.arrival_seq, failure=failure,
                delay_seconds=delay, definite=definite,
                owner_id=self.owner_id,
                lease_generation=claim.lease_generation, now=now)
        except WorkLeaseLost:
            # 代次被顶替（并发接管）：本轮放弃——接管者续驱。
            return

    def _complete_work(self, item: WorkItemV1, claim, now) -> None:
        """物化完成收口：ready 终态（result=None 占位——结论镜像 commit 域）。"""
        try:
            complete_work_item(
                self.store, item.scope_id, item.business_date,
                item.arrival_seq, result=None, owner_id=self.owner_id,
                lease_generation=claim.lease_generation, now=now)
        except WorkLeaseLost:
            return

    def _expire_work(self, item: WorkItemV1) -> None:
        """未持约活态到期收口（expired 终态；无墓碑——logical_expiry 非死）。"""
        try:
            terminalize_work_item(
                self.store, item.scope_id, item.business_date,
                item.arrival_seq, task_state=WORK_TASK_EXPIRED,
                terminal_reason="logical_expiry", owner_id=None,
                lease_generation=0, now=self.clock())
        except AdmissionConflict:
            # 竞写（他方已翻态）：跳过——状态机收口由持有者承载。
            return

    def _terminalize_fenced(self, item: WorkItemV1, task_state: str,
                            reason: str, generation: int, now) -> None:
        """持约终态收口（claim 后到期/入墓两路共用 fencing 面）。"""
        try:
            terminalize_work_item(
                self.store, item.scope_id, item.business_date,
                item.arrival_seq, task_state=task_state,
                terminal_reason=reason, owner_id=self.owner_id,
                lease_generation=generation, now=now)
        except WorkLeaseLost:
            return

    def _tombstone_work(self, item: WorkItemV1, claim, failure, now) -> None:
        """item_permanent 入墓（T3 墓碑 on-demand）+任务 tombstoned 终态。"""
        attempts = item.retry_count + 1
        first_failed = item.first_failed_at or _utc(now)
        tombstone = MaterializationTombstoneV1(
            scope_id=item.scope_id, business_date=item.business_date,
            arrival_seq=item.arrival_seq, record_id=item.record_id,
            item_id=item.item_id, raw_hash=item.raw_hash,
            failed_stage="materialize_bulk", error_class="item_permanent",
            error_code=failure.error_code, error_summary=failure.detail,
            attempt_count=attempts, first_failed_at=first_failed,
            last_failed_at=_utc(now), retryable=False,
            pipeline_version=item.pipeline_version,
            recorded_at=_utc(now),
        )
        try:
            persist_tombstone(self.store, tombstone)
        except TombstonePersistUnknown as error:
            self._note_retry_failure(error)
            raise AdmissionUnknown(
                "tombstone persistence is unconfirmed") from error
        main_index = ("news-dedup-items-v1-"
                      + item.business_date.replace("-", "."))
        try:
            reconcile_late_materialization(
                self.store, tombstone, main_index=main_index)
        except TombstonePersistUnknown as error:
            self._note_retry_failure(error)
            raise AdmissionUnknown(
                "late materialization correction is unconfirmed") from error
        # IdentityConflict 直接上抛（IDENTITY_CORRUPTION：停推进+报警）。
        self._terminalize_fenced(
            item, WORK_TASK_TOMBSTONED, failure.error_code,
            claim.lease_generation, now)

    def _terminal_evidence_prefix(self, terminal: int, allocated: int) -> int:
        """终态证据连续前缀（work 文档任务态为凭；页式扫描，fail-closed
        页溢出即停——保守低认不猜）。"""
        seqs: set[int] = set()
        cursor = terminal
        while cursor < allocated:
            hits = self._scan_work_states(
                _WORK_TERMINAL_STATES, cursor, allocated,
                size=_TERMINAL_SCAN_PAGE, projection=["arrival_seq"])
            if not hits:
                break
            for hit in hits:
                seq = hit.get("_source", {}).get("arrival_seq")
                if type(seq) is not int or seq < 1:
                    raise AdmissionConflict(
                        "terminal evidence scan hit is malformed")
                seqs.add(seq)
            last_seq = hits[-1].get("_source", {}).get("arrival_seq")
            if (len(hits) < _TERMINAL_SCAN_PAGE or
                    type(last_seq) is not int or last_seq >= allocated):
                break
            cursor = last_seq
        prefix = terminal
        while prefix + 1 in seqs:
            prefix += 1
        return prefix

    def _advance_terminal_cursor(self) -> int:
        """last_terminal 终态证据推进（CAS 单调；游标写基剥过渡字段）。

        推进量同步入深度计数器（ACTIVE→TERMINAL 出账：complete/墓碑/
        逻辑到期三路收口均经本写点落账——claim 循环独占终态推进面）。
        """
        for _ in range(8):
            if not self.owner_isolated():
                raise AdmissionConflict("old writer isolation not confirmed")
            head = self._cursor_head(label="terminal cursor advance")
            source = head["source"]
            terminal = source["last_terminal"]
            allocated = source["last_allocated_seq"]
            if terminal >= allocated:
                return terminal
            target = self._terminal_evidence_prefix(terminal, allocated)
            if target <= terminal:
                return terminal
            updated = {
                **_cursor_write(source),
                "last_terminal": target,
                "updated_at": _utc(self.clock()),
            }
            try:
                self.store.replace(CONTROL_INDEX, HEAD_ID, updated,
                                   head["seq_no"], head["primary_term"])
            except Exception as error:
                if self.store.is_conflict(error):
                    self._active()
                    continue
                self._note_retry_failure(error)
                raise AdmissionUnknown(
                    "terminal cursor CAS confirmation is unknown") from error
            self._note_retry_success()
            if self.durable_queue is not None:
                self.durable_queue.on_terminal(target - terminal)
            return target
        raise AdmissionConflict(
            "terminal cursor CAS contention exceeded retry bound")

    def _materialize_durable(self) -> int:
        """durable 物化驱动（materialize_oldest/prefix 的 claim 形态：
        单轮 claim 循环+终态游标推进；返回推进后的 last_terminal）。"""
        with self._lock:
            self._claim_round()
            return self._advance_terminal_cursor()

    # 形状单源对：admission.py:344-357 单槽物化 `main` 同构双份
    # （A1-009 在案）——改动任一处必须同步另一处 +
    # test_wave2_doc_shape_parity 卫测（W2 修复波 2 (a)-5；注：设计稿
    # 拟注释于 def 行上方，本窗置于 @staticmethod 上方以贴件头）。
    @staticmethod
    def _documents(entries: list[dict]) -> list[tuple[str, str, dict]]:
        documents: list[tuple[str, str, dict]] = []
        for entry in entries:
            target = "news-dedup-items-v1-" + entry["business_date"].replace("-", ".")
            main = {key: entry[key] for key in (
                "scope_id", "request_id", "item_id", "record_id", "schema_version",
                "pipeline_version", "embedding_space_id", "business_date", "arrival_seq",
                "received_at", "accepted_at", "expires_at", "text",
            )}
            main.update({
                "raw_hash": hashlib.sha256(entry["text"].encode("utf-8")).hexdigest(),
                "task_state": "accepted", "delivery_state": "not_ready", "result": None,
                "diagnostics": {"delivery": {
                    "route_ref": entry["delivery_route_ref"], "trace_id": entry["trace_id"],
                }},
            })
            common = {key: entry[key] for key in (
                "scope_id", "request_id", "item_id", "record_id", "business_fingerprint",
                "business_date", "arrival_seq", "expires_at",
            )}
            common["target_index"] = target
            _, request_key, item_key = _keys(entry["scope_id"], entry["request_id"], entry["item_id"])
            documents.extend([
                (target, entry["record_id"], main),
                (REQUEST_INDEX, request_key, {"kind": "request", **common}),
                (REQUEST_INDEX, item_key, {"kind": "item", **common}),
                (REQUEST_INDEX, "seq:" + str(entry["arrival_seq"]), {"kind": "seq", **common}),
            ])
        return documents

    def _legacy_bulk_verify(self, documents: list, batches: list,
                            entry_batches: list | None = None) -> None:
        """既有 legacy 物化写路径（store 无逐条解析面时；形态语义零 diff）。

        ``bulk_create`` 整批语义 + 全量 mget 对拍；PermanentBulkError 定位
        纪律与抽取前两调用点一致（oldest=无条件整批隔离；prefix=按文档
        位置定位条目批，不可定位=首批 unattributed）。
        """
        from .batch_es_store import PermanentBulkError

        def expiry() -> None:
            for batch in batches:
                self._quarantine_if_expired(batch)

        try:
            self.store.bulk_create(documents)
        except Exception as error:
            self._note_retry_failure(error)
            expiry()
            if isinstance(error, PermanentBulkError):
                if entry_batches is not None:
                    positions = error.failed_document_positions
                    first = positions[0] if positions else None
                    located = type(first) is int and 0 <= first < len(documents)
                    culprit = entry_batches[first // _DOCUMENTS_PER_ENTRY] if located else batches[0]
                    reason = "permanent_bulk_error" if located else "unattributed_permanent_bulk_error"
                    self._quarantine(culprit, reason)
                else:
                    self._quarantine(batches[0], "permanent_bulk_error")
            raise AdmissionUnknown("bulk materialization confirmation is unknown") from error
        expiry()
        try:
            found = self._mget([(index, key) for index, key, _ in documents])
        except Exception as error:
            self._note_retry_failure(error)
            expiry()
            raise
        finally:
            expiry()
        for position, ((_, _, expected), actual) in enumerate(zip(documents, found)):
            if actual is None or not isinstance(actual, dict) or not isinstance(actual.get("source"), dict):
                raise AdmissionUnknown("materialized document is not yet confirmed")
            culprit = (entry_batches[position // _DOCUMENTS_PER_ENTRY]
                       if entry_batches is not None else batches[0])
            self._compare_materialized(expected, actual["source"], culprit)

    def materialize_oldest(self) -> int:
        if self.use_durable_queue:
            # P1a-T3：claim 驱动形态（单轮有界 32 件+终态游标推进；
            # 批前缀收缩语义随 pending 全塞形态退役）。
            return self._materialize_durable()
        with self._lock:
            if not self.owner_isolated():
                raise AdmissionConflict("old writer isolation not confirmed")
            self._retry_guard_attempt()
            head = self._head()
            self._unquarantined(head)
            batches = head["source"]["pending"]["batches"]
            if not batches:
                return head["source"]["last_materialized_seq"]
            oldest = batches[0]
            entries = oldest["entries"]
            self._quarantine_if_expired(oldest)
            documents = self._documents(entries)
            self._unquarantined(self._head())
            if not self.owner_isolated():
                raise AdmissionConflict("old writer isolation not confirmed")
            # 构造文档和网络往返都可能跨零点；每次业务写前及读回后重新过门。
            self._quarantine_if_expired(oldest)
            classified = self._classified_bulk()
            if classified is not None:
                # P0-T4 逐条解析面（真实 ES store 形态）：批内单条隔离，
                # 未知读回收口，单条永久入墓——1 坏件不杀整批。
                self._resolve_classified_bulk(
                    classified, documents, entries,
                    [oldest] * len(entries), batches=[oldest],
                    unlocated_reason="permanent_bulk_error")
            else:
                self._legacy_bulk_verify(documents, [oldest])
            for _ in range(8):
                latest = self._head()
                self._unquarantined(latest)
                queued = latest["source"]["pending"]["batches"]
                if not queued or queued[0] != oldest:
                    raise AdmissionConflict("oldest batch changed before prefix shrink")
                if latest["source"]["last_materialized_seq"] != entries[0]["arrival_seq"] - 1:
                    raise AdmissionConflict("materialized sequence prefix is not contiguous")
                updated = {
                    **latest["source"],
                    "pending": {"version": LOG_VERSION, "batches": queued[1:]},
                    "last_materialized_seq": entries[-1]["arrival_seq"],
                    "updated_at": _utc(self.clock()),
                }
                if not self.owner_isolated():
                    raise AdmissionConflict("old writer isolation not confirmed")
                self._quarantine_if_expired(oldest)
                try:
                    self.store.replace(CONTROL_INDEX, HEAD_ID, updated,
                                       latest["seq_no"], latest["primary_term"])
                except Exception as error:
                    if self.store.is_conflict(error):
                        continue
                    self._note_retry_failure(error)
                    raise AdmissionUnknown("prefix shrink CAS confirmation is unknown") from error
                self._note_retry_success()
                return entries[-1]["arrival_seq"]
            raise AdmissionConflict("prefix shrink CAS contention exceeded retry bound")

    def _confirmed_prefix_head(self, selected: list[dict], before_seq: int) -> dict:
        head = self._head()
        self._unquarantined(head)
        queued = head["source"]["pending"]["batches"]
        if (queued[:len(selected)] != selected or
                head["source"]["last_materialized_seq"] != before_seq):
            raise AdmissionConflict("materialization prefix changed before confirmation")
        return head

    def materialize_prefix(self) -> int:
        """有界地物化连续完整批次；确认全部四文档后一次 CAS 收缩。"""
        if self.use_durable_queue:
            # P1a-T3：批前缀收缩语义退役——claim 轮界即件界（32 件/轮）。
            return self._materialize_durable()
        with self._lock:
            if not self.owner_isolated():
                raise AdmissionConflict("old writer isolation not confirmed")
            self._retry_guard_attempt()
            head = self._head()
            self._unquarantined(head)
            batches = head["source"]["pending"]["batches"]
            if not batches:
                return head["source"]["last_materialized_seq"]
            selected: list[dict] = []
            entries: list[dict] = []
            entry_batches: list[dict] = []
            item_limit = max(MATERIALIZE_PREFIX_ITEMS, self.limits.max_batch_items)
            for batch in batches[:MATERIALIZE_PREFIX_BATCHES]:
                batch_entries = batch["entries"]
                if selected and len(entries) + len(batch_entries) > item_limit:
                    break
                selected.append(batch)
                entries.extend(batch_entries)
                entry_batches.extend([batch] * len(batch_entries))
            before_seq = entries[0]["arrival_seq"] - 1
            for batch in selected:
                self._quarantine_if_expired(batch)
            documents = self._documents(entries)
            self._confirmed_prefix_head(selected, before_seq)
            if not self.owner_isolated():
                raise AdmissionConflict("old writer isolation not confirmed")
            for batch in selected:
                self._quarantine_if_expired(batch)
            classified = self._classified_bulk()
            if classified is not None:
                # P0-T4 逐条解析面（真实 ES store 形态）：批内单条隔离，
                # 未知读回收口，单条永久入墓——1 坏件不杀 8 件批。
                self._resolve_classified_bulk(
                    classified, documents, entries, entry_batches,
                    batches=selected,
                    unlocated_reason="unattributed_permanent_bulk_error")
            else:
                self._legacy_bulk_verify(documents, selected,
                                         entry_batches=entry_batches)
            for _ in range(8):
                latest = self._confirmed_prefix_head(selected, before_seq)
                queued = latest["source"]["pending"]["batches"]
                updated = {
                    **latest["source"],
                    "pending": {"version": LOG_VERSION, "batches": queued[len(selected):]},
                    "last_materialized_seq": entries[-1]["arrival_seq"],
                    "updated_at": _utc(self.clock()),
                }
                if not self.owner_isolated():
                    raise AdmissionConflict("old writer isolation not confirmed")
                for batch in selected:
                    self._quarantine_if_expired(batch)
                try:
                    self.store.replace(CONTROL_INDEX, HEAD_ID, updated,
                                       latest["seq_no"], latest["primary_term"])
                except Exception as error:
                    if self.store.is_conflict(error):
                        continue
                    self._note_retry_failure(error)
                    raise AdmissionUnknown("prefix shrink CAS confirmation is unknown") from error
                self._note_retry_success()
                return entries[-1]["arrival_seq"]
            raise AdmissionConflict("prefix shrink CAS contention exceeded retry bound")

    def recover(self) -> int:
        if self.use_durable_queue:
            # P1a-T3：排空驱动（轮至无可推进；等待件=退避到期时刻由
            # 编排层推进时钟后重入——域层零全局 sleep 纪律不变）。
            with self._lock:
                if not self.owner_isolated():
                    raise AdmissionConflict("old writer isolation not confirmed")
                while True:
                    head = self._cursor_head(label="recovery drain")
                    source = head["source"]
                    if source["last_terminal"] >= source["last_allocated_seq"]:
                        return source["last_terminal"]
                    processed = self._claim_round()
                    terminal = self._advance_terminal_cursor()
                    if not processed:
                        return terminal
        with self._lock:
            if not self.owner_isolated():
                raise AdmissionConflict("old writer isolation not confirmed")
            while True:
                head = self._head()
                self._unquarantined(head)
                if not head["source"]["pending"]["batches"]:
                    return head["source"]["last_materialized_seq"]
                self.materialize_oldest()

    def reconcile(self, request: AdmissionRequest) -> AdmissionReceipt:
        """实时读取 request→pending→request 恢复受理；全未命中不证明不存在。"""
        with self._lock:
            _, request_key, item_key = _keys(request.scope_id, request.request_id, request.item_id)
            request_map = self._read(REQUEST_INDEX, request_key)
            head = self._head()
            self._unquarantined(head)
            try:
                if self.use_durable_queue:
                    # P1a-T2：在途任务恢复面=work 索引（头文档 pending 恒空壳，
                    # pending 扫描面无对象）；终态/已物化复用走下方映射读回。
                    work = self._work_lookup([request])
                    work_req = work.get(("req", request.scope_id, request.request_id))
                    work_item = work.get(("item", request.scope_id, request.item_id))
                    if work_req is not None or work_item is not None:
                        if work_req is None or work_item is None or work_req != work_item:
                            raise IdentityConflict("request/item pending binding differs")
                        return self._matches(request, work_req)
                if "last_terminal" not in head["source"]:
                    # P1a-T4：游标形头不落 pending 键（旧形空壳扫描恒空，
                    # 行为等价）——pending 扫描只对 legacy 全塞头执行。
                    for batch in head["source"]["pending"]["batches"]:
                        for entry in batch["entries"]:
                            if entry["scope_id"] == request.scope_id and (
                                entry["request_id"] == request.request_id or entry["item_id"] == request.item_id
                            ):
                                self._quarantine_if_expired(batch)
                                return self._matches(request, entry)
                if request_map is None:
                    request_map = self._read(REQUEST_INDEX, request_key)
                item_map = self._read(REQUEST_INDEX, item_key)
                if request_map is None:
                    if item_map is not None:
                        raise AdmissionConflict("item identity already has a registration")
                    # 本对象的布尔值不证明所有写入者及其在途 CAS 已排空。
                    raise AdmissionUnknown("recovery lookup has insufficient evidence to prove absence")
                if item_map is None:
                    raise AdmissionConflict("materialized item registration is absent")
                return self._from_mappings(request, request_map, item_map)
            except IdentityConflict as error:
                raise AdmissionConflict("reconciliation identity binding differs") from error

    def takeover(self) -> int:
        with self._lock:
            if not self.owner_isolated():
                raise AdmissionConflict("old writer isolation not confirmed")
            for _ in range(8):
                head = self._read(CONTROL_INDEX, HEAD_ID)
                if head is None:
                    return self.recover()
                self._validate_log(head["source"])
                if head["source"]["owner_id"] == self.owner_id:
                    self._unknown_cas = False
                    return self.recover()
                updated = {**head["source"], "owner_id": self.owner_id,
                           "updated_at": _utc(self.clock())}
                try:
                    self.store.replace(CONTROL_INDEX, HEAD_ID, updated,
                                       head["seq_no"], head["primary_term"])
                except Exception as error:
                    if self.store.is_conflict(error):
                        continue
                    raise AdmissionUnknown("batch ownership transfer is unknown") from error
                self._unknown_cas = False
                return self.recover()
            raise AdmissionConflict("batch takeover CAS contention exceeded retry bound")
