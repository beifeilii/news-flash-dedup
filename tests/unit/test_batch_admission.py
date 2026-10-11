from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from news_flash_dedup import batch_admission
from news_flash_dedup.admission import AdmissionConflict, AdmissionUnknown, IdentityConflict
from news_flash_dedup.batch_admission import BatchAdmissionCoordinator, BatchLimits
from news_flash_dedup.materialize_runtime import MaterializeRetryRuntime
from news_flash_dedup.materialize_terminal import (
    load_tombstone, tombstone_exists_confirmed, tombstone_index)
from test_admission import MemoryStore, request


NOW = datetime(2026, 9, 24, 0, 0, tzinfo=timezone.utc)


class BatchMemoryStore(MemoryStore):
    def __init__(self):
        super().__init__()
        self.bulk_calls = 0
        self.mget_calls = 0
        self.fail_bulk_after = None

    def mget(self, keys):
        self.mget_calls += 1
        return [self.get(index, key) for index, key in keys]

    def bulk_create(self, documents):
        self.bulk_calls += 1
        for position, (index, key, body) in enumerate(documents):
            if self.fail_bulk_after is not None and position == self.fail_bulk_after:
                self.fail_bulk_after = None
                raise OSError("partial bulk acknowledgement lost")
            try:
                self.create(index, key, body)
            except Exception as error:
                if not self.is_conflict(error):
                    raise
        if self.fail_bulk_after == -1:
            self.fail_bulk_after = None
            raise OSError("bulk acknowledgement lost after all writes")


class ClassifiedBatchMemoryStore(BatchMemoryStore):
    """P0-T4：带 ``bulk_create_classified`` 逐条解析面的内存 store（真实 ES 形态）。

    - ``item_statuses``：按文档位置注入 bulk item 状态码（400/503 等）；
      未注入位置走真实 create（撞已存在→409）。
    - ``request_error``：请求级异常一次性注入（PermanentBulkError/OSError）。
    - ``mget_missing``：读回未命中注入（409→读回 None=未确认形态）。
    """

    def __init__(self):
        super().__init__()
        self.item_statuses = {}
        self.request_error = None
        self.mget_missing = set()

    def mget(self, keys):
        self.mget_calls += 1
        return [None if (index, key) in self.mget_missing
                else self.get(index, key) for index, key in keys]

    def bulk_create_classified(self, documents):
        from news_flash_dedup.materialize_failure import bulk_document_outcome

        self.bulk_calls += 1
        if self.request_error is not None:
            error, self.request_error = self.request_error, None
            raise error
        outcomes = []
        for position, (index, key, body) in enumerate(documents):
            status = self.item_statuses.get(position)
            if status is None:
                try:
                    self.create(index, key, body)
                    status = 201
                except Exception as error:
                    if not self.is_conflict(error):
                        raise
                    status = 409
            outcomes.append(bulk_document_outcome(
                position, index, key, status))
        return tuple(outcomes)


def coordinator(store, owner="writer-1", limits=None, when=NOW, isolated=True):
    return BatchAdmissionCoordinator(
        store, owner_id=owner, owner_isolated=lambda: isolated,
        limits=limits or BatchLimits(), clock=lambda: when,
    )


def test_one_cas_accepts_three_complete_requests_with_contiguous_sequences():
    store = BatchMemoryStore()
    service = coordinator(store)
    receipts = service.accept_batch([
        request(request_id=f"{n}-1", item_id=f"{n}-1", text=f"正文{n}")
        for n in (1, 2, 3)
    ])
    assert [receipt.arrival_seq for receipt in receipts] == [1, 2, 3]
    head = store.get("news-dedup-control-v1", "batch_admission_head")["source"]
    assert head["last_allocated_seq"] == 3
    assert head["last_materialized_seq"] == 0
    assert len(head["pending"]["batches"]) == 1
    assert len(head["pending"]["batches"][0]["entries"]) == 3
    assert store.bulk_calls == 0


def test_batch_coalesces_same_identity_and_rejects_cross_binding():
    store = BatchMemoryStore()
    service = coordinator(store)
    first, same = service.accept_batch([request(), request()])
    assert first.arrival_seq == same.arrival_seq == 1
    assert first.reused is False and same.reused is True
    assert service.accept_batch([request()])[0].reused is True
    with pytest.raises(IdentityConflict):
        service.accept_batch([request(item_id="other")])
    with pytest.raises(IdentityConflict):
        service.accept_batch([request(request_id="other")])
    with pytest.raises(IdentityConflict):
        service.accept_batch([request(text="甲公司发布新产品。 ")])


def test_conflict_inside_new_batch_aborts_entire_batch_without_sequence():
    store = BatchMemoryStore()
    service = coordinator(store)
    with pytest.raises(IdentityConflict):
        service.accept_batch([request(), request(text="变体")])
    assert store.get("news-dedup-control-v1", "batch_admission_head")["source"]["last_allocated_seq"] == 0


def test_capacity_rejects_before_cas_and_does_not_hide_memory_backlog():
    store = BatchMemoryStore()
    service = coordinator(store, limits=BatchLimits(max_batch_items=2, max_log_items=2))
    service.accept_batch([request(request_id="1-1", item_id="1-1"),
                          request(request_id="2-1", item_id="2-1")])
    with pytest.raises(AdmissionConflict):
        service.accept_batch([request(request_id="3-1", item_id="3-1")])
    assert store.get("news-dedup-control-v1", "batch_admission_head")["source"]["last_allocated_seq"] == 2


def test_unknown_cas_blocks_new_sequence_until_new_owner_takes_over():
    store = BatchMemoryStore()
    service = coordinator(store)
    service._head()
    fired = False

    def lost_ack(index, key):
        nonlocal fired
        if (key == "batch_admission_head" and
                store.docs[(index, key)]["source"]["last_allocated_seq"] == 1 and not fired):
            fired = True
            raise OSError("CAS acknowledgement lost")

    store.after_write = lost_ack
    with pytest.raises(AdmissionUnknown):
        service.accept_batch([request()])
    assert store.get("news-dedup-control-v1", "batch_admission_head")["source"]["last_allocated_seq"] == 1
    store.after_write = None
    with pytest.raises(AdmissionConflict):
        service.accept_batch([request(request_id="2-1", item_id="2-1")])
    replacement = coordinator(store, owner="writer-2")
    replacement.takeover()
    recovered = replacement.accept_batch([request()])[0]
    assert recovered.arrival_seq == 1 and recovered.reused is True
    assert replacement.accept_batch([request(request_id="2-1", item_id="2-1")])[0].arrival_seq == 2


def test_partial_bulk_replays_deterministic_ids_then_shrinks_prefix():
    store = BatchMemoryStore()
    service = coordinator(store)
    service.accept_batch([request(request_id="1-1", item_id="1-1"),
                          request(request_id="2-1", item_id="2-1")])
    store.fail_bulk_after = 3
    with pytest.raises(AdmissionUnknown):
        service.materialize_oldest()
    head = store.get("news-dedup-control-v1", "batch_admission_head")["source"]
    assert head["last_materialized_seq"] == 0
    assert len(head["pending"]["batches"]) == 1
    assert service.materialize_oldest() == 2
    head = store.get("news-dedup-control-v1", "batch_admission_head")["source"]
    assert head["last_materialized_seq"] == 2
    assert head["pending"]["batches"] == []
    assert all(store.get("news-dedup-requests-v1", f"seq:{n}") for n in (1, 2))


def test_interleaved_scope_and_date_keep_global_order_and_registration():
    store = BatchMemoryStore()
    service = coordinator(store)
    first = request(request_id="1-1", item_id="1-1")
    other = replace(request(request_id="2-1", item_id="2-1"), scope_id="other")
    receipts = service.accept_batch([first, other])
    next_day = coordinator(store, when=datetime(2026, 9, 24, 16, 1, tzinfo=timezone.utc))
    later = next_day.accept_batch([request(request_id="3-1", item_id="3-1")])[0]
    assert [(r.arrival_seq, r.business_date) for r in (*receipts, later)] == [
        (1, "2026-09-24"), (2, "2026-09-24"), (3, "2026-09-25")
    ]
    service.materialize_oldest()
    next_day.materialize_oldest()
    assert store.get("news-dedup-requests-v1", "seq:2")["source"]["scope_id"] == "other"
    assert store.get("news-dedup-requests-v1", "seq:3")["source"]["business_date"] == "2026-09-25"


