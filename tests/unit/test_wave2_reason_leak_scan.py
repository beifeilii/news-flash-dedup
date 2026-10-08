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
