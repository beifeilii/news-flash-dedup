# -*- coding: utf-8 -*-
"""C5R 判官口径加固窗 · judge prompt v2 单元钉（新文件；既有件零改动）。

钉守面（任务书①②⑤）：
- v2 版本号/sha256 入档钉死（llm_bidi_v2 模式闸参数=ResidualJudgeConfig.prompt_version）；
- v2 = v1 + 最小增量条款（单句插入，锚句唯一，diff 逐字钉死——防转写误差）；
- v1 保留可切回：缺省 prompt_version=judge_v1，既有钉（PROMPT_SHA256=add21915…）不动；
- 未知版本 fail-closed（ValueError）；
- 缓存键隔离：同对 v1/v2 键必异（prompt_sha 维度），prompt 变更→全缓存失效=全量重判；
- 装配级 mock 钉：指标身份差异型不重复判 → not_duplicate；单顺序签 → 顺序翻案格。
"""
from __future__ import annotations

import hashlib

import pytest

from news_flash_dedup.decide import llm_residual as lr

# C5R 任务书①最小增量条款（逐字钉；与 log/temp/c5r-probe-v2sha.py 构造同一）
_ANCHOR = "属事件更新，判不重复。"
_CLAUSE = ("统计指标/经济指标的口径定义身份差异属关键对象差异"
           "（如 CPI 与调和 CPI/HICP、同比与环比、初值与终值/修正值、"
           "名义与实际、总量与人均）：对应指标口径定义不同即关键对象不同，"
           "即使数值、时间完全一致也不救，判不重复。")

# v2 钉值（构造口径=v1 正文+单句插入；log/temp/c5r-probe-v2sha.py 预算同一）
_EXPECTED_V2_SHA256 = (
    "b5214c206ef085b08851f6728926af3905fcc6424314920894a175c58b6c37bf")


def _jjson(decision, ea, eb, na=(), nb=(), ta=(), tb=(),
           nc="一致", tc="一致", reason="r"):
    import json
    return json.dumps({
        "decision": decision, "evidence_a": list(ea), "evidence_b": list(eb),
        "numeric_check": {"numbers_a": list(na), "numbers_b": list(nb),
                          "conclusion": nc},
        "time_check": {"times_a": list(ta), "times_b": list(tb),
                       "conclusion": tc},
        "reason": reason}, ensure_ascii=False)


class _MockLLM:
    """(文本A,文本B) → 预置响应（tests/unit/test_decide_llm_residual.py 同款）。"""

    def __init__(self, table):
        self.table = table
        self.calls = []

    def __call__(self, *, model, system, user):
        import re
        m = re.search(r"【文本A】\n(.*?)\n\n【文本B】\n(.*)\Z", user, re.DOTALL)
        self.calls.append((m.group(1), m.group(2), system))
        return self.table[(m.group(1), m.group(2))], 0.01


# ---------------------------------------------------------------- 版本/sha 入档钉

def test_v2_version_and_sha256_pinned():
    assert lr.JUDGE_PROMPT_VERSION_V2 == "judge_v2"
    assert lr.PROMPT_SHA256_V2 == _EXPECTED_V2_SHA256
    # 独立复算（不读模块自算值，防同源造假）
    assert hashlib.sha256(lr.JUDGE_PROMPT_V2.encode("utf-8")).hexdigest() == (
        _EXPECTED_V2_SHA256)


def test_v2_prompt_minimal_increment_single_sentence():
    """v2 = v1 + 恰一句插入（锚句在 v1 唯一）；diff 逐字钉死=任务书①条款。"""
    assert lr.JUDGE_PROMPT_V1.count(_ANCHOR) == 1
    assert lr.JUDGE_PROMPT_V2 == lr.JUDGE_PROMPT_V1.replace(
        _ANCHOR, _ANCHOR + _CLAUSE)
    # 条款实体钉：口径身份族例齐全（CPI/HICP、同比/环比、初值/终值/修正值、
    # 名义/实际、总量/人均）+ "关键对象差异→不重复" 映射 + "数值全同不救"
    for token in ("口径定义身份", "关键对象差异", "调和 CPI/HICP", "同比与环比",
                  "初值与终值/修正值", "名义与实际", "总量与人均", "不救"):
        assert token in _CLAUSE, f"条款缺要素 {token!r}"


def test_v1_retained_default_switchback():
    """v1 保留可切回：缺省=judge_v1；既有钉值 add21915… 不动（零翻转面）。"""
    assert lr.JUDGE_PROMPT_VERSION == "judge_v1"
    assert lr.PROMPT_SHA256 == (
        "add219156ff72a0b9a8cabffc73eb975aa4ad51ae1a80c98c8a88118ba9cd149")
    cfg = lr.ResidualJudgeConfig()
    assert cfg.prompt_version == "judge_v1"
    prompt, sha = lr.judge_prompt_for_version("judge_v1")
    assert prompt == lr.JUDGE_PROMPT_V1 and sha == lr.PROMPT_SHA256
    prompt2, sha2 = lr.judge_prompt_for_version("judge_v2")
    assert prompt2 == lr.JUDGE_PROMPT_V2 and sha2 == lr.PROMPT_SHA256_V2


