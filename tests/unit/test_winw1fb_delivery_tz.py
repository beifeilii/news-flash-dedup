"""窗口W1Fβ（delivery 侧三项，W1/W1a 确认、主窗口对码）红测先行钉。

1. F-02 读侧时区归一：delivery/coordinator.py 三闸（deadline/expires_at/
   next_delivery_at）裸 fromisoformat → 局部 _parse_iso_utc 归一——
   历史裸串（naive）按 UTC 解释（写侧 deadline.py 已 naive fail-fast，
   读宽写严分工），aware 串 astimezone(UTC) 归一。
   红能力：旧代码 naive 记录遇 aware now → TypeError（comparison between
   offset-naive and offset-aware）逃逸，非 P20ReceiveError——下列 naive
   三态断言全红；aware 串行为钉（绿守卫，修复前后逐字节不变）。
2. F-03 注释对齐：coordinator.py "deadline 口径=min(...)" 误导注释 →
   实述"两闸顺序判定、拒绝语义等价 min（now≥min ⇔ now≥任一）"——
   源码钉红（行为零改动）。
3. W1a-F4 es_store 解析加固：delivery/es_store.py update_lease expires_at
   裸 fromisoformat → 同款归一纪律 + 畸形串 fail-closed——
   红能力：旧代码畸形串抛裸 ValueError、naive 抛 TypeError 逃逸（非
   P20ClaimPreconditionError）；aware 串行为钉（绿守卫）。
"""

from __future__ import annotations

import datetime
import hashlib
from datetime import date
from pathlib import Path

import pytest

from news_flash_dedup.delivery import (
    FakeDeliveryRecord,
    FakeDeliveryStore,
    P20ReceiveError,
    receive,
)
from news_flash_dedup.delivery import es_store as p20_es_store
from news_flash_dedup.delivery.es_store import (
    P20ClaimPreconditionError,
    RealESP20Config,
    RealESP20Store,
)
from news_flash_dedup.delivery.fake_store import FakeDeliveryLease

UTC = datetime.timezone.utc


def _iso(dt: datetime.datetime) -> str:
    return dt.isoformat()


# =====================================================================
# 1. F-02：coordinator.receive 三闸读侧时区归一
# =====================================================================

def _delivery_record(record_id="r-1", *, delivery_state="pending",
                     delivery_deadline_at=None, next_delivery_at=None,
                     expires_at="", round=1, round_attempts=0,
                     route_ref="http://127.0.0.1:8080/callback",
                     callback_body='{"item_id":"item-A"}') -> FakeDeliveryRecord:
    now = datetime.datetime.now(UTC)
    return FakeDeliveryRecord(
        record_id=record_id, item_id="item-A", scope_id="default",
        business_date="2026-09-26", arrival_seq=1,
        delivery_state=delivery_state,
        delivery_deadline_at=delivery_deadline_at or _iso(
            now + datetime.timedelta(hours=24)),
        next_delivery_at=next_delivery_at or _iso(
            now - datetime.timedelta(hours=1)),
        callback_attempts=0, round_attempts=round_attempts,
        round=round, callback_body=callback_body,
        payload_hash=hashlib.sha256(callback_body.encode("utf-8")).hexdigest(),
        route_ref=route_ref, vector_state="pending", audit_complete=True,
        expires_at=expires_at,
    )


def _receive(store, record_id="r-1", **kwargs):
    rec = store.main_records[record_id]
    return receive(
        store, record_id, payload_hash=rec.payload_hash,
        route_ref=rec.route_ref, expected_generation=0, event_id="e-1",
        **kwargs,
    )


def test_f02_naive_past_deadline_raises_p20_not_typeerror():
    """红能力：naive 过期 deadline + now 缺省（aware 当前时）——旧代码
    naive/aware 混比 TypeError 逃逸；修复后按 UTC 解释 → P20ReceiveError。"""
    store = FakeDeliveryStore()
    store.upsert_main(_delivery_record(
        delivery_deadline_at="2000-01-01T00:00:00"))      # naive 过期
    with pytest.raises(P20ReceiveError, match="(?i)deadline"):
        _receive(store)


