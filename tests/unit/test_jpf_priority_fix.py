# -*- coding: utf-8 -*-
"""2026-10-11 主窗令·判定优先级修复（拆分提交）配套测试。

commit1（bde65e9）范围=证书直签路小件 ①②+补充令 ③（T03 空文陷阱）：
① 来源壳闭表扩项（界面新闻快讯；用户验收五条件）；
② 独立证书码 SHELL_STRIPPED_TEXT_MATCH（与 EXACT/普通 LOSSLESS 三分立，
   审计可单独统计误判面）；
③ T03 空文陷阱（剥壳后空串/纯尾注/堆叠尾注——裸长地板必须拦住不签）。

commit2（本提交）范围=小件 ③+补充令 ②（C14 存活性对抗钉，最高优先）：
③ C14 同日同文放行：同 business_date+原文完全一致（raw 逐字相等）不受
   C14 相对时间撤证；跨日/非同文（含 LOSSLESS 变体/异尾注）维持原闸；
② 对抗钉：跨日同文+相对词（今日/昨日/刚刚）必须不签；缺工件时相对
   时间检测仍在原文词面运行、撤证留痕码 fail-closed（注：仓内注册未决
   码=TIME_RELATION_UNCERTAIN，无 TIME_ANCHOR_UNRESOLVED 码——按实际
   码钉，裁量呈主窗）；DECOUPLE 作用域=仅豁免文本相等证书工件门，
   其他读工件路径缺字段 fail-closed（禁止 None==None 进相等比较）。

目标案例=T 冻结 31 对核查表（log\\待人工核查-判官腿召回损失31对-2026-10-11.md）
中"正文逐字相同（含同尾注）13 对 + 同正文异尾注 1 对（#7 产联社 vs 界面
新闻快讯）"=14 对：前置直签+LLM 调用 0。

夹具纪律（同 test_p0_shell_strip.py / test_p0_c14_relgate.py）：全真实现类
——facts/rule.py 真规则抽取（零真 LLM）+ _wrap_facts_as_report 真校验包装
+ build_aligned/extract_p15_results/compare_pair/aggregate/decide_for_task
真全链；判官与 LLM 抽取均为 boom 件（被调用即 AssertionError 失败）。
环境=用户裁定 B 方案环境开（DEDUP_CERT_DECOUPLE=1+DEDUP_EXACT_SHELL_STRIP
=1；默认值零改动）。
"""

from __future__ import annotations

import dataclasses
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
from news_flash_dedup.compare.p15_integration import (
    SHELL_STRIP_ENV,
    SHELL_STRIP_RULE_VERSION,
    certify_shell_stripped_equality,
)
from news_flash_dedup.decide import service as decide_service
from news_flash_dedup.decide import types as decide_types
from news_flash_dedup.decide.service import _wrap_facts_as_report
from news_flash_dedup.facts import rule as facts_rule

RECORD_ID_H = "a" * 64
RECORD_ID_C = "c" * 64
T1 = "（产联社）"
T2 = "（界面新闻快讯）"
D = "2026-09-26"

# —— T 冻结 31 对核查表逐字（13 对正文逐字相同含同尾注 → EXACT 前置直签）——
GOLD_SAME13 = {
    "P09": ("9日，国际油价显著上涨。纽约商品交易所10月交货的轻质原油期货价格"
            "上涨3.02美元，收于每桶96.05美元，涨幅为3.25%；11月交货的伦敦布伦特"
            "原油期货价格上涨3.29美元，收于每桶101.21美元，涨幅为3.36%。（产联社）"),
    "P10": ("纳斯达克中国金龙指数收跌2.08%。闪送跌12.23%，天演药业跌6.88%，"
            "HTT跌5.66%，小马智行跌5.71%，爱奇艺跌5.12%。（产联社）"),
    "P12": "韩国KOSPI指数日内跌幅达2.00%。（产联社）",
    "P13": "创业板指下挫跌逾1%，沪指下跌0.28%，沪深京三市下跌个股近4200只。（产联社）",
    "P15": "创业板指翻红，此前一度跌超1%。沪指现跌0.16%，深证成指跌0.23%。（产联社）",
    "P16": ("美国30年期国债收益率升至5.35%，日内上涨6个基点，"
            "续创2007年6月以来新高。（产联社）"),
    "P17": "日经225指数跌幅扩大至1%。（产联社）",
    "P18": ("欧洲央行表示，其利率决策将尤其基于以下因素做出：结合最新经济与金融"
            "数据，对通胀前景及其相关风险的评估，同时还会考量核心通胀的走势以及"
            "货币政策传导的强度。（产联社）"),
    "P20": "纳斯达克100指数期货跌幅扩大至0.52%，报29295.98点。（产联社）",
    "P23": "WTI原油期货涨幅扩大至4%，报99.89美元/桶。（产联社）",
    "P24": "布伦特原油期货和美国原油期货结算价收于5月22日以来的最高水平。（产联社）",
    "P28": "恒指期货夜盘收跌0.90%，报24980.16点，低水294.80点。（产联社）",
    "P30": ("欧元斯托克50指数期货上涨0.25%，德国DAX指数期货上涨0.22%，"
            "英国富时指数期货下跌0.08%。（产联社）"),
}
# #7 同正文异尾注（判官腿召回损失面 → SHELL_STRIPPED 前置直签）
P07_A = ("日经225指数收涨0.20%，报65270.95点。韩国综指收跌0.24%，"
         "报7034.87点。（产联社）")
