"""窗口W1Fα compare 侧两项修复红绿钉（主窗口处方，红测先行逐项）。

- W1Fa-1（p15 docstring 精度，comment 级）：p15_integration._time_spec_from_slot
  docstring 旧述"stage_evidence=None → normalize_time 判 EVIDENCE_INVALID
  → unresolved"为无条件表述失实——value_time normalize_time 现役兜底为
  条件路径：stage 词 ∈ 已校验 expression 引文时引文兜底锚定判 valid；
  仅当无锚且 stage 词不在引文内才判 EVIDENCE_INVALID → unresolved。
  本窗仅改表述，行为零改动（value_time.py 禁读改；兜底行为由运行时
  探针实证并钉在本文件行为钉①②，前后均绿）。
- W1Fa-2（VerifiedConflict basis 标签实传，判定壁，证据保真级）：
  pair_compare 时间 conflict 入列 VerifiedConflict 时 basis 恒硬编码
  "TIME_SAME_RELATIVE"——p15 白名单允 TIME_SAME_ABSOLUTE/TIME_SAME_STAGE，
  非默认 time_basis 下标签与实际基底不符。修成按 time_pair 实际基底实传：
  TimePairComparison 增可选 basis 字段，p15_integration 以其调 compare_time
  的同一 time_basis 写入（与 AlignmentProof 内嵌基底同源；compare_time
  返回 TimeComparison 仅 relation/reason_code，结构上不携基底字段——
  基底真值在集成层调用参数处），compare_pair 消费侧白名单闸 + 外部旧式
  元组（无基底）回退默认 TIME_SAME_RELATIVE（现役契约零漂移）。
  不影响 outcome、不影响 audit basis（=pair.code，R2-H4 在案）。
"""
from __future__ import annotations

import copy
import hashlib

import pytest

from news_flash_dedup.compare import pair_alignment, pair_compare, p15_integration
from news_flash_dedup.compare.pair_compare import (
    ComparePairError,
    P15PairResults,
    TimePairComparison,
)
from news_flash_dedup.compare.value_time import EvidenceRef, TimeSpec, normalize_time
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


# ============================== W1Fa-1 docstring 条件路径实述 ==============================

W1FA1_TEXT = "9月10日开盘，甲公司公告回购100股。"


def _ev(raw: str, field: str) -> EvidenceRef:
    start = W1FA1_TEXT.index(raw)
    return EvidenceRef(RECORD_ID_H, field, raw, start, start + len(raw))


def test_w1fa1_docstring_describes_conditional_stage_evidence_path():
    """W1Fa-1 主红测：_time_spec_from_slot docstring 必须实述条件路径——
    修复前无条件链"stage_evidence=None → normalize_time L482 判
    EVIDENCE_INVALID → unresolved"失实（value_time 现役引文兜底未述）。"""
    doc = p15_integration._time_spec_from_slot.__doc__
    # 条件分派实述在场（引文兜底 valid / 无锚且不在引文才 EVIDENCE_INVALID）
    assert "引文" in doc
    assert "兜底" in doc
    assert "仅当无锚" in doc
    assert "EVIDENCE_INVALID" in doc
    # 旧无条件链表述已拆除（"L482"行号指针随之一并退役，免行号漂移再失实）
    assert "normalize_time L482" not in doc


def test_w1fa1_stage_word_in_expression_citation_rescued_valid_guard():
    """W1Fa-1 行为钉①（前后均绿）：stage_evidence=None 但 stage 词 ∈ 已校验
    expression 引文 → normalize_time 引文兜底锚定判 valid（stage 归一照常）——
    docstring 新表述的实读依据，兼钉本窗 comment-only 零行为改动。"""
    spec = TimeSpec(
        status="present", raw_value="9月10日开盘",
        evidence=_ev("9月10日开盘", "facts.f1.time.expression"),
        stage="开盘", stage_evidence=None,
        anchor_evidence=None, anchor_relation=None)
    checked = normalize_time(W1FA1_TEXT, RECORD_ID_H, spec)
    assert checked.valid is True
    assert checked.issue_code is None
    assert checked.stage == "open"


def test_w1fa1_stage_word_unanchored_outside_citation_evidence_invalid_guard():
    """W1Fa-1 行为钉②（前后均绿）：stage_evidence=None 且 stage 词不在
    expression 引文内、无锚 → EVIDENCE_INVALID（fail-closed 路径实存，
    不静默丢弃 stage 主张）。"""
    spec = TimeSpec(
        status="present", raw_value="9月10日",
        evidence=_ev("9月10日", "facts.f1.time.expression"),
        stage="开盘", stage_evidence=None,
        anchor_evidence=None, anchor_relation=None)
    checked = normalize_time(W1FA1_TEXT, RECORD_ID_H, spec)
    assert checked.valid is False
    assert checked.issue_code == "EVIDENCE_INVALID"


