from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from news_flash_dedup.admission import (
    AdmissionConflict, BUSINESS_ZONE, AdmissionUnknown,
)
from news_flash_dedup.batch_admission import (
    BatchAdmissionCoordinator, BatchLimits,
)
from news_flash_dedup.batch_collector import BatchAdmissionCollector

from test_batch_admission import BatchMemoryStore


def _clock_at(moment: datetime):
    return lambda: moment


def _make_request(scope, request_id, item_id, text, received_at):
    from news_flash_dedup.admission import AdmissionRequest
    return AdmissionRequest(
        scope_id=scope, request_id=request_id, item_id=item_id, text=text,
        received_at=received_at,
        schema_version="1", pipeline_version="prod-v1",
        embedding_space_id="none", delivery_route_ref="prod-route-v1",
        trace_id="quarantine-trace",
    )


def test_accept_after_d_plus_seven_quarantines_old_pending_and_blocks_new_acceptance():
    """D 日受理后等到 D+8，accept_batch 触发 quarantine，阻断了 D 日 pending 的后续访问。

    T020 核心：到期后重试 → 不发送过期正文；quarantine 阻断新受理。
    """
    d_end = datetime(2026, 9, 24, 23, 59, 0, tzinfo=BUSINESS_ZONE).astimezone(timezone.utc)
    d8 = datetime(2026, 10, 2, 0, 0, 30, tzinfo=BUSINESS_ZONE).astimezone(timezone.utc)
    store = BatchMemoryStore()
    service = BatchAdmissionCoordinator(
        store, owner_id="quarantine-1", owner_isolated=lambda: True,
        limits=BatchLimits(), clock=_clock_at(d_end),
    )
    collector = BatchAdmissionCollector(service, max_wait_seconds=0.005,
                                       max_queue_items=4, request_timeout_seconds=0.5)
    # D 日 23:59 受理（不物化）
    receipt = collector.accept(_make_request("default", "9001-1", "9001-1",
                                                "D 日原文", d_end))
    assert receipt.business_date == "2026-09-24"
    collector.close()

    # 切到 D+8 时钟：service 重启（持久化已写入 store）
    service_d8 = BatchAdmissionCoordinator(
        store, owner_id="quarantine-1", owner_isolated=lambda: True,
        limits=BatchLimits(), clock=_clock_at(d8),
    )
    # quarantine 触发：service 启动时 _head() 不自动 quarantine；下一次写操作触发。
    # 触发场景：D+8 同 owner 同 store 写入第二个请求时，_head() 看到 pending → _quarantine_if_expired
    collector_d8 = BatchAdmissionCollector(service_d8, max_wait_seconds=0.005,
                                        max_queue_items=4, request_timeout_seconds=0.5)
    try:
        with pytest.raises(AdmissionConflict, match="expired batch is quarantined"):
            collector_d8.accept(_make_request("default", "9002-1", "9002-1",
                                                "D+8 新文", d8))
    finally:
        collector_d8.close()
    # quarantine 已记录
    head = store.get("news-dedup-control-v1", "batch_admission_head")["source"]
    assert head["checkpoint"]["batch_quarantine"]["reason"] == "logical_expiry"


def test_materialize_after_d_plus_seven_quarantines_and_blocks_business_io():
    """D 日未物化的 pending → D+7 后 materialize 时 quarantine 触发，
    BusinessIOCounter 阻断业务 I/O，零 business_io = 0。

    验证 G0 expiry 场景的产品化（probe_g0_recovery.py 中已验证）。
    """
    d_start = datetime(2026, 9, 24, 12, 0, tzinfo=BUSINESS_ZONE).astimezone(timezone.utc)
    d8 = datetime(2026, 10, 2, 0, 0, 0, tzinfo=BUSINESS_ZONE).astimezone(timezone.utc)
    store = BatchMemoryStore()
    service = BatchAdmissionCoordinator(
        store, owner_id="quarantine-2", owner_isolated=lambda: True,
        limits=BatchLimits(), clock=_clock_at(d_start),
    )
    collector = BatchAdmissionCollector(service, max_wait_seconds=0.005)
    collector.accept(_make_request("default", "9101-1", "9101-1",
                                       "D 日未物化", d_start))
    collector.close()

    # D+8 materialize_oldest → quarantine 触发，bulk_create 阻断
    service_d8 = BatchAdmissionCoordinator(
        store, owner_id="quarantine-2", owner_isolated=lambda: True,
        limits=BatchLimits(), clock=_clock_at(d8),
    )
    with pytest.raises(AdmissionConflict, match="expired batch is quarantined"):
        service_d8.materialize_oldest()
    head = store.get("news-dedup-control-v1", "batch_admission_head")["source"]
    assert head["checkpoint"]["batch_quarantine"]["reason"] == "logical_expiry"


