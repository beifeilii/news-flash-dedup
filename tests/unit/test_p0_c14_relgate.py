# -*- coding: utf-8 -*-
"""2026-10-10 P0 收口包二③红测：宪章 C14/T-3 相对时间无锚文本自证闸
（主窗口裁定=选B）。

出处=判定宪章-草案-v2-1009 C14（"即使两条正文完全相同也先出边界……不得
用系统接收时间/发布时间替其补日期"，policy_v2 生效宪法）+ 主窗口裁定：
①文本自证闸按 A 方案落（相对词族在场+无共同绝对锚→证书路不直签转边界，
不分发布日——异日对被 pair_compare._check_binding:221 拦在证书路外，
"同日可签"腿明文作废）；②词族补员追认=machine_verify.RELATIVE_TIME_SPAN_RE
"过去N小时/N天/N周"短语族（G20 生产实证形态，补员属追认非新造；
value_time.py 不动，本闸不依赖它解析该族）；③红测四枚=G20 型→边界 /
同文+相对词+共同绝对锚→照签 / 同文无相对词→照签 / 空壳照边界。

词表单一来源=判官既有件 decide.machine_verify（证书路延迟导入消费同语义
同词表，别新造）。夹具纪律同 test_p0_shell_strip.py：全真实现类全链，
G20 用 T 冻结实测原文（P0 验收一冻结清单 log\\temp\\
p0acc-sametext30-pairs.json 逐字），数据面复跑证据=log\\temp\\
p0acc-sametext30-on-relgate.json（双开 25 签/5 边界：G20 依法翻转）。
"""

from __future__ import annotations

import hashlib

import pytest

from news_flash_dedup.compare import (
    aggregate as aggregate_module,
    p15_integration,
    pair_alignment,
    pair_compare,
)
from news_flash_dedup.compare.aggregate import CoverageStatus, FrozenRecallPlan
from news_flash_dedup.compare.p15_integration import (
    SHELL_STRIP_ENV,
    _relative_time_anchor_blocked,
)
from news_flash_dedup.decide.machine_verify import (
    RELATIVE_TIME_SPAN_RE,
    RELATIVE_TIME_TOKENS,
    machine_relative_time_anchor_check,
    relative_time_tokens_in,
)
from news_flash_dedup.decide.service import _wrap_facts_as_report
from news_flash_dedup.facts import rule as facts_rule

RECORD_ID_H = "a" * 64
RECORD_ID_C = "b" * 64

# —— T 冻结 G20 实测原文（67 字，"过去24小时"×2、零绝对日期、vc=False 锁①）——
G20 = ("比特币价格跌至77928.5美元，过去24小时内下跌1.88%；"
       "以太坊跌至2451.48美元，过去24小时内下跌1.94%。（产联社）")
# 相对词+共同绝对锚（38 裸字，"今日"+9月4日）
REL_WITH_ANCHOR = "9月4日，甲公司今日宣布完成股份回购计划，涉及资金约1200万元。（产联社）"
# 无相对词（28 裸字）
NO_REL = "甲公司宣布完成股份回购计划，涉及资金约1200万元。（产联社）"
# 合格完备+相对词无锚（旧路钉：vc=True 老路本签，闸压）
QUALIFIED_REL = "甲公司今日完成回购。"
QUALIFIED_NO_REL = "甲公司完成回购。"
SHELL = "（产联社）"


def _ctx(*, record_id, item_id, text, arrival_seq):
    return {"record_id": record_id, "item_id": item_id, "text": text,
            "raw_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "scope_id": "default", "business_date": "2026-09-26",
            "arrival_seq": arrival_seq, "pipeline_version": "dedup_v1"}


def _run(history_text, current_text):
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
    return history, current, p15, pair, out


def _both_on(monkeypatch):
    monkeypatch.setenv("DEDUP_CERT_DECOUPLE", "1")
    monkeypatch.setenv(SHELL_STRIP_ENV, "1")


def _assert_blocked_to_boundary(p15, pair, out):
    assert p15.p15_results.equivalence_ready is False
    assert p15.p15_results.text_proof is None
    assert any(i.code == "TIME_RELATION_UNCERTAIN" for i in p15.p15_results.issues)
    assert pair.outcome == "unresolved"
    assert out.decision != "重复"
    assert out.duplicate_ids == ()


