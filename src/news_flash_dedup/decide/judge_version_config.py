"""判官版本装配单源（2026-10-10，提交一修复并入提交二复审条 1/2/3）。

出处：提交二独立复审五条（老板批准转发，主窗令）——
- 条 1（版本装配单源化）：judge_adapter、cache key、run_manifest 全部从
  本模块的统一版本配置对象读取 prompt_version/prompt_sha256/
  policy_version/decision_mode——**禁止各自取默认值**（修复前错配实
  证：judge_adapter 无条件 judge_v5+policy_v3、run_manifest 缺省登记
  judge_v1+policy_v3="实跑 v5 却登记 v1"）。
- 条 3（judge_v5 不得无条件默认）：版本分发由
  DEDUP_JUDGE_DECISION_MODE 决定——legacy_proof_gate（默认）→
  judge_v1+policy_v2+旧证明门控合并（合并语义=judge_pair legacy 口径）；
  semantic_authority→judge_v5+policy_v3+语义权威合并。

prompt→policy 映射（条 1）：judge_v1/v2/v3→policy_v2（各自原
policy=judge_proof.PROOF_POLICY_VERSION 证明门控宪章版）；judge_v5→
policy_v3。新 prompt 版本注册后必须在本表显式登记——未登记 fail-closed
（绝不静默落错 policy）。
"""

from __future__ import annotations

from dataclasses import dataclass

from news_flash_dedup.decide import judge_pair as _judge_pair
from news_flash_dedup.decide import judge_proof as _judge_proof
from news_flash_dedup.decide import llm_residual as _lr
from news_flash_dedup.decide import policy_version as _pv

# prompt → policy（条 1 映射表，显式登记制）
_PROMPT_TO_POLICY = {
    _lr.JUDGE_PROMPT_VERSION: _judge_proof.PROOF_POLICY_VERSION,     # v1→v2
    _lr.JUDGE_PROMPT_VERSION_V2: _judge_proof.PROOF_POLICY_VERSION,  # v2→v2
    _lr.JUDGE_PROMPT_VERSION_V3: _judge_proof.PROOF_POLICY_VERSION,  # v3→v2
    _lr.JUDGE_PROMPT_VERSION_V5: _pv.POLICY_VERSION_V3,              # v5→v3
}

# 模式 → prompt（条 3 分发表；模式字面量单源=judge_pair）
_MODE_TO_PROMPT = {
    _judge_pair.MODE_LEGACY_PROOF_GATE: _lr.JUDGE_PROMPT_VERSION,
    _judge_pair.MODE_SEMANTIC_AUTHORITY: _lr.JUDGE_PROMPT_VERSION_V5,
}


@dataclass(frozen=True)
class JudgeVersionConfig:
    """统一版本配置对象（条 1）：prompt_version/prompt_sha256/
    policy_version/decision_mode 四维同源。decision_mode 为分发来源
    （按 prompt 直解时=空串）。"""
    prompt_version: str
    prompt_sha256: str
    policy_version: str
    decision_mode: str = ""


def judge_version_for_prompt(prompt_version: str,
                             *, decision_mode: str = "") -> JudgeVersionConfig:
    """按 prompt 版本直解（显式注入 judge 的合同证明路径；未知
    prompt_version 由注册处 fail-closed，未登记 policy 映射本层
    fail-closed）。"""
    _text, prompt_sha = _lr.judge_prompt_for_version(prompt_version)
    policy = _PROMPT_TO_POLICY.get(prompt_version)
    if policy is None:
        raise ValueError(
            f"prompt_version={prompt_version!r} 未登记 policy 映射（条 1 "
            f"显式登记制；合法：{sorted(_PROMPT_TO_POLICY)}）")
    return JudgeVersionConfig(
        prompt_version=prompt_version, prompt_sha256=prompt_sha,
        policy_version=policy, decision_mode=decision_mode)


def judge_version_for_mode(decision_mode: str) -> JudgeVersionConfig:
    """按判定模式分发（条 3：legacy→v1+policy_v2，semantic→v5+policy_v3；
    非法模式 fail-closed）。"""
    prompt_version = _MODE_TO_PROMPT.get(decision_mode)
    if prompt_version is None:
        raise ValueError(
            f"decision_mode={decision_mode!r} 非法：只允许 "
            f"{'|'.join(sorted(_MODE_TO_PROMPT))}")
    return judge_version_for_prompt(prompt_version,
                                    decision_mode=decision_mode)


def default_judge_version(environ=None) -> JudgeVersionConfig:
    """缺省装配（adapter 默认配置/run_manifest 缺省登记的共同单源）：
    读 DEDUP_JUDGE_DECISION_MODE（缺席/空串=legacy_proof_gate 默认；
    非法值=judge_pair 明确报错）后按模式分发。"""
    return judge_version_for_mode(_judge_pair.judge_decision_mode(environ))


__all__ = [
    "JudgeVersionConfig",
    "judge_version_for_prompt",
    "judge_version_for_mode",
    "default_judge_version",
]
