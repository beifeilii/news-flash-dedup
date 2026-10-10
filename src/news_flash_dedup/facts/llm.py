"""LLM 式 P14 事实抽取器（主窗口 2026-09-27 04:2x，决策包 A 首件，N3 原型）。

定位：B2 规则基线（rule.py）的可替换升级供给——同一输出形状（G2 封闭键集）、
同一 evidence 硬自检纪律（复用 rule._present/_ev 构造期自证），仅"识别"由
DashScope 兼容端点 chat completions 承担。诚实标注：

- **LLM 只做"识别"，不做"定位"**：提示词要求全部字符串逐字引自原文；
  偏移由本模块在原文中实搜回填——任何引词在原文中找不到 → 该槽 missing +
  issue 登记（宁缺勿编，幻觉字符串零机会进入 evidence）。
- **evidence 自证与 B2 同级**：每个 span 满足 text[start:end]==quote（构造期
  不满足即 LlmExtractionError，fail-fast 不静默）。
- **预算（N3 原型口径）**：单次 30s 超时、瞬时错误重试 2 次（线性退避
  1.5s×(attempt+1)；M-12/L 窗口P 订正：原误书"指数退避"）；
  全量故障 → LlmExtractionError（回放按 INV-4 记 failures，不冒充边界）。
- **磁盘缓存**：cache_dir/<text_sha256>.json，键含 model+prompt_version——
  复跑零调用零费用；缓存即证据（每文的原始 LLM JSON 留痕）。
  **零保护（R3-M2，W-R3b）**：零可定位事实的抽取结果（全幻觉 raw / 诚实空
  facts）**不落缓存**——幻觉 raw 钉入缓存即永久复用、再无重审机会；拒写
  随行告警计数（zero_facts_cache_skip_count）+ logging + 侧车 issue 登记。
- key 来源：环境变量 QWEN_API_KEY（用户 2026-09-27 04:0x 授权明文使用
  mabc_service/.env 之 key；回放 wiring 从该 .env 现读注入，证据文档掩码）。

已知限制（v1）：同词多义定位取偏好窗内首个命中（登记）；中文数词 value=None
后置解析同 B2（09 §7.2）；polarity/modality 依赖模型原词片段质量。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
import urllib.request
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Mapping

from .rule import (_RECORD_ID_RE, _decimal_text, _ev, _missing, _present)

_log = logging.getLogger(__name__)

LLM_PROMPT_VERSION = "p14_llm_prompt_v2"
DEFAULT_MODEL = "qwen-plus"  # 2026-10-10 用户令：qwen-turbo 下架，直换 qwen-plus（同上豁免）
DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"

# R3-M2（W-R3b）：零可定位事实抽取结果的缓存拒写告警计数（进程内单调只增，
# 不回零；回放/测试经 zero_facts_cache_skip_count() 实核）。
_ZERO_FACTS_CACHE_SKIPS = 0


def zero_facts_cache_skip_count() -> int:
    """R3-M2 告警计数：因零可定位事实被拒写缓存的抽取次数（单调只增）。"""
    return _ZERO_FACTS_CACHE_SKIPS

_SYSTEM_PROMPT = (
    "你是新闻快讯事实抽取器。对给定快讯文本抽取全部事件事实，只输出 JSON，"
    "禁止任何额外文字。输出格式：\n"
    '{"facts":[{"subject":str|null,"predicate":str,"polarity":str|null,'
    '"modality":str|null,"key_object":str|null,"time_expression":str|null,'
    '"time_stage":str|null,"attribution":str|null,'
    '"numerics":[{"value_raw":str,"metric":str|null,"unit":str|null,'
    '"magnitude":str|null,"comparator":str|null,"range_end_raw":str|null,'
    '"direction":str|null}]}]}\n'
    "规则：\n"
    "1. 所有字符串必须逐字摘自原文，禁止改写/翻译/补全/规范化为原文没有的写法；"
    "允许截取原文的连续片段（如原文「将立刻终止」可截「终止」），但禁止拼接"
    "（如「将终止」在原文「将立刻终止」中不是连续子串，属违规）。\n"
    "2. predicate=事件动词原词（连续子串）；polarity=含否定或完成前缀的极性片段"
    "原词（如「并未回购」「已增持」），无前缀时与 predicate 相同。\n"
    "3. modality=拟/将/传闻等情态词原词，无则 null。\n"
    "4. 未明确表达的字段一律 null，宁缺勿编。\n"
    "5. 一个动词一个事实；同句多动词拆多事实；无事件动词则输出空 facts。\n"
    "6. 【报道内容也是事件】报道动词（表示/称/宣布/说等）所报道的实质内容"
    "本身必须单独抽为一个事实，含其自身的主体/时间/条件成分（如「特朗普表示，"
    "中期选举后伊朗战争将立刻终止」除「表示」事实外，还须抽「伊朗战争将立刻终止」"
    "事实，其 time_expression=「中期选举后」）。\n"
    "7. numerics 归属最相关的事实；value_raw 保留原文写法（含千分位、中文数词、%）。\n"
    "8. comparator 仅取 约/超/超过/近/不足 之类的比较词原词，无则 null。"
)


class LlmExtractionError(ValueError):
    """LLM 抽取不可恢复失败（调用失败/JSON 非法/evidence 自检失败）。"""


def _default_call_fn(*, model: str, base_url: str, api_key: str,
                     text: str, timeout_s: float) -> tuple[str, float]:
    """DashScope 兼容端点 chat completions（urllib 零新依赖）；返 (content, 秒)。"""
    body = json.dumps({
        "model": model,
        "messages": [{"role": "system", "content": _SYSTEM_PROMPT},
                     {"role": "user", "content": text}],
        "response_format": {"type": "json_object"},
        "temperature": 0,
        "seed": 42,
    }).encode("utf-8")
    req = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions", data=body,
        headers={"Authorization": f"Bearer {api_key}",
                 "Content-Type": "application/json"})
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        # P2 工程债 C-12（窗口X 并线移植）：响应体 1MB 硬上限（读上限+1 判
        # 溢出，超长抛错不静默截断）——合法 chat completions 响应远小于此；
        # 无界 read 让失控端点可耗尽内存。LlmExtractionError 不重试
        # （确定性失败）。
        raw = resp.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise LlmExtractionError(
                f"chat 响应体超过 1MB 硬上限（已读 {len(raw)} 字节）")
        # W2Fα1（WA1a-D7③）：坏 UTF-8/坏 JSON=确定性失败——包装为
        # LlmExtractionError（不重试纪律同 1MB 上限）；修复前裸
        # UnicodeDecodeError/JSONDecodeError 被 _call_with_retries 当瞬时
        # 错误重试（同响应确定性重放，纯浪费预算）。
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise LlmExtractionError(
                f"chat 响应体非合法 UTF-8/JSON（确定性失败不重试）：{exc}") from exc
    latency = time.perf_counter() - t0
    try:
        return payload["choices"][0]["message"]["content"], latency
    except (KeyError, IndexError, TypeError) as exc:
        raise LlmExtractionError(f"chat 响应形态异常：{str(payload)[:200]!r}") from exc


def _call_with_retries(call_fn, *, retries: int, **kwargs) -> tuple[str, float]:
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            return call_fn(**kwargs)
        except LlmExtractionError:
            raise
        except Exception as exc:                      # 网络/HTTP/超时等瞬时错误
            last = exc
            if attempt == retries:
                break
            time.sleep(1.5 * (attempt + 1))
    raise LlmExtractionError(f"LLM 调用失败（重试 {retries} 次后）：{last!r}")


def _call_with_permit(call_fn, *, permit: "LogicalCallPermit", **kwargs) -> tuple[str, float]:
    """W2 ⑩①-6（N43 挂账清偿）：预算件驱动重试环——runtime_budget L15
    '失败或成功均扣模型额度，重试必须经共享追加预算授权'：每尝试由
    permit.next_attempt() 授权+供给截断 timeout_s（kwargs 内现役
    timeout_s 被覆盖——预算在场即预算说了算，不混用）；共享追加/账户/
    总闸尽→诚实 LlmExtractionError（预算拒付），不假装按时。"""
    last: Exception | None = None
    while True:
        ok, timeout_s = permit.next_attempt()
        if not ok:
            raise LlmExtractionError(
                f"LLM 调用预算拒付（共享追加/账户/总闸尽）：{last!r}")
        try:
            return call_fn(**{**kwargs, "timeout_s": timeout_s})
        except LlmExtractionError:
            raise
        except Exception as exc:                      # 网络/HTTP/超时等瞬时错误
            last = exc
            time.sleep(1.5 * permit.attempts)


def _parse_llm_json(content: str) -> list[dict]:
    """解析 LLM 输出；容忍 ```json 围栏与首尾噪声，不容忍结构非法。"""
    cleaned = content.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", cleaned, re.DOTALL)
    if fence:
        cleaned = fence.group(1)
    elif not cleaned.startswith("{"):
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start == -1 or end <= start:
            raise LlmExtractionError(f"LLM 输出无 JSON 对象：{cleaned[:120]!r}")
        cleaned = cleaned[start:end + 1]
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise LlmExtractionError(f"LLM JSON 非法：{exc}") from exc
    facts = payload.get("facts")
    if not isinstance(facts, list):
        raise LlmExtractionError("LLM 输出缺 facts 列表")
    # W2Fα1（WA1a-D5）：非 dict 条目原静默丢弃零留痕——计数告警
    # （丢弃行为不变=契约零漂移，留痕供供给质量监测）。
    dropped = sum(1 for entry in facts if not isinstance(entry, dict))
    if dropped:
        _log.warning("llm payload facts 列表丢弃 %d 个非 dict 条目（共 %d 条）",
                     dropped, len(facts))
    return [f for f in facts if isinstance(f, dict)]


def _find_quote(text: str, quote: str, *, lo: int = 0, hi: int | None = None,
                prefer_before: int | None = None) -> tuple[int, int] | None:
    """在 [lo,hi) 窗内定位逐字引词；prefer_before=谓词起点时取窗内最后一个命中。"""
    if not quote:
        return None
    hi = len(text) if hi is None else hi
    hits: list[int] = []
    start = text.find(quote, lo, hi)
    while start != -1:
        hits.append(start)
        start = text.find(quote, start + 1, hi)
    if not hits:
        return None
    chosen = hits[-1] if prefer_before is not None else hits[0]
    return (chosen, chosen + len(quote))


def _slot_or_missing(record_id: str, field: str, text: str, quote: Any,
                     issues: list[str], *, lo: int = 0, hi: int | None = None,
                     prefer_before: int | None = None,
                     extra: Mapping[str, Any] | None = None) -> dict:
    """LLM 引词 → 实搜回填 present 槽；找不到 → missing + issue（宁缺勿编）。"""
    if not isinstance(quote, str) or not quote.strip():
        return _missing()
    quote = quote.strip()
    found = _find_quote(text, quote, lo=lo, hi=hi, prefer_before=prefer_before)
    if found is None:
        issues.append(f"{field}: 引词原文未命中已按 missing 处理：{quote[:24]!r}")
        return _missing()
    return _present(record_id, field, quote, found[0], found[1], text, extra=extra)


def _numeric_slot(record_id: str, base: str, numeric_id: str, raw_num: Mapping,
                  fact_lo: int, fact_hi: int, text: str,
                  issues: list[str]) -> dict | None:
    """数值槽：value_raw 必须原文可定位；unit/magnitude/comparator/metric 逐字校验。"""
    value_raw = raw_num.get("value_raw")
    if not isinstance(value_raw, str) or not value_raw.strip():
        return None
    nbase = f"{base}.numerics.{numeric_id}"        # 密封 P15 expected_field 口径
    value_raw = value_raw.strip()
    found = _find_quote(text, value_raw, lo=fact_lo, hi=fact_hi)
    if found is None:
        found = _find_quote(text, value_raw)          # 全文兜底（归属窗可能估计偏）
    if found is None:
        issues.append(f"{base}.{numeric_id}: 数值引词未命中已跳过：{value_raw[:24]!r}")
        return None
    num_start, num_end = found

    # P15 密封消费契约（value_time L218-233）：槽 raw_value 必须是裸数值核
    # （_parse_value 可解析形态），量级/单位走独立键——LLM 常把"100万"整体
    # 返回，必须拆核（核=槽 raw_value 及其 span；量级/单位锚定核后窗）。
    core = re.search(_NUM_CORE_PATTERN, value_raw)
    if core is None:
        issues.append(f"{base}.{numeric_id}: 数值核不可识别已跳过：{value_raw[:24]!r}")
        return None
    core_raw = core.group(0)
    core_start = num_start + core.start()
    core_end = num_start + core.end()
    near_lo, near_hi = max(0, core_start - 8), min(len(text), core_end + 8)

    value_dec: str | None = None
    try:
        value_dec = _decimal_text(core_raw)
        Decimal(value_dec)
    except (InvalidOperation, ValueError):
        value_dec = None                                # 中文数词等：09 §7.2 后置
    inline: dict[str, Any] = {"value": value_dec, "approximate": False}

    def near_str(key: str) -> str | None:
        val = raw_num.get(key)
        if not isinstance(val, str) or not val.strip():
            return None
        val = val.strip()
        # 三方审计 V2 实证修复（D24）：原式 `… != -1 or text.find(val) == -1`
        # 逻辑反转——全文找不到（幻觉串）反而通过守卫；单位/量级必须锚定核后窗
        # （本函数 docstring 纪律），仅近窗命中才接受。
        return val if text.find(val, near_lo, near_hi) != -1 else None

    unit = near_str("unit")
    magnitude = near_str("magnitude")
    comparator = near_str("comparator")
    metric = near_str("metric")
    range_end_raw = raw_num.get("range_end_raw")
    range_slot = _missing()
    if isinstance(range_end_raw, str) and range_end_raw.strip():
        range_core = re.search(_NUM_CORE_PATTERN, range_end_raw.strip())
        re_dec = None
        if range_core:
            # W3F（W3a-F6②，E2 同款卫生）：_decimal_text（rule.py:863-865）
            # 仅去千分位逗号、不抛 ValueError——原 try/except ValueError
            # 防御腿结构性不可达，拆除直赋；re_dec 恒非 None（中文核
            # 原样通过，机制前提钉 test_winw3f_f6b_hygiene.py）。
            re_dec = _decimal_text(range_core.group(0))
        re_found = _find_quote(text, range_end_raw.strip(), lo=core_end,
                               hi=min(len(text), core_end + 12))
        if re_found is not None:
            range_slot = _present(record_id, f"{nbase}.range_end",
                                  range_end_raw.strip(), re_found[0], re_found[1],
                                  text, extra={"value": re_dec})
            comparator = comparator or "range"
        else:
            issues.append(f"{nbase}.range_end: 引词未命中按 missing："
                          f"{range_end_raw[:16]!r}")
    if comparator and comparator in ("约", "近"):
        inline["approximate"] = True
    inline.update({"unit": unit, "magnitude": magnitude,
                   "currency": None, "role": None, "metric": metric,
                   "comparator": comparator or "=",
                   # W2Fα1（WA1a-C2，判定壁）：inline range_end 统一为核串
                   # （re_dec=_decimal_text(core)：仅去千分位逗号，中文核
                   # 原样通过——W3F W3a-F6② 机制勘正：原注"中文核回退
                   # range_core 原串"失准，中文核实由 re_dec 路径产出）——与
                   # rule.py 十进制核串同消费合同（修复前=range 槽全串，
                   # "1.2%"/"8层" 型必触发 normalize_numeric ValueError →
                   # NUMERIC_ALIGNMENT_FAILED；探针金标 llm 9/9 触发、
                   # rule 双侧 0 非核串，log\temp\winw2fa1-probe-c2-range-
                   # end.json）。核串 ∈ range 槽 quote（_NUM_CORE_PATTERN
                   # 搜索于全串内）保 normalize ∈-quote 校验前提。
                   # W3F（W3a-F6②，E2 同款拆除）：re_dec is None ⟺
                   # range_core is None（_decimal_text 不抛错），原
                   # `else (range_core.group(0) if range_core is not None
                   # else None)` 回退腿结构性不可达，收口 re_dec 单源
                   # （断言面同形，c2 既有钉不受影响）。
                   "range_end": (re_dec if range_slot["status"] == "present"
                                 else None)})

    value_slot = {
        "status": "present", "raw_value": core_raw,
        "evidence": [_ev(record_id, f"{nbase}.value",
                         core_raw, core_start, core_end, text)],
    }
    value_slot.update(inline)
    entry: dict[str, Any] = {
        "numeric_id": numeric_id,
        "evidence": value_slot["evidence"],
        "metric": (_present(record_id, f"{nbase}.metric", metric,
                            text.find(metric, near_lo, near_hi),
                            text.find(metric, near_lo, near_hi) + len(metric), text)
                   if metric and text.find(metric, near_lo, near_hi) != -1
                   else _missing()),
        "value": value_slot,
        "range_end": range_slot,
        "magnitude": (_present(record_id, f"{nbase}.magnitude", magnitude,
                               text.find(magnitude, core_end, near_hi),
                               text.find(magnitude, core_end, near_hi) + len(magnitude),
                               text)
                      if magnitude and text.find(magnitude, core_end, near_hi) != -1
                      else _missing()),
        "unit": (_present(record_id, f"{nbase}.unit", unit,
                          text.find(unit, core_end, near_hi),
                          text.find(unit, core_end, near_hi) + len(unit), text)
                 if unit and text.find(unit, core_end, near_hi) != -1 else _missing()),
        "currency": _missing(), "role": _missing(),
        "comparator": (_present(record_id, f"{nbase}.comparator", comparator,
                                text.find(comparator, near_lo, core_end),
                                text.find(comparator, near_lo, core_end) + len(comparator),
                                text)
                       if comparator and comparator != "range"
                       and text.find(comparator, near_lo, core_end) != -1
                       else _missing()),
        "direction": _missing(), "time": _missing(),
    }
    return entry


