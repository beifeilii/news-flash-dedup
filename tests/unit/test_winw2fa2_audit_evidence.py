# -*- coding: utf-8 -*-
"""W2Fα2 红测（decide/audit.py 同域两项，先红后绿）：

条目 4（WA1b-M1，中危）：审计证据空心壳——build_audit_record 的
history_evidence/current_evidence 恒 quote:""/start:0/end:0 空壳入 ES 审计档
（第三方 L44 族申报残余）。修法=从 PairResult 实胜证据填充 quote/start/end。
  - 实胜优先：verified_conflicts 双侧证据（签发基底）先于 aligned 证据；
  - 来源正确：quote/start/end 逐值等于 PairResult.used_evidence 中对应侧证据；
  - 无证据对（used_evidence 空）保持诚实空壳（不伪造）。

条目 46（WB4 F-2，高危契约硬冲突）：comparison_id 三层两口径——
audit.py:115 "history:current" 裸拼接 vs 契约 10 §3 L48
`comparison_id = SHA256(JCS([query_record_id, candidate_record_id, pipeline_version]))`
vs api/contract.py validate_result L258-261/267 期望（query=current 当前新稿、
candidate=history 历史候选，方向见 10 §3 L55 明文）。修法=对齐规格公式。
（09-29 W3F W3b-F1 锚勘正：原注"L43/L48"系四级载体连锁错锚——10 L43
实为 record_id 公式行、公式实在 L48、方向句实在 L55；仅注释级勘正，
断言零触碰。）
"""
from __future__ import annotations

import hashlib
import json

from news_flash_dedup.compare.pair_alignment import AlignedPair, FactPointer
from news_flash_dedup.compare.pair_compare import PairResult, VerifiedConflict
from news_flash_dedup.compare.value_time import EvidenceRef
from news_flash_dedup.decide import audit as audit_module

H_ID = "h" * 64
C_ID = "c" * 64
PV = "dedup_v1"

H_EV = EvidenceRef(record_id=H_ID, field="facts.f1.numerics.n1.value",
                   quote="100元", start=10, end=14)
C_EV = EvidenceRef(record_id=C_ID, field="facts.f1.numerics.n1.value",
                   quote="101元", start=10, end=14)
H_EV_ALIGNED = EvidenceRef(record_id=H_ID, field="facts.f1.subject",
                           quote="甲公司", start=0, end=3)
C_EV_ALIGNED = EvidenceRef(record_id=C_ID, field="facts.f1.subject",
                           quote="甲公司", start=0, end=3)


def _jcs_sha256(value) -> str:
    """契约 10 §3 口径（与 api/contract.py:258-261 现役 JCS 习语逐字同形）。"""
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


def _pair(*, outcome="conflict", code="VERIFIED_CONFLICT",
          aligned_facts=(), verified_conflicts=(), used_evidence=()) -> PairResult:
    return PairResult(
        pair_id=f"{H_ID}|{C_ID}",
        history_record_id=H_ID, current_record_id=C_ID,
        history_item_id="item-h", current_item_id="item-c",
        history_arrival_seq=1, current_arrival_seq=2,
        history_raw_hash="hh", current_raw_hash="cc",
        pipeline_version=PV, outcome=outcome, code=code, detail="probe",
        aligned_facts=aligned_facts, verified_conflicts=verified_conflicts,
        unresolved_fields=(), used_evidence=used_evidence, budget_at=0,
    )


def _conflict_pair() -> PairResult:
    conflict = VerifiedConflict(field_path="facts.f1.numerics.n1.value",
                                basis="NUMERIC_SAME_DECIMAL",
                                history_evidence=H_EV, current_evidence=C_EV,
                                detail="100元 vs 101元")
    return _pair(outcome="conflict", code="VERIFIED_CONFLICT",
                 verified_conflicts=(conflict,),
                 used_evidence=(H_EV, C_EV))


def _aligned_pair() -> PairResult:
    aligned = AlignedPair(
        history=FactPointer(record_id=H_ID, fact_id="f1",
                            slot_path="facts.f1", text="t", evidence=H_EV_ALIGNED),
        current=FactPointer(record_id=C_ID, fact_id="f1",
                            slot_path="facts.f1", text="t", evidence=C_EV_ALIGNED),
        basis="EXACT_TEXT_MATCH")
    return _pair(outcome="equivalent", code="FACT_EQUIVALENT",
                 aligned_facts=(aligned,),
                 used_evidence=(H_EV_ALIGNED, C_EV_ALIGNED))


# ---------------------------------------------------------------- 条目 4：证据实填

def test_m1_conflict_pair_evidence_filled_from_winning_evidence():
    """条目4 主红测：conflict 对 → 双侧 quote/start/end 实填自实胜冲突证据。"""
    record = audit_module.build_audit_record(_conflict_pair())
    assert record.history_evidence["quote"] == "100元"
    assert record.history_evidence["start"] == 10
    assert record.history_evidence["end"] == 14
    assert record.history_evidence["record_id"] == H_ID
    assert record.current_evidence["quote"] == "101元"
    assert record.current_evidence["start"] == 10
    assert record.current_evidence["end"] == 14
    assert record.current_evidence["record_id"] == C_ID


