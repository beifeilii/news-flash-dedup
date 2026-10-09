"""P1 联调真件适配器（2026-10-09，施工窗 P1-a，合流树 p1-integrated）。

定位：P1-a 证明组件（judge_proof.py 证明级核验 + llm_residual.py 双序判官）
→ P1-b 合同 `judge_callable(pair_context) -> VerifiedJudgeProof`（合同
`log/temp/p1-interface-contract-v1.md` §一 JSON 形态 dict）的**真件接线**。
录取线=judge_pair.validate_proof 合同级硬校验（不合规自动 fail-closed 未决）。

依据（冻结件）：合同 §一/§二/§三 + 判定宪章 policy_v2（机检语义口径）。
文件纪律：本模块只接线不改判定语义；judge_proof/llm_residual/judge_pair
三件套零改动。

提交一（2026-10-10，分支 p3-semantic-authority，方案
`log/快讯去重_判官与证明层改造_可执行技术方案.md` §5.1 文件 B）：
- proof 新增 `reason`（取证明级核验留存的判官理由，缺省稳定兜底文案）；
- `machine_verify` 改**审计语义**：`mode="audit"`，`rules_triggered` 如实
  携带全部机器/绑定 P_*，`passed` 保留用于兼容现有审计数据——**消费侧
  不得再拿它改判**（judge_pair 自提交一起只把它当诊断）；
- 全部 P_* 记录保留不丢失：机器/绑定类入 rules_triggered，证据充分性
  P_NO_AXIS 入 proof 级 `evidence_warnings`——只告警，**不得**再把双序
  不重复降成未决（§4.2 矩阵：双序 not_duplicate → conflict）。

合同映射表（真件内部形态 → 合同形态，逐项）：
- 粒度：合同=每顺序一份证明（adjudicate_pair 双序各调一次）；本适配器每次
  调用只跑一个顺序（"ab"→内部 hc、"ba"→内部 ch，build_pair_context 注记
  "ab 与 hc 同向"），对级装配（双序组合/ORDER_DISAGREE）由 P1-b 适配层执行。
- verdict：内部 重复→duplicate / 不重复→not_duplicate / 存疑→doubtful /
  结构非法→invalid / 调用失败→failure（合同五态）。
- reason：内部 OrderVerification.judgment["reason"]（validate_judge 已保证
  非空）→ proof["reason"]；无存核（存疑/失败/非法顺序不送核）→ 稳定兜底
  文案 _REASON_FALLBACK。
- 引文：内部 OrderVerification.citations（side/field/quote/start/end，
  Unicode 码点 offset）→ quotes_a/quotes_b[{text, offset_start, offset_end,
  role=内部 field 槽位 evidence|numbers|times}]；绑定失败走 P_OFFSET 入
  rules_triggered（提交一起为诊断，不再有一票否决，见下）。
- 机侧分项结论三态（合同 numeric_check/time_check.conclusion，机侧独立
  抽取口径）：机检冲突→"不一致"；任一侧无机检材料（含双侧皆无）→
  "无法判定"（宪章缺失例外≠不一致，合同不拦）；双侧在场且无双向差异→
  "一致"。time_check.anchors_a/b=机抽归一日期锚列表。
- machine_verify={"mode": "audit", "rules_triggered": [P_*], "passed": bool}
  （提交一 audit 语义）：本适配器内部核验姿态=audit（不自我降级存疑，
  触发如实入 rules_triggered）；passed=false 当且仅当存在 P_* 机器/绑定
  失败（P_OFFSET 含）——**兼容保留字段，消费侧不得再拿它改判**；
  P_NO_AXIS 不入 rules_triggered（证据充分性问题，走 proof 级
  evidence_warnings 告警通道，见下）。
- 证据充分性告警：内部 P_NO_AXIS（不重复无机器可证伪轴）→
  proof["evidence_warnings"]=["P_NO_AXIS"]（提交一：只告警，不降级双序
  不重复为未决；§4.2 矩阵 not_duplicate+not_duplicate→conflict）。
- 不重复证伪（合同 §二 falsification）：内部首条证伪轴（定序）→
  {dimension, evidence_a/evidence_b{text, offset_start, offset_end},
  relation=内部 correspondence}。dimension=内部轴原生直发（D7 裁定
  2026-10-09：合同 v2 词表=实现五族 subject/numeric/time/stage/
  polarity，"event" 无机检判据删除——不再最近邻映射；内部轴不在
  合同词表=组件漂移，fail-closed 抛错归 JUDGE_EXCEPTION 未决）。
  无机检轴（P_NO_AXIS）→ 不携 falsification——提交一起由 validate_proof
  记 P_NO_AXIS 证据充分性告警（不再降级 doubtful）。
- 版本四维：model_version=cfg.model、prompt_sha256/prompt_version/
  policy_version 从 judge_version_config 统一版本配置对象同源读取
  （提交一修复并入提交二复审条 1：judge_v1/v2/v3→policy_v2 原 policy、
  judge_v5→policy_v3，禁止各自取默认值；judge_proof 组件内部机检口径
  仍为 policy_v2 宪章，诊断与治理分层各记）、judged_at=UTC ISO（合同
  必填；内部审计负载的确定性纪律不及此合同面——judged_at 只进合同
  证明件，不进 llm_residual 审计槽）。
- cache_key=合同 §一 逐字公式（judge_pair.compute_cache_key 重算自证）。
- 预算：内部预算尽（_call_cached 两枚预算字面错误前缀）→
  judge_pair.JudgeBudgetExceeded（合同 BUDGET_EXCEEDED 通道）；其余调用
  失败→verdict=failure（合同 JUDGE_FAILURE 未决）。超时=adjudicate_pair
  线程硬超时层（TIMEOUT），本适配器同步执行不自带超时。

装配闸（开关语义）：build_judge_callable 在 DEDUP_JUDGE_PROOF 关时返
None——装配侧未注入=adjudicate_pair 默认实现 fail-closed 未决
（JUDGE_NOT_INJECTED）；DEDUP_JUDGE_IN_CHAIN 关=decide_for_task 判官段
整体跳过（老行为逐字节）。双开关皆开=真件进主链。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from . import judge_pair, judge_proof, judge_version_config, llm_residual
from . import machine_verify as _mv

__all__ = ["build_judge_callable", "default_judge_config", "proof_for_order"]

# 合同 order → 内部顺序（"ab" a=history 先=hc 同向）
_ORDER_TO_INTERNAL = {"ab": "hc", "ba": "ch"}

# 内部预算尽两枚字面错误前缀（llm_residual._call_cached 预算环抛出，
# _judge_order 收容为 status="failure"、error=str(exc)[:300]——前缀稳定）
_BUDGET_ERROR_PREFIXES = ("预算软边界", "LLM 调用预算拒付")

# proof["reason"] 稳定兜底文案（无存核顺序：存疑/失败/非法不送证明级
# 核验，判官理由不可得——兜底不冒充模型原话）
_REASON_FALLBACK = "（判官本顺序未形成可绑定理由）"


def _budget_exhausted(error: str | None) -> bool:
    return bool(error) and error.startswith(_BUDGET_ERROR_PREFIXES)


def _numeric_conclusion(verification, text_a: str, text_b: str) -> str:
    """机侧数值三态（合同 conclusion；机抽口径，不信判官供数）。"""
    if verification.numeric_check["machine_conflict"]:
        return "不一致"
    na = {m["norm"] for m in _mv.number_mentions(text_a)}
    nb = {m["norm"] for m in _mv.number_mentions(text_b)}
    if not (na and nb):
        return "无法判定"            # 任一侧缺/皆无（宪章缺失例外≠不一致）
    return "一致"


def _time_conclusion(verification) -> str:
    """机侧时间三态（同上口径）。"""
    if verification.time_check["machine_conflict"]:
        return "不一致"
    if not (verification.time_check["machine_times_a"]
            and verification.time_check["machine_times_b"]):
        return "无法判定"
    return "一致"


def proof_for_order(judge: "llm_residual.SyncResidualJudge",
                    ctx: Mapping,
                    *, version_config=None) -> dict:
    """单顺序真件证明：跑内部证明级核验， emit 合同 §一 形态 dict。

    ctx=judge_pair._order_context 产物（含 order/item ids/text_a/text_b）。
    预算尽 → JudgeBudgetExceeded（合同 BUDGET_EXCEEDED 异常通道）。

    终审 P1-1（单源传递）：version_config 在场（decide_for_task 入口创建
    的同一 JudgeVersionConfig）时 prompt_sha/policy 从其同源读取——显式
    实参语义权威，不再各自回落 env/配置重解；缺省维持原口径（按
    judge.config.prompt_version 经 judge_version_config 单解）。cache key
    素材（compute_cache_key 的 prompt_sha/policy 位）同对象消费。
    """
    order = ctx["order"]
    internal_order = _ORDER_TO_INTERNAL[order]      # "ab"→"hc"、"ba"→"ch"
    pair_id = ctx["pair_id"]
    text_a, text_b = ctx["text_a"], ctx["text_b"]
    cfg = judge.config
    # 提交一修复并入提交二复审条 1：prompt_sha/policy 从统一版本配置对象
    # 同源读取（judge_v1/v2/v3→policy_v2 原 policy；judge_v5→policy_v3；
    # 未登记 fail-closed）——禁止各自取默认值；cache_key 同对象消费。
    # judge_proof 证明组件内部机检口径仍为 v2 宪章（PROOF_POLICY_VERSION
    # 不变，旧证明工件复验兼容）——治理口径与诊断口径分层，如实各记。
    vc = (version_config if version_config is not None
          else judge_version_config.judge_version_for_prompt(
              cfg.prompt_version))
    prompt_sha = vc.prompt_sha256
    policy = vc.policy_version
    sha_a = judge_proof.text_sha256(text_a)
    sha_b = judge_proof.text_sha256(text_b)

    outcome = judge._judge_order(pair_id, order=internal_order,
                                 text_a=text_a, text_b=text_b,
                                 proof_mode=True)
    if outcome.status == "failure":
        if _budget_exhausted(outcome.error):
            raise judge_pair.JudgeBudgetExceeded(
                outcome.error or "判官预算尽")
        verdict = "failure"
    elif outcome.status == "invalid":
        verdict = "invalid"
    elif outcome.decision == "重复":
        verdict = "duplicate"
    elif outcome.decision == "不重复":
        verdict = "not_duplicate"
    else:
        verdict = "doubtful"                # 存疑（含 gate 降级残留形态）

    verification = outcome.proof_verification
    quotes_a: list[dict] = []
    quotes_b: list[dict] = []
    failures: list[str] = []
    evidence_warnings: list[str] = []
    numeric = {"conclusion": "无法判定", "details": []}
    time_chk = {"conclusion": "无法判定", "anchors_a": [], "anchors_b": []}
    falsification: dict | None = None
    reason = _REASON_FALLBACK
    if verification is not None:
        judgment_reason = verification.judgment.get("reason")
        if isinstance(judgment_reason, str) and judgment_reason.strip():
            reason = judgment_reason.strip()      # 判官理由实传（§5.1 文件 B-1）
        for span in verification.citations:
            quote = {"text": span.quote, "offset_start": span.start,
                     "offset_end": span.end, "role": span.field}
            (quotes_a if span.side == "A" else quotes_b).append(quote)
        # 全部 P_* 记录保留（§5.1 文件 B-3 不丢诊断）：机器/绑定类入
        # rules_triggered（audit 语义，passed 兼容保留但消费侧不得改判）；
        # 证据充分性 P_NO_AXIS 入 proof 级 evidence_warnings——只告警，
        # 不得再把双序不重复降成未决（§5.1 文件 B-4）。
        failures = [f for f in verification.failures
                    if f != judge_proof.P_NO_AXIS]
        if judge_proof.P_NO_AXIS in verification.failures:
            evidence_warnings.append(judge_proof.P_NO_AXIS)
        numeric = {
            "conclusion": _numeric_conclusion(verification, text_a, text_b),
            "details": [{"only_a": verification.numeric_check["only_a"],
                         "only_b": verification.numeric_check["only_b"]}],
        }
        time_chk = {
            "conclusion": _time_conclusion(verification),
            "anchors_a": list(verification.time_check["machine_times_a"]),
            "anchors_b": list(verification.time_check["machine_times_b"]),
        }
        if verdict == "not_duplicate" and verification.axes:
            ax = verification.axes[0]       # 内部定序首轴（确定性）
            dimension = ax["axis"]
            if dimension not in judge_pair.FALSIFICATION_DIMENSIONS:
                # 内部轴漂移出合同 v2 词表（D7 五族）——fail-closed，
                # adjudicate_pair 收容为 JUDGE_EXCEPTION 未决，不冒充。
                raise ValueError(
                    f"证伪轴 {dimension!r} 不在合同词表 "
                    f"{judge_pair.FALSIFICATION_DIMENSIONS}")
            falsification = {
                "dimension": dimension,
                "evidence_a": {"text": ax["quote_a"],
                               "offset_start": ax["start_a"],
                               "offset_end": ax["end_a"]},
                "evidence_b": {"text": ax["quote_b"],
                               "offset_start": ax["start_b"],
                               "offset_end": ax["end_b"]},
                "relation": ax["correspondence"],
            }

    proof: dict[str, Any] = {
        "pair_id": pair_id,
        "item_a_id": ctx["item_a_id"],
        "item_b_id": ctx["item_b_id"],
        "text_a_sha256": sha_a,
        "text_b_sha256": sha_b,
        "order": order,
        "verdict": verdict,
        "reason": reason,
        "quotes_a": quotes_a,
        "quotes_b": quotes_b,
        "numeric_check": numeric,
        "time_check": time_chk,
        # 提交一（§5.1 文件 B-2）：machine_verify 审计语义——mode="audit"
        # 如实记录；passed 兼容保留（存在 P_* 机器/绑定失败即 false），
        # 消费侧不得再拿它改判。
        "machine_verify": {"mode": "audit",
                           "rules_triggered": failures,
                           "passed": not failures},
        "model_version": cfg.model,
        "prompt_sha256": prompt_sha,
        "policy_version": policy,
        "judged_at": datetime.now(timezone.utc).isoformat(),
    }
    if evidence_warnings:
        proof["evidence_warnings"] = evidence_warnings
    if falsification is not None:
        proof["falsification"] = falsification
    proof["cache_key"] = judge_pair.compute_cache_key(
        cfg.model, prompt_sha, policy, order, sha_a, sha_b)
    return proof


def default_judge_config(
        environ: Mapping | None = None,
        *, version_config=None,
) -> "llm_residual.ResidualJudgeConfig":
    """装配闸默认配置单源（提交一修复并入提交二复审条 2/3）：
    prompt_version 由 DEDUP_JUDGE_DECISION_MODE 经 judge_version_config
    决定（legacy_proof_gate 默认→judge_v1+policy_v2；semantic_authority→
    judge_v5+policy_v3）——judge_v5 不再无条件默认；mv 姿态恒 audit
    （核验姿态不自我降级，降级权归合同层）。manifest 一致性钉测对本
    函数与 lib.run_manifest 缺省登记同源自证。

    终审 P1-1：version_config 在场（decide_for_task 入口创建的同一
    JudgeVersionConfig）时 prompt_version 从其同源读取（显式实参语义
    权威，env 不再二次读值分裂）；缺省维持原 env 分发口径。
    """
    return llm_residual.ResidualJudgeConfig(
        mv_mode=llm_residual.MV_AUDIT,
        prompt_version=(version_config.prompt_version
                        if version_config is not None
                        else judge_version_config.default_judge_version(
                            environ).prompt_version))


def build_judge_callable(
        *, judge: "llm_residual.SyncResidualJudge | None" = None,
        config: "llm_residual.ResidualJudgeConfig | None" = None,
        budget=None, environ: Mapping | None = None,
        version_config=None,
        ) -> Callable[[Mapping], dict] | None:
    """装配闸：DEDUP_JUDGE_PROOF 开 → 真件 judge_callable；关 → None。

    None=未注入（adjudicate_pair 默认实现 fail-closed 未决，绝不冒签）。
    judge 显式传入优先（测试罐装 LLM 边界）；否则由 config（缺省=
    default_judge_config——提交一修复并入复审条 3：prompt 随
    DEDUP_JUDGE_DECISION_MODE 分发，legacy 默认 judge_v1，不再无条件
    judge_v5）+budget 构造真 SyncResidualJudge。

    终审 P1-1（单源传递）：version_config 在场（decide_for_task 入口
    创建的同一 JudgeVersionConfig）时装配配置与每顺序证明（proof_
    for_order）均从其同源读取 prompt/policy——显式实参语义权威，
    adapter 层不再各自回落 env 读值；缺省维持原 env 分发口径。
    """
    if not judge_proof.judge_proof_enabled(environ):
        return None
    if judge is None:
        judge = llm_residual.SyncResidualJudge(
            config or default_judge_config(environ,
                                           version_config=version_config),
            budget=budget)

    def _judge_callable(pair_context: Mapping) -> dict:
        return proof_for_order(judge, pair_context,
                               version_config=version_config)

    return _judge_callable
