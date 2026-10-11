"""P1a-T5 TTL/生命周期+跨日钉（卡3.6；验收钉 6.4 全谱；零成本内存面）。

设计锚 = log\\P1a-预备设计-2026-10-11.md §1.5（跨日恢复+7 天保留）：

- 7 天保留=索引级点名删除+硬正则闸（lifecycle 现役工艺平移），清理域
  扩 kind=work|tombstones（超期残留枚举同步覆盖）；
- TTL 不误删活任务（钉 6.4 核心）：清理器遇 RETRY_WAIT/LEASED（租约
  中）/COMPENSATING 拒绝删除该文档（逐条审计痕——留痕跳过≠静默
  删除），活任务在位可续 claim/收口；终态任务被清；
- 索引级删除前非终态在位检查 → 缓删+告警（fail-closed 不删）；
- 跨日重试复用首次 business_date+arrival_seq+expires_at（按重试日
  重算身份=禁止——重试日时钟注入即本钉负形态）；
- 7 天≠跨日召回：召回窗口限任务首次 business_date（test_recall_service
  域日同分区透传钉+T018 UAT 迁移承担；本文件钉跨日身份复用面）。

世界态构造：`_WorkClient`（文档级 fake：count/search/delete_by_query/
get 真求值 terms 检索）+协调器面 `BatchMemoryStore`（活任务连续性钉
——T4 同型工艺）。
"""

from __future__ import annotations

import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import pytest

from news_flash_dedup.admission import (
    AdmissionRequest, BUSINESS_ZONE, _digest, _utc,
)
from news_flash_dedup.batch_admission import (
    ADMISSION_CONFIG_VERSION, BatchAdmissionCoordinator, BatchLimits,
    HEAD_ID, CONTROL_INDEX, _new_work_item,
)
from news_flash_dedup.lifecycle import (
    CLEANUP_DAY_KINDS, CleanupAuditLog, candidate_indices_for_cleanup,
    dry_run_cleanup, enumerate_overdue_residue, execute_cleanup,
    execute_work_task_cleanup, is_valid_cleanup_target,
    plan_work_task_cleanup, work_task_state_query,
)
from news_flash_dedup.work_queue import (
    ACTIVE_TASK_STATES, TERMINAL_TASK_STATES, work_index, work_item_id,
)

from test_admission import request
from test_batch_admission import BatchMemoryStore


NOW = datetime(2026, 9, 25, 0, 0, tzinfo=timezone.utc)
TODAY = date(2026, 9, 25)
D7 = date(2026, 9, 18)
WORK_INDEX = "news-dedup-work-v1-2026.09.18"
TOMBSTONES_INDEX = "news-dedup-tombstones-v1-2026.09.18"


# ---------- 文档级 fake（count/search/delete_by_query/get 真求值） ----------


class _WorkClient:
    """lifecycle 面最小双倍体：任务文档 terms 检索真求值。

    文档形态 ``docs[(index, id)] = source``；count/search/delete_by_query
    按查询体 ``terms.task_state`` 求值（与真 ES 同语义面）；get 供
    "活任务在位"对拍。
    """

    def __init__(self, docs=None, present=None):
        self.docs = dict(docs or {})
        self.present = set(present or ())
        self.count_calls = []
        self.search_calls = []
        self.delete_by_query_calls = []

    # ---- indices ----

    def _indices(self):
        client = self

        class _Indices:
            def exists(self, *, index):
                return index in client.present or any(
                    idx == index for idx, _ in client.docs)

            def delete(self, *, index):
                client.present.discard(index)
                for (idx, key) in list(client.docs):
                    if idx == index:
                        del client.docs[(idx, key)]

            def refresh(self, *, index):
                return {"_shards": {"failed": 0}}

        return _Indices()

    indices = property(_indices)

    # ---- 查询求值 ----

    def _match(self, index, body):
        states = set(body["query"]["terms"]["task_state"])
        return [(key, source) for (idx, key), source in sorted(
            self.docs.items()) if idx == index
            and source.get("task_state") in states]

    def count(self, *, index, body):
        self.count_calls.append((index, body))
        return {"count": len(self._match(index, body)),
                "_shards": {"total": 1, "successful": 1, "failed": 0}}

    def search(self, *, index, body):
        self.search_calls.append((index, body))
        matched = self._match(index, body)
        offset = body.get("from", 0)
        size = body.get("size", 10)
        page = matched[offset:offset + size]
        return {"hits": {"hits": [
            {"_id": key, "_source": dict(source)} for key, source in page]}}

    def delete_by_query(self, *, index, body, refresh=False):
        self.delete_by_query_calls.append((index, body))
        matched = self._match(index, body)
        for key, _ in matched:
            del self.docs[(index, key)]
        return {"took": 1, "timed_out": False, "total": len(matched),
                "deleted": len(matched), "version_conflicts": 0,
                "failures": [], "_shards": {"total": 1, "successful": 1,
                                            "failed": 0}}

    def get(self, *, index, id, realtime=True):
        source = self.docs.get((index, id))
        return None if source is None else {"found": True,
                                             "_source": dict(source)}


