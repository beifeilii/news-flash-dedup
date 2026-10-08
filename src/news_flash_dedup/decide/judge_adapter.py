"""P1 联调真件适配器（2026-10-09，施工窗 P1-a，合流树 p1-integrated）。

定位：P1-a 证明组件（judge_proof.py 证明级核验 + llm_residual.py 双序判官）
→ P1-b 合同 `judge_callable(pair_context) -> VerifiedJudgeProof`（合同
`log/temp/p1-interface-contract-v1.md` §一 JSON 形态 dict）的**真件接线**。
录取线=judge_pair.validate_proof 全字段校验（不合规自动 fail-closed 未决）。

依据（冻结件）：合同 §一/§二/§三 + 判定宪章 policy_v2（机检语义口径）。
文件纪律：本模块只接线不改判定语义；judge_proof/llm_residual/judge_pair
三件套零改动。

合同映射表（真件内部形态 → 合同形态，逐项）：
- 粒度：合同=每顺序一份证明（adjudicate_pair 双序各调一次）；本适配器每次
  调用只跑一个顺序（"ab"→内部 hc、"ba"→内部 ch，build_pair_context 注记
  "ab 与 hc 同向"），对级装配（双序组合/ORDER_DISAGREE）由 P1-b 适配层执行。
- verdict：内部 重复→duplicate / 不重复→not_duplicate / 存疑→doubtful /
  结构非法→invalid / 调用失败→failure（合同五态）。
- 引文：内部 OrderVerification.citations（side/field/quote/start/end，
  Unicode 码点 offset）→ quotes_a/quotes_b[{text, offset_start, offset_end,
  role=内部 field 槽位 evidence|numbers|times}]；offset 精确回指本侧原文
  （绑定失败走 P_OFFSET→machine_verify.passed=false，见下）。
- 机侧分项结论三态（合同 numeric_check/time_check.conclusion，机侧独立
  抽取口径）：机检冲突→"不一致"；任一侧无机检材料（含双侧皆无）→
  "无法判定"（宪章缺失例外≠不一致，合同不拦）；双侧在场且无双向差异→
  "一致"。time_check.anchors_a/b=机抽归一日期锚列表。
- machine_verify={"mode": "gate", "rules_triggered": [P_*], "passed": bool}：
  mode 字面=合同 §一 gate（签发前机验闸由 P1-b 适配层执行）；本适配器内部
  核验姿态=audit（不自我降级存疑，触发如实入 rules_triggered，降级权归
  合同层）。passed=false 当且仅当存在 P_* 机器/绑定失败（P_OFFSET 含——
  签发判定引文绑定失败不得冒充通过）；P_NO_AXIS 不入 rules_triggered
  （证据充分性问题，走合同 §二 降级通道，见下）。
- 不重复证伪（合同 §二 falsification）：内部首条证伪轴（定序）→
  {dimension, evidence_a/evidence_b{text, offset_start, offset_end},
  relation=内部 correspondence}。dimension 映射（合同四维 ⊆ 内部五轴）：
  subject→subject、numeric→numeric、stage→stage、**time→stage**（宪章
  §二-3 时间/阶段同族）、**polarity→event**（宪章 §二-5 方向冲突=事件族
  差异；合同无 direction 维，最近邻映射，已呈主窗口备案）。无机检轴
  （P_NO_AXIS）→ 不携 falsification、passed=true——合同 §二 空口
  not_duplicate 由 validate_proof 降级 doubtful（未决同义），与内部
  "证据不足→未决"同义。
- 版本四维：model_version=cfg.model、prompt_sha256=judge_prompt_for_version
  的 sha、policy_version=judge_proof.PROOF_POLICY_VERSION（="policy_v2"
  宪章版本）、judged_at=UTC ISO（合同必填；内部审计负载的确定性纪律不
  及此合同面——judged_at 只进合同证明件，不进 llm_residual 审计槽）。
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

from . import judge_pair, judge_proof, llm_residual, machine_verify as _mv

__all__ = ["build_judge_callable", "proof_for_order"]

# 内部轴 → 合同 dimension（见模块 docstring 映射表；time/polarity 最近邻）
_DIMENSION_MAP = {
    "subject": "subject", "numeric": "numeric", "stage": "stage",
    "time": "stage", "polarity": "event",
}

# 合同 order → 内部顺序（"ab" a=history 先=hc 同向）
_ORDER_TO_INTERNAL = {"ab": "hc", "ba": "ch"}

# 内部预算尽两枚字面错误前缀（llm_residual._call_cached 预算环抛出，
# _judge_order 收容为 status="failure"、error=str(exc)[:300]——前缀稳定）
_BUDGET_ERROR_PREFIXES = ("预算软边界", "LLM 调用预算拒付")


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
                    ctx: Mapping) -> dict:
    """单顺序真件证明：跑内部证明级核验， emit 合同 §一 形态 dict。

    ctx=judge_pair._order_context 产物（含 order/item ids/text_a/text_b）。
    预算尽 → JudgeBudgetExceeded（合同 BUDGET_EXCEEDED 异常通道）。
    """
    order = ctx["order"]
    internal_order = _ORDER_TO_INTERNAL[order]      # "ab"→"hc"、"ba"→"ch"
    pair_id = ctx["pair_id"]
    text_a, text_b = ctx["text_a"], ctx["text_b"]
    cfg = judge.config
    prompt_sha = llm_residual.judge_prompt_for_version(cfg.prompt_version)[1]
    policy = judge_proof.PROOF_POLICY_VERSION
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
    numeric = {"conclusion": "无法判定", "details": []}
    time_chk = {"conclusion": "无法判定", "anchors_a": [], "anchors_b": []}
    falsification: dict | None = None
    if verification is not None:
        for span in verification.citations:
            quote = {"text": span.quote, "offset_start": span.start,
                     "offset_end": span.end, "role": span.field}
            (quotes_a if span.side == "A" else quotes_b).append(quote)
        # P_NO_AXIS=证据充分性（合同 §二 降级通道），不入机验闸；
        # 其余 P_*（含 P_OFFSET 绑定失败）全入 rules_triggered。
        failures = [f for f in verification.failures
                    if f != judge_proof.P_NO_AXIS]
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
            falsification = {
                "dimension": _DIMENSION_MAP[ax["axis"]],
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
        "quotes_a": quotes_a,
        "quotes_b": quotes_b,
        "numeric_check": numeric,
        "time_check": time_chk,
        "machine_verify": {"mode": "gate",
                           "rules_triggered": failures,
                           "passed": not failures},
        "model_version": cfg.model,
        "prompt_sha256": prompt_sha,
        "policy_version": policy,
        "judged_at": datetime.now(timezone.utc).isoformat(),
    }
    if falsification is not None:
        proof["falsification"] = falsification
    proof["cache_key"] = judge_pair.compute_cache_key(
        cfg.model, prompt_sha, policy, order, sha_a, sha_b)
    return proof


def build_judge_callable(
        *, judge: "llm_residual.SyncResidualJudge | None" = None,
        config: "llm_residual.ResidualJudgeConfig | None" = None,
        budget=None, environ: Mapping | None = None
        ) -> Callable[[Mapping], dict] | None:
    """装配闸：DEDUP_JUDGE_PROOF 开 → 真件 judge_callable；关 → None。

    None=未注入（adjudicate_pair 默认实现 fail-closed 未决，绝不冒签）。
    judge 显式传入优先（测试罐装 LLM 边界）；否则由 config（默认
    ResidualJudgeConfig，mv_mode=audit——核验姿态不自我降级，降级权归
    合同层，见模块 docstring）+budget 构造真 SyncResidualJudge。
    """
    if not judge_proof.judge_proof_enabled(environ):
        return None
    if judge is None:
        cfg = config or llm_residual.ResidualJudgeConfig(
            mv_mode=llm_residual.MV_AUDIT)
        judge = llm_residual.SyncResidualJudge(cfg, budget=budget)

    def _judge_callable(pair_context: Mapping) -> dict:
        return proof_for_order(judge, pair_context)

    return _judge_callable
