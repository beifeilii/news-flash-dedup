"""W-R4 红测：R3-M8 活性缺陷修复（09 §3.4 登记证明跳过语义）。

缺陷背景（log\修复报告-W-R3c码面修复窗.md §一 取证）：arrival_seq 全局连续
（batch_admission.py:186/391/417 全局计数器+_validate_log 全局连续不变量），
分区内稀疏；prepare.py 孔洞纪律按 (scope,day) 分区解读 → 不占有全局前缀的
分区（第二日起/任何非首域）prepared 前沿永不启动 → worker.py:358 awaiting 闸
恒触发 → 判定链停摆（活性失败）。
（W-R5 注记：上文 `batch_admission.py:186/391/417` 系修复前坐标——现行=
头文档初始化 L221 / `last_allocated_seq + len(new_entries) + 1` L426 /
CAS 推进 L452，`_validate_log` L157 起；B5′ F5 漂移注记式对齐，语义逐字
不变，见 log\卫生报告-W-R5卫生窗.md。）

修法（主窗口裁定=按 09 §3.4 原文口径）：前沿推进遇全局序号孔洞时，向
admission 域查证该序号登记归属（ES seq 登记映射 / pending 受理日志）；登记
证明属他域/日 → 凭证明跳过续推；无法证明（登记缺席/属本分区未就绪）→ 维持
停摆（fail-closed 不猜，「缺号未知须恢复，不能猜不存在」）。

红测清单：
- 纯函数面：凭证明跳过（foreign 凭据集）+ 未证孔洞 fail-closed（含部分推进
  过已证前缀、停于首个未证序号）；
- 两日/两域活性：分区 B 无全局前缀 → 现码停摆（红）；修后 B 前沿凭跳过证明
  推进（绿）；worker 级 shadow 链路 awaiting→shadowed 活性对拍；
- fail-closed：缺号未知（登记缺席）不猜不越过；同分区未就绪孔洞不猜外国；
- pending 受理日志登记证明覆盖未物化段孔洞；
- 前后均绿守卫：首域占有全局前缀场景无需证明、行为逐字节不变（既有钉
  test_recall_prepare.py:162 同语义场景的本窗复证）。
"""

from __future__ import annotations

from datetime import datetime, timezone

from news_flash_dedup.batch_admission import HEAD_ID
from news_flash_dedup.es_admission_schema import day_index
from news_flash_dedup.recall.models import ChannelResult
from news_flash_dedup.recall.prepare import (
    ElasticsearchWatermarkProvider,
    PrepareWorker,
    advance_prepared_frontier,
)
from news_flash_dedup.recall.service import RecallService
from news_flash_dedup.recall.worker import (
    DedupWorker,
    ElasticsearchRegistrationScanner,
)

from b4_fake_es import FakeESClient

DAY1 = "2026-09-28"
DAY2 = "2026-09-29"
PREFIX = "p01-batch-winr4-"
SCOPE_A = "scope-a"
SCOPE_B = "scope-b"
ITEMS1 = PREFIX + day_index(DAY1)
ITEMS2 = PREFIX + day_index(DAY2)
CONTROL = PREFIX + "news-dedup-control-v1"
REQUEST = PREFIX + "news-dedup-requests-v1"
NOW = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)
EXPIRY = "2026-10-06T00:00:00.000000Z"
TEXT = "美国能源信息署公布原油库存增加。"

R3 = f"r{3:062d}"


def _clock() -> datetime:
    return NOW


def _doc(seq: int, scope: str, day: str, *, text: str = TEXT,
         task_state: str = "accepted",
         preparation_state: str | None = None) -> dict:
    doc = {
        "scope_id": scope, "request_id": f"9-{seq}", "item_id": f"9-{seq}",
        "record_id": f"r{seq:062d}", "schema_version": "v1",
        "pipeline_version": "dedup_v1", "embedding_space_id": "space-x",
        "business_date": day, "arrival_seq": seq,
        "received_at": "2026-09-29T01:00:00.000000Z",
        "accepted_at": "2026-09-29T01:00:00.000000Z",
        "expires_at": EXPIRY, "text": text,
        "raw_hash": "0" * 64, "task_state": task_state,
        "delivery_state": "not_ready", "result": None,
    }
    if preparation_state is not None:
        doc["preparation_state"] = preparation_state
    return doc


