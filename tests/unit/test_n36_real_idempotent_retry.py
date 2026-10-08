"""N36 挂账并入（挂账分流核对表.md L109，owner=E 批 B4 施工面）：
真层 commit 幂等三路对齐钉（P17-3 验收用例补真层幂等重试钉）。

三路（persist/es_store.py 真层，FakeESClient 内存模拟，禁集群）：
① 审计通路：write_audit_documents bulk create 冲突（AuditPersistError）入捕获面——
   同内容重试逐文档实时 GET 对拍幂等返回；缺失/分歧原错误重抛不掩盖
② create 通路：write_main_record create 冲突（CASConflictError）语义映射——
   同内容重试幂等返回（commit_one 通路③语义），分歧重抛 fail-closed
③ main_records 内省视图：真层补协议视图（实时 GET 适配，_RETRY_STABLE_FIELDS
   十七属性同义），coordinator.py:319-340 重试幂等对拍路径对真层开放

场景对齐 M-3 三路幂等（fake 层已兑现，W2Fγ）：本钉兑现真层同语义。
"""

from __future__ import annotations

import hashlib
import json
from datetime import date

import pytest

from news_flash_dedup.commit.coordinator import CommitContext, CommitOneError, commit_one
from news_flash_dedup.es_client import APPROVED_UAT_HOST
from news_flash_dedup.persist.es_store import (
    AuditPersistError,
    CASConflictError,
    RealESP18Config,
    RealESP18Store,
)
from news_flash_dedup.recall.service import IdentityFactSupply

from b4_fake_es import FakeESClient

DAY_STR = "2026-09-29"
DAY = date(2026, 9, 29)
SCOPE = "default"
TEXT = "美国能源信息署公布原油库存增加。"
RUN = "n36pin"


def _rid(seed: str) -> str:
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


@pytest.fixture
def real_env(monkeypatch):
    monkeypatch.setenv("P18_CONFIRM_UAT", "1")
    monkeypatch.setenv("DEPLOY_ENV", "test")
    monkeypatch.setenv("TEST_ES_HOST", APPROVED_UAT_HOST)
    for key in ("PROD_ES_HOST", "PROD_ES_PORT", "PROD_ES_USER", "PROD_ES_PASS",
                "PROD_ES_SCHEME", "ALLOW_PROD_WRITE"):
        monkeypatch.delenv(key, raising=False)
    client = FakeESClient()
    config = RealESP18Config(run_uuid=RUN, business_date=DAY)
    store = RealESP18Store(client, config)
    return client, store, config


def _ctx() -> CommitContext:
    supply = IdentityFactSupply()
    current_id, history_id = _rid("n36-current"), _rid("n36-history")
    current = {
        "record_id": current_id, "item_id": "9-1", "text": TEXT,
        "raw_hash": hashlib.sha256(TEXT.encode("utf-8")).hexdigest(),
        "scope_id": SCOPE, "business_date": DAY_STR, "arrival_seq": 2,
        "facts": supply(current_id, TEXT),
    }
    history = {
        "record_id": history_id, "item_id": "9-0", "text": TEXT,
        "raw_hash": hashlib.sha256(TEXT.encode("utf-8")).hexdigest(),
        "scope_id": SCOPE, "business_date": DAY_STR, "arrival_seq": 1,
        "facts": supply(history_id, TEXT),
    }
    return CommitContext(
        scope_id=SCOPE, business_date=DAY_STR, arrival_seq=2,
        current=current, candidates=(history,), visible_seq=99, prepared_seq=99,
        coverage_complete=True)


def _commit(store, ctx):
    return commit_one(ctx, store, audit_complete=True, coverage_complete=True)


def _items_writes(client, config) -> list[tuple]:
    return [call for call in client.calls
            if call[0] == "index" and call[1] == config.items_index]


# ---------- 三路合钉：同内容重试全链幂等 ----------

def test_n36_retry_after_success_is_idempotent_via_audit_capture_and_view(real_env):
    client, store, config = real_env
    ctx = _ctx()
    first = _commit(store, ctx)
    assert first.state == "committed"
    assert first.decision_watermark_advanced_to == 2
    items_writes_after_first = len(_items_writes(client, config))

    # 同内容重试（模拟崩溃后整链重放）：审计 409→捕获面对拍幂等（①）；
    # 水位已推进→main_records 视图对拍幂等（③）
    second = _commit(store, ctx)
    assert second.state == "committed"
    assert second.decision_watermark_advanced_to == 2
    # 主记录零二次写入（create 壳+CAS 翻牌恰一次链路）
    assert len(_items_writes(client, config)) == items_writes_after_first
    # 审计文档不翻倍（仍是一份）
    audit_docs = [key for key in client.docs if key[0] == config.audits_index]
    assert len(audit_docs) == 1


