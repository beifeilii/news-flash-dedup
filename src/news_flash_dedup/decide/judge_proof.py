"""P1-a 判官可签发证明组件（decide/judge_proof.py）——2026-10-09 新建。

出处：log\判定链改造最终方案-Codex-20261008.md §3.1（语义判官主路签发条件）
+ §4 P1-a（施工包：缓存键/引文 offset/机侧独立核验/结论对拍/证明可审计
可复验）；log\判定链改造-终裁令-正典-1009.md §五-2/3（P1-a 范围=证明
组件，不接正式签发链；not_duplicate 证明须有双侧证伪证据字段+失败状态
枚举）。**本模块只建证明件，不接正式签发链（P1-b 的活，等 P0 验收）。**

生效面（任务书⑦ gate 语义）：一切证明产物只在环境开关
``DEDUP_JUDGE_PROOF=1``（精确 "1"，其余一律关，text/__init__.py:31-36
同型纪律）开启的证明路上构造；开关关=旧 audit/gate 调用路径逐字节
（llm_residual/machine_verify 现役函数零改动，本模块零消费）。开关开时
audit 模式只观测不降级、gate 模式按 P_* 触发降级存疑（含"不重复无机器
可证伪轴"降级——未证排除不得冒充已证，任务书⑥）。

七条口径落点（任务书①-⑦）：
① 缓存键加固 → llm_residual.residual_cache_key_hardened（本模块供
   PROOF_POLICY_VERSION 维度）；证明每跑必从判定+原文现算——缓存命中
   只省 LLM 调用、不省证明核验，旧缓存天然 miss 不升级为签发凭证。
② 引文 offset 绑定 → verify_order_judgment 的 citations：精确逐字
   Unicode 码点 offset（machine_verify.find_quote_spans）；锚去空白
   <4 字符 / 本侧精确无命中（去空白伪匹配拒签）/ 仅对侧可命中（错侧
   引文拒签）→ citation_failures。归属角色校验=本侧归属 + 错侧拒签 +
   主体锚一致性（机检 P_SUBJECT，6 位代码集互斥即引文救不回）。
③ 机侧独立核验 → 时间/阶段（machine_time_stage_compare）、数值
   （rule_r3_numeric_conflict 同口径机抽）、否定/极性
   （machine_negation_compare）全部从原文机抽，不信判官供数；一侧
   缺失放行=缺失≠冲突；判官声称一致处须机检无冲突背书（backed=False
   即拦签）。判官自供时间自相矛盾另列 P_JTIME（抓其自反，非信其供数）。
④ 结论对拍 → conclusion_check：decision="重复" 而 numeric_check/
   time_check.conclusion="不一致" → P_CONCLUSION 拦截。
⑤ VerifiedJudgeProof → build_duplicate_proof：双文 hash / 双序最终
   判决 / 引文 offset / 数值·时间·否定·阶段检查 / 机验结果 /
   model·prompt·policy 版本；to_dict/from_dict 可序列化，
   verify_proof_dict 可复验（自含判定快照，双文+版本对拍后全量重算）。
⑥ NotDuplicateProof → build_not_duplicate_proof：双侧证伪轴
   （FalsificationAxis 字典：subject/numeric/time/stage/polarity 差异
   + 双侧原文引用 offset + 对应关系说明）+ 失败状态枚举
   NotDuplicateFailure（证据不足→未决），供 P1-b 按"全部必检候选有效
   排除"聚合消费。禁止把"模型两次说不重复+机验未触发"包装成已证排除：
   双序无共同机器可证伪轴 → issued=False。
⑦ P_* 规则 id 与现役 R1-R6 audit 命名隔离，审计轨迹不混。
"""

from __future__ import annotations

import enum
import hashlib
import json
import os
import re
from dataclasses import dataclass
from typing import Any, Mapping

from . import machine_verify as _mv

# ---------------------------------------------------------------- 常量 / 开关

PROOF_POLICY_VERSION = "p1a-proof-v1"
PROOF_TYPE_DUPLICATE = "duplicate"
PROOF_TYPE_NOT_DUPLICATE = "not_duplicate"
JUDGE_PROOF_ENV = "DEDUP_JUDGE_PROOF"

