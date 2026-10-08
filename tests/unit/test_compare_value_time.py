from datetime import date
from fractions import Fraction

from news_flash_dedup.compare.value_time import (
    AlignmentProof,
    EvidenceRef,
    NumericSpec,
    TimeSpec,
    compare_numeric,
    compare_time,
    normalize_numeric,
    normalize_time,
    validate_original_evidence,
)


RECORD_ID = "b" * 64
ALIGNED = AlignmentProof(confirmed=True, basis="same subject, metric, time, role and original Evidence")


def evidence(text, quote, field="facts.f1.numerics.n1.value"):
    start = text.index(quote)
    return EvidenceRef(RECORD_ID, field, quote, start, start + len(quote))


def number(text, raw, *, unit="", magnitude="", currency=None, dimension="count", role="current",
           metric="amount", comparator="=", denominator="1", approximate=False, range_end=None,
           verified_missing=False):
    spec = NumericSpec(
        status="missing" if raw is None else "present",
        raw_value=raw,
        evidence=None if raw is None else evidence(text, raw),
        unit=unit,
        magnitude=magnitude,
        currency=currency,
        dimension=dimension,
        role=role,
        metric=metric,
        comparator=comparator,
        denominator=denominator,
        approximate=approximate,
        range_end=range_end,
        verified_missing=verified_missing,
    )
    return normalize_numeric(text, RECORD_ID, spec)


def time(text, raw, *, stage=None, anchor=None, relation=None, anchor_proof=None, verified_missing=False):
    spec = TimeSpec(
        status="missing" if raw is None else "present",
        raw_value=raw,
        evidence=None if raw is None else evidence(text, raw, "facts.f1.time.expression"),
        stage=stage,
        anchor_evidence=None if anchor is None else evidence(text, anchor, "facts.f1.time.anchor"),
        anchor_relation=relation,
        anchor_proof=anchor_proof,
        verified_missing=verified_missing,
    )
    return normalize_time(text, RECORD_ID, spec)


def test_original_code_point_evidence_is_required_before_local_comparison():
    text = "𠮷公司报价101.84。"
    valid = evidence(text, "101.84")
    assert validate_original_evidence(text, RECORD_ID, valid, valid.field)
    wrong = EvidenceRef(RECORD_ID, valid.field, valid.quote, valid.start + 1, valid.end + 1)
    assert not validate_original_evidence(text, RECORD_ID, wrong, valid.field)
    checked = normalize_numeric(text, RECORD_ID, NumericSpec(
        status="present", raw_value="101.84", evidence=wrong,
        unit="元", dimension="money", role="current", metric="price",
    ))
    assert not checked.valid and checked.issue_code == "EVIDENCE_INVALID"


def test_exact_decimal_conflict_requires_confirmed_corresponding_role():
    left = number("布伦特现价101.84美元/桶", "101.84", unit="美元", currency="USD", dimension="money", metric="price")
    right = number("布伦特现价101.85美元/桶", "101.85", unit="美元", currency="USD", dimension="money", metric="price")

    assert compare_numeric(left, right).status == "unresolved"
    result = compare_numeric(left, right, alignment=ALIGNED)
    assert result.status == "verified_conflict"
    assert result.reason_code == "VERIFIED_CONFLICT"
    assert left.exact == Fraction(2546, 25)
    assert right.exact == Fraction(2037, 20)


def test_decimal_format_and_unit_conversions_are_exact():
    same_format = compare_numeric(
        number("现价116.350元", "116.350", unit="元", dimension="money"),
        number("现价116.35元", "116.35", unit="元", dimension="money"),
        alignment=ALIGNED,
    )
    assert same_format.status == "format_equal"

    unit_equal = compare_numeric(
        number("金额1亿元", "1", unit="亿元", dimension="money"),
        number("金额10000万元", "10000", unit="万元", dimension="money"),
        alignment=ALIGNED,
    )
    assert unit_equal.status == "unit_equal"

    chinese_equal = compare_numeric(
        number("回购100万股", "100", magnitude="万", unit="股"),
        number("回购一百万股", "一百万", unit="股"),
        alignment=ALIGNED,
    )
    assert chinese_equal.status == "unit_equal"

    colloquial = number("金额一万二元", "一万二", unit="元", dimension="money")
    assert not colloquial.valid and colloquial.issue_code == "NUMERIC_ALIGNMENT_FAILED"


