"""P12 测试 Milvus 八字段/Strong ANN 与隔离 UAT ES 回查。"""

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
from news_flash_dedup.milvus_client import (
    assert_test_milvus_environment, build_test_milvus_client,
    collection_name_for_test, partition_name,
)
from news_flash_dedup.recall.models import RecallRequest
from news_flash_dedup.recall.vector_space import EmbeddingSpace
from news_flash_dedup.recall.vector_store import (
    MilvusVectorStore, prepare_vector_row, vector_index_params, vector_schema,
)


pytestmark = pytest.mark.skipif(
    os.environ.get("P12_CONFIRM_UAT") != "1",
    reason="P12 real test clusters require explicit P12_CONFIRM_UAT=1",
)


def _space():
    return EmbeddingSpace(
        "mock_embedding", "uat_fixture_v1", "codepoint_v1", "none", "COSINE", 4,
        "plain_v1", "plain_v1", "tokens512_overlap64_v1", "p09_v1",
    )


@pytest.fixture(scope="module")
def uat():
    load_environment()
    assert_test_environment()
    assert_test_milvus_environment()
    es = client_from_environment(load_dotenv_first=False)
    milvus = build_test_milvus_client()
    day = datetime.now(BUSINESS_ZONE).date()
    run_id = uuid4().hex[:12]
    es_prefix = "p01-batch-p12-" + run_id + "-"
    index = es_prefix + day_index(day.isoformat())
    space = _space()
    collection = collection_name_for_test(run_id, space.space_id)
    second_run_id = uuid4().hex[:12]
    second_collection = collection_name_for_test(second_run_id, space.space_id)
    partition = partition_name(day.isoformat())
    audit = CleanupAuditLog(operator="p12-integration")
    assert not bool(es.indices.exists(index=index))
    assert milvus.has_collection(collection_name=collection) is False
    assert milvus.has_collection(collection_name=second_collection) is False
    created_index = False
    created_collections = []
    try:
        es.indices.create(index=index, body=item_mapping())
        created_index = True
        milvus.create_collection(collection_name=collection, schema=vector_schema(space),
                                 index_params=vector_index_params())
        created_collections.append((run_id, collection))
        milvus.create_partition(collection_name=collection, partition_name=partition)
        milvus.create_collection(collection_name=second_collection,
                                 schema=vector_schema(space),
                                 index_params=vector_index_params())
        created_collections.append((second_run_id, second_collection))
        milvus.create_partition(collection_name=second_collection,
                                partition_name=partition)
        expiry = datetime.combine(day + timedelta(days=7), time.min,
                                  tzinfo=BUSINESS_ZONE).astimezone(timezone.utc)
        record_id = "a" * 64
        es.index(index=index, id=record_id, document={
            "scope_id": "default", "business_date": day.isoformat(),
            "record_id": record_id, "item_id": "p12-item-1", "arrival_seq": 1,
            "text": "甲公司完成回购", "embedding_space_id": space.space_id,
            "expires_at": expiry.isoformat(),
        }, op_type="create", refresh=True)
        yield (es, milvus, es_prefix, index, collection, second_collection,
               partition, day, space)
    finally:
        cleanup = {"milvus_dry_run": [name for _, name in created_collections],
                   "milvus_deleted": []}
        for generated_run_id, generated_name in created_collections:
            assert collection_name_for_test(generated_run_id, space.space_id) == generated_name
            milvus.drop_collection(collection_name=generated_name)
            assert milvus.has_collection(collection_name=generated_name) is False
            cleanup["milvus_deleted"].append(generated_name)
        if created_index:
            cleanup_today = day + timedelta(days=7)
            dry = dry_run_cleanup(es, cleanup_today, audit, index_prefix=es_prefix)
            assert dry["would_delete"] == [index]
            executed = execute_cleanup(es, cleanup_today, audit, index_prefix=es_prefix)
            assert executed["deleted"] == [index]
            cleanup.update(es_dry_run=dry, es_execution=executed,
                           audit={"dry_run_runs": audit.dry_run_runs,
                                  "deletion_runs": audit.deletion_runs})
        log_dir = Path(__file__).resolve().parents[3] / "log" / "temp"
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / f"p12-uat-cleanup-{run_id}.json").write_text(
            json.dumps(cleanup, ensure_ascii=False, indent=2), encoding="utf-8")
        milvus.close()
        es.close()


def test_strict_eight_fields_stable_upsert_and_strong_ann_es_readback(uat):
    es, milvus, es_prefix, index, collection, second_collection, partition, day, space = uat
    description = milvus.describe_collection(collection_name=collection)
    fields = description["fields"]
    assert {field["name"] for field in fields} == {
        "vector_id", "record_id", "scope_id", "business_date", "arrival_seq",
        "embedding_space_id", "chunk_id", "embedding"}
    assert description["enable_dynamic_field"] is False
    assert description["auto_id"] is False
    assert partition in milvus.list_partitions(collection_name=collection)

    store = MilvusVectorStore(milvus, es, collection, es_prefix, space)
    row = prepare_vector_row("a" * 64, "default", day.isoformat(), 1,
                             space, 0, [1.0, 0.0, 0.0, 0.0])
    assert store.upsert(row) == "created"
    assert store.upsert(row) == "reused"
    readback = milvus.get(collection_name=collection, ids=[row["vector_id"]],
                          output_fields=["record_id", "chunk_id"],
                          consistency_level="Strong")
    assert len(readback) == 1
    assert readback[0]["record_id"] == "a" * 64
    request = RecallRequest("default", day.isoformat(), "c" * 64, "current-item", 2,
                            "甲公司完成回购", embedding_space_id=space.space_id)
    result = store.search(request, [1.0, 0.0, 0.0, 0.0])
    assert result.status == "complete", (result.error_code, result.pages)
    assert result.coverage_complete is False
    assert [candidate.record_id for candidate in result.candidates] == ["a" * 64]
    assert result.candidates[0].chunk_ids == (0,)
    isolated = MilvusVectorStore(milvus, es, second_collection,
                                 es_prefix, space).search(request, [1.0, 0.0, 0.0, 0.0])
    assert isolated.status == "complete"
    assert isolated.candidates == ()
