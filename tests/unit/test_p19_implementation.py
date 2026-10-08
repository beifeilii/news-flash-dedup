"""P19 实施测试：upsert / mark_vector_state / reconcile_pending_failures + INV-6 独占权。

按 05:28 主审核方批复 + P19-设计-WIP §5/INV-6：
- P19 仅做 pending→ready/failed 翻转；
- 记录首次创建时由创建者写 pending（P17/P18 接管）；
- 不写真实向量；UAT 仅 mock embedding（INV-7）；
- 不连真 Milvus。
"""

from __future__ import annotations

import pytest

from news_flash_dedup.vector import (
    FakeMilvusStore,
    FakeVectorRow,
    P19VectorError,
    mark_vector_state,
    reconcile_pending_failures,
    upsert_vectors,
)


# ---------- INV-6 独占权：P19 仅做 pending→ready/failed 翻转 ----------

def test_mark_vector_state_pending_to_ready():
    """pending → ready：成功（store.pending 移除 record_id）。"""
    store = FakeMilvusStore()
    store.mark_pending("rec-A")
    assert "rec-A" in store.pending
    mark_vector_state(store, "rec-A", status="ready")
    assert "rec-A" not in store.pending


def test_mark_vector_state_pending_to_failed():
    """pending → failed：成功（store.failed 增加 record_id）。"""
    store = FakeMilvusStore()
    store.mark_pending("rec-A")
    mark_vector_state(store, "rec-A", status="failed")
    assert "rec-A" in store.failed
    assert "rec-A" not in store.pending


def test_mark_vector_state_invalid_status_rejected():
    """status ∉ {ready, failed} → P19VectorError。"""
    store = FakeMilvusStore()
    with pytest.raises(P19VectorError, match="(?i)status|ready|failed"):
        mark_vector_state(store, "rec-A", status="unknown")


def test_mark_vector_state_failed_to_ready_rejected():
    """failed → ready 被拒（INV-6：一旦失败不可自愈为 ready）。"""
    store = FakeMilvusStore()
    store.mark_failed("rec-A")
    with pytest.raises(P19VectorError, match="(?i)previously marked failed"):
        mark_vector_state(store, "rec-A", status="ready")


# ---------- upsert 真实写入 + pending 清除 ----------

def test_upsert_vectors_writes_rows_and_clears_pending():
    """upsert 写入行 + 清除 pending 标记。"""
    store = FakeMilvusStore()
    store.mark_pending("rec-A")
    rows = upsert_vectors(
        store, record_id="rec-A", embedding_space_id="text-embedding-v3",
        business_date="2026-09-26", scope_id="default", arrival_seq=1,
        chunks=[(0, (0.1, 0.2, 0.3)), (1, (0.4, 0.5, 0.6))],
    )
    assert len(rows) == 2
    assert len(store.rows) == 2
    assert "rec-A" not in store.pending
    # vector_id 稳定
    assert "rec-A_0" in store.rows
    assert "rec-A_1" in store.rows


def test_upsert_vectors_empty_chunks_rejected():
    """chunks=[] → P19VectorError（INV-1：必须真写至少一个向量）。"""
    store = FakeMilvusStore()
    with pytest.raises(P19VectorError, match="(?i)chunks|non-empty"):
        upsert_vectors(
            store, record_id="rec-A", embedding_space_id="text-embedding-v3",
            business_date="2026-09-26", scope_id="default", arrival_seq=1,
            chunks=[],
        )


# ---------- search：Strong 一致性 + 标量过滤 ----------

def test_search_filters_by_record_id_embedding_space_date_scope():
    """search 排除自身 record_id，按 embedding_space_id + business_date + scope_id 过滤。"""
    store = FakeMilvusStore()
    upsert_vectors(store, record_id="rec-A", embedding_space_id="space-v3",
                    business_date="2026-09-26", scope_id="default", arrival_seq=1,
                    chunks=[(0, (0.1, 0.2))])
    upsert_vectors(store, record_id="rec-B", embedding_space_id="space-v3",
                    business_date="2026-09-26", scope_id="default", arrival_seq=2,
                    chunks=[(0, (0.3, 0.4))])
    upsert_vectors(store, record_id="rec-C", embedding_space_id="space-other",
                    business_date="2026-09-26", scope_id="default", arrival_seq=3,
                    chunks=[(0, (0.5, 0.6))])
    results = store.search(record_id="rec-A", embedding_space_id="space-v3",
                           business_date="2026-09-26", scope_id="default")
    # rec-A 自身被排除；rec-B 命中；rec-C 不同 embedding_space 不命中
    record_ids = {row.record_id for row in results}
    assert "rec-A" not in record_ids
    assert "rec-B" in record_ids
    assert "rec-C" not in record_ids


# ---------- 孔洞修复 reconcile_pending_failures ----------

def test_reconcile_separates_still_pending_and_now_failed():
    """孔洞修复：pending → still_pending；failed → now_failed。"""
    store = FakeMilvusStore()
    store.mark_pending("rec-A")
    store.mark_pending("rec-B")
    store.mark_failed("rec-C")
    still_pending, now_failed = reconcile_pending_failures(
        store, ["rec-A", "rec-B", "rec-C", "rec-D"],
    )
    assert "rec-A" in still_pending
    assert "rec-B" in still_pending
    assert "rec-C" in now_failed
    assert "rec-D" not in still_pending
    assert "rec-D" not in now_failed
    # pending 已清
    assert "rec-A" not in store.pending
    assert "rec-B" not in store.pending


# ---------- fake_store 不连 Milvus ----------

def test_fake_store_does_not_import_milvus():
    """P19 fake_store 不引入真 Milvus 客户端（INV-7：UAT 仅 mock）。"""
    import news_flash_dedup.vector.fake_store as fs
    module_attrs = dir(fs)
    for attr in ("MilvusClient", "pymilvus", "milvus_client", "connections"):
        assert attr not in module_attrs, (
            f"P19 fake_store 不应引入真 Milvus 客户端 {attr!r}"
        )


# ---------- 端到端 smoke ----------

def test_p19_end_to_end_smoke():
    """完整路径：pending → upsert → ready。"""
    store = FakeMilvusStore()
    store.mark_pending("rec-A")
    upsert_vectors(
        store, record_id="rec-A", embedding_space_id="text-embedding-v3",
        business_date="2026-09-26", scope_id="default", arrival_seq=1,
        chunks=[(0, (0.1, 0.2))],
    )
    mark_vector_state(store, "rec-A", status="ready")
    assert "rec-A" not in store.pending
    assert "rec-A" not in store.failed
    assert "rec-A_0" in store.rows