"""B1 专项：P15 时间真路径 + 原文/无损等价证书（p15_integration 22:1x 实装）。

按 09 §11.3 + P09 certify_text_equality 已封原语（N29/D28 校正缺失语义；
D31 路径A 修订单侧 verified 缺失路由）：
- EXACT/LOSSLESS 文本证书 → equivalence_ready=True + text_proof（代码可验证）；
- 时间双侧"确无"（verified_missing）→ compatible（不追加未决信号）；
- 单侧 verified 缺失 → compatible（D31 用户裁定②路径A，09 §11.2
  L1010-1011/§8.3 表首/T19 回归；钉值见 test_n21_boundary_whitelist.py）；
- 时间实质冲突 → VERIFIED_CONFLICT；
- 抽取未命中型 missing = unknown（非"原文确无"）→ 未决集 → 边界
  （专项钉值 tests/unit/test_n29_honest_missing.py）；
- 证书双保险：工件不合格 / 无对齐 Fact 对 → 不发证书（宁严勿宽）。

红绿纪律：本文件随 B1 实装新增；已封套件 1179 复跑零回归为证。
"""

from __future__ import annotations

from news_flash_dedup.compare import (
    aggregate as aggregate_module,
    p15_integration,
    pair_alignment,
    pair_compare,
)
from news_flash_dedup.compare.aggregate import CoverageStatus, FrozenRecallPlan
from news_flash_dedup.compare.pair_compare import UNRESOLVED_CODES
from news_flash_dedup.facts import FactValidationReport


RECORD_ID_H = "a" * 64
RECORD_ID_C = "b" * 64


def _evidence(text, quote, field, *, record_id):
    start = text.index(quote)
    return {"record_id": record_id, "field": field, "quote": quote,
            "start": start, "end": start + len(quote)}


def _missing():
    return {"status": "missing", "raw_value": None, "evidence": []}


def _present(text, quote, field, *, record_id):
    return {"status": "present", "raw_value": quote,
            "evidence": [_evidence(text, quote, field, record_id=record_id)]}


def _time_present(text, date_str, *, record_id, fact_id="f1"):
    return {"status": "present", "raw_value": date_str,
            "evidence": [_evidence(text, date_str, f"facts.{fact_id}.time.expression",
                                   record_id=record_id)]}


def _time_verified_missing():
    """真 P14"原文确无"时间槽（N29/D28：抽取未命中型 missing 现记 unknown
    归未决集——本文件证书/签发机制的正向覆盖以合法"确无"槽维持；未命中
    语义的 honest-unknown 钉值归 tests/unit/test_n29_honest_missing.py）。"""
    return {"status": "missing", "raw_value": None, "evidence": [],
            "verified_missing": True}


def _make_fact(text, *, fact_id="f1", subject="甲公司", predicate="回购",
               record_id=RECORD_ID_H, time_slot=None, polarity=None,
               modality=None, numerics=()):
    base = f"facts.{fact_id}"
    if polarity is None:
        polarity = predicate
    modality_slot = (_present(text, modality, base + ".event_state.modality",
                              record_id=record_id) if modality else _missing())
    numeric_entries = []
    for idx, value in enumerate(numerics, 1):
        nid = f"n{idx}"
        nbase = f"{base}.numerics.{nid}"
        numeric_entries.append({
            "numeric_id": nid,
            "evidence": [_evidence(text, value, nbase, record_id=record_id)],
            "metric": _missing(),
            "value": _present(text, value, nbase + ".value", record_id=record_id),
            "range_end": _missing(),
            "magnitude": _missing(),
            "unit": _missing(),
            "currency": _missing(),
            "role": _missing(),
            "comparator": _missing(),
            "direction": _missing(),
            "time": _missing(),
        })
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
        "time": {"expression": time_slot if time_slot is not None else _missing(),
                 "stage": _missing(), "anchor": _missing()},
        "key_object": _missing(),
        "numerics": numeric_entries,
    }


