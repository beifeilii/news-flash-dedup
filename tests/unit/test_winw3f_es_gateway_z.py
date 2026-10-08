# -*- coding: utf-8 -*-
"""W3F 红测（W3c-F-1）：es_gateway.py:41 replace("Z","+00:00") 定点化。

缺陷面：candidate_eligible 对 expires_at 做 `replace("Z", "+00:00")` 全域
替换——仅**尾位** Z 是时区指示符，串内非尾位 Z 一并被误改写（同类不同步：
vector_store.py:147 已在 W2Fβ 条66 定点化为 endswith("Z") 单尾替换，本点
漏网）。探针（log\\temp\\winw3f-f1-z-probe.txt）：12 候选串新旧变换式
outcome 全同——行为等价 fail-closed（畸形串两式同被 fromisoformat 拒绝，
W3c F-1 裁定在案）；故红钉落源码变换式面，等价电池前后同绿守卫。
"""

from __future__ import annotations

import inspect
from datetime import datetime, timezone

import pytest

from news_flash_dedup.recall.es_gateway import candidate_eligible
from news_flash_dedup.recall.models import RecallRequest

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _request() -> RecallRequest:
    return RecallRequest(scope_id="s", business_date="2026-09-29",
                         record_id="r" * 64, item_id="item-a",
                         arrival_seq=10, text="正文")


def _source(expiry) -> dict:
    return {"scope_id": "s", "business_date": "2026-09-29",
            "record_id": "c" * 64, "item_id": "item-b",
            "arrival_seq": 5, "expires_at": expiry}


# ---------- 红测（源码面机制钉：全域 replace 未定点化即红） ----------

def test_f1_replace_z_pinpointed_to_tail_only():
    """机制钉：candidate_eligible 不再以全域 replace("Z") 改写 expires_at
    ——仅尾 Z 替换（endswith("Z") 定点式，对齐 vector_store.py:147 修法）。"""
    src = inspect.getsource(candidate_eligible)
    code_lines = [line.strip() for line in src.splitlines()
                  if not line.strip().startswith("#")]
    assert not any('.replace("Z"' in line for line in code_lines), \
        "全域 replace(\"Z\") 仍在（串内非尾位 Z 被误改写面未收口）"
    assert any('endswith("Z")' in line for line in code_lines), \
        "未见 endswith(\"Z\") 定点式（vector_store.py:147 同法未对齐）"


# ---------- 前后均绿守卫（行为等价电池，探针 outcome 全同在案） ----------

def test_f1_tail_z_future_eligible():
    """守卫：尾 Z aware 未来时——合格（现役契约不动）。"""
    assert candidate_eligible(
        _source("2099-01-01T00:00:00Z"), _request(), NOW) is True


def test_f1_tail_z_past_not_eligible():
    """守卫：尾 Z aware 过去时——不合格（expiry 执法不动）。"""
    assert candidate_eligible(
        _source("2020-01-01T00:00:00Z"), _request(), NOW) is False


def test_f1_numeric_offset_still_accepted():
    """守卫：±HH:MM 数值偏移 aware 串照常解析（非 Z 路径不动）。"""
    assert candidate_eligible(
        _source("2099-01-01T08:00:00+08:00"), _request(), NOW) is True


def test_f1_naive_expiry_still_rejected():
    """守卫：naive 串（无时区）→ ValueError（fail-closed 不动）。"""
    with pytest.raises(ValueError, match="no timezone"):
        candidate_eligible(_source("2099-01-01T00:00:00"), _request(), NOW)


def test_f1_non_tail_z_still_rejected_fail_closed():
    """守卫（"串内含 Z 非尾位不替换"等价钉）：非尾位含 Z 畸形串两式同被
    fromisoformat 拒绝 → ValueError（fail-closed 不变；探针 12 串
    outcome 全同，winw3f-f1-z-probe.txt 在案）。"""
    for bad in ("2099-01-01T0Z0:00:00", "2099-01-01T00:00:00+0Z0:00",
                "2099-01-01T00:00:00ZZ", "Z2099-01-01T00:00:00"):
        with pytest.raises(ValueError):
            candidate_eligible(_source(bad), _request(), NOW)


def test_f1_missing_expiry_still_rejected():
    """守卫：expires_at 缺失/非 str → ValueError（现役闸不动）。"""
    with pytest.raises(ValueError, match="expiry is missing"):
        candidate_eligible(_source(None), _request(), NOW)
