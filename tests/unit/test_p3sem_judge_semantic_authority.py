# -*- coding: utf-8 -*-
"""提交一：解除证明层一票否决（语义权威改造 1/3）验收测试。

方案：log/快讯去重_判官与证明层改造_可执行技术方案.md §4.2/§4.3/§5.1
（2026-10-10，分支 p3-semantic-authority）。

钉值面 = 方案 §5.1 末尾 8 条必增测试（逐条对应，见各用例 docstring）：
1. 双序重复 + P_OFFSET → 最终重复，同时有证据告警；
2. 双序重复 + P_SUBJECT_MISSING → 不由证明层降级，最终取决于判官双序；
3. 双序重复 + P_REL_TIME → 不由证明层降级；
4. 双序不重复 + 无 falsification → 最终不重复，同时记录 P_NO_AXIS；
5. 文本 SHA/order/item 绑定错误 → 仍然边界（合同级硬失败保留）；
6. 双序分歧、调用超时、JSON 非法 → 仍然边界；
7. 规则链已有 VerifiedConflict → 跳过判官且保持不重复；
8. 引文全部失效 → 完整原文回退证据，审计记录 quote/start/end 可回指。

主窗补强钉（每条适用处断言）：
- 完整原文回退证据只进内部审计/诊断，绝不进公共五字段
  （item_id/text/decision/duplicate_ids/reason）；
- 公共 reason 不含内部码（含新三码 JUDGE_EQUIVALENT/JUDGE_NON_DUPLICATE/
  JUDGE_UNCERTAIN）、不含模型原始响应/URL/超长原文。

夹具纪律：真件路=真 adapter+真 judge_proof+罐装 LLM（test_p1int 同型）；
double 路=合同合规假证明（test_p1b 同型）。
"""

from __future__ import annotations

import hashlib
import json
import time
from types import SimpleNamespace

import pytest

from news_flash_dedup.commit.coordinator import (
    CommitContext,
    build_commit_write_plan,
)
from news_flash_dedup.compare.pair_compare import UNRESOLVED_CODES
from news_flash_dedup.decide import judge_adapter, judge_pair
from news_flash_dedup.decide import judge_proof as jp
from news_flash_dedup.decide import llm_residual as lr
from news_flash_dedup.decide import service as decide_service
from news_flash_dedup.facts import rule as facts_rule


# ---------------------------------------------------------------- 夹具（P1-b/P1-int 同形）

def _ctx(record_id, item_id, text, arrival_seq, **kw):
    return {
        "record_id": record_id, "item_id": item_id, "text": text,
        "raw_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "scope_id": "default", "business_date": "2026-09-26",
        "arrival_seq": arrival_seq, "pipeline_version": "dedup_v1",
        **kw,
    }


def _fact(record_id, subject):
    """最小合法事实夹具（test_p1b 同形；文本首字=subject[0]，证据 [0,1)）。"""
    ev = [{"record_id": record_id, "field": "x",
           "quote": subject[0], "start": 0, "end": 1}]
    return {
        "fact_id": "f1", "evidence": ev,
        "fact_type": {"status": "present", "raw_value": "回购", "evidence": ev},
        "subject": {"status": "present", "raw_value": subject, "evidence": ev},
        "event_state": {
            "predicate": {"status": "present", "raw_value": "回购", "evidence": ev},
            "polarity": {"status": "present", "raw_value": "回购", "evidence": ev},
            "modality": {"status": "missing", "raw_value": None, "evidence": []},
            "attribution": {"status": "missing", "raw_value": None, "evidence": []}},
        "time": {"expression": {"status": "missing", "raw_value": None, "evidence": []},
                 "stage": {"status": "missing", "raw_value": None, "evidence": []},
                 "anchor": {"status": "missing", "raw_value": None, "evidence": []}},
        "key_object": {"status": "missing", "raw_value": None, "evidence": []},
        "numerics": [],
    }


H_ID, C_ID = "a" * 64, "c" * 64


def _history(text, subject):
    h = _ctx(H_ID, "item-A", text, 1)
    h["facts"] = [_fact(H_ID, subject)]
    return h


def _current(text, subject):
    c = _ctx(C_ID, "item-C", text, 3)
    c["facts"] = [_fact(C_ID, subject)]
    return c


# ---------------------------------------------------------------- 真件（LLM 边界罐装，test_p1int 同型）

