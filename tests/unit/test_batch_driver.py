"""B+ 批量落锤·编排层驱动器（commit/batch_driver.py）假件层钉。

FakeESClient 内存模拟（禁集群），fixture/假件用法照搬
tests/unit/test_batch_commit_store.py。覆盖合同要求八形态：

① 四段全绿（K=3：终态/审计/水位/refresh 全对，含一件 held 扣留）；
② 段 1 冲突走 A5 孤儿壳补翻成功；
③ 段 1 壳身份不符（raw_hash 异）→ CommitOneError，审计未落/零刷新；
④ 段 3 冲突内容一致（仅时间派生三键差）= 幂等跳过零重写；
⑤ 段 3 冲突分歧（raw_hash 异）→ CommitOneError，零刷新零水位；
⑥ 半批崩溃两形状整批重放 = 幂等成功（不重写、不重复推进水位）：
   a. 全量提交完成后重放；b. 壳全建+一件已翻+审计已落+水位未推；
⑦ 批内跳号 = 水位只推进到连续前缀，断裂件照样落库留下批；
⑧ 空批 = 零调用。
"""

from __future__ import annotations

from datetime import date

import pytest

from news_flash_dedup.commit.batch_driver import (
    BatchCommitItem,
    commit_batch,
)
from news_flash_dedup.commit.coordinator import CommitOneError
from news_flash_dedup.es_client import APPROVED_UAT_HOST
from news_flash_dedup.persist.es_store import RealESP18Config, RealESP18Store

from b4_fake_es import FakeESClient

RUN = "bd1008"
DAY = date(2026, 10, 8)
SCOPE = "scope-bd"
DATE_STR = "2026-10-08"


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


def _body(rid: str, seq: int, *, decision: str = "不重复") -> dict:
    """终态主记录体（P18 §3.1 形状镜像 _record_to_body；闸键全显式）。"""
    return {
        "record_id": rid,
        "item_id": f"item-{rid}",
        "text": f"text-{rid}",
        "scope_id": SCOPE,
        "business_date": DATE_STR,
        "arrival_seq": seq,
        "raw_hash": f"h-{rid}",
        "pipeline_version": "dedup_v1",
        "fact_artifact_hash": "",
        "vector_state": "pending",
        "result": {"item_id": f"item-{rid}", "decision": decision,
                    "duplicate_ids": [], "reason": f"reason-{rid}"},
        "result_item_id": f"item-{rid}",
        "result_decision": decision,
        "result_duplicate_ids": [],
        "result_reason": f"reason-{rid}",
        "task_state": "succeeded",
        "completed_at": "2026-10-08T01:00:00+00:00",
        "result_version": 1,
        "event_id": f"e-{rid}",
        "audit_ids": [f"cmp-{rid}"],
        "audit_complete": True,
        "reason_code": "NO_DUPLICATE_FOUND",
        "callback_body": "{}",
        "callback_body_hash": f"ph-{rid}",
        # 10-07 令（甲-i）：非"不重复"扣留 held，放行件 pending
        "delivery_state": "pending" if decision == "不重复" else "held",
        "delivery_deadline_at": "2026-10-09T01:00:00+00:00",
        "next_delivery_at": "2026-10-08T01:00:00+00:00",
        "callback_attempts": 0,
        "round_attempts": 0,
        "round": 1,
    }


def _audit(rid: str) -> dict:
    return {"comparison_id": f"cmp-{rid}", "record_id": rid,
            "decision": "不重复", "reason": f"reason-{rid}"}


def _item(rid: str, seq: int, **kw) -> BatchCommitItem:
    return BatchCommitItem(
        record_id=rid, arrival_seq=seq,
        audit_docs=(_audit(rid),), body=_body(rid, seq, **kw))


def _shell_of(item: BatchCommitItem, **override) -> dict:
    """段 1 派生壳形状（not_ready/accepted），可覆盖键造异身份。"""
    return dict(item.body, delivery_state="not_ready",
                task_state="accepted", **override)


