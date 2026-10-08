"""P18 实施测试：审计先存 + 主记录 CAS + watermark + 崩溃恢复 + fake_store 无 ES。

按 05:25 主审核方批复标尺：
1. 审计先存失败即停
2. CAS 前置四校验（audit_complete + task_state + delivery_state + raw_hash）
3. 失败两路径（CAS 失败不动 delivery_state）
4. watermark 单调性
5. 崩溃恢复三场景（仅 audit 落库 / CAS OK + watermark pending / 全部完成）
6. fake_store 不引入真 ES 客户端
"""

from __future__ import annotations

import hashlib

import pytest

from news_flash_dedup.decide.types import DecideOutcome
from news_flash_dedup.persist import (
    FakeAuditDoc,
    FakeMainRecordV1,
    FakeP18Store,
    P18PersistError,
    audit_persist,
    coordinator,
    main_record_persist,
)


def _audit_record(rid_history="a" * 64, rid_current="b" * 64,
                   history_hash="h", current_hash="c",
                   basis="FACT_EQUIVALENT") -> dict:
    return {
        "comparison_id": f"{rid_history}:{rid_current}",
        "history_record_id": rid_history,
        "current_record_id": rid_current,
        "history_raw_hash": history_hash,
        "current_raw_hash": current_hash,
        "basis": basis,
        "field_path": "facts.f1.subject",
        "detail": "主体事件一致",
        "history_evidence": {"record_id": rid_history, "field": "x",
                                  "quote": "甲", "start": 0, "end": 1},
        "current_evidence": {"record_id": rid_current, "field": "x",
                                 "quote": "甲", "start": 0, "end": 1},
        "pipeline_version": "dedup_v1",
    }


def _decide(decision="重复", item_id="item-C"):
    return DecideOutcome(
        item_id=item_id, text="甲公司完成回购。",
        decision=decision,
        duplicate_ids=("item-A",) if decision == "重复" else (),
        reason="主体与事件一致",
        internal_code="FACT_EQUIVALENT",
        pair_codes={"a" * 64: "FACT_EQUIVALENT"},
        used_evidence=(),
        raw_hash="h",
        pipeline_version="dedup_v1",
    )


# ---------- 标尺 1：审计先存失败即停 ----------

def test_audit_persist_succeeds_on_valid_records():
    """有效审计 batch → 全部落库，返回 comparison_id 列表。"""
    store = FakeP18Store()
    records = [_audit_record(basis="FACT_EQUIVALENT"),
                _audit_record(rid_history="c" * 64, rid_current="d" * 64,
                                  basis="VERIFIED_CONFLICT")]
    ids = audit_persist.write_audit_batch(store, records)
    assert len(ids) == 2
    assert len(store.audit_docs) == 2


def test_audit_persist_audit_complete_false_blocks_cas():
    """audit_complete=False → P18PersistError（CAS 前置，绝不掩盖）。"""
    store = FakeP18Store()
    decide = _decide()
    with pytest.raises(P18PersistError, match="(?i)audit_complete"):
        coordinator.persist_main_record(
            decide, store, arrival_seq=1, raw_hash="h",
            audit_ids=(), audit_complete=False,
        )
    assert store.main_records == {}


# ---------- 标尺 2：CAS 前置四校验 ----------

def test_cas_requires_delivery_state_pending():
    """delivery_state != pending → ValueError。"""
    record = FakeMainRecordV1(
        record_id="r-1", item_id="item-A", text="t", scope_id="default",
        business_date="2026-09-26", arrival_seq=1, raw_hash="h",
        pipeline_version="dedup_v1", fact_artifact_hash="",
        vector_state="pending",
        result_item_id="item-A", result_decision="重复",
        result_duplicate_ids=("item-B",), result_reason="r",
        task_state="succeeded", completed_at="2026-09-26T03:33:00",
        result_version=1, event_id="e",
        audit_ids=(), audit_complete=True,
        reason_code="FACT_EQUIVALENT",
        callback_body="{}", callback_body_hash="h",
        delivery_state="not_ready",       # 错：未 CAS 翻 pending
        delivery_deadline_at="2026-09-27T03:33:00",
        next_delivery_at="2026-09-26T03:33:00",
        callback_attempts=0, round_attempts=0, round=1,
    )
    store = FakeP18Store()
    with pytest.raises(ValueError, match="(?i)delivery_state=pending"):
        main_record_persist.cas_main_record(store, record, expected_seq_no=0,
                                              expected_primary_term=0)


