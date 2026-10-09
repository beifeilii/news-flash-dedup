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

终审复审补丁（2026-10-10，P1-1 未核销小补丁）：本配置对象为**唯一权威**
——decision_mode 非空时构造即校验固定映射（legacy_proof_gate→
judge_v1+policy_v2、semantic_authority→judge_v5+policy_v3），任一维不
匹配即 ValueError fail-closed，绝不静默选边（修"判官实际 v1、证明记成
v5/policy_v3"的架空形态）。

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

# 终审复审补丁：模式 → (prompt, policy) 固定映射（决策模式带版本语义，
# 构造即校验唯一权威口径）。v2/v3 提示词按 prompt 直解路径使用（decision_
# mode=空串，不受模式固定映射约束——回放/考试通道自供版本身份）。
_MODE_TO_VERSION = {
    _judge_pair.MODE_LEGACY_PROOF_GATE:
        (_lr.JUDGE_PROMPT_VERSION, _judge_proof.PROOF_POLICY_VERSION),
    _judge_pair.MODE_SEMANTIC_AUTHORITY:
        (_lr.JUDGE_PROMPT_VERSION_V5, _pv.POLICY_VERSION_V3),
}


@dataclass(frozen=True)
class JudgeVersionConfig:
    """统一版本配置对象（条 1）：prompt_version/prompt_sha256/
    policy_version/decision_mode 四维同源。decision_mode 为分发来源
    （按 prompt 直解时=空串——生产入口须经 allow_prompt_direct 显式放
    行，见 decide/service.py）。

    终审复审补丁（唯一权威）：decision_mode 非空时构造即按
    _MODE_TO_VERSION 固定映射校验 prompt/policy——legacy_proof_gate→
    judge_v1+policy_v2、semantic_authority→judge_v5+policy_v3；任一维
    不匹配直接 ValueError（fail-closed，绝不静默选边/记错版本）。

    终审复审第二轮（全量校验）：**无论 decision_mode 是否为空**均校验
    三维——①prompt_version 已注册（llm_residual 注册表 fail-closed，
    未知即 ValueError）；②prompt_sha256 == 注册表中该版本的真实哈希
    （全零/错哈希即拒——不得拿占位哈希冒充合法配置）；③policy_version
    == 该 prompt 的固定策略（judge_v1/v2/v3→policy_v2、judge_v5→
    policy_v3）。decision_mode="" 只豁免"模式↔提示词映射"校验，不豁免
    哈希与策略校验。
    """
    prompt_version: str
    prompt_sha256: str
    policy_version: str
    decision_mode: str = ""

    def __post_init__(self) -> None:
        # ①② 全量校验（空模式不豁免）：注册表单源取真实哈希
        _text, want_sha = _lr.judge_prompt_for_version(self.prompt_version)
        if self.prompt_sha256 != want_sha:
            raise ValueError(
                f"JudgeVersionConfig.prompt_sha256 与注册表真实哈希不符："
                f"prompt_version={self.prompt_version!r} 要求 "
                f"{want_sha}，实得 {self.prompt_sha256!r}（全零/错哈希"
                f"即拒——占位哈希不得冒充合法配置）")
        # ③ 全量校验（空模式不豁免）：prompt→固定策略
        want_policy = _PROMPT_TO_POLICY.get(self.prompt_version)
        if want_policy is None:
            raise ValueError(
                f"prompt_version={self.prompt_version!r} 未登记 policy 映射"
                f"（条 1 显式登记制；合法：{sorted(_PROMPT_TO_POLICY)}）")
        if self.policy_version != want_policy:
            raise ValueError(
                f"JudgeVersionConfig.policy_version 与固定策略不符："
                f"prompt_version={self.prompt_version!r} 要求 "
                f"{want_policy!r}，实得 {self.policy_version!r}（唯一权威，"
                f"冲突即错）")
        # 模式↔提示词映射：仅 decision_mode 非空时校验（空串=按 prompt
        # 直解路径，回放/考试通道自供版本身份，模式语义由入口显式确定）
        if self.decision_mode == "":
            return
        expected = _MODE_TO_VERSION.get(self.decision_mode)
        if expected is None:
            raise ValueError(
                f"decision_mode={self.decision_mode!r} 非法：只允许 "
                f"{'|'.join(sorted(_MODE_TO_VERSION))}")
        want_prompt, want_policy_mode = expected
        if (self.prompt_version != want_prompt
                or self.policy_version != want_policy_mode):
            raise ValueError(
                f"JudgeVersionConfig 与固定映射冲突：decision_mode="
                f"{self.decision_mode!r} 要求 prompt_version={want_prompt!r}"
                f"+policy_version={want_policy_mode!r}，实得 prompt_version="
                f"{self.prompt_version!r}+policy_version="
                f"{self.policy_version!r}（唯一权威，冲突即错）")


def judge_version_for_prompt(prompt_version: str,
                             *, decision_mode: str = "") -> JudgeVersionConfig:
    """按 prompt 版本直解（显式注入 judge 的合同证明路径；未知
    prompt_version 由注册处 fail-closed，未登记 policy 映射本层
    fail-closed；decision_mode 非空时由构造器按固定映射校验）。"""
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
