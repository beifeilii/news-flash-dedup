# -*- coding: utf-8 -*-
"""LLM 事实供给接进服务主链（用户令 2026-10-11 主窗拍板）单元钉。

四族+集成（主窗令 4）：
- 供给选择族：DEDUP_FACT_SUPPLY=rule|llm（缺席/空=rule 测试密封默认；
  非法 ValueError fail-closed 同 decision_mode 纪律）+超时/日闸 env 解析
  +resolve_fact_supply 工厂（T 接线替换口）；
- 降级族（令 2 fail-closed 方向）：单文 LLM 失败（异常/零事实被拒/日
  预算拒）→该文回落 rule 供给结果（不链级失败）+extraction.fallback
  计数+落回原因入台账；
- 计数/台账族（令 3）：逐条记 model/tokens in/out/延迟/缓存命中（含
  jsonl 侧车）+日 token 闸计账（缓存命中零 charge）；
- 集成族：mock 抽取器进 decide_for_task 链——R7 双方缺主体四要素
  确定性核验吃到 LLM 供给的谓词/时间/对象槽（jme.build_machine_
  evidence+r7_signing_gate+对级放行签）。

裁量点钉（主窗 2026-10-11 逐项裁定）：A 超时 30s/重试 2 沿用现役+env
可配；B 缺省串行逐文；C 供给/判官天然先后串行两闸独立；D 日 token 闸
默认 20M（EMB 部署配置档值族）；E qwen_bpe 精确+字节兜底；F
llm_empty_facts_fallback 标记判零事实；G 落回原因走供给层台账；H
manifest env 单源（test_p2_run_manifest.py 同批钉）。
"""

import json
import os

import pytest

from news_flash_dedup.facts import llm as facts_llm
from news_flash_dedup.facts import rule as facts_rule
from news_flash_dedup.recall import service as recall_service
from news_flash_dedup.recall import fact_supply as fsp

RID = "a" * 64

# 供给/集成共用文本（与 test_p3v6_phase1 REL_H/REL_C 同族：双缺主体+
# 谓词"公告"+对象"X地块"+时间"9月24日"+两核心数值 250亿/69%）
REL_H = "9月24日公告竞得X地块，总价250亿美元，全球市占率69%。"
REL_C = "9月24日公告竞得X地块，耗资250亿美元，全球市占率69%。"


def _fake_counter(text):
    """确定性 token 计数（E 项口径钉：注入替换口——生产=qwen_bpe 精确）。"""
    return len(text), "fake_exact"


def _llm_payload(text, *, predicate="公告", key_object="X地块",
                 time_expression="9月24日", subject=None):
    """P14 抽取器合同形 JSON（谓词/对象/时间逐字摘自原文——引文定位
    合同：非连续子串即丢弃）。"""
    return json.dumps({"facts": [{
        "subject": subject, "predicate": predicate, "polarity": predicate,
        "modality": None, "key_object": key_object,
        "time_expression": time_expression, "time_stage": None,
        "attribution": None,
        "numerics": [],
    }]}, ensure_ascii=False)


def _make_supply(responses, *, ledger=None, budget=None, cache_dir=None,
                 timeout_s=5.0, **kwargs):
    """responses: {text: (content, latency)}——单文一策。"""
    calls = []

    def call_fn(*, model, text, timeout_s):
        calls.append(text)
        content, latency = responses[text]
        return content, latency

    supply = fsp.LlmFactSupply(
        call_fn=call_fn, ledger=ledger, budget=budget,
        cache_dir=cache_dir, timeout_s=timeout_s,
        token_counter=_fake_counter, **kwargs)
    return supply, calls


# ================================ 供给选择族 ================================


@pytest.mark.parametrize("raw,expected", [
    ("", "rule"), ("   ", "rule"), ("rule", "rule"), ("llm", "llm"),
    (" llm ", "llm"),          # strip 容错（decision_mode 纪律同款）
])
def test_mode_from_env_valid(raw, expected):
    assert fsp.fact_supply_mode_from_env({"DEDUP_FACT_SUPPLY": raw}) == expected


