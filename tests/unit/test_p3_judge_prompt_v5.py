# -*- coding: utf-8 -*-
"""提交二单测（2026-10-10，分支 p3-semantic-authority）：judge_v5 创建并接入。

方案：log/快讯去重_判官与证明层改造_可执行技术方案.md §5.2 文件 E/F/G/H。

钉值面：
1. v5 已注册 _JUDGE_PROMPTS（v1/v2/v3 原样可切回、v4 维持不注册）；
2. v5 SHA 稳定（字面量钉 + 重算自证）；
3. 提交一修复并入复审条 3（judge_v5 不得无条件默认）：装配默认 prompt
   随 DEDUP_JUDGE_DECISION_MODE 分发——legacy_proof_gate（默认）→
   judge_v1；semantic_authority→judge_v5；
4. v5 正文九条业务条款 + R7 主体回填 / R8 前值修订统一判边界（未冻结
   产品规则不许抢跑判重复）；复审条 4（v5 预发布修订）：【暂定边界条
   款】开头补优先级条款，SHA 钉同步重钉；
5. policy 版本单源无循环依赖（judge_prompt_v5 不反向导入 run_manifest）；
6. v5 在线链端到端：system prompt=v5 正文、合同证明 prompt_sha/policy
   版本如实、v1 族输出契约 validate_judge 零改动消费、JSON 非法仍
   invalid、双序合并仍由调用侧完成。
"""

from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path

import pytest

from news_flash_dedup.decide import judge_adapter, judge_pair
from news_flash_dedup.decide import judge_prompt_v5 as v5
from news_flash_dedup.decide import llm_residual as lr
from news_flash_dedup.decide import policy_version as pv
from news_flash_dedup.lib import run_manifest as rm


# ---------------------------------------------------------------- 1. 注册钉

def test_v5_registered_and_old_versions_untouched():
    """§5.2 文件 G-1/2：v5 注册；v1/v2/v3 逐字原样（可切回）；v4 仍不注册。"""
    assert sorted(lr._JUDGE_PROMPTS) == ["judge_v1", "judge_v2", "judge_v3",
                                         "judge_v5"]
    text, sha = lr.judge_prompt_for_version("judge_v5")
    assert text == v5.JUDGE_PROMPT_V5 and sha == v5.PROMPT_SHA256_V5
    # 旧版本零漂移（注册处取值=模块常量同一对象）
    assert lr.judge_prompt_for_version("judge_v1") == (
        lr.JUDGE_PROMPT_V1, lr.PROMPT_SHA256)
    assert lr.judge_prompt_for_version("judge_v2") == (
        lr.JUDGE_PROMPT_V2, lr.PROMPT_SHA256_V2)
    assert lr.judge_prompt_for_version("judge_v3") == (
        lr.JUDGE_PROMPT_V3, lr.PROMPT_SHA256_V3)
    # 全局隐式默认未动（新链显式指定 v5，不依赖默认值改写）
    assert lr.JUDGE_PROMPT_VERSION == "judge_v1"
    # v4 维持不注册（其专属测试 test_p2_judge_prompt_v4 守卫在案）
    with pytest.raises(ValueError):
        lr.judge_prompt_for_version("judge_v4")


def test_v5_sha256_stable_literal_pin():
    """§5.2 文件 F：v5 独立 SHA（不改写 v4 沿用旧 SHA）——字面量钉 +
    重算自证双闸。提交二复审条 4（v5 预发布修订：补暂定边界条款优先
    级条款）：SHA 随正文重算，本钉同步重钉。"""
    assert v5.PROMPT_SHA256_V5 == hashlib.sha256(
        v5.JUDGE_PROMPT_V5.encode("utf-8")).hexdigest()
    assert v5.PROMPT_SHA256_V5 == (
        "25deb3d52cca233561c1b5eeccad3d481c2ecf5cbf76667182cb8f1b55bf77a6")
    # v5 SHA 不与任何旧版本雷同
    assert v5.PROMPT_SHA256_V5 not in (
        lr.PROMPT_SHA256, lr.PROMPT_SHA256_V2, lr.PROMPT_SHA256_V3)


# ---------------------------------------------------------------- 2. 装配默认随模式分发

