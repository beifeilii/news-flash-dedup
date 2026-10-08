# -*- coding: utf-8 -*-
"""W-A 族③红测（建议落 tests/unit/test_wina_family3_recovery.py）。

红态锚：③(a) batch_admission.py 无 release_quarantine；③(b) batch_collector.py
无 reset；③(c) lifecycle.py 无 execute_expired_pending_cleanup 且
delete_by_query 不验 failures/version_conflicts/timed_out；③(d)
commit/es_store.py:157-160 裸 RuntimeError 无幂等出口；③(e)
FakeDeliveryLease 无 record_id、es_store.py:317 落点单例属性；③(f)
dispatcher.py 无 scan_expired_delivering。
"""
from __future__ import annotations

import hashlib
from datetime import date, datetime, timedelta, timezone

import pytest

from news_flash_dedup.admission import (
    AdmissionConflict,
    AdmissionUnknown,
    BUSINESS_ZONE,
    AdmissionRequest,
)
from news_flash_dedup.batch_admission import (
    BatchAdmissionCoordinator,
    BatchLimits,
)
from news_flash_dedup.batch_collector import (
    BatchAdmissionCollector,
    CollectorRejected,
)
from news_flash_dedup.batch_es_store import PermanentBulkError

from test_batch_admission import BatchMemoryStore
from test_quarantine import _make_request, _clock_at

CONTROL = "news-dedup-control-v1"
BATCH_HEAD = "batch_admission_head"


# ==================== ③(a) quarantine_release ====================

def _quarantined_expired(store):
    """D 日受理 1 条 → D+8 materialize 触发 logical_expiry 隔离；返回 d8 服务。"""
    d_start = datetime(2026, 9, 24, 12, 0, tzinfo=BUSINESS_ZONE).astimezone(timezone.utc)
    d8 = datetime(2026, 10, 2, 0, 0, 0, tzinfo=BUSINESS_ZONE).astimezone(timezone.utc)
    service = BatchAdmissionCoordinator(
        store, owner_id="wa3a-1", owner_isolated=lambda: True,
        limits=BatchLimits(), clock=_clock_at(d_start))
    service.accept_batch([_make_request("default", "9401-1", "9401-1",
                                        "D 日过期正文", d_start)])
    service_d8 = BatchAdmissionCoordinator(
        store, owner_id="wa3a-1", owner_isolated=lambda: True,
        limits=BatchLimits(), clock=_clock_at(d8))
    with pytest.raises(AdmissionConflict, match="expired batch is quarantined"):
        service_d8.materialize_oldest()
    return service_d8, d8


def _quarantine_record(store):
    return store.get(CONTROL, BATCH_HEAD)["source"]["checkpoint"]["batch_quarantine"]


def test_r3a1_release_drain_clears_quarantine_and_restores_pipeline():
    store = BatchMemoryStore()
    service_d8, d8 = _quarantined_expired(store)
    quarantine = _quarantine_record(store)
    record = service_d8.release_quarantine(
        operator="ops-1", disposition="drain",
        expected_batch_id=quarantine["batch_id"])
    assert record["operator"] == "ops-1" and record["disposition"] == "drain"
    assert record["quarantine"]["reason"] == "logical_expiry"
    head = store.get(CONTROL, BATCH_HEAD)["source"]
    assert "batch_quarantine" not in head["checkpoint"]
    assert head["pending"]["batches"] == []
    assert head["last_materialized_seq"] == 1      # 单调序号越过清槽批
    assert head["last_allocated_seq"] == 1
    cleanup = head["checkpoint"]["lifecycle_cleanup"]
    assert cleanup["kind"] == "quarantine_drain"
    assert cleanup["operator"] == "ops-1"
    assert "text" not in cleanup                   # 无正文检查点
    assert head["checkpoint"]["quarantine_release"]["operator"] == "ops-1"
    # 管道恢复：新受理/物化不再被隔离闸拦
    receipts = service_d8.accept_batch([_make_request("default", "9402-1", "9402-1",
                                                      "D+8 新文", d8)])
    assert receipts[0].arrival_seq == 2


def test_r3a2_drain_never_rematermaterializes_expired_text():
    """10 L459：drain 后 recover 越过清槽批，过期正文零物化（零业务写）。"""
    store = BatchMemoryStore()
    service_d8, _ = _quarantined_expired(store)
    quarantine = _quarantine_record(store)
    service_d8.release_quarantine(operator="ops-1", disposition="drain",
                                  expected_batch_id=quarantine["batch_id"])
    bulk_before = store.bulk_calls
    watermark = service_d8.recover()
    assert watermark == 1
    assert store.bulk_calls == bulk_before == 0    # 无任何物化写


