# -*- coding: utf-8 -*-
"""修复波 2 红测·A3-F7 reason 内码泄漏扫描集补 CONFLICT_CODES。

红态锚：aggregate.py:359 扫描集=EQUIVALENT|UNRESOLVED——reason 含
VERIFIED_CONFLICT 不触发 AggregateError（纯防御缺口，现役无活性路径）。

实现窗登记：设计稿导入面 `recall.fusion.FrozenRecallPlan` /
`recall.models.CoverageStatus` 与现役不符——二者同出 compare.aggregate
（decide/service.py:24-27 现役导入面亲读），按现役修正（语义钉不动）。
"""
from __future__ import annotations

import pytest

from news_flash_dedup.compare.aggregate import (
    AggregateError, CoverageStatus, FrozenRecallPlan, aggregate)
from news_flash_dedup.compare.pair_compare import PairIssue


def _ctx():
    return {"record_id": "c" * 64, "item_id": "item-C", "text": "甲公司完成回购。",
            "raw_hash": "0" * 64, "scope_id": "default",
            "business_date": "2026-09-26", "arrival_seq": 3,
            "pipeline_version": "dedup_v1"}


def test_reason_containing_conflict_code_rejected():
    """reason 拼接面若含 VERIFIED_CONFLICT→AggregateError（扫描集补全后）。"""
    plan = FrozenRecallPlan(version="rrf_v1_k60_30_10", required={})
    issues = [PairIssue("FACT_INCOMPLETE",
                        "细节文本异常混入 VERIFIED_CONFLICT 字样")]
    coverage = CoverageStatus(visible_seq=10, prepared_seq=10, complete=True)
    with pytest.raises(AggregateError, match="VERIFIED_CONFLICT"):
        aggregate(_ctx(), plan, [], new_text_issues=issues, coverage=coverage)


# 提交一（2026-10-10，p3-semantic-authority，§5.1 白名单同步）：判官语义
# 权威三码同步入扫描集——公共 reason 出现任一内部码即拒（聚合层扫描
# EQUIVALENT|UNRESOLVED|CONFLICT 三集合全覆盖）。
@pytest.mark.parametrize("code", [
    "JUDGE_EQUIVALENT", "JUDGE_NON_DUPLICATE", "JUDGE_UNCERTAIN",
])
def test_reason_containing_judge_semantic_code_rejected(code):
    plan = FrozenRecallPlan(version="rrf_v1_k60_30_10", required={})
    issues = [PairIssue("FACT_INCOMPLETE", f"细节文本异常混入 {code} 字样")]
    coverage = CoverageStatus(visible_seq=10, prepared_seq=10, complete=True)
    with pytest.raises(AggregateError, match=code):
        aggregate(_ctx(), plan, [], new_text_issues=issues, coverage=coverage)


def test_decide_outcome_reason_containing_judge_code_rejected():
    """同源钉（decide/types.py 扫描闸）：DecideOutcome 公共 reason 携判官
    内码 → 构造即 ValueError（五字段合同的最后一道闸）。"""
    from news_flash_dedup.decide.types import DecideOutcome
    for code in ("JUDGE_EQUIVALENT", "JUDGE_NON_DUPLICATE", "JUDGE_UNCERTAIN"):
        with pytest.raises(ValueError, match=code):
            DecideOutcome(
                item_id="item-C", text="甲公司完成回购。", decision="不重复",
                duplicate_ids=(), reason=f"异常混入 {code} 字样",
                internal_code="NO_DUPLICATE_FOUND")
