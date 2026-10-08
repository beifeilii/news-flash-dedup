"""N21 专项：白名单对齐（D28 批准第 2 件）+ range 证据链 + 路径A 精修（D31 用户裁定②）。

三项裁定/核验齐备（窗口I《方案一致性核验-09.md》N21=一致、N29 局部段=冲突→路径A）：

1. H-01/N21 崩溃链根治（09 §11.3/§12.1 锚）：pair_compare.UNRESOLVED_CODES
   按 09 §12.1 十五码表（L1315）补 DEPENDENCY_TIMEOUT/EXTRACTION_FAILED/
   EVIDENCE_INVALID 三码；_BOUNDARY_PRIORITY（L45-56，同 09 §12.1 分层3
   L1326 优先序）本含此三码=死代码复活。09 §12.1 L1329："有持久有效解释的
   边界是正常业务成功"——证据无效落边界，技术失败通道仅留存储/原文/身份
   损坏。LLM 腿 7e9664be（ComparePairError: P15 issue code 'EVIDENCE_INVALID'
   is not in UNRESOLVED_CODES，红证据 p23-replay-uat041152.json）即本链。
2. range 证据核查结论 **(a)**：facts 工件 numeric 条目以兄弟 range_end 槽
   携带区间右端 evidence（rule.py L1088-1095 / llm.py L227-232 均产出，
   field 以 .range_end 结尾、真实 span 可核）→ p15_integration._spec_from_slot
   接线透传，facts/rule.py 零改动。无兄弟槽/证据校验不过 → 维持 None →
   normalize_numeric 判 EVIDENCE_INVALID → unresolved（fail-closed 诚实）。
3. 路径A 精修（D31 用户裁定②；窗口I 冲突裁决消解）：**verified 单侧缺失
   → compatible**（09"合法缺失允许重复"本义：§8.3 表首两行、§11.2
   L1010-1011 伪代码、§10.2"A100/B缺值/C101"、T10/T19 预期、§12.2 L1340
   "真实时间或数值缺失……不自动边界"）；**unverified（抽取未命中≠原文
   确无，N29 主体不动）→ 维持诚实 unknown**（normalize 判 invalid
   FACT_INCOMPLETE → 未决集 → 边界）。双向钉值：直注 verified_missing=
   True/False 槽位；规则/LLM 供给均不产出 True（facts 层 grep 实证），
   三腿锚零影响，钉值面向真 P14/LLM 未来供给。

红能力纪律：标【红能力】的用例在修复前代码下必红（崩溃或旧语义），逐条
经旧码复跑验证（证据见 log/N21白名单对齐实施报告.md §二）；保留面锚
（unverified 维持 unknown）修复前后皆绿。
"""

from __future__ import annotations

from fractions import Fraction

from news_flash_dedup.compare import (
    aggregate as aggregate_module,
    p15_integration,
    pair_alignment,
    pair_compare,
)
from news_flash_dedup.compare.aggregate import CoverageStatus, FrozenRecallPlan
from news_flash_dedup.compare.pair_compare import PairIssue, P15PairResults
from news_flash_dedup.compare.value_time import (
    AlignmentProof,
    EvidenceRef,
    NumericSpec,
    TimeSpec,
    compare_numeric,
    compare_time,
    normalize_numeric,
    normalize_time,
)
from news_flash_dedup.facts import FactValidationReport


RECORD_ID_H = "a" * 64
RECORD_ID_C = "b" * 64
ALIGNED = AlignmentProof(confirmed=True, basis="N21 测试对齐证明")


def _evidence(text, quote, field, *, record_id):
    start = text.index(quote)
    return {"record_id": record_id, "field": field, "quote": quote,
            "start": start, "end": start + len(quote)}


def _missing():
    return {"status": "missing", "raw_value": None, "evidence": []}


def _verified_missing():
    """真 P14"原文确无"槽（抽取层显式 verified_missing=True）。"""
    return {"status": "missing", "raw_value": None, "evidence": [],
            "verified_missing": True}


def _present(text, quote, field, *, record_id):
    return {"status": "present", "raw_value": quote,
            "evidence": [_evidence(text, quote, field, record_id=record_id)]}


