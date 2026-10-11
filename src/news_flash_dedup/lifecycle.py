"""P08 域日和生命周期：清理白名单与跨日不召回（§10 硬约束已就位）。

P08-C/D 产品化清理路径。环境边界严格按 P07 §10：
- 仅连 UAT ES；生产零连接；fail-fast 保留到上线前不得移除；
- 候选枚举 = 日期推算 + 全匹配正则；禁止宽通配符删除；
- 硬正则闸门：任何候选必须全匹配
  `^(?:p01-batch-[A-Za-z0-9-]+-)?news-dedup-(items|audits|work|tombstones)-v1-\\d{4}\\.\\d{2}\\.\\d{2}$`
  （p01-batch- 隔离前缀可选；M-12/L 窗口P 订正：以正则+test_lifecycle D24
  正向对照为规格，原 docstring 漏写前缀口径），不匹配的拒绝并告警，不得静默跳过；
- dry-run + 审计日志：删除前输出"将删除清单"，每次删除记录索引名+日期+触发者。

W2 批48（WB4 F-4，§9.3 清理清单 ES 侧子集补全）：
- reviews/requests 文档级 delete_by_query（expired 标记=expires_at < now 唯一判据）；
- 超期残留枚举（日索引形受限通配读取+硬正则逐个过闸，异形名告警不静默跳过）；
- 文档级目标名硬正则与日索引硬正则互不重叠（94 保留索引纪律兼容：
  控制文档/他组索引/内部索引均不在任何清理路径内）；
- 真集群执行走 UAT 闸；Milvus 分区清理不动（挂账：生产期，owner 待裁）；
- 控制文档清理：W-A 族③(c) 落地单槽面（execute_expired_pending_cleanup——
  过期 pending 清槽+无正文检查点+单调序号保留，10 号文 L459）；head 文档
  本体保留期=挂账面（登记）。

P1a-T5（卡3.6；蓝图 1.5"7 天保留+TTL 不误删活任务"）：
- 清理域扩 kind=work|tombstones（候选推算/硬正则/超期残留枚举三面同扩；
  墓碑索引 T3 按需 ensure 口径，滚动创建不进 provision——`provision_day_
  rollover` 只滚 items/audits/work）；
- work 日索引索引级删除前置非终态在位检查（task_state ∈ ACTIVE 四态
  count>0 → 缓删+告警+审计留痕，fail-closed 不删）；
- work 任务文档文档级清理（execute_work_task_cleanup）：终态任务可删
  （delete_by_query 终态集合点名）；非终态（RETRY_WAIT/租约中/补偿中）
  拒绝删除该文档——逐条审计痕（留痕跳过≠静默删除），活任务在位可续
  claim（钉 6.4）；
- `execute_expired_pending_cleanup` 为 G0 单槽头残面：P1a 批头（游标形）
  世界不走本函数（双写窗 legacy 侧仍活）；T6 迁移完成后正式退役，本卡
  起零新增调用面。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

from .admission import BUSINESS_ZONE
from .work_queue import ACTIVE_TASK_STATES, TERMINAL_TASK_STATES

_log = logging.getLogger(__name__)


_CLEANUP_TARGET_PATTERN = re.compile(
    r"^(?:p01-batch-[A-Za-z0-9-]+-)?news-dedup-(items|audits|work|tombstones)-v1-\d{4}\.\d{2}\.\d{2}$"
)

#: P1a-T5：清理域 kind 全集（items/audits=现役；work/tombstones=任务文档
#: 世界新增——蓝图 1.5"墓碑索引与任务索引纳入 lifecycle 清理清单"）。
CLEANUP_DAY_KINDS = ("items", "audits", "work", "tombstones")

_EXPIRED_DOCUMENT_INDEX_PATTERN = re.compile(
    r"^(?:p01-batch-[A-Za-z0-9-]+-)?news-dedup-(requests|reviews)-v1$"
)


@dataclass(frozen=True)
class CleanupTarget:
    """按日期推算的具体索引候选；宽通配符删除禁止。"""

    index_name: str
    business_date: date
    kind: str  # "items" or "audits"
    is_present: bool = False  # HEAD 后置填

    @classmethod
    def for_business_date(cls, business_date: date, kind: str,
                          index_prefix: str = "") -> "CleanupTarget":
        if kind not in CLEANUP_DAY_KINDS:
            raise ValueError(f"kind must be one of {CLEANUP_DAY_KINDS}")
        dotted = business_date.strftime("%Y.%m.%d")
        return cls(
            index_name=f"{index_prefix}news-dedup-{kind}-v1-{dotted}",
            business_date=business_date,
            kind=kind,
        )


def is_valid_cleanup_target(index_name: str) -> bool:
    """硬正则闸门：不匹配必须拒绝并告警，不得静默跳过。

    同时校验日期数值有效性（月 1-12、日 1-31 范围）；`2026.13.99` 等伪日期仍必须被拒。
    """
    match = _CLEANUP_TARGET_PATTERN.fullmatch(index_name)
    if not match:
        return False
    try:
        year_str, month_str, day_str = index_name.rsplit("-", 1)[1].split(".")
        year, month, day = int(year_str), int(month_str), int(day_str)
        if not (1 <= month <= 12):
            return False
        if not (1 <= day <= 31):
            return False
        date(year, month, day)
    except (ValueError, TypeError, IndexError):
        return False
    return True


def candidate_indices_for_cleanup(today: date, *, retention_days: int = 7,
                                  kinds: tuple[str, ...] = CLEANUP_DAY_KINDS,
                                  index_prefix: str = "") -> list[CleanupTarget]:
    """按 today - retention_days 反推每个 kind 的具体索引名清单。

    不接受宽通配符；每个候选都必须全匹配硬正则。`index_prefix` 用于隔离
    多租户/集成测试场景，默认为空。P1a-T5 起 kind 全集含 work/tombstones
    （任务文档/墓碑日索引同进 7 天保留清理清单——蓝图 1.5）。
    """
    if retention_days < 1:
        raise ValueError("retention_days must be >= 1")
    target_date = today - timedelta(days=retention_days)
    return [CleanupTarget.for_business_date(target_date, kind, index_prefix=index_prefix)
            for kind in kinds]


@dataclass
class CleanupAuditLog:
    """dry-run 与执行记录：每次删除记录索引名+日期+触发者。"""

    operator: str
    dry_run_runs: list[dict] = field(default_factory=list)
    deletion_runs: list[dict] = field(default_factory=list)
    document_cleanup_runs: list[dict] = field(default_factory=list)
    work_task_runs: list[dict] = field(default_factory=list)

    def record_dry_run(self, today: date, candidates: list[CleanupTarget]) -> None:
        self.dry_run_runs.append({
            "today": today.isoformat(),
            "operator": self.operator,
            "would_delete": [c.index_name for c in candidates if c.is_present],
            "absent": [c.index_name for c in candidates if not c.is_present],
        })

    def record_deletion(self, today: date, target: CleanupTarget, ok: bool,
                        error: str | None = None) -> None:
        self.deletion_runs.append({
            "today": today.isoformat(),
            "operator": self.operator,
            "index_name": target.index_name,
            "business_date": target.business_date.isoformat(),
            "kind": target.kind,
            "ok": ok,
            "error": error,
        })

    def record_document_cleanup(self, target: "ExpiredDocumentTarget", ok: bool,
                                *, error: str | None = None,
                                deleted: int | None = None,
                                expired_before: str) -> None:
        """文档级清理审计：expired 标记（expired_before）逐次入档（W2 批48）。"""
        self.document_cleanup_runs.append({
            "operator": self.operator,
            "index_name": target.index_name,
            "kind": target.kind,
            "expired_before": expired_before,
            "expired_count": target.expired_count,
            "deleted": deleted,
            "ok": ok,
            "error": error,
        })

    def record_work_task_cleanup(self, target: "WorkTaskCleanupTarget", ok: bool,
                                 *, task_id: str | None = None,
                                 task_state: str | None = None,
                                 error: str | None = None,
                                 deleted: int | None = None) -> None:
        """P1a-T5 work 任务文档清理审计：逐任务留痕（拒绝删除≠静默跳过）。

        - 逐文档拒绝（非终态活任务）：``task_id/task_state`` 必填、
          ``ok=False``、``error="active_task_not_deletable"``；
        - 索引级事件（absent/dry_run/删除完成/失败）：``task_id=None``、
          ``deleted`` 为删除计数（dry_run/absent=None）。
        """
        self.work_task_runs.append({
            "operator": self.operator,
            "index_name": target.index_name,
            "kind": "work",
            "task_id": task_id,
            "task_state": task_state,
            "deleted": deleted,
            "ok": ok,
            "error": error,
        })


def probe_targets_present(client: Any, targets: list[CleanupTarget]) -> list[CleanupTarget]:
    """逐个 HEAD 确认索引是否存在；不存在的标记 is_present=False 但仍保留在候选中。

    共享集群上严格点名核对，不做通配符列举。
    """
    result = []
    for target in targets:
        exists = bool(client.indices.exists(index=target.index_name))
        result.append(CleanupTarget(
            index_name=target.index_name,
            business_date=target.business_date,
            kind=target.kind,
            is_present=exists,
        ))
    return result


def dry_run_cleanup(client: Any, today: date, audit: CleanupAuditLog,
                    *, retention_days: int = 7,
                    index_prefix: str = "") -> dict:
    """dry-run：列"将删除清单"，不实际删除；记录到 audit。

    P1a-T5：work 日索引在位且有非终态任务 → 入 ``deferred``（缓删预告，
    与 execute 路径同判据），不进 would_delete。
    """
    candidates = probe_targets_present(
        client, candidate_indices_for_cleanup(
            today, retention_days=retention_days, index_prefix=index_prefix)
    )
    deferred = [c.index_name for c in candidates if c.is_present and c.kind == "work"
                and _validated_count(client, c.index_name,
                                     work_task_state_query(ACTIVE_TASK_STATES)) > 0]
    audit.record_dry_run(today, candidates)
    return {
        "today": today.isoformat(),
        "retention_days": retention_days,
        "dry_run": True,
        "would_delete": [c.index_name for c in candidates
                         if c.is_present and c.index_name not in deferred],
        "deferred": deferred,
        "absent": [c.index_name for c in candidates if not c.is_present],
        "all_targets": [c.index_name for c in candidates],
        "operator": audit.operator,
    }


def execute_cleanup(client: Any, today: date, audit: CleanupAuditLog,
                    *, retention_days: int = 7, dry_run: bool = False,
                    index_prefix: str = "") -> dict:
    """执行清理；逐个索引点名 DELETE，不允许宽通配符。

    任何不匹配硬正则的索引名（即便被手工传入）必须拒绝并告警，
    不静默跳过。`dry_run=True` 时只输出将删除清单，不实际删除。

    W2Fγ 条32（F-8③/WA5-L11 并案）字段名误导修复：dry_run 清单入
    `report["would_delete"]`（未真删不得冒充 deleted）；execute 路径维持
    `report["deleted"]` 不动，dry_run 下 deleted 恒空、would_delete 恒空于真删。

    P1a-T5（蓝图 1.5"TTL 不误删活任务"）：work 日索引删除前置非终态在位
    检查——task_state ∈ ACTIVE 四态 count>0 → 该索引缓删（report["deferred"]
    +审计 ok=False error="active_tasks_present"+告警日志，留痕跳过≠静默
    删除）；dry_run 同检同报。其余 kind（items/audits/tombstones）无任务态
    概念，直接删除。
    """
    candidates = candidate_indices_for_cleanup(
        today, retention_days=retention_days, index_prefix=index_prefix)
    present = probe_targets_present(client, candidates)
    report = {
        "today": today.isoformat(),
        "retention_days": retention_days,
        "dry_run": dry_run,
        "deleted": [],
        "would_delete": [],
        "deferred": [],
        "skipped_absent": [],
        "operator": audit.operator,
    }
    for target in present:
        if not is_valid_cleanup_target(target.index_name):
            audit.record_deletion(today, target, ok=False, error="invalid_target_name")
            raise ValueError(f"refused invalid cleanup target: {target.index_name}")
        if not target.is_present:
            report["skipped_absent"].append(target.index_name)
            audit.record_deletion(today, target, ok=True, error="absent")
            continue
        if target.kind == "work":
            active = _validated_count(
                client, target.index_name, work_task_state_query(ACTIVE_TASK_STATES))
            if active > 0:
                report["deferred"].append(target.index_name)
                audit.record_deletion(today, target, ok=False,
                                      error="active_tasks_present")
                _log.warning(
                    "work index deferred: %d active tasks still present in %s",
                    active, target.index_name)
                continue
        if dry_run:
            report["would_delete"].append(target.index_name)
            audit.record_deletion(today, target, ok=True, error="dry_run")
            continue
        try:
            client.indices.delete(index=target.index_name)
        except Exception as error:
            audit.record_deletion(today, target, ok=False, error=type(error).__name__)
            raise
        report["deleted"].append(target.index_name)
        audit.record_deletion(today, target, ok=True)
    return report


def today_in_business_zone(now: datetime | None = None) -> date:
    """服务端受控时钟（Asia/Shanghai）的今日日期；不接受请求时间。"""
    moment = now if now is not None else datetime.now(BUSINESS_ZONE)
    return moment.astimezone(BUSINESS_ZONE).date()


# ================= W2 批48：§9.3 ES 侧子集（文档级清理 + 残留枚举） =================


@dataclass(frozen=True)
class ExpiredDocumentTarget:
    """reviews/requests 文档级清理候选；按定名点验，禁止通配符删除。"""

    index_name: str
    kind: str  # "requests" or "reviews"
    is_present: bool = False
    expired_count: int | None = None  # 在场且 count 证实后填充


def is_valid_expired_document_index(index_name: str) -> bool:
    """文档级清理目标硬正则闸：只认 news-dedup-(requests|reviews)-v1 定名
    （p01-batch- 隔离前缀可选）；控制文档/日索引/他组索引一律拒绝。"""
    return _EXPIRED_DOCUMENT_INDEX_PATTERN.fullmatch(index_name) is not None


def _utc_iso(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("timestamp must be timezone aware")
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def expired_document_query(now: datetime) -> dict:
    """expired 标记：以 expires_at < now 为唯一过期判据（与受理侧 _live 同源语义）。"""
    return {"query": {"range": {"expires_at": {"lt": _utc_iso(now)}}}}


def _validated_count(client: Any, index_name: str, query: dict) -> int:
    """count 响应三面核验（计数形态+分片零失败）；畸形即拒。"""
    response = client.count(index=index_name, body=query)
    body = getattr(response, "body", response)
    if not isinstance(body, Mapping) or type(body.get("count")) is not int or body["count"] < 0:
        raise RuntimeError(f"count response for {index_name} is invalid")
    shards = body.get("_shards")
    if shards is not None and (not isinstance(shards, Mapping) or shards.get("failed") != 0):
        raise RuntimeError(f"count shard read for {index_name} failed")
    return body["count"]


def plan_expired_document_cleanup(
        client: Any, now: datetime, *,
        index_prefix: str = "",
        kinds: tuple[str, ...] = ("requests", "reviews")) -> list[ExpiredDocumentTarget]:
    """按定名点验 reviews/requests 索引并用 expired 标记计数；缺场保留在候选中。

    不接受宽通配符；每个候选名必须全匹配文档级硬正则，不匹配即拒（fail-closed）。
    """
    query = expired_document_query(now)
    targets: list[ExpiredDocumentTarget] = []
    for kind in kinds:
        index_name = f"{index_prefix}news-dedup-{kind}-v1"
        if not is_valid_expired_document_index(index_name):
            raise ValueError(f"refused invalid expired-document index: {index_name}")
        is_present = bool(client.indices.exists(index=index_name))
        count = _validated_count(client, index_name, query) if is_present else None
        targets.append(ExpiredDocumentTarget(
            index_name=index_name, kind=kind,
            is_present=is_present, expired_count=count))
    return targets


def execute_expired_document_cleanup(
        client: Any, now: datetime, audit: CleanupAuditLog, *,
        dry_run: bool = False, index_prefix: str = "",
        kinds: tuple[str, ...] = ("requests", "reviews")) -> dict:
    """对 reviews/requests 定名索引执行 delete_by_query（expired 标记判据）。

    - 逐索引点名，禁止宽通配符；硬正则不过即拒（构造期已闸）；
    - dry_run=True 只报"将删除"计数（would_delete），不发删除；
    - 每次处置（含 absent/dry_run/失败）都带 expired_before 标记入审计；
    - 真集群执行属 UAT 闸路径；Milvus 分区清理不在本函数口径（挂账）。
    """
    now_iso = _utc_iso(now)
    targets = plan_expired_document_cleanup(client, now, index_prefix=index_prefix,
                                            kinds=kinds)
    report: dict[str, Any] = {
        "expired_before": now_iso,
        "dry_run": dry_run,
        "deleted": {},
        "would_delete": {},
        "skipped_absent": [],
        "operator": audit.operator,
    }
    for target in targets:
        if not target.is_present:
            report["skipped_absent"].append(target.index_name)
            audit.record_document_cleanup(target, ok=True, error="absent",
                                          expired_before=now_iso)
            continue
        if dry_run:
            report["would_delete"][target.index_name] = target.expired_count
            audit.record_document_cleanup(target, ok=True, error="dry_run",
                                          expired_before=now_iso)
            continue
        # W-A 族③(c)-2（A1-014，10 号文 L459"检查 failures 及版本冲突并
        # 重试"逐字兑现）：delete_by_query 响应三面核验——timed_out 缺键同
        # 拒（bm25 闸口径）；failures 非空→报错+audit ok=False（权限/分片
        # 失败不得记成功）；version_conflicts 非 0→indices.refresh 后有界
        # 重试（最多 3 次）仍非 0→报错不吞。deleted 计数语义不变。
        failure: RuntimeError | None = None
        for attempt in range(4):                    # 1 + 最多 3 次重试（有界）
            if attempt:
                client.indices.refresh(index=target.index_name)
            try:
                response = client.delete_by_query(
                    index=target.index_name, body=expired_document_query(now),
                    refresh=True)
            except Exception as error:
                audit.record_document_cleanup(target, ok=False,
                                              error=type(error).__name__,
                                              expired_before=now_iso)
                raise
            body = getattr(response, "body", response)
            if not isinstance(body, Mapping) or type(body.get("deleted")) is not int:
                failure = RuntimeError(
                    "expired document deletion response is invalid")
                break
            if body.get("timed_out") is not False:
                failure = RuntimeError(
                    f"expired document deletion timed_out true or omitted "
                    f"status on {target.index_name}")
                break
            dbq_failures = body.get("failures")
            if dbq_failures:
                failure = RuntimeError(
                    f"expired document deletion reported failures on "
                    f"{target.index_name}: {str(dbq_failures)[:300]}")
                break
            conflicts = body.get("version_conflicts") or 0
            if conflicts:
                failure = RuntimeError(
                    f"expired document deletion version_conflicts persist on "
                    f"{target.index_name}: {conflicts}")
                continue                            # refresh 后有界重试
            failure = None
            break
        if failure is not None:
            audit.record_document_cleanup(target, ok=False,
                                          error=str(failure)[:200],
                                          expired_before=now_iso)
            raise failure
        report["deleted"][target.index_name] = body["deleted"]
        audit.record_document_cleanup(target, ok=True, deleted=body["deleted"],
                                      expired_before=now_iso)
    return report


# ================= P1a-T5：work 任务文档文档级清理（蓝图 1.5；钉 6.4） =================


def work_task_state_query(states: frozenset[str] | set[str] | tuple[str, ...]) -> dict:
    """任务状态 terms 检索体（终态集合=可删判据；非终态=在位/拒绝判据）。

    状态值取 ``work_queue.schema`` 单源（ACTIVE/TERMINAL 两集合）；
    逐值入 terms（排序输出——审计对拍确定性）。
    """
    values = sorted(states)
    if not values:
        raise ValueError("task state set must not be empty")
    return {"query": {"terms": {"task_state": values}}}


@dataclass(frozen=True)
class WorkTaskCleanupTarget:
    """work 日索引任务文档清理候选（文档级；按定名点验）。

    ``active_count``/``terminal_count``：在场且 count 证实后填充
    （active = 非终态四态计数；terminal = 终态三态计数）。
    """

    index_name: str
    business_date: date
    kind: str = "work"
    is_present: bool = False
    active_count: int | None = None
    terminal_count: int | None = None


def plan_work_task_cleanup(client: Any, today: date, *,
                           retention_days: int = 7,
                           index_prefix: str = "") -> list[WorkTaskCleanupTarget]:
    """按 today-retention 推算 work 日索引并点验任务态计数（只读）。

    候选名过硬正则闸（fail-closed）；索引缺场保留在候选中
    （active/terminal 计数 None——与 ExpiredDocumentTarget 同型）。
    """
    target_date = today - timedelta(days=retention_days)
    index_name = (f"{index_prefix}news-dedup-work-v1-"
                  + target_date.strftime("%Y.%m.%d"))
    if not is_valid_cleanup_target(index_name):
        raise ValueError(f"refused invalid work cleanup index: {index_name}")
    is_present = bool(client.indices.exists(index=index_name))
    active_count = terminal_count = None
    if is_present:
        active_count = _validated_count(
            client, index_name, work_task_state_query(ACTIVE_TASK_STATES))
        terminal_count = _validated_count(
            client, index_name, work_task_state_query(TERMINAL_TASK_STATES))
    return [WorkTaskCleanupTarget(
        index_name=index_name, business_date=target_date,
        is_present=is_present, active_count=active_count,
        terminal_count=terminal_count)]


_WORK_ENUMERATION_PAGE_SIZE = 200
_WORK_ENUMERATION_PAGE_BOUND = 100


def _enumerate_active_work_tasks(client: Any, index_name: str) -> list[dict]:
    """枚举索引内全部非终态任务文档（逐条审计痕的枚举源）。

    分页 from/size（页 200，上限 100 页=20000 > 队列硬档 4096 上界）；
    超界即 RuntimeError（fail-closed：宁可停也不无痕漏报活任务）。
    返回 ``[{"task_id", "task_state"}...]``（arrival_seq 升序）。
    """
    active: list[dict] = []
    for page in range(_WORK_ENUMERATION_PAGE_BOUND):
        response = client.search(index=index_name, body={
            **work_task_state_query(ACTIVE_TASK_STATES),
            "from": page * _WORK_ENUMERATION_PAGE_SIZE,
            "size": _WORK_ENUMERATION_PAGE_SIZE,
            "sort": [{"arrival_seq": "asc"}],
        })
        body = getattr(response, "body", response)
        hits = body.get("hits") if isinstance(body, Mapping) else None
        members = hits.get("hits") if isinstance(hits, Mapping) else None
        if not isinstance(members, list):
            raise RuntimeError(
                f"active work task enumeration response for {index_name} is invalid")
        if not members:
            return active
        for member in members:
            if not isinstance(member, Mapping) or "_id" not in member:
                raise RuntimeError(
                    f"active work task hit for {index_name} is malformed")
            source = member.get("_source")
            state = source.get("task_state") if isinstance(source, Mapping) else None
            if not isinstance(state, str):
                raise RuntimeError(
                    f"active work task state for {index_name} is malformed")
            active.append({"task_id": member["_id"], "task_state": state})
        if len(members) < _WORK_ENUMERATION_PAGE_SIZE:
            return active
    raise RuntimeError(
        f"active work task enumeration for {index_name} exceeded page bound")


def execute_work_task_cleanup(client: Any, today: date, audit: CleanupAuditLog,
                              *, retention_days: int = 7, dry_run: bool = False,
                              index_prefix: str = "") -> dict:
    """work 任务文档文档级清理（钉 6.4：TTL 不误删活任务）。

    对 today-retention 的 work 日索引（定名点验+硬正则闸）：
    - 逐条枚举非终态任务（RETRY_WAIT/租约中/补偿中/四活态）→ **拒绝删除
      该文档**：文档在位（可续 claim/收口），逐条审计痕
      （``work_task_runs``：ok=False error="active_task_not_deletable"，
      留痕跳过≠静默删除）；
    - 终态任务（READY/TOMBSTONED/EXPIRED）→ delete_by_query 点名终态
      集合删除（refresh=True；响应三面核验：timed_out 缺键同拒/failures
      非空报错/version_conflicts 有界重试 3 次仍非 0 报错不吞——与
      ``execute_expired_document_cleanup`` 同纪律）；索引级审计一条
      （deleted 计数）；
    - 索引缺场 → skipped+审计（error="absent"）；dry_run 只报
      would_delete 计数+活任务逐条痕（拒绝枚举照跑——dry-run 也要暴露
      拒删面），零写。
    """
    targets = plan_work_task_cleanup(client, today, retention_days=retention_days,
                                     index_prefix=index_prefix)
    report: dict[str, Any] = {
        "today": today.isoformat(),
        "retention_days": retention_days,
        "dry_run": dry_run,
        "deleted": {},
        "would_delete": {},
        "refused": {},
        "skipped_absent": [],
        "operator": audit.operator,
    }
    for target in targets:
        if not target.is_present:
            report["skipped_absent"].append(target.index_name)
            audit.record_work_task_cleanup(target, ok=True, error="absent")
            continue
        # 活任务逐条留痕（dry_run 与执行同跑——拒删面必须暴露）。
        refusals = _enumerate_active_work_tasks(client, target.index_name)
        for refusal in refusals:
            report["refused"].setdefault(target.index_name, []).append(
                refusal["task_id"])
            audit.record_work_task_cleanup(
                target, ok=False, task_id=refusal["task_id"],
                task_state=refusal["task_state"],
                error="active_task_not_deletable")
            _log.warning(
                "work task %s (state=%s) refused deletion in %s",
                refusal["task_id"], refusal["task_state"], target.index_name)
        if dry_run:
            report["would_delete"][target.index_name] = target.terminal_count
            audit.record_work_task_cleanup(target, ok=True, error="dry_run")
            continue
        deleted: int | None = None
        for attempt in range(4):                    # 1 + 最多 3 次重试（有界）
            if attempt:
                client.indices.refresh(index=target.index_name)
            try:
                response = client.delete_by_query(
                    index=target.index_name,
                    body=work_task_state_query(TERMINAL_TASK_STATES),
                    refresh=True)
            except Exception as error:
                audit.record_work_task_cleanup(
                    target, ok=False, error=type(error).__name__)
                raise
            body = getattr(response, "body", response)
            if not isinstance(body, Mapping) or type(body.get("deleted")) is not int:
                audit.record_work_task_cleanup(
                    target, ok=False, error="invalid_response")
                raise RuntimeError(
                    "work task deletion response is invalid")
            if body.get("timed_out") is not False:
                audit.record_work_task_cleanup(
                    target, ok=False, error="timed_out")
                raise RuntimeError(
                    "work task deletion timed out")
            if body.get("version_conflicts") not in (None, 0):
                continue                              # refresh 后有界重试
            failures = body.get("failures")
            if failures:
                audit.record_work_task_cleanup(
                    target, ok=False, error="delete_failures")
                raise RuntimeError(
                    "work task deletion reported failures")
            deleted = body["deleted"]
            break
        if deleted is None:
            audit.record_work_task_cleanup(
                target, ok=False, error="version_conflicts")
            raise RuntimeError(
                "work task deletion version conflicts exceeded retry bound")
        report["deleted"][target.index_name] = deleted
        audit.record_work_task_cleanup(target, ok=True, deleted=deleted)
    return report


def execute_expired_pending_cleanup(
        client: Any, now: datetime, audit: CleanupAuditLog, *,
        dry_run: bool = False, index_prefix: str = "",
        isolation_confirmed: bool = False,
        control_index: str | None = None) -> dict:
    """W-A 族③(c)-1（A1-003/A1-015）：过期 pending 单槽清槽（10 号文 L459）。

    【P1a-T5 废弃登记】本函数是 G0 单槽头（admission_head）形态残面：
    P1a 批头（游标形）世界不走本函数——批头 pending 恒空壳无对象可清；
    双写窗 legacy 侧（admission.py 域）仍活故暂不摘除；T6 迁移完成、
    legacy 侧退役后正式删除。本卡起 P1a 面零新增调用。

    权限：isolation_confirmed 默认 False→ValueError（单活隔离证明显式
    fail-closed，对齐 owner_isolated 纪律；本函数是运维具非写者实例）；
    control_index 默认 "news-dedup-control-v1"（与 admission.py:22 同源，
    显式参数化防拼写漂移）。

    流程：realtime GET admission_head（缺场→no-op+审计）；pending 缺场/
    未过期（expires_at > now，_live 同源判据）→no-op+审计；过期→核对已
    物化部分（主记录 items 日索引+request/item/seq 三映射逐键 realtime
    GET，在/缺布尔入证）→记录无正文 lifecycle_cleanup 检查点→清槽
    （pending=None、last_materialized_seq=max(现存, pending.arrival_seq)
    单调序号保留、last_allocated_seq 不动）——checkpoint+清槽单次 CAS
    原子写（无中间态）；CAS 冲突有界重试 3 次，耗尽→RuntimeError+audit
    ok=False；不把过期正文重新物化（唯一写=控制文档 CAS）。dry_run 只报
    would_clean+evidence，零写。
    """
    if not isolation_confirmed:
        raise ValueError(
            "single-writer isolation must be explicitly confirmed")
    from elasticsearch import ConflictError, NotFoundError

    control = control_index or "news-dedup-control-v1"
    now_iso = _utc_iso(now)
    report: dict[str, Any] = {
        "expired_before": now_iso, "dry_run": dry_run,
        "cleaned": False, "would_clean": False, "operator": audit.operator,
    }

    def _audit_pending(ok: bool, *, error: str | None = None) -> None:
        audit.record_document_cleanup(
            ExpiredDocumentTarget(index_name=control, kind="control",
                                  is_present=True, expired_count=None),
            ok, error=error, expired_before=now_iso)

    try:
        head = client.get(index=control, id="admission_head", realtime=True)
    except NotFoundError:
        _audit_pending(True, error="absent")
        return report
    source = head["_source"]
    pending = source.get("pending")
    if not isinstance(pending, Mapping):
        _audit_pending(True, error="no_pending")
        return report
    expires_at = pending.get("expires_at")
    try:
        expiry = datetime.fromisoformat(
            str(expires_at).replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(
            f"pending expires_at malformed: {expires_at!r}") from error
    if expiry.tzinfo is None or now < expiry:
        _audit_pending(True, error="not_expired")     # 未过期 → no-op
        return report

    record_id = pending["record_id"]
    item_id = pending["item_id"]
    request_id = pending["request_id"]
    scope_id = pending["scope_id"]
    business_date = pending["business_date"]
    arrival_seq = pending["arrival_seq"]
    # 日索引点名=canonical ISO(dash)→dotted 形（es_admission_schema.day_index
    # :22 同源工艺——pending 存 dash 形、索引名 dotted 形）。
    items_index = (f"{index_prefix}news-dedup-items-v1-"
                   + business_date.replace("-", "."))
    requests_index = f"{index_prefix}news-dedup-requests-v1"

    def _present(index: str, key: str) -> bool:
        try:
            client.get(index=index, id=key, realtime=True)
        except NotFoundError:
            return False
        return True

    evidence = {
        "main": _present(items_index, record_id),
        "request": _present(requests_index, f"{scope_id}|{request_id}"),
        "item": _present(requests_index, f"{scope_id}|item|{item_id}"),
        "seq": _present(requests_index, f"seq:{arrival_seq}"),
    }
    cleanup_checkpoint = {
        "kind": "expired_pending_cleanup",
        "record_id": record_id, "item_id": item_id,
        "arrival_seq": arrival_seq, "business_date": business_date,
        "expires_at": expires_at,
        "materialized_evidence": evidence,
        "cleaned_at": now_iso, "operator": audit.operator,
    }                                                   # 无 text 字段（钉）
    if dry_run:
        report["would_clean"] = True
        _audit_pending(True, error="dry_run")
        return report
    for _attempt in range(3):                           # CAS 有界重试 3 次
        checkpoint = source.get("checkpoint")
        updated = {
            **source,
            "pending": None,
            "last_materialized_seq": max(
                int(source.get("last_materialized_seq", 0) or 0),
                int(arrival_seq)),
            "checkpoint": {**(checkpoint if isinstance(checkpoint, Mapping) else {}),
                           "lifecycle_cleanup": cleanup_checkpoint},
            "updated_at": now_iso,
        }
        try:
            client.index(index=control, id="admission_head", document=updated,
                         if_seq_no=head["_seq_no"],
                         if_primary_term=head["_primary_term"],
                         refresh="wait_for")
        except ConflictError:
            try:
                head = client.get(index=control, id="admission_head",
                                  realtime=True)
            except NotFoundError:
                break
            source = head["_source"]
            if (source.get("pending") or {}).get("record_id") != record_id:
                break                                   # 槽位已易主：不冒领
            continue
        report["cleaned"] = True
        _audit_pending(True)
        return report
    _audit_pending(False, error="cas_contention")
    raise RuntimeError(
        "expired pending cleanup CAS contention exceeded retry bound")


def enumerate_overdue_residue(client: Any, today: date, *,
                              retention_days: int = 7,
                              index_prefix: str = "") -> dict:
    """超期残留枚举：列出仍在集群但业务日早于 today-retention_days 的日索引。

    日索引清理只点名 T-retention 当日；更早的残留（清理缺跑期形成）本函数枚举。
    读取用本命名空间日索引形受限通配；每个名字仍逐个过硬正则：
    - 匹配且业务日 < cutoff → residue；
    - 本命名空间内异形名（伪日期/未补零等） → alien_alarms 告警，不静默跳过；
    - 删除仍只允许经 execute_cleanup 点名执行，本函数不删任何索引。

    P1a-T5：通配覆盖扩 work/tombstones 两支（超期残留枚举=清理域全集）。
    """
    if retention_days < 1:
        raise ValueError("retention_days must be >= 1")
    cutoff = today - timedelta(days=retention_days)
    listing = client.indices.get(
        index=",".join(
            f"{index_prefix}news-dedup-{kind}-v1-*" for kind in CLEANUP_DAY_KINDS),
        allow_no_indices=True, ignore_unavailable=True)
    listing_map = listing if isinstance(listing, Mapping) else getattr(listing, "body", None)
    if not isinstance(listing_map, Mapping):
        raise RuntimeError("overdue residue listing response is invalid")
    residue: list[str] = []
    alien_alarms: list[str] = []
    for name in sorted(listing_map.keys()):
        if not isinstance(name, str) or not is_valid_cleanup_target(name):
            alien_alarms.append(str(name))
            continue
        dotted = name.rsplit("-", 1)[1]
        business = date(int(dotted[0:4]), int(dotted[5:7]), int(dotted[8:10]))
        if business < cutoff:
            residue.append(name)
    return {
        "today": today.isoformat(),
        "retention_days": retention_days,
        "cutoff": cutoff.isoformat(),
        "residue": residue,
        "alien_alarms": alien_alarms,
    }


__all__ = [
    "BUSINESS_ZONE",
    "CLEANUP_DAY_KINDS",
    "CleanupAuditLog",
    "CleanupTarget",
    "ExpiredDocumentTarget",
    "WorkTaskCleanupTarget",
    "_CLEANUP_TARGET_PATTERN",
    "_EXPIRED_DOCUMENT_INDEX_PATTERN",
    "candidate_indices_for_cleanup",
    "dry_run_cleanup",
    "enumerate_overdue_residue",
    "execute_cleanup",
    "execute_expired_document_cleanup",
    "execute_expired_pending_cleanup",
    "execute_work_task_cleanup",
    "expired_document_query",
    "is_valid_cleanup_target",
    "is_valid_expired_document_index",
    "plan_expired_document_cleanup",
    "plan_work_task_cleanup",
    "probe_targets_present",
    "today_in_business_zone",
    "work_task_state_query",
]