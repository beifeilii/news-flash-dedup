"""窗口Z2 中危代码批修：六项红测钉（外部审计第三轮确认项，主窗口分诊）。

红能力说明（全部关键断言针对新行为，旧代码必红）：
1. N2-01 persist/main_record_persist.cas_main_record 返回值 off-by-one：
   (0,0) 首创建自存版本 (0,1)（ES _seq_no 0 基，新 seq_no=0），旧返回
   expected_seq_no+1=1——与自存版本差一；以旧返回值链式喂下一次
   expected 必 FakeCASConflictError。钉：首创建返回 0 且等于
   main_version[0]；链式 CAS（返回值直接作 expected）成功。
2. N2-03 commit/fake_store.write_main_record 静默覆盖 vs 真 ES (0,0)
   拒重 ID 两态相反：钉重 ID → FakeCASConflictError（现役异常复用）、
   已存记录任何字段不动（INV-2）、版本轨迹 (0,1) 且 reset 清零。
3. N2-02 delivery/es_store.update_lease 仅查 deadline：钉 expires_at
   已过（deadline 未来）→ P20ClaimPreconditionError 且租约零写入；
   绿守卫：expires 未来 / 缺键（存量历史数据兼容）/ 空串照领。
4. 13 §1① api/contract.parse_submission pageDate 可选复原：钉缺省
   pageDate → page_date == ""（旧 422 红）；绿守卫：传入时
   min=1/max=128/类型校验逐字节不变（与 13 快照 schema 同格）。
5. milvus_store 裸异常语义化：search_strong distance=None → 旧裸
   TypeError / 新 VectorWriteUnknown（与 vector_store.py:235
   type+isfinite 口径一致）；_ensure_collection describe 缺 fields /
   字段缺 name / 非 Mapping → 旧裸 KeyError/TypeError / 新
   VectorWriteUnknown（读确认未知，仓内同口径）。
6. batch_es_store 四处 from None → from error（P 批保链纪律）：钉
   get/mget/bulk(4xx)/bulk(5xx) 包装异常的 __cause__ 即原异常
   （旧 __cause__ 为 None 红）。

7. H-05（主窗口追裁解禁）：delivery/coordinator.receive 的 now 缺省 ""
   → 缺省取当前时——deadline/expires/backoff 三闸永远执法（旧"缺省
   零校验"兼容形态废止）：钉缺省调用 + 过期 deadline / 未退避 /
   过期 expires_at 三形态即拒且零副作用；绿守卫：健康记录缺省照领、
   显式 now 语义不变。模块 docstring "round++" 与 L123 round=rec.round
   矛盾同步改实述（轮次推进由 reopen_round 承担）。
8. N2-01 cleanup 空白指令（外审升级，主窗口追裁）：api/cleanup.py
   execute 端 dry_run 默认翻 True——空 body/缺键一律演习路径；仅显式
   dry_run=false（严格布尔）才真删（0/null/"" 等非 false 值 fail-closed
   不落真删）：钉空 POST 空体/空 JSON 对象/非 false 值 → port.execute
   以 dry_run=True 被调；绿守卫：显式 false 真删、显式 true 演习不变。

禁碰自核：判定壁（compare/decide/facts/recall/gold/词典/五字段）、
delivery/coordinator 三闸【H-05 项经主窗口追裁解禁，仅此一处按令改】、
sender 发送前闸、P 系 UAT 测试、llm_residual/machine_verify 零触碰
——本文件只钉仓储/契约层语义。
"""

from __future__ import annotations

import datetime
import hashlib
from datetime import date

import pytest

