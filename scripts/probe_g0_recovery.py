"""Small, isolated batch recovery fixtures; not deployment fencing or a G0 verdict."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import uuid
from collections.abc import Mapping
from dataclasses import asdict, replace
from datetime import date, datetime, time as day_time, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOG_TEMP = ROOT.parent / "log" / "temp"
sys.path.insert(0, str(ROOT / "src"))

from bench_admission import client_from_environment  # noqa: E402
from news_flash_dedup.admission import (  # noqa: E402
    AdmissionConflict, AdmissionRequest, AdmissionUnknown, BUSINESS_ZONE,
    CONTROL_INDEX, REQUEST_INDEX, _digest, _utc,
)
from news_flash_dedup.batch_admission import (  # noqa: E402
    BatchAdmissionCoordinator, BatchLimits, HEAD_ID,
)
from news_flash_dedup.batch_es_store import ElasticsearchBatchStore  # noqa: E402
from news_flash_dedup.es_admission_schema import (  # noqa: E402
    control_mapping, item_mapping, request_mapping,
)
from news_flash_dedup.g0_persistence import FixedArtifactProbe  # noqa: E402

SCENARIOS = (
    "cas-before", "cas-after", "pending-confirmed", "main", "request", "item", "seq",
    "complete-no-shrink", "shrink-ack", "terminal", "lease", "multiscope-midnight", "expiry",
)
PARTIAL = {"main": 1, "request": 2, "item": 3, "seq": 4, "complete-no-shrink": 8}
BOUNDARIES = {
    "no_real_callback": True, "no_model": True, "no_real_model": True, "deployment_fencing_verified": False,
    "server_inflight_absence_proven": False, "g0_passed": False,
    "clock_mode": "trusted_simulated_business_clock", "ack_mode": "local_simulation_only",
    "artifact_mode": "fixed_fixture_no_recall_or_fact", "http_202_measured": False,
    "physical_deletion_verified": False,
}


def validate_run_id(run_id: str) -> str:
    if not isinstance(run_id, str) or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?", run_id):
        raise ValueError("run-id requires 1..32 lowercase ASCII letters/digits with internal hyphens")
    return run_id


def scenario_prefix(run_id: str, scenario: str) -> str:
    validate_run_id(run_id)
    if scenario not in SCENARIOS:
        raise ValueError("one explicit recovery scenario is required")
    return f"p01-batch-recovery-{run_id}-{scenario}-"


def validate_prefix(prefix: str) -> None:
    for scenario in SCENARIOS:
        start, end = "p01-batch-recovery-", "-" + scenario + "-"
        if prefix.startswith(start) and prefix.endswith(end):
            run_id = prefix[len(start):-len(end)]
            if scenario_prefix(run_id, scenario) == prefix:
                return
    raise ValueError("only a dedicated batch recovery scenario prefix is permitted")


def validate_day(day: date, real_today: date | None = None) -> None:
    today = real_today or datetime.now(BUSINESS_ZONE).date()
    if type(day) is not date or not today - timedelta(days=6) <= day < today:
        raise ValueError("fixture midnight pair must be inside the current seven-day retention window")


def parse_options(argv=None):
    parser = argparse.ArgumentParser(description="Isolated small batch crash/recovery fixtures")
    parser.add_argument("--run-id", default=uuid.uuid4().hex[:12])
    parser.add_argument("--phase", choices=("all", "accept", "resume", "lease"), default="all")
    parser.add_argument("--scenario", choices=("all", *SCENARIOS), default="all")
    parser.add_argument("--confirm-uat", action="store_true")
    parser.add_argument("--fixture-day", type=date.fromisoformat,
                        default=datetime.now(BUSINESS_ZONE).date() - timedelta(days=1))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--process-timeout", type=float, default=60.0)
    options = parser.parse_args(argv)
    validate_run_id(options.run_id)
    validate_day(options.fixture_day)
    if not options.confirm_uat:
        raise ValueError("explicit --confirm-uat is required before client creation")
    if not 0 < options.process_timeout <= 120:
        raise ValueError("process timeout must be positive and at most 120 seconds")
    if options.phase != "all" and options.scenario == "all":
        raise ValueError("child phases require one scenario and a parent handoff on stdin")
    return options


def audit_mapping() -> dict:
    fields = {name: {"type": "keyword"} for name in (
        "audit_id audit_type scope_id record_id candidate_record_id request_id business_date "
        "pipeline_version payload_hash").split()}
    fields.update({"arrival_seq": {"type": "long"}, "created_at": {"type": "date"},
                   "expires_at": {"type": "date"}, "payload": {"type": "object", "enabled": False}})
    return {"mappings": {"dynamic": "strict", "properties": fields}}


def day_index(day: date | str, kind="items") -> str:
    parsed = date.fromisoformat(day) if isinstance(day, str) else day
    return f"news-dedup-{kind}-v1-" + parsed.strftime("%Y.%m.%d")


def resources(day: date) -> dict:
    result = {CONTROL_INDEX: control_mapping(), REQUEST_INDEX: request_mapping()}
    for current in (day, day + timedelta(days=1)):
        result[day_index(current)] = item_mapping()
        result[day_index(current, "audits")] = audit_mapping()
    for mapping in result.values():
        mapping.setdefault("settings", {}).update(number_of_shards=1, number_of_replicas=0)
    return result


def provision(client, prefix: str, day: date, *, real_today: date | None = None) -> dict:
    validate_prefix(prefix)
    validate_day(day, real_today)
    mappings = resources(day)
    for logical in mappings:
        if client.indices.exists(index=prefix + logical):
            raise RuntimeError("recovery namespace already exists; choose a fresh run-id")
    for logical, mapping in mappings.items():
        response = client.indices.create(index=prefix + logical, body=mapping)
        body = getattr(response, "body", response)
        if not isinstance(body, Mapping) or body.get("acknowledged") is not True or body.get("shards_acknowledged") is not True:
            raise RuntimeError("isolated index creation is not fully acknowledged")
    return {"resource_count": len(mappings), "configured_shards": 1, "configured_replicas": 0,
            "topology_scope": "explicit_fixture_index_settings_not_production_redundancy"}


class RestrictedStore(ElasticsearchBatchStore):
    def __init__(self, client, prefix: str, day: date):
        validate_prefix(prefix)
        super().__init__(client, index_prefix=prefix)
        self.allowed = frozenset(resources(day))

    def _physical(self, index: str) -> str:
        if index not in self.allowed:
            raise AdmissionConflict("index is outside the fixed recovery resource set")
        return super()._physical(index)


def restricted_store(client, run_id: str, scenario: str, day: date):
    return RestrictedStore(client, scenario_prefix(run_id, scenario), day)


class Clock:
    def __init__(self, value: datetime):
        self.value = value

    def __call__(self):
        return self.value

    def move_to(self, value: datetime):
        if value < self.value:
            raise AdmissionConflict("simulated clock cannot move backwards")
        self.value = value


def timestamp(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("aware time required")
    return result


def start_time(day: date) -> datetime:
    return datetime.combine(day, day_time(23, 59), BUSINESS_ZONE).astimezone(timezone.utc)


def resume_time(day: date) -> datetime:
    return start_time(day) + timedelta(minutes=2)


def fixed_request(number: int, scope: str, when: datetime) -> AdmissionRequest:
    return AdmissionRequest(
        scope_id=scope, request_id=f"{number}-1", item_id=f"{number}-1",
        text=f"固定恢复正文 {number}。", received_at=when, schema_version="1",
        pipeline_version="g0-batch-recovery-fixed-v1", embedding_space_id="none",
        delivery_route_ref="g0-no-send-fixed-route-v1", trace_id=f"first-fixed-trace-{number}",
    )


def fixture_groups(scenario: str, day: date):
    first, second = start_time(day), resume_time(day)
    if scenario == "multiscope-midnight":
        return [(first, [fixed_request(1, "alpha", first), fixed_request(2, "beta", first),
                         fixed_request(3, "alpha", first)]),
                (second, [fixed_request(4, "beta", second), fixed_request(5, "alpha", second),
                          fixed_request(6, "beta", second)])]
    return [(first, [fixed_request(1, "alpha", first), fixed_request(2, "alpha", first)])]


def fixture_requests(scenario: str, day: date):
    return [request for _, group in fixture_groups(scenario, day) for request in group]


def new_request(day: date):
    return fixed_request(99, "alpha", resume_time(day))


def coordinator(store, owner: str, clock: Clock, isolated):
    return BatchAdmissionCoordinator(store, owner_id=owner, owner_isolated=isolated,
                                     limits=BatchLimits(max_batch_items=8, max_log_items=16), clock=clock)


def owner_id(run_id: str, scenario: str, generation: int):
    scenario_prefix(run_id, scenario)
    return f"{run_id}-{scenario}-writer-{generation}"


def exit_code(scenario: str):
    return 70 + SCENARIOS.index(scenario)


def sha(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def receipt_hash(receipt):
    return sha({key: value for key, value in asdict(receipt).items() if key != "reused"})


class FaultStore:
    """Own-store hooks only. Partial bulk splitting is not a performance path."""

    def __init__(self, base, scenario, emit, exit_process):
        self.base, self.scenario, self.emit, self.exit_process = base, scenario, emit, exit_process
        # P0-T4：协调器优先走逐条解析面——仅当 base 暴露该面时才保活
        # PARTIAL 注入（base 无该面=legacy 路径，注入仍走 bulk_create）。
        if callable(getattr(base, "bulk_create_classified", None)):
            self.bulk_create_classified = self._fault_bulk_classified

    def __getattr__(self, name):
        return getattr(self.base, name)

    def halt(self, point, *, write_confirmed=True, ack_lost=False, frozen=None):
        summary = {} if frozen is None else {
            "payload_sha256": frozen["diagnostics"]["delivery"]["payload_hash"],
            "result_sha256": sha(frozen["result"]),
            "callback_attempts": frozen["callback_attempts"],
            "lease_generation": frozen["delivery_lease"]["generation"],
        }
        self.emit({"event": "fault", "point": point, "write_confirmed": write_confirmed,
                   "ack_simulated_lost": ack_lost, "exit_code": exit_code(self.scenario), **summary})
        self.exit_process(exit_code(self.scenario))
        raise RuntimeError("exit hook unexpectedly returned")

    def replace(self, index, key, body, seq_no, primary_term):
        admission = key == HEAD_ID and body.get("last_allocated_seq", 0) > 0 and body.get("last_materialized_seq") == 0
        if self.scenario == "cas-before" and admission:
            self.halt("cas-before", write_confirmed=False)
        shrinking = key == HEAD_ID and body.get("last_materialized_seq", 0) > 0
        self.base.replace(index, key, body, seq_no, primary_term)
        if self.scenario == "cas-after" and admission:
            self.halt("cas-after", ack_lost=True)
        if self.scenario == "shrink-ack" and shrinking:
            self.halt("shrink-ack", ack_lost=True)
        if index.startswith("news-dedup-items-v1-"):
            if self.scenario == "terminal" and body.get("task_state") == "succeeded":
                self.halt("terminal", ack_lost=True, frozen=body)
            if self.scenario == "lease" and body.get("delivery_state") == "delivering":
                self.halt("lease", ack_lost=True, frozen=body)

    def bulk_create(self, documents):
        stop = PARTIAL.get(self.scenario)
        if stop is None:
            return self.base.bulk_create(documents)
        for position, (index, key, body) in enumerate(documents, 1):
            self.base.create(index, key, body)
            if position == stop:
                self.halt(self.scenario, ack_lost=True)
        raise RuntimeError("partial materialization hook was not reached")

    def _fault_bulk_classified(self, documents):
        # PARTIAL 注入在逐条解析面保活（挂点语义与 bulk_create 面零 diff）。
        stop = PARTIAL.get(self.scenario)
        if stop is None:
            return self.base.bulk_create_classified(documents)
        for position, (index, key, body) in enumerate(documents, 1):
            self.base.create(index, key, body)
            if position == stop:
                self.halt(self.scenario, ack_lost=True)
        raise RuntimeError("partial materialization hook was not reached")


def freeze_one(store, client, prefix, probe, receipt):
    main = store.get(day_index(receipt.business_date), receipt.record_id)
    if main is None:
        raise AdmissionConflict("fixed task has no materialized main")
    if main["source"]["task_state"] == "succeeded":
        probe.freeze(receipt)
    else:
        head = store.get(CONTROL_INDEX, HEAD_ID)
        snapshot = probe.capture_visibility(receipt, last_materialized_seq=head["source"]["last_materialized_seq"])
        logical = snapshot["index"]
        if logical != day_index(receipt.business_date):
            raise AdmissionConflict("refresh target differs from receipt day")
        validate_prefix(prefix)
        response = client.indices.refresh(index=prefix + logical)
        body = getattr(response, "body", response)
        shards = body.get("_shards") if isinstance(body, Mapping) else None
        if not isinstance(shards, Mapping):
            raise AdmissionConflict("refresh response has no shard confirmation")
        # Never invent shard success. Keep actual counts; the probe rejects partial success.
        receipt_refresh = {"index": logical, "_shards": {key: shards.get(key) for key in ("total", "successful", "failed")}}
        preparation = probe.prepare_visibility(receipt, snapshot=snapshot, refresh_receipt=receipt_refresh)
        probe.freeze(receipt, preparation=preparation)
    return probe.advance_watermark(receipt)


def accept_phase(store, client, run_id, scenario, day, *, emit, exit_process=os._exit):
    prefix = scenario_prefix(run_id, scenario)
    # Fresh provision is separately checked by the parent. This is local fixture authority only.
    fresh = store.get(CONTROL_INDEX, HEAD_ID) is None
    if not fresh:
        raise AdmissionConflict("writer requires a fresh namespace")
    clock = Clock(start_time(day))
    fault = FaultStore(store, scenario, emit, exit_process)
    service = coordinator(fault, owner_id(run_id, scenario, 1), clock, lambda: fresh)
    receipts = []
    for when, group in fixture_groups(scenario, day):
        clock.move_to(when)
        accepted = service.accept_batch(group)
        receipts.extend(accepted)
        emit({"event": "accepted", "count": len(accepted),
              "receipt_hashes": [receipt_hash(receipt) for receipt in accepted]})
        if scenario in {"pending-confirmed", "expiry"}:
            fault.halt(scenario)
        service.materialize_oldest()
    if scenario == "lease":
        # Freeze/claim at the restart fixture instant, then recover precisely at lease_until.
        clock.move_to(resume_time(day))
    probe = FixedArtifactProbe(fault, owner_id=service.owner_id, clock=clock)
    if scenario in {"terminal", "lease"}:
        freeze_one(fault, client, prefix, probe, receipts[0])
        probe.claim_delivery(receipts[0], lease_seconds=2)
    elif scenario == "multiscope-midnight":
        # Establish four distinct old-owner day controls, not a shared scope/day barrier.
        seen = set()
        for receipt in receipts:
            key = (receipt.scope_id, receipt.business_date)
            if key not in seen:
                seen.add(key)
                head = store.get(CONTROL_INDEX, HEAD_ID)
                snapshot = probe.capture_visibility(receipt, last_materialized_seq=head["source"]["last_materialized_seq"])
                if snapshot["index"] != day_index(receipt.business_date):
                    raise AdmissionConflict("unexpected fixed visibility day")
        fault.halt(scenario)
    raise RuntimeError("expected writer fault was not reached")


def observation_keys(scenario, day):
    keys = [(CONTROL_INDEX, HEAD_ID)]
    number = 0
    for when, group in fixture_groups(scenario, day):
        business_day = when.astimezone(BUSINESS_ZONE).date()
        for request in group:
            number += 1
            record = _digest([request.scope_id, request.item_id])
            keys.extend([(day_index(business_day), record),
                         (REQUEST_INDEX, "request:" + _digest([request.scope_id, request.request_id])),
                         (REQUEST_INDEX, "item:" + record), (REQUEST_INDEX, f"seq:{number}")])
    return keys


def observe_state(store, scenario, day):
    found = store.mget(observation_keys(scenario, day))
    if not isinstance(found, list) or len(found) != len(observation_keys(scenario, day)):
        raise AdmissionUnknown("exact-ID observation is incomplete")
    head = found[0]
    if head is None:
        raise AdmissionUnknown("writer head is missing")
    source = head["source"]
    counts = {"main": 0, "request": 0, "item": 0, "seq": 0}
    for number, document in enumerate(found[1:]):
        counts[("main", "request", "item", "seq")[number % 4]] += int(document is not None)
    return {"allocated": source["last_allocated_seq"], "materialized": source["last_materialized_seq"],
            "pending": sum(len(batch["entries"]) for batch in source["pending"]["batches"]),
            "four_document_parts": sum(counts.values()), "parts": counts,
            "stable_state_hash": sha(found)}


def make_handoff(run_id, scenario, day, events, *, returncode, isolated_at):
    faults = [event for event in events if event.get("event") == "fault"]
    accepted = [event for event in events if event.get("event") == "accepted"]
    if len(faults) != 1 or returncode != exit_code(scenario) or faults[0].get("exit_code") != returncode or faults[0].get("point") != scenario:
        raise AdmissionConflict("writer did not exit at the expected controlled fault")
    return {"run_id": validate_run_id(run_id), "scenario": scenario, "fixture_day": day.isoformat(),
            "prefix": scenario_prefix(run_id, scenario), "old_process_exited": True,
            "old_process_returncode": returncode, "isolated_at": isolated_at,
            "accepted_returns": sum(event["count"] for event in accepted),
            "receipt_hashes": [digest for event in accepted for digest in event["receipt_hashes"]],
            "fault": faults[0], "local_isolation_ref": f"local-child-exit:{run_id}:{scenario}:{returncode}"}


def verify_handoff(run_id, scenario, day, handoff):
    if (handoff.get("run_id") != run_id or handoff.get("scenario") != scenario or
            handoff.get("fixture_day") != day.isoformat() or handoff.get("prefix") != scenario_prefix(run_id, scenario) or
            handoff.get("old_process_exited") is not True or handoff.get("old_process_returncode") != exit_code(scenario) or
            handoff.get("local_isolation_ref") != f"local-child-exit:{run_id}:{scenario}:{exit_code(scenario)}"):
        raise AdmissionConflict("matching parent-observed local child exit is required")
    count = len(fixture_requests(scenario, day))
    accepted = 0 if scenario in {"cas-before", "cas-after"} else count
    if handoff.get("accepted_returns") != accepted or len(handoff.get("receipt_hashes", [])) != accepted:
        raise AdmissionConflict("writer acceptance evidence differs from the scenario")
    if type(handoff.get("isolated_at")) not in (int, float) or handoff["isolated_at"] < 0:
        raise AdmissionConflict("parent monotonic isolation timestamp is required")


def checked_originals(service, scenario, day):
    receipts = []
    for when, group in fixture_groups(scenario, day):
        for request in group:
            receipt = service.reconcile(request)
            if receipt is None:
                raise AdmissionUnknown("original accepted identity remains unconfirmed")
            # A retry intentionally supplies changed non-business metadata, including midnight time.
            retry = replace(request, received_at=resume_time(day), trace_id="retry-not-first",
                            delivery_route_ref="retry-not-first-route", pipeline_version="retry-version")
            reused = service.accept_batch([retry])[0]
            if not reused.reused or receipt_hash(reused) != receipt_hash(receipt):
                raise AdmissionConflict("same identity retry did not reuse its immutable receipt")
            if receipt.arrival_seq != len(receipts) + 1:
                raise AdmissionConflict("original sequence registration changed")
            main = service.store.get(day_index(receipt.business_date), receipt.record_id)
            if (main is None or main["source"].get("received_at") != _utc(request.received_at) or
                    any(getattr(receipt, key) != getattr(request, key) for key in (
                        "schema_version", "pipeline_version", "embedding_space_id", "trace_id", "delivery_route_ref"))):
                raise AdmissionConflict("first received time, version, trace or route changed")
            if scenario != "cas-before" and (
                    receipt.accepted_at != _utc(when) or receipt.business_date != when.astimezone(BUSINESS_ZONE).date().isoformat()):
                raise AdmissionConflict("first acceptance date or time changed")
            receipts.append(receipt)
    return receipts


class BusinessIOCounter:
    def __init__(self, store):
        self.base, self.business_io = store, 0
        # P0-T4：仅当 base 暴露逐条解析面时才同受业务 IO 禁令约束。
        if callable(getattr(store, "bulk_create_classified", None)):
            self.bulk_create_classified = self._gated_bulk_classified

    def __getattr__(self, name):
        return getattr(self.base, name)

    def _check(self, index):
        if index != CONTROL_INDEX:
            self.business_io += 1
            raise AdmissionConflict("expired fixture forbids business document IO")

    def get(self, index, key):
        self._check(index)
        return self.base.get(index, key)

    def replace(self, index, key, body, seq_no, primary_term):
        self._check(index)
        return self.base.replace(index, key, body, seq_no, primary_term)

    def create(self, index, key, body):
        self._check(index)
        return self.base.create(index, key, body)

    def mget(self, keys):
        for index, _ in keys:
            self._check(index)
        return self.base.mget(keys)

    def bulk_create(self, documents):
        for index, _, _ in documents:
            self._check(index)
        return self.base.bulk_create(documents)

    def _gated_bulk_classified(self, documents):
        # P0-T4：逐条解析面同受过期件业务 IO 禁令约束（probe 语义零 diff）。
        for index, _, _ in documents:
            self._check(index)
        return self.base.bulk_create_classified(documents)


def expired_resume(store, run_id, scenario, day, handoff):
    guard = BusinessIOCounter(store)
    clock = Clock(datetime.combine(day + timedelta(days=7), day_time.min, BUSINESS_ZONE).astimezone(timezone.utc))
    service = coordinator(guard, owner_id(run_id, scenario, 2), clock, lambda: handoff["old_process_exited"])
    try:
        service.takeover()
    except AdmissionConflict:
        pass
    head = guard.get(CONTROL_INDEX, HEAD_ID)["source"]
    quarantine = head.get("checkpoint", {}).get("batch_quarantine", {})
    if quarantine.get("reason") != "logical_expiry" or head["last_materialized_seq"] != 0:
        raise AdmissionConflict("expired pending was not quarantined without materialization")
    try:
        service.accept_batch([new_request(day)])
    except AdmissionConflict:
        pass
    else:
        raise AdmissionConflict("quarantined log accepted a new request")
    return {**BOUNDARIES, "status": "quarantined", "rto_seconds": None,
            "fresh_request_accepted": False, "expired_business_io": guard.business_io,
            "new_owner": head["owner_id"], "old_process_exited": True,
            "counts": {"input": 2, "accepted_returns": handoff["accepted_returns"],
                       "expired_quarantined": 2, "lost_accepted": 0, "unknown_unresolved": 0,
                       "four_documents_verified": 0},
            "head": {"allocated": head["last_allocated_seq"], "materialized": 0},
            "days": [], "payload_hashes": [], "fixed_terminal_seconds": None,
            "expiry_scope": "logical_pending_quarantine_not_physical_deletion"}


def resume_phase(store, client, run_id, scenario, day, handoff, *, monotonic=time.monotonic, emit=None):
    verify_handoff(run_id, scenario, day, handoff)
    if scenario == "expiry":
        return expired_resume(store, run_id, scenario, day, handoff)
    observations = [observe_state(store, scenario, day), observe_state(store, scenario, day)]
    if observations[0] != observations[1]:
        raise AdmissionUnknown("deterministic IDs are not stable; takeover is blocked")
    before = observations[1]
    count = len(fixture_requests(scenario, day))
    if before["allocated"] != (0 if scenario == "cas-before" else count):
        raise AdmissionUnknown("fault state does not explain original acceptance")
    clock = Clock(resume_time(day))
    service = coordinator(store, owner_id(run_id, scenario, 2), clock, lambda: handoff["old_process_exited"])
    service.takeover()
    if scenario == "cas-before":
        # Hook was before the request was sent; do not infer absence from an ES miss alone.
        if handoff["fault"].get("write_confirmed") is not False or before["four_document_parts"] != 0:
            raise AdmissionUnknown("pre-send absence is not established")
        service.accept_batch(fixture_requests(scenario, day))
        service.recover()
    originals = checked_originals(service, scenario, day)
    if handoff["receipt_hashes"] and [receipt_hash(receipt) for receipt in originals] != handoff["receipt_hashes"]:
        raise AdmissionConflict("accepted receipt hash was not preserved")
    old_payload_hash = handoff["fault"].get("payload_sha256")
    if scenario in {"terminal", "lease"}:
        original_main = store.get(day_index(originals[0].business_date), originals[0].record_id)["source"]
        if (not old_payload_hash or original_main["diagnostics"]["delivery"]["payload_hash"] != old_payload_hash or
                sha(original_main["result"]) != handoff["fault"].get("result_sha256")):
            raise AdmissionConflict("precrash frozen payload or result digest differs")
    fresh = service.accept_batch([new_request(day)])[0]
    service.recover()
    verified_fresh = service.reconcile(new_request(day))
    if fresh.reused or verified_fresh is None or receipt_hash(fresh) != receipt_hash(verified_fresh) or fresh.arrival_seq != count + 1:
        raise AdmissionUnknown("new independent request is not durably confirmed")
    # Stop RTO only after originals and a genuinely new request have passed four-document verification.
    rto = monotonic() - handoff["isolated_at"]
    if rto < 0:
        raise AdmissionConflict("parent/child monotonic clocks disagree")
    report = {**BOUNDARIES, "status": "admission_recovered", "before": before,
              "old_process_exited": True, "old_process_returncode": handoff["old_process_returncode"],
              "new_owner": service.owner_id, "fresh_request_accepted": True,
              "rto_seconds": rto, "rto_within_60s": rto <= 60,
              "rto_scope": "local_child_exit_through_new_child_start_and_fresh_four_document_acceptance",
              "counts": {"input": count, "accepted_returns": handoff["accepted_returns"],
                         "initial_not_accepted": count if scenario == "cas-before" else 0,
                         "initial_unknown": count if scenario == "cas-after" else 0,
                         "unknown_resolved": count if scenario == "cas-after" else 0,
                         "unknown_unresolved": 0, "lost_accepted": 0,
                         "reused_original": 0 if scenario == "cas-before" else count,
                         "four_documents_verified": count + 1, "fresh_accepted": 1},
              "old_frozen_payload_sha256": old_payload_hash,
              "old_frozen_result_sha256": handoff["fault"].get("result_sha256"),
              "old_frozen_payload_preserved": True if old_payload_hash else None,
              "payload_hashes": [], "day_takeovers": []}
    if emit:
        emit({"event": "admission_recovered", "report": json.loads(json.dumps(report))})
    terminal_started = time.monotonic()
    probe = FixedArtifactProbe(store, owner_id=service.owner_id, clock=clock)
    receipts = [*originals, fresh]
    groups = {}
    for receipt in receipts:
        groups.setdefault((receipt.scope_id, receipt.business_date), []).append(receipt)
    for (scope, business_day), group in sorted(groups.items()):
        receipt = group[0]
        key = f"day:{scope}:{business_day}"
        control = store.get(CONTROL_INDEX, key)
        if control is not None and control["source"]["owner_id"] != service.owner_id:
            probe.takeover_day(receipt, previous_owner_id=control["source"]["owner_id"],
                               expected_seq_no=control["seq_no"], expected_primary_term=control["primary_term"],
                               isolation_evidence_ref=handoff["local_isolation_ref"])
            report["day_takeovers"].append({"scope": scope, "day": business_day,
                                            "observed_seq_no": control["seq_no"],
                                            "observed_primary_term": control["primary_term"],
                                            "local_reference_only": True})
        for receipt in sorted(group, key=lambda value: value.arrival_seq):
            freeze_one(store, client, scenario_prefix(run_id, scenario), probe, receipt)
    days = []
    for (scope, business_day), group in sorted(groups.items()):
        state = store.get(CONTROL_INDEX, f"day:{scope}:{business_day}")["source"]
        sequences = [receipt.arrival_seq for receipt in group]
        days.append({"scope": scope, "day": business_day, "sequences": sequences,
                     "allocated": max(sequences), "materialized": max(sequences),
                     "allocated_count": len(sequences), "materialized_count": len(sequences),
                     "allocation_basis": "exact_reconciled_registrations_not_day_control_fields",
                     "decision": state["decision_watermark"], "lexical": state["lexical_watermark"],
                     "lexical_scope": "global_registered_prefix_for_this_day_barrier"})
    for receipt in receipts:
        main = store.get(day_index(receipt.business_date), receipt.record_id)["source"]
        report["payload_hashes"].append({"seq": receipt.arrival_seq,
                                         "sha256": main["diagnostics"]["delivery"]["payload_hash"]})
    head = store.get(CONTROL_INDEX, HEAD_ID)["source"]
    report.update(status="recovered", days=days,
                  head={"allocated": head["last_allocated_seq"], "materialized": head["last_materialized_seq"]},
                  fixed_terminal_seconds=time.monotonic() - terminal_started,
                  fixture_clock=_utc(clock()), run_id=run_id, scenario=scenario,
                  fixture_day=day.isoformat(), prefix=scenario_prefix(run_id, scenario))
    return report


def lease_phase(store, run_id, scenario, day, report):
    if report.get("status") != "recovered" or report.get("prefix") != scenario_prefix(run_id, scenario) or report.get("fixture_day") != day.isoformat():
        raise AdmissionConflict("lease phase requires this scenario's successful recovery report")
    started = time.monotonic()
    clock = Clock(timestamp(report["fixture_clock"]))
    service = coordinator(store, owner_id(run_id, scenario, 2), clock,
                          lambda: report.get("old_process_exited") is True)
    first_request = fixture_requests(scenario, day)[0]
    receipt = service.reconcile(first_request)
    if receipt is None:
        raise AdmissionUnknown("lease target is not reconciled")
    index = day_index(receipt.business_date)
    probe = FixedArtifactProbe(store, owner_id=owner_id(run_id, scenario, 3), clock=clock)
    original = store.get(index, receipt.record_id)["source"]
    original_hash = original["diagnostics"]["delivery"]["payload_hash"]
    if report["payload_hashes"][0]["sha256"] != original_hash:
        raise AdmissionConflict("first frozen payload hash changed")
    first = original["delivery_lease"] if original["delivery_state"] == "delivering" else probe.claim_delivery(receipt, lease_seconds=2)
    clock.move_to(max(clock(), timestamp(first["lease_until"])))
    if probe.recover_delivery(receipt) != "pending":
        raise AdmissionConflict("lease recovery did not persist pending backoff")
    due_main = store.get(index, receipt.record_id)["source"]
    due = timestamp(due_main["next_delivery_at"])
    try:
        probe.claim_delivery(receipt, lease_seconds=2)
    except AdmissionConflict:
        pass
    else:
        raise AdmissionConflict("lease recovery bypassed persisted backoff")
    # A new probe proves the stored schedule, not the previous instance's memory, controls retry.
    probe = FixedArtifactProbe(store, owner_id=owner_id(run_id, scenario, 3), clock=clock)
    if probe.recover_delivery(receipt) != "pending" or store.get(index, receipt.record_id)["source"]["next_delivery_at"] != due_main["next_delivery_at"]:
        raise AdmissionConflict("restart changed persisted backoff")
    clock.move_to(due)
    second = probe.claim_delivery(receipt, lease_seconds=2)
    try:
        probe.ack_delivery(receipt, lease=first, http_status=200, response_body=b'{"succeed":true}')
    except AdmissionConflict:
        pass
    else:
        raise AdmissionConflict("old delivery lease was accepted")
    state = probe.ack_delivery(receipt, lease=second, http_status=200, response_body=b'{"succeed":true}')
    final = store.get(index, receipt.record_id)["source"]
    if (final["callback_body"] != original["callback_body"] or final["result"] != original["result"] or
            second["generation"] != first["generation"] + 1 or second["attempt_id"] == first["attempt_id"] or
            final["callback_attempts"] != 2 or final["diagnostics"]["delivery"]["payload_hash"] != original_hash):
        raise AdmissionConflict("delivery recovery changed payload or attempt identity")
    return {**BOUNDARIES, "state": state, "payload_unchanged": True, "payload_sha256": original_hash,
            "first_generation": first["generation"], "second_generation": second["generation"],
            "attempts": final["callback_attempts"], "backoff_enforced": True,
            "persisted_next_delivery_at": due_main["next_delivery_at"],
            "lease_seconds_elapsed": time.monotonic() - started,
            "simulated_clock_advanced_seconds": (clock() - timestamp(report["fixture_clock"])).total_seconds()}


def emit_event(event):
    print(json.dumps(event, ensure_ascii=False, allow_nan=False), flush=True)


def safe_error(error):
    if isinstance(error, AdmissionUnknown):
        return "admission_unknown"
    if isinstance(error, AdmissionConflict):
        return "admission_conflict"
    if isinstance(error, (ValueError, TypeError, KeyError)):
        return "invalid_input_or_state"
    if isinstance(error, OSError):
        return "io_unconfirmed"
    return "operation_failed"


def execute_child(command, *, handoff, timeout, popen=subprocess.Popen):
    """Only this handle may be terminated; every wait has a finite timeout."""
    process = None
    result = {"returncode": None, "timed_out": False, "exited": False, "events": [],
              "errors": [], "stderr_suppressed": True}
    try:
        environment = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1"}
        process = popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                        text=True, encoding="utf-8", env=environment)
        try:
            output, _ = process.communicate(json.dumps(handoff, ensure_ascii=False), timeout=timeout)
        except subprocess.TimeoutExpired:
            result["timed_out"] = True
            process.terminate()
            try:
                output, _ = process.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                try:
                    output, _ = process.communicate(timeout=3)
                except subprocess.TimeoutExpired:
                    result["errors"].append("owned_child_exit_unconfirmed")
                    return result
        result["returncode"] = process.returncode
        result["exited"] = process.returncode is not None
        result["observed_exit_at"] = time.monotonic()
        for line in output.splitlines():
            try:
                event = json.loads(line)
                if not isinstance(event, dict) or event.get("event") not in {"accepted", "fault", "report", "error", "admission_recovered"}:
                    raise ValueError("unexpected child output")
                result["events"].append(event)
            except (ValueError, TypeError):
                result["errors"].append("invalid_child_output_suppressed")
    except Exception as error:
        result["errors"].append(safe_error(error))
        if process is not None and process.poll() is None:
            try:
                process.kill()
                process.wait(timeout=3)
                result["exited"] = True
                result["returncode"] = process.returncode
            except Exception:
                result["errors"].append("owned_child_exit_unconfirmed")
    return result


def child_command(options, scenario, phase):
    return [sys.executable, "-B", str(Path(__file__).resolve()), "--confirm-uat",
            "--run-id", options.run_id, "--scenario", scenario, "--phase", phase,
            "--fixture-day", options.fixture_day.isoformat()]


def final_report(process):
    reports = [event["report"] for event in process["events"] if event.get("event") == "report"]
    if process["timed_out"] or not process["exited"] or process["returncode"] != 0 or process["errors"] or len(reports) != 1:
        raise AdmissionUnknown("child phase did not complete with one safe report")
    return reports[0]


def run_all(options, *, client_factory=None, runner=execute_child, checkpoint=None):
    storage_mode = "real_elasticsearch" if client_factory is None else "injected_client_not_real_es_measurement"
    process_mode = "own_subprocesses_with_timeouts" if runner is execute_child else "injected_runner_not_process_measurement"
    client_factory = client_factory or client_from_environment
    # All programmatic callers receive the same gate as CLI callers.
    validate_run_id(options.run_id)
    validate_day(options.fixture_day)
    if not options.confirm_uat:
        raise ValueError("explicit UAT confirmation is required")
    scenarios = SCENARIOS if options.scenario == "all" else (options.scenario,)
    summary = {**BOUNDARIES, "run_id": options.run_id, "status": "running", "scenarios": [],
               "storage_mode": storage_mode, "process_mode": process_mode, "errors": []}
    for scenario in scenarios:
        case = {**BOUNDARIES, "scenario": scenario, "prefix": scenario_prefix(options.run_id, scenario),
                "status": "unresolved", "old_process_exited": False, "new_owner_started": False,
                "old_process_expected_exit": False,
                "counts": {"input_planned": len(fixture_requests(scenario, options.fixture_day)),
                           "accepted_returns": None, "lost_accepted": None,
                           "accepted_unverified": None, "initial_unknown": None,
                           "unknown_unresolved": None, "four_documents_verified": None},
                "rto_seconds": None, "errors": [], "processes": {}}
        summary["scenarios"].append(case)
        client = None
        case_start = time.monotonic()
        try:
            client = client_factory()
            info = client.info()
            if not str(info["version"]["number"]).startswith("8."):
                raise AdmissionConflict("Elasticsearch 8.x is required")
            case["es_version"] = str(info["version"]["number"])
            case["provision"] = provision(client, case["prefix"], options.fixture_day)
            client.close()
            client = None
            launch = {"fresh_namespace_provisioned": True, "prefix": case["prefix"]}
            writer = runner(child_command(options, scenario, "accept"), handoff=launch, timeout=options.process_timeout)
            case["processes"]["accept"] = writer
            case["old_process_exited"] = writer["exited"]
            if writer["timed_out"] or not writer["exited"] or writer["errors"]:
                raise AdmissionUnknown("old writer exit is unresolved")
            handoff = make_handoff(options.run_id, scenario, options.fixture_day, writer["events"],
                                   returncode=writer["returncode"], isolated_at=writer["observed_exit_at"])
            case["old_process_expected_exit"] = True
            unknown = len(fixture_requests(scenario, options.fixture_day)) if scenario == "cas-after" else 0
            case["counts"].update(accepted_returns=handoff["accepted_returns"],
                                  accepted_unverified=handoff["accepted_returns"],
                                  initial_unknown=unknown, unknown_unresolved=unknown)
            resumed = runner(child_command(options, scenario, "resume"), handoff=handoff, timeout=options.process_timeout)
            case["processes"]["resume"] = resumed
            # A started process is not yet evidence that owner transfer was confirmed.
            case["new_owner_process_exited"] = resumed.get("exited", False)
            # Retain the acceptance endpoint even if a later fixed-artifact phase fails.
            for event in resumed["events"]:
                if event.get("event") == "admission_recovered":
                    case["admission_recovery"] = event["report"]
                    case["rto_seconds"] = event["report"]["rto_seconds"]
                    case["counts"] = {**event["report"]["counts"], "accepted_unverified": 0}
                    case["new_owner_started"] = True
            recovered = final_report(resumed)
            case["recovery"] = recovered
            case["rto_seconds"] = recovered["rto_seconds"]
            case["counts"] = {**recovered["counts"], "accepted_unverified": 0}
            case["new_owner_started"] = True
            if scenario != "expiry":
                leased = runner(child_command(options, scenario, "lease"), handoff=recovered, timeout=options.process_timeout)
                case["processes"]["lease"] = leased
                case["delivery"] = final_report(leased)
            case["status"] = recovered["status"]
        except Exception as error:
            case["errors"].append(safe_error(error))
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception:
                    case["errors"].append("client_close_failed")
            case["whole_case_seconds"] = time.monotonic() - case_start
            if checkpoint:
                checkpoint(summary)
    summary["status"] = "completed_with_unresolved" if any(case["errors"] for case in summary["scenarios"]) else "fixtures_completed_not_g0"
    return summary


def run_child(options):
    validate_run_id(options.run_id)
    validate_day(options.fixture_day)
    if not options.confirm_uat:
        raise ValueError("explicit UAT confirmation is required before child client creation")
    # Parent handoffs contain only fixed metadata and digests, never bodies or credentials.
    handoff = json.loads(sys.stdin.read(256 * 1024))
    if not isinstance(handoff, dict):
        raise ValueError("parent handoff must be an object")
    prefix = scenario_prefix(options.run_id, options.scenario)
    if options.phase == "accept":
        if handoff != {"fresh_namespace_provisioned": True, "prefix": prefix}:
            raise AdmissionConflict("parent must provision this fresh namespace first")
    elif options.phase == "resume":
        verify_handoff(options.run_id, options.scenario, options.fixture_day, handoff)
    elif handoff.get("prefix") != prefix or handoff.get("status") != "recovered":
        raise AdmissionConflict("matching recovery report is required")
    client = client_from_environment()
    try:
        store = restricted_store(client, options.run_id, options.scenario, options.fixture_day)
        if options.phase == "accept":
            accept_phase(store, client, options.run_id, options.scenario, options.fixture_day, emit=emit_event)
        elif options.phase == "resume":
            report = resume_phase(store, client, options.run_id, options.scenario, options.fixture_day,
                                  handoff, emit=emit_event)
        else:
            report = lease_phase(store, options.run_id, options.scenario, options.fixture_day, handoff)
        emit_event({"event": "report", "report": report})
        return report
    finally:
        client.close()


def write_report(path: Path, report):
    path = path.resolve()
    if not path.parent.is_dir():
        raise ValueError("output parent must already exist")
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def main(argv=None):
    options, destination = None, None
    try:
        options = parse_options(argv)
        if options.phase != "all":
            report = run_child(options)
            if options.output:
                write_report(options.output, report)
            return 0
        destination = (options.output or LOG_TEMP / f"p01-batch-recovery-{options.run_id}.json").resolve()
        # Verify output before any remote operation; do not create arbitrary directories.
        write_report(destination, {**BOUNDARIES, "status": "starting"})
        summary = run_all(options, checkpoint=lambda value: write_report(destination, value))
        write_report(destination, summary)
        emit_event({"event": "report", "report": summary})
        return 1 if summary["status"] == "completed_with_unresolved" else 0
    except Exception as error:
        report = {**BOUNDARIES, "status": "unresolved", "errors": [safe_error(error)]}
        if destination is not None:
            try:
                write_report(destination, report)
            except Exception:
                report["errors"].append("safe_report_write_failed")
        emit_event({"event": "error", "report": report})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
