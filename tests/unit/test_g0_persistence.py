from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import json

import pytest

from news_flash_dedup.admission import (
    AdmissionConflict, AdmissionCoordinator, AdmissionUnknown, CONTROL_INDEX,
    REQUEST_INDEX, _digest, _utc,
)
from news_flash_dedup.g0_persistence import FixedArtifactProbe
from test_admission import MemoryStore, coordinator, request


NOW = datetime(2026, 9, 23, 15, 59, tzinfo=timezone.utc)


class Clock:
    def __init__(self, now=NOW):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


def probe(store, clock=None, owner="writer-1"):
    return FixedArtifactProbe(store, owner_id=owner, clock=clock or Clock())


def index(receipt):
    return "news-dedup-items-v1-" + receipt.business_date.replace("-", ".")


def main(store, receipt):
    return store.get(index(receipt), receipt.record_id)["source"]


def day_key(receipt):
    return f"day:{receipt.scope_id}:{receipt.business_date}"


def audit_index(receipt):
    return "news-dedup-audits-v1-" + receipt.business_date.replace("-", ".")


def mutate(store, idx, key, change):
    doc = store.get(idx, key)
    body = deepcopy(doc["source"])
    change(body)
    store.replace(idx, key, body, doc["seq_no"], doc["primary_term"])


def ready(worker, receipt):
    snapshot = worker.capture_visibility(receipt)
    return worker.prepare_visibility(receipt, snapshot=snapshot, refresh_receipt={
        "index": index(receipt), "_shards": {"total": 2, "successful": 2, "failed": 0},
    })


def freeze(worker, receipt):
    return worker.freeze(receipt, preparation=ready(worker, receipt))


def fixture():
    store, clock = MemoryStore(), Clock()
    receipt = coordinator(store).accept(request())
    worker = probe(store, clock)
    freeze(worker, receipt)
    return store, clock, receipt, worker


def frozen_identity(source):
    return {key: source[key] for key in (
        "result", "callback_body", "result_version", "event_id", "completed_at",
        "delivery_deadline_at", "audit_ids", "audit_complete",
    )}


def test_late_sequence_cannot_freeze_before_earlier_terminal():
    store = MemoryStore()
    first = coordinator(store).accept(request())
    second = coordinator(store).accept(request(request_id="200-1", item_id="200-1"))
    third = coordinator(store).accept(request(request_id="300-1", item_id="300-1"))
    worker = probe(store)
    # Also exposes the old implementation's premature terminal write without new APIs.
    with pytest.raises(AdmissionConflict):
        worker.freeze(third)
    assert main(store, third)["task_state"] == "accepted"
    prepared = ready(worker, third)
    with pytest.raises(AdmissionConflict):
        worker.freeze(third, preparation=prepared)
    assert main(store, third)["task_state"] == "running"
    freeze(worker, first)
    with pytest.raises(AdmissionConflict):
        freeze(worker, third)
    freeze(worker, second)
    # Refresh another task changed the day snapshot: re-establish this preparation.
    freeze(worker, third)
    assert worker.advance_watermark(third) == 3


def test_fixed_terminal_audit_and_p06_callback_share_one_main_cas():
    store = MemoryStore()
    receipt = coordinator(store).accept(request())
    worker = probe(store)
    prepared = ready(worker, receipt)
    writes = []
    store.after_write = lambda idx, key: writes.append((idx, key))
    result = worker.freeze(receipt, preparation=prepared)
    source = main(store, receipt)
    assert writes.count((index(receipt), receipt.record_id)) == 1
    assert writes[0][0] == audit_index(receipt)
    assert source["audit_complete"] is True
    assert source["delivery_state"] == "pending"
    assert set(result) == {"decision", "duplicate_ids", "reason"}
    callback = json.loads(source["callback_body"])
    assert set(callback) == {"requestId", "traceId", "success", "auditDecision", "reason", "completedTime", "resultJson"}
    assert callback["resultJson"] == {"item_id": receipt.item_id, "text": request().text, **result}
    assert callback["traceId"] == "trace-first"
    assert source["diagnostics"]["delivery"]["route_ref"] == receipt.delivery_route_ref
    assert source["diagnostics"]["delivery"]["payload_hash"] == hashlib.sha256(source["callback_body"].encode()).hexdigest()
    audit = store.get(audit_index(receipt), source["audit_ids"][0])["source"]
    assert set(audit) == {"audit_id", "audit_type", "scope_id", "record_id", "candidate_record_id", "request_id", "business_date", "pipeline_version", "payload_hash", "arrival_seq", "created_at", "expires_at", "payload"}
    assert audit["audit_type"] == "decision" and audit["candidate_record_id"] is None
    assert audit["payload"]["fixed_fixture"] is True
    assert audit["payload"]["algorithm_executed"] is False
    assert audit["payload"]["comparison_count"] == 0
    assert audit["payload"]["unresolved"]
    assert source["task_lease"]["preparation"]["generation"] == prepared["generation"] + 1
    assert source["task_lease"]["preparation"]["owner_id"] is None
    before = store.get(index(receipt), receipt.record_id)
    assert probe(store).freeze(receipt) == result
    assert store.get(index(receipt), receipt.record_id) == before


