"""P20 fake 仓储接口（单元层注测，不连真 ES / 不连真回调 URL）。"""

from __future__ import annotations

from dataclasses import dataclass, field

from news_flash_dedup.deadline import parse_utc_iso


@dataclass(frozen=True)
class FakeDeliveryLease:
    """fake 投递租约（含 P18 写入字段组 + P19/P20 翻转字段）。

    W-A 族③(e)（A4-D1）：第 10 字段 record_id——租约自证归属（真层
    update_lease 落点以 `lease.record_id or _last_claim_read` 解析，交错
    claim 不再错写）；默认空串=过渡兼容（现役构造点零改动即绿；全构造
    点迁移毕后收紧非空闸=挂账登记过渡态收口项，本波不收紧）。
    """
    owner_id: str
    owner_generation: int
    lease_until: str
    attempt_id: str
    round: int
    result_version: int
    event_id: str
    payload_hash: str
    route_ref: str
    record_id: str = ""


@dataclass(frozen=True)
class FakeDeliveryRecord:
    """fake 主记录投递字段组（按 P18 §3.1 + P19 §5.1 + P20 §3.1）。"""
    record_id: str
    item_id: str
    scope_id: str
    business_date: str
    arrival_seq: int
    # "not_ready" / "pending" / "delivering" / "delivered" / "exhausted" / "expired"
    # + "held"（10-07 令甲-i 新增：创建落点扣留态，decision≠不重复 由三写入点
    # 分派；mark_state 五态闸不扩 held——无运行时翻入路径，防误翻守卫）
    delivery_state: str
    delivery_deadline_at: str
    next_delivery_at: str
    callback_attempts: int
    round_attempts: int
    round: int
    callback_body: str
    payload_hash: str
    route_ref: str                    # P20 §3.1：CAS 校验锁定
    vector_state: str                  # P19 字段：pending / ready / failed / expired
    audit_complete: bool
    # P2 工程债 C-08（窗口X 并线移植）：expires_at 透传通道（默认空串 =
    # 现状/构造兼容；非空时 dispatcher scan_pending 按 10 §6.340 一并校验
    # now < expires_at）
    expires_at: str = ""


def _validate_lease(lease: FakeDeliveryLease) -> None:
    """fake 租约前置校验（P2 工程债 C-09，窗口X 并线移植）：对齐
    g0_persistence 真层纪律。

    原实现零校验直写——非法租约会喂给 sealed receive/下游断言制造假绿。
    规则出处（g0_persistence.py，不发明规则）：
    - owner_id 非空 str：L544（活跃租约 owner 必须非空 str）+ L68-69（显式
      owner 纪律）；
    - owner_generation 为 int 且 >= 1：L526/L545（活跃租约代次非零；
      claim_delivery L599-603 代次恒为 prev+1 >= 1）；
    - attempt_id 非空 str：L537-540（活跃租约 attempt_id 恒等于派生摘要，
      非空）。派生值全量对拍（event_id+generation 重算）留 P17-3；截断口径
      差已消解（主窗口追裁：10 §6.5 无截断，delivery 与 g0 同为全 64hex，
      见 coordinator.compute_attempt_id docstring——W2Fγ A8 注释更新）；
    - lease_until 为合法 ISO（aware，naive 拒）：复用 deadline.parse_utc_iso
      单源（P2 C-04/C-08 遗产）。
      ★"晚于现在"显式说明（不校验的理由）：真层 _delivery/_settle 对存量
      租约只要求时间戳合法且不超过 deadline——"此刻是否仍活"是恢复/续期
      路径的运行时判定（recover_delivery L629-631），过期租约是可恢复的
      合法状态而非非法数据；且 fake receive() 默认以当前时刻为 lease_until，
      写时强判"晚于现在"会误伤合法默认路径。故本层只执法良构性。
    """
    if not isinstance(lease.owner_id, str) or not lease.owner_id:
        raise ValueError(
            f"lease owner_id must be a non-empty str; got {lease.owner_id!r}"
        )
    if type(lease.owner_generation) is not int or lease.owner_generation < 1:
        raise ValueError(
            f"lease owner_generation must be int >= 1; "
            f"got {lease.owner_generation!r}"
        )
    if not isinstance(lease.attempt_id, str) or not lease.attempt_id:
        raise ValueError(
            f"lease attempt_id must be a non-empty str; "
            f"got {lease.attempt_id!r}"
        )
    try:
        parse_utc_iso(lease.lease_until)
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError(
            f"lease lease_until must be timezone-aware ISO8601; "
            f"got {lease.lease_until!r}"
        ) from exc
    # W-A 族③(e)-1：record_id 空串=合法过渡态（不强校验非空）；非空时
    # 要求 str（type 闸，防 int/None 漂入落点解析）。
    if not isinstance(lease.record_id, str):
        raise ValueError(
            f"lease record_id must be a str (empty allowed in transition); "
            f"got {lease.record_id!r}"
        )


class DeliveryCASConflictError(RuntimeError):
    """fake 投递主记录 CAS 版本竞争冲突（窗口M 伪 CAS 修复，窗口X 并线移植）。

    对齐真层 delivery/es_store.P20CASConflictError 语义：快照版本与 store
    当前版本不符（并发/过期写）→ 拒绝，写入不发生，已存记录不动。
    """