def test_unknown_version_fail_closed():
    with pytest.raises(ValueError):
        lr.ResidualJudgeConfig(prompt_version="judge_v9")
    with pytest.raises(ValueError):
        lr.judge_prompt_for_version("judge_v0")
    with pytest.raises(ValueError):
        lr.judge_prompt_for_version("")


def test_v2_cache_key_isolation_full_invalidation():
    """prompt 变更 → 缓存键全异（prompt_version+prompt_sha 双维度）= 全量重判。"""
    pid = "pair-x"
    k1 = lr.residual_cache_key("qwen-turbo", "judge_v1", pid,
                               lr.PROMPT_SHA256, "hc")
    k2 = lr.residual_cache_key("qwen-turbo", "judge_v2", pid,
                               lr.PROMPT_SHA256_V2, "hc")
    assert k1 != k2
    # ch 布局同构（order 维度在 arm 后）
    k1c = lr.residual_cache_key("qwen-turbo", "judge_v1", pid,
                                lr.PROMPT_SHA256, "ch")
    k2c = lr.residual_cache_key("qwen-turbo", "judge_v2", pid,
                                lr.PROMPT_SHA256_V2, "ch")
    assert k1c != k2c and k2 != k2c
    expect = hashlib.sha256(
        f"qwen-turbo|judge_v2|A|{pid}|{lr.PROMPT_SHA256_V2}".encode("utf-8")
    ).hexdigest()
    assert k2 == expect


# ---------------------------------------------------------------- 装配级 mock 钉（v2 路径）

_H = "德国8月CPI环比终值为0.2%，与预期及初值一致。（产联社）"
_C = "德国8月调和CPI环比终值为0.2%，与预期及初值一致。（产联社）"
_V2_NOTDUP_RESP = {
    (_H, _C): _jjson("不重复", ("德国8月CPI环比终值",), ("德国8月调和CPI环比终值",),
                     na=("0.2%",), nb=("0.2%",),
                     reason="CPI 与调和 CPI/HICP 口径定义身份不同=关键对象差异"),
    (_C, _H): _jjson("不重复", ("德国8月调和CPI环比终值",), ("德国8月CPI环比终值",),
                     na=("0.2%",), nb=("0.2%",),
                     reason="CPI 与调和 CPI/HICP 口径定义身份不同=关键对象差异"),
}


def test_v2_judge_uses_v2_prompt_and_cache_namespace(tmp_path):
    """v2 配置下 system=JUDGE_PROMPT_V2 且缓存条目登记 v2 键材（prompt_sha/judge_v2）。"""
    mock = _MockLLM(_V2_NOTDUP_RESP)
    judge = lr.SyncResidualJudge(
        lr.ResidualJudgeConfig(cache_dir=str(tmp_path), prompt_version="judge_v2"),
        call_fn=mock)
    out = judge.judge_pair("p-fp", _H, _C)
    assert out.cell == "not_duplicate" and out.signed is False
    assert mock.calls and all(c[2] == lr.JUDGE_PROMPT_V2 for c in mock.calls)
    entries = list(tmp_path.glob("*.json"))
    assert len(entries) == 2
    import json
    for p in entries:
        body = json.loads(p.read_text(encoding="utf-8"))
        assert body["key_material"]["prompt_version"] == "judge_v2"
        assert body["key_material"]["prompt_sha256"] == lr.PROMPT_SHA256_V2


def test_v2_indicator_identity_clause_not_signed():
    """口径身份差异型 mock（双向不重复）→ cell=not_duplicate（禁重复钉的装配面）。"""
    judge = lr.SyncResidualJudge(
        lr.ResidualJudgeConfig(prompt_version="judge_v2"),
        call_fn=_MockLLM(_V2_NOTDUP_RESP))
    out = judge.judge_pair("p-fp", _H, _C)
    assert out.signed is False
    assert out.cell == "not_duplicate"
    assert out.order_flip_signed is False


def test_v2_order_flip_still_doubtful():
    """单顺序签"重复"另一顺序否 → doubtful（顺序翻案格语义 v2 不变）。"""
    resp = dict(_V2_NOTDUP_RESP)
    resp[(_C, _H)] = _jjson("重复", ("德国8月调和CPI环比终值",),
                            ("德国8月CPI环比终值",),
                            na=("0.2%",), nb=("0.2%",))
    judge = lr.SyncResidualJudge(
        lr.ResidualJudgeConfig(prompt_version="judge_v2"),
        call_fn=_MockLLM(resp))
    out = judge.judge_pair("p-flip", _H, _C)
    assert out.signed is False and out.cell == "doubtful"
    assert out.order_flip_signed is True
