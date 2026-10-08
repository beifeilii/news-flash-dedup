"""D19 杠杆 a（rule_baseline_v2）契约单元测试。

D19 契约测试，src 实现主窗口进行中，当前红属预期（本文件只新增，不改 src/既有测试）。
契约来源：`log/D19-词典归一化设计冻结.md` §二-杠杆 a，逐条对应任务条款 1-4：

- 条款 1：`extract_facts(record_id, text, *, dict_version=RULE_DICT_VERSION)` 新增
  keyword-only 参数；默认（v1）行为与现状逐字节一致；
  `dict_version="rule_dict_v2"` 走 `_EVENT_VERBS + _EVENT_VERBS_V2_EXTRA`（纯追加新组）；
  非法版本串 raise ValueError。
- 条款 2：v1 命中文本稳定性回归——对一组 v1 已命中文本（覆盖四组动词 + 否定极性 +
  时间/数值槽），v2 模式输出 facts 与 v1 模式 json.dumps(sort_keys) 全等
  （冻结文档 §二"追加安全性（规则+测试双重）"）。
- 条款 3：v2 新动词命中——V2_VERB_SAMPLES 参数化驱动，每条新动词能产出非 fallback
  fact，且 fact_type/predicate raw=该动词、evidence 码点回核通过。
- 条款 4：人名主体模式（v2）——职务+人名型（"欧洲央行行长拉加德表示…"）subject
  非 missing、raw 含人名；具体模式以实现为准，本测试按 subject.status=='present' 断言。
"""

from __future__ import annotations

import json

import pytest

from news_flash_dedup.facts import rule as facts_rule


RECORD_A = "a" * 64
V2_DICT_VERSION = "rule_dict_v2"

# ---------------------------------------------------------------------------
# 占位参数化表（任务条款 3）：(v2 新动词, 该动词应命中的样例句)。
# 主窗口实现时以 S1 逐动词计数清单的实际词条回填本表；当前五个样例按任务给定先写。
# 选句纪律：样例句在 v1 模式下不得命中任何 v1 动词（当前实测五句均为单条 fallback
# minimal fact），以便同用例顺带断言"v1 未覆盖、v2 新增命中"的增量语义。
# ---------------------------------------------------------------------------
V2_VERB_SAMPLES = [
    ("放缓", "欧洲央行行长拉加德表示，就业增长持续放缓。"),
    ("上涨", "美元指数上涨0.03%。"),
    ("下跌", "土耳其主要银行指数下跌1%。"),
    ("收于", "美元指数在汇市尾市收于98.81。"),
    ("举行", "中欧双方举行经贸会谈。"),
]

# v1 已命中文本稳定性样本（任务条款 2）：覆盖四组动词 + 否定极性 + 时间/数值槽。
# 选句纪律：样本句不得含 v2 追加表（_EVENT_VERBS_V2_EXTRA）中任何动词——追加词
# 在 v1 未覆盖位置的新增命中属 v2 预期扩张而非漂移，但会使 json 全等断言失去意义。
# 已按当前 _EVENT_VERBS_V2_EXTRA 逐条核对（如"宣布/增长"均在表内，样本句已避开）。
V1_HIT_TEXTS = [
    "甲公司收购乙公司。",                  # 公司行动
    "乙集团发布年度报告。",                # 信息披露
    "丙公司上调产品价格。",                # 价格产量
    "央行降息25个基点。",                  # 金融事件 + 数值槽
    "甲公司未完成回购计划。",              # 否定极性（松散窗 polarity=未完成回购）
    "甲公司2026年9月9日回购100万股。",      # 时间槽 + 数值槽
]


def _canonical_dump(facts: list[dict]) -> str:
    """条款 2 指定比对口径：json.dumps(sort_keys)。"""
    return json.dumps(facts, ensure_ascii=False, sort_keys=True)


def _assert_evidence_real(text: str, record_id: str, facts: list[dict]) -> None:
    """evidence 码点回核（与 test_facts_rule.py G1 同姿势）。"""

    def walk(node):
        if isinstance(node, dict):
            for entry in node.get("evidence") or []:
                assert entry["record_id"] == record_id
                start, end, quote = entry["start"], entry["end"], entry["quote"]
                assert 0 <= start < end <= len(text)
                assert text[start:end] == quote
            for child in node.values():
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(facts)


