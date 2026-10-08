# -*- coding: utf-8 -*-
"""E 批 H-04 红测：decide 入口 validated_complete 全量单轨接线（修法 A）。

设计稿=log\\temp\\e-batch-design.md §三。裁定锚=log\\G门呈裁包.md ②定裁
（live 前置硬阻塞+修法 A 优先）。探针基线=log\\temp\\
winE-h04-validation-probe.txt（S1-S11）/winE-h04-projection-probe.txt（P-A~P-E）。
"""
from __future__ import annotations

import copy

from news_flash_dedup.decide.service import _wrap_facts_as_report
from news_flash_dedup.facts import rule as facts_rule
from news_flash_dedup.recall.service import IdentityFactSupply

RID_A = "a" * 64
TEXT_PLAIN = "甲公司完成回购。"
TEXT_T090 = "甲公司今日回购12345678901234567890123456789万股。"


def _field_x_fact(record_id, text):
    """G 门②(i) 族：field='x' 伪证据夹具（test_p17_implementation._evidence_for 同形）。"""
    ev = [{"record_id": record_id, "field": "x", "quote": "甲",
           "start": 0, "end": 1}]
    missing = {"status": "missing", "raw_value": None, "evidence": []}
    return {"fact_id": "f1", "evidence": ev,
            "fact_type": {"status": "present", "raw_value": "回购", "evidence": ev},
            "subject": {"status": "present", "raw_value": "甲公司", "evidence": ev},
            "event_state": {
                "predicate": {"status": "present", "raw_value": "回购",
                              "evidence": ev},
                "polarity": {"status": "present", "raw_value": "回购",
                             "evidence": ev},
                "modality": missing, "attribution": missing},
            "time": {"expression": missing, "stage": missing,
                     "anchor": missing},
            "key_object": missing, "numerics": []}


def _complete_fact(record_id, text):
    """P-C 正向对照族：真完整单 fact（全文覆盖、极性盖模态、顺序 id）。"""
    span = lambda field: {"record_id": record_id, "field": field,
                          "quote": text, "start": 0, "end": len(text)}
    present = lambda raw, field: {"status": "present", "raw_value": raw,
                                  "evidence": [span(field)]}
    missing = {"status": "missing", "raw_value": None, "evidence": []}
    return {"fact_id": "f1", "evidence": [span("facts.f1")],
            "fact_type": present(text, "facts.f1.fact_type"),
            "subject": present(text, "facts.f1.subject"),
            "event_state": {
                "predicate": present(text, "facts.f1.event_state.predicate"),
                "polarity": present(text, "facts.f1.event_state.polarity"),
                "modality": missing, "attribution": missing},
            "time": {"expression": missing, "stage": missing,
                     "anchor": missing},
            "key_object": missing, "numerics": []}


# ---------- R 族：短路放行面封闭（现役红） ----------

def test_r1_minimal_dict_facts_no_longer_pass():
    """G 门②(i)：最小 dict facts 不得再置 True（探针 S7）。"""
    report = _wrap_facts_as_report(RID_A, TEXT_PLAIN, [{"fact_id": "f1"}])
    assert report.validated_complete is False


def test_r2_field_x_fixture_no_longer_pass():
    """G 门②(i)：field='x' 伪证据夹具不得再置 True（探针 S6）。"""
    report = _wrap_facts_as_report(RID_A, TEXT_PLAIN,
                                   [_field_x_fact(RID_A, TEXT_PLAIN)])
    assert report.validated_complete is False


def test_r3_identity_supply_no_longer_pass():
    """G 门②(iv)：identity 裸包装供给恒 False 真值化（探针 S1/S2）。"""
    facts = IdentityFactSupply()(RID_A, TEXT_PLAIN)
    report = _wrap_facts_as_report(RID_A, TEXT_PLAIN, facts)
    assert report.validated_complete is False


