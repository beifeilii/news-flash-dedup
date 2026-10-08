"""P18 audit_persist 写入器（fake + 真 ES 接口占位）。

按 05:25 主审核方批复 + P18-设计-WIP §2：单元/fake_store 先开工；真 ES UAT 等凭据。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from typing import Any

from .fake_store import FakeAuditDoc, FakeP18Store


def build_audit_doc(audit_record: Mapping[str, Any],
                     created_at: str = "2026-09-26T03:33:00+08:00") -> FakeAuditDoc:
    """从 audit_record dict 构造 FakeAuditDoc（payload_hash 自计算）。"""
    canonical = {
        "comparison_id": audit_record["comparison_id"],
        "history_record_id": audit_record["history_record_id"],
        "current_record_id": audit_record["current_record_id"],
        "history_raw_hash": audit_record["history_raw_hash"],
        "current_raw_hash": audit_record["current_raw_hash"],
        "basis": audit_record["basis"],
        "field_path": audit_record["field_path"],
        "detail": audit_record["detail"],
        "history_evidence": audit_record["history_evidence"],
        "current_evidence": audit_record["current_evidence"],
        "pipeline_version": audit_record["pipeline_version"],
    }
    encoded = json.dumps(canonical, sort_keys=True, ensure_ascii=False).encode("utf-8")
    payload_hash = hashlib.sha256(encoded).hexdigest()
    return FakeAuditDoc(
        comparison_id=audit_record["comparison_id"],
        history_record_id=audit_record["history_record_id"],
        current_record_id=audit_record["current_record_id"],
        history_raw_hash=audit_record["history_raw_hash"],
        current_raw_hash=audit_record["current_raw_hash"],
        basis=audit_record["basis"],
        field_path=audit_record["field_path"],
        detail=audit_record["detail"],
        history_evidence=audit_record["history_evidence"],
        current_evidence=audit_record["current_evidence"],
        pipeline_version=audit_record["pipeline_version"],
        payload_hash=payload_hash,
        created_at=created_at,
    )


def write_audit_batch(store: FakeP18Store, audit_records: Iterable[Mapping[str, Any]],
                        created_at: str = "2026-09-26T03:33:00+08:00") -> list[str]:
    """批量写入审计文档（fake_store 路径）；全部落库后才允许 CAS。"""
    ids: list[str] = []
    for record in audit_records:
        doc = build_audit_doc(record, created_at=created_at)
        store.write_audit(doc)
        ids.append(doc.comparison_id)
    return ids


__all__ = ["build_audit_doc", "write_audit_batch"]