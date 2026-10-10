# -*- coding: utf-8 -*-
"""修复波 2·§1 ⑩预算挂载接线红测（31 件，单文件自洽夹具）。

设计真源=log\\temp\\wave2-design.md §1.1-§1.7（红测照稿施工，实现窗夹具微调
三处——均不改变语义钉，逐处登记如下）：

实现窗夹具微调登记（设计 §4"实现窗冒烟实证/按同族先例调整夹具——语义钉不动"
授权）：
1. _FakeChannel/_FakePrepare 增 **_kw 容忍：设计草稿的 2 参夹具与设计自身接线
   矛盾——预算在场路径 build_plan 向通道传 timeout_s=...（§1.4-4）、worker 向
   prepare 传 budget=...（§1.2-2），草稿夹具会在 b2/b5/c2 绿态 TypeError。
   零 diff 语义钉由专职严格双倍体承载（e3 _LegacyFakeChannel 2 参 strict——
   误传 timeout_s 即 TypeError；c1 签名钉；b1/b4/b6 行为钉），防漂移能力不降。
2. _worker 条件式 budget_kwargs：budget_config=None 时构造面逐字节同现役
   （不传 budget_config/clock_mono 两 kw）——保住 b1 设计红锚（AttributeError:
   budget_exhausted 尾字段缺失）与 b5 红锚（drive_once 无 budget kw TypeError），
   不被构造面 kw TypeError 抢先。
3. h4 "重复"已证面夹具按 test_p17_implementation.py:186-189 同族先例补
   verified_missing 注入（N29/D28 注记：未命中型 missing 现记 unknown→边界，
   无注入则 h4 结构性得不到"重复"——设计 §3.1 caveat 授权按先例调整）。
4. _FakeScanner 方法名 scan（worker.py:361 现役调用形 `scanner.scan(scope_id,
   business_date, after_seq=cursor)`——设计草稿 scan_registered 与亲读锚不符）；
   h 族 facts=[] 夹具按 test_recall_worker.py:190-212 同族先例改恒等投影单
   fact（冒烟实证：零 Fact 报告触发 pair_alignment.py:377 结构性拒收
   PairAlignmentError，非"边界"——设计 §3.1 caveat 授权按先例调整；语义钉
   "边界+预算尽→预算侧码覆盖/未尽不动/零 diff"逐字不动）；d4 夹具同此
   先例补恒等投影（其带候选路径必进 decide_for_task）；c2 按
   test_recall_worker.py:238-251 同族先例 arrival_seq=2（lexical=0<seq-1=1
   才触发 STALE——设计草稿 seq=1 恒不 STALE）；g 族夹具 API key 环境名
   按 embedding_client.py 现役键名 DEDUP_EMBEDDING_API_KEY（设计草稿
   QWEN_API_KEY 与现役键面不符）。
"""

from __future__ import annotations

import hashlib
import inspect
from dataclasses import replace as _dc_replace
from datetime import datetime, timedelta, timezone

import pytest

import news_flash_dedup.runtime_budget as rb
from news_flash_dedup.commit.coordinator import CommitContext, CommitOneError, commit_one
from news_flash_dedup.commit.fake_store import FakeCommitStore
from news_flash_dedup.recall.models import ChannelResult, RecallCandidate, RecallRequest
from news_flash_dedup.recall.prepare import PrepareWorker
from news_flash_dedup.recall.service import RecallService
from news_flash_dedup.recall.worker import DedupWorker, DriveReport

DAY = "2026-09-29"
SCOPE = "default"
PREFIX = "p01-batch-w2-"
T0_WALL = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)


class _Mono:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def _config(**over):
    base = dict(max_processing_s=15.0, prepare_soft_s=10.0,
                commit_target_s=1.0, channel_timeout_s=0.3,
                per_call_timeout_s=3.0, max_model_calls=20,
                shared_append_budget=1)
    base.update(over)
    return rb.RuntimeBudgetConfig(**base)


# ---------- ①-2 worker 总闸 ----------

def _doc(seq, text, *, accepted_at=T0_WALL.isoformat()):
    return {
        "scope_id": SCOPE, "request_id": f"9-{seq}", "item_id": f"9-{seq}",
        "record_id": hashlib.sha256(f"rec-{seq}".encode()).hexdigest(),
        "business_date": DAY, "arrival_seq": seq,
        "text": text, "raw_hash": hashlib.sha256(text.encode()).hexdigest(),
        "embedding_space_id": "space-x",
        "expires_at": "2026-10-06T00:00:00.000000Z",
        "task_state": "accepted", "delivery_state": "not_ready", "result": None,
        "accepted_at": accepted_at,
    }


