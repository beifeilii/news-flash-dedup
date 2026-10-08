"""W2Fα1 条目27（WA1a E 族微危卫生批 7 件）：E1-E7 红测/守卫钉。

- E1 pair_alignment _SIMILARITY_INPUT_KEYS 死常量清除 + 模块 docstring 无兑现声称对码；
- E2 llm._numeric_slot 尾部 range 重复置 missing 死代码拆除；
- E3 rule._is_identifier_numeric_v2 年份判据空串守卫不一致（"" in "年）)" 恒真）；
- E4 注释行号失锚×3（pair_compare _BOUNDARY_PRIORITY / p15 两处 value_time 与
  rule.py 行号指针）退役或校正；
- E5 core._fact_clauses_covered 覆盖集预算一次（消 O(子句×码点×span) 嵌套扫）+
  rule 每子句 _time_slots 双算消重（按子句一次 + 字段路径重卷）；
- E6 rule 区间挂载 candidates[-1] 脆弱假设 → add_amount_like 实返引用；
- E7 pair_alignment supplementary 全局标志粘滞（潜在缺陷，被 L351 守卫掩盖，
  公共 API 不可达）→ 逐对标志化（行为中性卫生修）。
"""

from __future__ import annotations

import inspect

from news_flash_dedup.compare import pair_alignment
from news_flash_dedup.compare.pair_alignment import PairAlignmentError
from news_flash_dedup.facts import core as facts_core
from news_flash_dedup.facts import llm as facts_llm
from news_flash_dedup.facts import rule as facts_rule

RID = "a" * 64


# ---------- E1：死常量 + docstring 声称对码 ----------

def test_e1_dead_similarity_input_keys_removed():
    assert not hasattr(pair_alignment, "_SIMILARITY_INPUT_KEYS")


def test_e1_module_docstring_claim_matches_reality():
    doc = pair_alignment.__doc__ or ""
    assert "对应入口参数被拒" not in doc      # 无此入口参数，声称不兑现
    assert "candidate_basis" in doc           # 实述：basis 黑白名单闸


# ---------- E2：llm 尾部死代码拆除 ----------

def test_e2_dead_range_comparator_reassign_removed():
    src = inspect.getsource(facts_llm._numeric_slot)
    assert 'if comparator == "range":' not in src, \
        "comparator 槽在构造时已按 range 置 missing，尾部重复分支=死代码"


def test_e2_range_comparator_slot_missing_behavior_pinned():
    """行为钉（前后同绿）：range 数值 comparator 槽恒 missing。"""
    def _call_fn(**_kwargs):
        return ('{"facts": [{"predicate": "升至", "subject": "指数", '
                '"polarity": null, "modality": null, "key_object": null, '
                '"time_expression": null, "time_stage": null, '
                '"attribution": null, "numerics": [{"value_raw": "100", '
                '"metric": null, "unit": "点", "magnitude": null, '
                '"comparator": null, "range_end_raw": "200点", '
                '"direction": null}]}]}', 0.01)

    facts = facts_llm.extract_facts_llm(RID, "指数升至100点至200点。",
                                        call_fn=_call_fn)
    numeric = facts[0]["numerics"][0]
    assert numeric["comparator"]["status"] == "missing"
    assert numeric["range_end"]["status"] == "present"


# ---------- E3：年份判据空串守卫不一致 ----------

def test_e3_year_like_number_at_text_end_not_excluded_v2():
    """v2：裸年份形态数字在文尾（after=""）不得按"年份"误排
    （修复前 "" in "年）)" 恒真 → 静默排除；实测 identifier_v2=True 实证）。"""
    facts = facts_rule.extract_facts(
        RID, "甲公司发布市值2025",
        dict_version=facts_rule.RULE_DICT_VERSION_V2)
    numerics = [n for fact in facts for n in fact["numerics"]]
    assert any(n["value"]["raw_value"] == "2025" for n in numerics), \
        "文尾裸 2025 应保留为数值事实（空串不等于年后缀）"


def test_e3_year_suffix_still_excluded_v2():
    """守卫（前后同绿）：真年后缀仍被排除（时间排除窗/年份判据双通道）。"""
    facts = facts_rule.extract_facts(
        RID, "甲公司发布到2027年回购股份",
        dict_version=facts_rule.RULE_DICT_VERSION_V2)
    numerics = [n for fact in facts for n in fact["numerics"]]
    assert not any(n["value"]["raw_value"] == "2027" for n in numerics)


