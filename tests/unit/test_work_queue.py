"""P1a-T1 队列持久化原语钉（零成本：内存 store+注入时钟+Fake 检索面）。

设计锚 = log\\P1a-预备设计-2026-10-11.md §1.1（字段定稿）/§1.3（租约）+
验收钉 6.2 的单元面（fencing——旧 owner CAS 必败；崩溃点矩阵/终态恰一份
的编排面钉在 P1a-T3 claim 循环测试族）：

- schema：33 字段 strict 契约+状态机不变量（extra=forbid/值域/枚举）+
  ES 映射逐字段镜像钉（schema↔mapping 两处同步纪律）；
- enqueue：确定性 ID create→读回对拍（重放幂等一致；冻结字段分歧=
  IdentityConflict=IDENTITY_CORRUPTION 口径）；
- claim/租约：代次单调；过期接管（claim 顺带+独立 recover 两路）；
- defer：T2 条目账持久化语义（确定失败计数/UNKNOWN_WRITE 零计数即时
  读回/first_failed_at 首证保持）；
- complete/terminalize：终态收口+幂等重放+僵尸 fencing 拒绝；
- 检索面：scan_expired_leases（注入时钟；404=空 fail-closed）；
- 真存储面集成：ElasticsearchBatchStore+FakeESClient 全原语贯通+
  work_documents↔bulk_create_classified 对接（T4 存储面零改动复用）。
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from typing import get_args

import pytest

from news_flash_dedup import work_queue
from news_flash_dedup.admission import IdentityConflict, _digest, _utc
from news_flash_dedup.es_admission_schema import work_mapping
from news_flash_dedup.materialize_failure import (
    FailureClass, MaterializeFailure,
)
from news_flash_dedup.materialize_terminal import tombstone_id
from news_flash_dedup.work_queue import (
    ACTIVE_TASK_STATES, FROZEN_FIELDS, TERMINAL_TASK_STATES, WorkItemV1,
    WorkLeaseLost, WorkPersistUnknown, WorkResultV1,
    claim_work_item, complete_work_item, defer_work_item,
    enqueue_work_item, load_work_item, recover_expired_lease,
    scan_expired_leases, terminalize_work_item, validate_work_item_invariants,
    work_documents, work_index, work_item_id,
)

from b4_fake_es import FakeESClient
from test_admission import MemoryStore


NOW = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)
BUSINESS_DATE = "2026-10-10"
TEXT = "甲公司发布三季度财报，营收同比增长两成。"


def make_item(seq: int = 1, *, text: str = TEXT,
              business_date: str = BUSINESS_DATE,
              **overrides) -> WorkItemV1:
    """构造合法 pending 任务（默认值即入队合法形态）。"""
    item_id = f"100-{seq}"
    payload = {
        "scope_id": "default", "request_id": item_id, "item_id": item_id,
        "record_id": _digest(["default", item_id]),
        "business_fingerprint": _digest({"item_id": item_id, "text": text}),
        "business_date": business_date, "arrival_seq": seq,
        "expires_at": "2026-10-17T00:00:00.000000Z",
        "received_at": "2026-10-10T11:59:59.000000Z",
        "accepted_at": "2026-10-10T12:00:00.000000Z",
        "schema_version": "1", "pipeline_version": "dedup_v1",
        "embedding_space_id": "v3", "delivery_route_ref": "audit-route-v1",
        "trace_id": "trace-1",
        "text": text,
        "raw_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "enqueued_at": _utc(NOW), "updated_at": _utc(NOW),
    }
    payload.update(overrides)
    return WorkItemV1.model_validate(payload)


def make_result(seq: int = 1, **overrides) -> WorkResultV1:
    payload = dict(
        decision="duplicate", duplicate_ids=["0" * 64],
        reason="EXACT_TEXT_MATCH",
        record_id=_digest(["default", f"100-{seq}"]), arrival_seq=seq)
    payload.update(overrides)
    return WorkResultV1.model_validate(payload)


def transient_failure() -> MaterializeFailure:
    return MaterializeFailure(
        failure_class=FailureClass.TRANSIENT_INFRA, error_code="http_503",
        detail="connection timed out")


def unknown_failure() -> MaterializeFailure:
    return MaterializeFailure(
        failure_class=FailureClass.UNKNOWN_WRITE, error_code="http_409",
        detail="write outcome unconfirmed")


# ---------- schema 契约 ----------


def test_schema_contract_has_33_strict_fields():
    fields = WorkItemV1.model_fields
    assert len(fields) == 33
    assert set(FROZEN_FIELDS) <= set(fields)
    assert WorkItemV1.model_config["extra"] == "forbid"


def test_schema_rejects_extra_fields_and_bad_values():
    with pytest.raises(Exception):
        WorkItemV1.model_validate({**make_item().model_dump(mode="json"),
                                   "rogue_field": "x"})
    with pytest.raises(Exception):
        make_item(record_id="not-hex")
    with pytest.raises(Exception):
        make_item(arrival_seq=0)
    with pytest.raises(Exception):
        make_item(business_date="2026/10/10")


def test_schema_state_enums_are_pinned():
    item = make_item()
    assert item.task_state == "pending"
    assert item.materialize_state == "accepted"
    assert TERMINAL_TASK_STATES == frozenset(
        {"ready", "tombstoned", "expired"})
    assert ACTIVE_TASK_STATES == frozenset(
        {"pending", "leased", "retry_wait", "compensating"})


def test_work_mapping_mirrors_schema_field_set():
    """映射↔schema 两处同步钉（字段增删必须两处一起改——防漂移守卫）。"""
    properties = work_mapping()["mappings"]["properties"]
    assert set(properties) == set(WorkItemV1.model_fields)
    assert work_mapping()["mappings"]["dynamic"] == "strict"
    assert properties["text"]["index"] is False        # 正文不入检索面
    assert properties["result"]["enabled"] is False    # 快照=对账面不查询
    assert properties["last_error_summary"]["index"] is False
    assert properties["arrival_seq"]["type"] == "long"
    assert properties["task_state"]["type"] == "keyword"


def test_failure_class_literal_syncs_with_t1_classifier():
    """last_failure_class 值域与 T1 FailureClass 五类逐值同步钉。"""
    annotation = WorkItemV1.model_fields["last_failure_class"].annotation
    literal_args = [arg for arg in get_args(annotation)
                    if arg is not type(None)]
    values = set()
    for arg in literal_args:
        values |= set(get_args(arg))
    assert values == {member.value for member in FailureClass}


def test_invariants_reject_illegal_state_shapes():
    base = make_item().model_dump(mode="json")
    # leased 缺租约三元组
    with pytest.raises(ValueError):
        validate_work_item_invariants(WorkItemV1.model_validate(
            {**base, "task_state": "leased"}))
    with pytest.raises(ValueError):
        validate_work_item_invariants(WorkItemV1.model_validate(
            {**base, "task_state": "leased", "lease_owner": "w1",
             "lease_expires_at": _utc(NOW)}))          # generation 0
    # retry_wait 缺退避时刻
    with pytest.raises(ValueError):
        validate_work_item_invariants(WorkItemV1.model_validate(
            {**base, "task_state": "retry_wait"}))
    # tombstoned 缺终态理由 / ready 带理由（ready 的 result 可空=
    # P1a-T2 勘误：物化收口早于决策产出，结论镜像由 commit 域后补）
    with pytest.raises(ValueError):
        validate_work_item_invariants(WorkItemV1.model_validate(
            {**base, "task_state": "tombstoned"}))
    with pytest.raises(ValueError):
        validate_work_item_invariants(WorkItemV1.model_validate(
            {**base, "task_state": "ready",
             "terminal_reason": "http_400",
             "result": make_result().model_dump(mode="json")}))
    # P1a-T2：ready 无快照=合法占位（决策未产）；带快照亦合法
    validate_work_item_invariants(WorkItemV1.model_validate(
        {**base, "task_state": "ready"}))
    validate_work_item_invariants(WorkItemV1.model_validate(
        {**base, "task_state": "ready",
         "result": make_result().model_dump(mode="json")}))
    # 终态持活租约
    with pytest.raises(ValueError):
        validate_work_item_invariants(WorkItemV1.model_validate(
            {**base, "task_state": "tombstoned",
             "terminal_reason": "http_400", "lease_owner": "w1",
             "lease_expires_at": _utc(NOW + timedelta(seconds=60))}))
    # 合法形态全通过（baseline）
    validate_work_item_invariants(make_item())


# ---------- 确定性 ID / 索引名 ----------


def test_work_index_uses_day_index_kind():
    assert work_index("2026-10-10") == "news-dedup-work-v1-2026.10.10"


def test_work_item_id_is_deterministic_and_tombstone_family():
    first = work_item_id("default", BUSINESS_DATE, 1)
    assert first == work_item_id("default", BUSINESS_DATE, 1)
    assert first != work_item_id("default", BUSINESS_DATE, 2)
    # 与墓碑 ID 同族工艺（同参 digest——不同索引不冲突；蓝图 1.1 原文口径）
    assert first == tombstone_id("default", BUSINESS_DATE, 1)
    with pytest.raises(ValueError):
        work_item_id("", BUSINESS_DATE, 1)
    with pytest.raises(ValueError):
        work_item_id("default", BUSINESS_DATE, 0)


# ---------- enqueue ----------


def test_enqueue_confirms_with_readback():
    store = MemoryStore()
    result = enqueue_work_item(store, make_item())
    assert result.confirmed and result.created
    assert result.index == work_index(BUSINESS_DATE)
    document = store.get(result.index, result.work_id)
    assert document["source"]["task_state"] == "pending"
    assert document["source"]["materialize_state"] == "accepted"
    assert document["source"]["lease_generation"] == 0
    assert document["source"] == make_item().model_dump(mode="json")


def test_enqueue_replay_is_idempotent():
    store = MemoryStore()
    first = enqueue_work_item(store, make_item())
    second = enqueue_work_item(store, make_item())
    assert second.confirmed and not second.created
    assert second.existing_source == make_item().model_dump(mode="json")
    # 首证为准：重放不覆盖（seq_no 未动）
    document = store.get(first.index, first.work_id)
    assert document["seq_no"] == 0


def test_enqueue_replay_divergence_is_identity_conflict():
    store = MemoryStore()
    enqueue_work_item(store, make_item())
    divergent = make_item(text="乙公司发布三季度财报。")
    with pytest.raises(IdentityConflict, match="frozen fields"):
        enqueue_work_item(store, divergent)


def test_enqueue_unknown_writes_are_temporary_not_confirmed():
    class UnknownCreateStore(MemoryStore):
        def create(self, index, key, body):
            raise RuntimeError("transport is unknown")

    with pytest.raises(WorkPersistUnknown):
        enqueue_work_item(UnknownCreateStore(), make_item())

    store = MemoryStore()
    store.after_get = lambda *args: (_ for _ in ()).throw(
        RuntimeError("readback is unknown"))
    with pytest.raises(WorkPersistUnknown):
        # create 成功但读回未证：不得当作已确认（confirmed 才是推进凭据）
        enqueue_work_item(store, make_item())


def test_enqueue_rejects_terminal_body():
    ready = make_item(task_state="ready",
                      result=make_result().model_dump(mode="json"))
    with pytest.raises(Exception, match="terminal"):
        enqueue_work_item(MemoryStore(), ready)


# ---------- claim / 租约 ----------


def test_claim_pending_grants_lease_with_generation_one():
    store = MemoryStore()
    enqueue_work_item(store, make_item(seq=7))
    result = claim_work_item(store, "default", BUSINESS_DATE, 7,
                              owner_id="worker-a", now=NOW)
    assert result.outcome == "claimed"
    assert result.lease_generation == 1
    assert result.lease_owner == "worker-a"
    assert result.lease_expires_at == _utc(NOW + timedelta(seconds=60))
    item = load_work_item(store, "default", BUSINESS_DATE, 7)
    assert item.task_state == "leased"
    assert item.materialize_state == "materializing"


def test_claim_is_busy_while_lease_is_live():
    store = MemoryStore()
    enqueue_work_item(store, make_item())
    claim_work_item(store, "default", BUSINESS_DATE, 1,
                    owner_id="worker-a", now=NOW)
    same_owner = claim_work_item(store, "default", BUSINESS_DATE, 1,
                                 owner_id="worker-a", now=NOW + timedelta(seconds=1))
    other_owner = claim_work_item(store, "default", BUSINESS_DATE, 1,
                                  owner_id="worker-b", now=NOW + timedelta(seconds=1))
    assert same_owner.outcome == "busy"
    assert other_owner.outcome == "busy"
    assert other_owner.lease_generation == 1


def test_claim_is_waiting_before_backoff_expiry_then_grants():
    store = MemoryStore()
    enqueue_work_item(store, make_item())
    claim = claim_work_item(store, "default", BUSINESS_DATE, 1,
                            owner_id="worker-a", now=NOW)
    assert defer_work_item(
        store, "default", BUSINESS_DATE, 1, failure=transient_failure(),
        delay_seconds=30.0, definite=True, owner_id="worker-a",
        lease_generation=claim.lease_generation, now=NOW) == "deferred"
    early = claim_work_item(store, "default", BUSINESS_DATE, 1,
                            owner_id="worker-b",
                            now=NOW + timedelta(seconds=29))
    assert early.outcome == "waiting"
    due = claim_work_item(store, "default", BUSINESS_DATE, 1,
                          owner_id="worker-b",
                          now=NOW + timedelta(seconds=30))
    assert due.outcome == "claimed"
    assert due.lease_generation == 2      # 代次单调：claim 再+1


def test_claim_takes_over_expired_lease():
    store = MemoryStore()
    enqueue_work_item(store, make_item())
    claim_work_item(store, "default", BUSINESS_DATE, 1,
                    owner_id="worker-a", now=NOW)
    takeover = claim_work_item(store, "default", BUSINESS_DATE, 1,
                               owner_id="worker-b",
                               now=NOW + timedelta(seconds=61))
    assert takeover.outcome == "claimed"
    assert takeover.lease_generation == 2
    assert takeover.lease_owner == "worker-b"


def test_claim_terminal_and_absent_outcomes():
    store = MemoryStore()
    enqueue_work_item(store, make_item())
    claim = claim_work_item(store, "default", BUSINESS_DATE, 1,
                            owner_id="worker-a", now=NOW)
    complete_work_item(store, "default", BUSINESS_DATE, 1,
                       result=make_result(), owner_id="worker-a",
                       lease_generation=claim.lease_generation, now=NOW)
    assert claim_work_item(
        store, "default", BUSINESS_DATE, 1, owner_id="worker-b",
        now=NOW + timedelta(seconds=1)).outcome == "terminal"
    assert claim_work_item(
        store, "default", BUSINESS_DATE, 99, owner_id="worker-b",
        now=NOW).outcome == "absent"


def test_claim_retries_through_spurious_cas_conflict():
    class FlakyReplaceStore(MemoryStore):
        def __init__(self):
            super().__init__()
            self.fail_once = True

        def replace(self, index, key, body, seq_no, primary_term):
            if self.fail_once:
                self.fail_once = False
                from test_admission import Conflict
                raise Conflict()
            return super().replace(index, key, body, seq_no, primary_term)

    store = FlakyReplaceStore()
    enqueue_work_item(store, make_item())
    result = claim_work_item(store, "default", BUSINESS_DATE, 1,
                             owner_id="worker-a", now=NOW)
    assert result.outcome == "claimed"
    assert load_work_item(store, "default", BUSINESS_DATE,
                          1).lease_owner == "worker-a"


# ---------- defer（T2 条目账持久化语义） ----------


def test_defer_definite_failure_counts_and_backs_off():
    store = MemoryStore()
    enqueue_work_item(store, make_item())
    claim = claim_work_item(store, "default", BUSINESS_DATE, 1,
                            owner_id="worker-a", now=NOW)
    outcome = defer_work_item(
        store, "default", BUSINESS_DATE, 1, failure=transient_failure(),
        delay_seconds=30.0, definite=True, owner_id="worker-a",
        lease_generation=claim.lease_generation, now=NOW)
    assert outcome == "deferred"
    item = load_work_item(store, "default", BUSINESS_DATE, 1)
    assert item.task_state == "retry_wait"
    assert item.materialize_state == "retry_wait"
    assert item.retry_count == 1
    assert item.next_retry_at == _utc(NOW + timedelta(seconds=30))
    assert item.first_failed_at == _utc(NOW)
    assert item.last_failed_at == _utc(NOW)
    assert item.last_failure_class == "transient_infra"
    assert item.last_error_code == "http_503"
    assert item.lease_owner is None            # 退避期不持约
    assert item.lease_generation == 1          # 代次保留（单调证据）


def test_defer_unknown_write_zero_counts_immediate_retry():
    store = MemoryStore()
    enqueue_work_item(store, make_item())
    claim = claim_work_item(store, "default", BUSINESS_DATE, 1,
                            owner_id="worker-a", now=NOW)
    assert defer_work_item(
        store, "default", BUSINESS_DATE, 1, failure=unknown_failure(),
        delay_seconds=0.0, definite=False, owner_id="worker-a",
        lease_generation=claim.lease_generation, now=NOW) == "deferred"
    item = load_work_item(store, "default", BUSINESS_DATE, 1)
    assert item.retry_count == 0               # UNKNOWN_WRITE 不计数（T2 口径）
    assert item.next_retry_at == _utc(NOW)     # 空槽=即时刻（立即读回）
    assert item.last_failure_class == "unknown_write"


def test_defer_keeps_first_failure_time_across_attempts():
    store = MemoryStore()
    enqueue_work_item(store, make_item())
    claim = claim_work_item(store, "default", BUSINESS_DATE, 1,
                            owner_id="worker-a", now=NOW)
    defer_work_item(store, "default", BUSINESS_DATE, 1,
                    failure=transient_failure(), delay_seconds=10.0,
                    definite=True, owner_id="worker-a",
                    lease_generation=claim.lease_generation, now=NOW)
    later = NOW + timedelta(seconds=10)
    reclaim = claim_work_item(store, "default", BUSINESS_DATE, 1,
                              owner_id="worker-a", now=later)
    assert reclaim.lease_generation == 2
    defer_work_item(store, "default", BUSINESS_DATE, 1,
                    failure=transient_failure(), delay_seconds=10.0,
                    definite=True, owner_id="worker-a",
                    lease_generation=reclaim.lease_generation,
                    now=later + timedelta(seconds=5))
    item = load_work_item(store, "default", BUSINESS_DATE, 1)
    assert item.retry_count == 2
    assert item.first_failed_at == _utc(NOW)   # 首证保持
    assert item.last_failed_at == _utc(later + timedelta(seconds=5))


def test_defer_replay_is_already_deferred():
    store = MemoryStore()
    enqueue_work_item(store, make_item())
    claim = claim_work_item(store, "default", BUSINESS_DATE, 1,
                            owner_id="worker-a", now=NOW)
    defer_work_item(store, "default", BUSINESS_DATE, 1,
                    failure=transient_failure(), delay_seconds=30.0,
                    definite=True, owner_id="worker-a",
                    lease_generation=claim.lease_generation, now=NOW)
    replay = defer_work_item(
        store, "default", BUSINESS_DATE, 1, failure=transient_failure(),
        delay_seconds=30.0, definite=True, owner_id="worker-a",
        lease_generation=claim.lease_generation,
        now=NOW + timedelta(seconds=1))
    assert replay == "already_deferred"
    item = load_work_item(store, "default", BUSINESS_DATE, 1)
    assert item.retry_count == 1               # 退避期不重复落账


def test_defer_without_lease_is_state_machine_violation():
    store = MemoryStore()
    enqueue_work_item(store, make_item())
    with pytest.raises(Exception, match="not leased"):
        defer_work_item(store, "default", BUSINESS_DATE, 1,
                        failure=transient_failure(), delay_seconds=1.0,
                        definite=True, owner_id="worker-a",
                        lease_generation=1, now=NOW)


# ---------- complete ----------


def test_complete_flips_ready_with_result_snapshot():
    store = MemoryStore()
    enqueue_work_item(store, make_item())
    claim = claim_work_item(store, "default", BUSINESS_DATE, 1,
                            owner_id="worker-a", now=NOW)
    result = make_result()
    assert complete_work_item(
        store, "default", BUSINESS_DATE, 1, result=result,
        owner_id="worker-a", lease_generation=claim.lease_generation,
        now=NOW) == "completed"
    item = load_work_item(store, "default", BUSINESS_DATE, 1)
    assert item.task_state == "ready"
    assert item.materialize_state == "ready"
    assert item.result == result
    assert item.terminal_reason == ""
    assert item.lease_owner is None and item.lease_expires_at is None
    assert item.lease_generation == 1          # 代次保留（fencing 证据）


def test_complete_replay_is_already_completed():
    store = MemoryStore()
    enqueue_work_item(store, make_item())
    claim = claim_work_item(store, "default", BUSINESS_DATE, 1,
                            owner_id="worker-a", now=NOW)
    args = dict(result=make_result(), owner_id="worker-a",
                lease_generation=claim.lease_generation, now=NOW)
    assert complete_work_item(store, "default", BUSINESS_DATE, 1,
                              **args) == "completed"
    assert complete_work_item(store, "default", BUSINESS_DATE, 1,
                              **args) == "already_completed"


def test_complete_accepts_placeholder_result_and_replays():
    # P1a-T2 勘误：物化收口早于决策产出——result=None=占位态
    # （结论镜像由 commit 域后补）；占位重放=already；占位↔快照分歧=
    # IdentityConflict（对账面不吞分歧）。
    store = MemoryStore()
    enqueue_work_item(store, make_item())
    claim = claim_work_item(store, "default", BUSINESS_DATE, 1,
                            owner_id="worker-a", now=NOW)
    args = dict(result=None, owner_id="worker-a",
                lease_generation=claim.lease_generation, now=NOW)
    assert complete_work_item(store, "default", BUSINESS_DATE, 1,
                              **args) == "completed"
    item = load_work_item(store, "default", BUSINESS_DATE, 1)
    assert item.task_state == "ready" and item.result is None
    assert complete_work_item(store, "default", BUSINESS_DATE, 1,
                              **args) == "already_completed"
    with pytest.raises(IdentityConflict, match="snapshot"):
        complete_work_item(store, "default", BUSINESS_DATE, 1,
                           result=make_result(), owner_id="worker-a",
                           lease_generation=claim.lease_generation, now=NOW)


def test_complete_snapshot_divergence_is_identity_conflict():
    store = MemoryStore()
    enqueue_work_item(store, make_item())
    claim = claim_work_item(store, "default", BUSINESS_DATE, 1,
                            owner_id="worker-a", now=NOW)
    complete_work_item(store, "default", BUSINESS_DATE, 1,
                       result=make_result(), owner_id="worker-a",
                       lease_generation=claim.lease_generation, now=NOW)
    with pytest.raises(IdentityConflict, match="snapshot"):
        complete_work_item(store, "default", BUSINESS_DATE, 1,
                           result=make_result(decision="unique",
                                              reason="NO_OVERLAP"),
                           owner_id="worker-a",
                           lease_generation=claim.lease_generation, now=NOW)


def test_complete_refuses_terminalized_task_without_revival():
    store = MemoryStore()
    enqueue_work_item(store, make_item())
    claim = claim_work_item(store, "default", BUSINESS_DATE, 1,
                            owner_id="worker-a", now=NOW)
    terminalize_work_item(store, "default", BUSINESS_DATE, 1,
                          task_state="tombstoned",
                          terminal_reason="http_400", owner_id="worker-a",
                          lease_generation=claim.lease_generation, now=NOW)
    with pytest.raises(Exception, match="different terminal"):
        complete_work_item(store, "default", BUSINESS_DATE, 1,
                           result=make_result(), owner_id="worker-a",
                           lease_generation=claim.lease_generation, now=NOW)


# ---------- terminalize ----------


def test_terminalize_tombstoned_keeps_reason_and_generation():
    store = MemoryStore()
    enqueue_work_item(store, make_item())
    claim = claim_work_item(store, "default", BUSINESS_DATE, 1,
                            owner_id="worker-a", now=NOW)
    assert terminalize_work_item(
        store, "default", BUSINESS_DATE, 1, task_state="tombstoned",
        terminal_reason="http_400", owner_id="worker-a",
        lease_generation=claim.lease_generation, now=NOW) == "terminalized"
    item = load_work_item(store, "default", BUSINESS_DATE, 1)
    assert item.task_state == "tombstoned"
    assert item.materialize_state == "tombstone"
    assert item.terminal_reason == "http_400"
    assert item.result is None                 # 墓态件不存结论正文
    assert item.lease_owner is None
    assert item.lease_generation == 1


def test_terminalize_replay_is_already_terminalized():
    store = MemoryStore()
    enqueue_work_item(store, make_item())
    claim = claim_work_item(store, "default", BUSINESS_DATE, 1,
                            owner_id="worker-a", now=NOW)
    args = dict(task_state="tombstoned", terminal_reason="http_400",
                owner_id="worker-a", lease_generation=claim.lease_generation,
                now=NOW)
    assert terminalize_work_item(store, "default", BUSINESS_DATE,
                                 1, **args) == "terminalized"
    assert terminalize_work_item(store, "default", BUSINESS_DATE,
                                 1, **args) == "already_terminalized"
    with pytest.raises(IdentityConflict):
        terminalize_work_item(store, "default", BUSINESS_DATE, 1,
                              task_state="tombstoned",
                              terminal_reason="http_422", owner_id="worker-a",
                              lease_generation=claim.lease_generation,
                              now=NOW)


def test_terminalize_expired_unleased_closes_pending_task():
    """逻辑到期闸（蓝图 1.5）：无约收口 pending 任务——不重物化、无租约。"""
    store = MemoryStore()
    enqueue_work_item(store, make_item())
    assert terminalize_work_item(
        store, "default", BUSINESS_DATE, 1, task_state="expired",
        terminal_reason="logical_expiry", owner_id=None, lease_generation=0,
        now=NOW) == "terminalized"
    item = load_work_item(store, "default", BUSINESS_DATE, 1)
    assert item.task_state == "expired"
    assert item.materialize_state == "expired"
    assert item.terminal_reason == "logical_expiry"
    # 无约重放：代次对无约观察者无意义——同态同理由即幂等
    assert terminalize_work_item(
        store, "default", BUSINESS_DATE, 1, task_state="expired",
        terminal_reason="logical_expiry", owner_id=None, lease_generation=0,
        now=NOW + timedelta(seconds=1)) == "already_terminalized"


def test_terminalize_unleased_rejects_live_lease():
    store = MemoryStore()
    enqueue_work_item(store, make_item())
    claim_work_item(store, "default", BUSINESS_DATE, 1,
                    owner_id="worker-a", now=NOW)
    with pytest.raises(Exception, match="live lease"):
        terminalize_work_item(store, "default", BUSINESS_DATE, 1,
                              task_state="expired",
                              terminal_reason="logical_expiry",
                              owner_id=None, lease_generation=0, now=NOW)


def test_terminalize_rejects_invalid_state_and_reason():
    store = MemoryStore()
    enqueue_work_item(store, make_item())
    with pytest.raises(ValueError):
        terminalize_work_item(store, "default", BUSINESS_DATE, 1,
                              task_state="ready", terminal_reason="x",
                              owner_id=None, lease_generation=0, now=NOW)
    with pytest.raises(ValueError):
        terminalize_work_item(store, "default", BUSINESS_DATE, 1,
                              task_state="tombstoned", terminal_reason="",
                              owner_id=None, lease_generation=0, now=NOW)


# ---------- recover_expired_lease + fencing（钉 6.2 单元面） ----------


def test_recover_expired_lease_reassigns_with_next_generation():
    store = MemoryStore()
    enqueue_work_item(store, make_item())
    claim_work_item(store, "default", BUSINESS_DATE, 1,
                    owner_id="worker-a", now=NOW)
    result = recover_expired_lease(
        store, "default", BUSINESS_DATE, 1, owner_id="worker-b",
        now=NOW + timedelta(seconds=61))
    assert result.outcome == "recovered"
    assert result.lease_generation == 2
    assert result.lease_owner == "worker-b"
    assert result.lease_expires_at == _utc(
        NOW + timedelta(seconds=61 + 60))
    item = load_work_item(store, "default", BUSINESS_DATE, 1)
    assert item.task_state == "leased"         # 接管即续驱（物化中）
    assert item.materialize_state == "materializing"


def test_recover_expired_lease_outcomes():
    store = MemoryStore()
    enqueue_work_item(store, make_item())
    # 活租约=busy
    claim_work_item(store, "default", BUSINESS_DATE, 1,
                    owner_id="worker-a", now=NOW)
    assert recover_expired_lease(
        store, "default", BUSINESS_DATE, 1, owner_id="worker-b",
        now=NOW + timedelta(seconds=1)).outcome == "busy"
    # 非 leased 活态=busy（claim 通道负责）
    defer_claim = claim_work_item(store, "default", BUSINESS_DATE, 1,
                                  owner_id="worker-a", now=NOW)
    defer_work_item(store, "default", BUSINESS_DATE, 1,
                    failure=transient_failure(), delay_seconds=3600.0,
                    definite=True, owner_id="worker-a",
                    lease_generation=defer_claim.lease_generation, now=NOW)
    assert recover_expired_lease(
        store, "default", BUSINESS_DATE, 1, owner_id="worker-b",
        now=NOW + timedelta(seconds=2)).outcome == "busy"
    # 终态=terminal；缺席=absent
    enqueue_work_item(store, make_item(seq=2))
    claim2 = claim_work_item(store, "default", BUSINESS_DATE, 2,
                             owner_id="worker-a", now=NOW)
    complete_work_item(store, "default", BUSINESS_DATE, 2,
                       result=make_result(seq=2), owner_id="worker-a",
                       lease_generation=claim2.lease_generation, now=NOW)
    assert recover_expired_lease(
        store, "default", BUSINESS_DATE, 2, owner_id="worker-b",
        now=NOW + timedelta(seconds=90)).outcome == "terminal"
    assert recover_expired_lease(
        store, "default", BUSINESS_DATE, 77, owner_id="worker-b",
        now=NOW).outcome == "absent"


def test_fencing_zombie_writer_is_rejected_across_all_fenced_ops():
    """钉 6.2 单元面：A 崩溃→B 接管→A 复活后任何 fenced 写入必败。

    （崩溃点矩阵/终态输出恰一份的编排面钉在 P1a-T3 claim 循环族。）
    """
    store = MemoryStore()
    enqueue_work_item(store, make_item())
    claim_a = claim_work_item(store, "default", BUSINESS_DATE, 1,
                              owner_id="worker-a", now=NOW)
    # 时钟越 T：B recover_expired_leases 接管（generation=k+1）
    recovered = recover_expired_lease(
        store, "default", BUSINESS_DATE, 1, owner_id="worker-b",
        now=NOW + timedelta(seconds=61))
    assert recovered.lease_generation == claim_a.lease_generation + 1
    # A 复活：三类 fenced 写入全部因 generation 不匹配失败
    for fenced in (
        lambda: complete_work_item(
            store, "default", BUSINESS_DATE, 1, result=make_result(),
            owner_id="worker-a", lease_generation=claim_a.lease_generation,
            now=NOW + timedelta(seconds=62)),
        lambda: defer_work_item(
            store, "default", BUSINESS_DATE, 1, failure=transient_failure(),
            delay_seconds=1.0, definite=True, owner_id="worker-a",
            lease_generation=claim_a.lease_generation,
            now=NOW + timedelta(seconds=62)),
        lambda: terminalize_work_item(
            store, "default", BUSINESS_DATE, 1, task_state="tombstoned",
            terminal_reason="http_400", owner_id="worker-a",
            lease_generation=claim_a.lease_generation,
            now=NOW + timedelta(seconds=62)),
    ):
        with pytest.raises(WorkLeaseLost) as captured:
            fenced()
        assert captured.value.expected_generation == claim_a.lease_generation
        assert captured.value.observed_generation == recovered.lease_generation
    # B 收口：终态恰一份
    assert complete_work_item(
        store, "default", BUSINESS_DATE, 1, result=make_result(),
        owner_id="worker-b", lease_generation=recovered.lease_generation,
        now=NOW + timedelta(seconds=62)) == "completed"
    item = load_work_item(store, "default", BUSINESS_DATE, 1)
    assert item.task_state == "ready"
    assert item.lease_owner is None


def test_lease_generation_never_regresses_across_cycles():
    store = MemoryStore()
    enqueue_work_item(store, make_item())
    seen = []
    claim = claim_work_item(store, "default", BUSINESS_DATE, 1,
                            owner_id="worker-a", now=NOW)
    seen.append(claim.lease_generation)
    for index, (owner, when) in enumerate((
            ("worker-b", NOW + timedelta(seconds=61)),
            ("worker-c", NOW + timedelta(seconds=122)),
            ("worker-d", NOW + timedelta(seconds=183)))):
        result = recover_expired_lease(
            store, "default", BUSINESS_DATE, 1, owner_id=owner, now=when)
        seen.append(result.lease_generation)
    assert seen == [1, 2, 3, 4]


# ---------- 读侧校验 ----------


def test_load_work_item_rejects_schema_drift():
    store = MemoryStore()
    item = make_item()
    index, key = work_index(item.business_date), work_item_id(
        item.scope_id, item.business_date, item.arrival_seq)
    corrupted = item.model_dump(mode="json")
    corrupted["rogue_field"] = "poison"
    store.create(index, key, corrupted)
    with pytest.raises(Exception, match="validation failed"):
        load_work_item(store, "default", BUSINESS_DATE, 1)


# ---------- 检索面（search 端口） ----------


def test_scan_expired_leases_enumerates_only_expired():
    client = FakeESClient()
    store = _batch_store(client)
    enqueue_work_item(store, make_item(seq=1))
    enqueue_work_item(store, make_item(seq=2))
    enqueue_work_item(store, make_item(seq=3))
    # seq1：租约 60s（在 now+61 已过期）；seq2：now+30 起租 60s（未过期）；
    # seq3：从未领取（pending 不在扫描面）。
    claim_work_item(store, "default", BUSINESS_DATE, 1,
                    owner_id="worker-a", now=NOW)
    claim_work_item(store, "default", BUSINESS_DATE, 2,
                    owner_id="worker-b", now=NOW + timedelta(seconds=30))
    found = scan_expired_leases(_search_port(client), "default",
                                BUSINESS_DATE, now=NOW + timedelta(seconds=61))
    assert found == [1]


def test_scan_expired_leases_absent_index_is_empty():
    class _NotFound(Exception):
        status_code = 404

    def search(index, body):
        raise _NotFound("index missing")

    assert scan_expired_leases(search, "default", BUSINESS_DATE, now=NOW) == []


def test_scan_expired_leases_raises_other_errors():
    def search(index, body):
        raise RuntimeError("cluster is unreachable")

    with pytest.raises(RuntimeError):
        scan_expired_leases(search, "default", BUSINESS_DATE, now=NOW)


# ---------- 真存储面集成（ElasticsearchBatchStore+FakeESClient） ----------


def _batch_store(client: FakeESClient):
    from news_flash_dedup.batch_es_store import ElasticsearchBatchStore
    return ElasticsearchBatchStore(client, index_prefix="p01-batch-wq1-")


def _search_port(client: FakeESClient):
    return lambda index, body: client.search(
        index="p01-batch-wq1-" + index, body=body)


def test_full_primitive_cycle_on_real_store_class():
    """enqueue→claim→defer→claim→complete 全链经真 store 适配层贯通。"""
    client = FakeESClient()
    store = _batch_store(client)
    enqueue_work_item(store, make_item(seq=5))
    claim = claim_work_item(store, "default", BUSINESS_DATE, 5,
                            owner_id="worker-a", now=NOW)
    assert claim.outcome == "claimed"
    defer_work_item(store, "default", BUSINESS_DATE, 5,
                    failure=transient_failure(), delay_seconds=15.0,
                    definite=True, owner_id="worker-a",
                    lease_generation=claim.lease_generation, now=NOW)
    reclaim = claim_work_item(store, "default", BUSINESS_DATE, 5,
                              owner_id="worker-a",
                              now=NOW + timedelta(seconds=15))
    assert reclaim.lease_generation == 2
    assert complete_work_item(
        store, "default", BUSINESS_DATE, 5, result=make_result(seq=5),
        owner_id="worker-a", lease_generation=reclaim.lease_generation,
        now=NOW + timedelta(seconds=16)) == "completed"
    item = load_work_item(store, "default", BUSINESS_DATE, 5)
    assert item.task_state == "ready"
    assert item.retry_count == 1


def test_work_documents_feed_bulk_create_classified():
    """work_documents↔bulk_create_classified 对接钉（T4 存储面零改动复用）。"""
    client = FakeESClient()
    store = _batch_store(client)
    items = [make_item(seq=1), make_item(seq=2)]
    outcomes = store.bulk_create_classified(work_documents(items))
    assert len(outcomes) == 2
    assert all(outcome.succeeded for outcome in outcomes)
    for item in items:
        document = store.get(work_index(item.business_date),
                             work_item_id(item.scope_id,
                                          item.business_date,
                                          item.arrival_seq))
        assert document["source"] == item.model_dump(mode="json")
    # 重放（确定性 ID）：bulk create 撞 409 → 逐条 outcome 分类（T4 面）
    replay = store.bulk_create_classified(work_documents(items))
    assert [outcome.status for outcome in replay] == [409, 409]


def test_work_queue_reexports_surface():
    assert work_queue.WORK_LEASE_DEFAULT_SECONDS == 60.0
    assert work_queue.work_index(BUSINESS_DATE) == work_index(BUSINESS_DATE)