def test_r3a3_expired_quarantine_retry_disposition_refused():
    """过期隔离必须 drain——retry=过期正文重新物化路径，代码闸死。"""
    store = BatchMemoryStore()
    service_d8, _ = _quarantined_expired(store)
    with pytest.raises(AdmissionConflict, match="drain"):
        service_d8.release_quarantine(operator="ops-1", disposition="retry")
    assert "batch_quarantine" in store.get(CONTROL, BATCH_HEAD)["source"]["checkpoint"]


def test_r3a4_release_requires_quarantine_named_batch_and_isolation():
    store = BatchMemoryStore()
    service = BatchAdmissionCoordinator(
        store, owner_id="wa3a-4", owner_isolated=lambda: True,
        limits=BatchLimits(), clock=_clock_at(datetime(2026, 10, 7, tzinfo=timezone.utc)))
    with pytest.raises(AdmissionConflict, match="quarantine"):
        service.release_quarantine(operator="ops-1", disposition="drain")
    service_d8, _ = _quarantined_expired(store := BatchMemoryStore())
    with pytest.raises(AdmissionConflict, match="batch"):      # 点名不符
        service_d8.release_quarantine(operator="ops-1", disposition="drain",
                                      expected_batch_id="0" * 64)
    with pytest.raises(ValueError):                            # disposition 闭集外
        service_d8.release_quarantine(operator="ops-1", disposition="ignore")
    # 单活证明缺失（owner_isolated=False）→ 拒
    service_d8.owner_isolated = lambda: False
    with pytest.raises(AdmissionConflict, match="isolation"):
        service_d8.release_quarantine(operator="ops-1", disposition="drain")


def test_r3a5_retry_disposition_on_bulk_error_quarantine():
    """retry 通路钉（特征红+语义守卫）：非过期隔离（permanent_bulk_error）
    → retry 合法，批次保留重试成功（现役：无 release_quarantine=红）。"""
    class _PermFailStore(BatchMemoryStore):
        fail_once = True
        def bulk_create(self, documents):
            if self.fail_once:
                self.fail_once = False
                raise PermanentBulkError("simulated poison record")
            super().bulk_create(documents)

    store = _PermFailStore()
    now = datetime(2026, 10, 7, 10, 0, tzinfo=timezone.utc)
    service = BatchAdmissionCoordinator(
        store, owner_id="wa3a-5", owner_isolated=lambda: True,
        limits=BatchLimits(), clock=_clock_at(now))
    service.accept_batch([_make_request("default", "9501-1", "9501-1",
                                        "正文", now)])
    # 实现窗补正（设计稿夹具类型面偏差，ew-impl-notes ⑦偏差在案）：无
    # lost_ack 钩时 _quarantine CAS 落旗成功即抛 AdmissionConflict
    # （AdmissionUnknown 仅落旗确认丢失路径冒出——test_batch_admission.py
    # :617-625 先例）；钉面意图="bulk 失败→隔离置位"，两类型皆现役受
    # sanction 的冒出形，后段断言（reason/retry 通路）零改动。
    with pytest.raises((AdmissionUnknown, AdmissionConflict)):
        service.materialize_oldest()               # bulk 失败 → quarantine
    assert _quarantine_record(store)["reason"] == "permanent_bulk_error"
    service.release_quarantine(operator="ops-2", disposition="retry")
    head = store.get(CONTROL, BATCH_HEAD)["source"]
    assert len(head["pending"]["batches"]) == 1    # 批次保留
    assert "batch_quarantine" not in head["checkpoint"]
    assert service.materialize_oldest() == 1       # 根因已除，重试成功


# ==================== ③(b) collector reset ====================

def _collector(service):
    return BatchAdmissionCollector(service, max_wait_seconds=0.005,
                                   max_queue_items=8, request_timeout_seconds=2.0)


