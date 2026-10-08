from __future__ import annotations

import copy
import fnmatch
import importlib.util
import json
import threading
import time
from pathlib import Path

import pytest
from elastic_transport import ApiResponseMeta, NodeConfig
from elasticsearch import ConflictError

from news_flash_dedup.admission import AdmissionConflict, AdmissionUnknown, IdentityConflict
from news_flash_dedup.batch_admission import AdmissionCapacityExceeded, BatchAdmissionCoordinator, BatchLimits, HEAD_ID
from news_flash_dedup.batch_collector import CollectorRejected
from news_flash_dedup.batch_es_store import ElasticsearchBatchStore

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "bench_batch_admission.py"
spec = importlib.util.spec_from_file_location("bench_batch_admission", SCRIPT)
bench = importlib.util.module_from_spec(spec)
import sys
sys.modules[spec.name] = bench
spec.loader.exec_module(bench)


@pytest.fixture(autouse=True)
def no_unrecorded_threads_after_test():
    before = set(threading.enumerate())
    yield
    remaining = [thread for thread in threading.enumerate() if thread not in before and thread.name.startswith("batch-")]
    for thread in remaining:
        thread.join(1)
    assert not [thread.name for thread in remaining if thread.is_alive()]


class FakeIndices:
    def __init__(self):
        self.schemas = {}
        self.created = []

    def exists(self, *, index):
        return any(fnmatch.fnmatch(name, index) for name in self.schemas)

    def get(self, *, index, allow_no_indices, ignore_unavailable):
        assert allow_no_indices is True and ignore_unavailable is True
        return {name: copy.deepcopy(schema) for name, schema in self.schemas.items()
                if fnmatch.fnmatch(name, index)}

    def create(self, *, index, body):
        assert index.startswith("p01-batch-")
        if index in self.schemas:
            raise RuntimeError("exists")
        self.schemas[index] = copy.deepcopy(body)
        self.created.append(index)
        return {"acknowledged": True}

    def get_settings(self, *, index):
        names = index.split(",")
        return {name: {"settings": {"index": {
            "number_of_shards": "3", "number_of_replicas": "2",
        }}} for name in names}


class FakeClient:
    def __init__(self, version="8.19.3"):
        self.indices = FakeIndices()
        self.documents = {}
        self.lock = threading.RLock()
        self.closed = False
        self.version = version
        self.mget_fault = None
        self.mget_sizes = []
        self.calls = []
        self.bulk_gate = None
        self.bulk_entered = threading.Event()

    def info(self):
        return {"version": {"number": self.version}}

    def get(self, *, index, id, realtime=True):
        assert realtime is True
        with self.lock:
            self.calls.append(("get", index))
            value = self.documents.get((index, id))
            result = {"_index": index, "_id": id, "found": value is not None}
            if value is not None:
                body, version = value
                result.update(_source=copy.deepcopy(body), _seq_no=version, _primary_term=1)
            return result

    def index(self, *, index, id, document, refresh, op_type=None,
              if_seq_no=None, if_primary_term=None):
        assert refresh is False
        with self.lock:
            self.calls.append(("index", index))
            previous = self.documents.get((index, id))
            if ((op_type == "create" and previous is not None) or
                    (op_type != "create" and (previous is None or previous[1] != if_seq_no))):
                raise ConflictError("conflict", ApiResponseMeta(
                    409, "1.1", {}, 0, NodeConfig("http", "127.0.0.1", 9200)), {})
            self.documents[index, id] = (copy.deepcopy(document), 0 if previous is None else previous[1] + 1)
        return {"result": "created"}

    def mget(self, *, docs, realtime):
        assert realtime is True
        self.mget_sizes.append(len(docs))
        result = [self.get(index=key["_index"], id=key["_id"]) for key in docs]
        if self.mget_fault:
            self.mget_fault(result)
        return {"docs": result}

    def bulk(self, *, operations, refresh):
        self.bulk_entered.set()
        if self.bulk_gate is not None:
            self.bulk_gate.wait()
        result = []
        for op, body in zip(operations[::2], operations[1::2]):
            key = op["create"]
            try:
                self.index(index=key["_index"], id=key["_id"], document=body,
                           op_type="create", refresh=refresh)
                status = 201
            except ConflictError:
                status = 409
            result.append({"create": {**key, "status": status}})
        return {"items": result}

    def close(self):
        self.closed = True


