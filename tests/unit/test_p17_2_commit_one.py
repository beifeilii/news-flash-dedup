"""P17-2 commit_one 编排端到端测试（按 04:06 强制令标尺 1-3）。

覆盖：
1. STALE 端到端双场景（prepared_seq / lexical_watermark 过期）
2. audit_complete=False → CAS 拒绝
3. CAS 成功路径字段断言
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from copy import deepcopy

import pytest

from news_flash_dedup.commit import (
    CommitContext,
    CommitOneError,
    CommitOutcome,
    DecideResultStale,
    commit_one,
)
from news_flash_dedup.commit.coordinator import (
    _build_callback_body,
    _compute_payload_hash,
)
from news_flash_dedup.commit.fake_store import (
    FakeCommitStore,
    FakeMainRecord,
)


def _text_with_facts(text="甲公司完成回购。", subject="甲公司",
                     predicate="回购", record_id="a" * 64):
    return [
        {"fact_id": "f1",
         "evidence": [{"record_id": record_id, "field": "x",
                       "quote": subject, "start": 0, "end": len(subject)}],
         "fact_type": {"status": "present", "raw_value": predicate,
                       "evidence": [{"record_id": record_id, "field": "x",
                                     "quote": subject, "start": 0,
                                     "end": len(subject)}]},
         "subject": {"status": "present", "raw_value": subject,
                     "evidence": [{"record_id": record_id, "field": "x",
                                   "quote": subject, "start": 0,
                                   "end": len(subject)}]},
         "event_state": {"predicate": {"status": "present", "raw_value": predicate,
                                      "evidence": [{"record_id": record_id, "field": "x",
                                                    "quote": subject, "start": 0,
                                                    "end": len(subject)}]},
                         "polarity": {"status": "present", "raw_value": predicate,
                                      "evidence": [{"record_id": record_id, "field": "x",
                                                    "quote": subject, "start": 0,
                                                    "end": len(subject)}]},
                         "modality": {"status": "missing", "raw_value": None,
                                      "evidence": []},
                         "attribution": {"status": "missing", "raw_value": None,
                                         "evidence": []}},
         "time": {"expression": {"status": "missing", "raw_value": None,
                                  "evidence": []},
                  "stage": {"status": "missing", "raw_value": None,
                             "evidence": []},
                  "anchor": {"status": "missing", "raw_value": None,
                             "evidence": []}},
         "key_object": {"status": "missing", "raw_value": None, "evidence": []},
         "numerics": []}
    ]


def _ctx_for(text, record_id, item_id, arrival_seq):
    return {
        "record_id": record_id, "item_id": item_id, "text": text,
        "raw_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "scope_id": "default", "business_date": "2026-09-26",
        "arrival_seq": arrival_seq, "pipeline_version": "dedup_v1",
        "facts": _text_with_facts(text=text, record_id=record_id),
    }


def _commit_ctx(*, arrival_seq, prepared_seq, visible_seq, candidates=(),
                 text="甲公司完成回购。", record_id="c" * 64,
                 item_id="item-C") -> CommitContext:
    current = _ctx_for(text, record_id, item_id, arrival_seq)
    # 构造 history = 一个已落库的先前条目（不同 record_id）
    history = _ctx_for(
        text + "（同日先到条）", "a" * 64, "item-A", max(1, arrival_seq - 1)
    )
    # 把 history 也作为候选（让 decide_for_task 见全 required）
    candidates = (history,) + tuple(candidates)
    return CommitContext(
        scope_id="default", business_date="2026-09-26",
        arrival_seq=arrival_seq, current=current, candidates=candidates,
        visible_seq=visible_seq, prepared_seq=prepared_seq,
        coverage_complete=True,
    )


# ---------- 标尺 0：R9 F3 分区首条（零候选）提交 ----------

def test_first_record_empty_candidates_commits_not_duplicate():
    """R9 外部审核 F3 回归（主窗口 06:5x）：arrival_seq=1 且 candidates=()
    （分区首条）原先 history=current 自对自身 → PairAlignmentError 冒泡、
    零写入（r9a-f3-repro.py 实证 100% 崩溃）。修复后短路为结构性"不重复"
    （NO_DUPLICATE_FOUND），正常提交推进水位。"""
    store = FakeCommitStore()
    current = _ctx_for("甲公司完成回购。", "c" * 64, "item-C", 1)
    ctx = CommitContext(
        scope_id="default", business_date="2026-09-26",
        arrival_seq=1, current=current, candidates=(),
        visible_seq=1, prepared_seq=1, coverage_complete=True,
    )
    outcome = commit_one(ctx, store, audit_complete=True)
    assert outcome.state == "committed"
    assert outcome.decide_outcome.decision == "不重复"
    assert outcome.decide_outcome.duplicate_ids == ()
    assert outcome.decide_outcome.internal_code == "NO_DUPLICATE_FOUND"
    record = store.main_records[current["record_id"]]
    assert record.decision == "不重复"
    assert store.get_watermark("default", "2026-09-26") == 1


def test_first_record_empty_candidates_incomplete_coverage_is_boundary():
    """D24 三方审计回归：零候选 + coverage_complete=False 时"无候选"可能是
    召回不全假象——案二不变量（覆盖不全不得"不重复"）对零候选分支同样
    适用，必须判边界（RECALL_INCOMPLETE），不得硬编码"不重复"。"""
    store = FakeCommitStore()
    current = _ctx_for("甲公司完成回购。", "c" * 64, "item-C", 1)
    ctx = CommitContext(
        scope_id="default", business_date="2026-09-26",
        arrival_seq=1, current=current, candidates=(),
        visible_seq=1, prepared_seq=1, coverage_complete=False,
    )
    outcome = commit_one(ctx, store, audit_complete=True)
    assert outcome.decide_outcome.decision == "边界case/疑难case"
    assert outcome.decide_outcome.internal_code == "RECALL_INCOMPLETE"
    assert outcome.decide_outcome.duplicate_ids == ()


def test_event_id_follows_frozen_jcs_derivation():
    """D24 三方审计回归：10 §5 冻结 event_id =
    SHA256(JCS([scope_id, request_id, result_version]))——拒绝旧式
    sha256(item_id+completed_at)[:32] 字符串拼接。

    冻结字面钉（W-R3b / R3-M7-c4，w-r3b-probes.json 实算）：原断言
    record.event_id == _admission._digest([...]) 系 f(x)==f(x) 自比——
    commit 派生本体即 _admission._digest（coordinator.py:273），JCS
    分隔符漂移时双侧同步漂移永不红；冻结字面对派生链任何漂移咬。"""
    store = FakeCommitStore()
    current = _ctx_for("甲公司完成回购。", "c" * 64, "item-C", 1)
    current = dict(current, request_id="req-42")
    ctx = CommitContext(
        scope_id="default", business_date="2026-09-26",
        arrival_seq=1, current=current, candidates=(),
        visible_seq=1, prepared_seq=1, coverage_complete=True,
    )
    outcome = commit_one(ctx, store, audit_complete=True)
    record = store.main_records[current["record_id"]]
    assert record.event_id == ("d11d8f62f0bba6d1f21286f32eee37292c9204410327b4"
                               "62af2f6b35189d75e8")
    # fake 层无 request_id 时确定性回退 record_id（同源替代，见 D24 注记）
    store2 = FakeCommitStore()
    current2 = _ctx_for("甲公司完成回购。", "c" * 64, "item-C", 1)
    ctx2 = CommitContext(
        scope_id="default", business_date="2026-09-26",
        arrival_seq=1, current=current2, candidates=(),
        visible_seq=1, prepared_seq=1, coverage_complete=True,
    )
    commit_one(ctx2, store2, audit_complete=True)
    record2 = store2.main_records[current2["record_id"]]
    assert record2.event_id == ("5687b8bc811adba45300a5ecdd4b55b64b3b442bfb562"
                                "4fd5b08cf282852ac1d")


def test_commit_writes_real_audit_batch_and_ids():
    """R9 外部审核 F4 回归（主窗口 07:0x）：原先 build_audit_batch([]) 恒空、
    audit_ids=() 而 audit_complete=True（r9b-f4-repro.py 实证）。修复后：
    审计文档按真实对级 PairResult 落库，audit_ids=comparison_id 集合与
    decide.pair_codes 键一致。"""
    store = FakeCommitStore()
    ctx = _commit_ctx(arrival_seq=10, prepared_seq=10, visible_seq=10)
    outcome = commit_one(ctx, store, audit_complete=True)
    assert outcome.state == "committed"
    record = store.main_records[ctx.current["record_id"]]
    assert record.audit_complete is True
    assert record.audit_ids, "审计 ID 不得为空（F4 修复点）"
    # 09-28 F-2 修复后契约形态（10 §3 L48 公式/L55 方向句——09-29 W3b-F1
    # 锚勘正：原注"L43/L48"系四级载体连锁错锚，L43 实为 record_id 公式行）：
    # comparison_id=SHA256(JCS([query=current 新稿, candidate=history 候选,
    # pipeline_version]))——单 64 hex 串无冒号；旧钉所钉 "history:current"
    # 裸拼接系被裁定移除的违规形态（WB4/WB3 审计，W2Fα2 条目 46 落地）。
    # 值=生产 _comparison_id 实算（log\temp\w2fe-comparison-id-values.py
    # 双路径互证；W3b 独立 JCS 复算 w3b-comparison-id-recheck.json verdict
    # 三键全 true）。
    expected = {
        "766f59d1bd05954e85b2d4b485bf49961597a1713bc60ff3159f0bf6b5c1440e"}
    assert set(record.audit_ids) == expected
    assert set(store.audit_docs) == expected
    doc = store.audit_docs[
        "766f59d1bd05954e85b2d4b485bf49961597a1713bc60ff3159f0bf6b5c1440e"]
    assert doc["history_record_id"] == "a" * 64
    assert doc["current_record_id"] == "c" * 64
    assert doc["payload_hash"], "审计载荷 hash 必填"
    # 审计覆盖不变式：pair_codes 键=被比对候选 record_id；每个候选必有
    # 对应 comparison_id（契约公式=SHA256(JCS([当前,候选,版本]))）审计文档
    current_rid = ctx.current["record_id"]
    covered = {hashlib.sha256(json.dumps(
        [current_rid, rid, "dedup_v1"],
        ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()
        for rid in outcome.decide_outcome.pair_codes}
    assert set(record.audit_ids) == covered


# ---------- 标尺 1：STALE 端到端双场景 ----------

def test_stale_endpoint_when_prepared_seq_insufficient():
    """prepared_seq < arrival_seq - 1 → DECIDE_RESULT_STALE(prepared_seq_stale)。"""
    store = FakeCommitStore()
    ctx = _commit_ctx(arrival_seq=10, prepared_seq=5, visible_seq=10)
    outcome = commit_one(ctx, store, audit_complete=True)
    assert outcome.state == "stale"
    assert isinstance(outcome.stale, DecideResultStale)
    assert outcome.stale.code == "prepared_seq_stale"
    assert "prepared_seq 5" in outcome.stale.detail
    # 不抛异常（state==stale 而非 raise）
    assert outcome.decide_outcome is None
    # 无写入动作
    assert store.main_records == {}
    assert store.cas_calls == []


def test_stale_endpoint_when_lexical_watermark_insufficient():
    """lexical_watermark < arrival_seq - 1 → DECIDE_RESULT_STALE(lexical_watermark_stale)。"""
    store = FakeCommitStore()
    ctx = _commit_ctx(arrival_seq=10, prepared_seq=10, visible_seq=5)
    outcome = commit_one(ctx, store, audit_complete=True)
    assert outcome.state == "stale"
    assert outcome.stale.code == "lexical_watermark_stale"
    assert "lexical_watermark 5" in outcome.stale.detail
    assert store.main_records == {}


# ---------- 标尺 2：audit_complete=False → CAS 拒绝 ----------

def test_cas_refused_when_audit_complete_false():
    """audit_complete=False 时 commit_one 拒绝 CAS（无写入动作发生）。"""
    store = FakeCommitStore()
    ctx = _commit_ctx(arrival_seq=10, prepared_seq=10, visible_seq=10)
    # 默认 allow_audit_complete_false=False → 抛 CommitOneError（CAS 拒绝合同）
    with pytest.raises(CommitOneError, match="(?i)audit_complete"):
        commit_one(ctx, store, audit_complete=False)
    # 拒绝路径：store 不应有任何写入
    assert store.main_records == {}
    assert store.audit_docs == {}
    assert store.watermark_seq == {}


def test_cas_refused_when_audit_complete_false_allow_returns_stale():
    """allow_audit_complete_false=True → 返回 state=stale，audit_complete=False。"""
    store = FakeCommitStore()
    ctx = _commit_ctx(arrival_seq=10, prepared_seq=10, visible_seq=10)
    outcome = commit_one(ctx, store, allow_audit_complete_false=True,
                          audit_complete=False)
    assert outcome.state == "stale"
    assert outcome.audit_complete is False
    assert outcome.decide_outcome is not None
    assert store.main_records == {}


# ---------- 标尺 3：CAS 成功路径字段断言 ----------

def test_cas_success_writes_main_record_with_required_fields():
    """CAS 成功：主记录字段集 + decision_watermark 推进。"""
    store = FakeCommitStore()
    text = "甲公司完成回购。"
    record_id = "c" * 64
    ctx = _commit_ctx(arrival_seq=10, prepared_seq=10, visible_seq=10,
                       text=text, record_id=record_id)
    outcome = commit_one(ctx, store, audit_complete=True)
    assert outcome.state == "committed"
    assert outcome.main_record_id == record_id
    assert outcome.audit_complete is True
    assert outcome.decision_watermark_advanced_to == 10

    # 主记录字段集断言（10 §6.3 一次 CAS 字段集）
    record = store.main_records[record_id]
    # 10-07 令甲-i（I-5）：三值通吃写法拆除，按设计备选口径"收紧为本夹具
    # 实际判定值"——实测本夹具判定=边界case/疑难case（current 与 history
    # 文本近缘判边界；设计稿"实为非重复路径"实测不符，偏差已登记核销表）。
    # 分派钉：边界↔held（扣留不投递）。
    assert record.decision == "边界case/疑难case"
    assert record.item_id == "item-C"
    assert record.text == text
    assert record.delivery_state == "held"
    assert record.callback_attempts == 0
    assert record.audit_complete is True
    assert record.result_version == 1
    assert record.pipeline_version == "dedup_v1"
    assert record.raw_hash == hashlib.sha256(text.encode("utf-8")).hexdigest()
    assert record.delivery_deadline_at != ""

    # payload_hash 应等于 sha256(callback_body 的 UTF-8 字节)
    expected_payload_hash = _compute_payload_hash(
        _build_callback_body(outcome.decide_outcome)
    )
    assert record.payload_hash == expected_payload_hash

    # decision_watermark 已推进
    assert store.watermark_seq["default|2026-09-26"] == 10
    assert len(store.watermark_calls) == 1
    assert store.watermark_calls[0]["arrival_seq"] == 10


def test_cas_success_callback_body_is_five_field_public_dict():
    """回调冻结载荷（callback_body）恰为 DecideOutcome 五字段公共 dict 序列化。"""
    store = FakeCommitStore()
    ctx = _commit_ctx(arrival_seq=5, prepared_seq=5, visible_seq=5)
    outcome = commit_one(ctx, store, audit_complete=True)
    record_id = "c" * 64
    record = store.main_records[record_id]
    # payload_hash 自洽：解码 callback_body 反推应与 decide_outcome 一致
    import hashlib
    body = _build_callback_body(outcome.decide_outcome)
    assert hashlib.sha256(body.encode("utf-8")).hexdigest() == record.payload_hash
    decoded = json.loads(body)
    assert set(decoded.keys()) == {"item_id", "text", "decision",
                                     "duplicate_ids", "reason"}


def test_cas_success_does_not_write_when_already_committed():
    """同 arrival_seq 已 committed 时不允许回归（watermark 单调）。"""
    store = FakeCommitStore()
    store.watermark_seq["default|2026-09-26"] = 10
    ctx = _commit_ctx(arrival_seq=10, prepared_seq=10, visible_seq=10)
    with pytest.raises(ValueError, match="(?i)regress|watermark"):
        commit_one(ctx, store, audit_complete=True)
    assert store.main_records == {}


# ---------- 标尺 5：单元层不接真 ES（伪测试，验证 store 类型） ----------

def test_fake_store_does_not_import_es_client():
    """FakeCommitStore 不引入真 ES 客户端；纯 dict 状态。"""
    import news_flash_dedup.commit.fake_store as fs
    module_attrs = dir(fs)
    for attr in ("ESClient", "elasticsearch", "es_client", "Elasticsearch"):
        assert attr not in module_attrs, (
            f"fake_store 不应引入真 ES 客户端 {attr!r}"
        )


# ---------- P16 回归零破损（继承 P17-1 已有，但加一个端到端 commit_one 完整路径） ----------

def test_commit_one_end_to_end_smoke():
    """完整路径：commit_one() 一次完整跑通；store 状态变化符合预期。"""
    store = FakeCommitStore()
    ctx = _commit_ctx(arrival_seq=10, prepared_seq=10, visible_seq=10)
    outcome = commit_one(ctx, store, audit_complete=True)
    assert outcome.state == "committed"
    assert len(store.cas_calls) >= 1   # write_main_record
    assert len(store.watermark_calls) == 1
    assert store.main_records["c" * 64].decision in {
        "重复", "不重复", "边界case/疑难case"
    }

def test_coverage_complete_false_blocks_decision_not_duplicate():
    """13:50 R3 补点名：coverage_complete=False → 决策不得为"不重复"。

    aggregate 已具备"coverage 不全走边界"语义（不是"不重复"）；commit_one 必须传 False。
    窗口V（N-03）勘误：原 docstring 虚标"已改用不匹配候选夹具……具备真红
    能力"——夹具实未改，`_commit_ctx` 仍为近似 history（判重复方向），
    coverage 穿透断裂经本用例不可翻红。真红能力由 :362-386
    `test_coverage_threading_wiring_is_real` 间谍测试兜底（monkeypatch
    decide_for_task 捕获 coverage_complete kwarg，穿透断即红）；本用例
    定位=聚合语义端到端冒烟（False 不判"不重复"）。
    """
    store = FakeCommitStore()
    ctx = _commit_ctx(arrival_seq=10, prepared_seq=10, visible_seq=10)
    outcome = commit_one(ctx, store, coverage_complete=False,
                         audit_complete=True)
    assert outcome.state != "committed" or outcome.decide_outcome.decision != "不重复", (
        "coverage_complete=False 时决策不得为不重复（半活地雷已排除）"
    )


def test_coverage_threading_wiring_is_real(monkeypatch):
    """D24 三方审计回归：原夹具恒判重复（近似 history），coverage 穿透
    断裂不可见；案二聚合语义已由 test_p16_c 钉死（零 issue+不全→边界
    RECALL_INCOMPLETE），本测试钉 commit→decide 的接线本身——间谍
    decide_for_task 捕获 coverage_complete kwarg（穿透断即红）。"""
    from news_flash_dedup.decide import service as _decide_service
    real = _decide_service.decide_for_task
    captured: dict = {}

    def spy(**kw):
        captured["coverage_complete"] = kw.get("coverage_complete")
        return real(**kw)

    monkeypatch.setattr(_decide_service, "decide_for_task", spy)
    ctx = _commit_ctx(arrival_seq=10, prepared_seq=10, visible_seq=10)
    commit_one(ctx, FakeCommitStore(), coverage_complete=False,
               audit_complete=True)
    assert captured["coverage_complete"] is False
    # None → 从 ctx 派生（案二"禁默认 True"整改点）
    ctx2 = CommitContext(
        scope_id="default", business_date="2026-09-26",
        arrival_seq=10, current=ctx.current, candidates=ctx.candidates,
        visible_seq=10, prepared_seq=10, coverage_complete=False,
    )
    commit_one(ctx2, FakeCommitStore(), audit_complete=True)
    assert captured["coverage_complete"] is False
