# -*- coding: utf-8 -*-
"""E 批 M-09/F1 红测：milvus 租约代次闸+dim/metric 核验闸（校验不过=拒读拒写）。

设计稿=log\\temp\\e-batch-design.md §二。裁定锚=log\\G门呈裁包.md ③定裁。
双倍体先例=tests\\unit\\test_winw1fc_milvus_unknowns.py:55-61 _bare_store。
"""
from __future__ import annotations

from datetime import date

import pytest

from news_flash_dedup.recall.vector_space import EmbeddingSpace
from news_flash_dedup.recall.vector_store import (
    VectorWriteUnknown, prepare_vector_row)
from news_flash_dedup.vector import milvus_store as m


def _space():
    return EmbeddingSpace("mock_embedding", "fixture_v1", "codepoint_v1", "none",
                          "COSINE", 4, "plain_v1", "plain_v1",
                          "tokens512_overlap64_v1", "p09_v1")


def _config():
    return m.RealMilvusP19Config(run_uuid="wine09a",
                                 business_date=date(2026, 9, 28),
                                 space=_space())


def _bare_store(milvus=None, es=None):
    store = m.RealMilvusP19Store.__new__(m.RealMilvusP19Store)
    store.milvus = milvus
    store.es = es
    store.config = _config()
    return store


RID = "a" * 64


def _row(**over):
    row = prepare_vector_row(RID, "default", "2026-09-28", 1, _space(), 0,
                             [1.0, 0.0, 0.0, 0.0])
    row.update(over)
    return row


# ---------- G0 绿守卫：名形闸与既有错误族零变更 ----------

def test_g0_name_shape_gates_unchanged():
    m.validate_p19_collection_name("p19_wine09a_s_" + "a" * 40)
    with pytest.raises(m.P19CollectionNameInvalid):
        m.validate_p19_collection_name("news_dedup_replay_ab12cd_s_" + "a" * 40)
    with pytest.raises(m.P19IndexNameInvalid):
        m.validate_p19_index_name("p18-batch-x-news-dedup-items-v1-2026.09.28")


# ---------- V1-V3 dim/metric 核验闸 ----------

class _DescribeMilvus:
    """采用面双倍体（签名对拍 _ensure_collection :152-216 亲读钉）。"""

    def __init__(self, fields, index_desc):
        self._fields = fields
        self._index = index_desc
        self.created = False

    def has_collection(self, collection_name):
        return True

    def describe_collection(self, collection_name):
        return {"collection_name": collection_name,
                "enable_dynamic_field": False, "fields": self._fields}

    def list_partitions(self, collection_name):
        return ["bd_20260928"]                    # config.business_date 分区在场

    def list_indexes(self, collection_name):
        return ["embedding"]

    def describe_index(self, collection_name, index_name):
        return self._index

    def create_collection(self, **kw):
        self.created = True


def _fields_with_dim(dim):
    names = ["vector_id", "record_id", "scope_id", "business_date",
             "arrival_seq", "embedding_space_id", "chunk_id", "embedding"]
    fields = []
    for name in names:
        field = {"name": name, "type": "FLOAT_VECTOR" if name == "embedding"
                 else "VARCHAR", "params": {}}
        if name == "embedding":
            field["params"] = {"dim": dim}
        fields.append(field)
    return fields


def test_v1_adopt_matching_dim_metric_accepted():
    milvus = _DescribeMilvus(_fields_with_dim(4),
                             {"index_type": "FLAT", "metric_type": "COSINE",
                              "field_name": "embedding"})
    store = _bare_store(milvus=milvus, es=None)
    store._ensure_collection()          # 采用成功（dim=4==space.dimension）


def test_v2_adopt_dim_mismatch_refused():
    milvus = _DescribeMilvus(_fields_with_dim(512),
                             {"index_type": "FLAT", "metric_type": "COSINE",
                              "field_name": "embedding"})
    store = _bare_store(milvus=milvus, es=None)
    with pytest.raises(m.P19CollectionDimensionMismatch):
        store._ensure_collection()


def test_v3_adopt_metric_mismatch_refused():
    milvus = _DescribeMilvus(_fields_with_dim(4),
                             {"index_type": "FLAT", "metric_type": "IP",
                              "field_name": "embedding"})
    store = _bare_store(milvus=milvus, es=None)
    with pytest.raises(m.P19CollectionMetricMismatch):
        store._ensure_collection()


def test_v3b_adopt_index_type_mismatch_refused():
    milvus = _DescribeMilvus(_fields_with_dim(4),
                             {"index_type": "HNSW", "metric_type": "COSINE",
                              "field_name": "embedding"})
    store = _bare_store(milvus=milvus, es=None)
    with pytest.raises(m.P19CollectionMetricMismatch):
        store._ensure_collection()