class _MockLLM:
    def __init__(self, responses, sleep_s=0.0):
        self.responses = responses
        self.sleep_s = sleep_s
        self.calls = []

    def __call__(self, model, system, user, timeout_s=None):
        self.calls.append(user)
        if self.sleep_s:
            time.sleep(self.sleep_s)
        a = user.split("【文本A】\n", 1)[1].split("\n\n【文本B】\n", 1)[0]
        b = user.split("\n\n【文本B】\n", 1)[1]
        return self.responses[(a, b)], 0.01


def _jjson(decision, ea, eb, *, na=(), nb=(), ta=(), tb=(),
           ncon="一致", tcon="一致"):
    return json.dumps({
        "decision": decision, "reason": "r",
        "evidence_a": list(ea), "evidence_b": list(eb),
        "numeric_check": {"conclusion": ncon, "numbers_a": list(na),
                          "numbers_b": list(nb)},
        "time_check": {"conclusion": tcon, "times_a": list(ta),
                       "times_b": list(tb)},
    }, ensure_ascii=False)


@pytest.fixture(autouse=True)
def _semantic_decision_mode(monkeypatch):
    """提交三（§5.3 灰度）：本文件钉的是判官语义权威口径（提交一八条
    验收），灰度开关显式置 semantic_authority；legacy_proof_gate 默认
    口径与新旧对照由 test_p3c_decision_mode.py 专项守卫。"""
    monkeypatch.setenv(judge_pair.JUDGE_DECISION_MODE_ENV,
                       judge_pair.MODE_SEMANTIC_AUTHORITY)


def _switches_on(monkeypatch):
    monkeypatch.setenv(jp.JUDGE_PROOF_ENV, "1")
    monkeypatch.setenv(judge_pair.JUDGE_IN_CHAIN_ENV, "1")


def _real_callable(responses, *, sleep_s=0.0):
    cfg = lr.ResidualJudgeConfig(mv_mode=lr.MV_AUDIT)
    judge = lr.SyncResidualJudge(
        cfg, call_fn=_MockLLM(responses, sleep_s=sleep_s))
    cb = judge_adapter.build_judge_callable(judge=judge)
    assert cb is not None
    return cb


def _adjudicate_direct(cb, h_text, c_text):
    """直跑判官适配层（诊断面断言）：返 JudgePairOutcome。"""
    pair = SimpleNamespace(
        pair_id="pair-p3sem-direct", history_record_id=H_ID,
        current_record_id=C_ID, history_item_id="item-A",
        current_item_id="item-C")
    pctx = judge_pair.build_pair_context(pair, history_text=h_text,
                                         current_text=c_text)
    return judge_pair.adjudicate_pair(cb, pctx)


# ---------------------------------------------------------------- double 假证明（test_p1b 同型）

_DOUBLE_MODEL = "judge-double-v1"
_DOUBLE_PROMPT_SHA = hashlib.sha256(b"double-prompt").hexdigest()
_DOUBLE_POLICY = "policy-v1"