def test_other_scope_and_cross_day_skip_only_legally_registered_entries():
    store = MemoryStore()
    first = coordinator(store).accept(request())
    other = coordinator(store).accept(replace(request(request_id="200-1", item_id="200-1"), scope_id="other"))
    next_day = AdmissionCoordinator(store, owner_id="writer-1", owner_isolated=lambda: True,
                                    clock=lambda: NOW + timedelta(minutes=2))
    crossed = next_day.accept(request(request_id="300-1", item_id="300-1"))
    last = coordinator(store).accept(request(request_id="400-1", item_id="400-1"))
    worker = probe(store)
    freeze(worker, first)
    freeze(worker, last)
    assert worker.advance_watermark(last) == 4
    assert main(store, other)["task_state"] == main(store, crossed)["task_state"] == "accepted"


@pytest.mark.parametrize("damage", ["hole", "wrong_seq", "wrong_scope", "wrong_target", "missing_mapping", "missing_text"])
def test_registration_holes_and_corruption_block_visibility_and_freeze(damage):
    store = MemoryStore()
    first = coordinator(store).accept(replace(request(), scope_id="other"))
    last = coordinator(store).accept(request(request_id="200-1", item_id="200-1"))
    if damage == "hole":
        del store.docs[REQUEST_INDEX, "seq:1"]
    elif damage == "missing_mapping":
        del store.docs[REQUEST_INDEX, "item:" + first.record_id]
    elif damage == "missing_text":
        mutate(store, index(first), first.record_id, lambda body: body.pop("text"))
    else:
        key, value = {"wrong_seq": ("arrival_seq", 8), "wrong_scope": ("scope_id", "fake"),
                      "wrong_target": ("target_index", "arbitrary-index")}[damage]
        mutate(store, REQUEST_INDEX, "seq:1", lambda body: body.update({key: value}))
    with pytest.raises(AdmissionConflict):
        ready(probe(store), last)
    assert main(store, last)["task_state"] == "accepted"


@pytest.mark.parametrize("shards", [
    {"total": 2, "successful": 1, "failed": 1},
    {"total": 2, "successful": 1, "failed": 0},
    {"total": 0, "successful": 0, "failed": 0},
    {"total": True, "successful": True, "failed": 0},
])
def test_refresh_partial_or_invalid_shards_never_advance(shards):
    store = MemoryStore()
    receipt = coordinator(store).accept(request())
    worker = probe(store)
    snapshot = worker.capture_visibility(receipt)
    with pytest.raises(AdmissionConflict):
        worker.prepare_visibility(receipt, snapshot=snapshot, refresh_receipt={"index": index(receipt), "_shards": shards})
    day = store.get(CONTROL_INDEX, day_key(receipt))["source"]
    assert day["lexical_watermark"] == 0
    assert main(store, receipt)["task_state"] == "accepted"


def test_refresh_rejects_wildcard_and_snapshot_from_another_process():
    store = MemoryStore()
    receipt = coordinator(store).accept(request())
    worker = probe(store)
    snapshot = worker.capture_visibility(receipt)
    response = {"index": "*", "_shards": {"total": 1, "successful": 1, "failed": 0}}
    with pytest.raises(AdmissionConflict):
        worker.prepare_visibility(receipt, snapshot=snapshot, refresh_receipt=response)
    response["index"] = index(receipt)
    with pytest.raises(AdmissionConflict):
        probe(store).prepare_visibility(receipt, snapshot=snapshot, refresh_receipt=response)
    prepared = ready(worker, receipt)
    with pytest.raises(AdmissionConflict):
        probe(store).freeze(receipt, preparation=prepared)
    freeze(probe(store), receipt)


@pytest.mark.parametrize("field", ["request_id", "item_id", "record_id", "business_date", "arrival_seq", "accepted_at", "expires_at", "schema_version", "pipeline_version", "embedding_space_id", "delivery_route_ref", "trace_id"])
def test_receipt_complete_identity_is_checked(field):
    store = MemoryStore()
    receipt = coordinator(store).accept(request())
    bad = replace(receipt, **{field: 99 if field == "arrival_seq" else "changed"})
    with pytest.raises(AdmissionConflict):
        ready(probe(store), bad)
    assert main(store, receipt)["task_state"] == "accepted"


