"""W-R3a 码面修复窗红测（外部双审核 R3-H4/R3-M4/N3-05/N3-06 五修）。

红测清单映射（修复清单①–⑤逐条对位）：
- ① R3-H4-① embedding 响应 index 对位（embedding_client._validate_payload）：
  乱序 / 缺 index / 错 index（错位·重复·跳号·布尔）三注入 → VectorWriteUnknown
  （fail-closed）；合法 index 保序对位放行。
- ② R3-H4-② pipeline 孤儿 chunk（pipeline.ingest_record）：upsert 前置源态
  门禁——源态非 pending（failed/ready）→ VectorStateTransitionError 明确异常 +
  零写入（无孤儿 chunk）+ 源态不动。
- ③ N3-05 gate3 实算化（backfill.b2_verify_gates）：fp 口径从工件重算——
  硬负例缺位/非法占比/占比与总数不自洽/判重结论在场 → gate3=False；
  gate2 从 missed_paraphrase+语料总数重算并交叉核验自报；gate1/warm_run
  回读自报字段者改名「汇总核验」（名实相符）。
- ④ N3-06 usage 计数器锁：并发写径 api_calls/total_tokens 与查询径磁盘缓存
  命中 cache_hits 终值精确（计数器读写面受锁，台账精度）。
- ⑤ R3-M4 可观测性对齐：default_probes 五通道齐备（hash/near/bm25 查日索引、
  embedding 查 Milvus 集合在场、entity 查准备产物），码全 ∈ readiness 五通道
  词表（recall_chain_unavailable:embedding 死码消除）；水位探针 scope_id 注入
  不再硬编码 "default"。
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from news_flash_dedup.admission import CONTROL_INDEX
from news_flash_dedup.api.host import default_probes
from news_flash_dedup.es_admission_schema import day_index
from news_flash_dedup.product.readiness import ReadinessPort
from news_flash_dedup.recall.prepare import ElasticsearchWatermarkProvider
from news_flash_dedup.recall.service import NullVectorSearcher
from news_flash_dedup.recall.vector_space import EmbeddingSpace
from news_flash_dedup.recall.vector_store import VectorWriteUnknown
from news_flash_dedup.vector import backfill as bf
from news_flash_dedup.vector import embedding_client as ec
from news_flash_dedup.vector import pipeline as vp
from news_flash_dedup.vector import qwen_bpe as qb
from news_flash_dedup.vector.milvus_store import VectorStateTransitionError

from b4_fake_es import FakeESClient


CN = timezone(timedelta(hours=8))
T0 = datetime(2026, 9, 29, 10, 0, tzinfo=CN)
DAY = "2026-09-29"
DIM = 4
PREFIX = "p01-batch-winr3a-"


def _vec(seed=1.0):
    return [float(seed), 0.5, 0.25, 0.125]


def _payload(vectors, *, indices=None, tokens=5):
    """合法响应形态（index 逐位对位）；indices=False 注入缺 index。"""
    data = []
    for position, vector in enumerate(vectors):
        item = {"embedding": list(vector)}
        if indices is not False:
            item["index"] = indices[position] if indices is not None else position
        data.append(item)
    return {"data": data, "usage": {"total_tokens": tokens}}


class FakeTransport:
    def __init__(self):
        self.calls = []
        self.script = []

    def push(self, item):
        self.script.append(item)
        return self

    def __call__(self, *, base_url, model, api_key, texts, timeout_s):
        self.calls.append(list(texts))
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _client(tmp_path, transport, **kwargs):
    config = ec.EmbeddingClientConfig(
        base_url="https://dashscope.example/v1", model="text-embedding-v3",
        dimension=DIM, cache_root=tmp_path / "cache", daily_token_budget=5_000_000,
        **kwargs)
    return ec.EmbeddingClient(
        config, transport_fn=transport,
        env={"DEDUP_EMBEDDING_API_KEY": "sk-testkey1234567890abcd"},
        clock=lambda: T0, sleep=lambda s: None, rng=lambda: 0.0)


# ---------- ① R3-H4-① embedding 响应 index 对位（fail-closed） ----------

def test_r3h4_index_aligned_response_accepted_in_order(tmp_path):
    transport = FakeTransport().push(_payload([_vec(1.0), _vec(2.0)]))
    client = _client(tmp_path, transport)
    assert client.embed_documents(["文本甲", "文本乙"]) == [_vec(1.0), _vec(2.0)]


def test_r3h4_out_of_order_index_rejected_write_unknown(tmp_path):
    # 乱序注入：data[0].index=1 / data[1].index=0 → 确认未知
    transport = FakeTransport().push(
        _payload([_vec(1.0), _vec(2.0)], indices=[1, 0]))
    client = _client(tmp_path, transport)
    with pytest.raises(VectorWriteUnknown, match="(?i)embedding response is unknown"):
        client.embed_documents(["文本甲", "文本乙"])


def test_r3h4_missing_index_rejected_write_unknown(tmp_path):
    # 缺 index 注入：item 无 index 字段 → 确认未知
    transport = FakeTransport().push(_payload([_vec()], indices=False))
    client = _client(tmp_path, transport)
    with pytest.raises(VectorWriteUnknown, match="(?i)embedding response is unknown"):
        client.embed_documents(["文本甲"])


@pytest.mark.parametrize("indices", [
    [0, 0],        # 重复 index（第二位错位）
    [0, 2],        # 跳号（index 越界）
    [1],           # 单件错位（0→1）
    [False],       # 布尔冒充（False==0 朴素相等陷阱）
])
def test_r3h4_wrong_index_rejected_write_unknown(tmp_path, indices):
    transport = FakeTransport().push(
        _payload([_vec(float(i + 1)) for i in range(len(indices))],
                 indices=indices))
    client = _client(tmp_path, transport)
    with pytest.raises(VectorWriteUnknown, match="(?i)embedding response is unknown"):
        client.embed_documents([f"文本{i}" for i in range(len(indices))])


# ---------- ② R3-H4-② pipeline 孤儿 chunk（upsert 前置源态门禁） ----------

def _space():
    return EmbeddingSpace("mock_embedding", "fixture_v1", "codepoint_v1", "none",
                          "COSINE", DIM, "plain_v1", "plain_v1",
                          "tokens512_overlap64_bpe_v1", "p09_v1")


class FakeP19Store:
    """RealMilvusP19Store 鸭式替身（接口面：state 读/upsert/cas）。"""

    def __init__(self, space, *, initial_states=()):
        self.space = space
        self.rows = {}
        self.states = dict(initial_states)
        self.upsert_calls = []

    def get_vector_state(self, record_id):
        return self.states.get(record_id, "pending")

    def upsert_chunks(self, *, record_id, chunks, scope_id, arrival_seq):
        if self.get_vector_state(record_id) is None:
            raise VectorWriteUnknown("ES authority record is absent")
        self.upsert_calls.append(record_id)
        report = []
        for chunk_id, embedding in chunks:
            key = f"{record_id}_{chunk_id}"
            self.rows.setdefault(key, (record_id, chunk_id, tuple(embedding)))
            report.append((key, "created"))
        return report

    def cas_vector_state(self, record_id, *, to):
        if to not in ("ready", "failed"):
            raise ValueError("to must be ready|failed")
        if self.get_vector_state(record_id) != "pending":
            raise VectorStateTransitionError("source state must be pending")
        self.states[record_id] = to


def _pipeline(tmp_path, transport, store):
    client = _client(tmp_path, transport)
    return vp.RealVectorPipeline(store, client, _space(),
                                 tokenizer=qb.QwenBpeTokenizer())


@pytest.mark.parametrize("blocked_state", ["failed", "ready"])
def test_r3h4_non_pending_source_state_blocks_upsert_no_orphan(
        tmp_path, blocked_state):
    transport = FakeTransport().push(_payload([_vec()]))
    rid = "r" * 64
    store = FakeP19Store(_space(), initial_states={rid: blocked_state})
    pipe = _pipeline(tmp_path, transport, store)
    with pytest.raises(VectorStateTransitionError, match="(?i)pending"):
        pipe.ingest_record(record_id=rid, text="中国人民银行宣布降准。",
                           scope_id="default", arrival_seq=7)
    assert store.rows == {}                 # 零写入：无孤儿 chunk
    assert store.upsert_calls == []         # upsert 未发生
    assert store.get_vector_state(rid) == blocked_state  # 源态不动


def test_r3h4_pending_source_state_still_ingests(tmp_path):
    # 合法前置态（pending）不受影响：写入 + CAS ready 照常
    transport = FakeTransport().push(_payload([_vec()]))
    rid = "r" * 64
    store = FakeP19Store(_space())
    pipe = _pipeline(tmp_path, transport, store)
    report = pipe.ingest_record(record_id=rid, text="中国人民银行宣布降准。",
                                scope_id="default", arrival_seq=7)
    assert report.state == "ready"
    assert store.get_vector_state(rid) == "ready"
    assert store.rows                       # 正常写入在案


# ---------- ③ N3-05 b2_verify_gates 实算化 ----------

def _evidence(**overrides):
    evidence = {
        "leg_b_real": {"recall@10_verbatim": 1.0, "recall@30_paraphrase": 1.0},
        "corpus": {"verbatim_pairs": 77, "paraphrase_pairs": 263,
                   "hard_negative_pairs": 6},
        "missed_paraphrase": [],
        "hard_negative_candidate_ratio": 0.5,   # 3/6 候选命中登记（不判重）
        "holes": [],
        "warm_run": {"recall_equal": True, "api_calls_delta": 0},
    }
    for key, value in overrides.items():
        if key == "corpus":
            evidence["corpus"].update(value)
        else:
            evidence[key] = value
    return evidence


def test_n305_gate3_fp0_recomputed_pass_on_registered_artifact():
    verdict = bf.b2_verify_gates(_evidence())
    assert verdict["checks"]["gate3_fp0_candidate_layer"] is True
    assert verdict["pass"] is True


@pytest.mark.parametrize("mutation", [
    {"corpus": {"hard_negative_pairs": 0}},                 # 硬负例缺位=fp 未验证
    {"hard_negative_candidate_ratio": 1.5},                 # 占比越界
    {"hard_negative_candidate_ratio": -0.1},                # 占比越界（负）
    {"hard_negative_candidate_ratio": float("nan")},        # 非有限值
    {"hard_negative_candidate_ratio": 0.7},                 # 0.7×6=4.2 计数不自洽
    {"candidate_layer_duplicate_verdicts": [{"pair_id": "x"}]},  # 判重结论在场
])
def test_n305_gate3_fp0_recomputed_fail_modes(mutation):
    verdict = bf.b2_verify_gates(_evidence(**mutation))
    assert verdict["checks"]["gate3_fp0_candidate_layer"] is False
    assert verdict["pass"] is False


def test_n305_gate3_missing_hard_negative_artifact_fail_closed():
    evidence = _evidence()
    del evidence["corpus"]["hard_negative_pairs"]           # 工件缺字段=未验证
    assert bf.b2_verify_gates(evidence)["checks"][
        "gate3_fp0_candidate_layer"] is False
    evidence = _evidence()
    del evidence["hard_negative_candidate_ratio"]
    assert bf.b2_verify_gates(evidence)["checks"][
        "gate3_fp0_candidate_layer"] is False


def test_n305_gate2_recomputed_cross_check_catches_self_report_lie():
    # 自报 recall@30=1.0 但 missed 清单非空——重算 (263-1)/263≠1.0 咬出矛盾
    evidence = _evidence(missed_paraphrase=[{"pair_id": "x", "rank": None}])
    verdict = bf.b2_verify_gates(evidence)
    assert verdict["checks"]["gate2_paraphrase_recall@30"] is False
    assert verdict["pass"] is False


def test_n305_gate1_and_warm_run_renamed_summary_verification():
    verdict = bf.b2_verify_gates(_evidence())
    checks = verdict["checks"]
    # 回读自报字段者改名「汇总核验」（名实相符），旧名退役
    assert "gate1_verbatim_recall@10_summary" in checks
    assert "gate1_verbatim_recall@10" not in checks
    assert "warm_run_consistent_summary" in checks
    assert "warm_run_consistent" not in checks
    # 汇总核验仍咬合：自报字段恶化即 False
    assert bf.b2_verify_gates(_evidence())["checks"][
        "gate1_verbatim_recall@10_summary"] is True
    lying = _evidence()
    lying["leg_b_real"]["recall@10_verbatim"] = 0.98
    assert bf.b2_verify_gates(lying)["checks"][
        "gate1_verbatim_recall@10_summary"] is False
    warm_bad = _evidence(warm_run={"recall_equal": True, "api_calls_delta": 3})
    assert bf.b2_verify_gates(warm_bad)["checks"][
        "warm_run_consistent_summary"] is False


def test_n305_gate4_holes_registration_surface_wellformed():
    assert bf.b2_verify_gates(_evidence())["checks"]["gate4_holes_registered"] is True
    malformed = _evidence(holes="not-a-list")                # 登记面畸形
    assert bf.b2_verify_gates(malformed)["checks"]["gate4_holes_registered"] is False


# ---------- ④ N3-06 usage 计数器锁（并发终值精确） ----------

class _ThreadSafeTransport:
    """无限供给同一合法 payload（自带锁计数，线程安全）。"""

    def __init__(self, payload):
        self._payload = payload
        self._lock = threading.Lock()
        self.calls = 0

    def __call__(self, *, base_url, model, api_key, texts, timeout_s):
        with self._lock:
            self.calls += 1
        return self._payload


class _RaceAmplifyingDict(dict):
    """竞态放大器：__setitem__ 落值前让出 GIL（毫秒级），把 `d[k] += 1` 的
    读-改-写窗口确定性拉宽——未受锁计数器在多线程下必丢更新（红）；受锁后
    读改写对原子串行，终值仍精确（绿）。仅放大窗口，不改生产代码语义。"""

    def __setitem__(self, key, value):
        time.sleep(0.002)
        super().__setitem__(key, value)


def test_n306_write_path_counters_exact_under_concurrency(tmp_path):
    threads_n, per_thread, tokens = 8, 60, 3
    transport = _ThreadSafeTransport(_payload([_vec()], tokens=tokens))
    client = _client(tmp_path, transport)
    client._usage = _RaceAmplifyingDict(client._usage)      # 竞态窗口确定性放大
    barrier = threading.Barrier(threads_n)

    def work(tag):
        barrier.wait()
        for i in range(per_thread):
            client.embed_documents([f"线程{tag}-文本{i}"])   # 唯一文本 → 全走 API

    workers = [threading.Thread(target=work, args=(n,)) for n in range(threads_n)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    snap = client.usage_snapshot()
    assert transport.calls == threads_n * per_thread
    assert snap["api_calls"] == threads_n * per_thread        # 计数器零丢失
    assert snap["total_tokens"] == threads_n * per_thread * tokens
    assert snap["budget_used_tokens"] == threads_n * per_thread * tokens


def test_n306_query_disk_cache_hit_counter_exact_under_concurrency(tmp_path):
    threads_n, per_thread = 8, 60
    transport = _ThreadSafeTransport(_payload([_vec()], tokens=3))
    client = _client(tmp_path, transport, query_disk_cache_enabled=True)
    client.embed_query("同一查询文本")                        # 首发 API 落磁盘缓存
    assert client.query_cache_size() == 1
    client.clear_query_cache()                                # 强制走磁盘层命中
    client._usage = _RaceAmplifyingDict(client._usage)      # 竞态窗口确定性放大
    # 放大器在首发之后换入——命中计数从零起钉（首发 API 未计 cache_hits）
    barrier = threading.Barrier(threads_n)

    def work():
        barrier.wait()
        for _ in range(per_thread):
            client.clear_query_cache()       # 逐轮清 LRU，逼出磁盘命中面（竞态所在）
            client.embed_query("同一查询文本")

    workers = [threading.Thread(target=work) for _ in range(threads_n)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    snap = client.usage_snapshot()
    assert snap["cache_hits"] == threads_n * per_thread       # 命中计数零丢失
    assert snap["api_calls"] == 1                             # 仅首发 API


# ---------- ⑤ R3-M4 五通道探针实查化 + scope_id 注入 ----------

class _FakeMilvus:
    def __init__(self, present):
        self._present = present

    def has_collection(self, *, collection_name):
        return self._present


class _SearcherWithStore:
    """真 searcher 鸭式（暴露 store.milvus+collection_name 供集合在场实查）。"""

    def __init__(self, present):
        self._store = SimpleNamespace(
            milvus=_FakeMilvus(present), collection_name="p19_winr3a_s_" + "0" * 40)


class _RecordingWatermarkProvider:
    def __init__(self):
        self.visible_scopes = []
        self.prepared_scopes = []

    def visible_seq(self, scope_id, business_date):
        self.visible_scopes.append((scope_id, business_date))
        return 0

    def prepared_seq(self, scope_id, business_date):
        self.prepared_scopes.append((scope_id, business_date))
        return 0


def _probes(client, *, mode="shadow", scope_id="default", **overrides):
    kwargs = dict(es_client=client, index_prefix=PREFIX, mode=mode,
                  clock=lambda: T0, business_date=DAY, scope_id=scope_id)
    kwargs.update(overrides)
    return default_probes(**kwargs)


def test_r3m4_five_channel_probes_present_and_codes_in_vocabulary():
    # 五通道齐备（readiness 词表统一；recall_chain_unavailable:embedding 死码消除）
    probes = _probes(FakeESClient(), vector_searcher=_SearcherWithStore(False))
    for channel in ("hash", "near", "bm25", "embedding", "entity"):
        assert f"recall_chain_{channel}" in probes, f"缺 {channel} 通道探针"
    ready, codes = ReadinessPort(probes, mode="shadow").issues()
    assert ready is False
    # 日索引缺失 → hash/near/bm25/entity 报码；集合缺席 → embedding 报码；词表全收
    for channel in ("hash", "near", "bm25", "embedding", "entity"):
        assert f"recall_chain_unavailable:{channel}" in codes


def test_r3m4_embedding_probe_checks_milvus_collection_presence():
    probes_absent = _probes(FakeESClient(), vector_searcher=_SearcherWithStore(False))
    assert probes_absent["recall_chain_embedding"]() == (
        "recall_chain_unavailable:embedding")
    probes_present = _probes(FakeESClient(), vector_searcher=_SearcherWithStore(True))
    assert probes_present["recall_chain_embedding"]() is None


def test_r3m4_embedding_probe_defers_when_searcher_not_wired():
    # Null/鸭式无仓 searcher：链缺口归 vector_gap 探针（两码不混，不重复报码）
    probes = _probes(FakeESClient(), vector_searcher=NullVectorSearcher())
    assert probes["recall_chain_embedding"]() is None
    assert probes["vector_gap"]() == "embedding_space_unconfirmed"


def test_r3m4_entity_probe_checks_prepare_artifact():
    client = FakeESClient()
    physical = PREFIX + day_index(DAY)
    client.indices.create(index=physical)                   # 日索引在场
    probes = _probes(client)
    # 准备产物缺席（无日控制 checkpoint）→ entity 报码；hash/near/bm25 绿
    assert probes["recall_chain_entity"]() == "recall_chain_unavailable:entity"
    assert probes["recall_chain_hash"]() is None
    assert probes["recall_chain_near"]() is None
    assert probes["recall_chain_bm25"]() is None
    # 准备产物在场（checkpoint.recall_prepared 快照）→ entity 转绿
    day_key = ElasticsearchWatermarkProvider.day_key("default", DAY)
    client.put(PREFIX + CONTROL_INDEX, day_key, {
        "kind": "day", "scope_id": "default", "business_date": DAY,
        "checkpoint": {"recall_prepared": {"prepared_seq": 3,
                                           "prepared_at": "2026-09-29T01:00:00Z",
                                           "scope_id": "default",
                                           "business_date": DAY,
                                           "evidence": "per_doc_ready"}}})
    assert probes["recall_chain_entity"]() is None


def test_r3m4_entity_probe_scope_follows_injected_scope_id():
    client = FakeESClient()
    physical = PREFIX + day_index(DAY)
    client.indices.create(index=physical)
    day_key_other = ElasticsearchWatermarkProvider.day_key("scope-x", DAY)
    client.put(PREFIX + CONTROL_INDEX, day_key_other, {
        "kind": "day", "scope_id": "scope-x", "business_date": DAY,
        "checkpoint": {"recall_prepared": {"prepared_seq": 1}}})
    probes = _probes(client, scope_id="scope-x")
    assert probes["recall_chain_entity"]() is None          # 按注入 scope 查实


def test_r3m4_watermark_probe_uses_injected_scope_id_not_default():
    provider = _RecordingWatermarkProvider()
    probes = _probes(FakeESClient(), watermark_provider=provider, scope_id="scope-x")
    assert probes["watermark"]() is None
    assert provider.visible_scopes == [("scope-x", DAY)]    # 不再硬编码 "default"
    assert provider.prepared_scopes == [("scope-x", DAY)]


def test_r3m4_create_host_passes_scope_id_into_default_probes():
    from news_flash_dedup.api.host import create_host

    client = FakeESClient()
    assembly = create_host(
        es_client=client, index_prefix=PREFIX, scope_id="scope-x",
        status_authenticated=lambda request: True,
        env={"DEDUP_RECALL_MODE": "shadow"}, clock=lambda: T0,
        artifact_sink=SimpleNamespace(emit=lambda kind, payload: None),
        collector_alive=True, delivery_route_configured=True)
    # 组装根注入面：默认探针组以 scope-x 为水位/准备产物核验口径——
    # scope-x 日控制产物缺席 → entity 报码（而非误查 default 域）
    ready, codes = assembly.readiness_port.issues()
    assert ready is False
    assert "recall_chain_unavailable:entity" in codes
