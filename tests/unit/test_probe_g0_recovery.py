from __future__ import annotations

import importlib.util
import json
import socket
import sys
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import probe_g0_recovery as recovery

from news_flash_dedup.admission import AdmissionConflict, AdmissionUnknown, CONTROL_INDEX, REQUEST_INDEX
from news_flash_dedup.batch_admission import HEAD_ID
from news_flash_dedup.batch_es_store import BatchReadError
from test_batch_admission import BatchMemoryStore

DAY = date(2026, 9, 24)
SCENARIOS = (
    "cas-before", "cas-after", "pending-confirmed", "main", "request", "item", "seq",
    "complete-no-shrink", "shrink-ack", "terminal", "lease", "multiscope-midnight", "expiry",
)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("network is forbidden in recovery unit tests")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(recovery, "client_from_environment", forbidden)


class Crash(BaseException):
    def __init__(self, code):
        self.code = code


def crash(code):
    raise Crash(code)


class Indices:
    def __init__(self):
        self.created = {}
        self.refreshed = []
        self.shards = {"total": 2, "successful": 2, "failed": 0}

    def exists(self, *, index):
        return index in self.created

    def create(self, *, index, body):
        self.created[index] = deepcopy(body)
        return {"acknowledged": True, "shards_acknowledged": True, "index": index}

    def refresh(self, *, index):
        self.refreshed.append(index)
        return {"_shards": deepcopy(self.shards)}


class FakeClient:
    def __init__(self):
        self.memory = BatchMemoryStore()
        self.indices = Indices()
        self.closed = False
        self.calls = []

    def info(self):
        return {"version": {"number": "8.99.0-fake"}}

    def get(self, *, index, id, realtime):
        self.calls.append(("get", index, id))
        found = self.memory.get(index, id)
        identity = {"_index": index, "_id": id, "found": found is not None}
        if found is None:
            return identity
        return {**identity, "_source": found["source"], "_seq_no": found["seq_no"],
                "_primary_term": found["primary_term"]}

    def mget(self, *, docs, realtime):
        return {"docs": [self.get(index=d["_index"], id=d["_id"], realtime=True) for d in docs]}

    def index(self, *, index, id, document, refresh, op_type=None,
              if_seq_no=None, if_primary_term=None):
        from elasticsearch import ConflictError
        from elastic_transport import ApiResponseMeta, NodeConfig
        self.calls.append(("index", index, id))
        try:
            if op_type == "create":
                self.memory.create(index, id, document)
            else:
                self.memory.replace(index, id, document, if_seq_no, if_primary_term)
        except Exception as error:
            if self.memory.is_conflict(error):
                raise ConflictError("conflict", ApiResponseMeta(
                    409, "1.1", {}, 0, NodeConfig("http", "localhost", 9200)), {}) from None
            raise
        return {"result": "created"}

    def bulk(self, *, operations, refresh):
        from elasticsearch import ConflictError
        result = []
        for operation, body in zip(operations[::2], operations[1::2]):
            item = operation["create"]
            status = 201
            try:
                self.index(index=item["_index"], id=item["_id"], document=body,
                           op_type="create", refresh=False)
            except ConflictError:
                status = 409
            result.append({"create": {**item, "status": status}})
        return {"items": result}

    def close(self):
        self.closed = True


def setup_case(scenario, memory=False):
    client = FakeClient()
    if memory:
        store = BatchMemoryStore()
    else:
        store = recovery.restricted_store(client, "unit", scenario, DAY)
    events = []
    with pytest.raises(Crash) as caught:
        recovery.accept_phase(store, client, "unit", scenario, DAY,
                              emit=events.append, exit_process=crash)
    assert caught.value.code == recovery.exit_code(scenario)
    handoff = recovery.make_handoff("unit", scenario, DAY, events,
                                    returncode=caught.value.code, isolated_at=10.0)
    return client, store, handoff


