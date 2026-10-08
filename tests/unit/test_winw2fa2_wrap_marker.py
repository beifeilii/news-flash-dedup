# -*- coding: utf-8 -*-
"""W2Fα2 条目 53 钉面（E 批 H-04 修法 A 后重写，e-batch-design §3.3 F2 行
+§3.4 对账程序第 5 条逐字执行）。

沿革：原钉 =裸包装 artifact 携带 unvalidated/unvalidated_note 显式标注。
H-04 全量单轨接线后裸包装短路退役（编排路径一律过 validate_fact_artifact），
"未经校验"标注成假命题、随实现一并退役——钉面按设计稿迁移到修法 A 后的
真值承载层：validation provenance 三键钉 + validated_complete 真值表钉；
既有七键守卫（test_h3_existing_artifact_keys_unchanged）逐字不动。
"""
from __future__ import annotations

from news_flash_dedup.decide.service import _wrap_facts_as_report


def test_h3_artifact_marks_unvalidated_explicitly():
    """F2 重写①：标注退役→校验 provenance 三键钉（validator/projection/
    issue_count）；unvalidated 旧键不得留存（修法 A 下=假命题）。"""
    report = _wrap_facts_as_report("r1", "正文", [])
    assert "unvalidated" not in report.artifact
    assert "unvalidated_note" not in report.artifact
    validation = report.artifact["validation"]
    assert validation["validator"] == "validate_fact_artifact"
    assert validation["schema_projection"] == "e1_numeric_slot_v1"
    assert validation["issue_count"] == len(report.issues)


def test_h3_marker_present_with_facts_and_nondefault_status():
    """F2 重写②：带 facts + 非默认 extraction_status 同钉 provenance
    三键；issue_count 与报告 issues 如实联动（最小 dict 不过 Schema
    →计数>0）。"""
    report = _wrap_facts_as_report("r1", "正文", [{"fact_id": "f1"}],
                                   extraction_status="partial")
    assert "unvalidated" not in report.artifact
    validation = report.artifact["validation"]
    assert validation["validator"] == "validate_fact_artifact"
    assert validation["schema_projection"] == "e1_numeric_slot_v1"
    assert validation["issue_count"] == len(report.issues) > 0


def test_h3_existing_artifact_keys_unchanged():
    """条目53 守卫（前后均绿）：既有七键逐字节不变（标注只增不改）。"""
    report = _wrap_facts_as_report("r1", "正文", [{"fact_id": "f1"}],
                                   extraction_status="partial")
    assert report.artifact["schema_version"] == "1.0"
    assert report.artifact["record_id"] == "r1"
    assert report.artifact["offset_unit"] == "unicode_code_point"
    assert report.artifact["extraction_status"] == "partial"
    assert report.artifact["facts"] == [{"fact_id": "f1"}]
    assert report.artifact["unparsed_spans"] == []
    assert report.artifact["uncertainties"] == []


def test_h3_validated_complete_semantics_unchanged():
    """F2 重写③："语义不变"命题作废→真值表钉（e-batch-design §3.3 F2
    行逐字：空=False same、最小 dict=True→False 翻、partial=False
    same；探针 S7/S9 同格）。"""
    empty = _wrap_facts_as_report("r1", "正文", [])
    assert empty.validated_complete is False
    minimal = _wrap_facts_as_report("r1", "正文", [{"fact_id": "f1"}])
    assert minimal.validated_complete is False      # 修法 A 真值化：最小 dict 不过校验
    partial = _wrap_facts_as_report("r1", "正文", [{"fact_id": "f1"}],
                                    extraction_status="partial")
    assert partial.validated_complete is False
