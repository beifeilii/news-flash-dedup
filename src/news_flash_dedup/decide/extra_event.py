"""P16-E extra_event RULE_UNCOVERED 检测。

按 03:44 批复标尺 1：在 aligned_facts 之上做 Fact 计数差比对。
同主体/谓词/关键对象核心事件配对成功，但单侧有额外 Fact → RULE_UNCOVERED。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ExtraEventReport:
    """额外事件检测报告。"""
    has_extra_event: bool
    uncovered_fact_ids: tuple[str, ...]
    history_fact_count: int
    current_fact_count: int
    matched_fact_ids: tuple[str, ...]


def _fact_ids(artifact_or_context: object) -> tuple[str, ...]:
    """提取 artifact/context 中的 fact_id 列表（按顺序）。

    W2Fα2-6（WA1b-L2，探针 winw2fa2-probe-extra-event.json）：None 前置
    拦截——缺 fact_id（None/缺键）的 Fact 不进集合匹配，不再经 str(None)
    伪造伪 id "None"（修复前双侧各一个无 id Fact 即 "None"=="None" 伪
    匹配吞掉真实额外事件=假阴，且与真字面值 "None" fact_id 不可区分）。
    fail-closed 方向：无 id Fact 不参与匹配，真 id 差集如实入 uncovered。
    """
    def _ids(facts: object) -> tuple[str, ...]:
        if not isinstance(facts, list):
            return ()
        out: list[str] = []
        for f in facts:
            if not isinstance(f, dict):
                continue
            fid = f.get("fact_id")
            if fid is None:                    # None 前置拦截（见 docstring）
                continue
            out.append(str(fid))
        return tuple(out)

    if isinstance(artifact_or_context, dict):
        return _ids(artifact_or_context.get("facts"))
    artifact = getattr(artifact_or_context, "artifact", None)
    if isinstance(artifact, dict):
        return _ids(artifact.get("facts"))
    return ()


def detect_extra_event(history_artifact: object,
                       current_artifact: object) -> ExtraEventReport:
    """检测 extra_event：双方 Facts 集合存在差异 → uncovered。

    当前实现：比较双方 fact_id 集合（不区分顺序），出现在任一侧且不在另一侧的
    fact_id 入 uncovered_fact_ids。**严格不做相似度比较**——同 P16-A 一致。
    后续 P16-E / P23 校准阶段可加更深的语义差检测，但当前不做。

    W2Fα2-6：history_fact_count/current_fact_count 计有效 fact_id 数
    （缺 id Fact 经 None 前置拦截不计——见 _fact_ids docstring）。
    """
    history_fact_ids = _fact_ids(history_artifact)
    current_fact_ids = _fact_ids(current_artifact)

    history_set = set(history_fact_ids)
    current_set = set(current_fact_ids)

    only_history = sorted(history_set - current_set)
    only_current = sorted(current_set - history_set)

    uncovered = tuple(f"history:{fid}" for fid in only_history) + \
                tuple(f"current:{fid}" for fid in only_current)

    has_extra = bool(uncovered)
    return ExtraEventReport(
        has_extra_event=has_extra,
        uncovered_fact_ids=uncovered,
        history_fact_count=len(history_fact_ids),
        current_fact_count=len(current_fact_ids),
        matched_fact_ids=tuple(sorted(history_set & current_set)),
    )


__all__ = ["ExtraEventReport", "detect_extra_event"]