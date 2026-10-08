"""P2 工程债 C-04/C-08：expires_at 三层透传 + delivery_deadline_at min 钳制。

规格出处：10 文 §6.337（终态 CAS 保存 delivery_deadline_at =
min(completed_at+24小时, expires_at)）、§6.340（调度/领取/发送前都检查
now < delivery_deadline_at 且 now < expires_at）。

红能力说明（本文件全部断言针对新行为，旧代码必红）：
- deadline 钳制助手不存在 → ImportError（collection 红）；
- 三层函数无 expires_at 参数 → TypeError: unexpected keyword argument；
- FakeDeliveryRecord 无 expires_at 字段 → TypeError / AttributeError；
- scan_pending 无 expires_at 闸 → 过期记录仍被扫出（断言红）；
- 旧代码无钳制 → deadline == now+24h 恒成立，min 断言红。
默认兼容：expires_at=None / 空串时现状行为逐字节保持（不钳制、不写字段）。

窗口X 并线适配（移植自主仓外 P2 副本，不照搬夹具——适配主仓演进）：
- CommitContext 补 coverage_complete=True（窗口J M-01：kw_only 强制显式）；
- persist_main_record / persist_main_record_es 补 audit_complete=True
  （窗口V M-01 同标准：强制关键字显式表态）。
"""

from __future__ import annotations

import datetime
import hashlib

import pytest

from news_flash_dedup import admission as _admission
from news_flash_dedup.commit import CommitContext, commit_one
from news_flash_dedup.commit.fake_store import FakeCommitStore, FakeMainRecord
from news_flash_dedup.decide.types import DecideOutcome
from news_flash_dedup.deadline import clamp_deadline, deadline_iso, parse_utc_iso
from news_flash_dedup.delivery import (
    FakeDeliveryRecord,
    FakeDeliveryStore,
    persist_next_delivery,
    scan_pending,
)
from news_flash_dedup.delivery.es_store import _doc_to_record
from news_flash_dedup.persist import coordinator as persist_coordinator
from news_flash_dedup.persist.es_store import _record_to_body, persist_main_record_es
from news_flash_dedup.persist.fake_store import FakeP18Store

UTC = datetime.timezone.utc


def _iso(dt: datetime.datetime) -> str:
    return dt.isoformat()


def _decide(item_id="item-C") -> DecideOutcome:
    return DecideOutcome(
        item_id=item_id, text="甲公司完成回购。",
        decision="重复", duplicate_ids=("item-A",),
        reason="主体与事件一致", internal_code="FACT_EQUIVALENT",
        pair_codes={"a" * 64: "FACT_EQUIVALENT"}, used_evidence=(),
        raw_hash="h", pipeline_version="dedup_v1",
    )


def _commit_store_and_ctx() -> tuple[FakeCommitStore, CommitContext, str]:
    """零候选分区首条路径（commit_one 最简 committed 路径）。"""
    record_id = "c" * 64
    text = "甲公司完成回购。"
    current = {
        "record_id": record_id, "item_id": "item-C", "text": text,
        "raw_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "scope_id": "default", "business_date": "2026-09-26",
        "arrival_seq": 1, "pipeline_version": "dedup_v1",
    }
    ctx = CommitContext(
        scope_id="default", business_date="2026-09-26",
        arrival_seq=1, current=current, candidates=(),
        visible_seq=1, prepared_seq=1,
        coverage_complete=True,                # 窗口X 适配：窗口J M-01 强制显式
    )
    return FakeCommitStore(), ctx, record_id


class _StubESStore:
    """persist_main_record_es 的鸭式 stub：只捕获 body，不连真 ES。"""

    def __init__(self) -> None:
        self.bodies: list[dict] = []
        self.watermarks: list[tuple] = []

    def write_audit_batch(self, records, *, created_at=None):
        return []

    def cas_main_record(self, record_id, body, *, expected_seq_no,
                        expected_primary_term):
        self.bodies.append(dict(body))
        return 1

    def advance_watermark(self, scope_id, business_date, arrival_seq):
        self.watermarks.append((scope_id, business_date, arrival_seq))
        return arrival_seq


# ---------- deadline 钳制助手（单源） ----------

def test_parse_utc_iso_accepts_z_and_offset():
    a = parse_utc_iso("2026-09-27T00:00:00Z")
    b = parse_utc_iso("2026-09-27T08:00:00+08:00")
    assert a == b                       # 同一瞬间
    assert a.utcoffset() == datetime.timedelta(0)


def test_parse_utc_iso_rejects_naive():
    with pytest.raises(ValueError, match="(?i)timezone|naive|aware"):
        parse_utc_iso("2026-09-27T00:00:00")


def test_clamp_deadline_none_keeps_24h_behavior():
    now = datetime.datetime(2026, 9, 26, 12, 0, 0, tzinfo=UTC)
    deadline = clamp_deadline(now, expires_at=None)
    assert deadline == now + datetime.timedelta(hours=24)