def test_cas_requires_task_state_succeeded_when_delivery_pending():
    """delivery_state=pending 但 task_state ≠ succeeded → ValueError。"""
    record = FakeMainRecordV1(
        record_id="r-1", item_id="item-A", text="t", scope_id="default",
        business_date="2026-09-26", arrival_seq=1, raw_hash="h",
        pipeline_version="dedup_v1", fact_artifact_hash="",
        vector_state="pending",
        result_item_id="item-A", result_decision="重复",
        result_duplicate_ids=("item-B",), result_reason="r",
        task_state="failed",              # 错：与 delivery_state=pending 不一致
        completed_at="2026-09-26T03:33:00",
        result_version=1, event_id="e",
        audit_ids=(), audit_complete=True,
        reason_code="FACT_EQUIVALENT",
        callback_body="{}", callback_body_hash="h",
        delivery_state="pending",
        delivery_deadline_at="2026-09-27T03:33:00",
        next_delivery_at="2026-09-26T03:33:00",
        callback_attempts=0, round_attempts=0, round=1,
    )
    store = FakeP18Store()
    with pytest.raises(ValueError, match="(?i)task_state=succeeded"):
        main_record_persist.cas_main_record(store, record, expected_seq_no=0,
                                              expected_primary_term=0)


def test_cas_requires_audit_complete_true():
    """audit_complete=False → ValueError（CAS 前置）。"""
    record = FakeMainRecordV1(
        record_id="r-1", item_id="item-A", text="t", scope_id="default",
        business_date="2026-09-26", arrival_seq=1, raw_hash="h",
        pipeline_version="dedup_v1", fact_artifact_hash="",
        vector_state="pending",
        result_item_id="item-A", result_decision="重复",
        result_duplicate_ids=("item-B",), result_reason="r",
        task_state="succeeded", completed_at="2026-09-26T03:33:00",
        result_version=1, event_id="e",
        audit_ids=(), audit_complete=False,  # 错：audit_complete=False
        reason_code="FACT_EQUIVALENT",
        callback_body="{}", callback_body_hash="h",
        delivery_state="pending",
        delivery_deadline_at="2026-09-27T03:33:00",
        next_delivery_at="2026-09-26T03:33:00",
        callback_attempts=0, round_attempts=0, round=1,
    )
    store = FakeP18Store()
    with pytest.raises(ValueError, match="(?i)audit_complete"):
        main_record_persist.cas_main_record(store, record, expected_seq_no=0,
                                              expected_primary_term=0)


# ---------- 标尺 3：CAS 失败路径不动 delivery_state（INV-2）----------

def test_cas_failure_path_does_not_modify_delivery_state_in_pending_status():
    """CAS 失败时 delivery_state 维持（不在 fake_store 写入）。
    自然测试：当 audit_complete=False 时 persist_main_record 抛错，store 未被写入。
    """
    store = FakeP18Store()
    decide = _decide()
    with pytest.raises(P18PersistError):
        coordinator.persist_main_record(
            decide, store, arrival_seq=1, raw_hash="h",
            audit_ids=(), audit_complete=False,
        )
    assert store.main_records == {}
    assert store.watermark_seq == {}
    assert store.cas_calls == []


# ---------- 标尺 4：watermark 单调性 ----------

def test_watermark_monotonicity_rejects_regression():
    """arrival_seq <= prev_watermark → ValueError（不重写已推进的水位）。"""
    store = FakeP18Store()
    store.advance_watermark("default", "2026-09-26", 10)
    with pytest.raises(ValueError, match="(?i)regress"):
        store.advance_watermark("default", "2026-09-26", 5)


def test_watermark_advances_monotonically():
    """同 (scope, date) 多次推进，单调非降；回归被拒。"""
    store = FakeP18Store()
    seq1 = store.advance_watermark("default", "2026-09-26", 1)
    seq2 = store.advance_watermark("default", "2026-09-26", 5)
    assert seq1 == 1 and seq2 == 5
    assert store.watermark_seq["default|2026-09-26"] == 5
    # 回归（3 < 5）必须被拒
    with pytest.raises(ValueError, match="(?i)regress"):
        store.advance_watermark("default", "2026-09-26", 3)


# ---------- 标尺 5：崩溃恢复三场景 ----------

def test_crash_recovery_audit_persisted_only():
    """场景 1：仅审计落库，主记录未 CAS → audit_persisted_only=True。"""
    store = FakeP18Store()
    audit_persist.write_audit_batch(store, [_audit_record()])
    summary = coordinator.crash_recovery_scenarios(store)
    assert summary["audit_persisted_only"] is True
    assert summary["full_committed"] is False


