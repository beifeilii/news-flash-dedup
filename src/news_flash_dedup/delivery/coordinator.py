"""P20 领取 CAS：含 payload_hash + route_ref 未变 + 代次隔离 + attempt_id 派生。

按 10 §6.5 第 2 步 + P20-设计-WIP §3：
- 前置条件（payload_hash + route_ref + round_attempts < 12 + 截止）任一不满足 → ValueError
- CAS 原子更新：delivery_state=delivering、generation+=1、attempt_id 新值、
  round 沿用当前轮（rec.round，不递增——轮次推进由 reopen_round 承担；
  窗口Z2 实述更正：本 docstring 原写 "round++" 与 L123 round=rec.round
  矛盾，以代码现役行为为准）
- attempt_id = SHA256(JCS([event_id, generation]))（按 10 §6.5 第 2 步）
- 代次隔离：旧 owner 发现换代立即丢弃旧结果

窗口Z2（H-05，主窗口追裁解禁）：receive 的 now 缺省 "" → 缺省取当前时——
deadline/expires_at/backoff 三闸永远执法（10 §6.340 领取同查）；旧"缺省
零校验"兼容形态（C-09 注记）自此废止，显式传 now 行为逐字节不变。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from news_flash_dedup import admission as _admission

from .fake_store import FakeDeliveryLease, FakeDeliveryStore


class P20ReceiveError(ValueError):
    """P20 领取 CAS 拒绝（payload_hash / route_ref / round_attempts / 截止 等）。"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_iso_utc(value: str) -> datetime:
    """读侧 ISO 串归一（窗口W1Fβ F-02，W1 确认/主窗口对码）。

    历史裸串（tzinfo is None）按 UTC 解释——读宽写严分工：写侧
    deadline.py 已 naive fail-fast（仓内新写入必 aware），本函数只服务
    读侧存量/外部数据；aware 串一律 astimezone(UTC) 归一。归一后三闸
    （deadline/expires_at/next_delivery_at）与 now 比较永远同型
    （aware-UTC vs aware-UTC），naive/aware 混比 TypeError 不再逃逸；
    aware 串比较语义与归一前逐字节一致（等瞬比较与时区表示无关）。

    W2Fγ（A11）：升公共名——本函数是 delivery 域读侧归一单一事实源，
    跨模块复用合同显式化（消费点：delivery/es_store.update_lease 闸、
    delivery/dispatcher.scan_pending）；私有别名 _parse_iso_utc 保留
    逐字节兼容（既有钉/test 引用不断）。
    """
    ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


# A11 兼容别名（公共名 = parse_iso_utc；见其 docstring 复用合同声明）
_parse_iso_utc = parse_iso_utc


def _parse_gate_ts(value: str, field: str) -> datetime:
    """三闸时间戳解析（W2Fγ A9+A10 时区分类学收口）：畸形串 fail-closed
    包 P20ReceiveError——裸 ValueError/TypeError 不再逃逸声明通道（与
    es_store.update_lease 的 P20ClaimPreconditionError 域分工：fake/sealed
    侧=P20ReceiveError，仓储侧=P20ClaimPreconditionError）。"""
    try:
        return parse_iso_utc(value)
    except (ValueError, TypeError, AttributeError) as err:
        raise P20ReceiveError(f"{field} unparsable: {value!r}") from err


def compute_attempt_id(event_id: str, generation: int) -> str:
    """attempt_id = SHA256(JCS([event_id, generation]))（10 §6.5 第 2 步，全 64hex）。

    D25/C-07 行为修正（窗口X 并线移植）：JCS 序列化复用 admission._digest
    （仓内 JCS 单一事实源，separators=(",", ":") 最简分隔符 + 代理码位拒绝），
    替代本地 json.dumps 默认分隔符（", "/": "）——二者字节不同，attempt_id
    实际派生值因此变更，属规格合规修正（钉值测试同步更新，见
    test_p20_implementation.py / test_p2_hardening.py / test_p20_uat.py 锚点）。

    主窗口追裁（对 10 文原文）：§6.5 规格为 SHA256(JCS(...)) **无截断**——
    旧实现与本函数 docstring 原写的 [:32] 系 delivery 层自造（g0 真层
    g0_persistence.py:603 用全 64hex 与规格一致）；本批去除截断，跨层
    口径对齐（B-3 遗留①随之消解）。

    W2Fγ（A11 消费点声明）：跨模块私有复用 admission._digest 为有意
    单源合同（仓内 JCS 单一事实源）；导出方公共化/声明归 δ 窗
    （admission 面），本消费点逐字节依赖其 JCS 语义不变。
    """
    return _admission._digest([event_id, generation])


