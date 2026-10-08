# -*- coding: utf-8 -*-
"""修复波 2 红测·F-4 H-02 读侧 fail-closed 包络孔洞（EWΔ 复审 F-4）。

红态锚：负 int 快照 → VectorCoverageProof.__post_init__（vector_store.py:82-85）
ValueError 逃逸（provider 型闸 :135 不闸负值；search() provider 调用在 try
之外且 except :437 不含 ValueError——双层孔洞）。
设计真源=log\\temp\\wave2-design.md §2.3（代码块照稿施工）。
"""
from __future__ import annotations

import pytest

from news_flash_dedup.recall.models import RecallRequest
from news_flash_dedup.recall.vector_space import EmbeddingSpace
from news_flash_dedup.recall.vector_store import (
    MilvusVectorStore, VectorFrontierProvider)

SPACE = EmbeddingSpace("mock_embedding", "fixture_v1", "codepoint_v1", "none",
                       "COSINE", 4, "plain_v1", "plain_v1",
                       "tokens512_overlap64_v1", "p09_v1")
DAY = "2026-09-28"


def _snapshot_doc(frontier, holes):
    return {"_source": {"checkpoint": {"vector_prepared": {
        "schema_version": "vector-prepared-v1", "space_id": SPACE.space_id,
        "vector_frontier": frontier, "hole_count": holes}}}}


class _DayStore:
    """日控制文档双倍体（provider :115-116 get 形）。"""
    def __init__(self, doc):
        self._doc = doc
    def get(self, index, doc_id):
        return self._doc


def _provider(doc):
    return VectorFrontierProvider(_DayStore(doc), "p01-batch-x1-")


def test_w2_f4_1_negative_frontier_returns_none_not_raise():
    """负 frontier 快照→None（F12 不传播异常）；红态=ValueError 逃逸。"""
    provider = _provider(_snapshot_doc(-1, 0))
    assert provider.vector_frontier("default", DAY, SPACE.space_id) is None


def test_w2_f4_2_negative_hole_count_returns_none_not_raise():
    """负 hole_count 快照→None；红态=ValueError 逃逸。"""
    provider = _provider(_snapshot_doc(5, -2))
    assert provider.vector_frontier("default", DAY, SPACE.space_id) is None


def test_w2_f4_3_legit_snapshot_still_proves():
    """绿守卫：良构快照→证明正常构造（A 修不误拦）。"""
    provider = _provider(_snapshot_doc(5, 0))
    proof = provider.vector_frontier("default", DAY, SPACE.space_id)
    assert proof is not None and proof.vector_frontier == 5 and proof.hole_count == 0


# ---- search() 包络级（B 修）----

class _FakeMilvus:
    def has_partition(self, collection_name, partition_name):
        return True
    def search(self, collection_name, data, filter, limit, output_fields,
               partition_names, anns_field, search_params, consistency_level,
               **kw):
        return [[{"id": "c" * 64 + "_0", "distance": 0.99, "entity": {
            "vector_id": "c" * 64 + "_0", "record_id": "c" * 64,
            "scope_id": "default", "business_date": DAY, "arrival_seq": 1,
            "embedding_space_id": SPACE.space_id, "chunk_id": 0,
            "embedding": [1.0, 0.0, 0.0, 0.0]}}]]


class _FakeES:
    def get(self, index, id, realtime=True):
        return {"_index": index, "_id": id, "found": True, "_source": {
            "record_id": "c" * 64, "item_id": "item-hit", "scope_id": "default",
            "business_date": DAY, "arrival_seq": 1,
            "embedding_space_id": SPACE.space_id,
            "text": "权威正文", "expires_at": "2099-01-01T00:00:00Z"}}


class _ExplodingProvider:
    """构造期即抛 ValueError 的 provider（负值快照逃逸形态模拟）。"""
    def vector_frontier(self, scope_id, business_date, space_id):
        raise ValueError("vector_frontier must be a nonnegative int")


def test_w2_f4_4_search_envelope_valueerror_degrades_not_propagates():
    """B 修：provider ValueError→coverage_complete=False 降级，不穿透 search()。"""
    store = MilvusVectorStore(_FakeMilvus(), _FakeES(),
                              f"news_dedup_replay_ab12cd_{SPACE.space_id}",
                              "p01-batch-x1-", SPACE,
                              frontier_provider=_ExplodingProvider())
    request = RecallRequest(scope_id="default", business_date=DAY,
                            record_id="f" * 64, item_id="item-q", arrival_seq=2,
                            text="查询正文", visible_seq=1, prepared_seq=1,
                            embedding_space_id=SPACE.space_id)
    result = store.search(request, [1.0, 0.0, 0.0, 0.0])   # 红态：ValueError 穿透
    assert result.coverage_complete is False
