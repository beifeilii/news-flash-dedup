# -*- coding: utf-8 -*-
"""E 批 H-02(b) 红测：store 层写入前沿证据化（coverage_complete 证明求值）。

设计稿=log\\temp\\e-batch-design.md §一。裁定锚=log\\G门呈裁包.md ①(b) 定裁。
红能力：现役 vector_store.py:325 coverage_complete 恒 False——
本文件 G2-G12 全部红（API 未落地），G1 前后均绿（复用纪律守卫）。
"""
from __future__ import annotations

import hashlib
import json

import pytest

from news_flash_dedup.recall.models import RecallRequest
from news_flash_dedup.recall.prepare import advance_prepared_frontier
from news_flash_dedup.recall.vector_space import EmbeddingSpace
from news_flash_dedup.recall.vector_store import MilvusVectorStore

SPACE = EmbeddingSpace("mock_embedding", "fixture_v1", "codepoint_v1", "none",
                       "COSINE", 4, "plain_v1", "plain_v1",
                       "tokens512_overlap64_v1", "p09_v1")
RID_Q = "f" * 64
RID_HIT = "c" * 64


# ---------- 双倍体（签名对拍 vector_store.py:237-246 milvus.search 实参形、
# :122-132 _es_source es.get 实参形亲读钉） ----------

class _FakeMilvus:
    """search 面双倍体：恒一命中（len<80 → :288 单页 break）。"""

    def __init__(self, hit_entity):
        self._entity = hit_entity

    def has_partition(self, collection_name, partition_name):
        return True

    def search(self, collection_name, data, filter, limit, output_fields,
               partition_names, anns_field, search_params, consistency_level,
               **kw):
        assert consistency_level == "Strong"
        return [[{"id": self._entity["vector_id"], "distance": 0.99,
                  "entity": self._entity}]]


class _FakeES:
    """权威回查双倍体（es.get 形）：按 record_id 供 found/_source。"""

    def __init__(self, docs):
        self._docs = docs

    def get(self, index, id, realtime=True):
        doc = self._docs.get(id)
        if doc is None:
            return {"_index": index, "_id": id, "found": False}
        return {"_index": index, "_id": id, "found": True, "_source": doc}


def _hit_entity(**over):
    entity = {
        "vector_id": f"{RID_HIT}_0", "record_id": RID_HIT,
        "scope_id": "default", "business_date": "2026-09-28",
        "arrival_seq": 1, "embedding_space_id": SPACE.space_id,
        "chunk_id": 0, "embedding": [1.0, 0.0, 0.0, 0.0],
    }
    entity.update(over)
    return entity


def _authority_doc(**over):
    doc = {
        "record_id": RID_HIT, "item_id": "item-hit", "scope_id": "default",
        "business_date": "2026-09-28", "arrival_seq": 1,
        # 实现窗补正（设计稿夹具转写漏键，ew-impl-notes ②偏差①在案）：
        # 现役 vector_store.py 候选对拍要求权威 _source 携 embedding_space_id
        # （缺失→ORPHAN_VECTOR），G3 锚"证明齐备∧无孤儿∧complete→True"
        # 依赖权威文档良构——补本键使夹具贴合自身锚文，断言零改动。
        "embedding_space_id": SPACE.space_id,
        "text": "权威正文", "expires_at": "2099-01-01T00:00:00Z",
    }
    doc.update(over)
    return doc


def _request(**over):
    kw = dict(scope_id="default", business_date="2026-09-28",
              record_id=RID_Q, item_id="item-q", arrival_seq=2,
              text="查询正文", visible_seq=1, prepared_seq=1,
              embedding_space_id=SPACE.space_id)
    kw.update(over)
    return RecallRequest(**kw)


def _store(milvus, es, **kw):
    # 集合名闸（vector_store.py:104-107）：replay 形态且 endswith(space_id)。
    return MilvusVectorStore(milvus, es,
                             f"news_dedup_replay_ab12cd_{SPACE.space_id}",
                             "p01-batch-x1-", SPACE, **kw)


