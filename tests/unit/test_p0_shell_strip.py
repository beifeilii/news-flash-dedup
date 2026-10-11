# -*- coding: utf-8 -*-
"""2026-10-09 P0 收口包二②红测：剥信源壳判据（DEDUP_EXACT_SHELL_STRIP
默认关）解 min_len=50 误杀。

出处=定稿决议-外部评审-1009 §二（"剥信源壳+残文≥15 字（或裸长≥20）判据下
26 对可签、4 对进人工，完美二分；min_len=50 将误杀 19 对真重复……照此
施工"）+ v2 §4.1（剥壳规则版本化、能回指原文、剥后为空返未决、长度作
候选警报而非唯一签发条件）+ 主窗口派包（新开关 DEDUP_EXACT_SHELL_STRIP
默认关；红测=19 对被误杀对开→签/关→不签+空壳 4 对仍边界+现役不受影响；
开关开复跑 30 对必须 26/4 达线——数据面复跑证据=log\\temp\\
p0acc-sametext30-onshell.json）。

夹具纪律（与 test_p0_cert_decouple.py 同）：全真实现类——facts/rule.py
真规则抽取+decide/service.py _wrap_facts_as_report 真校验包装+真
build_aligned/extract_p15_results/compare_pair/aggregate 全链；19 对被
误杀对用 T 冻结 30 对实测原文（P0 验收一冻结清单 log\\temp\\
p0acc-sametext30-pairs.json，双侧 vc=False 锁①逐对实证在案），零手写
假字段。
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
from news_flash_dedup.compare.p15_integration import (
    SHELL_STRIP_BARE_MIN,
    SHELL_STRIP_ENV,
    SHELL_STRIP_RESIDUAL_MIN,
    SHELL_STRIP_RULE_VERSION,
    _shell_criterion_pass,
    shell_strip_enabled,
    strip_source_shell,
)
from news_flash_dedup.decide.service import _wrap_facts_as_report
from news_flash_dedup.facts import rule as facts_rule

RECORD_ID_H = "a" * 64
RECORD_ID_C = "b" * 64

# —— T 冻结 30 对实测原文（P0 验收一冻结清单逐字；19 对被误杀对 18 组异文，
#    G26 组 3 件 2 后达对同文）——
KILLED19 = {
    "G01": "格芯获得美国商务部至多3.75亿美元芯片研发资助。（产联社）",
    "G02": "核能公司HGP计划以12亿美元估值进行SPAC交易。（产联社）",
    "G04": "道琼斯指数跌幅扩大至1.01%，报52877.02点。（产联社）",
    "G05": "标普500指数跌幅扩大至0.5%，刷新日低。（产联社）",
    "G06": "克里姆林宫表示，俄罗斯对欧洲没有敌对计划。（产联社）",
    "G07": "美国财长贝森特重申对美国通胀的看法，并指出短期能源价格将出现飙升。（产联社）",
    "G08": "美国总统特朗普重申，伊朗永远不会拥有核武器。（产联社）",
    "G09": "美国天然气期货下跌3%，报2.887美元/百万英热。（产联社）",
    "G10": "8月，美国纽约联储1年通胀预期为3.58%，前值为3.63%。（产联社）",
    "G11": "AI算力租赁商CoreWeave股价涨幅扩大至超10%。（产联社）",
    "G12": "美国财长贝森特表示，对伊朗制裁不会再有任何克制。（产联社）",
    "G14": "美国财长贝森特表示，当前通胀预期处于持平或下降状态。（产联社）",
    "G15": "美国国务卿鲁比奥表示，委内瑞拉的油田将再次恢复生产。（产联社）",
    "G18": "8月，美国谘商会就业趋势指数报108.53，前值由107.71修正为107.76。（产联社）",
    "G22": "8日，伊朗陆军防空部队在霍尔木兹海峡上空击落一架美军MQ-1无人机。（产联社）",
    "G23": "美国财政部拍卖580亿美元三年期国债，得标利率为4.474%，投标倍数达2.72。（产联社）",
    "G25": "恒指期货夜盘收涨0.24%，报25286.94点，低水30.24点。（产联社）",
    "G26": "福特汽车股价下跌4.5%，触及盘中低点。（产联社）",
}
# 19 后达对=18 组异文 + G26 组第二对（同文不同对例）。
KILLED19_PAIRS = [(gid, text) for gid, text in KILLED19.items()] + [
    ("G26-P2", KILLED19["G26"])]
LONG2 = {
    "G03": "英国外交大臣米利班德表示，将扩大现行人权制裁法的适用范围，以便能更迅速地用于遏制定居点扩张，包括E1开发项目。（产联社）",
    "G24": "美国财政部发行580亿美元3年期国债，中标收益率为4.474%，略低于纽约时间下午1点投标截止时4.475%的发行前交易水平，显示市场需求略好于预期。此次中标收益率创该期限国债2024年6月以来最高。拍卖需求稳健，该期限国债收益率对此反应不大，日内仍上涨近2个基点，表现逊于长期国债。（产联社）",
}
SHELL = "（产联社）"
# 判据不过合成件（17 裸/12 残，vc=False 形态同族）。
CRITERION_FAIL = "甲公司完成股份回购计划。（产联社）"
# 双侧完备短文（8 字符 <50，老路本可签——开关组合零效应对照）。
SHORT_COMPLETE = "甲公司完成回购。"


def _ctx(*, record_id, item_id, text, arrival_seq):
    return {"record_id": record_id, "item_id": item_id, "text": text,
            "raw_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "scope_id": "default", "business_date": "2026-09-26",
            "arrival_seq": arrival_seq, "pipeline_version": "dedup_v1"}


def _run(history_text, current_text):
    """真抽取 → 真校验包装 → build_aligned → extract_p15_results →
    compare_pair → aggregate 全链（test_p0_cert_decouple._run 同型）。"""
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


# ---------- 开关解析（严格、默认关） ----------

def test_shell_strip_switch_parsing_strict_default_off():
    assert shell_strip_enabled({}) is False
    assert shell_strip_enabled({SHELL_STRIP_ENV: ""}) is False
    assert shell_strip_enabled({SHELL_STRIP_ENV: "1"}) is True
    for bad in ("0", "true", "True", "ON", "on", "yes", " 1", "1 ", "2"):
        assert shell_strip_enabled({SHELL_STRIP_ENV: bad}) is False


# ---------- 规则版本化与剥壳原语（v2 §4.1 版本化+能回指原文） ----------

def test_rule_version_pinned():
    """版本串钉死：扩员/变形必升版本（闭表纪律的可执行钉）。
    2026-10-11（主窗令·判定优先级修复 ①）升 v2：闭表扩员（界面新闻快
    讯，T 冻结 31 对表 #7 实测同正文异尾注）+迭代剥变形双触发。"""
    assert SHELL_STRIP_RULE_VERSION == "source-shell-v2-2026-10-11"
    assert (SHELL_STRIP_RESIDUAL_MIN, SHELL_STRIP_BARE_MIN) == (15, 20)


