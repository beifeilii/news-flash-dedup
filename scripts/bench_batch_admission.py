"""隔离批次受理基线；回环 HTTP 与函数对照，不发送模型或回调。"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import multiprocessing
import os
import platform
import re
import socket
import statistics
import sys
import threading
import time
import uuid
from collections import Counter, deque
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from bench_admission import client_from_environment, percentile  # noqa: E402
from news_flash_dedup.admission import (  # noqa: E402
    AdmissionConflict, AdmissionReceipt, AdmissionRequest, AdmissionUnknown,
    BUSINESS_ZONE, CONTROL_INDEX, REQUEST_INDEX, IdentityConflict, _digest, _utc,
)
from news_flash_dedup.api.contract import Backpressure, BodyTooLarge, IngressContext, Unavailable  # noqa: E402
from news_flash_dedup.api.http import create_app  # noqa: E402
from news_flash_dedup.batch_admission import (  # noqa: E402
    AdmissionCapacityExceeded, BatchAdmissionCoordinator, BatchLimits, HEAD_ID, _bytes, _keys,
)
from news_flash_dedup.batch_collector import BatchAdmissionCollector, CollectorRejected  # noqa: E402
from news_flash_dedup.batch_es_store import ElasticsearchBatchStore  # noqa: E402
from news_flash_dedup.es_admission_schema import control_mapping, item_mapping, request_mapping  # noqa: E402

RUN_ID = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?", re.ASCII)
EXTERNAL_REQUIREMENTS = ["UAT two load tiers", "recovery and deployment write fencing",
                         "real callback receipt", "external review and sign-off"]


class ProvisionError(RuntimeError):
    """Only fixed reason codes enter the report; transport exception details stay private."""

    REASONS = frozenset({"prefix_exists", "index_listing_failed", "index_listing_invalid",
                         "index_create_failed", "index_settings_failed"})

    def __init__(self, reason: str):
        if reason not in self.REASONS:
            raise ValueError("invalid provision reason")
        self.reason = reason
        super().__init__(reason)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def arguments(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="P01 隔离批次基线（默认真实回环HTTP）")
    parser.add_argument("--rate", type=int, required=True)
    parser.add_argument("--duration", type=float, required=True)
    parser.add_argument("--run-id", default=uuid.uuid4().hex[:12])
    parser.add_argument("--confirm-uat", action="store_true")
    parser.add_argument("--probe-only", action="store_true")
    parser.add_argument("--entry-mode", choices=("http", "function"), default="http")
    parser.add_argument("--output", type=Path)
    # P0-T6：域层默认镜像（BatchLimits.max_log_items 512 / collector
    # max_wait 0.02 / 队列硬层 512——域层同梯，bench 代表默认工作点）。
    for name, default in (("max-batch-items", 8), ("max-batch-bytes", 512 * 1024),
                          ("max-log-items", 512), ("max-log-bytes", 4 * 1024 * 1024),
                          ("queue-items", 512), ("queue-bytes", 4 * 1024 * 1024),
                          ("entry-workers", 64), ("mget-batch-size", 128),
                          ("max-http-bytes", 256 * 1024), ("max-text-codepoints", 20000)):
        parser.add_argument("--" + name, type=int, default=default)
    for name, default in (("max-drain", 60), ("reconcile-timeout", 120),
                          ("collector-wait", 0.02), ("request-timeout", 5),
                          ("http-timeout", 6), ("close-timeout", 5),
                          ("startup-timeout", 15), ("sample-interval", 0.1)):
        parser.add_argument("--" + name, type=float, default=default)
    parser.add_argument("--drain-window-seconds", type=float, default=1.0,
                        help="head drained 必须持续保持的窗口时长（秒）")
    parser.add_argument("--materializer-mode", choices=("polling", "event-driven"),
                        default="polling")
    parser.add_argument("--materializer-poll-floor", type=float, default=0.01,
                        help="事件驱动模式下兜底短超时（秒）")
    parser.add_argument("--materializer-loop-cap", type=int, default=16,
                        help="单次事件触发后连续物化上限，防雪崩")
    return parser.parse_args(argv)


def validate(args) -> None:
    if not isinstance(args.run_id, str) or RUN_ID.fullmatch(args.run_id) is None:
        raise ValueError("run-id requires 1..32 lowercase ASCII letters/digits/hyphens, alphanumeric ends")
    if not args.probe_only and not args.confirm_uat:
        raise RuntimeError("explicit --confirm-uat is required for UAT writes")
    # drain_window_seconds 允许为 0（关闭排空窗口，回到"一次 drained 即退出"）。
    for key, value in vars(args).items():
        if key == "drain_window_seconds":
            continue
        if type(value) in (int, float) and (not math.isfinite(value) or value <= 0):
            raise ValueError(key + " must be positive and finite")
    if not math.isfinite(args.drain_window_seconds) or args.drain_window_seconds < 0:
        raise ValueError("drain_window_seconds must be finite and >= 0")
    if args.collector_wait > 0.1:
        raise ValueError("collector wait must not exceed 100ms")
    if any(value > threading.TIMEOUT_MAX for key, value in vars(args).items()
           if type(value) is float):
        raise ValueError("time limits exceed platform wait bound")
    if args.materializer_poll_floor < 0 or args.materializer_poll_floor > 1:
        raise ValueError("materializer poll floor must be in [0, 1]s")
    if args.materializer_loop_cap < 1 or args.materializer_loop_cap > 1024:
        raise ValueError("materializer loop cap must be in 1..1024")
    if args.drain_window_seconds < 0 or args.drain_window_seconds > 30:
        raise ValueError("drain window must be in [0, 30]s")
    if args.materializer_mode not in ("polling", "event-driven"):
        raise ValueError("invalid materializer mode")
    if args.entry_mode not in ("http", "function"):
        raise ValueError("invalid entry mode")
    total = args.rate * args.duration
    if not float(total).is_integer() or not 1 <= total <= 100000:
        raise ValueError("rate * duration must be an integer in 1..100000")


def provision(client, prefix: str, stop=None) -> dict:
    if not prefix.startswith("p01-batch-") or not prefix.endswith("-") or not RUN_ID.fullmatch(prefix[10:-1]):
        raise ValueError("isolated batch prefix is required")
    # HEAD 对通配符可返回200，而实际匹配集仍为空；仅以实际索引列表判重。
    # 窗口W2δ（批78 保链纪律）：from None → from error，原异常入 __cause__。
    try:
        listed = client.indices.get(index=prefix + "*", allow_no_indices=True,
                                    ignore_unavailable=True)
    except Exception as error:
        raise ProvisionError("index_listing_failed") from error
    try:
        names = tuple(listed.keys())
    except (AttributeError, TypeError) as error:
        raise ProvisionError("index_listing_invalid") from error
    if any(not isinstance(name, str) or not name.startswith(prefix) for name in names):
        raise ProvisionError("index_listing_invalid")
    if names:
        raise ProvisionError("prefix_exists")
    now = utc_now()
    days = {(now + timedelta(days=offset)).astimezone(BUSINESS_ZONE).date() for offset in (0, 1)}
    indices = {prefix + CONTROL_INDEX: control_mapping(), prefix + REQUEST_INDEX: request_mapping()}
    for day in sorted(days):
        indices[prefix + "news-dedup-items-v1-" + day.strftime("%Y.%m.%d")] = item_mapping()
    for name, schema in indices.items():
        if stop is not None and stop.is_set():
            raise DeadlineExceeded("provision stopped")
        try:
            client.indices.create(index=name, body=schema)
        except Exception as error:
            raise ProvisionError("index_create_failed") from error
    if stop is not None and stop.is_set():
        raise DeadlineExceeded("provision stopped")
    try:
        settings = client.indices.get_settings(index=",".join(indices))
    except Exception as error:
        raise ProvisionError("index_settings_failed") from error
    return {name: {"shards": int(settings[name]["settings"]["index"]["number_of_shards"]),
                   "replicas": int(settings[name]["settings"]["index"]["number_of_replicas"])}
            for name in indices}


def distribution(values) -> dict:
    return {"count": len(values), "p50_ms": percentile(values, 0.5),
            "p95_ms": percentile(values, 0.95), "p99_ms": percentile(values, 0.99),
            "max_ms": max(values, default=None),
            "mean_ms": statistics.mean(values) if values else None}


def classify_error(error: Exception) -> str:
    if isinstance(error, AdmissionCapacityExceeded):
        return "capacity:" + error.reason
    if isinstance(error, CollectorRejected):
        if error.reason in {"queue_full", "queue_timeout", "closed", "worker_failed", "request_too_large"}:
            return "collector:" + error.reason
        return "technical:InvalidCollectorReason"
    if isinstance(error, IdentityConflict):
        return "identity_conflict"
    if isinstance(error, AdmissionUnknown):
        return "unknown"
    if isinstance(error, AdmissionConflict):
        return "admission_conflict"
    return "technical:" + type(error).__name__


class DeadlineExceeded(TimeoutError):
    pass


class Threads:
    """只记录自己启动的线程；截止后不冒充已经停止。"""

    def __init__(self):
        self.threads: list[threading.Thread] = []

    def start(self, name, target):
        thread = threading.Thread(name=name, target=target, daemon=True)
        self.threads.append(thread)
        thread.start()
        return thread

    def call(self, name, function, deadline):
        if time.monotonic() >= deadline:
            raise DeadlineExceeded(name)
        result = []
        done = threading.Event()
        def invoke():
            try:
                result.append((True, function()))
            except BaseException as error:
                result.append((False, error))
            finally:
                done.set()
        thread = self.start(name, invoke)
        if not done.wait(max(0, deadline - time.monotonic())):
            raise DeadlineExceeded(name)
        thread.join(max(0, deadline - time.monotonic()))
        ok, value = result[0]
        if not ok:
            raise value
        return value

    def alive(self):
        return {f"{thread.name}:{i}": thread.is_alive() for i, thread in enumerate(self.threads)}


class ObservedStore:
    """复用已有读头观测；尝试写入的原始身份只留内存供事后独立核验。"""

    def __init__(self, store):
        self.store = store
        self.lock = threading.RLock()
        self.write_stop = threading.Event()
        self.stages = {}
        self.stage_errors = Counter()
        self.heads = {}
        self.latest_head = None
        self.candidates = {}
        self.written_keys = set()
        self.peak_head_items = self.peak_head_bytes = self.peak_log_bytes = 0
        self.max_pending_age = 0.0
        self.unknown_write = False

    def _observe(self, head):
        if head is None:
            return
        source = head["source"]
        with self.lock:
            self.heads[(head["seq_no"], head["primary_term"])] = (
                source["last_allocated_seq"], source["last_materialized_seq"])
            if len(self.heads) > 512:
                self.heads.pop(next(iter(self.heads)))
            if self.latest_head is None or head["seq_no"] >= self.latest_head["seq_no"]:
                self.latest_head = copy.deepcopy(head)
            batches = source["pending"]["batches"]
            entries = [entry for batch in batches for entry in batch["entries"]]
            self.peak_head_items = max(self.peak_head_items, len(entries))
            self.peak_head_bytes = max(self.peak_head_bytes, len(_bytes(source)))
            self.peak_log_bytes = max(self.peak_log_bytes, len(_bytes(batches)))
            for entry in entries:
                age = (utc_now() - datetime.fromisoformat(entry["accepted_at"].replace("Z", "+00:00"))).total_seconds()
                self.max_pending_age = max(self.max_pending_age, age)

    def _timed(self, stage, function, write=False):
        started = time.monotonic()
        try:
            if write and self.write_stop.is_set():
                raise AdmissionConflict("benchmark stopped before new local write")
            return function()
        except Exception as error:
            with self.lock:
                self.stage_errors[stage + ":" + type(error).__name__] += 1
                if write and not self.store.is_conflict(error):
                    self.unknown_write = True
            raise
        finally:
            with self.lock:
                self.stages.setdefault(stage, []).append((time.monotonic() - started) * 1000)

    def get(self, index, key):
        found = self._timed("get", lambda: self.store.get(index, key))
        if (index, key) == (CONTROL_INDEX, HEAD_ID):
            self._observe(found)
        return found

    def mget(self, keys):
        return self._timed("mget", lambda: self.store.mget(keys))

    def create(self, index, key, body):
        with self.lock:
            self.written_keys.add((index, key))
        return self._timed("create", lambda: self.store.create(index, key, body), True)

    def replace(self, index, key, body, seq_no, primary_term):
        with self.lock:
            before = self.heads.get((seq_no, primary_term), (0, 0))
            stage = "shrink" if body["last_materialized_seq"] > before[1] else "append"
            if stage == "append":
                for batch in body["pending"]["batches"]:
                    for entry in batch["entries"]:
                        if entry["arrival_seq"] > before[0]:
                            identity = (entry["scope_id"], entry["request_id"])
                            proposed = self.candidates.setdefault(identity, [])
                            if entry not in proposed:
                                proposed.append(copy.deepcopy(entry))
            self.written_keys.add((index, key))
        result = self._timed(stage, lambda: self.store.replace(index, key, body, seq_no, primary_term), True)
        self._observe({"source": body, "seq_no": seq_no + 1, "primary_term": primary_term})
        return result

    def bulk_create(self, documents):
        with self.lock:
            self.written_keys.update((index, key) for index, key, _ in documents)
        return self._timed("bulk", lambda: self.store.bulk_create(documents), True)

    def is_conflict(self, error):
        return self.store.is_conflict(error)

    def sample_age(self):
        with self.lock:
            if self.latest_head:
                for batch in self.latest_head["source"]["pending"]["batches"]:
                    for entry in batch["entries"]:
                        accepted = datetime.fromisoformat(entry["accepted_at"].replace("Z", "+00:00"))
                        self.max_pending_age = max(self.max_pending_age, (utc_now() - accepted).total_seconds())

    def metrics(self):
        self.sample_age()
        with self.lock:
            return {"stages": {name: distribution(values) for name, values in self.stages.items()},
                    "errors": dict(self.stage_errors), "peak_head_items": self.peak_head_items,
                    "peak_head_serialized_bytes": self.peak_head_bytes, "peak_log_bytes": self.peak_log_bytes,
                    "max_pending_age_seconds": self.max_pending_age}


@dataclass
class InputRecord:
    request: AdmissionRequest
    planned_at: float
    sent_at: float | None = None
    finished_at: float | None = None
    outcome: str = "not_sent"
    server_outcome: str = "not_called"
    receipt: AdmissionReceipt | None = None
    http_status: int | None = None
    response_hash: str | None = None
    client_error: str | None = None
    server_started: float | None = None
    server_finished: float | None = None


def make_request(number: int, run_id: str) -> AdmissionRequest:
    identity = f"{number + 1}-1"
    return AdmissionRequest("default", identity, identity, f"隔离基线固定工件 {number + 1}。",
                            utc_now(), "1", "batch-fixed-v1", "none", "batch-no-send",
                            f"batch-{run_id}-{number + 1}")


def http_payload(request: AdmissionRequest) -> dict:
    return {"traceId": request.trace_id, "requestId": request.request_id,
            "rewrittenId": request.item_id, "source": {"title": "", "body": "", "pageDate": "2026-09-25"},
            "rewrite": {"title": "", "body": request.text}}


class BenchmarkPort:
    def __init__(self, collector, records):
        self.collector = collector
        self.records = {(r.request.scope_id, r.request.request_id): r for r in records}
        self.active = 0
        self.lock = threading.Lock()

    def accept(self, request):
        record = self.records[(request.scope_id, request.request_id)]
        record.request = request
        record.server_started = time.monotonic()
        with self.lock:
            self.active += 1
        try:
            record.receipt = self.collector.accept(request)
            record.server_outcome = "accepted"
            return record.receipt
        except Exception as error:
            record.server_outcome = classify_error(error)
            if isinstance(error, AdmissionCapacityExceeded):
                if error.reason == "batch_byte_limit":
                    raise BodyTooLarge("configured single item batch limit") from error
                raise Backpressure("durable log capacity") from error
            if isinstance(error, CollectorRejected):
                if error.reason == "request_too_large":
                    raise BodyTooLarge("configured collector item limit") from error
                if error.reason in {"queue_full", "queue_timeout"}:
                    raise Backpressure("collector capacity") from error
                if error.reason in {"closed", "worker_failed"}:
                    raise Unavailable("collector not accepting") from error
            # unknown必须原样到HTTP500，不能冒充确定未受理。
            raise
        finally:
            record.server_finished = time.monotonic()
            with self.lock:
                self.active -= 1


class LoopbackHTTP:
    def __init__(self, port, args):
        self.port, self.args = port, args
        self.resources = Threads()
        self.closed = threading.Event()
        self.close_errors = Counter()
        self.socket = self.server = self.thread = self.client = None
        self.url = ""

    def start(self):
        import httpx
        import uvicorn
        if self.closed.is_set():
            raise DeadlineExceeded("loopback already closed")
        args = self.args
        app = create_app(admission_port=self.port, context_provider=lambda _: IngressContext(
            "default", utc_now(), "1", "batch-fixed-v1", "none", "batch-no-send",
            args.max_text_codepoints, args.max_http_bytes), max_http_bytes=args.max_http_bytes)
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.bind(("127.0.0.1", 0))
        self.socket.listen(128)
        self.url = "http://127.0.0.1:" + str(self.socket.getsockname()[1])
        config = uvicorn.Config(app, host="127.0.0.1", port=0, log_config=None, access_log=False,
                                log_level="critical", lifespan="off", ws="none",
                                timeout_graceful_shutdown=args.close_timeout)
        self.server = uvicorn.Server(config)
        self.thread = self.resources.start("batch-loopback-server", lambda: self.server.run(sockets=[self.socket]))
        deadline = time.monotonic() + args.startup_timeout
        ready = threading.Event()
        while not self.server.started:
            if self.closed.is_set() or not self.thread.is_alive() or time.monotonic() >= deadline:
                self.server.should_exit = True
                raise DeadlineExceeded("loopback startup")
            ready.wait(0.005)
        if self.closed.is_set():
            self.server.should_exit = True
            raise DeadlineExceeded("loopback closed during startup")
        self.client = httpx.Client(timeout=httpx.Timeout(args.http_timeout), trust_env=False,
                                   limits=httpx.Limits(max_connections=args.entry_workers,
                                                       max_keepalive_connections=args.entry_workers))

    def close(self, deadline):
        self.closed.set()
        if self.server is not None:
            self.server.should_exit = True
        if self.client is not None and not self.client.is_closed:
            try:
                self.resources.call("batch-http-client-close", self.client.close, deadline)
            except Exception as error:
                self.close_errors[type(error).__name__] += 1
        if self.thread is not None:
            self.thread.join(max(0, deadline - time.monotonic()))
            if self.thread.is_alive():
                self.server.force_exit = True
        if self.socket is not None:
            try:
                self.socket.close()
            except OSError as error:
                self.close_errors[type(error).__name__] += 1


def materialize_loop(store, coordinator, stop, interval, errors, *,
                      wake_event=None, poll_floor=0.01, loop_cap=16, mode="polling"):
    if mode not in {"polling", "event-driven"}:
        raise ValueError("invalid materializer mode")
    while not stop.is_set():
        if mode == "event-driven" and wake_event is not None:
            # 兜底短超时 + 事件触发双保险；事件触发后立即清除，下次再等。
            triggered = wake_event.wait(timeout=poll_floor)
            if triggered:
                wake_event.clear()
        else:
            stop.wait(interval)
        try:
            iterations = 0
            while iterations < loop_cap:
                if stop.is_set():
                    return
                head = store.get(CONTROL_INDEX, HEAD_ID)
                if stop.is_set():
                    return
                if not head or not head["source"]["pending"]["batches"]:
                    break
                coordinator.materialize_prefix()
                iterations += 1
            # 空头时回到等待；事件驱动下立即重新等事件。
        except Exception as error:
            name = type(error).__name__
            errors[name] = errors.get(name, 0) + 1


def durable_head_drained(store) -> bool:
    """Read the persistent control head in real time before declaring drain."""
    head = store.get(CONTROL_INDEX, HEAD_ID)
    if head is None:
        return False
    source = head["source"]
    BatchAdmissionCoordinator._validate_log(source)
    return (source["last_allocated_seq"] == source["last_materialized_seq"] and
            not source["pending"]["batches"])


def _unresolved(records, reason):
    return {"durable_pending": 0, "four_document_complete": 0, "lost_confirmed": 0,
            "unknown_pending": sum(r.outcome == "unknown" for r in records), "unknown_settled": 0,
            "unresolved": len(records), "reconcile_complete": False, "errors": {reason: 1},
            "watermarks": {}, "reconciled": [], "sequence_complete": False}


def _same_original(entry, record):
    request = record.request
    expected = {"scope_id": request.scope_id, "request_id": request.request_id, "item_id": request.item_id,
                "text": request.text, "received_at": _utc(request.received_at),
                "schema_version": request.schema_version, "pipeline_version": request.pipeline_version,
                "embedding_space_id": request.embedding_space_id, "delivery_route_ref": request.delivery_route_ref,
                "trace_id": request.trace_id, "record_id": _digest([request.scope_id, request.item_id]),
                "business_fingerprint": _digest({"item_id": request.item_id, "text": request.text})}
    if any(entry.get(key) != value for key, value in expected.items()):
        return False
    if record.receipt:
        if any(entry.get(key) != value for key, value in asdict(record.receipt).items() if key != "reused"):
            return False
    return True


def _check_documents(expected, found):
    for index, key, body in expected:
        actual = found.get((index, key))
        if actual is None:
            return False
        source = actual["source"]
        if any(source.get(field) != value for field, value in body.items()
               if field not in {"task_state", "delivery_state", "result", "diagnostics"}):
            return False
        if "diagnostics" in body:
            delivery = source.get("diagnostics", {}).get("delivery", {})
            if any(delivery.get(field) != value for field, value in body["diagnostics"]["delivery"].items()):
                return False
    return True


def _scan_inputs(store, records, deadline, batch_size, writers_quiescent, unknown_cas):
    before = store.get(CONTROL_INDEX, HEAD_ID)
    if before is None:
        return _unresolved(records, "head_absent")
    source = before["source"]
    BatchAdmissionCoordinator._validate_log(source)
    allocated, materialized = source["last_allocated_seq"], source["last_materialized_seq"]
    pending = {(e["scope_id"], e["request_id"]): e
               for batch in source["pending"]["batches"] for e in batch["entries"]}
    identities = {(r.request.scope_id, r.request.request_id) for r in records}
    if allocated > len(identities) or set(pending) - identities:
        return _unresolved(records, "unregistered_sequence_or_identity")
    with store.lock:
        candidates = copy.deepcopy(store.candidates)
        written_keys = set(store.written_keys)
    wanted = {(REQUEST_INDEX, "seq:" + str(seq)) for seq in range(1, allocated + 2)}
    wanted.update(written_keys - {(CONTROL_INDEX, HEAD_ID)})
    proposals = {}
    for record in records:
        request = record.request
        identity = (request.scope_id, request.request_id)
        _, request_key, item_key = _keys(*identity, request.item_id)
        wanted.update(((REQUEST_INDEX, request_key), (REQUEST_INDEX, item_key)))
        entries = [entry for entry in candidates.get(identity, []) if _same_original(entry, record)]
        proposals[identity] = entries
        for entry in entries:
            wanted.update((index, key) for index, key, _ in BatchAdmissionCoordinator._documents([entry]))
    found = {}
    keys = sorted(wanted)
    for offset in range(0, len(keys), batch_size):
        if time.monotonic() >= deadline:
            raise DeadlineExceeded("reconcile mget")
        batch = keys[offset:offset + batch_size]
        values = store.mget(batch)
        if len(values) != len(batch):
            raise ValueError("incomplete mget")
        found.update(zip(batch, values))
    after = store.get(CONTROL_INDEX, HEAD_ID)
    if before != after:
        return _unresolved(records, "head_changed")
    result = {**_unresolved(records, "unused"), "errors": {}, "unresolved": 0,
              "unknown_pending": 0, "watermarks": {"allocated": allocated, "materialized": materialized},
              "head_version": {"seq_no": before["seq_no"], "primary_term": before["primary_term"]}}
    errors = Counter()
    explained = {}
    for record in records:
        identity = (record.request.scope_id, record.request.request_id)
        entries = proposals[identity]
        match = None
        state = "unresolved"
        for entry in entries:
            if identity in pending and pending[identity] == entry:
                existing = [(index, key, body) for index, key, body in BatchAdmissionCoordinator._documents([entry])
                            if found.get((index, key)) is not None]
                if _check_documents(existing, found):
                    state, match = "pending", entry
                    break
            if identity not in pending and _check_documents(BatchAdmissionCoordinator._documents([entry]), found):
                state, match = "four_documents", entry
                break
        if match:
            seq = match["arrival_seq"]
            if seq > allocated or (state == "four_documents" and seq > materialized):
                state = "unresolved"
            elif seq in explained and explained[seq] != identity:
                state = "unresolved"
                errors["duplicate_sequence"] += 1
            else:
                explained[seq] = identity
        if state == "unresolved":
            _, req_key, item_key = _keys(*identity, record.request.item_id)
            related = {(REQUEST_INDEX, req_key), (REQUEST_INDEX, item_key)}
            for entry in entries:
                related.update((index, key) for index, key, _ in BatchAdmissionCoordinator._documents([entry]))
            absent = identity not in pending and all(found.get(key) is None for key in related)
            # 所有未知写入即使线程退出也可能仍在ES端，未命中不构成拒收证明。
            if absent and writers_quiescent and not unknown_cas:
                if record.outcome == "accepted" and record.receipt and entries:
                    state = "lost_confirmed"
                elif record.outcome not in {"unknown", "inflight"} and not record.outcome.startswith("technical:"):
                    state = "not_accepted"
        if state == "pending":
            result["durable_pending"] += 1
        elif state == "four_documents":
            result["four_document_complete"] += 1
        elif state == "lost_confirmed":
            result["lost_confirmed"] += 1
        elif state == "unresolved":
            result["unresolved"] += 1
            errors["input_unresolved"] += 1
        if record.outcome == "unknown":
            result["unknown_settled" if state in {"pending", "four_documents"} else "unknown_pending"] += 1
        result["reconciled"].append({"request_hash": _digest(list(identity)), "state": state,
                                     "arrival_seq": match["arrival_seq"] if match else None,
                                     "text_sha256": hashlib.sha256(record.request.text.encode("utf-8")).hexdigest()})
    if set(explained) != set(range(1, allocated + 1)):
        errors["unexplained_sequence"] += 1
    expected_keys = set()
    for identity, entries in proposals.items():
        for entry in entries:
            if explained.get(entry["arrival_seq"]) == identity:
                expected_keys.update((index, key) for index, key, _ in BatchAdmissionCoordinator._documents([entry]))
    for key, value in found.items():
        if value is not None and key not in expected_keys:
            errors["extra_document"] += 1
    for seq in range(1, materialized + 1):
        value = found.get((REQUEST_INDEX, "seq:" + str(seq)))
        if value is None or value["source"].get("kind") != "seq" or value["source"].get("arrival_seq") != seq:
            errors["invalid_sequence_registration"] += 1
    result["sequence_complete"] = not any(name in errors for name in (
        "unexplained_sequence", "duplicate_sequence", "extra_document", "invalid_sequence_registration"))
    result["errors"] = dict(errors)
    result["reconcile_complete"] = not errors and result["unresolved"] == 0
    result["sequence_scope"] = "all allocated + own attempted keys + next sequence; exclusive fresh prefix"
    return result


def reconcile_inputs(store, records, *, deadline, batch_size=128, writers_quiescent=True,
                     unknown_cas=False, resources=None, max_retries=4, retry_sleep=0.1):
    """对账全部输入；未收敛（unknown_pending 或 unresolved > 0）时持续回查直到收敛或 deadline。

    缺口 3 闭环：原版只一次性扫描，未收敛的 unknown 不会被同 ID 持续追查到。
    本版本在 deadline 内最多 max_retries 次重新扫描，每次 sleep retry_sleep；
    每次 MGET 同一组 keys，能识别"上次扫描时还在 pending、这次已物化"的情况。
    """
    resources = resources or Threads()
    last_result = None
    attempts = 0
    while True:
        attempts += 1
        try:
            result = resources.call("batch-reconcile", lambda: _scan_inputs(
                store, records, deadline, batch_size, writers_quiescent, unknown_cas), deadline)
        except Exception as error:
            result = _unresolved(records, type(error).__name__)
        result["reconcile_worker_alive"] = any(t.is_alive() for t in resources.threads if t.name == "batch-reconcile")
        result["reconcile_attempts"] = attempts
        last_result = result
        if result.get("reconcile_complete"):
            break
        if attempts >= max_retries:
            break
        if time.monotonic() + retry_sleep >= deadline:
            break
        time.sleep(retry_sleep)
    if last_result is None:
        last_result = _unresolved(records, "no_attempts")
    last_result["reconcile_attempts"] = attempts
    last_result["reconcile_resolved"] = last_result.get("reconcile_complete", False)
    return last_result


def source_summary():
    paths = [Path(__file__), ROOT / "scripts/bench_admission.py"]
    paths.extend(ROOT / "src/news_flash_dedup" / name for name in (
        "admission.py", "admission_schema.py", "batch_admission.py", "batch_collector.py",
        "batch_es_store.py", "es_admission_schema.py", "api/http.py", "api/contract.py"))
    return {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}


def _record_report(record, started):
    return {"request_hash": _digest([record.request.scope_id, record.request.request_id]),
            "text_sha256": hashlib.sha256(record.request.text.encode("utf-8")).hexdigest(),
            "text_codepoints": len(record.request.text), "text_bytes": len(record.request.text.encode("utf-8")),
            "planned_seconds": record.planned_at - started,
            "sent_seconds": None if record.sent_at is None else record.sent_at - started,
            "finished_seconds": None if record.finished_at is None else record.finished_at - started,
            "outcome": record.outcome, "server_outcome": record.server_outcome,
            "client_error": record.client_error,
            "server_duration_ms": None if record.server_finished is None else
            (record.server_finished - record.server_started) * 1000,
            "http_status": record.http_status, "response_sha256": record.response_hash}


def run(args: argparse.Namespace, progress=None) -> dict:
    validate(args)
    prefix = f"p01-batch-{args.run_id}-"
    resources = Threads()
    stop = threading.Event()
    client = store = collector = host = worker = port = None
    entry_threads = []
    records = []
    report = {}
    materialization_errors = {}
    close_errors = Counter()
    started = time.monotonic()
    setup_deadline = started + args.startup_timeout
    try:
        def connect():
            nonlocal client
            client = client_from_environment()
            if stop.is_set():
                client.close()
                raise DeadlineExceeded("late client creation")
            return client.info()
        info = resources.call("batch-client-start", connect, setup_deadline)
        if not str(info["version"]["number"]).startswith("8."):
            raise RuntimeError("batch prototype requires Elasticsearch 8.x")
        report = {"run_id": args.run_id, "prefix": prefix, "es_version": info["version"]["number"],
                  "config": {key: str(value) if isinstance(value, Path) else value for key, value in vars(args).items()},
                  "source_sha256": source_summary(), "g0_passed": False,
                  "external_requirements_remain": EXTERNAL_REQUIREMENTS,
                  "entry_mode": args.entry_mode, "http_202_measured": False,
                  "environment": {"python": platform.python_version(), "platform": platform.platform(),
                                  "cpu_count": os.cpu_count(), "clock": "UTC wall / monotonic intervals"},
                  "es_transport": {"request_timeout_seconds": 5, "max_retries": 0, "retry_on_timeout": False,
                                   "connections_per_node": None},
                  "fixture": {"scope": "default", "pipeline_version": "batch-fixed-v1", "schema_version": "1",
                              "embedding_space_id": "none", "callback_sent": False, "algorithm_executed": False,
                              "cache_state": "fresh isolated indices; shared cluster cache uncontrolled"}}
        if hasattr(client, "transport"):
            report["es_transport"]["connections_per_node"] = [
                node.config.connections_per_node for node in client.transport.node_pool.all()]
        frozen = {key: value for key, value in report["config"].items() if key not in {"run_id", "output", "rate", "duration"}}
        report["configuration_sha256"] = hashlib.sha256(_bytes(frozen)).hexdigest()
        report["source_and_configuration_sha256"] = hashlib.sha256(_bytes({
            "sources": report["source_sha256"], "configuration": frozen})).hexdigest()
        if args.probe_only:
            report.update(read_only_probe=True, http_202_measured=False)
            return report
        topology = resources.call("batch-provision", lambda: provision(client, prefix, stop), setup_deadline)
        store = ObservedStore(ElasticsearchBatchStore(client, index_prefix=prefix))
        limits = BatchLimits(args.max_batch_items, args.max_batch_bytes, args.max_log_items, args.max_log_bytes)
        def coordinator():
            return BatchAdmissionCoordinator(store, owner_id="batch-" + args.run_id,
                                             owner_isolated=lambda: not stop.is_set(), limits=limits, clock=utc_now)
        admission, materializer = coordinator(), coordinator()
        collector = BatchAdmissionCollector(admission, max_wait_seconds=args.collector_wait,
                                            max_queue_items=args.queue_items, max_queue_bytes=args.queue_bytes,
                                            request_timeout_seconds=args.request_timeout,
                                            close_timeout_seconds=args.close_timeout)
        total = int(args.rate * args.duration)
        records = [InputRecord(make_request(i, args.run_id), 0) for i in range(total)]
        port = BenchmarkPort(collector, records)
        if args.entry_mode == "http":
            host = LoopbackHTTP(port, args)
            resources.call("batch-http-start", host.start, setup_deadline)
            report["http_bind"] = host.url.removeprefix("http://")
        worker = resources.start("batch-materializer", lambda: materialize_loop(
            store, materializer, stop, args.sample_interval, materialization_errors,
            wake_event=(collector.batch_flushed if args.materializer_mode == "event-driven" else None),
            poll_floor=args.materializer_poll_floor, loop_cap=args.materializer_loop_cap,
            mode=args.materializer_mode))
        started = time.monotonic()
        report["started_utc"] = utc_now().isoformat()
        for number, record in enumerate(records):
            record.planned_at = started + number / args.rate
        slots = threading.BoundedSemaphore(args.entry_workers)
        snapshots = deque(maxlen=2048)
        last_sample = last_progress = 0.0
        peak_oldest = 0.0
        def sample():
            nonlocal last_sample, last_progress, peak_oldest
            now = time.monotonic()
            if now - last_sample >= args.sample_interval:
                snap = asdict(collector.snapshot())
                peak_oldest = max(peak_oldest, snap["oldest_waiting_seconds"])
                store.sample_age()
                snapshots.append({"elapsed_seconds": now - started, **snap})
                last_sample = now
                if progress and now - last_progress >= 1:
                    progress({"phase": "input_or_drain", "input": sum(r.outcome != "not_sent" for r in records),
                              "outcomes": dict(Counter(r.outcome for r in records))})
                    last_progress = now
        def accept_one(record):
            record.sent_at = time.monotonic()
            record.outcome = "inflight"
            try:
                if host:
                    response = host.client.post(host.url + "/v1/api/task/run", json=http_payload(record.request))
                    record.http_status = response.status_code
                    record.response_hash = hashlib.sha256(response.content).hexdigest()
                    if response.status_code == 202:
                        body = response.json()
                        if (body.get("accepted") is not True or body.get("requestId") != record.request.request_id or
                                record.receipt is None or body.get("duplicate") != record.receipt.reused):
                            raise AdmissionUnknown("HTTP receipt mismatch")
                        record.outcome = "accepted"
                    elif record.server_outcome == "accepted":
                        record.outcome = "unknown"
                    elif record.server_outcome != "not_called":
                        record.outcome = record.server_outcome
                    elif response.status_code >= 500:
                        record.outcome = "unknown"
                    else:
                        record.outcome = "http:" + str(response.status_code)
                else:
                    record.request = replace(record.request, received_at=utc_now())
                    record.receipt = collector.accept(record.request)
                    record.server_outcome = record.outcome = "accepted"
            except Exception as error:
                record.outcome = classify_error(error)
                record.client_error = record.outcome
                if host and record.outcome.startswith("technical:"):
                    record.outcome = "unknown"
                elif not host:
                    record.server_outcome = record.outcome
            finally:
                record.finished_at = time.monotonic()
                slots.release()
        for record in records:
            if stop.wait(max(0, record.planned_at - time.monotonic())):
                break
            if not slots.acquire(blocking=False):
                record.outcome = "local:entry_capacity"
                record.finished_at = time.monotonic()
            else:
                try:
                    entry_threads.append(resources.start("batch-entry", lambda r=record: accept_one(r)))
                except Exception as error:
                    slots.release()
                    record.outcome = classify_error(error)
                    record.finished_at = time.monotonic()
            sample()
        # 观察窗口保留到duration结束，排空不吞入最后一个到达间隔。
        stop.wait(max(0, started + args.duration - time.monotonic()))
        input_stopped = time.monotonic()
        # 排空窗口包含在 max_drain 内：drained 必须持续保持 window 秒数才确认排空。
        drain_deadline = input_stopped + args.max_drain
        drained_first = None
        drained_at = None
        drain_window_seconds = args.drain_window_seconds
        drain_read_errors = Counter()
        while time.monotonic() < drain_deadline:
            sample()
            snap = collector.snapshot()
            threads_idle = all(not thread.is_alive() for thread in entry_threads)
            if threads_idle and not snap.waiting_items and not snap.inflight_items and port.active == 0:
                try:
                    drained_now = resources.call("batch-drain-head",
                                                  lambda: durable_head_drained(store), drain_deadline)
                except Exception as error:
                    drain_read_errors[type(error).__name__] += 1
                    if isinstance(error, DeadlineExceeded):
                        break
                    drained_now = False
                if drained_now:
                    if drained_at is None:
                        drained_first = time.monotonic()
                        drained_at = drained_first
                    elif time.monotonic() - drained_at >= drain_window_seconds:
                        break
                    else:
                        # 持续 drained，等待窗口结束
                        stop.wait(min(args.sample_interval,
                                      max(0, drain_deadline - time.monotonic())))
                        continue
                else:
                    drained_at = None
            else:
                drained_at = None
            stop.wait(min(args.sample_interval, max(0, drain_deadline - time.monotonic())))
        # 排空时点：若达到窗口结束，记录到 drained_first + window；
        # 否则视为超时排空（input 完全停止后仍未达到持续 drained）。
        if drained_at is not None and time.monotonic() - drained_at >= drain_window_seconds:
            drain_completion = drained_at + drain_window_seconds
        else:
            drain_completion = time.monotonic()
        drain_seconds = drain_completion - input_stopped
        drained = drained_at is not None and time.monotonic() - drained_at >= drain_window_seconds
        stop.set()
        store.write_stop.set()
        cleanup_deadline = time.monotonic() + args.close_timeout
        try:
            resources.call("batch-collector-close", collector.close, cleanup_deadline)
        except Exception as error:
            close_errors[type(error).__name__] += 1
        if host:
            host.close(cleanup_deadline)
        worker.join(max(0, cleanup_deadline - time.monotonic()))
        for thread in entry_threads:
            thread.join(max(0, cleanup_deadline - time.monotonic()))
        for record in records:
            if record.outcome == "inflight":
                record.outcome = "unknown"
        reconcile_started = time.monotonic()
        records = [copy.copy(record) for record in records]
        worker_alive = worker.is_alive() or collector.snapshot().worker_alive
        quiescent = not worker_alive and not any(t.is_alive() for t in entry_threads) and port.active == 0
        reconciliation = reconcile_inputs(store, records, deadline=reconcile_started + args.reconcile_timeout,
                                           batch_size=args.mget_batch_size, writers_quiescent=quiescent,
                                           unknown_cas=store.unknown_write or admission._unknown_cas, resources=resources)
        counts = Counter(record.outcome for record in records)
        latency = {}
        for outcome in counts:
            subset = [r for r in records if r.outcome == outcome]
            latency[outcome] = {
                "planned_to_response": distribution([(r.finished_at - r.planned_at) * 1000 for r in subset if r.finished_at]),
                "sent_to_response": distribution([(r.finished_at - r.sent_at) * 1000 for r in subset if r.sent_at and r.finished_at]),
                "schedule_lateness": distribution([(r.sent_at - r.planned_at) * 1000 for r in subset if r.sent_at])}
        report.update(reconciliation)
        report.update(index_topology=topology, counts={"input": total, **counts}, latency_by_outcome=latency,
                      http_202_measured=bool(host and any(r.http_status == 202 for r in records)),
                      input_complete=all(r.outcome != "not_sent" for r in records),
                      response_complete=all(r.finished_at is not None for r in records),
                      drain_seconds=drain_seconds, drain_timed_out=not drained,
                      drain_head_realtime_confirmed=drained, drain_read_errors=dict(drain_read_errors),
                      worker_alive=worker_alive,
                      reconcile_seconds=time.monotonic() - reconcile_started,
                      collector_snapshot=asdict(collector.snapshot()), collector_samples=list(snapshots),
                      max_waiting_age_seconds=peak_oldest, store_metrics=store.metrics(),
                      materialization_errors=dict(materialization_errors),
                      client_errors=dict(Counter(r.client_error for r in records if r.client_error)),
                      server_responses=dict(Counter(r.server_outcome for r in records)),
                      client_responses=dict(Counter(str(r.http_status) for r in records if r.http_status is not None)),
                      inputs=[_record_report(r, started) for r in records],
                      local_writer_isolation_only=True, deployment_write_fencing=False)
        p95 = latency.get("accepted", {}).get("planned_to_response", {}).get("p95_ms")
        report["local_performance"] = {
            "passed": bool(counts.get("accepted", 0) == total and report["input_complete"] and
                            p95 is not None and p95 <= 300 and drained and
                            drain_seconds <= min(60, args.max_drain) and
                           reconciliation["reconcile_complete"] and reconciliation["four_document_complete"] == total and
                           reconciliation["lost_confirmed"] == 0 and quiescent),
            "criteria": {"certain_admission_percent": 100, "p95_ms": 300, "drain_seconds": 60, "lost": 0},
            "scope": "local HTTP to isolated storage" if host else "function microbaseline, NOT HTTP 202"}
        return report
    except BaseException as error:
        report.update(run_id=args.run_id, prefix=prefix, errors={type(error).__name__: 1},
                       input_complete=False, reconcile_complete=False, lost_confirmed=0, g0_passed=False,
                       local_performance={"passed": False}, external_requirements_remain=EXTERNAL_REQUIREMENTS)
        if isinstance(error, ProvisionError):
            report["failure_reason"] = error.reason
        error.benchmark_report = report
        raise
    finally:
        stop.set()
        if store:
            store.write_stop.set()
        deadline = time.monotonic() + args.close_timeout
        if collector and not collector.snapshot().closed:
            try:
                resources.call("batch-collector-final-close", collector.close, deadline)
            except Exception as error:
                close_errors[type(error).__name__] += 1
        if host:
            host.close(deadline)
            close_errors.update(host.close_errors)
        if worker:
            worker.join(max(0, deadline - time.monotonic()))
        if client:
            try:
                resources.call("batch-es-client-close", client.close, deadline)
            except Exception as error:
                close_errors[type(error).__name__] += 1
        alive = resources.alive()
        if host:
            alive.update(host.resources.alive())
            alive["http_active_requests"] = bool(port.active)
        if collector:
            alive["collector"] = collector.snapshot().worker_alive
        report["resources_alive"] = alive
        report["close_errors"] = dict(close_errors)
        report["finished_utc"] = utc_now().isoformat()
        if any(alive.values()) or close_errors:
            report.setdefault("local_performance", {})["passed"] = False


def _write_report(path, report):
    temporary = path.with_suffix(path.suffix + ".writing")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def _child(args, destination, checkpoint):
    last = 0.0
    def progress(value):
        nonlocal last
        now = time.monotonic()
        if now - last >= 1:
            _write_report(checkpoint, value)
            last = now
    try:
        result = run(args, progress=progress)
    except BaseException as error:
        result = getattr(error, "benchmark_report", {
            "run_id": args.run_id, "errors": {type(error).__name__: 1}, "input_complete": False,
            "reconcile_complete": False, "lost_confirmed": 0, "g0_passed": False,
            "local_performance": {"passed": False}, "external_requirements_remain": EXTERNAL_REQUIREMENTS})
    _write_report(destination, result)


def watchdog(args, *, context=None) -> dict:
    validate(args)
    destination = args.output or ROOT.parent / "log/temp" / f"g0-batch-{args.run_id}.json"
    destination = destination.resolve()
    if not destination.parent.is_dir():
        raise ValueError("output parent must already exist")
    checkpoint = destination.with_suffix(destination.suffix + ".checkpoint")
    if destination.exists() or checkpoint.exists():
        raise ValueError("output must be a fresh run artifact")
    process = (context or multiprocessing.get_context("spawn")).Process(
        target=_child, args=(args, destination, checkpoint), name="batch-benchmark")
    process.start()
    # 含启动和两次有界清理；只回收本次创建的进程，不把退出当ES撤写权。
    budget = args.startup_timeout + args.duration + args.max_drain + args.reconcile_timeout + 3 * args.close_timeout
    interrupted = False
    try:
        process.join(budget)
    except KeyboardInterrupt:
        interrupted = True
    killed = process.is_alive()
    if killed:
        process.terminate()
        process.join(args.close_timeout)
        if process.is_alive():
            process.kill()
            process.join(args.close_timeout)
    alive = process.is_alive()
    if destination.exists():
        result = json.loads(destination.read_text(encoding="utf-8"))
    else:
        result = {"run_id": args.run_id, "input_complete": False, "reconcile_complete": False,
                  "lost_confirmed": 0, "unknown_pending": int(args.rate * args.duration),
                  "drain_timed_out": True, "worker_alive": alive, "g0_passed": False,
                  "local_performance": {"passed": False}, "errors": {"child_incomplete": 1},
                  "external_requirements_remain": EXTERNAL_REQUIREMENTS}
        if checkpoint.exists():
            result["last_checkpoint"] = json.loads(checkpoint.read_text(encoding="utf-8"))
    result.setdefault("config", {key: str(value) if isinstance(value, Path) else value for key, value in vars(args).items()})
    result.setdefault("source_sha256", source_summary())
    result["watchdog"] = {"budget_seconds": budget, "terminated": killed, "interrupted": interrupted,
                          "child_alive": alive, "pid": process.pid, "exitcode": process.exitcode,
                          "local_process_reaped": not alive,
                          "server_inflight_writes_unresolved": True,
                          "server_write_fencing_proven": False,
                          "deployment_write_fencing": False}
    if killed or alive:
        result["local_performance"] = {"passed": False}
        result["reconcile_complete"] = False
        result["lost_confirmed"] = 0
        result.setdefault("errors", {})["watchdog_interrupted" if interrupted else "watchdog_timeout"] = 1
    if not alive:
        process.close()
    _write_report(destination, result)
    return result


if __name__ == "__main__":
    summary = watchdog(arguments())
    print(json.dumps({key: value for key, value in summary.items() if key not in {"inputs", "collector_samples", "reconciled"}},
                     ensure_ascii=False))
