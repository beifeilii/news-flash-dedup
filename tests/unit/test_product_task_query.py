"""B4/P21 红测（log\设计-P21接线包.md §八-8.1）：Q2 查询读序适配 product/task_query.py。

红测清单映射：
- R-Q5  假 404 读序：step1 映射 miss→step2 pending 有→accepted 投影；step1 miss→
        step2 无→step3 二读（清槽竞态），call 序列钉死=request→head→request
- R-Q7  物化竞态：step4 主记录 miss→回 step2 语义重估（pending 有=恢复中 accepted
        投影；无=QueryReadError，不伪装 404）
- R-Q12 投影字段：终态 result 恰五字段、非终态 null、callbackDelivered 三态、
        createdAt=accepted_at、startedAt/completedAt 直出、articleId/error 恒 null
- R-Q13 L443 到期硬门禁：逐步 now < expires_at；到期→404；缺失/畸形→503；
        临界 now==expires_at→404
- R-Q14 身份对拍：主记录 request_id/item_id 与 task_id 不符→QueryReadError（503 域，
        防误投影他域任务）
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from news_flash_dedup.admission import CONTROL_INDEX, REQUEST_INDEX
from news_flash_dedup.batch_admission import HEAD_ID, _keys
from news_flash_dedup.batch_es_store import ElasticsearchBatchStore
from news_flash_dedup.es_admission_schema import day_index
from news_flash_dedup.product.task_query import (
    QueryReadError,
    QueryView,
    TaskQueryPort,
)

from b4_fake_es import FakeESClient

DAY = "2026-09-29"
PREFIX = "p01-batch-b4qry-"
SCOPE = "default"
NOW = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)
FUTURE = "2026-10-06T00:00:00.000000Z"
PAST = "2026-09-01T00:00:00.000000Z"
ITEMS_LOGICAL = day_index(DAY)
ITEMS = PREFIX + ITEMS_LOGICAL
TASK_ID = "9-1"
RECORD_ID = "a" * 64


def _clock() -> datetime:
    return NOW


def _request_doc(*, expires_at=FUTURE, scope=SCOPE, request_id=TASK_ID,
                 item_id=TASK_ID, record_id=RECORD_ID) -> dict:
    return {
        "kind": "request", "scope_id": scope, "request_id": request_id,
        "item_id": item_id, "record_id": record_id,
        "business_fingerprint": "b" * 64, "business_date": DAY, "arrival_seq": 1,
        "expires_at": expires_at, "target_index": ITEMS_LOGICAL,
    }


def _main_doc(*, expires_at=FUTURE, scope=SCOPE, request_id=TASK_ID,
              item_id=TASK_ID, record_id=RECORD_ID, task_state="succeeded",
              result=None, delivery_state="pending", callback_attempts=0,
              started_at=None, completed_at=None) -> dict:
    doc = {
        "scope_id": scope, "request_id": request_id, "item_id": item_id,
        "record_id": record_id, "schema_version": "v1",
        "pipeline_version": "dedup_v1", "embedding_space_id": "space-x",
        "business_date": DAY, "arrival_seq": 1,
        "received_at": "2026-09-29T01:00:00.000000Z",
        "accepted_at": "2026-09-29T01:00:01.000000Z",
        "expires_at": expires_at,
        "text": "美国能源信息署公布原油库存增加。",
        "raw_hash": "c" * 64, "task_state": task_state,
        "delivery_state": delivery_state, "result": result,
        "callback_attempts": callback_attempts,
        "diagnostics": {"delivery": {"route_ref": "route-1", "trace_id": "trace-9"}},
    }
    if started_at is not None:
        doc["started_at"] = started_at
    if completed_at is not None:
        doc["completed_at"] = completed_at
    return doc


def _terminal_result() -> dict:
    return {"decision": "重复", "duplicate_ids": ["9-0"],
            "reason": "文本完全一致。"}


def _pending_entry(*, expires_at=FUTURE, scope=SCOPE, request_id=TASK_ID) -> dict:
    return {
        "scope_id": scope, "request_id": request_id, "item_id": request_id,
        "record_id": RECORD_ID, "business_fingerprint": "b" * 64,
        "text": "美国能源信息署公布原油库存增加。", "delivery_route_ref": "route-1",
        "trace_id": "trace-pending", "received_at": "2026-09-29T01:00:00.000000Z",
        "accepted_at": "2026-09-29T01:00:01.000000Z", "business_date": DAY,
        "expires_at": expires_at, "schema_version": "v1",
        "pipeline_version": "dedup_v1", "embedding_space_id": "space-x",
        "arrival_seq": 1,
    }


def _head_doc(*entries: dict) -> dict:
    batches = ([{"batch_id": "x" * 64, "digest": "y" * 64, "entries": list(entries)}]
               if entries else [])
    return {"kind": "head", "scope_id": SCOPE, "owner_id": "owner-1",
            "last_allocated_seq": 1, "last_materialized_seq": 0,
            "pending": {"version": "batch-v1", "batches": batches},
            "updated_at": "2026-09-29T01:00:00.000000Z"}


def _port(client: FakeESClient, *, scope=SCOPE) -> TaskQueryPort:
    return TaskQueryPort(ElasticsearchBatchStore(client, index_prefix=PREFIX),
                         scope_id=scope, clock=_clock)


def _request_key(scope=SCOPE, task_id=TASK_ID) -> str:
    return _keys(scope, task_id, task_id)[1]


# ---------- R-Q5 假 404 读序 ----------

def test_rq5_full_read_sequence_request_head_request_then_none():
    client = FakeESClient()
    client.put(PREFIX + CONTROL_INDEX, HEAD_ID, _head_doc())
    port = _port(client)
    assert port.lookup(TASK_ID) is None
    gets = [call for call in client.calls if call[0] == "get"]
    assert gets == [
        ("get", PREFIX + REQUEST_INDEX, _request_key()),
        ("get", PREFIX + CONTROL_INDEX, HEAD_ID),
        ("get", PREFIX + REQUEST_INDEX, _request_key()),
    ]


def test_rq5_pending_hit_projects_accepted_unmaterialized():
    client = FakeESClient()
    client.put(PREFIX + CONTROL_INDEX, HEAD_ID, _head_doc(_pending_entry()))
    view = _port(client).lookup(TASK_ID)
    assert isinstance(view, QueryView)
    body = view.to_dict()
    assert body["taskId"] == TASK_ID
    assert body["requestId"] == TASK_ID
    assert body["taskState"] == "accepted"
    assert body["deliveryState"] == "not_ready"
    assert body["createdAt"] == "2026-09-29T01:00:01.000000Z"
    assert body["traceId"] == "trace-pending"
    assert body["result"] is None
    # pending 命中不再二读 request（读序短路）
    gets = [call for call in client.calls if call[0] == "get"]
    assert gets == [("get", PREFIX + REQUEST_INDEX, _request_key()),
                    ("get", PREFIX + CONTROL_INDEX, HEAD_ID)]


def test_rq5_clear_slot_race_second_read_hits():
    client = FakeESClient()
    client.put(PREFIX + CONTROL_INDEX, HEAD_ID, _head_doc())
    client.put(ITEMS, RECORD_ID, _main_doc(result=_terminal_result(),
                                           completed_at="2026-09-29T01:02:03.000000Z"))
    original_get = client.get
    state = {"first": True}
    logged: list[tuple] = []

    def racing_get(*, index, id, realtime=True):
        logged.append(("get", index, id))
        # 清槽竞态：首读 request 未命中，二读前映射已建
        if index == PREFIX + REQUEST_INDEX and state["first"]:
            state["first"] = False
            client.put(PREFIX + REQUEST_INDEX, _request_key(), _request_doc())
            from elasticsearch import NotFoundError
            from elastic_transport import ApiResponseMeta, NodeConfig
            raise NotFoundError(
                "missing", ApiResponseMeta(
                    404, "1.1", {}, 0, NodeConfig("http", "localhost", 9200)),
                {"_index": index, "_id": id, "found": False})
        return original_get(index=index, id=id, realtime=realtime)

    client.get = racing_get
    view = _port(client).lookup(TASK_ID)
    assert view is not None
    assert view.taskState == "succeeded"
    assert [g[1] for g in logged] == [
        PREFIX + REQUEST_INDEX, PREFIX + CONTROL_INDEX,
        PREFIX + REQUEST_INDEX, ITEMS]


# ---------- R-Q7 物化竞态 ----------

def test_rq7_main_missing_with_pending_projects_recovering_accepted():
    client = FakeESClient()
    client.put(PREFIX + REQUEST_INDEX, _request_key(), _request_doc())
    client.put(PREFIX + CONTROL_INDEX, HEAD_ID, _head_doc(_pending_entry()))
    view = _port(client).lookup(TASK_ID)
    assert view is not None
    assert view.taskState == "accepted"          # 恢复中同型投影，不伪装 404
    assert view.deliveryState == "not_ready"


def test_rq7_main_missing_without_pending_is_read_error_not_fake_404():
    client = FakeESClient()
    client.put(PREFIX + REQUEST_INDEX, _request_key(), _request_doc())
    client.put(PREFIX + CONTROL_INDEX, HEAD_ID, _head_doc())
    with pytest.raises(QueryReadError):
        _port(client).lookup(TASK_ID)


# ---------- R-Q12 投影字段 ----------

def test_rq12_terminal_projection_exact_fields():
    client = FakeESClient()
    client.put(PREFIX + REQUEST_INDEX, _request_key(), _request_doc())
    client.put(ITEMS, RECORD_ID, _main_doc(
        result=_terminal_result(), delivery_state="delivered",
        callback_attempts=1, started_at="2026-09-29T01:01:00.000000Z",
        completed_at="2026-09-29T01:02:03.000000Z"))
    view = _port(client).lookup(TASK_ID)
    body = view.to_dict()
    assert set(body) == {"taskId", "requestId", "articleId", "traceId",
                         "taskState", "deliveryState", "createdAt", "startedAt",
                         "completedAt", "callbackDelivered", "result", "error"}
    assert body["taskId"] == TASK_ID
    assert body["requestId"] == TASK_ID
    assert body["articleId"] is None
    assert body["traceId"] == "trace-9"
    assert body["taskState"] == "succeeded"
    assert body["deliveryState"] == "delivered"
    assert body["createdAt"] == "2026-09-29T01:00:01.000000Z"
    assert body["startedAt"] == "2026-09-29T01:01:00.000000Z"
    assert body["completedAt"] == "2026-09-29T01:02:03.000000Z"
    assert body["callbackDelivered"] is True
    assert set(body["result"]) == {"item_id", "text", "decision",
                                   "duplicate_ids", "reason"}
    assert body["result"] == {
        "item_id": TASK_ID, "text": "美国能源信息署公布原油库存增加。",
        "decision": "重复", "duplicate_ids": ["9-0"], "reason": "文本完全一致。"}
    assert body["error"] is None


@pytest.mark.parametrize("delivery_state,attempts,expected", [
    ("delivered", 1, True),
    ("failed", 2, False),
    ("pending", 0, None),
    ("not_ready", 0, None),
])
def test_rq12_callback_delivered_three_states(delivery_state, attempts, expected):
    client = FakeESClient()
    client.put(PREFIX + REQUEST_INDEX, _request_key(), _request_doc())
    client.put(ITEMS, RECORD_ID, _main_doc(
        result=_terminal_result() if delivery_state != "not_ready" else None,
        delivery_state=delivery_state, callback_attempts=attempts,
        task_state="succeeded" if delivery_state != "not_ready" else "accepted"))
    view = _port(client).lookup(TASK_ID)
    assert view.callbackDelivered is expected


def test_rq12_non_terminal_result_null_and_started_at_null_when_absent():
    client = FakeESClient()
    client.put(PREFIX + REQUEST_INDEX, _request_key(), _request_doc())
    client.put(ITEMS, RECORD_ID, _main_doc(task_state="running"))
    view = _port(client).lookup(TASK_ID)
    assert view.result is None
    assert view.startedAt is None
    assert view.completedAt is None
    assert view.taskState == "running"


# ---------- R-Q13 L443 到期硬门禁 ----------

def test_rq13_expired_request_doc_is_404():
    client = FakeESClient()
    client.put(PREFIX + REQUEST_INDEX, _request_key(), _request_doc(expires_at=PAST))
    client.put(PREFIX + CONTROL_INDEX, HEAD_ID, _head_doc())
    # 映射到期→按未命中续走读序（head 空→二读仍到期→404）
    assert _port(client).lookup(TASK_ID) is None
    gets = [call for call in client.calls if call[0] == "get"]
    assert [g[1] for g in gets] == [
        PREFIX + REQUEST_INDEX, PREFIX + CONTROL_INDEX, PREFIX + REQUEST_INDEX]


def test_rq13_expired_pending_entry_is_404():
    client = FakeESClient()
    client.put(PREFIX + CONTROL_INDEX, HEAD_ID,
               _head_doc(_pending_entry(expires_at=PAST)))
    assert _port(client).lookup(TASK_ID) is None


def test_rq13_expired_main_doc_is_404():
    client = FakeESClient()
    client.put(PREFIX + REQUEST_INDEX, _request_key(), _request_doc())
    client.put(ITEMS, RECORD_ID, _main_doc(expires_at=PAST))
    assert _port(client).lookup(TASK_ID) is None


def test_rq13_expiry_boundary_now_equals_expiry_is_404():
    client = FakeESClient()
    client.put(PREFIX + REQUEST_INDEX, _request_key(),
               _request_doc(expires_at="2026-09-29T04:00:00.000000Z"))  # =NOW(UTC)
    client.put(PREFIX + CONTROL_INDEX, HEAD_ID, _head_doc())
    assert _port(client).lookup(TASK_ID) is None


@pytest.mark.parametrize("bad_expiry", [None, "", "not-a-date", 12345,
                                        "2026-10-06 00:00:00"])
def test_rq13_missing_or_malformed_expiry_is_503_not_404(bad_expiry):
    client = FakeESClient()
    doc = _request_doc()
    doc["expires_at"] = bad_expiry
    client.put(PREFIX + REQUEST_INDEX, _request_key(), doc)
    with pytest.raises(QueryReadError):
        _port(client).lookup(TASK_ID)


def test_rq13_malformed_pending_expiry_is_503():
    client = FakeESClient()
    entry = _pending_entry()
    entry["expires_at"] = "garbage"
    client.put(PREFIX + CONTROL_INDEX, HEAD_ID, _head_doc(entry))
    with pytest.raises(QueryReadError):
        _port(client).lookup(TASK_ID)


# ---------- R-Q14 身份对拍 ----------

@pytest.mark.parametrize("field", ["request_id", "item_id"])
def test_rq14_main_record_identity_mismatch_is_503_not_projection(field):
    client = FakeESClient()
    client.put(PREFIX + REQUEST_INDEX, _request_key(), _request_doc())
    mismatched = _main_doc(**{field: "other-9"})
    client.put(ITEMS, RECORD_ID, mismatched)
    with pytest.raises(QueryReadError):
        _port(client).lookup(TASK_ID)


def test_rq14_cross_scope_doc_has_no_existence_oracle():
    client = FakeESClient()
    client.put(PREFIX + REQUEST_INDEX, _request_key(),
               _request_doc(scope="other-scope"))
    client.put(PREFIX + CONTROL_INDEX, HEAD_ID, _head_doc())
    # scope 不符→按未命中（404，无跨域存在性 oracle），不报错不投影
    assert _port(client).lookup(TASK_ID) is None
