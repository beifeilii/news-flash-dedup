# -*- coding: utf-8 -*-
"""修复波 2·A1-004/A1-010 reused receipt 构造等价钉（__dict__→dataclasses.replace）。

实现窗登记：设计稿字段表含 "received_at"——AdmissionReceipt 无此字段
（admission.py:65-79 十四字段亲读），按设计语义钉"同 ID 重试逐字段等价"
剔除该键（其余八键不动）。
"""
from __future__ import annotations

from test_admission import MemoryStore, coordinator, request
from test_batch_admission import BatchMemoryStore
from test_batch_admission import coordinator as batch_coordinator

_FIELDS = ("scope_id", "request_id", "item_id", "record_id", "business_date",
           "arrival_seq", "accepted_at", "expires_at")


def test_single_slot_reused_receipt_fieldwise_equal():
    store = MemoryStore()
    service = coordinator(store)
    first = service.accept(request())
    again = service.accept(request())                     # 同 ID 重试
    assert again.reused is True
    for field in _FIELDS:
        assert getattr(again, field) == getattr(first, field), field


def test_batch_reused_receipt_fieldwise_equal():
    store = BatchMemoryStore()
    service = batch_coordinator(store)
    first = service.accept_batch([request()])[0]
    again = service.accept_batch([request()])[0]          # pending 命中→reused
    assert again.reused is True
    for field in _FIELDS:
        assert getattr(again, field) == getattr(first, field), field