P07_B = ("日经225指数收涨0.20%，报65270.95点。韩国综指收跌0.24%，"
         "报7034.87点。（界面新闻快讯）")

BODY_MULTI = "甲公司完成股份回购计划，涉及资金约一千二百万元。"
# —— C14 专项（commit2；判官窗甲忠告+补充令 ② 对抗钉）——
# G20=T 冻结实测原文（"过去24小时"×2、零绝对日期——曾依法撤证面）。
G20 = ("比特币价格跌至77928.5美元，过去24小时内下跌1.88%；"
       "以太坊跌至2451.48美元，过去24小时内下跌1.94%。（产联社）")
BODY_REL = "甲公司今日公告完成股份回购计划，涉及资金约一千二百万元。"
# 缺工件+相对词对抗件（补充令 ②-2）：真规则抽取 vc=False（1200万无动词
# 子句承载）+今日无绝对锚——检测在原文词面运行的实证载体。
LONG_REL = ("甲公司今日宣布完成股份回购计划，本次回购旨在提升股东价值并"
            "优化资本结构。公司另有备用金1200万元未列入本次计划。")
# 不完备工件面（test_p0_cert_decouple.LONG 同源）：真规则抽取 vc=False
# （"1200万"数值无动词子句承载→FACT_INCOMPLETE）——"同文 Fact 缺失仍直签"
# 的真缺口形态（零 facts 侧在 build_aligned 入口即被对齐纪律拦，非本面）。
LONG_INCOMPLETE = ("甲公司宣布完成股份回购计划，本次回购旨在提升股东价值并"
                   "优化资本结构。公司另有备用金1200万元未列入本次计划。")
# T03 堆叠尾注反例（补充令 ③）：非闭表壳（财联社/财联社快讯）堆叠+闭表尾。
STACKED = "（财联社）（财联社快讯）。（产联社）"
STACKED_B = "（财联社）（财联社快讯）。（界面新闻快讯）"
# 长文释义对（文本相异、双侧不完备）：FACT_EQUIVALENT 通道对照。
PARA_H = ("甲公司宣布回购股份，旨在提升股东价值并优化资本结构。"
          "公司另有备用金1200万元未列入本次计划。")
PARA_C = ("甲公司发布公告称回购股份，旨在提升股东价值并优化资本结构。"
          "公司另有备用金1200万元未列入本次计划。")


class _BoomExtractor:
    """①b 冻结接口（llm_facts 鸭子协议）boom 件：被调用即失败留痕。"""

    def __init__(self):
        self.calls: list = []

    def extract(self, record_id, text):
        self.calls.append((record_id, text))
        raise AssertionError("LLM 抽取不得被调用（Hash/剥壳前置直签）")


def _boom_judge():
    judge_calls: list = []

    def judge(pair_context):
        judge_calls.append(pair_context["pair_id"])
        raise AssertionError("判官不得被调用（前置直签，判径段不进）")

    return judge, judge_calls


def _env_on(monkeypatch):
    """用户裁定 B 方案环境开（默认值零改动——仅测试内 monkeypatch）。"""
    monkeypatch.setenv("DEDUP_CERT_DECOUPLE", "1")
    monkeypatch.setenv(SHELL_STRIP_ENV, "1")
    monkeypatch.setenv("DEDUP_JUDGE_DECISION_MODE", "semantic_authority")


def _record(rid, item, text, seq, *, facts=None, scope_id="default",
            business_date=D):
    rec = {"record_id": rid, "item_id": item, "text": text,
           "arrival_seq": seq, "scope_id": scope_id,
           "business_date": business_date, "pipeline_version": "dedup_v1"}
    rec["facts"] = (facts_rule.extract_facts(rid, text)
                    if facts is None else facts)
    return rec


def _decide(history, current, monkeypatch):
    """全真 decide 链（boom 判官+boom LLM 抽取=零真 LLM 纪律）——直签对
    专用：判径段结构性不进，boom 未触即证 LLM 调用 0。"""
    _env_on(monkeypatch)
    extractor = _BoomExtractor()
    judge, judge_calls = _boom_judge()
    out = decide_service.decide_for_task(
        history, (), current=current,
        judge_callable=judge, judge_in_chain=True,
        coverage_complete=True, llm_facts=extractor)
    return out, extractor.calls, judge_calls


def _decide_ruleonly(history, current, monkeypatch):
    """纯规则路（判官/LLM 抽取均不注入）——边界对照对专用：测量文本
    证书禁签面（判径段注入与否与证书禁签正交）。"""
    _env_on(monkeypatch)
    out = decide_service.decide_for_task(
        history, (), current=current,
        judge_callable=None, judge_in_chain=True,
        coverage_complete=True, llm_facts=None)
    return out


