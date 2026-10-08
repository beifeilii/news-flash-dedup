"""P1-b 判官适配层（2026-10-09，施工窗 P1-b，分支 p1b-judge-in-chain）。

依据（冻结件，禁止私改）：
- 接口合同：`log/temp/p1-interface-contract-v1.md`（2026-10-09 主窗口钉，
  §一 VerifiedJudgeProof 字段、§二 not_duplicate 证伪合同、§三 失败/未决
  枚举、§四 对级结论映射）；
- 工程正文：`log/判定链改造最终方案-Codex-20261008.md` §3.1 语义判官主路、
  §3.2 集合级三态（"对未决对补判……唯一最终聚合"）。

定位：decide/service.py 判定链与 P1-a 证明组件之间的**消费侧适配层**。
接口 = 注入的 `judge_callable(pair_context) -> VerifiedJudgeProof`
（合同 §一 的 JSON 形态 dict）。P1-a 真件合流时零改动替换注入件；
未注入（judge_callable=None）即 fail-closed 未决（默认实现，绝不冒签）。

纪律：
- 超时/异常/证明不合规一律 fail-closed → 对级 unresolved（合同 §三映射）；
- 无 falsification 的 not_duplicate 按合同 §二降级为 doubtful（未决同义），
  不得入"有效排除"；
- 对级产出只取三态：equivalent / conflict / unresolved（合同 §四）；
- detail 文案只携合同 §三枚举与适配层本地枚举，绝不嵌入
  EQUIVALENT/CONFLICT/UNRESOLVED 白名单码（aggregate reason 扫描闸在案）。
"""

from __future__ import annotations

import hashlib
import os
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Callable

from news_flash_dedup.compare.pair_compare import VerifiedConflict
from news_flash_dedup.compare.value_time import EvidenceRef


# ---------------------------------------------------------------- 开关

# P1-b 主开关（2026-10-09）：默认关=老行为逐字节；开=判官进主链+提交前
# 唯一最终聚合。decide/service.py decide_for_task 在 judge_in_chain=None
# 时经本函数读 env。
JUDGE_IN_CHAIN_ENV = "DEDUP_JUDGE_IN_CHAIN"
_SWITCH_ON_VALUES = frozenset({"1", "true", "yes", "on"})


def judge_in_chain_enabled(environ: Mapping | None = None) -> bool:
    """读 DEDUP_JUDGE_IN_CHAIN；缺省/非真值 → False（默认关，老行为）。"""
    env = os.environ if environ is None else environ
    return env.get(JUDGE_IN_CHAIN_ENV, "").strip().lower() in _SWITCH_ON_VALUES


# ---------------------------------------------------------------- 合同枚举（§一/§二/§三）

ORDERS = ("ab", "ba")
PROOF_VERDICTS = ("duplicate", "not_duplicate", "doubtful", "failure", "invalid")
# 合同 v2（D7 裁定 2026-10-09，主窗口 D1-D10 逐条）：证伪词表=实现五族
# （subject/numeric/time/stage/polarity，宪章 §二 机检可判族）——"event"
# 无机检判据不冒充，合同 v2 删除；与 judge_adapter 原生维发射同步。
FALSIFICATION_DIMENSIONS = ("subject", "numeric", "time", "stage", "polarity")
CHECK_CONCLUSIONS = ("一致", "不一致", "无法判定")

# 合同 §三 失败/未决枚举（P1-b 一律映射为"未决"进人工，禁止静默豁免）
MV_REJECTED = "MV_REJECTED"
ORDER_DISAGREE = "ORDER_DISAGREE"
INVALID_OUTPUT = "INVALID_OUTPUT"
TIMEOUT = "TIMEOUT"
BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
EVIDENCE_UNBOUND = "EVIDENCE_UNBOUND"
CONCLUSION_CONTRADICTS = "CONCLUSION_CONTRADICTS"

