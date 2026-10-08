"""Exact local numeric and time rules for two already aligned facts.

These functions do not choose candidate pairs or produce a business result.
The caller must supply a confirmed correspondence before a local conflict can
be treated as verified evidence.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, time as clock_time, timedelta
from decimal import Decimal, InvalidOperation
from fractions import Fraction


_DECIMAL = re.compile(r"-?(0|[1-9][0-9]*)(\.[0-9]+)?\Z")
_CHINESE_DATE = re.compile(r"([0-9]{4})年([0-9]{1,2})月([0-9]{1,2})日\Z")
_CHINESE_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_SMALL_UNITS = {"十": 10, "百": 100, "千": 1000}
_LARGE_UNITS = {"万": 10_000, "亿": 100_000_000}
_MAGNITUDES = {"": 1, "百": 100, "千": 1000, "万": 10_000, "亿": 100_000_000}
_UNITS: dict[str, tuple[str, Fraction, str | None, str]] = {
    "": ("", Fraction(1), None, ""),
    "股": ("shares", Fraction(1), None, ""),
    "万股": ("shares", Fraction(10_000), None, "万"),
    "亿股": ("shares", Fraction(100_000_000), None, "亿"),
    "元": ("money", Fraction(1), None, ""),
    "万元": ("money", Fraction(10_000), None, "万"),
    "亿元": ("money", Fraction(100_000_000), None, "亿"),
    "美元": ("money", Fraction(1), "USD", ""),
    "人民币": ("money", Fraction(1), "CNY", ""),
    "BP": ("rate", Fraction(1, 10_000), None, ""),
    "个百分点": ("rate", Fraction(1, 100), None, ""),
    "%": ("rate", Fraction(1, 100), None, ""),
    "％": ("rate", Fraction(1, 100), None, ""),
    # D19 C/D 包（S1 实测驱动）：P14 抽取词典（rule.py _NUM_QUANTITY）与 P15
    # 单位词典对齐——canonical 各立维度（scale 1，不跨单位等值），港元入 money/HKD。
    "吨": ("mass", Fraction(1), None, ""),
    "桶": ("volume_barrel", Fraction(1), None, ""),
    "手": ("lot", Fraction(1), None, ""),
    "台": ("count_tai", Fraction(1), None, ""),
    "辆": ("count_liang", Fraction(1), None, ""),
    "家": ("count_jia", Fraction(1), None, ""),
    "笔": ("count_bi", Fraction(1), None, ""),
    "单": ("count_dan", Fraction(1), None, ""),
    "人": ("count_ren", Fraction(1), None, ""),
    "千瓦时": ("energy_kwh", Fraction(1), None, ""),
    "平方米": ("area_sqm", Fraction(1), None, ""),
    "港元": ("money", Fraction(1), "HKD", ""),
    "点": ("points", Fraction(1), None, ""),  # D19 E5：指数点位（279.90点）
    "毫米": ("length_mm", Fraction(1), None, ""),  # 续行轮：1b93a701"2毫米至19毫米"
}

# D19 C/D 包：比较符中文词→规范符映射（S1 实测：双侧全同仍 invalid 系词典缺口）。
# 约/近 归 "="（W2Fα1/WB2-H2：approximate 标志**保留**不复位——09 §8.2"保留
# 近似模态，不擅设容差；不能证明同义则边界"，compare_numeric 近似闸执法）。
# 续行轮勘误（c6fb6a67 实测）："超"语义=严格大于（"涨幅超5%"≡"超过5%"），
# S1 稿映射 超→"=" 有误，修正为 超→">"；安全向：">"vs"=" 不等→未决→边界
# （不制造 fp）。
_COMPARATOR_CN_MAP = {
    "不低于": ">=", "不少于": ">=", "超过": ">", "高于": ">",
    "低于": "<", "不足": "<", "约": "=", "近": "=", "超": ">",
}
_RELATIVE = {
    "今日": "day_0", "当日": "day_0", "次日": "day_plus_1", "翌日": "day_plus_1",
    "当年": "year_0", "今年": "year_0", "次年": "year_plus_1", "明年": "year_plus_1",
    # 续行轮（3102a5b3 实测）：周/月粒度相对词——"上周"双侧全同仍 invalid 系
    # 同型词典缺口；异粒度异词（今日 vs 上周）落 uncertain 安全向（无冲突对列入）。
    "本周": "week_0", "上周": "week_minus_1",
    "本月": "month_0", "上月": "month_minus_1",
}
_STAGES = {
    "开盘": "open", "收盘": "close", "初值": "preliminary", "终值": "final",
    "同比": "year_on_year", "环比": "period_on_period",
    # 阶段词表扩充配套（用户 2026-09-28 裁定；抽取侧 rule.py v2 门控同步产出）
    "修正值": "revised", "月初": "month_start", "月末": "month_end",
}
_STAGE_CONFLICTS = {
    frozenset(("open", "close")),
    frozenset(("preliminary", "final")),
    frozenset(("year_on_year", "period_on_period")),
    # 扩充配套：修正值与初/终值互斥（同一指标不同发布轮次）；月初/月末互斥
    frozenset(("preliminary", "revised")),
    frozenset(("final", "revised")),
    frozenset(("month_start", "month_end")),
}
_RELATIVE_CONFLICTS = {
    frozenset(("day_0", "day_plus_1")),
    frozenset(("year_0", "year_plus_1")),
    # N26③：无年期间 symbol 冲突注册（设计稿 §3.2——同文年假设下成立，与
    # _YEAR_LESS_DEFAULT_YEAR 同纪律声明；异粒度异词 half_i vs quarter_j
    # 不注册 → 落尾部 unresolved 安全向，3102a5b3/D19 §5.9 先例）。
    frozenset(("half_1", "half_2")),
    frozenset(("quarter_1", "quarter_2")),
    frozenset(("quarter_1", "quarter_3")),
    frozenset(("quarter_1", "quarter_4")),
    frozenset(("quarter_2", "quarter_3")),
    frozenset(("quarter_2", "quarter_4")),
    frozenset(("quarter_3", "quarter_4")),
}

# N26③ 一期解析表（设计稿 §3.2，v3 门控纪律见 normalize_time/compare_time
# 增量处注记）：带年期间 → PeriodInterval；无年期间 → relative symbol
# （表面变体解析层归一：第二季度↔二季度 同 symbol，c2545905 双侧同文实证）。
# 半年度↔上半年 同值化（1fbf9fdf 双侧同指 2026H1 实证）系设计稿 §3.3 风险三
# 登记的**一期候选呈用户策展**项——按批准设计稿表内收录实施；用户否决则删
# 本表 半年度 两处即回退（失败方向 unresolved 安全向，不涉 fp）。
# 一期不接（设计稿 §3.2 表末行/§3.3 风险四）：裸年 YYYY年——维持 missing
# （抽取层 _TIME_EXPRESSION 本不命中，missing→present 分布迁移二期独立实测）；
# 本/上/下季度——指示性相对词需锚定，维持 invalid（TIME_RELATION_UNCERTAIN
# 安全向）。
_PERIOD_CN_QUARTER = {"一": 1, "二": 2, "三": 3, "四": 4}
_PERIOD_YEAR_HALF = re.compile(r"([0-9]{4})年(上半年|下半年|半年度)\Z")
_PERIOD_YEAR_QUARTER = re.compile(r"([0-9]{4})年第?([一二三四])季度\Z")
_PERIOD_RELATIVE = {
    "上半年": "half_1", "下半年": "half_2",
    "第一季度": "quarter_1", "第二季度": "quarter_2",
    "第三季度": "quarter_3", "第四季度": "quarter_4",
    "一季度": "quarter_1", "二季度": "quarter_2",
    "三季度": "quarter_3", "四季度": "quarter_4",
}


@dataclass(frozen=True)
class EvidenceRef:
    record_id: str
    field: str
    quote: str
    start: int
    end: int


@dataclass(frozen=True)
class AlignmentProof:
    confirmed: bool
    basis: str


def validate_original_evidence(
    text: str, record_id: str, evidence: EvidenceRef | None, expected_field: str,
) -> bool:
    return (
        isinstance(evidence, EvidenceRef)
        and evidence.record_id == record_id
        and evidence.field == expected_field
        and type(evidence.start) is int
        and type(evidence.end) is int
        and 0 <= evidence.start < evidence.end <= len(text)
        and text[evidence.start:evidence.end] == evidence.quote
    )


@dataclass(frozen=True)
class NumericSpec:
    status: str
    raw_value: str | None
    evidence: EvidenceRef | None = None
    unit: str = ""
    magnitude: str = ""
    currency: str | None = None
    dimension: str | None = None
    role: str | None = None
    metric: str | None = None
    comparator: str = "="
    denominator: str = "1"
    approximate: bool = False
    range_end: str | None = None
    range_end_evidence: EvidenceRef | None = None
    verified_missing: bool = False


@dataclass(frozen=True)
class CheckedNumeric:
    status: str
    valid: bool
    exact: Fraction | None
    range_end: Fraction | None
    raw_value: str | None
    unit: str
    magnitude: str
    canonical_unit: str
    currency: str | None
    dimension: str | None
    role: str | None
    metric: str | None
    comparator: str
    approximate: bool
    denominator: Fraction | None
    issue_code: str | None = None
    rule_id: str | None = None


@dataclass(frozen=True)
class NumericComparison:
    status: str
    reason_code: str | None = None


def _fraction_from_decimal(raw: str) -> Fraction:
    if _DECIMAL.fullmatch(raw) is None:
        raise ValueError("not a canonical decimal string")
    try:
        number = Decimal(raw)
    except InvalidOperation as exc:
        raise ValueError("invalid decimal") from exc
    sign, digits, exponent = number.as_tuple()
    coefficient = int("".join(str(digit) for digit in digits)) if digits else 0
    if sign:
        coefficient = -coefficient
    if exponent >= 0:
        return Fraction(coefficient * 10**exponent)
    return Fraction(coefficient, 10**(-exponent))


def _fraction_from_chinese(raw: str) -> Fraction:
    if re.search(r"[万亿][一二两三四五六七八九]$", raw):
        raise ValueError("ambiguous colloquial Chinese numeral")
    # M-05（外部审计，主窗口核验属实并入）：bare"千/百+尾数"口语形态
    # （"一千二"本义 1200、"一百二"本义 120，无万亿后缀旧守卫不盖）会被
    # 静默错值为 1002/102——扩展守卫 fail-closed：宁可 ambiguous 不可静默
    # 错值。标准形"一千零二"（零居中）与本义形"一千二百"不命中本守卫。
    if re.search(r"[一二两三四五六七八九][千百][一二两三四五六七八九]$", raw):
        raise ValueError("ambiguous colloquial Chinese numeral")
    section = 0
    total = 0
    current = 0
    for character in raw:
        if character in _CHINESE_DIGITS:
            current = _CHINESE_DIGITS[character]
        elif character in _SMALL_UNITS:
            section += (current or 1) * _SMALL_UNITS[character]
            current = 0
        elif character in _LARGE_UNITS:
            section += current
            total += (section or 1) * _LARGE_UNITS[character]
            section = 0
            current = 0
        else:
            raise ValueError("unsupported Chinese numeral")
    return Fraction(total + section + current)


def _parse_value(raw: str) -> Fraction:
    if _DECIMAL.fullmatch(raw):
        return _fraction_from_decimal(raw)
    return _fraction_from_chinese(raw)


def _numeric_invalid(spec: NumericSpec, code: str) -> CheckedNumeric:
    return CheckedNumeric(
        spec.status, False, None, None, spec.raw_value, spec.unit, spec.magnitude,
        "", spec.currency, spec.dimension, spec.role, spec.metric,
        spec.comparator, spec.approximate, None, code,
    )


def normalize_numeric(text: str, record_id: str, spec: NumericSpec) -> CheckedNumeric:
    """Use exact Decimal tuple arithmetic and rational scale factors."""
    if spec.status == "missing":
        if spec.raw_value is not None or spec.evidence is not None or not spec.verified_missing:
            return _numeric_invalid(spec, "FACT_INCOMPLETE")
        return CheckedNumeric(
            "missing", True, None, None, None, spec.unit, spec.magnitude,
            "", spec.currency, spec.dimension, spec.role, spec.metric,
            spec.comparator, spec.approximate, None, None, "MISSING_VERIFIED",
        )
    if spec.status != "present" or not isinstance(spec.raw_value, str) or not spec.raw_value:
        return _numeric_invalid(spec, "FACT_INCOMPLETE")
    if (spec.evidence is None or not spec.evidence.field.endswith(".value")
            or not validate_original_evidence(text, record_id, spec.evidence, spec.evidence.field)):
        return _numeric_invalid(spec, "EVIDENCE_INVALID")
    if spec.raw_value not in spec.evidence.quote:
        return _numeric_invalid(spec, "EVIDENCE_INVALID")
    if spec.range_end is not None:
        if (spec.range_end_evidence is None or not spec.range_end_evidence.field.endswith(".range_end")
                or not validate_original_evidence(
            text, record_id, spec.range_end_evidence, spec.range_end_evidence.field,
        ) or spec.range_end not in spec.range_end_evidence.quote):
            return _numeric_invalid(spec, "EVIDENCE_INVALID")
    if spec.unit not in _UNITS or spec.magnitude not in _MAGNITUDES:
        return _numeric_invalid(spec, "NUMERIC_ALIGNMENT_FAILED")
    # D19 C/D 包：比较符中文词典归一（不低于/高于/近 等）——词典缺口修复，
    # 双侧同词才可达此层（S1 fp 守卫实证）；raw_value 前导 '+' 正号剥离。
    # W2Fα1（WB2-H2，判定壁）：approximate 不再随 约/近 归一复位——09 §8.2
    # 明文"约100与100｜保留近似模态，不擅设容差；不能证明同义则边界"，复位
    # 把"约100"洗成与裸"100"全同（equal=fp 路径）；保留后由 compare_numeric
    # 现役 approximate 闸（任一近似→unresolved）执法，fp 安全向。
    comparator = _COMPARATOR_CN_MAP.get(spec.comparator, spec.comparator)
    approximate = spec.approximate
    if comparator not in {"=", "<", "<=", ">", ">=", "range"}:
        return _numeric_invalid(spec, "NUMERIC_ALIGNMENT_FAILED")
    if (comparator == "range") != (spec.range_end is not None):
        return _numeric_invalid(spec, "NUMERIC_ALIGNMENT_FAILED")
    canonical_unit, unit_scale, unit_currency, included_magnitude = _UNITS[spec.unit]
    if included_magnitude and spec.magnitude and included_magnitude != spec.magnitude:
        return _numeric_invalid(spec, "NUMERIC_ALIGNMENT_FAILED")
    if unit_currency and spec.currency and unit_currency != spec.currency:
        return _numeric_invalid(spec, "NUMERIC_ALIGNMENT_FAILED")
    try:
        denominator = _fraction_from_decimal(spec.denominator)
        if denominator <= 0:
            raise ValueError("invalid denominator")
        raw_number = _parse_value(spec.raw_value.lstrip("+"))
        range_end = (_parse_value(spec.range_end.lstrip("+"))
                     if spec.range_end is not None else None)
    except ValueError:
        return _numeric_invalid(spec, "NUMERIC_ALIGNMENT_FAILED")
    scale = unit_scale if included_magnitude else unit_scale * _MAGNITUDES[spec.magnitude]
    return CheckedNumeric(
        "present", True, raw_number * scale / denominator,
        range_end * scale / denominator if range_end is not None else None,
        spec.raw_value, spec.unit, spec.magnitude, canonical_unit,
        spec.currency or unit_currency, spec.dimension, spec.role, spec.metric,
        comparator, approximate, denominator, None,
        "DECIMAL_TUPLE_EXACT_SCALE" if _DECIMAL.fullmatch(spec.raw_value.lstrip("+")) else "CHINESE_NUMERAL_EXACT_SCALE",
    )


def _aligned(alignment: AlignmentProof | None) -> bool:
    return isinstance(alignment, AlignmentProof) and alignment.confirmed is True and bool(alignment.basis.strip())


def compare_numeric(
    left: CheckedNumeric, right: CheckedNumeric, *, alignment: AlignmentProof | None = None,
) -> NumericComparison:
    if not _aligned(alignment):
        return NumericComparison("unresolved", "NUMERIC_ALIGNMENT_FAILED")
    if not left.valid or not right.valid:
        return NumericComparison("unresolved", left.issue_code if not left.valid else right.issue_code)
    if left.status == right.status == "missing":
        return NumericComparison("not_applicable")
    if left.status == "missing":
        return NumericComparison("missing_in_history")
    if right.status == "missing":
        return NumericComparison("missing_in_new")
    if (
        left.canonical_unit != right.canonical_unit
        or left.dimension != right.dimension
        or left.currency != right.currency
        or left.role != right.role
        or left.metric != right.metric
    ):
        return NumericComparison("unresolved", "NUMERIC_ALIGNMENT_FAILED")
    if left.approximate != right.approximate or left.approximate:
        return NumericComparison("unresolved", "NUMERIC_ALIGNMENT_FAILED")
    if left.comparator != right.comparator:
        return NumericComparison("unresolved", "NUMERIC_ALIGNMENT_FAILED")
    if left.comparator == "range":
        if left.range_end is None or right.range_end is None:
            return NumericComparison("unresolved", "NUMERIC_ALIGNMENT_FAILED")
        equal = left.exact == right.exact and left.range_end == right.range_end
    elif left.comparator == "=":
        equal = left.exact == right.exact
    else:
        equal = left.exact == right.exact
        if not equal:
            return NumericComparison("unresolved", "NUMERIC_ALIGNMENT_FAILED")
    if not equal:
        return NumericComparison("verified_conflict", "VERIFIED_CONFLICT")
    if (
        left.unit != right.unit or left.magnitude != right.magnitude
        or left.denominator != right.denominator
    ):
        return NumericComparison("unit_equal")
    if left.raw_value != right.raw_value:
        return NumericComparison("format_equal")
    return NumericComparison("equal")


@dataclass(frozen=True)
class TimeSpec:
    status: str
    raw_value: str | None
    evidence: EvidenceRef | None = None
    stage: str | None = None
    stage_evidence: EvidenceRef | None = None
    anchor_evidence: EvidenceRef | None = None
    anchor_relation: str | None = None
    anchor_proof: AlignmentProof | None = None
    verified_missing: bool = False


@dataclass(frozen=True)
class PeriodInterval:
    """N26③（D28-4 批准立项第 4 件之③；设计稿 log/N26-三抽取设计稿.md §三
    一期）：期间粒度绝对时间——半开区间 [start, end)，两端皆为 date
    （end 排他）。回归 09 §11.2 L933 原设计"absolute 含 (kind, value)、只
    比较同粒度的值"——kind 经 Python 类型分派（date / clock_time /
    PeriodInterval），不并类比较。哨兵日期方案（终点哨兵语义造假开通 fp
    路径／起点哨兵主动 FN 误杀）已经设计稿 §3.2 论证否决，勿复用。"""
    start: date
    end: date                    # 排他


@dataclass(frozen=True)
class CheckedTime:
    status: str
    valid: bool
    absolute: date | clock_time | PeriodInterval | None
    relative: str | None
    stage: str | None
    weak_allowed: bool
    issue_code: str | None = None


@dataclass(frozen=True)
class TimeComparison:
    relation: str
    reason_code: str | None = None


_MD_ONLY_DATE = re.compile(r"([0-9]{1,2})月([0-9]{1,2})日\Z")
# D19 C 包：'M月D日' 缺年形态的补全年。**声明式假设**：V1 金标语料统一报道年
# （两本工作簿全部样本落在 2026-09）；缺年补全年使"9月10日"↔"9月10日"及
# "2026年9月10日"↔"9月10日"可比。跨年语料场景（如 12 月报道次年 1 月事件）
# 不在本假设覆盖内——失败方向是 TIME_RELATION_UNCERTAIN（安全方向），
# 二期以上下文 business_date 补全替代本常量（归属待裁定项）。
_YEAR_LESS_DEFAULT_YEAR = 2026


def _parse_absolute(raw: str) -> date | clock_time | None:
    match = _CHINESE_DATE.fullmatch(raw)
    try:
        if match:
            return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        if re.fullmatch(r"[0-9]{4}-[0-9]{1,2}-[0-9]{1,2}", raw):
            year, month, day = (int(part) for part in raw.split("-"))
            return date(year, month, day)
        if re.fullmatch(r"(?:[01]?[0-9]|2[0-3]):[0-5][0-9](?::[0-5][0-9])?", raw):
            parts = [int(part) for part in raw.split(":")]
            return clock_time(*parts)
        # D19 C 包：'M月D日' 缺年补全（假设见 _YEAR_LESS_DEFAULT_YEAR 注释）。
        md_match = _MD_ONLY_DATE.fullmatch(raw)
        if md_match:
            return date(_YEAR_LESS_DEFAULT_YEAR,
                        int(md_match.group(1)), int(md_match.group(2)))
    except ValueError:
        return None
    return None


def _parse_period(raw: str) -> PeriodInterval | None:
    """N26③：带年期间串 → 半开区间 [start, end)。

    YYYY年上半年/半年度 → [Y-01-01, Y-07-01)；YYYY年下半年 → [Y-07-01,
    (Y+1)-01-01)（跨年边界）；YYYY年[第]X季度（X∈一二三四）→ 对应季度区间
    （Q4 跨年至 (Y+1)-01-01）。不命中/非法年份 → None（fail-closed，与
    _parse_absolute 的 ValueError 纪律同款：宁 invalid 不静默错值）。
    """
    match = _PERIOD_YEAR_HALF.fullmatch(raw)
    try:
        if match:
            year = int(match.group(1))
            if match.group(2) == "下半年":
                return PeriodInterval(date(year, 7, 1), date(year + 1, 1, 1))
            return PeriodInterval(date(year, 1, 1), date(year, 7, 1))
        match = _PERIOD_YEAR_QUARTER.fullmatch(raw)
        if match:
            year = int(match.group(1))
            quarter = _PERIOD_CN_QUARTER[match.group(2)]
            if quarter < 4:
                return PeriodInterval(date(year, (quarter - 1) * 3 + 1, 1),
                                      date(year, quarter * 3 + 1, 1))
            return PeriodInterval(date(year, 10, 1), date(year + 1, 1, 1))
    except ValueError:
        return None
    return None


def normalize_time(text: str, record_id: str, spec: TimeSpec) -> CheckedTime:
    """Keep relative time relative unless this same text supplies its anchor."""
    if spec.status == "missing":
        if spec.raw_value is not None or spec.evidence is not None or not spec.verified_missing:
            return CheckedTime("missing", False, None, None, None, False, "FACT_INCOMPLETE")
        return CheckedTime("missing", True, None, None, None, False)
    if spec.status != "present" or not isinstance(spec.raw_value, str) or not spec.raw_value:
        return CheckedTime(spec.status, False, None, None, None, False, "TIME_RELATION_UNCERTAIN")
    if (spec.evidence is None or not spec.evidence.field.endswith(".time.expression")
            or not validate_original_evidence(text, record_id, spec.evidence, spec.evidence.field)):
        return CheckedTime(spec.status, False, None, None, None, False, "EVIDENCE_INVALID")
    if spec.raw_value not in spec.evidence.quote:
        return CheckedTime(spec.status, False, None, None, None, False, "EVIDENCE_INVALID")
    if spec.stage is not None:
        if spec.stage_evidence is not None:
            if (not spec.stage_evidence.field.endswith(".time.stage")
                    or not validate_original_evidence(text, record_id, spec.stage_evidence, spec.stage_evidence.field)
                    or spec.stage not in spec.stage_evidence.quote):
                return CheckedTime(spec.status, False, None, None, None, False, "EVIDENCE_INVALID")
        elif spec.stage not in spec.evidence.quote:
            return CheckedTime(spec.status, False, None, None, None, False, "EVIDENCE_INVALID")
    absolute = _parse_absolute(spec.raw_value)
    if absolute is None:
        # N26③（设计稿 §3.2"normalize_time 内接入点，先于 _RELATIVE 查表"）：
        # 带年期间 → PeriodInterval。v3 门控纪律落法：比较层系全腿共享单文件
        # （调用链无 dict_version 通道、规则抽取 v1/v2 的 _TIME_EXPRESSION 本就
        # 命中期间串），增量以纯追加生效、既有输入行为逐字节保持——v1 腿零
        # 效应由 391 对全量回放实测钉死（窗口S 报告在案）；v1/v2 抽取路径
        # （rule.py）本方案零改动。
        absolute = _parse_period(spec.raw_value)
    relative = _RELATIVE.get(spec.raw_value)
    if relative is None:
        # N26③：无年期间 → relative symbol（第二季度↔二季度 归一，c2545905）。
        relative = _PERIOD_RELATIVE.get(spec.raw_value)
    stage = _STAGES.get(spec.stage or spec.raw_value)
    weak = spec.raw_value in {"近期", "最近"}
    if spec.anchor_evidence is not None:
        anchor = spec.anchor_evidence
        if not anchor.field.endswith(".time.anchor") or not validate_original_evidence(text, record_id, anchor, anchor.field):
            return CheckedTime(spec.status, False, None, relative, stage, weak, "EVIDENCE_INVALID")
        if not _aligned(spec.anchor_proof):
            return CheckedTime(spec.status, False, None, relative, stage, weak, "TIME_RELATION_UNCERTAIN")
        anchor_date = _parse_absolute(anchor.quote)
        same_clause = anchor.end <= spec.evidence.start and not re.search(r"[。！？!?；;]", text[anchor.end:spec.evidence.start])
        if not isinstance(anchor_date, date) or not same_clause:
            return CheckedTime(spec.status, False, None, relative, stage, weak, "TIME_RELATION_UNCERTAIN")
        if spec.anchor_relation == "next_day_of" and relative == "day_plus_1":
            absolute = anchor_date + timedelta(days=1)
        elif spec.anchor_relation == "same_day_as" and relative == "day_0":
            absolute = anchor_date
        else:
            return CheckedTime(spec.status, False, None, relative, stage, weak, "TIME_RELATION_UNCERTAIN")
    elif spec.anchor_relation is not None:
        return CheckedTime(spec.status, False, None, relative, stage, weak, "TIME_RELATION_UNCERTAIN")
    if absolute is None and relative is None and stage is None and not weak:
        return CheckedTime(spec.status, False, None, None, None, False, "TIME_RELATION_UNCERTAIN")
    return CheckedTime("present", True, absolute, relative, stage, weak)


def _compare_period_pair(left_abs, right_abs) -> TimeComparison:
    """N26③ 期间分派矩阵（设计稿 §3.2 比较矩阵七格）：

    period vs period——同区间 compatible／相离 conflict（对应同一已确认事件
    的两个时间分属相离半开区间则必为不同时间，与 _STAGE_CONFLICTS 初值/终值、
    同比/环比同构，09 §11.2 L1005-1008"可归一的对应时间不同"→VERIFIED_CONFLICT）／
    包含·相交不等 unresolved（不能证明相等——09 §8.3 L829"不猜相等或冲突"
    类比，守边界绝不助签）；period vs date——date∈period unresolved／
    date∉period conflict（半开区间右端排他）；period vs clock_time
    unresolved（粒度不可比）。本函数只在 confirmed aligned fact 对内被调
    （09 §11.2 L933"不把无关时间放入同一对"）；失败方向恒为边界/FN，冲突
    仅在真异时发出（fp 安全向）。"""
    if isinstance(left_abs, PeriodInterval) and isinstance(right_abs, PeriodInterval):
        if left_abs == right_abs:
            return TimeComparison("compatible")
        if left_abs.end <= right_abs.start or right_abs.end <= left_abs.start:
            return TimeComparison("conflict", "VERIFIED_CONFLICT")
        return TimeComparison("unresolved", "TIME_RELATION_UNCERTAIN")
    if isinstance(left_abs, clock_time) or isinstance(right_abs, clock_time):
        return TimeComparison("unresolved", "TIME_RELATION_UNCERTAIN")
    period = left_abs if isinstance(left_abs, PeriodInterval) else right_abs
    day = right_abs if isinstance(left_abs, PeriodInterval) else left_abs
    if period.start <= day < period.end:
        return TimeComparison("unresolved", "TIME_RELATION_UNCERTAIN")
    return TimeComparison("conflict", "VERIFIED_CONFLICT")


def compare_time(
    left: CheckedTime, right: CheckedTime, *, alignment: AlignmentProof | None = None,
) -> TimeComparison:
    if not _aligned(alignment):
        return TimeComparison("unresolved", "TIME_RELATION_UNCERTAIN")
    if not left.valid or not right.valid:
        return TimeComparison("unresolved", left.issue_code if not left.valid else right.issue_code)
    if frozenset((left.stage, right.stage)) in _STAGE_CONFLICTS:
        return TimeComparison("conflict", "VERIFIED_CONFLICT")
    if left.absolute is not None and right.absolute is not None:
        # N26③：period 参与即走 kind 分派（矩阵见 _compare_period_pair）；
        # 双侧均非 period 时下方三行既有路径逐字节保持。
        if isinstance(left.absolute, PeriodInterval) or isinstance(right.absolute, PeriodInterval):
            return _compare_period_pair(left.absolute, right.absolute)
        if type(left.absolute) is not type(right.absolute):
            return TimeComparison("unresolved", "TIME_RELATION_UNCERTAIN")
        if left.absolute != right.absolute:
            return TimeComparison("conflict", "VERIFIED_CONFLICT")
        return TimeComparison("compatible")
    # D31（用户裁定②路径A；窗口I《方案一致性核验-09.md》N29 局部段冲突裁决
    # 消解）：09 §11.2 L1010-1011 伪代码明文回归——该分支位于 verified 检查
    # （L554-555；N26③ 纯追加移位前行号 L445-446）之后，抵达此处的 missing 槽
    # 必 valid，而 valid 的 missing 当且仅当抽取层显式 verified_missing=True
    # （normalize_time L465-468；移位前 L394-397）——即
    # "已验证真实缺失允许重复"：§8.3 表首"一方真正没有时间、其他核心事实
    # 一致 | missing，允许重复；不能因缺少日期边界"+ T19 预期"重复，
    # FACT_EQUIVALENT"+ §12.2 L1340"真实时间或数值缺失……不自动边界"。
    # unverified missing（抽取未命中≠原文确无，N29 主体不动）在 normalize_time
    # 判 invalid(FACT_INCOMPLETE)，由 L554-555 落 unresolved，永不抵达本分支。
    if left.status == "missing" or right.status == "missing":
        return TimeComparison("compatible")
    if left.relative and right.relative:
        if left.relative == right.relative:
            return TimeComparison("compatible")
        if frozenset((left.relative, right.relative)) in _RELATIVE_CONFLICTS:
            return TimeComparison("conflict", "VERIFIED_CONFLICT")
    if left.weak_allowed and right.weak_allowed:
        return TimeComparison("compatible")
    # M-06（外部审计，主窗口核验属实并入）：还原 09 L1019-1023 规格——同阶段
    # 判 compatible 须"双侧 absolute 皆 None"前置（带绝对日期侧 vs 仅阶段词
    # 侧的时间关系不可判 → 落尾部 uncertain，不得静默 compatible）。09 L1022
    # 的 status 双 present 前置由上方 D31 缺失分流分支蕴含（missing 已先行
    # 分流，抵达此处双侧必 present）。
    if (left.stage and left.stage == right.stage
            and left.absolute is None and right.absolute is None
            and left.relative is None and right.relative is None):
        return TimeComparison("compatible")
    return TimeComparison("unresolved", "TIME_RELATION_UNCERTAIN")