@pytest.mark.parametrize("phase", ["all", "accept", "resume", "lease"])
@pytest.mark.parametrize("run_id", ["UPPER", "中文", "a_b", "a/b", "a*", "-bad", "bad-", "a" * 33])
def test_all_phases_reject_invalid_run_ids_before_client(phase, run_id):
    with pytest.raises((ValueError, SystemExit)):
        recovery.parse_options(["--phase", phase, "--scenario", "main", "--run-id", run_id,
                                "--confirm-uat"])


@pytest.mark.parametrize("phase", ["all", "accept", "resume", "lease"])
def test_missing_confirmation_never_creates_client(phase):
    with pytest.raises((ValueError, SystemExit)):
        recovery.parse_options(["--phase", phase, "--scenario", "main", "--run-id", "valid"])


def test_provision_strict_audit_and_exact_fixture_days():
    client = FakeClient()
    prefix = recovery.scenario_prefix("unit", "main")
    recovery.provision(client, prefix, DAY, real_today=DAY + timedelta(days=1))
    assert len(client.indices.created) == 6
    for name, body in client.indices.created.items():
        assert name.startswith("p01-batch-recovery-unit-main-")
        assert "*" not in name and "reviews" not in name
        assert body["mappings"]["dynamic"] == "strict"
        if "audits" in name:
            fields = body["mappings"]["properties"]
            assert set(fields) == {
                "audit_id", "audit_type", "scope_id", "record_id", "candidate_record_id",
                "request_id", "business_date", "pipeline_version", "payload_hash",
                "arrival_seq", "created_at", "expires_at", "payload",
            }
            assert fields["payload"] == {"type": "object", "enabled": False}
    with pytest.raises(RuntimeError):
        recovery.provision(client, prefix, DAY, real_today=DAY + timedelta(days=1))


@pytest.mark.parametrize("prefix", ["p01-g0-unit-", "p01-batch-unit-", "p01-batch-recovery-X-main-",
                                      "p01-batch-recovery-unit-main-*", ""])
def test_provision_rejects_wrong_prefix_without_io(prefix):
    client = FakeClient()
    with pytest.raises(ValueError):
        recovery.provision(client, prefix, DAY, real_today=DAY + timedelta(days=1))
    assert not client.indices.created


@pytest.mark.parametrize("scenario", SCENARIOS)
@pytest.mark.parametrize("memory", [False, True])
def test_fault_state_and_full_recovery(scenario, memory):
    client, store, handoff = setup_case(scenario, memory)
    before = recovery.observe_state(store, scenario, DAY)
    count = 6 if scenario == "multiscope-midnight" else 2
    assert before["allocated"] == (0 if scenario == "cas-before" else count)
    assert before["materialized"] == (count if scenario in {
        "shrink-ack", "terminal", "lease", "multiscope-midnight"} else 0)
    expected_docs = {"main": 1, "request": 2, "item": 3, "seq": 4,
                     "complete-no-shrink": 8, "shrink-ack": 8,
                     "terminal": 8, "lease": 8, "multiscope-midnight": 24}.get(scenario, 0)
    assert before["four_document_parts"] == expected_docs
    assert handoff["accepted_returns"] == (0 if scenario in {"cas-before", "cas-after"} else count)
    report = recovery.resume_phase(store, client, "unit", scenario, DAY, handoff,
                                   monotonic=lambda: 11.0)
    assert report["deployment_fencing_verified"] is False
    assert report["no_real_callback"] and report["no_model"]
    if scenario == "expiry":
        assert report["status"] == "quarantined"
        assert report["rto_seconds"] is None
        assert report["fresh_request_accepted"] is False
        assert report["expired_business_io"] == 0
        assert report["counts"]["expired_quarantined"] == count
        assert recovery.observe_state(store, scenario, DAY)["four_document_parts"] == 0
        return
    assert report["status"] == "recovered"
    assert report["rto_seconds"] == 1.0
    assert report["fresh_request_accepted"] is True
    assert report["counts"]["lost_accepted"] == 0
    assert report["counts"]["unknown_unresolved"] == 0
    assert report["counts"]["four_documents_verified"] == count + 1
    assert report["counts"]["reused_original"] == (0 if scenario == "cas-before" else count)
    assert report["counts"]["initial_unknown"] == (count if scenario == "cas-after" else 0)
    assert report["head"]["allocated"] == report["head"]["materialized"] == count + 1
    for day in report["days"]:
        assert day["decision"] == max(day["sequences"])
        assert day["lexical"] >= max(day["sequences"])
    lease = recovery.lease_phase(store, "unit", scenario, DAY, report)
    assert lease["payload_unchanged"] and lease["backoff_enforced"]
    assert lease["second_generation"] == lease["first_generation"] + 1
    assert lease["attempts"] == 2
    assert lease["state"] == "delivered"
    safe = json.dumps({"resume": report, "lease": lease}, ensure_ascii=False)
    assert "固定恢复正文" not in safe and "callback_body" not in safe