def test_mode_from_env_absent_defaults_rule():
    assert fsp.fact_supply_mode_from_env({}) == "rule"
    assert fsp.fact_supply_mode_from_env(None) == "rule"   # 缺省读 os.environ
    # 密封性：宿主进程 env 有值也如实解析（单源=env 本身）
    assert fsp.fact_supply_mode_from_env(
        {"DEDUP_FACT_SUPPLY": "llm"}) == "llm"


@pytest.mark.parametrize("raw", ["Rule", "LLM", "identity", "auto", "1"])
def test_mode_from_env_invalid_fail_closed(raw):
    """非法值 ValueError fail-closed（同 decision_mode 纪律：宁可拒产
    不可静默选边——绝不把非法值当缺席处理）。"""
    with pytest.raises(fsp.FactSupplyModeInvalid):
        fsp.fact_supply_mode_from_env({"DEDUP_FACT_SUPPLY": raw})


def test_timeout_from_env_resolution():
    assert fsp.fact_timeout_from_env({}) == 30.0          # A 项：现役常量
    assert fsp.fact_timeout_from_env({"DEDUP_FACT_TIMEOUT_S": "12.5"}) == 12.5
    with pytest.raises(ValueError):
        fsp.fact_timeout_from_env({"DEDUP_FACT_TIMEOUT_S": "abc"})
    with pytest.raises(ValueError):
        fsp.fact_timeout_from_env({"DEDUP_FACT_TIMEOUT_S": "0"})
    with pytest.raises(ValueError):
        fsp.fact_timeout_from_env({"DEDUP_FACT_TIMEOUT_S": "-3"})


def test_daily_token_budget_env_resolution():
    assert (fsp.fact_daily_token_budget_from_env({})
            == fsp.DEFAULT_FACT_DAILY_TOKEN_BUDGET)
    assert fsp.DEFAULT_FACT_DAILY_TOKEN_BUDGET == 20_000_000   # D 项钉
    assert fsp.fact_daily_token_budget_from_env(
        {"DEDUP_FACT_DAILY_TOKEN_BUDGET": "1000"}) == 1000
    with pytest.raises(ValueError):
        fsp.fact_daily_token_budget_from_env(
            {"DEDUP_FACT_DAILY_TOKEN_BUDGET": "x"})
    with pytest.raises(ValueError):
        fsp.fact_daily_token_budget_from_env(
            {"DEDUP_FACT_DAILY_TOKEN_BUDGET": "0"})


def test_resolve_fact_supply_factory_dispatch():
    """T 接线替换口（令 5）：rule→RuleFactSupply（T 现役逐字节等价）、
    llm→LlmFactSupply；F3 装配缺省（不调工厂）恒 Identity——另行钉。"""
    rule_supply = fsp.resolve_fact_supply({"DEDUP_FACT_SUPPLY": "rule"})
    assert isinstance(rule_supply, recall_service.RuleFactSupply)
    llm_supply = fsp.resolve_fact_supply({"DEDUP_FACT_SUPPLY": "llm"})
    assert isinstance(llm_supply, fsp.LlmFactSupply)
    assert isinstance(fsp.resolve_fact_supply({}), recall_service.RuleFactSupply)
    with pytest.raises(fsp.FactSupplyModeInvalid):
        fsp.resolve_fact_supply({"DEDUP_FACT_SUPPLY": "nope"})


def test_default_supply_identity_untouched():
    """F3 装配缺省零翻转：RecallService 不注入 fact_supply 恒
    IdentityFactSupply（21:4x 主窗口裁定形态）。"""
    svc = recall_service.RecallService(None, "p01-batch-x-")
    assert isinstance(svc.fact_supply, recall_service.IdentityFactSupply)


# ================================ 降级族（令 2） ================================


