from __future__ import annotations

import time
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from news_flash_dedup.admission import (
    AdmissionReceipt, AdmissionRequest, AdmissionUnknown, IdentityConflict,
)
from news_flash_dedup.api.contract import Backpressure, IngressContext, Unavailable
from news_flash_dedup.api.http import create_app
from news_flash_dedup.batch_admission import (
    AdmissionCapacityExceeded, BatchAdmissionCoordinator, BatchLimits,
)
from news_flash_dedup.batch_collector import BatchAdmissionCollector, CollectorRejected
from news_flash_dedup.product.admission_port import CollectorAdmissionPort

from test_batch_admission import BatchMemoryStore


def _new_port_and_collector(*, owner="prod-1", max_log_items=64, max_wait=0.005):
    store = BatchMemoryStore()
    service = BatchAdmissionCoordinator(
        store, owner_id=owner, owner_isolated=lambda: True,
        limits=BatchLimits(max_log_items=max_log_items),
        clock=lambda: datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc),
    )
    collector = BatchAdmissionCollector(service, max_wait_seconds=max_wait,
                                        max_queue_items=8, max_queue_bytes=4 * 1024,
                                        request_timeout_seconds=0.5,
                                        close_timeout_seconds=0.05)
    return store, collector


def _context(scope="default"):
    return IngressContext(
        scope_id=scope,
        received_at=datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc),
        schema_version="1", pipeline_version="prod-v1",
        embedding_space_id="none", delivery_route_ref="prod-route-v1",
        max_text_codepoints=20000, max_http_bytes=256 * 1024,
    )


def _payload(request_id="1-1", text="新闻正文", trace_id="trace-1"):
    return {"traceId": trace_id, "requestId": request_id, "articleId": None,
            "rewrittenId": "123456", "integrationId": None,
            "source": {"title": "", "body": "", "pageDate": "2026-09-24"},
            "rewrite": {"title": "", "body": text}}


@pytest.mark.parametrize("error,expected_status", [
    (AdmissionCapacityExceeded("batch_byte_limit"), 413),
    (AdmissionCapacityExceeded("log_byte_limit"), 413),
    (CollectorRejected("request_too_large"), 413),
    # 非字节容量直达合同层→503（生产路径 log_item_limit 经端口层转
    # Backpressure→429，本用例测的是"绕过端口层"的合同层兜底口径）
    (AdmissionCapacityExceeded("log_item_limit"), 503),
    (CollectorRejected("queue_full"), 503),
])
def test_contract_maps_oversize_to_413_not_503(error, expected_status):
    """R9 外部审核 F8b 回归（主窗口 06:4x）：AdmissionCapacityExceeded /
    CollectorRejected 均为 AdmissionConflict 子类，原先穿透 except
    (Unavailable, AdmissionConflict)→503——与 admission_port docstring 及
    P07 设计文档声称的 413 不符。修复后字节超限语义→413，其余容量→503。"""
    from news_flash_dedup.api.contract import submit

    class _Rejecting:
        def accept(self, request):
            raise error

    response = submit(_payload(), _context(), _Rejecting())
    assert response.status == expected_status


def test_collector_port_accepts_and_returns_receipt():
    _, collector = _new_port_and_collector()
    port = CollectorAdmissionPort(collector)
    receipt = port.accept(AdmissionRequest(
        scope_id="default", request_id="1-1", item_id="1-1", text="正文",
        received_at=datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc),
        schema_version="1", pipeline_version="prod-v1", embedding_space_id="none",
        delivery_route_ref="prod-route-v1", trace_id="trace-1",
    ))
    assert isinstance(receipt, AdmissionReceipt)
    assert receipt.arrival_seq == 1
    collector.close()


def test_collector_port_raises_identity_conflict_on_same_id_different_text():
    """同 ID 异文：先把第一次写完持久化，再创建第二批 collector 看到 pending/物化记录。"""
    store1, collector1 = _new_port_and_collector()
    port1 = CollectorAdmissionPort(collector1)
    port1.accept(AdmissionRequest(
        scope_id="default", request_id="1-1", item_id="1-1", text="原文A",
        received_at=datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc),
        schema_version="1", pipeline_version="prod-v1", embedding_space_id="none",
        delivery_route_ref="prod-route-v1", trace_id="trace-1",
    ))
    collector1.close()
    # 等持久 log 收敛
    time.sleep(0.05)
    service2 = BatchAdmissionCoordinator(
        store1, owner_id="prod-1", owner_isolated=lambda: True,
        limits=BatchLimits(),
        clock=lambda: datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc),
    )
    collector2 = BatchAdmissionCollector(service2, max_wait_seconds=0.005,
                                         request_timeout_seconds=0.5)
    port2 = CollectorAdmissionPort(collector2)
    with pytest.raises(IdentityConflict):
        port2.accept(AdmissionRequest(
            scope_id="default", request_id="1-1", item_id="1-1", text="原文B",
            received_at=datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc),
            schema_version="1", pipeline_version="prod-v1", embedding_space_id="none",
            delivery_route_ref="prod-route-v1", trace_id="trace-1",
        ))
    collector2.close()


