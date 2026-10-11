from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timezone
from threading import Lock

import pytest

from news_flash_dedup.admission import (
    AdmissionConflict,
    AdmissionCoordinator,
    AdmissionRequest,
    AdmissionUnknown,
    IdentityConflict,
    _jcs,
    _digest,
)


class Conflict(Exception):
    pass


class MemoryStore:
    def __init__(self):
        self.docs = {}
        self.lock = Lock()
        self.after_write = None
        self.after_get = None

    def get(self, index, key):
        with self.lock:
            value = deepcopy(self.docs.get((index, key)))
        if self.after_get:
            self.after_get(index, key, value)
        return value

    def create(self, index, key, body):
        with self.lock:
            if (index, key) in self.docs:
                raise Conflict()
            self.docs[index, key] = {"source": deepcopy(body), "seq_no": 0, "primary_term": 1}
        if self.after_write:
            self.after_write(index, key)

    def replace(self, index, key, body, seq_no, primary_term):
        with self.lock:
            old = self.docs[index, key]
            if (old["seq_no"], old["primary_term"]) != (seq_no, primary_term):
                raise Conflict()
            self.docs[index, key] = {
                "source": deepcopy(body), "seq_no": seq_no + 1, "primary_term": primary_term
            }
        if self.after_write:
            self.after_write(index, key)

    def is_conflict(self, error):
        return isinstance(error, Conflict)

    # ---------- P1a-T2：任务文档检索面（受理复用/容量/孤儿探针消费） ----------

    @staticmethod
    def _match_clause(source, clause):
        if len(clause) != 1:
            raise RuntimeError("memory search: clause must be single-key")
        kind, body = next(iter(clause.items()))
        if kind == "term":
            field, value = next(iter(body.items()))
            return source.get(field) == value
        if kind == "terms":
            field, values = next(iter(body.items()))
            return source.get(field) in values
        if kind == "range":
            field, bounds = next(iter(body.items()))
            value = source.get(field)
            if value is None:
                return False
            if "gt" in bounds and not value > bounds["gt"]:
                return False
            if "gte" in bounds and not value >= bounds["gte"]:
                return False
            if "lt" in bounds and not value < bounds["lt"]:
                return False
            if "lte" in bounds and not value <= bounds["lte"]:
                return False
            return True
        raise RuntimeError(f"memory search: unsupported clause {kind!r}")

    @staticmethod
    def _index_matches(pattern, index):
        # 尾星通配（work 族跨日 "news-dedup-work-v1-*"）；中缀/多星即拒。
        if "*" not in pattern:
            return pattern == index
        prefix, star, suffix = pattern.partition("*")
        if star != "*" or "*" in prefix or "*" in suffix:
            raise RuntimeError(
                "memory search: only trailing-star wildcard supported")
        return index.startswith(prefix) and (
            not suffix or index.endswith(suffix))

    def search(self, index, body):
        """b4_fake_es.FakeESClient.search 同语义最小求值器（duck 面）。

        支持：bool.filter term/terms/range、must_not terms、
        should terms（minimum_should_match=1）、arrival_seq asc/desc 排序、
        尾星通配索引、size/_source 投影/track_total_hits；超出即拒。
        """
        query = body.get("query", {})
        bool_q = query.get("bool") if isinstance(query, dict) else None
        if bool_q is None and query not in ({}, {"match_all": {}}):
            raise RuntimeError("memory search: only bool/match_all queries")
        bool_q = bool_q or {}
        should = bool_q.get("should")
        if should is not None:
            msm = bool_q.get("minimum_should_match", 1)
            if msm != 1:
                raise RuntimeError(
                    "memory search: only minimum_should_match=1 supported")
        with self.lock:
            snapshot = [
                (idx, key, deepcopy(doc["source"]))
                for (idx, key), doc in self.docs.items()]
        matched = []
        for idx, doc_id, source in snapshot:
            if not self._index_matches(index, idx):
                continue
            if any(not self._match_clause(source, clause)
                    for clause in bool_q.get("filter", [])):
                continue
            if any(self._match_clause(source, clause)
                    for clause in bool_q.get("must_not", [])):
                continue
            if should is not None and not any(
                    self._match_clause(source, clause) for clause in should):
                continue
            matched.append((idx, doc_id, source))
        sort = body.get("sort") or [{"arrival_seq": "asc"}]
        sort_specs = []
        for spec in sort:
            field, order = next(iter(spec.items()))
            if order not in ("asc", "desc"):
                raise RuntimeError("memory search: only asc/desc sort")
            sort_specs.append((field, order))

        def _sort_key(item):
            keys = []
            for field, order in sort_specs:
                value = item[2].get(field) if field != "record_id" else item[1]
                if order == "desc":
                    if not isinstance(value, int) or isinstance(value, bool):
                        raise RuntimeError(
                            "memory search: desc sort requires int field")
                    value = -value
                keys.append(value)
            return tuple(keys) + (item[1],)
        matched.sort(key=_sort_key)
        total = len(matched)
        size = body.get("size", 10)
        page = matched[:size]
        wanted = body.get("_source")
        hits = []
        for idx, doc_id, source in page:
            shown = source if wanted is None else {
                key: source.get(key) for key in wanted if key in source}
            hits.append({"_index": idx, "_id": doc_id, "_source": shown})
        return {"timed_out": False,
                "_shards": {"total": 1, "successful": 1, "failed": 0},
                "hits": {"total": {"value": total, "relation": "eq"},
                         "hits": hits}}


