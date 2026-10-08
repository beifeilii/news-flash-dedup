# -*- coding: utf-8 -*-
"""W2Fγ 红测（sender 族）：条目 14（A2 decode 纪律）/45（F-1 发送双闸）/83（F-ν2 双头）。

红能力（修复前原貌）：
- 条目14 成功路径 `_read_capped(resp).decode("utf-8")` 无 errors 纪律——
  2xx + 非法 UTF-8 体 → UnicodeDecodeError 逃逸（非 ACK 不合格确定性语义）；
- 条目45 send_callback 入口无 deadline/expires_at 双查（10§6.340"调度、领取和
  实际发送前都检查"）——过期记录照发；
- 条目83 请求头仅 Content-Type（13§1③/P06§5 明文保留 Idempotency-Key=
  requestId 与 X-Trace-ID）。
"""
from __future__ import annotations

import datetime
import hashlib
import http.server
import json
import threading

import pytest

from news_flash_dedup.delivery.fake_store import FakeDeliveryRecord, FakeDeliveryStore
from news_flash_dedup.delivery.sender import send_callback

UTC = datetime.timezone.utc


def _record(callback_body='{"succeed": true}', **over):
    now = datetime.datetime.now(UTC)
    fields = dict(
        record_id="r-1", item_id="item-A", scope_id="default",
        business_date="2026-09-26", arrival_seq=1,
        delivery_state="delivering",
        delivery_deadline_at=(now + datetime.timedelta(hours=24)).isoformat(),
        next_delivery_at=(now - datetime.timedelta(hours=1)).isoformat(),
        callback_attempts=0, round_attempts=1, round=1,
        callback_body=callback_body,
        payload_hash=hashlib.sha256(callback_body.encode("utf-8")).hexdigest(),
        route_ref="http://127.0.0.1:8080/callback",
        vector_state="ready", audit_complete=True,
    )
    fields.update(over)
    return FakeDeliveryRecord(**fields)


class _PayloadHandler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        # 捕获请求头供 F-ν2 断言——键名一律小写归一（urllib/http 管道对
        # 头名逐段 title-case，如 X-Trace-ID → X-Trace-Id；HTTP 头名 RFC
        # 7230 大小写不敏感，断言语义=头存在+值）。
        self.server.captured_headers = {
            k.lower(): v for k, v in self.headers.items()}
        payload = self.server.payload
        self.send_response(self.server.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


def _serve(status: int, payload: bytes):
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _PayloadHandler)
    server.status = status
    server.payload = payload
    server.captured_headers = {}
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


# ---------- 条目14（A2）：成功路径 decode 纪律 ----------

def test_send_success_path_invalid_utf8_no_decode_escape():
    """红能力：2xx + 局部非法 UTF-8 体（JSON 骨架完好）——旧裸
    UnicodeDecodeError 逃逸（非三态语义）；修复后 errors 纪律（与错误路径
    L183 同款）→ 体仍可解析 succeed=true → delivered，不冒异常。"""
    server = _serve(200, b'{"succeed": true, "note": "\xff\xfe\xfd"}')
    try:
        store = FakeDeliveryStore()
        store.upsert_main(_record(
            route_ref=f"http://127.0.0.1:{server.server_port}/callback"))
        result = send_callback(store, "r-1", timeout_total=5.0)
        assert result.success is True
        assert store.main_records["r-1"].delivery_state == "delivered"
    finally:
        server.shutdown()


def test_send_success_path_binary_garbage_ack_failed_no_exception():
    """红能力：2xx + 纯二进制垃圾体——旧 UnicodeDecodeError 逃逸；
    修复后落 ACK 不合格（pending + backoff），不冒异常。"""
    server = _serve(200, b"\xff\xfe\x00\x01raw-bytes")
    try:
        store = FakeDeliveryStore()
        store.upsert_main(_record(
            route_ref=f"http://127.0.0.1:{server.server_port}/callback"))
        result = send_callback(store, "r-1", timeout_total=5.0)
        assert result.success is False
        assert result.error == "ACK failed"
        assert store.main_records["r-1"].delivery_state == "pending"
    finally:
        server.shutdown()


# ---------- 条目45（F-1）：发送前 deadline + expires_at 双查 fail-closed ----------

