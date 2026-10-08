"""隔离 UAT ES 的 G0 单槽受理基线，不发送模型或回调请求。"""

from __future__ import annotations

import argparse
import json
import os
import queue
import statistics
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv
from elasticsearch import Elasticsearch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
APPROVED_UAT_ES = "es-cn-9fr4srbma0001lus6.elasticsearch.aliyuncs.com"

from news_flash_dedup.admission import (  # noqa: E402
    AdmissionCoordinator, AdmissionRequest, AdmissionUnknown, BUSINESS_ZONE,
)
from news_flash_dedup.es_admission_schema import (  # noqa: E402
    control_mapping, item_mapping, request_mapping,
)
from news_flash_dedup.es_admission_store import ElasticsearchAdmissionStore  # noqa: E402


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="P01 UAT ES 单槽完整周期基线")
    parser.add_argument("--rate", type=int, required=True)
    parser.add_argument("--duration", type=int, required=True)
    parser.add_argument("--queue-capacity", type=int, default=1000)
    parser.add_argument("--confirm-uat", action="store_true")
    parser.add_argument("--probe-only", action="store_true")
    parser.add_argument("--run-id", default=uuid.uuid4().hex[:12])
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def client_from_environment() -> Elasticsearch:
    load_dotenv(ROOT / ".env")
    if os.getenv("DEPLOY_ENV", "test").lower() != "test":
        raise RuntimeError("G0 benchmark only permits DEPLOY_ENV=test")
    # W2 批78（L-6/L-7）：与 es_client.assert_test_environment 同口径补齐两项——
    if "PROD_ES_HOST" in os.environ:
        raise RuntimeError("PROD_ES_HOST must remain unset (G0 benchmark fail-closed)")
    if os.getenv("ALLOW_PROD_WRITE", "").strip().lower() not in {"", "false", "0", "no"}:
        raise RuntimeError("ALLOW_PROD_WRITE must be unset/false/0/no (G0 benchmark fail-closed)")
    host = os.environ["TEST_ES_HOST"]
    port = int(os.getenv("TEST_ES_PORT", "9200"))
    scheme = os.getenv("TEST_ES_SCHEME", "http").lower()
    if (host, port, scheme) != (APPROVED_UAT_ES, 9200, "http"):
        raise RuntimeError("G0 target is not the approved UAT ES endpoint")
    return Elasticsearch(
        f"{scheme}://{host}:{port}",
        basic_auth=(os.environ["TEST_ES_USER"], os.environ["TEST_ES_PASS"]),
        request_timeout=5,
        max_retries=0,
        retry_on_timeout=False,
    )


def provision(client: Elasticsearch, prefix: str) -> None:
    if not prefix.startswith("p01-g0-"):
        raise ValueError("isolated G0 prefix required")
    now = datetime.now(timezone.utc)
    dates = {(now + timedelta(days=offset)).astimezone(BUSINESS_ZONE).date()
             for offset in (0, 1)}
    indices = {
        prefix + "news-dedup-control-v1": control_mapping(),
        prefix + "news-dedup-requests-v1": request_mapping(),
    }
    for day in dates:
        indices[prefix + "news-dedup-items-v1-" + day.strftime("%Y.%m.%d")] = item_mapping()
    for name, mapping in indices.items():
        if client.indices.exists(index=name):
            raise RuntimeError(f"G0 index already exists: {name}")
        client.indices.create(index=name, body=mapping)


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int((len(ordered) - 1) * fraction))]


