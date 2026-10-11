"""P0-T7 恢复指标聚合（只读派生面：受理日志+墓簿+重试运行时三源对拍）。

口径（主窗令五项——drift 报告批复版）：

1. ``tombstone_count``：窗口内已确认墓碑数（``scan_terminal_tombstones``
   全文扫描源；窗口=调用方所传墓碑覆盖面，单域单日运行即精确窗口）；
2. ``death_distribution``：死亡分布（墓碑按 ``error_code``/``failed_stage``
   /``attempt_count`` 三分布——重试耗尽≠永久，attempt 直方图即审计位）；
3. ``watermark_gap_skips``：水位跳洞（物化水位下经证明跨过的序号——
   墓碑终态证明+外国登记证明两源并计（读侧前沿口径），分计各报）；
4. ``breaker_opens``：T2 熔断开启次数（调用方读
   ``retry_runtime.breaker.open_count`` 注入；无 runtime=0）；
5. ``still_processing`` 口径（恒等式对账）：
   ``accepted == ready + tombstone_count + still_processing``
   - ``accepted`` = head ``last_allocated_seq``（受理日志面）；
   - ``ready`` = ``last_materialized_seq`` − 水位下墓碑跳洞数（跨洞计数
     口径：序号已跨=已收口；外国序号按其属主物化计入 ready——写路径
     所有权，不因读侧外国证明扣除）；
   - ``still_processing`` = head pending 条目数 + 隔离批序号区间长度
     （两个独立读面：pending 受理日志 + checkpoint.batch_quarantine）；
      P1a-T2 游标形头（pending 恒空壳）在途账目=调用方注入 work 索引
      ACTIVE 计数（``active_work_items``——纯函数不持检索端口）；
   - 恒等式**不是**构造恒真：四计数来自三个存储面（受理日志/墓簿/
     checkpoint），静息态（无 in-flight 批次收口）失衡=数据不一致需
     人工；迁移期（墓碑已落、批未收缩）失衡可见即中间态信号。

纪律：纯函数——不持端口不自读存储（调用方读齐三源再聚合）；head 形态
不识别即 ValueError（fail-closed：残缺 head 上的指标是垃圾数据）；本面
零写路径、零全局状态。
"""

from __future__ import annotations

from collections import Counter
from typing import Iterable

from .materialize_terminal import MaterializationTombstoneV1


def _head_counters(head_source: dict) -> tuple[int, int, int, int, bool]:
    """head 受理日志面提取（allocated/materialized/pending/quarantined/
    cursor_form 标记）。

    形态不识别 → ValueError（fail-closed——受理日志残缺时指标无意义）。
    P1a-T2 兼容读：游标形头（``last_terminal`` 在案）以游标为物化水位
    读源（过渡别名 ``last_materialized_seq`` 仅 legacy 头消费）；pending
    恒空壳 → pending_items=0（在途账目由调用方以 work 索引 ACTIVE 计数
    注入 ``active_work_items``——纯函数不持检索端口）。
    """
    if not isinstance(head_source, dict):
        raise ValueError("head source must be a dict")
    allocated = head_source.get("last_allocated_seq")
    if (type(allocated) is not int or allocated < 0):
        raise ValueError("head watermarks must be nonnegative ints")
    cursor_form = "last_terminal" in head_source
    if cursor_form:
        materialized = head_source.get("last_terminal")
    else:
        materialized = head_source.get("last_materialized_seq")
    if type(materialized) is not int or materialized < 0:
        raise ValueError("head watermarks must be nonnegative ints")
    pending = head_source.get("pending")
    if pending is None and cursor_form:
        # P1a-T4：新形游标头不落 pending（过渡空壳已摘）；在途账目由
        # active_work_items 注入（同旧形游标头口径）。
        pending_items = 0
    else:
        if not isinstance(pending, dict) or not isinstance(
                pending.get("batches"), list):
            raise ValueError("head pending log is malformed")
        pending_items = 0
        for batch in pending["batches"]:
            entries = batch.get("entries") if isinstance(batch, dict) else None
            if not isinstance(entries, list):
                raise ValueError("head pending batch is malformed")
            pending_items += len(entries)
    checkpoint = head_source.get("checkpoint", {})
    if not isinstance(checkpoint, dict):
        raise ValueError("head checkpoint is malformed")
    quarantine = checkpoint.get("batch_quarantine")
    quarantined_items = 0
    if quarantine is not None:
        if (not isinstance(quarantine, dict)
                or type(quarantine.get("first_seq")) is not int
                or type(quarantine.get("last_seq")) is not int):
            raise ValueError("head quarantine record is malformed")
        first, last = quarantine["first_seq"], quarantine["last_seq"]
        if first < 1 or last < first:
            raise ValueError("head quarantine sequence range is malformed")
        quarantined_items = last - first + 1
    return allocated, materialized, pending_items, quarantined_items, cursor_form


