# -*- coding: utf-8 -*-
"""一期 v6-lite（2026-10-11，分支 p3-v6-phase1）验收测试——产品批准的
两条规则（就这两条，不扩 scope）：

规则一（R8 升级，零 fp 风险方向）：修订改值→不重复——
  - 显性"由177.9万修正为177.5万"→不重复（机器候选证据注入可观测+
    判官 v6 条款）；
  - 同指向同一修订值省略修订过程→重复；
  - 只补充背景有效值相同→重复（条款面钉）；
  - 无修订措辞的普通微差→不受本规则影响（不容差总闸不动：数值硬冲突
    照拦、机器候选证据空）。
  机器层=复用 compare/core_conflict.py 退役区 _REVISION_RE 产结构化
  候选证据（位置/前后值）注入判官上下文；机器绝不直接判，判官终审。

规则二（R7 升级，fp 风险全集中于此，三防线必配）：主体单方缺失受
  约束回填六条件——条件①机器前置硬闸（证券代码/主体抽取现有件）+
  条件②-⑤判官条款+条件⑥强制存疑转边界；原因码"主体单方缺失高置信
  对齐"；shadow 三计数 judge.backfill.triggered/signed/vetoed。
  正例：台积电市占率型、*ST清越型→重复；反例四枚：①双方都缺主体→
  回填不触发落边界 ②甲公司vs乙公司→不重复 ③万科剧本（B 缺主体但
  正文出现另一明确主体）→条件④拦截 ④新增独立事实（净利润+现金流
  双指标）→条件⑤拦截。

版本与映射钉：judge_prompt_v6.py 新建（v5 全量继承+两条条款修订）；
  llm_residual 注册 judge_v6+真实 SHA；_PROMPT_TO_POLICY 增 judge_v6→
  policy_v4；_MODE_TO_VERSION 的 semantic_authority→judge_v6+policy_v4
  （v5 保留注册供回放对比，prompt 直解通道可达）；默认模式仍
  legacy_proof_gate（生产零变化）。

legacy 全等回归：默认模式行为与 2d0d418 基线逐字全等（§7 钉测模式
  ——机器证据零装配、零计数、上下文零增量字段、基线码/文案逐字）。

纪律：判官全假件零真 API（罐装 LLM/双序假证明）；不得触碰 v5 提示词、
  公共五字段、金标文件。
"""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest

from news_flash_dedup.decide import judge_adapter, judge_pair
from news_flash_dedup.decide import judge_machine_evidence as jme
from news_flash_dedup.decide import judge_prompt_v5 as v5
from news_flash_dedup.decide import judge_prompt_v6 as v6
from news_flash_dedup.decide import judge_version_config as jvc
from news_flash_dedup.decide import llm_residual as lr
from news_flash_dedup.decide import policy_version as pv
from news_flash_dedup.decide import service as decide_service
from news_flash_dedup.lib import run_manifest as rm


# ---------------------------------------------------------------- 夹具（p3fix/p3sem 同形）

def _ctx(record_id, item_id, text, arrival_seq, **kw):
    return {
        "record_id": record_id, "item_id": item_id, "text": text,
        "raw_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "scope_id": "default", "business_date": "2026-09-26",
        "arrival_seq": arrival_seq, "pipeline_version": "dedup_v1",
        **kw,
    }


def _fact(record_id, subject=None, anchor="甲"):
    """最小合法事实夹具；subject=None=主体槽 missing（缺主体侧）。"""
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


H_ID, C_ID = "a" * 64, "c" * 64


def _history(text, subject=None):
    h = _ctx(H_ID, "item-A", text, 1)
    h["facts"] = [_fact(H_ID, subject, text[0])]
    return h


def _current(text, subject=None):
    c = _ctx(C_ID, "item-C", text, 3)
    c["facts"] = [_fact(C_ID, subject, text[0])]
    return c


def _semantic_env(monkeypatch):
    monkeypatch.setenv(judge_pair.JUDGE_DECISION_MODE_ENV,
                       judge_pair.MODE_SEMANTIC_AUTHORITY)
    monkeypatch.setenv("DEDUP_JUDGE_PROOF", "1")


def _legacy_env(monkeypatch):
    monkeypatch.delenv(judge_pair.JUDGE_DECISION_MODE_ENV, raising=False)
    monkeypatch.setenv("DEDUP_JUDGE_PROOF", "1")


class _MockLLM:
    """按 (文本A, 文本B) 路由罐装判定（p3sem 同型）；捕获 system 面钉
    v6 提示词生效。零真 API。"""

    def __init__(self, responses):
        self.responses = responses
        self.calls = []
        self.systems = []

    def __call__(self, model, system, user, timeout_s=None):
        self.calls.append(user)
        self.systems.append(system)
        a = user.split("【文本A】\n", 1)[1].split("\n\n【文本B】\n", 1)[0]
        b = user.split("\n\n【文本B】\n", 1)[1]
        return self.responses[(a, b)], 0.01


def _jjson(decision, reason, ea, eb, *, na=(), nb=(), ta=(), tb=()):
    return json.dumps({
        "decision": decision, "reason": reason,
        "evidence_a": list(ea), "evidence_b": list(eb),
        "numeric_check": {"conclusion": "一致", "numbers_a": list(na),
                          "numbers_b": list(nb)},
        "time_check": {"conclusion": "一致", "times_a": list(ta),
                       "times_b": list(tb)},
    }, ensure_ascii=False)


def _spy_wrap(cb):
    """包装真件 callable：捕获 (pair_context, proof) 双面（机器证据注入
    可观测+判官理由码留痕断言用）。"""
    ctxs, proofs = [], []

    def _callable(pair_context):
        ctxs.append(pair_context)
        proof = cb(pair_context)
        proofs.append(proof)
        return proof

    _callable.ctxs = ctxs
    _callable.proofs = proofs
    return _callable


def _v6_real_callable(monkeypatch, responses):
    """真件 judge_callable：真 adapter+真 judge_proof 核验+真装配闸
    （仅 LLM 边界罐装，prompt_version=judge_v6）。返回 (spy, mock)。"""
    _semantic_env(monkeypatch)
    mock = _MockLLM(responses)
    judge = lr.SyncResidualJudge(
        lr.ResidualJudgeConfig(prompt_version="judge_v6"),
        call_fn=mock)
    cb = judge_adapter.build_judge_callable(judge=judge)
    assert cb is not None
    return _spy_wrap(cb), mock