def test_batch_byte_limit_rejects_before_cas():
    store = BatchMemoryStore()
    service = coordinator(store, limits=BatchLimits(max_batch_bytes=400))
    with pytest.raises(AdmissionConflict):
        service.accept_batch([request(text="正文" * 100)])
    assert store.get("news-dedup-control-v1", "batch_admission_head")["source"]["last_allocated_seq"] == 0


def test_materialization_replay_preserves_advanced_main_state():
    store = BatchMemoryStore()
    service = coordinator(store)
    receipt = service.accept_batch([request()])[0]
    store.fail_bulk_after = -1
    with pytest.raises(AdmissionUnknown):
        service.materialize_oldest()
    main = store.docs[("news-dedup-items-v1-2026.09.24", receipt.record_id)]["source"]
    main["task_state"] = "succeeded"
    main["delivery_state"] = "pending"
    main["result"] = {"version": 1}
    main["diagnostics"]["delivery"]["payload_hash"] = "later"
    assert service.materialize_oldest() == 1
    saved = store.get("news-dedup-items-v1-2026.09.24", receipt.record_id)["source"]
    assert saved["task_state"] == "succeeded"
    assert saved["delivery_state"] == "pending"
    assert saved["result"] == {"version": 1}
    assert saved["diagnostics"]["delivery"]["payload_hash"] == "later"


@pytest.mark.parametrize("field,value", [
    ("route_ref", "wrong-route"),
    ("trace_id", "wrong-trace"),
])
def test_materialization_replay_rejects_changed_frozen_delivery(field, value):
    store = BatchMemoryStore()
    service = coordinator(store)
    receipt = service.accept_batch([request()])[0]
    store.fail_bulk_after = -1
    with pytest.raises(AdmissionUnknown):
        service.materialize_oldest()
    main = store.docs[("news-dedup-items-v1-2026.09.24", receipt.record_id)]["source"]
    main["diagnostics"]["delivery"][field] = value
    with pytest.raises(AdmissionConflict):
        service.materialize_oldest()


@pytest.mark.parametrize("materialized", [False, True])
def test_batch_reuse_rejects_logically_expired_request(materialized):
    store = BatchMemoryStore()
    service = coordinator(store)
    service.accept_batch([request()])
    if materialized:
        service.materialize_oldest()
    expired = coordinator(store, when=datetime(2026, 10, 2, tzinfo=timezone.utc))
    with pytest.raises(AdmissionConflict):
        expired.accept_batch([request()])


@pytest.mark.parametrize("field,value", [
    ("raw_hash", "wrong"),
    ("schema_version", ""),
    ("pipeline_version", ""),
    ("embedding_space_id", ""),
    ("accepted_at", ""),
])
def test_materialized_reuse_rejects_corrupt_main_fields(field, value):
    store = BatchMemoryStore()
    service = coordinator(store)
    receipt = service.accept_batch([request()])[0]
    service.materialize_oldest()
    main = store.docs[("news-dedup-items-v1-2026.09.24", receipt.record_id)]["source"]
    main[field] = value
    with pytest.raises(AdmissionConflict):
        service.accept_batch([request()])


@pytest.mark.parametrize("field,value", [
    ("route_ref", ""),
    ("trace_id", 123),
])
def test_materialized_reuse_rejects_invalid_frozen_delivery(field, value):
    store = BatchMemoryStore()
    service = coordinator(store)
    receipt = service.accept_batch([request()])[0]
    service.materialize_oldest()
    main = store.docs[("news-dedup-items-v1-2026.09.24", receipt.record_id)]["source"]
    main["diagnostics"]["delivery"][field] = value
    with pytest.raises(AdmissionConflict):
        service.accept_batch([request()])


def test_materialized_reuse_requires_consistent_sequence_registration():
    store = BatchMemoryStore()
    service = coordinator(store)
    service.accept_batch([request()])
    service.materialize_oldest()
    store.docs.pop(("news-dedup-requests-v1", "seq:1"))
    with pytest.raises(AdmissionConflict):
        service.accept_batch([request()])


def test_expired_pending_is_quarantined_without_bulk_or_watermark_advance():
    store = BatchMemoryStore()
    service = coordinator(store)
    service.accept_batch([request()])
    expired = coordinator(store, when=datetime(2026, 10, 2, tzinfo=timezone.utc))
    with pytest.raises(AdmissionConflict, match="expired"):
        expired.recover()
    head = store.get("news-dedup-control-v1", "batch_admission_head")["source"]
    assert head["last_allocated_seq"] == 1
    assert head["last_materialized_seq"] == 0
    assert len(head["pending"]["batches"]) == 1
    assert head["checkpoint"]["batch_quarantine"]["first_seq"] == 1
    assert store.bulk_calls == 0
    with pytest.raises(AdmissionConflict, match="quarantined"):
        expired.accept_batch([request(request_id="2-1", item_id="2-1")])


def test_reconcile_proves_pending_or_all_four_materialized_documents():
    store = BatchMemoryStore()
    service = coordinator(store)
    original = request()
    service.accept_batch([original])
    assert service.reconcile(original).arrival_seq == 1
    service.materialize_oldest()
    assert service.reconcile(original).arrival_seq == 1
    store.docs.pop(("news-dedup-requests-v1", "seq:1"))
    with pytest.raises(AdmissionConflict):
        service.reconcile(original)


CONTROL = "news-dedup-control-v1"
HEAD = "batch_admission_head"
EXPIRY = datetime(2026, 9, 30, 16, 0, tzinfo=timezone.utc)


def head_source(store):
    return store.docs[(CONTROL, HEAD)]["source"]


@pytest.mark.parametrize("partial", [False, True])
@pytest.mark.parametrize("action", ["recover", "takeover"])
def test_d_plus_7_recovery_retains_log_without_recreating_expired_documents(partial, action):
    from copy import deepcopy
    store = BatchMemoryStore()
    service = coordinator(store)
    service.accept_batch([request()])
    if partial:
        store.fail_bulk_after = 2
        with pytest.raises(AdmissionUnknown):
            service.materialize_oldest()
    pending = deepcopy(head_source(store)["pending"])
    business_docs = {key: deepcopy(value) for key, value in store.docs.items() if key != (CONTROL, HEAD)}
    calls = store.bulk_calls
    expired = coordinator(store, owner="writer-2" if action == "takeover" else "writer-1", when=EXPIRY)
    with pytest.raises(AdmissionConflict):
        getattr(expired, action)()
    source = head_source(store)
    assert source["pending"] == pending
    assert source["last_allocated_seq"] == 1 and source["last_materialized_seq"] == 0
    assert source["checkpoint"]["batch_quarantine"]["reason"] == "logical_expiry"
    assert set(source["checkpoint"]["batch_quarantine"]) == {
        "batch_id", "digest", "first_seq", "last_seq", "reason", "recorded_at"}
    assert {key: value for key, value in store.docs.items() if key != (CONTROL, HEAD)} == business_docs
    assert store.bulk_calls == calls


