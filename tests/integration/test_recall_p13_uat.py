"""隔离 UAT ES 验证真实通道工件进入 P13 融合。"""

from __future__ import annotations

import json
import os
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from news_flash_dedup.admission import BUSINESS_ZONE
from news_flash_dedup.es_admission_schema import day_index, item_mapping
from news_flash_dedup.es_client import assert_test_environment, client_from_environment, load_environment
from news_flash_dedup.lifecycle import CleanupAuditLog, dry_run_cleanup, execute_cleanup
from news_flash_dedup.recall.bm25_channel import BM25Channel
from news_flash_dedup.recall.entities import extract_body_entities
from news_flash_dedup.recall.entity_channel import EntityChannel
from news_flash_dedup.recall.fusion import freeze_recall_plan
from news_flash_dedup.recall.hash_channel import HashChannel, prepare_recall_fields
from news_flash_dedup.recall.models import ChannelResult, RecallRequest
from news_flash_dedup.recall.near_channel import NearChannel


pytestmark = pytest.mark.skipif(
    os.environ.get("P08_CONFIRM_UAT") != "1",
    reason="P13 UAT ES integration needs explicit P08_CONFIRM_UAT=1",
)


def test_real_es_channel_artifacts_fuse_without_erasing_vector_gap():
    load_environment()
    assert_test_environment()
    es = client_from_environment(load_dotenv_first=False)
    day = datetime.now(BUSINESS_ZONE).date()
    run_id = uuid4().hex[:12]
    prefix = "p01-batch-p13-" + run_id + "-"
    index = prefix + day_index(day.isoformat())
    audit = CleanupAuditLog(operator="p13-integration")
    assert not bool(es.indices.exists(index=index))
    es.indices.create(index=index, body=item_mapping())
    try:
        text = "美国能源信息署公布原油库存增加"
        record_id = "a" * 64
        expiry = datetime.combine(day + timedelta(days=7), time.min,
                                  tzinfo=BUSINESS_ZONE).astimezone(timezone.utc)
        source = {
            "scope_id": "default", "business_date": day.isoformat(),
            "record_id": record_id, "item_id": "p13-i1", "arrival_seq": 1,
            "text": text, "entity_ids": list(extract_body_entities(text).entity_ids),
            "expires_at": expiry.isoformat(), "preparation_state": "ready",
        }
        source.update(prepare_recall_fields(text, "default", day.isoformat()))
        es.index(index=index, id=record_id, document=source,
                 op_type="create", refresh=True)
        request = RecallRequest("default", day.isoformat(), "b" * 64,
                                "p13-current", 2, text,
                                visible_seq=1, prepared_seq=1)
        results = (
            HashChannel(es, prefix).search(request),
            NearChannel(es, prefix).search(request),
            BM25Channel(es, prefix).search(request),
            ChannelResult("embedding", "unavailable", (), "embedding_v1",
                          error_code="SPACE_UNCONFIRMED"),
            EntityChannel(es, prefix).search(request),
        )
        plan = freeze_recall_plan(request, results)
        assert [item.candidate.record_id for item in plan.pair_top10] == [record_id]
        assert [item.candidate.record_id for item in plan.hash_protected] == [record_id]
        assert [item.candidate.record_id for item in plan.required] == [record_id]
        assert {hit.channel for hit in plan.required[0].channel_hits} >= {
            "hash", "near", "bm25"}
        assert {gap.channel for gap in plan.recall_gaps} >= {"embedding", "entity"}
        assert plan.recall_incomplete is True
    finally:
        cleanup_today = day + timedelta(days=7)
        dry = dry_run_cleanup(es, cleanup_today, audit, index_prefix=prefix)
        assert dry["would_delete"] == [index]
        executed = execute_cleanup(es, cleanup_today, audit, index_prefix=prefix)
        assert executed["deleted"] == [index]
        log_dir = Path(__file__).resolve().parents[3] / "log" / "temp"
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / f"p13-uat-cleanup-{run_id}.json").write_text(
            json.dumps({"dry_run": dry, "execution": executed,
                        "audit": {"dry_run_runs": audit.dry_run_runs,
                                  "deletion_runs": audit.deletion_runs}},
                       ensure_ascii=False, indent=2), encoding="utf-8")
        es.close()