# ---------- 条款 1：keyword-only dict_version 参数 ----------

def test_default_call_byte_identical_to_explicit_v1():
    """默认 dict_version=RULE_DICT_VERSION（v1）与显式 v1 逐字节一致。"""
    text = "甲公司2026年9月9日回购100万股。"
    facts_default = facts_rule.extract_facts(RECORD_A, text)
    facts_v1 = facts_rule.extract_facts(RECORD_A, text, dict_version="rule_dict_v1")
    assert _canonical_dump(facts_default) == _canonical_dump(facts_v1)
    _assert_evidence_real(text, RECORD_A, facts_v1)


def test_rule_dict_version_constant_anchors_v1():
    """默认锚：RULE_DICT_VERSION 仍指 v1（条款 1"默认 v1 行为与现状逐字节一致"的前提）。

    注：冻结文档 §二另有"RULE_DICT_VERSION → rule_dict_v2"表述，与默认参数
    `dict_version=RULE_DICT_VERSION` + 默认 v1 零漂移组合解读为：常量本身不动、
    v2 仅以版本串 "rule_dict_v2" 可选启用（歧义已在回报中列出）。
    """
    assert facts_rule.RULE_DICT_VERSION == "rule_dict_v1"


def test_dict_version_is_keyword_only():
    """dict_version 为 keyword-only：位置传参一律 TypeError（实现前后均须成立）。"""
    with pytest.raises(TypeError):
        facts_rule.extract_facts(RECORD_A, "甲公司收购乙公司。", "rule_dict_v2")


@pytest.mark.parametrize("bad_version", ["", "dict_v1", "rule_dict_v0", "rule_dict_v3", "v2"])
def test_invalid_dict_version_raises_value_error(bad_version):
    """非法版本串 raise ValueError（对齐词典 fail-closed 口径）。"""
    with pytest.raises(ValueError):
        facts_rule.extract_facts(RECORD_A, "甲公司收购乙公司。", dict_version=bad_version)


# ---------- 条款 2：v1 命中文本稳定性回归（v2 纯追加安全性） ----------

@pytest.mark.parametrize("text", V1_HIT_TEXTS)
def test_v1_hit_texts_byte_stable_under_v2(text):
    """v1 已命中文本：v2 模式输出 facts 与 v1 模式 json.dumps(sort_keys) 全等。"""
    facts_default = facts_rule.extract_facts(RECORD_A, text)
    assert any(f["fact_type"]["status"] == "present" for f in facts_default), (
        "样本必须在 v1 已命中（非 fallback），否则稳定性断言失去意义"
    )
    facts_v1 = facts_rule.extract_facts(RECORD_A, text, dict_version="rule_dict_v1")
    facts_v2 = facts_rule.extract_facts(RECORD_A, text, dict_version=V2_DICT_VERSION)
    assert _canonical_dump(facts_default) == _canonical_dump(facts_v1)
    assert _canonical_dump(facts_default) == _canonical_dump(facts_v2), (
        "条款 2：v2 纯追加不得改变 v1 已命中文本的抽取结果（含 evidence 偏移）"
    )


# ---------- 条款 3：v2 新动词命中 ----------

@pytest.mark.parametrize("verb,sentence", V2_VERB_SAMPLES)
def test_v2_new_verb_produces_non_fallback_fact(verb, sentence):
    """每条 v2 新动词：产出非 fallback fact，fact_type/predicate raw=该动词。"""
    facts_v2 = facts_rule.extract_facts(RECORD_A, sentence, dict_version=V2_DICT_VERSION)
    hits = [f for f in facts_v2 if f["event_state"]["predicate"]["raw_value"] == verb]
    assert hits, f"条款 3：v2 新动词 {verb!r} 应产出非 fallback fact"
    for fact in hits:
        assert fact["fact_type"]["status"] == "present"
        assert fact["fact_type"]["raw_value"] == verb
        assert fact["event_state"]["predicate"]["status"] == "present"
    _assert_evidence_real(sentence, RECORD_A, facts_v2)
    # 增量语义：同句在 v1（默认）模式下不得命中该动词
    facts_v1 = facts_rule.extract_facts(RECORD_A, sentence)
    assert all(
        f["event_state"]["predicate"]["raw_value"] != verb for f in facts_v1
    ), "样例句须在 v1 未覆盖域（v1 不命中该动词），否则 v2 增量语义不成立"


