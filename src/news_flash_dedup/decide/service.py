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
from news_flash_dedup.decide import judge_machine_evidence as judge_machine_evidence_module
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
                     llm_facts=None,
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
    semantic_authority→judge_v6+policy_v4，一期 v6-lite 起映射 v6）与全量校验（注册哈希/固定
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

    一期 v6-lite（2026-10-11，分支 p3-v6-phase1，产品批准范围就两条
    规则）：semantic_authority 模式映射 judge_v6+policy_v4（judge_prompt_
    v6.py——v5 全量继承+R8 修订改值/R7 受约束回填两条条款修订）；判官
    循环内注入机器候选证据（judge_machine_evidence.build_machine_
    evidence——R8 修订候选证据复用 core_conflict 退役区 _REVISION_RE +
    R7 条件①前置硬闸证券代码/主体抽取现有件 + 条件③时间维度三分支
    证据【主窗补充令 2026-10-11：a 双方都有且一致/b 一方有一方无/
    c 双方都无→判官按硬门槛裁决——仅单一数值一致强制存疑转边界，防
    无时间锚的同口径跨期撞稿；双方时间取值不同→不重复不经回填规则】），
    机器**绝不直接判**、判官终审；R7 shadow 三计数 judge.backfill.
    triggered/signed/vetoed 入 judge_diagnostics（仅 semantic 模式），
    c) 情形单列可区分（.no_time 孪生计数，标签带时间态）。主窗补充令二
    （2026-10-11）：R7 回填独立开关（DEDUP_JUDGE_BACKFILL，policy_v4 层，
    不依赖整个 v6/semantic 开关；随 JudgeVersionConfig 单源携带+
    KNOWN_SWITCHES 快照入册可溯）——关闭时回填永不签发：硬闸触发对
    判官判"重复"→judge.backfill.disabled_vetoed 计数+对级终态强制存疑
    转边界（JUDGE_UNCERTAIN）；R8 修订规则不受影响照常工作（shadow/
    灰度期发现回填误判时不动代码、不重部署，单点关闭该规则）。
    终审 P0/P1 修复包（2026-10-11，第二 AI 终审三发现，老板逐条亲验
    属实，最小范围不扩一期）：①**R7 确定性后置签发闸**——机检硬事实
    在判官结论出来之后、对级映射之前机械执法（模型答错也拦得住）：
    双方均缺主体锚（both_missing），或恰一方缺主体+双方均无时间+
    机检一致核心数值不足 2 个（机器无法证明条件③c 硬门槛），判官判
    "重复"→撤签强制存疑转边界（JUDGE_UNCERTAIN，detail 写明闸因+判官
    原判，对级 used_evidence 留判官引文审计）；不改判不重复/存疑
    （机械闸不判语义只撤签发权）；双方都写主体不拦（R8/一般条款签发
    面不经回填通道）。②**机器证据不进判官输入**（终审 P1 证据路线
    裁定）——machine_evidence 只用于确定性闸+审计观测，判官按 v6
    条款就正文独立裁决（撤回一期"机器候选证据供判官终审"路线）。
    ③**DEDUP_JUDGE_BACKFILL 缺席=默认关**（直到硬门槛完工+金标重证
    ）。④manifest 记实际生效 backfill_enabled（run_manifest 结构化
    字段；config 与 env 显式冲突拒）。
    用户令 2026-10-10（R7 双方缺主体条款放开，主窗 2026-10-11 转发
    施工）："两条都缺主体，但事件、时间、对象和多个核心数值完全
    一致，也允许判重复。"——both_missing 不再恒拦：四要素机检
    （jme.both_missing_alignment：事件/谓词∧时间归一∧关键对象∧≥2
    一致核心数值）全证一致→放行判官签发面；任一未证成→拦（
    fail-closed 方向不变，detail 列明未证条目）。第二闸分支不动。
    默认模式仍
    legacy_proof_gate——机器证据零装配、零计数，端到端行为与基线
    2d0d418 全等。

    开工令①b（2026-10-11，LLM 抽取按需化·双轨改造）：``llm_facts``
    注入（缺省 None=双轨关，一切现役语义逐字节——判径/R7 闸/公共
    输出零触碰）时启用 track 2 按需面——
    - 触发域（令1）：仅判径对（规则未决且硬冲突未拦，semantic 模式
      机证面）双文抽取；只抽被判文本（history 侧被judged记录+current
      ）；同任务同 (record_id, text) 记忆化；无候选（聚合已定）/规则
      已定对/legacy 模式零调用零消耗（deterministic 规则段继续吃文档
      facts——build_commit_inputs 缺省供给不变）；
    - 证据面（令1）：判径对机器证据 facts 源=LLM facts（available）
      ，否则文档 facts（rule 回落只补证据——令4）；
    - 失败闭门（令4）：both_missing 域+判官判"重复"+闸放行（rule
      回落证据四要素证成）但 LLM facts 不可得（预算耗尽无缓存/抽取
      失败/零事实被拒）→撤签强制存疑转边界（不降级签重复），
      counter judge.backfill.gate.llm_facts_unavailable；
    - 协议（接口冻结）：``llm_facts.extract(record_id, text) ->
    LlmFactsOutcome(facts/available/source/reason)``——参考实现=
    recall.fact_supply.OnDemandLlmFacts（缓存键四元+日 token 闸
    +台账；预算耗尽合同：停新增/已有缓存继续用/告警计数）。判官
    输入零机器证据纪律不变（抽取只喂机证面，不进 pair_context）。
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
    #   judge_v6+policy_v4——一期 v6-lite 起映射 v6）由其构造器
    #   fail-closed 兜底。创建后的
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
        # （补充令二：对拍 config 携带传入 config 的 backfill_enabled——
        # R7 回填独立开关是 policy 层独立维度，不参与四维固定映射校验，
        # 显式传关闭态 config 不得被开关默认值架空）
        mode_config = (
            judge_version_config_module.judge_version_for_mode(
                judge_decision_mode,
                backfill_enabled=judge_version_config.backfill_enabled))
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
        # 补充令二：R7 回填独立开关（DEDUP_JUDGE_BACKFILL，policy_v4 层）
        # 随 env 单源解析入 config（终审修复包令 4：缺席=默认关；非法值
        # fail-closed）
        judge_version_cfg = (
            judge_version_config_module.judge_version_for_mode(
                active_decision_mode,
                backfill_enabled=(
                    judge_version_config_module.backfill_enabled_from_env())))

    # 补充令二：R7 回填独立开关单源读取（JudgeVersionConfig 携带——
    # 显式 config 传态>env 解析态；关闭时判官循环执行撤签：硬闸触发对
    # 判官判"重复"→强制存疑转边界，R8 修订规则不受影响）。
    backfill_enabled = judge_version_cfg.backfill_enabled

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
    # 一期 v6-lite（2026-10-11，分支 p3-v6-phase1）R7 shadow 三计数：
    #   judge.backfill.triggered——条件①机器前置硬闸通过（有且仅有一方
    #     缺主体且另一方明确写了主体）的对数（仅 semantic 模式）；
    #   judge.backfill.signed——triggered 且判官双序一致判"重复"（回填
    #     条款完成签发）的对数；
    #   judge.backfill.vetoed——triggered 但未签发（条件②-⑥任一不满足
    #     强制存疑转边界，或判官另判）的对数；
    #   judge.backfill.disabled_vetoed——补充令二：开关关闭时 triggered
    #     且判官判"重复"被撤签（对级终态强制存疑转边界）的对数（
    #     .no_time 孪生同步；signed/vetoed 开关关闭时不计数）；
    #   judge.backfill.gate.both_missing_subject / judge.backfill.gate.
    #     no_time_insufficient_values——终审 P0 修复包：确定性签发闸
    #     拦截的对数（判官判"重复"但机检硬事实不满足签发前提：双方均
    #     缺主体锚/无时间锚且一致核心数值不足两个——模型答错也拦住
    #     的可观测面；与开关态无关恒计数）。
    # 主窗补充令（2026-10-11）增量：c) 情形（双方都无时间/阶段表述，
    #   time_dimension.time_state=="both_missing"）单列可区分——上三计数
    #   各带 .no_time 后缀孪生计数（judge.backfill.triggered.no_time/
    #   signed.no_time/vetoed.no_time，标签带时间态；基础三计数仍聚合，
    #   供跨期撞稿防线灰度观测）。
    # 开工令①b（2026-10-11 双轨改造）增量：
    #   judge.llm_facts.pairs——判径对触发按需抽取的对数（llm_facts 注入
    #     且机证面开——semantic 模式专属）；
    #   judge.llm_facts.sides_unavailable——抽取侧不可得次数（预算耗尽
    #     无缓存/抽取失败/零事实被拒；判径对双文分侧计）；
    #   judge.backfill.gate.llm_facts_unavailable——失败闭门撤签对数
    #     （both_missing 域判官判"重复"+闸放行但 LLM facts 不可得→
    #     不降级，强制存疑转边界——令4）。
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

            # 一期 v6-lite（2026-10-11，分支 p3-v6-phase1）：机器确定性闸
            # 证据装配——仅 semantic_authority 模式（legacy 面零调用，默认
            # 生产行为与基线 2d0d418 全等）。终审 P1 证据路线裁定：机器
            # 证据**不进判官输入**——只用于 R7 确定性后置签发闸（判官
            # 结论出来后执法）与 shadow/审计观测；判官按 v6 条款就正文
            # 独立裁决（R8 修订候选证据复用 core_conflict 退役区
            # _REVISION_RE；R7 条件①硬闸用证券代码/主体抽取现有件；
            # 条件③c 硬门槛下限用核心数值归一集）。主体抽取现有件
            # 取数映射：pair 的 history 侧可为首对 history 或任一 candidate。
            machine_evidence_enabled = (
                active_decision_mode
                == judge_pair_module.MODE_SEMANTIC_AUTHORITY)
            records_by_id = {history["record_id"]: history}
            for _cand in candidate_list:
                records_by_id[_cand["record_id"]] = _cand

            # 开工令①b（2026-10-11 双轨改造）：track 2 按需 LLM 事实抽取
            # 记忆化——同任务同 (record_id, text) 只抽一次（判径对共享
            # current 侧零重复调用；磁盘缓存之外的任务内第二层去重）。
            _llm_facts_memo: dict = {}

            def _llm_facts_for_side(record_id: str, text: str):
                key = (record_id, text)
                if key not in _llm_facts_memo:
                    _llm_facts_memo[key] = llm_facts.extract(record_id, text)
                return _llm_facts_memo[key]

            # ------------------------------------------------------------------
            # 最终接线（点2，判官并发执行器消费——P1c 装配工厂
            # api/assemble.py build_judge_pair_executor 的消费点，
            # judge_pair_executor.py:33-34 接线合同）：
            # - ``DEDUP_JUDGE_PAIR_EXECUTOR`` 缺省 OFF（缺席/空/非真值
            #   一律关，assemble 开关族 fail-closed 默认关）——本块零动作，
            #   下方逐对循环 ``adjudicate_pair`` 走现役逐对路径**逐字节**
            #   （_pair_judge_callable 即 judge_callable 同一对象）；
            # - ON → 判径对集合（未决+硬冲突未拦——纯读预测，与逐对循环
            #   同一序同一谓词）批量预执行：双序每序=现役 judge_callable
            #   同一顺序调用（render=judge_pair._order_context 同一展开
            #   函数单对单序；call_fn 位置参数壳=adjudicate_pair 同一调用
            #   形态），经执行器对池+HTTP 信号量并发（双序同实例纪律=
            #   工厂捆扎面结构执法）；逐对循环 ``adjudicate_pair`` 改由
            #   服务闭包供给预执行结果——**adjudicate_pair 本体/判官
            #   prompt/核验/合并逻辑一字不动**（并发=纯管道）；
            # - 语义对账：预执行捕获的异常在服务闭包内**原样重放**——
            #   adjudicate_pair 的 _call_with_timeout 同一映射（Judge_
            #   BudgetExceeded→BUDGET_EXCEEDED；其余→JUDGE_EXCEPTION）；
            #   执行器层超时/拒新（status≠ok）→RuntimeError→JUDGE_
            #   EXCEPTION 未决 fail-closed（不冒签）；预读 miss（防御性
            #   缺位）同路。ON 态 judge_timeout_s 的单顺序等待闸自然
            #   让位执行器合同（pair_timeout_s 释对级槽位+传输层自带
            #   超时，语义分立见 assemble.py 开关注记）；
            # - 预扫描说明：硬冲突预测与逐对循环同一确定性检测（纯函数
            #   重放，静默钩子零计数零日志——真实拦截/计数/变异仍由逐对
            #   循环唯一执法；CPU 双跑 ON 态成本，无 LLM 调用）。
            _pair_judge_callable = judge_callable
            if judge_callable is not None:
                # 惰性导入（环导规避——api/assemble 模块级依赖
                # decide.judge_pair_executor，decide.service 模块级反向
                # 依赖 api.assemble 即成环；运行时导入时全模块已就绪。
                # 且仅判官在场才触达：无判官路径（judge=None fail-closed
                # 未决）零导入零开销，OFF 态只做开关单查）。
                from news_flash_dedup.api.assemble import (
                    JudgePairRequest as _JudgePairRequest,
                    build_judge_pair_executor as _build_judge_pair_executor,
                    judge_executor_spec_from_env as _judge_spec_from_env,
                    judge_pair_executor_enabled_from_env as _jx_enabled,
                )
                # 开关单查先行（OFF=零旋钮执法——judge_executor_spec_
                # from_env 的旋钮 ValueError 只在 ON 面解析，OFF 态判定
                # 路径对执行器旋钮 env 完全无感=现役行为逐字节）。
                if _jx_enabled():
                    _jx_spec = _judge_spec_from_env()
                    if _jx_spec.enabled:
                        _jx_pair_ctx: dict[str, dict] = {}
                        _jx_requests: list = []
                        for _pair in pair_results:
                            if _pair.outcome != "unresolved":
                                continue
                            if core_conflict_enabled:
                                _jx_hard = core_conflict_module.detect_core_conflict(
                                    re_freeze_required[_pair.history_record_id]["text"],
                                    current_text,
                                    history_record_id=_pair.history_record_id,
                                    current_record_id=_pair.current_record_id,
                                    on_detector_error=lambda _name, _exc: None)
                                if _jx_hard.has_conflict:
                                    continue
                            _jx_pc = judge_pair_module.build_pair_context(
                                _pair,
                                history_text=re_freeze_required[
                                    _pair.history_record_id]["text"],
                                current_text=current_text)
                            _jx_pair_ctx[_pair.pair_id] = _jx_pc
                            _jx_requests.append(_JudgePairRequest(
                                pair_id=_pair.pair_id,
                                text_history=re_freeze_required[
                                    _pair.history_record_id]["text"],
                                text_current=current_text))
                        if _jx_requests:
                            def _jx_render(_request, _order):
                                # 单对单序展开（组装权=业务侧：order
                                # context 展开=adjudicate_pair 同一函数
                                # judge_pair._order_context——语义零漂；
                                # 执行器结构保证 render 只见单个请求=
                                # 禁拼对）。
                                return {"pair_context": judge_pair_module._order_context(
                                    _jx_pair_ctx[_request.pair_id], _order)}

                            def _jx_call(pair_context):
                                # 位置参数壳（参数名=render 关键字面
                                # pair_context）：adjudicate_pair 同一调用
                                # 形态 judge_callable(order_ctx)（任意注入
                                # 件签名可容）。
                                return judge_callable(pair_context)

                            _jx_wired = _build_judge_pair_executor(
                                _jx_spec, call_fn=_jx_call, render=_jx_render)
                            _jx_served: dict[str, Any] = {}
                            for _jx_req, _jx_res in zip(
                                    _jx_requests, _jx_wired.execute(_jx_requests)):
                                _jx_served[_jx_req.pair_id] = _jx_res

                            def _jx_serve(_ctx):
                                # 服务闭包：adjudicate_pair 逐序调用改查
                                # 预执行结果——捕获异常原样重放（同一失败
                                # 码映射）；执行器层未完成（超时/拒新/
                                # 防御性缺位）→RuntimeError→JUDGE_
                                # EXCEPTION 未决 fail-closed。
                                _result = _jx_served.get(_ctx.get("pair_id"))
                                _status = getattr(_result, "status", None)
                                if (_status != "ok" or _result.value is None):
                                    raise RuntimeError(
                                        f"判官并发执行器单对结果不可用（pair_id="
                                        f"{_ctx.get('pair_id')!r} status={_status!r}"
                                        "）——执行器层超时/异常/拒新即未决 "
                                        "fail-closed 不冒签")
                                _order_outcome = getattr(
                                    _result.value, _ctx.get("order"))
                                if _order_outcome.error is not None:
                                    raise _order_outcome.error
                                return _order_outcome.value

                            _pair_judge_callable = _jx_serve

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
                # 一期 v6-lite（终审 P1 证据路线裁定后）：机器闸证据装配
                # （semantic 模式专属）——R8 修订候选证据+R7 条件①硬闸
                # 证据+条件③时间维度证据+核心数值下限证据；**不进判官
                # 输入**（build_pair_context 零机器证据，判官上下文键集
                # 与基线全等），只供确定性签发闸与 shadow/审计消费。
                machine_evidence = None
                llm_hist_outcome = None
                llm_cur_outcome = None
                if machine_evidence_enabled:
                    # 开工令①b（双轨改造，令1）：track2 按需 LLM facts——
                    # 判径对双文（被判文本：history 侧被judged记录+current
                    # ）；LLM facts available 即作机器证据 facts 源（质量线
                    # ），不可得→文档 facts（rule 回落只补证据——失败闭门
                    # 见下方 llm_facts_kill）。无候选（上方聚合已定不进本
                    # 段）/规则已定对（unresolved 过滤+硬冲突 continue）
                    # /legacy 模式（machine_evidence_enabled=False）零调用。
                    history_facts_source = (records_by_id.get(
                        pair.history_record_id, {}).get("facts") or ())
                    current_facts_source = current.get("facts") or ()
                    if llm_facts is not None:
                        llm_hist_outcome = _llm_facts_for_side(
                            pair.history_record_id,
                            re_freeze_required[pair.history_record_id]["text"])
                        llm_cur_outcome = _llm_facts_for_side(
                            current_record_id, current_text)
                        if llm_hist_outcome.available:
                            history_facts_source = list(
                                llm_hist_outcome.facts)
                        if llm_cur_outcome.available:
                            current_facts_source = list(
                                llm_cur_outcome.facts)
                        _jcount("judge.llm_facts.pairs")
                        for _side_outcome in (llm_hist_outcome,
                                              llm_cur_outcome):
                            if not _side_outcome.available:
                                _jcount("judge.llm_facts.sides_unavailable")
                    machine_evidence = (
                        judge_machine_evidence_module.build_machine_evidence(
                            re_freeze_required[pair.history_record_id]["text"],
                            current_text,
                            history_facts=history_facts_source,
                            current_facts=current_facts_source))
                pair_context = judge_pair_module.build_pair_context(
                    pair,
                    history_text=re_freeze_required[pair.history_record_id]["text"],
                    current_text=current_text)
                # R7 shadow 计数①：条件①机器前置硬闸通过=回填条款被触发
                # （双方都缺主体→硬闸不置位，回填不得触发，落判官存疑边界）；
                # signed/vetoed 在判官双序结论出来后分账（见下方）。
                # 补充令：c) 情形（双方都无时间/阶段表述）加 .no_time 孪生
                # 计数单列（标签带时间态，供跨期撞稿防线灰度观测）。
                backfill_gate_open = bool(
                    machine_evidence is not None
                    and machine_evidence["subject_backfill"]["unilateral_missing"])
                backfill_no_time = bool(
                    machine_evidence is not None
                    and machine_evidence.get("time_dimension", {}).get(
                        "time_state") == "both_missing")
                if backfill_gate_open:
                    _jcount("judge.backfill.triggered")
                    if backfill_no_time:
                        _jcount("judge.backfill.triggered.no_time")
                judged = judge_pair_module.adjudicate_pair(
                    _pair_judge_callable, pair_context, timeout_s=judge_timeout_s,
                    decision_mode=active_decision_mode)
                # （最终接线点2：_pair_judge_callable——OFF=judge_callable
                # 同一对象（现役逐对路径逐字节）；ON=执行器服务闭包
                # （预执行结果供给，adjudicate_pair 本体零改动）。）
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
                # R7 shadow 计数②③（一期 v6-lite）：触发的对按 semantic 侧
                # 双序结论分账——equivalent=回填条款完成签发（signed，原因
                # 码"主体单方缺失高置信对齐"随判官 reason 入证明审计件）；
                # 其余=条件②-⑥任一不满足强制存疑转边界或判官另判
                # （vetoed）。未触发不计数；legacy 恒不计数。补充令：c)
                # 情形（双方都无时间）同步分账 .no_time 孪生计数（硬门槛
                # 面跨期撞稿防线观测——signed.no_time=多数值过闸签发，
                # vetoed.no_time=仅单一数值被硬门槛拦下或其余条件不满足）。
                # 补充令二（R7 回填独立开关）：开关关闭时回填永不签发——
                # 判官若判"重复"（本应经 R7 条款签发）→ disabled_vetoed
                # 计数+对级终态强制存疑转边界（backfill_kill，见下方映射
                # 段）；判官存疑/不重复本就不经回填签发，照常放行（R8
                # 修订规则不受影响——其签发面在判官一般条款，非回填通道）。
                # 开关开=原令行为（signed/vetoed 分账不变）。
                # 终审 P0（R7 确定性后置签发闸）：机检硬事实在判官结论
                # 出来后执法（模型答错也必须拦得住）——机械闸只撤"重复"
                # 签发权，不改判不重复/存疑（不判语义）；拦截对的判官
                # 原判与引文证据经对级 detail/used_evidence 入审计留痕。
                machine_gate = (
                    judge_machine_evidence_module.r7_signing_gate(
                        machine_evidence)
                    if machine_evidence is not None else None)
                gate_block = bool(
                    machine_gate is not None
                    and machine_gate["blockable"]
                    and sem_outcome == "equivalent")
                if gate_block:
                    _jcount(f"judge.backfill.gate.{machine_gate['rule']}")
                backfill_kill = bool(
                    backfill_gate_open and not backfill_enabled
                    and sem_outcome == "equivalent")
                if backfill_gate_open:
                    if not backfill_enabled:
                        if backfill_kill:
                            _jcount("judge.backfill.disabled_vetoed")
                            if backfill_no_time:
                                _jcount(
                                    "judge.backfill.disabled_vetoed.no_time")
                    elif sem_outcome == "equivalent" and not gate_block:
                        # 签发=判官判"重复"且未被机械闸/开关拦截
                        _jcount("judge.backfill.signed")
                        if backfill_no_time:
                            _jcount("judge.backfill.signed.no_time")
                    else:
                        _jcount("judge.backfill.vetoed")
                        if backfill_no_time:
                            _jcount("judge.backfill.vetoed.no_time")
                # 开工令①b 失败闭门（令4）：both_missing 域判官判"重复"且
                # 闸放行（rule 回落证据四要素证成）但 LLM facts 不可得
                # （预算耗尽无缓存/抽取失败/零事实被拒）——不降级签重复：
                # 撤签强制存疑转边界（rule 回落只补证据，不构成 R7 四要素
                # 核证的签发质量线——用户令质量线=LLM 供给面）。
                _llm_sides_ready = bool(
                    llm_hist_outcome is not None
                    and llm_hist_outcome.available
                    and llm_cur_outcome is not None
                    and llm_cur_outcome.available)
                llm_facts_kill = bool(
                    llm_facts is not None
                    and machine_evidence is not None
                    and machine_evidence["subject_backfill"]["both_missing"]
                    and sem_outcome == "equivalent"
                    and not gate_block
                    and not _llm_sides_ready)
                if llm_facts_kill:
                    _jcount("judge.backfill.gate.llm_facts_unavailable")
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
                if gate_block:
                    # 终审 P0 执法（R7 确定性后置签发闸）：机械闸拦截——
                    # 对级终态强制存疑转边界（JUDGE_UNCERTAIN 白名单码）；
                    # detail 写明闸因+判官原判（可溯），判官引文证据入
                    # used_evidence 审计留痕（机器不判语义，只撤签发权；
                    # 判官实际双序结论经 proofs 审计件另在案）。
                    # 用户令 2026-10-10（双方缺主体条款放开）：both_
                    # missing 拦截 detail 升级为四要素未证条目清单（
                    # 四项全证一致才放行——见 jme.both_missing_
                    # alignment；未证条目=机器无法证明/核验失败的
                    # 确定性条目，fail-closed 方向不变）。
                    if machine_gate["rule"] == (
                            judge_machine_evidence_module.
                            GATE_RULE_BOTH_MISSING):
                        failed = machine_gate.get("failed_items") or ()
                        phrases = {
                            "event": "事件/谓词一致未证成",
                            "time": "时间归一一致未证成",
                            "object": "关键对象一致未证成",
                            "values": "一致核心数值不足2个",
                        }
                        items = "、".join(
                            phrases[f] for f in failed
                            if f in phrases) or "四要素"
                        gate_detail = (
                            "R7 确定性签发闸拦截：双方均缺主体锚且四要"
                            f"素机检未证全齐（{items}），判官原判'重复'"
                            "不得签发，强制存疑转边界。")
                    else:
                        matched = machine_evidence["core_values"][
                            "matched_count"]
                        gate_detail = (
                            f"R7 确定性签发闸拦截：双方均无时间锚且机检仅"
                            f"证得{matched}个一致核心数值（不足两个不同角"
                            f"色），判官原判'重复'不得签发，强制存疑转边"
                            f"界。")
                    pair_results[index] = replace(
                        pair, outcome="unresolved", code="JUDGE_UNCERTAIN",
                        detail=gate_detail,
                        used_evidence=tuple(judged.used_evidence),
                        **judged_diagnostics)
                elif backfill_kill:
                    # 补充令二终局（撤签改写）：R7 回填开关关闭——对级终态
                    # 强制存疑转边界（JUDGE_UNCERTAIN 白名单码）；判官实际
                    # 双序结论与证明已入 proofs 审计件留痕（机器不判语义，
                    # 仅执行 policy 层关闭令：R7 签发权收回，R8 及一般条款
                    # 不受影响）。detail 即公共 reason 源（可溯关闭成因）。
                    pair_results[index] = replace(
                        pair, outcome="unresolved", code="JUDGE_UNCERTAIN",
                        detail=("主体单方缺失回填规则已被独立开关关闭"
                                "（DEDUP_JUDGE_BACKFILL），强制存疑转边界。"),
                        **judged_diagnostics)
                elif llm_facts_kill:
                    # 开工令①b 终局（失败闭门）：LLM 事实不可得撤签——对级
                    # 终态强制存疑转边界（JUDGE_UNCERTAIN 白名单码）；detail
                    # 即公共 reason 源（可溯不可得成因：预算耗尽无缓存/抽取
                    # 失败/零事实被拒）；判官实际双序结论经 proofs 审计件在
                    # 案（机器不判语义，只执行质量线：R7 四要素核证据不得
                    # 降级——rule 回落只补证据）。
                    _unavail_reasons = [
                        _outcome.reason
                        for _outcome in (llm_hist_outcome, llm_cur_outcome)
                        if _outcome is not None and not _outcome.available
                        and _outcome.reason]
                    pair_results[index] = replace(
                        pair, outcome="unresolved", code="JUDGE_UNCERTAIN",
                        detail=("R7 四要素核证据不可降级：LLM 事实抽取不可得"
                                f"（{'；'.join(_unavail_reasons) or '未知原因'}），"
                                "rule 回落证据只补证据不构成签发质量线，判官"
                                "原判'重复'不得签发，强制存疑转边界。"),
                        used_evidence=tuple(judged.used_evidence),
                        **judged_diagnostics)
                elif judged.outcome == "unresolved":
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
                pair_codes[pair.history_record_id] = (
                    "JUDGE_UNCERTAIN" if (gate_block or backfill_kill
                                          or llm_facts_kill)
                    else judged.code)
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
        # 2026-10-11（主窗令·判定优先级修复 ③）：真实双侧 business_date
        # 转发入 p15——C14 同日同文放行门（同日+原文完全一致不撤证）消费；
        # 跨日本身仍由 pair_compare._check_binding fail-closed（禁签）。
        history_business_date=history_business_date,
        current_business_date=current_business_date,
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