def _make_proof(ctx, *, verdict, mv_passed=True, rules_triggered=(),
                with_falsification=True, unbound_quotes=False,
                stolen_binding=False, wrong_order=False, wrong_item=False):
    """合同 §一 schema 假证明（提交一 audit 语义）；各关键字构造一类
    不合规/诊断形态。"""
    text_a, text_b = ctx["text_a"], ctx["text_b"]
    sha_a = hashlib.sha256(text_a.encode("utf-8")).hexdigest()
    sha_b = hashlib.sha256(text_b.encode("utf-8")).hexdigest()
    if stolen_binding:
        sha_a = hashlib.sha256("别人的正文甲".encode("utf-8")).hexdigest()
        sha_b = hashlib.sha256("别人的正文乙".encode("utf-8")).hexdigest()
    if unbound_quotes:
        quotes_a = [{"text": "不存在的片段", "offset_start": 0, "offset_end": 2,
                     "role": "subject"}]
        quotes_b = [{"text": "也不存在的片段", "offset_start": 0, "offset_end": 2,
                     "role": "subject"}]
    else:
        quotes_a = [{"text": text_a[0:4], "offset_start": 0, "offset_end": 4,
                     "role": "subject"}]
        quotes_b = [{"text": text_b[0:4], "offset_start": 0, "offset_end": 4,
                     "role": "subject"}]
    proof = {
        "pair_id": ctx["pair_id"],
        "item_a_id": ("item-X" if wrong_item else ctx["item_a_id"]),
        "item_b_id": ctx["item_b_id"],
        "text_a_sha256": sha_a,
        "text_b_sha256": sha_b,
        "order": ("ba" if wrong_order and ctx["order"] == "ab" else "ab")
        if wrong_order else ctx["order"],
        "verdict": verdict,
        "reason": "双序测试理由。",
        "quotes_a": quotes_a,
        "quotes_b": quotes_b,
        "numeric_check": {"conclusion": "一致", "details": []},
        "time_check": {"conclusion": "一致", "anchors_a": [], "anchors_b": []},
        "machine_verify": {"mode": "audit",
                           "rules_triggered": list(rules_triggered),
                           "passed": mv_passed},
        "model_version": _DOUBLE_MODEL,
        "prompt_sha256": _DOUBLE_PROMPT_SHA,
        "policy_version": _DOUBLE_POLICY,
        "judged_at": "2026-10-10T00:00:00+00:00",
    }
    if verdict == "not_duplicate" and with_falsification:
        proof["falsification"] = {
            "dimension": "subject",
            "evidence_a": {"text": text_a[0:2], "offset_start": 0, "offset_end": 2},
            "evidence_b": {"text": text_b[0:2], "offset_start": 0, "offset_end": 2},
            "relation": "双侧主体不同，证伪同一事实。",
        }
    proof["cache_key"] = judge_pair.compute_cache_key(
        _DOUBLE_MODEL, _DOUBLE_PROMPT_SHA, _DOUBLE_POLICY,
        ctx["order"], sha_a, sha_b)
    return proof


def _double_judge(handler):
    calls = []

    def _callable(ctx):
        calls.append(ctx["order"])
        return handler(ctx)

    _callable.calls = calls
    return _callable


# 主窗补强断言：公共五字段封闭 + reason 无内部码
_PUBLIC_FIELDS = {"item_id", "text", "decision", "duplicate_ids", "reason"}
_ALL_INTERNAL_CODES = (
    {"EXACT_TEXT_MATCH", "LOSSLESS_TEXT_MATCH", "FACT_EQUIVALENT",
     "JUDGE_EQUIVALENT", "JUDGE_NON_DUPLICATE", "JUDGE_UNCERTAIN",
     "VERIFIED_CONFLICT"} | set(UNRESOLVED_CODES) | {"NO_DUPLICATE_FOUND"})


def _assert_public_contract(out):
    public = out.to_public_dict()
    assert set(public) == _PUBLIC_FIELDS          # 五字段封闭，一字不多
    for code in _ALL_INTERNAL_CODES:
        assert code not in public["reason"]
    assert "http" not in public["reason"]
    assert len(public["reason"]) <= 300
    if out.decision == "重复":
        assert public["duplicate_ids"]
    else:
        assert public["duplicate_ids"] == []
    return public


# ---------------------------------------------------------------- 文本对
DUP_H = "甲公司9月24日公告营收100万元。"
DUP_C = "乙公司9月24日公告营收100万元。"


# ============================================================ 1. P_OFFSET

def test_1_duplicate_with_p_offset_still_signs_with_warning(monkeypatch):
    """§5.1-1：双序重复 + P_OFFSET（判官引文无法绑定）→ 最终**重复**；
    P_OFFSET 入 machine_findings、MACHINE_VERIFY_REJECTED 入告警——
    证明层不再一票否决。"""
    _switches_on(monkeypatch)
    resp = {
        (DUP_H, DUP_C): _jjson("重复", ("原文不存在的引文甲",), ("营收100万元",),
                               na=("100万",), nb=("100万",),
                               ta=("9月24日",), tb=("9月24日",)),
        (DUP_C, DUP_H): _jjson("重复", ("营收100万元",), ("原文不存在的引文甲",),
                               na=("100万",), nb=("100万",),
                               ta=("9月24日",), tb=("9月24日",)),
    }
    cb = _real_callable(resp)
    out = decide_service.decide_for_task(
        _history(DUP_H, "甲公司"), [], current=_current(DUP_C, "乙公司"),
        judge_callable=cb, coverage_complete=True)
    assert out.decision == "重复"
    assert out.duplicate_ids == ("item-A",)
    assert out.internal_code == "JUDGE_EQUIVALENT"
    _assert_public_contract(out)
    judged = _adjudicate_direct(cb, DUP_H, DUP_C)
    assert judged.outcome == "equivalent"
    assert "P_OFFSET" in judged.machine_findings
    assert judge_pair.MACHINE_VERIFY_REJECTED in judged.evidence_warnings


