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

from news_flash_dedup.decide.llm_residual import (
    DEFAULT_MODEL,
    JUDGE_PROMPT_VERSION,
    judge_prompt_for_version,
)
from news_flash_dedup.decide.policy_version import (
    DEFAULT_POLICY_VERSION,
    POLICY_VERSION_V2,
    POLICY_VERSION_V3,
)
from news_flash_dedup.facts.rule import RULE_DICT_VERSION

MANIFEST_SCHEMA_VERSION = "run_manifest_v1"

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
    "DEDUP_EMBEDDING_DAILY_TOKEN_BUDGET",  # vector/embedding_client.py（预算闸）
    "DEDUP_EXACT_MIN_LEN",              # text/__init__.py（最小正文长度闸）
    # 提交二（§5.2 文件 H-2）补登记：判官证明路开关 + 判官进主链开关
    # 提交三（§5.3）补登记：判官裁决口径灰度开关（默认 legacy_proof_gate）
    "DEDUP_JUDGE_DECISION_MODE",        # decide/judge_pair.py（灰度，默认 legacy）
    "DEDUP_JUDGE_IN_CHAIN",             # decide/judge_pair.py（判官进主链，默认关）
    "DEDUP_JUDGE_PROOF",                # decide/judge_proof.py（证明路，默认关）
    "DEDUP_RECALL_MODE",                # recall/service.py（召回/判重模式闸）
)


def snapshot_switch_state(env: Mapping[str, str] | None = None) -> tuple[tuple[str, str], ...]:
    """已知开关快照：((名称, 值), …)，固定 KNOWN_SWITCHES 序；缺省读 os.environ。"""
    source = os.environ if env is None else env
    return tuple((name, source.get(name, "")) for name in KNOWN_SWITCHES)


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
    """一次跑批的不可变版本清单。字段语义见模块 docstring 六元组+两锚。"""
    schema_version: str
    pipeline_version: str
    dict_version: str
    prompt_version: str
    prompt_sha256: str
    policy_version: str
    model: str
    embedding_space: str
    switch_state: tuple[tuple[str, str], ...]
    code_git_sha: str
    inputs_sha256: str
    input_count: int

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "pipeline_version": self.pipeline_version,
            "dict_version": self.dict_version,
            "prompt_version": self.prompt_version,
            "prompt_sha256": self.prompt_sha256,
            "policy_version": self.policy_version,
            "model": self.model,
            "embedding_space": self.embedding_space,
            "switch_state": {name: value for name, value in self.switch_state},
            "code_git_sha": self.code_git_sha,
            "inputs_sha256": self.inputs_sha256,
            "input_count": self.input_count,
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
    prompt_version: str = JUDGE_PROMPT_VERSION,
    prompt_sha256: str | None = None,
    policy_version: str = DEFAULT_POLICY_VERSION,
    model: str = DEFAULT_MODEL,
    code_git_sha: str | None = None,
    env: Mapping[str, str] | None = None,
    repo_root: str | Path | None = None,
) -> RunManifest:
    """装配跑批版本清单。

    必填：inputs（输入身份串清单）、embedding_space（EmbeddingSpace 或
    空间 id 串）。其余缺省取现役常量单源；code_git_sha 三级解析见
    resolve_code_git_sha（缺省 git 子进程，测试请显式注入）。env 同时
    供开关快照与 git sha 环境级解析（单测 hermetic）。
    """
    items = tuple(inputs)
    if prompt_sha256 is None:
        # 注册处单源解析（未知 prompt_version fail-closed ValueError）。
        prompt_sha256 = judge_prompt_for_version(prompt_version)[1]
    if not isinstance(prompt_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", prompt_sha256):
        raise ValueError(f"prompt_sha256 非法：{prompt_sha256!r}（须 64 位小写 hex）")
    for name, value in (("pipeline_version", pipeline_version),
                        ("dict_version", dict_version),
                        ("prompt_version", prompt_version),
                        ("policy_version", policy_version),
                        ("model", model)):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} 不能为空串")
    return RunManifest(
        schema_version=MANIFEST_SCHEMA_VERSION,
        pipeline_version=pipeline_version,
        dict_version=dict_version,
        prompt_version=prompt_version,
        prompt_sha256=prompt_sha256,
        policy_version=policy_version,
        model=model,
        embedding_space=_embedding_space_id(embedding_space),
        switch_state=snapshot_switch_state(env),
        code_git_sha=resolve_code_git_sha(code_git_sha, env=env, repo_root=repo_root),
        inputs_sha256=inputs_manifest_sha256(items),
        input_count=len(items),
    )


__all__ = [
    "CODE_GIT_SHA_ENV",
    "DEFAULT_MODEL",
    "DEFAULT_PIPELINE_VERSION",
    "DEFAULT_POLICY_VERSION",
    "JUDGE_PROMPT_VERSION",
    "KNOWN_SWITCHES",
    "MANIFEST_SCHEMA_VERSION",
    "POLICY_VERSION_V2",
    "POLICY_VERSION_V3",
    "RULE_DICT_VERSION",
    "RunManifest",
    "build_run_manifest",
    "inputs_manifest_sha256",
    "resolve_code_git_sha",
    "snapshot_switch_state",
]
