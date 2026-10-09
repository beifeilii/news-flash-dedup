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
import logging
from collections.abc import Iterable, Mapping
from dataclasses import replace
from typing import Any

from news_flash_dedup.compare import (
    aggregate as aggregate_module,
    core_conflict as core_conflict_module,
    p15_integration,
    pair_alignment,
    pair_compare,
)
from news_flash_dedup.compare.aggregate import (
    CoverageStatus,
    FrozenRecallPlan,
)
from news_flash_dedup.compare.pair_compare import VerifiedConflict
from news_flash_dedup.decide import judge_adapter as judge_adapter_module
from news_flash_dedup.decide import judge_pair as judge_pair_module
from news_flash_dedup.decide import judge_version_config as judge_version_config_module
from news_flash_dedup.decide.extra_event import detect_extra_event
from news_flash_dedup.decide.types import DecideOutcome
from news_flash_dedup.facts import FactValidationReport
from news_flash_dedup.facts.core import validate_fact_artifact

# 提交一（2026-10-10，p3-semantic-authority，§5.1 文件 D-3）：判官证据
# 诊断（evidence warnings/machine findings/P_* 告警）只进内部日志——
# 绝不进公共五字段（item_id/text/decision/duplicate_ids/reason）。
_logger = logging.getLogger(__name__)


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
                     budget=None,
                     judge_callable=None,
                     judge_in_chain: bool | None = None,
                     judge_timeout_s: float | None = None,
                     judge_decision_mode: str | None = None,
                     judge_version_config=None,
                     allow_prompt_direct: bool = False) -> DecideOutcome:
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

    P1-b（2026-10-09，合同 log/temp/p1-interface-contract-v1.md §四/§五 +
    工程正文 判定链改造最终方案-Codex-20261008 §3.2）：判官进主链段——
    开关 `DEDUP_JUDGE_IN_CHAIN`（judge_pair.JUDGE_IN_CHAIN_ENV）默认关，
    关=上述老行为逐字节；开=初聚合（仅用于定位未决对，不写主记录）产出
    "边界且存在未决对"时，按候选序对未决对逐对过 judge_pair 适配层
    （judge_callable(pair_context)->VerifiedJudgeProof；None=默认
    fail-closed 未决），再以 strict 口径做提交前唯一最终聚合——
    decide_for_task 的返回即终聚合结果，commit_one 只接本结果。
    `judge_in_chain` 显式 True/False 覆盖 env；`judge_timeout_s` 为单顺序
    判官调用硬超时（None=judge_pair.DEFAULT_JUDGE_TIMEOUT_S）。

    提交一（2026-10-10，p3-semantic-authority，方案 §5.1 文件 D）：
    判官一致结果（双序 duplicate→JUDGE_EQUIVALENT、双序 not_duplicate
    →JUDGE_NON_DUPLICATE）替换原未决 PairResult 时带入新 code、合并
    detail 与可用引文/完整原文回退证据；证明层告警（P_*、引文绑定、
    machine_verify.passed）不再有一票否决，只进内部诊断日志；
    `DecideOutcome.to_public_dict()` 五字段合同不变。

    提交三（2026-10-10，同方案 §5.3）：`judge_decision_mode` 灰度闸——
    None=读 DEDUP_JUDGE_DECISION_MODE（默认 legacy_proof_gate，旧证明闸
    口径生效+新口径离线对照计 judge.legacy_vs_new.changed，不增 LLM
    调用）；显式 semantic_authority=提交一口径直签。判官诊断计数挂
    `DecideOutcome.judge_diagnostics`（judge.semantic.* /
    judge.order_disagree / judge.evidence.* / judge.legacy_vs_new.changed），
    budget 在场时同步 bump_counter 轻量钩子；计数绝不进公共五字段。

    终审 P0-1（2026-10-10 模式闸）：硬冲突前置层（detect_core_conflict
    调用块）仅 active_decision_mode==semantic_authority 时执行；
    legacy_proof_gate 下整块跳过（判官照常被调用），端到端公共
    decision/reason/duplicate_ids 与基线 2d0d418 完全一致。

    终审 P1-1（2026-10-10 单源传递）+复审补丁（唯一权威）：判官版本配置
    （JudgeVersionConfig）在本入口创建一次，依次传给：判官装配
    （judge_callable 未注入且判官在主链时经 build_judge_callable/
    proof_for_order 消费同一配置）、本服务模式分流、cache key 素材
    （proof_for_order 同源 prompt/policy）、run manifest 登记
    （DecideOutcome.judge_version_config 外露供 build_run_manifest 消费）、
    审计记录（judge_decision_mode 经对级 PairResult→AuditRecord 同源）。
    JudgeVersionConfig 为**唯一权威**：`judge_decision_mode` 与
    `judge_version_config` 同传时一致则接受、不一致直接 ValueError（禁
    静默选边——防"实际合并模式=新、提示词/policy=旧"架空单源）；config
    自身经构造器校验固定映射（legacy_proof_gate→judge_v1+policy_v2、
    semantic_authority→judge_v5+policy_v3）与全量校验（注册哈希/固定
    策略）。仅传 mode（或皆缺）时按 mode 分发同一映射（config 构造器
    校验兜底）。env 仅在两实参皆缺时参与（缺席/空串=默认
    legacy_proof_gate）。

    终审复审第二轮（空模式与生产隔离，allow_prompt_direct）：收到
    decision_mode="" 的 config（按 prompt 直解、v2/v3 提示词的回放/考试
    形态）时——`allow_prompt_direct` 缺省 False：生产入口直接 ValueError
    （空模式 config 不得进生产决策链）；显式传 True（回放/考试工具专属）
    才放行，且生效合并模式必须**显式确定并记录**：经标准单源链
    （env→默认 legacy_proof_gate，非法 env 报错）解析为具体模式字面量，
    写入 active_decision_mode 并流经 PairResult→AuditRecord 审计留痕，
    不得留空串。
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

    # ------------------------------------------------------------------
    # 终审 P1-1（2026-10-10 单源传递）+复审补丁（唯一权威，冲突即错）：
    # 判官版本配置在本入口创建一次。JudgeVersionConfig 为唯一权威——
    # judge_decision_mode 与 judge_version_config 同传时：
    #   一致（mode 固定映射与 config 四维吻合）→接受，config 即单源；
    #   不一致（如 mode=semantic_authority 却传 legacy v1/v2 config）→
    #   直接 ValueError，绝不静默选边（修"实际合并模式=新、提示词/
    #   policy=旧"的单源架空形态）。config 自身的固定映射校验
    #   （legacy_proof_gate→judge_v1+policy_v2、semantic_authority→
    #   judge_v5+policy_v3）由其构造器 fail-closed 兜底。创建后的
    # JudgeVersionConfig 单源依次供给判官装配（build_judge_callable/
    # proof_for_order）、本服务模式分流、cache key 素材、run manifest
    # 登记与审计记录。env 仅在两实参皆缺时参与（缺席/空串=默认
    # legacy_proof_gate；非法值 judge_pair 明确报错，整改令二口径）。
    if judge_decision_mode is not None:
        if judge_decision_mode not in (
                judge_pair_module.MODE_LEGACY_PROOF_GATE,
                judge_pair_module.MODE_SEMANTIC_AUTHORITY):
            raise ValueError(
                f"judge_decision_mode={judge_decision_mode!r} 非法：只允许 "
                f"{judge_pair_module.MODE_LEGACY_PROOF_GATE}|"
                f"{judge_pair_module.MODE_SEMANTIC_AUTHORITY}")
    if judge_decision_mode is not None and judge_version_config is not None:
        # 唯一权威对拍：mode 的固定映射 vs 传入 config 四维——不一致即错
        mode_config = (
            judge_version_config_module.judge_version_for_mode(
                judge_decision_mode))
        if judge_version_config != mode_config:
            raise ValueError(
                f"judge_decision_mode={judge_decision_mode!r} 与 "
                f"judge_version_config 不一致（唯一权威，冲突即错）：mode "
                f"固定映射要求 {mode_config}，实得 {judge_version_config}。"
                f"同传一致则接受，不一致不得静默选边。")
        judge_version_cfg = judge_version_config
        active_decision_mode = judge_decision_mode
    elif judge_version_config is not None:
        judge_version_cfg = judge_version_config
        active_decision_mode = judge_version_config.decision_mode
        if active_decision_mode == "":
            # 终审复审第二轮（空模式与生产隔离）：按 prompt 直解的
            # config（decision_mode=空串，回放/考试通道形态）——生产
            # 入口（allow_prompt_direct 缺省 False）直接 ValueError；
            # 回放/考试工具显式传 True 才放行，且生效合并模式必须显式
            # 确定并记录：经标准单源链（env→默认 legacy_proof_gate，
            # 非法 env 报错）解析为具体模式字面量，写入
            # active_decision_mode 并流经 PairResult→AuditRecord 审计
            # 留痕，不得留空串。
            if not allow_prompt_direct:
                raise ValueError(
                    f"judge_version_config.decision_mode 为空串（按 prompt "
                    f"直解形态，prompt_version="
                    f"{judge_version_config.prompt_version!r}）：生产入口"
                    f"拒绝空模式 config。回放/考试通道须显式传 "
                    f"allow_prompt_direct=True，且生效合并模式将按标准"
                    f"单源链显式确定并审计留痕。")
            active_decision_mode = (
                judge_pair_module.judge_decision_mode())
            if not active_decision_mode:
                raise ValueError(
                    "生效合并模式解析为空串（不可达：单源链默认 "
                    "legacy_proof_gate）——空模式不得流经生产审计面")
    else:
        active_decision_mode = (judge_decision_mode
                                if judge_decision_mode is not None
                                else judge_pair_module.judge_decision_mode())
        judge_version_cfg = (
            judge_version_config_module.judge_version_for_mode(
                active_decision_mode))

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
    # P1-b（2026-10-09）：extra_event 来源 issue 平行账——判官进主链段重建
    # 终聚合 issue 集时只保留本账（对级未决 issue 由 aggregate 按终态对自动
    # 重记，初态 stale issue 不得污染终聚合）；开关关时本账零消费、老行为
    # 逐字节不变。
    extra_event_issues: list = []

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
            cand_extra_issue = pair_compare.PairIssue(
                "RULE_UNCOVERED",
                f"重合事件之外存在 {len(cand_extra.uncovered_fact_ids)} 个未覆盖的独立事件。",
            )
            new_text_issues.append(cand_extra_issue)
            extra_event_issues.append(cand_extra_issue)  # P1-b 平行账（开关关零消费）
            extra_uncovered.extend(cand_extra.uncovered_fact_ids)

    # extra_event：history vs current 首对（与原单对一致；H-03 起候选对
    # 在循环内逐对检测，见上）
    extra = detect_extra_event(history_report, current_report)
    if extra.has_extra_event:
        first_extra_issue = pair_compare.PairIssue(
            "RULE_UNCOVERED",
            f"重合事件之外存在 {len(extra.uncovered_fact_ids)} 个未覆盖的独立事件。",
        )
        new_text_issues.append(first_extra_issue)
        extra_event_issues.append(first_extra_issue)  # P1-b 平行账（开关关零消费）

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

    # ------------------------------------------------------------------
    # P1-b（2026-10-09，施工窗 P1-b）：判官进主链 + 提交前唯一最终聚合。
    # 依据：log/temp/p1-interface-contract-v1.md §四/§五 + 工程正文
    # 判定链改造最终方案-Codex-20261008 §3.2（"在 decide/service.py 中对
    # 未决对补判，然后把完整 PairResult 集合交给 compare/aggregate.py 做
    # 提交前的唯一最终聚合；若先做一次初聚合，它仅用于定位未决，不能写
    # 主记录"）。开关 DEDUP_JUDGE_IN_CHAIN 默认关（judge_in_chain=None→
    # 读 env）：关=上方初聚合结果原样下行，老行为逐字节。
    judge_active = (judge_pair_module.judge_in_chain_enabled()
                    if judge_in_chain is None else bool(judge_in_chain))
    # 提交三（§5.3 观测指标）：判官诊断计数（内部观测面；budget 在场时
    # 同步轻量计数钩子 bump_counter——同一份计数双通道，绝不进公共
    # 五字段）。语义面键释义：
    #   judge.semantic.duplicate/not_duplicate/unresolved——semantic
    #     authority 口径结论计数（legacy 模式下取离线对照的 semantic 侧）；
    #   judge.order_disagree——semantic 侧双序分歧（ORDER_DISAGREE）；
    #   judge.evidence.warn——存在任一证据告警/机检发现的对数（对级）；
    #   judge.evidence.fallback_full_text——走完整原文回退证据的对数；
    #   judge.evidence.rule.P_*——各机检规则触发次数（token 级）；
    #   judge.legacy_vs_new.changed——legacy 生效结论与 semantic 离线
    #     结论不一致的对数。
    judge_diagnostics: dict[str, int] = {}

    def _jcount(key: str, n: int = 1) -> None:
        judge_diagnostics[key] = judge_diagnostics.get(key, 0) + n
        bump = getattr(budget, "bump_counter", None)
        if bump is not None:            # 鸭子类型防御：无钩子的预算件不阻断
            bump(key, n)

    if judge_active:
        # 终审 P1-1：判定模式已在入口单源解析（active_decision_mode——
        # 显式实参 > 显式版本配置 > env；非法值入口明确报错，整改令二），
        # 版本配置 judge_version_cfg 同入口创建一次，此处不再重复解析。
        # 判官装配单源：judge_callable 未注入（None）且判官在主链时，以
        # 入口版本配置装配真件（build_judge_callable 消费同一 vc——
        # prompt/policy/cache key 素材同源；DEDUP_JUDGE_PROOF 关→返
        # None=未注入，adjudicate_pair 默认实现 fail-closed 未决，与调用
        # 方（commit_one/shadow_decide）预装配语义完全一致）。
        if judge_callable is None:
            judge_callable = judge_adapter_module.build_judge_callable(
                budget=budget, version_config=judge_version_cfg)
        # ① 聚合后判官段：初聚合产出"边界且存在未决对"时，按候选序
        # （pair_results 顺序=首对+候选到达序）对未决对逐对过判官适配层；
        # judge_callable 未注入即默认实现 fail-closed 未决（绝不冒签）。
        # 提交一（§5.1 文件 D-1/2）：规则链已判 conflict/equivalent 的对
        # 不重复调用判官（判官只补未决对）；最终聚合继续是唯一公共结果
        # 来源。
        if aggregate_outcome.decision == "边界case/疑难case":
            # 终审 P0-1（模式闸）：硬冲突前置层仅 semantic_authority 模式
            # 执行；legacy_proof_gate（默认）下整块跳过——判官照常被调用，
            # 端到端公共 decision/reason/duplicate_ids 与基线 2d0d418
            # 完全一致（硬冲突拦截是新增行为，不得渗入 legacy 面）。
            core_conflict_enabled = (
                active_decision_mode
                == judge_pair_module.MODE_SEMANTIC_AUTHORITY)

            def _core_conflict_detector_error(detector_name: str, exc) -> None:
                """终审 P0-5 fail-open 上报钩子：检测器异常→诊断计数
                （judge.core_conflict.detector_error）+警告日志；检测层
                已放行该检测器给判官，绝不中断判定。"""
                _jcount("judge.core_conflict.detector_error")
                _logger.warning(
                    "硬冲突检测器异常 fail-open 放行给判官 detector=%s: %r",
                    detector_name, exc)

            for index, pair in enumerate(pair_results):
                if pair.outcome != "unresolved":
                    continue
                # 提交一修复（2026-10-10 整改令一）：核心字段确定性硬冲突
                # 前置层——执行顺序=规则比较→硬冲突前置→硬冲突直接不重复
                # 并**跳过判官**→无硬冲突才进判官双序合并。命中即 VERIFIED_
                # CONFLICT（双侧原文证据+人类可读理由），判官零调用。
                # 终审 P0-5：逐检测器 try/except fail-open（异常→
                # judge.core_conflict.detector_error 计数+放行给判官，
                # 绝不中断判定）在 core_conflict.detect_core_conflict
                # 本体实现，经 on_detector_error 钩子接线。
                if core_conflict_enabled:
                    hard = core_conflict_module.detect_core_conflict(
                        re_freeze_required[pair.history_record_id]["text"],
                        current_text,
                        history_record_id=pair.history_record_id,
                        current_record_id=pair.current_record_id,
                        on_detector_error=_core_conflict_detector_error)
                    if hard.has_conflict:
                        conflict = VerifiedConflict(
                            field_path=hard.field_path, basis="CORE_CONFLICT",
                            history_evidence=hard.history_evidence,
                            current_evidence=hard.current_evidence,
                            detail=hard.human_reason)
                        # 终审复审第三轮：拦截路径补审计模式留痕——
                        # semantic_authority 模式下被硬冲突拦截的对，
                        # PairResult.judge_decision_mode 原为空串（replace
                        # 未设该字段），AuditRecord 看不出是 semantic 模式
                        # 触发的拦截。legacy 不走此层，模式即判定成因的
                        # 一部分，必须留痕：引用入口已解析的
                        # active_decision_mode（含 allow_prompt_direct
                        # 回放通道显式确定的合并模式）。
                        pair_results[index] = replace(
                            pair, outcome="conflict", code="VERIFIED_CONFLICT",
                            detail=hard.human_reason,
                            used_evidence=(hard.history_evidence,
                                           hard.current_evidence),
                            verified_conflicts=(conflict,),
                            judge_decision_mode=active_decision_mode)
                        pair_codes[pair.history_record_id] = "VERIFIED_CONFLICT"
                        _jcount("judge.core_conflict.intercepted")
                        _jcount(f"judge.core_conflict.{hard.conflict_type}")
                        _logger.info(
                            "核心硬冲突前置拦截 pair=%s type=%s（判官零调用）",
                            pair.pair_id, hard.conflict_type)
                        continue
                pair_context = judge_pair_module.build_pair_context(
                    pair,
                    history_text=re_freeze_required[pair.history_record_id]["text"],
                    current_text=current_text)
                judged = judge_pair_module.adjudicate_pair(
                    judge_callable, pair_context, timeout_s=judge_timeout_s,
                    decision_mode=active_decision_mode)
                # 提交一（§5.1 文件 D-3）：证据告警/机检发现只进内部
                # 诊断日志——绝不进公共五字段、绝不再改判。
                if judged.evidence_warnings or judged.machine_findings:
                    _logger.info(
                        "判官证据诊断 pair=%s status=%s warnings=%s findings=%s",
                        pair.pair_id, judged.evidence_status,
                        list(judged.evidence_warnings),
                        list(judged.machine_findings))
                # 提交三（§5.3 观测指标）：semantic 侧结论恒计（legacy
                # 模式下取离线对照值——同一份判官结果，不增 LLM 调用）。
                if judged.comparison is not None:
                    sem_outcome = judged.comparison["semantic_outcome"]
                    sem_failure = judged.comparison["semantic_failure_reason"]
                    if judged.comparison["changed"]:
                        _jcount("judge.legacy_vs_new.changed")
                        _logger.info(
                            "判官新旧口径差异 pair=%s legacy=%s/%s semantic=%s/%s",
                            pair.pair_id, judged.comparison["legacy_outcome"],
                            judged.comparison["legacy_code"], sem_outcome,
                            judged.comparison["semantic_code"])
                else:
                    sem_outcome = judged.outcome
                    sem_failure = judged.failure_reason
                if sem_outcome == "equivalent":
                    _jcount("judge.semantic.duplicate")
                elif sem_outcome == "conflict":
                    _jcount("judge.semantic.not_duplicate")
                else:
                    _jcount("judge.semantic.unresolved")
                if sem_failure == judge_pair_module.ORDER_DISAGREE:
                    _jcount("judge.order_disagree")
                if judged.evidence_warnings or judged.machine_findings:
                    _jcount("judge.evidence.warn")
                if (judge_pair_module.EVIDENCE_FALLBACK_FULL_TEXT
                        in judged.evidence_warnings):
                    _jcount("judge.evidence.fallback_full_text")
                for finding in judged.machine_findings:
                    _jcount(f"judge.evidence.rule.{finding}")
                # 提交一修复（2026-10-10 整改令四）：判官证据诊断结构化
                # 落对级结果（持久化审计由 decide/audit.py 接力）；普通
                # 日志不再是唯一载体。绝不进公共五字段。
                judged_diagnostics = dict(
                    evidence_status=judged.evidence_status,
                    evidence_warnings=tuple(judged.evidence_warnings),
                    machine_findings=tuple(judged.machine_findings),
                    full_text_fallback_used=judged.full_text_fallback_used,
                    judge_decision_mode=judged.decision_mode)
                if judged.outcome == "unresolved":
                    # 仍未决：只换终态码/detail（detail 携合同 §三枚举），
                    # 原对级证据字段不动。
                    pair_results[index] = replace(
                        pair, code=judged.code, detail=judged.detail,
                        **judged_diagnostics)
                else:
                    # ② 对级映射（§4.2 矩阵）产物落成标准 PairResult：带入
                    # 新 code（JUDGE_EQUIVALENT/JUDGE_NON_DUPLICATE）、判官
                    # 源固定措辞 detail、可用引文或完整原文回退证据（回退
                    # 证据只进内部审计）；证据 warnings/findings 入本对象
                    # 诊断字段（整改令四），但绝不进公共五字段。
                    pair_results[index] = replace(
                        pair, outcome=judged.outcome, code=judged.code,
                        detail=judged.detail,
                        used_evidence=tuple(judged.used_evidence),
                        verified_conflicts=tuple(judged.verified_conflicts),
                        **judged_diagnostics)
                pair_codes[pair.history_record_id] = judged.code
        # ③ 件级最终聚合（合同 §五 strict 口径）：本结果才是 decide_for_task
        # 的返回、才进 commit_one 写入计划；上方初聚合仅用于定位未决对，
        # 不写主记录。issue 集重建=extra_event 平行账（对级未决 issue 由
        # aggregate 按终态对自动重记，初态 stale issue 不得污染终聚合）。
        aggregate_outcome = aggregate_module.aggregate(
            current_ctx, plan, pair_results,
            new_text_issues=list(extra_event_issues),
            coverage=coverage,
            strict_required_coverage=True,
        )
        # 预算件归因覆盖闸同款逻辑对终聚合复用（T011 决议口径不变）。
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
        judge_diagnostics=judge_diagnostics,  # 提交三：判官观测计数（内部面）
        # 终审 P1-1：入口创建的判官版本配置单源外露（内部审计面——run
        # manifest 登记/审计记录消费；绝不进 to_public_dict 五字段）。
        judge_version_config=judge_version_cfg,
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