def _p15(history_text, current_text, *, dates=(None, None),
         facts_h=None, facts_c=None):
    """p15 层直调（证书路/撤证闸面），dates=(history_date, current_date)
    ——缺省 (None, None)=既有直接调用方形态（不豁免）。"""
    history = _wrap_facts_as_report(
        RECORD_ID_H, history_text,
        facts_rule.extract_facts(RECORD_ID_H, history_text)
        if facts_h is None else facts_h)
    current = _wrap_facts_as_report(
        RECORD_ID_C, current_text,
        facts_rule.extract_facts(RECORD_ID_C, current_text)
        if facts_c is None else facts_c)
    alignment = pair_alignment.build_aligned(
        history, current, history_text, current_text,
        dictionary_version="dict_v1", alignment_version="alignment_v1",
    )
    return p15_integration.extract_p15_results(
        history, current, history_text, current_text, alignment,
        history_business_date=dates[0], current_business_date=dates[1],
    )


def _ctx(*, record_id, item_id, text, arrival_seq, scope_id="default"):
    return {"record_id": record_id, "item_id": item_id, "text": text,
            "raw_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "scope_id": scope_id, "business_date": D,
            "arrival_seq": arrival_seq, "pipeline_version": "dedup_v1"}


def _pair_outcome(p15_report, history_text, current_text):
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
    history_ctx = _ctx(record_id=RECORD_ID_H, item_id="item-H",
                       text=history_text, arrival_seq=1)
    current_ctx = _ctx(record_id=RECORD_ID_C, item_id="item-C",
                       text=current_text, arrival_seq=2)
    pair = pair_compare.compare_pair(
        history_ctx, current_ctx,
        history_artifact=history, current_artifact=current,
        alignment=alignment, pipeline_version="dedup_v1",
        p15_results=p15_report.p15_results,
    )
    plan = FrozenRecallPlan(version="rrf_v1", required={RECORD_ID_H: history_ctx})
    out = aggregate_module.aggregate(
        current_ctx, plan, [pair],
        coverage=CoverageStatus(visible_seq=10, prepared_seq=10, complete=True),
    )
    return pair, out


# ============ §一 14 对目标案例：前置直签 + LLM 调用 0 ============

_SAME14 = [(pid, text, text) for pid, text in GOLD_SAME13.items()]
_GOLD14 = _SAME14 + [("P07", P07_A, P07_B)]


@pytest.mark.parametrize("pid,ta,tb", _GOLD14, ids=[p for p, _, _ in _GOLD14])
def test_gold14_direct_sign_zero_llm(pid, ta, tb, monkeypatch):
    """14 对金标（13 同文+1 同正文异尾注）：证书前置直签 → 重复，判官/LLM
    抽取零调用（boom 件未触即证）。"""
    out, ext_calls, judge_calls = _decide(
        _record(RECORD_ID_H, "item-A", ta, 1),
        _record(RECORD_ID_C, "item-C", tb, 3), monkeypatch)
    assert out.decision == "重复"
    assert out.duplicate_ids == ("item-A",)
    assert out.internal_code in ("EXACT_TEXT_MATCH", "SHELL_STRIPPED_TEXT_MATCH")
    assert ext_calls == []
    assert judge_calls == []


def test_gold_p07_uses_independent_shell_code(monkeypatch):
    """#7 同正文异尾注 → 独立证书码 SHELL_STRIPPED_TEXT_MATCH（审计可单独
    统计误判面；与 EXACT 三分立的实证）。"""
    out, ext_calls, judge_calls = _decide(
        _record(RECORD_ID_H, "item-A", P07_A, 1),
        _record(RECORD_ID_C, "item-C", P07_B, 3), monkeypatch)
    assert out.internal_code == "SHELL_STRIPPED_TEXT_MATCH"
    assert out.pair_codes[RECORD_ID_H] == "SHELL_STRIPPED_TEXT_MATCH"
    assert out.decision == "重复"
    assert ext_calls == [] and judge_calls == []


# ============ §二 同文 Fact 缺失（不完备工件）仍直签 ============

def test_same_text_incomplete_facts_still_direct_sign(monkeypatch):
    """同文长文+双侧工件不完备（真规则抽取 vc=False：数值无动词子句承载）
    → 文本相等本身是判定依据，直签不受工件完备性影响（LLM/判官零调用）。"""
    history = _record(RECORD_ID_H, "item-A", LONG_INCOMPLETE, 1)
    current = _record(RECORD_ID_C, "item-C", LONG_INCOMPLETE, 3)
    h_report = _wrap_facts_as_report(
        RECORD_ID_H, LONG_INCOMPLETE, history["facts"])
    c_report = _wrap_facts_as_report(
        RECORD_ID_C, LONG_INCOMPLETE, current["facts"])
    assert h_report.validated_complete is False      # 真缺口形态锁（Fact 缺失）
    assert c_report.validated_complete is False
    out, ext_calls, judge_calls = _decide(history, current, monkeypatch)
    assert out.decision == "重复"
    assert out.internal_code == "EXACT_TEXT_MATCH"
    assert ext_calls == [] and judge_calls == []


