"""窗口T · 残判层单元钉值（decide/llm_residual.py）。

钉值面：
- 提示词/缓存键：judge_v1 内联提示词 sha256 钉死（与 D32 prompts/judge_v1.md
  逐字节对拍，integration 侧另有文件级对拍）；缓存键含
  model+prompt_version+order+pair_id 且 hc/ch 布局与 D32 d32_lib 同构；
- 双向装配全格：双签→signed；恰一签→doubtful（order_flip）；双不重复→
  not_duplicate；invalid/failure 单列（不折算、不冒充）；疑似度公式钉值；
- R0 闸门纪律：机验仅对判"重复"的判定计算（不重复判定零机验触发）；
- mv_mode：audit（默认）不降级、触发规则入审计；gate 拦截→存疑；
- 缓存确定性：同一缓存目录双 judge 复跑，第二遍零 API 全命中、审计负载同一；
  损坏条目按 miss（fail-closed）；
- 失败纪律：连续失败→failure 计数入台账（INV 式，不折算边界）；
- 异步接口骨架：ResidualJudgePort submit/collect 票据式同步实现。
"""

from __future__ import annotations

import hashlib
import json
import re

import pytest

from news_flash_dedup.decide import llm_residual as lr


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
        "reason": "测试判定",
    }, ensure_ascii=False)


class _MockLLM:
    """按 (文本A, 文本B) 精确路由的罐装响应；resp 为 Exception 实例时抛出。"""

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


# 注意：H/C 必须互异——_MockLLM 以 (文本A,文本B) 元组为键，H==C 会使 (H,C)
# 与 (C,H) 撞键（两顺序路由不分）。引文"甲公司营收100万"同为两侧子串。
H, C = "甲公司营收100万", "甲公司营收100万元整"
_SIGNED_RESP = {
    (H, C): _jjson("重复", ("甲公司营收100万",), ("甲公司营收100万",),
                   na=("100万",), nb=("100万",)),
    (C, H): _jjson("重复", ("甲公司营收100万",), ("甲公司营收100万",),
                   na=("100万",), nb=("100万",)),
}


# ---------------------------------------------------------------- 提示词 / 缓存键

def test_prompt_sha256_pinned_to_d32_judge_v1():
    # 与 log/temp/d32-llm-judge/prompts/judge_v1.md 正文逐字节对拍值
    assert lr.PROMPT_SHA256 == (
        "add219156ff72a0b9a8cabffc73eb975aa4ad51ae1a80c98c8a88118ba9cd149")
    assert lr.judge_prompt_sha256() == lr.PROMPT_SHA256
    assert lr.JUDGE_PROMPT_VERSION == "judge_v1"


def test_cache_key_layout_d32_compatible():
    model, pid, sha = "qwen-turbo", "pair-x", "s" * 64
    # hc 不书 order 维度（与 D32 既有条目同键）；ch 在 arm 后插入 order 维度
    expect_hc = hashlib.sha256(
        f"{model}|judge_v1|A|{pid}|{sha}".encode("utf-8")).hexdigest()
    expect_ch = hashlib.sha256(
        f"{model}|judge_v1|A|ch|{pid}|{sha}".encode("utf-8")).hexdigest()
    assert lr.residual_cache_key(model, "judge_v1", pid, sha, "hc") == expect_hc
    assert lr.residual_cache_key(model, "judge_v1", pid, sha, "ch") == expect_ch


def test_cache_key_dimensions():
    base = lr.residual_cache_key("qwen-turbo", "judge_v1", "pid", "s" * 64, "hc")
    assert lr.residual_cache_key("other-model", "judge_v1", "pid", "s" * 64, "hc") != base
    assert lr.residual_cache_key("qwen-turbo", "judge_v2", "pid", "s" * 64, "hc") != base
    assert lr.residual_cache_key("qwen-turbo", "judge_v1", "pid2", "s" * 64, "hc") != base
    assert lr.residual_cache_key("qwen-turbo", "judge_v1", "pid", "s" * 64, "ch") != base
    assert lr.residual_cache_key("qwen-turbo", "judge_v1", "pid", "t" * 64, "hc") != base
    with pytest.raises(ValueError):
        lr.residual_cache_key("qwen-turbo", "judge_v1", "pid", "s" * 64, "xx")


# ---------------------------------------------------------------- 双向装配全格

def test_both_orders_signed():
    out = _judge(_MockLLM(_SIGNED_RESP)).judge_pair("p1", H, C)
    assert out.signed is True and out.cell == "signed"
    assert out.suspicion_score == 1.0
    assert out.order_flip_signed is False
    assert out.hc.final_decision == "重复" and out.ch.final_decision == "重复"