def request(request_id="100-1", item_id="100-1", text="甲公司发布新产品。", when=None):
    return AdmissionRequest(
        scope_id="default", request_id=request_id, item_id=item_id, text=text,
        received_at=when or datetime(2026, 9, 23, 15, 59, tzinfo=timezone.utc),
        schema_version="1", pipeline_version="v1", embedding_space_id="v3",
        delivery_route_ref="audit-route-v1",
        trace_id="trace-first",
    )


def coordinator(store):
    return AdmissionCoordinator(
        store, owner_id="writer-1", owner_isolated=lambda: True,
        clock=lambda: datetime(2026, 9, 23, 15, 59, tzinfo=timezone.utc),
    )


def test_t001_persisted_slot_survives_restart_before_materialization():
    store = MemoryStore()
    service = coordinator(store)
    accepted = service.accept(request(), materialize=False)
    assert accepted.arrival_seq == 1
    assert accepted.business_date == "2026-09-23"
    assert service.lookup("default", "100-1").arrival_seq == 1

    restarted = coordinator(store)
    restarted.recover()
    assert restarted.lookup("default", "100-1") == accepted
    assert store.get("news-dedup-control-v1", "admission_head")["source"]["pending"] is None


def test_t002_same_identity_100_concurrent_retries_use_one_sequence():
    store = MemoryStore()
    service = coordinator(store)
    with ThreadPoolExecutor(max_workers=20) as pool:
        results = list(pool.map(lambda _: service.accept(request()), range(100)))
    assert {result.arrival_seq for result in results} == {1}
    assert store.get("news-dedup-control-v1", "admission_head")["source"]["last_allocated_seq"] == 1


def test_t003_t078_raw_text_change_conflicts_even_if_only_whitespace_changes():
    store = MemoryStore()
    service = coordinator(store)
    service.accept(request(), materialize=False)
    with pytest.raises(IdentityConflict):
        service.accept(request(text="甲公司发布新产品。 "))
    assert service.accept(request(), materialize=False).arrival_seq == 1


def test_t079_same_item_with_different_request_is_conflict():
    store = MemoryStore()
    service = coordinator(store)
    service.accept(request(), materialize=False)
    with pytest.raises(IdentityConflict):
        service.accept(request(request_id="100-2", item_id="100-1"))


@pytest.mark.parametrize("failed_write", [
    "news-dedup-items-v1-2026.09.23",
    "request:unique", "item:unique", "seq:1", "admission_head",
])
def test_t013_recovery_after_each_materialization_write(failed_write):
    store = MemoryStore()
    service = coordinator(store)
    service.accept(request(), materialize=False)
    raised = False

    def crash(index, key):
        nonlocal raised
        if not raised and (index == failed_write or key == failed_write or
                           (failed_write == "request:unique" and key.startswith("request:")) or
                           (failed_write == "item:unique" and key.startswith("item:"))):
            raised = True
            raise OSError("injected crash after server write")

    store.after_write = crash
    with pytest.raises(AdmissionUnknown):
        service.recover()
    store.after_write = None
    coordinator(store).recover()
    assert coordinator(store).lookup("default", "100-1").arrival_seq == 1
    assert store.get("news-dedup-control-v1", "admission_head")["source"]["pending"] is None


