# -*- coding: utf-8 -*-
"""W-A ①运行时预算机制（值无关骨架——数值注入点全留白，w-a-design §4.2 全形）。

四域共用件（不属于 recall/decide/facts/vector 任一域——预算贯穿受理→召回
→判定→提交全链）。三型：

- RuntimeBudgetConfig：数值配置（全部必填无默认，N3 契约修订窗注入定值；
  本模块与任一调用点均不内嵌契约数值——12§7.1 L372-377 契约行登记：
  max_processing_s→15 / prepare_soft_s→10 / commit_target_s→1 /
  channel_timeout_s→0.3 / per_call_timeout_s→3 / max_model_calls→20 /
  shared_append_budget→1，定值权属 N3 窗，本稿只留白不代行）；
- ProcessingBudget：单条记录预算实例——accepted_at 派生一次，重领/重试/
  切阶段不重建（12 L372；码面不提供"重建 budget"路径，调用方持原实例
  重入=结构性保证 deadline 不重置）；注入时钟 clock_mono=T082 锚；
- LogicalCallPermit：一次逻辑模型调用的尝试闸门——首次+至多
  shared_append_budget 次追加；Schema修复/Evidence修复/瞬时故障共用同一
  追加额度（不各加一次——T047）；追加计数器在本型、账户计数器在
  budget（两型分离，防语义混淆）。

归因词表（T011，机制件只出预算侧两码；FACT_INCOMPLETE 由 facts 层缺工件
自带归因，非本件职责）：
- 模型账户尽/追加尽 → "CANDIDATE_BUDGET_EXHAUSTED"；
- 依赖实际超时（budgeted_timeout 截断或 exhausted）→ "DEPENDENCY_TIMEOUT"。

缓存命中路径不调用 charge_model_call（12 L377"缓存命中不计"）——命中分支
无 charge 调用属调用方纪律，本件只锁账户接口语义。
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class RuntimeBudgetConfig:
    """运行时预算数值配置（值无关骨架——全部必填无默认）。"""

    max_processing_s: float
    prepare_soft_s: float
    commit_target_s: float
    channel_timeout_s: float
    per_call_timeout_s: float
    max_model_calls: int
    shared_append_budget: int

    def __post_init__(self) -> None:
        # fail-closed 校验（值合法域闸；非型式闸——bool 是 int 子型但
        # 语义非法，一并以 type 闸拒之）。
        for name in ("max_processing_s", "prepare_soft_s", "commit_target_s",
                     "channel_timeout_s", "per_call_timeout_s"):
            value = getattr(self, name)
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise ValueError(f"{name} must be a number")
            if not value > 0:
                raise ValueError(f"{name} must be positive")
        if type(self.max_model_calls) is not int or self.max_model_calls < 1:
            raise ValueError("max_model_calls must be >= 1")
        if type(self.shared_append_budget) is not int or self.shared_append_budget < 0:
            raise ValueError("shared_append_budget must be >= 0")
        if self.prepare_soft_s >= self.max_processing_s:
            raise ValueError("prepare_soft_s must be < max_processing_s")
        if self.commit_target_s >= self.max_processing_s:
            raise ValueError("commit_target_s must be < max_processing_s")


class ProcessingBudget:
    """单条记录的预算实例（derive 一次随行；重领=持原 accepted_at 重派生
    同 deadline，实例本身不重建语义）。"""

    __slots__ = ("_accepted_at_mono", "_config", "_clock_mono",
                 "_model_calls_spent")

    def __init__(self, *, accepted_at_mono: float,
                 config: RuntimeBudgetConfig,
                 clock_mono: Callable[[], float]) -> None:
        self._accepted_at_mono = float(accepted_at_mono)
        self._config = config
        self._clock_mono = clock_mono
        self._model_calls_spent = 0

    @classmethod
    def derive(cls, *, accepted_at_mono: float, config: RuntimeBudgetConfig,
               clock_mono: Callable[[], float]) -> "ProcessingBudget":
        """accepted_at 派生（12 L372）；重领=调用方持原 accepted_at 重派生
        ——deadline 相同（不重置），实例随行传播全链。"""
        return cls(accepted_at_mono=accepted_at_mono, config=config,
                   clock_mono=clock_mono)

    @property
    def config(self) -> RuntimeBudgetConfig:
        return self._config

    @property
    def deadline_mono(self) -> float:
        """accepted_at_mono + max_processing_s（固定，跨重领不重置）。"""
        return self._accepted_at_mono + float(self._config.max_processing_s)

    def remaining_s(self) -> float:
        """deadline_mono - clock_mono()（可负——调用方判 exhausted）。"""
        return self.deadline_mono - float(self._clock_mono())

    def exhausted(self) -> bool:
        """总闸到时（remaining_s() <= 0）。"""
        return self.remaining_s() <= 0.0

    def budgeted_timeout(self, requested_s: float) -> float:
        """服从剩余预算：min(requested_s, max(0, remaining_s()))（12 L376）。"""
        return min(float(requested_s), max(0.0, self.remaining_s()))

    def charge_model_call(self, n: int = 1) -> bool:
        """⑥统一账户：超限或已 exhausted → False 不扣（到期不再启动模型，
        T082）；重试统一计账（12 L377）。缓存命中路径不调用本方法。"""
        if type(n) is not int or n < 1:
            raise ValueError("charge count must be a positive int")
        if self.exhausted():
            return False
        if self._model_calls_spent + n > self._config.max_model_calls:
            return False
        self._model_calls_spent += n
        return True

    @property
    def model_calls_spent(self) -> int:
        return self._model_calls_spent

    def prepare_deadline_mono(self) -> float:
        """②准备软预算上界=deadline_mono - commit_target_s（余量划分，
        不是额外时长——12 L373）。"""
        return self.deadline_mono - float(self._config.commit_target_s)

    def beyond_prepare_soft(self) -> bool:
        """②软语义探针：越过 accepted_at+prepare_soft_s（临界区预备态——
        不再启动新模型调用、只收尾已启动者；软=归因倾向不硬抛）。"""
        return (float(self._clock_mono())
                > self._accepted_at_mono + float(self._config.prepare_soft_s))

    def permit(self) -> "LogicalCallPermit":
        """⑤同一逻辑调用的追加许可（T047 共享额度）。"""
        return LogicalCallPermit(self)

    def exhaustion_attribution(self) -> str | None:
        """T011 预算侧归因二码词表：账户尽→CANDIDATE_BUDGET_EXHAUSTED；
        实际超时→DEPENDENCY_TIMEOUT；两皆未尽→None（未耗尽不归因）。"""
        if self._model_calls_spent >= self._config.max_model_calls:
            return "CANDIDATE_BUDGET_EXHAUSTED"
        if self.exhausted():
            return "DEPENDENCY_TIMEOUT"
        return None


class LogicalCallPermit:
    """一次逻辑模型调用的尝试闸门（追加额度共享计数器在本型，非 budget）。"""

    __slots__ = ("_budget", "_attempts")

    def __init__(self, budget: ProcessingBudget) -> None:
        self._budget = budget
        self._attempts = 0

    @property
    def attempts(self) -> int:
        return self._attempts

    def next_attempt(self) -> tuple[bool, float]:
        """返回 (允许?, timeout_s)；首次恒允许（账户有余且未到期时），
        追加尝试消耗共享额度（首次+至多 shared_append_budget 次追加——
        Schema修复/Evidence修复/瞬时故障共用同一额度）；额度尽/账户尽/
        到期 → (False, 0.0)。每次允许前经 charge_model_call() 统一计账，
        timeout 取 budgeted_timeout(per_call_timeout_s)（服从剩余预算）。"""
        if self._attempts >= 1 + self._budget.config.shared_append_budget:
            return False, 0.0
        if not self._budget.charge_model_call():
            return False, 0.0
        self._attempts += 1
        return True, self._budget.budgeted_timeout(
            float(self._budget.config.per_call_timeout_s))


__all__ = [
    "LogicalCallPermit",
    "ProcessingBudget",
    "RuntimeBudgetConfig",
]