def _seq_map(seq: int, scope: str, day: str) -> dict:
    """物化 seq 登记映射（batch_admission.py:467 形态的最小要素）。"""
    return {
        "kind": "seq", "scope_id": scope, "request_id": f"9-{seq}",
        "item_id": f"9-{seq}", "record_id": f"r{seq:062d}",
        "business_fingerprint": "f" * 64, "business_date": day,
        "arrival_seq": seq, "expires_at": EXPIRY,
        "target_index": "news-dedup-items-v1-" + day.replace("-", "."),
    }


def _pending_entry(seq: int, scope: str, day: str) -> dict:
    return {
        "scope_id": scope, "request_id": f"9-{seq}", "item_id": f"9-{seq}",
        "record_id": f"r{seq:062d}", "business_fingerprint": "f" * 64,
        "text": TEXT, "delivery_route_ref": "route-1", "trace_id": None,
        "received_at": "2026-09-29T01:00:00.000000Z",
        "accepted_at": "2026-09-29T01:00:00.000000Z",
        "business_date": day, "expires_at": EXPIRY,
        "schema_version": "v1", "pipeline_version": "dedup_v1",
        "embedding_space_id": "space-x", "arrival_seq": seq,
    }


def _head(*, materialized: int, allocated: int,
          batches: list[dict] | None = None) -> dict:
    return {
        "kind": "admission", "owner_id": "owner-1",
        "last_allocated_seq": allocated, "last_materialized_seq": materialized,
        "pending": {"version": "batch-v1", "batches": list(batches or [])},
        "checkpoint": {}, "updated_at": "2026-09-29T01:00:00.000000Z",
    }


def _batch(entries: list[dict]) -> dict:
    return {"batch_id": "b" * 64, "digest": "d" * 64, "entries": entries}


def _seed(client: FakeESClient, *, docs: list[tuple[str, dict]],
          seq_maps: list[dict], head: dict) -> None:
    for index, doc in docs:
        client.put(index, doc["record_id"], doc)
    for seq_map in seq_maps:
        client.put(REQUEST, "seq:" + str(seq_map["arrival_seq"]), seq_map)
    client.put(CONTROL, HEAD_ID, head)


# ---------- 纯函数面：凭证明跳过 + fail-closed ----------

def test_wr4_frontier_skips_holes_with_registration_proof():
    # 09 §3.4：登记证明属他域/日的缺号可跳过续推
    assert advance_prepared_frontier(0, {3}, foreign={1, 2}) == 3
    assert advance_prepared_frontier(0, {2, 4}, foreign={1, 3}) == 4
    assert advance_prepared_frontier(0, {2, 3}, foreign={1}) == 3


def test_wr4_frontier_fail_closed_at_unproven_hole():
    # 3 已证外国、4 无证明 → 推进过已证前缀停在 3，ready 5 不越过（不猜）
    assert advance_prepared_frontier(0, {2, 5}, foreign={1, 3}) == 3
    # 全部孔洞无证明 → 不启动（既有钉 test_recall_prepare.py:162 同语义场景）
    assert advance_prepared_frontier(0, {2, 3}, foreign=set()) == 0
    # 单调不回退：凭据不改变已认证前沿
    assert advance_prepared_frontier(7, {1, 2}, foreign={3, 4}) == 7


# ---------- 两日/两域活性：分区 B 无全局前缀 ----------

def test_wr4_partition_without_global_prefix_advances_with_proofs():
    # scope A 占有全局前缀（seq 1,2@DAY1）；scope B 首条=seq 3@DAY2——
    # 现码 prepared 前沿对 B 永不启动（红）；修后凭 seq 登记证明推进（绿）
    client = FakeESClient()
    _seed(client,
          docs=[(ITEMS1, _doc(1, SCOPE_A, DAY1, preparation_state="ready")),
                (ITEMS1, _doc(2, SCOPE_A, DAY1, preparation_state="ready")),
                (ITEMS2, _doc(3, SCOPE_B, DAY2))],
          seq_maps=[_seq_map(1, SCOPE_A, DAY1), _seq_map(2, SCOPE_A, DAY1),
                    _seq_map(3, SCOPE_B, DAY2)],
          head=_head(materialized=3, allocated=3))
    worker = PrepareWorker(client, PREFIX, clock=_clock)
    report = worker.prepare(SCOPE_B, DAY2)
    assert report.prepared == (R3,)          # B 文档本轮就绪
    assert report.prepared_seq == 3          # 红：修复前恒 0（前沿不启动）
    provider = ElasticsearchWatermarkProvider(client, PREFIX)
    assert provider.prepared_seq(SCOPE_B, DAY2) == 3
    assert provider.visible_seq(SCOPE_B, DAY2) == 3   # lexical 前提二不动


