# -*- coding: utf-8 -*-
"""W2Fα2 红测（条目 5，WA1b-L1）：index_prefix 死参数接线。

探针（winw2fa2-probe-index-prefix.json）实证：build_audit_batch 的
index_prefix 形参被静默丢弃（AuditBatch 无该字段）；生产唯一调用方
commit/coordinator.commit_one 实传 "p17-fake-audits-v1"，实际落库不经
to_index_actions（src 内零消费方）——参数纯死、审计批不落错索引（不升中危）。
修法（按探针定）=接线：AuditBatch 携带 index_prefix，to_index_actions 缺省
用之；不可删参（coordinator 在禁碰面实传，删参会 TypeError）。
"""
from __future__ import annotations

from news_flash_dedup.compare.pair_compare import PairResult
from news_flash_dedup.decide import audit as audit_module


def _pair() -> PairResult:
    return PairResult(
        pair_id="h|c", history_record_id="h" * 64, current_record_id="c" * 64,
        history_item_id="item-h", current_item_id="item-c",
        history_arrival_seq=1, current_arrival_seq=2,
        history_raw_hash="hh", current_raw_hash="cc",
        pipeline_version="dedup_v1", outcome="unresolved",
        code="FACT_INCOMPLETE", detail="probe",
        aligned_facts=(), verified_conflicts=(), unresolved_fields=(),
        used_evidence=(), budget_at=0,
    )


def test_l1_batch_carries_passed_index_prefix():
    """条目5 主红测：实传 index_prefix 不再被静默丢弃。"""
    batch = audit_module.build_audit_batch(
        [_pair()], index_prefix="p17-fake-audits-v1", audit_complete=True)
    assert batch.index_prefix == "p17-fake-audits-v1"


def test_l1_to_index_actions_defaults_to_batch_prefix():
    """条目5 接线钉：to_index_actions() 缺省用批次自带 prefix。"""
    batch = audit_module.build_audit_batch(
        [_pair()], index_prefix="winw2fa2-probe-audits", audit_complete=True)
    actions = batch.to_index_actions()
    assert actions[0]["index"]["_index"] == "winw2fa2-probe-audits"


def test_l1_explicit_prefix_still_overrides():
    """条目5 兼容守卫（前后均绿）：显式实参仍优先（现役测试契约不破）。"""
    batch = audit_module.build_audit_batch(
        [_pair()], index_prefix="batch-own-prefix", audit_complete=True)
    actions = batch.to_index_actions("explicit-override-prefix")
    assert actions[0]["index"]["_index"] == "explicit-override-prefix"


def test_l1_default_prefix_value_unchanged():
    """条目5 缺省守卫（前后均绿）：未实传时缺省值沿用原默认串。"""
    batch = audit_module.build_audit_batch([_pair()], audit_complete=True)
    assert batch.index_prefix == "p17-batch-decide-audits-v1"