def test_r3b1_reset_rebuilds_worker_after_unknown_shutdown():
    store = BatchMemoryStore()
    now = datetime(2026, 10, 7, 10, 0, tzinfo=timezone.utc)
    service = BatchAdmissionCoordinator(
        store, owner_id="wa3b-1", owner_isolated=lambda: True,
        limits=BatchLimits(), clock=_clock_at(now))
    collector = _collector(service)
    original = service.accept_batch
    service.accept_batch = lambda requests: (_ for _ in ()).throw(
        OSError("simulated confirmation lost"))
    with pytest.raises(AdmissionUnknown):
        collector.accept(_make_request("default", "9601-1", "9601-1", "正文甲", now))
    collector.close()
    snap = collector.snapshot()
    assert snap.failed is True and snap.closed is True
    with pytest.raises(CollectorRejected):
        collector.accept(_make_request("default", "9602-1", "9602-1", "正文乙", now))
    # 根因修复 → reset 重建（unknown 批不重放：9601-1 仍需上层决定重提）
    service.accept_batch = original
    collector.reset()
    assert collector.snapshot().worker_alive is True
    assert collector.snapshot().resets == 1
    receipt = collector.accept(_make_request("default", "9603-1", "9603-1", "正文丙", now))
    assert receipt.arrival_seq >= 1
    collector.close()
    assert collector.snapshot().resets == 1        # 计数审计连续


def test_r3b2_reset_requires_closed_state():
    store = BatchMemoryStore()
    now = datetime(2026, 10, 7, 10, 0, tzinfo=timezone.utc)
    service = BatchAdmissionCoordinator(
        store, owner_id="wa3b-2", owner_isolated=lambda: True,
        limits=BatchLimits(), clock=_clock_at(now))
    collector = _collector(service)
    with pytest.raises(ValueError, match="closed"):
        collector.reset()                          # 活着的收集器禁 reset
    collector.close()


def test_r3b3_normal_lifecycle_guard_and_resets_default():
    """生命周期守卫+新计数钉：accept→flush→close 正常生命周期逐字节不变
    （守卫面）；CollectorSnapshot.resets 默认 0（新字段钉，现役无此字段=红）。"""
    store = BatchMemoryStore()
    now = datetime(2026, 10, 7, 10, 0, tzinfo=timezone.utc)
    service = BatchAdmissionCoordinator(
        store, owner_id="wa3b-3", owner_isolated=lambda: True,
        limits=BatchLimits(), clock=_clock_at(now))
    collector = _collector(service)
    receipt = collector.accept(_make_request("default", "9701-1", "9701-1", "正文", now))
    assert receipt.arrival_seq == 1
    collector.close()
    snap = collector.snapshot()
    assert snap.resets == 0 and snap.failed is False and snap.closed is True


# ==================== ③(c) lifecycle 控制文档面 ====================

from news_flash_dedup.lifecycle import (
    CleanupAuditLog,
    execute_expired_document_cleanup,
)


@pytest.fixture()
def epc():
    """execute_expired_pending_cleanup 句柄；未实现时显式红（非收集错误）。

    （test_pair_alignment_normalize.py:45-53 `nd` 夹具同姿势——未实现件
    惰性导入，保护同文件 ③(a)/③(b) 先行落地的收集面。）"""
    try:
        from news_flash_dedup.lifecycle import execute_expired_pending_cleanup
    except ImportError:
        pytest.fail("③(c)：lifecycle.execute_expired_pending_cleanup 尚未实现，"
                    "本测试当前红属预期")
    return execute_expired_pending_cleanup