def _proof(frontier=1, holes=0, space_id=SPACE.space_id):
    """设计签名：VectorCoverageProof（H2-3 新件）。"""
    from news_flash_dedup.recall.vector_store import VectorCoverageProof
    return VectorCoverageProof(
        scope_id="default", business_date="2026-09-28", space_id=space_id,
        vector_frontier=frontier, hole_count=holes,
        schema_version="vector-prepared-v1")


class _StaticProvider:
    def __init__(self, proof):
        self._proof = proof

    def vector_frontier(self, scope_id, business_date, space_id):
        return self._proof


def _search(store):
    return store.search(_request(), [1.0, 0.0, 0.0, 0.0])


# ---------- G1 前后均绿守卫：前沿纯函数复用纪律（W-R4 语义钉） ----------

def test_g1_frontier_function_reuse_discipline_unchanged():
    """advance_prepared_frontier 零改动复用（H2-4）：孔洞不越过/凭证明跳过/
    单调不回退——09 §3.2 L175+§3.4 L193 语义钉（修复前后均绿）。"""
    assert advance_prepared_frontier(0, [1, 2, 4]) == 2          # 缺 3 不越过
    assert advance_prepared_frontier(0, [1, 2, 4], foreign={3}) == 4
    assert advance_prepared_frontier(5, [1, 2]) == 5              # 不回退
    assert advance_prepared_frontier(0, [2]) == 0                 # 首号即洞
    with pytest.raises(ValueError):
        advance_prepared_frontier(-1, [1])


# ---------- G2 指纹纯函数（H2-3） ----------

def test_g2_row_fingerprint_deterministic_and_content_sensitive():
    from news_flash_dedup.recall.vector_store import (
        prepare_vector_row, vector_row_fingerprint)
    row = prepare_vector_row(RID_HIT, "default", "2026-09-28", 1, SPACE, 0,
                             [1.0, 0.0, 0.0, 0.0])
    fp1 = vector_row_fingerprint(row)
    fp2 = vector_row_fingerprint(dict(reversed(list(row.items()))))
    assert fp1 == fp2 and fp1.startswith("sha256:")
    row2 = dict(row, arrival_seq=2)
    assert vector_row_fingerprint(row2) != fp1                   # 内容敏感
    canon = json.dumps(row, sort_keys=True, separators=(",", ":"),
                       ensure_ascii=False).encode("utf-8")
    assert fp1 == "sha256:" + hashlib.sha256(canon).hexdigest()  # 规范形钉


# ---------- G3-G9 search 证明求值（H2-1/H2-2） ----------

def test_g3_coverage_true_with_full_proof():
    """三层证明齐备（前沿≥floor∧孔洞=0∧空间符∧无孤儿∧complete）→ True。"""
    store = _store(_FakeMilvus(_hit_entity()), _FakeES({RID_HIT: _authority_doc()}),
                   frontier_provider=_StaticProvider(_proof(frontier=1)))
    result = _search(store)
    assert result.status == "complete"
    assert result.coverage_complete is True


def test_g4_coverage_false_without_provider_green_guard():
    """绿守卫：default 构造（无 provider）=现役逐字节（恒 False）。"""
    store = _store(_FakeMilvus(_hit_entity()), _FakeES({RID_HIT: _authority_doc()}))
    assert _search(store).coverage_complete is False


def test_g5_coverage_false_when_frontier_short():
    store = _store(_FakeMilvus(_hit_entity()), _FakeES({RID_HIT: _authority_doc()}),
                   frontier_provider=_StaticProvider(_proof(frontier=0)))
    assert _search(store).coverage_complete is False             # floor=1


def test_g6_coverage_false_when_holes_open():
    store = _store(_FakeMilvus(_hit_entity()), _FakeES({RID_HIT: _authority_doc()}),
                   frontier_provider=_StaticProvider(_proof(frontier=5, holes=1)))
    assert _search(store).coverage_complete is False


def test_g7_coverage_false_when_space_mismatch():
    store = _store(_FakeMilvus(_hit_entity()), _FakeES({RID_HIT: _authority_doc()}),
                   frontier_provider=_StaticProvider(
                       _proof(frontier=5, space_id="s_" + "0" * 40)))
    assert _search(store).coverage_complete is False


