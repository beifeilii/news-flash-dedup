"""P16-D P15 集成层：从 AlignedPair 抽取数值/时间槽并真实调用 compare_numeric/compare_time。

按 09 §9.2 / 03:28 批复标尺 2：p15_results 由真调用构造，非手工拼装。
未确认对齐的数值/时间槽归 unresolved_fields，不进入 verified_conflicts。
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from typing import Any, Mapping

from news_flash_dedup import text as text_mod
from news_flash_dedup.compare import pair_alignment
from news_flash_dedup.compare import value_time
from news_flash_dedup.compare.pair_alignment import AlignedPair, PairAlignmentOutcome
from news_flash_dedup.compare.pair_compare import (
    P15PairResults,
    PairBindingError,
    PairIssue,
    TimePairComparison,
    VerifiedConflict,
)
from news_flash_dedup.facts import FactValidationReport


@dataclass(frozen=True)
class P15IntegrationReport:
    """P15 集成层结果，包含 VerifiedConflict + 未对齐项。"""
    p15_results: P15PairResults
    used_alignment_pairs: tuple[AlignedPair, ...]
    unresolved_alignment_pairs: tuple[AlignedPair, ...]
    # N24（D28-3 谨慎版）豁免审计字段：实际经白名单填充豁免放行的 leftover
    # 描述（side/fact_id/predicate/twin_fact_id；不删事实、仅不计入覆盖闸）。
    # 空 tuple = 本次未发生豁免放行；豁免评估命中但因其他签发壁条件未签发
    # 时不入账（严格对应"放行"语义，评估痕迹由 fn-trace 探针侧车承担）。
    exempted_filler_facts: tuple[Mapping[str, Any], ...] = ()


def _evidence_from_dict(raw: Mapping[str, Any]) -> value_time.EvidenceRef:
    return value_time.EvidenceRef(
        record_id=str(raw["record_id"]),
        field=str(raw["field"]),
        quote=str(raw["quote"]),
        start=int(raw["start"]),
        end=int(raw["end"]),
    )


def _spec_from_slot(slot: Mapping[str, Any], text: str, record_id: str,
                    expected_field: str,
                    range_end_slot: Mapping[str, Any] | None = None
                    ) -> value_time.NumericSpec:
    """把 P14 slot 转 value_time.NumericSpec。

    N21（D28 批准第 2 件，range 证据核查结论 **(a)**）：facts 工件 numeric
    条目以**兄弟 range_end 槽**携带区间右端 evidence（rule.py L1088-1095 /
    llm.py L227-232 均产出，field 以 .range_end 结尾、真实 span 可核）——
    本层接线透传（facts/rule.py 零改动）。与 value 证据同款纪律：record_id/
    span/quote 原文校验不过 → None → normalize_numeric 判 EVIDENCE_INVALID
    → unresolved（fail-closed 诚实，宁严勿宽）；field 精确等于 expected_field
    派生的 .range_end 路径（与 value 的 expected_field 纪律对称）；
    `range_end ∈ quote` 的语义校验归 normalize_numeric 单一执法
    （W2Fα1-E4 失锚行号指针退役）。
    """
    status = str(slot.get("status", "missing"))
    raw_value = slot.get("raw_value") if isinstance(slot.get("raw_value"), str) else None
    evidence = None
    for entry in slot.get("evidence") or []:
        if not isinstance(entry, Mapping):
            continue
        ref = _evidence_from_dict(entry)
        if ref.record_id != record_id:
            continue
        if not value_time.validate_original_evidence(text, record_id, ref, expected_field):
            continue
        evidence = ref
        break
    range_end = slot.get("range_end") if isinstance(slot.get("range_end"), str) else None
    range_end_evidence = None
    if range_end is not None and isinstance(range_end_slot, Mapping):
        expected_range_field = (
            expected_field[:-len(".value")] + ".range_end"
            if expected_field.endswith(".value") else None
        )
        for entry in range_end_slot.get("evidence") or []:
            if not isinstance(entry, Mapping):
                continue
            ref = _evidence_from_dict(entry)
            if ref.record_id != record_id:
                continue
            if expected_range_field is not None and ref.field != expected_range_field:
                continue
            if not ref.field.endswith(".range_end"):
                continue
            if not value_time.validate_original_evidence(text, record_id, ref, ref.field):
                continue
            range_end_evidence = ref
            break
    return value_time.NumericSpec(
        status=status,
        raw_value=raw_value,
        evidence=evidence,
        unit=str(slot.get("unit", "") or ""),
        magnitude=str(slot.get("magnitude", "") or ""),
        currency=slot.get("currency") if isinstance(slot.get("currency"), str) else None,
        role=slot.get("role") if isinstance(slot.get("role"), str) else None,
        metric=slot.get("metric") if isinstance(slot.get("metric"), str) else None,
        comparator=str(slot.get("comparator", "=") or "="),
        approximate=bool(slot.get("approximate", False)),
        range_end=range_end,
        range_end_evidence=range_end_evidence,
        # N29（D28 裁定选 a，诚实降级）：verified_missing 只认抽取层显式真值，
        # 不再把 status=="missing" 洗成"原文确无"——facts/rule.py 自家 docstring
        # 明确 missing 语义="规则未命中"，≠09 §7.2"正文确实未表达"（抽取未命中
        # ≠原文确无）。规则基线从不产出 verified_missing=True，故规则抽取的
        # missing 在此一律记 unknown：normalize_numeric 判 invalid
        # （FACT_INCOMPLETE，missing 分支纪律）→ compare_numeric 归 unresolved
        # → 进 issues 未决集（宁严勿宽，不再静默兼容/互相洗白；
        # W2Fα1-E4 失锚行号指针退役）。
        verified_missing=bool(slot.get("verified_missing", False)),
    )


def _checked_numeric(spec: value_time.NumericSpec, text: str,
                     record_id: str) -> value_time.CheckedNumeric:
    """对 NumericSpec 调 P15 normalize_numeric；保留缺失值。"""
    return value_time.normalize_numeric(text, record_id, spec)


def _time_spec_from_slot(slot: Mapping[str, Any], text: str,
                         record_id: str,
                         stage_slot: Mapping[str, Any] | None = None,
                         ) -> value_time.TimeSpec:
    """把 P14 time.expression slot 转 value_time.TimeSpec（B1 实装；N29 校正；
    R2-H2 窗口Z1 stage 槽转发规格还原）。

    缺失槽 → verified_missing 只认抽取层显式真值（N29/D28 裁定选 a，诚实
    降级：抽取 missing="规则未命中"，≠"原文确无"——不再无条件洗真）。
    规则基线下 missing 槽一律 verified_missing=False → normalize_time 判
    invalid（FACT_INCOMPLETE）→ compare_time 归 unresolved → 进未决集；
    真 P14 显式标 verified_missing=True 的"确无"槽仍走合法缺失路径。
    present 槽 evidence 经原文校验（与 numeric 同款纪律：校验不过 → None
    → EVIDENCE_INVALID → unresolved，宁严勿宽）。

    stage 槽（同 fact time.stage 兄弟槽，本层经 stage_slot 转发）：规则抽取
    基线**实际产出**（facts/rule.py _time_slots L798-806——盘态词表
    L179-181 开盘/收盘/盘中/早盘/尾盘/盘前/盘后/午间；facts/llm.py
    L369-371 同构产出），09 §8.3"对应时间的阶段不同必为不同时间"的规格
    数据源。旧述"规则抽取基线暂不产出"失实，本窗按 M-06 先例还原转发：
    present 且 raw 为串即携带 stage；stage_evidence 经 expression 同款原文
    校验（record_id 一致 + validate_original_evidence 切片自洽），校验不过
    → stage_evidence=None 而 stage 本体仍携带 → normalize_time 按条件路径
    分派（W1Fα 实述校正，行为零改动；旧述"一律判 EVIDENCE_INVALID"失实，
    现役兜底未述）：stage 词 ∈ 已校验 expression 引文时引文兜底锚定判
    valid（stage 归一照常进入下述 _STAGES 分派）；仅当无锚且 stage 词
    不在引文内才判 EVIDENCE_INVALID → unresolved（与 expression 证据
    纪律同一 fail-closed 口径，不静默丢弃 stage 主张）；归一/冲突分派由
    value_time 现役 _STAGES/_STAGE_CONFLICTS 执法（盘中/早盘等未注册词
    落 None=无信号）。
    词表扩充（同比/环比/初值/终值入 _TIME_STAGE_WORDS）属主窗口后续项，
    本窗只做转发，rule.py 零改动。anchor 槽规则基线暂不产出，接入真 P14
    时按同纪律补齐。
    """
    status = str(slot.get("status", "missing"))
    raw_value = slot.get("raw_value") if isinstance(slot.get("raw_value"), str) else None
    evidence = None
    for entry in slot.get("evidence") or []:
        if not isinstance(entry, Mapping):
            continue
        ref = _evidence_from_dict(entry)
        if ref.record_id != record_id:
            continue
        if not value_time.validate_original_evidence(text, record_id, ref, ref.field):
            continue
        evidence = ref
        break
    stage = None
    stage_evidence = None
    if isinstance(stage_slot, Mapping):
        stage_status = str(stage_slot.get("status", "missing"))
        stage_raw = stage_slot.get("raw_value") if isinstance(
            stage_slot.get("raw_value"), str) else None
        if stage_status == "present" and stage_raw:
            stage = stage_raw
            for entry in stage_slot.get("evidence") or []:
                if not isinstance(entry, Mapping):
                    continue
                ref = _evidence_from_dict(entry)
                if ref.record_id != record_id:
                    continue
                if not value_time.validate_original_evidence(
                        text, record_id, ref, ref.field):
                    continue
                stage_evidence = ref
                break
    return value_time.TimeSpec(
        status=status,
        raw_value=raw_value,
        evidence=evidence,
        # R2-H2（外部三审，窗口Z1）：stage/stage_evidence 如实转发（见 docstring）。
        stage=stage,
        stage_evidence=stage_evidence,
        anchor_evidence=None,
        anchor_relation=None,
        anchor_proof=None,
        # N29（D28 裁定选 a）：见 docstring——missing 不再洗成 verified_missing。
        verified_missing=bool(slot.get("verified_missing", False)),
    )


def _slot_status_raw(fact: Mapping[str, Any], *path: str) -> tuple[str, str | None]:
    """沿 path 取槽位 (status, raw_value)；缺层一律按 missing 处理。"""
    node: Any = fact
    for key in path:
        if not isinstance(node, Mapping):
            return "missing", None
        node = node.get(key) or {}
    if not isinstance(node, Mapping):
        return "missing", None
    status = str(node.get("status", "missing"))
    raw = node.get("raw_value") if isinstance(node.get("raw_value"), str) else None
    return status, raw


def _polarity_normalized_equal(pair: AlignedPair, history_raw: str | None,
                               current_raw: str | None) -> bool:
    """N27（D28-5）：polarity 槽"归一后等"判定——只消费对齐门 Tier-2 已留痕的
    predicate 词典同义证据（AlignedPair.normalization_hits，canonical/entry_id
    钉死）；本门自身不持词典、不做主动归一。

    证据链纪律（与 pair_alignment._pointer_dict_match 锚定规则同构）：predicate
    同义须经对齐层"单槽归一+另一槽逐字锚"执法后落 hit，签发门仅核验该 hit 恰好
    覆盖 polarity 双侧 raw（slot=="predicate" 且 history/current 值精确对得上）；
    无 hit 覆盖（Tier-1 对齐零 hit / subject 槽 hit / hit 前后值与 polarity raw
    不符）一律 False（fail-closed，"回购 vs 未回购"不同案保护不破）。v1 腿
    （dictionary=None）Tier-2 不点火、零 hit，本判定永不放行——行为与旧码
    逐字节一致。
    """
    if history_raw is None or current_raw is None:
        return False
    for hit in pair.normalization_hits:
        if (hit.slot == "predicate"
                and hit.history_raw == history_raw
                and hit.current_raw == current_raw):
            return True
    return False


def _fact_equivalence_provable(
    used_pairs: list[AlignedPair],
    history_facts: dict[str, Any],
    current_facts: dict[str, Any],
    issues: list[PairIssue],
    time_pairs: list[TimePairComparison],
    verified: list[VerifiedConflict],
    history_artifact: FactValidationReport,
    current_artifact: FactValidationReport,
) -> bool:
    """B3 FACT_EQUIVALENT 保守签发（主窗口 22:4x；23:5x 第 i 条加固；N27/D28-5
    条件 g polarity 归一化——证据只吃对齐 Tier-2 留痕，另一槽 modality 保持逐字）。

    09 §11.3"主体/事件明确、全文核心差异已覆盖"的代码化代理（宁严勿宽，
    任一存疑 → 不签发 → 走边界）：
      a. ≥1 对齐对（主体/谓词逐字等由 pair_alignment 对齐门执法，此处蕴含）；
      b. 双侧工件抽取均 complete（09 §11.3"partial 工件仍可提供独立冲突"的
         逆命题：等价签发要求完整抽取，partial 只供冲突不供等价）；
      c. 双侧 Fact 全对齐、无 leftover（"全文核心差异已覆盖"的覆盖代理）；
         N24（D28-3 谨慎版）白名单填充豁免——leftover 全部命中类① 言语包裹
         闭表条件（_filler_exempted_leftovers）→ 不计入本闸，独立事件/实质
         附加照拒；
      d. issues 空（数值未对齐/FACT_INCOMPLETE 任一即拒）；
      e. time_pairs 空（时间 conflict/unresolved 任一即拒；compatible 不入列）；
      f. 每对齐对 numerics 数量相等（防 zip 截断静默吞单侧数值差异）；
      g. polarity 双侧 present 且（逐字等 或 N27/D28-5：对齐 Tier-2 predicate
         归一 hit 恰好覆盖双侧 raw——"回购" vs "未回购"无 hit 覆盖必须不同案）；
      h. modality 双缺失或双侧 present 逐字等（"拟回购" vs 无模态不对称即拒；
         N27 另一槽保持逐字——双槽同归一照拒锚规则不破，fp 结构性保证）；
      i. verified 空（23:5x 加固：已验证冲突与 equivalence_ready 互斥——
         决策层冲突虽先行，P15 结果内部一致性不容"带冲突的等价就绪"）。
    """
    if not used_pairs or issues or time_pairs or verified:
        return False
    if not (_artifact_qualified(history_artifact)
            and _artifact_qualified(current_artifact)):
        return False
    if len(used_pairs) != len(history_facts) or len(used_pairs) != len(current_facts):
        # N24（D28-3 谨慎版，窗口Q）白名单填充豁免：leftover 全部命中豁免类
        # （类① 言语包裹闭表 + 结构特征 + D21 守卫，定义与逐类实测见
        # _filler_exempted_leftovers 文档串及 log/N24套话豁免实施报告.md）
        # → 不计入覆盖闸（事实不删、审计字段照记）；独立事件（09 L846
        # 附属/独立分界）与实质附加（N24B-SUBST 红线）不在豁免范围，照拒。
        if _filler_exempted_leftovers(used_pairs, history_facts, current_facts) is None:
            return False
    for pair in used_pairs:
        history_fact = history_facts.get(pair.history.fact_id)
        current_fact = current_facts.get(pair.current.fact_id)
        if history_fact is None or current_fact is None:
            return False
        if len(history_fact.get("numerics", [])) != len(current_fact.get("numerics", [])):
            return False
        p_status, p_raw = _slot_status_raw(history_fact, "event_state", "polarity")
        q_status, q_raw = _slot_status_raw(current_fact, "event_state", "polarity")
        if p_status != "present" or q_status != "present":
            return False
        if p_raw != q_raw and not _polarity_normalized_equal(pair, p_raw, q_raw):
            return False
        m_status, m_raw = _slot_status_raw(history_fact, "event_state", "modality")
        n_status, n_raw = _slot_status_raw(current_fact, "event_state", "modality")
        if m_status == "present" and n_status == "present":
            if m_raw != n_raw:
                return False
        elif m_status == "present" or n_status == "present":
            return False
    return True


def _artifact_qualified(artifact: FactValidationReport) -> bool:
    """certify_text_equality 合格门（B1 口径）。

    其 docstring 要求合格覆盖主体/事件/事实数值完整性/模态/内部一致性/原文
    Evidence。对 EXACT/LOSSLESS 文本证书路径，两侧语义内容逐字（或无损）同一，
    合格门退化为工件完整性：抽取完成且校验完整（validated_complete）。
    释义级 FACT_EQUIVALENT 不走本门（需真 P14 覆盖证明，见 extract_p15_results
    尾部注记）。
    """
    return bool(artifact.validated_complete
                and artifact.extraction_status == "complete")


# ---------- 2026-10-09（P0 收口包二②，主窗口派包）：剥信源壳判据 ----------
#
# 出处：判定链改造技术设计书-v2-定稿决议-外部评审-1009.md §二（同文对定案：
# "剥信源壳+残文≥15 字（或裸长≥20）判据下 26 对可签、4 对进人工，完美二分；
# min_len=50 将误杀 19 对真重复……照此施工"）+ v2 §4.1（"正文剥信源壳的
# 规则须版本化、能回指原文；剥后为空……时返回未决；长度可作为候选警报
# 而非唯一签发条件"）。
#
# 开关 DEDUP_EXACT_SHELL_STRIP（默认关=现役逐字节零效应）：开且
# DEDUP_CERT_DECOUPLE 同开时，证书路最小长度闸改按剥壳判据求值——双侧
# 归一文各剥版本化信源尾注壳，残文≥15 字或裸长≥20 字 → 长度闸放行
# （解耦路签发）；判据不过（空壳/超短）→ 维持 DEDUP_EXACT_MIN_LEN 闸
# （默认 50，不足回落老路双合格闸，空壳自然落未决）。开关关或
# DEDUP_CERT_DECOUPLE 关 → 本判据零调用零效应（现役逐字节）。
SHELL_STRIP_ENV = "DEDUP_EXACT_SHELL_STRIP"
# 规则版本化（v2 §4.1）：v1 尾注闭表=T 冻结 30 对实测唯一样式"（产联社）"
# （定稿决议 §二"裸信源尾注，无事实载体"；P0 验收一 26 组全带同一 5 字
# 尾注实测）。闭表只按实测扩充，任何扩员/变形必升 SHELL_STRIP_RULE_VERSION。
SHELL_STRIP_RULE_VERSION = "source-shell-v1-2026-10-09"
SHELL_STRIP_RESIDUAL_MIN = 15
SHELL_STRIP_BARE_MIN = 20
_SOURCE_SHELL_TAILS_V1 = ("（产联社）",)


def shell_strip_enabled(env: Mapping[str, str] | None = None) -> bool:
    """开关解析：精确 "1"=开；缺省/空串/其余一律关（fail-closed 默认关，
    "true"/大小写变体等不放大——cert_decouple_enabled 同型纪律）。"""
    source = os.environ if env is None else env
    return source.get(SHELL_STRIP_ENV, "") == "1"


def strip_source_shell(normalized_text: str) -> str:
    """剥版本化信源尾注壳（SHELL_STRIP_RULE_VERSION 现行版）。

    能回指原文（v2 §4.1）：确定性尾匹配剥除，剥除串恒为原文真后缀、
    残文恒为原文真前缀，任何人可按版本规则复算；归一文尾无闭表壳 →
    原文一字不动（不猜、不模糊匹配）。v1 至多剥一层尾注。
    """
    if not isinstance(normalized_text, str):
        raise TypeError("normalized_text must be str")
    for tail in _SOURCE_SHELL_TAILS_V1:
        if normalized_text.endswith(tail):
            return normalized_text[: len(normalized_text) - len(tail)]
    return normalized_text


def _shell_criterion_pass(normalized_text: str) -> bool:
    """剥壳判据（定稿决议 §二原文）：剥信源壳后残文≥15 字、或裸长≥20 字。"""
    if len(normalized_text) >= SHELL_STRIP_BARE_MIN:
        return True
    return len(strip_source_shell(normalized_text)) >= SHELL_STRIP_RESIDUAL_MIN


def _relative_time_anchor_blocked(left_text: str, right_text: str) -> bool:
    """相对时间无锚闸（宪章 C14/T-3 生效口径）：消费判官既有件
    decide.machine_verify.machine_relative_time_anchor_check——同语义同词表
    （字面值表 + 2026-10-10 policy_v2-C14 追认的"过去N小时/N天/N周"短语族），
    别新造。block=True ⇔ 任一侧含相对词族且双侧无共同绝对锚点。

    延迟导入：decide/__init__ 包初始化即拉 service→compare 本模块，静态
    反向边成循环初始化；函数内导入只在真调用时解析，零初始化期边。
    """
    from news_flash_dedup.decide import machine_verify as _mv
    return bool(_mv.machine_relative_time_anchor_check(left_text, right_text)["block"])


# ---------- N24（D28-3 谨慎版，窗口Q）白名单填充豁免 ----------
#
# 裁定：用户 D28-3（谨慎版=逐类实测+fp 闸）+ 窗口I 一致性裁决 N24=一致
# （双锚：09 §7.6-7"已确认背景/附属关系" + L846"附属/独立关系"分界）。
# 逐类测量与 fp 实测见 log/N24套话豁免实施报告.md（v2 唯一干净命中
# 78c71d5e；tn 51 集三腿零覆盖闸唯一阻断 → fp 结构面为零）。
#
# 类① 宣布/公告/披露/发布 言语包裹（本轮唯一实施类；类② 板块总述/来源括号/
# 回顾修饰经实测在当前诚实基线全部被诚实时间未知共阻断、零翻转收益，
# 本轮不实施，见报告 §二）：leftover fact 命中下列全部闭表条件 → 不计入
# 覆盖闸（事实不删、审计字段照记）；独立事件（independent relation，
# 09 L846 分界）与实质附加（N24B-SUBST 红线）不在豁免范围。
_FILLER_WRAPPER_PREDICATES = frozenset({"宣布", "公告", "披露", "发布"})

# D21 锚规则同纪（谓词-词典条目不豁免 → N27 已定豁免范围外行为）：
# 闭表内 宣布/发布 同属 norm_dict_v1 pred-0002 同义类——对侧存在未对齐
# 的同义类 fact 时，覆盖缺口归词典对齐通道（anchored-not-yet-fired，
# N27 领地，如 389b26fb 双主体缺位案），本豁免一律不签发。
_WRAPPER_DICT_SYNONYM_CLASS = frozenset({"发布", "宣布"})


def _fact_primary_span(fact: Mapping[str, Any]) -> tuple[int, int] | None:
    """fact 主 evidence 的 (start, end) span；缺/坏 → None（fail-closed）。"""
    for entry in fact.get("evidence") or []:
        if not isinstance(entry, Mapping):
            continue
        try:
            return (int(entry["start"]), int(entry["end"]))
        except (KeyError, TypeError, ValueError):
            continue
    return None


def _wrapper_filler_twin(fact: Mapping[str, Any],
                         same_side_used: tuple[tuple[str, Mapping[str, Any]], ...],
                         opposite_leftover_predicates: tuple[str, ...],
                         ) -> str | None:
    """类① 言语包裹判定：命中全部闭表条件 → 返回共指已对齐孪生 fact_id，否则 None。

    g1 谓词 present 且 raw ∈ {宣布, 公告, 披露, 发布}（闭表，逐字等值，无开放匹配）；
    g2 numerics 为零（包裹不得携带新数值——d4107222"34观摩国/4组织"、
       447226bd"收益率29.331%"型实质数据拦截，N24B-SUBST 红线）；
    g3 modality 非 present（包裹不得携带模态承诺）；
    g4 D21 守卫：谓词属 pred-0002（发布/宣布）时，对侧不得存在未对齐的
       同义类 fact（防抢占 N27 词典对齐通道）；
    g5 已确认附属关系双锚（09 §7.6-7）：主 evidence span 等于同侧某已对齐
       fact 的主 evidence span（同句双重抽取实证；span 不共指即独立事件，
       L846 分界，不豁免）；
    g6 主体同值：subject 与该已对齐 fact 均 present 且逐字相等。
    """
    p_status, p_raw = _slot_status_raw(fact, "event_state", "predicate")
    if p_status != "present" or p_raw not in _FILLER_WRAPPER_PREDICATES:
        return None                                              # g1
    if len(fact.get("numerics") or []) != 0:
        return None                                              # g2
    m_status, _ = _slot_status_raw(fact, "event_state", "modality")
    if m_status == "present":
        return None                                              # g3
    if p_raw in _WRAPPER_DICT_SYNONYM_CLASS and any(
            raw in _WRAPPER_DICT_SYNONYM_CLASS
            for raw in opposite_leftover_predicates):
        return None                                              # g4
    span = _fact_primary_span(fact)
    if span is None:
        return None                                              # g5 前置
    f_status, f_raw = _slot_status_raw(fact, "subject")
    if f_status != "present":
        return None                                              # g6 前置
    for twin_id, twin in same_side_used:
        if _fact_primary_span(twin) != span:
            continue                                             # g5
        t_status, t_raw = _slot_status_raw(twin, "subject")
        if t_status == "present" and t_raw == f_raw:
            return twin_id                                       # g6
    return None


def _filler_exempted_leftovers(used_pairs, history_facts, current_facts):
    """N24 覆盖闸豁免评估：全部 leftover 命中类① → 豁免描述 tuple；否则 None。

    无 leftover → 空 tuple（覆盖本即满足，无豁免发生）。任一 leftover 不命中
    （含独立事件/实质附加/D21 同义类竞争/闭表外谓词）→ None（fail-closed，
    覆盖闸维持拒签）。描述字段仅审计用途：side/fact_id/predicate/twin_fact_id。
    """
    used_h = tuple((pair.history.fact_id, history_facts[pair.history.fact_id])
                   for pair in used_pairs if pair.history.fact_id in history_facts)
    used_c = tuple((pair.current.fact_id, current_facts[pair.current.fact_id])
                   for pair in used_pairs if pair.current.fact_id in current_facts)
    used_h_ids = {fid for fid, _ in used_h}
    used_c_ids = {fid for fid, _ in used_c}
    leftover_h = tuple(fid for fid in history_facts if fid not in used_h_ids)
    leftover_c = tuple(fid for fid in current_facts if fid not in used_c_ids)
    if not leftover_h and not leftover_c:
        return ()

    def _leftover_predicates(facts, ids):
        raws = []
        for fid in ids:
            status, raw = _slot_status_raw(facts[fid], "event_state", "predicate")
            if status == "present":
                raws.append(raw)
        return tuple(raws)

    opp_for_h = _leftover_predicates(current_facts, leftover_c)
    opp_for_c = _leftover_predicates(history_facts, leftover_h)
    exempted = []
    for fid in leftover_h:
        twin = _wrapper_filler_twin(history_facts[fid], used_h, opp_for_h)
        if twin is None:
            return None
        status, raw = _slot_status_raw(history_facts[fid], "event_state", "predicate")
        exempted.append({"side": "history", "fact_id": fid,
                         "predicate": raw, "twin_fact_id": twin})
    for fid in leftover_c:
        twin = _wrapper_filler_twin(current_facts[fid], used_c, opp_for_c)
        if twin is None:
            return None
        status, raw = _slot_status_raw(current_facts[fid], "event_state", "predicate")
        exempted.append({"side": "current", "fact_id": fid,
                         "predicate": raw, "twin_fact_id": twin})
    return tuple(exempted)


def extract_p15_results(
    history_artifact: FactValidationReport,
    current_artifact: FactValidationReport,
    history_text: str,
    current_text: str,
    alignment: PairAlignmentOutcome,
    *,
    pipeline_version: str = "dedup_v1",
    numeric_role_basis: str = "NUMERIC_SAME_DECIMAL",
    time_basis: str = "TIME_SAME_RELATIVE",
) -> P15IntegrationReport:
    """对每条 AlignedPair 抽取数值/时间槽并真实调 P15。

    数值：仅当双侧 status=present 且 value 均可解析（Decimal 或显式中文数词），
    且双方 raw_value 来自同一 metric/role/unit 字符串相等（角色同字符串相等即对齐），
    且 evidence 通过 P14/P15 校验 → 调 compare_numeric，未对齐/无法对齐 → PairIssue。

    时间（B1 实装，主窗口 22:1x）：真调 P15 `compare_time`——时间槽经
    normalize_time 归一（合法缺失 compatible；非法/无法归一 unresolved；
    实质冲突 conflict）。09 §11.3 阻塞语义由 compare_time 自身执法，不再
    由本层无条件占位。

    等价证书（B1 实装）：尾部经 P09 `certify_text_equality` 颁发
    EXACT_TEXT_MATCH / LOSSLESS_TEXT_MATCH（代码可验证的原文/无损相等证书，
    09 §11.3 text_proof 唯二来源之一）；FACT_EQUIVALENT 释义级证书需真 P14
    覆盖证明，本层不颁发。
    """
    if numeric_role_basis not in {"NUMERIC_SAME_DECIMAL", "EXACT_TEXT_MATCH"}:
        raise PairBindingError(
            f"numeric_role_basis {numeric_role_basis!r} is not in whitelist"
        )
    if time_basis not in {"TIME_SAME_RELATIVE", "TIME_SAME_ABSOLUTE",
                            "TIME_SAME_STAGE"}:
        raise PairBindingError(
            f"time_basis {time_basis!r} is not in whitelist"
        )

    verified: list[VerifiedConflict] = []
    issues: list[PairIssue] = []
    used_pairs: list[AlignedPair] = []
    time_pairs: list[TimePairComparison] = []

    history_facts = {f.get("fact_id"): f for f in (history_artifact.artifact or {}).get("facts", [])}
    current_facts = {f.get("fact_id"): f for f in (current_artifact.artifact or {}).get("facts", [])}

    for pair in alignment.aligned_facts:
        history_fact = history_facts.get(pair.history.fact_id)
        current_fact = current_facts.get(pair.current.fact_id)
        if history_fact is None or current_fact is None:
            issues.append(PairIssue("FACT_INCOMPLETE",
                                    f"aligned pair {pair.history.fact_id} missing fact"))
            continue

        h_numerics = history_fact.get("numerics", [])
        c_numerics = current_fact.get("numerics", [])
        if len(h_numerics) != len(c_numerics):
            # W2Fα1（WA1a-D3）：zip 截断吞差异——双侧 numerics 数量不等时
            # 短侧截断、长尾条目零留痕（静默吞差异）；记 FACT_INCOMPLETE
            # issue（fail-closed 未决向，不冒充可比），公共前缀仍按序对比较。
            issues.append(PairIssue(
                "FACT_INCOMPLETE",
                f"aligned pair {pair.history.fact_id}/{pair.current.fact_id} "
                f"numerics 数量不等（{len(h_numerics)} vs {len(c_numerics)}），"
                f"长尾条目未参与对比较"))
        for n_hist, n_curr in zip(h_numerics, c_numerics):
            slot_h = n_hist.get("value", {})
            slot_c = n_curr.get("value", {})
            spec_h = _spec_from_slot(slot_h, history_text, history_artifact.record_id,
                                      f"facts.{pair.history.fact_id}.numerics.{n_hist.get('numeric_id', 'n')}.value",
                                      range_end_slot=n_hist.get("range_end"))
            spec_c = _spec_from_slot(slot_c, current_text, current_artifact.record_id,
                                      f"facts.{pair.current.fact_id}.numerics.{n_curr.get('numeric_id', 'n')}.value",
                                      range_end_slot=n_curr.get("range_end"))
            checked_h = _checked_numeric(spec_h, history_text, history_artifact.record_id)
            checked_c = _checked_numeric(spec_c, current_text, current_artifact.record_id)

            alignment_proof = value_time.AlignmentProof(
                confirmed=True,
                basis=f"{numeric_role_basis}:{pair.history.fact_id}/{pair.current.fact_id}",
            )
            comparison = value_time.compare_numeric(
                checked_h, checked_c, alignment=alignment_proof,
            )
            if comparison.status == "verified_conflict":
                verified.append(VerifiedConflict(
                    field_path=f"facts.{pair.history.fact_id}.numerics.{n_hist.get('numeric_id', 'n')}.value",
                    basis=numeric_role_basis,
                    history_evidence=spec_h.evidence,
                    current_evidence=spec_c.evidence,
                    detail=f"{spec_h.raw_value!r} vs {spec_c.raw_value!r}",
                ))
            elif comparison.status == "unresolved":
                issues.append(PairIssue(comparison.reason_code or "NUMERIC_ALIGNMENT_FAILED",
                                        f"数值未对齐：{comparison.status}"))
            elif comparison.status in ("missing_in_history", "missing_in_new"):
                # D31（用户裁定②路径A；窗口I《方案一致性核验-09.md》N29 局部段
                # 冲突裁决消解）：verified 单侧数值缺失 → compatible，**不发
                # issue**——09 §10.2"A100/B缺值/C101：B正文确未给价格……A-B与
                # B-C可以重复"+ T10 预期"重复，FACT_EQUIVALENT（缺值不是模型
                # 漏抽）"+ §12.2 L1340"真实时间或数值缺失……不自动边界"。
                # compare_numeric 仅双侧 valid 才可达 missing_in_*，而 missing
                # 槽 valid 当且仅当抽取层显式 verified_missing=True
                # （value_time.normalize_numeric missing 分支；W2Fα1-E4 失锚
                # 行号指针退役）——本分支只消费
                # verified 缺失；unverified missing（抽取未命中≠原文确无，N29
                # 主体不动）由上方 unresolved 分支维持 FACT_INCOMPLETE 具名
                # issue。双侧确无（not_applicable）无可比对象，不产信号
                # （09 §11.3 本义）。不得判 conflict（无证据差异可言）。
                pass

        # 时间（B1 实装，主窗口 22:1x）：真调 compare_time——时间槽由 P14
        # time.expression 构造 TimeSpec；双侧合法缺失 → compatible（09 §11.3
        # "允许合法数值/时间缺失"），不再无条件追加 TIME_RELATION_UNCERTAIN 占位。
        # R2-H2（窗口Z1）：同 fact time.stage 兄弟槽随 expression 一并转发
        # （规则基线实际产出，09 §8.3 阶段规格数据源；转发纪律见
        # _time_spec_from_slot docstring）。
        history_time_slot = (history_fact.get("time") or {}).get("expression") or {}
        current_time_slot = (current_fact.get("time") or {}).get("expression") or {}
        history_stage_slot = (history_fact.get("time") or {}).get("stage") or {}
        current_stage_slot = (current_fact.get("time") or {}).get("stage") or {}
        time_alignment = value_time.AlignmentProof(
            confirmed=True,
            basis=f"{time_basis}:{pair.history.fact_id}/{pair.current.fact_id}",
        )
        time_comparison = value_time.compare_time(
            value_time.normalize_time(
                history_text, history_artifact.record_id,
                _time_spec_from_slot(history_time_slot, history_text,
                                     history_artifact.record_id,
                                     stage_slot=history_stage_slot)),
            value_time.normalize_time(
                current_text, current_artifact.record_id,
                _time_spec_from_slot(current_time_slot, current_text,
                                     current_artifact.record_id,
                                     stage_slot=current_stage_slot)),
            alignment=time_alignment,
        )
        if time_comparison.relation == "conflict":
            time_pairs.append(TimePairComparison(
                "conflict", time_comparison.reason_code or "VERIFIED_CONFLICT",
                "已验证时间冲突",
                # W1Fa-2（窗口W1Fα，判定壁）：实际基底实传——与上方
                # AlignmentProof 内嵌基底同源（本层调 compare_time 的
                # time_basis 即本次比较真实基底；compare_time 返回
                # TimeComparison 仅 relation/reason_code，结构上不携基底
                # 字段），供 compare_pair 入列 VerifiedConflict 时按实标注
                # （修复前下游恒洗 TIME_SAME_RELATIVE）。
                basis=time_basis,
                # W2Fα1（WA1a-D4）：当事对齐对证据实传——本 time_pair 由
                # 当前 pair 的 compare_time 产生，证据即该对双侧 evidence
                # （修复前 compare_pair 恒取 aligned_facts[0] 张冠李戴）。
                history_evidence=pair.history.evidence,
                current_evidence=pair.current.evidence,
            ))
        elif time_comparison.relation == "unresolved":
            time_pairs.append(TimePairComparison(
                "unresolved",
                time_comparison.reason_code or "TIME_RELATION_UNCERTAIN",
                "时间关系未确定",
            ))
        used_pairs.append(pair)

    # 等价证书（B1 实装，主窗口 22:1x）：09 §11.3 "text_proof 只接受代码验证的
    # 原文/无损相等证书"——certify_text_equality（P09 已封原语）即该证书。
    # FACT_EQUIVALENT（释义级）不在此颁发：需真 P14 全文核心差异覆盖证明，
    # 仍归后续模型迭代（本层只发 EXACT/LOSSLESS 两种代码可验证证书）。
    # 2026-10-09（P0-b，log\temp\判定链修复方案-P0施工单-呈外部评审.md
    # §一-P0-b）：DEDUP_CERT_DECOUPLE 开关默认关=旧路逐字节（双侧工件
    # validated_complete 合格门先行）；开=证书通道与完备性闸解耦（文本相等
    # 本身是判定依据）+ DEDUP_EXACT_MIN_LEN 最小正文长度闸（默认 50 字符，
    # 不足长度回落旧路）。FACT_EQUIVALENT 通道完备性要求一字不动（下方
    # _fact_equivalence_provable 及其 _artifact_qualified 调用面零改动）。
    if text_mod.cert_decouple_enabled():
        left_norm = text_mod.normalize_text(history_text)
        right_norm = text_mod.normalize_text(current_text)
        min_body = text_mod.exact_min_len()
        if (shell_strip_enabled()
                and _shell_criterion_pass(left_norm.normalized_text)
                and _shell_criterion_pass(right_norm.normalized_text)):
            # P0 收口包二②：剥壳判据双侧过 → 长度闸放行（min_body_length=0
            # =解耦路无长度闸）；判据不过维持 DEDUP_EXACT_MIN_LEN 闸——
            # 不过者裸长必 <20<50，默认闸必回落老路（空壳自然落未决，
            # 与"判据不过仍走老路"语义合一，不另辟第三通道）。
            min_body = 0
        certificate = text_mod.certify_text_equality(
            left_norm,
            right_norm,
            left_qualified=_artifact_qualified(history_artifact),
            right_qualified=_artifact_qualified(current_artifact),
            decouple_qualification=True,
            min_body_length=min_body,
        )
    else:
        certificate = text_mod.certify_text_equality(
            text_mod.normalize_text(history_text),
            text_mod.normalize_text(current_text),
            left_qualified=_artifact_qualified(history_artifact),
            right_qualified=_artifact_qualified(current_artifact),
        )
    if certificate is not None and _relative_time_anchor_blocked(
            history_text, current_text):
        # 2026-10-10（P0 收口包二③，主窗口裁定=选B）：宪章 C14/T-3 文本自证
        # 闸——相对时间词族在场且双侧无共同绝对锚 → 即使原文/无损相等，证书
        # 亦撤回、对落未决转边界（"即使两条正文完全相同也先出边界"生效宪法在
        # 证书路的落实；与判官路 machine_relative_time_anchor_check 同语义同
        # 词表，含 policy_v2-C14 追认的"过去N小时/N天/N周"短语族）。不分发布
        # 日：异 business_date 对被 pair_compare._check_binding:221 拦在证书
        # 路外，"同日可签"腿已由主窗口明文作废。具名 issue 留痕
        # （TIME_RELATION_UNCERTAIN=注册未决码，fail-closed 不静默吞因）。
        certificate = None
        issues.append(PairIssue(
            "TIME_RELATION_UNCERTAIN",
            "相对时间词族在场且双侧无共同绝对锚（宪章 C14/T-3：即使同文亦不直签，"
            "判官路 machine_relative_time_anchor_check 同语义）"))
    if certificate is not None:
        # 文本证书路径（23:4x 校准，fp 3139b94f 复盘）：不再要求 used_pairs——
        # EXACT/LOSSLESS 是字节级（或无损变换级）相等，确定性抽取下双侧 facts
        # 必然同一，对齐对不提供额外保证；证书合格门（_artifact_qualified）
        # 仍在 certify 调用内执法。used_pairs 要求仅保留给 FACT_EQUIVALENT。
        # （2026-10-09 P0-b 补注：合格门执法仅限旧路——DEDUP_CERT_DECOUPLE
        # 开时按上方分流解耦，长度闸不足仍回落本合格门，见 :620-625。）
        equivalence_ready = True
        text_proof = certificate.kind.value
    elif _fact_equivalence_provable(used_pairs, history_facts, current_facts,
                                    issues, time_pairs, verified,
                                    history_artifact, current_artifact):
        # B3 实装（主窗口 22:4x）：释义级 FACT_EQUIVALENT 保守签发。
        equivalence_ready = True
        text_proof = "FACT_EQUIVALENT"
    else:
        equivalence_ready = False
        text_proof = None
    # N24（D28-3 谨慎版）豁免审计：仅当覆盖闸确经白名单填充豁免放行并签发
    # FACT_EQUIVALENT 时入账（严格对应"放行"语义；豁免评估命中但被其他签发
    # 壁条件拦回不入账，评估痕迹由 fn-trace 探针侧车承担）。事实不删——
    # extra_event（decide 层）uncovered 记录与对齐 unresolved_fields 原样保留。
    exempted_filler = _filler_exempted_leftovers(
        used_pairs, history_facts, current_facts) or ()
    p15 = P15PairResults(
        verified_conflicts=tuple(verified),
        time_pairs=tuple(time_pairs),
        equivalence_ready=equivalence_ready,
        text_proof=text_proof,
        uncovered_independent_relation=False,
        issues=tuple(issues),
    )
    return P15IntegrationReport(
        p15_results=p15,
        used_alignment_pairs=tuple(used_pairs),
        unresolved_alignment_pairs=tuple(p for p in alignment.aligned_facts if p not in used_pairs),
        exempted_filler_facts=(
            exempted_filler
            if (equivalence_ready and text_proof == "FACT_EQUIVALENT")
            else ()
        ),
    )


__all__ = [
    "P15IntegrationReport",
    "SHELL_STRIP_BARE_MIN",
    "SHELL_STRIP_ENV",
    "SHELL_STRIP_RESIDUAL_MIN",
    "SHELL_STRIP_RULE_VERSION",
    "extract_p15_results",
    "shell_strip_enabled",
    "strip_source_shell",
]