def test_large_decimal_scale_does_not_use_default_28_digit_context():
    left_raw = "12345678901234567890123456789"
    right_raw = "12345678901234567890123456788"
    left = number(left_raw + "万股", left_raw, magnitude="万", unit="股")
    right = number(right_raw + "万股", right_raw, magnitude="万", unit="股")

    assert left.exact == Fraction(123456789012345678901234567890000)
    assert right.exact == Fraction(123456789012345678901234567880000)
    assert left.exact - right.exact == 10000
    assert compare_numeric(left, right, alignment=ALIGNED).status == "verified_conflict"


def test_bp_percentage_points_denominator_and_dimension_are_preserved():
    bp = number("上升3BP", "3", unit="BP", dimension="rate_change")
    points = number("上升0.03个百分点", "0.03", unit="个百分点", dimension="rate_change")
    assert compare_numeric(bp, points, alignment=ALIGNED).status == "unit_equal"

    per_ten = number("每10股派1.20元", "1.20", unit="元", dimension="money", denominator="10")
    per_one = number("每股派0.12元", "0.12", unit="元", dimension="money", denominator="1")
    assert compare_numeric(per_ten, per_one, alignment=ALIGNED).status == "unit_equal"

    level = number("利率3%", "3", unit="%", dimension="rate_level")
    change = number("上升3个百分点", "3", unit="个百分点", dimension="rate_change")
    assert compare_numeric(level, change, alignment=ALIGNED).status == "unresolved"


def test_currency_and_comparator_uncertainty_do_not_become_numeric_conflicts():
    usd = number("金额1美元", "1", unit="美元", currency="USD", dimension="money")
    cny = number("金额7人民币", "7", unit="人民币", currency="CNY", dimension="money")
    assert compare_numeric(usd, cny, alignment=ALIGNED).status == "unresolved"

    at_least = number("至少3", "3", comparator=">=")
    greater = number("超过3", "3", comparator=">")
    assert compare_numeric(at_least, greater, alignment=ALIGNED).status == "unresolved"


def test_true_numeric_missing_is_compatible_but_unverified_missing_is_not():
    present = number("现价100元", "100", unit="元", dimension="money")
    missing = number("现价未披露", None, unit="元", dimension="money", verified_missing=True)
    unverified = number("现价100元", None, unit="元", dimension="money", verified_missing=False)
    assert compare_numeric(present, missing, alignment=ALIGNED).status == "missing_in_new"
    assert missing.issue_code is None
    assert compare_numeric(present, unverified, alignment=ALIGNED).status == "unresolved"


def test_numeric_role_mismatch_and_range_endpoints_are_not_conflated():
    current = number("现值100", "100", role="current")
    prior = number("前值101", "101", role="prior")
    assert compare_numeric(current, prior, alignment=ALIGNED).status == "unresolved"

    def interval(text, start, end):
        return normalize_numeric(text, RECORD_ID, NumericSpec(
            status="present", raw_value=start,
            evidence=evidence(text, start), range_end=end,
            range_end_evidence=evidence(text, end, "facts.f1.numerics.n1.range_end"),
            comparator="range", dimension="count", role="current", metric="amount",
        ))

    first = interval("范围100—110", "100", "110")
    second = interval("范围105—115", "105", "115")
    assert compare_numeric(first, second, alignment=ALIGNED).status == "verified_conflict"

    missing_endpoint = normalize_numeric("范围100以上", RECORD_ID, NumericSpec(
        status="present", raw_value="100", evidence=evidence("范围100以上", "100"),
        comparator="range", dimension="count", role="current", metric="amount",
    ))
    assert not missing_endpoint.valid and missing_endpoint.issue_code == "NUMERIC_ALIGNMENT_FAILED"


def test_valid_quote_in_wrong_field_cannot_be_used_as_value_evidence():
    text = "现价100元"
    wrong_field = evidence(text, "100", "facts.f1.time.expression")
    checked = normalize_numeric(text, RECORD_ID, NumericSpec(
        status="present", raw_value="100", evidence=wrong_field,
        unit="元", dimension="money", metric="price", role="current",
    ))
    assert not checked.valid and checked.issue_code == "EVIDENCE_INVALID"


def test_same_relative_time_without_absolute_anchor_remains_compatible():
    left = time("甲公司今日回购", "今日")
    right = time("甲公司今日回购", "今日")
    assert left.absolute is None and left.relative == "day_0"
    assert compare_time(left, right).relation == "unresolved"
    assert compare_time(left, right, alignment=ALIGNED).relation == "compatible"


