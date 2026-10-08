# -*- coding: utf-8 -*-
"""W2Fγ 红测（时区分类学收口族）：条目 12（A9+A10）/13（A1）/20（A11 公共名）/21（A15）。

红能力（修复前原貌）：
- 条目12① delivery/coordinator.receive 三闸（deadline/expires_at/next_delivery_at）
  畸形串 + 畸形 now → 裸 ValueError 逃逸（非 P20ReceiveError）；
- 条目12② es_store.update_lease deadline 闸裸 fromisoformat（与已硬化 expires
  闸不同形）→ 畸形串裸 ValueError、naive 串不拒；
- 条目13 dispatcher.scan_pending：now="" 零校验形态下仍解析记录字段
  （白崩面）+ naive/aware 混比 TypeError；
- 条目20 coordinator.parse_iso_utc 公共名缺席；
- 条目21 fake receive 空 deadline 崩溃（真层 update_lease 空串跳闸不保真）。
"""
from __future__ import annotations

import datetime
import hashlib

import pytest

from news_flash_dedup.delivery import dispatcher
from news_flash_dedup.delivery.coordinator import P20ReceiveError, receive
from news_flash_dedup.delivery.fake_store import FakeDeliveryRecord, FakeDeliveryStore

UTC = datetime.timezone.utc


def _iso(dt):
    return dt.isoformat()


def _record(**over):
    now = datetime.datetime.now(UTC)
    body = '{"item_id":"item-A"}'
    fields = dict(
        record_id="r-1", item_id="item-A", scope_id="default",
        business_date="2026-09-26", arrival_seq=1,
        delivery_state="pending",
        delivery_deadline_at=_iso(now + datetime.timedelta(hours=24)),
        next_delivery_at=_iso(now - datetime.timedelta(hours=1)),
        callback_attempts=0, round_attempts=0, round=1,
        callback_body=body,
        payload_hash=hashlib.sha256(body.encode("utf-8")).hexdigest(),
        route_ref="http://127.0.0.1:8080/callback",
        vector_state="pending", audit_complete=True,
    )
    fields.update(over)
    return FakeDeliveryRecord(**fields)


def _receive(store, **kw):
    rec = store.main_records["r-1"]
    args = dict(payload_hash=rec.payload_hash, route_ref=rec.route_ref,
                expected_generation=0, event_id="e-1")
    args.update(kw)
    return receive(store, "r-1", **args)


# ---------- 条目12①：receive 三闸畸形串 fail-closed（P20ReceiveError 域） ----------

def test_receive_malformed_deadline_wrapped_not_bare_valueerror():
    """红能力：畸形 delivery_deadline_at——旧裸 ValueError 逃逸（非 P20ReceiveError）。"""
    store = FakeDeliveryStore()
    store.upsert_main(_record(delivery_deadline_at="not-an-iso"))
    with pytest.raises(P20ReceiveError, match="(?i)deadline"):
        _receive(store)


def test_receive_malformed_expires_at_wrapped_not_bare_valueerror():
    """红能力：畸形 expires_at——旧裸 ValueError 逃逸。"""
    store = FakeDeliveryStore()
    store.upsert_main(_record(expires_at="@@bad@@"))
    with pytest.raises(P20ReceiveError, match="(?i)expires_at"):
        _receive(store)


def test_receive_malformed_next_delivery_at_wrapped_not_bare_valueerror():
    """红能力：畸形 next_delivery_at——旧裸 ValueError 逃逸。"""
    store = FakeDeliveryStore()
    store.upsert_main(_record(next_delivery_at="garbage"))
    with pytest.raises(P20ReceiveError, match="(?i)next_delivery_at"):
        _receive(store)


def test_receive_malformed_now_wrapped_not_bare_valueerror():
    """红能力：畸形 now 入参——旧裸 ValueError 逃逸。"""
    store = FakeDeliveryStore()
    store.upsert_main(_record())
    with pytest.raises(P20ReceiveError, match="(?i)now"):
        _receive(store, now="not-a-time")