def _make_report(text, facts, *, record_id, status=None):
    if status is None:
        status = "complete" if facts else "failed"
    return FactValidationReport(
        record_id=record_id,
        validated_complete=bool(facts) and status == "complete",
        extraction_status=status,
        valid_fact_ids=tuple(f["fact_id"] for f in facts),
        numeric_inventory=(),
        issues=(),
        artifact={
            "schema_version": "1.0", "record_id": record_id,
            "offset_unit": "unicode_code_point",
            "extraction_status": status,
            "facts": facts, "unparsed_spans": [], "uncertainties": [],
        },
    )


def _ctx(*, record_id, item_id, text, arrival_seq):
    import hashlib

    return {"record_id": record_id, "item_id": item_id, "text": text,
            "raw_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "scope_id": "default", "business_date": "2026-09-26",
            "arrival_seq": arrival_seq, "pipeline_version": "dedup_v1"}


def _run(history_text, current_text, *, h_time=None, c_time=None,
         h_subject="甲公司", c_subject="甲公司",
         h_predicate="回购", c_predicate="回购", c_status="complete",
         h_polarity=None, c_polarity=None, h_modality=None, c_modality=None,
         h_numerics=(), c_numerics=(), h_extra_facts=()):
    """build_aligned → extract_p15_results → compare_pair → aggregate 全链。"""
    history_facts = [_make_fact(history_text, subject=h_subject,
                                predicate=h_predicate, polarity=h_polarity,
                                modality=h_modality, numerics=h_numerics,
                                record_id=RECORD_ID_H, time_slot=h_time)]
    history_facts.extend(h_extra_facts)
    current_facts = [_make_fact(current_text, subject=c_subject,
                                predicate=c_predicate, polarity=c_polarity,
                                modality=c_modality, numerics=c_numerics,
                                record_id=RECORD_ID_C, time_slot=c_time)]
    history = _make_report(history_text, history_facts, record_id=RECORD_ID_H)
    current = _make_report(current_text, current_facts, record_id=RECORD_ID_C,
                           status=c_status)
    alignment = pair_alignment.build_aligned(
        history, current, history_text, current_text,
        dictionary_version="dict_v1", alignment_version="alignment_v1",
    )
    p15 = p15_integration.extract_p15_results(
        history, current, history_text, current_text, alignment,
    )
    history_ctx = _ctx(record_id=RECORD_ID_H, item_id="item-H",
                       text=history_text, arrival_seq=1)
    current_ctx = _ctx(record_id=RECORD_ID_C, item_id="item-C",
                       text=current_text, arrival_seq=2)
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


# ---------- 证书路径：EXACT / LOSSLESS / 差异 ----------

def test_exact_text_match_certifies_equivalent():
    """B1-1：原文逐字相等 → EXACT_TEXT_MATCH 证书 → equivalent → 重复。
    （N29 注：时间槽以真 P14"确无"verified 形态给出；抽取未命中型 missing
    现记 unknown → 未决集 → 边界，钉值见 test_n29_honest_missing。）"""
    pair, out, p15 = _run("甲公司完成回购。", "甲公司完成回购。",
                          h_time=_time_verified_missing(),
                          c_time=_time_verified_missing())
    assert p15.p15_results.equivalence_ready is True
    assert p15.p15_results.text_proof == "EXACT_TEXT_MATCH"
    assert p15.p15_results.time_pairs == ()          # 时间双侧合法缺失 → compatible
    assert pair.outcome == "equivalent"
    assert pair.code == "EXACT_TEXT_MATCH"
    assert out.decision == "重复"
    assert out.duplicate_ids == ("item-H",)


def test_lossless_crlf_match_certifies_equivalent():
    """B1-2：CRLF/LF 无损相等 → LOSSLESS_TEXT_MATCH 证书 → equivalent → 重复。
    （N29 注：同 B1-1，时间双侧"确无"verified 槽维持证书路径覆盖。）"""
    pair, out, p15 = _run("甲公司\r\n完成回购。", "甲公司\n完成回购。",
                          h_time=_time_verified_missing(),
                          c_time=_time_verified_missing())
    assert p15.p15_results.equivalence_ready is True
    assert p15.p15_results.text_proof == "LOSSLESS_TEXT_MATCH"
    assert pair.outcome == "equivalent"
    assert pair.code == "LOSSLESS_TEXT_MATCH"
    assert out.decision == "重复"


