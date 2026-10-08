"""B+ 并行预检模式·编排驱动（2026-10-08 主窗口）。

设计 = log\吞吐方案-最终方案.md v3 §三.1 + log\temp\batch-commit-impl-contract.md
「接线设计」。本模块把并行模式的两个相位拼起来（worker 新模式调用）：

池内（并发，PrecheckPool 保序）：预算派生+闸门（prepared ≥ seq−1，现役
语义）→ build_plan → build_commit_inputs → decide_for_task 草稿判定
（pair_results 随包携带——decide 零改动接法，合同已钉）。

池外（保序 drain，逐件）：reconstruct 提交时刻候选集差集 →
- identical 且 coverage 未变：直接复用草稿 DecideOutcome；
- 否则：缺对逐对补算（单候选调用取 pair_results[0]，pair_cache 命中即免）
  → 存活候选缓存对+新算对，按新鲜 required 集+新鲜 coverage 重跑
  aggregate（廉价纯函数）。

写相位：攒连续前缀成批 → commit_batch 四段落锤（本模块不管写，产出
BatchCommitItem 交给批量驱动器）。

首版组装为纯函数式编排核（fake 可测）；worker 模式接线层负责喂件与
驱动批量提交。判别口径（外部终审钉）：missing 只来自同可见域统计漂移/
旧文档迟到（新到件被 arrival_seq<N 结构性排除）；dropped 退出聚合
（集合语义非判错）。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from news_flash_dedup.decide.pair_cache import PairCacheKey, PairResultCache
from news_flash_dedup.recall.reconstruct import (
    CandidateSetDiff,
    diff_candidate_sets,
)


@dataclass(frozen=True)
class PrecheckPacket:
    """池内产出（保序交付）：草稿判定 + 逐对结果 + 预检计划/请求。"""

    plan: Any                    # 预检 RecallPlan
    request: Any                 # 预检 RecallRequest（身份对拍锚）
    current: Mapping             # build_commit_inputs 产出
    candidates: tuple            # 同上（history=末位语义由 decide 内部处理）
    coverage: bool
    decide: Any                  # 草稿 DecideOutcome（pair_results 外露）


@dataclass(frozen=True)
class CommitDecision:
    """池外定稿：写相位输入（decide 终稿 + 使用的差集证据）。"""

    decide: Any                  # 终稿 DecideOutcome（复用草稿或重聚合）
    diff: CandidateSetDiff
    recomputed: bool             # True=发生了补算/重聚合；False=草稿直用
    pair_cache_stats: Mapping    # 本次涉及的缓存观测面


def finalize_decision(
        packet: PrecheckPacket,
        *,
        commit_request: Any,
        fresh_plan: Any,
        decide_pair: Callable[[Mapping, Mapping], Any],
        aggregate: Callable[..., Any],
        pair_cache: PairResultCache,
        version_chain: tuple[str, ...],
        candidate_lookup: Callable[[str], Mapping],
) -> CommitDecision:
    """提交时刻定稿（纯编排，fake 可测）。

    入参职责：
    - commit_request / fresh_plan：提交时刻新鲜请求（水位新鲜）与
      重构计划（调用侧=reconstruct 的产物；本函数不做 IO）；
    - decide_pair(history_mapping, current_mapping) -> PairResult：
      逐对补算口（缺对候选用；实现=decide_for_task 单候选调用取
      pair_results[0]，由接线层供给）；
    - aggregate(current, frozen_plan, pair_results, ...) -> DecideOutcome：
      集合级重聚合口（decide.aggregate 同源，由接线层供给）；
    - candidate_lookup(record_id) -> Mapping：缺对候选的全字段取数口
      （新鲜计划里只有 RecallCandidate，补算需 decide 输入形态）；
    - version_chain：facts/dict/normalizer/pipeline 全版本链（缓存键）。

    语义（合同钉）：
    - diff.identical 且 fresh coverage == 预检 coverage → 草稿直用
      （recomputed=False）；
    - 否则：缺对（diff.missing）补算（缓存优先），dropped 退出，
      存活对（预检 pair_results 中候选仍在新鲜 required 集内者）+
      新算对 → aggregate 重聚合（coverage 取新鲜值）。
    """
    diff = diff_candidate_sets(packet.plan, fresh_plan)
    fresh_coverage = _coverage_of(fresh_plan)
    if diff.identical and fresh_coverage == packet.coverage:
        return CommitDecision(
            decide=packet.decide, diff=diff, recomputed=False,
            pair_cache_stats=pair_cache.stats)

    # 缺对补算（缓存优先——逐对结果时不变，命中即免算）
    fresh_ids = {fused.candidate.record_id for fused in fresh_plan.required}
    pair_by_candidate: dict[str, Any] = {}
    for pair in getattr(packet.decide, "pair_results", ()):
        rid = _pair_candidate_id(pair)
        if rid in fresh_ids:
            pair_by_candidate[rid] = pair
    for rid in diff.missing:
        history = candidate_lookup(rid)
        key = PairCacheKey(
            history_raw_hash=str(history.get("raw_hash", "")),
            current_raw_hash=str(packet.current.get("raw_hash", "")),
            version_chain=version_chain)
        pair_by_candidate[rid] = pair_cache.get_or_compute(
            key, lambda h=history: decide_pair(h, packet.current))

    frozen_plan = _frozen_plan_from(fresh_plan)
    decide = aggregate(packet.current, frozen_plan,
                       list(pair_by_candidate.values()),
                       coverage_complete=fresh_coverage)
    return CommitDecision(
        decide=decide, diff=diff, recomputed=True,
        pair_cache_stats=pair_cache.stats)


def _coverage_of(plan: Any) -> bool:
    """计划覆盖旗标=全通道审计 coverage_complete 合取（与现役口径同源：
    recall_incomplete 即 False；缺口语义不新增）。"""
    if plan.recall_incomplete:
        return False
    audits = getattr(plan, "channel_audits", ())
    return all(getattr(a, "coverage_complete", True) for a in audits)


def _pair_candidate_id(pair: Any) -> str:
    """对级结果取候选侧 record_id（鸭子读取：history_record_id 优先，
    缺则 history.mapping.record_id——接线层按现役 PairResult 形态适配）。"""
    rid = getattr(pair, "history_record_id", None)
    if rid:
        return rid
    history = getattr(pair, "history", None)
    if isinstance(history, Mapping) and history.get("record_id"):
        return history["record_id"]
    raise ValueError(
        f"cannot locate candidate record_id on pair result "
        f"{type(pair).__name__} (adapter contract)")


def _frozen_plan_from(plan: Any) -> Any:
    """新鲜 required 集的 FrozenRecallPlan 形态（aggregate 输入口；
    接线层按 decide 侧 FrozenRecallPlan 类型适配）。"""
    return getattr(plan, "frozen_plan", plan)


__all__ = [
    "CommitDecision",
    "PrecheckPacket",
    "finalize_decision",
]
