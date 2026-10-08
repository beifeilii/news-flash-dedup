import asyncio

import pytest
from fastapi.testclient import TestClient

from news_flash_dedup.admission import AdmissionReceipt
from news_flash_dedup.api.contract import IngressContext, safe_log_fields
from news_flash_dedup.api.http import create_app
from test_api_contract import context, payload, receipt


class Port:
    def __init__(self):
        self.requests = []
        self.in_event_loop = []

    def accept(self, request):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            self.in_event_loop.append(False)
        else:
            self.in_event_loop.append(True)
        self.requests.append(request)
        return receipt(reused=len(self.requests) > 1)


def app(port=None, provider=None, max_bytes=4096):
    return create_app(admission_port=port or Port(),
                      context_provider=provider or (lambda request: context()),
                      max_http_bytes=max_bytes)


def test_factory_requires_explicit_trusted_dependencies():
    with pytest.raises(ValueError):
        create_app(admission_port=None, context_provider=lambda request: context(), max_http_bytes=4096)
    with pytest.raises(ValueError):
        create_app(admission_port=Port(), context_provider=None, max_http_bytes=4096)
    with pytest.raises(ValueError):
        create_app(admission_port=Port(), context_provider=lambda request: context(), max_http_bytes=None)


def test_post_returns_existing_202_and_runs_blocking_accept_off_event_loop():
    port = Port()
    with TestClient(app(port)) as client:
        first = client.post("/v1/api/task/run", json=payload())
        second = client.post("/v1/api/task/run", json=payload())
    assert first.status_code == second.status_code == 202
    assert first.json()["duplicate"] is False
    assert second.json()["duplicate"] is True
    assert port.in_event_loop == [False, False]
    assert port.requests[0].scope_id == "trusted-scope"
    assert port.requests[0].text == payload()["rewrite"]["body"]


def test_post_rejects_absent_or_invalid_trusted_context_before_admission():
    port = Port()
    with TestClient(app(port, provider=lambda request: None)) as client:
        response = client.post("/v1/api/task/run", json=payload())
    assert response.status_code == 503
    assert port.requests == []


def test_post_limits_raw_http_bytes_and_rejects_malformed_json():
    port = Port()
    with TestClient(app(port, max_bytes=50)) as client:
        oversize = client.post("/v1/api/task/run", json=payload())
    assert oversize.status_code == 413
    assert port.requests == []
    with TestClient(app(port)) as client:
        invalid = client.post("/v1/api/task/run", content=b"{broken", headers={"content-type": "application/json"})
    assert invalid.status_code == 422
    assert port.requests == []


def test_health_is_process_only_and_does_not_claim_dependency_ready():
    with TestClient(app()) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "alive"}


def test_log_fields_drop_untrusted_trace_even_when_ascii():
    safe = safe_log_fields({"stage": "accepted", "status": 202,
                            "trace_id": "password123", "record_id": "password123",
                            "version": "password123", "error_code": "password123"})
    assert safe == {"stage": "accepted", "status": 202}
