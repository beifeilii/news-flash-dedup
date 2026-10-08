"""判官 prompt v4（宪章 policy_v2 全文落地版）——2026-10-09 P2 先行件③ 新建。

正典/出处：《判定宪章-草案-v2-1009.md》（业务方 2026-10-09 审签，
policy_version = policy_v2 生效，含 §七 待确认项默认处置 T-1/T-2/T-3）；
《判定链改造-终裁令-正典-1009.md》§二 正典栈（宪章=判定口径唯一来源）。

纪律声明：
- **纯新增文件，不 patch v3**——decide/llm_residual.py 的 judge_v1/v2/v3
  与 _JUDGE_PROMPTS 注册处逐字节原样（v3 可切回）；v4 接入注册处与
  ResidualJudgeConfig.prompt_version 四态属主线合流时接线（llm_residual.py
  为 P2 禁区，本批不动），接线点见完工报告。
- 条款锚点：prompt 正文逐条携带宪章 §/C 编号，供审计对拍"判官指令 =
  宪章条款改写"（宪章 §六-1：判官、标注员、机检三方同据此书）。
- policy_version 字面量单源 = lib/run_manifest.POLICY_VERSION_V2
  （"policy_v2"），prompt 文本经插值携带，禁止手抄漂移。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

from news_flash_dedup.lib.run_manifest import POLICY_VERSION_V2

JUDGE_PROMPT_VERSION_V4 = "judge_v4"
POLICY_VERSION = POLICY_VERSION_V2

# 三态判定词 = 宪章 §〇-3 原词（与既有链五字段契约词汇不同族——v4 为判官
# 输出契约层，未经接线不进入生产判定链）。
VERDICTS_V4 = ("重复", "不重复", "边界case")

# not_duplicate 证伪要素闭合枚举（宪章 §二 六项差异类型对应）。
FALSIFICATION_ELEMENTS = (
    "subject",         # §二-1 核心主体完全不一致（C07）
    "core_numeric",    # §二-2 任一对应核心数值差异（C05）
    "time_stage",      # §二-3 时间/阶段可识别差异（C06）
    "key_object",      # §二-4 关键对象不同
    "direction",       # §二-5 方向冲突
    "atomic_event",    # §二-6 未对齐的独立原子事件（C09）
)

# 输出契约示例（契约骨架即合法实例；嵌入 prompt 正文供判官照排，单测钉
# 其可解析+过 validate_judge_v4_output）。
JUDGE_V4_OUTPUT_EXAMPLE: dict[str, Any] = {
    "verdict": "不重复",
    "policy_version": POLICY_VERSION,
    "quotes": [
        {"side": "A", "start": 0, "end": 9, "text": "甲公司9月20日公告"},
        {"side": "B", "start": 0, "end": 5, "text": "乙公司公告"},
    ],
    "three_directions": {
        "subject": {"consistent": False, "note": "A 侧主体甲公司，B 侧主体乙公司"},
        "core_numeric": {"consistent": None, "note": "双侧均无对应核心数值"},
        "event": {"consistent": False, "note": "主体不同即主体—事件组合不同"},
    },
    "falsification": {
        "element": "subject",
        "evidence": "A 侧核心主体为甲公司，B 侧为乙公司，完全不一致（C07）",
    },
    "self_check": {
        "quotes_verbatim": True,
        "offsets_valid": True,
        "dual_order_consistent": True,
        "conclusion": "自检通过：引文逐字、offset 在原文可定位、结论与 A/B 顺序无关",
    },
    "reason": "核心主体完全不一致（C07，§二-1），判不重复。",
}

_CONTRACT_EXAMPLE_JSON = json.dumps(
    JUDGE_V4_OUTPUT_EXAMPLE, ensure_ascii=False, indent=2, sort_keys=True)

# 宪章 policy_v2 全文改写为判官指令（§〇/§一/§二/§三 全条款 + §四/§五/§六
# 相关纪律 + §七 T-1/T-3 默认处置 + 双序一致要求）。
JUDGE_PROMPT_V4 = f"""你是快讯文本去重判定裁判（policy_version={POLICY_VERSION}，《判定宪章》policy_v2 为判定口径唯一来源）。给定同一新闻域内两条快讯正文（文本A、文本B），只回答一个问题：它们说的是不是同一个现实世界事实？判定拆成三个方向（宪章 §〇-1）：**主体｜核心数值｜事件**（事件=发生了什么＋处于哪个时间/阶段＋针对哪个关键对象）。

【总纲（§〇）】
1. 判定唯一输入=快讯正文（C01/C02）：不使用标题、发布时间、系统接收时间、来源渠道等正文外信息，也不用任何正文外来源替正文补日期。
2. 误删是最高风险（C04，§〇-2）：只有确认重复才判重复；咬不准一律按不重复保留或进边界，绝不错杀。漏判重复可接受，误判重复不可接受。
3. 三态输出（§〇-3）：重复 / 不重复 / 边界case。边界是合法产出，不得为追求自动化率强判。
4. 指标口径（§五，C13）：优先保障重复类别精确率——置信不足一律不判重复。

