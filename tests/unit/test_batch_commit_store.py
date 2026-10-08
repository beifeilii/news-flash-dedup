"""B+ 第二步·批量落锤四段式（2026-10-08 主窗口，persist/es_store.py）：

段 1 bulk_create_shells / 段 3 bulk_cas_flip / 段 4 前置 refresh_indices 的
假件层钉（FakeESClient 内存模拟，禁集群；真机层钉随 UAT 复测）：

- 闸同款：段 1 壳闸（not_ready/受理壳词表/显式 audit_complete 键）、
  段 3 终态闸（pending|held ⇒ succeeded ∧ audit_complete=True）逐件
  fail-closed，违例整批不发（bulk 零调用）；
- 冲突语义：段 1 version_conflict → "conflict"（A5 孤儿壳协议信号），
  段 3 版本竞争 → "conflict" 且已存文档不动（INV-2 对拍）；
- 重放幂等形：成功段重放=全 conflict，可安全整段重来；
- refresh_indices 一次刷三索引（批量写 refresh=False 的可见性收口）。
"""

from __future__ import annotations

from datetime import date

import pytest

from news_flash_dedup.es_client import APPROVED_UAT_HOST
from news_flash_dedup.persist.es_store import RealESP18Config, RealESP18Store

from b4_fake_es import FakeESClient

RUN = "bc1008"
DAY = date(2026, 10, 8)


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("P18_CONFIRM_UAT", "1")
    monkeypatch.setenv("DEPLOY_ENV", "test")
    monkeypatch.setenv("TEST_ES_HOST", APPROVED_UAT_HOST)
    for key in ("PROD_ES_HOST", "PROD_ES_PORT", "PROD_ES_USER", "PROD_ES_PASS",
                "PROD_ES_SCHEME", "ALLOW_PROD_WRITE"):
        monkeypatch.delenv(key, raising=False)
    client = FakeESClient()
    config = RealESP18Config(run_uuid=RUN, business_date=DAY)
    return client, RealESP18Store(client, config), config


def _shell(rid: str) -> dict:
    return {"record_id": rid, "delivery_state": "not_ready",
            "task_state": "accepted", "audit_complete": False,
            "raw_hash": f"h-{rid}"}


def _flip(rid: str) -> dict:
    return {"record_id": rid, "delivery_state": "pending",
            "task_state": "succeeded", "audit_complete": True,
            "raw_hash": f"h-{rid}"}


# ---------- 段 1：bulk_create_shells ----------

def test_shells_happy(env):
    client, store, config = env
    out = store.bulk_create_shells(
        [(f"r{i}", _shell(f"r{i}")) for i in range(3)])
    assert set(out) == {"r0", "r1", "r2"}
    assert all(v == (0, 1) for v in out.values())
    for i in range(3):
        assert client.source(config.items_index, f"r{i}")["delivery_state"] \
            == "not_ready"
    # 一次 bulk 收口（refresh=False——可见性归段 4，假件记录调用形）
    assert [c for c in client.calls if c[0] == "bulk"] == [("bulk", 6)]


@pytest.mark.parametrize("field,value", [
    ("delivery_state", "pending"),
    ("task_state", "succeeded"),
])
def test_shells_gate_rejects_and_writes_nothing(env, field, value):
    client, store, config = env
    bad = _shell("r0")
    bad[field] = value
    with pytest.raises(ValueError):
        store.bulk_create_shells([("r0", bad), ("r1", _shell("r1"))])
    assert not [c for c in client.calls if c[0] == "bulk"]
    assert client.source(config.items_index, "r1") is None


def test_shells_gate_requires_audit_complete_key(env):
    client, store, config = env
    bad = _shell("r0")
    del bad["audit_complete"]
    with pytest.raises(ValueError):
        store.bulk_create_shells([("r0", bad)])
    assert not [c for c in client.calls if c[0] == "bulk"]


def test_shells_conflict_marks_only_existing(env):
    client, store, config = env
    client.put(config.items_index, "r0", _shell("r0"))
    out = store.bulk_create_shells(
        [(f"r{i}", _shell(f"r{i}")) for i in range(3)])
    assert out["r0"] == "conflict"          # A5 孤儿壳协议信号
    assert out["r1"] == (0, 1) and out["r2"] == (0, 1)


# ---------- 段 3：bulk_cas_flip ----------

def test_flip_happy_after_shells(env):
    client, store, config = env
    shells = store.bulk_create_shells(
        [(f"r{i}", _shell(f"r{i}")) for i in range(2)])
    out = store.bulk_cas_flip([
        ("r0", _flip("r0"), *shells["r0"]),
        ("r1", _flip("r1"), *shells["r1"]),
    ])
    assert out == {"r0": 1, "r1": 1}
    for i in range(2):
        doc = client.source(config.items_index, f"r{i}")
        assert doc["delivery_state"] == "pending"
        assert doc["task_state"] == "succeeded"


@pytest.mark.parametrize("field,value", [
    ("delivery_state", "not_ready"),
    ("task_state", "accepted"),
    ("audit_complete", False),
])
def test_flip_gate_rejects_and_writes_nothing(env, field, value):
    client, store, config = env
    shells = store.bulk_create_shells([("r0", _shell("r0"))])
    bad = _flip("r0")
    bad[field] = value
    with pytest.raises(ValueError):
        store.bulk_cas_flip([("r0", bad, *shells["r0"])])
    assert client.source(config.items_index, "r0")["delivery_state"] \
        == "not_ready"


def test_flip_conflict_leaves_doc_untouched(env):
    client, store, config = env
    shells = store.bulk_create_shells([("r0", _shell("r0"))])
    out = store.bulk_cas_flip([("r0", _flip("r0"), 99, 1)])  # 错版本
    assert out["r0"] == "conflict"
    doc = client.source(config.items_index, "r0")
    assert doc["delivery_state"] == "not_ready"   # INV-2：竞争不翻态


def test_flip_missing_doc_raises_segment_failure(env):
    client, store, config = env
    with pytest.raises(RuntimeError):
        store.bulk_cas_flip([("ghost", _flip("ghost"), 0, 1)])


# ---------- 重放幂等形 + 段 4 刷新 ----------

def test_replay_shape_all_conflicts(env):
    """半批崩溃后整段重放：壳段重放=conflict（走 A5 补翻），
    翻壳段用旧版本重放=conflict——两段均可安全重来。"""
    client, store, config = env
    shells = store.bulk_create_shells([("r0", _shell("r0"))])
    assert store.bulk_create_shells([("r0", _shell("r0"))])["r0"] == "conflict"
    store.bulk_cas_flip([("r0", _flip("r0"), *shells["r0"])])
    assert store.bulk_cas_flip(
        [("r0", _flip("r0"), *shells["r0"])])["r0"] == "conflict"


def test_refresh_indices_covers_three(env):
    client, store, config = env
    store.refresh_indices()
    assert client.indices.refreshed == [
        f"{config.items_index},{config.audits_index},{config.control_index}"]
