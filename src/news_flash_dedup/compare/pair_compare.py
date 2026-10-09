"""P16-B 对级精判 compare_pair。

按 09 §9.2 / §11.3 实现的直接文本对判定：先 VerifiedConflict，再关键未解，最后等价的
三态机。`PairBindingError` 严格核 raw_hash / pipeline_version / record_id / item_id /
scope_id / business_date / arrival_seq 早序。

设计依据：`log/P14-16-Fact精判集成设计.md` §3.3（PairResult、PairBindingError、对级预算）。

2026-10-09（P0-b 修订一，主窗口修订令）：DEDUP_CERT_DECOUPLE 开时判定序
改为 冲突→有效 EXACT/LOSSLESS 证书→直接 equivalent→issues→equivalence_ready
（冲突一票否决保留；开关关=旧序逐字节）。详见 compare_pair 判定段注释。
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from news_flash_dedup.facts import FactValidationReport
from news_flash_dedup.text import cert_decouple_enabled

from .pair_alignment import AlignedPair, PairAlignmentOutcome
from .value_time import EvidenceRef


PairOutcome = Literal["equivalent", "conflict", "unresolved"]


EQUIVALENT_CODES = frozenset({
    "EXACT_TEXT_MATCH",
    "LOSSLESS_TEXT_MATCH",
    "FACT_EQUIVALENT",
    # 提交一（2026-10-10，p3-semantic-authority，§5.1 修改点 2）：判官
    # 双序一致判重复的语义权威码——不伪装成机器已验证的 FACT_EQUIVALENT。
    "JUDGE_EQUIVALENT",
})
# 2026-10-09（P0-b 修订一，主窗口修订令）：开关 DEDUP_CERT_DECOUPLE 开时
# 可直接签发的有效文本证书码集——EXACT/LOSSLESS 双码（代码可复核通道）；
# FACT_EQUIVALENT 不在其列，仍走旧序 issues→equivalence_ready 闸（完备性
# 约束不被本修订触碰）。
_TEXT_CERT_PROOF_CODES = frozenset({
    "EXACT_TEXT_MATCH",
    "LOSSLESS_TEXT_MATCH",
})
CONFLICT_CODES = frozenset({
    "VERIFIED_CONFLICT",
    # 提交一（§5.1）：判官双序一致判不重复的语义权威码——不再强制机器
    # 证伪轴（无轴记 P_NO_AXIS 审计告警，不降级），也不冒充规则链
    # 已验证硬冲突 VERIFIED_CONFLICT。
    "JUDGE_NON_DUPLICATE",
})
# N21（D28 批准第 2 件；窗口I《方案一致性核验-09.md》N21=一致）：按 09
# §12.1 十五码表（L1315）补 DEPENDENCY_TIMEOUT/EXTRACTION_FAILED/
# EVIDENCE_INVALID 三码——_BOUNDARY_PRIORITY（同模块内常量，同 09 §12.1
# 分层3 主边界码优先序口径；W2Fα1-E4 失锚行号指针退役）本含此三码，
# 白名单补齐后死代码复活、码集与优先序表完全重合。09 §12.1："有持久有效
# 解释的边界是正常业务成功"——证据无效/抽取失败/依赖超时落边界而非
# ComparePairError 技术崩溃（技术失败通道仅留存储/原文/身份损坏）。
# 只补此三码，既有七码一词不动。
UNRESOLVED_CODES = frozenset({
    "SUBJECT_UNRESOLVED",
    "DEPENDENCY_TIMEOUT",
    "RECALL_INCOMPLETE",
    "CANDIDATE_BUDGET_EXHAUSTED",
    "EXTRACTION_FAILED",
    "EVIDENCE_INVALID",
    "NUMERIC_ALIGNMENT_FAILED",
    "TIME_RELATION_UNCERTAIN",
    "RULE_UNCOVERED",
    "FACT_INCOMPLETE",
    # 提交一（§5.1）：判官双序存疑或分歧的未决码（判官已跑、语义未决，
    # 与判官未跑/调用失败的 SUBJECT_UNRESOLVED 通道区分）。
    "JUDGE_UNCERTAIN",
})


_BOUNDARY_PRIORITY = (
    "SUBJECT_UNRESOLVED",
    # 提交一：JUDGE_UNCERTAIN 紧随语义近邻 SUBJECT_UNRESOLVED 之后
    # （判官已跑而未决次于判官未跑/失败进人工）。
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

# W1Fa-2（窗口W1Fα，判定壁，证据保真级）：时间 VerifiedConflict basis 白名单
# ——与 p15_integration.extract_p15_results time_basis 入口闸同一集合；消费侧
# 闸防外部构造 time_pairs 携白名单外基底洗标签。
_TIME_BASIS_WHITELIST = frozenset({
    "TIME_SAME_RELATIVE",
    "TIME_SAME_ABSOLUTE",
    "TIME_SAME_STAGE",
})


class PairBindingError(ValueError):
    """直接工件身份、原文或版本不匹配（INV-2/INV-12 落地）。"""


class ComparePairError(ValueError):
    """对级精判的非业务失败（如 P15 工件构造错误）。"""


@dataclass(frozen=True)
class VerifiedConflict:
    """充分冲突：双侧原文 Evidence + 对应关系 + 角色/单位/币种/方向/否定/事实性质 + 实际差异。"""
    field_path: str
    basis: str                                # 白名单 basis（来自 P15 / P16-A）
    history_evidence: EvidenceRef
    current_evidence: EvidenceRef
    detail: str


@dataclass(frozen=True)
class PairIssue:
    """对级未解：主体不明、覆盖未证、对齐缺失等。"""
    code: str
    detail: str


@dataclass(frozen=True)
class TimePairComparison:
    """P15 compare_time 返回值轻量封装。

    basis：本次时间比较的实际基底（∈ _TIME_BASIS_WHITELIST；W1Fa-2 实传，
    由 p15 集成层以调 compare_time 的同一 time_basis 写入——compare_time
    返回 TimeComparison 仅 relation/reason_code，结构上不携基底字段，
    基底真值在集成层调用参数处）。None 表示外部旧式构造未携带——入列
    VerifiedConflict 时回退默认 TIME_SAME_RELATIVE（修复前恒值，现役
    契约零漂移）。

    history_evidence / current_evidence（W2Fα1，WA1a-D4）：时间冲突**当事
    对齐对**的证据——p15 集成层按产生该 time_pair 的对齐对实传；None=
    旧式构造未携带，入列 VerifiedConflict 时回退 aligned_facts[0]（修复前
    恒值——多对齐对场景恒取首对=张冠李戴，实传后按当事对标注）。实传
    证据入列前经 record_id/原文切片绑定校验（fail-closed）。
    """
    outcome: str        # "conflict" / "compatible" / "unresolved"
    code: str | None = None
    detail: str | None = None
    basis: str | None = None
    history_evidence: EvidenceRef | None = None
    current_evidence: EvidenceRef | None = None

    @classmethod
    def from_value(cls, value: Any) -> "TimePairComparison":
        if isinstance(value, TimePairComparison):
            return value
        if isinstance(value, tuple):
            if len(value) == 2:
                outcome, code = value
                return cls(outcome, code)
            if len(value) == 3:
                outcome, code, detail = value
                return cls(outcome, code, detail)
            if len(value) == 4:
                outcome, code, detail, basis = value
                return cls(outcome, code, detail, basis)
        raise ComparePairError(f"time_pairs entry must be TimePairComparison or (outcome, code[, detail[, basis]]): {value!r}")


@dataclass(frozen=True)
class P15PairResults:
    """P15 局部比较的产物（由 P16 集成层从 AlignedPair 提取并调用 compare_numeric/compare_time 后构造）。"""
    verified_conflicts: tuple[VerifiedConflict, ...] = ()
    time_pairs: tuple[Any, ...] = ()            # 每条是 TimePairComparison 或 (outcome, code[, detail[, basis]]) 元组
    equivalence_ready: bool = False
    text_proof: str | None = None                # 必须 ∈ EQUIVALENT_CODES 当 outcome=equivalent
    uncovered_independent_relation: bool = False
    issues: tuple[PairIssue, ...] = ()


@dataclass(frozen=True)
class PairResult:
    """对级精判输出。"""
    pair_id: str
    history_record_id: str
    current_record_id: str
    history_item_id: str
    current_item_id: str
    history_arrival_seq: int
    current_arrival_seq: int
    history_raw_hash: str
    current_raw_hash: str
    pipeline_version: str
    outcome: PairOutcome
    code: str
    detail: str
    aligned_facts: tuple[AlignedPair, ...]
    verified_conflicts: tuple[VerifiedConflict, ...]
    unresolved_fields: tuple[str, ...]
    used_evidence: tuple[EvidenceRef, ...]
    budget_at: int = 0


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _normalize(obj: Any) -> Mapping:
    """把 dataclass / 普通对象归一为 dict。"""
    if hasattr(obj, "__dataclass_fields__"):
        return asdict(obj)
    if hasattr(obj, "_asdict"):
        return obj._asdict()
    return dict(obj)


def _check_binding(history: Any, current: Any, pipeline_version: str) -> tuple[Mapping, Mapping]:
    """直接工件身份、原文或版本不匹配即抛 PairBindingError。返回归一化的 (history_dict, current_dict)。"""
    history_dict = history if isinstance(history, Mapping) else _normalize(history)
    current_dict = current if isinstance(current, Mapping) else _normalize(current)
    for required in ("record_id", "item_id", "raw_hash", "scope_id",
                     "business_date", "arrival_seq", "text"):
        if required not in history_dict or required not in current_dict:
            raise PairBindingError(f"missing required field {required!r}")
    if history_dict["record_id"] == current_dict["record_id"]:
        raise PairBindingError("history and current record_id must differ")
    if history_dict["item_id"] == current_dict["item_id"]:
        raise PairBindingError("history and current item_id must differ")
    if history_dict["scope_id"] != current_dict["scope_id"]:
        raise PairBindingError(
            f"scope_id mismatch: {history_dict['scope_id']!r} vs {current_dict['scope_id']!r}"
        )
    if history_dict["business_date"] != current_dict["business_date"]:
        raise PairBindingError(
            f"business_date mismatch: {history_dict['business_date']!r} "
            f"vs {current_dict['business_date']!r}"
        )
    if (not isinstance(history_dict["arrival_seq"], int)
            or not isinstance(current_dict["arrival_seq"], int)):
        raise PairBindingError("arrival_seq must be int")
    if not (0 < history_dict["arrival_seq"] < current_dict["arrival_seq"]):
        raise PairBindingError(
            f"history.arrival_seq ({history_dict['arrival_seq']}) must be < "
            f"current.arrival_seq ({current_dict['arrival_seq']})"
        )
    if _sha256(history_dict["text"]) != history_dict["raw_hash"]:
        raise PairBindingError("history raw_hash does not match sha256(history.text)")
    if _sha256(current_dict["text"]) != current_dict["raw_hash"]:
        raise PairBindingError("current raw_hash does not match sha256(current.text)")
    if history_dict.get("pipeline_version") != pipeline_version:
        raise PairBindingError(
            f"history.pipeline_version {history_dict.get('pipeline_version')!r} "
            f"does not match current pipeline_version {pipeline_version!r}"
        )
    if current_dict.get("pipeline_version") != pipeline_version:
        raise PairBindingError(
            f"current.pipeline_version {current_dict.get('pipeline_version')!r} "
            f"does not match stated pipeline_version {pipeline_version!r}"
        )
    return history_dict, current_dict


def _primary_issue(issues: Iterable[PairIssue]) -> PairIssue:
    items = list(issues)
    if not items:
        raise ComparePairError("issues iterable is empty")
    rank = {code: index for index, code in enumerate(_BOUNDARY_PRIORITY)}
    for issue in items:
        if issue.code not in UNRESOLVED_CODES:
            raise ComparePairError(
                f"issue code {issue.code!r} is not in UNRESOLVED_CODES whitelist"
            )
    return min(items, key=lambda issue: (rank.get(issue.code, len(rank)), issue.detail))


def compare_pair(
    history: Any,
    current: Any,
    *,
    history_artifact: FactValidationReport,
    current_artifact: FactValidationReport,
    alignment: PairAlignmentOutcome,
    pipeline_version: str = "dedup_v1",
    p15_results: P15PairResults | None = None,
    budget_at: int = 0,
) -> PairResult:
    """对级精判：先 VerifiedConflict，再关键未解，最后等价（2026-10-09
    P0-b 修订一：DEDUP_CERT_DECOUPLE 开时有效 EXACT/LOSSLESS 证书在关键
    未解之前直接等价，冲突一票否决保留；关=旧序逐字节）。

    参数：
        history / current：PairRecordContext-like mapping，含
            record_id / item_id / text / raw_hash / scope_id / business_date /
            arrival_seq / pipeline_version。
        history_artifact / current_artifact：双侧 P14 `FactValidationReport`。
        alignment：P16-A `PairAlignmentOutcome`，含 aligned_facts / unresolved_fields。
        pipeline_version：当前比对版本；history / current 必须与之一致。
        p15_results：P15 局部比较产物（数值 + 时间 + 覆盖）。None 视作空集。
        budget_at：预算时点序号（用于 P18 审计峰值时延）。
    """
    history_dict, current_dict = _check_binding(history, current, pipeline_version)

    p15 = p15_results or P15PairResults()
    for issue in p15.issues:
        if issue.code not in UNRESOLVED_CODES:
            raise ComparePairError(
                f"P15 issue code {issue.code!r} is not in UNRESOLVED_CODES"
            )
    for conflict in p15.verified_conflicts:
        if not isinstance(conflict, VerifiedConflict):
            raise ComparePairError("verified_conflicts entries must be VerifiedConflict")
        if conflict.history_evidence.record_id != history_dict["record_id"]:
            raise PairBindingError(
                f"VerifiedConflict history_evidence record_id {conflict.history_evidence.record_id!r} "
                f"does not match history record_id {history_dict['record_id']!r}"
            )
        if conflict.current_evidence.record_id != current_dict["record_id"]:
            raise PairBindingError(
                f"VerifiedConflict current_evidence record_id {conflict.current_evidence.record_id!r} "
                f"does not match current record_id {current_dict['record_id']!r}"
            )
        # W2Fα1（WA1a-D2）：证据 span 类型闸先于切片——外部构造的
        # VerifiedConflict 可携非 int start/end（dataclass 不强制类型），
        # 裸切片逃逸为 TypeError 技术崩溃；归 PairBindingError 声明通道
        # （fail-closed，与 record_id/quote 校验同域）。
        for label, evidence in (("history", conflict.history_evidence),
                                ("current", conflict.current_evidence)):
            if (not isinstance(evidence.start, int)
                    or not isinstance(evidence.end, int)
                    or isinstance(evidence.start, bool)
                    or isinstance(evidence.end, bool)
                    or not isinstance(evidence.quote, str)):
                raise PairBindingError(
                    f"VerifiedConflict {label}_evidence span/quote has invalid "
                    f"types: start={evidence.start!r} end={evidence.end!r}"
                )
        if history_dict["text"][conflict.history_evidence.start:conflict.history_evidence.end] \
                != conflict.history_evidence.quote:
            raise PairBindingError(
                f"VerifiedConflict history_evidence quote {conflict.history_evidence.quote!r} "
                f"does not match history.text[{conflict.history_evidence.start}:{conflict.history_evidence.end}]"
            )
        if current_dict["text"][conflict.current_evidence.start:conflict.current_evidence.end] \
                != conflict.current_evidence.quote:
            raise PairBindingError(
                f"VerifiedConflict current_evidence quote {conflict.current_evidence.quote!r} "
                f"does not match current.text[{conflict.current_evidence.start}:{conflict.current_evidence.end}]"
            )

    aligned_facts = alignment.aligned_facts
    conflicts: list[VerifiedConflict] = list(p15.verified_conflicts)
    issues: list[PairIssue] = list(p15.issues)
    for entry in p15.time_pairs:
        time_pair = TimePairComparison.from_value(entry)
        if time_pair.outcome == "conflict":
            if time_pair.code not in CONFLICT_CODES:
                raise ComparePairError(
                    f"time_pair conflict code {time_pair.code!r} not in CONFLICT_CODES"
                )
            head = aligned_facts[0].history.evidence if aligned_facts else None
            tail = aligned_facts[0].current.evidence if aligned_facts else None
            if (time_pair.history_evidence is not None
                    or time_pair.current_evidence is not None):
                # W2Fα1（WA1a-D4）：实传证据=时间冲突当事对齐对（p15 集成层
                # 按产生该 time_pair 的对齐对写入），取代恒取 aligned_facts[0]
                # 的张冠李戴；单侧实传=形态非法。实传证据入列前经
                # record_id/原文切片绑定校验（fail-closed，与 D2 同域）。
                if (time_pair.history_evidence is None
                        or time_pair.current_evidence is None):
                    raise PairBindingError(
                        "time_pair carried evidence must be both-sided or absent"
                    )
                carried_h = time_pair.history_evidence
                carried_c = time_pair.current_evidence
                if carried_h.record_id != history_dict["record_id"]:
                    raise PairBindingError(
                        f"time_pair history_evidence record_id {carried_h.record_id!r} "
                        f"does not match history record_id {history_dict['record_id']!r}"
                    )
                if carried_c.record_id != current_dict["record_id"]:
                    raise PairBindingError(
                        f"time_pair current_evidence record_id {carried_c.record_id!r} "
                        f"does not match current record_id {current_dict['record_id']!r}"
                    )
                if (not isinstance(carried_h.start, int)
                        or not isinstance(carried_h.end, int)
                        or isinstance(carried_h.start, bool)
                        or isinstance(carried_h.end, bool)
                        or not isinstance(carried_h.quote, str)
                        or not isinstance(carried_c.start, int)
                        or not isinstance(carried_c.end, int)
                        or isinstance(carried_c.start, bool)
                        or isinstance(carried_c.end, bool)
                        or not isinstance(carried_c.quote, str)):
                    raise PairBindingError(
                        "time_pair carried evidence span/quote has invalid types"
                    )
                if history_dict["text"][carried_h.start:carried_h.end] \
                        != carried_h.quote:
                    raise PairBindingError(
                        f"time_pair history_evidence quote {carried_h.quote!r} "
                        f"does not match history.text[{carried_h.start}:{carried_h.end}]"
                    )
                if current_dict["text"][carried_c.start:carried_c.end] \
                        != carried_c.quote:
                    raise PairBindingError(
                        f"time_pair current_evidence quote {carried_c.quote!r} "
                        f"does not match current.text[{carried_c.start}:{carried_c.end}]"
                    )
                head, tail = carried_h, carried_c
            if head is None or tail is None:
                # R2-H3（外部三审，窗口Z1 规格还原）：时间冲突主张无对齐证据
                # 可核发 → 真降级未决（09 §12.1 边界码 TIME_RELATION_UNCERTAIN
                # ∈ UNRESOLVED_CODES）。修复前把 VERIFIED_CONFLICT 装入 issues，
                # 下行 _primary_issue 白名单闸抛 ComparePairError（降级分支
                # 自毁）；本分支只消费白名单内未决码，不崩溃、不签发。
                issues.append(PairIssue(
                    "TIME_RELATION_UNCERTAIN",
                    time_pair.detail or "时间阶段冲突缺少对齐证据"))
            else:
                # W1Fa-2（窗口W1Fα，判定壁，证据保真级）：basis 按 time_pair
                # 实际基底实传（p15 集成层以调 compare_time 的同一 time_basis
                # 写入）；外部旧式构造未携带基底 → 回退默认
                # TIME_SAME_RELATIVE（修复前恒值，现役契约零漂移）。修复前
                # 恒硬编码 TIME_SAME_RELATIVE——非默认 time_basis 下标签与
                # 实际基底不符（p15 白名单允 TIME_SAME_ABSOLUTE/TIME_SAME_STAGE）。
                # 不影响 outcome、不影响 audit basis（=pair.code，R2-H4 在案）。
                basis = time_pair.basis if time_pair.basis is not None else "TIME_SAME_RELATIVE"
                if basis not in _TIME_BASIS_WHITELIST:
                    raise ComparePairError(
                        f"time_pair conflict basis {basis!r} not in TIME basis whitelist"
                    )
                conflicts.append(VerifiedConflict(
                    field_path="time.aligned",
                    basis=basis,
                    history_evidence=head,
                    current_evidence=tail,
                    detail=time_pair.detail or "已验证时间阶段冲突",
                ))
        elif time_pair.outcome == "unresolved":
            code = time_pair.code or "TIME_RELATION_UNCERTAIN"
            if code not in UNRESOLVED_CODES:
                raise ComparePairError(
                    f"time_pair unresolved code {code!r} not in UNRESOLVED_CODES"
                )
            issues.append(PairIssue(code, time_pair.detail or "时间关系未确定"))

    if conflicts:
        outcome: PairOutcome = "conflict"
        code = "VERIFIED_CONFLICT"
        head = conflicts[0]
        detail = f"已验证至少一条充分冲突：{head.field_path}（{head.basis}）。"
    else:
        if p15.uncovered_independent_relation:
            issues.append(PairIssue("RULE_UNCOVERED", "重合事件之外存在未覆盖的独立事件组合。"))
        # 2026-10-09（P0-b 修订一，主窗口修订令 / log\temp\判定链修复方案
        # -P0施工单-呈外部评审.md §一修订稿）：开关 DEDUP_CERT_DECOUPLE 开时
        # 判定序改为 冲突→有效 EXACT/LOSSLESS 证书→直接 equivalent→issues
        # →equivalence_ready——修复前旧序（冲突→issues→equivalence_ready）
        # 让未决 issue 对证书通道一票否决，同文对恒落边界（T 冻结件 30/30
        # 逐字节同文对全边界实证在案）。
        # 冲突保留一票否决（本分支仅在 conflicts 为空时可达）：逐字节同文
        # 在确定性抽取下同文同出，真数值/时间冲突结构性不可能；LOSSLESS
        # 归一仅差布局字符，冲突若出现=抽取发散或数据损坏信号——宁严勿宽，
        # 冲突优先于一切等价主张。
        # 作用域边界：同文签发与正常签发同等窗口——_check_binding :204-212
        # 入口闸已强制同 scope_id+business_date（异窗 PairBindingError，
        # 本分支结构性不可达）；模板/超短文本由 p15_integration 的
        # min_body_length 闸在证书签发前挡（本层不复闸）。
        # 开关关=旧序逐字节（本分支不成立，直下 issues 判定）。
        if (cert_decouple_enabled()
                and p15.equivalence_ready
                and p15.text_proof in _TEXT_CERT_PROOF_CODES):
            outcome = "equivalent"
            code = p15.text_proof
            detail = "主体和核心事件一致，差异属于已允许的表达或信息差异。"
        elif issues:
            primary = _primary_issue(issues)
            outcome = "unresolved"
            code = primary.code
            detail = primary.detail
        elif p15.equivalence_ready:
            proof = p15.text_proof or "FACT_EQUIVALENT"
            if proof not in EQUIVALENT_CODES:
                raise ComparePairError(
                    f"text_proof {proof!r} is not in EQUIVALENT_CODES"
                )
            outcome = "equivalent"
            code = proof
            detail = "主体和核心事件一致，差异属于已允许的表达或信息差异。"
        else:
            outcome = "unresolved"
            code = "FACT_INCOMPLETE"
            detail = "关键主体与事件关系或核心覆盖证据不足。"

    used_evidence: list[EvidenceRef] = []
    for pair in aligned_facts:
        used_evidence.append(pair.history.evidence)
        used_evidence.append(pair.current.evidence)
    for conflict in conflicts:
        used_evidence.append(conflict.history_evidence)
        used_evidence.append(conflict.current_evidence)

    if outcome == "unresolved":
        unresolved_fields = tuple(alignment.unresolved_fields) or (
            "facts:alignment_empty" if not aligned_facts else "facts:coverage_insufficient",
        )
    else:
        unresolved_fields = tuple(alignment.unresolved_fields)

    return PairResult(
        pair_id=f"{history_dict['record_id']}|{current_dict['record_id']}",
        history_record_id=history_dict["record_id"],
        current_record_id=current_dict["record_id"],
        history_item_id=history_dict["item_id"],
        current_item_id=current_dict["item_id"],
        history_arrival_seq=history_dict["arrival_seq"],
        current_arrival_seq=current_dict["arrival_seq"],
        history_raw_hash=history_dict["raw_hash"],
        current_raw_hash=current_dict["raw_hash"],
        pipeline_version=pipeline_version,
        outcome=outcome,
        code=code,
        detail=detail,
        aligned_facts=aligned_facts,
        verified_conflicts=tuple(conflicts),
        unresolved_fields=unresolved_fields,
        used_evidence=tuple(used_evidence),
        budget_at=budget_at,
    )


__all__ = [
    "CONFLICT_CODES",
    "EQUIVALENT_CODES",
    "PairBindingError",
    "ComparePairError",
    "PairIssue",
    "PairOutcome",
    "PairResult",
    "P15PairResults",
    "TimePairComparison",
    "UNRESOLVED_CODES",
    "VerifiedConflict",
    "compare_pair",
]