# ---------- 词族补员单元钉（policy_v2-C14 追认，粒度闭集语义从严） ----------

def test_span_family_hits_and_closed_granularity():
    assert relative_time_tokens_in("过去24小时内下跌1.88%") == ("过去24小时",)
    assert relative_time_tokens_in("过去3天成交量放大") == ("过去3天",)
    assert relative_time_tokens_in("过去两周累计上涨") == ("过去两周",)
    assert relative_time_tokens_in("过去半小时内") == ("过去半小时",)
    assert relative_time_tokens_in("过去1.5小时") == ("过去1.5小时",)
    # 粒度闭集=小时/天/周（月/年未呈裁不收）；无数词不成族
    assert relative_time_tokens_in("过去一年") == ()
    assert relative_time_tokens_in("过去一个月") == ()
    assert relative_time_tokens_in("过去的交易日") == ()
    assert relative_time_tokens_in("未来24小时") == ()
    # 字面值表一字未动照常命中；混排同槽出证、排序确定性
    assert relative_time_tokens_in("今日央行降准") == ("今日",)
    assert relative_time_tokens_in("今日过去24小时") == ("今日", "过去24小时")
    assert RELATIVE_TIME_TOKENS == ("今日", "昨日", "当年", "当日", "次日",
                                    "明日", "今年")


def test_machine_check_with_span_family():
    chk = machine_relative_time_anchor_check(G20, G20)
    assert chk["block"] is True
    assert chk["relative_tokens_a"] == ["过去24小时"]
    assert chk["shared_absolute_anchor"] is False
    # 族词+共同绝对锚 → 不拦（锚在=正文自证可解）
    anchored = "9月4日公布，过去24小时甲公司成交量放大。"
    chk2 = machine_relative_time_anchor_check(anchored, anchored)
    assert chk2["block"] is False
    assert chk2["relative_tokens_a"] == ["过去24小时"]
    assert chk2["shared_absolute_anchor"] is True


# ---------- 红测① G20 型：同文+"过去24小时"+无绝对锚 → 转边界不直签 ----------

def test_g20_real_text_blocked_both_on(monkeypatch):
    """①a 双开：G20 实测原文同文对——剥壳判据过（裸 67≥20）本可签发，
    C14 闸撤回证书→边界（②态 26 签中依法翻转身=收口包二③本体）。"""
    _both_on(monkeypatch)
    _, _, p15, pair, out = _run(G20, G20)
    assert _relative_time_anchor_blocked(G20, G20) is True
    _assert_blocked_to_boundary(p15, pair, out)


def test_g20_blocked_decouple_only_gate_switch_independent(monkeypatch):
    """①b 仅解耦开（剥壳关）：G20 裸 67≥50 本经 min_len 闸签发，闸照压——
    C14 闸无开关、不挂 DEDUP_EXACT_SHELL_STRIP（生效宪法非可选特性）。"""
    monkeypatch.setenv("DEDUP_CERT_DECOUPLE", "1")
    monkeypatch.delenv(SHELL_STRIP_ENV, raising=False)
    _, _, p15, pair, out = _run(G20, G20)
    _assert_blocked_to_boundary(p15, pair, out)


def test_g20_lossless_variant_blocked(monkeypatch):
    """①c 双开：G20 无损变体（CRLF/LF）同压——闸对 EXACT/LOSSLESS 一视同仁。"""
    _both_on(monkeypatch)
    history_text = G20.replace("。", "。\r\n", 1)
    current_text = G20.replace("。", "。\n", 1)
    _, _, p15, pair, out = _run(history_text, current_text)
    _assert_blocked_to_boundary(p15, pair, out)


# ---------- 红测② 同文+相对词+共同绝对锚 → 照签 ----------

