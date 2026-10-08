"""N31 日索引滚动创建（owner=部署侧 provisioning，09-28 用户批准）。

为指定 business_date 创建 items/audits 日索引，补齐"D+2 起日索引无创建者"
缺口（W2 批37/F2）：
- 命名与映射：10 §2 L24-25（`news-dedup-items-v1-YYYY.MM.DD` /
  `news-dedup-audits-v1-YYYY.MM.DD`，按 business_date 建索引）；映射复用
  `es_admission_schema`（10 §4.2 模板：shards=1、refresh_interval=1s、
  dedup_cjk 分析器）；副本按 UAT 惯例 0——10 §2 L32 明载实际副本由压测决定；
- 前缀口径与 W2Fδ2 四位一体裁定一致：缺省 "" = 10 §2 canonical 生产形；
  非空必须全匹配 `p01-batch-[A-Za-z0-9-]+-`（UAT 隔离运行形，fail-closed）；
- 硬正则校验：每个目标名构造期即过 lifecycle 日索引硬正则闸（双兼容形），
  不过即拒，永不创建异形名；
- 幂等：已存在跳过（HEAD 点验，不重建不报错），重复执行安全；
- 默认 dry-run（只报计划零写）；`--execute` 才实际创建；
- 环境闸：仅 UAT（DEPLOY_ENV=test，经 es_client.assert_test_environment，
  由 client_from_environment 内含执行）。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Mapping
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from news_flash_dedup.es_admission_index import ProvisionError  # noqa: E402
from news_flash_dedup.es_admission_schema import audit_mapping, day_index, item_mapping  # noqa: E402
from news_flash_dedup.lifecycle import is_valid_cleanup_target  # noqa: E402

_PREFIX_PATTERN = re.compile(r"p01-batch-[A-Za-z0-9-]+-")


def rollover_indices(business_date: date, index_prefix: str = "") -> dict[str, dict]:
    """指定业务日的 items/audits 日索引名 → mapping（复用 schema + 10 §4.2 settings）。

    前缀口径与 W2Fδ2 四位一体裁定一致；每个生成名过硬正则闸（fail-closed，
    永不产异形名）。
    """
    if type(business_date) is not date:
        raise ValueError("business_date must be a datetime.date")
    if index_prefix and not _PREFIX_PATTERN.fullmatch(index_prefix):
        raise ValueError("isolated prefix required (p01-batch-*)")
    iso = business_date.isoformat()
    mappings = {
        index_prefix + day_index(iso): item_mapping(),
        index_prefix + day_index(iso, "audits"): audit_mapping(),
    }
    for mapping in mappings.values():
        # 与 es_admission_index.required_indices 同口径：shards=1、replicas=0
        # （UAT 惯例；10 §2 L32：实际副本由压测决定）。
        mapping.setdefault("settings", {}).update(number_of_shards=1, number_of_replicas=0)
    for name in mappings:
        if not is_valid_cleanup_target(name):
            raise ValueError(f"refused invalid rollover index name: {name}")
    return mappings


def plan_day_rollover(client: Any, business_date: date, index_prefix: str = "") -> dict:
    """dry-run 计划：HEAD 逐名点验在场性（只读），只报将创建/已存在，零写。"""
    mappings = rollover_indices(business_date, index_prefix)
    present = {name: bool(client.indices.exists(index=name)) for name in mappings}
    return {
        "business_date": business_date.isoformat(),
        "index_prefix": index_prefix,
        "dry_run": True,
        "would_create": [name for name in mappings if not present[name]],
        "skipped_existing": [name for name in mappings if present[name]],
    }


def execute_day_rollover(client: Any, business_date: date, index_prefix: str = "") -> dict:
    """实际创建缺失的 items/audits 日索引；已存在跳过（幂等）。

    逐名点名创建（不用宽通配符）；执行前硬正则复闸；create 异常以
    ProvisionError("index_create_failed") 保链包装；未 ack 不冒充成功。
    """
    mappings = rollover_indices(business_date, index_prefix)
    report: dict[str, Any] = {
        "business_date": business_date.isoformat(),
        "index_prefix": index_prefix,
        "dry_run": False,
        "created": [],
        "skipped_existing": [],
    }
    for name, schema in mappings.items():
        if not is_valid_cleanup_target(name):  # 执行前硬正则复闸（fail-closed）
            raise ValueError(f"refused invalid rollover index name: {name}")
        if bool(client.indices.exists(index=name)):
            report["skipped_existing"].append(name)
            continue
        try:
            response = client.indices.create(index=name, body=schema)
        except Exception as error:
            raise ProvisionError("index_create_failed") from error
        body = getattr(response, "body", response)
        if not isinstance(body, Mapping) or body.get("acknowledged") is not True:
            raise RuntimeError(f"day rollover index creation is not acknowledged: {name}")
        report["created"].append(name)
    return report


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="N31 日索引滚动创建（默认 dry-run 零写；--execute 才建）")
    parser.add_argument("--business-date", type=date.fromisoformat, required=True,
                        help="业务日 YYYY-MM-DD（10 §2：按 business_date 建索引）")
    parser.add_argument("--index-prefix", default="",
                        help="缺省 canonical 生产形；UAT 隔离形 p01-batch-<run-id>-")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", dest="execute", action="store_false", default=False,
                      help="（默认）只报计划，零写")
    mode.add_argument("--execute", dest="execute", action="store_true",
                      help="实际创建缺失日索引（幂等，已存在跳过）")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    """CLI 入口：经 client_from_environment 取 UAT 客户端（内含 DEPLOY_ENV=test 闸）。"""
    args = parse_args(argv)
    from news_flash_dedup.es_client import client_from_environment
    client = client_from_environment()
    try:
        report = (execute_day_rollover(client, args.business_date, args.index_prefix)
                  if args.execute else
                  plan_day_rollover(client, args.business_date, args.index_prefix))
    finally:
        client.close()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
