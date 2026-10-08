# -*- coding: utf-8 -*-
"""2026-10-09 P0-a 红测：覆盖闸 frontier 接线（DEDUP_COVERAGE_FRONTIER 默认关）。

出处=log\\temp\\判定链修复方案-P0施工单-呈外部评审.md §一-P0-a（六份外部
评审+主控亲核坐实的锁②：VectorFrontierAdvancer 无现役消费点 →
vector_prepared 快照零写者 → vector_store.py:430-447 fail-closed 恒
unproven → coverage_complete 恒 False，T 数据 4,789/4,789 实证）：
- 开关关=老行为逐字节（恒 False，且日控制文档零写入——接线点零副作用）；
- 开关开+证据真实（prepared 水印覆盖 arrival_seq-1、快照无孔）→ True；
- 开关开+快照有孔/前沿滞后（时序倒错）→ 仍 False（fail-closed 不破，
  宁缺毋滥——施工单防护①）。

接线点（施工报告同源）：recall/service.py build_plan 五通道扇出前——覆盖
求值发生在向量通道 search 内部（vector_store.py:430-447，扇出并行体内），
融合完成后再推进对本请求恒无效且按单飞升序链结构性滞后一件，故推进
必须先于扇出。

夹具纪律（施工单④）：被测面全真实现类——VectorFrontierAdvancer（真写侧
CAS 推进）/VectorFrontierProvider（真读侧）/MilvusVectorStore（真覆盖求值）/
RealVectorSearcher+QueryEmbedder（真查询编码装配）/RecallService（真编排）；
仅外部系统（Milvus/ES 权威/嵌入 API/日控制文档底座）用双倍体——
test_e_h02_vector_frontier.py/test_recall_service.py 同型先例。
"""

from __future__ import annotations

from datetime import datetime, timezone

from news_flash_dedup.recall.embedding_query import (
    QueryEmbedder,
    RealVectorSearcher,
)
from news_flash_dedup.recall.models import ChannelResult, RecallRequest
from news_flash_dedup.recall.service import (
    NullVectorSearcher,
    RecallService,
    coverage_frontier_enabled,
)
from news_flash_dedup.recall.vector_frontier import VectorFrontierAdvancer
from news_flash_dedup.recall.vector_space import EmbeddingSpace
from news_flash_dedup.recall.vector_store import (
    MilvusVectorStore,
    VectorFrontierProvider,
)

NOW = datetime(2026, 10, 9, 1, tzinfo=timezone.utc)
DAY = "2026-09-28"
SPACE = EmbeddingSpace("mock_embedding", "fixture_v1", "codepoint_v1", "none",
                       "COSINE", 4, "plain_v1", "plain_v1",
                       "tokens512_overlap64_v1", "p09_v1")
RID_Q = "f" * 64
RID_HIT = "c" * 64
COLLECTION = f"news_dedup_replay_ab12cd_{SPACE.space_id}"
ES_PREFIX = "p01-batch-x1-"


# ---------- 外部系统双倍体（test_e_h02_vector_frontier.py 同型） ----------

class _FakeMilvus:
    """Milvus 双倍体：恒一命中（len<80 → 单页 break）。"""

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
    """ES 权威回查双倍体（es.get 形）：命中记录供 found/__source。"""

    def get(self, index, id, realtime=True):
        if id != RID_HIT:
            return {"_index": index, "_id": id, "found": False}
        return {"_index": index, "_id": id, "found": True,
                "_source": {
                    "record_id": RID_HIT, "item_id": "item-hit",
                    "scope_id": "default", "business_date": DAY,
                    "arrival_seq": 1, "embedding_space_id": SPACE.space_id,
                    "text": "权威正文", "expires_at": "2099-01-01T00:00:00Z"}}


class _FakeEmbeddingClient:
    """嵌入 API 双倍体：维度与空间一致（QueryEmbedder INV-4 闸），恒向量化。"""

    dimension = SPACE.dimension

    def embed_query(self, text):
        return [1.0, 0.0, 0.0, 0.0]


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


