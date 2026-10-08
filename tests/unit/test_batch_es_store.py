from __future__ import annotations

import pytest

from news_flash_dedup.batch_es_store import ElasticsearchBatchStore, PermanentBulkError


class FakeClient:
    def __init__(self):
        self.calls = []
        self.bulk_result = None

    def get(self, **kwargs):
        self.calls.append(("get", kwargs))
        return {"_index": kwargs["index"], "_id": kwargs["id"], "found": True,
                "_source": {"x": 1}, "_seq_no": 2, "_primary_term": 3}

    def index(self, **kwargs):
        self.calls.append(("index", kwargs))

    def mget(self, **kwargs):
        self.calls.append(("mget", kwargs))
        return {"docs": [
            {**key, "found": True, "_source": {"x": 1}, "_seq_no": 2, "_primary_term": 3}
            if position == 0 else {**key, "found": False}
            for position, key in enumerate(kwargs["docs"])
        ]}

    def bulk(self, **kwargs):
        self.calls.append(("bulk", kwargs))
        if self.bulk_result is not None:
            return self.bulk_result
        return {"errors": False, "items": [
            {"create": {**operation["create"], "status": 201, "result": "created"}}
            for operation in kwargs["operations"][::2]
        ]}


def test_batch_adapter_routes_all_operations_to_isolated_prefix():
    client = FakeClient()
    store = ElasticsearchBatchStore(client, index_prefix="p01-batch-test42-")
    assert store.get("news-dedup-control-v1", "head")["seq_no"] == 2
    store.create("news-dedup-control-v1", "head", {"x": 1})
    store.replace("news-dedup-control-v1", "head", {"x": 2}, 2, 3)
    docs = store.mget([("news-dedup-requests-v1", "a"), ("news-dedup-requests-v1", "b")])
    store.bulk_create([("news-dedup-items-v1-2026.09.24", "c", {"x": 3})])
    assert docs[0]["source"] == {"x": 1} and docs[1] is None
    assert all("p01-batch-test42-" in str(args) for _, args in client.calls)
    bulk = client.calls[-1][1]["operations"]
    assert bulk == [
        {"create": {"_index": "p01-batch-test42-news-dedup-items-v1-2026.09.24", "_id": "c"}},
        {"x": 3},
    ]


@pytest.mark.parametrize("prefix", ["", "p01-g0-old-", "p01-batch-", "prod-p01-batch-x-", "p01-batch-x"])
def test_batch_adapter_rejects_non_isolated_prefix(prefix):
    with pytest.raises(ValueError):
        ElasticsearchBatchStore(FakeClient(), index_prefix=prefix)


def test_bulk_conflict_can_be_replayed_but_other_item_errors_fail():
    client = FakeClient()
    store = ElasticsearchBatchStore(client, index_prefix="p01-batch-test42-")
    identity = {"_index": "p01-batch-test42-idx", "_id": "id"}
    client.bulk_result = {"errors": True, "items": [{"create": {**identity, "status": 409}}]}
    store.bulk_create([("idx", "id", {"x": 1})])
    client.bulk_result = {"errors": True, "items": [{"create": {**identity, "status": 400}}]}
    with pytest.raises(RuntimeError):
        store.bulk_create([("idx", "id", {"x": 1})])


def test_bulk_permanent_item_error_identifies_verified_document_position():
    client = FakeClient()
    store = ElasticsearchBatchStore(client, index_prefix="p01-batch-test42-")
    client.bulk_result = {"errors": True, "items": [
        {"create": {"_index": "p01-batch-test42-idx", "_id": "first", "status": 201}},
        {"create": {"_index": "p01-batch-test42-idx", "_id": "second", "status": 400}},
    ]}
    documents = [("idx", "first", {"x": 1}), ("idx", "second", {"x": 2})]
    with pytest.raises(PermanentBulkError) as caught:
        store.bulk_create(documents)
    assert caught.value.failed_document_positions == (1,)


def test_bulk_request_level_permanent_error_has_no_document_position():
    class PermanentRequestError(Exception):
        status_code = 413

    client = FakeClient()
    client.bulk = lambda **kwargs: (_ for _ in ()).throw(PermanentRequestError())
    store = ElasticsearchBatchStore(client, index_prefix="p01-batch-test42-")
    with pytest.raises(PermanentBulkError) as caught:
        store.bulk_create([("idx", "first", {"x": 1})])
    assert caught.value.failed_document_positions == ()