def _decide(h_text, h_subject, c_text, c_subject, judge, **kw):
    return decide_service.decide_for_task(
        _history(h_text, h_subject), [],
        current=_current(c_text, c_subject),
        judge_callable=judge, judge_in_chain=True,
        coverage_complete=True, **kw)


# ---------------------------------------------------------------- 双序假证明（p3fix 同型）

_DOUBLE_MODEL = "judge-double-v1"
_DOUBLE_PROMPT_SHA = hashlib.sha256(b"double-prompt").hexdigest()
_DOUBLE_POLICY = "policy-v1"


def _make_proof(ctx, *, verdict, reason="双序测试理由。"):
    text_a, text_b = ctx["text_a"], ctx["text_b"]
    sha_a = hashlib.sha256(text_a.encode("utf-8")).hexdigest()
    sha_b = hashlib.sha256(text_b.encode("utf-8")).hexdigest()
    proof = {
        "pair_id": ctx["pair_id"],
        "item_a_id": ctx["item_a_id"],
        "item_b_id": ctx["item_b_id"],
        "text_a_sha256": sha_a,
        "text_b_sha256": sha_b,
        "order": ctx["order"],
        "verdict": verdict,
        "reason": reason,
        "quotes_a": [{"text": text_a[0:4], "offset_start": 0, "offset_end": 4,
                      "role": "subject"}],
        "quotes_b": [{"text": text_b[0:4], "offset_start": 0, "offset_end": 4,
                      "role": "subject"}],
        "numeric_check": {"conclusion": "一致", "details": []},
        "time_check": {"conclusion": "一致", "anchors_a": [], "anchors_b": []},
        "machine_verify": {"mode": "audit",
                           "rules_triggered": [], "passed": True},
        "model_version": _DOUBLE_MODEL,
        "prompt_sha256": _DOUBLE_PROMPT_SHA,
        "policy_version": _DOUBLE_POLICY,
        "judged_at": "2026-10-11T00:00:00+00:00",
    }
    proof["cache_key"] = judge_pair.compute_cache_key(
        _DOUBLE_MODEL, _DOUBLE_PROMPT_SHA, _DOUBLE_POLICY,
        ctx["order"], sha_a, sha_b)
    return proof


def _judge(handler):
    calls = []

    def _callable(ctx):
        calls.append(ctx)
        return handler(ctx)

    _callable.calls = calls
    return _callable


# ============================================================ 1. 版本注册与映射钉

def test_v6_registered_v5_kept_for_replay():
    """一期钉①：judge_v6 注册进 _JUDGE_PROMPTS（judge_prompt_for_version
    取 (v6 正文, v6 SHA)==模块常量）；v1/v2/v3/v5 原样可取；v4 维持
    不注册。"""
    assert sorted(lr._JUDGE_PROMPTS) == [
        "judge_v1", "judge_v2", "judge_v3", "judge_v5", "judge_v6"]
    text, sha = lr.judge_prompt_for_version("judge_v6")
    assert text == v6.JUDGE_PROMPT_V6
    assert sha == v6.PROMPT_SHA256_V6
    # v5 保留注册供回放对比（prompt 直解通道可达，见钉⑤）
    assert lr.judge_prompt_for_version("judge_v5") == (
        v5.JUDGE_PROMPT_V5, v5.PROMPT_SHA256_V5)
    with pytest.raises(ValueError):
        lr.judge_prompt_for_version("judge_v4")


def test_v6_sha256_real_and_distinct():
    """一期钉②：v6 真实 SHA（重算自证）+字面量钉；不与 v1/v2/v3/v5
    雷同；v5 SHA 零漂移（本窗口不得触碰 v5 提示词）。"""
    assert v6.PROMPT_SHA256_V6 == hashlib.sha256(
        v6.JUDGE_PROMPT_V6.encode("utf-8")).hexdigest()
    assert v6.PROMPT_SHA256_V6 == (
        "414fcfb6af7664023bb707dd69da01cd4447b0bc5fa4bd5e9275004a717f18b0")
    assert v6.PROMPT_SHA256_V6 not in (
        lr.PROMPT_SHA256, lr.PROMPT_SHA256_V2, lr.PROMPT_SHA256_V3,
        v5.PROMPT_SHA256_V5)
    # v5 原文零漂移（提交二钉值原样）
    assert v5.PROMPT_SHA256_V5 == (
        "25deb3d52cca233561c1b5eeccad3d481c2ecf5cbf76667182cb8f1b55bf77a6")


def test_v6_prompt_clauses_and_v5_inheritance():
    """一期钉③：v6=v5 全量继承+两条产品规则条款修订——
    （a）v6 含 R8 修订改值条款锚点（改值→不重复/同值省略过程→重复/
    只补背景→重复/角色指标时间对不上→存疑）；
    （b）v6 含 R7 受约束回填条款锚点（六条件+原因码"主体单方缺失高
    置信对齐"+强制存疑+双方都缺主体不适用）；
    （c）优先级条款保留（v6 语义：先按本条款裁决）；
    （d）v5 暂定边界表述（"产品规则未冻结""一律存疑，不得判重复"）在
    v6 不复现——两条规则已冻结；
    （e）其余段落逐字继承：v6 与 v5 的公共前缀至【暂定边界条款/产品
    规则条款】分节、公共后缀自【输出契约】起，均逐字相等。"""
    t6, t5 = v6.JUDGE_PROMPT_V6, v5.JUDGE_PROMPT_V5
    # （a）R8 条款锚点
    for anchor in ("前值修订（修订改值）", "修订后最新有效值改变的，判不重复",
                   "只是一方省略了修订过程的", "判重复",
                   "一方仅补充修订背景、双方有效值相同的，判重复",
                   "修订对应的角色、指标或时间对不上", "判存疑"):
        assert anchor in t6, anchor
    # （b）R7 条款锚点（六条件）
    for anchor in ("主体单方缺失受约束回填",
                   "有且仅有一方缺主体、且另一方明确写了主体",
                   "至少两个不同角色的核心数值一致",
                   "单一金额一致不足以支撑回填",
                   "有明确事件，且有事实时间或阶段",
                   "缺主体一方的正文中没有出现另一个明确主体",
                   "无新增独立事实", "新指标、新事件、新对象均不属于信息补充",
                   "任一条件不满足，强制判存疑",
                   "双方都缺主体时本条不适用，判存疑",
                   "主体单方缺失高置信对齐"):
        assert anchor in t6, anchor
    # （c）优先级条款保留（冻结后语义）
    assert "产品规则条款优先级高于一般重复/不重复规则" in t6
    assert "先按本条款裁决，不再适用一般数值冲突规则" in t6
    # （d）v5 暂定边界表述不复现
    assert "产品规则未冻结" not in t6
    assert "在产品规则确认前一律存疑，不得判重复" not in t6
    assert "暂定边界条款" not in t6
    # （e）逐字继承：首行（policy 版本行）之后的头段至分节前逐字相等；
    # 【输出契约】起的尾段逐字相等
    body6, body5 = t6.split("\n", 1)[1], t5.split("\n", 1)[1]
    cut6 = body6.index("【产品规则条款")
    cut5 = body5.index("【暂定边界条款")
    assert body6[:cut6] == body5[:cut5]      # 【输入纪律】+【判定口径】逐字
    tail6 = t6.index("【输出契约】")
    tail5 = t5.index("【输出契约】")
    assert t6[tail6:] == t5[tail5:]       # 输出契约/硬性规则/用户消息格式逐字
    # policy 版本头行不同（policy_v4 vs policy_v3）
    assert t6.split("。", 1)[0] == "你是快讯文本去重判定裁判（policy_version=policy_v4）"
    assert t5.split("。", 1)[0] == "你是快讯文本去重判定裁判（policy_version=policy_v3）"


