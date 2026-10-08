# -*- coding: utf-8 -*-
"""W-A ①预算机制红测（建议落 tests/unit/test_wina_runtime_budget.py）。

红态锚：runtime_budget 模块不存在（全件红）；T082/T047/T011 骨架断言
对机制件行为钉。数值=测试显式注入（契约场景字面 15/12/3/20 来自 12§7.1/
T082/T047 原文，非模块默认——值无关骨架纪律）。
"""
from __future__ import annotations

import pytest

try:
    from news_flash_dedup import runtime_budget as _rb
except ImportError:                                   # 未实现 → 显式红夹具
    _rb = None


@pytest.fixture()
def rb():
    """runtime_budget 模块句柄；未实现时显式红（非收集错误、非 skip——
    test_pair_alignment_normalize.py:45-53 `nd` 夹具同姿势）。"""
    if _rb is None:
        pytest.fail("①预算机制：news_flash_dedup.runtime_budget 尚未实现，"
                    "本测试当前红属预期")
    return _rb


def _config(rb, **over):
    base = dict(max_processing_s=15.0, prepare_soft_s=10.0, commit_target_s=1.0,
                channel_timeout_s=0.3, per_call_timeout_s=3.0,
                max_model_calls=20, shared_append_budget=1)
    base.update(over)
    return rb.RuntimeBudgetConfig(**base)


class _Clock:
    def __init__(self, t=1000.0):
        self.t = t
    def __call__(self):
        return self.t


# ---------- T082 骨架：注入时钟/预算传播 ----------

def test_t082_deadline_fixed_across_reclaim(rb):
    """accepted_at+15s 固定：排队 12s 仅余 3s；重领重派生不重置（12 L372/L531）。"""
    clock = _Clock()
    cfg = _config(rb)
    budget = rb.ProcessingBudget.derive(accepted_at_mono=1000.0, config=cfg,
                                        clock_mono=clock)
    clock.t += 12.0                                    # 排队 12s
    assert budget.remaining_s() == pytest.approx(3.0)  # 仅余 3s
    # 重领=持原 accepted_at 重派生：deadline 相同（不重置）
    reclaimed = rb.ProcessingBudget.derive(accepted_at_mono=1000.0, config=cfg,
                                           clock_mono=clock)
    assert reclaimed.deadline_mono == budget.deadline_mono == 1015.0
    clock.t += 3.5                                     # 越过 deadline
    assert budget.exhausted() is True


def test_t082_no_model_call_after_deadline(rb):
    """到期不再启动模型：charge 拒付且不扣账（T082'到期不再启动模型'）。"""
    clock = _Clock()
    budget = rb.ProcessingBudget.derive(accepted_at_mono=1000.0,
                                        config=_config(rb), clock_mono=clock)
    clock.t += 16.0                                    # 过 15s
    assert budget.charge_model_call() is False
    assert budget.model_calls_spent == 0


def test_budgeted_timeout_obeys_remaining(rb):
    """服从剩余预算（12 L376）：请求 3s 但仅剩 1.5s → 截断 1.5s。"""
    clock = _Clock()
    budget = rb.ProcessingBudget.derive(accepted_at_mono=1000.0,
                                        config=_config(rb), clock_mono=clock)
    clock.t += 13.5
    assert budget.budgeted_timeout(3.0) == pytest.approx(1.5)
    clock.t += 2.0
    assert budget.budgeted_timeout(3.0) == 0.0


# ---------- T047 骨架：共享一次追加+20 次账户 ----------

def test_t047_shared_append_single_budget_across_failure_kinds(rb):
    """同一逻辑调用：Schema修复/Evidence修复/瞬时故障共享一次追加（不各加一次）。

    序列：首次 3s 超时（瞬时）→ 追加一次（schema 修复消耗同一额度）→
    再失败（evidence）→ 无第三次（T047'同一逻辑调用共享最多一次追加尝试'）。"""
    clock = _Clock()
    budget = rb.ProcessingBudget.derive(accepted_at_mono=1000.0,
                                        config=_config(rb), clock_mono=clock)
    permit = budget.permit()
    ok1, t1 = permit.next_attempt()                    # 首次
    assert ok1 and t1 == pytest.approx(3.0)
    ok2, _ = permit.next_attempt()                     # 追加（任一失败族，共享额度）
    assert ok2
    ok3, t3 = permit.next_attempt()                    # 第二追加 → 拒
    assert not ok3 and t3 == 0.0
    assert permit.attempts == 2
    assert budget.model_calls_spent == 2               # 重试统一计账（12 L377）


