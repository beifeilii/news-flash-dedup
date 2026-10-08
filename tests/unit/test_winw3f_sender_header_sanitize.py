# -*- coding: utf-8 -*-
"""W3F 红测（W3d-F-NEW-2）：回调双头值 sanitize（省略向 fail-safe）。

缺陷面（delivery/sender.py:192-198 现役）：Idempotency-Key=requestId /
X-Trace-ID=traceId 头值取自冻结 callback_body——traceId 合同未限字符集
（contract.py:291 `_string` 仅长度 1..256），非 latin-1 值发送期
UnicodeEncodeError（http.client putheader latin-1 编码）、含 CR/LF 值
ValueError（http.client 头校验）——两者均**逃逸** send_callback 捕获面
（:205-246 仅 HTTPError/URLError/TimeoutError/OSError），fail-safe
"发送语义逐字节不变"对该形态不成立（修复前无此头、可正常发送）。

修法（省略向）：`_latin1_safe_header_value` 闸——非法头值（非 latin-1
可编码 / 含 \x00-\x08·\x0a-\x1f·\x7f 控制码）省略该头不发送；合法值
逐字节不变通过。边界声明：逐字节不变仅对头实际发送形态成立——非法
头值形态下该头省略（与该头从不存在逐字节同一），体与其余头不变。
"""

from __future__ import annotations

import inspect
import json

from news_flash_dedup.delivery import sender as sender_module
from news_flash_dedup.delivery.fake_store import FakeDeliveryStore

from tests.unit.test_winw2fc_sender import _record, _serve


def _send_with_body(body: str, server):
    store = FakeDeliveryStore()
    store.upsert_main(_record(
        callback_body=body,
        route_ref=f"http://127.0.0.1:{server.server_port}/callback"))
    return sender_module.send_callback(store, "r-1", timeout_total=5.0), store


# ---------- 红测（修复前非法头值异常逃逸捕获面） ----------

def test_new2_non_latin1_trace_id_omitted_send_survives():
    """红能力：非 latin-1 traceId（"追踪-α-①"）——修复前 UnicodeEncodeError
    逃逸 send_callback 捕获面（记录滞留 delivering、发送中断）；修复后
    该头省略、发送照常 delivered、合法 requestId 头不受影响。"""
    server = _serve(200, b'{"succeed": true}')
    try:
        body = json.dumps({"requestId": "1098406-3",
                           "traceId": "追踪-α-①",
                           "success": True, "resultJson": {}},
                          ensure_ascii=False)
        result, store = _send_with_body(body, server)
        assert result.success is True
        assert store.main_records["r-1"].delivery_state == "delivered"
        assert "x-trace-id" not in server.captured_headers      # 省略不发送
        assert server.captured_headers.get("idempotency-key") == "1098406-3"
        assert server.captured_headers.get("content-type") == "application/json"
    finally:
        server.shutdown()


def test_new2_crlf_trace_id_omitted_no_header_injection():
    """红能力：含 CR/LF traceId（"abc\\r\\nX-Injected: evil"）——修复前
    ValueError 逃逸捕获面；修复后该头省略、发送照常、无注入头落网。"""
    server = _serve(200, b'{"succeed": true}')
    try:
        body = json.dumps({"requestId": "1098406-3",
                           "traceId": "abc\r\nX-Injected: evil",
                           "success": True, "resultJson": {}},
                          ensure_ascii=False)
        result, store = _send_with_body(body, server)
        assert result.success is True
        assert "x-trace-id" not in server.captured_headers
        assert "x-injected" not in server.captured_headers      # 注入零落网
        assert server.captured_headers.get("idempotency-key") == "1098406-3"
    finally:
        server.shutdown()


def test_new2_non_latin1_request_id_omitted_independently():
    """红能力：非 latin-1 requestId——独立闸（合法 traceId 头照发）。"""
    server = _serve(200, b'{"succeed": true}')
    try:
        body = json.dumps({"requestId": "请求-1098406-3",
                           "traceId": "trace-xyz",
                           "success": True, "resultJson": {}},
                          ensure_ascii=False)
        result, store = _send_with_body(body, server)
        assert result.success is True
        assert "idempotency-key" not in server.captured_headers
        assert server.captured_headers.get("x-trace-id") == "trace-xyz"
    finally:
        server.shutdown()


def test_new2_boundary_declaration_comment_present():
    """机制钉（注释面）：send_callback 携带"发送语义逐字节不变"的边界
    声明——非法头值形态下该头省略（与该头从不存在逐字节同一）。"""
    src = inspect.getsource(sender_module.send_callback)
    assert "非法头值形态下该头省略" in src, \
        "省略向边界声明注释未落地（fail-safe 逐字节不变对该形态的边界不明）"


# ---------- 前后均绿守卫（合法值逐字节不变） ----------

def test_new2_latin1_legal_values_pass_byte_identical():
    """守卫（前后同绿）：合法 latin-1 双头值（含 é/ü 等 latin-1 字符）
    逐字节不变通过——sanitize 对合法形态零改写。"""
    server = _serve(200, b'{"succeed": true}')
    try:
        body = json.dumps({"requestId": "1098406-3-é",
                           "traceId": "trace-xyz-ü-ÿ",
                           "success": True, "resultJson": {}},
                          ensure_ascii=False)
        result, _store = _send_with_body(body, server)
        assert result.success is True
        assert server.captured_headers.get("idempotency-key") == "1098406-3-é"
        assert server.captured_headers.get("x-trace-id") == "trace-xyz-ü-ÿ"
    finally:
        server.shutdown()


def test_new2_no_identity_headers_still_omitted():
    """守卫（前后同绿）：体无既有身份 → 不发明头（F-ν2 现役 fail-safe）。"""
    server = _serve(200, b'{"succeed": true}')
    try:
        result, _store = _send_with_body('{"item_id":"item-A"}', server)
        assert result.success is True
        assert "idempotency-key" not in server.captured_headers
        assert "x-trace-id" not in server.captured_headers
    finally:
        server.shutdown()