def options(**overrides):
    values = dict(rate=20, duration=0.1, run_id="offline", confirm_uat=True,
                  probe_only=False, max_drain=0.8, reconcile_timeout=0.8,
                  close_timeout=0.2, startup_timeout=2, entry_mode="function",
                  collector_wait=0.01, request_timeout=0.2, http_timeout=0.4,
                  sample_interval=0.01, drain_window_seconds=0)
    values.update(overrides)
    return bench.arguments([part for key, value in values.items() for part in (
        (["--" + key.replace("_", "-")] if value else []) if type(value) is bool
        else ["--" + key.replace("_", "-") + "=" + str(value)])])


def run_fake(monkeypatch, client=None, **kwargs):
    client = client or FakeClient()
    monkeypatch.setattr(bench, "client_from_environment", lambda: client)
    return bench.run(options(**kwargs)), client


def prepared(count=1, materialize=False):
    client = FakeClient()
    prefix = "p01-batch-offline-"
    bench.provision(client, prefix)
    store = bench.ObservedStore(ElasticsearchBatchStore(client, index_prefix=prefix))
    coordinator = BatchAdmissionCoordinator(store, owner_id="batch-offline", owner_isolated=lambda: True,
                                            limits=BatchLimits(), clock=bench.utc_now)
    records = [bench.InputRecord(bench.make_request(i, "offline"), time.monotonic()) for i in range(count)]
    for record in records:
        record.receipt = coordinator.accept_batch([record.request])[0]
        record.outcome = "accepted"
    if materialize:
        while store.get(bench.CONTROL_INDEX, HEAD_ID)["source"]["pending"]["batches"]:
            coordinator.materialize_oldest()
    return client, store, coordinator, records


def reconcile(store, records, **kwargs):
    return bench.reconcile_inputs(store, records, deadline=time.monotonic() + 0.5,
                                  batch_size=3, **kwargs)


def test_import_provision_complete_run_and_close(monkeypatch):
    report, client = run_fake(monkeypatch)
    assert report["counts"]["accepted"] == report["counts"]["input"] == 2
    assert report["four_document_complete"] == 2
    assert report["input_complete"] and report["reconcile_complete"]
    assert not report["drain_timed_out"] and not report["worker_alive"]
    assert client.closed and all(not alive for alive in report["resources_alive"].values())
    assert all(name.startswith(report["prefix"]) for _, name in client.calls)
    assert all(item == {"shards": 3, "replicas": 2} for item in report["index_topology"].values())
    assert report["entry_mode"] == "function" and not report["http_202_measured"]
    assert report["g0_passed"] is False
    assert report["source_sha256"] and report["config"]["max_batch_items"] == 8
    assert {"append", "bulk", "mget", "shrink"} <= report["store_metrics"]["stages"].keys()
    assert "隔离基线固定工件" not in json.dumps(report, ensure_ascii=False)


@pytest.mark.parametrize("run_id", ["", "ABC", "中文", "a_b", "a.b", "a/../b", "-x", "x-", "-", "a" * 33])
def test_bad_run_id_never_creates_client(monkeypatch, run_id):
    def forbidden():
        pytest.fail("client must not be created")
    monkeypatch.setattr(bench, "client_from_environment", forbidden)
    with pytest.raises(ValueError):
        bench.run(options(run_id=run_id))


def test_no_write_authorization_never_creates_client(monkeypatch):
    monkeypatch.setattr(bench, "client_from_environment", lambda: pytest.fail("unauthorized client"))
    with pytest.raises(RuntimeError):
        bench.run(options(confirm_uat=False))


@pytest.mark.parametrize("prefix", ["p01-batch-", "p01-batch-中文-", "p01-batch-UPPER-", "prod-", "p01-batch--bad-"])
def test_provision_rejects_nonisolated_prefix(prefix):
    client = FakeClient()
    with pytest.raises(ValueError):
        bench.provision(client, prefix)
    assert not client.indices.created


def test_reused_prefix_rejected_before_any_creation():
    client = FakeClient()
    client.indices.schemas["p01-batch-offline-old-index"] = {}
    with pytest.raises(bench.ProvisionError) as caught:
        bench.provision(client, "p01-batch-offline-")
    assert caught.value.reason == "prefix_exists"
    assert not client.indices.created


def test_wildcard_exists_true_without_real_indices_does_not_block_provision(monkeypatch):
    client = FakeClient()
    monkeypatch.setattr(client.indices, "exists", lambda **kwargs: True)
    topology = bench.provision(client, "p01-batch-offline-")
    assert len(client.indices.created) == len(topology) == 4
    assert all(name.startswith("p01-batch-offline-") for name in client.indices.created)


def test_index_listing_outside_run_prefix_is_rejected(monkeypatch):
    client = FakeClient()
    monkeypatch.setattr(client.indices, "get", lambda **kwargs: {"shared-index": {}})
    with pytest.raises(bench.ProvisionError) as caught:
        bench.provision(client, "p01-batch-offline-")
    assert caught.value.reason == "index_listing_invalid"
    assert not client.indices.created