def test_partial_refresh_blocks_terminal_and_watermark():
    client, store, handoff = setup_case("main")
    client.indices.shards = {"total": 2, "successful": 1, "failed": 1}
    with pytest.raises(AdmissionConflict):
        recovery.resume_phase(store, client, "unit", "main", DAY, handoff,
                              monotonic=lambda: 11.0)
    source = store.get(CONTROL_INDEX, "day:alpha:" + DAY.isoformat())["source"]
    assert source["decision_watermark"] == source["lexical_watermark"] == 0


def test_resume_without_observed_expected_exit_does_not_takeover():
    client, store, handoff = setup_case("main")
    handoff["old_process_exited"] = False
    with pytest.raises(AdmissionConflict):
        recovery.resume_phase(store, client, "unit", "main", DAY, handoff)
    assert store.get(CONTROL_INDEX, HEAD_ID)["source"]["owner_id"] == "unit-main-writer-1"


def test_read_unknown_blocks_new_owner_and_new_allocation():
    client, store, handoff = setup_case("cas-after", memory=True)
    original = store.mget
    calls = 0
    def unstable(keys):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("do not log connection values")
        return original(keys)
    store.mget = unstable
    # 窗口 Z3（M-14）：裸 Exception 收紧——实测注入 OSError 原样传播
    # （fail-closed 不接管、不新分配；消息钉值顺带守卫"连接值不入日志"夹具语义）
    with pytest.raises(OSError, match="do not log connection values"):
        recovery.resume_phase(store, client, "unit", "cas-after", DAY, handoff)
    assert store.get(CONTROL_INDEX, HEAD_ID)["source"]["owner_id"] == "unit-cas-after-writer-1"


def test_rto_not_stopped_before_new_request_confirmed(monkeypatch):
    client, store, handoff = setup_case("pending-confirmed")
    stopped = []
    original = recovery.BatchAdmissionCoordinator.accept_batch
    def reject_fresh(self, requests):
        if any(request.request_id == "99-1" for request in requests):
            raise AdmissionConflict("fresh request cannot be accepted")
        return original(self, requests)
    monkeypatch.setattr(recovery.BatchAdmissionCoordinator, "accept_batch", reject_fresh)
    with pytest.raises(AdmissionConflict):
        recovery.resume_phase(store, client, "unit", "pending-confirmed", DAY, handoff,
                              monotonic=lambda: stopped.append(True) or 11.0)
    assert not stopped


@pytest.mark.parametrize("scenario", ["terminal", "lease"])
def test_precrash_payload_digest_is_compared_not_only_new_payload(scenario):
    client, store, handoff = setup_case(scenario)
    assert len(handoff["fault"]["payload_sha256"]) == 64
    report = recovery.resume_phase(store, client, "unit", scenario, DAY, handoff,
                                   monotonic=lambda: 11.0)
    assert report["old_frozen_payload_preserved"] is True
    assert report["old_frozen_payload_sha256"] == handoff["fault"]["payload_sha256"]