def test_t077_pending_to_mapping_handoff_does_not_look_missing():
    store = MemoryStore()
    service = coordinator(store)
    accepted = service.accept(request(), materialize=False)
    calls = 0

    def handoff(index, key, value):
        nonlocal calls
        if index == "news-dedup-requests-v1" and key.startswith("request:"):
            calls += 1
            if calls == 1:
                store.after_get = None
                service.recover()
                store.after_get = handoff

    store.after_get = handoff
    assert service.lookup("default", "100-1") == accepted


def test_t077_read_error_is_not_treated_as_absence():
    store = MemoryStore()
    service = coordinator(store)
    service.accept(request(), materialize=False)

    def fail_read(index, key, value):
        if index == "news-dedup-requests-v1":
            raise OSError("read failed")

    store.after_get = fail_read
    with pytest.raises(OSError):
        service.lookup("default", "100-1")


def test_t080_unknown_head_cas_requires_reconciliation():
    store = MemoryStore()
    service = coordinator(store)

    def lost_ack(index, key):
        if key == "admission_head":
            raise OSError("lost CAS acknowledgement")

    store.after_write = lost_ack
    with pytest.raises(AdmissionUnknown):
        service.accept(request())
    store.after_write = None
    assert coordinator(store).accept(request()).arrival_seq == 1


def test_unisolated_owner_cannot_accept():
    store = MemoryStore()
    service = AdmissionCoordinator(store, owner_id="writer-1", owner_isolated=lambda: False)
    with pytest.raises(AdmissionConflict):
        service.accept(request())
    assert store.docs == {}


def test_t079_mixed_concurrent_retries_keep_one_binding():
    store = MemoryStore()
    service = coordinator(store)

    def submit(index):
        try:
            return service.accept(request(text="原文" if index % 2 == 0 else "原文 ")).arrival_seq
        except IdentityConflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=20) as pool:
        outcomes = list(pool.map(submit, range(100)))
    assert set(outcomes) in ({1, "conflict"}, {"conflict", 1})
    assert outcomes.count(1) == 50
    assert outcomes.count("conflict") == 50
    assert store.get("news-dedup-control-v1", "admission_head")["source"]["last_allocated_seq"] == 1


def test_t084_jcs_identity_vectors_and_fingerprint_scope():
    assert _jcs({"text": "汉😀 ", "item_id": "9-1"}) == (
        '{"item_id":"9-1","text":"汉😀 "}'.encode("utf-8")
    )
    assert _jcs(["ab", "c"]) != _jcs(["a", "bc"])
    assert _digest({"item_id": "9-1", "text": "正文"}) != _digest(
        {"item_id": "9-1", "text": "正文 "}
    )
    store = MemoryStore()
    first = coordinator(store).accept(request())
    same = coordinator(store).accept(request())
    assert first.record_id == same.record_id


def test_t086_global_sequence_preserves_scope_and_business_date_gaps():
    store = MemoryStore()
    first = coordinator(store).accept(request())
    other_scope = AdmissionCoordinator(
        store, owner_id="writer-1", owner_isolated=lambda: True,
        clock=lambda: datetime(2026, 9, 23, 15, 59, tzinfo=timezone.utc),
    ).accept(AdmissionRequest(**{**request(request_id="200-1", item_id="200-1").__dict__,
                                 "scope_id": "other"}))
    next_day = AdmissionCoordinator(
        store, owner_id="writer-1", owner_isolated=lambda: True,
        clock=lambda: datetime(2026, 9, 23, 16, 1, tzinfo=timezone.utc),
    ).accept(request(request_id="300-1", item_id="300-1"))
    assert [(r.arrival_seq, r.business_date) for r in (first, other_scope, next_day)] == [
        (1, "2026-09-23"), (2, "2026-09-23"), (3, "2026-09-24")
    ]
    assert store.get("news-dedup-requests-v1", "seq:2")["source"]["scope_id"] == "other"
    assert store.get("news-dedup-requests-v1", "seq:3")["source"]["business_date"] == "2026-09-24"


