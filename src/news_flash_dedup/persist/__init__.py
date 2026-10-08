"""P18 包：ES 结果/outbox 单元层实现（fake_store + 真 ES 占位）。"""

from . import audit_persist
from . import coordinator
from . import es_store  # noqa: F401  （19:58 主窗口落位：T2 真 ES 接入层）
from . import main_record_persist
from .coordinator import P18PersistError, crash_recovery_scenarios, persist_main_record
from .fake_store import (
    FakeAuditDoc,
    FakeCASConflictError,
    FakeMainRecordV1,
    FakeP18Store,
)

__all__ = [
    "FakeAuditDoc",
    "FakeCASConflictError",
    "FakeMainRecordV1",
    "FakeP18Store",
    "P18PersistError",
    "audit_persist",
    "coordinator",
    "crash_recovery_scenarios",
    "main_record_persist",
    "persist_main_record",
]