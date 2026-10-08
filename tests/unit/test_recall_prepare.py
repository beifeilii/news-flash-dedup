"""B4/E1 红测（D1 §五 T1）：F2 准备层 recall/prepare.py。

红测清单映射（log\设计-真链路接线包.md §五-T1）：
- R-6  准备幂等：同文档两轮准备 CAS 后字段逐字节同值；text 变→放弃并报冲突；
       preparation_state 翻 ready 后通道查询可命中
- R-7  水位孔洞纪律：seq{1,2,4} ready（3 缺）→prepared_seq=2 而非 4（09 §3.2 L175）；
       checkpoint 内嵌读写往返一致
另钉：空体/不可索引文本 not_applicable 不硬拦（F2⑥）；日控制文档 create-if-absent
（N-4，g0_persistence.py:182-198 先例）；lexical_watermark 写既有 long 槽位。
"""

from __future__ import annotations

from datetime import datetime, timezone

from news_flash_dedup.batch_admission import HEAD_ID
from news_flash_dedup.es_admission_schema import day_index
from news_flash_dedup.recall.prepare import (
    ElasticsearchWatermarkProvider,
    PrepareWorker,
    advance_prepared_frontier,
)

from b4_fake_es import FakeESClient

DAY = "2026-09-29"
PREFIX = "p01-batch-b4prep-"
SCOPE = "default"
ITEMS = PREFIX + day_index(DAY)
CONTROL = PREFIX + "news-dedup-control-v1"
DAY_KEY = f"day:{SCOPE}:{DAY}"
NOW = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)

R1 = f"r{1:062d}"
R2 = f"r{2:062d}"
R3 = f"r{3:062d}"

TEXT = "美国能源信息署公布原油库存增加。"
EXPIRY = "2026-10-06T00:00:00.000000Z"


def _clock() -> datetime:
    return NOW


def _doc(seq: int, text: str = TEXT, *, task_state: str = "accepted",
         preparation_state: str | None = None) -> dict:
    doc = {
        "scope_id": SCOPE, "request_id": f"9-{seq}", "item_id": f"9-{seq}",
        "record_id": f"r{seq:062d}", "schema_version": "v1",
        "pipeline_version": "dedup_v1", "embedding_space_id": "space-x",
        "business_date": DAY, "arrival_seq": seq,
        "received_at": "2026-09-29T01:00:00.000000Z",
        "accepted_at": "2026-09-29T01:00:00.000000Z",
        "expires_at": EXPIRY, "text": text,
        "raw_hash": "0" * 64, "task_state": task_state,
        "delivery_state": "not_ready", "result": None,
    }
    if preparation_state is not None:
        doc["preparation_state"] = preparation_state
    return doc


def _seed_client(*docs: dict, materialized: int = 0) -> FakeESClient:
    client = FakeESClient()
    for doc in docs:
        client.put(ITEMS, doc["record_id"], doc)
    client.put(CONTROL, HEAD_ID, {
        "kind": "head", "scope_id": SCOPE, "owner_id": "owner-1",
        "last_allocated_seq": materialized, "last_materialized_seq": materialized,
        "pending": {"version": "batch-v1", "batches": []},
        "updated_at": "2026-09-29T01:00:00.000000Z",
    })
    return client


# ---------- R-6 准备幂等 ----------

def test_r6_prepare_merges_derived_fields_and_marks_ready():
    client = _seed_client(_doc(1), materialized=1)
    worker = PrepareWorker(client, PREFIX, clock=_clock)
    report = worker.prepare(SCOPE, DAY)
    assert report.prepared == (R1,)
    assert report.conflicts == ()
    source = client.source(ITEMS, R1)
    # 派生字段并入既有 mapping 槽位（es_admission_schema.py:56-74）
    assert source["preparation_state"] == "ready"
    for key in ("raw_hash", "normalized_hash", "normalizer_version", "normalized_text",
                "position_map", "simhash", "simhash_bands", "minhash_bands",
                "fingerprint_payload", "entity_ids"):
        assert key in source, key
    assert "entity_v1|org:美国能源信息署" in source["entity_ids"]
    # preparation_state 翻 ready 后通道查询可命中（通道过滤形态：term preparation_state=ready）
    hits = client.search(index=ITEMS, body={"query": {"bool": {
        "filter": [{"term": {"preparation_state": "ready"}}]}}})
    assert [hit["_id"] for hit in hits["hits"]["hits"]] == [R1]


def test_r6_second_round_is_byte_identical_and_write_free():
    client = _seed_client(_doc(1), materialized=1)
    worker = PrepareWorker(client, PREFIX, clock=_clock)
    worker.prepare(SCOPE, DAY)
    before = client.source(ITEMS, R1)
    index_calls_before = [c for c in client.calls if c[0] == "index"]
    report = worker.prepare(SCOPE, DAY)
    after = client.source(ITEMS, R1)
    assert before == after                       # 两轮后字段逐字节同值
    assert report.prepared == ()                 # 第二轮无可准备
    assert [c for c in client.calls if c[0] == "index"] == index_calls_before