class _FakeChannel:
    def __init__(self, fn):
        self.fn = fn

    def search(self, request, query_vector=None, **_kw):   # 微调①见文件头
        return self.fn(request)


def _complete(channel, qv, request):
    return ChannelResult(channel, "complete", (), qv,
                         visible_seq=request.visible_seq,
                         prepared_seq=request.prepared_seq,
                         coverage_complete=True)


def _service():
    channels = {
        "hash": _FakeChannel(lambda r: _complete("hash", "hash_v1", r)),
        "near": _FakeChannel(lambda r: _complete("near", "near_v1", r)),
        "bm25": _FakeChannel(lambda r: _complete("bm25", "bm25_v1", r)),
        "entity": _FakeChannel(lambda r: _complete("entity", "entity_v1", r)),
    }
    vector = _FakeChannel(lambda r: _complete("embedding", "embedding_v1", r))
    return RecallService(None, PREFIX, channels=channels, vector_searcher=vector)


class _FakePrepare:
    def __init__(self):
        self.calls = []

    def prepare(self, scope_id, business_date, **_kw):     # 微调①见文件头
        self.calls.append((scope_id, business_date))


class _MutableWatermark:
    def __init__(self, visible, prepared):
        self.visible = visible
        self.prepared = prepared

    def visible_seq(self, scope_id, business_date):
        return self.visible

    def prepared_seq(self, scope_id, business_date):
        return self.prepared


class _FakeScanner:
    def __init__(self, docs):
        self._docs = docs

    def scan(self, scope_id, business_date, *, after_seq=0):   # worker.py:361 同形
        return [dict(doc) for doc in self._docs]


class _FakeArtifactSink:
    def __init__(self):
        self.calls = []

    def write_artifact(self, **kw):
        self.calls.append(kw)
        return f"artifacts/fake-{len(self.calls)}.json"


def _worker(docs, *, store, budget_config=None, watermark=None, mono=None):
    scanner = _FakeScanner(docs)
    budget_kwargs = {}                                     # 微调②见文件头
    if budget_config is not None:
        budget_kwargs = {"budget_config": budget_config,
                         "clock_mono": mono or _Mono()}
    worker = DedupWorker(
        mode="live", recall_service=_service(),
        prepare_worker=_FakePrepare(),
        watermark_provider=watermark or _MutableWatermark(99, 99),
        scanner=scanner, commit_store=store, artifact_sink=_FakeArtifactSink(),
        clock=lambda: T0_WALL,
        **budget_kwargs)
    return worker


def test_w2_b1_budget_absent_worker_zero_diff():
    """零 diff 主轴：无预算件驱动——现役逐字节（提交发生、无 budget_exhausted 桶）。"""
    doc = _doc(1, "正文一。")
    store = FakeCommitStore()
    worker = _worker([doc], store=store)
    report = worker.drive_once(SCOPE, DAY)
    assert report.committed == (doc["record_id"],)
    assert report.budget_exhausted == ()


def test_w2_b2_derive_queue_delay_t082():
    """T082 联动：accepted_at=12s 前（墙钟）→derive 平移后 remaining≈3s（排队不重置）。"""
    accepted = (T0_WALL - timedelta(seconds=12)).isoformat()
    doc = _doc(1, "正文一。", accepted_at=accepted)
    store = FakeCommitStore()
    mono = _Mono()
    seen = {}
    real_derive = rb.ProcessingBudget.derive

    def spy_derive(*, accepted_at_mono, config, clock_mono):
        seen["accepted_at_mono"] = accepted_at_mono
        return real_derive(accepted_at_mono=accepted_at_mono,
                           config=config, clock_mono=clock_mono)

    worker = _worker([doc], store=store, budget_config=_config(), mono=mono)
    orig = rb.ProcessingBudget.derive
    rb.ProcessingBudget.derive = staticmethod(spy_derive)
    try:
        report = worker.drive_once(SCOPE, DAY)
    finally:
        rb.ProcessingBudget.derive = orig
    # 平移锚：accepted_at_mono = mono_now - 12（排队 12s 仅余 3s，不重置）
    assert seen["accepted_at_mono"] == pytest.approx(mono.t - 12.0)
    assert report.committed == (doc["record_id"],)    # 余 3s 未到期→正常提交