@pytest.mark.parametrize("damage", ["owner", "generation", "facts", "main_version", "expired"])
def test_prepared_snapshot_cannot_survive_owner_generation_or_artifact_change(damage):
    store, clock = MemoryStore(), Clock()
    receipt = coordinator(store).accept(request())
    worker = probe(store, clock)
    prepared = ready(worker, receipt)
    if damage == "owner":
        mutate(store, CONTROL_INDEX, day_key(receipt), lambda body: body.update(owner_id="writer-2"))
    elif damage == "generation":
        mutate(store, index(receipt), receipt.record_id,
               lambda body: body["task_lease"]["preparation"].update(generation=77))
    elif damage == "facts":
        mutate(store, index(receipt), receipt.record_id, lambda body: body.update(facts=["late"]))
    elif damage == "main_version":
        mutate(store, index(receipt), receipt.record_id, lambda body: body.update(vector_state="ready"))
    else:
        clock.now = datetime.fromisoformat(receipt.expires_at.replace("Z", "+00:00"))
    with pytest.raises(AdmissionConflict):
        worker.freeze(receipt, preparation=prepared)
    assert main(store, receipt)["task_state"] == "running"


def test_explicit_day_takeover_invalidates_old_preparation_without_claiming_isolation():
    store = MemoryStore()
    receipt = coordinator(store).accept(request())
    old = probe(store)
    prepared = ready(old, receipt)
    new = probe(store, owner="writer-2")
    with pytest.raises(AdmissionConflict):
        ready(new, receipt)
    day = store.get(CONTROL_INDEX, day_key(receipt))
    args = {"previous_owner_id": "writer-1", "expected_seq_no": day["seq_no"],
            "expected_primary_term": day["primary_term"], "isolation_evidence_ref": ""}
    with pytest.raises(AdmissionConflict):
        new.takeover_day(receipt, **args)
    args["isolation_evidence_ref"] = "local-fixture-observation-only"
    new.takeover_day(receipt, **args)
    with pytest.raises(AdmissionConflict):
        old.freeze(receipt, preparation=prepared)
    freeze(new, receipt)
    with pytest.raises(AdmissionConflict):
        old.advance_watermark(receipt)
    assert new.advance_watermark(receipt) == 1


@pytest.mark.parametrize("fault", ["create_before", "create_ack_lost", "read_unknown", "different_hash", "extra_payload", "extra_top"])
def test_audit_failure_or_schema_conflict_blocks_terminal(fault):
    store = MemoryStore()
    receipt = coordinator(store).accept(request())
    worker = probe(store)
    prepared = ready(worker, receipt)
    original = store.create

    def create(idx, key, body):
        if idx == audit_index(receipt):
            if fault == "create_before":
                raise OSError("no write")
            altered = deepcopy(body)
            if fault == "different_hash":
                altered["payload_hash"] = "0" * 64
            if fault == "extra_payload":
                altered["payload"]["arbitrary"] = True
            if fault == "extra_top":
                altered["arbitrary"] = True
            original(idx, key, altered)
            if fault == "create_ack_lost":
                raise OSError("audit acknowledgement lost")
        else:
            original(idx, key, body)

    store.create = create
    if fault == "read_unknown":
        def unknown(idx, key, value):
            if idx == audit_index(receipt):
                raise OSError("audit read unknown")
        store.after_get = unknown
    with pytest.raises((AdmissionUnknown, AdmissionConflict)):
        worker.freeze(receipt, preparation=prepared)
    assert main(store, receipt)["task_state"] == "running"
    store.after_get, store.create = None, original
    if fault in {"create_before", "create_ack_lost", "read_unknown"}:
        worker.freeze(receipt, preparation=prepared)
        assert main(store, receipt)["task_state"] == "succeeded"


# Counts all Store calls, including exception classification; it can prohibit IO
# entirely or move the injected clock across expiry at any completed IO boundary.
class CountingStore(MemoryStore):
    def __init__(self):
        super().__init__()
        self.calls = []
        self.deny_calls = False
        self.expire_after = None
        self.cross_expiry = None
        self.fail_crossing = False

    def _call(self, method, *args):
        self.calls.append(method)
        if self.deny_calls:
            raise AssertionError("expired business operation must not call Store")
        result = getattr(super(), method)(*args)
        if len(self.calls) == self.expire_after:
            self.cross_expiry()
            if self.fail_crossing:
                raise OSError("IO completed but acknowledgement unavailable")
        return result

    def get(self, *args):
        return self._call("get", *args)

    def create(self, *args):
        return self._call("create", *args)

    def replace(self, *args):
        return self._call("replace", *args)

    def is_conflict(self, *args):
        return self._call("is_conflict", *args)