def test_relative_with_shared_anchor_signs(monkeypatch):
    """② 双开：同文+"今日"+共同绝对锚（9月4日双侧同载）→锚在=正文自证
    可解，闸不拦→签发 EXACT→全链重复。"""
    _both_on(monkeypatch)
    assert _relative_time_anchor_blocked(REL_WITH_ANCHOR, REL_WITH_ANCHOR) is False
    _, _, p15, pair, out = _run(REL_WITH_ANCHOR, REL_WITH_ANCHOR)
    assert relative_time_tokens_in(REL_WITH_ANCHOR) == ("今日",)
    assert p15.p15_results.text_proof == "EXACT_TEXT_MATCH"
    assert pair.outcome == "equivalent"
    assert out.decision == "重复"
    assert out.duplicate_ids == ("item-H",)


# ---------- 红测③ 同文无相对词 → 照签 ----------

def test_no_relative_word_signs(monkeypatch):
    """③ 双开：同文零相对词零绝对日期→闸不触→签发 EXACT→全链重复
    （闸不误伤无相对词同文对=26/4 基本盘不动的解析钉）。"""
    _both_on(monkeypatch)
    assert relative_time_tokens_in(NO_REL) == ()
    _, _, p15, pair, out = _run(NO_REL, NO_REL)
    assert p15.p15_results.text_proof == "EXACT_TEXT_MATCH"
    assert pair.outcome == "equivalent"
    assert out.decision == "重复"


def test_killed19_and_long2_carry_no_relative_tokens():
    """③b 解析钉：T 冻结 19 对被误杀对+2 对 ≥50 对照（G03/G24）全不含
    相对词族→③闸对 26 签基本盘零扰动，双开复跑期望=26-1(G20)=25 签。"""
    from tests.unit.test_p0_shell_strip import KILLED19, LONG2
    for gid, text in {**KILLED19, **LONG2}.items():
        assert relative_time_tokens_in(text) == (), f"{gid} 含相对词：{text}"
        assert _relative_time_anchor_blocked(text, text) is False
    assert relative_time_tokens_in(G20) == ("过去24小时",)


# ---------- 红测④ 空壳照边界 ----------

def test_shell_stays_boundary_under_gate(monkeypatch):
    """④ 双开：空壳同文对——无相对词（闸不触）但剥壳判据不过+无事实
    载体→照边界（②判据与③闸正交，各自执法互不洇渗）。"""
    _both_on(monkeypatch)
    assert relative_time_tokens_in(SHELL) == ()
    _, _, p15, pair, out = _run(SHELL, SHELL)
    assert p15.p15_results.text_proof is None
    assert not any(i.code == "TIME_RELATION_UNCERTAIN"
                   for i in p15.p15_results.issues)
    assert pair.outcome == "unresolved"
    assert out.decision != "重复"


# ---------- 旧路（双关）闸效：合格完备+相对词无锚本签→压；无相对词照签 ----------

def test_old_path_qualified_relative_blocked(monkeypatch):
    """旧路钉 a：双关+双侧完备合格同文"今日"无锚——老合格门本可签发，
    C14 闸对旧路同法压制（闸挂证书签发点不分新旧路）。"""
    monkeypatch.delenv("DEDUP_CERT_DECOUPLE", raising=False)
    monkeypatch.delenv(SHELL_STRIP_ENV, raising=False)
    history, current, p15, pair, out = _run(QUALIFIED_REL, QUALIFIED_REL)
    assert history.validated_complete is True            # 老路合格门本过
    assert current.validated_complete is True
    _assert_blocked_to_boundary(p15, pair, out)


def test_old_path_qualified_no_relative_still_signs(monkeypatch):
    """旧路钉 b：双关+双侧完备合格同文零相对词→证书照常签发、闸不触
    （无 TIME_RELATION_UNCERTAIN 洇渗）。对级结局属旧序 issues 先判
    领地（现役逐字节），非本闸语义，不钉。"""
    monkeypatch.delenv("DEDUP_CERT_DECOUPLE", raising=False)
    monkeypatch.delenv(SHELL_STRIP_ENV, raising=False)
    history, _, p15, _, _ = _run(QUALIFIED_NO_REL, QUALIFIED_NO_REL)
    assert history.validated_complete is True
    assert p15.p15_results.text_proof == "EXACT_TEXT_MATCH"
    assert p15.p15_results.equivalence_ready is True
    assert not any(i.code == "TIME_RELATION_UNCERTAIN"
                   for i in p15.p15_results.issues)
