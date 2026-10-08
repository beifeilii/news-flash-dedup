"""P2 工程债 C-07/C-09/C-12 硬化测试（第三任收尾批次）。

规格出处：
- C-07：10 文 §6.5 第 2 步 attempt_id = SHA256(JCS([event_id, generation]))
  （全 64hex，主窗口追裁：规格原文无截断，[:32] 系 delivery 层自造已去除），
  JCS 单一事实源 = admission._jcs/_digest（separators=(",", ":") 最简分隔符）。
- C-09：g0_persistence.py 真层纪律（_delivery L526-548 / _time / claim_delivery
  L585-615 / recover_delivery L617-631）。
- C-12：回调/LLM 响应体 1MB 硬上限（fail-closed，超长抛错不静默截断）。

红能力说明（全部关键断言针对新行为，旧代码必红）：
- C-07：旧 compute_attempt_id 用 json.dumps 默认分隔符（", "/": "）——
  单源等价断言与"旧值作废"断言双红；
- C-09：旧 update_lease 零校验不拒非法租约 → pytest.raises 全红；旧 receive
  无 deadline/expires_at/backoff 闸 → pytest.raises 全红；
- C-12：旧 resp.read() 无界不抛 P20SendError → pytest.raises 红；旧 llm
  无上限分支（超限体落 JSONDecodeError，非 LlmExtractionError）→ 红。
默认兼容：now 缺省（""）时 receive 现状零校验逐字节保持（兼容守卫测试钉住）。
【窗口Z2 注：本行系 C-09 旧兼容形态记述，已被 H-05 主窗口追裁废止——
now 缺省 → 以当前时执法（三闸永远生效），见下方 L189 测试同步注记。】

窗口X 并线适配（移植自主仓外 P2 副本）：本文件零适配——所触接口
（delivery receive/send_callback/update_lease、facts.llm._default_call_fn）
主仓签名与副本同一，直接移植。
"""

from __future__ import annotations

import datetime
import hashlib
import http.server
import json
import threading

import pytest

from news_flash_dedup.delivery import (
    FakeDeliveryRecord,
    FakeDeliveryStore,
    P20ReceiveError,
    P20SendError,
    compute_attempt_id,
    receive,
    send_callback,
)
from news_flash_dedup.delivery.fake_store import FakeDeliveryLease
from news_flash_dedup.facts.llm import LlmExtractionError, _default_call_fn

UTC = datetime.timezone.utc
OVER_CAP = 1024 * 1024 + 100                      # 超 1MB 上限的响应体尺寸


def _iso(dt: datetime.datetime) -> str:
    return dt.isoformat()


def _record(record_id="r-1", *, delivery_state="pending",
            delivery_deadline_at=None, next_delivery_at=None,
            expires_at="", route_ref="http://127.0.0.1:8080/callback",
            round_attempts=0) -> FakeDeliveryRecord:
    # 10-07 令甲-i（I-2 翻转）：默认夹具翻"不重复"放行件——本文件断言语义
    # （闸/上限/代次）与 decision 无耦合，翻转后断言照绿。
    callback_body = '{"item_id":"item-A","text":"t","decision":"不重复"}'
    return FakeDeliveryRecord(
        record_id=record_id, item_id="item-A", scope_id="default",
        business_date="2026-09-26", arrival_seq=1,
        delivery_state=delivery_state,
        delivery_deadline_at=delivery_deadline_at or _iso(
            datetime.datetime.now(UTC) + datetime.timedelta(hours=24)),
        next_delivery_at=next_delivery_at or _iso(
            datetime.datetime.now(UTC) - datetime.timedelta(hours=1)),
        callback_attempts=0, round_attempts=round_attempts, round=1,
        callback_body=callback_body,
        payload_hash=hashlib.sha256(callback_body.encode("utf-8")).hexdigest(),
        route_ref=route_ref,
        vector_state="pending", audit_complete=True,
        expires_at=expires_at,
    )


# ---------- C-07：attempt_id 单源化（10 §6.5 第 2 步） ----------

def test_attempt_id_uses_admission_jcs_single_source():
    """红能力：旧实现 == 旧默认分隔符值 → 两条断言双红。"""
    aid = compute_attempt_id("e-1", 3)
    # 冻结字面钉（W-R3b / R3-M7-c3，w-r3b-probes.json 实算）：原断言
    # aid == _admission._digest(["e-1", 3]) 系 f(x)==f(x) 自比——
    # compute_attempt_id 本体即 _admission._digest 调用（coordinator.py:89），
    # JCS 分隔符漂移时双侧同步漂移永不红；冻结字面对派生链任何漂移咬。
    # （全 64hex，10 文规格无截断——主窗口追裁去 [:32]，与 g0:603 对齐）
    assert aid == ("3e87e032afbeca1ce4277a3fe6966cd42265af3f91ea8678c3ac5ed"
                   "34ec4cda0")
    # D25/C-07 行为修正：旧 json.dumps 默认分隔符（", "/": "）派生值作废
    old_style = hashlib.sha256(
        json.dumps(["e-1", 3], sort_keys=True).encode("utf-8")
    ).hexdigest()[:32]
    assert aid != old_style
    assert len(aid) == 64