# P_* 证明级规则 id（与现役 R1-R6 audit 规则命名隔离）
P_OFFSET = "P_OFFSET"            # ② 引文 offset 绑定（锚短/伪匹配/错侧拒签）
P_NUMERIC = "P_NUMERIC"          # ③ 机侧数值冲突（R3 同口径机抽集合）
P_TIME = "P_TIME"                # ③ 机侧时间冲突（机抽日期集，阶段限定）
P_STAGE = "P_STAGE"              # ③ 机侧阶段冲突（机抽阶段词集不对称）
P_NEGATION = "P_NEGATION"        # 否定/极性机检冲突（R6+否定词族不对称）
P_SUBJECT = "P_SUBJECT"          # 主体锚一致性（R1 同口径机抽代码集）
P_CONCLUSION = "P_CONCLUSION"    # ④ 判定与分项结论对拍（重复 vs 不一致）
P_JTIME = "P_JTIME"              # 判官自供时间自相矛盾（R2 同型抓自反）
P_NO_AXIS = "P_NO_AXIS"          # ⑥ 不重复无机器可证伪轴（证据不足→未决）

# to_interceptions 确定性输出序
_P_RULE_ORDER = (P_OFFSET, P_SUBJECT, P_NUMERIC, P_TIME, P_STAGE,
                 P_NEGATION, P_CONCLUSION, P_JTIME, P_NO_AXIS)

_WS_RE = re.compile(r"\s+")      # R5 锚长检查同款空白归一（machine_verify._WS 同型）


def judge_proof_enabled(env: Mapping[str, str] | None = None) -> bool:
    """开关解析（2026-10-09 P1-a）：精确 "1"=开；缺省/空串/其余一律关
    （fail-closed 默认关，"true"/大小写变体等不放大——cert_decouple_enabled
    同型纪律，text/__init__.py:31-36）。"""
    source = os.environ if env is None else env
    return source.get(JUDGE_PROOF_ENV, "") == "1"


