# -*- coding: utf-8 -*-
"""窗口J 判定区修复红能力测试（批次A：H-03/M-01/F4-4/M-13；批次B：M-04）。

每个用例先红后绿：修前对当前代码失败（实证缺陷），修后转绿。
夹具纪律：时间槽一律 verified_missing=True（合法"确无"→ compare_time
compatible），排除 TIME_RELATION_UNCERTAIN 占位对断言路径的干扰。
"""
from __future__ import annotations

import hashlib
import json

import pytest

from news_flash_dedup.commit.coordinator import CommitContext
from news_flash_dedup.compare import pair_alignment, pair_compare
from news_flash_dedup.decide import service as decide_service
from news_flash_dedup.facts import FactValidationReport


# ---------- 夹具助手 ----------

def _fact(record_id: str, subject: str, predicate: str, fact_id: str = "f1") -> dict:
    """单 Fact 夹具：subject/predicate present（evidence quote=subject 于文首），
    时间槽 verified_missing=True（合法确无），其余 missing。"""
    span = {"record_id": record_id, "field": "x",
            "quote": subject, "start": 0, "end": len(subject)}
    present_s = {"status": "present", "raw_value": subject, "evidence": [dict(span)]}
    present_p = {"status": "present", "raw_value": predicate, "evidence": [dict(span)]}
    missing = {"status": "missing", "raw_value": None, "evidence": []}
    time_missing_verified = {"status": "missing", "raw_value": None,
                             "evidence": [], "verified_missing": True}
    return {
        "fact_id": fact_id,
        "evidence": [dict(span)],
        "fact_type": present_p,
        "subject": present_s,
        "event_state": {
            "predicate": dict(present_p),
            "polarity": dict(present_p),
            "modality": dict(missing),
            "attribution": dict(missing),
        },
        "time": {"expression": dict(time_missing_verified),
                 "stage": dict(missing), "anchor": dict(missing)},
        "key_object": dict(missing),
        "numerics": [],
    }


def _ctx(record_id: str, item_id: str, text: str, arrival_seq: int,
         facts: list, **kw) -> dict:
    ctx = {
        "record_id": record_id, "item_id": item_id, "text": text,
        "raw_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "scope_id": "default", "business_date": "2026-09-26",
        "arrival_seq": arrival_seq, "pipeline_version": "dedup_v1",
        "facts": facts,
    }
    ctx.update(kw)
    return ctx


# ---------- H-03：多候选 extra_event 逐对记账 ----------

def test_h03_multi_candidate_extra_event_recorded_per_pair():
    """H-03（外审+四轮）：多候选任务候选 #2+ 的独立事件组合必须记账。

    修前：detect_extra_event 只在候选循环外对 (history, current) 跑一次；
    p15_integration.uncovered_independent_relation 恒 False（L367）使
    pair_compare L305-306 消费分支在编排层无供给——候选 d 的独立事件 f2
    仅经对齐 leftover 退化为 FACT_INCOMPLETE，RULE_UNCOVERED 漏记。
    修后：候选循环内逐对检测入账——RULE_UNCOVERED 上浮为主码、
    uncovered fact_id 进 unresolved_fields。
    """
    history = _ctx("a" * 64, "item-A", "甲公司完成回购。", 1,
                   [_fact("a" * 64, "甲公司", "回购")])
    current = _ctx("c" * 64, "item-C", "丁公司发布新手机。", 4,
                   [_fact("c" * 64, "丁公司", "发布")])
    cand1 = _ctx("b" * 64, "item-B", "乙公司宣布分红。", 2,
                 [_fact("b" * 64, "乙公司", "分红")])
    # 候选 #2：与 current 共享 f1（丁公司/发布逐字三元组），另带独立事件 f2
    cand2 = _ctx("d" * 64, "item-D", "丁公司发布新手机，同时收购芯片公司。", 3,
                 [_fact("d" * 64, "丁公司", "发布", fact_id="f1"),
                  _fact("d" * 64, "丁公司", "收购", fact_id="f2")])
    outcome = decide_service.decide_for_task(
        history, [cand1, cand2], current=current, coverage_complete=True,
    )
    assert set(outcome.pair_codes.keys()) == {"a" * 64, "b" * 64, "d" * 64}
    assert outcome.decision == "边界case/疑难case"
    # 红能力断言 1：候选 d 的独立事件 uncovered id 必须入账（修前漏记）
    assert "history:f2" in outcome.unresolved_fields, (
        f"候选 #2 独立事件未入账（H-03 漏记）：unresolved_fields="
        f"{outcome.unresolved_fields!r}"
    )
    # 红能力断言 2：RULE_UNCOVERED 上浮为聚合主码（修前退化 FACT_INCOMPLETE）
    assert outcome.internal_code == "RULE_UNCOVERED", (
        f"候选 #2 独立事件组合退化为 {outcome.internal_code!r}（H-03："
        "RULE_UNCOVERED 未记账）"
    )


