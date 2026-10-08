"""P12 测试 Milvus 端点硬护栏与隔离资源命名。"""

from __future__ import annotations

import pytest

from news_flash_dedup.milvus_client import (
    ProductionMilvusAccessDenied,
    assert_test_milvus_environment,
    build_test_milvus_client,
    collection_name_for_test,
    partition_name,
)


ENV = {
    "DEPLOY_ENV": "test",
    "TEST_MILVUS_HOST": "c-75641abe71c37c6f-internal.milvus.aliyuncs.com",
    "TEST_MILVUS_PORT": "19530",
    "TEST_MILVUS_USER": "root",
    "TEST_MILVUS_PASS": "sentinel-only",
}


def test_exact_test_endpoint_is_accepted_and_factory_uses_only_test_values():
    assert_test_milvus_environment(ENV)
    calls = []

    class FakeClient:
        def __init__(self, **kwargs):
            calls.append(kwargs)

    build_test_milvus_client(ENV, client_class=FakeClient)
    # D9★3（T7）迁移：user/password 分离 → token 组合（A1 预检双形式实测可用，
    # pymilvus 推荐统一入口；迁移记录见 目标文档.md D9 与 T7 关账纪要）
    assert calls == [{
        "uri": "http://c-75641abe71c37c6f-internal.milvus.aliyuncs.com:19530",
        "token": "root:sentinel-only", "timeout": 5,
    }]


@pytest.mark.parametrize("change", [
    {"DEPLOY_ENV": "prod"},
    {"TEST_MILVUS_HOST": "other.example.com"},
    {"TEST_MILVUS_PORT": "19531"},
    {"TEST_MILVUS_PASS": ""},
    {"PROD_MILVUS_HOST": "production.example.com"},
    {"ALLOW_PROD_WRITE": "true"},
])
def test_production_or_unapproved_endpoint_fails_before_client_construction(change):
    env = {**ENV, **change}
    with pytest.raises(ProductionMilvusAccessDenied):
        assert_test_milvus_environment(env)


def test_collection_and_partition_names_are_validated_not_from_user_text():
    assert collection_name_for_test("abc123", "s_deadbeef") == \
        "news_dedup_replay_abc123_s_deadbeef"
    assert partition_name("2026-09-26") == "bd_20260926"
    with pytest.raises(ValueError):
        collection_name_for_test("../other", "s_deadbeef")
    with pytest.raises(ValueError):
        partition_name("2026-13-99")