from news_flash_dedup.api.contract import (
    ContractError,
    IngressContext,
    parse_submission,
    submit,
)
from news_flash_dedup.admission import AdmissionReceipt
from news_flash_dedup.batch_es_store import (
    BatchReadError,
    ElasticsearchBatchStore,
    PermanentBulkError,
)
from news_flash_dedup.commit.fake_store import FakeCommitStore, FakeMainRecord
from news_flash_dedup.delivery import (
    FakeDeliveryRecord,
    FakeDeliveryStore,
    P20ReceiveError,
    receive,
)
from news_flash_dedup.delivery.es_store import (
    P20ClaimPreconditionError,
    RealESP20Config,
    RealESP20Store,
)
from news_flash_dedup.delivery import es_store as p20_es_store
from news_flash_dedup.delivery.fake_store import FakeDeliveryLease
from news_flash_dedup.persist import main_record_persist
from news_flash_dedup.persist.fake_store import (
    FakeCASConflictError,
    FakeMainRecordV1,
    FakeP18Store,
)
from news_flash_dedup.recall.vector_space import EmbeddingSpace
from news_flash_dedup.recall.vector_store import VECTOR_FIELDS, VectorWriteUnknown
from news_flash_dedup.vector.milvus_store import (
    RealMilvusP19Config,
    RealMilvusP19Store,
)

UTC = datetime.timezone.utc


def _iso(dt: datetime.datetime) -> str:
    return dt.isoformat()


# =====================================================================
# 1. N2-01：cas_main_record 返回值对齐自存版本 / 真 ES 0 基语义
# =====================================================================

def _p18_record(record_id="r-1", *, text="甲公司完成回购。") -> FakeMainRecordV1:
    return FakeMainRecordV1(
        record_id=record_id, item_id="item-A", text=text, scope_id="default",
        business_date="2026-09-26", arrival_seq=1, raw_hash="h",
        pipeline_version="dedup_v1", fact_artifact_hash="",
        vector_state="pending", result_item_id="item-A",
        # 10-07 令甲-i（I-7）：翻不重复，保持"合法 pending 载荷"测意图
        result_decision="不重复", result_duplicate_ids=(),
        result_reason="r", task_state="succeeded",
        completed_at="2026-09-26T03:33:00+00:00", result_version=1,
        event_id="e", audit_ids=(), audit_complete=True,
        reason_code="FACT_EQUIVALENT", callback_body="{}",
        callback_body_hash="h", delivery_state="pending",
        delivery_deadline_at="2026-09-27T03:33:00+00:00",
        next_delivery_at="2026-09-26T03:33:00+00:00",
        callback_attempts=0, round_attempts=0, round=1,
    )


def test_cas_first_create_returns_stored_zero_based_seq():
    """红能力：旧返回 expected+1=1，自存 (0,1)——断言 0 红（off-by-one）。"""
    store = FakeP18Store()
    seq = main_record_persist.cas_main_record(
        store, _p18_record(), expected_seq_no=0, expected_primary_term=0)
    assert seq == 0                                  # ES _seq_no 0 基首写
    assert seq == store.main_version("r-1")[0]       # 返回值 == 自存版本
    assert store.main_records["r-1"].delivery_state == "pending"


def test_cas_return_value_chains_into_next_expected_version():
    """红能力：以返回值链式喂 expected——旧 off-by-one 返回值 (1) 与自存
    (0,1) 不符 → 第二次 CAS 必 FakeCASConflictError（红）。"""
    store = FakeP18Store()
    seq0 = main_record_persist.cas_main_record(
        store, _p18_record(), expected_seq_no=0, expected_primary_term=0)
    seq1 = main_record_persist.cas_main_record(
        store, _p18_record(text="更新文本。"),
        expected_seq_no=seq0, expected_primary_term=1)
    assert seq1 == seq0 + 1
    assert store.main_version("r-1") == (seq1, 1)
    assert store.main_records["r-1"].text == "更新文本。"


# =====================================================================
# 2. N2-03：FakeCommitStore.write_main_record 版本化（拒重 ID）
# =====================================================================

def _commit_record(record_id="r-1", *, text="甲公司完成回购。") -> FakeMainRecord:
    return FakeMainRecord(
        record_id=record_id, item_id="item-A", text=text, decision="不重复",
        duplicate_ids=(), reason="r", payload_hash="p",
        delivery_state="pending",
        delivery_deadline_at="2026-09-27T00:00:00+00:00",
        callback_attempts=0, audit_ids=(), audit_complete=True,
        raw_hash="h", pipeline_version="dedup_v1",
        completed_at="2026-09-26T00:00:00+00:00",
        result_version=1, event_id="e",
    )


