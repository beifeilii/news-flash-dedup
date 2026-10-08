"""P17 fake 仓储接口（单元层注测，不连真 ES）。

按 04:06 强制令标尺 5：CAS/watermark 用 fake 仓储注测；真 ES UAT 继续挂账
至 P17-3，与 P18 设计一并评审。

窗口Z2（N2-03，外部审计三轮确认）：write_main_record 版本化——旧实现
静默覆盖同 record_id（与真层 (0,0) 首创建约定"重 ID = create 冲突"两态
相反）；现补版本对拍对齐真层语义（persist/es_store.RealESP18Store.
write_main_record → cas_main_record(0,0) → op_type=create）：首写落
(0,1)（ES _seq_no 0 基 / primary_term fake 恒 1，persist/fake_store
同例），重 ID 一律 FakeCASConflictError（现役异常复用），写入不发生、
已存记录任何字段不动（INV-2 同款语义）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from news_flash_dedup.persist.fake_store import FakeCASConflictError


@dataclass(frozen=True)
class FakeMainRecord:
    """fake 主记录结构（模拟 ES 主记录落地后）。"""
    record_id: str
    item_id: str
    text: str
    decision: str
    duplicate_ids: tuple[str, ...]
    reason: str
    payload_hash: str
    delivery_state: str
    delivery_deadline_at: str
    callback_attempts: int
    audit_ids: tuple[str, ...]
    audit_complete: bool
    raw_hash: str
    pipeline_version: str
    completed_at: str
    result_version: int
    event_id: str
    internal_code: str = ""
    # P2 工程债 C-08（窗口X 并线移植）：expires_at 透传通道（默认空串 =
    # 现状行为/构造兼容；传入时 commit_one 一并按 10 §6.337 钳制
    # delivery_deadline_at）
    expires_at: str = ""


@dataclass
class FakeCommitStore:
    """fake 主记录 + watermark 仓储。

    窗口Z2（N2-03）：补 main_record_versions 版本跟踪（ES _seq_no 0 基 /
    primary_term fake 恒 1，persist/fake_store.FakeP18Store 同例）——
    write_main_record 对齐真层 (0,0) 首创建约定：重 ID = create 冲突
    （FakeCASConflictError），不再静默覆盖。

    W2Fγ（ζ2 校验子集合同声明）：本 fake 仅执法真层校验子集——
    write_main_record 创建落点重 ID 自查（Z2 已封）与 write_audit
    create-only（W2Fγ A4 并案：重 comparison_id 冲突即
    FakeCASConflictError，对齐真层 write_audit_documents bulk create
    冲突即错）；其余守卫在上层（commit_one 的 watermark 前置/幂等对拍、
    decide 域审计完整性），本层不重复执法。
    """
    main_records: dict[str, FakeMainRecord] = field(default_factory=dict)
    watermark_seq: dict[str, int] = field(default_factory=dict)
    audit_docs: dict[str, dict] = field(default_factory=dict)
    cas_calls: list[dict] = field(default_factory=list)
    watermark_calls: list[dict] = field(default_factory=list)
    main_record_versions: dict[str, tuple[int, int]] = field(default_factory=dict)

    def main_version(self, record_id: str) -> tuple[int, int] | None:
        """当前 (seq_no, primary_term)；None = 记录从未写入。"""
        return self.main_record_versions.get(record_id)

    def write_main_record(self, record: FakeMainRecord, **_ctx: Any) -> str:
        # 19:58 主窗口（方案 B 纯增量）：签名加 **_ctx 吞掉 ctx 三参，行为不变
        # 窗口Z2（N2-03）：真层 write_main_record 恒走 cas_main_record(0,0)
        # = op_type=create——重 ID 两态对齐：FakeCASConflictError，写入不发生，
        # 已存记录不动（INV-2）。
        if record.record_id in self.main_records:
            raise FakeCASConflictError(
                f"main record {record.record_id!r} already exists "
                f"(create conflict at expected (0,0) first-create)"
            )
        self.main_records[record.record_id] = record
        # ES _seq_no 0 基：首写 (0,1)（create-only 语义，版本不二次推进）
        self.main_record_versions[record.record_id] = (0, 1)
        self.cas_calls.append({
            "action": "write_main_record",
            "record_id": record.record_id,
            "decision": record.decision,
            "audit_complete": record.audit_complete,
        })
        return record.record_id

    def write_audit(self, audit_id: str, doc: dict) -> str:
        # W2Fγ（A4 并案 WA4b-35①）：create-only——重 comparison_id 冲突即
        # FakeCASConflictError（对齐真层 RealESCommitStore.write_audit_documents
        # bulk create 冲突即错；旧静默覆盖可篡改审计证据）。已存文档不动。
        if audit_id in self.audit_docs:
            raise FakeCASConflictError(
                f"audit doc {audit_id!r} already exists "
                f"(create conflict; audit evidence is create-only)"
            )
        self.audit_docs[audit_id] = doc
        self.cas_calls.append({"action": "write_audit", "audit_id": audit_id})
        return audit_id

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

    def get_watermark(self, scope_id: str, business_date: str) -> int | None:
        """19:58 主窗口（方案 B 纯增量）：协议方法，读 watermark_seq dict 同义封装。"""
        return self.watermark_seq.get(f"{scope_id}|{business_date}")

    def reset(self) -> None:
        self.main_records.clear()
        self.watermark_seq.clear()
        self.audit_docs.clear()
        self.cas_calls.clear()
        self.watermark_calls.clear()
        self.main_record_versions.clear()


__all__ = ["FakeCommitStore", "FakeMainRecord"]