class _LifecycleFakeClient:
    """lifecycle 面最小双倍体：get（realtime）/count/delete_by_query/indices。"""

    def __init__(self):
        self.docs = {}            # (index, id) -> {"_source":..., "_seq_no":0, "_primary_term":1}
        self.present_indices = set()
        self.count_response = {"count": 0, "_shards": {"total": 1, "successful": 1, "failed": 0}}
        self.dbq_responses = []   # 依次返回
        self.dbq_calls = []
        self.indices = type("Idx", (), {
            "exists": lambda s, *, index: (index in self.present_indices
                                           or any(i == index for i, _ in self.docs)),
            "refresh": lambda s, *, index: {"_shards": {"failed": 0}},
        })()

    def seed_index(self, index):
        """声明索引在场（plan_expired_document_cleanup 的 is_present 闸）。"""
        self.present_indices.add(index)

    def get(self, *, index, id, realtime=True):
        doc = self.docs.get((index, id))
        if doc is None:
            import elasticsearch as es
            from elastic_transport import ApiResponseMeta, NodeConfig
            raise es.NotFoundError("nf", ApiResponseMeta(
                404, "1.1", {}, 0, NodeConfig("http", "localhost", 9200)),
                {"found": False})
        return {"_index": index, "_id": id, "found": True,
                "_source": dict(doc["_source"]),
                "_seq_no": doc["_seq_no"], "_primary_term": doc["_primary_term"]}

    def count(self, *, index, body):
        return dict(self.count_response)

    def delete_by_query(self, *, index, body, refresh=False):
        self.dbq_calls.append(index)
        if self.dbq_responses:
            return self.dbq_responses.pop(0)
        return {"took": 1, "timed_out": False, "total": 0, "deleted": 0,
                "version_conflicts": 0, "failures": [],
                "_shards": {"total": 1, "successful": 1, "failed": 0}}

    # 实现窗补正（设计稿双倍体写 API 缺位，ew-impl-notes ⑧偏差在案）：
    # ③(c)-1 清槽唯一写=控制文档 CAS（client.index 带 if_seq_no/if_primary_term）
    # ——双倍体按 ES 语义补本方法（版本不符→ConflictError；不放宽任何断言）。
    def index(self, *, index, id, document, if_seq_no=None, if_primary_term=None,
              refresh=False):
        key = (index, id)
        old = self.docs.get(key)
        if old is not None and if_seq_no is not None:
            if (old["_seq_no"], old["_primary_term"]) != (if_seq_no, if_primary_term):
                import elasticsearch as es
                from elastic_transport import ApiResponseMeta, NodeConfig
                raise es.ConflictError("cas", ApiResponseMeta(
                    409, "1.1", {}, 0, NodeConfig("http", "localhost", 9200)),
                    {"error": {"type": "version_conflict_engine_exception"}})
        self.docs[key] = {"_source": dict(document),
                          "_seq_no": (old["_seq_no"] + 1) if old else 0,
                          "_primary_term": (old["_primary_term"] if old else 1)}
        return {"result": "updated" if old else "created"}


def _audit():
    return CleanupAuditLog(operator="ops-lc")


def test_r3c1_delete_by_query_failures_not_silent():
    """A1-014：delete_by_query 返回 failures 非空 → 报错+audit ok=False（不得记成功）。"""
    client = _LifecycleFakeClient()
    client.seed_index("p01-batch-x-news-dedup-requests-v1")
    client.count_response = {"count": 2, "_shards": {"failed": 0}}
    # 实现窗补正（设计稿夹具欠一发，ew-impl-notes ⑧偏差在案）：两轮执行
    # 皆须 failures 报错（第一轮钉报错、第二轮钉 audit ok=False）——队列
    # 补第二发同形坏响应；断言面零改动。
    client.dbq_responses = [
        {"timed_out": False, "deleted": 1, "version_conflicts": 0,
         "failures": [{"shard": 0, "reason": "forbidden"}]},
        {"timed_out": False, "deleted": 1, "version_conflicts": 0,
         "failures": [{"shard": 0, "reason": "forbidden"}]},
    ]
    with pytest.raises(RuntimeError, match="failures"):
        execute_expired_document_cleanup(client, datetime(2026, 10, 8, tzinfo=timezone.utc),
                                         _audit(), index_prefix="p01-batch-x-",
                                         kinds=("requests",))
    audit = _audit()
    with pytest.raises(RuntimeError):
        execute_expired_document_cleanup(client, datetime(2026, 10, 8, tzinfo=timezone.utc),
                                         audit, index_prefix="p01-batch-x-", kinds=("requests",))
    assert any(run["ok"] is False for run in audit.document_cleanup_runs)


def test_r3c2_delete_by_query_version_conflicts_bounded_retry():
    """A1-014：version_conflicts 触发有界重试；持续冲突→报错不吞。"""
    client = _LifecycleFakeClient()
    client.seed_index("p01-batch-x-news-dedup-requests-v1")
    client.count_response = {"count": 3, "_shards": {"failed": 0}}
    client.dbq_responses = [
        {"timed_out": False, "deleted": 1, "version_conflicts": 2, "failures": []},
        {"timed_out": False, "deleted": 1, "version_conflicts": 2, "failures": []},
        {"timed_out": False, "deleted": 1, "version_conflicts": 2, "failures": []},
        {"timed_out": False, "deleted": 1, "version_conflicts": 2, "failures": []},
    ]
    with pytest.raises(RuntimeError, match="version_conflicts"):
        execute_expired_document_cleanup(client, datetime(2026, 10, 8, tzinfo=timezone.utc),
                                         _audit(), index_prefix="p01-batch-x-",
                                         kinds=("requests",))
    assert len(client.dbq_calls) <= 4              # 1+最多 3 次重试（有界）