def test_fallback_on_llm_exception_returns_rule_facts():
    """超时/API 失败族（LlmExtractionError）→该文回落 rule 结果（不链
    级失败）+extraction.fallback 计数+原因可溯。"""
    ledger = fsp.FactExtractionLedger()

    def boom(*, model, text, timeout_s):
        raise facts_llm.LlmExtractionError("timeout after retries")

    supply = fsp.LlmFactSupply(call_fn=boom, ledger=ledger,
                               token_counter=_fake_counter)
    got = supply(RID, REL_H)
    assert got == recall_service.RuleFactSupply()(RID, REL_H)  # 逐字节 rule
    snap = ledger.snapshot()
    assert snap["extraction.fallback"] == 1
    assert snap["llm_failures"] == 1
    assert snap["llm_successes"] == 0
    assert ledger.entries[0]["fallback_reason"].startswith(
        "llm_extraction_failed: LlmExtractionError")


def test_fallback_on_zero_facts_rejected():
    """F 项：零可定位事实被拒（issues 含 llm_empty_facts_fallback）→
    回落 rule——不直通全槽 missing 的语义空 fallback fact。"""
    ledger = fsp.FactExtractionLedger()
    supply, _ = _make_supply({REL_H: ('{"facts":[]}', 0.01)}, ledger=ledger)
    got = supply(RID, REL_H)
    assert got == recall_service.RuleFactSupply()(RID, REL_H)
    assert ledger.entries[0]["fallback_reason"] == "zero_facts_rejected"
    assert ledger.snapshot()["extraction.fallback"] == 1


def test_fallback_on_budget_exceeded():
    """D 项：日 token 闸越限 → 本日新调用拒启（fail-closed：回落 rule
    不旁路）——check_open 拒在调用前（token 零消耗）。"""
    ledger = fsp.FactExtractionLedger()
    budget = fsp.FactsDailyTokenBudget(daily_tokens=10)
    budget.charge(10)                       # 当日额度耗尽
    supply, calls = _make_supply({REL_H: (_llm_payload(REL_H), 0.01)},
                                 ledger=ledger, budget=budget)
    got = supply(RID, REL_H)
    assert got == recall_service.RuleFactSupply()(RID, REL_H)
    assert calls == []                      # 拒启新调用（诚实零消耗）
    assert "budget_exceeded" in ledger.entries[0]["fallback_reason"]
    assert budget.snapshot()["used_tokens"] == 10   # 账不旁路


def test_fallback_on_oserror():
    """OSError 族（网络层）同降级纪律。"""
    ledger = fsp.FactExtractionLedger()

    def network_boom(*, model, text, timeout_s):
        raise OSError("connection reset")

    supply = fsp.LlmFactSupply(call_fn=network_boom, ledger=ledger)
    got = supply(RID, REL_H)
    assert got == recall_service.RuleFactSupply()(RID, REL_H)
    assert "OSError" in ledger.entries[0]["fallback_reason"]


def test_success_path_returns_llm_facts_not_rule():
    """成功路径：LLM facts 原样直通（非 rule 回落）——谓词槽=LLM 供给
    原文引文（集成族进一步对闸四要素）。"""
    ledger = fsp.FactExtractionLedger()
    supply, _ = _make_supply({REL_H: (_llm_payload(REL_H), 0.02)},
                             ledger=ledger)
    facts = supply(RID, REL_H)
    rule_facts = recall_service.RuleFactSupply()(RID, REL_H)
    assert facts != rule_facts               # 语义面确证不同供给
    assert facts[0]["event_state"]["predicate"]["raw_value"] == "公告"
    assert facts[0]["time"]["expression"]["raw_value"] == "9月24日"
    assert facts[0]["key_object"]["raw_value"] == "X地块"
    assert facts[0]["subject"]["status"] == "missing"
    snap = ledger.snapshot()
    assert snap["extraction.fallback"] == 0
    assert snap["llm_successes"] == 1


# ================================ 计数/台账族（令 3） ================================