def test_write_main_record_rejects_duplicate_id_create_conflict():
    """红能力：旧静默覆盖（与真 ES (0,0) 拒重 ID 相反）——重 ID 写入
    不抛错且原文被篡改（红）；修复后 FakeCASConflictError + INV-2 不动。"""
    store = FakeCommitStore()
    store.write_main_record(_commit_record())
    with pytest.raises(FakeCASConflictError,
                       match="(?i)already exists|create conflict"):
        store.write_main_record(_commit_record(text="篡改文本。"))
    assert store.main_records["r-1"].text == "甲公司完成回购。"   # INV-2 不动


def test_write_main_record_tracks_zero_based_version():
    """红能力：旧零版本概念——main_version 不存在（AttributeError 红）。"""
    store = FakeCommitStore()
    store.write_main_record(_commit_record("r-1"))
    store.write_main_record(_commit_record("r-2"))
    assert store.main_version("r-1") == (0, 1)         # ES _seq_no 0 基首写
    assert store.main_version("r-2") == (0, 1)


def test_commit_store_reset_clears_versions_and_allows_recreate():
    """reset 清零版本轨迹后可重创建（红能力同源：旧无版本可清）。"""
    store = FakeCommitStore()
    store.write_main_record(_commit_record())
    store.reset()
    assert store.main_version("r-1") is None
    store.write_main_record(_commit_record())
    assert store.main_version("r-1") == (0, 1)


# =====================================================================
# 3. N2-02：update_lease 补 expires_at 校验（10 §6.340 领取同查）
# =====================================================================

class _StubIndicesApi:
    def exists(self, *, index):
        return True

    def create(self, *, index):     # exists=True 恒成立，不应触发
        raise AssertionError(f"unexpected index create: {index}")


class _StubESClient:
    """RealESP20Store 的最小鸭式客户端（单元层，不连真 ES）。"""

    def __init__(self, docs):
        self.indices = _StubIndicesApi()
        self._docs = {rid: dict(doc) for rid, doc in docs.items()}
        self._seq: dict[str, int] = {}

    def get(self, *, index, id, realtime):
        from elasticsearch import NotFoundError
        if id not in self._docs:
            raise NotFoundError("missing", None, None)
        return {"_source": self._docs[id],
                "_seq_no": self._seq.get(id, 0), "_primary_term": 1}

    def index(self, *, index, id, document, **kwargs):
        self._seq[id] = self._seq.get(id, -1) + 1
        self._docs[id] = dict(document)
        return {"_seq_no": self._seq[id], "_primary_term": 1}

    def search(self, **kwargs):
        return {"hits": {"hits": [{"_id": rid} for rid in sorted(self._docs)]}}


def _seed_doc(*, expires_at="", drop_expires_key=False):
    now = datetime.datetime.now(UTC)
    callback_body = '{"item_id":"item-A"}'
    doc = {
        "record_id": "r-1", "item_id": "item-A", "scope_id": "default",
        "business_date": "2026-09-26", "arrival_seq": 1,
        "delivery_state": "pending", "task_state": "succeeded",
        "audit_complete": True,
        "delivery_deadline_at": _iso(now + datetime.timedelta(hours=24)),
        "next_delivery_at": _iso(now - datetime.timedelta(hours=1)),
        "callback_attempts": 0, "round_attempts": 0, "round": 1,
        "callback_body": callback_body,
        "callback_body_hash": hashlib.sha256(
            callback_body.encode("utf-8")).hexdigest(),
        "route_ref": "http://127.0.0.1:8080/callback",
        "vector_state": "pending", "expires_at": expires_at,
    }
    if drop_expires_key:
        del doc["expires_at"]                       # 存量历史数据形态
    return doc


def _real_store(monkeypatch, doc):
    monkeypatch.setattr(p20_es_store, "assert_p20_uat_open", lambda: None)
    config = RealESP20Config(run_uuid="winz2", business_date=date(2026, 9, 26))
    return RealESP20Store(_StubESClient({"r-1": doc}), config)