def test_v3c_adopt_dim_key_missing_refused():
    """describe 回包 dim 键缺/型错=无法证明=拒采用（fail-closed 不猜）。"""
    fields = _fields_with_dim(4)
    fields[-1] = {"name": "embedding", "type": "FLOAT_VECTOR", "params": {}}
    milvus = _DescribeMilvus(fields,
                             {"index_type": "FLAT", "metric_type": "COSINE",
                              "field_name": "embedding"})
    store = _bare_store(milvus=milvus, es=None)
    with pytest.raises(m.P19CollectionDimensionMismatch):
        store._ensure_collection()


# ---------- V4/V6 租约代次闸（写路径；签名对拍 upsert_chunks :288-343
# （仅 kw：record_id/chunks/scope_id/arrival_seq，business_date 取自 config）、
# cas_vector_state :410-442（record_id + kw to；es.index CAS 形）亲读钉） ----------

class _LeaseES:
    """权威双倍体：es.get（es_authority_source :233-261 形）+ es.index CAS
    （:433-438 形）；index 落地即改存 doc（供 claim→release 链读）。"""

    def __init__(self, doc):
        self._doc = doc
        self.bodies = []

    def get(self, index, id, realtime=True):
        return {"_seq_no": 11, "_primary_term": 2, "_source": self._doc}

    def index(self, index, id, document, if_seq_no, if_primary_term, refresh):
        assert (if_seq_no, if_primary_term) == (11, 2)
        self.bodies.append(document)
        self._doc = document
        return {"result": "updated"}


def _authority_with_lease(generation, *, with_journal=True, released=False):
    doc = {"record_id": RID, "item_id": "item-a", "scope_id": "default",
           "business_date": "2026-09-28", "arrival_seq": 1,
           "expires_at": "2099-01-01T00:00:00Z",
           "task_lease": {"vector": {
               "owner_id": None if released else "w1",
               "generation": generation,
               "lease_until": (None if released
                               else "2099-01-01T00:00:00Z")}},
           "diagnostics": {"vector": {}}}
    if with_journal:
        from news_flash_dedup.recall.vector_store import (
            vector_row_fingerprint)
        doc["diagnostics"]["vector"]["write_confirmation"] = {
            "generation": generation, "owner_id": "w1",
            "confirmed_at": "2026-10-07T01:00:00Z",
            "rows": {f"{RID}_0": vector_row_fingerprint(_row())},
        }
    return doc


class _WriteSpyMilvus:
    """upsert/get 双面（upsert_chunks :315-342 流程钉）：get 读存量。"""

    def __init__(self):
        self.rows = {}
        self.upsert_calls = 0

    def upsert(self, collection_name, data, partition_name=None):
        self.upsert_calls += 1
        self.rows[data["vector_id"]] = data
        return {"upsert_count": 1}

    def get(self, collection_name, ids, output_fields, consistency_level,
            **kw):
        assert consistency_level == "Strong"
        return [self.rows[v] for v in ids if v in self.rows]


def _lease_store(milvus, es):
    store = _bare_store(milvus=milvus, es=es)
    store.lease_authority = True                      # 设计签名：配置驱动
    return store


def test_v4_upsert_stale_generation_refused_zero_writes():
    """旧代次（持有 generation=2，权威已 generation=3）→ VectorLeaseStale
    且 Milvus 零写入（拒写先于任何副作用）。"""
    es = _LeaseES(_authority_with_lease(3))
    milvus = _WriteSpyMilvus()
    store = _lease_store(milvus, es)
    stale = m.VectorLease(owner_id="w1", generation=2,
                          lease_until="2099-01-01T00:00:00Z")
    with pytest.raises(m.VectorLeaseStale):
        store.upsert_chunks(record_id=RID, scope_id="default", arrival_seq=1,
                            chunks=[(0, [1.0, 0.0, 0.0, 0.0])], lease=stale)
    assert milvus.upsert_calls == 0
    assert es.bodies == []


def test_v4b_upsert_current_generation_succeeds_and_journals():
    """现行代次→写通；读回确认后日志落 ES（generation==租约代次、行指纹集）。"""
    es = _LeaseES(_authority_with_lease(3, with_journal=False))
    milvus = _WriteSpyMilvus()
    store = _lease_store(milvus, es)
    current = m.VectorLease(owner_id="w1", generation=3,
                            lease_until="2099-01-01T00:00:00Z")
    result = store.upsert_chunks(record_id=RID, scope_id="default",
                                 arrival_seq=1,
                                 chunks=[(0, [1.0, 0.0, 0.0, 0.0])],
                                 lease=current)
    assert result == [(f"{RID}_0", "created")]
    assert milvus.upsert_calls == 1
    from news_flash_dedup.recall.vector_store import vector_row_fingerprint
    journal = es.bodies[-1]["diagnostics"]["vector"]["write_confirmation"]
    assert journal["generation"] == 3
    assert journal["rows"][f"{RID}_0"] == vector_row_fingerprint(_row())