# 适配层本地枚举（合同 §三之外的兜底通道，同属"未决进人工"）
JUDGE_NOT_INJECTED = "JUDGE_NOT_INJECTED"   # 默认实现：未注入 judge_callable
JUDGE_EXCEPTION = "JUDGE_EXCEPTION"         # 判官调用抛异常（fail-closed）
JUDGE_FAILURE = "JUDGE_FAILURE"             # 证明 verdict="failure"
JUDGE_DOUBTFUL = "JUDGE_DOUBTFUL"           # 双序存疑/混合（非分歧）

# 合同 §三/本地枚举 → 对级 unresolved 码（pair_compare UNRESOLVED_CODES
# 白名单成员；码面不动，语义最近邻映射，真值由 detail 携合同枚举承载）
_FAILURE_TO_CODE = {
    BUDGET_EXCEEDED: "CANDIDATE_BUDGET_EXHAUSTED",
    TIMEOUT: "DEPENDENCY_TIMEOUT",
    MV_REJECTED: "EVIDENCE_INVALID",
    EVIDENCE_UNBOUND: "EVIDENCE_INVALID",
    INVALID_OUTPUT: "EVIDENCE_INVALID",
    CONCLUSION_CONTRADICTS: "NUMERIC_ALIGNMENT_FAILED",
    ORDER_DISAGREE: "SUBJECT_UNRESOLVED",
    JUDGE_NOT_INJECTED: "SUBJECT_UNRESOLVED",
    JUDGE_EXCEPTION: "SUBJECT_UNRESOLVED",
    JUDGE_FAILURE: "SUBJECT_UNRESOLVED",
    JUDGE_DOUBTFUL: "SUBJECT_UNRESOLVED",
}

# 未决归因确定性优先序（硬失败先于双序分歧先于存疑；同序多因取首见）
_HARD_FAILURE_PRIORITY = (
    BUDGET_EXCEEDED,
    TIMEOUT,
    MV_REJECTED,
    CONCLUSION_CONTRADICTS,
    EVIDENCE_UNBOUND,
    INVALID_OUTPUT,
    JUDGE_EXCEPTION,
    JUDGE_FAILURE,
)

DEFAULT_JUDGE_TIMEOUT_S = 120.0   # 单顺序判官调用硬超时（注入件自带超时之外的本层兜底）


class JudgeBudgetExceeded(RuntimeError):
    """判官侧预算耗尽信号（异常通道）——适配层映射 BUDGET_EXCEEDED 未决。

    注入件（含 P1-a 真件）预算尽时抛本异常；其他异常一律 JUDGE_EXCEPTION。
    """


class JudgeProofError(ValueError):
    """VerifiedJudgeProof 合同合规校验失败（detail 首词=失败枚举码）。"""


# ---------------------------------------------------------------- 结果形态

@dataclass(frozen=True)
class JudgePairOutcome:
    """判官适配层对级产出（合同 §四映射后）。"""
    outcome: str                      # "equivalent" | "conflict" | "unresolved"
    code: str                         # PairResult 码（对应白名单成员）
    detail: str                       # 人读说明（携合同枚举，不携白名单码）
    failure_reason: str | None = None  # 合同 §三/本地枚举；签发态 None
    used_evidence: tuple = ()         # EvidenceRef（签发态实填，未决空）
    verified_conflicts: tuple = ()    # VerifiedConflict（conflict 实填）
    proofs: Mapping = field(default_factory=dict)  # {"ab": proof|None, "ba": proof|None} 审计留存


# ---------------------------------------------------------------- 证明构造辅助（缓存键=合同 §一公式）

def compute_cache_key(model_version: str, prompt_sha256: str,
                      policy_version: str, order: str,
                      text_a_sha256: str, text_b_sha256: str) -> str:
    """cache_key = sha256(model|prompt_sha|policy|order|text_a_sha|text_b_sha)
    （合同 §一 逐字公式）。"""
    parts = [model_version, prompt_sha256, policy_version, order,
             text_a_sha256, text_b_sha256]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- pair_context 构造

