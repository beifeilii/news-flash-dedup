"""隔离 G0 原型：有界多条受理日志、批次 CAS 与异步物化。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from datetime import datetime, time, timedelta, timezone
from threading import RLock
from typing import Callable, Literal, Protocol

from .admission import (
    AdmissionConflict, AdmissionReceipt, AdmissionRequest, AdmissionUnknown,
    BUSINESS_ZONE, CONTROL_INDEX, REQUEST_INDEX, _digest, _receipt, _utc,
    IdentityConflict,
)
from .admission_schema import PendingAdmissionV1


HEAD_ID = "batch_admission_head"
LOG_VERSION = "batch-v1"
MATERIALIZE_PREFIX_BATCHES = 4
MATERIALIZE_PREFIX_ITEMS = 32
# W2 批41（F7 常量化，原魔法数 4）：每条目物化四文档=主记录+request/item/seq
# 三映射，与 _documents() 的 documents.extend 结构一一对应；改结构必须同步本常量。
_DOCUMENTS_PER_ENTRY = 4


class AdmissionCapacityExceeded(AdmissionConflict):
    """本次整批受理 CAS 前容量不足；不否认其中已有身份的受理事实。

    适配层须先于 AdmissionConflict 捕获并映射429，而非身份冲突409；
    单条 batch_byte_limit 可按既有配置映射413。不得用于写入未知。
    """

    def __init__(self, reason: Literal["log_item_limit", "batch_byte_limit", "log_byte_limit"]) -> None:
        if reason not in ("log_item_limit", "batch_byte_limit", "log_byte_limit"):
            raise ValueError("invalid batch capacity reason")
        self.reason = reason
        super().__init__("batch admission capacity exceeded: " + reason)


class BatchStore(Protocol):
    def get(self, index: str, key: str) -> dict | None: ...
    def mget(self, keys: list[tuple[str, str]]) -> list[dict | None]: ...
    def create(self, index: str, key: str, body: dict) -> None: ...
    def replace(self, index: str, key: str, body: dict, seq_no: int, primary_term: int) -> None: ...
    def bulk_create(self, documents: list[tuple[str, str, dict]]) -> None: ...
    def is_conflict(self, error: Exception) -> bool: ...


@dataclass(frozen=True)
class BatchLimits:
    max_batch_items: int = 8
    max_batch_bytes: int = 512 * 1024
    max_log_items: int = 64
    max_log_bytes: int = 4 * 1024 * 1024

    def __post_init__(self) -> None:
        if min(self.max_batch_items, self.max_batch_bytes,
               self.max_log_items, self.max_log_bytes) <= 0:
            raise ValueError("batch limits must be positive")


def _bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def _batch_digest(entries: list[dict]) -> str:
    return hashlib.sha256(_bytes(entries)).hexdigest()


def _keys(scope_id: str, request_id: str, item_id: str) -> tuple[str, str, str]:
    record_id = _digest([scope_id, item_id])
    return record_id, "request:" + _digest([scope_id, request_id]), "item:" + record_id


def pending_registration(source: dict, arrival_seq: int) -> tuple[str, str] | None:
    """登记证明查询面（W-R4 最小加法，09 §3.4「只有登记证明属于其他域/日
    才可跳过；缺号未知须恢复，不能猜不存在」）：头文档 pending 受理日志内
    给定全局序号的 (scope_id, business_date) 登记归属。

    只读查询面：不改动全局编号/物化语义（编号分配、前缀收缩、_validate_log
    不变量逐字节不动）。未登记、日志版本/形态不识别、条目身份残缺 → None
    （调用方 fail-closed，不猜不推）。
    """
    if not isinstance(source, dict) or type(arrival_seq) is not int:
        return None
    pending = source.get("pending")
    if not isinstance(pending, dict) or pending.get("version") != LOG_VERSION:
        return None
    batches = pending.get("batches")
    if not isinstance(batches, list):
        return None
    for batch in batches:
        entries = batch.get("entries") if isinstance(batch, dict) else None
        if not isinstance(entries, list):
            return None
        for entry in entries:
            if not isinstance(entry, dict):
                return None
            if entry.get("arrival_seq") != arrival_seq:
                continue
            scope_id = entry.get("scope_id")
            business_date = entry.get("business_date")
            if (isinstance(scope_id, str) and scope_id and
                    isinstance(business_date, str) and business_date):
                return scope_id, business_date
            return None
    return None


class BatchAdmissionCoordinator:
    def __init__(
        self, store: BatchStore, *, owner_id: str,
        owner_isolated: Callable[[], bool], limits: BatchLimits,
        clock: Callable[[], datetime],
    ) -> None:
        self.store = store
        self.owner_id = owner_id
        self.owner_isolated = owner_isolated
        self.limits = limits
        self.clock = clock
        self._lock = RLock()
        self._unknown_cas = False

    def _active(self) -> None:
        if not self.owner_isolated():
            raise AdmissionConflict("old writer isolation not confirmed")
        if self._unknown_cas:
            raise AdmissionConflict("unknown batch CAS blocks further sequence allocation")

    def _read(self, index: str, key: str) -> dict | None:
        try:
            return self.store.get(index, key)
        except Exception as error:
            raise AdmissionUnknown("realtime document read is unconfirmed") from error

    def _mget(self, keys: list[tuple[str, str]]) -> list[dict | None]:
        try:
            found = self.store.mget(keys)
        except Exception as error:
            raise AdmissionUnknown("realtime mget is unconfirmed") from error
        if not isinstance(found, list) or len(found) != len(keys):
            raise AdmissionUnknown("realtime mget is incomplete")
        return found

    @staticmethod
    def _timestamp(value: str) -> datetime:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None:
            raise ValueError("timestamp must be timezone aware")
        return result

    @classmethod
    def _validate_log(cls, source: dict) -> None:
        # enabled=false 日志必须恢复时重新校验，不能只信写入时的 Schema。
        try:
            allocated = source["last_allocated_seq"]
            materialized = source["last_materialized_seq"]
            if (source["kind"] != "admission" or
                    not isinstance(source["owner_id"], str) or not source["owner_id"] or
                    type(allocated) is not int or type(materialized) is not int or
                    not 0 <= materialized <= allocated <= (1 << 53) - 1 or
                    not isinstance(source.get("checkpoint", {}), dict)):
                raise ValueError("invalid head")
            pending = source["pending"]
            if (set(pending) != {"version", "batches"} or pending["version"] != LOG_VERSION or
                    not isinstance(pending["batches"], list)):
                raise ValueError("invalid pending")
            sequence = materialized
            batch_ids: set[str] = set()
            requests: set[tuple[str, str]] = set()
            items: set[tuple[str, str]] = set()
            for batch in pending["batches"]:
                if set(batch) != {"batch_id", "digest", "entries"}:
                    raise ValueError("invalid batch envelope")
                batch_id = batch["batch_id"]
                if (not isinstance(batch_id, str) or len(batch_id) != 64 or
                        any(char not in "0123456789abcdef" for char in batch_id) or
                        batch_id in batch_ids):
                    raise ValueError("invalid batch identity")
                batch_ids.add(batch_id)
                entries = batch["entries"]
                if not isinstance(entries, list) or not entries or _batch_digest(entries) != batch["digest"]:
                    raise ValueError("invalid batch digest")
                for entry in entries:
                    PendingAdmissionV1.model_validate(entry)
                    sequence += 1
                    request_key = (entry["scope_id"], entry["request_id"])
                    item_key = (entry["scope_id"], entry["item_id"])
                    accepted = cls._timestamp(entry["accepted_at"])
                    cls._timestamp(entry["received_at"])
                    day = accepted.astimezone(BUSINESS_ZONE).date()
                    expiry = datetime.combine(day + timedelta(days=7), time.min, BUSINESS_ZONE)
                    if (entry["arrival_seq"] != sequence or request_key in requests or item_key in items or
                            entry["record_id"] != _digest([entry["scope_id"], entry["item_id"]]) or
                            entry["business_fingerprint"] != _digest({
                                "item_id": entry["item_id"], "text": entry["text"]}) or
                            entry["business_date"] != day.isoformat() or
                            cls._timestamp(entry["expires_at"]) != expiry):
                        raise ValueError("invalid pending identity or sequence")
                    requests.add(request_key)
                    items.add(item_key)
            if sequence != allocated:
                raise ValueError("invalid sequence boundary")
        except (KeyError, TypeError, ValueError, AttributeError, OverflowError) as error:
            raise AdmissionConflict("batch log validation failed") from error

    @staticmethod
    def _unquarantined(head: dict) -> None:
        if head["source"].get("checkpoint", {}).get("batch_quarantine"):
            raise AdmissionConflict("batch log is quarantined")

    def _head(self) -> dict:
        head = self._read(CONTROL_INDEX, HEAD_ID)
        if head is None:
            body = {
                "kind": "admission", "owner_id": self.owner_id,
                "last_allocated_seq": 0, "last_materialized_seq": 0,
                "pending": {"version": LOG_VERSION, "batches": []},
                "checkpoint": {}, "updated_at": _utc(self.clock()),
            }
            try:
                self.store.create(CONTROL_INDEX, HEAD_ID, body)
            except Exception as error:
                if not self.store.is_conflict(error):
                    self._unknown_cas = True
                    raise AdmissionUnknown("batch head creation unconfirmed") from error
            head = self._read(CONTROL_INDEX, HEAD_ID)
        if head is None:
            raise AdmissionUnknown("batch head is absent after creation")
        source = head["source"]
        self._validate_log(source)
        if source["owner_id"] != self.owner_id:
            raise AdmissionConflict("batch admission owner differs")
        return head

    def _live(self, expires_at: str) -> None:
        try:
            expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        except (AttributeError, ValueError) as error:
            raise AdmissionConflict("invalid logical expiry") from error
        if expiry.tzinfo is None or self.clock() >= expiry:
            raise AdmissionConflict("task has passed its logical expiry")

    def _quarantine_if_expired(self, batch: dict) -> None:
        if any(self.clock() >= self._timestamp(entry["expires_at"]) for entry in batch["entries"]):
            self._quarantine(batch, "logical_expiry")

    def _quarantine(self, batch: dict, reason: str) -> None:
        for _ in range(8):
            if not self.owner_isolated():
                raise AdmissionConflict("old writer isolation not confirmed")
            head = self._head()
            queued = head["source"]["pending"]["batches"]
            if not any(current == batch for current in queued):
                raise AdmissionConflict("batch changed before quarantine")
            self._unquarantined(head)
            checkpoint = head["source"].get("checkpoint", {})
            quarantine = {
                "batch_id": batch["batch_id"], "digest": batch["digest"],
                "first_seq": batch["entries"][0]["arrival_seq"],
                "last_seq": batch["entries"][-1]["arrival_seq"],
                "reason": reason, "recorded_at": _utc(self.clock()),
            }
            updated = {**head["source"],
                       "checkpoint": {**checkpoint, "batch_quarantine": quarantine},
                       "updated_at": _utc(self.clock())}
            try:
                self.store.replace(CONTROL_INDEX, HEAD_ID, updated,
                                   head["seq_no"], head["primary_term"])
            except Exception as error:
                if self.store.is_conflict(error):
                    continue
                self._unknown_cas = True
                raise AdmissionUnknown("batch quarantine confirmation is unknown") from error
            message = "expired batch is quarantined" if reason == "logical_expiry" else "batch is quarantined"
            raise AdmissionConflict(message)
        raise AdmissionConflict("batch quarantine CAS contention exceeded retry bound")

    def release_quarantine(self, *, operator: str, disposition: str,
                           expected_batch_id: str | None = None) -> dict:
        """W-A 族③(a)（A1-007）：quarantine 解除通道（10 号文 L459 批面形态）。

        三面齐：权限面（operator 署名强制+owner_isolated 单活证明+点名
        expected_batch_id 防误清）｜条件面（disposition 二态闭集——drain=
        过期隔离规范通道：清槽 pending 头批、last_materialized_seq 推进至
        批末序（保留单调序号、allocated 不动、过期正文不重新物化，唯一
        写入=控制文档 CAS）+无正文 lifecycle_cleanup 检查点；retry=非
        过期原因根因已除：仅清旗保留批次，logical_expiry 闸死拒）｜审计
        落账面（checkpoint.quarantine_release=署名/处置/时刻/被解除记录
        全快照，返回值供运维日志外发）。CAS 8 次有界重试（:553-578 同
        先例）；非冲突异常 _unknown_cas=True+AdmissionUnknown（:277-278
        同纪律）。
        """
        if not isinstance(operator, str) or not operator:
            raise ValueError("operator must be a non-empty str")
        if disposition not in ("drain", "retry"):
            raise ValueError(
                f"disposition must be 'drain' or 'retry'; got {disposition!r}")
        with self._lock:
            for _ in range(8):
                if not self.owner_isolated():
                    raise AdmissionConflict("old writer isolation not confirmed")
                head = self._head()
                source = head["source"]
                checkpoint = source.get("checkpoint", {})
                quarantine = checkpoint.get("batch_quarantine")
                if not quarantine:
                    raise AdmissionConflict("no batch quarantine to release")
                if (expected_batch_id is not None
                        and expected_batch_id != quarantine["batch_id"]):
                    raise AdmissionConflict(
                        "quarantined batch differs from named batch")
                now = _utc(self.clock())
                new_checkpoint = {key: value for key, value in checkpoint.items()
                                  if key != "batch_quarantine"}
                if disposition == "drain":
                    batches = source["pending"]["batches"]
                    if (not batches
                            or batches[0]["batch_id"] != quarantine["batch_id"]):
                        raise AdmissionConflict(
                            "quarantined batch is not the pending head")
                    if (source["last_materialized_seq"]
                            != quarantine["first_seq"] - 1):
                        raise AdmissionConflict(
                            "materialized sequence prefix is not contiguous")
                    new_checkpoint["lifecycle_cleanup"] = {
                        "kind": "quarantine_drain",
                        "batch_id": quarantine["batch_id"],
                        "digest": quarantine["digest"],
                        "first_seq": quarantine["first_seq"],
                        "last_seq": quarantine["last_seq"],
                        "cleaned_at": now, "operator": operator,
                    }
                    new_pending = {"version": LOG_VERSION,
                                   "batches": list(batches[1:])}
                    new_materialized = quarantine["last_seq"]
                else:
                    if quarantine["reason"] == "logical_expiry":
                        raise AdmissionConflict(
                            "expired batch must be drained, not retried")
                    new_pending = source["pending"]
                    new_materialized = source["last_materialized_seq"]
                release = {
                    "operator": operator, "disposition": disposition,
                    "released_at": now, "quarantine": dict(quarantine),
                }
                new_checkpoint["quarantine_release"] = release
                updated = {**source, "pending": new_pending,
                           "last_materialized_seq": new_materialized,
                           "checkpoint": new_checkpoint, "updated_at": now}
                try:
                    self.store.replace(CONTROL_INDEX, HEAD_ID, updated,
                                       head["seq_no"], head["primary_term"])
                except Exception as error:
                    if self.store.is_conflict(error):
                        continue
                    self._unknown_cas = True
                    raise AdmissionUnknown(
                        "release quarantine confirmation is unknown") from error
                return release
            raise AdmissionConflict(
                "release quarantine CAS contention exceeded retry bound")

    def _matches(self, request: AdmissionRequest, entry: dict) -> AdmissionReceipt:
        self._live(entry["expires_at"])
        if (entry["scope_id"] != request.scope_id or
                entry["request_id"] != request.request_id or
                entry["item_id"] != request.item_id or
                entry["business_fingerprint"] != _digest({
                    "item_id": request.item_id, "text": request.text
                }) or entry["text"] != request.text):
            raise IdentityConflict("request or item identity already binds other content")
        # W2 修复波 2 (a)-7（A1-010）：__dict__ 展开→dataclasses.replace
        # （与 admission.py:196 同型；逐字段语义等价——receipt_reuse 卫测钉）。
        return replace(_receipt(entry), reused=True)

    def _from_mappings(self, request: AdmissionRequest, request_map: dict, item_map: dict) -> AdmissionReceipt:
        try:
            return self._checked_mappings(request, request_map, item_map)
        except IdentityConflict:
            raise
        except (KeyError, TypeError, ValueError, AttributeError, OverflowError) as error:
            raise AdmissionConflict("materialized registration is invalid") from error

    def _checked_mappings(self, request: AdmissionRequest, request_map: dict, item_map: dict) -> AdmissionReceipt:
        source = request_map["source"]
        self._live(source["expires_at"])
        if source != {**item_map["source"], "kind": "request"}:
            raise AdmissionConflict("request and item registration disagree")
        record_id, _, _ = _keys(request.scope_id, request.request_id, request.item_id)
        target = "news-dedup-items-v1-" + source["business_date"].replace("-", ".")
        if (source.get("kind") != "request" or item_map["source"].get("kind") != "item" or
                source["record_id"] != record_id or source["target_index"] != target):
            raise AdmissionConflict("materialized registration target differs")
        if (source["scope_id"] != request.scope_id or source["request_id"] != request.request_id or
                source["item_id"] != request.item_id or
                source["business_fingerprint"] != _digest({
                    "item_id": request.item_id, "text": request.text
                })):
            raise IdentityConflict("materialized identity or text differs")
        if type(source["arrival_seq"]) is not int or not 1 <= source["arrival_seq"] <= (1 << 53) - 1:
            raise AdmissionConflict("sequence registration is invalid")
        seq_map = self._read(REQUEST_INDEX, "seq:" + str(source["arrival_seq"]))
        if (seq_map is None or seq_map["source"].get("kind") != "seq" or
                source != {**seq_map["source"], "kind": "request"}):
            raise AdmissionConflict("sequence registration is absent or inconsistent")
        main = self._read(source["target_index"], source["record_id"])
        if main is None:
            raise AdmissionConflict("materialized mapping has no main record")
        item = main["source"]
        if any(item.get(key) != source[key] for key in (
            "scope_id", "request_id", "item_id", "record_id", "business_date",
            "arrival_seq", "expires_at",
        )) or item.get("text") != request.text:
            raise AdmissionConflict("materialized main identity differs")
        if item.get("raw_hash") != hashlib.sha256(request.text.encode("utf-8")).hexdigest():
            raise AdmissionConflict("materialized raw hash differs")
        if not all(isinstance(item.get(key), str) and item[key] for key in (
            "schema_version", "pipeline_version", "embedding_space_id", "accepted_at", "received_at",
        )):
            raise AdmissionConflict("materialized version or time is invalid")
        delivery = item.get("diagnostics", {}).get("delivery", {})
        if not isinstance(delivery, dict):
            raise AdmissionConflict("frozen delivery diagnostics are invalid")
        route = delivery.get("route_ref")
        trace_id = delivery.get("trace_id")
        if (not isinstance(route, str) or not route or
                (trace_id is not None and not isinstance(trace_id, str))):
            raise AdmissionConflict("frozen delivery route or trace is invalid")
        accepted = self._timestamp(item["accepted_at"])
        self._timestamp(item["received_at"])
        day = accepted.astimezone(BUSINESS_ZONE).date()
        expiry = datetime.combine(day + timedelta(days=7), time.min, BUSINESS_ZONE)
        if source["business_date"] != day.isoformat() or self._timestamp(source["expires_at"]) != expiry:
            raise AdmissionConflict("materialized date or expiry differs")
        self._live(source["expires_at"])
        return AdmissionReceipt(
            source["scope_id"], source["request_id"], source["item_id"],
            source["record_id"], source["arrival_seq"], source["business_date"],
            item["accepted_at"], source["expires_at"], item["schema_version"],
            item["pipeline_version"], item["embedding_space_id"],
            route, trace_id=trace_id, reused=True,
        )

    def accept_batch(self, requests: list[AdmissionRequest]) -> list[AdmissionReceipt]:
        if not requests or len(requests) > self.limits.max_batch_items:
            raise AdmissionConflict("batch item limit exceeded")
        with self._lock:
            self._active()
            for _ in range(8):
                head = self._head()
                self._unquarantined(head)
                batches = head["source"]["pending"]["batches"]
                for batch in batches:
                    self._quarantine_if_expired(batch)
                outstanding = [entry for batch in batches for entry in batch["entries"]]
                by_request = {(entry["scope_id"], entry["request_id"]): entry
                              for entry in outstanding}
                by_item = {(entry["scope_id"], entry["item_id"]): entry
                           for entry in outstanding}
                key_pairs = [_keys(r.scope_id, r.request_id, r.item_id) for r in requests]
                lookup_keys = [(REQUEST_INDEX, key) for _, req_key, item_key in key_pairs
                               for key in (req_key, item_key)]
                mappings = self._mget(lookup_keys)
                results: list[AdmissionReceipt] = []
                new_entries: list[dict] = []
                accepted_at = self.clock()
                day = accepted_at.astimezone(BUSINESS_ZONE).date()
                expires_at = datetime.combine(day + timedelta(days=7), time.min, BUSINESS_ZONE)
                for offset, request in enumerate(requests):
                    record_id, _, _ = key_pairs[offset]
                    req_map, item_map = mappings[2 * offset:2 * offset + 2]
                    req_pending = by_request.get((request.scope_id, request.request_id))
                    item_pending = by_item.get((request.scope_id, request.item_id))
                    if req_pending is not None or item_pending is not None:
                        if req_pending is None or item_pending is None or req_pending != item_pending:
                            raise IdentityConflict("request/item pending binding differs")
                        results.append(self._matches(request, req_pending))
                        continue
                    if req_map is not None and (
                        req_map["source"]["request_id"] != request.request_id or
                        req_map["source"]["item_id"] != request.item_id
                    ):
                        raise IdentityConflict("request identity binds another item")
                    if item_map is not None and (
                        item_map["source"]["item_id"] != request.item_id or
                        item_map["source"]["request_id"] != request.request_id
                    ):
                        raise IdentityConflict("item identity binds another request")
                    if req_map is not None or item_map is not None:
                        if req_map is None or item_map is None:
                            raise AdmissionConflict("materialized registration is incomplete")
                        results.append(self._from_mappings(request, req_map, item_map))
                        continue
                    entry = PendingAdmissionV1.model_validate({
                        "scope_id": request.scope_id,
                        "request_id": request.request_id, "item_id": request.item_id,
                        "record_id": record_id,
                        "business_fingerprint": _digest({
                            "item_id": request.item_id, "text": request.text
                        }),
                        "text": request.text, "delivery_route_ref": request.delivery_route_ref,
                        "trace_id": request.trace_id, "received_at": _utc(request.received_at),
                        "accepted_at": _utc(accepted_at), "business_date": day.isoformat(),
                        "expires_at": _utc(expires_at),
                        "schema_version": request.schema_version,
                        "pipeline_version": request.pipeline_version,
                        "embedding_space_id": request.embedding_space_id,
                        "arrival_seq": head["source"]["last_allocated_seq"] + len(new_entries) + 1,
                    }).model_dump()
                    by_request[(request.scope_id, request.request_id)] = entry
                    by_item[(request.scope_id, request.item_id)] = entry
                    new_entries.append(entry)
                    results.append(_receipt(entry))
                self._active()
                for batch in batches:
                    self._quarantine_if_expired(batch)
                for receipt in results:
                    self._live(receipt.expires_at)
                if not new_entries:
                    return results
                if len(outstanding) + len(new_entries) > self.limits.max_log_items:
                    raise AdmissionCapacityExceeded("log_item_limit")
                if len(_bytes(new_entries)) > self.limits.max_batch_bytes:
                    raise AdmissionCapacityExceeded("batch_byte_limit")
                envelope = {
                    "batch_id": _digest([self.owner_id, new_entries[0]["arrival_seq"]]),
                    "digest": _batch_digest(new_entries), "entries": new_entries,
                }
                proposed = [*batches, envelope]
                if len(_bytes(proposed)) > self.limits.max_log_bytes:
                    raise AdmissionCapacityExceeded("log_byte_limit")
                updated = {
                    **head["source"],
                    "last_allocated_seq": new_entries[-1]["arrival_seq"],
                    "pending": {"version": LOG_VERSION, "batches": proposed},
                    "updated_at": _utc(self.clock()),
                }
                try:
                    self.store.replace(CONTROL_INDEX, HEAD_ID, updated,
                                       head["seq_no"], head["primary_term"])
                except Exception as error:
                    if self.store.is_conflict(error):
                        # CAS 冲突意味着 head 已被人改写，必须重读。
                        self._active()
                        continue
                    self._unknown_cas = True
                    raise AdmissionUnknown("batch CAS confirmation is unknown") from error
                # CAS 已确认；出站到期只否认完整输出，不撤销新条目的受理事实。
                try:
                    for receipt in results:
                        self._live(receipt.expires_at)
                except AdmissionConflict as error:
                    raise AdmissionUnknown("batch CAS confirmed but receipt expiry prevents complete response") from error
                return results
            raise AdmissionConflict("batch CAS contention exceeded retry bound")

    # 形状单源对：admission.py:344-357 单槽物化 `main` 同构双份
    # （A1-009 在案）——改动任一处必须同步另一处 +
    # test_wave2_doc_shape_parity 卫测（W2 修复波 2 (a)-5；注：设计稿
    # 拟注释于 def 行上方，本窗置于 @staticmethod 上方以贴件头）。
    @staticmethod
    def _documents(entries: list[dict]) -> list[tuple[str, str, dict]]:
        documents: list[tuple[str, str, dict]] = []
        for entry in entries:
            target = "news-dedup-items-v1-" + entry["business_date"].replace("-", ".")
            main = {key: entry[key] for key in (
                "scope_id", "request_id", "item_id", "record_id", "schema_version",
                "pipeline_version", "embedding_space_id", "business_date", "arrival_seq",
                "received_at", "accepted_at", "expires_at", "text",
            )}
            main.update({
                "raw_hash": hashlib.sha256(entry["text"].encode("utf-8")).hexdigest(),
                "task_state": "accepted", "delivery_state": "not_ready", "result": None,
                "diagnostics": {"delivery": {
                    "route_ref": entry["delivery_route_ref"], "trace_id": entry["trace_id"],
                }},
            })
            common = {key: entry[key] for key in (
                "scope_id", "request_id", "item_id", "record_id", "business_fingerprint",
                "business_date", "arrival_seq", "expires_at",
            )}
            common["target_index"] = target
            _, request_key, item_key = _keys(entry["scope_id"], entry["request_id"], entry["item_id"])
            documents.extend([
                (target, entry["record_id"], main),
                (REQUEST_INDEX, request_key, {"kind": "request", **common}),
                (REQUEST_INDEX, item_key, {"kind": "item", **common}),
                (REQUEST_INDEX, "seq:" + str(entry["arrival_seq"]), {"kind": "seq", **common}),
            ])
        return documents

    def materialize_oldest(self) -> int:
        from .batch_es_store import PermanentBulkError

        with self._lock:
            if not self.owner_isolated():
                raise AdmissionConflict("old writer isolation not confirmed")
            head = self._head()
            self._unquarantined(head)
            batches = head["source"]["pending"]["batches"]
            if not batches:
                return head["source"]["last_materialized_seq"]
            oldest = batches[0]
            entries = oldest["entries"]
            self._quarantine_if_expired(oldest)
            documents = self._documents(entries)
            self._unquarantined(self._head())
            if not self.owner_isolated():
                raise AdmissionConflict("old writer isolation not confirmed")
            # 构造文档和网络往返都可能跨零点；每次业务写前及读回后重新过门。
            self._quarantine_if_expired(oldest)
            try:
                self.store.bulk_create(documents)
            except Exception as error:
                self._quarantine_if_expired(oldest)
                if isinstance(error, PermanentBulkError):
                    self._quarantine(oldest, "permanent_bulk_error")
                raise AdmissionUnknown("bulk materialization confirmation is unknown") from error
            self._quarantine_if_expired(oldest)
            try:
                found = self._mget([(index, key) for index, key, _ in documents])
            finally:
                self._quarantine_if_expired(oldest)
            for (_, _, expected), actual in zip(documents, found):
                if actual is None:
                    raise AdmissionUnknown("materialized document is not yet confirmed")
                actual_source = actual["source"]
                if any(actual_source.get(key) != value for key, value in expected.items()
                       if key not in {"task_state", "delivery_state", "result", "diagnostics"}):
                    self._quarantine(oldest, "deterministic_content_conflict")
                if "diagnostics" in expected:
                    expected_delivery = expected["diagnostics"]["delivery"]
                    diagnostics = actual_source.get("diagnostics")
                    delivery = diagnostics.get("delivery") if isinstance(diagnostics, dict) else None
                    if not isinstance(delivery, dict) or any(
                        delivery.get(key) != value for key, value in expected_delivery.items()
                    ):
                        self._quarantine(oldest, "deterministic_content_conflict")
            for _ in range(8):
                latest = self._head()
                self._unquarantined(latest)
                queued = latest["source"]["pending"]["batches"]
                if not queued or queued[0] != oldest:
                    raise AdmissionConflict("oldest batch changed before prefix shrink")
                if latest["source"]["last_materialized_seq"] != entries[0]["arrival_seq"] - 1:
                    raise AdmissionConflict("materialized sequence prefix is not contiguous")
                updated = {
                    **latest["source"],
                    "pending": {"version": LOG_VERSION, "batches": queued[1:]},
                    "last_materialized_seq": entries[-1]["arrival_seq"],
                    "updated_at": _utc(self.clock()),
                }
                if not self.owner_isolated():
                    raise AdmissionConflict("old writer isolation not confirmed")
                self._quarantine_if_expired(oldest)
                try:
                    self.store.replace(CONTROL_INDEX, HEAD_ID, updated,
                                       latest["seq_no"], latest["primary_term"])
                except Exception as error:
                    if self.store.is_conflict(error):
                        continue
                    raise AdmissionUnknown("prefix shrink CAS confirmation is unknown") from error
                return entries[-1]["arrival_seq"]
            raise AdmissionConflict("prefix shrink CAS contention exceeded retry bound")

    def _confirmed_prefix_head(self, selected: list[dict], before_seq: int) -> dict:
        head = self._head()
        self._unquarantined(head)
        queued = head["source"]["pending"]["batches"]
        if (queued[:len(selected)] != selected or
                head["source"]["last_materialized_seq"] != before_seq):
            raise AdmissionConflict("materialization prefix changed before confirmation")
        return head

    def materialize_prefix(self) -> int:
        """有界地物化连续完整批次；确认全部四文档后一次 CAS 收缩。"""
        from .batch_es_store import PermanentBulkError

        with self._lock:
            if not self.owner_isolated():
                raise AdmissionConflict("old writer isolation not confirmed")
            head = self._head()
            self._unquarantined(head)
            batches = head["source"]["pending"]["batches"]
            if not batches:
                return head["source"]["last_materialized_seq"]
            selected: list[dict] = []
            entries: list[dict] = []
            entry_batches: list[dict] = []
            item_limit = max(MATERIALIZE_PREFIX_ITEMS, self.limits.max_batch_items)
            for batch in batches[:MATERIALIZE_PREFIX_BATCHES]:
                batch_entries = batch["entries"]
                if selected and len(entries) + len(batch_entries) > item_limit:
                    break
                selected.append(batch)
                entries.extend(batch_entries)
                entry_batches.extend([batch] * len(batch_entries))
            before_seq = entries[0]["arrival_seq"] - 1
            for batch in selected:
                self._quarantine_if_expired(batch)
            documents = self._documents(entries)
            self._confirmed_prefix_head(selected, before_seq)
            if not self.owner_isolated():
                raise AdmissionConflict("old writer isolation not confirmed")
            for batch in selected:
                self._quarantine_if_expired(batch)
            try:
                self.store.bulk_create(documents)
            except Exception as error:
                for batch in selected:
                    self._quarantine_if_expired(batch)
                if isinstance(error, PermanentBulkError):
                    positions = error.failed_document_positions
                    first = positions[0] if positions else None
                    located = type(first) is int and 0 <= first < len(documents)
                    culprit = entry_batches[first // _DOCUMENTS_PER_ENTRY] if located else selected[0]
                    reason = "permanent_bulk_error" if located else "unattributed_permanent_bulk_error"
                    self._quarantine(culprit, reason)
                raise AdmissionUnknown("bulk materialization confirmation is unknown") from error
            for batch in selected:
                self._quarantine_if_expired(batch)
            try:
                found = self._mget([(index, key) for index, key, _ in documents])
            finally:
                for batch in selected:
                    self._quarantine_if_expired(batch)
            for position, ((_, _, expected), actual) in enumerate(zip(documents, found)):
                if actual is None or not isinstance(actual, dict) or not isinstance(actual.get("source"), dict):
                    raise AdmissionUnknown("materialized document is not yet confirmed")
                actual_source = actual["source"]
                culprit = entry_batches[position // _DOCUMENTS_PER_ENTRY]
                if any(actual_source.get(key) != value for key, value in expected.items()
                       if key not in {"task_state", "delivery_state", "result", "diagnostics"}):
                    self._quarantine(culprit, "deterministic_content_conflict")
                if "diagnostics" in expected:
                    expected_delivery = expected["diagnostics"]["delivery"]
                    diagnostics = actual_source.get("diagnostics")
                    delivery = diagnostics.get("delivery") if isinstance(diagnostics, dict) else None
                    if not isinstance(delivery, dict) or any(
                        delivery.get(key) != value for key, value in expected_delivery.items()
                    ):
                        self._quarantine(culprit, "deterministic_content_conflict")
            for _ in range(8):
                latest = self._confirmed_prefix_head(selected, before_seq)
                queued = latest["source"]["pending"]["batches"]
                updated = {
                    **latest["source"],
                    "pending": {"version": LOG_VERSION, "batches": queued[len(selected):]},
                    "last_materialized_seq": entries[-1]["arrival_seq"],
                    "updated_at": _utc(self.clock()),
                }
                if not self.owner_isolated():
                    raise AdmissionConflict("old writer isolation not confirmed")
                for batch in selected:
                    self._quarantine_if_expired(batch)
                try:
                    self.store.replace(CONTROL_INDEX, HEAD_ID, updated,
                                       latest["seq_no"], latest["primary_term"])
                except Exception as error:
                    if self.store.is_conflict(error):
                        continue
                    raise AdmissionUnknown("prefix shrink CAS confirmation is unknown") from error
                return entries[-1]["arrival_seq"]
            raise AdmissionConflict("prefix shrink CAS contention exceeded retry bound")

    def recover(self) -> int:
        with self._lock:
            if not self.owner_isolated():
                raise AdmissionConflict("old writer isolation not confirmed")
            while True:
                head = self._head()
                self._unquarantined(head)
                if not head["source"]["pending"]["batches"]:
                    return head["source"]["last_materialized_seq"]
                self.materialize_oldest()

    def reconcile(self, request: AdmissionRequest) -> AdmissionReceipt:
        """实时读取 request→pending→request 恢复受理；全未命中不证明不存在。"""
        with self._lock:
            _, request_key, item_key = _keys(request.scope_id, request.request_id, request.item_id)
            request_map = self._read(REQUEST_INDEX, request_key)
            head = self._head()
            self._unquarantined(head)
            try:
                for batch in head["source"]["pending"]["batches"]:
                    for entry in batch["entries"]:
                        if entry["scope_id"] == request.scope_id and (
                            entry["request_id"] == request.request_id or entry["item_id"] == request.item_id
                        ):
                            self._quarantine_if_expired(batch)
                            return self._matches(request, entry)
                if request_map is None:
                    request_map = self._read(REQUEST_INDEX, request_key)
                item_map = self._read(REQUEST_INDEX, item_key)
                if request_map is None:
                    if item_map is not None:
                        raise AdmissionConflict("item identity already has a registration")
                    # 本对象的布尔值不证明所有写入者及其在途 CAS 已排空。
                    raise AdmissionUnknown("recovery lookup has insufficient evidence to prove absence")
                if item_map is None:
                    raise AdmissionConflict("materialized item registration is absent")
                return self._from_mappings(request, request_map, item_map)
            except IdentityConflict as error:
                raise AdmissionConflict("reconciliation identity binding differs") from error

    def takeover(self) -> int:
        with self._lock:
            if not self.owner_isolated():
                raise AdmissionConflict("old writer isolation not confirmed")
            for _ in range(8):
                head = self._read(CONTROL_INDEX, HEAD_ID)
                if head is None:
                    return self.recover()
                self._validate_log(head["source"])
                if head["source"]["owner_id"] == self.owner_id:
                    self._unknown_cas = False
                    return self.recover()
                updated = {**head["source"], "owner_id": self.owner_id,
                           "updated_at": _utc(self.clock())}
                try:
                    self.store.replace(CONTROL_INDEX, HEAD_ID, updated,
                                       head["seq_no"], head["primary_term"])
                except Exception as error:
                    if self.store.is_conflict(error):
                        continue
                    raise AdmissionUnknown("batch ownership transfer is unknown") from error
                self._unknown_cas = False
                return self.recover()
            raise AdmissionConflict("batch takeover CAS contention exceeded retry bound")
