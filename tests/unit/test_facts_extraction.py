from copy import deepcopy

import pytest

from news_flash_dedup.facts import (
    FACT_SCHEMA,
    DeterministicFactModel,
    FactExtractionService,
    scan_numeric_inventory,
    validate_fact_artifact,
)


RECORD_ID = "a" * 64


def evidence(text, quote, field, *, occurrence=0):
    start = -1
    for _ in range(occurrence + 1):
        start = text.index(quote, start + 1)
    return {"record_id": RECORD_ID, "field": field, "quote": quote, "start": start, "end": start + len(quote)}


def missing(*, decimal=False):
    result = {"status": "missing", "raw_value": None, "evidence": []}
    if decimal:
        result["value"] = None
    return result


def present(text, quote, field, *, value=None, occurrence=0):
    result = {"status": "present", "raw_value": quote, "evidence": [evidence(text, quote, field, occurrence=occurrence)]}
    if value is not None:
        result["value"] = value
    return result


def fact(text, subject="甲公司", predicate="回购", *, fact_id="f1", sentence=None, numeric=None):
    base = f"facts.{fact_id}"
    sentence = sentence or text
    result = {
        "fact_id": fact_id,
        "evidence": [evidence(text, sentence, base)],
        "fact_type": present(text, predicate, base + ".fact_type"),
        "subject": present(text, subject, base + ".subject"),
        "event_state": {
            "predicate": present(text, predicate, base + ".event_state.predicate"),
            "polarity": present(text, predicate, base + ".event_state.polarity"),
            "modality": missing(),
            "attribution": missing(),
        },
        "time": {"expression": missing(), "stage": missing(), "anchor": missing()},
        "key_object": missing(),
        "numerics": [],
    }
    if numeric is not None:
        value = numeric
        nbase = base + ".numerics.n1"
        result["numerics"].append(
            {
                "numeric_id": "n1",
                "evidence": [evidence(text, value, nbase)],
                "metric": missing(),
                "value": present(text, value, nbase + ".value", value=value),
                "range_end": missing(decimal=True),
                "magnitude": missing(),
                "unit": missing(),
                "currency": missing(),
                "role": missing(),
                "comparator": missing(),
                "direction": missing(),
                "time": missing(),
            }
        )
    return result


def artifact(text, facts):
    return {
        "schema_version": "1.0",
        "record_id": RECORD_ID,
        "offset_unit": "unicode_code_point",
        "extraction_status": "complete",
        "facts": facts,
        "unparsed_spans": [],
        "uncertainties": [],
    }


def issue_codes(report):
    return {issue.code for issue in report.issues}


def test_strict_schema_and_legal_missing_values_are_accepted():
    text = "甲公司完成回购。"
    report = validate_fact_artifact(text, RECORD_ID, artifact(text, [fact(text)]))

    assert FACT_SCHEMA["$schema"].endswith("2020-12/schema")
    assert report.validated_complete
    assert report.issues == ()
    assert report.valid_fact_ids == ("f1",)
    assert report.numeric_inventory == ()


@pytest.mark.parametrize("mutation", [
    lambda a: a.update(extra="model_decision"),
    lambda a: a["facts"][0]["subject"].update(raw_value=None),
    lambda a: a["facts"][0]["time"]["expression"].update(raw_value="今日"),
    lambda a: a["facts"][0].update(fact_id="f2"),
    lambda a: a["facts"][0]["event_state"].update(extra="future"),
])
def test_schema_rejects_unknown_fields_status_lies_and_local_id_gaps(mutation):
    text = "甲公司完成回购。"
    data = artifact(text, [fact(text)])
    mutation(data)
    report = validate_fact_artifact(text, RECORD_ID, data)
    assert not report.validated_complete
    assert "EXTRACTION_FAILED" in issue_codes(report)


