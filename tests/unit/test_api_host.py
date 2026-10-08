"""B4/P21 红测：api/host.py 组装根（R154 裁定 P-Q8 并入 B4 施工窗）。

钉装层面：
- 模式闸解析落点：env 缺省=fixed；shadow/live 解析；未知值 RecallModeInvalid
- fail-closed：es_client/index_prefix/status_authenticated 缺省即拒；
  live 缺 commit_store 拒；shadow 缺 artifact_sink 拒；受理三件套部分注入拒
- 组装产物：HostAssembly 暴露 mode/query_app/readiness_port/query_port/worker；
  worker 模式与闸一致；fixed 默认下 /ready 经默认探针可达三态
- 端点经组装根连通（TestClient 驱动 /ready 与 /v1/api/task/{id}）
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from news_flash_dedup.admission import CONTROL_INDEX, REQUEST_INDEX
from news_flash_dedup.batch_admission import HEAD_ID, _keys
from news_flash_dedup.es_admission_schema import day_index
from news_flash_dedup.api.host import HostAssembly, create_host
from news_flash_dedup.recall.service import NullVectorSearcher, RecallModeInvalid

from b4_fake_es import FakeESClient

DAY = "2026-09-29"
PREFIX = "p01-batch-b4host-"
SCOPE = "default"
NOW = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)


def _clock() -> datetime:
    return NOW


def _host(client=None, **overrides):
    base = dict(
        es_client=client if client is not None else FakeESClient(),
        index_prefix=PREFIX,
        status_authenticated=lambda request: True,
        clock=_clock,
    )
    base.update(overrides)
    return create_host(**base)


# ---------- 模式闸解析落点 ----------

def test_mode_defaults_fixed_when_env_unset():
    assembly = _host(env={})
    assert assembly.mode == "fixed"
    assert assembly.worker.mode == "fixed"
    assert assembly.readiness_port.mode == "fixed"


@pytest.mark.parametrize("mode", ["fixed", "shadow", "live"])
def test_mode_parsed_from_env(mode):
    overrides = {"env": {"DEDUP_RECALL_MODE": mode}}
    if mode == "shadow":
        from types import SimpleNamespace
        overrides["artifact_sink"] = SimpleNamespace(
            emit=lambda kind, payload: None)
    if mode == "live":
        from news_flash_dedup.commit.fake_store import FakeCommitStore
        overrides["commit_store"] = FakeCommitStore()
    assembly = _host(**overrides)
    assert assembly.mode == mode
    assert assembly.worker.mode == mode


def test_unknown_mode_rejected():
    with pytest.raises(RecallModeInvalid):
        _host(env={"DEDUP_RECALL_MODE": "SHADOW"})


# ---------- fail-closed 注入校验 ----------

def test_factory_requires_es_client_and_prefix_and_auth():
    with pytest.raises(ValueError):
        create_host(es_client=None, index_prefix=PREFIX,
                    status_authenticated=lambda request: True)
    with pytest.raises(ValueError):
        create_host(es_client=FakeESClient(), index_prefix="news-dedup-",
                    status_authenticated=lambda request: True)
    with pytest.raises(ValueError):
        create_host(es_client=FakeESClient(), index_prefix=PREFIX,
                    status_authenticated=None)


def test_live_requires_commit_store_and_shadow_requires_sink():
    with pytest.raises(ValueError):
        _host(env={"DEDUP_RECALL_MODE": "live"})
    with pytest.raises(ValueError):
        _host(env={"DEDUP_RECALL_MODE": "shadow"})


def test_admission_partial_injection_rejected():
    with pytest.raises(ValueError):
        _host(admission_port=object())          # 缺 context_provider/max_http_bytes


def test_admission_full_injection_builds_app():
    class _Port:
        def accept(self, request):
            raise NotImplementedError

    from news_flash_dedup.api.contract import IngressContext

    def provider(request):
        return IngressContext(scope_id="s", received_at=NOW, schema_version="v1",
                              pipeline_version="dedup_v1", embedding_space_id="x",
                              delivery_route_ref="r")

    assembly = _host(admission_port=_Port(), context_provider=provider,
                     max_http_bytes=4096)
    assert assembly.admission_app is not None


def test_admission_absent_by_default():
    assert _host().admission_app is None


# ---------- 组装产物与端点连通 ----------

def test_assembly_exposes_wired_components():
    assembly = _host()
    assert isinstance(assembly, HostAssembly)
    assert assembly.query_app is not None
    assert assembly.query_port is not None
    assert assembly.readiness_port is not None
    assert assembly.worker is not None
    assert isinstance(assembly.recall_service.vector_searcher, NullVectorSearcher)


def test_ready_endpoint_through_assembly_with_default_probes():
    client = FakeESClient()
    client.put(PREFIX + CONTROL_INDEX, HEAD_ID, {
        "kind": "head", "scope_id": SCOPE, "owner_id": "o",
        "last_allocated_seq": 0, "last_materialized_seq": 0,
        "pending": {"version": "batch-v1", "batches": []},
        "updated_at": "2026-09-29T01:00:00.000000Z"})
    assembly = _host(client=client, collector_alive=lambda: True,
                     delivery_route_configured=True)
    with TestClient(assembly.query_app) as http:
        response = http.get("/ready")
    assert response.status_code == 200
    assert response.json() == {"ready": True, "issues": [], "mode": "fixed"}


def test_ready_endpoint_reports_fatal_when_control_read_fails():
    assembly = _host(collector_alive=lambda: True, delivery_route_configured=True)
    with TestClient(assembly.query_app) as http:
        response = http.get("/ready")
    assert response.status_code == 503
    body = response.json()
    assert body["ready"] is False
    assert "es_control_read_failed" in body["issues"]


def test_task_query_through_assembly():
    client = FakeESClient()
    client.put(PREFIX + REQUEST_INDEX, _keys(SCOPE, "9-1", "9-1")[1], {
        "kind": "request", "scope_id": SCOPE, "request_id": "9-1",
        "item_id": "9-1", "record_id": "a" * 64,
        "business_fingerprint": "b" * 64, "business_date": DAY, "arrival_seq": 1,
        "expires_at": "2026-10-06T00:00:00.000000Z",
        "target_index": day_index(DAY)})
    client.put(PREFIX + day_index(DAY), "a" * 64, {
        "scope_id": SCOPE, "request_id": "9-1", "item_id": "9-1",
        "record_id": "a" * 64, "business_date": DAY, "arrival_seq": 1,
        "accepted_at": "2026-09-29T01:00:01.000000Z",
        "expires_at": "2026-10-06T00:00:00.000000Z", "text": "正文。",
        "raw_hash": "c" * 64, "task_state": "accepted",
        "delivery_state": "not_ready", "result": None,
        "diagnostics": {"delivery": {"route_ref": "r", "trace_id": "t"}}})
    assembly = _host(client=client)
    with TestClient(assembly.query_app) as http:
        response = http.get("/v1/api/task/9-1")
    assert response.status_code == 200
    body = response.json()
    assert body["taskState"] == "accepted"
    assert body["result"] is None
