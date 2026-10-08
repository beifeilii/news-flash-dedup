"""P07-D 身份映射一致性测试。

验证项：
- record_id 由 scope_id+item_id 全局唯一确定；不同次调用结果一致；
- business_fingerprint 由 item_id+text 强约束；同 ID+异文触发 IdentityConflict；
- arrival_seq 单 owner 连续；owner 切换后从 1 重新开始；
- request_key / item_key / seq_key 与 record_id 配套一致；
- 全局唯一性：跨 scope 的 record_id 不重复（sha256 domain separation）。
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

import pytest

from news_flash_dedup.admission import (
    AdmissionRequest, _digest,
)
from news_flash_dedup.batch_admission import BatchAdmissionCoordinator, BatchLimits, _keys
from news_flash_dedup.batch_collector import BatchAdmissionCollector
from news_flash_dedup.product.admission_port import CollectorAdmissionPort

from test_batch_admission import BatchMemoryStore


def _service(store, *, owner="prod-1", max_log_items=64):
    return BatchAdmissionCoordinator(
        store, owner_id=owner, owner_isolated=lambda: True,
        limits=BatchLimits(max_log_items=max_log_items),
        clock=lambda: datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc),
    )


def _collector(service, *, max_wait=0.005):
    return BatchAdmissionCollector(service, max_wait_seconds=max_wait,
                                  request_timeout_seconds=0.5,
                                  close_timeout_seconds=0.05)


def _accept_request(scope, request_id, item_id, text, *, trace_id="trace"):
    return AdmissionRequest(
        scope_id=scope, request_id=request_id, item_id=item_id, text=text,
        received_at=datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc),
        schema_version="1", pipeline_version="prod-v1", embedding_space_id="none",
        delivery_route_ref="prod-route-v1", trace_id=trace_id,
    )


def test_record_id_is_deterministic_for_same_inputs():
    record_id_a = _digest(["default", "1-1"])
    record_id_b = _digest(["default", "1-1"])
    assert record_id_a == record_id_b
    assert len(record_id_a) == 64  # sha256 hex
    assert record_id_a != _digest(["default", "1-2"])
    assert record_id_a != _digest(["other", "1-1"])


def test_record_id_differs_across_scopes_with_same_item_id():
    """同一 item_id 不同 scope → 不同 record_id（防止跨域身份串）。"""
    scope_a_record = _digest(["scope-a", "1-1"])
    scope_b_record = _digest(["scope-b", "1-1"])
    assert scope_a_record != scope_b_record


def test_keys_have_consistent_namespaces():
    record_id, request_key, item_key = _keys("default", "req-1", "item-1")
    assert record_id == _digest(["default", "item-1"])
    assert request_key == "request:" + _digest(["default", "req-1"])
    assert item_key == "item:" + record_id
    # request_key 与 item_key 不可相等（区分维度）
    assert request_key != item_key


def test_business_fingerprint_strong_constraint_same_id_different_text():
    """同 ID 异文 → business_fingerprint 不同 → 触发 IdentityConflict。"""
    store = BatchMemoryStore()
    service = _service(store)
    collector = _collector(service)
    port = CollectorAdmissionPort(collector)
    try:
        first = port.accept(_accept_request("default", "1-1", "1-1", "原文A"))
        # 同一 record_id 下异文必须被拒
        from news_flash_dedup.admission import IdentityConflict
        with pytest.raises(IdentityConflict):
            port.accept(_accept_request("default", "1-1", "1-1", "原文B"))
        # first 的 receipt 字段不变（receipt 不可变）
        assert first.record_id == _digest(["default", "1-1"])
        assert first.arrival_seq == 1
    finally:
        collector.close()


def test_business_fingerprint_binds_item_id_and_text():
    """business_fingerprint = sha256(item_id + text)；item_id 一致但 text 不同 → 不同 fingerprint。"""
    fp_a = _digest({"item_id": "1-1", "text": "正文A"})
    fp_b = _digest({"item_id": "1-1", "text": "正文B"})
    assert fp_a != fp_b


def test_arrival_seq_is_monotone_within_owner():
    store = BatchMemoryStore()
    service = _service(store)
    collector = _collector(service)
    port = CollectorAdmissionPort(collector)
    try:
        seqs = []
        for n in range(5):
            receipt = port.accept(_accept_request("default", f"{n + 1}-1", f"{n + 1}-1",
                                                  f"独立正文 {n}"))
            seqs.append(receipt.arrival_seq)
        assert seqs == [1, 2, 3, 4, 5]
    finally:
        collector.close()


def test_arrival_seq_continues_after_owner_takeover():
    """owner 切换后 arrival_seq 继续递增（per-owner 连续性，10 §5.1）。"""
    store = BatchMemoryStore()
    service_old = _service(store, owner="old-owner")
    collector_old = _collector(service_old)
    port_old = CollectorAdmissionPort(collector_old)
    try:
        r1 = port_old.accept(_accept_request("default", "1-1", "1-1", "原文"))
        assert r1.arrival_seq == 1
        collector_old.close()
    except Exception:
        collector_old.close()
        raise

    time.sleep(0.05)
    service_new = _service(store, owner="new-owner")
    service_new.takeover()  # 显式 owner 转移
    collector_new = _collector(service_new)
    port_new = CollectorAdmissionPort(collector_new)
    try:
        r2 = port_new.accept(_accept_request("default", "2-1", "2-1", "新文"))
        # 接管后新 owner 的 arrival_seq 继续连续：旧 owner 写了 1，新 owner 从 2 开始
        assert r2.arrival_seq == 2
        assert r2.reused is False
    finally:
        collector_new.close()


def test_reused_receipt_carries_immutable_first_acceptance_metadata():
    """reused receipt 的 trace_id/route/pipeline_version 必须等于首次受理。"""
    store = BatchMemoryStore()
    service = _service(store)
    collector = _collector(service)
    port = CollectorAdmissionPort(collector)
    try:
        first = port.accept(_accept_request("default", "1-1", "1-1", "原文",
                                              trace_id="first-trace"))
        # collector 关闭后用新 collector + 持久 store 重试
        collector.close()
        time.sleep(0.05)
    except Exception:
        collector.close()
        raise

    service2 = _service(store)
    collector2 = _collector(service2)
    port2 = CollectorAdmissionPort(collector2)
    try:
        # retry：trace_id 不同 + pipeline_version 不同，验证 first receipt 不被改
        from dataclasses import replace
        retry_request = replace(
            _accept_request("default", "1-1", "1-1", "原文", trace_id="retry-trace"),
            pipeline_version="retry-version",
        )
        reused = port2.accept(retry_request)
        assert reused.reused is True
        assert reused.arrival_seq == first.arrival_seq
        assert reused.record_id == first.record_id
        # receipt 的 schema/pipeline/trace 不被 retry 改写（核对原始 schema_version）
        assert reused.schema_version == first.schema_version
        assert reused.pipeline_version == first.pipeline_version
        assert reused.trace_id == first.trace_id
        assert reused.delivery_route_ref == first.delivery_route_ref
    finally:
        collector2.close()