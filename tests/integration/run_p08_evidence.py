"""P08 证据补档：跑 P08-B/D 真实 UAT 集成测试，原始输出存 log/temp。

按用户裁决（2026-09-26 04:30）：
- 重跑 9 项 UAT 集成测试（test_cross_day_uat.py 3 项 + test_cleanup_uat.py 6 项）；
- 原始 stdout/stderr 完整保存到 log/temp/p08-evidence-<ts>.log；
- 每项测试独立执行，输出 PASSED/FAILED + 测试名 + 耗时。

跑法：P08_CONFIRM_UAT=1 python tests/integration/run_p08_evidence.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path("C:/Users/ASUS/Desktop/文本去重/workspace-dedup/news-flash-dedup")


def run_test(test_file: str, test_name: str, venv_python: str,
             per_test_log_dir: Path) -> dict:
    """跑单个测试，stdout/stderr 落盘到 per_test_log_dir。

    每个测试的 stdout + stderr 独立保存到 {test_name}.log；最后再汇总。
    """
    per_test_log_dir.mkdir(parents=True, exist_ok=True)
    per_test_log = per_test_log_dir / f"{test_name}.log"
    cmd = [
        venv_python, "-B", "-m", "pytest",
        "-v", "--tb=short",
        f"--basetemp={ROOT / 'log' / 'temp' / f'p08-evidence-{test_name}'}",
        f"tests/integration/{test_file}",
    ]
    if test_name:
        cmd.extend(["-k", test_name])
    env = dict(os.environ)
    env["P08_CONFIRM_UAT"] = "1"
    env["PYTHONPATH"] = f"{ROOT / 'src'}"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    started = time.monotonic()
    proc = subprocess.run(cmd, cwd=str(ROOT), env=env, capture_output=True, text=True,
                          timeout=600)
    elapsed = time.monotonic() - started
    # 把 stdout/stderr 实际写到文件（每个测试一个 log）
    with open(per_test_log, "w", encoding="utf-8") as f:
        f.write(f"# cmd: {' '.join(cmd)}\n")
        f.write(f"# returncode: {proc.returncode}\n")
        f.write(f"# elapsed_seconds: {round(elapsed, 3)}\n\n")
        f.write("--- STDOUT ---\n")
        f.write(proc.stdout)
        f.write("\n--- STDERR ---\n")
        f.write(proc.stderr)
    return {
        "test_name": test_name or "(all)",
        "test_file": test_file,
        "cmd": " ".join(cmd),
        "returncode": proc.returncode,
        "elapsed_seconds": round(elapsed, 3),
        "stdout": proc.stdout,
        "stderr": proc.stderr,
        "per_test_log": str(per_test_log),
        "per_test_log_size_bytes": per_test_log.stat().st_size,
    }


def main() -> int:
    if os.environ.get("P08_CONFIRM_UAT") != "1":
        print("ERROR: must run with P08_CONFIRM_UAT=1")
        return 2

    venv_python = str(ROOT / ".venv-v1" / "Scripts" / "python.exe")
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log_dir = ROOT / "log" / "temp"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"p08-evidence-{ts}.log"
    per_test_log_dir = log_dir / f"p08-evidence-{ts}-per-test"

    results = []
    # P08-B：跨日对账（test_cross_day_uat.py 3 项）
    p08b_tests = [
        "test_cross_day_retry_keeps_first_business_date_and_arrival_seq",
        "test_cross_day_retry_lands_physical_record_in_D_index_not_D1",
        "test_d_plus_one_new_id_same_text_does_not_recall_d_index",
    ]
    # P08-D：物理清理（test_cleanup_uat.py 6 项）
    p08d_tests = [
        "test_dry_run_lists_only_d_minus_seven_targets_not_invaders",
        "test_execute_cleanup_removes_only_d_minus_seven_preserves_d_minus_six",
        "test_dry_run_does_not_delete_anything",
        "test_cleanup_rejects_invalid_target_name_on_force_injection",
        "test_cold_startup_after_cleanup_confirms_d_minus_seven_gone_d_minus_six_present",
        "test_audit_log_records_operator_today_and_index_names",
    ]

    print(f"=== P08 证据补档（{ts}）===")
    print(f"汇总日志: {log_path}")
    print(f"逐项日志: {per_test_log_dir}/")

    for name in p08b_tests:
        print(f"--- 跑 P08-B / {name} ---")
        result = run_test("test_cross_day_uat.py", name, venv_python, per_test_log_dir)
        results.append(result)
        print(f"returncode={result['returncode']} elapsed={result['elapsed_seconds']}s "
              f"per_test_log_size={result['per_test_log_size_bytes']}")

    for name in p08d_tests:
        print(f"--- 跑 P08-D / {name} ---")
        result = run_test("test_cleanup_uat.py", name, venv_python, per_test_log_dir)
        results.append(result)
        print(f"returncode={result['returncode']} elapsed={result['elapsed_seconds']}s "
              f"per_test_log_size={result['per_test_log_size_bytes']}")

    # 写汇总日志（包含每项 per_test_log 路径与字节数）
    with open(log_path, "w", encoding="utf-8") as f:
        f.write(f"P08 证据补档 {ts}\n")
        f.write(f"P08-B（3 项）：{', '.join(p08b_tests)}\n")
        f.write(f"P08-D（6 项）：{', '.join(p08d_tests)}\n")
        f.write("=" * 60 + "\n\n")
        for r in results:
            f.write(f"\n{'#' * 60}\n")
            f.write(f"### {r['test_file']} :: {r['test_name']}\n")
            f.write(f"returncode: {r['returncode']}, elapsed: {r['elapsed_seconds']}s\n")
            f.write(f"per_test_log: {r['per_test_log']} ({r['per_test_log_size_bytes']} bytes)\n")
            f.write(f"cmd: {r['cmd']}\n")
            f.write(f"{'#' * 60}\n")
            f.write("--- STDOUT ---\n")
            f.write(r["stdout"])
            f.write("\n--- STDERR ---\n")
            f.write(r["stderr"])
            f.write("\n")

    # 写 size 报告（账本用）
    total_size = sum(r["per_test_log_size_bytes"] for r in results)
    summary_path = log_dir / f"p08-evidence-{ts}-size.txt"
    with open(summary_path, "w", encoding="utf-8") as f:
        f.write(f"p08-evidence-summary {ts}\n")
        f.write(f"summary_log: {log_path} ({log_path.stat().st_size} bytes)\n")
        f.write(f"per_test_log_dir: {per_test_log_dir}/\n")
        f.write(f"total_per_test_logs_bytes: {total_size}\n")
        for r in results:
            f.write(f"  - {r['test_name']}: {r['per_test_log_size_bytes']} bytes\n")

    passed = sum(1 for r in results if r["returncode"] == 0)
    failed = sum(1 for r in results if r["returncode"] != 0)
    print(f"\n=== P08 证据补档汇总：passed={passed}/{len(results)}, failed={failed} ===")
    print(f"汇总日志: {log_path} ({log_path.stat().st_size} bytes)")
    print(f"逐项日志: {per_test_log_dir}/ (total {total_size} bytes)")
    print(f"size 报告: {summary_path}")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())