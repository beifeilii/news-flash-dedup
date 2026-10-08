"""B+ 第三步核心件①：提交时候选集精确重构（2026-10-08 主窗口）。

精神模型（log\吞吐方案-最终方案.md v3 §三.1）：逐对比对结果时不变（可缓存）
→候选集成员时变（提交时刻重建）→聚合廉价永远新鲜。本模块负责"重建+差集"：
提交时刻以同一查询路径（RecallService.build_plan）重查候选集，与预检冻结
计划差集比对：

- missing（提交时刻新入比对名单 required）：调用侧须就地补算逐对
  （缓存未命中件——外部终审口径：新到件被 arrival_seq<N 过滤结构性排除，
  missing 只来自同可见域内的排名统计漂移与旧文档迟到可见）；
- dropped（预检有、提交时刻掉出）：其逐对结果不再参与聚合——集合语义，
  非"判错"：串行版在同一提交时刻可见状态下同样不会拿它进比对名单；
- fresh_plan 的通道审计/覆盖旗标=提交时刻新鲜值（coverage_complete 与
  聚合由调用侧据此重算，对应"水位不完整→双方同落边界"验收用例）。

首版以 build_plan 全路径重查（正确优先——提交相位本来就在预检池遮蔽下；
瘦字段投影=后续性能项，届时同一代码路径仅投影收窄，集合语义不变）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .fusion import RecallPlan
from .models import RecallRequest


@dataclass(frozen=True)
class CandidateSetDiff:
    """预检计划 vs 提交时刻计划的比对名单差集。"""

    fresh_plan: RecallPlan
    missing: tuple[str, ...]    # 提交时刻新入 required（须补算），确定性序
    dropped: tuple[str, ...]    # 预检 required 中掉出者（不再参与聚合）
    identical: bool             # missing 与 dropped 皆空


def required_id_set(plan: RecallPlan) -> frozenset[str]:
    """比对名单（required）候选 record_id 集合——差集运算的原子。"""
    return frozenset(fused.candidate.record_id for fused in plan.required)


def _ordered(plan: RecallPlan, ids: frozenset[str]) -> tuple[str, ...]:
    """集合按 (arrival_seq, record_id) 确定性排序（差集输出可复现）。"""
    keyed = {fused.candidate.record_id: fused.candidate.arrival_seq
             for fused in plan.required}
    return tuple(sorted(ids, key=lambda rid: (keyed.get(rid, 0), rid)))


def diff_candidate_sets(precheck_plan: RecallPlan,
                        fresh_plan: RecallPlan) -> CandidateSetDiff:
    """纯集合差：missing=fresh−precheck，dropped=precheck−fresh。"""
    pre_ids = required_id_set(precheck_plan)
    fresh_ids = required_id_set(fresh_plan)
    missing = fresh_ids - pre_ids
    dropped = pre_ids - fresh_ids
    return CandidateSetDiff(
        fresh_plan=fresh_plan,
        missing=_ordered(fresh_plan, missing),
        dropped=_ordered(precheck_plan, dropped),
        identical=not missing and not dropped,
    )


def reconstruct(recall_service: Any, request: RecallRequest,
                precheck_plan: RecallPlan, *,
                precheck_request: RecallRequest | None = None,
                budget: Any = None) -> CandidateSetDiff:
    """提交时刻重构：同一查询路径重查 + 差集。

    调用侧纪律（fail-closed）：
    - request 的水位字段（visible_seq/prepared_seq）必须取提交时刻新鲜值；
      身份字段（scope/date/record_id/item_id/arrival_seq/text）必须与预检
      请求逐字节一致——传入 precheck_request 时本函数核验，不符 ValueError
      （接错线的结构性错误不得静默流入差集语义）。
    """
    if precheck_request is not None:
        identity = ("scope_id", "business_date", "record_id", "item_id",
                    "arrival_seq", "text")
        mismatched = [name for name in identity
                      if getattr(precheck_request, name)
                      != getattr(request, name)]
        if mismatched:
            raise ValueError(
                f"commit-time request identity diverges from precheck on "
                f"{mismatched} (wiring bug; refuse to diff)")
    fresh_plan = recall_service.build_plan(request, budget=budget)
    return diff_candidate_sets(precheck_plan, fresh_plan)


__all__ = [
    "CandidateSetDiff",
    "diff_candidate_sets",
    "reconstruct",
    "required_id_set",
]