def test_provision_failure_report_uses_fixed_reason_only(monkeypatch):
    client = FakeClient()
    client.indices.schemas["p01-batch-offline-old-index"] = {}
    monkeypatch.setattr(bench, "client_from_environment", lambda: client)
    with pytest.raises(bench.ProvisionError) as caught:
        bench.run(options())
    assert caught.value.benchmark_report["failure_reason"] == "prefix_exists"
    assert caught.value.benchmark_report["errors"] == {"ProvisionError": 1}
    assert not caught.value.benchmark_report["http_202_measured"]
    assert client.closed


def test_probe_and_wrong_version_always_close(monkeypatch):
    report, client = run_fake(monkeypatch, probe_only=True, confirm_uat=False)
    assert report["read_only_probe"] and client.closed and not client.indices.created
    client = FakeClient(version="9.0.0")
    monkeypatch.setattr(bench, "client_from_environment", lambda: client)
    with pytest.raises(RuntimeError):
        bench.run(options())
    assert client.closed


@pytest.mark.parametrize("error, expected", [
    *[(AdmissionCapacityExceeded(reason), "capacity:" + reason) for reason in
      ("log_item_limit", "batch_byte_limit", "log_byte_limit")],
    *[(CollectorRejected(reason), "collector:" + reason) for reason in
      ("queue_full", "queue_timeout", "closed", "worker_failed", "request_too_large")],
    (IdentityConflict("private"), "identity_conflict"),
    (AdmissionUnknown("private"), "unknown"),
    (AdmissionConflict("private"), "admission_conflict"),
    (RuntimeError("private"), "technical:RuntimeError"),
])
def test_error_classification(error, expected):
    assert bench.classify_error(error) == expected


def test_pending_atomic_evidence_is_not_lost_or_four_documents():
    _, store, _, records = prepared()
    result = reconcile(store, records)
    assert result["durable_pending"] == 1
    assert result["four_document_complete"] == result["lost_confirmed"] == 0
    assert result["reconcile_complete"]


def test_item_error_is_unresolved_not_lost_or_success():
    client, store, _, records = prepared(materialize=True)
    client.mget_fault = lambda docs: docs[0].update(error={"reason": "private"})
    result = reconcile(store, records)
    assert not result["reconcile_complete"] and result["unresolved"] == 1
    assert result["lost_confirmed"] == result["four_document_complete"] == 0
    assert "private" not in json.dumps(result)


@pytest.mark.parametrize("field", ["request_id", "item_id", "scope_id", "arrival_seq", "raw_hash", "text",
                                   "schema_version", "pipeline_version", "embedding_space_id", "accepted_at",
                                   "received_at", "expires_at", "business_date", "route_ref", "trace_id"])
def test_four_document_wrong_immutable_field_is_unresolved(field):
    client, store, _, records = prepared(materialize=True)
    main = next(body for (index, _), (body, _) in client.documents.items() if "items-v1" in index)
    if field in ("route_ref", "trace_id"):
        main["diagnostics"]["delivery"][field] = "wrong"
    else:
        main[field] = "wrong"
    result = reconcile(store, records)
    assert not result["reconcile_complete"] and result["four_document_complete"] == 0
    assert result["lost_confirmed"] == 0


@pytest.mark.parametrize("kind, field", [("request", "kind"), ("item", "business_fingerprint"),
                                        ("seq", "target_index")])
def test_mapping_wrong_fields_are_unresolved(kind, field):
    client, store, _, records = prepared(materialize=True)
    body = next(body for (_, key), (body, _) in client.documents.items() if key.startswith(kind + ":"))
    body[field] = "wrong"
    assert not reconcile(store, records)["reconcile_complete"]


def test_half_materialization_without_pending_is_not_confirmed_loss():
    client, store, _, records = prepared(materialize=True)
    key = next(key for key in client.documents if "items-v1" in key[0])
    del client.documents[key]
    result = reconcile(store, records)
    assert result["unresolved"] == 1 and result["lost_confirmed"] == 0


def test_extra_sequence_and_unregistered_identity_are_detected():
    _, store, coordinator, records = prepared(materialize=True)
    coordinator.accept_batch([bench.make_request(99, "offline")])
    coordinator.materialize_oldest()
    result = reconcile(store, records)
    assert not result["reconcile_complete"] and result["errors"]