def _range_value_slot(text, start_raw, end_raw, *, record_id,
                      fact_id="f1", nid="n1", unit="毫米"):
    """range 形态 value 槽（'2毫米至19毫米' 的左端；inline range_end=右端裸串）。"""
    nbase = f"facts.{fact_id}.numerics.{nid}"
    slot = _present(text, start_raw, nbase + ".value", record_id=record_id)
    slot["unit"] = unit
    slot["comparator"] = "range"
    slot["range_end"] = end_raw
    return slot


def _range_end_slot(text, end_raw, *, record_id, fact_id="f1", nid="n1"):
    """兄弟 range_end 槽（rule.py L1088-1095 / llm.py L227-232 产出形态）。"""
    nbase = f"facts.{fact_id}.numerics.{nid}"
    return _present(text, end_raw, nbase + ".range_end", record_id=record_id)


def _numeric_entry(value_slot, range_slot=None):
    return {
        "numeric_id": "n1",
        "evidence": value_slot.get("evidence") or [],
        "metric": _missing(),
        "value": value_slot,
        "range_end": range_slot if range_slot is not None else _missing(),
        "magnitude": _missing(),
        "unit": _missing(),
        "currency": _missing(),
        "role": _missing(),
        "comparator": _missing(),
        "direction": _missing(),
        "time": _missing(),
    }


def _make_fact(text, *, fact_id="f1", subject="甲公司", predicate="回购",
               record_id=RECORD_ID_H, time_slot=None, numerics=()):
    base = f"facts.{fact_id}"
    return {
        "fact_id": fact_id,
        "evidence": [_evidence(text, text, base, record_id=record_id)],
        "fact_type": _present(text, predicate, base + ".fact_type", record_id=record_id),
        "subject": _present(text, subject, base + ".subject", record_id=record_id),
        "event_state": {
            "predicate": _present(text, predicate, base + ".event_state.predicate",
                                  record_id=record_id),
            "polarity": _present(text, predicate, base + ".event_state.polarity",
                                 record_id=record_id),
            "modality": _missing(),
            "attribution": _missing(),
        },
        "time": {"expression": time_slot if time_slot is not None else _missing(),
                 "stage": _missing(), "anchor": _missing()},
        "key_object": _missing(),
        "numerics": list(numerics),
    }


def _make_report(text, facts, *, record_id):
    return FactValidationReport(
        record_id=record_id,
        validated_complete=bool(facts),
        extraction_status="complete" if facts else "failed",
        valid_fact_ids=tuple(f["fact_id"] for f in facts),
        numeric_inventory=(),
        issues=(),
        artifact={
            "schema_version": "1.0", "record_id": record_id,
            "offset_unit": "unicode_code_point",
            "extraction_status": "complete" if facts else "failed",
            "facts": facts, "unparsed_spans": [], "uncertainties": [],
        },
    )


def _ctx(*, record_id, item_id, text, arrival_seq):
    import hashlib

    return {"record_id": record_id, "item_id": item_id, "text": text,
            "raw_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "scope_id": "default", "business_date": "2026-09-28",
            "arrival_seq": arrival_seq, "pipeline_version": "dedup_v1"}


def _scaffold(history_text, current_text, *, h_time=None, c_time=None,
              h_numerics=(), c_numerics=(), h_predicate="回购",
              c_predicate="回购"):
    history_facts = [_make_fact(history_text, record_id=RECORD_ID_H,
                                predicate=h_predicate,
                                time_slot=h_time, numerics=h_numerics)]
    current_facts = [_make_fact(current_text, record_id=RECORD_ID_C,
                                predicate=c_predicate,
                                time_slot=c_time, numerics=c_numerics)]
    history = _make_report(history_text, history_facts, record_id=RECORD_ID_H)
    current = _make_report(current_text, current_facts, record_id=RECORD_ID_C)
    alignment = pair_alignment.build_aligned(
        history, current, history_text, current_text,
        dictionary_version="dict_v1", alignment_version="alignment_v1",
    )
    history_ctx = _ctx(record_id=RECORD_ID_H, item_id="item-H",
                       text=history_text, arrival_seq=1)
    current_ctx = _ctx(record_id=RECORD_ID_C, item_id="item-C",
                       text=current_text, arrival_seq=2)
    return history, current, alignment, history_ctx, current_ctx