def _task(task_id, state, seq):
    return {f"w-{seq}": {"task_state": state, "arrival_seq": seq}}


def _four_state_world(*, states=None):
    """day-7 边界四态世界（钉 6.4）：RETRY_WAIT/租约中/补偿中/终态各一。"""
    states = states or {
        "w-1": ("retry_wait", 1),
        "w-2": ("leased", 2),
        "w-3": ("compensating", 3),
        "w-4": ("ready", 4),
    }
    docs = {(WORK_INDEX, key): {"task_state": state, "arrival_seq": seq}
            for key, (state, seq) in states.items()}
    return _WorkClient(docs=docs, present={WORK_INDEX})


# ---------- 纯函数/闸门面 ----------


def test_work_task_state_query_covers_schema_sets():
    active = work_task_state_query(ACTIVE_TASK_STATES)
    assert active == {"query": {"terms": {"task_state": [
        "compensating", "leased", "pending", "retry_wait"]}}}
    terminal = work_task_state_query(TERMINAL_TASK_STATES)
    assert terminal == {"query": {"terms": {"task_state": [
        "expired", "ready", "tombstoned"]}}}
    with pytest.raises(ValueError):
        work_task_state_query(set())


@pytest.mark.parametrize("name,ok", [
    ("news-dedup-work-v1-2026.09.18", True),
    ("news-dedup-tombstones-v1-2026.09.18", True),
    ("p01-batch-r1-news-dedup-work-v1-2026.09.18", True),
    ("news-dedup-works-v1-2026.09.18", False),        # kind 拼写漂移
    ("news-dedup-work-v2-2026.09.18", False),         # 版本漂移
    ("news-dedup-work-v1-2026.13.99", False),         # 伪日期
    ("news-dedup-work-v1-2026.9.18", False),          # 未补零
    ("news-dedup-work-v1-*", False),                  # 通配符
    ("news-dedup-control-v1", False),                 # 控制文档越界
])
def test_hard_regex_admits_work_and_tombstones_only(name, ok):
    assert is_valid_cleanup_target(name) is ok


def test_candidate_domain_is_four_kinds():
    candidates = candidate_indices_for_cleanup(TODAY)
    assert [c.kind for c in candidates] == ["items", "audits", "work",
                                            "tombstones"]
    assert CLEANUP_DAY_KINDS == ("items", "audits", "work", "tombstones")
    by_kind = {c.kind: c.index_name for c in candidates}
    assert by_kind["work"] == WORK_INDEX
    assert by_kind["tombstones"] == TOMBSTONES_INDEX


# ---------- 文档级清理（钉 6.4：TTL 不误删活任务） ----------


def test_plan_counts_active_and_terminal_tasks():
    client = _four_state_world()
    targets = plan_work_task_cleanup(client, TODAY)
    assert len(targets) == 1
    target = targets[0]
    assert target.index_name == WORK_INDEX
    assert target.business_date == D7
    assert target.is_present is True
    assert target.active_count == 3 and target.terminal_count == 1
    # 缺场：保留在候选中（计数 None——与 ExpiredDocumentTarget 同型）。
    absent = plan_work_task_cleanup(_WorkClient(), TODAY)
    assert absent[0].is_present is False
    assert absent[0].active_count is None and absent[0].terminal_count is None


