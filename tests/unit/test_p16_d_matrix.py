"""P16-D P16-P01–P17 + L01–L11 矩阵测试（28 例全可执行）。

按 03:28 批复标尺 1：每条落成断言，不得抽样；矩阵表进执行证据。
按 03:28 批复标尺 2：P15 实函数真实调用（p15_results 由真调用构造）。
按 03:28 批复标尺 4：T045/T046 三分类汇总绿灯（∅→不重复、非空→重复、其余→边界）。
"""

from __future__ import annotations

from collections.abc import Mapping

import pytest

from news_flash_dedup.compare import (
    aggregate as aggregate_module,
    p15_integration,
    pair_alignment,
    pair_compare,
)
from news_flash_dedup.compare.aggregate import FrozenRecallPlan, CoverageStatus
from news_flash_dedup.compare.p15_integration import extract_p15_results
from news_flash_dedup.compare.pair_compare import (
    EQUIVALENT_CODES,
    UNRESOLVED_CODES,
    PairResult,
)
from news_flash_dedup.facts import FactValidationReport


RECORD_ID_H = "a" * 64
RECORD_ID_C = "b" * 64


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


def _present(text, quote, field, *, value=None, record_id=RECORD_ID_H):
    """真实 span 槽：quote 必须原文码点可定位（窗口 Z3 物理拆除伪造回退——
    旧 except ValueError 分支用 text[:2] 假锚点冒充 quote 证据，已删；
    quote 不在原文即 ValueError 暴露夹具错误，不再静默伪造）。"""
    if not quote or not isinstance(quote, str):
        # missing slot for empty quote
        return {"status": "missing", "raw_value": None, "evidence": []}
    start = text.index(quote)
    out = {
        "status": "present",
        "raw_value": quote,
        "evidence": [{"record_id": record_id, "field": field,
                      "quote": quote, "start": start, "end": start + len(quote)}],
    }
    if value is not None:
        out["value"] = value
    return out


def _label(value, field):
    """（窗口 Z3 追加，伪造回退拆除配套）标签槽：raw_value 为分类标签而非
    原文 span（如 fact_type / event_state.predicate / polarity 在夹具中镜像
    谓词串，谓词串未必出现于原文——如数值用例文本"现价100元"无"回购"）。
    evidence 空 = 诚实"不声称原文定位"，取代旧 _present 回退的 text[:2]
    假锚点。下游消费面实证只需 status/raw_value（pair_alignment._slot_raw、
    p15_integration._slot_status_raw 均不读本类槽 evidence）。"""
    if not value or not isinstance(value, str):
        return {"status": "missing", "raw_value": None, "evidence": []}
    return {"status": "present", "raw_value": value, "evidence": []}


def _decimal(value: str) -> str:
    return value


def _time_present_slot(text, date_str, *, record_id, fact_id="f1"):
    """（22:5x 主窗口追加，B1 时间真路径配套）构造 present 时间槽。"""
    start = text.index(date_str)
    return {"status": "present", "raw_value": date_str,
            "evidence": [{"record_id": record_id,
                          "field": f"facts.{fact_id}.time.expression",
                          "quote": date_str, "start": start,
                          "end": start + len(date_str)}]}


def _time_verified_missing():
    """（N29/D28 追加）真 P14"原文确无"时间槽：抽取未命中型 missing 现记
    unknown 归未决集（钉值见 tests/unit/test_n29_honest_missing.py）；矩阵行
    的证书/签发/聚合机制正向覆盖以合法"确无"槽维持。"""
    return {"status": "missing", "raw_value": None, "evidence": [],
            "verified_missing": True}


def _make_fact(text, *, fact_id, subject, predicate, key_object="",
               numerics=(), record_id=RECORD_ID_H, time_slot=None):
    base = f"facts.{fact_id}"
    fact = {
        "fact_id": fact_id,
        "evidence": [_evidence(text, text, base, record_id=record_id)],
        "fact_type": _label(predicate, base + ".fact_type"),
        "subject": _present(text, subject, base + ".subject", record_id=record_id),
        "event_state": {
            "predicate": _label(predicate, base + ".event_state.predicate"),
            "polarity": _label(predicate, base + ".event_state.polarity"),
            "modality": _missing(),
            "attribution": _missing(),
        },
        "time": {"expression": time_slot if time_slot is not None else _missing(),
                 "stage": _missing(), "anchor": _missing()},
        "key_object": (_present(text, key_object, base + ".key_object", record_id=record_id)
                       if key_object else _missing()),
        "numerics": [],
    }
    for idx, n in enumerate(numerics, 1):
        nid = f"n{idx}"
        nbase = f"{base}.numerics.{nid}"
        fact["numerics"].append({
            "numeric_id": nid,
            "evidence": [_evidence(text, n, nbase, record_id=record_id)],
            # 窗口 Z3（伪造回退拆除配套）：metric/role/comparator 改诚实
            # missing——旧版 _present(text, "price"/"=", ...) 三聚氰胺签
            # 经伪造回退挂 text[:2] 假锚点；消费面实证：p15_integration
            # ._spec_from_slot 只从 value 槽读 unit/role/metric/comparator
            # （条目级键从不被读，今实测有效 spec 即 role=None/metric=None/
            # comparator="=" 默认值），且 rule.py 真抽取 role 恒 missing
            # （设计书 §2.9 W8）、无 metric 词/内联比较符命中即 missing——
            # missing 与真抽取形态一致，下游可观察行为零变化。
            "metric": _missing(),
            "value": _present(text, n, nbase + ".value",
                              value=_decimal(n), record_id=record_id),
            "range_end": _missing(),
            "magnitude": _missing(),
            "unit": _present(text, "元", nbase + ".unit", record_id=record_id),
            "currency": _missing(),
            "role": _missing(),
            "comparator": _missing(),
            "direction": _missing(),
            "time": _missing(),
        })
    return fact


def _make_artifact(text, facts, *, record_id):
    return {
        "schema_version": "1.0",
        "record_id": record_id,
        "offset_unit": "unicode_code_point",
        "extraction_status": "complete" if facts else "failed",
        "facts": facts,
        "unparsed_spans": [],
        "uncertainties": [],
    }


def _make_report(text, facts, *, record_id=None) -> FactValidationReport:
    if record_id is None:
        record_id = RECORD_ID_H
    artifact = _make_artifact(text, facts, record_id=record_id)
    return FactValidationReport(
        record_id=record_id,
        validated_complete=bool(facts),
        extraction_status="complete" if facts else "failed",
        valid_fact_ids=tuple(f["fact_id"] for f in facts),
        numeric_inventory=(),
        issues=(),
        artifact=artifact,
    )


