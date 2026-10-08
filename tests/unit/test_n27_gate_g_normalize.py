"""N27（D28-5）签发门 g polarity 归一化契约单元测试（红能力测试先行）。

裁定范围（用户 D28 第 5 件 + 窗口I 一致性裁决=一致）：B3 签发壁
`_fact_equivalence_provable` 条件 g 的 polarity 槽由"双侧 present 且逐字等"
改"双侧 present 且（逐字等 或 对齐门 Tier-2 已留痕的 predicate 词典同义
覆盖）"——归一证据只从 AlignedPair.normalization_hits 消费（pair_alignment
锚定规则已执法：单槽归一+另一槽逐字锚，双槽同归一在对齐层即拒），签发门
不私查词典、不改签名；modality（条件 h）保持逐字不动——"另一槽保持逐字
（双槽同归一照拒）"锚规则为 fp 结构性保证，钉值在案。

红绿纪律：
- test_n27_gate_g_predicate_synonym_hit_signs：旧码红（polarity 逐字拒签）、
  新码绿（归一命中覆盖 → 签发 FACT_EQUIVALENT）——本裁定唯一行为变更点；
- 其余锚/拒绝用例新旧码均绿（fp 保护面零松动）：
  · 双槽同归一照拒：polarity 归一可解但 modality 双槽异（另一槽逐字）→ 照拒；
  · 否定极性：subject 别名对齐 + polarity 否定不对称（hit 不覆盖 polarity
    raw）→ 照拒（"回购 vs 未回购"不同案保护）；
  · Tier-1 对齐（无 hit）polarity 差异即便在典 → 照拒（门不私查词典，
    fail-closed）；
  · 无词典供给（v1 腿口径）同义对在对齐层即不配对 → 零效应。
夹具姿势复用 tests/unit/test_p15_text_certificate.py（B3 签发壁）与
tests/unit/test_pair_alignment_normalize.py（Tier-2 词典）既有形态。
"""

from __future__ import annotations

import json

import pytest

from news_flash_dedup.compare import (
    aggregate as aggregate_module,
    normalize_dict,
    p15_integration,
    pair_alignment,
    pair_compare,
)
from news_flash_dedup.compare.aggregate import CoverageStatus, FrozenRecallPlan
from news_flash_dedup.facts import FactValidationReport


RECORD_ID_H = "a" * 64
RECORD_ID_C = "b" * 64
NORM_DICT_VERSION = "norm_dict_v1"
NORM_DICT_DATE = "2026-09-28"


@pytest.fixture(autouse=True)
def _clean_dict_cache():
    normalize_dict.clear_cache()
    yield
    normalize_dict.clear_cache()


# ---------- 夹具（姿势同 test_p15_text_certificate.py） ----------

def _evidence(text, quote, field, *, record_id):
    start = text.index(quote)
    return {"record_id": record_id, "field": field, "quote": quote,
            "start": start, "end": start + len(quote)}


def _missing():
    return {"status": "missing", "raw_value": None, "evidence": []}


def _present(text, quote, field, *, record_id):
    return {"status": "present", "raw_value": quote,
            "evidence": [_evidence(text, quote, field, record_id=record_id)]}


def _time_verified_missing():
    """真 P14"原文确无"时间槽（维持签发路径不被时间门占位阻断，同 B3 专项）。"""
    return {"status": "missing", "raw_value": None, "evidence": [],
            "verified_missing": True}


def _make_fact(text, *, fact_id="f1", subject, predicate, polarity=None,
               modality=None, record_id=RECORD_ID_H):
    base = f"facts.{fact_id}"
    if polarity is None:
        polarity = predicate
    modality_slot = (_present(text, modality, base + ".event_state.modality",
                              record_id=record_id) if modality else _missing())
    return {
        "fact_id": fact_id,
        "evidence": [_evidence(text, text, base, record_id=record_id)],
        "fact_type": _present(text, predicate, base + ".fact_type",
                              record_id=record_id),
        "subject": _present(text, subject, base + ".subject", record_id=record_id),
        "event_state": {
            "predicate": _present(text, predicate, base + ".event_state.predicate",
                                  record_id=record_id),
            "polarity": _present(text, polarity, base + ".event_state.polarity",
                                 record_id=record_id),
            "modality": modality_slot,
            "attribution": _missing(),
        },
        "time": {"expression": _time_verified_missing(),
                 "stage": _missing(), "anchor": _missing()},
        "key_object": _missing(),
        "numerics": [],
    }


def _make_report(text, facts, *, record_id):
    return FactValidationReport(
        record_id=record_id,
        validated_complete=bool(facts),
        extraction_status="complete",
        valid_fact_ids=tuple(f["fact_id"] for f in facts),
        numeric_inventory=(),
        issues=(),
        artifact={
            "schema_version": "1.0", "record_id": record_id,
            "offset_unit": "unicode_code_point",
            "extraction_status": "complete",
            "facts": facts, "unparsed_spans": [], "uncertainties": [],
        },
    )


