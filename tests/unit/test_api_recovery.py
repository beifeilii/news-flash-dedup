from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from news_flash_dedup.api.recovery import create_recovery_app


class _StubPort:
    def __init__(self):
        self.takeover_called = 0
        self.snapshot_called = 0

    def takeover(self):
        self.takeover_called += 1
        return {"status": "recovered", "owner_id": "new-owner",
                "last_allocated_seq": 5, "last_materialized_seq": 4}

    def snapshot(self):
        self.snapshot_called += 1
        return {"owner_id": "current", "last_allocated_seq": 5,
                "last_materialized_seq": 4, "pending_batches": 1}


def _always_admin(_request):
    return True


def _never_admin(_request):
    return False


def _client(port=None, auth=_always_admin):
    return TestClient(create_recovery_app(recovery_port=port or _StubPort(),
                                          admin_authenticated=auth))


def test_factory_rejects_missing_dependencies():
    with pytest.raises(ValueError):
        create_recovery_app(recovery_port=None, admin_authenticated=_always_admin)
    with pytest.raises(ValueError):
        create_recovery_app(recovery_port=_StubPort(), admin_authenticated=None)


def test_post_takeover_returns_recovery_report_when_admin():
    port = _StubPort()
    client = _client(port)
    response = client.post("/v1/api/admin/takeover")
    assert response.status_code == 200
    assert response.json()["status"] == "recovered"
    assert port.takeover_called == 1


def test_post_takeover_returns_403_when_not_admin():
    client = _client(auth=_never_admin)
    response = client.post("/v1/api/admin/takeover")
    assert response.status_code == 403


def test_get_health_returns_snapshot_when_admin():
    port = _StubPort()
    client = _client(port)
    response = client.get("/v1/api/admin/health")
    assert response.status_code == 200
    assert response.json()["owner_id"] == "current"
    assert response.json()["pending_batches"] == 1


def test_post_takeover_returns_503_when_port_raises():
    class _BoomPort(_StubPort):
        def takeover(self):
            super().takeover()
            raise RuntimeError("isolation failed")

    client = _client(_BoomPort())
    response = client.post("/v1/api/admin/takeover")
    assert response.status_code == 503
    assert "isolation failed" not in response.text


def test_get_health_returns_503_when_port_raises():
    class _BoomPort(_StubPort):
        def snapshot(self):
            super().snapshot()
            raise RuntimeError("realtime read failed")

    client = _client(_BoomPort())
    response = client.get("/v1/api/admin/health")
    assert response.status_code == 503
    assert "realtime read failed" not in response.text


def test_admin_endpoints_are_not_exposed_on_business_prefix():
    """业务 POST `/v1/api/task/run` 不会进入 admin 路由；admin 路由独立。"""
    from news_flash_dedup.api.http import create_app
    from news_flash_dedup.api.contract import IngressContext

    port = _StubPort()
    admin_client = _client(port)
    business_app = create_app(admission_port=type("P", (), {
        "accept": lambda r: None,
    })(), context_provider=lambda r: IngressContext(
        scope_id="default",
        received_at=__import__("datetime").datetime(2026, 9, 24, 12, 0,
                                             tzinfo=__import__("datetime").timezone.utc),
        schema_version="1", pipeline_version="v1",
        embedding_space_id="none", delivery_route_ref="r",
    ), max_http_bytes=4096)
    with TestClient(business_app) as business, admin_client:
        # business 不应暴露 admin takeover
        business_response = business.post("/v1/api/admin/takeover")
        assert business_response.status_code == 404
        # admin 暴露 takeover
        admin_response = admin_client.post("/v1/api/admin/takeover")
        assert admin_response.status_code == 200