def test_d_plus_seven_calendar_boundary_uses_business_zone():
    """Asia/Shanghai 跨零点：D 日 23:59 → D+1 00:00 在不同索引但同日；D+7 24:00 才���期。

    expires_at = accepted_at + 7 自然日零点 SHA。
    """
    d_end = datetime(2026, 9, 24, 23, 59, 0, tzinfo=BUSINESS_ZONE).astimezone(timezone.utc)
    expected_expiry = datetime(2026, 10, 1, 0, 0, tzinfo=BUSINESS_ZONE).astimezone(timezone.utc)
    store = BatchMemoryStore()
    service = BatchAdmissionCoordinator(
        store, owner_id="quarantine-3", owner_isolated=lambda: True,
        limits=BatchLimits(), clock=_clock_at(d_end),
    )
    collector = BatchAdmissionCollector(service, max_wait_seconds=0.005)
    receipt = collector.accept(_make_request("default", "9201-1", "9201-1",
                                                "D 日", d_end))
    collector.close()

    head = store.get("news-dedup-control-v1", "batch_admission_head")["source"]
    entry = head["pending"]["batches"][0]["entries"][0]
    # 接受毫秒精度差异；用 datetime.fromisoformat 解析后再比较
    from datetime import datetime as _dt
    actual = _dt.fromisoformat(entry["expires_at"].replace("Z", "+00:00"))
    assert actual == expected_expiry


def test_quarantine_then_recovery_lets_new_owner_recover_quarantined_log():
    """quarantine 后新 owner 接管 → takeover 触发 recover → quarantine 仍阻断业务 I/O。"""
    d_start = datetime(2026, 9, 24, 10, 0, tzinfo=BUSINESS_ZONE).astimezone(timezone.utc)
    d8 = datetime(2026, 10, 2, 0, 0, 0, tzinfo=BUSINESS_ZONE).astimezone(timezone.utc)
    store = BatchMemoryStore()
    service = BatchAdmissionCoordinator(
        store, owner_id="quarantine-4", owner_isolated=lambda: True,
        limits=BatchLimits(), clock=_clock_at(d_start),
    )
    collector = BatchAdmissionCollector(service, max_wait_seconds=0.005)
    collector.accept(_make_request("default", "9301-1", "9301-1",
                                       "D 日 pending", d_start))
    collector.close()

    # 切 D+8 时钟
    service_d8 = BatchAdmissionCoordinator(
        store, owner_id="quarantine-4", owner_isolated=lambda: True,
        limits=BatchLimits(), clock=_clock_at(d8),
    )
    # 触发 quarantine：尝试一次写入
    collector_d8 = BatchAdmissionCollector(service_d8, max_wait_seconds=0.005)
    with pytest.raises(AdmissionConflict, match="expired batch is quarantined"):
        collector_d8.accept(_make_request("default", "9302-1", "9302-1",
                                            "D+8 新", d8))
    collector_d8.close()

    # 新 owner 接管：quarantine 阻断 takeover（quarantine 是终止态）
    service_new = BatchAdmissionCoordinator(
        store, owner_id="quarantine-4-new", owner_isolated=lambda: True,
        limits=BatchLimits(), clock=_clock_at(d8),
    )
    with pytest.raises(AdmissionConflict, match="batch log is quarantined"):
        service_new.takeover()
    # 即使 takeover 失败，quarantine 仍生效
    head = store.get("news-dedup-control-v1", "batch_admission_head")["source"]
    assert head["checkpoint"]["batch_quarantine"]["reason"] == "logical_expiry"