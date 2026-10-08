# -*- coding: utf-8 -*-
"""P1-a 判官可签发证明组件红绿钉值（2026-10-09 施工窗）。

出处：log\判定链改造最终方案-Codex-20261008.md §4 P1-a（"换文复用 pair_id、
错侧引文、去空白伪匹配、矛盾数值/时间、双序分歧均不得签发；判官证明可
审计、可复验"）+ log\判定链改造-终裁令-正典-1009.md §五-2/3。

钉值面（任务书①-⑦）：
① 缓存键加固：加固键=双文 sha+双序+model/prompt/policy 版本；同 pair_id
   换文本→miss 重判；旧键缓存证明路不消费（旧缓存不升级为签发凭证）；
   缓存命中仍全量证明核验（命中只省 LLM 调用）。
② 引文 offset 绑定：citations 记录 Unicode offset；错侧引文/去空白伪匹配/
   锚过短均拒签（P_OFFSET）。
③ 机侧独立核验：机抽数值/时间/阶段/否定冲突拦截判"重复"（P_NUMERIC/
   P_TIME/P_STAGE/P_NEGATION/P_SUBJECT）；一侧缺失放行；判官声称一致处
   backed=False 记录；判官自供时间矛盾 P_JTIME。
④ 结论对拍：decision="重复"+numeric/time conclusion="不一致"→P_CONCLUSION。
⑤ VerifiedJudgeProof：绿对签发、字段合同、JSON 往返、复验/篡改拒验。
⑥ NotDuplicateProof：双序说不重复但无机器可证伪轴→不可签发
   （insufficient_falsification_evidence，证据不足→未决）；真证伪轴→签发；
   引文不可绑定→citation_unbound；双序分歧→无证明。
⑦ gate 语义：开关默认关=老行为逐字节（含 gate 下去空白伪匹配照放、
   R1-R6 旧规则、审计负载无证明键）；开关开 audit 只观测、gate 降级。

夹具纪律：全真实现类——SyncResidualJudge/judge_proof/machine_verify 真
实现，仅 LLM 网络边界以罐装响应 mock（test_decide_llm_residual.py 同型）。
"""

from __future__ import annotations

import hashlib
import json
import re

import pytest

from news_flash_dedup.decide import judge_proof as jp
from news_flash_dedup.decide import llm_residual as lr
from news_flash_dedup.decide import machine_verify as mv


# ---------------------------------------------------------------- 测试工装

def _jjson(decision, ea, eb, na=(), nb=(), ta=(), tb=(),
           ncon="一致", tcon="一致"):
    return json.dumps({
        "decision": decision,
        "evidence_a": list(ea), "evidence_b": list(eb),
        "numeric_check": {"numbers_a": list(na), "numbers_b": list(nb),
                           "conclusion": ncon},
        "time_check": {"times_a": list(ta), "times_b": list(tb),
                        "conclusion": tcon},
        "reason": "P1-a 测试判定",
    }, ensure_ascii=False)


class _MockLLM:
    """按 (文本A, 文本B) 精确路由的罐装响应（test_decide_llm_residual 同型）。"""

    def __init__(self, responses):
        self.responses = responses
        self.calls = 0

    def __call__(self, *, model, system, user):
        self.calls += 1
        m = re.match(r"【文本A】\n(.*)\n\n【文本B】\n(.*)\Z", user, re.DOTALL)
        assert m, f"用户消息形态异常：{user[:60]!r}"
        resp = self.responses[(m.group(1), m.group(2))]
        if isinstance(resp, Exception):
            raise resp
        return resp, 0.01


def _judge(mock, *, cache_dir=None, mv_mode=lr.MV_AUDIT, ledger=None):
    return lr.SyncResidualJudge(
        lr.ResidualJudgeConfig(cache_dir=cache_dir, mv_mode=mv_mode),
        call_fn=mock, ledger=ledger)


@pytest.fixture(autouse=True)
def _proof_switch_off_by_default(monkeypatch):
    """环境隔离：默认保证开关关（各用例自行 setenv 开启）。"""
    monkeypatch.delenv(jp.JUDGE_PROOF_ENV, raising=False)


def _proof_on(monkeypatch):
    monkeypatch.setenv(jp.JUDGE_PROOF_ENV, "1")


# ---------------------------------------------------------------- 文本对夹具
# 绿对（机检全过）：同主体/同数值/同时间/同阶段，仅措辞差

G_H = "甲公司（600001）9月24日公告营收100万，同比增长5%，业绩符合预期"
G_C = "甲公司（600001）9月24日公告：营收100万元，同比增长5%，符合预期"
G_RESP = {
    (G_H, G_C): _jjson("重复", ("甲公司（600001）9月24日公告营收100万",),
                       ("甲公司（600001）9月24日公告：营收100万元",),
                       na=("100万",), nb=("100万",),
                       ta=("9月24日",), tb=("9月24日",)),
    (G_C, G_H): _jjson("重复", ("甲公司（600001）9月24日公告：营收100万元",),
                       ("甲公司（600001）9月24日公告营收100万",),
                       na=("100万",), nb=("100万",),
                       ta=("9月24日",), tb=("9月24日",)),
}

# 数值冲突对（787.8 vs 789.8；判官声称一致）
N_H = "WTI原油涨9%报787.8美元"
N_C = "WTI原油涨超9%报789.8美元"
N_RESP = {
    (N_H, N_C): _jjson("重复", ("WTI原油涨9%报787.8美元",),
                       ("WTI原油涨超9%报789.8美元",),
                       na=("787.8",), nb=("789.8",), ncon="一致",
                       tcon="均无时间"),
    (N_C, N_H): _jjson("重复", ("WTI原油涨超9%报789.8美元",),
                       ("WTI原油涨9%报787.8美元",),
                       na=("789.8",), nb=("787.8",), ncon="一致",
                       tcon="均无时间"),
}