_NUM_CORE_PATTERN = r"[+-]?[0-9]+(?:,[0-9]{3})*(?:\.[0-9]+)?|[零〇一二两三四五六七八九十百千万亿]+"


def _facts_from_llm_payload(record_id: str, text: str, raw_facts: list[dict],
                            issues: list[str]) -> list[dict]:
    facts: list[dict] = []
    for index, raw in enumerate(raw_facts, 1):
        predicate = raw.get("predicate")
        if not isinstance(predicate, str) or not predicate.strip():
            issues.append(f"facts.f{index}: predicate 缺失，该条丢弃")
            continue
        predicate = predicate.strip()
        pred_found = _find_quote(text, predicate)
        if pred_found is None:
            issues.append(f"facts.f{index}: 谓词引词未命中，该条丢弃：{predicate[:24]!r}")
            continue
        fact_id = f"f{len(facts) + 1}"
        base = f"facts.{fact_id}"
        pred_start, pred_end = pred_found

        # W2Fα1（WA1a-D8，探针裁定）：主体搜索窗 hi 恒为 pred_start——
        # 原 `hi=pred_start if pred_start else None` 在谓词居文首
        # （pred_start==0）时退化为**全文末命中**（prefer_before 取 hits[-1]
        # 可取谓词之后命中）；改为空窗 → missing → 走下行谓词后窗兜底
        # （hits[0]=倒装标题路径）。金标探针：pred_start==0 零触发
        # （log\temp\winw2fa1-probe-d8-subject.json）=零翻转。
        # 探针同时证伪任务书原拟"子句局域"修法：现役 prefer_before 已是
        # 谓词前**最近**命中，子句局域会把 177/2634 个跨子句合法主体洗成
        # missing（LLM 腿 fn/tp 回归向），不实施（裁定留档同探针 JSON）。
        subject = _slot_or_missing(record_id, f"{base}.subject", text,
                                   raw.get("subject"), issues,
                                   hi=pred_start,
                                   prefer_before=pred_start)
        if subject["status"] == "missing":            # 谓词后窗兜底（标题倒装等）
            subject = _slot_or_missing(record_id, f"{base}.subject", text,
                                       raw.get("subject"), issues)
        polarity_raw = raw.get("polarity")
        if isinstance(polarity_raw, str) and predicate not in polarity_raw:
            polarity_raw = None                       # 极性片段必须含谓词原词
        polarity = _slot_or_missing(record_id, f"{base}.event_state.polarity",
                                    text, polarity_raw, issues,
                                    lo=max(0, pred_start - 6), hi=pred_end + 1)
        if polarity["status"] == "missing":
            polarity = _present(record_id, f"{base}.event_state.polarity",
                                predicate, pred_start, pred_end, text)
        fact_lo = subject["evidence"][0]["start"] if subject["evidence"] else max(0, pred_start - 30)
        fact_hi = min(len(text), pred_end + 40)

        numerics: list[dict] = []
        raw_nums = raw.get("numerics")
        if isinstance(raw_nums, list):
            for num_index, raw_num in enumerate(raw_nums, 1):
                if not isinstance(raw_num, dict):
                    continue
                entry = _numeric_slot(record_id, base, f"n{len(numerics) + 1}",
                                      raw_num, fact_lo, fact_hi, text, issues)
                if entry is not None:
                    numerics.append(entry)

        fact_evidence = [_ev(record_id, base, predicate, pred_start, pred_end, text)]
        facts.append({
            "fact_id": fact_id,
            "evidence": fact_evidence,
            "fact_type": _present(record_id, f"{base}.fact_type",
                                  predicate, pred_start, pred_end, text),
            "subject": subject,
            "event_state": {
                "predicate": _present(record_id, f"{base}.event_state.predicate",
                                      predicate, pred_start, pred_end, text),
                "polarity": polarity,
                # W3F（W3a-F6①，判定壁）：modality 槽同源收口 D8——原
                # `hi=pred_start if pred_start else None` 在谓词居文首
                # （pred_start==0）时退化为**全文窗首命中**（modality 无
                # prefer_before，可误取谓词后情态词；subject 槽 D8 已修
                # 同型退化，:349-352）。改为 hi=pred_start 恒值：==0 →
                # 空窗 → missing 显式（宁缺勿编，issue 登记）。金标探针：
                # pred_start==0 零触发（log\temp\winw3f-probe-f6-
                # modality.json：facts=3006/pred_at_zero=0）=零翻转。
                "modality": _slot_or_missing(record_id,
                                             f"{base}.event_state.modality",
                                             text, raw.get("modality"), issues,
                                             hi=pred_start),
                "attribution": _slot_or_missing(record_id,
                                                f"{base}.event_state.attribution",
                                                text, raw.get("attribution"), issues),
            },
            "time": {
                "expression": _slot_or_missing(record_id, f"{base}.time.expression",
                                               text, raw.get("time_expression"), issues),
                "stage": _slot_or_missing(record_id, f"{base}.time.stage",
                                          text, raw.get("time_stage"), issues),
                "anchor": _missing(),
            },
            "key_object": _slot_or_missing(record_id, f"{base}.key_object",
                                           text, raw.get("key_object"), issues,
                                           lo=pred_end, hi=fact_hi),
            "numerics": numerics,
        })
    return facts