class _FakeChannel:
    """ES 四通道双倍体：固定 complete ChannelResult 回放
    （test_recall_service._FakeChannel 同型）。"""

    def __init__(self, result):
        self._result = result

    def search(self, request, query_vector=None):
        return self._result


def _complete(channel):
    return ChannelResult(channel, "complete", (), f"{channel}_v1",
                         visible_seq=4, prepared_seq=4, coverage_complete=True)


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


def _service(backing, *, embedding_space_id=SPACE.space_id):
    """全真接线：Advancer(真)→底座←Provider(真)→MilvusVectorStore(真)
    →RealVectorSearcher(真)→RecallService(真)。"""
    advancer = VectorFrontierAdvancer(
        _AdvancerStoreView(backing), clock=lambda: NOW)
    provider = VectorFrontierProvider(_ProviderStoreView(backing), ES_PREFIX)
    store = MilvusVectorStore(_FakeMilvus(), _FakeES(), COLLECTION, ES_PREFIX,
                              SPACE, clock=lambda: NOW,
                              frontier_provider=provider)
    searcher = RealVectorSearcher(
        store, QueryEmbedder(_FakeEmbeddingClient(), SPACE))
    service = RecallService(
        None, ES_PREFIX,
        channels={name: _FakeChannel(_complete(name))
                  for name in ("hash", "near", "bm25", "entity")},
        vector_searcher=searcher,
        vector_frontier_advancer=advancer,
        clock=lambda: NOW,
    )
    return service


def _request(*, arrival_seq=2, prepared_seq=1,
             embedding_space_id=SPACE.space_id):
    return RecallRequest("default", DAY, RID_Q, "item-q", arrival_seq,
                         "查询正文", visible_seq=1, prepared_seq=prepared_seq,
                         embedding_space_id=embedding_space_id)


def _embedding_audit(plan):
    return {a.channel: a for a in plan.channel_audits}["embedding"]


def _snapshot_of(backing):
    source = backing.source()
    return (source.get("checkpoint") or {}).get("vector_prepared")


# ---------- 开关解析 ----------

def test_coverage_frontier_switch_parsing_strict_default_off():
    assert coverage_frontier_enabled({}) is False
    assert coverage_frontier_enabled({"DEDUP_COVERAGE_FRONTIER": ""}) is False
    assert coverage_frontier_enabled({"DEDUP_COVERAGE_FRONTIER": "1"}) is True
    for bad in ("0", "true", "True", "ON", "on", "yes", " 1", "1 ", "2"):
        assert coverage_frontier_enabled(
            {"DEDUP_COVERAGE_FRONTIER": bad}) is False


# ---------- ① 开关关=老行为逐字节 ----------

def test_switch_off_coverage_stays_false_and_zero_snapshot_writes(monkeypatch):
    """① 开关关：coverage_complete 恒 False（现役 4,789/4,789 形态），且
    接线点零副作用——日控制文档全程无写入（老路径逐字节）。"""
    monkeypatch.delenv("DEDUP_COVERAGE_FRONTIER", raising=False)
    backing = _DayControlBacking()
    service = _service(backing)
    plan = service.build_plan(_request())
    assert backing.source() is None                     # 零快照写入
    assert _embedding_audit(plan).coverage_complete is False
    assert ("embedding", "COVERAGE_UNPROVEN") in {
        (gap.channel, gap.reason) for gap in plan.recall_gaps}
    assert plan.recall_incomplete is True


# ---------- ② 开关开+证据真实→True ----------

