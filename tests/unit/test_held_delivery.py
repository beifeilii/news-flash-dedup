# -*- coding: utf-8 -*-
"""疑似确认系统内扣留（held-delivery）红测——设计稿 log/temp/held-delivery-design.md 附录 A。

令（10-07，权威=会议口径"疑似确认=不通过、不展示"）：
decision != "不重复" 的主记录不进入投递（重复/边界在本系统内扣留 held，
不下发）；下游只收"不重复"放行件。

红测锚=现役码面亲读（禁臆造 API）：
- 写入点：commit/coordinator.py:307、persist/coordinator.py:113
- 拒投闸：delivery/dispatcher.py:45（scan_pending 单态收录）、
  delivery/coordinator.py:118-121（receive 双态闸）
- 查询面：product/task_query.py:115-149（五步读序）/:222-262（投影）
- 生命周期：product/task_query.py:90-96（L443）、lifecycle.py:34-36/:86-98/:164-181
- 词表闸：delivery/fake_store.py:206-208、delivery/es_store.py:215-217

现役码面预期：G1-1/G1-2/G2-1 三件红（held 未实现）；其余结构性绿钉/回归守卫。
实现批（甲-i 案）落锤后全绿。
"""

from __future__ import annotations

import datetime
import hashlib
import http.server
import json
import threading

import pytest

from news_flash_dedup.commit import CommitContext, commit_one
from news_flash_dedup.commit.fake_store import FakeCommitStore
from news_flash_dedup.decide.types import DecideOutcome
from news_flash_dedup.delivery import (
    FakeDeliveryRecord,
    FakeDeliveryStore,
    P20ReceiveError,
    receive,
    scan_pending,
    send_callback,
)
from news_flash_dedup.delivery.dispatcher import scan_expired_delivering
from news_flash_dedup.delivery.es_store import RealESP20Store, _doc_to_record
from news_flash_dedup.delivery.fake_store import FakeDeliveryLease
from news_flash_dedup import lifecycle
from news_flash_dedup.persist import coordinator as persist_coordinator
from news_flash_dedup.persist.fake_store import FakeP18Store
from news_flash_dedup.admission import REQUEST_INDEX
from news_flash_dedup.batch_admission import _keys
from news_flash_dedup.product.task_query import TaskQueryPort

UTC = datetime.timezone.utc
DAY = "2026-09-29"


def _iso(dt: datetime.datetime) -> str:
    return dt.isoformat()


# ---------------------------------------------------------------- 夹具

def _decide(decision: str, *, item_id="item-C") -> DecideOutcome:
    """DecideOutcome 真构造（decide/types.py:26-79 五字段+基数闸在案）。"""
    return DecideOutcome(
        item_id=item_id, text="甲公司完成回购。",
        decision=decision,
        duplicate_ids=("item-A",) if decision == "重复" else (),
        reason="主体与事件一致。",
        internal_code=("FACT_EQUIVALENT" if decision == "重复"
                       else "NO_DUPLICATE_FOUND"),
        pair_codes={}, used_evidence=(),
        raw_hash="h", pipeline_version="dedup_v1",
    )


def _commit_ctx(*, coverage_complete: bool,
                with_candidate: bool = False) -> tuple[FakeCommitStore, CommitContext, str]:
    """CommitContext 夹具（commit/coordinator.py:163-200 分支锚）。

    with_candidate=False=零候选分区首条（:173-186 短路不重复 / :188-200
    短路边界）；True=带一条 history 候选（decide_for_task 真实调用路径——
    G1-1 monkeypatch 面；零候选短路不调 decide，不可用作判定注入面）。
    """
    record_id = "c" * 64
    text = "甲公司完成回购。"
    current = {
        "record_id": record_id, "item_id": "item-C", "text": text,
        "raw_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "scope_id": "default", "business_date": DAY,
        "arrival_seq": 1, "pipeline_version": "dedup_v1",
    }
    history = {
        "record_id": "a" * 64, "item_id": "item-A", "text": text,
        "raw_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "scope_id": "default", "business_date": DAY,
        "arrival_seq": 0, "pipeline_version": "dedup_v1",
    }
    ctx = CommitContext(
        scope_id="default", business_date=DAY,
        arrival_seq=1, current=current,
        candidates=(history,) if with_candidate else (),
        visible_seq=1, prepared_seq=1,
        coverage_complete=coverage_complete,   # M-01 强制显式（commit/coordinator.py:43）
    )
    return FakeCommitStore(), ctx, record_id


