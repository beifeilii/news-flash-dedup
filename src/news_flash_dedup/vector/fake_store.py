"""P19 fake 仓储接口（单元层注测，不连真 Milvus）。

按 05:28 主审核方批复：单元/fake_store 层先开工；真 Milvus UAT 与 P17-3 同队等凭据。
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from news_flash_dedup.milvus_client import partition_name
from news_flash_dedup.recall.vector_space import stable_vector_id
from news_flash_dedup.recall.vector_store import VectorIdentityConflict


@dataclass(frozen=True)
class FakeVectorRow:
    """fake Milvus 向量行（模拟 upsert 后状态）。"""
    vector_id: str            # <record_id>_<chunk_id>
    record_id: str
    chunk_id: int
    embedding_space_id: str
    business_date: str
    scope_id: str
    arrival_seq: int
    embedding: tuple[float, ...]


@dataclass
class FakeMilvusStore:
    """fake Milvus 仓储（pure dict 状态，无 Milvus 客户端）。"""
    rows: dict[str, FakeVectorRow] = field(default_factory=dict)
    pending: set[str] = field(default_factory=set)         # record_id 集合（待 upsert）
    failed: set[str] = field(default_factory=set)          # record_id 集合（失败）
    upsert_calls: list[dict] = field(default_factory=list)
    search_calls: list[dict] = field(default_factory=list)

    def upsert(self, row: FakeVectorRow) -> str:
        """写入一行（原子 compare-and-set：同 ID 异内容→冲突，同内容→幂等）。

        窗口W2Fβ（WA3b-M2 条64 + WA3b 条66）：fake 校验拉齐真层结构闸子集
        ——稳定主键一致性（公式单源 stable_vector_id）、字段类型/取值域
        （business_date 规范日、arrival_seq 正整数、非空有限非零向量）、
        同 vector_id 异内容不静默覆盖（VectorIdentityConflict，对齐真层
        INV-2）。校验子集合同声明：record_id 字符域（真层 64-hex）与
        ES 权威对拍由上层守卫，fake 层不重复（既有 P19 钉用非 hex 短 ID）。
        """
        if not isinstance(row, FakeVectorRow):
            raise ValueError("row must be a FakeVectorRow")
        if (not isinstance(row.record_id, str) or not row.record_id or
                not isinstance(row.embedding_space_id, str) or
                not row.embedding_space_id or
                not isinstance(row.scope_id, str) or not row.scope_id):
            raise ValueError("row identities must be nonempty strings")
        if type(row.chunk_id) is not int or row.chunk_id < 0:
            raise ValueError("chunk_id must be nonnegative")
        if type(row.arrival_seq) is not int or row.arrival_seq < 1:
            raise ValueError("arrival_seq must be positive")
        partition_name(row.business_date)   # 规范日校验（真层同款）
        if (not isinstance(row.embedding, tuple) or not row.embedding or
                any(type(value) not in (int, float) or not math.isfinite(value)
                    for value in row.embedding) or
                not any(value != 0 for value in row.embedding)):
            raise ValueError("embedding must be a nonzero finite numeric tuple")
        if row.vector_id != stable_vector_id(row.record_id, row.chunk_id):
            raise ValueError("vector_id differs from its stable identity")
        existing = self.rows.get(row.vector_id)
        if existing is not None:
            if existing != row:
                raise VectorIdentityConflict(
                    "same vector_id has a different artifact"
                )
            return row.vector_id     # reused：幂等零改写
        self.rows[row.vector_id] = row
        self.pending.discard(row.record_id)
        self.upsert_calls.append({
            "action": "upsert",
            "vector_id": row.vector_id,
            "record_id": row.record_id,
        })
        return row.vector_id

    def mark_failed(self, record_id: str) -> None:
        self.failed.add(record_id)
        self.pending.discard(record_id)

    def mark_pending(self, record_id: str) -> None:
        # INV-6 收紧（M-12/L 窗口P）：failed 为终态，不可直接复活回 pending；
        # 仅新记录首次 pending（创建者职责）允许。
        if record_id in self.failed:
            raise ValueError(
                f"record {record_id!r} is in terminal failed state; "
                "cannot return to pending (INV-6)"
            )
        self.pending.add(record_id)

    def search(self, record_id: str, embedding_space_id: str,
                business_date: str, scope_id: str) -> list[FakeVectorRow]:
        """Strong 一致性 search（fake 路径过滤后返回）。"""
        results: list[FakeVectorRow] = []
        for row in self.rows.values():
            if (row.record_id != record_id
                    and row.embedding_space_id == embedding_space_id
                    and row.business_date == business_date
                    and row.scope_id == scope_id):
                results.append(row)
        self.search_calls.append({"action": "search",
                                    "record_id": record_id,
                                    "embedding_space_id": embedding_space_id})
        return results

    def reset(self) -> None:
        self.rows.clear()
        self.pending.clear()
        self.failed.clear()
        self.upsert_calls.clear()
        self.search_calls.clear()


__all__ = ["FakeMilvusStore", "FakeVectorRow"]