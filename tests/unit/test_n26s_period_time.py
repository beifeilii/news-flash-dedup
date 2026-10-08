"""N26③ 窗口S：期间/年份粒度时间（设计稿 log/N26-三抽取设计稿.md §三 一期）单元矩阵。

覆盖清单（设计稿 §3.4"单元矩阵"逐条）：
- 期间解析：上半年/下半年/四季/跨年边界（下半年→次年 1-1、Q4→次年 1-1）/
  无年 symbol（第二季度↔二季度 归一）/半年度候选（1fbf9fdf 实证，呈用户策展登记）；
- 比较矩阵全 7 格（period 同区间/相离/包含相交/date∈period/date∉period/
  clock_time/period↔relative symbol）+ symbol 冲突对；
- N29 missing 语义回归（value_time.py L449-465 D31 精修版零漂移）；
- 既有 date/clock/relative/stage 路径零漂移回归。

红能力纪律：本文件先于实现落笔（见红），实现落地后全绿。
"""

from datetime import date

from news_flash_dedup.compare.value_time import (
    AlignmentProof,
    EvidenceRef,
    PeriodInterval,
    TimeSpec,
    compare_time,
    normalize_time,
)


RECORD_ID = "c" * 64
ALIGNED = AlignmentProof(confirmed=True, basis="same subject, metric, time, role and original Evidence")


def _evidence(text, quote, field="facts.f1.time.expression"):
    start = text.index(quote)
    return EvidenceRef(RECORD_ID, field, quote, start, start + len(quote))


def _time(text, raw, *, stage=None, verified_missing=False):
    spec = TimeSpec(
        status="missing" if raw is None else "present",
        raw_value=raw,
        evidence=None if raw is None else _evidence(text, raw),
        stage=stage,
        stage_evidence=(None if stage is None
                        else _evidence(text, stage, "facts.f1.time.stage")),
        verified_missing=verified_missing,
    )
    return normalize_time(text, RECORD_ID, spec)


def _compare(left, right):
    return compare_time(left, right, alignment=ALIGNED)


# ---------- 期间解析（normalize_time → CheckedTime.absolute/relative） ----------

def test_parse_half_year_with_year():
    checked = _time("公司2026年上半年净利润增长。", "2026年上半年")
    assert checked.valid
    assert checked.absolute == PeriodInterval(date(2026, 1, 1), date(2026, 7, 1))
    assert checked.relative is None


def test_parse_half_year_baidu_variant_same_interval():
    """半年度↔上半年 同值（1fbf9fdf 双侧同指 2026H1 实证；呈用户策展登记项）。"""
    checked = _time("公司2026年半年度净利润增长。", "2026年半年度")
    assert checked.valid
    assert checked.absolute == PeriodInterval(date(2026, 1, 1), date(2026, 7, 1))


def test_parse_second_half_crosses_year_boundary():
    """下半年 → [Y-07-01, (Y+1)-01-01)：跨年边界端点钉死。"""
    checked = _time("公司2026年下半年计划投产。", "2026年下半年")
    assert checked.valid
    assert checked.absolute == PeriodInterval(date(2026, 7, 1), date(2027, 1, 1))


def test_parse_quarters_with_year():
    text = "一季度第二季度第三季度第四季度"
    cases = {
        "2026年第一季度": (date(2026, 1, 1), date(2026, 4, 1)),
        "2026年第二季度": (date(2026, 4, 1), date(2026, 7, 1)),
        "2026年第三季度": (date(2026, 7, 1), date(2026, 10, 1)),
        "2026年第四季度": (date(2026, 10, 1), date(2027, 1, 1)),  # Q4 跨年
    }
    for raw, (start, end) in cases.items():
        full = f"报告{raw}数据{text}"
        checked = _time(full, raw)
        assert checked.valid, raw
        assert checked.absolute == PeriodInterval(start, end), raw


def test_parse_quarter_without_di_prefix():
    checked = _time("公司2026年二季度营收。", "2026年二季度")
    assert checked.valid
    assert checked.absolute == PeriodInterval(date(2026, 4, 1), date(2026, 7, 1))


def test_parse_yearless_half_as_relative_symbol():
    first = _time("公司上半年净利润增长。", "上半年")
    second = _time("公司下半年计划投产。", "下半年")
    assert first.valid and first.absolute is None and first.relative == "half_1"
    assert second.valid and second.absolute is None and second.relative == "half_2"


def test_parse_yearless_quarter_surface_variants_share_symbol():
    """表面变体解析层归一：第二季度↔二季度 → 同 symbol（c2545905 实证）。"""
    formal = _time("公司第二季度营收增长。", "第二季度")
    short = _time("公司二季度营收增长。", "二季度")
    assert formal.valid and formal.absolute is None and formal.relative == "quarter_2"
    assert short.valid and short.absolute is None and short.relative == "quarter_2"