def _run(history_text, current_text, *, h_time=None, c_time=None,
         h_numerics=(), c_numerics=(), h_predicate="回购", c_predicate="回购"):
    """build_aligned → extract_p15_results → compare_pair → aggregate 全链。"""
    history, current, alignment, history_ctx, current_ctx = _scaffold(
        history_text, current_text, h_time=h_time, c_time=c_time,
        h_numerics=h_numerics, c_numerics=c_numerics,
        h_predicate=h_predicate, c_predicate=c_predicate)
    p15 = p15_integration.extract_p15_results(
        history, current, history_text, current_text, alignment,
    )
    pair = pair_compare.compare_pair(
        history_ctx, current_ctx,
        history_artifact=history, current_artifact=current,
        alignment=alignment, pipeline_version="dedup_v1",
        p15_results=p15.p15_results,
    )
    plan = FrozenRecallPlan(version="rrf_v1", required={RECORD_ID_H: history_ctx})
    out = aggregate_module.aggregate(
        current_ctx, plan, [pair],
        coverage=CoverageStatus(visible_seq=10, prepared_seq=10, complete=True),
    )
    return pair, out, p15


# ---------- 点位 1：UNRESOLVED_CODES 补三码（09 §12.1 L1315/L1326/L1329） ----------

def test_n21_unresolved_codes_aligned_with_09_12_1():
    """【红能力·点位1】DEPENDENCY_TIMEOUT/EXTRACTION_FAILED/EVIDENCE_INVALID
    三码 ∈ 09 §12.1 十五码表（L1315）且 ∈ §12.1 分层3 主边界码优先序
    （L1326）——_BOUNDARY_PRIORITY（pair_compare L45-56）本含此三码，
    白名单补齐后码集与优先序表完全重合（死代码复活）；原七码一词不动。
    修复前白名单缺三码 → 本断言红。"""
    for code in ("DEPENDENCY_TIMEOUT", "EXTRACTION_FAILED", "EVIDENCE_INVALID"):
        assert code in pair_compare.UNRESOLVED_CODES, (
            f"09 §12.1 合法边界码 {code} 不在 UNRESOLVED_CODES（N21 崩溃链）"
        )
    assert set(pair_compare.UNRESOLVED_CODES) == set(pair_compare._BOUNDARY_PRIORITY), (
        "白名单与 _BOUNDARY_PRIORITY（09 §12.1 分层3 优先序）应完全重合"
    )
    # 既有词汇不动（只补不删不改）
    for code in ("SUBJECT_UNRESOLVED", "FACT_INCOMPLETE", "TIME_RELATION_UNCERTAIN",
                 "NUMERIC_ALIGNMENT_FAILED", "RULE_UNCOVERED", "RECALL_INCOMPLETE",
                 "CANDIDATE_BUDGET_EXHAUSTED"):
        assert code in pair_compare.UNRESOLVED_CODES


def test_n21_three_codes_land_boundary_via_compare_pair():
    """【红能力·点位1 决策面】P15 产物携带三码 issue → 修复前
    ComparePairError（'not in UNRESOLVED_CODES'，LLM 腿 7e9664be 同型崩溃）
    → 修复后 unresolved 落边界、主码即该码（单 issue 无优先序竞争）。"""
    history, current, alignment, history_ctx, current_ctx = _scaffold(
        "甲公司完成回购。", "甲公司完成回购。")
    for code in ("DEPENDENCY_TIMEOUT", "EXTRACTION_FAILED", "EVIDENCE_INVALID"):
        pair = pair_compare.compare_pair(
            history_ctx, current_ctx,
            history_artifact=history, current_artifact=current,
            alignment=alignment, pipeline_version="dedup_v1",
            p15_results=P15PairResults(issues=(PairIssue(code, f"{code} 模拟"),)),
        )
        assert pair.outcome == "unresolved"
        assert pair.code == code, f"{code} 应落边界主码而非崩溃"


# ---------- 点位 2（结论 a）：range_end_evidence 自兄弟槽接线透传 ----------