def test_n36_retry_with_watermark_reset_absorbs_create_conflict(real_env):
    client, store, config = real_env
    ctx = _ctx()
    assert _commit(store, ctx).state == "committed"
    record_id = ctx.current["record_id"]
    stored_before = dict(client.docs[(config.items_index, record_id)])
    # 崩溃形态：主记录已写、水位未推进（控制文档丢失→重读为 0）
    del client.docs[(config.control_index, f"{SCOPE}|{DAY_STR}")]

    second = _commit(store, ctx)
    assert second.state == "committed"
    assert second.decision_watermark_advanced_to == 2
    # create 通路冲突被语义映射吸收（②）：重试 create 被拒后零二次写入——
    # 已存记录任何字段不动、版本不二次推进（INV-2 同款语义）
    assert client.docs[(config.items_index, record_id)] == stored_before
    # 水位重新推进落回控制文档
    assert store.get_watermark(SCOPE, DAY_STR) == 2


# ---------- 分歧即拒（fail-closed 不掩盖） ----------

def test_n36_divergent_main_record_refused_on_both_paths(real_env):
    client, store, config = real_env
    ctx = _ctx()
    assert _commit(store, ctx).state == "committed"
    record_id = ctx.current["record_id"]
    # 篡改已存主记录（分歧：决策翻转）
    doc = client.docs[(config.items_index, record_id)]
    doc["_source"] = {**doc["_source"], "result_decision": "不重复",
                      "result": {"decision": "不重复", "duplicate_ids": [],
                                 "reason": "篡改。"}}

    # 视图路径（水位在）：重试幂等对拍分歧→CommitOneError
    with pytest.raises(CommitOneError, match="diverges"):
        _commit(store, ctx)
    # 写路径（水位失）：create 冲突对拍分歧→CASConflictError
    del client.docs[(config.control_index, f"{SCOPE}|{DAY_STR}")]
    with pytest.raises(CASConflictError):
        _commit(store, ctx)


def test_n36_divergent_audit_doc_refused(real_env):
    client, store, config = real_env
    ctx = _ctx()
    assert _commit(store, ctx).state == "committed"
    audit_keys = [key for key in client.docs if key[0] == config.audits_index]
    assert len(audit_keys) == 1
    doc = client.docs[audit_keys[0]]
    doc["_source"] = {**doc["_source"], "detail": "tampered-detail"}
    with pytest.raises(AuditPersistError):
        _commit(store, ctx)


# ---------- ① write_audit_documents 直钉 ----------

def test_n36_write_audit_documents_same_content_retry_idempotent(real_env):
    client, store, config = real_env
    docs = [{
        "comparison_id": "cmp-1", "history_record_id": _rid("h"),
        "current_record_id": _rid("c"), "history_raw_hash": "1" * 64,
        "current_raw_hash": "2" * 64, "basis": "text", "field_path": "text",
        "detail": "same", "history_evidence": {}, "current_evidence": {},
        "pipeline_version": "dedup_v1", "payload_hash": "p" * 64,
    }]
    assert store.write_audit_documents(docs) == ["cmp-1"]
    # 同内容重试：bulk 409 冲突入捕获面→逐文档对拍一致→幂等返回（不抛）
    assert store.write_audit_documents(docs) == ["cmp-1"]
    # 分歧同 ID：重抛 AuditPersistError 不掩盖
    divergent = [dict(docs[0], detail="different")]
    with pytest.raises(AuditPersistError):
        store.write_audit_documents(divergent)
    # 既有直调面语义不变（persist_main_record_es 路径）：write_audit_batch 失败即停
    with pytest.raises(AuditPersistError):
        store.write_audit_batch(docs)


# ---------- ③ main_records 视图直钉 ----------

def test_n36_main_records_view_exposes_retry_stable_fields(real_env):
    client, store, config = real_env
    ctx = _ctx()
    assert _commit(store, ctx).state == "committed"
    record_id = ctx.current["record_id"]
    view = store.main_records
    snapshot = view.get(record_id)
    assert snapshot is not None
    source = client.source(config.items_index, record_id)
    # 十七稳定字段同义映射（commit/coordinator.py:75-80 _RETRY_STABLE_FIELDS）
    assert snapshot.record_id == record_id
    assert snapshot.item_id == ctx.current["item_id"]
    assert snapshot.text == TEXT
    # 恒等投影占位下判疑难（decide 现役语义，test_p23_uat.py:352 同述）
    assert snapshot.decision == "边界case/疑难case"
    assert snapshot.duplicate_ids == ()
    assert snapshot.reason == source["result"]["reason"]
    assert snapshot.payload_hash == source["callback_body_hash"]
    assert snapshot.delivery_state == "held"           # 10-07 令甲-i（I-4）：边界件扣留不投递
    assert snapshot.callback_attempts == 0
    assert tuple(snapshot.audit_ids) == tuple(source["audit_ids"])
    assert snapshot.audit_complete is True
    assert snapshot.raw_hash == ctx.current["raw_hash"]
    assert snapshot.pipeline_version == "dedup_v1"
    assert snapshot.result_version == 1
    assert snapshot.event_id == source["event_id"]
    assert snapshot.internal_code == "FACT_INCOMPLETE"
    assert snapshot.expires_at == source.get("expires_at", "")
    # 未命中→None（视图协议 .get 语义）
    assert view.get("f" * 64) is None


