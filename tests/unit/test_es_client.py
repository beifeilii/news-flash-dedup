from __future__ import annotations

import importlib

import pytest

from news_flash_dedup import es_client
from news_flash_dedup.es_client import (
    APPROVED_UAT_HOST,
    APPROVED_UAT_PORT,
    APPROVED_UAT_SCHEME,
    ProductionAccessDenied,
    assert_test_environment,
)


@pytest.fixture
def uat_env(monkeypatch):
    """最小可用 UAT 环境变量；不含 ALLOW_PROD_WRITE 等可选字段。"""
    env = {
        "TEST_ES_HOST": APPROVED_UAT_HOST,
        "TEST_ES_PORT": str(APPROVED_UAT_PORT),
        "TEST_ES_SCHEME": APPROVED_UAT_SCHEME,
        "TEST_ES_USER": "uat_user",
        "TEST_ES_PASS": "uat_pass",
    }
    for key in list(os_environ()):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("DEPLOY_ENV", "test")
    return env


def os_environ():
    from os import environ
    return list(environ.keys())


def test_assert_test_environment_accepts_uat(monkeypatch, uat_env):
    for key in list(os_environ()):
        monkeypatch.delenv(key, raising=False)
    for key, value in uat_env.items():
        monkeypatch.setenv(key, value)
    # 窗口 Z3 伪断言修复：旧 `assert_test_environment() is None` 为裸比较表达式
    # （求值即弃，永不失败）；补 assert 使返回值判定真实生效
    assert assert_test_environment() is None


def test_assert_test_environment_rejects_prod_env(monkeypatch, uat_env):
    for key in list(os_environ()):
        monkeypatch.delenv(key, raising=False)
    for key, value in uat_env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("DEPLOY_ENV", "prod")
    with pytest.raises(ProductionAccessDenied):
        assert_test_environment()


def test_assert_test_environment_rejects_prod_host(monkeypatch, uat_env):
    for key in list(os_environ()):
        monkeypatch.delenv(key, raising=False)
    for key, value in uat_env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("PROD_ES_HOST", "es-prod.example.com")
    with pytest.raises(ProductionAccessDenied):
        assert_test_environment()


@pytest.mark.parametrize("value", ["true", "1", "yes", "True", "YES"])
def test_assert_test_environment_rejects_allow_prod_write(monkeypatch, uat_env, value):
    for key in list(os_environ()):
        monkeypatch.delenv(key, raising=False)
    for key, value_ in uat_env.items():
        monkeypatch.setenv(key, value_)
    monkeypatch.setenv("ALLOW_PROD_WRITE", value)
    with pytest.raises(ProductionAccessDenied):
        assert_test_environment()


@pytest.mark.parametrize("allow_value", ["false", "0", "no", "False", ""])
def test_assert_test_environment_accepts_inert_allow_prod_write(monkeypatch, uat_env, allow_value):
    for key in list(os_environ()):
        monkeypatch.delenv(key, raising=False)
    for key, value in uat_env.items():
        monkeypatch.setenv(key, value)
    if allow_value:
        monkeypatch.setenv("ALLOW_PROD_WRITE", allow_value)
    else:
        monkeypatch.delenv("ALLOW_PROD_WRITE", raising=False)
    # 窗口 Z3 伪断言修复：同上（裸比较表达式补 assert）
    assert assert_test_environment() is None


@pytest.mark.parametrize("host, port, scheme", [
    ("es-other.aliyuncs.com", "9200", "http"),
    ("elasticsearch.aliyuncs.com", "9201", "http"),
    ("elasticsearch.aliyuncs.com", "9200", "https"),
    ("", "9200", "http"),
])
def test_assert_test_environment_rejects_wrong_endpoint(monkeypatch, uat_env, host, port, scheme):
    for key in list(os_environ()):
        monkeypatch.delenv(key, raising=False)
    for key, value in uat_env.items():
        monkeypatch.setenv(key, value)
    if host:
        monkeypatch.setenv("TEST_ES_HOST", host)
    else:
        monkeypatch.delenv("TEST_ES_HOST", raising=False)
    monkeypatch.setenv("TEST_ES_PORT", port)
    monkeypatch.setenv("TEST_ES_SCHEME", scheme)
    with pytest.raises(ProductionAccessDenied):
        assert_test_environment()


def test_assert_test_environment_requires_test_es_host(monkeypatch):
    for key in list(os_environ()):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("DEPLOY_ENV", "test")
    with pytest.raises(ProductionAccessDenied):
        assert_test_environment()


def test_build_uat_client_runs_assertion(monkeypatch, uat_env):
    """即使不连真实 ES，build_uat_client 也必须先过 assert_test_environment。"""
    for key in list(os_environ()):
        monkeypatch.delenv(key, raising=False)
    for key, value in uat_env.items():
        monkeypatch.setenv(key, value)
    # 三方审计 V2 实证修复：原断言 isinstance(client, object) 恒真空转；
    # 改为哨兵实例同一性断言（build 未返回构造产物即红）。
    stub = object()
    monkeypatch.setattr("elasticsearch.Elasticsearch", lambda *a, **kw: stub)
    client = es_client.build_uat_client()
    assert client is stub


def test_prod_alone_is_rejected_even_without_prod_host(monkeypatch, uat_env):
    for key in list(os_environ()):
        monkeypatch.delenv(key, raising=False)
    for key, value in uat_env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("PROD_ES_HOST", raising=False)
    monkeypatch.delenv("ALLOW_PROD_WRITE", raising=False)
    monkeypatch.setenv("DEPLOY_ENV", "prod")
    with pytest.raises(ProductionAccessDenied):
        assert_test_environment()