def test_h03_single_candidate_path_unchanged():
    """H-03 锚稳守卫：candidates=() 单对路径（decide_once/固定候选回放形态）
    行为逐字节不变——history 带独立事件时仍按旧口径记账一次。"""
    history = _ctx("a" * 64, "item-A", "甲公司完成回购，同时收购芯片公司。", 1,
                   [_fact("a" * 64, "甲公司", "回购", fact_id="f1"),
                    _fact("a" * 64, "甲公司", "收购", fact_id="f2")])
    current = _ctx("c" * 64, "item-C", "甲公司完成回购。", 2,
                   [_fact("c" * 64, "甲公司", "回购", fact_id="f1")])
    outcome = decide_service.decide_for_task(
        history, [], current=current, coverage_complete=True,
    )
    assert outcome.decision == "边界case/疑难case"
    assert "history:f2" in outcome.unresolved_fields
    assert outcome.internal_code == "RULE_UNCOVERED"


# ---------- M-01：coverage_complete 默认 True 拆除（强制关键字无默认） ----------

def test_m01_coverage_complete_mandatory_everywhere():
    """M-01（13:20 原令）：coverage_complete 不得再有默认 True——
    decide_once / decide_for_task / CommitContext 缺省调用必须 TypeError。"""
    history = _ctx("a" * 64, "item-A", "甲公司完成回购。", 1,
                   [_fact("a" * 64, "甲公司", "回购")])
    current = _ctx("c" * 64, "item-C", "甲公司完成回购。", 2,
                   [_fact("c" * 64, "甲公司", "回购")])
    with pytest.raises(TypeError):
        decide_service.decide_for_task(history, [], current=current)
    with pytest.raises(TypeError):
        decide_service.decide_once(history, current)
    with pytest.raises(TypeError):
        CommitContext(
            scope_id="default", business_date="2026-09-26",
            arrival_seq=2, current=current, candidates=(history,),
            visible_seq=2, prepared_seq=2,
        )


# ---------- F4-4：current=None 退化路径守卫 ----------

def test_f4_4_current_none_degenerate_path_semantic_error():
    """F4-4（四轮 D 轮）：current=None + 候选非空的退化路径原先
    current=history 首对自比，由 pair_alignment 深层冒出
    PairAlignmentError('history and current record_id must differ')（非语义）。
    修后：编排层守卫直接抛带语义的 DecideInputError。"""
    history = _ctx("a" * 64, "item-A", "甲公司完成回购。", 1,
                   [_fact("a" * 64, "甲公司", "回购")])
    cand = _ctx("b" * 64, "item-B", "甲公司完成回购。", 2,
                [_fact("b" * 64, "甲公司", "回购")])
    with pytest.raises(decide_service.DecideInputError, match="current"):
        decide_service.decide_for_task(
            history, [cand], current=None, coverage_complete=True,
        )


# ---------- M-13：raw_hash 口径统一（上游键优先，缺省才重算） ----------

def test_m13_candidate_upstream_raw_hash_not_laundered():
    """M-13：候选自带 raw_hash 键与 text 不符时，修前被 __import__('hashlib')
    内联重算静默洗值（照常跑完）；修后上游键入账 → pair_compare 绑定门
    fail-closed 抛 PairBindingError。"""
    history = _ctx("a" * 64, "item-A", "甲公司完成回购。", 1,
                   [_fact("a" * 64, "甲公司", "回购")])
    current = _ctx("c" * 64, "item-C", "甲公司完成回购。", 3,
                   [_fact("c" * 64, "甲公司", "回购")])
    cand_bad = _ctx("b" * 64, "item-B", "甲公司完成回购。", 2,
                    [_fact("b" * 64, "甲公司", "回购")])
    cand_bad["raw_hash"] = "0" * 64  # 与 sha256(text) 不符的脏键
    with pytest.raises(pair_compare.PairBindingError):
        decide_service.decide_for_task(
            history, [cand_bad], current=current, coverage_complete=True,
        )


