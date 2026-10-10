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
  semantic_authority→judge_v6+policy_v4+语义权威合并（一期 v6-lite 起
  semantic 模式映射 v6；此前为 judge_v5+policy_v3）。

终审复审补丁（2026-10-10，P1-1 未核销小补丁）：本配置对象为**唯一权威**
——decision_mode 非空时构造即校验固定映射（legacy_proof_gate→
judge_v1+policy_v2、semantic_authority→judge_v6+policy_v4），任一维不
匹配即 ValueError fail-closed，绝不静默选边（修"判官实际 v1、证明记成
v5/policy_v3"的架空形态）。

prompt→policy 映射（条 1）：judge_v1/v2/v3→policy_v2（各自原
policy=judge_proof.PROOF_POLICY_VERSION 证明门控宪章版）；judge_v5→
policy_v3；judge_v6→policy_v4（一期 v6-lite，2026-10-11 分支
p3-v6-phase1 登记）。新 prompt 版本注册后必须在本表显式登记——未登记
fail-closed（绝不静默落错 policy）。

一期 v6-lite（2026-10-11，分支 p3-v6-phase1）：
- _MODE_TO_VERSION/_MODE_TO_PROMPT 的 semantic_authority 映射更新为
  judge_v6+policy_v4（judge_prompt_v6.py 新建：v5 全量继承+R8 修订
  改值/R7 受约束回填两条条款修订）；默认模式仍 legacy_proof_gate
  （judge_v1+policy_v2）——生产行为零变化，v6 只经 semantic 开关生效；
- judge_v5 保留注册供回放对比：prompt 直解通道（judge_version_for_
  prompt("judge_v5")，decision_mode=空串）仍可达 policy_v3。

主窗补充令二（2026-10-11，第二 AI 战略复审，老板批准收入一期范围）：
- R7 回填规则**独立开关**（不依赖整个 v6 开关）：DEDUP_JUDGE_BACKFILL
  环境变量（缺席/空串=开【一期原令行为】；0/false/off/no=关；
  1/true/on/yes=开；其他非空值 ValueError fail-closed——同
  DEDUP_JUDGE_DECISION_MODE 整改令二口径）。开关态随
  JudgeVersionConfig.backfill_enabled 字段单源携带（可审计）并经
  run_manifest.KNOWN_SWITCHES 快照入册。关闭时 R7 回填永不签发
  （decide/service.py 判官循环执行撤签：硬闸触发对判官判"重复"→
  强制存疑转边界），R8 修订规则不受影响照常工作——shadow/灰度期发现
  回填误判时不动代码、不重部署，单点关闭该规则。

终审 P0/P1 修复包（2026-10-11，第二 AI 终审，老板批准，最小范围不
扩一期）：
- **开关默认改关（修复包令 4）**：DEDUP_JUDGE_BACKFILL 缺席/空串=
  OFF（一期原令"缺席=默认开"废止）——在确定性签发闸完工+金标重证
  之前默认关闭，显式置 1/true/on/yes 才开；JudgeVersionConfig.
  backfill_enabled 及各构造函数缺省值同步改 False（缺省维度全局一致
  ——缺席任何一层都不得默认签发）。