# ============================== W1Fa-2 basis 实传 ==============================

# 同主体同数值异阶段（开盘100 vs 收盘100）：09 §8.3 规格对（窗口Z1 R2-H2 在案）。
STAGE_HISTORY_TEXT = "9月10日开盘，甲公司公告回购100股。"
STAGE_CURRENT_TEXT = "9月10日收盘，甲公司公告回购100股。"


def _stage_pair_reports(dict_version: str):
    history_facts = facts_rule.extract_facts(
        RECORD_ID_H, STAGE_HISTORY_TEXT, dict_version=dict_version)
    current_facts = facts_rule.extract_facts(
        RECORD_ID_C, STAGE_CURRENT_TEXT, dict_version=dict_version)
    return (
        _report(RECORD_ID_H, STAGE_HISTORY_TEXT, history_facts),
        _report(RECORD_ID_C, STAGE_CURRENT_TEXT, current_facts),
    )


def _stage_pair_result(time_basis: str):
    history_report, current_report = _stage_pair_reports(
        facts_rule.RULE_DICT_VERSION_V2)
    alignment = pair_alignment.build_aligned(
        history_report, current_report, STAGE_HISTORY_TEXT, STAGE_CURRENT_TEXT,
        dictionary_version="rule_dict_v2", alignment_version="alignment_v1")
    assert alignment.aligned_facts, "同主体同谓词两侧应有对齐对"
    p15_report = p15_integration.extract_p15_results(
        history_report, current_report, STAGE_HISTORY_TEXT, STAGE_CURRENT_TEXT,
        alignment, time_basis=time_basis)
    pair = pair_compare.compare_pair(
        _ctx(RECORD_ID_H, "item-h", STAGE_HISTORY_TEXT, 1),
        _ctx(RECORD_ID_C, "item-c", STAGE_CURRENT_TEXT, 2),
        history_artifact=history_report, current_artifact=current_report,
        alignment=alignment, p15_results=p15_report.p15_results)
    return p15_report, pair


@pytest.mark.parametrize("time_basis", ("TIME_SAME_ABSOLUTE", "TIME_SAME_STAGE"))
def test_w1fa2_time_conflict_basis_follows_actual_time_basis(time_basis):
    """W1Fa-2 主红测：非默认 time_basis 下时间 conflict 入列 VerifiedConflict
    的 basis 实传实际基底（修复前恒洗 TIME_SAME_RELATIVE，标签失真）。"""
    p15_report, pair = _stage_pair_result(time_basis)
    # 集成层契约钉：time_pairs 冲突条目携带实际基底
    conflict_tp = [tp for tp in p15_report.p15_results.time_pairs
                   if tp.outcome == "conflict"]
    assert conflict_tp, "stage 冲突应真实进入 time_pairs"
    assert all(tp.basis == time_basis for tp in conflict_tp)
    # 判定壁钉：VerifiedConflict.basis 实传（非硬编码默认）
    assert pair.outcome == "conflict"
    assert pair.code == "VERIFIED_CONFLICT"
    time_conflicts = [c for c in pair.verified_conflicts
                      if c.field_path == "time.aligned"]
    assert time_conflicts
    assert all(c.basis == time_basis for c in time_conflicts)
    assert time_basis in pair.detail


def test_w1fa2_default_time_basis_label_unchanged_guard():
    """W1Fa-2 守卫（前后均绿）：默认 time_basis 下标签恒 TIME_SAME_RELATIVE——
    现役默认路径零漂移（三腿翻转中性在单元级的直接钉）。
    钉对级 VerifiedConflict 标签（修复前恒值即 TIME_SAME_RELATIVE，
    修复后默认 time_basis 实传同值）——故本守卫前后均绿。"""
    _p15_report, pair = _stage_pair_result("TIME_SAME_RELATIVE")
    assert pair.outcome == "conflict"
    time_conflicts = [c for c in pair.verified_conflicts
                      if c.field_path == "time.aligned"]
    assert time_conflicts
    assert all(c.basis == "TIME_SAME_RELATIVE" for c in time_conflicts)


