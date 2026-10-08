"""窗口T · 残判层机器验 R0-R6 单元钉值（decide/machine_verify.py）。

钉值口径（主窗口 2026-09-28 裁定）：
- R1-R6 合成红绿钉值 + fp 三对（6e02c930/d27e0196/f74eaded）规则直评模式
  （与 log/temp/d32-llm-judge/d32-hardening-fp.py 直评结果逐一对拍：
  6e02c930→R2 中；d27e0196→R1 中（R3 同中，D32 实测一致）；f74eaded→R3 中）；
- 误伤数与 D32 模拟一致（d32-hardening.json：签发集上 R1=0/R2=3/R3=2/R6=4）
  的全量对拍在 tests/integration/test_p23_residual_judge.py（读 D32 产物）；
- R0 闸门纪律（仅闸/验判"重复"的判定）由 llm_residual 侧测试守底。
"""

from __future__ import annotations

from news_flash_dedup.decide import machine_verify as mv


def _judgment(*, evidence_a=("营收100万",), evidence_b=("营收100万",),
              numbers_a=(), numbers_b=(), num_conclusion="一致",
              times_a=(), times_b=(), time_conclusion="一致",
              reason="测试判定"):
    return {
        "decision": "重复",
        "evidence_a": list(evidence_a),
        "evidence_b": list(evidence_b),
        "numeric_check": {"numbers_a": list(numbers_a),
                           "numbers_b": list(numbers_b),
                           "conclusion": num_conclusion},
        "time_check": {"times_a": list(times_a), "times_b": list(times_b),
                        "conclusion": time_conclusion},
        "reason": reason,
    }


# ---------------------------------------------------------------- R1 主体锚词对拍

def test_r1_fires_on_disjoint_subject_codes():
    ta = "易天股份（300812）近5日主力资金净流入1234万"
    tb = "名臣健康（002919）近5日主力资金净流入1234万"
    assert mv.rule_r1_subject_codes(ta, tb) is True


def test_r1_green_same_code_both_sides():
    ta = "易天股份（300812）近5日净流入"
    tb = "易天股份(300812)今日再度披露"
    assert mv.rule_r1_subject_codes(ta, tb) is False


def test_r1_green_one_side_no_code():
    assert mv.rule_r1_subject_codes("易天股份（300812）净流入", "名臣健康净流入") is False
    assert mv.rule_r1_subject_codes("纯文本无代码", "（002919）另一侧") is False


def test_r1_green_non6digit_numbers_ignored():
    # 5 位/7 位数字不匹配 6 位代码模式（(?<!\d)\d{6}(?!\d)）
    assert mv.rule_r1_subject_codes("编号12345 与 1234567", "编号54321 与 7654321") is False


# ---------------------------------------------------------------- R2 时间锚对拍

def test_r2_fires_fp_pattern_6e02c930():
    """fp 对 6e02c930 模式：中期选举后 vs 大选结束后（归一不等+含阶段词"选举"）。"""
    j = _judgment(times_a=("中期选举后",), times_b=("大选结束后",),
                  time_conclusion="不一致")
    assert mv.rule_r2_time_anchor(j) is True


def test_r2_green_normalized_equal_times():
    j = _judgment(times_a=("2026年9月24日",), times_b=("2026 9 月 24 号",))
    assert mv.rule_r2_time_anchor(j) is False


def test_r2_green_one_side_empty():
    assert mv.rule_r2_time_anchor(_judgment(times_a=("9月24日",), times_b=())) is False
    assert mv.rule_r2_time_anchor(_judgment(times_a=(), times_b=())) is False


def test_r2_green_diff_without_stage_word():
    j = _judgment(times_a=("3月4日",), times_b=("3月5日",))
    assert mv.rule_r2_time_anchor(j) is False


# ---------------------------------------------------------------- R3 关键数值对拍