def build_pair_context(pair, *, history_text: str, current_text: str) -> dict:
    """由 PairResult + 双侧正文构造判官注入上下文（纯 JSON 数据）。

    order 维度由 adjudicate_pair 按 "ab"（a=history 先）/ "ba"（a=current 先）
    双序各自展开（_order_context）。"ab" 与 llm_residual 的 hc 同向。
    """
    return {
        "pair_id": pair.pair_id,
        "history_record_id": pair.history_record_id,
        "current_record_id": pair.current_record_id,
        "history_item_id": pair.history_item_id,
        "current_item_id": pair.current_item_id,
        "history_text": history_text,
        "current_text": current_text,
        "history_text_sha256": _sha256(history_text),
        "current_text_sha256": _sha256(current_text),
    }


def _order_context(pair_context: Mapping, order: str) -> dict:
    """按判定序展开 a/b 双侧："ab" a=history、b=current；"ba" 互换。"""
    if order == "ab":
        a_side, b_side = "history", "current"
    elif order == "ba":
        a_side, b_side = "current", "history"
    else:
        raise ValueError(f"order 非法：{order!r}（合法：{ORDERS}）")
    return {
        **pair_context,
        "order": order,
        "item_a_id": pair_context[f"{a_side}_item_id"],
        "item_b_id": pair_context[f"{b_side}_item_id"],
        "record_a_id": pair_context[f"{a_side}_record_id"],
        "record_b_id": pair_context[f"{b_side}_record_id"],
        "text_a": pair_context[f"{a_side}_text"],
        "text_b": pair_context[f"{b_side}_text"],
        "text_a_sha256": pair_context[f"{a_side}_text_sha256"],
        "text_b_sha256": pair_context[f"{b_side}_text_sha256"],
    }


# ---------------------------------------------------------------- 调用（超时/异常 fail-closed）

def _call_with_timeout(fn: Callable, arg: Mapping, timeout_s: float):
    """daemon 线程包裹同步判官调用。返 (proof, failure_reason)。

    超时 → (None, TIMEOUT)（结果丢弃，线程 daemon 不拖进程）；
    JudgeBudgetExceeded → (None, BUDGET_EXCEEDED)；其余异常 → (None, JUDGE_EXCEPTION)。
    """
    box: dict = {}

    def _runner() -> None:
        try:
            box["value"] = fn(arg)
        except BaseException as exc:  # noqa: BLE001 — fail-closed 收容一切
            box["error"] = exc

    worker = threading.Thread(target=_runner, daemon=True)
    worker.start()
    worker.join(timeout_s)
    if worker.is_alive():
        return None, TIMEOUT
    if "error" in box:
        if isinstance(box["error"], JudgeBudgetExceeded):
            return None, BUDGET_EXCEEDED
        return None, JUDGE_EXCEPTION
    return box.get("value"), None


# ---------------------------------------------------------------- 证明校验（合同 §一/§二）

_PROOF_REQUIRED_FIELDS = (
    "pair_id", "item_a_id", "item_b_id",
    "text_a_sha256", "text_b_sha256",
    "order", "verdict",
    "quotes_a", "quotes_b",
    "numeric_check", "time_check", "machine_verify",
    "model_version", "prompt_sha256", "policy_version",
    "judged_at", "cache_key",
)


def _fail(reason: str, detail: str) -> JudgeProofError:
    return JudgeProofError(f"{reason}: {detail}")


def _is_hex64(value: Any) -> bool:
    return (isinstance(value, str) and len(value) == 64
            and all(c in "0123456789abcdef" for c in value))


