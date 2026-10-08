"""N29（D28 裁定选 a，诚实降级）专项：抽取 missing ≠ 原文确无。

【D31 路径A 修订声明】点位 2/3 的 verified 单侧缺失路由已被 D31 用户裁定②
（窗口I《方案一致性核验-09.md》N29 局部段=冲突→路径A"改实施就文档"）
 superseded：verified 单侧缺失 → compatible（09 §8.3 表首两行/§11.2
L1010-1011/§10.2/T10/T19/§12.2 L1340"合法缺失允许重复"本义），不再是
C-10/C-11 的 FACT_INCOMPLETE/TIME_RELATION_UNCERTAIN 路由。**N29 主体不动**：
unverified missing（抽取未命中≠原文确无）仍记诚实 unknown 进未决集。
本文件点位 2/3 用例已按 D31 重写（旧断言在 D31 后代码下必红，反之亦然，
红能力方向对调有案）；双向钉值增补见 tests/unit/test_n21_boundary_whitelist.py。

三点位钉值（挂账分流核对表 N29 行 + C-10/C-11 族扩，D31 修订后口径）：
1. p15_integration._spec_from_slot / _time_spec_from_slot：missing 槽不再把
   status=="missing" 洗成 verified_missing（"规则未命中"≠09 §7.2"原文确无"）；
   仅认抽取层显式 verified_missing=True。规则基线 missing → normalize 判
   invalid(FACT_INCOMPLETE) → 比较归 unresolved → 未决集。
2. C-10（D31 修订）：verified 单侧数值缺失 → compatible 不发 issue
   （09 §10.2/T10）；unverified 单侧 → 维持 FACT_INCOMPLETE 未决项。
3. C-11（D31 修订）：compare_time 单侧 verified 缺失 → compatible
   （09 §11.2 L1010-1011 回归）；unverified 单侧 → 维持 unresolved
   (FACT_INCOMPLETE)；双侧 verified 缺失 compatible 不动（09 §11.3 本义）。

族扩（外部审计 M-05/M-06，主窗口核验属实并入，同族诚实未知方向）：
4. M-06：compare_time 尾部 stage 同分支还原 09 L1019-1023 规格丢失的
   `absolute is None` 双前置——带绝对日期侧 vs 仅阶段词侧不可判 compatible。
5. M-05：_fraction_from_chinese 口语尾数守卫扩展至 bare 千/百+尾数形态
   （"一千二"/"一百二"）——宁可 ambiguous 不可静默错值（fail-closed）。

红能力纪律：标【红能力】的断言在旧代码下必红（逐条经旧码复跑验证，证据见
log/N29诚实降级实施报告.md §三；D31 修订点位见 log/N21白名单对齐实施报告.md）；
标【保留面】的断言钉死裁定不应误伤的合法路径，防过度矫正。
"""

from __future__ import annotations

from fractions import Fraction

from news_flash_dedup.compare import (
    aggregate as aggregate_module,
    p15_integration,
    pair_alignment,
    pair_compare,
)
from news_flash_dedup.compare.aggregate import CoverageStatus, FrozenRecallPlan
from news_flash_dedup.compare.value_time import (
    AlignmentProof,
    EvidenceRef,
    NumericSpec,
    TimeSpec,
    compare_time,
    normalize_numeric,
    normalize_time,
)
from news_flash_dedup.facts import FactValidationReport


RECORD_ID_H = "a" * 64
RECORD_ID_C = "b" * 64
ALIGNED = AlignmentProof(confirmed=True, basis="N29 测试对齐证明")


def _evidence(text, quote, field, *, record_id):
    start = text.index(quote)
    return {"record_id": record_id, "field": field, "quote": quote,
            "start": start, "end": start + len(quote)}


def _missing():
    return {"status": "missing", "raw_value": None, "evidence": []}


def _verified_missing():
    """真 P14"原文确无"槽（抽取层显式 verified_missing=True）。"""
    return {"status": "missing", "raw_value": None, "evidence": [],
            "verified_missing": True}