def test_r3c3_delete_by_query_timed_out_rejected():
    client = _LifecycleFakeClient()
    client.seed_index("p01-batch-x-news-dedup-requests-v1")
    client.count_response = {"count": 1, "_shards": {"failed": 0}}
    client.dbq_responses = [{"timed_out": True, "deleted": 1, "version_conflicts": 0,
                             "failures": []}]
    with pytest.raises(RuntimeError, match="timed_out"):
        execute_expired_document_cleanup(client, datetime(2026, 10, 8, tzinfo=timezone.utc),
                                         _audit(), index_prefix="p01-batch-x-",
                                         kinds=("requests",))


def test_r3c4_delete_by_query_clean_response_green_guard():
    """绿守卫：干净响应（deleted 计数）语义逐字节不变。"""
    client = _LifecycleFakeClient()
    client.seed_index("p01-batch-x-news-dedup-requests-v1")
    client.count_response = {"count": 2, "_shards": {"failed": 0}}
    client.dbq_responses = [{"timed_out": False, "deleted": 2, "version_conflicts": 0,
                             "failures": []}]
    audit = _audit()
    report = execute_expired_document_cleanup(
        client, datetime(2026, 10, 8, tzinfo=timezone.utc), audit,
        index_prefix="p01-batch-x-", kinds=("requests",))
    assert report["deleted"]["p01-batch-x-news-dedup-requests-v1"] == 2


def _seed_head_with_expired_pending(client, *, materialized: bool):
    """控制文档=单槽 admission_head，pending 已过期；materialized=True 时四文档在场。"""
    now = datetime(2026, 10, 8, tzinfo=timezone.utc)
    expired = (now - timedelta(days=1)).isoformat().replace("+00:00", "Z")
    record_id = hashlib.sha256("正文".encode("utf-8")).hexdigest()
    pending = {"version": "admission_pending_v1", "record_id": record_id,
               "item_id": "9901-1", "request_id": "9901-1",
               "scope_id": "default", "business_date": "2026-10-01",
               "arrival_seq": 7, "expires_at": expired,
               "text_hash": hashlib.sha256("正文".encode("utf-8")).hexdigest(),
               "pipeline_version": "v1", "embedding_space_id": "v3",
               "delivery_route_ref": "r", "trace_id": "t",
               "schema_version": "1", "received_at": expired,
               "raw_hash": hashlib.sha256("正文".encode("utf-8")).hexdigest()}
    client.docs[(CONTROL, "admission_head")] = {
        "_source": {"kind": "admission", "owner_id": "w1", "version": 1,
                    "last_allocated_seq": 7, "last_materialized_seq": 6,
                    "pending": pending, "checkpoint": {}, "updated_at": expired},
        "_seq_no": 0, "_primary_term": 1}
    if materialized:
        items = "news-dedup-items-v1-2026.10.01"
        requests = "news-dedup-requests-v1"
        for index, key in ((items, record_id),
                           (requests, "default|9901-1"),
                           (requests, "default|item|9901-1"),
                           (requests, "seq:7")):
            client.docs[(index, key)] = {"_source": {"kind": "x"},
                                         "_seq_no": 0, "_primary_term": 1}
    return record_id


def test_r3c5_expired_pending_cleanup_clears_slot_with_checkpoint(epc):
    """10 L459 单槽面：过期 pending 清槽+无正文检查点+单调序号保留+零正文物化。"""
    client = _LifecycleFakeClient()
    record_id = _seed_head_with_expired_pending(client, materialized=True)
    now = datetime(2026, 10, 8, tzinfo=timezone.utc)
    audit = _audit()
    report = epc(client, now, audit, isolation_confirmed=True)
    assert report["cleaned"] is True
    head = client.docs[(CONTROL, "admission_head")]["_source"]
    assert head["pending"] is None
    assert head["last_materialized_seq"] == 7      # max(6, 7) 单调推进
    assert head["last_allocated_seq"] == 7         # 分配序号不回退
    checkpoint = head["checkpoint"]["lifecycle_cleanup"]
    assert checkpoint["kind"] == "expired_pending_cleanup"
    assert checkpoint["record_id"] == record_id
    assert checkpoint["operator"] == "ops-lc"
    assert checkpoint["materialized_evidence"] == {
        "main": True, "request": True, "item": True, "seq": True}
    assert "text" not in checkpoint                # 无正文检查点


