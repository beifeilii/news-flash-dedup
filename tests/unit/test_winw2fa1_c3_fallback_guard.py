"""W2Fα1 条目23（WA1a-C3）：分隔符-only 文本 IndexError 兜底守卫。

rule.py fallback minimal fact 与 llm.py _fallback_fact 对 "。"类无实义子句
文本执行 `_clauses(text)[0]` —— _clauses 对纯分隔符/全空白子句文本返回
空列表，裸 IndexError 逃逸声明通道（入口判空闸不覆盖：text.strip() 非空）。
修法：空 clauses 归"空文本/全空白 → []"同一声明路径（不足证据，调用方
判空接线），两侧同纪律。
"""

from __future__ import annotations

import pytest

from news_flash_dedup.facts import llm as facts_llm
from news_flash_dedup.facts import rule as facts_rule

RID = "a" * 64


def _empty_facts_call_fn(**_kwargs):
    """零事实 LLM mock（触发 _fallback_fact 路径）。"""
    return '{"facts": []}', 0.01


# ---------- 红测（修复前抛 IndexError） ----------

@pytest.mark.parametrize("text", ["。", "！？；。", "！？；", " 。", "。！？ ",
                                  "；；", "。\n。"])
def test_rule_separator_only_text_returns_empty(text):
    """rule：分隔符-only/无实义子句文本 → []（不足证据声明路径，不抛 IndexError）。"""
    assert facts_rule.extract_facts(RID, text) == []


@pytest.mark.parametrize("text", ["。", "！？；"])
def test_rule_separator_only_text_returns_empty_v2(text):
    """rule v2 词典腿同纪律。"""
    assert facts_rule.extract_facts(
        RID, text, dict_version=facts_rule.RULE_DICT_VERSION_V2) == []


@pytest.mark.parametrize("text", ["。", "！？；。"])
def test_llm_separator_only_text_returns_empty(text):
    """llm：零事实兜底遇分隔符-only 文本 → []（不抛 IndexError）。"""
    facts = facts_llm.extract_facts_llm(RID, text, call_fn=_empty_facts_call_fn)
    assert facts == []


def test_llm_separator_only_cached_path_returns_empty(tmp_path):
    """llm 磁盘缓存命中路径同款守卫（_fallback_fact 第二调用点）。"""
    cache_dir = str(tmp_path)
    first = facts_llm.extract_facts_llm(RID, "。", call_fn=_empty_facts_call_fn,
                                        cache_dir=cache_dir)
    assert first == []
    second = facts_llm.extract_facts_llm(RID, "。", call_fn=_empty_facts_call_fn,
                                         cache_dir=cache_dir)
    assert second == []


# ---------- 前后均绿守卫（正常路径零漂移） ----------

def test_rule_normal_text_still_extracts():
    facts = facts_rule.extract_facts(RID, "甲公司宣布回购股份。")
    assert len(facts) >= 1
    assert facts[0]["event_state"]["predicate"]["status"] == "present"


def test_rule_blank_text_still_empty():
    assert facts_rule.extract_facts(RID, "   ") == []
    assert facts_rule.extract_facts(RID, "") == []


def test_llm_normal_text_fallback_fact_unchanged():
    """非空可切子句文本零事实 → 1 个 fallback minimal fact（现役契约保持）。"""
    facts = facts_llm.extract_facts_llm(RID, "今天天气不错。",
                                        call_fn=_empty_facts_call_fn)
    assert len(facts) == 1
    assert facts[0]["fact_id"] == "f1"
    assert facts[0]["subject"]["status"] == "missing"
