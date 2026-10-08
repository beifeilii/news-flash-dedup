"""B5/E2 S6 红测（设计 §5.2 B-0..B-3 程序本体）：vector/backfill.py。

红测清单映射（真轨执行归 test_emb_shadow.py，本件钉程序逻辑单元层）：
- B-0：schema 对拍报告字段完备 + 异构/缺件即 pass=False；
- B-1：逐记录 ready 台账 + 失败诚实入册（不跳过不掩盖）+ chunk 覆盖率 +
  抽样向量有限值核对；
- B-2：shadow 证据四闸独立复算（真/假证据两态）；
- B-3：dry-run 先行 + 非本前缀即拒（assert）+ 快照 diff 零漂移判定。
"""

from __future__ import annotations

import pytest

from news_flash_dedup.recall.vector_space import EmbeddingSpace
from news_flash_dedup.vector import backfill as bf
from news_flash_dedup.vector.milvus_store import RealMilvusP19Config


DAY_STR = "2026-09-29"


def _space():
    return EmbeddingSpace("mock_embedding", "fixture_v1", "codepoint_v1", "none",
                          "COSINE", 4, "plain_v1", "plain_v1",
                          "tokens512_overlap64_bpe_v1", "p09_v1")


def _config():
    import datetime as _dt

    return RealMilvusP19Config(run_uuid="b5bf1234",
                               business_date=_dt.date(2026, 9, 29),
                               space=_space())


class FakeMilvus:
    def __init__(self):
        self.collections = {}

    def describe_collection(self, *, collection_name):
        return {"fields": [
            {"name": "vector_id"}, {"name": "record_id"}, {"name": "scope_id"},
            {"name": "business_date"}, {"name": "arrival_seq"},
            {"name": "embedding_space_id"}, {"name": "chunk_id"},
            {"name": "embedding", "params": {"dim": 4}},
        ]}

    def list_partitions(self, *, collection_name):
        return ["bd_20260929", "_default"]

    def list_collections(self):
        return ["deerflow_rag_knowledge_base", "rag_knowledge_base"]


class FakeESIndices:
    def __init__(self, config):
        self._names = {config.items_index, config.control_index}

    def exists(self, *, index):
        return index in self._names


class FakeES:
    def __init__(self, config):
        self.indices = FakeESIndices(config)

    class _Cat:
        def indices(self, *, format, h):
            return [{"index": "news_dedup_replay_keep1-news-dedup-items-v1-2026.09.01"}]

    cat = _Cat()


class FakeStore:
    def __init__(self, config):
        self.config = config
        self.cleaned = False

    def cleanup_plan(self):
        return {"collections": [self.config.collection_name],
                "indices": [self.config.items_index, self.config.control_index]}

    def cleanup(self):
        self.cleaned = True
        return self.cleanup_plan()


# ---------- B-0 ----------

def test_b0_schema_report_pass_and_fail_modes():
    config = _config()
    out = bf.b0_establish(FakeMilvus(), FakeES(config), config,
                          store_factory=lambda m, e, c: FakeStore(c))
    schema = out["schema"]
    assert schema["pass"] is True
    assert schema["fields_exact"] and schema["dimension"] == 4
    assert schema["dimension_matches_space"] and schema["endswith_space_id"]
    assert schema["partition_present"] and schema["items_index_present"]

    class BadMilvus(FakeMilvus):
        def describe_collection(self, *, collection_name):
            return {"fields": [{"name": "vector_id"}]}  # 字段缺件

    out2 = bf.b0_establish(BadMilvus(), FakeES(config), config,
                           store_factory=lambda m, e, c: FakeStore(c))
    assert out2["schema"]["pass"] is False
    assert out2["schema"]["fields_exact"] is False


# ---------- B-1 ----------

class FakePipelineSpace:
    pass


class FakePipeline:
    def __init__(self, space, fail_on=()):
        self._space = space
        self._fail_on = set(fail_on)
        self.calls = []

    def ingest_record(self, *, record_id, text, scope_id, arrival_seq):
        from news_flash_dedup.vector.pipeline import IngestReport

        self.calls.append(record_id)
        if record_id in self._fail_on:
            raise RuntimeError("injected embed outage")
        return IngestReport(
            record_id=record_id, chunk_ids=(0,),
            outcomes=((f"{record_id}_0", "created"),), state="ready",
            truncated_chunk_ids=(), tokens_used=9, cache_hits=0,
            chunking_version="tokens512_overlap64_bpe_v1")


class FakeVectorStoreRows:
    def __init__(self):
        self.rows = {}

    def get_vector_row(self, vector_id):
        return self.rows.get(vector_id)


def test_b1_backfill_ledger_and_honest_failure():
    space = _space()
    pipe = FakePipeline(space, fail_on={"r2"})
    store = FakeVectorStoreRows()
    written = []
    for rid in ("r1", "r3"):
        store.rows[f"{rid}_0"] = {"embedding": [1.0, 0.5, 0.25, 0.125]}
    records = [{"record_id": rid, "text": f"正文{rid}", "scope_id": "gold",
                "arrival_seq": index + 1} for index, rid in enumerate(("r1", "r2", "r3"))]
    out = bf.b1_backfill(pipe, store, records,
                         write_main_record=lambda record: written.append(
                             record["record_id"]))
    assert out["records_total"] == 3
    assert out["records_ready"] == 2
    assert out["records_failed"] == 1
    assert out["failed"][0]["record_id"] == "r2"          # 诚实入册不跳过
    assert "injected embed outage" in out["failed"][0]["error"]
    assert written == ["r1", "r2", "r3"]                   # 主记录均先落（创建者职责）
    assert out["chunks_written"] == 2
    assert out["sample_finite_all_pass"] is True