def test_w2_b3_exhausted_gate_blocks_and_breaks():
    """总闸到期：不启动（零写零提交）、入 budget_exhausted 桶、后续升序条同断。"""
    old = (T0_WALL - timedelta(seconds=20)).isoformat()   # 过 15s
    doc1, doc2 = _doc(1, "正文一。", accepted_at=old), _doc(2, "正文二。", accepted_at=old)
    store = FakeCommitStore()
    worker = _worker([doc1, doc2], store=store, budget_config=_config())
    report = worker.drive_once(SCOPE, DAY)
    assert report.committed == ()
    assert report.budget_exhausted == (doc1["record_id"],)   # 首条到期即停（单飞）
    assert store.get_watermark(SCOPE, DAY) in (None, 0)
    assert getattr(store, "main_records", {}) == {}


def test_w2_b4_missing_accepted_at_fail_closed():
    """缺 accepted_at=预算不可证 fail-closed：不启动模型/提交，入桶不猜。"""
    doc = _doc(1, "正文一。", accepted_at=None)
    store = FakeCommitStore()
    worker = _worker([doc], store=store, budget_config=_config())
    report = worker.drive_once(SCOPE, DAY)
    assert report.committed == ()
    assert report.budget_exhausted == (doc["record_id"],)


def test_w2_b5_explicit_budget_reclaim_same_instance():
    """显式 budget 重领：STALE 重试环持同一实例重入（结构性不重置）。"""
    from news_flash_dedup.recall import worker as worker_mod
    doc = _doc(1, "正文一。", accepted_at=T0_WALL.isoformat())
    store = FakeCommitStore()
    budget = rb.ProcessingBudget.derive(accepted_at_mono=1000.0,
                                        config=_config(), clock_mono=_Mono())
    seen = []
    real_commit_one = worker_mod.commit_one

    def spy_commit_one(ctx, store_, **kw):
        seen.append(kw.get("budget"))
        return real_commit_one(ctx, store_, **kw)

    worker = _worker([doc], store=store)
    worker_mod.commit_one = spy_commit_one
    try:
        worker.drive_once(SCOPE, DAY, budget=budget)
    finally:
        worker_mod.commit_one = real_commit_one
    assert seen and all(instance is budget for instance in seen)


def test_w2_b6_report_field_additive_default():
    """DriveReport additive 尾字段：现役位置构造（无 budget_exhausted 实参）零冲击。"""
    report = DriveReport(mode="fixed", scope_id=SCOPE, business_date=DAY)
    assert report.budget_exhausted == ()


# ---------- ①-3 prepare 软预算 ----------

def test_w2_c1_prepare_accepts_budget_kw_default_none():
    """prepare 传播通路：kw-only budget，None 默认——现役调用形态零冲击（签名钉）。"""
    sig = inspect.signature(PrepareWorker.prepare)
    assert "budget" in sig.parameters
    param = sig.parameters["budget"]
    assert param.kind is inspect.Parameter.KEYWORD_ONLY
    assert param.default is None


def test_w2_c2_worker_propagates_budget_to_prepare_both_sites():
    """worker 首驱+STALE 重驱两调用点传播同一 budget 实例（不重建）。"""
    # 微调④（见文件头）：arrival_seq=2 使 lexical=0<1 恒 STALE
    # （test_recall_worker.py:238-251 同族先例同水位形）。
    doc = _doc(2, "正文二。", accepted_at=T0_WALL.isoformat())
    store = FakeCommitStore()
    watermark = _MutableWatermark(visible=0, prepared=99)  # lexical 恒过期→STALE
    prepare_calls = []

    class _SpyPrepare:
        def prepare(self, scope_id, business_date, **kw):
            prepare_calls.append(kw.get("budget"))

    budget = rb.ProcessingBudget.derive(accepted_at_mono=1000.0,
                                        config=_config(), clock_mono=_Mono())
    scanner = _FakeScanner([doc])
    worker = DedupWorker(
        mode="live", recall_service=_service(),
        prepare_worker=_SpyPrepare(), watermark_provider=watermark,
        scanner=scanner, commit_store=store, artifact_sink=_FakeArtifactSink(),
        clock=lambda: T0_WALL, max_stale_retries=1,
        budget_config=_config(), clock_mono=_Mono())
    report = worker.drive_once(SCOPE, DAY, budget=budget)
    assert report.stale == (doc["record_id"],)
    assert len(prepare_calls) == 2                     # 首驱 + STALE 重驱
    assert all(call is budget for call in prepare_calls)