def _delivery_record(record_id: str, state: str, *,
                     decision: str = "不重复", route_ref="http://127.0.0.1:8080/cb",
                     event_id="e-1") -> FakeDeliveryRecord:
    """FakeDeliveryRecord（delivery/fake_store.py:31-53 字段集亲读）。"""
    body = json.dumps(
        {"item_id": "item-C", "text": "甲公司完成回购。", "decision": decision,
         "duplicate_ids": (["item-A"] if decision == "重复" else []),
         "reason": "主体与事件一致。"},
        sort_keys=True, ensure_ascii=False)
    now = datetime.datetime.now(UTC)
    return FakeDeliveryRecord(
        record_id=record_id, item_id="item-C", scope_id="default",
        business_date=DAY, arrival_seq=1,
        delivery_state=state,
        delivery_deadline_at=_iso(now + datetime.timedelta(hours=24)),
        next_delivery_at=_iso(now - datetime.timedelta(hours=1)),
        callback_attempts=0, round_attempts=0, round=1,
        callback_body=body,
        payload_hash=hashlib.sha256(body.encode("utf-8")).hexdigest(),
        route_ref=route_ref,
        vector_state="pending", audit_complete=True,
        expires_at="",
    )


class _AckHandler(http.server.BaseHTTPRequestHandler):
    """ACK succeed=true 接收器（test_p2_hardening.py:213-236 同族工艺）。"""

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        payload = b'{"succeed": true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


def _serve_ack():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _AckHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


# ---------------------------------------------------------------- G1：重复→held 不投递

def test_g1_1_duplicate_decision_held_at_commit_gate(monkeypatch):
    """①重复→不投递（commit 写入点锚 commit/coordinator.py:307）。

    红（现役）：decide 判重复的主记录 delivery_state 仍为 "pending"。
    绿（甲-i）：delivery_state == "held"。
    monkeypatch decide_for_task 为重复判定（decide 判定内核不属本令，
    此处只钉写入点分派；decide/service.decide_for_task 调用形态见
    commit/coordinator.py:151-162）。
    """
    from news_flash_dedup.decide import service as decide_service

    monkeypatch.setattr(
        decide_service, "decide_for_task",
        lambda *args, **kwargs: _decide("重复"))   # commit_one 位置传参（:151-152）
    store, ctx, record_id = _commit_ctx(coverage_complete=True,
                                        with_candidate=True)
    outcome = commit_one(ctx, store, audit_complete=True)
    assert outcome.state == "committed"
    assert outcome.decide_outcome.decision == "重复"
    record = store.main_records[record_id]
    assert record.delivery_state == "held"          # 红→绿分界钉


def test_g1_2_duplicate_decision_held_at_persist_gate():
    """①重复→不投递（persist 写入点锚 persist/coordinator.py:113）。"""
    store = FakeP18Store()
    record = persist_coordinator.persist_main_record(
        _decide("重复"), store, arrival_seq=1, raw_hash="h",
        audit_ids=(), audit_complete=True,
    )
    assert record.delivery_state == "held"          # 红→绿分界钉
    stored = store.main_records[record.record_id]
    assert stored.delivery_state == "held"


def test_g1_3_held_record_not_scanned_not_claimable():
    """①结构钉：held 件扫描排除（dispatcher.py:45）+ 领取拒（coordinator.py:118）。

    现役即绿（held 非 pending 天然排除）——本钉防实现走样成"held 仍可领"。
    """
    store = FakeDeliveryStore()
    store.upsert_main(_delivery_record("r-held", "held", decision="重复"))
    now = _iso(datetime.datetime.now(UTC))
    assert "r-held" not in scan_pending(store, now=now)
    with pytest.raises(P20ReceiveError, match="(?i)delivery_state"):
        receive(store, "r-held",
                payload_hash=store.main_records["r-held"].payload_hash,
                route_ref="http://127.0.0.1:8080/cb",
                expected_generation=0, event_id="e-1")
    assert store.lease_index == {}                   # 拒领零副作用


# ---------------------------------------------------------------- G2：边界→held 不投递

def test_g2_1_boundary_decision_held_at_commit_gate():
    """②边界→不投递（零候选 + coverage_complete=False 真路径，
    commit/coordinator.py:188-200：RECALL_INCOMPLETE 判边界——禁臆造判定路径）。"""
    store, ctx, record_id = _commit_ctx(coverage_complete=False)
    outcome = commit_one(ctx, store, audit_complete=True)
    assert outcome.state == "committed"
    assert outcome.decide_outcome.decision == "边界case/疑难case"
    assert outcome.decide_outcome.internal_code == "RECALL_INCOMPLETE"
    record = store.main_records[record_id]
    assert record.delivery_state == "held"          # 红→绿分界钉
    # 结构钉：扫描排除 + 领取拒（与 G1-3 同族）
    delivery_store = FakeDeliveryStore()
    delivery_store.upsert_main(_delivery_record(record_id, "held", decision="边界case/疑难case"))
    now = _iso(datetime.datetime.now(UTC))
    assert record_id not in scan_pending(delivery_store, now=now)


