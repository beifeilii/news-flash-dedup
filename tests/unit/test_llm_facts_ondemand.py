# -*- coding: utf-8 -*-
"""开工令①b（用户令 2026-10-11，LLM 抽取按需化·双轨改造）钉面。

六族（主窗令 5 触发矩阵+缓存键四元）：
- 触发矩阵：无候选（聚合已定）不调用/规则已定对不抽（硬冲突候选零
  调用）/判径对调用（边界调用——双文恰抽一次）/R7 域调用（四要素
  核验吃 LLM 槽放行签）/legacy 模式零调用（机证面 semantic 专属）/
  同任务记忆化（current 跨对零重复抽取）；
- 失败闭门（令4）：LLM 不可得（预算耗尽无缓存/抽取失败）+both_
  missing+判官"重复"+rule 回落证据四要素证成→不降级签重复→边界
  （判别性钉：无闭门则此构造放行签重复）；
- 预算合同（令3）：耗尽停新增（call_fn 零调用零消耗）+已有缓存继续
  用（探针命中不经预算）+告警计数 budget_exhausted_events；
- 缓存键四元（令2）：条目四维身份+遗产条目（无 schema 维）miss
  重抽；
- 默认关：llm_facts=None（缺省）现役语义零翻转（0a0e5de 放行钉
  原样复刻+零 llm_facts 计数）；
- 台账：tokens in/out/latency/cache_hit 逐条+budget 告警计数。

全假件零真 API（call_fn mock）；判径链=真件（真 adapter+真 proof
核验——与 test_p3v6_phase1 同纪律）。
"""

import hashlib
import json

import pytest

from news_flash_dedup.decide import judge_adapter, judge_pair, service as decide_service
from news_flash_dedup.decide import llm_residual as lr
from news_flash_dedup.decide import judge_version_config as jvc
from news_flash_dedup.facts import llm as facts_llm
from news_flash_dedup.recall import fact_supply as fsp

H_ID, C_ID = "a" * 64, "c" * 64

# 双缺主体+谓词"公告"+对象"X地块"+时间"9月24日"+双核心数值（0a0e5de
# 放行钉同族文本——四要素可证形状）
REL_H = "9月24日公告竞得X地块，总价250亿美元，全球市占率69%。"
REL_C = "9月24日公告竞得X地块，耗资250亿美元，全球市占率69%。"
# 规则已定对（核心数值硬冲突——同骨架差数值，VERIFIED_CONFLICT 判官
# 零调用；骨架须全同才同槽位匹配）
NUM_H = "9月24日公告竞得X地块，耗资251亿美元，全球市占率69%。"
# 双方写主体（R8 形——判径内非 R7 域）
R8_H = "甲公司公告：净利由177.9万修正为177.5万。"
R8_C = "甲公司公告净利为177.5万。"


def _fake_counter(text):
    return len(text), "fake_exact"


def _llm_payload(text, *, subject=None, predicate="公告",
                 key_object="X地块", time_expression="9月24日"):
    """P14 抽取器合同形 JSON（谓词/对象/时间逐字摘自原文——引文定位
    合同：非连续子串即丢弃）。"""
    return json.dumps({"facts": [{
        "subject": subject, "predicate": predicate, "polarity": predicate,
        "modality": None, "key_object": key_object,
        "time_expression": time_expression, "time_stage": None,
        "attribution": None, "numerics": [],
    }]}, ensure_ascii=False)


def _ctx(record_id, item_id, text, arrival_seq):
    return {
        "record_id": record_id, "item_id": item_id, "text": text,
        "raw_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "scope_id": "default", "business_date": "2026-09-26",
        "arrival_seq": arrival_seq, "pipeline_version": "dedup_v1",
    }