def test_document_cleanup_refuses_active_and_deletes_terminal():
    """day-7 边界四态（钉 6.4 全谱核心）：
    活任务（RETRY_WAIT/租约中/补偿中）拒绝删除+逐条审计痕（在位）；
    终态被清；未删者逐条留痕（拒绝≠静默跳过）。
    """
    client = _four_state_world()
    audit = CleanupAuditLog(operator="t5-test")
    report = execute_work_task_cleanup(client, TODAY, audit)
    # 终态清：delete_by_query 点名终态集合（ready/tombstoned/expired）。
    assert report["deleted"] == {WORK_INDEX: 1}
    assert report["refused"] == {WORK_INDEX: ["w-1", "w-2", "w-3"]}
    assert report["skipped_absent"] == []
    assert len(client.delete_by_query_calls) == 1
    index, body = client.delete_by_query_calls[0]
    assert index == WORK_INDEX
    assert body == work_task_state_query(TERMINAL_TASK_STATES)
    # 活任务在位（realtime GET 对拍）+终态文档已删。
    assert client.get(index=WORK_INDEX, id="w-1")["_source"]["task_state"] \
        == "retry_wait"
    assert client.get(index=WORK_INDEX, id="w-2")["_source"]["task_state"] \
        == "leased"
    assert client.get(index=WORK_INDEX, id="w-3")["_source"]["task_state"] \
        == "compensating"
    assert client.get(index=WORK_INDEX, id="w-4") is None
    # 逐条审计痕：3 拒（ok=False+task_id/task_state/error）+1 删（计数）。
    refusals = [run for run in audit.work_task_runs
                if run["error"] == "active_task_not_deletable"]
    assert [(run["task_id"], run["task_state"]) for run in refusals] == [
        ("w-1", "retry_wait"), ("w-2", "leased"), ("w-3", "compensating")]
    assert all(run["ok"] is False and run["operator"] == "t5-test"
               and run["kind"] == "work" for run in refusals)
    deleted_runs = [run for run in audit.work_task_runs if run["deleted"] == 1]
    assert len(deleted_runs) == 1 and deleted_runs[0]["ok"] is True


def test_document_cleanup_dry_run_exposes_refusals_zero_writes():
    client = _four_state_world()
    audit = CleanupAuditLog(operator="t5-test")
    report = execute_work_task_cleanup(client, TODAY, audit, dry_run=True)
    assert client.delete_by_query_calls == []       # 零写
    assert report["deleted"] == {}
    assert report["would_delete"] == {WORK_INDEX: 1}
    # dry-run 也要暴露拒删面（拒枚举照跑——留痕跳过≠静默）。
    assert report["refused"] == {WORK_INDEX: ["w-1", "w-2", "w-3"]}
    assert client.get(index=WORK_INDEX, id="w-4") is not None
    refusal_errors = {run["error"] for run in audit.work_task_runs}
    assert "active_task_not_deletable" in refusal_errors
    assert "dry_run" in refusal_errors


def test_document_cleanup_absent_index_skips_with_audit():
    client = _WorkClient()
    audit = CleanupAuditLog(operator="t5-test")
    report = execute_work_task_cleanup(client, TODAY, audit)
    assert report["skipped_absent"] == [WORK_INDEX]
    assert report["deleted"] == {} and report["refused"] == {}
    assert audit.work_task_runs[0]["error"] == "absent"


def test_document_cleanup_delete_failures_not_silent():
    class _Failing(_WorkClient):
        def delete_by_query(self, *, index, body, refresh=False):
            self.delete_by_query_calls.append((index, body))
            return {"timed_out": False, "deleted": 0, "version_conflicts": 0,
                    "failures": [{"shard": 0, "reason": "forbidden"}]}

    client = _Failing(docs={(WORK_INDEX, "w-4"): {
        "task_state": "ready", "arrival_seq": 4}})
    audit = CleanupAuditLog(operator="t5-test")
    with pytest.raises(RuntimeError, match="failures"):
        execute_work_task_cleanup(client, TODAY, audit)
    assert any(run["ok"] is False for run in audit.work_task_runs)


# ---------- 索引级清理：非终态在位 → 缓删+告警（钉 6.4） ----------


def test_index_level_cleanup_defers_work_with_active_tasks():
    client = _WorkClient(
        docs={(WORK_INDEX, "w-1"): {"task_state": "retry_wait",
                                    "arrival_seq": 1}},
        present={WORK_INDEX, "news-dedup-items-v1-2026.09.18",
                 "news-dedup-audits-v1-2026.09.18"})
    audit = CleanupAuditLog(operator="t5-test")
    report = execute_cleanup(client, TODAY, audit)
    # work 缓删（非终态在位——不删不越点），items/audits 照删（独立）。
    assert report["deferred"] == [WORK_INDEX]
    assert WORK_INDEX not in report["deleted"]
    assert sorted(report["deleted"]) == [
        "news-dedup-audits-v1-2026.09.18",
        "news-dedup-items-v1-2026.09.18"]
    assert WORK_INDEX in {key[0] for key in client.docs}   # 文档在位
    by_name = {run["index_name"]: run for run in audit.deletion_runs}
    assert by_name[WORK_INDEX]["ok"] is False
    assert by_name[WORK_INDEX]["error"] == "active_tasks_present"


