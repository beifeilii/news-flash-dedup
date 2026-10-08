"""集群卫生 dry-run：列 UAT ES 所有 p01-* 索引；按用户指令分类为删/留/其它。

严格按清理纪律：dry-run 不删任何索引；输出清单与用户给的 ① 删列表 + ② 留列表逐字核对；
多一个名字都停下报告；不删任何与指令不符的索引。
"""
from __future__ import annotations

import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone

ROOT = "C:/Users/ASUS/Desktop/文本去重/workspace-dedup/news-flash-dedup"
sys.path.insert(0, ROOT + "/src")


def _uat_env_ready() -> bool:
    return os.environ.get("P08_CONFIRM_UAT") == "1"


if not _uat_env_ready():
    sys.exit("ERROR: must run with P08_CONFIRM_UAT=1")


from news_flash_dedup.es_client import (
    ProductionAccessDenied, assert_test_environment, client_from_environment,
    load_environment,
)

load_environment()
try:
    assert_test_environment()
except ProductionAccessDenied as error:
    sys.exit(f"ERROR: UAT env check failed: {error}")

client = client_from_environment(load_dotenv_first=False)
info = client.info()
assert str(info["version"]["number"]).startswith("8."), info["version"]["number"]


# ===== 用户给的删除清单（①）+ 保留清单（②）=====

# ① 删除：micro925b–micro925m 全系列 + micro926a–micro926v 全系列
# 旧单槽 G0 失败后的所有微测全套
DELETE_LIST = {
    "p01-batch-micro925b-*",
    "p01-batch-micro925c-*",
    "p01-batch-micro925d-*",
    "p01-batch-micro925e-*",
    "p01-batch-micro925f-*",
    "p01-batch-micro925g-*",
    "p01-batch-micro925h-*",
    "p01-batch-micro925i-*",
    "p01-batch-micro925j-*",
    "p01-batch-micro925k-*",
    "p01-batch-micro925l-*",
    "p01-batch-micro925m-*",
    "p01-batch-micro926a-*",
    "p01-batch-micro926b-*",
    "p01-batch-micro926c-*",
    "p01-batch-micro926d-*",
    "p01-batch-micro926e-*",
    "p01-batch-micro926f-*",
    "p01-batch-micro926g-*",
    "p01-batch-micro926h-*",
    "p01-batch-micro926i-*",
    "p01-batch-micro926j-*",
    "p01-batch-micro926k-*",
    "p01-batch-micro926l-*",
    "p01-batch-micro926m-*",
    "p01-batch-micro926n-*",
    "p01-batch-micro926o-*",
    "p01-batch-micro926p-*",
    "p01-batch-micro926q-*",
    "p01-batch-micro926r-*",
    "p01-batch-micro926s-*",
    "p01-batch-micro926t-*",
    "p01-batch-micro926u-*",
    "p01-batch-micro926v-*",
    # 旧单槽 G0 失败（2026-09-23）的三套
    "p01-g0-peak2401-*",
    "p01-g0-probe2401-*",
    "p01-g0-recover2401-*",
}

# ② 保留：封门证据五套
KEEP_LIST = {
    "p01-batch-g0-t1-10s-*",
    "p01-batch-g0-t2-5s-*",
    "p01-batch-g0-t3-5s-*",
    "p01-batch-g0-final-10s-*",
    "p01-batch-recovery-g0recovery-*",
}


def classify(name: str) -> str:
    """逐个 name 独立匹配用户列表；不依赖 by_prefix 桶。"""
    if any(name.startswith(p.rstrip("*")) for p in DELETE_LIST):
        return "delete"
    if any(name.startswith(p.rstrip("*")) for p in KEEP_LIST):
        return "keep"
    return "other"


def main():
    listed = client.indices.get(
        index="p01-*",
        allow_no_indices=True,
        ignore_unavailable=True,
    )
    all_indices = sorted(listed.keys())
    delete_matched = []
    keep_matched = []
    other_p01 = []
    for name in all_indices:
        kind = classify(name)
        if kind == "delete":
            delete_matched.append(name)
        elif kind == "keep":
            keep_matched.append(name)
        else:
            other_p01.append(name)

    report = {
        "ts_utc": datetime.now(timezone.utc).isoformat(),
        "es_version": info["version"]["number"],
        "total_p01_indices": len(all_indices),
        "delete_target_count": len(delete_matched),
        "keep_target_count": len(keep_matched),
        "other_p01_count": len(other_p01),
        "DELETE_TO_DELETE": delete_matched,
        "KEEP_TO_KEEP": keep_matched,
        "OTHER_p01_NOT_IN_USER_LIST": sorted(other_p01),
        "user_delete_list_size": len(DELETE_LIST),
        "user_keep_list_size": len(KEEP_LIST),
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))

    client.close()


if __name__ == "__main__":
    main()