def _claim(store, doc):
    lease = FakeDeliveryLease(
        owner_id="worker-1", owner_generation=1,
        lease_until=_iso(datetime.datetime.now(UTC)
                         + datetime.timedelta(minutes=5)),
        attempt_id="a" * 64, round=1, result_version=1, event_id="e-1",
        payload_hash=doc["callback_body_hash"],
        route_ref=doc["route_ref"],
    )
    store.main_records.claim_read("r-1")
    return store.update_lease(lease)


def test_update_lease_rejects_past_expires_at(monkeypatch):
    """红能力：旧 update_lease 仅查 deadline——deadline 未来而 expires_at
    已过照领（raises 红）；修复后拒领且租约/计数零写入。"""
    past = _iso(datetime.datetime.now(UTC) - datetime.timedelta(hours=1))
    doc = _seed_doc(expires_at=past)
    store = _real_store(monkeypatch, doc)
    with pytest.raises(P20ClaimPreconditionError, match="(?i)expires_at"):
        _claim(store, doc)
    source = store.get_main_record("r-1")["source"]
    assert "delivery_lease" not in source           # 拒领零写入
    assert source["round_attempts"] == 0            # 计数不自增
    assert source["callback_attempts"] == 0


def test_update_lease_allows_future_expires_at(monkeypatch):
    """绿守卫：deadline/expires 均未来 → 照常领取。"""
    future = _iso(datetime.datetime.now(UTC) + datetime.timedelta(hours=48))
    doc = _seed_doc(expires_at=future)
    store = _real_store(monkeypatch, doc)
    assert _claim(store, doc) == "worker-1"
    source = store.get_main_record("r-1")["source"]
    assert source["delivery_lease"]["owner_generation"] == 1
    assert source["round_attempts"] == 1


def test_update_lease_allows_missing_expires_key(monkeypatch):
    """绿守卫（存量历史数据兼容）：expires_at 缺键 → 不拦，照常领取。"""
    doc = _seed_doc(drop_expires_key=True)
    store = _real_store(monkeypatch, doc)
    assert _claim(store, doc) == "worker-1"


def test_update_lease_allows_empty_expires_at(monkeypatch):
    """绿守卫：expires_at 空串（现役默认形态）→ 不拦，照常领取。"""
    doc = _seed_doc(expires_at="")
    store = _real_store(monkeypatch, doc)
    assert _claim(store, doc) == "worker-1"


# =====================================================================
# 4. 13 §1①：pageDate 可选复原（缺省 ""；传入校验不变）
# =====================================================================

def _payload():
    return {
        "traceId": "trace-1", "requestId": "100-2", "articleId": None,
        "rewrittenId": 2,
        "source": {"title": "", "body": "  原稿\r\n  ", "pageDate": "2026-09-23"},
        "rewrite": {"title": "", "body": "  甲公司\r\n回购。  "},
        "integrationId": None,
    }


def _context():
    return IngressContext(
        "trusted-scope", datetime.datetime(2026, 9, 23, tzinfo=UTC),
        "schema-v1", "pipeline-v1", "embedding-v1", "route-revision")


def _receipt():
    return AdmissionReceipt("trusted-scope", "100-2", "100-2", "record-q", 12,
                            "2026-09-23", "2026-09-23T00:00:00Z",
                            "2026-09-30T00:00:00Z", "schema-v1", "pipeline-v1",
                            "embedding-v1", "route-revision", "trace-1", False)


def test_page_date_absent_defaults_empty_string():
    """红能力：13 §1① pageDate 可选——旧缺省 KeyError→422 必填（红）；
    修复后缺省解析成功、缺省值 ""（与可选 source.title 同缺省纪律）。"""
    payload = _payload()
    del payload["source"]["pageDate"]
    dto = parse_submission(payload)
    assert dto.page_date == ""
    assert dto.text == "  甲公司\r\n回购。  "       # 其余字段解析不变


def test_page_date_absent_submit_still_202():
    """红能力（端到端同项）：缺省 pageDate 的提交不再 422，照常 202 受理。"""
    payload = _payload()
    del payload["source"]["pageDate"]

    class Port:
        def accept(self, request):
            return _receipt()

    response = submit(payload, _context(), Port())
    assert response.status == 202
    assert response.body["accepted"] is True