def _ctx(*, record_id, item_id, text, arrival_seq):
    import hashlib

    return {"record_id": record_id, "item_id": item_id, "text": text,
            "raw_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "scope_id": "default", "business_date": "2026-09-26",
            "arrival_seq": arrival_seq, "pipeline_version": "dedup_v1"}


# ---------- 词典夹具（测试本地临时词典，真词典零增删） ----------

def _norm_payload() -> dict:
    return {
        "version": NORM_DICT_VERSION,
        "date": NORM_DICT_DATE,
        "sections": {
            "subject": {
                "kind": "alias",
                "directional": False,
                "entries": [
                    {"id": "subj-0001", "canonical": "贵州茅台",
                     "variants": ["茅台"]},
                ],
            },
            "predicate": {
                "kind": "synonym_class",
                "directional": False,
                "entries": [
                    {"id": "pred-0001", "canonical": "上涨",
                     "variants": ["上升", "升至"]},
                ],
            },
        },
    }


@pytest.fixture()
def dictionary(tmp_path):
    path = tmp_path / "norm_dict.json"
    path.write_text(json.dumps(_norm_payload(), ensure_ascii=False),
                    encoding="utf-8")
    return normalize_dict.load_dictionary(str(path))


# ---------- 全链驱动（build_aligned → extract_p15_results → compare_pair → aggregate） ----------

def _run(history_text, current_text, *, h_fact, c_fact, dictionary=None):
    history = _make_report(history_text, [h_fact], record_id=RECORD_ID_H)
    current = _make_report(current_text, [c_fact], record_id=RECORD_ID_C)
    align_kwargs = dict(
        dictionary_version=(NORM_DICT_VERSION if dictionary is not None
                            else "dict_v1"),
        alignment_version="alignment_v1",
    )
    if dictionary is not None:
        align_kwargs["dictionary_date"] = NORM_DICT_DATE
        align_kwargs["dictionary"] = dictionary
    alignment = pair_alignment.build_aligned(
        history, current, history_text, current_text, **align_kwargs)
    p15 = p15_integration.extract_p15_results(
        history, current, history_text, current_text, alignment)
    history_ctx = _ctx(record_id=RECORD_ID_H, item_id="item-H",
                       text=history_text, arrival_seq=1)
    current_ctx = _ctx(record_id=RECORD_ID_C, item_id="item-C",
                       text=current_text, arrival_seq=2)
    pair = pair_compare.compare_pair(
        history_ctx, current_ctx,
        history_artifact=history, current_artifact=current,
        alignment=alignment, pipeline_version="dedup_v1",
        p15_results=p15.p15_results)
    plan = FrozenRecallPlan(version="rrf_v1", required={RECORD_ID_H: history_ctx})
    out = aggregate_module.aggregate(
        current_ctx, plan, [pair],
        coverage=CoverageStatus(visible_seq=10, prepared_seq=10, complete=True))
    return alignment, pair, out, p15


# ---------- 行为变更点（旧码红 → 新码绿） ----------

def test_n27_gate_g_predicate_synonym_hit_signs(dictionary):
    """N27 主用例：subject 逐字锚 + predicate/polarity 同义（升至↔上升，
    pred-0001）经对齐 Tier-2 留痕 → 门 g 归一后等 → FACT_EQUIVALENT 签发。

    旧码：polarity raw 逐字不等（升至≠上升）→ 拒签（本用例红属预期）；
    新码：对齐留痕 predicate hit 恰好覆盖 polarity 双侧 raw → 签发。
    """
    history_text = "甲公司股价升至百元。"
    current_text = "甲公司股价上升百元。"
    alignment, pair, out, p15 = _run(
        history_text, current_text,
        h_fact=_make_fact(history_text, subject="甲公司", predicate="升至",
                          record_id=RECORD_ID_H),
        c_fact=_make_fact(current_text, subject="甲公司", predicate="上升",
                          record_id=RECORD_ID_C),
        dictionary=dictionary)
    # 前置钉死：对齐确为 Tier-2 且留痕恰为 predicate 同义（签发门证据来源）
    assert len(alignment.aligned_facts) == 1
    aligned = alignment.aligned_facts[0]
    assert aligned.basis == "SUBJECT_DETERMINISTIC_ALIAS"
    assert len(aligned.normalization_hits) == 1
    hit = aligned.normalization_hits[0]
    assert hit.slot == "predicate"
    assert {hit.history_raw, hit.current_raw} == {"升至", "上升"}
    assert hit.canonical == "上涨"
    assert hit.entry_id == "pred-0001"
    # 裁定行为：归一命中覆盖 polarity → 签发等价
    assert p15.p15_results.equivalence_ready is True
    assert p15.p15_results.text_proof == "FACT_EQUIVALENT"
    assert pair.outcome == "equivalent"
    assert pair.code == "FACT_EQUIVALENT"
    assert out.decision == "重复"
    assert out.duplicate_ids == ("item-H",)


