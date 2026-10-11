"""P1a-T6 回退脚本（蓝图 §3.3：逆向 CAS，纯数据操作；不改代码）。

用法（UAT ES，环境边界 P07 §10 同其它脚本——只连 TEST_ES_* 受批准
集群，非 test 环境 fail-fast 拒启动）：

  python scripts/rollback_batch_head.py --epoch 20261011 [--prefix p01-]

行为 = ``work_queue.migration.rollback_batch_head``：
- 读 ``admission_head_backup_<epoch>`` 备份快照 → 逆向 CAS 精确写回
  头文档（pending/last_materialized_seq 随快照恢复；游标三字段随形态
  退役）；
- 回退后 durable 侧形态卫兵自动拒受理（零双写零复活）；旧路径物化
  循环照常排空收敛；任务文档不删（幂等语义见 migration.py docstring）。

演练（Fake ES）由 ``tests/unit/test_p1a_t6_migration.py::
test_rollback_drill_restores_legacy_and_drains_without_revival`` 全流程
钉住——本脚本仅薄 CLI 壳（连接+调用+JSON 结果打印）。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from news_flash_dedup.batch_es_store import ElasticsearchBatchStore  # noqa: E402
from news_flash_dedup.es_client import client_from_environment  # noqa: E402
from news_flash_dedup.work_queue.migration import (  # noqa: E402
    MigrationConflict, MigrationUnknown, rollback_batch_head,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Roll back the P1a durable-queue batch head (pure data "
                    "operation; no code change, no dual-write).")
    parser.add_argument("--epoch", type=int, required=True,
                        help="migration epoch to roll back (positive int)")
    parser.add_argument("--prefix", default="",
                        help="index prefix (default: none)")
    args = parser.parse_args(argv)

    client = client_from_environment(load_dotenv_first=True)
    store = ElasticsearchBatchStore(client, index_prefix=args.prefix)
    try:
        result = rollback_batch_head(
            store, epoch=args.epoch, clock=lambda: datetime.now(timezone.utc))
    except (MigrationConflict, MigrationUnknown) as error:
        print(json.dumps({"restored": False, "error": str(error)},
                         ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
