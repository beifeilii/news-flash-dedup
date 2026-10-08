# -*- coding: utf-8 -*-
"""W2Fγ 红测（persist 族）：条目 15（A3 task_state 词表闸）/16（A4 write_audit
create-only）/22（ζ2 重 ID 自查 + 校验子集合同声明）。

红能力（修复前原貌）：
- 条目15 persist/es_store.create_main_record docstring 声称"受理壳 task_state
  词汇对拍 admission.py：accepted/running/failed 均可，禁 pending/succeeded
  直建"，码无此闸；
- 条目16 persist/fake_store.FakeP18Store.write_audit 与
  commit/fake_store.FakeCommitStore.write_audit 同 comparison_id 覆盖静默
  （真层 create-only：冲突即错）；
- 条目22① persist FakeP18Store.write_main_record 重 ID 静默覆盖（commit 同款
  自查已封 Z2，persist 侧缺）；
- 条目22② 两 fake docstring 缺"校验子集合同、守卫在上层"声明（源码实述钉）。
"""
from __future__ import annotations

import pytest

from news_flash_dedup.persist.fake_store import (
    FakeAuditDoc,
    FakeCASConflictError,
    FakeMainRecordV1,
    FakeP18Store,
)


def _audit_doc(comparison_id="cmp-1", detail="v1"):
    return FakeAuditDoc(
        comparison_id=comparison_id, history_record_id="h",
        current_record_id="c", history_raw_hash="h1", current_raw_hash="c1",
        basis="FACT_EQUIVALENT", field_path="f", detail=detail,
        history_evidence={}, current_evidence={}, pipeline_version="dedup_v1")


def _p18_record(record_id="r-1", text="甲公司完成回购。"):
    return FakeMainRecordV1(
        record_id=record_id, item_id="item-A", text=text, scope_id="default",
        business_date="2026-09-26", arrival_seq=1, raw_hash="h",
        pipeline_version="dedup_v1", fact_artifact_hash="",
        vector_state="pending", result_item_id="item-A",
        result_decision="不重复", result_duplicate_ids=(),
        result_reason="r", task_state="succeeded", completed_at="2026-09-26T00:00:00+00:00",
        result_version=1, event_id="e", audit_ids=(), audit_complete=True,
        reason_code="NO_DUPLICATE_FOUND", callback_body="{}", callback_body_hash="h",
        delivery_state="pending", delivery_deadline_at="2026-09-27T00:00:00+00:00",
        next_delivery_at="2026-09-26T00:00:00+00:00", callback_attempts=0,
        round_attempts=0, round=1)


# ---------- 条目16（A4 并案 35①）：两 fake write_audit create-only ----------

def test_p18_fake_write_audit_duplicate_id_raises_cas_conflict():
    """红能力：同 comparison_id 再写——旧静默覆盖（审计证据可篡改）；
    修复后 FakeCASConflictError（对齐真层 bulk create 冲突即错）。"""
    store = FakeP18Store()
    store.write_audit(_audit_doc("cmp-1", detail="v1"))
    with pytest.raises(FakeCASConflictError, match="cmp-1"):
        store.write_audit(_audit_doc("cmp-1", detail="v2-tampered"))
    assert store.audit_docs["cmp-1"].detail == "v1"          # 已存不动


def test_p18_fake_write_audit_distinct_ids_unaffected():
    """绿守卫：异 comparison_id 正常落库（create 语义不误伤）。"""
    store = FakeP18Store()
    store.write_audit(_audit_doc("cmp-1"))
    store.write_audit(_audit_doc("cmp-2"))
    assert set(store.audit_docs) == {"cmp-1", "cmp-2"}


def test_commit_fake_write_audit_duplicate_id_raises_cas_conflict():
    """红能力（并案）：FakeCommitStore.write_audit 同 ID 覆盖静默。"""
    from news_flash_dedup.commit.fake_store import FakeCommitStore
    store = FakeCommitStore()
    store.write_audit("cmp-1", {"v": 1})
    with pytest.raises(FakeCASConflictError, match="cmp-1"):
        store.write_audit("cmp-1", {"v": 2})
    assert store.audit_docs["cmp-1"] == {"v": 1}              # 已存不动


# ---------- 条目22①（ζ2）：persist fake write_main_record 重 ID 自查 ----------

