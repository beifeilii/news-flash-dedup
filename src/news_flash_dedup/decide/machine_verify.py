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


__all__ = [
    "STAGE_WORDS", "POLARITY_PAIRS",
    "norm_number_token", "extract_number_set", "filtered_number_set",
    "quote_in_text",
    "rule_r1_subject_codes", "rule_r2_time_anchor", "rule_r3_numeric_conflict",
    "rule_r4_ungrounded_claims", "rule_r5_evidence_verbatim",
    "rule_r6_polarity_conflict", "verify_signed_judgment",
]