def test_wr4_foreign_gap_mid_partition_skipped_with_proofs():
    # 分区 B 持有 seq 6,8；1-5,7 全属他域他日——凭证明跨段推进
    client = FakeESClient()
    _seed(client,
          docs=[(ITEMS2, _doc(6, SCOPE_B, DAY2)),
                (ITEMS2, _doc(8, SCOPE_B, DAY2))],
          seq_maps=[_seq_map(n, SCOPE_A, DAY1) for n in (1, 2, 3, 4, 5, 7)]
                   + [_seq_map(6, SCOPE_B, DAY2), _seq_map(8, SCOPE_B, DAY2)],
          head=_head(materialized=8, allocated=8))
    worker = PrepareWorker(client, PREFIX, clock=_clock)
    report = worker.prepare(SCOPE_B, DAY2)
    assert report.prepared_seq == 8          # 红：修复前恒 0
    provider = ElasticsearchWatermarkProvider(client, PREFIX)
    assert provider.prepared_seq(SCOPE_B, DAY2) == 8


# ---------- fail-closed：无法证明=维持停摆（不猜） ----------

def test_wr4_unknown_gap_fail_closed_no_guessing():
    # seq 3 无任何登记证明（seq 映射缺席+pending 空）= 缺号未知——
    # 推进过已证 1,2 后停在 2，ready 4 不越过（「缺号未知须恢复，不能猜不存在」）
    client = FakeESClient()
    _seed(client,
          docs=[(ITEMS2, _doc(4, SCOPE_B, DAY2))],
          seq_maps=[_seq_map(1, SCOPE_A, DAY1), _seq_map(2, SCOPE_A, DAY1),
                    _seq_map(4, SCOPE_B, DAY2)],
          head=_head(materialized=4, allocated=4))
    worker = PrepareWorker(client, PREFIX, clock=_clock)
    report = worker.prepare(SCOPE_B, DAY2)
    assert report.prepared_seq == 2          # 红：修复前 0；修后 ≠4（fail-closed）
    provider = ElasticsearchWatermarkProvider(client, PREFIX)
    assert provider.prepared_seq(SCOPE_B, DAY2) == 2


def test_wr4_same_partition_unready_gap_fail_closed():
    # seq 3 登记属本分区但未就绪（task_state 终态、无 ready 证据）——
    # 登记证明不属他域/日 → 不可跳过，停在 2，ready 4 不越过
    client = FakeESClient()
    _seed(client,
          docs=[(ITEMS2, _doc(2, SCOPE_B, DAY2)),
                (ITEMS2, _doc(3, SCOPE_B, DAY2, task_state="succeeded")),
                (ITEMS2, _doc(4, SCOPE_B, DAY2))],
          seq_maps=[_seq_map(1, SCOPE_A, DAY1), _seq_map(2, SCOPE_B, DAY2),
                    _seq_map(3, SCOPE_B, DAY2), _seq_map(4, SCOPE_B, DAY2)],
          head=_head(materialized=4, allocated=4))
    worker = PrepareWorker(client, PREFIX, clock=_clock)
    report = worker.prepare(SCOPE_B, DAY2)
    assert report.prepared_seq == 2          # 红：修复前 0；修后 ≠4（不猜外国）
    provider = ElasticsearchWatermarkProvider(client, PREFIX)
    assert provider.prepared_seq(SCOPE_B, DAY2) == 2


def test_wr4_pending_log_registration_proof_covers_unmaterialized_gap():
    # 头文档读数落后于在途物化：seq 2 仍居 pending 受理日志（他域他日），
    # seq 3 文档及 seq 映射已由在途物化落盘——pending 登记证明覆盖未物化段
    client = FakeESClient()
    _seed(client,
          docs=[(ITEMS2, _doc(3, SCOPE_B, DAY2))],
          seq_maps=[_seq_map(1, SCOPE_A, DAY1), _seq_map(3, SCOPE_B, DAY2)],
          head=_head(materialized=1, allocated=3, batches=[
              _batch([_pending_entry(2, SCOPE_A, DAY2)]),
              _batch([_pending_entry(3, SCOPE_B, DAY2)]),
          ]))
    worker = PrepareWorker(client, PREFIX, clock=_clock)
    report = worker.prepare(SCOPE_B, DAY2)
    assert report.prepared_seq == 3          # 红：修复前 0（pending 证明未用）
    assert report.lexical_watermark == 1     # 前提二不动：lexical 仍取头文档
    provider = ElasticsearchWatermarkProvider(client, PREFIX)
    assert provider.prepared_seq(SCOPE_B, DAY2) == 3


