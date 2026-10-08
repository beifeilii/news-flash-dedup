# -*- coding: utf-8 -*-
"""2026-10-09 P0-a 修订二红测：覆盖闸 frontier 写侧接线
（DEDUP_COVERAGE_FRONTIER 默认关）。

出处=主窗口修订令 + log\\判定链改造技术设计书-v1-1009.md §②（六份外部评审+
主控亲核坐实的锁②：VectorFrontierAdvancer 无现役消费点 → vector_prepared
快照零写者 → vector_store.py:430-447 fail-closed 恒 unproven →
coverage_complete 恒 False，T 数据 4,789/4,789 实证；修订令废止初稿
"召回融合时推进"——那是读侧，证明不了写已发生）：
- 接线点=vector/milvus_store.py upsert_chunks 写确认尾段（全 chunk
  upsert_count==1 + Strong 回读逐字段相等之后——Milvus 写入 ack 之侧）；
- 开关关=老行为逐字节（真写确认照常返回、日控制文档零写入、读侧恒 False）；
- 开关开+真写确认 → 真 Advancer 推进 → 真 Provider 读回 → 读侧覆盖 True
  （验收口径"覆盖证明真实有效"：证明由写 ack 产出，非读侧捏造）；
- 开关开+序号断档（无证明不跳洞）/预置孔洞（保留不洗零）/推进器抛错
  （不反咬已确认写入）→ 读侧仍 False（fail-closed 不破，宁缺毋滥）。

夹具纪律（施工单④）：被测面全真实现类——RealMilvusP19Store.upsert_chunks
（真写确认路径）/VectorFrontierAdvancer（真写侧 CAS 推进）/
VectorFrontierProvider（真读侧）/MilvusVectorStore（真覆盖求值）；仅外部
系统（Milvus/ES 权威/日控制文档底座）用双倍体——test_e_m09_vector_lease.py
_bare_store / test_e_h02_vector_frontier.py 同型先例。
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from news_flash_dedup.recall.models import RecallRequest
from news_flash_dedup.recall.vector_frontier import (
    VectorFrontierAdvancer,
    coverage_frontier_enabled,
)
from news_flash_dedup.recall.vector_space import EmbeddingSpace
from news_flash_dedup.recall.vector_store import (
    MilvusVectorStore,
    VectorFrontierProvider,
)
from news_flash_dedup.vector import milvus_store as p19

NOW = datetime(2026, 10, 9, 1, tzinfo=timezone.utc)
DAY = "2026-09-28"
SPACE = EmbeddingSpace("mock_embedding", "fixture_v1", "codepoint_v1", "none",
                       "COSINE", 4, "plain_v1", "plain_v1",
                       "tokens512_overlap64_v1", "p09_v1")
CONFIG = p19.RealMilvusP19Config(run_uuid="wine09a",
                                 business_date=date(2026, 9, 28), space=SPACE)
RID_Q = "f" * 64
RID_HIT = "c" * 64
COLLECTION = f"news_dedup_replay_ab12cd_{SPACE.space_id}"
ES_PREFIX = "p01-batch-x1-"


def _rid(seed: str) -> str:
    return f"{ord(seed):x}".rjust(64, "0")


# ---------- 外部系统双倍体：P19 写路径（Milvus/ES 权威） ----------

class _FakeP19Milvus:
    """Milvus 双倍体（P19 写路径最小面）：upsert 落行 + Strong get 按主键读。"""

    def __init__(self):
        self.rows = {}
        self.upsert_calls = []

    def upsert(self, collection_name, data, partition_name):
        self.upsert_calls.append((data["vector_id"], partition_name))
        self.rows[data["vector_id"]] = dict(data)
        return {"upsert_count": 1}

    def get(self, collection_name, ids, output_fields, consistency_level):
        assert consistency_level == "Strong"
        return [self.rows[i] for i in ids if i in self.rows]


class _FakeP19ES:
    """ES 权威双倍体：按 record_id 供主记录（es_authority_source 实时 GET 形）。"""

    def __init__(self):
        self.docs = {}

    def put(self, record_id, *, scope_id="default", business_date=DAY,
            arrival_seq=1):
        self.docs[record_id] = {
            "record_id": record_id, "scope_id": scope_id,
            "business_date": business_date, "arrival_seq": arrival_seq}

    def get(self, index, id, realtime=True):
        assert realtime is True
        return {"_seq_no": 1, "_primary_term": 1, "_source": self.docs[id]}


class _RaisingAdvancer:
    """推进端口双倍体：advance 恒抛错（快照故障场景）。"""

    def __init__(self):
        self.calls = 0

    def advance(self, *args, **kwargs):
        self.calls += 1
        raise RuntimeError("snapshot store outage")


def _p19_store(milvus, es, *, advancer=None):
    """真 RealMilvusP19Store（__new__ 裸件=绕过 P19_CONFIRM_UAT 环境闸，
    test_e_m09_vector_lease._bare_store 同型先例）；advancer 显式注入。"""
    store = p19.RealMilvusP19Store.__new__(p19.RealMilvusP19Store)
    store.milvus = milvus
    store.es = es
    store.config = CONFIG
    if advancer is not None:
        store.vector_frontier_advancer = advancer
    return store


def _write(store, es, *, rid, arrival_seq, chunks=((0, [1.0, 0.0, 0.0, 0.0]),)):
    """真写确认路径：ES 权威落主记录 → upsert_chunks 全 chunk 回读确认。"""
    es.put(rid, arrival_seq=arrival_seq)
    return store.upsert_chunks(record_id=rid, chunks=list(chunks),
                               scope_id="default", arrival_seq=arrival_seq)


# ---------- 外部系统双倍体：日控制文档底座 + 读侧（test_e_h02 同型） ----------

class _DayControlBacking:
    """日控制文档底座：Advancer 写侧与 Provider 读侧双鸭式共享同一真源。"""

    def __init__(self, source=None):
        self._source = source
        self._seq_no = 0

    def source(self):
        return self._source


class _AdvancerStoreView:
    """Advancer 写侧鸭式（ElasticsearchBatchStore 形：get→{source,seq_no,
    primary_term}/create/replace/is_conflict）。"""

    def __init__(self, backing):
        self._b = backing

    def get(self, index, key):
        if self._b.source() is None:
            return None
        return {"source": self._b.source(), "seq_no": self._b._seq_no,
                "primary_term": 1}

    def create(self, index, key, body):
        self._b._source = body
        self._b._seq_no += 1

    def replace(self, index, key, body, seq_no, primary_term):
        assert (seq_no, primary_term) == (self._b._seq_no, 1)
        self._b._source = body
        self._b._seq_no += 1

    @staticmethod
    def is_conflict(error):
        return False


class _ProviderStoreView:
    """Provider 读侧鸭式（get(index, doc_id)→{"_source": ...} | None）。"""

    def __init__(self, backing):
        self._b = backing

    def get(self, index, doc_id):
        if self._b.source() is None:
            return None
        return {"_source": self._b.source()}


class _FakeMilvus:
    """读侧 Milvus 双倍体：恒一命中（len<80 → 单页 break）。"""

    def search(self, collection_name, data, filter, limit, output_fields,
               partition_names, anns_field, search_params, consistency_level,
               **kw):
        assert consistency_level == "Strong"
        return [[{"id": f"{RID_HIT}_0", "distance": 0.99,
                  "entity": {"vector_id": f"{RID_HIT}_0", "record_id": RID_HIT,
                             "scope_id": "default", "business_date": DAY,
                             "arrival_seq": 1,
                             "embedding_space_id": SPACE.space_id,
                             "chunk_id": 0}}]]


class _FakeES:
    """读侧 ES 权威回查双倍体（es.get 形）：命中记录供 found/_source。"""

    def get(self, index, id, realtime=True):
        if id != RID_HIT:
            return {"_index": index, "_id": id, "found": False}
        return {"_index": index, "_id": id, "found": True,
                "_source": {
                    "record_id": RID_HIT, "item_id": "item-hit",
                    "scope_id": "default", "business_date": DAY,
                    "arrival_seq": 1, "embedding_space_id": SPACE.space_id,
                    "text": "权威正文", "expires_at": "2099-01-01T00:00:00Z"}}


def _advancer(backing):
    return VectorFrontierAdvancer(_AdvancerStoreView(backing), clock=lambda: NOW)


def _readside_coverage(backing, *, arrival_seq):
    """真读侧链：Provider(真)→MilvusVectorStore(真) 覆盖求值（search 内径）。"""
    provider = VectorFrontierProvider(_ProviderStoreView(backing), ES_PREFIX)
    store = MilvusVectorStore(_FakeMilvus(), _FakeES(), COLLECTION, ES_PREFIX,
                              SPACE, clock=lambda: NOW,
                              frontier_provider=provider)
    request = RecallRequest("default", DAY, RID_Q, "item-q", arrival_seq,
                            "查询正文", visible_seq=1,
                            prepared_seq=arrival_seq - 1,
                            embedding_space_id=SPACE.space_id)
    return store.search(request, [1.0, 0.0, 0.0, 0.0]).coverage_complete


def _snapshot(**over):
    snap = {"schema_version": "vector-prepared-v1", "scope_id": "default",
            "business_date": DAY, "space_id": SPACE.space_id,
            "vector_frontier": 0, "max_ready_seen": 0,
            "reconciled_through": 0, "hole_count": 0,
            "foreign_proof_count": 0, "generation": 0,
            "advanced_at": "2026-10-09T00:00:00Z"}
    snap.update(over)
    return snap


def _day_source(snapshot):
    return {"kind": "day", "scope_id": "default", "business_date": DAY,
            "checkpoint": {"vector_prepared": snapshot}}


def _snapshot_of(backing):
    source = backing.source()
    return (source.get("checkpoint") or {}).get("vector_prepared")


# ---------- 开关解析（严格、默认关） ----------

def test_coverage_frontier_switch_parsing_strict_default_off():
    assert coverage_frontier_enabled({}) is False
    assert coverage_frontier_enabled({"DEDUP_COVERAGE_FRONTIER": ""}) is False
    assert coverage_frontier_enabled({"DEDUP_COVERAGE_FRONTIER": "1"}) is True
    for bad in ("0", "true", "True", "ON", "on", "yes", " 1", "1 ", "2"):
        assert coverage_frontier_enabled(
            {"DEDUP_COVERAGE_FRONTIER": bad}) is False


# ---------- ① 开关关=老行为逐字节 ----------

def test_switch_off_write_confirmed_zero_snapshot_readside_false(monkeypatch):
    """① 开关关：真写确认照常（upsert_count==1+回读相等→created），但
    日控制文档零写入（接线点零副作用）→ 读侧覆盖恒 False（现役
    4,789/4,789 形态，老路径逐字节）。"""
    monkeypatch.delenv("DEDUP_COVERAGE_FRONTIER", raising=False)
    milvus, es, backing = _FakeP19Milvus(), _FakeP19ES(), _DayControlBacking()
    store = _p19_store(milvus, es, advancer=_advancer(backing))
    report = _write(store, es, rid=_rid("a"), arrival_seq=1)
    assert report == [(_rid("a") + "_0", "created")]     # 写入照常确认
    assert len(milvus.upsert_calls) == 1
    assert backing.source() is None                       # 零快照写入
    assert _readside_coverage(backing, arrival_seq=2) is False


# ---------- ② 开关开+真写确认→覆盖证明真实有效 ----------

def test_switch_on_write_ack_advances_snapshot_readside_true(monkeypatch):
    """② 开关开：upsert_chunks 写确认尾段经真 Advancer 推进快照
    （frontier=1/无孔/代际=1/schema/空间/域日正确）→ 真 Provider 读回 →
    读侧覆盖求值 True——覆盖证明真实有效（证明源自 Milvus 写 ack 之侧）。"""
    monkeypatch.setenv("DEDUP_COVERAGE_FRONTIER", "1")
    milvus, es, backing = _FakeP19Milvus(), _FakeP19ES(), _DayControlBacking()
    store = _p19_store(milvus, es, advancer=_advancer(backing))
    report = _write(store, es, rid=_rid("a"), arrival_seq=1)
    assert report == [(_rid("a") + "_0", "created")]
    snapshot = _snapshot_of(backing)
    assert snapshot is not None
    assert snapshot["vector_frontier"] == 1
    assert snapshot["max_ready_seen"] == 1
    assert snapshot["hole_count"] == 0
    assert snapshot["generation"] == 1
    assert snapshot["schema_version"] == "vector-prepared-v1"
    assert snapshot["space_id"] == SPACE.space_id
    assert (snapshot["scope_id"], snapshot["business_date"]) == ("default", DAY)
    assert _readside_coverage(backing, arrival_seq=2) is True


def test_switch_on_advancer_not_injected_no_advance(monkeypatch):
    """②b 开关开但未显式注入推进器 → 零推进零快照 → 读侧恒 False
    （fail-closed 装配缺省：不自装配、不伪造写者）。"""
    monkeypatch.setenv("DEDUP_COVERAGE_FRONTIER", "1")
    milvus, es, backing = _FakeP19Milvus(), _FakeP19ES(), _DayControlBacking()
    store = _p19_store(milvus, es)                       # 未注入 advancer
    report = _write(store, es, rid=_rid("a"), arrival_seq=1)
    assert report == [(_rid("a") + "_0", "created")]     # 写入不受影响
    assert backing.source() is None
    assert _readside_coverage(backing, arrival_seq=2) is False


# ---------- ③ 开关开+断档/孔洞/推进故障→读侧仍 False ----------

def test_switch_on_gap_sequence_frontier_stalls_no_hole_jump(monkeypatch):
    """③a 开关开+序号断档（写 seq=1 后写 seq=3，seq=2 无写确认）：前沿
    停在 1（无证明不跳洞）→ 查询 arrival_seq=3 需 frontier≥2 → 恒 False
    （宁缺毋滥，不以未证连续冒充完整）。"""
    monkeypatch.setenv("DEDUP_COVERAGE_FRONTIER", "1")
    milvus, es, backing = _FakeP19Milvus(), _FakeP19ES(), _DayControlBacking()
    store = _p19_store(milvus, es, advancer=_advancer(backing))
    _write(store, es, rid=_rid("a"), arrival_seq=1)
    _write(store, es, rid=_rid("b"), arrival_seq=3)      # 跳过 seq=2
    snapshot = _snapshot_of(backing)
    assert snapshot["vector_frontier"] == 1              # 停在断档前
    assert snapshot["max_ready_seen"] == 3               # 已见如实记账
    assert _readside_coverage(backing, arrival_seq=3) is False


def test_switch_on_preexisting_hole_preserved_stays_false(monkeypatch):
    """③b 开关开+预置孔洞（hole_count=1 快照在场）：推进保留孔洞计数
    （写侧不透传 hole_count、不洗零）→ 读侧恒 False（fail-closed 不破）。"""
    monkeypatch.setenv("DEDUP_COVERAGE_FRONTIER", "1")
    backing = _DayControlBacking(
        _day_source(_snapshot(vector_frontier=0, hole_count=1, generation=3)))
    milvus, es = _FakeP19Milvus(), _FakeP19ES()
    store = _p19_store(milvus, es, advancer=_advancer(backing))
    _write(store, es, rid=_rid("a"), arrival_seq=1)
    snapshot = _snapshot_of(backing)
    assert snapshot["vector_frontier"] == 1              # 前沿推进
    assert snapshot["hole_count"] == 1                   # 孔洞保留（不洗零）
    assert snapshot["generation"] == 4                   # 推进留痕
    assert _readside_coverage(backing, arrival_seq=2) is False


def test_switch_on_advance_failure_does_not_bite_confirmed_write(monkeypatch):
    """③c 开关开+推进器抛错（快照故障）：已确认写入不被反咬——
    upsert_chunks 照常返回 created；快照缺席 → 读侧恒 False（维持停摆，
    fail-closed 安全向）。"""
    monkeypatch.setenv("DEDUP_COVERAGE_FRONTIER", "1")
    milvus, es, backing = _FakeP19Milvus(), _FakeP19ES(), _DayControlBacking()
    raising = _RaisingAdvancer()
    store = _p19_store(milvus, es, advancer=raising)
    report = _write(store, es, rid=_rid("a"), arrival_seq=1)
    assert report == [(_rid("a") + "_0", "created")]     # 写入确认不受影响
    assert raising.calls == 1                            # 推进确被尝试
    assert backing.source() is None                      # 快照未落
    assert _readside_coverage(backing, arrival_seq=2) is False