# ============================================================ 2. P_SUBJECT_MISSING

SM_H = "易天股份（300812）主力净流入5亿。"
SM_C = "名臣健康主力净流入5亿。"
SM_RESP = {
    (SM_H, SM_C): _jjson("重复", ("主力净流入5亿",), ("主力净流入5亿",),
                         na=("5亿",), nb=("5亿",), tcon="均无时间"),
    (SM_C, SM_H): _jjson("重复", ("主力净流入5亿",), ("主力净流入5亿",),
                         na=("5亿",), nb=("5亿",), tcon="均无时间"),
}


def test_2_duplicate_with_p_subject_missing_not_demoted(monkeypatch):
    """§5.1-2：双序重复 + P_SUBJECT_MISSING（恰一侧有主体代码）→ 不由
    证明层降级，最终取决于判官双序结果（此处双序一致=重复）。"""
    _switches_on(monkeypatch)
    cb = _real_callable(SM_RESP)
    out = decide_service.decide_for_task(
        _history(SM_H, "易天股份"), [], current=_current(SM_C, "名臣健康"),
        judge_callable=cb, coverage_complete=True)
    assert out.decision == "重复"
    assert out.internal_code == "JUDGE_EQUIVALENT"
    _assert_public_contract(out)
    judged = _adjudicate_direct(cb, SM_H, SM_C)
    assert judged.outcome == "equivalent"
    assert "P_SUBJECT_MISSING" in judged.machine_findings


# ============================================================ 3. P_REL_TIME

RT_H = "甲金所今日宣布降准0.5个百分点。"
RT_C = "乙金所今日宣布降准0.5个百分点。"
RT_RESP = {
    (RT_H, RT_C): _jjson("重复", ("今日宣布降准0.5个百分点",),
                         ("今日宣布降准0.5个百分点",),
                         na=("0.5",), nb=("0.5",),
                         ta=("今日",), tb=("今日",)),
    (RT_C, RT_H): _jjson("重复", ("今日宣布降准0.5个百分点",),
                         ("今日宣布降准0.5个百分点",),
                         na=("0.5",), nb=("0.5",),
                         ta=("今日",), tb=("今日",)),
}


def test_3_duplicate_with_p_rel_time_not_demoted(monkeypatch):
    """§5.1-3：双序重复 + P_REL_TIME（相对时间无共同绝对锚点）→ 不由
    证明层降级（§4.3：P_REL_TIME 降为审计告警，不再拦签）。"""
    _switches_on(monkeypatch)
    cb = _real_callable(RT_RESP)
    out = decide_service.decide_for_task(
        _history(RT_H, "甲金所"), [], current=_current(RT_C, "乙金所"),
        judge_callable=cb, coverage_complete=True)
    assert out.decision == "重复"
    assert out.internal_code == "JUDGE_EQUIVALENT"
    _assert_public_contract(out)
    judged = _adjudicate_direct(cb, RT_H, RT_C)
    assert judged.outcome == "equivalent"
    assert "P_REL_TIME" in judged.machine_findings


# ============================================================ 4. 无 falsification 双序不重复

ND_H = "甲公司预期业绩增长。"
ND_C = "乙公司预期业绩增长。"
ND_RESP = {
    (ND_H, ND_C): _jjson("不重复", ("预期业绩增长",), ("预期业绩增长",),
                         ncon="无关键数值", tcon="均无时间"),
    (ND_C, ND_H): _jjson("不重复", ("预期业绩增长",), ("预期业绩增长",),
                         ncon="无关键数值", tcon="均无时间"),
}


