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

提交一（2026-10-10，分支 p3-semantic-authority，方案
`log/快讯去重_判官与证明层改造_可执行技术方案.md` §4/§5.1）：**解除证明层
一票否决**——判定与证据分层（§4.1）：
- 合同级硬校验保留（身份绑定/正文 SHA/order/cache key/verdict 枚举/
  版本四维/输出结构），不合规仍 fail-closed 未决；
- 证据级检查（引文绑定/offset 回指/machine_verify.passed/结论对拍/
  falsification 在场性）全部降为**诊断**（ValidatedJudgeOrder.
  evidence_warnings/machine_findings），不再抛错、不再单独改判；
- 双序合并按 §4.2 矩阵：双序 duplicate→JUDGE_EQUIVALENT；双序
  not_duplicate→JUDGE_NON_DUPLICATE（不再强制机器证伪轴）；分歧/存疑/
  失败→未决（JUDGE_UNCERTAIN 或合同 §三 映射码）；
- 某一侧无可绑定引文时构造"完整原文 span"审计回退证据
  （field="text"、quote=该侧完整正文、start=0、end=len），并记
  EVIDENCE_FALLBACK_FULL_TEXT 告警——该回退只证明"判定对应的原文版本"，
  不冒充精确字段证明；只进内部审计/诊断，绝不进公共五字段。

提交三（2026-10-10，同方案 §5.3）：**灰度开关**——
DEDUP_JUDGE_DECISION_MODE=legacy_proof_gate|semantic_authority，默认
legacy_proof_gate（阶段一）：生效结论回到提交一前证明闸口径，同一份
判官结果离线并行计算 semantic_authority 结论记入 outcome.comparison
（不增加 LLM 调用，新旧差异由 service 计 judge.legacy_vs_new.changed）；
金标回放稳定后切 semantic_authority；旧口径至少保留一个发布周期。

纪律：
- 超时/异常/合同不合规一律 fail-closed → 对级 unresolved（合同 §三映射）；
- 对级产出只取三态：equivalent / conflict / unresolved（合同 §四）；
- detail 文案只携合同 §三枚举与适配层本地枚举，绝不嵌入
  EQUIVALENT/CONFLICT/UNRESOLVED 白名单码（含新三码 JUDGE_EQUIVALENT/
  JUDGE_NON_DUPLICATE/JUDGE_UNCERTAIN——aggregate reason 扫描闸在案）；
- evidence_warnings/machine_findings 只进内部诊断（本层 outcome 字段 +
  service 层日志），绝不进公共五字段。