def test_r4_rule_supply_truthful_gap_not_schema_failure():
    """rule 真供给经投影：Schema 阶段零 issue，真缺口如实判 False
    （探针 P-A：T090 含量级词"万"未认领 → FACT_INCOMPLETE，非 EXTRACTION_FAILED）。"""
    facts = facts_rule.extract_facts(RID_A, TEXT_T090)
    report = _wrap_facts_as_report(RID_A, TEXT_T090, facts)
    assert report.validated_complete is False
    assert report.issues, "校验真值化后 issues 不得恒空"
    assert not any(i.code == "EXTRACTION_FAILED" for i in report.issues), (
        "投影未落地——内联束被误送 Schema（unknown keys）")
    assert any(i.code == "FACT_INCOMPLETE" for i in report.issues)


def test_r5_valid_fact_ids_truthful_for_invalid_fixture():
    """valid_fact_ids=校验器口径：伪证据 fact 拦截不入册（现役布尔口径含 f1）。"""
    report = _wrap_facts_as_report(RID_A, TEXT_PLAIN,
                                   [_field_x_fact(RID_A, TEXT_PLAIN)])
    assert report.valid_fact_ids == ()


def test_r6_numeric_inventory_populated():
    """numeric_inventory 由校验器实扫供给（现役恒 ()）。"""
    facts = facts_rule.extract_facts(RID_A, TEXT_T090)
    report = _wrap_facts_as_report(RID_A, TEXT_T090, facts)
    assert report.numeric_inventory, "T090 含 29 位数值，实扫清单不得为空"


def test_r7_unvalidated_markers_retired():
    """标注键退役（假命题不留存）；校验 provenance 三键在场。"""
    report = _wrap_facts_as_report(RID_A, TEXT_PLAIN, [_complete_fact(RID_A, TEXT_PLAIN)])
    assert "unvalidated" not in report.artifact
    assert "unvalidated_note" not in report.artifact
    provenance = report.artifact["validation"]
    assert provenance["validator"] == "validate_fact_artifact"
    assert provenance["schema_projection"] == "e1_numeric_slot_v1"
    assert isinstance(provenance["issue_count"], int)


def test_r8_downstream_bundle_preserved():
    """返回 artifact 的 facts 原样保留内联束（_spec_from_slot 消费面零破）。"""
    facts = facts_rule.extract_facts(RID_A, TEXT_T090)
    report = _wrap_facts_as_report(RID_A, TEXT_T090, facts)
    value_slot = report.artifact["facts"][0]["numerics"][0]["value"]
    assert "approximate" in value_slot and "unit" in value_slot, (
        "投影误伤原工件——下游数值消费合同被剥")


# ---------- R9 绿守卫：真完整工件 True 仍可达（P-C 正向对照，前后均绿） ----------

def test_r9_genuinely_complete_artifact_still_true():
    """修法非"全灭"：真完整工件 validated=True 前后均绿（探针 P-C）。"""
    report = _wrap_facts_as_report(RID_A, TEXT_PLAIN,
                                   [_complete_fact(RID_A, TEXT_PLAIN)])
    assert report.validated_complete is True
    assert report.valid_fact_ids == ("f1",)


# ---------- R10-R12 状态与边界（S8/S9 same 钉） ----------

def test_r10_empty_facts_stays_false():
    report = _wrap_facts_as_report(RID_A, TEXT_PLAIN, [])
    assert report.validated_complete is False


def test_r11_partial_status_stays_false():
    """状态合取保留：partial+洁净工件不得洗 True（:68 语义不放大）。"""
    report = _wrap_facts_as_report(RID_A, TEXT_PLAIN,
                                   [_complete_fact(RID_A, TEXT_PLAIN)],
                                   extraction_status="partial")
    assert report.validated_complete is False


