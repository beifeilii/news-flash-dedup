# -*- coding: utf-8 -*-
"""P1c 运行时装配接线包（2026-10-11，施工窗 P1c：运行时装配，分支
infra/p1c-runtime-wiring，基线=主线 238fa49）。

定位：把 P1b 两模块（991be05 合并入主线，入库未接线=休眠件）接进运行时
装配层——**接线但默认不启用**（两装配开关缺席=OFF=影子态，与用户口径
「判官在链=点头 B 阻塞」一致）：

- **判官并发执行器**（decide/judge_pair_executor.py，P1b-T1）：本包提供
  装配工厂（call_fn/render 由最终接线窗注入——判定语义面禁碰，提示词
  组装权按 P1b 合同留业务侧）；消费点=decide/service.py adjudicate_pair
  逐对循环（judge_pair_executor.py:33-34 接线合同），实际启用归点头 B
  后的最终接线窗，本包不碰 decide 面；
- **Embedding 微批网关**（vector/embedding_batch.py，P1b-T2）：向量写
  路径（vector/pipeline.py:94 RealVectorPipeline.ingest_record 的
  client.embed_documents）前置跨记录合批——经 BatchedEmbeddingClient
  鸭型包装 client 实现，vector/pipeline.py **零改动**（写径合同=嵌入
  三态/保序/upsert/CAS 全继承，包装面只前置合批层）。

**落地形态（为何独立模块而非 create_host 内联）**：现役组合根=api/
host.py create_host（B4 组装根）；但其消费面均不在本窗接线权内——
判官装配链落 decide 判定语义面+recall/worker.py（禁地），向量写路径
不在 create_host 装配面（写径组装位=影子 harness/backfill 注入，src
内无生产构造点）。故 P1c 接线以本模块落地（装配层新增件），create_host
零改动；任何装配根（emb harness/回填/未来 P0-rebase 前沿窗/点头 B 后
的判官接线窗）以 env 开关+工厂函数取用：

    from news_flash_dedup.api.assemble import (
        wire_embedding_micro_batch, wire_judge_pair_executor)
    client2 = wire_embedding_micro_batch(client, env)        # OFF→原样
    wired = wire_judge_pair_executor(env, call_fn=..., render=...)
    # wired is None（OFF 缺省）；ON→WiredJudgePairExecutor.execute(reqs)

开关族（**默认全 OFF**；lib/run_manifest.py KNOWN_SWITCHES 已同步登记
——「新增开关须显式登记入本表」规矩照办）：

- DEDUP_JUDGE_PAIR_EXECUTOR：判官并发执行器装配开关（"1"/"true"/
  "yes"/"on" 开，judge_pair.py:67 _SWITCH_ON_VALUES 同款；缺席/其余
  一律关）；
- DEDUP_JUDGE_PAIR_CONCURRENCY：对池大小（int，窗 1..24=模块钉值
  DEFAULT_MAX_PAIR_CONCURRENCY，缺省 20=DEFAULT_PAIR_CONCURRENCY；
  越窗 ValueError fail-closed）；
- DEDUP_JUDGE_PAIR_TIMEOUT_S：单对硬超时秒（正浮点；缺席=None=模块
  缺省不包裹——注意与判官单顺序调用超时 judge_pair.py:196
  DEFAULT_JUDGE_TIMEOUT_S=120.0 分立：本超时释的是**对级等待槽位**，
  见 judge_pair_executor.py:22-26 家法注记）；
- DEDUP_EMBEDDING_MICRO_BATCH：嵌入写径微批网关开关（同款真值集，
  缺省关）；
- DEDUP_EMBEDDING_BATCH_SIZE：微批批大小（int，窗 1..10=DashScope
  端点硬顶（主窗裁定），缺省 8；越窗 ValueError）；
- DEDUP_EMBEDDING_BATCH_MAX_WAIT_MS：微批窗口毫秒（float，窗
  20..50（任务书窗保留），缺省 30；越窗/非数值 ValueError）。

判官执行器接线合同（judge_pair_executor.py:28-36/:307-312 原文）：
- call_fn：判官传输面（llm_residual.py:615-620 SyncResidualJudge 构造
  参数 call_fn，签名 model/system/user/timeout_s → (content, latency)）
  ——注入方保证签名与预算/缓存纪律，本包只校验可调用；
- render(request, order)：单对单序提示词组装 kwargs（order ∈
  "ab"/"ba"）——**严禁拼对**（执行器结构保证：render 只见单个请求）；
- pair_fn = executor.dual_order_pair_fn(...) **同实例纪律**：HTTP 信号量
  与工人池同家执法（勿跨实例——两把尺子分家即失限流）；鸡生蛋问题由
  execute 的每调用 pair_fn 覆盖参数解决，WiredJudgePairExecutor.execute
  是该合同的结构化封装（构造件=哨兵占位，误用即 RuntimeError）；
- 每对结果 → 业务侧映射 ResidualOrderOutcome/合同 proof（语义归业务
  接线方，本包零触碰）。

Embedding 微批接线合同（embedding_batch.py:28-34 原文+本包落地）：
- batch_call_fn = 被包装 client.embed_documents **原方法**（磁盘缓存/
  日 token 预算闸/usage 台账/6000 字截断护栏全保留在 client 内——P1b
  纪律「现有 EmbeddingClient 行为零改动」的接线侧对偶：本包不改
  client 一行，只前置合批层；批大小 ≤10 时内层再分批恒 ≤10，端点硬顶
  不破）；
- 三态失败映射（embedding_client.py §2.5 家族口径的包装侧执法）：批内
  任一条 VectorWriteUnknown → 原样传播（确认未知——pipeline.py:100-103
  不翻状态口径）；否则首错原样传播（EmbeddingApiError → pipeline.py:
  96-99 翻 failed 口径）；**绝不吞错冒充成功**（INV-1 同构）；
- 批级失败→逐条回落单发（一批坏一条不死全批，embedding_batch.py:
  329-348）由网关自携，包装层只做终态映射；
- 读径 embed_query / usage_snapshot / dimension / max_input_chars 直通
  原 client（微批=写径专属，查询径零触碰）；未代理面缺省 AttributeError
  即报（fail-closed：消费方要新面须显式接线，不静默绕过微批层）。

零成本测试：tests/unit/test_p1c_runtime_wiring.py（全 Fake——判官传输
罐装函数+嵌入客户端罐装计数器+FakeP19Store 同 test_p19_real_pipeline
形态；零真 LLM/ES/Milvus）。
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, Sequence

from news_flash_dedup.decide.judge_pair_executor import (
    DEFAULT_MAX_PAIR_CONCURRENCY,
    DEFAULT_PAIR_CONCURRENCY,
    JudgePairExecutor,
    JudgePairRequest,
)
from news_flash_dedup.recall.vector_store import VectorWriteUnknown
from news_flash_dedup.vector.embedding_batch import (
    DEFAULT_BATCH_SIZE,
    DEFAULT_MAX_WAIT_S,
    MAX_BATCH_SIZE,
    MAX_MAX_WAIT_S,
    MIN_BATCH_SIZE,
    MIN_MAX_WAIT_S,
    EmbeddingBatchConfig,
    EmbeddingBatcher,
)

# ---------------------------------------------------------------- 开关族（默认全 OFF）

#: 判官并发执行器装配开关（点头 B 口径：接线但默认不启用）。
JUDGE_PAIR_EXECUTOR_ENV = "DEDUP_JUDGE_PAIR_EXECUTOR"
#: 对池大小（窗 1..24，缺省 20——judge_pair_executor.py 钉值）。
JUDGE_PAIR_CONCURRENCY_ENV = "DEDUP_JUDGE_PAIR_CONCURRENCY"
#: 单对硬超时秒（缺席=None=不包裹；judge_pair_executor.py:22-26 释槽位语义）。
JUDGE_PAIR_TIMEOUT_ENV = "DEDUP_JUDGE_PAIR_TIMEOUT_S"

#: Embedding 写径微批网关开关（向量写路径接入，默认 OFF）。
EMBEDDING_MICRO_BATCH_ENV = "DEDUP_EMBEDDING_MICRO_BATCH"
#: 微批批大小（窗 1..10=DashScope 端点硬顶，缺省 8——embedding_batch.py 钉值）。
EMBEDDING_BATCH_SIZE_ENV = "DEDUP_EMBEDDING_BATCH_SIZE"
#: 微批窗口毫秒（窗 20..50，缺省 30——embedding_batch.py 钉值；模块单位=秒）。
EMBEDDING_BATCH_MAX_WAIT_MS_ENV = "DEDUP_EMBEDDING_BATCH_MAX_WAIT_MS"

#: 开关真值集（judge_pair.py:67 同款；缺席/空串/其余一律关）。
_SWITCH_ON_VALUES = frozenset({"1", "true", "yes", "on"})


def _env_source(env: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if env is None else env


def _switch_from_env(source: Mapping[str, str], name: str) -> bool:
    """开关解析：strip+lower 后 ∈ 真值集才开（fail-closed 默认关）。"""
    return source.get(name, "").strip().lower() in _SWITCH_ON_VALUES


# ---------------------------------------------------------------- 判官执行器 spec

@dataclass(frozen=True)
class JudgeExecutorSpec:
    """判官并发执行器装配 spec（env 单源解析产物；构造校验 fail-closed）。

    enabled=False（缺省）=不装配（wire 返 None——OFF 态执行器不存在，
    不是"存在但没人用"的僵尸件）；pair_concurrency 窗 1..24 与
    pair_timeout_s 正数窗在 spec 层先执法（执行器构造器再校验=纵深防御）。
    """

    enabled: bool = False
    pair_concurrency: int = DEFAULT_PAIR_CONCURRENCY
    pair_timeout_s: float | None = None

    def __post_init__(self) -> None:
        if type(self.pair_concurrency) is not int or not (
                1 <= self.pair_concurrency <= DEFAULT_MAX_PAIR_CONCURRENCY):
            raise ValueError(
                f"pair_concurrency 必须为 int 且在 1.."
                f"{DEFAULT_MAX_PAIR_CONCURRENCY}（模块钉值窗；env "
                f"{JUDGE_PAIR_CONCURRENCY_ENV}）：{self.pair_concurrency!r}")
        if self.pair_timeout_s is not None and (
                not isinstance(self.pair_timeout_s, (int, float))
                or isinstance(self.pair_timeout_s, bool)
                or not math.isfinite(self.pair_timeout_s)
                or not self.pair_timeout_s > 0):
            raise ValueError(
                f"pair_timeout_s 须为正有限数或 None（env "
                f"{JUDGE_PAIR_TIMEOUT_ENV} 缺席=None）："
                f"{self.pair_timeout_s!r}")


def judge_executor_spec_from_env(
        env: Mapping[str, str] | None = None) -> JudgeExecutorSpec:
    """解析判官执行器装配 spec（缺席=全缺省=OFF；非法值 ValueError）。

    解析面：开关 JUDGE_PAIR_EXECUTOR_ENV + 对池 JUDGE_PAIR_CONCURRENCY_
    ENV（int 窗 1..24）+ 超时 JUDGE_PAIR_TIMEOUT_ENV（正浮点秒，缺席
    =None）；越窗/非数值 fail-closed（fact_supply.py fact_timeout_from_
    env 同型纪律——非法值宁可拒装不可静默选边）。
    """
    source = _env_source(env)
    raw_concurrency = (source.get(JUDGE_PAIR_CONCURRENCY_ENV) or "").strip()
    pair_concurrency = DEFAULT_PAIR_CONCURRENCY
    if raw_concurrency:
        try:
            pair_concurrency = int(raw_concurrency)
        except ValueError as error:
            raise ValueError(
                f"{JUDGE_PAIR_CONCURRENCY_ENV}={raw_concurrency!r} 非整数"
                f"（合法窗 1..{DEFAULT_MAX_PAIR_CONCURRENCY}）") from error
    raw_timeout = (source.get(JUDGE_PAIR_TIMEOUT_ENV) or "").strip()
    pair_timeout_s: float | None = None
    if raw_timeout:
        try:
            pair_timeout_s = float(raw_timeout)
        except ValueError as error:
            raise ValueError(
                f"{JUDGE_PAIR_TIMEOUT_ENV}={raw_timeout!r} 非数值"
                f"（须正浮点秒；缺席=None=不包裹单对超时）") from error
    return JudgeExecutorSpec(
        enabled=_switch_from_env(source, JUDGE_PAIR_EXECUTOR_ENV),
        pair_concurrency=pair_concurrency,
        pair_timeout_s=pair_timeout_s)


# ---------------------------------------------------------------- 判官执行器装配工厂

def _unwired_pair_fn(request: JudgePairRequest) -> Any:
    """构造件哨兵占位（judge_pair_executor.py:307-312 鸡生蛋合同）。

    执行器构造要求 pair_fn，而双序工厂是执行器实例方法——占位件恒不可
    达：真实 pair_fn 经 WiredJudgePairExecutor.execute → execute(requests,
    pair_fn=…) 覆盖注入。本占位被调用=接线误用（fail-closed 响亮报错，
    不静默半接线）。
    """
    raise RuntimeError(
        "P1c 装配纪律：判官执行器构造件占位被调用（误用）——双序 pair_fn "
        "须经 WiredJudgePairExecutor.execute(requests) 覆盖注入"
        "（judge_pair_executor.py:307-312 配套用法合同：同实例持 HTTP "
        "信号量与台账，勿用裸执行器跑工厂产物）")


class WiredJudgePairExecutor:
    """已接线判官并发执行器（executor + 双序 pair_fn 同实例捆扎面）。

    execute(requests) 恒以捆扎 pair_fn 覆盖注入（P1b 配套用法合同的结构
    化封装——消费方无法误用裸构造 pair_fn 或跨实例失限流）；ledger/
    executor 只读外露（台账观测/深度诊断用）。
    """

    __slots__ = ("spec", "executor", "pair_fn")

    def __init__(self, spec: JudgeExecutorSpec,
                 executor: JudgePairExecutor,
                 pair_fn: Callable[[JudgePairRequest], Any]) -> None:
        self.spec = spec
        self.executor = executor
        self.pair_fn = pair_fn

    def execute(self, requests: Iterable[JudgePairRequest]):
        """并发执行候选对判定（逐对结果序=输入序；详见执行器合同）。"""
        return self.executor.execute(requests, pair_fn=self.pair_fn)

    @property
    def ledger(self):
        """执行器台账只读面（submitted/rejected/…/http_peak）。"""
        return self.executor.ledger


def build_judge_pair_executor(
        spec: JudgeExecutorSpec, *,
        call_fn: Callable[..., Any],
        render: Callable[[JudgePairRequest, str], Mapping[str, Any]],
        combine: Callable[..., Any] | None = None,
) -> WiredJudgePairExecutor:
    """按 spec 装配判官并发执行器+双序 pair_fn（P1b 接线合同落地）。

    - call_fn：判官传输面（SyncResidualJudge 构造参数合同——签名
      model/system/user/timeout_s → (content, latency)；注入方保证）；
    - render(request, order)：单对单序提示词组装（**组装权留业务侧**，
      执行器结构上不可能拼对——judge_pair_executor.py:308-316）；
    - combine：缺省 None → DualOrderOutcome(ab, ba)；自定义 combine 收
      两序结果装配业务值（映射 ResidualOrderOutcome 等归最终接线窗）；
    - spec.enabled 必须 True（OFF spec 构造=半接线僵尸，ValueError
      fail-closed）；执行器钉值（max 24/HTTP 40）沿用模块缺省，spec 只
      开窗 pair_concurrency/pair_timeout_s。
    """
    if not spec.enabled:
        raise ValueError(
            "JudgeExecutorSpec.enabled=False——OFF 态不装配（开关 "
            f"{JUDGE_PAIR_EXECUTOR_ENV} 缺省关；半接线僵尸即拒）")
    if not callable(call_fn):
        raise TypeError("call_fn 必须可调用（判官传输面注入合同）")
    if not callable(render):
        raise TypeError("render 必须可调用（单对单序提示词组装面）")
    if combine is not None and not callable(combine):
        raise TypeError("combine 必须可调用或缺省 None")
    executor = JudgePairExecutor(
        _unwired_pair_fn,
        pair_concurrency=spec.pair_concurrency,
        pair_timeout_s=spec.pair_timeout_s)
    pair_fn = executor.dual_order_pair_fn(
        call_fn, render=render, combine=combine)
    return WiredJudgePairExecutor(spec, executor, pair_fn)


def wire_judge_pair_executor(
        env: Mapping[str, str] | None = None, *,
        call_fn: Callable[..., Any],
        render: Callable[[JudgePairRequest, str], Mapping[str, Any]],
        combine: Callable[..., Any] | None = None,
) -> WiredJudgePairExecutor | None:
    """env 驱动装配入口：开关 OFF（缺省）→ None（不装配）；ON → 捆扎件。

    判官在链=点头 B 阻塞口径的装配侧落点：本函数缺席 env 恒 None——
    判定链行为与基线逐字节（decide 面零感知）；点头 B 后最终接线窗以
    ON env + call_fn/render（判官语义面注入件）取用本入口。
    """
    spec = judge_executor_spec_from_env(env)
    if not spec.enabled:
        return None
    return build_judge_pair_executor(
        spec, call_fn=call_fn, render=render, combine=combine)


# ---------------------------------------------------------------- Embedding 微批 spec

@dataclass(frozen=True)
class EmbeddingMicroBatchSpec:
    """Embedding 写径微批网关装配 spec（env 单源解析产物；窗=模块钉值窗）。

    enabled=False（缺省）=不接线（wire 返原 client——写径逐字节现役）；
    batch_size 窗 1..10 / max_wait_s 窗 0.020~0.050 与
    EmbeddingBatchConfig 构造校验同窗（spec 层先执法=纵深防御）。
    """

    enabled: bool = False
    batch_size: int = DEFAULT_BATCH_SIZE
    max_wait_s: float = DEFAULT_MAX_WAIT_S

    def __post_init__(self) -> None:
        if type(self.batch_size) is not int or not (
                MIN_BATCH_SIZE <= self.batch_size <= MAX_BATCH_SIZE):
            raise ValueError(
                f"batch_size 必须为 int 且在 {MIN_BATCH_SIZE}.."
                f"{MAX_BATCH_SIZE}（DashScope 端点硬顶，主窗裁定窗；env "
                f"{EMBEDDING_BATCH_SIZE_ENV}）：{self.batch_size!r}")
        if (not isinstance(self.max_wait_s, (int, float))
                or isinstance(self.max_wait_s, bool)
                or not math.isfinite(self.max_wait_s)
                or not MIN_MAX_WAIT_S <= self.max_wait_s <= MAX_MAX_WAIT_S):
            raise ValueError(
                f"max_wait_s 必须为有限数且在 {MIN_MAX_WAIT_S}~"
                f"{MAX_MAX_WAIT_S}s（20~50ms 窗；env "
                f"{EMBEDDING_BATCH_MAX_WAIT_MS_ENV}）：{self.max_wait_s!r}")


def embedding_micro_batch_spec_from_env(
        env: Mapping[str, str] | None = None) -> EmbeddingMicroBatchSpec:
    """解析嵌入微批装配 spec（缺席=全缺省=OFF；非法值 ValueError）。

    DEDUP_EMBEDDING_BATCH_MAX_WAIT_MS 以毫秒入参（运维口径），模块/规格
    面单位=秒（EmbeddingBatchConfig 同单位）——解析层换算后执法 20..50ms
    窗。
    """
    source = _env_source(env)
    raw_size = (source.get(EMBEDDING_BATCH_SIZE_ENV) or "").strip()
    batch_size = DEFAULT_BATCH_SIZE
    if raw_size:
        try:
            batch_size = int(raw_size)
        except ValueError as error:
            raise ValueError(
                f"{EMBEDDING_BATCH_SIZE_ENV}={raw_size!r} 非整数"
                f"（合法窗 {MIN_BATCH_SIZE}..{MAX_BATCH_SIZE}）") from error
    raw_wait_ms = (source.get(EMBEDDING_BATCH_MAX_WAIT_MS_ENV) or "").strip()
    max_wait_s = DEFAULT_MAX_WAIT_S
    if raw_wait_ms:
        try:
            wait_ms = float(raw_wait_ms)
        except ValueError as error:
            raise ValueError(
                f"{EMBEDDING_BATCH_MAX_WAIT_MS_ENV}={raw_wait_ms!r} 非数值"
                f"（合法窗 {MIN_MAX_WAIT_S * 1000:g}.."
                f"{MAX_MAX_WAIT_S * 1000:g} 毫秒）") from error
        max_wait_s = wait_ms / 1000.0
    return EmbeddingMicroBatchSpec(
        enabled=_switch_from_env(source, EMBEDDING_MICRO_BATCH_ENV),
        batch_size=batch_size,
        max_wait_s=max_wait_s)


# ---------------------------------------------------------------- Embedding 写径微批包装

class BatchedEmbeddingClient:
    """EmbeddingClient 写径微批包装（读径/台账/护栏直通；三态失败映射）。

    装配面（RealVectorPipeline 构造位以本件替 client——pipeline 零改动）：
    - ``dimension`` / ``max_input_chars``：护栏属性直通（INV-4 装配期
      对拍/6000 字截断事件口径不变）；
    - ``usage_snapshot()``：台账直通（pipeline 的 tokens_used/cache_hits
      增量口径不变——嵌入经微批后计数仍发生被包 client 内）；
    - ``embed_query``：读径直通（微批=写径专属；查询径 embedding_query
      零触碰）；
    - ``embed_documents``：写径经 EmbeddingBatcher（batch_call_fn=被包
      client.embed_documents 原方法——缓存/预算闸/usage/截断全保留）；
    - ``close()``：停 flusher 收尾（停批在判条目照发完再退；拒新提交）；
      网关台账经 ``batcher.ledger`` 观测。

    失败语义（包装侧三态执法，INV-1 不吞错冒充）：批内任一条
    VectorWriteUnknown → 该异常原样传播（确认未知——pipeline 不翻状态，
    pipeline.py:100-103 口径；未知优先于终败传播=fail-closed 方向：不把
    未知冒充已知终败）；否则首错原样传播（EmbeddingApiError → pipeline
    翻 failed，pipeline.py:96-99 口径）。批级失败→逐条回落单发（一批坏
    一条不死全批）由网关自携（embedding_batch.py:329-348）。

    未代理面缺省 AttributeError 即报（显式接线面收口——消费方要新面须
    在本类显式补，不静默绕过微批层）。
    """

    def __init__(self, client: Any, *, spec: EmbeddingMicroBatchSpec,
                 clock: Callable[[], float] | None = None) -> None:
        if not spec.enabled:
            raise ValueError(
                "EmbeddingMicroBatchSpec.enabled=False——OFF 态不接线"
                f"（开关 {EMBEDDING_MICRO_BATCH_ENV} 缺省关）")
        for face, name in (("embed_documents", "写径嵌入面"),
                           ("embed_query", "读径嵌入面"),
                           ("usage_snapshot", "台账面")):
            if not callable(getattr(client, face, None)):
                raise TypeError(
                    f"被包装 client 缺 {face}（{name}）——鸭式装配面不全"
                    "即拒（fail-closed，不静默半接线）")
        for face, name in (("dimension", "INV-4 对拍维度"),
                           ("max_input_chars", "截断护栏")):
            if not hasattr(client, face) or getattr(client, face) is None:
                raise TypeError(
                    f"被包装 client 缺 {face} 或为 None（{name}）——鸭式"
                    "装配面不全即拒（fail-closed，不静默半接线）")
        self._client = client
        self.spec = spec
        self.batcher = EmbeddingBatcher(
            client.embed_documents,
            config=EmbeddingBatchConfig(
                batch_size=spec.batch_size, max_wait_s=spec.max_wait_s),
            clock=clock)

    # ---------- 直通面（读径/台账/护栏——微批不触碰） ----------

    @property
    def dimension(self) -> Any:
        """空间维度直通（INV-4 装配防线：pipeline/QueryEmbedder 对拍）。"""
        return self._client.dimension

    @property
    def max_input_chars(self) -> Any:
        """6000 字截断护栏直通（pipeline 截断事件口径不变）。"""
        return self._client.max_input_chars

    def usage_snapshot(self) -> dict:
        """usage/成本台账直通（pipeline tokens_used/cache_hits 增量口径）。"""
        return self._client.usage_snapshot()

    def embed_query(self, text: str) -> list[float]:
        """读径直通（查询侧 LRU/缓存/预算语义逐字节现役）。"""
        return self._client.embed_query(text)

    # ---------- 写径微批面 ----------

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        """写径嵌入经微批网关（跨调用方合批；位次=输入序保序回填）。

        空输入/非 str 校验由网关自携（embedding_batch.py:214-218，与
        embedding_client.py:617-618 家族同文案）；结果三态映射见类
        docstring（未知优先传播/首错原样/绝不吞错）。
        """
        outcomes = self.batcher.embed_documents(texts)
        for outcome in outcomes:
            error = outcome.error
            if error is not None and isinstance(error, VectorWriteUnknown):
                raise error        # 确认未知优先（不把未知冒充已知终败）
        for outcome in outcomes:
            if outcome.error is not None:
                raise outcome.error    # 首错原样（EmbeddingApiError→failed）
        return [outcome.vector for outcome in outcomes]

    def close(self, timeout: float = 5.0) -> None:
        """停 flusher 收尾（在判条目照发完再退；此后拒新提交）。"""
        self.batcher.close(timeout)


def wire_embedding_micro_batch(
        client: Any, env: Mapping[str, str] | None = None, *,
        clock: Callable[[], float] | None = None) -> Any:
    """向量写路径微批接线入口：OFF（缺省）→ 原样返回 client（逐字节现役
    ——同一对象，零包装零行为差）；ON → BatchedEmbeddingClient 包装。

    接线位=RealVectorPipeline 构造面（vector/pipeline.py:62-79）：装配根
    以本函数产物替 client 传入即完成写径接入（pipeline 零改动）；读径
    （QueryEmbedder）不经本函数（微批=写径专属）。clock 注入面供单元层
    假钟定序（现役装配缺省真钟 time.monotonic）。
    """
    spec = embedding_micro_batch_spec_from_env(env)
    if not spec.enabled:
        return client
    return BatchedEmbeddingClient(client, spec=spec, clock=clock)


__all__ = [
    "EMBEDDING_BATCH_MAX_WAIT_MS_ENV",
    "EMBEDDING_BATCH_SIZE_ENV",
    "EMBEDDING_MICRO_BATCH_ENV",
    "JUDGE_PAIR_CONCURRENCY_ENV",
    "JUDGE_PAIR_EXECUTOR_ENV",
    "JUDGE_PAIR_TIMEOUT_ENV",
    "BatchedEmbeddingClient",
    "EmbeddingMicroBatchSpec",
    "JudgeExecutorSpec",
    "WiredJudgePairExecutor",
    "build_judge_pair_executor",
    "embedding_micro_batch_spec_from_env",
    "judge_executor_spec_from_env",
    "wire_embedding_micro_batch",
    "wire_judge_pair_executor",
]
