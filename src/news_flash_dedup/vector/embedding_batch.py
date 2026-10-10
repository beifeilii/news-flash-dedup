"""P1b 任务二（2026-10-11，施工窗 P1b：判官并发池+Embedding 批量，分支
infra/p1b-modules）：Embedding 真批量微批网关（独立新模块，不动现有
vector/embedding_client.py）。

任务书钉值 + 主窗批复（2026-10-11）：
- batch_size 构造校验窗 = **1..10**（主窗裁定：DashScope text-embedding-v3
  端点硬顶=10 条/批，任务书原 16~32 作废，总图记"16~32 被端点硬顶 10
  取代"）；缺省 8；越界/非 int → ValueError（fail-closed）；
- max_wait 构造校验窗 = 0.020~0.050s（20~50ms 保留），缺省 0.030；
  越界/非有限 → ValueError；
- **不凑批等待**：满批即发（batch_size 到量立刻发）；超时即按**现有量**
  发（pending 不足一批时到 max_wait 截止发现有条数，绝不为一批硬等）；
- 批量失败 → **逐条回落单发**：一批调用失败（异常/响应形态不符）→ 该批
  逐条 [单文本] 重发——一批坏一条不死全批（好条各自成活，坏条只死该条）；
- 结果**按序一一对应**：embed_documents 返回逐位 outcome（vector|error），
  位次=输入序；并发多调用方同池共批时槽位回填按位对应不串号。

形态纪律（家法对齐）：
- 纯网关：batch_call_fn(texts)→vectors 注入（真端点 transport 归接线方，
  本模块零 urllib 零网络零 env）；时钟可注入（缺省 time.monotonic，
  单测假钟定时序钉）；
- 响应形态校验（家族口径：len(data)==len(input)、逐项为非空序列）：
  不符 → EmbeddingBatchMalformed → 按批量失败走逐条回落（确认面不
  冒充成功，INV-1 同构）；
- 严格变体：embed_documents_strict / embed —— 任一项失败抛
  EmbeddingBatchError（携 .failures 位次→异常，绝不吞错冒充）。

未来接线点（本窗零接线，P0 完成后主窗统一 rebase 时装配）：
- batch_call_fn := 现有 EmbeddingClient 的 _call_batch 传输面（transport_fn
  同签名族 base_url/model/api_key/texts/timeout_s，经 functools.partial
  绑参；或直接包 client.embed_documents 的未缓存单批路径）；
- 消费点 = vector/pipeline.py ingest_record 的 client.embed_documents
  （逐记录调用）——微批网关前置此处做跨记录合批；
- 现有 EmbeddingClient（batch_size 1..10 钉面、缓存/预算闸）行为零改动。

单测（tests/unit/test_p1b_t2_embedding_batch.py，全 mock 传输层）：
批量组装钉（满批即发/切批/余量新窗）/ 超时即发钉（假钟定序：现有量
即发不凑批）/ 失败逐条回落钉（批坏→单发救好条、坏条只死该条）/ 顺序
对应钉（乱序完成/多调用方并发不串号）/ 形态不符回落钉 / 构造校验钉。
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Sequence

__all__ = [
    "MIN_BATCH_SIZE", "MAX_BATCH_SIZE", "DEFAULT_BATCH_SIZE",
    "MIN_MAX_WAIT_S", "MAX_MAX_WAIT_S", "DEFAULT_MAX_WAIT_S",
    "EmbeddingBatchConfig", "EmbeddingItemOutcome", "EmbeddingBatchError",
    "EmbeddingBatchMalformed", "EmbeddingBatchLedger", "EmbeddingBatcher",
]

# ---------------------------------------------------------------- 常量（任务书+主窗裁定）

MIN_BATCH_SIZE = 1
MAX_BATCH_SIZE = 10              # DashScope text-embedding-v3 端点硬顶（主窗裁定）
DEFAULT_BATCH_SIZE = 8           # 缺省批大小（1..10 窗内）
MIN_MAX_WAIT_S = 0.020           # 20ms 下沿（任务书窗保留）
MAX_MAX_WAIT_S = 0.050           # 50ms 上沿
DEFAULT_MAX_WAIT_S = 0.030       # 缺省 30ms

_POLL_S = 0.004                  # flusher 哨醒间隔（真时长；假钟测试靠它拾钟）
_CLOSE_TIMEOUT_S = 5.0


# ---------------------------------------------------------------- 异常 / 配置

class EmbeddingBatchError(RuntimeError):
    """严格变体聚合错：embed_documents_strict/embed 在任一项失败时抛出。

    .failures = {位次: 原始异常}——绝不吞错冒充成功（INV-1 同构）。
    """

    def __init__(self, message: str,
                 failures: dict[int, Exception]) -> None:
        super().__init__(message)
        self.failures = failures


class EmbeddingBatchMalformed(ValueError):
    """批量响应形态不符（长度不等/逐项非序列/数值不可转）——按批量失败
    走逐条回落，不冒充成功。"""


@dataclass(frozen=True)
class EmbeddingBatchConfig:
    """微批网关配置（构造校验 fail-closed）。

    batch_size：1..10（主窗裁定窗；16~32 被端点硬顶 10 取代）。
    max_wait_s：0.020~0.050（20~50ms 任务书窗保留）。
    """

    batch_size: int = DEFAULT_BATCH_SIZE
    max_wait_s: float = DEFAULT_MAX_WAIT_S

    def __post_init__(self) -> None:
        if type(self.batch_size) is not int or not (
                MIN_BATCH_SIZE <= self.batch_size <= MAX_BATCH_SIZE):
            raise ValueError(
                f"batch_size 必须为 int 且在 {MIN_BATCH_SIZE}.."
                f"{MAX_BATCH_SIZE}（端点硬顶，主窗裁定窗；16~32 作废）："
                f"{self.batch_size!r}")
        if (not isinstance(self.max_wait_s, (int, float))
                or isinstance(self.max_wait_s, bool)
                or not math.isfinite(self.max_wait_s)
                or not MIN_MAX_WAIT_S <= self.max_wait_s <= MAX_MAX_WAIT_S):
            raise ValueError(
                f"max_wait_s 必须为有限数且在 {MIN_MAX_WAIT_S}~"
                f"{MAX_MAX_WAIT_S}s（20~50ms 窗）：{self.max_wait_s!r}")


@dataclass(frozen=True)
class EmbeddingItemOutcome:
    """逐位结果：vector 与 error 互斥（位次=输入序）。"""

    vector: list[float] | None = None
    error: Exception | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


# ---------------------------------------------------------------- 台账

class EmbeddingBatchLedger:
    """线程安全微批台账：批计数（满批/超时）+ 失败回落 + 逐条计数。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.batches_sent = 0          # 成功发出的批数
        self.full_batches = 0          # 满批即发（batch_size 到量）
        self.deadline_batches = 0      # 超时即发（按现有量）
        self.malformed_batches = 0     # 响应形态不符（批/单条口径合计）
        self.batch_failures = 0        # 批级失败（进逐条回落）
        self.fallback_batches = 0      # 触发逐条回落的批数
        self.fallback_single_calls = 0  # 回落单发调用数
        self.item_failures = 0         # 回落后仍失败的条数

    def bump(self, **deltas: int) -> None:
        with self._lock:
            for key, delta in deltas.items():
                setattr(self, key, getattr(self, key) + delta)

    def snapshot(self) -> dict:
        with self._lock:
            return {key: getattr(self, key) for key in (
                "batches_sent", "full_batches", "deadline_batches",
                "malformed_batches", "batch_failures", "fallback_batches",
                "fallback_single_calls", "item_failures")}


