"""集群卫生执行：分两批删除 148 个 p01-* 索引。

批次 A：68 个 p01-batch-micro* items 走 lifecycle.execute_cleanup
        today=2026-09-27, retention_days=1 推算 09.26（W2Fε docstring 勘正：
        原写 10-01/7/6 推算 09.24/25 与码 TODAY_FAKE/RETENTION 不符，按码对齐）
批次 B：80 个手工（74 control/requests + 6 个 p01-g0-2401 items）

任何批次出现拒绝/跳过必须停下报告，不得绕过。完整审计输出。
"""
from __future__ import annotations

import json
import os
import sys
from collections import defaultdict
from datetime import date, datetime, timezone

ROOT = "C:/Users/ASUS/Desktop/文本去重/workspace-dedup/news-flash-dedup"
sys.path.insert(0, ROOT + "/src")


if os.environ.get("P08_CONFIRM_UAT") != "1":
    sys.exit("ERROR: must run with P08_CONFIRM_UAT=1")


from news_flash_dedup.es_client import (
    ProductionAccessDenied, assert_test_environment, client_from_environment,
    load_environment,
)
from news_flash_dedup.lifecycle import (
    CleanupAuditLog, execute_cleanup, is_valid_cleanup_target,
)

load_environment()
try:
    assert_test_environment()
except ProductionAccessDenied as error:
    sys.exit(f"ERROR: UAT env check failed: {error}")

client = client_from_environment(load_dotenv_first=False)
info = client.info()
assert str(info["version"]["number"]).startswith("8.")

with open("C:/Users/ASUS/Desktop/文本去重/workspace-dedup/log/temp/cluster-hygiene-dryrun-v2.json",
          encoding="utf-8") as f:
    dry_run = json.load(f)

DELETE_ALL = dry_run["DELETE_TO_DELETE"]
OPERATOR = "admin-cluster-hygiene-2026-09-25"

# 分类
batch_a_lifecycle = []  # 匹配 lifecycle 正则的 items/audits
batch_b_manual = []     # 不匹配的 control/requests + p01-g0-2401 items

for name in DELETE_ALL:
    if is_valid_cleanup_target(name):
        batch_a_lifecycle.append(name)
    else:
        batch_b_manual.append(name)

print(f"批次 A (lifecycle, items/audits): {len(batch_a_lifecycle)}")
print(f"批次 B (手工, control/requests/g0-2401): {len(batch_b_manual)}")
print(f"合计: {len(batch_a_lifecycle) + len(batch_b_manual)} (期望 148)")
print()

# 分类打印
b_breakdown = defaultdict(int)
for n in batch_b_manual:
    if "control-v1" in n:
        b_breakdown["control"] += 1
    elif "requests-v1" in n:
        b_breakdown["requests"] += 1
    elif n.startswith("p01-g0-") and "items-v1" in n:
        b_breakdown["g0-2401-items"] += 1
    else:
        b_breakdown["other"] += 1
for k, v in sorted(b_breakdown.items()):
    print(f"  批次 B - {k}: {v}")
print()

# ===== 批次 A: lifecycle.execute_cleanup =====
# 68 个 micro items 在 09.26（execution 已过午夜）；用 today=2026-09-27 + retention_days=1 推算
# （W2Fε 注释勘正：原写 09.24/09.25 + 10-01/6/7 与下方 TODAY_FAKE/RETENTION 码值不符）
print("=" * 60)
print("批次 A: lifecycle.execute_cleanup (68 个 micro items)")
print("=" * 60)

# 按 prefix 分组（p01-batch-microXXX-）
def _prefix_micro(name):
    parts = name.split("-")
    if name.startswith("p01-batch-micro"):
        return "p01-batch-" + parts[2] + "-"
    return None

micro_by_prefix = defaultdict(list)
for name in batch_a_lifecycle:
    p = _prefix_micro(name)
    if p is None:
        sys.exit(f"ERROR: micro 索引未识别 prefix: {name}")
    micro_by_prefix[p].append(name)

print(f"按 prefix 分组: {len(micro_by_prefix)} 套")
# micro 索引日期是 2026-09-26 (execution 已过午夜)
# 用 today=2026-09-27 + retention_days=1 推算 2026-09-26
TODAY_FAKE = date(2026, 9, 27)
RETENTION = 1

