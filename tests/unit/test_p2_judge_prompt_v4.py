"""P2 先行件③单测（2026-10-09）：decide/judge_prompt_v4 判官 prompt v4。

正典/出处：《判定宪章-草案-v2-1009.md》policy_v2 全文（§〇/§一/§二/§三
条款 + §七 T-1/T-3 默认处置）+ 终裁令 §二 正典栈。钉：prompt 含宪章各
条款锚点 + policy_version="policy_v2" 字面量 + 输出契约示例可解析 +
v3 注册处零 patch（可切回）。
"""

from __future__ import annotations

import json

import pytest

from news_flash_dedup.decide import judge_prompt_v4 as v4
from news_flash_dedup.decide import llm_residual


# ---------- 宪章条款锚点（§〇/§一/§二/§三 + §七 默认 + 双序 + 契约） ----------

CHARTER_ANCHORS = (
    # §〇 总纲
    "policy_v2", "主体｜核心数值｜事件", "误删是最高风险", "只有确认重复才判重复",
    "判定唯一输入=快讯正文", "C01/C02", "重复 / 不重复 / 边界case",
    "优先保障重复类别精确率",
    # §一 判重复（须同时成立）
    "核心主体一致（C07）", "对应核心数值完全一致（C05）", "缺失≠差异",
    "时间/阶段一致（C06）", "时间缺失例外", "核心事件一致（C08）",
    "长文多原子事件（C09）", "无损展示格式",
    # §二 判不重复（满足其一）
    "核心主体完全不一致（C07）", "任一对应核心数值存在差异（C05）",
    "开盘/收盘", "同比/环比", "当日/次日", "初值/终值", "当年/次年",
    "关键对象不同", "方向冲突", "未对齐的独立原子事件（C09）",
    "不重复并保留整条", "T-1",
    # §三 边界（不得自动签）
    "主体缺失（C07）", "相对时间无锚点（C14）", "即使两条正文完全相同",
    "不得用系统接收时间/发布时间/另一条正文替其补日期", "T-3",
    "无事实载体文本", "（产联社）", "置信不足、双向判不一致、机检未通过、证据不足",
    # §四/§六 纪律
    "decision≠action", "KEEP/SUPPRESS",
    # 双序一致
    "双序一致", "交换顺序后结论必须相同",
    # 输出契约
    '"verdict"', '"policy_version"', '"quotes"', '"start"', '"end"',
    '"three_directions"', '"falsification"', '"self_check"', '"reason"',
    "offset", "码点", "自检",
)


@pytest.mark.parametrize("anchor", CHARTER_ANCHORS)
def test_prompt_contains_charter_anchor(anchor: str):
    assert anchor in v4.JUDGE_PROMPT_V4, f"prompt 缺宪章锚点：{anchor}"


def test_prompt_carries_policy_version_literal():
    """policy_version="policy_v2" 字面量在文本内，且与单源常量一致。"""
    assert 'policy_version=policy_v2' in v4.JUDGE_PROMPT_V4
    assert '"policy_version": "policy_v2"' in v4.JUDGE_PROMPT_V4
    assert v4.POLICY_VERSION == "policy_v2"


def test_prompt_sha256_deterministic():
    import hashlib
    assert v4.PROMPT_SHA256_V4 == hashlib.sha256(
        v4.JUDGE_PROMPT_V4.encode("utf-8")).hexdigest()


# ---------- 输出契约示例可解析 ----------

def test_output_contract_example_parseable():
    payload = json.loads(json.dumps(v4.JUDGE_V4_OUTPUT_EXAMPLE, ensure_ascii=False))
    normalized, error = v4.validate_judge_v4_output(payload)
    assert error is None
    assert normalized is not None
    assert normalized["verdict"] == "不重复"
    assert normalized["policy_version"] == "policy_v2"
    assert normalized["falsification"]["element"] == "subject"


def test_embedded_example_block_in_prompt_is_parseable_json():
    """嵌入 prompt 正文的示例块本身就是合法 JSON（照排契约防漂移）。"""
    marker = '{\n  "falsification"'
    start = v4.JUDGE_PROMPT_V4.index(marker)
    block = v4.JUDGE_PROMPT_V4[start:]
    end = block.index("\n}\n") + 3
    payload = json.loads(block[:end])
    normalized, error = v4.validate_judge_v4_output(payload)
    assert error is None and normalized is not None


def _valid_payload() -> dict:
    return json.loads(json.dumps(v4.JUDGE_V4_OUTPUT_EXAMPLE, ensure_ascii=False))


# ---------- 契约校验（闭合枚举/falsification/offset/自检） ----------

def test_validator_rejects_bad_verdict():
    payload = _valid_payload()
    payload["verdict"] = "存疑"
    _, error = v4.validate_judge_v4_output(payload)
    assert error and "verdict" in error


def test_validator_rejects_missing_falsification_for_not_duplicate():
    payload = _valid_payload()
    payload["falsification"] = None
    _, error = v4.validate_judge_v4_output(payload)
    assert error and "falsification" in error


def test_validator_accepts_null_falsification_for_duplicate():
    payload = _valid_payload()
    payload["verdict"] = "重复"
    payload["falsification"] = None
    normalized, error = v4.validate_judge_v4_output(payload)
    assert error is None and normalized["verdict"] == "重复"


def test_validator_rejects_falsification_for_boundary():
    payload = _valid_payload()
    payload["verdict"] = "边界case"
    _, error = v4.validate_judge_v4_output(payload)
    assert error and "falsification" in error


def test_validator_rejects_bad_offset():
    payload = _valid_payload()
    payload["quotes"][0]["start"] = payload["quotes"][0]["end"]
    _, error = v4.validate_judge_v4_output(payload)
    assert error and "offset" in error


def test_validator_rejects_missing_side_coverage():
    payload = _valid_payload()
    payload["quotes"] = [q for q in payload["quotes"] if q["side"] == "A"]
    _, error = v4.validate_judge_v4_output(payload)
    assert error and "quotes" in error


def test_validator_rejects_failed_self_check():
    payload = _valid_payload()
    payload["self_check"]["quotes_verbatim"] = False
    _, error = v4.validate_judge_v4_output(payload)
    assert error and "self_check" in error


def test_validator_rejects_wrong_policy_version():
    payload = _valid_payload()
    payload["policy_version"] = "policy_v1"
    _, error = v4.validate_judge_v4_output(payload)
    assert error and "policy_version" in error


# ---------- v3 零 patch（可切回守卫） ----------

def test_v3_registry_untouched_v4_not_registered():
    """禁区守卫：llm_residual 注册处仍三态（v1/v2/v3 原样），v4 未接线——
    本批不 patch v3，接入属主线合流接线点。"""
    v3_text, v3_sha = llm_residual.judge_prompt_for_version("judge_v3")
    assert v3_sha == llm_residual.PROMPT_SHA256_V3
    assert v3_text == llm_residual.JUDGE_PROMPT_V3
    with pytest.raises(ValueError):
        llm_residual.judge_prompt_for_version(v4.JUDGE_PROMPT_VERSION_V4)