def test_mode_mapping_semantic_authority_to_v6():
    """一期钉④：_MODE_TO_VERSION/_MODE_TO_PROMPT 的 semantic_authority
    → judge_v6+policy_v4（judge_version_for_mode 构造+真实 SHA 同源）；
    legacy_proof_gate 映射不变（judge_v1+policy_v2）。"""
    assert jvc._MODE_TO_VERSION[judge_pair.MODE_SEMANTIC_AUTHORITY] == (
        "judge_v6", "policy_v4")
    assert jvc._MODE_TO_PROMPT[judge_pair.MODE_SEMANTIC_AUTHORITY] == "judge_v6"
    assert jvc._MODE_TO_VERSION[judge_pair.MODE_LEGACY_PROOF_GATE] == (
        "judge_v1", "policy_v2")
    vc = jvc.judge_version_for_mode(judge_pair.MODE_SEMANTIC_AUTHORITY)
    assert (vc.prompt_version, vc.prompt_sha256, vc.policy_version,
            vc.decision_mode) == (
        "judge_v6", v6.PROMPT_SHA256_V6, "policy_v4", "semantic_authority")
    legacy = jvc.judge_version_for_mode(judge_pair.MODE_LEGACY_PROOF_GATE)
    assert (legacy.prompt_version, legacy.policy_version) == (
        "judge_v1", "policy_v2")


def test_version_config_full_validation_for_v6():
    """一期钉⑤：JudgeVersionConfig 全量校验对 v6 生效——
    v6+全零/错 SHA→拒；v6+错 policy（policy_v3）→拒；semantic 模式配
    v5（v5 自洽但模式现值=v6）→固定映射冲突拒；v6+policy_v4+semantic+
    真实 SHA→过；v5 空 mode（prompt 直解回放通道）→policy_v3 合法。"""
    sha_v6 = lr.judge_prompt_for_version("judge_v6")[1]
    with pytest.raises(ValueError, match="注册表真实哈希不符"):
        jvc.JudgeVersionConfig(prompt_version="judge_v6",
                               prompt_sha256="0" * 64,
                               policy_version="policy_v4",
                               decision_mode="semantic_authority")
    wrong_sha = ("f" if sha_v6[0] != "f" else "e") + sha_v6[1:]
    with pytest.raises(ValueError, match="注册表真实哈希不符"):
        jvc.JudgeVersionConfig(prompt_version="judge_v6",
                               prompt_sha256=wrong_sha,
                               policy_version="policy_v4",
                               decision_mode="semantic_authority")
    with pytest.raises(ValueError, match="固定策略不符"):
        jvc.JudgeVersionConfig(prompt_version="judge_v6",
                               prompt_sha256=sha_v6,
                               policy_version="policy_v3",
                               decision_mode="semantic_authority")
    with pytest.raises(ValueError, match="固定映射冲突"):
        jvc.JudgeVersionConfig(prompt_version="judge_v5",
                               prompt_sha256=lr.judge_prompt_for_version(
                                   "judge_v5")[1],
                               policy_version="policy_v3",
                               decision_mode="semantic_authority")
    ok = jvc.JudgeVersionConfig(prompt_version="judge_v6",
                                prompt_sha256=sha_v6,
                                policy_version="policy_v4",
                                decision_mode="semantic_authority")
    assert ok.prompt_sha256 == sha_v6
    # v5 prompt 直解回放通道（空模式）仍可达
    replay = jvc.judge_version_for_prompt("judge_v5")
    assert (replay.prompt_version, replay.policy_version,
            replay.decision_mode) == ("judge_v5", "policy_v3", "")


def test_default_mode_and_policy_default_unchanged():
    """一期钉⑥：默认模式仍 legacy_proof_gate（装配=judge_v1+policy_v2，
    生产零变化）；DEFAULT_POLICY_VERSION 维持 policy_v3（一期不改缺省）。"""
    assert jvc.default_judge_version({}).prompt_version == "judge_v1"
    assert jvc.default_judge_version(
        {}).policy_version == "policy_v2"
    assert pv.DEFAULT_POLICY_VERSION == "policy_v3"
    assert pv.POLICY_VERSION_V4 == "policy_v4"
    assert rm.DEFAULT_POLICY_VERSION == "policy_v3"


# ============================================================ 2. 机器候选证据层（unit）