def test_technical_failure_still_reconciles_acceptance_in_bounded_mget():
    client, store, _, records = prepared(count=7, materialize=True)
    for record in records:
        record.outcome = "technical:RuntimeError"
        record.receipt = None
    client.mget_sizes.clear()
    result = reconcile(store, records)
    assert result["four_document_complete"] == 7 and result["reconcile_complete"]
    assert max(client.mget_sizes) <= 3


def test_unknown_notfound_never_proves_rejection():
    client, store, _, records = prepared()
    for key in list(client.documents):
        del client.documents[key]
    records[0].outcome = "unknown"
    records[0].receipt = None
    result = reconcile(store, records, unknown_cas=True)
    assert result["unknown_pending"] == result["unresolved"] == 1
    assert result["lost_confirmed"] == 0 and not result["reconcile_complete"]


def test_changing_head_does_not_certify_snapshot():
    client, store, _, records = prepared(materialize=True)
    def mutate(docs):
        key = next(key for key in client.documents if key[1] == HEAD_ID)
        body, version = client.documents[key]
        client.documents[key] = body, version + 1
    client.mget_fault = mutate
    result = reconcile(store, records)
    assert not result["reconcile_complete"] and result["lost_confirmed"] == 0


def test_permanently_blocked_bulk_returns_bounded_and_reports_writer(monkeypatch):
    client = FakeClient()
    client.bulk_gate = threading.Event()
    started = time.monotonic()
    try:
        report, _ = run_fake(monkeypatch, client, max_drain=0.08, reconcile_timeout=0.08, close_timeout=0.03)
        assert time.monotonic() - started < 2
        assert report["drain_timed_out"] and report["worker_alive"]
        assert any(report["resources_alive"].values())
        assert report["lost_confirmed"] == 0
    finally:
        client.bulk_gate.set()


def test_materializer_observes_stop_even_with_pending():
    _, store, coordinator, _ = prepared()
    stop = threading.Event()
    stop.set()
    bench.materialize_loop(store, coordinator, stop, 0.01, {})
    assert store.get(bench.CONTROL_INDEX, HEAD_ID)["source"]["pending"]["batches"]


def test_materializer_uses_one_prefix_for_three_pending_batches(monkeypatch):
    _, store, coordinator, _ = prepared(count=3)
    stop = threading.Event()
    calls = []
    original = coordinator.materialize_prefix
    def prefix_once():
        calls.append("prefix")
        result = original()
        stop.set()
        return result
    def forbidden_oldest():
        stop.set()
        calls.append("oldest")
        raise RuntimeError("old path")
    monkeypatch.setattr(coordinator, "materialize_prefix", prefix_once)
    monkeypatch.setattr(coordinator, "materialize_oldest", forbidden_oldest)
    errors = {}
    bench.materialize_loop(store, coordinator, stop, 0.01, errors)
    head = store.get(bench.CONTROL_INDEX, HEAD_ID)["source"]
    assert calls == ["prefix"] and not errors
    assert head["last_allocated_seq"] == head["last_materialized_seq"] == 3
    assert not head["pending"]["batches"]
    assert store.metrics()["stages"]["bulk"]["count"] == 1


def test_materializer_keeps_prefix_unknown_without_fallback(monkeypatch):
    _, store, coordinator, _ = prepared()
    stop = threading.Event()
    calls = []
    def uncertain():
        calls.append("prefix")
        stop.set()
        raise AdmissionUnknown("private")
    def forbidden_oldest():
        calls.append("oldest")
        stop.set()
        raise RuntimeError("old path")
    monkeypatch.setattr(coordinator, "materialize_prefix", uncertain)
    monkeypatch.setattr(coordinator, "materialize_oldest", forbidden_oldest)
    errors = {}
    bench.materialize_loop(store, coordinator, stop, 0.01, errors)
    assert calls == ["prefix"] and errors == {"AdmissionUnknown": 1}
    assert store.get(bench.CONTROL_INDEX, HEAD_ID)["source"]["pending"]["batches"]


def test_drain_uses_realtime_head_instead_of_cached_observation():
    _, store, coordinator, _ = prepared()
    cached = copy.deepcopy(store.latest_head)
    cached["source"]["last_materialized_seq"] = cached["source"]["last_allocated_seq"]
    cached["source"]["pending"]["batches"] = []
    store.latest_head = cached
    assert not bench.durable_head_drained(store)
    coordinator.materialize_oldest()
    assert bench.durable_head_drained(store)


def test_blocked_realtime_drain_read_obeys_deadline(monkeypatch):
    gate = threading.Event()
    monkeypatch.setattr(bench, "durable_head_drained", lambda store: gate.wait())
    started = time.monotonic()
    try:
        report, _ = run_fake(monkeypatch, max_drain=0.03, close_timeout=0.03)
        assert time.monotonic() - started < 2
        assert report["drain_timed_out"] and not report["drain_head_realtime_confirmed"]
        assert any(report["resources_alive"].values())
        assert not report["local_performance"]["passed"]
    finally:
        gate.set()