def test_diff_tail_incomplete_facts_shell_direct_sign(monkeypatch):
    """同正文异尾注+双侧工件不完备 → 剥壳证书直签（Fact 缺失不阻文本
    证书路）。"""
    history = _record(RECORD_ID_H, "item-A", LONG_INCOMPLETE + T1, 1)
    current = _record(RECORD_ID_C, "item-C", LONG_INCOMPLETE + T2, 3)
    out, ext_calls, judge_calls = _decide(history, current, monkeypatch)
    assert out.decision == "重复"
    assert out.internal_code == "SHELL_STRIPPED_TEXT_MATCH"
    assert ext_calls == [] and judge_calls == []


# ============ §三 C14 同日同文放行 + 留存专项 + 对抗钉（补充令 ②） ============

def test_relative_time_same_text_same_day_direct_sign(monkeypatch):
    """③ 同 business_date+原文完全一致（含相对词族"过去24小时"无绝对锚）
    → 不受 C14 撤证 → 直签（G20=T 冻结实测原文，曾依法撤证面）。"""
    out, ext_calls, judge_calls = _decide(
        _record(RECORD_ID_H, "item-A", G20, 1),
        _record(RECORD_ID_C, "item-C", G20, 3), monkeypatch)
    assert out.decision == "重复"
    assert out.internal_code == "EXACT_TEXT_MATCH"
    assert ext_calls == [] and judge_calls == []


def test_same_day_same_text_p15_certificate_kept(monkeypatch):
    """③ p15 面：同日同文 → 证书保留（无 TIME_RELATION_UNCERTAIN 洇渗）。"""
    _env_on(monkeypatch)
    p15 = _p15(G20, G20, dates=(D, D))
    assert p15.p15_results.text_proof == "EXACT_TEXT_MATCH"
    assert p15.p15_results.equivalence_ready is True
    assert not any(i.code == "TIME_RELATION_UNCERTAIN"
                   for i in p15.p15_results.issues)


def test_c14_retained_cross_day_same_text(monkeypatch):
    """留存专项① 跨日同文：dates 不同 → C14 照撤（证同日腿不放跨日）。"""
    _env_on(monkeypatch)
    p15 = _p15(G20, G20, dates=(D, "2026-09-27"))
    assert p15.p15_results.text_proof is None
    assert p15.p15_results.equivalence_ready is False
    assert any(i.code == "TIME_RELATION_UNCERTAIN" for i in p15.p15_results.issues)


def test_c14_retained_non_same_text_diff_tail(monkeypatch):
    """留存专项② 非同文（同正文异尾注+相对词"今日"无锚）→ 剥壳证书同受
    C14 撤证（证豁免窄=原文完全一致）。"""
    _env_on(monkeypatch)
    p15 = _p15(BODY_REL + T1, BODY_REL + T2, dates=(D, D))
    assert p15.p15_results.text_proof is None
    assert p15.p15_results.equivalence_ready is False
    assert any(i.code == "TIME_RELATION_UNCERTAIN" for i in p15.p15_results.issues)


def test_c14_retained_lossless_variant_not_exempt(monkeypatch):
    """留存专项③ 同日+LOSSLESS 变体（原文非逐字一致）→ 照撤（"原文完全
    一致"=raw 逐字相等，归一等值不享豁免）。"""
    _env_on(monkeypatch)
    h = G20.replace("；", "；\r\n")
    c = G20.replace("；", "；\n")
    p15 = _p15(h, c, dates=(D, D))
    assert p15.p15_results.text_proof is None
    assert any(i.code == "TIME_RELATION_UNCERTAIN" for i in p15.p15_results.issues)


def test_c14_dates_absent_defaults_to_blocked(monkeypatch):
    """留存专项④ 日期未传（既有直接调用方形态）→ 不豁免，C14 照撤（缺省
    行为逐字节不变钉）。"""
    _env_on(monkeypatch)
    p15 = _p15(G20, G20)      # dates=(None, None)
    assert p15.p15_results.text_proof is None
    assert any(i.code == "TIME_RELATION_UNCERTAIN" for i in p15.p15_results.issues)


# ---------- 补充令 ②：C14 存活性对抗钉（最高优先） ----------

def test_adversarial_cross_day_today_not_signed(monkeypatch):
    """②-1a 跨日同文+"今日"：p15 撤证（TIME_RELATION_UNCERTAIN fail-closed
    留痕）+ 全链跨日 PairBindingError——必须不签（双层实证）。"""
    _env_on(monkeypatch)
    p15 = _p15(BODY_REL, BODY_REL, dates=(D, "2026-09-27"))
    assert p15.p15_results.text_proof is None
    assert any(i.code == "TIME_RELATION_UNCERTAIN" for i in p15.p15_results.issues)
    with pytest.raises(pair_compare.PairBindingError):
        _decide_ruleonly(
            _record(RECORD_ID_H, "item-A", BODY_REL, 1),
            _record(RECORD_ID_C, "item-C", BODY_REL, 3,
                    business_date="2026-09-27"), monkeypatch)