def test_r6_text_change_during_cas_abandons_and_reports_conflict():
    client = _seed_client(_doc(1), materialized=1)
    original_index = client.index
    flipped = {"done": False}

    def racing_index(**kwargs):
        # 首次 CAS 时注入并发改写（text 变）并以冲突拒绝，模拟准备窗口内文本换代
        if (kwargs.get("id") == R1 and kwargs.get("op_type") != "create"
                and not flipped["done"]):
            flipped["done"] = True
            doc = client.docs[(ITEMS, R1)]
            client.docs[(ITEMS, R1)] = {
                "_source": {**doc["_source"], "text": "完全不同的正文。",
                            "raw_hash": "f" * 64},
                "_seq_no": doc["_seq_no"] + 1,
                "_primary_term": doc["_primary_term"],
            }
            from elasticsearch import ConflictError
            raise ConflictError("racing write", None, None)
        return original_index(**kwargs)

    client.index = racing_index
    worker = PrepareWorker(client, PREFIX, clock=_clock)
    report = worker.prepare(SCOPE, DAY)
    assert report.conflicts == (R1,)                # 放弃该轮并报冲突
    assert report.prepared == ()
    source = client.source(ITEMS, R1)
    assert source["text"] == "完全不同的正文。"      # 已存文档不被准备写污染
    assert "preparation_state" not in source


def test_r6_unindexable_text_marks_not_applicable_without_blocking():
    client = _seed_client(_doc(1), _doc(2, text=""), _doc(3), materialized=3)
    worker = PrepareWorker(client, PREFIX, clock=_clock)
    report = worker.prepare(SCOPE, DAY)
    assert report.prepared == (R1, R3)
    assert report.not_applicable == (R2,)
    assert client.source(ITEMS, R2)["preparation_state"] == "not_applicable"
    # 空体不硬拦：准备前沿越过 not_applicable 推进（F2⑥）
    provider = ElasticsearchWatermarkProvider(client, PREFIX)
    assert provider.prepared_seq(SCOPE, DAY) == 3


# ---------- R-7 水位孔洞纪律 ----------

def test_r7_frontier_never_skips_holes():
    assert advance_prepared_frontier(0, {1, 2, 4}) == 2    # 3 缺 → 停在 2 而非 4
    assert advance_prepared_frontier(2, {1, 2, 4}) == 2
    assert advance_prepared_frontier(0, {1, 2, 3, 4}) == 4
    assert advance_prepared_frontier(4, {5, 7}) == 5       # 6 缺 → 5
    assert advance_prepared_frontier(0, {2, 3}) == 0       # 1 缺 → 不启动
    assert advance_prepared_frontier(7, {1, 2}) == 7       # 单调不回退


def test_r7_checkpoint_embedded_roundtrip_and_lexical_slot():
    # seq{1,2,4} ready（3 缺）→ prepared_seq=2；lexical=物化水位 4
    docs = [_doc(1, preparation_state="ready"),
            _doc(2, preparation_state="ready"),
            _doc(4, preparation_state="ready")]
    client = _seed_client(*docs, materialized=4)
    worker = PrepareWorker(client, PREFIX, clock=_clock)
    report = worker.prepare(SCOPE, DAY)
    assert report.prepared_seq == 2
    assert report.lexical_watermark == 4

    provider = ElasticsearchWatermarkProvider(client, PREFIX)
    assert provider.prepared_seq(SCOPE, DAY) == 2          # checkpoint 内嵌读回
    assert provider.visible_seq(SCOPE, DAY) == 4           # 既有 long 槽位读回
    # checkpoint 内嵌写入形态：disabled 对象内嵌快照（g0_persistence.py:330-334 先例）
    day_source = client.source(CONTROL, DAY_KEY)
    assert day_source["lexical_watermark"] == 4
    snapshot = day_source["checkpoint"]["recall_prepared"]
    assert snapshot["prepared_seq"] == 2
    assert snapshot["scope_id"] == SCOPE
    assert snapshot["business_date"] == DAY
    assert day_source["kind"] == "day"                     # create-if-absent 形态


def test_r7_day_control_doc_created_if_absent_and_reused():
    client = _seed_client(_doc(1), materialized=1)
    worker = PrepareWorker(client, PREFIX, clock=_clock)
    worker.prepare(SCOPE, DAY)
    created = client.source(CONTROL, DAY_KEY)
    assert created is not None
    assert created["kind"] == "day"
    assert created["scope_id"] == SCOPE
    assert created["business_date"] == DAY
    assert created["decision_watermark"] == 0              # F2 不动决策水位
    second = PrepareWorker(client, PREFIX, clock=_clock)
    second.prepare(SCOPE, DAY)
    assert client.source(CONTROL, DAY_KEY)["checkpoint"]["recall_prepared"][
        "prepared_seq"] == 1


def test_r7_watermark_provider_defaults_zero_when_day_doc_absent():
    provider = ElasticsearchWatermarkProvider(FakeESClient(), PREFIX)
    assert provider.visible_seq(SCOPE, DAY) == 0
    assert provider.prepared_seq(SCOPE, DAY) == 0