def test_expired_mapping_cannot_be_reused_as_live_task():
    store = MemoryStore()
    coordinator(store).accept(request())
    late = AdmissionCoordinator(
        store, owner_id="writer-1", owner_isolated=lambda: True,
        clock=lambda: datetime(2026, 9, 30, 0, 1, tzinfo=timezone.utc),
    )
    with pytest.raises(AdmissionConflict):
        late.accept(request())


def test_unfrozen_main_record_has_no_delivery_intent():
    store = MemoryStore()
    receipt = coordinator(store).accept(request())
    main = store.get("news-dedup-items-v1-2026.09.23", receipt.record_id)["source"]
    assert main["task_state"] == "accepted"
    assert main["delivery_state"] == "not_ready"


def test_new_owner_requires_old_writer_isolation_then_recovers_pending():
    store = MemoryStore()
    old = coordinator(store)
    accepted = old.accept(request(), materialize=False)
    unsafe = AdmissionCoordinator(
        store, owner_id="writer-2", owner_isolated=lambda: False,
        clock=lambda: datetime(2026, 9, 23, 15, 59, tzinfo=timezone.utc),
    )
    with pytest.raises(AdmissionConflict):
        unsafe.takeover()
    assert store.get("news-dedup-control-v1", "admission_head")["source"]["owner_id"] == "writer-1"

    new = AdmissionCoordinator(
        store, owner_id="writer-2", owner_isolated=lambda: True,
        clock=lambda: datetime(2026, 9, 23, 15, 59, tzinfo=timezone.utc),
    )
    new.takeover()
    assert new.lookup("default", "100-1") == accepted
    assert store.get("news-dedup-control-v1", "admission_head")["source"]["pending"] is None
    with pytest.raises(AdmissionConflict):
        old.accept(request(request_id="200-1", item_id="200-1"))


def test_first_trace_is_durable_and_retry_trace_does_not_replace_it():
    store = MemoryStore()
    service = coordinator(store)
    first = service.accept(request(), materialize=False)
    assert first.reused is False
    retried = service.accept(AdmissionRequest(**{**request().__dict__, "trace_id": "trace-retry"}),
                             materialize=False)
    assert retried.reused is True
    assert retried.trace_id == "trace-first"
    coordinator(store).recover()
    after_restart = coordinator(store).accept(
        AdmissionRequest(**{**request().__dict__, "trace_id": "trace-later"})
    )
    assert after_restart.reused is True
    assert after_restart.trace_id == "trace-first"
    main = store.get("news-dedup-items-v1-2026.09.23", first.record_id)["source"]
    assert main["diagnostics"]["delivery"]["trace_id"] == "trace-first"


@pytest.mark.parametrize("field,value", [
    ("request_id", "different"),
    ("item_id", "different"),
    ("record_id", "different"),
    ("business_date", "2026-09-24"),
    ("expires_at", "2026-10-02T16:00:00.000000Z"),
    ("pipeline_version", None),
])
def test_materialized_retry_rejects_corrupt_main_identity(field, value):
    store = MemoryStore()
    accepted = coordinator(store).accept(request())
    key = ("news-dedup-items-v1-2026.09.23", accepted.record_id)
    store.docs[key]["source"][field] = value
    with pytest.raises(AdmissionConflict):
        coordinator(store).accept(request())


def test_materialized_retry_rejects_missing_sequence_registration():
    store = MemoryStore()
    coordinator(store).accept(request())
    del store.docs["news-dedup-requests-v1", "seq:1"]
    with pytest.raises(AdmissionConflict):
        coordinator(store).accept(request())


def test_materialized_request_id_cannot_bind_a_new_item_id():
    store = MemoryStore()
    coordinator(store).accept(request())
    with pytest.raises(IdentityConflict):
        coordinator(store).accept(request(item_id="another-item"))


def test_materialized_item_id_cannot_bind_a_new_request_id():
    store = MemoryStore()
    coordinator(store).accept(request())
    with pytest.raises(IdentityConflict):
        coordinator(store).accept(request(request_id="another-request"))


def test_incomplete_same_identity_mapping_is_not_reported_as_409():
    store = MemoryStore()
    coordinator(store).accept(request())
    receipt = coordinator(store).lookup("default", "100-1")
    del store.docs["news-dedup-requests-v1", "item:" + receipt.record_id]
    with pytest.raises(AdmissionConflict):
        coordinator(store).accept(request())