def test_adversarial_cross_day_yesterday_not_signed(monkeypatch):
    """②-1b 跨日同文+"昨日"：同 ②-1a 双层不签（相对词表命中腿）。"""
    text_y = "甲公司昨日公告完成股份回购计划，涉及资金约一千二百万元。"
    _env_on(monkeypatch)
    p15 = _p15(text_y, text_y, dates=(D, "2026-09-27"))
    assert p15.p15_results.text_proof is None
    assert any(i.code == "TIME_RELATION_UNCERTAIN" for i in p15.p15_results.issues)
    with pytest.raises(pair_compare.PairBindingError):
        _decide_ruleonly(
            _record(RECORD_ID_H, "item-A", text_y, 1),
            _record(RECORD_ID_C, "item-C", text_y, 3,
                    business_date="2026-09-27"), monkeypatch)


def test_adversarial_cross_day_ganggang_not_signed(monkeypatch):
    """②-1c 跨日同文+"刚刚"：必须不签——跨日禁签由 pair_compare._check_
    binding 结构性保证（与词面无关）。（如实呈报：'刚刚'不在闭合相对词
    表 RELATIVE_TIME_TOKENS——该表为判官腿共用单源冻结面，扩员归主窗
    裁量，本令不擅动。）"""
    text_g = "甲公司刚刚公告完成股份回购计划，涉及资金约一千二百万元。"
    _env_on(monkeypatch)
    with pytest.raises(pair_compare.PairBindingError):
        _decide_ruleonly(
            _record(RECORD_ID_H, "item-A", text_g, 1),
            _record(RECORD_ID_C, "item-C", text_g, 3,
                    business_date="2026-09-27"), monkeypatch)


def test_adversarial_missing_artifacts_detection_runs_on_text(monkeypatch):
    """②-2 缺工件（双侧真抽取 vc=False）+相对词"今日"无锚：相对时间检测
    仍在**原文词面**运行（不读工件字段）→ 撤证留痕 fail-closed。"""
    _env_on(monkeypatch)
    h = LONG_REL.replace("。", "。\r\n")
    c = LONG_REL.replace("。", "。\n")     # LOSSLESS 变体：非同文（raw 不等）
    h_report = _wrap_facts_as_report(
        RECORD_ID_H, h, facts_rule.extract_facts(RECORD_ID_H, h))
    c_report = _wrap_facts_as_report(
        RECORD_ID_C, c, facts_rule.extract_facts(RECORD_ID_C, c))
    assert h_report.validated_complete is False      # 缺工件实证（双侧）
    assert c_report.validated_complete is False
    p15 = _p15(h, c, dates=(D, D))
    assert p15.p15_results.text_proof is None
    assert any(i.code == "TIME_RELATION_UNCERTAIN" for i in p15.p15_results.issues)


def test_adversarial_detection_text_only_by_construction():
    """②-2b 结构性钉：相对时间检测消费面=纯文本对（原文词面），工件零
    参与——缺工件不改变检测结果（今日无锚→block）。"""
    from news_flash_dedup.compare.p15_integration import (
        _relative_time_anchor_blocked,
    )
    assert _relative_time_anchor_blocked(BODY_REL, BODY_REL) is True
    assert _relative_time_anchor_blocked(BODY_MULTI, BODY_MULTI) is False


def test_adversarial_same_day_gate_none_guards(monkeypatch):
    """②-3 禁止 None==None 进相等比较：同日豁免门 is-not-None 先行守卫——
    单侧缺 (None,D)/(D,None) 皆不豁免（G20 同文对 C14 照撤）；缺省
    (None,None) 态由 test_c14_dates_absent 钉。"""
    _env_on(monkeypatch)
    p15 = _p15(G20, G20, dates=(None, D))
    assert p15.p15_results.text_proof is None
    assert any(i.code == "TIME_RELATION_UNCERTAIN" for i in p15.p15_results.issues)
    p15_b = _p15(G20, G20, dates=(D, None))
    assert p15_b.p15_results.text_proof is None
    assert any(i.code == "TIME_RELATION_UNCERTAIN"
               for i in p15_b.p15_results.issues)


def test_adversarial_zero_facts_fail_closed_not_none_equality(monkeypatch):
    """②-3b 缺工件 fail-closed：零 facts 侧 → build_aligned 入口
    PairAlignmentError（"必须至少一个 Fact"纪律），绝不"缺==缺"当相等
    放行签发（无 None==None 洗白通道）。"""
    _env_on(monkeypatch)
    with pytest.raises(pair_alignment.PairAlignmentError):
        _decide_ruleonly(
            _record(RECORD_ID_H, "item-A", LONG_REL, 1, facts=[]),
            _record(RECORD_ID_C, "item-C", LONG_REL, 3), monkeypatch)


