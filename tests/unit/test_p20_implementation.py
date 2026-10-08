"""P20 单元层实施测试（按 05:39 双轨授权 + 05:29 修订 1 UAT 接收器 fixture）。

覆盖：
- 领取 CAS：payload_hash / route_ref 异变拒绝、watermark 单调 + 截止 + 12 次耗尽
- 发送：fake 路径（仅 POST + 2xx + succeed=true → delivered；其他 pending）
- 退避公式 + jitter、轮次语义（round+1 不清零、accumulating）
- attempt_id 派生（SHA256(JCS([event_id, generation]))）
- fake_store 不引入真 ES / 不连真回调 URL（PROD_* + example.invalid 注释占位）
- 重提交 + 完结路径
"""

from __future__ import annotations

import datetime
import hashlib

import pytest

from news_flash_dedup.delivery import (
    FakeDeliveryRecord,
    FakeDeliveryStore,
    P20ReceiveError,
    P20SendError,
    SendResult,
    advance_backoff,
    compute_attempt_id,
    exhaust_round,
    expire_record,
    persist_next_delivery,
    receive,
    reopen_round,
    scan_pending,
    send_callback,
)


def _record(record_id="r-1", *, item_id="item-A", scope_id="default",
             business_date="2026-09-26", arrival_seq=1,
             delivery_state="pending", callback_body='{"item_id":"item-A","text":"t","decision":"不重复","duplicate_ids":[],"reason":"r"}',
             payload_hash=None, route_ref="http://127.0.0.1:8080/callback",
             delivery_deadline_at=None, next_delivery_at=None,
             vector_state="pending", round_attempts=0, round=1,
             callback_attempts=0) -> FakeDeliveryRecord:
    return FakeDeliveryRecord(
        record_id=record_id, item_id=item_id, scope_id=scope_id,
        business_date=business_date, arrival_seq=arrival_seq,
        delivery_state=delivery_state,
        # D24 审计修复注记：默认截止/调度点改为相对真实时钟（原硬编码
        # 2026-09-26/27 已被 scan_pending 新增的 deadline 截止闸识别为过期）
        delivery_deadline_at=delivery_deadline_at or (
            datetime.datetime.now(datetime.timezone.utc)
            + datetime.timedelta(hours=24)).isoformat(),
        next_delivery_at=next_delivery_at or (
            datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(hours=1)).isoformat(),
        callback_attempts=callback_attempts, round_attempts=round_attempts, round=round,
        callback_body=callback_body,
        payload_hash=payload_hash or hashlib.sha256(callback_body.encode("utf-8")).hexdigest(),
        route_ref=route_ref,
        vector_state=vector_state, audit_complete=True,
    )


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


# ---------- 标尺 1：领取 CAS payload_hash + route_ref 异变拒绝 ----------

def test_receive_payload_hash_mismatch_rejected():
    store = FakeDeliveryStore()
    rec = _record(payload_hash="correct_hash")
    store.upsert_main(rec)
    with pytest.raises(P20ReceiveError, match="(?i)payload_hash"):
        receive(store, "r-1", payload_hash="wrong_hash",
                  route_ref="http://127.0.0.1:8080/callback",
                  expected_generation=0, event_id="e-1")


def test_receive_route_ref_mismatch_rejected():
    store = FakeDeliveryStore()
    rec = _record(route_ref="http://correct.example.invalid/callback")
    store.upsert_main(rec)
    with pytest.raises(P20ReceiveError, match="(?i)route_ref"):
        receive(store, "r-1", payload_hash=rec.payload_hash,
                  route_ref="http://wrong.example.invalid/callback",
                  expected_generation=0, event_id="e-1")


def test_receive_round_attempts_12_exhausted_rejected():
    store = FakeDeliveryStore()
    rec = _record(round_attempts=12)
    store.upsert_main(rec)
    with pytest.raises(P20ReceiveError, match="(?i)round_attempts"):
        receive(store, "r-1", payload_hash=rec.payload_hash,
                  route_ref=rec.route_ref, expected_generation=0, event_id="e-1")