def test_real_loopback_http_golden_and_resource_close(monkeypatch):
    report, client = run_fake(monkeypatch, entry_mode="http", duration=0.1, startup_timeout=5)
    assert report["http_202_measured"] and report["client_responses"] == {"202": 2}
    assert report["server_responses"] == {"accepted": 2}
    assert report["four_document_complete"] == 2 and client.closed
    assert not any(report["resources_alive"].values())
    assert report["http_bind"].startswith("127.0.0.1:")


@pytest.mark.parametrize("error,status", [(IdentityConflict("private"), 409),
                                          (AdmissionCapacityExceeded("log_item_limit"), 429),
                                          (CollectorRejected("request_too_large"), 413),
                                          (AdmissionUnknown("private"), 500),
                                          (AdmissionConflict("private"), 503)])
def test_real_loopback_error_contract(error, status):
    record = bench.InputRecord(bench.make_request(0, "offline"), time.monotonic())
    class Rejecting:
        def accept(self, request):
            raise error
    port = bench.BenchmarkPort(Rejecting(), [record])
    host = bench.LoopbackHTTP(port, options(startup_timeout=5))
    try:
        host.start()
        response = host.client.post(host.url + "/v1/api/task/run", json=bench.http_payload(record.request))
        assert response.status_code == status
        assert record.server_outcome == bench.classify_error(error)
        assert "private" not in response.text
    finally:
        host.close(time.monotonic() + 1)
    assert not host.thread.is_alive() and host.client.is_closed


def test_pending_with_wrong_existing_document_is_not_clean_evidence():
    client, store, _, records = prepared()
    candidate = next(iter(store.candidates.values()))[0]
    index, key, body = bench.BatchAdmissionCoordinator._documents([candidate])[1]
    body["kind"] = "wrong"
    client.index(index="p01-batch-offline-" + index, id=key, document=body, op_type="create", refresh=False)
    result = reconcile(store, records)
    assert not result["reconcile_complete"] and result["lost_confirmed"] == 0


def test_stalled_reconciliation_is_bounded_and_resource_is_recorded():
    client, store, _, records = prepared()
    gate = threading.Event()
    resources = bench.Threads()
    original = client.mget
    client.mget = lambda **kwargs: (gate.wait(), original(**kwargs))[1]
    started = time.monotonic()
    try:
        result = bench.reconcile_inputs(store, records, deadline=time.monotonic() + 0.03, resources=resources)
        assert time.monotonic() - started < 0.4
        assert not result["reconcile_complete"] and result["reconcile_worker_alive"]
        assert result["lost_confirmed"] == 0
    finally:
        gate.set()
        for thread in resources.threads:
            thread.join(0.5)


def test_client_close_stall_is_bounded_and_reported(monkeypatch):
    client = FakeClient()
    gate = threading.Event()
    client.close = gate.wait
    try:
        report, _ = run_fake(monkeypatch, client, close_timeout=0.03)
        assert report["close_errors"]
        assert any(report["resources_alive"].values())
        assert not report["local_performance"]["passed"]
    finally:
        gate.set()


def test_local_slot_rejection_registers_all_inputs(monkeypatch):
    original = bench.BatchAdmissionCollector.accept
    gate = threading.Event()
    def slow(self, request):
        gate.wait(0.1)
        return original(self, request)
    monkeypatch.setattr(bench.BatchAdmissionCollector, "accept", slow)
    report, _ = run_fake(monkeypatch, rate=100, duration=0.1, entry_workers=1)
    assert report["counts"]["local:entry_capacity"] > 0
    assert len(report["inputs"]) == len(report["reconciled"]) == 10
    assert report["reconcile_complete"]
    assert report["counts"]["input"] == sum(value for key, value in report["counts"].items() if key != "input")


def test_technical_client_failure_after_server_acceptance_is_unknown(monkeypatch):
    import httpx
    original = httpx.Client.post
    def false_500(self, *args, **kwargs):
        response = original(self, *args, **kwargs)
        response.status_code = 500
        return response
    monkeypatch.setattr(httpx.Client, "post", false_500)
    report, _ = run_fake(monkeypatch, entry_mode="http", startup_timeout=5)
    assert report["counts"].get("accepted", 0) == 0
    assert report["counts"]["unknown"] == report["unknown_settled"] == 2
    assert report["four_document_complete"] == 2
    assert not report["local_performance"]["passed"]


