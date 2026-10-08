"""B2 规则式 P14 抽取基线单元测试（主窗口 23:1x，按设计书 §7 测试计划实装）。

绿条件（设计书 §7.3 诚实边界）：G1/G2 全局断言 + build_aligned 硬门；
不以 validate_fact_artifact 全量校验为绿条件（基线自知覆盖不全）。
"""

from __future__ import annotations

import pytest

from news_flash_dedup.decide.service import _wrap_facts_as_report
from news_flash_dedup.compare import pair_alignment
from news_flash_dedup.facts import rule as facts_rule


RECORD_A = "a" * 64
RECORD_B = "b" * 64

_FACT_KEYS = {"fact_id", "evidence", "fact_type", "subject", "event_state",
              "time", "key_object", "numerics"}
_EVENT_STATE_KEYS = {"predicate", "polarity", "modality", "attribution"}
_TIME_KEYS = {"expression", "stage", "anchor"}
_NUMERIC_KEYS = {"numeric_id", "evidence", "metric", "value", "range_end",
                 "magnitude", "unit", "currency", "role", "comparator",
                 "direction", "time"}
_DECIMAL_INLINE_KEYS = {"value", "unit", "magnitude", "currency", "role",
                        "metric", "comparator", "approximate", "range_end"}


def _walk_slots(node):
    """递归产出全部 slot dict（含 numeric 内槽）。"""
    if isinstance(node, dict):
        if set(node.keys()) >= {"status", "raw_value", "evidence"}:
            yield node
        for child in node.values():
            yield from _walk_slots(child)
    elif isinstance(node, list):
        for child in node:
            yield from _walk_slots(child)


def assert_g1_evidence_real(text: str, record_id: str, facts: list[dict]) -> None:
    """G1：全部 evidence 偏移真实（含 fact 级与 numeric 级）。"""
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


def assert_g2_shape(facts: list[dict]) -> None:
    """G2：全字段形态（键封闭/编号连续/present-missing 互斥）。"""
    assert facts, "G2 需要至少一个 fact"
    for index, fact in enumerate(facts, 1):
        assert set(fact.keys()) == _FACT_KEYS
        assert fact["fact_id"] == f"f{index}"
        assert set(fact["event_state"].keys()) == _EVENT_STATE_KEYS
        assert set(fact["time"].keys()) == _TIME_KEYS
        numeric_ids = []
        for numeric in fact["numerics"]:
            assert set(numeric.keys()) == _NUMERIC_KEYS
            numeric_ids.append(numeric["numeric_id"])
        assert numeric_ids == [f"n{k}" for k in range(1, len(numeric_ids) + 1)]
    for fact in facts:
        for slot in _walk_slots(fact):
            if slot["status"] == "present":
                assert slot["raw_value"] and slot["evidence"]
            elif slot["status"] == "missing":
                assert slot["raw_value"] is None and slot["evidence"] == []
            else:
                raise AssertionError(f"未知 status: {slot['status']!r}")


def _extract(text: str, record_id: str = RECORD_A) -> list[dict]:
    facts = facts_rule.extract_facts(record_id, text)
    assert_g1_evidence_real(text, record_id, facts)
    assert_g2_shape(facts)
    return facts


def _first(facts):
    return facts[0]


# ---------- subject ----------

def test_subject_org_suffix():
    facts = _extract("甲公司完成回购。")
    assert _first(facts)["subject"]["raw_value"] == "甲公司"


def test_subject_ministry():
    facts = _extract("发改委宣布下调成品油价格。")
    assert _first(facts)["subject"]["raw_value"] == "发改委"


def test_subject_missing_when_no_entity():
    facts = _extract("今日油价上调。")
    assert _first(facts)["subject"]["status"] == "missing"


def test_subject_full_group_name():
    facts = _extract("甲集团股份有限公司发布年报。")
    assert _first(facts)["subject"]["raw_value"] == "甲集团股份有限公司"


# ---------- predicate / fact_type / 多动词 ----------

def test_predicate_and_fact_type_same_span():
    facts = _extract("甲公司收购乙公司。")
    fact = _first(facts)
    assert fact["event_state"]["predicate"]["raw_value"] == "收购"
    assert fact["fact_type"]["raw_value"] == "收购"
    assert (fact["fact_type"]["evidence"][0]["start"]
            == fact["event_state"]["predicate"]["evidence"][0]["start"])