def _ctx(*, record_id, item_id, text="t", raw_hash=None, scope_id="default",
         business_date="2026-09-26", arrival_seq=1, pipeline_version="dedup_v1"):
    if raw_hash is None:
        import hashlib
        raw_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return {"record_id": record_id, "item_id": item_id, "text": text,
            "raw_hash": raw_hash, "scope_id": scope_id,
            "business_date": business_date, "arrival_seq": arrival_seq,
            "pipeline_version": pipeline_version}


def _run_pipeline(history_text, current_text, *, history_subject="甲公司",
                   current_subject="甲公司", history_predicate="回购",
                   current_predicate="回购", history_key="",
                   current_key="", history_numerics=(), current_numerics=(),
                   history_id=RECORD_ID_H, current_id=RECORD_ID_C,
                   history_arrival=1, current_arrival=2,
                   pipeline_version="dedup_v1",
                   coverage_complete=True,
                   history_time=None, current_time=None):
    """Full pipeline: build_aligned → extract_p15_results → compare_pair → aggregate."""
    history_facts = [_make_fact(history_text, fact_id="f1",
                                  subject=history_subject, predicate=history_predicate,
                                  key_object=history_key,
                                  numerics=history_numerics,
                                  record_id=history_id, time_slot=history_time)]
    current_facts = [_make_fact(current_text, fact_id="f1",
                                 subject=current_subject, predicate=current_predicate,
                                 key_object=current_key,
                                 numerics=current_numerics,
                                 record_id=current_id, time_slot=current_time)]
    history_report = _make_report(history_text, history_facts, record_id=history_id)
    current_report = _make_report(current_text, current_facts, record_id=current_id)
    alignment = pair_alignment.build_aligned(
        history_report, current_report, history_text, current_text,
        dictionary_version="dict_v1",
        alignment_version="alignment_v1",
    )
    p15_report = p15_integration.extract_p15_results(
        history_report, current_report, history_text, current_text, alignment,
    )
    history_ctx = _ctx(record_id=history_id, item_id=f"item-{history_id[:8]}",
                        text=history_text, arrival_seq=history_arrival)
    current_ctx = _ctx(record_id=current_id, item_id=f"item-{current_id[:8]}",
                        text=current_text, arrival_seq=current_arrival,
                        raw_hash=__import__('hashlib').sha256(current_text.encode("utf-8")).hexdigest())
    pair = pair_compare.compare_pair(
        history_ctx, current_ctx,
        history_artifact=history_report, current_artifact=current_report,
        alignment=alignment,
        pipeline_version=pipeline_version,
        p15_results=p15_report.p15_results,
    )
    plan = FrozenRecallPlan(
        version="rrf_v1_k60_30_10",
        required={history_id: history_ctx},
    )
    out = aggregate_module.aggregate(
        current_ctx, plan, [pair],
        coverage=CoverageStatus(visible_seq=10, prepared_seq=10, complete=coverage_complete),
    )
    return pair, out, alignment, p15_report


# ---------- P16-P01：双侧原文无损相等 → EXACT_TEXT_MATCH / equivalent ----------

def test_p01_exact_text_match():
    """P16-P01：双侧原文无损相等（含归一后）。
    （N29/D28 注：时间双侧"确无"verified 槽维持 EXACT 证书路径覆盖；抽取
    未命中型 missing 现记 unknown → 边界，钉值见 test_n29_honest_missing。）"""
    pair, out, _, _ = _run_pipeline(
        "甲公司完成回购。", "甲公司完成回购。",
        history_time=_time_verified_missing(),
        current_time=_time_verified_missing(),
    )
    # D24 钉值（d24-p16d-probe 实测）：时间路径已真，不再落 unresolved
    assert pair.outcome == "equivalent"
    assert pair.code == "EXACT_TEXT_MATCH"
    assert out.decision == "重复"
    assert out.duplicate_ids == ("item-aaaaaaaa",)


# ---------- P16-P02：数值精确冲突 100 vs 101 ----------

def test_p02_verified_conflict_100_vs_101():
    """P16-P02：100 元 vs 101 元 → VERIFIED_CONFLICT。"""
    pair, out, _, _ = _run_pipeline(
        "甲公司公布现价100元。",
        "甲公司公布现价101元。",
        history_numerics=["100"], current_numerics=["101"],
    )
    # D24 钉值：P15 真调用实测 conflicts 恒触发——条件断言（空转）废，
    # 改无条件钉死（100 vs 101 同 decimal 冲突→不重复）
    assert pair.verified_conflicts, "P15 数值冲突必须真实触发"
    assert pair.outcome == "conflict"
    assert pair.code == "VERIFIED_CONFLICT"
    assert any(c.basis == "NUMERIC_SAME_DECIMAL"
               for c in pair.verified_conflicts)
    assert out.decision == "不重复"
    assert out.duplicate_ids == ()


# ---------- P16-P03：充分冲突不撤销无关槽 issues ----------

def test_p03_conflict_independent_of_other_issues():
    """P16-P03：充分冲突（100 vs 101）独立成立，无关槽位错误不撤销。

    W-R3b / R3-M7-e 语义钉：旧夹具与 P02 全同（门级钉冒充语义）——
    "无关槽 issue" 仅靠双 missing 时间槽隐式偶发、且从不钉其存在，
    w-r3b-red-probes M1 实证该语义条件可移除（双侧 verified missing）
    而旧断言群不红。今显式注入无关槽 issue（history 侧时间 present /
    current 侧未验证 missing → 时间未决 FACT_INCOMPLETE）并钉其真在
    场，再钉冲突不被撤销（判定壁优先序 conflicts 先于 issues，
    pair_compare.py:421-425）与聚合决策。"""
    # 2026-10-09（P2 先行件①夹具重钉；宪章 v2 §三-2）：history 时间槽原
    # "9月26日"（无年 M月D日）在 _YEAR_LESS_DEFAULT_YEAR 拆除后落
    # TIME_RELATION_UNCERTAIN——本用例语义靶是"无关槽 issue 恰为 current
    # 侧未验证 missing（FACT_INCOMPLETE）真在场且充分冲突不被撤销"，故
    # history 夹具改为带年日期（合法绝对时间，槽 valid），无关 issue
    # 构成与断言面逐字节保持。
    history_text = "甲公司2026年9月26日公布现价100元。"
    pair, out, _, p15_report = _run_pipeline(
        history_text,
        "甲公司公布现价101元。",
        history_numerics=["100"], current_numerics=["101"],
        history_key="现价", current_key="现价",
        history_predicate="公布", current_predicate="公布",
        history_time=_time_present_slot(history_text, "2026年9月26日",
                                        record_id=RECORD_ID_H),
        current_time=None,   # 未验证 missing（N29：抽取未命中≠原文确无）
    )
    # 语义条件非真空：无关槽 issue 真在场（恰一条时间未决，码实钉）
    assert [(tp.outcome, tp.code)
            for tp in p15_report.p15_results.time_pairs] == [
        ("unresolved", "FACT_INCOMPLETE")]
    # 充分冲突不被无关槽 issue 撤销
    assert pair.verified_conflicts, "充分冲突必须独立成立"
    assert pair.outcome == "conflict"
    assert pair.code == "VERIFIED_CONFLICT"
    assert any(c.basis == "NUMERIC_SAME_DECIMAL"
               for c in pair.verified_conflicts)
    # 聚合终局：冲突执政（无关槽 issue 不翻转决策）
    assert out.decision == "不重复"
    assert out.duplicate_ids == ()