# ---------- ①-4 CAS 临界区 ----------

def _commit_ctx(*, candidates=()):
    from news_flash_dedup.recall.service import IdentityFactSupply
    # 微调④（见文件头）：恒等投影单 fact——d4 带候选路径必进 decide，
    # 零 Fact 触发 pair_alignment.py:377 结构性拒收（同 h 族先例）；
    # d1/d2/d3 零候选路径不经 decide，无副作用。
    text = "正文二。"
    current = {
        "record_id": "c" * 64, "item_id": "item-C", "text": text,
        "raw_hash": hashlib.sha256(text.encode()).hexdigest(),
        "scope_id": "default", "business_date": "2026-09-26",
        "arrival_seq": 3, "pipeline_version": "dedup_v1",
        "facts": IdentityFactSupply()("c" * 64, text),
    }
    return CommitContext(scope_id="default", business_date="2026-09-26",
                         arrival_seq=3, current=current, candidates=candidates,
                         visible_seq=10, prepared_seq=10, coverage_complete=True)


def test_w2_d1_commit_below_target_refuses_before_audit_and_cas():
    """临界区启动闸：remaining<commit_target_s→CommitOneError，审计/CAS 零写。"""
    mono = _Mono()
    budget = rb.ProcessingBudget.derive(accepted_at_mono=1000.0,
                                        config=_config(), clock_mono=mono)
    mono.t += 14.5                                     # 余 0.5s < 1s
    store = FakeCommitStore()
    with pytest.raises(CommitOneError, match="budget below commit target"):
        commit_one(_commit_ctx(), store, audit_complete=True, budget=budget)
    assert getattr(store, "cas_calls", []) == []
    assert store.get_watermark("default", "2026-09-26") in (None, 0)


def test_w2_d2_commit_with_headroom_commits_normally():
    """绿守卫：remaining≥commit_target_s→现役提交流全通（水位推进照原）。"""
    mono = _Mono()
    budget = rb.ProcessingBudget.derive(accepted_at_mono=1000.0,
                                        config=_config(), clock_mono=mono)
    mono.t += 5.0                                      # 余 10s ≥ 1s
    store = FakeCommitStore()
    outcome = commit_one(_commit_ctx(), store, audit_complete=True, budget=budget)
    assert outcome.state == "committed"
    assert store.get_watermark("default", "2026-09-26") == 3


def test_w2_d3_budget_none_commit_zero_diff():
    """budget=None→commit_one 行为逐字节同现役（无预算 kw 调用形态）。"""
    store = FakeCommitStore()
    outcome = commit_one(_commit_ctx(), store, audit_complete=True)
    assert outcome.state == "committed"


def test_w2_d4_commit_propagates_budget_to_decide():
    """decide 透传钉：commit_one 收到的 budget 原样传入 decide_for_task。"""
    import news_flash_dedup.commit.coordinator as coord_mod
    from news_flash_dedup.recall.service import IdentityFactSupply
    text_h = "正文一。"
    history = {
        "record_id": "b" * 64, "item_id": "item-B", "text": text_h,
        "raw_hash": hashlib.sha256(text_h.encode()).hexdigest(),
        "scope_id": "default", "business_date": "2026-09-26",
        "arrival_seq": 1, "pipeline_version": "dedup_v1",
        "facts": IdentityFactSupply()("b" * 64, text_h),
    }
    budget = rb.ProcessingBudget.derive(accepted_at_mono=1000.0,
                                        config=_config(), clock_mono=_Mono())
    seen = {}
    real_decide = coord_mod.decide_service.decide_for_task

    def spy_decide(**kw):
        seen["budget"] = kw.get("budget")
        return real_decide(**kw)

    coord_mod.decide_service.decide_for_task = spy_decide
    try:
        ctx = _dc_replace(_commit_ctx(), candidates=(history,))
        commit_one(ctx, FakeCommitStore(), audit_complete=True, budget=budget)
    finally:
        coord_mod.decide_service.decide_for_task = real_decide
    assert seen.get("budget") is budget


# ---------- ①-5 build_plan 300ms ----------

class _TimeoutRecordingChannel:
    def __init__(self, name):
        self.name = name
        self.timeouts = []

    def search(self, request, query_vector=None, *, timeout_s=None):
        self.timeouts.append(timeout_s)
        return ChannelResult(self.name, "complete", (), self.name + "_v1",
                             visible_seq=request.visible_seq,
                             prepared_seq=request.prepared_seq,
                             coverage_complete=True)