def test_differing_texts_no_certificate():
    """B1-3：文本相异（谓词不同）→ 无证书 → equivalence_ready=False → 边界。"""
    pair, out, p15 = _run("甲公司完成回购。", "甲公司完成增发。", c_predicate="增发")
    assert p15.p15_results.equivalence_ready is False
    assert p15.p15_results.text_proof is None
    assert pair.outcome == "unresolved"
    assert pair.code in UNRESOLVED_CODES
    assert out.decision != "重复"
    assert out.duplicate_ids == ()


# ---------- 时间真路径 ----------

def test_missing_time_both_sides_compatible():
    """B1-4（N29 重钉）：时间双侧"确无"（verified_missing）→ compatible，
    不追加 TIME_RELATION_UNCERTAIN（09 §11.3"允许合法缺失"本义保留面）。
    抽取未命中型 missing 不再享受本路径——记 unknown 进 time_pairs，
    钉值见 test_n29_honest_missing。"""
    _, _, p15 = _run("甲公司完成回购。", "甲公司完成回购。",
                     h_time=_time_verified_missing(),
                     c_time=_time_verified_missing())
    assert p15.p15_results.time_pairs == ()


def test_time_present_conflict_verified():
    """B1-5：双侧绝对日期不同 → 时间冲突 → VERIFIED_CONFLICT（不重复路径）。"""
    pair, out, p15 = _run(
        "甲公司2025年9月26日完成回购。", "甲公司2025年9月27日完成回购。",
        h_time=_time_present("甲公司2025年9月26日完成回购。", "2025年9月26日",
                             record_id=RECORD_ID_H),
        c_time=_time_present("甲公司2025年9月27日完成回购。", "2025年9月27日",
                             record_id=RECORD_ID_C),
    )
    conflicts = [t for t in p15.p15_results.time_pairs if t.outcome == "conflict"]
    assert conflicts, "时间冲突未进入 time_pairs"
    assert pair.outcome == "conflict"
    assert pair.code == "VERIFIED_CONFLICT"
    assert out.decision == "不重复"
    assert out.duplicate_ids == ()


def test_missing_vs_present_time_honest_unknown_boundary():
    """B1-6（N29/D28 重钉；D31 路径A 注记）：一侧时间**抽取未命中**
    （unverified）vs 一侧 present → 诚实 unknown（FACT_INCOMPLETE 未决信号
    进 time_pairs）→ FACT_EQUIVALENT 拒签发 → 边界。旧预期（"允许合法缺失"
    → compatible → 重复）系把"规则未命中"扩大解释为"原文确无"，即 N29 挂账
    的洗白链路，D28 裁定选 a 废除。真"确无"（verified_missing）单侧场景经
    D31 用户裁定②路径A 回归 09 §11.2 L1010-1011/§8.3 表首/T19 本义 →
    compatible（钉值见 test_n21_boundary_whitelist.py /
    test_n29_honest_missing.py 点位3）；双侧确无可比不动。"""
    pair, out, p15 = _run(
        "甲公司完成回购。", "甲公司2025年9月26日完成回购。",
        c_time=_time_present("甲公司2025年9月26日完成回购。", "2025年9月26日",
                             record_id=RECORD_ID_C),
    )
    assert p15.p15_results.time_pairs, "单侧时间未知必须进 time_pairs"
    assert p15.p15_results.equivalence_ready is False
    assert p15.p15_results.text_proof is None
    assert pair.outcome == "unresolved"
    assert out.decision != "重复"
    assert out.duplicate_ids == ()


# ---------- 证书双保险（宁严勿宽） ----------

