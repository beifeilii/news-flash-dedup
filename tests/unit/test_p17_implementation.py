"""P17 实现测试：decide_for_task + audit writer + commit skeleton。

按 03:52/03:57 设计五标尺的本地实现覆盖：
- decide_for_task 集合级决策（aggregate 见全 required 集）
- audit writer payload_hash 一致性
- commit skeleton 不变量
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy

import pytest

from news_flash_dedup.commit import (
    CommitPlan,
    DecideResultStale,
    WatermarkSnapshot,
    incremental_query_window,
    is_watermark_complete,
    re_freeze_required,
)
from news_flash_dedup.decide import (
    audit as audit_module,
    service as decide_service,
)
from news_flash_dedup.decide.audit import (
    AuditBatch,
    AuditRecord,
    build_audit_batch,
    build_audit_record,
)


# ---------- 标尺 1：decide_for_task 集合级决策（L01/L02/L08 不变量） ----------

def _history_facts(text="甲公司完成回购。", subject="甲公司",
                    predicate="回购", record_id="a" * 64):
    return [
        {
            "fact_id": "f1",
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
            "event_state": {
                "predicate": {"status": "present", "raw_value": predicate,
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
                                "evidence": []},
            },
            "time": {"expression": {"status": "missing", "raw_value": None,
                                     "evidence": []},
                     "stage": {"status": "missing", "raw_value": None,
                                "evidence": []},
                     "anchor": {"status": "missing", "raw_value": None,
                                "evidence": []}},
            "key_object": {"status": "missing", "raw_value": None, "evidence": []},
            "numerics": [],
        }
    ]


def _ctx(record_id, item_id, text, arrival_seq, **kw):
    return {
        "record_id": record_id, "item_id": item_id, "text": text,
        "raw_hash": __import__('hashlib').sha256(text.encode("utf-8")).hexdigest(),
        "scope_id": "default", "business_date": "2026-09-26",
        "arrival_seq": arrival_seq, "pipeline_version": "dedup_v1",
        **kw,
    }


def _evidence_for(record_id, text):
    return {
        "fact_id": "f1",
        "evidence": [{"record_id": record_id, "field": "x",
                      "quote": "甲", "start": 0, "end": 1}],
        "fact_type": {"status": "present", "raw_value": "回购",
                      "evidence": [{"record_id": record_id, "field": "x",
                                    "quote": "甲", "start": 0, "end": 1}]},
        "subject": {"status": "present", "raw_value": "甲公司",
                    "evidence": [{"record_id": record_id, "field": "x",
                                  "quote": "甲", "start": 0, "end": 1}]},
        "event_state": {"predicate": {"status": "present", "raw_value": "回购",
                                      "evidence": [{"record_id": record_id, "field": "x",
                                                    "quote": "甲", "start": 0, "end": 1}]},
                        "polarity": {"status": "present", "raw_value": "回购",
                                      "evidence": [{"record_id": record_id, "field": "x",
                                                    "quote": "甲", "start": 0, "end": 1}]},
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
        "numerics": [],
    }


def test_decide_for_task_required_includes_history_and_candidates():
    """decide_for_task 必须把 history 与 candidates 都进 re_freeze required。"""
    text_a = "甲公司完成回购。"
    text_b = "甲公司完成回购。"
    text_c = "甲公司完成回购。"
    history = _ctx("a" * 64, "item-A", text_a, 1)
    current = _ctx("c" * 64, "item-C", text_c, 3)
    cand = _ctx("b" * 64, "item-B", text_b, 2)
    history["facts"] = [_evidence_for("a" * 64, text_a)]
    current["facts"] = [_evidence_for("c" * 64, text_c)]
    cand["facts"] = [_evidence_for("b" * 64, text_b)]
    outcome = decide_service.decide_for_task(history, [cand], current=current,
                                             coverage_complete=True)
    assert outcome.item_id == "item-C"
    assert set(outcome.pair_codes.keys()) == {"a" * 64, "b" * 64}
    for code in outcome.pair_codes.values():
        # 集合自含注记（W2Fε）：此白名单有意就地枚举、不从码面单一事实源
        # 导入——白名单纪律正是"码面新增未决码未评审时本钉转红"；若改为
        # 导入同一份常量，新增码自动入白、守闸失效
        assert code in {"FACT_EQUIVALENT", "EXACT_TEXT_MATCH",
                          "LOSSLESS_TEXT_MATCH", "TIME_RELATION_UNCERTAIN",
                          "FACT_INCOMPLETE", "VERIFIED_CONFLICT"}


def test_decide_cross_scope_cross_date_member_rejected():
    """R9 外部审核 F2 回归（主窗口 06:1x）：跨 scope/跨 business_date 的同文
    记录不得判重复——decide_for_task 曾把 history/current 的 scope_id/
    business_date 落默认值（候选却转发真实值，不对称），聚合绑定门
    （aggregate L177-184）比对 default==default 形同虚设。修复后转发真实
    值 → 域日不一致的成员被 IllegalMemberError fail-closed 拒绝。"""
    from news_flash_dedup.compare.aggregate import IllegalMemberError
    from news_flash_dedup.compare.pair_compare import PairBindingError
    text = "甲公司完成回购。"
    history = _ctx("a" * 64, "item-A", text, 1,
                   scope_id="scope-A", business_date="2026-09-25")
    current = _ctx("c" * 64, "item-C", text, 3,
                   scope_id="scope-B", business_date="2026-09-26")
    history["facts"] = [_evidence_for("a" * 64, text)]
    current["facts"] = [_evidence_for("c" * 64, text)]
    # 双层执法（06:5x 起）：对级门（pair_compare→PairBindingError）先触发，
    # 聚合门（aggregate→IllegalMemberError）兜底——二者任一即拒绝。
    with pytest.raises((PairBindingError, IllegalMemberError)):
        decide_service.decide_for_task(history, [], current=current,
                                       coverage_complete=True)


def test_decide_same_nondefault_scope_still_duplicates():
    """R9 F2 修复对照组：同 scope/同日（非默认值）转发后域日门通过，
    同文 EXACT 证书路径不受影响仍判重复。
    （N29/D28 注：时间槽补真 P14"确无"verified 形态维持 EXACT 路径覆盖——
    未命中型 missing 现记 unknown → 边界，与本用例域日门对照意图无关；
    语义钉值见 test_n29_honest_missing。）"""
    text = "甲公司完成回购。"
    history = _ctx("a" * 64, "item-A", text, 1,
                   scope_id="scope-X", business_date="2026-09-25")
    current = _ctx("c" * 64, "item-C", text, 3,
                   scope_id="scope-X", business_date="2026-09-25")
    # E 批 H-04 F1 夹具升级（e-batch-design §3.3 F1 行+§3.4 第 4 条）：
    # field='x' 伪证据→校验可过形态（正规槽位路径+全文覆盖 [0,len)+
    # 文本无数值 token 免认领+无模态词免 polarity 覆盖+fact_id 顺序）；
    # "重复"签署断言不动。verified_missing 注入保留于原工件（投影只剥
    # 校验输入，p15_integration.py:121/:208 消费面零破）。
    from test_e_h04_facts_projection import _complete_fact
    history["facts"] = [_complete_fact("a" * 64, text)]
    current["facts"] = [_complete_fact("c" * 64, text)]
    for facts in (history["facts"], current["facts"]):
        facts[0]["time"]["expression"] = {
            "status": "missing", "raw_value": None, "evidence": [],
            "verified_missing": True}
    outcome = decide_service.decide_for_task(history, [], current=current,
                                             coverage_complete=True)
    assert outcome.decision == "重复"
    assert outcome.duplicate_ids == ("item-A",)


def test_decide_for_task_raises_when_no_current_and_no_candidates():
    """P17 03:57 §6.1：current 与 candidates 同时缺失 → DecideInputError。"""
    text = "甲公司完成回购。"
    history = _ctx("a" * 64, "item-A", text, 1)
    history["facts"] = [_evidence_for("a" * 64, text)]
    with pytest.raises(decide_service.DecideInputError):
        decide_service.decide_for_task(history, [], coverage_complete=True)


# ---------- 标尺 2：audit writer payload_hash 一致性 ----------

def _make_pair():
    from news_flash_dedup.compare.pair_compare import PairResult
    return PairResult(
        pair_id="aaa|bbb",
        history_record_id="a" * 64, current_record_id="b" * 64,
        history_item_id="item-A", current_item_id="item-B",
        history_arrival_seq=1, current_arrival_seq=2,
        history_raw_hash="h-hash", current_raw_hash="c-hash",
        pipeline_version="dedup_v1",
        outcome="equivalent", code="FACT_EQUIVALENT",
        detail="stub",
        aligned_facts=(), verified_conflicts=(),
        unresolved_fields=(), used_evidence=(), budget_at=0,
    )


def test_audit_record_payload_hash_stable_across_construction():
    """AuditRecord.payload_hash 自计算；相同输入两次构造 hash 一致。"""
    pair = _make_pair()
    rec1 = build_audit_record(pair, field_path="facts.f1", basis="FACT_EQUIVALENT",
                                detail="subject/predicate aligned")
    rec2 = build_audit_record(pair, field_path="facts.f1", basis="FACT_EQUIVALENT",
                                detail="subject/predicate aligned")
    assert rec1.payload_hash == rec2.payload_hash
    # 09-28 F-2 修复后契约形态（10 §3 L48 公式/L55 方向句——09-29 W3b-F1
    # 锚勘正：原注"L43/L48"系四级载体连锁错锚，L43 实为 record_id 公式行）：
    # comparison_id=SHA256(JCS([query=current 新稿, candidate=history 候选,
    # pipeline_version]))——单 64 hex 串无冒号；旧钉所钉 "a…:b…" 裸拼接系被
    # 裁定移除的违规形态（WB4/WB3 审计，W2Fα2 条目 46 落地）。值=生产
    # _comparison_id 实算（log\temp\w2fe-comparison-id-values.py 双路径互证；
    # W3b 独立 JCS 复算 w3b-comparison-id-recheck.json verdict 三键全 true）。
    assert rec1.comparison_id == (
        "82195df256d8674bf37e6a9fc02b98780d8cf701b630a85000a1586f0695f701")


def test_audit_record_rejects_self_audit():
    """history_record_id == current_record_id → 禁止（防止自指审计）。"""
    pair = _make_pair()
    pair = pair.__class__(
        pair_id=pair.pair_id,
        history_record_id=pair.history_record_id,
        current_record_id=pair.history_record_id,  # 同 record_id
        history_item_id=pair.history_item_id,
        current_item_id=pair.current_item_id,
        history_arrival_seq=pair.history_arrival_seq,
        current_arrival_seq=pair.current_arrival_seq,
        history_raw_hash=pair.history_raw_hash,
        current_raw_hash=pair.current_raw_hash,
        pipeline_version=pair.pipeline_version,
        outcome=pair.outcome, code=pair.code, detail=pair.detail,
        aligned_facts=pair.aligned_facts,
        verified_conflicts=pair.verified_conflicts,
        unresolved_fields=pair.unresolved_fields,
        used_evidence=pair.used_evidence,
        budget_at=pair.budget_at,
    )
    with pytest.raises(ValueError, match="(?i)self-audit|self audit|forbidden"):
        build_audit_record(pair)


def test_audit_batch_provides_bulk_actions():
    """AuditBatch.to_index_actions 生成 ES bulk 写入 actions。"""
    batch = build_audit_batch([_make_pair()], index_prefix="p17-test-audits-v1",
                              audit_complete=True)  # 窗口V：去默认后显式化
    assert isinstance(batch, AuditBatch)
    assert batch.audit_complete is True
    actions = batch.to_index_actions("p17-test-audits-v1")
    assert len(actions) == 2  # 1 action + 1 doc
    assert "index" in actions[0]


# ---------- 标尺 3：commit skeleton 不变量 ----------

def test_watermark_complete_when_both_cover_floor():
    wm = WatermarkSnapshot(
        scope_id="default", business_date="2026-09-26",
        last_materialized_seq=10, lexical_watermark=10, prepared_seq=10,
        owner_id="owner-A", owner_generation=1,
    )
    ok, detail = is_watermark_complete(wm, arrival_seq=11)
    assert ok is True
    assert detail == ""


def test_watermark_incomplete_when_prepared_short():
    wm = WatermarkSnapshot(
        scope_id="default", business_date="2026-09-26",
        last_materialized_seq=10, lexical_watermark=10, prepared_seq=5,
        owner_id="owner-A", owner_generation=1,
    )
    ok, detail = is_watermark_complete(wm, arrival_seq=11)
    assert ok is False
    assert "prepared_seq 5" in detail


def test_incremental_query_window_default_budget():
    floor, ceiling = incremental_query_window(arrival_seq=300, budget=200)
    assert floor == 100
    assert ceiling == 299


def test_re_freeze_required_sorts_by_arrival_seq():
    """re_freeze 必须按 arrival_seq 升序；history ∪ candidates 全集。"""
    history = _ctx("a" * 64, "item-A", "x", 1)
    cand1 = _ctx("b" * 64, "item-B", "x", 3)
    cand2 = _ctx("c" * 64, "item-C", "x", 2)
    out = re_freeze_required(history, [cand1, cand2])
    seqs = [v["arrival_seq"] for v in out.values()]
    assert seqs == [1, 2, 3]
    assert set(out.keys()) == {"a" * 64, "b" * 64, "c" * 64}


def test_decide_result_stale_string_format():
    stale = DecideResultStale(
        code="prepared_seq_stale",
        detail="prepared_seq 5 < 10",
        arrival_seq=11, scope_id="default", business_date="2026-09-26",
    )
    assert "DECIDE_RESULT_STALE[prepared_seq_stale]" in str(stale)
    assert "arrival_seq=11" in str(stale)


# ---------- 标尺 1：多候选混合场景（≥3 候选混合 equivalent/conflict/unresolved + extra_event） ----------

def test_decide_for_task_three_candidates_mixed_outcomes():
    """≥3 候选：1 个 equivalent + 1 个 conflict + 1 个 unrelated；aggregate 见全 required。"""
    text_equiv = "甲公司完成回购。"
    text_conflict = "甲公司公布现价100元。"
    text_unrelated = "乙公司发布新手机。"

    def _ctx_for(text, record_id, item_id, arrival_seq, facts):
        return {
            "record_id": record_id, "item_id": item_id, "text": text,
            "raw_hash": __import__('hashlib').sha256(text.encode("utf-8")).hexdigest(),
            "scope_id": "default", "business_date": "2026-09-26",
            "arrival_seq": arrival_seq, "pipeline_version": "dedup_v1",
            "facts": facts,
        }

    def _ev(record_id, text, subject="甲公司", predicate="回购"):
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

    history = _ctx_for(text_equiv, "a" * 64, "item-A", 1,
                          _ev("a" * 64, text_equiv))
    current = _ctx_for(text_equiv, "c" * 64, "item-C", 4,
                          _ev("c" * 64, text_equiv))
    cand_equiv = _ctx_for(text_equiv, "b" * 64, "item-B", 2,
                            _ev("b" * 64, text_equiv))
    cand_conflict = _ctx_for(text_conflict, "d" * 64, "item-D", 3,
                               _ev("d" * 64, text_conflict))
    outcome = decide_service.decide_for_task(
        history, [cand_equiv, cand_conflict], current=current,
        coverage_complete=True,
    )
    # aggregate 必须见全 required（L02/L01）；pair_codes 含全部 4 个 record_id
    assert set(outcome.pair_codes.keys()) >= {"a" * 64, "b" * 64, "d" * 64}


# ---------- 标尺 3：STALE 路径（prepared_seq/lexical_watermark 过期） ----------

def test_stale_when_prepared_seq_insufficient():
    """prepared_seq < arrival_seq - 1 → DECIDE_RESULT_STALE。"""
    wm = WatermarkSnapshot(
        scope_id="default", business_date="2026-09-26",
        last_materialized_seq=10, lexical_watermark=10, prepared_seq=5,
        owner_id="owner-A", owner_generation=1,
    )
    ok, detail = is_watermark_complete(wm, arrival_seq=11)
    assert ok is False
    assert detail.startswith("prepared_seq")
    # DecideResultStale 形式
    stale = DecideResultStale(
        code="prepared_seq_stale", detail=detail,
        arrival_seq=11, scope_id="default", business_date="2026-09-26",
    )
    assert "prepared_seq 5" in str(stale)


def test_stale_when_lexical_watermark_insufficient():
    """lexical_watermark < arrival_seq - 1 → DECIDE_RESULT_STALE。"""
    wm = WatermarkSnapshot(
        scope_id="default", business_date="2026-09-26",
        last_materialized_seq=10, lexical_watermark=5, prepared_seq=10,
        owner_id="owner-A", owner_generation=1,
    )
    ok, detail = is_watermark_complete(wm, arrival_seq=11)
    assert ok is False
    assert detail.startswith("lexical_watermark")


# ---------- 标尺 4：审计前置（audit_complete=False 时 CAS 拒绝） ----------

def test_audit_complete_required_for_cas():
    """audit_complete 真值驱动 commit_one CAS 前置：False 拒写 / True 提交。

    窗口K 重铸（D30 H-07-c）：原版仅构造 AuditBatch 回显 audit_complete
    （注释自承"CAS 拒绝逻辑待 commit_one 实现"）——零驱动恒真占位。
    commit_one 的 CAS 前置早已落地（commit/coordinator.py 审计前置段：
    audit_complete=False 且未显式放行 → CommitOneError 零写入），现重铸
    为真驱动两路 + 显式放行对照路。
    """
    from news_flash_dedup.commit import CommitContext, CommitOneError, commit_one
    from news_flash_dedup.commit.fake_store import FakeCommitStore

    def _commit_ctx():
        current = _ctx("c" * 64, "item-C", "甲公司完成回购。", 1)
        return CommitContext(
            scope_id="default", business_date="2026-09-26",
            arrival_seq=1, current=current, candidates=(),
            visible_seq=10, prepared_seq=10, coverage_complete=True)

    # 路 1：audit_complete=False → CAS 拒绝（CommitOneError）且零写入
    store_refused = FakeCommitStore()
    with pytest.raises(CommitOneError, match="audit_complete"):
        commit_one(_commit_ctx(), store_refused, audit_complete=False)
    assert store_refused.main_records == {}
    assert store_refused.cas_calls == []
    assert store_refused.audit_docs == {}

    # 路 2：audit_complete=True → CAS 提交，主记录 audit_complete=True 实落
    store_committed = FakeCommitStore()
    outcome = commit_one(_commit_ctx(), store_committed, audit_complete=True)
    assert outcome.state == "committed"
    assert outcome.audit_complete is True
    assert outcome.main_record_id == "c" * 64
    record = store_committed.main_records["c" * 64]
    assert record.audit_complete is True
    assert [c["action"] for c in store_committed.cas_calls] == ["write_main_record"]

    # 路 3：显式放行 audit_complete=False → 不写主记录，outcome 如实 False
    store_allowed = FakeCommitStore()
    outcome_allowed = commit_one(_commit_ctx(), store_allowed,
                                 allow_audit_complete_false=True,
                                 audit_complete=False)
    assert outcome_allowed.audit_complete is False
    assert store_allowed.main_records == {}


def test_audit_batch_complete_marker_passes():
    """AuditBatch.audit_complete=True 时下游可正常推进 CAS（标记信号）。"""
    batch = build_audit_batch([_make_pair()], audit_complete=True)
    assert batch.audit_complete is True
    assert len(batch.records) == 1
    actions = batch.to_index_actions("p17-test-audits-v1")
    assert len(actions) == 2  # action + doc


# ---------- 标尺 2：P16 回归零破损（decide_once 委托包装） ----------

def test_p16_decide_once_delegates_to_for_task():
    """decide_once 旧单对签名仍可用；内部委托 decide_for_task 不破坏既有行为。"""
    text = "甲公司完成回购。"
    history = {
        "record_id": "a" * 64, "item_id": "item-A", "text": text,
        "raw_hash": __import__('hashlib').sha256(text.encode("utf-8")).hexdigest(),
        "scope_id": "default", "business_date": "2026-09-26",
        "arrival_seq": 1, "pipeline_version": "dedup_v1",
        "facts": [_evidence_for("a" * 64, text)],
    }
    current = {
        "record_id": "b" * 64, "item_id": "item-B", "text": text,
        "raw_hash": __import__('hashlib').sha256(text.encode("utf-8")).hexdigest(),
        "scope_id": "default", "business_date": "2026-09-26",
        "arrival_seq": 2, "pipeline_version": "dedup_v1",
        "facts": [_evidence_for("b" * 64, text)],
    }
    out = decide_service.decide_once(history, current, coverage_complete=True)
    assert out.item_id == "item-B"
    assert out.pipeline_version == "dedup_v1"


# ---------- 标尺 5：单元层不接真 ES ----------

def test_no_es_client_imported_at_module_level():
    """P17 模块不引入 ES 客户端；ES bulk 仅生成 actions 由上层执行。"""
    import news_flash_dedup.commit as commit_module
    import news_flash_dedup.decide as decide_module
    import news_flash_dedup.decide.audit as audit_module
    for mod in (commit_module, decide_module, audit_module):
        module_attrs = dir(mod)
        for attr in ("ESClient", "elasticsearch", "es_client", "Elasticsearch"):
            assert attr not in module_attrs, (
                f"{mod.__name__} 不应在单元层导入 ES 客户端 {attr!r}"
            )