# -*- coding: utf-8 -*-
"""F3 事实供给装配面·LLM 实现（用户令 2026-10-11 主窗拍板）。

在 p3-v6-phase1（0a0e5de）之上把 LLM 事实抽取（facts/llm.py P14 抽取器
）接进供给面：供给选择 env DEDUP_FACT_SUPPLY=rule|llm——**缺席=rule**
（测试密封默认，同 DEDUP_JUDGE_DECISION_MODE 缺席=legacy 纪律）；非法
值 ValueError fail-closed。RunManifest 记 fact_supply 实态结构化字段
（schema v2→v3，字段集变更）。

降级纪律（fail-closed 方向，主窗令 2）：单文 LLM 抽取失败（超时/API/
零事实被拒/日预算拒）→**该文**回落 rule 供给结果（不链级失败），记计
数 extraction.fallback；落回原因入供给层台账（逐条 jsonl 侧车可落盘
——G 项：decide 可观测面不动，如需 decide 层可观测另行下令）。

八裁量点（主窗 2026-10-11 逐项裁定"按拟实施全准"）：
- A 超时 30s/重试 2 沿用现役（facts/llm.py 常量）+DEDUP_FACT_TIMEOUT_S
  可配（缺席=30.0，非法/负→ValueError）；
- B 缺省串行逐文（build_commit_inputs 现役 for 循环语义不变；并发扇
  出若需 T 窗另行令）；
- C 供给/判官天然先后串行（供给=build_commit_inputs 时刻，判官=pair
  循环时刻），两闸独立；ProcessingBudget 在场同账户扣减（保留构造接
  线位，缺省不接）；
- D 日 token 闸默认 20M（DEDUP_FACT_DAILY_TOKEN_BUDGET；EMB 部署配
  置档值族同构——EMB 代码缺省 5M、部署档 20M；facts 按 17k 文/天×
  ~1K 总 token 测算约 13M，留头合理）；
- E qwen_bpe 本地精确计数+UTF-8 字节保守兜底（EMB 校准 oracle 同族
  ；加载失败永久降级记日志，计数方法入台账条目）；
- F issues 含 llm_empty_facts_fallback 标记判"零事实被拒"（facts/llm.py
  现役稳定标记，含缓存命中遗产条目同兜底路径）；
- G 落回原因走供给层台账+jsonl 侧车（见上）；
- H manifest env 单源（无 config 对象→无同传冲突面）。

T 接线预留（令 5）：工装装配点=组合根构造 RecallService(fact_supply=...)
处的替换口——现役 T 传 RuleFactSupply()；切 LLM 供给时改为
``resolve_fact_supply(env)``（rule→RuleFactSupply、llm→LlmFactSupply）
或直接 LlmFactSupply(...)。本模块不动 create_host 默认（缺省恒
IdentityFactSupply——21:4x 主窗口裁定形态零翻转）；api/host.py 组合根
的 fact_supply 注入位不变（显式注入单源）。

开工令①b（2026-10-11，LLM 抽取按需化·双轨改造，基于 9e0bb4d）：
本模块新增 **track 2 按需面**——``OnDemandLlmFacts``（decide 层注入
协议实现：extract(record_id, text) -> LlmFactsOutcome）+缓存键四元
（facts/llm.py EXTRACTOR_SCHEMA_VERSION 升维）+日 token 耗尽合同
（停新增+已有缓存继续用+告警计数 budget_exhausted_events）。与 F3
 unconditional 面（LlmFactSupply——①令产物，整批文档供给）并存：
双轨生产装配=build_commit_inputs 缺省 rule 供给（deterministic 段）
+decide 判径对按需 OnDemandLlmFacts（LLM 段）。回落纪律差异（令4）
：F3 面失败回落 rule 供给结果；按需面失败**不替换**——如实报
unavailable，由 decide 层执行失败闭门（rule 回落只补证据，both_
missing 域不降级签重复）。接口冻结清单见 decide/service.py
decide_for_task(llm_facts=...) docstring 与本模块 OnDemandLlmFacts
docstring（P0 运行时装配对接面）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, NamedTuple

from news_flash_dedup.facts import llm as _facts_llm
from news_flash_dedup.facts.llm import LlmExtractionError

_log = logging.getLogger(__name__)

#: 供给选择开关（H 项：env 单源）。
FACT_SUPPLY_ENV = "DEDUP_FACT_SUPPLY"
#: 合法供给模式集（固定序=确定性报错）。
FACT_SUPPLY_MODES = ("rule", "llm")
#: 缺席/空=rule（测试密封默认）。
FACT_SUPPLY_MODE_DEFAULT = "rule"

#: A 项：抽取超时可配（缺席=现役 30.0s）。
FACT_TIMEOUT_ENV = "DEDUP_FACT_TIMEOUT_S"
DEFAULT_FACT_TIMEOUT_S = 30.0

#: D 项：日 token 闸（EMB 部署配置档值族同构，主窗确认 20M）。
FACT_DAILY_TOKEN_BUDGET_ENV = "DEDUP_FACT_DAILY_TOKEN_BUDGET"
DEFAULT_FACT_DAILY_TOKEN_BUDGET = 20_000_000

#: 按需 LLM facts 注入开关（最终接线窗，P1c 装配注入设计单 §二-B 注入
#: 位 B：shadow_decide/commit_one 预装配 None=env 装配；judge 开关族
#: 真值集同款——缺席/空/非真值一律关（fail-closed 默认关=影子态）。
#: 已登记 lib/run_manifest.py KNOWN_SWITCHES。
LLM_FACTS_ONDEMAND_ENV = "DEDUP_LLM_FACTS_ONDEMAND"
_LLM_FACTS_ONDEMAND_ON = frozenset({"1", "true", "yes", "on"})

#: F 项：零事实被拒标记（facts/llm.py 兜底路径稳定标记）。
ZERO_FACTS_MARKER = "llm_empty_facts_fallback"

#: 业务日口径（EMB R14 同构：快讯域日=UTC+8）。
_BUSINESS_TZ = timezone(timedelta(hours=8))


class FactSupplyModeInvalid(ValueError):
    """DEDUP_FACT_SUPPLY 非法值（fail-closed，同 decision_mode 纪律）。"""


# ---------- 供给选择（令 1） ----------


def fact_supply_mode_from_env(env: Mapping[str, str] | None = None) -> str:
    """解析 DEDUP_FACT_SUPPLY：缺席/空=rule；"rule"/"llm" 原样；其余
    FactSupplyModeInvalid（ValueError 族——非法值宁可拒产不可静默选边）。"""
    source = os.environ if env is None else env
    raw = (source.get(FACT_SUPPLY_ENV) or "").strip()
    if not raw:
        return FACT_SUPPLY_MODE_DEFAULT
    if raw not in FACT_SUPPLY_MODES:
        raise FactSupplyModeInvalid(
            f"{FACT_SUPPLY_ENV}={raw!r} 非法（合法值：{'/'.join(FACT_SUPPLY_MODES)}；"
            f"缺席={FACT_SUPPLY_MODE_DEFAULT!r}）")
    return raw


def fact_timeout_from_env(env: Mapping[str, str] | None = None) -> float:
    """解析 DEDUP_FACT_TIMEOUT_S：缺席/空=30.0（现役常量）；数值须>0。"""
    source = os.environ if env is None else env
    raw = (source.get(FACT_TIMEOUT_ENV) or "").strip()
    if not raw:
        return DEFAULT_FACT_TIMEOUT_S
    try:
        value = float(raw)
    except ValueError as error:
        raise ValueError(
            f"{FACT_TIMEOUT_ENV}={raw!r} 非数值（须正浮点秒）") from error
    if value <= 0.0:
        raise ValueError(f"{FACT_TIMEOUT_ENV}={raw!r} 须为正数（秒）")
    return value


def fact_daily_token_budget_from_env(
        env: Mapping[str, str] | None = None) -> int:
    """解析 DEDUP_FACT_DAILY_TOKEN_BUDGET：缺席/空=20M（D 项）；整数须>0。"""
    source = os.environ if env is None else env
    raw = (source.get(FACT_DAILY_TOKEN_BUDGET_ENV) or "").strip()
    if not raw:
        return DEFAULT_FACT_DAILY_TOKEN_BUDGET
    try:
        value = int(raw)
    except ValueError as error:
        raise ValueError(
            f"{FACT_DAILY_TOKEN_BUDGET_ENV}={raw!r} 非整数（须正整数 token 数）"
        ) from error
    if value <= 0:
        raise ValueError(f"{FACT_DAILY_TOKEN_BUDGET_ENV}={raw!r} 须为正整数")
    return value


# ---------- E 项：token 计数（qwen_bpe 精确+字节保守兜底） ----------

_TOKENIZER: Any = None
_TOKENIZER_LOCK = threading.Lock()
_TOKENIZER_FAILED = False


def _shared_tokenizer() -> Any:
    """惰性共享单例（加载失败永久降级记日志——oracle 家族同款，不重试
    冒泡；字节保守估计始终可用，闸不旁路）。"""
    global _TOKENIZER, _TOKENIZER_FAILED
    if _TOKENIZER is not None or _TOKENIZER_FAILED:
        return _TOKENIZER
    with _TOKENIZER_LOCK:
        if _TOKENIZER is None and not _TOKENIZER_FAILED:
            try:
                from news_flash_dedup.vector.qwen_bpe import QwenBpeTokenizer
                _TOKENIZER = QwenBpeTokenizer()
            except Exception as error:      # LoadError(vendored 缺失/损毁)族
                _TOKENIZER_FAILED = True
                _log.warning(
                    "qwen_bpe tokenizer unavailable, token counts fall back "
                    "to UTF-8 byte estimate: %s: %s",
                    type(error).__name__, error)
    return _TOKENIZER


def _count_text_tokens(text: str) -> tuple[int, str]:
    """单串计数 → (tokens, method)。method="qwen_bpe_exact"｜
    "utf8_byte_estimate"（EMB R14 缺 usage 同口径：字节保守上界）。"""
    tokenizer = _shared_tokenizer()
    if tokenizer is None:
        return len(text.encode("utf-8")), "utf8_byte_estimate"
    from news_flash_dedup.vector.qwen_bpe import count_tokens
    return count_tokens(tokenizer, text), "qwen_bpe_exact"


# ---------- 台账（令 3：逐条记 model/tokens in/out/延迟/缓存命中） ----------


class FactExtractionLedger:
    """线程安全抽取台账（判官 ResidualJudgeLedger 同构：聚合计数+运行
    元数据；本件另含 tokens in/out 与逐条 jsonl 侧车落盘口——判官用量
    jsonl 侧车同构）。计数键 extraction.fallback=主窗令 2 落回计数。"""

    def __init__(self, *, jsonl_path: str | os.PathLike[str] | None = None,
                 clock: Callable[[], datetime] | None = None) -> None:
        self._lock = threading.Lock()
        self._jsonl_path = (Path(jsonl_path) if jsonl_path is not None else None)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._counters = {
            "extractions": 0,        # 供给调用总次数（成功+回落）
            "llm_calls": 0,          # 实际 LLM 供给路径（含零事实被拒回落）
            "llm_successes": 0,      # LLM 产物直通（未回落）
            "cache_hits": 0,
            "extraction.fallback": 0,  # 主窗令 2 计数名
            "llm_failures": 0,       # 异常/预算拒/零事实被拒（回落超集）
            "tokens_in": 0,
            "tokens_out": 0,
            "llm_latency_s": 0.0,
            # ①b 令3（日 token 耗尽合同）预算告警计数：
            "budget_exhausted_events": 0,   # 预算闸拒绝启动新抽取的次数
            "budget_charge_failures": 0,    # charge 落盘失败（fail-closed 降级）
        }
        self.entries: list[dict] = []      # 内存台账（审计/断言面）

    def record(self, entry: Mapping[str, Any]) -> None:
        """逐条记账（narrative 键集：ts/record_id/text_sha256/model/
        tokens_in/tokens_out/latency_s/cache_hit/fallback/fallback_reason/
        n_facts）+jsonl 追加一行（构造时给路径才落盘；行级 append=审计
        侧车语义，崩溃至多丢最后一行，权威账=聚合 snapshot）。"""
        normalized = dict(entry)
        with self._lock:
            self._counters["extractions"] += 1
            if normalized.get("fallback"):
                self._counters["extraction.fallback"] += 1
            if normalized.get("cache_hit"):
                self._counters["cache_hits"] += 1
            self._counters["tokens_in"] += int(normalized.get("tokens_in", 0))
            self._counters["tokens_out"] += int(normalized.get("tokens_out", 0))
            self._counters["llm_latency_s"] += float(
                normalized.get("latency_s", 0.0))
            self.entries.append(normalized)
            if self._jsonl_path is not None:
                line = json.dumps(normalized, ensure_ascii=False,
                                  sort_keys=True, separators=(",", ":"))
                try:
                    self._jsonl_path.parent.mkdir(parents=True, exist_ok=True)
                    with open(self._jsonl_path, "a", encoding="utf-8") as fh:
                        fh.write(line + "\n")
                except OSError as error:
                    # 侧车落盘失败不阻断供给（台账主账在内存聚合）；告警
                    # 可溯（判官用量侧车同款容错纪律：审计面降级不断链）。
                    _log.warning(
                        "fact extraction ledger jsonl append failed: %s (%s)",
                        self._jsonl_path, type(error).__name__)

    def snapshot(self) -> dict:
        """聚合快照（判官 ledger.snapshot 同构：只读副本）。"""
        with self._lock:
            out = dict(self._counters)
            out["llm_latency_s"] = round(out["llm_latency_s"], 3)
            return out

    def count_llm_path(self, *, success: bool) -> None:
        """llm_calls/llm_successes/llm_failures 归账（LlmFactSupply 调）。"""
        with self._lock:
            self._counters["llm_calls"] += 1
            if success:
                self._counters["llm_successes"] += 1
            else:
                self._counters["llm_failures"] += 1

    def count_budget_exhausted(self) -> None:
        """①b 令3 预算告警计数：日 token 闸拒绝启动新抽取（每次+1）。"""
        with self._lock:
            self._counters["budget_exhausted_events"] += 1

    def count_budget_charge_failure(self) -> None:
        """①b：charge 落盘失败（结果保留、后续调用拒）降级计数。"""
        with self._lock:
            self._counters["budget_charge_failures"] += 1


# ---------- D 项：日 token 闸（EMB R14 族同构） ----------


class FactsBudgetExceeded(RuntimeError):
    """facts 日 token 预算越限（fail-closed：本日不再启动新抽取调用，
    单文回落 rule——不旁路不静默）。"""


class FactsDailyTokenBudget:
    """UTC+8 业务日翻转；计数器可选 JSON 落盘（tmp+os.replace 原子——
    EMB 同款）；落盘失败 → broken → 此后一切调用拒（重启不可绕闸）。

    store_path 缺省=None（内存态——单元/工装轻量装配）；T 生产接线时
    配 cache_root 同域路径即得 EMB R14 全语义（持久化接线属 T 窗令）。
    """

    def __init__(self, *, daily_tokens: int = DEFAULT_FACT_DAILY_TOKEN_BUDGET,
                 store_path: str | os.PathLike[str] | None = None,
                 clock: Callable[[], datetime] | None = None) -> None:
        if type(daily_tokens) is not int or daily_tokens < 1:
            raise ValueError("daily_tokens must be a positive int")
        self.daily_tokens = daily_tokens
        self._store_path = (Path(store_path) if store_path is not None else None)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.Lock()
        self._broken = False
        self._day, self._used = self._load_counter()

    # ---------- 日口径与持久化（EMB 同构） ----------

    def _business_day(self) -> str:
        now = self._clock()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        return now.astimezone(_BUSINESS_TZ).date().isoformat()

    def _load_counter(self) -> tuple[str, int]:
        today = self._business_day()
        if self._store_path is None or not self._store_path.exists():
            return today, 0
        try:
            body = json.loads(self._store_path.read_text(encoding="utf-8"))
            day = body.get("business_date")
            used = body.get("used_tokens")
        except (OSError, ValueError):
            # 计数器不可读=确认未知 → fail-closed（不当作零用量放行）。
            self._broken = True
            _log.warning("facts budget counter unreadable (fail-closed): %s",
                         self._store_path)
            return today, 0
        if day != today or type(used) is not int or used < 0:
            return today, 0            # 业务日翻转重置
        return day, used

    def _persist_counter(self) -> None:
        assert self._store_path is not None
        body = {"business_date": self._day, "used_tokens": self._used,
                "daily_tokens": self.daily_tokens,
                "updated_at": datetime.now(timezone.utc).isoformat()}
        temp = self._store_path.with_suffix(".json.tmp")
        self._store_path.parent.mkdir(parents=True, exist_ok=True)
        temp.write_text(json.dumps(body, ensure_ascii=False, indent=2),
                        encoding="utf-8")
        os.replace(temp, self._store_path)

    # ---------- 闸面 ----------

    def check_open(self) -> None:
        """调用前检查：翻日重置；越限/计数器损毁 → FactsBudgetExceeded。"""
        with self._lock:
            self._ensure_open_locked()

    def _ensure_open_locked(self) -> None:
        if self._broken:
            raise FactsBudgetExceeded(
                "facts budget counter broken (fail-closed, R14 同构)")
        today = self._business_day()
        if today != self._day:           # 业务日翻转重置
            self._day = today
            self._used = 0
            if self._store_path is not None:
                try:
                    self._persist_counter()
                except OSError:
                    self._broken = True
                    raise FactsBudgetExceeded(
                        "facts budget counter persistence failed "
                        "(fail-closed, R14 同构)") from None
        if self._used >= self.daily_tokens:
            raise FactsBudgetExceeded(
                f"facts daily token budget exceeded: {self._used} >= "
                f"{self.daily_tokens}（本日回落 rule 供给；明日 UTC+8 翻日重置）")

    def charge(self, tokens: int) -> None:
        """成功调用后计账（缓存命中不 charge——零 token 消耗）；落盘
        失败 → broken fail-closed。"""
        if type(tokens) is not int or tokens < 0:
            raise ValueError("tokens must be a non-negative int")
        with self._lock:
            self._used += tokens
            if self._store_path is not None:
                try:
                    self._persist_counter()
                except OSError:
                    self._broken = True
                    raise FactsBudgetExceeded(
                        "facts budget counter persistence failed "
                        "(fail-closed, R14 同构)") from None

    def snapshot(self) -> dict:
        """闸态快照（business_date/used_tokens/daily_tokens/broken）。"""
        with self._lock:
            return {"business_date": self._day,
                    "used_tokens": self._used,
                    "daily_tokens": self.daily_tokens,
                    "broken": self._broken}


# ---------- LLM 供给（令 1/2 主体） ----------


class LlmFactSupply:
    """F3 端口 LLM 实现：__call__(record_id, text) → list[dict]（与
    IdentityFactSupply/RuleFactSupply 同签名）。

    单文流程：预算 check_open → extract_facts_llm（缓存/重试/零事实拒
    缓存纪律全沿现役）→ 成功且非零事实兜底 → LLM facts 原样直通；任一
    失败（异常/零事实被拒/预算拒）→ 该文回落 fallback（缺省
    RuleFactSupply）——不链级失败（主窗令 2 fail-closed 方向：宁缺
    LLM 语义，不编造不冒泡）。

    B 项：缺省串行逐文（RecallService.build_commit_inputs 语义不变）；
    C 项：与判官调用天然先后串行，本闸（日 token）独立；ProcessingBudget
    （每记录逻辑预算）可经 budget 参数接线——在场同账户扣减，缺省不接。
    """

    def __init__(self, *, call_fn: Callable[..., tuple[str, float]] | None = None,
                 model: str | None = None,
                 base_url: str | None = None,
                 api_key: str | None = None,
                 cache_dir: str | os.PathLike[str] | None = None,
                 timeout_s: float | None = None,
                 max_retries: int = 2,
                 fallback: Callable[[str, str], list[dict]] | None = None,
                 ledger: FactExtractionLedger | None = None,
                 budget: FactsDailyTokenBudget | None = None,
                 token_counter: Callable[[str], tuple[int, str]] | None = None,
                 env: Mapping[str, str] | None = None,
                 processing_budget: Any = None) -> None:
        self._env = os.environ if env is None else env
        self.model = model if model is not None else _facts_llm.DEFAULT_MODEL
        self.base_url = base_url
        self.api_key = api_key
        self.cache_dir = cache_dir
        self.timeout_s = (timeout_s if timeout_s is not None
                          else fact_timeout_from_env(self._env))
        self.max_retries = max_retries
        self.ledger = ledger if ledger is not None else FactExtractionLedger()
        self.budget = budget if budget is not None else FactsDailyTokenBudget()
        self._token_counter = (token_counter if token_counter is not None
                               else _count_text_tokens)
        self._call_fn = call_fn
        self._fallback = fallback          # 懒缺省 RuleFactSupply（防循环导入）
        # C 项保留位：ProcessingBudget 在场同账户（缺省不接=两闸独立）。
        self._processing_budget = processing_budget
        self._last_call: dict | None = None
        self._system_tokens: int | None = None   # system 提示词 token（一次缓存）

    # ---------- 内部 ----------

    def _fallback_supply(self) -> Callable[[str, str], list[dict]]:
        if self._fallback is not None:
            return self._fallback
        from news_flash_dedup.recall.service import RuleFactSupply
        return RuleFactSupply()

    def _call_with_accounting(self) -> Callable[..., tuple[str, float]]:
        """包 call_fn：计数 tokens in（system+user）/out（content）——两
        路（注入 mock/默认 DashScope）content 均可见；失败调用不计 token
        （无响应可计，账面=llm_failures 计数器）。"""
        inner = self._call_fn
        base_url = (self.base_url if self.base_url is not None
                    else _facts_llm.DEFAULT_BASE_URL)
        api_key = self.api_key or (self._env.get("QWEN_API_KEY") or "")
        counter = self._token_counter

        def wrapped(*, model: str, text: str, timeout_s: float) -> tuple[str, float]:
            if inner is None:
                content, latency = _facts_llm._default_call_fn(
                    model=model, base_url=base_url, api_key=api_key,
                    text=text, timeout_s=timeout_s)
            else:
                content, latency = inner(model=model, text=text,
                                         timeout_s=timeout_s)
            if self._system_tokens is None:
                self._system_tokens = counter(_facts_llm._SYSTEM_PROMPT)[0]
            tokens_in = self._system_tokens + counter(text)[0]
            tokens_out = counter(content)[0]
            self._last_call = {"tokens_in": tokens_in,
                               "tokens_out": tokens_out,
                               "latency_s": latency}
            return content, latency

        return wrapped

    def _record(self, *, record_id: str, text: str, facts_count: int,
                tokens_in: int = 0, tokens_out: int = 0, latency_s: float = 0.0,
                cache_hit: bool = False, fallback: bool = False,
                fallback_reason: str | None = None) -> None:
        text_sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "record_id": record_id,
            "text_sha256": text_sha,
            "model": self.model,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "latency_s": round(float(latency_s), 3),
            "cache_hit": cache_hit,
            "fallback": fallback,
            "fallback_reason": fallback_reason,
            "n_facts": facts_count,
        }
        self.ledger.record(entry)

    def _fall_back(self, record_id: str, text: str, reason: str) -> list[dict]:
        """回落纪律（令 2）：该文取 rule 供给结果+计数+原因入台账。"""
        self.ledger.count_llm_path(success=False)
        facts = self._fallback_supply()(record_id, text)
        self._record(record_id=record_id, text=text, facts_count=len(facts),
                     fallback=True, fallback_reason=reason)
        return facts

    # ---------- F3 端口 ----------

    def __call__(self, record_id: str, text: str) -> list[dict]:
        if not isinstance(text, str):
            raise TypeError("text must be str")
        report: dict = {}

        def hook(info: Mapping[str, Any]) -> None:
            report.update(info)

        try:
            if self.budget is not None:
                self.budget.check_open()
        except FactsBudgetExceeded as error:
            # ①b 令3 预算告警计数（两供给面对称：F3 面同样"拒绝启动新
            # 抽取"即+1——单点口径=count_budget_exhausted 语义注释）。
            self.ledger.count_budget_exhausted()
            return self._fall_back(
                record_id, text, f"budget_exceeded: {error}")

        self._last_call = None
        try:
            facts = _facts_llm.extract_facts_llm(
                record_id, text, call_fn=self._call_with_accounting(),
                model=self.model, base_url=self.base_url,
                api_key=self.api_key, cache_dir=self.cache_dir,
                timeout_s=self.timeout_s, max_retries=self.max_retries,
                report_hook=hook)
        except LlmExtractionError as error:
            # 超时/API 失败/JSON 非法/预算软边界（含 ProcessingBudget 拒）
            # ——异常面统一回落（令 2 fail-closed 方向）。
            return self._fall_back(
                record_id, text,
                f"llm_extraction_failed: {type(error).__name__}: {error}")
        except OSError as error:
            return self._fall_back(
                record_id, text,
                f"llm_extraction_failed: OSError: {error}")

        issues = report.get("issues") or []
        if any(ZERO_FACTS_MARKER in str(issue) for issue in issues):
            # F 项：零可定位事实被拒（含缓存遗产条目同兜底路径）——该文
            # 回落 rule（不直通全槽 missing 的语义空 facts）。
            return self._fall_back(
                record_id, text, "zero_facts_rejected")

        cache_hit = bool(report.get("cache_hit"))
        latency = float(report.get("latency_s", 0.0))
        tokens_in = 0
        tokens_out = 0
        if not cache_hit and self._last_call is not None:
            tokens_in = int(self._last_call.get("tokens_in", 0))
            tokens_out = int(self._last_call.get("tokens_out", 0))
            if self.budget is not None:
                try:
                    self.budget.charge(tokens_in + tokens_out)
                except FactsBudgetExceeded as error:
                    # charge 落盘失败（fail-closed）：本结果仍直通（调用已
                    # 发生、token 已耗），此后调用由 check_open 拒——账不
                    # 旁路；台账记 degraded 原因可溯。
                    self.ledger.count_budget_charge_failure()
                    self._record(record_id=record_id, text=text,
                                 facts_count=len(facts), tokens_in=tokens_in,
                                 tokens_out=tokens_out, latency_s=latency,
                                 cache_hit=cache_hit, fallback=True,
                                 fallback_reason=f"budget_charge_failed: {error}")
                    self.ledger.count_llm_path(success=False)
                    return facts
        self.ledger.count_llm_path(success=True)
        self._record(record_id=record_id, text=text, facts_count=len(facts),
                     tokens_in=tokens_in, tokens_out=tokens_out,
                     latency_s=latency, cache_hit=cache_hit)
        return facts


# ---------- ①b（开工令 2026-10-11）：track 2 按需 LLM 事实抽取面 ----------


class LlmFactsOutcome(NamedTuple):
    """按需抽取结果（接口冻结面——decide 层结构性消费，鸭子型属性访问）。

    - facts：LLM facts 原样（facts/llm.py 合同形——evidence 引文真跨度
      +record_id 归属）；None=不可得；
    - available：True=LLM facts 可用（source="llm"|"cache"）；False=
      不可得（预算耗尽无缓存/抽取失败/零事实被拒——回落纪律由 decide
      层执行：rule 回落只补证据+失败闭门）；
    - source："llm"（新调用）|"cache"（缓存命中——零 token 消耗）|
      "unavailable"；
    - reason：不可得原因（审计串；available 时 None）。
    """

    facts: list[dict] | None
    available: bool
    source: str
    reason: str | None


class _BudgetRefusedCall(LlmExtractionError):
    """①b 令3 预算闸拒绝（调用时刻执法——探针/预检与调用间 TOCTOU 兜底
    ）：LlmExtractionError 子类→facts/llm.py 重试环确定性直抛（不重试
    ），extract() 归类 budget_exceeded 不可得。"""


def _record_extraction(ledger: FactExtractionLedger, *, model: str,
                       record_id: str, text: str, facts_count: int,
                       tokens_in: int = 0, tokens_out: int = 0,
                       latency_s: float = 0.0, cache_hit: bool = False,
                       fallback: bool = False,
                       fallback_reason: str | None = None) -> None:
    """台账逐条记账（与 LlmFactSupply._record 同键集——两供给面账面同构）。"""
    text_sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
    ledger.record({
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "record_id": record_id,
        "text_sha256": text_sha,
        "model": model,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "latency_s": round(float(latency_s), 3),
        "cache_hit": cache_hit,
        "fallback": fallback,
        "fallback_reason": fallback_reason,
        "n_facts": facts_count,
    })


class OnDemandLlmFacts:
    """①b track 2：按需 LLM 事实抽取面（decide 层注入协议参考实现）。

    【接口冻结清单（P0 运行时装配对接面）】
    - 协议：``extract(record_id: str, text: str) -> LlmFactsOutcome``
      （幂等性不要求——decide 层每任务按 (record_id, text) 记忆化，
      同文多次判定只抽一次）；
    - 构造面：call_fn（注入 mock/默认 DashScope urllib）|model|base_url
      |api_key|cache_dir|timeout_s（缺席=DEDUP_FACT_TIMEOUT_S/30.0）
      |max_retries=2|ledger|budget|token_counter|env；
    - 语义（与 decide_for_task(llm_facts=...) 消费面共同冻结）：
      · 缓存键四元（令2）：raw_text_hash+model+prompt_version+
        extractor_schema_version——遗产条目（无 schema 维）miss 重抽；
      · 日 token 耗尽合同（令3）：探针命中（cache_entry_usable）→不经
        预算（已有缓存继续用）；未命中→预算闸在调用前（停新增，诚实
        零 token 消耗）+调用时刻 belt 再核（TOCTOU 兜底）；拒绝→
        unavailable(budget_exceeded)+告警计数 budget_exhausted_events；
      · 失败闭门（令4）：抽取失败/零事实被拒→unavailable（**不做
        rule 替换**——F3 无条件面失败回落 rule 供给结果，按需面回落
        纪律由 decide 层执行：rule 回落只补证据不越 R7 硬门槛）。

    与 LlmFactSupply（①令 F3 unconditional 面）的关系：共用
    extract_facts_llm 抽取器+缓存+日闸+台账件；供给语义分野——F3 面
    整批文档供给（build_commit_inputs 调用），本面判径对按需（decide
    层调用），失败处置分道（见上）。
    """

    def __init__(self, *, call_fn: Callable[..., tuple[str, float]] | None = None,
                 model: str | None = None,
                 base_url: str | None = None,
                 api_key: str | None = None,
                 cache_dir: str | os.PathLike[str] | None = None,
                 timeout_s: float | None = None,
                 max_retries: int = 2,
                 ledger: FactExtractionLedger | None = None,
                 budget: FactsDailyTokenBudget | None = None,
                 token_counter: Callable[[str], tuple[int, str]] | None = None,
                 env: Mapping[str, str] | None = None) -> None:
        self._env = os.environ if env is None else env
        self.model = model if model is not None else _facts_llm.DEFAULT_MODEL
        self.base_url = base_url
        self.api_key = api_key
        self.cache_dir = cache_dir
        self.timeout_s = (timeout_s if timeout_s is not None
                          else fact_timeout_from_env(self._env))
        self.max_retries = max_retries
        self.ledger = ledger if ledger is not None else FactExtractionLedger()
        self.budget = budget if budget is not None else FactsDailyTokenBudget()
        self._token_counter = (token_counter if token_counter is not None
                               else _count_text_tokens)
        self._call_fn = call_fn

    # ---------- 内部 ----------

    def _accounting_call_fn(self, call_state: dict) -> Callable:
        """包 call_fn：调用时刻预算 belt 再核（TOCTOU）+tokens in/out 计数
        （cache 命中不经此处——零 token 消耗）；逐调用闭包态（线程安全，
        与 LlmFactSupply 实例态计数分野）。"""
        inner = self._call_fn
        base_url = (self.base_url if self.base_url is not None
                    else _facts_llm.DEFAULT_BASE_URL)
        api_key = self.api_key or (self._env.get("QWEN_API_KEY") or "")
        counter = self._token_counter

        def wrapped(*, model: str, text: str, timeout_s: float) -> tuple[str, float]:
            try:
                self.budget.check_open()
            except FactsBudgetExceeded as error:
                raise _BudgetRefusedCall(str(error)) from None
            if inner is None:
                content, latency = _facts_llm._default_call_fn(
                    model=model, base_url=base_url, api_key=api_key,
                    text=text, timeout_s=timeout_s)
            else:
                content, latency = inner(model=model, text=text,
                                         timeout_s=timeout_s)
            tokens_in = counter(_facts_llm._SYSTEM_PROMPT)[0] + counter(text)[0]
            tokens_out = counter(content)[0]
            call_state.update(tokens_in=tokens_in, tokens_out=tokens_out,
                              latency_s=latency)
            return content, latency

        return wrapped

    def _unavailable(self, record_id: str, text: str, reason: str, *,
                     budget_exhausted: bool = False) -> LlmFactsOutcome:
        self.ledger.count_llm_path(success=False)
        if budget_exhausted:
            self.ledger.count_budget_exhausted()
        _record_extraction(self.ledger, model=self.model, record_id=record_id,
                           text=text, facts_count=0, fallback=True,
                           fallback_reason=reason)
        return LlmFactsOutcome(None, False, "unavailable", reason)

    # ---------- ①b 协议 ----------

    def extract(self, record_id: str, text: str) -> LlmFactsOutcome:
        """按需抽取单文（decide 层判径对调用；协议见类 docstring）。"""
        if not isinstance(text, str):
            raise TypeError("text must be str")
        if not text or not text.strip():
            # 空文本：抽取器合同=[]（零事实）——按需面如实报不可得
            # （判径上游最小长度闸本已滤除，防御面）。
            return LlmFactsOutcome(None, False, "unavailable", "empty_text")
        # 令3 缓存优先（只读探针）：命中→不经预算（已有缓存继续用）；
        # 未命中（含遗产条目键门失配）→预算闸在调用前（停新增）。
        cache_usable = (self.cache_dir is not None
                        and _facts_llm.cache_entry_usable(
                            self.cache_dir, text, model=self.model))
        if not cache_usable:
            try:
                self.budget.check_open()
            except FactsBudgetExceeded as error:
                return self._unavailable(
                    record_id, text, f"budget_exceeded: {error}",
                    budget_exhausted=True)
        report: dict = {}
        call_state: dict = {}
        try:
            facts = _facts_llm.extract_facts_llm(
                record_id, text, call_fn=self._accounting_call_fn(call_state),
                model=self.model, base_url=self.base_url,
                api_key=self.api_key, cache_dir=self.cache_dir,
                timeout_s=self.timeout_s, max_retries=self.max_retries,
                report_hook=report.update)
        except _BudgetRefusedCall as error:
            return self._unavailable(
                record_id, text, f"budget_exceeded: {error}",
                budget_exhausted=True)
        except LlmExtractionError as error:
            # 超时/API 失败/JSON 非法/预算软边界——失败闭门（令4）：不
            # 做 rule 替换，如实报不可得（decide 层 rule 回落只补证据）。
            return self._unavailable(
                record_id, text,
                f"llm_extraction_failed: {type(error).__name__}: {error}")
        except OSError as error:
            return self._unavailable(
                record_id, text,
                f"llm_extraction_failed: OSError: {error}")
        issues = report.get("issues") or []
        if any(ZERO_FACTS_MARKER in str(issue) for issue in issues):
            # F 项：零可定位事实被拒（含缓存命中遗产条目同兜底路径）。
            return self._unavailable(record_id, text, "zero_facts_rejected")
        cache_hit = bool(report.get("cache_hit"))
        latency = float(report.get("latency_s", 0.0))
        tokens_in = int(call_state.get("tokens_in", 0))
        tokens_out = int(call_state.get("tokens_out", 0))
        if not cache_hit and call_state:
            try:
                self.budget.charge(tokens_in + tokens_out)
            except FactsBudgetExceeded as error:
                # charge 落盘失败（fail-closed，R14 同构）：结果已取得照实
                # 直通（token 已耗、facts 真实），此后调用由 check_open
                # 拒——账不旁路；告警计数入台账（budget_charge_failures）。
                self.ledger.count_budget_charge_failure()
                _log.warning("facts budget charge failed (result kept, "
                             "future calls refused): %s", error)
        self.ledger.count_llm_path(success=True)
        _record_extraction(self.ledger, model=self.model, record_id=record_id,
                           text=text, facts_count=len(facts),
                           tokens_in=tokens_in, tokens_out=tokens_out,
                           latency_s=latency, cache_hit=cache_hit)
        return LlmFactsOutcome(list(facts), True,
                               "cache" if cache_hit else "llm", None)


# ---------- 工厂（T 接线替换口，令 5 文档化） ----------


def resolve_fact_supply(
        env: Mapping[str, str] | None = None,
        **llm_kwargs: Any) -> Callable[[str, str], list[dict]]:
    """env 单源工厂（组合根/T 工装替换口）：

    - DEDUP_FACT_SUPPLY 缺席/空="rule" → RuleFactSupply()（T 现役逐字节
      等价——缺席零翻转）；
    - "llm" → LlmFactSupply(env=env, **llm_kwargs)（cache_dir/ledger/
      budget/call_fn 等由调用方显式给）；
    - 非法 → FactSupplyModeInvalid（fail-closed）。

    注意：F3 装配缺省（不调本工厂）仍为 IdentityFactSupply——21:4x
    裁定形态零触碰；本工厂仅供显式选择供给的装配根使用。
    """
    mode = fact_supply_mode_from_env(env)
    if mode == "llm":
        return LlmFactSupply(env=env, **llm_kwargs)
    from news_flash_dedup.recall.service import RuleFactSupply
    return RuleFactSupply()


def build_llm_facts_from_env(
        env: Mapping[str, str] | None = None,
        **kwargs: Any) -> OnDemandLlmFacts | None:
    """按需 LLM facts 注入工厂（最终接线窗，P1c 装配注入设计单 §二-B）。

    注入位 B 预装配专用（shadow_decide/commit_one 的 ``llm_facts is None``
    位；显式注入=注入位 A 优先，不经本工厂）：

    - ``DEDUP_LLM_FACTS_ONDEMAND`` 缺席/空/非真值 → **None**（decide_
      for_task 缺省双轨关，一切现役语义逐字节——fail-closed 默认关，
      judge_pair._SWITCH_ON_VALUES 同款真值集 "1"/"true"/"yes"/"on"）；
    - 开 → ``OnDemandLlmFacts(env=…)``（构造面 env 单源：超时沿
      DEDUP_FACT_TIMEOUT_S[构造器缺省自读]、日 token 闸沿 DEDUP_FACT_
      DAILY_TOKEN_BUDGET[本工厂显式接线——OnDemandLlmFacts 构造缺省不
      自读 budget env；两轨道共用同一 env 档，P1c 设计单 §五-3 共用裁量]；
      model/base_url/api_key 沿构造器缺省[DEFAULT_MODEL/DEFAULT_BASE_URL/
      QWEN_API_KEY 现读]）。非法 knob 值在构造时刻 ValueError fail-closed
      （fact_timeout_from_env/fact_daily_token_budget_from_env 同纪律）；
    - ``**kwargs`` 直通 OnDemandLlmFacts 构造面（call_fn/ledger/cache_dir/
      budget 等——罐装测试/未来 T 窗生产接线位；``env`` 键由本工厂单源
      传入， kwargs 携带即弹出丢弃防重复实参；显式 ``budget`` 在场优先，
      缺省才 env 接线）。跨任务预算持久化（store_path）与磁盘缓存
      （cache_dir）归 T 窗生产接线（FactsDailyTokenBudget docstring 同
      口径——缺省内存态）。
    """
    source = os.environ if env is None else env
    raw = (source.get(LLM_FACTS_ONDEMAND_ENV) or "").strip().lower()
    if raw not in _LLM_FACTS_ONDEMAND_ON:
        return None
    kwargs.pop("env", None)        # env 由本工厂单源传（防重复实参）
    budget = kwargs.pop("budget", None)
    if budget is None:
        budget = FactsDailyTokenBudget(
            daily_tokens=fact_daily_token_budget_from_env(source))
    return OnDemandLlmFacts(env=source, budget=budget, **kwargs)


__all__ = [
    "DEFAULT_FACT_DAILY_TOKEN_BUDGET",
    "DEFAULT_FACT_TIMEOUT_S",
    "FACT_DAILY_TOKEN_BUDGET_ENV",
    "FACT_SUPPLY_ENV",
    "FACT_SUPPLY_MODES",
    "FACT_SUPPLY_MODE_DEFAULT",
    "FACT_TIMEOUT_ENV",
    "FactExtractionLedger",
    "FactSupplyModeInvalid",
    "FactsBudgetExceeded",
    "FactsDailyTokenBudget",
    "LLM_FACTS_ONDEMAND_ENV",
    "LlmFactSupply",
    "LlmFactsOutcome",
    "OnDemandLlmFacts",
    "ZERO_FACTS_MARKER",
    "build_llm_facts_from_env",
    "fact_daily_token_budget_from_env",
    "fact_supply_mode_from_env",
    "fact_timeout_from_env",
    "resolve_fact_supply",
]