# ---------- N38（挂账分流核对表 N38 行，B4′ F-4）：真层分歧异常面 ↔ worker 捕获面 ----------
#
# N36 真层分歧两异常（write_main_record→CASConflictError、write_audit_documents
# →AuditPersistError）不经 CommitOneError 包装时，worker.py:416-417 仅捕
# CommitOneError → 未捕获冒泡=响亮死（非受控 failed 停）。修法①：仓储边界
# 包装归一——分歧异常=CommitOneError 语义子型 ∧ 原契约类型子型（p18"失败
# 即停"契约与 N36 钉零改动；真并发 CAS 冲突=破隔离，保持裸 CASConflictError
# 冒泡响亮死不静默）。本组三钉：worker 级受控 failed+台账可辨+不冒泡（两
# 异常路径各一）+双契约子型直钉。

def _materialized(record_id: str, item_id: str, seq: int, text: str) -> dict:
    """p01 物化文档（batch_admission._documents :441-469 同形，回放置远 expiry）。"""
    return {
        "scope_id": SCOPE, "request_id": "req-" + record_id[:12],
        "item_id": item_id, "record_id": record_id,
        "schema_version": "v1", "pipeline_version": "dedup_v1",
        "embedding_space_id": "space-x", "business_date": DAY_STR,
        "arrival_seq": seq, "received_at": "2026-09-29T00:00:00+08:00",
        "accepted_at": "2026-09-29T00:00:00+08:00",
        "expires_at": "2100-01-01T00:00:00+08:00",
        "text": text,
        "raw_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "task_state": "accepted", "delivery_state": "not_ready",
        "result": None,
        "diagnostics": {"delivery": {"route_ref": "n38", "trace_id": None}},
    }


class _UnavailableChannel:
    """非焦点通道桩：恒 unavailable（NullVectorSearcher 同形，缺口语义）。"""

    def __init__(self, channel: str) -> None:
        self._channel = channel

    def search(self, request, query_vector=None):
        from news_flash_dedup.recall.models import ChannelResult

        return ChannelResult(self._channel, "unavailable", (),
                             f"{self._channel}_v1", error_code="TEST_STUB",
                             elapsed_ms=0.0)


class _ScriptedHashChannel:
    """焦点通道脚本件：按 record_id 返回预置候选（HashChannel 契约同形——
    候选合法性 self-check：同 scope/day、arrival_seq 严格小于 current）。
    FakeESClient 查询评估面不覆盖真 HashChannel 查询体（ES_SEARCH_FAILED
    实证），候选供给非本钉焦点故脚本化。"""

    def __init__(self, candidates_by_record: dict) -> None:
        self._map = candidates_by_record

    def search(self, request, query_vector=None):
        from news_flash_dedup.recall.models import ChannelResult

        candidates = self._map.get(request.record_id, ())
        for candidate in candidates:
            assert candidate.arrival_seq < request.arrival_seq
        return ChannelResult("hash", "complete", tuple(candidates), "hash_v1",
                             visible_seq=request.visible_seq,
                             prepared_seq=request.prepared_seq,
                             coverage_complete=True, elapsed_ms=0.0)


def _seed_p01_head(client, prefix: str, materialized_seq: int) -> None:
    """种受理头（lexical 水位供源；无头→visible=0→commit_one 恒 STALE）。"""
    from news_flash_dedup.batch_admission import HEAD_ID, LOG_VERSION

    client.put(f"{prefix}news-dedup-control-v1", HEAD_ID, {
        "kind": "admission", "owner_id": "n38-pin",
        "last_allocated_seq": materialized_seq,
        "last_materialized_seq": materialized_seq,
        "checkpoint": {}, "pending": {"version": LOG_VERSION, "batches": []},
        "updated_at": "2026-09-29T00:00:00+08:00",
    })