def test_r3c6_pending_cleanup_requires_isolation_and_expiry(epc):
    client = _LifecycleFakeClient()
    _seed_head_with_expired_pending(client, materialized=False)
    now = datetime(2026, 10, 8, tzinfo=timezone.utc)
    with pytest.raises(ValueError, match="isolation"):
        epc(client, now, _audit())                           # 默认未证实
    # 未过期 pending → no-op
    future = (now + timedelta(days=1)).isoformat().replace("+00:00", "Z")
    client.docs[(CONTROL, "admission_head")]["_source"]["pending"]["expires_at"] = future
    report = epc(client, now, _audit(), isolation_confirmed=True)
    assert report["cleaned"] is False
    assert client.docs[(CONTROL, "admission_head")]["_source"]["pending"] is not None


def test_r3c7_pending_cleanup_dry_run_zero_writes(epc):
    client = _LifecycleFakeClient()
    _seed_head_with_expired_pending(client, materialized=False)
    now = datetime(2026, 10, 8, tzinfo=timezone.utc)
    before = client.docs[(CONTROL, "admission_head")]["_source"]["pending"]
    report = epc(client, now, _audit(), isolation_confirmed=True, dry_run=True)
    assert report["would_clean"] is True
    assert client.docs[(CONTROL, "admission_head")]["_source"]["pending"] == before


# ==================== ③(d) commit 真层 N36 幂等对拍 ====================

from news_flash_dedup.es_client import APPROVED_UAT_HOST
from news_flash_dedup.commit.es_store import RealESCommitStore, RealESConfig
from news_flash_dedup.persist.divergence import AuditDivergenceError
from b4_fake_es import FakeESClient

DAY = date(2026, 10, 7)


@pytest.fixture
def p17_env(monkeypatch):
    monkeypatch.setenv("P17_CONFIRM_UAT", "1")
    monkeypatch.setenv("DEPLOY_ENV", "test")
    monkeypatch.setenv("TEST_ES_HOST", APPROVED_UAT_HOST)
    for key in ("PROD_ES_HOST", "PROD_ES_PORT", "PROD_ES_USER", "PROD_ES_PASS",
                "PROD_ES_SCHEME", "ALLOW_PROD_WRITE"):
        monkeypatch.delenv(key, raising=False)
    client = FakeESClient()
    store = RealESCommitStore(client, RealESConfig(run_uuid="wa3d", business_date=DAY))
    return client, store


def _audit_record(cid, note="audit"):
    return {"comparison_id": cid, "kind": "audit", "note": note,
            "decision": "SUPPRESSED_DUPLICATE"}


def test_r3d1_bulk_conflict_same_content_idempotent(p17_env):
    """bulk 409 + 同内容已存 → 幂等返回 ids（崩溃重放确认丢失场景）。"""
    client, store = p17_env
    record = _audit_record("cmp-1")
    first = store.write_audit_documents([record])
    assert first == ["cmp-1"]
    second = store.write_audit_documents([_audit_record("cmp-1")])   # 重放
    assert second == ["cmp-1"]
    audit_docs = [key for key in client.docs if key[0] == store.config.audits_index]
    assert len(audit_docs) == 1


def test_r3d2_bulk_conflict_divergent_raises(p17_env):
    """bulk 409 + 内容分歧 → AuditDivergenceError 带键明细（fail-closed）。"""
    client, store = p17_env
    store.write_audit_documents([_audit_record("cmp-2", note="original")])
    with pytest.raises(AuditDivergenceError, match="cmp-2"):
        store.write_audit_documents([_audit_record("cmp-2", note="tampered")])


def test_r3d3_bulk_conflict_missing_reraises_original(p17_env):
    """bulk error 但核验读缺失 → 原 RuntimeError 重抛不掩盖（失败即停）。"""
    client, store = p17_env
    # 伪造：bulk 返回 error 但 GET 缺失（瘦 spy/不一致视图）——
    # 通过 monkeypatch bulk 直接回 error 项构造
    def _fake_bulk(*, operations, refresh=False):
        return {"errors": True, "items": [{"create": {
            "_index": store.config.audits_index, "_id": "cmp-3", "status": 500,
            "error": {"type": "es_rejected_execution_exception",
                      "reason": "simulated partial failure"}}}]}
    client.bulk = _fake_bulk
    with pytest.raises(RuntimeError, match="audit bulk write failed"):
        store.write_audit_documents([_audit_record("cmp-3")])


