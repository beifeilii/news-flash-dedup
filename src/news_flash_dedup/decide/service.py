"""P16-E 编排层：P16-A → P15 → P16-B → extra_event → P16-C → DecideOutcome。

按 03:44 批复标尺 1-4 + P17 03:57 修订 §6.1：
- extra_event RULE_UNCOVERED 检测落地
- DTO 封门（DecideOutcome.to_public_dict 恰五字段）
- P16-C 挂账执行（test_aggregate.py 18 WIP-DRAFT 处理）
- 09 §11.3 时间阻塞路径编排层端到端复验
- P17 集合级决策（`decide_for_task`）：对每个候选逐对跑 P16-A→P15→P16-B，
  最后一次 `aggregate` 见全 `required` 集，保证 L01/L02/L08 不变量。
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from dataclasses import replace
from typing import Any

from news_flash_dedup.compare import (
    aggregate as aggregate_module,
    p15_integration,
    pair_alignment,
    pair_compare,
)
from news_flash_dedup.compare.aggregate import (
    CoverageStatus,
    FrozenRecallPlan,
)
from news_flash_dedup.decide.extra_event import detect_extra_event
from news_flash_dedup.decide.types import DecideOutcome
from news_flash_dedup.facts import FactValidationReport
from news_flash_dedup.facts.core import validate_fact_artifact


class DecideInputError(ValueError):
    """P17 03:57 §6.1：输入不合法（如 current 与 candidates 同时缺失）。"""


def _raw_hash_or_recompute(record: Mapping, text: str) -> str:
    """M-13（外审+四轮，窗口J）raw_hash 口径统一：上游 `raw_hash` 键存在且
    非空则用键——与 re_freeze required 上下文（history["raw_hash"] /
    current["raw_hash"] 直取）同一口径；键缺失或空串才按 text 重算。
    上游键与 text 不符不再被内联重算静默洗值，由 pair_compare 绑定门
    （_sha256(text) != raw_hash → PairBindingError）fail-closed。"""
    upstream = record.get("raw_hash")
    if isinstance(upstream, str) and upstream:
        return upstream
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _strip_verified_missing(node: Any) -> None:
    """H-04 投影辅助：全槽递归剥 "verified_missing"（测试约定键，下游
    p15_integration.py:121/:208 消费保留于原工件——投影只整形校验输入；
    FACT_SCHEMA 槽 additionalProperties=False 不容该键）。"""
    if isinstance(node, dict):
        node.pop("verified_missing", None)
        for value in node.values():
            _strip_verified_missing(value)
    elif isinstance(node, list):
        for item in node:
            _strip_verified_missing(item)


def _project_artifact_for_validation(artifact: Mapping) -> dict:
    """E 批 H-04 修法 A（§3.2-②）确定性投影：同输入同投影同判定。

    - deepcopy 出投影，不碰输入对象（P1 钉）；
    - 数值 value/range_end 槽收敛为 {status,raw_value,evidence,value}
      ——剥内联束（unit/magnitude/currency/role/metric/comparator/
      approximate/range_end 等束键：束信息=兄弟槽冗余，rule.py:1110-1132
      在案，零信息损失于校验语义）；缺 value 键的 decimal 槽补
      "value": None（schema.py:25-29 decimal 必需键+missing 时 null
      闸 :52）；
    - 全槽递归剥 verified_missing（_strip_verified_missing）；
    - 其余逐字节不动（facts 顺序/fact_id/evidence/槽层级）；
    - 幂等（投影再投影=自身，P1 钉）；仅剥非 Schema 键（P2/P3 钉）。
    """
    import copy

    projected = copy.deepcopy(artifact)
    facts = projected.get("facts") if isinstance(projected, dict) else None
    if isinstance(facts, list):
        for fact in facts:
            _strip_verified_missing(fact)
            if not isinstance(fact, dict):
                continue
            numerics = fact.get("numerics")
            if not isinstance(numerics, list):
                continue
            for numeric in numerics:
                if not isinstance(numeric, dict):
                    continue
                for slot_name in ("value", "range_end"):
                    slot = numeric.get(slot_name)
                    if isinstance(slot, dict):
                        numeric[slot_name] = {
                            **{key: slot[key]
                               for key in ("status", "raw_value", "evidence")
                               if key in slot},
                            "value": slot.get("value"),
                        }
    return projected


def _wrap_facts_as_report(record_id: str, text: str, facts: list,
                          extraction_status: str = "complete") -> FactValidationReport:
    # E 批 H-04 修法 A（e-batch-design §三，G 门呈裁包②定裁：live 前置硬
    # 阻塞+全量单轨接线）：裸包装短路退役——编排路径一律经确定性投影过
    # validate_fact_artifact（单一校验合同，与 facts\core.py:467
    # FactExtractionService 模型抽取径同一校验器）。三调用点零改动。
    bare_artifact = {
        "schema_version": "1.0",
        "record_id": record_id,
        "offset_unit": "unicode_code_point",
        "extraction_status": extraction_status,
        "facts": facts,
        "unparsed_spans": [],
        "uncertainties": [],
    }
    projected = _project_artifact_for_validation(bare_artifact)
    report = validate_fact_artifact(text, record_id, projected)
    artifact = dict(bare_artifact)
    # 校验 provenance（additive 非 Schema 键）：返回工件=原包装（facts 原样
    # 含内联束，下游消费面零破）；W2Fα2-53 unvalidated/unvalidated_note
    # 过渡标注退役（修法 A 下"未过 Schema"成假命题，不留存谎言）。
    artifact["validation"] = {
        "validator": "validate_fact_artifact",
        "schema_projection": "e1_numeric_slot_v1",
        "issue_count": len(report.issues),
    }
    return FactValidationReport(
        record_id=record_id,
        # 状态合取保留（§3.2-③④）：status=="complete" 语义不放大——
        # partial/failed+洁净工件不洗 True；真值化只收紧 facts 维。
        validated_complete=bool(report.validated_complete)
        and extraction_status == "complete",
        extraction_status=extraction_status,
        # 报告字段真值化升级：issues/valid_fact_ids/numeric_inventory 全部
        # 取校验器真口径（现役恒空/布尔口径一并退役；W-R3c g None/缺键
        # 拦截语义由校验器 :355-362 同源承载，对齐不破坏）。
        valid_fact_ids=report.valid_fact_ids,
        numeric_inventory=report.numeric_inventory,
        issues=report.issues,
        artifact=artifact,
    )


def _record_context(_record: Mapping, *, record_id: str, item_id: str,
                    text: str, raw_hash: str, arrival_seq: int,
                    scope_id: str = "default", business_date: str = "2026-09-26",
                    pipeline_version: str = "dedup_v1") -> dict:
    """W2Fα2-8⑤：history/current 两 context 构造函数逐字重复 → 抽单源
    （原 _history_context/_current_context L71-92 同体双份；首参为兼容
    形位不入体）。两旧名保留为别名（调用方零改动）。"""
    return {
        "record_id": record_id, "item_id": item_id, "text": text,
        "raw_hash": raw_hash, "scope_id": scope_id,
        "business_date": business_date, "arrival_seq": arrival_seq,
        "pipeline_version": pipeline_version,
    }


_history_context = _record_context
_current_context = _record_context


def decide_once(history: Mapping, current: Mapping, *,
                dictionary_version: str = "dict_v1",
                alignment_version: str = "alignment_v1",
                pipeline_version: str = "dedup_v1",
                coverage_complete: bool,
                visible_seq: int | None = 10,
                prepared_seq: int | None = 10,
                dictionary=None) -> DecideOutcome:
    """编排：build_aligned → P15 集成 → compare_pair → extra_event → aggregate → DecideOutcome。

    保留为单对场景便捷包装。集合级（P17）请用 `decide_for_task`。
    `dictionary`（D19）：可选 NormalizationDictionary，None 路径行为不变。
    """
    return decide_for_task(history, [], current=current,
                            dictionary_version=dictionary_version,
                            alignment_version=alignment_version,
                            pipeline_version=pipeline_version,
                            coverage_complete=coverage_complete,
                            visible_seq=visible_seq,
                            prepared_seq=prepared_seq,
                            dictionary=dictionary)


def decide_for_task(history: Mapping, candidates: Iterable[Mapping], *,
                     current: Mapping | None = None,
                     dictionary_version: str = "dict_v1",
                     alignment_version: str = "alignment_v1",
                     pipeline_version: str = "dedup_v1",
                     coverage_complete: bool,
                     visible_seq: int | None = 10,
                     prepared_seq: int | None = 10,
                     dictionary=None,
                     budget=None) -> DecideOutcome:
    """集合级决策（P17 03:57 修订 §6.1）：当前条 vs 全部 `required` 候选。

    流水线（窗口V 实述勘正：旧述第 3 步 `detect_extra_event(history,
    current_or_first_candidate)` 系 H-03 前形态，第 2/5 步亦与逐对实现漂移，
    按现行实现如实改写）：
    1. `current=None` → `DecideInputError`（F4-4 守卫，fail-closed）；
    2. history 与 current 各包裹为 `FactValidationReport`；
    3. 首对 (history, current) 先行、候选循环每对 (candidate, current) 逐对
       跑 P16-A → P15 → P16-B → 收集 `pair_results`；`outcome=="unresolved"`
       的对以 `PairIssue(code, detail)` 入 `new_text_issues`；
    4. extra_event 检测（H-03）：候选循环内对每对 (candidate, current) 真跑
       `detect_extra_event`，循环外对首对 (history, current) 跑一次；存在未
       覆盖独立事件即记 RULE_UNCOVERED 入 `new_text_issues`，uncovered id
       分账累积（候选对→`extra_uncovered`，首对→`extra.uncovered_fact_ids`）；
    5. 构造 `FrozenRecallPlan(version, required=history ∪ candidates)`（按
       (arrival_seq, record_id) 升序）；
    6. `aggregate(current_ctx, frozen_plan, pair_results, ...)` —— aggregate
       必须同时见全部 pair_results + 完整 required 集（L01/L02/L08 不变量基础）；
    7. 返回 `DecideOutcome`（`unresolved_fields` = 首对 uncovered + 候选
       uncovered + `aggregate_outcome.internal_code`）。
    """
    if current is None:
        # F4-4（四轮 D 轮，窗口J 守卫）：current=None 退化路径原先以
        # current=history 续跑，首对自比由 pair_alignment 深层冒出
        # PairAlignmentError("history and current record_id must differ")——
        # 非语义异常、调用方无法与输入错误区分。守卫：一律 DecideInputError
        # （fail-closed 带语义；candidates 为空与否均同型，既有
        # test_decide_for_task_raises_when_no_current_and_no_candidates 不破）。
        raise DecideInputError(
            "decide_for_task requires explicit current; the current=None "
            "degenerate path (current=history self-pair) is unsupported"
        )

    # W-A 族⑤(b)（A3-F2）：identity 四键显式在检（身份不可发明，不做
    # 重算容错）——裸 KeyError 归入声明通道 DecideInputError（F4-4 同族、
    # ValueError 子型，调用方既有捕获面零冲击）；只查四必需键，不拒额外键。
    for side, record in (("history", history), ("current", current)):
        missing = [k for k in ("record_id", "text", "item_id", "arrival_seq")
                   if k not in record]
        if missing:
            raise DecideInputError(
                f"{side} record missing required keys: {missing}")

    history_record_id = history["record_id"]
    current_record_id = current["record_id"]
    history_text = history["text"]
    current_text = current["text"]

    history_report = _wrap_facts_as_report(
        history_record_id, history_text, history.get("facts", []),
        extraction_status=history.get("extraction_status", "complete"),
    )
    current_report = _wrap_facts_as_report(
        current_record_id, current_text, current.get("facts", []),
        extraction_status=current.get("extraction_status", "complete"),
    )

    # 逐对跑 P16-A → P15 → P16-B
    pair_results: list = []
    pair_codes: dict[str, str] = {}
    candidate_list = list(candidates)
    # W-A 族⑤(b)（A3-F2）：history/current raw_hash 拉齐候选侧 M-13 口径
    # （上游键优先、缺键/空串才按 text 重算；键在但错配仍由 pair_compare
    # 绑定门 fail-closed，本层绝不洗值）——一次计算 :193/:212/:340 同引，
    # 消除双解析漂移面。
    history_raw_hash = _raw_hash_or_recompute(history, history_text)
    current_raw_hash = _raw_hash_or_recompute(current, current_text)
    re_freeze_required: dict[str, dict] = {
        history_record_id: _history_context(
            history,
            record_id=history_record_id,
            item_id=history["item_id"],
            text=history_text,
            raw_hash=history_raw_hash,
            arrival_seq=history["arrival_seq"],
            scope_id=history.get("scope_id", "default"),
            business_date=history.get("business_date", "2026-09-26"),
            pipeline_version=pipeline_version,
        ),
    }
    new_text_issues: list = []
    # H-03（外审+四轮，窗口J）：候选逐对 extra_event 的 uncovered id 累积器
    # （首对 history vs current 仍由循环外 detect_extra_event 记账，见下）。
    extra_uncovered: list = []

    # W2Fα2-8④：原死局部变量（自 re_freeze_required 取 history 侧 ctx 后
    # 零读取，原 L196-197）拆除；aggregate 只消费 current_ctx/plan。
    current_ctx = _current_context(
        current,
        record_id=current_record_id,
        item_id=current["item_id"],
        text=current_text,
        raw_hash=current_raw_hash,
        arrival_seq=current["arrival_seq"],
        scope_id=current.get("scope_id", "default"),
        business_date=current.get("business_date", "2026-09-26"),
        pipeline_version=pipeline_version,
    )
    _alignment, _p15, pair_first = _run_single_pair(
        history_record_id, history_text, history_report,
        current_record_id, current_text, current_report,
        history["arrival_seq"], current["arrival_seq"],
        history["item_id"], current["item_id"],
        dictionary_version, alignment_version, pipeline_version,
        history_scope_id=history.get("scope_id", "default"),
        history_business_date=history.get("business_date", "2026-09-26"),
        current_scope_id=current.get("scope_id", "default"),
        current_business_date=current.get("business_date", "2026-09-26"),
        history_raw_hash=history.get("raw_hash"),
        current_raw_hash=current.get("raw_hash"),
        dictionary=dictionary,
    )
    pair_results.append(pair_first)
    pair_codes[history_record_id] = pair_first.code
    # 三轮审计 C-06 修复（D25）：首对门控与候选循环不一致——原以
    # unresolved_fields 非空为门，VERIFIED_CONFLICT 对携 leftover 字段时
    # 被路由成 PairIssue → aggregate 白名单（fail-closed 设计）崩溃
    # （71be651f/7d4bf35f/fdd302e0/044281ff 四对实证）。与候选循环统一
    # 为 outcome 门控：conflict 对不是 issue（冲突即证伪非重复，由聚合
    # 判"不重复"），白名单保持原封不动的 fail-closed 守卫。
    if pair_first.outcome == "unresolved":
        new_text_issues.append(
            pair_compare.PairIssue(pair_first.code, pair_first.detail))

    # 与每个 candidate 对齐的项
    for cand in candidate_list:
        cand_record_id = cand["record_id"]
        cand_text = cand["text"]
        cand_ctx = {
            "record_id": cand_record_id,
            "item_id": cand["item_id"],
            "text": cand_text,
            "raw_hash": _raw_hash_or_recompute(cand, cand_text),  # M-13：上游键优先，缺省才重算
            "scope_id": cand.get("scope_id", "default"),
            "business_date": cand.get("business_date", "2026-09-26"),
            "arrival_seq": cand["arrival_seq"],
            "pipeline_version": pipeline_version,
        }
        re_freeze_required[cand_record_id] = cand_ctx
        cand_report = _wrap_facts_as_report(
            cand_record_id, cand_text, cand.get("facts", []),
            extraction_status=cand.get("extraction_status", "complete"),
        )
        _alignment_c, _p15_c, pair_c = _run_single_pair(
            cand_record_id, cand_text, cand_report,
            current_record_id, current_text, current_report,
            cand["arrival_seq"], current["arrival_seq"],
            cand["item_id"], current["item_id"],
            dictionary_version, alignment_version, pipeline_version,
            history_scope_id=cand.get("scope_id", "default"),
            history_business_date=cand.get("business_date", "2026-09-26"),
            current_scope_id=current.get("scope_id", "default"),
            current_business_date=current.get("business_date", "2026-09-26"),
            history_raw_hash=cand.get("raw_hash"),
            current_raw_hash=current.get("raw_hash"),
            dictionary=dictionary,
        )
        pair_results.append(pair_c)
        pair_codes[cand_record_id] = pair_c.code
        if pair_c.outcome == "unresolved":
            new_text_issues.append(pair_compare.PairIssue(pair_c.code, pair_c.detail))
        # H-03（外审+四轮，窗口J）：extra_event 逐对检测——原先只在候选循环
        # 外对 (history, current) 跑一次，候选 #2+ 的独立事件组合不入账
        # （p15_integration.uncovered_independent_relation 恒 False 使
        # pair_compare L305-306 消费分支在编排层无供给，候选独立事件经对齐
        # leftover 退化为 FACT_INCOMPLETE）。现对每对 (candidate, current)
        # 真跑 detect_extra_event 记账；candidates=() 路径（decide_once /
        # 固定候选回放形态）本循环不执行，行为逐字节不变。
        cand_extra = detect_extra_event(cand_report, current_report)
        if cand_extra.has_extra_event:
            new_text_issues.append(pair_compare.PairIssue(
                "RULE_UNCOVERED",
                f"重合事件之外存在 {len(cand_extra.uncovered_fact_ids)} 个未覆盖的独立事件。",
            ))
            extra_uncovered.extend(cand_extra.uncovered_fact_ids)

    # extra_event：history vs current 首对（与原单对一致；H-03 起候选对
    # 在循环内逐对检测，见上）
    extra = detect_extra_event(history_report, current_report)
    if extra.has_extra_event:
        new_text_issues.append(pair_compare.PairIssue(
            "RULE_UNCOVERED",
            f"重合事件之外存在 {len(extra.uncovered_fact_ids)} 个未覆盖的独立事件。",
        ))

    # re_freeze（P17 03:57 修订 §6.3）：按 arrival_seq 升序构造 FrozenRecallPlan
    plan = FrozenRecallPlan(
        version="rrf_v1_k60_30_10",
        required=dict(sorted(
            re_freeze_required.items(),
            key=lambda kv: (kv[1]["arrival_seq"], kv[1]["record_id"]),
        )),
    )

    coverage = CoverageStatus(
        visible_seq=visible_seq,
        prepared_seq=prepared_seq,
        complete=coverage_complete,
    )

    aggregate_outcome = aggregate_module.aggregate(
        current_ctx, plan, pair_results,
        new_text_issues=new_text_issues,
        coverage=coverage,
    )

    # W2 ⑩①-8（T011 决议）：预算件归因覆盖闸——边界裁决+预算耗尽→
    # exhaustion_attribution() 具体码（CANDIDATE_BUDGET_EXHAUSTED/
    # DEPENDENCY_TIMEOUT）替换 internal_code；已证实面（重复/不重复）
    # 不覆盖（h4 钉）；预算未尽/None→现役归因零改动。reason 文本不携
    # 新码（既有 reason 扫描闸零冲击）。
    budget_attribution = (budget.exhaustion_attribution()
                          if budget is not None else None)
    if (budget_attribution is not None
            and aggregate_outcome.decision == "边界case/疑难case"):
        aggregate_outcome = replace(aggregate_outcome,
                                    internal_code=budget_attribution)

    unresolved_fields = list(extra.uncovered_fact_ids) + extra_uncovered
    if aggregate_outcome.internal_code:
        unresolved_fields.append(aggregate_outcome.internal_code)

    return DecideOutcome(
        item_id=current["item_id"],
        text=current_text,
        decision=aggregate_outcome.decision,
        duplicate_ids=aggregate_outcome.duplicate_ids,
        reason=aggregate_outcome.reason,
        internal_code=aggregate_outcome.internal_code,
        pair_codes=pair_codes,
        used_evidence=tuple(aggregate_outcome.used_evidence),  # 13:20 案三a整改：真证据引用，不混 pair_codes
        unresolved_fields=tuple(unresolved_fields),
        raw_hash=current_raw_hash,
        pipeline_version=pipeline_version,
        pair_results=tuple(pair_results),  # R9 F4：外露对级结果供 commit 审计批次
    )


def _run_single_pair(history_record_id, history_text, history_report,
                      current_record_id, current_text, current_report,
                      history_arrival_seq, current_arrival_seq,
                      history_item_id, current_item_id,
                      dictionary_version, alignment_version, pipeline_version,
                      *, history_scope_id="default",
                      history_business_date="2026-09-26",
                      current_scope_id="default",
                      current_business_date="2026-09-26",
                      history_raw_hash=None,
                      current_raw_hash=None,
                      dictionary=None):
    """单对跑 P16-A → P15 → P16-B；返回 (alignment, p15_report, pair)。

    R9 F2 残余修复（主窗口 06:5x）：对级 ctx 原先硬编码 scope_id="default"，
    pair_compare L164-172 对级绑定门被洗值架空（仅靠 aggregate 门兜底）；
    现由 decide_for_task 转发真实 scope_id/business_date，恢复双层执法。
    """
    # M-13（窗口J）：对级 raw_hash 上游键优先（调用方传 history/current/cand
    # mapping 的 .get("raw_hash")），缺省（None/空串）才按 text 重算——与
    # re_freeze required 上下文同口径；键文不符由 pair_compare 绑定门
    # fail-closed，不再 __import__('hashlib') 内联重算洗值。
    if not (isinstance(history_raw_hash, str) and history_raw_hash):
        history_raw_hash = hashlib.sha256(history_text.encode("utf-8")).hexdigest()
    if not (isinstance(current_raw_hash, str) and current_raw_hash):
        current_raw_hash = hashlib.sha256(current_text.encode("utf-8")).hexdigest()
    alignment = pair_alignment.build_aligned(
        history_report, current_report, history_text, current_text,
        dictionary_version=dictionary_version,
        alignment_version=alignment_version,
        dictionary=dictionary,
    )
    p15_report = p15_integration.extract_p15_results(
        history_report, current_report, history_text, current_text, alignment,
    )
    history_ctx = {
        "record_id": history_record_id, "item_id": history_item_id,
        "text": history_text,
        "raw_hash": history_raw_hash,
        "scope_id": history_scope_id,
        "business_date": history_business_date,
        "arrival_seq": history_arrival_seq, "pipeline_version": pipeline_version,
    }
    current_ctx = {
        "record_id": current_record_id, "item_id": current_item_id,
        "text": current_text,
        "raw_hash": current_raw_hash,
        "scope_id": current_scope_id,
        "business_date": current_business_date,
        "arrival_seq": current_arrival_seq, "pipeline_version": pipeline_version,
    }
    pair = pair_compare.compare_pair(
        history_ctx, current_ctx,
        history_artifact=history_report, current_artifact=current_report,
        alignment=alignment,
        pipeline_version=pipeline_version,
        p15_results=p15_report.p15_results,
    )
    return alignment, p15_report, pair