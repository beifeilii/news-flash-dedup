# -*- coding: utf-8 -*-
"""2026-10-09 P0-b 红测：EXACT/LOSSLESS 文本证书与完备性闸解耦
（DEDUP_CERT_DECOUPLE 默认关 + DEDUP_EXACT_MIN_LEN 默认 50）。

出处=log\\temp\\判定链修复方案-P0施工单-呈外部评审.md §一-P0-b（六份外部
评审+主控亲核坐实的锁①：text/__init__.py 证书签发要求双侧工件
validated_complete，T 数据逐字节同文对 30/30 全边界实证）：
- 开关关=老行为逐字节（同文但工件不完备→无证书）；
- 开关开=同文长文（≥50 归一字符）→签发 EXACT；
- 开关开但正文<50 字→不签（不足长度仍走老路：双侧 qualified 照常可签）；
- LOSSLESS 同法；
- FACT_EQUIVALENT 通道完备性要求一字不动（开关开不洗白）。

夹具纪律（施工单④）：全真实现类——facts/rule.py 真规则抽取 +
decide/service.py _wrap_facts_as_report 真校验包装（validate_fact_artifact
同一校验器）+ build_aligned/extract_p15_results/compare_pair/aggregate
真链路；零手写假字段。探针实证=log\\temp\\p0-1009-probe-chain.py：
long_identical 双侧 vc=False 且 p15 issues/time_pairs 均空（证书一解耦
全链通"重复"）；short_complete vc=True 老路本可签。
"""

from __future__ import annotations

import hashlib

import pytest

from news_flash_dedup import text as text_mod
from news_flash_dedup.compare import (
    aggregate as aggregate_module,
    p15_integration,
    pair_alignment,
    pair_compare,
)
from news_flash_dedup.compare.aggregate import CoverageStatus, FrozenRecallPlan
from news_flash_dedup.decide.service import _wrap_facts_as_report
from news_flash_dedup.facts import rule as facts_rule

RECORD_ID_H = "a" * 64
RECORD_ID_C = "b" * 64

# 长文（57 归一字符 ≥50）：真规则抽取 2 facts、validated_complete=False
# （"1200万"数值无动词子句承载→FACT_INCOMPLETE），同文对 p15 issues 空。
LONG = ("甲公司今日宣布完成股份回购计划，本次回购旨在提升股东价值并优化资本结构。"
        "公司另有备用金1200万元未列入本次计划。")
# 短文双侧完备（8 字符 <50，老路本可签）；短文不完备（18 字符 <50）。
SHORT_COMPLETE = "甲公司完成回购。"
SHORT_INCOMPLETE = "甲公司完成回购。备用金1200万元。"
# 长文释义对（文本相异、双侧不完备）：FACT_EQUIVALENT 通道专用。
PARA_H = ("甲公司今日宣布回购股份，旨在提升股东价值并优化资本结构。"
          "公司另有备用金1200万元未列入本次计划。")
PARA_C = ("甲公司发布公告称回购股份，旨在提升股东价值并优化资本结构。"
          "公司另有备用金1200万元未列入本次计划。")


def _ctx(*, record_id, item_id, text, arrival_seq):
    return {"record_id": record_id, "item_id": item_id, "text": text,
            "raw_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "scope_id": "default", "business_date": "2026-09-26",
            "arrival_seq": arrival_seq, "pipeline_version": "dedup_v1"}


def _run(history_text, current_text):
    """真抽取 → 真校验包装 → build_aligned → extract_p15_results →
    compare_pair → aggregate 全链（test_p15_text_certificate._run 同型）。"""
    history = _wrap_facts_as_report(
        RECORD_ID_H, history_text,
        facts_rule.extract_facts(RECORD_ID_H, history_text))
    current = _wrap_facts_as_report(
        RECORD_ID_C, current_text,
        facts_rule.extract_facts(RECORD_ID_C, current_text))
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
    return p15, pair, out


# ---------- 开关解析（严格、默认关） ----------

def test_cert_decouple_switch_parsing_strict_default_off():
    assert text_mod.cert_decouple_enabled({}) is False
    assert text_mod.cert_decouple_enabled({"DEDUP_CERT_DECOUPLE": ""}) is False
    assert text_mod.cert_decouple_enabled({"DEDUP_CERT_DECOUPLE": "1"}) is True
    # 大小写变体/近形值一律关（fail-closed 不放大）
    for bad in ("0", "true", "True", "ON", "on", "yes", " 1", "1 ", "2"):
        assert text_mod.cert_decouple_enabled(
            {"DEDUP_CERT_DECOUPLE": bad}) is False


def test_exact_min_len_parsing():
    assert text_mod.exact_min_len({}) == 50
    assert text_mod.exact_min_len({"DEDUP_EXACT_MIN_LEN": ""}) == 50
    assert text_mod.exact_min_len({"DEDUP_EXACT_MIN_LEN": "80"}) == 80
    assert text_mod.exact_min_len({"DEDUP_EXACT_MIN_LEN": "0"}) == 0
    for bad in ("abc", "12.5", "-1", " 50", "50 ", "５０"):
        with pytest.raises(ValueError):
            text_mod.exact_min_len({"DEDUP_EXACT_MIN_LEN": bad})


# ---------- ① 开关关=老行为逐字节 ----------

def test_switch_off_identical_long_unqualified_no_certificate(monkeypatch):
    """① 开关关：同文长文但双侧工件不完备 → 无证书（老行为逐字节，
    T 数据 30/30 同文对全边界实证的现役锁①形态）。"""
    monkeypatch.delenv("DEDUP_CERT_DECOUPLE", raising=False)
    monkeypatch.delenv("DEDUP_EXACT_MIN_LEN", raising=False)
    p15, pair, out = _run(LONG, LONG)
    assert len(LONG) >= 50
    assert p15.p15_results.equivalence_ready is False
    assert p15.p15_results.text_proof is None
    assert pair.outcome == "unresolved"
    assert out.decision != "重复"
    assert out.duplicate_ids == ()