def test_f02_naive_past_deadline_explicit_aware_now():
    """红能力（显式 aware now 同型）：naive 过期 deadline → P20ReceiveError。"""
    store = FakeDeliveryStore()
    store.upsert_main(_delivery_record(
        delivery_deadline_at="2000-01-01T00:00:00"))
    with pytest.raises(P20ReceiveError, match="(?i)deadline"):
        _receive(store, now="2026-09-28T00:00:00+00:00")


def test_f02_naive_future_deadline_passes_gate():
    """红能力：naive 未来 deadline → 过闸不拦，照常领取（旧 TypeError 红）。"""
    store = FakeDeliveryStore()
    store.upsert_main(_delivery_record(
        delivery_deadline_at="2099-01-01T00:00:00",        # naive 未来
        next_delivery_at="2000-01-01T00:00:00+00:00"))     # 退避已 elapsed
    lease = _receive(store, now="2026-09-28T00:00:00+00:00")
    assert lease.owner_generation == 1
    assert store.main_records["r-1"].delivery_state == "delivering"


def test_f02_naive_past_expires_at_raises_p20_not_typeerror():
    """红能力：expires_at 闸——naive 过期 expires_at（deadline aware 未来）
    → P20ReceiveError（旧 TypeError 红）。"""
    store = FakeDeliveryStore()
    store.upsert_main(_delivery_record(
        expires_at="2000-06-01T12:00:00"))                # naive 过期
    with pytest.raises(P20ReceiveError, match="(?i)expires_at"):
        _receive(store, now="2026-09-28T00:00:00+00:00")


def test_f02_naive_future_next_delivery_at_raises_p20_not_typeerror():
    """红能力：backoff 闸——naive 未来 next_delivery_at（退避未 elapsed）
    → P20ReceiveError（旧 TypeError 红）。"""
    store = FakeDeliveryStore()
    store.upsert_main(_delivery_record(
        next_delivery_at="2099-01-01T00:00:00"))          # naive 未来退避
    with pytest.raises(P20ReceiveError, match="(?i)backoff"):
        _receive(store, now="2026-09-28T00:00:00+00:00")


def test_f02_naive_now_with_aware_deadline_no_typeerror():
    """红能力：naive now + aware 未来 deadline——旧代码 now 侧裸解析与
    aware 闸值混比 TypeError 在闸内逃逸；修复后 now 同归一、三闸不再
    TypeError——naive now 一路过闸，最终由写侧 lease_until aware fail-fast
    （fake_store.update_lease → parse_utc_iso）以 ValueError 拒写
    （读宽写严分工实证；非 TypeError 非 P20ReceiveError）。"""
    store = FakeDeliveryStore()
    store.upsert_main(_delivery_record(
        delivery_deadline_at="2099-01-01T00:00:00+00:00",
        next_delivery_at="2000-01-01T00:00:00+00:00"))     # 退避已 elapsed
    with pytest.raises(ValueError, match="(?i)lease_until.*aware"):
        _receive(store, now="2026-09-28T00:00:00")         # naive now
    assert store.main_records["r-1"].delivery_state == "pending"  # 拒写不动状态


def test_f02_naive_now_naive_past_deadline_compat_pin():
    """绿守卫（兼容钉）：naive now + naive 过期 deadline——旧代码同型
    naive 可比即抛 P20ReceiveError；归一后语义逐字节不变。"""
    store = FakeDeliveryStore()
    store.upsert_main(_delivery_record(
        delivery_deadline_at="2000-01-01T00:00:00"))
    with pytest.raises(P20ReceiveError, match="(?i)deadline"):
        _receive(store, now="2026-09-28T00:00:00")


def test_f02_aware_deadline_rejection_message_byte_pinned():
    """绿守卫（aware 逐字节钉）：Z 形 aware 过期 deadline 的拒绝报文
    修复前后逐字节不变。"""
    store = FakeDeliveryStore()
    store.upsert_main(_delivery_record(
        delivery_deadline_at="2020-03-01T08:00:00Z"))
    with pytest.raises(P20ReceiveError) as excinfo:
        _receive(store, now="2026-09-28T00:00:00+00:00")
    assert str(excinfo.value) == (
        "delivery_deadline_at passed: '2020-03-01T08:00:00Z' "
        "<= now '2026-09-28T00:00:00+00:00'"
    )


