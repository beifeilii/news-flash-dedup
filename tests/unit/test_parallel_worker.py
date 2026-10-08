"""并行模式 worker 适配层端到端假件钉（recall/parallel_worker.py）：

喂池（闸门）→ 保序定稿（重构+定稿核）→ build_commit_write_plan（串行
同源）→ commit_batch 四段——全链 fake（真 DecideOutcome/PairResult 类型、
真 RealESP18Store+FakeESClient、假召回/扫描/水位）：

- 全绿轮：两件提交、水位推进、主记录 pending（不重复放行）；
- 闸门：prepared 不足即停喂（awaiting），余件留下轮；
- 收容：池内异常件 → failed 即停、零落锤（现役单飞边界同款）。
"""

from __future__ import annotations

from datetime import date

import pytest

from news_flash_dedup.compare.pair_compare import PairResult
from news_flash_dedup.decide.types import DecideOutcome
from news_flash_dedup.es_client import APPROVED_UAT_HOST
from news_flash_dedup.persist.es_store import RealESP18Config, RealESP18Store
from news_flash_dedup.recall.fusion import FusedCandidate, RecallPlan
from news_flash_dedup.recall.models import RecallCandidate, RecallRequest
from news_flash_dedup.recall.parallel_worker import ParallelLiveDriver

from b4_fake_es import FakeESClient

DAY_STR = "2026-10-08"
SCOPE = "default"


def _cand(rid: str, seq: int) -> RecallCandidate:
    return RecallCandidate(
        record_id=rid, item_id=f"it-{rid}", arrival_seq=seq,
        text=f"text-{rid}", subpaths=(), score=1.0, query_version="v1")


def _plan_for(ids) -> RecallPlan:
    return RecallPlan(
        version="v1", merged_top30=(), pair_top10=(), hash_protected=(),
        incremental_required=(),
        required=tuple(
            FusedCandidate(candidate=_cand(rid, seq), channel_hits=(),
                           rrf_score=0, hash_protected=False,
                           selected_for_pair_check=True,
                           incremental_required=False)
            for rid, seq in ids),
        recall_gaps=(), planning_excluded=0, channel_audits=())


def _pair(history_rid: str, current_rid: str, hseq: int, cseq: int) -> PairResult:
    return PairResult(
        pair_id=f"p-{current_rid}-{history_rid}",
        history_record_id=history_rid, current_record_id=current_rid,
        history_item_id=f"it-{history_rid}", current_item_id=f"it-{current_rid}",
        history_arrival_seq=hseq, current_arrival_seq=cseq,
        history_raw_hash=f"raw-{history_rid}", current_raw_hash=f"raw-{current_rid}",
        pipeline_version="dedup_v1", outcome="aligned",
        code="NO_CONFLICT", detail="", aligned_facts=(),
        verified_conflicts=(), unresolved_fields=(), used_evidence=())


def _decide(current: Mapping, pairs=()) -> DecideOutcome:
    return DecideOutcome(
        item_id=current["item_id"], text=current["text"], decision="不重复",
        duplicate_ids=(), reason="无冲突。",
        internal_code="NO_DUPLICATE_FOUND", pair_codes={},
        used_evidence=(), unresolved_fields=(),
        raw_hash=current["raw_hash"], pipeline_version="dedup_v1",
        pair_results=tuple(pairs))


class _Watermark:
    def __init__(self, prepared: int):
        self._prepared = prepared

    def prepared_seq(self, scope_id, business_date):
        return self._prepared

    def visible_seq(self, scope_id, business_date):
        return self._prepared


class _Scanner:
    def __init__(self, docs):
        self._docs = docs

    def scan(self, scope_id, business_date, *, after_seq):
        return [d for d in self._docs if d["arrival_seq"] > after_seq]


class _Prepare:
    def prepare(self, scope_id, business_date):
        return None


def _doc(rid: str, seq: int) -> dict:
    return {"record_id": rid, "item_id": f"it-{rid}", "arrival_seq": seq,
            "text": f"text-{rid}", "raw_hash": f"raw-{rid}"}


def _current(doc: Mapping) -> Mapping:
    return {"record_id": doc["record_id"], "item_id": doc["item_id"],
            "text": doc["text"], "raw_hash": doc["raw_hash"],
            "arrival_seq": doc["arrival_seq"], "facts": []}


class _RecallStub:
    """召回假件：预检/重构同一计划（差集恒空=直用道）。"""

    def build_plan(self, request, **kw):
        return _plan_for((("hist-1", 1),))

    def build_commit_inputs(self, plan, doc):
        current = _current(doc)
        candidates = (_current(_doc("hist-1", 1)),)
        return current, candidates, True, None