def test_r3d4_clean_bulk_green_guard(p17_env):
    """绿守卫：干净 bulk 语义逐字节不变（ids 顺序同输入、审计不重写）。"""
    client, store = p17_env
    ids = store.write_audit_documents([_audit_record("cmp-4"),
                                       _audit_record("cmp-5")])
    assert ids == ["cmp-4", "cmp-5"]


# ==================== ③(e) 租约 record_id 自证归属 ====================

from news_flash_dedup.delivery.fake_store import (
    FakeDeliveryLease,
    FakeDeliveryStore,
)


def _lease(owner, record_id="", generation=1, until="2026-10-07T11:00:00Z",
           payload_hash="h" * 64):
    return FakeDeliveryLease(
        owner_id=owner, owner_generation=generation, lease_until=until,
        attempt_id=f"attempt-{owner}", round=1, result_version=1,
        event_id=f"event-{owner}", payload_hash=payload_hash,
        route_ref="route-v1", record_id=record_id)


def test_r3e1_lease_carries_record_id_field():
    """FakeDeliveryLease 第 10 字段 record_id（默认空=过渡兼容）。"""
    lease = _lease("w1", record_id="rec-a")
    assert lease.record_id == "rec-a"
    legacy = FakeDeliveryLease(             # 旧构造形（无 record_id 键）仍合法
        owner_id="w1", owner_generation=1, lease_until="2026-10-07T11:00:00Z",
        attempt_id="a", round=1, result_version=1, event_id="e",
        payload_hash="h" * 64, route_ref="r")
    assert legacy.record_id == ""


# ----- R3E2 真层交错钉（RealESP20Store over FakeESClient，禁集群） -----

from news_flash_dedup.delivery.es_store import (
    RealESP20Config,
    RealESP20Store,
)


@pytest.fixture
def p20_env(monkeypatch):
    monkeypatch.setenv("P20_CONFIRM_UAT", "1")
    monkeypatch.setenv("DEPLOY_ENV", "test")
    monkeypatch.setenv("TEST_ES_HOST", APPROVED_UAT_HOST)
    for key in ("PROD_ES_HOST", "PROD_ES_PORT", "PROD_ES_USER", "PROD_ES_PASS",
                "PROD_ES_SCHEME", "ALLOW_PROD_WRITE"):
        monkeypatch.delenv(key, raising=False)
    client = FakeESClient()
    store = RealESP20Store(client, RealESP20Config(run_uuid="wa3e",
                                                   business_date=DAY))
    return client, store


def _p20_source(record_id):
    body = ""
    return {
        "record_id": record_id, "item_id": f"item-{record_id}",
        "scope_id": "default", "business_date": "2026-10-07",
        "arrival_seq": 1, "delivery_state": "pending",
        "delivery_deadline_at": "2099-01-01T00:00:00Z",
        "next_delivery_at": "2026-10-07T10:00:00Z",
        "callback_attempts": 0, "round_attempts": 0, "round": 1,
        "callback_body": body,
        "callback_body_hash": hashlib.sha256(body.encode("utf-8")).hexdigest(),
        "route_ref": "route-v1", "task_state": "succeeded",
        "audit_complete": True, "vector_state": "ready",
    }


def test_r3e2_real_store_interleaved_claim_targets_lease_record_id(p20_env):
    """交错场景：claim_read(A)→claim_read(B)→update_lease(leaseA 携 record_id=A)
    → 租约落 A 不落 B（现役：FakeDeliveryLease 无 record_id 键、
    es_store.py:317 落点=_last_claim_read=B 错写=红）。"""
    client, store = p20_env
    for rid in ("rec-a", "rec-b"):
        client.put(store.config.items_index, rid, _p20_source(rid))
    store.main_records.claim_read("rec-a")
    store.main_records.claim_read("rec-b")          # 交错：领取上下文被 B 覆盖
    lease_a = _lease("w1", record_id="rec-a",
                     payload_hash=hashlib.sha256("".encode("utf-8")).hexdigest())
    store.update_lease(lease_a)
    doc_a = client.source(store.config.items_index, "rec-a")
    doc_b = client.source(store.config.items_index, "rec-b")
    assert doc_a.get("delivery_lease", {}).get("owner_id") == "w1", (
        "租约必须落在 record_id 自证的 A")
    assert "delivery_lease" not in doc_b, "租约不得错写 B"