# 不重复·无机器可证伪轴对（⑥核心红测：模型两次说不重复，机验无差异证据）
ND0_H = "甲公司发布年度业绩预告，经营情况良好"
ND0_C = "甲公司发布年度业绩预告称经营态势向好"
ND0_RESP = {
    (ND0_H, ND0_C): _jjson("不重复", ("经营情况良好",), ("经营态势向好",),
                           ncon="无关键数值", tcon="均无时间"),
    (ND0_C, ND0_H): _jjson("不重复", ("经营态势向好",), ("经营情况良好",),
                           ncon="无关键数值", tcon="均无时间"),
}

# 不重复·主体代码互斥对（真证伪轴）
ND1_H = "易天股份（300812）近5日主力资金净流入居前"
ND1_C = "名臣健康（002919）近5日主力资金净流入居前"
ND1_RESP = {
    (ND1_H, ND1_C): _jjson("不重复", ("易天股份（300812）",),
                           ("名臣健康（002919）",),
                           na=("300812",), nb=("002919",), ncon="不一致",
                           tcon="均无时间"),
    (ND1_C, ND1_H): _jjson("不重复", ("名臣健康（002919）",),
                           ("易天股份（300812）",),
                           na=("002919",), nb=("300812",), ncon="不一致",
                           tcon="均无时间"),
}


# ---------------------------------------------------------------- ①⑦ 开关与缓存键

def test_switch_parse_discipline():
    assert jp.judge_proof_enabled({}) is False
    assert jp.judge_proof_enabled({jp.JUDGE_PROOF_ENV: ""}) is False
    assert jp.judge_proof_enabled({jp.JUDGE_PROOF_ENV: "true"}) is False
    assert jp.judge_proof_enabled({jp.JUDGE_PROOF_ENV: "2"}) is False
    assert jp.judge_proof_enabled({jp.JUDGE_PROOF_ENV: "1"}) is True
    assert jp.JUDGE_PROOF_ENV == "DEDUP_JUDGE_PROOF"


def test_hardened_key_layout_and_dimensions():
    model, pid, sha = "qwen-turbo", "pair-x", "s" * 64
    ta, tb = "a" * 64, "b" * 64
    key_hc = lr.residual_cache_key_hardened(model, "judge_v1", pid, sha,
                                            "hc", ta, tb)
    expect = hashlib.sha256(
        f"{model}|judge_v1|A|hc|{pid}|{sha}|{ta}|{tb}|"
        f"{jp.PROOF_POLICY_VERSION}".encode("utf-8")).hexdigest()
    assert key_hc == expect                     # 布局逐字节钉（双序恒书 order）
    legacy = lr.residual_cache_key(model, "judge_v1", pid, sha, "hc")
    assert key_hc != legacy                     # 与旧键空间隔离
    # 双序维度：hc/ch 必异（旧键 hc 省 order 的兼容布局不影响本键）
    assert lr.residual_cache_key_hardened(model, "judge_v1", pid, sha,
                                          "ch", tb, ta) != key_hc
    # 双文 hash 维度：同 pair_id 换任一文本 → 键必异（同键换文本吃陈判切除）
    assert lr.residual_cache_key_hardened(model, "judge_v1", pid, sha,
                                          "hc", "c" * 64, tb) != key_hc
    assert lr.residual_cache_key_hardened(model, "judge_v1", pid, sha,
                                          "hc", ta, "d" * 64) != key_hc
    # policy 版本维度
    assert lr.residual_cache_key_hardened(model, "judge_v1", pid, sha, "hc",
                                          ta, tb,
                                          policy_version="p1a-proof-v2") != key_hc
    # model/prompt 维度（现役纪律照承）
    assert lr.residual_cache_key_hardened("other", "judge_v1", pid, sha,
                                          "hc", ta, tb) != key_hc
    assert lr.residual_cache_key_hardened(model, "judge_v2", pid, sha,
                                          "hc", ta, tb) != key_hc
    # fail-closed：order/文本 hash 非法拒识
    with pytest.raises(ValueError):
        lr.residual_cache_key_hardened(model, "judge_v1", pid, sha,
                                       "xx", ta, tb)
    with pytest.raises(ValueError):
        lr.residual_cache_key_hardened(model, "judge_v1", pid, sha,
                                       "hc", "not-a-sha", tb)
    with pytest.raises(ValueError):
        lr.residual_cache_key_hardened(model, "judge_v1", pid, sha,
                                       "hc", None, tb)


def test_proof_mode_same_pair_id_changed_texts_rejudges(monkeypatch, tmp_path):
    """① 红测：同 pair_id 换文本 → 加固键必异 → miss 重判（不吃陈判）。"""
    _proof_on(monkeypatch)
    mock = _MockLLM({**G_RESP, **N_RESP})
    judge = _judge(mock, cache_dir=str(tmp_path))
    out1 = judge.judge_pair("same-pid", G_H, G_C)
    assert out1.cell == "signed" and mock.calls == 2
    out2 = judge.judge_pair("same-pid", N_H, N_C)   # 同 pair_id 换文本对
    assert mock.calls == 4                          # 两顺序均 miss 重判
    assert "P_NUMERIC" in out2.fired_rules_union    # 吃的是新判非陈判
    assert out2.proof is not None and out2.proof.issued is False


def test_legacy_cache_entry_not_consumed_in_proof_mode(monkeypatch, tmp_path):
    """① 红测：旧键缓存（开关关写入）在证明路天然 miss=不升级为签发凭证。"""
    mock1 = _MockLLM(G_RESP)
    _judge(mock1, cache_dir=str(tmp_path)).judge_pair("p1", G_H, G_C)
    assert mock1.calls == 2
    legacy_key = lr.residual_cache_key(lr.DEFAULT_MODEL,
                                       lr.JUDGE_PROMPT_VERSION, "p1",
                                       lr.PROMPT_SHA256, "hc")
    assert (tmp_path / f"{legacy_key}.json").exists()   # 旧键条目在场
    _proof_on(monkeypatch)
    mock2 = _MockLLM(G_RESP)
    ledger2 = lr.ResidualJudgeLedger()
    out = _judge(mock2, cache_dir=str(tmp_path),
                 ledger=ledger2).judge_pair("p1", G_H, G_C)
    assert mock2.calls == 2                    # 旧条目不被消费，全量重判
    assert ledger2.snapshot()["cache_hits"] == 0
    assert out.proof is not None and out.proof.issued is True
    hardened_key = lr.residual_cache_key_hardened(
        lr.DEFAULT_MODEL, lr.JUDGE_PROMPT_VERSION, "p1", lr.PROMPT_SHA256,
        "hc", jp.text_sha256(G_H), jp.text_sha256(G_C))
    assert (tmp_path / f"{hardened_key}.json").exists()  # 新键条目另立


