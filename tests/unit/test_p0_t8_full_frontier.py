# -*- coding: utf-8 -*-
"""P0-T8b 红测：全前沿 200 件规模钉（真实受理→物化→准备→向量全链）。

主窗令（T8 ②）：seq 1~16 READY + 17 TOMBSTONE + 18~200 READY →
materialized/prepared/lexical/vector 四水位**全达 200**；17 不进召回
结果；无 record_hole(17) 永久阻塞；重启一致。

与域级前身（test_p0_tombstone_frontier.py 16/17/20 钉）的分立：
- 前身：种子置件（materialized=20 直接种入 head，preparation_state=ready
  直接种入主记录）——域内水位链钉；
- 本钉：走**真实受理物化链**（BatchAdmissionCoordinator + RecoveryFaultInjector
  纯死注入——T7 栈形态），200 件分 25 批受理、序 17 注入 item_permanent
  纯死 → materialized 水位是故障恢复**实跑**结果；prepare 面 199 件真实
  准备（派生字段真算）；vector 面批节奏分块写确认。四水位一次全量对齐，
  恢复账务恒等式（accepted == ready + tombstone + still_processing）。
"""
from __future__ import annotations

from datetime import datetime, timezone

from news_flash_dedup.admission import CONTROL_INDEX
from news_flash_dedup.batch_admission import (
    BatchAdmissionCoordinator, BatchLimits, HEAD_ID,
)
from news_flash_dedup.batch_es_store import ElasticsearchBatchStore
from news_flash_dedup.es_admission_schema import day_index
from news_flash_dedup.materialize_terminal import (
    scan_terminal_tombstone_seqs,
    scan_terminal_tombstones,
)
from news_flash_dedup.recall.prepare import (
    ElasticsearchWatermarkProvider,
    PrepareWorker,
)
from news_flash_dedup.recall.vector_frontier import (
    VectorFrontierAdvancer,
    VectorReconciler,
)
from news_flash_dedup.recovery_faults import RecoveryFaultInjector
from news_flash_dedup.recovery_metrics import recovery_metrics

from b4_fake_es import FakeESClient
from test_admission import MemoryStore, request

PREFIX = "p01-batch-t8front-"
DAY = "2026-09-24"
SCOPE = "default"
ITEMS = day_index(DAY)
CONTROL = PREFIX + CONTROL_INDEX
TOTAL = 200
DEAD = 17
DAY_KEY = f"day:{SCOPE}:{DAY}"
NOW = datetime(2026, 9, 24, 0, 0, tzinfo=timezone.utc)


def _clock() -> datetime:
    return NOW


def _stack() -> tuple[FakeESClient, ElasticsearchBatchStore, RecoveryFaultInjector]:
    client = FakeESClient()
    base = ElasticsearchBatchStore(client, index_prefix=PREFIX)
    injector = RecoveryFaultInjector(base)
    return client, base, injector


def _service(store) -> BatchAdmissionCoordinator:
    return BatchAdmissionCoordinator(
        store, owner_id="writer-1", owner_isolated=lambda: True,
        limits=BatchLimits(), clock=lambda: NOW,
    )


def _port(client: FakeESClient):
    """search 只读检索端口（物理前缀经端口包装；T7 _port 同形）。"""
    return lambda index, body: client.search(index=PREFIX + index, body=body)


def _terminal_port(client: FakeESClient):
    """⑧同一终态证据源端口：各前沿共读同一墓簿（物理前缀经端口包装）。"""
    return lambda scope_id, business_date: scan_terminal_tombstone_seqs(
        _port(client), scope_id, business_date)


def _head(base: ElasticsearchBatchStore) -> dict:
    return base.get(CONTROL_INDEX, HEAD_ID)["source"]


def _vsnapshot(store: MemoryStore) -> dict:
    day = store.get(CONTROL_INDEX, DAY_KEY)
    return day["source"]["checkpoint"]["vector_prepared"]


def _recall_candidate_ids(client: FakeESClient) -> list[str]:
    """召回登记扫描（recall/worker.py 扫描 filter 同形）候选集。"""
    hits = client.search(index=PREFIX + ITEMS, body={
        "size": TOTAL + 8,
        "query": {"bool": {"filter": [
            {"term": {"scope_id": SCOPE}},
            {"terms": {"task_state": ["accepted", "running"]}},
        ]}}},
    )
    return [hit["_id"] for hit in hits["hits"]["hits"]]