def test_m13_missing_raw_hash_falls_back_to_recompute():
    """M-13 守卫：候选缺 raw_hash 键时仍按 text 重算（缺省 fallback 不断路）。"""
    history = _ctx("a" * 64, "item-A", "甲公司完成回购。", 1,
                   [_fact("a" * 64, "甲公司", "回购")])
    current = _ctx("c" * 64, "item-C", "甲公司完成回购。", 3,
                   [_fact("c" * 64, "甲公司", "回购")])
    cand_nokey = _ctx("b" * 64, "item-B", "甲公司完成回购。", 2,
                      [_fact("b" * 64, "甲公司", "回购")])
    del cand_nokey["raw_hash"]
    outcome = decide_service.decide_for_task(
        history, [cand_nokey], current=current, coverage_complete=True,
    )
    assert "b" * 64 in outcome.pair_codes


def test_m13_correct_upstream_key_same_as_recompute():
    """M-13 守卫：上游键正确时结果与缺键重算逐字段一致（口径统一不改语义）。"""
    history = _ctx("a" * 64, "item-A", "甲公司完成回购。", 1,
                   [_fact("a" * 64, "甲公司", "回购")])
    current = _ctx("c" * 64, "item-C", "甲公司完成回购。", 3,
                   [_fact("c" * 64, "甲公司", "回购")])
    cand_with = _ctx("b" * 64, "item-B", "甲公司完成回购。", 2,
                     [_fact("b" * 64, "甲公司", "回购")])
    cand_without = dict(cand_with)
    del cand_without["raw_hash"]
    out_with = decide_service.decide_for_task(
        history, [cand_with], current=current, coverage_complete=True,
    )
    out_without = decide_service.decide_for_task(
        history, [cand_without], current=current, coverage_complete=True,
    )
    assert out_with.decision == out_without.decision
    assert out_with.internal_code == out_without.internal_code
    assert out_with.pair_codes == out_without.pair_codes
    assert out_with.unresolved_fields == out_without.unresolved_fields


# ---------- M-04：对齐 Tier-1 优先不被 Tier-2 词典匹配抢占 ----------

_RECORD_H = "e" * 64
_RECORD_C = "f" * 64


def _pa_evidence(text, quote, field, *, record_id, occurrence=0):
    start = -1
    for _ in range(occurrence + 1):
        start = text.index(quote, start + 1)
    return {"record_id": record_id, "field": field,
            "quote": quote, "start": start, "end": start + len(quote)}


def _pa_present(text, quote, field, *, record_id):
    return {"status": "present", "raw_value": quote,
            "evidence": [_pa_evidence(text, quote, field, record_id=record_id)]}


def _pa_missing():
    return {"status": "missing", "raw_value": None, "evidence": []}


def _pa_fact(text, *, fact_id, subject, predicate, sentence, record_id):
    base = f"facts.{fact_id}"
    return {
        "fact_id": fact_id,
        "evidence": [_pa_evidence(text, sentence, base, record_id=record_id)],
        "fact_type": _pa_present(text, predicate, base + ".fact_type",
                                 record_id=record_id),
        "subject": _pa_present(text, subject, base + ".subject",
                               record_id=record_id),
        "event_state": {
            "predicate": _pa_present(text, predicate,
                                     base + ".event_state.predicate",
                                     record_id=record_id),
            "polarity": _pa_present(text, predicate,
                                    base + ".event_state.polarity",
                                    record_id=record_id),
            "modality": _pa_missing(),
            "attribution": _pa_missing(),
        },
        "time": {"expression": _pa_missing(), "stage": _pa_missing(),
                 "anchor": _pa_missing()},
        "key_object": _pa_missing(),
        "numerics": [],
    }


def _pa_report(text, facts, *, record_id) -> FactValidationReport:
    artifact = {
        "schema_version": "1.0", "record_id": record_id,
        "offset_unit": "unicode_code_point", "extraction_status": "complete",
        "facts": facts, "unparsed_spans": [], "uncertainties": [],
    }
    return FactValidationReport(
        record_id=record_id, validated_complete=bool(facts),
        extraction_status="complete",
        valid_fact_ids=tuple(f["fact_id"] for f in facts),
        numeric_inventory=(), issues=(), artifact=artifact,
    )


