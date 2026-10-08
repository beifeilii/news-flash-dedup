"""P23 残判层机器验（D32 §10 硬化规则 R0-R6 生产化 · 审计模式默认）。

定位与口径裁定（窗口T 反馈节点②，主窗口 2026-09-28 裁定=变体B）：
- 机器验对残判层双向裁判的"重复"判定**全量计算**，但**默认不降级签发**
  （audit）——触发规则入审计槽并参与疑似度排序（llm_residual 侧）；
- 闸门语义（拦截→降级存疑，D32 §10 字面）保留为可配置项
  （llm_residual.ResidualJudgeConfig.mv_mode="gate"，默认 "audit"）——
  D32 数据实证：双向判自身 fp=0/51，闸门边际 fp 收益为零、tp 成本 15
  （净负收益）；影子期以审计槽实测"闸门会拦什么"，证据驱动再定 A/B 终态。
- 规则实现与 log/temp/d32-llm-judge/d32-hardening.py 模拟器逐字同款，
  保证"fp 三对（6e02c930/d27e0196/f74eaded）规则直评被拦 + 误伤数与
  D32 模拟一致（d32-hardening.json：R1=0/R2=3/R3=2/R6=4）"钉值可对拍。

规则总表（D32 报告 §10.1-10.4）：
- R0 闸门纪律：机器验仅闸/验判"重复"的判定（不重复/存疑本已保守，不需闸）。
  本模块纯函数不做决策词检查——由调用方（llm_residual）按 R0 执行调用时机。
- R1 主体锚词对拍（P1 实体混淆）：两侧独立 6 位数字代码集均非空且不相交 → 触发。
- R2 时间锚对拍（P2 时间冲突）：裁判自供 times_a/times_b 均非空、归一
  （去 \\s年月日号）集合不等、并集含定义性阶段词表 → 触发；一方为空不拦
  （金标"缺失≠冲突"）。
- R3 关键数值对拍（P3 附属当核心精确化）：剔除日期型（后随年月日）与
  编号/序数型（前邻"第"、后随号届版）token 后归一集合对拍，仅双向差异向
  （only_a 与 only_b 均非空）才触发。
- R4 单位宽容声称匹配（P4 数字误读拆分）：声称值与原文值差 10^k
  （k∈{2,3,4,8}）且原文该处紧随万/亿/千/百后缀 → 视为命中；% 旗标差异
  同理宽容；无数值 token 的声称项记 warning 不触发；凭空数值 → 触发。
- R5 引文逐字验（P5 引文改写级幻觉，现役真战绩保留）：证据锚（去空白
  ≥4 字符）逐字子串（全空白归一模糊匹配）+ 数值/时间引文逐字。
  §10.3 锚放宽（数字+单位形态即有效锚）登记为后续项——本版与
  d32-hardening.py 模拟器同款（锚 ≥4），保持钉值一致。
- R6 方向词对拍（P6 方向/极性冲突）：反义极性词对两侧分别命中 → 触发。

数值归一工艺移植自 d32_lib.py（千分位/小数/%/万仟佰亿/中文数词节式+点小数）。
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any, Mapping

# ---------------------------------------------------------------- 词表（D32 终版）

STAGE_WORDS = ("初值", "终值", "开盘", "收盘", "同比", "环比", "当年", "次年",
               "选举", "预期", "修正", "投产", "中标", "生效", "复牌", "停牌")
POLARITY_PAIRS = (("上涨", "下跌"), ("高于", "低于"), ("净流入", "净流出"),
                  ("增持", "减持"), ("超预期", "不及预期"), ("走强", "走弱"),
                  ("扩张", "收缩"), ("升值", "贬值"))

_CODE_RE = re.compile(r"(?<!\d)\d{6}(?!\d)")
_NUM_ARABIC = re.compile(r"[+-]?\d+(?:,\d{3})*(?:\.\d+)?\s*(?:%|％|万亿|万|亿|千|百)?")
_NUM_CN = re.compile(r"[零〇一二两三四五六七八九十百千万亿]+(?:点[零〇一二两三四五六七八九]+)?%?")
_CN_DIGIT = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
             "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CN_UNIT = {"十": 10, "百": 100, "千": 1000}
_WS = re.compile(r"\s+")


# ---------------------------------------------------------------- 数值归一（d32_lib 同款）

# winw3f-C4（T090，12号文 L539）：阻断默认 28 位 Decimal context 静默舍入。
# 病灶：归一化的 `Decimal(core) * mult` 与 `value.normalize()` 都吃**环境**
# context 精度（Decimal 构造器本身不舍入，算术与 normalize 才舍入）——
# 默认 prec=28 下 29 位数值即使 mult=1 也被 .normalize() 抹尾，
# 12345678901234567890123456789万 与 …88万 被洗成同一规范串
# （R3 双向差异集洗空=冲突漏检；R4 凭空声称被舍入碰撞放行；
# 红证据 log/temp/winw3f-t090-red-01.txt，探针 winw3f-t090-probe.json 腿 D）。
# 修法裁定（任务②二选一）：契约首选"精确算出即冲突不重复"——局部高精度
# context 精确归一，**不**走 NUMERIC_ALIGNMENT_FAILED 边界：机验归一化无该码
# 通道（非 compare 链 UNRESOLVED_CODES 语义），且预算即操作数数码上界可平凡
# 精确化，无任何"无法支持"情形。≤28 位值零漂移论证：结果本即 28 位内精确
# 表示时，提高局部精度不改变任何输出位（字节级钉 tests/unit/test_winw3f_t090.py
# _SMALL_TOKEN_PINS）。预算 10*len+20：每字符最大量级贡献 亿=10^8≈9 位，
# 逗号/点/字母开销一并覆盖。
def _exact_norm_prec(core: str) -> int:
    """精确归一所需局部精度（操作数数码上界 + 乘位/格式化余量）。"""
    return max(28, 10 * len(core) + 20)


def _cn_to_decimal(text: str) -> Decimal | None:
    """中文数词 → Decimal（十/百/千/万/亿 节式 + 点小数；不可解析返 None）。"""
    text = text.rstrip("%")
    frac = ""
    if "点" in text:
        text, frac = text.split("点", 1)
    total = Decimal(0)
    section = Decimal(0)
    number = 0
    for ch in text:
        if ch in _CN_DIGIT:
            number = _CN_DIGIT[ch]
        elif ch in _CN_UNIT:
            unit = _CN_UNIT[ch]
            section += Decimal(number if number else 1) * unit
            number = 0
        elif ch in ("万", "亿"):
            mult = Decimal(10000 if ch == "万" else 100000000)
            total += (section + number) * mult
            section, number = Decimal(0), 0
        else:
            return None
    value = total + section + number
    if frac:
        digits = []
        for ch in frac:
            if ch not in _CN_DIGIT:
                return None
            digits.append(str(_CN_DIGIT[ch]))
        value += Decimal("0." + "".join(digits))
    return value


def norm_number_token(tok: str) -> str | None:
    """数值 token 归一化为规范串 'v[|p]'（p=百分号标记）；不可解析返 None。"""
    tok = tok.strip().replace(" ", "")
    if not tok:
        return None
    is_pct = tok.endswith(("%", "％"))
    core = tok.rstrip("%％")
    mult = Decimal(1)
    for suffix, m in (("万亿", Decimal(10) ** 12), ("万", Decimal(10) ** 4),
                      ("亿", Decimal(10) ** 8), ("千", Decimal(10) ** 3),
                      ("百", Decimal(10) ** 2)):
        if core.endswith(suffix) and re.search(r"\d", core):
            cand = core[: -len(suffix)]
            try:
                Decimal(cand.replace(",", ""))
                mult = m
                core = cand
                break
            except InvalidOperation:
                continue
    value: Decimal | None = None
    # winw3f-C4（T090）：算术与 normalize 全程局部高精度 context（线程内生效，
    # 退出自动还原环境 context）——_cn_to_decimal 内部算术同受此 budget 覆盖。
    with localcontext() as _ctx:
        _ctx.prec = _exact_norm_prec(core)
        if re.fullmatch(r"[+-]?\d+(?:,\d{3})*(?:\.\d+)?", core):
            try:
                value = Decimal(core.replace(",", "")) * mult
            except InvalidOperation:
                value = None
        elif re.fullmatch(r"[零〇一二两三四五六七八九十百千万亿]+(?:点[零〇一二两三四五六七八九]+)?", core):
            value = _cn_to_decimal(core)
        if value is None:
            return None
        return f"{value.normalize()}{'|p' if is_pct else ''}"


def extract_number_set(text: str) -> set[str]:
    """全文正则抽取数值集（归一化规范串集合）。"""
    out: set[str] = set()
    for rex in (_NUM_ARABIC, _NUM_CN):
        for m in rex.finditer(text):
            v = norm_number_token(m.group(0))
            if v is not None:
                out.add(v)
    return out


def filtered_number_set(text: str) -> set[str]:
    """R3 语境过滤数值集：剔除日期型（后随年月日）与编号/序数型
    （前邻"第"、后随号届版）token。"""
    out: set[str] = set()
    for rex in (_NUM_ARABIC, _NUM_CN):
        for m in rex.finditer(text):
            s, e = m.span()
            after = text[e] if e < len(text) else ""
            before = text[s - 1] if s > 0 else ""
            if after in "年月日" or before == "第" or after in "号届版":
                continue
            v = norm_number_token(m.group(0))
            if v is not None:
                out.add(v)
    return out


# ---------------------------------------------------------------- 引文（d32_lib 同款）

def _strip_ws(s: str) -> str:
    return _WS.sub("", s)


def quote_in_text(quote: str, text: str) -> bool:
    """逐字子串；模糊匹配=全空白归一后子串。"""
    if quote in text:
        return True
    return _strip_ws(quote) in _strip_ws(text)


# ---------------------------------------------------------------- R1-R6 规则（d32-hardening.py 同款）

def rule_r1_subject_codes(text_a: str, text_b: str) -> bool:
    """R1 主体锚词对拍：两侧 6 位数字代码集均非空且不相交 → True（触发）。"""
    ca, cb = set(_CODE_RE.findall(text_a)), set(_CODE_RE.findall(text_b))
    return bool(ca and cb and ca.isdisjoint(cb))


def _norm_time(t: str) -> str:
    return re.sub(r"[\s年月日号]", "", t)


def rule_r2_time_anchor(judgment: Mapping[str, Any]) -> bool:
    """R2 时间锚对拍：times_a/b 均非空、归一集合不等、并集含阶段词 → True。"""
    tc = judgment["time_check"]
    ta, tb = tc["times_a"], tc["times_b"]
    if not ta or not tb:
        return False
    if {_norm_time(x) for x in ta} == {_norm_time(x) for x in tb}:
        return False
    return any(w in x for x in ta + tb for w in STAGE_WORDS)


def rule_r3_numeric_conflict(text_a: str, text_b: str) -> bool:
    """R3 关键数值对拍：过滤后集合仅双向差异向（均非空）→ True。"""
    sa, sb = filtered_number_set(text_a), filtered_number_set(text_b)
    return bool((sa - sb) and (sb - sa))


def rule_r4_ungrounded_claims(judgment: Mapping[str, Any],
                              text_a: str, text_b: str) -> list[dict]:
    """R4 单位宽容声称匹配：返凭空数值声称明细（空=全过/仅 warning）。

    声称值与原文值差 10^k（k∈{2,3,4,8}）且原文该处紧随万亿千百后缀 → 命中；
    % 旗标差异宽容；无数值 token 的声称项跳过（warning，不触发）。
    """
    bad: list[dict] = []
    nc = judgment["numeric_check"]
    for side, claims, text in (("A", nc["numbers_a"], text_a),
                               ("B", nc["numbers_b"], text_b)):
        tset = extract_number_set(text)
        tvals = {v.split("|")[0] for v in tset}
        for q in claims:
            qset = extract_number_set(q)
            if not qset or qset <= tset:
                continue                      # 无数值声称（warning）/ 严格命中
            ok = True
            for v in qset:
                base = v.split("|")[0]
                if base in tvals:
                    continue                  # % 旗标差异，宽容
                dv = Decimal(base)
                mag_ok = False
                # winw3f-C4（T090）：×10^k 与 normalize 同病灶同修法——局部
                # 高精度 context 精确对拍，阻断默认 28 位舍入碰撞放行凭空声称。
                with localcontext() as _ctx:
                    _ctx.prec = _exact_norm_prec(base)
                    for k in (2, 3, 4, 8):
                        cand = dv * (Decimal(10) ** k)
                        if str(cand.normalize()) in tvals:
                            # 原文该数字串后须紧随对应量级后缀
                            qpos = text.find(q)
                            suffix = (text[qpos + len(q):qpos + len(q) + 1]
                                      if qpos >= 0 else "")
                            if suffix in "万亿千百":
                                mag_ok = True
                                break
                if not mag_ok:
                    ok = False
                    break
            if not ok:
                bad.append({"side": side, "quote": q[:60],
                            "claimed": sorted(qset - tset)[:5]})
    return bad


def rule_r5_evidence_verbatim(judgment: Mapping[str, Any],
                              text_a: str, text_b: str) -> list[dict]:
    """R5 引文逐字验（现役真战绩保留，与模拟器同款含锚长检查）。

    证据锚去空白 <4 字符 → evidence_anchor_too_short；否则非逐字 →
    evidence_not_verbatim；数值/时间引文非逐字 → numbers/times_quote_not_verbatim。
    """
    hits: list[dict] = []
    nc = judgment["numeric_check"]
    tc = judgment["time_check"]
    for side, quotes, text in (("A", judgment["evidence_a"], text_a),
                               ("B", judgment["evidence_b"], text_b)):
        for q in quotes:
            if len(_strip_ws(q)) < 4:
                hits.append({"check": "evidence_anchor_too_short", "side": side,
                             "quote": q[:60]})
            elif not quote_in_text(q, text):
                hits.append({"check": "evidence_not_verbatim", "side": side,
                             "quote": q[:60]})
    for tag, pairs in (("numbers", (("A", nc["numbers_a"], text_a),
                                    ("B", nc["numbers_b"], text_b))),
                       ("times", (("A", tc["times_a"], text_a),
                                  ("B", tc["times_b"], text_b)))):
        for side, quotes, text in pairs:
            for q in quotes:
                if not quote_in_text(q, text):
                    hits.append({"check": f"{tag}_quote_not_verbatim", "side": side,
                                 "quote": q[:60]})
    return hits


def rule_r6_polarity_conflict(text_a: str, text_b: str) -> bool:
    """R6 方向词对拍：反义极性词对两侧分别命中 → True。"""
    for w1, w2 in POLARITY_PAIRS:
        if (w1 in text_a and w2 in text_b) or (w2 in text_a and w1 in text_b):
            return True
    return False


# ---------------------------------------------------------------- 组合（模拟器同款顺序）

def verify_signed_judgment(judgment: Mapping[str, Any],
                           text_a: str, text_b: str) -> list[dict]:
    """对一秩序判"重复"的判定跑组合硬化机验（R5+R4+R3+R1+R2+R6，模拟器同款顺序）。

    R0 纪律（仅闸/验"重复"判定）由调用方执行；本函数假定输入判定 decision=="重复"。
    返触发明细列表（空=全过），每条 {"rule": "R1"..|"R6", ...明细}，确定性可序列化。
    """
    fired: list[dict] = []
    r5 = rule_r5_evidence_verbatim(judgment, text_a, text_b)
    if r5:
        fired.append({"rule": "R5", "checks": r5})
    r4 = rule_r4_ungrounded_claims(judgment, text_a, text_b)
    if r4:
        fired.append({"rule": "R4", "claims": r4})
    if rule_r3_numeric_conflict(text_a, text_b):
        sa, sb = filtered_number_set(text_a), filtered_number_set(text_b)
        fired.append({"rule": "R3",
                      "only_a": sorted(sa - sb)[:5], "only_b": sorted(sb - sa)[:5]})
    if rule_r1_subject_codes(text_a, text_b):
        fired.append({"rule": "R1",
                      "codes_a": sorted(set(_CODE_RE.findall(text_a))),
                      "codes_b": sorted(set(_CODE_RE.findall(text_b)))})
    if rule_r2_time_anchor(judgment):
        tc = judgment["time_check"]
        fired.append({"rule": "R2", "times_a": list(tc["times_a"]),
                      "times_b": list(tc["times_b"])})
    if rule_r6_polarity_conflict(text_a, text_b):
        pairs = [list(p) for p in POLARITY_PAIRS
                 if (p[0] in text_a and p[1] in text_b)
                 or (p[1] in text_a and p[0] in text_b)]
        fired.append({"rule": "R6", "polarity_pairs": pairs})
    return fired


# ---------------------------------------------------------------- P1-a 证明级机检原语
#
# 2026-10-09 新增（P1-a 施工窗）。出处：log\判定链改造最终方案-Codex-20261008.md
# §4 P1-a（"引文绑定原文 offset；机器独立核关键时间/数值及判定与分项结论"）+
# log\判定链改造-终裁令-正典-1009.md §五-2/3。本区全部为**增量**函数——上方
# R1-R6 现役规则一字未动（audit 旧路逐字节锚）；证明级核验只在
# DEDUP_JUDGE_PROOF 开关开启的新路上消费（decide/judge_proof.py）。
#
# 与现役口径的三处原则差异（任务书②③）：
# - 引文定位=精确逐字子串的 Unicode 码点 offset；现役 quote_in_text 的去空白
#   模糊匹配在证明路上**拒签**（去空白伪匹配不得充当签发证据）。
# - 时间/阶段由机器从原文独立抽取比对（extract_time_mentions/extract_stage_set），
#   不信判官 time_check 供数；判官声称一致处须机器无冲突背书（judge_proof 侧
#   backed 字段）。一侧缺失放行=缺失≠冲突（金标口径照承 R2/R3 纪律）。
# - 数值复用现役 filtered_number_set/rule_r3_numeric_conflict（本就是机侧
#   从原文抽取），仅补表面形+offset 抽取（number_mentions）供证明引用定位。

NEGATION_TOKENS = ("否认", "不予", "不再", "并无", "并未", "未有", "取消",
                   "终止", "否决")

# 证明路阶段词表（2026-10-09 policy_v2 对齐，宪章 §二-3 枚举：开盘/收盘、
# 同比/环比、当日/次日、初值/终值、当年/次年）=现役 STAGE_WORDS + 宪章补员
# （当日/次日）。现役 STAGE_WORDS 一字不动（R2 旧路锚），本表仅证明路消费。
PROOF_STAGE_WORDS = STAGE_WORDS + ("当日", "次日")

# 相对时间词族（宪章 C14/T-3 生效口径：含"今日/昨日/当年"等相对时间且
# 双侧无共同绝对锚点 → 即使同文也按边界，不得签"重复"）。闭合词表=
# 宪章示例（今日/昨日/当年）+ §二-3 列员（当日/次日）+ 直系同族（明日/今年）。
RELATIVE_TIME_TOKENS = ("今日", "昨日", "当年", "当日", "次日", "明日", "今年")

# 机器时间抽取（闭合形态：YYYY年M月D日[号]/M月D日[号]/YYYY-M-D/YYYY/M/D；
# 归一复用 _norm_time 去\s年月日号）。阶段词复用 STAGE_WORDS 闭合词表。
_TIME_DATE_RE = re.compile(
    r"\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*[日号]"
    r"|\d{1,2}\s*月\s*\d{1,2}\s*[日号]"
    r"|\d{4}[-/]\d{1,2}[-/]\d{1,2}")


def find_quote_spans(quote: str, text: str) -> tuple[tuple[int, int], ...]:
    """精确逐字子串的全部 Unicode 码点 offset 区间 (start, end)；无命中返空元组。

    P1-a ②：证明路只认精确逐字——去空白归一后的伪匹配在本函数无通道（拒签由
    judge_proof 侧按空命中落实）。offset 为 Python str 码点下标（左闭右开）。
    """
    if not quote:
        return ()
    spans: list[tuple[int, int]] = []
    start = 0
    while True:
        pos = text.find(quote, start)
        if pos < 0:
            break
        spans.append((pos, pos + len(quote)))
        start = pos + 1
    return tuple(spans)


def extract_stage_set(text: str) -> set[str]:
    """机器独立抽取阶段词集（证明路词表 PROOF_STAGE_WORDS=STAGE_WORDS+宪章
    §二-3 补员 当日/次日，policy_v2 对齐；不信判官供数）。"""
    return {w for w in PROOF_STAGE_WORDS if w in text}


def extract_time_mentions(text: str) -> tuple[dict, ...]:
    r"""机器独立抽取日期型时间表述：({"surface","start","end","norm"},...)。

    norm=_norm_time(surface)（去\s年月日号）。顺序=文中出现顺序（确定性）。
    """
    out: list[dict] = []
    for m in _TIME_DATE_RE.finditer(text):
        surface = m.group(0)
        out.append({"surface": surface, "start": m.start(), "end": m.end(),
                    "norm": _norm_time(surface)})
    return tuple(out)


def extract_time_set(text: str) -> set[str]:
    """机器时间集（归一化规范串集合）。"""
    return {m["norm"] for m in extract_time_mentions(text)}


def number_mentions(text: str) -> tuple[dict, ...]:
    """数值 token 表面形+offset+归一：({"surface","start","end","norm"},...)。

    过滤口径与 filtered_number_set 同款（剔除日期型=后随年月日、编号/序数型=
    前邻"第"、后随号届版）——证明路数值引用的 offset 定位与证伪轴取词用；
    现役 filtered_number_set 本体一字未动。
    """
    out: list[dict] = []
    for rex in (_NUM_ARABIC, _NUM_CN):
        for m in rex.finditer(text):
            s, e = m.span()
            after = text[e] if e < len(text) else ""
            before = text[s - 1] if s > 0 else ""
            if after in "年月日" or before == "第" or after in "号届版":
                continue
            v = norm_number_token(m.group(0))
            if v is not None:
                out.append({"surface": m.group(0), "start": s, "end": e,
                            "norm": v})
    return tuple(out)


def subject_code_mentions(text: str) -> tuple[dict, ...]:
    """主体锚代码（R1 同口径 6 位数字）表面形+offset：证明引用定位用。"""
    return tuple({"surface": m.group(0), "start": m.start(), "end": m.end()}
                 for m in _CODE_RE.finditer(text))


def machine_time_stage_compare(text_a: str, text_b: str) -> dict:
    """R2 证明级替代：时间/阶段由机器从原文独立抽取比对（P1-a ③）。

    policy_v2 对齐（宪章 §一-3/§二-3，2026-10-09 生效）：
    - time_conflict：双侧机抽时间集均非空且归一不等 → **直接**冲突
      （宪章"时间/阶段可识别差异"为不重复直接事由，判"重复"须时间归一
      一致；此前"阶段词在场"限定词超出宪章放宽，policy_v2 起删除）；
      一侧缺失放行=缺失≠冲突（宪章"时间缺失例外"）。
    - stage_conflict：双侧机抽阶段词集均非空且不等（开盘/收盘、同比/
      环比、当日/次日、初值/终值、当年/次年——宪章 §二-3 枚举族）。
    """
    ta, tb = extract_time_set(text_a), extract_time_set(text_b)
    sa, sb = extract_stage_set(text_a), extract_stage_set(text_b)
    time_conflict = bool(ta and tb and ta != tb)
    stage_conflict = bool(sa and sb and sa != sb)
    return {"times_a": sorted(ta), "times_b": sorted(tb),
            "stage_a": sorted(sa), "stage_b": sorted(sb),
            "time_conflict": time_conflict, "stage_conflict": stage_conflict}


def machine_relative_time_anchor_check(text_a: str, text_b: str) -> dict:
    """相对时间无锚点机检（宪章 C14 + T-3 生效处置，policy_v2 对齐）。

    block=True 当且仅当：任一侧含相对时间词族（RELATIVE_TIME_TOKENS）
    且双侧无共同绝对锚点（机抽日期集交集为空）——此时即使两条正文完全
    相同也不得签"重复"（按边界保守口径）；不得用系统接收时间/发布时间/
    另一条正文替其补日期（本函数只看正文，结构性合规）。
    """
    ra = sorted({t for t in RELATIVE_TIME_TOKENS if t in text_a})
    rb = sorted({t for t in RELATIVE_TIME_TOKENS if t in text_b})
    shared = bool(extract_time_set(text_a) & extract_time_set(text_b))
    return {"relative_tokens_a": ra, "relative_tokens_b": rb,
            "shared_absolute_anchor": shared,
            "block": bool((ra or rb) and not shared)}


def machine_negation_compare(text_a: str, text_b: str) -> dict:
    """否定/方向证明级机检：R6 极性词对 + 否定词族（NEGATION_TOKENS 闭合
    多字词表）单侧不对称 → conflict（P1-a"否定"检查族；判"重复"须两侧
    极性/否定态势一致）。"""
    na = sorted({t for t in NEGATION_TOKENS if t in text_a})
    nb = sorted({t for t in NEGATION_TOKENS if t in text_b})
    pol = [list(p) for p in POLARITY_PAIRS
           if (p[0] in text_a and p[1] in text_b)
           or (p[1] in text_a and p[0] in text_b)]
    asymmetry = set(na) != set(nb)
    return {"polarity_pairs": pol, "negation_a": na, "negation_b": nb,
            "negation_asymmetry": asymmetry,
            "conflict": bool(pol) or asymmetry}


__all__ = [
    "STAGE_WORDS", "POLARITY_PAIRS", "NEGATION_TOKENS",
    "norm_number_token", "extract_number_set", "filtered_number_set",
    "quote_in_text",
    "rule_r1_subject_codes", "rule_r2_time_anchor", "rule_r3_numeric_conflict",
    "rule_r4_ungrounded_claims", "rule_r5_evidence_verbatim",
    "rule_r6_polarity_conflict", "verify_signed_judgment",
    # 2026-10-09 P1-a 证明级机检原语（增量；旧 audit 路径零消费）
    "PROOF_STAGE_WORDS", "RELATIVE_TIME_TOKENS",
    "find_quote_spans", "extract_stage_set", "extract_time_mentions",
    "extract_time_set", "number_mentions", "subject_code_mentions",
    "machine_time_stage_compare", "machine_negation_compare",
    "machine_relative_time_anchor_check",
]