def _check_quote_binding(quotes: Any, bound_text: str, side: str) -> list:
    """quotes=[{text, offset_start, offset_end, role}] 逐条校验并回指本侧原文
    （合同 §一：offset 必须能回指原文，否则 verdict 不得为 duplicate/not_duplicate）。
    返归一化 quote dict 列表；任何不合规 → JudgeProofError。"""
    if not isinstance(quotes, list) or not quotes:
        raise _fail(EVIDENCE_UNBOUND, f"{side} 引文缺失或为空（签发判定须带引文）")
    normalized = []
    for index, quote in enumerate(quotes):
        if not isinstance(quote, Mapping):
            raise _fail(EVIDENCE_UNBOUND, f"{side} 引文[{index}]非对象")
        for key in ("text", "offset_start", "offset_end", "role"):
            if key not in quote:
                raise _fail(INVALID_OUTPUT, f"{side} 引文[{index}]缺键 {key!r}")
        text = quote["text"]
        start, end = quote["offset_start"], quote["offset_end"]
        if not isinstance(text, str) or not text:
            raise _fail(INVALID_OUTPUT, f"{side} 引文[{index}] text 非非空字符串")
        if not isinstance(quote["role"], str) or not quote["role"]:
            raise _fail(INVALID_OUTPUT, f"{side} 引文[{index}] role 非非空字符串")
        if (type(start) is not int or type(end) is not int
                or not (0 <= start < end <= len(bound_text))):
            raise _fail(EVIDENCE_UNBOUND,
                        f"{side} 引文[{index}] offset 越界（[{start},{end}) vs "
                        f"原文长 {len(bound_text)}）")
        if bound_text[start:end] != text:
            raise _fail(EVIDENCE_UNBOUND,
                        f"{side} 引文[{index}] offset 回指失败："
                        f"原文切片 {bound_text[start:end]!r} != 引文 {text!r}")
        normalized.append({"text": text, "offset_start": start,
                           "offset_end": end, "role": quote["role"]})
    return normalized


def _check_span_binding(span: Any, bound_text: str, side: str) -> dict:
    """falsification evidence 单跨 {text, offset_start, offset_end} 绑定校验。"""
    if not isinstance(span, Mapping):
        raise _fail(EVIDENCE_UNBOUND, f"{side} 证伪证据非对象")
    for key in ("text", "offset_start", "offset_end"):
        if key not in span:
            raise _fail(INVALID_OUTPUT, f"{side} 证伪证据缺键 {key!r}")
    text, start, end = span["text"], span["offset_start"], span["offset_end"]
    if not isinstance(text, str) or not text:
        raise _fail(INVALID_OUTPUT, f"{side} 证伪证据 text 非非空字符串")
    if (type(start) is not int or type(end) is not int
            or not (0 <= start < end <= len(bound_text))
            or bound_text[start:end] != text):
        raise _fail(EVIDENCE_UNBOUND, f"{side} 证伪证据 offset 回指失败")
    return {"text": text, "offset_start": start, "offset_end": end}