def test_proof_cache_hit_still_runs_full_verification(monkeypatch, tmp_path):
    """① 钉：缓存命中只省 LLM 调用、不省证明核验（命中条目仍全检）。"""
    _proof_on(monkeypatch)
    resp = {
        (G_H, G_C): _jjson("重复", ("原文中不存在的引文片段",), ("营收100万",)),
        (G_C, G_H): _jjson("重复", ("营收100万",), ("原文中不存在的引文片段",)),
    }
    out1 = _judge(_MockLLM(resp), cache_dir=str(tmp_path)).judge_pair(
        "p1", G_H, G_C)
    assert "P_OFFSET" in out1.fired_rules_union      # 首跑拦截
    mock2 = _MockLLM(resp)
    ledger2 = lr.ResidualJudgeLedger()
    out2 = _judge(mock2, cache_dir=str(tmp_path),
                  ledger=ledger2).judge_pair("p1", G_H, G_C)
    assert mock2.calls == 0 and ledger2.snapshot()["cache_hits"] == 2
    assert "P_OFFSET" in out2.fired_rules_union      # 命中照核（不吃缓存免检）
    assert out2.proof is not None and out2.proof.issued is False
    assert out1.to_audit_dict() == out2.to_audit_dict()   # 审计负载逐字节同一


# ---------------------------------------------------------------- ② 引文 offset 绑定

def test_citation_offsets_bound_to_own_side_unicode(monkeypatch):
    """② 绿钉：引文绑定本侧原文 Unicode offset（码点左闭右开，首处+命中数）。"""
    _proof_on(monkeypatch)
    out = _judge(_MockLLM(G_RESP)).judge_pair("p1", G_H, G_C)
    assert out.proof is not None and out.proof.issued is True
    hc = jp.OrderVerification.from_dict(out.proof.orders[0])
    ev_a = next(c for c in hc.citations
                if c.side == "A" and c.field == "evidence")
    q = "甲公司（600001）9月24日公告营收100万"
    assert ev_a.quote == q
    assert ev_a.start == G_H.find(q) and ev_a.end == G_H.find(q) + len(q)
    assert ev_a.occurrences == 1
    assert G_H[ev_a.start:ev_a.end] == q            # offset 可切回原引文
    time_c = next(c for c in hc.citations
                  if c.side == "A" and c.field == "times")
    assert G_H[time_c.start:time_c.end] == "9月24日"


def test_wrong_side_citation_rejected(monkeypatch):
    """② 红测：错侧引文（仅对侧可定位）→ P_OFFSET wrong_side 拒签。"""
    _proof_on(monkeypatch)
    resp = dict(G_RESP)
    resp[(G_H, G_C)] = _jjson(
        "重复", ("公告：营收100万元",), ("甲公司（600001）9月24日公告：营收100万元",),
        na=("100万",), nb=("100万",), ta=("9月24日",), tb=("9月24日",))
    out = _judge(_MockLLM(resp), mv_mode=lr.MV_GATE).judge_pair("p1", G_H, G_C)
    assert out.cell == "doubtful"                   # gate 降级
    assert out.proof is not None and out.proof.issued is False
    fails = out.hc.proof_verification.citation_failures
    assert [f["check"] for f in fails] == ["wrong_side"]
    assert fails[0]["side"] == "A" and fails[0]["field"] == "evidence"
    assert "hc:P_OFFSET" in out.proof.failure_reasons


def test_whitespace_fuzzy_citation_rejected_in_proof_accepted_in_legacy(
        monkeypatch):
    """② 红测：去空白伪匹配证明路拒签；⑦ 旧 gate 路照放（老行为逐字节）。"""
    resp = {
        (G_H, G_C): _jjson("重复", ("营收 100 万，同比 增长5%",),
                           ("营收100万元，同比增长5%",),
                           na=("100万",), nb=("100万",),
                           ta=("9月24日",), tb=("9月24日",)),
        (G_C, G_H): _jjson("重复", ("营收100万元，同比增长5%",),
                           ("营收 100 万，同比 增长5%",),
                           na=("100万",), nb=("100万",),
                           ta=("9月24日",), tb=("9月24日",)),
    }
    # 旧路（开关关）gate：现役 R5 全空白归一模糊匹配放行 → signed（老行为锚）
    out_legacy = _judge(_MockLLM(resp), mv_mode=lr.MV_GATE).judge_pair(
        "p1", G_H, G_C)
    assert out_legacy.signed is True
    assert out_legacy.proof is None and "proof" not in out_legacy.to_audit_dict()
    # 证明路（开关开）gate：精确 offset 无命中 → P_OFFSET not_found_exact 拒签
    _proof_on(monkeypatch)
    out_proof = _judge(_MockLLM(resp), mv_mode=lr.MV_GATE).judge_pair(
        "p1", G_H, G_C)
    assert out_proof.cell == "doubtful"
    checks = [f["check"]
              for f in out_proof.hc.proof_verification.citation_failures]
    assert checks == ["not_found_exact"]


def test_evidence_anchor_too_short_rejected(monkeypatch):
    """② 红测：证据锚去空白 <4 字符（即使逐字真实）→ anchor_too_short。"""
    _proof_on(monkeypatch)
    resp = dict(G_RESP)
    resp[(G_H, G_C)] = _jjson(
        "重复", ("5%",), ("同比增长5%，符合预期",),
        na=("100万",), nb=("100万",), ta=("9月24日",), tb=("9月24日",))
    out = _judge(_MockLLM(resp)).judge_pair("p1", G_H, G_C)
    fails = out.hc.proof_verification.citation_failures
    assert [f["check"] for f in fails] == ["anchor_too_short"]
    assert out.proof is not None and out.proof.issued is False


