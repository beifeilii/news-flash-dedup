# -*- coding: utf-8 -*-
"""winw3f-T090 契约验收钉（C4 修护窗；12号文 L539 原文亲读）。

T090（12号文 §8.7 L539）：
    同主体同Fact同时间同角色，12345678901234567890123456789万股
    与 12345678901234567890123456788万股；注入默认 Decimal context。
    必须断言：两数超过28位，精确差10000股；精确算出即冲突不重复，
    无需无关字段全齐。阻断默认精度静默舍入，无法支持则
    NUMERIC_ALIGNMENT_FAILED 边界，不伪数值缺失。
    证据/门禁：原值/精度/换算/差值；S。

修复前侦察（log\\temp\\winw3f-t090-probe.json，默认 context prec=28 注入下实测）：
- 主判定链（facts/rule.py 抽取 → pair_alignment → p15_integration →
  compare/value_time.compare_numeric → pair_compare → aggregate）已精确：
  value_time._fraction_from_decimal 仅消费 Decimal 构造器（构造不舍入，
  context 精度只作用于算术），随后全链 Fraction 精确有理算术——
  decide_for_task 实测 pair=conflict/VERIFIED_CONFLICT、decision=不重复、
  精确差 10000 股（腿 A/E）。本文件对该链下**不退化钉**。
- decide/machine_verify.py（残判层 R3/R4 数值对拍）存在**默认 28 位静默舍入**：
  norm_number_token 的 `Decimal(core) * mult` 与 `value.normalize()` 均在
  环境 context 下算术/归一——29 位数即使 mult=1 也被 .normalize() 抹尾
  （腿 D：…789万 与 …788万 归一后同为 1.234567890123456789012345679E+32，
  R3 双向差异集被洗空=冲突漏检；R4 凭空声称被舍入碰撞放行）。
  本文件对该路径下**精度守卫红钉**。

修法裁定（任务②二选一）：契约首选"精确算出即冲突不重复"——
machine_verify 归一化为局部高精度 context 精确计算（非 NUMERIC_ALIGNMENT_FAILED
边界；机验层无该码通道，且归一化可平凡精确化），理由钉于源码注记。
"""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal, getcontext
from fractions import Fraction

import pytest

from news_flash_dedup.compare import value_time
from news_flash_dedup.decide import machine_verify as mv
from news_flash_dedup.decide.service import decide_for_task
from news_flash_dedup.facts import llm as facts_llm
from news_flash_dedup.facts import rule as facts_rule

# T090 契约当事值（29 位，超过默认 context 28 位精度；精确差 1 万股 = 10000 股）
V_H = "12345678901234567890123456789"
V_C = "12345678901234567890123456788"
assert len(V_H) == 29 and len(V_C) == 29
assert int(V_H) - int(V_C) == 1                       # 万股口径差 1
TEXT_H = f"甲公司今日回购{V_H}万股。"               # 同主体/同Fact/同时间(今日)/同角色
TEXT_C = f"甲公司今日回购{V_C}万股。"
RID_H = "a" * 64
RID_C = "b" * 64


@pytest.fixture(autouse=True)
def _inject_default_decimal_context():
    """契约注入：默认 Decimal context（prec=28，Python 出厂值）。

    显式钉死防环境漂移；用例结束还原调用前 prec（兼作被测代码不泄漏
    私改全局精度的守卫）。用例内 getcontext().prec 必须读到 28——
    证明被测代码不依赖调用方抬高精度。
    """
    ctx = getcontext()
    saved_prec = ctx.prec
    ctx.prec = 28
    yield
    ctx.prec = saved_prec


def _ctx(record_id: str, item_id: str, text: str, seq: int,
         facts: list[dict]) -> dict:
    return {
        "record_id": record_id, "item_id": item_id, "text": text,
        "raw_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "scope_id": "default", "business_date": "2026-09-26",
        "arrival_seq": seq, "facts": facts,
    }