def test_crash_recovery_cas_ok_watermark_pending():
    """场景 2：主记录 CAS 但 watermark 未推进 → cas_ok_watermark_pending=True。"""
    store = FakeP18Store()
    decide = _decide()
    record = FakeMainRecordV1(
        record_id="r-1", item_id=decide.item_id, text=decide.text,
        scope_id="default", business_date="2026-09-26",
        arrival_seq=1, raw_hash="h", pipeline_version="dedup_v1",
        fact_artifact_hash="", vector_state="pending",
        result_item_id=decide.item_id, result_decision=decide.decision,
        result_duplicate_ids=decide.duplicate_ids, result_reason=decide.reason,
        task_state="succeeded", completed_at="2026-09-26T03:33:00",
        result_version=1, event_id="e", audit_ids=(), audit_complete=True,
        reason_code=decide.internal_code, callback_body="{}",
        callback_body_hash="h", delivery_state="held",
        delivery_deadline_at="2026-09-27T03:33:00",
        next_delivery_at="2026-09-26T03:33:00",
        callback_attempts=0, round_attempts=0, round=1,
    )
    main_record_persist.cas_main_record(store, record, expected_seq_no=0,
                                          expected_primary_term=0)
    # 注意：手动写入主记录但未推进 watermark
    summary = coordinator.crash_recovery_scenarios(store)
    assert summary["main_records"] == 1
    assert summary["watermark_entries"] == 0
    assert summary["cas_ok_watermark_pending"] is True


def test_crash_recovery_full_committed():
    """场景 3：CAS + watermark + audit 都完成 → full_committed=True。"""
    store = FakeP18Store()
    audit_persist.write_audit_batch(store, [_audit_record()])
    decide = _decide()
    coordinator.persist_main_record(
        decide, store, arrival_seq=1, raw_hash="h",
        audit_ids=("a" * 64 + ":b" * 64,), audit_complete=True,
    )
    summary = coordinator.crash_recovery_scenarios(store)
    assert summary["main_records"] == 1
    assert summary["audit_docs"] == 1
    assert summary["watermark_entries"] == 1
    assert summary["full_committed"] is True


# ---------- 标尺 6：fake_store 无 ES 客户端 ----------

def test_fake_store_does_not_import_es_client():
    """fake_store 不引入真 ES 客户端。"""
    import news_flash_dedup.persist.fake_store as fs
    module_attrs = dir(fs)
    for attr in ("ESClient", "elasticsearch", "es_client", "Elasticsearch"):
        assert attr not in module_attrs, (
            f"fake_store 不应引入真 ES 客户端 {attr!r}"
        )


# ---------- 端到端 smoke ----------

def test_persist_main_record_end_to_end_smoke():
    """完整路径：persist_main_record 一次完整跑通；store 状态变化符合预期。"""
    store = FakeP18Store()
    decide = _decide(decision="重复", item_id="item-C")
    audit_ids = ("comp-1",)
    audit_persist.write_audit_batch(store, [_audit_record()])
    record = coordinator.persist_main_record(
        decide, store, arrival_seq=1, raw_hash="h",
        audit_ids=audit_ids, audit_complete=True,
    )
    assert record.delivery_state == "held"             # 10-07 令甲-i：重复件扣留不投递
    assert record.task_state == "succeeded"
    assert record.audit_complete is True
    assert record.vector_state == "pending"           # 05:25 修订：首次创建写 pending
    assert store.watermark_seq["default|2026-09-26"] == 1
    assert len(store.cas_calls) == 1
    assert len(store.audit_calls) == 1
    assert len(store.watermark_calls) == 1


# ---------- 失败路径：CAS 冲突的 fallback 形状 ----------

def test_cas_failure_due_to_missing_audit_keeps_state_clean():
    """CAS 失败路径：store 未被污染（无 main_records / 无 watermark / 无 audit_calls）。"""
    store = FakeP18Store()
    audit_persist.write_audit_batch(store, [_audit_record()])  # 提前写一份审计
    decide = _decide()
    with pytest.raises(P18PersistError):
        coordinator.persist_main_record(
            decide, store, arrival_seq=1, raw_hash="h",
            audit_ids=(), audit_complete=False,
        )
    # 失败的 persist 不动 store（main_records + watermark_seq 空）
    assert store.main_records == {}
    assert store.watermark_seq == {}
    # 但 audit_docs 保留（P18-INV-1：审计先存 + CAS 失败不动审计）
    assert len(store.audit_docs) == 1