def _real_evidence_stage_free_alignment():
    """真实对齐证据夹具（镜像窗口Z1 R2-H3 守卫）：时间槽 missing、无 stage。"""
    history_text = "甲公司完成回购。"
    fact_h = [{
        "fact_id": "f1",
        "evidence": [{"record_id": RECORD_ID_H, "field": "facts.f1",
                      "quote": history_text, "start": 0, "end": len(history_text)}],
        "fact_type": {"status": "present", "raw_value": "回购", "evidence": []},
        "subject": {"status": "present", "raw_value": "甲公司",
                    "evidence": [{"record_id": RECORD_ID_H,
                                  "field": "facts.f1.subject",
                                  "quote": "甲公司", "start": 0, "end": 3}]},
        "event_state": {
            "predicate": {"status": "present", "raw_value": "回购", "evidence": []},
            "polarity": {"status": "present", "raw_value": "回购", "evidence": []},
            "modality": {"status": "missing", "raw_value": None, "evidence": []},
            "attribution": {"status": "missing", "raw_value": None, "evidence": []}},
        "time": {"expression": {"status": "missing", "raw_value": None, "evidence": []},
                 "stage": {"status": "missing", "raw_value": None, "evidence": []},
                 "anchor": {"status": "missing", "raw_value": None, "evidence": []}},
        "key_object": {"status": "missing", "raw_value": None, "evidence": []},
        "numerics": []}]
    fact_c = copy.deepcopy(fact_h)
    for entry in fact_c[0]["evidence"]:
        entry["record_id"] = RECORD_ID_C
    for entry in fact_c[0]["subject"]["evidence"]:
        entry["record_id"] = RECORD_ID_C
    history_report = _report(RECORD_ID_H, history_text, fact_h)
    current_report = _report(RECORD_ID_C, history_text, fact_c)
    alignment = pair_alignment.build_aligned(
        history_report, current_report, history_text, history_text,
        dictionary_version="dict_v1", alignment_version="alignment_v1")
    assert alignment.aligned_facts
    return history_text, history_report, current_report, alignment


def test_w1fa2_tuple_time_pair_without_basis_falls_back_default_guard():
    """W1Fa-2 守卫（前后均绿）：外部旧式 (outcome, code[, detail]) 元组不携
    基底 → 入列 VerifiedConflict 回退默认 TIME_SAME_RELATIVE（现役契约保留，
    修复前唯一行为，零漂移）。"""
    history_text, history_report, current_report, alignment = (
        _real_evidence_stage_free_alignment())
    p15 = P15PairResults(
        verified_conflicts=(),
        time_pairs=(("conflict", "VERIFIED_CONFLICT"),),
        equivalence_ready=False, text_proof=None,
        uncovered_independent_relation=False, issues=(),
    )
    pair = pair_compare.compare_pair(
        _ctx(RECORD_ID_H, "item-h", history_text, 1),
        _ctx(RECORD_ID_C, "item-c", history_text, 2),
        history_artifact=history_report, current_artifact=current_report,
        alignment=alignment, p15_results=p15)
    assert pair.outcome == "conflict"
    time_conflicts = [c for c in pair.verified_conflicts
                      if c.field_path == "time.aligned"]
    assert time_conflicts
    assert all(c.basis == "TIME_SAME_RELATIVE" for c in time_conflicts)


def test_w1fa2_out_of_whitelist_basis_rejected_fail_closed():
    """W1Fa-2 同族红测（fail-closed）：TimePairComparison 携白名单外 basis →
    ComparePairError（与 CONFLICT_CODES/UNRESOLVED_CODES 消费侧闸同一纪律；
    修复前无 basis 字段，白名单外基底可静默洗标）。"""
    history_text, history_report, current_report, alignment = (
        _real_evidence_stage_free_alignment())
    p15 = P15PairResults(
        verified_conflicts=(),
        time_pairs=(TimePairComparison(
            "conflict", "VERIFIED_CONFLICT", "已验证时间冲突",
            basis="TIME_SAME_FUTURE"),),
        equivalence_ready=False, text_proof=None,
        uncovered_independent_relation=False, issues=(),
    )
    with pytest.raises(ComparePairError):
        pair_compare.compare_pair(
            _ctx(RECORD_ID_H, "item-h", history_text, 1),
            _ctx(RECORD_ID_C, "item-c", history_text, 2),
            history_artifact=history_report, current_artifact=current_report,
            alignment=alignment, p15_results=p15)


def test_w1fa2_from_value_accepts_optional_basis_quadruple():
    """W1Fa-2 契约钉：from_value 接受 (outcome, code, detail, basis) 四元组
    （外部元组生产者实传基底的完整通道）；旧二/三元组形态保留。"""
    tp = TimePairComparison.from_value(
        ("conflict", "VERIFIED_CONFLICT", "已验证时间冲突", "TIME_SAME_ABSOLUTE"))
    assert tp.outcome == "conflict"
    assert tp.code == "VERIFIED_CONFLICT"
    assert tp.basis == "TIME_SAME_ABSOLUTE"
    assert TimePairComparison.from_value(("conflict", "VERIFIED_CONFLICT")).basis is None
    assert TimePairComparison.from_value(
        ("unresolved", "TIME_RELATION_UNCERTAIN", "时间关系未确定")).basis is None