def test_parse_deictic_quarter_words_stay_uncertain():
    """本/上/下季度（指示性相对词，需锚定）一期不接——维持 invalid 安全向。"""
    for raw in ("本季度", "上季度", "下季度"):
        checked = _time(f"公司{raw}营收增长。", raw)
        assert not checked.valid and checked.issue_code == "TIME_RELATION_UNCERTAIN", raw


def test_parse_bare_year_not_collected_in_phase_one():
    """裸年一期不接：即便抽取层喂入 present 裸年槽，解析层维持 invalid（登记二期）。"""
    checked = _time("项目拟于2028年开始产气。", "2028年")
    assert not checked.valid and checked.issue_code == "TIME_RELATION_UNCERTAIN"


def test_parse_invalid_year_fails_closed():
    checked = _time("公司0000年上半年数据。", "0000年上半年")
    assert not checked.valid and checked.issue_code == "TIME_RELATION_UNCERTAIN"


# ---------- 比较矩阵 7 格（compare_time absolute 段 kind 分派） ----------

def test_matrix_cell_period_same_interval_compatible():
    left = _time("甲2026年上半年业绩。", "2026年上半年")
    right = _time("乙2026年上半年业绩。", "2026年上半年")
    assert _compare(left, right).relation == "compatible"


def test_matrix_cell_period_baidu_variant_compatible():
    left = _time("甲2026年半年度业绩。", "2026年半年度")
    right = _time("乙2026年上半年业绩。", "2026年上半年")
    assert _compare(left, right).relation == "compatible"


def test_matrix_cell_period_disjoint_conflict():
    """相离 → conflict（真异时，与 _STAGE_CONFLICTS 同构；fp 安全向）。"""
    left = _time("甲2026年上半年业绩。", "2026年上半年")
    right = _time("乙2026年下半年业绩。", "2026年下半年")
    result = _compare(left, right)
    assert result.relation == "conflict" and result.reason_code == "VERIFIED_CONFLICT"
    q1 = _time("甲2026年第一季度数据。", "2026年第一季度")
    q3 = _time("乙2026年第三季度数据。", "2026年第三季度")
    assert _compare(q1, q3).relation == "conflict"


def test_matrix_cell_period_containment_or_overlap_unresolved():
    """包含/相交不等 → unresolved（不能证明相等，守边界绝不助签）。"""
    half = _time("甲2026年上半年业绩。", "2026年上半年")
    q2 = _time("乙2026年第二季度业绩。", "2026年第二季度")
    assert _compare(half, q2).relation == "unresolved"          # 包含
    assert _compare(q2, half).relation == "unresolved"          # 对侧对称
    q1 = _time("甲2026年第一季度业绩。", "2026年第一季度")
    assert _compare(q1, half).relation == "unresolved"          # 包含（Q1⊂H1）


def test_matrix_cell_date_inside_period_unresolved():
    inside = _time("甲2026年3月5日公告。", "2026年3月5日")
    half = _time("乙2026年上半年业绩。", "2026年上半年")
    assert _compare(inside, half).relation == "unresolved"
    assert _compare(half, inside).relation == "unresolved"
    edge_in = _time("甲2026年1月1日公告。", "2026年1月1日")     # 左闭端点 ∈
    assert _compare(edge_in, half).relation == "unresolved"


def test_matrix_cell_date_outside_period_conflict():
    """date ∉ period → conflict（半开区间：右端点排他钉死）。"""
    half = _time("甲2026年上半年业绩。", "2026年上半年")
    after = _time("乙2026年9月20日公告。", "2026年9月20日")
    result = _compare(after, half)
    assert result.relation == "conflict" and result.reason_code == "VERIFIED_CONFLICT"
    edge_out = _time("乙2026年7月1日公告。", "2026年7月1日")     # 右开端点 ∉
    assert _compare(edge_out, half).relation == "conflict"


def test_matrix_cell_period_vs_clock_unresolved():
    half = _time("甲2026年上半年业绩。", "2026年上半年")
    clock = _time("乙09:30盘中报价。", "09:30")
    assert _compare(half, clock).relation == "unresolved"


def test_matrix_cell_period_vs_relative_symbol_unresolved():
    """带年期间 vs 无年 symbol：年不可证 → unresolved（按 relative 规则落尾）。"""
    half_year = _time("甲2026年上半年业绩。", "2026年上半年")
    half_symbol = _time("乙上半年业绩。", "上半年")
    assert _compare(half_year, half_symbol).relation == "unresolved"


# ---------- relative symbol 冲突注册（_RELATIVE_CONFLICTS 扩展） ----------

def test_symbol_same_after_surface_unification_compatible():
    formal = _time("甲第二季度营收。", "第二季度")
    short = _time("乙二季度营收。", "二季度")
    assert _compare(formal, short).relation == "compatible"