def test_slow_materializer_can_drain_and_report_latency(monkeypatch):
    client = FakeClient()
    original = client.bulk
    def slow(**kwargs):
        threading.Event().wait(0.03)
        return original(**kwargs)
    client.bulk = slow
    report, _ = run_fake(monkeypatch, client)
    assert not report["drain_timed_out"] and report["four_document_complete"] == 2
    assert report["store_metrics"]["stages"]["bulk"]["max_ms"] >= 25


def test_defaults_keep_frozen_limits():
    args = bench.arguments(["--rate=10", "--duration=1800", "--confirm-uat"])
    assert (args.max_batch_items, args.max_batch_bytes, args.max_log_items, args.max_log_bytes) == (8, 524288, 64, 4194304)
    assert (args.collector_wait, args.queue_items, args.queue_bytes, args.max_drain, args.reconcile_timeout) == (0.1, 64, 4194304, 60, 120)
    assert args.entry_mode == "http"


def test_function_error_after_durable_acceptance_is_audited(monkeypatch):
    original = bench.BatchAdmissionCollector.accept
    def fail_after(self, request):
        original(self, request)
        raise RuntimeError("private")
    monkeypatch.setattr(bench.BatchAdmissionCollector, "accept", fail_after)
    report, _ = run_fake(monkeypatch)
    assert report["counts"]["technical:RuntimeError"] == 2
    assert report["four_document_complete"] == 2 and report["reconcile_complete"]
    assert "private" not in json.dumps(report)


def test_watchdog_only_terminates_its_child_and_retains_unknown(tmp_path):
    class Process:
        pid = 123
        exitcode = None
        live = True
        terminated = False
        def start(self):
            pass
        def join(self, timeout):
            assert timeout > 0
        def is_alive(self):
            return self.live
        def terminate(self):
            self.terminated = True
            self.live = False
            self.exitcode = -15
        def close(self):
            pass
    process = Process()
    class Context:
        def Process(self, **kwargs):
            assert kwargs["target"] is bench._child
            return process
    args = options(output=tmp_path / "watchdog.json")
    report = bench.watchdog(args, context=Context())
    assert process.terminated and report["watchdog"]["terminated"]
    assert report["unknown_pending"] == 2 and not report["reconcile_complete"]
    assert report["lost_confirmed"] == 0 and not report["g0_passed"]


def test_full_http_golden_including_reused_receipt():
    _, store, coordinator, records = prepared()
    class Port:
        def accept(self, request):
            return coordinator.accept_batch([request])[0]
    port = bench.BenchmarkPort(Port(), records)
    host = bench.LoopbackHTTP(port, options(startup_timeout=5))
    try:
        host.start()
        request = records[0].request
        response = host.client.post(host.url + "/v1/api/task/run", json=bench.http_payload(request))
        assert response.status_code == 202
        assert response.json() == {"accepted": True, "message": "任务已接收，正在处理", "taskId": "1-1",
                                   "traceId": request.trace_id, "requestId": "1-1", "articleId": None,
                                   "duplicate": True, "state": "accepted"}
        assert records[0].receipt.arrival_seq == 1 and records[0].receipt.reused
    finally:
        host.close(time.monotonic() + 1)


def test_unknown_inflight_cas_notfound_is_never_rejection(monkeypatch):
    client = FakeClient()
    original = client.index
    gate = threading.Event()
    def blocked(**kwargs):
        if kwargs["id"] == HEAD_ID and kwargs["document"]["last_allocated_seq"] > 0:
            gate.wait()
        return original(**kwargs)
    client.index = blocked
    started = time.monotonic()
    try:
        report, _ = run_fake(monkeypatch, client, request_timeout=0.03, max_drain=0.06,
                             reconcile_timeout=0.06, close_timeout=0.03)
        assert time.monotonic() - started < 1.5
        assert report["unknown_pending"] > 0 and report["lost_confirmed"] == 0
        assert report["worker_alive"] and report["drain_timed_out"]
        assert not report["reconcile_complete"]
    finally:
        gate.set()


def test_partial_four_documents_with_intact_pending_is_atomic_not_drained():
    client, store, _, records = prepared()
    entry = next(iter(store.candidates.values()))[0]
    documents = bench.BatchAdmissionCoordinator._documents([entry])
    store.bulk_create(documents[:2])
    result = reconcile(store, records)
    assert result["durable_pending"] == 1 and result["four_document_complete"] == 0
    assert result["reconcile_complete"] and result["lost_confirmed"] == 0


