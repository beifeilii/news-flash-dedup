# -*- coding: utf-8 -*-
"""B2-01 构造例红钉（09-注记-B2-01-贪心匹配.md ⑤；panel-b ③(b) 三窗共同条件）。

★判定壁型刻画测试：现状贪心语义下本文件全绿；任何"静默升格"（二分图最大
匹配/锚定规则改写/遍历序改写）→主钉转红，强制先过契约判定壁。
构造例=Tier-2 词典锚定规则（pair_alignment.py:294-295）破坏传递性路径
（Tier-1 _pointer_text_match 为等价关系，字面构造例不可实现——论证见
log/temp/w-a-design.md §5.1 边集表）。
夹具形复用 tests/unit/test_pair_alignment_normalize.py:68-176 姿势。
"""
from __future__ import annotations

import json

import pytest

from news_flash_dedup.compare import normalize_dict, pair_alignment
from news_flash_dedup.compare.pair_alignment import build_aligned

RECORD_ID_H = "h" * 64
RECORD_ID_C = "c" * 64
DICT_VERSION = "norm_dict_v1"
DICT_DATE = "2026-10-07"
ALIGNMENT_VERSION = "alignment_v1"

HISTORY_TEXT = "埃克森美孚上调年度盈利预测。EXXON调高原油产量目标。"
CURRENT_TEXT_A = "埃克森美孚调高年度盈利预测。埃克森上调原油产量目标。"
CURRENT_TEXT_B = "埃克森上调原油产量目标。埃克森美孚调高年度盈利预测。"


# ---------- 夹具（test_pair_alignment_normalize.py 同形） ----------

def _evidence(text, quote, field, *, record_id, occurrence=0):
    start = -1
    for _ in range(occurrence + 1):
        start = text.index(quote, start + 1)
    return {"record_id": record_id, "field": field,
            "quote": quote, "start": start, "end": start + len(quote)}


def _missing():
    return {"status": "missing", "raw_value": None, "evidence": []}


def _present(text, quote, field, *, record_id, occurrence=0):
    return {"status": "present", "raw_value": quote,
            "evidence": [_evidence(text, quote, field,
                                   record_id=record_id, occurrence=occurrence)]}


def _fact(text, *, fact_id, subject, predicate, sentence, record_id,
          subject_occurrence=0):
    base = f"facts.{fact_id}"
    return {
        "fact_id": fact_id,
        "evidence": [_evidence(text, sentence, base, record_id=record_id)],
        "fact_type": _present(text, predicate, base + ".fact_type",
                              record_id=record_id),
        "subject": _present(text, subject, base + ".subject",
                            record_id=record_id, occurrence=subject_occurrence),
        "event_state": {
            "predicate": _present(text, predicate, base + ".event_state.predicate",
                                  record_id=record_id),
            "polarity": _present(text, predicate, base + ".event_state.polarity",
                                 record_id=record_id),
            "modality": _missing(),
            "attribution": _missing(),
        },
        "time": {"expression": _missing(), "stage": _missing(),
                 "anchor": _missing()},
        "key_object": _missing(),
        "numerics": [],
    }


def _report(text, facts, *, record_id):
    from news_flash_dedup.facts import FactValidationReport
    artifact = {"schema_version": "1.0", "record_id": record_id,
                "offset_unit": "unicode_code_point",
                "extraction_status": "complete", "facts": facts,
                "unparsed_spans": [], "uncertainties": []}
    return FactValidationReport(
        record_id=record_id, validated_complete=True,
        extraction_status="complete",
        valid_fact_ids=tuple(f["fact_id"] for f in facts),
        numeric_inventory=(), issues=(), artifact=artifact)


def _norm_payload():
    return {
        "version": DICT_VERSION, "date": DICT_DATE,
        "sections": {
            "subject": {"kind": "alias", "directional": False, "entries": [
                {"id": "subj-0001", "canonical": "埃克森美孚",
                 "variants": ["埃克森", "EXXON"]}]},
            "predicate": {"kind": "synonym_class", "directional": False,
                          "entries": [
                {"id": "pred-0001", "canonical": "上调",
                 "variants": ["调高"]}]},
        },
    }


@pytest.fixture()
def dictionary(tmp_path):
    normalize_dict.clear_cache()
    path = tmp_path / "norm_dict.json"
    path.write_text(json.dumps(_norm_payload(), ensure_ascii=False),
                    encoding="utf-8")
    loaded = normalize_dict.load_dictionary(str(path))
    yield loaded
    normalize_dict.clear_cache()


def _history_report():
    return _report(HISTORY_TEXT, [
        _fact(HISTORY_TEXT, fact_id="f1", subject="埃克森美孚", predicate="上调",
              sentence="埃克森美孚上调年度盈利预测。", record_id=RECORD_ID_H),
        _fact(HISTORY_TEXT, fact_id="f2", subject="EXXON", predicate="调高",
              sentence="EXXON调高原油产量目标。", record_id=RECORD_ID_H),
    ], record_id=RECORD_ID_H)