def test_page_date_present_validation_unchanged():
    """绿守卫：传入 pageDate 时校验逐字节不变（13 快照 schema 同格：
    min=1/max=128/必须 str）。"""
    payload = _payload()
    assert parse_submission(payload).page_date == "2026-09-23"
    for bad_value in ("", "x" * 129, 123, None):
        payload = _payload()
        payload["source"]["pageDate"] = bad_value
        with pytest.raises(ContractError):
            parse_submission(payload)


# =====================================================================
# 5. milvus_store 裸 TypeError/KeyError → 语义化 VectorWriteUnknown
# =====================================================================

def _space():
    return EmbeddingSpace("mock_embedding", "fixture_v1", "codepoint_v1", "none",
                          "COSINE", 4, "plain_v1", "plain_v1",
                          "tokens512_overlap64_v1", "p09_v1")


def _p19_config():
    return RealMilvusP19Config(run_uuid="winz02",
                               business_date=date(2026, 9, 26),
                               space=_space())


def _bare_store(milvus):
    """绕开 __init__ 的 UAT 闸门/真建集合，仅注入依赖（单元层）。"""
    store = RealMilvusP19Store.__new__(RealMilvusP19Store)
    store.milvus = milvus
    store.config = _p19_config()
    return store


class _SearchStubMilvus:
    def __init__(self, hit):
        self._hit = hit

    def search(self, **kwargs):
        return [[self._hit]]


def _hit(distance):
    return {
        "entity": {
            "record_id": "r-1", "chunk_id": 0, "arrival_seq": 1,
            "scope_id": "default", "business_date": "2026-09-26",
            "embedding_space_id": _p19_config().space.space_id,
        },
        "distance": distance,
        "vector_id": "v-1",
    }


def test_search_strong_rejects_none_distance_semantically():
    """红能力：distance=None → 旧 float(None) 裸 TypeError 逃逸（红）；
    修复后 VectorWriteUnknown（仓内读确认未知口径）。"""
    store = _bare_store(_SearchStubMilvus(_hit(None)))
    with pytest.raises(VectorWriteUnknown, match="(?i)distance"):
        store.search_strong(query_vector=[1.0, 0.0, 0.0, 0.0],
                            scope_id="default", business_date="2026-09-26")


@pytest.mark.parametrize("bad", ["0.5", float("nan")])   # W2Fε：循环改参数化
def test_search_strong_rejects_non_numeric_or_nonfinite_distance(bad):
    """同口径红：distance 非 (int,float) 或非有限（str/float('nan')）。

    参数化（原 for 循环首败即掩后续点位）：str 与 NaN 两点位各自独立红绿。"""
    store = _bare_store(_SearchStubMilvus(_hit(bad)))
    with pytest.raises(VectorWriteUnknown, match="(?i)distance"):
        store.search_strong(query_vector=[1.0, 0.0, 0.0, 0.0],
                            scope_id="default", business_date="2026-09-26")


def test_search_strong_accepts_numeric_distance():
    """绿守卫：合法数值 distance 照常折算 score。"""
    store = _bare_store(_SearchStubMilvus(_hit(0.95)))
    hits = store.search_strong(query_vector=[1.0, 0.0, 0.0, 0.0],
                               scope_id="default", business_date="2026-09-26")
    assert hits == [{"record_id": "r-1", "chunk_id": 0, "arrival_seq": 1,
                     "score": 0.95, "vector_id": "v-1"}]


class _DescribeStubMilvus:
    def __init__(self, description):
        self._description = description
        self.created_partitions: list[str] = []

    def has_collection(self, *, collection_name):
        return True

    def describe_collection(self, *, collection_name):
        return self._description

    def list_partitions(self, *, collection_name):
        return []

    def create_partition(self, *, collection_name, partition_name):
        self.created_partitions.append(partition_name)

    # E 批 M-09 采用闸配套：良构索引证据（FLAT/COSINE embedding）；
    # 畸形 describe 参数化族在 dim/metric 闸前已被 VectorWriteUnknown 拦截，
    # 本补全不改变其断言路径。
    def list_indexes(self, *, collection_name):
        return ["embedding"]

    def describe_index(self, *, collection_name, index_name):
        return {"index_type": "FLAT", "metric_type": "COSINE",
                "field_name": "embedding"}