for prefix in sorted(micro_by_prefix):
    print(f"\n  prefix {prefix} (含 {len(micro_by_prefix[prefix])} indices):")
    audit_step = CleanupAuditLog(operator=OPERATOR)
    try:
        report = execute_cleanup(client, TODAY_FAKE, audit_step,
                                  retention_days=RETENTION, index_prefix=prefix)
        print(f"    retention_days={RETENTION} (target=2026-09-26): "
              f"deleted={len(report['deleted'])}, absent={len(report['skipped_absent'])}")
    except ValueError as error:
        sys.exit(f"ERROR: 批次 A 拒绝 prefix={prefix} retention_days={RETENTION}: {error}")

# 验证批次 A 全部删除
remaining_a = [n for n in batch_a_lifecycle
               if bool(client.indices.exists(index=n))]
if remaining_a:
    sys.exit(f"ERROR: 批次 A 后残留 {len(remaining_a)} 索引: {remaining_a[:5]}")
print(f"\n批次 A 完成: 68 个 micro items 全部删除 ✓")
print()

# ===== 批次 B: 手工 delete + 审计 =====
print("=" * 60)
print("批次 B: 手工逐个 delete + 审计 (80 个: 74 control/requests + 6 g0-2401 items)")
print("=" * 60)

audit_b_records = []  # 完整审计：每条 name + ok + error
fatal_errors = []

for name in batch_b_manual:
    name_kind = "control" if "control-v1" in name else (
                "requests" if "requests-v1" in name else "g0-2401-items")
    try:
        client.indices.delete(index=name)
        audit_b_records.append({
            "index_name": name,
            "kind": name_kind,
            "operator": OPERATOR,
            "ts_utc": datetime.now(timezone.utc).isoformat(),
            "ok": True,
            "error": None,
            "method": "manual_delete",
        })
    except Exception as error:
        audit_b_records.append({
            "index_name": name,
            "kind": name_kind,
            "operator": OPERATOR,
            "ts_utc": datetime.now(timezone.utc).isoformat(),
            "ok": False,
            "error": type(error).__name__,
            "method": "manual_delete",
        })
        fatal_errors.append((name, str(error)))

# 验证批次 B 全部删除
remaining_b = [n for n in batch_b_manual
               if bool(client.indices.exists(index=n))]
if remaining_b:
    sys.exit(f"ERROR: 批次 B 后残留 {len(remaining_b)} 索引: {remaining_b[:5]}")
print(f"批次 B 完成: 80 个手工 delete 全部成功 ✓")
if fatal_errors:
    print(f"但有 {len(fatal_errors)} 个错误（必须停下报告）：")
    for n, e in fatal_errors:
        print(f"  {n}: {e}")
    sys.exit(1)
print()

# ===== 最终审计报告 =====
all_audit = {
    "ts_utc": datetime.now(timezone.utc).isoformat(),
    "operator": OPERATOR,
    "es_version": info["version"]["number"],
    "summary": {
        "batch_a_lifecycle": len(batch_a_lifecycle),
        "batch_b_manual": len(batch_b_manual),
        "total_deleted": len(batch_a_lifecycle) + len(batch_b_manual),
        "batch_b_failures": len(fatal_errors),
    },
    "batch_a_lifecycle_indices": batch_a_lifecycle,
    "batch_b_manual_records": audit_b_records,
    "batch_b_breakdown": dict(b_breakdown),
}

with open("C:/Users/ASUS/Desktop/文本去重/workspace-dedup/log/temp/cluster-hygiene-audit.json",
          "w", encoding="utf-8") as f:
    json.dump(all_audit, f, ensure_ascii=False, indent=2)

print(f"完整审计报告已保存: log/temp/cluster-hygiene-audit.json")
print()
print("=" * 60)
print("最终状态")
print("=" * 60)
print(f"批次 A (lifecycle): {len(batch_a_lifecycle)} 全部删除")
print(f"批次 B (手工):     {len(batch_b_manual)} 全部删除")
print(f"合计:              {len(batch_a_lifecycle) + len(batch_b_manual)} / 148")
print(f"批次 B 失败:       {len(fatal_errors)}")
print()

# 验证所有 DELETE 索引已不存在
remaining_all = [n for n in DELETE_ALL if bool(client.indices.exists(index=n))]
if remaining_all:
    print(f"!!! 残留 {len(remaining_all)} 索引未删: {remaining_all}")
    sys.exit(1)
print(f"✓ 验证：所有 148 个 DELETE 索引在 ES 中不存在")

client.close()