"""

from __future__ import annotations

import os
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
    _lr.JUDGE_PROMPT_VERSION_V6: _pv.POLICY_VERSION_V4,              # v6→v4
}

# 模式 → prompt（条 3 分发表；模式字面量单源=judge_pair）。一期 v6-lite
# （2026-10-11）：semantic_authority→judge_v6（v5 保留注册供回放对比，
# prompt 直解通道可达）。
_MODE_TO_PROMPT = {
    _judge_pair.MODE_LEGACY_PROOF_GATE: _lr.JUDGE_PROMPT_VERSION,
    _judge_pair.MODE_SEMANTIC_AUTHORITY: _lr.JUDGE_PROMPT_VERSION_V6,
}

# 终审复审补丁：模式 → (prompt, policy) 固定映射（决策模式带版本语义，
# 构造即校验唯一权威口径）。v2/v3 提示词按 prompt 直解路径使用（decision_
# mode=空串，不受模式固定映射约束——回放/考试通道自供版本身份）。一期
# v6-lite（2026-10-11）：semantic_authority→judge_v6+policy_v4（v5+policy_v3
# 不再经模式映射，仅 prompt 直解回放）。
_MODE_TO_VERSION = {
    _judge_pair.MODE_LEGACY_PROOF_GATE:
        (_lr.JUDGE_PROMPT_VERSION, _judge_proof.PROOF_POLICY_VERSION),
    _judge_pair.MODE_SEMANTIC_AUTHORITY:
        (_lr.JUDGE_PROMPT_VERSION_V6, _pv.POLICY_VERSION_V4),
}


# 主窗补充令二（2026-10-11）：R7 回填独立开关（policy_v4 层，不依赖
# 整个 v6/semantic 开关）。env 名登记入 run_manifest.KNOWN_SWITCHES
# 快照入册（manifest 可溯）。终审修复包令 4（2026-10-11）：缺席=
# 默认关（确定性签发闸完工+金标重证前不默认签发）。
JUDGE_BACKFILL_ENV = "DEDUP_JUDGE_BACKFILL"
_BACKFILL_OFF_VALUES = ("0", "false", "off", "no")
_BACKFILL_ON_VALUES = ("1", "true", "on", "yes")


def backfill_enabled_from_env(environ=None) -> bool:
    """读 DEDUP_JUDGE_BACKFILL（R7 回填独立开关）：

    - 缺席/空串 → False（**默认关**——终审修复包令 4：确定性签发闸
      完工+金标重证前不得默认签发；一期原令"缺席=默认开"废止）；
    - 0/false/off/no（大小写不敏感）→ False（关：回填永不签发）；
    - 1/true/on/yes → True（显式开）；
    - 其他非空值 → ValueError（fail-closed 明确报错，不得静默选边
      ——同 DEDUP_JUDGE_DECISION_MODE 整改令二口径）。
    """
    raw = ((os.environ if environ is None else environ)
           .get(JUDGE_BACKFILL_ENV) or "").strip().lower()
    if not raw:
        return False
    if raw in _BACKFILL_OFF_VALUES:
        return False
    if raw in _BACKFILL_ON_VALUES:
        return True
    raise ValueError(
        f"{JUDGE_BACKFILL_ENV}={raw!r} 非法：只允许 "
        f"{'|'.join(_BACKFILL_OFF_VALUES)}（关）或 "
        f"{'|'.join(_BACKFILL_ON_VALUES)}（开）；缺席=默认关"
        f"（终审修复包令 4）")


@dataclass(frozen=True)
class JudgeVersionConfig:
    """统一版本配置对象（条 1）：prompt_version/prompt_sha256/
    policy_version/decision_mode 四维同源。decision_mode 为分发来源
    （按 prompt 直解时=空串——生产入口须经 allow_prompt_direct 显式放
    行，见 decide/service.py）。

    终审复审补丁（唯一权威）：decision_mode 非空时构造即按
    _MODE_TO_VERSION 固定映射校验 prompt/policy——legacy_proof_gate→
    judge_v1+policy_v2、semantic_authority→judge_v6+policy_v4（一期
    v6-lite 起映射 v6）；任一维不匹配直接 ValueError（fail-closed，
    绝不静默选边/记错版本）。

    终审复审第二轮（全量校验）：**无论 decision_mode 是否为空**均校验
    三维——①prompt_version 已注册（llm_residual 注册表 fail-closed，
    未知即 ValueError）；②prompt_sha256 == 注册表中该版本的真实哈希
    （全零/错哈希即拒——不得拿占位哈希冒充合法配置）；③policy_version
    == 该 prompt 的固定策略（judge_v1/v2/v3→policy_v2、judge_v5→
    policy_v3、judge_v6→policy_v4）。decision_mode="" 只豁免"模式↔
    提示词映射"校验，不豁免哈希与策略校验。

    主窗补充令二：backfill_enabled（R7 回填独立开关）——policy_v4
    层开关随本配置对象单源携带（不参与模式固定映射校验：模式↔版本
    四维一致性不受开关态影响；开关只控制 service 判官循环的 R7 签发
    权）。非 bool 值构造即 ValueError（fail-closed）。终审修复包令 4
    （2026-10-11）：缺省值改 **False（默认关）**——确定性签发闸完工+
    金标重证前缺席任何一层都不得默认签发。
    """
    prompt_version: str
    prompt_sha256: str
    policy_version: str
    decision_mode: str = ""
    backfill_enabled: bool = False

    def __post_init__(self) -> None:
        # 补充令二：开关字段类型闸（fail-closed，1/0 int 不得冒充 bool）
        if not isinstance(self.backfill_enabled, bool):
            raise ValueError(
                f"JudgeVersionConfig.backfill_enabled 必须为 bool：实得 "
                f"{type(self.backfill_enabled).__name__}（"
                f"{self.backfill_enabled!r}——R7 回填独立开关，"
                f"env {JUDGE_BACKFILL_ENV} 解析见 backfill_enabled_from_env）")
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
                             *, decision_mode: str = "",
                             backfill_enabled: bool = False
                             ) -> JudgeVersionConfig:
    """按 prompt 版本直解（显式注入 judge 的合同证明路径；未知
    prompt_version 由注册处 fail-closed，未登记 policy 映射本层
    fail-closed；decision_mode 非空时由构造器按固定映射校验；
    backfill_enabled=R7 回填独立开关随配置携带【补充令二；终审修复包
    令 4：缺省=默认关】）。"""
    _text, prompt_sha = _lr.judge_prompt_for_version(prompt_version)
    policy = _PROMPT_TO_POLICY.get(prompt_version)
    if policy is None:
        raise ValueError(
            f"prompt_version={prompt_version!r} 未登记 policy 映射（条 1 "
            f"显式登记制；合法：{sorted(_PROMPT_TO_POLICY)}）")
    return JudgeVersionConfig(
        prompt_version=prompt_version, prompt_sha256=prompt_sha,
        policy_version=policy, decision_mode=decision_mode,
        backfill_enabled=backfill_enabled)


def judge_version_for_mode(decision_mode: str, *,
                           backfill_enabled: bool = False
                           ) -> JudgeVersionConfig:
    """按判定模式分发（条 3：legacy→v1+policy_v2，semantic→v6+policy_v4
    ——一期 v6-lite 起 semantic 映射 v6；非法模式 fail-closed；
    backfill_enabled=R7 回填独立开关【补充令二；终审修复包令 4：
    缺省=默认关——不参与模式固定映射校验，仅控制 service 判官循环
    的 R7 签发权】）。"""
    prompt_version = _MODE_TO_PROMPT.get(decision_mode)
    if prompt_version is None:
        raise ValueError(
            f"decision_mode={decision_mode!r} 非法：只允许 "
            f"{'|'.join(sorted(_MODE_TO_PROMPT))}")
    return judge_version_for_prompt(prompt_version,
                                    decision_mode=decision_mode,
                                    backfill_enabled=backfill_enabled)


def default_judge_version(environ=None) -> JudgeVersionConfig:
    """缺省装配（adapter 默认配置/run_manifest 缺省登记的共同单源）：
    读 DEDUP_JUDGE_DECISION_MODE（缺席/空串=legacy_proof_gate 默认；
    非法值=judge_pair 明确报错）后按模式分发；读 DEDUP_JUDGE_BACKFILL
    （缺席/空串=默认关——终审修复包令 4：签发闸完工+金标重证前
    不默认签发；非法值 fail-closed）定 R7 回填独立开关（补充令二
    ——开关态随 config 单源携带可审计）。"""
    return judge_version_for_mode(
        _judge_pair.judge_decision_mode(environ),
        backfill_enabled=backfill_enabled_from_env(environ))


__all__ = [
    "JUDGE_BACKFILL_ENV",
    "JudgeVersionConfig",
    "backfill_enabled_from_env",
    "judge_version_for_prompt",
    "judge_version_for_mode",
    "default_judge_version",
]