@pytest.mark.parametrize("description", [
    {},                                             # 缺 fields → 旧裸 KeyError
    {"fields": [{"no_name": 1}]},                   # 字段缺 name → 旧裸 KeyError
    None,                                           # 非 Mapping → 旧裸 TypeError
    {"fields": "not-a-list"},                       # fields 非 list → 旧裸 TypeError
])
def test_ensure_collection_rejects_malformed_description_semantically(description):
    """红能力：describe 回包畸形 → 旧裸 KeyError/TypeError 逃逸（红）；
    修复后 VectorWriteUnknown（无法确认 = 读确认未知，fail-closed）。"""
    store = _bare_store(_DescribeStubMilvus(description))
    with pytest.raises(VectorWriteUnknown, match="(?i)schema|description"):
        store._ensure_collection()


def test_ensure_collection_accepts_wellformed_description():
    """绿守卫：回包良构且 schema 同构 → 照常接管并补分区。"""
    description = {
        # E 批 M-09 采用闸（dim 必验等）配套：良构回包携带 dim 证据。
        "fields": [
            {"name": name, "params": {"dim": 4}} if name == "embedding"
            else {"name": name}
            for name in sorted(VECTOR_FIELDS)],
        "enable_dynamic_field": False,
    }
    milvus = _DescribeStubMilvus(description)
    store = _bare_store(milvus)
    store._ensure_collection()
    assert milvus.created_partitions == ["bd_20260926"]


# =====================================================================
# 6. batch_es_store：from None → from error（P 批保链纪律）
# =====================================================================

class _StatusError(Exception):
    def __init__(self, status_code):
        super().__init__(f"boom-{status_code}")
        self.status_code = status_code


class _RaisingClient:
    def __init__(self, error):
        self._error = error

    def get(self, **kwargs):
        raise self._error

    def mget(self, **kwargs):
        raise self._error

    def bulk(self, **kwargs):
        raise self._error


def _batch_store(error):
    return ElasticsearchBatchStore(_RaisingClient(error),
                                   index_prefix="p01-batch-winz2-")


def test_get_wrap_preserves_cause_chain():
    """红能力：L74 旧 from None 断链——__cause__ 为 None（红）。"""
    error = RuntimeError("boom")
    store = _batch_store(error)
    with pytest.raises(BatchReadError, match="(?i)unknown") as caught:
        store.get("news-dedup-control-v1", "head")
    assert caught.value.__cause__ is error


def test_mget_wrap_preserves_cause_chain():
    """红能力：L86 旧 from None 断链（红）。"""
    error = RuntimeError("boom")
    store = _batch_store(error)
    with pytest.raises(BatchReadError, match="(?i)unknown") as caught:
        store.mget([("news-dedup-requests-v1", "a")])
    assert caught.value.__cause__ is error


def test_bulk_permanent_wrap_preserves_cause_chain():
    """红能力：L116 旧 from None 断链（红）。"""
    error = _StatusError(400)
    store = _batch_store(error)
    with pytest.raises(PermanentBulkError) as caught:
        store.bulk_create([("idx", "id", {"x": 1})])
    assert caught.value.__cause__ is error


def test_bulk_unknown_wrap_preserves_cause_chain():
    """红能力：L117 旧 from None 断链（红）。"""
    error = _StatusError(500)
    store = _batch_store(error)
    with pytest.raises(RuntimeError, match="(?i)unknown") as caught:
        store.bulk_create([("idx", "id", {"x": 1})])
    assert caught.value.__cause__ is error


# =====================================================================
# 7. H-05（主窗口追裁）：receive now 缺省 → 以当前时执法（三闸永远生效）
# =====================================================================

