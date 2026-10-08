# -*- coding: utf-8 -*-
"""窗口W2Fβ 条64（WA3b-M2）+条66 fake 两件：_vector_id 单源化钉 + fake 校验/原子性/ready 核验钉。

条64：coordinator._vector_id 与 recall/vector_space.vector_id 双实现 → 公式
单源 stable_vector_id（recall 严格路径保留 64-hex 身份校验；P19 fake 编排层
共用同一公式写法，域校验留在上层）。
条66（fake upsert 原子性/mark ready 核验）：fake 对齐真层结构闸子集——稳定
主键一致性、字段类型/取值域、同 ID 异内容冲突不静默覆盖、同内容幂等 reused；
mark ready 仅对已知记录（pending 或已写入）放行，未知记录拒绝。
"""
from __future__ import annotations

import pytest

from news_flash_dedup.recall.vector_space import stable_vector_id, vector_id
from news_flash_dedup.recall.vector_store import VectorIdentityConflict
from news_flash_dedup.vector import (
    FakeMilvusStore,
    FakeVectorRow,
    P19VectorError,
    mark_vector_state,
    upsert_vectors,
)
from news_flash_dedup.vector import coordinator as vector_coordinator


RID = "a" * 64


def _row(record_id="rec-A", chunk_id=0, **overrides):
    values = {
        "vector_id": f"{record_id}_{chunk_id}",
        "record_id": record_id,
        "chunk_id": chunk_id,
        "embedding_space_id": "space-v3",
        "business_date": "2026-09-26",
        "scope_id": "default",
        "arrival_seq": 1,
        "embedding": (0.1, 0.2, 0.3),
    }
    values.update(overrides)
    return FakeVectorRow(**values)


# ---------- 条64：公式单源 ----------

def test_coordinator_vector_id_uses_single_source_formula():
    """钉（条64）：编排层与共享公式逐值一致（含 recall 严格域输入）。"""
    assert vector_coordinator._vector_id("rec-A", 3) == stable_vector_id("rec-A", 3)
    assert vector_coordinator._vector_id(RID, 12) == vector_id(RID, 12)
    assert stable_vector_id("rec-A", 0) == "rec-A_0"


def test_shared_formula_keeps_length_guard():
    """同钉（条64）：共享公式保留 VARCHAR(96) 长度闸。"""
    with pytest.raises(ValueError):
        stable_vector_id("x" * 96, 0)


# ---------- 条64+66：fake 校验拉齐 ----------

def test_fake_upsert_rejects_stable_id_mismatch():
    """红能力：vector_id 与 record_id/chunk_id 不一致 → 拒（旧：零校验存入）。"""
    store = FakeMilvusStore()
    with pytest.raises(ValueError):
        store.upsert(_row(vector_id="forged-id"))
    assert store.rows == {}


@pytest.mark.parametrize("override", [
    {"chunk_id": -1},
    {"arrival_seq": 0},
    {"arrival_seq": "1"},
    {"business_date": "not-a-date"},
    {"business_date": "2026-13-99"},
    {"record_id": ""},
    {"scope_id": ""},
    {"embedding_space_id": ""},
    {"embedding": ()},
    {"embedding": (0.0, 0.0)},          # 零向量（真层 validate_vector 同款拒）
    {"embedding": (0.1, float("nan"))},
    {"embedding": ("x",)},
])
def test_fake_upsert_rejects_structural_garbage(override):
    """红能力：字段类型/取值域垃圾 → 拒（真层结构闸子集，零写入）。"""
    store = FakeMilvusStore()
    with pytest.raises(ValueError):
        store.upsert(_row(**override))
    assert store.rows == {}


def test_fake_upsert_atomic_reuse_and_conflict():
    """红能力（条66 原子性）：同 ID 同内容→幂等；同 ID 异内容→冲突不覆盖。"""
    store = FakeMilvusStore()
    row = _row()
    store.upsert(row)
    store.upsert(_row())                       # 同内容 → 幂等 reused
    assert store.rows[row.vector_id] == row
    with pytest.raises(VectorIdentityConflict):
        store.upsert(_row(embedding=(0.4, 0.5, 0.6)))
    assert store.rows[row.vector_id] == row    # 未被静默覆盖


def test_fake_upsert_wellformed_flow_unchanged():
    """绿守卫：良构 upsert 流程不变（pending 清除 + 调用台账）。"""
    store = FakeMilvusStore()
    store.mark_pending("rec-A")
    rows = upsert_vectors(
        store, record_id="rec-A", embedding_space_id="space-v3",
        business_date="2026-09-26", scope_id="default", arrival_seq=1,
        chunks=[(0, (0.1, 0.2)), (1, (0.3, 0.4))])
    assert [row.vector_id for row in rows] == ["rec-A_0", "rec-A_1"]
    assert "rec-A" not in store.pending
    assert len(store.upsert_calls) == 2


# ---------- 条66：mark ready 核验 ----------

def test_mark_ready_rejects_unknown_record():
    """红能力（条66）：未知记录（非 pending 且无写入行）标 ready → 拒。"""
    store = FakeMilvusStore()
    with pytest.raises(P19VectorError):
        mark_vector_state(store, "rec-ghost", status="ready")


def test_mark_ready_pending_or_written_still_allowed():
    """绿守卫：pending 中 / 已写入记录标 ready 照常。"""
    pending_store = FakeMilvusStore()
    pending_store.mark_pending("rec-A")
    mark_vector_state(pending_store, "rec-A", status="ready")
    written_store = FakeMilvusStore()
    upsert_vectors(written_store, record_id="rec-B",
                   embedding_space_id="space-v3", business_date="2026-09-26",
                   scope_id="default", arrival_seq=1,
                   chunks=[(0, (0.1, 0.2))])
    mark_vector_state(written_store, "rec-B", status="ready")
    assert "rec-B" not in written_store.failed
