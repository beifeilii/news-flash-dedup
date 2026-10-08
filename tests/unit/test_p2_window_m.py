"""P2 追加批（窗口M）：D30 处置追加 + B-3 遗留，五项硬化测试。

红能力说明（全部关键断言针对新行为，旧代码必红）：
1. M-10 reopen_round 截止闸：旧 reopen_round 不核"仍在原截止内"——
   过期 delivered/exhausted 照样重开（pytest.raises 红）；
2. 伪 CAS 两处：旧 persist/main_record_persist.cas_main_record 从不比对
   expected 版本（(0,0) 重创建/陈旧版本更新均静默放行）；旧
   persist_next_delivery 读-改-写裸 dict 赋值无版本条件（并发写不被拒）
   ——冲突断言全红（新错误类不存在亦红）；
3. _last_claim_read 隐式上下文：旧 _MainRecordsView.__getitem__ 任何读
   都改写领取上下文——"普通读不写上下文 / 普通读后 update_lease 拒领"
   两条断言红；
4. 回调 URL 零校验：旧 _OPENER 保留 File/FTP/Data handler 且无 scheme 闸
   ——file:// 等 scheme 不抛 P20SendError（pytest.raises 红）；
5. F4-5 .body 回退异型 fail-open：异型响应（非 Mapping 且无 Mapping
   .body）被静默判空，fresh-namespace 闸形同虚设——pytest.raises 红。

绿守卫钉住兼容面：健康记录重开/领取/发送/(0,0) 首创建/快照一致写照常。

窗口X 并线适配（移植自主仓外 P2 副本）：本文件零适配——所触接口
（FakeP18Store/main_record_persist/RealESP20Store 鸭式面/
provision_isolated_indices/delivery sealed 函数）主仓签名与副本同一。
"""

from __future__ import annotations

import dataclasses
import datetime
import hashlib
from datetime import date

import pytest

from news_flash_dedup.delivery import (
    FakeDeliveryRecord,
    FakeDeliveryStore,
    P20ReceiveError,
    P20SendError,
    persist_next_delivery,
    receive,
    reopen_round,
    send_callback,
)
from news_flash_dedup.delivery import es_store as p20_es_store
from news_flash_dedup.delivery.es_store import (
    P20ClaimPreconditionError,
    RealESP20Config,
    RealESP20Store,
)
from news_flash_dedup.delivery.fake_store import FakeDeliveryLease
from news_flash_dedup.es_admission_index import provision_isolated_indices
from news_flash_dedup.persist import main_record_persist
from news_flash_dedup.persist.fake_store import FakeMainRecordV1, FakeP18Store

UTC = datetime.timezone.utc


def _iso(dt: datetime.datetime) -> str:
    return dt.isoformat()


# ---------- 共用构造 ----------

def _delivery_record(record_id="r-1", *, delivery_state="delivered",
                     delivery_deadline_at=None, next_delivery_at=None,
                     expires_at="", round=1, round_attempts=12,
                     callback_attempts=5,
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
        callback_attempts=callback_attempts, round_attempts=round_attempts,
        round=round, callback_body=callback_body,
        payload_hash=hashlib.sha256(callback_body.encode("utf-8")).hexdigest(),
        route_ref=route_ref, vector_state="pending", audit_complete=True,
        expires_at=expires_at,
    )


def _p18_record(record_id="r-1", *, text="甲公司完成回购。",
                delivery_state="pending", task_state="succeeded",
                audit_complete=True) -> FakeMainRecordV1:
    return FakeMainRecordV1(
        record_id=record_id, item_id="item-A", text=text, scope_id="default",
        business_date="2026-09-26", arrival_seq=1, raw_hash="h",
        pipeline_version="dedup_v1", fact_artifact_hash="",
        vector_state="pending", result_item_id="item-A",
        # 10-07 令甲-i（I-8）：翻不重复，保持"合法 pending 载荷"测意图
        result_decision="不重复", result_duplicate_ids=(),
        result_reason="r", task_state=task_state,
        completed_at="2026-09-26T03:33:00+00:00", result_version=1,
        event_id="e", audit_ids=(), audit_complete=audit_complete,
        reason_code="FACT_EQUIVALENT", callback_body="{}",
        callback_body_hash="h", delivery_state=delivery_state,
        delivery_deadline_at="2026-09-27T03:33:00+00:00",
        next_delivery_at="2026-09-26T03:33:00+00:00",
        callback_attempts=0, round_attempts=0, round=1,
    )


