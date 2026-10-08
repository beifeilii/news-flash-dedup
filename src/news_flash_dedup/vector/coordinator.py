"""P19 向量双写编排器：领取 → embedding → upsert → ready/failed 翻转。

按 05:28 主审核方批复 + P19-设计-WIP §5.1：P19 仅做 pending→ready/failed
翻转（INV-6 独占权）；记录首次创建时的 pending 写入由创建者（P17/P18）负责。
"""

from __future__ import annotations

from collections.abc import Iterable

from news_flash_dedup.recall.vector_space import stable_vector_id

from .fake_store import FakeMilvusStore, FakeVectorRow


class P19VectorError(RuntimeError):
    """P19 向量双写错误（embedding / upsert / CAS）。"""


def _vector_id(record_id: str, chunk_id: int) -> str:
    """vector_id = <record_id>_<chunk_id>（稳定主键）。

    窗口W2Fβ（WA3b-M2 条64）：公式单源化——委托
    recall.vector_space.stable_vector_id（双实现已消）；recall 严格域的
    64-hex 身份校验在 vector_id()，本层（fake 编排）只共用公式写法。
    """
    return stable_vector_id(record_id, chunk_id)


def upsert_vectors(
    store: FakeMilvusStore,
    *,
    record_id: str,
    embedding_space_id: str,
    business_date: str,
    scope_id: str,
    arrival_seq: int,
    chunks: list[tuple[int, tuple[float, ...]]],   # [(chunk_id, embedding), ...]
) -> list[FakeVectorRow]:
    """P19 编排器：逐 chunk 调 FakeMilvusStore.upsert；返回写入的行列表。

    强一致性搜索由 FakeMilvusStore.search() 提供（Strong 一致性合同 §3）。
    """
    if not chunks:
        raise P19VectorError("chunks must be non-empty")
    rows: list[FakeVectorRow] = []
    for chunk_id, embedding in chunks:
        row = FakeVectorRow(
            vector_id=_vector_id(record_id, chunk_id),
            record_id=record_id,
            chunk_id=chunk_id,
            embedding_space_id=embedding_space_id,
            business_date=business_date,
            scope_id=scope_id,
            arrival_seq=arrival_seq,
            embedding=tuple(embedding),
        )
        store.upsert(row)
        rows.append(row)
    return rows


def mark_vector_state(
    store: FakeMilvusStore, record_id: str, *, status: str,
) -> None:
    """P19 翻转向量状态（pending → ready/failed）。INV-6：仅 P19 改此字段。"""
    if status not in {"ready", "failed"}:
        raise P19VectorError(f"status must be 'ready' or 'failed'; got {status!r}")
    if status == "ready":
        if record_id in store.failed:
            raise P19VectorError(
                f"record {record_id!r} previously marked failed; cannot mark ready"
            )
        # 窗口W2Fβ（WA3b 条66）：mark ready 核验——fake 对齐真层「写确认后
        # 才 CAS ready」：记录必须已知（仍在 pending 待写，或已有写入行）；
        # 未知记录标 ready 拒绝（旧：静默 discard 空 pending 冒充成功）。
        if record_id not in store.pending and not any(
                row.record_id == record_id for row in store.rows.values()):
            raise P19VectorError(
                f"record {record_id!r} is unknown; cannot mark ready"
            )
        store.pending.discard(record_id)
    elif status == "failed":
        store.mark_failed(record_id)


def reconcile_pending_failures(
    store: FakeMilvusStore, record_ids: Iterable[str],
) -> tuple[set[str], set[str]]:
    """孔洞修复：标记 record_ids 中的 pending/failed 项；返回 (still_pending, now_failed)。

    仅以 store 内部状态为准；不查 ES / Milvus 真集群。
    """
    still_pending: set[str] = set()
    now_failed: set[str] = set()
    for record_id in record_ids:
        if record_id in store.failed:
            now_failed.add(record_id)
        elif record_id in store.pending:
            still_pending.add(record_id)
            store.pending.discard(record_id)
    return still_pending, now_failed


__all__ = [
    "P19VectorError",
    "mark_vector_state",
    "reconcile_pending_failures",
    "upsert_vectors",
]