@pytest.mark.parametrize("stage", ["before_bulk", "bulk", "partial_bulk", "readback", "before_shrink"])
def test_expiry_crossing_materialization_stages_never_shrinks_or_resumes(stage):
    from copy import deepcopy
    store = BatchMemoryStore()
    service = coordinator(store)
    service.accept_batch([request()])
    pending = deepcopy(head_source(store)["pending"])
    now = [NOW]
    service.clock = lambda: now[0]
    if stage == "before_bulk":
        original = service._documents

        def documents(entries):
            result = original(entries)
            now[0] = EXPIRY
            return result

        service._documents = documents
    elif stage in {"bulk", "partial_bulk"}:
        original = store.bulk_create

        def bulk(documents):
            try:
                if stage == "partial_bulk":
                    store.fail_bulk_after = 2
                original(documents)
            finally:
                now[0] = EXPIRY

        store.bulk_create = bulk
    elif stage == "readback":
        original = store.mget

        def mget(keys):
            result = original(keys)
            now[0] = EXPIRY
            return result

        store.mget = mget
    else:
        def after_get(index, key, value):
            if key == HEAD and store.mget_calls >= 2:
                now[0] = EXPIRY
        store.after_get = after_get
    with pytest.raises(AdmissionConflict):
        service.materialize_oldest()
    assert head_source(store)["pending"] == pending
    assert head_source(store)["last_materialized_seq"] == 0
    assert head_source(store)["checkpoint"]["batch_quarantine"]["reason"] == "logical_expiry"
    assert store.bulk_calls == (0 if stage == "before_bulk" else 1)
    calls = store.bulk_calls
    with pytest.raises(AdmissionConflict):
        service.recover()
    assert store.bulk_calls == calls


@pytest.mark.parametrize("part", ["main", "request", "item", "seq", "route"])
def test_deterministic_content_conflict_persists_block_across_restart(part):
    from copy import deepcopy
    store = BatchMemoryStore()
    service = coordinator(store)
    service.accept_batch([request()])
    pending = deepcopy(head_source(store)["pending"])
    store.fail_bulk_after = -1
    with pytest.raises(AdmissionUnknown):
        service.materialize_oldest()
    docs = service._documents(pending["batches"][0]["entries"])
    position = {"main": 0, "request": 1, "item": 2, "seq": 3, "route": 0}[part]
    index, key, _ = docs[position]
    source = store.docs[index, key]["source"]
    if part == "route":
        source["diagnostics"]["delivery"]["route_ref"] = "other-route"
    else:
        source["request_id"] = "other-binding"
    with pytest.raises(AdmissionConflict):
        service.materialize_oldest()
    checkpoint = head_source(store)["checkpoint"]["batch_quarantine"]
    assert checkpoint["reason"] == "deterministic_content_conflict"
    assert head_source(store)["pending"] == pending
    assert head_source(store)["last_allocated_seq"] == 1
    assert head_source(store)["last_materialized_seq"] == 0
    restarted = coordinator(store)
    calls = store.bulk_calls
    with pytest.raises(AdmissionConflict):
        restarted.accept_batch([request(request_id="new", item_id="new")])
    with pytest.raises(AdmissionConflict):
        restarted.recover()
    assert store.bulk_calls == calls


@pytest.mark.parametrize("fault", ["digest", "extra_field", "missing_field", "wrong_type", "fingerprint",
                                   "record_id", "seq_gap", "allocated", "watermark", "empty_batch",
                                   "duplicate_batch", "duplicate_binding", "invalid_date", "expiry", "accepted_at"])
@pytest.mark.parametrize("action", ["recover", "takeover"])
def test_corrupt_restored_log_is_validated_before_any_materialization(fault, action):
    from copy import deepcopy
    from news_flash_dedup.batch_admission import _batch_digest
    store = BatchMemoryStore()
    service = coordinator(store)
    service.accept_batch([request(), request(request_id="second", item_id="second")])
    source = head_source(store)
    batch = source["pending"]["batches"][0]
    entry = batch["entries"][0]
    if fault == "digest":
        batch["digest"] = "0" * 64
    elif fault == "extra_field":
        entry["unexpected"] = True
    elif fault == "missing_field":
        entry.pop("text")
    elif fault == "wrong_type":
        entry["trace_id"] = 42
    elif fault == "fingerprint":
        entry["business_fingerprint"] = "0" * 64
    elif fault == "record_id":
        entry["record_id"] = "0" * 64
    elif fault == "seq_gap":
        entry["arrival_seq"] = 3
    elif fault == "allocated":
        source["last_allocated_seq"] = 3
    elif fault == "watermark":
        source["last_materialized_seq"] = 1
    elif fault == "empty_batch":
        batch["entries"] = []
    elif fault == "duplicate_batch":
        source["pending"]["batches"].append(deepcopy(batch))
    elif fault == "duplicate_binding":
        batch["entries"][1] = {**entry, "arrival_seq": 2}
    elif fault == "invalid_date":
        entry["business_date"] = "2026-99-99"
    elif fault == "expiry":
        entry["expires_at"] = "2027-01-01T00:00:00Z"
    else:
        entry["accepted_at"] = "not-a-time"
    if fault != "digest":
        batch["digest"] = _batch_digest(batch["entries"])
    before = deepcopy(source)
    restarted = coordinator(store, owner="writer-2" if action == "takeover" else "writer-1")
    with pytest.raises(AdmissionConflict):
        getattr(restarted, action)()
    assert store.bulk_calls == 0
    assert head_source(store) == before
    with pytest.raises(AdmissionConflict):
        service.accept_batch([request(request_id="new", item_id="new")])


@pytest.mark.parametrize("materialized", [False, True])
def test_reconcile_same_item_other_request_cannot_report_absence(materialized):
    store = BatchMemoryStore()
    service = coordinator(store)
    service.accept_batch([request()])
    if materialized:
        service.materialize_oldest()
    with pytest.raises(AdmissionConflict):
        service.reconcile(request(request_id="alias"))
    assert head_source(store)["last_allocated_seq"] == 1


def test_reconcile_handoff_after_initial_miss_reads_mapping_again():
    store = BatchMemoryStore()
    service = coordinator(store)
    expected = service.accept_batch([request()])[0]
    seen = []

    def after_get(index, key, value):
        seen.append(key)
        if key.startswith("request:") and value is None:
            store.after_get = None
            service.materialize_oldest()
            store.after_get = lambda index, key, value: seen.append(key)

    store.after_get = after_get
    assert service.reconcile(request()) == expected
    assert sum(key.startswith("request:") for key in seen) >= 2


@pytest.mark.parametrize("part", ["request", "item", "seq", "main"])
def test_reconcile_requires_four_valid_documents(part):
    store = BatchMemoryStore()
    service = coordinator(store)
    service.accept_batch([request()])
    docs = service._documents(head_source(store)["pending"]["batches"][0]["entries"])
    service.recover()
    position = {"main": 0, "request": 1, "item": 2, "seq": 3}[part]
    index, key, _ = docs[position]
    store.docs[index, key]["source"]["kind" if part != "main" else "record_id"] = "incorrect"
    with pytest.raises(AdmissionConflict):
        service.reconcile(request())


def test_mixed_receipts_keep_positions_reuse_and_original_frozen_metadata():
    store = BatchMemoryStore()
    service = coordinator(store)
    first = request()
    second = request(request_id="pending", item_id="pending")
    first_receipt = service.accept_batch([first])[0]
    service.recover()
    second_receipt = service.accept_batch([second])[0]
    next_day = coordinator(store, when=datetime(2026, 9, 24, 16, 1, tzinfo=timezone.utc))
    changed = {"schema_version": "2", "pipeline_version": "v2", "embedding_space_id": "v4",
               "delivery_route_ref": "route-new", "trace_id": "trace-new"}
    new = replace(request(request_id="new", item_id="new"), **changed)
    result = next_day.accept_batch([replace(first, **changed), replace(second, **changed), new, new])
    assert result[:2] == [first_receipt, second_receipt]
    assert [item.arrival_seq for item in result] == [1, 2, 3, 3]
    assert [item.reused for item in result] == [True, True, False, True]
    assert result[2].business_date == "2026-09-25"
    assert result[2].delivery_route_ref == "route-new" and result[2].trace_id == "trace-new"
    next_day.recover()
    assert next_day.reconcile(first) == first_receipt
    assert next_day.reconcile(second) == second_receipt
    assert next_day.reconcile(new) == result[2]


