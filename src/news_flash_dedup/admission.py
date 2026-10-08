"""ES 单槽受理与可重放物化。

W2 批40 标注（WA2-F4）：本模块为 G0 单槽受理 **bench 对照原型**，与
batch_admission/batch_es_store 侧存在**纪律代际差**（fail-closed 包装、保链、
身份/shards 校验强度以批次侧为新一代尺）——非产品路径，保留原型形态不升格修；
产品入口为 P07-C CollectorAdmissionPort × 批次协调器。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from datetime import datetime, time, timedelta, timezone
from threading import RLock
from typing import Callable, Protocol
from zoneinfo import ZoneInfo

from .admission_schema import DeliveryAdmissionV1, PendingAdmissionV1


CONTROL_INDEX = "news-dedup-control-v1"
REQUEST_INDEX = "news-dedup-requests-v1"
HEAD_ID = "admission_head"
BUSINESS_ZONE = ZoneInfo("Asia/Shanghai")


class IdentityConflict(ValueError):
    """同一业务身份被用于不同原文或不同绑定。"""


class AdmissionConflict(RuntimeError):
    """单活隔离或持久状态不能证明安全受理。"""


class AdmissionUnknown(RuntimeError):
    """写入确认丢失；调用方须以同身份重试或内部对账。"""


class Store(Protocol):
    def get(self, index: str, key: str) -> dict | None: ...

    def create(self, index: str, key: str, body: dict) -> None: ...

    def replace(self, index: str, key: str, body: dict, seq_no: int, primary_term: int) -> None: ...

    def is_conflict(self, error: Exception) -> bool: ...


@dataclass(frozen=True)
class AdmissionRequest:
    scope_id: str
    request_id: str
    item_id: str
    text: str
    received_at: datetime
    schema_version: str
    pipeline_version: str
    embedding_space_id: str
    delivery_route_ref: str
    trace_id: str | None = None


@dataclass(frozen=True)
class AdmissionReceipt:
    scope_id: str
    request_id: str
    item_id: str
    record_id: str
    arrival_seq: int
    business_date: str
    accepted_at: str
    expires_at: str
    schema_version: str
    pipeline_version: str
    embedding_space_id: str
    delivery_route_ref: str
    trace_id: str | None = None
    reused: bool = field(default=False, compare=False)


def _jcs(value: object) -> bytes:
    # 当前确定键只含字符串；拒绝代理码位，避免跨语言编码分歧。
    def validate(part: object) -> None:
        if isinstance(part, str):
            if any(0xD800 <= ord(char) <= 0xDFFF for char in part):
                raise ValueError("identity contains surrogate code point")
        elif isinstance(part, list):
            for member in part:
                validate(member)
        elif isinstance(part, dict):
            for key, member in part.items():
                validate(key)
                validate(member)
        elif type(part) is int and 0 <= part <= (1 << 53) - 1:
            pass
        else:
            raise TypeError("identity JCS permits strings and safe nonnegative integers only")

    validate(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _digest(value: object) -> str:
    return hashlib.sha256(_jcs(value)).hexdigest()


def _utc(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("timestamp must be timezone aware")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _receipt(pending: dict) -> AdmissionReceipt:
    PendingAdmissionV1.model_validate(pending)
    return AdmissionReceipt(**{
        key: pending[key] for key in (
            "scope_id", "request_id", "item_id", "record_id", "arrival_seq",
            "business_date", "accepted_at", "expires_at", "schema_version",
            "pipeline_version", "embedding_space_id", "delivery_route_ref",
        )
    }, trace_id=pending.get("trace_id"))


class AdmissionCoordinator:
    def __init__(
        self,
        store: Store,
        *,
        owner_id: str,
        owner_isolated: Callable[[], bool],
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.store = store
        self.owner_id = owner_id
        self.owner_isolated = owner_isolated
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = RLock()

    def _active(self) -> None:
        # CAS 只保护单文档；旧实例写权限隔离须由部署层证明。
        if not self.owner_isolated():
            raise AdmissionConflict("old writer isolation is not confirmed")

    def _live(self, expires_at: str) -> None:
        expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        if self.clock() >= expiry:
            raise AdmissionConflict("task has passed its logical expiry")

    def _head(self) -> dict:
        head = self.store.get(CONTROL_INDEX, HEAD_ID)
        if head is not None:
            if head["source"]["owner_id"] != self.owner_id:
                raise AdmissionConflict("admission owner differs")
            return head
        body = {
            "kind": "admission", "owner_id": self.owner_id,
            "last_allocated_seq": 0, "last_materialized_seq": 0,
            "updated_at": _utc(self.clock()), "pending": None, "checkpoint": {},
        }
        try:
            self.store.create(CONTROL_INDEX, HEAD_ID, body)
        except Exception as error:
            if not self.store.is_conflict(error):
                raise AdmissionUnknown("admission head creation is unknown") from error
        head = self.store.get(CONTROL_INDEX, HEAD_ID)
        if head is None:
            raise AdmissionUnknown("admission head creation is unconfirmed")
        if head["source"]["owner_id"] != self.owner_id:
            raise AdmissionConflict("admission owner differs")
        return head

    def _head_readonly(self) -> dict | None:
        """W-A 族⑤(d)（A1-001）：lookup 纯读径专用——无创建、无 owner 闸。

        10 号文 L262 三次实时 GET 协议只要求"读"；读路径抢建 head 会产生
        写副作用且 owner_id 归属可能错误（本实例不一定是指定写者）。
        """
        return self.store.get(CONTROL_INDEX, HEAD_ID)

    @staticmethod
    def _keys(scope_id: str, request_id: str, item_id: str) -> tuple[str, str, str]:
        record_id = _digest([scope_id, item_id])
        return record_id, "request:" + _digest([scope_id, request_id]), "item:" + record_id

    def _match(self, request: AdmissionRequest, pending: dict) -> AdmissionReceipt:
        self._live(pending["expires_at"])
        fingerprint = _digest({"item_id": request.item_id, "text": request.text})
        if (pending["scope_id"] != request.scope_id or
                pending["request_id"] != request.request_id or
                pending["item_id"] != request.item_id or
                pending["business_fingerprint"] != fingerprint or
                pending["text"] != request.text):
            raise IdentityConflict("request or item identity already binds another text")
        # W2 修复波 2 (a)-6（A1-004）：__dict__ 展开→dataclasses.replace
        # （构造器契约正解，逐字段语义等价——receipt_reuse 卫测钉）。
        return replace(_receipt(pending), reused=True)

    def _from_mapping(self, request: AdmissionRequest, mapping: dict) -> AdmissionReceipt:
        source = mapping["source"]
        self._live(source["expires_at"])
        if source["scope_id"] != request.scope_id:
            raise AdmissionConflict("mapping scope mismatch")
        if source["request_id"] != request.request_id or source["item_id"] != request.item_id:
            raise IdentityConflict("request or item identity already exists")
        if source["business_fingerprint"] != _digest({"item_id": request.item_id, "text": request.text}):
            raise IdentityConflict("same identity has different original text")
        main = self.store.get(source["target_index"], source["record_id"])
        if main is None:
            raise AdmissionConflict("mapping points to absent main record")
        actual = main["source"]
        immutable = (
            "scope_id", "request_id", "item_id", "record_id", "business_date",
            "arrival_seq", "expires_at",
        )
        if any(actual.get(key) != source[key] for key in immutable):
            raise AdmissionConflict("mapping and main record differ")
        if actual.get("text") != request.text:
            raise AdmissionConflict("mapping and main text differ")
        if not all(isinstance(actual.get(key), str) and actual[key] for key in (
            "schema_version", "pipeline_version", "embedding_space_id", "accepted_at",
        )):
            raise AdmissionConflict("materialized version or time is invalid")
        if actual.get("raw_hash") != hashlib.sha256(request.text.encode("utf-8")).hexdigest():
            raise AdmissionConflict("materialized raw hash differs")
        delivery = actual.get("diagnostics", {}).get("delivery", {})
        route = delivery.get("route_ref")
        if not isinstance(route, str) or not route:
            raise AdmissionConflict("frozen delivery route is absent")
        return AdmissionReceipt(
            request.scope_id, request.request_id, request.item_id, source["record_id"],
            source["arrival_seq"], source["business_date"], actual["accepted_at"],
            source["expires_at"], actual["schema_version"], actual["pipeline_version"],
            actual["embedding_space_id"], route,
            trace_id=delivery.get("trace_id"), reused=True,
        )

    def _lookup_request(self, request: AdmissionRequest) -> AdmissionReceipt | None:
        record_id, request_key, item_key = self._keys(request.scope_id, request.request_id, request.item_id)
        request_map = self.store.get(REQUEST_INDEX, request_key)
        item_map = self.store.get(REQUEST_INDEX, item_key)
        if request_map is None and item_map is None:
            return None
        if request_map is not None and (
            request_map["source"].get("request_id") != request.request_id or
            request_map["source"].get("item_id") != request.item_id
        ):
            raise IdentityConflict("request identity binds another item")
        if item_map is not None and (
            item_map["source"].get("item_id") != request.item_id or
            item_map["source"].get("request_id") != request.request_id
        ):
            raise IdentityConflict("item identity binds another request")
        if request_map is None or item_map is None:
            raise AdmissionConflict("identity mappings are incomplete")
        if request_map["source"]["record_id"] != record_id:
            raise IdentityConflict("request identity binds another item")
        if request_map["source"] != {**item_map["source"], "kind": "request"}:
            raise AdmissionConflict("request and item mappings differ")
        seq_key = "seq:" + str(request_map["source"]["arrival_seq"])
        sequence_map = self.store.get(REQUEST_INDEX, seq_key)
        if sequence_map is None or request_map["source"] != {
            **sequence_map["source"], "kind": "request"
        }:
            raise AdmissionConflict("sequence registration is absent or inconsistent")
        return self._from_mapping(request, request_map)

    def accept(self, request: AdmissionRequest, *, materialize: bool = True) -> AdmissionReceipt:
        with self._lock:
            self._active()
            for _ in range(8):
                head = self._head()
                pending = head["source"]["pending"]
                if pending is not None:
                    if pending["scope_id"] == request.scope_id and (
                        pending["request_id"] == request.request_id or pending["item_id"] == request.item_id
                    ):
                        receipt = self._match(request, pending)
                        if materialize:
                            self._recover_head(head)
                        return receipt
                    self._recover_head(head)
                    continue
                existing = self._lookup_request(request)
                if existing is not None:
                    return existing
                now = self.clock()
                day = now.astimezone(BUSINESS_ZONE).date()
                expiry = datetime.combine(day + timedelta(days=7), time.min, BUSINESS_ZONE)
                record_id, _, _ = self._keys(request.scope_id, request.request_id, request.item_id)
                pending = {
                    "scope_id": request.scope_id, "request_id": request.request_id,
                    "item_id": request.item_id, "record_id": record_id,
                    "business_fingerprint": _digest({"item_id": request.item_id, "text": request.text}),
                    "text": request.text, "delivery_route_ref": request.delivery_route_ref,
                    "trace_id": request.trace_id,
                    "received_at": _utc(request.received_at), "accepted_at": _utc(now),
                    "business_date": day.isoformat(), "expires_at": _utc(expiry),
                    "schema_version": request.schema_version,
                    "pipeline_version": request.pipeline_version,
                    "embedding_space_id": request.embedding_space_id,
                    "arrival_seq": head["source"]["last_allocated_seq"] + 1,
                }
                pending = PendingAdmissionV1.model_validate(pending).model_dump()
                updated = {**head["source"], "last_allocated_seq": pending["arrival_seq"],
                           "pending": pending, "updated_at": _utc(now)}
                try:
                    self.store.replace(CONTROL_INDEX, HEAD_ID, updated, head["seq_no"], head["primary_term"])
                except Exception as error:
                    if self.store.is_conflict(error):
                        self._active()
                        continue
                    raise AdmissionUnknown("pending CAS confirmation is unknown") from error
                receipt = _receipt(pending)
                if materialize:
                    self.recover()
                return receipt
            raise AdmissionConflict("admission CAS contention exceeds retry bound")

    def _create_verified(self, index: str, key: str, body: dict, expected: dict) -> None:
        try:
            self.store.create(index, key, body)
        except Exception as error:
            if not self.store.is_conflict(error):
                raise AdmissionUnknown("materialization write confirmation is unknown") from error
        found = self.store.get(index, key)
        if found is None:
            raise AdmissionUnknown("materialization readback is absent")
        if any(found["source"].get(name) != value for name, value in expected.items()):
            raise AdmissionConflict("determinate ID has conflicting content")

    def _recover_head(self, head: dict) -> None:
        pending = head["source"]["pending"]
        if pending is None:
            return
        PendingAdmissionV1.model_validate(pending)
        self._active()
        self._live(pending["expires_at"])
        record_id, request_key, item_key = self._keys(
            pending["scope_id"], pending["request_id"], pending["item_id"]
        )
        if record_id != pending["record_id"]:
            raise AdmissionConflict("pending record ID mismatch")
        target = "news-dedup-items-v1-" + pending["business_date"].replace("-", ".")
        # 形状单源对：batch_admission.py:561-589 `_documents` 同构双份
        # （A1-009 在案）——改动任一处必须同步另一处 +
        # test_wave2_doc_shape_parity 卫测（W2 修复波 2 (a)-5）。
        main = {
            key: pending[key] for key in (
                "scope_id", "request_id", "item_id", "record_id", "schema_version",
                "pipeline_version", "embedding_space_id", "business_date", "arrival_seq",
                "received_at", "accepted_at", "expires_at", "text",
            )
        }
        delivery = DeliveryAdmissionV1.model_validate({
            "route_ref": pending["delivery_route_ref"], "trace_id": pending["trace_id"]
        }).model_dump()
        main.update({"raw_hash": hashlib.sha256(pending["text"].encode("utf-8")).hexdigest(),
                     "task_state": "accepted", "delivery_state": "not_ready", "result": None,
                     "diagnostics": {"delivery": delivery}})
        common = {key: pending[key] for key in (
            "scope_id", "request_id", "item_id", "record_id", "business_fingerprint",
            "business_date", "arrival_seq", "expires_at",
        )}
        common["target_index"] = target
        self._create_verified(target, record_id, main, {
            key: main[key] for key in (
                "scope_id", "request_id", "item_id", "record_id", "schema_version",
                "pipeline_version", "embedding_space_id", "business_date", "arrival_seq",
                "received_at", "accepted_at", "expires_at", "text", "raw_hash", "diagnostics",
            )
        })
        for kind, key in (("request", request_key), ("item", item_key),
                          ("seq", "seq:" + str(pending["arrival_seq"]))):
            body = {"kind": kind, **common}
            self._create_verified(REQUEST_INDEX, key, body, body)
        refreshed = self._head()
        if refreshed["source"]["pending"] != pending:
            raise AdmissionConflict("pending changed during materialization")
        updated = {**refreshed["source"], "pending": None,
                   "last_materialized_seq": pending["arrival_seq"],
                   "updated_at": _utc(self.clock())}
        try:
            self.store.replace(CONTROL_INDEX, HEAD_ID, updated,
                               refreshed["seq_no"], refreshed["primary_term"])
        except Exception as error:
            if self.store.is_conflict(error):
                raise AdmissionConflict("head changed before clear") from error
            raise AdmissionUnknown("clear slot confirmation is unknown") from error

    def recover(self) -> None:
        with self._lock:
            self._active()
            self._recover_head(self._head())

    def takeover(self) -> None:
        """部署层隔离旧写者后转移控制文档所有权并恢复单槽。"""
        with self._lock:
            self._active()
            for _ in range(8):
                head = self.store.get(CONTROL_INDEX, HEAD_ID)
                if head is None:
                    self.recover()
                    return
                if head["source"]["owner_id"] == self.owner_id:
                    self._recover_head(head)
                    return
                updated = {**head["source"], "owner_id": self.owner_id,
                           "updated_at": _utc(self.clock())}
                try:
                    self.store.replace(
                        CONTROL_INDEX, HEAD_ID, updated, head["seq_no"], head["primary_term"]
                    )
                except Exception as error:
                    if self.store.is_conflict(error):
                        self._active()
                        continue
                    raise AdmissionUnknown("ownership transfer confirmation is unknown") from error
                self.recover()
                return
            raise AdmissionConflict("ownership transfer CAS contention exceeds retry bound")

    def lookup(self, scope_id: str, request_id: str) -> AdmissionReceipt | None:
        _, request_key, _ = self._keys(scope_id, request_id, request_id)
        # 三次request读取跨越pending清槽/映射物化的交接窗口。
        for _ in range(2):
            mapping = self.store.get(REQUEST_INDEX, request_key)
            if mapping is not None:
                return self._mapped_receipt(scope_id, request_id, mapping)
            # W-A 族⑤(d)（A1-001）：只读 head——head 缺场 ⇒ 无任何受理发生过
            # ⇒ pending 必缺场，跳过 pending 检查直进末次 request 重读兜底
            # （三读协议逐字保持：mapping→head.pending→mapping，head 读仍在，
            # 只是只读）；_live 过期语义/写路径 _head() 创建语义均不动。
            head = self._head_readonly()
            pending = head["source"]["pending"] if head is not None else None
            if pending and pending["scope_id"] == scope_id and pending["request_id"] == request_id:
                self._live(pending["expires_at"])
                return _receipt(pending)
        mapping = self.store.get(REQUEST_INDEX, request_key)
        if mapping is None:
            return None
        return self._mapped_receipt(scope_id, request_id, mapping)

    def _mapped_receipt(self, scope_id: str, request_id: str, mapping: dict) -> AdmissionReceipt:
        source = mapping["source"]
        if source["scope_id"] != scope_id or source["request_id"] != request_id:
            raise AdmissionConflict("mapped identity mismatch")
        self._live(source["expires_at"])
        item_map = self.store.get(REQUEST_INDEX, "item:" + source["record_id"])
        seq_map = self.store.get(REQUEST_INDEX, "seq:" + str(source["arrival_seq"]))
        if (item_map is None or seq_map is None or
                any(source != {**mapping_doc["source"], "kind": "request"}
                    for mapping_doc in (item_map, seq_map))):
            raise AdmissionConflict("identity registration is incomplete")
        main = self.store.get(source["target_index"], source["record_id"])
        if main is None:
            raise AdmissionConflict("mapped main record absent")
        item = main["source"]
        if any(item.get(key) != source[key] for key in (
            "scope_id", "request_id", "item_id", "record_id", "business_date",
            "arrival_seq", "expires_at",
        )):
            raise AdmissionConflict("mapping and main identity differ")
        delivery = item.get("diagnostics", {}).get("delivery", {})
        if not isinstance(delivery.get("route_ref"), str) or not delivery["route_ref"]:
            raise AdmissionConflict("frozen delivery route is absent")
        return AdmissionReceipt(
            source["scope_id"], source["request_id"], source["item_id"],
            source["record_id"], source["arrival_seq"], source["business_date"],
            item["accepted_at"], source["expires_at"], item["schema_version"],
            item["pipeline_version"], item["embedding_space_id"],
            delivery["route_ref"], trace_id=delivery.get("trace_id"), reused=True,
        )