def _decide_t090(**extract_kwargs) -> tuple:
    """T090 全形真链：rule 真抽取 → decide_for_task（编排层裸包装→P16-A→P15→P16-B→聚合）。"""
    history = _ctx(RID_H, "item-" + "a" * 8, TEXT_H, 1,
                   facts_rule.extract_facts(RID_H, TEXT_H, **extract_kwargs))
    current = _ctx(RID_C, "item-" + "b" * 8, TEXT_C, 2,
                   facts_rule.extract_facts(RID_C, TEXT_C, **extract_kwargs))
    out = decide_for_task(history, [], current=current, coverage_complete=True)
    pair = out.pair_results[0]
    return out, pair, history["facts"], current["facts"]


def _assert_t090_conflict_contract(out, pair, facts_h, facts_c) -> None:
    # 抽取不伪数值缺失：双侧 value 槽 present 且完整 29 位原值
    val_h = facts_h[0]["numerics"][0]["value"]
    val_c = facts_c[0]["numerics"][0]["value"]
    assert val_h["status"] == "present" and val_h["raw_value"] == V_H
    assert val_c["status"] == "present" and val_c["raw_value"] == V_C
    assert val_h["unit"] == "股" and val_h["magnitude"] == "万"
    assert val_c["unit"] == "股" and val_c["magnitude"] == "万"
    # 精确算出即冲突不重复（无需无关字段全齐：role/metric 恒 missing 不妨碍）
    assert pair.outcome == "conflict"
    assert pair.code == "VERIFIED_CONFLICT"
    # 精确路线可达 → 不得走 NUMERIC_ALIGNMENT_FAILED 边界，更不得伪数值缺失
    # （FACT_INCOMPLETE）；契约次序：先精确算出，支持不了才许 NUMERIC 边界
    assert pair.code != "NUMERIC_ALIGNMENT_FAILED"
    assert pair.code != "FACT_INCOMPLETE"
    # 原值留痕（证据列：原值/精度/换算/差值）
    assert pair.verified_conflicts, "T090 必须产出数值 VerifiedConflict"
    conflict = pair.verified_conflicts[0]
    assert conflict.basis == "NUMERIC_SAME_DECIMAL"
    assert V_H in conflict.detail and V_C in conflict.detail
    # 聚合层终判：不重复、零重复成员
    assert out.decision == "不重复"
    assert out.duplicate_ids == ()


# ---------- ① T090 场景全形（主判定链，v1/v2 抽取双腿） ----------

def test_t090_full_form_rule_v1_conflict_not_duplicate():
    assert getcontext().prec == 28, "默认 Decimal context 注入必须在役"
    out, pair, facts_h, facts_c = _decide_t090()
    _assert_t090_conflict_contract(out, pair, facts_h, facts_c)


def test_t090_full_form_rule_v2_conflict_not_duplicate():
    assert getcontext().prec == 28, "默认 Decimal context 注入必须在役"
    out, pair, facts_h, facts_c = _decide_t090(
        dict_version=facts_rule.RULE_DICT_VERSION_V2)
    _assert_t090_conflict_contract(out, pair, facts_h, facts_c)


# ---------- ② 精度/换算/差值钉（value_time 直调，证据列四要素） ----------

def _spec_29digit(text: str, value: str, record_id: str) -> value_time.NumericSpec:
    start = text.index(value)
    return value_time.NumericSpec(
        status="present", raw_value=value,
        evidence=value_time.EvidenceRef(
            record_id, "facts.f1.numerics.n1.value", value,
            start, start + len(value)),
        unit="股", magnitude="万",
    )


