"""B+ 并行预检模式·worker 适配层（2026-10-08 主窗口收官件）。

设计 = log\temp\batch-commit-impl-contract.md「接线设计」。本模块把 B+
全部内核拼成一轮并行驱动（worker 新模式调用；串行 live 路径零改动）：

  喂池（并发相位）：prepare → 水位游标 → 扫描 → 升序逐件过闸门
    （prepared ≥ seq−1，现役 _drive_one 同语义）→ PrecheckPool.submit
    （池内=build_plan→build_commit_inputs→decide_for_task 草稿）。
  定稿（保序相位）：drain_one 按到达序 → 提交时刻新鲜水位重建请求 →
    build_plan 重构 → finalize_decision（差集/补算/重聚合，内核①②）
    → CommitContext（新鲜水位）→ build_commit_write_plan（与串行
    commit_one 同源单源）→ BatchCommitItem。
  落锤（批量相位）：连续前缀 ≤ batch_size 成批 → commit_batch 四段
    （内核=es_store 批量三函数+驱动器）；RuntimeError=整段失败本轮
    报败下轮重放（幂等收口）；CommitOneError=该件 failed 即停（现役
    单飞边界同款：不推进不投递）。

freshness 口径：定稿相位本身即新鲜机制（提交时刻重构），串行 STALE
有界重试在并行版由"重构+批量驱动器水位/CAS 预检"承载——首轮简化，
P6/P7 对拍验收钉。

v1 边界：受理侧 task_state 翻归与投递联动=最终接线段（组合根）职责，
本模块产出 DriveReport 同款结果映射。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from news_flash_dedup.commit.batch_driver import (
    BatchCommitItem,
    commit_batch,
)
from news_flash_dedup.commit.coordinator import (
    CommitContext,
    CommitOneError,
    build_commit_write_plan,
)
from news_flash_dedup.decide.pair_cache import PairResultCache
from news_flash_dedup.persist.es_store import _record_to_body
from news_flash_dedup.recall.parallel_driver import (
    PrecheckPacket,
    finalize_decision,
)
from news_flash_dedup.recall.precheck_pool import PrecheckPool
from news_flash_dedup.recall.reconstruct import reconstruct


@dataclass(frozen=True)
class ParallelRoundReport:
    """一轮并行驱动结果（DriveReport 同款桶）。"""
    committed: tuple[str, ...]
    failed: tuple[str, ...]
    awaiting: tuple[str, ...]
    deferred: tuple[str, ...]           # 断裂件（水位未覆盖，留下批）
    watermark_advanced_to: int | None
    recomputed: tuple[str, ...]         # 发生提交时刻重聚合的件（观测面）


def _precheck_one(worker: "ParallelLiveDriver", scope_id: str,
                  business_date: str, doc: Mapping) -> PrecheckPacket:
    """池内相位（并发安全：全部只读+纯函数判定）。"""
    arrival_seq = int(doc["arrival_seq"])
    prepared = worker.watermark_provider.prepared_seq(scope_id, business_date)
    visible = worker.watermark_provider.visible_seq(scope_id, business_date)
    request = worker.request_factory(
        scope_id, business_date, doc["record_id"], doc["item_id"],
        arrival_seq, doc["text"], visible_seq=visible,
        prepared_seq=prepared,
        embedding_space_id=doc.get("embedding_space_id"))
    plan = worker.recall_service.build_plan(request)
    current, candidates, coverage, _expires = \
        worker.recall_service.build_commit_inputs(plan, doc)
    if candidates:
        history, remaining = candidates[-1], candidates[:-1]
        decide = worker.decide_for_task(
            history=history, candidates=remaining, current=current,
            pipeline_version=worker.pipeline_version,
            coverage_complete=coverage, visible_seq=visible,
            prepared_seq=prepared, dictionary=worker.dictionary,
            dictionary_version=worker.dictionary_version)
    else:
        decide = worker.decide_for_task(
            history=current, candidates=(), current=current,
            pipeline_version=worker.pipeline_version,
            coverage_complete=coverage, visible_seq=visible,
            prepared_seq=prepared, dictionary=worker.dictionary,
            dictionary_version=worker.dictionary_version)
    return PrecheckPacket(
        plan=plan, request=request, current=current, candidates=candidates,
        coverage=coverage, decide=decide)


class ParallelLiveDriver:
    """并行预检模式驱动（依赖全注入，fake 可测）。"""

    def __init__(self, *, prepare_worker: Any, scanner: Any,
                 recall_service: Any, commit_store: Any,
                 watermark_provider: Any, decide_for_task: Any,
                 aggregate: Any, decide_pair: Any,
                 candidate_lookup: Any, frozen_plan_from: Any,
                 request_factory: Any,
                 pipeline_version: str = "dedup_v1",
                 dictionary: Any = None,
                 dictionary_version: str = "dict_v1",
                 version_chain: tuple[str, ...] = (),
                 pair_cache: PairResultCache | None = None,
                 workers: int = 4, batch_size: int = 16) -> None:
        if workers < 1 or batch_size < 1:
            raise ValueError("workers/batch_size must be >= 1")
        self.prepare_worker = prepare_worker
        self.scanner = scanner
        self.recall_service = recall_service
        self.commit_store = commit_store
        self.watermark_provider = watermark_provider
        self.decide_for_task = decide_for_task
        self.aggregate = aggregate
        self.decide_pair = decide_pair
        self.candidate_lookup = candidate_lookup
        self.frozen_plan_from = frozen_plan_from
        self.request_factory = request_factory
        self.pipeline_version = pipeline_version
        self.dictionary = dictionary
        self.dictionary_version = dictionary_version
        self.version_chain = version_chain
        self.pair_cache = pair_cache or PairResultCache()
        self.workers = workers
        self.batch_size = batch_size

    def drive_round(self, scope_id: str, business_date: str,
                    ) -> ParallelRoundReport:
        """一轮并行驱动（喂池→保序定稿→连续前缀批量落锤）。"""
        self.prepare_worker.prepare(scope_id, business_date)
        watermark = self.commit_store.get_watermark(scope_id, business_date)
        cursor = watermark if watermark is not None else 0
        docs = sorted(
            self.scanner.scan(scope_id, business_date, after_seq=cursor),
            key=lambda d: (int(d["arrival_seq"]), str(d["record_id"])))
        if not docs:
            return ParallelRoundReport(
                committed=(), failed=(), awaiting=(), deferred=(),
                watermark_advanced_to=None, recomputed=())

        # ---- 喂池（闸门同现役语义；前沿未满即停喂，余件留下轮）----
        pool = PrecheckPool(
            workers=self.workers,
            fn=lambda doc: _precheck_one(self, scope_id, business_date, doc))
        fed: list[Mapping] = []
        awaiting: list[str] = []
        for doc in docs:
            seq = int(doc["arrival_seq"])
            prepared = self.watermark_provider.prepared_seq(
                scope_id, business_date)
            if prepared < seq - 1:
                awaiting.append(doc["record_id"])
                break
            pool.submit(seq, doc)
            fed.append(doc)

        # ---- 保序定稿 + 收集连续前缀批量项 ----
        batch_items: list[BatchCommitItem] = []
        recomputed: list[str] = []
        failed: list[str] = []
        stop = False
        for doc in fed:
            item = None
            while item is None:
                item = pool.drain_one()
            if item.error is not None:
                failed.append(doc["record_id"])
                stop = True
                break
            packet: PrecheckPacket = item.result
            seq = int(doc["arrival_seq"])
            # 提交时刻新鲜水位重建（内核①：同一路径重构+差集）
            prepared = self.watermark_provider.prepared_seq(
                scope_id, business_date)
            visible = self.watermark_provider.visible_seq(
                scope_id, business_date)
            commit_request = self.request_factory(
                scope_id, business_date, doc["record_id"], doc["item_id"],
                seq, doc["text"], visible_seq=visible,
                prepared_seq=prepared,
                embedding_space_id=doc.get("embedding_space_id"))
            diff = reconstruct(
                self.recall_service, commit_request, packet.plan,
                precheck_request=packet.request)
            decision = finalize_decision(
                packet,
                commit_request=commit_request,
                fresh_plan=diff.fresh_plan,
                decide_pair=self.decide_pair,
                aggregate=self._aggregate_adapter,
                pair_cache=self.pair_cache,
                version_chain=self.version_chain,
                candidate_lookup=self.candidate_lookup)
            if decision.recomputed:
                recomputed.append(doc["record_id"])
            ctx = CommitContext(
                scope_id=scope_id, business_date=business_date,
                arrival_seq=seq, current=packet.current,
                candidates=packet.candidates, visible_seq=visible,
                prepared_seq=prepared,
                pipeline_version=self.pipeline_version,
                coverage_complete=self._coverage_of(decision))
            write_plan = build_commit_write_plan(
                ctx, decision.decide, audit_complete=True)
            batch_items.append(BatchCommitItem(
                record_id=doc["record_id"], arrival_seq=seq,
                audit_docs=tuple(record.to_doc()
                                 for record in
                                 write_plan.audit_batch.records),
                body=_record_to_body(
                    write_plan.main_record, scope_id=scope_id,
                    business_date=business_date, arrival_seq=seq)))
            if len(batch_items) >= self.batch_size:
                break
        pool.close()
        if stop:
            return ParallelRoundReport(
                committed=(), failed=tuple(failed), awaiting=tuple(awaiting),
                deferred=(), watermark_advanced_to=None,
                recomputed=tuple(recomputed))
        if not batch_items:
            return ParallelRoundReport(
                committed=(), failed=(), awaiting=tuple(awaiting),
                deferred=(), watermark_advanced_to=None,
                recomputed=tuple(recomputed))

        # ---- 批量落锤（四段；失败语义=现役单飞边界同款）----
        outcome = commit_batch(
            batch_items, self.commit_store,
            scope_id=scope_id, business_date=business_date)
        committed = list(outcome.flipped_ids) + list(outcome.idempotent_ids)
        return ParallelRoundReport(
            committed=tuple(committed), failed=(),
            awaiting=tuple(awaiting), deferred=outcome.deferred_ids,
            watermark_advanced_to=outcome.watermark_advanced_to,
            recomputed=tuple(recomputed))

    def _aggregate_adapter(self, current, frozen_plan, pair_results, *,
                           coverage_complete):
        return self.aggregate(
            current, self.frozen_plan_from(frozen_plan), pair_results,
            coverage_complete=coverage_complete)

    @staticmethod
    def _coverage_of(decision: Any) -> bool:
        plan = decision.diff.fresh_plan
        if plan.recall_incomplete:
            return False
        return all(getattr(a, "coverage_complete", True)
                   for a in getattr(plan, "channel_audits", ()))


__all__ = ["ParallelLiveDriver", "ParallelRoundReport"]
