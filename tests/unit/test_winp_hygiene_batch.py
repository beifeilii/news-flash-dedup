"""窗口P（M-12/L 簇卫生债批次）行为微变项红测 + 复核锁定。

配套 log/窗口P-卫生批报告.md：本文件只新增不改既有套件。
每项微变先证红（旧行为下本测试失败）后修复转绿；报告逐项登记证据。
"""

from __future__ import annotations

import hashlib
import logging
import typing

import pytest


# ---------- 项4a：batch_admission raise 统一 from error（保异常链） ----------

def test_validate_log_keeps_exception_chain():
    """_validate_log 拒绝非法 head：__cause__ 必须非 None（旧 from None 断链）。"""
    from news_flash_dedup.batch_admission import (
        AdmissionConflict, BatchAdmissionCoordinator,
    )
    with pytest.raises(AdmissionConflict) as captured:
        BatchAdmissionCoordinator._validate_log({"kind": "admission"})
    assert captured.value.__cause__ is not None


def test_live_invalid_expiry_keeps_exception_chain():
    """_live 非法到期串：__cause__ 必须非 None（旧 from None 断链）。"""
    from news_flash_dedup.batch_admission import (
        AdmissionConflict, BatchAdmissionCoordinator,
    )
    coordinator = BatchAdmissionCoordinator(
        object(), owner_id="w", owner_isolated=lambda: True,
        limits=None, clock=None)
    with pytest.raises(AdmissionConflict) as captured:
        coordinator._live("not-a-date")
    assert captured.value.__cause__ is not None


# ---------- 项4b：api/cleanup.py 吞错 fail-closed + fromisoformat 入 try ----------

def _cleanup_app():
    from news_flash_dedup.api.cleanup import create_cleanup_app

    class _Port:
        def dry_run(self, today, operator):
            return {"today": today.isoformat(), "operator": operator,
                    "candidates": []}

        def execute(self, today, operator, dry_run):
            return {"today": today.isoformat(), "operator": operator,
                    "dry_run": dry_run, "deleted": []}

    return create_cleanup_app(cleanup_port=_Port(),
                              admin_authenticated=lambda request: True)


def test_cleanup_dry_run_malformed_json_is_400():
    """dry_run：JSON 体损坏 → 400（旧：吞错退化为空体默认值，可能按今天干跑）。"""
    from fastapi.testclient import TestClient
    client = TestClient(_cleanup_app())
    response = client.post(
        "/v1/api/admin/cleanup/dry_run", content=b"{not-json",
        headers={"content-type": "application/json"})
    assert response.status_code == 400


def test_cleanup_dry_run_invalid_today_is_400():
    """dry_run：非法 today → 400（旧：ValueError 裸冒 500）。"""
    from fastapi.testclient import TestClient
    client = TestClient(_cleanup_app())
    response = client.post("/v1/api/admin/cleanup/dry_run",
                           json={"today": "2026-13-99"})
    assert response.status_code == 400


def test_cleanup_execute_invalid_today_is_400():
    """execute：非法 today → 400（旧：ValueError 裸冒 500）。"""
    from fastapi.testclient import TestClient
    client = TestClient(_cleanup_app())
    response = client.post("/v1/api/admin/cleanup/execute",
                           json={"today": "not-a-date"})
    assert response.status_code == 400


def test_cleanup_dry_run_valid_request_still_200():
    """对照：合法 dry_run 不受影响。"""
    from fastapi.testclient import TestClient
    client = TestClient(_cleanup_app())
    response = client.post("/v1/api/admin/cleanup/dry_run",
                           json={"today": "2026-09-26"})
    assert response.status_code == 200
    assert response.json()["today"] == "2026-09-26"


# ---------- 项5：api/contract.py safe_log_fields 500 入列 ----------

def test_safe_log_fields_keeps_status_500():
    """500（AdmissionUnknown 映射码）必须可入日志（旧：被白名单静默丢弃）。"""
    from news_flash_dedup.api.contract import safe_log_fields
    assert safe_log_fields({"status": 500}) == {"status": 500}
    assert safe_log_fields({"status": "500"}) == {"status": "500"}


def test_safe_log_fields_still_drops_non_whitelist():
    """对照：非白名单状态码仍丢弃。"""
    from news_flash_dedup.api.contract import safe_log_fields
    assert safe_log_fields({"status": 418}) == {}


# ---------- 项6a：vector/coordinator.py Iterable 导入（get_type_hints 可解析） ----------

def test_reconcile_pending_failures_type_hints_resolvable():
    """get_type_hints 不再 NameError（旧：Iterable 未导入）。"""
    from news_flash_dedup.vector import coordinator as vector_coordinator
    hints = typing.get_type_hints(vector_coordinator.reconcile_pending_failures)
    assert "record_ids" in hints