# ---------- ② 开关开=同文长文签发 EXACT ----------

def test_switch_on_identical_long_signs_exact_certificate(monkeypatch):
    """② 开关开：同文长文（≥50 归一字符）不再要求工件完备 → 签发
    EXACT_TEXT_MATCH；本对 p15 issues 空（探针实证）→ 全链 equivalent→重复。"""
    monkeypatch.setenv("DEDUP_CERT_DECOUPLE", "1")
    p15, pair, out = _run(LONG, LONG)
    assert p15.p15_results.equivalence_ready is True
    assert p15.p15_results.text_proof == "EXACT_TEXT_MATCH"
    assert pair.outcome == "equivalent"
    assert pair.code == "EXACT_TEXT_MATCH"
    assert out.decision == "重复"
    assert out.duplicate_ids == ("item-H",)


# ---------- ③ 开关开但正文<50 字→不签（不足长度仍走老路） ----------

def test_switch_on_short_body_below_gate_not_signed(monkeypatch):
    """③a 开关开+短文（18 归一字符 <50）且工件不完备 → 长度闸拦截回落
    老路 → 合格门拒 → 不签。"""
    monkeypatch.setenv("DEDUP_CERT_DECOUPLE", "1")
    p15, _, out = _run(SHORT_INCOMPLETE, SHORT_INCOMPLETE)
    assert len(SHORT_INCOMPLETE) < 50
    assert p15.p15_results.equivalence_ready is False
    assert p15.p15_results.text_proof is None
    assert out.decision != "重复"


def test_switch_on_short_body_qualified_still_signed_via_old_path(monkeypatch):
    """③b 开关开+短文（8 归一字符 <50）但双侧工件完备 → 回落老路合格门
    放行 → 照常签发 EXACT（"不足长度仍走老路"的双向钉：闸只拦截不完备者）。"""
    monkeypatch.setenv("DEDUP_CERT_DECOUPLE", "1")
    p15, _, _ = _run(SHORT_COMPLETE, SHORT_COMPLETE)
    assert len(SHORT_COMPLETE) < 50
    assert p15.p15_results.equivalence_ready is True
    assert p15.p15_results.text_proof == "EXACT_TEXT_MATCH"


def test_switch_on_custom_min_len_threshold_honored(monkeypatch):
    """③c DEDUP_EXACT_MIN_LEN 可调：阈值降为 10 时 18 字符短文过闸 →
    不完备工件亦签发（阈值语义实证，非建议配置）。"""
    monkeypatch.setenv("DEDUP_CERT_DECOUPLE", "1")
    monkeypatch.setenv("DEDUP_EXACT_MIN_LEN", "10")
    p15, _, _ = _run(SHORT_INCOMPLETE, SHORT_INCOMPLETE)
    assert p15.p15_results.equivalence_ready is True
    assert p15.p15_results.text_proof == "EXACT_TEXT_MATCH"


# ---------- ④ LOSSLESS 同法 ----------

def test_switch_on_lossless_crlf_signs_lossless_certificate(monkeypatch):
    """④ 开关开：CRLF/LF 无损对（归一后等值、58 归一字符 ≥50、双侧工件
    不完备）→ 签发 LOSSLESS_TEXT_MATCH → 全链 equivalent→重复。"""
    monkeypatch.setenv("DEDUP_CERT_DECOUPLE", "1")
    history_text = LONG.replace("。", "。\r\n", 1)
    current_text = LONG.replace("。", "。\n", 1)
    assert history_text != current_text
    p15, pair, out = _run(history_text, current_text)
    assert p15.p15_results.equivalence_ready is True
    assert p15.p15_results.text_proof == "LOSSLESS_TEXT_MATCH"
    assert pair.outcome == "equivalent"
    assert pair.code == "LOSSLESS_TEXT_MATCH"
    assert out.decision == "重复"
    assert out.duplicate_ids == ("item-H",)


def test_switch_off_lossless_unqualified_no_certificate(monkeypatch):
    """④b 开关关：同一无损对双侧不完备 → 不签（老行为对照）。"""
    monkeypatch.delenv("DEDUP_CERT_DECOUPLE", raising=False)
    p15, _, out = _run(LONG.replace("。", "。\r\n", 1),
                       LONG.replace("。", "。\n", 1))
    assert p15.p15_results.equivalence_ready is False
    assert p15.p15_results.text_proof is None
    assert out.decision != "重复"


# ---------- ⑤ FACT_EQUIVALENT 通道完备性要求一字不动 ----------

@pytest.mark.parametrize("switch", [None, "1"])
def test_fact_equivalent_completeness_requirement_untouched(monkeypatch, switch):
    """⑤ 长文释义对（文本相异、双侧工件不完备）：开关关/开行为一致——
    无文本证书、FACT_EQUIVALENT 不因开关洗白（完备性要求一字不动）。"""
    if switch is None:
        monkeypatch.delenv("DEDUP_CERT_DECOUPLE", raising=False)
    else:
        monkeypatch.setenv("DEDUP_CERT_DECOUPLE", switch)
    p15, pair, out = _run(PARA_H, PARA_C)
    assert p15.p15_results.equivalence_ready is False
    assert p15.p15_results.text_proof is None
    assert pair.outcome == "unresolved"
    assert out.decision != "重复"
    assert out.duplicate_ids == ()
