"""P2 先行件②单测（2026-10-09）：lib/run_manifest 跑批不可变版本清单。

正典/出处：终裁令 §二 正典栈版本化纪律 + 宪章《判定宪章-草案-v2-1009.md》
§六-2（policy_version 版本化、考卷封存不得无痕重标）——清单须字段齐全、
可序列化、同输入同 manifest。
"""

from __future__ import annotations

import json

import pytest

from news_flash_dedup.decide.llm_residual import (
    DEFAULT_MODEL,
    JUDGE_PROMPT_VERSION,
    PROMPT_SHA256,
)
from news_flash_dedup.facts.rule import RULE_DICT_VERSION
from news_flash_dedup.lib import run_manifest as rm
from news_flash_dedup.recall.vector_space import EmbeddingSpace

GIT_SHA = "0123456789abcdef0123456789abcdef01234567"
OTHER_GIT_SHA = "ffffffffffffffffffffffffffffffffffffffff"
FAKE_SPACE_ID = "s_" + "1" * 40


def _space() -> EmbeddingSpace:
    return EmbeddingSpace(
        model="text_embedding_v3", revision="n11_20260927",
        tokenizer="qwen_bpe_5dfeb309", pooling="none", metric="COSINE",
        dimension=1024, document_encoding="plain_v1",
        query_encoding="plain_v1", chunking="tokens512_overlap64_bpe_v1",
        preprocessing="p09_v1")


def _build(**overrides):
    params = dict(
        inputs=("rec-a", "rec-b", "rec-c"),
        embedding_space=FAKE_SPACE_ID,
        code_git_sha=GIT_SHA,
        env={},
    )
    params.update(overrides)
    return rm.build_run_manifest(**params)


# ---------- 字段齐全 ----------

def test_manifest_fields_complete():
    d = _build().to_dict()
    assert set(d) == {
        "schema_version", "pipeline_version", "dict_version",
        "prompt_version", "prompt_sha256", "policy_version", "model",
        "embedding_space", "switch_state", "code_git_sha",
        "inputs_sha256", "input_count",
    }
    assert d["schema_version"] == rm.MANIFEST_SCHEMA_VERSION
    assert d["pipeline_version"] == "dedup_v1"
    assert d["dict_version"] == RULE_DICT_VERSION
    assert d["prompt_version"] == JUDGE_PROMPT_VERSION
    assert d["prompt_sha256"] == PROMPT_SHA256      # 注册处单源解析
    assert d["policy_version"] == "policy_v2"       # 宪章生效版字面量
    assert d["model"] == DEFAULT_MODEL
    assert d["embedding_space"] == FAKE_SPACE_ID
    assert d["code_git_sha"] == GIT_SHA
    assert d["input_count"] == 3
    assert set(d["switch_state"]) == set(rm.KNOWN_SWITCHES)


def test_policy_version_literal_is_policy_v2():
    """宪章 §六-2 字面量钉：默认 policy_version 恒为 policy_v2。"""
    assert rm.POLICY_VERSION_V2 == "policy_v2"
    assert rm.DEFAULT_POLICY_VERSION == "policy_v2"
    assert _build().policy_version == "policy_v2"


# ---------- 可序列化 ----------

def test_manifest_json_serializable_and_deterministic():
    manifest = _build()
    payload = manifest.to_json()
    assert json.loads(payload) == manifest.to_dict()          # 往返无损
    assert payload == _build().to_json()                      # 确定性序列化
    again = json.loads(json.dumps(manifest.to_dict(), ensure_ascii=False))
    assert again == manifest.to_dict()                        # dict 面可 dumps


# ---------- 同输入同 manifest ----------

def test_same_inputs_same_manifest_sha256():
    left = _build()
    right = _build(inputs=("rec-c", "rec-a", "rec-b"))        # 乱序同集
    assert left.manifest_sha256() == right.manifest_sha256()  # 输入排序无关
    assert left.inputs_sha256 == right.inputs_sha256
    assert left == right                                      # frozen 值等


def test_different_inputs_different_manifest():
    assert _build().manifest_sha256() != _build(inputs=("rec-a",)).manifest_sha256()
    assert _build().manifest_sha256() != _build(code_git_sha=OTHER_GIT_SHA).manifest_sha256()
    assert _build().manifest_sha256() != _build(
        embedding_space=_space()).manifest_sha256()


def test_embedding_space_instance_uses_space_id():
    space = _space()
    manifest = _build(embedding_space=space)
    assert manifest.embedding_space == space.space_id
    with pytest.raises(TypeError):
        _build(embedding_space=object())


# ---------- 开关快照 ----------

def test_switch_state_snapshot_honors_injected_env():
    env = {"DEDUP_RECALL_MODE": "shadow", "DEDUP_CERT_DECOUPLE": "1",
           "UNRELATED_ENV": "x"}
    manifest = _build(env=env)
    state = dict(manifest.switch_state)
    assert state["DEDUP_RECALL_MODE"] == "shadow"
    assert state["DEDUP_CERT_DECOUPLE"] == "1"
    assert state["DEDUP_COVERAGE_FRONTIER"] == ""             # 缺席记空串
    assert "UNRELATED_ENV" not in state                       # 非登记开关不入册
    assert [name for name, _ in manifest.switch_state] == list(rm.KNOWN_SWITCHES)


# ---------- git sha 解析（fail-closed） ----------

def test_git_sha_explicit_and_env_precedence():
    assert rm.resolve_code_git_sha(GIT_SHA) == GIT_SHA
    assert rm.resolve_code_git_sha(env={rm.CODE_GIT_SHA_ENV: OTHER_GIT_SHA}) == OTHER_GIT_SHA
    assert rm.resolve_code_git_sha(GIT_SHA, env={rm.CODE_GIT_SHA_ENV: OTHER_GIT_SHA}) == GIT_SHA
    with pytest.raises(ValueError):
        rm.resolve_code_git_sha("deadbeef")                   # 截断值拒识
    with pytest.raises(ValueError):
        rm.resolve_code_git_sha(env={rm.CODE_GIT_SHA_ENV: "not-a-sha"})
    with pytest.raises(ValueError):
        _build(code_git_sha="UNKNOWN")                        # 占位值拒识


def test_inputs_manifest_sha256_sorted_and_typed():
    assert rm.inputs_manifest_sha256(["b", "a"]) == rm.inputs_manifest_sha256(["a", "b"])
    assert rm.inputs_manifest_sha256([]) != rm.inputs_manifest_sha256(["a"])
    with pytest.raises(TypeError):
        rm.inputs_manifest_sha256([1])
