"""N38（挂账分流核对表 N38 行，B4′ F-4）：真层**重试分歧**双契约异常。

修法①（仓储边界包装归一，修复令二选一之①）：真层分歧异常包入
CommitOneError 语义子型——worker.py:416-417 仅捕 CommitOneError 的
捕获面下，"重试遇分歧"从**未捕获冒泡响亮死**转为**受控 failed 停+
台账可辨**；同时保持原契约类型子型（N36 既有钉面零改动）。

独立成模块的原因（导入环实证，非风格选择）：子型须同时继承
commit.coordinator.CommitOneError 与 es_store 两异常，而 commit 包加载
链 commit/fake_store.py:20 → persist/__init__:5 → es_store——es_store
模块级回引 commit 即真环（partially initialized module ImportError，
落盘 n38-fix-green-1201.txt 实证）。故 es_store 仅在分歧 raise 处
惰性导入本模块（调用期全模块已就绪），本模块模块级正常继承。

边界语义（修复令+实现判断登记）：
- 仅"重试遇分歧"预期运维态（崩溃重放撞上内容相异既有记录/审计文档）
  使用本模块两型；
- **真并发 CAS 冲突=多写者破隔离**：保持裸 CASConflictError 冒泡响亮死
  不静默（单飞纪律正确语义，修法②会把该型一并吞成 failed 而掩盖破
  隔离——选①不选②的核心依据）；
- p18"失败即停"原路（write_audit_batch 首次 bulk 失败、核验读不可用、
  既有文档缺失）保持裸 AuditPersistError 不变——契约钉
  test_p18_commit_one_audit_contract 零改动。
"""

from __future__ import annotations

from news_flash_dedup.commit.coordinator import CommitOneError
from news_flash_dedup.persist.es_store import (
    AuditPersistError,
    CASConflictError,
)


class MainRecordDivergenceError(CommitOneError, CASConflictError):
    """真层主记录重试分歧：worker 捕获面（CommitOneError）∧ N36 钉面
    （CASConflictError）双契约子型。消息带分歧稳定键明细（fail-closed）。"""


class AuditDivergenceError(CommitOneError, AuditPersistError):
    """真层审计重试分歧：worker 捕获面（CommitOneError）∧ N36/p18 钉面
    （AuditPersistError）双契约子型。消息带分歧键明细（fail-closed）。"""


__all__ = [
    "AuditDivergenceError",
    "MainRecordDivergenceError",
]