def test_revision_candidates_structured():
    """R8 机器候选证据结构化：_REVISION_RE（core_conflict 退役区正则复用）
    命中→位置（start/end）、表面形、前值/后值、归一值；位置可切片自证。"""
    text = "甲公司公告：净利由177.9万修正为177.5万。"
    hits = jme.revision_candidates(text)
    assert len(hits) == 1
    hit = hits[0]
    assert text[hit["start"]:hit["end"]] == hit["surface"]
    assert hit["before"] == "177.9万"
    assert hit["after"] == "177.5万"
    assert hit["before_norm"] == "1.779E+6"
    assert hit["after_norm"] == "1.775E+6"
    assert hit["before_norm"] != hit["after_norm"]


def test_revision_candidates_empty_without_wording():
    """R8 机器候选证据只在显性修订措辞命中：无"由X修正为Y"措辞（含普通
    微差 100万/100.1万、*ST清越型）→零命中（机器绝不凭数值差异硬判，
    不容差总闸不动）。"""
    for text in ("甲公司9月24日公告营收100万元。",
                 "甲公司9月24日公告营收100.1万元。",
                 "*ST清越（600146）9月24日主力净流入5亿元，收盘价3.21元。",
                 "前值由177.9万人修正为177.5万人。"):  # 单位后缀阻断=退役口径
        assert jme.revision_candidates(text) == (), text
    ev = jme.build_machine_evidence(
        "甲公司9月24日公告营收100万元。", "甲公司9月24日公告营收100.1万元。")
    assert ev["revision_candidates"] == {
        "history": [], "current": [], "hit_any_side": False}


def test_subject_backfill_gate_unilateral_missing():
    """R7 条件①机器前置硬闸：恰一方明确（证券代码或主体抽取现有件）→
    unilateral_missing=True（哪侧缺可读 *_has_subject）。"""
    # 证券代码侧明确、另一侧泛指（无代码无主体名）
    gate = jme.subject_backfill_gate(
        "*ST清越（600146）9月24日主力净流入5亿元。",
        "公司股票9月24日主力净流入5亿元。")
    assert gate["unilateral_missing"] is True
    assert gate["history_has_subject"] is True
    assert gate["current_has_subject"] is False
    assert gate["history_codes"] == ["600146"]
    assert gate["current_codes"] == []
    assert gate["both_missing"] is False
    # 主体名侧明确（主体抽取现有件 facts subject 槽）
    gate2 = jme.subject_backfill_gate(
        "台积电9月24日公布二季度营收250亿美元，全球市占率69%。",
        "该公司9月24日公布二季度营收250亿美元，全球市占率69%。",
        history_subjects=("台积电",))
    assert gate2["unilateral_missing"] is True
    assert gate2["history_subjects"] == ["台积电"]
    assert gate2["current_subjects"] == []
    # 反向：current 明确、history 缺
    gate3 = jme.subject_backfill_gate(
        "该公司9月24日公布二季度营收250亿美元。",
        "台积电9月24日公布二季度营收250亿美元。",
        current_subjects=("台积电",))
    assert gate3["unilateral_missing"] is True
    assert gate3["history_has_subject"] is False
    assert gate3["current_has_subject"] is True


def test_subject_backfill_gate_both_missing_and_both_present():
    """R7 条件①边界形态：双方都缺主体→both_missing=True、回填不得触发
    （unilateral=False）；双方都写主体（名或码）→两布尔皆 False（不同
    主体→不重复为现状路径，硬闸不置位）。"""
    gate = jme.subject_backfill_gate(
        "9月24日公布二季度营收250亿美元，全球市占率69%。",
        "9月25日公布二季度营收250亿美元，全球市占率69%。",
        history_subjects=(), current_subjects=())
    assert gate["both_missing"] is True
    assert gate["unilateral_missing"] is False
    both_present = jme.subject_backfill_gate(
        "甲公司（600001）公告回购股份。", "乙公司（600002）公告回购股份。")
    assert both_present["both_missing"] is False
    assert both_present["unilateral_missing"] is False
    both_names = jme.subject_backfill_gate(
        "万科9月24日公告净利润100万元。", "万科9月24日公告净利润100万元。",
        history_subjects=("万科",), current_subjects=("万科",))
    assert both_names["unilateral_missing"] is False


def test_fact_subject_values_extraction():
    """主体抽取现有件取值：facts 主体槽 present 才取 raw_value（去重
    保序）；missing/非 dict/空串一律不取（机检无判据≠明确写了主体）。"""
    facts = [
        _fact("r1", "台积电", "台"),
        {"fact_id": "f2", "subject": {"status": "missing",
                                       "raw_value": None, "evidence": []}},
        {"not_a_fact": True},
        {"subject": {"status": "present", "raw_value": "  ", "evidence": []}},
        {"fact_id": "f3", "subject": {"status": "present",
                                      "raw_value": "台积电", "evidence": []}},
    ]
    assert jme.fact_subject_values(facts) == ("台积电",)
    assert jme.fact_subject_values(()) == ()
    assert jme.fact_subject_values(None) == ()
    assert jme.fact_subject_values({"facts": []}) == ()


def test_build_pair_context_machine_evidence_injection():
    """机器候选证据注入判官上下文：build_pair_context(machine_evidence=
    None)→键缺席（legacy 面零增量字段、键集与基线一致）；传证据→
    context["machine_evidence"] 同对象注入（缓存键/证明面不读它——
    compute_cache_key 输入不含该键）。"""
    pair = SimpleNamespace(
        pair_id="p-v6", history_record_id=H_ID, current_record_id=C_ID,
        history_item_id="item-A", current_item_id="item-C")
    base = judge_pair.build_pair_context(
        pair, history_text="甲公司公告净利为177.9万。",
        current_text="甲公司公告：净利由177.9万修正为177.5万。")
    assert "machine_evidence" not in base
    assert set(base) == {
        "pair_id", "history_record_id", "current_record_id",
        "history_item_id", "current_item_id", "history_text", "current_text",
        "history_text_sha256", "current_text_sha256"}
    ev = jme.build_machine_evidence(
        "甲公司公告净利为177.9万。",
        "甲公司公告：净利由177.9万修正为177.5万。")
    ctx = judge_pair.build_pair_context(
        pair, history_text="甲公司公告净利为177.9万。",
        current_text="甲公司公告：净利由177.9万修正为177.5万。",
        machine_evidence=ev)
    assert ctx["machine_evidence"] is ev
    # 注入不改内容寻址缓存键素材（同文同键）
    assert ctx["history_text_sha256"] == base["history_text_sha256"]
    assert ctx["current_text_sha256"] == base["current_text_sha256"]
    # 双序展开机器证据随上下文传播（判官可见）
    for order in judge_pair.ORDERS:
        assert "machine_evidence" in judge_pair._order_context(ctx, order)