def _fact(record_id, subject=None, anchor="甲"):
    """最小合法事实夹具（主体槽可缺——pair_alignment 报告前置）。"""
    ev = [{"record_id": record_id, "field": "x",
           "quote": anchor, "start": 0, "end": 1}]
    if subject is None:
        subj = {"status": "missing", "raw_value": None, "evidence": []}
    else:
        subj = {"status": "present", "raw_value": subject, "evidence": ev}
    return {
        "fact_id": "f1", "evidence": ev,
        "fact_type": {"status": "present", "raw_value": "公告", "evidence": ev},
        "subject": subj,
        "event_state": {
            "predicate": {"status": "present", "raw_value": "公告", "evidence": ev},
            "polarity": {"status": "present", "raw_value": "公告", "evidence": ev},
            "modality": {"status": "missing", "raw_value": None, "evidence": []},
            "attribution": {"status": "missing", "raw_value": None, "evidence": []}},
        "time": {"expression": {"status": "missing", "raw_value": None, "evidence": []},
                 "stage": {"status": "missing", "raw_value": None, "evidence": []},
                 "anchor": {"status": "missing", "raw_value": None, "evidence": []}},
        "key_object": {"status": "missing", "raw_value": None, "evidence": []},
        "numerics": [],
    }


def _fact_full(record_id, text):
    """四要素全齐 rule 侧夹具（0a0e5de 同形：谓词+key_object present、
    主体 missing——rule 回落证据可证四要素的判别性构造）。"""
    ev = [{"record_id": record_id, "field": "x",
           "quote": text[0:4], "start": 0, "end": 4}]
    return {
        "fact_id": "f1", "evidence": ev,
        "fact_type": {"status": "present", "raw_value": "公告", "evidence": ev},
        "subject": {"status": "missing", "raw_value": None, "evidence": []},
        "event_state": {
            "predicate": {"status": "present", "raw_value": "公告",
                          "evidence": ev},
            "polarity": {"status": "present", "raw_value": "公告",
                         "evidence": ev},
            "modality": {"status": "missing", "raw_value": None, "evidence": []},
            "attribution": {"status": "missing", "raw_value": None, "evidence": []}},
        "time": {"expression": {"status": "missing", "raw_value": None, "evidence": []},
                 "stage": {"status": "missing", "raw_value": None, "evidence": []},
                 "anchor": {"status": "missing", "raw_value": None, "evidence": []}},
        "key_object": {"status": "present", "raw_value": "X地块",
                       "evidence": ev},
        "numerics": [],
    }


def _history(text, subject=None, facts=None):
    h = _ctx(H_ID, "item-A", text, 1)
    h["facts"] = facts if facts is not None else [_fact(H_ID, subject, text[0])]
    return h


def _current(text, subject=None, facts=None):
    c = _ctx(C_ID, "item-C", text, 3)
    c["facts"] = facts if facts is not None else [_fact(C_ID, subject, text[0])]
    return c


class _MockLLM:
    """按 (文本A, 文本B) 路由罐装判定；零真 API。"""

    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def __call__(self, model, system, user, timeout_s=None):
        self.calls.append(user)
        a = user.split("【文本A】\n", 1)[1].split("\n\n【文本B】\n", 1)[0]
        b = user.split("\n\n【文本B】\n", 1)[1]
        return self.responses[(a, b)], 0.01


def _jjson(decision, reason, ea, eb):
    return json.dumps({
        "decision": decision, "reason": reason,
        "evidence_a": list(ea), "evidence_b": list(eb),
        "numeric_check": {"conclusion": "一致", "numbers_a": [],
                          "numbers_b": []},
        "time_check": {"conclusion": "一致", "times_a": ["9月24日"],
                       "times_b": ["9月24日"]},
    }, ensure_ascii=False)


def _dup_responses(*pairs):
    """罐装判官双序"重复"（判径对全判重）。"""
    responses = {}
    for a, b in pairs:
        responses[(a, b)] = _jjson("重复", "口径一致", ("公告竞得X地块",),
                                   ("公告竞得X地块",))
        responses[(b, a)] = _jjson("重复", "口径一致", ("公告竞得X地块",),
                                   ("公告竞得X地块",))
    return responses


def _semantic_env(monkeypatch):
    monkeypatch.setenv(judge_pair.JUDGE_DECISION_MODE_ENV,
                       judge_pair.MODE_SEMANTIC_AUTHORITY)
    monkeypatch.setenv("DEDUP_JUDGE_PROOF", "1")
    monkeypatch.setenv(jvc.JUDGE_BACKFILL_ENV, "1")
    monkeypatch.delenv("DEDUP_JUDGE_IN_CHAIN", raising=False)


def _legacy_env(monkeypatch):
    monkeypatch.delenv(judge_pair.JUDGE_DECISION_MODE_ENV, raising=False)
    monkeypatch.setenv("DEDUP_JUDGE_PROOF", "1")
    monkeypatch.delenv("DEDUP_JUDGE_IN_CHAIN", raising=False)