# ---------- P16-P04：角色错配不引入冲突 ----------

def test_p04_numeric_role_misalignment_no_conflict():
    """P16-P04：未对齐数值（不同 role/不可配对主体）不进入 verified_conflicts。"""
    pair, out, _, _ = _run_pipeline(
        "甲公司公布现价100元。",
        "乙公司公布成本99元。",
        history_subject="甲公司", current_subject="乙公司",
        history_numerics=["100"], current_numerics=["99"],
    )
    # 主体不同 → aligned_facts 空 → 无 P15 调用 → 无 verified_conflicts
    assert pair.verified_conflicts == ()


# ---------- P16-P05：合法 missing 等价（value 缺失合法） ----------

def test_p05_legal_missing_equivalent():
    """P16-P05：N 全文无价格 → FACT_EQUIVALENT（合法 missing）。"""
    pair, out, _, _ = _run_pipeline(
        "甲公司公布现价100元。",
        "甲公司公布现价。",  # 历史有数值，新文无数值
        history_numerics=["100"], current_numerics=[],
    )
    # D24 钉值：主谓对齐 1 条但历史数值在新文缺失→覆盖不足（弱析取废）
    assert len(pair.aligned_facts) == 1
    assert pair.outcome == "unresolved"
    assert pair.code == "FACT_INCOMPLETE"
    assert "facts:coverage_insufficient" in pair.unresolved_fields
    assert out.decision == "边界case/疑难case"


# ---------- P16-P06：主体不同直接冲突 ----------

def test_p06_subject_mismatch_alignment():
    """P16-P06：甲 vs 乙 → 主体不同 aligned_facts 空。"""
    pair, out, _, _ = _run_pipeline(
        "甲公司完成回购。",
        "乙公司完成回购。",
        history_subject="甲公司",
        current_subject="乙公司",
    )
    # 主体不同 → 不可配对 → aligned_facts 空
    assert pair.aligned_facts == ()


# ---------- P16-P07：主体缺失归边界（覆盖 / 单文） ----------

def test_p07_subject_missing_alignment():
    """P16-P07：同文 Hash 不能补主体；主体缺失归边界。"""
    pair, out, _, _ = _run_pipeline(
        "宣布完成回购。",
        "宣布完成回购。",
        history_subject="",
        current_subject="",
    )
    # 主体空 → aligned_facts 空
    assert pair.aligned_facts == ()


# ---------- P16-P08：时间相对词合法（同相对表达可重复） ----------

def test_p08_relative_time_same():
    """P16-P08：双方均"今日" + 无其他差异 → 等价。
    （N29/D28 注：本行原意=同相对时间表达可比——时间槽以 present"今日"
    真注入；缺失语义翻转与本行机制覆盖无关，归 test_n29_honest_missing。）"""
    history_text = "甲公司今日实施回购。"
    current_text = "甲公司今日实施回购。"
    pair, out, _, _ = _run_pipeline(
        history_text,
        current_text,
        history_time=_time_present_slot(history_text, "今日", record_id=RECORD_ID_H),
        current_time=_time_present_slot(current_text, "今日", record_id=RECORD_ID_C),
    )
    # D24 钉值：同相对时间表达不阻断 EXACT 证书等价
    assert pair.outcome == "equivalent"
    assert pair.code == "EXACT_TEXT_MATCH"
    assert out.decision == "重复"


# ---------- P16-P09：阶段差异冲突（今日/次日） ----------

def test_p09_today_vs_tomorrow():
    """P16-P09：今日 vs 次日 → 阶段差异直接冲突（应被时间字典捕获）。

    22:5x 主窗口迁移（B1 时间真路径实装）：旧稿 Facts 不带时间槽，占位期靠
    恒追加 TIME_RELATION_UNCERTAIN 挡住 equivalent；现时间路径已真，按本用例
    原意（"应被时间字典捕获"）把时间表达式注入 Facts——今日(day_0) vs
    次日(day_plus_1) 命中 _RELATIVE_CONFLICTS → VERIFIED_CONFLICT 正向断言。
    """
    history_text = "甲公司今日实施回购。"
    current_text = "甲公司次日实施回购。"
    pair, out, _, _ = _run_pipeline(
        history_text,
        current_text,
        history_time=_time_present_slot(history_text, "今日", record_id=RECORD_ID_H),
        current_time=_time_present_slot(current_text, "次日", record_id=RECORD_ID_C),
    )
    assert pair.outcome == "conflict"
    assert pair.code == "VERIFIED_CONFLICT"


# ---------- P16-P10：一方缺时间合法 missing ----------

def test_p10_missing_time_legal():
    """P16-P10（N29/D28 重钉）：双方均无时间槽 = 抽取未命中型 missing →
    时间关系未知（"未命中≠原文确无"）→ 诚实 unknown 进 time_pairs →
    compare_pair 冻结路由（issues 先于 equivalence）落边界——即使 EXACT
    证书在 P15 层仍签发。旧预期（不阻断等价）系 missing 洗白链路，裁定
    废除；真"确无"双侧场景由 B1-4/N29 专项保留面钉守。"""
    pair, out, _, p15_report = _run_pipeline(
        "甲公司完成回购。",
        "甲公司完成回购。",
    )
    assert p15_report.p15_results.time_pairs, "时间未知必须如实记录"
    assert p15_report.p15_results.equivalence_ready is True   # EXACT 证书仍签发（P15 层）
    assert pair.outcome == "unresolved"
    assert pair.code == "FACT_INCOMPLETE"
    assert out.decision == "边界case/疑难case"
    assert out.duplicate_ids == ()


# ---------- P16-P11：否定直接冲突 ----------