def test_switch_on_genuine_evidence_coverage_true(monkeypatch):
    """② 开关开：prepared 水印（=1）覆盖 arrival_seq-1（=1），推进器真写
    快照（无孔）→ 读侧证明在场 → 覆盖求值 True；计划级覆盖同步 True
    （"不重复"分支结构性可达）。"""
    monkeypatch.setenv("DEDUP_COVERAGE_FRONTIER", "1")
    backing = _DayControlBacking()
    service = _service(backing)
    plan = service.build_plan(_request(arrival_seq=2, prepared_seq=1))
    # 快照真推进：前沿=水印、代际=1、无孔、schema/空间/域日正确
    snapshot = _snapshot_of(backing)
    assert snapshot is not None
    assert snapshot["vector_frontier"] == 1
    assert snapshot["max_ready_seen"] == 1
    assert snapshot["hole_count"] == 0
    assert snapshot["generation"] == 1
    assert snapshot["schema_version"] == "vector-prepared-v1"
    assert snapshot["space_id"] == SPACE.space_id
    assert (snapshot["scope_id"], snapshot["business_date"]) == ("default", DAY)
    # 向量通道覆盖求值 True（融合审计面）→ 计划级零缺口 → True
    assert _embedding_audit(plan).coverage_complete is True
    assert plan.recall_incomplete is False
    current_doc = {
        "record_id": RID_Q, "item_id": "item-q", "text": "查询正文",
        "raw_hash": "0" * 64, "scope_id": "default", "business_date": DAY,
        "arrival_seq": 2, "expires_at": "2099-01-01T00:00:00Z",
    }
    _, _, coverage_complete, _ = service.build_commit_inputs(plan, current_doc)
    assert coverage_complete is True


def test_switch_on_null_vector_searcher_never_advances(monkeypatch):
    """②b 防护：NullVectorSearcher 过渡态=无向量通道——开关开亦不推进
    （无通道写覆盖快照=伪造向量证据，结构性禁绝）。"""
    monkeypatch.setenv("DEDUP_COVERAGE_FRONTIER", "1")
    backing = _DayControlBacking()
    advancer = VectorFrontierAdvancer(
        _AdvancerStoreView(backing), clock=lambda: NOW)
    service = RecallService(
        None, ES_PREFIX,
        channels={name: _FakeChannel(_complete(name))
                  for name in ("hash", "near", "bm25", "entity")},
        vector_searcher=NullVectorSearcher(clock=lambda: NOW),
        vector_frontier_advancer=advancer,
        clock=lambda: NOW,
    )
    plan = service.build_plan(_request())
    assert backing.source() is None                     # 零推进
    assert _embedding_audit(plan).coverage_complete is False
    assert plan.recall_incomplete is True


# ---------- ③ 开关开+快照有孔/时序倒错→仍 False ----------

def test_switch_on_snapshot_with_holes_stays_false(monkeypatch):
    """③a 开关开+快照有孔（hole_count=1 预置）：推进保留孔洞计数（服务
    不透传 hole_count）→ 覆盖求值恒 False（fail-closed 不破）。"""
    monkeypatch.setenv("DEDUP_COVERAGE_FRONTIER", "1")
    backing = _DayControlBacking(
        _day_source(_snapshot(vector_frontier=5, max_ready_seen=5,
                              hole_count=1, generation=3)))
    service = _service(backing)
    plan = service.build_plan(_request(arrival_seq=2, prepared_seq=1))
    snapshot = _snapshot_of(backing)
    assert snapshot["vector_frontier"] == 5             # 单调不回退
    assert snapshot["hole_count"] == 1                  # 孔洞保留（不洗零）
    assert snapshot["generation"] == 4                  # 推进留痕
    assert _embedding_audit(plan).coverage_complete is False
    assert plan.recall_incomplete is True


def test_switch_on_frontier_lagging_stays_false(monkeypatch):
    """③b 开关开+前沿滞后（时序倒错：prepared=1 < arrival_seq-1=2）：
    快照如实推进至 1 但够不到本请求水位 → 覆盖求值恒 False（宁缺毋滥，
    不以前沿不足的快照冒充完整）。"""
    monkeypatch.setenv("DEDUP_COVERAGE_FRONTIER", "1")
    backing = _DayControlBacking()
    service = _service(backing)
    plan = service.build_plan(_request(arrival_seq=3, prepared_seq=1))
    snapshot = _snapshot_of(backing)
    assert snapshot["vector_frontier"] == 1             # 如实推进到证据所及
    assert _embedding_audit(plan).coverage_complete is False
    assert plan.recall_incomplete is True