# ---------------------------------------------------------------- ③ 机侧独立核验

def test_machine_numeric_conflict_blocks_signed(monkeypatch):
    """③ 红测：判官声称数值一致，机抽发现双向差异 → P_NUMERIC 拦截，
    backed=False（判官声称一致处必须有机检背书）。"""
    _proof_on(monkeypatch)
    out = _judge(_MockLLM(N_RESP), mv_mode=lr.MV_GATE).judge_pair(
        "p1", N_H, N_C)
    assert out.cell == "doubtful"
    assert out.hc.final_decision == "存疑" and out.hc.decision == "重复"
    ver = out.hc.proof_verification
    assert jp.P_NUMERIC in ver.failures
    assert ver.numeric_check["machine_conflict"] is True
    assert ver.numeric_check["judge_conclusion"] == "一致"
    assert ver.numeric_check["backed"] is False    # 声称一致无机检背书
    assert ver.numeric_check["only_a"] == ["787.8"]
    assert ver.numeric_check["only_b"] == ["789.8"]


def test_machine_time_and_stage_conflict_blocks(monkeypatch):
    """③ 红测：机抽时间/阶段双侧不一致 → P_TIME+P_STAGE（不信判官供数）。"""
    _proof_on(monkeypatch)
    s_h = "某公司三季度营收初值3月4日公布为10亿"
    s_c = "某公司三季度营收初值3月5日公布为10亿"
    resp = {
        (s_h, s_c): _jjson("重复", ("初值3月4日公布为10亿",),
                           ("初值3月5日公布为10亿",),
                           na=("10亿",), nb=("10亿",),
                           ta=("3月4日",), tb=("3月5日",)),
        (s_c, s_h): _jjson("重复", ("初值3月5日公布为10亿",),
                           ("初值3月4日公布为10亿",),
                           na=("10亿",), nb=("10亿",),
                           ta=("3月5日",), tb=("3月4日",)),
    }
    out = _judge(_MockLLM(resp), mv_mode=lr.MV_GATE).judge_pair("p1", s_h, s_c)
    assert out.cell == "doubtful"
    ver = out.hc.proof_verification
    assert jp.P_TIME in ver.failures               # 机抽 {34}≠{35} 且阶段词在场
    assert ver.time_check["machine_times_a"] == ["34"]
    assert ver.time_check["machine_times_b"] == ["35"]
    assert jp.P_STAGE not in ver.failures          # 阶段集相同（{初值}）不拦
    # 阶段不对称型（初值 vs 终值）
    s2_c = "某公司三季度营收终值3月4日公布为10亿"
    resp2 = {
        (s_h, s2_c): _jjson("重复", ("初值3月4日",), ("终值3月4日",),
                            na=("10亿",), nb=("10亿",),
                            ta=("3月4日",), tb=("3月4日",)),
        (s2_c, s_h): _jjson("重复", ("终值3月4日",), ("初值3月4日",),
                            na=("10亿",), nb=("10亿",),
                            ta=("3月4日",), tb=("3月4日",)),
    }
    out2 = _judge(_MockLLM(resp2), mv_mode=lr.MV_GATE).judge_pair(
        "p2", s_h, s2_c)
    ver2 = out2.hc.proof_verification
    assert jp.P_STAGE in ver2.failures             # {初值} vs {终值}
    assert ver2.stage_check["stage_a"] == ["初值"]
    assert ver2.stage_check["stage_b"] == ["终值"]
    assert jp.P_TIME not in ver2.failures          # 时间集相同不拦


def test_one_side_missing_time_passes(monkeypatch):
    """③ 绿钉：一侧时间缺失放行=缺失≠冲突（金标口径照承 R2 纪律）。"""
    _proof_on(monkeypatch)
    a_h = "某公司9月24日公告中标重大项目，合同额5亿"
    a_c = "某公司公告中标重大项目，合同额5亿元"
    resp = {
        (a_h, a_c): _jjson("重复", ("9月24日公告中标重大项目",),
                           ("公告中标重大项目",),
                           na=("5亿",), nb=("5亿",),
                           ta=("9月24日",), tb=(), tcon="一方缺失"),
        (a_c, a_h): _jjson("重复", ("公告中标重大项目",),
                           ("9月24日公告中标重大项目",),
                           na=("5亿",), nb=("5亿",),
                           ta=(), tb=("9月24日",), tcon="一方缺失"),
    }
    out = _judge(_MockLLM(resp), mv_mode=lr.MV_GATE).judge_pair("p1", a_h, a_c)
    assert out.cell == "signed"
    ver = out.hc.proof_verification
    assert ver.time_check["machine_conflict"] is False
    assert jp.P_TIME not in ver.failures
    assert out.proof is not None and out.proof.issued is True


def test_judge_supplied_time_selfcontradiction_blocks(monkeypatch):
    """③ 红测：判官自供时间锚自相矛盾（声称一致而引文含阶段词不等）
    → P_JTIME（抓判官自反，非信其供数；机抽无冲突不救）。"""
    _proof_on(monkeypatch)
    j_h = "特朗普表示，中期选举后伊朗战争将立刻终止"
    j_c = "特朗普表示，大选结束后，伊朗战争将立刻终止"
    resp = {
        (j_h, j_c): _jjson("重复", ("中期选举后伊朗战争将立刻终止",),
                           ("大选结束后，伊朗战争将立刻终止",),
                           ta=("中期选举后",), tb=("大选结束后",), tcon="一致"),
        (j_c, j_h): _jjson("重复", ("大选结束后，伊朗战争将立刻终止",),
                           ("中期选举后伊朗战争将立刻终止",),
                           ta=("大选结束后",), tb=("中期选举后",), tcon="一致"),
    }
    out = _judge(_MockLLM(resp), mv_mode=lr.MV_GATE).judge_pair("p1", j_h, j_c)
    assert out.cell == "doubtful"
    ver = out.hc.proof_verification
    assert jp.P_JTIME in ver.failures
    assert ver.time_check["machine_conflict"] is False   # 机抽无日期不拦
    assert jp.P_TIME not in ver.failures