def test_unqualified_artifact_blocks_certificate():
    """B1-7：一侧工件抽取非 complete（不合格）→ 即使文本全同也不发证书 → 边界。

    注：零 Fact 侧在 build_aligned 入口即抛 PairAlignmentError（纪律，已封），
    故本用例以 extraction_status="partial"（facts 在场但校验不完整）触发合格门。
    """
    pair, out, p15 = _run("甲公司完成回购。", "甲公司完成回购。", c_status="partial")
    assert p15.p15_results.equivalence_ready is False
    assert p15.p15_results.text_proof is None
    assert pair.outcome == "unresolved"
    assert out.decision != "重复"


def test_certificate_fires_without_aligned_pair():
    """B1-8（23:4x 按 fp 3139b94f 复盘校准）：EXACT/LOSSLESS 字节级证书不再
    要求 used_pairs——确定性抽取下全同文本的 facts 必然同一，对齐对不提供
    额外保证；used_pairs 要求仅保留给 FACT_EQUIVALENT 路径。本用例构造
    全同文本 + 手工不对称 facts（真抽取下不可达），证书仍应签发。"""
    pair, out, p15 = _run("甲公司完成回购。", "甲公司完成回购。",
                          h_subject="甲公司", c_subject="回购")
    assert pair.aligned_facts == ()
    assert p15.p15_results.equivalence_ready is True
    assert p15.p15_results.text_proof == "EXACT_TEXT_MATCH"
    assert out.decision == "重复"
    assert out.duplicate_ids == ("item-H",)


def test_fact_equivalent_still_requires_alignment():
    """B1-8b：无证书（文本相异）且零对齐 → FACT_EQUIVALENT 不签发 → 边界。"""
    pair, out, p15 = _run("甲公司完成回购。", "乙公司完成增发。",
                          h_subject="甲公司", c_subject="乙公司",
                          c_predicate="增发")
    assert pair.aligned_facts == ()
    assert p15.p15_results.equivalence_ready is False
    assert p15.p15_results.text_proof is None
    assert pair.outcome == "unresolved"
    assert out.decision != "重复"


def test_exact_certificate_with_fallback_facts():
    """B1-8c：全同文本 + fallback 型全 missing facts（无动词文本）→ EXACT
    证书签发（23:4x 校准回收的 62 对逐字重复场景）。"""
    def fallback_report(text, record_id):
        missing = {"status": "missing", "raw_value": None, "evidence": []}
        span = {"record_id": record_id, "field": "facts.f1",
                "quote": text, "start": 0, "end": len(text)}
        return _make_report(text, [{
            "fact_id": "f1", "evidence": [span],
            "fact_type": missing, "subject": missing,
            "event_state": {k: dict(missing) for k in
                            ("predicate", "polarity", "modality", "attribution")},
            "time": {k: dict(missing) for k in ("expression", "stage", "anchor")},
            "key_object": missing, "numerics": [],
        }], record_id=record_id)

    # 2026-10-10（收口包二③）：夹具去"今日"——原稿"今日天气晴朗无事件。"
    # 含相对时间词且无绝对锚，C14/T-3 生效宪法证书路自证闸必压（闸压即
    # 宪法本意）；本用例测量 fallback 型全 missing facts 的 EXACT 证书机制，
    # 去相对词保留原测量意图（无动词文本/全 missing/全同 三性质不变）。
    text = "窗外天气晴朗无事件。"
    history = fallback_report(text, RECORD_ID_H)
    current = fallback_report(text, RECORD_ID_C)
    alignment = pair_alignment.build_aligned(
        history, current, text, text,
        dictionary_version="dict_v1", alignment_version="alignment_v1")
    assert alignment.aligned_facts == ()           # subject/predicate missing 不可配
    p15 = p15_integration.extract_p15_results(history, current, text, text, alignment)
    assert p15.p15_results.equivalence_ready is True
    assert p15.p15_results.text_proof == "EXACT_TEXT_MATCH"


# ---------- B3：FACT_EQUIVALENT 保守签发（释义级等价） ----------

