# -*- coding: utf-8 -*-
"""W3F 红测（W3a-F6①）：llm modality 槽 pred_start==0 退化窗同源收口。

缺陷面（facts/llm.py:393 现役）：`hi=pred_start if pred_start else None`——
谓词居文首（pred_start==0）时 hi=None 退化为**全文窗首命中**（modality
无 prefer_before，与 subject 槽 D8 已修的末命中形态不同但同属空窗退化）：
文首谓词可误取谓词后情态词。D8（subject 槽 :349-352）已修为 hi=pred_start
恒值（==0 → 空窗 → missing 显式）；本钉钉死 modality 槽同款收口。

探针（log\\temp\\winw3f-probe-f6-modality.json，修前金标 llm 缓存回放）：
facts=3006 / modality_present=36 / pred_at_zero=0 / zero_mod_present=0 /
after_pred=0 / would_change=0——退化窗金标零触发，收口=零翻转面
（与 D8 subject 面 pred_at_zero=0 同型安全裁定）。
"""

from __future__ import annotations

import inspect
import json

from news_flash_dedup.facts import llm as facts_llm

from tests.unit.test_facts_rule import assert_g1_evidence_real, assert_g2_shape

RID = "a" * 64


def _extract(text: str, payload: dict, record_id: str = RID, **kwargs):
    facts = facts_llm.extract_facts_llm(record_id, text, **kwargs)
    assert_g1_evidence_real(text, record_id, facts)
    assert_g2_shape(facts)
    return facts


def _call_fn(payload: dict):
    def fn(**_kwargs):
        return json.dumps(payload, ensure_ascii=False), 0.01
    return fn


# ---------- 红测（修复前 modality 槽全文窗退化命中） ----------

def test_f6a_modality_at_pred_start_zero_no_fulltext_degradation():
    """谓词居文首（pred_start==0）+ 情态词在谓词之后——修复前 hi=None
    全文窗首命中误收"将"（present 且 span 在谓词后）；修复后 hi=0 空窗
    → missing + issue 登记（宁缺勿编，D8 同款显式化）。"""
    text = "终止了协议，公司将回购股份。"
    payload = {"facts": [{
        "subject": None, "predicate": "终止", "polarity": None,
        "modality": "将", "key_object": None, "time_expression": None,
        "time_stage": None, "attribution": None, "numerics": []}]}
    issues: list[dict] = []
    facts = _extract(text, payload, call_fn=_call_fn(payload),
                     report_hook=issues.append)
    modality = facts[0]["event_state"]["modality"]
    assert modality["status"] == "missing", (
        f"pred_start==0 时 modality 不得经全文窗命中谓词后情态词；"
        f"实测 status={modality['status']} span="
        f"{modality['evidence'][0] if modality['evidence'] else None}")
    hooked = issues[0]["issues"]
    assert any("modality" in i and "将" in i for i in hooked), \
        "missing 化必须经 issue 登记留痕（宁缺勿编纪律）"


def test_f6a_modality_fulltext_window_pattern_removed_from_source():
    """机制钉（源码面）：_facts_from_llm_payload 不再含
    `hi=pred_start if pred_start else None` 退化式（D8 同源收口）。"""
    src = inspect.getsource(facts_llm._facts_from_llm_payload)
    code_hits = [line for line in src.splitlines()
                 if "hi=pred_start if pred_start else None" in line
                 and not line.strip().startswith("#")]
    assert code_hits == [], \
        f"modality 槽 pred_start==0 退化窗仍在（D8 同型残留）：{code_hits}"


# ---------- 前后均绿守卫 ----------

def test_f6a_modality_before_predicate_still_found():
    """守卫（前后同绿）：谓词非文首时搜索窗 [0, pred_start) 两式同值——
    谓词前情态词照常命中、span 逐字节不变。"""
    text = "公司将回购股份。"
    payload = {"facts": [{
        "subject": "公司", "predicate": "回购", "polarity": None,
        "modality": "将", "key_object": None, "time_expression": None,
        "time_stage": None, "attribution": None, "numerics": []}]}
    facts = _extract(text, payload, call_fn=_call_fn(payload))
    modality = facts[0]["event_state"]["modality"]
    assert modality["status"] == "present"
    assert modality["raw_value"] == "将"
    span = modality["evidence"][0]
    assert (span["start"], span["end"]) == (2, 3)     # "将" 在 "回购"(3-5) 之前


def test_f6a_modality_missing_when_quote_absent_unchanged():
    """守卫（前后同绿）：情态引词原文不存在 → missing + issue（现役契约）。"""
    text = "公司回购股份。"
    payload = {"facts": [{
        "subject": "公司", "predicate": "回购", "polarity": None,
        "modality": "拟", "key_object": None, "time_expression": None,
        "time_stage": None, "attribution": None, "numerics": []}]}
    facts = _extract(text, payload, call_fn=_call_fn(payload))
    assert facts[0]["event_state"]["modality"]["status"] == "missing"


def test_f6a_subject_d8_closure_unchanged_by_modality_fix():
    """守卫（前后同绿）：D8 subject 槽收口不被本修触碰——谓词居文首时
    subject 仍走空窗 missing → 谓词后窗兜底（倒装标题路径）。"""
    text = "回购股份，甲公司今日宣布。"
    payload = {"facts": [{
        "subject": "甲公司", "predicate": "回购", "polarity": None,
        "modality": None, "key_object": None, "time_expression": None,
        "time_stage": None, "attribution": None, "numerics": []}]}
    facts = _extract(text, payload, call_fn=_call_fn(payload))
    subject = facts[0]["subject"]
    assert subject["status"] == "present"              # 兜底窗命中（谓词后）
    assert subject["raw_value"] == "甲公司"