# ---------- C-09：fake 租约前置收紧（对齐 g0 真层纪律） ----------

def _valid_lease(**over) -> FakeDeliveryLease:
    base = dict(
        owner_id="worker-1", owner_generation=1,
        lease_until=_iso(datetime.datetime.now(UTC)
                         + datetime.timedelta(minutes=5)),
        attempt_id=compute_attempt_id("e-1", 1),
        round=1, result_version=1, event_id="e-1",
        payload_hash="p", route_ref="http://127.0.0.1:8080/callback",
    )
    base.update(over)
    return FakeDeliveryLease(**base)


@pytest.mark.parametrize("field,bad", [
    ("owner_id", ""),                            # 真层 L544：owner 必须非空 str
    ("owner_id", None),
    ("owner_generation", 0),                     # 真层 L545：活跃租约代次非零
    ("owner_generation", -1),
    ("owner_generation", 1.5),                   # 真层 L526：type 必须 int
    ("attempt_id", ""),                          # 真层 L537-540：派生摘要非空
    ("attempt_id", None),
    ("lease_until", "not-an-iso"),               # 合法 ISO（aware）单源校验
    ("lease_until", "2026-09-27T00:00:00"),      # naive 拒（无法判定瞬间）
    ("lease_until", ""),
])
def test_update_lease_rejects_illegal_lease(field, bad):
    """红能力：旧 update_lease 零校验直写 → pytest.raises 全红。"""
    store = FakeDeliveryStore()
    with pytest.raises(ValueError, match="(?i)lease"):
        store.update_lease(_valid_lease(**{field: bad}))
    assert store.lease_index == {}               # 拒绝不落写
    assert store.cas_calls == []                 # 拒绝不留 CAS 痕


def test_update_lease_accepts_valid_lease():
    """绿守卫：合法租约照常落写（现状行为兼容）。"""
    store = FakeDeliveryStore()
    lease = _valid_lease()
    assert store.update_lease(lease) == "worker-1"
    assert store.lease_index["worker-1"] == lease
    assert store.cas_calls[-1]["action"] == "update_lease"


def test_receive_rejects_past_deadline_when_now_provided():
    """对照真层 claim_delivery L594（now >= deadline → 拒）补齐 fake 缺漏。"""
    store = FakeDeliveryStore()
    past = _iso(datetime.datetime.now(UTC) - datetime.timedelta(hours=1))
    rec = _record(delivery_deadline_at=past, next_delivery_at=past)
    store.upsert_main(rec)
    with pytest.raises(P20ReceiveError, match="(?i)deadline"):
        receive(store, "r-1", payload_hash=rec.payload_hash,
                route_ref=rec.route_ref, expected_generation=0,
                event_id="e-1", now=_iso(datetime.datetime.now(UTC)))


def test_receive_rejects_past_expires_at_when_now_provided():
    """10 §6.340：领取前 now < expires_at（字段非空时）——C-08 字段的领取闸。"""
    store = FakeDeliveryStore()
    past = _iso(datetime.datetime.now(UTC) - datetime.timedelta(hours=1))
    future = _iso(datetime.datetime.now(UTC) + datetime.timedelta(hours=48))
    rec = _record(delivery_deadline_at=future, next_delivery_at=past,
                  expires_at=past)
    store.upsert_main(rec)
    with pytest.raises(P20ReceiveError, match="(?i)expires_at"):
        receive(store, "r-1", payload_hash=rec.payload_hash,
                route_ref=rec.route_ref, expected_generation=0,
                event_id="e-1", now=_iso(datetime.datetime.now(UTC)))


def test_receive_rejects_backoff_not_elapsed_when_now_provided():
    """对照真层 claim_delivery L597（now < next_delivery_at → 拒）。"""
    store = FakeDeliveryStore()
    future = _iso(datetime.datetime.now(UTC) + datetime.timedelta(hours=1))
    rec = _record(next_delivery_at=future)
    store.upsert_main(rec)
    with pytest.raises(P20ReceiveError, match="(?i)backoff|next_delivery_at"):
        receive(store, "r-1", payload_hash=rec.payload_hash,
                route_ref=rec.route_ref, expected_generation=0,
                event_id="e-1", now=_iso(datetime.datetime.now(UTC)))


def test_receive_with_now_accepts_healthy_record():
    """绿守卫：三闸不误伤健康记录（now 显式提供仍照常领取）。"""
    store = FakeDeliveryStore()
    rec = _record()
    store.upsert_main(rec)
    lease = receive(store, "r-1", payload_hash=rec.payload_hash,
                    route_ref=rec.route_ref, expected_generation=0,
                    event_id="e-1", now=_iso(datetime.datetime.now(UTC)))
    assert lease.owner_generation == 1
    assert store.main_records["r-1"].delivery_state == "delivering"