def run(args: argparse.Namespace) -> dict:
    if not args.confirm_uat and not args.probe_only:
        raise RuntimeError("explicit --confirm-uat is required for test ES writes")
    if args.rate <= 0 or args.duration <= 0 or args.queue_capacity <= 0:
        raise ValueError("rate, duration and queue capacity must be positive")
    if not args.run_id.replace("-", "").isalnum():
        raise ValueError("run-id must be alphanumeric or hyphenated")
    prefix = f"p01-g0-{args.run_id}-"
    client = client_from_environment()
    info = client.info()
    if not str(info["version"]["number"]).startswith("8."):
        raise RuntimeError("G0 requires Elasticsearch 8.x")
    if args.probe_only:
        return {"read_only_probe": True, "es_version": info["version"]["number"]}
    provision(client, prefix)
    store = ElasticsearchAdmissionStore(client, index_prefix=prefix)
    admission = AdmissionCoordinator(
        store, owner_id="g0-" + args.run_id,
        owner_isolated=lambda: True,
    )
    work: queue.Queue[tuple[int, float, datetime] | None] = queue.Queue(maxsize=args.queue_capacity)
    metrics = {"input": 0, "accepted": 0, "rejected_429": 0,
               "unknown": 0, "failed": 0, "latency_ms": [],
               "max_queue_depth": 0, "max_wait_ms": 0.0}
    submitted: list[tuple[str, str]] = []
    error_types: dict[str, int] = {}
    started_utc = datetime.now(timezone.utc).isoformat()
    started = time.monotonic()

    def worker() -> None:
        while True:
            item = work.get()
            if item is None:
                work.task_done()
                return
            number, enqueued_at, received_at = item
            accepted_at = time.monotonic()
            metrics["max_wait_ms"] = max(metrics["max_wait_ms"], (accepted_at - enqueued_at) * 1000)
            identity = f"{number + 1}-1"
            try:
                admission.accept(AdmissionRequest(
                    scope_id="default", request_id=identity, item_id=identity,
                    text=f"G0 固定正文 {number + 1}。", received_at=received_at,
                    schema_version="1", pipeline_version="g0-fixed-v1",
                    embedding_space_id="none", delivery_route_ref="g0-no-send",
                    trace_id=f"g0-{args.run_id}-{number + 1}",
                ))
                metrics["accepted"] += 1
                metrics["latency_ms"].append((time.monotonic() - enqueued_at) * 1000)
                submitted.append((identity, "accepted"))
            except AdmissionUnknown:
                metrics["unknown"] += 1
                submitted.append((identity, "unknown"))
            except Exception as error:
                metrics["failed"] += 1
                name = type(error).__name__
                error_types[name] = error_types.get(name, 0) + 1
            finally:
                work.task_done()

    thread = threading.Thread(target=worker, name="g0-admission", daemon=False)
    thread.start()
    total = args.rate * args.duration
    for number in range(total):
        target = started + number / args.rate
        remain = target - time.monotonic()
        if remain > 0:
            time.sleep(remain)
        metrics["input"] += 1
        try:
            work.put_nowait((number, time.monotonic(), datetime.now(timezone.utc)))
            metrics["max_queue_depth"] = max(metrics["max_queue_depth"], work.qsize())
        except queue.Full:
            metrics["rejected_429"] += 1
        if number > 0 and number % max(1, args.rate * 30) == 0:
            print(json.dumps({
                "phase": "input", "elapsed_seconds": round(time.monotonic() - started, 1),
                "input": metrics["input"], "accepted": metrics["accepted"],
                "rejected_429": metrics["rejected_429"], "unknown": metrics["unknown"],
                "queue_depth": work.qsize(),
            }), file=sys.stderr, flush=True)
    input_stopped = time.monotonic()
    work.join()
    drain_seconds = time.monotonic() - input_stopped
    work.put(None)
    thread.join()
    recovery_error = None
    try:
        admission.recover()
    except Exception as error:
        recovery_error = type(error).__name__
    durable = 0
    lost_202 = 0
    unknown_resolved = 0
    reconcile_errors = 0
    for identity, outcome in submitted:
        try:
            found = admission.lookup("default", identity)
        except Exception:
            reconcile_errors += 1
            continue
        if found is not None:
            durable += 1
            if outcome == "unknown":
                unknown_resolved += 1
        elif outcome == "accepted":
            lost_202 += 1
    head = store.get("news-dedup-control-v1", "admission_head")
    report = {
        "run_id": args.run_id,
        "started_utc": started_utc,
        "finished_utc": datetime.now(timezone.utc).isoformat(),
        "prefix": prefix,
        "es_version": info["version"]["number"],
        "rate_per_second": args.rate,
        "duration_seconds": args.duration,
        "queue_capacity": args.queue_capacity,
        **{key: value for key, value in metrics.items() if key != "latency_ms"},
        "p50_ms": percentile(metrics["latency_ms"], 0.50),
        "p95_ms": percentile(metrics["latency_ms"], 0.95),
        "p99_ms": percentile(metrics["latency_ms"], 0.99),
        "max_ms": max(metrics["latency_ms"], default=None),
        "mean_ms": statistics.mean(metrics["latency_ms"]) if metrics["latency_ms"] else None,
        "drain_seconds": drain_seconds,
        "head_last_allocated_seq": head["source"]["last_allocated_seq"] if head else None,
        "head_last_materialized_seq": head["source"]["last_materialized_seq"] if head else None,
        "submitted_count": len(submitted),
        "durable_reconciled": durable,
        "lost_202": lost_202,
        "unknown_resolved": unknown_resolved,
        "unknown_unresolved": metrics["unknown"] - unknown_resolved,
        "reconcile_errors": reconcile_errors,
        "recovery_error": recovery_error,
        "error_types": error_types,
        "latencies_ms": metrics["latency_ms"],
        "rto_seconds": None,
    }
    client.close()
    return report


if __name__ == "__main__":
    options = arguments()
    summary = run(options)
    destination = options.output or ROOT / "tmp" / f"g0-{options.run_id}.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: value for key, value in summary.items() if key != "latencies_ms"},
                     ensure_ascii=False, indent=2))