def test_order_flip_signed_becomes_doubtful():
    resp = dict(_SIGNED_RESP)
    resp[(C, H)] = _jjson("不重复", ("甲公司营收100万",), ("甲公司营收100万",))
    out = _judge(_MockLLM(resp)).judge_pair("p1", H, C)
    assert out.signed is False and out.cell == "doubtful"
    assert out.order_flip_signed is True
    assert out.suspicion_score == 0.5          # 0.5×(1.0+0.0)−0


def test_both_orders_not_duplicate():
    resp = {k: _jjson("不重复", ("甲公司营收100万",), ("甲公司营收100万",))
            for k in _SIGNED_RESP}
    out = _judge(_MockLLM(resp)).judge_pair("p1", H, C)
    assert out.signed is False and out.cell == "not_duplicate"
    assert out.suspicion_score == 0.0


def test_both_orders_doubtful():
    resp = {k: _jjson("存疑", ("甲公司营收100万",), ("甲公司营收100万",))
            for k in _SIGNED_RESP}
    out = _judge(_MockLLM(resp)).judge_pair("p1", H, C)
    assert out.signed is False and out.cell == "doubtful"
    assert out.suspicion_score == 0.5          # 0.5×(0.5+0.5)


def test_invalid_output_single_listed():
    resp = dict(_SIGNED_RESP)
    resp[(C, H)] = "此处不是 JSON"
    out = _judge(_MockLLM(resp)).judge_pair("p1", H, C)
    assert out.signed is False and out.cell == "invalid"
    assert out.suspicion_score is None
    assert out.ch.status == "invalid" and out.ch.struct_error
    assert out.hc.status == "ok"


def test_struct_invalid_missing_evidence():
    bad = json.dumps({"decision": "重复", "evidence_a": [],
                       "evidence_b": ["x"], "numeric_check": {},
                       "time_check": {}, "reason": "r"}, ensure_ascii=False)
    resp = {k: bad for k in _SIGNED_RESP}
    out = _judge(_MockLLM(resp)).judge_pair("p1", H, C)
    assert out.cell == "invalid"
    assert out.hc.status == "invalid" and out.ch.status == "invalid"


# ---------------------------------------------------------------- R0 纪律 / mv_mode

def test_r0_mv_only_computed_for_signed_decision():
    # 判"不重复"时即使文本会触发 R1，机验也不计算（R0：仅闸/验"重复"）
    h, c = "易天股份（300812）近5日净流入", "名臣健康（002919）近5日净流入"
    resp = {
        (h, c): _jjson("不重复", ("易天股份",), ("名臣健康",)),
        (c, h): _jjson("不重复", ("名臣健康",), ("易天股份",)),
    }
    out = _judge(_MockLLM(resp)).judge_pair("p1", h, c)
    assert out.cell == "not_duplicate"
    assert out.hc.interceptions == () and out.ch.interceptions == ()
    assert out.fired_rules_union == ()


def test_audit_mode_records_fired_rules_without_demotion():
    h, c = "易天股份（300812）近5日净流入", "名臣健康（002919）近5日净流入"
    resp = {
        (h, c): _jjson("重复", ("易天股份",), ("名臣健康",)),
        (c, h): _jjson("重复", ("名臣健康",), ("易天股份",)),
    }
    out = _judge(_MockLLM(resp), mv_mode=lr.MV_AUDIT).judge_pair("p1", h, c)
    assert out.signed is True                            # audit：不降级
    assert out.fired_rules_union == ("R1", "R3")         # 300812 vs 2919 双向差异
    assert out.suspicion_score == 0.9                    # 1.0−0.05×2


def test_gate_mode_demotes_on_interception():
    h, c = "易天股份（300812）近5日净流入", "名臣健康（002919）近5日净流入"
    resp = {
        (h, c): _jjson("重复", ("易天股份",), ("名臣健康",)),
        (c, h): _jjson("重复", ("名臣健康",), ("易天股份",)),
    }
    out = _judge(_MockLLM(resp), mv_mode=lr.MV_GATE).judge_pair("p1", h, c)
    assert out.signed is False and out.cell == "doubtful"
    assert out.hc.final_decision == "存疑" and out.ch.final_decision == "存疑"
    assert out.hc.decision == "重复"                     # 原判保留可审计


def test_mv_mode_invalid_rejected():
    with pytest.raises(ValueError):
        lr.ResidualJudgeConfig(mv_mode="bogus")


# ---------------------------------------------------------------- 缓存确定性