# ============================================================ 3. R8 端到端（semantic 真件链）

R8_H = "甲公司公告净利为177.9万。"
R8_C = "甲公司公告：净利由177.9万修正为177.5万。"


def _r8_nd_responses():
    return {
        (R8_H, R8_C): _jjson("不重复", "修订后最新有效值改变：177.9万被修正为177.5万。",
                              ("净利为177.9万",), ("由177.9万修正为177.5万",)),
        (R8_C, R8_H): _jjson("不重复", "修订后最新有效值改变：177.9万被修正为177.5万。",
                              ("由177.9万修正为177.5万",), ("净利为177.9万",)),
    }


def _r8_dup_responses():
    return {
        (R8_H, R8_C): _jjson("重复", "同指向同一修订值：省略修订过程。",
                              ("净利为177.9万",), ("由177.9万修正为177.5万",)),
        (R8_C, R8_H): _jjson("重复", "同指向同一修订值：省略修订过程。",
                              ("由177.9万修正为177.5万",), ("净利为177.9万",)),
    }


def test_r8_revision_changes_value_not_duplicate(monkeypatch):
    """R8 正钉①：显性"由177.9万修正为177.5万"→**不重复**
    （JUDGE_NON_DUPLICATE，判官 v6 条款+双序一致）——机器候选证据注入
    可观测（判官上下文 current 侧结构化命中：位置/前后值/归一值）+
    v6 提示词作为 system 实际生效；R7 硬闸不触发（双侧主体明确）。"""
    spy, mock = _v6_real_callable(monkeypatch, _r8_nd_responses())
    out = _decide(R8_H, "甲公司", R8_C, "甲公司", spy)
    assert out.decision == "不重复"
    assert out.internal_code == "JUDGE_NON_DUPLICATE"
    assert out.duplicate_ids == ()
    assert mock.calls and all(s == v6.JUDGE_PROMPT_V6 for s in mock.systems)
    # 机器候选证据注入可观测（判官上下文，双侧独立）
    assert len(spy.ctxs) == 2
    for ctx in spy.ctxs:
        ev = ctx["machine_evidence"]
        assert ev["version"] == "v6_lite_phase1"
        assert ev["revision_candidates"]["hit_any_side"] is True
        assert ev["revision_candidates"]["history"] == []
        cur = ev["revision_candidates"]["current"]
        assert len(cur) == 1
        assert cur[0]["before"] == "177.9万"
        assert cur[0]["after"] == "177.5万"
        assert cur[0]["before_norm"] == "1.779E+6"
        assert cur[0]["after_norm"] == "1.775E+6"
        assert R8_C[cur[0]["start"]:cur[0]["end"]] == cur[0]["surface"]
        assert ev["subject_backfill"]["unilateral_missing"] is False
    # 判官理由（修订改值）随证明件留痕
    assert all("修正" in p["reason"] for p in spy.proofs)
    # R7 shadow：本对不涉及回填（零计数）
    assert not {k: v for k, v in out.judge_diagnostics.items()
                if "backfill" in k}


def test_r8_same_revised_value_omits_process_duplicate(monkeypatch):
    """R8 正钉②：两条指向同一修订值（177.5万）、只是一方省略修订过程
    →**重复**（JUDGE_EQUIVALENT）；机器候选证据在修订措辞侧（history）
    命中可观测；数值硬冲突不拦（mention 数不等=信息补充放行）。"""
    spy, mock = _v6_real_callable(monkeypatch, _r8_dup_responses())
    out = _decide(R8_C, "甲公司", R8_H, "甲公司", spy)
    assert out.decision == "重复"
    assert out.internal_code == "JUDGE_EQUIVALENT"
    assert out.duplicate_ids == ("item-A",)
    assert out.reason == judge_pair.JUDGE_DUPLICATE_REASON
    assert all(s == v6.JUDGE_PROMPT_V6 for s in mock.systems)
    for ctx in spy.ctxs:
        ev = ctx["machine_evidence"]
        assert len(ev["revision_candidates"]["history"]) == 1
        assert ev["revision_candidates"]["current"] == []
        assert ev["revision_candidates"]["history"][0]["after"] == "177.5万"


def test_r8_plain_micro_difference_unaffected(monkeypatch):
    """R8 反钉：无修订措辞的普通微差（100万 vs 100.1万）→不受本规则
    影响——机器候选证据空（unit 级）+数值硬冲突前置层照拦
    （VERIFIED_CONFLICT+判官零调用，不容差总闸不动）。"""
    micro_h = "甲公司9月24日公告营收100万元。"
    micro_c = "甲公司9月24日公告营收100.1万元。"
    # unit 级：零候选证据
    ev = jme.build_machine_evidence(micro_h, micro_c)
    assert ev["revision_candidates"] == {
        "history": [], "current": [], "hit_any_side": False}
    # 服务级：数值硬冲突拦截（semantic 模式）+判官零调用
    _semantic_env(monkeypatch)

    def _boom(ctx):
        raise AssertionError("硬冲突对不得进判官")

    out = _decide(micro_h, "甲公司", micro_c, "甲公司", _boom)
    assert out.decision == "不重复"
    assert out.internal_code == "VERIFIED_CONFLICT"
    assert out.reason == "两条快讯的核心数值不同，属于同一槽位明确冲突，因此判定为不重复。"
    assert out.judge_diagnostics.get("judge.core_conflict.numeric") == 1
    assert not {k: v for k, v in out.judge_diagnostics.items()
                if "backfill" in k}


# ============================================================ 4. R7 端到端（semantic 真件链）

TSMC_H = "台积电9月24日公布二季度营收250亿美元，全球市占率69%。"
TSMC_C = "该公司9月24日公布二季度营收250亿美元，全球市占率69%。"


def _tsmc_responses():
    return {
        (TSMC_H, TSMC_C): _jjson(
            "重复", "主体单方缺失高置信对齐：多角色数值一致+明确事件+时间。",
            ("营收250亿美元",), ("营收250亿美元",),
            na=("250亿",), nb=("250亿",),
            ta=("9月24日",), tb=("9月24日",)),
        (TSMC_C, TSMC_H): _jjson(
            "重复", "主体单方缺失高置信对齐：多角色数值一致+明确事件+时间。",
            ("营收250亿美元",), ("营收250亿美元",),
            na=("250亿",), nb=("250亿",),
            ta=("9月24日",), tb=("9月24日",)),
    }