_OPERATIONS = ["capture", "prepare", "freeze", "reuse_freeze", "takeover",
               "advance", "claim", "recover", "ack"]


def expiry_case(operation):
    store, clock = CountingStore(), Clock()
    receipt = coordinator(store).accept(request())
    expiry = datetime.fromisoformat(receipt.expires_at.replace("Z", "+00:00"))
    clock.now = expiry - timedelta(seconds=10)
    worker = probe(store, clock)
    snapshot = worker.capture_visibility(receipt)
    refresh = {"index": index(receipt), "_shards": {"total": 1, "successful": 1, "failed": 0}}
    if operation == "prepare":
        invoke = lambda: worker.prepare_visibility(receipt, snapshot=snapshot, refresh_receipt=refresh)
    elif operation == "capture":
        # Exercise day-control creation as well as the existing-document IO paths.
        del store.docs[CONTROL_INDEX, day_key(receipt)]
        invoke = lambda: worker.capture_visibility(receipt)
    else:
        prepared = worker.prepare_visibility(receipt, snapshot=snapshot, refresh_receipt=refresh)
        if operation == "freeze":
            invoke = lambda: worker.freeze(receipt, preparation=prepared)
        elif operation == "takeover":
            day = store.get(CONTROL_INDEX, day_key(receipt))
            worker = probe(store, clock, owner="writer-2")
            invoke = lambda: worker.takeover_day(
                receipt, previous_owner_id="writer-1", expected_seq_no=day["seq_no"],
                expected_primary_term=day["primary_term"],
                isolation_evidence_ref="local-fixture-observation-only")
        else:
            worker.freeze(receipt, preparation=prepared)
            if operation == "reuse_freeze":
                invoke = lambda: worker.freeze(receipt)
            elif operation == "advance":
                invoke = lambda: worker.advance_watermark(receipt)
            elif operation == "claim":
                invoke = lambda: worker.claim_delivery(receipt, lease_seconds=5)
            elif operation == "recover":
                worker.claim_delivery(receipt, lease_seconds=1)
                clock.advance(1)
                invoke = lambda: worker.recover_delivery(receipt)
            else:
                lease = worker.claim_delivery(receipt, lease_seconds=5)
                invoke = lambda: worker.ack_delivery(
                    receipt, lease=lease, http_status=200, response_body=b'{"succeed":true}')
    store.calls.clear()
    store.cross_expiry = lambda: setattr(clock, "now", expiry)
    return store, clock, receipt, worker, invoke


@pytest.mark.parametrize("operation", _OPERATIONS)
@pytest.mark.parametrize("late_seconds", [0, 1200])
def test_data_expiry_rejects_every_business_entry_without_any_store_call(operation, late_seconds):
    store, clock, _, _, invoke = expiry_case(operation)
    before = deepcopy(store.docs)
    store.cross_expiry()
    clock.advance(late_seconds)
    store.deny_calls = True
    with pytest.raises(AdmissionConflict, match="expir"):
        invoke()
    assert store.calls == []
    assert store.docs == before


@pytest.mark.parametrize("operation", _OPERATIONS)
@pytest.mark.parametrize("fail_crossing", [False, True])
def test_every_store_io_crossing_expiry_discards_output_and_stops_further_io(operation, fail_crossing):
    baseline, _, _, _, run_baseline = expiry_case(operation)
    run_baseline()
    calls = list(baseline.calls)
    assert calls
    # Includes registration/main/audit reads, day/audit creates, CAS and readback.
    # A write already submitted before expiry cannot be undone by this local probe.
    for boundary in range(1, len(calls) + 1):
        store, _, _, _, invoke = expiry_case(operation)
        store.expire_after = boundary
        store.fail_crossing = fail_crossing
        with pytest.raises(AdmissionConflict, match="expir"):
            invoke()
        assert store.calls == calls[:boundary], (operation, boundary)


@pytest.mark.parametrize("operation", ["freeze", "reuse_freeze", "claim"])
def test_callback_build_crossing_data_expiry_cannot_write_or_return_authorization(operation, monkeypatch):
    store, _, _, worker, invoke = expiry_case(operation)
    original = worker._callback

    def crossed(*args):
        callback = original(*args)
        store.cross_expiry()
        store.calls.clear()
        return callback

    monkeypatch.setattr(worker, "_callback", crossed)
    with pytest.raises(AdmissionConflict, match="expir"):
        invoke()
    assert store.calls == []


