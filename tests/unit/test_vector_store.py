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


# ---------- P2 确定性破序（2026-10-09 主窗口派活）----------

def _rid(i):
    """定宽 64hex record_id：字典序==数值序（i≥1，十六进制定宽）。"""
    return f"{i:064x}"


def scored_hit(record_id, chunk_id, seq, score):
    return {**hit(record_id, chunk_id, seq), "distance": score}


def tied_layout(n, chunks_per, *, rotate=0):
    """n 件记录各 chunks_per 块、全部同分 0.9（hit 夹具硬编 0.9）；
    rotate 模拟段物理布局差异（chunk 行序旋转）。"""
    ids = [_rid(i) for i in range(1, n + 1)]
    rows = [hit(rid, c, i + 1)
            for i, rid in enumerate(ids) for c in range(chunks_per)]
    if rotate:
        rows = rows[rotate:] + rows[:rotate]
    es = FakeES([doc(rid, f"item-{i}", i + 1) for i, rid in enumerate(ids)])
    return rows, es, ids


def test_tie_overfetch_deterministic_across_layouts_and_calls():
    """P2-① 同分 45 件（90 块，页界并列）跨布局/跨调用逐字节一致：
    首页 80 块仅覆 40 件（第 K 名分==页界分）→ 过取扩页 → 45 全入池 →
    (分数 DESC, record_id ASC) 整排截断 40——与段物理布局无关。"""
    for rotate in (0, 37):
        rows, es, ids = tied_layout(45, 2, rotate=rotate)
        milvus = FakeMilvus()
        milvus.search_rows = rows
        adapter = store(milvus, es)
        first = adapter.search(request(), [1.0, 0.0, 0.0, 0.0])
        second = adapter.search(request(), [1.0, 0.0, 0.0, 0.0])
        assert first.status == "complete"
        # 过取实证：每次调用首页 80 块不满 45 件且页界并列 → 扩 160 穷尽
        # （searches 跨两次调用累计=[80,160]×2）
        assert [call["limit"] for call in milvus.searches] == [80, 160, 80, 160]
        got = [c.record_id for c in first.candidates]
        assert got == [c.record_id for c in second.candidates]  # 跨调用
        assert got == ids[:40]              # record_id ASC 前 40（全序确定）
        assert first.topk_excluded == 5     # 边距内并列 5 件入池后定序落选
        if rotate == 0:
            baseline = got
        else:
            assert got == baseline          # 跨布局逐字节


def test_tie_within_margin_all_retained_displaces_by_record_id():
    """P2-② 边距内并列全保留不丢：首页 80 块只装 record_id 较大的
    r6..r45（40 件），r1..r5（字典序最小）未取——过取后 r1..r5 入池
    并凭 record_id ASC 挤入 top-40，落选者=r41..r45 而非布局牺牲品。"""
    ids = [_rid(i) for i in range(1, 46)]
    rows = ([hit(rid, c, i + 1) for i, rid in enumerate(ids) if i >= 5
             for c in range(2)]                       # r6..r45 共 80 块先回
            + [hit(rid, c, i + 1) for i, rid in enumerate(ids) if i < 5
               for c in range(2)])                    # r1..r5 共 10 块殿后
    milvus = FakeMilvus()
    milvus.search_rows = rows
    es = FakeES([doc(rid, f"item-{i}", i + 1) for i, rid in enumerate(ids)])
    result = store(milvus, es).search(request(), [1.0, 0.0, 0.0, 0.0])
    assert [call["limit"] for call in milvus.searches] == [80, 160]
    got = [c.record_id for c in result.candidates]
    assert got[:5] == ids[:5]               # r1..r5 保留且居前
    assert got == ids[:40]                  # 落选=r41..r45（record_id 定序）
    assert result.topk_excluded == 5


def test_non_tie_path_byte_identical_single_page():
    """P2-③ 非同分路径逐字节不动：45 件各异分，第 K 名严格高于页界分
    → 首页即停（pages=1/单次检索/集合与序与施工前一致）。"""
    ids = [_rid(i) for i in range(1, 46)]
    rows = []
    for i, rid in enumerate(ids):
        base = 1.0 - (i + 1) * 0.001        # 记录 i 最高分（各异、递减）
        rows.append(scored_hit(rid, 0, i + 1, base))
        rows.append(scored_hit(rid, 1, i + 1, base - 0.00001))
    milvus = FakeMilvus()
    milvus.search_rows = rows               # 首页 80 块=r1..r40 两两块
    es = FakeES([doc(rid, f"item-{i}", i + 1) for i, rid in enumerate(ids)])
    result = store(milvus, es).search(request(), [1.0, 0.0, 0.0, 0.0])
    assert result.status == "complete"
    assert [call["limit"] for call in milvus.searches] == [80]   # 单页即停
    assert result.pages == 1
    got = [c.record_id for c in result.candidates]
    assert got == ids[:40]                  # 分降序=id 序（分各异）
    scores = [c.score for c in result.candidates]
    assert all(a > b for a, b in zip(scores, scores[1:]))   # 严格降序
    assert result.topk_excluded == 0        # 池恰 40，与施工前同形


def test_tie_storm_beyond_margin_idempotent_documented():
    """P2-④ 边距尽钉（登记风险边界文档化）：同分组 100 件>边距 10——
    首页 80 块池即≥K+边距 → 停；给定布局两次调用逐字节一致（幂等），
    top-40=已取 80 件中 record_id ASC 前 40。跨布局发散=同分组>边距
    的登记接受风险（组更大需 bump 边距版本），此钉防静默漂移。"""
    rows, es, ids = tied_layout(100, 1)
    milvus = FakeMilvus()
    milvus.search_rows = rows
    adapter = store(milvus, es)
    first = adapter.search(request(), [1.0, 0.0, 0.0, 0.0])
    second = adapter.search(request(), [1.0, 0.0, 0.0, 0.0])
    assert [call["limit"] for call in milvus.searches] == [80, 80]  # 边距停
    got = [c.record_id for c in first.candidates]
    assert got == [c.record_id for c in second.candidates]
    assert got == ids[:40]                  # 已取 80 件（r1..r80）中前 40
    assert first.topk_excluded == 40        # 80-40（池截于边距，非穷尽）
