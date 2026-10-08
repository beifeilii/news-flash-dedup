"""F1 召回服务（B4/E1 真链路实施 J9 片；设计=log\设计-真链路接线包.md §三-F1）。

编排五通道 → freeze_recall_plan 冻结计划；decide/commit 输入双侧映射；
VectorSearcher 鸭式端口 + NullVectorSearcher 过渡态（§2.4，N20-b 缺口明示）；
模式闸解析（§2.2）；F3 FactSupplyPort 默认恒等投影。

纪律（§三-F1⑦）：不判重、不签发、不碰白名单——候选角色非判重器；
通道自报耗时经 ChannelResult.elapsed_ms 透传 ChannelAudit（窗口W2Fβ 条58
已封通道面）；非法候选注入由通道查询 filter + candidate_eligible + decide
绑定门双层现役执法，编排器不另加过滤（不引入新洞口）。
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any

from .bm25_channel import BM25Channel
from .entity_channel import EntityChannel
from .es_gateway import elapsed_ms_since
from .fusion import RecallPlan, freeze_recall_plan
from .hash_channel import HashChannel
from .models import ChannelResult, RecallCandidate, RecallRequest
from .near_channel import NearChannel


# ---------- 模式闸（§2.2，fail-closed） ----------

RECALL_MODE_ENV = "DEDUP_RECALL_MODE"
# 2026-10-08（B+）：parallel_live 注册——并行预检模式（默认 fixed/live
# 路径逐字节不动；装配根注入 parallel_driver 方可启用，缺注 fail-closed）
RECALL_MODES = frozenset({"fixed", "shadow", "live", "parallel_live"})
RECALL_MODE_DEFAULT = "fixed"


class RecallModeInvalid(ValueError):
    """DEDUP_RECALL_MODE 未知值（含大小写变体）——fail-closed 拒启
    （es_client.py:48-53 / milvus_client.py:21-22 同型纪律）。"""


def mode_from_environment(env: Mapping[str, str] | None = None) -> str:
    """解析模式闸：缺省/空串=fixed（现役逐字节）；fixed/shadow/live 三态；
    其余一律 RecallModeInvalid（精确匹配，"FIXED" 等大小写变体同拒）。"""
    source = os.environ if env is None else env
    raw = source.get(RECALL_MODE_ENV, "")
    if not raw:
        return RECALL_MODE_DEFAULT
    if raw not in RECALL_MODES:
        raise RecallModeInvalid(
            f"{RECALL_MODE_ENV} must be one of fixed|shadow|live; got {raw!r}")
    return raw


# 2026-10-09（P0-a，log\temp\判定链修复方案-P0施工单-呈外部评审.md §一-P0-a）：
# 覆盖闸 frontier 接线开关 DEDUP_COVERAGE_FRONTIER（默认关=现役逐字节，
# vector_prepared 快照零写者、vector_store.py:430-447 恒 unproven 形态不动）。
COVERAGE_FRONTIER_ENV = "DEDUP_COVERAGE_FRONTIER"


def coverage_frontier_enabled(env: Mapping[str, str] | None = None) -> bool:
    """2026-10-09（P0-a）开关解析：精确 "1"=开；缺省/空串/其余一律关
    （fail-closed 默认关，"true"/大小写变体等不放大——上方模式闸同型纪律）。"""
    source = os.environ if env is None else env
    return source.get(COVERAGE_FRONTIER_ENV, "") == "1"


# ---------- VectorSearcher 鸭式端口 + 过渡态（§2.4） ----------


class NullVectorSearcher:
    """过渡态装配（N20-b 缺口明示）：B5 真向量落地前的显式缺口端口。

    返回形态与 vector_store.py:306-309 现役拒收码同型：
    ChannelResult("embedding","unavailable",(),"embedding_v1",
    error_code="SPACE_UNCONFIRMED")——recall_gaps 含 embedding、
    recall_incomplete=True、coverage 派生 fail-closed=False、缺口入工件。
    B1′ 已验证（W2 修复波 2 (a)-1 锚勘正）：vector_store.search(
    query_vector=None) 经 validate_vector 判型闸（vector_space.py:83-84）
    抛 ValueError→vector_store.py:314-324 捕获返回 unavailable+
    EMBEDDING_INVALID（W2Fβ 错误面对齐）——本端口不调真 store，纯缺口
    应答。
    """

    def __init__(self, *, clock: Callable[[], datetime] | None = None) -> None:
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def search(self, request: RecallRequest,
               query_vector: list[float] | None = None, *,
               timeout_s: float | None = None) -> ChannelResult:
        # W2 ⑩①-5：向量端口协议同姿势增 timeout_s kw——缺口端口不消费
        # （纯缺口应答，无下游调用可传）。
        started = self.clock()
        return ChannelResult(
            "embedding", "unavailable", (), "embedding_v1",
            error_code="SPACE_UNCONFIRMED",
            elapsed_ms=elapsed_ms_since(started, self.clock()))


# ---------- F3 FactSupplyPort（current 侧 facts 供给） ----------


class IdentityFactSupply:
    """默认恒等投影（21:4x 主窗口裁定形态，与回放 test_p23_uat.py:346-380
    `_identity_facts` 同形）。

    零语义主张：subject=全文原文（evidence 偏移真实可核）、predicate 固定
    常量、time/key_object/numerics 全 missing/空——不抽取、不推断、不编造
    任何语义；仅使 decide 的"报告须含 ≥1 Fact"结构性前置得到满足
    （pair_alignment.py:377）。同文同版本确定性幂等（重算同值，不缓存）。
    """

    def __call__(self, record_id: str, text: str) -> list[dict]:
        if not isinstance(text, str):
            raise TypeError("text must be str")
        raw_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        span = {"record_id": record_id, "field": "text",
                "quote": text, "start": 0, "end": len(text)}
        present = {"status": "present", "raw_value": text, "evidence": [span]}
        missing = {"status": "missing", "raw_value": None, "evidence": []}
        return [{
            "fact_id": f"idem-{raw_hash[:16]}",
            "evidence": [span],
            "fact_type": {"status": "present",
                          "raw_value": "verbatim_identity_projection",
                          "evidence": [span]},
            "subject": present,
            "event_state": {
                "predicate": {"status": "present",
                              "raw_value": "verbatim_identity_projection",
                              "evidence": [span]},
                "polarity": {"status": "present",
                             "raw_value": "verbatim_identity_projection",
                             "evidence": [span]},
                "modality": missing,
                "attribution": missing,
            },
            "time": {"expression": missing, "stage": missing, "anchor": missing},
            "key_object": missing,
            "numerics": [],
        }]


class RuleFactSupply:
    """可选 rule 供给（facts/rule.py extract_facts，回放
    P23_FACT_SUPPLY=rule_baseline_v1 同款；dict_version 可显式指定 v2）。
    懒导入——identity 默认路径零新依赖（test_p23_uat.py:401-432 同纪律）。"""

    def __init__(self, *, dict_version: str | None = None) -> None:
        self.dict_version = dict_version

    def __call__(self, record_id: str, text: str) -> list[dict]:
        from news_flash_dedup.facts import rule as facts_rule

        if self.dict_version is None:
            return facts_rule.extract_facts(record_id, text)
        return facts_rule.extract_facts(record_id, text,
                                        dict_version=self.dict_version)


# ---------- F1 召回服务 ----------

#: decide 消费键集（decide/service.py:170-173/:237-249 消费形；R-3 双侧恰含）
COMMIT_INPUT_KEYS = frozenset({
    "record_id", "item_id", "text", "raw_hash", "arrival_seq",
    "scope_id", "business_date", "facts",
})

_ES_CHANNELS = ("hash", "near", "bm25", "entity")


class RecallService:
    """召回编排器：五通道齐递 → 冻结计划 → decide/commit 输入映射。

    构造注入（显式注入，同五通道风格 hash_channel.py:66-79）：
    - client/index_prefix：四个 ES 通道的构造原料（默认装配）；单元层可经
      `channels` 注入 fake 通道（键=hash/near/bm25/entity，鸭式 search(request)）；
    - watermark_provider：F2⑤ 读侧端口（visible_seq/prepared_seq 供源；
      V1 消费点在 F0 组合根构造 RecallRequest 处，本服务持引用为装配单源）；
    - vector_searcher：VectorSearcher 鸭式端口（search(request, query_vector)
      ->ChannelResult）；缺省 NullVectorSearcher（§2.4 过渡态）；
    - fact_supply：F3 端口（__call__(record_id, text)->list[dict]）；缺省
      IdentityFactSupply；
    - clock：通道构造时钟同源注入。
    """

    def __init__(self, client: Any, index_prefix: str, *,
                 watermark_provider: Any = None,
                 vector_searcher: Any = None,
                 fact_supply: Callable[[str, str], list[dict]] | None = None,
                 clock: Callable[[], datetime] | None = None,
                 channels: Mapping[str, Any] | None = None,
                 allowed_namespaces: tuple[str, ...] = ("p01-batch-",),
                 vector_frontier_advancer: Any = None) -> None:
        self.client = client
        self.index_prefix = index_prefix
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.watermark_provider = watermark_provider
        self.vector_searcher = (vector_searcher if vector_searcher is not None
                                else NullVectorSearcher(clock=self.clock))
        self.fact_supply = (fact_supply if fact_supply is not None
                            else IdentityFactSupply())
        # 2026-10-09（P0-a）：vector_prepared 前沿推进器端口（additive，
        # 显式注入同五通道风格；None+开关开+client 在场→懒装配真件，
        # 开关关→本字段零消费，现役逐字节）。
        self.vector_frontier_advancer = vector_frontier_advancer
        self._frontier_advancer_cache: Any = None
        if channels is not None:
            unknown = set(channels) - set(_ES_CHANNELS)
            if unknown:
                raise ValueError(f"unknown ES channel overrides: {sorted(unknown)}")
            self._channels = dict(channels)
        else:
            # W-A 族④(a)-5（A2-06）：allowed_namespaces 透传四通道默认装配
            # （channels 覆盖注入时本参数不适用——通道已构造完毕）；默认
            # ("p01-batch-",) 与现役逐字节等价，生产命名空间由装配根注入。
            self._channels = {
                "hash": HashChannel(client, index_prefix, clock=self.clock,
                                    allowed_namespaces=allowed_namespaces),
                "near": NearChannel(client, index_prefix, clock=self.clock,
                                    allowed_namespaces=allowed_namespaces),
                "bm25": BM25Channel(client, index_prefix, clock=self.clock,
                                    allowed_namespaces=allowed_namespaces),
                "entity": EntityChannel(client, index_prefix, clock=self.clock,
                                        allowed_namespaces=allowed_namespaces),
            }

    # ---------- 计划 ----------

    def build_plan(self, request: RecallRequest, *,
                   budget: "ProcessingBudget | None" = None) -> RecallPlan:
        """五通道齐递（四 ES 通道+向量端口）→ freeze_recall_plan 冻结。

        缺路 CHANNEL_MISSING 记账属融合现役面（fusion.py:154-158）；编排器
        恒五路齐递，不吞通道异常（通道自报 unavailable 为缺口语义，融合派生）。

        W2 ⑩①-5（N43 挂账清偿，12 L375）：budget 在场→五通道收
        timeout_s=budget.budgeted_timeout(channel_timeout_s)（300ms 注入、
        服从剩余预算截断）；budget=None→不增传 timeout_s（现役逐字节，
        旧通道双倍体零冲击）。
        """
        channel_timeout = (budget.budgeted_timeout(
            float(budget.config.channel_timeout_s))
            if budget is not None else None)

        def _call(channel, *args):
            return (channel.search(*args, timeout_s=channel_timeout)
                    if channel_timeout is not None else channel.search(*args))

        # 2026-10-09（P0-a，log\temp\判定链修复方案-P0施工单-呈外部评审.md
        # §一-P0-a）：覆盖闸 frontier 接线点=build_plan 五通道扇出前（融合段
        # 入口）。覆盖求值发生在向量通道 search 内部（vector_store.py:430-447，
        # 扇出并行体内）——融合完成后再推进对本请求恒无效且按单飞升序链
        # 结构性滞后一件（当日首件永 False，"让不重复能签出"落空），故推进
        # 必须先于扇出。开关关→零调用（现役逐字节）。
        self._advance_vector_frontier(request)
        # B+ 第一步（2026-10-08）：五通道扇出并行化——通道间无依赖（融合层
        # 只做结果汇总），I/O 等待互相重叠。行为与串行逐字节等价：
        # ① responses 顺序恒为 [hash, near, bm25, vector, entity]（future
        #    按提交顺序取 result）；
        # ② 异常传播按通道序首个失败（f.result() 依序抛出，与串行"hash 先
        #    败先抛"一致；并行下后续通道仍会执行，皆为只读无副作用）；
        # ③ channel_timeout 单快照全通道共享（与串行同源，L218-221 不变）。
        calls = (
            (self._channels["hash"], (request,)),
            (self._channels["near"], (request,)),
            (self._channels["bm25"], (request,)),
            (self.vector_searcher, (request, None)),
            (self._channels["entity"], (request,)),
        )
        with ThreadPoolExecutor(max_workers=len(calls)) as pool:
            futures = [pool.submit(_call, channel, *args)
                       for channel, args in calls]
            responses = [future.result() for future in futures]
        return freeze_recall_plan(request, responses)

    # ---------- P0-a 覆盖闸 frontier 接线（2026-10-09，开关默认关） ----------

    def _advance_vector_frontier(self, request: RecallRequest) -> None:
        """推进 vector_prepared 前沿快照证据（开关关=零效应，现役逐字节）。

        证据纪律（施工单 §一-P0-a 防护①"接线只认真实快照证据"）：
        - NullVectorSearcher 过渡态=无向量通道 → 不推进（无通道而写覆盖
          快照=伪造向量证据，后接真 store 即成假 True，结构性禁绝）；
        - ready 证据=request.prepared_seq 已证准备前沿（live 路径 worker.py:
          488-490 已闸 prepared≥arrival_seq-1）——装配方负责"prepared⟹向量
          写确认"不变式（T 跑/回放管线语料先验全量播种；实时管线 P19 双写
          先于准备就绪），本服务只中继水印证据、不新造；
        - 推进经 VectorFrontierAdvancer 孔洞/单调/CAS 纪律（无证明不跳洞、
          单调不回退、混空间拒写、冲突有界放弃）；
        - 推进失败=维持停摆（覆盖求值由 vector_store.py:430-447 独立
          fail-closed 执法——快照缺席/有孔/前沿滞后（时序倒错）恒 False，
          宁缺毋滥；快照 generation/advanced_at 停滞即可观测面，E 批 R2
          对冲同口径）。
        """
        if not coverage_frontier_enabled():
            return
        if isinstance(self.vector_searcher, NullVectorSearcher):
            return
        space_id = request.embedding_space_id
        if not isinstance(space_id, str) or not space_id:
            return
        prepared = request.prepared_seq
        if type(prepared) is not int or prepared < 1:
            return
        advancer = self.vector_frontier_advancer
        if advancer is None:
            advancer = self._self_assembled_frontier_advancer()
            if advancer is None:
                return
        try:
            advancer.advance(request.scope_id, request.business_date, space_id,
                             range(1, prepared + 1))
        except Exception:
            # 推进失败=维持停摆（fail-closed 安全向）：本请求覆盖求值独立
            # 执法，快照缺席/陈旧恒 False，绝不以失败推进冒充证据。
            pass

    def _self_assembled_frontier_advancer(self) -> Any:
        """懒装配真 VectorFrontierAdvancer（开关开+未显式注入+client 在场）。

        ElasticsearchBatchStore 前缀闸不符/client 缺席 → None（fail-closed
        不推进）；懒导入——默认关路径零新依赖（RuleFactSupply 同纪律）；
        成功装配后缓存复用（CAS 有界重试参数/时钟同源）。
        """
        if self._frontier_advancer_cache is not None:
            return self._frontier_advancer_cache
        if self.client is None:
            return None
        try:
            from news_flash_dedup.batch_es_store import ElasticsearchBatchStore

            from .vector_frontier import VectorFrontierAdvancer

            self._frontier_advancer_cache = VectorFrontierAdvancer(
                ElasticsearchBatchStore(self.client,
                                        index_prefix=self.index_prefix),
                clock=self.clock)
        except Exception:
            return None
        return self._frontier_advancer_cache

    # ---------- decide/commit 输入映射（F1③） ----------

    def _mapping(self, *, record_id: str, item_id: str, text: str,
                 raw_hash: str, arrival_seq: int, scope_id: str,
                 business_date: str, supply: Callable[[str, str], list[dict]]
                 ) -> dict:
        return {
            "record_id": record_id,
            "item_id": item_id,
            "text": text,
            "raw_hash": raw_hash,
            "arrival_seq": arrival_seq,
            "scope_id": scope_id,
            "business_date": business_date,
            "facts": supply(record_id, text),
        }

    def build_commit_inputs(
            self, plan: RecallPlan, current_doc: Mapping,
            supply: Callable[[str, str], list[dict]] | None = None,
    ) -> tuple[dict, tuple[dict, ...], bool, str | None]:
        """返回 (current, candidates, coverage_complete, expires_at)。

        - current：物化文档字段直取（record_id/item_id/text/raw_hash/
          scope_id/business_date/arrival_seq）+ facts（F3 供给）；
        - candidates：plan.required（fusion.py:227-233）逐员映射，键集同
          current；raw_hash=text 派生（decide/service.py:37-46 重算口径同文）；
          scope_id/business_date=同分区恒等透传；候选 arrival_seq 严格小于
          current（fusion.py:89-98 同型校验，此处双层断言不替代通道执法）；
        - coverage_complete=not plan.recall_incomplete（13:20 案二：真路径
          禁默认 True，恒由缺口派生）；
        - expires_at：物化文档透传键原值（batch_admission.py:448；缺键=None
          =commit_one 现役默认不钳制）。
        """
        supply = supply if supply is not None else self.fact_supply
        current = self._mapping(
            record_id=current_doc["record_id"],
            item_id=current_doc["item_id"],
            text=current_doc["text"],
            raw_hash=current_doc["raw_hash"],
            arrival_seq=current_doc["arrival_seq"],
            scope_id=current_doc["scope_id"],
            business_date=current_doc["business_date"],
            supply=supply,
        )
        candidates: list[dict] = []
        for item in plan.required:
            candidate = item.candidate
            if not isinstance(candidate, RecallCandidate):
                raise TypeError("plan.required members must carry RecallCandidate")
            if candidate.arrival_seq >= current["arrival_seq"]:
                raise ValueError(
                    "candidate arrival_seq must be strictly below current")
            candidates.append(self._mapping(
                record_id=candidate.record_id,
                item_id=candidate.item_id,
                text=candidate.text,
                raw_hash=hashlib.sha256(
                    candidate.text.encode("utf-8")).hexdigest(),
                arrival_seq=candidate.arrival_seq,
                scope_id=current["scope_id"],
                business_date=current["business_date"],
                supply=supply,
            ))
        coverage_complete = not plan.recall_incomplete
        expires_at = current_doc.get("expires_at")
        return current, tuple(candidates), coverage_complete, expires_at


__all__ = [
    "COMMIT_INPUT_KEYS",
    "COVERAGE_FRONTIER_ENV",
    "IdentityFactSupply",
    "NullVectorSearcher",
    "RECALL_MODE_DEFAULT",
    "RECALL_MODE_ENV",
    "RECALL_MODES",
    "RecallModeInvalid",
    "RecallService",
    "RuleFactSupply",
    "coverage_frontier_enabled",
    "mode_from_environment",
]