def test_ledger_records_tokens_model_latency_cache_hit():
    """逐条记 model/tokens in/out/延迟/缓存命中（E 项注入计数器——
    tokens_in=system+user，tokens_out=content）。"""
    ledger = fsp.FactExtractionLedger()
    supply, _ = _make_supply({REL_H: (_llm_payload(REL_H), 0.03)},
                             ledger=ledger)
    supply(RID, REL_H)
    entry = ledger.entries[0]
    assert set(entry) == {"ts", "record_id", "text_sha256", "model",
                          "tokens_in", "tokens_out", "latency_s",
                          "cache_hit", "fallback", "fallback_reason",
                          "n_facts"}
    assert entry["model"] == facts_llm.DEFAULT_MODEL
    assert entry["record_id"] == RID
    assert entry["latency_s"] == 0.03
    assert entry["cache_hit"] is False
    assert entry["fallback"] is False
    assert entry["fallback_reason"] is None
    assert entry["n_facts"] == 1
    # fake 计数口径：in=system 提示词+text，out=content
    assert entry["tokens_in"] == (len(facts_llm._SYSTEM_PROMPT)
                                  + len(REL_H))
    assert entry["tokens_out"] == len(_llm_payload(REL_H))
    snap = ledger.snapshot()
    assert snap["tokens_in"] == entry["tokens_in"]
    assert snap["tokens_out"] == entry["tokens_out"]
    assert snap["extractions"] == 1
    assert snap["llm_calls"] == 1


def test_ledger_jsonl_sidecar(tmp_path):
    """jsonl 侧车：逐行 JSON（键集=台账条目键集；审计面，崩溃至多丢
    末行——G 项）。"""
    path = tmp_path / "facts-usage.jsonl"
    ledger = fsp.FactExtractionLedger(jsonl_path=path)
    supply, _ = _make_supply({REL_H: (_llm_payload(REL_H), 0.01)},
                             ledger=ledger)
    supply(RID, REL_H)
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0]) == ledger.entries[0]


def test_budget_charge_on_success_and_cache_hit_free(tmp_path):
    """日闸计账：成功非缓存 charge(tokens in+out)；缓存命中零 charge
    （零 token 消耗——EMB 缓存纪律同构）。"""
    cache_dir = tmp_path / "facts-cache"
    ledger = fsp.FactExtractionLedger()
    budget = fsp.FactsDailyTokenBudget(daily_tokens=10_000_000)
    supply, calls = _make_supply({REL_H: (_llm_payload(REL_H), 0.01)},
                                 ledger=ledger, budget=budget,
                                 cache_dir=cache_dir)
    supply(RID, REL_H)
    used_after_miss = budget.snapshot()["used_tokens"]
    assert used_after_miss == (len(facts_llm._SYSTEM_PROMPT) + len(REL_H)
                               + len(_llm_payload(REL_H)))
    # 二次同文（新实例、同 cache_dir）：命中缓存——零调用零 charge
    supply2, calls2 = _make_supply({REL_H: (_llm_payload(REL_H), 0.01)},
                                   ledger=ledger, budget=budget,
                                   cache_dir=cache_dir)
    facts2 = supply2(RID, REL_H)
    assert calls2 == []                      # 无新 LLM 调用
    assert facts2[0]["event_state"]["predicate"]["raw_value"] == "公告"
    assert budget.snapshot()["used_tokens"] == used_after_miss  # 零 charge
    snap = ledger.snapshot()
    assert snap["cache_hits"] == 1
    hit_entry = ledger.entries[-1]
    assert hit_entry["cache_hit"] is True
    assert hit_entry["tokens_in"] == 0 and hit_entry["tokens_out"] == 0