def test_4_bare_not_duplicate_signs_with_p_no_axis(monkeypatch):
    """§5.1-4：双序不重复 + 无 falsification（机检无证伪轴）→ 最终
    **不重复**（JUDGE_NON_DUPLICATE），同时记录 P_NO_AXIS 告警。"""
    _switches_on(monkeypatch)
    cb = _real_callable(ND_RESP)
    out = decide_service.decide_for_task(
        _history(ND_H, "甲公司"), [], current=_current(ND_C, "乙公司"),
        judge_callable=cb, coverage_complete=True)
    assert out.decision == "不重复"
    assert out.duplicate_ids == ()
    assert out.internal_code == "JUDGE_NON_DUPLICATE"
    assert out.pair_results[0].code == "JUDGE_NON_DUPLICATE"
    _assert_public_contract(out)
    judged = _adjudicate_direct(cb, ND_H, ND_C)
    assert judged.outcome == "conflict"
    assert "P_NO_AXIS" in judged.evidence_warnings
    assert judged.verified_conflicts == ()       # 无轴不构造审计冲突件


# ============================================================ 5. 绑定错误仍边界

def test_5_contract_binding_errors_still_boundary():
    """§5.1-5：文本 SHA 错绑（换文复用）/order 错绑/item 错绑 → 合同级
    硬失败保留，仍边界进人工（证明解耦不松绑身份合同）。"""
    for kwargs in ({"stolen_binding": True},
                   {"wrong_order": True},
                   {"wrong_item": True}):
        judge = _double_judge(lambda ctx, kw=kwargs: _make_proof(
            ctx, verdict="duplicate", **kw))
        out = decide_service.decide_for_task(
            _history(DUP_H, "甲公司"), [],
            current=_current(DUP_C, "乙公司"),
            judge_callable=judge, judge_in_chain=True, coverage_complete=True)
        assert out.decision == "边界case/疑难case", kwargs
        assert out.duplicate_ids == ()
        assert out.pair_results[0].outcome == "unresolved"
        assert judge_pair.INVALID_OUTPUT in out.pair_results[0].detail
        _assert_public_contract(out)


# ============================================================ 6. 分歧/超时/JSON 非法仍边界

def test_6_disagree_timeout_invalid_json_still_boundary(monkeypatch):
    """§5.1-6：双序分歧 / 调用超时 / JSON 非法 → 仍然边界（§4.2 矩阵：
    任一 doubtful/failure/invalid 或双序不一致 → 未决进人工）。"""
    _switches_on(monkeypatch)
    # 双序分歧
    disagree = _double_judge(lambda ctx: _make_proof(
        ctx, verdict=("duplicate" if ctx["order"] == "ab" else "not_duplicate")))
    out = decide_service.decide_for_task(
        _history(DUP_H, "甲公司"), [], current=_current(DUP_C, "乙公司"),
        judge_callable=disagree, judge_in_chain=True, coverage_complete=True)
    assert out.decision == "边界case/疑难case"
    assert out.pair_results[0].code == "JUDGE_UNCERTAIN"
    assert judge_pair.ORDER_DISAGREE in out.pair_results[0].detail
    _assert_public_contract(out)
    # 调用超时（double 慢调用 + 真线程硬超时）
    def _slow(ctx):
        time.sleep(1.0)
        return _make_proof(ctx, verdict="duplicate")
    out2 = decide_service.decide_for_task(
        _history(DUP_H, "甲公司"), [], current=_current(DUP_C, "乙公司"),
        judge_callable=_double_judge(_slow), judge_in_chain=True,
        judge_timeout_s=0.2, coverage_complete=True)
    assert out2.decision == "边界case/疑难case"
    assert judge_pair.TIMEOUT in out2.pair_results[0].detail
    _assert_public_contract(out2)
    # JSON 非法（真件路：LLM 返非 JSON → verdict=invalid → 未决）
    bad_json = _real_callable({(DUP_H, DUP_C): "not-json{{{",
                               (DUP_C, DUP_H): "也不是JSON"})
    out3 = decide_service.decide_for_task(
        _history(DUP_H, "甲公司"), [], current=_current(DUP_C, "乙公司"),
        judge_callable=bad_json, coverage_complete=True)
    assert out3.decision == "边界case/疑难case"
    assert out3.pair_results[0].outcome == "unresolved"
    assert judge_pair.INVALID_OUTPUT in out3.pair_results[0].detail
    _assert_public_contract(out3)


# ============================================================ 7. 规则链硬冲突跳过判官

STAGE_H = "9月10日开盘，甲公司公告回购100股。"
STAGE_C = "9月10日收盘，甲公司公告回购100股。"