def test_send_callback_deadline_passed_refuses_and_marks_expired():
    """红能力：delivering 但 delivery_deadline_at 已过——旧照发（契约硬冲突：
    10§6.340 发送前三查）；修复后不发 + 记 expired（11§4.3 到期处置）。"""
    server = _serve(200, b'{"succeed": true}')
    try:
        store = FakeDeliveryStore()
        store.upsert_main(_record(
            delivery_deadline_at=(
                datetime.datetime.now(UTC) - datetime.timedelta(hours=1)
            ).isoformat(),
            route_ref=f"http://127.0.0.1:{server.server_port}/callback"))
        result = send_callback(store, "r-1", timeout_total=5.0)
        assert result.success is False
        assert "deadline" in (result.error or "").lower()
        assert store.main_records["r-1"].delivery_state == "expired"
        # W-R3b（R3-M7-a）：对拍实际发送头捕获面——旧 "Content-Type" not in
        # 恒真（handler 键一律小写归一，Title-Case 查询永不命中）；捕获面
        # 为空才证明未发出请求（任何到达的 POST 必落小写键捕获，:54-55）。
        assert server.captured_headers == {}                     # 未发出请求
    finally:
        server.shutdown()


def test_send_callback_expires_at_passed_refuses_and_marks_expired():
    """红能力：expires_at 已过（deadline 未过）——10§6.340 双查缺一不可。"""
    server = _serve(200, b'{"succeed": true}')
    try:
        store = FakeDeliveryStore()
        store.upsert_main(_record(
            expires_at=(
                datetime.datetime.now(UTC) - datetime.timedelta(minutes=5)
            ).isoformat(),
            route_ref=f"http://127.0.0.1:{server.server_port}/callback"))
        result = send_callback(store, "r-1", timeout_total=5.0)
        assert result.success is False
        assert "expires_at" in (result.error or "").lower()
        assert store.main_records["r-1"].delivery_state == "expired"
        assert server.captured_headers == {}   # 未发出请求（对拍捕获面，同上）
    finally:
        server.shutdown()


def test_send_callback_live_record_still_sends_pinned():
    """绿守卫：未过期 delivering 照常发送（双闸不拦合法路径）。"""
    server = _serve(200, b'{"succeed": true}')
    try:
        store = FakeDeliveryStore()
        store.upsert_main(_record(
            route_ref=f"http://127.0.0.1:{server.server_port}/callback"))
        result = send_callback(store, "r-1", timeout_total=5.0)
        assert result.success is True
        assert store.main_records["r-1"].delivery_state == "delivered"
    finally:
        server.shutdown()


# ---------- 条目83（F-ν2）：Idempotency-Key / X-Trace-ID 双头 ----------

def test_send_callback_preserves_idempotency_and_trace_headers():
    """红能力：冻结 callback_body 含 requestId/traceId——旧仅 Content-Type
    （13§1③/P06§5 明文保留双头）；修复后 Idempotency-Key=requestId +
    X-Trace-ID=traceId。"""
    server = _serve(200, b'{"succeed": true}')
    try:
        body = json.dumps({"requestId": "1098406-3", "traceId": "trace-xyz",
                           "success": True, "resultJson": {}},
                          ensure_ascii=False)
        store = FakeDeliveryStore()
        store.upsert_main(_record(
            callback_body=body,
            route_ref=f"http://127.0.0.1:{server.server_port}/callback"))
        result = send_callback(store, "r-1", timeout_total=5.0)
        assert result.success is True
        assert server.captured_headers.get("idempotency-key") == "1098406-3"
        assert server.captured_headers.get("x-trace-id") == "trace-xyz"
        assert server.captured_headers.get("content-type") == "application/json"
    finally:
        server.shutdown()


def test_send_callback_without_frozen_identity_omits_headers_safely():
    """绿守卫（fail-safe）：体无既有身份 → 不发明头，发送行为逐字节不变。"""
    server = _serve(200, b'{"succeed": true}')
    try:
        store = FakeDeliveryStore()
        store.upsert_main(_record(
            callback_body='{"item_id":"item-A"}',
            route_ref=f"http://127.0.0.1:{server.server_port}/callback"))
        result = send_callback(store, "r-1", timeout_total=5.0)
        assert result.success is True
        assert "idempotency-key" not in server.captured_headers
        assert "x-trace-id" not in server.captured_headers
    finally:
        server.shutdown()