def _fallback_fact(record_id: str, text: str) -> list[dict]:
    """B2 同款 fallback minimal fact（设计书 §4 裁定镜像）：真实首子句
    evidence + 全槽 missing——避免 build_aligned 零 Fact 侧 PairAlignmentError
    冒泡成技术失败；语义="LLM 未产出可定位事实"，非"正文确实未表达"。

    W2Fα1（C3，WA1a）：分隔符-only/无实义子句文本（"。"/" 。"型）零实义
    子句——裸 `_clauses(text)[0]` 抛 IndexError 逃逸声明通道（入口
    text.strip() 判空闸不覆盖）；归 rule.py 同一声明路径"空文本/全空白
    → []"（不足证据，调用方判空接线），不伪造 evidence。"""
    from .rule import _CLAUSE_SEPARATORS, _clauses
    clauses = _clauses(text)
    if not any(text[s:e].strip().strip(_CLAUSE_SEPARATORS)
               for s, e in clauses):
        return []
    first_start, first_end = clauses[0]
    return [{
        "fact_id": "f1",
        "evidence": [_ev(record_id, "facts.f1",
                         text[first_start:first_end], first_start, first_end,
                         text)],
        "fact_type": _missing(),
        "subject": _missing(),
        "event_state": {key: _missing()
                        for key in ("predicate", "polarity", "modality",
                                    "attribution")},
        "time": {key: _missing()
                 for key in ("expression", "stage", "anchor")},
        "key_object": _missing(),
        "numerics": [],
    }]