# ============ §四 空文纯空白禁签 + T03 空文陷阱（补充令 ③） ============

@pytest.mark.parametrize("ta,tb", [
    ("", ""),
    ("   ", "   "),
    ("", "   "),
    ("（产联社）", "（界面新闻快讯）"),      # 纯尾注双侧 → 残文空禁签
    ("（产联社）（界面新闻快讯）", "（产联社）"),
], ids=["empty", "whitespace", "empty-vs-ws", "shell-only", "multi-shell"])
def test_empty_or_shell_only_never_certified(ta, tb):
    """空文/纯空白/纯尾注：indexable 空指纹或残文空 → 剥壳证书禁签；
    EXACT 原语同守（防伪证面双守）。"""
    left = text_mod.normalize_text(ta)
    right = text_mod.normalize_text(tb)
    assert certify_shell_stripped_equality(left, right) is None
    assert text_mod.certify_text_equality(
        left, right, left_qualified=True, right_qualified=True) is None


def test_shell_only_pair_stays_boundary(monkeypatch):
    """纯尾注对全链：无证书 → 边界（不冒签）。"""
    _env_on(monkeypatch)
    p15 = _p15("（产联社）", "（界面新闻快讯）")
    pair, out = _pair_outcome(p15, "（产联社）", "（界面新闻快讯）")
    assert p15.p15_results.text_proof is None
    assert pair.outcome == "unresolved"
    assert out.decision != "重复"


def test_t03_pure_shell_text_real_data_form_not_signed(monkeypatch):
    """T03（补充令 ③）：纯尾注空文（真实数据 7 条"（产联社）"形态）——
    裸长地板必须拦住不签：判据不过（残 0<15 且裸 5<20）→50 闸→老路合格
    门拒（vc=False 无事实载体）→全链边界。"""
    _env_on(monkeypatch)
    out = _decide_ruleonly(
        _record(RECORD_ID_H, "item-A", "（产联社）", 1),
        _record(RECORD_ID_C, "item-C", "（产联社）", 3), monkeypatch)
    assert out.decision != "重复"
    assert out.duplicate_ids == ()


def test_t03_stacked_tails_floor_blocks_signing(monkeypatch):
    """T03（补充令 ③）：堆叠尾注"（财联社）（财联社快讯）。（产联社）"——
    闭表只剥末尾（产联社）（非闭表壳零模糊不剥），残文 13<15 地板拦截；
    EXACT 腿同被地板拦（裸 18<50 老路拒）→全链边界。异尾注变体（界面
    尾）同拦——残文相等也不得越过地板签发。"""
    _env_on(monkeypatch)
    out = _decide_ruleonly(
        _record(RECORD_ID_H, "item-A", STACKED, 1),
        _record(RECORD_ID_C, "item-C", STACKED, 3), monkeypatch)
    assert out.decision != "重复"
    out_b = _decide_ruleonly(
        _record(RECORD_ID_H, "item-A", STACKED, 1),
        _record(RECORD_ID_C, "item-C", STACKED_B, 3), monkeypatch)
    assert out_b.decision != "重复"


# ============ §五 跨日跨域禁签（同文签发与正常签发同等窗口） ============

def test_cross_scope_pair_binding_rejected(monkeypatch):
    """跨域（scope_id 不同）→ PairBindingError（禁签=入口闸 fail-closed）。"""
    _env_on(monkeypatch)
    with pytest.raises(pair_compare.PairBindingError):
        _decide_ruleonly(
            _record(RECORD_ID_H, "item-A", GOLD_SAME13["P10"], 1,
                    scope_id="scope-a"),
            _record(RECORD_ID_C, "item-C", GOLD_SAME13["P10"], 3,
                    scope_id="scope-b"), monkeypatch)


def test_cross_date_pair_binding_rejected(monkeypatch):
    """跨日（business_date 不同）→ PairBindingError（同文签发与正常签发
    同等窗口——跨日结构性禁签，证书路不可绕）。"""
    _env_on(monkeypatch)
    with pytest.raises(pair_compare.PairBindingError):
        _decide_ruleonly(
            _record(RECORD_ID_H, "item-A", GOLD_SAME13["P10"], 1,
                    business_date="2026-09-26"),
            _record(RECORD_ID_C, "item-C", GOLD_SAME13["P10"], 3,
                    business_date="2026-09-27"), monkeypatch)


# ============ §六 一方新增独立 Fact 不走文本证书 ============

def test_extra_independent_fact_no_text_certificate(monkeypatch):
    """一方新增独立 Fact（B 增第二句）→ 去壳后正文不等 → 无文本证书 →
    边界（不冒签重复）。"""
    _env_on(monkeypatch)
    a = "日经225指数收涨0.20%，报65270.95点。（产联社）"
    b = ("日经225指数收涨0.20%，报65270.95点。韩国综指收跌0.24%，"
         "报7034.87点。（产联社）")
    p15 = _p15(a, b)
    assert p15.p15_results.text_proof is None
    assert p15.p15_results.equivalence_ready is False
    out = _decide_ruleonly(_record(RECORD_ID_H, "item-A", a, 1),
                           _record(RECORD_ID_C, "item-C", b, 3), monkeypatch)
    assert out.decision != "重复"
    assert out.internal_code not in ("EXACT_TEXT_MATCH",
                                     "LOSSLESS_TEXT_MATCH",
                                     "SHELL_STRIPPED_TEXT_MATCH")