"""

from __future__ import annotations

import hashlib
import os
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
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


# 提交三灰度开关（§5.3）：DEDUP_JUDGE_DECISION_MODE=
# legacy_proof_gate|semantic_authority。
# - semantic_authority：判官语义结论直签（提交一口径，§4.2 矩阵）；
# - legacy_proof_gate（**默认**，阶段一）：生效结论回到提交一前证明闸
#   口径（证据诊断重升否决项：无可绑定引文→EVIDENCE_UNBOUND、
#   machine_verify.passed=false→MV_REJECTED、结论对拍冲突→
#   CONCLUSION_CONTRADICTS、not_duplicate 无证伪轴→降级 doubtful），
#   同一份判官结果**离线**并行计算 semantic_authority 结论记入
#   outcome.comparison（新旧差异由 service 计入
#   judge.legacy_vs_new.changed）——不增加任何 LLM 调用；金标回放
#   稳定后切 semantic_authority；旧口径至少保留一个发布周期。
JUDGE_DECISION_MODE_ENV = "DEDUP_JUDGE_DECISION_MODE"
MODE_LEGACY_PROOF_GATE = "legacy_proof_gate"
MODE_SEMANTIC_AUTHORITY = "semantic_authority"
_JUDGE_DECISION_MODES = (MODE_LEGACY_PROOF_GATE, MODE_SEMANTIC_AUTHORITY)
DEFAULT_JUDGE_DECISION_MODE = MODE_LEGACY_PROOF_GATE


def judge_decision_mode(environ: Mapping[str, str] | None = None) -> str:
    """读 DEDUP_JUDGE_DECISION_MODE：合法值原样；缺席/空串 → 默认
    legacy_proof_gate（fail-closed 回旧口径，§5.3 阶段一）；**非法非空值
    → ValueError 明确报错，不得静默回退**（2026-10-10 整改令二：非法值
    启动时明确报错）。"""
    raw = ((os.environ if environ is None else environ)
           .get(JUDGE_DECISION_MODE_ENV) or "").strip()
    if not raw:
        return DEFAULT_JUDGE_DECISION_MODE
    if raw not in _JUDGE_DECISION_MODES:
        raise ValueError(
            f"{JUDGE_DECISION_MODE_ENV}={raw!r} 非法：只允许 "
            f"{'|'.join(_JUDGE_DECISION_MODES)}（缺席=默认 "
            f"{DEFAULT_JUDGE_DECISION_MODE}）")
    return raw


# ---------------------------------------------------------------- 合同枚举（§一/§二/§三）

ORDERS = ("ab", "ba")
PROOF_VERDICTS = ("duplicate", "not_duplicate", "doubtful", "failure", "invalid")
# 合同 v2（D7 裁定 2026-10-09，主窗口 D1-D10 逐条）：证伪词表=实现五族
# （subject/numeric/time/stage/polarity，宪章 §二 机检可判族）——"event"
# 无机检判据不冒充，合同 v2 删除；与 judge_adapter 原生维发射同步。
FALSIFICATION_DIMENSIONS = ("subject", "numeric", "time", "stage", "polarity")
CHECK_CONCLUSIONS = ("一致", "不一致", "无法判定")

# 对级内部结果码（提交一 §5.1 修改点 2，语义权威）——判官语义结论不再
# 伪装成机器已验证结论（不再占用 FACT_EQUIVALENT/VERIFIED_CONFLICT）：
JUDGE_EQUIVALENT = "JUDGE_EQUIVALENT"        # 双序判官一致判重复
JUDGE_NON_DUPLICATE = "JUDGE_NON_DUPLICATE"  # 双序判官一致判不重复
JUDGE_UNCERTAIN = "JUDGE_UNCERTAIN"          # 双序存疑或分歧

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

# 证据诊断告警 token（提交一：只进内部诊断，绝不改判、绝不进公共五字段）
EVIDENCE_QUOTES_EMPTY = "EVIDENCE_QUOTES_EMPTY"            # 签发判定引文为空
EVIDENCE_FALLBACK_FULL_TEXT = "EVIDENCE_FALLBACK_FULL_TEXT"  # 完整原文回退证据
MACHINE_VERIFY_REJECTED = "MACHINE_VERIFY_REJECTED"        # machine_verify.passed=false
# 与 judge_proof.P_NO_AXIS 同字面（证据充分性告警：not_duplicate 无机器可
# 证伪轴）。本层不 import judge_proof（组件分层），字面一致由两侧测试钉死。
P_NO_AXIS_WARNING = "P_NO_AXIS"

# 合同 §三/本地枚举 → 对级 unresolved 码（pair_compare UNRESOLVED_CODES
# 白名单成员；码面不动，语义最近邻映射，真值由 detail 携合同枚举承载）。
# 提交一：ORDER_DISAGREE/JUDGE_DOUBTFUL 改映 JUDGE_UNCERTAIN（判官已跑、
# 语义未决，与"判官未跑/调用失败"的 SUBJECT_UNRESOLVED 通道区分开）。
_FAILURE_TO_CODE = {
    BUDGET_EXCEEDED: "CANDIDATE_BUDGET_EXHAUSTED",
    TIMEOUT: "DEPENDENCY_TIMEOUT",
    MV_REJECTED: "EVIDENCE_INVALID",           # 兼容保留（提交一起不再硬抛）
    EVIDENCE_UNBOUND: "EVIDENCE_INVALID",      # 兼容保留（提交一起降为诊断）
    INVALID_OUTPUT: "EVIDENCE_INVALID",
    CONCLUSION_CONTRADICTS: "NUMERIC_ALIGNMENT_FAILED",  # 兼容保留（降为诊断）
    ORDER_DISAGREE: JUDGE_UNCERTAIN,
    JUDGE_NOT_INJECTED: "SUBJECT_UNRESOLVED",
    JUDGE_EXCEPTION: "SUBJECT_UNRESOLVED",
    JUDGE_FAILURE: "SUBJECT_UNRESOLVED",
    JUDGE_DOUBTFUL: JUDGE_UNCERTAIN,
}

# legacy_proof_gate 模式（提交三 §5.3）硬失败→内部码映射——提交一前口径：
# 双序分歧/双序存疑回落 SUBJECT_UNRESOLVED（无 JUDGE_UNCERTAIN 分流）。
_LEGACY_FAILURE_TO_CODE = {
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

_REASON_FALLBACK = "（判官本顺序未形成可绑定理由）"


class JudgeBudgetExceeded(RuntimeError):
    """判官侧预算耗尽信号（异常通道）——适配层映射 BUDGET_EXCEEDED 未决。

    注入件（含 P1-a 真件）预算尽时抛本异常；其他异常一律 JUDGE_EXCEPTION。
    """


class JudgeProofError(ValueError):
    """VerifiedJudgeProof 合同合规校验失败（detail 首词=失败枚举码）。"""


# ---------------------------------------------------------------- 结果形态

@dataclass(frozen=True)
class ValidatedJudgeOrder:
    """单顺序判官结果的合同校验 + 证据诊断产物（提交一 §4.1 分层）。

    verdict/reason 为语义面；quotes/falsification 为成功绑定的证据面；
    evidence_status/evidence_warnings/machine_findings 为证据诊断面——
    诊断只描述证据质量（pass/warn/fail），不得单独改变语义判定。
    """
    verdict: str                          # ∈ PROOF_VERDICTS
    reason: str                           # 模型理由（缺省稳定兜底文案）
    quotes_a: tuple[dict, ...]            # 成功绑定本侧原文的引文（归一化）
    quotes_b: tuple[dict, ...]
    falsification: dict | None            # 绑定成功的证伪结构；无/失效 None
    evidence_status: str                  # "pass" | "warn" | "fail"
    evidence_warnings: tuple[str, ...]    # EVIDENCE_*/P_NO_AXIS 等告警 token
    machine_findings: tuple[str, ...]     # machine_verify.rules_triggered（P_*）


@dataclass(frozen=True)
class JudgePairOutcome:
    """判官适配层对级产出（合同 §四/§4.2 矩阵映射后）。"""
    outcome: str                      # "equivalent" | "conflict" | "unresolved"
    code: str                         # PairResult 码（对应白名单成员）
    detail: str                       # 人读说明（携合同枚举，不携白名单码）
    failure_reason: str | None = None  # 合同 §三/本地枚举；签发态 None
    used_evidence: tuple = ()         # EvidenceRef（签发态实填，未决空）
    verified_conflicts: tuple = ()    # VerifiedConflict（conflict 实填）
    proofs: Mapping = field(default_factory=dict)  # {"ab": proof|None, "ba": proof|None} 审计留存
    # 提交一：证据诊断（内部审计/日志专用，绝不进公共五字段、绝不改判）
    evidence_status: str = "pass"     # 双序合并最劣档（fail > warn > pass）
    evidence_warnings: tuple = ()     # 双序告警 token 并集（定序去重）
    machine_findings: tuple = ()      # 双序 P_* 机检发现并集（定序去重）
    # 提交三（§5.3 灰度）：本次裁决所用口径；comparison 仅 legacy 模式
    # 实填（新旧两口径离线对照 dict：legacy_*/semantic_* 三元组 +
    # changed 布尔），semantic 模式 None。诊断面，不进公共五字段。
    decision_mode: str = MODE_SEMANTIC_AUTHORITY
    comparison: Mapping | None = None
    # 提交一修复（2026-10-10 整改令四）：完整原文回退证据显式标记——
    # 不与精确引文同质量级（此前只能从 warnings 反推，现在结构化字段直给；
    # 诊断面，不进公共五字段）。legacy 口径无回退证据，恒 False。
    full_text_fallback_used: bool = False


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

    终审 P1 证据路线裁定（2026-10-11 修复包）：判官输入**零机器证据**
    ——judge_machine_evidence 产物只用于 decide/service 的确定性签发
    闸（r7_signing_gate，判官结论出来后执法）与审计观测，不经本函数
    注入判官上下文（撤回一期"机器候选证据供判官终审"的实现路线；判官
    按 judge_v6 条款就正文独立裁决）。上下文键集与基线 2d0d418 全等
    （缓存键/证明/审计面零增量字段）。
    """
    ctx = {
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
    return ctx


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


# ---------------------------------------------------------------- 合同级硬校验（提交一：与证据校验拆分）

_PROOF_REQUIRED_FIELDS = (
    "pair_id", "item_a_id", "item_b_id",
    "text_a_sha256", "text_b_sha256",
    "order", "verdict",
    "quotes_a", "quotes_b",
    "numeric_check", "time_check", "machine_verify",
    "model_version", "prompt_sha256", "policy_version",
    "judged_at", "cache_key",
)

# machine_verify.mode 合法值："audit"（提交一起真件在线语义）与 "gate"
# （历史证明工件/测试 double 兼容）。mode 只陈述核验姿态，passed 自提交一
# 起为审计保留字段——消费侧不得再拿它改判（§5.1 文件 B）。
_MACHINE_VERIFY_MODES = ("audit", "gate")


def _fail(reason: str, detail: str) -> JudgeProofError:
    return JudgeProofError(f"{reason}: {detail}")


def _is_hex64(value: Any) -> bool:
    return (isinstance(value, str) and len(value) == 64
            and all(c in "0123456789abcdef" for c in value))


def _validate_contract(proof: Any, ctx: Mapping) -> None:
    """合同级硬校验（提交一保留的全部硬闸）：身份绑定/正文 SHA/order/
    verdict 枚举/版本四维/cache key/机检段结构。不合规一律 JudgeProofError
    （首词=合同 §三 枚举；适配层 catch 后映射未决）。

    证据级内容（引文能否绑定、passed 真假、结论对拍、falsification 在场性）
    一律不在本层——那是 validate_proof 的诊断面。
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
    # 机侧独立核验段（数值/时间结论三值闭合——结构闸，不信其结论内容）
    for check_key in ("numeric_check", "time_check"):
        check = proof[check_key]
        if not isinstance(check, Mapping):
            raise _fail(INVALID_OUTPUT, f"{check_key} 非对象")
        if check.get("conclusion") not in CHECK_CONCLUSIONS:
            raise _fail(INVALID_OUTPUT,
                        f"{check_key}.conclusion 非法：{check.get('conclusion')!r}")
    # 机器验段结构闸（mode 合法 + passed bool）；passed 值本身只是诊断
    mv = proof["machine_verify"]
    if not isinstance(mv, Mapping):
        raise _fail(INVALID_OUTPUT, "machine_verify 非对象")
    if mv.get("mode") not in _MACHINE_VERIFY_MODES:
        raise _fail(INVALID_OUTPUT,
                    f"machine_verify.mode 非法：{mv.get('mode')!r}"
                    f"（合法：{_MACHINE_VERIFY_MODES}）")
    if type(mv.get("passed")) is not bool:
        raise _fail(INVALID_OUTPUT, "machine_verify.passed 非 bool")


# ---------------------------------------------------------------- 证据级诊断（提交一：不再抛错）

def _check_quote_structure(quotes: Any, side: str) -> list:
    """引文段结构硬校验（输出结构非法=合同失败，§4.1）：list + 每条
    Mapping + 四键齐全 + text/role 非空 str + offset 为 int。
    返原始条目列表；结构非法 → JudgeProofError(INVALID_OUTPUT)。"""
    if not isinstance(quotes, list):
        raise _fail(INVALID_OUTPUT, f"{side} 引文段非 list：{type(quotes).__name__}")
    for index, quote in enumerate(quotes):
        if not isinstance(quote, Mapping):
            raise _fail(INVALID_OUTPUT, f"{side} 引文[{index}]非对象")
        for key in ("text", "offset_start", "offset_end", "role"):
            if key not in quote:
                raise _fail(INVALID_OUTPUT, f"{side} 引文[{index}]缺键 {key!r}")
        if not isinstance(quote["text"], str) or not quote["text"]:
            raise _fail(INVALID_OUTPUT, f"{side} 引文[{index}] text 非非空字符串")
        if not isinstance(quote["role"], str) or not quote["role"]:
            raise _fail(INVALID_OUTPUT, f"{side} 引文[{index}] role 非非空字符串")
        if (type(quote["offset_start"]) is not int
                or type(quote["offset_end"]) is not int):
            raise _fail(INVALID_OUTPUT, f"{side} 引文[{index}] offset 非 int")
    return quotes


def _bind_quotes_diagnostic(quotes: list, bound_text: str, side: str,
                            warnings: list) -> list:
    """引文 offset 绑定诊断（提交一：越界/回指失败只记 EVIDENCE_UNBOUND
    告警并丢弃该条，不再抛错）。返成功绑定的归一化引文列表。"""
    normalized = []
    for index, quote in enumerate(quotes):
        text = quote["text"]
        start, end = quote["offset_start"], quote["offset_end"]
        if not (0 <= start < end <= len(bound_text)):
            warnings.append(EVIDENCE_UNBOUND)
            continue
        if bound_text[start:end] != text:
            warnings.append(EVIDENCE_UNBOUND)
            continue
        normalized.append({"text": text, "offset_start": start,
                           "offset_end": end, "role": quote["role"]})
    return normalized


def _bind_falsification_diagnostic(fals: Any, ctx: Mapping,
                                   warnings: list) -> dict | None:
    """falsification 绑定诊断（提交一 §5.1 修改点 1）：

    - 缺失/非 Mapping/relation 空 → None（调用侧记 P_NO_AXIS 告警）；
    - dimension 越出合同词表/结构键非法 → 硬失败 INVALID_OUTPUT
      （输出结构非法=合同失败，组件漂移不冒充）；
    - 证据 offset 无法回指 → 记 EVIDENCE_UNBOUND 告警并返 None
      （证伪失效=无轴，调用侧补 P_NO_AXIS）。
    """
    if not isinstance(fals, Mapping):
        return None
    if fals.get("dimension") not in FALSIFICATION_DIMENSIONS:
        raise _fail(INVALID_OUTPUT,
                    f"证伪 dimension 非法：{fals.get('dimension')!r}")
    if (not isinstance(fals.get("relation"), str)
            or not fals["relation"].strip()):
        return None
    spans: dict[str, dict] = {}
    for key, side in (("evidence_a", "a 侧"), ("evidence_b", "b 侧")):
        span = fals.get(key)
        bound_text = ctx["text_a"] if key == "evidence_a" else ctx["text_b"]
        if not isinstance(span, Mapping):
            raise _fail(INVALID_OUTPUT, f"{side} 证伪证据非对象")
        for field_key in ("text", "offset_start", "offset_end"):
            if field_key not in span:
                raise _fail(INVALID_OUTPUT, f"{side} 证伪证据缺键 {field_key!r}")
        text, start, end = span["text"], span["offset_start"], span["offset_end"]
        if not isinstance(text, str) or not text:
            raise _fail(INVALID_OUTPUT, f"{side} 证伪证据 text 非非空字符串")
        if type(start) is not int or type(end) is not int:
            raise _fail(INVALID_OUTPUT, f"{side} 证伪证据 offset 非 int")
        if (not (0 <= start < end <= len(bound_text))
                or bound_text[start:end] != text):
            warnings.append(EVIDENCE_UNBOUND)   # 诊断：证伪引文无法回指
            return None
        spans[key] = {"text": text, "offset_start": start, "offset_end": end}
    return {"dimension": fals["dimension"],
            "evidence_a": spans["evidence_a"],
            "evidence_b": spans["evidence_b"],
            "relation": fals["relation"].strip()}


def validate_proof(proof: Any, ctx: Mapping) -> ValidatedJudgeOrder:
    """VerifiedJudgeProof 合同校验 + 证据诊断（提交一 §4.1/§5.1）。

    两道合同级硬校验（_validate_contract：身份绑定/SHA/order/cache key/
    verdict 枚举/版本四维/结构）不合规仍抛 JudgeProofError——映射未决。
    证据级检查全部降为诊断，**不再抛错、不再改判**：

    - quotes_a/quotes_b 为空 → EVIDENCE_QUOTES_EMPTY 告警；
    - 引文 offset 越界/回指失败 → EVIDENCE_UNBOUND 告警（丢弃该条）；
    - machine_verify.passed=false → MACHINE_VERIFY_REJECTED 告警，
      rules_triggered 全量入 machine_findings（P_* 诊断不丢失）；
    - verdict=duplicate 与机侧 numeric/time 结论"不一致"对拍
      → CONCLUSION_CONTRADICTS 告警（不再否决语义判定）；
    - not_duplicate 缺/失效 falsification → P_NO_AXIS 告警
      （证据充分性问题，不再降级 doubtful）。

    返 ValidatedJudgeOrder（语义面 + 证据面 + 诊断面三段）。
    """
    _validate_contract(proof, ctx)
    verdict = proof["verdict"]
    raw_reason = proof.get("reason")
    reason = (raw_reason.strip()
              if isinstance(raw_reason, str) and raw_reason.strip()
              else _REASON_FALLBACK)

    warnings: list[str] = []
    findings: list[str] = []
    mv = proof["machine_verify"]
    rules = mv.get("rules_triggered") or []
    if isinstance(rules, (list, tuple)):
        findings.extend(str(rule) for rule in rules)
    if mv["passed"] is False:
        warnings.append(MACHINE_VERIFY_REJECTED)   # 诊断：不改判
    adapter_warnings = proof.get("evidence_warnings")
    if isinstance(adapter_warnings, (list, tuple)):
        warnings.extend(str(w) for w in adapter_warnings
                        if isinstance(w, str) and w)

    quotes_a: list[dict] = []
    quotes_b: list[dict] = []
    falsification: dict | None = None
    if verdict in ("duplicate", "not_duplicate"):
        # 引文段：结构硬闸（INVALID_OUTPUT）+ 绑定诊断（EVIDENCE_UNBOUND）
        for key, side, bound in (("quotes_a", "a 侧", ctx["text_a"]),
                                 ("quotes_b", "b 侧", ctx["text_b"])):
            entries = _check_quote_structure(proof[key], side)
            if not entries:
                warnings.append(EVIDENCE_QUOTES_EMPTY)
            bound_quotes = _bind_quotes_diagnostic(entries, bound, side, warnings)
            if key == "quotes_a":
                quotes_a = bound_quotes
            else:
                quotes_b = bound_quotes
        # duplicate 与机侧分项结论对拍（诊断：不再 CONCLUSION_CONTRADICTS 硬抛）
        if verdict == "duplicate":
            contradicted = [
                key for key in ("numeric_check", "time_check")
                if proof[key]["conclusion"] == "不一致"]
            if contradicted:
                warnings.append(CONCLUSION_CONTRADICTS)
        # not_duplicate 证伪（诊断：缺/失效 → P_NO_AXIS 告警，不降级）
        if verdict == "not_duplicate":
            falsification = _bind_falsification_diagnostic(
                proof.get("falsification"), ctx, warnings)
            if falsification is None:
                warnings.append(P_NO_AXIS_WARNING)

    # 定序去重（首见序；告警/发现集合确定性输出）
    warnings = list(dict.fromkeys(warnings))
    findings = list(dict.fromkeys(findings))
    if verdict in ("duplicate", "not_duplicate") and not (quotes_a or quotes_b):
        status = "fail"        # 签发判定双侧均无可绑定引文（回退证据兜底）
    elif warnings or findings:
        status = "warn"
    else:
        status = "pass"
    return ValidatedJudgeOrder(
        verdict=verdict, reason=reason,
        quotes_a=tuple(quotes_a), quotes_b=tuple(quotes_b),
        falsification=falsification, evidence_status=status,
        evidence_warnings=tuple(warnings), machine_findings=tuple(findings))


# ---------------------------------------------------------------- 对级映射（合同 §四 + §4.2 矩阵）

def _quotes_to_evidence(quotes: list | tuple, record_id: str) -> tuple:
    """归一化引文 → EvidenceRef 元组（审计/聚合消费面=EvidenceRef 属性协议）。"""
    return tuple(EvidenceRef(record_id=record_id, field="text",
                             quote=q["text"], start=q["offset_start"],
                             end=q["offset_end"]) for q in quotes)


def _side_evidence(quotes: tuple, record_id: str, full_text: str,
                   fallback_sides: list) -> tuple:
    """单侧审计证据：有绑定成功引文用引文；**一条都绑不上** → 完整原文
    回退证据（field="text"、quote=该侧完整正文、start=0、end=len——
    只证明"判定对应的原文版本"，不冒充精确字段证明），并登记
    EVIDENCE_FALLBACK_FULL_TEXT（由调用侧并表）。回退证据只进内部
    审计/诊断，绝不进公共五字段（§5.1 修改点 3）。"""
    if quotes:
        return _quotes_to_evidence(quotes, record_id)
    fallback_sides.append(record_id)
    return (EvidenceRef(record_id=record_id, field="text", quote=full_text,
                        start=0, end=len(full_text)),)


def _collect_order_evidence(pair_context: Mapping,
                            per_order: Mapping) -> tuple:
    """双序 EvidenceRef 汇总（ab 后 ba、每序 a 侧后 b 侧，定序确定）；
    返 (evidence, had_fallback)——任一侧走了完整原文回退则 had_fallback。"""
    evidence: list = []
    fallback_sides: list = []
    for order in ORDERS:
        validated = per_order[order]
        ctx = _order_context(pair_context, order)
        evidence.extend(_side_evidence(
            validated.quotes_a, ctx["record_a_id"], ctx["text_a"],
            fallback_sides))
        evidence.extend(_side_evidence(
            validated.quotes_b, ctx["record_b_id"], ctx["text_b"],
            fallback_sides))
    return tuple(evidence), bool(fallback_sides)


def _merge_diagnostics(per_order: Mapping, orders: tuple) -> tuple:
    """双序诊断并表（定序去重）：返 (status, warnings, findings)。
    status 取最劣档（fail > warn > pass）；orders=参与并表的顺序。"""
    warnings: list[str] = []
    findings: list[str] = []
    status = "pass"
    for order in orders:
        validated = per_order.get(order)
        if validated is None:
            continue
        warnings.extend(validated.evidence_warnings)
        findings.extend(validated.machine_findings)
        if validated.evidence_status == "fail":
            status = "fail"
        elif validated.evidence_status == "warn" and status != "fail":
            status = "warn"
    return (status, tuple(dict.fromkeys(warnings)),
            tuple(dict.fromkeys(findings)))


# 提交一修复（2026-10-10 整改令五）：判官源公共理由**固定措辞**——
# 不得使用"已验证""已逐一核验"等超出实际证据能力的表述；双序模型理由
# 仍完整留存 proofs 审计件（内部面），公共 detail 只给能力内声明。
JUDGE_DUPLICATE_REASON = (
    "判官双序一致认为两条快讯描述同一核心事实，因此判定为重复。")
JUDGE_NON_DUPLICATE_REASON = (
    "判官双序一致判定为不重复；未形成确定性规则冲突，"
    "相关证据质量信息已记录审计。")


def _unresolved(reason: str, proofs: Mapping,
                per_order: Mapping | None = None,
                *, decision_mode: str = MODE_SEMANTIC_AUTHORITY,
                ) -> JudgePairOutcome:
    code = _FAILURE_TO_CODE[reason]
    status, warnings, findings = ("pass", (), ())
    if per_order:
        status, warnings, findings = _merge_diagnostics(per_order, ORDERS)
    return JudgePairOutcome(
        outcome="unresolved", code=code,
        detail=f"判官未决进人工（{reason}）。",
        failure_reason=reason, proofs=proofs,
        evidence_status=status, evidence_warnings=warnings,
        machine_findings=findings, decision_mode=decision_mode)


def _legacy_unresolved(reason: str, proofs: Mapping,
                       per_order: Mapping | None = None) -> JudgePairOutcome:
    """legacy_proof_gate 模式未决（提交一前口径：码映射走
    _LEGACY_FAILURE_TO_CODE，分歧/存疑回落 SUBJECT_UNRESOLVED）。"""
    code = _LEGACY_FAILURE_TO_CODE[reason]
    status, warnings, findings = ("pass", (), ())
    if per_order:
        status, warnings, findings = _merge_diagnostics(per_order, ORDERS)
    return JudgePairOutcome(
        outcome="unresolved", code=code,
        detail=f"判官未决进人工（{reason}）。",
        failure_reason=reason, proofs=proofs,
        evidence_status=status, evidence_warnings=warnings,
        machine_findings=findings, decision_mode=MODE_LEGACY_PROOF_GATE)


def _collect_order_evidence_strict(pair_context: Mapping,
                                   per_order: Mapping) -> tuple:
    """legacy 模式审计证据：只取成功绑定引文（提交一前无完整原文回退）。"""
    evidence: list = []
    for order in ORDERS:
        validated = per_order[order]
        ctx = _order_context(pair_context, order)
        evidence.extend(_quotes_to_evidence(validated.quotes_a,
                                            ctx["record_a_id"]))
        evidence.extend(_quotes_to_evidence(validated.quotes_b,
                                            ctx["record_b_id"]))
    return tuple(evidence)


def _semantic_outcome(pair_context: Mapping, per_order: Mapping,
                      raw_proofs: Mapping, hard_failures: list,
                      verdicts: Mapping) -> JudgePairOutcome:
    """§4.2 矩阵合并（提交一 semantic_authority 口径）。"""
    # 分支 1：双序 duplicate → equivalent（证明告警只进审计）
    if verdicts["ab"] == "duplicate" and verdicts["ba"] == "duplicate":
        evidence, had_fallback = _collect_order_evidence(pair_context, per_order)
        status, warnings, findings = _merge_diagnostics(per_order, ORDERS)
        if had_fallback and EVIDENCE_FALLBACK_FULL_TEXT not in warnings:
            warnings = warnings + (EVIDENCE_FALLBACK_FULL_TEXT,)
        return JudgePairOutcome(
            outcome="equivalent", code=JUDGE_EQUIVALENT,
            detail=JUDGE_DUPLICATE_REASON,
            used_evidence=evidence, proofs=raw_proofs,
            evidence_status=status, evidence_warnings=warnings,
            machine_findings=findings,
            full_text_fallback_used=had_fallback)

    # 分支 2：双序 not_duplicate → conflict（不再强制机器证伪轴）
    if verdicts["ab"] == "not_duplicate" and verdicts["ba"] == "not_duplicate":
        evidence, had_fallback = _collect_order_evidence(pair_context, per_order)
        status, warnings, findings = _merge_diagnostics(per_order, ORDERS)
        if had_fallback and EVIDENCE_FALLBACK_FULL_TEXT not in warnings:
            warnings = warnings + (EVIDENCE_FALLBACK_FULL_TEXT,)
        # 有绑定成功的 falsification 时构造 VerifiedConflict 审计件
        # （ab 序优先、ba 序兜底——双序各独立绑定，取首份可用，确定性）；
        # 证据归属按**来源序**的 a/b 角色映射回 history/current（ba 序
        # a=current、b=history，直取会张冠李戴）。
        conflicts: tuple = ()
        fals_order = next((order for order in ORDERS
                           if per_order[order].falsification is not None), None)
        if fals_order is not None:
            fals = per_order[fals_order].falsification
            ctx_f = _order_context(pair_context, fals_order)
            side_evidence = {
                "a": EvidenceRef(
                    record_id=ctx_f["record_a_id"], field="text",
                    quote=fals["evidence_a"]["text"],
                    start=fals["evidence_a"]["offset_start"],
                    end=fals["evidence_a"]["offset_end"]),
                "b": EvidenceRef(
                    record_id=ctx_f["record_b_id"], field="text",
                    quote=fals["evidence_b"]["text"],
                    start=fals["evidence_b"]["offset_start"],
                    end=fals["evidence_b"]["offset_end"]),
            }
            history_side = "a" if fals_order == "ab" else "b"
            current_side = "b" if fals_order == "ab" else "a"
            conflict = VerifiedConflict(
                field_path=f"judge_falsification.{fals['dimension']}",
                basis="JUDGE_FALSIFICATION",
                history_evidence=side_evidence[history_side],
                current_evidence=side_evidence[current_side],
                detail=fals["relation"])
            conflicts = (conflict,)
            evidence = (conflict.history_evidence,
                        conflict.current_evidence) + evidence
        return JudgePairOutcome(
            outcome="conflict", code=JUDGE_NON_DUPLICATE,
            detail=JUDGE_NON_DUPLICATE_REASON,
            used_evidence=evidence, verified_conflicts=conflicts,
            proofs=raw_proofs,
            evidence_status=status, evidence_warnings=warnings,
            machine_findings=findings,
            full_text_fallback_used=had_fallback)

    # 分支 3：其余一切组合 → unresolved（确定性归因，硬失败优先）
    for reason in _HARD_FAILURE_PRIORITY:
        if reason in hard_failures:
            return _unresolved(reason, proofs=raw_proofs, per_order=per_order)
    if {v for v in verdicts.values() if v} == {"duplicate", "not_duplicate"}:
        return _unresolved(ORDER_DISAGREE, proofs=raw_proofs,
                           per_order=per_order)
    return _unresolved(JUDGE_DOUBTFUL, proofs=raw_proofs, per_order=per_order)


def _legacy_veto(validated: ValidatedJudgeOrder) -> str | None:
    """legacy_proof_gate 单顺序否决链（提交一前证明闸语义，按旧优先序）：
    签发判定无可绑定引文 → EVIDENCE_UNBOUND；机器验 passed=false →
    MV_REJECTED；duplicate 与机侧结论对拍冲突 → CONCLUSION_CONTRADICTS。
    未命中 → None。只作用于 duplicate/not_duplicate 签发态。"""
    if validated.verdict not in ("duplicate", "not_duplicate"):
        return None
    warnings = set(validated.evidence_warnings)
    if EVIDENCE_UNBOUND in warnings or EVIDENCE_QUOTES_EMPTY in warnings:
        return EVIDENCE_UNBOUND
    if MACHINE_VERIFY_REJECTED in warnings:
        return MV_REJECTED
    if validated.verdict == "duplicate" and CONCLUSION_CONTRADICTS in warnings:
        return CONCLUSION_CONTRADICTS
    return None


def _legacy_outcome(pair_context: Mapping, per_order: Mapping,
                    raw_proofs: Mapping, hard_failures: list,
                    verdicts: Mapping) -> JudgePairOutcome:
    """legacy_proof_gate 对级合并（提交一前证明闸口径，§5.3 灰度回滚通道）。

    与提交一前 adjudicate_pair 逐条对应：证据诊断在本口径下重升否决项；
    not_duplicate 无证伪轴降级 doubtful（合同 §二 旧条款）；双序分歧/存疑
    回落 SUBJECT_UNRESOLVED；审计证据只用绑定成功引文（无回退证据）。
    """
    legacy_verdicts: dict[str, str | None] = {}
    legacy_hard: list[str] = list(hard_failures)
    for order in ORDERS:
        validated = per_order[order]
        if validated is None:
            legacy_verdicts[order] = None
            continue
        veto = _legacy_veto(validated)
        if veto is not None:
            legacy_hard.append(veto)
            legacy_verdicts[order] = None
            continue
        if (validated.verdict == "not_duplicate"
                and validated.falsification is None):
            legacy_verdicts[order] = "doubtful"   # 旧 §二：无轴降级存疑
        else:
            legacy_verdicts[order] = validated.verdict

    if legacy_verdicts["ab"] == "duplicate" and legacy_verdicts["ba"] == "duplicate":
        status, warnings, findings = _merge_diagnostics(per_order, ORDERS)
        return JudgePairOutcome(
            outcome="equivalent", code="FACT_EQUIVALENT",
            detail="判官双序一致判定同一事实，引文已绑定双侧原文且机器验 gate 通过。",
            used_evidence=_collect_order_evidence_strict(pair_context, per_order),
            proofs=raw_proofs,
            evidence_status=status, evidence_warnings=warnings,
            machine_findings=findings, decision_mode=MODE_LEGACY_PROOF_GATE)

    if (legacy_verdicts["ab"] == "not_duplicate"
            and legacy_verdicts["ba"] == "not_duplicate"):
        # 到达此处两侧 falsification 必非 None（None 已降级 doubtful）
        status, warnings, findings = _merge_diagnostics(per_order, ORDERS)
        fals = per_order["ab"].falsification
        ctx_ab = _order_context(pair_context, "ab")
        conflict = VerifiedConflict(
            field_path=f"judge_falsification.{fals['dimension']}",
            basis="JUDGE_FALSIFICATION",
            history_evidence=EvidenceRef(
                record_id=ctx_ab["record_a_id"], field="text",
                quote=fals["evidence_a"]["text"],
                start=fals["evidence_a"]["offset_start"],
                end=fals["evidence_a"]["offset_end"]),
            current_evidence=EvidenceRef(
                record_id=ctx_ab["record_b_id"], field="text",
                quote=fals["evidence_b"]["text"],
                start=fals["evidence_b"]["offset_start"],
                end=fals["evidence_b"]["offset_end"]),
            detail=fals["relation"])
        return JudgePairOutcome(
            outcome="conflict", code="VERIFIED_CONFLICT",
            detail=(f"判官双序一致证伪同一事实（维度 {fals['dimension']}），"
                    "双侧证伪引文已绑定原文且机器验 gate 通过。"),
            used_evidence=(conflict.history_evidence,
                           conflict.current_evidence),
            verified_conflicts=(conflict,), proofs=raw_proofs,
            evidence_status=status, evidence_warnings=warnings,
            machine_findings=findings, decision_mode=MODE_LEGACY_PROOF_GATE)

    for reason in _HARD_FAILURE_PRIORITY:
        if reason in legacy_hard:
            return _legacy_unresolved(reason, proofs=raw_proofs,
                                      per_order=per_order)
    if {v for v in legacy_verdicts.values() if v} == {"duplicate", "not_duplicate"}:
        return _legacy_unresolved(ORDER_DISAGREE, proofs=raw_proofs,
                                  per_order=per_order)
    return _legacy_unresolved(JUDGE_DOUBTFUL, proofs=raw_proofs,
                              per_order=per_order)


def adjudicate_pair(judge_callable: Callable | None, pair_context: Mapping, *,
                    timeout_s: float | None = None,
                    decision_mode: str | None = None) -> JudgePairOutcome:
    """对一对未决候选跑双序判官并合并对级结论（提交三 §5.3 灰度版）。

    decision_mode=None → 读 DEDUP_JUDGE_DECISION_MODE（默认
    legacy_proof_gate）；显式传参优先（测试/回放通道）。

    - judge_callable=None → 默认实现 fail-closed 未决（JUDGE_NOT_INJECTED）；
    - semantic_authority（提交一 §4.2 矩阵）：双序 duplicate →
      equivalent（JUDGE_EQUIVALENT）；双序 not_duplicate → conflict
      （JUDGE_NON_DUPLICATE，无轴记 P_NO_AXIS 不降级）；分歧/存疑 →
      JUDGE_UNCERTAIN；证明层告警只进审计/诊断，无一票否决；
    - legacy_proof_gate（默认）：生效结论=提交一前证明闸口径（证据诊断
      重升否决项），同一份判官结果**离线**并行计算 semantic 结论记入
      comparison（不增加 LLM 调用）；
    - 超时/预算/异常/合同不合规/verdict=failure·invalid → 未决
      （合同 §三 映射 UNRESOLVED 白名单码；两口径各自映射表）。
    """
    timeout_s = DEFAULT_JUDGE_TIMEOUT_S if timeout_s is None else timeout_s
    if decision_mode is not None and decision_mode not in _JUDGE_DECISION_MODES:
        raise ValueError(
            f"decision_mode={decision_mode!r} 非法：只允许 "
            f"{'|'.join(_JUDGE_DECISION_MODES)}（None=读 "
            f"{JUDGE_DECISION_MODE_ENV}，默认 {DEFAULT_JUDGE_DECISION_MODE}）")
    mode = (decision_mode if decision_mode is not None
            else judge_decision_mode())
    if judge_callable is None:
        if mode == MODE_LEGACY_PROOF_GATE:
            return _legacy_unresolved(JUDGE_NOT_INJECTED,
                                      proofs={"ab": None, "ba": None})
        return _unresolved(JUDGE_NOT_INJECTED, proofs={"ab": None, "ba": None})

    per_order: dict[str, ValidatedJudgeOrder | None] = {}
    raw_proofs: dict[str, Any] = {"ab": None, "ba": None}
    hard_failures: list[str] = []
    for order in ORDERS:
        ctx = _order_context(pair_context, order)
        proof, failure = _call_with_timeout(judge_callable, ctx, timeout_s)
        raw_proofs[order] = proof
        if failure is not None:
            hard_failures.append(failure)
            per_order[order] = None
            continue
        try:
            validated = validate_proof(proof, ctx)
        except JudgeProofError as exc:
            reason = str(exc).split(":", 1)[0]
            hard_failures.append(reason if reason in _FAILURE_TO_CODE
                                 else INVALID_OUTPUT)
            per_order[order] = None
            continue
        verdict = validated.verdict
        if verdict == "failure":
            hard_failures.append(JUDGE_FAILURE)
            per_order[order] = None
        elif verdict == "invalid":
            hard_failures.append(INVALID_OUTPUT)
            per_order[order] = None
        else:
            per_order[order] = validated

    verdicts = {order: (per_order[order].verdict if per_order[order] is not None
                        else None) for order in ORDERS}

    # 同一份判官结果：semantic 结论恒算（legacy 模式下离线对照用，
    # 不增加 LLM 调用——per_order/raw_proofs 均为已跑结果）。
    semantic = _semantic_outcome(pair_context, per_order, raw_proofs,
                                 hard_failures, verdicts)
    if mode == MODE_SEMANTIC_AUTHORITY:
        return replace(semantic, decision_mode=MODE_SEMANTIC_AUTHORITY)

    legacy = _legacy_outcome(pair_context, per_order, raw_proofs,
                             hard_failures, verdicts)
    comparison = {
        "legacy_outcome": legacy.outcome,
        "legacy_code": legacy.code,
        "legacy_failure_reason": legacy.failure_reason,
        "semantic_outcome": semantic.outcome,
        "semantic_code": semantic.code,
        "semantic_failure_reason": semantic.failure_reason,
        "changed": ((legacy.outcome, legacy.code, legacy.failure_reason)
                    != (semantic.outcome, semantic.code,
                        semantic.failure_reason)),
    }
    return replace(legacy, comparison=comparison)


__all__ = [
    "JUDGE_IN_CHAIN_ENV", "judge_in_chain_enabled",
    "JUDGE_DECISION_MODE_ENV", "judge_decision_mode",
    "MODE_LEGACY_PROOF_GATE", "MODE_SEMANTIC_AUTHORITY",
    "DEFAULT_JUDGE_DECISION_MODE",
    "ORDERS", "PROOF_VERDICTS", "FALSIFICATION_DIMENSIONS", "CHECK_CONCLUSIONS",
    "JUDGE_EQUIVALENT", "JUDGE_NON_DUPLICATE", "JUDGE_UNCERTAIN",
    "MV_REJECTED", "ORDER_DISAGREE", "INVALID_OUTPUT", "TIMEOUT",
    "BUDGET_EXCEEDED", "EVIDENCE_UNBOUND", "CONCLUSION_CONTRADICTS",
    "JUDGE_NOT_INJECTED", "JUDGE_EXCEPTION", "JUDGE_FAILURE", "JUDGE_DOUBTFUL",
    "EVIDENCE_QUOTES_EMPTY", "EVIDENCE_FALLBACK_FULL_TEXT",
    "MACHINE_VERIFY_REJECTED", "P_NO_AXIS_WARNING",
    "JUDGE_DUPLICATE_REASON", "JUDGE_NON_DUPLICATE_REASON",
    "DEFAULT_JUDGE_TIMEOUT_S",
    "JudgeBudgetExceeded", "JudgeProofError", "JudgePairOutcome",
    "ValidatedJudgeOrder",
    "compute_cache_key", "build_pair_context", "validate_proof",
    "adjudicate_pair",
]
