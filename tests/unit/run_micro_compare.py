"""串行跑四轮 G0 微测对比：polling vs event-driven × 10/s / 30/s。

仅连接 UAT ES + 本机回环 HTTP；不动 ES 集群、写权隔离或共享索引。
所有 run 用独立 prefix，结果 JSON 写到 log/temp/g0-batch-{run_id}.json。
"""
from __future__ import annotations

import argparse
import json
import multiprocessing
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

import bench_batch_admission as bench


def arguments():
    parser = argparse.ArgumentParser(description="P01 事件驱动微测对比")
    parser.add_argument("--rate", type=int, required=True)
    parser.add_argument("--duration", type=float, required=True)
    parser.add_argument("--materializer-mode", choices=("polling", "event-driven"), required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--confirm-uat", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--collector-wait", type=float, default=None)
    parser.add_argument("--materializer-loop-cap", type=int, default=None)
    parser.add_argument("--max-batch-items", type=int, default=None)
    parser.add_argument("--materializer-poll-floor", type=float, default=None)
    return parser.parse_args()


def main():
    args = arguments()
    extra = []
    for name in ("collector_wait", "materializer_loop_cap", "max_batch_items",
                 "materializer_poll_floor"):
        value = getattr(args, name)
        if value is not None:
            extra += ["--" + name.replace("_", "-"), str(value)]
    bench_args = bench.arguments([
        "--rate", str(args.rate),
        "--duration", str(args.duration),
        "--run-id", args.run_id,
        "--materializer-mode", args.materializer_mode,
        "--output", str(args.output),
    ] + (["--confirm-uat"] if args.confirm_uat else []) + extra)
    started = time.monotonic()
    result = bench.watchdog(bench_args, context=multiprocessing.get_context("spawn"))
    elapsed = time.monotonic() - started
    # 输出关键指标到 stdout
    summary = {
        "run_id": args.run_id,
        "rate": args.rate,
        "duration": args.duration,
        "materializer_mode": args.materializer_mode,
        "elapsed_seconds": round(elapsed, 3),
        "counts": result.get("counts", {}),
        "client_responses": result.get("client_responses", {}),
        "server_responses": result.get("server_responses", {}),
        "local_performance": result.get("local_performance", {}),
        "drain_seconds": result.get("drain_seconds"),
        "drain_timed_out": result.get("drain_timed_out"),
        "reconcile_complete": result.get("reconcile_complete"),
        "four_document_complete": result.get("four_document_complete"),
        "lost_confirmed": result.get("lost_confirmed"),
        "unknown_pending": result.get("unknown_pending"),
        "watermarks": result.get("watermarks", {}),
        "errors": result.get("errors", {}),
        "latency_accepted_p95": (result.get("latency_by_outcome", {})
                                 .get("accepted", {})
                                 .get("planned_to_response", {})
                                 .get("p95_ms")),
    }
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())