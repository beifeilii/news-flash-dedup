"""产品化 Elasticsearch 8.x 客户端工厂。

P07-B 真实 ES 适配（环境边界：§10）：
- 唯一允许的连接端点是受批准的 UAT ES 集群（`TEST_ES_*` 环境变量）。
- 生产环境（`DEPLOY_ENV=prod`、`PROD_ES_*`、或 `ALLOW_PROD_WRITE=true`）启动时 fail-fast 拒绝，
  不允许任何连接、探针、读写；正式路径待生产权限开通后再启用。
- 工厂方法只创建连接，不发起任何业务请求；调用方按业务路径使用。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

APPROVED_UAT_HOST = "es-cn-9fr4srbma0001lus6.elasticsearch.aliyuncs.com"
APPROVED_UAT_PORT = 9200
APPROVED_UAT_SCHEME = "http"


class ProductionAccessDenied(RuntimeError):
    """生产环境访问被拒绝；详见 P07-接入实施设计.md §10。"""


def load_environment(dotenv_path: Path | None = None) -> None:
    """从 .env 加载变量；缺省路径 = 项目根 .env。"""
    from dotenv import load_dotenv

    path = dotenv_path or Path(__file__).resolve().parents[2] / ".env"
    if path.exists():
        load_dotenv(path)


def assert_test_environment(env: dict[str, str] | None = None) -> None:
    """校验当前为开发/UAT 环境；任何生产迹象都拒绝启动。

    检查项（窗口W2Fβ，WA3b-M3 条65 起向 milvus_client 闸收拢 fail-closed）：
    1. `DEPLOY_ENV` 必须为精确 `test`（缺省按 `test` 容忍——与 milvus_client
       缺省即拒的已知残余差异：tests/unit/test_es_client.py 既有钉
       L40-47/L81-92/L124-135 清环境后断言缺省放行，禁碰既有测试→
       缺省即拒半项挂账呈主窗口裁定）；
    2. 任何 `PROD_ES_` 前缀键必须未设置（前缀全拒含空串——旧单键
       PROD_ES_HOST 查，PORT/USER/PASS/SCHEME 漏网）；
    3. `ALLOW_PROD_WRITE` 必须未设置或为 `false`/`0`/`no`；
    4. `TEST_ES_HOST/PORT/SCHEME` 必须指向受批准的 UAT ES 集群。
    """
    env = env if env is not None else os.environ
    deploy_env = env.get("DEPLOY_ENV", "test")
    if deploy_env != "test":
        raise ProductionAccessDenied(
            "DEPLOY_ENV must be 'test' during P07 development; got "
            + repr(deploy_env),
        )
    prod_keys = sorted(key for key in env if key.startswith("PROD_ES_"))
    if prod_keys:
        raise ProductionAccessDenied(
            "PROD_ES_* must remain unset during P07; got " + ", ".join(prod_keys),
        )
    allowed = env.get("ALLOW_PROD_WRITE", "").strip().lower()
    if allowed and allowed not in {"false", "0", "no"}:
        raise ProductionAccessDenied(
            "ALLOW_PROD_WRITE must be unset/false/0/no during P07; got " + repr(allowed),
        )
    host = env.get("TEST_ES_HOST")
    if not host:
        raise ProductionAccessDenied("TEST_ES_HOST is required")
    try:
        # 窗口W2Fβ（WA3b 条66，原 L62）：非数值端口原以裸 ValueError 逃逸，
        # 统一包领域异常（fail-closed，milvus_client.py L27-35 同款）。
        port = int(env.get("TEST_ES_PORT", str(APPROVED_UAT_PORT)))
    except (TypeError, ValueError) as error:
        raise ProductionAccessDenied("TEST_ES_PORT is invalid") from error
    scheme = env.get("TEST_ES_SCHEME", APPROVED_UAT_SCHEME).lower()
    if (host, port, scheme) != (APPROVED_UAT_HOST, APPROVED_UAT_PORT, APPROVED_UAT_SCHEME):
        raise ProductionAccessDenied(
            "TEST_ES_* must point to the approved UAT ES "
            f"({APPROVED_UAT_SCHEME}://{APPROVED_UAT_HOST}:{APPROVED_UAT_PORT}); "
            f"got {scheme}://{host}:{port}",
        )


def build_uat_client(env: dict[str, str] | None = None,
                     *, request_timeout: float = 5, max_retries: int = 0,
                     retry_on_timeout: bool = False) -> Any:
    """创建指向 UAT ES 8.x 的 elasticsearch.Elasticsearch 客户端；调用前已通过 assert_test_environment。"""
    assert_test_environment(env)
    source = env if env is not None else os.environ
    try:
        from elasticsearch import Elasticsearch
    except ImportError as error:
        raise RuntimeError("elasticsearch package is required") from error
    host = source["TEST_ES_HOST"]
    port = int(source.get("TEST_ES_PORT", str(APPROVED_UAT_PORT)))
    scheme = source.get("TEST_ES_SCHEME", APPROVED_UAT_SCHEME).lower()
    user = source.get("TEST_ES_USER")
    password = source.get("TEST_ES_PASS")
    basic_auth = (user, password) if user and password else None
    return Elasticsearch(
        f"{scheme}://{host}:{port}",
        basic_auth=basic_auth,
        request_timeout=request_timeout,
        max_retries=max_retries,
        retry_on_timeout=retry_on_timeout,
    )


def client_from_environment(load_dotenv_first: bool = True) -> Any:
    """从环境变量构建客户端的便捷入口。

    - 若 `load_dotenv_first` 为真，先尝试加载项目根 .env（路径固定）；
    - 通过 `assert_test_environment` fail-fast 检查；
    - 返回 elasticsearch.Elasticsearch 客户端；调用方按业务路径使用。
    """
    if load_dotenv_first:
        load_environment()
    return build_uat_client()