def test_b3_fact_equivalent_paraphrase():
    """B3-1：释义级同事件（主体/谓词逐字等、极性模态齐、数值无、时间双侧
    "确无"verified）→ FACT_EQUIVALENT 签发 → equivalent → 重复。
    （N29 注：签发壁正向覆盖以合法"确无"槽维持；未命中型 missing 阻签发
    的钉值见 test_n29_honest_missing。）"""
    pair, out, p15 = _run("甲公司今日宣布回购股份。", "甲公司公告称回购股份。",
                          h_time=_time_verified_missing(),
                          c_time=_time_verified_missing())
    assert p15.p15_results.equivalence_ready is True
    assert p15.p15_results.text_proof == "FACT_EQUIVALENT"
    assert pair.outcome == "equivalent"
    assert pair.code == "FACT_EQUIVALENT"
    assert out.decision == "重复"
    assert out.duplicate_ids == ("item-H",)


def test_b3_polarity_mismatch_blocks():
    """B3-2：极性不一致（回购 vs 未回购）→ 拒签发 → 边界（不同案保护）。"""
    pair, out, p15 = _run("甲公司完成回购。", "甲公司并未回购。",
                          c_polarity="并未回购")
    assert p15.p15_results.equivalence_ready is False
    assert pair.outcome == "unresolved"
    assert out.decision != "重复"
    assert out.duplicate_ids == ()


def test_b3_modality_asymmetry_blocks():
    """B3-3：模态不对称（拟 vs 无）→ 拒签发 → 边界。"""
    pair, out, p15 = _run("甲公司拟回购股份。", "甲公司回购股份。",
                          h_modality="拟")
    assert p15.p15_results.equivalence_ready is False
    assert pair.outcome == "unresolved"
    assert out.decision != "重复"


def test_b3_numeric_count_mismatch_blocks():
    """B3-4：数值数量不等（防 zip 截断吞单侧差异）→ 拒签发 → 边界。"""
    pair, out, p15 = _run("甲公司回购1000万元。", "甲公司回购股份。",
                          h_numerics=("1000",))
    assert p15.p15_results.equivalence_ready is False
    assert pair.outcome == "unresolved"
    assert out.decision != "重复"


def test_b3_leftover_fact_blocks():
    """B3-5：history 多一条未对齐 Fact（覆盖不全）→ 拒签发 → 边界。"""
    history_text = "甲公司完成回购。乙公司宣布增发。"
    extra = _make_fact(history_text, fact_id="f2", subject="乙公司",
                       predicate="增发", record_id=RECORD_ID_H)
    pair, out, p15 = _run(history_text, "甲公司完成回购。",
                          h_extra_facts=(extra,))
    assert p15.p15_results.equivalence_ready is False
    assert pair.outcome == "unresolved"
    assert out.decision != "重复"


def test_b3_equal_numerics_still_equivalent():
    """B3-6：双侧数值相等（真比较通过）→ 不阻塞 FACT_EQUIVALENT → 重复。
    （N29 注：时间双侧"确无"verified 槽维持等价路径；未命中型 missing
    现产未决信号阻等价，钉值见 test_n29_honest_missing。）"""
    pair, out, p15 = _run("甲公司回购1000万元。", "甲公司回购1000万元。",
                          h_numerics=("1000",), c_numerics=("1000",),
                          h_time=_time_verified_missing(),
                          c_time=_time_verified_missing())
    # 文本全同 → 文本证书优先（EXACT）；数值真比较不制造 issue/冲突
    assert p15.p15_results.equivalence_ready is True
    assert not p15.p15_results.verified_conflicts
    assert pair.outcome == "equivalent"
    assert out.decision == "重复"


def test_b3_numeric_conflict_preempts_equivalence():
    """B3-7：双侧数值冲突（1000 vs 1200）→ VERIFIED_CONFLICT 先于等价 → 不重复。"""
    pair, out, p15 = _run("甲公司回购1000万元。", "甲公司回购1200万元。",
                          h_numerics=("1000",), c_numerics=("1200",))
    assert pair.outcome == "conflict"
    assert pair.code == "VERIFIED_CONFLICT"
    assert out.decision == "不重复"
    assert out.duplicate_ids == ()