def test_all_four_present_but_pending_not_shrunk_is_not_drained():
    _, store, _, records = prepared()
    entry = next(iter(store.candidates.values()))[0]
    store.bulk_create(bench.BatchAdmissionCoordinator._documents([entry]))
    result = reconcile(store, records)
    assert result["durable_pending"] == 1 and result["four_document_complete"] == 0
    assert result["watermarks"] == {"allocated": 1, "materialized": 0}


@pytest.mark.parametrize("setting,value", [("duration", "nan"), ("max_drain", 0),
                                            ("entry_workers", -1), ("collector_wait", 0.2)])
def test_bad_limit_rejected_before_client(monkeypatch, setting, value):
    monkeypatch.setattr(bench, "client_from_environment", lambda: pytest.fail("client should not exist"))
    with pytest.raises(ValueError):
        bench.run(options(**{setting: value}))


def _offline_child(args, destination, checkpoint):
    bench.client_from_environment = FakeClient
    bench._child(args, destination, checkpoint)


def _blocked_child(args, destination, checkpoint):
    bench._write_report(checkpoint, {"phase": "blocked-local-test"})
    threading.Event().wait()


class SpawnContext:
    def __init__(self, target):
        self.target = target

    def Process(self, **kwargs):
        import multiprocessing
        kwargs["target"] = self.target
        return multiprocessing.get_context("spawn").Process(**kwargs)


def test_real_spawn_http_run_closes_child_and_clients(tmp_path):
    args = options(entry_mode="http", startup_timeout=5, output=tmp_path / "spawn-http.json")
    result = bench.watchdog(args, context=SpawnContext(_offline_child))
    assert result["counts"]["accepted"] == result["four_document_complete"] == 2
    assert not result["watchdog"]["terminated"] and not result["watchdog"]["child_alive"]
    assert result["watchdog"]["exitcode"] == 0
    assert result["watchdog"]["server_inflight_writes_unresolved"]
    assert result["watchdog"]["local_process_reaped"]
    assert not result["watchdog"]["server_write_fencing_proven"]
    assert not any(result["resources_alive"].values())


def test_real_spawn_watchdog_reaps_permanent_block_without_proving_absence(tmp_path):
    args = options(startup_timeout=2, max_drain=0.02, reconcile_timeout=0.02,
                   close_timeout=0.1, output=tmp_path / "spawn-blocked.json")
    started = time.monotonic()
    result = bench.watchdog(args, context=SpawnContext(_blocked_child))
    assert time.monotonic() - started < 5
    assert result["last_checkpoint"]["phase"] == "blocked-local-test"
    assert result["watchdog"]["terminated"] and not result["watchdog"]["child_alive"]
    assert result["watchdog"]["server_inflight_writes_unresolved"]
    assert result["unknown_pending"] == 2 and result["lost_confirmed"] == 0


def _run_materializer_with_auto_stop(prepare_kwargs, mode, **kwargs):
    """包装 helper：物化清空 pending 后由 wrap 自动 stop.set() 退出循环。"""
    _, store, coordinator, _ = prepared(**prepare_kwargs)
    stop = threading.Event()
    wake = threading.Event()
    wake.set()
    started = time.monotonic()
    original = coordinator.materialize_prefix
    def wrapper():
        result = original()
        head = store.get(bench.CONTROL_INDEX, bench.HEAD_ID)["source"]
        if not head["pending"]["batches"]:
            stop.set()
        return result
    coordinator.materialize_prefix = wrapper
    poll_interval = 0.1 if mode == "polling" else 5.0
    bench.materialize_loop(store, coordinator, stop, poll_interval, {},
                           wake_event=wake, poll_floor=0.005,
                           loop_cap=kwargs.get("loop_cap", 4), mode=mode)
    return time.monotonic() - started, store


def test_event_driven_full_run_drains_via_single_signal():
    """单次 wake 后连续物化直至 pending 清空，并自动退出。"""
    elapsed, store = _run_materializer_with_auto_stop({"count": 5}, "event-driven")
    assert elapsed < 1.0
    head = store.get(bench.CONTROL_INDEX, HEAD_ID)["source"]
    assert head["last_materialized_seq"] == 5
    assert not head["pending"]["batches"]


def test_event_driven_single_signal_wakes_within_poll_floor():
    """事件触发后应在一个 poll_floor 周期内完成首次物化。"""
    elapsed, store = _run_materializer_with_auto_stop({"count": 1}, "event-driven")
    assert elapsed < 1.0
    head = store.get(bench.CONTROL_INDEX, HEAD_ID)["source"]
    assert head["last_materialized_seq"] == 1