def test_multi_verb_clause_splits_facts():
    facts = _extract("甲公司签署协议并获批。")
    assert len(facts) == 2
    predicates = [f["event_state"]["predicate"]["raw_value"] for f in facts]
    assert predicates == ["签署", "获批"]


def test_three_verbs_three_facts():
    """一动词一 fact 不合并（设计书 §2.2 裁定）：获批+上市 各自成 fact。"""
    facts = _extract("甲公司签署协议并获批上市。")
    predicates = [f["event_state"]["predicate"]["raw_value"] for f in facts]
    assert predicates == ["签署", "获批", "上市"]


def test_no_verb_clause_no_fact():
    facts = _extract("今天天气不错。")
    assert len(facts) == 1                       # fallback minimal fact
    fact = _first(facts)
    assert fact["event_state"]["predicate"]["status"] == "missing"
    assert fact["evidence"], "fallback fact 必须有真实子句 evidence"


# ---------- polarity ----------

def test_polarity_negation_prefix():
    facts = _extract("甲公司并未回购。")
    assert _first(facts)["event_state"]["polarity"]["raw_value"] == "并未回购"


def test_polarity_completion_prefix():
    facts = _extract("甲公司已回购。")
    assert _first(facts)["event_state"]["polarity"]["raw_value"] == "已回购"


def test_polarity_bare_verb():
    facts = _extract("甲公司回购股票。")
    assert _first(facts)["event_state"]["polarity"]["raw_value"] == "回购"


# ---------- modality / attribution ----------

def test_modality_present():
    facts = _extract("甲公司拟收购乙公司。")
    assert _first(facts)["event_state"]["modality"]["raw_value"] == "拟"


def test_modality_missing():
    facts = _extract("甲公司回购股票。")
    assert _first(facts)["event_state"]["modality"]["status"] == "missing"


def test_attribution_report():
    facts = _extract("据新华社报道，甲公司完成回购。")
    assert _first(facts)["event_state"]["attribution"]["raw_value"] == "新华社"


def test_attribution_missing():
    facts = _extract("甲公司完成回购。")
    assert _first(facts)["event_state"]["attribution"]["status"] == "missing"


# ---------- numerics ----------

def test_numeric_amount():
    facts = _extract("甲公司回购100万元。")
    numerics = _first(facts)["numerics"]
    assert len(numerics) == 1
    value = numerics[0]["value"]
    assert value["raw_value"] == "100"
    assert value["value"] == "100"
    assert numerics[0]["magnitude"]["raw_value"] == "万"
    assert numerics[0]["unit"]["raw_value"] == "元"
    assert value["unit"] == "元" and value["magnitude"] == "万"


def test_numeric_thousands_decimal():
    facts = _extract("甲公司回购1,200.50元。")
    value = _first(facts)["numerics"][0]["value"]
    assert value["raw_value"] == "1,200.50"
    assert value["value"] == "1200.50"


def test_numeric_percent_and_direction():
    """（上涨非事件动词——设计书动词表只含动作词；数值归左邻动词 fact）"""
    facts = _extract("甲公司发布年报，股价上涨3.5%。")
    numerics = _first(facts)["numerics"]
    assert len(numerics) == 1
    numeric = numerics[0]
    assert numeric["unit"]["raw_value"] == "%"
    assert numeric["direction"]["raw_value"] == "上涨"
    assert numeric["value"]["value"] == "3.5"


def test_numeric_range():
    facts = _extract("甲公司减持100-200万股。")
    numeric = _first(facts)["numerics"][0]
    assert numeric["value"]["value"] == "100"
    assert numeric["range_end"]["status"] == "present"
    assert numeric["range_end"]["value"] == "200"
    assert numeric["value"]["comparator"] == "range"


def test_numeric_chinese_numeral_value_null():
    facts = _extract("甲公司回购一百万股。")
    numeric = _first(facts)["numerics"][0]
    assert numeric["value"]["raw_value"] == "一百万"
    assert numeric["value"]["value"] is None     # 09 §7.2 授权：归一后置


def test_numeric_comparator_kept():
    facts = _extract("甲公司披露净利润超5亿元。")
    numeric = _first(facts)["numerics"][0]
    assert numeric["comparator"]["raw_value"] == "超"
    assert numeric["value"]["comparator"] == "超"   # T55：不删比较符
    assert numeric["metric"]["raw_value"] == "净利润"