def test_collector_port_constructor_rejects_non_collector():
    with pytest.raises(TypeError):
        CollectorAdmissionPort("not a collector")  # type: ignore[arg-type]


def test_collector_port_maps_unknown_to_admission_unknown():
    """用 monkeypatch 替换 collector.accept 返回 AdmissionUnknown。"""
    _, collector = _new_port_and_collector()
    port = CollectorAdmissionPort(collector)
    original = collector.accept
    def boom(request):
        raise AdmissionUnknown("unexpected CAS loss")
    collector.accept = boom  # type: ignore[method-assign]
    with pytest.raises(AdmissionUnknown):
        port.accept(AdmissionRequest(
            scope_id="default", request_id="1-1", item_id="1-1", text="正文",
            received_at=datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc),
            schema_version="1", pipeline_version="prod-v1", embedding_space_id="none",
            delivery_route_ref="prod-route-v1", trace_id="trace-1",
        ))
    collector.accept = original  # type: ignore[method-assign]
    collector.close()


def test_post_endpoint_returns_202_for_distinct_requests():
    _, collector = _new_port_and_collector()
    port = CollectorAdmissionPort(collector)
    client = TestClient(create_app(admission_port=port, context_provider=lambda r: _context(),
                                   max_http_bytes=256 * 1024))
    try:
        first = client.post("/v1/api/task/run", json=_payload(request_id="1-1", text="新闻一"))
        second = client.post("/v1/api/task/run", json=_payload(request_id="2-1", text="新闻二"))
        assert first.status_code == 202, first.text
        assert second.status_code == 202, second.text
        assert first.json()["duplicate"] is False
        assert second.json()["duplicate"] is False
        assert first.json()["requestId"] == "1-1"
        assert second.json()["requestId"] == "2-1"
    finally:
        collector.close()


def test_post_endpoint_returns_409_for_same_id_different_text():
    store1, collector1 = _new_port_and_collector()
    port1 = CollectorAdmissionPort(collector1)
    client1 = TestClient(create_app(admission_port=port1, context_provider=lambda r: _context(),
                                    max_http_bytes=256 * 1024))
    try:
        first = client1.post("/v1/api/task/run", json=_payload(request_id="7-1", text="原文A"))
        assert first.status_code == 202
    finally:
        collector1.close()
    time.sleep(0.05)
    service2 = BatchAdmissionCoordinator(
        store1, owner_id="prod-1", owner_isolated=lambda: True,
        limits=BatchLimits(),
        clock=lambda: datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc),
    )
    collector2 = BatchAdmissionCollector(service2, max_wait_seconds=0.005)
    port2 = CollectorAdmissionPort(collector2)
    client2 = TestClient(create_app(admission_port=port2, context_provider=lambda r: _context(),
                                    max_http_bytes=256 * 1024))
    try:
        second = client2.post("/v1/api/task/run", json=_payload(request_id="7-1", text="原文B"))
        assert second.status_code == 409
        assert "请求身份与已受理正文冲突" in second.json()["message"]
    finally:
        collector2.close()


def test_post_endpoint_returns_503_when_unavailable():
    class _ClosedPort:
        def accept(self, request):
            raise Unavailable("collector not accepting")

    client = TestClient(create_app(admission_port=_ClosedPort(),
                                   context_provider=lambda r: _context(),
                                   max_http_bytes=256 * 1024))
    response = client.post("/v1/api/task/run", json=_payload())
    assert response.status_code == 503


def test_post_endpoint_returns_500_on_admission_unknown():
    _, collector = _new_port_and_collector()
    port = CollectorAdmissionPort(collector)
    original = collector.accept
    def boom(request):
        raise AdmissionUnknown("CAS lost")
    collector.accept = boom  # type: ignore[method-assign]
    client = TestClient(create_app(admission_port=port, context_provider=lambda r: _context(),
                                   max_http_bytes=256 * 1024))
    try:
        response = client.post("/v1/api/task/run", json=_payload())
        assert response.status_code == 500
    finally:
        collector.accept = original  # type: ignore[method-assign]
        collector.close()