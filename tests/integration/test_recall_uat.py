"""P10 在隔离 UAT ES 验证 strict 主记录上的 Hash/近重复召回。"""

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
from news_flash_dedup.recall.hash_channel import HashChannel, prepare_recall_fields
from news_flash_dedup.recall.models import RecallRequest
from news_flash_dedup.recall.near_channel import NearChannel
from news_flash_dedup.text import normalize_text


pytestmark = pytest.mark.skipif(
    os.environ.get("P08_CONFIRM_UAT") != "1",
    reason="P10 UAT ES integration needs explicit P08_CONFIRM_UAT=1",
)


@pytest.fixture(scope="module")
def uat():
    load_environment()
    assert_test_environment()
    client = client_from_environment(load_dotenv_first=False)
    business_day = datetime.now(BUSINESS_ZONE).date()
    prefix = "p01-batch-p10-" + uuid4().hex[:12] + "-"
    index = prefix + day_index(business_day.isoformat())
    audit = CleanupAuditLog(operator="p10-integration")
    assert not bool(client.indices.exists(index=index))
    client.indices.create(index=index, body=item_mapping())
    try:
        for source in (
            _record("p10-r1", "p10-i1", 1, "甲公司\n完成3亿元回购", business_day),
            _record("p10-r2", "p10-i2", 2, "甲公司\r\n完成3亿元回购", business_day),
            _record("p10-blank", "p10-blank-item", 4, " \t\n", business_day, ready=False),
        ):
            client.index(index=index, id=source["record_id"], document=source,
                         op_type="create", refresh=True)
        yield client, prefix, index, business_day
    finally:
        cleanup_today = business_day + timedelta(days=7)
        dry = dry_run_cleanup(client, cleanup_today, audit, index_prefix=prefix)
        assert dry["would_delete"] == [index]
        executed = execute_cleanup(client, cleanup_today, audit, index_prefix=prefix)
        assert executed["deleted"] == [index]
        log_dir = Path(__file__).resolve().parents[3] / "log" / "temp"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / ("p10-uat-cleanup-" + prefix.removeprefix("p01-batch-").removesuffix("-") + ".json")
        log_path.write_text(json.dumps({
            "dry_run": dry, "execution": executed,
            "audit": {"dry_run_runs": audit.dry_run_runs,
                      "deletion_runs": audit.deletion_runs},
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        client.close()


def _record(record_id, item_id, seq, text, day, *, ready=True):
    expiry = datetime.combine(day + timedelta(days=7), time.min,
                              tzinfo=BUSINESS_ZONE).astimezone(timezone.utc)
    source = {
        "scope_id": "default", "business_date": day.isoformat(),
        "record_id": record_id, "item_id": item_id, "arrival_seq": seq,
        "text": text, "expires_at": expiry.isoformat(),
        "preparation_state": "ready" if ready else "pending",
    }
    if ready:
        source.update(prepare_recall_fields(text, "default", day.isoformat()))
    else:
        # P01 受理物化会保存 raw_hash；它不是 P09 入桶许可。
        source["raw_hash"] = normalize_text(text).raw_hash
    return source


def test_hash_near_real_term_query_and_cold_restart(uat):
    client, prefix, index, day = uat
    lf = "甲公司\n完成3亿元回购"
    crlf = "甲公司\r\n完成3亿元回购"
    first = _record("p10-r1", "p10-i1", 1, lf, day)
    second = _record("p10-r2", "p10-i2", 2, crlf, day)
    raw = client.search(index=index, body={"query": {"term": {"raw_hash": first["raw_hash"]}}})
    normalized = client.search(index=index, body={
        "query": {"term": {"normalized_hash": first["normalized_hash"]}}
    })
    band = client.search(index=index, body={
        "query": {"term": {"simhash_bands": first["simhash_bands"][0]}}
    })
    assert {hit["_id"] for hit in raw["hits"]["hits"]} == {"p10-r1"}
    assert {hit["_id"] for hit in normalized["hits"]["hits"]} == {"p10-r1", "p10-r2"}
    assert {hit["_id"] for hit in band["hits"]["hits"]} == {"p10-r1", "p10-r2"}

    request = RecallRequest("default", day.isoformat(), "p10-current", "p10-current-item",
                            3, lf, visible_seq=2, prepared_seq=2)
    hash_result = HashChannel(client, prefix).search(request)
    near_result = NearChannel(client, prefix).search(request)
    assert hash_result.status == near_result.status == "complete"
    assert hash_result.coverage_complete and near_result.coverage_complete
    assert {candidate.record_id for candidate in hash_result.candidates} == {"p10-r1", "p10-r2"}
    assert {candidate.record_id for candidate in near_result.candidates} == {"p10-r1", "p10-r2"}
    assert all(set(candidate.subpaths) == {"simhash", "minhash"}
               for candidate in near_result.candidates)

    # 新通道实例不依赖进程缓存；数据仍从同一 ES 主记录恢复。
    restarted = HashChannel(client, prefix).search(request)
    assert [candidate.record_id for candidate in restarted.candidates] == [
        candidate.record_id for candidate in hash_result.candidates]


def test_blank_admission_raw_hash_is_not_a_recall_bucket(uat):
    client, prefix, index, day = uat
    blank_hash = normalize_text(" \t\n").raw_hash
    ready_bucket = client.search(index=index, body={"query": {"bool": {"filter": [
        {"term": {"raw_hash": blank_hash}},
        {"term": {"preparation_state": "ready"}},
    ]}}})
    assert ready_bucket["hits"]["hits"] == []
    request = RecallRequest("default", day.isoformat(), "p10-new-blank", "p10-new-item",
                            5, " \t\n", visible_seq=4)
    assert HashChannel(client, prefix).search(request).status == "not_applicable"
    assert NearChannel(client, prefix).search(request).status == "not_applicable"
