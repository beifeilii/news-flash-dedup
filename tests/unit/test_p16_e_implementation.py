"""P16-E 决定层 DTO 与 extra_event RULE_UNCOVERED 检测（红/绿双落盘）。

按 03:44 批复五标尺：
1. extra_event RULE_UNCOVERED 检测（aligned_facts 之上做 Fact 计数差比对）
2. DTO 封门（PairResult/AggregateOutcome → 公共 dict 序列化）
3. P16-C 挂账执行（test_aggregate.py 18 WIP-DRAFT 激活或删除）
4. 09 §11.3 时间阻塞路径编排层端到端复验
5. 红绿双落盘 + 全量单测 + footer 时间戳实测
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy

import pytest

from news_flash_dedup.decide import (
    service as decide_service,
    types as decide_types,
)
from news_flash_dedup.decide.extra_event import (
    detect_extra_event,
    ExtraEventReport,
)
from news_flash_dedup.facts import FactValidationReport


# ---------- 标尺 1：extra_event RULE_UNCOVERED 检测 ----------

def test_extra_event_detects_more_facts_in_current():
    """extra_event：current 有更多 Facts（无关补充），应报 uncovered。"""
    history_text = "甲公司发布新手机。"
    current_text = "甲公司发布新手机，同时披露季度财报。"
    history = {"record_id": "a" * 64, "facts": [{"fact_id": "f1"}], "text": history_text}
    current = {"record_id": "b" * 64,
                "facts": [{"fact_id": "f1"}, {"fact_id": "f2"}],
                "text": current_text}
    report = detect_extra_event(history, current)
    assert isinstance(report, ExtraEventReport)
    assert report.has_extra_event is True
    assert report.uncovered_fact_ids == ("current:f2",)


def test_extra_event_detects_more_facts_in_history():
    """extra_event：history 有更多 Facts（无关补充），应报 uncovered。"""
    history_text = "甲公司发布新手机，同时披露季度财报。"
    current_text = "甲公司发布新手机。"
    history = {"record_id": "a" * 64,
                "facts": [{"fact_id": "f1"}, {"fact_id": "f2"}],
                "text": history_text}
    current = {"record_id": "b" * 64, "facts": [{"fact_id": "f1"}],
                "text": current_text}
    report = detect_extra_event(history, current)
    assert report.has_extra_event is True
    assert report.uncovered_fact_ids == ("history:f2",)


def test_extra_event_no_more_facts_returns_clean():
    """同 Fact 基数 → 不报 uncovered。"""
    history = {"record_id": "a" * 64, "facts": [{"fact_id": "f1"}], "text": "x"}
    current = {"record_id": "b" * 64, "facts": [{"fact_id": "f1"}], "text": "x"}
    report = detect_extra_event(history, current)
    assert report.has_extra_event is False
    assert report.uncovered_fact_ids == ()


# ---------- 标尺 2：DTO 封门（PairResult → 公共 dict 序列化） ----------

def test_decide_outcome_to_public_dict_field_closure():
    """DecideOutcome.to_public_dict() 恰五字段封闭（07 §5/11 §1）；不含内部码。"""
    outcome = decide_types.DecideOutcome(
        item_id="news-003",
        text="甲公司宣布完成回购。",
        decision="重复",
        duplicate_ids=("news-001", "news-002"),
        reason="已逐一核验与列表中的条目主体和回购事件一致，仅为表达方式不同。",
        internal_code="FACT_EQUIVALENT",
        pair_codes={"news-001": "FACT_EQUIVALENT", "news-002": "EXACT_TEXT_MATCH"},
        used_evidence=(),
        raw_hash="h",
        pipeline_version="dedup_v1",
    )
    pub = outcome.to_public_dict()
    assert set(pub.keys()) == {"item_id", "text", "decision",
                                "duplicate_ids", "reason"}
    assert pub["item_id"] == "news-003"
    assert pub["decision"] == "重复"
    assert pub["duplicate_ids"] == ["news-001", "news-002"]
    assert "FACT_EQUIVALENT" not in pub["reason"]
    assert "FACT_EQUIVALENT" not in str(pub)
    assert "EXACT_TEXT_MATCH" not in str(pub)


def test_decide_outcome_to_public_dict_strips_internal_fields():
    """to_public_dict() 不暴露 internal_code / pair_codes / used_evidence / raw_hash。"""
    outcome = decide_types.DecideOutcome(
        item_id="x", text="t",
        decision="不重复", duplicate_ids=(),
        reason="本次健康的限定召回中未发现可比较的重复候选。",
        internal_code="NO_DUPLICATE_FOUND",
        pair_codes={},
        used_evidence=(),
        raw_hash="h", pipeline_version="dedup_v1",
    )
    pub = outcome.to_public_dict()
    assert "internal_code" not in pub
    assert "pair_codes" not in pub
    assert "used_evidence" not in pub
    assert "raw_hash" not in pub
    assert "pipeline_version" not in pub


def test_decide_outcome_coupling_invariant():
    """决策与列表严格耦合：重复 ↔ 非空；其余两类 ↔ 空（INV-2 / INV-9）。"""
    with pytest.raises(ValueError):
        decide_types.DecideOutcome(
            item_id="x", text="t", decision="重复", duplicate_ids=(),
            reason="r", internal_code="c", pair_codes={}, used_evidence=(),
            raw_hash="h", pipeline_version="dedup_v1",
        )
    with pytest.raises(ValueError):
        decide_types.DecideOutcome(
            item_id="x", text="t", decision="不重复",
            duplicate_ids=("a",), reason="r", internal_code="c",
            pair_codes={}, used_evidence=(),
            raw_hash="h", pipeline_version="dedup_v1",
        )


# ---------- 标尺 3：编排层端到端（extra_event + 时间阻塞 + aggregate） ----------

def test_orchestration_pipeline_returns_decide_outcome():
    """decide_once 编排：P16-A → P15 → P16-B → extra_event → P16-C → DecideOutcome。"""
    history_text = "甲公司发布新手机。"
    current_text = "甲公司发布新手机，同时披露季度财报。"
    history = {
        "record_id": "a" * 64, "item_id": "item-A", "text": history_text,
        "raw_hash": __import__('hashlib').sha256(history_text.encode("utf-8")).hexdigest(),
        "scope_id": "default", "business_date": "2026-09-26",
        "arrival_seq": 1, "pipeline_version": "dedup_v1",
        "facts": [
            {"fact_id": "f1", "evidence": [{"record_id": "a" * 64, "field": "x",
                                            "quote": "甲", "start": 0, "end": 1}],
             "fact_type": {"status": "present", "raw_value": "发布",
                            "evidence": [{"record_id": "a" * 64, "field": "x",
                                           "quote": "甲", "start": 0, "end": 1}]},
             "subject": {"status": "present", "raw_value": "甲公司",
                         "evidence": [{"record_id": "a" * 64, "field": "x",
                                       "quote": "甲", "start": 0, "end": 1}]},
             "event_state": {"predicate": {"status": "present", "raw_value": "发布",
                                            "evidence": [{"record_id": "a" * 64, "field": "x",
                                                          "quote": "甲", "start": 0, "end": 1}]},
                            "polarity": {"status": "present", "raw_value": "发布",
                                          "evidence": [{"record_id": "a" * 64, "field": "x",
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
             "numerics": []},
        ],
    }
    current = {
        "record_id": "b" * 64, "item_id": "item-C", "text": current_text,
        "raw_hash": __import__('hashlib').sha256(current_text.encode("utf-8")).hexdigest(),
        "scope_id": "default", "business_date": "2026-09-26",
        "arrival_seq": 2, "pipeline_version": "dedup_v1",
        "facts": [
            {"fact_id": "f1", "evidence": [{"record_id": "b" * 64, "field": "x",
                                            "quote": "甲", "start": 0, "end": 1}],
             "fact_type": {"status": "present", "raw_value": "发布",
                            "evidence": [{"record_id": "b" * 64, "field": "x",
                                           "quote": "甲", "start": 0, "end": 1}]},
             "subject": {"status": "present", "raw_value": "甲公司",
                         "evidence": [{"record_id": "b" * 64, "field": "x",
                                       "quote": "甲", "start": 0, "end": 1}]},
             "event_state": {"predicate": {"status": "present", "raw_value": "发布",
                                            "evidence": [{"record_id": "b" * 64, "field": "x",
                                                          "quote": "甲", "start": 0, "end": 1}]},
                            "polarity": {"status": "present", "raw_value": "发布",
                                          "evidence": [{"record_id": "b" * 64, "field": "x",
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
             "numerics": []},
            {"fact_id": "f2", "evidence": [{"record_id": "b" * 64, "field": "x",
                                            "quote": "甲", "start": 0, "end": 1}],
             "fact_type": {"status": "present", "raw_value": "披露",
                            "evidence": [{"record_id": "b" * 64, "field": "x",
                                           "quote": "甲", "start": 0, "end": 1}]},
             "subject": {"status": "present", "raw_value": "甲公司",
                         "evidence": [{"record_id": "b" * 64, "field": "x",
                                       "quote": "甲", "start": 0, "end": 1}]},
             "event_state": {"predicate": {"status": "present", "raw_value": "披露",
                                            "evidence": [{"record_id": "b" * 64, "field": "x",
                                                          "quote": "甲", "start": 0, "end": 1}]},
                            "polarity": {"status": "present", "raw_value": "披露",
                                          "evidence": [{"record_id": "b" * 64, "field": "x",
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
             "numerics": []},
        ],
    }
    outcome = decide_service.decide_once(history, current, coverage_complete=True)
    assert outcome.decision == "边界case/疑难case"
    assert outcome.duplicate_ids == ()
    # extra_event 必须被检测到——W2Fε 收紧（winj:92-100 同款双硬钉模式）：
    # 原 OR 弱钉任一侧恒真即绿、单侧退化不红；探针实测（.venv-v1 驱动
    # decide_once）两侧同时成立，改双硬钉各守一侧
    assert "current:f2" in outcome.unresolved_fields
    assert outcome.internal_code == "RULE_UNCOVERED"


def test_orchestration_repeated_match_unresolved_on_time_dict():
    """同文/同 Fact 复测 → 因 P15 时间字典未实装走 unresolved，等价阻塞 boundary。

    临时命名直到时间字典实装；测试断言当前已知行为（boundary）而非 quality 结论。"""
    text = "甲公司发布新手机。"
    history = {
        "record_id": "a" * 64, "item_id": "item-A", "text": text,
        "raw_hash": __import__('hashlib').sha256(text.encode("utf-8")).hexdigest(),
        "scope_id": "default", "business_date": "2026-09-26",
        "arrival_seq": 1, "pipeline_version": "dedup_v1",
        "facts": [
            {"fact_id": "f1", "evidence": [{"record_id": "a" * 64, "field": "x",
                                            "quote": "甲", "start": 0, "end": 1}],
             "fact_type": {"status": "present", "raw_value": "发布",
                            "evidence": [{"record_id": "a" * 64, "field": "x",
                                           "quote": "甲", "start": 0, "end": 1}]},
             "subject": {"status": "present", "raw_value": "甲公司",
                         "evidence": [{"record_id": "a" * 64, "field": "x",
                                       "quote": "甲", "start": 0, "end": 1}]},
             "event_state": {"predicate": {"status": "present", "raw_value": "发布",
                                            "evidence": [{"record_id": "a" * 64, "field": "x",
                                                          "quote": "甲", "start": 0, "end": 1}]},
                            "polarity": {"status": "present", "raw_value": "发布",
                                          "evidence": [{"record_id": "a" * 64, "field": "x",
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
             "numerics": []},
        ],
    }
    current = deepcopy(history)
    current["record_id"] = "b" * 64
    current["item_id"] = "item-C"
    current["arrival_seq"] = 2
    # 把所有 evidence 的 record_id 改为当前 record_id（深拷贝后 history evidence 仍是 "a" * 64）
    def _rewrite_evidence(node, rid):
        if isinstance(node, dict):
            if "evidence" in node and isinstance(node["evidence"], list):
                for entry in node["evidence"]:
                    if isinstance(entry, dict) and "record_id" in entry:
                        entry["record_id"] = rid
            for v in node.values():
                _rewrite_evidence(v, rid)
        elif isinstance(node, list):
            for item in node:
                _rewrite_evidence(item, rid)
    _rewrite_evidence(current["facts"], "b" * 64)
    outcome = decide_service.decide_once(history, current, coverage_complete=True)
    # 窗口K 收紧（D30 M-14）：原 `decision in {"重复","边界case/疑难case"}` 二选一
    # 宽收；实测（N27 后态 uat054137）恒为 边界case/疑难case（时间槽未命中型
    # missing 记 unknown 归未决集 → FACT_INCOMPLETE 阻塞等价），收紧为等值钉
    # + 内部码钉值；后续链站合法翻转须按站纪律重钉+机制说明。
    assert outcome.decision == "边界case/疑难case"
    assert outcome.internal_code == "FACT_INCOMPLETE"
    assert outcome.duplicate_ids == ()

def test_used_evidence_is_evidence_refs_not_pair_codes():
    """13:50 R3 补点名：used_evidence 是 EvidenceRef 元组，不是 pair_code 字符串。

    案三a 整改后：used_evidence 必须来自 aggregate_outcome.used_evidence（真 EvidenceRef 列表）。
    旧实现曾误用 aggregate_outcome.pair_codes（字符串 dict）— 不再出现。
    """
    import sys
    from pathlib import Path
    # W2Fε 修复：原硬编码绝对路径仅本机可达——改 __file__ 派生（本文件目录）
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from news_flash_dedup.decide.types import DecideOutcome
    from news_flash_dedup.compare.aggregate import AggregateOutcome
    from news_flash_dedup.compare.value_time import EvidenceRef

    # 构造决定 Outcome（走完 decide_once → DecideOutcome.used_evidence 为 EvidenceRef 元组）
    # 此处仅测 DecideOutcome 字段定义 + 序列化
    o = DecideOutcome(
        item_id="item-C", text="t", decision="重复",
        duplicate_ids=("r-0",), reason="stub",
        internal_code="FACT_EQUIVALENT",
        pair_codes={"r-0": "FACT_EQUIVALENT"},
        used_evidence=(EvidenceRef("a"*64, "facts.f1.subject", "甲", 0, 1),),
        unresolved_fields=(),
        raw_hash="h", pipeline_version="dedup_v1",
    )
    pub = o.to_public_dict()
    assert set(pub.keys()) == {"item_id", "text", "decision",
                                  "duplicate_ids", "reason"}
    # used_evidence 字段在完整 dataclass 而非公开 dict
    assert all(isinstance(er, EvidenceRef) for er in o.used_evidence)
    assert all(not isinstance(er, str) for er in o.used_evidence), (
        "used_evidence 不能是字符串（pair_code 顶替已废）"
    )


def test_used_evidence_from_real_decide_pipeline():
    """D24 驱动层补强：上测试仅构造 DTO（对 R3 点名 bug 无红能力——
    decide 真回填错绑 pair_codes 时它仍绿）。本测试真实驱动
    decide_for_task：used_evidence 必须非空、全为 EvidenceRef、
    record_id 属于参与对，且不得出现字符串（pair_code 顶替即红）。"""
    # 跨测试模块夹具复用（W2Fε 注记）：pytest 无 __init__.py 时按文件目录
    # 入 sys.path（rootdir 直跑成立）；上方 :309-310 的 sys.path.insert 亦保
    # 跨目录/打包形态下本导入可解析——两机制任一成立即绿
    from test_p17_2_commit_one import _ctx_for, _text_with_facts  # noqa: E402
    from news_flash_dedup.compare.value_time import EvidenceRef as ER
    from news_flash_dedup.decide import service as decide_service

    current = _ctx_for("甲公司完成回购。", "c" * 64, "item-C", 10)
    history = _ctx_for("甲公司完成回购。", "a" * 64, "item-A", 9)
    # E 批 H-04 F1 夹具升级（e-batch-design §3.3 F1 行+§3.4 第 4 条）：
    # _text_with_facts field='x' 伪证据→校验可过形态（正规槽位路径+全文
    # 覆盖）；断言面（重复/used_evidence 型钉）零改动。
    from test_e_h04_facts_projection import _complete_fact
    current["facts"] = [_complete_fact("c" * 64, "甲公司完成回购。")]
    history["facts"] = [_complete_fact("a" * 64, "甲公司完成回购。")]
    # N29/D28 注：时间槽补真 P14"确无"verified 形态，维持 EXACT 证书路径
    # （未命中型 missing 现记 unknown → 边界；语义钉值见 test_n29_honest_missing）。
    for ctx in (current, history):
        ctx["facts"][0]["time"]["expression"] = {
            "status": "missing", "raw_value": None, "evidence": [],
            "verified_missing": True}
    outcome = decide_service.decide_for_task(
        history=history, candidates=(), current=current,
        pipeline_version="dedup_v1", coverage_complete=True,
        visible_seq=10, prepared_seq=10,
    )
    assert outcome.decision == "重复"
    assert outcome.used_evidence, "真管线 used_evidence 不得为空"
    assert all(isinstance(er, ER) for er in outcome.used_evidence)
    assert all(not isinstance(er, str) for er in outcome.used_evidence), (
        "used_evidence 混入字符串（pair_code 顶替复活）"
    )
    assert all(
        er.record_id in {"a" * 64, "c" * 64}
        for er in outcome.used_evidence
    )