def test_fixed_reason_uses_the_fifteen_code_dictionary_without_claiming_algorithm_execution():
    store, _, receipt, _ = fixture()
    allowed = {
        "EXACT_TEXT_MATCH", "LOSSLESS_TEXT_MATCH", "FACT_EQUIVALENT", "VERIFIED_CONFLICT",
        "NO_DUPLICATE_FOUND", "SUBJECT_UNRESOLVED", "RULE_UNCOVERED", "TIME_RELATION_UNCERTAIN",
        "NUMERIC_ALIGNMENT_FAILED", "EVIDENCE_INVALID", "EXTRACTION_FAILED", "FACT_INCOMPLETE",
        "RECALL_INCOMPLETE", "CANDIDATE_BUDGET_EXHAUSTED", "DEPENDENCY_TIMEOUT",
    }
    source = main(store, receipt)
    diagnostics = source["diagnostics"]
    assert len(allowed) == 15
    assert diagnostics["reason_code"] in allowed
    assert diagnostics["reason_code"] == "NO_DUPLICATE_FOUND"
    audit = store.get(audit_index(receipt), source["audit_ids"][0])["source"]["payload"]
    for payload in (diagnostics["decision"], audit):
        assert payload["fixed_fixture"] is True
        assert payload["algorithm_executed"] is False
        assert payload["unresolved"] == ["REAL_RECALL_NOT_EXECUTED", "REAL_FACT_NOT_EXECUTED"]


def test_successful_freeze_releases_only_its_own_capture_and_preparation_references():
    store = MemoryStore()
    receipt = coordinator(store).accept(request())
    other = coordinator(store).accept(replace(request(request_id="200-1", item_id="200-1"), scope_id="other"))
    worker = probe(store)
    other_prepared = ready(worker, other)
    worker.capture_visibility(receipt)  # An older unused snapshot must also be released.
    prepared = ready(worker, receipt)
    assert len(worker._captures) == 3
    assert set(worker._prepared) == {receipt.record_id, other.record_id}
    result = worker.freeze(receipt, preparation=prepared)
    assert set(worker._prepared) == {other.record_id}
    assert list(worker._captures.values()) == [other_prepared["snapshot"]]
    assert worker.freeze(receipt) == result
    assert probe(store).freeze(receipt) == result
    worker.freeze(other, preparation=other_prepared)
    assert worker._captures == worker._prepared == {}


def test_owner_changes_after_audit_before_terminal_cas_is_rechecked():
    store = MemoryStore()
    receipt = coordinator(store).accept(request())
    worker = probe(store)
    prepared = ready(worker, receipt)

    def transfer(idx, key):
        if idx == audit_index(receipt):
            store.after_write = None
            mutate(store, CONTROL_INDEX, day_key(receipt), lambda body: body.update(owner_id="writer-2"))

    store.after_write = transfer
    with pytest.raises(AdmissionConflict):
        worker.freeze(receipt, preparation=prepared)
    assert main(store, receipt)["task_state"] == "running"


def test_terminal_written_watermark_not_written_restart_reuses_exact_frozen_payload():
    store, _, receipt, worker = fixture()
    before = deepcopy(main(store, receipt))
    assert store.get(CONTROL_INDEX, day_key(receipt))["source"]["decision_watermark"] == 0
    restarted = probe(store)
    assert restarted.freeze(receipt) == before["result"]
    assert restarted.advance_watermark(receipt) == 1
    assert main(store, receipt) == before


@pytest.mark.parametrize("stage", ["terminal", "claim", "ack"])
def test_unknown_cas_ack_readback_confirms_same_identity_without_extra_attempt(stage):
    store, clock = MemoryStore(), Clock()
    receipt = coordinator(store).accept(request())
    worker = probe(store, clock)
    prepared = ready(worker, receipt)
    if stage != "terminal":
        worker.freeze(receipt, preparation=prepared)
    if stage == "ack":
        lease = worker.claim_delivery(receipt, lease_seconds=5)

    def lost_ack(idx, key):
        if idx == index(receipt):
            store.after_write = None
            raise OSError("CAS applied; ACK lost")

    store.after_write = lost_ack
    if stage == "terminal":
        worker.freeze(receipt, preparation=prepared)
        assert main(store, receipt)["task_state"] == "succeeded"
    elif stage == "claim":
        lease = worker.claim_delivery(receipt, lease_seconds=5)
        assert lease == main(store, receipt)["delivery_lease"]
    else:
        assert worker.ack_delivery(receipt, lease=lease, http_status=200, response_body=b'{"succeed":true}') == "delivered"
    assert main(store, receipt)["callback_attempts"] == (0 if stage == "terminal" else 1)


def test_claim_unknown_not_written_does_not_authorize_attempt():
    store, _, receipt, worker = fixture()
    original = store.replace
    store.replace = lambda *args: (_ for _ in ()).throw(OSError("not written"))
    with pytest.raises(AdmissionUnknown):
        worker.claim_delivery(receipt, lease_seconds=5)
    store.replace = original
    assert main(store, receipt)["callback_attempts"] == 0