def recovery_metrics(
    head_source: dict,
    tombstones: Iterable[MaterializationTombstoneV1],
    *,
    foreign_seqs: Iterable[int] = (),
    breaker_opens: int = 0,
    active_work_items: int | None = None,
) -> dict:
    """P0-T7 恢复指标聚合（纯函数；详见模块 docstring 五项口径）。

    - ``tombstones``：窗口已确认墓碑（``scan_terminal_tombstones`` 结果
      或其并集；恒等式精确性要求覆盖 head 分配窗口——单域单日运行天然
      满足）；
    - ``foreign_seqs``：登记证明的他域/日序号（读侧前沿跳洞凭据之二；
      默认空=单窗口）；
    - ``breaker_opens``：T2 熔断开启计数（无 runtime 传 0）；
    - ``active_work_items``：P1a-T2 游标形头专用——work 索引 ACTIVE 任务
      计数（调用方检索注入；缺省且头为游标形 → ValueError：在途账目无
      读源，fail-closed 不猜）。legacy 头忽略本参数（pending 日志在案）。
    """
    if type(breaker_opens) is not int or breaker_opens < 0:
        raise ValueError("breaker_opens must be a nonnegative int")
    accepted, materialized, pending_items, quarantined_items, cursor_form = \
        _head_counters(head_source)
    if cursor_form:
        if active_work_items is None:
            raise ValueError(
                "cursor-form head requires active_work_items "
                "(work-index ACTIVE task count) for still_processing")
        if (type(active_work_items) is not int or active_work_items < 0):
            raise ValueError("active_work_items must be a nonnegative int")
    window: list[MaterializationTombstoneV1] = []
    seen_seqs: set[int] = set()
    for tombstone in tombstones:
        if not isinstance(tombstone, MaterializationTombstoneV1):
            raise ValueError(
                "tombstones must be MaterializationTombstoneV1 entries "
                "(scan_terminal_tombstones output)")
        seq = tombstone.arrival_seq
        if not 1 <= seq <= accepted or seq in seen_seqs:
            continue        # 窗口外/重放条目不双计（首证为准）
        seen_seqs.add(seq)
        window.append(tombstone)
    terminal_below = sorted(seq for seq in seen_seqs if seq <= materialized)
    foreign = sorted({seq for seq in foreign_seqs
                      if type(seq) is int and 1 <= seq <= accepted})
    foreign_below = [seq for seq in foreign if seq <= materialized]
    ready = materialized - len(terminal_below)
    still_processing = ((active_work_items if cursor_form else pending_items)
                        + quarantined_items)
    return {
        # 五项主口径（主窗令）：
        "tombstone_count": len(window),
        "death_distribution": {
            "by_error_code": dict(Counter(
                t.error_code for t in window)),
            "by_failed_stage": dict(Counter(
                t.failed_stage for t in window)),
            "by_attempt_count": dict(Counter(
                t.attempt_count for t in window)),
        },
        "watermark_gap_skips": {
            "count": len(terminal_below) + len(foreign_below),
            "seqs": sorted(set(terminal_below) | set(foreign_below)),
            "tombstone": len(terminal_below),
            "foreign": len(foreign_below),
        },
        "breaker_opens": breaker_opens,
        "still_processing": still_processing,
        # 恒等式对账（accepted == ready + tombstone + still_processing）：
        "accepted": accepted,
        "ready": ready,
        "accounting_balanced": (
            accepted == ready + len(window) + still_processing),
        # 佐证位（独立读面原值——对账失衡时定位哪一面漂了）：
        "materialized_seq": materialized,
        "pending_items": pending_items,
        "quarantined_items": quarantined_items,
    }


__all__ = ["recovery_metrics"]
