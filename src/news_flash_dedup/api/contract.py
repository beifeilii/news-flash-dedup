"""提交与回调载荷合同；不负责网络路由、终态提交或投递。"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Mapping, Protocol

from news_flash_dedup.admission import (
    AdmissionConflict, AdmissionRequest, AdmissionReceipt, IdentityConflict,
)
from news_flash_dedup.batch_admission import AdmissionCapacityExceeded
from news_flash_dedup.batch_collector import CollectorRejected


class ContractError(ValueError):
    """线协议结构或冻结前业务结果不满足合同。"""


class BodyTooLarge(ContractError):
    """按已核实配置传入的大小限制被突破。"""


class Backpressure(RuntimeError):
    """受理点前容量不足；由真实入口的容量适配器抛出。"""


class Unavailable(RuntimeError):
    """确定未受理；由真实入口的依赖适配器抛出。"""


@dataclass(frozen=True)
class Submission:
    trace_id: str
    request_id: str
    article_id: str | None
    rewritten_id: str | int
    source_title: str
    source_body: str
    page_date: str
    rewrite_title: str
    text: str
    integration_id: str | None


@dataclass(frozen=True)
class IngressContext:
    scope_id: str
    received_at: datetime
    schema_version: str
    pipeline_version: str
    embedding_space_id: str
    delivery_route_ref: str
    max_text_codepoints: int | None = None
    max_http_bytes: int | None = None


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: dict


@dataclass(frozen=True)
class ResultContext:
    item_id: str
    text: str
    record_id: str
    scope_id: str
    business_date: str
    arrival_seq: int
    pipeline_version: str


@dataclass(frozen=True)
class MemberAudit:
    item_id: str
    record_id: str
    scope_id: str
    business_date: str
    arrival_seq: int
    query_record_id: str
    pipeline_version: str
    query_raw_hash: str
    audit_id: str
    evidence_valid: bool
    decision: str


@dataclass(frozen=True)
class CallbackBytes:
    body: bytes
    sha256: str


class AdmissionPort(Protocol):
    def accept(self, request: AdmissionRequest) -> AdmissionReceipt: ...


_REQUEST_KEYS = frozenset({"traceId", "requestId", "articleId", "rewrittenId", "source", "rewrite", "integrationId"})
_SOURCE_KEYS = frozenset({"title", "body", "pageDate"})
_REWRITE_KEYS = frozenset({"title", "body"})
_RESULT_KEYS = frozenset({"item_id", "text", "decision", "duplicate_ids", "reason"})
_DECISIONS = frozenset({"重复", "不重复", "边界case/疑难case"})
_ID_PATTERN = re.compile(r"^[1-9][0-9]*-[1-9][0-9]*$", re.ASCII)
_LOG_STAGES = frozenset({"accepted", "validation", "delivery", "callback", "error", "failed", "succeeded"})
# M-12/L 窗口P 日志可观测性修复：500 入列——AdmissionUnknown 保持"未确定"
# 语义由 HTTP 层 exception handler 转 500（见 validate_admission 尾部注释），
# 白名单缺 500 会导致该类事件的 status 在日志中被静默丢弃。
_LOG_STATUSES = frozenset({202, 409, 413, 422, 429, 500, 503})
_LOG_ERROR_CODES = frozenset({"CONTRACT_INVALID", "IDENTITY_CONFLICT", "BACKPRESSURE", "UNAVAILABLE", "ADMISSION_UNKNOWN"})
_HEX_ID = re.compile(r"[0-9a-f]{64}", re.ASCII)


def _object(value: object, keys: frozenset[str], name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(type(key) is not str for key in value):
        raise ContractError(f"{name} must be an object")
    if set(value) - keys:
        raise ContractError(f"{name} contains unknown fields")
    return value


def _string(value: object, name: str, *, minimum: int = 0, maximum: int | None = None) -> str:
    if type(value) is not str or len(value) < minimum or (maximum is not None and len(value) > maximum):
        raise ContractError(f"{name} has invalid type or length")
    if any(0xD800 <= ord(char) <= 0xDFFF for char in value):
        raise ContractError(f"{name} contains invalid Unicode")
    return value


def _optional_string(value: object, name: str, maximum: int) -> str | None:
    if value is None:
        return None
    return _string(value, name, minimum=1, maximum=maximum)


def parse_submission(value: object, *, max_text_codepoints: int | None = None,
                     max_http_bytes: int | None = None, raw_http_body: bytes | None = None) -> Submission:
    if max_http_bytes is not None and raw_http_body is not None and len(raw_http_body) > max_http_bytes:
        raise BodyTooLarge("HTTP body exceeds configured limit")
    obj = _object(value, _REQUEST_KEYS, "request")
    try:
        trace = _string(obj["traceId"], "traceId", minimum=1, maximum=256)
        request_id = _string(obj["requestId"], "requestId", minimum=1, maximum=64)
        rewritten = obj["rewrittenId"]
        source = _object(obj["source"], _SOURCE_KEYS, "source")
        rewrite = _object(obj["rewrite"], _REWRITE_KEYS, "rewrite")
        source_body = _string(source["body"], "source.body")
        # 窗口Z2（13 §1①，外部审计三轮确认）：pageDate 可选复原——缺省
        # ""（与同对象可选 title 的 .get(...,"") 缺省纪律一致；pageDate
        # 不进入判定、下游零消费，Submission.page_date:str 型不变）。
        # 传入时 min=1/max=128/必须 str 校验逐字节不变（13 快照
        # api/schemas.py SourceDocumentPayload 同格）。
        page_date = (
            _string(source["pageDate"], "source.pageDate", minimum=1, maximum=128)
            if "pageDate" in source else ""
        )
        text = _string(rewrite["body"], "rewrite.body")
    except KeyError as error:
        raise ContractError(f"missing required field: {error.args[0]}") from None
    if not _ID_PATTERN.fullmatch(request_id):
        raise ContractError("requestId has invalid format")
    if type(rewritten) is str:
        rewritten = _string(rewritten, "rewrittenId", minimum=1)
    elif type(rewritten) is not int:
        raise ContractError("rewrittenId must be string or integer")
    if max_text_codepoints is not None and len(text) > max_text_codepoints:
        raise BodyTooLarge("rewrite.body exceeds configured limit")
    article = _optional_string(obj.get("articleId"), "articleId", 256)
    integration = _optional_string(obj.get("integrationId"), "integrationId", 128)
    return Submission(
        trace, request_id, article, rewritten,
        _string(source.get("title", ""), "source.title", maximum=4000), source_body, page_date,
        _string(rewrite.get("title", ""), "rewrite.title", maximum=4000), text, integration,
    )


def _error(status: int, message: str) -> HttpResponse:
    # 快照错误外壳；现网字段级封装须由外层已核实适配器替换。
    return HttpResponse(status, {"accepted": False, "message": message, "details": []})


def submit(value: object, context: IngressContext, port: AdmissionPort,
           *, raw_http_body: bytes | None = None) -> HttpResponse:
    try:
        dto = parse_submission(value, max_text_codepoints=context.max_text_codepoints,
                               max_http_bytes=context.max_http_bytes, raw_http_body=raw_http_body)
        if not context.scope_id or not context.delivery_route_ref or context.received_at.tzinfo is None:
            raise Unavailable("trusted ingress context is incomplete")
        request = AdmissionRequest(
            context.scope_id, dto.request_id, dto.request_id, dto.text, context.received_at,
            context.schema_version, context.pipeline_version, context.embedding_space_id,
            context.delivery_route_ref, dto.trace_id,
        )
        receipt = port.accept(request)
    except BodyTooLarge:
        return _error(413, "请求正文超出限制")
    except ContractError:
        return _error(422, "请求字段无效")
    except IdentityConflict:
        return _error(409, "请求身份与已受理正文冲突")
    except Backpressure:
        return _error(429, "暂时无法受理")
    except AdmissionCapacityExceeded as error:
        # R9 外部审核 F8b 修复（主窗口 06:3x）：两类容量异常均为 AdmissionConflict
        # 子类，原先穿透到下方 503 分支——与 admission_port docstring 及
        # log/P07-接入实施设计.md L44 声称的 413 不符。字节超限语义=413；
        # 其余容量原因（log_item_limit 已被端口层转 Backpressure）保守落 503。
        if error.reason in {"batch_byte_limit", "log_byte_limit"}:
            return _error(413, "请求正文超出限制")
        return _error(503, "服务暂时不可用")
    except CollectorRejected as error:
        if error.reason == "request_too_large":
            return _error(413, "请求正文超出限制")
        return _error(503, "服务暂时不可用")
    except (Unavailable, AdmissionConflict):
        return _error(503, "服务暂时不可用")
    # AdmissionUnknown 不映射：必须保持"未确定"语义，由 HTTP 层 exception handler 转 500。
    if receipt.scope_id != context.scope_id or receipt.request_id != dto.request_id or receipt.item_id != dto.request_id:
        raise ContractError("admission receipt identity mismatch")
    return HttpResponse(202, {
        "accepted": True, "message": "任务已接收，正在处理", "taskId": receipt.request_id,
        "traceId": dto.trace_id, "requestId": receipt.request_id, "articleId": dto.article_id,
        "duplicate": receipt.reused, "state": "accepted",
    })


def validate_result(value: object, current: ResultContext,
                    audit_lookup: Callable[[str], MemberAudit | None]) -> dict:
    obj = _object(value, _RESULT_KEYS, "result")
    if set(obj) != _RESULT_KEYS:
        raise ContractError("result must contain exactly five fields")
    item_id = _string(obj["item_id"], "item_id", minimum=1)
    body = _string(obj["text"], "text")
    decision = obj["decision"]
    reason = _string(obj["reason"], "reason", minimum=1)
    ids = obj["duplicate_ids"]
    if type(decision) is not str or decision not in _DECISIONS:
        raise ContractError("decision is invalid")
    if type(ids) is not list or any(type(member) is not str or not member for member in ids):
        raise ContractError("duplicate_ids must be nonempty strings")
    if len(ids) != len(set(ids)) or (decision == "重复") != bool(ids):
        raise ContractError("duplicate_ids violates decision cardinality")
    if item_id != current.item_id or body != current.text:
        raise ContractError("result identity or original text differs")
    if decision == "重复":
        previous_seq = -1
        raw_hash = hashlib.sha256(current.text.encode("utf-8")).hexdigest()
        for member_id in ids:
            try:
                audit = audit_lookup(member_id)
            except KeyError:
                audit = None
            expected_audit_id = None if audit is None else hashlib.sha256(json.dumps(
                [current.record_id, audit.record_id, current.pipeline_version],
                ensure_ascii=False, separators=(",", ":"),
            ).encode("utf-8")).hexdigest()
            if (audit is None or audit.item_id != member_id or member_id == item_id or
                    not audit.record_id or audit.record_id == current.record_id or
                    audit.audit_id != expected_audit_id or
                    audit.scope_id != current.scope_id or audit.business_date != current.business_date or
                    type(audit.arrival_seq) is not int or not previous_seq < audit.arrival_seq < current.arrival_seq or
                    audit.query_record_id != current.record_id or
                    audit.pipeline_version != current.pipeline_version or audit.query_raw_hash != raw_hash or
                    audit.evidence_valid is not True or audit.decision != "重复"):
                raise ContractError("duplicate member lacks valid direct persisted evidence")
            previous_seq = audit.arrival_seq
    return {"item_id": item_id, "text": body, "decision": decision,
            "duplicate_ids": list(ids), "reason": reason}


def _encode(value: dict) -> CallbackBytes:
    try:
        body = json.dumps(value, ensure_ascii=False, allow_nan=False,
                          separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as error:
        raise ContractError("callback cannot be encoded") from error
    return CallbackBytes(body, hashlib.sha256(body).hexdigest())


def success_callback(value: object, current: ResultContext,
                     audit_lookup: Callable[[str], MemberAudit | None], *,
                     request_id: str, trace_id: str, completed_time: str) -> CallbackBytes:
    result = validate_result(value, current, audit_lookup)
    if request_id != result["item_id"]:
        raise ContractError("callback request identity differs")
    _string(trace_id, "traceId", minimum=1, maximum=256)
    _string(completed_time, "completedTime", minimum=1)
    return _encode({
        "requestId": request_id, "traceId": trace_id, "success": True,
        "auditDecision": result["decision"], "reason": result["reason"],
        "completedTime": completed_time, "resultJson": result,
    })


def failure_callback(*, request_id: str, trace_id: str, completed_time: str,
                     code: str, allowed_codes: frozenset[str], message: str,
                     detail: str) -> CallbackBytes:
    _string(request_id, "requestId", minimum=1)
    _string(trace_id, "traceId", minimum=1, maximum=256)
    _string(completed_time, "completedTime", minimum=1)
    _string(code, "code", minimum=1, maximum=128)
    _string(message, "message", minimum=1, maximum=256)
    _string(detail, "detail", maximum=256)
    if code not in allowed_codes or any(char in message + detail for char in "\r\n"):
        raise ContractError("failure notification has unverified or unsafe content")
    return _encode({
        "requestId": request_id, "traceId": trace_id, "completedTime": completed_time,
        "success": False, "auditDecision": "FAIL", "code": code,
        "message": message, "detail": detail, "reason": None, "resultJson": None,
    })


def ack_succeeded(status: int, body: bytes) -> bool:
    if type(status) is not int or not 200 <= status < 300:
        return False
    try:
        value = json.loads(body)
    except (TypeError, ValueError, UnicodeDecodeError):
        return False
    return type(value) is dict and value.get("succeed") is True


def safe_log_fields(event: Mapping[str, object]) -> dict[str, object]:
    """只接收有限内部枚举/数字；不记录客户端 trace 或任意字符串。"""
    safe: dict[str, object] = {}
    if event.get("stage") in _LOG_STAGES:
        safe["stage"] = event["stage"]
    status = event.get("status")
    if type(status) is int and status in _LOG_STATUSES:
        safe["status"] = status
    elif type(status) is str and status.isascii() and status.isdecimal() and int(status) in _LOG_STATUSES:
        safe["status"] = status
    record_id = event.get("record_id")
    if type(record_id) is str and _HEX_ID.fullmatch(record_id):
        safe["record_id"] = record_id
    attempt = event.get("attempt")
    if type(attempt) is int and 0 <= attempt <= 1000:
        safe["attempt"] = attempt
    duration = event.get("duration_ms")
    if type(duration) in (int, float) and math.isfinite(duration) and 0 <= duration <= 86_400_000:
        safe["duration_ms"] = duration
    if event.get("error_code") in _LOG_ERROR_CODES:
        safe["error_code"] = event["error_code"]
    return safe