def _current_report(text, *, first_subject, first_predicate,
                    second_subject, second_predicate, second_occurrence):
    return _report(text, [
        _fact(text, fact_id="g1", subject=first_subject, predicate=first_predicate,
              sentence=text.split("。")[0] + "。", record_id=RECORD_ID_C),
        _fact(text, fact_id="g2", subject=second_subject, predicate=second_predicate,
              sentence=text.split("。")[1] + "。", record_id=RECORD_ID_C,
              subject_occurrence=second_occurrence),
    ], record_id=RECORD_ID_C)


def _align(current_text, dictionary, **current_kw):
    return build_aligned(
        _history_report(), _current_report(current_text, **current_kw),
        HISTORY_TEXT, current_text,
        dictionary_version=DICT_VERSION, dictionary_date=DICT_DATE,
        alignment_version=ALIGNMENT_VERSION, dictionary=dictionary)


# ---------- 主钉：序 A 贪心漏配（钉住现状语义） ----------

def test_b201_greedy_order_a_leaves_perfect_matching_unmatched(dictionary):
    """主钉（判定壁）：序 A 贪心首配抢占→漏配 1 对；完美匹配 2 对客观存在。

    边集={(h1,c1),(h1,c2),(h2,c1)}；h2≁c2（锚定规则 :294 双槽同归一拒）。
    现状语义断言（转正条件=契约修订后本钉按新语义改写，不得静默改绿）：
    - aligned 恰 1 对 (f1,g1)，basis=SUBJECT_DETERMINISTIC_ALIAS（Tier-2 命中）；
    - unresolved 恰 ("history:f2","current:g2")；
    - 完美匹配 {(f1,g2),(f2,g1)} 存在性=本注释伴生断言（贪心未达）。
    """
    outcome = _align(
        CURRENT_TEXT_A, dictionary,
        first_subject="埃克森美孚", first_predicate="调高",
        second_subject="埃克森", second_predicate="上调",
        second_occurrence=1)       # "埃克森" 在序 A 第二段是第 2 次出现
    assert len(outcome.aligned_facts) == 1
    pair = outcome.aligned_facts[0]
    assert (pair.history.fact_id, pair.current.fact_id) == ("f1", "g1")
    assert pair.basis == "SUBJECT_DETERMINISTIC_ALIAS"
    assert pair.normalization_hits                       # Tier-2 命中留痕
    assert outcome.unresolved_fields == ("history:f2", "current:g2")
    assert outcome.code == "FACT_EQUIVALENT"
    assert dict(outcome.provenance) == {
        "dictionary_version": DICT_VERSION,
        "alignment_version": ALIGNMENT_VERSION,
        "dictionary_date": DICT_DATE,
    }


def test_b201_greedy_order_b_full_alignment_companion_pin(dictionary):
    """伴生绿钉：序 B（候选位置互换）→ 同一词典同一 history 全配 2 对——
    钉"贪心语义由遍历序 (evidence.start, fact_id) 决定"（pair_alignment.py:194）。
    任何人改排序键/改贪心为最大匹配→本钉与主钉行为同改=漂移告警。"""
    outcome = _align(
        CURRENT_TEXT_B, dictionary,
        first_subject="埃克森", first_predicate="上调",
        second_subject="埃克森美孚", second_predicate="调高",
        second_occurrence=0)       # "埃克森美孚" 在序 B 全文唯一出现（第二段）
    assert len(outcome.aligned_facts) == 2
    matched = {(p.history.fact_id, p.current.fact_id)
               for p in outcome.aligned_facts}
    assert matched == {("f1", "g1"), ("f2", "g2")}
    assert all(p.basis == "SUBJECT_DETERMINISTIC_ALIAS"
               for p in outcome.aligned_facts)
    assert outcome.unresolved_fields == ()


def test_b201_tier1_equivalence_blocks_literal_construction_green_guard():
    """绿守卫：去词典（dictionary=None）后序 A/B 均 Tier-1 零配——
    构造例只能经 Tier-2 锚定非等价路径实现（Tier-1 贪心恒最大匹配的
    结构性证明之行为面投影）。"""
    for text in (CURRENT_TEXT_A, CURRENT_TEXT_B):
        outcome = build_aligned(
            _history_report(),
            _current_report(text,
                            first_subject="埃克森美孚", first_predicate="调高",
                            second_subject="埃克森", second_predicate="上调",
                            second_occurrence=1),
            HISTORY_TEXT, text,
            dictionary_version=DICT_VERSION, alignment_version=ALIGNMENT_VERSION,
            dictionary=None)
        assert outcome.aligned_facts == ()
        assert outcome.code == "RULE_UNCOVERED"