def test_n21_range_end_evidence_wired_from_sibling_slot():
    """【红能力·点位2】facts 工件兄弟 range_end 槽携带 evidence（结论 (a)）
    → _spec_from_slot 透传进 NumericSpec.range_end_evidence → normalize_numeric
    归一成功（comparator=range、range_end=Fraction(19)）。修复前接线不存在
    （L73 恒 None）→ spec.range_end_evidence is None 断言红。
    保留面（fail-closed 不动）：无兄弟槽 / 兄弟槽 evidence record_id 不符
    → None → EVIDENCE_INVALID。"""
    text = "甲公司公布钢板厚度2毫米至19毫米。"
    value_slot = _range_value_slot(text, "2", "19", record_id=RECORD_ID_H)
    range_slot = _range_end_slot(text, "19", record_id=RECORD_ID_H)

    spec = p15_integration._spec_from_slot(
        value_slot, text, RECORD_ID_H, "facts.f1.numerics.n1.value",
        range_end_slot=range_slot)
    assert spec.range_end == "19"
    assert spec.range_end_evidence is not None, "兄弟槽 range evidence 必须接线透传"
    assert spec.range_end_evidence.field.endswith(".range_end")
    checked = normalize_numeric(text, RECORD_ID_H, spec)
    assert checked.valid, f"接线后 range 数值应归一成功：{checked.issue_code}"
    assert checked.comparator == "range"
    assert checked.range_end == Fraction(19)
    assert checked.canonical_unit == "length_mm"

    # 【保留面 1】无兄弟槽（旧供给/旧工件）→ None → EVIDENCE_INVALID 不崩溃
    spec_none = p15_integration._spec_from_slot(
        value_slot, text, RECORD_ID_H, "facts.f1.numerics.n1.value")
    assert spec_none.range_end_evidence is None
    checked_none = normalize_numeric(text, RECORD_ID_H, spec_none)
    assert not checked_none.valid and checked_none.issue_code == "EVIDENCE_INVALID"

    # 【保留面 2】兄弟槽 evidence record_id 不符 → 拒收 → None → EVIDENCE_INVALID
    bad_range_slot = {"status": "present", "raw_value": "19",
                      "evidence": [_evidence(text, "19",
                                             "facts.f1.numerics.n1.range_end",
                                             record_id="x" * 64)]}
    spec_bad = p15_integration._spec_from_slot(
        value_slot, text, RECORD_ID_H, "facts.f1.numerics.n1.value",
        range_end_slot=bad_range_slot)
    assert spec_bad.range_end_evidence is None
    checked_bad = normalize_numeric(text, RECORD_ID_H, spec_bad)
    assert not checked_bad.valid and checked_bad.issue_code == "EVIDENCE_INVALID"


def test_n21_range_pair_without_range_evidence_lands_boundary_not_crash():
    """【红能力·点位3】range 数值对（value_time.py:52 注释的 '2毫米至19毫米'
    形态）：range_end 在场但抽取未供 range evidence（兄弟槽 missing，旧工件
    形态）→ normalize_numeric 判 EVIDENCE_INVALID → 落边界不崩溃。
    修复前：PairIssue('EVIDENCE_INVALID') 越出白名单 → ComparePairError
    （LLM 腿 7e9664be 同型）→ 本用例以崩溃形式红。"""
    text_h = "甲公司公布钢板厚度2毫米至19毫米。"
    text_c = "甲公司公布钢板厚度2毫米至19毫米。"
    h_entry = _numeric_entry(_range_value_slot(text_h, "2", "19",
                                               record_id=RECORD_ID_H))
    c_entry = _numeric_entry(_range_value_slot(text_c, "2", "19",
                                               record_id=RECORD_ID_C))
    pair, out, p15 = _run(text_h, text_c,
                          h_numerics=(h_entry,), c_numerics=(c_entry,),
                          h_predicate="公布", c_predicate="公布",
                          h_time=_verified_missing(), c_time=_verified_missing())
    assert any(i.code == "EVIDENCE_INVALID" for i in p15.p15_results.issues), (
        "range 证据缺失必须 fail-closed 记 EVIDENCE_INVALID（不静默吞）"
    )
    assert pair.outcome == "unresolved"
    assert pair.code == "EVIDENCE_INVALID", "崩溃链根治后落边界主码 EVIDENCE_INVALID"
    assert out.decision == "边界case/疑难case"