def test_strip_source_shell_closed_tail_only():
    assert strip_source_shell(KILLED19["G01"]) == "格芯获得美国商务部至多3.75亿美元芯片研发资助。"
    assert strip_source_shell(SHELL) == ""                 # 空壳剥尽（v2 §4.1 剥后为空）
    assert strip_source_shell("福特汽车股价下跌4.5%，触及盘中低点。") == "福特汽车股价下跌4.5%，触及盘中低点。"  # 无壳不动
    # 能回指原文：剥除串恒为原文真后缀、残文恒为原文真前缀
    text = KILLED19["G26"]
    assert strip_source_shell(text) + "（产联社）" == text
    with pytest.raises(TypeError):
        strip_source_shell(None)


def test_shell_criterion_boundaries():
    """判据边界钉：残文≥15 或 裸长≥20（定稿决议 §二原文判据）。"""
    assert _shell_criterion_pass("a" * 20) is True          # 裸长 20 过
    assert _shell_criterion_pass("a" * 19) is True          # 裸 19 无壳 → 残=19≥15 过
    assert _shell_criterion_pass("a" * 15) is True          # 残=15 门槛恰过
    assert _shell_criterion_pass("a" * 14) is False         # 裸<20 且 残=14<15 → 不过
    assert _shell_criterion_pass("a" * 15 + "（产联社）") is True    # 残 15 过
    assert _shell_criterion_pass("a" * 14 + "（产联社）") is False   # 残 14 不过
    assert _shell_criterion_pass("a" * 10 + "（产联社）") is False   # 15 裸/10 残不过
    assert _shell_criterion_pass(SHELL) is False            # 空壳不过（残 0 裸 5）
    assert _shell_criterion_pass(CRITERION_FAIL) is False   # 17 裸/12 残不过
    for text in KILLED19.values():
        assert _shell_criterion_pass(text) is True          # 19 对全过判据