def test_day_counts_and_sequence_boundaries_are_separate():
    client, store, handoff = setup_case("multiscope-midnight")
    report = recovery.resume_phase(store, client, "unit", "multiscope-midnight", DAY, handoff,
                                   monotonic=lambda: 11.0)
    assert len(report["days"]) == 4
    assert len(report["day_takeovers"]) == 4
    for entry in report["days"]:
        assert entry["allocated"] == entry["materialized"] == max(entry["sequences"])
        assert entry["allocated_count"] == entry["materialized_count"] == len(entry["sequences"])


@pytest.mark.parametrize("phase", ["accept", "resume", "lease"])
def test_programmatic_child_entry_checks_confirmation_before_any_client(monkeypatch, phase):
    from types import SimpleNamespace
    from io import StringIO
    options = SimpleNamespace(confirm_uat=False, run_id="valid", phase=phase, scenario="main",
                              fixture_day=datetime.now(recovery.BUSINESS_ZONE).date() - timedelta(days=1))
    monkeypatch.setattr(sys, "stdin", StringIO(json.dumps({
        "fresh_namespace_provisioned": True, "prefix": recovery.scenario_prefix("valid", "main")})))
    with pytest.raises(ValueError):
        recovery.run_child(options)


class InProcessPhases:
    """Full phase orchestration with shared FakeClient; not a real process measurement."""
    def __init__(self, client):
        self.client = client
        self.phases = []

    def __call__(self, command, *, handoff, timeout):
        import time
        options = recovery.parse_options(command[3:])
        self.phases.append((options.scenario, options.phase))
        store = recovery.restricted_store(self.client, options.run_id, options.scenario, options.fixture_day)
        events = []
        code = 0
        try:
            if options.phase == "accept":
                recovery.accept_phase(store, self.client, options.run_id, options.scenario, options.fixture_day,
                                      emit=events.append, exit_process=crash)
            elif options.phase == "resume":
                report = recovery.resume_phase(store, self.client, options.run_id, options.scenario,
                                               options.fixture_day, handoff, emit=events.append)
                events.append({"event": "report", "report": report})
            else:
                report = recovery.lease_phase(store, options.run_id, options.scenario, options.fixture_day, handoff)
                events.append({"event": "report", "report": report})
        except Crash as error:
            code = error.code
        except Exception as error:
            events.append({"event": "error", "report": {"errors": [recovery.safe_error(error)]}})
            code = 2
        return {"returncode": code, "exited": True, "timed_out": False, "events": events,
                "errors": [], "observed_exit_at": time.monotonic()}


def options_for_all():
    return recovery.parse_options(["--confirm-uat", "--run-id", "offline-phases", "--scenario", "all"])


def test_all_scenarios_full_fakeclient_orchestration_without_real_client():
    options = options_for_all()
    client = FakeClient()
    runner = InProcessPhases(client)
    snapshots = []
    report = recovery.run_all(options, client_factory=lambda: client, runner=runner,
                              checkpoint=lambda value: snapshots.append(deepcopy(value)))
    assert report["status"] == "fixtures_completed_not_g0"
    assert report["g0_passed"] is False and len(report["scenarios"]) == len(SCENARIOS)
    assert len({case["prefix"] for case in report["scenarios"]}) == len(SCENARIOS)
    assert len(client.indices.created) == 6 * len(SCENARIOS)
    assert len(snapshots) == len(SCENARIOS)
    assert runner.phases == [(case, phase) for case in SCENARIOS
                             for phase in (("accept", "resume") if case == "expiry" else ("accept", "resume", "lease"))]
    assert all(case["old_process_exited"] and not case["errors"] for case in report["scenarios"])
    assert all("*" not in index for _, index, _ in client.calls)


