# -*- coding: utf-8 -*-
"""W3F 红测（W3d-F-NEW-1）：dispatcher.scan_pending 畸形/空串时间字段跳闸。

缺陷面（delivery/dispatcher.py:33,41 现役）：非零校验形态（now 非空）下
`parse_iso_utc(rec.next_delivery_at)` / `parse_iso_utc(rec.delivery_deadline_at)`
裸解析——空串/畸形字段裸 ValueError 炸整个扫描批（A15"缺失/空串=存量
历史数据不拦"口径已覆盖 receive/update_lease，调度扫描路径未覆盖；
expires_at 同族裸解析一并收口）。

修法（对齐 receive/update_lease 跳闸型）：空串=闸跳过（存量不拦）；
畸形串=该记录跳过出扫描批 + skip_stats["malformed_record_fields"] 计数
+ 日志留痕（不炸批）；合法记录扫描语义逐字节不变。
"""

from __future__ import annotations

import datetime

from news_flash_dedup.delivery import dispatcher
from news_flash_dedup.delivery.fake_store import FakeDeliveryStore

from tests.unit.test_winw2fc_tz_gates import _iso, _record

UTC = datetime.timezone.utc


def _now_iso(**delta_kw):
    return _iso(datetime.datetime.now(UTC) + datetime.timedelta(**delta_kw))


# ---------- 红测（修复前裸 ValueError 炸批） ----------

def test_new1_malformed_next_delivery_at_skipped_batch_survives():
    """红能力：畸形 next_delivery_at——旧裸 ValueError 炸整批（合法记录
    陪葬）；修复后该记录跳过 + 计数，合法记录照常扫出。"""
    store = FakeDeliveryStore()
    store.upsert_main(_record(record_id="r-bad", next_delivery_at="garbage"))
    store.upsert_main(_record(record_id="r-ok"))
    skip_stats: dict[str, int] = {}
    scanned = dispatcher.scan_pending(store, now=_now_iso(), skip_stats=skip_stats)
    assert scanned == ["r-ok"]
    assert skip_stats.get("malformed_record_fields") == 1


def test_new1_malformed_deadline_skipped_batch_survives():
    """红能力：畸形 delivery_deadline_at——同型跳闸（跳过+计数不炸批）。"""
    store = FakeDeliveryStore()
    store.upsert_main(_record(record_id="r-bad",
                              delivery_deadline_at="not-an-iso"))
    store.upsert_main(_record(record_id="r-ok"))
    skip_stats: dict[str, int] = {}
    scanned = dispatcher.scan_pending(store, now=_now_iso(), skip_stats=skip_stats)
    assert scanned == ["r-ok"]
    assert skip_stats.get("malformed_record_fields") == 1


def test_new1_empty_fields_trip_gate_record_not_blocked():
    """红能力：空串 next_delivery_at/delivery_deadline_at——旧裸
    ValueError 炸批；修复后 A15 同纪律闸跳过（存量不拦），记录照常扫出
    且不计入畸形计数。"""
    store = FakeDeliveryStore()
    store.upsert_main(_record(record_id="r-empty", next_delivery_at="",
                              delivery_deadline_at=""))
    skip_stats: dict[str, int] = {}
    scanned = dispatcher.scan_pending(store, now=_now_iso(), skip_stats=skip_stats)
    assert scanned == ["r-empty"]
    assert skip_stats.get("malformed_record_fields", 0) == 0


def test_new1_skip_stats_optional_param_absent_still_no_crash():
    """红能力：不传 skip_stats 时畸形记录同样不炸批（跳闸本体不依赖
    计数落点）。"""
    store = FakeDeliveryStore()
    store.upsert_main(_record(record_id="r-bad", next_delivery_at="@@bad@@"))
    store.upsert_main(_record(record_id="r-ok"))
    assert dispatcher.scan_pending(store, now=_now_iso()) == ["r-ok"]


# ---------- 前后均绿守卫（合法文档扫描语义逐字节不变） ----------

def test_new1_legal_records_scan_semantics_unchanged():
    """守卫（前后同绿）：到点+截止内扫出；未到点/过截止/过 expires 不扫。"""
    store = FakeDeliveryStore()
    store.upsert_main(_record(record_id="r-due"))
    store.upsert_main(_record(record_id="r-not-due",
                              next_delivery_at=_now_iso(hours=1)))
    store.upsert_main(_record(record_id="r-expired",
                              delivery_deadline_at=_now_iso(hours=-1)))
    store.upsert_main(_record(record_id="r-exp",
                              expires_at=_now_iso(minutes=-5)))
    assert dispatcher.scan_pending(store, now=_now_iso()) == ["r-due"]


def test_new1_zero_validation_form_still_untouched():
    """守卫（前后同绿）：now="" 零校验形态不触碰记录字段（A1 现役契约）。"""
    store = FakeDeliveryStore()
    store.upsert_main(_record(next_delivery_at="garbage",
                              delivery_deadline_at="also-garbage"))
    assert dispatcher.scan_pending(store, now="") == ["r-1"]
