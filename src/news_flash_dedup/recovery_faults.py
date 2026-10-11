"""P0-T7 故障注入钩子（诊断/模拟专用面；域层零生产装配）。

四钩子（主窗令——drift 报告批复版，Scenario A/B/C 模拟的注入源）：

- **指定序号永死**（``arm_permanent_death``）：该序号条目的 item 文档
  按确定性 4xx（400）收口 → ITEM_PERMANENT → 墓碑（唯一入口铁律不破）；
  批内其余条目照常（1 坏件不杀整批）。``main_record=True``=混合形态
  （主记录真实写入——场景 C 同批变体：墓碑权威+即时补正）；
  ``main_record=False``=纯死形态（主记录不写——迟到成功变体的前置）；
- **60 秒级断连**（``arm_disconnect``）：注入时钟窗内的 bulk 全部按
  传输级异常拒绝（请求级 OSError → TRANSIENT_INFRA → 熔断/退避账；
  Scenario A：受理照常持久化、水位不推进、零墓碑）；
- **未知写入**（``arm_unknown_write``）：该序号按 409 收口——
  ``written=True``=丢失回执（写入真实成功，读回对拍自愈，零二次物化）；
  ``written=False``=未确认（读回未命中，保持重试不定永久）；
- **迟到成功**（``arm_late_success`` + ``deliver_late_success``）：纯死
  形态入墓后，把当时的主记录体**迟到落库**（模拟失联重试最终抵达
  ES）；补正走场景 C 确定规则（``reconcile_late_materialization``——
  CAS 翻 ``task_state="tombstoned"``，不删除不复活不二次物化）。

纪律：
- 只在 base store 提供 ``bulk_create_classified`` 逐条解析面时才挂
  ``bulk_create_classified``（条件实例挂接——probe_g0_recovery.py
  FaultStore 同型承重机制；base 无该面 → 走既有 legacy 路径零扰动）；
- 协议面（get/create/replace/mget/is_conflict）显式透传，其余属性
  ``__getattr__`` 透传 base（薄壳，不复制不缓存存储态）；
- 域层零全局时钟：断连窗需要注入时钟（构造参数 ``clock``）；计数面
  ``calls`` 只做审计观测。
"""

from __future__ import annotations

from typing import Callable


