"""阶段词表扩充红绿钉（主窗口亲修，用户 2026-09-28 裁定批准）。

背景：外部三审 R2-H2 后续——比较层 value_time._STAGES/_STAGE_CONFLICTS
（L73-81）早已认识 同比/环比/初值/终值，但抽取层 rule.py _TIME_STAGE_WORDS
仅盘态词，09 §8.3"对应时间的阶段不同必为不同时间"对这批词不可达。
本批：抽取侧 v2 门控扩充 同比/环比/初值/终值/修正值/月初/月末，
比较侧补 修正值→revised、月初/月末 规范名与三对新冲突对。

红代理纪律：v1 门控缺席钉（test_v1_gate）钉住"改前行为=无阶段槽"，
与 v2 阳性钉共同构成红绿对；v1 腿行为逐字节不变由门控保证+三腿对锚复核。
"""
from __future__ import annotations

import hashlib

import pytest

from news_flash_dedup.compare import pair_alignment, pair_compare, p15_integration
from news_flash_dedup.facts import FactValidationReport
from news_flash_dedup.facts import rule as facts_rule


RECORD_ID_H = "a" * 64
RECORD_ID_C = "b" * 64
PIPELINE_VERSION = "dedup_v1"


def _report(record_id: str, text: str, facts: list) -> FactValidationReport:
    artifact = {
        "schema_version": "1.0",
        "record_id": record_id,
        "offset_unit": "unicode_code_point",
        "extraction_status": "complete",
        "facts": facts,
        "unparsed_spans": [],
        "uncertainties": [],
    }
    return FactValidationReport(
        record_id=record_id,
        validated_complete=bool(facts),
        extraction_status="complete",
        valid_fact_ids=tuple(f.get("fact_id", "") for f in facts),
        numeric_inventory=(),
        issues=(),
        artifact=artifact,
    )


def _ctx(record_id: str, item_id: str, text: str, arrival_seq: int) -> dict:
    return {
        "record_id": record_id, "item_id": item_id, "text": text,
        "raw_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "scope_id": "default", "business_date": "2026-09-26",
        "arrival_seq": arrival_seq, "pipeline_version": PIPELINE_VERSION,
    }


def _stage_values(record_id: str, text: str, dict_version: str) -> list:
    facts = facts_rule.extract_facts(record_id, text, dict_version=dict_version)
    values = []
    for fact in facts:
        stage = (fact.get("time") or {}).get("stage") or {}
        if stage.get("status") == "present":
            values.append(stage.get("raw_value"))
    return values


def _conflict_pair_code(history_text: str, current_text: str) -> tuple:
    """双侧 v2 抽取 → 对齐 → P15 → 整对判定；返回 (outcome, code)。"""
    history_facts = facts_rule.extract_facts(
        RECORD_ID_H, history_text, dict_version=facts_rule.RULE_DICT_VERSION_V2)
    current_facts = facts_rule.extract_facts(
        RECORD_ID_C, current_text, dict_version=facts_rule.RULE_DICT_VERSION_V2)
    history_report = _report(RECORD_ID_H, history_text, history_facts)
    current_report = _report(RECORD_ID_C, current_text, current_facts)
    alignment = pair_alignment.build_aligned(
        history_report, current_report, history_text, current_text,
        dictionary_version="rule_dict_v2", alignment_version="alignment_v1")
    p15_report = p15_integration.extract_p15_results(
        history_report, current_report, history_text, current_text, alignment)
    pair = pair_compare.compare_pair(
        _ctx(RECORD_ID_H, "item-h", history_text, 1),
        _ctx(RECORD_ID_C, "item-c", current_text, 2),
        history_artifact=history_report, current_artifact=current_report,
        alignment=alignment, p15_results=p15_report.p15_results)
    return pair.outcome, pair.code


# ---------------- 抽取层：v1 门控缺席（红代理）/ v2 阳性 ----------------

def test_v1_gate_expanded_words_produce_no_stage_slot():
    """红代理：v1 词表不含扩充词——同比文本 v1 腿 stage 恒 missing（改前行为钉）。"""
    assert _stage_values(
        RECORD_ID_H, "甲公司公告净利润同比增长100万元。",
        facts_rule.RULE_DICT_VERSION) == []


@pytest.mark.parametrize("word", (
    "同比", "环比", "初值", "终值", "修正值", "月初", "月末"))
def test_v2_expanded_words_produce_stage_slot(word: str):
    """v2 门控：七个扩充词各自造 time.stage 槽（带证据；多动词句多槽同值）。"""
    assert set(_stage_values(
        RECORD_ID_H, f"甲公司公告{word}净利润100万元。",
        facts_rule.RULE_DICT_VERSION_V2)) == {word}


def test_v1_pan_words_still_extract_identically():
    """守卫：v1 盘态词行为不变（开盘 在 v1 仍正常造槽；多动词句多槽同值）。"""
    assert set(_stage_values(
        RECORD_ID_H, "9月10日开盘，甲公司公告回购100股。",
        facts_rule.RULE_DICT_VERSION)) == {"开盘"}


# ---------------- 端到端：异阶段冲突对（09 §8.3 规格对） ----------------

# 冲突对文本须带同日绝对日期：无日期时时间槽 missing → 未决分流早于
# 阶段冲突检查（compare_time L554 前置），阶段永不可达——非本批语义。
@pytest.mark.parametrize("left,right", (
    ("同比", "环比"),
    ("初值", "终值"),
    ("初值", "修正值"),
    ("终值", "修正值"),
    ("月初", "月末"),
))
def test_same_subject_same_value_different_stage_is_verified_conflict(
        left: str, right: str):
    """同主体同谓词同数值同日异阶段 → VERIFIED_CONFLICT（不得签发）。"""
    outcome, code = _conflict_pair_code(
        f"9月10日，甲公司公告{left}净利润100万元。",
        f"9月10日，甲公司公告{right}净利润100万元。")
    assert (outcome, code) == ("conflict", "VERIFIED_CONFLICT")


def test_same_stage_guard_no_conflict():
    """守卫：同阶段（同比×同比）不得判冲突。"""
    outcome, code = _conflict_pair_code(
        "9月10日，甲公司公告同比净利润100万元。",
        "9月10日，甲公司公告同比净利润100万元。")
    assert not (outcome == "conflict" and code == "VERIFIED_CONFLICT")


def test_one_side_stage_missing_is_not_conflict():
    """守卫：单侧带阶段词（另侧无）不得静默判冲突——未决安全向。"""
    outcome, code = _conflict_pair_code(
        "9月10日，甲公司公告同比净利润100万元。",
        "9月10日，甲公司公告净利润100万元。")
    assert not (outcome == "conflict" and code == "VERIFIED_CONFLICT")
