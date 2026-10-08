"""P20 发送器：fake + 真 ES 占位（UAT 接收器 ephemeral port）。

按 10 §6.5 第 4 步 + P20-设计-WIP §4：
- 仅 POST；连接 1s / 总 3s；不发明头
- ACK 单一口径 2xx + succeed=true → delivered
- 未知结果（超时/断开）→ 不重发不重计；attempt_id 核对进入续期
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import (
    DataHandler,
    FileHandler,
    FTPHandler,
    HTTPRedirectHandler,
    Request,
    build_opener,
)

from news_flash_dedup.deadline import parse_utc_iso

from .coordinator import P20ReceiveError, parse_iso_utc
from .fake_store import FakeDeliveryRecord, FakeDeliveryStore


class P20SendError(RuntimeError):
    """P20 发送失败（非业务 ACK 失败 = 网络/超时/接收器崩/响应体超限）。"""


# P2 工程债 C-12（窗口X 并线移植）：响应体 1MB 硬上限。回调 ACK 是
# succeed=true/false 小 JSON，1MB 已远超合法载荷；无界 read() 让恶意/失控
# 接收器可耗尽内存。
_MAX_RESPONSE_BYTES = 1024 * 1024


def _read_capped(stream, *, limit: int = _MAX_RESPONSE_BYTES) -> bytes:
    """读响应体，上限 limit 字节；超长抛 P20SendError（fail-closed，不静默截断——
    截断后 json 解析可能把截断体误判为 ACK 不合格，掩盖接收器异常）。"""
    body = stream.read(limit + 1)
    if len(body) > limit:
        raise P20SendError(
            f"response body exceeds {limit}-byte cap "
            f"(read {len(body)} bytes; C-12 fail-closed)"
        )
    return body


# W3F（W3d-F-NEW-2，省略向 fail-safe）：http.client.putheader 对头值做
# latin-1 编码（非 latin-1 → UnicodeEncodeError）并拒含非法控制码的值
#（\x00-\x08/\x0a-\x1f/\x7f → ValueError；TAB 合法，与 http.client
# _is_illegal_header_value 同集）——两异常均逃逸 send_callback 捕获面
#（仅 HTTPError/URLError/TimeoutError/OSError），故在头构造处前置闸掉。
_HEADER_VALUE_ILLEGAL_CHARS = frozenset(
    chr(c) for c in (*range(0x00, 0x09), *range(0x0a, 0x20), 0x7f))


def _latin1_safe_header_value(value: str) -> str | None:
    """回调附加头值合法性闸（W3F W3d-F-NEW-2，省略向）：latin-1 可编码
    且不含 http.client 非法控制码（\x00-\x08/\x0a-\x1f/\x7f）→ 原值
    逐字节通过；否则 → None（调用方省略该头不发送，fail-safe）。"""
    try:
        value.encode("latin-1")
    except UnicodeEncodeError:
        return None
    if any(ch in _HEADER_VALUE_ILLEGAL_CHARS for ch in value):
        return None
    return value


class _NoRedirect(HTTPRedirectHandler):
    """三方审计 V2 实证修复（D24）：urlopen 默认跟随重定向——301/302 会把
    POST 转 GET 投向重定向目标，回调载荷被丢弃而目标端伪 ACK（2xx+
    succeed=true）可致回调静默丢失。禁重定向：3xx 落 HTTPError →
    确定性"ACK 不合格"回 pending 进退避（与下方 HTTPError 分支同语义）。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = build_opener(_NoRedirect)
# 窗口M（回调 URL 白名单·防御纵深，窗口X 并线移植）：剔除 file/ftp/data
# 处理器——即便下方构造期 scheme 校验被绕过，opener 也无处理器可打开非
# http(s) URL（未知 scheme 落 URLError → 未知结果语义，绝不读本地文件
# 伪 ACK）。
_OPENER.handlers = [
    h for h in _OPENER.handlers
    if not isinstance(h, (FileHandler, FTPHandler, DataHandler))
]

# 窗口M（窗口X 并线）：回调 URL scheme 白名单（仅 http/https；其余 scheme
# fail-closed）。
_CALLBACK_SCHEMES = frozenset({"http", "https"})