# ---------- 锚/拒绝面（新旧码均必须绿：fp 结构性保证零松动） ----------

def test_n27_modality_verbatim_anchor_still_blocks(dictionary):
    """双槽同归一照拒锚：polarity 同义可经归一解，但 modality（另一槽，
    保持逐字）双侧 present 且不等（拟≠将）→ 照拒签发、照落边界。

    若两槽同走归一，本对会被误放——锚规则（另一槽逐字）是 fp 结构性保证，
    新旧码均绿：旧码在门 g 逐字拒，新码在门 h 逐字拒。
    """
    history_text = "甲公司拟升至百元。"
    current_text = "甲公司将上升百元。"
    _, pair, out, p15 = _run(
        history_text, current_text,
        h_fact=_make_fact(history_text, subject="甲公司", predicate="升至",
                          modality="拟", record_id=RECORD_ID_H),
        c_fact=_make_fact(current_text, subject="甲公司", predicate="上升",
                          modality="将", record_id=RECORD_ID_C),
        dictionary=dictionary)
    assert p15.p15_results.equivalence_ready is False
    assert p15.p15_results.text_proof is None
    assert pair.outcome == "unresolved"
    assert out.decision != "重复"
    assert out.duplicate_ids == ()


def test_n27_negation_polarity_not_covered_by_hit_still_blocks(dictionary):
    """否定极性保护：subject 别名（茅台↔贵州茅台）Tier-2 对齐 + predicate
    逐字等，polarity 否定不对称（回购 vs 并未回购）→ 照拒。

    对齐留痕为 subject 槽 hit，不覆盖 polarity raw；门 g 只消费 predicate
    槽 hit——"回购 vs 未回购必须不同案"的 fp 保护钉值，新旧码均绿。
    """
    history_text = "茅台完成回购。"
    current_text = "贵州茅台完成并未回购。"
    _, pair, out, p15 = _run(
        history_text, current_text,
        h_fact=_make_fact(history_text, subject="茅台", predicate="回购",
                          polarity="回购", record_id=RECORD_ID_H),
        c_fact=_make_fact(current_text, subject="贵州茅台", predicate="回购",
                          polarity="并未回购", record_id=RECORD_ID_C),
        dictionary=dictionary)
    assert p15.p15_results.equivalence_ready is False
    assert p15.p15_results.text_proof is None
    assert pair.outcome == "unresolved"
    assert out.decision != "重复"
    assert out.duplicate_ids == ()


def test_n27_tier1_polarity_difference_without_hit_still_blocks(dictionary):
    """门不私查词典：Tier-1 逐字对齐（subject/predicate 双逐字锚，零 hit），
    polarity 槽独立呈同义异写（上涨 vs 上升，在典）→ 照拒。

    归一证据必须经对齐门锚定规则留痕方可消费；签发门自身不持词典、不做
    主动归一——fail-closed，新旧码均绿。
    """
    history_text = "甲公司股价上涨。"
    current_text = "甲公司股价上涨（前日上升）。"
    _, pair, out, p15 = _run(
        history_text, current_text,
        h_fact=_make_fact(history_text, subject="甲公司", predicate="上涨",
                          polarity="上涨", record_id=RECORD_ID_H),
        c_fact=_make_fact(current_text, subject="甲公司", predicate="上涨",
                          polarity="上升", record_id=RECORD_ID_C),
        dictionary=dictionary)
    assert p15.p15_results.equivalence_ready is False
    assert p15.p15_results.text_proof is None
    assert pair.outcome == "unresolved"
    assert out.decision != "重复"
    assert out.duplicate_ids == ()


def test_n27_no_dictionary_zero_effect():
    """v1 腿口径（无词典供给）：同义对在对齐层即不配对（RULE_UNCOVERED），
    签发门零归一证据可消费 → 拒签；新旧码行为逐字节一致（v1 锚零效应）。
    """
    history_text = "甲公司股价升至百元。"
    current_text = "甲公司股价上升百元。"
    alignment, pair, out, p15 = _run(
        history_text, current_text,
        h_fact=_make_fact(history_text, subject="甲公司", predicate="升至",
                          record_id=RECORD_ID_H),
        c_fact=_make_fact(current_text, subject="甲公司", predicate="上升",
                          record_id=RECORD_ID_C),
        dictionary=None)
    assert alignment.code == "RULE_UNCOVERED"
    assert not alignment.aligned_facts
    assert p15.p15_results.equivalence_ready is False
    assert p15.p15_results.text_proof is None
    assert pair.outcome == "unresolved"
    assert out.decision != "重复"
    assert out.duplicate_ids == ()
