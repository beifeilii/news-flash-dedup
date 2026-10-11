"""F0 组合根（B4/E1 真链路实施 J9 片；设计=log\设计-真链路接线包.md §三-F0）。

六职责（§三-F0）：
① 模式闸解析（RecallModeInvalid 未匹配→不启动）；
② fixed 态不启动判重循环（零调用——scan/prepare/召回/仓储全不触）；
③ shadow/live 态：触发 F2 准备驱动 → 拉取 watermark → 扫描注册序
   （arrival_seq 升序）；
④ 逐条构造 RecallRequest（RecallRequest 域模型是 recall 层内部真源）；
⑤ 调 commit_one（live）或影子决策（shadow）；
⑥ 有序提交（同 (scope_id,business_date) 内 arrival_seq 升序单飞）+ STALE
   重试（重读水位→重驱准备→重建计划→有界重试）。

双闸正交（§2.3）：DEDUP_RECALL_MODE 管召回/判重路径；DEPLOY_ENV+CONFIRM 族
管持久化/集群触达；p01-batch-/p18-batch- 前缀闸族管索引命名空间——三族正交。

影子双腿（§五-R148）：shadow 态全程零 commit 仓储调用（协议三件套无一被调）、
零 callback/零投递意图；影子决策与生效腿同输入同语义源（coordinator.py:144-160
决策调用形 /:161-198 零候选分支同型镜像——本文件 shadow_decide 即该镜像，
对拍钉 test_recall_worker.py::test_r11_* 执法）；影子产物双落（计划工件+
影子决策工件，单飞、有序、边界终止条件与生效腿一致）。
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from news_flash_dedup.commit.coordinator import (
    CommitContext,
    CommitOneError,
    CommitOutcome,
    commit_one,
)
from news_flash_dedup.deadline import parse_utc_iso
from news_flash_dedup.decide import judge_adapter as judge_adapter_module
from news_flash_dedup.decide import service as decide_service
from news_flash_dedup.decide.types import DecideOutcome
from news_flash_dedup.es_admission_schema import day_index
from news_flash_dedup.persist.es_store import build_commit_one_store
from news_flash_dedup.runtime_budget import ProcessingBudget, RuntimeBudgetConfig

from .es_gateway import response_body
from .fact_supply import build_llm_facts_from_env
from .fusion import RecallPlan
from .models import RecallRequest
from .service import RECALL_MODES, mode_from_environment

_TASK_STATES_REGISTRATION = ("accepted", "running")
_PLAN_ARTIFACT_KIND = "recall_plan"
_SHADOW_ARTIFACT_KIND = "shadow_decision"


def _jcs(value: object) -> bytes:
    # admission.py:82 同款 JCS（UTF-8、键排序、紧致分隔符）
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def _aware_iso(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("clock must be timezone aware")
    return value.isoformat()


# ---------- 影子决策镜像（同型源：coordinator.py:144-160 /:161-198） ----------


def shadow_decide(current: Mapping, candidates: tuple[Mapping, ...], *,
                  coverage_complete: bool, visible_seq: int,
                  prepared_seq: int, pipeline_version: str = "dedup_v1",
                  dictionary=None,
                  dictionary_version: str = "dict_v1",
                  judge_callable=None,
                  judge_version_config=None,
                  llm_facts=None) -> DecideOutcome:
    """影子腿决策：与生效腿 commit_one 决策段同型镜像（零副作用纯函数）。

    候选非空：decide_for_task(history=candidates[-1], candidates=candidates[:-1],
    ...)（coordinator.py:144-160 同型调用）；零候选：coordinator.py:161-198
    两分支逐字镜像（reason/internal_code/unresolved_fields 同文）。

    judge_callable（2026-10-09 P1 联调，coordinator.py commit_one 装配同型）：
    显式注入优先；None=按 env 装配真件（DEDUP_JUDGE_PROOF 关→未注入
    fail-closed 未决）。影子腿与生效腿同闸同件，镜像语义不破。

    终审复审补丁（唯一权威单源传递）：judge_version_config 在场时判官
    预装配（judge_callable 未注入时）与 decide_for_task 同一配置一路
    传入（禁止各读环境变量）；缺省维持原 env 分发口径（与生效腿
    commit_one 缺省行为同型镜像）。

    llm_facts（最终接线窗，P1c 装配注入设计单 §二 A+B）：显式注入优先
    （注入位 A——组合根/工装罐装）；None=按 env 预装配（注入位 B——
    build_llm_facts_from_env：DEDUP_LLM_FACTS_ONDEMAND 缺省关→None=
    decide_for_task 缺省双轨关，现役语义逐字节；与 judge_callable
    预装配完全同型）。影子腿与生效腿（commit_one）同闸同件。
    """
    if candidates:
        if judge_callable is None:
            judge_callable = judge_adapter_module.build_judge_callable(
                version_config=judge_version_config)
        if llm_facts is None:
            # 注入位 B 预装配（设计单 §二-B：judge_callable=None→
            # build_judge_callable 同型；开关关→None=缺省双轨关零 diff）。
            llm_facts = build_llm_facts_from_env()
        return decide_service.decide_for_task(
            history=candidates[-1],
            candidates=candidates[:-1],
            current=current,
            pipeline_version=pipeline_version,
            coverage_complete=coverage_complete,
            visible_seq=visible_seq,
            prepared_seq=prepared_seq,
            dictionary=dictionary,
            dictionary_version=dictionary_version,
            judge_callable=judge_callable,
            judge_version_config=judge_version_config,
            llm_facts=llm_facts,
        )
    if coverage_complete:
        return DecideOutcome(
            item_id=current["item_id"],
            text=current["text"],
            decision="不重复",
            duplicate_ids=(),
            reason="本域日本分区首条，无历史候选可比对。",
            internal_code="NO_DUPLICATE_FOUND",
            pair_codes={},
            used_evidence=(),
            unresolved_fields=(),
            raw_hash=current["raw_hash"],
            pipeline_version=pipeline_version,
        )
    return DecideOutcome(
        item_id=current["item_id"],
        text=current["text"],
        decision="边界case/疑难case",
        duplicate_ids=(),
        reason="召回覆盖不全且零候选，无法确认无重复。",
        internal_code="RECALL_INCOMPLETE",
        pair_codes={},
        used_evidence=(),
        unresolved_fields=("candidates",),
        raw_hash=current["raw_hash"],
        pipeline_version=pipeline_version,
    )


# ---------- 影子双工件（§五-R148） ----------


def plan_artifact(plan: RecallPlan, request: RecallRequest) -> dict:
    """召回计划工件（真计划）：request 身份 + 冻结计划全要素
    （fusion.py:66-79 RecallPlan 全字段 + :45-62 ChannelAudit 全字段）。"""
    return {
        "kind": _PLAN_ARTIFACT_KIND,
        "scope_id": request.scope_id,
        "business_date": request.business_date,
        "record_id": request.record_id,
        "item_id": request.item_id,
        "arrival_seq": request.arrival_seq,
        "visible_seq": request.visible_seq,
        "prepared_seq": request.prepared_seq,
        "version": plan.version,
        "recall_incomplete": plan.recall_incomplete,
        "recall_gaps": [{"channel": gap.channel, "reason": gap.reason}
                        for gap in plan.recall_gaps],
        "channel_audits": [{
            "channel": audit.channel,
            "status": audit.status,
            "query_version": audit.query_version,
            "candidate_count": audit.candidate_count,
            "pages": audit.pages,
            "topk_excluded": audit.topk_excluded,
            "visible_seq": audit.visible_seq,
            "prepared_seq": audit.prepared_seq,
            "coverage_complete": audit.coverage_complete,
            "truncated_buckets": list(audit.truncated_buckets),
            "error_code": audit.error_code,
            "elapsed_ms": audit.elapsed_ms,
            "truncate_reason": audit.truncate_reason,
        } for audit in plan.channel_audits],
        "required": [{
            "record_id": fused.candidate.record_id,
            "item_id": fused.candidate.item_id,
            "arrival_seq": fused.candidate.arrival_seq,
            "channels": [hit.channel for hit in fused.channel_hits],
        } for fused in plan.required],
        "pair_top10": [fused.candidate.record_id for fused in plan.pair_top10],
        "hash_protected": [fused.candidate.record_id
                           for fused in plan.hash_protected],
        "planning_excluded": plan.planning_excluded,
    }


def shadow_decision_artifact(outcome: DecideOutcome, *, plan: RecallPlan,
                             request: RecallRequest,
                             generated_at: str) -> dict:
    """影子决策工件：DecideOutcome 全要素（五字段+签发码）+ 计划引用链
    （plan_ref=计划工件 JCS 摘要；回放对拍可溯源）。零 callback/delivery 字段。"""
    plan_ref = hashlib.sha256(_jcs(plan_artifact(plan, request))).hexdigest()
    public = outcome.to_public_dict()
    return {
        "kind": _SHADOW_ARTIFACT_KIND,
        "leg": "B",
        "generated_at": generated_at,
        "plan_ref": plan_ref,
        "scope_id": request.scope_id,
        "business_date": request.business_date,
        "record_id": request.record_id,
        "item_id": request.item_id,
        "arrival_seq": request.arrival_seq,
        "outcome": {
            "item_id": public["item_id"],
            "text": public["text"],
            "decision": public["decision"],
            "duplicate_ids": public["duplicate_ids"],
            "reason": public["reason"],
            "internal_code": outcome.internal_code,
            "pair_codes": dict(outcome.pair_codes),
            "unresolved_fields": list(outcome.unresolved_fields),
            "raw_hash": outcome.raw_hash,
            "pipeline_version": outcome.pipeline_version,
        },
    }


# ---------- 注册序扫描（F0③） ----------


class RegistrationScanError(RuntimeError):
    """W-A 族④(c)（A2-08）：扫描响应确认未知/形态畸形——记账不崩溃词表。"""


class ElasticsearchRegistrationScanner:
    """(scope,business_date) 内按注册序（arrival_seq 升序）枚举待处理物化文档。

    过滤形：scope_id + task_state∈{accepted,running} + arrival_seq>游标；
    排序 arrival_seq asc + record_id asc（确定性）。raw client.search 最小面。
    """

    def __init__(self, client: Any, index_prefix: str, *,
                 page_size: int = 100, max_pages: int = 1000) -> None:
        if min(page_size, max_pages) < 1:
            raise ValueError("scanner limits must be positive")
        self.client = client
        self.index_prefix = index_prefix
        self.page_size = page_size
        self.max_pages = max_pages

    def scan(self, scope_id: str, business_date: str, *,
             after_seq: int = 0) -> list[dict]:
        physical = self.index_prefix + day_index(business_date)
        docs: list[dict] = []
        from_seq = after_seq
        for _page in range(self.max_pages):
            body = {
                "size": self.page_size,
                "track_total_hits": True,
                "query": {"bool": {"filter": [
                    {"term": {"scope_id": scope_id}},
                    {"terms": {"task_state": list(_TASK_STATES_REGISTRATION)}},
                    {"range": {"arrival_seq": {"gt": from_seq}}},
                ]}},
                "sort": [{"arrival_seq": "asc"}, {"record_id": "asc"}],
            }
            # W-A 族④(c)（A2-08）：每页响应过四闸（判定式逐行对拍
            # bm25_channel.py:106-115 同源口径，异常类型域各属）——
            # 任一不过 → RegistrationScanError（确认未知，不裸逃、不
            # 静默默认）；空 hits list（合法末页）break 语义不变。
            try:
                result = response_body(self.client.search(index=physical, body=body))
            except ValueError as error:
                # G1 解包闸：非 Mapping/形态畸形（es_gateway 公共件 ValueError）
                # 归入 RegistrationScanError 类型化通道（不裸逃）。
                raise RegistrationScanError(
                    f"registration scan response is not a mapping: {error}"
                ) from error
            if result.get("timed_out") is not False:
                raise RegistrationScanError(
                    "registration scan timed_out true or omitted status")
            shards = result.get("_shards")
            if not isinstance(shards, Mapping) or shards.get("failed") != 0:
                raise RegistrationScanError("registration scan shard read failed")
            hits_block = result.get("hits")
            if not isinstance(hits_block, Mapping) or not isinstance(
                    hits_block.get("hits"), list):
                raise RegistrationScanError(
                    "registration scan hits structure is incomplete")
            hits = hits_block["hits"]
            if not hits:
                break
            for hit in hits:
                if not isinstance(hit, Mapping) or not isinstance(
                        hit.get("_source"), Mapping):
                    raise RegistrationScanError(
                        "registration scan hit source is malformed")
                docs.append(hit["_source"])
            if len(hits) < self.page_size:
                break
            from_seq = int(hits[-1]["_source"]["arrival_seq"])
        return docs


# ---------- 驱动报告 ----------


@dataclass(frozen=True)
class DriveReport:
    """drive_once 一轮报告（影子腿不生效闸的审计面）。"""
    mode: str
    scope_id: str
    business_date: str
    committed: tuple[str, ...] = ()
    shadowed: tuple[str, ...] = ()
    stale: tuple[str, ...] = ()
    awaiting: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()
    # W-A 族④(c)-3（A2-08）：扫描故障轮级留痕（additive 尾字段，现役
    # 构造点零冲击）；非 None ⇒ 当轮 committed/shadowed 恒空、游标未推进。
    scan_error: str | None = None
    # W2 ⑩①-2（N43 挂账清偿）：预算总闸到期桶——additive 尾字段（scan_error
    # 同例，现役构造点零冲击）；到期条目不启动模型/提交，单飞即停同 stale 族。
    budget_exhausted: tuple[str, ...] = ()


# ---------- F0 worker ----------


class _UnprovenBudget:
    """W2 ⑩①-2：预算不可证 fail-closed 哨兵（缺 accepted_at 键/解析失败
    →总闸到期同义处置，不启动模型调用、不猜预算——runtime_budget 模块零
    diff，哨兵仅在总闸探针处消费，绝不向 build_plan/commit_one 传播）。"""

    __slots__ = ()

    def exhausted(self) -> bool:
        return True


_UNPROVEN_BUDGET = _UnprovenBudget()


class DedupWorker:
    """召回→判重→提交组合根（单飞升序，STALE 有界重试）。

    构造注入（fail-closed）：
    - mode∈{fixed,shadow,live}（构造即校验，未匹配→ValueError 不启动）；
    - live 必须 commit_store（协议自检 persist/es_store.py:556-558
      build_commit_one_store 先行，TypeError 冒泡）；
    - shadow 必须 artifact_sink（emit(kind, payload) 鸭式；缺→ValueError）——
      影子工件双落无落点即拒装，防"影子腿静默蒸发"；
    - shadow 态 commit_store 可注入（测试对拍面），但 drive 全程零调用
      （R-11 钉执法）。
    """

    def __init__(self, *, mode: str, recall_service: Any, prepare_worker: Any,
                 watermark_provider: Any, scanner: Any,
                 commit_store: Any = None, artifact_sink: Any = None,
                 clock: Callable[[], datetime] | None = None,
                 max_stale_retries: int = 3,
                 pipeline_version: str = "dedup_v1",
                 dictionary=None, dictionary_version: str = "dict_v1",
                 budget_config: RuntimeBudgetConfig | None = None,
                 clock_mono: Callable[[], float] | None = None,
                 parallel_driver: Any = None,
                 judge_version_config: Any = None,
                 llm_facts: Any = None) -> None:
        if mode not in RECALL_MODES:
            raise ValueError(f"unknown recall mode: {mode!r}")
        if max_stale_retries < 1:
            raise ValueError("max_stale_retries must be positive")
        if mode == "live":
            if commit_store is None:
                raise ValueError("live mode requires commit_store")
            commit_store = build_commit_one_store(commit_store)
        if mode == "shadow":
            if artifact_sink is None or not callable(
                    getattr(artifact_sink, "emit", None)):
                raise ValueError("shadow mode requires artifact_sink.emit")
        # 2026-10-08（B+）：parallel_live=并行预检模式——驱动器由装配根
        # 注入（ParallelLiveDriver 协议：drive_round(scope, date)），缺注
        # fail-closed 不启动；fixed/shadow/live 三态语义逐字节不动。
        if mode == "parallel_live" and not callable(
                getattr(parallel_driver, "drive_round", None)):
            raise ValueError(
                "parallel_live mode requires parallel_driver.drive_round")
        self.mode = mode
        self.recall_service = recall_service
        self.prepare_worker = prepare_worker
        self.watermark_provider = watermark_provider
        self.scanner = scanner
        self.commit_store = commit_store
        self.artifact_sink = artifact_sink
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.max_stale_retries = max_stale_retries
        self.pipeline_version = pipeline_version
        self.dictionary = dictionary
        # 终审复审补丁（唯一权威单源传递）：判官版本配置单源注入——
        # 在场时影子腿预装配（shadow_decide）与生效腿（commit_one→
        # decide_for_task）同一配置一路传入，禁止各读环境变量；None=
        # 现役 env 分发口径逐字节（缺省零 diff）。
        self.judge_version_config = judge_version_config
        # 最终接线窗（P1c 装配注入设计单 §二 A）：按需 LLM facts 单源注入
        # ——在场时影子腿（shadow_decide）与生效腿（commit_one→
        # decide_for_task）同一件一路传递（显式注入优先；None=两腿各自按
        # env 预装配注入位 B——build_llm_facts_from_env 缺省关→None=
        # decide_for_task 缺省双轨关，现役语义逐字节零 diff）。
        self.llm_facts = llm_facts
        self.dictionary_version = dictionary_version
        # W2 ⑩①-2（N43 挂账清偿）：预算件——budget_config 在场→逐条按
        # accepted_at 派生 ProcessingBudget（T082 排队不重置）；None=现役
        # 逐字节零 diff。clock_mono 在场缺省 time.monotonic（T082 锚）。
        self.budget_config = budget_config
        self.clock_mono = (clock_mono if clock_mono is not None
                           else (time.monotonic
                                 if budget_config is not None else None))
        self._shadow_cursor: dict[tuple[str, str], int] = {}
        self._parallel_driver = parallel_driver

    # ---------- 一轮驱动 ----------

    def drive_once(self, scope_id: str, business_date: str, *,
                   budget: ProcessingBudget | None = None) -> DriveReport:
        """一轮驱动。W2 ⑩①-2：显式 budget=单条直驱/重领形态显式供给
        （挂账①形态：API/重试链显式重领同一实例——不重建故 deadline 跨
        重领不重置）；多条目轮共享一显式 budget 是把整轮误当一条处理
        （R3 注记：轮级预算应经构造注入 budget_config 逐条派生形态承载）。
        budget=None 且 budget_config=None → 现役逐字节零 diff。"""
        if self.mode == "fixed":
            # ② fixed 态不启动判重循环（现役逐字节等价：零调用）
            return DriveReport(mode=self.mode, scope_id=scope_id,
                               business_date=business_date)
        if self.mode == "parallel_live":
            # 2026-10-08（B+）：并行预检模式——整轮委托 ParallelLiveDriver
            # （喂池/保序定稿/批量落锤内核在其内）；DriveReport 桶映射：
            # committed/failed/awaiting 同款，deferred（断裂件）并入
            # awaiting（留下轮同语义），recomputed 为观测面不映射桶。
            report = self._parallel_driver.drive_round(scope_id, business_date)
            return DriveReport(
                mode=self.mode, scope_id=scope_id,
                business_date=business_date,
                committed=report.committed, failed=report.failed,
                awaiting=tuple(report.awaiting) + tuple(report.deferred))
        budget_kw: dict[str, Any] = {"budget": budget} if budget is not None else {}
        self.prepare_worker.prepare(scope_id, business_date, **budget_kw)
        if self.mode == "live":
            watermark = self.commit_store.get_watermark(scope_id, business_date)
            cursor = watermark if watermark is not None else 0
        else:
            cursor = self._shadow_cursor.get((scope_id, business_date), 0)
        # W-A 族④(c)-4（A2-08 轮级收容，与单飞即停状态机同哲学）：扫描
        # 故障不当轮崩溃——committed/shadowed 恒空（scan 在提交循环前）、
        # scan_error 留痕、游标未推进（下轮自动重扫，宁滞留不误扫）。
        try:
            docs = self.scanner.scan(scope_id, business_date, after_seq=cursor)
        except RegistrationScanError as error:
            return DriveReport(mode=self.mode, scope_id=scope_id,
                               business_date=business_date,
                               scan_error=str(error)[:300])
        # 有序提交（⑥）：升序单飞——扫描面排序之外，组合根自查排序一次
        docs = sorted(docs, key=lambda doc: (int(doc["arrival_seq"]),
                                             str(doc["record_id"])))
        committed: list[str] = []
        shadowed: list[str] = []
        stale: list[str] = []
        awaiting: list[str] = []
        failed: list[str] = []
        budget_exhausted: list[str] = []
        for doc in docs:
            outcome = self._drive_one(scope_id, business_date, doc, **budget_kw)
            record_id = doc["record_id"]
            if outcome == "committed":
                committed.append(record_id)
            elif outcome == "shadowed":
                shadowed.append(record_id)
            elif outcome == "stale":
                stale.append(record_id)
                break                      # 单飞边界：重试耗尽即停，不越过
            elif outcome == "awaiting":
                awaiting.append(record_id)
                break                      # 前沿未满：后续升序条同样阻塞
            elif outcome == "budget_exhausted":
                # W2 ⑩①-2（T082）：预算总闸到期/不可证——不启动（零模型
                # 调用零提交），单飞即停同 stale 族，升序链后续同断。
                budget_exhausted.append(record_id)
                break
            else:
                failed.append(record_id)
                break                      # 终态未确认：不推进不投递即停
        return DriveReport(mode=self.mode, scope_id=scope_id,
                           business_date=business_date,
                           committed=tuple(committed), shadowed=tuple(shadowed),
                           stale=tuple(stale), awaiting=tuple(awaiting),
                           failed=tuple(failed),
                           budget_exhausted=tuple(budget_exhausted))

    def _drive_one(self, scope_id: str, business_date: str,
                   doc: Mapping, *,
                   budget: ProcessingBudget | None = None) -> str:
        # W2 ⑩①-2（N43 挂账清偿）：预算解析序——显式 budget 优先（挂账①
        # 重领形态）；否则 budget_config 在场→按 accepted_at 派生（墙钟
        # accepted_at→mono 平移：elapsed=max(0, clock()-accepted_wall)，
        # accepted_at_mono=mono()-elapsed——T082 排队时间不重置）；缺
        # accepted_at 键/解析失败→预算不可证 fail-closed 哨兵（不猜）。
        # budget_config is None 且显式 None → 不派生不传（现役逐字节）。
        if budget is None and self.budget_config is not None:
            accepted_raw = doc.get("accepted_at")
            try:
                accepted_wall = (parse_utc_iso(accepted_raw)
                                 if isinstance(accepted_raw, str) else None)
            except ValueError:
                accepted_wall = None
            if accepted_wall is None:
                budget = _UNPROVEN_BUDGET
            else:
                clock_mono = self.clock_mono
                elapsed = max(0.0, (self.clock() - accepted_wall).total_seconds())
                budget = ProcessingBudget.derive(
                    accepted_at_mono=clock_mono() - elapsed,
                    config=self.budget_config, clock_mono=clock_mono)
        # 总闸执法点（prepared 读前）：到期/不可证→不启动模型调用与提交。
        if budget is not None and budget.exhausted():
            return "budget_exhausted"
        budget_kw: dict[str, Any] = {"budget": budget} if budget is not None else {}
        arrival_seq = int(doc["arrival_seq"])
        prepared = self.watermark_provider.prepared_seq(scope_id, business_date)
        if prepared < arrival_seq - 1:
            return "awaiting"              # 准备前沿未满：等下一轮（不判 STALE）
        visible = self.watermark_provider.visible_seq(scope_id, business_date)
        request = RecallRequest(
            scope_id, business_date, doc["record_id"], doc["item_id"],
            arrival_seq, doc["text"], visible_seq=visible,
            prepared_seq=prepared,
            embedding_space_id=doc.get("embedding_space_id"))
        plan = self.recall_service.build_plan(request, **budget_kw)
        current, candidates, coverage, expires_at = \
            self.recall_service.build_commit_inputs(plan, doc)
        if self.mode == "shadow":
            return self._shadow_one(scope_id, business_date, plan, request,
                                    current, candidates, coverage)
        return self._live_one(scope_id, business_date, plan, request, doc,
                              current, candidates, coverage, expires_at,
                              **budget_kw)

    # ---------- 影子腿（零仓储调用） ----------

    def _shadow_one(self, scope_id: str, business_date: str,
                    plan: RecallPlan, request: RecallRequest,
                    current: Mapping, candidates: tuple, coverage: bool) -> str:
        outcome = shadow_decide(
            current, candidates, coverage_complete=coverage,
            visible_seq=request.visible_seq, prepared_seq=request.prepared_seq,
            pipeline_version=self.pipeline_version,
            dictionary=self.dictionary,
            dictionary_version=self.dictionary_version,
            judge_version_config=self.judge_version_config,
            llm_facts=self.llm_facts)
        generated_at = _aware_iso(self.clock())
        self.artifact_sink.emit(_PLAN_ARTIFACT_KIND,
                                plan_artifact(plan, request))
        self.artifact_sink.emit(_SHADOW_ARTIFACT_KIND,
                                shadow_decision_artifact(
                                    outcome, plan=plan, request=request,
                                    generated_at=generated_at))
        self._shadow_cursor[(scope_id, business_date)] = request.arrival_seq
        return "shadowed"

    # ---------- 生效腿（STALE 有界重试状态机） ----------

    def _live_one(self, scope_id: str, business_date: str,
                  plan: RecallPlan, request: RecallRequest, doc: Mapping,
                  current: Mapping, candidates: tuple, coverage: bool,
                  expires_at: Any, *,
                  budget: ProcessingBudget | None = None) -> str:
        # W2 ⑩①-2：budget 随行传播（commit_one/prepare/build_plan 三点；
        # None 零传参=现役逐字节）；重试环持同一实例重入——环内不重建，
        # 结构性保证"STALE 重试预算不重置"（runtime_budget L12-14 同源）。
        budget_kw: dict[str, Any] = {"budget": budget} if budget is not None else {}
        for attempt in range(self.max_stale_retries + 1):
            ctx = CommitContext(
                scope_id=scope_id, business_date=business_date,
                arrival_seq=request.arrival_seq, current=current,
                candidates=candidates, visible_seq=request.visible_seq,
                prepared_seq=request.prepared_seq,
                pipeline_version=self.pipeline_version,
                coverage_complete=coverage)
            try:
                outcome: CommitOutcome = commit_one(
                    ctx, self.commit_store, audit_complete=True,
                    coverage_complete=coverage, expires_at=expires_at,
                    dictionary=self.dictionary,
                    dictionary_version=self.dictionary_version,
                    judge_version_config=self.judge_version_config,
                    llm_facts=self.llm_facts,
                    **budget_kw)
            except CommitOneError:
                return "failed"            # 终态未确认：不推进不投递
            if outcome.state == "committed":
                return "committed"
            # STALE→重读水位→重驱准备→重建计划→有界重试
            if attempt >= self.max_stale_retries:
                return "stale"
            self.prepare_worker.prepare(scope_id, business_date, **budget_kw)
            prepared = self.watermark_provider.prepared_seq(
                scope_id, business_date)
            visible = self.watermark_provider.visible_seq(
                scope_id, business_date)
            if prepared < request.arrival_seq - 1:
                return "awaiting"
            request = RecallRequest(
                scope_id, business_date, doc["record_id"], doc["item_id"],
                request.arrival_seq, doc["text"], visible_seq=visible,
                prepared_seq=prepared,
                embedding_space_id=doc.get("embedding_space_id"))
            plan = self.recall_service.build_plan(request, **budget_kw)
            current, candidates, coverage, expires_at = \
                self.recall_service.build_commit_inputs(plan, doc)
        # W2 修复波 2 (a)-8（A2-09）：原尾部 `return "stale"` 死代码删除——
        # 环内全径返回（committed/stale/awaiting/failed 四径+末迭代
        # attempt>=max_stale_retries 先判），尾部结构性不可达；stale 语义
        # 产出自环内有界重试（test_wave2_worker_stale_pin 钉）。


# ---------- 组合根工厂 ----------


def worker_from_environment(env: Mapping[str, str] | None = None,
                            **deps) -> DedupWorker:
    """① 模式闸解析落点：读 DEDUP_RECALL_MODE（service.mode_from_environment
    单源），装配 DedupWorker；未知值 RecallModeInvalid 冒泡（不启动）。"""
    mode = mode_from_environment(env)
    return DedupWorker(mode=mode, **deps)


__all__ = [
    "DedupWorker",
    "DriveReport",
    "ElasticsearchRegistrationScanner",
    "plan_artifact",
    "shadow_decide",
    "shadow_decision_artifact",
    "worker_from_environment",
]