# ---------- 项6b：vector/fake_store.py failed 终态不可回 pending（INV-6） ----------

def test_failed_terminal_cannot_return_to_pending():
    """failed → pending 直接复活被拒（旧：failed.discard 静默复活，INV-6 相悖）。"""
    from news_flash_dedup.vector import FakeMilvusStore
    store = FakeMilvusStore()
    store.mark_failed("rec-X")
    with pytest.raises(ValueError, match="(?i)failed|INV-6"):
        store.mark_pending("rec-X")
    assert "rec-X" in store.failed
    assert "rec-X" not in store.pending


def test_mark_pending_fresh_record_still_allowed():
    """对照：新记录首次 pending（创建者职责）不受影响。"""
    from news_flash_dedup.vector import FakeMilvusStore
    store = FakeMilvusStore()
    store.mark_pending("rec-Y")
    assert "rec-Y" in store.pending


# ---------- 项8a：es_client PROD_ES_HOST 空串即拒（与 docstring 对齐） ----------

def _uat_env(**overrides):
    env = {
        "DEPLOY_ENV": "test",
        "TEST_ES_HOST": "es-cn-9fr4srbma0001lus6.elasticsearch.aliyuncs.com",
        "TEST_ES_PORT": "9200",
        "TEST_ES_SCHEME": "http",
    }
    env.update(overrides)
    return env


def test_prod_es_host_empty_string_rejected():
    """PROD_ES_HOST 空串也拒（docstring：'即便为空也拒绝'；旧：真值检查漏空串）。"""
    from news_flash_dedup.es_client import (
        ProductionAccessDenied, assert_test_environment,
    )
    with pytest.raises(ProductionAccessDenied):
        assert_test_environment(_uat_env(PROD_ES_HOST=""))


def test_prod_es_host_unset_still_allowed():
    """对照：未设置仍放行。"""
    from news_flash_dedup.es_client import assert_test_environment
    assert_test_environment(_uat_env())


# ---------- 项8b：milvus_client TypeError 包装为领域异常 ----------

def test_milvus_non_str_port_wrapped_as_domain_error():
    """TEST_MILVUS_PORT=None → 领域异常（旧：int(None) TypeError 逃逸）。"""
    from news_flash_dedup.milvus_client import (
        ProductionMilvusAccessDenied, assert_test_milvus_environment,
    )
    env = {
        "DEPLOY_ENV": "test",
        "TEST_MILVUS_HOST": "c-75641abe71c37c6f-internal.milvus.aliyuncs.com",
        "TEST_MILVUS_PORT": None,
        "TEST_MILVUS_USER": "u",
        "TEST_MILVUS_PASS": "p",
    }
    with pytest.raises(ProductionMilvusAccessDenied):
        assert_test_milvus_environment(env)


# ---------- 项9b：facts/llm.py 磁盘缓存损坏 fail-closed（按 miss + 记日志） ----------

def _extract_with_cache(tmp_path, caplog, cached_text: str):
    from news_flash_dedup.facts.llm import extract_facts_llm
    text = "某公司宣布回购股份。"
    text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    (tmp_path / f"{text_hash}.json").write_text(cached_text, encoding="utf-8")
    calls = []

    def fake_call(**kwargs):
        calls.append(kwargs)
        return '{"facts": []}', 0.01

    with caplog.at_level(logging.WARNING, logger="news_flash_dedup.facts.llm"):
        facts = extract_facts_llm("a" * 64, text, call_fn=fake_call,
                                  cache_dir=tmp_path)
    return facts, calls, caplog


def test_llm_corrupted_cache_json_treated_as_miss(tmp_path, caplog):
    """缓存 JSON 损坏 → 按 miss 走 live + 记 warning（旧：JSONDecodeError 裸冒 crash）。"""
    facts, calls, caplog = _extract_with_cache(tmp_path, caplog, "{corrupted json")
    assert len(calls) == 1
    assert isinstance(facts, list)
    assert any("cache" in record.getMessage().lower()
               for record in caplog.records)


def test_llm_cache_missing_raw_key_treated_as_miss(tmp_path, caplog):
    """缓存缺 raw_llm_content 键 → 按 miss（旧：KeyError 裸冒 crash）。"""
    import json
    facts, calls, caplog = _extract_with_cache(
        tmp_path, caplog,
        json.dumps({"model": "qwen-turbo",
                    "prompt_version": "p14_llm_prompt_v2"}))
    assert len(calls) == 1
    assert isinstance(facts, list)
