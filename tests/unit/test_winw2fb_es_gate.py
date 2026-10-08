# -*- coding: utf-8 -*-
"""窗口W2Fβ 条65（WA3b-M3）+条66 es_client 端口：ES 生产闸收拢 fail-closed 钉。

条65：es_client.assert_test_environment 向 milvus_client 闸收拢——
① DEPLOY_ENV 精确 `test`（大小写/空白变体不再静默归一；缺省即拒半项
   未修：tests/unit/test_es_client.py 既有钉 L40-47/L81-92/L124-135 清环境后
   断言缺省放行，与本半项正面冲突，禁碰既有测试 → 挂账呈主窗口，
   缺省容忍残余在本文件钉为已知行为防伪绿）；
② PROD_ES_ 前缀全拒（旧：单键 PROD_ES_HOST 查，PROD_ES_PORT/USER 等漏网）。
条66（es_client L62）：TEST_ES_PORT 非数值原以裸 ValueError 逃逸 → 包
ProductionAccessDenied（fail-closed，milvus_client L27-35 同款）。
"""
from __future__ import annotations

import pytest

from news_flash_dedup.es_client import (
    ProductionAccessDenied,
    assert_test_environment,
)


def _uat_env(**overrides):
    env = {
        "DEPLOY_ENV": "test",
        "TEST_ES_HOST": "es-cn-9fr4srbma0001lus6.elasticsearch.aliyuncs.com",
        "TEST_ES_PORT": "9200",
        "TEST_ES_SCHEME": "http",
    }
    env.update(overrides)
    return env


def test_missing_deploy_env_remains_tolerated_documented_residual():
    """残余钉（条65① 未修半项）：缺省仍按 test 容忍——钉为已知行为防伪绿。

    缺省即拒与既有钉正面冲突（见模块 docstring）；若主窗口裁定改钉落地，
    本测试应翻转为断言 ProductionAccessDenied。
    """
    env = _uat_env()
    del env["DEPLOY_ENV"]
    assert assert_test_environment(env) is None


@pytest.mark.parametrize("value", ["prod", "TEST", " test", "test "])
def test_non_exact_test_deploy_env_is_denied(value):
    """红能力（条65① 已修半项）：非精确 "test" 一律拒（大小写/空白不归一）。"""
    with pytest.raises(ProductionAccessDenied):
        assert_test_environment(_uat_env(DEPLOY_ENV=value))


@pytest.mark.parametrize("key", ["PROD_ES_HOST", "PROD_ES_PORT",
                                 "PROD_ES_USER", "PROD_ES_PASS",
                                 "PROD_ES_SCHEME"])
def test_any_prod_es_prefixed_key_is_denied(key):
    """红能力（条65②）：PROD_ES_ 前缀全拒（含空串），不再只查 HOST 单键。"""
    with pytest.raises(ProductionAccessDenied):
        assert_test_environment(_uat_env(**{key: ""}))


def test_bad_test_es_port_is_wrapped_as_domain_error():
    """红能力（条66）：TEST_ES_PORT 非数值 → ProductionAccessDenied，非裸 ValueError。"""
    with pytest.raises(ProductionAccessDenied):
        assert_test_environment(_uat_env(TEST_ES_PORT="not-a-port"))
    with pytest.raises(ProductionAccessDenied):
        assert_test_environment(_uat_env(TEST_ES_PORT=None))


def test_wellformed_uat_env_still_accepted():
    """绿守卫：显式 DEPLOY_ENV=test + UAT 端点照常放行。"""
    assert assert_test_environment(_uat_env()) is None
    assert assert_test_environment(
        _uat_env(ALLOW_PROD_WRITE="false")) is None


def test_wrong_endpoint_and_prod_write_still_denied():
    """绿守卫：端点偏移/生产写旗标两闸不回归。"""
    with pytest.raises(ProductionAccessDenied):
        assert_test_environment(_uat_env(TEST_ES_HOST="other.example.com"))
    with pytest.raises(ProductionAccessDenied):
        assert_test_environment(_uat_env(TEST_ES_PORT="9201"))
    with pytest.raises(ProductionAccessDenied):
        assert_test_environment(_uat_env(ALLOW_PROD_WRITE="true"))