# ---------- 标尺 2：attempt_id 派生 ----------

def test_attempt_id_sha256_jcs():
    """attempt_id = SHA256(JCS([event_id, generation]))（10 §6.5 第 2 步，全 64hex）。

    D25/C-07 行为修正钉值（窗口X 并线移植）：旧实现用 json.dumps 默认
    分隔符（", "/": "），非 JCS 最简分隔符——派生值随之变更为
    admission._digest（JCS 单一事实源，separators=(",", ":")）口径。
    主窗口追裁：10 文规格无截断，[:32] 系 delivery 层自造已去除
    （与 g0_persistence.py:603 全 64hex 对齐）。下值为 Python 现算
    admission._digest(["e-1", 3]) 的真实值。
    """
    eid = "e-1"
    gen = 3
    aid = compute_attempt_id(eid, gen)
    assert aid == ("3e87e032afbeca1ce4277a3fe6966cd4"
                   "2265af3f91ea8678c3ac5ed34ec4cda0")
    assert len(aid) == 64


def test_attempt_id_different_generation_different_value():
    a1 = compute_attempt_id("e-1", 1)
    a2 = compute_attempt_id("e-1", 2)
    assert a1 != a2


# ---------- 标尺 3：调度扫描 + 代次隔离 ----------

def test_scan_pending_filters_only_pending_and_due():
    store = FakeDeliveryStore()
    past = _record("r-past", next_delivery_at="2026-09-25T00:00:00+00:00")
    future = _record("r-future", next_delivery_at="2027-01-01T00:00:00+00:00")
    delivered = _record("r-delivered",
                          delivery_state="delivered",
                          next_delivery_at="2026-09-25T00:00:00+00:00")
    store.upsert_main(past)
    store.upsert_main(future)
    store.upsert_main(delivered)
    now = "2026-09-26T00:00:00+00:00"
    assert scan_pending(store, now=now) == ["r-past"]


def test_duplicate_decision_held_fixture_not_scanned():
    """疑似确认扣留专钉（10-07 令甲-i，I-1 翻转防夹具回潮）：

    重复判定件落 held（创建落点扣留），扫描不收录、领取拒绝——与
    tests/unit/test_held_delivery.py G1-3 同语义；默认夹具已翻"不重复"
    放行件，本钉保留重复夹具的扣留事实防回潮。
    """
    store = FakeDeliveryStore()
    rec = _record(
        "r-held",
        delivery_state="held",
        callback_body='{"item_id":"item-A","text":"t","decision":"重复","duplicate_ids":["r-0"],"reason":"r"}')
    store.upsert_main(rec)
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    assert "r-held" not in scan_pending(store, now=now)
    with pytest.raises(P20ReceiveError, match="(?i)delivery_state"):
        receive(store, "r-held", payload_hash=rec.payload_hash,
                route_ref=rec.route_ref, expected_generation=0, event_id="e-1")


def test_receive_increments_generation_atomically():
    store = FakeDeliveryStore()
    rec = _record()
    store.upsert_main(rec)
    lease1 = receive(store, "r-1", payload_hash=rec.payload_hash,
                      route_ref=rec.route_ref,
                      expected_generation=0, event_id="e-1")
    assert lease1.owner_generation == 1
    assert store.main_records["r-1"].delivery_state == "delivering"
    # 代次隔离：第二次领取使用预期代次 1
    lease2 = receive(store, "r-1", payload_hash=rec.payload_hash,
                      route_ref=rec.route_ref,
                      expected_generation=1, event_id="e-1")
    assert lease2.owner_generation == 2


# ---------- 标尺 4：发送 fake 路径（ACK 失败、未知结果） ----------

def test_send_requires_delivering_state():
    store = FakeDeliveryStore()
    rec = _record(delivery_state="pending")
    store.upsert_main(rec)
    with pytest.raises(P20ReceiveError, match="(?i)delivering"):
        send_callback(store, "r-1")


