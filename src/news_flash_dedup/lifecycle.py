"""P08 域日和生命周期：清理白名单与跨日不召回（§10 硬约束已就位）。

P08-C/D 产品化清理路径。环境边界严格按 P07 §10：
- 仅连 UAT ES；生产零连接；fail-fast 保留到上线前不得移除；
- 候选枚举 = 日期推算 + 全匹配正则；禁止宽通配符删除；
- 硬正则闸门：任何候选必须全匹配
  `^(?:p01-batch-[A-Za-z0-9-]+-)?news-dedup-(items|audits)-v1-\\d{4}\\.\\d{2}\\.\\d{2}$`
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
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

from .admission import BUSINESS_ZONE


_CLEANUP_TARGET_PATTERN = re.compile(
    r"^(?:p01-batch-[A-Za-z0-9-]+-)?news-dedup-(items|audits)-v1-\d{4}\.\d{2}\.\d{2}$"
)

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
        if kind not in {"items", "audits"}:
            raise ValueError("kind must be 'items' or 'audits'")
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
                                  kinds: tuple[str, ...] = ("items", "audits"),
                                  index_prefix: str = "") -> list[CleanupTarget]:
    """按 today - retention_days 反推每个 kind 的具体索引名清单。

    不接受宽通配符；每个候选都必须全匹配硬正则。`index_prefix` 用于隔离
    多租户/集成测试场景，默认为空。
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
    """dry-run：列"将删除清单"，不实际删除；记录到 audit。"""
    candidates = probe_targets_present(
        client, candidate_indices_for_cleanup(
            today, retention_days=retention_days, index_prefix=index_prefix)
    )
    audit.record_dry_run(today, candidates)
    return {
        "today": today.isoformat(),
        "retention_days": retention_days,
        "dry_run": True,
        "would_delete": [c.index_name for c in candidates if c.is_present],
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


def _expired_count(client: Any, index_name: str, query: dict) -> int:
    response = client.count(index=index_name, body=query)
    body = getattr(response, "body", response)
    if not isinstance(body, Mapping) or type(body.get("count")) is not int or body["count"] < 0:
        raise RuntimeError("expired document count response is invalid")
    shards = body.get("_shards")
    if shards is not None and (not isinstance(shards, Mapping) or shards.get("failed") != 0):
        raise RuntimeError("expired document count shard read failed")
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
        count = _expired_count(client, index_name, query) if is_present else None
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


def execute_expired_pending_cleanup(
        client: Any, now: datetime, audit: CleanupAuditLog, *,
        dry_run: bool = False, index_prefix: str = "",
        isolation_confirmed: bool = False,
        control_index: str | None = None) -> dict:
    """W-A 族③(c)-1（A1-003/A1-015）：过期 pending 单槽清槽（10 号文 L459）。

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
    """
    if retention_days < 1:
        raise ValueError("retention_days must be >= 1")
    cutoff = today - timedelta(days=retention_days)
    listing = client.indices.get(
        index=(f"{index_prefix}news-dedup-items-v1-*,"
               f"{index_prefix}news-dedup-audits-v1-*"),
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
    "CleanupAuditLog",
    "CleanupTarget",
    "ExpiredDocumentTarget",
    "_CLEANUP_TARGET_PATTERN",
    "_EXPIRED_DOCUMENT_INDEX_PATTERN",
    "candidate_indices_for_cleanup",
    "dry_run_cleanup",
    "enumerate_overdue_residue",
    "execute_cleanup",
    "execute_expired_document_cleanup",
    "execute_expired_pending_cleanup",
    "expired_document_query",
    "is_valid_cleanup_target",
    "is_valid_expired_document_index",
    "plan_expired_document_cleanup",
    "probe_targets_present",
    "today_in_business_zone",
]