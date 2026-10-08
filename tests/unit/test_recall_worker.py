"""B4/E1 红测（D1 §五 T1）：F0 组合根 recall/worker.py。

红测清单映射（log\设计-真链路接线包.md §五-T1）：
- R-9  worker 组合根：fixed 态不启动判重循环；有序性（同 (scope,day) 升序单飞）；
       STALE→重读水位→重驱准备→重建计划→有界重试状态机；终态未确认不推进不投递
- R-10 提交仓储协议：build_commit_one_store 对缺三件套同名方法的仓储 TypeError
       （persist/es_store.py:556-558 自检先行）；RealESP18Store 过自检
- R-11 影子决策不生效闸（R148）：shadow 态全程 fake 仓储调用计数=0
       （get_watermark/write_main_record/advance_watermark 无一被调）、零 callback/零投递意图；
       影子决策工件五字段+签发码全形，且与生效腿同输入同语义源
       （coordinator.py:144-160/:161-198 同型对拍——同 current 同候选集两路决策全等）
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone

import pytest

from news_flash_dedup.commit.coordinator import CommitContext, commit_one
from news_flash_dedup.commit.fake_store import FakeCommitStore
from news_flash_dedup.recall.models import ChannelResult, RecallCandidate, RecallRequest
from news_flash_dedup.recall.service import NullVectorSearcher, RecallService
from news_flash_dedup.recall.worker import DedupWorker, DriveReport

from b4_fake_es import FakeESClient

DAY = "2026-09-29"
PREFIX = "p01-batch-b4wrk-"
SCOPE = "default"
NOW = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)
EXPIRY = "2026-10-06T00:00:00.000000Z"


def _rid(seed: str) -> str:
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def _clock() -> datetime:
    return NOW


def _doc(seq: int, text: str) -> dict:
    return {
        "scope_id": SCOPE, "request_id": f"9-{seq}", "item_id": f"9-{seq}",
        "record_id": _rid(f"rec-{seq}"), "business_date": DAY, "arrival_seq": seq,
        "text": text, "raw_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "embedding_space_id": "space-x", "expires_at": EXPIRY,
        "task_state": "accepted", "delivery_state": "not_ready", "result": None,
    }


class _FakeChannel:
    def __init__(self, fn) -> None:
        self.fn = fn
        self.calls = []

    def search(self, request, query_vector=None):
        self.calls.append(request)
        return self.fn(request)


def _complete(channel: str, qv: str, request, *, candidates: tuple = ()) -> ChannelResult:
    return ChannelResult(
        channel, "complete", candidates, qv,
        visible_seq=request.visible_seq, prepared_seq=request.prepared_seq,
        coverage_complete=True)


def _service(plan_map: dict[int, tuple]) -> RecallService:
    """plan_map: arrival_seq → 该 current 应见的候选元组（hash 路注入）。"""
    def hash_fn(request):
        return _complete("hash", "hash_v1", request,
                         candidates=plan_map.get(request.arrival_seq, ()))
    channels = {
        "hash": _FakeChannel(hash_fn),
        "near": _FakeChannel(lambda r: _complete("near", "near_v1", r)),
        "bm25": _FakeChannel(lambda r: _complete("bm25", "bm25_v1", r)),
        "entity": _FakeChannel(lambda r: _complete("entity", "entity_v1", r)),
    }
    vector = _FakeChannel(lambda r: _complete("embedding", "embedding_v1", r))
    return RecallService(None, PREFIX, channels=channels, vector_searcher=vector)


class _FakePrepare:
    def __init__(self, hook=None) -> None:
        self.calls: list[tuple[str, str]] = []
        self._hook = hook

    def prepare(self, scope_id: str, business_date: str):
        self.calls.append((scope_id, business_date))
        if self._hook is not None:
            self._hook(len(self.calls))
        return None


class _MutableWatermark:
    def __init__(self, visible: int, prepared: int) -> None:
        self.visible = visible
        self.prepared = prepared
        self.reads = 0

    def visible_seq(self, scope_id, business_date) -> int:
        self.reads += 1
        return self.visible

    def prepared_seq(self, scope_id, business_date) -> int:
        self.reads += 1
        return self.prepared


class _FakeScanner:
    def __init__(self, docs: list[dict]) -> None:
        self.docs = docs
        self.calls: list[int] = []

    def scan(self, scope_id, business_date, *, after_seq: int = 0) -> list[dict]:
        self.calls.append(after_seq)
        return [doc for doc in self.docs if doc["arrival_seq"] > after_seq]


class _ListSink:
    def __init__(self) -> None:
        self.emitted: list[tuple[str, dict]] = []

    def emit(self, kind: str, payload: dict) -> None:
        self.emitted.append((kind, payload))


class _CountingStore(FakeCommitStore):
    def __init__(self) -> None:
        super().__init__()
        self.protocol_calls = {"get_watermark": 0, "write_main_record": 0,
                               "advance_watermark": 0}

    def get_watermark(self, scope_id, business_date):
        self.protocol_calls["get_watermark"] += 1
        return super().get_watermark(scope_id, business_date)

    def write_main_record(self, record, **ctx):
        self.protocol_calls["write_main_record"] += 1
        return super().write_main_record(record, **ctx)

    def advance_watermark(self, scope_id, business_date, arrival_seq):
        self.protocol_calls["advance_watermark"] += 1
        return super().advance_watermark(scope_id, business_date, arrival_seq)


def _worker(mode: str, *, docs: list[dict], plan_map=None, store=None,
            sink=None, watermark=None, prepare=None, service=None,
            max_stale_retries: int = 3) -> tuple[DedupWorker, dict]:
    parts = {
        "service": service or _service(plan_map or {}),
        "prepare": prepare or _FakePrepare(),
        "watermark": watermark or _MutableWatermark(visible=99, prepared=99),
        "scanner": _FakeScanner(docs),
    }
    worker = DedupWorker(
        mode=mode,
        recall_service=parts["service"],
        prepare_worker=parts["prepare"],
        watermark_provider=parts["watermark"],
        scanner=parts["scanner"],
        commit_store=store,
        artifact_sink=sink,
        clock=_clock,
        max_stale_retries=max_stale_retries,
    )
    return worker, parts


# ---------- R-9 worker 组合根 ----------

def test_r9_fixed_mode_never_starts_dedup_loop():
    store = _CountingStore()
    worker, parts = _worker("fixed", docs=[_doc(1, "正文一。")], store=store)
    report = worker.drive_once(SCOPE, DAY)
    assert isinstance(report, DriveReport)
    assert report.mode == "fixed"
    assert report.committed == report.stale == report.awaiting == ()
    assert report.shadowed == report.failed == ()
    # 不启动判重循环：扫描/准备/召回/仓储全零调用
    assert parts["scanner"].calls == []
    assert parts["prepare"].calls == []
    assert store.protocol_calls == {"get_watermark": 0, "write_main_record": 0,
                                    "advance_watermark": 0}
    assert all(not channel.calls
               for channel in parts["service"]._channels.values())


def test_r9_live_commits_in_arrival_order_single_flight():
    text1, text2 = "美国能源信息署公布原油库存增加。", "美国能源信息署公布原油库存增加。"
    doc1, doc2 = _doc(1, text1), _doc(2, text2)
    candidate = RecallCandidate(
        record_id=doc1["record_id"], item_id=doc1["item_id"], arrival_seq=1,
        text=text1, subpaths=("raw",), score=1.0, query_version="hash_v1")
    store = _CountingStore()
    worker, _ = _worker("live", docs=[doc2, doc1],  # 扫描乱序输入
                        plan_map={2: (candidate,)}, store=store)
    report = worker.drive_once(SCOPE, DAY)
    assert report.committed == (doc1["record_id"], doc2["record_id"])  # 升序单飞
    assert report.stale == report.failed == ()
    # 仓储写入序=升序；水位推进至 2
    assert [call["record_id"] for call in store.cas_calls
            if call["action"] == "write_main_record"] == [
                doc1["record_id"], doc2["record_id"]]
    assert store.get_watermark(SCOPE, DAY) == 2
    # 有序提交：扫描游标初始 0（水位读数）；恒等投影事实占位下判疑难
    # （test_p23_uat.py:352：该投影不产生任何"重复"输出——decide 现役语义）
    assert store.main_records[doc2["record_id"]].decision == "边界case/疑难case"
    assert store.main_records[doc2["record_id"]].internal_code == "FACT_INCOMPLETE"


def test_r9_stale_triggers_rewatermark_reprepare_replan_bounded_retry():
    doc = _doc(2, "正文二。")
    watermark = _MutableWatermark(visible=0, prepared=99)  # lexical 过期（< seq-1）

    def flip_on_reprepare(call_no: int) -> None:
        if call_no == 2:                                  # 重驱准备后水位恢复
            watermark.visible = 99

    prepare = _FakePrepare(hook=flip_on_reprepare)
    service = _service({})
    store = _CountingStore()
    worker, _ = _worker("live", docs=[doc], store=store, watermark=watermark,
                        prepare=prepare, service=service)
    report = worker.drive_once(SCOPE, DAY)
    assert report.committed == (doc["record_id"],)       # STALE→有界重试→成
    assert report.stale == ()
    assert len(prepare.calls) == 2                       # 初驱+STALE 后重驱准备前沿
    # 重建计划：两次完整五路搜索（四通道×2，向量端口另计）
    assert sum(len(ch.calls) for ch in service._channels.values()) == 8
    assert store.protocol_calls["write_main_record"] == 1
    assert store.protocol_calls["advance_watermark"] == 1


def test_r9_stale_exhaustion_does_not_advance_and_does_not_commit():
    doc = _doc(2, "正文二。")
    watermark = _MutableWatermark(visible=0, prepared=99)  # lexical 永不过期恢复
    store = _CountingStore()
    worker, parts = _worker("live", docs=[doc], store=store, watermark=watermark,
                            max_stale_retries=2)
    report = worker.drive_once(SCOPE, DAY)
    assert report.committed == ()
    assert report.stale == (doc["record_id"],)
    assert store.protocol_calls["write_main_record"] == 0   # 未提交
    assert store.protocol_calls["advance_watermark"] == 0   # 不推进
    # 下一轮驱动仍从同一条起（游标未推进）
    second = worker.drive_once(SCOPE, DAY)
    assert second.stale == (doc["record_id"],)
    assert parts["scanner"].calls == [0, 0]


def test_r9_terminal_unconfirmed_blocks_advance_and_delivery():
    from news_flash_dedup.commit.fake_store import FakeMainRecord

    doc = _doc(1, "正文一。")
    store = _CountingStore()
    # 已存分歧主记录：create 冲突→重试幂等对拍分歧→CommitOneError（终态未确认）
    store.main_records[doc["record_id"]] = FakeMainRecord(
        record_id=doc["record_id"], item_id=doc["item_id"], text="篡改正文。",
        decision="不重复", duplicate_ids=(), reason="分歧。", payload_hash="p",
        delivery_state="pending", delivery_deadline_at="d", callback_attempts=0,
        audit_ids=(), audit_complete=True, raw_hash="0" * 64,
        pipeline_version="dedup_v1", completed_at="c", result_version=1,
        event_id="e", internal_code="", expires_at="")
    worker, _ = _worker("live", docs=[doc], store=store)
    report = worker.drive_once(SCOPE, DAY)
    assert report.failed == (doc["record_id"],)             # 终态未确认
    assert report.committed == ()
    assert store.protocol_calls["advance_watermark"] == 0   # 禁推进禁投递
    # 游标未推进：下一轮驱动仍撞上同一终态未确认（不静默越过）
    second = worker.drive_once(SCOPE, DAY)
    assert second.failed == (doc["record_id"],)
    assert store.get_watermark(SCOPE, DAY) is None


def test_r9_awaiting_when_prepared_frontier_below_floor():
    doc1, doc2 = _doc(2, "正文二。"), _doc(3, "正文三。")
    watermark = _MutableWatermark(visible=99, prepared=0)   # 前沿 < seq-1
    store = _CountingStore()
    worker, _ = _worker("live", docs=[doc1, doc2], store=store, watermark=watermark)
    report = worker.drive_once(SCOPE, DAY)
    assert report.awaiting == (doc1["record_id"],)          # 首条即阻（升序不越过）
    assert report.committed == ()
    assert store.protocol_calls["write_main_record"] == 0


def test_r9_mode_and_dependency_validation():
    service = _service({})
    with pytest.raises(ValueError):
        DedupWorker(mode="bogus", recall_service=service,
                    prepare_worker=_FakePrepare(),
                    watermark_provider=_MutableWatermark(0, 0),
                    scanner=_FakeScanner([]))
    with pytest.raises(ValueError):  # live 缺仓储
        DedupWorker(mode="live", recall_service=service,
                    prepare_worker=_FakePrepare(),
                    watermark_provider=_MutableWatermark(0, 0),
                    scanner=_FakeScanner([]))
    with pytest.raises(ValueError):  # shadow 缺工件落点
        DedupWorker(mode="shadow", recall_service=service,
                    prepare_worker=_FakePrepare(),
                    watermark_provider=_MutableWatermark(0, 0),
                    scanner=_FakeScanner([]),
                    commit_store=_CountingStore())
    with pytest.raises(TypeError):  # 仓储协议自检先行
        DedupWorker(mode="live", recall_service=service,
                    prepare_worker=_FakePrepare(),
                    watermark_provider=_MutableWatermark(0, 0),
                    scanner=_FakeScanner([]),
                    commit_store=object())


# ---------- R-10 提交仓储协议 ----------

def test_r10_build_commit_one_store_protocol_gate():
    from news_flash_dedup.persist.es_store import build_commit_one_store

    class Incomplete:
        pass

    class Partial:
        def get_watermark(self, scope_id, business_date):
            return None

    with pytest.raises(TypeError):
        build_commit_one_store(Incomplete())
    with pytest.raises(TypeError):
        build_commit_one_store(Partial())
    store = build_commit_one_store(_CountingStore())
    assert isinstance(store, FakeCommitStore)


def test_r10_real_es_p18_store_passes_protocol_gate(monkeypatch):
    from news_flash_dedup.es_client import APPROVED_UAT_HOST
    from news_flash_dedup.persist.es_store import (
        RealESP18Config,
        RealESP18Store,
        build_commit_one_store,
    )

    monkeypatch.setenv("P18_CONFIRM_UAT", "1")
    monkeypatch.setenv("DEPLOY_ENV", "test")
    monkeypatch.setenv("TEST_ES_HOST", APPROVED_UAT_HOST)
    for key in ("PROD_ES_HOST", "PROD_ES_PORT", "PROD_ES_USER", "PROD_ES_PASS",
                "PROD_ES_SCHEME", "ALLOW_PROD_WRITE"):
        monkeypatch.delenv(key, raising=False)
    client = FakeESClient()
    store = RealESP18Store(client, RealESP18Config(
        run_uuid="b4r10", business_date=datetime(2026, 9, 29, tzinfo=timezone.utc).date()))
    assert build_commit_one_store(store) is store
    # 三件套同名方法齐全
    for name in ("get_watermark", "write_main_record", "advance_watermark"):
        assert callable(getattr(store, name))


# ---------- R-11 影子决策不生效闸（R148） ----------

def _shadow_setup():
    text1 = "美国能源信息署公布原油库存增加。"
    doc1, doc2 = _doc(1, text1), _doc(2, text1)
    candidate = RecallCandidate(
        record_id=doc1["record_id"], item_id=doc1["item_id"], arrival_seq=1,
        text=text1, subpaths=("raw",), score=1.0, query_version="hash_v1")
    store = _CountingStore()
    sink = _ListSink()
    service = _service({2: (candidate,)})
    worker, parts = _worker("shadow", docs=[doc2], plan_map={2: (candidate,)},
                            store=store, sink=sink, service=service)
    return worker, parts, store, sink, doc1, doc2


def test_r11_shadow_never_calls_commit_store_and_never_delivers():
    worker, parts, store, sink, _, doc2 = _shadow_setup()
    report = worker.drive_once(SCOPE, DAY)
    assert report.shadowed == (doc2["record_id"],)
    assert report.committed == ()
    # fake 仓储协议三件套调用计数=0（无一被调）
    assert store.protocol_calls == {"get_watermark": 0, "write_main_record": 0,
                                    "advance_watermark": 0}
    # 零 callback/零投递意图：仓储全记录面零写入（cas/watermark/audit 全空）
    assert store.cas_calls == [] and store.watermark_calls == []
    assert store.main_records == {} and store.audit_docs == {}
    # 双工件双落：真计划+影子决策
    kinds = [kind for kind, _ in sink.emitted]
    assert kinds == ["recall_plan", "shadow_decision"]


def test_r11_shadow_decision_artifact_full_form_and_equal_to_effective_leg():
    worker, _, store, sink, doc1, doc2 = _shadow_setup()
    worker.drive_once(SCOPE, DAY)
    plan_payload = dict(sink.emitted[0][1])
    shadow_payload = dict(sink.emitted[1][1])
    # 影子决策工件：五字段+签发码全形（DecideOutcome 全要素）
    outcome = shadow_payload["outcome"]
    assert set(outcome) == {"item_id", "text", "decision", "duplicate_ids",
                            "reason", "internal_code", "pair_codes",
                            "unresolved_fields", "raw_hash", "pipeline_version"}
    assert shadow_payload["leg"] == "B"
    assert shadow_payload["plan_ref"]
    assert shadow_payload["generated_at"] == NOW.isoformat()
    # 零投递意图：工件无 callback/delivery 字段
    assert not any("callback" in key or "delivery" in key
                   for key in shadow_payload)
    # 计划工件承载通道审计与缺口（embedding 由 fake 通道 complete → 无缺口）
    assert plan_payload["kind"] == "recall_plan"
    assert len(plan_payload["channel_audits"]) == 5
    assert plan_payload["required"][0]["record_id"] == doc1["record_id"]

    # 同输入同语义源对拍（coordinator.py:144-160 同型）：同 current 同候选集
    # 走生效腿 commit_one（fake 仓储），两路决策全等（五字段+签发码）
    service = _service({2: (RecallCandidate(
        record_id=doc1["record_id"], item_id=doc1["item_id"], arrival_seq=1,
        text=doc1["text"], subpaths=("raw",), score=1.0, query_version="hash_v1"),)})
    request = service.build_plan(RecallRequest(
        SCOPE, DAY, doc2["record_id"], doc2["item_id"], 2,
        doc2["text"], visible_seq=99, prepared_seq=99,
        embedding_space_id="space-x"))
    current, candidates, coverage, expires_at = service.build_commit_inputs(
        request, doc2)
    effective_store = FakeCommitStore()
    ctx = CommitContext(
        scope_id=SCOPE, business_date=DAY, arrival_seq=2,
        current=current, candidates=candidates, visible_seq=99, prepared_seq=99,
        coverage_complete=coverage)
    effective = commit_one(ctx, effective_store, audit_complete=True,
                           coverage_complete=coverage, expires_at=expires_at)
    assert effective.state == "committed"
    public = effective.decide_outcome.to_public_dict()
    assert outcome["item_id"] == public["item_id"]
    assert outcome["text"] == public["text"]
    assert outcome["decision"] == public["decision"]
    assert outcome["duplicate_ids"] == public["duplicate_ids"]
    assert outcome["reason"] == public["reason"]
    assert outcome["internal_code"] == effective.decide_outcome.internal_code
    # 同文恒等投影→两路同判疑难（decide 现役语义：占位投影不产生"重复"输出）
    assert effective.decide_outcome.decision == "边界case/疑难case"
    assert effective.decide_outcome.internal_code == "FACT_INCOMPLETE"


def test_r11_shadow_zero_candidate_branch_matches_commit_one_shape():
    doc = _doc(1, "首条正文。")
    store = _CountingStore()
    sink = _ListSink()
    service = _service({})
    worker, _ = _worker("shadow", docs=[doc], store=store, sink=sink, service=service)
    worker.drive_once(SCOPE, DAY)
    shadow_outcome = dict(sink.emitted[1][1])["outcome"]
    # 生效腿零候选同型分支（coordinator.py:161-198）：coverage=True → 不重复
    service2 = _service({})
    plan = service2.build_plan(RecallRequest(
        SCOPE, DAY, doc["record_id"], doc["item_id"], 1,
        doc["text"], visible_seq=99, prepared_seq=99,
        embedding_space_id="space-x"))
    current, candidates, coverage, expires_at = service2.build_commit_inputs(plan, doc)
    assert candidates == ()
    ctx = CommitContext(scope_id=SCOPE, business_date=DAY, arrival_seq=1,
                        current=current, candidates=(), visible_seq=99,
                        prepared_seq=99, coverage_complete=coverage)
    effective = commit_one(ctx, FakeCommitStore(), audit_complete=True,
                           coverage_complete=coverage, expires_at=expires_at)
    assert shadow_outcome["decision"] == effective.decide_outcome.decision == "不重复"
    assert shadow_outcome["internal_code"] == effective.decide_outcome.internal_code
    assert shadow_outcome["reason"] == effective.decide_outcome.reason
    assert store.protocol_calls["write_main_record"] == 0