def test_adapter_default_config_dispatched_by_mode(monkeypatch):
    """提交一修复并入复审条 3（judge_v5 不得无条件默认，取代原 §5.2
    G-3"新链默认显式 v5"口径）：build_judge_callable 未显式传 judge/
    config 时，默认 ResidualJudgeConfig 的 prompt_version 由
    DEDUP_JUDGE_DECISION_MODE 决定——legacy_proof_gate（默认）→
    judge_v1；semantic_authority→judge_v5；mv_mode 恒 audit。"""
    captured = []

    class _SpyJudge:
        def __init__(self, cfg, budget=None):
            captured.append(cfg)

    monkeypatch.setattr(lr, "SyncResidualJudge", _SpyJudge)
    cb = judge_adapter.build_judge_callable(
        environ={"DEDUP_JUDGE_PROOF": "1"})          # 模式缺席=legacy 默认
    assert cb is not None
    assert captured[-1].prompt_version == "judge_v1"
    assert captured[-1].mv_mode == lr.MV_AUDIT
    cb2 = judge_adapter.build_judge_callable(
        environ={"DEDUP_JUDGE_PROOF": "1",
                 "DEDUP_JUDGE_DECISION_MODE": "legacy_proof_gate"})
    assert cb2 is not None
    assert captured[-1].prompt_version == "judge_v1"
    cb3 = judge_adapter.build_judge_callable(
        environ={"DEDUP_JUDGE_PROOF": "1",
                 "DEDUP_JUDGE_DECISION_MODE": "semantic_authority"})
    assert cb3 is not None
    assert captured[-1].prompt_version == "judge_v5"
    assert captured[-1].mv_mode == lr.MV_AUDIT


def test_explicit_config_still_honored(monkeypatch):
    """显式 config 优先（旧版本可切回通道不堵死）。"""
    captured = {}

    class _SpyJudge:
        def __init__(self, cfg, budget=None):
            captured["cfg"] = cfg

    monkeypatch.setattr(lr, "SyncResidualJudge", _SpyJudge)
    cfg = lr.ResidualJudgeConfig(prompt_version=lr.JUDGE_PROMPT_VERSION_V3)
    cb = judge_adapter.build_judge_callable(
        config=cfg, environ={"DEDUP_JUDGE_PROOF": "1"})
    assert cb is not None
    assert captured["cfg"].prompt_version == "judge_v3"


# ---------------------------------------------------------------- 3. v5 正文条款锚点

CLAUSE_ANCHORS = (
    # 条款 1：唯一输入是正文
    "唯一输入是正文", "不使用来源渠道、标题、发布时间、接收时间",
    # 条款 7：来源尾注不参与语义判定
    "来源尾注", "不参与语义判定",
    # 条款 2：同义改写/动词替换/信息补充缺失+槽位不冲突可判重复
    "同义改写、动词替换、单方信息补充或缺失", "已出现槽位不冲突",
    # 条款 3：同槽位冲突判不重复（时间/数值/方向/单位/阶段/关键对象）
    "同一槽位两侧均出现且归一化后不同", "时间、数值、方向、单位、阶段、关键对象",
    # 条款 4：无损等价+相对时间无锚点允许重复，不强制 C14
    "今日/近日", "没有正文外绝对锚点，也允许判重复",
    "不得再因相对时间无锚点强制判存疑",
    # 条款 5：一侧仅缺证券代码不算主体完全缺失
    "一侧仅缺证券代码", "不视为\"主体完全缺失\"",
    # 条款 6 + R7：主体完全缺失→边界；看似可回填也一律存疑（不抢跑）
    "主体完全缺失", "无法仅依据正文唯一确认主体时，判存疑",
    "看起来可以唯一回填主体", "一律存疑，不得判重复",
    # R8：前值修订→边界（不抢跑）
    "前值修订", "177.9万", "177.5万",
    # 复审条 4（v5 预发布修订）：暂定边界条款优先级条款
    "暂定边界条款优先级高于一般重复/不重复规则",
    "不再适用一般数值冲突规则",
    # 条款 8：置信不足必须输出边界
    "证据不足、置信不足", "必须输出存疑",
    # 条款 9：不输出 action/KEEP/SUPPRESS
    "不输出 action/KEEP/SUPPRESS",
    # policy 字面量
    "policy_version=policy_v3",
)


@pytest.mark.parametrize("anchor", CLAUSE_ANCHORS)
def test_v5_prompt_contains_clause_anchor(anchor):
    assert anchor in v5.JUDGE_PROMPT_V5, f"v5 缺条款锚点：{anchor}"


def test_v5_prompt_output_contract_is_v1_family():
    """v5 输出契约沿用 v1 族字段（在线链 validate_judge 零改动消费）。"""
    for field in ('"decision"', '"evidence_a"', '"evidence_b"',
                  '"numeric_check"', '"time_check"', '"reason"'):
        assert field in v5.JUDGE_PROMPT_V5
    # v5 不引入 dual_order_consistent 自检字段（双序合并由调用侧完成）
    assert "dual_order_consistent" not in v5.JUDGE_PROMPT_V5


# ---------------------------------------------------------------- 4. policy 单源 / 无循环依赖