def _request():
    return RecallRequest(SCOPE, DAY, "c" * 64, "item-C", 3, "正文三。",
                         visible_seq=10, prepared_seq=10,
                         embedding_space_id="space-x")


def _service_with_recorders():
    channels = {name: _TimeoutRecordingChannel(name)
                for name in ("hash", "near", "bm25", "entity")}
    vector = _TimeoutRecordingChannel("embedding")
    svc = RecallService(None, PREFIX, channels=channels, vector_searcher=vector)
    return svc, list(channels.values()) + [vector]


def test_w2_e1_channel_timeout_injected_300ms():
    """④300ms：budget 在场→五通道齐收 timeout_s=budgeted_timeout(0.3)。"""
    svc, recorders = _service_with_recorders()
    mono = _Mono()
    budget = rb.ProcessingBudget.derive(accepted_at_mono=1000.0,
                                        config=_config(), clock_mono=mono)
    mono.t += 5.0                                     # 余 10s > 0.3s
    svc.build_plan(_request(), budget=budget)
    for rec in recorders:
        assert rec.timeouts == [pytest.approx(0.3)]


def test_w2_e2_channel_timeout_obeys_remaining():
    """服从剩余预算：余 0.1s → timeout_s 截断 0.1（12 L376）。"""
    svc, recorders = _service_with_recorders()
    mono = _Mono()
    budget = rb.ProcessingBudget.derive(accepted_at_mono=1000.0,
                                        config=_config(), clock_mono=mono)
    mono.t += 14.9
    svc.build_plan(_request(), budget=budget)
    for rec in recorders:
        assert rec.timeouts == [pytest.approx(0.1)]


def test_w2_e3_budget_none_no_timeout_kw():
    """budget=None→调用形态零 diff：通道不接收 timeout_s kw（既有双倍体守卫）。"""
    calls = []

    class _LegacyFakeChannel:                          # test_recall_worker.py:54 同形
        def __init__(self, fn):
            self.fn = fn

        def search(self, request, query_vector=None):
            calls.append(request)
            return self.fn(request)

    def ok(name):
        return lambda r: ChannelResult(name, "complete", (), name + "_v1",
                                       coverage_complete=True)

    channels = {name: _LegacyFakeChannel(ok(name))
                for name in ("hash", "near", "bm25", "entity")}
    svc = RecallService(None, PREFIX, channels=channels,
                        vector_searcher=_LegacyFakeChannel(ok("embedding")))
    svc.build_plan(_request())                        # 无 budget——旧双倍体不炸即证
    assert len(calls) == 5


def test_w2_e4_real_channel_consumes_request_timeout():
    """通道消费钉（bm25 代表，余三通道同型）：timeout_s→client.search request_timeout。"""
    from news_flash_dedup.recall.bm25_channel import BM25Channel
    seen = {}

    class _RecordingES:
        def search(self, *, index, body, request_timeout=None):
            seen["request_timeout"] = request_timeout
            return {"timed_out": False, "_shards": {"failed": 0},
                    "hits": {"hits": []}}

    channel = BM25Channel(_RecordingES(), "p01-batch-b4wrk-",
                          clock=lambda: T0_WALL)
    channel.search(_request(), timeout_s=0.3)
    assert seen["request_timeout"] == pytest.approx(0.3)


# ---------- ①-6 facts 与 llm_residual 双注入点 ----------

def test_w2_f1_facts_budget_none_constants_byte_identical():
    """值无关主轴钉：budget=None→timeout_s=30.0/max_retries=2 现役常量路径逐字节。"""
    from news_flash_dedup.facts.llm import extract_facts_llm
    seen = {}

    def ok_fn(**kw):
        seen.update(kw)
        return ('{"facts": []}', 0.01)

    extract_facts_llm("a" * 64, "正文二。", call_fn=ok_fn)
    assert seen["timeout_s"] == 30.0                  # 现役 :479 常量（零 diff 钉）
    # 7b965b6（2026-10-10 用户令：qwen-turbo 下架直换 qwen-plus，测试移交
    # 第二 AI）：值无关主轴钉的模型字面量随现役 DEFAULT_MODEL 对齐
    assert seen["model"] == "qwen-plus"