def test_numeric_date_components_excluded():
    facts = _extract("甲公司2026年9月26日完成回购。")
    assert _first(facts)["numerics"] == []           # 日期数字不产生 numeric


def test_numeric_security_code_excluded():
    """证券代码在动词后窗口 → key_object；已占代码不再入数值（设计书 §2.9 排除）。"""
    facts = _extract("甲公司增持600519。")
    fact = _first(facts)
    assert fact["key_object"]["raw_value"] == "600519"
    assert fact["numerics"] == []                   # 已作 key_object 的代码不再入数值


def test_numeric_id_order():
    facts = _extract("甲公司披露业绩：上涨3.5%，成交100万元。")
    numerics = _first(facts)["numerics"]
    assert [n["numeric_id"] for n in numerics] == ["n1", "n2"]
    assert numerics[0]["value"]["value"] == "3.5"
    assert numerics[1]["value"]["value"] == "100"


# ---------- time / key_object ----------

def test_time_absolute_and_stage():
    facts = _extract("甲公司2026年9月26日收盘发布年报。")
    fact = _first(facts)
    assert fact["time"]["expression"]["raw_value"] == "2026年9月26日"
    assert fact["time"]["stage"]["raw_value"] == "收盘"
    assert fact["time"]["anchor"]["status"] == "missing"


def test_time_relative():
    facts = _extract("甲公司今日完成回购。")
    assert _first(facts)["time"]["expression"]["raw_value"] == "今日"


def test_time_missing():
    facts = _extract("甲公司完成回购。")
    time = _first(facts)["time"]
    assert all(time[key]["status"] == "missing" for key in _TIME_KEYS)


def test_key_object_noun():
    facts = _extract("甲公司回购本公司股票。")
    assert _first(facts)["key_object"]["raw_value"] == "本公司股票"


def test_key_object_missing():
    facts = _extract("甲公司获批。")
    assert _first(facts)["key_object"]["status"] == "missing"


def test_numeric_pre_verb_attaches_right_verb():
    """动词前判别数字归右邻动词（23:4x fp 3139b94f 根因修正）：
    "宣传费1"型编号不再双侧对称丢弃，保留判别证据。"""
    facts = _extract("中共依安县委宣传部县域发展宣传费1中标（成交）结果公告")
    zhongbiao = [f for f in facts
                 if f["event_state"]["predicate"]["raw_value"] == "中标"]
    assert zhongbiao, "应抽出 中标 fact"
    numerics = zhongbiao[0]["numerics"]
    assert len(numerics) == 1
    assert numerics[0]["value"]["raw_value"] == "1"


def test_integration_batch_number_conflict():
    """费1 vs 费2（fp 3139b94f 原型）→ 数值冲突 → VERIFIED_CONFLICT（不重复）。"""
    history_text = "中共依安县委宣传部县域发展宣传费1中标（成交）结果公告"
    current_text = "中共依安县委宣传部县域发展宣传费2中标（成交）结果公告"
    report_a = _wrap_facts_as_report(
        RECORD_A, history_text, facts_rule.extract_facts(RECORD_A, history_text))
    report_b = _wrap_facts_as_report(
        RECORD_B, current_text, facts_rule.extract_facts(RECORD_B, current_text))
    alignment = pair_alignment.build_aligned(
        report_a, report_b, history_text, current_text,
        dictionary_version="dict_v1", alignment_version="alignment_v1")
    from news_flash_dedup.compare import p15_integration
    p15 = p15_integration.extract_p15_results(
        report_a, report_b, history_text, current_text, alignment)
    assert p15.p15_results.verified_conflicts, "1 vs 2 应产出已验证数值冲突"
    assert p15.p15_results.equivalence_ready is False


# ---------- R9 F1：否定词松散窗（外部审核阻断项回归） ----------

def test_negation_loose_window_polarity():
    """R9 外部审核 F1 回归（主窗口 06:5x）："未完成回购"中"未"被"完成"
    隔开——紧邻实现 polarity 双侧同为"回购"，正反事件会被误判等价。
    松散窗后否定侧 raw 必须含"未"。"""
    neg = facts_rule.extract_facts(RECORD_A, "甲公司未完成回购。")
    pos = facts_rule.extract_facts(RECORD_B, "甲公司完成回购。")
    neg_pol = neg[0]["event_state"]["polarity"]["raw_value"]
    pos_pol = pos[0]["event_state"]["polarity"]["raw_value"]
    assert neg_pol == "未完成回购"
    assert pos_pol == "回购"
    assert neg_pol != pos_pol