def test_e3_bracketed_year_still_excluded_v2():
    """守卫（前后同绿）：括号年份 after="）" 仍按年份排除（rule75 本义不误伤）。"""
    facts = facts_rule.extract_facts(
        RID, "甲公司发布公告（2025）",
        dict_version=facts_rule.RULE_DICT_VERSION_V2)
    numerics = [n for fact in facts for n in fact["numerics"]]
    assert not any(n["value"]["raw_value"] == "2025" for n in numerics)


def test_e3_v1_path_unchanged():
    """守卫（前后同绿）：v1 腿不过 identifier 判据，裸 2025 始终保留。"""
    facts = facts_rule.extract_facts(RID, "甲公司发布市值2025")
    numerics = [n for fact in facts for n in fact["numerics"]]
    assert any(n["value"]["raw_value"] == "2025" for n in numerics)


# ---------- E4：注释行号失锚×3 ----------

def test_e4_pair_compare_boundary_priority_anchor_fixed():
    from news_flash_dedup.compare import pair_compare
    module_src = inspect.getsource(pair_compare)
    assert "L45-56" not in module_src, \
        "_BOUNDARY_PRIORITY 行号指针失锚（实物 L55-66），须退役或校正"


def test_e4_p15_value_time_anchors_fixed():
    from news_flash_dedup.compare import p15_integration
    module_src = inspect.getsource(p15_integration)
    assert "normalize_numeric（L238-243）" not in module_src, \
        "range_end∈quote 执法点行号指针失锚，须退役或校正"
    assert "normalize_numeric L223-230" not in module_src, \
        "verified_missing 分支行号指针失锚，须退役或校正"


# ---------- E5：core 覆盖集预算 + rule 双算消重 ----------

def test_e5_fact_clauses_covered_behavior_pinned():
    """行为钉（前后同绿）：覆盖/未覆盖两态判定不变。"""
    text = "甲公司回购。乙公司增持。"
    # 09-29 W3a-F7③ span 勘正：quote "甲公司回购。" 长 6（索引 0-5，end=6），
    # 原 fixture end=7 超文本长——惰性无害（_fact_clauses_covered 只按 span
    # 建覆盖集，钉仍咬合），按文书纪律回锚实物长。
    ev = {"record_id": RID, "field": "facts.f1", "quote": "甲公司回购。",
          "start": 0, "end": 6}
    facts_covered = [{"evidence": [ev]}]
    assert facts_core._fact_clauses_covered("甲公司回购。", facts_covered) is True
    assert facts_core._fact_clauses_covered(text, facts_covered) is False


