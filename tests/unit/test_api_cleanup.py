from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from news_flash_dedup.api.cleanup import create_cleanup_app
from news_flash_dedup.lifecycle import CleanupAuditLog


class FakeCleanupPort:
    def __init__(self):
        self.calls = []

    def dry_run(self, today, operator):
        self.calls.append(("dry_run", today.isoformat(), operator))
        return {"today": today.isoformat(), "operator": operator,
                "would_delete": ["news-dedup-items-v1-2026.09.18",
                                  "news-dedup-audits-v1-2026.09.18"],
                "absent": [], "dry_run": True}

    def execute(self, today, operator, dry_run):
        self.calls.append(("execute", today.isoformat(), operator, dry_run))
        return {"today": today.isoformat(), "operator": operator, "dry_run": dry_run,
                "deleted": ["news-dedup-items-v1-2026.09.18"],
                "skipped_absent": ["news-dedup-audits-v1-2026.09.18"]}


def _always_admin(_request):
    return True


def _never_admin(_request: object) -> bool:
    return False


def _client(port=None, auth=_always_admin):
    return TestClient(create_cleanup_app(cleanup_port=port or FakeCleanupPort(),
                                          admin_authenticated=auth))


def test_factory_rejects_missing_dependencies():
    with pytest.raises(ValueError):
        create_cleanup_app(cleanup_port=None, admin_authenticated=_always_admin)
    with pytest.raises(ValueError):
        create_cleanup_app(cleanup_port=FakeCleanupPort(), admin_authenticated=None)


def test_dry_run_endpoint_returns_would_delete_list():
    port = FakeCleanupPort()
    client = _client(port)
    response = client.post("/v1/api/admin/cleanup/dry_run",
                            json={"today": "2026-09-25", "operator": "admin-test"})
    assert response.status_code == 200
    body = response.json()
    assert body["today"] == "2026-09-25"
    assert body["operator"] == "admin-test"
    assert "news-dedup-items-v1-2026.09.18" in body["would_delete"]
    assert body["dry_run"] is True


def test_dry_run_endpoint_returns_403_when_not_admin():
    client = _client(auth=_never_admin)
    response = client.post("/v1/api/admin/cleanup/dry_run",
                            json={"today": "2026-09-25"})
    assert response.status_code == 403


def test_execute_endpoint_returns_deletion_report():
    port = FakeCleanupPort()
    client = _client(port)
    response = client.post("/v1/api/admin/cleanup/execute",
                            json={"today": "2026-09-25", "dry_run": False})
    assert response.status_code == 200
    body = response.json()
    assert body["dry_run"] is False
    assert "news-dedup-items-v1-2026.09.18" in body["deleted"]


def test_execute_endpoint_returns_400_on_invalid_cleanup_target():
    class _BoomPort(FakeCleanupPort):
        def execute(self, today, operator, dry_run):
            raise ValueError("refused invalid cleanup target: malicious-index")

    client = _client(_BoomPort())
    response = client.post("/v1/api/admin/cleanup/execute",
                            json={"today": "2026-09-25"})
    assert response.status_code == 400
    assert "refused invalid cleanup target" in response.json()["error"]


def test_health_endpoint_returns_200_when_admin():
    client = _client()
    response = client.get("/v1/api/admin/cleanup/health")
    assert response.status_code == 200


def test_cleanup_endpoints_not_exposed_on_business_prefix():
    """业务 POST `/v1/api/task/run` 不会进入 admin cleanup 路由。"""
    from news_flash_dedup.api.http import create_app
    from news_flash_dedup.api.contract import IngressContext
    business_app = create_app(admission_port=type("P", (), {
        "accept": lambda r: None,
    })(), context_provider=lambda r: IngressContext(
        scope_id="default",
        received_at=__import__("datetime").datetime(2026, 9, 24, 12, 0,
                                             tzinfo=__import__("datetime").timezone.utc),
        schema_version="1", pipeline_version="v1",
        embedding_space_id="none", delivery_route_ref="r",
    ), max_http_bytes=4096)
    with TestClient(business_app) as business, _client() as admin:
        business_response = business.post("/v1/api/admin/cleanup/execute", json={})
        assert business_response.status_code == 404
        admin_response = admin.post("/v1/api/admin/cleanup/execute",
                                     json={"today": "2026-09-25"})
        assert admin_response.status_code == 200


def test_audit_log_records_operator_and_dates():
    """CleanupAuditLog 必须记录 operator 与日期。"""
    from datetime import date as _date
    log = CleanupAuditLog(operator="admin-test")
    from news_flash_dedup.lifecycle import CleanupTarget
    target = CleanupTarget.for_business_date(_date(2026, 9, 18), "items")
    log.record_deletion(_date(2026, 9, 25), target, ok=True)
    assert len(log.deletion_runs) == 1
    entry = log.deletion_runs[0]
    assert entry["operator"] == "admin-test"
    assert entry["index_name"] == "news-dedup-items-v1-2026.09.18"
    assert entry["ok"] is True