def _judge(responses):
    mock = _MockLLM(responses)
    judge = lr.SyncResidualJudge(
        lr.ResidualJudgeConfig(prompt_version="judge_v6"), call_fn=mock)
    return judge_adapter.build_judge_callable(judge=judge), mock


def _extractor(responses=None, *, fail_texts=(), ledger=None, budget=None,
               cache_dir=None, env=None):
    """真件 OnDemandLlmFacts（call_fn mock——全链路真件，零真 API）。

    responses: {text: content}；fail_texts: 抛 LlmExtractionError 的文本。
    返回 (extractor, calls)——calls 记录 (record_id, text) 逐调用。"""
    calls = []

    def call_fn(*, model, text, timeout_s):
        calls.append(text)
        if text in fail_texts:
            raise facts_llm.LlmExtractionError("timeout after retries")
        return responses[text], 0.02

    extractor = fsp.OnDemandLlmFacts(
        call_fn=call_fn, ledger=ledger, budget=budget,
        cache_dir=cache_dir, token_counter=_fake_counter, env=env or {})
    return extractor, calls


def _decide(h, c, judge, llm_facts=None, candidates=()):
    return decide_service.decide_for_task(
        h, list(candidates), current=c, judge_callable=judge,
        judge_in_chain=True, coverage_complete=True,
        llm_facts=llm_facts)


# ============================ 触发矩阵（令1） ============================


def test_no_recall_candidates_unreachable_zero_extraction(monkeypatch):
    """无召回候选不抽（令1①，调用方短路）：生产调用形态=worker/
    coordinator 候选非空才进 decide_for_task（history=顶位召回候选、
    candidates=余位）——零召回候选走调用方短路分支（NO_DUPLICATE/
    边界），decide_for_task 不可达→按需抽取面零发生。钉：monkeypatch
    decide_for_task 为 boom 件，影子腿零候选仍短路返回（不炸）。"""
    from news_flash_dedup.recall import worker as recall_worker
    monkeypatch.setattr(decide_service, "decide_for_task",
                        lambda *a, **k: pytest.fail("零召回候选不得进 decide_for_task"))
    cur = {"item_id": "item-C", "text": REL_C, "raw_hash":
           hashlib.sha256(REL_C.encode("utf-8")).hexdigest()}
    out = recall_worker.shadow_decide(
        cur, (), coverage_complete=True, visible_seq=1, prepared_seq=1)
    assert out.decision == "不重复"          # 首条短路（无历史候选可比对）
    assert out.internal_code == "NO_DUPLICATE_FOUND"


def test_rule_decided_pair_zero_extraction(monkeypatch):
    """规则已定对不抽（令1②）：核心数值硬冲突（同骨架差数值）→
    VERIFIED_CONFLICT 前置拦截（判官零调用）→按需抽取零调用——确定性
    规则已定即不触发 LLM 面。"""
    _semantic_env(monkeypatch)

    def _boom(ctx):
        raise AssertionError("硬冲突对不得进判官")

    extractor, calls = _extractor({})
    h = _history(NUM_H)
    c = _current(REL_C)
    out = _decide(h, c, _boom, llm_facts=extractor)
    assert out.decision == "不重复"
    assert out.internal_code == "VERIFIED_CONFLICT"
    assert calls == []                   # 规则已定：零抽取零判官
    assert extractor.ledger.snapshot()["extractions"] == 0


def test_single_recall_candidate_form_still_extracts(monkeypatch):
    """单召回候选生产形态（令1①+裁量点钉）：candidates=[]+history=顶位
    召回候选（worker.py 候选切分形态 candidates[-1]/[:-1] 的单候选落点）
    ——存在召回候选（history 即是）+首对判径边界→照常抽取双文（无候选
    不抽=零召回候选调用方短路，非本形态）。"""
    _semantic_env(monkeypatch)
    judge, _mock = _judge(_dup_responses((REL_H, REL_C)))
    extractor, calls = _extractor({REL_H: _llm_payload(REL_H),
                                   REL_C: _llm_payload(REL_C)})
    h = _history(REL_H)                  # 顶位召回候选（唯一候选）
    c = _current(REL_C)
    out = _decide(h, c, judge, llm_facts=extractor)   # candidates=[]
    assert sorted(calls) == sorted([REL_H, REL_C])
    assert out.judge_diagnostics.get("judge.llm_facts.pairs") == 1


