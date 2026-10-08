"""W2Fα1 条目24（WA1a-C2）：range_end 两产出侧合同统一（探针先已量化）。

rule.py inline range_end=十进制核串（_decimal_text(core)），llm.py 原=
range 槽 raw_value 全串（"1.2%"/"8层" 型含单位后缀）——同一 P15 消费字段
两制不对码，llm 全串在 normalize_numeric._parse_value 必 ValueError →
NUMERIC_ALIGNMENT_FAILED（LLM 腿区间数值结构性不可比）。
探针（log\temp\winw2fa1-probe-c2-range-end.json）：rule v1 3 槽/v2 2 槽全
canonical（0 非核串）；llm 9 槽全非核串=触发面 9/9；回放暴露对 llm 4 对
全金标重复（t0 均边界）→ 修法只可能向 tp+ 向翻转（fp 零暴露）。
修法：llm inline range_end 统一为核串（re_dec；中文核回退 range_core 原串，
与 rule 十进制核串同消费合同）。
"""

from __future__ import annotations

from news_flash_dedup.compare import value_time
from news_flash_dedup.facts import llm as facts_llm
from news_flash_dedup.facts import rule as facts_rule

RID = "a" * 64


def _range_call_fn(range_end_raw: str, unit: str = "点", value_raw: str = "100"):
    def _call_fn(**_kwargs):
        unit_json = f'"{unit}"' if unit else "null"
        return ('{"facts": [{"predicate": "升至", "subject": "指数", '
                '"polarity": null, "modality": null, "key_object": null, '
                '"time_expression": null, "time_stage": null, '
                '"attribution": null, "numerics": [{"value_raw": "' + value_raw + '", '
                '"metric": null, "unit": ' + unit_json + ', "magnitude": null, '
                '"comparator": null, "range_end_raw": "' + range_end_raw + '", '
                '"direction": null}]}]}', 0.01)
    return _call_fn


# ---------- 红测（修复前 llm inline range_end=全串） ----------

def test_c2_llm_inline_range_end_is_core_not_full_string():
    """llm：range_end_raw="200点" → inline range_end="200"（核串，与 rule 同合同）。"""
    facts = facts_llm.extract_facts_llm(
        RID, "指数升至100点至200点。", call_fn=_range_call_fn("200点"))
    numeric = facts[0]["numerics"][0]
    assert numeric["range_end"]["status"] == "present"
    assert numeric["value"]["range_end"] == "200"     # 修复前="200点"（全串）


def test_c2_llm_inline_range_end_percent_suffix():
    """llm："1.2%" 型 → inline "1.2"（探针金标实测形态）。"""
    facts = facts_llm.extract_facts_llm(
        RID, "收益率升至0.5%至1.2%。",
        call_fn=_range_call_fn("1.2%", "%", value_raw="0.5"))
    numeric = facts[0]["numerics"][0]
    assert numeric["value"]["range_end"] == "1.2"


def test_c2_llm_inline_range_end_chinese_core_kept():
    """llm：中文核（十进制解析失败 re_dec=None）→ 回退核原串（_parse_value 中文路径可解）。"""
    facts = facts_llm.extract_facts_llm(
        RID, "指数升至一百点至两百点。",
        call_fn=_range_call_fn("两百点", value_raw="一百"))
    numeric = facts[0]["numerics"][0]
    assert numeric["value"]["range_end"] == "两百"


def test_c2_llm_range_end_parses_under_normalize_contract():
    """统一后 inline range_end 过 normalize_numeric._parse_value（修复前必 ValueError）。"""
    facts = facts_llm.extract_facts_llm(
        RID, "指数升至100点至200点。", call_fn=_range_call_fn("200点"))
    inline = facts[0]["numerics"][0]["value"]["range_end"]
    assert value_time._parse_value(inline.lstrip("+")) is not None


# ---------- 前后均绿守卫 ----------

def test_c2_llm_range_slot_missing_inline_none_unchanged():
    """range 槽缺席 → inline range_end=None（现役契约不动）。"""
    def _call_fn(**_kwargs):
        return ('{"facts": [{"predicate": "升至", "subject": "指数", '
                '"polarity": null, "modality": null, "key_object": null, '
                '"time_expression": null, "time_stage": null, '
                '"attribution": null, "numerics": [{"value_raw": "100", '
                '"metric": null, "unit": "点", "magnitude": null, '
                '"comparator": null, "range_end_raw": null, '
                '"direction": null}]}]}', 0.01)

    facts = facts_llm.extract_facts_llm(RID, "指数升至100点。", call_fn=_call_fn)
    numeric = facts[0]["numerics"][0]
    assert numeric["range_end"]["status"] == "missing"
    assert numeric["value"]["range_end"] is None


def test_c2_rule_inline_range_end_decimal_core_unchanged():
    """rule 侧合同不动：十进制核串（含千分位去逗号）。"""
    facts = facts_rule.extract_facts(RID, "指数从100点升至200点。")
    numerics = [n for fact in facts for n in fact["numerics"]]
    ranged = [n for n in numerics if n["range_end"]["status"] == "present"]
    for numeric in ranged:
        inline = numeric["value"]["range_end"]
        assert value_time._parse_value(inline.lstrip("+")) is not None


def test_c2_llm_range_evidence_still_validates():
    """range 槽 evidence 自证不动：quote=raw 全串且切片自洽（核串 ∈ quote）。"""
    facts = facts_llm.extract_facts_llm(
        RID, "指数升至100点至200点。", call_fn=_range_call_fn("200点"))
    numeric = facts[0]["numerics"][0]
    slot = numeric["range_end"]
    assert slot["raw_value"] == "200点"
    inline = numeric["value"]["range_end"]
    assert inline in slot["raw_value"]           # normalize ∈-quote 校验前提保持