def test_t047_model_call_account_ceiling_20(rb):
    """20 次统一账户：第 21 次拒付不扣；多逻辑调用共享同一账户。"""
    clock = _Clock()
    budget = rb.ProcessingBudget.derive(accepted_at_mono=1000.0,
                                        config=_config(rb, max_model_calls=20),
                                        clock_mono=clock)
    for _ in range(20):
        assert budget.charge_model_call() is True
    assert budget.charge_model_call() is False         # 第 21 次
    assert budget.model_calls_spent == 20
    # 新逻辑调用许可也受同一账户约束
    permit = budget.permit()
    ok, _ = permit.next_attempt()
    assert not ok


def test_t047_cache_hit_not_charged(rb):
    """缓存命中不计（12 L377）：命中路径不调 charge——机制钉=账户只经
    charge_model_call 变动，调用方纪律由集成钉（facts/embedding 命中分支
    无 charge 调用）保证；本钉锁账户接口语义。"""
    clock = _Clock()
    budget = rb.ProcessingBudget.derive(accepted_at_mono=1000.0,
                                        config=_config(rb), clock_mono=clock)
    assert budget.model_calls_spent == 0               # 构造/查询零副作用


# ---------- ②③ 阶段预算骨架 ----------

def test_prepare_soft_and_commit_target_derived_not_additive(rb):
    """准备软预算=deadline-1s 的余量划分，不是额外 10s（12 L373/L374）。"""
    clock = _Clock()
    budget = rb.ProcessingBudget.derive(accepted_at_mono=1000.0,
                                        config=_config(rb), clock_mono=clock)
    assert budget.prepare_deadline_mono() == pytest.approx(1014.0)   # 1015-1
    clock.t += 10.5
    assert budget.beyond_prepare_soft() is True
    assert budget.exhausted() is False                               # 软≠硬


# ---------- T011 归因骨架 ----------

def test_t011_exhaustion_attribution_vocabulary(rb):
    """预算侧归因二码词表：账户尽→CANDIDATE_BUDGET_EXHAUSTED；
    实际超时→DEPENDENCY_TIMEOUT（12 L401/L435；FACT_INCOMPLETE 属 facts 层）。"""
    clock = _Clock()
    budget = rb.ProcessingBudget.derive(accepted_at_mono=1000.0,
                                        config=_config(rb, max_model_calls=1),
                                        clock_mono=clock)
    assert budget.charge_model_call() is True
    assert budget.charge_model_call() is False
    assert budget.exhaustion_attribution() == "CANDIDATE_BUDGET_EXHAUSTED"
    clock2 = _Clock()
    budget2 = rb.ProcessingBudget.derive(accepted_at_mono=1000.0,
                                         config=_config(rb), clock_mono=clock2)
    clock2.t += 16.0
    assert budget2.charge_model_call() is False
    assert budget2.exhaustion_attribution() == "DEPENDENCY_TIMEOUT"


# ---------- 值无关纪律钉 ----------

def test_config_has_no_defaults(rb):
    """值无关骨架：RuntimeBudgetConfig 全部必填无默认（N3 定值注入点留白）。"""
    import inspect
    sig = inspect.signature(rb.RuntimeBudgetConfig)
    assert all(param.default is inspect.Parameter.empty
               for param in sig.parameters.values())


def test_config_fail_closed_validation(rb):
    with pytest.raises(ValueError):
        _config(rb, max_processing_s=0.0)
    with pytest.raises(ValueError):
        _config(rb, max_model_calls=0)
    with pytest.raises(ValueError):
        _config(rb, shared_append_budget=-1)