def test_r7_tsmc_type_backfill_duplicate(monkeypatch):
    """R7 正钉①（台积电市占率型）：一方缺主体+多字段一致（营收250亿
    美元+市占率69%两角色数值+9月24日时间）→**重复**（六条件满足，
    原因码"主体单方缺失高置信对齐"随证明件留痕）；条件①机器前置硬闸
    触发可观测（unilateral_missing=True）；shadow triggered/signed=1。"""
    spy, mock = _v6_real_callable(monkeypatch, _tsmc_responses())
    out = _decide(TSMC_H, "台积电", TSMC_C, None, spy)
    assert out.decision == "重复"
    assert out.internal_code == "JUDGE_EQUIVALENT"
    assert out.duplicate_ids == ("item-A",)
    assert out.reason == judge_pair.JUDGE_DUPLICATE_REASON
    assert all(s == v6.JUDGE_PROMPT_V6 for s in mock.systems)
    for ctx in spy.ctxs:
        gate = ctx["machine_evidence"]["subject_backfill"]
        assert gate["unilateral_missing"] is True
        assert gate["history_has_subject"] is True     # 主体抽取现有件：台积电
        assert gate["current_has_subject"] is False
        assert gate["history_subjects"] == ["台积电"]
    # 原因码随证明件留痕（判官理由通道）
    assert all("主体单方缺失高置信对齐" in p["reason"] for p in spy.proofs)
    # shadow 三计数：triggered/signed=1、vetoed 缺席
    assert out.judge_diagnostics["judge.backfill.triggered"] == 1
    assert out.judge_diagnostics["judge.backfill.signed"] == 1
    assert "judge.backfill.vetoed" not in out.judge_diagnostics


STQ_H = "*ST清越（600146）9月24日主力净流入5亿元，收盘价3.21元。"
STQ_C = "公司股票9月24日主力净流入5亿元，收盘价3.21元。"


def _stq_responses():
    return {
        (STQ_H, STQ_C): _jjson(
            "重复", "主体单方缺失高置信对齐：净流入+收盘价双数值一致。",
            ("主力净流入5亿元",), ("主力净流入5亿元",),
            na=("5亿", "3.21"), nb=("5亿", "3.21"),
            ta=("9月24日",), tb=("9月24日",)),
        (STQ_C, STQ_H): _jjson(
            "重复", "主体单方缺失高置信对齐：净流入+收盘价双数值一致。",
            ("主力净流入5亿元",), ("主力净流入5亿元",),
            na=("5亿", "3.21"), nb=("5亿", "3.21"),
            ta=("9月24日",), tb=("9月24日",)),
    }


def test_r7_stqingyue_type_backfill_duplicate(monkeypatch):
    """R7 正钉②（*ST清越型）：证券代码侧明确（600146）+双角色数值
    （主力净流入5亿+收盘价3.21元）→**重复**；条件①硬闸经证券代码
    现有件触发；P_SUBJECT_MISSING 只进审计诊断（机检代码缺失告警），
    不再有证明层一票否决（提交一语义）；shadow triggered/signed=1。"""
    spy, mock = _v6_real_callable(monkeypatch, _stq_responses())
    out = _decide(STQ_H, "*ST清越", STQ_C, None, spy)
    assert out.decision == "重复"
    assert out.internal_code == "JUDGE_EQUIVALENT"
    assert out.duplicate_ids == ("item-A",)
    for ctx in spy.ctxs:
        gate = ctx["machine_evidence"]["subject_backfill"]
        assert gate["unilateral_missing"] is True
        assert gate["history_codes"] == ["600146"]     # 证券代码现有件
        assert gate["current_codes"] == []
        assert gate["current_has_subject"] is False
    assert all("主体单方缺失高置信对齐" in p["reason"] for p in spy.proofs)
    assert out.judge_diagnostics["judge.backfill.triggered"] == 1
    assert out.judge_diagnostics["judge.backfill.signed"] == 1
    assert "judge.backfill.vetoed" not in out.judge_diagnostics
    # P_SUBJECT_MISSING 审计诊断在案（语义权威下不再否决签发）
    assert out.pair_results[0].machine_findings.count("P_SUBJECT_MISSING") >= 1


BOTHMISS_H = "9月24日公布二季度营收250亿美元，全球市占率69%。"
BOTHMISS_C = "9月25日公布二季度营收250亿美元，全球市占率69%。"


def test_r7_counter_both_missing_subject_boundary(monkeypatch):
    """R7 反例①：双方都缺主体+其余全一致→回填**不得触发**（条件①机器
    前置硬闸不置位、both_missing=True）→判官存疑→边界（JUDGE_UNCERTAIN）；
    shadow 三计数全部缺席（triggered 都不发生）。"""
    responses = {
        (BOTHMISS_H, BOTHMISS_C): _jjson("存疑", "双方都缺主体，无法唯一确认。",
                                         ("营收250亿美元",), ("营收250亿美元",)),
        (BOTHMISS_C, BOTHMISS_H): _jjson("存疑", "双方都缺主体，无法唯一确认。",
                                         ("营收250亿美元",), ("营收250亿美元",)),
    }
    spy, mock = _v6_real_callable(monkeypatch, responses)
    out = _decide(BOTHMISS_H, None, BOTHMISS_C, None, spy)
    assert out.decision == "边界case/疑难case"
    assert out.internal_code == "JUDGE_UNCERTAIN"
    assert len(mock.calls) == 2                      # 判官照常被调用
    for ctx in spy.ctxs:
        gate = ctx["machine_evidence"]["subject_backfill"]
        assert gate["both_missing"] is True
        assert gate["unilateral_missing"] is False
    assert not {k: v for k, v in out.judge_diagnostics.items()
                if "backfill" in k}


DIFFSUBJ_H = "甲公司公告回购股份。"
DIFFSUBJ_C = "乙公司公告回购股份。"