def test_r3_fires_fp_pattern_f74eaded():
    """fp 对 f74eaded 模式：787.8 vs 789.8 真实价差（双向差异向）。"""
    ta = "WTI原油涨9%报787.8美元"
    tb = "WTI原油涨超9%报789.8美元"
    assert mv.rule_r3_numeric_conflict(ta, tb) is True


def test_r3_green_one_sided_difference():
    # 一方多出数值（缺失≠冲突）：only_a 为空 → 不拦
    assert mv.rule_r3_numeric_conflict("营收100万", "营收100万，去年80万") is False


def test_r3_green_date_tokens_filtered():
    # 日期型 token（后随年月日）剔除后两侧集合均空 → 不拦
    assert mv.rule_r3_numeric_conflict("2026年9月24日投产", "2026年9月25日投产") is False


def test_r3_green_ordinal_tokens_filtered():
    # 编号/序数型（前邻"第"、后随号届版）剔除 → 不拦
    assert mv.rule_r3_numeric_conflict("第3季度营收持平", "第4季度营收持平") is False
    assert mv.rule_r3_numeric_conflict("公告编号2026-5号", "公告编号2026-6号") is False


def test_r3_green_equal_sets():
    assert mv.rule_r3_numeric_conflict("营收100万同比涨5%", "营收100万同比涨5%") is False


# ---------------------------------------------------------------- R4 单位宽容声称匹配

def test_r4_magnitude_suffix_tolerated():
    # 声称"330" vs 原文"330万"（差 10^4 且紧随"万"后缀）→ 宽容不拦
    j = _judgment(numbers_a=("330",))
    assert mv.rule_r4_ungrounded_claims(j, "成交330万手", "无数值") == []


def test_r4_percent_flag_tolerated():
    # 声称"9" vs 原文"9%"（% 旗标差异）→ 宽容不拦
    j = _judgment(numbers_a=("9",))
    assert mv.rule_r4_ungrounded_claims(j, "涨幅9%", "无数值") == []


def test_r4_fires_on_fabricated_number():
    # 凭空数值（原文连数字串都没有）→ 拦
    j = _judgment(numbers_a=("999",))
    bad = mv.rule_r4_ungrounded_claims(j, "成交330万手", "无数值")
    assert len(bad) == 1 and bad[0]["side"] == "A" and bad[0]["quote"] == "999"


def test_r4_unparseable_claim_skipped():
    # 无数值 token 的声称项（输出纪律违规）→ warning 不拦
    j = _judgment(numbers_a=("很高",))
    assert mv.rule_r4_ungrounded_claims(j, "成交330万手", "无数值") == []


def test_r4_green_strict_hit():
    j = _judgment(numbers_a=("787.8",), numbers_b=("789.8",))
    assert mv.rule_r4_ungrounded_claims(j, "报787.8美元", "报789.8美元") == []


# ---------------------------------------------------------------- R5 引文逐字验

def test_r5_fires_evidence_not_verbatim():
    j = _judgment(evidence_a=("原文中不存在的引文片段",))
    hits = mv.rule_r5_evidence_verbatim(j, "营收100万，同比涨5%", "营收100万")
    assert [h["check"] for h in hits] == ["evidence_not_verbatim"]
    assert hits[0]["side"] == "A"


def test_r5_fires_anchor_too_short():
    # 去空白 <4 字符的锚（即使逐字真实）→ anchor_too_short
    j = _judgment(evidence_a=("42人",), evidence_b=("裁员42人",))
    hits = mv.rule_r5_evidence_verbatim(j, "裁员42人引发关注", "裁员42人")
    assert [h["check"] for h in hits] == ["evidence_anchor_too_short"]


def test_r5_fires_numbers_and_times_quote_not_verbatim():
    j = _judgment(numbers_a=("999",), times_b=("明年今日",))
    hits = mv.rule_r5_evidence_verbatim(j, "营收100万", "营收100万")
    checks = {h["check"] for h in hits}
    assert "numbers_quote_not_verbatim" in checks
    assert "times_quote_not_verbatim" in checks