def test_symbol_quarter_mismatch_conflict():
    q1 = _time("甲第一季度营收。", "第一季度")
    q3 = _time("乙第三季度营收。", "第三季度")
    result = _compare(q1, q3)
    assert result.relation == "conflict" and result.reason_code == "VERIFIED_CONFLICT"


def test_symbol_half_mismatch_conflict():
    h1 = _time("甲上半年业绩。", "上半年")
    h2 = _time("乙下半年业绩。", "下半年")
    result = _compare(h1, h2)
    assert result.relation == "conflict" and result.reason_code == "VERIFIED_CONFLICT"


def test_symbol_cross_granularity_unresolved():
    """异粒度异词（half vs quarter）→ unresolved（3102a5b3 安全向先例）。"""
    half = _time("甲上半年业绩。", "上半年")
    quarter = _time("乙第一季度业绩。", "第一季度")
    assert _compare(half, quarter).relation == "unresolved"


def test_symbol_vs_legacy_relative_word_unresolved():
    half = _time("甲上半年业绩。", "上半年")
    today = _time("乙今日公告。", "今日")
    assert _compare(half, today).relation == "unresolved"


# ---------- 既有路径零漂移回归（date/clock/stage/relative 旧语义） ----------

def test_legacy_date_paths_repinned_p2_yearless_undecided():
    """2026-10-09（P2 先行件①重钉；宪章《判定宪章-草案-v2-1009.md》§三-2 +
    §〇-4）：本用例原名 test_legacy_date_paths_unchanged，旧钉值系 D19 C 包
    '_YEAR_LESS_DEFAULT_YEAR=2026' 缺年补全行为（同 '9月20日' compatible /
    异日 conflict）——该常量已按宪章拆除（无年不得靠默认年份解析、禁用正文
    外来源补日期），无年 'M月D日' 双腿重钉为时间未决（红能力证据见
    tests/unit/test_p2_yearless_time.py，拆除前本重钉全红）；带年 date vs
    clock 型别未决腿零漂移（前后均绿）。"""
    same = _compare(_time("甲9月20日公告。", "9月20日"),
                    _time("乙9月20日公告。", "9月20日"))
    assert same.relation == "unresolved"       # P2①：年不可证 → 未决（旧=compatible）
    assert same.reason_code == "TIME_RELATION_UNCERTAIN"
    diff = _compare(_time("甲9月20日公告。", "9月20日"),
                    _time("乙9月21日公告。", "9月21日"))
    assert diff.relation == "unresolved"       # P2①：年不可证 → 未决（旧=conflict）
    assert diff.reason_code == "TIME_RELATION_UNCERTAIN"
    mixed = _compare(_time("甲2026年9月20日公告。", "2026年9月20日"),
                     _time("乙09:30盘中。", "09:30"))
    assert mixed.relation == "unresolved"      # date vs clock 型别不同维持未决


def test_legacy_stage_conflict_precedence_unchanged():
    """stage 冲突仍先行于 absolute 段（期间 absolute 不抢 stage 路径）。"""
    open_ = _time("2026年9月22日开盘报价。", "2026年9月22日", stage="开盘")
    close = _time("2026年9月22日收盘报价。", "2026年9月22日", stage="收盘")
    result = _compare(open_, close)
    assert result.relation == "conflict" and result.reason_code == "VERIFIED_CONFLICT"


def test_alignment_gate_unchanged_for_period_pairs():
    left = _time("甲2026年上半年业绩。", "2026年上半年")
    right = _time("乙2026年上半年业绩。", "2026年上半年")
    assert compare_time(left, right).relation == "unresolved"   # 无对齐证明未决


# ---------- N29/D31 missing 语义回归（L449-465 区段零漂移） ----------

def test_n29_double_verified_missing_compatible():
    both = _compare(_time("甲公司完成回购。", None, verified_missing=True),
                    _time("甲公司完成回购。", None, verified_missing=True))
    assert both.relation == "compatible"


def test_n29_unverified_missing_invalid_unresolved():
    """unverified missing（抽取未命中≠原文确无）→ invalid(FACT_INCOMPLETE) → 未决。"""
    checked = _time("甲公司完成回购。", None)
    assert not checked.valid and checked.issue_code == "FACT_INCOMPLETE"
    other = _time("乙2026年上半年业绩。", "2026年上半年")
    result = _compare(checked, other)
    assert result.relation == "unresolved" and result.reason_code == "FACT_INCOMPLETE"


def test_d31_single_verified_missing_vs_period_compatible():
    """D31 精修版：verified 单侧缺失 → compatible（期间 absolute 不改 missing 分流）。"""
    missing = _time("甲公司完成回购。", None, verified_missing=True)
    period = _time("乙2026年上半年业绩。", "2026年上半年")
    assert _compare(missing, period).relation == "compatible"
    assert _compare(period, missing).relation == "compatible"