def test_mget_item_error_is_not_interpreted_as_absent_identity():
    client = FakeClient()
    client.mget = lambda **kwargs: {"docs": [
        {"found": False, "error": {"type": "shard_not_available_exception"}},
    ]}
    store = ElasticsearchBatchStore(client, index_prefix="p01-batch-test42-")
    with pytest.raises(RuntimeError, match="mget item failed"):
        store.mget([("news-dedup-requests-v1", "request-id")])


@pytest.mark.parametrize("fault", ["one_error", "all_errors", "no_found", "wrong_count",
                                   "wrong_id", "wrong_index", "reordered", "false_source"])
def test_mget_rejects_unproven_absence_and_misbound_results(fault):
    keys = [("idx", "a"), ("idx", "b")]
    docs = [{"_index": "p01-batch-test42-idx", "_id": key, "found": False}
            for _, key in keys]
    if fault in {"one_error", "all_errors"}:
        for doc in docs[:1 if fault == "one_error" else 2]:
            doc["error"] = {"reason": "sensitive-response"}
    elif fault == "no_found":
        docs[0].pop("found")
    elif fault == "wrong_count":
        docs.pop()
    elif fault == "wrong_id":
        docs[0]["_id"] = "other"
    elif fault == "wrong_index":
        docs[0]["_index"] = "other"
    elif fault == "reordered":
        docs.reverse()
    else:
        docs[0]["_source"] = {"private": "sensitive-response"}
    client = FakeClient()
    client.mget = lambda **kwargs: {"docs": docs}
    with pytest.raises(RuntimeError) as caught:
        ElasticsearchBatchStore(client, index_prefix="p01-batch-test42-").mget(keys)
    assert "sensitive-response" not in str(caught.value)


@pytest.mark.parametrize("kind", ["missing_document", "missing_index", "missing_shard",
                                  "wrong_identity", "unknown_404"])
def test_get_only_explicit_document_not_found_is_absent(kind):
    es = pytest.importorskip("elasticsearch")
    from elastic_transport import ApiResponseMeta, NodeConfig

    body = {"_index": "p01-batch-test42-idx", "_id": "id", "found": False}
    if kind in {"missing_index", "missing_shard"}:
        body = {"error": {"type": "index_not_found_exception" if kind == "missing_index"
                          else "no_shard_available_action_exception",
                          "reason": "sensitive-response"}, "status": 404}
    elif kind == "wrong_identity":
        body["_id"] = "other"
    elif kind == "unknown_404":
        body = {"status": 404}
    error = es.NotFoundError("sensitive-response", ApiResponseMeta(
        404, "1.1", {}, 0, NodeConfig("http", "localhost", 9200)), body)
    client = FakeClient()

    def fail(**kwargs):
        raise error

    client.get = fail
    store = ElasticsearchBatchStore(client, index_prefix="p01-batch-test42-")
    if kind == "missing_document":
        assert store.get("idx", "id") is None
    else:
        with pytest.raises(RuntimeError) as caught:
            store.get("idx", "id")
        assert "sensitive-response" not in str(caught.value)
        assert caught.value.__suppress_context__


@pytest.mark.parametrize("fault", ["empty_items", "missing_items", "missing_status",
                                   "wrong_count", "wrong_id", "wrong_index", "status_200"])
def test_bulk_does_not_trust_errors_false_without_complete_item_status(fault):
    item = {"_index": "p01-batch-test42-idx", "_id": "id", "status": 201}
    result = {"errors": False, "items": [{"create": item}]}
    if fault == "empty_items":
        result["items"] = []
    elif fault == "missing_items":
        result.pop("items")
    elif fault == "missing_status":
        item.pop("status")
    elif fault == "wrong_count":
        result["items"] *= 2
    elif fault == "wrong_id":
        item["_id"] = "other"
    elif fault == "wrong_index":
        item["_index"] = "other"
    else:
        item["status"] = 200
    client = FakeClient()
    client.bulk_result = result
    with pytest.raises(RuntimeError):
        ElasticsearchBatchStore(client, index_prefix="p01-batch-test42-").bulk_create(
            [("idx", "id", {"x": 1})])