def _assert_callback_scheme(url: str) -> None:
    """回调 URL 构造期校验：仅 http/https scheme 放行（fail-closed）。

    旧 _OPENER 保留默认 File/FTP handler 且零 scheme 校验——file:// 会
    读本地文件并把文件内容当 ACK 解析（构造 {"succeed": true} 的文件即
    伪 ACK 误标 delivered，回调静默丢失且本地文件内容经错误信道外泄）。
    白名单外 scheme（含空 scheme 的相对地址）与不可解析 URL 一律拒绝。
    """
    try:
        scheme = urlsplit(url).scheme.lower()
    except (TypeError, ValueError) as exc:
        raise P20SendError(
            f"callback URL unparseable (fail-closed): {url!r}"
        ) from exc
    if scheme not in _CALLBACK_SCHEMES:
        raise P20SendError(
            f"callback URL scheme {scheme!r} not in http/https whitelist "
            f"(fail-closed): {url!r}"
        )


@dataclass(frozen=True)
class SendResult:
    """单次发送结果。"""
    success: bool
    response_code: int | None
    response_body: str | None
    error: str | None
    elapsed_seconds: float


def send_callback(
    store: FakeDeliveryStore, record_id: str, *, timeout_connect: float = 1.0,
    timeout_total: float = 3.0,
) -> SendResult:
    """发送一次回调（仅 POST + 2xx + succeed=true → delivered；其他 → 失败）。

    三方审计 V2 注记：timeout_connect 在 urllib 单超时模型下无法与
    总超时分离实现（open() 的 timeout 同时覆盖连接与读）——保留参数
    仅作语义占位并防 连接>总 倒挂；连接/总双超时需 requests/httpx 层
    实装，已挂账（工作进度 P2 批）。

    P2 工程债 C-12 双超时裁定（窗口X 并线移植；选 a：保持单 timeout +
    文档化限制 + 保留倒挂检查）：改 http.client 拆连接/读双超时（选 b）
    需重写已封 UAT 传输路径（D24 禁重定向、HTTPError 确定性语义、opener
    链全部回归），爆炸面大；收益仅连接/总分拆——现状 timeout_total=3s
    同时约束连接，慢连接 fail-fast 落 URLError→"未知结果"续期，语义安全
    （不重发不重计）。响应体读侧风险已由 _read_capped 1MB 上限关闭
    （C-12 主修点）。
    """
    import time
    if timeout_connect > timeout_total:
        raise P20SendError(
            f"timeout_connect {timeout_connect} > timeout_total {timeout_total}"
        )
    rec = store.main_records.get(record_id)
    if rec is None:
        raise P20ReceiveError(f"unknown record {record_id}")
    if rec.delivery_state != "delivering":
        raise P20ReceiveError(
            f"record {record_id} must be in 'delivering' state to send; "
            f"got {rec.delivery_state!r}"
        )

    # 窗口M（窗口X 并线）：scheme 白名单在 Request 构造/任何 IO 之前
    # fail-closed（记录状态保持 delivering 不动——URL 非法属配置错误，
    # 不消耗尝试不计次）。
    _assert_callback_scheme(rec.route_ref)

    # 窗口W2Fγ（F-1，WB4 高危，10 §6.340 复核 L340 原文"调度、领取和实际
    # 发送前都检查 now < delivery_deadline_at 且 now < expires_at"）：
    # 发送前双查 fail-closed——任一过期即不发 + 记 expired（11 §4.3"未完成
    # 记录到期记 expired"；g0_persistence._settle L562-564 同形处置）。
    # 空串跳闸对齐 receive/update_lease（W2Fγ A15 同纪律）；畸形串包
    # P20ReceiveError fail-closed（不发送、状态不动）。
    import datetime
    now = datetime.datetime.now(datetime.timezone.utc)
    if rec.delivery_deadline_at:
        try:
            deadline_ts = parse_iso_utc(rec.delivery_deadline_at)
        except (ValueError, TypeError, AttributeError) as err:
            raise P20ReceiveError(
                f"delivery_deadline_at unparsable: "
                f"{rec.delivery_deadline_at!r}; send refused"
            ) from err
        if now >= deadline_ts:
            store.mark_state(record_id, "expired")
            return SendResult(
                False, None, None,
                f"delivery_deadline_at passed: {rec.delivery_deadline_at!r}; "
                f"record marked expired (10§6.340 send-gate)", 0.0)
    if rec.expires_at:
        try:
            expires_ts = parse_iso_utc(rec.expires_at)
        except (ValueError, TypeError, AttributeError) as err:
            raise P20ReceiveError(
                f"expires_at unparsable: {rec.expires_at!r}; send refused"
            ) from err
        if now >= expires_ts:
            store.mark_state(record_id, "expired")
            return SendResult(
                False, None, None,
                f"expires_at passed: {rec.expires_at!r}; "
                f"record marked expired (10§6.340 send-gate)", 0.0)

    t0 = time.time()
    # 窗口W2Fγ（F-ν2，WB5，13 §1③ 事实真 + P06 §5 明文"保留"）：请求头补
    # Idempotency-Key=requestId 与 X-Trace-ID——值取自冻结 callback_body
    # 内既有幂等/追踪身份（不发明新身份）；体无身份或非 JSON → 不附加
    # （HTTP 附加头 fail-safe：收件方忽略未知头，发送语义逐字节不变）。
    # 窗口W3F（W3d-F-NEW-2，省略向）：双头值经 _latin1_safe_header_value
    # 闸——非 latin-1 可编码或含非法控制码（CR/LF 等）的头值省略该头
    # 不发送（修复前该形态 UnicodeEncodeError/ValueError 逃逸本函数
    # 捕获面）；合法值逐字节不变通过。"发送语义逐字节不变"的边界声明：
    # 仅对头实际发送的形态成立——非法头值形态下该头省略（与该头从不
    # 存在逐字节同一），体与其余头不变。
    headers = {"Content-Type": "application/json"}
    try:
        frozen = json.loads(rec.callback_body)
    except (ValueError, TypeError):
        frozen = None
    if isinstance(frozen, dict):
        frozen_request_id = frozen.get("requestId")
        if type(frozen_request_id) is str and frozen_request_id:
            safe_request_id = _latin1_safe_header_value(frozen_request_id)
            if safe_request_id is not None:
                headers["Idempotency-Key"] = safe_request_id
        frozen_trace_id = frozen.get("traceId")
        if type(frozen_trace_id) is str and frozen_trace_id:
            safe_trace_id = _latin1_safe_header_value(frozen_trace_id)
            if safe_trace_id is not None:
                headers["X-Trace-ID"] = safe_trace_id
    req = Request(
        rec.route_ref,
        data=rec.callback_body.encode("utf-8"),
        method="POST",
        headers=headers,
    )
    try:
        with _OPENER.open(req, timeout=timeout_total) as resp:
            # C-12（窗口X 并线）：1MB 上限；超限 P20SendError 冒出，记录维持
            # delivering（未知结果语义：ACK 不可判定，不重发不重计，待续期
            # 恢复）。
            # 窗口W2Fγ（A2）：成功路径 decode 与错误路径同款 errors="replace"
            # 纪律——非法 UTF-8 不再裸 UnicodeDecodeError 逃逸；替换字符使
            # 垃圾体确定性落 ACK 不合格（JSON 骨架完好时仍按 succeed 字段
            # 判定，语义不放大）。
            body = _read_capped(resp).decode("utf-8", errors="replace")
            elapsed = time.time() - t0
            success = (
                200 <= resp.status < 300
                and _response_succeed(body)
            )
            if success:
                store.mark_state(record_id, "delivered")
                return SendResult(True, resp.status, body, None, elapsed)
            else:
                # ACK 不合格（业务 succeed=false）→ 不视为成功
                store.mark_state(record_id, "pending")
                return SendResult(False, resp.status, body, "ACK failed", elapsed)
    except HTTPError as exc:
        # 21:0x 主窗口修复（UAT T4 红证据 log/temp/p20-uat-run-2056.txt 两条 FAILED）：
        # urllib 对 4xx/5xx 抛 HTTPError（URLError 子类）——未单列时确定性非 2xx
        # ACK 被误并入"未知结果"（§4.4 仅限超时/断开），上方 status 检查与
        # else 分支实为死代码。按 §4.3 单一口径：非 2xx = 确定性 ACK 不合格
        # → 不视为成功、回 pending 进退避（与 else 分支同语义）。
        elapsed = time.time() - t0
        # C-12（窗口X 并线）：ACK 不合格由 status 确定性判定，先落 pending
        # 再读诊断体——诊断体同样受 1MB 上限；超限抛 P20SendError 时状态已
        # 安全落位，不静默截断（接收器异常不掩盖）。
        store.mark_state(record_id, "pending")
        err_body = _read_capped(exc).decode("utf-8", errors="replace")
        return SendResult(False, exc.code, err_body, "ACK failed", elapsed)
    except (URLError, TimeoutError, OSError) as exc:
        elapsed = time.time() - t0
        # 未知结果：维持 delivering 等待续期
        return SendResult(False, None, None, str(exc), elapsed)


