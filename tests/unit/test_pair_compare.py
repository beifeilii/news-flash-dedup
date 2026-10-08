"""P16-B 对级精判 compare_pair 测试。

覆盖 03:00 批复验收标尺：
1. 三态输出合同（equivalent/conflict/unresolved）
2. PairBindingError 绑定门（raw_hash/pipeline_version/record_id/item_id/域日/早序）
3. 接 P15：只对 AlignedPair 调 compare_numeric/compare_time，未确认对齐入 unresolved_fields
4. 受控内部码三组封闭断言；公共 detail 不泄内部码
5. 09 §9.2 充分冲突独立成立（无关槽位错误不撤销该冲突）
6. 单对 conflict 不结束整条（待 P16-C 体现）
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping

import pytest

from news_flash_dedup.compare import pair_alignment, pair_compare
from news_flash_dedup.compare.pair_alignment import AlignedPair
from news_flash_dedup.compare.pair_compare import (
    PairBindingError,
    PairResult,
    P15PairResults,
    VerifiedConflict,
    PairIssue,
)
from news_flash_dedup.compare.value_time import EvidenceRef
from news_flash_dedup.facts import FactValidationReport


RECORD_ID_H = "a" * 64
RECORD_ID_C = "b" * 64
PIPELINE_VERSION = "dedup_v1"
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


def _missing():
    return {"status": "missing", "raw_value": None, "evidence": []}


def _present(text, quote, field, *, record_id=RECORD_ID_H, occurrence=0):
    return {
        "status": "present",
        "raw_value": quote,
        "evidence": [_evidence(text, quote, field, record_id=record_id, occurrence=occurrence)],
    }


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


def _make_artifact(text, facts, *, record_id, extraction_status="complete"):
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
    return FactValidationReport(
        record_id=record_id,
        validated_complete=bool(facts) and extraction_status == "complete",
        extraction_status=extraction_status,
        valid_fact_ids=tuple(fact["fact_id"] for fact in facts),
        numeric_inventory=(),
        issues=(),
        artifact=artifact,
    )


def _pair_context(*, record_id, item_id, text, scope_id, business_date,
                   arrival_seq, raw_hash=None, pipeline_version=PIPELINE_VERSION):
    from dataclasses import dataclass
    if raw_hash is None:
        raw_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    @dataclass(frozen=True)
    class _PairCtx:
        record_id: str
        item_id: str
        text: str
        raw_hash: str
        scope_id: str
        business_date: str
        arrival_seq: int
        pipeline_version: str
    return _PairCtx(record_id=record_id, item_id=item_id, text=text,
                    raw_hash=raw_hash, scope_id=scope_id,
                    business_date=business_date, arrival_seq=arrival_seq,
                    pipeline_version=pipeline_version)


def _aligned_alignment(history_report, current_report, history_text, current_text):
    return pair_alignment.build_aligned(
        history_report, current_report, history_text, current_text,
        dictionary_version="dict_v1",
        dictionary_date="2026-09-26",
        alignment_version=ALIGNMENT_VERSION,
    )


def _aligned_facts(outcome):
    return tuple(
        AlignedPair(p.history, p.current, basis=p.basis, supplementary=p.supplementary)
        for p in outcome.aligned_facts
    )


def _empty_p15():
    return P15PairResults(
        verified_conflicts=(),
        time_pairs=(),
        equivalence_ready=True,
        text_proof="FACT_EQUIVALENT",
        uncovered_independent_relation=False,
        issues=(),
    )


# ---------- 03:00 验收 1：三态输出合同 ----------

def test_equivalent_outcome_requires_aligned_pair_and_no_conflict():
    """equivalent 必须有 ≥1 AlignedPair 覆盖核心主体/对象且 verified_conflicts 空。"""
    history_text = "甲公司完成回购。"
    current_text = "甲公司完成回购。"
    history = _make_report(history_text, [
        _make_fact(history_text, fact_id="f1", subject="甲公司", predicate="回购",
                   record_id=RECORD_ID_H),
    ])
    current = _make_report(current_text, [
        _make_fact(current_text, fact_id="f1", subject="甲公司", predicate="回购",
                   record_id=RECORD_ID_C),
    ])
    alignment = _aligned_alignment(history, current, history_text, current_text)
    history_ctx = _pair_context(record_id=RECORD_ID_H, item_id="item-h", text=history_text,
                                scope_id="default", business_date="2026-09-26",
                                arrival_seq=1)
    current_ctx = _pair_context(record_id=RECORD_ID_C, item_id="item-c", text=current_text,
                                scope_id="default", business_date="2026-09-26",
                                arrival_seq=2)
    result = pair_compare.compare_pair(
        history_ctx, current_ctx,
        history_artifact=history, current_artifact=current,
        alignment=alignment,
        p15_results=_empty_p15(),
    )
    assert result.outcome == "equivalent"
    assert result.code in pair_compare.EQUIVALENT_CODES
    assert result.verified_conflicts == ()


def test_conflict_outcome_requires_verified_conflict_per_09_9_2():
    """conflict 必须有 VerifiedConflict（双侧 Evidence + 角色/单位/币种/方向/否定/事实性质）。"""
    history_text = "甲公司公布该商品现价100元。"
    current_text = "甲公司公布该商品现价101元。"
    history = _make_report(history_text, [
        _make_fact(history_text, fact_id="f1", subject="甲公司", predicate="公布",
                   key_object="该商品", record_id=RECORD_ID_H),
    ])
    current = _make_report(current_text, [
        _make_fact(current_text, fact_id="f1", subject="甲公司", predicate="公布",
                   key_object="该商品", record_id=RECORD_ID_C),
    ])
    alignment = _aligned_alignment(history, current, history_text, current_text)
    verified = VerifiedConflict(
        field_path="facts.f1.numerics.n1.value",
        basis="NUMERIC_SAME_DECIMAL",
        history_evidence=EvidenceRef(RECORD_ID_H, "facts.f1.numerics.n1.value", "100元", 10, 14),
        current_evidence=EvidenceRef(RECORD_ID_C, "facts.f1.numerics.n1.value", "101元", 10, 14),
        detail="现价 100 元 vs 101 元",
    )
    p15 = P15PairResults(
        verified_conflicts=(verified,),
        time_pairs=(),
        equivalence_ready=False,
        text_proof=None,
        uncovered_independent_relation=False,
        issues=(),
    )
    history_ctx = _pair_context(record_id=RECORD_ID_H, item_id="item-h", text=history_text,
                                scope_id="default", business_date="2026-09-26",
                                arrival_seq=1)
    current_ctx = _pair_context(record_id=RECORD_ID_C, item_id="item-c", text=current_text,
                                scope_id="default", business_date="2026-09-26",
                                arrival_seq=2)
    result = pair_compare.compare_pair(
        history_ctx, current_ctx,
        history_artifact=history, current_artifact=current,
        alignment=alignment,
        p15_results=p15,
    )
    assert result.outcome == "conflict"
    assert result.code == "VERIFIED_CONFLICT"
    assert len(result.verified_conflicts) == 1


def test_unresolved_outcome_when_aligned_empty_and_no_text_proof():
    """unresolved：aligned_facts 与 verified_conflicts 都不足。"""
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
    alignment = _aligned_alignment(history, current, history_text, current_text)
    history_ctx = _pair_context(record_id=RECORD_ID_H, item_id="item-h", text=history_text,
                                scope_id="default", business_date="2026-09-26",
                                arrival_seq=1)
    current_ctx = _pair_context(record_id=RECORD_ID_C, item_id="item-c", text=current_text,
                                scope_id="default", business_date="2026-09-26",
                                arrival_seq=2)
    result = pair_compare.compare_pair(
        history_ctx, current_ctx,
        history_artifact=history, current_artifact=current,
        alignment=alignment,
        p15_results=P15PairResults(
            verified_conflicts=(), time_pairs=(),
            equivalence_ready=False, text_proof=None,
            uncovered_independent_relation=True,  # 但 aligned_facts 空，无法进 RULE_UNCOVERED
            issues=(PairIssue("RULE_UNCOVERED", "无共同核心事件"),),
        ),
    )
    assert result.outcome == "unresolved"
    assert result.code in pair_compare.UNRESOLVED_CODES
    assert result.unresolved_fields


# ---------- 03:00 验收 2：PairBindingError 绑定门 ----------

def _base_ctx_pair(history_text, current_text):
    history = _make_report(history_text, [
        _make_fact(history_text, fact_id="f1", subject="甲公司", predicate="回购",
                   record_id=RECORD_ID_H),
    ])
    current = _make_report(current_text, [
        _make_fact(current_text, fact_id="f1", subject="甲公司", predicate="回购",
                   record_id=RECORD_ID_C),
    ])
    history_ctx = _pair_context(record_id=RECORD_ID_H, item_id="item-h", text=history_text,
                                scope_id="default", business_date="2026-09-26",
                                arrival_seq=1)
    current_ctx = _pair_context(record_id=RECORD_ID_C, item_id="item-c", text=current_text,
                                scope_id="default", business_date="2026-09-26",
                                arrival_seq=2)
    return history, current, history_ctx, current_ctx


def test_pair_binding_rejects_raw_hash_mismatch():
    """双侧 raw_hash 与文本实际不一致 → PairBindingError。"""
    history_text = "甲公司完成回购。"
    current_text = "甲公司完成回购。"
    history, current, history_ctx, current_ctx = _base_ctx_pair(history_text, current_text)
    # history ctx 给了 hash='wrong' 但 text 不匹配
    bad_history_ctx = _pair_context(record_id=RECORD_ID_H, item_id="item-h", text=history_text,
                                    scope_id="default", business_date="2026-09-26",
                                    arrival_seq=1, raw_hash="wrong-hash")
    alignment = _aligned_alignment(history, current, history_text, current_text)
    with pytest.raises(PairBindingError, match="(?i)raw_hash|hash"):
        pair_compare.compare_pair(
            bad_history_ctx, current_ctx,
            history_artifact=history, current_artifact=current,
            alignment=alignment,
            p15_results=_empty_p15(),
        )


def test_pair_binding_rejects_pipeline_version_mismatch():
    """pipeline_version 错配（不在 current 上下文一致）→ PairBindingError。"""
    history_text = "甲公司完成回购。"
    current_text = "甲公司完成回购。"
    history, current, history_ctx, current_ctx = _base_ctx_pair(history_text, current_text)
    bad_history_ctx = _pair_context(record_id=RECORD_ID_H, item_id="item-h", text=history_text,
                                    scope_id="default", business_date="2026-09-26",
                                    arrival_seq=1, raw_hash="h-hash",
                                    pipeline_version="dedup_v0")
    # bad hash + bad pipeline_version：raw_hash 校验先于 pipeline_version，
    # 为隔离 pipeline_version 校验，先把 hash 修正：
    import hashlib
    bad_history_ctx = _pair_context(record_id=RECORD_ID_H, item_id="item-h", text=history_text,
                                    scope_id="default", business_date="2026-09-26",
                                    arrival_seq=1,
                                    raw_hash=hashlib.sha256(history_text.encode("utf-8")).hexdigest(),
                                    pipeline_version="dedup_v0")
    alignment = _aligned_alignment(history, current, history_text, current_text)
    with pytest.raises(PairBindingError, match="(?i)version|pipeline"):
        pair_compare.compare_pair(
            bad_history_ctx, current_ctx,
            history_artifact=history, current_artifact=current,
            alignment=alignment,
            p15_results=_empty_p15(),
        )


def test_pair_binding_rejects_arrival_seq_not_earlier():
    """history.arrival_seq 必须 < current.arrival_seq；否则 PairBindingError。"""
    history_text = "甲公司完成回购。"
    current_text = "甲公司完成回购。"
    history, current, _, current_ctx = _base_ctx_pair(history_text, current_text)
    bad_history_ctx = _pair_context(record_id=RECORD_ID_H, item_id="item-h", text=history_text,
                                    scope_id="default", business_date="2026-09-26",
                                    arrival_seq=5, raw_hash="h-hash")  # 5 > 2
    alignment = _aligned_alignment(history, current, history_text, current_text)
    with pytest.raises(PairBindingError, match="(?i)arrival_seq|earlier|order"):
        pair_compare.compare_pair(
            bad_history_ctx, current_ctx,
            history_artifact=history, current_artifact=current,
            alignment=alignment,
            p15_results=_empty_p15(),
        )


def test_pair_binding_rejects_scope_mismatch():
    """scope_id 不同 → PairBindingError。"""
    history_text = "甲公司完成回购。"
    current_text = "甲公司完成回购。"
    history, current, history_ctx, current_ctx = _base_ctx_pair(history_text, current_text)
    bad_history_ctx = _pair_context(record_id=RECORD_ID_H, item_id="item-h", text=history_text,
                                    scope_id="other-scope", business_date="2026-09-26",
                                    arrival_seq=1, raw_hash="h-hash")
    alignment = _aligned_alignment(history, current, history_text, current_text)
    with pytest.raises(PairBindingError, match="(?i)scope"):
        pair_compare.compare_pair(
            bad_history_ctx, current_ctx,
            history_artifact=history, current_artifact=current,
            alignment=alignment,
            p15_results=_empty_p15(),
        )


def test_pair_binding_rejects_business_date_mismatch():
    """business_date 不同 → PairBindingError（跨日重复不处理）。"""
    history_text = "甲公司完成回购。"
    current_text = "甲公司完成回购。"
    history, current, history_ctx, current_ctx = _base_ctx_pair(history_text, current_text)
    bad_history_ctx = _pair_context(record_id=RECORD_ID_H, item_id="item-h", text=history_text,
                                    scope_id="default", business_date="2026-09-27",
                                    arrival_seq=1, raw_hash="h-hash")
    alignment = _aligned_alignment(history, current, history_text, current_text)
    with pytest.raises(PairBindingError, match="(?i)business_date|date"):
        pair_compare.compare_pair(
            bad_history_ctx, current_ctx,
            history_artifact=history, current_artifact=current,
            alignment=alignment,
            p15_results=_empty_p15(),
        )


def test_pair_binding_rejects_record_id_equal():
    """history.record_id == current.record_id → PairBindingError。"""
    history_text = "甲公司完成回购。"
    current_text = "甲公司完成回购。"
    history, current, _, _ = _base_ctx_pair(history_text, current_text)
    history_ctx = _pair_context(record_id=RECORD_ID_H, item_id="item-h", text=history_text,
                                scope_id="default", business_date="2026-09-26",
                                arrival_seq=1)
    current_ctx = _pair_context(record_id=RECORD_ID_H, item_id="item-c", text=current_text,
                                scope_id="default", business_date="2026-09-26",
                                arrival_seq=2)
    alignment = _aligned_alignment(history, current, history_text, current_text)
    with pytest.raises(PairBindingError, match="(?i)record_id|self|equal"):
        pair_compare.compare_pair(
            history_ctx, current_ctx,
            history_artifact=history, current_artifact=current,
            alignment=alignment,
            p15_results=_empty_p15(),
        )


def test_pair_binding_rejects_item_id_equal():
    """history.item_id == current.item_id → PairBindingError（同 ID 异文应冲突由 07/13 处理；这里属 P16 输入校验）。"""
    history_text = "甲公司完成回购。"
    current_text = "甲公司完成回购。"
    history, current, _, _ = _base_ctx_pair(history_text, current_text)
    history_ctx = _pair_context(record_id=RECORD_ID_H, item_id="same-item", text=history_text,
                                scope_id="default", business_date="2026-09-26",
                                arrival_seq=1)
    current_ctx = _pair_context(record_id=RECORD_ID_C, item_id="same-item", text=current_text,
                                scope_id="default", business_date="2026-09-26",
                                arrival_seq=2)
    alignment = _aligned_alignment(history, current, history_text, current_text)
    with pytest.raises(PairBindingError, match="(?i)item_id|same"):
        pair_compare.compare_pair(
            history_ctx, current_ctx,
            history_artifact=history, current_artifact=current,
            alignment=alignment,
            p15_results=_empty_p15(),
        )


# ---------- 03:00 验收 3：接 P15；未确认对齐不进入冲突 ----------

def test_unaligned_numeric_evidence_does_not_become_verified_conflict():
    """P15 未确认对齐的数值不进入 verified_conflicts。模拟：P15PairResults 中 verified_conflicts
    为空时，conflict 不可凭空成立。"""
    history_text = "甲公司完成回购。"
    current_text = "甲公司完成回购。"
    history = _make_report(history_text, [
        _make_fact(history_text, fact_id="f1", subject="甲公司", predicate="回购",
                   record_id=RECORD_ID_H),
    ])
    current = _make_report(current_text, [
        _make_fact(current_text, fact_id="f1", subject="甲公司", predicate="回购",
                   record_id=RECORD_ID_C),
    ])
    alignment = _aligned_alignment(history, current, history_text, current_text)
    history_ctx = _pair_context(record_id=RECORD_ID_H, item_id="item-h", text=history_text,
                                scope_id="default", business_date="2026-09-26",
                                arrival_seq=1)
    current_ctx = _pair_context(record_id=RECORD_ID_C, item_id="item-c", text=current_text,
                                scope_id="default", business_date="2026-09-26",
                                arrival_seq=2)
    p15 = P15PairResults(
        verified_conflicts=(),  # 无对齐的冲突
        time_pairs=(),
        equivalence_ready=True,
        text_proof="FACT_EQUIVALENT",
        uncovered_independent_relation=False,
        issues=(),
    )
    result = pair_compare.compare_pair(
        history_ctx, current_ctx,
        history_artifact=history, current_artifact=current,
        alignment=alignment,
        p15_results=p15,
    )
    assert result.outcome == "equivalent"
    assert result.code == "FACT_EQUIVALENT"
    assert result.verified_conflicts == ()


def test_time_pair_conflict_propagates_to_outcome_conflict():
    """P15 compare_time 返回 conflict（VERIFIED_CONFLICT）→ 整体 outcome=conflict。"""
    history_text = "甲公司完成回购。"
    current_text = "甲公司完成回购。"
    history = _make_report(history_text, [
        _make_fact(history_text, fact_id="f1", subject="甲公司", predicate="回购",
                   record_id=RECORD_ID_H),
    ])
    current = _make_report(current_text, [
        _make_fact(current_text, fact_id="f1", subject="甲公司", predicate="回购",
                   record_id=RECORD_ID_C),
    ])
    alignment = _aligned_alignment(history, current, history_text, current_text)
    history_ctx = _pair_context(record_id=RECORD_ID_H, item_id="item-h", text=history_text,
                                scope_id="default", business_date="2026-09-26",
                                arrival_seq=1)
    current_ctx = _pair_context(record_id=RECORD_ID_C, item_id="item-c", text=current_text,
                                scope_id="default", business_date="2026-09-26",
                                arrival_seq=2)
    p15 = P15PairResults(
        verified_conflicts=(),
        time_pairs=(("conflict", "VERIFIED_CONFLICT"),),
        equivalence_ready=False,
        text_proof=None,
        uncovered_independent_relation=False,
        issues=(),
    )
    result = pair_compare.compare_pair(
        history_ctx, current_ctx,
        history_artifact=history, current_artifact=current,
        alignment=alignment,
        p15_results=p15,
    )
    assert result.outcome == "conflict"
    assert result.code == "VERIFIED_CONFLICT"


def test_time_pair_unresolved_blocks_equivalence_per_09_11_3():
    """按 09 §11.3，时间 unresolved 进入 issues，存在 issues 时阻断 equivalence。
    维持此行为是因为时间关系未确定属关键未解，不是局部兼容。"""
    history_text = "甲公司完成回购。"
    current_text = "甲公司完成回购。"
    history = _make_report(history_text, [
        _make_fact(history_text, fact_id="f1", subject="甲公司", predicate="回购",
                   record_id=RECORD_ID_H),
    ])
    current = _make_report(current_text, [
        _make_fact(current_text, fact_id="f1", subject="甲公司", predicate="回购",
                   record_id=RECORD_ID_C),
    ])
    alignment = _aligned_alignment(history, current, history_text, current_text)
    history_ctx = _pair_context(record_id=RECORD_ID_H, item_id="item-h", text=history_text,
                                scope_id="default", business_date="2026-09-26",
                                arrival_seq=1)
    current_ctx = _pair_context(record_id=RECORD_ID_C, item_id="item-c", text=current_text,
                                scope_id="default", business_date="2026-09-26",
                                arrival_seq=2)
    p15 = P15PairResults(
        verified_conflicts=(),
        time_pairs=(("unresolved", "TIME_RELATION_UNCERTAIN"),),
        equivalence_ready=True,
        text_proof="FACT_EQUIVALENT",
        uncovered_independent_relation=False,
        issues=(),
    )
    result = pair_compare.compare_pair(
        history_ctx, current_ctx,
        history_artifact=history, current_artifact=current,
        alignment=alignment,
        p15_results=p15,
    )
    assert result.outcome == "unresolved"
    assert result.code == "TIME_RELATION_UNCERTAIN"


# ---------- 03:00 验收 4：受控内部码封闭 ----------

def test_internal_codes_are_three_disjoint_sets():
    """EQUIVALENT/CONFLICT/UNRESOLVED 三组码域必须两两不相交。"""
    eq = set(pair_compare.EQUIVALENT_CODES)
    cf = set(pair_compare.CONFLICT_CODES)
    un = set(pair_compare.UNRESOLVED_CODES)
    assert eq.isdisjoint(cf)
    assert eq.isdisjoint(un)
    assert cf.isdisjoint(un)


def test_public_detail_does_not_leak_internal_code():
    """公共 detail 不暴露内部码；detail 是人类可读语义原因。"""
    history_text = "甲公司完成回购。"
    current_text = "甲公司完成回购。"
    history = _make_report(history_text, [
        _make_fact(history_text, fact_id="f1", subject="甲公司", predicate="回购",
                   record_id=RECORD_ID_H),
    ])
    current = _make_report(current_text, [
        _make_fact(current_text, fact_id="f1", subject="甲公司", predicate="回购",
                   record_id=RECORD_ID_C),
    ])
    alignment = _aligned_alignment(history, current, history_text, current_text)
    history_ctx = _pair_context(record_id=RECORD_ID_H, item_id="item-h", text=history_text,
                                scope_id="default", business_date="2026-09-26",
                                arrival_seq=1)
    current_ctx = _pair_context(record_id=RECORD_ID_C, item_id="item-c", text=current_text,
                                scope_id="default", business_date="2026-09-26",
                                arrival_seq=2)
    result = pair_compare.compare_pair(
        history_ctx, current_ctx,
        history_artifact=history, current_artifact=current,
        alignment=alignment,
        p15_results=P15PairResults(
            verified_conflicts=(), time_pairs=(),
            equivalence_ready=False, text_proof=None,
            uncovered_independent_relation=True,
            issues=(PairIssue("RULE_UNCOVERED", "无共同核心事件"),),
        ),
    )
    assert result.outcome == "unresolved"
    assert "RULE_UNCOVERED" not in result.detail, \
        "公共 detail 不得泄露内部码"


# ---------- 09 §9.2 充分冲突独立成立 ----------

def test_sufficient_conflict_holds_despite_unrelated_slot_issues():
    """充分冲突（数值 100 vs 101）独立成立，不被无关槽位 issues 撤销。"""
    history_text = "甲公司公布该商品现价100元。"
    current_text = "甲公司公布该商品现价101元。"
    history = _make_report(history_text, [
        _make_fact(history_text, fact_id="f1", subject="甲公司", predicate="公布",
                   key_object="该商品", record_id=RECORD_ID_H),
    ])
    current = _make_report(current_text, [
        _make_fact(current_text, fact_id="f1", subject="甲公司", predicate="公布",
                   key_object="该商品", record_id=RECORD_ID_C),
    ])
    alignment = _aligned_alignment(history, current, history_text, current_text)
    history_ctx = _pair_context(record_id=RECORD_ID_H, item_id="item-h", text=history_text,
                                scope_id="default", business_date="2026-09-26",
                                arrival_seq=1)
    current_ctx = _pair_context(record_id=RECORD_ID_C, item_id="item-c", text=current_text,
                                scope_id="default", business_date="2026-09-26",
                                arrival_seq=2)
    verified = VerifiedConflict(
        field_path="facts.f1.numerics.n1.value",
        basis="NUMERIC_SAME_DECIMAL",
        history_evidence=EvidenceRef(RECORD_ID_H, "facts.f1.numerics.n1.value", "100元", 10, 14),
        current_evidence=EvidenceRef(RECORD_ID_C, "facts.f1.numerics.n1.value", "101元", 10, 14),
        detail="现价 100 元 vs 101 元",
    )
    p15 = P15PairResults(
        verified_conflicts=(verified,),
        time_pairs=(),
        equivalence_ready=False,
        text_proof=None,
        uncovered_independent_relation=False,
        issues=(PairIssue("FACT_INCOMPLETE", "无关槽位 issues 不得撤销充分冲突"),),
    )
    result = pair_compare.compare_pair(
        history_ctx, current_ctx,
        history_artifact=history, current_artifact=current,
        alignment=alignment,
        p15_results=p15,
    )
    assert result.outcome == "conflict"
    assert result.code == "VERIFIED_CONFLICT"