def test_machine_negation_and_polarity_blocks(monkeypatch):
    """③ 红测：否定词族不对称 / 反义极性对 → P_NEGATION。"""
    _proof_on(monkeypatch)
    p_h = "某公司资金净流入5亿"
    p_c = "某公司资金净流出5亿"
    resp = {
        (p_h, p_c): _jjson("重复", ("资金净流入5亿",), ("资金净流出5亿",),
                           na=("5亿",), nb=("5亿",), tcon="均无时间"),
        (p_c, p_h): _jjson("重复", ("资金净流出5亿",), ("资金净流入5亿",),
                           na=("5亿",), nb=("5亿",), tcon="均无时间"),
    }
    out = _judge(_MockLLM(resp), mv_mode=lr.MV_GATE).judge_pair("p1", p_h, p_c)
    ver = out.hc.proof_verification
    assert jp.P_NEGATION in ver.failures
    assert ver.negation_check["polarity_pairs"] == [["净流入", "净流出"]]
    assert out.cell == "doubtful"
    # 否定词族不对称（一侧"否认"）
    n_h = "某公司否认收到监管函，经营正常"
    n_c = "某公司公告称经营正常，未提监管事项"
    resp2 = {
        (n_h, n_c): _jjson("重复", ("否认收到监管函",), ("经营正常",),
                           ncon="无关键数值", tcon="均无时间"),
        (n_c, n_h): _jjson("重复", ("经营正常",), ("否认收到监管函",),
                           ncon="无关键数值", tcon="均无时间"),
    }
    out2 = _judge(_MockLLM(resp2), mv_mode=lr.MV_GATE).judge_pair(
        "p2", n_h, n_c)
    ver2 = out2.hc.proof_verification
    assert jp.P_NEGATION in ver2.failures
    assert ver2.negation_check["negation_a"] == ["否认"]
    assert ver2.negation_check["negation_asymmetry"] is True


def test_subject_codes_disjoint_blocks(monkeypatch):
    """③ 红测：主体代码互斥判"重复" → P_SUBJECT（归属角色校验主体维度）。"""
    _proof_on(monkeypatch)
    resp = {
        (ND1_H, ND1_C): _jjson("重复", ("易天股份（300812）",),
                               ("名臣健康（002919）",),
                               na=("300812",), nb=("002919",), tcon="均无时间"),
        (ND1_C, ND1_H): _jjson("重复", ("名臣健康（002919）",),
                               ("易天股份（300812）",),
                               na=("002919",), nb=("300812",), tcon="均无时间"),
    }
    out = _judge(_MockLLM(resp), mv_mode=lr.MV_GATE).judge_pair(
        "p1", ND1_H, ND1_C)
    ver = out.hc.proof_verification
    assert jp.P_SUBJECT in ver.failures
    assert ver.subject_check["codes_a"] == ["300812"]
    assert ver.subject_check["codes_b"] == ["002919"]
    assert out.cell == "doubtful"


# ---------------------------------------------------------------- ④ 结论对拍

def test_conclusion_crosscheck_numeric_and_time(monkeypatch):
    """④ 红测：decision="重复" 而分项 conclusion="不一致" → P_CONCLUSION。"""
    _proof_on(monkeypatch)
    resp_n = {
        (G_H, G_C): _jjson("重复", ("营收100万，同比增长5%",),
                           ("营收100万元，同比增长5%",),
                           na=("100万",), nb=("100万",), ncon="不一致",
                           ta=("9月24日",), tb=("9月24日",)),
        (G_C, G_H): _jjson("重复", ("营收100万元，同比增长5%",),
                           ("营收100万，同比增长5%",),
                           na=("100万",), nb=("100万",), ncon="不一致",
                           ta=("9月24日",), tb=("9月24日",)),
    }
    out = _judge(_MockLLM(resp_n), mv_mode=lr.MV_GATE).judge_pair(
        "p1", G_H, G_C)
    assert out.cell == "doubtful"
    ver = out.hc.proof_verification
    assert jp.P_CONCLUSION in ver.failures
    assert ver.conclusion_check["violations"] == [
        {"field": "numeric_check", "conclusion": "不一致"}]
    assert ver.numeric_check["machine_conflict"] is False  # 机检无冲突也拦
    # time_check.conclusion="不一致" 同法
    resp_t = {
        k: _jjson("重复", ("营收100万，同比增长5%",),
                  ("营收100万元，同比增长5%",),
                  na=("100万",), nb=("100万",),
                  ta=("9月24日",), tb=("9月24日",), tcon="不一致")
        for k in resp_n}
    out2 = _judge(_MockLLM(resp_t), mv_mode=lr.MV_GATE).judge_pair(
        "p2", G_H, G_C)
    ver2 = out2.hc.proof_verification
    assert jp.P_CONCLUSION in ver2.failures
    assert ver2.conclusion_check["violations"] == [
        {"field": "time_check", "conclusion": "不一致"}]


# ---------------------------------------------------------------- ⑤ VerifiedJudgeProof