def test_receive_naive_fields_normalized_still_passes():
    """绿守卫：naive 合法串按 UTC 解释（W1Fβ 读宽纪律）照常领取。"""
    now = datetime.datetime.now(UTC)
    store = FakeDeliveryStore()
    store.upsert_main(_record(
        delivery_deadline_at=(now + datetime.timedelta(hours=24)).replace(tzinfo=None).isoformat(),
        next_delivery_at=(now - datetime.timedelta(hours=1)).replace(tzinfo=None).isoformat(),
    ))
    lease = _receive(store, now=now.isoformat())
    assert lease.owner_generation == 1


# ---------- 条目21（A15）：fake receive 空 deadline 对齐真层空串跳闸 ----------

def test_receive_empty_deadline_skips_gate_aligned_with_real_layer():
    """红能力：空 delivery_deadline_at——fake 旧实现解析崩溃（真层 update_lease
    空串跳闸不拦）；修复后 fake 对齐跳闸照常领取。"""
    store = FakeDeliveryStore()
    store.upsert_main(_record(delivery_deadline_at=""))
    lease = _receive(store)
    assert lease.owner_generation == 1


# ---------- 条目20（A11）：coordinator.parse_iso_utc 公共名 ----------

def test_parse_iso_utc_public_name_exported():
    """红能力：跨模块复用合同以私有名 _parse_iso_utc 维持——公共名缺席即红。"""
    from news_flash_dedup.delivery import coordinator
    assert hasattr(coordinator, "parse_iso_utc")
    assert "parse_iso_utc" in coordinator.__all__
    # 兼容别名留场（既有消费点逐字节不动）
    assert coordinator._parse_iso_utc is coordinator.parse_iso_utc


# ---------- 条目13（A1）：dispatcher 归一 + 解析移入 now_ts 分支 ----------

def test_scan_pending_zero_validation_form_does_not_parse_record_fields():
    """红能力：now="" 零校验形态下记录字段畸形——旧实现先解析后短路
    （白崩面：零校验形态也裸 ValueError）；修复后解析移入 now_ts 分支，
    零校验形态不触碰记录字段。"""
    store = FakeDeliveryStore()
    store.upsert_main(_record(next_delivery_at="garbage",
                              delivery_deadline_at="also-garbage"))
    assert dispatcher.scan_pending(store, now="") == ["r-1"]


def test_scan_pending_naive_now_aware_record_no_typeerror():
    """红能力：naive now + aware 记录字段——旧裸 fromisoformat 混比 TypeError；
    修复后同款归一（naive 按 UTC）正常比较。"""
    now = datetime.datetime.now(UTC)
    store = FakeDeliveryStore()
    store.upsert_main(_record())                     # aware 字段（含 +00:00）
    naive_now = now.replace(tzinfo=None).isoformat()
    assert dispatcher.scan_pending(store, now=naive_now) == ["r-1"]


def test_scan_pending_aware_semantics_pinned():
    """绿守卫：aware 调度语义逐字节钉（到点+截止内扫出；未到点/过截止不扫）。"""
    now = datetime.datetime.now(UTC)
    store = FakeDeliveryStore()
    due = _record(record_id="r-due")
    not_due = _record(record_id="r-not-due",
                      next_delivery_at=_iso(now + datetime.timedelta(hours=1)))
    past_deadline = _record(
        record_id="r-expired",
        delivery_deadline_at=_iso(now - datetime.timedelta(hours=1)))
    store.upsert_main(due)
    store.upsert_main(not_due)
    store.upsert_main(past_deadline)
    assert dispatcher.scan_pending(store, now=now.isoformat()) == ["r-due"]


# ---------- 条目12②：es_store.update_lease deadline 闸硬化（同 expires 闸形） ----------