def _delivery_record(record_id="r-1", *, delivery_state="pending",
                     delivery_deadline_at=None, next_delivery_at=None,
                     expires_at="", round_attempts=0,
                     route_ref="http://127.0.0.1:8080/callback",
                     callback_body='{"item_id":"item-A"}') -> FakeDeliveryRecord:
    now = datetime.datetime.now(UTC)
    return FakeDeliveryRecord(
        record_id=record_id, item_id="item-A", scope_id="default",
        business_date="2026-09-26", arrival_seq=1,
        delivery_state=delivery_state,
        delivery_deadline_at=delivery_deadline_at or _iso(
            now + datetime.timedelta(hours=24)),
        next_delivery_at=next_delivery_at or _iso(
            now - datetime.timedelta(hours=1)),
        callback_attempts=0, round_attempts=round_attempts, round=1,
        callback_body=callback_body,
        payload_hash=hashlib.sha256(callback_body.encode("utf-8")).hexdigest(),
        route_ref=route_ref, vector_state="pending", audit_complete=True,
        expires_at=expires_at,
    )


def _receive_default(store, rec):
    """缺省 now 调用（H-05 钉守形态：三闸以当前时执法）。"""
    return receive(store, rec.record_id, payload_hash=rec.payload_hash,
                   route_ref=rec.route_ref, expected_generation=0,
                   event_id="e-1")


def test_receive_default_now_enforces_deadline_gate():
    """红能力（H-05）：缺省调用 + deadline 已过 → 旧"缺省零校验"照领
    （raises 红）；新形态缺省 = 以当前时执法即拒，且零副作用。"""
    store = FakeDeliveryStore()
    past = _iso(datetime.datetime.now(UTC) - datetime.timedelta(hours=1))
    rec = _delivery_record(delivery_deadline_at=past, next_delivery_at=past)
    store.upsert_main(rec)
    with pytest.raises(P20ReceiveError, match="(?i)deadline"):
        _receive_default(store, rec)
    assert store.main_records["r-1"].delivery_state == "pending"   # 零副作用
    assert store.lease_index == {}
    assert store.cas_calls == []


def test_receive_default_now_enforces_backoff_gate():
    """红能力（H-05）：缺省调用 + 退避未 elapsed（next_delivery_at 未来）
    → 旧照领（红）；新形态即拒。"""
    store = FakeDeliveryStore()
    future = _iso(datetime.datetime.now(UTC) + datetime.timedelta(hours=1))
    rec = _delivery_record(next_delivery_at=future)
    store.upsert_main(rec)
    with pytest.raises(P20ReceiveError, match="(?i)backoff|next_delivery_at"):
        _receive_default(store, rec)
    assert store.lease_index == {}


def test_receive_default_now_enforces_expires_gate():
    """红能力（H-05）：缺省调用 + expires_at 已过（deadline 未来）→ 旧
    照领（红）；新形态即拒（10 §6.340 领取同查，缺省亦执法）。"""
    store = FakeDeliveryStore()
    past = _iso(datetime.datetime.now(UTC) - datetime.timedelta(hours=1))
    future = _iso(datetime.datetime.now(UTC) + datetime.timedelta(hours=48))
    rec = _delivery_record(delivery_deadline_at=future,
                           next_delivery_at=past, expires_at=past)
    store.upsert_main(rec)
    with pytest.raises(P20ReceiveError, match="(?i)expires_at"):
        _receive_default(store, rec)
    assert store.lease_index == {}


def test_receive_default_now_accepts_healthy_record():
    """绿守卫（新旧皆绿）：健康记录缺省调用照常领取，lease_until 落当前时。"""
    store = FakeDeliveryStore()
    rec = _delivery_record()
    store.upsert_main(rec)
    lease = _receive_default(store, rec)
    assert lease.owner_generation == 1
    assert store.main_records["r-1"].delivery_state == "delivering"
    datetime.datetime.fromisoformat(lease.lease_until)           # 合法 ISO 非空


def test_receive_explicit_now_semantics_unchanged():
    """绿守卫：显式 now 三闸语义逐字节不变——过期 deadline 拒、健康记录
    以过去时刻 now 照领。"""
    store = FakeDeliveryStore()
    past = _iso(datetime.datetime.now(UTC) - datetime.timedelta(hours=1))
    rec = _delivery_record(delivery_deadline_at=past, next_delivery_at=past)
    store.upsert_main(rec)
    with pytest.raises(P20ReceiveError, match="(?i)deadline"):
        receive(store, "r-1", payload_hash=rec.payload_hash,
                route_ref=rec.route_ref, expected_generation=0,
                event_id="e-1", now=_iso(datetime.datetime.now(UTC)))
    store2 = FakeDeliveryStore()
    healthy = _delivery_record()
    store2.upsert_main(healthy)
    lease = receive(store2, "r-1", payload_hash=healthy.payload_hash,
                    route_ref=healthy.route_ref, expected_generation=0,
                    event_id="e-1",
                    now=_iso(datetime.datetime.now(UTC)
                             - datetime.timedelta(minutes=30)))
    assert lease.owner_generation == 1