def test_clamp_deadline_picks_earlier_expires_at():
    now = datetime.datetime(2026, 9, 26, 12, 0, 0, tzinfo=UTC)
    exp = _iso(now + datetime.timedelta(hours=1))
    deadline = clamp_deadline(now, expires_at=exp)
    assert deadline == now + datetime.timedelta(hours=1)


def test_clamp_deadline_keeps_24h_when_expires_later():
    now = datetime.datetime(2026, 9, 26, 12, 0, 0, tzinfo=UTC)
    exp = _iso(now + datetime.timedelta(hours=48))
    deadline = clamp_deadline(now, expires_at=exp)
    assert deadline == now + datetime.timedelta(hours=24)


def test_deadline_iso_empty_string_means_no_clamp():
    now = datetime.datetime(2026, 9, 26, 12, 0, 0, tzinfo=UTC)
    assert deadline_iso(now, expires_at="") == _iso(now + datetime.timedelta(hours=24))


# ---------- commit 层（C-04：commit/coordinator.py 写入点） ----------

def test_commit_one_clamps_deadline_to_expires_at():
    store, ctx, record_id = _commit_store_and_ctx()
    exp = _iso(datetime.datetime.now(UTC) + datetime.timedelta(hours=1))
    outcome = commit_one(ctx, store, expires_at=exp, audit_complete=True)
    assert outcome.state == "committed"
    record = store.main_records[record_id]
    deadline = parse_utc_iso(record.delivery_deadline_at)
    assert abs((deadline - parse_utc_iso(exp)).total_seconds()) < 2
    assert record.expires_at == exp                    # 透传落记录


def test_commit_one_keeps_24h_when_expires_later():
    store, ctx, record_id = _commit_store_and_ctx()
    exp = _iso(datetime.datetime.now(UTC) + datetime.timedelta(hours=48))
    commit_one(ctx, store, expires_at=exp, audit_complete=True)
    record = store.main_records[record_id]
    deadline = parse_utc_iso(record.delivery_deadline_at)
    completed = parse_utc_iso(record.completed_at)
    delta = (deadline - completed).total_seconds()
    assert 23 * 3600 < delta <= 24 * 3600 + 5          # 未被 48h 钳制
    assert record.expires_at == exp


def test_commit_one_none_expires_keeps_current_behavior():
    store, ctx, record_id = _commit_store_and_ctx()
    commit_one(ctx, store, audit_complete=True)        # 默认：无 expires_at
    record = store.main_records[record_id]
    deadline = parse_utc_iso(record.delivery_deadline_at)
    completed = parse_utc_iso(record.completed_at)
    delta = (deadline - completed).total_seconds()
    assert 23 * 3600 < delta <= 24 * 3600 + 5
    assert record.expires_at == ""                     # 默认空串，构造兼容


# ---------- persist fake 层（C-04：persist/coordinator.py 写入点） ----------

def test_persist_main_record_clamps_deadline_to_expires_at():
    store = FakeP18Store()
    exp = _iso(datetime.datetime.now(UTC) + datetime.timedelta(hours=1))
    record = persist_coordinator.persist_main_record(
        _decide(), store, arrival_seq=1, raw_hash="h", audit_ids=(),
        audit_complete=True,                           # 窗口X 适配：窗口V 强制显式
        expires_at=exp,
    )
    deadline = parse_utc_iso(record.delivery_deadline_at)
    assert abs((deadline - parse_utc_iso(exp)).total_seconds()) < 2
    assert record.expires_at == exp
    stored = store.main_records[record.record_id]
    assert stored.expires_at == exp


def test_persist_main_record_none_expires_keeps_current_behavior():
    store = FakeP18Store()
    record = persist_coordinator.persist_main_record(
        _decide(), store, arrival_seq=1, raw_hash="h", audit_ids=(),
        audit_complete=True,                           # 窗口X 适配：窗口V 强制显式
    )
    deadline = parse_utc_iso(record.delivery_deadline_at)
    completed = parse_utc_iso(record.completed_at)
    delta = (deadline - completed).total_seconds()
    assert 23 * 3600 < delta <= 24 * 3600 + 5
    assert record.expires_at == ""


# ---------- persist 真 ES 层（C-04：persist/es_store.py 写入点） ----------

def test_persist_main_record_es_clamps_deadline_and_passes_expires():
    store = _StubESStore()
    exp = _iso(datetime.datetime.now(UTC) + datetime.timedelta(hours=1))
    body = persist_main_record_es(
        _decide(), store, record_id="r-es-1",
        audit_records=(), scope_id="default", business_date="2026-09-26",
        arrival_seq=1, raw_hash="h",
        audit_complete=True,                           # 窗口X 适配：窗口V 强制显式
        expires_at=exp,
    )
    deadline = parse_utc_iso(body["delivery_deadline_at"])
    assert abs((deadline - parse_utc_iso(exp)).total_seconds()) < 2
    assert body["expires_at"] == exp                   # 真 ES body 透传
    assert store.bodies[0]["expires_at"] == exp