def test_t090_exact_difference_10000_shares():
    """原值/精度/换算/差值：万股×10^4 换算后精确差恰 10000 股。"""
    chk_h = value_time.normalize_numeric(
        TEXT_H, RID_H, _spec_29digit(TEXT_H, V_H, RID_H))
    chk_c = value_time.normalize_numeric(
        TEXT_C, RID_C, _spec_29digit(TEXT_C, V_C, RID_C))
    assert chk_h.valid and chk_c.valid
    assert chk_h.rule_id == "DECIMAL_TUPLE_EXACT_SCALE"
    assert chk_c.rule_id == "DECIMAL_TUPLE_EXACT_SCALE"
    # 换算：万股 → 股（×10^4 精确，不取默认 context 28 位舍入值）
    assert chk_h.exact == Fraction(int(V_H) * 10_000)
    assert chk_c.exact == Fraction(int(V_C) * 10_000)
    # 差值：精确 10000 股
    assert chk_h.exact - chk_c.exact == Fraction(10_000)
    # 精度：默认 context（prec=28）下 Decimal 算术会把两数洗成同一舍入值——
    # 本路径不得出现该形态（Decimal 构造器不舍入，全链 Fraction 精确）
    rounded_h = Decimal(V_H) * Decimal(10) ** 4       # 默认 context 算术
    rounded_c = Decimal(V_C) * Decimal(10) ** 4
    assert rounded_h == rounded_c, "前提钉：默认 28 位算术确实把两数洗同（契约要阻断的形态）"
    cmp_ = value_time.compare_numeric(
        chk_h, chk_c,
        alignment=value_time.AlignmentProof(True, "NUMERIC_SAME_DECIMAL:f1/f1"))
    assert cmp_.status == "verified_conflict"
    assert cmp_.reason_code == "VERIFIED_CONFLICT"


# ---------- ③ LLM 供给链精度钉（facts/llm.py _decimal_text 消费面） ----------

def test_t090_llm_supply_preserves_full_29digit_value():
    """LLM 抽取链 29 位原值完整入槽（不截断/不舍入/不伪 missing）。"""
    payload = {"facts": [{
        "subject": "甲公司", "predicate": "回购", "polarity": "回购",
        "modality": None, "key_object": None, "time_expression": "今日",
        "time_stage": None, "attribution": None,
        "numerics": [{"value_raw": V_H + "万股", "metric": None, "unit": "股",
                      "magnitude": "万", "comparator": None,
                      "range_end_raw": None, "direction": None}],
    }]}
    facts = facts_llm.extract_facts_llm(
        RID_H, TEXT_H,
        call_fn=lambda **_kw: (json.dumps(payload, ensure_ascii=False), 0.001))
    slot = facts[0]["numerics"][0]["value"]
    assert slot["status"] == "present"
    assert slot["raw_value"] == V_H
    assert slot["value"] == V_H                      # _decimal_text 全 29 位
    assert slot["unit"] == "股" and slot["magnitude"] == "万"


# ---------- ④ 精度守卫红钉（machine_verify 归一化：不得静默舍入为相等） ----------

def test_precision_guard_norm_number_token_29digit_not_silently_equal():
    """红核：29 位数值归一化不得被默认 28 位精度静默舍入为相等。"""
    norm_h = mv.norm_number_token(V_H)
    norm_c = mv.norm_number_token(V_C)
    assert norm_h is not None and norm_c is not None
    # 不得静默舍入为相等（修复前同为 1.234567890123456789012345679E+28）
    assert norm_h != norm_c
    # 精确性：归一规范串反解必须与 29 位原值逐位相等（Decimal 比较精确，
    # 不经 context 算术；期望真值由字符串构造，不受默认精度污染）
    assert Decimal(norm_h) == Decimal(V_H)
    assert Decimal(norm_c) == Decimal(V_C)


def test_precision_guard_norm_number_token_29digit_wan_exact():
    """红核：29 位数+万级后缀（×10^4 换算）归一化精确且双侧可分。"""
    norm_h = mv.norm_number_token(V_H + "万")
    norm_c = mv.norm_number_token(V_C + "万")
    assert norm_h is not None and norm_c is not None
    assert norm_h != norm_c
    # 精确差 10000 股：两侧反解差恰为 10^4（换算/差值钉）
    assert Decimal(norm_h) - Decimal(norm_c) == Decimal(10_000)
    assert Decimal(norm_h) == Decimal(V_H + "0000")
    assert Decimal(norm_c) == Decimal(V_C + "0000")