def test_f02_aware_offset_deadline_equivalent_instant_rejected():
    """绿守卫（aware 逐字节钉）：+08:00 偏移串与 UTC 同一瞬——过期照拒
    （astimezone 归一不改变 aware 比较结果）。"""
    store = FakeDeliveryStore()
    store.upsert_main(_delivery_record(
        delivery_deadline_at="2020-03-01T16:00:00+08:00"))  # = 08:00Z 过期
    with pytest.raises(P20ReceiveError, match="(?i)deadline"):
        _receive(store, now="2026-09-28T00:00:00+00:00")


def test_f02_aware_offset_future_deadline_passes():
    """绿守卫（aware 逐字节钉）：+08:00 偏移未来串照常过闸。"""
    store = FakeDeliveryStore()
    store.upsert_main(_delivery_record(
        delivery_deadline_at="2099-01-01T08:00:00+08:00",
        next_delivery_at="2000-01-01T00:00:00+00:00"))     # 退避已 elapsed
    lease = _receive(store, now="2026-09-28T00:00:00+00:00")
    assert lease.owner_generation == 1


# =====================================================================
# 2. F-03：coordinator.py 注释对齐（行为零改动，源码实述钉）
# =====================================================================

def test_f03_comment_states_sequential_gates_equivalent_to_min():
    """红能力：旧注释"deadline 口径 = min(delivery_deadline_at, ...)"
    误导（实码两闸顺序判定、无 min 调用）——实述注释缺席即红。"""
    # W2Fε 修复：原 CWD 相对路径仅在仓根直跑时可达——改 __file__ 派生仓根
    src = (Path(__file__).resolve().parents[2]
           / "src" / "news_flash_dedup" / "delivery" / "coordinator.py"
           ).read_text(encoding="utf-8")
    assert "拒绝语义等价 min" in src                        # 实述在场
    assert "now ≥ min" in src or "now≥min" in src           # 等价式在场
    assert "deadline 口径 = min(delivery_deadline_at," not in src  # 误导清除


# =====================================================================
# 3. W1a-F4：es_store.update_lease expires_at 解析加固（fail-closed）
# =====================================================================

class _StubIndicesApi:
    def exists(self, *, index):
        return True

    def create(self, *, index):     # exists=True 恒成立，不应触发
        raise AssertionError(f"unexpected index create: {index}")


class _StubESClient:
    """RealESP20Store 的最小鸭式客户端（单元层，不连真 ES）。"""

    def __init__(self, docs):
        self.indices = _StubIndicesApi()
        self._docs = {rid: dict(doc) for rid, doc in docs.items()}
        self._seq: dict[str, int] = {}

    def get(self, *, index, id, realtime):
        from elasticsearch import NotFoundError
        if id not in self._docs:
            raise NotFoundError("missing", None, None)
        return {"_source": self._docs[id],
                "_seq_no": self._seq.get(id, 0), "_primary_term": 1}

    def index(self, *, index, id, document, **kwargs):
        self._seq[id] = self._seq.get(id, -1) + 1
        self._docs[id] = dict(document)
        return {"_seq_no": self._seq[id], "_primary_term": 1}

    def search(self, **kwargs):
        return {"hits": {"hits": [{"_id": rid} for rid in sorted(self._docs)]}}


def _seed_doc(*, expires_at=""):
    now = datetime.datetime.now(UTC)
    callback_body = '{"item_id":"item-A"}'
    return {
        "record_id": "r-1", "item_id": "item-A", "scope_id": "default",
        "business_date": "2026-09-26", "arrival_seq": 1,
        "delivery_state": "pending", "task_state": "succeeded",
        "audit_complete": True,
        "delivery_deadline_at": _iso(now + datetime.timedelta(hours=24)),
        "next_delivery_at": _iso(now - datetime.timedelta(hours=1)),
        "callback_attempts": 0, "round_attempts": 0, "round": 1,
        "callback_body": callback_body,
        "callback_body_hash": hashlib.sha256(
            callback_body.encode("utf-8")).hexdigest(),
        "route_ref": "http://127.0.0.1:8080/callback",
        "vector_state": "pending", "expires_at": expires_at,
    }


