# -*- coding: utf-8 -*-
"""P0-T8c 红测：千件本地加速回放（Fake ES + 故障注入；零真 LLM/ES/批次跑）。

主窗令（T8 ③）：1000 件本地加速回放——P0 全故障谱一次收口：
- Scenario A（断连→熔断）：受理照常持久化、熔断开路期零 ES 触达、
  窗尽+期满半开探测恢复；
- Scenario B（丢失回执）：写入真实成功 409 → 读回对拍自愈（零二次物化）；
- Scenario C（纯死+迟到成功）：item_permanent 入墓（1 坏件不杀整批），
  迟到主记录落库 → 墓碑权威补正翻 tombstoned；
- 全前沿：1000 件跨 3 墓洞（100/500/900）——materialized/prepared/
  lexical/vector 四水位全达 1000；召回面 997；恢复账务恒等式平衡；
  重启一致（全新协调器/准备器/推进器/对账器零漂移）。

加速=注入时钟（FakeClock）驱动断连/熔断窗，墙钟零等待；全部本地
内存件（FakeESClient + MemoryStore），无网络无模型无真实批次系统。
"""
from __future__ import annotations

from datetime import datetime, timezone

from news_flash_dedup.admission import (
    AdmissionUnknown, CONTROL_INDEX, REQUEST_INDEX,
)
from news_flash_dedup.batch_admission import (
    BatchAdmissionCoordinator, BatchLimits, HEAD_ID,
)
from news_flash_dedup.batch_es_store import ElasticsearchBatchStore
from news_flash_dedup.es_admission_schema import day_index
from news_flash_dedup.materialize_runtime import (
    MaterializeDeferred, MaterializeRetryPolicy, MaterializeRetryRuntime,
)
from news_flash_dedup.materialize_terminal import (
    reconcile_late_materialization,
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
from test_p0_t7_faults_metrics import FakeClock

PREFIX = "p01-batch-t8replay-"
DAY = "2026-09-24"
SCOPE = "default"
ITEMS = day_index(DAY)
CONTROL = PREFIX + CONTROL_INDEX
TOTAL = 1000
DEADS = (100, 500, 900)          # 纯死×2 + 迟到成功×1（900）
UNKNOWN_ACK = 200                # 丢失回执（写入真实成功 → 读回自愈）
DAY_KEY = f"day:{SCOPE}:{DAY}"
NOW = datetime(2026, 9, 24, 0, 0, tzinfo=timezone.utc)


def _clock() -> datetime:
    return NOW


def _terminal_port(client: FakeESClient):
    """⑧同一终态证据源端口：各前沿共读同一墓簿。"""
    def search(index, body):
        return client.search(index=PREFIX + index, body=body)

    return lambda scope_id, business_date: scan_terminal_tombstone_seqs(
        search, scope_id, business_date)


def _head(base: ElasticsearchBatchStore) -> dict:
    return base.get(CONTROL_INDEX, HEAD_ID)["source"]


def _vsnapshot(store: MemoryStore) -> dict:
    return store.get(CONTROL_INDEX, DAY_KEY)["source"]["checkpoint"][
        "vector_prepared"]


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


def test_replay_1000_items_accelerated_local_full_fault_spectrum():
    """千件回放：断连熔断恢复+丢失回执自愈+纯死入墓+迟到补正，四水位
    全达 1000、账务平衡、重启一致——本地加速（注入时钟，零墙钟等待）。"""
    clock = FakeClock(0.0)
    policy = MaterializeRetryPolicy(
        active_attempt_limit=5, breaker_window_requests=6,
        breaker_min_failures=3, breaker_failure_ratio=0.5,
        breaker_open_seconds=30.0, breaker_half_open_probes=1,
    )
    runtime = MaterializeRetryRuntime(
        policy=policy, clock=clock, random_source=lambda: 0.0)
    client = FakeESClient()
    base = ElasticsearchBatchStore(client, index_prefix=PREFIX)
    injector = RecoveryFaultInjector(base, clock=clock)
    service = BatchAdmissionCoordinator(
        injector, owner_id="writer-1", owner_isolated=lambda: True,
        limits=BatchLimits(), clock=lambda: NOW, retry_runtime=runtime,
    )

    # ---- 故障谱装配（波次启动前：断连窗从首次 bulk 起算）。----
    injector.arm_disconnect(30.0)                       # Scenario A：30s 断连窗
    injector.arm_permanent_death([100])                 # Scenario C：混合形态
    injector.arm_permanent_death([500], main_record=False)   # 纯死形态
    injector.arm_late_success([900])                    # Scenario C：迟到成功×1
    injector.arm_unknown_write([UNKNOWN_ACK], written=True)   # Scenario B

    # ---- 受理+物化面：批节奏波次驱动（125 批×8；每 4 批=32 件排水一轮，
    # 与 MATERIALIZE_PREFIX_ITEMS=32 对齐——持久日志恒 ≤512 件纪律下回放）。
    # 断连窗内传输退避、熔断开路期跳窗（注入时钟推进，零墙钟等待）。----
    receipts = []
    allocated = 0
    wave_pending = 0
    for n in range(1, 126):
        receipts.extend(service.accept_batch([
            request(request_id=f"rp-{n}-{k}", item_id=f"rp-{n}-{k}")
            for k in range(1, 9)]))
        allocated += 8
        wave_pending += 1
        if wave_pending < 4 and n != 125:
            continue
        wave_pending = 0
        for _drain in range(200):
            try:
                if service.materialize_prefix() >= allocated:
                    break
            except MaterializeDeferred:            # 熔断开路期（零 ES 触达）
                clock.advance(31.0)
            except AdmissionUnknown:                # 断连窗内传输退避
                clock.advance(2.0)
    head = _head(base)
    assert (head["last_allocated_seq"],
            head["last_materialized_seq"]) == (TOTAL, TOTAL)
    assert head["pending"]["batches"] == []
    assert receipts[TOTAL - 1].arrival_seq == TOTAL
    dead_receipts = {seq: receipts[seq - 1] for seq in DEADS}
    # Scenario A 证据：熔断开过（≥1）、断连窗内传输级拒 ≥3、恢复后闭合。
    assert runtime.breaker.open_count >= 1
    assert runtime.breaker.state == "closed"
    assert injector.calls["disconnect_refused"] >= 3
    # Scenario B 证据：丢失回执自愈（item 登记真实在场，读回对拍收口）。
    assert base.get(REQUEST_INDEX,
                    "item:" + receipts[UNKNOWN_ACK - 1].record_id) is not None
    # Scenario C 证据：3 座墓碑（1 坏件不杀整批），锚定真实受理条目。
    entries = scan_terminal_tombstones(
        lambda index, body: client.search(index=PREFIX + index, body=body),
        SCOPE, DAY)
    assert [t.arrival_seq for t in entries] == list(DEADS)
    assert all(t.error_class == "item_permanent" for t in entries)
    assert {t.record_id for t in entries} == {
        r.record_id for r in dead_receipts.values()}
    assert injector.calls["item_faults"] == 3
    # 死亡三形态收口：混合形态主记录已落且批内补正翻 tombstoned；纯死者
    # 主记录未落；迟到者投递后补正（墓碑权威翻 tombstoned）。
    assert base.get(ITEMS, dead_receipts[100].record_id)[
        "source"]["task_state"] == "tombstoned"
    assert base.get(ITEMS, dead_receipts[500].record_id) is None
    assert injector.deliver_late_success(900) is True
    landed = base.get(ITEMS, dead_receipts[900].record_id)
    assert landed["source"]["task_state"] == "accepted"
    tomb_900 = next(t for t in entries if t.arrival_seq == 900)
    assert reconcile_late_materialization(
        base, tomb_900, main_index=ITEMS) == "corrected"
    assert base.get(ITEMS, dead_receipts[900].record_id)[
        "source"]["task_state"] == "tombstoned"

    # ---- 准备/词法水位面：997 件真实准备 → 前沿跨 3 墓洞达 1000。----
    report = PrepareWorker(client, PREFIX, clock=_clock).prepare(SCOPE, DAY)
    assert len(report.prepared) == TOTAL - len(DEADS)
    assert report.conflicts == ()
    assert (report.prepared_seq, report.lexical_watermark) == (TOTAL, TOTAL)
    provider = ElasticsearchWatermarkProvider(client, PREFIX)
    assert provider.visible_seq(SCOPE, DAY) == TOTAL
    assert provider.prepared_seq(SCOPE, DAY) == TOTAL
    prepared_snapshot = base.get(CONTROL_INDEX, DAY_KEY)["source"][
        "checkpoint"]["recall_prepared"]
    assert prepared_snapshot["prepared_seq"] == TOTAL
    assert prepared_snapshot["tombstone_proof_count"] == len(DEADS)
    assert prepared_snapshot["foreign_proof_count"] == 0

    # ---- 向量前沿面：批节奏分块写确认 → 跨 3 墓洞达 1000。----
    vstore = MemoryStore()
    advancer = VectorFrontierAdvancer(
        vstore, clock=_clock, terminal_evidence=_terminal_port(client))
    for start in range(0, TOTAL, 8):
        chunk = [seq for seq in range(start + 1, min(start + 8, TOTAL) + 1)
                 if seq not in DEADS]
        advancer.advance(SCOPE, DAY, "space-x", chunk)
    snapshot = _vsnapshot(vstore)
    assert snapshot["vector_frontier"] == TOTAL
    assert snapshot["tombstone_proof_count"] == len(DEADS)
    assert snapshot["hole_count"] == 0

    # ---- 召回面：3 墓态件不进召回（纯死缺席+墓态翻 tombstoned）。----
    ids = _recall_candidate_ids(client)
    assert len(ids) == TOTAL - len(DEADS)
    assert all(dead_receipts[seq].record_id not in ids for seq in DEADS)

    # ---- 对账面：墓证序号跳过且不 record_hole。----
    holes: list[int] = []

    def row_for_seq(seq):
        if seq in DEADS:                    # 纯死无行；墓态行=补正后形态
            return None, {"task_state": "tombstoned"}
        return object(), {"task_state": "accepted"}

    reconcile_report = VectorReconciler(
        vstore, clock=_clock, terminal_evidence=_terminal_port(client)).reconcile(
            SCOPE, DAY, "space-x", row_for_seq=row_for_seq,
            journal_confirmed=lambda seq, row, authority: True,
            record_hole=holes.append)
    assert reconcile_report.reconciled_through == TOTAL
    assert reconcile_report.holes_opened == 0
    assert reconcile_report.foreign_proofs == 0
    assert reconcile_report.tombstone_proofs == len(DEADS)
    assert holes == []

    # ---- 恢复账务面：恒等式 + 墓洞分计 + 死亡分布。----
    metrics = recovery_metrics(head, entries)
    assert (metrics["accepted"], metrics["ready"],
            metrics["tombstone_count"],
            metrics["still_processing"]) == (TOTAL, TOTAL - len(DEADS),
                                             len(DEADS), 0)
    assert metrics["accounting_balanced"] is True
    assert metrics["watermark_gap_skips"] == {
        "count": len(DEADS), "seqs": list(DEADS),
        "tombstone": len(DEADS), "foreign": 0}
    assert metrics["death_distribution"]["by_error_code"] == {"http_400": 3}

    # ---- 重启一致（四面前沿不回退零漂移）。----
    resumed = BatchAdmissionCoordinator(
        base, owner_id="writer-1", owner_isolated=lambda: True,
        limits=BatchLimits(), clock=lambda: NOW)
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
    fresh_reconcile = VectorReconciler(
        vstore, clock=_clock, terminal_evidence=_terminal_port(client)).reconcile(
            SCOPE, DAY, "space-x", row_for_seq=row_for_seq,
            journal_confirmed=lambda seq, row, authority: True,
            record_hole=holes.append)
    assert fresh_reconcile.reconciled_through == TOTAL
    assert fresh_reconcile.holes_opened == 0
    assert holes == []
    assert len(_recall_candidate_ids(client)) == TOTAL - len(DEADS)