def validate_proof(proof: Any, ctx: Mapping) -> dict:
    """VerifiedJudgeProof 合同合规校验（合同 §一/§二/§三）。

    返 {"verdict": 生效 verdict, "falsification": dict|None,
        "quotes_a": [...], "quotes_b": [...]}；
    不合规一律 JudgeProofError（首词=合同 §三枚举；适配层 catch 后映射未决）。
    无/不合格 falsification 的 not_duplicate 不抛错——按合同 §二降级
    doubtful 由调用侧处理（本函数仅对结构硬失败与证据绑定负责）。
    """
    if not isinstance(proof, Mapping):
        raise _fail(INVALID_OUTPUT, f"证明非 Mapping：{type(proof).__name__}")
    missing = [key for key in _PROOF_REQUIRED_FIELDS if key not in proof]
    if missing:
        raise _fail(INVALID_OUTPUT, f"证明缺必填字段：{missing}")
    # 身份/绑定四维对拍（证明必须属于本次请求的这一对、这一序、这两份正文）
    if proof["pair_id"] != ctx["pair_id"]:
        raise _fail(INVALID_OUTPUT,
                    f"pair_id 对拍失败：{proof['pair_id']!r} != {ctx['pair_id']!r}")
    if proof["order"] not in ORDERS or proof["order"] != ctx["order"]:
        raise _fail(INVALID_OUTPUT,
                    f"order 对拍失败：{proof['order']!r} != {ctx['order']!r}")
    if proof["item_a_id"] != ctx["item_a_id"] or proof["item_b_id"] != ctx["item_b_id"]:
        raise _fail(INVALID_OUTPUT, "item_a_id/item_b_id 与判定序上下文不符")
    if not (_is_hex64(proof["text_a_sha256"]) and _is_hex64(proof["text_b_sha256"])):
        raise _fail(INVALID_OUTPUT, "text_a_sha256/text_b_sha256 非 64 hex")
    if proof["text_a_sha256"] != _sha256(ctx["text_a"]):
        raise _fail(INVALID_OUTPUT, "text_a_sha256 与上下文正文不符（换文复用拒绝）")
    if proof["text_b_sha256"] != _sha256(ctx["text_b"]):
        raise _fail(INVALID_OUTPUT, "text_b_sha256 与上下文正文不符（换文复用拒绝）")
    verdict = proof["verdict"]
    if verdict not in PROOF_VERDICTS:
        raise _fail(INVALID_OUTPUT, f"verdict 非法：{verdict!r}")
    for key in ("model_version", "prompt_sha256", "policy_version", "judged_at"):
        if not isinstance(proof[key], str) or not proof[key]:
            raise _fail(INVALID_OUTPUT, f"{key} 缺失或非非空字符串")
    expected_key = compute_cache_key(
        proof["model_version"], proof["prompt_sha256"], proof["policy_version"],
        proof["order"], proof["text_a_sha256"], proof["text_b_sha256"])
    if proof["cache_key"] != expected_key:
        raise _fail(INVALID_OUTPUT, "cache_key 与合同 §一 公式重算不符")
    # 机侧独立核验段（数值/时间结论三值闭合）
    for check_key in ("numeric_check", "time_check"):
        check = proof[check_key]
        if not isinstance(check, Mapping):
            raise _fail(INVALID_OUTPUT, f"{check_key} 非对象")
        if check.get("conclusion") not in CHECK_CONCLUSIONS:
            raise _fail(INVALID_OUTPUT,
                        f"{check_key}.conclusion 非法：{check.get('conclusion')!r}")
    # 机器验 gate 段（合同 §一 mode="gate" + passed bool）
    mv = proof["machine_verify"]
    if not isinstance(mv, Mapping):
        raise _fail(INVALID_OUTPUT, "machine_verify 非对象")
    if mv.get("mode") != "gate":
        raise _fail(INVALID_OUTPUT,
                    f"machine_verify.mode 非 gate：{mv.get('mode')!r}")
    if type(mv.get("passed")) is not bool:
        raise _fail(INVALID_OUTPUT, "machine_verify.passed 非 bool")

    if verdict in ("doubtful", "failure", "invalid"):
        return {"verdict": verdict, "falsification": None,
                "quotes_a": [], "quotes_b": []}

    # 签发判定（duplicate/not_duplicate）：引文必须回指本侧原文（合同 §一）
    quotes_a = _check_quote_binding(proof["quotes_a"], ctx["text_a"], "a 侧")
    quotes_b = _check_quote_binding(proof["quotes_b"], ctx["text_b"], "b 侧")
    # 机验 gate 拦截（合同 §四：签发须 machine_verify.passed=true）
    if mv["passed"] is not True:
        raise _fail(MV_REJECTED,
                    f"machine_verify.passed=false（触发 {mv.get('rules_triggered')!r}）")
    # 判定与机侧分项结论对拍（合同 §三 CONCLUSION_CONTRADICTS）
    if verdict == "duplicate":
        contradicted = [
            key for key in ("numeric_check", "time_check")
            if proof[key]["conclusion"] == "不一致"]
        if contradicted:
            raise _fail(CONCLUSION_CONTRADICTS,
                        f"verdict=duplicate 与 {contradicted} 结论'不一致'对拍失败")
    # not_duplicate 证伪合同（合同 §二）：结构缺失/维度非法 → 降级 doubtful
    # （未决同义，本函数返 downgrade 标记）；证据 offset 回指失败 → 硬失败。
    falsification = None
    downgrade_not_duplicate = False
    if verdict == "not_duplicate":
        fals = proof.get("falsification")
        if (not isinstance(fals, Mapping)
                or fals.get("dimension") not in FALSIFICATION_DIMENSIONS
                or not isinstance(fals.get("relation"), str)
                or not fals["relation"].strip()):
            downgrade_not_duplicate = True   # 合同 §二：空口 not_duplicate=未决
        else:
            falsification = {
                "dimension": fals["dimension"],
                "evidence_a": _check_span_binding(
                    fals.get("evidence_a"), ctx["text_a"], "a 侧"),
                "evidence_b": _check_span_binding(
                    fals.get("evidence_b"), ctx["text_b"], "b 侧"),
                "relation": fals["relation"].strip(),
            }
    return {"verdict": verdict, "falsification": falsification,
            "downgrade_not_duplicate": downgrade_not_duplicate,
            "quotes_a": quotes_a, "quotes_b": quotes_b}