def test_shrink_ack_loss_is_reconciled_without_reallocating_or_replaying():
    store = BatchMemoryStore()
    service = coordinator(store)
    expected = service.accept_batch([request()])[0]

    def lost_ack(index, key):
        if key == HEAD and head_source(store)["last_materialized_seq"] == 1:
            store.after_write = None
            raise OSError("shrink acknowledgement lost")

    store.after_write = lost_ack
    with pytest.raises(AdmissionUnknown):
        service.materialize_oldest()
    assert service.reconcile(request()) == expected
    calls = store.bulk_calls
    assert service.recover() == 1
    assert store.bulk_calls == calls
    assert service.accept_batch([request()])[0].reused
    assert head_source(store)["last_allocated_seq"] == 1


@pytest.mark.parametrize("winner", ["append", "shrink"])
def test_append_and_shrink_cas_competition_preserves_both_prefix_and_new_batch(winner):
    store = BatchMemoryStore()
    service = coordinator(store)
    worker = coordinator(store)
    service.accept_batch([request()])
    original = store.replace
    fired = False

    def competing(index, key, body, seq_no, primary_term):
        nonlocal fired
        target = body["last_materialized_seq"] == 1 if winner == "append" else body["last_allocated_seq"] == 2
        if not fired and target:
            fired = True
            if winner == "append":
                service.accept_batch([request(request_id="new", item_id="new")])
            else:
                worker.materialize_oldest()
        original(index, key, body, seq_no, primary_term)

    store.replace = competing
    if winner == "append":
        worker.materialize_oldest()
    else:
        service.accept_batch([request(request_id="new", item_id="new")])
    assert fired
    assert head_source(store)["last_allocated_seq"] == 2
    assert head_source(store)["last_materialized_seq"] == 1
    assert len(head_source(store)["pending"]["batches"]) == 1
    assert service.recover() == 2


def test_takeover_rejects_unisolated_old_writer_without_touching_head():
    from copy import deepcopy
    store = BatchMemoryStore()
    coordinator(store).accept_batch([request()])
    before = deepcopy(store.docs)
    replacement = coordinator(store, owner="writer-2", isolated=False)
    with pytest.raises(AdmissionConflict):
        replacement.takeover()
    assert store.docs == before and store.bulk_calls == 0


def test_quarantine_ack_loss_keeps_checkpoint_and_pending_blocked_after_takeover():
    from copy import deepcopy
    from news_flash_dedup.batch_es_store import PermanentBulkError
    store = BatchMemoryStore()
    service = coordinator(store)
    service.accept_batch([request()])
    before = deepcopy(head_source(store)["pending"])

    def permanent(documents):
        raise PermanentBulkError("permanent test failure")

    def lost_ack(index, key):
        if key == HEAD and head_source(store)["checkpoint"]:
            store.after_write = None
            raise OSError("checkpoint acknowledgement lost")

    store.bulk_create = permanent
    store.after_write = lost_ack
    with pytest.raises(AdmissionUnknown):
        service.materialize_oldest()
    with pytest.raises(AdmissionConflict):
        service.accept_batch([request(request_id="new", item_id="new")])
    with pytest.raises(AdmissionConflict):
        coordinator(store, owner="writer-2").takeover()
    assert head_source(store)["pending"] == before
    assert head_source(store)["last_materialized_seq"] == 0
    assert head_source(store)["checkpoint"]["batch_quarantine"]["reason"] == "permanent_bulk_error"


def test_corruption_between_readback_and_shrink_cannot_advance_prefix():
    store = BatchMemoryStore()
    service = coordinator(store)
    service.accept_batch([request()])
    original = store.mget

    def corrupt(keys):
        result = original(keys)
        head_source(store)["pending"]["batches"][0]["entries"][0]["text"] = "corrupt"
        return result

    store.mget = corrupt
    with pytest.raises(AdmissionConflict):
        service.materialize_oldest()
    assert head_source(store)["last_materialized_seq"] == 0
    assert len(head_source(store)["pending"]["batches"]) == 1


def test_unknown_inflight_append_cannot_be_reported_as_definitely_absent():
    store = BatchMemoryStore()
    service = coordinator(store)
    service._head()

    def in_flight(index, key, body, seq_no, primary_term):
        raise OSError("request still in flight")

    store.replace = in_flight
    with pytest.raises(AdmissionUnknown):
        service.accept_batch([request()])
    with pytest.raises(AdmissionUnknown):
        service.reconcile(request())
    assert head_source(store)["last_allocated_seq"] == 0


@pytest.mark.parametrize("field", ["request_id", "item_id", "text"])
def test_identity_conflict_is_only_reported_before_entire_mixed_batch_cas(field):
    store = BatchMemoryStore()
    service = coordinator(store)
    service.accept_batch([request()])
    before = store.get(CONTROL, HEAD)
    conflict = replace(request(), **{field: "different"})
    with pytest.raises(IdentityConflict):
        service.accept_batch([request(request_id="new", item_id="new"), conflict])
    assert store.get(CONTROL, HEAD) == before


def test_shrink_cas_conflict_rechecks_expiry_without_reusing_old_liveness():
    from test_admission import Conflict
    store = BatchMemoryStore()
    service = coordinator(store)
    service.accept_batch([request()])
    original = store.replace
    fired = False

    def conflict_at_boundary(index, key, body, seq_no, primary_term):
        nonlocal fired
        if not fired and body["last_materialized_seq"] == 1:
            fired = True
            service.clock = lambda: EXPIRY
            raise Conflict()
        original(index, key, body, seq_no, primary_term)

    store.replace = conflict_at_boundary
    with pytest.raises(AdmissionConflict):
        service.materialize_oldest()
    assert fired
    assert head_source(store)["last_materialized_seq"] == 0
    assert head_source(store)["checkpoint"]["batch_quarantine"]["reason"] == "logical_expiry"


@pytest.mark.parametrize("limit,reason", [
    ("max_log_items", "log_item_limit"),
    ("max_batch_bytes", "batch_byte_limit"),
    ("max_log_bytes", "log_byte_limit"),
])
def test_capacity_failure_is_typed_and_entire_batch_has_no_cas(limit, reason, monkeypatch):
    store = BatchMemoryStore()
    service = coordinator(store, limits=BatchLimits(**{limit: 1}))
    service._head()
    before = store.get(CONTROL, HEAD)
    attempts = []
    original = store.replace

    def counted(*args):
        attempts.append(1)
        return original(*args)

    monkeypatch.setattr(store, "replace", counted)
    with pytest.raises(AdmissionConflict) as caught:
        service.accept_batch([request(), request(request_id="next", item_id="next")])
    assert attempts == []
    assert store.get(CONTROL, HEAD) == before
    assert store.bulk_calls == 0
    error = caught.value
    assert isinstance(error, batch_admission.AdmissionCapacityExceeded)
    assert not isinstance(error, (IdentityConflict, AdmissionUnknown))
    assert error.reason == reason
    assert str(error) == "batch admission capacity exceeded: " + reason


@pytest.mark.parametrize("reason", ["log_item_limit", "batch_byte_limit", "log_byte_limit"])
def test_capacity_reason_is_a_fixed_mapping_contract(reason):
    error = batch_admission.AdmissionCapacityExceeded(reason)
    assert isinstance(error, AdmissionConflict)
    assert not isinstance(error, IdentityConflict)
    assert error.reason == reason
    assert str(error) == "batch admission capacity exceeded: " + reason


@pytest.mark.parametrize("reason", ["owner", "unknown", "", "arbitrary", None])
def test_capacity_reason_rejects_unbounded_or_noncapacity_values(reason):
    with pytest.raises(ValueError):
        batch_admission.AdmissionCapacityExceeded(reason)


