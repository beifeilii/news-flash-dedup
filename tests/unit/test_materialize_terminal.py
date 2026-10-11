"""P0-T3 单测：单条墓碑（schema/确定性 ID/持久化读回/迟到补正）。

钉面（施工简报 P0-T3 + 设计稿 §5.3）：
- 字段合同：简报十字段全在场+身份锚+证据位；extra=forbid strict；
- error_class 结构性只容 item_permanent（网络/未知/系统级进不了墓碑）；
- 墓碑 ID=sha256(scope_id, business_date, arrival_seq) 确定性；
- 持久化+读回确认（confirmed）才是跳洞凭据；读回未知=可重试暂时态；
- 重放对拍幂等（身份字段参拍；账务字段不参拍——首证为准不更新）；
- 身份分歧=IdentityConflict→T1 分类 IDENTITY_CORRUPTION（停推进+报警）；
- 迟到成功补正：不删除/不复活/不二次物化——CAS 翻 tombstoned；
  prepare/worker 扫描词表不含该值（自然跳过）；他件水位零触碰。
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from news_flash_dedup.admission import IdentityConflict
from news_flash_dedup.es_admission_schema import tombstone_mapping
from news_flash_dedup.materialize_failure import (
    FailureClass, classify_exception)
from news_flash_dedup.materialize_terminal import (
    TOMBSTONE_TASK_STATE,
    MaterializationTombstoneV1,
    TombstonePersistResult,
    TombstonePersistUnknown,
    load_tombstone,
    persist_tombstone,
    reconcile_late_materialization,
    tombstone_exists_confirmed,
    tombstone_id,
    tombstone_index,
)
from test_admission import MemoryStore


DAY = "2026-09-24"
SCOPE = "default"
NOW = datetime(2026, 9, 24, 0, 0, tzinfo=timezone.utc)
ITEM_MAIN_INDEX = "news-dedup-items-v1-2026.09.24"


def _utc(offset_seconds: float = 0.0) -> str:
    stamp = NOW + timedelta(seconds=offset_seconds)
    return stamp.astimezone(timezone.utc).isoformat(
        timespec="microseconds").replace("+00:00", "Z")


def _tombstone(arrival_seq: int = 17, **overrides) -> MaterializationTombstoneV1:
    base = dict(
        scope_id=SCOPE, business_date=DAY, arrival_seq=arrival_seq,
        record_id="a" * 64, item_id="9-17", raw_hash="b" * 64,
        failed_stage="materialize_bulk", error_class="item_permanent",
        error_code="http_412", error_summary="mapper rejected the document",
        attempt_count=2, first_failed_at=_utc(0.5), last_failed_at=_utc(9.5),
        retryable=False, pipeline_version="dedup_v1", recorded_at=_utc(10.0),
    )
    base.update(overrides)
    return MaterializationTombstoneV1(**base)


class _FlakyGet(MemoryStore):
    """读回故障注入：fail_get=True 时 get 全失败（断网形态）。"""

    fail_get = False
    fail_create_once = False

    def get(self, index, key):
        if self.fail_get:
            raise OSError("connection refused during get")
        return super().get(index, key)

    def create(self, index, key, body):
        if self.fail_create_once:
            self.fail_create_once = False
            raise OSError("connection refused during create")
        return super().create(index, key, body)


# ---------- schema 合同 ----------

def test_tombstone_schema_carries_mandated_fields():
    body = _tombstone().model_dump()
    # 简报十字段逐一在场。
    for field in ("arrival_seq", "item_id", "failed_stage", "error_class",
                  "error_code", "first_failed_at", "last_failed_at",
                  "attempt_count", "raw_hash", "retryable"):
        assert field in body
    # 身份锚+证据位。
    for field in ("scope_id", "business_date", "record_id", "error_summary",
                  "recorded_at", "pipeline_version", "late_success_at"):
        assert field in body


def test_tombstone_schema_extra_fields_rejected():
    with pytest.raises(Exception):
        _tombstone(extra_field="x")


@pytest.mark.parametrize("overrides", [
    {"error_class": "transient_infra"},     # 铁律：网络瞬态进不了墓碑
    {"error_class": "unknown_write"},       # 铁律：未知写入进不了墓碑
    {"error_class": "systemic_outage"},     # 铁律：系统级故障进不了墓碑
    {"arrival_seq": 0},
    {"record_id": "not-hex"},
    {"raw_hash": "zz"},
    {"attempt_count": 0},
    {"first_failed_at": "2026-09-24T00:00:00Z"},       # 非 canonical 微秒形
])
def test_tombstone_schema_rejects_invalid_shapes(overrides):
    with pytest.raises(Exception):
        _tombstone(**overrides)


def test_tombstone_id_is_deterministic():
    first = tombstone_id(SCOPE, DAY, 17)
    assert first == tombstone_id(SCOPE, DAY, 17)
    assert first != tombstone_id(SCOPE, DAY, 18)
    assert first != tombstone_id("other", DAY, 17)
    assert first != tombstone_id(SCOPE, "2026-09-25", 17)
    assert len(first) == 64


def test_tombstone_index_name_shape():
    assert tombstone_index(DAY) == "news-dedup-tombstones-v1-2026.09.24"


def test_tombstone_mapping_has_no_text_body():
    mapping = tombstone_mapping()
    properties = mapping["mappings"]["properties"]
    assert "text" not in properties                     # 正文不进墓碑
    assert properties["raw_hash"]["type"] == "keyword"
    assert properties["arrival_seq"]["type"] == "long"
    assert properties["retryable"]["type"] == "boolean"
    assert mapping["mappings"]["dynamic"] == "strict"


# ---------- 持久化+读回确认 ----------

def test_persist_tombstone_creates_and_confirms_via_readback():
    store = MemoryStore()
    result = persist_tombstone(store, _tombstone())
    assert isinstance(result, TombstonePersistResult)
    assert result.confirmed is True
    assert result.created is True
    assert result.tombstone_id == tombstone_id(SCOPE, DAY, 17)
    assert result.index == tombstone_index(DAY)
    stored = store.get(tombstone_index(DAY), result.tombstone_id)
    assert stored is not None
    assert stored["source"]["item_id"] == "9-17"


def test_persist_tombstone_replay_is_idempotent_first_evidence_wins():
    store = MemoryStore()
    first = persist_tombstone(store, _tombstone(attempt_count=2))
    # 崩溃恢复重放：账务字段漂移（attempt_count/时刻/摘要），身份不变。
    replay = persist_tombstone(store, _tombstone(
        attempt_count=3, last_failed_at=_utc(20.0),
        error_code="http_400", error_summary="replay variant",
        recorded_at=_utc(21.0), retryable=True))
    assert replay.confirmed is True
    assert replay.created is False
    # 首证为准：存储中的墓碑未被重放覆盖。
    stored = store.get(tombstone_index(DAY), first.tombstone_id)
    assert stored["source"]["attempt_count"] == 2
    assert stored["source"]["error_code"] == "http_412"


def test_persist_tombstone_readback_unknown_is_retryable_not_confirmed():
    store = _FlakyGet()
    store.fail_get = True                               # create 落、读回断网
    with pytest.raises(TombstonePersistUnknown):
        persist_tombstone(store, _tombstone())
    # create 已落（重试将走冲突对拍幂等路径）。
    key = tombstone_id(SCOPE, DAY, 17)
    store.fail_get = False
    assert store.get(tombstone_index(DAY), key) is not None
    result = persist_tombstone(store, _tombstone())
    assert result.confirmed is True and result.created is False


def test_persist_tombstone_create_unknown_is_retryable():
    store = _FlakyGet()
    store.fail_create_once = True
    with pytest.raises(TombstonePersistUnknown):
        persist_tombstone(store, _tombstone())
    store.fail_create_once = False
    result = persist_tombstone(store, _tombstone())
    assert result.confirmed is True and result.created is True


def test_persist_tombstone_identity_divergence_is_identity_corruption():
    store = MemoryStore()
    persist_tombstone(store, _tombstone())
    # 同一确定性 ID 但身份字段分歧（raw_hash 不同→撞键异体）。
    with pytest.raises(IdentityConflict):
        persist_tombstone(store, _tombstone(raw_hash="c" * 64))
    with pytest.raises(IdentityConflict):
        persist_tombstone(store, _tombstone(item_id="9-99"))
    # 分类口径：IdentityConflict → IDENTITY_CORRUPTION（停推进+报警，
    # 不重试不墓碑升级）。
    failure = classify_exception(
        IdentityConflict("tombstone replay diverges"))
    assert failure.failure_class is FailureClass.IDENTITY_CORRUPTION


def test_load_tombstone_roundtrip_and_absence():
    store = MemoryStore()
    assert load_tombstone(store, SCOPE, DAY, 17) is None
    persist_tombstone(store, _tombstone())
    loaded = load_tombstone(store, SCOPE, DAY, 17)
    assert loaded == _tombstone()
    # 读未知 fail-closed：存在性速查=False（不产跳洞凭据）。
    flaky = _FlakyGet()
    persist_tombstone(flaky, _tombstone())
    flaky.fail_get = True
    assert tombstone_exists_confirmed(flaky, SCOPE, DAY, 17) is False


# ---------- 迟到成功补正（场景 C 确定规则） ----------

def _put_main_record(store: MemoryStore, *, task_state: str = "accepted",
                     **overrides) -> None:
    tomb = _tombstone(**overrides)
    body = {
        "scope_id": tomb.scope_id, "business_date": tomb.business_date,
        "arrival_seq": tomb.arrival_seq, "record_id": tomb.record_id,
        "item_id": tomb.item_id, "raw_hash": tomb.raw_hash,
        "text": "迟到落库正文。",
        "task_state": task_state, "delivery_state": "not_ready",
        "result": None,
    }
    store.create(ITEM_MAIN_INDEX, tomb.record_id, body)


def test_reconcile_absent_when_nothing_landed():
    store = MemoryStore()
    tomb = _tombstone()
    assert reconcile_late_materialization(
        store, tomb, main_index=ITEM_MAIN_INDEX) == "absent"


def test_reconcile_corrects_late_landed_record_to_tombstoned():
    store = MemoryStore()
    _put_main_record(store)                             # 迟到主记录已落
    tomb = _tombstone()
    disposition = reconcile_late_materialization(
        store, tomb, main_index=ITEM_MAIN_INDEX)
    assert disposition == "corrected"
    source = store.get(ITEM_MAIN_INDEX, tomb.record_id)["source"]
    assert source["task_state"] == TOMBSTONE_TASK_STATE
    # 记录本体不删除、身份字段原样（不复活、不二次物化）。
    assert source["raw_hash"] == tomb.raw_hash
    assert source["result"] is None
    # 幂等：再次补正=already_corrected。
    assert reconcile_late_materialization(
        store, tomb, main_index=ITEM_MAIN_INDEX) == "already_corrected"


def test_reconcile_divergent_late_record_is_identity_corruption():
    store = MemoryStore()
    _put_main_record(store, raw_hash="d" * 64)         # 身份分歧的迟到件
    with pytest.raises(IdentityConflict):
        reconcile_late_materialization(
            store, _tombstone(), main_index=ITEM_MAIN_INDEX)


def test_reconcile_never_touches_other_records_or_watermarks():
    # 他件主记录在场；补正只翻墓碑件本体，他件记录与头文档零触碰。
    store = MemoryStore()
    _put_main_record(store, arrival_seq=16, record_id="e" * 64,
                     item_id="9-16", raw_hash="f" * 64)   # 他件（序 16）
    other_before = deepcopy(
        store.get(ITEM_MAIN_INDEX, "e" * 64)["source"])
    _put_main_record(store)                             # 墓碑件（序 17）迟到落
    tomb = _tombstone()
    assert reconcile_late_materialization(
        store, tomb, main_index=ITEM_MAIN_INDEX) == "corrected"
    assert store.get(ITEM_MAIN_INDEX, "e" * 64)["source"] == other_before
    # 补正面无头文档写（不覆盖他件水位——结构上不触 CONTROL_INDEX）。
    assert store.get("news-dedup-control-v1", "batch_admission_head") is None


def test_tombstone_state_value_is_ignored_by_prepare_and_worker_scanners():
    # 扫描词表纪律：tombstoned 不在 prepare/worker 扫描词表内（自然跳过）。
    from news_flash_dedup.recall.prepare import _TASK_STATES_PREPARE
    from news_flash_dedup.recall.worker import _TASK_STATES_REGISTRATION
    assert TOMBSTONE_TASK_STATE not in _TASK_STATES_PREPARE
    assert TOMBSTONE_TASK_STATE not in _TASK_STATES_REGISTRATION