def test_rule_decided_candidate_not_extracted(monkeypatch):
    """规则已定对不抽（令1）：判径任务含①未决对（REL 双文→判径→抽取
    ）与②数值硬冲突对（NUM 候选→规则层已定→零抽取）——只抽被判文本，
    硬冲突候选文本零调用。"""
    _semantic_env(monkeypatch)
    judge, mock = _judge(_dup_responses((REL_H, REL_C)))
    extractor, calls = _extractor({REL_H: _llm_payload(REL_H),
                                   REL_C: _llm_payload(REL_C)})
    h = _history(REL_H)
    c = _current(REL_C)
    num_cand = _ctx("b" * 64, "item-B", NUM_H, 2)
    num_cand["facts"] = [_fact("b" * 64, None, NUM_H[0])]
    out = _decide(h, c, judge, llm_facts=extractor, candidates=[num_cand])
    # 未决对（REL_H vs REL_C）判径→抽取双文；NUM 候选同骨架差数值→
    # 核心数值硬冲突（规则已定）→判官零调用零抽取
    assert sorted(calls) == sorted([REL_H, REL_C])
    assert NUM_H not in calls
    assert mock.calls                    # 判官只接未决对
    assert out.judge_diagnostics.get("judge.llm_facts.pairs") == 1


def test_boundary_pair_extracts_both_judged_texts(monkeypatch):
    """边界调用（令1②）：判径对（R8 形——双方写主体、规则未决）→抽取
    被判文本恰两条（history 侧+current），先 history 后 current；判官
    判"重复"→非 R7 域零闸→照常签发（JUDGE_EQUIVALENT）。"""
    _semantic_env(monkeypatch)
    judge, _mock = _judge(_dup_responses((R8_H, R8_C)))
    extractor, calls = _extractor(
        {R8_H: _llm_payload(R8_H, subject="甲公司", key_object=None,
                            time_expression=None),
         R8_C: _llm_payload(R8_C, subject="甲公司", predicate="公告",
                            key_object=None, time_expression=None)})
    h = _history(R8_H, subject="甲公司")
    c = _current(R8_C, subject="甲公司")
    out = _decide(h, c, judge, llm_facts=extractor)
    assert out.decision == "重复"
    assert out.internal_code == "JUDGE_EQUIVALENT"
    assert calls == [R8_H, R8_C]         # 被判文本恰双文
    snap = extractor.ledger.snapshot()
    assert snap["extractions"] == 2 and snap["extraction.fallback"] == 0


def test_r7_domain_release_from_llm_facts(monkeypatch):
    """R7 调用（令1③）：both_missing 判径对→抽取双文→四要素核验吃 LLM
    供给槽（谓词"公告"/时间"9月24日"/对象"X地块"）→四项全证→闸放行
    →判官"重复"签发（重复，JUDGE_EQUIVALENT）——LLM 质量线放行面。"""
    _semantic_env(monkeypatch)
    judge, _mock = _judge(_dup_responses((REL_H, REL_C)))
    extractor, calls = _extractor({REL_H: _llm_payload(REL_H),
                                   REL_C: _llm_payload(REL_C)})
    h = _history(REL_H)                  # 主体缺（facts 谓词/对象全缺）
    c = _current(REL_C)
    out = _decide(h, c, judge, llm_facts=extractor)
    assert out.decision == "重复"
    assert out.internal_code == "JUDGE_EQUIVALENT"
    assert sorted(calls) == sorted([REL_H, REL_C])
    assert out.judge_diagnostics.get("judge.llm_facts.pairs") == 1
    # 证据面 unit 对拍：四要素全证（LLM 槽位直供）
    outcome_h = extractor.extract(H_ID, REL_H)
    outcome_c = extractor.extract(C_ID, REL_C)
    from news_flash_dedup.decide import judge_machine_evidence as jme
    ev = jme.build_machine_evidence(
        REL_H, REL_C, history_facts=outcome_h.facts,
        current_facts=outcome_c.facts)
    assert ev["both_missing_alignment"]["all_aligned"] is True
    assert jme.r7_signing_gate(ev)["blockable"] is False
    assert extractor.ledger.snapshot()["extraction.fallback"] == 0