def test_parent_does_not_start_new_owner_after_writer_timeout():
    options = options_for_all()
    options.scenario = "cas-after"
    client = FakeClient()
    calls = []
    def timeout_runner(command, **kwargs):
        calls.append(command)
        return {"returncode": -15, "exited": True, "timed_out": True, "events": [], "errors": []}
    report = recovery.run_all(options, client_factory=lambda: client, runner=timeout_runner)
    case = report["scenarios"][0]
    assert len(calls) == 1 and not case["new_owner_started"]
    assert case["status"] == "unresolved" and case["rto_seconds"] is None
    assert case["counts"]["lost_accepted"] is None
    assert case["counts"]["unknown_unresolved"] is None


def test_partial_terminal_failure_preserves_new_acceptance_rto_evidence():
    options = options_for_all()
    options.scenario = "main"
    client = FakeClient()
    client.indices.shards = {"total": 2, "successful": 1, "failed": 1}
    report = recovery.run_all(options, client_factory=lambda: client, runner=InProcessPhases(client))
    case = report["scenarios"][0]
    assert case["status"] == "unresolved"
    assert case["admission_recovery"]["fresh_request_accepted"] is True
    assert case["rto_seconds"] is not None and "delivery" not in case
    assert case["counts"]["lost_accepted"] == 0


def test_safe_error_report_never_contains_exception_message(monkeypatch, tmp_path, capsys):
    def failed(*args, **kwargs):
        raise OSError("secret-connection-value-must-not-appear")
    monkeypatch.setattr(recovery, "client_from_environment", failed)
    target = tmp_path / "safe.json"
    code = recovery.main(["--confirm-uat", "--scenario", "main", "--run-id", "safe",
                          "--output", str(target)])
    output = target.read_text(encoding="utf-8") + capsys.readouterr().out
    assert code == 1
    assert "secret-connection-value" not in output and "Traceback" not in output
    assert "io_unconfirmed" in output


def test_restricted_store_rejects_other_indices_without_client_io():
    client = FakeClient()
    store = recovery.restricted_store(client, "unit", "main", DAY)
    for index in ["*", "news-dedup-reviews-v1-2026.09.24", "news-dedup-items-v1-2026.09.26",
                  "p01-g0-old-news-dedup-control-v1", "news-dedup-control-v1,*"]:
        # 窗口 Z3（M-14）：裸 Exception 收紧——实测 BatchReadError
        # （batch_es_store.py:10，"get confirmation is unknown"；非法索引名
        # 被受限存储 fail-closed 拒读）
        with pytest.raises(BatchReadError, match="get confirmation is unknown"):
            store.get(index, "x")
        with pytest.raises(AdmissionConflict):
            store.create(index, "x", {})
    assert not client.calls


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_real_local_child_exits_at_own_hook_without_network(scenario):
    command = [sys.executable, "-B", str(Path(__file__).resolve()), "--local-child-probe", scenario]
    result = recovery.execute_child(command, handoff={}, timeout=15)
    assert result["exited"] and not result["timed_out"] and not result["errors"]
    assert result["returncode"] == recovery.exit_code(scenario)
    handoff = recovery.make_handoff("unit", scenario, DAY, result["events"],
                                    returncode=result["returncode"], isolated_at=result["observed_exit_at"])
    assert handoff["old_process_exited"] is True


def test_real_owned_child_timeout_is_reported_and_reaped():
    command = [sys.executable, "-B", str(Path(__file__).resolve()), "--local-child-probe", "block"]
    result = recovery.execute_child(command, handoff={}, timeout=0.1)
    assert result["timed_out"] and result["exited"] and result["returncode"] is not None
    with pytest.raises(AdmissionUnknown):
        recovery.final_report(result)


def test_real_unexpected_child_exit_is_not_valid_isolation():
    command = [sys.executable, "-B", str(Path(__file__).resolve()), "--local-child-probe", "unexpected"]
    result = recovery.execute_child(command, handoff={}, timeout=15)
    assert result["exited"] and result["returncode"] == 3
    with pytest.raises(AdmissionConflict):
        recovery.make_handoff("unit", "main", DAY, result["events"],
                              returncode=result["returncode"], isolated_at=result["observed_exit_at"])