# ---------- ① 19 对被误杀对：开关开→签、剥壳关→不签（红测主件） ----------

@pytest.mark.parametrize("case_id,text", KILLED19_PAIRS,
                         ids=[cid for cid, _ in KILLED19_PAIRS])
def test_killed19_sign_with_shell_strip_on(monkeypatch, case_id, text):
    """①a 双开（解耦+剥壳）：19 对被误杀对逐对签发 EXACT→全链重复
    （双侧 vc=False 锁①在案，签发纯经剥壳判据放行）。"""
    _both_on(monkeypatch)
    history, current, p15, pair, out = _run(text, text)
    assert 20 <= len(text) < 50                          # 全落 min_len=50 误杀区
    assert history.validated_complete is False           # 锁①：双侧工件不完备
    assert current.validated_complete is False
    assert _shell_criterion_pass(text) is True
    assert p15.p15_results.equivalence_ready is True
    assert p15.p15_results.text_proof == "EXACT_TEXT_MATCH"
    assert pair.outcome == "equivalent"
    assert pair.code == "EXACT_TEXT_MATCH"
    assert out.decision == "重复"
    assert out.duplicate_ids == ("item-H",)


@pytest.mark.parametrize("case_id,text", KILLED19_PAIRS,
                         ids=[cid for cid, _ in KILLED19_PAIRS])
def test_killed19_still_killed_with_shell_strip_off(monkeypatch, case_id, text):
    """①b 解耦开+剥壳关=误杀原样（min_len=50 闸回落老路拒签）——剥壳开关
    是唯一解杀旋钮，误杀钉不死灰复。"""
    monkeypatch.setenv("DEDUP_CERT_DECOUPLE", "1")
    monkeypatch.delenv(SHELL_STRIP_ENV, raising=False)
    _, _, p15, pair, out = _run(text, text)
    assert p15.p15_results.equivalence_ready is False
    assert p15.p15_results.text_proof is None
    assert pair.outcome == "unresolved"
    assert out.decision != "重复"
    assert out.duplicate_ids == ()


# ---------- ② 空壳 4 对仍边界（双开也不签） ----------

def test_shell_empty_pair_stays_boundary_with_both_on(monkeypatch):
    """② 空壳"（产联社）"同文对：双开下判据不过（残 0<15 且 裸 5<20）→
    维持 50 闸→回落老路→无事实载体 vc=False→不签→边界（定稿决议 §二
    4 对进人工=v2 §4.1"剥后为空返回未决"落实）。"""
    _both_on(monkeypatch)
    history, current, p15, pair, out = _run(SHELL, SHELL)
    assert _shell_criterion_pass(SHELL) is False
    assert history.validated_complete is False
    assert current.validated_complete is False
    assert p15.p15_results.equivalence_ready is False
    assert p15.p15_results.text_proof is None
    assert pair.outcome == "unresolved"
    assert out.decision != "重复"
    assert out.duplicate_ids == ()


def test_criterion_fail_template_stays_boundary(monkeypatch):
    """②b 判据不过非空壳合成件（17 裸/12 残）：双开下维持 50 闸回落老路、
    工件不完备拒签——判据不是"超短一律放行"，残文门槛实打。"""
    _both_on(monkeypatch)
    _, _, p15, pair, out = _run(CRITERION_FAIL, CRITERION_FAIL)
    assert _shell_criterion_pass(CRITERION_FAIL) is False
    assert p15.p15_results.text_proof is None
    assert pair.outcome == "unresolved"
    assert out.decision != "重复"


# ---------- ③ 现役不受影响（开关关=老行为逐字节） ----------