# =====================================================================
# 1. M-10：reopen_round 截止闸（10 §6.5-7 补发不刷新固定截止）
# =====================================================================

def test_reopen_round_rejects_past_deadline():
    """红能力：旧 reopen_round 不核截止——过期 delivered 照样重开（raises 红）。"""
    store = FakeDeliveryStore()
    past = _iso(datetime.datetime.now(UTC) - datetime.timedelta(hours=1))
    store.upsert_main(_delivery_record(delivery_deadline_at=past))
    with pytest.raises(P20ReceiveError, match="(?i)deadline"):
        reopen_round(store, "r-1")
    kept = store.main_records["r-1"]
    assert kept.delivery_state == "delivered"      # 拒绝不动状态
    assert kept.round == 1                          # 拒绝不动轮次
    assert kept.round_attempts == 12


def test_reopen_round_rejects_past_expires_at():
    """deadline 尚未来但 expires_at 已过 → 拒（10 §6.340 同口径）。"""
    store = FakeDeliveryStore()
    past = _iso(datetime.datetime.now(UTC) - datetime.timedelta(hours=1))
    future = _iso(datetime.datetime.now(UTC) + datetime.timedelta(hours=24))
    store.upsert_main(_delivery_record(
        delivery_deadline_at=future, expires_at=past))
    with pytest.raises(P20ReceiveError, match="(?i)expires_at"):
        reopen_round(store, "r-1")
    assert store.main_records["r-1"].delivery_state == "delivered"
    assert store.main_records["r-1"].round == 1


def test_reopen_round_allows_delivered_within_deadline_and_expires():
    """绿守卫：delivered + deadline/expires 均未来 → 照常重开。"""
    store = FakeDeliveryStore()
    future = _iso(datetime.datetime.now(UTC) + datetime.timedelta(hours=24))
    later = _iso(datetime.datetime.now(UTC) + datetime.timedelta(hours=48))
    store.upsert_main(_delivery_record(
        delivery_deadline_at=future, expires_at=later))
    reopen_round(store, "r-1")
    new = store.main_records["r-1"]
    assert new.round == 2
    assert new.round_attempts == 0
    assert new.callback_attempts == 5               # 累计不清零
    assert new.delivery_state == "pending"


def test_reopen_round_allows_exhausted_within_deadline():
    """绿守卫：exhausted + 截止内 → 照常重开（原语义保持）。"""
    store = FakeDeliveryStore()
    store.upsert_main(_delivery_record(delivery_state="exhausted"))
    reopen_round(store, "r-1")
    assert store.main_records["r-1"].round == 2
    assert store.main_records["r-1"].delivery_state == "pending"


# =====================================================================
# 2a. 伪 CAS：persist/main_record_persist.cas_main_record 版本对拍
# =====================================================================

def test_cas_main_record_first_create_still_succeeds():
    """绿守卫：(0,0) 首创建约定照常（真层两阶段内化语义的 fake 侧）。

    窗口Z2（N2-01 off-by-one 更正）：返回值 = 自存版本真实新 seq_no——
    首创建自存 (0,1) 即返回 0；旧断言 seq == 1 系过期规格（与自存版本
    差一），同步更正。
    """
    store = FakeP18Store()
    seq = main_record_persist.cas_main_record(
        store, _p18_record(), expected_seq_no=0, expected_primary_term=0)
    assert seq == 0                                  # 窗口Z2：对齐自存版本/0 基
    assert store.main_records["r-1"].delivery_state == "pending"
    assert store.main_version("r-1") == (0, 1)      # ES _seq_no 0 基首写