def test_r12_wrap_signature_and_seven_keys_unchanged():
    """键面守卫（test_winw2fa2_wrap_marker.py:31-41 同族）：七 Schema 键逐字节。"""
    report = _wrap_facts_as_report(RID_A, TEXT_PLAIN,
                                   [_complete_fact(RID_A, TEXT_PLAIN)],
                                   extraction_status="complete")
    artifact = report.artifact
    assert artifact["schema_version"] == "1.0"
    assert artifact["record_id"] == RID_A
    assert artifact["offset_unit"] == "unicode_code_point"
    assert artifact["extraction_status"] == "complete"
    assert artifact["unparsed_spans"] == [] and artifact["uncertainties"] == []
    assert artifact["facts"], "facts 原样透传"


# ---------- P 族：投影规格钉（同文件私有 _project_artifact_for_validation） ----------

def test_p1_projection_idempotent_and_input_untouched():
    from news_flash_dedup.decide.service import _project_artifact_for_validation
    facts = facts_rule.extract_facts(RID_A, TEXT_T090)
    artifact = {"schema_version": "1.0", "record_id": RID_A,
                "offset_unit": "unicode_code_point",
                "extraction_status": "complete", "facts": facts,
                "unparsed_spans": [], "uncertainties": []}
    frozen = copy.deepcopy(artifact)
    projected = _project_artifact_for_validation(artifact)
    assert artifact == frozen, "投影必须 deepcopy——输入对象零改动"
    assert _project_artifact_for_validation(projected) == projected


def test_p2_projection_strips_bundle_only_in_projection():
    from news_flash_dedup.decide.service import _project_artifact_for_validation
    facts = facts_rule.extract_facts(RID_A, TEXT_T090)
    artifact = {"schema_version": "1.0", "record_id": RID_A,
                "offset_unit": "unicode_code_point",
                "extraction_status": "complete", "facts": facts,
                "unparsed_spans": [], "uncertainties": []}
    projected = _project_artifact_for_validation(artifact)
    value_slot = projected["facts"][0]["numerics"][0]["value"]
    assert set(value_slot) <= {"status", "raw_value", "evidence", "value"}
    assert "approximate" not in value_slot


def test_p3_projection_strips_verified_missing_and_backfills_decimal_value():
    from news_flash_dedup.decide.service import _project_artifact_for_validation
    fact = _complete_fact(RID_A, TEXT_PLAIN)
    fact["time"]["expression"] = {"status": "missing", "raw_value": None,
                                  "evidence": [], "verified_missing": True}
    fact["numerics"] = [{
        "numeric_id": "n1",
        "evidence": [{"record_id": RID_A, "field": "facts.f1.numerics.n1",
                      "quote": "回", "start": 4, "end": 5}],
        "metric": {"status": "missing", "raw_value": None, "evidence": []},
        "value": {"status": "missing", "raw_value": None, "evidence": []},
        "range_end": {"status": "missing", "raw_value": None, "evidence": []},
        "magnitude": {"status": "missing", "raw_value": None, "evidence": []},
        "unit": {"status": "missing", "raw_value": None, "evidence": []},
        "currency": {"status": "missing", "raw_value": None, "evidence": []},
        "role": {"status": "missing", "raw_value": None, "evidence": []},
        "comparator": {"status": "missing", "raw_value": None, "evidence": []},
        "direction": {"status": "missing", "raw_value": None, "evidence": []},
        "time": {"status": "missing", "raw_value": None, "evidence": []},
    }]
    artifact = {"schema_version": "1.0", "record_id": RID_A,
                "offset_unit": "unicode_code_point",
                "extraction_status": "complete", "facts": [fact],
                "unparsed_spans": [], "uncertainties": []}
    projected = _project_artifact_for_validation(artifact)
    assert "verified_missing" not in projected["facts"][0]["time"]["expression"]
    numeric = projected["facts"][0]["numerics"][0]
    assert numeric["value"]["value"] is None                       # decimal 补键
    assert numeric["range_end"]["value"] is None
