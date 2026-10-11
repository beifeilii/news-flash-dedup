# -*- coding: utf-8 -*-
"""最终接线窗钉面（2026-10-12，分支 infra/integration-v1）。

四接线点零成本钉（全假件零真 API/ES/Milvus；每点三钉制：
OFF 等价钉+ON 生效钉+非法值 fail-closed 钉——任务令铁律）：

- 点1 llm_facts 注入（P1c 装配注入设计单 §二 A+B）：
  工厂 OFF→None（缺省双轨关）/ON→OnDemandLlmFacts（env 单源 budget
  接线）/非法 knob→构造时刻 ValueError；注入位 B 预装配两腿
  （shadow_decide/commit_one）OFF=None 逐字节、ON=env 装配、显式注入
  优先；注入位 A（create_host→DedupWorker 单源两腿）路由钉；
  KNOWN_SWITCHES 登记（单次不重复）。
- 点2 判官并发执行器消费（decide/service.py 判官段）：
  OFF=judge 双序逐对现役路径（每对 2 路调用+判重签发 JUDGE_EQUIVALENT
  同构；**OFF 态旋钮零执法**——非法 concurrency 不触发 ValueError）；
  ON=预执行供给同一判重签发（调用数守恒 2/对）；ON+单对硬超时→执行器
  弃件→JUDGE_EXCEPTION 未决 fail-closed（不冒签）；ON+非法旋钮→
  decide_for_task ValueError（装配时刻拒产）。
- 点3 Embedding 微批写径（emb 真轨 harness 构造位=设计单 §四-1 具名
  消费点）：OFF=同一对象直通（identity 钉）；ON=BatchedEmbeddingClient
  鸭型包装；非法批大小/窗口→装配时刻 ValueError（解析器无条件执法
  ——与开关态无关，P1c 既有纪律）；harness 源面接线序钉（构造→包装
  →进 pipeline，只读核对）。
- 点4 durable 队列旗标接 env（assemble 组合根构造入口）：
  缺省/0→legacy pending 头（现役路径逐字节——头形态与 test_batch_
  admission 基线同构）；1→durable 游标头（P1a-T2 形态）；显式参
  优先于 env；非法值 ValueError fail-closed；读取点唯一=装配层。
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from news_flash_dedup.api import assemble as asm
from news_flash_dedup.decide import judge_adapter, judge_pair, service as decide_service
from news_flash_dedup.decide import llm_residual as lr
from news_flash_dedup.lib import run_manifest as rm
from news_flash_dedup.recall import fact_supply as fsp
from news_flash_dedup.recall import worker as recall_worker
from news_flash_dedup.commit.coordinator import CommitContext, commit_one
from news_flash_dedup.commit.fake_store import FakeCommitStore
from news_flash_dedup.api.host import create_host

from b4_fake_es import FakeESClient
from test_llm_facts_ondemand import (
    H_ID, R8_C, R8_H, REL_C, REL_H,
    _MockLLM, _current, _dup_responses, _history, _judge, _semantic_env,
)

NOW_ISO_DAY = "2026-09-26"


# ================================================================ 点1：llm_facts 注入

# ---------- 工厂三钉（OFF 等价 / ON 生效 / 非法值 fail-closed）


def test_p1_factory_off_env_absent_and_falsy_values_return_none():
    """OFF 等价钉：DEDUP_LLM_FACTS_ONDEMAND 缺席/空/非真值→None——
    decide_for_task(llm_facts=None) 缺省双轨关，现役语义逐字节
    （fail-closed 默认关：宁可不开不可猜错边）。"""
    assert fsp.build_llm_facts_from_env(env={}) is None
    assert fsp.build_llm_facts_from_env() is None          # 缺省读 os.environ
    for off in ("", "0", "2", "banana", "off", "false"):
        assert fsp.build_llm_facts_from_env(
            env={fsp.LLM_FACTS_ONDEMAND_ENV: off}) is None


def test_p1_factory_on_truthy_values_build_ondemand_instance():
    """ON 生效钉：真值集（strip+lower）→ OnDemandLlmFacts 实例；env 单源
    budget 接线（DEDUP_FACT_DAILY_TOKEN_BUDGET→budget.daily_tokens——
    OnDemandLlmFacts 构造缺省不自读 budget env，工厂显式接线）；
    timeout 沿构造器 env 自读（DEDUP_FACT_TIMEOUT_S）。"""
    for on in ("1", "true", "YES", " On "):
        instance = fsp.build_llm_facts_from_env(env={
            fsp.LLM_FACTS_ONDEMAND_ENV: on})
        assert isinstance(instance, fsp.OnDemandLlmFacts)
    env = {fsp.LLM_FACTS_ONDEMAND_ENV: "1",
           fsp.FACT_DAILY_TOKEN_BUDGET_ENV: "12345",
           fsp.FACT_TIMEOUT_ENV: "7.5"}
    instance = fsp.build_llm_facts_from_env(env=env)
    assert isinstance(instance, fsp.OnDemandLlmFacts)
    assert instance.budget.daily_tokens == 12345
    assert instance.timeout_s == 7.5


def test_p1_factory_invalid_budget_fail_closed():
    """非法值 fail-closed 钉：开关 ON+DEDUP_FACT_DAILY_TOKEN_BUDGET 非法
    →构造时刻 ValueError（拒装不静默选边——fact_daily_token_budget_from_
    env 同纪律）。"""
    with pytest.raises(ValueError):
        fsp.build_llm_facts_from_env(env={
            fsp.LLM_FACTS_ONDEMAND_ENV: "1",
            fsp.FACT_DAILY_TOKEN_BUDGET_ENV: "abc"})
    with pytest.raises(ValueError):
        fsp.build_llm_facts_from_env(env={
            fsp.LLM_FACTS_ONDEMAND_ENV: "1",
            fsp.FACT_DAILY_TOKEN_BUDGET_ENV: "0"})


# ---------- 注入位 B 预装配（shadow_decide / commit_one 两腿）


def _spy_decide(monkeypatch):
    """包 decide_for_task：记录 llm_facts 实参后照实透传（不改行为）。"""
    captured: dict = {}
    real = decide_service.decide_for_task

    def spy(*args, **kwargs):
        captured["llm_facts"] = kwargs.get("llm_facts", "<absent>")
        return real(*args, **kwargs)

    monkeypatch.setattr(decide_service, "decide_for_task", spy)
    return captured


def test_p1_shadow_decide_off_passes_none(monkeypatch):
    """OFF 等价钉（影子腿）：env 缺席→预装配产出 None→decide_for_task
    收 llm_facts=None（缺省双轨关，现役调用形态逐字节）。"""
    monkeypatch.delenv(fsp.LLM_FACTS_ONDEMAND_ENV, raising=False)
    captured = _spy_decide(monkeypatch)
    history = _history(REL_H)
    current = _current(REL_C)
    out = recall_worker.shadow_decide(
        current, (history,), coverage_complete=True,
        visible_seq=3, prepared_seq=3)
    assert captured["llm_facts"] is None
    assert out.decision == "边界case/疑难case"    # 判官未注入 fail-closed 同役


def test_p1_shadow_decide_on_env_preassembles(monkeypatch):
    """ON 生效钉（影子腿）：开关 ON→预装配产出 OnDemandLlmFacts 单例
    传入 decide_for_task（本腿零真调用——legacy 缺省模式抽取面不触发）。"""
    monkeypatch.setenv(fsp.LLM_FACTS_ONDEMAND_ENV, "1")
    captured = _spy_decide(monkeypatch)
    history = _history(REL_H)
    current = _current(REL_C)
    recall_worker.shadow_decide(
        current, (history,), coverage_complete=True,
        visible_seq=3, prepared_seq=3)
    assert isinstance(captured["llm_facts"], fsp.OnDemandLlmFacts)


def test_p1_shadow_decide_explicit_injection_wins(monkeypatch):
    """显式注入优先钉（A>B）：注入位 A 显式件在场→工厂不参与
    （开关 ON 亦然——同 judge_callable 显式优先同型）。"""
    monkeypatch.setenv(fsp.LLM_FACTS_ONDEMAND_ENV, "1")
    captured = _spy_decide(monkeypatch)
    sentinel = SimpleNamespace(extract=lambda rid, text: None)
    history = _history(REL_H)
    current = _current(REL_C)
    recall_worker.shadow_decide(
        current, (history,), coverage_complete=True,
        visible_seq=3, prepared_seq=3, llm_facts=sentinel)
    assert captured["llm_facts"] is sentinel


def _commit_ctx():
    history = _history(REL_H)
    current = _current(REL_C)
    return CommitContext(
        scope_id="default", business_date=NOW_ISO_DAY, arrival_seq=3,
        current=current, candidates=(history,), visible_seq=10,
        prepared_seq=10, coverage_complete=True)


def test_p1_commit_one_off_passes_none(monkeypatch):
    """OFF 等价钉（生效腿）：env 缺席→commit_one→decide_for_task 收
    llm_facts=None（现役路径逐字节；outcome 形态与基线同构）。"""
    monkeypatch.delenv(fsp.LLM_FACTS_ONDEMAND_ENV, raising=False)
    captured = _spy_decide(monkeypatch)
    store = FakeCommitStore()
    outcome = commit_one(_commit_ctx(), store, audit_complete=True)
    assert captured["llm_facts"] is None
    assert outcome.state == "committed"
    assert outcome.decide_outcome.decision == "边界case/疑难case"


def test_p1_commit_one_on_env_preassembles(monkeypatch):
    """ON 生效钉（生效腿）：开关 ON→预装配 OnDemandLlmFacts 传入
    decide_for_task（零真调用——判官未注入的 fail-closed 边界径）。"""
    monkeypatch.setenv(fsp.LLM_FACTS_ONDEMAND_ENV, "1")
    captured = _spy_decide(monkeypatch)
    store = FakeCommitStore()
    commit_one(_commit_ctx(), store, audit_complete=True)
    assert isinstance(captured["llm_facts"], fsp.OnDemandLlmFacts)


def test_p1_commit_one_explicit_injection_wins(monkeypatch):
    """显式注入优先钉（生效腿）：显式件优先于 env 预装配。"""
    monkeypatch.setenv(fsp.LLM_FACTS_ONDEMAND_ENV, "1")
    captured = _spy_decide(monkeypatch)
    sentinel = SimpleNamespace(extract=lambda rid, text: None)
    store = FakeCommitStore()
    commit_one(_commit_ctx(), store, audit_complete=True,
               llm_facts=sentinel)
    assert captured["llm_facts"] is sentinel


# ---------- 注入位 A（create_host→DedupWorker 单源路由）+ 登记面


def test_p1_create_host_routes_llm_facts_to_worker():
    """A 注入路由钉：create_host(llm_facts=…)→DedupWorker 单源存储
    （影子腿 _shadow_one/生效腿 _live_one 同读 self.llm_facts——两条
    腿同一件）；缺省 None（两腿各自按 env 预装配注入位 B）。"""
    from news_flash_dedup.recall.worker import DedupWorker
    sentinel = SimpleNamespace(extract=lambda rid, text: None)
    assembly = create_host(
        es_client=FakeESClient(), index_prefix="p01-batch-finwire-",
        status_authenticated=lambda request: True, llm_facts=sentinel)
    assert assembly.worker.llm_facts is sentinel
    default_assembly = create_host(
        es_client=FakeESClient(), index_prefix="p01-batch-finwire-",
        status_authenticated=lambda request: True)
    assert default_assembly.worker.llm_facts is None
    assert isinstance(default_assembly.worker, DedupWorker)


def test_p1_worker_legs_pass_self_llm_facts():
    """两腿透传源面钉（只读核对）：worker.py _shadow_one/_live_one 以
    llm_facts=self.llm_facts 透传（shadow_decide/commit_one 调用位）——
    A 注入单源两腿同件的结构保证（与 judge_version_config 同型）。"""
    source = (Path(__file__).parents[2] / "src" / "news_flash_dedup"
              / "recall" / "worker.py").read_text(encoding="utf-8")
    assert source.count("llm_facts=self.llm_facts") == 2    # 影子腿+生效腿


def test_p1_switch_registered_in_known_switches():
    """登记钉：DEDUP_LLM_FACTS_ONDEMAND 入 KNOWN_SWITCHES（单次——
    新增开关须显式登记规则；快照序=表序由 test_p2_run_manifest 守卫）。"""
    assert rm.KNOWN_SWITCHES.count("DEDUP_LLM_FACTS_ONDEMAND") == 1


# ================================================================ 点2：判官并发执行器消费


def _r8_pair_decide(judge):
    """判径对最小配方（R8 形非 R7 域：双写主体→判官双序"重复"直接签发
    JUDGE_EQUIVALENT——test_llm_facts_ondemand 同族文本）。"""
    history = _history(R8_H, subject="甲公司")
    current = _current(R8_C, subject="甲公司")
    return decide_service.decide_for_task(
        history, [], current=current, judge_callable=judge,
        judge_in_chain=True, coverage_complete=True)


def test_p2_executor_off_judge_path_byte_identical(monkeypatch):
    """OFF 等价钉：开关缺席→判官走现役逐对路径——每对恰 2 路调用
    （ab/ba）、判重签发 JUDGE_EQUIVALENT 同构；**旋钮零执法**：OFF 态
    非法 DEDUP_JUDGE_PAIR_CONCURRENCY 不触发任何 ValueError（开关单查
    先行——judge_executor_spec_from_env 的旋钮解析只在 ON 面）。"""
    _semantic_env(monkeypatch)
    monkeypatch.delenv(asm.JUDGE_PAIR_EXECUTOR_ENV, raising=False)
    judge, mock = _judge(_dup_responses((R8_H, R8_C)))
    out = _r8_pair_decide(judge)
    assert out.decision == "重复"
    assert out.internal_code == "JUDGE_EQUIVALENT"
    assert out.duplicate_ids == ("item-A",)
    assert len(mock.calls) == 2                    # 单对双序=2 路
    # OFF+非法旋钮：零执法（现役行为逐字节，开关关=旋钮无感）
    judge2, mock2 = _judge(_dup_responses((R8_H, R8_C)))
    monkeypatch.setenv(asm.JUDGE_PAIR_CONCURRENCY_ENV, "25")   # 越窗
    out2 = _r8_pair_decide(judge2)
    assert out2.decision == "重复"
    assert out2.internal_code == "JUDGE_EQUIVALENT"
    assert len(mock2.calls) == 2


def test_p2_executor_on_same_verdict_call_count_conserved(monkeypatch):
    """ON 生效钉：开关 ON→判径对批量预执行（执行器对池+HTTP 信号量）
    ——**判官调用数守恒**（单对双序=2 路，与 OFF 同数）+判重签发
    JUDGE_EQUIVALENT 同构（预执行结果=adjudicate_pair 同一 validate/
    merge 面消费——语义零漂）。"""
    _semantic_env(monkeypatch)
    monkeypatch.setenv(asm.JUDGE_PAIR_EXECUTOR_ENV, "1")
    monkeypatch.setenv(asm.JUDGE_PAIR_CONCURRENCY_ENV, "4")
    judge, mock = _judge(_dup_responses((R8_H, R8_C)))
    out = _r8_pair_decide(judge)
    assert out.decision == "重复"
    assert out.internal_code == "JUDGE_EQUIVALENT"
    assert out.duplicate_ids == ("item-A",)
    assert len(mock.calls) == 2                    # 调用数守恒（无重跑）


class _SleepyMockLLM(_MockLLM):
    """慢传输罐装（executor 单对硬超时钉用：每序恒睡 delay 秒）。"""

    def __init__(self, responses, delay):
        super().__init__(responses)
        self.delay = delay

    def __call__(self, model, system, user, timeout_s=None):
        time.sleep(self.delay)
        return super().__call__(model, system, user, timeout_s)


def test_p2_executor_on_timeout_fail_closed_unresolved(monkeypatch):
    """ON fail-closed 钉：DEDUP_JUDGE_PAIR_TIMEOUT_S 单对硬超时→执行器
    弃件（结果丢弃）→服务闭包 RuntimeError→JUDGE_EXCEPTION 未决——
    绝不冒签重复/不重复（判定语义零改：未决对进边界，duplicate_ids
    空）。"""
    _semantic_env(monkeypatch)
    monkeypatch.setenv(asm.JUDGE_PAIR_EXECUTOR_ENV, "1")
    monkeypatch.setenv(asm.JUDGE_PAIR_TIMEOUT_ENV, "0.05")     # 50ms 硬超时
    mock = _SleepyMockLLM(_dup_responses((R8_H, R8_C)), delay=0.4)
    judge = judge_adapter.build_judge_callable(
        judge=lr.SyncResidualJudge(
            lr.ResidualJudgeConfig(prompt_version="judge_v6"), call_fn=mock))
    out = _r8_pair_decide(judge)
    assert out.decision == "边界case/疑难case"      # 未决 fail-closed
    assert out.duplicate_ids == ()                  # 不冒签
    # 未决对留痕：detail 携 JUDGE_EXCEPTION 归因（对级码=SUBJECT_UNRESOLVED
    # 现役映射——judge_pair._unresolved 同一落点，语义零漂）。
    assert any(p.outcome == "unresolved"
               and "JUDGE_EXCEPTION" in (p.detail or "")
               for p in out.pair_results)


def test_p2_executor_on_invalid_knob_fail_closed(monkeypatch):
    """非法值 fail-closed 钉：开关 ON+DEDUP_JUDGE_PAIR_CONCURRENCY 越窗
    →decide_for_task 装配时刻 ValueError（P1c spec 解析纪律：非法值
    宁可拒装不可静默选边）。"""
    _semantic_env(monkeypatch)
    monkeypatch.setenv(asm.JUDGE_PAIR_EXECUTOR_ENV, "1")
    monkeypatch.setenv(asm.JUDGE_PAIR_CONCURRENCY_ENV, "25")
    judge, _mock = _judge(_dup_responses((R8_H, R8_C)))
    with pytest.raises(ValueError, match=asm.JUDGE_PAIR_CONCURRENCY_ENV):
        _r8_pair_decide(judge)


# ================================================================ 点3：Embedding 微批写径


class _FakeEmbClient:
    """零网络假 client（读径/写径/护栏/台账最小鸭面——BatchedEmbedding
    Client 装配校验四件全齐：embed_documents/embed_query/usage_snapshot/
    dimension+max_input_chars）。"""

    dimension = 4
    max_input_chars = 6000

    def __init__(self):
        self.embed_documents_calls = 0

    def embed_documents(self, texts):
        self.embed_documents_calls += 1
        return [[0.0, 0.1, 0.2, 0.3] for _ in texts]

    def embed_query(self, text):
        return [0.0, 0.1, 0.2, 0.3]

    def usage_snapshot(self):
        return {"tokens_used": 0, "cache_hits": 0}


def test_p3_wire_off_returns_same_object():
    """OFF 等价钉：DEDUP_EMBEDDING_MICRO_BATCH 缺席/非真值→同一对象直通
    （identity 钉——写径逐字节现役）。

    旋钮执法分立说明（与点2 判官消费点对照）：本接线位=装配面，
    embedding_micro_batch_spec_from_env 解析器无条件执法（P1c 既有钉
    test_embedding_batch_size_window 口径：非法旋钮值装配时刻即 ValueError，
    与开关态无关——拒装不静默选边）；点2 消费位在判定路径内，OFF 必须
    逐字节→开关单查先行。"""
    client = _FakeEmbClient()
    assert asm.wire_embedding_micro_batch(client, env={}) is client
    for off in ("", "0", "banana", "off"):
        assert asm.wire_embedding_micro_batch(client, env={
            asm.EMBEDDING_MICRO_BATCH_ENV: off}) is client


def test_p3_wire_on_wraps_client():
    """ON 生效钉：开关 ON→BatchedEmbeddingClient 鸭型包装（真值集
    strip+lower）；护栏/读径直通面。"""
    client = _FakeEmbClient()
    for on in ("1", "true", "YES", " On "):
        wrapped = asm.wire_embedding_micro_batch(client, env={
            asm.EMBEDDING_MICRO_BATCH_ENV: on})
        assert isinstance(wrapped, asm.BatchedEmbeddingClient)
        assert wrapped.dimension == 4               # 护栏直通
    assert wrapped.embed_query("查询") == [0.0, 0.1, 0.2, 0.3]   # 读径直通


def test_p3_wire_invalid_knob_fail_closed():
    """非法值 fail-closed 钉（装配面无条件执法）：DEDUP_EMBEDDING_BATCH_
    SIZE 越窗→装配时刻 ValueError——**与开关态无关**（P1c 解析器既有
    纪律：非法 env 宁可拒装不可静默选边；对照点2 判定路径内开关单查
    先行的 OFF 逐字节纪律——装配面与判定路径执法分立）。"""
    client = _FakeEmbClient()
    with pytest.raises(ValueError, match=asm.EMBEDDING_BATCH_SIZE_ENV):
        asm.wire_embedding_micro_batch(client, env={
            asm.EMBEDDING_MICRO_BATCH_ENV: "1",
            asm.EMBEDDING_BATCH_SIZE_ENV: "11"})
    with pytest.raises(ValueError, match=asm.EMBEDDING_BATCH_SIZE_ENV):
        asm.wire_embedding_micro_batch(client, env={     # 开关关同拒
            asm.EMBEDDING_BATCH_SIZE_ENV: "11"})
    with pytest.raises(ValueError, match=asm.EMBEDDING_BATCH_MAX_WAIT_MS_ENV):
        asm.wire_embedding_micro_batch(client, env={
            asm.EMBEDDING_MICRO_BATCH_ENV: "1",
            asm.EMBEDDING_BATCH_MAX_WAIT_MS_ENV: "19"})


def test_p3_shadow_harness_wires_micro_batch_at_construction():
    """harness 构造位接线序钉（只读核对，设计单 §四-1 具名消费点）：
    test_emb_shadow.py 真轨组合根=EmbeddingClient 构造→wire_embedding_
    micro_batch 包装→包装件进 RealVectorPipeline（b1_backfill 注入位随
    本组合根一并接线）。"""
    source = (Path(__file__).parent.parent / "integration"
              / "test_emb_shadow.py").read_text(encoding="utf-8")
    construct_at = source.index("client = ec.EmbeddingClient(")
    wire_at = source.index("client = wire_embedding_micro_batch(client")
    pipeline_at = source.index("pipeline = vp.RealVectorPipeline(")
    assert construct_at < wire_at < pipeline_at    # 构造→包装→进 pipeline


# ================================================================ 点4：durable 队列旗标接 env


def _admission_request():
    from test_admission import request
    return request()


def _head_source(store):
    from news_flash_dedup.batch_admission import HEAD_ID
    return store.get("news-dedup-control-v1", HEAD_ID)["source"]


def _accept(coordinator):
    return coordinator.accept_batch([_admission_request()])


def test_p4_coordinator_off_env_legacy_pending_head():
    """OFF 等价钉：env 缺席→use_durable_queue=False→legacy pending 全塞
    路径逐字节（头形态=test_batch_admission 基线同构：pending.batches
    在场，无游标字段——现役受理路径零新行为）。"""
    from news_flash_dedup.batch_admission import BatchLimits
    from test_batch_admission import BatchMemoryStore
    store = BatchMemoryStore()
    coordinator = asm.build_batch_admission_coordinator(
        store, env={}, owner_id="writer-1",
        owner_isolated=lambda: True, limits=BatchLimits(),
        clock=lambda: datetime(2026, 9, 26, 0, 0, tzinfo=timezone.utc))
    receipts = _accept(coordinator)
    assert receipts[0].arrival_seq == 1
    head = _head_source(store)
    assert "pending" in head                       # legacy 形态
    assert len(head["pending"]["batches"]) == 1
    assert "last_terminal" not in head             # 无 durable 游标


def test_p4_coordinator_on_env_durable_cursor_head():
    """ON 生效钉：DEDUP_DURABLE_QUEUE=1→durable 游标头（P1a-T2 形态：
    last_terminal/last_decision_seq/config_version，无 pending 空壳——
    头形态与 test_p1a_t2_durable_admission 基线同构）。"""
    from news_flash_dedup.batch_admission import BatchLimits
    from test_batch_admission import BatchMemoryStore
    store = BatchMemoryStore()
    coordinator = asm.build_batch_admission_coordinator(
        store, env={"DEDUP_DURABLE_QUEUE": "1"}, owner_id="writer-1",
        owner_isolated=lambda: True, limits=BatchLimits(),
        clock=lambda: datetime(2026, 9, 26, 0, 0, tzinfo=timezone.utc))
    _accept(coordinator)
    head = _head_source(store)
    assert "pending" not in head                   # durable 形态
    assert head["last_terminal"] == 0
    assert head["last_decision_seq"] == 0
    assert head["config_version"] == 1


def test_p4_coordinator_explicit_false_wins_over_env_on():
    """显式参优先钉：use_durable_queue=False（测试/回放通道）压过 env
    ON（注入合同：显式注入优先——与点1 llm_facts 同型纪律）。"""
    from news_flash_dedup.batch_admission import BatchLimits
    from test_batch_admission import BatchMemoryStore
    store = BatchMemoryStore()
    coordinator = asm.build_batch_admission_coordinator(
        store, env={"DEDUP_DURABLE_QUEUE": "1"},
        use_durable_queue=False, owner_id="writer-1",
        owner_isolated=lambda: True, limits=BatchLimits(),
        clock=lambda: datetime(2026, 9, 26, 0, 0, tzinfo=timezone.utc))
    _accept(coordinator)
    head = _head_source(store)
    assert "pending" in head                       # 显式 False→legacy


def test_p4_coordinator_invalid_value_fail_closed():
    """非法值 fail-closed 钉：DEDUP_DURABLE_QUEUE 非空非法值→装配时刻
    ValueError（durable_queue_enabled_from_env 单源解析器纪律：
    拒产不静默选边）。"""
    from news_flash_dedup.batch_admission import BatchLimits
    from test_batch_admission import BatchMemoryStore
    with pytest.raises(ValueError, match="DEDUP_DURABLE_QUEUE"):
        asm.build_batch_admission_coordinator(
            BatchMemoryStore(), env={"DEDUP_DURABLE_QUEUE": "banana"},
            owner_id="writer-1", owner_isolated=lambda: True,
            limits=BatchLimits(), clock=lambda: datetime(2026, 9, 26, 0, 0, tzinfo=timezone.utc))


def test_p4_switch_single_registered_no_duplicate():
    """登记钉：DEDUP_DURABLE_QUEUE 既有登记不重复（P1a-T4 已入册——
    点4 只接线不重登；单次计数钉）。"""
    assert rm.KNOWN_SWITCHES.count("DEDUP_DURABLE_QUEUE") == 1