def test_cas_main_record_create_conflict_when_record_exists():
    """红能力：旧实现从不比对 expected——(0,0) 对已存在记录静默覆写（红）。"""
    from news_flash_dedup.persist.fake_store import FakeCASConflictError
    store = FakeP18Store()
    main_record_persist.cas_main_record(
        store, _p18_record(), expected_seq_no=0, expected_primary_term=0)
    with pytest.raises(FakeCASConflictError, match="(?i)already exists|create conflict"):
        main_record_persist.cas_main_record(
            store, _p18_record(text="篡改文本。"),
            expected_seq_no=0, expected_primary_term=0)
    assert store.main_records["r-1"].text == "甲公司完成回购。"   # INV-2 不动


def test_cas_main_record_rejects_stale_expected_version():
    """红能力：陈旧 expected 版本更新被拒（旧实现静默覆写 = 红）。"""
    from news_flash_dedup.persist.fake_store import FakeCASConflictError
    store = FakeP18Store()
    main_record_persist.cas_main_record(
        store, _p18_record(), expected_seq_no=0, expected_primary_term=0)
    with pytest.raises(FakeCASConflictError, match="(?i)conflict"):
        main_record_persist.cas_main_record(
            store, _p18_record(text="并发覆写。"),
            expected_seq_no=3, expected_primary_term=1)     # 当前实为 (0,1)
    assert store.main_records["r-1"].text == "甲公司完成回购。"
    assert store.main_version("r-1") == (0, 1)              # 拒绝不推进版本


def test_cas_main_record_rejects_update_on_absent_record():
    """非 (0,0) 更新路径要求记录已存在（对齐真层 not-found 拒）。"""
    from news_flash_dedup.persist.fake_store import FakeCASConflictError
    store = FakeP18Store()
    with pytest.raises(FakeCASConflictError, match="(?i)not found"):
        main_record_persist.cas_main_record(
            store, _p18_record(), expected_seq_no=0, expected_primary_term=1)


def test_cas_main_record_update_with_current_version_succeeds():
    """绿守卫：读当前版本 → 精确 expected 更新成功并推进版本。"""
    store = FakeP18Store()
    main_record_persist.cas_main_record(
        store, _p18_record(), expected_seq_no=0, expected_primary_term=0)
    cur_seq, cur_term = store.main_version("r-1")
    seq = main_record_persist.cas_main_record(
        store, _p18_record(text="更新文本。"),
        expected_seq_no=cur_seq, expected_primary_term=cur_term)
    assert seq == cur_seq + 1
    assert store.main_records["r-1"].text == "更新文本。"
    assert store.main_version("r-1") == (cur_seq + 1, cur_term)


# =====================================================================
# 2b. 伪 CAS：delivery persist_next_delivery 版本对拍（fake 层）
# =====================================================================

def test_cas_write_main_rejects_stale_expected_version():
    """红能力：store 层版本对拍——快照后版本被推进 → 过期写被拒。"""
    from news_flash_dedup.delivery.fake_store import DeliveryCASConflictError
    store = FakeDeliveryStore()
    store.upsert_main(_delivery_record(delivery_state="delivering"))
    rec, version = store.read_main_for_cas("r-1")
    store.mark_state("r-1", "pending")                      # 并发写者 bump
    stale = dataclasses.replace(
        rec, next_delivery_at="2099-01-01T00:00:00+00:00")
    with pytest.raises(DeliveryCASConflictError, match="(?i)conflict|stale"):
        store.cas_write_main(stale, expected_version=version)
    assert store.main_records["r-1"].delivery_state == "pending"   # 写未发生


