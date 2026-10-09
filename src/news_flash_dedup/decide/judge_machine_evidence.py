"""v6-lite 一期机器候选证据层（2026-10-11，分支 p3-v6-phase1）。

出处：产品批准的一期范围（就两条规则，不得扩 scope）——

规则一（R8 修订改值）机器层：复用 compare/core_conflict.py **退役区**
正则 _REVISION_RE（终审 P0-4 摘除后保留在案）产"修订候选证据"——
结构化（模式命中位置/表面形/前值/后值/归一值），经 judge_pair.
build_pair_context 注入判官上下文（context["machine_evidence"]）。
机器**绝不直接判**（退役层不得复活硬判）：只作候选证据供判官终审；
判官按 judge_v6 R8 条款裁决（修订改值→不重复；同指向同一修订值省略
过程→重复；只补背景有效值相同→重复；角色/指标/时间对不上→存疑）。

规则二（R7 主体单方缺失受约束回填）机器层：条件①机器前置硬闸——
"有且仅有一方缺主体且另一方明确写了主体"的机检可判域判定，判据=
证券代码（machine_verify.subject_code_mentions，6 位代码）∪主体抽取
现有件（facts 主体槽 status=present 的 raw_value）：
- 恰一方明确（代码或主体名在场）→ unilateral_missing=True（回填条款
  可触发；判官按 v6 条款②-⑤终审，条件⑥强制存疑兜底）；
- 双方都缺主体 → both_missing=True，回填**不得触发**（判存疑落边界）；
- 双方都写主体 → 硬闸不置位（不同主体→不重复为现状路径；同名→按
  一般规则裁决）。

主窗补充令（2026-10-11，R7 条件③时间维度三分支）机器层：time_
dimension_gate——机检可判域=日期型（machine_verify.extract_time_
mentions 闭形正则）∪阶段词（extract_stage_set 闭词表）∪相对时间词
（relative_time_tokens_in 闭词表）∪facts 时间槽现有件（time.
expression present），输出双侧表面形+在场布尔+三分支态：
- both_present（条款③a：双方都有时间/阶段——是否一致由判官裁决，
  取值不同→不重复不经回填规则）；
- unilateral_missing（条款③b：一方有一方无=单方信息补充）；
- both_missing（条款③c：双方都无时间/阶段——判官按硬门槛裁决：
  仅单一数值一致强制存疑转边界，防无时间锚的同口径跨期撞稿）。
机器只观测不判：time_state 喂 shadow 计数 no_time 单列标签与判官
上下文证据（闭形域外的表述由判官按正文语义自行评估——机检无判据
绝不冒充判据）。

shadow 指标（decide/service.py 现状机制 _jcount+DecideOutcome.
judge_diagnostics，仅 semantic 模式计数）：
- judge.backfill.triggered：条件①硬闸通过的对数（判官循环内）；
- judge.backfill.signed：triggered 且判官双序一致判"重复"（回填完成
  签发）的对数；
- judge.backfill.vetoed：triggered 但未签发（条件②-⑥任一不满足转
  边界，或判官另判）的对数；
- 补充令增量：c) 情形（双方都无时间/阶段）单列可区分——上三计数
  各带 .no_time 后缀孪生计数（judge.backfill.triggered.no_time/
  signed.no_time/vetoed.no_time，标签带时间态；基础三计数仍聚合）。

纪律：
- 本模块只产**纯 JSON 证据**（可入 pair_context / 审计面），不判、
  不降级、不改公共五字段；机器绝不直接判是铁律（fp 风险全集中在
  R7 签发侧——三防线：①本层硬闸+②-⑤判官条款+⑥强制存疑）；
- 仅 semantic_authority 模式由 service 装配（legacy 面零调用，默认
  生产行为与基线 2d0d418 全等）；
- 泛指占位（"公司股票"/"该公司"）不在机检可判域——主体抽取现有件
  主体槽 present 才算"明确写了主体"；机检无判据时留判官域。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from news_flash_dedup.compare import core_conflict as _cc
from news_flash_dedup.decide import machine_verify as _mv

MACHINE_EVIDENCE_VERSION = "v6_lite_phase1b"

# 判官上下文注入键（build_pair_context 消费；缓存键/证明/审计面不读它
# ——内容寻址键不受影响，legacy 面 None=缺席零字段）
CONTEXT_KEY = "machine_evidence"


# ---------------------------------------------------------------- R8 修订候选证据

def revision_candidates(text: str) -> tuple[dict, ...]:
    """单侧修订候选证据：_REVISION_RE（core_conflict 退役区正则）全量
    命中，逐条结构化（模式命中位置 start/end、表面形 surface、前值
    before、后值 after、归一值 before_norm/after_norm——归一 None=
    该值不可归一，仍保留候选）。

    机器不判：只产候选证据（判官终审按 v6 R8 条款）。位置口径=Python
    str 切片（与 core_conflict._ref 同款）。
    """
    out: list[dict] = []
    for m in _cc._REVISION_RE.finditer(text):
        before, after = m.group(1), m.group(2)
        out.append({
            "start": m.start(), "end": m.end(),
            "surface": m.group(0),
            "before": before, "after": after,
            "before_norm": _mv.norm_number_token(before),
            "after_norm": _mv.norm_number_token(after),
        })
    return tuple(out)


# ---------------------------------------------------------------- R7 条件①前置硬闸

def fact_subject_values(facts: Iterable | None) -> tuple[str, ...]:
    """主体抽取现有件取值：facts 主体槽 status=present 且 raw_value
    非空的主体名（去重保序）。缺省/非可读槽 → 空元组（机检无判据，
    留判官域——绝不把"抽不出"当"明确写了主体"）。"""
    if not facts:
        return ()
    values: list[str] = []
    for fact in facts:
        if not isinstance(fact, Mapping):
            continue
        subj = fact.get("subject")
        if not isinstance(subj, Mapping):
            continue
        raw = subj.get("raw_value")
        if subj.get("status") == "present" and isinstance(raw, str) and raw.strip():
            values.append(raw.strip())
    return tuple(dict.fromkeys(values))


def _subject_side_codes(text: str) -> tuple[str, ...]:
    """证券代码现有件：6 位主体锚代码 surface（去重保序）。"""
    return tuple(dict.fromkeys(
        m["surface"] for m in _mv.subject_code_mentions(text)))


def subject_backfill_gate(history_text: str, current_text: str, *,
                          history_subjects: Iterable[str] | None = (),
                          current_subjects: Iterable[str] | None = ()) -> dict:
    """R7 条件①机器前置硬闸（只产证据，不判）。

    每侧"明确写了主体"机检可判域 = 证券代码在场 ∪ 主体抽取现有件
    主体名在场（泛指占位天然不在域）。输出结构化证据：
    - unilateral_missing=True：有且仅有一方缺主体（回填条款可触发——
      判官终审条件②-⑤；哪侧缺读 *_has_subject）；
    - both_missing=True：双方都缺主体（回填不得触发，落边界）；
    - 双方都写：两布尔皆 False（不同主体→不重复为现状路径）。
    """
    h_codes = _subject_side_codes(history_text)
    c_codes = _subject_side_codes(current_text)
    h_subjects = tuple(history_subjects or ())
    c_subjects = tuple(current_subjects or ())
    h_has = bool(h_codes or h_subjects)
    c_has = bool(c_codes or c_subjects)
    return {
        "history_codes": list(h_codes),
        "current_codes": list(c_codes),
        "history_subjects": list(h_subjects),
        "current_subjects": list(c_subjects),
        "history_has_subject": h_has,
        "current_has_subject": c_has,
        "unilateral_missing": h_has != c_has,
        "both_missing": (not h_has) and (not c_has),
    }


# ---------------------------------------------------------------- R7 条件③时间维度（主窗补充令）

def fact_time_values(facts: Iterable | None) -> tuple[str, ...]:
    """时间抽取现有件取值：facts 时间槽 time.expression status=present
    且 raw_value 非空（去重保序）。缺省/非可读槽 → 空元组（机检无判据
    留判官域——绝不把"抽不出"当"写了时间"）。"""
    if not facts:
        return ()
    values: list[str] = []
    for fact in facts:
        if not isinstance(fact, Mapping):
            continue
        time_slot = fact.get("time")
        if not isinstance(time_slot, Mapping):
            continue
        expr = time_slot.get("expression")
        if not isinstance(expr, Mapping):
            continue
        raw = expr.get("raw_value")
        if (expr.get("status") == "present" and isinstance(raw, str)
                and raw.strip()):
            values.append(raw.strip())
    return tuple(dict.fromkeys(values))


def _time_mention_surfaces(text: str) -> tuple[str, ...]:
    """文本侧时间/阶段表述表面形（machine_verify 现有件三合一）：日期型
    extract_time_mentions（文中出现顺序）+ 阶段词 extract_stage_set
    （闭词表，排序）+ 相对时间词 relative_time_tokens_in（闭词表，排序）
    ；去重保序。只报在场，不比对、不判一致性（判官域）。"""
    surfaces: list[str] = [
        m["surface"] for m in _mv.extract_time_mentions(text)]
    surfaces.extend(sorted(_mv.extract_stage_set(text)))
    surfaces.extend(_mv.relative_time_tokens_in(text))
    return tuple(dict.fromkeys(surfaces))


def time_dimension_gate(history_text: str, current_text: str, *,
                        history_times: Iterable[str] | None = (),
                        current_times: Iterable[str] | None = ()) -> dict:
    """R7 条件③时间维度三分支证据（主窗补充令 2026-10-11；只产证据，
    绝不判）。

    每侧"有时间/阶段表述"机检可判域 = 日期型（闭形正则）∪阶段词（闭
    词表）∪相对时间词（闭词表）∪时间抽取现有件（facts 时间槽）。输出：
    - both_present（三分支 a）：双侧都有时间/阶段表述——取值是否一致
      由判官按条款①裁决（取值不同→不重复，不经回填规则）；
    - unilateral_missing（三分支 b）：一方有一方无（按单方信息补充）；
    - both_missing（三分支 c）：双侧都无——判官按硬门槛裁决（仅单一
      数值一致强制存疑转边界，防无时间锚的同口径跨期撞稿）。
    time_state 供 shadow 计数 .no_time 单列标签消费；机器绝不直接判。
    """
    h_text_times = _time_mention_surfaces(history_text)
    c_text_times = _time_mention_surfaces(current_text)
    h_times = tuple(dict.fromkeys(
        tuple(history_times or ()) + h_text_times))
    c_times = tuple(dict.fromkeys(
        tuple(current_times or ()) + c_text_times))
    h_has, c_has = bool(h_times), bool(c_times)
    if h_has and c_has:
        state = "both_present"
    elif h_has or c_has:
        state = "unilateral_missing"
    else:
        state = "both_missing"
    return {
        "history_time_mentions": list(h_times),
        "current_time_mentions": list(c_times),
        "history_has_time": h_has,
        "current_has_time": c_has,
        "time_state": state,
    }


# ---------------------------------------------------------------- 总装（注入判官上下文）

def build_machine_evidence(history_text: str, current_text: str, *,
                           history_facts: Iterable | None = (),
                           current_facts: Iterable | None = ()) -> dict:
    """v6-lite 一期机器候选证据总装（纯 JSON dict，判官上下文注入件）。

    - revision_candidates：R8 机器候选证据（双侧独立抽取，结构化位置/
      前后值；hit_any_side=任一侧有修订措辞命中——可观测性旗标，非判据）；
    - subject_backfill：R7 条件①机器前置硬闸证据（证券代码+主体抽取
      现有件）；
    - time_dimension：R7 条件③时间维度三分支证据（主窗补充令：日期/
      阶段/相对词闭形域+时间抽取现有件；time_state 供 shadow no_time
      单列标签）。

    机器绝不直接判：判官按 judge_v6 产品规则条款终审。
    """
    h_rev = revision_candidates(history_text)
    c_rev = revision_candidates(current_text)
    gate = subject_backfill_gate(
        history_text, current_text,
        history_subjects=fact_subject_values(history_facts),
        current_subjects=fact_subject_values(current_facts))
    time_dim = time_dimension_gate(
        history_text, current_text,
        history_times=fact_time_values(history_facts),
        current_times=fact_time_values(current_facts))
    return {
        "version": MACHINE_EVIDENCE_VERSION,
        "revision_candidates": {
            "history": [dict(item) for item in h_rev],
            "current": [dict(item) for item in c_rev],
            "hit_any_side": bool(h_rev or c_rev),
        },
        "subject_backfill": gate,
        "time_dimension": time_dim,
    }


__all__ = [
    "MACHINE_EVIDENCE_VERSION",
    "CONTEXT_KEY",
    "revision_candidates",
    "fact_subject_values",
    "fact_time_values",
    "subject_backfill_gate",
    "time_dimension_gate",
    "build_machine_evidence",
]