class _StubIndicesApi:
    def exists(self, *, index):
        return True

    def create(self, *, index):
        raise AssertionError(f"unexpected index create: {index}")


class _StubESClient:
    """RealESP20Store 最小鸭式客户端（单元层，不连真 ES）。"""

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


def _seed_doc(**over):
    now = datetime.datetime.now(UTC)
    body = '{"item_id":"item-A"}'
    doc = {
        "record_id": "r-1", "item_id": "item-A", "scope_id": "default",
        "business_date": "2026-09-26", "arrival_seq": 1,
        "delivery_state": "pending", "task_state": "succeeded",
        "audit_complete": True,
        "delivery_deadline_at": _iso(now + datetime.timedelta(hours=24)),
        "next_delivery_at": _iso(now - datetime.timedelta(hours=1)),
        "callback_attempts": 0, "round_attempts": 0, "round": 1,
        "callback_body": body,
        "callback_body_hash": hashlib.sha256(body.encode("utf-8")).hexdigest(),
        "route_ref": "http://127.0.0.1:8080/callback",
        "vector_state": "pending", "expires_at": "",
    }
    doc.update(over)
    return doc


def _real_store(monkeypatch, doc):
    from news_flash_dedup.delivery import es_store as p20_es_store
    from news_flash_dedup.delivery.es_store import RealESP20Config, RealESP20Store
    monkeypatch.setattr(p20_es_store, "assert_p20_uat_open", lambda: None)
    config = RealESP20Config(run_uuid="winw2fc", business_date=datetime.date(2026, 9, 26))
    return RealESP20Store(_StubESClient({"r-1": doc}), config)


def _claim(store, doc):
    from news_flash_dedup.delivery.fake_store import FakeDeliveryLease
    lease = FakeDeliveryLease(
        owner_id="worker-1", owner_generation=1,
        lease_until=_iso(datetime.datetime.now(UTC)
                         + datetime.timedelta(minutes=5)),
        attempt_id="a" * 64, round=1, result_version=1, event_id="e-1",
        payload_hash=doc["callback_body_hash"], route_ref=doc["route_ref"],
    )
    store.main_records.claim_read("r-1")
    return store.update_lease(lease)


def test_update_lease_malformed_deadline_fails_closed(monkeypatch):
    """红能力：畸形 delivery_deadline_at——旧裸 fromisoformat ValueError 逃逸
    （与已硬化 expires 闸不同形）；修复后 P20ClaimPreconditionError 且零写入。"""
    from news_flash_dedup.delivery.es_store import P20ClaimPreconditionError
    doc = _seed_doc(delivery_deadline_at="not-an-iso")
    store = _real_store(monkeypatch, doc)
    with pytest.raises(P20ClaimPreconditionError, match="(?i)deadline"):
        _claim(store, doc)
    source = store.get_main_record("r-1")["source"]
    assert "delivery_lease" not in source
    assert source["round_attempts"] == 0


def test_update_lease_naive_past_deadline_rejected_not_typeerror(monkeypatch):
    """红能力：naive 过期 deadline——旧 naive/aware 混比 TypeError 逃逸；
    修复后按 UTC 解释 → P20ClaimPreconditionError。"""
    from news_flash_dedup.delivery.es_store import P20ClaimPreconditionError
    doc = _seed_doc(delivery_deadline_at="2000-01-01T00:00:00")
    store = _real_store(monkeypatch, doc)
    with pytest.raises(P20ClaimPreconditionError, match="(?i)deadline"):
        _claim(store, doc)


def test_update_lease_aware_future_deadline_passes_pinned(monkeypatch):
    """绿守卫（aware 逐字节钉）：合法未来 deadline 照常领取。"""
    doc = _seed_doc()
    store = _real_store(monkeypatch, doc)
    assert _claim(store, doc) == "worker-1"
    source = store.get_main_record("r-1")["source"]
    assert source["delivery_lease"]["owner_generation"] == 1
    assert source["round_attempts"] == 1