def _response_succeed(body: str) -> bool:
    # R9 外部审核 F8a 修复（主窗口 06:3x）：bool("false")/bool(1) 均为 truthy
    # 曾把伪 ACK 误判成功（误标 delivered 丢回调）；12 L228 冻结 succeed=true，
    # 与本仓 contract.py ack 严格口径（is True）对齐。
    try:
        return json.loads(body).get("succeed") is True
    except (ValueError, KeyError, TypeError, AttributeError):
        return False


def advance_backoff(*, round_attempts: int) -> float:
    """退避公式：min(1s * 2^(round_attempts-1), 30min) + ≤20% 抖动。"""
    import random
    base = min(1.0 * (2 ** (round_attempts - 1)), 30 * 60)
    jitter = base * 0.2 * (2 * random.random() - 1)
    return max(0.0, base + jitter)


def persist_next_delivery(
    store: FakeDeliveryStore, record_id: str, next_seconds: float,
) -> str:
    """CAS 写 next_delivery_at = now + next_seconds。

    窗口M 伪 CAS 修复（窗口X 并线移植）：fake 仓储走 read_main_for_cas →
    cas_write_main（一致性快照 + 写时版本对拍；快照后版本被并发推进即拒，
    写不发生）。真 ES 仓储无此二法——main_records 视图 __setitem__ 内部
    即版本缓存 CAS（_cas_merge_record → _cas_write 带 if_seq_no/
    if_primary_term），维持原赋值路径逐字节不动。
    """
    import datetime
    read_cas = getattr(store, "read_main_for_cas", None)
    if callable(read_cas):
        rec, expected_version = read_cas(record_id)
    else:
        rec, expected_version = store.main_records[record_id], None
    next_at = (datetime.datetime.now(datetime.timezone.utc)
               + datetime.timedelta(seconds=next_seconds)).isoformat()
    new = FakeDeliveryRecord(
        record_id=rec.record_id, item_id=rec.item_id,
        scope_id=rec.scope_id, business_date=rec.business_date,
        arrival_seq=rec.arrival_seq,
        delivery_state=rec.delivery_state,
        delivery_deadline_at=rec.delivery_deadline_at,
        next_delivery_at=next_at,
        callback_attempts=rec.callback_attempts,
        round_attempts=rec.round_attempts,
        round=rec.round,
        callback_body=rec.callback_body,
        payload_hash=rec.payload_hash,
        route_ref=rec.route_ref,
        vector_state=rec.vector_state,
        audit_complete=rec.audit_complete,
        expires_at=rec.expires_at,
    )
    cas_write = getattr(store, "cas_write_main", None)
    if callable(cas_write):
        cas_write(new, expected_version=expected_version)
    else:
        store.main_records[record_id] = new
    return next_at