def test_duplicate_proof_issued_field_contract(monkeypatch):
    """⑤ 绿钉：证明字段合同逐项（双文 hash/双序判决/引文 offset/数值时间
    否定阶段检查/机验结果/model·prompt·policy 版本）。"""
    _proof_on(monkeypatch)
    out = _judge(_MockLLM(G_RESP), mv_mode=lr.MV_GATE).judge_pair(
        "p1", G_H, G_C)
    assert out.cell == "signed"
    proof = out.proof
    assert proof is not None and proof.issued is True
    assert proof.proof_type == jp.PROOF_TYPE_DUPLICATE == "duplicate"
    assert proof.pair_id == "p1"
    assert proof.text_history_sha256 == jp.text_sha256(G_H)
    assert proof.text_current_sha256 == jp.text_sha256(G_C)
    assert proof.model == lr.DEFAULT_MODEL
    assert proof.prompt_version == lr.JUDGE_PROMPT_VERSION
    assert proof.prompt_sha256 == lr.PROMPT_SHA256
    assert proof.policy_version == jp.PROOF_POLICY_VERSION == "p1a-proof-v1"
    assert proof.final_decision == "重复"
    assert proof.failure_reasons == ()
    assert len(proof.orders) == 2
    hc = jp.OrderVerification.from_dict(proof.orders[0])
    ch = jp.OrderVerification.from_dict(proof.orders[1])
    assert (hc.order, ch.order) == ("hc", "ch")
    assert hc.decision == "重复" and ch.decision == "重复"
    # 双序各自绑定本顺序 文本A/文本B 角色 hash
    assert hc.text_a_sha256 == jp.text_sha256(G_H)
    assert hc.text_b_sha256 == jp.text_sha256(G_C)
    assert ch.text_a_sha256 == jp.text_sha256(G_C)
    assert ch.text_b_sha256 == jp.text_sha256(G_H)
    # 数值/时间/否定/阶段检查字段在场且机检无冲突有背书
    for ver in (hc, ch):
        assert ver.ok is True and ver.failures == ()
        assert ver.numeric_check["backed"] is True
        assert ver.time_check["backed"] is True
        assert ver.stage_check["machine_conflict"] is False
        assert ver.negation_check["conflict"] is False
        assert ver.subject_check["machine_conflict"] is False
        assert ver.conclusion_check["ok"] is True
        assert ver.citations and not ver.citation_failures


def test_proof_json_roundtrip_and_reverify(monkeypatch):
    """⑤ 可序列化+可复验：JSON 往返逐字节同一；verify_proof_dict 真。"""
    _proof_on(monkeypatch)
    out = _judge(_MockLLM(G_RESP), mv_mode=lr.MV_GATE).judge_pair(
        "p1", G_H, G_C)
    d = out.proof.to_dict()
    blob = json.dumps(d, ensure_ascii=False)
    restored = jp.VerifiedJudgeProof.from_dict(json.loads(blob))
    assert restored.to_dict() == d                    # 往返逐字节同一
    assert jp.verify_proof_dict(d, G_H, G_C) is True
    assert jp.verify_proof_dict(json.loads(blob), G_H, G_C) is True


@pytest.mark.parametrize("mutate", [
    "quote", "span", "issued", "texts", "swap_texts", "policy", "decision",
])
def test_verify_proof_tamper_rejected(monkeypatch, mutate):
    """⑤ 红测：篡改引文/offset/签发标志/文本/版本/判决 → 复验 False。"""
    _proof_on(monkeypatch)
    out = _judge(_MockLLM(G_RESP), mv_mode=lr.MV_GATE).judge_pair(
        "p1", G_H, G_C)
    d = out.proof.to_dict()
    th, tc = G_H, G_C
    if mutate == "quote":
        d["orders"][0]["judgment"]["evidence_a"][0] += "改"
    elif mutate == "span":
        d["orders"][0]["citations"][0]["start"] += 1
    elif mutate == "issued":
        d["issued"] = False
    elif mutate == "texts":
        th = "另一段完全不同的原文"
    elif mutate == "swap_texts":
        th, tc = G_C, G_H
    elif mutate == "policy":
        d["policy_version"] = "p1a-proof-v0"
    elif mutate == "decision":
        d["orders"][0]["judgment"]["decision"] = "存疑"
    assert jp.verify_proof_dict(d, th, tc) is False


def test_audit_mode_observes_without_demotion(monkeypatch):
    """⑦ audit：证明如实记录核验结果（issued=False 可观测）但不降级。"""
    _proof_on(monkeypatch)
    out = _judge(_MockLLM(N_RESP), mv_mode=lr.MV_AUDIT).judge_pair(
        "p1", N_H, N_C)
    assert out.cell == "signed"                       # audit 不降级
    assert out.proof is not None and out.proof.issued is False
    assert "hc:P_NUMERIC" in out.proof.failure_reasons
    assert "ch:P_NUMERIC" in out.proof.failure_reasons
    d = out.to_audit_dict()
    assert d["proof"]["issued"] is False              # 证明入审计负载
    assert "P_NUMERIC" in d["fired_rules_union"]


def test_gate_mode_issued_invariant(monkeypatch):
    """⑦ gate 不变式：幸存 signed cell ⟺ proof.issued（触发 P_* 的顺序已
    降级存疑，cell 自然翻转）。"""
    _proof_on(monkeypatch)
    ok = _judge(_MockLLM(G_RESP), mv_mode=lr.MV_GATE).judge_pair("p1", G_H, G_C)
    assert ok.cell == "signed" and ok.proof.issued is True
    bad = _judge(_MockLLM(N_RESP), mv_mode=lr.MV_GATE).judge_pair(
        "p2", N_H, N_C)
    assert bad.cell == "doubtful" and bad.proof.issued is False


# ---------------------------------------------------------------- ⑥ NotDuplicateProof

def test_not_duplicate_no_axis_not_issuable_audit(monkeypatch):
    """⑥ 核心红测（audit）：模型两次说不重复+机验无轴 → 不得包装成已证
    排除——issued=False，failure=insufficient_falsification_evidence（证据
    不足→未决）。"""
    _proof_on(monkeypatch)
    out = _judge(_MockLLM(ND0_RESP), mv_mode=lr.MV_AUDIT).judge_pair(
        "p1", ND0_H, ND0_C)
    assert out.cell == "not_duplicate"                # audit 不降级
    nd = out.not_duplicate_proof
    assert nd is not None and nd.issued is False
    assert nd.failure == jp.NotDuplicateFailure(
        "insufficient_falsification_evidence").value
    assert nd.axes_union == ()
    assert "证据不足" in nd.failure_detail
    assert out.proof is None
    assert "P_NO_AXIS" in out.fired_rules_union