def test_relative_and_stage_conflicts_need_no_absolute_anchor():
    today = time("今日实施", "今日")
    tomorrow = time("次日实施", "次日")
    assert compare_time(today, tomorrow, alignment=ALIGNED).relation == "conflict"
    assert compare_time(today, tomorrow, alignment=ALIGNED).reason_code == "VERIFIED_CONFLICT"

    open_time = time("开盘报价", "开盘", stage="开盘")
    close_time = time("收盘报价", "收盘", stage="收盘")
    assert compare_time(open_time, close_time, alignment=ALIGNED).relation == "conflict"

    with_date_open = normalize_time("2026年9月22日开盘报价", RECORD_ID, TimeSpec(
        status="present", raw_value="2026年9月22日",
        evidence=evidence("2026年9月22日开盘报价", "2026年9月22日", "facts.f1.time.expression"),
        stage="开盘", stage_evidence=evidence("2026年9月22日开盘报价", "开盘", "facts.f1.time.stage"),
    ))
    with_date_close = normalize_time("2026年9月22日收盘报价", RECORD_ID, TimeSpec(
        status="present", raw_value="2026年9月22日",
        evidence=evidence("2026年9月22日收盘报价", "2026年9月22日", "facts.f1.time.expression"),
        stage="收盘", stage_evidence=evidence("2026年9月22日收盘报价", "收盘", "facts.f1.time.stage"),
    ))
    assert with_date_open.valid and with_date_close.valid
    assert compare_time(with_date_open, with_date_close, alignment=ALIGNED).relation == "conflict"


def test_one_sided_time_verified_missing_compatible_unverified_unknown():
    """D31（用户裁定②路径A；窗口I N29 局部段冲突裁决消解）：verified 单侧
    缺失 → compatible（双向）——09 §11.2 L1010-1011 伪代码明文回归（该分支
    位于 verified 检查之后，抵达即已验证真实缺失；§8.3 表首"一方真正没有
    时间…允许重复；不能因缺少日期边界"；T19 预期"重复，FACT_EQUIVALENT"）。
    unverified 单侧（抽取未命中≠原文确无，N29 主体不动）→ 维持 unresolved
    (FACT_INCOMPLETE)；双侧 verified 缺失 compatible 不动（09 §11.3 本义）。
    【红能力】D31 前 C-11 路由单侧→uncertain，本用例 compatible 断言在
    D31 前代码下必红（红绿证据见 log/N21白名单对齐实施报告.md）。"""
    today = time("今日实施", "今日")
    absent = time("实施回购", None, verified_missing=True)
    unverified = time("今日实施", None, verified_missing=False)
    one_sided = compare_time(today, absent, alignment=ALIGNED)
    assert one_sided.relation == "compatible"
    mirrored = compare_time(absent, today, alignment=ALIGNED)
    assert mirrored.relation == "compatible"
    stay = compare_time(today, unverified, alignment=ALIGNED)
    assert stay.relation == "unresolved"
    assert stay.reason_code == "FACT_INCOMPLETE"
    both_absent = compare_time(absent, time("公告收购", None, verified_missing=True),
                               alignment=ALIGNED)
    assert both_absent.relation == "compatible"


def test_absolute_dates_are_derived_only_from_each_original_text():
    chinese = time("2026年9月22日公告", "2026年9月22日")
    iso = time("2026-09-22公告", "2026-09-22")
    assert chinese.absolute == iso.absolute == date(2026, 9, 22)
    assert compare_time(chinese, iso, alignment=ALIGNED).relation == "compatible"

    next_day = time("2026年9月22日公告，次日生效", "次日", anchor="2026年9月22日", relation="next_day_of", anchor_proof=ALIGNED)
    assert next_day.absolute == date(2026, 9, 23)
    assert compare_time(chinese, next_day, alignment=ALIGNED).relation == "conflict"

    unproven = time("2026年9月22日公告，次日生效", "次日", anchor="2026年9月22日", relation="next_day_of")
    assert unproven.absolute is None and not unproven.valid

    today = time("今日实施", "今日")
    assert compare_time(today, iso, alignment=ALIGNED).reason_code == "TIME_RELATION_UNCERTAIN"


def test_explicit_observation_clock_times_conflict_without_received_time():
    first = time("10:01报价", "10:01")
    second = time("10:02报价", "10:02")
    assert first.valid and second.valid
    assert compare_time(first, second, alignment=ALIGNED).relation == "conflict"


def test_valid_time_quote_in_wrong_field_is_rejected():
    text = "今日实施"
    spec = TimeSpec(status="present", raw_value="今日", evidence=evidence(text, "今日", "facts.f1.subject"))
    checked = normalize_time(text, RECORD_ID, spec)
    assert not checked.valid and checked.issue_code == "EVIDENCE_INVALID"