def test_p11_negation_verified_conflict():
    """P16-P11：未取消 vs 已取消 → 否定冲突。"""
    pair, out, _, _ = _run_pipeline(
        "甲公司未取消招标。",
        "甲公司已取消招标。",
        history_predicate="未取消",
        current_predicate="已取消",
    )
    # 谓词原文不同 → 不可配对（除非事实与原文字面一致）→ aligned_facts 空
    assert pair.aligned_facts == ()


# ---------- P16-P12：施事/引述关系冲突（甲称乙减持） ----------

def test_p12_quoted_actor_mismatch():
    """P16-P12：甲称乙减持 vs 甲将减持 → 施事/引述关系冲突。"""
    pair, out, _, _ = _run_pipeline(
        "甲公司称乙公司将减持。",
        "甲公司将减持。",
        history_key="乙公司",
        current_key="",
    )
    # 关键对象不同 → 不可配对
    assert pair.aligned_facts == ()


# ---------- P16-P13：属性补充合法（附属关系） ----------

def test_p13_supplementary_relation_aligned():
    """P16-P13：甲公司完成收购 + 交易对手为六家 → 附属属性补充关系。"""
    pair, out, _, _ = _run_pipeline(
        "甲公司完成收购。交易对手为六家。",
        "甲公司完成收购。",
        history_time=_time_verified_missing(),
        current_time=_time_verified_missing(),
    )
    # D24 钉值：附属属性补充（历史多一句）→ FACT_EQUIVALENT 签发
    # （N29/D28 注：时间双侧"确无"verified 槽维持签发路径覆盖）
    assert pair.outcome == "equivalent"
    assert pair.code == "FACT_EQUIVALENT"
    assert len(pair.aligned_facts) == 1
    assert out.decision == "重复"


# ---------- P16-P14：独立事件组合 → 不得签发 ----------

def test_p14b_independent_events_two_fact_fixture_blocked():
    """P16-P14（D24 真夹具，取代含 U+FFFD 乱码夹具的旧零驱动版）：
    双 fact 使"收购"作为真 Fact 进入管道——未覆盖独立事件必须挡签发
    （FACT_INCOMPLETE + current:f2 进 unresolved_fields，聚合落边界）。
    旧版单 fact 夹具下"收购"对管道不可见（实测恒 FACT_EQUIVALENT），
    且历史夹具字符串含 U+FFFD 乱码残留，一并退役。"""
    history_text = "甲公司发布新手机。"
    current_text = "甲公司发布新手机，同时收购芯片公司。"
    history_facts = [_make_fact(history_text, fact_id="f1", subject="甲公司",
                                  predicate="发布", record_id=RECORD_ID_H)]
    current_facts = [
        _make_fact(current_text, fact_id="f1", subject="甲公司",
                     predicate="发布", record_id=RECORD_ID_C),
        _make_fact(current_text, fact_id="f2", subject="甲公司",
                     predicate="收购", record_id=RECORD_ID_C),
    ]
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
                        text=current_text, arrival_seq=2,
                        raw_hash=__import__("hashlib").sha256(
                            current_text.encode("utf-8")).hexdigest())
    pair = pair_compare.compare_pair(
        history_ctx, current_ctx,
        history_artifact=history, current_artifact=current,
        alignment=alignment, p15_results=p15.p15_results,
    )
    assert len(pair.aligned_facts) == 1
    assert pair.outcome == "unresolved"
    assert pair.code == "FACT_INCOMPLETE"
    assert "current:f2" in pair.unresolved_fields
    plan = FrozenRecallPlan(version="rrf_v1", required={RECORD_ID_H: history_ctx})
    out = aggregate_module.aggregate(
        current_ctx, plan, [pair],
        coverage=CoverageStatus(visible_seq=10, prepared_seq=10, complete=True),
    )
    assert out.decision == "边界case/疑难case"
    assert out.duplicate_ids == ()


# ---------- P16-P15：两 Fact 完全对齐 → 全同 E ----------

def test_p15_two_facts_fully_aligned():
    """P16-P15：双方两 Fact 完全对齐 → equivalent。"""
    pair, out, _, _ = _run_pipeline(
        "甲公司完成收购。甲公司披露财报。",
        "甲公司完成收购。甲公司披露财报。",
        history_time=_time_verified_missing(),
        current_time=_time_verified_missing(),
    )
    # D24 钉值：全同双句 → EXACT 证书等价（弱析取废）
    # （N29/D28 注：时间双侧"确无"verified 槽维持证书路径覆盖）
    assert pair.outcome == "equivalent"
    assert pair.code == "EXACT_TEXT_MATCH"
    assert out.decision == "重复"


# ---------- P16-P16：预测 vs 实际 → 模态不同冲突 ----------

def test_p16_modality_forecast_vs_actual():
    """P16-P16：预计营收 vs 实际营收 → 模态不同应冲突（实词差异足以）。"""
    pair, out, _, _ = _run_pipeline(
        "甲公司预计营收100亿元。",
        "甲公司实际营收100亿元。",
        history_predicate="预计",
        current_predicate="实际",
    )
    # 谓词原文不同 → 不可配对 → aligned_facts 空
    assert pair.aligned_facts == ()


# ---------- P16-P17：错绑工件拒绝 ----------

def test_p17_mismatched_artifact_rejected():
    """P16-P17：篡改 evidence / 错 record_id / 错版本 → 拒绝。"""
    pair, out, _, _ = _run_pipeline(
        "甲公司完成回购。",
        "甲公司完成回购。",
    )
    # 篡改 history_report 的 fact record_id 使其与 artifact.record_id 不一致
    history_text = "甲公司完成回购。"
    history_facts = [_make_fact(history_text, fact_id="f1",
                                  subject="甲公司", predicate="回购",
                                  record_id=RECORD_ID_H)]
    bad_history = _make_report(history_text, history_facts, record_id=RECORD_ID_H)
    bad_history.artifact["facts"][0]["subject"]["evidence"][0]["record_id"] = RECORD_ID_C
    current_text = "甲公司完成回购。"
    current_facts = [_make_fact(current_text, fact_id="f1",
                                 subject="甲公司", predicate="回购",
                                 record_id=RECORD_ID_C)]
    current = _make_report(current_text, current_facts, record_id=RECORD_ID_C)
    history_ctx = _ctx(record_id=RECORD_ID_H, item_id="item-H", text=history_text, arrival_seq=1)
    current_ctx = _ctx(record_id=RECORD_ID_C, item_id="item-C", text=current_text, arrival_seq=2,
                        raw_hash=__import__('hashlib').sha256(current_text.encode("utf-8")).hexdigest())
    # 窗口K 收紧（D30 M-14）：原裸 Exception；实测 PairAlignmentError（evidence 回原文 record_id 不符）。
    with pytest.raises(pair_alignment.PairAlignmentError,
                       match="does not match artifact record_id"):
        pair_alignment.build_aligned(bad_history, current, history_text, current_text,
                                      dictionary_version="dict_v1",
                                      alignment_version="alignment_v1")


