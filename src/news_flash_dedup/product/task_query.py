"""P21 Q2 查询读序适配（B4 施工窗；设计=log\设计-P21接线包.md §三-Q2/§4.2）。

五步读序（10 §6.262，§三-Q2 实述版）：
step1 request key GET → step2 HEAD pending 扫 → step3 request key 再 GET
（清槽竞态）→ step4 主记录 GET（含身份对拍 R-Q14）→ step5 done。
step4 miss → 回 step2 语义重估（物化竞态恢复中=accepted 投影；无 pending
= QueryReadError，不伪装 404——R-Q7）。

逐步硬门禁（§4.2 + 10 L443 裁定句 R-Q13）：
- 授权 scope：文档 scope_id==注入 scope；不符→按未命中（404，无跨域存在性
  oracle——不报错不投影）；
- L443 到期：now < expires_at 才可见；到期→按未命中（404）；缺失/畸形→
  QueryReadError（503 域——服务端状态不可判，绝不按 404 泄露假死）。

投影（§4.2 表 R-Q12）：外层恰 12 字段（articleId/error 恒 null 占位）；
result 非终态 null、终态恰五字段（contract.py:107 _RESULT_KEYS 同集）；
callbackDelivered 三态（delivered→True / attempts>0→False / else None）；
createdAt=accepted_at；startedAt/completedAt 有出无 null；
traceId=diagnostics.delivery.trace_id（经 delivery 子对象——api/http.py:113-122
同键先例）。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from news_flash_dedup.admission import CONTROL_INDEX, REQUEST_INDEX
from news_flash_dedup.batch_admission import HEAD_ID, _keys

_RESULT_KEYS = frozenset(
    {"item_id", "text", "decision", "duplicate_ids", "reason"})


def _parse_expiry_strict(value: Any) -> datetime | None:
    """L443 到期字段严格解析（Z 尾定点化对齐 es_gateway.py:46-47 W3F 修法）：
    非串/畸形/无时区 → None（调用方映射 QueryReadError 503 域，fail-closed）。"""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(
            value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


class QueryReadError(RuntimeError):
    """服务端读序异常（503 域）：映射在、主记录/pending 均失、到期字段缺失
    或畸形、身份对拍不符——绝不伪装 404。"""


@dataclass(frozen=True)
class QueryView:
    """响应投影（§4.2 外层恰 12 字段；articleId/error 恒 null 占位）。"""
    taskId: str
    requestId: str
    articleId: None
    traceId: str | None
    taskState: str
    deliveryState: str
    createdAt: str | None
    startedAt: str | None
    completedAt: str | None
    callbackDelivered: bool | None
    result: dict | None
    error: None

    def to_dict(self) -> dict:
        return {
            "taskId": self.taskId,
            "requestId": self.requestId,
            "articleId": self.articleId,
            "traceId": self.traceId,
            "taskState": self.taskState,
            "deliveryState": self.deliveryState,
            "createdAt": self.createdAt,
            "startedAt": self.startedAt,
            "completedAt": self.completedAt,
            "callbackDelivered": self.callbackDelivered,
            "result": self.result,
            "error": self.error,
        }


def _check_expiry(value: Any, now: datetime) -> bool:
    """L443 到期门禁：True=可见（未到期）；False=到期（404 域）；
    缺失/畸形→QueryReadError（503 域）。"""
    expiry = _parse_expiry_strict(value)
    if expiry is None:
        raise QueryReadError(f"expires_at missing or malformed: {value!r}")
    return now < expiry


class TaskQueryPort:
    """查询端口：store=ElasticsearchBatchStore 语义（get(index, key) 实时 GET，
    前缀闸现役执法）；scope_id 注入（跨域调用方零 scope 参数）；clock 注入。"""

    def __init__(self, store: Any, *, scope_id: str,
                 clock: Callable[[], datetime] | None = None) -> None:
        if store is None or not callable(getattr(store, "get", None)):
            raise ValueError("TaskQueryPort requires a store with .get")
        if not scope_id:
            raise ValueError("scope_id must be non-empty")
        self._store = store
        self._scope_id = scope_id
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    # ---------- 读序 ----------

    def lookup(self, task_id: str) -> QueryView | None:
        """五步读序；None=404 域；QueryReadError=503 域。"""
        now = self._clock()
        if now.tzinfo is None:
            raise ValueError("clock must be timezone aware")
        request_key = _keys(self._scope_id, task_id, task_id)[1]
        # step1：request key 首读
        request_doc = self._get_request(request_key, now)
        if request_doc is None:
            # step2：HEAD pending 扫（映射滞后窗口/物化竞态）
            entry = self._scan_pending(task_id, now)
            if entry is not None:
                return self._project_pending(entry)
            # step3：request key 再读（清槽竞态）
            request_doc = self._get_request(request_key, now)
            if request_doc is None:
                return None
        # step4：主记录读 + 身份对拍（R-Q14）
        record_id = request_doc.get("record_id")
        target_index = request_doc.get("target_index")
        if not record_id or not target_index:
            raise QueryReadError(
                f"request doc {task_id!r} lacks record_id/target_index")
        main_state, main = self._get_main(target_index, record_id, task_id,
                                          now)
        if main_state == "ok":
            return self._project_main(main)
        if main_state == "invisible":
            return None                      # 到期/跨域→404（非物化竞态）
        # step4 miss（真缺失）→ 回 step2 语义重估（R-Q7 物化竞态恢复中）
        entry = self._scan_pending(task_id, now)
        if entry is not None:
            return self._project_pending(entry)
        raise QueryReadError(
            f"request {task_id!r} mapped but main record and pending are absent")

    def _get_request(self, request_key: str, now: datetime) -> dict | None:
        doc = self._store.get(REQUEST_INDEX, request_key)
        if doc is None:
            return None
        source = doc["source"]
        if source.get("scope_id") != self._scope_id:
            return None                          # 跨域无存在性 oracle
        if not _check_expiry(source.get("expires_at"), now):
            return None                          # L443 到期→未命中
        return source

    def _get_main(self, target_index: str, record_id: str, task_id: str,
                  now: datetime) -> tuple[str, dict | None]:
        """三态：ok=可见可投影；absent=真缺失（物化竞态候选）；
        invisible=在库但到期/跨域（404 域，非竞态）。"""
        doc = self._store.get(target_index, record_id)
        if doc is None:
            return "absent", None
        source = doc["source"]
        if source.get("scope_id") != self._scope_id:
            return "invisible", None
        if not _check_expiry(source.get("expires_at"), now):
            return "invisible", None
        # 身份对拍（R-Q14）：request_id/item_id 与 task_id 不符→503 域
        if (source.get("request_id") != task_id
                or source.get("item_id") != task_id):
            raise QueryReadError(
                f"main record {record_id!r} identity mismatch for task "
                f"{task_id!r}")
        return "ok", source

    def _scan_pending(self, task_id: str, now: datetime) -> dict | None:
        head = self._store.get(CONTROL_INDEX, HEAD_ID)
        if head is None:
            return None
        pending = head["source"].get("pending")
        if not isinstance(pending, Mapping):
            return None
        for batch in pending.get("batches") or ():
            if not isinstance(batch, Mapping):
                continue
            for entry in batch.get("entries") or ():
                if not isinstance(entry, Mapping):
                    continue
                if entry.get("scope_id") != self._scope_id:
                    continue
                if entry.get("request_id") != task_id:
                    continue
                if not _check_expiry(entry.get("expires_at"), now):
                    continue                   # 到期 entry 透明化→续扫
                return dict(entry)
        return None

    # ---------- 投影（§4.2 表） ----------

    def _project_pending(self, entry: Mapping) -> QueryView:
        return QueryView(
            taskId=entry["item_id"],
            requestId=entry["request_id"],
            articleId=None,
            traceId=entry.get("trace_id"),
            taskState="accepted",
            deliveryState="not_ready",
            createdAt=entry.get("accepted_at"),
            startedAt=None,
            completedAt=None,
            callbackDelivered=None,
            result=None,
            error=None,
        )

    def _project_main(self, source: Mapping) -> QueryView:
        diagnostics = source.get("diagnostics")
        delivery = (diagnostics.get("delivery")
                    if isinstance(diagnostics, Mapping) else None)
        trace_id = (delivery.get("trace_id")
                    if isinstance(delivery, Mapping) else None)
        delivery_state = source.get("delivery_state")
        attempts = source.get("callback_attempts", 0)
        if delivery_state == "delivered":
            callback_delivered: bool | None = True
        elif isinstance(attempts, int) and attempts > 0:
            callback_delivered = False
        else:
            callback_delivered = None
        result = source.get("result")
        if isinstance(result, Mapping) and result.get("decision") is not None:
            projected_result = {
                "item_id": source["item_id"],
                "text": source.get("text", ""),
                "decision": result["decision"],
                "duplicate_ids": list(result.get("duplicate_ids") or []),
                "reason": result.get("reason", ""),
            }
            if set(projected_result) != _RESULT_KEYS:  # 结构不变量自查
                raise QueryReadError("result projection keys drifted")
        else:
            projected_result = None
        return QueryView(
            taskId=source["item_id"],
            requestId=source["request_id"],
            articleId=None,
            traceId=trace_id,
            taskState=source.get("task_state"),
            deliveryState=delivery_state,
            createdAt=source.get("accepted_at"),
            startedAt=source.get("started_at"),
            completedAt=source.get("completed_at"),
            callbackDelivered=callback_delivered,
            result=projected_result,
            error=None,
        )


__all__ = ["QueryReadError", "QueryView", "TaskQueryPort"]
