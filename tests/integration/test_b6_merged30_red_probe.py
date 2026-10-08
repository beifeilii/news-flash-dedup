r"""B6/E3 ④：plan_artifact 增补 merged_top30 键候选评估——红测先行探针。

呈裁背景（B4′ 建议面）：Recall@30 与全量合法性可从工件复算的前提是
plan_artifact 携带 merged_top30 键；现 worker.py:122-163 字段集（version/
recall_incomplete/recall_gaps/channel_audits/required/pair_top10/
hash_protected/planning_excluded）无该键。本探针断言该键存在——
**当前实现下必红**（红测先行纪律：红绿跑必须归档，B4′ F-1 补丁⑫）。
红证据落 log\temp\b6-e3-merged30-red-<stamp>.json 呈 B6′/主窗口裁决：
若裁实施=唯一既有面改动候选（worker.plan_artifact 增键+本探针转绿）；
若裁不实施=④ 由双轨件证据面（b6-e3-dual-track-<run>.json 的
merged_top30_by_current）承载，本探针保持红探针档案。

闸：`B6_MERGED30_PROBE=1`（默认关，协奏正常跑不携带本探针）。

跑站命令（红证据归档）：

    $env:PYTHONPATH='src'
    $env:B6_MERGED30_PROBE='1'
    & '.venv-v1\\Scripts\\python.exe' -m pytest -p no:cacheprovider `
        --basetemp '.\\tmp\\b6-m30-probe' -v tests/integration/test_b6_merged30_red_probe.py `
        > ..\\log\\temp\\b6-e3-merged30-probe-run-<HHMM>.txt
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

PROBE_ENV = "B6_MERGED30_PROBE"
_REPO_ROOT = Path(__file__).resolve().parents[2]
_LOG_TEMP = _REPO_ROOT / "log" / "temp"

pytestmark = pytest.mark.skipif(
    os.environ.get(PROBE_ENV) != "1",
    reason=f"④ merged_top30 红探针闸未开（需 {PROBE_ENV}=1；协奏正常跑不携带）",
)


def test_plan_artifact_carries_merged_top30():
    from news_flash_dedup.recall.fusion import freeze_recall_plan
    from news_flash_dedup.recall.models import (
        ChannelResult,
        RecallCandidate,
        RecallRequest,
    )
    from news_flash_dedup.recall.worker import plan_artifact

    request = RecallRequest(
        "default", "2000-01-01", "e" * 64, "i-cur", 2, "央行开展中期借贷便利操作。",
        visible_seq=1, prepared_seq=1, embedding_space_id="s-probe")
    candidate = RecallCandidate("a" * 64, "i-hist", 1, "央行开展中期借贷便利操作。",
                                ("exact",), 1.0, "hash_v1")
    results = tuple(
        ChannelResult(channel, "complete",
                      (candidate,) if channel == "hash" else (),
                      f"{channel}_v1", visible_seq=1, prepared_seq=1,
                      coverage_complete=True)
        for channel in ("hash", "near", "bm25", "embedding", "entity"))
    plan = freeze_recall_plan(request, results)
    assert [f.candidate.record_id for f in plan.merged_top30] == ["a" * 64]
    artifact = plan_artifact(plan, request)
    stamp = datetime.now(timezone.utc).strftime("%H%M%S%f")   # W-R3b 秒级→微秒
    evidence = {
        "probe": "plan_artifact 增补 merged_top30 键（④ 呈裁候选）",
        "stamp": stamp,
        "artifact_keys_now": sorted(artifact),
        "expectation": "merged_top30 ∈ artifact keys",
        "actual": "merged_top30" in artifact,
        "plan_merged_top30_truth": ["a" * 64][: len(plan.merged_top30)],
        "note": ("现 worker.py:122-163 字段集无 merged_top30 键→本测当前必红；"
                 "红证据呈裁：裁实施=worker 增键（唯一既有面改动候选，须先呈裁）"
                 "+本探针转绿；裁不实施=④ 由双轨件证据面承载"),
    }
    out = _LOG_TEMP / f"b6-e3-merged30-red-{stamp}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(evidence, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    print(f"[B6-M30] red probe evidence -> {out}")
    assert "merged_top30" in artifact, (
        f"plan_artifact 缺 merged_top30 键（④ 候选未实施；keys="
        f"{sorted(artifact)}）——红测先行预期形态，证据已归档呈裁")
    assert artifact["merged_top30"] == ["a" * 64]