# ============ §七 数值单位方向不同不得被标准化合并 ============

@pytest.mark.parametrize("ta,tb", [
    ("甲公司公告营收环比上涨3%，毛利率改善。（产联社）",
     "甲公司公告营收环比下跌3%，毛利率改善。（产联社）"),
    ("项目总投资约12亿美元。（产联社）",
     "项目总投资约120亿美元。（产联社）"),
], ids=["direction-up-vs-down", "unit-value-differs"])
def test_numeric_direction_unit_not_merged(ta, tb, monkeypatch):
    """数值方向/单位不同 → 去壳后正文逐字不等 → 剥壳证书禁签（标准化
    只剥尾注壳，正文数值/方向/单位一字不动）。"""
    left = text_mod.normalize_text(ta)
    right = text_mod.normalize_text(tb)
    assert certify_shell_stripped_equality(left, right) is None
    _env_on(monkeypatch)
    out = _decide_ruleonly(_record(RECORD_ID_H, "item-A", ta, 1),
                           _record(RECORD_ID_C, "item-C", tb, 3), monkeypatch)
    assert out.decision != "重复"


# ============ §八 伪 Hash 碰撞必须规范化复核拦截 ============

def test_fake_hash_collision_intercepted():
    """伪 Hash 碰撞（篡改/持久化伪证）：重建对拍拦截——证书只认按原文
    复算的规范化结果，不认声称相等的 Hash（"不许只比 Hash"双向钉）。"""
    left = text_mod.normalize_text(BODY_MULTI + T1)
    right = text_mod.normalize_text(BODY_MULTI + T2)
    assert certify_shell_stripped_equality(left, right) is not None  # 正向对照
    # ① 声称双侧同 raw/normalized hash（body 实不同侧）
    fake_hash = dataclasses.replace(right, raw_hash=left.raw_hash,
                                    normalized_hash=left.normalized_hash)
    assert certify_shell_stripped_equality(left, fake_hash) is None
    # ② 换文留 hash（text 字段被替换）
    fake_text = dataclasses.replace(right, text=left.text)
    assert certify_shell_stripped_equality(left, fake_text) is None
    # ③ 归一器版本不一致
    fake_ver = dataclasses.replace(right, normalizer_version="vX-evil")
    assert certify_shell_stripped_equality(left, fake_ver) is None
    # ④ 非 NormalizationResult 输入 fail-closed
    assert certify_shell_stripped_equality(None, right) is None
    assert certify_shell_stripped_equality(left, "not-a-result") is None


# ============ §九 闭表纪律（中部不剥/异正文/多尾注/裸长边界/T03 原语） ============

def test_closed_table_mid_text_not_stripped():
    """闭表词在正文中部（文尾非尾注）不剥——确定性尾匹配零模糊。"""
    mid = "甲公司公告（产联社）完成回购计划。"
    assert p15_integration.strip_source_shell(mid) == mid
    head = "（产联社）甲公司完成回购计划。"
    assert p15_integration.strip_source_shell(head) == head


def test_different_body_same_tail_not_signed(monkeypatch):
    """不同正文同尾注 → 去壳后正文不等 → 不签（证书不冒签）。"""
    left = text_mod.normalize_text("甲公司完成回购。（产联社）")
    right = text_mod.normalize_text("乙公司完成增发。（产联社）")
    assert certify_shell_stripped_equality(left, right) is None
    _env_on(monkeypatch)
    a, b = "甲公司完成回购计划涉及现金一千二百万。（产联社）", \
           "乙公司完成增发计划涉及现金一千二百万。（产联社）"
    out = _decide_ruleonly(_record(RECORD_ID_H, "item-A", a, 1),
                           _record(RECORD_ID_C, "item-C", b, 3), monkeypatch)
    assert out.decision != "重复"


def test_multi_tail_notes_iterative_strip():
    """多尾注迭代剥：逐层剥尽闭表壳；剥后与裸正文（或部分剥侧）逐字相等
    → 签发。"""
    assert p15_integration.strip_source_shell(
        BODY_MULTI + T1 + T2) == BODY_MULTI
    bare = text_mod.normalize_text(BODY_MULTI)
    both = text_mod.normalize_text(BODY_MULTI + T1 + T2)
    partial = text_mod.normalize_text(BODY_MULTI + T1)
    assert certify_shell_stripped_equality(both, bare) is not None
    assert certify_shell_stripped_equality(both, partial) is not None


