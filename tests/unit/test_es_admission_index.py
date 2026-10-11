from __future__ import annotations

from datetime import date, timedelta

import pytest

from news_flash_dedup.es_admission_index import (
    provision_isolated_indices,
    required_indices,
)
from news_flash_dedup.es_admission_schema import day_index


def test_required_indices_contains_eight_indices():
    business = date(2026, 9, 24)
    indices = required_indices(business)
    # control + request + 2 days × (items + audits + work) = 8
    # P1a-T1：work 任务索引进 required（蓝图 §七-2 裁定——受理主路径）。
    assert len(indices) == 8


def test_required_indices_includes_control_and_request():
    business = date(2026, 9, 24)
    indices = required_indices(business)
    assert "news-dedup-control-v1" in indices
    assert "news-dedup-requests-v1" in indices


def test_required_indices_includes_two_days_of_items_and_audits():
    business = date(2026, 9, 24)
    indices = required_indices(business)
    assert day_index(business.isoformat()) in indices
    # 窗口 Z3 恒死分支清理：旧表达式 `... if False else "2026-09-25"` 恒取字面
    # 量分支（前支永不求值），改为由 business 真实推导次日索引名
    # （2026-09-24 + 1d = 2026-09-25，与旧恒取分支同值，语义不变但不再有死码）
    assert day_index((business + timedelta(days=1)).isoformat()) in indices
    assert day_index(business.isoformat(), "audits") in indices
    assert day_index("2026-09-25", "audits") in indices


def test_required_indices_includes_two_days_of_work():
    """P1a-T1：work 任务队列索引两日窗与 items 同梯（受理主路径）。"""
    business = date(2026, 9, 24)
    indices = required_indices(business)
    assert day_index(business.isoformat(), "work") in indices
    assert day_index((business + timedelta(days=1)).isoformat(), "work") in indices
    work_props = indices[day_index(business.isoformat(), "work")][
        "mappings"]["properties"]
    assert work_props["task_state"]["type"] == "keyword"
    assert work_props["text"]["index"] is False
    assert work_props["arrival_seq"]["type"] == "long"


def test_required_indices_set_one_shard_no_replica():
    business = date(2026, 9, 24)
    indices = required_indices(business)
    for mapping in indices.values():
        settings = mapping["settings"]
        assert settings["number_of_shards"] == 1
        assert settings["number_of_replicas"] == 0


def test_required_indices_uses_canonical_field_types():
    business = date(2026, 9, 24)
    indices = required_indices(business)
    assert indices["news-dedup-control-v1"]["mappings"]["properties"]["owner_id"]["type"] == "keyword"
    audit_props = indices[day_index(business.isoformat(), "audits")]["mappings"]["properties"]
    assert audit_props["audit_type"]["type"] == "keyword"
    assert audit_props["payload"]["enabled"] is False
    item_props = indices[day_index(business.isoformat())]["mappings"]["properties"]
    assert item_props["text"]["analyzer"] == "dedup_cjk"
    assert item_props["result"]["properties"]["decision"]["type"] == "keyword"


# ---------- D25：provision_isolated_indices 真实驱动（原 Mapping 隐雷区） ----------

class _FakeIndicesApi:
    """dict-fake（模拟 ES8 .body 已展开的 dict 路径）。"""

    def __init__(self, existing=None):
        self._existing = dict(existing or {})
        self.created = []

    def get(self, *, index, allow_no_indices, ignore_unavailable):
        return {n: {} for n in self._existing if n.startswith(index.rstrip("*"))}

    def create(self, *, index, body):
        self.created.append(index)
        return {"acknowledged": True, "shards_acknowledged": True}

    def get_settings(self, *, index):
        return {
            name: {"settings": {"index": {"number_of_shards": "1",
                                           "number_of_replicas": "0"}}}
            for name in index.split(",")
        }


class _FakeResponseClient:
    """ObjectApiResponse 形态：响应包在 .body 里（C-03 回退路径）。"""

    class _Resp:
        def __init__(self, body):
            self.body = body

    def __init__(self, existing=None):
        self.indices = self
        self._existing = dict(existing or {})
        self.created = []

    def get(self, *, index, allow_no_indices, ignore_unavailable):
        return self._Resp(
            {n: {} for n in self._existing if n.startswith(index.rstrip("*"))})

    def create(self, *, index, body):
        self.created.append(index)
        return self._Resp({"acknowledged": True,
                            "shards_acknowledged": True})

    def get_settings(self, *, index):
        return {
            name: {"settings": {"index": {"number_of_shards": "1",
                                           "number_of_replicas": "0"}}}
            for name in index.split(",")
        }


class _FakeClient:
    def __init__(self, indices_api):
        self.indices = indices_api


def test_provision_creates_eight_indices_and_topology():
    """D25：fresh namespace 下全函数真实驱动（原 Mapping NameError 隐雷区）。

    现契约钉值（09-28 用户授权四位一体裁定）：create 用 index_prefix+逻辑名
    带前缀形——建/闸/访/清同口径（与 batch_es_store 强制前缀、bench/probe
    两原件、lifecycle 双兼容正则一致；旧"canonical 名+命名归属待裁定"钉值
    随裁定落地作废），本测试钉住该形态防静默漂移。P1a-T1：8 索引（+两日
    work）。"""
    api = _FakeIndicesApi()
    result = provision_isolated_indices(
        _FakeClient(api), "p01-batch-test1-", business_day=date(2026, 9, 24))
    expected = set(required_indices(date(2026, 9, 24), index_prefix="p01-batch-test1-").keys())
    assert set(api.created) == expected
    assert len(api.created) == 8
    assert len(result["topology"]) == 8
    assert result["business_day"] == "2026-09-24"


def test_provision_rejects_existing_namespace():
    """D25：命名空间已存在必须拒绝（fresh-namespace 闸）。"""
    api = _FakeIndicesApi(
        existing={"p01-batch-test2-news-dedup-items-v1-2026.09.24": {}})
    with pytest.raises(RuntimeError, match="fresh run-id"):
        provision_isolated_indices(
            _FakeClient(api), "p01-batch-test2-",
            business_day=date(2026, 9, 24))
    assert api.created == []


def test_provision_response_wrapped_client_uses_body_fallback():
    """D25（C-03）：ObjectApiResponse 形态客户端——.body 回退必须让
    fresh-namespace 闸真看到既有索引（原仅认 Mapping 恒判空静默失效）。"""
    client = _FakeResponseClient(
        existing={"p01-batch-test3-news-dedup-items-v1-2026.09.24": {}})
    with pytest.raises(RuntimeError, match="fresh run-id"):
        provision_isolated_indices(
            client, "p01-batch-test3-", business_day=date(2026, 9, 24))
    assert client.created == []


def test_provision_rejects_non_isolated_prefix():
    with pytest.raises(ValueError, match="isolated prefix"):
        provision_isolated_indices(
            _FakeClient(_FakeIndicesApi()), "news-dedup-",
            business_day=date(2026, 9, 24))