class RecoveryFaultInjector:
    """P0-T7 故障注入钩子（wrap 一个 BatchStore 形态 base；诊断/模拟专用）。"""

    def __init__(self, base, *, clock: Callable[[], float] | None = None) -> None:
        self._base = base
        self._clock = clock
        # 指定序号永死：seq -> {"main_record": bool}
        self._permanent: dict[int, dict] = {}
        # 未知写入：seq -> {"written": bool}
        self._unknown: dict[int, dict] = {}
        # 迟到成功待投递：seq -> (index, key, body)（纯死形态捕获的主记录）
        self._late_pending: dict[int, tuple] = {}
        # 断连窗：(seconds, deadline|None)；首次 bulk 起算。
        self._disconnect_seconds: float | None = None
        self._disconnect_deadline: float | None = None
        # 审计观测面（只增计数）。
        self.calls = {"bulk": 0, "disconnect_refused": 0,
                      "item_faults": 0, "late_delivered": 0}
        if callable(getattr(base, "bulk_create_classified", None)):
            self.bulk_create_classified = self._fault_bulk_classified

    # ---------- 钩子装配（可重复装配/解除；seq 级幂等覆盖） ----------

    def arm_permanent_death(self, seqs, *, main_record: bool = True) -> None:
        """指定序号永死（item 文档 400→ITEM_PERMANENT→墓碑）。

        ``main_record=True``：主记录照写（混合形态——同批即补正）；
        ``False``：主记录不写（纯死——配合 ``arm_late_success``）。
        """
        for seq in seqs:
            if type(seq) is not int or seq < 1:
                raise ValueError("permanent death seqs must be positive ints")
            self._permanent[seq] = {"main_record": main_record}

    def arm_disconnect(self, seconds: float) -> None:
        """断连窗（秒；从**下一次** bulk 起算——注入时钟域）。

        需要构造时注入 ``clock``（域层零全局时钟纪律）；窗内 bulk 按传输
        级 OSError 拒绝（请求级 → TRANSIENT_INFRA）。
        """
        if self._clock is None:
            raise ValueError("disconnect window requires an injected clock")
        if not isinstance(seconds, (int, float)) or seconds <= 0:
            raise ValueError("disconnect seconds must be positive")
        self._disconnect_seconds = float(seconds)
        self._disconnect_deadline = None

    def arm_unknown_write(self, seqs, *, written: bool = True) -> None:
        """未知写入（409→读回对拍；``written``=写入是否真实发生）。"""
        for seq in seqs:
            if type(seq) is not int or seq < 1:
                raise ValueError("unknown write seqs must be positive ints")
            self._unknown[seq] = {"written": written}

    def arm_late_success(self, seqs) -> None:
        """迟到成功预置（该序号走纯死形态并捕获主记录体，待投递）。"""
        for seq in seqs:
            if type(seq) is not int or seq < 1:
                raise ValueError("late success seqs must be positive ints")
            if seq not in self._permanent:
                self._permanent[seq] = {"main_record": False}
            else:
                self._permanent[seq]["main_record"] = False

    def disarm(self) -> None:
        """解除全部钩子（恢复透传；计数面保留审计）。"""
        self._permanent.clear()
        self._unknown.clear()
        self._late_pending.clear()
        self._disconnect_seconds = None
        self._disconnect_deadline = None

    def deliver_late_success(self, seq: int) -> bool:
        """投递迟到成功（把纯死时捕获的主记录体迟到落库）。

        返回 True=已投递；False=无待投递（未预置/已投递/已被他方写入
        ——幂等失败不重复写）。落库后补正由调用方走
        ``reconcile_late_materialization``（场景 C 确定规则）。
        """
        if type(seq) is not int or seq < 1:
            raise ValueError("late success seq must be a positive int")
        pending = self._late_pending.pop(seq, None)
        if pending is None:
            return False
        index, key, body = pending
        if self._base.get(index, key) is not None:
            return False        # 已在场（幂等：不重复写）
        self._base.create(index, key, body)
        self.calls["late_delivered"] += 1
        return True

    # ---------- bulk 逐条解析面（条件挂接——base 有面才挂） ----------

    def _disconnect_active(self) -> bool:
        if self._disconnect_seconds is None:
            return False
        if self._disconnect_deadline is None:
            self._disconnect_deadline = (self._clock()
                                         + self._disconnect_seconds)
        return self._clock() < self._disconnect_deadline

    def _fault_bulk_classified(self, documents):
        from news_flash_dedup.materialize_failure import bulk_document_outcome

        self.calls["bulk"] += 1
        if self._disconnect_active():
            self.calls["disconnect_refused"] += 1
            raise OSError("simulated transport disconnect (injection window)")
        outcomes = []
        for position, (index, key, body) in enumerate(documents):
            kind = body.get("kind") if isinstance(body, dict) else None
            seq = body.get("arrival_seq") if isinstance(body, dict) else None
            permanent = self._permanent.get(seq) \
                if type(seq) is int else None
            if permanent is not None:
                if kind == "item":
                    # 指定序号永死：item 文档 400（确定性单条拒绝）。
                    self.calls["item_faults"] += 1
                    outcomes.append(bulk_document_outcome(
                        position, index, key, 400,
                        error_type="injected",
                        error_reason="permanent item rejection"))
                    continue
                if kind is None:
                    # 主记录文档：混合形态照写（201）；纯死形态 400 且
                    # 捕获主记录体供迟到成功投递。
                    if not permanent["main_record"]:
                        self._late_pending.setdefault(
                            seq, (index, key, body))
                        outcomes.append(bulk_document_outcome(
                            position, index, key, 400,
                            error_type="injected",
                            error_reason="permanent rejection without write"))
                        continue
            unknown = self._unknown.get(seq) if type(seq) is int else None
            if unknown is not None and kind == "item":
                if unknown["written"]:
                    self._base.create(index, key, body)  # 写成功，回执丢失
                outcomes.append(bulk_document_outcome(
                    position, index, key, 409,
                    error_type="injected", error_reason="lost acknowledgement"))
                continue
            try:
                self._base.create(index, key, body)
                status = 201
            except Exception as error:
                if not self._base.is_conflict(error):
                    raise
                status = 409
            outcomes.append(bulk_document_outcome(
                position, index, key, status))
        return tuple(outcomes)

    # ---------- 协议面透传（TombstoneStore/BatchStore 薄壳） ----------

    def get(self, index, key):
        return self._base.get(index, key)

    def create(self, index, key, body):
        return self._base.create(index, key, body)

    def replace(self, index, key, body, seq_no, primary_term):
        return self._base.replace(index, key, body, seq_no, primary_term)

    def mget(self, keys):
        return self._base.mget(keys)

    def is_conflict(self, error):
        return self._base.is_conflict(error)

    def __getattr__(self, name):
        return getattr(self._base, name)


__all__ = ["RecoveryFaultInjector"]