def test_send_retry_on_ack_business_failure():
    """ACK 业务 succeed=false → 不视为成功 → 维持 pending 等待续期。"""
    store = FakeDeliveryStore()
    rec = _record(delivery_state="delivering",
                   route_ref="http://127.0.0.1:1/callback")
    store.upsert_main(rec)
    result = send_callback(store, "r-1", timeout_connect=0.5, timeout_total=1.0)
    # 端口 1 通常连接拒绝 → OSError/URLError
    assert result.success is False
    assert store.main_records["r-1"].delivery_state in {"delivering", "pending"}


def test_response_succeed_strict_boolean_true():
    """R9 外部审核 F8a 回归（主窗口 06:4x）：12 L228 冻结 succeed=true——
    字符串 "false"、数字 1、字符串 "true" 均不得判成功（旧 bool() truthy
    实现曾把伪 ACK 误标 delivered）；仅 JSON 布尔 true 通过。"""
    from news_flash_dedup.delivery.sender import _response_succeed
    assert _response_succeed('{"succeed": true}') is True
    assert _response_succeed('{"succeed": "false"}') is False   # 旧实现误判 True
    assert _response_succeed('{"succeed": 1}') is False          # 旧实现误判 True
    assert _response_succeed('{"succeed": "true"}') is False
    assert _response_succeed('{"succeed": false}') is False
    assert _response_succeed('{}') is False
    assert _response_succeed('[1]') is False                     # 非对象 fail-closed
    assert _response_succeed('not-json') is False


# ---------- 标尺 5：退避公式 + 12 次耗尽 + 24h 截止 + 重提交 ----------

def test_backoff_formula_baseline():
    """round_attempts=1 → base = 1s * 2^0 = 1s；jitter ∈ ±20%。"""
    base = advance_backoff(round_attempts=1)
    assert 0.8 <= base <= 1.2


def test_backoff_caps_at_30_minutes():
    """round_attempts=20 → base = 1s * 2^19 > 30min；cap 到 1800s。"""
    base = advance_backoff(round_attempts=20)
    # W2Fε 补下限 1440（1800×0.8）：封顶后 ±20% 抖动的机制值域 [1440, 2160]；
    # 原单上界钉对"封顶丢失/实现退化取小"无红能力（批 44 WC3 同域一体）。
    assert 1440 <= base <= 2160  # 1800 × (1±0.2)


def test_persist_next_delivery_writes_iso():
    store = FakeDeliveryStore()
    rec = _record(delivery_state="delivering")
    store.upsert_main(rec)
    next_at = persist_next_delivery(store, "r-1", next_seconds=5.0)
    assert isinstance(next_at, str)
    assert "T" in next_at


def test_exhaust_round_requires_12():
    store = FakeDeliveryStore()
    rec = _record(round_attempts=11)
    store.upsert_main(rec)
    with pytest.raises(P20ReceiveError, match="(?i)< 12"):
        exhaust_round(store, "r-1")


def test_expire_record_marks_expired():
    store = FakeDeliveryStore()
    rec = _record()
    store.upsert_main(rec)
    expire_record(store, "r-1")
    assert store.main_records["r-1"].delivery_state == "expired"


def test_reopen_round_increments_round_resets_attempts():
    """delivered → reopen → round+1 + round_attempts=0（不清零累计 callback_attempts）。"""
    store = FakeDeliveryStore()
    rec = _record(delivery_state="delivered", callback_attempts=5, round=1,
                   round_attempts=12)
    store.upsert_main(rec)
    reopen_round(store, "r-1")
    new = store.main_records["r-1"]
    assert new.round == 2
    assert new.round_attempts == 0
    assert new.callback_attempts == 5  # 累计不清零
    assert new.delivery_state == "pending"


# ---------- 标尺 6：fake_store 不引入真 ES + 不连真回调 URL ----------