class _ConcurrentFlipStore:
    """段 1 成功后、段 3 之前并发翻终态的注入包装（其余方法透传内层）。"""

    def __init__(self, inner, record_id: str, body: dict) -> None:
        self._inner = inner
        self._record_id = record_id
        self._body = body

    def bulk_create_shells(self, shells):
        out = self._inner.bulk_create_shells(shells)
        version = out.get(self._record_id)
        if version not in (None, "conflict"):
            self._inner.bulk_cas_flip(
                [(self._record_id, self._body, *version)])
        return out

    def __getattr__(self, name):
        return getattr(self._inner, name)


# ---------- ① 四段全绿路径 ----------

def test_happy_path_three_items(env):
    client, store, config = env
    items = [_item("r0", 1), _item("r1", 2),
             _item("r2", 3, decision="重复")]  # 扣留件走 held
    outcome = commit_batch(items, store, scope_id=SCOPE,
                           business_date=DATE_STR)
    assert outcome.flipped_ids == ("r0", "r1", "r2")
    assert outcome.idempotent_ids == ()
    assert outcome.audit_doc_count == 3
    assert outcome.watermark_prev == 0
    assert outcome.watermark_advanced_to == 3
    assert outcome.deferred_ids == ()
    # 终态：CAS 从壳翻入（INV-2 形状），放行 pending / 扣留 held
    for rid in ("r0", "r1"):
        doc = client.source(config.items_index, rid)
        assert doc["delivery_state"] == "pending"
        assert doc["task_state"] == "succeeded"
        assert doc["audit_complete"] is True
        assert doc["audit_ids"] == [f"cmp-{rid}"]
    held = client.source(config.items_index, "r2")
    assert held["delivery_state"] == "held"
    assert held["task_state"] == "succeeded"
    # 审计先存于终态：三件审计文档落 audits 索引
    for rid in ("r0", "r1", "r2"):
        assert client.source(config.audits_index, f"cmp-{rid}") is not None
    # 水位推进到批内最大连续 arrival_seq
    assert client.source(config.control_index, f"{SCOPE}|{DATE_STR}")[
        "decision_watermark_seq"] == 3
    # 段 4 一次显式刷新三索引（批量写 refresh=False 的可见性收口）
    assert client.indices.refreshed == [
        f"{config.items_index},{config.audits_index},{config.control_index}"]
    # 段 1/2/3 各一次 bulk（每段 3 动作×2 行），段 4 无 bulk
    assert [c for c in client.calls if c[0] == "bulk"] == [
        ("bulk", 6), ("bulk", 6), ("bulk", 6)]


# ---------- ② 段 1 冲突走 A5 补翻成功 ----------

def test_segment1_conflict_orphan_shell_a5_flip(env):
    client, store, config = env
    items = [_item(f"r{i}", i + 1) for i in range(3)]
    # 预置 r1 孤儿壳（段 1/段 3 之间崩溃残留形状：not_ready 未翻）
    client.put(config.items_index, "r1", _shell_of(items[1]))
    outcome = commit_batch(items, store, scope_id=SCOPE,
                           business_date=DATE_STR)
    # A5：核 raw_hash 身份一致 → 真实版本 (0,1) 走段 3 更新路径补翻
    assert outcome.flipped_ids == ("r0", "r1", "r2")
    assert outcome.idempotent_ids == ()
    assert outcome.watermark_advanced_to == 3
    doc = client.source(config.items_index, "r1")
    assert doc["delivery_state"] == "pending"
    assert doc["task_state"] == "succeeded"
    # 孤儿壳补翻 = 一次 index（seq_no 0→1），非重建
    assert client.docs[(config.items_index, "r1")]["_seq_no"] == 1


# ---------- ③ 段 1 壳身份不符 → CommitOneError ----------