@dataclass
class FakeDeliveryStore:
    """fake 主记录 + 投递仓储（pure dict 状态）。

    窗口M（窗口X 并线）：补 versions 版本跟踪（ES _seq_no 0 基语义：首写 0，
    每写 +1）——旧实现零版本概念，persist_next_delivery 读-改-写裸 dict
    赋值无版本条件（伪 CAS）。所有主记录写方法（upsert_main/mark_state/
    advance_round/cas_write_main）经 _bump 单点推进。
    """
    main_records: dict[str, FakeDeliveryRecord] = field(default_factory=dict)
    lease_index: dict[str, FakeDeliveryLease] = field(default_factory=dict)
    cas_calls: list[dict] = field(default_factory=list)
    send_calls: list[dict] = field(default_factory=list)
    versions: dict[str, int] = field(default_factory=dict)

    def _bump(self, record_id: str) -> int:
        prev = self.versions.get(record_id)
        self.versions[record_id] = 0 if prev is None else prev + 1
        return self.versions[record_id]

    def upsert_main(self, record: FakeDeliveryRecord) -> str:
        self.main_records[record.record_id] = record
        self._bump(record.record_id)
        return record.record_id

    def read_main_for_cas(self, record_id: str) -> tuple[FakeDeliveryRecord, int]:
        """读-改-写路径的一致性快照 (记录, 版本)（窗口M，窗口X 并线）。

        对应真 ES GET 同时返回 _source 与 _seq_no；fake 单线程内两字段
        同读即一致。快照后任何主记录写都会推进版本，使后续
        cas_write_main(expected_version=快照版本) 检出过期。
        """
        return self.main_records[record_id], self.versions[record_id]

    def cas_write_main(self, record: FakeDeliveryRecord, *,
                       expected_version: int) -> None:
        """版本对拍写：store 当前版本 != expected → 拒（写不发生）。

        persist_next_delivery 的 fake 落点（窗口M，窗口X 并线）；语义对齐
        真层 _cas_write 的 if_seq_no/if_primary_term。
        """
        current = self.versions.get(record.record_id)
        if current is None or record.record_id not in self.main_records:
            raise DeliveryCASConflictError(
                f"main record {record.record_id!r} not found for CAS write"
            )
        if current != expected_version:
            raise DeliveryCASConflictError(
                f"main record {record.record_id!r} CAS conflict: "
                f"stale expected version {expected_version}, "
                f"current {current} (record untouched)"
            )
        self.main_records[record.record_id] = record
        self.versions[record.record_id] = current + 1

    def update_lease(self, lease: FakeDeliveryLease) -> str:
        _validate_lease(lease)          # C-09（窗口X 并线）：非法租约 fail-fast（对齐真层）
        key = lease.owner_id
        self.lease_index[key] = lease
        self.cas_calls.append({
            "action": "update_lease",
            "owner_id": lease.owner_id,
            "generation": lease.owner_generation,
            "attempt_id": lease.attempt_id,
            "record_id": lease.record_id,     # 族③(e)-4：additive 观测面
        })
        return key

    def advance_round(self, record_id: str, next_round: int) -> None:
        record = self.main_records[record_id]
        new = FakeDeliveryRecord(
            record_id=record.record_id, item_id=record.item_id,
            scope_id=record.scope_id, business_date=record.business_date,
            arrival_seq=record.arrival_seq,
            delivery_state="pending",
            delivery_deadline_at=record.delivery_deadline_at,
            next_delivery_at=record.next_delivery_at,
            callback_attempts=record.callback_attempts,
            round_attempts=0,
            round=next_round,
            callback_body=record.callback_body,
            payload_hash=record.payload_hash,
            route_ref=record.route_ref,
            vector_state=record.vector_state,
            audit_complete=record.audit_complete,
            expires_at=record.expires_at,
        )
        self.main_records[record_id] = new
        self._bump(record_id)

    def mark_state(self, record_id: str, state: str) -> None:
        if state not in {"pending", "delivering", "delivered", "exhausted", "expired"}:
            raise ValueError(f"invalid delivery_state {state!r}")
        old = self.main_records[record_id]
        self.main_records[record_id] = FakeDeliveryRecord(
            record_id=old.record_id, item_id=old.item_id,
            scope_id=old.scope_id, business_date=old.business_date,
            arrival_seq=old.arrival_seq,
            delivery_state=state,
            delivery_deadline_at=old.delivery_deadline_at,
            next_delivery_at=old.next_delivery_at,
            callback_attempts=old.callback_attempts,
            round_attempts=old.round_attempts,
            round=old.round,
            callback_body=old.callback_body,
            payload_hash=old.payload_hash,
            route_ref=old.route_ref,
            vector_state=old.vector_state,
            audit_complete=old.audit_complete,
            expires_at=old.expires_at,
        )
        self._bump(record_id)

    def lease_for(self, record_id: str) -> FakeDeliveryLease | None:
        """W-A 族③(f)-2：按 record_id 反查租约（scan_expired_delivering
        租约来源；依赖族③(e) record_id 入租约——空串过渡租约不匹配任何
        记录，属"无租约"跳闸面）。"""
        for lease in self.lease_index.values():
            if lease.record_id and lease.record_id == record_id:
                return lease
        return None

    def reset(self) -> None:
        self.main_records.clear()
        self.lease_index.clear()
        self.cas_calls.clear()
        self.send_calls.clear()
        self.versions.clear()


__all__ = [
    "DeliveryCASConflictError",
    "FakeDeliveryLease",
    "FakeDeliveryRecord",
    "FakeDeliveryStore",
]