def test_policy_version_single_source_no_reverse_import():
    """§5.2 文件 E：policy 字面量单源=decide/policy_version.py；
    judge_prompt_v5 不反向导入 lib/run_manifest.py（AST 级守卫）。"""
    assert v5.POLICY_VERSION == pv.POLICY_VERSION_V3 == "policy_v3"
    assert pv.DEFAULT_POLICY_VERSION == pv.POLICY_VERSION_V3
    assert rm.DEFAULT_POLICY_VERSION == "policy_v3"     # 同源再导出
    source_path = Path(v5.__file__)
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert not any("run_manifest" in name for name in imported)


# ---------------------------------------------------------------- 5. v5 在线链端到端

# 提交一修复（整改令三 + 复审条 5 指定文本）：判"重复"的样本必须同
# 主体、无核心硬冲突（原"甲公司/乙公司营收100万→重复"夹具已废弃）。
DUP_H = "甲公司9月24日公告营收100万元。"
DUP_C = "甲公司公告称，9月24日营收为100万元。"


def _jjson(decision, ea, eb, *, na=(), nb=(), ta=(), tb=(),
           ncon="一致", tcon="一致"):
    return json.dumps({
        "decision": decision, "reason": "r",
        "evidence_a": list(ea), "evidence_b": list(eb),
        "numeric_check": {"conclusion": ncon, "numbers_a": list(na),
                          "numbers_b": list(nb)},
        "time_check": {"conclusion": tcon, "times_a": list(ta),
                       "times_b": list(tb)},
    }, ensure_ascii=False)


class _MockLLM:
    def __init__(self, responses):
        self.responses = responses
        self.seen_system = []

    def __call__(self, model, system, user, timeout_s=None):
        self.seen_system.append(system)
        a = user.split("【文本A】\n", 1)[1].split("\n\n【文本B】\n", 1)[0]
        b = user.split("\n\n【文本B】\n", 1)[1]
        return self.responses[(a, b)], 0.01


def _order_ctx(pair_id, h, c, order):
    return {
        "pair_id": pair_id, "order": order,
        "item_a_id": "item-A", "item_b_id": "item-C",
        "record_a_id": "a" * 64, "record_b_id": "c" * 64,
        "text_a": h, "text_b": c,
    }


def test_v5_end_to_end_system_prompt_and_proof_versions(monkeypatch):
    """v5 真件链：LLM 收到的 system=v5 正文；合同证明 prompt_sha256=
    PROMPT_SHA256_V5、policy_version=policy_v3（治理口径如实记录）。"""
    monkeypatch.setenv("DEDUP_JUDGE_PROOF", "1")
    resp = {
        (DUP_H, DUP_C): _jjson("重复", ("100万元",),
                               ("100万元",),
                               na=("100万",), nb=("100万",),
                               ta=("9月24日",), tb=("9月24日",)),
        (DUP_C, DUP_H): _jjson("重复", ("100万元",),
                               ("100万元",),
                               na=("100万",), nb=("100万",),
                               ta=("9月24日",), tb=("9月24日",)),
    }
    mock = _MockLLM(resp)
    judge = lr.SyncResidualJudge(
        lr.ResidualJudgeConfig(prompt_version="judge_v5"), call_fn=mock)
    cb = judge_adapter.build_judge_callable(judge=judge)
    ctx = _order_ctx("p-v5", DUP_H, DUP_C, "ab")
    proof = cb(ctx)
    assert mock.seen_system == [v5.JUDGE_PROMPT_V5]
    assert proof["prompt_sha256"] == v5.PROMPT_SHA256_V5
    assert proof["policy_version"] == "policy_v3"
    validated = judge_pair.validate_proof(proof, ctx)
    assert validated.verdict == "duplicate"


def test_v5_invalid_json_still_invalid(monkeypatch):
    """§5.2 文件 G-4：真正 JSON 缺字段/verdict 非法仍 invalid（证据绑定
    失败才只进诊断——结构非法不放行）。"""
    monkeypatch.setenv("DEDUP_JUDGE_PROOF", "1")
    mock = _MockLLM({(DUP_H, DUP_C): "not-json{{{",
                     (DUP_C, DUP_H): json.dumps({"decision": "四次元"},
                                                ensure_ascii=False)})
    judge = lr.SyncResidualJudge(
        lr.ResidualJudgeConfig(prompt_version="judge_v5"), call_fn=mock)
    cb = judge_adapter.build_judge_callable(judge=judge)
    ctx = _order_ctx("p-v5-bad", DUP_H, DUP_C, "ab")
    assert cb(ctx)["verdict"] == "invalid"
    ctx_ba = _order_ctx("p-v5-bad", DUP_H, DUP_C, "ba")
    ctx_ba["text_a"], ctx_ba["text_b"] = DUP_C, DUP_H
    assert cb(ctx_ba)["verdict"] == "invalid"