def test_r5_green_whitespace_fuzzy_match():
    # 全空白归一模糊匹配：引文含空白、原文不含 → 视为逐字
    j = _judgment(evidence_a=("成交 330万 手",), evidence_b=("成交330万手",))
    assert mv.rule_r5_evidence_verbatim(j, "成交330万手", "成交330万手") == []


# ---------------------------------------------------------------- R6 方向词对拍

def test_r6_fires_polarity_conflict():
    assert mv.rule_r6_polarity_conflict("CPI高于预期", "CPI低于预期") is True
    assert mv.rule_r6_polarity_conflict("资金净流入5亿", "资金净流出5亿") is True


def test_r6_green_same_direction():
    assert mv.rule_r6_polarity_conflict("CPI高于预期", "CPI高于预期") is False
    assert mv.rule_r6_polarity_conflict("营收增长", "营收增长") is False


# ---------------------------------------------------------------- fp 三对规则直评（D32 对拍钉值）

def test_fp_trio_rule_coverage_direct_evaluation():
    """fp 三对模式直评（与 d32-hardening-fp.py 实测逐一对拍）：

    - 6e02c930：R2 中（R1/R3/R6 不中）——时间锚 中期选举后 vs 大选结束后；
    - d27e0196：R1 中（R3 同中，D32 实测一致：300812 vs 002919 双向差异）；
    - f74eaded：R3 中（R1/R2/R6 不中）——787.8 vs 789.8 真实价差。
    """
    # 6e02c930 模式
    ta = "特朗普表示，中期选举后伊朗战争将立刻终止"
    tb = "特朗普表示，大选结束后，伊朗战争将立刻终止"
    j6 = _judgment(times_a=("中期选举后",), times_b=("大选结束后",),
                   time_conclusion="不一致")
    assert mv.rule_r1_subject_codes(ta, tb) is False
    assert mv.rule_r2_time_anchor(j6) is True
    assert mv.rule_r3_numeric_conflict(ta, tb) is False
    assert mv.rule_r6_polarity_conflict(ta, tb) is False
    # d27e0196 模式
    ta = "易天股份（300812）近5日主力资金净流入居前"
    tb = "名臣健康（002919）近5日主力资金净流入居前"
    assert mv.rule_r1_subject_codes(ta, tb) is True
    assert mv.rule_r3_numeric_conflict(ta, tb) is True   # D32 实测同中（300812/2919）
    assert mv.rule_r6_polarity_conflict(ta, tb) is False
    # f74eaded 模式
    ta = "WTI原油涨9%报787.8美元"
    tb = "WTI原油涨超9%报789.8美元"
    assert mv.rule_r1_subject_codes(ta, tb) is False
    assert mv.rule_r3_numeric_conflict(ta, tb) is True
    assert mv.rule_r6_polarity_conflict(ta, tb) is False


# ---------------------------------------------------------------- 组合机验（模拟器同款顺序）

def test_verify_signed_judgment_combined_order_and_content():
    """组合机验返回规则顺序=R5,R4,R3,R1,R2,R6（d32-hardening 模拟器同款）。"""
    ta = "易天股份（300812）近5日净流入，高于预期"
    tb = "名臣健康（002919）近5日净流入，低于预期"
    j = _judgment(evidence_a=("不存在的引文片段",),
                  times_a=("中期选举后",), times_b=("大选结束后",))
    fired = mv.verify_signed_judgment(j, ta, tb)
    rules = [f["rule"] for f in fired]
    assert rules == ["R5", "R3", "R1", "R2", "R6"], rules


def test_verify_signed_judgment_green_pair_passes():
    ta = "营收100万，同比涨5%"
    tb = "营收100万，同比涨5%"
    j = _judgment(evidence_a=("营收100万",), evidence_b=("营收100万",),
                  numbers_a=("100万",), numbers_b=("100万",))
    assert mv.verify_signed_judgment(j, ta, tb) == []