def test_cas_write_main_rejects_absent_record():
    """CAS 写不存在的记录 → 拒（不静默首建）。"""
    from news_flash_dedup.delivery.fake_store import DeliveryCASConflictError
    store = FakeDeliveryStore()
    with pytest.raises(DeliveryCASConflictError, match="(?i)not found"):
        store.cas_write_main(_delivery_record(), expected_version=0)


def test_persist_next_delivery_racing_write_rejected():
    """红能力：persist_next_delivery 读-改-写之间插入并发写 → 旧裸赋值
    静默覆写并发者的状态（红）；修复后快照过期即拒。"""
    from news_flash_dedup.delivery.fake_store import DeliveryCASConflictError

    class _RacingStore(FakeDeliveryStore):
        """快照返回后、CAS 前确定性注入并发写（复现单线程内交错）。"""

        def read_main_for_cas(self, record_id):
            snapshot = super().read_main_for_cas(record_id)
            self.mark_state(record_id, "pending")           # 并发写者 bump
            return snapshot

    store = _RacingStore()
    original_next = _iso(datetime.datetime.now(UTC) - datetime.timedelta(hours=1))
    store.upsert_main(_delivery_record(
        delivery_state="delivering", next_delivery_at=original_next))
    with pytest.raises(DeliveryCASConflictError):
        persist_next_delivery(store, "r-1", next_seconds=5.0)
    kept = store.main_records["r-1"]
    assert kept.delivery_state == "pending"                 # 并发者写入保留
    assert kept.next_delivery_at == original_next           # 未被过期快照覆写


def test_persist_next_delivery_normal_path_bumps_version():
    """绿守卫：无竞争时 persist_next_delivery 照常写并推进版本。"""
    store = FakeDeliveryStore()
    store.upsert_main(_delivery_record(delivery_state="delivering"))
    _, before = store.read_main_for_cas("r-1")
    next_at = persist_next_delivery(store, "r-1", next_seconds=5.0)
    assert isinstance(next_at, str) and "T" in next_at
    _, after = store.read_main_for_cas("r-1")
    assert after == before + 1


# =====================================================================
# 3. _last_claim_read：领取上下文只在 claim 路径写
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


def _seed_doc(record_id="r-1", callback_body='{"item_id":"item-A"}'):
    now = datetime.datetime.now(UTC)
    return {
        "record_id": record_id, "item_id": "item-A", "scope_id": "default",
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
        "vector_state": "pending", "expires_at": "",
    }


def _real_store(monkeypatch, docs):
    monkeypatch.setattr(p20_es_store, "assert_p20_uat_open", lambda: None)
    config = RealESP20Config(run_uuid="winm", business_date=date(2026, 9, 26))
    return RealESP20Store(_StubESClient(docs), config)


def test_plain_read_does_not_pollute_claim_context(monkeypatch):
    """红能力：旧 __getitem__ 任何读都写 _last_claim_read——普通读后
    上下文已被污染（断言 is None 红）。"""
    store = _real_store(monkeypatch, {"r-1": _seed_doc()})
    rec = store.main_records["r-1"]                         # 普通读
    assert rec.record_id == "r-1"
    assert store._last_claim_read is None                   # 普通读不写上下文


def test_update_lease_refused_after_plain_read_only(monkeypatch):
    """红能力：旧实现普通读即可"凑出"领取上下文——仅普通读后 update_lease
    静默放行（红）；修复后无 claim 路径读 → 拒（fail-closed）。"""
    doc = _seed_doc()
    store = _real_store(monkeypatch, {"r-1": doc})
    store.main_records["r-1"]                               # 仅普通读
    lease = FakeDeliveryLease(
        owner_id="worker-1", owner_generation=1,
        lease_until=_iso(datetime.datetime.now(UTC)
                         + datetime.timedelta(minutes=5)),
        attempt_id="a" * 64, round=1, result_version=1, event_id="e-1",
        payload_hash=doc["callback_body_hash"],
        route_ref=doc["route_ref"],
    )
    with pytest.raises(P20ClaimPreconditionError,
                       match="(?i)claim context absent"):
        store.update_lease(lease)