def test_claim_before_send_crash_recovers_persisted_backoff_and_new_attempt():
    store, clock, receipt, worker = fixture()
    first = worker.claim_delivery(receipt, lease_seconds=5)
    clock.advance(5)
    restarted = probe(store, clock, owner="delivery-2")
    assert restarted.recover_delivery(receipt) == "pending"
    due = main(store, receipt)["next_delivery_at"]
    assert due == _utc(clock.now + timedelta(seconds=1))
    with pytest.raises(AdmissionConflict):
        restarted.claim_delivery(receipt, lease_seconds=5)
    assert restarted.recover_delivery(receipt) == "pending"
    assert main(store, receipt)["next_delivery_at"] == due
    clock.advance(1)
    second = restarted.claim_delivery(receipt, lease_seconds=5)
    assert second["generation"] == first["generation"] + 1
    assert second["attempt_id"] != first["attempt_id"]
    assert main(store, receipt)["callback_attempts"] == 2


@pytest.mark.parametrize("status,body", [(500, b'{"succeed":true}'), (200, b'{"succeed":false}'), (200, b'{}'), (200, b'bad'), (None, None)])
def test_unconfirmed_or_failed_ack_retries_without_mutating_frozen_data(status, body):
    store, _, receipt, worker = fixture()
    before = frozen_identity(main(store, receipt))
    lease = worker.claim_delivery(receipt, lease_seconds=5)
    assert worker.ack_delivery(receipt, lease=lease, http_status=status, response_body=body) == "pending"
    assert frozen_identity(main(store, receipt)) == before
    assert main(store, receipt)["next_delivery_at"] == _utc(NOW + timedelta(seconds=1))


def test_twelve_attempts_include_first_restart_does_not_allow_thirteenth():
    store, clock, receipt, worker = fixture()
    before = frozen_identity(main(store, receipt))
    for attempt in range(1, 13):
        worker = probe(store, clock)
        worker.claim_delivery(receipt, lease_seconds=1)
        clock.advance(1)
        state = worker.recover_delivery(receipt)
        source = main(store, receipt)
        assert source["callback_attempts"] == source["diagnostics"]["delivery"]["round_attempts"] == attempt
        if attempt < 12:
            assert state == "pending"
            delay = min(2 ** (attempt - 1), 1800)
            assert source["next_delivery_at"] == _utc(clock.now + timedelta(seconds=delay))
            clock.advance(delay)
        else:
            assert state == "exhausted" and source["next_delivery_at"] is None
    with pytest.raises(AdmissionConflict):
        probe(store, clock).claim_delivery(receipt, lease_seconds=1)
    assert main(store, receipt)["callback_attempts"] == 12
    assert frozen_identity(main(store, receipt)) == before


@pytest.mark.parametrize("expiry_first", [False, True])
@pytest.mark.parametrize("delivered", [False, True])
def test_fixed_deadline_caps_lease_and_expiry_never_reverts_delivered(expiry_first, delivered):
    store, clock = MemoryStore(), Clock()
    receipt = coordinator(store).accept(request())
    if expiry_first:
        clock.now = datetime.fromisoformat(receipt.expires_at.replace("Z", "+00:00")) - timedelta(hours=1)
    worker = probe(store, clock)
    freeze(worker, receipt)
    source = main(store, receipt)
    deadline = min(clock.now + timedelta(hours=24), datetime.fromisoformat(receipt.expires_at.replace("Z", "+00:00")))
    assert source["delivery_deadline_at"] == _utc(deadline)
    lease = worker.claim_delivery(receipt, lease_seconds=100000)
    assert lease["lease_until"] == _utc(deadline)
    if delivered:
        worker.ack_delivery(receipt, lease=lease, http_status=204, response_body=b'{"succeed":true}')
    clock.now = deadline
    if expiry_first:
        # Ordinary recovery is not the P08 metadata-only lifecycle marker.
        with pytest.raises(AdmissionConflict, match="expir"):
            worker.recover_delivery(receipt)
        expected = "delivered" if delivered else "delivering"
    else:
        expected = "delivered" if delivered else "expired"
        assert worker.recover_delivery(receipt) == expected
    with pytest.raises(AdmissionConflict):
        worker.claim_delivery(receipt, lease_seconds=1)
    assert main(store, receipt)["delivery_state"] == expected


