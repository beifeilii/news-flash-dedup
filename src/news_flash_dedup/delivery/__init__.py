"""P20 投递包：调度器 + 领取 CAS + 发送器 + fake 仓储（单元层）。"""

from . import coordinator, dispatcher, sender
from . import es_store  # noqa: F401  （20:52 主窗口落位：T4 真 ES 投递仓储）
from .coordinator import P20ReceiveError, compute_attempt_id, receive
from .dispatcher import scan_pending
from .fake_store import (
    DeliveryCASConflictError,
    FakeDeliveryLease,
    FakeDeliveryRecord,
    FakeDeliveryStore,
)
from .sender import (
    P20SendError,
    SendResult,
    advance_backoff,
    exhaust_round,
    expire_record,
    persist_next_delivery,
    reopen_round,
    send_callback,
)

__all__ = [
    "DeliveryCASConflictError",
    "FakeDeliveryLease",
    "FakeDeliveryRecord",
    "FakeDeliveryStore",
    "P20ReceiveError",
    "P20SendError",
    "SendResult",
    "advance_backoff",
    "compute_attempt_id",
    "coordinator",
    "dispatcher",
    "exhaust_round",
    "expire_record",
    "persist_next_delivery",
    "receive",
    "reopen_round",
    "scan_pending",
    "send_callback",
    "sender",
]