def test_integration_negation_blocks_equivalence():
    """完成回购 vs 未完成回购 → 签发壁条件 g（polarity raw 不等）拒发等价。"""
    history_text = "甲公司完成回购。"
    current_text = "甲公司未完成回购。"
    report_a = _wrap_facts_as_report(
        RECORD_A, history_text, facts_rule.extract_facts(RECORD_A, history_text))
    report_b = _wrap_facts_as_report(
        RECORD_B, current_text, facts_rule.extract_facts(RECORD_B, current_text))
    alignment = pair_alignment.build_aligned(
        report_a, report_b, history_text, current_text,
        dictionary_version="dict_v1", alignment_version="alignment_v1")
    from news_flash_dedup.compare import p15_integration
    p15 = p15_integration.extract_p15_results(
        report_a, report_b, history_text, current_text, alignment)
    assert p15.p15_results.equivalence_ready is False


# ---------- fallback / 空文本 / 参数校验 ----------

def test_empty_text_returns_empty():
    assert facts_rule.extract_facts(RECORD_A, "") == []
    assert facts_rule.extract_facts(RECORD_A, "   \n  ") == []


def test_record_id_validation():
    with pytest.raises(ValueError):
        facts_rule.extract_facts("not-hex", "甲公司完成回购。")
    with pytest.raises(ValueError):
        facts_rule.extract_facts("A" * 64, "甲公司完成回购。")


# ---------- 集成 sanity（decide 注入键 → build_aligned 硬门） ----------

def test_integration_same_text_aligns():
    text = "甲公司完成回购。"
    report_a = _wrap_facts_as_report(RECORD_A, text,
                                     facts_rule.extract_facts(RECORD_A, text))
    report_b = _wrap_facts_as_report(RECORD_B, text,
                                     facts_rule.extract_facts(RECORD_B, text))
    alignment = pair_alignment.build_aligned(
        report_a, report_b, text, text,
        dictionary_version="dict_v1", alignment_version="alignment_v1",
    )
    assert alignment.aligned_facts, "同文自对齐应成功"


def test_integration_paraphrase_aligns():
    history_text = "甲公司今日宣布回购股份。"
    current_text = "甲公司公告称回购股份。"
    report_a = _wrap_facts_as_report(
        RECORD_A, history_text, facts_rule.extract_facts(RECORD_A, history_text))
    report_b = _wrap_facts_as_report(
        RECORD_B, current_text, facts_rule.extract_facts(RECORD_B, current_text))
    alignment = pair_alignment.build_aligned(
        report_a, report_b, history_text, current_text,
        dictionary_version="dict_v1", alignment_version="alignment_v1",
    )
    assert alignment.aligned_facts, "同主体同谓词改写照理应对齐"


def test_integration_synonym_verb_no_align():
    history_text = "甲公司完成收购。"
    current_text = "甲公司完成并购。"
    report_a = _wrap_facts_as_report(
        RECORD_A, history_text, facts_rule.extract_facts(RECORD_A, history_text))
    report_b = _wrap_facts_as_report(
        RECORD_B, current_text, facts_rule.extract_facts(RECORD_B, current_text))
    alignment = pair_alignment.build_aligned(
        report_a, report_b, history_text, current_text,
        dictionary_version="dict_v1", alignment_version="alignment_v1",
    )
    assert alignment.aligned_facts == ()           # W1：同义动词对齐不了（登记弱项）


def test_integration_zero_fact_side_raises():
    text = "甲公司完成回购。"
    report_a = _wrap_facts_as_report(RECORD_A, text,
                                     facts_rule.extract_facts(RECORD_A, text))
    report_b = _wrap_facts_as_report(RECORD_B, text, [])   # 构造零 Fact 侧
    with pytest.raises(pair_alignment.PairAlignmentError):
        pair_alignment.build_aligned(
            report_a, report_b, text, text,
            dictionary_version="dict_v1", alignment_version="alignment_v1",
        )