def test_not_duplicate_no_axis_demoted_in_gate(monkeypatch):
    """⑥ 核心红测（gate）：无轴不重复 → P_NO_AXIS 降级存疑（未证排除不得
    冒充）——cell 变 doubtful，证明留痕 failure 枚举。"""
    _proof_on(monkeypatch)
    out = _judge(_MockLLM(ND0_RESP), mv_mode=lr.MV_GATE).judge_pair(
        "p1", ND0_H, ND0_C)
    assert out.cell == "doubtful"
    assert out.hc.final_decision == "存疑" and out.ch.final_decision == "存疑"
    assert out.hc.decision == "不重复"                # 原判保留可审计
    nd = out.not_duplicate_proof
    assert nd is not None and nd.issued is False
    assert nd.failure == "insufficient_falsification_evidence"
    assert jp.verify_proof_dict(nd.to_dict(), ND0_H, ND0_C) is True  # 败状亦可复验


def test_not_duplicate_subject_axis_issued(monkeypatch):
    """⑥ 绿钉：主体代码互斥证伪轴 → 签发；双侧原文引用 offset+对应关系。"""
    _proof_on(monkeypatch)
    out = _judge(_MockLLM(ND1_RESP), mv_mode=lr.MV_GATE).judge_pair(
        "p1", ND1_H, ND1_C)
    assert out.cell == "not_duplicate"
    nd = out.not_duplicate_proof
    assert nd is not None and nd.issued is True and nd.failure is None
    assert nd.axes_union == ("numeric", "subject")
    assert nd.final_decision == "不重复"
    hc = jp.OrderVerification.from_dict(nd.orders[0])
    subj = next(a for a in hc.axes if a["axis"] == "subject")
    assert subj["quote_a"] == "300812" and subj["quote_b"] == "002919"
    assert ND1_H[subj["start_a"]:subj["end_a"]] == "300812"
    assert ND1_C[subj["start_b"]:subj["end_b"]] == "002919"
    assert "主体代码互斥" in subj["correspondence"]
    assert jp.verify_proof_dict(nd.to_dict(), ND1_H, ND1_C) is True
    # 序列化往返
    blob = json.dumps(nd.to_dict(), ensure_ascii=False)
    assert jp.NotDuplicateProof.from_dict(
        json.loads(blob)).to_dict() == nd.to_dict()


def test_not_duplicate_stage_time_polarity_axes(monkeypatch):
    """⑥ 绿钉：阶段/时间/极性三族证伪轴各可独立签发。"""
    _proof_on(monkeypatch)
    cases = [
        ("某公司今日开盘价10.5元", "某公司今日收盘价10.5元", ("stage",)),
        ("某项目9月24日投产仪式举行", "某项目9月25日投产仪式举行", ("time",)),
        ("美国CPI高于预期，市场反应平淡", "美国CPI低于预期，市场反应平淡",
         ("polarity",)),
    ]
    for i, (h, c, axes_expect) in enumerate(cases):
        resp = {
            (h, c): _jjson("不重复", (h[:8],), (c[:8],),
                           ncon="无关键数值", tcon="均无时间"),
            (c, h): _jjson("不重复", (c[:8],), (h[:8],),
                           ncon="无关键数值", tcon="均无时间"),
        }
        out = _judge(_MockLLM(resp), mv_mode=lr.MV_GATE).judge_pair(
            f"ax{i}", h, c)
        nd = out.not_duplicate_proof
        assert nd is not None and nd.issued is True, (h, c)
        for ax in axes_expect:
            assert ax in nd.axes_union
        assert jp.verify_proof_dict(nd.to_dict(), h, c) is True


def test_not_duplicate_citation_unbound_failure(monkeypatch):
    """⑥ 红测：判"不重复"但引文无法绑定本侧原文 → citation_unbound（即使
    机器证伪轴在场也不签——判官输出不可信）。"""
    _proof_on(monkeypatch)
    resp = {
        (ND1_H, ND1_C): _jjson("不重复", ("幻觉引文不存在",),
                               ("名臣健康（002919）",),
                               na=("300812",), nb=("002919",), tcon="均无时间"),
        (ND1_C, ND1_H): _jjson("不重复", ("名臣健康（002919）",),
                               ("易天股份（300812）",),
                               na=("002919",), nb=("300812",), tcon="均无时间"),
    }
    out = _judge(_MockLLM(resp), mv_mode=lr.MV_AUDIT).judge_pair(
        "p1", ND1_H, ND1_C)
    nd = out.not_duplicate_proof
    assert nd is not None and nd.issued is False
    assert nd.failure == "citation_unbound"
    assert nd.axes_union == ("numeric", "subject")   # 轴在场也不救


def test_dual_order_disagreement_no_proofs(monkeypatch):
    """⑥ 红测：双序分歧（一重复一不重复）→ doubtful，两型证明均不建。"""
    _proof_on(monkeypatch)
    resp = dict(G_RESP)
    resp[(G_C, G_H)] = _jjson("不重复", ("符合预期",), ("业绩符合预期",),
                              ncon="无关键数值", tcon="均无时间")
    out = _judge(_MockLLM(resp), mv_mode=lr.MV_AUDIT).judge_pair(
        "p1", G_H, G_C)
    assert out.cell == "doubtful" and out.order_flip_signed is True
    assert out.proof is None and out.not_duplicate_proof is None
    # 枚举面补钉：build_not_duplicate_proof 直造分歧 → dual_order_disagreement
    ver_dup = jp.verify_order_judgment(
        order="hc", judgment=json.loads(G_RESP[(G_H, G_C)]),
        text_a=G_H, text_b=G_C)
    ver_nd = jp.verify_order_judgment(
        order="ch", judgment=json.loads(resp[(G_C, G_H)]),
        text_a=G_C, text_b=G_H)
    nd = jp.build_not_duplicate_proof(
        pair_id="p1", text_history=G_H, text_current=G_C,
        hc=ver_dup, ch=ver_nd, model="m", prompt_version="judge_v1",
        prompt_sha256="s" * 64)
    assert nd.issued is False
    assert nd.failure == jp.NotDuplicateFailure.DUAL_ORDER_DISAGREEMENT.value