def text_sha256(text: str) -> str:
    """原文 sha256（双文 hash 绑定维度；UTF-8 编码）。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _strip_ws(s: str) -> str:
    return _WS_RE.sub("", s)


class NotDuplicateFailure(enum.Enum):
    """not_duplicate 证明失败状态枚举（任务书⑥：一切失败=未决，供 P1-b
    按"全部必检候选有效排除"聚合时剔除）。"""
    DUAL_ORDER_DISAGREEMENT = "dual_order_disagreement"
    CITATION_UNBOUND = "citation_unbound"
    INSUFFICIENT_FALSIFICATION_EVIDENCE = "insufficient_falsification_evidence"


# ---------------------------------------------------------------- 引文 offset 绑定（②）

@dataclass(frozen=True)
class CitationSpan:
    """单条引文的本侧原文 offset 绑定。offset=Unicode 码点下标（左闭右开，
    Python str 语义；非 UTF-16/字节下标）。多次精确命中取首处、全数备案。"""
    side: str        # "A" | "B"（本顺序 user message 的 文本A/文本B 角色）
    field: str       # "evidence" | "numbers" | "times"
    quote: str
    start: int
    end: int
    occurrences: int

    def to_dict(self) -> dict:
        return {"side": self.side, "field": self.field, "quote": self.quote,
                "start": self.start, "end": self.end,
                "occurrences": self.occurrences}

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "CitationSpan":
        return cls(side=d["side"], field=d["field"], quote=d["quote"],
                   start=d["start"], end=d["end"],
                   occurrences=d["occurrences"])


def _bind_citations(judgment: Mapping[str, Any], text_a: str, text_b: str
                    ) -> tuple[tuple[CitationSpan, ...], tuple[dict, ...]]:
    """全量引文（证据/数值/时间×双侧）精确 offset 绑定。

    拒签三类（citation_failures，quote 全量留存不截断——证明自含复验）：
    - anchor_too_short：证据锚去空白 <4 字符（R5 同口径锚长纪律，仅 evidence）；
    - not_found_exact：本侧精确无命中——去空白伪匹配在证明路拒签（现役 R5
      的 whitespace 模糊通道此处不存在）；
    - wrong_side：本侧无命中而仅对侧可命中（错侧引文拒签=归属角色校验）。
    """
    nc, tc = judgment["numeric_check"], judgment["time_check"]
    groups = (
        ("evidence", "A", judgment["evidence_a"], text_a, text_b),
        ("evidence", "B", judgment["evidence_b"], text_b, text_a),
        ("numbers", "A", nc["numbers_a"], text_a, text_b),
        ("numbers", "B", nc["numbers_b"], text_b, text_a),
        ("times", "A", tc["times_a"], text_a, text_b),
        ("times", "B", tc["times_b"], text_b, text_a),
    )
    spans: list[CitationSpan] = []
    failures: list[dict] = []
    for field, side, quotes, own, other in groups:
        for q in quotes:
            if field == "evidence" and len(_strip_ws(q)) < 4:
                failures.append({"check": "anchor_too_short", "side": side,
                                 "field": field, "quote": q})
                continue
            found = _mv.find_quote_spans(q, own)
            if not found:
                check = ("wrong_side" if _mv.find_quote_spans(q, other)
                         else "not_found_exact")
                failures.append({"check": check, "side": side,
                                 "field": field, "quote": q})
                continue
            s, e = found[0]
            spans.append(CitationSpan(side=side, field=field, quote=q,
                                      start=s, end=e,
                                      occurrences=len(found)))
    return tuple(spans), tuple(failures)


# ---------------------------------------------------------------- 机侧独立核验（③④）

def _numeric_check(judgment: Mapping[str, Any], text_a: str, text_b: str) -> dict:
    """数值机检（R3 同口径机抽集合对拍）+ 判官声称一致的机检背书。"""
    conflict = _mv.rule_r3_numeric_conflict(text_a, text_b)
    sa, sb = _mv.filtered_number_set(text_a), _mv.filtered_number_set(text_b)
    return {"judge_conclusion": judgment["numeric_check"]["conclusion"],
            "machine_conflict": conflict,
            "only_a": sorted(sa - sb)[:5], "only_b": sorted(sb - sa)[:5],
            "backed": not conflict}


def _time_check(judgment: Mapping[str, Any], comp: Mapping[str, Any]) -> dict:
    """时间机检（机抽日期集；一侧缺失放行=缺失≠冲突）+ 机检背书。"""
    return {"judge_conclusion": judgment["time_check"]["conclusion"],
            "machine_times_a": list(comp["times_a"]),
            "machine_times_b": list(comp["times_b"]),
            "machine_conflict": comp["time_conflict"],
            "backed": not comp["time_conflict"]}


def _conclusion_check(judgment: Mapping[str, Any]) -> dict:
    """④ 结论对拍：decision="重复" 而 numeric_check/time_check.conclusion
    ="不一致" → 拦截（判官自述与判定直接矛盾，不得签发）。"""
    violations: list[dict] = []
    if judgment["decision"] == "重复":
        nc = judgment["numeric_check"]["conclusion"]
        tc = judgment["time_check"]["conclusion"]
        if nc == "不一致":
            violations.append({"field": "numeric_check", "conclusion": nc})
        if tc == "不一致":
            violations.append({"field": "time_check", "conclusion": tc})
    return {"ok": not violations, "violations": violations}


def _judge_time_selfcheck(judgment: Mapping[str, Any]) -> dict:
    """判官自供时间锚自相矛盾（现役 R2 同型）——抓判官自反（其供数不作
    签发背书，但其自相矛盾必须拦截）。"""
    tc = judgment["time_check"]
    return {"ok": not _mv.rule_r2_time_anchor(judgment),
            "times_a": list(tc["times_a"]), "times_b": list(tc["times_b"])}


# ---------------------------------------------------------------- 证伪轴（⑥ not_duplicate）

def _first_mention(mentions: tuple[dict, ...], key: str, value: str) -> dict:
    for m in mentions:
        if m[key] == value:
            return m
    raise KeyError(value)      # 结构性不可能（值集来自同一 mentions），fail-closed


def _build_falsification_axes(text_a: str, text_b: str) -> tuple[dict, ...]:
    """机器可证伪轴（⑥）：足以证伪"同一现实事实"的双侧差异证据。

    五族（全机抽、双侧原文引用 offset、对应关系说明）：
    - subject：双侧 6 位主体代码集均非空且互斥（R1 同口径）；
    - numeric：过滤日期/序数后数值集双向差异（R3 同口径——双侧各有独值）；
    - time：双侧机抽日期集均非空且归一不等（可识别时间表述不一致）；
    - stage：双侧机抽阶段词集均非空且不等（如 初值 vs 终值、开盘 vs 收盘）；
    - polarity：反义极性词对两侧分别命中（R6 同口径——方向冲突双侧成词）。
    否定词族不对称因对侧无对应成词引用（⑥要求双侧原文引用+对应关系），
    不证伪轴——仅作 duplicate 侧 P_NEGATION 拦截用。
    """
    axes: list[dict] = []
    # subject
    codes_a = _mv.subject_code_mentions(text_a)
    codes_b = _mv.subject_code_mentions(text_b)
    ca = sorted({m["surface"] for m in codes_a})
    cb = sorted({m["surface"] for m in codes_b})
    if ca and cb and set(ca).isdisjoint(set(cb)):
        ma = _first_mention(codes_a, "surface", ca[0])
        mb = _first_mention(codes_b, "surface", cb[0])
        axes.append({
            "axis": "subject", "quote_a": ca[0], "start_a": ma["start"],
            "end_a": ma["end"], "quote_b": cb[0], "start_b": mb["start"],
            "end_b": mb["end"],
            "correspondence": "主体代码互斥：双侧 6 位数字代码集均非空且不相交",
            "detail": {"codes_a": ca, "codes_b": cb}})
    # numeric
    na = _mv.number_mentions(text_a)
    nb = _mv.number_mentions(text_b)
    sa = {m["norm"] for m in na}
    sb = {m["norm"] for m in nb}
    only_a = sorted(sa - sb)
    only_b = sorted(sb - sa)
    if only_a and only_b:
        ma = _first_mention(na, "norm", only_a[0])
        mb = _first_mention(nb, "norm", only_b[0])
        axes.append({
            "axis": "numeric", "quote_a": ma["surface"], "start_a": ma["start"],
            "end_a": ma["end"], "quote_b": mb["surface"],
            "start_b": mb["start"], "end_b": mb["end"],
            "correspondence": "关键数值双向差异：过滤日期/序数后两侧互有独值",
            "detail": {"only_a": only_a[:5], "only_b": only_b[:5]}})
    # time
    ta_m = _mv.extract_time_mentions(text_a)
    tb_m = _mv.extract_time_mentions(text_b)
    tsa = {m["norm"] for m in ta_m}
    tsb = {m["norm"] for m in tb_m}
    if tsa and tsb and tsa != tsb:
        diff_a = sorted(tsa - tsb) or sorted(tsa)
        diff_b = sorted(tsb - tsa) or sorted(tsb)
        ma = _first_mention(ta_m, "norm", diff_a[0])
        mb = _first_mention(tb_m, "norm", diff_b[0])
        axes.append({
            "axis": "time", "quote_a": ma["surface"], "start_a": ma["start"],
            "end_a": ma["end"], "quote_b": mb["surface"],
            "start_b": mb["start"], "end_b": mb["end"],
            "correspondence": "可识别时间表述双侧不一致（机抽日期集归一不等）",
            "detail": {"times_a": sorted(tsa), "times_b": sorted(tsb)}})
    # stage
    sta = _mv.extract_stage_set(text_a)
    stb = _mv.extract_stage_set(text_b)
    if sta and stb and sta != stb:
        wa = sorted(sta - stb)[0] if sta - stb else sorted(sta)[0]
        wb = sorted(stb - sta)[0] if stb - sta else sorted(stb)[0]
        axes.append({
            "axis": "stage", "quote_a": wa, "start_a": text_a.find(wa),
            "end_a": text_a.find(wa) + len(wa), "quote_b": wb,
            "start_b": text_b.find(wb), "end_b": text_b.find(wb) + len(wb),
            "correspondence": "事实阶段词双侧不对称（机抽阶段词集不等）",
            "detail": {"stage_a": sorted(sta), "stage_b": sorted(stb)}})
    # polarity
    for w1, w2 in _mv.POLARITY_PAIRS:
        if w1 in text_a and w2 in text_b:
            axes.append({
                "axis": "polarity", "quote_a": w1, "start_a": text_a.find(w1),
                "end_a": text_a.find(w1) + len(w1), "quote_b": w2,
                "start_b": text_b.find(w2), "end_b": text_b.find(w2) + len(w2),
                "correspondence": "方向/极性冲突：反义词对双侧分别命中",
                "detail": {"pair": [w1, w2]}})
            break
        if w2 in text_a and w1 in text_b:
            axes.append({
                "axis": "polarity", "quote_a": w2, "start_a": text_a.find(w2),
                "end_a": text_a.find(w2) + len(w2), "quote_b": w1,
                "start_b": text_b.find(w1), "end_b": text_b.find(w1) + len(w1),
                "correspondence": "方向/极性冲突：反义词对双侧分别命中",
                "detail": {"pair": [w2, w1]}})
            break
    return tuple(axes)


# ---------------------------------------------------------------- 单顺序核验

@dataclass(frozen=True)
class OrderVerification:
    """单顺序证明级核验记录（decision ∈ {重复, 不重复}；存疑不送核——本已
    保守）。judgment=判定快照（解析后结构），证明自含复验的判官侧输入。"""
    order: str                              # "hc" | "ch"
    decision: str
    text_a_sha256: str
    text_b_sha256: str
    judgment: dict
    citations: tuple[CitationSpan, ...]
    citation_failures: tuple[dict, ...]
    numeric_check: dict
    time_check: dict
    stage_check: dict
    negation_check: dict
    subject_check: dict
    conclusion_check: dict
    judge_time_selfcheck: dict
    axes: tuple[dict, ...]                  # 仅 decision=="不重复" 时非空
    failures: tuple[str, ...]               # 触发的 P_* 规则 id（_P_RULE_ORDER 序）
    ok: bool

    def to_interceptions(self) -> tuple[dict, ...]:
        """触发明细（llm_residual interceptions 槽同款 {"rule": ...} 形态）。"""
        out: list[dict] = []
        for rule in _P_RULE_ORDER:
            if rule not in self.failures:
                continue
            if rule == P_OFFSET:
                out.append({"rule": rule,
                            "failures": [dict(f) for f in self.citation_failures]})
            elif rule == P_SUBJECT:
                out.append({"rule": rule,
                            "codes_a": list(self.subject_check["codes_a"]),
                            "codes_b": list(self.subject_check["codes_b"])})
            elif rule == P_NUMERIC:
                out.append({"rule": rule,
                            "only_a": list(self.numeric_check["only_a"]),
                            "only_b": list(self.numeric_check["only_b"])})
            elif rule == P_TIME:
                out.append({"rule": rule,
                            "times_a": list(self.time_check["machine_times_a"]),
                            "times_b": list(self.time_check["machine_times_b"])})
            elif rule == P_STAGE:
                out.append({"rule": rule,
                            "stage_a": list(self.stage_check["stage_a"]),
                            "stage_b": list(self.stage_check["stage_b"])})
            elif rule == P_NEGATION:
                out.append({"rule": rule,
                            "polarity_pairs": [list(p) for p in
                                               self.negation_check["polarity_pairs"]],
                            "negation_a": list(self.negation_check["negation_a"]),
                            "negation_b": list(self.negation_check["negation_b"])})
            elif rule == P_CONCLUSION:
                out.append({"rule": rule,
                            "violations": [dict(v) for v in
                                           self.conclusion_check["violations"]]})
            elif rule == P_JTIME:
                out.append({"rule": rule,
                            "times_a": list(self.judge_time_selfcheck["times_a"]),
                            "times_b": list(self.judge_time_selfcheck["times_b"])})
            else:                            # P_NO_AXIS
                out.append({"rule": rule})
        return tuple(out)

    def to_dict(self) -> dict:
        return {
            "order": self.order, "decision": self.decision,
            "text_a_sha256": self.text_a_sha256,
            "text_b_sha256": self.text_b_sha256,
            "judgment": json.loads(json.dumps(self.judgment, ensure_ascii=False)),
            "citations": [c.to_dict() for c in self.citations],
            "citation_failures": json.loads(
                json.dumps(list(self.citation_failures), ensure_ascii=False)),
            "numeric_check": dict(self.numeric_check),
            "time_check": dict(self.time_check),
            "stage_check": dict(self.stage_check),
            "negation_check": json.loads(
                json.dumps(self.negation_check, ensure_ascii=False)),
            "subject_check": dict(self.subject_check),
            "conclusion_check": json.loads(
                json.dumps(self.conclusion_check, ensure_ascii=False)),
            "judge_time_selfcheck": dict(self.judge_time_selfcheck),
            "axes": json.loads(json.dumps(list(self.axes), ensure_ascii=False)),
            "failures": list(self.failures), "ok": self.ok}

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "OrderVerification":
        return cls(
            order=d["order"], decision=d["decision"],
            text_a_sha256=d["text_a_sha256"], text_b_sha256=d["text_b_sha256"],
            judgment=dict(d["judgment"]),
            citations=tuple(CitationSpan.from_dict(c) for c in d["citations"]),
            citation_failures=tuple(dict(f) for f in d["citation_failures"]),
            numeric_check=dict(d["numeric_check"]),
            time_check=dict(d["time_check"]),
            stage_check=dict(d["stage_check"]),
            negation_check=dict(d["negation_check"]),
            subject_check=dict(d["subject_check"]),
            conclusion_check=dict(d["conclusion_check"]),
            judge_time_selfcheck=dict(d["judge_time_selfcheck"]),
            axes=tuple(dict(a) for a in d["axes"]),
            failures=tuple(d["failures"]), ok=d["ok"])


def verify_order_judgment(*, order: str, judgment: Mapping[str, Any],
                          text_a: str, text_b: str) -> OrderVerification:
    """单顺序证明级核验（任务书②③④⑥；decision ∈ {重复, 不重复}）。

    - 重复路：引文 offset 绑定 + 机侧主体/数值/时间/阶段/否定独立核验 +
      结论对拍（任一触发 → ok=False，gate 模式由 llm_residual 降级存疑）；
    - 不重复路：引文 offset 绑定 + 机器可证伪轴（无轴 → P_NO_AXIS，
      证据不足→未决，禁止"模型说不重复"冒充已证排除）。
    """
    if order not in ("hc", "ch"):
        raise ValueError(f"order 非法：{order!r}")
    decision = judgment["decision"]
    if decision not in ("重复", "不重复"):
        raise ValueError(f"证明级核验不覆盖 decision={decision!r}（存疑不送核）")
    citations, citation_failures = _bind_citations(judgment, text_a, text_b)
    numeric = _numeric_check(judgment, text_a, text_b)
    ts_comp = _mv.machine_time_stage_compare(text_a, text_b)
    time_chk = _time_check(judgment, ts_comp)
    stage_chk = {"stage_a": list(ts_comp["stage_a"]),
                 "stage_b": list(ts_comp["stage_b"]),
                 "machine_conflict": ts_comp["stage_conflict"],
                 "backed": not ts_comp["stage_conflict"]}
    neg = _mv.machine_negation_compare(text_a, text_b)
    codes_a = sorted({m["surface"] for m in _mv.subject_code_mentions(text_a)})
    codes_b = sorted({m["surface"] for m in _mv.subject_code_mentions(text_b)})
    subj = {"codes_a": codes_a, "codes_b": codes_b,
            "machine_conflict": _mv.rule_r1_subject_codes(text_a, text_b),
            "backed": not _mv.rule_r1_subject_codes(text_a, text_b)}
    concl = _conclusion_check(judgment)
    jtime = _judge_time_selfcheck(judgment)
    failures: list[str] = []
    if citation_failures:
        failures.append(P_OFFSET)
    axes: tuple[dict, ...] = ()
    if decision == "重复":
        if subj["machine_conflict"]:
            failures.append(P_SUBJECT)
        if numeric["machine_conflict"]:
            failures.append(P_NUMERIC)
        if time_chk["machine_conflict"]:
            failures.append(P_TIME)
        if stage_chk["machine_conflict"]:
            failures.append(P_STAGE)
        if neg["conflict"]:
            failures.append(P_NEGATION)
        if not concl["ok"]:
            failures.append(P_CONCLUSION)
        if not jtime["ok"]:
            failures.append(P_JTIME)
    else:                                    # 不重复：②绑定 + ⑥证伪轴
        axes = _build_falsification_axes(text_a, text_b)
        if not axes:
            failures.append(P_NO_AXIS)
    return OrderVerification(
        order=order, decision=decision,
        text_a_sha256=text_sha256(text_a), text_b_sha256=text_sha256(text_b),
        judgment=dict(judgment), citations=citations,
        citation_failures=citation_failures, numeric_check=numeric,
        time_check=time_chk, stage_check=stage_chk, negation_check=neg,
        subject_check=subj, conclusion_check=concl,
        judge_time_selfcheck=jtime, axes=axes,
        failures=tuple(failures), ok=not failures)


# ---------------------------------------------------------------- 对级证明（⑤⑥）

@dataclass(frozen=True)
class VerifiedJudgeProof:
    """"重复"可签发证明（⑤）。可序列化（to_dict/from_dict）、可复验
    （verify_proof_dict：双文 hash+policy 版本对拍后全量机检重算对拍）。"""
    proof_type: str                          # PROOF_TYPE_DUPLICATE
    pair_id: str
    text_history_sha256: str
    text_current_sha256: str
    model: str
    prompt_version: str
    prompt_sha256: str
    policy_version: str
    final_decision: str                      # "重复"（双序最终判决一致才成立）
    orders: tuple[dict, ...]                 # (hc OrderVerification dict, ch ...)
    issued: bool
    failure_reasons: tuple[str, ...]

    def to_dict(self) -> dict:
        return {
            "proof_type": self.proof_type, "pair_id": self.pair_id,
            "text_history_sha256": self.text_history_sha256,
            "text_current_sha256": self.text_current_sha256,
            "model": self.model, "prompt_version": self.prompt_version,
            "prompt_sha256": self.prompt_sha256,
            "policy_version": self.policy_version,
            "final_decision": self.final_decision,
            "orders": json.loads(json.dumps(list(self.orders),
                                            ensure_ascii=False)),
            "issued": self.issued,
            "failure_reasons": list(self.failure_reasons)}

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "VerifiedJudgeProof":
        return cls(
            proof_type=d["proof_type"], pair_id=d["pair_id"],
            text_history_sha256=d["text_history_sha256"],
            text_current_sha256=d["text_current_sha256"],
            model=d["model"], prompt_version=d["prompt_version"],
            prompt_sha256=d["prompt_sha256"],
            policy_version=d["policy_version"],
            final_decision=d["final_decision"],
            orders=tuple(dict(o) for o in d["orders"]),
            issued=d["issued"],
            failure_reasons=tuple(d["failure_reasons"]))


@dataclass(frozen=True)
class NotDuplicateProof:
    """"不重复"证伪证明（⑥）。双侧证伪轴 + 失败状态枚举；一切失败=未决
    （供 P1-b 按"全部必检候选有效排除"聚合消费——issued=False 的对不得
    计入有效排除）。"""
    proof_type: str                          # PROOF_TYPE_NOT_DUPLICATE
    pair_id: str
    text_history_sha256: str
    text_current_sha256: str
    model: str
    prompt_version: str
    prompt_sha256: str
    policy_version: str
    final_decision: str                      # "不重复"
    axes_union: tuple[str, ...]              # 双序证伪轴名并集（排序，观测用）
    orders: tuple[dict, ...]                 # (hc OrderVerification dict, ch ...)
    issued: bool
    failure: str | None                      # NotDuplicateFailure.value | None
    failure_detail: str

    def to_dict(self) -> dict:
        return {
            "proof_type": self.proof_type, "pair_id": self.pair_id,
            "text_history_sha256": self.text_history_sha256,
            "text_current_sha256": self.text_current_sha256,
            "model": self.model, "prompt_version": self.prompt_version,
            "prompt_sha256": self.prompt_sha256,
            "policy_version": self.policy_version,
            "final_decision": self.final_decision,
            "axes_union": list(self.axes_union),
            "orders": json.loads(json.dumps(list(self.orders),
                                            ensure_ascii=False)),
            "issued": self.issued, "failure": self.failure,
            "failure_detail": self.failure_detail}

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "NotDuplicateProof":
        return cls(
            proof_type=d["proof_type"], pair_id=d["pair_id"],
            text_history_sha256=d["text_history_sha256"],
            text_current_sha256=d["text_current_sha256"],
            model=d["model"], prompt_version=d["prompt_version"],
            prompt_sha256=d["prompt_sha256"],
            policy_version=d["policy_version"],
            final_decision=d["final_decision"],
            axes_union=tuple(d["axes_union"]),
            orders=tuple(dict(o) for o in d["orders"]),
            issued=d["issued"], failure=d["failure"],
            failure_detail=d["failure_detail"])


def build_duplicate_proof(*, pair_id: str, text_history: str,
                          text_current: str,
                          hc: OrderVerification, ch: OrderVerification,
                          model: str, prompt_version: str,
                          prompt_sha256: str) -> VerifiedJudgeProof:
    """⑤ 双序"重复"证明装配：双序原判均"重复" 且 双序核验全过 才签发。"""
    reasons: list[str] = []
    if hc.decision != "重复" or ch.decision != "重复":
        reasons.append("dual_order_not_signed")
    for name, ver in (("hc", hc), ("ch", ch)):
        for f in ver.failures:
            reasons.append(f"{name}:{f}")
    return VerifiedJudgeProof(
        proof_type=PROOF_TYPE_DUPLICATE, pair_id=pair_id,
        text_history_sha256=text_sha256(text_history),
        text_current_sha256=text_sha256(text_current),
        model=model, prompt_version=prompt_version,
        prompt_sha256=prompt_sha256, policy_version=PROOF_POLICY_VERSION,
        final_decision="重复",
        orders=(hc.to_dict(), ch.to_dict()),
        issued=not reasons, failure_reasons=tuple(reasons))


def build_not_duplicate_proof(*, pair_id: str, text_history: str,
                              text_current: str,
                              hc: OrderVerification, ch: OrderVerification,
                              model: str, prompt_version: str,
                              prompt_sha256: str) -> NotDuplicateProof:
    """⑥ 双序"不重复"证伪证明装配。

    签发三条件（缺一→未决，failure 枚举落因）：
    1. 双序原判均"不重复"（DUAL_ORDER_DISAGREEMENT）；
    2. 双序判官引文全部 offset 绑定本侧原文（CITATION_UNBOUND）；
    3. 双序机器可证伪轴**交集**非空——同一族证伪证据在双序各自成立
       （INSUFFICIENT_FALSIFICATION_EVIDENCE：机验未触发/无轴=证据不足，
       禁止"模型两次说不重复"包装成已证排除）。
    """
    ax_hc = {a["axis"] for a in hc.axes}
    ax_ch = {a["axis"] for a in ch.axes}
    axes_union = tuple(sorted(ax_hc | ax_ch))
    failure: NotDuplicateFailure | None = None
    detail = ""
    if hc.decision != "不重复" or ch.decision != "不重复":
        failure = NotDuplicateFailure.DUAL_ORDER_DISAGREEMENT
        detail = f"双序原判非均不重复：hc={hc.decision!r} ch={ch.decision!r}"
    elif hc.citation_failures or ch.citation_failures:
        failure = NotDuplicateFailure.CITATION_UNBOUND
        detail = ("判官引文无法绑定本侧原文 offset："
                  f"hc={len(hc.citation_failures)} 处 ch={len(ch.citation_failures)} 处")
    elif not (ax_hc & ax_ch):
        failure = NotDuplicateFailure.INSUFFICIENT_FALSIFICATION_EVIDENCE
        detail = ("双序无共同机器可证伪轴（主体/数值/时间/阶段/极性差异"
                  "均无可机检证据）——证据不足，未决")
    return NotDuplicateProof(
        proof_type=PROOF_TYPE_NOT_DUPLICATE, pair_id=pair_id,
        text_history_sha256=text_sha256(text_history),
        text_current_sha256=text_sha256(text_current),
        model=model, prompt_version=prompt_version,
        prompt_sha256=prompt_sha256, policy_version=PROOF_POLICY_VERSION,
        final_decision="不重复", axes_union=axes_union,
        orders=(hc.to_dict(), ch.to_dict()),
        issued=failure is None,
        failure=None if failure is None else failure.value,
        failure_detail=detail)


# ---------------------------------------------------------------- 复验（⑤可复验）

def _json_norm(x: Any) -> Any:
    return json.loads(json.dumps(x, ensure_ascii=False, sort_keys=True))


def _reverify_order(order_dict: Mapping[str, Any], expect_order: str,
                    text_a: str, text_b: str) -> OrderVerification | None:
    """按证明自含判定快照重算单顺序核验；结构非法/角色不符 → None。"""
    try:
        recorded = OrderVerification.from_dict(order_dict)
    except (KeyError, TypeError, ValueError):
        return None
    if recorded.order != expect_order:
        return None
    if recorded.decision not in ("重复", "不重复"):
        return None
    recomputed = verify_order_judgment(
        order=expect_order, judgment=recorded.judgment,
        text_a=text_a, text_b=text_b)
    if _json_norm(recomputed.to_dict()) != _json_norm(recorded.to_dict()):
        return None
    return recomputed


def verify_proof_dict(proof: Mapping[str, Any], text_history: str,
                      text_current: str) -> bool:
    """证明复验（⑤可复验；fail-closed 任何不符→False，不猜不补）。

    链式对拍：proof_type/policy 版本 → 双文 hash → 双序核验记录全量重算
    （引文 offset/机检冲突/证伪轴逐项重算并与记录逐字节对拍）→ 对级签发
    标志与失败枚举重算对拍。篡改引文/offset/机检结论/签发标志或换文本
    均 False。
    """
    try:
        if not isinstance(proof, Mapping):
            return False
        ptype = proof.get("proof_type")
        if ptype not in (PROOF_TYPE_DUPLICATE, PROOF_TYPE_NOT_DUPLICATE):
            return False
        if proof.get("policy_version") != PROOF_POLICY_VERSION:
            return False
        if proof.get("text_history_sha256") != text_sha256(text_history):
            return False
        if proof.get("text_current_sha256") != text_sha256(text_current):
            return False
        orders = proof.get("orders")
        if not (isinstance(orders, (list, tuple)) and len(orders) == 2):
            return False
        hc = _reverify_order(orders[0], "hc", text_history, text_current)
        ch = _reverify_order(orders[1], "ch", text_current, text_history)
        if hc is None or ch is None:
            return False
        common = dict(pair_id=proof["pair_id"], text_history=text_history,
                      text_current=text_current, hc=hc, ch=ch,
                      model=proof["model"], prompt_version=proof["prompt_version"],
                      prompt_sha256=proof["prompt_sha256"])
        if ptype == PROOF_TYPE_DUPLICATE:
            rebuilt = build_duplicate_proof(**common)
            return (rebuilt.issued == proof.get("issued")
                    and rebuilt.final_decision == proof.get("final_decision")
                    and list(rebuilt.failure_reasons)
                    == list(proof.get("failure_reasons", [])))
        rebuilt_nd = build_not_duplicate_proof(**common)
        return (rebuilt_nd.issued == proof.get("issued")
                and rebuilt_nd.failure == proof.get("failure")
                and rebuilt_nd.final_decision == proof.get("final_decision")
                and rebuilt_nd.failure_detail == proof.get("failure_detail", "")
                and list(rebuilt_nd.axes_union)
                == list(proof.get("axes_union", [])))
    except (KeyError, TypeError, ValueError):
        return False


__all__ = [
    "PROOF_POLICY_VERSION", "PROOF_TYPE_DUPLICATE", "PROOF_TYPE_NOT_DUPLICATE",
    "JUDGE_PROOF_ENV", "P_OFFSET", "P_NUMERIC", "P_TIME", "P_STAGE",
    "P_NEGATION", "P_SUBJECT", "P_CONCLUSION", "P_JTIME", "P_NO_AXIS",
    "judge_proof_enabled", "text_sha256", "NotDuplicateFailure",
    "CitationSpan", "OrderVerification", "VerifiedJudgeProof",
    "NotDuplicateProof", "verify_order_judgment", "build_duplicate_proof",
    "build_not_duplicate_proof", "verify_proof_dict",
]