def test_legacy_mode_zero_extraction(monkeypatch):
    """legacy 模式零调用：机证面/按需面 semantic 专属——legacy（默认）
    下判径照常（判官调用），llm_facts 注入亦零抽取（令1 双轨只在
    semantic 面生效）。"""
    _legacy_env(monkeypatch)
    judge, mock = _judge(_dup_responses((REL_H, REL_C)))
    extractor, calls = _extractor({})
    h = _history(REL_H)
    c = _current(REL_C)
    out = _decide(h, c, judge, llm_facts=extractor)
    assert calls == []
    assert mock.calls                    # 判径照常（legacy 判官面不动）
    assert "judge.llm_facts.pairs" not in out.judge_diagnostics


def test_task_level_memoization_current_once(monkeypatch):
    """同任务记忆化：current 跨对共享侧零重复抽取——三未决对（history+
    两候选）恰 4 次抽取（current 只 1 次，非 3 次）。"""
    cand1_text = "9月24日公告竞得X地块，总代价250亿美元，全球市占率69%。"
    _semantic_env(monkeypatch)
    judge, _mock = _judge(_dup_responses(
        (REL_H, REL_C), (cand1_text, REL_C), (REL_C, cand1_text)))
    extractor, calls = _extractor(
        {REL_H: _llm_payload(REL_H), REL_C: _llm_payload(REL_C),
         cand1_text: _llm_payload(cand1_text)})
    h = _history(REL_H)
    c = _current(REL_C)
    cand1 = _ctx("b" * 64, "item-B", cand1_text, 2)
    cand1["facts"] = [_fact("b" * 64, None, cand1_text[0])]
    out = _decide(h, c, judge, llm_facts=extractor, candidates=[cand1])
    assert out.decision == "重复"
    assert len(calls) == 3               # REL_H+cand1+REL_C（current 仅一次）
    assert calls.count(REL_C) == 1
    assert out.judge_diagnostics.get("judge.llm_facts.pairs") == 2


# ============================ 失败闭门（令4） ============================


def _release_facts_docs():
    """判别性构造：docs 带 rule 侧四要素全齐 facts（_fact_full）——LLM
    不可得时 rule 回落证据可证四要素（无闭门则放行签重复）。"""
    return (_history(REL_H, facts=[_fact_full(H_ID, REL_H)]),
            _current(REL_C, facts=[_fact_full(C_ID, REL_C)]))


def test_budget_exhausted_no_cache_forced_boundary(monkeypatch):
    """预算耗尽边界（令3+令4 判别性钉）：both_missing+判官"重复"+rule
    回落证据四要素证成（闸放行）+LLM 不可得（预算耗尽无缓存）→**不
    降级签重复**：撤签强制存疑转边界（JUDGE_UNCERTAIN，detail 可溯
    budget_exceeded）；call_fn 零调用（停新增=诚实零消耗）；告警计数
    budget_exhausted_events；对级 kill 计数入 judge_diagnostics。"""
    _semantic_env(monkeypatch)
    judge, _mock = _judge(_dup_responses((REL_H, REL_C)))
    budget = fsp.FactsDailyTokenBudget(daily_tokens=1)
    budget.charge(1)                     # 当日额度耗尽（无缓存）
    extractor, calls = _extractor({}, budget=budget)
    h, c = _release_facts_docs()
    out = _decide(h, c, judge, llm_facts=extractor)
    assert out.decision == "边界case/疑难case"
    assert out.internal_code == "JUDGE_UNCERTAIN"
    assert "不可降级" in out.reason
    assert "budget_exceeded" in out.reason
    assert calls == []                   # 停新增：预算拒在调用前（零消耗）
    snap = extractor.ledger.snapshot()
    assert snap["budget_exhausted_events"] == 2    # 双文各拒一次
    assert snap["extraction.fallback"] == 2
    assert out.judge_diagnostics.get(
        "judge.backfill.gate.llm_facts_unavailable") == 1
    assert out.judge_diagnostics.get("judge.llm_facts.sides_unavailable") == 2