def test_decimal_value_requires_string_and_evidence_must_match_original_code_points():
    text = "𠮷公司回购100股。"
    data = artifact(text, [fact(text, subject="𠮷公司", numeric="100")])
    assert validate_fact_artifact(text, RECORD_ID, data).validated_complete

    float_data = deepcopy(data)
    float_data["facts"][0]["numerics"][0]["value"]["value"] = 100.0
    assert "EXTRACTION_FAILED" in issue_codes(validate_fact_artifact(text, RECORD_ID, float_data))

    bad_offset = deepcopy(data)
    bad_offset["facts"][0]["subject"]["evidence"][0]["end"] += 1
    assert "EVIDENCE_INVALID" in issue_codes(validate_fact_artifact(text, RECORD_ID, bad_offset))

    bad_path = deepcopy(data)
    bad_path["facts"][0]["subject"]["evidence"][0]["field"] = "facts.f1.time.expression"
    assert "EVIDENCE_INVALID" in issue_codes(validate_fact_artifact(text, RECORD_ID, bad_path))


def test_full_numeric_inventory_detects_omission_instead_of_accepting_missing():
    text = "甲公司回购100股，累计101股。"
    data = artifact(text, [fact(text, numeric="100")])
    report = validate_fact_artifact(text, RECORD_ID, data)

    assert [(m.raw, m.start, m.assigned_as) for m in report.numeric_inventory] == [
        ("100", text.index("100"), "numeric"),
        ("101", text.index("101"), "unassigned"),
    ]
    assert not report.validated_complete
    assert "FACT_INCOMPLETE" in issue_codes(report)
    assert report.valid_fact_ids == ("f1",)


def test_broad_value_quote_cannot_hide_an_unextracted_second_number():
    text = "甲公司回购100/101股。"
    data = artifact(text, [fact(text, numeric="100")])
    value_evidence = data["facts"][0]["numerics"][0]["value"]["evidence"][0]
    value_evidence.update(evidence(text, "100/101", value_evidence["field"]))
    report = validate_fact_artifact(text, RECORD_ID, data)
    assert report.numeric_inventory[-1].raw == "101"
    assert report.numeric_inventory[-1].assigned_as == "unassigned"
    assert "FACT_INCOMPLETE" in issue_codes(report)


def test_date_code_and_chinese_quantity_are_inventory_items_with_separate_assignments():
    text = "甲公司2026年9月22日回购代码600000共一百万股。"
    mentions = scan_numeric_inventory(text)
    assert [m.raw for m in mentions] == ["2026", "9", "22", "600000", "一百万"]
    assert [m.category_hint for m in mentions] == ["date", "date", "date", "identifier", "quantity"]

    dashed = scan_numeric_inventory("甲公司2026-09-22回购。")
    assert [m.raw for m in dashed] == ["2026", "09", "22"]
    assert [m.category_hint for m in dashed] == ["date", "date", "date"]


def test_subject_or_event_missing_is_not_rescued_by_model_complete_status():
    text = "上涨1%"
    report = validate_fact_artifact(text, RECORD_ID, artifact(text, []))
    assert not report.validated_complete
    assert "SUBJECT_UNRESOLVED" in issue_codes(report)
    assert report.numeric_inventory[-1].assigned_as == "unassigned"


def test_model_cannot_add_a_predicate_absent_from_this_original_text():
    text = "甲公司回购。"
    data = artifact(text, [fact(text)])
    data["facts"][0]["event_state"]["predicate"]["raw_value"] = "关闭"
    report = validate_fact_artifact(text, RECORD_ID, data)
    assert "EVIDENCE_INVALID" in issue_codes(report)


def test_unparsed_tail_and_negation_omission_block_complete_but_keep_independent_fact():
    text = "甲公司回购。乙公司未取消招标。"
    first = fact(text, sentence="甲公司回购。")
    report = validate_fact_artifact(text, RECORD_ID, artifact(text, [first]))
    assert not report.validated_complete
    assert "FACT_INCOMPLETE" in issue_codes(report)
    assert report.valid_fact_ids == ("f1",)


def test_two_independent_events_in_one_sentence_need_two_fact_entries():
    text = "甲公司发布手机，同时收购芯片公司。"
    one = artifact(text, [fact(text, predicate="发布")])
    report = validate_fact_artifact(text, RECORD_ID, one)
    assert not report.validated_complete
    assert "FACT_INCOMPLETE" in issue_codes(report)

    two = artifact(text, [
        fact(text, predicate="发布", sentence="甲公司发布手机，"),
        fact(text, predicate="收购", fact_id="f2", sentence="同时收购芯片公司。"),
    ])
    assert validate_fact_artifact(text, RECORD_ID, two).validated_complete