def test_receive_writes_claim_context_via_claim_path(monkeypatch):
    """绿守卫：claim 路径（receive → claim_read）照常写领取上下文，
    且其后的普通读不再改写/污染。"""
    doc = _seed_doc()
    store = _real_store(monkeypatch, {"r-1": doc})
    lease = receive(store, "r-1", payload_hash=doc["callback_body_hash"],
                    route_ref=doc["route_ref"],
                    expected_generation=0, event_id="e-1")
    assert lease.owner_generation == 1
    assert store._last_claim_read == "r-1"                  # claim 路径写入
    rec = store.main_records["r-1"]                         # 普通读（send 路径同型）
    assert rec.delivery_state == "delivering"
    assert store._last_claim_read == "r-1"                  # 普通读不再改写


# =====================================================================
# 4. 回调 URL scheme 白名单（仅 http/https，fail-closed）
# =====================================================================

@pytest.mark.parametrize("url", [
    "file:///C:/Windows/win.ini",                           # 本地文件读取/伪 ACK
    "ftp://127.0.0.1:21/callback",                          # FTP handler
    "data:application/json,{\"succeed\": true}",            # data: 伪 ACK
])
def test_send_callback_rejects_non_http_scheme(url):
    """红能力：旧 _OPENER 保留 File/FTP/Data handler 且零 scheme 校验——
    file:// 等不抛 P20SendError（红）。状态必须保持 delivering 不动。"""
    store = FakeDeliveryStore()
    store.upsert_main(_delivery_record(
        delivery_state="delivering", route_ref=url))
    with pytest.raises(P20SendError, match="(?i)scheme"):
        send_callback(store, "r-1", timeout_total=1.0)
    assert store.main_records["r-1"].delivery_state == "delivering"


# =====================================================================
# 5. F4-5：ObjectApiResponse .body 回退异型 fail-closed
# =====================================================================

class _AlienShapeClient:
    """indices.get 返回既非 Mapping 又无 .body 的异型响应。"""

    class _Indices:
        def __init__(self):
            self.created = []

        def get(self, *, index, allow_no_indices, ignore_unavailable):
            return object()                                  # 异型

        def create(self, *, index, body):
            self.created.append(index)
            return {"acknowledged": True, "shards_acknowledged": True}

    def __init__(self):
        self.indices = self._Indices()


class _BodyNonMappingClient:
    """indices.get 返回 .body 非 Mapping 的 ObjectApiResponse 异型。"""

    class _Resp:
        body = None

    class _Indices:
        def __init__(self):
            self.created = []

        def get(self, *, index, allow_no_indices, ignore_unavailable):
            return _BodyNonMappingClient._Resp()

        def create(self, *, index, body):
            self.created.append(index)
            return {"acknowledged": True, "shards_acknowledged": True}

    def __init__(self):
        self.indices = self._Indices()


def test_provision_rejects_alien_listing_shape_fail_closed():
    """红能力：异型 listing 旧实现静默判空 → fresh-namespace 闸失效并
    继续建索引（红）；修复后 fail-closed 即拒且零创建。"""
    client = _AlienShapeClient()
    with pytest.raises(RuntimeError, match="(?i)unexpected shape"):
        provision_isolated_indices(client, "p01-batch-winm-",
                                   business_day=date(2026, 9, 24))
    assert client.indices.created == []


def test_provision_rejects_body_non_mapping_fail_closed():
    """红能力：.body 非 Mapping（None）同样 fail-open → 修复后即拒。"""
    client = _BodyNonMappingClient()
    with pytest.raises(RuntimeError, match="(?i)unexpected shape"):
        provision_isolated_indices(client, "p01-batch-winm-",
                                   business_day=date(2026, 9, 24))
    assert client.indices.created == []
