"""窗口Z1 判定链规格还原红绿钉（外部三审 R2-H2/R2-H3/R2-H4 + F-K1 裁定实施）。

四件（主窗口处方，逐项红测先行）：
- R2-H2：p15_integration._time_spec_from_slot 硬编码 stage=None 失实——
  facts/rule.py _time_slots（L798-806）实际产出 time.stage 盘态词槽
  （词表 L179-181 开盘/收盘/盘中/早盘/尾盘/盘前/盘后/午间），09 §8.3
  "对应时间的阶段不同必为不同时间"规格被吞。本窗只做转发（rule.py 禁碰；
  同比/环比/初值/终值词表扩充属主窗口后续项）。转发后 value_time 现役
  _STAGES/_STAGE_CONFLICTS（value_time.py L73-81）与 compare_time L556
  冲突分派自然执法。
- R2-H3：pair_compare.py 时间 conflict 降级分支（L288-292）把 VERIFIED_CONFLICT
  装入 issues → _primary_issue 白名单闸抛 ComparePairError（自毁）——
  修成真降级未决（TIME_RELATION_UNCERTAIN，09 §12.1 边界码，不崩）。
- R2-H4：decide/audit.build_audit_record 默认 basis="FACT_EQUIVALENT" 洗值——
  build_audit_batch 不传 basis → 任意对恒洗成 FACT_EQUIVALENT。修成缺省
  按 pair.code 实传（conflict→VERIFIED_CONFLICT、unresolved→各自码、
  equivalent→证书码），payload_hash 按真 basis 固化；显式 basis 覆盖保留。
- F-K1（主窗口裁定实施）：pair_alignment._verify_evidence_against_text 增
  "引文↔槽位 raw_value 一致性"校验——evidence 条目的 field 声称锚定本槽
  （以槽路径末段结尾）时，quote 必须含槽 raw_value（与 value_time
  normalize_* 现役 raw∈quote 纪律同一口径）；不一致即 PairAlignmentError
  （fail-closed）。不声称本槽的条目（恒等投影 field="text"/封板模板
  field="x"）维持既有切片校验——全套件探针实证：222 处 quote≠raw 全部
  field="x"（log/temp/winz1-fk1-probe.json），canonical 字段零不一致，
  本作用域对既有套件零翻转。
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping

import pytest

from news_flash_dedup.compare import pair_alignment, pair_compare, p15_integration
from news_flash_dedup.compare.pair_alignment import (
    AlignedPair,
    FactPointer,
    PairAlignmentError,
    PairAlignmentOutcome,
)
from news_flash_dedup.compare.pair_compare import (
    ComparePairError,
    PairResult,
    P15PairResults,
)
from news_flash_dedup.compare.value_time import EvidenceRef
from news_flash_dedup.decide import audit as audit_module
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


def _pair_result(*, outcome: str, code: str) -> PairResult:
    return PairResult(
        pair_id="aaa|bbb",
        history_record_id="a" * 64, current_record_id="b" * 64,
        history_item_id="item-A", current_item_id="item-B",
        history_arrival_seq=1, current_arrival_seq=2,
        history_raw_hash="h-hash", current_raw_hash="c-hash",
        pipeline_version=PIPELINE_VERSION,
        outcome=outcome, code=code, detail="stub detail",
        aligned_facts=(), verified_conflicts=(),
        unresolved_fields=(), used_evidence=(), budget_at=0,
    )


# ============================== R2-H2 stage 槽转发 ==============================

# 同主体同数值异阶段（开盘100 vs 收盘100）：09 §8.3 规格对——
# 修复前 stage 被吞：v1 腿 FACT_EQUIVALENT 误签（活 fp 路径实证），
# v2 腿 leftover 阻等价落 unresolved；两形态均非 VERIFIED_CONFLICT（红）。
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


@pytest.mark.parametrize("dict_version", (
    facts_rule.RULE_DICT_VERSION, facts_rule.RULE_DICT_VERSION_V2))
def test_r2h2_same_subject_same_value_different_stage_is_verified_conflict(
        dict_version):
    """R2-H2 主红测：开盘100 vs 收盘100 → VERIFIED_CONFLICT（09 §8.3 规格还原）。

    机制钉：time.stage 槽经 p15 转发 → normalize_time 归一 open/close →
    compare_time _STAGE_CONFLICTS 分派 conflict → time_pairs 携
    VERIFIED_CONFLICT → compare_pair 判 conflict。
    """
    history_report, current_report = _stage_pair_reports(dict_version)
    alignment = pair_alignment.build_aligned(
        history_report, current_report, STAGE_HISTORY_TEXT, STAGE_CURRENT_TEXT,
        dictionary_version="rule_dict_v2", alignment_version="alignment_v1")
    assert alignment.aligned_facts, "同主体同谓词两侧应有对齐对"
    p15_report = p15_integration.extract_p15_results(
        history_report, current_report, STAGE_HISTORY_TEXT, STAGE_CURRENT_TEXT,
        alignment)
    # 机制钉：stage 冲突真实进入 time_pairs（不是绕过 P15 的空泛 outcome 巧合）
    assert any(tp.outcome == "conflict" and tp.code == "VERIFIED_CONFLICT"
               for tp in p15_report.p15_results.time_pairs), (
        f"stage 冲突未入 time_pairs：{p15_report.p15_results.time_pairs!r}")
    pair = pair_compare.compare_pair(
        _ctx(RECORD_ID_H, "item-h", STAGE_HISTORY_TEXT, 1),
        _ctx(RECORD_ID_C, "item-c", STAGE_CURRENT_TEXT, 2),
        history_artifact=history_report, current_artifact=current_report,
        alignment=alignment, p15_results=p15_report.p15_results)
    assert pair.outcome == "conflict"
    assert pair.code == "VERIFIED_CONFLICT"


def test_r2h2_time_spec_forwards_stage_and_stage_evidence():
    """R2-H2 转发契约钉：_time_spec_from_slot 携带 stage + 校验后 stage_evidence。"""
    history_report, _ = _stage_pair_reports(facts_rule.RULE_DICT_VERSION_V2)
    fact = (history_report.artifact or {})["facts"][0]
    time_map = fact.get("time") or {}
    spec = p15_integration._time_spec_from_slot(
        time_map.get("expression") or {}, STAGE_HISTORY_TEXT, RECORD_ID_H,
        stage_slot=time_map.get("stage") or {})
    assert spec.stage == "开盘"
    assert isinstance(spec.stage_evidence, EvidenceRef)
    assert spec.stage_evidence.field.endswith(".time.stage")
    assert spec.stage_evidence.quote == "开盘"
    start = STAGE_HISTORY_TEXT.index("开盘")
    assert (spec.stage_evidence.start, spec.stage_evidence.end) == (start, start + 2)


def test_r2h2_stage_evidence_forged_fails_closed_to_unresolved():
    """R2-H2 fail-closed 双层钉：
    ①span 错位伪证据 → 对齐门既有切片校验 PairAlignmentError（门级 fail-closed）；
    ②present stage + 空 evidence（主张无锚，门可过）→ p15 转发后
    normalize_time 判 EVIDENCE_INVALID → 时间对 unresolved——不得静默丢
    stage 主张而洗成 compatible/conflict。"""
    history_report, current_report = _stage_pair_reports(facts_rule.RULE_DICT_VERSION_V2)
    # ① span 错位伪证据：对齐门直接拒（pre-existing gate，早于 p15 执法）
    forged_facts = json.loads(json.dumps(history_report.artifact["facts"]))
    for fact in forged_facts:
        stage = (fact.get("time") or {}).get("stage") or {}
        if stage.get("status") == "present":
            stage["evidence"] = [{**stage["evidence"][0], "start": 0, "end": 1}]
    forged_report = _report(RECORD_ID_H, STAGE_HISTORY_TEXT, forged_facts)
    with pytest.raises(PairAlignmentError):
        pair_alignment.build_aligned(
            forged_report, current_report, STAGE_HISTORY_TEXT, STAGE_CURRENT_TEXT,
            dictionary_version="rule_dict_v2", alignment_version="alignment_v1")
    # ② present stage + 空 evidence：门可过 → p15 层 EVIDENCE_INVALID 未决
    unanchored_facts = json.loads(json.dumps(history_report.artifact["facts"]))
    for fact in unanchored_facts:
        stage = (fact.get("time") or {}).get("stage") or {}
        if stage.get("status") == "present":
            stage["evidence"] = []
    unanchored_report = _report(RECORD_ID_H, STAGE_HISTORY_TEXT, unanchored_facts)
    alignment = pair_alignment.build_aligned(
        unanchored_report, current_report, STAGE_HISTORY_TEXT, STAGE_CURRENT_TEXT,
        dictionary_version="rule_dict_v2", alignment_version="alignment_v1")
    p15_report = p15_integration.extract_p15_results(
        unanchored_report, current_report, STAGE_HISTORY_TEXT, STAGE_CURRENT_TEXT,
        alignment)
    assert not any(tp.outcome == "conflict" for tp in p15_report.p15_results.time_pairs)
    assert any(tp.outcome == "unresolved" and tp.code == "EVIDENCE_INVALID"
               for tp in p15_report.p15_results.time_pairs), (
        f"无锚 stage 主张未落 EVIDENCE_INVALID 未决："
        f"{p15_report.p15_results.time_pairs!r}")


def test_r2h2_same_stage_guard_no_conflict():
    """R2-H2 守卫（前后均绿）：双侧同阶段（开盘/开盘）不得判 conflict。"""
    same_text = "9月10日开盘，甲公司公告回购100股。"
    history_facts = facts_rule.extract_facts(
        RECORD_ID_H, same_text, dict_version=facts_rule.RULE_DICT_VERSION_V2)
    current_facts = facts_rule.extract_facts(
        RECORD_ID_C, same_text, dict_version=facts_rule.RULE_DICT_VERSION_V2)
    history_report = _report(RECORD_ID_H, same_text, history_facts)
    current_report = _report(RECORD_ID_C, same_text, current_facts)
    alignment = pair_alignment.build_aligned(
        history_report, current_report, same_text, same_text,
        dictionary_version="rule_dict_v2", alignment_version="alignment_v1")
    p15_report = p15_integration.extract_p15_results(
        history_report, current_report, same_text, same_text, alignment)
    assert not any(tp.outcome == "conflict" for tp in p15_report.p15_results.time_pairs)


# ============================== R2-H3 降级分支自毁 ==============================

def _empty_alignment() -> PairAlignmentOutcome:
    return PairAlignmentOutcome(
        aligned_facts=(), unresolved_fields=("history:f1", "current:f1"),
        code="RULE_UNCOVERED", provenance={"dictionary_version": "dict_v1",
                                           "alignment_version": "alignment_v1"})


def test_r2h3_time_conflict_without_aligned_facts_downgrades_not_crashes():
    """R2-H3 主红测：直接构造 P15PairResults（时间 conflict + 零对齐对）——
    修复前 VERIFIED_CONFLICT 装入 issues 触白名单闸 ComparePairError（自毁）；
    修复后真降级未决（outcome=unresolved，码 TIME_RELATION_UNCERTAIN），不崩。"""
    history_text = "甲公司完成回购。"
    history_report = _report(RECORD_ID_H, history_text, [])
    current_report = _report(RECORD_ID_C, history_text, [])
    p15 = P15PairResults(
        verified_conflicts=(),
        time_pairs=(("conflict", "VERIFIED_CONFLICT", "已验证时间阶段冲突"),),
        equivalence_ready=False, text_proof=None,
        uncovered_independent_relation=False, issues=(),
    )
    pair = pair_compare.compare_pair(
        _ctx(RECORD_ID_H, "item-h", history_text, 1),
        _ctx(RECORD_ID_C, "item-c", history_text, 2),
        history_artifact=history_report, current_artifact=current_report,
        alignment=_empty_alignment(), p15_results=p15)
    assert pair.outcome == "unresolved"
    assert pair.code == "TIME_RELATION_UNCERTAIN"
    assert pair.code in pair_compare.UNRESOLVED_CODES
    assert pair.unresolved_fields


def test_r2h3_time_conflict_with_null_pair_evidence_downgrades_not_crashes():
    """R2-H3 同族钉：对齐对在案但 evidence 为 None（合成态）同样真降级不崩。"""
    history_text = "甲公司完成回购。"
    history_report = _report(RECORD_ID_H, history_text, [])
    current_report = _report(RECORD_ID_C, history_text, [])
    null_pointer = FactPointer(
        record_id=RECORD_ID_H, fact_id="f1", slot_path="facts.f1",
        text=history_text, evidence=None)  # type: ignore[arg-type]
    null_pointer_c = FactPointer(
        record_id=RECORD_ID_C, fact_id="f1", slot_path="facts.f1",
        text=history_text, evidence=None)  # type: ignore[arg-type]
    alignment = PairAlignmentOutcome(
        aligned_facts=(AlignedPair(null_pointer, null_pointer_c,
                                   basis="FACT_EQUIVALENT"),),
        unresolved_fields=(), code="FACT_EQUIVALENT",
        provenance={"dictionary_version": "dict_v1",
                    "alignment_version": "alignment_v1"})
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
    assert pair.outcome == "unresolved"
    assert pair.code == "TIME_RELATION_UNCERTAIN"


def test_r2h3_time_conflict_with_real_evidence_still_conflict_guard():
    """R2-H3 守卫（前后均绿）：时间 conflict 且有真实对齐证据 → 仍判 conflict
    （现役主路径零漂移）。"""
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
    import copy
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
    assert pair.code == "VERIFIED_CONFLICT"


# ============================== R2-H4 审计 basis 洗值 ==============================

def test_r2h4_conflict_pair_audit_basis_not_washed_to_fact_equivalent():
    """R2-H4 主红测：conflict 对 → 审计 basis 实传 VERIFIED_CONFLICT，
    不得洗成默认 FACT_EQUIVALENT。"""
    record = audit_module.build_audit_record(
        _pair_result(outcome="conflict", code="VERIFIED_CONFLICT"))
    assert record.basis == "VERIFIED_CONFLICT"
    assert record.basis != "FACT_EQUIVALENT"


def test_r2h4_unresolved_pair_audit_basis_follows_pair_code():
    """R2-H4 同族钉：unresolved 对 → basis 实传该对实际码。"""
    record = audit_module.build_audit_record(
        _pair_result(outcome="unresolved", code="TIME_RELATION_UNCERTAIN"))
    assert record.basis == "TIME_RELATION_UNCERTAIN"


def test_r2h4_equivalent_pair_audit_basis_follows_certificate():
    """R2-H4 同族钉：equivalent 对 → basis 实传证书码（EXACT_TEXT_MATCH 等）。"""
    record = audit_module.build_audit_record(
        _pair_result(outcome="equivalent", code="EXACT_TEXT_MATCH"))
    assert record.basis == "EXACT_TEXT_MATCH"


def test_r2h4_audit_batch_mixed_outcomes_not_uniform_washed():
    """R2-H4 批次钉：混合对批次 basis 各按其实——修复前恒一排 FACT_EQUIVALENT。"""
    batch = audit_module.build_audit_batch(
        [_pair_result(outcome="conflict", code="VERIFIED_CONFLICT"),
         _pair_result(outcome="unresolved", code="FACT_INCOMPLETE"),
         _pair_result(outcome="equivalent", code="FACT_EQUIVALENT")],
        index_prefix="winz1-test-audits-v1", audit_complete=True)
    assert [r.basis for r in batch.records] == [
        "VERIFIED_CONFLICT", "FACT_INCOMPLETE", "FACT_EQUIVALENT"]


def test_r2h4_payload_hash_sealed_by_real_basis():
    """R2-H4 固化钉：payload_hash 按真 basis 计算——与手算真 basis 哈希一致，
    且与"洗成 FACT_EQUIVALENT"的伪哈希不同。"""
    pair = _pair_result(outcome="conflict", code="VERIFIED_CONFLICT")
    record = audit_module.build_audit_record(pair)
    canonical = {
        "history_record_id": record.history_record_id,
        "current_record_id": record.current_record_id,
        "history_raw_hash": record.history_raw_hash,
        "current_raw_hash": record.current_raw_hash,
        "basis": "VERIFIED_CONFLICT",
        "field_path": record.field_path,
        "detail": record.detail,
        "history_evidence": record.history_evidence,
        "current_evidence": record.current_evidence,
        "pipeline_version": record.pipeline_version,
    }
    expected = hashlib.sha256(
        json.dumps(canonical, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    assert record.payload_hash == expected
    washed = dict(canonical, basis="FACT_EQUIVALENT")
    washed_hash = hashlib.sha256(
        json.dumps(washed, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    assert record.payload_hash != washed_hash


def test_r2h4_explicit_basis_override_still_honored_guard():
    """R2-H4 守卫（前后均绿）：显式 basis 覆盖保留（现役测试契约不破）。"""
    record = audit_module.build_audit_record(
        _pair_result(outcome="equivalent", code="FACT_EQUIVALENT"),
        field_path="facts.f1", basis="FACT_EQUIVALENT",
        detail="subject/predicate aligned")
    assert record.basis == "FACT_EQUIVALENT"
    assert record.field_path == "facts.f1"


# ============================== F-K1 引文↔槽值一致性 ==============================

FK1_TEXT = "甲公司宣布分红。"


def _fk1_fact(*, subject_quote: str, subject_span: tuple[int, int],
              subject_field: str = "facts.f1.subject",
              record_id: str = RECORD_ID_H) -> list[dict]:
    return [{
        "fact_id": "f1",
        "evidence": [{"record_id": record_id, "field": "facts.f1",
                      "quote": FK1_TEXT, "start": 0, "end": len(FK1_TEXT)}],
        "fact_type": {"status": "present", "raw_value": "宣布", "evidence": []},
        "subject": {"status": "present", "raw_value": "甲公司",
                    "evidence": [{"record_id": record_id,
                                  "field": subject_field,
                                  "quote": subject_quote,
                                  "start": subject_span[0],
                                  "end": subject_span[1]}]},
        "event_state": {
            "predicate": {"status": "present", "raw_value": "宣布", "evidence": []},
            "polarity": {"status": "present", "raw_value": "宣布", "evidence": []},
            "modality": {"status": "missing", "raw_value": None, "evidence": []},
            "attribution": {"status": "missing", "raw_value": None, "evidence": []}},
        "time": {"expression": {"status": "missing", "raw_value": None, "evidence": []},
                 "stage": {"status": "missing", "raw_value": None, "evidence": []},
                 "anchor": {"status": "missing", "raw_value": None, "evidence": []}},
        "key_object": {"status": "missing", "raw_value": None, "evidence": []},
        "numerics": []}]


def test_fk1_h06_forged_slot_anchor_rejected():
    """F-K1 主红测（H-06 型）：subject 槽 raw="甲公司" 配 quote=text[:2]="甲公"
    （切片自洽但与槽值不符）→ 对齐门 fail-closed PairAlignmentError。"""
    facts = _fk1_fact(subject_quote=FK1_TEXT[:2], subject_span=(0, 2))
    report = _report(RECORD_ID_H, FK1_TEXT, facts)
    other = _report(RECORD_ID_C, FK1_TEXT,
                    _fk1_fact(subject_quote="甲公司", subject_span=(0, 3),
                              record_id=RECORD_ID_C))
    with pytest.raises(PairAlignmentError):
        pair_alignment.build_aligned(
            report, other, FK1_TEXT, FK1_TEXT,
            dictionary_version="dict_v1", alignment_version="alignment_v1")


def test_fk1_disjoint_forged_quote_on_claimed_field_rejected():
    """F-K1 同族钉：声称本槽字段（.subject）的互不相含伪引文
    （quote="宣布" 锚 subject 槽）同样拒。"""
    facts = _fk1_fact(subject_quote="宣布", subject_span=(3, 5))
    report = _report(RECORD_ID_H, FK1_TEXT, facts)
    other = _report(RECORD_ID_C, FK1_TEXT,
                    _fk1_fact(subject_quote="甲公司", subject_span=(0, 3),
                              record_id=RECORD_ID_C))
    with pytest.raises(PairAlignmentError):
        pair_alignment.build_aligned(
            report, other, FK1_TEXT, FK1_TEXT,
            dictionary_version="dict_v1", alignment_version="alignment_v1")


def test_fk1_nonclaimed_field_entries_unaffected_guard():
    """F-K1 作用域守卫（前后均绿）：field 不声称本槽的条目（封板模板
    field="x" 形态：quote=他槽文本）维持既有切片校验，不追加槽值校验。"""
    facts = _fk1_fact(subject_quote="甲公司", subject_span=(0, 3),
                      subject_field="x")
    # 模板形态：predicate 槽 quote 用 subject 文本、field="x"
    facts[0]["event_state"]["predicate"] = {
        "status": "present", "raw_value": "宣布",
        "evidence": [{"record_id": RECORD_ID_H, "field": "x",
                      "quote": "甲公司", "start": 0, "end": 3}]}
    report = _report(RECORD_ID_H, FK1_TEXT, facts)
    other_facts = _fk1_fact(subject_quote="甲公司", subject_span=(0, 3),
                            subject_field="x", record_id=RECORD_ID_C)
    other_facts[0]["event_state"]["predicate"] = {
        "status": "present", "raw_value": "宣布",
        "evidence": [{"record_id": RECORD_ID_C, "field": "x",
                      "quote": "甲公司", "start": 0, "end": 3}]}
    other = _report(RECORD_ID_C, FK1_TEXT, other_facts)
    outcome = pair_alignment.build_aligned(
        report, other, FK1_TEXT, FK1_TEXT,
        dictionary_version="dict_v1", alignment_version="alignment_v1")
    assert outcome.aligned_facts


def test_fk1_honest_rule_artifact_passes_guard():
    """F-K1 守卫（前后均绿）：rule.py 真实抽取工件（quote==raw 恒成立）
    过对齐门不受影响。"""
    text = "9月10日，甲公司公告回购100股。"
    facts = facts_rule.extract_facts(
        RECORD_ID_H, text, dict_version=facts_rule.RULE_DICT_VERSION_V2)
    report = _report(RECORD_ID_H, text, facts)
    other_facts = facts_rule.extract_facts(
        RECORD_ID_C, text, dict_version=facts_rule.RULE_DICT_VERSION_V2)
    other = _report(RECORD_ID_C, text, other_facts)
    outcome = pair_alignment.build_aligned(
        report, other, text, text,
        dictionary_version="rule_dict_v2", alignment_version="alignment_v1")
    assert outcome.aligned_facts