# ---------- B-2 ----------

def _evidence(*, recall30=1.0, recall10=1.0, paraphrase=263, verbatim=77,
              missed=(), warm_equal=True, warm_api=0):
    # 真 harness 证据形态（W-R3a 呈裁钉：test_emb_shadow.py:605-626 实发字段——
    # corpus.hard_negative_pairs + hard_negative_candidate_ratio；N3-05 起
    # gate3 从该工件重算 fp 口径，缺工件 fail-closed）
    return {
        "leg_b_real": {"recall@30_paraphrase": recall30,
                       "recall@10_verbatim": recall10,
                       "recall@1_paraphrase": 0.9962,
                       "rank_p50": 1, "rank_p90": 1, "rank_max": 2},
        "corpus": {"verbatim_pairs": verbatim, "paraphrase_pairs": paraphrase,
                   "hard_negative_pairs": 6},
        "missed_paraphrase": list(missed),
        "hard_negative_candidate_ratio": 0.5,
        "holes": [],
        "warm_run": {"recall_equal": warm_equal, "api_calls_delta": warm_api},
    }


def test_b2_verify_gates_pass_on_anchor_evidence():
    verdict = bf.b2_verify_gates(_evidence())
    assert verdict["pass"] is True
    assert all(verdict["checks"].values())


def test_b2_verify_gates_fail_modes():
    assert bf.b2_verify_gates(_evidence(recall30=0.999))["pass"] is False
    assert bf.b2_verify_gates(_evidence(paraphrase=262))["pass"] is False
    assert bf.b2_verify_gates(_evidence(missed=[{"pair_id": "x", "rank": None}]))["pass"] is False
    assert bf.b2_verify_gates(_evidence(warm_api=3))["pass"] is False
    assert bf.b2_verify_gates(_evidence(recall10=0.98))["pass"] is False


# ---------- B-3（R191 冻结版：只读签收，零删除） ----------

class SealedMilvus(FakeMilvus):
    """跑后名单 = 基线（含封存物）+ 本 run 新对象（原地封存）。"""

    def __init__(self, config, extra=()):
        super().__init__()
        self._names = (["deerflow_rag_knowledge_base", "rag_knowledge_base",
                        config.collection_name] + list(extra))

    def list_collections(self):
        return list(self._names)


class SealedES(FakeES):
    def __init__(self, config, extra=()):
        super().__init__(config)
        self._extra = list(extra)
        self._config = config

    class _Cat:
        def __init__(self, outer):
            self._outer = outer

        def indices(self, *, format, h):
            names = (["news_dedup_replay_keep1-news-dedup-items-v1-2026.09.01",
                      self._outer._config.items_index,
                      self._outer._config.control_index]
                     + self._outer._extra)
            return [{"index": name} for name in names]

    @property
    def cat(self):
        return SealedES._Cat(self)


def test_b3_signoff_inventory_sealed_and_drift_free():
    config = _config()
    store = FakeStore(config)
    out = bf.b3_signoff_inventory(
        store, SealedMilvus(config), SealedES(config),
        baseline_collections=["deerflow_rag_knowledge_base", "rag_knowledge_base"],
        baseline_indices=["news_dedup_replay_keep1-news-dedup-items-v1-2026.09.01"])
    assert out["drift_free"] is True                       # 新 run 对象之外零变化
    assert out["own_present"] is True
    assert out["sealed"]["collections"] == [config.collection_name]
    assert sorted(out["sealed"]["indices"]) == sorted(
        [config.items_index, config.control_index])        # 本 run 对象原地封存
    assert not any(out["diff"].values())


def test_b3_signoff_inventory_detects_sealed_tamper_and_foreign_add():
    config = _config()
    store = FakeStore(config)
    # 封存物被删（基线有、跑后无）→ collections_missing 非空 → drift
    out = bf.b3_signoff_inventory(
        store, SealedMilvus(config, extra=[]), SealedES(config),
        baseline_collections=["deerflow_rag_knowledge_base", "rag_knowledge_base",
                              "p19_emb999999_s_" + "0" * 40],
        baseline_indices=["news_dedup_replay_keep1-news-dedup-items-v1-2026.09.01"])
    assert out["drift_free"] is False
    assert out["diff"]["collections_missing"] == ["p19_emb999999_s_" + "0" * 40]
    # 白名单之外新增 → collections_added_foreign 非空 → drift
    out2 = bf.b3_signoff_inventory(
        store, SealedMilvus(config, extra=["p19_evil99_s_" + "1" * 40]),
        SealedES(config),
        baseline_collections=["deerflow_rag_knowledge_base", "rag_knowledge_base"],
        baseline_indices=["news_dedup_replay_keep1-news-dedup-items-v1-2026.09.01"])
    assert out2["drift_free"] is False
    assert out2["diff"]["collections_added_foreign"] == [
        "p19_evil99_s_" + "1" * 40]