# ---------------------------------------------------------------- 对级映射（合同 §四）

def _quotes_to_evidence(quotes: list, record_id: str) -> tuple:
    """归一化引文 → EvidenceRef 元组（审计/聚合消费面=EvidenceRef 属性协议）。"""
    return tuple(EvidenceRef(record_id=record_id, field="text",
                             quote=q["text"], start=q["offset_start"],
                             end=q["offset_end"]) for q in quotes)


def _unresolved(reason: str, proofs: Mapping) -> JudgePairOutcome:
    code = _FAILURE_TO_CODE[reason]
    return JudgePairOutcome(
        outcome="unresolved", code=code,
        detail=f"判官未决进人工（{reason}）。",
        failure_reason=reason, proofs=proofs)


def adjudicate_pair(judge_callable: Callable | None, pair_context: Mapping, *,
                    timeout_s: float | None = None) -> JudgePairOutcome:
    """对一对未决候选跑双序判官并按合同 §四映射对级结论。

    - judge_callable=None → 默认实现 fail-closed 未决（JUDGE_NOT_INJECTED）；
    - 双序均 duplicate 且机验 gate 通过 → equivalent（FACT_EQUIVALENT）；
    - 双序均 not_duplicate 且各带合格 falsification 且机验通过
      → conflict（VERIFIED_CONFLICT）；
    - 其余一切组合（含超时/异常/机验拦截/引文回指失败/结论对拍失败/双序
      分歧/存疑）→ unresolved（合同 §三映射 UNRESOLVED 白名单码）。
    """
    timeout_s = DEFAULT_JUDGE_TIMEOUT_S if timeout_s is None else timeout_s
    if judge_callable is None:
        return _unresolved(JUDGE_NOT_INJECTED, proofs={"ab": None, "ba": None})

    per_order: dict[str, dict] = {}
    raw_proofs: dict[str, Any] = {"ab": None, "ba": None}
    hard_failures: list[str] = []
    for order in ORDERS:
        ctx = _order_context(pair_context, order)
        proof, failure = _call_with_timeout(judge_callable, ctx, timeout_s)
        raw_proofs[order] = proof
        if failure is not None:
            hard_failures.append(failure)
            per_order[order] = {"verdict": None, "falsification": None}
            continue
        try:
            validated = validate_proof(proof, ctx)
        except JudgeProofError as exc:
            reason = str(exc).split(":", 1)[0]
            hard_failures.append(reason if reason in _FAILURE_TO_CODE
                                 else INVALID_OUTPUT)
            per_order[order] = {"verdict": None, "falsification": None}
            continue
        verdict = validated["verdict"]
        if verdict == "failure":
            hard_failures.append(JUDGE_FAILURE)
            verdict = None
        elif verdict == "invalid":
            hard_failures.append(INVALID_OUTPUT)
            verdict = None
        elif verdict == "not_duplicate" and validated.get(
                "downgrade_not_duplicate"):
            verdict = "doubtful"   # 合同 §二：空口 not_duplicate=未决同义
        per_order[order] = {"verdict": verdict,
                            "falsification": validated["falsification"],
                            "quotes_a": validated["quotes_a"],
                            "quotes_b": validated["quotes_b"]}

    verdicts = {order: per_order[order]["verdict"] for order in ORDERS}

    # 合同 §四 分支 1：双序 duplicate + 机验 gate 通过 → equivalent
    if verdicts["ab"] == "duplicate" and verdicts["ba"] == "duplicate":
        evidence = []
        for order in ORDERS:
            ctx = _order_context(pair_context, order)
            evidence.extend(_quotes_to_evidence(
                per_order[order]["quotes_a"], ctx["record_a_id"]))
            evidence.extend(_quotes_to_evidence(
                per_order[order]["quotes_b"], ctx["record_b_id"]))
        return JudgePairOutcome(
            outcome="equivalent", code="FACT_EQUIVALENT",
            detail="判官双序一致判定同一事实，引文已绑定双侧原文且机器验 gate 通过。",
            used_evidence=tuple(evidence), proofs=raw_proofs)

    # 合同 §四 分支 2：双序 not_duplicate + 合格 falsification + 机验 → conflict
    if verdicts["ab"] == "not_duplicate" and verdicts["ba"] == "not_duplicate":
        fals_ab = per_order["ab"]["falsification"]   # ab 序 a=history、b=current
        ctx_ab = _order_context(pair_context, "ab")
        conflict = VerifiedConflict(
            field_path=f"judge_falsification.{fals_ab['dimension']}",
            basis="JUDGE_FALSIFICATION",
            history_evidence=EvidenceRef(
                record_id=ctx_ab["record_a_id"], field="text",
                quote=fals_ab["evidence_a"]["text"],
                start=fals_ab["evidence_a"]["offset_start"],
                end=fals_ab["evidence_a"]["offset_end"]),
            current_evidence=EvidenceRef(
                record_id=ctx_ab["record_b_id"], field="text",
                quote=fals_ab["evidence_b"]["text"],
                start=fals_ab["evidence_b"]["offset_start"],
                end=fals_ab["evidence_b"]["offset_end"]),
            detail=fals_ab["relation"])
        return JudgePairOutcome(
            outcome="conflict", code="VERIFIED_CONFLICT",
            detail=f"判官双序一致证伪同一事实（维度 {fals_ab['dimension']}），"
                   "双侧证伪引文已绑定原文且机器验 gate 通过。",
            used_evidence=(conflict.history_evidence, conflict.current_evidence),
            verified_conflicts=(conflict,), proofs=raw_proofs)

    # 合同 §四 分支 3：其余一切组合 → unresolved（确定性归因）
    for reason in _HARD_FAILURE_PRIORITY:
        if reason in hard_failures:
            return _unresolved(reason, proofs=raw_proofs)
    if {v for v in verdicts.values() if v} == {"duplicate", "not_duplicate"}:
        return _unresolved(ORDER_DISAGREE, proofs=raw_proofs)
    return _unresolved(JUDGE_DOUBTFUL, proofs=raw_proofs)


__all__ = [
    "JUDGE_IN_CHAIN_ENV", "judge_in_chain_enabled",
    "ORDERS", "PROOF_VERDICTS", "FALSIFICATION_DIMENSIONS", "CHECK_CONCLUSIONS",
    "MV_REJECTED", "ORDER_DISAGREE", "INVALID_OUTPUT", "TIMEOUT",
    "BUDGET_EXCEEDED", "EVIDENCE_UNBOUND", "CONCLUSION_CONTRADICTS",
    "JUDGE_NOT_INJECTED", "JUDGE_EXCEPTION", "JUDGE_FAILURE", "JUDGE_DOUBTFUL",
    "DEFAULT_JUDGE_TIMEOUT_S",
    "JudgeBudgetExceeded", "JudgeProofError", "JudgePairOutcome",
    "compute_cache_key", "build_pair_context", "validate_proof",
    "adjudicate_pair",
]