def test_g8_coverage_false_when_orphan():
    """孤儿命中：现役 ORPHAN_VECTOR 降级不吞（:318-321）+ coverage False。"""
    store = _store(_FakeMilvus(_hit_entity()), _FakeES({}),      # 权威缺席
                   frontier_provider=_StaticProvider(_proof(frontier=5)))
    result = _search(store)
    assert result.status == "unavailable"
    assert result.error_code == "ORPHAN_VECTOR"
    assert result.coverage_complete is False


def test_g9_coverage_false_when_provider_none_proof():
    """provider 在场但证明缺席（F1/F2/F3 族）→ False（fail-closed 不猜）。"""
    store = _store(_FakeMilvus(_hit_entity()), _FakeES({RID_HIT: _authority_doc()}),
                   frontier_provider=_StaticProvider(None))
    assert _search(store).coverage_complete is False


# ---------- G10-G12 VectorFrontierProvider（H2-3） ----------

class _FakeStoreDoc:
    """ElasticsearchBatchStore 最小鸭式（get only）。"""

    def __init__(self, doc):
        self._doc = doc

    def get(self, index, doc_id):
        return self._doc


def _day_doc(snapshot):
    return {"_source": {"kind": "day", "scope_id": "default",
                        "business_date": "2026-09-28",
                        "checkpoint": ({} if snapshot is None
                                       else {"vector_prepared": snapshot})}}


def _snapshot(**over):
    snap = {"schema_version": "vector-prepared-v1", "scope_id": "default",
            "business_date": "2026-09-28", "space_id": SPACE.space_id,
            "vector_frontier": 7, "max_ready_seen": 9,
            "reconciled_through": 7, "hole_count": 0,
            "foreign_proof_count": 0, "generation": 3,
            "advanced_at": "2026-10-07T01:30:00Z"}
    snap.update(over)
    return snap


def test_g10_provider_reads_snapshot_and_typed_proof():
    from news_flash_dedup.recall.vector_store import VectorFrontierProvider
    provider = VectorFrontierProvider(_FakeStoreDoc(_day_doc(_snapshot())),
                                      "p01-batch-x1-")
    proof = provider.vector_frontier("default", "2026-09-28", SPACE.space_id)
    assert proof is not None
    assert proof.vector_frontier == 7 and proof.hole_count == 0
    assert proof.space_id == SPACE.space_id


def test_g11_provider_fail_closed_on_absent_or_malformed():
    from news_flash_dedup.recall.vector_store import VectorFrontierProvider
    p_absent = VectorFrontierProvider(_FakeStoreDoc(None), "p01-batch-x1-")
    assert p_absent.vector_frontier("default", "2026-09-28",
                                    SPACE.space_id) is None
    p_no_snap = VectorFrontierProvider(_FakeStoreDoc(_day_doc(None)),
                                       "p01-batch-x1-")
    assert p_no_snap.vector_frontier("default", "2026-09-28",
                                     SPACE.space_id) is None
    bad = _snapshot(vector_frontier="7")                          # 型错
    p_bad = VectorFrontierProvider(_FakeStoreDoc(_day_doc(bad)),
                                   "p01-batch-x1-")
    assert p_bad.vector_frontier("default", "2026-09-28",
                                 SPACE.space_id) is None
    wrong_space = _snapshot(space_id="s_" + "0" * 40)
    p_ws = VectorFrontierProvider(_FakeStoreDoc(_day_doc(wrong_space)),
                                  "p01-batch-x1-")
    assert p_ws.vector_frontier("default", "2026-09-28",
                                SPACE.space_id) is None          # F3 混空间


def test_g12_provider_realtime_get_no_cache():
    """证明读侧实时 GET（不缓存——水位语义同 prepare.py:129-137）。"""
    from news_flash_dedup.recall.vector_store import VectorFrontierProvider
    backing = _FakeStoreDoc(_day_doc(_snapshot(vector_frontier=7)))
    provider = VectorFrontierProvider(backing, "p01-batch-x1-")
    assert provider.vector_frontier("default", "2026-09-28",
                                    SPACE.space_id).vector_frontier == 7
    backing._doc = _day_doc(_snapshot(vector_frontier=8))        # 外部推进
    assert provider.vector_frontier("default", "2026-09-28",
                                    SPACE.space_id).vector_frontier == 8
