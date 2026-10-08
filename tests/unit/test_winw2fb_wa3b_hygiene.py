# -*- coding: utf-8 -*-
"""窗口W2Fβ 条66（WA3b 低危族）卫生钉：写路径裸逃逸/TOCTOU/cleanup_plan 裸解析/
space_id 正则/near 留证/vector_space 死分支/recall vector_store 三件。

各子项均为 fail-closed 或留证方向，良构流程设绿守卫逐字节不动。
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

import pytest

from news_flash_dedup.milvus_client import collection_name_for_test
from news_flash_dedup.recall.hash_channel import prepare_recall_fields
from news_flash_dedup.recall.models import RecallRequest
from news_flash_dedup.recall.near_channel import NearChannel
from news_flash_dedup.recall.vector_space import (
    CodepointTokenizer,
    EmbeddingSpace,
    chunk_text,
)
from news_flash_dedup.recall.vector_store import (
    MilvusVectorStore,
    VectorWriteUnknown,
    prepare_vector_row,
)
from news_flash_dedup.vector.milvus_store import (
    RealMilvusP19Config,
    RealMilvusP19Store,
)


NOW = datetime(2026, 9, 26, 1, tzinfo=timezone.utc)
DAY = "2026-09-26"
PREFIX = "p01-batch-w2fb-hyg-"
RID1 = "a" * 64
RID2 = "b" * 64


def _space():
    return EmbeddingSpace("mock_embedding", "fixture_v1", "codepoint_v1", "none",
                          "COSINE", 4, "plain_v1", "plain_v1",
                          "tokens512_overlap64_v1", "p09_v1")


def _p19_config():
    return RealMilvusP19Config(run_uuid="winw2fb",
                               business_date=date(2026, 9, 26),
                               space=_space())


def _bare_store(milvus=None, es=None):
    store = RealMilvusP19Store.__new__(RealMilvusP19Store)
    store.milvus = milvus
    store.es = es
    store.config = _p19_config()
    return store


def _wellformed_description():
    # E 批 M-09 采用闸（dim/metric 必验等）落地后：良构 describe 双倍体
    # 必须携带 dim/metric 证据（缺键 fail-closed 拒——设计红测 V3c 钉）；
    # 本补全不改变各测试原有断言语义（焦点仍是 TOCTOU/分区/裸逃逸面）。
    return {"fields": [
        {"name": name, "params": {"dim": 4}} if name == "embedding"
        else {"name": name}
        for name in sorted({
            "vector_id", "record_id", "scope_id", "business_date", "arrival_seq",
            "embedding_space_id", "chunk_id", "embedding"})],
        "enable_dynamic_field": False}


# =====================================================================
# 1. 写路径裸逃逸两处 → VectorWriteUnknown（W1Fγ 同类延伸）
# =====================================================================

class _GetRaisingMilvus:
    def get(self, **kwargs):
        raise RuntimeError("boom-get")


def test_get_vector_row_wraps_milvus_get_failure():
    """红能力①：Strong get 抛错原裸逃逸 → VectorWriteUnknown。"""
    store = _bare_store(milvus=_GetRaisingMilvus())
    with pytest.raises(VectorWriteUnknown):
        store.get_vector_row("v-1")


class _UpsertRaisingMilvus:
    def get(self, *, collection_name, ids, output_fields, consistency_level):
        return []

    def upsert(self, **kwargs):
        raise RuntimeError("boom-upsert")


class _AuthorityES:
    """良构 ES 权威（四元组与写入意图一致）。"""

    def get(self, *, index, id, realtime):
        return {"_index": index, "_id": id, "found": True,
                "_source": {"record_id": id, "scope_id": "default",
                            "business_date": DAY, "arrival_seq": 1},
                "_seq_no": 1, "_primary_term": 1}


def test_upsert_chunks_wraps_milvus_upsert_failure():
    """红能力②：milvus.upsert 抛错原裸逃逸 → VectorWriteUnknown（确认未知）。"""
    store = _bare_store(milvus=_UpsertRaisingMilvus(), es=_AuthorityES())
    with pytest.raises(VectorWriteUnknown):
        store.upsert_chunks(record_id=RID1, chunks=[(0, [1.0, 0.0, 0.0, 0.0])],
                            scope_id="default", arrival_seq=1)


# =====================================================================
# 2. ensure_collection / ensure_indices TOCTOU
# =====================================================================

class _RaceCollectionMilvus:
    """create 竞态：并发者先建（create 抛错但集合随后可查）。"""

    def __init__(self, description, win_race=True):
        self._description = description
        self._win_race = win_race
        self._exists = False

    def has_collection(self, *, collection_name):
        return self._exists

    def create_collection(self, **kwargs):
        if self._win_race:
            self._exists = True
        raise RuntimeError("collection already exists")

    def describe_collection(self, *, collection_name):
        return self._description

    def list_partitions(self, *, collection_name):
        return ["bd_20260926"]

    def create_partition(self, *, collection_name, partition_name):
        raise AssertionError("partition already present")

    # E 批 M-09 采用闸配套：良构索引证据（FLAT/COSINE embedding）。
    def list_indexes(self, *, collection_name):
        return ["embedding"]

    def describe_index(self, *, collection_name, index_name):
        return {"index_type": "FLAT", "metric_type": "COSINE",
                "field_name": "embedding"}


def test_ensure_collection_race_falls_back_to_describe_adoption():
    """红能力：create 竞态败→回退 describe 核验，同构照常接管。"""
    milvus = _RaceCollectionMilvus(_wellformed_description())
    _bare_store(milvus=milvus)._ensure_collection()   # 不抛即接管成功


def test_ensure_collection_create_failure_without_collection_is_unknown():
    """同钉：create 抛错且集合仍不存在 → VectorWriteUnknown（fail-closed）。"""
    milvus = _RaceCollectionMilvus(_wellformed_description(), win_race=False)
    with pytest.raises(VectorWriteUnknown):
        _bare_store(milvus=milvus)._ensure_collection()


class _RaceIndices:
    def __init__(self, win_race=True):
        self._win_race = win_race
        self._existing = set()

    def exists(self, *, index):
        return index in self._existing

    def create(self, *, index):
        if self._win_race:
            self._existing.add(index)
        raise RuntimeError("resource_already_exists_exception")


class _RaceES:
    def __init__(self, win_race=True):
        self.indices = _RaceIndices(win_race)


def test_ensure_indices_race_tolerates_concurrent_creation():
    """红能力：索引 create 竞态败→复检存在即视为已建，不抛。"""
    _bare_store(es=_RaceES())._ensure_indices()


def test_ensure_indices_create_failure_without_index_is_unknown():
    """同钉：create 抛错且索引仍不存在 → VectorWriteUnknown。"""
    with pytest.raises(VectorWriteUnknown):
        _bare_store(es=_RaceES(win_race=False))._ensure_indices()


# =====================================================================
# 3. cleanup_plan L497 裸解析 → fail-closed
# =====================================================================

class _Cat:
    def __init__(self, listing):
        self._listing = listing

    def indices(self, *, format, h):
        return self._listing


class _CleanupES:
    def __init__(self, listing):
        self.cat = _Cat(listing)


class _CleanupMilvus:
    def list_collections(self):
        return ["p19_winw2fb_other", "unrelated"]


@pytest.mark.parametrize("listing", [
    None,                            # 非 list → 旧裸 TypeError
    ["not-a-mapping"],               # 条目非 Mapping → 旧裸 AttributeError
    [{"index": 7}],                  # index 非 str → 旧裸 AttributeError
])
def test_cleanup_plan_malformed_listing_is_unknown(listing):
    """红能力：cat.indices 回包畸形 → VectorWriteUnknown（不再裸解析）。"""
    store = _bare_store(milvus=_CleanupMilvus(), es=_CleanupES(listing))
    with pytest.raises(VectorWriteUnknown):
        store.cleanup_plan()


def test_cleanup_plan_wellformed_listing_unchanged():
    """绿守卫：良构回包仅列本 run 前缀；缺 index 键条目按 "" 容忍（旧兼容）。"""
    run_index = "p19-batch-winw2fb-news-dedup-items-v1-2026.09.26"
    listing = [{"index": "other-idx"}, {"index": run_index}, {}]
    plan = _bare_store(milvus=_CleanupMilvus(), es=_CleanupES(listing)).cleanup_plan()
    assert plan == {"collections": ["p19_winw2fb_other"], "indices": [run_index]}


# =====================================================================
# 4. milvus_client L64 space_id 正则收紧（字符集 → hex）
# =====================================================================

def test_collection_name_rejects_non_hex_space_id():
    """红能力：非 hex 字符 space_id → 拒（旧 [a-z0-9] 放行）。"""
    with pytest.raises(ValueError):
        collection_name_for_test("abc123", "s_xyzq")


def test_collection_name_hex_window_still_accepted():
    """绿守卫：hex 字符空间标识照常（既有钉 s_deadbeef 短形在案）。"""
    assert collection_name_for_test("abc123", "s_deadbeef") == \
        "news_dedup_replay_abc123_s_deadbeef"


# =====================================================================
# 5. near_channel 双 except 补留证（日志）
# =====================================================================

def _near_item(record_id, item_id, seq, text, **overrides):
    source = {"scope_id": "default", "business_date": DAY,
              "record_id": record_id, "item_id": item_id, "arrival_seq": seq,
              "text": text, "expires_at": (NOW + timedelta(days=1)).isoformat(),
              "preparation_state": "ready"}
    source.update(prepare_recall_fields(text, "default", DAY) or {})
    source.update(overrides)
    return source


class _NearES:
    def __init__(self, docs, fail_at=None):
        self.docs = {doc["record_id"]: doc for doc in docs}
        self.fail_at = fail_at
        self.queries = 0

    def search(self, *, index, body):
        self.queries += 1
        if self.fail_at == self.queries:
            raise RuntimeError("injected outage")
        clause = body["query"]["bool"]
        band = next(part["term"] for part in clause["filter"]
                    if "term" in part and
                    ("simhash_bands" in part["term"] or "minhash_bands" in part["term"]))
        band_field, band_value = next(iter(band.items()))
        found = [doc for doc in self.docs.values()
                 if band_value in doc.get(band_field, [])]
        found.sort(key=lambda doc: (doc["arrival_seq"], doc["record_id"]))
        return {"timed_out": False, "_shards": {"failed": 0}, "hits": {"hits": [
            {"_index": index, "_id": doc["record_id"],
             "_source": {"record_id": doc["record_id"]}}
            for doc in found[:body["size"]]
        ]}}

    def get(self, *, index, id, realtime):
        return {"_index": index, "_id": id, "found": True, "_source": self.docs[id]}


def _near_request():
    return RecallRequest("default", DAY, "current", "current-item", 100,
                         "甲公司完成3亿元回购")


def test_near_bucket_failure_leaves_log_evidence(caplog):
    """红能力：桶查询失败吞异常前留 warning 日志（error_code 面不变）。"""
    es = _NearES([_near_item("prior", "p", 1, "甲公司完成3亿元回购")], fail_at=1)
    with caplog.at_level(logging.WARNING,
                         logger="news_flash_dedup.recall.near_channel"):
        result = NearChannel(es, PREFIX, clock=lambda: NOW).search(_near_request())
    assert result.status == "unavailable"
    assert result.error_code == "ES_SEARCH_FAILED"
    assert any(record.levelno == logging.WARNING
               for record in caplog.records), "no evidence log left"


def test_near_candidate_mismatch_leaves_log_evidence(caplog):
    """红能力：候选指纹对拍失败吞异常前留 warning 日志（error_code 面不变）。"""
    doc = _near_item("bad", "b", 1, "甲公司完成3亿元回购",
                     fingerprint_payload={"minhash_signature": [0] * 128,
                                          "simhash_version": "simhash_v1",
                                          "minhash_version": "minhash_v1"})
    with caplog.at_level(logging.WARNING,
                         logger="news_flash_dedup.recall.near_channel"):
        result = NearChannel(_NearES([doc]), PREFIX,
                             clock=lambda: NOW).search(_near_request())
    assert result.status == "unavailable"
    assert result.error_code == "FINGERPRINT_INDEX_MISMATCH"
    assert any(record.levelno == logging.WARNING
               for record in caplog.records), "no evidence log left"


# =====================================================================
# 6. vector_space L135-136 死分支删除（行为零变化守卫）
# =====================================================================

def test_chunking_behavior_unchanged_after_dead_branch_removal():
    """绿守卫：多块分块全覆盖/句界优先/重叠语义不变。"""
    text = "甲" * 1097 + "最后一段"
    chunks = chunk_text(text, CodepointTokenizer())
    assert len(chunks) >= 3
    assert chunks[0].start == 0
    assert chunks[-1].end == len(text)
    assert all(any(chunk.start <= point < chunk.end for chunk in chunks)
               for point in range(len(text)))
    bounded = chunk_text("甲" * 490 + "。" + "乙" * 100, CodepointTokenizer())
    assert bounded[0].end == 491
    assert bounded[1].start < bounded[0].end


# =====================================================================
# 7. recall/vector_store：truncated 孤儿补降级 + L184 错误面 + Z 定点化
# =====================================================================

class _SearchMilvus:
    def __init__(self, search_rows):
        self.rows = {}
        self.search_rows = search_rows

    def get(self, *, collection_name, ids, output_fields, consistency_level):
        return [self.rows[key] for key in ids if key in self.rows]

    def upsert(self, *, collection_name, data, partition_name):
        self.rows[data["vector_id"]] = data
        return {"upsert_count": 1}

    def search(self, **kwargs):
        return [self.search_rows[:kwargs["limit"]]]


class _VectorES:
    def __init__(self, docs):
        self.docs = {doc["record_id"]: doc for doc in docs}

    def get(self, *, index, id, realtime):
        if id not in self.docs:
            return {"_index": index, "_id": id, "found": False}
        return {"_index": index, "_id": id, "found": True,
                "_source": self.docs[id]}


def _vector_doc(record_id, item_id, seq, **overrides):
    source = {"scope_id": "default", "business_date": DAY,
              "record_id": record_id, "item_id": item_id, "arrival_seq": seq,
              "text": "甲公司完成回购", "embedding_space_id": _space().space_id,
              "expires_at": (NOW + timedelta(days=1)).isoformat()}
    source.update(overrides)
    return source


def _hit(record_id, chunk_id, seq):
    return {"id": f"{record_id}_{chunk_id}", "distance": 0.9,
            "entity": {"record_id": record_id, "scope_id": "default",
                       "business_date": DAY, "arrival_seq": seq,
                       "embedding_space_id": _space().space_id,
                       "chunk_id": chunk_id}}


def _vector_store(milvus, es, *, max_search_limit=640):
    return MilvusVectorStore(
        milvus, es, "news_dedup_replay_abc123_" + _space().space_id,
        PREFIX, _space(), clock=lambda: NOW, max_search_limit=max_search_limit)


def _vector_request():
    return RecallRequest("default", DAY, "c" * 64, "current-item", 100,
                         "甲公司完成回购", embedding_space_id=_space().space_id)


def test_truncated_orphan_degrades_to_unavailable():
    """红能力：截断态遇孤儿向量同样降级 unavailable/ORPHAN_VECTOR（信号不丢）。"""
    rows = ([_hit(RID1, chunk, 1) for chunk in range(40)] +
            [_hit(RID2, chunk, 2) for chunk in range(40)])
    store = _vector_store(_SearchMilvus(rows), _VectorES([]), max_search_limit=80)
    result = store.search(_vector_request(), [1.0, 0.0, 0.0, 0.0])
    assert result.status == "unavailable"
    assert result.error_code == "ORPHAN_VECTOR"


def test_truncated_without_orphan_stays_truncated():
    """绿守卫：截断无孤儿 → truncated/VECTOR_CHUNK_LIMIT 形态不变。"""
    rows = ([_hit(RID1, chunk, 1) for chunk in range(40)] +
            [_hit(RID2, chunk, 2) for chunk in range(40)])
    docs = [_vector_doc(RID1, "i1", 1), _vector_doc(RID2, "i2", 2)]
    store = _vector_store(_SearchMilvus(rows), _VectorES(docs),
                          max_search_limit=80)
    result = store.search(_vector_request(), [1.0, 0.0, 0.0, 0.0])
    assert result.status == "truncated"
    assert result.error_code == "VECTOR_CHUNK_LIMIT"


def test_invalid_query_vector_is_unavailable_not_raw_raise():
    """红能力（L184 错误面对齐）：非法查询向量 → unavailable/EMBEDDING_INVALID。"""
    store = _vector_store(_SearchMilvus([]), _VectorES([]))
    result = store.search(_vector_request(), [1.0, 0.0])
    assert result.status == "unavailable"
    assert result.error_code == "EMBEDDING_INVALID"


def test_expiry_trailing_z_accepted_and_garbage_rejected():
    """绿守卫（L144 Z 定点化）：尾 Z 照常解析；畸形/naive 仍 fail-closed。"""
    milvus = _SearchMilvus([])
    authority = _vector_doc(RID1, "i1", 1,
                            expires_at="2026-09-27T00:00:00Z")
    store = _vector_store(milvus, _VectorES([authority]))
    row = prepare_vector_row(RID1, "default", DAY, 1, _space(), 0,
                             [1.0, 0.0, 0.0, 0.0])
    assert store.upsert(row) == "created"
    for bad in ("2026-09-27T00:00:00", "not-a-date", "2026-09-27T00:00:00ZZ"):
        bad_doc = _vector_doc(RID2, "i2", 2, expires_at=bad)
        with pytest.raises(VectorWriteUnknown):
            _vector_store(_SearchMilvus([]), _VectorES([bad_doc])).upsert(
                prepare_vector_row(RID2, "default", DAY, 2, _space(), 0,
                                   [1.0, 0.0, 0.0, 0.0]))