def receive(
    store: FakeDeliveryStore, record_id: str, *, payload_hash: str,
    route_ref: str, expected_generation: int, event_id: str,
    now: str = "",
) -> FakeDeliveryLease:
    """领取 CAS：原子更新 + 返回新 lease。

    前置条件：
    - record.delivery_state ∈ {pending, delivering}
    - record.round_attempts < 12
    - record.delivery_deadline_at > now（字段非空时；W2Fγ A15 空串跳闸
      对齐真层）且 now < expires_at（字段非空时）
    - record.next_delivery_at <= now（退避已 elapsed）
    - record.payload_hash == payload_hash
    - record.route_ref == route_ref

    窗口Z2（H-05，主窗口追裁）：now 缺省 "" → 以当前时刻执法——三闸
    （deadline/expires_at/backoff）永远生效，不再存在"缺省零校验"路径。
    """
    # 窗口M（_last_claim_read 显式化，窗口X 并线移植）：真 ES 仓储的领取
    # 上下文只在 claim 路径写——本函数是 claim 路径，经视图 claim_read
    # 显式读取（普通读不写上下文，防并发污染）；fake dict 视图无此方法，
    # 回退下标读（fake 路径行为逐字节不变）。
    view = store.main_records
    claim_read = getattr(view, "claim_read", None)
    rec = claim_read(record_id) if callable(claim_read) else view[record_id]
    if rec.delivery_state not in {"pending", "delivering"}:
        raise P20ReceiveError(
            f"delivery_state must be pending|delivering; got {rec.delivery_state!r}"
        )
    if rec.round_attempts >= 12:
        raise P20ReceiveError(
            f"round_attempts {rec.round_attempts} >= 12"
        )
    if rec.payload_hash != payload_hash:
        raise P20ReceiveError(
            f"payload_hash mismatch: stored={rec.payload_hash!r} given={payload_hash!r}"
        )
    if rec.route_ref != route_ref:
        raise P20ReceiveError(
            f"route_ref mismatch: stored={rec.route_ref!r} given={route_ref!r}"
        )
    # P2 工程债 C-09（窗口X 并线移植）：对照真层 claim_delivery
    # （g0_persistence.py L592-598）补齐 fake 侧缺漏——deadline/
    # expires/backoff 三闸原本层不执法（旧 docstring 称"由 caller
    # 保证"）。
    # 窗口Z2（H-05，主窗口追裁）：now 缺省（""）→ 取当前时刻——三闸
    # 永远执法（旧"仅 now 显式提供时校验、缺省零校验"兼容形态废止）；
    # 显式传 now 行为逐字节不变。deadline/expires_at 两闸顺序判定、
    # 拒绝语义等价 min（now ≥ min(deadline, expires_at) ⇔ now ≥ 任一，
    # 先撞哪闸仅影响报错文案）（真层 _deadline L551-553；10 §6.340
    # 调度/领取/发送前都检查）。
    # 窗口W1Fβ（F-02）：三闸与 now 共用 parse_iso_utc 归一——naive 按
    # UTC 解释、aware 归一 UTC，比较永远同型（TypeError 不再逃逸）。
    # 窗口W2Fγ（A9+A10 时区分类学收口）：三闸与 now 的畸形串经
    # _parse_gate_ts 一律 fail-closed 包 P20ReceiveError（旧裸 ValueError
    # 逃逸面收口；es_store.update_lease 同族硬化见 P20ClaimPreconditionError
    # 域）。
    # 窗口W2Fγ（A15）：delivery_deadline_at 空串跳过闸——对齐真层
    # update_lease"缺失/空串 = 存量历史数据不拦"（fake 保真；旧 fake
    # 对空串解析崩溃与真层两态）。
    effective_now = now or _now_iso()
    now_ts = _parse_gate_ts(effective_now, "now")
    if rec.delivery_deadline_at:
        deadline = _parse_gate_ts(rec.delivery_deadline_at,
                                  "delivery_deadline_at")
        if now_ts >= deadline:
            raise P20ReceiveError(
                f"delivery_deadline_at passed: {rec.delivery_deadline_at!r} "
                f"<= now {effective_now!r}"
            )
    if rec.expires_at:
        expires = _parse_gate_ts(rec.expires_at, "expires_at")
        if now_ts >= expires:
            raise P20ReceiveError(
                f"expires_at passed: {rec.expires_at!r} <= now {effective_now!r}"
            )
    if rec.next_delivery_at:
        next_at = _parse_gate_ts(rec.next_delivery_at, "next_delivery_at")
        if now_ts < next_at:
            raise P20ReceiveError(
                f"callback backoff has not elapsed: next_delivery_at "
                f"{rec.next_delivery_at!r} > now {effective_now!r}"
            )

    new_generation = expected_generation + 1
    new_attempt_id = compute_attempt_id(event_id, new_generation)
    lease = FakeDeliveryLease(
        owner_id=f"worker-{new_generation}",
        owner_generation=new_generation,
        lease_until=effective_now,
        attempt_id=new_attempt_id,
        round=rec.round,
        result_version=1,
        event_id=event_id,
        payload_hash=payload_hash,
        route_ref=route_ref,
        record_id=record_id,     # W-A 族③(e)-2：租约自证归属（A4-D1）
    )
    store.update_lease(lease)
    store.mark_state(record_id, "delivering")
    return lease


__all__ = ["P20ReceiveError", "compute_attempt_id", "parse_iso_utc", "receive"]