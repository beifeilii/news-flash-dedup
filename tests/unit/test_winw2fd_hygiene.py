"""W2Fδ 修复窗口红测：条39（死代码族）/条40（原型标注）/条41（F7 低面子集）/条73（.env.example）/条76（p23 docstring）/条78（scripts 卫生）。

只新建不改既有套件。
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------- 条39：text/hash 死函数删除 + __all__ 同步 + 未用 import ----------

def test_text_hashes_for_text_safe_removed():
    import news_flash_dedup.text.hash as module
    assert not hasattr(module, "text_hashes_for_text_safe")
    assert "text_hashes_for_text_safe" not in module.__all__


def test_dead_imports_removed():
    import news_flash_dedup.es_admission_index as index_module
    import news_flash_dedup.metrics.p23_metrics as metrics_module
    import news_flash_dedup.text.hash as hash_module
    assert not hasattr(hash_module, "hashlib")
    assert not hasattr(index_module, "time")
    assert not hasattr(index_module, "timezone")
    assert not hasattr(metrics_module, "Iterable")


def test_surviving_hash_entry_points_intact():
    """条39 对照：保留面（bucket_key_if_indexable/text_hashes_for_text 等）不受删改影响。"""
    from news_flash_dedup.text.hash import (
        bucket_key_if_indexable, text_hashes_for_text,
    )
    assert text_hashes_for_text("正文").raw_hash
    key = bucket_key_if_indexable("正文", "scope", "2026-09-28", "raw")
    assert key is not None and key.count("/") == 4


# ---------- 条40：admission.py bench 对照原型·纪律代际差标注 ----------

def test_admission_module_marks_bench_prototype():
    import news_flash_dedup.admission as module
    doc = module.__doc__ or ""
    assert "bench 对照原型" in doc
    assert "纪律代际差" in doc


# ---------- 条41：魔法数 4 常量化 / flushed_* 语义注释 / bucket 双 normalize 合并 ----------

def test_documents_per_entry_constant_matches_documents_shape():
    from datetime import datetime, timezone
    from news_flash_dedup.admission import AdmissionRequest
    from news_flash_dedup.batch_admission import (
        _DOCUMENTS_PER_ENTRY, BatchAdmissionCoordinator, BatchLimits,
    )
    from test_batch_admission import BatchMemoryStore

    assert _DOCUMENTS_PER_ENTRY == 4
    store = BatchMemoryStore()
    coordinator = BatchAdmissionCoordinator(
        store, owner_id="w2fd", owner_isolated=lambda: True,
        limits=BatchLimits(),
        clock=lambda: datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc))
    coordinator.accept_batch([AdmissionRequest(
        scope_id="default", request_id="1-1", item_id="1-1", text="正文",
        received_at=datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc),
        schema_version="1", pipeline_version="w2fd-v1", embedding_space_id="none",
        delivery_route_ref="route-v1", trace_id=None)])
    entries = store.docs[("news-dedup-control-v1", "batch_admission_head")]["source"]["pending"]["batches"][0]["entries"]
    documents = BatchAdmissionCoordinator._documents(entries)
    assert len(documents) == _DOCUMENTS_PER_ENTRY * len(entries)


def test_documents_per_entry_constant_is_used_at_quarantine_sites():
    from news_flash_dedup import batch_admission
    source = inspect.getsource(batch_admission)
    assert "first // _DOCUMENTS_PER_ENTRY" in source
    assert "position // _DOCUMENTS_PER_ENTRY" in source


def test_flushed_counters_carry_attempt_semantics_note():
    from news_flash_dedup import batch_collector
    source = inspect.getsource(batch_collector)
    assert "尝试" in source  # flushed_* 计"尝试"非"成功"语义钉


def test_bucket_key_if_indexable_normalizes_once(monkeypatch):
    """条41：双 normalize 合并——hash_value 缺省路径归一只跑一次（行为等价钉）。"""
    import news_flash_dedup.text.hash as module

    calls = []
    original = module.normalize_text

    def counting(text, *, outer_trim_approved=False):
        calls.append(text)
        return original(text, outer_trim_approved=outer_trim_approved)

    monkeypatch.setattr(module, "normalize_text", counting)
    key = module.bucket_key_if_indexable("  正文  ", "scope", "2026-09-28", "normalized")
    assert key is not None
    assert len(calls) == 1


def test_bucket_key_if_indexable_behavior_unchanged():
    """条41 等价对照：合并后三种调用方式同文同键；空白仍拒。"""
    from news_flash_dedup.text.hash import (
        bucket_key, bucket_key_if_indexable, compute_text_hashes, normalize_text,
    )
    auto = bucket_key_if_indexable("正文", "scope", "2026-09-28", "raw")
    manual = bucket_key("scope", "2026-09-28", "raw",
                        compute_text_hashes(normalize_text("正文")).raw_hash)
    assert auto == manual
    assert bucket_key_if_indexable("   ", "scope", "2026-09-28", "raw") is None
    assert bucket_key_if_indexable("", "scope", "2026-09-28", "raw") is None


# ---------- 条73：.env.example PROD_ 行注释化 + 部署注记 ----------

def test_env_example_has_no_active_prod_keys():
    env_example = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    active_prod = [line for line in env_example.splitlines()
                   if line.strip().startswith("PROD_")]
    assert active_prod == [], f"PROD_ 键存在（含空值）即被启动闸拒绝：{active_prod}"


def test_env_example_carries_deployment_note():
    env_example = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    assert "PROD_ES_HOST" in env_example  # 注记中解释为何不得设置
    assert "秘密管理" in env_example or "部署" in env_example


# ---------- 条76：p23_metrics load_pairs_from_manifest docstring 勘正 ----------

def test_load_pairs_docstring_marks_synthetic_only():
    from news_flash_dedup.metrics.p23_metrics import load_pairs_from_manifest
    doc = load_pairs_from_manifest.__doc__ or ""
    assert "合成/测试专用" in doc
    assert "真实金标回放不经此函数" in doc or "不经此函数" in doc


# ---------- 条78：scripts 卫生 ----------

def test_bench_batch_script_has_no_broken_exception_chains():
    import re
    source = (REPO_ROOT / "scripts" / "bench_batch_admission.py").read_text(encoding="utf-8")
    broken = [line for line in source.splitlines()
              if re.search(r"\braise\b.*\bfrom None\b", line)]
    assert broken == [], f"断链 raise 残留：{broken}"


def test_bench_client_from_environment_rejects_prod_host(monkeypatch):
    """L-6/L-7：PROD_ES_HOST 键存在（含空值）即 fail-closed 拒绝。"""
    monkeypatch.setenv("DEPLOY_ENV", "test")
    monkeypatch.setenv("PROD_ES_HOST", "")
    monkeypatch.delenv("ALLOW_PROD_WRITE", raising=False)
    import sys
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    import bench_admission
    with pytest.raises(RuntimeError, match="PROD_ES_HOST"):
        bench_admission.client_from_environment()


def test_bench_client_from_environment_rejects_allow_prod_write(monkeypatch):
    monkeypatch.setenv("DEPLOY_ENV", "test")
    monkeypatch.delenv("PROD_ES_HOST", raising=False)
    monkeypatch.setenv("ALLOW_PROD_WRITE", "true")
    import sys
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    import bench_admission
    with pytest.raises(RuntimeError, match="ALLOW_PROD_WRITE"):
        bench_admission.client_from_environment()


def test_bench_client_from_environment_rejects_prod_deploy_env(monkeypatch):
    """条78 对照：DEPLOY_ENV 闸不受新增检查影响。"""
    monkeypatch.setenv("DEPLOY_ENV", "prod")
    monkeypatch.delenv("PROD_ES_HOST", raising=False)
    monkeypatch.delenv("ALLOW_PROD_WRITE", raising=False)
    import sys
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    import bench_admission
    with pytest.raises(RuntimeError):
        bench_admission.client_from_environment()