class StatefulClient(FakeClient):
    def __init__(self):
        super().__init__()
        from test_batch_admission import BatchMemoryStore
        self.memory = BatchMemoryStore()
        self.read_fault = None
        self.bulk_status = 201

    def get(self, **kwargs):
        self.calls.append(("get", kwargs))
        key = {"_index": kwargs["index"], "_id": kwargs["id"]}
        value = self.memory.get(kwargs["index"], kwargs["id"])
        return {**key, "found": False} if value is None else {
            **key, "found": True, "_source": value["source"],
            "_seq_no": value["seq_no"], "_primary_term": value["primary_term"],
        }

    def index(self, **kwargs):
        self.calls.append(("index", kwargs))
        if kwargs.get("op_type") == "create":
            self.memory.create(kwargs["index"], kwargs["id"], kwargs["document"])
        else:
            self.memory.replace(kwargs["index"], kwargs["id"], kwargs["document"],
                                kwargs["if_seq_no"], kwargs["if_primary_term"])

    def mget(self, **kwargs):
        self.calls.append(("mget", kwargs))
        docs = [self.get(index=key["_index"], id=key["_id"]) for key in kwargs["docs"]]
        if self.read_fault:
            self.read_fault(docs)
        return {"docs": docs}

    def bulk(self, **kwargs):
        self.calls.append(("bulk", kwargs))
        items = []
        for operation, body in zip(kwargs["operations"][::2], kwargs["operations"][1::2]):
            key = operation["create"]
            status = self.bulk_status[len(items)] if isinstance(self.bulk_status, list) else self.bulk_status
            if status == 201:
                try:
                    self.memory.create(key["_index"], key["_id"], body)
                except Exception as error:
                    if not self.memory.is_conflict(error):
                        raise
                    status = 409
            items.append({"create": {**key, "status": status}})
        return {"errors": any(item["create"]["status"] != 201 for item in items), "items": items}


@pytest.mark.parametrize("fault", ["request_error", "item_error", "both_error", "missing_found",
                                   "swapped", "truncated"])
def test_adapter_bad_identity_read_blocks_coordinator_cas(fault):
    from news_flash_dedup.admission import AdmissionUnknown
    from test_batch_admission import coordinator, request

    client = StatefulClient()
    store = ElasticsearchBatchStore(client, index_prefix="p01-batch-test42-")
    service = coordinator(store)
    service.accept_batch([request()])
    service.materialize_oldest()
    before = store.get("news-dedup-control-v1", "batch_admission_head")
    writes = sum(name == "index" for name, _ in client.calls)

    def break_read(docs):
        if fault.endswith("error"):
            positions = [0, 1] if fault == "both_error" else [0 if fault == "request_error" else 1]
            for position in positions:
                docs[position] = {**{key: docs[position][key] for key in ("_id", "_index")},
                                  "error": {"reason": "sensitive-response"}}
        elif fault == "missing_found":
            docs[0].pop("found")
        elif fault == "swapped":
            docs[0], docs[1] = docs[1], docs[0]
        else:
            docs.pop()

    client.read_fault = break_read
    with pytest.raises(AdmissionUnknown):
        service.accept_batch([request(), request(request_id="new", item_id="new")])
    assert store.get("news-dedup-control-v1", "batch_admission_head") == before
    assert sum(name == "index" for name, _ in client.calls) == writes


@pytest.mark.parametrize("status,quarantined", [(400, True), (403, True), (413, True),
                                                (429, False), (503, False)])
def test_bulk_status_distinguishes_persistent_quarantine_from_unknown(status, quarantined):
    from news_flash_dedup.admission import AdmissionConflict, AdmissionUnknown
    from test_batch_admission import coordinator, request

    client = StatefulClient()
    store = ElasticsearchBatchStore(client, index_prefix="p01-batch-test42-")
    service = coordinator(store)
    service.accept_batch([request()])
    client.bulk_status = status
    with pytest.raises(AdmissionConflict if quarantined else AdmissionUnknown):
        service.materialize_oldest()
    head = store.get("news-dedup-control-v1", "batch_admission_head")["source"]
    assert bool(head["checkpoint"].get("batch_quarantine")) is quarantined
    assert head["last_materialized_seq"] == 0 and head["last_allocated_seq"] == 1
    assert len(head["pending"]["batches"]) == 1
    if quarantined:
        restarted = coordinator(store)
        calls = len(client.calls)
        with pytest.raises(AdmissionConflict):
            restarted.accept_batch([request(request_id="new", item_id="new")])
        with pytest.raises(AdmissionConflict):
            restarted.materialize_oldest()
        assert all(name != "bulk" for name, _ in client.calls[calls:])
    else:
        client.bulk_status = 201
        assert service.recover() == 1