def _live_worker(client, store, prefix: str, hash_channel):
    """live 组合根：脚本 hash（焦点通道）+ 三路桩 + 真 F2/F0/仓储适配。"""
    from news_flash_dedup.recall.prepare import (
        ElasticsearchWatermarkProvider,
        PrepareWorker,
    )
    from news_flash_dedup.recall.service import (
        NullVectorSearcher,
        RecallService,
    )
    from news_flash_dedup.recall.worker import (
        DedupWorker,
        ElasticsearchRegistrationScanner,
    )

    service = RecallService(
        client, prefix,
        vector_searcher=NullVectorSearcher(),
        channels={
            "hash": hash_channel,
            "near": _UnavailableChannel("near"),
            "bm25": _UnavailableChannel("bm25"),
            "entity": _UnavailableChannel("entity"),
        })
    return DedupWorker(
        mode="live", recall_service=service,
        prepare_worker=PrepareWorker(client, prefix),
        watermark_provider=ElasticsearchWatermarkProvider(client, prefix),
        scanner=ElasticsearchRegistrationScanner(client, prefix),
        commit_store=store)


def test_n38_divergent_main_record_maps_to_controlled_failed(real_env):
    """真层主记录分歧注入→worker 受控 failed+台账可辨+不冒泡（create 通路）。"""
    client, store, config = real_env
    prefix = "p01-batch-n38pin-"
    record_id = _rid("n38-main")
    items_index = f"{prefix}news-dedup-items-v1-2026.09.29"
    client.put(items_index, record_id, _materialized(record_id, "9-1", 1, TEXT))
    # 预种分歧主记录（写路径 create 冲突→稳定键对拍分歧：text 相异）
    client.put(config.items_index, record_id, {
        "record_id": record_id, "item_id": "9-1", "text": "篡改文本。",
        "task_state": "succeeded",
    })
    _seed_p01_head(client, prefix, 1)
    worker = _live_worker(client, store, prefix, _ScriptedHashChannel({}))
    report = worker.drive_once(SCOPE, DAY_STR)     # 不冒泡=受控（修复前此行冒泡）
    assert report.failed == (record_id,)
    assert report.committed == report.stale == report.awaiting == ()
    # 终态未确认：水位不推进
    assert store.get_watermark(SCOPE, DAY_STR) is None


def test_n38_divergent_audit_doc_maps_to_controlled_failed(real_env):
    """真层审计分歧注入→worker 受控 failed+台账可辨+不冒泡（审计通路）。"""
    client, store, config = real_env
    prefix = "p01-batch-n38pin-"
    items_index = f"{prefix}news-dedup-items-v1-2026.09.29"
    history_id, current_id = _rid("n38-a-history"), _rid("n38-a-current")
    client.put(items_index, history_id, _materialized(history_id, "9-0", 1, TEXT))
    client.put(items_index, current_id, _materialized(current_id, "9-1", 2, TEXT))
    # 预种分歧审计文档（同 comparison_id——契约公式 decide/audit.py:99-121
    # SHA256(JCS([current, history, pipeline_version]))，detail 键相异）
    comparison_id = hashlib.sha256(json.dumps(
        [current_id, history_id, "dedup_v1"],
        ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()
    client.put(config.audits_index, comparison_id, {
        "comparison_id": comparison_id, "history_record_id": history_id,
        "current_record_id": current_id, "detail": "tampered-divergent",
    })
    _seed_p01_head(client, prefix, 2)
    from news_flash_dedup.recall.models import RecallCandidate

    hash_channel = _ScriptedHashChannel({
        current_id: (RecallCandidate(
            record_id=history_id, item_id="9-0", arrival_seq=1, text=TEXT,
            subpaths=(), score=1.0, query_version="hash_v1"),),
    })
    worker = _live_worker(client, store, prefix, hash_channel)
    report = worker.drive_once(SCOPE, DAY_STR)     # 不冒泡=受控（修复前此行冒泡）
    # 首条零候选正常提交；次条 hash 命中→审计分歧→受控 failed
    assert report.committed == (history_id,)
    assert report.failed == (current_id,)
    assert report.stale == report.awaiting == ()
    assert store.get_watermark(SCOPE, DAY_STR) == 1
    # "失败即停"语义保持：审计失败，current 主记录未写
    assert store.get_main_record(current_id) is None


def test_n38_divergence_exceptions_are_dual_contract_subtypes():
    """双契约子型直钉：CommitOneError（worker 捕获面）∧ 原类型（N36/p18 钉面）。

    子型居 persist/divergence.py（导入环规避：es_store 模块级回引 commit
    即真环，实证见模块注），es_store 分歧 raise 处惰性导入同源类。
    """
    from news_flash_dedup.persist.divergence import (
        AuditDivergenceError,
        MainRecordDivergenceError,
    )

    assert issubclass(MainRecordDivergenceError, CommitOneError)
    assert issubclass(MainRecordDivergenceError, CASConflictError)
    assert issubclass(AuditDivergenceError, CommitOneError)
    assert issubclass(AuditDivergenceError, AuditPersistError)