def test_cache_determinism_dual_run(tmp_path):
    mock1 = _MockLLM(_SIGNED_RESP)
    out1 = _judge(mock1, cache_dir=str(tmp_path)).judge_pair("p1", H, C)
    assert mock1.calls == 2                              # hc+ch 各一次真实调用
    mock2 = _MockLLM(_SIGNED_RESP)
    ledger2 = lr.ResidualJudgeLedger()
    out2 = _judge(mock2, cache_dir=str(tmp_path),
                  ledger=ledger2).judge_pair("p1", H, C)
    assert mock2.calls == 0                              # 第二遍全缓存命中零调用
    assert ledger2.snapshot()["cache_hits"] == 2
    assert out1.to_audit_dict() == out2.to_audit_dict()  # 审计负载逐字节同一


def test_corrupted_cache_entry_treated_as_miss(tmp_path):
    _judge(_MockLLM(_SIGNED_RESP), cache_dir=str(tmp_path)).judge_pair("p1", H, C)
    key_hc = lr.residual_cache_key(lr.DEFAULT_MODEL, lr.JUDGE_PROMPT_VERSION,
                                   "p1", lr.PROMPT_SHA256, "hc")
    (tmp_path / f"{key_hc}.json").write_text("{损坏的JSON", encoding="utf-8")
    mock = _MockLLM(_SIGNED_RESP)
    ledger = lr.ResidualJudgeLedger()
    _judge(mock, cache_dir=str(tmp_path), ledger=ledger).judge_pair("p1", H, C)
    assert mock.calls == 1                               # 仅 hc 损坏条目重算
    snap = ledger.snapshot()
    assert snap["cache_hits"] == 1 and snap["api_calls"] == 1
    assert snap["failures"] == 0


# ---------------------------------------------------------------- 失败纪律 / 重试

def test_failure_after_retries_single_listed(monkeypatch):
    monkeypatch.setattr(lr.time, "sleep", lambda _s: None)
    mock = _MockLLM({k: ConnectionError("mock 网络中断") for k in _SIGNED_RESP})
    ledger = lr.ResidualJudgeLedger()
    out = _judge(mock, ledger=ledger).judge_pair("p1", H, C)
    assert out.signed is False and out.cell == "failure"
    assert out.suspicion_score is None
    assert out.hc.status == "failure" and out.ch.status == "failure"
    snap = ledger.snapshot()
    assert snap["failures"] == 2                         # 两顺序各记一次（INV 式单列）
    assert snap["api_calls"] == 2 * 4                    # 首次+3 重试 × 2 顺序
    assert snap["retries"] == 2 * 3


def test_retry_then_success(monkeypatch):
    monkeypatch.setattr(lr.time, "sleep", lambda _s: None)
    state = {"n": 0}
    good = _SIGNED_RESP[(H, C)]

    def flaky(*, model, system, user):
        state["n"] += 1
        if state["n"] == 1:
            raise TimeoutError("mock 瞬时超时")
        return good, 0.01

    ledger = lr.ResidualJudgeLedger()
    out = _judge(flaky, ledger=ledger).judge_pair("p1", H, C)
    assert out.signed is True
    snap = ledger.snapshot()
    assert snap["api_calls"] == 3 and snap["retries"] == 1
    assert snap["failures"] == 0
    assert out.hc.attempts == 2 and out.ch.attempts == 1


# ---------------------------------------------------------------- 异步接口骨架

def test_port_submit_collect_sync_form():
    judge = _judge(_MockLLM(_SIGNED_RESP))
    ticket = judge.submit(lr.ResidualTask(pair_id="p1", text_history=H,
                                          text_current=C))
    assert ticket == "p1"
    out = judge.collect(ticket)
    assert out.signed is True and out.pair_id == "p1"


# ---------------------------------------------------------------- 结构校验钉值

def test_validate_judge_structural_errors():
    payload = json.loads(_SIGNED_RESP[(H, C)])
    j, err = lr.validate_judge(payload)
    assert err is None and j["decision"] == "重复"
    for mutant, frag in (
        ({**payload, "decision": "可能重复"}, "decision 非法"),
        ({**payload, "evidence_a": []}, "evidence_a"),
        ({**payload, "numeric_check": None}, "numeric_check"),
        ({**payload, "time_check": {"times_a": [], "times_b": []}},
         "time_check.conclusion"),
        ({**payload, "reason": ""}, "reason"),
    ):
        j2, err2 = lr.validate_judge(mutant)
        assert j2 is None and err2 and frag in err2


def test_parse_json_loose_fence_and_noise():
    inner = json.loads(_SIGNED_RESP[(H, C)])
    fenced = "前言噪声```json\n" + json.dumps(inner, ensure_ascii=False) + "\n```后缀"
    assert lr.parse_json_loose(fenced)["decision"] == "重复"
    with pytest.raises(ValueError):
        lr.parse_json_loose("没有 JSON 对象")
