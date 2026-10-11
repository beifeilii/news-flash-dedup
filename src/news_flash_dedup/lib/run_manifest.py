"""跑批不可变版本清单（Run Manifest）——2026-10-09 P2 先行件② 新建。

正典/出处：
- 终裁令《判定链改造-终裁令-正典-1009.md》§二 正典栈（版本化纪律）与
  宪章《判定宪章-草案-v2-1009.md》§六-2（修改版本化 policy_version、
  历史考卷保留旧标签旧版本、不得无痕重标）——每次跑批须封存一份不可变
  版本清单，使任一签发/判定结果可回溯到"哪一版代码+哪一版词典+哪一版
  提示词+哪一版宪章+哪个模型+哪个向量空间+哪些开关+哪些输入"。
- 测试口径：终裁令 §五之补（跑批限定 tdata0909 冻结件）——输入清单
  sha256 使"同卷实测"可证。

性质：纯新增模块，不修改任何既有文件行为。全部字段显式可注入（hermetic
单测），缺省值取自现役常量单源（facts/rule.py、decide/llm_residual.py、
recall/vector_space.py 的 EmbeddingSpace.space_id 工艺）——只读引用，
不回写。

六元组+两锚：
  pipeline_version / dict_version / prompt_sha256 / policy_version /
  model / embedding_space ＋ switch_state 快照 ＋ code git sha ＋
  输入清单 sha256。

不可变性：RunManifest 为 frozen dataclass；to_json() 确定性序列化
（sort_keys+紧凑分隔符），manifest_sha256 为其内容指纹——同输入同
manifest（单测钉死）。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping

from news_flash_dedup.batch_admission import (
    ADMISSION_CONFIG_VERSION, DURABLE_QUEUE_CAPACITY,
    DURABLE_QUEUE_CRITICAL_DEPTH, DURABLE_QUEUE_WARNING_DEPTH,
)
from news_flash_dedup.decide import judge_version_config as _jvc
from news_flash_dedup.decide.llm_residual import (
    DEFAULT_MODEL,
    JUDGE_PROMPT_VERSION,
)
from news_flash_dedup.decide.policy_version import (
    DEFAULT_POLICY_VERSION,
    POLICY_VERSION_V2,
    POLICY_VERSION_V3,
)
from news_flash_dedup.facts.rule import RULE_DICT_VERSION
from news_flash_dedup.recall.fact_supply import fact_supply_mode_from_env
from news_flash_dedup.work_queue import WORK_LEASE_DEFAULT_SECONDS

# 终审 P1-manifest（2026-10-11 修复包）：R7 回填开关实际生效态入正式
# 结构化字段 backfill_enabled（字段集变更）→schema 升 v2。
# 用户令 2026-10-11（LLM 事实供给接进服务主链）：供给选择实际生效态入
# 正式结构化字段 fact_supply（env 单源 DEDUP_FACT_SUPPLY，缺席=rule；
# 字段集变更）→schema 升 v3。
# P1a-T4（2026-10-11，卡3.5）：durable 队列实态五字段——durable_queue
# （env 单源 DEDUP_DURABLE_QUEUE，缺席=0=双写窗 legacy 侧）+容量三档
# （4096/3277/3891，batch_admission 单源常量）+work_lease_seconds
# （work_queue 单源缺省租约）+admission_config_version（头文档配置纪元
# 单源）→schema 升 v4。
MANIFEST_SCHEMA_VERSION = "run_manifest_v4"

# 提交二（2026-10-10，p3-semantic-authority，方案 §5.2 文件 E/H）：policy
# 版本字面量单源迁至 decide/policy_version.py（无循环依赖；本模块与
# judge_prompt_v5 同源导入，提示词模块不再反向导入本模块）。此处仅做
# 兼容再导出——POLICY_VERSION_V2 仍为 "policy_v2"（judge_prompt_v4 与
# judge_proof 组件锚定）；DEFAULT_POLICY_VERSION 自本提交起为
# POLICY_VERSION_V3="policy_v3"（judge_v5 配套治理口径，manifest 如实
# 记录真实使用的 policy version）。

DEFAULT_PIPELINE_VERSION = "dedup_v1"

CODE_GIT_SHA_ENV = "DEDUP_CODE_GIT_SHA"
_GIT_SHA_RE = re.compile(r"[0-9a-f]{40}\Z")

# 开关快照全集（固定有序=确定性；只列判定相关 DEDUP_* 开关，键缺席记
# 空串=未设置）。新增开关须显式登记入本表——不全env扫描（防非确定性
# 与无关环境泄漏入审计）。
KNOWN_SWITCHES: tuple[str, ...] = (
    "DEDUP_CERT_DECOUPLE",              # text/__init__.py（证书解耦，默认关）
    "DEDUP_COVERAGE_FRONTIER",          # recall/service.py（覆盖闸 frontier）
    # 2026-10-11（P1c 运行时装配窗，api/assemble.py）：Embedding 写径微批
    # 网关三开关（DEDUP_EMBEDDING_MICRO_BATCH 缺省关=写径逐字节现役；批
    # 大小窗 1..10/窗口 20..50ms 沿 embedding_batch.py 钉值窗）。
    "DEDUP_EMBEDDING_BATCH_MAX_WAIT_MS",  # api/assemble.py（微批窗口，默认 30）
    "DEDUP_EMBEDDING_BATCH_SIZE",       # api/assemble.py（微批批大小，默认 8）
    "DEDUP_EMBEDDING_DAILY_TOKEN_BUDGET",  # vector/embedding_client.py（预算闸）
    "DEDUP_EMBEDDING_MICRO_BATCH",      # api/assemble.py（写径微批开关，默认关）
    "DEDUP_EXACT_MIN_LEN",              # text/__init__.py（最小正文长度闸）
    # 2026-10-11（主窗令·判定优先级修复）：剥信源壳开关补登记（既有开关
    # p15_integration.py DEDUP_EXACT_SHELL_STRIP 默认关——此前漏册，审计
    # 留痕面补齐；规则版本 SHELL_STRIP_RULE_VERSION=v2 由 test_jpf_
    # priority_fix 钉死）。
    "DEDUP_EXACT_SHELL_STRIP",          # compare/p15_integration.py（剥壳，默认关）
    # 用户令 2026-10-11（LLM 事实供给）：供给选择开关（recall/fact_supply.py
    # ——rule|llm，缺席=rule；实际生效态同时记结构化字段 fact_supply）
    "DEDUP_FACT_SUPPLY",                # recall/fact_supply.py（F3 供给选择）
    # 提交二（§5.2 文件 H-2）补登记：判官证明路开关 + 判官进主链开关
    # 主窗补充令二（2026-10-11）补登记：R7 回填独立开关（policy_v4 层，
    # decide/judge_version_config.py——开关态随 JudgeVersionConfig 单源
    # 携带可审计）；终审修复包令 4（2026-10-11）：缺席=默认关（空串
    # 记"未显式设置"；实际生效态见结构化字段 backfill_enabled——
    # 终审 P1-manifest：快照之外再记 config 实态）
    "DEDUP_JUDGE_BACKFILL",             # decide/judge_version_config.py（R7 回填，默认关）
    # 提交三（§5.3）补登记：判官裁决口径灰度开关（默认 legacy_proof_gate）
    "DEDUP_JUDGE_DECISION_MODE",        # decide/judge_pair.py（灰度，默认 legacy）
    "DEDUP_JUDGE_IN_CHAIN",             # decide/judge_pair.py（判官进主链，默认关）
    # 2026-10-11（P1c 运行时装配窗，api/assemble.py）：判官并发执行器装配
    # 三开关（DEDUP_JUDGE_PAIR_EXECUTOR 缺省关=不装配——点头 B 口径"接线
    # 但默认不启用"；对池窗 1..24 缺省 20 沿 judge_pair_executor.py 钉值）。
    "DEDUP_JUDGE_PAIR_CONCURRENCY",     # api/assemble.py（对池大小，默认 20）
    "DEDUP_JUDGE_PAIR_EXECUTOR",        # api/assemble.py（执行器装配开关，默认关）
    "DEDUP_JUDGE_PAIR_TIMEOUT_S",       # api/assemble.py（单对硬超时，缺省 None）
    "DEDUP_JUDGE_PROOF",                # decide/judge_proof.py（证明路，默认关）
    # 最终接线窗（2026-10-12，装配注入设计单 §二-B）：按需 LLM facts 注入
    # 开关（recall/fact_supply.py build_llm_facts_from_env——影子腿/生效腿
    # 预装配位 B 单源；缺省关=decide_for_task(llm_facts=None) 双轨关，
    # 现役语义逐字节零 diff）。
    "DEDUP_LLM_FACTS_ONDEMAND",         # recall/fact_supply.py（按需 facts，默认关）
    # P1a-T4（卡3.5）：durable 队列模式（双写窗受理侧选择；缺席=0=
    # legacy pending 全塞路径；实际生效态同时记结构化字段 durable_queue）
    "DEDUP_DURABLE_QUEUE",              # batch_admission.py（受理模式，双写窗）
    "DEDUP_RECALL_MODE",                # recall/service.py（召回/判重模式闸）
)


def snapshot_switch_state(env: Mapping[str, str] | None = None) -> tuple[tuple[str, str], ...]:
    """已知开关快照：((名称, 值), …)，固定 KNOWN_SWITCHES 序；缺省读 os.environ。"""
    source = os.environ if env is None else env
    return tuple((name, source.get(name, "")) for name in KNOWN_SWITCHES)


# P1a-T4（卡3.5）：durable 队列模式 env 单源（缺席=0=双写窗 legacy 侧；
# 解析口径同 DEDUP_JUDGE_BACKFILL——非空非法值 fail-closed 拒产）。
DURABLE_QUEUE_ENV = "DEDUP_DURABLE_QUEUE"
_DURABLE_OFF_VALUES = ("0", "false", "off", "no")
_DURABLE_ON_VALUES = ("1", "true", "on", "yes")


def durable_queue_enabled_from_env(environ: Mapping[str, str] | None = None) -> bool:
    """读 DEDUP_DURABLE_QUEUE（durable 队列模式实态）：

    - 缺席/空串 → False（双写窗默认 legacy 侧——P1a-T6 全量翻转前
      生产主链仍走 pending 全塞路径）；
    - 0/false/off/no（大小写不敏感）→ False；
    - 1/true/on/yes → True；
    - 其他非空值 → ValueError（fail-closed，同 backfill 解析口径）。
    """
    raw = ((os.environ if environ is None else environ)
           .get(DURABLE_QUEUE_ENV) or "").strip().lower()
    if not raw:
        return False
    if raw in _DURABLE_OFF_VALUES:
        return False
    if raw in _DURABLE_ON_VALUES:
        return True
    raise ValueError(
        f"{DURABLE_QUEUE_ENV}={raw!r} 非法：只允许 "
        f"{'|'.join(_DURABLE_OFF_VALUES)}（legacy）或 "
        f"{'|'.join(_DURABLE_ON_VALUES)}（durable）；缺席=默认 legacy")


def inputs_manifest_sha256(inputs: Iterable[str]) -> str:
    """输入清单 sha256：排序后 '\n' 连接 UTF-8 取摘要（顺序无关、确定性）。

    每个元素为一条输入身份串（record_id / 行键 / 文件 sha 等，由跑批方
    定义粒度）；不做去重（调用方语义），空清单 = 空串摘要（显式可区分
    "无输入"与"未封存"）。
    """
    items = list(inputs)
    for item in items:
        if not isinstance(item, str):
            raise TypeError(f"输入身份串必须为 str：{type(item).__name__}")
    return hashlib.sha256("\n".join(sorted(items)).encode("utf-8")).hexdigest()


def _validate_git_sha(value: str, *, origin: str) -> str:
    if not isinstance(value, str) or not _GIT_SHA_RE.fullmatch(value):
        raise ValueError(
            f"代码 git sha 非法（{origin}）：{value!r}（须 40 位小写 hex；"
            f"fail-closed，审计清单不接纳占位/截断值）")
    return value


def resolve_code_git_sha(
    explicit: str | None = None,
    *,
    env: Mapping[str, str] | None = None,
    repo_root: str | Path | None = None,
) -> str:
    """代码 git sha 三级解析：显式入参 > DEDUP_CODE_GIT_SHA 环境值 >
    `git rev-parse HEAD` 子进程（repo_root 缺省=本包源码树所属仓库根）。

    全部不可得/非法 → ValueError/RuntimeError（fail-closed：版本清单是
    审计锚，宁可拒产不可静默 "unknown"）。
    """
    if explicit is not None:
        return _validate_git_sha(explicit, origin="explicit")
    source = os.environ if env is None else env
    from_env = (source.get(CODE_GIT_SHA_ENV) or "").strip()
    if from_env:
        return _validate_git_sha(from_env, origin=CODE_GIT_SHA_ENV)
    root = Path(repo_root) if repo_root is not None else Path(__file__).resolve().parents[3]
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True,
            text=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"代码 git sha 解析失败（git 不可用）：{exc}") from exc
    if proc.returncode != 0:
        raise RuntimeError(
            f"代码 git sha 解析失败（git rev-parse HEAD rc={proc.returncode}）："
            f"{proc.stderr.strip()[:200]}")
    return _validate_git_sha(proc.stdout.strip(), origin="git rev-parse HEAD")


@dataclass(frozen=True)
class RunManifest:
    """一次跑批的不可变版本清单。字段语义见模块 docstring 六元组+两锚。

    终审 P1-manifest（2026-10-11 修复包）：增结构化字段 backfill_
    enabled（R7 回填开关**实际生效态**，规范化 "1"/"0"——switch_state
    只快照 env 原值，实际生效单源=JudgeVersionConfig.backfill_enabled
    或 env 解析态；缺席=默认关，见 judge_version_config 修复包令 4）。

    用户令 2026-10-11（LLM 事实供给）：增结构化字段 fact_supply（供给
    选择实际生效态 "rule"/"llm"——env 单源 DEDUP_FACT_SUPPLY，缺席=
    rule；无 config 对象故无同传冲突面，与 switch_state 快照双口径可
    对拍）。

    P1a-T4（卡3.5）：增 durable 队列实态五字段（schema v4）——
    durable_queue（env 单源 DEDUP_DURABLE_QUEUE 实态，规范化 "1"/"0"）
    +queue_capacity/queue_warning_depth/queue_critical_depth（batch_
    admission 单源常量——manifest 记录**该代码版**容量三档；运行期
    测试注入的小容量不入生产 manifest 面）+work_lease_seconds（work_
    queue 单源缺省租约）+admission_config_version（头文档配置纪元
    单源——受理配置漂移可回溯）。
    """
    schema_version: str
    pipeline_version: str
    dict_version: str
    prompt_version: str
    prompt_sha256: str
    policy_version: str
    backfill_enabled: str
    fact_supply: str
    model: str
    embedding_space: str
    switch_state: tuple[tuple[str, str], ...]
    code_git_sha: str
    inputs_sha256: str
    input_count: int
    durable_queue: str
    queue_capacity: int
    queue_warning_depth: int
    queue_critical_depth: int
    work_lease_seconds: int
    admission_config_version: int

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "pipeline_version": self.pipeline_version,
            "dict_version": self.dict_version,
            "prompt_version": self.prompt_version,
            "prompt_sha256": self.prompt_sha256,
            "policy_version": self.policy_version,
            "backfill_enabled": self.backfill_enabled,
            "fact_supply": self.fact_supply,
            "model": self.model,
            "embedding_space": self.embedding_space,
            "switch_state": {name: value for name, value in self.switch_state},
            "code_git_sha": self.code_git_sha,
            "inputs_sha256": self.inputs_sha256,
            "input_count": self.input_count,
            "durable_queue": self.durable_queue,
            "queue_capacity": self.queue_capacity,
            "queue_warning_depth": self.queue_warning_depth,
            "queue_critical_depth": self.queue_critical_depth,
            "work_lease_seconds": self.work_lease_seconds,
            "admission_config_version": self.admission_config_version,
        }

    def to_json(self) -> str:
        """确定性 JSON（sort_keys+紧凑分隔符+ensure_ascii=False）——内容指纹载体。"""
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"))

    def manifest_sha256(self) -> str:
        """清单内容指纹（to_json 的 sha256）：封存/对拍锚。"""
        return hashlib.sha256(self.to_json().encode("utf-8")).hexdigest()


def _embedding_space_id(space) -> str:
    """EmbeddingSpace 实例 → space_id；str 原样（调用方自供身份）。"""
    space_id = getattr(space, "space_id", None)
    if isinstance(space_id, str) and space_id:
        return space_id
    if isinstance(space, str) and space:
        return space
    raise TypeError(
        "embedding_space 须为 EmbeddingSpace（携 space_id）或非空 str 身份")


def build_run_manifest(
    *,
    inputs: Iterable[str],
    embedding_space,
    pipeline_version: str = DEFAULT_PIPELINE_VERSION,
    dict_version: str = RULE_DICT_VERSION,
    prompt_version: str | None = None,
    prompt_sha256: str | None = None,
    policy_version: str | None = None,
    model: str = DEFAULT_MODEL,
    code_git_sha: str | None = None,
    env: Mapping[str, str] | None = None,
    repo_root: str | Path | None = None,
    judge_version_config=None,
) -> RunManifest:
    """装配跑批版本清单。

    必填：inputs（输入身份串清单）、embedding_space（EmbeddingSpace 或
    空间 id 串）。其余缺省取现役常量单源；code_git_sha 三级解析见
    resolve_code_git_sha（缺省 git 子进程，测试请显式注入）。env 同时
    供开关快照与 git sha 环境级解析（单测 hermetic）。

    提交一修复并入提交二复审条 1/2（版本装配单源化 + 如实登记生效版
    本）：prompt_version 缺省=按 DEDUP_JUDGE_DECISION_MODE 经
    judge_version_config 分发的**实际生效**版本（legacy_proof_gate 默认
    →judge_v1；semantic_authority→judge_v5），policy_version/prompt_sha256
    缺省随 prompt 映射同解（judge_v1/v2/v3→policy_v2、judge_v5→
    policy_v3）——修掉"实跑 v5 却登记 v1 / 实跑 legacy 却登记 v3"错配；
    显式实参仍优先（回放/考试通道自供版本身份）。

    终审 P1-1（2026-10-10 单源传递）：judge_version_config 在场（
    decide_for_task 入口创建、经 DecideOutcome.judge_version_config 外露
    的同一 JudgeVersionConfig）时，prompt_version/prompt_sha256/
    policy_version 缺省项从其同源读取——显式实参语义权威（env 空时
    manifest 不得各自回落 env 读值）。

    终审复审第二轮（防覆盖）：judge_version_config 在场时，显式
    prompt_version/prompt_sha256/policy_version 实参**只允许不传或与
    config 完全一致**——任何不一致直接 ValueError（修"传 v5 config 却
    用显式字符串登记 v1"的覆盖通道）。回放需要任意组合（提示词/策略/
    哈希不配套）走单独回放接口，不得经生产 manifest 普通参数绕过。

    终审 P1-manifest（2026-10-11 修复包）：R7 回填开关**实际生效态**
    记入正式结构化字段 backfill_enabled（规范化 "1"/"0"）——config
    在场记 config.backfill_enabled（实际生效单源）；config 缺席按 env
    解析（DEDUP_JUDGE_BACKFILL，缺席=默认关）。显式 config 与 env
    DEDUP_JUDGE_BACKFILL **同传且冲突→ValueError fail-closed**（实际
    生效开关态必须单源可溯，不得静默选边；env 显式设置且与 config 一
    致、或 env 未显式设置=无冲突，config 权威）。switch_state 仍只快
    照 env 原值（两口径并存可对拍：env 快照 vs 实际生效态）。
    """
    items = tuple(inputs)
    if judge_version_config is not None:
        # 终审复审第二轮（防覆盖）：显式实参与 config 同传只允许完全
        # 一致（不传=从 config 同源；传且相等=显式确认；传且不等=拒绝）
        explicit = {"prompt_version": prompt_version,
                    "prompt_sha256": prompt_sha256,
                    "policy_version": policy_version}
        config_values = {
            "prompt_version": judge_version_config.prompt_version,
            "prompt_sha256": judge_version_config.prompt_sha256,
            "policy_version": judge_version_config.policy_version,
        }
        for name, value in explicit.items():
            if value is not None and value != config_values[name]:
                raise ValueError(
                    f"{name}={value!r} 与 judge_version_config 同传不一致"
                    f"（防覆盖：config 在场时显式实参只允许不传或与 "
                    f"{config_values[name]!r} 完全一致；回放需任意组合走"
                    f"单独回放接口，不得经生产 manifest 绕过）")
        if prompt_version is None:
            prompt_version = judge_version_config.prompt_version
        if prompt_sha256 is None:
            prompt_sha256 = judge_version_config.prompt_sha256
        if policy_version is None:
            policy_version = judge_version_config.policy_version
    if prompt_version is None:
        prompt_version = _jvc.default_judge_version(
            env).prompt_version
    resolved = _jvc.judge_version_for_prompt(prompt_version)
    if prompt_sha256 is None:
        prompt_sha256 = resolved.prompt_sha256
    if policy_version is None:
        policy_version = resolved.policy_version
    if not isinstance(prompt_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", prompt_sha256):
        raise ValueError(f"prompt_sha256 非法：{prompt_sha256!r}（须 64 位小写 hex）")
    for name, value in (("pipeline_version", pipeline_version),
                        ("dict_version", dict_version),
                        ("prompt_version", prompt_version),
                        ("policy_version", policy_version),
                        ("model", model)):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} 不能为空串")
    # 终审 P1-manifest（2026-10-11 修复包）：R7 回填开关实际生效态单源
    # 解析（config 在场记 config 态；否则 env 解析；显式同传冲突拒——
    # 见 build_run_manifest docstring）。
    switch_source = os.environ if env is None else env
    raw_backfill = (
        switch_source.get(_jvc.JUDGE_BACKFILL_ENV) or "").strip()
    if judge_version_config is not None:
        if raw_backfill and _jvc.backfill_enabled_from_env(
                switch_source) != judge_version_config.backfill_enabled:
            raise ValueError(
                f"judge_version_config.backfill_enabled="
                f"{judge_version_config.backfill_enabled!r} 与 env "
                f"{_jvc.JUDGE_BACKFILL_ENV}={raw_backfill!r} 同传冲突"
                f"（终审 P1-manifest：实际生效开关态必须单源可溯，"
                f"不得静默选边——显式 config 与 env 冲突即拒）")
        effective_backfill = judge_version_config.backfill_enabled
    else:
        effective_backfill = _jvc.backfill_enabled_from_env(switch_source)
    backfill_enabled_state = "1" if effective_backfill else "0"
    # 用户令 2026-10-11（LLM 事实供给）：供给选择实际生效态（env 单源，
    # H 项——无 config 对象无同传冲突面；switch_state 已同时快照 env
    # 原值，双口径可对拍）。
    fact_supply_state = fact_supply_mode_from_env(env)
    # P1a-T4（卡3.5）：durable 队列实态（env 单源；容量/租约/纪元取
    # 域层单源常量——代码版配置即版本清单锚）。
    durable_queue_state = (
        "1" if durable_queue_enabled_from_env(env) else "0")
    return RunManifest(
        schema_version=MANIFEST_SCHEMA_VERSION,
        pipeline_version=pipeline_version,
        dict_version=dict_version,
        prompt_version=prompt_version,
        prompt_sha256=prompt_sha256,
        policy_version=policy_version,
        backfill_enabled=backfill_enabled_state,
        fact_supply=fact_supply_state,
        model=model,
        embedding_space=_embedding_space_id(embedding_space),
        switch_state=snapshot_switch_state(env),
        code_git_sha=resolve_code_git_sha(code_git_sha, env=env, repo_root=repo_root),
        inputs_sha256=inputs_manifest_sha256(items),
        input_count=len(items),
        durable_queue=durable_queue_state,
        queue_capacity=DURABLE_QUEUE_CAPACITY,
        queue_warning_depth=DURABLE_QUEUE_WARNING_DEPTH,
        queue_critical_depth=DURABLE_QUEUE_CRITICAL_DEPTH,
        work_lease_seconds=int(WORK_LEASE_DEFAULT_SECONDS),
        admission_config_version=ADMISSION_CONFIG_VERSION,
    )


__all__ = [
    "CODE_GIT_SHA_ENV",
    "DEFAULT_MODEL",
    "DEFAULT_PIPELINE_VERSION",
    "DEFAULT_POLICY_VERSION",
    "DURABLE_QUEUE_ENV",
    "JUDGE_PROMPT_VERSION",
    "KNOWN_SWITCHES",
    "MANIFEST_SCHEMA_VERSION",
    "POLICY_VERSION_V2",
    "POLICY_VERSION_V3",
    "RULE_DICT_VERSION",
    "RunManifest",
    "build_run_manifest",
    "durable_queue_enabled_from_env",
    "inputs_manifest_sha256",
    "resolve_code_git_sha",
    "snapshot_switch_state",
]
