"""W2Fδ 修复窗口红测：条79（flushed_signal/Event 移入锁）+ 条80（admission_schema 时间 pattern）。

探针先行：log/temp/winw2fd-probe-collector-signal.json、winw2fd-probe-admission-schema.json。
只新建不改既有套件。
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from news_flash_dedup.admission import AdmissionRequest
from news_flash_dedup.batch_admission import BatchAdmissionCoordinator, BatchLimits
from news_flash_dedup.batch_collector import BatchAdmissionCollector

from test_batch_admission import BatchMemoryStore


# ---------- 条79：_flushed_signal 递增与 batch_flushed.set 须在 condition 锁内 ----------

def _collector():
    store = BatchMemoryStore()
    coordinator = BatchAdmissionCoordinator(
        store, owner_id="w2fd", owner_isolated=lambda: True,
        limits=BatchLimits(),
        clock=lambda: datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc))
    return BatchAdmissionCollector(
        coordinator, max_wait_seconds=0.005, request_timeout_seconds=2,
        close_timeout_seconds=2)


def _request():
    return AdmissionRequest(
        scope_id="default", request_id="1-1", item_id="1-1", text="正文",
        received_at=datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc),
        schema_version="1", pipeline_version="w2fd-v1", embedding_space_id="none",
        delivery_route_ref="route-v1", trace_id=None)


def test_event_set_happens_under_condition_lock():
    """条79：batch_flushed.set 调用点必须持有 condition 锁（探针实证旧码在锁外）。"""
    collector = _collector()
    observations = []
    original_set = collector.batch_flushed.set

    def wrapped_set():
        observations.append(collector._condition._is_owned())
        return original_set()

    collector.batch_flushed.set = wrapped_set
    try:
        collector.accept(_request())
    finally:
        collector.batch_flushed.set = original_set
        collector.close()
    assert observations, "一次成功 flush 必须触发一次 Event set"
    assert all(observations), "Event set 必须在 condition 锁内（否则物化器可观察到中间态）"


def test_flushed_signal_consistent_with_event_under_lock():
    """条79：snapshot 锁内读的 flushed_signal 与 Event 触发次数一致。"""
    collector = _collector()
    try:
        collector.accept(_request())
        snapshot = collector.snapshot()
    finally:
        collector.close()
    assert snapshot.flushed_signal == 1
    assert collector.batch_flushed.is_set()


# ---------- 条80：PendingAdmissionV1 时间字段 pattern（_utc canonical 形） ----------

def _pending(**overrides):
    value = {
        "scope_id": "default", "request_id": "100-1", "item_id": "100-1",
        "record_id": "a" * 64, "business_fingerprint": "b" * 64,
        "text": "正文", "delivery_route_ref": "route-v1", "trace_id": None,
        "received_at": "2026-09-28T04:00:00.000000Z",
        "accepted_at": "2026-09-28T04:00:00.000000Z",
        "business_date": "2026-09-28",
        "expires_at": "2026-10-05T00:00:00.000000Z",
        "schema_version": "1", "pipeline_version": "v1",
        "embedding_space_id": "v3", "arrival_seq": 1,
    }
    value.update(overrides)
    return value


@pytest.mark.parametrize("field", ["received_at", "accepted_at", "expires_at"])
@pytest.mark.parametrize("bad", [
    "2026-09-28T04:00:00Z",              # 缺微秒
    "2026-09-28T04:00:00.000000+00:00",  # 偏移形非 canonical Z
    "2026-09-28 04:00:00.000000Z",       # 缺 T 分隔
    "2026-09-28T04:00:00.000000",        # naive
    "not-a-timestamp",
])
def test_time_fields_reject_non_canonical_forms(field, bad):
    """条80：时间三字段钉 _utc canonical 形（探针：1001/1001 产出全匹配）。"""
    from news_flash_dedup.admission_schema import PendingAdmissionV1
    with pytest.raises(ValidationError):
        PendingAdmissionV1.model_validate(_pending(**{field: bad}))


def test_time_fields_accept_canonical_form():
    from news_flash_dedup.admission_schema import PendingAdmissionV1
    model = PendingAdmissionV1.model_validate(_pending())
    assert model.accepted_at == "2026-09-28T04:00:00.000000Z"


def test_utc_producer_output_matches_pinned_pattern():
    """条80 闭环：现役唯一产出面 _utc 的实产串必须过 pattern（防钉错方向）。"""
    from news_flash_dedup.admission import _utc
    from news_flash_dedup.admission_schema import PendingAdmissionV1
    produced = _utc(datetime(2026, 9, 28, 4, 0, 0, tzinfo=timezone.utc))
    PendingAdmissionV1.model_validate(_pending(accepted_at=produced))