【判"重复"（§一，须同时全部成立）】
1. 核心主体一致（C07）。
2. 对应核心数值完全一致（C05）：金额、价格、比例、数量、涨跌幅，任何对应数值存在差异（哪怕小数点后一位）即不得判重复；仅可证明为无损展示格式的差异（如尾零）可归一。数值缺失例外：一方缺少对应数值、其余核心要素完全一致 → 可判重复（缺失≠差异）。
3. 时间/阶段一致（C06）：正文可识别的时间表述经归一后一致。时间缺失例外：一方无时间表述、其余核心要素一致 → 可判重复。
4. 核心事件一致（C08）：同义改写、动词替换（维护/捍卫类）、信息补充/缺失，不影响判重复。
5. 长文多原子事件（C09）：先拆成独立原子事件逐一对齐；缺失的是同一原子事件内部的属性/背景/解释 → 可判重复。

【判"不重复"（§二，满足其一即可）】
1. 核心主体完全不一致（C07）。
2. 任一对应核心数值存在差异（C05）。
3. 时间/阶段可识别差异（C06）：开盘/收盘、同比/环比、当日/次日、初值/终值、当年/次年。
4. 关键对象不同（如 日本20Y/10Y国债、燃料油/低硫燃料油）。
5. 方向冲突（如 上涨/下跌、净流入/净流出）。
6. 长文中存在未对齐的独立原子事件（C09）：已对齐原子事件的主体与对方不一致 → 整体直接不重复；缺失内容构成新的"主体—事件—关键对象"组合、可独立成一条快讯 → 不重复并保留整条（§七 T-1 默认处置）。

【必须进"边界case"（§三，不得自动签）】
1. 主体缺失（C07）：一方抽不出核心主体，即便其余要素一致 → 边界。
2. 相对时间无锚点（C14）：正文含"今日/昨日/当年"等相对时间且正文自身无共同绝对锚点时——**即使两条正文完全相同**也先出边界并 KEEP；不得用系统接收时间/发布时间/另一条正文替其补日期（§七 T-3 默认：边界+KEEP 保守口径）。
3. 无事实载体文本（空文本/仅"（产联社）"式信源尾注）：按 C10 走普通规则 → 因主体缺失自然落边界；不单独判"不通过"。
4. 判官置信不足、双向判不一致、机检未通过、证据不足 → 边界（C04/C13）。

【双序一致要求】你的判定不得依赖"文本A/文本B"的先后标签：同一对文本交换顺序后结论必须相同。调用方将以两种顺序各判一次并核验一致；两顺序结论不一致即落边界（§三-4 双向判不一致）。凡你对顺序敏感、拿不准是否一致，直接出边界case。

【输出与投递纪律（§四，C12）】你只产出三态判定与理由，不产出投递动作：判定"重复"不必然等于抑制展示（decision≠action），KEEP/SUPPRESS 由下游 action 字段决定，不在你的输出内。

【输出契约】只输出一个 JSON 对象，禁止任何其他文字、解释或 markdown 围栏。字段：
- "verdict"："重复" | "不重复" | "边界case"；
- "policy_version"：固定填 "{POLICY_VERSION}"；
- "quotes"：判定依据引文数组，双侧至少各 1 条，每条 = {{"side": "A"|"B", "start": 整数, "end": 整数, "text": 引文}}——text 必须是对应原文的连续逐字片段（至少 4 字符，禁止改写/翻译/拼接/补全），start/end 为该片段在对应原文中的码点 offset（0 起、end 排他），须可被原文 [start:end] 切片精确复现；
- "three_directions"：三方向逐项结论——"subject"/"core_numeric"/"event" 各为 {{"consistent": true|false|null, "note": "简述"}}（null=一方缺失或不可判）；
- "falsification"：verdict="不重复" 时**必填**的证伪证据——{{"element": "subject"|"core_numeric"|"time_stage"|"key_object"|"direction"|"atomic_event", "evidence": "哪一侧哪个要素证伪了同一事实（须能落到 quotes）"}}；verdict 为其余两态时填 null；
- "self_check"：自检结论——{{"quotes_verbatim": true, "offsets_valid": true, "dual_order_consistent": true, "conclusion": "自检通过/未过及原因"}}；任一自检项不成立即不得输出"重复"；
- "reason"：判定理由（200 字以内，含宪条锚点如 C05/§二-3）。

合法输出示例（形态照排，内容按实际对）：
{_CONTRACT_EXAMPLE_JSON}

【用户消息格式】
【文本A】
<文本A全文>