def test_r3e3_interleaved_claim_with_record_id_not_rejected_green_guard():
    """绿守卫：携 record_id 的合法交错不得被误拒（落点自证归属即安全）。"""
    store = FakeDeliveryStore()
    lease_a = _lease("w1", record_id="rec-a")
    key = store.update_lease(lease_a)
    assert key == "w1"
    assert store.lease_index["w1"].record_id == "rec-a"
    assert store.cas_calls[-1]["record_id"] == "rec-a"


# ==================== ③(f) delivering 到期重扫 ====================

from news_flash_dedup.delivery.dispatcher import scan_pending
from news_flash_dedup.delivery.fake_store import FakeDeliveryRecord


@pytest.fixture()
def sed():
    """scan_expired_delivering 句柄；未实现时显式红（`nd` 夹具同姿势）。

    注：③(f) 依赖 ③(e) record_id 字段先行（落地次序附录 A 序 3→4）——
    本夹具红在 ③(e) 落地前由 `_lease(record_id=...)` TypeError 先显。"""
    try:
        from news_flash_dedup.delivery.dispatcher import scan_expired_delivering
    except ImportError:
        pytest.fail("③(f)：dispatcher.scan_expired_delivering 尚未实现，"
                    "本测试当前红属预期")
    return scan_expired_delivering


def _record(record_id, state, *, expires_at=""):
    return FakeDeliveryRecord(
        record_id=record_id, item_id=f"item-{record_id}", scope_id="default",
        business_date="2026-10-07", arrival_seq=1,
        delivery_state=state,
        delivery_deadline_at="2026-10-07T12:00:00Z",
        next_delivery_at="2026-10-07T10:00:00Z",
        callback_attempts=0, round_attempts=0, round=1,
        callback_body="{}", payload_hash="h" * 64, route_ref="route-v1",
        vector_state="ready", audit_complete=True, expires_at=expires_at)


def test_r3f1_expired_delivering_lease_rescanned(sed):
    """11 L166：delivering+租约到期 → 重扫可见（现役：永久滞留=红）。"""
    store = FakeDeliveryStore()
    rec = _record("r1", "delivering")
    store.main_records["r1"] = rec
    store.update_lease(_lease("w1", record_id="r1",
                              until="2026-10-07T09:00:00Z"))   # 已过期
    found = sed(store, now="2026-10-07T10:00:00Z")
    assert found == ["r1"]


def test_r3f2_live_delivering_and_pending_untouched_green_guard(sed):
    """绿守卫：租约未到期 delivering 不收录；scan_pending 语义零扰动。"""
    store = FakeDeliveryStore()
    store.main_records["r1"] = _record("r1", "delivering")
    store.main_records["r2"] = _record("r2", "pending")
    store.update_lease(_lease("w1", record_id="r1",
                              until="2026-10-07T11:00:00Z"))   # 未到期
    assert sed(store, now="2026-10-07T10:00:00Z") == []
    assert scan_pending(store, now="2026-10-07T10:30:00Z") == ["r2"]


def test_r3f3_union_covers_both_pending_and_delivering(sed):
    """11 L166 并集钉：同店并存到期 pending+到期 delivering → 两扫描并集覆盖。"""
    store = FakeDeliveryStore()
    store.main_records["r1"] = _record("r1", "delivering")
    store.main_records["r2"] = _record("r2", "pending")
    store.update_lease(_lease("w1", record_id="r1", until="2026-10-07T09:00:00Z"))
    now = "2026-10-07T10:00:00Z"
    union = set(scan_pending(store, now=now)) | set(sed(store, now=now))
    assert union == {"r1", "r2"}               # 不能只恢复 pending


def test_r3f4_delivering_without_lease_skipped_with_stats(sed):
    """不一致状态（delivering 无租约）→ 跳闸+skip_stats 可观测，不静默。"""
    store = FakeDeliveryStore()
    store.main_records["r1"] = _record("r1", "delivering")
    stats = {}
    found = sed(store, now="2026-10-07T10:00:00Z", skip_stats=stats)
    assert found == []
    assert stats.get("delivering_without_lease") == 1


def test_r3f5_now_must_be_explicit(sed):
    """恢复面不接受零校验形态：now 空/naive → ValueError。"""
    store = FakeDeliveryStore()
    with pytest.raises(ValueError):
        sed(store, now="")
    with pytest.raises(ValueError):
        sed(store, now="2026-10-07 10:00")