def test_w2_f2_facts_permit_drives_retry_and_charge():
    """permit 驱动环：瞬时失败→共享追加一次（无第三次）；per_call_timeout 生效。"""
    from news_flash_dedup.facts.llm import LlmExtractionError, extract_facts_llm
    budget = rb.ProcessingBudget.derive(accepted_at_mono=1000.0,
                                        config=_config(), clock_mono=_Mono())
    calls = []

    def flaky(**kw):
        calls.append(kw)
        raise ConnectionError("boom")

    with pytest.raises(LlmExtractionError, match="预算拒付"):
        extract_facts_llm("a" * 64, "正文二。", call_fn=flaky, budget=budget)
    assert len(calls) == 2                            # 首次+共享追加（shared_append_budget=1）
    assert all(c["timeout_s"] == pytest.approx(3.0) for c in calls)
    assert budget.model_calls_spent == 2              # 统一账户：两尝试同扣


def test_w2_f3_facts_cache_hit_not_charged():
    """缓存命中结构性不计（12 L377）：命中分支在 permit 创建前返回。"""
    import json as _json
    from news_flash_dedup.facts.llm import LLM_PROMPT_VERSION, extract_facts_llm
    budget = rb.ProcessingBudget.derive(accepted_at_mono=1000.0,
                                        config=_config(), clock_mono=_Mono())
    text = "正文二。"
    key = hashlib.sha256(text.encode()).hexdigest()
    cache = tmp_path_factory_fixture(key)
    _json.dumps({})                                   # noqa：保持 json 引用显式
    (cache / f"{key}.json").write_text(_json.dumps({
        # 7b965b6 模型切换对齐：缓存命中键含 model——与现役默认一致才命中
        "model": "qwen-plus", "prompt_version": LLM_PROMPT_VERSION,
        "raw_llm_content": '{"facts": []}', "issues": []}), encoding="utf-8")

    def boom(**kw):                                   # 命中面若误创建调用即炸
        raise AssertionError("cache hit must not invoke call_fn")

    facts = extract_facts_llm("a" * 64, text, call_fn=boom,
                              cache_dir=str(cache), budget=budget)
    assert budget.model_calls_spent == 0
    assert facts                                      # 缓存面正常产出（兜底 facts 不空）


def tmp_path_factory_fixture(key):
    """f3 缓存目录夹具（模块级 tmp 路径，测试内唯一）。"""
    import tempfile
    from pathlib import Path
    path = Path(tempfile.mkdtemp(prefix="wave2-f3-cache-"))
    return path


def test_w2_f4_facts_soft_gate_refuses_new_call_uncharged():
    """软预算执法面：越 accepted+10s → 拒启新调用（诚实失败），charge 未发生。"""
    from news_flash_dedup.facts.llm import LlmExtractionError, extract_facts_llm
    mono = _Mono()
    budget = rb.ProcessingBudget.derive(accepted_at_mono=1000.0,
                                        config=_config(), clock_mono=mono)
    mono.t += 10.5                                    # 越 prepare_soft_s=10

    def boom(**kw):
        raise AssertionError("软边界后不得启动新模型调用")

    with pytest.raises(LlmExtractionError, match="预算软边界"):
        extract_facts_llm("a" * 64, "正文二。", call_fn=boom, budget=budget)
    assert budget.model_calls_spent == 0


def test_w2_f5_residual_permit_drives_retry_and_charge():
    """llm_residual：permit 驱动环——瞬时失败共享追加一次；台账 failures 记账。"""
    from news_flash_dedup.decide.llm_residual import (
        LlmResidualError, ResidualJudgeConfig, SyncResidualJudge)
    budget = rb.ProcessingBudget.derive(accepted_at_mono=1000.0,
                                        config=_config(), clock_mono=_Mono())
    calls = []

    def flaky(**kw):
        calls.append(kw)
        raise ConnectionError("boom")

    judge = SyncResidualJudge(ResidualJudgeConfig(), api_key="sk-test",
                              call_fn=flaky, budget=budget)
    with pytest.raises(LlmResidualError, match="预算拒付"):
        judge._call_cached(pair_id="p1", order="hc", system="sys", user="u")
    assert len(calls) == 2
    assert budget.model_calls_spent == 2
    assert judge.ledger.snapshot().get("failures", 0) >= 1