def test_precision_guard_r3_sees_29digit_conflict():
    """R3 关键数值对拍：双向差异集不得被舍入洗空（修复前冲突漏检）。"""
    set_h = mv.filtered_number_set(TEXT_H)
    set_c = mv.filtered_number_set(TEXT_C)
    assert set_h != set_c
    assert (set_h - set_c) and (set_c - set_h)
    assert mv.rule_r3_numeric_conflict(TEXT_H, TEXT_C) is True


def test_precision_guard_r4_ungrounded_29digit_claim_flagged():
    """R4 单位宽容声称匹配：凭空 29 位声称不得被舍入碰撞放行。"""
    judgment = {
        "numeric_check": {"numbers_a": [f"约{V_C}万"], "numbers_b": []},
        "time_check": {"times_a": [], "times_b": []},
        "evidence_a": [], "evidence_b": [],
    }
    # A 侧原文 789万，裁判声称 788万：精确口径=凭空声称必须 flagged；
    # 修复前两值同舍入为 1.234567890123456789012345679E+32 → 严格命中放行（漏检）
    bad = mv.rule_r4_ungrounded_claims(judgment, TEXT_H, TEXT_C)
    assert bad, "凭空 29 位声称必须 flagged（不得被默认精度舍入碰撞洗白）"
    assert bad[0]["side"] == "A"


# ---------- ⑤ 28 位以下正常路径不退化钉（字节级零漂移） ----------

# 修复前现役输出逐字节钉值（≤28 位精确值在提高局部精度后输出必须同一——
# 结果在 28 位内本即精确表示，提高精度不改变任何位）
_SMALL_TOKEN_PINS = (
    ("123456789012345678901234567", "123456789012345678901234567"),      # 27 位
    ("1234567890123456789012345678", "1234567890123456789012345678"),    # 28 位（界上）
    ("100万", "1E+6"),
    ("1.5亿", "1.5E+8"),
    ("一千二百三十四万", "1.234E+7"),
    ("5%", "5|p"),
    ("0.08%", "0.08|p"),
    ("100", "1E+2"),
    ("34", "34"),
    ("10000", "1E+4"),
    ("12.50", "12.5"),
    ("0.100", "0.1"),
)


@pytest.mark.parametrize("token, expected", _SMALL_TOKEN_PINS)
def test_normal_path_token_norm_byte_identical(token, expected):
    assert mv.norm_number_token(token) == expected


def test_normal_path_extract_number_set_decimal_form():
    """小数归一（去尾零）现役形态钉：'279.90点' 语境抽 279.9。"""
    assert mv.extract_number_set("涨幅279.90点") == {"279.9"}


def test_normal_path_r4_magnitude_tolerance_kept():
    """R4 正常量级宽容不退化：原文 100万 对声称 100（裸值）仍判 grounded。"""
    judgment = {
        "numeric_check": {"numbers_a": ["增持100"], "numbers_b": []},
        "time_check": {"times_a": [], "times_b": []},
        "evidence_a": [], "evidence_b": [],
    }
    bad = mv.rule_r4_ungrounded_claims(
        judgment, "甲公司今日回购100万股。", TEXT_C)
    assert bad == []


def test_normal_path_main_chain_small_conflict_unchanged():
    """主判定链 28 位以下正常冲突路径不退化：100万股 vs 101万股。"""
    history = _ctx(RID_H, "item-" + "a" * 8, "甲公司今日回购100万股。", 1,
                   facts_rule.extract_facts(RID_H, "甲公司今日回购100万股。"))
    current = _ctx(RID_C, "item-" + "b" * 8, "甲公司今日回购101万股。", 2,
                   facts_rule.extract_facts(RID_C, "甲公司今日回购101万股。"))
    out = decide_for_task(history, [], current=current, coverage_complete=True)
    pair = out.pair_results[0]
    assert pair.outcome == "conflict"
    assert pair.code == "VERIFIED_CONFLICT"
    assert out.decision == "不重复"
    assert out.duplicate_ids == ()