def test_both_off_old_behavior_byte_identical(monkeypatch):
    """③a 双关：同文长文（≥50）不完备→不签（现役锁①形态，剥壳零效应）。"""
    monkeypatch.delenv("DEDUP_CERT_DECOUPLE", raising=False)
    monkeypatch.delenv(SHELL_STRIP_ENV, raising=False)
    _, _, p15, pair, out = _run(LONG2["G03"], LONG2["G03"])
    assert p15.p15_results.equivalence_ready is False
    assert p15.p15_results.text_proof is None
    assert pair.outcome == "unresolved"
    assert out.decision != "重复"


def test_strip_on_decouple_off_zero_effect(monkeypatch):
    """③b 剥壳开+解耦关：剥壳判据零调用——长文不完备不签、短文完备
    老路照签（剥壳开关在旧路无任何洇渗）。"""
    monkeypatch.delenv("DEDUP_CERT_DECOUPLE", raising=False)
    monkeypatch.setenv(SHELL_STRIP_ENV, "1")
    _, _, p15, _, out = _run(LONG2["G03"], LONG2["G03"])
    assert p15.p15_results.text_proof is None
    assert out.decision != "重复"
    _, _, p15b, _, _ = _run(SHORT_COMPLETE, SHORT_COMPLETE)
    assert p15b.p15_results.text_proof == "EXACT_TEXT_MATCH"   # 老路合格门照常


def test_long_pairs_sign_with_strip_on_no_regression(monkeypatch):
    """③c 双开下 ≥50 对（裸长腿过判据）照签不回退——剥壳只解杀不新杀。"""
    _both_on(monkeypatch)
    for text in LONG2.values():
        _, _, p15, pair, out = _run(text, text)
        assert len(text) >= 50
        assert p15.p15_results.text_proof == "EXACT_TEXT_MATCH"
        assert pair.outcome == "equivalent"
        assert out.decision == "重复"


# ---------- ④ LOSSLESS 同法（剥壳判据按归一文求值） ----------

def test_lossless_killed_pair_signs_with_strip_on(monkeypatch):
    """④ 双开：被误杀对 CRLF/LF 无损变体（归一等值、判据按归一文过）
    →签发 LOSSLESS_TEXT_MATCH→全链重复。"""
    _both_on(monkeypatch)
    history_text = KILLED19["G01"].replace("。", "。\r\n", 1)
    current_text = KILLED19["G01"].replace("。", "。\n", 1)
    assert history_text != current_text
    _, _, p15, pair, out = _run(history_text, current_text)
    assert p15.p15_results.text_proof == "LOSSLESS_TEXT_MATCH"
    assert pair.outcome == "equivalent"
    assert pair.code == "LOSSLESS_TEXT_MATCH"
    assert out.decision == "重复"


# ---------- ⑤ FACT_EQUIVALENT 通道一字不动 ----------

def test_fact_equivalent_channel_untouched_by_shell_strip(monkeypatch):
    """⑤ 长文释义对（文本相异、双侧不完备）：双开下仍无文本证书、
    FACT_EQUIVALENT 不因剥壳洗白（完备性要求一字不动）。"""
    _both_on(monkeypatch)
    para_h = ("甲公司今日宣布回购股份，旨在提升股东价值并优化资本结构。"
              "公司另有备用金1200万元未列入本次计划。")
    para_c = ("甲公司发布公告称回购股份，旨在提升股东价值并优化资本结构。"
              "公司另有备用金1200万元未列入本次计划。")
    _, _, p15, pair, out = _run(para_h, para_c)
    assert p15.p15_results.equivalence_ready is False
    assert p15.p15_results.text_proof is None
    assert pair.outcome == "unresolved"
    assert out.decision != "重复"


# ---------- ⑥ DEDUP_EXACT_MIN_LEN 定制阈值在判据不过腿仍有效 ----------

def test_custom_min_len_honored_on_criterion_fail_leg(monkeypatch):
    """⑥ 双开+DEDUP_EXACT_MIN_LEN=10：判据不过件（17 裸）经定制阈值
    放行签发——剥壳判据不过≠死路，min_len 定制语义在回落腿一字不动。"""
    _both_on(monkeypatch)
    monkeypatch.setenv("DEDUP_EXACT_MIN_LEN", "10")
    _, _, p15, _, _ = _run(CRITERION_FAIL, CRITERION_FAIL)
    assert _shell_criterion_pass(CRITERION_FAIL) is False
    assert p15.p15_results.text_proof == "EXACT_TEXT_MATCH"
