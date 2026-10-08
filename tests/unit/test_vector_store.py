"""P12 Milvus 八字段、幂等写入与 chunk→record 回查。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from news_flash_dedup.recall.models import RecallRequest
from news_flash_dedup.recall.vector_space import EmbeddingSpace
from news_flash_dedup.recall.vector_store import (
    MilvusVectorStore, VectorIdentityConflict, VectorWriteUnknown,
    prepare_vector_row, vector_schema,
)

NOW = datetime(2026, 9, 26, 1, tzinfo=timezone.utc)
DAY = "2026-09-26"
RID1 = "a" * 64
RID2 = "b" * 64
PREFIX = "p01-batch-p12-unit-"


def space():
    return EmbeddingSpace("mock_embedding", "fixture_v1", "codepoint_v1", "none",
                          "COSINE", 4, "plain_v1", "plain_v1",
                          "tokens512_overlap64_v1", "p09_v1")


def row(record_id=RID1, chunk_id=0):
    return prepare_vector_row(record_id, "default", DAY, 1, space(), chunk_id,
                              [1.0, 0.0, 0.0, 0.0])


class FakeMilvus:
    def __init__(self):
        self.rows = {}
        self.upserts = []
        self.searches = []
        self.search_rows = []
        self.fail_search = False

    def get(self, *, collection_name, ids, output_fields, consistency_level):
        assert consistency_level == "Strong"
        return [self.rows[key] for key in ids if key in self.rows]

    def upsert(self, *, collection_name, data, partition_name):
        self.upserts.append(data)
        self.rows[data["vector_id"]] = data
        return {"upsert_count": 1}

    def search(self, **kwargs):
        self.searches.append(kwargs)
        if self.fail_search:
            raise RuntimeError("injected outage")
        return [self.search_rows[:kwargs["limit"]]]


class FakeES:
    def __init__(self, docs):
        self.docs = {doc["record_id"]: doc for doc in docs}

    def get(self, *, index, id, realtime):
        assert index == PREFIX + "news-dedup-items-v1-2026.09.26"
        assert realtime is True
        if id not in self.docs:
            return {"_index": index, "_id": id, "found": False}
        return {"_index": index, "_id": id, "found": True,
                "_source": self.docs[id]}


def doc(record_id, item_id, seq):
    return {"scope_id": "default", "business_date": DAY,
            "record_id": record_id, "item_id": item_id, "arrival_seq": seq,
            "text": "甲公司完成回购", "embedding_space_id": space().space_id,
            "expires_at": (NOW + timedelta(days=1)).isoformat()}


def hit(record_id, chunk_id, seq):
    return {"id": f"{record_id}_{chunk_id}", "distance": 0.9,
            "entity": {"record_id": record_id, "scope_id": "default",
                       "business_date": DAY, "arrival_seq": seq,
                       "embedding_space_id": space().space_id,
                       "chunk_id": chunk_id}}


def store(milvus, es):
    return MilvusVectorStore(milvus, es,
                             "news_dedup_replay_abc123_" + space().space_id,
                             PREFIX, space(), clock=lambda: NOW)


def request():
    return RecallRequest("default", DAY, "c" * 64, "current-item", 100,
                         "甲公司完成回购", embedding_space_id=space().space_id)


def test_schema_has_eight_fields_and_no_dynamic_field():
    schema = vector_schema(space())
    assert {field.name for field in schema.fields} == {
        "vector_id", "record_id", "scope_id", "business_date", "arrival_seq",
        "embedding_space_id", "chunk_id", "embedding"}
    assert schema.to_dict()["enable_dynamic_field"] is False
    assert schema.to_dict()["auto_id"] is False


def test_row_has_stable_id_exact_fields_and_no_text():
    value = row()
    assert value["vector_id"] == RID1 + "_0"
    assert set(value) == {"vector_id", "record_id", "scope_id", "business_date",
                          "arrival_seq", "embedding_space_id", "chunk_id", "embedding"}


def test_upsert_reuses_identical_artifact_and_rejects_different_payload():
    milvus = FakeMilvus()
    adapter = store(milvus, FakeES([doc(RID1, "i1", 1)]))
    assert adapter.upsert(row()) == "created"
    assert adapter.upsert(row()) == "reused"
    assert len(milvus.upserts) == 1
    with pytest.raises(VectorIdentityConflict):
        adapter.upsert({**row(), "embedding": [0.0, 1.0, 0.0, 0.0]})


def test_ann_expands_80_chunks_for_unique_records_and_reads_es():
    milvus = FakeMilvus()
    milvus.search_rows = [hit(RID1, chunk, 1) for chunk in range(80)] + [
        hit(RID2, 0, 2)]
    adapter = store(milvus, FakeES([doc(RID1, "i1", 1), doc(RID2, "i2", 2)]))
    result = adapter.search(request(), [1.0, 0.0, 0.0, 0.0])
    assert result.status == "complete"
    assert result.coverage_complete is False
    assert {candidate.record_id for candidate in result.candidates} == {RID1, RID2}
    assert len(next(candidate for candidate in result.candidates
                    if candidate.record_id == RID1).chunk_ids) == 80
    assert [call["limit"] for call in milvus.searches] == [80, 160]
    assert all(call["consistency_level"] == "Strong" for call in milvus.searches)
    assert all(call["partition_names"] == ["bd_20260926"] for call in milvus.searches)


def test_orphan_vector_and_search_outage_are_not_complete_empty():
    milvus = FakeMilvus()
    milvus.search_rows = [hit(RID1, 0, 1)]
    adapter = store(milvus, FakeES([]))
    orphan = adapter.search(request(), [1.0, 0.0, 0.0, 0.0])
    assert orphan.status == "unavailable"
    assert orphan.error_code == "ORPHAN_VECTOR"
    milvus.fail_search = True
    failed = adapter.search(request(), [1.0, 0.0, 0.0, 0.0])
    assert failed.status == "unavailable"
    assert failed.error_code == "MILVUS_SEARCH_FAILED"


def test_actual_pymilvus_search_hit_uses_vector_id_key():
    milvus = FakeMilvus()
    actual_hit = hit(RID1, 0, 1)
    actual_hit["vector_id"] = actual_hit.pop("id")
    milvus.search_rows = [actual_hit]
    result = store(milvus, FakeES([doc(RID1, "i1", 1)])).search(
        request(), [1.0, 0.0, 0.0, 0.0])
    assert result.status == "complete"
    assert [candidate.record_id for candidate in result.candidates] == [RID1]


@pytest.mark.parametrize("expiry", ["not-a-date", "2026-09-27T00:00:00"])
def test_invalid_authority_expiry_fails_closed_before_vector_write(expiry):
    milvus = FakeMilvus()
    authority = {**doc(RID1, "i1", 1), "expires_at": expiry}
    with pytest.raises(VectorWriteUnknown):
        store(milvus, FakeES([authority])).upsert(row())
    assert milvus.upserts == []