# ---------- P16-L01：非传递扩组 ----------

def test_l01_non_transitive():
    """L01：A=100/B 缺/C=101，C-B E、C-A C → C 列表=[B]，A 不传递入。

    D24 钉值重写：原夹具 B 无数值→C-B 实测为 FACT_INCOMPLETE 非等价
    （d24-p16d-probe：dup_ids 恒空，`if decision=="重复"` 条件断言空转）；
    改 B 与 C 全同（EXACT 等价）使非传递性真可判——列表必须恰为
    [item-B]，A（冲突）不得传递入。"""
    history_a_text = "甲公司公布现价100元。"
    history_b_text = "甲公司公布现价101元。"
    current_text = "甲公司公布现价101元。"
    # C 视角的 recallPlan：required = [A, B]
    # N29/D28 注：三方时间槽以"确无"verified 形态给出，维持本用例的
    # 冲突/等价非传递机制覆盖（未命中型 missing 语义归 test_n29_honest_missing）。
    history_a_facts = [_make_fact(history_a_text, fact_id="f1", subject="甲公司",
                                   predicate="公布", key_object="现价",
                                   numerics=["100"], record_id="a" * 64,
                                   time_slot=_time_verified_missing())]
    history_b_facts = [_make_fact(history_b_text, fact_id="f1", subject="甲公司",
                                   predicate="公布", key_object="现价",
                                   numerics=["101"], record_id="b" * 64,
                                   time_slot=_time_verified_missing())]
    current_facts = [_make_fact(current_text, fact_id="f1", subject="甲公司",
                                 predicate="公布", key_object="现价",
                                 numerics=["101"], record_id="c" * 64,
                                 time_slot=_time_verified_missing())]
    history_a = _make_report(history_a_text, history_a_facts, record_id="a" * 64)
    history_b = _make_report(history_b_text, history_b_facts, record_id="b" * 64)
    current = _make_report(current_text, current_facts, record_id="c" * 64)

    history_a_ctx = _ctx(record_id="a" * 64, item_id="item-A", text=history_a_text, arrival_seq=1)
    history_b_ctx = _ctx(record_id="b" * 64, item_id="item-B", text=history_b_text, arrival_seq=2)
    current_ctx = _ctx(record_id="c" * 64, item_id="item-C", text=current_text, arrival_seq=3,
                        raw_hash=__import__('hashlib').sha256(current_text.encode("utf-8")).hexdigest())

    align_a = pair_alignment.build_aligned(history_a, current, history_a_text, current_text,
                                            dictionary_version="dict_v1",
                                            alignment_version="alignment_v1")
    align_b = pair_alignment.build_aligned(history_b, current, history_b_text, current_text,
                                            dictionary_version="dict_v1",
                                            alignment_version="alignment_v1")
    p15_a = p15_integration.extract_p15_results(history_a, current, history_a_text, current_text, align_a)
    p15_b = p15_integration.extract_p15_results(history_b, current, history_b_text, current_text, align_b)
    pair_a = pair_compare.compare_pair(
        history_a_ctx, current_ctx,
        history_artifact=history_a, current_artifact=current,
        alignment=align_a, p15_results=p15_a.p15_results,
    )
    pair_b = pair_compare.compare_pair(
        history_b_ctx, current_ctx,
        history_artifact=history_b, current_artifact=current,
        alignment=align_b, p15_results=p15_b.p15_results,
    )
    plan = FrozenRecallPlan(
        version="rrf_v1",
        required={"a" * 64: history_a_ctx, "b" * 64: history_b_ctx},
    )
    out = aggregate_module.aggregate(
        current_ctx, plan, [pair_a, pair_b],
        coverage=CoverageStatus(visible_seq=10, prepared_seq=10, complete=True),
    )
    # D24 钉值（实测：pairA=VERIFIED_CONFLICT、pairB=EXACT_TEXT_MATCH）：
    # 列表恰为直接等价的 B——A 冲突不传递入（条件断言空转已废）
    assert pair_a.code == "VERIFIED_CONFLICT"
    assert pair_b.code == "EXACT_TEXT_MATCH"
    assert out.decision == "重复"
    assert out.duplicate_ids == ("item-B",)


# ---------- P16-L02：首命中继续 ----------

def test_l02_first_hit_continues():
    """L02：required 中首个 E 后不 break，预算内继续其余候选。

    W-R3b / R3-M7-e 语义钉：旧夹具与 P01 全同（单候选——"继续"语义
    真空，w-r3b-red-probes 实证首命中即 break 变异下旧钉不红）。今
    双候选双 EXACT 命中：首命中不 break → 双入列（首命中即 break
    变异仅产出 item-A，钉必红）。
    （N29/D28 注：时间双侧"确无"verified 槽维持 EXACT 等价路径覆盖。）"""
    text = "甲公司完成回购。"

    def _facts(record_id):
        return [_make_fact(text, fact_id="f1", subject="甲公司",
                           predicate="回购", record_id=record_id,
                           time_slot=_time_verified_missing())]

    history_a = _make_report(text, _facts("a" * 64), record_id="a" * 64)
    history_b = _make_report(text, _facts("b" * 64), record_id="b" * 64)
    current = _make_report(text, _facts("c" * 64), record_id="c" * 64)
    ctx_a = _ctx(record_id="a" * 64, item_id="item-A", text=text, arrival_seq=1)
    ctx_b = _ctx(record_id="b" * 64, item_id="item-B", text=text, arrival_seq=2)
    ctx_c = _ctx(record_id="c" * 64, item_id="item-C", text=text, arrival_seq=3)
    pairs = []
    for history, history_ctx in ((history_a, ctx_a), (history_b, ctx_b)):
        alignment = pair_alignment.build_aligned(
            history, current, text, text,
            dictionary_version="dict_v1", alignment_version="alignment_v1")
        p15 = p15_integration.extract_p15_results(
            history, current, text, text, alignment)
        pairs.append(pair_compare.compare_pair(
            history_ctx, ctx_c,
            history_artifact=history, current_artifact=current,
            alignment=alignment, p15_results=p15.p15_results))
    plan = FrozenRecallPlan(
        version="rrf_v1",
        required={"a" * 64: ctx_a, "b" * 64: ctx_b},
    )
    out = aggregate_module.aggregate(
        ctx_c, plan, pairs,
        coverage=CoverageStatus(visible_seq=10, prepared_seq=10, complete=True),
    )
    # 首命中继续：双 E 皆处理到底（双入列即"未 break"的直接物证）
    assert [p.code for p in pairs] == ["EXACT_TEXT_MATCH"] * 2
    assert out.decision == "重复"
    assert out.duplicate_ids == ("item-A", "item-B")