def _make_driver(store, *, docs, prepared=99, decide_fn=None):
    def decide_for_task(**kw):
        current = kw["current"]
        history = kw["history"]
        pairs = () if history["record_id"] == current["record_id"] else (
            _pair(history["record_id"], current["record_id"],
                  history["arrival_seq"], current["arrival_seq"]),)
        return _decide(current, pairs)

    return ParallelLiveDriver(
        prepare_worker=_Prepare(),
        scanner=_Scanner(docs),
        recall_service=_RecallStub(),
        commit_store=store,
        watermark_provider=_Watermark(prepared),
        decide_for_task=decide_fn or decide_for_task,
        aggregate=lambda current, frozen, pairs, *, coverage_complete:
            _decide(current, pairs),
        decide_pair=lambda h, c: _pair(
            h["record_id"], c["record_id"], h["arrival_seq"], c["arrival_seq"]),
        candidate_lookup=lambda rid: _current(_doc(rid, 1)),
        frozen_plan_from=lambda plan: plan,
        request_factory=RecallRequest,
        version_chain=("f1", "d1", "n1", "p1"),
        workers=2, batch_size=16)


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("P18_CONFIRM_UAT", "1")
    monkeypatch.setenv("DEPLOY_ENV", "test")
    monkeypatch.setenv("TEST_ES_HOST", APPROVED_UAT_HOST)
    for key in ("PROD_ES_HOST", "PROD_ES_PORT", "PROD_ES_USER", "PROD_ES_PASS",
                "PROD_ES_SCHEME", "ALLOW_PROD_WRITE"):
        monkeypatch.delenv(key, raising=False)
    client = FakeESClient()
    config = RealESP18Config(run_uuid="pw1008", business_date=date(2026, 10, 8))
    return client, RealESP18Store(client, config), config


def test_round_commits_two_and_advances_watermark(env):
    client, store, config = env
    docs = [_doc("n1", 1), _doc("n2", 2)]
    report = _make_driver(store, docs=docs).drive_round(SCOPE, DAY_STR)
    assert set(report.committed) == {"n1", "n2"}
    assert report.failed == () and report.awaiting == ()
    assert report.watermark_advanced_to == 2
    assert store.get_watermark(SCOPE, DAY_STR) == 2
    for rid in ("n1", "n2"):
        doc = store.get_main_record(rid)["source"]
        assert doc["delivery_state"] == "pending"   # 不重复放行
        assert doc["task_state"] == "succeeded"


def test_gate_stops_feeding_when_frontier_short(env):
    client, store, config = env
    docs = [_doc("n1", 1), _doc("n5", 5)]
    # prepared=3：n1 过闸（3≥0）、n5 卡闸（3<4）——喂一件停喂
    report = _make_driver(store, docs=docs, prepared=3).drive_round(
        SCOPE, DAY_STR)
    assert report.committed == ("n1",)
    assert report.awaiting == ("n5",)
    assert store.get_watermark(SCOPE, DAY_STR) == 1


def test_pool_error_contains_failed_and_zero_writes(env):
    client, store, config = env

    def boom(**kw):
        raise RuntimeError("预检爆炸样例")

    report = _make_driver(store, docs=[_doc("n1", 1)],
                          decide_fn=boom).drive_round(SCOPE, DAY_STR)
    assert report.failed == ("n1",)
    assert report.committed == ()
    assert store.get_watermark(SCOPE, DAY_STR) is None   # 零推进
    assert store.get_main_record("n1") is None           # 零落锤


# ---------- worker 模式分支（recall/worker.py parallel_live 注册钉） ----------

def test_worker_parallel_live_requires_driver():
    from news_flash_dedup.recall.worker import DedupWorker

    with pytest.raises(ValueError):
        DedupWorker(mode="parallel_live", recall_service=object(),
                    prepare_worker=object(), watermark_provider=object(),
                    scanner=object())     # 缺 parallel_driver → fail-closed


def test_worker_parallel_live_delegates_and_maps_buckets():
    from news_flash_dedup.recall.parallel_worker import ParallelRoundReport
    from news_flash_dedup.recall.worker import DedupWorker

    class _DriverStub:
        def drive_round(self, scope_id, business_date):
            return ParallelRoundReport(
                committed=("a",), failed=("b",), awaiting=("c",),
                deferred=("d",), watermark_advanced_to=3, recomputed=("a",))

    worker = DedupWorker(mode="parallel_live", recall_service=object(),
                         prepare_worker=object(), watermark_provider=object(),
                         scanner=object(), parallel_driver=_DriverStub())
    report = worker.drive_once(SCOPE, DAY_STR)
    assert report.mode == "parallel_live"
    assert report.committed == ("a",) and report.failed == ("b",)
    assert report.awaiting == ("c", "d")   # deferred 并入 awaiting（留下轮）