def test_n21_range_pair_with_wired_evidence_compares_equal_and_conflict():
    """【红能力·点位2 决策面】兄弟槽 evidence 接线后 range 数值对真实可比：
    双侧 '2毫米至19毫米' → equal（不再产 EVIDENCE_INVALID，FACT_EQUIVALENT
    签发 → 重复）；一侧 '2毫米至20毫米' → 区间右端实质差异 → VERIFIED_CONFLICT
    → 不重复。修复前接线不存在 → 双侧 EVIDENCE_INVALID → 崩溃/边界，两断言皆红。"""
    text_h = "甲公司公布钢板厚度2毫米至19毫米。"
    same_c = "甲公司公布钢板厚度2毫米至19毫米！"
    diff_c = "甲公司公布钢板厚度2毫米至20毫米。"

    h_same = _numeric_entry(
        _range_value_slot(text_h, "2", "19", record_id=RECORD_ID_H),
        _range_end_slot(text_h, "19", record_id=RECORD_ID_H))
    c_same = _numeric_entry(
        _range_value_slot(same_c, "2", "19", record_id=RECORD_ID_C),
        _range_end_slot(same_c, "19", record_id=RECORD_ID_C))
    pair, out, p15 = _run(text_h, same_c,
                          h_numerics=(h_same,), c_numerics=(c_same,),
                          h_predicate="公布", c_predicate="公布",
                          h_time=_verified_missing(), c_time=_verified_missing())
    assert not [i for i in p15.p15_results.issues if i.code == "EVIDENCE_INVALID"], (
        "range evidence 接线后不得再产 EVIDENCE_INVALID"
    )
    assert not p15.p15_results.verified_conflicts
    assert pair.outcome == "equivalent"
    assert out.decision == "重复"

    h_diff = _numeric_entry(
        _range_value_slot(text_h, "2", "19", record_id=RECORD_ID_H),
        _range_end_slot(text_h, "19", record_id=RECORD_ID_H))
    c_diff = _numeric_entry(
        _range_value_slot(diff_c, "2", "20", record_id=RECORD_ID_C),
        _range_end_slot(diff_c, "20", record_id=RECORD_ID_C))
    pair2, out2, p15_2 = _run(text_h, diff_c,
                              h_numerics=(h_diff,), c_numerics=(c_diff,),
                              h_predicate="公布", c_predicate="公布",
                              h_time=_verified_missing(), c_time=_verified_missing())
    assert p15_2.p15_results.verified_conflicts, "range 右端 19 vs 20 必须判已验证冲突"
    assert pair2.outcome == "conflict"
    assert pair2.code == "VERIFIED_CONFLICT"
    assert out2.decision == "不重复"


# ---------- 点位 3（D31 路径A）：时间 verified 单侧缺失 → compatible ----------

def test_d31_compare_time_verified_one_sided_missing_compatible_bidirectional():
    """【红能力·点位3a 单元层】present vs verified 缺失 → compatible（双向）
    ——09 §11.2 L1010-1011 伪代码明文回归（verified 检查在 L445-446 先行，
    抵达 missing 分支的缺失槽必 verified）；§8.3 表首"一方真正没有时间…
    允许重复"；T19 预期。修复前 C-11 判 unresolved(TIME_RELATION_UNCERTAIN)
    → 本断言红。保留面：unverified 单侧 → unresolved(FACT_INCOMPLETE) 不动。"""
    text_h = "甲公司今日实施回购。"
    today = normalize_time(text_h, RECORD_ID_H, TimeSpec(
        status="present", raw_value="今日",
        evidence=EvidenceRef(RECORD_ID_H, "facts.f1.time.expression",
                             "今日", text_h.index("今日"), text_h.index("今日") + 2),
    ))
    verified_absent = normalize_time("实施回购。", RECORD_ID_C, TimeSpec(
        status="missing", raw_value=None, verified_missing=True))
    unverified_absent = normalize_time("实施回购。", RECORD_ID_C, TimeSpec(
        status="missing", raw_value=None))

    assert verified_absent.valid, "verified 确无槽应 valid（MISSING_VERIFIED 同义）"
    assert not unverified_absent.valid
    assert unverified_absent.issue_code == "FACT_INCOMPLETE"

    one_sided = compare_time(today, verified_absent, alignment=ALIGNED)
    assert one_sided.relation == "compatible", "D31：verified 单侧缺失 → compatible"
    mirrored = compare_time(verified_absent, today, alignment=ALIGNED)
    assert mirrored.relation == "compatible", "D31：verified 单侧缺失（镜像）→ compatible"

    # 【保留面】unverified（抽取未命中≠原文确无，N29 主体不动）→ 维持 unknown
    stay = compare_time(today, unverified_absent, alignment=ALIGNED)
    assert stay.relation == "unresolved"
    assert stay.reason_code == "FACT_INCOMPLETE"
    stay_mirror = compare_time(unverified_absent, today, alignment=ALIGNED)
    assert stay_mirror.relation == "unresolved"
    assert stay_mirror.reason_code == "FACT_INCOMPLETE"

    # 【保留面】双侧 verified 确无 → compatible 不动（09 §11.3 本义）
    both = compare_time(verified_absent,
                        normalize_time("公告收购。", RECORD_ID_H, TimeSpec(
                            status="missing", raw_value=None, verified_missing=True)),
                        alignment=ALIGNED)
    assert both.relation == "compatible"