【文本B】
<文本B全文>"""

PROMPT_SHA256_V4 = hashlib.sha256(JUDGE_PROMPT_V4.encode("utf-8")).hexdigest()


def _is_int(value: Any) -> bool:
    return type(value) is int


def validate_judge_v4_output(payload: Mapping[str, Any]) -> tuple[dict | None, str | None]:
    """v4 判官输出结构校验（契约执法；形态对齐 llm_residual.validate_judge）。

    返 (规范化 dict, None) 或 (None, 错误串)。引文 offset 与原文逐字
    对拍（text == 原文[start:end]）属机检职责（宪章 §六-4：机检是
    "重复"结论的录取线），本层只验结构与闭合枚举。
    """
    if not isinstance(payload, Mapping):
        return None, "输出非 JSON 对象"
    verdict = payload.get("verdict")
    if verdict not in VERDICTS_V4:
        return None, f"verdict 非法：{verdict!r}（闭合三态 {VERDICTS_V4}）"
    if payload.get("policy_version") != POLICY_VERSION:
        return None, f"policy_version 非法：{payload.get('policy_version')!r}"
    quotes = payload.get("quotes")
    if (not isinstance(quotes, list) or not quotes
            or not any(isinstance(q, Mapping) and q.get("side") == "A" for q in quotes)
            or not any(isinstance(q, Mapping) and q.get("side") == "B" for q in quotes)):
        return None, "quotes 缺失或双侧未各至少 1 条"
    for quote in quotes:
        if not isinstance(quote, Mapping):
            return None, "quotes 元素非对象"
        if quote.get("side") not in ("A", "B"):
            return None, f"quotes.side 非法：{quote.get('side')!r}"
        start, end, text = quote.get("start"), quote.get("end"), quote.get("text")
        if not _is_int(start) or not _is_int(end) or not 0 <= start < end:
            return None, f"quotes offset 非法：start={start!r} end={end!r}"
        if not isinstance(text, str) or len(text.strip()) < 4:
            return None, "quotes.text 缺失或不足 4 字符（须逐字片段）"
    directions = payload.get("three_directions")
    if not isinstance(directions, Mapping):
        return None, "three_directions 缺失"
    for key in ("subject", "core_numeric", "event"):
        entry = directions.get(key)
        if not isinstance(entry, Mapping) or entry.get("consistent") not in (True, False, None):
            return None, f"three_directions.{key}.consistent 非法（须 true|false|null）"
        if not isinstance(entry.get("note"), str) or not entry["note"].strip():
            return None, f"three_directions.{key}.note 缺失"
    falsification = payload.get("falsification")
    if verdict == "不重复":
        if not isinstance(falsification, Mapping):
            return None, "verdict=不重复 时 falsification 必填（证伪证据）"
        if falsification.get("element") not in FALSIFICATION_ELEMENTS:
            return None, f"falsification.element 非法：{falsification.get('element')!r}"
        if not isinstance(falsification.get("evidence"), str) or not falsification["evidence"].strip():
            return None, "falsification.evidence 缺失"
    elif falsification is not None:
        return None, "verdict≠不重复 时 falsification 必须为 null"
    self_check = payload.get("self_check")
    if not isinstance(self_check, Mapping):
        return None, "self_check 缺失（自检结论必填）"
    for flag in ("quotes_verbatim", "offsets_valid", "dual_order_consistent"):
        if self_check.get(flag) is not True:
            return None, f"self_check.{flag} 非 true"
    if not isinstance(self_check.get("conclusion"), str) or not self_check["conclusion"].strip():
        return None, "self_check.conclusion 缺失"
    reason = payload.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        return None, "reason 缺失"
    return {
        "verdict": verdict,
        "policy_version": payload["policy_version"],
        "quotes": [
            {"side": q["side"], "start": q["start"], "end": q["end"],
             "text": q["text"]}
            for q in quotes
        ],
        "three_directions": {
            key: {"consistent": directions[key]["consistent"],
                  "note": directions[key]["note"].strip()}
            for key in ("subject", "core_numeric", "event")
        },
        "falsification": (
            None if falsification is None else {
                "element": falsification["element"],
                "evidence": falsification["evidence"].strip()}
        ),
        "self_check": {
            "quotes_verbatim": True, "offsets_valid": True,
            "dual_order_consistent": True,
            "conclusion": self_check["conclusion"].strip(),
        },
        "reason": reason.strip(),
    }, None


__all__ = [
    "FALSIFICATION_ELEMENTS",
    "JUDGE_PROMPT_V4",
    "JUDGE_PROMPT_VERSION_V4",
    "JUDGE_V4_OUTPUT_EXAMPLE",
    "POLICY_VERSION",
    "PROMPT_SHA256_V4",
    "VERDICTS_V4",
    "validate_judge_v4_output",
]
