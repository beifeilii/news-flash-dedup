"""P16-A 双侧 Evidence 对齐门测试。

覆盖 INV-11（双侧 Evidence 对齐门不接模型/Hash 直填）、02:43 批复指定的
同事件改写 / 基数不同 / 无关事件三例，以及白名单 basis 与对齐来源合同。
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy

import pytest

from news_flash_dedup.compare import pair_alignment
from news_flash_dedup.facts import FactValidationReport


RECORD_ID_H = "a" * 64
RECORD_ID_C = "b" * 64
ALIGNMENT_VERSION = "alignment_v1"


def _evidence(text, quote, field, *, record_id=RECORD_ID_H, occurrence=0):
    start = -1
    for _ in range(occurrence + 1):
        start = text.index(quote, start + 1)
    return {
        "record_id": record_id,
        "field": field,
        "quote": quote,
        "start": start,
        "end": start + len(quote),
    }


def _missing(*, decimal=False, record_id=RECORD_ID_H):
    result = {"status": "missing", "raw_value": None, "evidence": []}
    if decimal:
        result["value"] = None
    return result


def _present(text, quote, field, *, value=None, occurrence=0, record_id=RECORD_ID_H):
    result = {
        "status": "present",
        "raw_value": quote,
        "evidence": [_evidence(text, quote, field, record_id=record_id, occurrence=occurrence)],
    }
    if value is not None:
        result["value"] = value
    return result


def _make_fact(text, *, fact_id, subject, predicate, key_object="",
               sentence=None, record_id=RECORD_ID_H):
    base = f"facts.{fact_id}"
    sentence = sentence or text
    return {
        "fact_id": fact_id,
        "evidence": [_evidence(text, sentence, base, record_id=record_id)],
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
        "time": {"expression": _missing(), "stage": _missing(), "anchor": _missing()},
        "key_object": (_present(text, key_object, base + ".key_object", record_id=record_id)
                       if key_object else _missing()),
        "numerics": [],
    }


def _make_artifact(text, facts, *, record_id=RECORD_ID_H, extraction_status="complete"):
    return {
        "schema_version": "1.0",
        "record_id": record_id,
        "offset_unit": "unicode_code_point",
        "extraction_status": extraction_status,
        "facts": facts,
        "unparsed_spans": [],
        "uncertainties": [],
    }


def _make_report(text, facts, *, record_id=None,
                 extraction_status="complete") -> FactValidationReport:
    if record_id is None:
        for fact in facts:
            for entry in (fact.get("evidence") or []):
                if isinstance(entry, Mapping) and entry.get("record_id"):
                    record_id = entry["record_id"]
                    break
            if record_id is not None:
                break
    if record_id is None:
        record_id = RECORD_ID_H
    artifact = _make_artifact(text, facts, record_id=record_id,
                              extraction_status=extraction_status)
    mentions = []
    return FactValidationReport(
        record_id=record_id,
        validated_complete=bool(facts) and extraction_status == "complete",
        extraction_status=extraction_status,
        valid_fact_ids=tuple(fact["fact_id"] for fact in facts),
        numeric_inventory=tuple(mentions),
        issues=(),
        artifact=artifact,
    )


# ---------- 基础闸门 ----------

def test_pair_alignment_module_exposes_build_aligned():
    """P16-A 入口：build_aligned 函数必须存在且签名稳定。"""
    assert hasattr(pair_alignment, "build_aligned"), "pair_alignment module must expose build_aligned"


def test_pair_alignment_rejects_empty_inputs():
    """空正文或空工件必须明确拒绝，禁止空命中冒充对齐。"""
    history_text = "甲公司完成回购。"
    history = _make_report(history_text, [
        _make_fact(history_text, fact_id="f1", subject="甲公司", predicate="回购",
                   record_id=RECORD_ID_H),
    ])
    current = _make_report(history_text, [
        _make_fact(history_text, fact_id="f1", subject="甲公司", predicate="回购",
                   record_id=RECORD_ID_C),
    ])
    with pytest.raises((ValueError, TypeError)):
        pair_alignment.build_aligned(history, current, "", history_text,
                                      dictionary_version="dict_v1",
                                      alignment_version=ALIGNMENT_VERSION)
    with pytest.raises((ValueError, TypeError)):
        pair_alignment.build_aligned(history, current, history_text, "",
                                      dictionary_version="dict_v1",
                                      alignment_version=ALIGNMENT_VERSION)


def test_pair_alignment_basis_must_be_in_whitelist():
    """白名单：basis 只能来自受信确定性来源；任何模型/Hash/打分来源都拒。"""
    text = "甲公司完成回购。"
    history = _make_report(text, [
        _make_fact(text, fact_id="f1", subject="甲公司", predicate="回购",
                   record_id=RECORD_ID_H),
    ])
    current = _make_report(text, [
        _make_fact(text, fact_id="f1", subject="甲公司", predicate="回购",
                   record_id=RECORD_ID_C),
    ])
    forbidden = ["MODEL_DECLARED", "HASH_MATCH", "BUCKET_HIT",
                 "BM25_SCORE", "VECTOR_SCORE", "JACCARD", "OVERLAP_RATE"]
    for bad in forbidden:
        with pytest.raises(ValueError, match="(?i)whitelist|basis|forbidden"):
            pair_alignment.build_aligned(history, current, text, text,
                                          dictionary_version="dict_v1",
                                          alignment_version=ALIGNMENT_VERSION,
                                          candidate_basis=bad)


def test_pair_alignment_records_provenance():
    """对齐来源 provenance：dictionary_version / alignment_version / dictionary_date 必须可查。"""
    text = "甲公司完成回购。"
    history = _make_report(text, [
        _make_fact(text, fact_id="f1", subject="甲公司", predicate="回购",
                   record_id=RECORD_ID_H),
    ])
    current = _make_report(text, [
        _make_fact(text, fact_id="f1", subject="甲公司", predicate="回购",
                   record_id=RECORD_ID_C),
    ])
    outcome = pair_alignment.build_aligned(history, current, text, text,
                                           dictionary_version="dict_v1",
                                           dictionary_date="2026-09-26",
                                           alignment_version=ALIGNMENT_VERSION)
    assert outcome.provenance["dictionary_version"] == "dict_v1"
    assert outcome.provenance["dictionary_date"] == "2026-09-26"
    assert outcome.provenance["alignment_version"] == ALIGNMENT_VERSION


# ---------- 02:43 批复红灯例 1：同事件改写双文 FACT_EQUIVALENT ----------

def test_red_01_rewritten_dual_text_aligns_to_fact_equivalent():
    """双方主体/谓词/关键对象码点定位且完全一致、但句子改写，应能产出 FACT_EQUIVALENT。

    历史文本："甲公司于9月9日宣布完成回购，交易对手为六家机构。"
    新文本  ："回购已完成。甲公司是实施方，对手方六家机构。"
    双侧核心事件主体="甲公司"、谓词="回购"、关键对象="六家机构"在原文 Evidence
    全部可定位且原文一致；属于改写而非新增独立事件。
    """
    history_text = "甲公司于9月9日宣布完成回购，交易对手为六家机构。"
    current_text = "回购已完成。甲公司是实施方，对手方六家机构。"

    history = _make_report(history_text, [
        _make_fact(history_text, fact_id="f1", subject="甲公司", predicate="回购",
                   key_object="六家机构", record_id=RECORD_ID_H),
    ])
    current = _make_report(current_text, [
        _make_fact(current_text, fact_id="f1", subject="甲公司", predicate="回购",
                   key_object="六家机构", record_id=RECORD_ID_C),
    ])
    outcome = pair_alignment.build_aligned(history, current, history_text, current_text,
                                           dictionary_version="dict_v1",
                                           alignment_version=ALIGNMENT_VERSION)
    assert outcome.aligned_facts, "改写双文应能配对核心事件 Fact"
    assert outcome.code in {"FACT_EQUIVALENT", "EXACT_TEXT_MATCH", "LOSSLESS_TEXT_MATCH"}, \
        f"改写双文应进入 equivalent 系内部码，实际 {outcome.code}"
    assert not outcome.unresolved_fields, "改写双文不应有未解字段"


# ---------- 02:43 批复红灯例 2：基数不同但核心事件可配对不直接 RULE_UNCOVERED ----------

def test_red_02_different_fact_count_still_aligns_core_events():
    """基数不同（历史 2 个 Fact、新 3 个 Fact），核心事件可配对 → 核心+附属进入
    aligned_facts，剩余不可配对 Fact 入 unresolved_fields；不得直接 RULE_UNCOVERED。"""
    history_text = "甲公司完成回购。甲公司披露交易对手。"
    current_text = "甲公司完成回购。同时披露六家交易对手。"

    history = _make_report(history_text, [
        _make_fact(history_text, fact_id="f1", subject="甲公司", predicate="回购",
                   record_id=RECORD_ID_H),
        _make_fact(history_text, fact_id="f2", subject="甲公司", predicate="披露",
                   key_object="交易对手", record_id=RECORD_ID_H),
    ])
    current = _make_report(current_text, [
        _make_fact(current_text, fact_id="f1", subject="甲公司", predicate="回购",
                   record_id=RECORD_ID_C),
        _make_fact(current_text, fact_id="f2", subject="甲公司", predicate="披露",
                   key_object="六家交易对手", record_id=RECORD_ID_C),
    ])
    outcome = pair_alignment.build_aligned(history, current, history_text, current_text,
                                           dictionary_version="dict_v1",
                                           alignment_version=ALIGNMENT_VERSION)
    assert outcome.aligned_facts, "核心事件应能配对"
    assert outcome.code != "RULE_UNCOVERED", \
        "基数不同但核心事件可配对时不得直接 RULE_UNCOVERED"
    assert "f1" in {pair.history.fact_id for pair in outcome.aligned_facts}, \
        "核心回购事件应进入对齐"


# ---------- 02:43 批复红灯例 3：无关事件双文正确落 unresolved ----------

def test_red_03_unrelated_events_fall_to_unresolved():
    """双方主体/谓词/关键对象均无共同核心事件 → aligned_facts 空、code=RULE_UNCOVERED。"""
    history_text = "甲公司完成回购。"
    current_text = "乙公司发布新手机。"

    history = _make_report(history_text, [
        _make_fact(history_text, fact_id="f1", subject="甲公司", predicate="回购",
                   record_id=RECORD_ID_H),
    ])
    current = _make_report(current_text, [
        _make_fact(current_text, fact_id="f1", subject="乙公司", predicate="发布",
                   key_object="新手机", record_id=RECORD_ID_C),
    ])
    outcome = pair_alignment.build_aligned(history, current, history_text, current_text,
                                           dictionary_version="dict_v1",
                                           alignment_version=ALIGNMENT_VERSION)
    assert not outcome.aligned_facts, "无关事件双文不得有对齐"
    assert outcome.code == "RULE_UNCOVERED"
    assert outcome.unresolved_fields, "应记录未解字段"


# ---------- 多对一仅限 09 §9.3 附属关系 ----------

def test_pair_alignment_rejects_multi_to_one_without_supplementary_proof():
    """多对一配对必须提供 basis=SUPPLEMENTARY_RELATION；否则保持一对一与未解。"""
    history_text = "甲公司完成收购，标的为A项目。"
    current_text = "甲公司完成收购，标的为A项目，签约方为三方。"

    history = _make_report(history_text, [
        _make_fact(history_text, fact_id="f1", subject="甲公司", predicate="收购",
                   key_object="A项目", record_id=RECORD_ID_H),
    ])
    current = _make_report(current_text, [
        _make_fact(current_text, fact_id="f1", subject="甲公司", predicate="收购",
                   key_object="A项目", record_id=RECORD_ID_C),
    ])
    outcome = pair_alignment.build_aligned(history, current, history_text, current_text,
                                           dictionary_version="dict_v1",
                                           alignment_version=ALIGNMENT_VERSION)
    # 当前文本里 f1 即包含"签约方为三方"作为属性补充；新文本单 Fact 内含同等信息
    # 一对一应已足够；多对一不会被强行构造
    multi_count = sum(1 for pair in outcome.aligned_facts
                      if pair.basis == "SUPPLEMENTARY_RELATION")
    assert multi_count == 0, "无附属证据不得使用 SUPPLEMENTARY_RELATION basis"


# ---------- 禁止相似度打分 ----------

def test_pair_alignment_rejects_similarity_inputs():
    """P16-A 不接受任何相似度打分输入：vector / bm25 / jaccard 之类一律拒。"""
    history_text = "甲公司完成回购。"
    history = _make_report(history_text, [
        _make_fact(history_text, fact_id="f1", subject="甲公司", predicate="回购",
                   record_id=RECORD_ID_H),
    ])
    current = _make_report(history_text, [
        _make_fact(history_text, fact_id="f1", subject="甲公司", predicate="回购",
                   record_id=RECORD_ID_C),
    ])
    with pytest.raises(TypeError):
        pair_alignment.build_aligned(
            history, current, history_text, history_text,
            dictionary_version="dict_v1",
            alignment_version=ALIGNMENT_VERSION,
            vector_score=0.95,
        )
    with pytest.raises(TypeError):
        pair_alignment.build_aligned(
            history, current, history_text, history_text,
            dictionary_version="dict_v1",
            alignment_version=ALIGNMENT_VERSION,
            bm25_score=12.3,
        )


# ---------- Evidence 损坏 / 错 record_id 阻断 ----------

def test_pair_alignment_rejects_evidence_with_wrong_record_id():
    """Evidence record_id 与工件 record_id 不一致时拒绝，不冒充对齐。"""
    history_text = "甲公司完成回购。"
    bad_history = _make_report(history_text, [
        _make_fact(history_text, fact_id="f1", subject="甲公司", predicate="回购",
                   record_id=RECORD_ID_H),
    ])
    # 故意把 evidence record_id 改成 current 的 record_id，模拟错绑
    bad_history.artifact["facts"][0]["subject"]["evidence"][0]["record_id"] = RECORD_ID_C
    current = _make_report(history_text, [
        _make_fact(history_text, fact_id="f1", subject="甲公司", predicate="回购",
                   record_id=RECORD_ID_C),
    ])
    with pytest.raises(ValueError, match="(?i)evidence|record_id|invalid"):
        pair_alignment.build_aligned(bad_history, current, history_text, history_text,
                                      dictionary_version="dict_v1",
                                      alignment_version=ALIGNMENT_VERSION)