def test_budget_daily_rollover_and_persistence(tmp_path):
    """UTC+8 业务日翻转重置+计数器落盘（EMB R14 同构：tmp+replace 原子
    、重启加载）。"""
    import datetime as dt
    path = tmp_path / "facts-budget.json"
    day1 = dt.datetime(2026, 10, 11, 20, 0, tzinfo=dt.timezone.utc)
    budget = fsp.FactsDailyTokenBudget(
        daily_tokens=100, store_path=path, clock=lambda: day1)
    budget.charge(40)
    assert budget.snapshot()["used_tokens"] == 40
    # 落盘在案（业务日=UTC+8 → 2026-10-12）
    body = json.loads(path.read_text(encoding="utf-8"))
    assert body["business_date"] == "2026-10-12"
    assert body["used_tokens"] == 40
    # 翻日（clock 前进）：重置；新实例加载旧值同语义
    day2 = dt.datetime(2026, 10, 12, 10, 0, tzinfo=dt.timezone.utc)
    budget2 = fsp.FactsDailyTokenBudget(
        daily_tokens=100, store_path=path, clock=lambda: day2)
    assert budget2.snapshot()["used_tokens"] == 40   # 重启加载（同业务日）
    day3 = dt.datetime(2026, 10, 13, 1, 0, tzinfo=dt.timezone.utc)
    budget3 = fsp.FactsDailyTokenBudget(
        daily_tokens=100, store_path=path, clock=lambda: day3)
    assert budget3.snapshot()["used_tokens"] == 0    # 业务日翻转重置
    budget3.charge(100)
    with pytest.raises(fsp.FactsBudgetExceeded):
        budget3.check_open()


# ================================ 集成族（令 4） ================================


def _jjson(decision, reason, ea, eb):
    return json.dumps({
        "decision": decision, "reason": reason,
        "evidence_a": list(ea), "evidence_b": list(eb),
        "numeric_check": {"conclusion": "一致", "numbers_a": [],
                          "numbers_b": []},
        "time_check": {"conclusion": "一致", "times_a": ["9月24日"],
                       "times_b": ["9月24日"]},
    }, ensure_ascii=False)