def _real_store(monkeypatch, doc):
    monkeypatch.setattr(p20_es_store, "assert_p20_uat_open", lambda: None)
    config = RealESP20Config(run_uuid="winw1fb", business_date=date(2026, 9, 26))
    return RealESP20Store(_StubESClient({"r-1": doc}), config)


def _claim(store, doc):
    lease = FakeDeliveryLease(
        owner_id="worker-1", owner_generation=1,
        lease_until=_iso(datetime.datetime.now(UTC)
                         + datetime.timedelta(minutes=5)),
        attempt_id="a" * 64, round=1, result_version=1, event_id="e-1",
        payload_hash=doc["callback_body_hash"],
        route_ref=doc["route_ref"],
    )
    store.main_records.claim_read("r-1")
    return store.update_lease(lease)


def test_f4_malformed_expires_at_fails_closed(monkeypatch):
    """红能力：畸形 expires_at 串——旧代码裸 ValueError 逃逸；修复后
    P20ClaimPreconditionError fail-closed 且租约/计数零写入。"""
    doc = _seed_doc(expires_at="not-an-iso-datetime")
    store = _real_store(monkeypatch, doc)
    with pytest.raises(P20ClaimPreconditionError, match="(?i)expires_at"):
        _claim(store, doc)
    source = store.get_main_record("r-1")["source"]
    assert "delivery_lease" not in source           # 拒领零写入
    assert source["round_attempts"] == 0
    assert source["callback_attempts"] == 0


def test_f4_naive_past_expires_at_rejected_not_typeerror(monkeypatch):
    """红能力：naive 过期 expires_at——旧代码 naive/aware 混比 TypeError
    逃逸；修复后按 UTC 解释 → P20ClaimPreconditionError。"""
    doc = _seed_doc(expires_at="2000-01-01T00:00:00")
    store = _real_store(monkeypatch, doc)
    with pytest.raises(P20ClaimPreconditionError, match="(?i)expires_at"):
        _claim(store, doc)
    assert "delivery_lease" not in store.get_main_record("r-1")["source"]


def test_f4_naive_future_expires_at_passes(monkeypatch):
    """红能力：naive 未来 expires_at → 过闸不拦，照常领取
    （旧 TypeError 红）。"""
    doc = _seed_doc(expires_at="2099-01-01T00:00:00")
    store = _real_store(monkeypatch, doc)
    assert _claim(store, doc) == "worker-1"
    source = store.get_main_record("r-1")["source"]
    assert source["delivery_lease"]["owner_generation"] == 1
    assert source["round_attempts"] == 1


def test_f4_aware_past_expires_at_rejection_pinned(monkeypatch):
    """绿守卫（aware 逐字节钉）：Z 形 aware 过期 expires_at 照拒
    （修复前后同形同报文）。"""
    past_z = _iso(datetime.datetime.now(UTC) - datetime.timedelta(hours=1))
    past_z = past_z.replace("+00:00", "Z")
    doc = _seed_doc(expires_at=past_z)
    store = _real_store(monkeypatch, doc)
    with pytest.raises(P20ClaimPreconditionError,
                       match="expires_at passed; claim refused"):
        _claim(store, doc)


def test_f4_aware_offset_future_expires_at_passes_pinned(monkeypatch):
    """绿守卫（aware 逐字节钉）：+08:00 偏移未来串照常领取
    （astimezone 归一不改变 aware 比较结果）。"""
    future = datetime.datetime.now(UTC) + datetime.timedelta(hours=48)
    future_cn = future.astimezone(datetime.timezone(
        datetime.timedelta(hours=8)))
    doc = _seed_doc(expires_at=_iso(future_cn))
    store = _real_store(monkeypatch, doc)
    assert _claim(store, doc) == "worker-1"
