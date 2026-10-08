"""P12 测试 Milvus 客户端；正式连接在开发期硬拒绝。"""

from __future__ import annotations

import os
import re
from datetime import date
from typing import Any


APPROVED_TEST_MILVUS_HOST = "c-75641abe71c37c6f-internal.milvus.aliyuncs.com"
APPROVED_TEST_MILVUS_PORT = 19530


class ProductionMilvusAccessDenied(RuntimeError):
    pass


def assert_test_milvus_environment(env: dict[str, str] | None = None) -> None:
    source = env if env is not None else os.environ
    if source.get("DEPLOY_ENV") != "test":
        raise ProductionMilvusAccessDenied("DEPLOY_ENV must be test for P12")
    if any(key.startswith("PROD_MILVUS_") for key in source):
        raise ProductionMilvusAccessDenied("production Milvus configuration is forbidden")
    if source.get("ALLOW_PROD_WRITE", "").strip().lower() not in ("", "false", "0", "no"):
        raise ProductionMilvusAccessDenied("production write flag is forbidden")
    try:
        port = int(source.get("TEST_MILVUS_PORT", ""))
    except (TypeError, ValueError) as error:
        # M-12/L 窗口P：非数值可解析值（如 None）原以 TypeError 逃逸，
        # 统一包装为领域异常（fail-closed）。
        # 窗口V：from None → from error——原 `from None` 断链逆 P 批 15 处
        # 保链纪律（batch_admission 等一致 `from error`）；本处原异常即
        # 失败根因（int 解析 TypeError/ValueError），无须断链，入 __cause__。
        raise ProductionMilvusAccessDenied("test Milvus port is invalid") from error
    if (source.get("TEST_MILVUS_HOST"), port) != (
        APPROVED_TEST_MILVUS_HOST, APPROVED_TEST_MILVUS_PORT
    ):
        raise ProductionMilvusAccessDenied("Milvus endpoint is not the approved test cluster")
    if not source.get("TEST_MILVUS_USER") or not source.get("TEST_MILVUS_PASS"):
        raise ProductionMilvusAccessDenied("test Milvus credentials are incomplete")


def build_test_milvus_client(env: dict[str, str] | None = None,
                             *, client_class: Any = None) -> Any:
    assert_test_milvus_environment(env)
    source = env if env is not None else os.environ
    if client_class is None:
        from pymilvus import MilvusClient
        client_class = MilvusClient
    # D9★3（T7 执行）：token 组合形式替代 user/password 分离传参——A1 预检已
    # 对真集群双形式实测可用；token 为 pymilvus 推荐统一入口（P19-UAT 接线期
    # 双形式均验证，token 入选）。已封 test_milvus_client 断言同步迁移。
    return client_class(
        uri=f"http://{APPROVED_TEST_MILVUS_HOST}:{APPROVED_TEST_MILVUS_PORT}",
        token=f"{source['TEST_MILVUS_USER']}:{source['TEST_MILVUS_PASS']}",
        timeout=5,
    )


def collection_name_for_test(run_id: str, space_id: str) -> str:
    if not re.fullmatch(r"[a-z0-9]{6,16}", run_id):
        raise ValueError("run_id must be a generated lowercase token")
    # 窗口W2Fβ（WA3b 条66，原 L64）：space_id 字符集收紧 [a-z0-9]→hex——
    # 实物 space_id = "s_" + sha256[:40]（recall.vector_space.space_id）；
    # 长度窗口保留（既有钉 test_milvus_client.py 用 s_deadbeef 短形在案，
    # 禁碰既有测试）；全等 40hex 闸在 milvus_store.build_p19_collection_name。
    if not re.fullmatch(r"s_[0-9a-f]{4,60}", space_id):
        raise ValueError("embedding_space_id is invalid")
    return f"news_dedup_replay_{run_id}_{space_id}"


def partition_name(business_date: str) -> str:
    try:
        if date.fromisoformat(business_date).isoformat() != business_date:
            raise ValueError("noncanonical date")
    except (TypeError, ValueError) as error:
        raise ValueError("business_date must be YYYY-MM-DD") from error
    return "bd_" + business_date.replace("-", "")