# ---------------------------------------------------------------- G3：不重复→pending→投递

def test_g3_1_not_duplicate_pending_scanned_claimed_delivered():
    """③不重复→正常 pending→投递全链（零候选 coverage_complete=True 真路径，
    commit/coordinator.py:173-186：NO_DUPLICATE_FOUND 判不重复）。"""
    store, ctx, record_id = _commit_ctx(coverage_complete=True)
    outcome = commit_one(ctx, store, audit_complete=True)
    assert outcome.state == "committed"
    assert outcome.decide_outcome.decision == "不重复"
    record = store.main_records[record_id]
    assert record.delivery_state == "pending"       # 放行件现役语义不动

    server = _serve_ack()
    try:
        route = f"http://127.0.0.1:{server.server_port}/callback"
        dstore = FakeDeliveryStore()
        dstore.upsert_main(_delivery_record(record_id, "pending",
                                            decision="不重复", route_ref=route,
                                            event_id=record.event_id))
        now = _iso(datetime.datetime.now(UTC))
        assert record_id in scan_pending(dstore, now=now)      # 扫描收录
        lease = receive(                                       # 领取成功
            dstore, record_id,
            payload_hash=dstore.main_records[record_id].payload_hash,
            route_ref=route, expected_generation=0,
            event_id=record.event_id)
        assert lease.owner_generation == 1
        assert dstore.main_records[record_id].delivery_state == "delivering"
        result = send_callback(dstore, record_id, timeout_total=5.0)
        assert result.success is True                          # ACK 单一口径（sender.py:248-254）
        assert dstore.main_records[record_id].delivery_state == "delivered"
    finally:
        server.shutdown()


# ---------------------------------------------------------------- G4：held 可查询

class _StubQueryStore:
    """TaskQueryPort 鸭式 store（product/task_query.py:99-111：仅需 .get）。"""

    def __init__(self, docs: dict) -> None:
        self._docs = dict(docs)

    def get(self, index: str, key: str):
        return self._docs.get((index, key))


def _query_fixture(*, delivery_state: str, expires_at: str,
                   decision: str = "重复") -> TaskQueryPort:
    """五步读序最小夹具（task_query.py:115-149；主记录形状亲读
    tests 侧 P21 投影消费键：scope_id/request_id/item_id/expires_at/
    task_state/delivery_state/result/callback_attempts/accepted_at/diagnostics）。"""
    task_id = "9-1"
    items_index = f"news-dedup-items-v1-{DAY.replace('-', '.')}"
    request_key = _keys("default", task_id, task_id)[1]
    request_doc = {
        "kind": "request", "scope_id": "default", "request_id": task_id,
        "item_id": task_id, "record_id": "a" * 64,
        "business_date": DAY, "arrival_seq": 1,
        "expires_at": expires_at, "target_index": items_index,
    }
    main_doc = {
        "scope_id": "default", "request_id": task_id, "item_id": task_id,
        "record_id": "a" * 64, "schema_version": "v1",
        "pipeline_version": "dedup_v1", "business_date": DAY, "arrival_seq": 1,
        "accepted_at": f"{DAY}T01:00:01+00:00",
        "started_at": f"{DAY}T01:00:02+00:00",
        "completed_at": f"{DAY}T01:00:03+00:00",
        "expires_at": expires_at,
        "text": "甲公司完成回购。", "raw_hash": "c" * 64,
        "task_state": "succeeded", "delivery_state": delivery_state,
        "callback_attempts": 0,
        "result": {"decision": decision,
                   "duplicate_ids": ["9-0"] if decision == "重复" else [],
                   "reason": "文本完全一致。"},
        "diagnostics": {"delivery": {"route_ref": "route-1",
                                     "trace_id": "trace-9"}},
    }
    store = _StubQueryStore({
        (REQUEST_INDEX, request_key): {"source": request_doc},
        (items_index, "a" * 64): {"source": main_doc},
    })
    clock = lambda: datetime.datetime(2026, 9, 30, 12, 0, tzinfo=UTC)  # noqa: E731
    return TaskQueryPort(store, scope_id="default", clock=clock)


def test_g4_1_held_queryable_with_five_field_result():
    """④held 件可查询（离线复核池取数道钉，设计稿 §2 轨①）。

    现役即绿（deliveryState 透传 task_query.py:228/:255）——本钉防实现把
    held 挡在查询面外（扣留≠消失）。
    """
    port = _query_fixture(delivery_state="held",
                          expires_at="2026-10-06T00:00:00+00:00")
    view = port.lookup("9-1")
    assert view is not None                          # 扣留≠消失：可查询
    assert view.taskState == "succeeded"
    assert view.deliveryState == "held"              # 态词透传直出
    assert view.callbackDelivered is None            # 未投递三态（task_query.py:230-235）
    assert view.result == {                          # 五字段投影全量可取
        "item_id": "9-1", "text": "甲公司完成回购。",   # :239-240 item_id/text 居源顶层
        "decision": "重复", "duplicate_ids": ["9-0"],
        "reason": "文本完全一致。"}