def test_gate_four_item_verification_consumes_llm_supply(monkeypatch, tmp_path):
    """主窗令 4 集成钉：mock 抽取器进 decide_for_task 链——R7 双方缺
    主体四要素确定性核验**吃到 LLM 供给的谓词/时间/对象槽**：both_
    missing_alignment 四项全证→r7_signing_gate 不可拦→判官判"重复"
    放行签（JUDGE_EQUIVALENT，与 test_p3v6_phase1 放行钉同口径）。"""
    from news_flash_dedup.decide import judge_adapter, judge_pair, service as decide_service
    from news_flash_dedup.decide import llm_residual as lr
    from news_flash_dedup.decide import judge_machine_evidence as jme

    monkeypatch.setenv("DEDUP_JUDGE_DECISION_MODE", "semantic_authority")
    monkeypatch.setenv("DEDUP_JUDGE_PROOF", "1")
    monkeypatch.delenv("DEDUP_JUDGE_IN_CHAIN", raising=False)

    ledger = fsp.FactExtractionLedger()
    supply, _ = _make_supply(
        {REL_H: (_llm_payload(REL_H), 0.01),
         REL_C: (_llm_payload(REL_C), 0.01)},
        ledger=ledger)
    h_facts = supply(RID, REL_H)              # LLM 供给（双缺主体）
    c_facts = supply("c" * 64, REL_C)

    # unit 面：四要素核验吃 LLM 供给槽位
    ev = jme.build_machine_evidence(
        REL_H, REL_C, history_facts=h_facts, current_facts=c_facts)
    alignment = ev["both_missing_alignment"]
    assert alignment["event"]["aligned"] is True       # 谓词槽双侧"公告"
    assert alignment["event"]["history_predicates"] == ["公告"]
    assert alignment["time"]["aligned"] is True        # 时间槽"9月24日"
    assert alignment["object"]["aligned"] is True      # 对象槽"X地块"
    assert alignment["values"]["aligned"] is True      # 250亿+69%（文本侧锚）
    assert alignment["all_aligned"] is True
    assert jme.r7_signing_gate(ev) == {
        "blockable": False, "rule": "", "failed_items": ()}

    # 对级面：decide_for_task 放行签（判官 mock 双序"重复"）
    responses = {
        (REL_H, REL_C): _jjson("重复", "双方缺主体但四要素全一致",
                               ("公告竞得X地块",), ("公告竞得X地块",)),
        (REL_C, REL_H): _jjson("重复", "双方缺主体但四要素全一致",
                               ("公告竞得X地块",), ("公告竞得X地块",)),
    }

    class _MockLLM:
        def __init__(self):
            self.calls = []
            self.systems = []

        def __call__(self, model, system, user, timeout_s=None):
            self.calls.append(user)
            self.systems.append(system)
            a = user.split("【文本A】\n", 1)[1].split("\n\n【文本B】\n", 1)[0]
            b = user.split("\n\n【文本B】\n", 1)[1]
            return responses[(a, b)], 0.01

    mock = _MockLLM()
    judge = lr.SyncResidualJudge(
        lr.ResidualJudgeConfig(prompt_version="judge_v6"), call_fn=mock)
    cb = judge_adapter.build_judge_callable(judge=judge)
    ctxs = []

    def spy(pair_context):
        ctxs.append(pair_context)
        return cb(pair_context)

    import hashlib
    def _doc(record_id, item_id, text, seq, facts):
        return {"record_id": record_id, "item_id": item_id, "text": text,
                "raw_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "scope_id": "default", "business_date": "2026-09-26",
                "arrival_seq": seq, "pipeline_version": "dedup_v1",
                "facts": facts}

    h = _doc(RID, "item-A", REL_H, 1, h_facts)
    c = _doc("c" * 64, "item-C", REL_C, 3, c_facts)
    out = decide_service.decide_for_task(
        h, [], current=c, judge_callable=spy,
        judge_in_chain=True, coverage_complete=True)
    assert out.decision == "重复"
    assert out.internal_code == "JUDGE_EQUIVALENT"
    assert out.reason == judge_pair.JUDGE_DUPLICATE_REASON
    assert len(mock.calls) == 2
    for ctx in ctxs:
        assert "machine_evidence" not in ctx       # 判官输入零机器证据
    # 供给台账：双文 LLM 直通零回落
    snap = ledger.snapshot()
    assert snap["extraction.fallback"] == 0
    assert snap["llm_calls"] == 2 and snap["llm_successes"] == 2


def test_gate_blockable_when_llm_supply_object_mismatch(monkeypatch):
    """集成反钉：LLM 供给对象槽不一致（X地块 vs Y地块）→四要素核验
    object 未证成→闸可拦（failed_items=("object",)）——供给面确实进入
    核验判据（非恒放行）。"""
    from news_flash_dedup.decide import judge_machine_evidence as jme

    OBJ_C = "9月24日公告竞得Y地块，耗资250亿美元，全球市占率69%。"
    monkeypatch.setenv("DEDUP_JUDGE_DECISION_MODE", "semantic_authority")
    monkeypatch.setenv("DEDUP_JUDGE_PROOF", "1")
    ledger = fsp.FactExtractionLedger()
    supply, _ = _make_supply(
        {REL_H: (_llm_payload(REL_H), 0.01),
         OBJ_C: (_llm_payload(OBJ_C, key_object="Y地块"), 0.01)},
        ledger=ledger)
    h_facts = supply(RID, REL_H)
    c_facts = supply("c" * 64, OBJ_C)
    ev = jme.build_machine_evidence(
        REL_H, OBJ_C, history_facts=h_facts, current_facts=c_facts)
    alignment = ev["both_missing_alignment"]
    assert alignment["object"]["aligned"] is False
    assert alignment["object"]["history_key_objects"] == ["X地块"]
    assert alignment["object"]["current_key_objects"] == ["Y地块"]
    gate = jme.r7_signing_gate(ev)
    assert gate["blockable"] is True
    assert gate["rule"] == "both_missing_subject"
    assert gate["failed_items"] == ("object",)