def test_llm_failure_forced_boundary(monkeypatch):
    """抽取失败闭门（令4）：LLM 抽取失败（超时族）同门撤签→边界
    （detail 可溯 llm_extraction_failed）——回落只补证据不越质量线。"""
    _semantic_env(monkeypatch)
    judge, _mock = _judge(_dup_responses((REL_H, REL_C)))
    extractor, calls = _extractor(
        {REL_H: _llm_payload(REL_H), REL_C: _llm_payload(REL_C)},
        fail_texts=(REL_H, REL_C))
    h, c = _release_facts_docs()
    out = _decide(h, c, judge, llm_facts=extractor)
    assert out.decision == "边界case/疑难case"
    assert out.internal_code == "JUDGE_UNCERTAIN"
    assert "llm_extraction_failed" in out.reason
    assert len(calls) == 2               # 抽取尝试发生（失败如实报不可得）
    assert extractor.ledger.snapshot()["extraction.fallback"] == 2


def test_zero_facts_rejected_forced_boundary(monkeypatch):
    """零事实被拒闭门（令4）：LLM 零可定位事实（fallback fact 标记）=
    不可得→同门撤签→边界（reason 可溯 zero_facts_rejected）。"""
    _semantic_env(monkeypatch)
    judge, _mock = _judge(_dup_responses((REL_H, REL_C)))
    extractor, calls = _extractor(
        {REL_H: '{"facts":[]}', REL_C: '{"facts":[]}'})
    h, c = _release_facts_docs()
    out = _decide(h, c, judge, llm_facts=extractor)
    assert out.decision == "边界case/疑难case"
    assert "zero_facts_rejected" in out.reason


# ============================ 预算合同（令3） ============================


def test_budget_exhausted_cache_still_used(monkeypatch, tmp_path):
    """已有缓存继续用（令3）：首次抽取成功落缓存（预算正常）；第二任务
    预算耗尽+同缓存目录→探针命中→不经预算→LLM facts 可用（source=
    cache）→四要素证成照常放行签重复——缓存面零新调用零消耗。"""
    _semantic_env(monkeypatch)
    cache_dir = str(tmp_path / "facts-cache")
    # 任务一：正常预算→抽取+落缓存→放行
    judge, _mock = _judge(_dup_responses((REL_H, REL_C)))
    budget1 = fsp.FactsDailyTokenBudget(daily_tokens=20_000_000)
    extractor1, calls1 = _extractor(
        {REL_H: _llm_payload(REL_H), REL_C: _llm_payload(REL_C)},
        budget=budget1, cache_dir=cache_dir)
    h, c = _release_facts_docs()
    out1 = _decide(h, c, judge, llm_facts=extractor1)
    assert out1.decision == "重复" and len(calls1) == 2
    # 任务二：预算耗尽+缓存命中→照常用→放行（零新调用）
    budget2 = fsp.FactsDailyTokenBudget(daily_tokens=1)
    budget2.charge(1)
    extractor2, calls2 = _extractor(
        {REL_H: _llm_payload(REL_H), REL_C: _llm_payload(REL_C)},
        budget=budget2, cache_dir=cache_dir)
    out2 = _decide(h, c, judge, llm_facts=extractor2)
    assert out2.decision == "重复"
    assert out2.internal_code == "JUDGE_EQUIVALENT"
    assert calls2 == []                  # 缓存命中零新调用
    snap2 = extractor2.ledger.snapshot()
    assert snap2["cache_hits"] == 2
    assert snap2["budget_exhausted_events"] == 0   # 探针命中不经预算
    assert snap2["tokens_in"] == 0 and snap2["tokens_out"] == 0


# ============================ 缓存键四元（令2） ============================


