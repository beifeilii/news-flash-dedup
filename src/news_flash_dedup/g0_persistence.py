"""P01 本地固定工件探针；不是召回/Fact、发送器或部署隔离实现。"""

from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timedelta
import hashlib
import json
from typing import Callable
from uuid import uuid4

from .admission import (
    AdmissionConflict, AdmissionReceipt, AdmissionUnknown, CONTROL_INDEX,
    HEAD_ID, REQUEST_INDEX, Store, _digest, _utc,
)
from .api.contract import ContractError, ResultContext, ack_succeeded, success_callback


_REGISTRATION_KEYS = frozenset({
    "kind", "scope_id", "request_id", "item_id", "record_id", "business_fingerprint",
    "business_date", "arrival_seq", "expires_at", "target_index",
})
_AUDIT_KEYS = frozenset({
    "audit_id", "audit_type", "scope_id", "record_id", "candidate_record_id",
    "request_id", "business_date", "pipeline_version", "payload_hash",
    "arrival_seq", "created_at", "expires_at", "payload",
})
_PAYLOAD_KEYS = frozenset({
    "schema_version", "fixed_fixture", "algorithm_executed", "comparison_count",
    "unresolved", "query_raw_hash", "preparation_generation", "preparation_hash",
    "lexical_watermark", "result",
})
_UNRESOLVED = ["REAL_RECALL_NOT_EXECUTED", "REAL_FACT_NOT_EXECUTED"]


def _hash(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    allow_nan=False, separators=(",", ":")).encode("utf-8")).hexdigest()


def _time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
            raise ValueError("not UTC")
        return parsed
    except (ValueError, TypeError, AttributeError) as error:
        raise AdmissionConflict("invalid UTC timestamp") from error


def _day_index(business_date: str, kind: str = "items") -> str:
    try:
        if date.fromisoformat(business_date).isoformat() != business_date:
            raise ValueError("not canonical date")
    except (TypeError, ValueError) as error:
        raise AdmissionConflict("invalid business date") from error
    return f"news-dedup-{kind}-v1-" + business_date.replace("-", ".")


def _result() -> dict:
    return {"decision": "不重复", "duplicate_ids": [], "reason": "固定工件：无直接重复候选。"}