def test_7_rule_chain_verified_conflict_skips_judge(monkeypatch):
    """§5.1-7（§4.3 前置硬冲突）：规则链已产出 VerifiedConflict 的对不
    进判官——判官零调用，保持不重复（判官无权推翻规则链硬冲突）。"""
    _switches_on(monkeypatch)
    history = _ctx(H_ID, "item-A", STAGE_H, 1)
    current = _ctx(C_ID, "item-C", STAGE_C, 3)
    history["facts"] = facts_rule.extract_facts(
        H_ID, STAGE_H, dict_version=facts_rule.RULE_DICT_VERSION_V2)
    current["facts"] = facts_rule.extract_facts(
        C_ID, STAGE_C, dict_version=facts_rule.RULE_DICT_VERSION_V2)
    judge = _double_judge(lambda ctx: _make_proof(ctx, verdict="duplicate"))
    out = decide_service.decide_for_task(
        history, [], current=current, judge_callable=judge,
        judge_in_chain=True, coverage_complete=True)
    assert judge.calls == []                     # 判官零调用
    assert out.decision == "不重复"
    assert out.duplicate_ids == ()
    assert out.internal_code == "VERIFIED_CONFLICT"
    assert out.pair_results[0].outcome == "conflict"
    assert out.pair_results[0].code == "VERIFIED_CONFLICT"
    _assert_public_contract(out)


# ============================================================ 8. 完整原文回退证据

def test_8_all_quotes_unbound_full_text_fallback_evidence():
    """§5.1-8：引文全部失效（双侧均无可绑定片段）→ 仍按双序语义签重复；
    使用完整原文回退证据（field="text"、quote=该侧完整正文、start=0、
    end=len），审计记录 quote/start/end 可回指；EVIDENCE_FALLBACK_FULL_TEXT
    告警在案；回退证据只进内部审计，公共五字段零泄漏。"""
    judge = _double_judge(lambda ctx: _make_proof(
        ctx, verdict="duplicate", unbound_quotes=True,
        mv_passed=False, rules_triggered=("P_OFFSET",)))
    history, current = _history(DUP_H, "甲公司"), _current(DUP_C, "乙公司")
    out = decide_service.decide_for_task(
        history, [], current=current, judge_callable=judge,
        judge_in_chain=True, coverage_complete=True)
    assert out.decision == "重复"
    assert out.duplicate_ids == ("item-A",)
    public = _assert_public_contract(out)
    # 公共五字段无回退证据通道（evidence 从不在公共面；reason 无原文倾泻）
    assert public["text"] == DUP_C               # text=本条自身正文（现役合同）
    assert DUP_H not in public["reason"]
    # 对级回退证据实形：双侧完整原文 span，可回指
    pair = out.pair_results[0]
    by_record = {}
    for ev in pair.used_evidence:
        by_record.setdefault(ev.record_id, ev)
    assert by_record[H_ID].field == "text"
    assert (by_record[H_ID].start, by_record[H_ID].end) == (0, len(DUP_H))
    assert DUP_H[by_record[H_ID].start:by_record[H_ID].end] == \
        by_record[H_ID].quote == DUP_H
    assert (by_record[C_ID].start, by_record[C_ID].end) == (0, len(DUP_C))
    assert DUP_C[by_record[C_ID].start:by_record[C_ID].end] == \
        by_record[C_ID].quote == DUP_C
    # 审计记录（commit 写入计划）同样可回指
    ctx = CommitContext(
        scope_id="default", business_date="2026-09-26", arrival_seq=3,
        current=current, candidates=(history,), visible_seq=10,
        prepared_seq=10, coverage_complete=True)
    plan = build_commit_write_plan(ctx, out, audit_complete=True)
    record = plan.audit_batch.records[0]
    assert record.history_evidence["quote"] == DUP_H
    assert record.history_evidence["start"] == 0
    assert record.history_evidence["end"] == len(DUP_H)
    assert DUP_H[0:len(DUP_H)] == record.history_evidence["quote"]
    assert record.current_evidence["quote"] == DUP_C
    # 适配层诊断面：EVIDENCE_FALLBACK_FULL_TEXT 告警在案
    judged = _adjudicate_direct(judge, DUP_H, DUP_C)
    assert judged.outcome == "equivalent"
    assert judge_pair.EVIDENCE_FALLBACK_FULL_TEXT in judged.evidence_warnings
    assert judge_pair.EVIDENCE_UNBOUND in judged.evidence_warnings
    assert judged.evidence_status == "fail"      # 双侧均无可绑定引文