def _preemption_dict(tmp_path):
    from news_flash_dedup.compare import normalize_dict
    payload = {
        "version": "norm_dict_v1", "date": "2026-09-28",
        "sections": {
            "predicate": {
                "kind": "synonym_class", "directional": False,
                "entries": [
                    {"id": "pred-winj-0001", "canonical": "买入",
                     "variants": ["增持"]},
                ],
            },
        },
    }
    path = tmp_path / "winj_norm_dict.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    normalize_dict.clear_cache()
    return normalize_dict.load_dictionary(str(path))


def test_m04_tier1_verbatim_not_preempted_by_tier2(tmp_path):
    """M-04(a)（外审+四轮）：Tier-2 词典匹配不得抢占后序 history 的 Tier-1 逐字三元组。

    夹具：history f1（甲公司/增持，句序在前）对 current g1（甲公司/买入）
    仅 Tier-2 词典可配（增持→买入 canonical 同、subject 逐字锚定）；
    history f2（甲公司/买入，句序在后）对 g1 是 Tier-1 逐字三元组。
    修前：f1 先处理，Tier-2 命中即 discard g1（L399 提前 discard）→
    f2 的 Tier-1 被抢占落未匹配。修后：Tier-1 全局优先——f2↔g1 逐字配对，
    f1 落未匹配。
    """
    dictionary = _preemption_dict(tmp_path)
    history_text = "甲公司增持。甲公司买入。"
    current_text = "甲公司买入。"
    history = _pa_report(history_text, [
        _pa_fact(history_text, fact_id="f1", subject="甲公司", predicate="增持",
                 sentence="甲公司增持。", record_id=_RECORD_H),
        _pa_fact(history_text, fact_id="f2", subject="甲公司", predicate="买入",
                 sentence="甲公司买入。", record_id=_RECORD_H),
    ], record_id=_RECORD_H)
    current = _pa_report(current_text, [
        _pa_fact(current_text, fact_id="g1", subject="甲公司", predicate="买入",
                 sentence="甲公司买入。", record_id=_RECORD_C),
    ], record_id=_RECORD_C)
    outcome = pair_alignment.build_aligned(
        history, current, history_text, current_text,
        dictionary_version="norm_dict_v1", dictionary_date="2026-09-28",
        alignment_version="alignment_v1", dictionary=dictionary,
    )
    assert len(outcome.aligned_facts) == 1
    pair = outcome.aligned_facts[0]
    # 红能力断言：Tier-1 逐字对（f2,g1）必须胜出，basis 不得是词典别名
    assert (pair.history.fact_id, pair.current.fact_id) == ("f2", "g1"), (
        f"Tier-2 抢占了后序 history 的 Tier-1 逐字三元组（M-04(a)）："
        f"aligned={[(p.history.fact_id, p.current.fact_id, p.basis) for p in outcome.aligned_facts]!r}"
    )
    assert pair.basis == "FACT_EQUIVALENT"
    assert set(outcome.unresolved_fields) == {"history:f1"}


def test_m04_tier2_fallback_when_no_tier1_competition(tmp_path):
    """M-04 守卫：无 Tier-1 竞争时 Tier-2 词典匹配照常命中（修复不误伤杠杆 b）。"""
    dictionary = _preemption_dict(tmp_path)
    history_text = "甲公司增持。"
    current_text = "甲公司买入。"
    history = _pa_report(history_text, [
        _pa_fact(history_text, fact_id="f1", subject="甲公司", predicate="增持",
                 sentence="甲公司增持。", record_id=_RECORD_H),
    ], record_id=_RECORD_H)
    current = _pa_report(current_text, [
        _pa_fact(current_text, fact_id="g1", subject="甲公司", predicate="买入",
                 sentence="甲公司买入。", record_id=_RECORD_C),
    ], record_id=_RECORD_C)
    outcome = pair_alignment.build_aligned(
        history, current, history_text, current_text,
        dictionary_version="norm_dict_v1", dictionary_date="2026-09-28",
        alignment_version="alignment_v1", dictionary=dictionary,
    )
    assert len(outcome.aligned_facts) == 1
    pair = outcome.aligned_facts[0]
    assert pair.basis == "SUBJECT_DETERMINISTIC_ALIAS"
    assert not outcome.unresolved_fields