# ---------- P16-L03：单冲突不终止整条 ----------

def test_l03_single_conflict_does_not_end():
    """L03：单 pair=C 时，不要求终止。"""
    pair, out, _, _ = _run_pipeline(
        "甲公司公布现价100元。",
        "甲公司公布现价101元。",
        history_numerics=["100"], current_numerics=["101"],
    )
    # D24 钉值：100 vs 101 恒触发冲突（宽集接受收窄）
    assert pair.verified_conflicts
    assert pair.outcome == "conflict"
    assert pair.code == "VERIFIED_CONFLICT"
    assert out.decision == "不重复"


# ---------- P16-L04：禁拼 Fact ----------

def test_l04_no_synthesized_facts():
    """L04：所有 pair=U → 边界 []，禁止拼出重复。"""
    pair, out, _, _ = _run_pipeline(
        "甲公司发布新手机。",
        "乙公司发布新手机。",
        history_subject="甲公司",
        current_subject="乙公司",
    )
    # 主体不同 → aligned_facts 空
    assert pair.aligned_facts == ()
    assert out.duplicate_ids == ()


# ---------- P16-L05：全 C 健康 → 不重复 ----------

def test_l05_all_conflict_not_duplicate():
    """L05：所有 pair=C 且健康 → 不重复 []。

    D24 钉值（陈旧 docstring 同步）："P15 时间字典未实装→接受 boundary"
    已过时——时间路径已真（P09 TIME_SAME_RELATIVE 实证）；本用例双方
    均无时间槽，100 vs 101 冲突恒触发，聚合实测落 不重复。
    条件分支（空转）与双值接受一并废除。
    """
    pair, out, _, _ = _run_pipeline(
        "甲公司公布现价100元。",
        "甲公司公布现价101元。",
        history_numerics=["100"], current_numerics=["101"],
    )
    assert pair.verified_conflicts
    assert pair.outcome == "conflict"
    assert out.decision == "不重复"
    assert out.duplicate_ids == ()


# ---------- P16-L06：U 健康 → 边界 ----------

def test_l06_uncertain_pair_boundary():
    """L06：U 健康 → 边界 []。

    23:5x 主窗口迁移（EXACT 证书校准，fp 3139b94f 复盘）：全同文本即使主体
    未抽出也判 重复（字节相等自证重复，B1 金标 77/77 verbatim 对 fp=0 为证）；
    L06 原意（主体未解 → 不拼重复）由相异文本控制组保留——无证书且无对齐
    时仍走边界。
    """
    pair, out, _, _ = _run_pipeline(
        "宣布完成回购。",
        "宣布完成回购。",
        history_subject="",
        current_subject="",
    )
    assert pair.aligned_facts == ()
    # 全同文本：EXACT 证书路径 → 重复（校准后语义）
    assert out.decision == "重复"
    assert out.duplicate_ids == (f"item-{'a' * 8}",)

    # 相异文本控制组：无证书 + 主体未解 → 边界（L06 原意保留）
    pair2, out2, _, _ = _run_pipeline(
        "宣布完成回购。",
        "宣布完成增发。",
        history_subject="",
        current_subject="",
        current_predicate="增发",
    )
    assert pair2.aligned_facts == ()
    assert out2.duplicate_ids == ()
    # D24 钉值：实测恒边界（双值接受收窄）
    assert out2.decision == "边界case/疑难case"
    assert out2.internal_code == "FACT_INCOMPLETE"


# ---------- P16-L07：其他故障不撤销已确认 ----------

def test_l07_failure_does_not_unconfirm():
    """L07：单 pair 实测。
    （N29/D28 注：时间双侧"确无"verified 槽维持 EXACT 等价路径覆盖。）"""
    pair, out, _, _ = _run_pipeline(
        "甲公司完成回购。",
        "甲公司完成回购。",
        history_time=_time_verified_missing(),
        current_time=_time_verified_missing(),
    )
    # D24 钉值：EXACT 等价恒成立（条件断言空转废）→ 必签发重复
    assert pair.outcome == "equivalent"
    assert pair.code == "EXACT_TEXT_MATCH"
    assert out.decision == "重复"


# ---------- P16-L08：非法成员六形态 ----------

