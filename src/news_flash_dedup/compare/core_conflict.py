"""核心字段确定性硬冲突前置层（2026-10-10 提交一修复；终审 P0 收口
2026-10-10，分支 p3-semantic-authority）。

出处：独立复审整改令一（老板批准转发）+ 独立终审整改令（P0 六项）——
判官签发结果之前，对明确核心字段冲突做**确定性拦截**。仅当全部满足
时才允许硬判"不重复"：1. 两侧对应同一个事实槽位；2. 两侧字段都明确
出现（不是单方缺失）；3. 归一化后仍然不同；4. 两侧都能绑定原文证据；
5. 冲突属于确定性冲突，不需要语义猜测。

执行顺序（decide/service.py 判官循环内）：
规则比较 → 本层硬冲突前置（**仅 semantic_authority 模式**，终审 P0-1
模式闸；legacy_proof_gate 下本层不执行，行为与基线 2d0d418 全等）→
若硬冲突则直接不重复并**跳过判官** → 无硬冲突才进入判官双序合并 →
证明层只记录证据质量告警。

反冤杀纪律（fp=0 铁律：任一 false positive=把真重复冤杀成不重复）：
- 本层一切检测**FN 优先**（宁可漏给判官，不可错杀重复）——凡无法
  **证明**"同一原子 Fact+同一字段槽位"的，一律放行给判官；
- 数值冲突的"同槽位"判据=**剔数骨架逐字相等 + 双侧数值 token 数相等 +
  同位置归一值不同**（骨架不同=可能错位/跨指标，放行；"每10股派3元"
  vs "每股派0.3元"骨架 token 数不等，放行）——终审 P0-6 保留数值族；
- 主体冲突（终审 P0-2 收窄）=**双侧明确证券代码互斥**（6 位代码双侧
  非空且互斥）——名称比对整族降级：无代码=不判主体冲突，交给判官
  （"特朗普表示…"vs"特朗普总统表示…"曾被截成"特朗普表"/"特朗普总"
  误判主体不同，名称截短无法证明同一主体槽位）；
- 极性/单位/时间/阶段四族（终审 P0-3 摘除）：无法证明"同一原子
  Fact+同一字段槽位"就不得判冲突——四族整体从 _DETECTORS 摘除
  （槽位归属未证前停用，交判官），函数与测试保留备日后槽位对齐版
  回归；
- 修订冲突（终审 P0-4 摘除）：v5 提示词 R8 已定"前值修订在产品确认
  前一律存疑"，硬冲突层不得抢判不重复——_detect_revision 从
  _DETECTORS 摘除，函数与测试保留备日后回归。

异常纪律（终审 P0-5 fail-open）：detect_core_conflict 逐检测器
try/except——任一检测器异常即经 on_detector_error 上报诊断计数
（judge.core_conflict.detector_error，service 层记）+放行给判官，
绝不中断判定。

明确**不**升级为硬冲突：引文偏移失败、单方主体缺失、单方数值缺失、
相对时间缺少绝对锚点、无证伪轴——仍只是告警或边界。
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

# 终审 P0-2（2026-10-10 收窄）：原"文首取段首 4 字"回退（_name_core +
# _LEAD_RE/_STRIP_SUFFIXES/_GENERIC_LEADS 名称归一核机抽族）整体拆除——
# "特朗普表示…"vs"特朗普总统表示…"被截成"特朗普表"/"特朗普总"误判
# 主体不同（fp 实锤：把真重复冤杀成不重复）。名称比对整族降级：无代码
# =不判主体冲突，交给判官；主体硬冲突只保留双侧明确证券代码互斥分支
# （_mv.subject_code_mentions，"公司股票"等泛指占位词天然无代码=缺失
# 放行口径不变）。


def _detect_subject(text_a, text_b, id_a, id_b):
    """主体硬冲突（终审 P0-2）：只保留双侧明确证券代码互斥分支。

    codes 分支判据=双侧 6 位证券代码均非空且 surface 互斥（R1 同口径）；
    任一侧无代码（含泛指占位"公司股票"）→ 主体缺失，不判主体冲突，
    放行给判官（FN 优先——名称/简称/别名归一无法证明同一主体槽位）。
    """
    codes_a = _mv.subject_code_mentions(text_a)
    codes_b = _mv.subject_code_mentions(text_b)
    if codes_a and codes_b:
        sa = {m["surface"] for m in codes_a}
        sb = {m["surface"] for m in codes_b}
        if sa.isdisjoint(sb):                    # R1 同口径：双侧互斥
            return _hit("subject",
                        _ref(id_a, text_a, codes_a[0]["start"], codes_a[0]["end"]),
                        _ref(id_b, text_b, codes_b[0]["start"], codes_b[0]["end"]))
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
# 终审 P0-3（2026-10-10 摘除）：本族已从 _DETECTORS 摘除——"同一归一
# 数值挂载单位互斥"无法证明"同一原子 Fact+同一字段槽位"（跨槽位错配：
# "目标价100美元" vs "指数报100点"同值不同槽即被误伤）；槽位归属未证
# 前停用，交判官。函数与测试保留备日后槽位对齐版回归。

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
# 终审 P0-3（2026-10-10 摘除）：两族已从 _DETECTORS 摘除——双向差集只
# 证"双侧时间/阶段词不同"，不证"同一原子 Fact+同一字段槽位"（单方补
# 充/跨槽位/异事件同名词均可能）；槽位归属未证前停用，交判官。函数与
# 测试保留备日后槽位对齐版回归。

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
# 终审 P0-3（2026-10-10 摘除）：本族已从 _DETECTORS 摘除——反义词对
# 双侧命中只证"两文各含一个方向词"，不证"同一原子 Fact+同一字段槽位"
# （"甲指数上涨，乙指数下跌" vs "甲指数上涨"的双主体跨槽形态即被误
# 伤）；槽位归属未证前停用，交判官。函数与测试保留备日后槽位对齐版
# 回归。

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
# 终审 P0-4（2026-10-10 摘除）：本族已从 _DETECTORS 摘除——v5 提示词
# R8 已定"前值修订在产品确认前一律存疑"（修订链=同一事实的时间演化
# 而非冲突证据），硬冲突层不得抢判不重复；交判官按存疑口径裁决。函数
# 与测试保留备日后（产品确认后的）修订语义回归。

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

# 终审 P0-3/P0-4（2026-10-10）：极性/单位/时间/阶段四族+修订族已从
# _DETECTORS 摘除（槽位归属未证前停用，交判官；注释与反例钉见各族
# 分节）——现役仅主体（P0-2 收窄：仅双侧代码互斥）与数值（P0-6 保留：
# 剔数骨架同槽位判据）两族。
_DETECTORS = (_detect_subject, _detect_numeric)

# 已摘除族清单（备日后槽位对齐版回归时复位；函数本体保留在案）
_RETIRED_DETECTORS = (_detect_unit, _detect_time, _detect_stage,
                      _detect_polarity, _detect_revision)


def detect_core_conflict(history_text: str, current_text: str, *,
                         history_record_id: str,
                         current_record_id: str,
                         on_detector_error=None) -> CoreConflict:
    """确定性硬冲突检测（现役两族=主体代码互斥+数值骨架同槽位；固定
    优先序首中即返；全过=NO_CONFLICT）。

    只消费双侧正文（机抽确定性信号，不做任何语义猜测）；text_a=
    history、text_b=current（证据 record_id 按侧归属）。

    终审 P0-5 fail-open：逐检测器 try/except——任一检测器异常即调用
    on_detector_error(detector_name, exc) 上报（service 层据此记
    judge.core_conflict.detector_error 诊断计数）并**放行给判官**，
    绝不中断判定（本层 FN 优先：异常=证据不可判，不是冲突证据）。
    on_detector_error 未注入（None）时同样放行，不向上抛。
    """
    for detector in _DETECTORS:
        try:
            hit = detector(history_text, current_text,
                           history_record_id, current_record_id)
        except Exception as exc:  # noqa: BLE001 — fail-open：异常=放行给判官
            if on_detector_error is not None:
                on_detector_error(detector.__name__, exc)
            continue
        if hit is not None:
            return hit
    return NO_CONFLICT


__all__ = [
    "CONFLICT_TYPES",
    "CoreConflict",
    "NO_CONFLICT",
    "detect_core_conflict",
]