def test_d31_compare_numeric_missing_signal_names_preserved_bidirectional():
    """【点位3b 单元层】compare_numeric 具名信号不动：verified 单侧 →
    missing_in_history / missing_in_new（compatible 的消费判定归 p15 层）；
    unverified 单侧 → unresolved(FACT_INCOMPLETE) 双向不动（N29 主体）。"""
    text = "现价100元"
    start = text.index("100")
    present = normalize_numeric(text, RECORD_ID_H, NumericSpec(
        status="present", raw_value="100",
        evidence=EvidenceRef(RECORD_ID_H, "facts.f1.numerics.n1.value",
                             "100", start, start + 3),
        unit="元", dimension="money"))
    verified_missing = normalize_numeric("现价未披露", RECORD_ID_C, NumericSpec(
        status="missing", raw_value=None, unit="元", dimension="money",
        verified_missing=True))
    unverified_missing = normalize_numeric("现价100元", RECORD_ID_C, NumericSpec(
        status="missing", raw_value=None, unit="元", dimension="money"))

    assert verified_missing.valid, "verified 确无槽应 valid（MISSING_VERIFIED）"
    assert compare_numeric(verified_missing, present,
                           alignment=ALIGNED).status == "missing_in_history"
    assert compare_numeric(present, verified_missing,
                           alignment=ALIGNED).status == "missing_in_new"
    assert not unverified_missing.valid
    assert compare_numeric(unverified_missing, present,
                           alignment=ALIGNED).status == "unresolved"
    assert compare_numeric(unverified_missing, present,
                           alignment=ALIGNED).reason_code == "FACT_INCOMPLETE"
    assert compare_numeric(present, unverified_missing,
                           alignment=ALIGNED).status == "unresolved"


def test_d31_t19_one_sided_time_verified_missing_allows_duplicate():
    """【红能力·点位3a 集成面】09 T19 形态：一方确无时间（verified）、一方
    明确时间、其他核心一致 → compatible 不产 time_pairs → FACT_EQUIVALENT
    签发 → 重复（§8.3 表首"不能因缺少日期边界"）。修复前 C-11 路由
    TIME_RELATION_UNCERTAIN → 边界 → 本断言红。"""
    current_text = "甲公司2025年9月26日完成回购。"
    c_time = _present(current_text, "2025年9月26日", "facts.f1.time.expression",
                      record_id=RECORD_ID_C)
    # history 确无 vs current 有值
    pair, out, p15 = _run("甲公司完成回购。", current_text,
                          h_time=_verified_missing(), c_time=c_time)
    assert p15.p15_results.time_pairs == (), "verified 单侧缺失不再产未决信号"
    assert p15.p15_results.equivalence_ready is True
    assert p15.p15_results.text_proof == "FACT_EQUIVALENT"
    assert pair.outcome == "equivalent"
    assert out.decision == "重复"
    assert out.duplicate_ids == ("item-H",)

    # 镜像：history 有值 vs current 确无
    history_text = "甲公司2025年9月26日完成回购。"
    h_time = _present(history_text, "2025年9月26日", "facts.f1.time.expression",
                      record_id=RECORD_ID_H)
    pair2, out2, p15_2 = _run(history_text, "甲公司完成回购。",
                              h_time=h_time, c_time=_verified_missing())
    assert p15_2.p15_results.time_pairs == ()
    assert pair2.outcome == "equivalent"
    assert out2.decision == "重复"