def test_full_frontier_200_all_watermarks_reach_tail_across_tombstone():
    """1~16/18~200 READY + 17 TOMBSTONE（纯死注入）：materialized/prepared/
    lexical/vector 四水位全达 200；17 不进召回；零 record_hole；重启一致。"""
    client, base, injector = _stack()
    service = _service(injector)

    # ---- 受理面：200 件分 25 批（8 件/批），全局序号 1..200。----
    receipts = []
    for n in range(1, 26):
        receipts.extend(service.accept_batch([
            request(request_id=f"t8-{n}-{k}", item_id=f"t8-{n}-{k}")
            for k in range(1, 9)]))
    dead = receipts[DEAD - 1]
    assert [r.arrival_seq for r in
            (receipts[0], dead, receipts[-1])] == [1, DEAD, TOTAL]
    injector.arm_permanent_death([DEAD], main_record=False)   # 纯死：主记录不落

    # ---- 物化面：前缀有界驱动（32 件/轮）直至水位跨墓洞达 200。----
    for _round in range(20):
        if service.materialize_prefix() >= TOTAL:
            break
    head = _head(base)
    assert (head["last_allocated_seq"],
            head["last_materialized_seq"]) == (TOTAL, TOTAL)
    assert head["pending"]["batches"] == []
    assert base.get(ITEMS, dead.record_id) is None     # 纯死：17 主记录未落
    entries = scan_terminal_tombstones(_port(client), SCOPE, DAY)
    assert [t.arrival_seq for t in entries] == [DEAD]
    assert entries[0].record_id == dead.record_id      # 墓证锚定真实受理条目
    assert entries[0].error_class == "item_permanent"

    # ---- 准备/词法水位面：199 件真实准备（派生字段真算，非种子）→ 达 200。----
    worker = PrepareWorker(client, PREFIX, clock=_clock)
    report = worker.prepare(SCOPE, DAY)
    assert len(report.prepared) == TOTAL - 1
    assert report.conflicts == ()
    assert (report.prepared_seq, report.lexical_watermark) == (TOTAL, TOTAL)
    provider = ElasticsearchWatermarkProvider(client, PREFIX)
    assert provider.visible_seq(SCOPE, DAY) == TOTAL
    assert provider.prepared_seq(SCOPE, DAY) == TOTAL
    prepared_snapshot = base.get(CONTROL_INDEX, DAY_KEY)["source"][
        "checkpoint"]["recall_prepared"]
    assert prepared_snapshot["prepared_seq"] == TOTAL
    assert prepared_snapshot["tombstone_proof_count"] == 1
    assert prepared_snapshot["foreign_proof_count"] == 0

    # ---- 向量前沿面：批节奏分块写确认（25 块×8，17 结构性缺席）→ 达 200。----
    vstore = MemoryStore()
    advancer = VectorFrontierAdvancer(
        vstore, clock=_clock, terminal_evidence=_terminal_port(client))
    for start in range(0, TOTAL, 8):
        chunk = [seq for seq in range(start + 1, min(start + 8, TOTAL) + 1)
                 if seq != DEAD]
        advancer.advance(SCOPE, DAY, "space-x", chunk)
    snapshot = _vsnapshot(vstore)
    assert snapshot["vector_frontier"] == TOTAL
    assert snapshot["tombstone_proof_count"] == 1
    assert snapshot["hole_count"] == 0

    # ---- 召回面：墓态 17 不进召回结果（纯死=登记扫描结构性缺席）。----
    ids = _recall_candidate_ids(client)
    assert len(ids) == TOTAL - 1
    assert dead.record_id not in ids

    # ---- 对账面：墓证序号跳过且不 record_hole（无永久阻塞）。----
    holes: list[int] = []

    def row_for_seq(seq):
        if seq == DEAD:                     # 纯死无行；墓证先拦（不落孔洞）
            return None, None
        return object(), {"task_state": "accepted"}

    reconciler = VectorReconciler(
        vstore, clock=_clock, terminal_evidence=_terminal_port(client))
    reconcile_report = reconciler.reconcile(
        SCOPE, DAY, "space-x", row_for_seq=row_for_seq,
        journal_confirmed=lambda seq, row, authority: True,
        record_hole=holes.append)
    assert reconcile_report.reconciled_through == TOTAL
    assert reconcile_report.holes_opened == 0
    assert reconcile_report.foreign_proofs == 0
    assert reconcile_report.tombstone_proofs == 1
    assert holes == []                      # 17 不落孔洞

    # ---- 恢复账务面：恒等式 + 墓洞分计。----
    metrics = recovery_metrics(head, entries)
    assert (metrics["accepted"], metrics["ready"],
            metrics["tombstone_count"],
            metrics["still_processing"]) == (TOTAL, TOTAL - 1, 1, 0)
    assert metrics["accounting_balanced"] is True
    assert metrics["watermark_gap_skips"] == {
        "count": 1, "seqs": [DEAD], "tombstone": 1, "foreign": 0}

    # ---- 重启一致（四面前沿不回退零漂移）。----
    resumed = _service(base)                # 全新协调器（无故障钩子）
    assert resumed.takeover() == TOTAL
    fresh_prepare = PrepareWorker(
        client, PREFIX, clock=_clock).prepare(SCOPE, DAY)
    assert (fresh_prepare.prepared_seq,
            fresh_prepare.lexical_watermark) == (TOTAL, TOTAL)
    assert fresh_prepare.prepared == ()
    fresh_advancer = VectorFrontierAdvancer(
        vstore, clock=_clock, terminal_evidence=_terminal_port(client))
    assert fresh_advancer.advance(
        SCOPE, DAY, "space-x", []).vector_frontier == TOTAL
    fresh_reconciler = VectorReconciler(
        vstore, clock=_clock, terminal_evidence=_terminal_port(client))
    second = fresh_reconciler.reconcile(
        SCOPE, DAY, "space-x", row_for_seq=row_for_seq,
        journal_confirmed=lambda seq, row, authority: True,
        record_hole=holes.append)
    assert second.reconciled_through == TOTAL
    assert second.holes_opened == 0
    assert holes == []                      # 重启后仍零孔洞
    assert _recall_candidate_ids(client) == ids    # 召回面零漂移