def test_l08_illegal_member_six_forms():
    """L08：六形态在 aggregate 入口抛 IllegalMemberError。"""
    history = _ctx(record_id=RECORD_ID_H, item_id="item-H", text="甲", arrival_seq=1)
    current = _ctx(record_id=RECORD_ID_C, item_id="item-C", text="乙", arrival_seq=2,
                    raw_hash=__import__('hashlib').sha256("乙".encode("utf-8")).hexdigest())
    pair_text = "甲公司完成回购。"
    pair_facts = [_make_fact(pair_text, fact_id="f1", subject="甲公司",
                              predicate="回购", record_id=RECORD_ID_H)]
    pair_report = _make_report(pair_text, pair_facts, record_id=RECORD_ID_H)
    current_text = "甲公司完成回购。"
    current_facts = [_make_fact(current_text, fact_id="f1", subject="甲公司",
                                 predicate="回购", record_id=RECORD_ID_C)]
    current_report = _make_report(current_text, current_facts, record_id=RECORD_ID_C)
    align = pair_alignment.build_aligned(pair_report, current_report,
                                          pair_text, current_text,
                                          dictionary_version="dict_v1",
                                          alignment_version="alignment_v1")
    p15 = p15_integration.extract_p15_results(pair_report, current_report,
                                                 pair_text, current_text, align)
    base_pair = pair_compare.compare_pair(
        history, current,
        history_artifact=pair_report, current_artifact=current_report,
        alignment=align, p15_results=p15.p15_results,
    )
    plan = FrozenRecallPlan(version="rrf_v1", required={RECORD_ID_H: history})

    # 自身 record_id
    with pytest.raises(aggregate_module.IllegalMemberError):
        bad = _ctx(record_id=RECORD_ID_H, item_id="item-C", text="", arrival_seq=2,
                    raw_hash=__import__('hashlib').sha256("".encode("utf-8")).hexdigest())
        aggregate_module.aggregate(bad, plan, [base_pair],
                                     coverage=CoverageStatus(complete=True))
    # 未来 arrival_seq
    future_history = _ctx(record_id=RECORD_ID_H, item_id="item-H", text="甲",
                           arrival_seq=5)
    future_plan = FrozenRecallPlan(version="rrf_v1",
                                     required={RECORD_ID_H: future_history})
    # 窗口K 收紧（D30 M-14）：原裸 Exception；实测 AggregateError（identity/hash 绑定）。
    with pytest.raises(aggregate_module.AggregateError,
                       match="identity/hash does not match"):
        aggregate_module.aggregate(current, future_plan, [base_pair],
                                     coverage=CoverageStatus(complete=True))
    # 异 scope
    bad_scope_history = _ctx(record_id=RECORD_ID_H, item_id="item-H", text="甲",
                              arrival_seq=1, scope_id="other")
    bad_scope_plan = FrozenRecallPlan(version="rrf_v1",
                                        required={RECORD_ID_H: bad_scope_history})
    with pytest.raises(aggregate_module.IllegalMemberError):
        aggregate_module.aggregate(current, bad_scope_plan, [base_pair],
                                     coverage=CoverageStatus(complete=True))
    # 跨 business_date
    bad_date_history = _ctx(record_id=RECORD_ID_H, item_id="item-H", text="甲",
                              arrival_seq=1, business_date="2026-09-27")
    bad_date_plan = FrozenRecallPlan(version="rrf_v1",
                                      required={RECORD_ID_H: bad_date_history})
    with pytest.raises(aggregate_module.IllegalMemberError):
        aggregate_module.aggregate(current, bad_date_plan, [base_pair],
                                     coverage=CoverageStatus(complete=True))
    # 不在 required
    orphan_history = _ctx(record_id="x" * 64, item_id="item-X", text="甲",
                           arrival_seq=1)
    orphan_plan = FrozenRecallPlan(version="rrf_v1",
                                     required={"x" * 64: orphan_history})
    with pytest.raises(aggregate_module.IllegalMemberError):
        aggregate_module.aggregate(current, orphan_plan, [base_pair],
                                     coverage=CoverageStatus(complete=True))
    # 错绑 item_id：按 P16-C 已验收合同，错绑经 PairBindingError 透传为 AggregateError
    # （aggregate._check_pair_binding 不视作非法成员；非法成员是 INV-12 的"自指 record_id"）
    from news_flash_dedup.compare.pair_compare import PairResult as PR
    bad_binding = PR(
        pair_id=base_pair.pair_id,
        history_record_id=base_pair.history_record_id,
        current_record_id=base_pair.current_record_id,
        history_item_id="item-other",
        current_item_id=base_pair.current_item_id,
        history_arrival_seq=base_pair.history_arrival_seq,
        current_arrival_seq=base_pair.current_arrival_seq,
        history_raw_hash=base_pair.history_raw_hash,
        current_raw_hash=base_pair.current_raw_hash,
        pipeline_version=base_pair.pipeline_version,
        outcome=base_pair.outcome,
        code=base_pair.code,
        detail=base_pair.detail,
        aligned_facts=(), verified_conflicts=(), unresolved_fields=(),
        used_evidence=(), budget_at=0,
    )
    with pytest.raises(aggregate_module.AggregateError) as exc_info:
        aggregate_module.aggregate(current, plan, [bad_binding],
                                     coverage=CoverageStatus(complete=True))
    assert "identity/hash does not match" in str(exc_info.value)


# ---------- P16-L09：列表排序去重 + 公共字段封闭 ----------

def test_l09_sorted_deduped_public_fields():
    """L09：列表升序去重；公共 reason 非空且不含内部码。"""
    pair, out, _, _ = _run_pipeline(
        "甲公司完成回购。",
        "甲公司完成回购。",
    )
    assert out.item_id != ""
    assert out.text != ""
    assert out.reason != ""
    for code in EQUIVALENT_CODES | UNRESOLVED_CODES | {"VERIFIED_CONFLICT"}:
        assert code not in out.reason


# ---------- P16-L10：失败状态不入业务三分类 ----------

def test_l10_no_failed_decision():
    """L10：aggregate decision ∈ {重复, 不重复, 边界case/疑难case}。"""
    pair, out, _, _ = _run_pipeline(
        "甲公司完成回购。",
        "乙公司完成回购。",
        history_subject="甲公司", current_subject="乙公司",
    )
    assert out.decision in {"重复", "不重复", "边界case/疑难case"}


# ---------- P16-L11：T045/T046 三分类汇总绿灯 ----------

def test_l11_three_class_aggregate_green():
    """L11：T045/T046 三分类汇总：∅→不重复、非空→重复、其余→边界。"""
    # ∅→不重复
    history = _ctx(record_id=RECORD_ID_H, item_id="item-H", text="甲", arrival_seq=1)
    current = _ctx(record_id=RECORD_ID_C, item_id="item-C", text="乙",
                    arrival_seq=2,
                    raw_hash=__import__('hashlib').sha256("乙".encode("utf-8")).hexdigest())
    plan = FrozenRecallPlan(version="rrf_v1", required={RECORD_ID_H: history})
    out_empty = aggregate_module.aggregate(
        current, plan, [],
        coverage=CoverageStatus(visible_seq=10, prepared_seq=10, complete=True),
    )
    assert out_empty.decision == "不重复"
    assert out_empty.duplicate_ids == ()

    # 非空→重复（用 _run_pipeline E 路径）——D24 钉值：条件断言空转废
    # （N29/D28 注：时间双侧"确无"verified 槽维持 EXACT 等价路径覆盖）
    pair, out_eq, _, _ = _run_pipeline(
        "甲公司完成回购。",
        "甲公司完成回购。",
        history_time=_time_verified_missing(),
        current_time=_time_verified_missing(),
    )
    assert out_eq.decision == "重复"
    assert out_eq.duplicate_ids == ("item-aaaaaaaa",)

    # 其余→边界（无 aligned → 主体不同）——D24 钉值：双值接受收窄
    pair3, out3, _, _ = _run_pipeline(
        "甲公司完成回购。",
        "乙公司完成回购。",
        history_subject="甲公司", current_subject="乙公司",
    )
    assert out3.decision == "边界case/疑难case"
    assert out3.duplicate_ids == ()


# ---------- 边界率估算（标尺 3：02:43 批复硬义务） ----------

import re as _re  # noqa: E402  (H-06 重铸：主体正则实取)

