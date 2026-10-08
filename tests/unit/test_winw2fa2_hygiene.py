# -*- coding: utf-8 -*-
"""W2Fα2 红测（条目 8，WA1b 卫生批 · decide 域死代码族 5 件）：

① types.py:55-59 不可达死分支（基数门 L51 先行拒绝，第二分支结构性不达）；
② audit.py:53 AuditBatch.created_at 死字段 + build_audit_batch 恒值默认
   （全 src/tests 零消费方，实证见本文件钉）；
③ audit.py:11 Mapping 死导入；
④ service.py:197 死局部变量 history_ctx（定义后零读取）；
⑤ service.py:71-92 _history_context/_current_context 逐字重复 → 抽单源。

死代码红测形态：结构钉（先红后绿）+ 行为守卫（前后均绿，防清扫误伤语义）。
"""
from __future__ import annotations

import dataclasses
import inspect

import pytest

from news_flash_dedup.decide import audit as audit_module
from news_flash_dedup.decide import service as service_module
from news_flash_dedup.decide import types as types_module


# ---------------------------------------------------------------- ① types 死分支

def test_h1_decide_outcome_no_dead_cardinality_branch():
    """①结构钉：__post_init__ 不含第二基数分支（L55-59 死代码）。"""
    src = inspect.getsource(types_module.DecideOutcome)
    assert "but duplicate_ids is non-empty" not in src


def test_h1_cardinality_still_enforced_for_non_duplicate():
    """①行为守卫（前后均绿）：不重复+非空 duplicate_ids 仍 ValueError
    （由第一基数门执法，死分支拆除不松闸）。"""
    with pytest.raises(ValueError, match="duplicate_ids"):
        types_module.DecideOutcome(
            item_id="i1", text="正文", decision="不重复",
            duplicate_ids=("x",), reason="r", internal_code="NO_DUPLICATE_FOUND")


def test_h1_cardinality_still_enforced_for_boundary():
    """①行为守卫（前后均绿）：边界+非空 duplicate_ids 仍 ValueError。"""
    with pytest.raises(ValueError, match="duplicate_ids"):
        types_module.DecideOutcome(
            item_id="i1", text="正文", decision="边界case/疑难case",
            duplicate_ids=("x",), reason="r", internal_code="FACT_INCOMPLETE")


# ---------------------------------------------------------------- ② audit created_at 死字段

def test_h2_audit_batch_has_no_created_at_dead_field():
    """②结构钉：AuditBatch 字段封闭为 records/audit_complete/index_prefix。"""
    names = {f.name for f in dataclasses.fields(audit_module.AuditBatch)}
    assert "created_at" not in names


def test_h2_build_audit_batch_signature_drops_created_at():
    """②结构钉：build_audit_batch 不再收 created_at 恒值默认形参。"""
    params = inspect.signature(audit_module.build_audit_batch).parameters
    assert "created_at" not in params


# ---------------------------------------------------------------- ③ audit Mapping 死导入

def test_h3_audit_module_no_dead_mapping_import():
    """③结构钉：audit 模块命名空间无 Mapping（死导入清扫）。"""
    assert "Mapping" not in vars(audit_module)


# ---------------------------------------------------------------- ④ service 死局部变量

def test_h4_decide_for_task_no_dead_history_ctx_local():
    """④结构钉：decide_for_task 源码无 history_ctx 死局部变量。"""
    src = inspect.getsource(service_module.decide_for_task)
    assert "history_ctx" not in src


# ---------------------------------------------------------------- ⑤ context 函数单源

def test_h5_context_builders_single_sourced():
    """⑤结构钉：_history_context 与 _current_context 同一函数对象（单源）。"""
    assert service_module._history_context is service_module._current_context


def test_h5_context_builder_output_shape_unchanged():
    """⑤行为守卫（前后均绿）：context dict 八键形态与缺省值不变。"""
    ctx = service_module._history_context(
        {}, record_id="r", item_id="i", text="t", raw_hash="h", arrival_seq=3)
    assert ctx == {
        "record_id": "r", "item_id": "i", "text": "t", "raw_hash": "h",
        "scope_id": "default", "business_date": "2026-09-26",
        "arrival_seq": 3, "pipeline_version": "dedup_v1",
    }
    ctx2 = service_module._current_context(
        {}, record_id="r2", item_id="i2", text="t2", raw_hash="h2",
        arrival_seq=4, scope_id="s", business_date="2026-09-27",
        pipeline_version="pv")
    assert ctx2["scope_id"] == "s" and ctx2["business_date"] == "2026-09-27"
    assert ctx2["pipeline_version"] == "pv" and ctx2["arrival_seq"] == 4