def test_w2_f6_residual_budget_none_3arg_mock_unchanged():
    """budget=None→:564 mock 三参形态与 60.0/3 现役常量逐字节（旧双倍体守卫）。"""
    from news_flash_dedup.decide.llm_residual import (
        ResidualJudgeConfig, SyncResidualJudge)
    cfg = ResidualJudgeConfig()
    assert cfg.timeout_s == 60.0 and cfg.max_retries == 3   # 现役 :71-72 常量钉
    seen = {}

    def ok_fn(**kw):
        seen.update(kw)
        return ("raw-content", 0.01)

    judge = SyncResidualJudge(cfg, api_key="sk-test", call_fn=ok_fn)
    raw, latency, hit, attempts = judge._call_cached(
        pair_id="p1", order="hc", system="sys", user="u")
    assert set(seen) == {"model", "system", "user"}   # 无 timeout_s 键（现役 :564）
    assert (hit, attempts) == (False, 1)


# ---------- ①-7 统一计账 ----------

def _embedding_client(*, budget=None, transport=None):
    from news_flash_dedup.vector.embedding_client import (
        EmbeddingClient, EmbeddingClientConfig)
    cfg = EmbeddingClientConfig(base_url="https://example.invalid/v1",
                                model="text-embedding-v3", dimension=4,
                                cache_root=None)
    calls = []

    def recording_transport(**kw):
        calls.append(kw)
        return (transport(**kw) if transport is not None else {
            "data": [{"index": i, "embedding": [0.1, 0.2, 0.3, 0.4]}
                     for i, _ in enumerate(kw["texts"])],
            "usage": {"total_tokens": 5}})

    client = EmbeddingClient(cfg, transport_fn=recording_transport,
                             env={"DEDUP_EMBEDDING_API_KEY": "sk-test-key-123456"},
                             sleep=lambda _s: None, persist_budget=False,
                             budget=budget)
    return client, calls


def test_w2_g1_charge_per_attempt_unified_account():
    """每次 transport 尝试前 charge：2 批成功=spent 2；与 facts/residual 同一账户。"""
    budget = rb.ProcessingBudget.derive(accepted_at_mono=1000.0,
                                        config=_config(), clock_mono=_Mono())
    client, calls = _embedding_client(budget=budget)
    client.embed_documents(["文本甲。", "文本乙。"])
    assert len(calls) == 1                                  # ≤10 单批
    assert budget.model_calls_spent == 1


def test_w2_g2_retries_charged_each_attempt():
    """瞬时重试统一计账（12 L377）：连接故障重试 2 次=spent 3（首次+2 追加）。"""
    from news_flash_dedup.vector.embedding_client import EmbeddingConnectionFault
    budget = rb.ProcessingBudget.derive(accepted_at_mono=1000.0,
                                        config=_config(), clock_mono=_Mono())
    state = {"n": 0}

    def flaky_transport(**kw):
        state["n"] += 1
        if state["n"] < 3:
            raise EmbeddingConnectionFault("boom")
        return {"data": [{"index": 0, "embedding": [0.1, 0.2, 0.3, 0.4]}],
                "usage": {"total_tokens": 5}}

    client, _ = _embedding_client(budget=budget, transport=flaky_transport)
    client.embed_documents(["文本甲。"])
    assert budget.model_calls_spent == 3


def test_w2_g3_account_ceiling_refuses_without_charge():
    """账户尽→拒付不扣（T082'到期不再启动模型'同义）：spent 停在上限。"""
    from news_flash_dedup.vector.embedding_client import EmbeddingApiError
    budget = rb.ProcessingBudget.derive(
        accepted_at_mono=1000.0, config=_config(max_model_calls=1),
        clock_mono=_Mono())
    client, calls = _embedding_client(budget=budget)
    client.embed_documents(["文本甲。"])                     # spent=1
    with pytest.raises(EmbeddingApiError, match="budget exhausted"):
        client.embed_documents(["文本丙。"])
    assert budget.model_calls_spent == 1
    assert len(calls) == 1                                  # 拒付零新 transport


def test_w2_g4_budget_none_zero_diff():
    """budget=None：零计账零闸——现役行为逐字节（无 budget 属性面暴露）。"""
    client, calls = _embedding_client(budget=None)
    client.embed_documents(["文本甲。"])
    assert len(calls) == 1


# ---------- ①-8 归因面 ----------

def _decide_current():
    from news_flash_dedup.recall.service import IdentityFactSupply
    text = "甲公司完成回购。"
    return {"record_id": "c" * 64, "item_id": "item-C", "text": text,
            "raw_hash": hashlib.sha256(text.encode()).hexdigest(),
            "scope_id": "default", "business_date": "2026-09-26",
            "arrival_seq": 3, "pipeline_version": "dedup_v1",
            "facts": IdentityFactSupply()("c" * 64, text)}