# ---------------------------------------------------------------- G5：held 生命周期可清理

def test_g5_1_held_invisible_after_expiry_l443():
    """⑤到期 held 件按未命中（L443 门禁 task_query.py:90-96/:172-173）。"""
    port = _query_fixture(delivery_state="held",
                          expires_at="2026-09-01T00:00:00+00:00")
    assert port.lookup("9-1") is None                # 到期→404 域，不泄露假死


def test_g5_2_held_day_index_named_by_cleanup_dry_run():
    """⑤held 件随本业务日 items 日索引 T-7 整索引清理（lifecycle.py:34-36
    硬正则 + :86-98 候选反推 + :164-181 dry-run 点名）。"""
    dotted = DAY.replace("-", ".")
    items_index = f"news-dedup-items-v1-{dotted}"
    assert lifecycle.is_valid_cleanup_target(items_index) is True

    class _StubIndices:
        def exists(self, *, index):
            return True

    class _StubClient:
        indices = _StubIndices()

    audit = lifecycle.CleanupAuditLog(operator="held-red")
    report = lifecycle.dry_run_cleanup(
        _StubClient(), datetime.date(2026, 10, 6), audit, retention_days=7)
    assert items_index in report["would_delete"]     # T-7 点名在册=可清理


# ---------------------------------------------------------------- G6：扫描器/生命周期回归绿守卫

def test_g6_1_scan_pending_baseline_unchanged():
    """⑥现役钉：pending 件零校验形态照收（dispatcher.py:47-50）——
    本令不得收窄正常放行件的扫描语义。"""
    store = FakeDeliveryStore()
    store.upsert_main(_delivery_record("r-ok", "pending", decision="不重复"))
    assert scan_pending(store, now="") == ["r-ok"]


def test_g6_2_scan_expired_delivering_only_delivering():
    """⑥现役钉：到期重扫只认 delivering（dispatcher.py:88-132）——
    pending/held 均不入恢复扫描面。"""
    store = FakeDeliveryStore()
    store.upsert_main(_delivery_record("r-del", "delivering"))
    store.upsert_main(_delivery_record("r-pen", "pending"))
    store.upsert_main(_delivery_record("r-held", "held", decision="重复"))
    store.update_lease(FakeDeliveryLease(
        owner_id="w1", owner_generation=1,
        lease_until="2026-10-07T09:00:00+00:00",     # 已过期（aware ISO，fake_store.py:92-98 闸）
        attempt_id="a" * 64, round=1, result_version=1, event_id="e-1",
        payload_hash="p", route_ref="route", record_id="r-del"))
    found = scan_expired_delivering(store, now="2026-10-07T10:00:00+00:00")
    assert found == ["r-del"]


def test_g6_3_fake_mark_state_rejects_held_vocabulary():
    """⑥设计抉择钉：held 仅创建落点写入，mark_state 词表闸拒翻入
    （delivery/fake_store.py:206-208 不扩——防误翻守卫）。"""
    store = FakeDeliveryStore()
    store.upsert_main(_delivery_record("r-1", "pending"))
    with pytest.raises(ValueError, match="(?i)invalid delivery_state"):
        store.mark_state("r-1", "held")


def test_g6_4_real_store_state_vocabulary_unchanged():
    """⑥设计抉择钉：真层五态 frozenset 不扩 held（delivery/es_store.py:215-217）。"""
    assert RealESP20Store._DELIVERY_STATES == frozenset(
        {"pending", "delivering", "delivered", "exhausted", "expired"})


def test_g6_5_doc_to_record_passthrough_held():
    """⑥读径钉：_doc_to_record 对 held 逐字透传（delivery/es_store.py:126-146
    无词表校验）——held 真层可读。"""
    body = '{"item_id":"item-C"}'
    rec = _doc_to_record({
        "record_id": "r-1", "callback_body": body,
        "callback_body_hash": hashlib.sha256(body.encode("utf-8")).hexdigest(),
        "delivery_state": "held",
        "delivery_deadline_at": "2026-10-06T00:00:00+00:00",
        "next_delivery_at": "2026-09-30T00:00:00+00:00",
    })
    assert rec.delivery_state == "held"


def test_g6_6_cleanup_rejects_alien_index_name():
    """⑥生命周期守卫：硬正则不匹配即拒（lifecycle.py:65-83 fail-closed）——
    held 引入不得放宽清理闸。"""
    assert lifecycle.is_valid_cleanup_target("news-dedup-items-v1-2026.13.99") is False
    assert lifecycle.is_valid_cleanup_target("news-dedup-items-v2-2026.09.29") is False