def exhaust_round(store: FakeDeliveryStore, record_id: str) -> None:
    """12 次耗尽 → exhausted + 告警。"""
    rec = store.main_records[record_id]
    if rec.round_attempts < 12:
        raise P20ReceiveError(
            f"round_attempts {rec.round_attempts} < 12; can't exhaust"
        )
    store.mark_state(record_id, "exhausted")


def expire_record(store: FakeDeliveryStore, record_id: str) -> None:
    """24h 截止到期 → expired。"""
    store.mark_state(record_id, "expired")


def reopen_round(store: FakeDeliveryStore, record_id: str) -> None:
    """delivered/exhausted 且仍在原截止内 → round+1 重开。

    M-10（10 §6.5-7"补发不刷新固定截止"，窗口X 并线移植）：重开前校验
    now < 原固定截止——now < delivery_deadline_at，且 expires_at 非空时
    now < expires_at（复用 deadline.parse_utc_iso 单源，与 scan_pending/
    receive 同口径）。越限拒绝：状态与轮次一律不动（过期记录应走
    expire_record 而非重开延寿）。delivery_deadline_at 空串 = 退化记录
    不核（现状行为保持；调度/领取路径对该形态早已解析失败，不构成新
    放行面）。
    """
    import datetime
    rec = store.main_records[record_id]
    if rec.delivery_state not in {"delivered", "exhausted"}:
        raise P20ReceiveError(
            f"only delivered/exhausted can reopen; got {rec.delivery_state!r}"
        )
    now = datetime.datetime.now(datetime.timezone.utc)
    if rec.delivery_deadline_at:
        if now >= parse_utc_iso(rec.delivery_deadline_at):
            raise P20ReceiveError(
                f"delivery_deadline_at passed: {rec.delivery_deadline_at!r} "
                f"<= now {now.isoformat()!r}; reopen refused (M-10)"
            )
    if rec.expires_at:
        if now >= parse_utc_iso(rec.expires_at):
            raise P20ReceiveError(
                f"expires_at passed: {rec.expires_at!r} "
                f"<= now {now.isoformat()!r}; reopen refused (M-10)"
            )
    store.advance_round(record_id, rec.round + 1)


__all__ = [
    "P20SendError",
    "SendResult",
    "advance_backoff",
    "exhaust_round",
    "expire_record",
    "persist_next_delivery",
    "reopen_round",
    "send_callback",
]