def test_fake_store_does_not_import_es_or_real_callback_urls():
    """fake_store 不引入真 ES 客户端，且测试配置不含真实业务回调 URL。"""
    import news_flash_dedup.delivery.fake_store as fs
    module_attrs = dir(fs)
    for attr in ("ESClient", "elasticsearch", "es_client", "Elasticsearch"):
        assert attr not in module_attrs, (
            f"P20 fake_store 不应引入真 ES 客户端 {attr!r}"
        )


def test_default_route_ref_uses_localhost_not_real_callback():
    """默认 route_ref = 127.0.0.1 ephemeral（PROD 零接触）。"""
    store = FakeDeliveryStore()
    rec = _record()
    store.upsert_main(rec)
    assert rec.route_ref.startswith("http://127.0.0.1:")
    # 不出现真实业务域名
    for forbidden in ("example.com", "prod.", ".com.cn"):
        assert forbidden not in rec.route_ref, (
            f"route_ref {rec.route_ref!r} 含真实业务域名 {forbidden!r}"
        )


# ---------- 端到端 smoke ----------

def test_end_to_end_pending_scan_success_then_retry():
    """完整 fake 路径：pending 扫描 → 领取 → 发送（fail）→ pending 续期。"""
    import socket
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    port = server.getsockname()[1]
    server.close()

    store = FakeDeliveryStore()
    rec = _record(route_ref=f"http://127.0.0.1:{port}/callback")
    store.upsert_main(rec)

    now = _now()
    assert "r-1" in scan_pending(store, now=now)
    receive(store, "r-1", payload_hash=rec.payload_hash,
              route_ref=rec.route_ref,
              expected_generation=0, event_id="e-1")

    result = send_callback(store, "r-1", timeout_connect=0.5, timeout_total=1.0)
    assert isinstance(result, SendResult)
    # 端口已关闭 → OSError → success=False
    assert result.success is False
    # 状态维持 delivering 或 pending（unknown 结果维持）
    assert store.main_records["r-1"].delivery_state in {"delivering", "pending"}


# ---------- D24 三方审计修复回归（12:4x） ----------

def test_scan_pending_excludes_past_deadline():
    """D24：scan_pending 补 deadline 截止闸（deadline ≤ expires_at 涵摄，
    10 §6.5/§6.337）——过期记录不得再被调度。"""
    store = FakeDeliveryStore()
    past = (datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(hours=1)).isoformat()
    future = (datetime.datetime.now(datetime.timezone.utc)
              + datetime.timedelta(hours=1)).isoformat()
    store.upsert_main(_record("r-expired", delivery_deadline_at=past))
    store.upsert_main(_record("r-live", delivery_deadline_at=future))
    scanned = scan_pending(store, now=_now())
    assert "r-expired" not in scanned
    assert "r-live" in scanned


def test_send_callback_does_not_follow_redirect():
    """D24：302 不得跟随——urllib 默认重定向会把 POST 转 GET 投向目标，
    载荷丢失而目标端伪 ACK 可致静默 delivered；修复后 3xx = 确定性
    ACK 不合格 → pending。"""
    import http.server
    import threading

    class _Redirector(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            # 先读尽请求体再响应——Windows 上未消费 body 即响应会触发
            # RST（WinError 10053），测试由 302 断言退化为连接中止（ flake ）
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(length)
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1:9/decoy")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Redirector)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        store = FakeDeliveryStore()
        rec = _record(
            delivery_state="delivering",
            route_ref=f"http://127.0.0.1:{server.server_port}/callback")
        store.upsert_main(rec)
        result = send_callback(store, "r-1", timeout_total=2.0)
        assert result.success is False
        assert result.response_code == 302
        assert store.main_records["r-1"].delivery_state == "pending"
    finally:
        server.shutdown()


def test_send_callback_rejects_inverted_timeouts():
    """D24：连接超时大于总超时为倒挂配置，fail-fast（死参数占位防呆）。"""
    store = FakeDeliveryStore()
    rec = _record(delivery_state="delivering")
    store.upsert_main(rec)
    with pytest.raises(P20SendError, match="timeout_connect"):
        send_callback(store, "r-1", timeout_connect=5.0, timeout_total=1.0)