def test_index_level_cleanup_deletes_work_once_active_gone():
    drained = _WorkClient(
        docs={(WORK_INDEX, "w-4"): {"task_state": "ready", "arrival_seq": 4}},
        present={WORK_INDEX})
    audit = CleanupAuditLog(operator="t5-test")
    report = execute_cleanup(drained, TODAY, audit)
    assert report["deferred"] == []
    assert report["deleted"] == [WORK_INDEX]
    assert WORK_INDEX not in {key[0] for key in drained.docs}


def test_dry_run_cleanup_previews_deferral():
    client = _WorkClient(
        docs={(WORK_INDEX, "w-1"): {"task_state": "leased", "arrival_seq": 1}},
        present={WORK_INDEX, "news-dedup-tombstones-v1-2026.09.18"})
    audit = CleanupAuditLog(operator="t5-test")
    report = dry_run_cleanup(client, TODAY, audit)
    assert report["deferred"] == [WORK_INDEX]
    assert WORK_INDEX not in report["would_delete"]
    assert report["would_delete"] == [TOMBSTONES_INDEX]   # 无任务态概念


# ---------- 超期残留枚举（清理域全集覆盖） ----------


def test_residue_enumeration_covers_work_and_tombstones():
    listing = {
        "news-dedup-work-v1-2026.09.10": {},        # 残留（早 cutoff）
        "news-dedup-tombstones-v1-2026.09.10": {},  # 残留
        "news-dedup-items-v1-2026.09.18": {},       # == cutoff（T-7 点名）
        "news-dedup-work-v1-2026.10.01": {},        # 新于 cutoff
        "news-dedup-work-v1-2026.13.99": {},        # 异形 → 告警
    }

    class _ListingClient:
        indices = type("I", (), {
            "get": staticmethod(lambda *, index, allow_no_indices,
                                ignore_unavailable: dict(
                                    (name, {}) for name in listing)),
        })()

    report = enumerate_overdue_residue(_ListingClient(), TODAY)
    assert report["residue"] == [
        "news-dedup-tombstones-v1-2026.09.10", "news-dedup-work-v1-2026.09.10"]
    assert report["alien_alarms"] == ["news-dedup-work-v1-2026.13.99"]


# ---------- 活任务连续性：TTL 拒删后可续 claim/收口（钉 6.4） ----------


def _seed_head(store, allocated=3):
    store.create(CONTROL_INDEX, HEAD_ID, {
        "kind": "admission", "owner_id": "writer-1",
        "last_allocated_seq": allocated, "last_terminal": 0,
        "last_decision_seq": 0, "config_version": ADMISSION_CONFIG_VERSION,
        "checkpoint": {}, "updated_at": _utc(NOW),
    })


def _work_body(seq, *, state, **updates):
    item = _new_work_item(
        request(request_id=f"{seq}-1", item_id=f"{seq}-1",
                text=f"正文{seq}"),
        _digest(["default", f"{seq}-1"]), seq, NOW, "2026-09-18",
        NOW + timedelta(days=7))
    body = item.model_dump(mode="json")
    body["task_state"] = state
    body.update(updates)
    return body


def _seed_work_doc(store, seq, *, state, **updates):
    body = _work_body(seq, state=state, **updates)
    store.create(work_index("2026-09-18"),
                 work_item_id("default", "2026-09-18", seq), body)
    return body


def _durable(store, when=NOW, durable_queue=None):
    clock = when if callable(when) else (lambda: when)
    return BatchAdmissionCoordinator(
        store, owner_id="writer-1", owner_isolated=lambda: True,
        limits=BatchLimits(), clock=clock,
        use_durable_queue=True, durable_queue=durable_queue)


