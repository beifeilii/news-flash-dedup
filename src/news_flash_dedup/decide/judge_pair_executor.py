"""P1b 任务一（2026-10-11，施工窗 P1b：判官并发池+Embedding 批量，分支
infra/p1b-modules）：判官候选对并发执行器（纯执行器，零业务感知）。

任务书钉值（P1b 开发窗令）：
- 候选对并发池 pair_concurrency=20 / max_pair_concurrency=24（构造可配）；
- AB/BA 双序 = 每对 2 路 HTTP，总 HTTP 并发初始上限 40 **单独限流**
  （threading.Semaphore，与对池上限两把尺子分开执法）；
- **严禁把多个不同候选对拼进同一提示词**——本执行器结构上不可为：单对
  pair_fn 只收单个 JudgePairRequest，双序工厂 render(request, order) 也
  只见单个请求，提示词组装权全在注入方（render/call_fn），执行器不拼、
  不改、不看正文；
- 单对失败/超时独立处理不连坐池内其他对（异常分类进逐对结果，不上抛
  打断池）；
- 有界提交队列+背压：in-flight 窗口 = pair_concurrency 工人 +
  queue_capacity 等位槽；窗口满 → 该对**拒新**（status="rejected"、台账
  rejected 计数，不抛错、不连坐后续对）；
- 纯执行器：输入 = 判定请求迭代，输出 = 逐对结果/异常分类；不读 env、
  不感知任何业务开关（mv_mode/decision_mode/proof 等一概不知）。

家法同形（只读摸底报告在案）：
- 单对硬超时 = daemon 线程包裹 + join(timeout)（decide/judge_pair.py
  _call_with_timeout 同形）：超时结果丢弃、线程 daemon 不拖进程；
  超时释的是"等待槽位"（对窗口回收，可接新对），殭尸内层线程如仍在
  跑最终会走 HTTP 信号量收口——HTTP 并发真上限恒由信号量执法，不受
  超时回收影响；
- 异常摘要截断 300 字（llm_residual error=str(exc)[:300] 同款）。

未来接线点（本窗零接线，P0 完成后主窗统一 rebase 时装配）：
- pair_fn := judge_pair_executor.dual_order_pair_fn 产物——call_fn 接
  llm_residual.SyncResidualJudge 的 call_fn 注入面（llm_residual.py 构造
  参数，签名 model/system/user/timeout_s → (content, latency)），
  render 按业务拼单对单序提示词（组装权留在业务侧）；
- 消费点 = decide/service.py adjudicate_pair 逐对调用处（判定链内
  串行 → 并发池化包此处）；
- 每对结果 → 业务侧映射 ResidualOrderOutcome/合同 proof（本模块只给
  status/value/error，语义归业务）。

单测（tests/unit/test_p1b_t1_judge_pair_executor.py，全 mock 传输层）：
并发上限钉 / HTTP 独立限流钉 / 失败与超时隔离钉 / 背压拒新钉 / 顺序
对应钉 / 双序 2 路 HTTP 与禁拼对钉 / 构造校验钉。
"""

from __future__ import annotations

import queue
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping

__all__ = [
    "DEFAULT_PAIR_CONCURRENCY", "DEFAULT_MAX_PAIR_CONCURRENCY",
    "DEFAULT_HTTP_CONCURRENCY", "STATUS_OK", "STATUS_TIMEOUT",
    "STATUS_ERROR", "STATUS_REJECTED", "ORDERS", "JudgePairRequest",
    "OrderCallOutcome", "DualOrderOutcome", "JudgePairResult",
    "ExecutorLedger", "JudgePairExecutor",
]

# ---------------------------------------------------------------- 常量（任务书钉值）

DEFAULT_PAIR_CONCURRENCY = 20       # 候选对并发池初始值
DEFAULT_MAX_PAIR_CONCURRENCY = 24   # 对池上限（pair_concurrency 不得越此值）
DEFAULT_HTTP_CONCURRENCY = 40       # 总 HTTP 并发初始上限（独立信号量执法）

STATUS_OK = "ok"                    # 单对判定成功（value=pair_fn 返回值）
STATUS_TIMEOUT = "timeout"          # 单对硬超时（结果丢弃，不连坐）
STATUS_ERROR = "error"              # 单对异常（摘要 300 字，不连坐）
STATUS_REJECTED = "rejected"        # 背压拒新（窗口满，未进池）