def test_receive_without_now_enforces_gates_with_current_time():
    """窗口Z2（H-05 主窗口追裁解禁）：now 缺省（""）→ 以当前时执法——
    deadline/expires_at/backoff 三闸永远生效；旧"缺省零校验、过期记录
    仍可领取"兼容形态（本测试原钉）自此废止：过期记录缺省调用即拒。
    """
    store = FakeDeliveryStore()
    past = _iso(datetime.datetime.now(UTC) - datetime.timedelta(hours=1))
    rec = _record(delivery_deadline_at=past, next_delivery_at=past)
    store.upsert_main(rec)
    with pytest.raises(P20ReceiveError, match="(?i)deadline"):
        receive(store, "r-1", payload_hash=rec.payload_hash,
                route_ref=rec.route_ref, expected_generation=0,
                event_id="e-1")
    assert store.main_records["r-1"].delivery_state == "pending"   # 拒领零副作用
    assert store.lease_index == {}


# ---------- C-12：响应体 1MB 硬上限（fail-closed） ----------

class _PayloadHandler(http.server.BaseHTTPRequestHandler):
    """按 server 属性回配置载荷；先读尽请求体（Windows RST flake 规避）。"""

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
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
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_send_callback_response_body_over_1mb_fails_closed():
    """红能力：旧无界 read 不抛错（超限体落 ACK 失败分支）→ raises 红。

    语义：2xx 但响应体超限 → ACK 不可判定 = 未知结果 → 维持 delivering
    待续期（不重发不重计），P20SendError 冒出上送。
    """
    server = _serve(200, b'{"succeed": true}' + b" " * OVER_CAP)
    try:
        store = FakeDeliveryStore()
        rec = _record(
            delivery_state="delivering",
            route_ref=f"http://127.0.0.1:{server.server_port}/callback")
        store.upsert_main(rec)
        with pytest.raises(P20SendError, match="(?i)exceeds.*cap"):
            send_callback(store, "r-1", timeout_total=5.0)
        assert store.main_records["r-1"].delivery_state == "delivering"
    finally:
        server.shutdown()


def test_send_callback_error_body_over_1mb_state_pending_then_raise():
    """非 2xx + 诊断体超限：ACK 不合格由 status 确定性判定——先落 pending
    再抛 P20SendError（不静默截断，接收器异常不掩盖）。"""
    server = _serve(500, b"x" * OVER_CAP)
    try:
        store = FakeDeliveryStore()
        rec = _record(
            delivery_state="delivering",
            route_ref=f"http://127.0.0.1:{server.server_port}/callback")
        store.upsert_main(rec)
        with pytest.raises(P20SendError, match="(?i)exceeds.*cap"):
            send_callback(store, "r-1", timeout_total=5.0)
        assert store.main_records["r-1"].delivery_state == "pending"
    finally:
        server.shutdown()


def test_send_callback_body_within_cap_unaffected():
    """绿守卫：1MB 内正常 ACK 路径逐字节保持（2xx + succeed=true → delivered）。"""
    server = _serve(200, b'{"succeed": true}')
    try:
        store = FakeDeliveryStore()
        rec = _record(
            delivery_state="delivering",
            route_ref=f"http://127.0.0.1:{server.server_port}/callback")
        store.upsert_main(rec)
        result = send_callback(store, "r-1", timeout_total=5.0)
        assert result.success is True
        assert store.main_records["r-1"].delivery_state == "delivered"
    finally:
        server.shutdown()


def test_llm_response_body_over_1mb_fails_closed():
    """红能力：旧无界 read → 超限体落 JSONDecodeError（非 LlmExtractionError）
    → pytest.raises(LlmExtractionError) 红。上限内 fail-closed 不重试。"""
    server = _serve(200, b'{"choices": ' + b" " * OVER_CAP)
    try:
        with pytest.raises(LlmExtractionError, match="1MB"):
            _default_call_fn(
                model="m", base_url=f"http://127.0.0.1:{server.server_port}",
                api_key="k", text="t", timeout_s=5.0)
    finally:
        server.shutdown()


def test_llm_response_body_within_cap_unaffected():
    """绿守卫：1MB 内正常 chat 响应解析逐字节保持。"""
    payload = json.dumps({
        "choices": [{"message": {"content": '{"facts": []}'}}]
    }).encode("utf-8")
    server = _serve(200, payload)
    try:
        content, latency = _default_call_fn(
            model="m", base_url=f"http://127.0.0.1:{server.server_port}",
            api_key="k", text="t", timeout_s=5.0)
        assert content == '{"facts": []}'
        # W2Fε 改钉超时上界（原 latency>=0.0 近恒真）：latency=perf_counter 跨度
        # 且 urlopen(timeout=timeout_s) 执法——成功返回必经 <5s；超时闸失效
        # （挂死仍返回/上限被改）即红。呼应本文件 OVER_CAP/超时族纪律。
        assert 0.0 <= latency < 5.0
    finally:
        server.shutdown()