@pytest.mark.parametrize("new_first", [False, True])
def test_post_cas_expiry_is_unknown_without_rollback_or_second_cas(new_first, monkeypatch):
    store = BatchMemoryStore()
    service = coordinator(store)
    old = request(text="synthetic-old")
    old_receipt = service.accept_batch([old])[0]
    service.materialize_oldest()
    expiry = datetime.fromisoformat(old_receipt.expires_at.replace("Z", "+00:00"))
    now = [expiry - timedelta(milliseconds=1)]
    service.clock = lambda: now[0]
    fresh = request(request_id="200-1", item_id="200-1", text="synthetic-fresh", when=now[0])
    writes = []
    original = store.replace

    def cross_after_cas(index, key, body, seq_no, primary_term):
        writes.append(deepcopy(body))
        original(index, key, body, seq_no, primary_term)
        now[0] = expiry

    monkeypatch.setattr(store, "replace", cross_after_cas)
    values = [fresh, old] if new_first else [old, fresh]
    with pytest.raises(AdmissionUnknown):
        service.accept_batch(values)
    assert len(writes) == 1
    assert head_source(store) == writes[0]
    assert head_source(store)["last_allocated_seq"] == 2
    assert head_source(store)["last_materialized_seq"] == 1
    batches = head_source(store)["pending"]["batches"]
    assert len(batches) == len(batches[0]["entries"]) == 1
    entry = batches[0]["entries"][0]
    assert entry["request_id"] == fresh.request_id and entry["arrival_seq"] == 2
    assert not service._unknown_cas  # CAS is confirmed; only the complete output is unknown.
    recovered = service.reconcile(fresh)
    assert recovered == batch_admission._receipt(entry) and recovered.reused
    assert service.accept_batch([fresh])[0] == recovered
    with pytest.raises(AdmissionConflict, match="logical expiry"):
        service.reconcile(old)
    with pytest.raises(AdmissionConflict, match="logical expiry"):
        service.accept_batch([old])
    assert head_source(store) == writes[0] and len(writes) == 1