def test_t03_stacked_tails_primitive_floor():
    """T03（补充令 ③）原语面：堆叠尾注剥闭表尾后残文"（财联社）（财联社
    快讯）。"=13<15 → 剥壳证书地板禁签（残文相等也不签）；非闭表壳
    （财联社/财联社快讯）零模糊不剥。"""
    assert p15_integration.strip_source_shell(STACKED) == "（财联社）（财联社快讯）。"
    left = text_mod.normalize_text(STACKED)
    right = text_mod.normalize_text(STACKED_B)
    assert certify_shell_stripped_equality(left, right) is None
    assert certify_shell_stripped_equality(
        left, text_mod.normalize_text(STACKED)) is None


@pytest.mark.parametrize("body_len,expected", [(14, None), (15, "sign"),
                                               (16, "sign")],
                         ids=["bare19-residual14", "bare20-residual15",
                              "bare21-residual16"])
def test_bare_length_boundaries_19_20_21(body_len, expected):
    """裸长边界 19/20/21（残文门槛 14/15/16 同侧对拍）：残文<15 禁签、
    ≥15 恰过——长度是候选警报级闸（v2 §4.1）。"""
    body = "甲乙丙丁戊己庚辛壬癸子丑寅卯辰巳午未申酉戌亥乾"[:body_len]
    assert len(body) == body_len
    left = text_mod.normalize_text(body + T1)
    right = text_mod.normalize_text(body + T2)
    cert = certify_shell_stripped_equality(left, right)
    if expected is None:
        assert cert is None
    else:
        assert cert is not None
        assert cert.kind is text_mod.TextMatchKind.SHELL_STRIPPED_TEXT_MATCH


# ============ §十 开关与 manifest 审计留痕 + 三分立码集 + DECOUPLE 作用域 ============

def test_shell_rule_version_pinned_v2():
    """规则版本钉（闭表纪律可执行钉）：v2=扩员（界面新闻快讯）+迭代剥。"""
    assert SHELL_STRIP_RULE_VERSION == "source-shell-v2-2026-10-11"


def test_manifest_registers_shell_strip_switch():
    """开关审计留痕：DEDUP_EXACT_SHELL_STRIP 补登记 KNOWN_SWITCHES——
    manifest 快照如实记录 env 态（此前漏册面补齐）。"""
    from news_flash_dedup.lib import run_manifest as rm
    assert "DEDUP_EXACT_SHELL_STRIP" in rm.KNOWN_SWITCHES
    snap = dict(rm.snapshot_switch_state(
        {"DEDUP_CERT_DECOUPLE": "1", "DEDUP_EXACT_SHELL_STRIP": "1"}))
    assert snap["DEDUP_EXACT_SHELL_STRIP"] == "1"
    assert snap["DEDUP_CERT_DECOUPLE"] == "1"
    # 正式 manifest 装配面：switch_state 携带该开关（build_run_manifest 全链）
    manifest = rm.build_run_manifest(
        inputs=["r1"], embedding_space="es-jpf-test",
        code_git_sha="0" * 40,
        env={"DEDUP_CERT_DECOUPLE": "1", "DEDUP_EXACT_SHELL_STRIP": "1"})
    assert ("DEDUP_EXACT_SHELL_STRIP", "1") in manifest.switch_state


def test_three_certificate_codes_distinct():
    """令 ②：SHELL_STRIPPED_TEXT_MATCH 与 EXACT_TEXT_MATCH、普通
    LOSSLESS_TEXT_MATCH 三分立——枚举面+直签码集+公共白名单三处同步。"""
    kinds = {k.value for k in text_mod.TextMatchKind}
    assert {"EXACT_TEXT_MATCH", "LOSSLESS_TEXT_MATCH",
            "SHELL_STRIPPED_TEXT_MATCH"} <= kinds
    assert "SHELL_STRIPPED_TEXT_MATCH" in pair_compare.EQUIVALENT_CODES
    assert "SHELL_STRIPPED_TEXT_MATCH" in pair_compare._TEXT_CERT_PROOF_CODES
    assert "SHELL_STRIPPED_TEXT_MATCH" in decide_types._EQUIVALENT_CODES


def test_decouple_scope_exempts_only_text_cert_gate(monkeypatch):
    """DECOUPLE 作用域对照：文本相等证书工件门被豁免（同文不完备→直签）；
    释义对（文本相异）不完备→FACT_EQUIVALENT 完备性要求一字不动→边界
    （其他读工件路径不因解耦洗白）。"""
    _env_on(monkeypatch)
    out1 = _decide_ruleonly(
        _record(RECORD_ID_H, "item-A", LONG_INCOMPLETE, 1),
        _record(RECORD_ID_C, "item-C", LONG_INCOMPLETE, 3), monkeypatch)
    assert out1.decision == "重复"                     # 豁免面=文本证书
    out2 = _decide_ruleonly(
        _record(RECORD_ID_H, "item-A", PARA_H, 1),
        _record(RECORD_ID_C, "item-C", PARA_C, 3), monkeypatch)
    assert out2.decision != "重复"                     # 释义面不豁免
    assert out2.internal_code != "FACT_EQUIVALENT"