def test_unreaped_child_never_claims_isolation_and_all_waits_are_bounded():
    import subprocess
    class Hung:
        returncode = None
        def __init__(self, *args, **kwargs):
            self.timeouts = []
            self.terminated = self.killed = False
        def communicate(self, *args, timeout):
            self.timeouts.append(timeout)
            raise subprocess.TimeoutExpired("owned", timeout)
        def terminate(self):
            self.terminated = True
        def kill(self):
            self.killed = True
    process = Hung()
    result = recovery.execute_child(["owned"], handoff={}, timeout=0.5, popen=lambda *a, **k: process)
    assert process.timeouts == [0.5, 3, 3]
    assert process.terminated and process.killed
    assert not result["exited"] and "owned_child_exit_unconfirmed" in result["errors"]


def test_old_lease_recovery_advances_exactly_to_lease_then_persisted_due():
    client, store, handoff = setup_case("lease")
    report = recovery.resume_phase(store, client, "unit", "lease", DAY, handoff,
                                   monotonic=lambda: 11.0)
    result = recovery.lease_phase(store, "unit", "lease", DAY, report)
    assert result["simulated_clock_advanced_seconds"] == 3.0
    assert result["persisted_next_delivery_at"] == recovery._utc(
        recovery.resume_time(DAY) + timedelta(seconds=3))


def test_injected_fake_orchestration_is_not_labeled_real_storage_or_process_rto():
    options = options_for_all()
    options.scenario = "main"
    client = FakeClient()
    report = recovery.run_all(options, client_factory=lambda: client, runner=InProcessPhases(client))
    assert report["storage_mode"] == "injected_client_not_real_es_measurement"
    assert report["process_mode"] == "injected_runner_not_process_measurement"
    assert report["no_real_model"] is True


@pytest.mark.parametrize("operation", ["capture", "prepare", "freeze", "takeover", "advance", "claim", "recover", "ack"])
def test_fixed_probe_expiry_interfaces_perform_zero_business_io(operation):
    client, store, handoff = setup_case("pending-confirmed", memory=True)
    clock = recovery.Clock(recovery.start_time(DAY))
    old = recovery.coordinator(store, recovery.owner_id("unit", "pending-confirmed", 1), clock, lambda: True)
    receipt = old.reconcile(recovery.fixture_requests("pending-confirmed", DAY)[0])
    guard = recovery.BusinessIOCounter(store)
    clock.move_to(recovery.timestamp(receipt.expires_at))
    probe = recovery.FixedArtifactProbe(guard, owner_id="new-owner", clock=clock)
    calls = {
        "capture": lambda: probe.capture_visibility(receipt, last_materialized_seq=2),
        "prepare": lambda: probe.prepare_visibility(receipt, snapshot={}, refresh_receipt={}),
        "freeze": lambda: probe.freeze(receipt),
        "takeover": lambda: probe.takeover_day(receipt, previous_owner_id="old-owner", expected_seq_no=0,
                                                expected_primary_term=1, isolation_evidence_ref="local-only"),
        "advance": lambda: probe.advance_watermark(receipt),
        "claim": lambda: probe.claim_delivery(receipt, lease_seconds=2),
        "recover": lambda: probe.recover_delivery(receipt),
        "ack": lambda: probe.ack_delivery(receipt, lease={}),
    }
    with pytest.raises(AdmissionConflict):
        calls[operation]()
    assert guard.business_io == 0


def test_changed_exact_id_snapshot_blocks_takeover_and_new_sequence():
    client, store, handoff = setup_case("request", memory=True)
    original = store.mget
    seen = 0
    def changed(keys):
        nonlocal seen
        seen += 1
        result = original(keys)
        if seen == 2:
            result[1]["source"]["raw_hash"] = "changed"
        return result
    store.mget = changed
    with pytest.raises(AdmissionUnknown):
        recovery.resume_phase(store, client, "unit", "request", DAY, handoff)
    assert store.get(CONTROL_INDEX, HEAD_ID)["source"]["owner_id"] == "unit-request-writer-1"