def test_segment1_shell_identity_mismatch_raises(env):
    client, store, config = env
    items = [_item(f"r{i}", i + 1) for i in range(3)]
    # r1 壳 raw_hash 与本批身份不符（损坏/撞号数据形状）
    client.put(config.items_index, "r1",
               _shell_of(items[1], raw_hash="h-ALIEN"))
    with pytest.raises(CommitOneError):
        commit_batch(items, store, scope_id=SCOPE, business_date=DATE_STR)
    # 段 1 fail-closed：异身份壳原样不动（不自动覆盖/回收）
    assert client.source(config.items_index, "r1")["raw_hash"] == "h-ALIEN"
    # 同批他件壳已建（段 1 bulk 已发）但滞留 not_ready——段 3 未启动
    assert client.source(config.items_index, "r0")["delivery_state"] \
        == "not_ready"
    assert client.source(config.items_index, "r2")["delivery_state"] \
        == "not_ready"
    # 审计未落（段 2 未启动）、零刷新、零水位
    assert client.source(config.audits_index, "cmp-r0") is None
    assert client.indices.refreshed == []
    assert client.source(config.control_index, f"{SCOPE}|{DATE_STR}") is None


# ---------- ④ 段 3 冲突内容一致 = 幂等跳过 ----------

def test_segment3_conflict_identical_is_idempotent_skip(env):
    client, store, config = env
    items = [_item(f"r{i}", i + 1) for i in range(3)]
    # 并发方在段 1 后抢先翻 r1，内容与本批一致——仅时间派生三键
    # 不同（对拍排除面：completed_at/delivery_deadline_at/next_delivery_at）
    concurrent_body = dict(
        items[1].body,
        completed_at="1999-01-01T00:00:00+00:00",
        delivery_deadline_at="1999-01-02T00:00:00+00:00",
        next_delivery_at="1999-01-01T00:00:00+00:00",
    )
    wrapped = _ConcurrentFlipStore(store, "r1", concurrent_body)
    outcome = commit_batch(items, wrapped, scope_id=SCOPE,
                           business_date=DATE_STR)
    assert outcome.flipped_ids == ("r0", "r2")
    assert outcome.idempotent_ids == ("r1",)
    assert outcome.watermark_advanced_to == 3
    # r1 只被并发方翻过一次（seq_no=1），驱动器零重写
    assert client.docs[(config.items_index, "r1")]["_seq_no"] == 1
    assert client.source(config.items_index, "r1")["delivery_state"] \
        == "pending"


# ---------- ⑤ 段 3 冲突分歧 → CommitOneError ----------

def test_segment3_conflict_divergence_raises(env):
    client, store, config = env
    items = [_item(f"r{i}", i + 1) for i in range(3)]
    # 并发方翻出的 r1 与本批身份分歧（raw_hash 异）→ 对拍拒绝
    wrapped = _ConcurrentFlipStore(
        store, "r1", dict(items[1].body, raw_hash="h-TAMPERED"))
    with pytest.raises(CommitOneError):
        commit_batch(items, wrapped, scope_id=SCOPE, business_date=DATE_STR)
    # 段 3 半批形状：r0 已随本批 bulk 翻成（单次 bulk 部分成功，
    # 留给调用侧重放幂等收口），分歧件保持并发方内容不动
    assert client.source(config.items_index, "r0")["delivery_state"] \
        == "pending"
    assert client.source(config.items_index, "r1")["raw_hash"] == "h-TAMPERED"
    # 段 4 未启动：零刷新、零水位
    assert client.indices.refreshed == []
    assert client.source(config.control_index, f"{SCOPE}|{DATE_STR}") is None


# ---------- ⑥ 半批崩溃后整批重放 = 幂等成功 ----------