def test_backoff_at_deadline_is_not_scheduled_or_sent():
    store, clock, receipt, worker = fixture()
    deadline = datetime.fromisoformat(main(store, receipt)["delivery_deadline_at"].replace("Z", "+00:00"))
    clock.now = deadline - timedelta(milliseconds=500)
    lease = worker.claim_delivery(receipt, lease_seconds=1)
    assert worker.ack_delivery(receipt, lease=lease) == "pending"
    assert main(store, receipt)["next_delivery_at"] is None
    with pytest.raises(AdmissionConflict):
        worker.claim_delivery(receipt, lease_seconds=1)
    clock.now = deadline
    assert worker.recover_delivery(receipt) == "expired"


@pytest.mark.parametrize("alter", ["old_attempt", "owner", "generation", "attempt_id", "route_ref", "payload_hash"])
def test_late_or_forged_lease_cannot_overwrite_current_owner(alter):
    store, clock, receipt, worker = fixture()
    first = worker.claim_delivery(receipt, lease_seconds=1)
    clock.advance(1)
    worker.recover_delivery(receipt)
    clock.advance(1)
    current = worker.claim_delivery(receipt, lease_seconds=5)
    token = deepcopy(first if alter == "old_attempt" else current)
    if alter != "old_attempt":
        key = "owner_id" if alter == "owner" else alter
        token[key] = 88 if key == "generation" else "wrong"
    before = deepcopy(main(store, receipt))
    with pytest.raises(AdmissionConflict):
        worker.ack_delivery(receipt, lease=token, http_status=200, response_body=b'{"succeed":true}')
    assert main(store, receipt) == before
    assert worker.ack_delivery(receipt, lease=current, http_status=200, response_body=b'{"succeed":true}') == "delivered"


@pytest.mark.parametrize("field", ["callback_body", "event_id", "result", "route_ref"])
def test_frozen_payload_corruption_blocks_claim_and_terminal_reuse(field):
    store, _, receipt, worker = fixture()
    def corrupt(body):
        if field == "route_ref":
            body["diagnostics"]["delivery"][field] = "changed"
        else:
            body[field] = "changed"
    mutate(store, index(receipt), receipt.record_id, corrupt)
    with pytest.raises(AdmissionConflict):
        worker.claim_delivery(receipt, lease_seconds=5)
    with pytest.raises(AdmissionConflict):
        worker.freeze(receipt)


def test_no_model_or_callback_network_ports_are_used(monkeypatch):
    import socket
    calls = []
    def forbidden(*args, **kwargs):
        calls.append("network")
        raise AssertionError("fixed probe cannot use network")
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    store, _, receipt, worker = fixture()
    lease = worker.claim_delivery(receipt, lease_seconds=1)
    worker.ack_delivery(receipt, lease=lease, http_status=200, response_body=b'{"succeed":true}')
    assert calls == []
    audit = store.get(audit_index(receipt), main(store, receipt)["audit_ids"][0])["source"]
    assert audit["payload"]["algorithm_executed"] is False
    assert audit["payload"]["comparison_count"] == 0


@pytest.mark.parametrize("field,value", [
    ("event_id", "wrong"), ("payload_hash", "wrong"), ("route_ref", "wrong"),
    ("result_version", 2), ("round", 2), ("attempt_id", "wrong"),
    ("generation", 99), ("lease_until", _utc(NOW + timedelta(days=2))),
])
def test_persisted_lease_must_match_frozen_identity_even_if_token_matches(field, value):
    store, _, receipt, worker = fixture()
    worker.claim_delivery(receipt, lease_seconds=5)
    mutate(store, index(receipt), receipt.record_id,
           lambda body: body["delivery_lease"].update({field: value}))
    source = main(store, receipt)
    with pytest.raises(AdmissionConflict):
        worker.ack_delivery(receipt, lease=source["delivery_lease"], http_status=200,
                            response_body=b'{"succeed":true}')
    assert main(store, receipt) == source


def test_ack_crossing_lease_expiry_before_state_cas_is_not_confirmed(monkeypatch):
    store, clock, receipt, worker = fixture()
    lease = worker.claim_delivery(receipt, lease_seconds=5)

    def confirm_after_expiry(status, body):
        clock.advance(6)
        return True

    # Tie the crossing to ACK processing, not the implementation's clock-call count.
    monkeypatch.setattr("news_flash_dedup.g0_persistence.ack_succeeded", confirm_after_expiry)
    with pytest.raises(AdmissionConflict):
        worker.ack_delivery(receipt, lease=lease, http_status=200, response_body=b'{"succeed":true}')
    assert main(store, receipt)["delivery_state"] == "delivering"


