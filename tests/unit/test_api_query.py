"""B4/P21 红测（log\设计-P21接线包.md §八-8.1）：Q1 查询端点层 api/query.py。

红测清单映射：
- R-Q1  工厂 fail-closed：query_port/readiness_port/status_authenticated 缺省/不可调→ValueError
- R-Q2  状态码矩阵：404 壳两键（无 details）/503/422/403 壳形钉死
- R-Q3  终态命中投影：result 键集恰五字段、值透传；外层恰 12 字段、
        无 mode/diagnostics/record_id
- R-Q4  端点+端口联钉：真 TaskQueryPort（fake ES 仓储）喂终态文档→200 投影
- R-Q6  未知 task_id 404 不泄露内部；message 含 task_id
- R-Q8  /ready 三态（端点半）：fatal→503 ready=false / degraded→200 ready=true 带码 /
        全绿→200；mode 回显
- R-Q10 trace 透传：X-Trace-ID 回显/生成
- R-Q11 鉴权先于路径参数校验（未鉴权+非法格式→403）
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from news_flash_dedup.admission import CONTROL_INDEX, REQUEST_INDEX
from news_flash_dedup.batch_admission import HEAD_ID, _keys
from news_flash_dedup.batch_es_store import ElasticsearchBatchStore
from news_flash_dedup.es_admission_schema import day_index
from news_flash_dedup.api.query import create_query_app
from news_flash_dedup.product.task_query import (
    QueryReadError,
    QueryView,
    TaskQueryPort,
)

from b4_fake_es import FakeESClient

DAY = "2026-09-29"
PREFIX = "p01-batch-b4api-"
SCOPE = "default"
NOW = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)
FUTURE = "2026-10-06T00:00:00.000000Z"


def _view(**overrides) -> QueryView:
    base = dict(
        taskId="9-1", requestId="9-1", articleId=None, traceId="trace-9",
        taskState="succeeded", deliveryState="delivered",
        createdAt="2026-09-29T01:00:01.000000Z",
        startedAt="2026-09-29T01:01:00.000000Z",
        completedAt="2026-09-29T01:02:03.000000Z",
        callbackDelivered=True,
        result={"item_id": "9-1", "text": "正文。", "decision": "重复",
                "duplicate_ids": ["9-0"], "reason": "文本完全一致。"},
        error=None,
    )
    base.update(overrides)
    return QueryView(**base)


class _FakeQueryPort:
    def __init__(self, outcome) -> None:
        self.outcome = outcome
        self.calls: list[str] = []
        self.in_event_loop: list[bool] = []

    def lookup(self, task_id: str):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            self.in_event_loop.append(False)
        else:
            self.in_event_loop.append(True)
        self.calls.append(task_id)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


class _FakeReadinessPort:
    def __init__(self, ready: bool, codes: list[str], mode: str = "fixed") -> None:
        self._ready = ready
        self._codes = codes
        self.mode = mode
        self.calls = 0

    def issues(self):
        self.calls += 1
        return self._ready, list(self._codes)


def _app(query_port=None, readiness_port=None, auth=lambda request: True):
    return create_query_app(
        query_port=query_port if query_port is not None else _FakeQueryPort(None),
        readiness_port=(readiness_port if readiness_port is not None
                        else _FakeReadinessPort(True, [])),
        status_authenticated=auth,
    )


# ---------- R-Q1 工厂 fail-closed ----------

def test_rq1_factory_requires_three_injections():
    with pytest.raises(ValueError):
        create_query_app(query_port=None,
                         readiness_port=_FakeReadinessPort(True, []),
                         status_authenticated=lambda request: True)
    with pytest.raises(ValueError):
        create_query_app(query_port=_FakeQueryPort(None),
                         readiness_port=None,
                         status_authenticated=lambda request: True)
    with pytest.raises(ValueError):
        create_query_app(query_port=_FakeQueryPort(None),
                         readiness_port=_FakeReadinessPort(True, []),
                         status_authenticated=None)
    with pytest.raises(ValueError):  # lookup 不可调
        create_query_app(query_port=object(),
                         readiness_port=_FakeReadinessPort(True, []),
                         status_authenticated=lambda request: True)
    with pytest.raises(ValueError):  # issues 不可调
        create_query_app(query_port=_FakeQueryPort(None),
                         readiness_port=object(),
                         status_authenticated=lambda request: True)


# ---------- R-Q2 状态码矩阵 ----------

def test_rq2_404_shell_has_exactly_two_keys_and_embeds_task_id():
    with TestClient(_app(query_port=_FakeQueryPort(None))) as client:
        response = client.get("/v1/api/task/9-99")
    assert response.status_code == 404
    body = response.json()
    assert set(body) == {"accepted", "message"}          # 两键、无 details
    assert body["accepted"] is False
    assert "9-99" in body["message"]


def test_rq2_503_on_read_error_never_disguised_as_404():
    with TestClient(_app(query_port=_FakeQueryPort(QueryReadError("boom")))) as client:
        response = client.get("/v1/api/task/9-1")
    assert response.status_code == 503
    assert response.json() == {"accepted": False, "message": "服务暂时不可用",
                               "details": []}


@pytest.mark.parametrize("bad_id", ["abc", "1-", "-1", "9_1", "0-1", "1-0",
                                    "9-1-1", " 9-1", "9-1 ", "x" * 65])
def test_rq2_422_on_invalid_task_id_format(bad_id):
    port = _FakeQueryPort(None)
    with TestClient(_app(query_port=port)) as client:
        response = client.get("/v1/api/task/" + bad_id)
    assert response.status_code == 422
    assert response.json() == {"accepted": False, "message": "请求字段无效",
                               "details": []}
    assert port.calls == []                                 # 校验先于端口


def test_rq2_403_admin_shell_when_unauthenticated():
    with TestClient(_app(auth=lambda request: False)) as client:
        response = client.get("/v1/api/task/9-1")
    assert response.status_code == 403
    assert response.json() == {"error": "admin required"}


# ---------- R-Q3 终态命中投影 ----------

def test_rq3_terminal_projection_passthrough_exact_outer_keys():
    with TestClient(_app(query_port=_FakeQueryPort(_view()))) as client:
        response = client.get("/v1/api/task/9-1")
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"taskId", "requestId", "articleId", "traceId",
                         "taskState", "deliveryState", "createdAt", "startedAt",
                         "completedAt", "callbackDelivered", "result", "error"}
    for leaked in ("mode", "diagnostics", "record_id", "recordId"):
        assert leaked not in body
    assert set(body["result"]) == {"item_id", "text", "decision",
                                   "duplicate_ids", "reason"}
    assert body["result"]["item_id"] == "9-1"
    assert body["result"]["text"] == "正文。"
    assert body["result"]["duplicate_ids"] == ["9-0"]
    assert body["articleId"] is None
    assert body["error"] is None


def test_rq3_non_terminal_result_null():
    view = _view(taskState="running", deliveryState="not_ready", startedAt=None,
                 completedAt=None, callbackDelivered=None, result=None)
    with TestClient(_app(query_port=_FakeQueryPort(view))) as client:
        response = client.get("/v1/api/task/9-1")
    assert response.status_code == 200
    body = response.json()
    assert body["result"] is None
    assert body["callbackDelivered"] is None
    assert body["startedAt"] is None


# ---------- R-Q4 端点+端口联钉（真 TaskQueryPort） ----------

def test_rq4_endpoint_wired_to_real_task_query_port():
    client = FakeESClient()
    request_key = _keys(SCOPE, "9-1", "9-1")[1]
    client.put(PREFIX + REQUEST_INDEX, request_key, {
        "kind": "request", "scope_id": SCOPE, "request_id": "9-1",
        "item_id": "9-1", "record_id": "a" * 64,
        "business_fingerprint": "b" * 64, "business_date": DAY, "arrival_seq": 1,
        "expires_at": FUTURE, "target_index": day_index(DAY),
    })
    client.put(PREFIX + day_index(DAY), "a" * 64, {
        "scope_id": SCOPE, "request_id": "9-1", "item_id": "9-1",
        "record_id": "a" * 64, "business_date": DAY, "arrival_seq": 1,
        "accepted_at": "2026-09-29T01:00:01.000000Z",
        "completed_at": "2026-09-29T01:02:03.000000Z",
        "expires_at": FUTURE, "text": "正文。", "raw_hash": "c" * 64,
        "task_state": "succeeded", "delivery_state": "delivered",
        "callback_attempts": 1,
        "result": {"decision": "不重复", "duplicate_ids": [],
                   "reason": "无候选。"},
        "diagnostics": {"delivery": {"route_ref": "r", "trace_id": "trace-9"}},
    })
    port = TaskQueryPort(ElasticsearchBatchStore(client, index_prefix=PREFIX),
                         scope_id=SCOPE, clock=lambda: NOW)
    with TestClient(_app(query_port=port)) as http:
        response = http.get("/v1/api/task/9-1")
    assert response.status_code == 200
    body = response.json()
    assert body["taskState"] == "succeeded"
    assert body["deliveryState"] == "delivered"
    assert body["result"]["decision"] == "不重复"
    assert body["callbackDelivered"] is True
    assert port  # 真端口被端点驱动（读序经 fake ES 完成）


# ---------- R-Q6 未知 task_id 不泄露内部 ----------

def test_rq6_404_leaks_no_internals():
    port = _FakeQueryPort(None)
    with TestClient(_app(query_port=port)) as client:
        response = client.get("/v1/api/task/7-42")
    body = response.json()
    assert set(body) == {"accepted", "message"}
    assert "record" not in body["message"].lower()
    assert port.calls == ["7-42"]


# ---------- R-Q8 /ready 三态（端点半） ----------

def test_rq8_ready_all_green():
    with TestClient(_app(readiness_port=_FakeReadinessPort(True, []))) as client:
        response = client.get("/ready")
    assert response.status_code == 200
    assert response.json() == {"ready": True, "issues": [], "mode": "fixed"}


def test_rq8_ready_fatal_is_503_with_codes():
    port = _FakeReadinessPort(False, ["es_control_read_failed"])
    with TestClient(_app(readiness_port=port)) as client:
        response = client.get("/ready")
    assert response.status_code == 503
    assert response.json() == {"ready": False,
                               "issues": ["es_control_read_failed"],
                               "mode": "fixed"}


def test_rq8_ready_degraded_stays_200_and_reports_code():
    port = _FakeReadinessPort(True, ["embedding_space_unconfirmed"], mode="shadow")
    with TestClient(_app(readiness_port=port)) as client:
        response = client.get("/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["ready"] is True
    assert body["issues"] == ["embedding_space_unconfirmed"]
    assert body["mode"] == "shadow"                       # mode 回显


def test_rq8_ready_requires_auth():
    port = _FakeReadinessPort(True, [])
    with TestClient(_app(readiness_port=port, auth=lambda request: False)) as client:
        response = client.get("/ready")
    assert response.status_code == 403
    assert response.json() == {"error": "admin required"}
    assert port.calls == 0


# ---------- R-Q10 trace 透传 ----------

def test_rq10_trace_id_echoed_or_generated():
    with TestClient(_app(query_port=_FakeQueryPort(_view()))) as client:
        echoed = client.get("/v1/api/task/9-1", headers={"X-Trace-ID": "trace-abc"})
        generated = client.get("/v1/api/task/9-1")
    assert echoed.headers["X-Trace-ID"] == "trace-abc"
    assert generated.headers["X-Trace-ID"]                # 缺省生成非空
    assert generated.headers["X-Trace-ID"] != "trace-abc"


# ---------- R-Q11 鉴权先于路径参数校验 ----------

def test_rq11_auth_runs_before_task_id_validation():
    port = _FakeQueryPort(None)
    with TestClient(_app(query_port=port, auth=lambda request: False)) as client:
        response = client.get("/v1/api/task/not-a-valid-id")
    assert response.status_code == 403                    # 先鉴权后校验
    assert port.calls == []


def test_rq11_query_port_runs_off_event_loop():
    port = _FakeQueryPort(_view())
    with TestClient(_app(query_port=port)) as client:
        response = client.get("/v1/api/task/9-1")
    assert response.status_code == 200
    assert port.in_event_loop == [False]
