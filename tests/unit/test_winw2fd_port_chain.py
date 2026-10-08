"""W2Fδ 修复窗口红测：条38（admission_port 4 处 from None→from error）+ 条39 AdmissionPortLike 删除。

只新建不改既有套件。
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from news_flash_dedup.admission import (
    AdmissionConflict, AdmissionRequest, IdentityConflict,
)
from news_flash_dedup.api.contract import Backpressure, Unavailable
from news_flash_dedup.batch_admission import (
    AdmissionCapacityExceeded, BatchAdmissionCoordinator, BatchLimits,
)
from news_flash_dedup.batch_collector import BatchAdmissionCollector
from news_flash_dedup.product.admission_port import CollectorAdmissionPort

from test_batch_admission import BatchMemoryStore


def _request():
    return AdmissionRequest(
        scope_id="default", request_id="1-1", item_id="1-1", text="正文",
        received_at=datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc),
        schema_version="1", pipeline_version="w2fd-v1", embedding_space_id="none",
        delivery_route_ref="route-v1", trace_id="trace-1")


def _port():
    store = BatchMemoryStore()
    coordinator = BatchAdmissionCoordinator(
        store, owner_id="w2fd", owner_isolated=lambda: True,
        limits=BatchLimits(),
        clock=lambda: datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc))
    collector = BatchAdmissionCollector(
        coordinator, max_wait_seconds=0.005, request_timeout_seconds=0.5,
        close_timeout_seconds=0.05)
    return CollectorAdmissionPort(collector), collector


def _raise_via(collector, error):
    def boom(request):
        raise error
    collector.accept = boom  # type: ignore[method-assign]


# ---------- 条38：四处替换型 raise 必须保链（__cause__ 为原异常） ----------

def test_log_item_limit_backpressure_keeps_chain():
    port, collector = _port()
    error = AdmissionCapacityExceeded("log_item_limit")
    _raise_via(collector, error)
    with pytest.raises(Backpressure) as captured:
        port.accept(_request())
    assert captured.value.__cause__ is error
    collector.close()


def test_batch_byte_limit_still_passes_through():
    """条38 对照：batch_byte_limit 透传（合同层映射 413）不受保链改动影响。"""
    port, collector = _port()
    error = AdmissionCapacityExceeded("batch_byte_limit")
    _raise_via(collector, error)
    with pytest.raises(AdmissionCapacityExceeded) as captured:
        port.accept(_request())
    assert captured.value is error
    collector.close()


def test_collector_queue_timeout_backpressure_keeps_chain():
    from news_flash_dedup.batch_collector import CollectorRejected
    port, collector = _port()
    error = CollectorRejected("queue_timeout")
    _raise_via(collector, error)
    with pytest.raises(Backpressure) as captured:
        port.accept(_request())
    assert captured.value.__cause__ is error
    collector.close()


def test_collector_closed_unavailable_keeps_chain():
    from news_flash_dedup.batch_collector import CollectorRejected
    port, collector = _port()
    error = CollectorRejected("closed")
    _raise_via(collector, error)
    with pytest.raises(Unavailable) as captured:
        port.accept(_request())
    assert captured.value.__cause__ is error
    collector.close()


def test_admission_conflict_unavailable_keeps_chain():
    port, collector = _port()
    error = AdmissionConflict("synthetic durable divergence")
    _raise_via(collector, error)
    with pytest.raises(Unavailable) as captured:
        port.accept(_request())
    assert captured.value.__cause__ is error
    collector.close()


def test_identity_conflict_still_passes_through():
    """条38 对照：透传路径（IdentityConflict/413/409）不受保链改动影响。"""
    port, collector = _port()
    _raise_via(collector, IdentityConflict("synthetic identity clash"))
    with pytest.raises(IdentityConflict):
        port.accept(_request())
    collector.close()


# ---------- 条39：AdmissionPortLike 零消费删除 ----------

def test_admission_port_like_removed():
    import news_flash_dedup.product.admission_port as module
    assert not hasattr(module, "AdmissionPortLike")
    assert "AdmissionPortLike" not in module.__all__