@pytest.mark.parametrize("stage", ["claim", "ack"])
def test_persisted_write_with_unavailable_readback_remains_unknown_until_recovery(stage):
    store, clock, receipt, worker = fixture()
    if stage == "ack":
        lease = worker.claim_delivery(receipt, lease_seconds=1)
    def lose_readback(idx, key):
        if idx == index(receipt):
            store.after_write = None
            def unreadable(read_index, read_key, value):
                if read_index == index(receipt):
                    raise OSError("readback unavailable")
            store.after_get = unreadable
            raise OSError("write acknowledgement unavailable")
    store.after_write = lose_readback
    with pytest.raises(AdmissionUnknown):
        if stage == "claim":
            worker.claim_delivery(receipt, lease_seconds=1)
        else:
            worker.ack_delivery(receipt, lease=lease, http_status=200, response_body=b'{"succeed":true}')
    store.after_get = None
    clock.advance(1)
    restarted = probe(store, clock)
    assert restarted.recover_delivery(receipt) == ("pending" if stage == "claim" else "delivered")
    assert main(store, receipt)["callback_attempts"] == 1
    if stage == "claim":
        with pytest.raises(AdmissionConflict):
            restarted.claim_delivery(receipt, lease_seconds=1)


@pytest.mark.parametrize("damage", ["generation", "expiry"])
def test_audit_io_cannot_bypass_final_generation_or_expiry_check(damage):
    store, clock = MemoryStore(), Clock()
    receipt = coordinator(store).accept(request())
    worker = probe(store, clock)
    prepared = ready(worker, receipt)
    def change(idx, key):
        if idx == audit_index(receipt):
            store.after_write = None
            if damage == "expiry":
                clock.now = datetime.fromisoformat(receipt.expires_at.replace("Z", "+00:00"))
            else:
                mutate(store, index(receipt), receipt.record_id,
                       lambda body: body["task_lease"]["preparation"].update(generation=99))
    store.after_write = change
    with pytest.raises(AdmissionConflict):
        worker.freeze(receipt, preparation=prepared)
    assert main(store, receipt)["task_state"] == "running"
    assert "callback_body" not in main(store, receipt)


def test_orphan_decision_audit_after_restart_is_not_misused_for_new_preparation():
    store = MemoryStore()
    receipt = coordinator(store).accept(request())
    worker = probe(store)
    prepared = ready(worker, receipt)
    def crash(idx, key):
        if idx == audit_index(receipt):
            store.after_write = None
            raise OSError("audit create acknowledgement lost")
    store.after_write = crash
    with pytest.raises(AdmissionUnknown):
        worker.freeze(receipt, preparation=prepared)
    orphan = [key for idx, key in store.docs if idx == audit_index(receipt)]
    freeze(probe(store), receipt)
    assert len(orphan) == 1
    assert main(store, receipt)["audit_ids"] != orphan
    assert store.get(audit_index(receipt), orphan[0]) is not None


def test_current_preparation_cannot_reuse_refresh_after_original_disappears():
    store = MemoryStore()
    first = coordinator(store).accept(replace(request(), scope_id="other"))
    receipt = coordinator(store).accept(request(request_id="200-1", item_id="200-1"))
    worker = probe(store)
    prepared = ready(worker, receipt)
    del store.docs[index(first), first.record_id]
    with pytest.raises(AdmissionConflict):
        worker.freeze(receipt, preparation=prepared)
    assert main(store, receipt)["task_state"] == "running"


def test_pending_with_persisted_twelve_attempts_is_exhausted_without_claim():
    store, _, receipt, worker = fixture()
    def exhausted(body):
        body["callback_attempts"] = 12
        body["diagnostics"]["delivery"]["round_attempts"] = 12
        body["delivery_lease"]["generation"] = 12
        body["delivery_lease"]["attempt_id"] = _digest([body["event_id"], 12])
    mutate(store, index(receipt), receipt.record_id, exhausted)
    with pytest.raises(AdmissionConflict):
        worker.claim_delivery(receipt, lease_seconds=1)
    assert main(store, receipt)["delivery_state"] == "exhausted"
    assert main(store, receipt)["callback_attempts"] == 12


@pytest.mark.parametrize("materialized_seq", [1, 0, True])
def test_explicit_materialization_snapshot_does_not_require_legacy_single_slot_head(materialized_seq):
    store = MemoryStore()
    receipt = coordinator(store).accept(request())
    del store.docs[CONTROL_INDEX, "admission_head"]
    worker = probe(store)
    if type(materialized_seq) is not int or materialized_seq < receipt.arrival_seq:
        with pytest.raises(AdmissionConflict):
            worker.capture_visibility(receipt, last_materialized_seq=materialized_seq)
    else:
        snapshot = worker.capture_visibility(receipt, last_materialized_seq=materialized_seq)
        prepared = worker.prepare_visibility(receipt, snapshot=snapshot, refresh_receipt={
            "index": index(receipt), "_shards": {"total": 1, "successful": 1, "failed": 0},
        })
        worker.freeze(receipt, preparation=prepared)
        assert main(store, receipt)["task_state"] == "succeeded"
