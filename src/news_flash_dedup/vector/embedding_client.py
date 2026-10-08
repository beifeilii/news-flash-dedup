"""B5/E2 S1 模型网关：真 text-embedding-v3 嵌入客户端（设计 §2 全件）。

职责单一：文本 → dimension 维 float 向量。形态对齐两个已验证先例：
- 调用形态 = log/temp/n11-embedding-admission.py:27-45（OpenAI 兼容
  POST {base}/embeddings，{"model","input":[...]}，Bearer key，零依赖 urllib）；
- 限速/重试/缓存形态 = mabc_service rag/embedding.py（§2.4 逐项取舍在案）。

失败语义（§2.5 三态映射）：
- API 终败（429 超退避/连接断/超时/4xx 非 429）→ EmbeddingApiError
  （写径 = 确定的任务失败，重试安全；查询径由 embedding_query 映射 unavailable）；
  429 退避尊重服务端 Retry-After 头（W-R3c 散项 e：有则照退避，缺/畸形
  回退内建 min(60,2·2^attempt)+jitter 公式）；
- 响应畸形（响应体超 1MB 硬上限（W-R3c 散项 d，对齐 facts/llm.py:105-112）/
  data 数≠input 数 / index 乱序缺错 / 维度≠dimension / 非有限 float /
  空向量）→ VectorWriteUnknown("embedding response is unknown")——确认未知，
  fail-closed，不抛 EmbeddingApiError（三态分立）；维度漂移另记 canary 事件
  （R1/R2）；index 对位核验（R3-H4-①）：逐项 index 必须恰为批次位置
  （0..n-1 顺序恒等），乱序/缺 index/错位/布尔冒充皆确认未知；
- 铁律：任何失败路径不产出零向量/均值向量冒充成功（INV-1 嵌入域同构）。

预算闸（§2.4 + §6.3 + R14）：DEDUP_EMBEDDING_DAILY_TOKEN_BUDGET 默认 5M tok/日；
usage.total_tokens 逐响应累计；缺 usage 按 UTF-8 字节数保守上界估值（闸不旁路），
连续 3 批缺 usage 告警；计数器落盘 JSON（cache_root 同域）、启动加载、业务日
（UTC+8）翻转重置、落盘失败 fail-closed——重启不可绕闸。N3-06：usage 台账
计数器读写面与 usage_snapshot 同一 _budget_lock 锁域（台账精度，预算闸本体
受锁面不动）。

缓存（§2.3 + R149 候选 A 布局）：键 = sha256(model|dim|preprocessing|text)；
磁盘布局 cache_root/<model>/<dimension>/<preprocessing>/<cache_key>.json，
条目只存向量+created_at（不存正文）；整批作废 = 删目录整树 + 台账登记 +
同进程 LRU 显式清空（R13）。查询侧内存 LRU 512 + 可开合磁盘层。
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import random
import shutil
import socket
import threading
import time
import urllib.error
import urllib.request
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from news_flash_dedup.recall.vector_store import VectorWriteUnknown


# ---------- 限速常量（§2.4，mabc embedding.py:16-18 先例） ----------
INGEST_CONCURRENCY = 2          # ingest（写入/回填）信号量
QUERY_CONCURRENCY = 8           # query（召回）信号量
QUERY_CACHE_SIZE = 512          # 查询侧内存 LRU（mabc _EMBED_CACHE_SIZE 先例）
MAX_BATCH_SIZE = 10             # DashScope 硬上限（N11 §一）
DEFAULT_DAILY_TOKEN_BUDGET = 5_000_000   # §6.3：5M tok/日（生产高沿 3.42M 的 ≈1.46×）
DEFAULT_TIMEOUT_S = 30          # N11 诊断件 urlopen timeout=30
RATE_LIMIT_MAX_ATTEMPTS = 6     # 429：min(60, 2·2^attempt)+jitter(0,1)，最多 6 次
CONNECTION_MAX_ATTEMPTS = 3     # 连接/超时：线性 0.5·(attempt+1)，3 次
USAGE_MISSING_STREAK_ALARM = 3  # 连续缺 usage 批数告警阈（R14）
MAX_RESPONSE_BYTES = 1024 * 1024  # W-R3c 散项 d：响应体 1MB 硬上限（对齐
                                  # facts/llm.py:105-112 口径——读上限+1 判
                                  # 溢出，超长拒收不静默截断不无界 read）

_BUSINESS_TZ = timezone(timedelta(hours=8))   # 业务日（快讯域日口径 UTC+8）


# ---------- 异常词表（三态映射 §2.5） ----------

class EmbeddingApiError(RuntimeError):
    """嵌入 API 终败（429 超退避 / 连接断 / 超时 / 确定性 4xx / 缺 key）。

    写径 = 确定的任务失败（嵌入是纯函数无部分态，重试安全；不抛
    VectorWriteUnknown——那是确认未知专用）；查询径由
    recall/embedding_query.py 映射 unavailable + EMBEDDING_QUERY_FAILED。
    """


class EmbeddingBudgetExceeded(EmbeddingApiError):
    """日 token 预算闸超闸 / 计数器落盘失败——fail-closed，不静默降级（R14）。"""


class EmbeddingKeyMissing(EmbeddingApiError):
    """key 引用缺失（DEDUP_EMBEDDING_API_KEY / DEDUP_EMBEDDING_KEY_FILE 均不可得）。"""


# transport 内部故障分类（重试策略分流；非领域语义，不进对外词表）
class EmbeddingRateLimited(Exception):
    """HTTP 429——指数退避，最多 6 次。

    W-R3c 散项 e：retry_after 携带服务端 Retry-After 头秒数（None=头缺/
    畸形/负值，回退内建 min(60,2·2^attempt)+jitter 公式）；调用侧有则照
    退避（尊重头），无则内建公式不动。
    """

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class EmbeddingConnectionFault(Exception):
    """连接/超时/5xx——线性退避，3 次。"""


class EmbeddingHttpTerminal(Exception):
    """HTTP 4xx 非 429——确定性失败，不重试。"""

    def __init__(self, status: int, detail: str = "") -> None:
        super().__init__(f"HTTP {status} {detail}".strip())
        self.status = status


# ---------- 缓存键与 key 引用（§2.2/§2.3） ----------

def cache_key(model: str, dimension: int, preprocessing: str, text: str) -> str:
    """sha256(model|dimension|preprocessing|text)——跨模型/维度/预处理零串扰（R12）。

    键含 model+dimension 为强制（任务书要求）；preprocessing 段纳归一化/截断
    版本（初版 p09_v1，与空间配置同名同步演进）。N23 附注 1 LLM 缓存单槽教训
    （文件名=text_sha256 单槽、plus 覆写 turbo 条目）为键含模型必要性佐证。
    """
    material = "|".join((model, str(dimension), preprocessing, text))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def resolve_api_key(env: Mapping[str, str] | None = None) -> str | None:
    """key 环境引用（§2.2）：DEDUP_EMBEDDING_API_KEY 优先；
    DEDUP_EMBEDDING_KEY_FILE 备选（指向 key 文件，现阶段 = mabc_service/.env
    的 QWEN_API_KEY 行，现读不落地；单引号 strip 仅兜底，T1/T2 教训）。禁抄值。
    """
    env = env if env is not None else os.environ
    direct = (env.get("DEDUP_EMBEDDING_API_KEY") or "").strip()
    if direct:
        return direct
    key_file = (env.get("DEDUP_EMBEDDING_KEY_FILE") or "").strip()
    if not key_file:
        return None
    try:
        text = Path(key_file).read_text(encoding="utf-8")
    except OSError:
        return None
    value: str | None = None
    for line in text.splitlines():
        if line.startswith("QWEN_API_KEY=") and not line.startswith("#"):
            value = line.split("=", 1)[1].strip()
            break
    if value is None:
        candidate = text.strip()
        value = candidate if candidate and "\n" not in candidate else None
    if not value:
        return None
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        value = value[1:-1]  # .env 单/双引号 strip（去引号由导出侧保证，此为兜底）
    return value or None


def mask_api_key(key: str | None) -> str:
    """证据/日志掩码（§2.2）：N11 文档形态 sk-b1b6...918a；短键/缺失全掩。"""
    if not key or len(key) < 12:
        return "***"
    return key[:6] + "..." + key[-4:]


# ---------- 配置 ----------

@dataclass(frozen=True)
class EmbeddingClientConfig:
    """嵌入客户端配置（§2.2 取值表逐项落位）。"""
    base_url: str
    model: str
    dimension: int
    cache_root: Path | None
    batch_size: int = MAX_BATCH_SIZE
    max_input_chars: int = 6000                      # N11 §三护栏
    preprocessing: str = "p09_v1"                    # 与空间配置同名同步演进
    daily_token_budget: int = DEFAULT_DAILY_TOKEN_BUDGET
    budget_store_path: Path | None = None            # 默认 cache_root/budget_counter.json
    query_disk_cache_enabled: bool = False           # shadow 期间开；生产默认关
    timeout_s: int = DEFAULT_TIMEOUT_S

    def __post_init__(self) -> None:
        if not self.base_url or not self.model:
            raise ValueError("base_url and model must be nonempty")
        if type(self.dimension) is not int or self.dimension < 1:
            raise ValueError("dimension must be positive")
        if type(self.batch_size) is not int or not 1 <= self.batch_size <= MAX_BATCH_SIZE:
            raise ValueError("batch_size must be within 1..10 (DashScope cap)")
        if type(self.max_input_chars) is not int or self.max_input_chars < 1:
            raise ValueError("max_input_chars must be positive")
        if type(self.daily_token_budget) is not int or self.daily_token_budget < 1:
            raise ValueError("daily_token_budget must be positive")


# ---------- 默认 transport（N11 诊断件同款零依赖 urllib） ----------

def _retry_after_seconds(headers: Any) -> float | None:
    """W-R3c 散项 e：解析 429 Retry-After 秒数头；头缺/畸形/非有限/负值
    → None（回退内建退避公式，不捏造服务端意图）。HTTP-date 形态不解析
    （DashScope/OpenAI 兼容面实发秒数），同按 None 回退。"""
    if headers is None:
        return None
    raw = headers.get("Retry-After")
    if raw is None:
        return None
    try:
        seconds = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(seconds) or seconds < 0:
        return None
    return seconds


def urllib_transport(*, base_url: str, model: str, api_key: str,
                     texts: Sequence[str], timeout_s: int) -> dict:
    """OpenAI 兼容 POST {base}/embeddings；故障按重试策略分流分类。"""
    body = json.dumps({"model": model, "input": list(texts)}).encode("utf-8")
    request = urllib.request.Request(
        base_url.rstrip("/") + "/embeddings", data=body,
        headers={"Authorization": f"Bearer {api_key}",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            # W-R3c 散项 d：响应体 1MB 硬上限（对齐 facts/llm.py:105-112
            # 口径——读上限+1 判溢出，超长拒收不静默截断；修复前无界 read
            # 让失控端点可耗尽内存）。合法 embeddings 响应远小于此；溢出
            # 按响应畸形族 → VectorWriteUnknown（确认未知 fail-closed）。
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise VectorWriteUnknown("embedding response is unknown")
            # W-A 族④(b)（A2-19）：200-非-JSON=响应畸形族，与 1MB 溢出/
            # 结构畸形同词汇 → VectorWriteUnknown（确认未知 fail-closed；
            # 传导链 embedding_query.py → unavailable 记账不崩溃）；文案与
            # :235 全族逐字节一致，不引入新异常风格分支。
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise VectorWriteUnknown("embedding response is unknown") from None
    except urllib.error.HTTPError as error:
        if error.code == 429:
            # W-R3c 散项 e：Retry-After 头秒数随异常携带（尊重头）
            raise EmbeddingRateLimited(
                f"HTTP 429 {error.reason}",
                retry_after=_retry_after_seconds(error.headers)) from None
        if 500 <= error.code:
            raise EmbeddingConnectionFault(f"HTTP {error.code} {error.reason}") from None
        raise EmbeddingHttpTerminal(error.code, str(error.reason)) from None
    except (urllib.error.URLError, TimeoutError, socket.timeout, ConnectionError,
            OSError) as error:
        raise EmbeddingConnectionFault(str(error)) from None
    if not isinstance(payload, dict):
        # 顶层非 JSON 对象 = 确认未知（交由响应校验统一映射）
        return {"data": None}
    return payload


# ---------- 客户端 ----------

class EmbeddingClient:
    """真 embedding 网关（§2.1–2.5 全件；transport/时钟/睡眠/随机源可注入）。"""

    def __init__(self, config: EmbeddingClientConfig, *,
                 transport_fn: Callable[..., dict] | None = None,
                 env: Mapping[str, str] | None = None,
                 clock: Callable[[], datetime] | None = None,
                 sleep: Callable[[float], None] | None = None,
                 rng: Callable[[], float] | None = None,
                 persist_budget: bool = True,
                 budget: "ProcessingBudget | None" = None) -> None:
        self._config = config
        self._transport = transport_fn or urllib_transport
        self._env = env if env is not None else os.environ
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._sleep = sleep or time.sleep
        self._rng = rng or (lambda: random.uniform(0.0, 1.0))
        self._ingest_semaphore = threading.Semaphore(INGEST_CONCURRENCY)
        self._query_semaphore = threading.Semaphore(QUERY_CONCURRENCY)
        self._cache_lock = threading.Lock()
        self._query_cache: OrderedDict[str, list[float]] = OrderedDict()
        self.alarms: list[dict] = []          # canary/告警事件台账（R1/R2/R13/R14）
        self._usage_missing_streak = 0
        self._budget_lock = threading.Lock()
        self._budget_broken = False
        self._usage = {"total_tokens": 0, "api_calls": 0, "cache_hits": 0,
                       "estimated_usage_events": 0}
        # R14：预算计数器必须落盘（重启不可绕闸）；路径解析失败即构造期拒绝。
        self._budget_path: Path | None = config.budget_store_path
        if self._budget_path is None and config.cache_root is not None:
            self._budget_path = Path(config.cache_root) / "budget_counter.json"
        if persist_budget and self._budget_path is None:
            raise ValueError(
                "budget counter persistence path required "
                "(cache_root or budget_store_path; R14 重启不可绕闸)"
            )
        self._budget_day, self._budget_used = self._load_budget_counter()
        # W2 ⑩①-7（N43 挂账清偿，12 L377 统一计账义务）：W2 运行时
        # ProcessingBudget（每条记录一个逻辑预算）——与上方 R14 每日
        # token 账户（_budget_lock/_budget_path/_budget_used 族）是两笔
        # 独立账，本件只承担"逻辑调用次数"计账+软边界闸；None=零计账
        # 零闸（现役逐字节）。
        self._budget = budget

    # ---------- key ----------

    @property
    def dimension(self) -> int:
        """空间维度（INV-4 装配防线：QueryEmbedder/pipeline 装配期对拍）。"""
        return self._config.dimension

    @property
    def max_input_chars(self) -> int:
        return self._config.max_input_chars

    def _api_key(self) -> str:
        key = resolve_api_key(self._env)
        if key is None:
            raise EmbeddingKeyMissing(
                "embedding API key unresolved: set DEDUP_EMBEDDING_API_KEY or "
                "DEDUP_EMBEDDING_KEY_FILE (key 环境引用，禁抄值)"
            )
        return key

    # ---------- 预算闸（R14：落盘 + 启动加载 + 日翻转 + 落盘失败 fail-closed） ----------

    def _business_day(self) -> str:
        now = self._clock()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        return now.astimezone(_BUSINESS_TZ).date().isoformat()

    def _load_budget_counter(self) -> tuple[str, int]:
        today = self._business_day()
        path = self._budget_path
        if path is None or not path.exists():
            return today, 0
        try:
            body = json.loads(path.read_text(encoding="utf-8"))
            day = body.get("business_date")
            used = body.get("used_tokens")
        except (OSError, ValueError):
            # 计数器不可读 = 确认未知 → fail-closed（不当作零用量放行）
            self._budget_broken = True
            self.alarms.append({"type": "budget_counter_unreadable",
                                "at": self._now_iso()})
            return today, 0
        if day != today or type(used) is not int or used < 0:
            return today, 0  # 业务日翻转重置
        return day, used

    def _persist_budget_counter(self) -> None:
        assert self._budget_path is not None
        body = {"business_date": self._budget_day, "used_tokens": self._budget_used,
                "updated_at": self._now_iso()}
        temp = self._budget_path.with_suffix(".json.tmp")
        self._budget_path.parent.mkdir(parents=True, exist_ok=True)
        temp.write_text(json.dumps(body, ensure_ascii=False, indent=2),
                        encoding="utf-8")
        os.replace(temp, self._budget_path)

    def _charge_tokens(self, tokens: int) -> None:
        """计账 + 落盘；落盘失败 fail-closed（此后一切调用拒）。"""
        with self._budget_lock:
            self._budget_used += tokens
            if self._budget_path is not None:
                try:
                    self._persist_budget_counter()
                except OSError:
                    self._budget_broken = True
                    self.alarms.append({"type": "budget_counter_persist_failed",
                                        "at": self._now_iso()})
                    raise EmbeddingBudgetExceeded(
                        "budget counter persistence failed (fail-closed, R14)"
                    ) from None

    def _check_budget_open(self) -> None:
        with self._budget_lock:
            if self._budget_broken:
                raise EmbeddingBudgetExceeded(
                    "budget counter persistence failed (fail-closed, R14)"
                )
            today = self._business_day()
            if today != self._budget_day:       # 业务日翻转重置
                self._budget_day = today
                self._budget_used = 0
                if self._budget_path is not None:
                    try:
                        self._persist_budget_counter()
                    except OSError:
                        self._budget_broken = True
                        raise EmbeddingBudgetExceeded(
                            "budget counter persistence failed (fail-closed, R14)"
                        ) from None
            if self._budget_used >= self._config.daily_token_budget:
                self.alarms.append({"type": "budget_exceeded",
                                    "used_tokens": self._budget_used,
                                    "budget": self._config.daily_token_budget,
                                    "at": self._now_iso()})
                raise EmbeddingBudgetExceeded(
                    f"daily token budget exceeded: {self._budget_used} >= "
                    f"{self._config.daily_token_budget} (fail-closed, §6.3)"
                )

    # ---------- 用量台账 ----------

    def usage_snapshot(self) -> dict:
        """usage/成本台账（随 shadow/UAT 证据 JSON 落盘，N11 total_tokens 同口径）。"""
        with self._budget_lock:
            return {**self._usage, "budget_used_tokens": self._budget_used,
                    "budget_business_date": self._budget_day,
                    "daily_token_budget": self._config.daily_token_budget}

    def _bump_usage(self, field: str) -> None:
        """计数器单步自增（N3-06：读写面与 usage_snapshot 同一锁域，台账精度）。"""
        with self._budget_lock:
            self._usage[field] += 1

    def query_cache_size(self) -> int:
        with self._cache_lock:
            return len(self._query_cache)

    def _now_iso(self) -> str:
        now = self._clock()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        return now.isoformat()

    # ---------- 缓存（§2.3 候选 A 分目录布局） ----------

    def _entry_path(self, key: str) -> Path | None:
        if self._config.cache_root is None:
            return None
        return (Path(self._config.cache_root) / self._config.model
                / str(self._config.dimension) / self._config.preprocessing
                / f"{key}.json")

    def _read_disk_cache(self, key: str) -> list[float] | None:
        path = self._entry_path(key)
        if path is None or not path.exists():
            return None
        try:
            body = json.loads(path.read_text(encoding="utf-8"))
            vector = body.get("vector") if isinstance(body, dict) else None
        except (OSError, ValueError):
            return None  # 崩损条目按 miss 自愈重写（缓存是优化，非确认面）
        if (isinstance(vector, list) and len(vector) == self._config.dimension
                and all(type(v) in (int, float) and math.isfinite(v) for v in vector)):
            return [float(v) for v in vector]
        return None

    def _write_disk_cache(self, key: str, vector: Sequence[float]) -> None:
        path = self._entry_path(key)
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temp = path.with_suffix(".json.tmp")
            temp.write_text(json.dumps(
                {"vector": [float(v) for v in vector],
                 "created_at": self._now_iso()}, ensure_ascii=False),
                encoding="utf-8")  # 条目只存向量+created_at，不存正文（§2.3）
            os.replace(temp, path)
        except OSError:
            pass  # 缓存写失败 = 优化失效，非确认面（预算计数器才是 fail-closed 面）

    def invalidate_cache(self, *, dimension: int | None = None,
                         preprocessing: str | None = None) -> dict:
        """R13 作废规程：漂移事件 → 按目录整批删除 + 同进程 LRU 显式清空。

        默认删本 model 整树 cache_root/<model>/；dimension/preprocessing 给定时
        只删对应子树。目录列举即可枚举，零哈希反解（R149 候选 A 布局收益）。
        返回作废事件（台账登记用）。
        """
        deleted: list[str] = []
        root = self._config.cache_root
        if root is not None:
            target = Path(root) / self._config.model
            if dimension is not None:
                target = target / str(dimension)
                if preprocessing is not None:
                    target = target / preprocessing
            if target.exists():
                shutil.rmtree(target)
                deleted.append(str(target))
        with self._cache_lock:
            cleared = len(self._query_cache)
            self._query_cache.clear()
        event = {"type": "cache_invalidated", "model": self._config.model,
                 "dimension": dimension, "preprocessing": preprocessing,
                 "deleted_dirs": deleted, "lru_cleared": cleared,
                 "at": self._now_iso()}
        self.alarms.append(event)
        return event

    # ---------- 截断护栏（§2.2；mabc _truncate 先例） ----------

    def _truncate(self, text: str) -> str:
        if len(text) <= self._config.max_input_chars:
            return text
        self.alarms.append({"type": "input_truncated",
                            "original_chars": len(text),
                            "kept_chars": self._config.max_input_chars,
                            "at": self._now_iso()})
        return text[: self._config.max_input_chars]

    # ---------- 响应校验（§2.5 畸形 → VectorWriteUnknown 确认未知） ----------

    def _validate_payload(self, payload: Any, batch: Sequence[str]) -> list[list[float]]:
        data = payload.get("data") if isinstance(payload, Mapping) else None
        if not isinstance(data, list) or len(data) != len(batch):
            raise VectorWriteUnknown("embedding response is unknown")
        vectors: list[list[float]] = []
        for position, item in enumerate(data):
            # R3-H4-① index 对位：逐项 index 必须恰为批次位置（0..n-1 顺序恒等）。
            # 乱序/缺 index/错位 → 确认未知 fail-closed（不按位置盲信追加——
            # 对位错误会把向量错挂到别的文本上，精度面不可自愈）；
            # type 精确判定拒 bool 冒充（True==1 朴素相等陷阱）。
            index = item.get("index") if isinstance(item, Mapping) else None
            if type(index) is not int or index != position:
                raise VectorWriteUnknown("embedding response is unknown")
            vector = item.get("embedding") if isinstance(item, Mapping) else None
            if not isinstance(vector, (list, tuple)):
                raise VectorWriteUnknown("embedding response is unknown")
            if len(vector) != self._config.dimension:
                self.alarms.append({"type": "dimension_drift",
                                    "expected": self._config.dimension,
                                    "got": len(vector), "at": self._now_iso()})
                raise VectorWriteUnknown("embedding response is unknown")
            if (any(type(v) not in (int, float) or not math.isfinite(v)
                    for v in vector)
                    or not any(v != 0 for v in vector)):
                raise VectorWriteUnknown("embedding response is unknown")
            vectors.append([float(v) for v in vector])
        return vectors

    def _account_usage(self, payload: Any, batch: Sequence[str]) -> None:
        usage = payload.get("usage") if isinstance(payload, Mapping) else None
        tokens = usage.get("total_tokens") if isinstance(usage, Mapping) else None
        # N3-06：计数器/连击读写面受 _budget_lock（与 usage_snapshot 同一锁域）；
        # _charge_tokens 自持锁，必须在锁外调用（threading.Lock 非可重入）。
        with self._budget_lock:
            if type(tokens) is not int or tokens < 0:
                # R14：缺 usage → UTF-8 字节数保守上界估值（byte-level BPE 下
                # 1 token ≥ 1 byte 恒成立，字节数为诚实上界；估值公式评审钉死），
                # 闸不旁路；连续 3 批告警。
                tokens = sum(len(t.encode("utf-8")) for t in batch)
                self._usage["estimated_usage_events"] += 1
                self._usage_missing_streak += 1
                if self._usage_missing_streak >= USAGE_MISSING_STREAK_ALARM:
                    self.alarms.append({"type": "usage_missing_streak",
                                        "streak": self._usage_missing_streak,
                                        "at": self._now_iso()})
            else:
                self._usage_missing_streak = 0
            self._usage["total_tokens"] += tokens
        self._charge_tokens(tokens)

    # ---------- 单批调用（重试策略 §2.4） ----------

    def _call_batch(self, api_key: str, batch: Sequence[str], *,
                    semaphore: threading.Semaphore) -> list[list[float]]:
        # W2 ⑩①-7 软预算执法面：越 accepted+prepare_soft_s→拒启新模型
        # 调用（诚实领域失败，charge 未发生；缓存命中面永不达本方法）。
        if self._budget is not None and self._budget.beyond_prepare_soft():
            raise EmbeddingApiError(
                "预算软边界：不再启动新模型调用（已启动者收尾）")
        last_fault: Exception | None = None
        for attempt in range(RATE_LIMIT_MAX_ATTEMPTS):
            # W2 ⑩①-7 统一计账：每次 transport 尝试前 charge——失败与
            # 成功同扣（runtime_budget L15），账户尽→拒付不扣。
            if self._budget is not None and not self._budget.charge_model_call():
                raise EmbeddingApiError(
                    "model call budget exhausted (unified per-record account)")
            try:
                with semaphore:
                    payload = self._transport(
                        base_url=self._config.base_url, model=self._config.model,
                        api_key=api_key, texts=list(batch),
                        timeout_s=self._config.timeout_s)
                break
            except EmbeddingRateLimited as error:
                last_fault = error
                if attempt == RATE_LIMIT_MAX_ATTEMPTS - 1:
                    raise EmbeddingApiError(
                        f"embedding rate limit retries exceeded "
                        f"({RATE_LIMIT_MAX_ATTEMPTS} attempts)") from error
                # W-R3c 散项 e：Retry-After 头在场照头退避（尊重服务端限速
                # 意图）；头缺/畸形 → 内建 min(60,2·2^attempt)+jitter 不动。
                self._sleep(error.retry_after
                            if error.retry_after is not None
                            else min(60.0, 2.0 * (2 ** attempt)) + self._rng())
            except EmbeddingConnectionFault as error:
                last_fault = error
                if attempt >= CONNECTION_MAX_ATTEMPTS - 1:
                    raise EmbeddingApiError(
                        f"embedding connection/timeout retries exhausted "
                        f"({CONNECTION_MAX_ATTEMPTS} attempts)") from error
                self._sleep(0.5 * (attempt + 1))
            except EmbeddingHttpTerminal as error:
                raise EmbeddingApiError(
                    f"embedding HTTP {error.status} terminal failure") from error
        else:  # pragma: no cover - 结构性不可达
            raise EmbeddingApiError(f"embedding call failed: {last_fault!r}")
        vectors = self._validate_payload(payload, batch)   # 畸形 → VectorWriteUnknown
        self._bump_usage("api_calls")                      # N3-06 计数器受锁
        self._account_usage(payload, batch)                # 预算闸计账（R14）
        return vectors

    # ---------- 写入径：文档嵌入（分批 ≤10 保序 + 磁盘缓存） ----------

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        """文本序列 → 向量序列（保序；磁盘缓存命中跳过 API，回放零成本）。"""
        if not texts:
            raise ValueError("texts must be non-empty")
        api_key = self._api_key()
        prepared = [self._truncate(t) for t in texts]
        results: list[list[float] | None] = []
        pending: list[tuple[int, str]] = []
        for index, text in enumerate(prepared):
            key = cache_key(self._config.model, self._config.dimension,
                            self._config.preprocessing, text)
            cached = self._read_disk_cache(key)
            if cached is not None:
                self._bump_usage("cache_hits")             # N3-06 计数器受锁
                results.append(cached)
            else:
                results.append(None)
                pending.append((index, text))
        for start in range(0, len(pending), self._config.batch_size):
            segment = pending[start:start + self._config.batch_size]
            self._check_budget_open()                        # 超闸 fail-closed 零调用
            vectors = self._call_batch(
                api_key, [text for _, text in segment],
                semaphore=self._ingest_semaphore)
            for (index, text), vector in zip(segment, vectors):
                key = cache_key(self._config.model, self._config.dimension,
                                self._config.preprocessing, text)
                self._write_disk_cache(key, vector)
                results[index] = vector
        return [v for v in results if v is not None]

    # ---------- 查询径：单文本（LRU 512 + 可开合磁盘层） ----------

    def embed_query(self, text: str) -> list[float]:
        """单查询文本 → 向量（对称编码路径 §4.1；LRU 命中零 API）。"""
        if not isinstance(text, str):
            raise TypeError("text must be str")
        api_key = self._api_key()
        prepared = self._truncate(text)
        key = cache_key(self._config.model, self._config.dimension,
                        self._config.preprocessing, prepared)
        with self._cache_lock:
            if key in self._query_cache:
                self._query_cache.move_to_end(key)
                hit = list(self._query_cache[key])
            else:
                hit = None
        if hit is not None:
            # N3-06：计数器锁域 = _budget_lock；不嵌套入 _cache_lock（锁序单向
            # cache→budget 虽无回边，出锁再计更稳，命中值已快照）
            self._bump_usage("cache_hits")
            return hit
        if self._config.query_disk_cache_enabled:
            cached = self._read_disk_cache(key)
            if cached is not None:
                self._bump_usage("cache_hits")             # N3-06 计数器受锁
                self._store_query_cache(key, cached)
                return cached
        self._check_budget_open()
        vectors = self._call_batch(api_key, [prepared],
                                   semaphore=self._query_semaphore)
        vector = vectors[0]
        self._store_query_cache(key, vector)
        if self._config.query_disk_cache_enabled:
            self._write_disk_cache(key, vector)
        return vector

    def _store_query_cache(self, key: str, vector: Sequence[float]) -> None:
        with self._cache_lock:
            self._query_cache[key] = list(vector)
            self._query_cache.move_to_end(key)
            while len(self._query_cache) > QUERY_CACHE_SIZE:
                self._query_cache.popitem(last=False)

    def clear_query_cache(self) -> int:
        """同进程续跑显式清空（§2.3 R13 钉）。"""
        with self._cache_lock:
            cleared = len(self._query_cache)
            self._query_cache.clear()
        return cleared


__all__ = [
    "CONNECTION_MAX_ATTEMPTS",
    "DEFAULT_DAILY_TOKEN_BUDGET",
    "EmbeddingApiError",
    "EmbeddingBudgetExceeded",
    "EmbeddingClient",
    "EmbeddingClientConfig",
    "EmbeddingConnectionFault",
    "EmbeddingHttpTerminal",
    "EmbeddingKeyMissing",
    "EmbeddingRateLimited",
    "INGEST_CONCURRENCY",
    "MAX_BATCH_SIZE",
    "MAX_RESPONSE_BYTES",
    "QUERY_CACHE_SIZE",
    "QUERY_CONCURRENCY",
    "RATE_LIMIT_MAX_ATTEMPTS",
    "USAGE_MISSING_STREAK_ALARM",
    "cache_key",
    "mask_api_key",
    "resolve_api_key",
    "urllib_transport",
]
