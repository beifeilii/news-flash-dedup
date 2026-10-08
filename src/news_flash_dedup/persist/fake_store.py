"""P18 fake 仓储接口（单元层注测，不连真 ES）。

按 05:25 主审核方批复：单元/fake_store 层先开工；真 ES UAT 与 P17-3 同队等凭据。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class FakeAuditDoc:
    """fake 审计文档（模拟 ES audits-v1 索引落地后）。"""
    comparison_id: str
    history_record_id: str
    current_record_id: str
    history_raw_hash: str
    current_raw_hash: str
    basis: str
    field_path: str
    detail: str
    history_evidence: Mapping[str, Any]
    current_evidence: Mapping[str, Any]
    pipeline_version: str
    payload_hash: str = ""
    created_at: str = ""
    decision_artifact_version: str = "1.0"


@dataclass(frozen=True)
class FakeMainRecordV1:
    """fake 主记录结构 v1（按 P18-设计-WIP §3.1 + 05:25 修订 2）。"""
    record_id: str
    item_id: str
    text: str
    scope_id: str
    business_date: str
    arrival_seq: int
    raw_hash: str
    pipeline_version: str
    fact_artifact_hash: str
    vector_state: str               # "pending" 创建时写入 / P19 翻 ready/failed
    result_item_id: str
    result_decision: str
    result_duplicate_ids: tuple[str, ...]
    result_reason: str
    task_state: str
    completed_at: str
    result_version: int
    event_id: str
    audit_ids: tuple[str, ...]
    audit_complete: bool
    reason_code: str
    callback_body: str
    callback_body_hash: str
    delivery_state: str            # 首次 not_ready；CAS 成功翻 pending；CAS 失败不动
                                   # 10-07 令甲-i：decision≠不重复 时 CAS 落 held（扣留不投递）
    delivery_deadline_at: str
    next_delivery_at: str
    callback_attempts: int
    round_attempts: int
    round: int
    # P2 工程债 C-08（窗口X 并线移植）：expires_at 透传通道（默认空串 =
    # 现状行为/构造兼容；传入时 persist_main_record 一并按 10 §6.337 钳制
    # delivery_deadline_at）
    expires_at: str = ""


class FakeCASConflictError(RuntimeError):
    """fake 主记录 CAS 版本竞争冲突（窗口M 伪 CAS 修复，窗口X 并线移植）。

    对齐真层 persist/es_store.CASConflictError 语义：expected 版本与 store
    当前版本不符（或 (0,0) 首创建约定下记录已存在）→ 拒绝，写入未发生，
    已存记录任何字段不动（INV-2/INV-3 同款语义）。
    """


@dataclass
class FakeP18Store:
    """fake P18 仓储（单元层注测；模拟 ES 主记录 + 审计 + watermark）。

    窗口M（窗口X 并线）：补 main_record_versions 版本跟踪（ES _seq_no 0 基 /
    primary_term fake 恒 1）——旧实现零版本概念，cas_main_record 的
    expected 参数因此从不比对（伪 CAS）。版本由 _write 单点推进。

    W2Fγ（ζ2 校验子集合同声明）：本 fake 仅执法真层校验子集——
    write_audit create-only（重 ID 冲突即 FakeCASConflictError，对齐真层
    bulk create）与 write_main_record 创建落点重 ID 自查（对齐真层方案 B
    write_main_record→cas_main_record(0,0)→op_type=create）；其余守卫在
    上层（main_record_persist.cas_main_record 的 delivery_state/task_state/
    audit_complete/版本对拍四校验、coordinator.persist_main_record 的
    audit_complete 前置），本层不重复执法。更新落点 update_main_record
    仅供上层版本对拍通过后调用（本层不复核版本——守卫在上层）。
    """
    main_records: dict[str, FakeMainRecordV1] = field(default_factory=dict)
    audit_docs: dict[str, FakeAuditDoc] = field(default_factory=dict)
    watermark_seq: dict[str, int] = field(default_factory=dict)
    cas_calls: list[dict] = field(default_factory=list)
    audit_calls: list[dict] = field(default_factory=list)
    watermark_calls: list[dict] = field(default_factory=list)
    main_record_versions: dict[str, tuple[int, int]] = field(default_factory=dict)

    def write_audit(self, doc: FakeAuditDoc) -> str:
        # W2Fγ（A4 并案 WA4b-35①）：create-only——重 comparison_id 冲突即
        # FakeCASConflictError（对齐真层 es_store.write_audit_batch bulk
        # create 冲突即 AuditPersistError；旧静默覆盖可篡改审计证据）。
        # 已存文档任何字段不动（INV-2 同款语义）。
        if doc.comparison_id in self.audit_docs:
            raise FakeCASConflictError(
                f"audit doc {doc.comparison_id!r} already exists "
                f"(create conflict; audit evidence is create-only)"
            )
        self.audit_docs[doc.comparison_id] = doc
        self.audit_calls.append({"action": "write_audit",
                                   "comparison_id": doc.comparison_id})
        return doc.comparison_id

    def main_version(self, record_id: str) -> tuple[int, int] | None:
        """当前 (seq_no, primary_term)；None = 记录从未写入。"""
        return self.main_record_versions.get(record_id)

    def _write(self, record: FakeMainRecordV1) -> str:
        self.main_records[record.record_id] = record
        prev = self.main_record_versions.get(record.record_id)
        # ES _seq_no 0 基：首写 (0,1)；其后每次写 seq+1（term fake 恒 1）
        self.main_record_versions[record.record_id] = (
            (0, 1) if prev is None else (prev[0] + 1, prev[1]))
        self.cas_calls.append({
            "action": "write_main_record",
            "record_id": record.record_id,
            "audit_complete": record.audit_complete,
            "delivery_state": record.delivery_state,
        })
        return record.record_id

    def write_main_record(self, record: FakeMainRecordV1) -> str:
        """创建落点（create-only）：重 ID = FakeCASConflictError（W2Fγ ζ2，
        commit/fake_store Z2 同款自查；对齐真层方案 B write_main_record
        协议 cas_main_record(0,0)→op_type=create）。更新落点走
        update_main_record（上层版本对拍通过后）。"""
        if record.record_id in self.main_records:
            raise FakeCASConflictError(
                f"main record {record.record_id!r} already exists "
                f"(create conflict at create-only write_main_record)"
            )
        return self._write(record)

    def update_main_record(self, record: FakeMainRecordV1) -> str:
        """更新落点：仅供 main_record_persist.cas_main_record 版本对拍通过
        后调用（守卫在上层——ζ2 校验子集合同）；记录不存在 =
        FakeCASConflictError（对齐创建/更新分途两态）。"""
        if record.record_id not in self.main_records:
            raise FakeCASConflictError(
                f"main record {record.record_id!r} not found for update"
            )
        return self._write(record)

    def advance_watermark(self, scope_id: str, business_date: str,
                           arrival_seq: int) -> int:
        key = f"{scope_id}|{business_date}"
        prev = self.watermark_seq.get(key, 0)
        if arrival_seq <= prev:
            raise ValueError(
                f"decision_watermark cannot regress: {prev} -> {arrival_seq}"
            )
        self.watermark_seq[key] = arrival_seq
        self.watermark_calls.append({
            "action": "advance_watermark",
            "key": key,
            "arrival_seq": arrival_seq,
        })
        return arrival_seq

    def reset(self) -> None:
        self.main_records.clear()
        self.audit_docs.clear()
        self.watermark_seq.clear()
        self.cas_calls.clear()
        self.audit_calls.clear()
        self.watermark_calls.clear()
        self.main_record_versions.clear()


__all__ = [
    "FakeAuditDoc",
    "FakeCASConflictError",
    "FakeMainRecordV1",
    "FakeP18Store",
]