def test_quote_speaker_is_not_accepted_as_the_reported_actor():
    text = "甲公司称乙公司减持。"
    data = artifact(text, [fact(text, subject="甲公司", predicate="减持")])
    report = validate_fact_artifact(text, RECORD_ID, data)
    assert not report.validated_complete
    assert "SUBJECT_UNRESOLVED" in issue_codes(report)


def test_same_fact_same_role_conflicting_values_are_not_complete():
    text = "甲公司发行总额为100亿元，总额为101亿元。"
    data = artifact(text, [fact(text, predicate="发行", numeric="100")])
    numerics = data["facts"][0]["numerics"]
    second = deepcopy(numerics[0])
    second["numeric_id"] = "n2"
    second["evidence"] = [evidence(text, "101", "facts.f1.numerics.n2")]
    second["value"] = present(text, "101", "facts.f1.numerics.n2.value", value="101")
    numerics.append(second)
    for index, number in enumerate(numerics, 1):
        nbase = f"facts.f1.numerics.n{index}"
        number["metric"] = present(text, "总额", nbase + ".metric", occurrence=index - 1)
        number["role"] = present(text, "总额", nbase + ".role", occurrence=index - 1)
        number["unit"] = present(text, "亿元", nbase + ".unit", occurrence=index - 1)
        number["comparator"] = present(text, f"{99 + index}亿元", nbase + ".comparator")
    report = validate_fact_artifact(text, RECORD_ID, data)
    assert not report.validated_complete
    assert any("contradiction" in issue.detail for issue in report.issues)


def test_model_failed_and_partial_are_not_relabelled_as_real_missing():
    text = "甲公司回购。"
    failed = artifact(text, [])
    failed["extraction_status"] = "failed"
    partial = artifact(text, [fact(text)])
    partial["extraction_status"] = "partial"
    assert "EXTRACTION_FAILED" in issue_codes(validate_fact_artifact(text, RECORD_ID, failed))
    assert "FACT_INCOMPLETE" in issue_codes(validate_fact_artifact(text, RECORD_ID, partial))


def test_uncertain_slot_and_nonexistent_uncertainty_path_block_validated_complete():
    text = "甲公司完成回购。"
    data = artifact(text, [fact(text)])
    data["facts"][0]["time"]["expression"] = {
        "status": "uncertain", "raw_value": "回购", "evidence": [evidence(text, "回购", "facts.f1.time.expression")]
    }
    report = validate_fact_artifact(text, RECORD_ID, data)
    assert not report.validated_complete
    assert "FACT_INCOMPLETE" in issue_codes(report)

    bad_path = artifact(text, [fact(text)])
    bad_path["uncertainties"].append({"field": "facts.f9.subject", "issue": "unknown", "evidence": []})
    assert "EVIDENCE_INVALID" in issue_codes(validate_fact_artifact(text, RECORD_ID, bad_path))


def test_injectable_model_is_single_text_only_and_cached_by_text_and_version():
    text = "甲公司完成回购。"
    model = DeterministicFactModel({text: artifact(text, [fact(text)])})
    service = FactExtractionService(model, extraction_artifact_version="model-prompt-schema-parser-v1")

    first = service.extract_once(RECORD_ID, text)
    second = service.extract_once(RECORD_ID, text)

    assert first == second
    assert first.validated_complete
    assert len(model.calls) == 1
    assert model.calls[0].record_id == RECORD_ID
    assert model.calls[0].text == text
    assert not hasattr(model.calls[0], "candidate_text")
    assert not hasattr(model.calls[0], "business_date")

    first.artifact["facts"][0]["subject"]["raw_value"] = "乙公司"
    third = service.extract_once(RECORD_ID, text)
    assert third.artifact["facts"][0]["subject"]["raw_value"] == "甲公司"
    assert len(model.calls) == 1