def test_persist_main_record_es_none_expires_body_unchanged():
    store = _StubESStore()
    body = persist_main_record_es(
        _decide(), store, record_id="r-es-2",
        audit_records=(), scope_id="default", business_date="2026-09-26",
        arrival_seq=1, raw_hash="h",
        audit_complete=True,                           # 窗口X 适配：窗口V 强制显式
    )
    assert "expires_at" not in body                    # 现状 body 形状不动
    deadline = parse_utc_iso(body["delivery_deadline_at"])
    delta = (deadline - datetime.datetime.now(UTC)).total_seconds()
    assert 23 * 3600 < delta <= 24 * 3600 + 5


def test_record_to_body_maps_expires_at_only_when_present():
    base = dict(
        record_id="r-1", item_id="item-A", text="t",
        decision="重复", duplicate_ids=("item-B",), reason="r",
        # 10-07 令甲-i（I-6）：重复+pending 组合在新口径非法——翻 held 对齐
        payload_hash="p", delivery_state="held",
        delivery_deadline_at="2026-09-27T00:00:00+00:00",
        callback_attempts=0, audit_ids=(), audit_complete=True,
        raw_hash="h", pipeline_version="dedup_v1",
        completed_at="2026-09-26T00:00:00+00:00", result_version=1,
        event_id="e",
    )
    body_default = _record_to_body(
        FakeMainRecord(**base), scope_id="s", business_date="2026-09-26",
        arrival_seq=1)
    assert "expires_at" not in body_default            # 默认空串 → 不落 key
    body_with = _record_to_body(
        FakeMainRecord(**base, expires_at="2026-09-29T00:00:00+00:00"),
        scope_id="s", business_date="2026-09-26", arrival_seq=1)
    assert body_with["expires_at"] == "2026-09-29T00:00:00+00:00"


# ---------- delivery 层（C-08：FakeDeliveryRecord + scan_pending + es_store 读取） ----------

def _delivery_record(record_id="r-1", *, deadline_hours=24.0, expires_at="",
                     next_hours=-1.0, state="pending") -> FakeDeliveryRecord:
    now = datetime.datetime.now(UTC)
    callback_body = '{"item_id":"item-A"}'
    return FakeDeliveryRecord(
        record_id=record_id, item_id="item-A", scope_id="default",
        business_date="2026-09-26", arrival_seq=1,
        delivery_state=state,
        delivery_deadline_at=_iso(now + datetime.timedelta(hours=deadline_hours)),
        next_delivery_at=_iso(now + datetime.timedelta(hours=next_hours)),
        callback_attempts=0, round_attempts=0, round=1,
        callback_body=callback_body,
        payload_hash=hashlib.sha256(callback_body.encode("utf-8")).hexdigest(),
        route_ref="http://127.0.0.1:8080/callback",
        vector_state="pending", audit_complete=True,
        expires_at=expires_at,
    )


def test_fake_delivery_record_expires_at_defaults_empty():
    rec = _delivery_record()
    assert rec.expires_at == ""                        # 构造兼容：默认空串


def test_scan_pending_excludes_expired_record_even_with_future_deadline():
    """§6.340：调度须同时检查 now < expires_at（字段存在时）。

    红能力：旧 scan_pending 只查 delivery_deadline_at——deadline 未来而
    expires_at 已过的记录仍被扫出（断言红）。
    """
    store = FakeDeliveryStore()
    past = _iso(datetime.datetime.now(UTC) - datetime.timedelta(hours=1))
    future = _iso(datetime.datetime.now(UTC) + datetime.timedelta(hours=48))
    store.upsert_main(_delivery_record("r-expired", expires_at=past))
    store.upsert_main(_delivery_record("r-live", expires_at=future))
    store.upsert_main(_delivery_record("r-no-exp"))    # 空串 → 现状行为
    scanned = scan_pending(store, now=_iso(datetime.datetime.now(UTC)))
    assert "r-expired" not in scanned
    assert "r-live" in scanned
    assert "r-no-exp" in scanned


def test_doc_to_record_reads_expires_at():
    source = {
        "record_id": "r-1", "callback_body": "",
        "callback_body_hash": hashlib.sha256(b"").hexdigest(),
        "delivery_state": "pending",
        "delivery_deadline_at": "2026-09-27T00:00:00+00:00",
        "next_delivery_at": "2026-09-26T00:00:00+00:00",
        "expires_at": "2026-09-29T00:00:00+00:00",
    }
    rec = _doc_to_record(source)
    assert rec.expires_at == "2026-09-29T00:00:00+00:00"
    source_absent = dict(source)
    del source_absent["expires_at"]
    assert _doc_to_record(source_absent).expires_at == ""


def test_fake_store_state_transitions_preserve_expires_at():
    """mark_state / advance_round / persist_next_delivery 重建记录不得丢字段。"""
    store = FakeDeliveryStore()
    exp = "2026-09-29T00:00:00+00:00"
    store.upsert_main(_delivery_record("r-1", expires_at=exp))
    store.mark_state("r-1", "delivering")
    assert store.main_records["r-1"].expires_at == exp
    store.advance_round("r-1", 2)
    assert store.main_records["r-1"].expires_at == exp
    persist_next_delivery(store, "r-1", next_seconds=5.0)
    assert store.main_records["r-1"].expires_at == exp