def test_cache_key_four_tuple(tmp_path):
    """缓存键四元（令2）：条目四维身份（text_sha 文件名+model+
    prompt_version+extractor_schema_version 内容维）；同四元→命中零
    调用；遗产条目（无 schema 维）→键门失配→miss 重抽。"""
    cache_dir = str(tmp_path / "facts-cache")
    budget = fsp.FactsDailyTokenBudget(daily_tokens=20_000_000)
    ledger = fsp.FactExtractionLedger()

    def _make():
        return _extractor({REL_H: _llm_payload(REL_H)}, budget=budget,
                          cache_dir=cache_dir, ledger=ledger)

    extractor1, calls1 = _make()
    outcome1 = extractor1.extract(H_ID, REL_H)
    assert outcome1.available and outcome1.source == "llm"
    assert len(calls1) == 1
    # 条目四维身份在案
    text_hash = hashlib.sha256(REL_H.encode("utf-8")).hexdigest()
    entry = json.loads((tmp_path / "facts-cache" / f"{text_hash}.json")
                       .read_text(encoding="utf-8"))
    assert entry["model"] == facts_llm.DEFAULT_MODEL
    assert entry["prompt_version"] == facts_llm.LLM_PROMPT_VERSION
    assert (entry["extractor_schema_version"]
            == facts_llm.EXTRACTOR_SCHEMA_VERSION)
    assert entry["text_sha256"] == text_hash
    # 同四元→命中（新实例零新调用）
    extractor2, calls2 = _make()
    outcome2 = extractor2.extract(H_ID, REL_H)
    assert outcome2.available and outcome2.source == "cache"
    assert calls2 == []
    # 探针单源：同参 usable / 遗产条目（去 schema 维）miss 重抽
    assert facts_llm.cache_entry_usable(cache_dir, REL_H)
    entry.pop("extractor_schema_version")          # 遗产三原条目形态
    (tmp_path / "facts-cache" / f"{text_hash}.json").write_text(
        json.dumps(entry, ensure_ascii=False), encoding="utf-8")
    assert not facts_llm.cache_entry_usable(cache_dir, REL_H)
    extractor3, calls3 = _make()
    outcome3 = extractor3.extract(H_ID, REL_H)
    assert outcome3.available and outcome3.source == "llm"
    assert len(calls3) == 1          # miss 重抽（不错误复用旧 Fact）
    assert ledger.snapshot()["cache_hits"] == 1


def test_ondemand_extract_facade_contract():
    """协议合同 unit 钉：extract→LlmFactsOutcome 四字段（facts 原样/
    available/source/reason）；不可得时 facts=None+reason 可溯；空文本
    防御面 unavailable。"""
    budget = fsp.FactsDailyTokenBudget(daily_tokens=20_000_000)
    extractor, _calls = _extractor({REL_H: _llm_payload(REL_H)},
                                   budget=budget)
    outcome = extractor.extract(H_ID, REL_H)
    assert outcome._fields == ("facts", "available", "source", "reason")
    assert outcome.available is True
    assert outcome.source == "llm"
    assert outcome.reason is None
    assert outcome.facts[0]["event_state"]["predicate"]["raw_value"] == "公告"
    empty = extractor.extract(H_ID, "")
    assert empty.available is False and empty.source == "unavailable"


def test_ondemand_ledger_accounts_tokens_and_budget_alarm():
    """台账面：成功逐条 tokens in/out（fake 计数 in=system+text、out=
    content）/latency/cache_hit；预算拒→budget_exhausted_events+
    unavailable（零 token 账）。"""
    ledger = fsp.FactExtractionLedger()
    budget = fsp.FactsDailyTokenBudget(daily_tokens=1)
    budget.charge(1)
    extractor, _calls = _extractor({REL_H: _llm_payload(REL_H)},
                                   ledger=ledger, budget=budget)
    outcome = extractor.extract(H_ID, REL_H)
    assert outcome.available is False
    assert "budget_exceeded" in outcome.reason
    snap = ledger.snapshot()
    assert snap["budget_exhausted_events"] == 1
    assert snap["tokens_in"] == 0 and snap["tokens_out"] == 0
    entry = ledger.entries[0]
    assert entry["fallback"] is True and entry["fallback_reason"] == outcome.reason


# ============================ 默认关（双轨 off） ============================


def test_llm_facts_none_semantics_unchanged(monkeypatch):
    """默认关零翻转：llm_facts=None（缺省不传）→0a0e5de 放行钉原样
    （docs rule 侧四要素证成→闸放行→签重复）+judge_diagnostics 零
    llm_facts 计数（现役语义逐字节——令6 判定口径/公共输出禁碰面）。"""
    _semantic_env(monkeypatch)
    judge, _mock = _judge(_dup_responses((REL_H, REL_C)))
    h, c = _release_facts_docs()
    out = _decide(h, c, judge)          # llm_facts 缺省 None
    assert out.decision == "重复"
    assert out.internal_code == "JUDGE_EQUIVALENT"
    assert "llm_facts" not in json.dumps(out.judge_diagnostics)