def test_r7_counter_different_subjects_not_duplicate(monkeypatch):
    """R7 反例②：甲公司 vs 乙公司（双方都明确写主体且不同）→**不重复**
    （现状已是——判官 v6 条款"双方都写主体但不一致时按一般不重复规则
    处理"）；条件①硬闸不置位（both present），shadow 零计数。"""
    responses = {
        (DIFFSUBJ_H, DIFFSUBJ_C): _jjson("不重复", "主体不一致。",
                                         ("公告回购股份",), ("公告回购股份",)),
        (DIFFSUBJ_C, DIFFSUBJ_H): _jjson("不重复", "主体不一致。",
                                         ("公告回购股份",), ("公告回购股份",)),
    }
    spy, mock = _v6_real_callable(monkeypatch, responses)
    out = _decide(DIFFSUBJ_H, "甲公司", DIFFSUBJ_C, "乙公司", spy)
    assert out.decision == "不重复"
    assert out.internal_code == "JUDGE_NON_DUPLICATE"
    for ctx in spy.ctxs:
        gate = ctx["machine_evidence"]["subject_backfill"]
        assert gate["unilateral_missing"] is False
        assert gate["history_has_subject"] is True
        assert gate["current_has_subject"] is True
    assert not {k: v for k, v in out.judge_diagnostics.items()
                if "backfill" in k}


VANKE_H = "万科9月24日公告以2.3亿元竞得一宗地块。"
VANKE_C = "该公司9月24日公告以2.3亿元竞得一宗地块，保利发展亦参与竞价。"


def test_r7_counter_another_explicit_subject_vetoed(monkeypatch):
    """R7 反例③（万科剧本：同行业同日同类型同金额）：A 写万科、B 写
    "该公司"且 B 正文出现另一个明确主体（保利发展）→条件④（缺主体一
    方正文中没有出现另一个明确主体）拦截→判存疑→边界；回填已触发但
    被否决：shadow triggered=1/vetoed=1、signed 缺席。"""
    responses = {
        (VANKE_H, VANKE_C): _jjson("存疑", "正文出现另一明确主体，条件④不满足。",
                                   ("以2.3亿元竞得",), ("以2.3亿元竞得",)),
        (VANKE_C, VANKE_H): _jjson("存疑", "正文出现另一明确主体，条件④不满足。",
                                   ("以2.3亿元竞得",), ("以2.3亿元竞得",)),
    }
    spy, mock = _v6_real_callable(monkeypatch, responses)
    out = _decide(VANKE_H, "万科", VANKE_C, None, spy)
    assert out.decision == "边界case/疑难case"
    assert out.internal_code == "JUDGE_UNCERTAIN"
    for ctx in spy.ctxs:
        gate = ctx["machine_evidence"]["subject_backfill"]
        assert gate["unilateral_missing"] is True
    assert out.judge_diagnostics["judge.backfill.triggered"] == 1
    assert out.judge_diagnostics["judge.backfill.vetoed"] == 1
    assert "judge.backfill.signed" not in out.judge_diagnostics


NEWFACT_H = "万科9月24日公告净利润100万元。"
NEWFACT_C = "该公司9月24日公告净利润100万元，经营现金流净额50万元。"


def test_r7_counter_new_independent_fact_vetoed(monkeypatch):
    """R7 反例④：一方缺主体且另一侧有新增独立事实（净利润100万+
    经营现金流净额50万双指标）→条件⑤（无新增独立事实：新指标不算
    信息补充）拦截→判存疑→边界；shadow triggered=1/vetoed=1。"""
    responses = {
        (NEWFACT_H, NEWFACT_C): _jjson("存疑", "新增独立事实（现金流指标），条件⑤不满足。",
                                       ("净利润100万元",), ("净利润100万元",)),
        (NEWFACT_C, NEWFACT_H): _jjson("存疑", "新增独立事实（现金流指标），条件⑤不满足。",
                                       ("净利润100万元",), ("净利润100万元",)),
    }
    spy, mock = _v6_real_callable(monkeypatch, responses)
    out = _decide(NEWFACT_H, "万科", NEWFACT_C, None, spy)
    assert out.decision == "边界case/疑难case"
    assert out.internal_code == "JUDGE_UNCERTAIN"
    for ctx in spy.ctxs:
        gate = ctx["machine_evidence"]["subject_backfill"]
        assert gate["unilateral_missing"] is True
    assert out.judge_diagnostics["judge.backfill.triggered"] == 1
    assert out.judge_diagnostics["judge.backfill.vetoed"] == 1
    assert "judge.backfill.signed" not in out.judge_diagnostics


# ============================================================ 5. shadow 三计数可观测

def test_backfill_shadow_counters_semantic_only_not_public(monkeypatch):
    """shadow 三计数（现状机制=DecideOutcome.judge_diagnostics 结构化
    计数器）：semantic 模式下台积电型对——triggered/signed 在案、
    vetoed 缺席；公共五字段封闭（计数绝不进公共面）；legacy 模式同一
    对零计数（机器证据零装配=零触发）。"""
    spy, _ = _v6_real_callable(monkeypatch, _tsmc_responses())
    out = _decide(TSMC_H, "台积电", TSMC_C, None, spy)
    assert out.judge_diagnostics["judge.backfill.triggered"] == 1
    assert out.judge_diagnostics["judge.backfill.signed"] == 1
    assert "judge.backfill.vetoed" not in out.judge_diagnostics
    # 公共五字段封闭（计数不泄漏）
    public = out.to_public_dict()
    assert set(public) == {"item_id", "text", "decision",
                           "duplicate_ids", "reason"}
    assert "backfill" not in json.dumps(public, ensure_ascii=False)
    # legacy 同对：机器证据零装配、零计数（默认模式生产零变化）
    _legacy_env(monkeypatch)
    mock2 = _MockLLM(_tsmc_responses())
    judge2 = lr.SyncResidualJudge(
        lr.ResidualJudgeConfig(prompt_version="judge_v6"),
        call_fn=mock2)
    cb2 = judge_adapter.build_judge_callable(judge=judge2)
    assert cb2 is not None
    spy2 = _spy_wrap(cb2)
    out2 = _decide(TSMC_H, "台积电", TSMC_C, None, spy2)
    assert out2.decision == "重复"
    assert out2.internal_code == "FACT_EQUIVALENT"      # legacy 合并口径
    assert not {k: v for k, v in out2.judge_diagnostics.items()
                if "backfill" in k}
    # 机器证据零装配（legacy 面判官上下文无 machine_evidence 键）
    assert spy2.ctxs
    assert all("machine_evidence" not in ctx for ctx in spy2.ctxs)