def test_active_tasks_survive_ttl_and_remain_claimable():
    """清理拒删的同一活态族在协调器世界各归其道（钉 6.4）：

    retry_wait（退避到期）→ claim→物化→ready（活任务可续收口）；
    leased（他方活租约）→ busy 跳过——在位不动（等其持有者收口/租约
    到期接管）；compensating → busy（补正域驱动，不进 claim 通道
    ——es_store.py:395 现役口径）——在位不动。水位连续前缀止于
    leased/compensating 之后（fail-closed 不猜）。
    """
    store = BatchMemoryStore()
    _seed_head(store, allocated=3)
    _seed_work_doc(store, 1, state="retry_wait",
                   retry_count=1, next_retry_at=_utc(NOW),
                   first_failed_at=_utc(NOW - timedelta(seconds=5)),
                   last_failed_at=_utc(NOW - timedelta(seconds=5)),
                   last_failure_class="transient_infra",
                   last_error_code="bulk_unknown",
                   last_error_summary="synthetic transient")
    _seed_work_doc(store, 2, state="leased", lease_owner="other-worker",
                   lease_expires_at=_utc(NOW + timedelta(seconds=60)),
                   lease_generation=1)
    _seed_work_doc(store, 3, state="compensating")
    service = _durable(store)
    terminal = service.materialize_oldest()
    # 终态证据连续前缀：1 ready、2/3 在位 → 前缀止于 1。
    assert terminal == 1
    docs = {seq: store.get(work_index("2026-09-18"),
                           work_item_id("default", "2026-09-18", seq))["source"]
            for seq in (1, 2, 3)}
    assert docs[1]["task_state"] == "ready"           # retry_wait 续驱收口
    # 活租约件在位未动（TTL 拒删=claim 不越权）——owner/代次原值。
    assert docs[2]["task_state"] == "leased"
    assert docs[2]["lease_owner"] == "other-worker"
    assert docs[2]["lease_generation"] == 1
    # 补偿件在位（补正域驱动；claim 通道不越权收编）。
    assert docs[3]["task_state"] == "compensating"


# ---------- 跨日重试复用首次身份（钉 6.1/6.4 呼应；重试日时钟注入） ----------


def _admission(request_id, text, when):
    return AdmissionRequest(
        scope_id="default", request_id=request_id, item_id=request_id,
        text=text, received_at=when, schema_version="1",
        pipeline_version="v1", embedding_space_id="v3",
        delivery_route_ref="audit-route-v1",
        trace_id=f"trace-{request_id}",
    )


def test_cross_day_retry_reuses_first_identity_durable():
    """D 日 23:59:30（Asia/Shanghai）首次受理；D+1 00:00:30 重试日时钟
    注入重提 → 复用首次 business_date/arrival_seq/expires_at（按重试
    日重算身份=禁止——复用读回即负钉）；任务文档驻 D 日 work 索引。
    """
    sha = BUSINESS_ZONE
    d_late = datetime(2026, 9, 24, 23, 59, 30, tzinfo=sha)
    d1_early = datetime(2026, 9, 25, 0, 0, 30, tzinfo=sha)
    store = BatchMemoryStore()
    first = _durable(store, when=d_late).accept_batch(
        [_admission("1001-1", "甲公司2026年9月24日发布回购。", d_late)])[0]
    assert first.business_date == "2026-09-24"
    # D+1 时钟的新协调器同身份重提（重试日时钟注入）。
    retry = _durable(store, when=d1_early).accept_batch(
        [_admission("1001-1", "甲公司2026年9月24日发布回购。", d1_early)])[0]
    assert retry.reused is True
    assert retry.business_date == "2026-09-24"          # 首次业务日冻结
    assert retry.arrival_seq == first.arrival_seq == 1
    assert retry.record_id == first.record_id
    assert retry.accepted_at == first.accepted_at
    assert retry.expires_at == first.expires_at
    # 任务文档驻 D 日 work 索引；D+1 work 索引无文档（跨日不重算落位）。
    key = work_item_id("default", "2026-09-24", 1)
    doc = store.get(work_index("2026-09-24"), key)["source"]
    assert doc["business_date"] == "2026-09-24"
    assert doc["arrival_seq"] == 1 and doc["task_state"] == "pending"
    assert store.get(work_index("2026-09-25"), key) is None


# ---------- 滚动创建：work 进滚动集（tombstones 按需不进） ----------


def test_day_rollover_includes_work_not_tombstones():
    ROOT = Path(__file__).resolve().parents[2]
    scripts = str(ROOT / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    import provision_day_rollover as rollover  # noqa: E402

    names = rollover.rollover_indices(D7)
    assert set(names) == {
        "news-dedup-items-v1-2026.09.18",
        "news-dedup-audits-v1-2026.09.18",
        "news-dedup-work-v1-2026.09.18",
    }
    assert "news-dedup-tombstones-v1-2026.09.18" not in names
    for name in names:
        assert is_valid_cleanup_target(name)     # 双闸同源（lifecycle 硬正则）
