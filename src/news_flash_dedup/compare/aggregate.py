"""P16-C 五字段汇总 aggregate。

按 09 §10 / 07 §5 / 11 §2 五字段合同做最终汇总：
- duplicate_ids 严格按 arrival_seq 升序去重（INV-2）
- decision 与 duplicate_ids 严格耦合（重复 ↔ 非空，其余两类 []）（INV-2）
- 单对 conflict 不结束整条（INV-4）
- 非法成员拒绝门六形态（INV-12 / L08）
- 不返回 failed（INV-8）
- 五字段封闭校验（INV-9）

设计依据：`log/P14-16-Fact精判集成设计.md` §3.4 / §4.2。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from news_flash_dedup.compare.pair_compare import (
    CONFLICT_CODES,
    EQUIVALENT_CODES,
    UNRESOLVED_CODES,
    PairBindingError,
    PairIssue,
    PairResult,
)


_DECISION = Literal["重复", "不重复", "边界case/疑难case"]

# 提交三（§5.3 公共理由）：重复/不重复的公共 reason 优先取对级
# PairResult.detail 的清洗版（最早重复对 / 首个冲突对），无可用 detail
# 才落固定兜底文案。清洗=去 URL、折叠空白、上限 300 字符；理由不得
# 包含内部码（下方扫描闸兜底，不静默洗码）/模型原始响应全文/密钥/超长
# 原文。
_REASON_TEXT_CAP = 300
_URL_RE = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)


def _clean_public_reason(text: str | None) -> str:
    """公共理由整形：去 URL → 折叠空白 → 截 300 字。空/全被剥除 → ""
    （调用侧落固定兜底文案）。"""
    if not isinstance(text, str):
        return ""
    cleaned = " ".join(_URL_RE.sub("", text).split())
    if len(cleaned) > _REASON_TEXT_CAP:
        cleaned = cleaned[: _REASON_TEXT_CAP - 1] + "…"
    return cleaned


@dataclass(frozen=True)
class AggregateOutcome:
    """五字段汇总结果 + 审计内部码（不入五字段）。"""
    item_id: str
    text: str
    decision: _DECISION
    duplicate_ids: tuple[str, ...]
    reason: str
    internal_code: str
    pair_codes: tuple[str, ...]
    used_evidence: tuple[Any, ...] = field(repr=False)


@dataclass(frozen=True)
class FrozenRecallPlan:
    """P13 `RecallPlan.required` 的视图，与 P16-E 的 DecideOutcome 输入 1:1。"""
    version: str
    required: Mapping[str, Mapping[str, Any]]   # record_id -> PairRecordContext
    pair_top10: tuple[str, ...] = ()
    hash_protected: tuple[str, ...] = ()
    incremental_required: tuple[str, ...] = ()


@dataclass(frozen=True)
class CoverageStatus:
    """双水位（visible_seq + prepared_seq）真实覆盖状态。

    双水位均 ≥ arrival_seq - 1 时 `complete=True`；否则视作召回不完整。
    """
    visible_seq: int | None = None
    prepared_seq: int | None = None
    complete: bool = False


class AggregateError(ValueError):
    """汇总阶段的非业务失败（结构/合同不合法）。"""


class IllegalMemberError(AggregateError):
    """非法成员（INV-12 / L08）——拒绝并抛错，不静默过滤。"""


# ---------- 内部码 / 决策映射 ----------

_BOUNDARY_PRIORITY = (
    "SUBJECT_UNRESOLVED",
    # 提交一（2026-10-10，p3-semantic-authority，§5.1）：判官双序存疑/
    # 分歧未决码——与 pair_compare.py 同表同步（紧随语义近邻之后）。
    "JUDGE_UNCERTAIN",
    "DEPENDENCY_TIMEOUT",
    "RECALL_INCOMPLETE",
    "CANDIDATE_BUDGET_EXHAUSTED",
    "EXTRACTION_FAILED",
    "EVIDENCE_INVALID",
    "NUMERIC_ALIGNMENT_FAILED",
    "TIME_RELATION_UNCERTAIN",
    "RULE_UNCOVERED",
    "FACT_INCOMPLETE",
)


def _normalize_ctx(obj: Any) -> Mapping:
    if isinstance(obj, Mapping):
        return obj
    if hasattr(obj, "__dataclass_fields__"):
        from dataclasses import asdict
        return asdict(obj)
    if hasattr(obj, "_asdict"):
        return obj._asdict()
    raise AggregateError(f"context must be Mapping or dataclass-like: {type(obj).__name__}")


def _normalize_plan(obj: Any) -> FrozenRecallPlan:
    if isinstance(obj, FrozenRecallPlan):
        return obj
    if isinstance(obj, Mapping):
        required = obj.get("required") or {}
        if not isinstance(required, Mapping):
            raise AggregateError("frozen_plan['required'] must be a Mapping")
        normalized_required = {
            record_id: _normalize_ctx(ctx) for record_id, ctx in required.items()
        }
        return FrozenRecallPlan(
            version=obj.get("version", ""),
            required=normalized_required,
            pair_top10=tuple(obj.get("pair_top10", ())),
            hash_protected=tuple(obj.get("hash_protected", ())),
            incremental_required=tuple(obj.get("incremental_required", ())),
        )
    if hasattr(obj, "__dataclass_fields__"):
        from dataclasses import asdict
        return _normalize_plan(asdict(obj))
    raise AggregateError(
        f"frozen_plan must be FrozenRecallPlan or Mapping, got {type(obj).__name__}"
    )


def _primary_issue(issues: Iterable[PairIssue]) -> PairIssue:
    items = list(issues)
    if not items:
        raise AggregateError("issues iterable is empty")
    rank = {code: index for index, code in enumerate(_BOUNDARY_PRIORITY)}
    for issue in items:
        if issue.code not in UNRESOLVED_CODES:
            raise AggregateError(
                f"issue code {issue.code!r} not in UNRESOLVED_CODES whitelist"
            )
    return min(items, key=lambda issue: (rank.get(issue.code, len(rank)), issue.detail))


def _check_member_identity(record_id: str, item_id: str, current: Mapping) -> None:
    """非法成员拒绝门（INV-12）：record_id/item_id 不能等于 current。"""
    if record_id == current["record_id"]:
        raise IllegalMemberError(f"member record_id {record_id!r} equals current record_id")
    if item_id == current["item_id"]:
        raise IllegalMemberError(f"member item_id {item_id!r} equals current item_id")


def _check_member_in_required(record_id: str, frozen_plan: FrozenRecallPlan) -> Mapping:
    ctx = frozen_plan.required.get(record_id)
    if ctx is None:
        raise IllegalMemberError(
            f"member record_id {record_id!r} not in frozen_plan.required"
        )
    return ctx


def _check_pair_binding(pair: PairResult, current: Mapping, expected_ctx: Mapping) -> None:
    # self-record-id 检查优先于绑定门其他项（让 IllegalMemberError 先于 PairBindingError 抛出）
    if pair.history_record_id == current["record_id"]:
        raise IllegalMemberError(
            f"pair history_record_id {pair.history_record_id!r} equals current record_id"
        )
    if pair.history_record_id != expected_ctx["record_id"]:
        raise PairBindingError(
            f"pair history_record_id {pair.history_record_id!r} does not match "
            f"expected_ctx record_id {expected_ctx['record_id']!r}"
        )
    if (pair.current_record_id != current["record_id"]
            or pair.history_item_id != expected_ctx["item_id"]
            or pair.current_item_id != current["item_id"]
            or pair.history_arrival_seq != expected_ctx["arrival_seq"]
            or pair.current_arrival_seq != current["arrival_seq"]
            or pair.history_raw_hash != expected_ctx["raw_hash"]
            or pair.current_raw_hash != current["raw_hash"]):
        raise PairBindingError(
            f"pair {pair.pair_id} identity/hash does not match frozen plan"
        )
    if expected_ctx["scope_id"] != current["scope_id"]:
        raise IllegalMemberError(
            f"member scope_id {expected_ctx['scope_id']!r} differs from current scope_id"
        )
    if expected_ctx["business_date"] != current["business_date"]:
        raise IllegalMemberError(
            f"member business_date {expected_ctx['business_date']!r} differs from current"
        )
    if pair.history_arrival_seq >= current["arrival_seq"]:
        raise PairBindingError(
            f"pair history_arrival_seq ({pair.history_arrival_seq}) "
            f"must be < current arrival_seq ({current['arrival_seq']})"
        )


def aggregate(
    current: Any,
    frozen_plan: FrozenRecallPlan,
    pair_results: Iterable[PairResult],
    *,
    new_text_issues: Iterable[PairIssue] = (),
    recall_issues: Iterable[PairIssue] = (),
    coverage: CoverageStatus | None = None,
    strict_required_coverage: bool = False,
) -> AggregateOutcome:
    """五字段汇总：先收集已确认直接重复（按 arrival_seq 升序去重），再按缺口决定
    不重复或边界。

    strict_required_coverage（P1-b，2026-10-09，合同
    log/temp/p1-interface-contract-v1.md §五）：判官进主链新路径的提交前
    最终聚合口径——True 时"required 非空而 pair_results 为空"记
    FACT_INCOMPLETE 缺口（不得签"不重复"）；默认 False=旧路径逐字节
    （L11/T045 冻结契约"∅→不重复"，test_p16_d_matrix 钉死，不动）。
    """
    current_ctx = _normalize_ctx(current)
    plan = _normalize_plan(frozen_plan)
    coverage = coverage or CoverageStatus()
    pairs = list(pair_results)
    issues: list[PairIssue] = []
    issues.extend(recall_issues)
    issues.extend(new_text_issues)

    # 先做非法成员检查（成员拒绝门在所有逻辑之前），保证 IllegalMemberError
    # 优先于公共字段非空错误抛出；公共字段非空检查放在循环后。

    seen_item_ids: dict[str, PairResult] = {}
    pair_codes: list[str] = []
    used_evidence: list[Any] = []
    conflicts: list[PairResult] = []
    # W2Fα1（WB3 发现#3 防御③）：arrival_seq 全局唯一——不同 history 记录
    # 共享同一到达序=到达序损坏（同一记录的重复对行=既有去重语义，放行）。
    seen_arrival_seqs: dict[int, str] = {}
    for pair in pairs:
        if not isinstance(pair, PairResult):
            raise AggregateError("pair_results entries must be PairResult")
        try:
            expected_ctx = _check_member_in_required(pair.history_record_id, plan)
            _check_pair_binding(pair, current_ctx, expected_ctx)
        except IllegalMemberError:
            raise
        except PairBindingError as exc:
            raise AggregateError(
                f"pair {pair.pair_id} identity/hash does not match frozen plan: {exc}"
            ) from exc
        _check_member_identity(pair.history_record_id, pair.history_item_id, current_ctx)
        seq_owner = seen_arrival_seqs.setdefault(pair.history_arrival_seq,
                                                 pair.history_record_id)
        if seq_owner != pair.history_record_id:
            raise AggregateError(
                f"arrival_seq {pair.history_arrival_seq} shared by two distinct "
                f"history records {seq_owner!r} and {pair.history_record_id!r}"
            )
        pair_codes.append(pair.code)
        used_evidence.extend(pair.used_evidence)
        if pair.outcome == "equivalent":
            if pair.code not in EQUIVALENT_CODES:
                raise AggregateError(
                    f"pair {pair.pair_id} outcome=equivalent but code {pair.code!r} "
                    f"not in EQUIVALENT_CODES"
                )
            previous = seen_item_ids.get(pair.history_item_id)
            if previous is not None and (
                    previous.history_raw_hash != pair.history_raw_hash
                    or previous.history_record_id != pair.history_record_id
            ):
                raise IllegalMemberError(
                    f"item_id {pair.history_item_id!r} maps to two different histories"
                )
            seen_item_ids[pair.history_item_id] = pair
        elif pair.outcome == "conflict":
            # W2Fα1（WB3 发现#3 防御②）：conflict 必须携 CONFLICT_CODES 内码
            # （与 equivalent/unresolved 两分支既有白名单闸同纪律）。
            if pair.code not in CONFLICT_CODES:
                raise AggregateError(
                    f"pair {pair.pair_id} outcome=conflict but code {pair.code!r} "
                    f"not in CONFLICT_CODES"
                )
            conflicts.append(pair)
        elif pair.outcome == "unresolved":
            if pair.code not in UNRESOLVED_CODES:
                raise AggregateError(
                    f"pair {pair.pair_id} outcome=unresolved but code {pair.code!r} "
                    f"not in UNRESOLVED_CODES"
                )
            issues.append(PairIssue(pair.code, pair.detail))
        else:
            raise AggregateError(
                f"pair {pair.pair_id} outcome {pair.outcome!r} is not in "
                f"{{equivalent, conflict, unresolved}}"
            )

    # W2Fα1（WB3 发现#3 防御①）：计划内（frozen_plan.required）成员有对级
    # 产出但部分成员静默漏对 → FACT_INCOMPLETE 记账（漏判不可静默判
    # "不重复"）。记账域限 coverage.complete=True 且 pairs 非空：
    # - 静默风险只住在"不重复"签发路径（分支 2 要求覆盖完整）；覆盖不完整
    #   时分支 4 本落边界且 RECALL_INCOMPLETE 是历史主码（rank 2 优先于
    #   FACT_INCOMPLETE），此时记账只会掩盖更具名的召回信号（test_p16_c
    #   现役契约钉死该优先级）；
    # - pairs 全空 + 覆盖完整 = L11/T045 冻结契约"∅→不重复"（test_p16_d_
    #   matrix 钉死），不在本防御面内；本防御只挡"有产出但被静默截短"。
    if coverage.complete and pairs:
        covered_record_ids = {pair.history_record_id for pair in pairs}
        for record_id in plan.required:
            if record_id not in covered_record_ids:
                issues.append(PairIssue(
                    "FACT_INCOMPLETE",
                    f"计划内成员 {record_id!r} 无对级结果（漏判不可静默）"))

    # P1-b（2026-10-09，合同 p1-interface-contract-v1 §五红线"required 非空
    # 而 pair_results 为空 → 不得签不重复"；工程正文 Codex 最终方案 §3.2
    # "新路径须补"）：strict 口径下 required 非空 + 覆盖完整 + 对级产出全空
    # =全量漏判缺口，记 FACT_INCOMPLETE 落边界进人工。默认 False：旧路径
    # L11/T045"∅→不重复"冻结契约逐字节不动（上方既有块 pairs 非空才记账的
    # 纪律同源——本块只在新路径补 pairs 全空这一格）。
    if strict_required_coverage and coverage.complete and not pairs and plan.required:
        issues.append(PairIssue(
            "FACT_INCOMPLETE",
            "计划内成员全部无对级结果（required 非空而 pair_results 为空，"
            "漏判不可静默判不重复）"))

    ordered_pairs = sorted(
        seen_item_ids.values(),
        key=lambda pair: (pair.history_arrival_seq, pair.history_item_id),
    )
    duplicate_ids = tuple(pair.history_item_id for pair in ordered_pairs)

    if duplicate_ids:
        decision: _DECISION = "重复"
        primary_equivalent = ordered_pairs[0]
        internal_code = primary_equivalent.code
        # 提交三（§5.3）：公共理由优先=最早重复对的已清洗 detail
        # （判官重复对=双序合并理由；规则链等价对=规则 detail）；清洗后
        # 为空才落固定兜底。
        reason = (_clean_public_reason(primary_equivalent.detail)
                  or "已逐一核验与列表条目的主体和核心事件一致，差异属于已允许的表达或信息差异。")
    elif not issues and coverage.complete and not current_ctx.get("subject_missing", False):
        # D25（三轮审计 C-05）：全冲突+覆盖完整场景原统一落本分支——
        # reason 错称"未发现候选"（候选明明存在且冲突证伪）、code 错挂
        # NO_DUPLICATE_FOUND，使下方冲突感知 else 成死代码。将冲突感知
        # 上提本分支（决策"不重复"不变：冲突即证伪非重复）。
        # 提交一（p3-semantic-authority，§5.1）：internal_code 取首个冲突
        # 对的真实码——规则链冲突仍 VERIFIED_CONFLICT，判官双序不重复
        # 实传 JUDGE_NON_DUPLICATE（不再把语义结论洗成机器已验证结论）。
        decision = "不重复"
        internal_code = conflicts[0].code if conflicts else "NO_DUPLICATE_FOUND"
        # 提交三（§5.3）：有冲突对时公共理由优先=首个冲突对的已清洗
        # detail（规则硬冲突 detail / 判官 JUDGE_NON_DUPLICATE 双序合并
        # 理由），清洗后为空才落固定兜底。
        reason = (_clean_public_reason(conflicts[0].detail)
                  if conflicts else "")
        reason = reason or (
            "本次限定召回及已完成直接比较中，候选均存在已验证的对应事实差异。"
            if conflicts
            else "本次健康的限定召回中未发现可比较的重复候选。")
    elif current_ctx.get("subject_missing", False):
        primary = _primary_issue(issues) if issues else PairIssue(
            "SUBJECT_UNRESOLVED", "主体或事件关系无法确认"
        )
        if primary.code not in UNRESOLVED_CODES:
            raise AggregateError(
                f"primary_issue code {primary.code!r} not in UNRESOLVED_CODES"
            )
        decision = "边界case/疑难case"
        internal_code = primary.code
        reason = primary.detail
    elif not coverage.complete:
        # W2Fα1（WB2-M6 / WA1a-D6 同案）：主码选取对齐分支 5 的
        # _primary_issue（原取 issues[0]=两制不对码；探针实证金标回放
        # coverage.complete 恒 True、分支 4 零触发=惰性对齐零翻转，
        # log\temp\winw2fa1-probe-m6-branch4.json）。
        primary = (_primary_issue(issues) if issues else PairIssue(
            "RECALL_INCOMPLETE", "召回未达双水位完整覆盖"
        ))
        if primary.code not in UNRESOLVED_CODES:
            raise AggregateError(
                f"primary_issue code {primary.code!r} not in UNRESOLVED_CODES"
            )
        decision = "边界case/疑难case"
        internal_code = primary.code
        reason = primary.detail
    elif issues:
        primary = _primary_issue(issues)
        decision = "边界case/疑难case"
        internal_code = primary.code
        reason = primary.detail
    else:
        # D25：本 else 原承载冲突感知文案但永不可达（分支链恒在前面
        # 命中——三轮审计 C-05 死代码实证）；冲突感知已上提至
        # coverage.complete 分支，此处保留兜底并标注不可达防御。
        # 提交一：与上方分支同源——conflicts[0].code 实传（见上注）。
        decision = "不重复"
        internal_code = conflicts[0].code if conflicts else "NO_DUPLICATE_FOUND"
        reason = (_clean_public_reason(conflicts[0].detail)
                  if conflicts else "")
        reason = reason or (
            "本次限定召回及已完成直接比较中，候选均存在已验证的对应事实差异。"
            if conflicts else "本次健康的限定召回中未发现可比较的重复候选。")

    # W2 修复波 2 (a)-10（A3-F7）：扫描集补 CONFLICT_CODES（:21 现役已
    # import——纯防御收口，现役静态模板 :356-357 零命中=零行为差，
    # test_wave2_reason_leak_scan 红绿双证；金标回放 15 对翻转实证与本件
    # 无关——bisect-1 撤复两轮 payload 不动在案）。
    for internal in EQUIVALENT_CODES | UNRESOLVED_CODES | CONFLICT_CODES:
        if internal in reason:
            raise AggregateError(
                f"public reason {reason!r} must not contain internal code {internal!r}"
            )

    if decision == "重复" and not duplicate_ids:
        raise AggregateError("decision=重复 but duplicate_ids is empty")
    if decision in {"不重复", "边界case/疑难case"} and duplicate_ids:
        raise AggregateError(
            f"decision={decision} but duplicate_ids is non-empty: {duplicate_ids}"
        )

    if not current_ctx.get("item_id") or not current_ctx.get("text"):
        raise AggregateError("current item_id/text must be nonempty")

    return AggregateOutcome(
        item_id=current_ctx["item_id"],
        text=current_ctx["text"],
        decision=decision,
        duplicate_ids=duplicate_ids,
        reason=reason,
        internal_code=internal_code,
        pair_codes=tuple(pair_codes),
        used_evidence=tuple(used_evidence),
    )


__all__ = [
    "AggregateError",
    "AggregateOutcome",
    "CoverageStatus",
    "FrozenRecallPlan",
    "IllegalMemberError",
    "aggregate",
]