def test_polling_mode_still_drains_within_poll_interval():
    """保留 polling 模式回归：单次 wait 后再停表。"""
    elapsed, store = _run_materializer_with_auto_stop({"count": 3}, "polling", loop_cap=4)
    # polling 模式等 interval=0.1s 才启动物化；上限 1.5s。
    assert elapsed < 1.5
    head = store.get(bench.CONTROL_INDEX, HEAD_ID)["source"]
    assert head["last_materialized_seq"] == 3


def test_event_driven_invalid_mode_rejected():
    _, store, coordinator, _ = prepared()
    stop = threading.Event()
    stop.set()
    with pytest.raises(ValueError):
        bench.materialize_loop(store, coordinator, stop, 0.01, {},
                               wake_event=threading.Event(), poll_floor=0.01,
                               loop_cap=4, mode="unknown")


def test_event_driven_loop_cap_bounds_burst():
    """loop_cap 应限定单次事件最多连续物化次数，防止雪崩。

    W2Fε 场景强化（WC1 F2：旧版上界无判别力——包装器 `len(calls) >= 3`
    自截断 stop，3 的上界由测试自杀保证而非 loop_cap 机制）：
    ① 待物化批 13 > loop_cap 3 × 单次物化上限（MATERIALIZE_PREFIX_BATCHES=4）
    = 12——cap 失守（退化无界/改大）单次事件即可排空，必有第 4 调用抢跑；
    ② 包装器不自截断——闸门只记录与阻断现场，stop 全程由主线程执掌；
    ③ 判别钉：第 3 调用返回后放行 0.3s（≪ poll_floor 1.0s 的下一外层拍），
    单次事件内不得出现第 4 次 materialize_prefix——cap 失效即红。
    """
    client = FakeClient()
    prefix = "p01-batch-offline-"
    bench.provision(client, prefix)
    store = bench.ObservedStore(ElasticsearchBatchStore(client, index_prefix=prefix))
    coordinator = BatchAdmissionCoordinator(store, owner_id="batch-offline",
                                            owner_isolated=lambda: True,
                                            limits=BatchLimits(), clock=bench.utc_now)
    # 注入 13 个独立批次（每 accept_batch 一个、每批 1 项）——显著超 cap×单次上限。
    for index in range(13):
        record = bench.InputRecord(bench.make_request(index, "offline"), time.monotonic())
        coordinator.accept_batch([record.request])
    head = store.get(bench.CONTROL_INDEX, HEAD_ID)["source"]
    assert len(head["pending"]["batches"]) == 13
    wake = threading.Event()
    wake.set()
    stop = threading.Event()
    calls = []
    entered3, gate1 = threading.Event(), threading.Event()
    burst3_done, gate2 = threading.Event(), threading.Event()
    call4_entered = threading.Event()
    original = coordinator.materialize_prefix

    def gated():
        calls.append(time.monotonic())
        if len(calls) == 3:
            entered3.set()
            assert gate1.wait(5), "主线程未放行 gate1"      # 阻于第 3 调用前
        if len(calls) == 4:
            call4_entered.set()                             # cap 失守抢跑证据
        result = original()
        if len(calls) == 3:
            burst3_done.set()
            assert gate2.wait(5), "主线程未放行 gate2"      # 阻于第 3 调用后、第 4 前
        return result

    coordinator.materialize_prefix = gated
    errors: dict = {}
    thread = threading.Thread(
        target=bench.materialize_loop,
        args=(store, coordinator, stop, 5.0, errors),
        kwargs=dict(wake_event=wake, poll_floor=1.0, loop_cap=3, mode="event-driven"),
        daemon=True)
    thread.start()
    try:
        assert entered3.wait(2), "loop_cap=3 下未见第 3 次物化调用"
        assert len(calls) == 3                                # 阻断中：恰 3 次
        head = store.get(bench.CONTROL_INDEX, HEAD_ID)["source"]
        assert len(head["pending"]["batches"]) == 5           # 2 调用×4 批=8，余 5
        gate1.set()                                           # 放行第 3 调用
        assert burst3_done.wait(2)
        head = store.get(bench.CONTROL_INDEX, HEAD_ID)["source"]
        assert len(head["pending"]["batches"]) == 1           # 3 调用×4 批=12，余 1
        gate2.set()                                           # 放行；cap 守住则回等待
        time.sleep(0.3)                                       # ≪ poll_floor：无下拍
        # 判别钉：单次事件物化恰至 cap 即止——第 4 调用出现即 cap 失守
        assert not call4_entered.is_set(), (
            "loop_cap=3 失守：单次事件内出现第 4 次 materialize_prefix")
        assert len(calls) == 3
    finally:
        gate1.set()
        gate2.set()
        stop.set()
        thread.join(3)
    assert not thread.is_alive()
    assert errors == {}, f"物化循环吞了异常：{errors}"