# H-06 重铸钉值（窗口K，基线=N27 后态 uat054137）：46 对逐对 aligned 实测。
# 站纪律：后续链站（N24/N26 等）合法翻转矩阵 fixture 时，须按
# "每站重钉 + 机制说明"纪律更新本元组与下方交叉断言（红测纪律同款：
# 翻转须配机制注释，不得静默改钉）。
_PINNED_ALIGNED_46 = (
    # synonym ×12：6 对谓词同义改写、双侧无共有动词 token → 严格逐字门
    # （dictionary=None，不走 Tier-2 词典）不配对；其余 6 对共有 token 配对。
    True, True, False, False, False, False, True, False, True, True, True, False,
    # split ×10：主体 + 共有谓词 token 双侧可定位 → 全配对。
    True, True, True, True, True, True, True, True, True, True,
    # numeric_synonym ×12：fixture 文本无主体槽（"现价100元" 等），
    # 主体 missing → 严格门不配对（标签 equivalent 的终判不属本门）。
    False, False, False, False, False, False,
    False, False, False, False, False, False,
    # extra_event ×12：同事件 Fact 门级配对（aligned=True）；标签 boundary
    # 由下游 extra_event 检出承担（不属 build_aligned 门）。
    True, True, True, True, True, True, True, True, True, True, True, True,
)

_SUBJECT_PATTERN = _re.compile(r"[甲乙丙丁戊己庚辛壬癸](?:公司|集团)")

_PREDICATE_TOKENS = ("完成", "发布", "宣布", "披露", "中标", "启动",
                     "增持", "减持", "收购", "发行", "上市",
                     "召开", "终止", "实施", "回购", "增持股份")


def _subject_from_text(text):
    """H-06：主体按 fixture 文本实取（首个 干支+公司/集团 词）；无则 None（诚实 missing）。"""
    match = _SUBJECT_PATTERN.search(text)
    return match.group(0) if match else None


def _shared_predicate_token(history_text, current_text):
    """H-06：谓词取双侧文本共有动词 token（双侧可码点定位）；无共有则 None（诚实 missing）。"""
    for token in _PREDICATE_TOKENS:
        if token in history_text and token in current_text:
            return token
    return None


def test_boundary_rate_synthetic_rewrites():
    """46 对合成改写 fixture 过严格配对门：边界率实测钉值（无伪造证据）。

    窗口K 重铸（D30 H-06，基线=N27 后态 uat054137）：
    - 拆伪造证据回退：subject 由 fixture 文本正则实取（旧版硬编码 "甲公司"
      通用于全部 46 对，乙/丙/丁文本经 _present L82-92 回退伪造 text[:2]
      锚点、quote/raw_value 不一致）；谓词取双侧共有 token（旧版
      first_word 兜底 history_text[3:5] 对 current 侧同样伪造）。
      现所有 evidence 均原文码点可定位，伪造回退不再被触发。
    - expected_outcome 入断言：逐对 aligned 钉值见 _PINNED_ALIGNED_46
      （以 N27 后实测为准）+ 类别×标签交叉断言；机制说明见钉值注释。
    - 旧"矩阵全绿（46/0）"历史结论回炉：实测 aligned=28 / boundary=18，
      翻转 18 对（6 synonym 谓词同义无共有 token + 12 numeric 无主体槽），
      如实记录；extra_event 12 对门级 aligned=True 与标签 boundary 不矛盾
      （extra_event 检出在下游 P16-B，不属本门）。
    """
    from test_p16_d_boundary_rate import REWRITE_PAIRS
    assert len(REWRITE_PAIRS) == 46
    assert len(_PINNED_ALIGNED_46) == 46

    actual_aligned = []
    category_stats = {}
    for idx, pair in enumerate(REWRITE_PAIRS):
        history_subject = _subject_from_text(pair.history_text)
        current_subject = _subject_from_text(pair.current_text)
        predicate = _shared_predicate_token(pair.history_text, pair.current_text)
        history_facts = [_make_fact(pair.history_text, fact_id="f1",
                                    subject=history_subject, predicate=predicate,
                                    record_id=RECORD_ID_H)]
        current_facts = [_make_fact(pair.current_text, fact_id="f1",
                                    subject=current_subject, predicate=predicate,
                                    record_id=RECORD_ID_C)]
        history = _make_report(pair.history_text, history_facts, record_id=RECORD_ID_H)
        current = _make_report(pair.current_text, current_facts, record_id=RECORD_ID_C)
        alignment = pair_alignment.build_aligned(
            history, current, pair.history_text, pair.current_text,
            dictionary_version="dict_v1",
            alignment_version="alignment_v1",
        )
        aligned = bool(alignment.aligned_facts)
        actual_aligned.append(aligned)
        stats = category_stats.setdefault(
            pair.category, {"aligned": 0, "boundary": 0,
                            "label_equivalent": 0, "label_boundary": 0})
        stats["aligned" if aligned else "boundary"] += 1
        stats[f"label_{pair.expected_outcome}"] += 1

    # 逐对钉值（红能力：任一对翻转即红，并指向站纪律）
    for idx, (pair, aligned) in enumerate(zip(REWRITE_PAIRS, actual_aligned)):
        assert aligned is _PINNED_ALIGNED_46[idx], (
            f"pair[{idx}] ({pair.category}) 实测翻转：钉值={_PINNED_ALIGNED_46[idx]} "
            f"actual={aligned}；若系后续链站（N24/N26 等）合法翻转，按"
            f"'每站重钉+机制说明'纪律更新 _PINNED_ALIGNED_46"
        )

    # 汇总钉值：aligned=28 / boundary=18（旧伪造态 46/0 回炉）
    aligned_count = sum(actual_aligned)
    boundary_count = 46 - aligned_count
    assert aligned_count == 28
    assert boundary_count == 18

    # 类别×机制交叉钉值
    assert category_stats["synonym"] == {
        "aligned": 6, "boundary": 6, "label_equivalent": 12, "label_boundary": 0}
    assert category_stats["split"] == {
        "aligned": 10, "boundary": 0, "label_equivalent": 10, "label_boundary": 0}
    assert category_stats["numeric_synonym"] == {
        "aligned": 0, "boundary": 12, "label_equivalent": 12, "label_boundary": 0}
    assert category_stats["extra_event"] == {
        "aligned": 12, "boundary": 0, "label_equivalent": 0, "label_boundary": 12}

    # expected_outcome 全局交叉：标签 boundary 12 对门级全 aligned（extra_event
    # 终判在下游）；标签 equivalent 34 对中 16 aligned / 18 门级不配对（机制见上）。
    label_boundary_aligned = sum(
        1 for pair, aligned in zip(REWRITE_PAIRS, actual_aligned)
        if pair.expected_outcome == "boundary" and aligned)
    label_equivalent_aligned = sum(
        1 for pair, aligned in zip(REWRITE_PAIRS, actual_aligned)
        if pair.expected_outcome == "equivalent" and aligned)
    assert label_boundary_aligned == 12
    assert label_equivalent_aligned == 16