def test_e5_time_slots_computed_once_per_clause(monkeypatch):
    """同子句多动词：_time_slots 每子句只算一次（修复前 1+N 次双算）。"""
    calls = {"n": 0}
    original = facts_rule._time_slots

    def _spy(*args, **kwargs):
        calls["n"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(facts_rule, "_time_slots", _spy)
    facts = facts_rule.extract_facts(RID, "甲公司宣布回购股份，乙公司宣布增持股份。")
    assert len(facts) == 2
    assert calls["n"] == 1, f"单子句双动词应只算 1 次（实测 {calls['n']}）"


def test_e5_time_slots_rewrap_byte_identical():
    """行为钉（前后同绿）：双动词子句两事实的时间槽内容一致且字段路径各自独立。"""
    facts = facts_rule.extract_facts(RID, "9月10日，甲公司宣布回购股份，乙公司宣布增持股份。")
    assert len(facts) == 2
    for index, fact in enumerate(facts, 1):
        expr = fact["time"]["expression"]
        assert expr["status"] == "present"
        assert expr["raw_value"] == "9月10日"
        assert expr["evidence"][0]["field"] == f"facts.f{index}.time.expression"


# ---------- E6：区间挂载实返引用 ----------

def test_e6_range_attach_uses_returned_candidate():
    src = inspect.getsource(facts_rule._numeric_candidates)
    assert "add_amount_like" in src
    # 结构钉：区间挂载不得再用 candidates[-1] 脆弱假设
    attach_region = src[src.index("def _numeric_candidates"):]
    assert "candidates[-1].range_end_core" not in attach_region, \
        "区间右端必须挂到 add_amount_like 实返的候选引用上"


def test_e6_range_extraction_behavior_pinned():
    """行为钉（前后同绿）：区间数值照常产出。"""
    facts = facts_rule.extract_facts(RID, "甲公司宣布分红100至200元。")
    numerics = [n for fact in facts for n in fact["numerics"]]
    assert any(n["value"]["comparator"] == "range" for n in numerics)
    ranged = [n for n in numerics if n["value"]["comparator"] == "range"][0]
    assert ranged["value"]["range_end"] == "200"
    assert ranged["range_end"]["status"] == "present"


# ---------- E7：supplementary 逐对标志（不粘滞） ----------

def _evidence(text, quote, field, record_id):
    start = text.index(quote)
    return {"record_id": record_id, "field": field, "quote": quote,
            "start": start, "end": start + len(quote)}


def _slot(text, quote, field, record_id):
    return {"status": "present", "raw_value": quote,
            "evidence": [_evidence(text, quote, field, record_id)]}


def _missing_slot():
    return {"status": "missing", "raw_value": None, "evidence": []}


def _two_fact_report(text, record_id, subjects, predicates):
    facts = []
    for index, (subj, pred) in enumerate(zip(subjects, predicates), 1):
        base = f"facts.f{index}"
        sentence = text if index == 1 else text
        facts.append({
            "fact_id": f"f{index}",
            "evidence": [_evidence(text, sentence, base, record_id)],
            "fact_type": _slot(text, pred, f"{base}.fact_type", record_id),
            "subject": _slot(text, subj, f"{base}.subject", record_id),
            "event_state": {
                "predicate": _slot(text, pred, f"{base}.event_state.predicate",
                                   record_id),
                "polarity": _slot(text, pred, f"{base}.event_state.polarity",
                                  record_id),
                "modality": _missing_slot(), "attribution": _missing_slot(),
            },
            "time": {"expression": _missing_slot(), "stage": _missing_slot(),
                     "anchor": _missing_slot()},
            "key_object": _missing_slot(),
            "numerics": [],
        })
    artifact = {"schema_version": "1.0", "record_id": record_id,
                "offset_unit": "unicode_code_point",
                "extraction_status": "complete", "facts": facts,
                "unparsed_spans": [], "uncertainties": []}
    from news_flash_dedup.facts import FactValidationReport
    return FactValidationReport(
        record_id=record_id, validated_complete=True,
        extraction_status="complete",
        valid_fact_ids=tuple(f["fact_id"] for f in facts),
        numeric_inventory=(), issues=(), artifact=artifact)


def test_e7_supplementary_flag_is_per_pair_in_source():
    """结构钉（红）：AlignedPair 构造不得再用 supplementary_used 全局粘滞标志
    ——须为逐对标志。粘滞缺陷当前被 L351 守卫掩盖（supplementary_set 非空
    强制 candidate_basis=SUPPLEMENTARY_RELATION → elif 分支全对 SUPPLEMENTARY），
    公共 API 不可达，但潜在缺陷本体在 append 处，按 E 族卫生裁定修逐对化。"""
    src = inspect.getsource(pair_alignment.build_aligned)
    assert "supplementary=supplementary_used" not in src, \
        "supplementary 必须按对计算（= basis 是否 SUPPLEMENTARY_RELATION），不得粘滞"


def test_e7_supplementary_semantics_pinned():
    """行为钉（前后同绿）：可达配置下逐对语义不变——
    全局 basis 全 True；无 basis 无清单全 False；清单无匹配对 → 拒。"""
    import pytest
    text_h = "甲公司回购。乙公司增持。"
    text_c = "甲公司回购。乙公司增持。"
    history = _two_fact_report(text_h, RID, ("甲公司", "乙公司"), ("回购", "增持"))
    current = _two_fact_report(text_c, "b" * 64, ("甲公司", "乙公司"),
                               ("回购", "增持"))
    outcome = pair_alignment.build_aligned(
        history, current, text_h, text_c,
        dictionary_version="dict_v1",
        candidate_basis="SUPPLEMENTARY_RELATION",
        supplementary_pairs=(("f1", "f1"),))
    assert all(p.supplementary for p in outcome.aligned_facts)
    assert all(p.basis == "SUPPLEMENTARY_RELATION"
               for p in outcome.aligned_facts)

    plain = pair_alignment.build_aligned(
        history, current, text_h, text_c, dictionary_version="dict_v1")
    assert all(not p.supplementary for p in plain.aligned_facts)

    unmatched_h = _two_fact_report("甲公司回购。", RID, ("甲公司",), ("回购",))
    unmatched_c = _two_fact_report("乙公司增持。", "b" * 64, ("乙公司",), ("增持",))
    with pytest.raises(PairAlignmentError):
        pair_alignment.build_aligned(
            unmatched_h, unmatched_c, "甲公司回购。", "乙公司增持。",
            dictionary_version="dict_v1",
            candidate_basis="SUPPLEMENTARY_RELATION",
            supplementary_pairs=(("f9", "f9"),))      # 清单提供但零对命中 → 拒
    with pytest.raises(PairAlignmentError):
        pair_alignment.build_aligned(
            history, current, text_h, text_c, dictionary_version="dict_v1",
            supplementary_pairs=(("f1", "f1"),))       # 清单无全局 basis → 拒