# ---------- 条款 4：人名主体模式（v2） ----------

def test_v2_person_name_subject_present():
    """条款 4 主断言（任务给定样例句）：v2 模式 subject.status=='present'。

    实现注记：本句窗口内 v1 P2 机构后缀（央行）优先命中——v2 扩展一律排在
    v1 优先级之后（零漂移机制），故人名模式不在本句触发，subject raw 为机构名；
    "raw 含人名"断言见 test_v2_person_name_subject_raw_contains_name。
    """
    text = "欧洲央行行长拉加德表示，就业增长持续放缓。"
    facts_v2 = facts_rule.extract_facts(RECORD_A, text, dict_version=V2_DICT_VERSION)
    real_facts = [f for f in facts_v2
                  if f["event_state"]["predicate"]["status"] == "present"]
    assert real_facts, "v2 应命中事件动词（放缓族）产出非 fallback fact"
    assert any(
        f["subject"]["status"] == "present" for f in real_facts
    ), "条款 4：职务+人名型主体 subject.status 须为 present（非 missing）"
    _assert_evidence_real(text, RECORD_A, facts_v2)
    # v1 零漂移对照：默认（v1）模式不得新增人名主体
    facts_v1 = facts_rule.extract_facts(RECORD_A, text)
    assert all(
        "拉加德" not in (f["subject"]["raw_value"] or "") for f in facts_v1
    ), "v1 模式不得因 v2 人名主体模式而漂移"


@pytest.mark.parametrize("text", [
    "行长拉加德表示，就业增长持续放缓。",      # P2b 职务+人名模式触发窗
    "拉加德表示，将维持当前利率水平。",        # P3a 人名小词典触发窗（尾段副词"将"
                                             # →承前省略回退全窗；旧用例"…，就业增长
                                             # 持续放缓"尾段自带主体"就业"，跨逗号继承
                                             # 已于腿⑱语义更正——2a2ca73d 错挂实证）
])
def test_v2_person_name_subject_raw_contains_name(text):
    """条款 4 人名模式实际触发（v1 P1-P3 均不命中的窗口）：subject raw 含人名。

    只按契约断言"含人名"，不钉人名边界：现状 P2b 职务词后 [一-龥]{2,4} 贪婪
    会过捕后续动词首字（如"行长拉加德表"），该实现质量点已在回报评审意见中列出。
    腿⑱语义更正：P3a 跨逗号继承仅限尾段副词回退（承前省略），尾段自带主体
    （就业/博通）时人名不再错挂（2a2ca73d 实证，D19 §5.11）。
    """
    facts_v2 = facts_rule.extract_facts(RECORD_A, text, dict_version=V2_DICT_VERSION)
    real_facts = [f for f in facts_v2
                  if f["event_state"]["predicate"]["status"] == "present"]
    assert real_facts, "v2 应命中事件动词（放缓族）产出非 fallback fact"
    assert any(
        f["subject"]["status"] == "present"
        and "拉加德" in (f["subject"]["raw_value"] or "")
        for f in real_facts
    ), "条款 4：人名主体模式触发时 subject raw 须含人名"
    _assert_evidence_real(text, RECORD_A, facts_v2)


# ---------- 条款 1 附：v2 模式边界域行为不变（纯追加不放大命中面以外行为） ----------

def test_v2_empty_text_returns_empty_list():
    """空文本/全空白 → []（T024 边界域，v2 模式保持一致）。"""
    assert facts_rule.extract_facts(RECORD_A, "", dict_version=V2_DICT_VERSION) == []
    assert facts_rule.extract_facts(RECORD_A, "   ", dict_version=V2_DICT_VERSION) == []


def test_v2_fallback_shape_preserved_for_uncovered_text():
    """v1/v2 均未覆盖文本 → 恰 1 个 fallback minimal fact（v2 不消灭 fallback 路径）。"""
    text = "今天天气晴朗。"
    facts = facts_rule.extract_facts(RECORD_A, text, dict_version=V2_DICT_VERSION)
    assert len(facts) == 1
    assert facts[0]["fact_id"] == "f1"
    assert facts[0]["fact_type"]["status"] == "missing"
    assert facts[0]["event_state"]["predicate"]["status"] == "missing"
    _assert_evidence_real(text, RECORD_A, facts)
