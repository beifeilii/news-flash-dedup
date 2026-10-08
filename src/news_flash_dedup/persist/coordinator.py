"""P18 提交协调器：审计先存 + 主记录 CAS + watermark 推进 + 崩溃恢复三场景。

按 P18-设计-WIP §1 + §5：fake_store 路径先开工；真 ES UAT 与 P17-3 同队等凭据。
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import datetime, timezone

from news_flash_dedup.decide.types import DecideOutcome
from . import main_record_persist
from .fake_store import FakeMainRecordV1, FakeP18Store


class P18PersistError(RuntimeError):
    """P18 提交协调器不可恢复错误（审计不全 / CAS 冲突 / 准备快照过期）。"""


def _compute_event_id(scope_id: str, request_id: str,
                      result_version: int = 1) -> str:
    """event_id = SHA256(JCS([scope_id, request_id, result_version]))。

    三轮审计 C-02 修复（D25）：原 docstring 把违规公式（SHA256(record_id+
    completed_at)[:32] 字符串拼接）写成规格——10 §5 L50/L55 逐字不符，
    改为 JCS 单一事实源（admission._digest）。

    W2Fγ（A11 消费点声明）：跨模块私有复用 admission._digest 为有意
    单源合同（仓内 JCS 单一事实源）；导出方公共化/声明归 δ 窗
    （admission 面），本消费点逐字节依赖其 JCS 语义不变。"""
    from news_flash_dedup import admission as _admission
    return _admission._digest([scope_id, request_id, result_version])


def _build_callback_body(decide: DecideOutcome) -> str:
    """构造冻结回调载荷（DecideOutcome 五字段公共 dict 序列化）。"""
    return json.dumps(decide.to_public_dict(), sort_keys=True, ensure_ascii=False)


def persist_main_record(
    decide: DecideOutcome,
    store: FakeP18Store,
    *,
    scope_id: str = "default",
    business_date: str = "2026-09-26",
    arrival_seq: int,
    raw_hash: str,
    audit_ids: Iterable[str],
    audit_complete: bool,  # 窗口V（M-01 同标准）：去默认强制关键字显式表态
    fact_artifact_hash: str = "",
    pipeline_version: str = "dedup_v1",
    expected_seq_no: int = 0,
    expected_primary_term: int = 0,
    expires_at: str | None = None,   # P2 C-04/C-08（窗口X 并线）：None=现状（不钳制不透传）
    request_id: str | None = None,   # P2 P17-3 前置（窗口X 并线）：None=record_id 回退不变
) -> FakeMainRecordV1:
    """P18 提交协调器：审计先存 + 主记录 CAS + watermark 推进。

    流水线：
    1. 计算 callback_body + payload_hash；
    2. 构造 FakeMainRecordV1（delivery_state=pending 由 CAS 成功翻）；
    3. CAS 写主记录（前置：audit_complete 必须 True）；
    4. 推进 decision_watermark；
    5. 返回 FakeMainRecordV1（可由 P20 接管投递）。
    """
    if not audit_complete:
        raise P18PersistError("audit_complete=False; CAS refused (no write action)")

    callback_body = _build_callback_body(decide)
    callback_body_hash = main_record_persist.compute_callback_body_hash(callback_body)

    # W2Fγ（WA4b-35 双 now 收口）：completed_at 与 delivery_deadline_at
    # 同源单一 now——旧两次 datetime.now() 各取时刻，截止与完成时间存在
    # 微秒级不一致来源（语义无害但非单源）。
    now = datetime.now(timezone.utc)
    completed_at = now.isoformat()
    # D25（C-02）：event_id 输入改规格三元组；P2（P17-3 前置，窗口X 并线）
    # request_id 参数透传优先，缺省以本记录 record_id（占位身份）同源替代。
    placeholder_record_id = decide.item_id + "-" + str(arrival_seq)
    event_id = _compute_event_id(
        scope_id, request_id or placeholder_record_id, 1)
    # P2（C-04/C-08，窗口X 并线）：10 §6.337 钳制单源 deadline_iso——
    # expires_at 传入时 deadline=min(+24h, expires_at)；None=现状 now+24h
    # 逐字节不变（与原 _wmt_iso 输出同式，私有助手随移植退役）
    from news_flash_dedup.deadline import deadline_iso as _deadline_iso
    deadline = _deadline_iso(now, expires_at=expires_at)

    record = FakeMainRecordV1(
        record_id=placeholder_record_id,  # 占位 record_id
        item_id=decide.item_id,
        text=decide.text,
        scope_id=scope_id,
        business_date=business_date,
        arrival_seq=arrival_seq,
        raw_hash=raw_hash,
        pipeline_version=pipeline_version,
        fact_artifact_hash=fact_artifact_hash,
        vector_state="pending",            # 05:25 修订：记录首次创建时由创建者写 pending
        result_item_id=decide.item_id,
        result_decision=decide.decision,
        result_duplicate_ids=decide.duplicate_ids,
        result_reason=decide.reason,
        task_state="succeeded",
        completed_at=completed_at,
        result_version=1,
        event_id=event_id,
        audit_ids=tuple(audit_ids),
        audit_complete=audit_complete,
        reason_code=decide.internal_code,
        callback_body=callback_body,
        callback_body_hash=callback_body_hash,
        # 05:25 修订：CAS 成功翻 pending；首次 not_ready。
        # 10-07 用户令（甲-i，held-delivery-design.md）：decision != "不重复"
        # → held（系统内扣留不投递）；放行件仍 pending。
        delivery_state=("pending" if decide.decision == "不重复" else "held"),
        delivery_deadline_at=deadline,
        expires_at=expires_at or "",
        next_delivery_at=completed_at,
        callback_attempts=0,
        round_attempts=0,
        round=1,
    )

    main_record_persist.cas_main_record(
        store, record,
        expected_seq_no=expected_seq_no,
        expected_primary_term=expected_primary_term,
    )
    store.advance_watermark(scope_id, business_date, arrival_seq)

    return record


def crash_recovery_scenarios(store: FakeP18Store) -> dict:
    """崩溃恢复三场景（按 P18-设计-WIP §5）。

    返回当前 store 状态摘要（W2Fγ A6 实述三件计数）：
    - audit_persisted_only: 主记录未 CAS 但审计落库（布尔场景判）；
    - cas_ok_watermark_pending: 主记录 CAS 成功但 watermark 未推进（布尔场景判）；
    - full_committed: CAS + watermark + delivery_state=pending 都完成（布尔场景判）；
    - main_records / audit_docs / watermark_entries: fake 仓储三件实时
      计数（主记录数 / 审计文档数 / 水位键数，裸 len() 直读不做场景
      推断），供恢复路径断言对拍——三场景布尔即由这三件计数组合推出。
    """
    main_count = len(store.main_records)
    audit_count = len(store.audit_docs)
    watermark_count = len(store.watermark_seq)
    return {
        "audit_persisted_only": (audit_count > 0 and main_count == 0),
        "cas_ok_watermark_pending": (
            main_count > 0 and watermark_count == 0
        ),
        "full_committed": (
            main_count > 0 and watermark_count > 0 and audit_count > 0
        ),
        "main_records": main_count,
        "audit_docs": audit_count,
        "watermark_entries": watermark_count,
    }


__all__ = ["P18PersistError", "crash_recovery_scenarios", "persist_main_record"]