ORDERS = ("ab", "ba")               # 双序词表（业务中立；hc/ch 映射归接线方）

_ERROR_SNIPPET_MAX = 300            # 家法同款异常摘要截断


# ---------------------------------------------------------------- 请求 / 结果形态

@dataclass(frozen=True)
class JudgePairRequest:
    """单对判定请求（执行器只认对身份与双侧正文，不感知任何开关）。

    pair_id 是结果对位键（execute 输出序 = 输入序，逐对 pair_id 回显）；
    text_history/text_current 对执行器是不透明正文（拼提示词的权在
    注入方 render/call_fn，本模块不拼不改不看）。
    """

    pair_id: str
    text_history: str
    text_current: str

    def __post_init__(self) -> None:
        if not isinstance(self.pair_id, str) or not self.pair_id:
            raise ValueError("pair_id 必须为非空 str（结果对位键）")


@dataclass(frozen=True)
class OrderCallOutcome:
    """双序工厂单序调用结果：value 与 error 互斥（error 为原始异常实例，
    语义分类归业务接线方——本层不吞不改）。"""

    value: Any = None
    error: Exception | None = None


@dataclass(frozen=True)
class DualOrderOutcome:
    """单对双序产物（缺省 combine 形态）：ab/ba 各一份 OrderCallOutcome。"""

    ab: OrderCallOutcome
    ba: OrderCallOutcome


@dataclass(frozen=True)
class JudgePairResult:
    """逐对结果（异常分类内嵌，不上抛）：status ∈ ok/timeout/error/rejected。

    value 仅 status="ok" 时为 pair_fn 返回值；error 为摘要文本（≤300 字，
    timeout/rejected 各有稳定前缀文案）；latency_s 为单对墙钟（3 位小数）。
    """

    pair_id: str
    status: str
    value: Any = None
    error: str | None = None
    latency_s: float = 0.0


# ---------------------------------------------------------------- 台账