# ============================================================ 6. 版本装配单源钉（semantic= v6+policy_v4+真实 SHA）

def test_semantic_mode_assembly_v6_real_sha(monkeypatch):
    """版本映射钉：semantic 模式装配=judge_v6+policy_v4+真实 SHA——
    adapter 默认配置（default_judge_config）、run_manifest 缺省登记、
    罐装真件证明四维（prompt_sha256/policy_version/cache_key）三处同源。"""
    _semantic_env(monkeypatch)
    env = {"DEDUP_JUDGE_PROOF": "1",
           "DEDUP_JUDGE_DECISION_MODE": "semantic_authority"}
    vc = jvc.default_judge_version(env)
    assert (vc.prompt_version, vc.prompt_sha256, vc.policy_version) == (
        "judge_v6", v6.PROMPT_SHA256_V6, "policy_v4")
    cfg = judge_adapter.default_judge_config(env)
    assert cfg.prompt_version == "judge_v6"
    assert cfg.mv_mode == lr.MV_AUDIT
    manifest = rm.build_run_manifest(
        inputs=("rec-a",), embedding_space="fake_space",
        code_git_sha="0" * 40, env=env)
    assert manifest.prompt_version == "judge_v6"
    assert manifest.prompt_sha256 == v6.PROMPT_SHA256_V6
    assert manifest.policy_version == "policy_v4"
    # 罐装真件证明四维同源（adapter 实跑产物==manifest 记录）
    t_h = "甲公司9月24日公告营收100万元。"
    t_c = "甲公司公告称，9月24日营收为100万元。"
    resp = json.dumps({
        "decision": "重复", "reason": "r",
        "evidence_a": ["100万元"], "evidence_b": ["100万元"],
        "numeric_check": {"conclusion": "一致", "numbers_a": ["100万"],
                          "numbers_b": ["100万"]},
        "time_check": {"conclusion": "一致", "times_a": ["9月24日"],
                       "times_b": ["9月24日"]},
    }, ensure_ascii=False)
    judge = lr.SyncResidualJudge(
        lr.ResidualJudgeConfig(prompt_version="judge_v6"),
        call_fn=lambda model, system, user, timeout_s=None: (resp, 0.01))
    cb = judge_adapter.build_judge_callable(judge=judge, environ=env)
    assert cb is not None
    ctx = {"pair_id": "p-v6-ver", "order": "ab",
           "item_a_id": "item-A", "item_b_id": "item-C",
           "record_a_id": H_ID, "record_b_id": C_ID,
           "text_a": t_h, "text_b": t_c}
    proof = cb(ctx)
    assert proof["prompt_sha256"] == v6.PROMPT_SHA256_V6 == \
        vc.prompt_sha256 == manifest.prompt_sha256
    assert proof["policy_version"] == "policy_v4" == \
        vc.policy_version == manifest.policy_version
    assert proof["cache_key"] == judge_pair.compute_cache_key(
        proof["model_version"], vc.prompt_sha256, vc.policy_version,
        "ab", proof["text_a_sha256"], proof["text_b_sha256"])


# ============================================================ 7. legacy 全等回归（§7 钉测模式）

def test_legacy_default_mode_revision_pair_baseline_equal(monkeypatch):
    """legacy 全等①：默认模式（env 缺席）下 R8 修订对——判官照常被调用
    （双序实调）、机器候选证据零装配（判官上下文无 machine_evidence 键、
    R8 证据不可观测）、shadow 零计数；判官双序重复→基线字面全等
    （重复/FACT_EQUIVALENT/基线文案+公共五字段逐字段）。"""
    monkeypatch.delenv(judge_pair.JUDGE_DECISION_MODE_ENV, raising=False)
    judge = _judge(lambda ctx: _make_proof(ctx, verdict="duplicate"))
    out = _decide(R8_H, "甲公司", R8_C, "甲公司", judge)
    assert [c["order"] for c in judge.calls] == ["ab", "ba"]
    # 机器证据零装配（legacy 面上下文键集=基线）
    for ctx in judge.calls:
        assert "machine_evidence" not in ctx
    assert not {k: v for k, v in out.judge_diagnostics.items()
                if "backfill" in k}
    # 基线字面全等（§7 模式：码/文案/公共五字段逐字）
    assert out.decision == "重复"
    assert out.internal_code == "FACT_EQUIVALENT"
    assert out.duplicate_ids == ("item-A",)
    assert out.pair_results[0].code == "FACT_EQUIVALENT"
    assert out.reason == (
        "判官双序一致判定同一事实，引文已绑定双侧原文且机器验 gate 通过。")
    assert out.to_public_dict() == {
        "item_id": "item-C", "text": R8_C, "decision": "重复",
        "duplicate_ids": ["item-A"],
        "reason": "判官双序一致判定同一事实，引文已绑定双侧原文且机器验 gate 通过。"}


def test_legacy_default_mode_r7_pair_no_backfill(monkeypatch):
    """legacy 全等②：默认模式下 R7 台积电型对（单侧缺主体）——判官
    照常被调用且机器证据零装配（条件①硬闸零评估）、shadow 零计数；
    判官双序存疑→基线未决口径（SUBJECT_UNRESOLVED/边界）；公共五字段
    封闭不变。"""
    monkeypatch.delenv(judge_pair.JUDGE_DECISION_MODE_ENV, raising=False)
    judge = _judge(lambda ctx: _make_proof(ctx, verdict="doubtful"))
    out = _decide(TSMC_H, "台积电", TSMC_C, None, judge)
    assert [c["order"] for c in judge.calls] == ["ab", "ba"]
    for ctx in judge.calls:
        assert "machine_evidence" not in ctx
    assert out.decision == "边界case/疑难case"
    assert out.internal_code == "SUBJECT_UNRESOLVED"
    assert out.reason == "判官未决进人工（JUDGE_DOUBTFUL）。"
    assert not {k: v for k, v in out.judge_diagnostics.items()
                if "backfill" in k}
    assert set(out.to_public_dict()) == {
        "item_id", "text", "decision", "duplicate_ids", "reason"}