def test_m1_evidence_source_is_pair_used_evidence():
    """条目4 来源钉：填充值逐值等于 PairResult.used_evidence 对应侧成员。"""
    pair = _conflict_pair()
    record = audit_module.build_audit_record(pair)
    by_rid = {ev.record_id: ev for ev in pair.used_evidence}
    for side, rid in (("history_evidence", H_ID), ("current_evidence", C_ID)):
        filled = getattr(record, side)
        src = by_rid[rid]
        assert (filled["quote"], filled["start"], filled["end"]) == \
               (src.quote, src.start, src.end)


def test_m1_equivalent_pair_evidence_filled_from_aligned_facts():
    """条目4 同族钉：equivalent 对（无冲突）→ 实填自对齐事实证据。"""
    record = audit_module.build_audit_record(_aligned_pair())
    assert record.history_evidence["quote"] == "甲公司"
    assert (record.history_evidence["start"], record.history_evidence["end"]) == (0, 3)
    assert record.current_evidence["quote"] == "甲公司"


def test_m1_winning_conflict_beats_aligned_when_both_present():
    """条目4 实胜优先钉：对齐+冲突同时在场 → 取冲突证据（签发基底）。"""
    pair_aligned = _aligned_pair()
    conflict = VerifiedConflict(field_path="facts.f1.numerics.n1.value",
                                basis="NUMERIC_SAME_DECIMAL",
                                history_evidence=H_EV, current_evidence=C_EV,
                                detail="100元 vs 101元")
    pair = _pair(outcome="conflict", code="VERIFIED_CONFLICT",
                 aligned_facts=pair_aligned.aligned_facts,
                 verified_conflicts=(conflict,),
                 used_evidence=(H_EV_ALIGNED, C_EV_ALIGNED, H_EV, C_EV))
    record = audit_module.build_audit_record(pair)
    assert record.history_evidence["quote"] == "100元"      # 非对齐的 "甲公司"
    assert record.current_evidence["quote"] == "101元"


def test_m1_empty_used_evidence_stays_honest_hollow():
    """条目4 诚实兜底（前后均绿守卫）：无证据对不伪造，保持空壳。"""
    record = audit_module.build_audit_record(
        _pair(outcome="unresolved", code="FACT_INCOMPLETE", used_evidence=()))
    assert record.history_evidence["quote"] == ""
    assert record.history_evidence["start"] == 0
    assert record.history_evidence["end"] == 0
    assert record.current_evidence["quote"] == ""


# ---------------------------------------------------------------- 条目 46：comparison_id 契约公式

def test_f2_comparison_id_matches_contract_formula_verbatim():
    """条目46 主红测：comparison_id = SHA256(JCS([query=current,
    candidate=history, pipeline_version]))（10 §3 L48 逐字——09-29 W3F
    W3b-F1 锚勘正：原注"L43"错锚，L43 实为 record_id 公式行）。"""
    record = audit_module.build_audit_record(_conflict_pair())
    expected = _jcs_sha256([C_ID, H_ID, PV])
    assert record.comparison_id == expected


def test_f2_comparison_id_direction_query_is_current():
    """条目46 方向钉：query_record_id=当前新稿（10 §3 L55 明文义：
    query 为当前新稿——09-29 W3F W3b-F1 勘正：原注"L48 'B为当前新稿'"
    锚误且系字面编造引语，10 号文无此文，述不引）；
    反方向（history 在前）必须得出不同 ID（防方向洗回）。"""
    record = audit_module.build_audit_record(_conflict_pair())
    forward = _jcs_sha256([C_ID, H_ID, PV])
    reverse = _jcs_sha256([H_ID, C_ID, PV])
    assert forward != reverse
    assert record.comparison_id == forward
    assert record.comparison_id != reverse


def test_f2_comparison_id_aligns_with_validate_result_expectation():
    """条目46 对拍钉：与 api/contract.py validate_result L258-261 的
    expected_audit_id 计算逐字同式（query=current.record_id,
    candidate=audit.record_id=历史成员, pipeline_version）。"""
    pair = _conflict_pair()
    record = audit_module.build_audit_record(pair)
    # 镜像 api/contract.py:258-261 现役表达式（current=当前新稿侧）
    expected_audit_id = hashlib.sha256(json.dumps(
        [pair.current_record_id, pair.history_record_id, pair.pipeline_version],
        ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    assert record.comparison_id == expected_audit_id


def test_f2_comparison_id_format_64_lower_hex_and_deterministic():
    """条目46 形态钉：64 位小写 hex（12 § 口径），两次构造同一。"""
    r1 = audit_module.build_audit_record(_conflict_pair())
    r2 = audit_module.build_audit_record(_conflict_pair())
    assert len(r1.comparison_id) == 64
    assert all(ch in "0123456789abcdef" for ch in r1.comparison_id)
    assert r1.comparison_id == r2.comparison_id


def test_f2_pipeline_version_binds_comparison_id():
    """条目46 版本钉：pipeline_version 进公式——异版本异 ID。"""
    pair = _conflict_pair()
    other = PairResult(**{**pair.__dict__, "pipeline_version": "dedup_v2"})
    assert audit_module.build_audit_record(pair).comparison_id != \
           audit_module.build_audit_record(other).comparison_id


def test_f2_comparison_id_no_longer_bare_concat():
    """条目46 违规形态钉：不得再是 "history:current" 裸拼接。"""
    record = audit_module.build_audit_record(_conflict_pair())
    assert record.comparison_id != f"{H_ID}:{C_ID}"
    assert ":" not in record.comparison_id