def test_p18_fake_write_main_record_duplicate_id_create_conflict():
    """红能力：write_main_record 重 ID 静默覆盖（与真层方案 B 协议
    cas_main_record(0,0)→op_type=create 两态相反；commit 同款自查 Z2 已封）；
    修复后 FakeCASConflictError 且已存记录不动。"""
    store = FakeP18Store()
    store.write_main_record(_p18_record())
    with pytest.raises(FakeCASConflictError, match="r-1"):
        store.write_main_record(_p18_record(text="篡改文本。"))
    assert store.main_records["r-1"].text == "甲公司完成回购。"


def test_p18_fake_cas_update_path_still_works_via_upper_guard():
    """绿守卫：cas_main_record 版本对拍更新路径（非 (0,0)）不受重 ID 自查
    误伤——守卫在上层，更新落点与创建落点分途。"""
    from news_flash_dedup.persist import main_record_persist
    store = FakeP18Store()
    seq0 = main_record_persist.cas_main_record(
        store, _p18_record(), expected_seq_no=0, expected_primary_term=0)
    cur_seq, cur_term = store.main_version("r-1")
    assert (cur_seq, cur_term) == (seq0, 1)
    seq1 = main_record_persist.cas_main_record(
        store, _p18_record(text="更新文本。"),
        expected_seq_no=cur_seq, expected_primary_term=cur_term)
    assert seq1 == cur_seq + 1
    assert store.main_records["r-1"].text == "更新文本。"


def test_two_fakes_docstring_declares_validation_subset_contract():
    """红能力（源码实述钉）：两 fake 仓储 docstring 缺"校验子集合同、
    守卫在上层"声明即红。"""
    from news_flash_dedup.commit.fake_store import FakeCommitStore
    for cls in (FakeP18Store, FakeCommitStore):
        doc = cls.__doc__ or ""
        assert "校验子集" in doc, f"{cls.__name__} docstring 缺校验子集声明"
        assert "守卫在上层" in doc, f"{cls.__name__} docstring 缺守卫在上层声明"


# ---------- 条目15（A3）：create_main_record task_state 词表闸 ----------

class _StubIndicesApi:
    def exists(self, *, index):
        return True

    def create(self, *, index):
        return {}


class _StubESClient:
    def __init__(self):
        self.indices = _StubIndicesApi()
        self._docs: dict[str, dict] = {}
        self._seq: dict[str, int] = {}

    def index(self, *, index, id, document, **kwargs):
        from elasticsearch import ConflictError
        if kwargs.get("op_type") == "create" and id in self._docs:
            raise ConflictError("exists", None, None)
        self._seq[id] = self._seq.get(id, -1) + 1
        self._docs[id] = dict(document)
        return {"_seq_no": self._seq[id], "_primary_term": 1}


def _p18_store(monkeypatch):
    import datetime as _dt
    from news_flash_dedup.persist import es_store as p18_es
    from news_flash_dedup.persist.es_store import RealESP18Config, RealESP18Store
    monkeypatch.setattr(p18_es, "assert_p18_uat_open", lambda: None)
    config = RealESP18Config(run_uuid="winw2fc",
                             business_date=_dt.date(2026, 9, 26))
    return RealESP18Store(_StubESClient(), config)


@pytest.mark.parametrize("bad_state", ["succeeded", "pending", "", None])
def test_create_main_record_rejects_non_shell_task_state(monkeypatch, bad_state):
    """红能力：create_main_record 对 task_state∉{accepted,running,failed}
    无闸（docstring 声称"受理壳词汇对拍"码无）；缺失/禁词一律 fail-closed。"""
    store = _p18_store(monkeypatch)
    body = {"delivery_state": "not_ready", "audit_complete": False}
    if bad_state is not None:
        body["task_state"] = bad_state
    with pytest.raises(ValueError, match="(?i)task_state"):
        store.create_main_record("r-1", body)


@pytest.mark.parametrize("ok_state", ["accepted", "running", "failed"])
def test_create_main_record_accepts_shell_task_vocabulary(monkeypatch, ok_state):
    """绿守卫：受理壳三词放行（docstring 词表逐字）。"""
    store = _p18_store(monkeypatch)
    seq, term = store.create_main_record("r-1", {
        "delivery_state": "not_ready", "task_state": ok_state,
        "audit_complete": False})
    assert (seq, term) == (0, 1)