class FixedArtifactProbe:
    """仅操作Store与注入时钟。单活撤写权必须在本探针之外证明。"""

    def __init__(self, store: Store, *, owner_id: str, clock: Callable[[], datetime]) -> None:
        if not isinstance(owner_id, str) or not owner_id:
            raise AdmissionConflict("explicit owner is required")
        self.store = store
        self.owner_id = owner_id
        self.clock = clock
        self._session = uuid4().hex
        self._captures: dict[str, dict] = {}
        self._prepared: dict[str, dict] = {}

    @staticmethod
    def _index(receipt: AdmissionReceipt) -> str:
        return _day_index(receipt.business_date)

    @staticmethod
    def _day_key(receipt: AdmissionReceipt) -> str:
        return f"day:{receipt.scope_id}:{receipt.business_date}"

    def _live(self, expires_at: str) -> datetime:
        now = self.clock()
        if now >= _time(expires_at):
            raise AdmissionConflict("expired task; business IO/output is forbidden")
        return now

    def _get(self, receipt: AdmissionReceipt, index: str, key: str) -> dict | None:
        # The receipt is trusted admission metadata, not a client-supplied lifetime.
        self._live(receipt.expires_at)
        try:
            found = self.store.get(index, key)
        except Exception as error:
            self._live(receipt.expires_at)
            raise AdmissionUnknown("realtime read is unconfirmed") from error
        self._live(receipt.expires_at)  # Discard output of an IO that crossed expiry.
        return found

    def _cas(self, receipt: AdmissionReceipt, index: str, key: str, current: dict, updated: dict) -> dict:
        self._live(receipt.expires_at)
        try:
            self.store.replace(index, key, updated, current["seq_no"], current["primary_term"])
        except Exception as error:
            self._live(receipt.expires_at)
            if self.store.is_conflict(error):
                raise AdmissionConflict("CAS conflict; revalidate original ownership and snapshot") from error
            # Never read back after expiry, even when a pre-expiry write may have landed.
            found = self._get(receipt, index, key)
            if found is not None and found["source"] == updated:
                return found
            raise AdmissionUnknown("CAS confirmation is unknown; do not send") from error
        found = self._get(receipt, index, key)
        if found is None or found["source"] != updated:
            raise AdmissionUnknown("CAS readback differs; reconcile before proceeding")
        return found

    def _entry(self, receipt: AdmissionReceipt, seq: int) -> dict:
        if type(seq) is not int or seq < 1:
            raise AdmissionConflict("invalid arrival sequence")
        doc = self._get(receipt, REQUEST_INDEX, f"seq:{seq}")
        if doc is None:
            raise AdmissionConflict("sequence registration has an unknown hole")
        entry = doc["source"]
        if (set(entry) != _REGISTRATION_KEYS or entry["kind"] != "seq" or
                type(entry["arrival_seq"]) is not int or entry["arrival_seq"] != seq or
                any(not isinstance(entry[key], str) or not entry[key] for key in
                    _REGISTRATION_KEYS - {"arrival_seq"})):
            raise AdmissionConflict("invalid sequence registration")
        if (entry["target_index"] != _day_index(entry["business_date"]) or
                entry["record_id"] != _digest([entry["scope_id"], entry["item_id"]])):
            raise AdmissionConflict("sequence pointer identity mismatch")
        self._live(entry["expires_at"])
        for kind, key in (
            ("request", "request:" + _digest([entry["scope_id"], entry["request_id"]])),
            ("item", "item:" + entry["record_id"]),
        ):
            mapping = self._get(receipt, REQUEST_INDEX, key)
            if mapping is None or mapping["source"] != {**entry, "kind": kind}:
                raise AdmissionConflict("sequence has no matching immutable identity mappings")
        return entry

    def _registered_main(self, receipt: AdmissionReceipt, entry: dict) -> dict:
        self._live(entry["expires_at"])
        main = self._get(receipt, entry["target_index"], entry["record_id"])
        self._live(entry["expires_at"])
        if main is None:
            raise AdmissionConflict("registered original text is absent")
        source = main["source"]
        keys = ("scope_id", "request_id", "item_id", "record_id", "business_date", "arrival_seq", "expires_at")
        if any(source.get(key) != entry[key] for key in keys):
            raise AdmissionConflict("registration and main identity differ")
        text = source.get("text")
        if (not isinstance(text, str) or
                source.get("raw_hash") != hashlib.sha256(text.encode("utf-8")).hexdigest() or
                entry["business_fingerprint"] != _digest({"item_id": entry["item_id"], "text": text})):
            raise AdmissionConflict("registered original text/hash is unreadable or inconsistent")
        return main

    def _main(self, receipt: AdmissionReceipt) -> dict:
        self._live(receipt.expires_at)
        if _time(receipt.accepted_at) >= _time(receipt.expires_at):
            raise AdmissionConflict("invalid acceptance lifetime")
        self._index(receipt)
        main = self._registered_main(receipt, self._entry(receipt, receipt.arrival_seq))
        source = main["source"]
        keys = ("scope_id", "request_id", "item_id", "record_id", "business_date", "arrival_seq",
                "accepted_at", "expires_at", "schema_version", "pipeline_version", "embedding_space_id")
        if any(source.get(key) != getattr(receipt, key) for key in keys):
            raise AdmissionConflict("receipt and main complete identity/version differ")
        delivery = source.get("diagnostics", {}).get("delivery", {})
        if (not receipt.delivery_route_ref or delivery.get("route_ref") != receipt.delivery_route_ref or
                delivery.get("trace_id") != receipt.trace_id):
            raise AdmissionConflict("first accepted trace or route differs")
        self._live(receipt.expires_at)
        return main

    def _day(self, receipt: AdmissionReceipt, *, require_owner: bool = True) -> dict:
        key = self._day_key(receipt)
        day = self._get(receipt, CONTROL_INDEX, key)
        if day is None:
            body = {
                "kind": "day", "scope_id": receipt.scope_id, "business_date": receipt.business_date,
                "pipeline_version": receipt.pipeline_version, "owner_id": self.owner_id,
                "decision_watermark": 0, "lexical_watermark": 0,
                "updated_at": _utc(self.clock()), "expires_at": receipt.expires_at,
                "checkpoint": {"fixed_fixture_epoch": 0},
            }
            self._live(receipt.expires_at)
            try:
                self.store.create(CONTROL_INDEX, key, body)
            except Exception as error:
                self._live(receipt.expires_at)
                if not self.store.is_conflict(error):
                    raise AdmissionUnknown("day creation is unconfirmed") from error
            day = self._get(receipt, CONTROL_INDEX, key)
        if day is None:
            raise AdmissionUnknown("day control is absent")
        source = day["source"]
        expected = {"kind": "day", "scope_id": receipt.scope_id, "business_date": receipt.business_date,
                    "pipeline_version": receipt.pipeline_version, "expires_at": receipt.expires_at}
        if any(source.get(key) != value for key, value in expected.items()):
            raise AdmissionConflict("day control identity/version differs")
        if require_owner and source.get("owner_id") != self.owner_id:
            raise AdmissionConflict("day control owner differs; explicit takeover required")
        for field in ("decision_watermark", "lexical_watermark"):
            if type(source.get(field)) is not int or source[field] < 0:
                raise AdmissionConflict("day watermarks require fixture migration")
        return day

    def takeover_day(self, receipt: AdmissionReceipt, *, previous_owner_id: str,
                     expected_seq_no: int, expected_primary_term: int,
                     isolation_evidence_ref: str) -> None:
        """外部隔离完成后的本地转移；引用不是隔离证明，也不撤销外部写权限。"""
        self._main(receipt)
        if type(isolation_evidence_ref) is not str or not isolation_evidence_ref.strip():
            raise AdmissionConflict("external isolation evidence reference is required")
        day = self._day(receipt, require_owner=False)
        source = day["source"]
        if (previous_owner_id == self.owner_id or source["owner_id"] != previous_owner_id or
                day["seq_no"] != expected_seq_no or day["primary_term"] != expected_primary_term):
            raise AdmissionConflict("takeover must match the observed previous owner/version")
        checkpoint = {**source["checkpoint"],
                      "fixed_fixture_epoch": source["checkpoint"].get("fixed_fixture_epoch", 0) + 1,
                      "isolation_evidence_ref": isolation_evidence_ref}
        checkpoint.pop("fixed_visibility", None)
        self._cas(receipt, CONTROL_INDEX, self._day_key(receipt), day,
                  {**source, "owner_id": self.owner_id, "lexical_watermark": 0,
                   "checkpoint": checkpoint, "updated_at": _utc(self.clock())})
        self._captures.clear()
        self._prepared.clear()

    def _registrations(self, receipt: AdmissionReceipt, limit: int, *, terminal_before: int = 0) -> str:
        entries, originals = [], []
        # Fixed probe deliberately uses exact GETs, never search-max or cached watermarks.
        for seq in range(1, limit + 1):
            entry = self._entry(receipt, seq)
            entries.append(entry)
            if entry["business_date"] == receipt.business_date:
                source = self._registered_main(receipt, entry)["source"]
                originals.append([entry["record_id"], source["raw_hash"]])
                if (entry["scope_id"] == receipt.scope_id and seq < terminal_before and
                        source.get("task_state") not in {"succeeded", "failed"}):
                    raise AdmissionConflict("earlier same-scope/day task is not terminal")
        return _hash({"registrations": entries, "originals": originals})

    @staticmethod
    def _preparation_lease(source: dict) -> dict:
        lease = source.get("task_lease", {}).get("preparation", {
            "owner_id": None, "generation": 0, "lease_until": None,
        })
        if (set(lease) != {"owner_id", "generation", "lease_until"} or
                type(lease["generation"]) is not int or lease["generation"] < 0):
            raise AdmissionConflict("invalid preparation lease")
        return lease

    @staticmethod
    def _artifacts(source: dict) -> str:
        return _hash({
            "facts": source.get("facts"), "entity_ids": source.get("entity_ids"),
            "fact_type": source.get("fact_type"), "preparation_state": source.get("preparation_state"),
            "lease": source.get("task_lease", {}).get("preparation"),
            "preparation": source.get("diagnostics", {}).get("preparation"),
        })

    def capture_visibility(self, receipt: AdmissionReceipt, *,
                           last_materialized_seq: int | None = None) -> dict:
        """刷新前捕获登记/原文；显式水位须来自持久控制GET，不能来自search最大值。"""
        main = self._main(receipt)
        source = main["source"]
        if source["task_state"] not in {"accepted", "running"}:
            raise AdmissionConflict("only nonterminal tasks can prepare")
        lease = self._preparation_lease(source)
        if lease["owner_id"] is not None:
            raise AdmissionConflict("active preparation owner must finish or be separately recovered")
        day = self._day(receipt)["source"]
        limit = last_materialized_seq
        if limit is None:
            head = self._get(receipt, CONTROL_INDEX, HEAD_ID)
            limit = None if head is None else head["source"].get("last_materialized_seq")
        if type(limit) is not int or limit < receipt.arrival_seq:
            raise AdmissionConflict("materialization snapshot does not cover current record")
        snapshot = {
            "schema_version": "g0-fixed-visibility-v1", "snapshot_id": uuid4().hex,
            "session_id": self._session, "owner_id": self.owner_id,
            "day_epoch": day["checkpoint"].get("fixed_fixture_epoch", 0),
            "record_id": receipt.record_id, "index": self._index(receipt),
            "last_materialized_seq": limit,
            "registrations_hash": self._registrations(receipt, limit),
            "main_seq_no": main["seq_no"], "main_primary_term": main["primary_term"],
            "preparation_generation": lease["generation"], "artifacts_hash": self._artifacts(source),
        }
        self._live(receipt.expires_at)
        self._captures[snapshot["snapshot_id"]] = deepcopy(snapshot)
        return snapshot

    def prepare_visibility(self, receipt: AdmissionReceipt, *, snapshot: dict,
                           refresh_receipt: dict) -> dict:
        """接收刷新回执{index, _shards:{total,successful,failed}}，不自行访问ES。"""
        self._live(receipt.expires_at)
        if (not isinstance(snapshot, dict) or
                self._captures.get(snapshot.get("snapshot_id")) != snapshot or
                snapshot["record_id"] != receipt.record_id):
            raise AdmissionConflict("visibility snapshot is not owned by this process")
        shards = refresh_receipt.get("_shards", {})
        if (set(refresh_receipt) != {"index", "_shards"} or
                refresh_receipt["index"] != snapshot["index"] or
                set(shards) != {"total", "successful", "failed"} or
                any(type(shards[key]) is not int for key in shards) or
                shards["total"] <= 0 or shards["successful"] != shards["total"] or shards["failed"] != 0):
            raise AdmissionConflict("restricted day refresh must confirm every shard")
        main = self._main(receipt)
        source = main["source"]
        if (main["seq_no"] != snapshot["main_seq_no"] or main["primary_term"] != snapshot["main_primary_term"] or
                self._artifacts(source) != snapshot["artifacts_hash"] or
                source["task_state"] not in {"accepted", "running"}):
            raise AdmissionConflict("main/preparation changed since pre-refresh capture")
        day = self._day(receipt)
        state = day["source"]
        if (state["checkpoint"].get("fixed_fixture_epoch", 0) != snapshot["day_epoch"] or
                self._registrations(receipt, snapshot["last_materialized_seq"]) != snapshot["registrations_hash"]):
            raise AdmissionConflict("registration or ownership changed during refresh")
        prepared = {
            "schema_version": "g0-fixed-preparation-v1", "fixed_fixture": True,
            "comparison_count": 0, "generation": snapshot["preparation_generation"] + 1,
            "prepared_at": _utc(self.clock()), "snapshot": deepcopy(snapshot),
        }
        self._cas(receipt, CONTROL_INDEX, self._day_key(receipt), day, {
            **state, "lexical_watermark": snapshot["last_materialized_seq"],
            "checkpoint": {**state["checkpoint"], "fixed_visibility": deepcopy(snapshot)},
            "updated_at": _utc(self.clock()),
        })
        # The day snapshot alone never authorizes freeze; preparation CAS must also be confirmed.
        self._day(receipt)
        self._main(receipt)
        updated = {
            **source, "task_state": "running", "preparation_state": "ready",
            "started_at": source.get("started_at") or _utc(self.clock()),
            "task_lease": {**source.get("task_lease", {}), "preparation": {
                "owner_id": None, "generation": prepared["generation"], "lease_until": None,
            }},
            "diagnostics": {**source["diagnostics"], "preparation": prepared},
        }
        found = self._cas(receipt, self._index(receipt), receipt.record_id, main, updated)
        self._prepared[receipt.record_id] = {
            "payload": deepcopy(prepared), "seq_no": found["seq_no"],
            "primary_term": found["primary_term"], "artifacts_hash": self._artifacts(updated),
        }
        self._live(receipt.expires_at)
        return deepcopy(prepared)

    def _check_prepared(self, receipt: AdmissionReceipt, preparation: dict | None) -> dict:
        saved = self._prepared.get(receipt.record_id)
        if saved is None or preparation != saved["payload"]:
            raise AdmissionConflict("current-process refresh/preparation is required")
        main = self._main(receipt)
        source = main["source"]
        snapshot = preparation["snapshot"]
        day = self._day(receipt)["source"]
        if (source["task_state"] != "running" or source.get("preparation_state") != "ready" or
                main["seq_no"] != saved["seq_no"] or main["primary_term"] != saved["primary_term"] or
                self._artifacts(source) != saved["artifacts_hash"] or
                self._preparation_lease(source)["generation"] != preparation["generation"] or
                day["checkpoint"].get("fixed_visibility") != snapshot or
                day["lexical_watermark"] < snapshot["last_materialized_seq"] or
                day["checkpoint"].get("fixed_fixture_epoch", 0) != snapshot["day_epoch"]):
            raise AdmissionConflict("original version, owner, generation or visibility snapshot changed")
        if self._registrations(receipt, snapshot["last_materialized_seq"],
                               terminal_before=receipt.arrival_seq) != snapshot["registrations_hash"]:
            raise AdmissionConflict("exact registration snapshot changed")
        return main

    def _audit_body(self, receipt: AdmissionReceipt, source: dict, prepared: dict) -> dict:
        payload = {
            "schema_version": "g0-fixed-decision-v1", "fixed_fixture": True,
            "algorithm_executed": False, "comparison_count": 0, "unresolved": list(_UNRESOLVED),
            "query_raw_hash": source["raw_hash"], "preparation_generation": prepared["generation"],
            "preparation_hash": _hash(prepared),
            "lexical_watermark": prepared["snapshot"]["last_materialized_seq"], "result": _result(),
        }
        audit_id = _digest([receipt.record_id, receipt.pipeline_version,
                            "fixed-fixture-decision", prepared["generation"]])
        return {
            "audit_id": audit_id, "audit_type": "decision", "scope_id": receipt.scope_id,
            "record_id": receipt.record_id, "candidate_record_id": None, "request_id": receipt.request_id,
            "business_date": receipt.business_date, "pipeline_version": receipt.pipeline_version,
            "payload_hash": _hash(payload), "arrival_seq": receipt.arrival_seq,
            "created_at": prepared["prepared_at"], "expires_at": receipt.expires_at, "payload": payload,
        }

    def _verify_audit(self, receipt: AdmissionReceipt, expected: dict) -> None:
        doc = self._get(receipt, _day_index(receipt.business_date, "audits"), expected["audit_id"])
        if doc is None:
            raise AdmissionUnknown("required decision audit is not confirmed")
        actual = doc["source"]
        payload = actual.get("payload")
        if (set(actual) != _AUDIT_KEYS or not isinstance(payload, dict) or
                set(payload) != _PAYLOAD_KEYS or payload.get("fixed_fixture") is not True or
                payload.get("algorithm_executed") is not False or
                type(payload.get("comparison_count")) is not int or payload["comparison_count"] != 0 or
                payload.get("unresolved") != _UNRESOLVED or
                actual.get("payload_hash") != _hash(payload) or actual != expected):
            raise AdmissionConflict("same audit ID has different hash/content or invalid fixed schema")

    def _persist_audit(self, receipt: AdmissionReceipt, expected: dict) -> None:
        unknown = None
        self._live(receipt.expires_at)
        try:
            self.store.create(_day_index(receipt.business_date, "audits"), expected["audit_id"], expected)
        except Exception as error:
            self._live(receipt.expires_at)
            if not self.store.is_conflict(error):
                unknown = error
        self._verify_audit(receipt, expected)
        if unknown is not None:
            # Deliberately require a verified replay after an ambiguous create acknowledgement.
            raise AdmissionUnknown("audit create acknowledgement unknown; retry verification") from unknown

    @staticmethod
    def _callback(receipt: AdmissionReceipt, source: dict, completed: str):
        context = ResultContext(receipt.item_id, source["text"], receipt.record_id, receipt.scope_id,
                                receipt.business_date, receipt.arrival_seq, receipt.pipeline_version)
        try:
            return success_callback(
                {"item_id": receipt.item_id, "text": source["text"], **_result()}, context,
                lambda _: None, request_id=receipt.request_id, trace_id=receipt.trace_id,
                completed_time=completed,
            )
        except ContractError as error:
            raise AdmissionConflict("fixed callback does not satisfy existing P06 contract") from error

    def _frozen(self, receipt: AdmissionReceipt, source: dict) -> None:
        self._live(receipt.expires_at)
        try:
            if source["task_state"] != "succeeded" or source["result"] != _result():
                raise AdmissionConflict("not a supported immutable fixture terminal")
            prepared = source["diagnostics"]["preparation"]
            audit = self._audit_body(receipt, source, prepared)
            callback = self._callback(receipt, source, source["completed_at"])
            deadline = min(_time(source["completed_at"]) + timedelta(hours=24), _time(receipt.expires_at))
            if (source["audit_ids"] != [audit["audit_id"]] or source["audit_complete"] is not True or
                    type(source["result_version"]) is not int or source["result_version"] != 1 or
                    source["event_id"] != _digest([receipt.scope_id, receipt.request_id, 1]) or
                    source["callback_body"] != callback.body.decode("utf-8") or
                    source["diagnostics"]["delivery"]["payload_hash"] != callback.sha256 or
                    source["delivery_deadline_at"] != _utc(deadline)):
                raise AdmissionConflict("frozen result/outbox identity or payload differs")
            self._verify_audit(receipt, audit)
        except (KeyError, TypeError) as error:
            raise AdmissionConflict("incomplete fixed terminal; old probe requires migration") from error
        self._live(receipt.expires_at)

    def _release_preparation(self, receipt: AdmissionReceipt) -> None:
        self._prepared.pop(receipt.record_id, None)
        for key in [key for key, snapshot in self._captures.items()
                    if snapshot["record_id"] == receipt.record_id]:
            del self._captures[key]

    def freeze(self, receipt: AdmissionReceipt, *, preparation: dict | None = None) -> dict:
        main = self._main(receipt)
        if main["source"]["task_state"] == "succeeded":
            self._frozen(receipt, main["source"])
            self._release_preparation(receipt)
            return deepcopy(main["source"]["result"])
        main = self._check_prepared(receipt, preparation)
        source = main["source"]
        audit = self._audit_body(receipt, source, preparation)
        self._persist_audit(receipt, audit)
        # Audit IO may have crossed an expiry, ownership change or preparation generation change.
        main = self._check_prepared(receipt, preparation)
        source = main["source"]
        completed = self.clock()
        if completed >= _time(receipt.expires_at):
            raise AdmissionConflict("expired before terminal CAS")
        callback = self._callback(receipt, source, _utc(completed))
        deadline = min(completed + timedelta(hours=24), _time(receipt.expires_at))
        updated = {
            **source, "task_state": "succeeded", "delivery_state": "pending", "result": _result(),
            "result_version": 1, "completed_at": _utc(completed), "audit_ids": [audit["audit_id"]],
            "audit_complete": True, "callback_body": callback.body.decode("utf-8"), "callback_attempts": 0,
            "next_delivery_at": _utc(completed), "delivery_deadline_at": _utc(deadline),
            "event_id": _digest([receipt.scope_id, receipt.request_id, 1]),
            "delivery_lease": {"owner_id": None, "generation": 0, "lease_until": None,
                               "attempt_id": None, "round": 1, "result_version": 1,
                               "event_id": _digest([receipt.scope_id, receipt.request_id, 1]),
                               "payload_hash": callback.sha256, "route_ref": receipt.delivery_route_ref},
            "task_lease": {**source.get("task_lease", {}), "preparation": {
                "owner_id": None, "generation": preparation["generation"] + 1, "lease_until": None,
            }},
            # Legal dictionary code for this no-candidate fixture, not an algorithm conclusion.
            "diagnostics": {**source["diagnostics"], "reason_code": "NO_DUPLICATE_FOUND",
                            "decision": {"schema_version": "g0-fixed-decision-ref-v1", "fixed_fixture": True,
                                         "algorithm_executed": False,
                                         "audit_id": audit["audit_id"], "unresolved": list(_UNRESOLVED)},
                            "delivery": {**source["diagnostics"]["delivery"], "payload_hash": callback.sha256,
                                         "round": 1, "round_attempts": 0}},
        }
        found = self._cas(receipt, self._index(receipt), receipt.record_id, main, updated)
        self._frozen(receipt, found["source"])
        self._release_preparation(receipt)
        return deepcopy(found["source"]["result"])

    def advance_watermark(self, receipt: AdmissionReceipt) -> int:
        self._main(receipt)
        day = self._day(receipt)
        source = day["source"]
        # Never trust a high stored watermark to excuse an unexplained earlier registration hole.
        self._registrations(receipt, receipt.arrival_seq, terminal_before=receipt.arrival_seq + 1)
        self._live(receipt.expires_at)
        if source["decision_watermark"] >= receipt.arrival_seq:
            return source["decision_watermark"]
        self._day(receipt)
        self._cas(receipt, CONTROL_INDEX, self._day_key(receipt), day,
                  {**source, "decision_watermark": receipt.arrival_seq, "updated_at": _utc(self.clock())})
        return receipt.arrival_seq

    def _delivery(self, receipt: AdmissionReceipt) -> dict:
        main = self._main(receipt)
        self._frozen(receipt, main["source"])
        source = main["source"]
        delivery = source["diagnostics"]["delivery"]
        lease = source["delivery_lease"]
        for number in (source["callback_attempts"], delivery["round_attempts"], lease["generation"]):
            if type(number) is not int or number < 0:
                raise AdmissionConflict("invalid persisted delivery count/generation")
        if (type(delivery["round"]) is not int or delivery["round"] != 1 or
                source["callback_attempts"] != delivery["round_attempts"] or
                delivery["round_attempts"] > 12):
            raise AdmissionConflict("only the original fixed delivery round is supported")
        identity = {
            "round": delivery["round"], "result_version": source["result_version"],
            "event_id": source["event_id"], "payload_hash": delivery["payload_hash"],
            "route_ref": delivery["route_ref"],
        }
        expected_attempt = None if lease["generation"] == 0 else _digest([source["event_id"], lease["generation"]])
        if (set(lease) != {*identity, "owner_id", "generation", "lease_until", "attempt_id"} or
                any(type(lease[key]) is not type(value) or lease[key] != value for key, value in identity.items()) or
                lease["attempt_id"] != expected_attempt or
                lease["generation"] != source["callback_attempts"]):
            raise AdmissionConflict("persisted lease differs from frozen identity/attempt counters")
        if source["delivery_state"] == "delivering":
            if (not isinstance(lease["owner_id"], str) or not lease["owner_id"] or
                    lease["generation"] == 0 or _time(lease["lease_until"]) > self._deadline(source)):
                raise AdmissionConflict("persisted active lease is invalid or exceeds deadline")
        elif lease["owner_id"] is not None or lease["lease_until"] is not None:
            raise AdmissionConflict("released delivery state still has an active owner")
        return main

    @staticmethod
    def _deadline(source: dict) -> datetime:
        return min(_time(source["delivery_deadline_at"]), _time(source["expires_at"]))

    def _settle(self, receipt: AdmissionReceipt, main: dict, *, outcome: str,
                held_lease: dict | None = None) -> str:
        source = main["source"]
        now = self._live(receipt.expires_at)
        if held_lease is not None and now >= min(_time(held_lease["lease_until"]), self._deadline(source)):
            raise AdmissionConflict("ACK lease expired before state CAS; recover instead")
        delivery = source["diagnostics"]["delivery"]
        due = None
        if now >= self._deadline(source):
            state = "expired"
        elif outcome == "confirmed":
            state = "delivered"
        elif delivery["round_attempts"] >= 12:
            state = "exhausted"
        else:
            state = "pending"
            # Fixed fixture uses zero jitter (within the allowed 0..20%), persisted only once.
            next_time = now + timedelta(seconds=min(2 ** max(delivery["round_attempts"] - 1, 0), 1800))
            if next_time < self._deadline(source):
                due = _utc(next_time)
        updated = {
            **source, "delivery_state": state, "next_delivery_at": due,
            "delivery_lease": {**source["delivery_lease"], "owner_id": None, "lease_until": None},
            "diagnostics": {**source["diagnostics"], "delivery": {
                **delivery, "last_outcome": {"state": state, "at": _utc(now), "confirmation": outcome},
            }},
        }
        self._cas(receipt, self._index(receipt), receipt.record_id, main, updated)
        return state

    def claim_delivery(self, receipt: AdmissionReceipt, *, lease_seconds: int) -> dict:
        if type(lease_seconds) is not int or lease_seconds <= 0:
            raise AdmissionConflict("positive explicit lease duration required")
        main = self._delivery(receipt)
        source = main["source"]
        if source["delivery_state"] != "pending":
            raise AdmissionConflict("callback intent is not pending")
        now = self._live(receipt.expires_at)
        delivery = source["diagnostics"]["delivery"]
        if now >= self._deadline(source) or delivery["round_attempts"] >= 12:
            self._settle(receipt, main, outcome="unconfirmed")
            raise AdmissionConflict("callback deadline or 12-attempt budget reached")
        if source["next_delivery_at"] is None or now < _time(source["next_delivery_at"]):
            raise AdmissionConflict("callback backoff has not elapsed or is beyond deadline")
        generation = source["delivery_lease"]["generation"] + 1
        lease = {
            "owner_id": self.owner_id, "generation": generation,
            "lease_until": _utc(min(now + timedelta(seconds=lease_seconds), self._deadline(source))),
            "attempt_id": _digest([source["event_id"], generation]), "round": delivery["round"],
            "result_version": source["result_version"], "event_id": source["event_id"],
            "payload_hash": delivery["payload_hash"], "route_ref": delivery["route_ref"],
        }
        self._cas(receipt, self._index(receipt), receipt.record_id, main, {
            **source, "delivery_state": "delivering", "delivery_lease": lease, "next_delivery_at": None,
            "callback_attempts": source["callback_attempts"] + 1,
            "diagnostics": {**source["diagnostics"], "delivery": {
                **delivery, "round_attempts": delivery["round_attempts"] + 1,
                "last_outcome": {"state": "delivering", "at": _utc(now), "confirmation": "unconfirmed"},
            }},
        })
        return deepcopy(lease)

    def recover_delivery(self, receipt: AdmissionReceipt) -> str:
        """仅有效数据的投递恢复；数据到期标记属于尚未实现的P08最小元数据任务。"""
        main = self._delivery(receipt)
        source = main["source"]
        state = source["delivery_state"]
        now = self._live(receipt.expires_at)
        if state in {"delivered", "expired"}:
            return state
        if now >= self._deadline(source):
            return self._settle(receipt, main, outcome="unconfirmed")
        if state != "delivering":
            return state
        if now < _time(source["delivery_lease"]["lease_until"]):
            raise AdmissionConflict("delivery lease is still live")
        return self._settle(receipt, main, outcome="unconfirmed")

    def ack_delivery(self, receipt: AdmissionReceipt, *, lease: dict,
                     http_status: int | None = None, response_body: bytes | None = None) -> str:
        """只记录本地模拟收件结果；无网络发送，也不收养重读后的新owner/代次。"""
        main = self._delivery(receipt)
        source = main["source"]
        if (source["delivery_state"] != "delivering" or lease != source["delivery_lease"] or
                lease.get("owner_id") != self.owner_id):
            raise AdmissionConflict("late or mismatched owner/generation/attempt result")
        if self.clock() >= min(_time(lease["lease_until"]), self._deadline(source)):
            raise AdmissionConflict("late result after lease or fixed deadline")
        success = ack_succeeded(http_status, response_body)
        outcome = "confirmed" if success else "unconfirmed" if http_status is None else "rejected"
        return self._settle(receipt, main, outcome=outcome, held_lease=lease)