# ---------- worker 级活性对拍（shadow 链路） ----------

class _FakeChannel:
    def __init__(self, fn) -> None:
        self.fn = fn

    def search(self, request, query_vector=None):
        return self.fn(request)


def _complete(channel: str, qv: str, request) -> ChannelResult:
    return ChannelResult(
        channel, "complete", (), qv,
        visible_seq=request.visible_seq, prepared_seq=request.prepared_seq,
        coverage_complete=True)


def _service() -> RecallService:
    channels = {
        "hash": _FakeChannel(lambda r: _complete("hash", "hash_v1", r)),
        "near": _FakeChannel(lambda r: _complete("near", "near_v1", r)),
        "bm25": _FakeChannel(lambda r: _complete("bm25", "bm25_v1", r)),
        "entity": _FakeChannel(lambda r: _complete("entity", "entity_v1", r)),
    }
    vector = _FakeChannel(lambda r: _complete("embedding", "embedding_v1", r))
    return RecallService(None, PREFIX, channels=channels, vector_searcher=vector)


class _ListSink:
    def __init__(self) -> None:
        self.emitted: list[tuple[str, dict]] = []

    def emit(self, kind: str, payload: dict) -> None:
        self.emitted.append((kind, payload))


def test_wr4_worker_liveness_partition_b_day2_shadow():
    # 全真件组合（真 PrepareWorker/真水位端口/真注册序扫描 over 假 ES）：
    # 分区 B 第二日首条 seq 3——现码 prepared=0 < seq-1 → awaiting 恒闸停摆
    # （红）；修后凭跳过证明前沿推进 → shadowed（绿）
    client = FakeESClient()
    _seed(client,
          docs=[(ITEMS1, _doc(1, SCOPE_A, DAY1, preparation_state="ready")),
                (ITEMS1, _doc(2, SCOPE_A, DAY1, preparation_state="ready")),
                (ITEMS2, _doc(3, SCOPE_B, DAY2))],
          seq_maps=[_seq_map(1, SCOPE_A, DAY1), _seq_map(2, SCOPE_A, DAY1),
                    _seq_map(3, SCOPE_B, DAY2)],
          head=_head(materialized=3, allocated=3))
    worker = DedupWorker(
        mode="shadow",
        recall_service=_service(),
        prepare_worker=PrepareWorker(client, PREFIX, clock=_clock),
        watermark_provider=ElasticsearchWatermarkProvider(client, PREFIX),
        scanner=ElasticsearchRegistrationScanner(client, PREFIX),
        artifact_sink=_ListSink(),
        clock=_clock,
    )
    report = worker.drive_once(SCOPE_B, DAY2)
    assert report.awaiting == ()              # 红：修复前 awaiting=(R3,) 停摆
    assert report.shadowed == (R3,)           # 修后活性恢复
    assert report.failed == report.stale == ()


# ---------- 前后均绿守卫：首域占有全局前缀场景零变化 ----------

def test_wr4_guard_first_scope_prefix_needs_no_proof():
    # 首域自 1 连续占有全局前缀：无孔洞、无任何登记证明需求——
    # 修复前后行为逐字节一致（既有钉 test_recall_prepare.py R-7 族同语义）
    client = FakeESClient()
    _seed(client,
          docs=[(ITEMS1, _doc(1, SCOPE_A, DAY1)),
                (ITEMS1, _doc(2, SCOPE_A, DAY1))],
          seq_maps=[],
          head=_head(materialized=2, allocated=2))
    worker = PrepareWorker(client, PREFIX, clock=_clock)
    report = worker.prepare(SCOPE_A, DAY1)
    assert report.prepared_seq == 2
    provider = ElasticsearchWatermarkProvider(client, PREFIX)
    assert provider.prepared_seq(SCOPE_A, DAY1) == 2
    assert provider.visible_seq(SCOPE_A, DAY1) == 2