def test_replay_after_full_commit_is_idempotent(env):
    """崩溃点 = 段 4 水位推进之后：整批重放全 conflict + 对拍一致，
    零重写、零重复推进。"""
    client, store, config = env
    items = [_item(f"r{i}", i + 1) for i in range(3)]
    first = commit_batch(items, store, scope_id=SCOPE,
                         business_date=DATE_STR)
    assert first.watermark_advanced_to == 3
    client.calls.clear()

    second = commit_batch(items, store, scope_id=SCOPE,
                          business_date=DATE_STR)
    assert second.flipped_ids == ()
    assert second.idempotent_ids == ("r0", "r1", "r2")
    assert second.watermark_prev == 3
    assert second.watermark_advanced_to is None      # 不重复推进水位
    assert second.deferred_ids == ()
    # 不重写：主记录保持首翻 seq_no=1；审计保持首建 seq_no=0；
    # 水位文档保持首推进 seq_no=0（create 落点后未再 index）
    for i in range(3):
        assert client.docs[(config.items_index, f"r{i}")]["_seq_no"] == 1
        assert client.docs[(config.audits_index, f"cmp-r{i}")]["_seq_no"] == 0
    assert client.docs[(config.control_index, f"{SCOPE}|{DATE_STR}")][
        "_seq_no"] == 0
    # 重放只有两段 bulk：段 1 壳全 conflict + 段 2 审计重放对拍；
    # 段 3 零动作（全件已在段 1 判为幂等跳过）
    assert [c for c in client.calls if c[0] == "bulk"] == [
        ("bulk", 6), ("bulk", 6)]


def test_replay_after_partial_crash_completes(env):
    """崩溃点 = 段 3 中段：壳全建 + r0 已翻 + 审计已落 + 水位未推。
    整批重放：r0 幂等跳过零重写，r1/r2 走 A5 补翻，水位补推一次。"""
    client, store, config = env
    items = [_item(f"r{i}", i + 1) for i in range(3)]
    shells = store.bulk_create_shells([
        (it.record_id, _shell_of(it)) for it in items])
    store.bulk_cas_flip([("r0", dict(items[0].body), *shells["r0"])])
    store.write_audit_documents(
        [doc for it in items for doc in it.audit_docs])
    client.calls.clear()

    outcome = commit_batch(items, store, scope_id=SCOPE,
                           business_date=DATE_STR)
    assert outcome.flipped_ids == ("r1", "r2")
    assert outcome.idempotent_ids == ("r0",)
    assert outcome.watermark_prev == 0
    assert outcome.watermark_advanced_to == 3
    # r0 不重写：保持崩溃前首翻 seq_no=1；r1/r2 各补翻一次
    for i in range(3):
        assert client.docs[(config.items_index, f"r{i}")]["_seq_no"] == 1
        assert client.source(config.items_index, f"r{i}")[
            "delivery_state"] == "pending"
    # 审计重放对拍幂等：仍保持首建 seq_no=0，无重复写入
    for i in range(3):
        assert client.docs[(config.audits_index, f"cmp-r{i}")]["_seq_no"] == 0


# ---------- ⑦ 水位只推进到连续前缀（批内跳号） ----------

def test_watermark_advances_only_to_contiguous_prefix(env):
    client, store, config = env
    items = [_item("r0", 1), _item("r1", 2), _item("r2", 4)]  # 跳号 3
    outcome = commit_batch(items, store, scope_id=SCOPE,
                           business_date=DATE_STR)
    # 断裂件照样落库翻终态
    assert outcome.flipped_ids == ("r0", "r1", "r2")
    assert client.source(config.items_index, "r2")["delivery_state"] \
        == "pending"
    # 水位只推进到连续前缀 2，断裂件留下批补推进
    assert outcome.watermark_advanced_to == 2
    assert outcome.deferred_ids == ("r2",)
    assert client.source(config.control_index, f"{SCOPE}|{DATE_STR}")[
        "decision_watermark_seq"] == 2


# ---------- ⑧ 空批 = 零调用 ----------

def test_empty_batch_zero_calls(env):
    client, store, config = env
    outcome = commit_batch([], store, scope_id=SCOPE,
                           business_date=DATE_STR)
    assert outcome.flipped_ids == ()
    assert outcome.idempotent_ids == ()
    assert outcome.audit_doc_count == 0
    assert outcome.watermark_prev is None
    assert outcome.watermark_advanced_to is None
    assert outcome.deferred_ids == ()
    assert client.calls == []
    assert client.indices.refreshed == []