# =====================================================================
# 8. N2-01 cleanup 空白指令：execute 端 dry_run 默认翻 True
# =====================================================================

class _SpyCleanupPort:
    """cleanup_port 间谍：记录 dry_run/execute 调用（test_winv 同形）。"""

    def __init__(self):
        self.dry_run_calls: list = []
        self.execute_calls: list = []

    def dry_run(self, today, operator):
        self.dry_run_calls.append((today.isoformat(), operator))
        return {"today": today.isoformat(), "operator": operator,
                "would_delete": [], "absent": [], "dry_run": True}

    def execute(self, today, operator, dry_run):
        self.execute_calls.append((today.isoformat(), operator, dry_run))
        return {"today": today.isoformat(), "operator": operator,
                "dry_run": dry_run, "deleted": [], "skipped_absent": []}


def _cleanup_client(port):
    from fastapi.testclient import TestClient
    from news_flash_dedup.api.cleanup import create_cleanup_app
    return TestClient(create_cleanup_app(
        cleanup_port=port, admin_authenticated=lambda _request: True))


def test_cleanup_execute_empty_body_takes_drill_path():
    """红能力（N2-01 cleanup）：空 POST（非 JSON 空体）→ 旧默认
    dry_run=False 落真删路径（断言 True 红）；新形态空 body→演习路径，
    且 port.execute 以 dry_run=True 被调。"""
    port = _SpyCleanupPort()
    client = _cleanup_client(port)
    response = client.post("/v1/api/admin/cleanup/execute",
                            content=b"", headers={"content-type": "text/plain"})
    assert response.status_code == 200
    assert len(port.execute_calls) == 1
    _today, _operator, dry_run = port.execute_calls[0]
    assert dry_run is True
    assert response.json()["dry_run"] is True


def test_cleanup_execute_empty_json_object_takes_drill_path():
    """红能力：JSON 空对象 {}（缺 dry_run 键）→ 旧落真删（红）；新演习。"""
    port = _SpyCleanupPort()
    client = _cleanup_client(port)
    response = client.post("/v1/api/admin/cleanup/execute", json={})
    assert response.status_code == 200
    assert port.execute_calls[0][2] is True


@pytest.mark.parametrize("value", [0, None, ""])
def test_cleanup_execute_non_strict_false_values_take_drill_path(value):
    """红能力（fail-closed）：dry_run=0/null/"" 非严格布尔 false——旧
    bool() 折叠为 False 落真删路径（红）；新形态仅显式 false 才真删。"""
    port = _SpyCleanupPort()
    client = _cleanup_client(port)
    response = client.post("/v1/api/admin/cleanup/execute",
                            json={"dry_run": value})
    assert response.status_code == 200
    assert port.execute_calls[0][2] is True


def test_cleanup_execute_explicit_false_still_deletes():
    """绿守卫（新旧皆绿）：显式 dry_run=false → 真删路径语义不变。"""
    port = _SpyCleanupPort()
    client = _cleanup_client(port)
    response = client.post("/v1/api/admin/cleanup/execute",
                            json={"today": "2026-09-25", "operator": "ops",
                                  "dry_run": False})
    assert response.status_code == 200
    assert port.execute_calls == [("2026-09-25", "ops", False)]


def test_cleanup_execute_explicit_true_takes_drill_path():
    """绿守卫（新旧皆绿）：显式 dry_run=true → 演习路径不变。"""
    port = _SpyCleanupPort()
    client = _cleanup_client(port)
    response = client.post("/v1/api/admin/cleanup/execute",
                            json={"today": "2026-09-25", "operator": "ops",
                                  "dry_run": True})
    assert response.status_code == 200
    assert port.execute_calls == [("2026-09-25", "ops", True)]