class SyntheticApiError(Exception):
    def __init__(self, status, body):
        super().__init__("sensitive-response")
        self.status_code = status
        self.body = body


@pytest.mark.parametrize("status,permanent", [(400, True), (403, True), (413, True),
                                             (408, False), (429, False), (503, False)])
def test_bulk_http_failure_classification_and_redaction(status, permanent):
    from news_flash_dedup.batch_es_store import PermanentBulkError
    client = FakeClient()

    def fail(**kwargs):
        raise SyntheticApiError(status, {"error": {"reason": "sensitive-response"}})

    client.bulk = fail
    store = ElasticsearchBatchStore(client, index_prefix="p01-batch-test42-")
    with pytest.raises(RuntimeError) as caught:
        store.bulk_create([("idx", "id", {"x": 1})])
    assert isinstance(caught.value, PermanentBulkError) is permanent
    assert "sensitive-response" not in str(caught.value)
    assert caught.value.__suppress_context__


@pytest.mark.parametrize("shape", ["absent", "index_error", "shard_error", "unknown", "wrong_id"])
def test_get_404_response_contract_without_es_dependency(shape):
    client = FakeClient()
    body = {"_index": "p01-batch-test42-idx", "_id": "id", "found": False}
    if shape == "index_error":
        body["error"] = {"type": "index_not_found_exception", "reason": "sensitive-response"}
    elif shape == "shard_error":
        body["_shards"] = {"failed": 1}
    elif shape == "unknown":
        body.pop("found")
    elif shape == "wrong_id":
        body["_id"] = "other"

    def fail(**kwargs):
        raise SyntheticApiError(404, body)

    client.get = fail
    store = ElasticsearchBatchStore(client, index_prefix="p01-batch-test42-")
    if shape == "absent":
        assert store.get("idx", "id") is None
    else:
        with pytest.raises(RuntimeError) as caught:
            store.get("idx", "id")
        assert "sensitive-response" not in str(caught.value)


@pytest.mark.parametrize("part", ["request", "item", "seq", "main"])
@pytest.mark.parametrize("method", ["accept_batch", "reconcile"])
def test_get_backend_error_cannot_reuse_materialized_identity_or_allocate(part, method):
    from news_flash_dedup.admission import AdmissionUnknown
    from test_batch_admission import coordinator, request
    client = StatefulClient()
    store = ElasticsearchBatchStore(client, index_prefix="p01-batch-test42-")
    service = coordinator(store)
    service.accept_batch([request()])
    head = store.get("news-dedup-control-v1", "batch_admission_head")["source"]
    docs = service._documents(head["pending"]["batches"][0]["entries"])
    service.recover()
    index, key, _ = docs[{"main": 0, "request": 1, "item": 2, "seq": 3}[part]]
    before = store.get("news-dedup-control-v1", "batch_admission_head")
    original = client.get

    def fail(**kwargs):
        if kwargs["id"] == key and kwargs["index"] == store._physical(index):
            raise SyntheticApiError(404, {"error": {"type": "index_not_found_exception"}})
        return original(**kwargs)

    client.get = fail
    with pytest.raises(AdmissionUnknown):
        if method == "accept_batch":
            service.accept_batch([request(), request(request_id="new", item_id="new")])
        else:
            service.reconcile(request())
    assert store.get("news-dedup-control-v1", "batch_admission_head") == before


def test_partial_bulk_permanent_item_preserves_successes_and_original_pending():
    from copy import deepcopy
    from news_flash_dedup.admission import AdmissionConflict
    from test_batch_admission import coordinator, request
    client = StatefulClient()
    store = ElasticsearchBatchStore(client, index_prefix="p01-batch-test42-")
    service = coordinator(store)
    service.accept_batch([request()])
    before = deepcopy(store.get("news-dedup-control-v1", "batch_admission_head")["source"])
    client.bulk_status = [201, 201, 400, 503]
    with pytest.raises(AdmissionConflict):
        service.materialize_oldest()
    after = store.get("news-dedup-control-v1", "batch_admission_head")["source"]
    assert after["pending"] == before["pending"]
    assert after["checkpoint"]["batch_quarantine"]["reason"] == "permanent_bulk_error"
    assert after["last_materialized_seq"] == 0 and after["last_allocated_seq"] == 1
    assert len(client.memory.docs) == 3
