# -*- coding: utf-8 -*-
"""P0-T5 红测：墓碑全前沿链（主窗口扩充令——墓簿=全前沿统一终态证据源）。

出处=主窗口第二条指令（墓碑全前沿链并入 T5/T8）：
- ① 物化+读回确认后 materialize 水位跨洞（物化面钉在
  test_batch_admission.py T4/T5 组；本文件钉 prepare/lexical/vector 面）；
- ② prepare 前沿视墓碑为终态已处理（非未解释孔洞）——additive，
  foreign-day 语义零触碰（`test_frontier_function_legacy_zero_diff…` 守卫）；
- ③ 墓态件不进召回候选（登记扫描词表结构性缺席，扫描面直钉）；
- ④⑤ 墓碑不要求 embedding；vector 前沿不因缺向量永久停滞（墓证=合法
  跳洞凭据；对账区间墓证序号跳过且**不 record_hole**）；
- ⑥ 重启自墓簿恢复续推（全新实例重导同一前沿，幂等零写）；
- ⑧ 各前沿共读**同一**终态证据源（materialize_terminal.scan_terminal_
  tombstone_seqs 共用读面）——墓碑不伪装成新闻记录（无 items 假体）。

16/17/20 域级全链钉= T8 千件钉（1~16 READY+17 TOMBSTONE+18~200 READY）的
域内前身；既有钉 test_r7_frontier_never_skips_holes（无凭证明场景）继续
绿=零 diff 证据。
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from news_flash_dedup.admission import CONTROL_INDEX
from news_flash_dedup.batch_admission import HEAD_ID
from news_flash_dedup.batch_es_store import ElasticsearchBatchStore
from news_flash_dedup.es_admission_schema import day_index
from news_flash_dedup.materialize_terminal import (
    MaterializationTombstoneV1,
    persist_tombstone,
    scan_terminal_tombstone_seqs,
    tombstone_index,
)
from news_flash_dedup.recall.prepare import (
    ElasticsearchWatermarkProvider,
    PrepareWorker,
    advance_prepared_frontier,
)
from news_flash_dedup.recall.vector_frontier import (
    VectorFrontierAdvancer,
    VectorReconciler,
)

from b4_fake_es import FakeESClient
from test_admission import MemoryStore

DAY = "2026-09-29"
PREFIX = "p01-batch-t5front-"
SCOPE = "default"
ITEMS = PREFIX + day_index(DAY)
CONTROL = PREFIX + "news-dedup-control-v1"
DAY_KEY = f"day:{SCOPE}:{DAY}"
NOW = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)
TEXT = "美国能源信息署公布原油库存增加。"
EXPIRY = "2026-10-06T00:00:00.000000Z"


def _clock() -> datetime:
    return NOW


def _rid(seq: int) -> str:
    # 墓碑 schema 要求 record_id 严格 64hex——items/tombstone 共用同 id
    # （场景 C 补正身份对拍同锚）。
    return f"{seq:064x}"


def _tombstone(seq: int, *, scope_id: str = SCOPE,
               business_date: str = DAY) -> MaterializationTombstoneV1:
    return MaterializationTombstoneV1(
        scope_id=scope_id, business_date=business_date, arrival_seq=seq,
        record_id=_rid(seq), item_id=f"9-{seq}",
        raw_hash=f"{seq + 1:064x}",
        failed_stage="materialize_bulk", error_class="item_permanent",
        error_code="http_400", error_summary="synthetic item permanent rejection",
        attempt_count=1, first_failed_at="2026-09-29T01:00:00.000000Z",
        last_failed_at="2026-09-29T01:00:00.000000Z", retryable=False,
        pipeline_version="dedup_v1", recorded_at="2026-09-29T01:00:00.000000Z",
    )


def _doc(seq: int, *, task_state: str = "accepted",
         preparation_state: str | None = "ready") -> dict:
    doc = {
        "scope_id": SCOPE, "request_id": f"9-{seq}", "item_id": f"9-{seq}",
        "record_id": _rid(seq), "schema_version": "v1",
        "pipeline_version": "dedup_v1", "embedding_space_id": "space-x",
        "business_date": DAY, "arrival_seq": seq,
        "received_at": "2026-09-29T01:00:00.000000Z",
        "accepted_at": "2026-09-29T01:00:00.000000Z",
        "expires_at": EXPIRY, "text": TEXT, "raw_hash": "0" * 64,
        "task_state": task_state, "delivery_state": "not_ready", "result": None,
    }
    if preparation_state is not None:
        doc["preparation_state"] = preparation_state
    return doc


def _seed_client(*docs: dict, materialized: int = 0,
                 tombstones=()) -> FakeESClient:
    client = FakeESClient()
    for doc in docs:
        client.put(ITEMS, doc["record_id"], doc)
    client.put(CONTROL, HEAD_ID, {
        "kind": "head", "scope_id": SCOPE, "owner_id": "owner-1",
        "last_allocated_seq": materialized,
        "last_materialized_seq": materialized,
        "pending": {"version": "batch-v1", "batches": []},
        "updated_at": "2026-09-29T01:00:00.000000Z",
    })
    store = ElasticsearchBatchStore(client, index_prefix=PREFIX)
    for tombstone in tombstones:
        persist_tombstone(store, tombstone)
    return client


def _terminal_port(client: FakeESClient):
    """⑧同一终态证据源端口：各前沿共读同一墓簿（物理前缀经端口包装）。"""
    return lambda scope_id, business_date: scan_terminal_tombstone_seqs(
        lambda index, body: client.search(index=PREFIX + index, body=body),
        scope_id, business_date)


def _recall_candidate_ids(client: FakeESClient) -> list[str]:
    """召回登记扫描（recall/worker.py:253 同形 filter）候选集。"""
    hits = client.search(index=ITEMS, body={"query": {"bool": {"filter": [
        {"term": {"scope_id": SCOPE}},
        {"terms": {"task_state": ["accepted", "running"]}},
    ]}}})
    return [hit["_id"] for hit in hits["hits"]["hits"]]


def _vsnapshot(store: MemoryStore) -> dict:
    day = store.get(CONTROL_INDEX, DAY_KEY)
    return day["source"]["checkpoint"]["vector_prepared"]


# ---------- ②前沿纯函数：terminal=墓证跳洞凭据（additive） ----------

def test_frontier_function_terminal_skips_tombstone_hole():
    # 墓洞（本域本日终态）与外国洞同为合法跳洞凭据；两源可并用。
    assert advance_prepared_frontier(0, {1, 2, 4}, terminal={3}) == 4
    assert advance_prepared_frontier(2, {1, 2, 4}, terminal={3}) == 4
    assert advance_prepared_frontier(0, {1, 3, 5}, foreign={2}, terminal={4}) == 5
    assert advance_prepared_frontier(0, {1, 3}, terminal={2}) == 3


def test_frontier_function_unproven_hole_still_blocks():
    # 墓证只证已证序号：4 未证 → 停在 3；无关墓证（99）不放大跳洞。
    assert advance_prepared_frontier(0, {1, 3, 5}, terminal={2}) == 3
    assert advance_prepared_frontier(0, {1, 2, 4}, terminal={99}) == 2


def test_frontier_function_legacy_zero_diff_and_monotonic():
    # 两参均 None=legacy 逐字节（无凭证明场景，既有 R-7 钉兼容零改动）；
    # terminal 空集与 None 同效（缺号不越过）；单调不回退。
    assert advance_prepared_frontier(0, {1, 2, 4}) == 2
    assert advance_prepared_frontier(0, {2, 3}) == 0
    assert advance_prepared_frontier(0, {1, 2, 4}, terminal=set()) == 2
    assert advance_prepared_frontier(7, {1, 2}, terminal={8}) == 7


# ---------- ⑧墓簿共读面：scan_terminal_tombstone_seqs ----------

def test_scan_terminal_enumerates_scope_day_tombstones():
    client = FakeESClient()
    store = ElasticsearchBatchStore(client, index_prefix=PREFIX)
    persist_tombstone(store, _tombstone(3))
    persist_tombstone(store, _tombstone(7))
    persist_tombstone(store, _tombstone(5, scope_id="other"))      # 他域
    persist_tombstone(store, _tombstone(9, business_date="2026-09-30"))
    seqs = scan_terminal_tombstone_seqs(
        lambda index, body: client.search(index=PREFIX + index, body=body),
        SCOPE, DAY)
    assert seqs == {3, 7}            # 他域/他日墓证不混入本域日证据集


class _StatusError(RuntimeError):
    def __init__(self, status: int) -> None:
        super().__init__(f"http {status}")
        self.status_code = status


def test_scan_terminal_absent_index_empty_other_errors_raise():
    # 404 形态=无墓簿→空集（无证据≠证据缺失，fail-closed 不证不猜）；
    # 其他异常上抛（前沿停摆可见，不静默吞错）。
    def _raising(status: int):
        def search(index, body):
            raise _StatusError(status)
        return search

    assert scan_terminal_tombstone_seqs(_raising(404), SCOPE, DAY) == set()
    with pytest.raises(RuntimeError):
        scan_terminal_tombstone_seqs(_raising(503), SCOPE, DAY)


def test_scan_terminal_malformed_entries_are_not_proven():
    # 畸形条目（arrival_seq 非 int/越界/缺席/条目非映射）不计入证据
    # （不证）——端口直供回包，FakeESClient 排序面不介入。
    response = {"hits": {"hits": [
        {"_id": "a", "_source": {"scope_id": SCOPE, "arrival_seq": "x"}},
        {"_id": "b", "_source": {"scope_id": SCOPE, "arrival_seq": 11}},
        {"_id": "c", "_source": {"scope_id": SCOPE, "arrival_seq": -5}},
        {"_id": "d", "_source": {}},
        "not-a-mapping",
    ]}}
    seqs = scan_terminal_tombstone_seqs(
        lambda index, body: response, SCOPE, DAY)
    assert seqs == {11}


# ---------- ①②③⑥prepare 面 ----------

def test_prepare_frontier_crosses_tombstone_hole_with_audit():
    """①②：墓证=prepare 前沿合法跳洞凭据；lexical 跟随物化水位（T4 面）；
    tombstone_proof_count 审计位与 foreign 分计。"""
    docs = [_doc(seq) for seq in (1, 2, 3, 4, 5, 6, 8, 9, 10)]
    client = _seed_client(*docs, materialized=10, tombstones=[_tombstone(7)])
    worker = PrepareWorker(client, PREFIX, clock=_clock)
    report = worker.prepare(SCOPE, DAY)
    assert report.prepared_seq == 10
    assert report.lexical_watermark == 10
    provider = ElasticsearchWatermarkProvider(client, PREFIX)
    assert provider.prepared_seq(SCOPE, DAY) == 10
    assert provider.visible_seq(SCOPE, DAY) == 10
    snapshot = client.source(CONTROL, DAY_KEY)["checkpoint"]["recall_prepared"]
    assert snapshot["tombstone_proof_count"] == 1
    assert snapshot["foreign_proof_count"] == 0


def test_prepare_frontier_stalls_without_tombstone_proof():
    """无墓证=缺号不越过（fail-closed 对照面）：跳洞凭据恰在墓证供给，
    非默认跳洞——「水位永久停滞=0」由 T4 物化+T5 各前沿供给链闭合。"""
    docs = [_doc(seq) for seq in (1, 2, 3, 4, 5, 6, 8, 9, 10)]
    client = _seed_client(*docs, materialized=10)          # 无墓簿
    worker = PrepareWorker(client, PREFIX, clock=_clock)
    report = worker.prepare(SCOPE, DAY)
    assert report.prepared_seq == 6
    snapshot = client.source(CONTROL, DAY_KEY)["checkpoint"]["recall_prepared"]
    assert snapshot["tombstone_proof_count"] == 0


def test_prepare_frontier_mixed_tombstoned_main_record_is_terminal():
    """⑧混合形态：迟到主记录已翻 tombstoned——不进 prepare 扫描（零
    preparation_state 写入）、不进 ready 证据集，纯凭墓证跳洞（不伪装成
    ready 新闻记录）。"""
    mixed = _doc(7, task_state="tombstoned", preparation_state=None)
    docs = [_doc(seq) for seq in (1, 2, 3, 4, 5, 6, 8, 9, 10)] + [mixed]
    client = _seed_client(*docs, materialized=10, tombstones=[_tombstone(7)])
    worker = PrepareWorker(client, PREFIX, clock=_clock)
    report = worker.prepare(SCOPE, DAY)
    assert report.prepared == ()                 # 墓态件无可准备（扫描词表跳过）
    assert report.prepared_seq == 10
    after = client.source(ITEMS, mixed["record_id"])
    assert after["task_state"] == "tombstoned"
    assert "preparation_state" not in after      # 零准备写：不伪装成已处理件


def test_prepare_frontier_restart_recovers_from_tombstone_ledger():
    """⑥重启恢复：全新 worker 实例自墓簿+已存快照重导同一前沿；幂等轮
    零 CAS 写（日控制快照逐字节不动）。"""
    docs = [_doc(seq) for seq in (1, 2, 3, 4, 5, 6, 8, 9, 10)]
    client = _seed_client(*docs, materialized=10, tombstones=[_tombstone(7)])
    PrepareWorker(client, PREFIX, clock=_clock).prepare(SCOPE, DAY)
    before = client.source(CONTROL, DAY_KEY)
    writes_before = [c for c in client.calls if c[0] == "index"]
    second = PrepareWorker(client, PREFIX, clock=_clock)    # 重启=全新实例
    report = second.prepare(SCOPE, DAY)
    assert report.prepared_seq == 10 and report.lexical_watermark == 10
    assert client.source(CONTROL, DAY_KEY) == before        # 幂等零写
    assert [c for c in client.calls if c[0] == "index"] == writes_before


def test_tombstoned_record_never_enters_recall_candidate_scan():
    """③：墓态件不进召回候选扫描（登记扫描词表 task_state∈{accepted,
    running}——tombstoned 结构性缺席）。"""
    mixed = _doc(7, task_state="tombstoned", preparation_state=None)
    docs = [_doc(seq) for seq in (1, 2, 3)] + [mixed]
    client = _seed_client(*docs, materialized=3, tombstones=[_tombstone(7)])
    assert _recall_candidate_ids(client) == [_rid(1), _rid(2), _rid(3)]


# ---------- ④⑤⑥⑧vector 面（推进器） ----------

def test_advancer_terminal_proofs_cross_missing_vector_hole():
    """⑤：墓证=vector 前沿合法跳洞凭据——缺向量序号（17 形态的 2）不
    造永久停摆；hole_count 零开（孤儿与墓证语义分立）。"""
    store = MemoryStore()
    advancer = VectorFrontierAdvancer(store, clock=lambda: NOW)
    advancer.advance(SCOPE, DAY, "space-x", [1])
    report = advancer.advance(SCOPE, DAY, "space-x", [3], terminal_proofs={2})
    assert report.vector_frontier == 3
    assert report.tombstone_proofs == 1 and report.advanced is True
    snapshot = _vsnapshot(store)
    assert snapshot["vector_frontier"] == 3
    assert snapshot["tombstone_proof_count"] == 1
    assert snapshot["hole_count"] == 0


def test_advancer_terminal_evidence_port_reads_same_ledger():
    """⑧端口注入：advance 自取墓证（与 prepare 共读同一墓簿证据源——
    materialize_terminal.scan_terminal_tombstone_seqs 同一读面）；显式
    terminal_proofs 与端口证据并集。"""
    client = _seed_client(tombstones=[_tombstone(2)])
    store = MemoryStore()
    advancer = VectorFrontierAdvancer(
        store, clock=lambda: NOW, terminal_evidence=_terminal_port(client))
    advancer.advance(SCOPE, DAY, "space-x", [1])
    report = advancer.advance(SCOPE, DAY, "space-x", [3])
    assert report.vector_frontier == 3
    # 端口墓证 {2} + 显式墓证 {5}：ready {4,6} 跨两洞并推。
    report = advancer.advance(SCOPE, DAY, "space-x", [4, 6],
                              terminal_proofs={5})
    assert report.vector_frontier == 6
    assert _vsnapshot(store)["tombstone_proof_count"] == 2    # 2 与 5


def test_advancer_without_terminal_stalls_zero_diff():
    """零 diff 守卫：无墓证（端口+显式均缺）→ 序号断档不跳洞（既有
    ③a 钉同语义的推进器直钉）。"""
    store = MemoryStore()
    advancer = VectorFrontierAdvancer(store, clock=lambda: NOW)
    advancer.advance(SCOPE, DAY, "space-x", [1])
    report = advancer.advance(SCOPE, DAY, "space-x", [3])
    assert report.vector_frontier == 1
    assert report.tombstone_proofs == 0
    assert _vsnapshot(store)["max_ready_seen"] == 3


def test_advancer_restart_is_monotonic():
    """⑥重启：全新推进器实例读同一持久快照——前沿不回退（静默轮
    advanced=False 零推进）。"""
    store = MemoryStore()
    VectorFrontierAdvancer(store, clock=lambda: NOW).advance(
        SCOPE, DAY, "space-x", [1, 2])
    fresh = VectorFrontierAdvancer(store, clock=lambda: NOW)
    report = fresh.advance(SCOPE, DAY, "space-x", [])
    assert report.vector_frontier == 2 and report.advanced is False


# ---------- ⑤vector 面（对账器：墓证序号不 record_hole） ----------

def test_reconciler_terminal_seq_skips_without_hole_or_foreign():
    """⑤对账面：区间内墓证序号=确定性终态——跳过且**不 record_hole**
    （缺向量≠孤儿），tombstone_proofs 分计不算外国。"""
    store = MemoryStore()
    VectorFrontierAdvancer(store, clock=lambda: NOW).advance(
        SCOPE, DAY, "space-x", [1, 2, 3])
    holes: list[int] = []
    rows = {1: object(), 3: object()}

    def row_for_seq(seq):
        if seq == 2:
            # 混合形态：主记录在场（task_state=tombstoned）但无向量行。
            return None, {"task_state": "tombstoned"}
        return rows[seq], {"task_state": "accepted"}

    reconciler = VectorReconciler(
        store, clock=lambda: NOW, terminal_evidence=lambda scope, day: {2})
    report = reconciler.reconcile(
        SCOPE, DAY, "space-x", row_for_seq=row_for_seq,
        journal_confirmed=lambda seq, row, authority: seq != 2,
        record_hole=holes.append)
    assert report.reconciled_through == 3
    assert report.holes_opened == 0 and report.foreign_proofs == 0
    assert report.tombstone_proofs == 1
    assert holes == []                          # 墓证序号不落孔洞
    assert _vsnapshot(store)["tombstone_proof_count"] == 1


def test_reconciler_without_terminal_port_keeps_hole_semantics():
    """零 diff 守卫：未注入端口→无行有权威（孤儿形态）照旧 record_hole
    （stale_generation_row）；tombstone_proofs 恒 0。"""
    store = MemoryStore()
    VectorFrontierAdvancer(store, clock=lambda: NOW).advance(
        SCOPE, DAY, "space-x", [1, 2, 3])
    holes: list[int] = []

    def row_for_seq(seq):
        if seq == 2:
            return None, {"task_state": "tombstoned"}
        return object(), {"task_state": "accepted"}

    reconciler = VectorReconciler(store, clock=lambda: NOW)
    report = reconciler.reconcile(
        SCOPE, DAY, "space-x", row_for_seq=row_for_seq,
        journal_confirmed=lambda seq, row, authority: seq != 2,
        record_hole=holes.append)
    assert report.holes_opened == 1 and holes == [2]
    assert report.tombstone_proofs == 0
    assert _vsnapshot(store)["tombstone_proof_count"] == 0


# ---------- 全前沿域内链钉（T8 千件钉的域级前身） ----------

def test_full_frontier_tombstone_chain_reaches_tail_at_domain_level():
    """1~16 READY+17 TOMBSTONE+18~20 READY → prepared/lexical/vector 前沿
    全达 20（materialize 水位=T4 面）；17 不进召回候选；重启一致。"""
    docs = [_doc(seq) for seq in list(range(1, 17)) + list(range(18, 21))]
    client = _seed_client(*docs, materialized=20, tombstones=[_tombstone(17)])
    # vector 面：同一墓簿证据源（⑧），逐写推进（18~20 向量写确认形态；
    # 17 无向量且不要求生成 ④）。
    vstore = MemoryStore()
    advancer = VectorFrontierAdvancer(
        vstore, clock=lambda: NOW, terminal_evidence=_terminal_port(client))
    for seq in list(range(1, 17)) + list(range(18, 21)):
        advancer.advance(SCOPE, DAY, "space-x", [seq])
    assert _vsnapshot(vstore)["vector_frontier"] == 20
    # prepare 面 + lexical（=物化水位）。
    worker = PrepareWorker(client, PREFIX, clock=_clock)
    report = worker.prepare(SCOPE, DAY)
    provider = ElasticsearchWatermarkProvider(client, PREFIX)
    assert (report.prepared_seq, report.lexical_watermark) == (20, 20)
    assert provider.visible_seq(SCOPE, DAY) == 20
    assert provider.prepared_seq(SCOPE, DAY) == 20
    # ③17 不进召回候选。
    assert _rid(17) not in _recall_candidate_ids(client)
    # ⑥重启：全新 worker/advancer——前沿一致不回退。
    assert PrepareWorker(client, PREFIX, clock=_clock).prepare(
        SCOPE, DAY).prepared_seq == 20
    fresh = VectorFrontierAdvancer(
        vstore, clock=lambda: NOW, terminal_evidence=_terminal_port(client))
    assert fresh.advance(SCOPE, DAY, "space-x", []).vector_frontier == 20