def test_not_duplicate_proof_field_contract(monkeypatch):
    """⑥ 字段合同钉：双文 hash/双序判决/证伪轴并集/版本/失败枚举槽。"""
    _proof_on(monkeypatch)
    out = _judge(_MockLLM(ND1_RESP), mv_mode=lr.MV_GATE).judge_pair(
        "p1", ND1_H, ND1_C)
    nd = out.not_duplicate_proof
    assert nd.proof_type == "not_duplicate"
    assert nd.text_history_sha256 == jp.text_sha256(ND1_H)
    assert nd.text_current_sha256 == jp.text_sha256(ND1_C)
    assert nd.model == lr.DEFAULT_MODEL
    assert nd.prompt_version == lr.JUDGE_PROMPT_VERSION
    assert nd.policy_version == jp.PROOF_POLICY_VERSION
    assert len(nd.orders) == 2
    d = out.to_audit_dict()
    assert d["not_duplicate_proof"]["issued"] is True
    assert set(jp.NotDuplicateFailure) == {
        jp.NotDuplicateFailure.DUAL_ORDER_DISAGREEMENT,
        jp.NotDuplicateFailure.CITATION_UNBOUND,
        jp.NotDuplicateFailure.INSUFFICIENT_FALSIFICATION_EVIDENCE}


# ---------------------------------------------------------------- ⑦ 开关关=老行为逐字节

def test_switch_off_legacy_behavior_byte_identical(monkeypatch, tmp_path):
    """⑦ 锚：开关关——旧缓存键/旧 R0-R6/无证明产物/审计负载无证明键。"""
    # 现役 R 规则照旧触发（R1 主体代码互斥判重复，audit 记录不降级）
    out_legacy = _judge(_MockLLM({
        (ND1_H, ND1_C): _jjson("重复", ("易天股份",), ("名臣健康",),
                               tcon="均无时间"),
        (ND1_C, ND1_H): _jjson("重复", ("名臣健康",), ("易天股份",),
                               tcon="均无时间"),
    }), mv_mode=lr.MV_AUDIT).judge_pair("p1", ND1_H, ND1_C)
    assert out_legacy.signed is True
    assert out_legacy.fired_rules_union == ("R1", "R3")   # 旧 R 系非 P_*
    assert out_legacy.proof is None
    assert out_legacy.not_duplicate_proof is None
    d = out_legacy.to_audit_dict()
    assert "proof" not in d and "not_duplicate_proof" not in d
    # 不重复判定零机验触发（R0 旧纪律：证明开关关时不验不重复）
    out_nd = _judge(_MockLLM(ND0_RESP), mv_mode=lr.MV_GATE).judge_pair(
        "p2", ND0_H, ND0_C)
    assert out_nd.cell == "not_duplicate"            # 旧 gate 不拦不重复
    assert out_nd.hc.interceptions == () and out_nd.ch.interceptions == ()
    # 缓存键：开关关时写入旧键（证明路键不出现在缓存目录）
    _judge(_MockLLM(G_RESP), cache_dir=str(tmp_path)).judge_pair(
        "p3", G_H, G_C)
    legacy_key = lr.residual_cache_key(lr.DEFAULT_MODEL,
                                       lr.JUDGE_PROMPT_VERSION, "p3",
                                       lr.PROMPT_SHA256, "hc")
    hardened_key = lr.residual_cache_key_hardened(
        lr.DEFAULT_MODEL, lr.JUDGE_PROMPT_VERSION, "p3", lr.PROMPT_SHA256,
        "hc", jp.text_sha256(G_H), jp.text_sha256(G_C))
    assert (tmp_path / f"{legacy_key}.json").exists()
    assert not (tmp_path / f"{hardened_key}.json").exists()


def test_failure_and_invalid_paths_carry_no_proof(monkeypatch):
    """⑦ 边界钉：failure/invalid 单列路径证明恒 None（不冒充、不折算）。"""
    _proof_on(monkeypatch)
    out = _judge(_MockLLM({k: ConnectionError("mock 断网")
                           for k in G_RESP})).judge_pair("p1", G_H, G_C)
    assert out.cell == "failure"
    assert out.proof is None and out.not_duplicate_proof is None
    resp = {k: "此处不是 JSON" for k in G_RESP}
    out2 = _judge(_MockLLM(resp)).judge_pair("p2", G_H, G_C)
    assert out2.cell == "invalid"
    assert out2.proof is None and out2.not_duplicate_proof is None


# ---------------------------------------------------------------- 机检原语直钉

def test_machine_primitives_direct_pins():
    """机检原语直钉（machine_verify P1-a 增量面）。"""
    text = "甲公司9月24日公告：营收100万，9月24日生效"
    spans = mv.find_quote_spans("9月24日", text)
    assert spans == ((3, 8), (18, 23))
    assert mv.find_quote_spans("9月 24 日", text) == ()   # 伪匹配无通道
    assert mv.extract_time_set(text) == {"924"}
    assert mv.extract_stage_set(text) == {"生效"}
    mentions = mv.number_mentions("营收100万，9月24日投产")
    # 日期型剔除（"9"/"24"后随月日）；裸"万"为 _NUM_CN 中文数词正则的现役
    # 同款匹配（legacy extract_number_set 同语义，norm=0，双侧对称不影响
    # R3 差集）——钉住防静默漂移
    assert [(m["surface"], m["norm"]) for m in mentions] == [("100万", "1E+6"),
                                                             ("万", "0")]
    assert mentions[0]["start"] == 2
    codes = mv.subject_code_mentions("易天股份（300812）净流入")
    assert codes[0]["surface"] == "300812" and codes[0]["start"] == 5
    comp = mv.machine_time_stage_compare("初值3月4日公布", "终值3月5日公布")
    assert comp["time_conflict"] is True and comp["stage_conflict"] is True
    comp2 = mv.machine_time_stage_compare("3月4日公告", "3月5日公告")
    assert comp2["time_conflict"] is False       # 无阶段词在场的日期差宽放
    neg = mv.machine_negation_compare("公司否认指控", "公司公告称正常")
    assert neg["negation_asymmetry"] is True and neg["conflict"] is True