def test_v4c_upsert_without_authority_config_legacy_unchanged():
    """绿守卫：未配置 lease_authority=现役逐字节（UAT fake 轨零回归）。"""
    es = _LeaseES(_authority_with_lease(3, with_journal=False))
    milvus = _WriteSpyMilvus()
    store = _bare_store(milvus=milvus, es=es)
    result = store.upsert_chunks(record_id=RID, scope_id="default",
                                 arrival_seq=1,
                                 chunks=[(0, [1.0, 0.0, 0.0, 0.0])])
    assert result == [(f"{RID}_0", "created")]


def test_v6_claim_vector_lease_generation_monotonic():
    """领取=代次旧+1；释放保留代次（10 §6.4 L325 释放不清零）。"""
    es = _LeaseES(_authority_with_lease(3, with_journal=False,
                                        released=True))
    store = _lease_store(_WriteSpyMilvus(), es)
    claimed = store.claim_vector_lease(RID, owner_id="w2", ttl_seconds=60)
    assert claimed.generation == 4
    vec = es.bodies[-1]["task_lease"]["vector"]
    assert vec["owner_id"] == "w2" and vec["generation"] == 4
    store.release_vector_lease(RID, claimed)
    vec2 = es.bodies[-1]["task_lease"]["vector"]
    assert vec2["owner_id"] is None and vec2["generation"] == 4    # 不清零


# ---------- V5 读路径代次闸 ----------

def test_v5_get_row_stale_fingerprint_rejected():
    """行指纹与日志不符（陈旧代次行）→ VectorRowStale（fail-closed 族）。"""
    es = _LeaseES(_authority_with_lease(3))

    class _StaleGetMilvus:
        def get(self, collection_name, ids, output_fields, consistency_level,
                **kw):
            return [_row(arrival_seq=9)]          # 与日志指纹不符

    store = _lease_store(_StaleGetMilvus(), es)
    with pytest.raises(m.VectorRowStale):
        store.get_vector_row(f"{RID}_0")
    assert issubclass(m.VectorRowStale, VectorWriteUnknown)       # 族谱钉


def test_v5b_get_row_unconfirmed_rejected():
    """日志缺席（未确认行）→ VectorRowStale（未确认不冒充存在）。"""
    es = _LeaseES(_authority_with_lease(3, with_journal=False))

    class _GetMilvus:
        def get(self, collection_name, ids, output_fields, consistency_level,
                **kw):
            return [_row()]

    store = _lease_store(_GetMilvus(), es)
    with pytest.raises(m.VectorRowStale):
        store.get_vector_row(f"{RID}_0")


# ---------- V7 ready 翻转日志前置（与 H-02 H2-6 同修钉） ----------

def test_v7_cas_ready_requires_and_carries_journal():
    """pending→ready：日志在场（upsert 已落）且 generation==租约代次→放行，
    CAS body 原样携带（日志-状态同源，INV-6 族扩展）。"""
    doc = _authority_with_lease(3)
    doc["vector_state"] = "pending"
    es = _LeaseES(doc)
    store = _lease_store(_WriteSpyMilvus(), es)
    current = m.VectorLease(owner_id="w1", generation=3,
                            lease_until="2099-01-01T00:00:00Z")
    store.cas_vector_state(RID, to="ready", lease=current)
    journal = es.bodies[-1]["diagnostics"]["vector"]["write_confirmation"]
    assert journal["generation"] == 3
    assert f"{RID}_0" in journal["rows"]
    assert es.bodies[-1]["vector_state"] == "ready"


def test_v7b_cas_ready_without_journal_refused():
    """日志缺席→非法翻转拒绝零写入（INV-6 族；确认前不冒充 ready）。"""
    doc = _authority_with_lease(3, with_journal=False)
    doc["vector_state"] = "pending"
    es = _LeaseES(doc)
    store = _lease_store(_WriteSpyMilvus(), es)
    current = m.VectorLease(owner_id="w1", generation=3,
                            lease_until="2099-01-01T00:00:00Z")
    with pytest.raises(m.VectorStateTransitionError):
        store.cas_vector_state(RID, to="ready", lease=current)
    assert es.bodies == []