def test_d31_one_sided_time_unverified_missing_stays_boundary():
    """【保留面·点位3a 集成面】抽取未命中型单侧缺失（unverified）→ 维持
    诚实 unknown：time_pairs 记 unresolved(FACT_INCOMPLETE) → FACT_EQUIVALENT
    拒签发 → 边界（N29 主体不动；修复前后皆绿）。"""
    current_text = "甲公司2025年9月26日完成回购。"
    c_time = _present(current_text, "2025年9月26日", "facts.f1.time.expression",
                      record_id=RECORD_ID_C)
    pair, out, p15 = _run("甲公司完成回购。", current_text, c_time=c_time)
    assert p15.p15_results.time_pairs, "unverified 单侧缺失必须如实记录"
    assert all(t.outcome == "unresolved" for t in p15.p15_results.time_pairs)
    assert p15.p15_results.time_pairs[0].code == "FACT_INCOMPLETE"
    assert p15.p15_results.equivalence_ready is False
    assert pair.outcome == "unresolved"
    assert out.decision == "边界case/疑难case"


# ---------- 点位 4（D31 路径A）：数值 verified 单侧缺失 → compatible 不发 issue ----------

def test_d31_t10_one_sided_numeric_verified_missing_allows_duplicate():
    """【红能力·点位4 集成面】09 §10.2/T10 形态：同主体同事件，一方 100 元、
    一方正文确未给值（verified_missing）→ missing_in_* 信号在 p15 层消费为
    compatible（不发 issue）→ FACT_EQUIVALENT 签发 → 重复。修复前 C-10 路由
    FACT_INCOMPLETE 未决 → 边界 → 本断言红。双向各钉一次。"""
    # history 有值 vs current 确无（09 §10.2 A-B 形态）
    h_entry = _numeric_entry(_present("甲公司公布该商品现价100元。", "100",
                                      "facts.f1.numerics.n1.value",
                                      record_id=RECORD_ID_H))
    c_entry = _numeric_entry(_verified_missing())
    pair, out, p15 = _run("甲公司公布该商品现价100元。", "甲公司公布该商品现价。",
                          h_numerics=(h_entry,), c_numerics=(c_entry,),
                          h_predicate="公布", c_predicate="公布",
                          h_time=_verified_missing(), c_time=_verified_missing())
    assert not [i for i in p15.p15_results.issues if "missing_in" in i.detail], (
        "D31：verified 单侧数值缺失不再发 FACT_INCOMPLETE 具名 issue"
    )
    assert not p15.p15_results.verified_conflicts, "单侧缺失不得判已验证冲突"
    assert pair.outcome == "equivalent"
    assert pair.code == "FACT_EQUIVALENT"
    assert out.decision == "重复"

    # 镜像：history 确无 vs current 有值
    h_entry2 = _numeric_entry(_verified_missing())
    c_entry2 = _numeric_entry(_present("甲公司公布该商品现价100元。", "100",
                                       "facts.f1.numerics.n1.value",
                                       record_id=RECORD_ID_C))
    pair2, out2, p15_2 = _run("甲公司公布该商品现价。", "甲公司公布该商品现价100元。",
                              h_numerics=(h_entry2,), c_numerics=(c_entry2,),
                              h_predicate="公布", c_predicate="公布",
                              h_time=_verified_missing(), c_time=_verified_missing())
    assert not [i for i in p15_2.p15_results.issues if "missing_in" in i.detail]
    assert pair2.outcome == "equivalent"
    assert out2.decision == "重复"


def test_d31_one_sided_numeric_unverified_missing_stays_boundary():
    """【保留面·点位4 集成面】抽取未命中型单侧数值缺失（unverified）→
    维持 FACT_INCOMPLETE 具名 issue → 边界（N29 主体不动；修复前后皆绿）。"""
    h_entry = _numeric_entry(_present("甲公司公布该商品现价100元。", "100",
                                      "facts.f1.numerics.n1.value",
                                      record_id=RECORD_ID_H))
    c_entry = _numeric_entry(_missing())          # 抽取未命中（无 verified_missing）
    pair, out, p15 = _run("甲公司公布该商品现价100元。", "甲公司公布该商品现价。",
                          h_numerics=(h_entry,), c_numerics=(c_entry,),
                          h_predicate="公布", c_predicate="公布",
                          h_time=_verified_missing(), c_time=_verified_missing())
    assert any(i.code == "FACT_INCOMPLETE" for i in p15.p15_results.issues), (
        "unverified 单侧数值缺失必须维持 FACT_INCOMPLETE 具名 issue（N29 不动）"
    )
    assert pair.outcome == "unresolved"
    assert out.decision == "边界case/疑难case"
