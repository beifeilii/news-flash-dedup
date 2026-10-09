"""核心字段确定性硬冲突前置层（2026-10-10，提交一修复，分支 p3-semantic-authority）。

出处：独立复审整改令一（老板批准转发）——判官签发结果之前，对明确核心
字段冲突做**确定性拦截**。仅当全部满足时才允许硬判"不重复"：
1. 两侧对应同一个事实槽位；2. 两侧字段都明确出现（不是单方缺失）；
3. 归一化后仍然不同；4. 两侧都能绑定原文证据；5. 冲突属于确定性冲突，
不需要语义猜测。

执行顺序（decide/service.py 判官循环内）：
规则比较 → 本层硬冲突前置 → 若硬冲突则直接不重复并**跳过判官** →
无硬冲突才进入判官双序合并 → 证明层只记录证据质量告警。

反冤杀纪律（主窗附加纪律 1：任一 false positive=把真重复冤杀）：
- 本层一切检测**FN 优先**（宁可漏给判官，不可错杀重复）——凡需"语义
  猜测"才能确认同槽位的情形一律放行；
- 数值冲突的"同槽位"判据=**剔数骨架逐字相等 + 双侧数值 token 数相等 +
  同位置归一值不同**（骨架不同=可能错位/跨指标，放行；"每10股派3元"
  vs "每股派0.3元"骨架 token 数不等，放行）；
- 时间/阶段冲突=**双向**差集均非空（单侧补一条时间/阶段词=信息补充，
  放行）；
- 极性冲突=反义词对双侧分别命中（否定词族不对称不算——"并未拉低" vs
  "未拉低"只进证明层告警）；
- 主体冲突=6 位代码双侧非空互斥，或文首主体名双侧可抽取且归一核互不
  包含（"公司股票"等泛指占位词按缺失处理，别名/简称/带 ST 前缀按包含
  归一，不得误判）；
- 单位冲突=**同一归一数值**两侧挂载单位词且互斥（"6612.30美元" vs
  "6612.30点"；数值不同的归数值族，单位缺失侧放行）；
- 修订冲突=一侧"由 X 修正/修订/更正为 Y"且 X 在另一侧在场、Y 与 X
  归一不等、Y 不在另一侧在场（缺一放行）。
明确**不**升级为硬冲突（整改令一末段）：引文偏移失败、单方主体缺失、
单方数值缺失、相对时间缺少绝对锚点、无证伪轴——仍只是告警或边界。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping

from news_flash_dedup.compare.value_time import EvidenceRef
from news_flash_dedup.decide import machine_verify as _mv

CONFLICT_TYPES = ("subject", "numeric", "unit", "time", "stage",
                  "polarity", "revision")

# 公共理由措辞（整改令五：硬冲突不重复→按实际冲突类型措辞，禁用
# "已验证""已逐一核验"等超出实际证据能力的表述）
_HUMAN_REASON = {
    "subject": "两条快讯的核心主体不同，属于同一槽位明确冲突，因此判定为不重复。",
    "numeric": "两条快讯的核心数值不同，属于同一槽位明确冲突，因此判定为不重复。",
    "unit": "两条快讯同槽位数值的单位不同，属于同一槽位明确冲突，因此判定为不重复。",
    "time": "两条快讯的事实时间不同，属于同一槽位明确冲突，因此判定为不重复。",
    "stage": "两条快讯的时间阶段不同，属于同一槽位明确冲突，因此判定为不重复。",
    "polarity": "两条快讯的方向相反，属于同一槽位明确冲突，因此判定为不重复。",
    "revision": "两条快讯为修订关系且修订后最新有效值不同，属于同一槽位明确冲突，因此判定为不重复。",
}


@dataclass(frozen=True)
class CoreConflict:
    """硬冲突检测输出（整改令一：至少含 has_conflict/conflict_type/
    field_path/human_reason/history_evidence/current_evidence）。"""
    has_conflict: bool
    conflict_type: str = ""                    # ∈ CONFLICT_TYPES（无冲突=空串）
    field_path: str = ""                       # 无冲突=空串
    human_reason: str = ""                     # 无冲突=空串
    history_evidence: EvidenceRef | None = None
    current_evidence: EvidenceRef | None = None


NO_CONFLICT = CoreConflict(has_conflict=False)


def _hit(ctype: str, h_ev: EvidenceRef, c_ev: EvidenceRef) -> CoreConflict:
    return CoreConflict(
        has_conflict=True, conflict_type=ctype,
        field_path=f"core_conflict.{ctype}",
        human_reason=_HUMAN_REASON[ctype],
        history_evidence=h_ev, current_evidence=c_ev)


def _ref(record_id: str, text: str, start: int, end: int) -> EvidenceRef:
    return EvidenceRef(record_id=record_id, field="text",
                       quote=text[start:end], start=start, end=end)


# ---------------------------------------------------------------- 主体

# 文首主体名抽取：跳过【】/空白后，取 "*ST/ST 前缀 + 连续 CJK 段"（遇
# 括号代码/标点/字母数字即止）。泛指占位词按缺失处理（整改令三反例：
# "公司股票"不按主体冲突处理）。
_LEAD_RE = re.compile(r"^[\s【〔「\x22']*(\*?ST)?([一-鿿]{2,12})")
_STRIP_SUFFIXES = ("股份有限公司", "有限责任公司", "有限公司",
                   "股份", "公司", "集团", "控股", "银行", "证券")
_GENERIC_LEADS = ("公司股票", "该公司股票", "该公司", "本公司", "我公司",
                  "本集团", "公司", "集团", "发行人", "标的", "个股")


def _name_core(text: str) -> tuple[str, int, int] | None:
    """文首主体名归一核（剥 *ST/ST 前缀与公司类后缀）+原文 span；
    抽取失败/泛指占位 → None（按主体缺失，绝不当冲突证据）。

    定界工艺（FN 优先）：文首 CJK 段内**首个公司类后缀锚定**名称边界
    （"甲公司营收…"→"甲公司"）；无后缀时回退取段首 4 字（快讯简称惯
    例，配合归一核包含比对，截短只可能漏判不可能冤杀）；段首命中泛
    指占位词族（"公司股票""该公司"…）按主体缺失处理。
    """
    m = _LEAD_RE.match(text)
    if not m:
        return None
    run = m.group(2)
    for generic in _GENERIC_LEADS:
        if run.startswith(generic):
            return None
    raw_start = m.start(2) - (len(m.group(1)) if m.group(1) else 0)
    core, raw_end = "", m.end(2)
    for suf in _STRIP_SUFFIXES:
        pos = run.find(suf)
        if pos > 0:                              # 后缀前有实芯才算锚定
            core, raw_end = run[:pos], m.start(2) + pos + len(suf)
            break
    if not core:
        core = run[:4]
        raw_end = m.start(2) + len(core)
    if len(core) < 2 or any(core.startswith(g) for g in _GENERIC_LEADS):
        return None
    return core, raw_start, raw_end


def _detect_subject(text_a, text_b, id_a, id_b):
    codes_a = _mv.subject_code_mentions(text_a)
    codes_b = _mv.subject_code_mentions(text_b)
    if codes_a and codes_b:
        sa = {m["surface"] for m in codes_a}
        sb = {m["surface"] for m in codes_b}
        if sa.isdisjoint(sb):                    # R1 同口径：双侧互斥
            return _hit("subject",
                        _ref(id_a, text_a, codes_a[0]["start"], codes_a[0]["end"]),
                        _ref(id_b, text_b, codes_b[0]["start"], codes_b[0]["end"]))
    name_a = _name_core(text_a)
    name_b = _name_core(text_b)
    if name_a and name_b:
        core_a, core_b = name_a[0], name_b[0]
        # 归一核互相包含=同一主体可无损归一（别名/全称/简称），放行；
        # 互不包含=明确主体不同（FN 优先：包含关系一律不判冲突）。
        if core_a not in core_b and core_b not in core_a:
            return _hit("subject",
                        _ref(id_a, text_a, name_a[1], name_a[2]),
                        _ref(id_b, text_b, name_b[1], name_b[2]))
    return None


# ---------------------------------------------------------------- 数值（剔数骨架同槽位判据）

_BARE_UNIT_SURFACE_RE = re.compile(r"[万亿千百%％]+\Z")


def _real_mentions(text: str) -> tuple[dict, ...]:
    """数值 mentions（文中出现序），剔除裸单位词抽取伪影（如"328.6万"
    内嵌的孤立"万" surface——norm=0 的抽取噪声）。"""
    mentions = [m for m in _mv.number_mentions(text)
                if not _BARE_UNIT_SURFACE_RE.fullmatch(m["surface"])]
    mentions.sort(key=lambda m: m["start"])
    return tuple(mentions)


def _number_skeleton(text: str, mentions: tuple[dict, ...]) -> str:
    """剔数骨架：数值 mention 切片全部移除（长 span 优先、重叠只剔外层）。"""
    spans = sorted((m["start"], m["end"]) for m in mentions)
    out: list[str] = []
    pos = 0
    for s, e in spans:
        if s < pos:                              # 重叠（内层伪影已剔除，双保险）
            continue
        out.append(text[pos:s])
        pos = e
    out.append(text[pos:])
    return "".join(out)


def _detect_numeric(text_a, text_b, id_a, id_b):
    ma, mb = _real_mentions(text_a), _real_mentions(text_b)
    if not ma or not mb or len(ma) != len(mb):
        return None                              # 单方缺失/数量不等=信息补充，放行
    if _number_skeleton(text_a, ma) != _number_skeleton(text_b, mb):
        return None                              # 骨架不同=可能错位/跨指标，放行
    for m_a, m_b in zip(ma, mb):
        if m_a["norm"] != m_b["norm"]:
            return _hit("numeric",
                        _ref(id_a, text_a, m_a["start"], m_a["end"]),
                        _ref(id_b, text_b, m_b["start"], m_b["end"]))
    return None


# ---------------------------------------------------------------- 单位（同一归一数值挂载单位互斥）

# 闭合单位词表（多字优先匹配；% 全半角归一）。
_UNITS = ("人民币", "个基点", "美元", "港元", "欧元", "日元", "英镑",
          "元", "点", "股", "手", "张", "吨", "桶", "倍", "厘",
          "%", "％", "bp", "BP")


def _unit_after(text: str, end: int) -> str | None:
    tail = text[end:end + 3]
    for unit in _UNITS:
        if tail.startswith(unit):
            return "%" if unit == "％" else unit
    return None


def _detect_unit(text_a, text_b, id_a, id_b):
    ma, mb = _real_mentions(text_a), _real_mentions(text_b)
    if not ma or not mb:
        return None
    units_a: dict[str, set] = {}
    units_b: dict[str, set] = {}
    for store, mentions, text in ((units_a, ma, text_a),
                                  (units_b, mb, text_b)):
        for m in mentions:
            unit = _unit_after(text, m["end"])
            if unit is not None:
                store.setdefault(m["norm"], set()).add(unit)
    for norm in sorted(set(units_a) & set(units_b)):
        ua, ub = units_a[norm], units_b[norm]
        if ua and ub and ua.isdisjoint(ub):
            m_a = next(m for m in ma if m["norm"] == norm)
            m_b = next(m for m in mb if m["norm"] == norm)
            return _hit("unit",
                        _ref(id_a, text_a, m_a["start"],
                             m_a["end"] + len(sorted(ua)[0])),
                        _ref(id_b, text_b, m_b["start"],
                             m_b["end"] + len(sorted(ub)[0])))
    return None


# ---------------------------------------------------------------- 时间 / 阶段（双向差集）

def _detect_time(text_a, text_b, id_a, id_b):
    mentions_a = _mv.extract_time_mentions(text_a)
    mentions_b = _mv.extract_time_mentions(text_b)
    ta = {m["norm"] for m in mentions_a}
    tb = {m["norm"] for m in mentions_b}
    only_a, only_b = ta - tb, tb - ta
    if only_a and only_b:                        # 双向差集（单侧补充放行）
        norm_a, norm_b = sorted(only_a)[0], sorted(only_b)[0]
        m_a = next(m for m in mentions_a if m["norm"] == norm_a)
        m_b = next(m for m in mentions_b if m["norm"] == norm_b)
        return _hit("time",
                    _ref(id_a, text_a, m_a["start"], m_a["end"]),
                    _ref(id_b, text_b, m_b["start"], m_b["end"]))
    return None


def _detect_stage(text_a, text_b, id_a, id_b):
    sa = _mv.extract_stage_set(text_a)
    sb = _mv.extract_stage_set(text_b)
    only_a, only_b = sa - sb, sb - sa
    if only_a and only_b:
        word_a, word_b = sorted(only_a)[0], sorted(only_b)[0]
        start_a, start_b = text_a.find(word_a), text_b.find(word_b)
        return _hit("stage",
                    _ref(id_a, text_a, start_a, start_a + len(word_a)),
                    _ref(id_b, text_b, start_b, start_b + len(word_b)))
    return None


# ---------------------------------------------------------------- 极性（反义词对双侧命中；否定不对称不拦）

def _detect_polarity(text_a, text_b, id_a, id_b):
    for pos, neg in _mv.POLARITY_PAIRS:
        if pos in text_a and neg in text_b:
            w_a, w_b = pos, neg
        elif neg in text_a and pos in text_b:
            w_a, w_b = neg, pos
        else:
            continue
        start_a, start_b = text_a.find(w_a), text_b.find(w_b)
        return _hit("polarity",
                    _ref(id_a, text_a, start_a, start_a + len(w_a)),
                    _ref(id_b, text_b, start_b, start_b + len(w_b)))
    return None


# ---------------------------------------------------------------- 修订（由 X 修正/修订/更正为 Y）

_REVISION_RE = re.compile(
    r"(?:由|从)\s*([+-]?\d+(?:,\d{3})*(?:\.\d+)?\s*(?:万亿|万|亿|千|百)?)"
    r"\s*(?:修正|修订|更正)\s*(?:为|至|到)\s*"
    r"([+-]?\d+(?:,\d{3})*(?:\.\d+)?\s*(?:万亿|万|亿|千|百)?)")


def _detect_revision(text_a, text_b, id_a, id_b):
    """一侧明示"由 X 修正为 Y"，X 在另一侧在场、Y 与 X 归一不等、Y 不在
    另一侧在场 → 冲突；缺一放行（无旧值锚=槽位不可证，FN 优先）。"""
    for text_new, text_old, id_new, id_old in (
            (text_b, text_a, id_b, id_a), (text_a, text_b, id_a, id_b)):
        m = _REVISION_RE.search(text_new)
        if not m:
            continue
        old_norm = _mv.norm_number_token(m.group(1))
        new_norm = _mv.norm_number_token(m.group(2))
        if old_norm is None or new_norm is None or old_norm == new_norm:
            continue
        old_mentions = _real_mentions(text_old)
        old_side_norms = {mm["norm"] for mm in old_mentions}
        if old_norm not in old_side_norms or new_norm in old_side_norms:
            continue
        old_hit = next(mm for mm in old_mentions if mm["norm"] == old_norm)
        new_start = m.start(2)
        ev_new = _ref(id_new, text_new, new_start, m.end(2))
        ev_old = _ref(id_old, text_old, old_hit["start"], old_hit["end"])
        # history/current 归属按入参侧还原（text_a=history）
        h_ev, c_ev = ((ev_old, ev_new) if id_new == id_b
                      else (ev_new, ev_old))
        return _hit("revision", h_ev, c_ev)
    return None


# ---------------------------------------------------------------- 主入口

_DETECTORS = (_detect_subject, _detect_numeric, _detect_unit,
              _detect_time, _detect_stage, _detect_polarity,
              _detect_revision)


def detect_core_conflict(history_text: str, current_text: str, *,
                         history_record_id: str,
                         current_record_id: str) -> CoreConflict:
    """确定性硬冲突检测（七族，固定优先序首中即返；全过=NO_CONFLICT）。

    只消费双侧正文（机抽确定性信号，不做任何语义猜测）；text_a=history、
    text_b=current（证据 record_id 按侧归属）。任一检测族内部异常不向
    上抛——本层 FN 优先，异常=放行给判官（不做静默拦截）。
    """
    for detector in _DETECTORS:
        hit = detector(history_text, current_text,
                       history_record_id, current_record_id)
        if hit is not None:
            return hit
    return NO_CONFLICT


__all__ = [
    "CONFLICT_TYPES",
    "CoreConflict",
    "NO_CONFLICT",
    "detect_core_conflict",
]