@pytest.mark.parametrize("offset", [-7, 0, 1])
def test_provision_forbids_out_of_retention_and_future_midnight_pairs(offset):
    client = FakeClient()
    with pytest.raises(ValueError):
        recovery.provision(client, recovery.scenario_prefix("unit", "main"), DAY + timedelta(days=offset),
                           real_today=DAY)
    assert not client.indices.created


@pytest.mark.parametrize("shards", [None, {}, {"total": 0, "successful": 0, "failed": 0},
                                    {"total": True, "successful": True, "failed": 0},
                                    {"total": 2, "successful": 1, "failed": 0},
                                    {"total": 2, "successful": 2, "failed": 1}])
def test_refresh_requires_actual_complete_integer_shard_receipt(shards):
    client, store, handoff = setup_case("pending-confirmed")
    client.indices.shards = shards
    with pytest.raises(AdmissionConflict):
        recovery.resume_phase(store, client, "unit", "pending-confirmed", DAY, handoff,
                              monotonic=lambda: 11.0)
    for (_, key), document in client.memory.docs.items():
        if key.startswith("day:"):
            assert document["source"]["lexical_watermark"] == document["source"]["decision_watermark"] == 0


def test_precrash_payload_digest_mismatch_blocks_fresh_admission():
    client, store, handoff = setup_case("terminal")
    handoff["fault"]["payload_sha256"] = "0" * 64
    with pytest.raises(AdmissionConflict):
        recovery.resume_phase(store, client, "unit", "terminal", DAY, handoff)
    assert store.get(CONTROL_INDEX, HEAD_ID)["source"]["last_allocated_seq"] == 2


def test_first_received_time_mismatch_blocks_new_request():
    client, store, handoff = setup_case("shrink-ack", memory=True)
    key = (recovery.day_index(DAY), recovery._digest(["alpha", "1-1"]))
    store.docs[key]["source"]["received_at"] = recovery._utc(recovery.resume_time(DAY))
    with pytest.raises(AdmissionConflict):
        recovery.resume_phase(store, client, "unit", "shrink-ack", DAY, handoff)
    assert store.get(CONTROL_INDEX, HEAD_ID)["source"]["last_allocated_seq"] == 2


def test_old_head_and_day_owner_are_rejected_after_explicit_takeover():
    client, store, handoff = setup_case("terminal", memory=True)
    recovery.resume_phase(store, client, "unit", "terminal", DAY, handoff, monotonic=lambda: 11.0)
    clock = recovery.Clock(recovery.resume_time(DAY))
    old = recovery.coordinator(store, "unit-terminal-writer-1", clock, lambda: True)
    with pytest.raises(AdmissionConflict):
        old.accept_batch([recovery.fixed_request(100, "alpha", clock())])
    new = recovery.coordinator(store, "unit-terminal-writer-2", clock, lambda: True)
    receipt = new.reconcile(recovery.fixture_requests("terminal", DAY)[0])
    old_probe = recovery.FixedArtifactProbe(store, owner_id=old.owner_id, clock=clock)
    with pytest.raises(AdmissionConflict):
        old_probe.advance_watermark(receipt)


if __name__ == "__main__":
    import os
    import threading
    def forbidden_network(*args, **kwargs):
        raise AssertionError("real client or network forbidden")
    socket.socket.connect = forbidden_network
    socket.create_connection = forbidden_network
    recovery.client_from_environment = forbidden_network
    scenario = sys.argv[-1]
    if scenario == "block":
        threading.Event().wait(30)
        os._exit(4)
    if scenario == "unexpected":
        os._exit(3)
    client = FakeClient()
    store = recovery.restricted_store(client, "unit", scenario, DAY)
    recovery.accept_phase(store, client, "unit", scenario, DAY, emit=recovery.emit_event)