# ---------------------------------------------------------------- 微批网关

class _PendingEntry:
    """单条待嵌条目（槽位回填单位；event 为该条完成信号）。"""

    __slots__ = ("text", "submitted_at", "event", "vector", "error")

    def __init__(self, text: str, submitted_at: float) -> None:
        self.text = text
        self.submitted_at = submitted_at
        self.event = threading.Event()
        self.vector: list[float] | None = None
        self.error: Exception | None = None


class EmbeddingBatcher:
    """Embedding 真批量微批网关（batch_call_fn 注入，传输层零感知）。

    模型：
    - 多调用方线程并发 embed_documents 同池共批（跨调用方合批）；
    - 专职 flusher（daemon，首提交懒启动）：满批即发 / 超时按现有量发
      （deadline 锚=最早未决条的 submitted_at，绝不为凑批硬等）；
      余量（切批后剩余）以剩余最早条重新起窗；
    - 批失败 → 该批逐条 [单文本] 回落单发（好条成活、坏条只死该条）；
    - 传输调用一律在锁外（长 IO 不持 cv）。
    """

    def __init__(self, batch_call_fn: Callable[[list[str]], Any], *,
                 config: EmbeddingBatchConfig | None = None,
                 clock: Callable[[], float] | None = None) -> None:
        if not callable(batch_call_fn):
            raise TypeError("batch_call_fn 必须可调用（传输层注入面）")
        self._batch_call_fn = batch_call_fn
        self._config = config or EmbeddingBatchConfig()
        self._clock = clock or time.monotonic
        self._cv = threading.Condition()
        self._pending: list[_PendingEntry] = []
        self._closed = False
        self._flusher: threading.Thread | None = None
        self.ledger = EmbeddingBatchLedger()

    # ---------- 配置只读面 ----------

    @property
    def batch_size(self) -> int:
        return self._config.batch_size

    @property
    def max_wait_s(self) -> float:
        return self._config.max_wait_s

    # ---------- 主入口 ----------

    def embed_documents(
            self, texts: Sequence[str]) -> list[EmbeddingItemOutcome]:
        """批量嵌入：逐位 outcome（位次=输入序；条级失败在 outcome.error，
        不冒充、不连坐其他条）。空输入/非 str → ValueError/TypeError。"""
        if not texts:
            raise ValueError("texts must be non-empty")
        for text in texts:
            if not isinstance(text, str):
                raise TypeError(f"text 必须为 str：{text!r}")
        entries = self._submit(texts)
        outcomes: list[EmbeddingItemOutcome] = []
        for entry in entries:
            entry.event.wait()
            outcomes.append(EmbeddingItemOutcome(
                vector=entry.vector, error=entry.error))
        return outcomes

    def embed_documents_strict(
            self, texts: Sequence[str]) -> list[list[float]]:
        """严格变体：任一条失败 → EmbeddingBatchError（.failures=位次→异常）；
        全成 → 按序向量列表。"""
        outcomes = self.embed_documents(texts)
        failures = {i: outcome.error for i, outcome in enumerate(outcomes)
                    if outcome.error is not None}
        if failures:
            raise EmbeddingBatchError(
                f"embedding batch 严格变体：{len(failures)}/{len(outcomes)} "
                f"项失败（位次→异常见 .failures）", failures)
        return [outcome.vector for outcome in outcomes]

    def embed(self, text: str) -> list[float]:
        """单条严格便捷口（经微批机制——孤条按 max_wait 截止单发）。"""
        if not isinstance(text, str):
            raise TypeError(f"text 必须为 str：{text!r}")
        return self.embed_documents_strict([text])[0]

    # ---------- 生命周期 ----------

    def close(self, timeout: float = _CLOSE_TIMEOUT_S) -> None:
        """停 flusher 并收尾（在判条目照发完再退；拒新提交）。"""
        with self._cv:
            self._closed = True
            self._cv.notify_all()
        flusher = self._flusher
        if flusher is not None and flusher.is_alive():
            flusher.join(timeout)

    def __enter__(self) -> "EmbeddingBatcher":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()

    # ---------- 内部：提交 / flusher ----------

    def _submit(self, texts: Sequence[str]) -> list[_PendingEntry]:
        with self._cv:
            if self._closed:
                raise RuntimeError("EmbeddingBatcher 已关闭（拒新提交）")
            now = self._clock()
            entries = [_PendingEntry(text, now) for text in texts]
            self._pending.extend(entries)
            if self._flusher is None:
                self._flusher = threading.Thread(
                    target=self._flush_loop, daemon=True,
                    name="p1b-emb-flusher")
                self._flusher.start()
            self._cv.notify_all()
        return entries

    def _flush_loop(self) -> None:
        while True:
            batch: list[_PendingEntry]
            reason: str
            with self._cv:
                while True:
                    if not self._pending:
                        if self._closed:
                            return
                        self._cv.wait()
                        continue
                    if len(self._pending) >= self._config.batch_size:
                        # 满批即发：到量立刻发，不等钟（假钟冻结也能发）。
                        batch = self._pending[:self._config.batch_size]
                        del self._pending[:self._config.batch_size]
                        reason = "full"
                        break
                    now = self._clock()
                    deadline = (min(e.submitted_at for e in self._pending)
                                + self._config.max_wait_s)
                    if now >= deadline:
                        # 超时即按现有量发：绝不为一批硬等（不凑批纪律）。
                        batch = list(self._pending)
                        self._pending.clear()
                        reason = "deadline"
                        break
                    self._cv.wait(timeout=min(deadline - now, _POLL_S))
            self._send_batch(batch, reason)

    def _send_batch(self, batch: list[_PendingEntry], reason: str) -> None:
        texts = [entry.text for entry in batch]
        try:
            vectors = self._checked_vectors(
                self._batch_call_fn(list(texts)), len(texts))
        except EmbeddingBatchMalformed:
            self.ledger.bump(batch_failures=1, malformed_batches=1)
            self._fallback(batch)
            return
        except Exception:
            self.ledger.bump(batch_failures=1)
            self._fallback(batch)
            return
        self.ledger.bump(batches_sent=1,
                         full_batches=1 if reason == "full" else 0,
                         deadline_batches=1 if reason == "deadline" else 0)
        for entry, vector in zip(batch, vectors):
            entry.vector = vector
            entry.event.set()

    def _fallback(self, batch: list[_PendingEntry]) -> None:
        """批量失败 → 逐条回落单发（一批坏一条不死全批）。"""
        self.ledger.bump(fallback_batches=1)
        for entry in batch:
            self.ledger.bump(fallback_single_calls=1)
            try:
                vectors = self._checked_vectors(
                    self._batch_call_fn([entry.text]), 1)
            except EmbeddingBatchMalformed as exc:
                self.ledger.bump(item_failures=1, malformed_batches=1)
                entry.error = exc
                entry.event.set()
                continue
            except Exception as exc:
                self.ledger.bump(item_failures=1)
                entry.error = exc
                entry.event.set()
                continue
            entry.vector = vectors[0]
            entry.event.set()

    @staticmethod
    def _checked_vectors(result: Any, count: int) -> list[list[float]]:
        """响应形态校验（家族口径）：len==count、逐项非空序列、数值可转。
        不符 → EmbeddingBatchMalformed（确认面不冒充成功）。"""
        if not isinstance(result, (list, tuple)) or len(result) != count:
            raise EmbeddingBatchMalformed(
                f"embedding 批响应长度不符：期望 {count}，实得 "
                f"{len(result) if isinstance(result, (list, tuple)) else type(result)!r}")
        vectors: list[list[float]] = []
        for item in result:
            if not isinstance(item, (list, tuple)) or not item:
                raise EmbeddingBatchMalformed(
                    f"embedding 批响应项非非空序列：{item!r}")
            try:
                vectors.append([float(x) for x in item])
            except (TypeError, ValueError) as exc:
                raise EmbeddingBatchMalformed(
                    f"embedding 批响应项数值不可转 float：{exc}") from exc
        return vectors
