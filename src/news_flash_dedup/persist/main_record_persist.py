"""P18 main_record_persist：主记录 CAS 写入（fake_store）。

按 P18-设计-WIP §3 + 05:25 修订 2：`delivery_state` 首次创建为 `not_ready`，
CAS 成功翻 `pending`；CAS 失败路径不动 `delivery_state`（天然满足 INV-2）。
10-07 用户令（甲-i）：decision != "不重复" 的主记录 CAS 落 `held`（系统内
扣留不投递）；CAS 闸①②扩 {pending, held} 白名单（held 仅创建落点写入，
无运行时翻入路径）。
"""

from __future__ import annotations

import hashlib

from .fake_store import FakeCASConflictError, FakeMainRecordV1, FakeP18Store


def compute_callback_body_hash(callback_body: str) -> str:
    """callback_body 的 UTF-8 字节 sha256 hash。"""
    return hashlib.sha256(callback_body.encode("utf-8")).hexdigest()


def cas_main_record(store: FakeP18Store, record: FakeMainRecordV1, *,
                     expected_seq_no: int, expected_primary_term: int) -> int:
    """CAS 写主记录（fake_store 路径）；返回新 seq_no。

    校验：delivery_state ∈ {pending, held}（10-07 令甲-i 白名单）必须是
    CAS 成功才能写入；CAS 失败路径不动 delivery_state 字段。

    窗口M 伪 CAS 修复（窗口X 并线移植）：expected 版本与 store 当前版本
    对拍（旧实现从不比对，expected 形同虚设）。契约对齐真层
    persist/es_store.cas_main_record：(0,0) = 首创建约定（记录已存在 =
    create 冲突）；非 (0,0) = 更新，要求记录存在且 (seq_no, primary_term)
    精确相等。不符即 FakeCASConflictError，写入不发生、已存记录任何字段
    不动（INV-2）。

    窗口Z2（N2-01，外部审计三轮确认）：返回值 = 写入后自存版本的真实新
    seq_no（真 ES _seq_no 0 基语义）。原 expected_seq_no+1 在 (0,0) 首
    创建路径返回 1，与自存版本 (0,1) 差一（off-by-one）——调用方以返回
    值链式喂下一次 expected 必 FakeCASConflictError。
    """
    # 10-07 用户令（甲-i）：扣留件 held 亦经 CAS 创建落点写入——闸①单值
    # delivery_state=pending 扩 {pending, held} 双值白名单；闸②对称扩为
    # {pending,held} ⇒ task_state=succeeded（扣留件同为终态 succeeded）。
    # held 无运行时翻入路径（delivery 侧 mark_state/_DELIVERY_STATES 词表
    # 闸不扩——防误翻守卫，红测 G6-3/G6-4 双钉）。
    if record.delivery_state not in ("pending", "held"):
        raise ValueError(
            f"CAS main record requires delivery_state=pending|held "
            f"(放行/扣留白名单); got {record.delivery_state!r}"
        )
    if record.delivery_state in ("pending", "held") and record.task_state != "succeeded":
        raise ValueError(
            f"CAS main record with delivery_state=pending|held requires "
            f"task_state=succeeded; got {record.task_state!r}"
        )
    if not record.audit_complete:
        raise ValueError("audit_complete=False; CAS refused")
    if expected_seq_no == 0 and expected_primary_term == 0:
        # (0,0) = 首创建约定（真层两阶段内化的 fake 侧）：已存在 = create 冲突
        if record.record_id in store.main_records:
            raise FakeCASConflictError(
                f"main record {record.record_id!r} already exists "
                f"(create conflict at expected (0,0) first-create)"
            )
        store.write_main_record(record)
    else:
        current = store.main_record_versions.get(record.record_id)
        if current is None:
            raise FakeCASConflictError(
                f"main record {record.record_id!r} not found for CAS update"
            )
        if current != (expected_seq_no, expected_primary_term):
            raise FakeCASConflictError(
                f"main record {record.record_id!r} CAS conflict: "
                f"stored version {current} != expected "
                f"({expected_seq_no}, {expected_primary_term}) "
                f"(delivery_state untouched)"
            )
        # W2Fγ（ζ2）：版本对拍通过后走更新落点（write_main_record 已收紧为
        # create-only 重 ID 自查——创建/更新分途，守卫在本层）
        store.update_main_record(record)
    # 窗口Z2（N2-01）：回读自存版本返回真实新 seq_no——(0,0) 首创建自存
    # (0,1) 即返回 0（对齐真 ES 0 基与自存版本）；更新路径自存
    # (expected+1, term)，返回值与原 expected_seq_no+1 同值不变。
    new_version = store.main_version(record.record_id)
    if new_version is None:                      # 防御：write_main_record 必落版本
        raise RuntimeError(
            f"main record {record.record_id!r} version missing after write"
        )
    return new_version[0]


__all__ = ["cas_main_record", "compute_callback_body_hash"]