def _present(text, quote, field, *, record_id):
    return {"status": "present", "raw_value": quote,
            "evidence": [_evidence(text, quote, field, record_id=record_id)]}


def _make_fact(text, *, fact_id="f1", subject="甲公司", predicate="回购",
               record_id=RECORD_ID_H, time_slot=None, numerics=()):
    base = f"facts.{fact_id}"
    numeric_entries = []
    for idx, slot in enumerate(numerics, 1):
        nid = f"n{idx}"
        nbase = f"{base}.numerics.{nid}"
        if isinstance(slot, str):  # 便捷形：裸字符串=present value
            value_slot = _present(text, slot, nbase + ".value", record_id=record_id)
        else:                      # 完整槽 dict（如 _verified_missing()）
            value_slot = slot
        numeric_entries.append({
            "numeric_id": nid,
            "evidence": value_slot.get("evidence") or [],
            "metric": _missing(),
            "value": value_slot,
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
        "time": {"expression": time_slot if time_slot is not None else _missing(),
                 "stage": _missing(), "anchor": _missing()},
        "key_object": _missing(),
        "numerics": numeric_entries,
    }


def _make_report(text, facts, *, record_id):
    return FactValidationReport(
        record_id=record_id,
        validated_complete=bool(facts),
        extraction_status="complete" if facts else "failed",
        valid_fact_ids=tuple(f["fact_id"] for f in facts),
        numeric_inventory=(),
        issues=(),
        artifact={
            "schema_version": "1.0", "record_id": record_id,
            "offset_unit": "unicode_code_point",
            "extraction_status": "complete" if facts else "failed",
            "facts": facts, "unparsed_spans": [], "uncertainties": [],
        },
    )


def _ctx(*, record_id, item_id, text, arrival_seq):
    import hashlib

    return {"record_id": record_id, "item_id": item_id, "text": text,
            "raw_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "scope_id": "default", "business_date": "2026-09-28",
            "arrival_seq": arrival_seq, "pipeline_version": "dedup_v1"}


def _run(history_text, current_text, *, h_time=None, c_time=None,
         h_numerics=(), c_numerics=()):
    """build_aligned → extract_p15_results → compare_pair → aggregate 全链。"""
    history_facts = [_make_fact(history_text, record_id=RECORD_ID_H,
                                time_slot=h_time, numerics=h_numerics)]
    current_facts = [_make_fact(current_text, record_id=RECORD_ID_C,
                                time_slot=c_time, numerics=c_numerics)]
    history = _make_report(history_text, history_facts, record_id=RECORD_ID_H)
    current = _make_report(current_text, current_facts, record_id=RECORD_ID_C)
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


# ---------- 点位 1：missing 不洗 verified_missing（:74 / :116） ----------

def test_numeric_missing_slot_not_washed_verified():
    """【红能力·点位1a】_spec_from_slot：抽取 missing 槽 verified_missing 恒
    False（旧码 `or status == "missing"` 洗真 → 本断言旧码必红）；显式
    verified_missing=True 仍如实透传。normalize 层：未 verified 的 missing
    判 invalid(FACT_INCOMPLETE)——unknown 而非"确无"。"""
    text = "甲公司完成回购。"
    slot = _missing()
    spec = p15_integration._spec_from_slot(slot, text, RECORD_ID_H,
                                           "facts.f1.numerics.n1.value")
    assert spec.status == "missing"
    assert spec.verified_missing is False
    checked = normalize_numeric(text, RECORD_ID_H, spec)
    assert not checked.valid
    assert checked.issue_code == "FACT_INCOMPLETE"

    honest = p15_integration._spec_from_slot(_verified_missing(), text,
                                             RECORD_ID_H,
                                             "facts.f1.numerics.n1.value")
    assert honest.verified_missing is True


def test_time_missing_slot_not_washed_verified():
    """【红能力·点位1b】_time_spec_from_slot：同款洗白（旧码
    `verified_missing=status == "missing"`）废除；未 verified 的 missing
    时间槽 normalize_time 判 invalid(FACT_INCOMPLETE)。"""
    text = "甲公司完成回购。"
    spec = p15_integration._time_spec_from_slot(_missing(), text, RECORD_ID_H)
    assert spec.status == "missing"
    assert spec.verified_missing is False
    checked = normalize_time(text, RECORD_ID_H, spec)
    assert not checked.valid
    assert checked.issue_code == "FACT_INCOMPLETE"

    honest = p15_integration._time_spec_from_slot(_verified_missing(), text,
                                                  RECORD_ID_H)
    assert honest.verified_missing is True
    assert normalize_time(text, RECORD_ID_H, honest).valid


# ---------- 点位 2（C-10，D31 修订）：数值单侧缺失路由 ----------

def test_one_sided_numeric_verified_missing_compatible_allows_duplicate():
    """【红能力·点位2，D31 路径A 重写】history 数值槽 verified 确无 vs
    current 数值 present → compare_numeric 得 missing_in_history → p15 层
    消费为 compatible（不发 issue，09 §10.2/T10"缺值不是模型漏抽→重复，
    FACT_EQUIVALENT"）→ FACT_EQUIVALENT 签发 → 重复。D31 前 C-10 路由
    FACT_INCOMPLETE 未决 → 边界，本断言在 D31 前代码下必红；旧 C-10 断言
    （必须进未决集）在 D31 后代码下必红——红能力方向随裁定对调有案。"""
    pair, out, p15 = _run(
        "甲公司完成回购。", "甲公司回购100元。",
        h_numerics=(_verified_missing(),), c_numerics=("100",),
        h_time=_verified_missing(), c_time=_verified_missing(),
    )
    assert not p15.p15_results.verified_conflicts, "单侧缺失不得判已验证冲突"
    assert not [i for i in p15.p15_results.issues if "missing_in" in i.detail], (
        "D31：verified 单侧数值缺失 → compatible，不再发 FACT_INCOMPLETE issue"
    )
    assert pair.outcome == "equivalent"
    assert pair.code == "FACT_EQUIVALENT"
    assert out.decision == "重复"


def test_one_sided_numeric_unverified_missing_enters_pending_set():
    """【保留面·点位2】抽取未命中型单侧数值缺失（unverified，规则基线形态）
    → 维持 FACT_INCOMPLETE 具名 issue 进未决集（N29 主体不动：不静默吞、
    不直接判 conflict、不洗成"原文确无"）；D31 前后皆绿。"""
    pair, out, p15 = _run(
        "甲公司完成回购。", "甲公司回购100元。",
        h_numerics=(_missing(),), c_numerics=("100",),
        h_time=_verified_missing(), c_time=_verified_missing(),
    )
    assert not p15.p15_results.verified_conflicts, "单侧缺失不得判已验证冲突"
    pendings = [i for i in p15.p15_results.issues if i.code == "FACT_INCOMPLETE"]
    assert pendings, "unverified 单侧数值缺失必须维持 FACT_INCOMPLETE 未决项"
    assert pair.outcome == "unresolved"
    assert out.decision == "边界case/疑难case"


# ---------- 点位 3（C-11，D31 修订）：compare_time 单侧缺失路由 ----------

def test_one_sided_time_verified_missing_compatible_unverified_unknown():
    """【红能力·点位3，D31 路径A 重写】present vs verified 缺失 → compatible
    （双向）——09 §11.2 L1010-1011 伪代码明文回归（§8.3 表首"一方真正没有
    时间…允许重复"；T19 预期）。D31 前 C-11 判 unresolved(TIME_RELATION_
    UNCERTAIN)，本断言在 D31 前代码下必红。保留面：unverified 单侧 →
    unresolved(FACT_INCOMPLETE) 不动；双侧 verified 缺失 compatible 不动。"""
    text_h = "甲公司今日实施回购。"
    today = normalize_time(text_h, RECORD_ID_H, TimeSpec(
        status="present", raw_value="今日",
        evidence=EvidenceRef(RECORD_ID_H, "facts.f1.time.expression",
                             "今日", text_h.index("今日"), text_h.index("今日") + 2),
    ))
    absent = normalize_time("实施回购。", RECORD_ID_C, TimeSpec(
        status="missing", raw_value=None, verified_missing=True))
    unverified = normalize_time("实施回购。", RECORD_ID_C, TimeSpec(
        status="missing", raw_value=None))

    one_sided = compare_time(today, absent, alignment=ALIGNED)
    assert one_sided.relation == "compatible", "D31：verified 单侧缺失 → compatible"
    mirrored = compare_time(absent, today, alignment=ALIGNED)
    assert mirrored.relation == "compatible"

    # 【保留面】unverified（抽取未命中≠原文确无，N29 主体不动）→ 维持 unknown
    stay = compare_time(today, unverified, alignment=ALIGNED)
    assert stay.relation == "unresolved"
    assert stay.reason_code == "FACT_INCOMPLETE"

    # 【保留面】双侧 verified 确无 → compatible（无可比对象，非未知）
    absent_h = normalize_time("实施回购。", RECORD_ID_H, TimeSpec(
        status="missing", raw_value=None, verified_missing=True))
    assert compare_time(absent_h, absent, alignment=ALIGNED).relation == "compatible"


def test_one_sided_time_missing_integration_d31_split():
    """【红能力·点位3 集成面，D31 路径A 重写】双向钉值：
    unverified（抽取未命中）vs present → time_pairs 记 unresolved
    (FACT_INCOMPLETE) → 全链落边界（N29 主体不动，D31 前后皆绿）；
    verified 确无 vs present → compatible 不产 time_pairs → FACT_EQUIVALENT
    签发 → 重复（09 §8.3 表首/T19"不能因缺少日期边界"）——后半 D31 前
    代码（C-11 路由 TIME_RELATION_UNCERTAIN → 边界）下必红。"""
    current_text = "甲公司2025年9月26日完成回购。"
    pair, out, p15 = _run(
        "甲公司完成回购。", current_text,
        c_time=_present(current_text, "2025年9月26日",
                        "facts.f1.time.expression", record_id=RECORD_ID_C),
    )
    assert p15.p15_results.time_pairs, "unverified 单侧时间未知必须记入 time_pairs"
    assert all(t.outcome == "unresolved" for t in p15.p15_results.time_pairs)
    assert p15.p15_results.time_pairs[0].code == "FACT_INCOMPLETE"
    assert p15.p15_results.equivalence_ready is False
    assert pair.outcome == "unresolved"
    assert out.decision == "边界case/疑难case"

    pair2, out2, p15_2 = _run(
        "甲公司完成回购。", current_text,
        h_time=_verified_missing(),
        c_time=_present(current_text, "2025年9月26日",
                        "facts.f1.time.expression", record_id=RECORD_ID_C),
    )
    assert p15_2.p15_results.time_pairs == (), "D31：verified 单侧缺失不产未决信号"
    assert p15_2.p15_results.equivalence_ready is True
    assert p15_2.p15_results.text_proof == "FACT_EQUIVALENT"
    assert pair2.outcome == "equivalent"
    assert out2.decision == "重复"


# ---------- 点位 1+3 决策面：未知时间阻断释义级等价/证书路由 ----------

def test_unknown_time_blocks_fact_equivalent():
    """【红能力·点位1+3】释义对双侧时间均未命中（规则基线 missing）→
    诚实 unknown 进 time_pairs → FACT_EQUIVALENT 拒签发 → 边界（旧码双侧
    洗白 compatible → 签发 → 重复，必红）。"""
    pair, out, p15 = _run("甲公司今日宣布回购股份。", "甲公司公告称回购股份。")
    assert p15.p15_results.time_pairs, "双侧时间未知必须如实记录"
    assert p15.p15_results.equivalence_ready is False
    assert p15.p15_results.text_proof is None
    assert pair.outcome == "unresolved"
    assert out.decision == "边界case/疑难case"
    assert out.duplicate_ids == ()


def test_exact_text_with_unknown_time_records_signal():
    """【红能力·点位1+3 路由钉值】逐字全同但时间槽未命中：EXACT 证书在 P15
    层仍签发（equivalence_ready=True），但 time_pairs 如实记 unknown →
    compare_pair 冻结路由（issues 先于 equivalence）落边界。旧码 time_pairs
    空 + pair equivalent + 重复，必红。证书与未决信号的优先级裁定归
    pair_compare 属主窗口（本窗口封存不动），此处仅钉现状。"""
    pair, out, p15 = _run("甲公司完成回购。", "甲公司完成回购。")
    assert p15.p15_results.equivalence_ready is True
    assert p15.p15_results.text_proof == "EXACT_TEXT_MATCH"
    assert p15.p15_results.time_pairs, "时间未知必须如实记录（不再静默 compatible）"
    assert pair.outcome == "unresolved"
    assert pair.code == "FACT_INCOMPLETE"
    assert out.decision == "边界case/疑难case"


def test_verified_missing_both_sides_keeps_fact_equivalent():
    """【保留面】真 P14 显式标"确无"（verified_missing=True）的双侧时间槽
    → compatible 不产信号 → FACT_EQUIVALENT 签发路径不受 N29 误伤。"""
    pair, out, p15 = _run(
        "甲公司今日宣布回购股份。", "甲公司公告称回购股份。",
        h_time=_verified_missing(), c_time=_verified_missing(),
    )
    assert p15.p15_results.time_pairs == ()
    assert p15.p15_results.equivalence_ready is True
    assert p15.p15_results.text_proof == "FACT_EQUIVALENT"
    assert pair.outcome == "equivalent"
    assert out.decision == "重复"
    assert out.duplicate_ids == ("item-H",)


# ---------- M-06（外部审计新发现，主窗口核验属实并入）：stage 同分支缺 absolute 前置 ----------

def test_m06_absolute_date_vs_bare_stage_word_is_uncertain():
    """【红能力·M-06】一侧带绝对日期+阶段词、另一侧仅同阶段词（absolute=None）
    → 不得判 compatible：09 规格 L1019-1023 stage 分支本含
    `left.absolute is None and right.absolute is None` 前置，实装丢失——
    规格还原修复（fp 方向：带日期侧与仅阶段词侧的时间关系不可判）。"""
    text_h = "甲公司2026年9月22日收盘报价。"
    quote_date = "2026年9月22日"
    with_date_close = normalize_time(text_h, RECORD_ID_H, TimeSpec(
        status="present", raw_value=quote_date,
        evidence=EvidenceRef(RECORD_ID_H, "facts.f1.time.expression", quote_date,
                             text_h.index(quote_date),
                             text_h.index(quote_date) + len(quote_date)),
        stage="收盘",
        stage_evidence=EvidenceRef(RECORD_ID_H, "facts.f1.time.stage", "收盘",
                                   text_h.index("收盘"), text_h.index("收盘") + 2),
    ))
    text_c = "甲公司收盘报价。"
    bare_close = normalize_time(text_c, RECORD_ID_C, TimeSpec(
        status="present", raw_value="收盘",
        evidence=EvidenceRef(RECORD_ID_C, "facts.f1.time.expression", "收盘",
                             text_c.index("收盘"), text_c.index("收盘") + 2),
    ))
    assert with_date_close.valid and with_date_close.absolute is not None
    assert bare_close.valid and bare_close.absolute is None
    assert bare_close.stage == "close"

    result = compare_time(with_date_close, bare_close, alignment=ALIGNED)
    assert result.relation == "unresolved"
    assert result.reason_code == "TIME_RELATION_UNCERTAIN"
    mirrored = compare_time(bare_close, with_date_close, alignment=ALIGNED)
    assert mirrored.relation == "unresolved"
    assert mirrored.reason_code == "TIME_RELATION_UNCERTAIN"

    # 【保留面】双侧皆仅阶段词（absolute 均 None）且同阶段 → compatible 不动
    assert compare_time(bare_close, bare_close, alignment=ALIGNED).relation == "compatible"


# ---------- M-05（外部审计新发现，主窗口核验属实并入）：bare 千/百+尾数口语守卫 ----------

def test_m05_bare_hundreds_thousands_trailing_digit_fail_closed():
    """【红能力·M-05】"一千二"/"一百二"（无万亿后缀的 bare 千/百+尾数口语）
    不得静默解析成 1002/102——fail-closed 判 NUMERIC_ALIGNMENT_FAILED
    （宁可 ambiguous 不可静默错值）。保留面：规范形"一千二百"=1200、
    标准形"一千零二"=1002 不受影响；旧守卫"一万二"行为保持。"""
    def check(text, raw, field="facts.f1.numerics.n1.value"):
        start = text.index(raw)
        return normalize_numeric(text, RECORD_ID_H, NumericSpec(
            status="present", raw_value=raw,
            evidence=EvidenceRef(RECORD_ID_H, field, raw, start, start + len(raw)),
            unit="元", dimension="money",
        ))

    for raw in ("一千二", "一百二"):
        checked = check(f"金额{raw}元", raw)
        assert not checked.valid, f"{raw} 不得静默错值解析"
        assert checked.issue_code == "NUMERIC_ALIGNMENT_FAILED"

    canonical = check("金额一千二百元", "一千二百")
    assert canonical.valid and canonical.exact == Fraction(1200)
    standard = check("金额一千零二元", "一千零二")
    assert standard.valid and standard.exact == Fraction(1002)
    legacy_guard = check("金额一万二元", "一万二")
    assert not legacy_guard.valid
    assert legacy_guard.issue_code == "NUMERIC_ALIGNMENT_FAILED"


def test_m05_colloquial_pins_san_qian_wan_er_bai_wu():
    """【红绿钉·M-05 守卫覆盖证明，窗口 Z3】外部审计点位"三千万/二百五"中文
    口语钉值——src 零改动，钉现役守卫实测行为（探针证据
    log/temp/winz3-m05-probe.txt）：

    - 绿钉"三千万"：标准形（万后缀在位，非 bare 千+尾数），两道口语守卫
      均不命中，实测收 valid / exact=30000000——正确解析不得被守卫误伤。
    - 红钉"二百五"：bare 百+尾数口语（本义 250），命中 M-05 第二道守卫
      （[数][百][数]$），实测拒 NUMERIC_ALIGNMENT_FAILED——fail-closed
      正确覆盖（若放行将被静默错值为 205，属守卫设计防止的漏判形态，
      实测无漏判，无需上报）。
    - 保留面绿钉：标准形"二百五十"=250、"三千万零五"=30000005 不受影响。
    """
    def check(text, raw, field="facts.f1.numerics.n1.value"):
        start = text.index(raw)
        return normalize_numeric(text, RECORD_ID_H, NumericSpec(
            status="present", raw_value=raw,
            evidence=EvidenceRef(RECORD_ID_H, field, raw, start, start + len(raw)),
            unit="元", dimension="money",
        ))

    # 绿钉：标准形"三千万"实测收，exact=30,000,000
    accepted = check("成交三千万元", "三千万")
    assert accepted.valid, "标准形 三千万 不得被口语守卫误伤"
    assert accepted.exact == Fraction(30000000)

    # 红钉：bare 百+尾数口语"二百五"实测拒（fail-closed，防静默错值 205）
    rejected = check("报价二百五元", "二百五")
    assert not rejected.valid, "二百五 不得静默错值解析（守卫必须覆盖）"
    assert rejected.issue_code == "NUMERIC_ALIGNMENT_FAILED"

    # 保留面：标准形不受影响
    canonical = check("报价二百五十元", "二百五十")
    assert canonical.valid and canonical.exact == Fraction(250)
    standard = check("成交三千万零五元", "三千万零五")
    assert standard.valid and standard.exact == Fraction(30000005)
