"""delivery_deadline_at 钳制与 expires_at 解析单源（P2 工程债 C-04/C-08）。

规格出处（10 文《数据存储、索引与生命周期设计》V1）：
- §6.337：终态 CAS 保存 ``delivery_deadline_at=min(completed_at+24小时, expires_at)``；
- §6.340：调度、领取和实际发送前都检查 ``now < delivery_deadline_at``
  且 ``now < expires_at``。

三轮审计 D25-2 登记的结构性缺口：min 钳制原先仅 g0_persistence.py 两处
（L442/L478）有，persist/es_store.py、persist/coordinator.py、
commit/coordinator.py 三处写入路径无钳制且不透传 expires_at。本模块是
三层共用的最小单源：调用方传 expires_at（可选，None/空串 = 现状行为不钳制），
由本模块统一解析与取 min。

与 g0_persistence._time 的差异：g0 系固定工件探针纪律，要求偏移恰为 UTC 零；
本模块接受任意 aware 偏移并归一到 UTC（瞬间语义），naive 一律 ValueError
（fail-fast，与 g0 同向不放宽）。

窗口X 并线：本模块自 P2 副本语义移植（逐字节同一），主仓原无此单源。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

__all__ = ["clamp_deadline", "deadline_iso", "parse_utc_iso"]


def parse_utc_iso(value: str) -> datetime:
    """ISO8601（容许 Z 后缀与任意 aware 偏移）→ UTC aware datetime。

    naive 时间戳无法判定瞬间，fail-fast（ValueError）。
    """
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(
            f"timestamp must be timezone aware (naive rejected): {value!r}"
        )
    return parsed.astimezone(timezone.utc)


def clamp_deadline(now: datetime, *, hours: int = 24,
                   expires_at: str | None = None) -> datetime:
    """delivery_deadline = min(now + hours, expires_at)。

    expires_at 为 None 或空串 → 不钳制（现状行为：now + hours）。

    W2Fγ（WA4b-35⑤）：naive now fail-fast（潜伏收口）——旧对 naive now
    静默产 naive 截止（仓内纪律 aware-only；expires_at 参与比较时还会
    抛 naive/aware 混比 TypeError 逃逸），与 parse_utc_iso naive 拒同向
    补闸（W1Fβ 族 fail-fast 纪律）。
    """
    if now.tzinfo is None:
        raise ValueError(
            f"now must be timezone aware (naive rejected): {now!r}"
        )
    deadline = now + timedelta(hours=hours)
    if expires_at:
        expires = parse_utc_iso(expires_at)
        if expires < deadline:
            deadline = expires
    return deadline


def deadline_iso(now: datetime, *, hours: int = 24,
                 expires_at: str | None = None) -> str:
    """clamp_deadline 的 ISO8601 文本形态（UTC，+00:00 后缀）。"""
    return clamp_deadline(now, hours=hours, expires_at=expires_at).isoformat()