def _decide_history():
    from news_flash_dedup.recall.service import IdentityFactSupply
    text = "甲公司完成回购。"
    return {"record_id": "b" * 64, "item_id": "item-A", "text": text,
            "raw_hash": hashlib.sha256(text.encode()).hexdigest(),
            "scope_id": "default", "business_date": "2026-09-26",
            "arrival_seq": 1, "pipeline_version": "dedup_v1",
            "facts": IdentityFactSupply()("b" * 64, text)}


def test_w2_h1_boundary_with_time_exhausted_covers_to_dependency_timeout():
    """T011：边界+总闸尽→DEPENDENCY_TIMEOUT 具体归因覆盖。"""
    from news_flash_dedup.decide.service import decide_for_task
    mono = _Mono()
    budget = rb.ProcessingBudget.derive(accepted_at_mono=1000.0,
                                        config=_config(), clock_mono=mono)
    mono.t += 16.0                                    # 总闸尽
    outcome = decide_for_task(_decide_history(), [], current=_decide_current(),
                              coverage_complete=True, budget=budget)
    assert outcome.decision == "边界case/疑难case"
    assert outcome.internal_code == "DEPENDENCY_TIMEOUT"
    assert "DEPENDENCY_TIMEOUT" not in outcome.reason  # reason 扫描闸不动


def test_w2_h2_boundary_with_account_exhausted_covers_to_candidate_budget():
    """T011：边界+账户尽→CANDIDATE_BUDGET_EXHAUSTED 具体归因覆盖。"""
    from news_flash_dedup.decide.service import decide_for_task
    budget = rb.ProcessingBudget.derive(
        accepted_at_mono=1000.0, config=_config(max_model_calls=1),
        clock_mono=_Mono())
    budget.charge_model_call()                        # spent=1=上限
    outcome = decide_for_task(_decide_history(), [], current=_decide_current(),
                              coverage_complete=True, budget=budget)
    assert outcome.internal_code == "CANDIDATE_BUDGET_EXHAUSTED"
    assert "CANDIDATE_BUDGET_EXHAUSTED" not in outcome.reason


def test_w2_h3_boundary_with_budget_alive_keeps_incumbent_attribution():
    """预算未尽→现役归因不动（FACT_INCOMPLETE 族——facts 层缺工件自带）。"""
    from news_flash_dedup.decide.service import decide_for_task
    budget = rb.ProcessingBudget.derive(accepted_at_mono=1000.0,
                                        config=_config(), clock_mono=_Mono())
    outcome = decide_for_task(_decide_history(), [], current=_decide_current(),
                              coverage_complete=True, budget=budget)
    assert outcome.decision == "边界case/疑难case"
    assert outcome.internal_code not in ("DEPENDENCY_TIMEOUT",
                                         "CANDIDATE_BUDGET_EXHAUSTED")


def test_w2_h4_proven_duplicate_not_overridden():
    """已证面不动：'重复'（已签发确认）+预算尽→不覆盖（EXACT 证书路径）。"""
    from test_e_h04_facts_projection import _complete_fact

    from news_flash_dedup.decide.service import decide_for_task
    mono = _Mono()
    budget = rb.ProcessingBudget.derive(accepted_at_mono=1000.0,
                                        config=_config(), clock_mono=mono)
    mono.t += 16.0
    history, current = _decide_history(), _decide_current()
    text = current["text"]
    history["facts"] = [_complete_fact("b" * 64, text)]
    current["facts"] = [_complete_fact("c" * 64, text)]
    # 微调③（见文件头）：test_p17_implementation.py:186-189 同族先例——
    # 时间槽 verified_missing 注入维持 EXACT 证书路径（N29/D28 注记）。
    for facts in (history["facts"], current["facts"]):
        facts[0]["time"]["expression"] = {
            "status": "missing", "raw_value": None, "evidence": [],
            "verified_missing": True}
    outcome = decide_for_task(history, [], current=current,
                              coverage_complete=True, budget=budget)
    assert outcome.decision == "重复"
    assert outcome.internal_code == "EXACT_TEXT_MATCH"
    assert outcome.duplicate_ids == ("item-A",)


def test_w2_h5_budget_none_decide_zero_diff():
    """budget=None→decide_for_task 逐字节同现役（零 kw 调用形态）。"""
    from news_flash_dedup.decide.service import decide_for_task
    outcome = decide_for_task(_decide_history(), [], current=_decide_current(),
                              coverage_complete=True)
    assert outcome.decision == "边界case/疑难case"