class ExecutorLedger:
    """线程安全执行器台账：提交/拒新/完成/超时/异常计数 + 并发峰值观测。

    pairs_peak/http_peak 是并发钉测的观测面（单测全 mock 传输层直读钉值）。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.submitted = 0        # 已受理进窗口
        self.rejected = 0         # 背压拒新
        self.completed = 0        # status=ok
        self.timeouts = 0         # status=timeout
        self.errors = 0           # status=error
        self.pairs_peak = 0       # 同时在判对数峰值（工人池实际上限观测）
        self.http_peak = 0        # 同时在途 HTTP 峰值（信号量执法观测）
        self._pairs_active = 0
        self._http_active = 0

    def bump(self, *, submitted: int = 0, rejected: int = 0,
             completed: int = 0, timeouts: int = 0, errors: int = 0) -> None:
        with self._lock:
            self.submitted += submitted
            self.rejected += rejected
            self.completed += completed
            self.timeouts += timeouts
            self.errors += errors

    def _pair_enter(self) -> None:
        with self._lock:
            self._pairs_active += 1
            if self._pairs_active > self.pairs_peak:
                self.pairs_peak = self._pairs_active

    def _pair_exit(self) -> None:
        with self._lock:
            self._pairs_active -= 1

    def _http_enter(self) -> None:
        with self._lock:
            self._http_active += 1
            if self._http_active > self.http_peak:
                self.http_peak = self._http_active

    def _http_exit(self) -> None:
        with self._lock:
            self._http_active -= 1

    def snapshot(self) -> dict:
        with self._lock:
            return {"submitted": self.submitted, "rejected": self.rejected,
                    "completed": self.completed, "timeouts": self.timeouts,
                    "errors": self.errors, "pairs_peak": self.pairs_peak,
                    "http_peak": self.http_peak}


# ---------------------------------------------------------------- 执行器

class JudgePairExecutor:
    """判官候选对并发执行器（纯执行器；pair_fn 注入，业务零感知）。

    并发模型：
    - execute() 每次调用起 pair_concurrency 个工人线程消费内部队列（哨兵
      退出，全部 join 后返回——结果序恒等于输入序）；
    - in-flight 窗口 = pair_concurrency + queue_capacity（queue_capacity
      =None 时窗口无界）；窗口满 → 拒新（不抛错，逐对结果 status=
      "rejected" + 台账计数）；
    - HTTP 并发由独立信号量执法（http_call/http_window），与对池两把尺
      子互不挤占。

    纪律：
    - execute() 不可并发复入（每实例串行；复入 → RuntimeError）；
    - 单对异常/超时只进该对结果，绝不打断池内其他对；
    - 单对超时用 daemon 包裹线程：等待槽位即时回收（可接新对），殭尸线
      程的 HTTP 仍受信号量收口。
    """

    def __init__(self, pair_fn: Callable[[JudgePairRequest], Any], *,
                 pair_concurrency: int = DEFAULT_PAIR_CONCURRENCY,
                 max_pair_concurrency: int = DEFAULT_MAX_PAIR_CONCURRENCY,
                 http_concurrency: int = DEFAULT_HTTP_CONCURRENCY,
                 pair_timeout_s: float | None = None,
                 queue_capacity: int | None = None) -> None:
        if not callable(pair_fn):
            raise TypeError("pair_fn 必须可调用（纯执行器注入面）")
        if type(pair_concurrency) is not int or pair_concurrency < 1:
            raise ValueError(
                f"pair_concurrency 必须为正 int：{pair_concurrency!r}")
        if type(max_pair_concurrency) is not int or max_pair_concurrency < 1:
            raise ValueError(
                f"max_pair_concurrency 必须为正 int：{max_pair_concurrency!r}")
        if pair_concurrency > max_pair_concurrency:
            raise ValueError(
                f"pair_concurrency={pair_concurrency} 越上限 "
                f"max_pair_concurrency={max_pair_concurrency}")
        if type(http_concurrency) is not int or http_concurrency < 1:
            raise ValueError(
                f"http_concurrency 必须为正 int：{http_concurrency!r}")
        if pair_timeout_s is not None and (
                not isinstance(pair_timeout_s, (int, float))
                or not pair_timeout_s > 0):
            raise ValueError(
                f"pair_timeout_s 须为正数或 None：{pair_timeout_s!r}")
        if queue_capacity is not None and (
                type(queue_capacity) is not int or queue_capacity < 0):
            raise ValueError(
                f"queue_capacity 须为非负 int 或 None：{queue_capacity!r}")
        self._pair_fn = pair_fn
        self._pair_concurrency = pair_concurrency
        self._max_pair_concurrency = max_pair_concurrency
        self._http_concurrency = http_concurrency
        self._pair_timeout_s = (float(pair_timeout_s)
                                if pair_timeout_s is not None else None)
        self._window: int | None = (
            None if queue_capacity is None
            else pair_concurrency + queue_capacity)
        self._http_semaphore = threading.Semaphore(http_concurrency)
        self._ledger = ExecutorLedger()
        self._state_lock = threading.Lock()
        self._inflight = 0
        self._executing = False

    # ---------- 只读面 ----------

    @property
    def pair_concurrency(self) -> int:
        return self._pair_concurrency

    @property
    def max_pair_concurrency(self) -> int:
        return self._max_pair_concurrency

    @property
    def http_concurrency(self) -> int:
        return self._http_concurrency

    @property
    def window(self) -> int | None:
        """in-flight 窗口（对池工人 + 等位槽）；None=无界。"""
        return self._window

    @property
    def ledger(self) -> ExecutorLedger:
        return self._ledger

    # ---------- HTTP 独立限流（信号量执法；两把尺子分开） ----------

    def http_call(self, fn: Callable[..., Any], *args: Any,
                  **kwargs: Any) -> Any:
        """在执行器 HTTP 信号量窗口内调用 fn（总并发独立于对池上限）。"""
        with self._http_semaphore:
            self._ledger._http_enter()
            try:
                return fn(*args, **kwargs)
            finally:
                self._ledger._http_exit()

    @contextmanager
    def http_window(self):
        """HTTP 信号量窗口上下文（enter 观测峰值，exit 必释）。"""
        with self._http_semaphore:
            self._ledger._http_enter()
            try:
                yield
            finally:
                self._ledger._http_exit()

    # ---------- 双序 pair_fn 工厂（每对 2 路 HTTP；禁拼对的结构保证） ----------

    def dual_order_pair_fn(
            self, call_fn: Callable[..., Any], *,
            render: Callable[[JudgePairRequest, str], Mapping[str, Any]],
            combine: Callable[[OrderCallOutcome, OrderCallOutcome],
                              Any] | None = None,
    ) -> Callable[[JudgePairRequest], Any]:
        """构造双序 pair_fn：单对 AB/BA 各一路 HTTP（均经本执行器
        http_call 信号量）。

        **配套用法**（HTTP 闸门与工人池同实例共享，勿跨实例）：
        ``pair_fn = executor.dual_order_pair_fn(...)`` 后以
        ``executor.execute(requests, pair_fn=pair_fn)`` 覆盖注入执行——
        工厂先于执行器构造的鸡生蛋问题由 execute 的每调用 pair_fn 覆盖
        参数解决（勿用第二个执行器实例跑本工厂产物：那会把 HTTP 限流
        留在宿主实例上，两把尺子分家）。

        - render(request, order) → call_fn 关键字参数（order ∈ "ab"/"ba"）：
          提示词组装权全在业务注入方——render 只见**单个**请求，执行器
          结构上不可能把多个候选对拼进同一提示词；
        - call_fn(**kwargs) → 任意返回值（透传，不改不看）；
        - 每序异常互不连坐：OrderCallOutcome(value=…, error=原始异常)；
        - combine 缺省 → DualOrderOutcome(ab, ba)；自定义 combine 收两序
          结果装配业务值（映射 ResidualOrderOutcome 等归接线方）。
        """
        if not callable(call_fn):
            raise TypeError("call_fn 必须可调用（HTTP 传输层注入面）")
        if not callable(render):
            raise TypeError("render 必须可调用（单对单序提示词组装面）")
        if combine is not None and not callable(combine):
            raise TypeError("combine 必须可调用或缺省 None")

        def pair_fn(request: JudgePairRequest) -> Any:
            outcomes: dict[str, OrderCallOutcome] = {}

            def _one(order: str) -> None:
                kwargs = dict(render(request, order))
                try:
                    outcomes[order] = OrderCallOutcome(
                        value=self.http_call(call_fn, **kwargs))
                except Exception as exc:     # 单序失败不连坐另一序
                    outcomes[order] = OrderCallOutcome(error=exc)

            threads = [
                threading.Thread(
                    target=_one, args=(order,), daemon=True,
                    name=f"p1b-order-{request.pair_id}-{order}")
                for order in ORDERS]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            if combine is not None:
                return combine(outcomes["ab"], outcomes["ba"])
            return DualOrderOutcome(ab=outcomes["ab"], ba=outcomes["ba"])

        return pair_fn

    # ---------- 主入口（输入=判定请求迭代；输出=逐对结果，序=输入序） ----------

    def execute(
            self, requests: Iterable[JudgePairRequest], *,
            pair_fn: Callable[[JudgePairRequest], Any] | None = None,
    ) -> list[JudgePairResult]:
        """并发执行候选对判定；返回逐对结果（异常分类内嵌，不上抛）。

        pair_fn：每调用覆盖注入（缺省=构造注入件）。配套双序工厂用法
        ``executor.execute(reqs, pair_fn=executor.dual_order_pair_fn(...))``
        ——同一实例持有 HTTP 信号量与台账，两把尺子同家执法。

        背压：窗口满的对立即 status="rejected"（台账计数，不抛错、不连坐
        后续对）。空输入 → []。本方法不可并发复入（复入 → RuntimeError）。
        """
        with self._state_lock:
            if self._executing:
                raise RuntimeError(
                    "JudgePairExecutor.execute 不可并发复入（每实例串行调用）")
            self._executing = True
        try:
            effective_fn = pair_fn if pair_fn is not None else self._pair_fn
            items = list(requests)
            results: list[JudgePairResult | None] = [None] * len(items)
            work: "queue.Queue[tuple[int, JudgePairRequest] | None]" = (
                queue.Queue())
            workers = [
                threading.Thread(
                    target=self._worker, args=(work, results, effective_fn),
                    daemon=True, name=f"p1b-judge-worker-{n}")
                for n in range(self._pair_concurrency)]
            for worker in workers:
                worker.start()
            try:
                for index, request in enumerate(items):
                    if not self._try_submit():
                        self._ledger.bump(rejected=1)
                        results[index] = JudgePairResult(
                            pair_id=request.pair_id, status=STATUS_REJECTED,
                            error=(f"并发窗口已满（in-flight 窗口="
                                   f"{self._window}），本对拒新"))
                        continue
                    work.put((index, request))
            finally:
                for _ in workers:
                    work.put(None)            # 哨兵：工人各退一枚
                for worker in workers:
                    worker.join()
            return [
                result if result is not None else
                JudgePairResult(
                    pair_id=items[i].pair_id, status=STATUS_ERROR,
                    error="执行器工人异常终止（结果槽位未回填，防御性单列）")
                for i, result in enumerate(results)]
        finally:
            with self._state_lock:
                self._executing = False

    # ---------- 内部 ----------

    def _try_submit(self) -> bool:
        """窗口受理（无界窗口恒受理）；受理即占一个 in-flight 槽。"""
        with self._state_lock:
            if (self._window is not None
                    and self._inflight >= self._window):
                return False
            self._inflight += 1
            self._ledger.bump(submitted=1)
            return True

    def _release_slot(self) -> None:
        with self._state_lock:
            self._inflight -= 1

    def _worker(self, work, results: list,
                pair_fn: Callable[[JudgePairRequest], Any]) -> None:
        while True:
            item = work.get()
            if item is None:
                return
            index, request = item
            results[index] = self._run_pair(request, pair_fn)
            self._release_slot()

    def _run_pair(self, request: JudgePairRequest,
                  pair_fn: Callable[[JudgePairRequest], Any]
                  ) -> JudgePairResult:
        self._ledger._pair_enter()
        try:
            return self._judge_with_timeout(request, pair_fn)
        finally:
            self._ledger._pair_exit()

    def _judge_with_timeout(
            self, request: JudgePairRequest,
            pair_fn: Callable[[JudgePairRequest], Any]
    ) -> JudgePairResult:
        t0 = time.perf_counter()
        if self._pair_timeout_s is None:
            try:
                value = pair_fn(request)
            except Exception as exc:          # 单对异常不连坐
                self._ledger.bump(errors=1)
                return JudgePairResult(
                    pair_id=request.pair_id, status=STATUS_ERROR,
                    error=str(exc)[:_ERROR_SNIPPET_MAX],
                    latency_s=round(time.perf_counter() - t0, 3))
            self._ledger.bump(completed=1)
            return JudgePairResult(
                pair_id=request.pair_id, status=STATUS_OK, value=value,
                latency_s=round(time.perf_counter() - t0, 3))
        box: dict = {}

        def _runner() -> None:
            try:
                box["value"] = pair_fn(request)
            except Exception as exc:          # noqa: BLE001 — 收容进 box
                box["error"] = exc

        runner = threading.Thread(
            target=_runner, daemon=True,
            name=f"p1b-pair-{request.pair_id}")
        runner.start()
        runner.join(self._pair_timeout_s)
        latency = round(time.perf_counter() - t0, 3)
        if runner.is_alive():
            # 硬超时（家法 _call_with_timeout 同形）：结果丢弃、线程 daemon
            # 不拖进程；等待槽位由工人释放（可接新对），殭尸线程的 HTTP 仍
            # 受信号量收口。
            self._ledger.bump(timeouts=1)
            return JudgePairResult(
                pair_id=request.pair_id, status=STATUS_TIMEOUT,
                error=(f"单对判定超时（>{self._pair_timeout_s}s，"
                       f"结果丢弃不连坐）"),
                latency_s=latency)
        if "error" in box:
            self._ledger.bump(errors=1)
            return JudgePairResult(
                pair_id=request.pair_id, status=STATUS_ERROR,
                error=str(box["error"])[:_ERROR_SNIPPET_MAX],
                latency_s=latency)
        self._ledger.bump(completed=1)
        return JudgePairResult(
            pair_id=request.pair_id, status=STATUS_OK, value=box.get("value"),
            latency_s=latency)