@pytest.mark.parametrize("cross_expiry", [False, True])
def test_mixed_expiry_collector_contract_never_returns_expired_202(cross_expiry, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from news_flash_dedup.api.contract import IngressContext, submit
    from test_batch_collector import PausedCollector, RecordingCoordinator

    store = BatchMemoryStore()
    service = coordinator(store)
    old = request(text="")
    old_receipt = service.accept_batch([old])[0]
    service.materialize_oldest()
    expiry = datetime.fromisoformat(old_receipt.expires_at.replace("Z", "+00:00"))
    now = [expiry - timedelta(milliseconds=1)]
    service.clock = lambda: now[0]
    context = IngressContext("default", now[0], "1", "v1", "v3", "route-retry")
    writes = []
    original = store.replace

    def after_cas(index, key, body, seq_no, primary_term):
        original(index, key, body, seq_no, primary_term)
        writes.append(deepcopy(body))
        if cross_expiry:
            now[0] = expiry

    monkeypatch.setattr(store, "replace", after_cas)
    recording = RecordingCoordinator(service)
    collector = PausedCollector(recording, request_timeout_seconds=5, close_timeout_seconds=1)
    results = []
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = []
            try:
                for number in (100, 200):
                    payload = {"traceId": "trace-retry", "requestId": f"{number}-1", "rewrittenId": 1,
                               "source": {"body": "", "pageDate": "2026-09-30"}, "rewrite": {"body": ""}}
                    futures.append(pool.submit(submit, payload, context, collector))
                    with collector._condition:
                        assert collector._condition.wait_for(
                            lambda: len(collector._waiting) == len(futures), timeout=1)
            finally:
                collector.start_worker.set()
            for future in futures:
                try:
                    results.append(future.result(timeout=3))
                except AdmissionUnknown as error:
                    results.append(error)
    finally:
        collector.start_worker.set()
        collector.close()
        collector._thread.join(1)
    assert not collector._thread.is_alive()
    outcomes = ["unknown" if isinstance(result, AdmissionUnknown) else result.status for result in results]
    assert outcomes == (["unknown", "unknown"] if cross_expiry else [202, 202])
    assert len(recording.calls) == len(writes) == 1  # unknown must not trigger split/retry.
    assert head_source(store) == writes[0]
    assert head_source(store)["last_allocated_seq"] == 2
    assert head_source(store)["last_materialized_seq"] == 1
    assert len(head_source(store)["pending"]["batches"][0]["entries"]) == 1
    assert collector.snapshot().failed is cross_expiry
    assert not service._unknown_cas
    fresh = request(request_id="200-1", item_id="200-1", text="")
    recovered = service.reconcile(fresh)
    assert recovered.arrival_seq == 2 and recovered.reused
    assert recovered.delivery_route_ref == "route-retry" and recovered.trace_id == "trace-retry"
    if not cross_expiry:
        assert [result.body["duplicate"] for result in results] == [True, False]
        reused = service.reconcile(old)
        assert reused == old_receipt and reused.reused


def test_peer_reconcile_cannot_prove_absence_before_original_inflight_cas_arrives(monkeypatch):
    store = BatchMemoryStore()
    writer, peer = coordinator(store), coordinator(store)
    writer._head()
    before = deepcopy(store.docs)
    pending = []
    original_replace = store.replace
    value = request(text="synthetic-delayed")

    def delayed(*args):
        pending.append(deepcopy(args))
        raise TimeoutError("synthetic CAS still in flight")

    monkeypatch.setattr(store, "replace", delayed)
    with pytest.raises(AdmissionUnknown):
        writer.accept_batch([value])
    assert len(pending) == 1 and store.docs == before
    assert writer._unknown_cas and not peer._unknown_cas
    for service in (writer, peer):
        with pytest.raises(AdmissionUnknown):
            service.reconcile(value)
    with pytest.raises(AdmissionConflict, match="unknown batch CAS"):
        writer.accept_batch([request(request_id="new", item_id="new")])
    assert store.docs == before and len(pending) == 1

    monkeypatch.setattr(store, "replace", original_replace)
    original_replace(*pending[0])  # Apply exactly the captured CAS, not a replacement request.
    entry = head_source(store)["pending"]["batches"][0]["entries"][0]
    expected = batch_admission._receipt(entry)
    retry = replace(value, trace_id="trace-retry", delivery_route_ref="route-retry", pipeline_version="v2")
    for service in (writer, peer):
        receipt = service.reconcile(retry)
        assert receipt == expected and receipt.reused and receipt.arrival_seq == 1
    assert peer.materialize_oldest() == 1
    for service in (writer, peer):
        receipt = service.reconcile(retry)
        assert receipt == expected and receipt.reused
        assert receipt.trace_id == value.trace_id and receipt.delivery_route_ref == value.delivery_route_ref
    with pytest.raises(AdmissionConflict, match="unknown batch CAS"):
        writer.accept_batch([request(request_id="new", item_id="new")])
    assert peer.accept_batch([retry])[0] == expected
    assert head_source(store)["last_allocated_seq"] == head_source(store)["last_materialized_seq"] == 1
    assert head_source(store)["pending"]["batches"] == []


@pytest.mark.parametrize("pre_cas_rejected", [False, True])
def test_reconcile_without_records_is_unknown_not_a_404_port(pre_cas_rejected):
    from typing import get_type_hints
    from news_flash_dedup.admission import AdmissionReceipt

    store = BatchMemoryStore()
    service = coordinator(store, limits=BatchLimits(max_batch_bytes=1))
    service._head()
    before = deepcopy(store.docs)
    if pre_cas_rejected:
        with pytest.raises(batch_admission.AdmissionCapacityExceeded):
            service.accept_batch([request(text="synthetic-rejected")])
    # Only the original pre-CAS call proves rejection; this recovery read cannot.
    with pytest.raises(AdmissionUnknown):
        service.reconcile(request(text="synthetic-rejected"))
    assert store.docs == before and not service._unknown_cas
    assert get_type_hints(BatchAdmissionCoordinator.reconcile)["return"] is AdmissionReceipt


def _accept_separate_batches(service, count=3):
    return [service.accept_batch([request(request_id=f"prefix-{n}", item_id=f"prefix-{n}")])[0]
            for n in range(1, count + 1)]


def test_materialize_prefix_confirms_multiple_complete_batches_with_one_shrink():
    store = BatchMemoryStore()
    service = coordinator(store)
    accepted = _accept_separate_batches(service)
    shrinks = []
    original = store.replace

    def counted(index, key, body, seq_no, primary_term):
        if key == HEAD and body["last_materialized_seq"]:
            shrinks.append(body["last_materialized_seq"])
        return original(index, key, body, seq_no, primary_term)

    store.replace = counted
    store.mget_calls = 0
    assert service.materialize_prefix() == 3
    assert shrinks == [3] and store.bulk_calls == 1 and store.mget_calls == 1
    assert head_source(store)["pending"]["batches"] == []
    assert [receipt.arrival_seq for receipt in accepted] == [1, 2, 3]
    for receipt in accepted:
        assert store.get("news-dedup-requests-v1", f"seq:{receipt.arrival_seq}") is not None


def test_materialize_prefix_is_bounded_and_preserves_suffix():
    store = BatchMemoryStore()
    service = coordinator(store)
    _accept_separate_batches(service, count=5)
    assert service.materialize_prefix() == 4
    assert head_source(store)["last_materialized_seq"] == 4
    assert [batch["entries"][0]["arrival_seq"] for batch in head_source(store)["pending"]["batches"]] == [5]
    assert service.materialize_prefix() == 5
    assert store.bulk_calls == 2


def test_materialize_prefix_permanent_item_error_tombstones_entry_and_completes_batch():
    """P0-T4：定位到的单条永久错误→条目入墓（1 坏件不杀 3 批前缀）。

    原 pin（整批隔离）已按逐条解析面新世界改写：序 2 条目 request 映射
    文档 400（item_permanent）→ 单条隔离入墓+同批已落主记录补正翻
    tombstoned；序 1/3 照常物化；一次 CAS 收缩水位跨 READY+TOMBSTONE
    推进到 3；零隔离。
    """
    store = ClassifiedBatchMemoryStore()
    service = coordinator(store)
    accepted = _accept_separate_batches(service)
    store.item_statuses = {5: 400}    # 序 2 条目的 request 映射文档
    assert service.materialize_prefix() == 3
    source = head_source(store)
    assert source["last_materialized_seq"] == 3
    assert source["pending"]["batches"] == []
    assert "batch_quarantine" not in source["checkpoint"]
    dead = accepted[1]
    scope, day, seq = dead.scope_id, dead.business_date, dead.arrival_seq
    assert tombstone_exists_confirmed(store, scope, day, seq) is True
    tombstone = load_tombstone(store, scope, day, seq)
    assert tombstone.error_class == "item_permanent"
    assert tombstone.failed_stage == "materialize_bulk"
    assert tombstone.error_code == "http_400"
    assert tombstone.arrival_seq == 2 and tombstone.item_id == dead.item_id
    assert tombstone.retryable is False and tombstone.attempt_count >= 1
    main_index = "news-dedup-items-v1-" + day.replace("-", ".")
    # 混合形态：序 2 主记录本批已落（201）→ 场景 C 补正翻 tombstoned。
    main = store.get(main_index, dead.record_id)
    assert main["source"]["task_state"] == "tombstoned"
    # 序 1/3 主记录照常物化（accepted=READY 面，未被他件死亡牵连）。
    for receipt in (accepted[0], accepted[2]):
        alive = store.get(main_index, receipt.record_id)
        assert alive["source"]["task_state"] == "accepted"
        assert tombstone_exists_confirmed(
            store, receipt.scope_id, receipt.business_date,
            receipt.arrival_seq) is False


def test_materialize_prefix_permanent_item_error_quarantines_verified_later_batch_legacy_store():
    """legacy 面（store 无逐条解析）：既有整批隔离纪律零 diff 保留。"""
    from news_flash_dedup.batch_es_store import PermanentBulkError

    store = BatchMemoryStore()
    service = coordinator(store)
    _accept_separate_batches(service)
    before = deepcopy(head_source(store)["pending"])
    store.bulk_create = lambda documents: (_ for _ in ()).throw(
        PermanentBulkError("synthetic permanent", failed_document_positions=(5,)))
    with pytest.raises(AdmissionConflict):
        service.materialize_prefix()
    source = head_source(store)
    assert source["pending"] == before and source["last_materialized_seq"] == 0
    assert source["checkpoint"]["batch_quarantine"]["first_seq"] == 2
    with pytest.raises(AdmissionConflict):
        coordinator(store).materialize_prefix()


def test_materialize_restart_after_tombstone_validates_log_and_continues():
    """P0-T5 ⑥（物化面）：墓碑收缩后重启——takeover 全量校验 pending 日志
    （_validate_log 连续性：墓碑条目已出 pending，序号边界=水位），跨墓位
    继续分配物化；墓证幂等零重放。"""
    store = ClassifiedBatchMemoryStore()
    service = coordinator(store)
    accepted = _accept_separate_batches(service)
    store.item_statuses = {5: 400}    # 序 2 条目 request 映射文档（item_permanent）
    assert service.materialize_prefix() == 3
    # 重启=全新协调器实例（同 owner）：takeover → _validate_log 执法连续性。
    resumed = coordinator(store)
    assert resumed.takeover() == 3
    assert head_source(store)["pending"]["batches"] == []
    # 跨墓位继续：序 4 新受理物化（全局序号域跨墓推进，无 hole 停摆）。
    receipt = resumed.accept_batch([
        request(request_id="post-4", item_id="post-4", text="重启后续件。")])[0]
    assert receipt.arrival_seq == 4
    assert resumed.materialize_prefix() == 4
    assert head_source(store)["last_materialized_seq"] == 4
    # 墓证幂等：序 2 墓碑仍在且唯一（重放零二次物化/零新墓）。
    dead = accepted[1]
    assert tombstone_exists_confirmed(
        store, dead.scope_id, dead.business_date, dead.arrival_seq) is True
    main_index = "news-dedup-items-v1-" + dead.business_date.replace("-", ".")
    assert store.get(main_index, dead.record_id)["source"][
        "task_state"] == "tombstoned"


def test_materialize_prefix_unlocated_permanent_error_quarantines_first_batch():
    """P0-T4：请求级 PermanentBulkError（不可定位）→ 既有 unlocated 隔离。"""
    from news_flash_dedup.batch_es_store import PermanentBulkError

    store = ClassifiedBatchMemoryStore()
    service = coordinator(store)
    _accept_separate_batches(service)
    store.request_error = PermanentBulkError("request-level")
    with pytest.raises(AdmissionConflict):
        service.materialize_prefix()
    source = head_source(store)
    assert source["last_materialized_seq"] == 0
    assert len(source["pending"]["batches"]) == 3
    assert source["checkpoint"]["batch_quarantine"]["first_seq"] == 1
    assert source["checkpoint"]["batch_quarantine"]["reason"] == "unattributed_permanent_bulk_error"
    with pytest.raises(AdmissionConflict, match="quarantined"):
        coordinator(store).recover()
    assert head_source(store)["last_materialized_seq"] == 0


def test_materialize_prefix_unlocated_permanent_error_quarantines_first_batch_legacy_store():
    from news_flash_dedup.batch_es_store import PermanentBulkError

    store = BatchMemoryStore()
    service = coordinator(store)
    _accept_separate_batches(service)
    store.bulk_create = lambda documents: (_ for _ in ()).throw(PermanentBulkError("request-level"))
    with pytest.raises(AdmissionConflict):
        service.materialize_prefix()
    source = head_source(store)
    assert source["last_materialized_seq"] == 0
    assert len(source["pending"]["batches"]) == 3
    assert source["checkpoint"]["batch_quarantine"]["first_seq"] == 1
    assert source["checkpoint"]["batch_quarantine"]["reason"] == "unattributed_permanent_bulk_error"
    with pytest.raises(AdmissionConflict, match="quarantined"):
        coordinator(store).recover()
    assert head_source(store)["last_materialized_seq"] == 0


def test_materialize_prefix_tombstone_replay_after_shrink_failure_is_idempotent():
    """P0-T4：墓碑持久化后收缩失败（崩溃恢复形态）→ 重放幂等不重复死亡件。"""
    store = ClassifiedBatchMemoryStore()
    service = coordinator(store)
    accepted = _accept_separate_batches(service)
    store.item_statuses = {5: 400}
    original_replace = store.replace

    def failing_replace(index, key, body, seq_no, primary_term):
        if key == "batch_admission_head" and body.get("last_materialized_seq"):
            raise OSError("head CAS transport lost")
        return original_replace(index, key, body, seq_no, primary_term)

    store.replace = failing_replace
    with pytest.raises(AdmissionUnknown):
        service.materialize_prefix()
    store.replace = original_replace
    # 墓碑已确认在库（持久化+读回确认先于收缩）。
    dead = accepted[1]
    assert tombstone_exists_confirmed(
        store, dead.scope_id, dead.business_date, dead.arrival_seq) is True
    # 重放：已落文档 409→读回对拍自愈；坏件重撞 400→墓碑幂等重放。
    assert service.materialize_prefix() == 3
    index = tombstone_index(dead.business_date)
    tombstones = [key for (doc_index, key) in store.docs if doc_index == index]
    assert len(tombstones) == 1
    source = head_source(store)
    assert source["pending"]["batches"] == []
    assert source["last_materialized_seq"] == 3


def test_materialize_prefix_transient_item_failure_is_retryable_no_tombstone():
    """P0-T4：单条瞬态失败=可重试暂态——零墓碑、零隔离、批完整保留。"""
    store = ClassifiedBatchMemoryStore()
    service = coordinator(store)
    accepted = _accept_separate_batches(service)
    store.item_statuses = {4: 503}    # 序 2 条目主记录文档
    with pytest.raises(AdmissionUnknown):
        service.materialize_prefix()
    source = head_source(store)
    assert source["last_materialized_seq"] == 0
    assert len(source["pending"]["batches"]) == 3
    assert "batch_quarantine" not in source["checkpoint"]
    for receipt in accepted:
        assert tombstone_exists_confirmed(
            store, receipt.scope_id, receipt.business_date,
            receipt.arrival_seq) is False
    # 瞬态消除后重试：全部完成（409→读回对拍自愈）。
    store.item_statuses = {}
    assert service.materialize_prefix() == 3
    assert head_source(store)["pending"]["batches"] == []


def test_materialize_prefix_unknown_readback_missing_is_retryable():
    """P0-T4：409 但读回未命中=未确认（重试收敛，不定永久不隔离）。"""
    store = ClassifiedBatchMemoryStore()
    service = coordinator(store)
    accepted = _accept_separate_batches(service)
    original_replace = store.replace

    def failing_replace(index, key, body, seq_no, primary_term):
        if key == "batch_admission_head" and body.get("last_materialized_seq"):
            raise OSError("head CAS transport lost")
        return original_replace(index, key, body, seq_no, primary_term)

    store.replace = failing_replace
    with pytest.raises(AdmissionUnknown):
        service.materialize_prefix()          # 全部落库，收缩传输失败
    store.replace = original_replace
    main_index = "news-dedup-items-v1-" + accepted[0].business_date.replace("-", ".")
    store.mget_missing = {(main_index, accepted[0].record_id)}
    with pytest.raises(AdmissionUnknown):
        service.materialize_prefix()          # 409→读回未命中=未确认
    source = head_source(store)
    assert source["last_materialized_seq"] == 0
    assert len(source["pending"]["batches"]) == 3
    assert "batch_quarantine" not in source["checkpoint"]
    store.mget_missing = set()
    assert service.materialize_prefix() == 3
    assert head_source(store)["pending"]["batches"] == []


def test_materialize_prefix_unknown_readback_divergence_quarantines():
    """P0-T4：读回身份分歧=既有整批隔离（deterministic_content_conflict）。"""
    store = ClassifiedBatchMemoryStore()
    service = coordinator(store)
    _accept_separate_batches(service)
    _, request_key, _ = batch_admission._keys("default", "prefix-2", "prefix-2")
    store.docs[("news-dedup-requests-v1", request_key)] = {
        "source": {"kind": "request", "scope_id": "default",
                   "request_id": "prefix-2", "item_id": "alien-content",
                   "record_id": "b" * 64, "business_fingerprint": "c" * 64,
                   "business_date": "2026-09-24", "arrival_seq": 2,
                   "expires_at": "2026-10-01T00:00:00.000000Z",
                   "target_index": "x"},
        "seq_no": 0, "primary_term": 1,
    }
    with pytest.raises(AdmissionConflict):
        service.materialize_prefix()
    source = head_source(store)
    assert source["last_materialized_seq"] == 0
    assert len(source["pending"]["batches"]) == 3
    assert source["checkpoint"]["batch_quarantine"]["first_seq"] == 2
    assert source["checkpoint"]["batch_quarantine"]["reason"] == \
        "deterministic_content_conflict"


def test_materialize_prefix_item_failures_record_entry_retry_state():
    """P0-T4：单条失败入条目重试账（attempt_count/first_failed_at 入墓）。"""
    runtime = MaterializeRetryRuntime(clock=lambda: 1000.0,
                                      random_source=lambda: 0.5)
    store = ClassifiedBatchMemoryStore()
    service = BatchAdmissionCoordinator(
        store, owner_id="writer-1", owner_isolated=lambda: True,
        limits=BatchLimits(), clock=lambda: NOW,
        retry_runtime=runtime)
    accepted = _accept_separate_batches(service)
    store.item_statuses = {5: 503}    # 序 2 条目瞬态失败一次
    with pytest.raises(AdmissionUnknown):
        service.materialize_prefix()
    state = runtime.entry_state(2)
    assert state is not None and state.active_attempts == 1
    assert state.first_failed_at == 1000.0
    store.item_statuses = {5: 400}    # 同条目转永久
    assert service.materialize_prefix() == 3
    dead = accepted[1]
    tombstone = load_tombstone(
        store, dead.scope_id, dead.business_date, dead.arrival_seq)
    assert tombstone.attempt_count == 2              # 首次瞬态+本次永久
    assert tombstone.first_failed_at < tombstone.last_failed_at
    assert runtime.entry_state(2) is None            # 条目已出账（终态）


def test_materialize_oldest_classified_permanent_item_tombstones_and_shrinks():
    """P0-T4：materialize_oldest 逐条解析面——单条主记录 4xx 入墓收缩。"""
    store = ClassifiedBatchMemoryStore()
    service = coordinator(store)
    accepted = _accept_separate_batches(service, count=2)
    store.item_statuses = {0: 400}    # 序 1 条目主记录文档
    assert service.materialize_oldest() == 1
    source = head_source(store)
    assert source["last_materialized_seq"] == 1
    assert [batch["entries"][0]["arrival_seq"]
            for batch in source["pending"]["batches"]] == [2]
    dead = accepted[0]
    assert tombstone_exists_confirmed(
        store, dead.scope_id, dead.business_date, dead.arrival_seq) is True
    main_index = "news-dedup-items-v1-" + dead.business_date.replace("-", ".")
    # 主记录 4xx 未落（absent 补正）→ 无主记录残留、无重复死亡件。
    assert store.get(main_index, dead.record_id) is None
    store.item_statuses = {}          # 注入只针对首批（序 1 主记录文档）
    assert service.materialize_oldest() == 2
    assert head_source(store)["pending"]["batches"] == []
    assert head_source(store)["last_materialized_seq"] == 2
    assert sum(1 for (index, key) in store.docs
               if index == tombstone_index(dead.business_date)) == 1


def test_materialize_prefix_partial_bulk_unknown_keeps_all_pending_and_recovers():
    store = BatchMemoryStore()
    service = coordinator(store)
    _accept_separate_batches(service)
    store.fail_bulk_after = 5
    with pytest.raises(AdmissionUnknown):
        service.materialize_prefix()
    assert head_source(store)["last_materialized_seq"] == 0
    assert len(head_source(store)["pending"]["batches"]) == 3
    assert coordinator(store).recover() == 3
    assert head_source(store)["pending"]["batches"] == []


def test_materialize_prefix_append_during_shrink_rechecks_and_preserves_suffix():
    store = BatchMemoryStore()
    service = coordinator(store)
    appender = coordinator(store)
    _accept_separate_batches(service, count=2)
    original = store.replace
    injected = False

    def competing(index, key, body, seq_no, primary_term):
        nonlocal injected
        if key == HEAD and body["last_materialized_seq"] == 2 and not injected:
            injected = True
            appender.accept_batch([request(request_id="prefix-3", item_id="prefix-3")])
        return original(index, key, body, seq_no, primary_term)

    store.replace = competing
    assert service.materialize_prefix() == 2
    assert injected and head_source(store)["last_allocated_seq"] == 3
    assert head_source(store)["last_materialized_seq"] == 2
    assert [batch["entries"][0]["arrival_seq"] for batch in head_source(store)["pending"]["batches"]] == [3]


def test_materialize_prefix_expiry_after_bulk_keeps_full_log_and_quarantines():
    store = BatchMemoryStore()
    service = coordinator(store)
    _accept_separate_batches(service, count=2)
    before = deepcopy(head_source(store)["pending"])
    now = [NOW]
    service.clock = lambda: now[0]
    original = store.bulk_create

    def crossing(documents):
        original(documents)
        now[0] = EXPIRY

    store.bulk_create = crossing
    with pytest.raises(AdmissionConflict):
        service.materialize_prefix()
    source = head_source(store)
    assert source["pending"] == before and source["last_materialized_seq"] == 0
    assert source["checkpoint"]["batch_quarantine"]["reason"] == "logical_expiry"
    assert store.bulk_calls == 1


def test_materialize_prefix_preserves_interleaved_identity_date_order_and_four_documents():
    store = BatchMemoryStore()
    service = coordinator(store)
    first = request(request_id="prefix-first", item_id="prefix-first")
    second = replace(request(request_id="prefix-other", item_id="prefix-other"), scope_id="other")
    service.accept_batch([first])
    service.accept_batch([second])
    next_day = coordinator(store, when=datetime(2026, 9, 24, 16, 1, tzinfo=timezone.utc))
    third = request(request_id="prefix-next", item_id="prefix-next")
    next_day.accept_batch([third])
    assert next_day.materialize_prefix() == 3
    for item, seq, scope, day in (
        (first, 1, "default", "2026-09-24"),
        (second, 2, "other", "2026-09-24"),
        (third, 3, "default", "2026-09-25"),
    ):
        receipt = next_day.reconcile(item)
        assert (receipt.arrival_seq, receipt.scope_id, receipt.business_date) == (seq, scope, day)
        assert store.get("news-dedup-requests-v1", f"seq:{seq}")["source"]["record_id"] == receipt.record_id
        assert store.get("news-dedup-requests-v1", "request:" + batch_admission._digest([scope, item.request_id]))
        assert store.get("news-dedup-requests-v1", "item:" + receipt.record_id)
        assert store.get("news-dedup-items-v1-" + day.replace("-", "."), receipt.record_id)


def test_materialize_prefix_replay_keeps_advanced_main_state():
    store = BatchMemoryStore()
    service = coordinator(store)
    receipts = _accept_separate_batches(service, count=2)
    store.fail_bulk_after = -1
    with pytest.raises(AdmissionUnknown):
        service.materialize_prefix()
    first_main = store.docs[("news-dedup-items-v1-2026.09.24", receipts[0].record_id)]["source"]
    first_main["task_state"] = "succeeded"
    first_main["delivery_state"] = "pending"
    first_main["result"] = {"synthetic": "frozen"}
    assert service.materialize_prefix() == 2
    assert first_main == store.docs[("news-dedup-items-v1-2026.09.24", receipts[0].record_id)]["source"]


def test_materialize_prefix_shrink_ack_loss_is_reconciled_without_reallocation():
    store = BatchMemoryStore()
    service = coordinator(store)
    _accept_separate_batches(service, count=2)

    def lost_ack(index, key):
        if key == HEAD and head_source(store)["last_materialized_seq"] == 2:
            store.after_write = None
            raise OSError("synthetic shrink ack loss")

    store.after_write = lost_ack
    with pytest.raises(AdmissionUnknown):
        service.materialize_prefix()
    assert head_source(store)["last_materialized_seq"] == 2
    assert head_source(store)["pending"]["batches"] == []
    calls = store.bulk_calls
    assert coordinator(store).recover() == 2
    assert store.bulk_calls == calls
    assert service.accept_batch([request(request_id="prefix-1", item_id="prefix-1")])[0].reused


def test_materialize_prefix_later_batch_content_conflict_quarantines_without_shrink():
    store = BatchMemoryStore()
    service = coordinator(store)
    _accept_separate_batches(service, count=2)
    store.fail_bulk_after = -1
    with pytest.raises(AdmissionUnknown):
        service.materialize_prefix()
    second = head_source(store)["pending"]["batches"][1]["entries"][0]
    store.docs[("news-dedup-items-v1-2026.09.24", second["record_id"])]["source"]["request_id"] = "wrong"
    with pytest.raises(AdmissionConflict):
        service.materialize_prefix()
    source = head_source(store)
    assert source["last_materialized_seq"] == 0 and len(source["pending"]["batches"]) == 2
    assert source["checkpoint"]["batch_quarantine"]["first_seq"] == 2


# ---------- P0-T6 参数分层（域层默认值+行为钉，主窗令） ----------

def test_t6_domain_default_limits_and_prefix_constants():
    """P0-T6：域层默认梯次——log 硬 512（原 64）/批 8/prefix 32+4 批；
    collector 面默认钉在 test_batch_collector.py（wait 0.02/队列硬 512/
    告警层 400）。"""
    from news_flash_dedup.batch_admission import (
        MATERIALIZE_PREFIX_BATCHES, MATERIALIZE_PREFIX_ITEMS,
    )

    limits = BatchLimits()
    assert limits.max_batch_items == 8
    assert limits.max_log_items == 512
    assert (MATERIALIZE_PREFIX_BATCHES, MATERIALIZE_PREFIX_ITEMS) == (4, 32)


def test_t6_durable_log_hard_capacity_boundary_at_512():
    """P0-T6 硬层行为钉：512 件满日志边界——恰 512 受理（64 批×8），
    第 513 件 AdmissionCapacityExceeded("log_item_limit")（CAS 零改写）。"""
    store = BatchMemoryStore()
    service = coordinator(store)
    for batch in range(64):
        service.accept_batch([
            request(request_id=f"t6-{batch}-{n}", item_id=f"t6-{batch}-{n}")
            for n in range(8)])
    assert head_source(store)["last_allocated_seq"] == 512
    before = deepcopy(head_source(store))
    with pytest.raises(batch_admission.AdmissionCapacityExceeded) as caught:
        service.accept_batch([request(request_id="t6-overflow", item_id="t6-overflow")])
    assert caught.value.reason == "log_item_limit"
    assert head_source(store) == before          # 拒收零改写（不否认已受理）


def test_t6_materialize_prefix_bounded_at_32_items():
    """P0-T6 prefix 行为钉：9 批×8 条（72 件）→ item_limit=32（4 批）一次
    CAS 收缩；后缀 5 批原序保留，续推收敛。"""
    store = BatchMemoryStore()
    service = coordinator(store)
    for batch in range(9):
        service.accept_batch([
            request(request_id=f"t6p-{batch}-{n}", item_id=f"t6p-{batch}-{n}")
            for n in range(8)])
    assert service.materialize_prefix() == 32
    source = head_source(store)
    assert source["last_materialized_seq"] == 32
    # 后缀 5 批原序保留（每批 8 条，首条序号 33/41/49/57/65）。
    remaining = [batch["entries"][0]["arrival_seq"]
                 for batch in source["pending"]["batches"]]
    assert remaining == [33, 41, 49, 57, 65]
    assert all(len(batch["entries"]) == 8
               for batch in source["pending"]["batches"])
    assert service.materialize_prefix() == 64
    assert service.materialize_prefix() == 72


def test_t6_batch_item_limit_8():
    """P0-T6 批容量行为钉：单批 >8 条拒绝（AdmissionConflict，零分配）。"""
    store = BatchMemoryStore()
    service = coordinator(store)
    service.accept_batch([request(request_id="t6b-0", item_id="t6b-0")])
    with pytest.raises(AdmissionConflict):
        service.accept_batch([
            request(request_id=f"t6b-{n}", item_id=f"t6b-{n}")
            for n in range(1, 10)])
    assert head_source(store)["last_allocated_seq"] == 1   # 拒收零分配
