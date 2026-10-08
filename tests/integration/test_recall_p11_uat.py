"""P11 隔离 UAT ES 的中文分析器、BM25 与正文实体 term 查询。"""

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
from news_flash_dedup.recall.models import RecallRequest


pytestmark = pytest.mark.skipif(
    os.environ.get("P08_CONFIRM_UAT") != "1",
    reason="P11 UAT ES integration needs explicit P08_CONFIRM_UAT=1",
)


@pytest.fixture(scope="module")
def uat():
    load_environment()
    assert_test_environment()
    client = client_from_environment(load_dotenv_first=False)
    day = datetime.now(BUSINESS_ZONE).date()
    prefix = "p01-batch-p11-" + uuid4().hex[:12] + "-"
    index = prefix + day_index(day.isoformat())
    audit = CleanupAuditLog(operator="p11-integration")
    assert not bool(client.indices.exists(index=index))
    client.indices.create(index=index, body=item_mapping())
    try:
        text = "美国能源信息署公布原油库存增加，证券代码：000010.SZ。"
        expiry = datetime.combine(day + timedelta(days=7), time.min,
                                  tzinfo=BUSINESS_ZONE).astimezone(timezone.utc)
        document = {
            "scope_id": "default", "business_date": day.isoformat(),
            "record_id": "p11-r1", "item_id": "p11-i1", "arrival_seq": 1,
            "text": text, "entity_ids": list(extract_body_entities(text).entity_ids),
            "expires_at": expiry.isoformat(),
        }
        client.index(index=index, id="p11-r1", document=document,
                     op_type="create", refresh=True)
        yield client, prefix, index, day
    finally:
        cleanup_today = day + timedelta(days=7)
        dry = dry_run_cleanup(client, cleanup_today, audit, index_prefix=prefix)
        assert dry["would_delete"] == [index]
        executed = execute_cleanup(client, cleanup_today, audit, index_prefix=prefix)
        assert executed["deleted"] == [index]
        log_dir = Path(__file__).resolve().parents[3] / "log" / "temp"
        log_dir.mkdir(parents=True, exist_ok=True)
        path = log_dir / ("p11-uat-cleanup-" + prefix.removeprefix("p01-batch-").removesuffix("-") + ".json")
        path.write_text(json.dumps({"dry_run": dry, "execution": executed,
                                    "audit": {"dry_run_runs": audit.dry_run_runs,
                                              "deletion_runs": audit.deletion_runs}},
                                   ensure_ascii=False, indent=2), encoding="utf-8")
        client.close()


def test_dedup_cjk_actual_tokens_and_bm25_full_record_readback(uat):
    client, prefix, index, day = uat
    analyzed = client.indices.analyze(index=index, body={
        "analyzer": "dedup_cjk",
        "text": "甲公司代码000010.SZ同比增长3%",
    })
    tokens = [token["token"] for token in analyzed["tokens"]]
    assert any("000010" in token for token in tokens)
    assert any("3" in token for token in tokens)
    assert any(any("\u4e00" <= char <= "\u9fff" for char in token) for token in tokens)

    request = RecallRequest("default", day.isoformat(), "p11-current", "p11-current-item",
                            2, "美国能源信息署公布原油库存增加", visible_seq=1)
    result = BM25Channel(client, prefix).search(request)
    assert result.status == "complete"
    assert result.coverage_complete is True
    assert [candidate.record_id for candidate in result.candidates] == ["p11-r1"]
    assert "证券代码：000010.SZ" in result.candidates[0].text


def test_entity_alias_uses_real_keyword_terms_but_keeps_unresolved_status(uat):
    client, prefix, index, day = uat
    direct = client.search(index=index, body={"query": {"term": {
        "entity_ids": "entity_v1|org:美国能源信息署",
    }}})
    assert [hit["_id"] for hit in direct["hits"]["hits"]] == ["p11-r1"]
    request = RecallRequest("default", day.isoformat(), "p11-current", "p11-current-item",
                            2, "EIA公布原油库存增加", visible_seq=1)
    result = EntityChannel(client, prefix).search(request)
    assert result.status == "unavailable"
    assert result.error_code == "ENTITY_EXTRACTION_INCOMPLETE"
    assert [candidate.record_id for candidate in result.candidates] == ["p11-r1"]