def extract_facts_llm(
    record_id: str,
    text: str,
    *,
    call_fn: Callable[..., tuple[str, float]] | None = None,
    model: str = DEFAULT_MODEL,
    base_url: str = DEFAULT_BASE_URL,
    api_key: str | None = None,
    cache_dir: str | os.PathLike[str] | None = None,
    timeout_s: float = 30.0,
    max_retries: int = 2,
    report_hook: Callable[[dict], None] | None = None,
    budget: "ProcessingBudget | None" = None,
) -> list[dict]:
    """LLM P14 抽取主入口（形状与 rule.extract_facts 完全一致）。

    call_fn 注入点：单元测试用 mock（零网络）；默认 DashScope urllib 调用。
    report_hook：每文一次的旁路上报（issues/latency/cache 命中），回放接线
    用于落侧车 JSON；不改变返回契约（list[facts]）。

    W2 ⑩①-6（N43 挂账清偿）：budget=None→现役常量路径逐字节
    （:479-480 30.0/2 驱动 _call_with_retries）；budget 在场→软预算闸
    （越 prepare_soft_s 拒启新调用，charge 未发生）+permit 驱动环
    （_call_with_permit：共享追加授权、截断 timeout_s、统一计账）。
    """
    if not _RECORD_ID_RE.fullmatch(record_id):
        raise ValueError(f"record_id must be 64 lowercase hex: {record_id!r}")
    if not text or not text.strip():
        return []

    text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    cache_path: Path | None = None
    if cache_dir is not None:
        cache_path = Path(cache_dir) / f"{text_hash}.json"
        if cache_path.exists():
            # fail-closed（M-12/L 窗口P）：损坏条目（坏 JSON / 非对象 / 缺
            # raw_llm_content 键 / issues 非列表）按 miss 处理并记日志，不 crash。
            try:
                cached = json.loads(cache_path.read_text(encoding="utf-8"))
                if not isinstance(cached, dict):
                    raise ValueError("llm cache entry is not an object")
                raw_cached_content = cached["raw_llm_content"]
                if not isinstance(raw_cached_content, str):
                    raise TypeError("llm cache raw_llm_content is not str")
                cached_issues = cached.get("issues", [])
                if not isinstance(cached_issues, list):
                    raise TypeError("llm cache issues is not a list")
            except (OSError, UnicodeDecodeError, ValueError, KeyError,
                    TypeError) as error:
                _log.warning("llm cache entry corrupted, treated as miss: "
                             "%s (%s)", cache_path, type(error).__name__)
                cached = None
            if (cached is not None
                    and cached.get("model") == model
                    and cached.get("prompt_version") == LLM_PROMPT_VERSION):
                # 缓存只存 record_id 无关的原始 LLM 输出；facts 按请求方
                # record_id 确定性重建——金标集存在同行文不同 row 的文本复用，
                # 直接缓存 facts 会把他人 record_id 带进 evidence（R2 84 条
                # PairAlignmentError 根因，主窗口 05:1x 修复）。
                issues = list(cached_issues)
                facts = _facts_from_llm_payload(
                    record_id, text,
                    _parse_llm_json(raw_cached_content), issues)
                if not facts:                       # 缓存零事实同样兜底（确定性）
                    facts = _fallback_fact(record_id, text)
                    issues.append("llm_empty_facts_fallback: 零可定位事实已兜底")
                if report_hook:
                    # R3-M2：命中/未命中分支键集对称（补 n_facts）。
                    report_hook({"cache_hit": True, "model": model,
                                 "issues": issues,
                                 "latency_s": 0.0, "text_sha256": text_hash,
                                 "n_facts": len(facts)})
                return facts

    fn = call_fn or _default_call_fn
    kwargs: dict[str, Any] = {"model": model, "text": text, "timeout_s": timeout_s}
    if call_fn is None:
        key = api_key or os.environ.get("QWEN_API_KEY", "")
        if not key:
            raise LlmExtractionError("QWEN_API_KEY 未设置且未显式传 api_key")
        kwargs.update({"base_url": base_url, "api_key": key})
    if budget is None:
        # 现役路径逐字节：:479-480 常量驱动（值无关主轴钉 f1 同锚）。
        content, latency = _call_with_retries(fn, retries=max_retries, **kwargs)
    else:
        # W2 ⑩①-6 软预算执法面：越 accepted+prepare_soft_s→拒启新逻辑
        # 调用（诚实领域失败，charge 未发生）；在场→permit 驱动环。
        if budget.beyond_prepare_soft():
            raise LlmExtractionError(
                "预算软边界：不再启动新模型调用（已启动者收尾）")
        content, latency = _call_with_permit(fn, permit=budget.permit(), **kwargs)
    raw_facts = _parse_llm_json(content)
    issues: list[str] = []
    facts = _facts_from_llm_payload(record_id, text, raw_facts, issues)

    if cache_path is not None:
        if facts:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            # W2Fα1（WA1a-D7②）：缓存写原子化——tmp+os.replace（同目录内
            # rename 原子语义），修复前裸 write_text 可留半截损坏条目。
            tmp_path = cache_path.with_name(cache_path.name + ".tmp")
            tmp_path.write_text(json.dumps({
                "model": model, "prompt_version": LLM_PROMPT_VERSION,
                "text_sha256": text_hash, "raw_llm_content": content,
                "issues": issues,
            }, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp_path, cache_path)
        else:
            # R3-M2（W-R3b）零保护：零可定位事实（全幻觉 raw / 诚实空
            # facts）不落缓存——钉入即永久复用、幻觉再无重审机会；告警
            # 计数 + 日志 + 侧车 issue 同行登记。遗产条目（修复前已落盘
            # 的零事实 raw）命中路径兜底语义不动（:515-517）。
            global _ZERO_FACTS_CACHE_SKIPS
            _ZERO_FACTS_CACHE_SKIPS += 1
            _log.warning("llm extraction yielded zero locatable facts; "
                         "raw response NOT cached (skip #%d, text_sha256=%s)",
                         _ZERO_FACTS_CACHE_SKIPS, text_hash)
            issues.append(
                "llm_zero_facts_cache_skip: 零可定位事实抽取结果未落缓存")
    if not facts:                                   # B2 §4 裁定镜像：非空文本零事实兜底
        facts = _fallback_fact(record_id, text)
        issues.append("llm_empty_facts_fallback: 零可定位事实已兜底")
    if report_hook:
        report_hook({"cache_hit": False, "model": model, "issues": issues,
                     "latency_s": round(latency, 3), "text_sha256": text_hash,
                     "n_facts": len(facts)})
    return facts
