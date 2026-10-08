"""W2Fα1 条目26（WA1a D 族低危批）：D2-D8 红测。

- D1【未修，探针+冲突证据在案】pair_alignment F-K1 present 槽空 evidence
  无锚过闸：金标三腿探针零触发（log\temp\winw2fa1-probe-d1-fk1.json：
  rule v1 0/1197、v2 0/2071、llm 0/3006），但任何拒绝/降级形态均与 15 枚
  **禁改**既有钉的合成填充契约正面冲突（winz1×3+winw1fa×2 以 raw∈fact
  引文的空 evidence 槽作便利填充；p16_d_matrix×10 进一步构造 raw 不落
  fact 引文的空 evidence present 槽且为合法输入）。判责纪律禁改既有测试
  → 回滚登记，呈主窗口裁定（改既有钉 vs 放弃闸）。
- D2 pair_compare VerifiedConflict 证据切片先于类型校验 → 类型闸（PairBindingError）；
- D3 p15_integration zip 截断吞 numerics 数量差 → issue 化（FACT_INCOMPLETE）；
- D4 pair_compare 时间冲突证据恒取 aligned_facts[0] → 取当事对（TimePairComparison
  实传证据，旧式构造回退 aligned_facts[0] 契约零漂移）；
- D5 llm._parse_llm_json 静默丢非 dict 条目 → 计数告警；
- D7 facts/core 无界缓存有界化 + llm 缓存原子写 + llm 确定性失败（坏 JSON）不误重试；
- D8 llm 主体搜索窗 pred_start==0 退化全文末命中 → 空窗走谓词后兜底
  （探针 log\temp\winw2fa1-probe-d8-subject.json：退化形态金标零触发；任务书
  原拟"子句局域"修法被探针证伪——177/2634 跨子句合法主体会被洗 missing，
  不实施，现役"谓词前最近命中"语义钉死保留）。
"""

from __future__ import annotations

import json
import logging
import os

import pytest

from news_flash_dedup.compare import pair_alignment, p15_integration, value_time
from news_flash_dedup.compare.pair_compare import (
    PairBindingError,
    P15PairResults,
    TimePairComparison,
    VerifiedConflict,
    compare_pair,
)
from news_flash_dedup.facts import FactValidationReport
from news_flash_dedup.facts import core as facts_core
from news_flash_dedup.facts import llm as facts_llm

RID_H = "a" * 64
RID_C = "b" * 64
H_TEXT = "甲公司回购5元。"
C_TEXT = "甲公司回购5元7元。"


def _sha(text):
    import hashlib
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _binding(record_id, item_id, text, seq):
    return {"record_id": record_id, "item_id": item_id, "text": text,
            "raw_hash": _sha(text), "scope_id": "s", "business_date": "2026-09-28",
            "arrival_seq": seq, "pipeline_version": "dedup_v1"}


def _ev_ref(record_id, field, text, quote, occurrence=0):
    start = -1
    for _ in range(occurrence + 1):
        start = text.index(quote, start + 1)
    return value_time.EvidenceRef(record_id=record_id, field=field, quote=quote,
                                  start=start, end=start + len(quote))


def _report(record_id, artifact):
    return FactValidationReport(
        record_id=record_id, validated_complete=False,
        extraction_status="complete", valid_fact_ids=(), numeric_inventory=(),
        issues=(), artifact=artifact)


def _alignment_empty():
    return pair_alignment.PairAlignmentOutcome(
        aligned_facts=(), unresolved_fields=(), code="RULE_UNCOVERED",
        provenance={})


# ---------- D2：VerifiedConflict 证据切片先于类型校验 → PairBindingError ----------

# 09-29 W3a-F7④：补 bool 形态一参——生产闸 pair_compare.py:303-304 显式
# 排 bool（isinstance bool 先于 int 子类陷阱），原参数化 str/None/float 三
# 形态未覆该腿（既有闸内细则，非修复缺口，补钉守闸）。
@pytest.mark.parametrize("bad_start,bad_end", [("0", 1), (0, "1"), (None, 1), (0.0, 1), (True, 1)])
def test_d2_conflict_evidence_span_type_guard(bad_start, bad_end):
    history = _binding(RID_H, "A", H_TEXT, 1)
    current = _binding(RID_C, "C", C_TEXT, 2)
    conflict = VerifiedConflict(
        field_path="facts.f1.numerics.n1.value", basis="NUMERIC_SAME_DECIMAL",
        history_evidence=value_time.EvidenceRef(
            record_id=RID_H, field="facts.f1.numerics.n1.value", quote="5",
            start=bad_start, end=bad_end),
        current_evidence=_ev_ref(RID_C, "facts.f1.numerics.n1.value", C_TEXT, "5"),
        detail="5 vs 5")
    with pytest.raises(PairBindingError):
        compare_pair(history, current,
                     history_artifact=_report(RID_H, {}),
                     current_artifact=_report(RID_C, {}),
                     alignment=_alignment_empty(),
                     p15_results=P15PairResults(verified_conflicts=(conflict,)))


def test_d2_valid_conflict_evidence_passes():
    """守卫：合法证据不过闸不误伤（修复前后同绿）。"""
    history = _binding(RID_H, "A", H_TEXT, 1)
    current = _binding(RID_C, "C", C_TEXT, 2)
    conflict = VerifiedConflict(
        field_path="facts.f1.numerics.n1.value", basis="NUMERIC_SAME_DECIMAL",
        history_evidence=_ev_ref(RID_H, "facts.f1.numerics.n1.value", H_TEXT, "5"),
        current_evidence=_ev_ref(RID_C, "facts.f1.numerics.n1.value", C_TEXT, "5"),
        detail="5 vs 5")
    result = compare_pair(history, current,
                          history_artifact=_report(RID_H, {}),
                          current_artifact=_report(RID_C, {}),
                          alignment=_alignment_empty(),
                          p15_results=P15PairResults(verified_conflicts=(conflict,)))
    assert result.outcome == "conflict"


# ---------- D3：zip 截断吞 numerics 数量差 → FACT_INCOMPLETE issue ----------

def _numeric_entry(record_id, fact_id, numeric_id, text, raw, *, unit="元"):
    nbase = f"facts.{fact_id}.numerics.{numeric_id}"
    start = text.index(raw)
    return {
        "numeric_id": numeric_id,
        "evidence": [{"record_id": record_id, "field": nbase, "quote": raw,
                      "start": start, "end": start + len(raw)}],
        "metric": {"status": "missing", "raw_value": None, "evidence": []},
        "value": {"status": "present", "raw_value": raw,
                  "evidence": [{"record_id": record_id,
                                "field": f"{nbase}.value", "quote": raw,
                                "start": start, "end": start + len(raw)}],
                  "value": raw, "unit": unit, "magnitude": "", "currency": None,
                  "role": None, "metric": None, "comparator": "=",
                  "approximate": False, "range_end": None},
        "range_end": {"status": "missing", "raw_value": None, "evidence": []},
        "magnitude": {"status": "missing", "raw_value": None, "evidence": []},
        "unit": {"status": "present", "raw_value": unit,
                 "evidence": [{"record_id": record_id,
                               "field": f"{nbase}.unit", "quote": unit,
                               "start": start + len(raw),
                               "end": start + len(raw) + len(unit)}]},
        "currency": {"status": "missing", "raw_value": None, "evidence": []},
        "role": {"status": "missing", "raw_value": None, "evidence": []},
        "comparator": {"status": "missing", "raw_value": None, "evidence": []},
        "direction": {"status": "missing", "raw_value": None, "evidence": []},
        "time": {"status": "missing", "raw_value": None, "evidence": []},
    }


def _fact_with_numerics(record_id, fact_id, text, numerics):
    base = f"facts.{fact_id}"
    subj_start = text.index("甲公司")
    pred_quote = "回购"
    pred_start = text.index(pred_quote)
    missing = {"status": "missing", "raw_value": None, "evidence": []}
    present = lambda quote, start, field: {
        "status": "present", "raw_value": quote,
        "evidence": [{"record_id": record_id, "field": field, "quote": quote,
                      "start": start, "end": start + len(quote)}]}
    return {
        "fact_id": fact_id,
        "evidence": [{"record_id": record_id, "field": base, "quote": text,
                      "start": 0, "end": len(text)}],
        "fact_type": present(pred_quote, pred_start, f"{base}.fact_type"),
        "subject": present("甲公司", subj_start, f"{base}.subject"),
        "event_state": {
            "predicate": present(pred_quote, pred_start,
                                 f"{base}.event_state.predicate"),
            "polarity": present(pred_quote, pred_start,
                                f"{base}.event_state.polarity"),
            "modality": dict(missing), "attribution": dict(missing),
        },
        "time": {"expression": dict(missing), "stage": dict(missing),
                 "anchor": dict(missing)},
        "key_object": dict(missing),
        "numerics": numerics,
    }


def _artifact(record_id, facts):
    return {"schema_version": "1.0", "record_id": record_id,
            "offset_unit": "unicode_code_point", "extraction_status": "complete",
            "facts": facts, "unparsed_spans": [], "uncertainties": []}


def _pointer(record_id, fact_id, text):
    return pair_alignment.FactPointer(
        record_id=record_id, fact_id=fact_id, slot_path=f"facts.{fact_id}",
        text=text,
        evidence=_ev_ref(record_id, f"facts.{fact_id}", text, text),
        subject_raw="甲公司", predicate_raw="回购", key_object_raw=None)


def test_d3_numerics_count_mismatch_booked_as_issue():
    """双侧 numerics 数量不等（1 vs 2）——修复前 zip 静默截断零留痕；
    修复后记 FACT_INCOMPLETE issue。"""
    h_fact = _fact_with_numerics(
        RID_H, "f1", H_TEXT,
        [_numeric_entry(RID_H, "f1", "n1", H_TEXT, "5")])
    c_fact = _fact_with_numerics(
        RID_C, "f1", C_TEXT,
        [_numeric_entry(RID_C, "f1", "n1", C_TEXT, "5"),
         _numeric_entry(RID_C, "f1", "n2", C_TEXT, "7")])
    alignment = pair_alignment.PairAlignmentOutcome(
        aligned_facts=(pair_alignment.AlignedPair(
            _pointer(RID_H, "f1", H_TEXT), _pointer(RID_C, "f1", C_TEXT),
            basis="FACT_EQUIVALENT"),),
        unresolved_fields=(), code="FACT_EQUIVALENT", provenance={})
    report = p15_integration.extract_p15_results(
        _report(RID_H, _artifact(RID_H, [h_fact])),
        _report(RID_C, _artifact(RID_C, [c_fact])),
        H_TEXT, C_TEXT, alignment)
    mismatch = [i for i in report.p15_results.issues
                if i.code == "FACT_INCOMPLETE" and "numerics" in i.detail]
    assert mismatch, "双侧 numerics 数量差必须 issue 化（zip 截断不得静默）"


def test_d3_equal_numerics_count_no_new_issue():
    """守卫：双侧 numerics 数量相等不产新 issue（修复前后同绿）。"""
    h_fact = _fact_with_numerics(
        RID_H, "f1", H_TEXT,
        [_numeric_entry(RID_H, "f1", "n1", H_TEXT, "5")])
    c_fact = _fact_with_numerics(
        RID_C, "f1", "甲公司回购5元。",
        [_numeric_entry(RID_C, "f1", "n1", "甲公司回购5元。", "5")])
    alignment = pair_alignment.PairAlignmentOutcome(
        aligned_facts=(pair_alignment.AlignedPair(
            _pointer(RID_H, "f1", H_TEXT), _pointer(RID_C, "f1", "甲公司回购5元。"),
            basis="FACT_EQUIVALENT"),),
        unresolved_fields=(), code="FACT_EQUIVALENT", provenance={})
    report = p15_integration.extract_p15_results(
        _report(RID_H, _artifact(RID_H, [h_fact])),
        _report(RID_C, _artifact(RID_C, [c_fact])),
        H_TEXT, "甲公司回购5元。", alignment)
    assert not [i for i in report.p15_results.issues
                if i.code == "FACT_INCOMPLETE" and "numerics" in i.detail]


# ---------- D4：时间冲突证据取当事对（TimePairComparison 实传） ----------

def _time_conflict_pair_result(time_pair):
    history = _binding(RID_H, "A", H_TEXT, 1)
    current = _binding(RID_C, "C", C_TEXT, 2)
    ptr_h1 = _pointer(RID_H, "f1", H_TEXT)
    ptr_c1 = _pointer(RID_C, "f1", C_TEXT)
    ptr_h2 = pair_alignment.FactPointer(
        record_id=RID_H, fact_id="f2", slot_path="facts.f2", text=H_TEXT,
        evidence=_ev_ref(RID_H, "facts.f2", H_TEXT, "回购"),
        subject_raw="甲公司", predicate_raw="回购", key_object_raw=None)
    ptr_c2 = pair_alignment.FactPointer(
        record_id=RID_C, fact_id="f2", slot_path="facts.f2", text=C_TEXT,
        evidence=_ev_ref(RID_C, "facts.f2", C_TEXT, "回购"),
        subject_raw="甲公司", predicate_raw="回购", key_object_raw=None)
    alignment = pair_alignment.PairAlignmentOutcome(
        aligned_facts=(pair_alignment.AlignedPair(ptr_h1, ptr_c1,
                                                  basis="FACT_EQUIVALENT"),
                       pair_alignment.AlignedPair(ptr_h2, ptr_c2,
                                                  basis="FACT_EQUIVALENT")),
        unresolved_fields=(), code="FACT_EQUIVALENT", provenance={})
    return compare_pair(
        history, current,
        history_artifact=_report(RID_H, {}), current_artifact=_report(RID_C, {}),
        alignment=alignment,
        p15_results=P15PairResults(time_pairs=(time_pair,))), ptr_h2, ptr_c2


def test_d4_time_conflict_uses_owning_pair_evidence():
    """时间冲突属于第 2 对齐对时，VerifiedConflict 证据必须取当事对
    （修复前恒取 aligned_facts[0] 张冠李戴）。"""
    ptr_h2_ev = _ev_ref(RID_H, "facts.f2", H_TEXT, "回购")
    ptr_c2_ev = _ev_ref(RID_C, "facts.f2", C_TEXT, "回购")
    time_pair = TimePairComparison(
        "conflict", "VERIFIED_CONFLICT", "已验证时间冲突",
        basis="TIME_SAME_RELATIVE",
        history_evidence=ptr_h2_ev, current_evidence=ptr_c2_ev)
    result, ptr_h2, _ = _time_conflict_pair_result(time_pair)
    assert result.outcome == "conflict"
    assert result.verified_conflicts[0].history_evidence == ptr_h2.evidence
    assert result.verified_conflicts[0].history_evidence.field == "facts.f2"


def test_d4_legacy_time_conflict_falls_back_to_first_aligned():
    """守卫：旧式构造未携证据 → 回退 aligned_facts[0]（现役契约零漂移，前后同绿）。"""
    time_pair = TimePairComparison("conflict", "VERIFIED_CONFLICT", "时间冲突",
                                   basis="TIME_SAME_RELATIVE")
    result, _, _ = _time_conflict_pair_result(time_pair)
    assert result.outcome == "conflict"
    assert result.verified_conflicts[0].history_evidence.field == "facts.f1"


def test_d4_carried_evidence_binding_validated():
    """实传证据 record_id 与 history 不符 → PairBindingError（fail-closed）。"""
    rogue_ev = value_time.EvidenceRef(record_id="f" * 64, field="facts.f2",
                                      quote="回购", start=3, end=5)
    ptr_c2_ev = _ev_ref(RID_C, "facts.f2", C_TEXT, "回购")
    time_pair = TimePairComparison(
        "conflict", "VERIFIED_CONFLICT", "已验证时间冲突",
        basis="TIME_SAME_RELATIVE",
        history_evidence=rogue_ev, current_evidence=ptr_c2_ev)
    with pytest.raises(PairBindingError):
        _time_conflict_pair_result(time_pair)


# ---------- D5：LLM facts 非 dict 条目丢弃计数告警 ----------

def test_d5_non_dict_facts_entries_counted_and_warned(caplog):
    content = '{"facts": [{"predicate": "回购"}, 42, "ghost", null]}'
    with caplog.at_level(logging.WARNING, logger="news_flash_dedup.facts.llm"):
        facts = facts_llm._parse_llm_json(content)
    assert facts == [{"predicate": "回购"}]
    assert any("3" in rec.getMessage() for rec in caplog.records), \
        "丢弃的非 dict 条目数必须入告警"


def test_d5_all_dict_facts_no_warning(caplog):
    """守卫：全 dict 列表零告警（前后同绿）。"""
    with caplog.at_level(logging.WARNING, logger="news_flash_dedup.facts.llm"):
        facts = facts_llm._parse_llm_json('{"facts": [{"predicate": "回购"}]}')
    assert facts == [{"predicate": "回购"}]
    assert not caplog.records


# ---------- D7①：facts/core 抽取缓存有界化 ----------

class _StubModel:
    def __init__(self):
        self.calls = 0

    def extract(self, request):
        self.calls += 1
        raise RuntimeError("deterministic stub failure")


def test_d7_cache_bounded_fifo():
    """cache_max_entries=2：第 3 文入场逐出最旧条目（修复前无界增长）。"""
    model = _StubModel()
    service = facts_core.FactExtractionService(
        model, extraction_artifact_version="v1", cache_max_entries=2)
    for text in ("正文一", "正文二", "正文三"):
        service.extract_once("a" * 64, text)
    assert len(service._cache) == 2
    assert model.calls == 3
    # 最旧条目已逐出 → 重取"正文一"触发重算
    service.extract_once("a" * 64, "正文一")
    assert model.calls == 4
    # 命中路径：重复"正文一"不重算
    service.extract_once("a" * 64, "正文一")
    assert model.calls == 4


def test_d7_cache_default_unbounded_semantics_kept():
    """守卫：默认容量下常规三文全缓存命中不重算（前后同绿）。"""
    model = _StubModel()
    service = facts_core.FactExtractionService(model, extraction_artifact_version="v1")
    for text in ("正文一", "正文二", "正文一"):
        service.extract_once("a" * 64, text)
    assert model.calls == 2
    assert len(service._cache) == 2


# ---------- D7②：LLM 磁盘缓存原子写 ----------

def _one_fact_call_fn(**_kwargs):
    return ('{"facts": [{"predicate": "宣布", "subject": "甲公司", '
            '"polarity": null, "modality": null, "key_object": null, '
            '"time_expression": null, "time_stage": null, "attribution": null, '
            '"numerics": []}]}', 0.01)


def test_d7_llm_cache_write_is_atomic(tmp_path, monkeypatch):
    """缓存落盘必经 tmp+os.replace（修复前裸 write_text 非原子）。"""
    replaced = []
    real_replace = os.replace

    def _spy(src, dst):
        replaced.append((os.fspath(src), os.fspath(dst)))
        return real_replace(src, dst)

    monkeypatch.setattr(facts_llm.os, "replace", _spy)
    facts = facts_llm.extract_facts_llm(RID_H, "甲公司宣布回购。",
                                        call_fn=_one_fact_call_fn,
                                        cache_dir=str(tmp_path))
    assert facts
    assert replaced, "os.replace 必须被调用（原子写）"
    dst = replaced[0][1]
    assert dst.endswith(".json") and not dst.endswith(".tmp")
    assert json.loads((tmp_path / os.path.basename(dst)).read_text("utf-8"))
    assert not list(tmp_path.glob("*.tmp")), "临时文件不得残留"


# ---------- D7③：LLM 确定性失败（坏 JSON）不误重试 ----------

class _FakeResp:
    def __init__(self, body: bytes):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _n=-1):
        return self._body


def test_d7_invalid_json_body_raises_llm_extraction_error(monkeypatch):
    """chat 响应体坏 JSON = 确定性失败 → LlmExtractionError（修复前裸 JSONDecodeError
    被当瞬时错误重试）。"""
    monkeypatch.setattr(facts_llm.urllib.request, "urlopen",
                        lambda _req, timeout: _FakeResp(b"not-json{{{"))
    with pytest.raises(facts_llm.LlmExtractionError):
        facts_llm._default_call_fn(model="m", base_url="http://x", api_key="k",
                                   text="t", timeout_s=1)


def test_d7_llm_extraction_error_not_retried():
    """守卫：LlmExtractionError 不重试（现役纪律，前后同绿）。"""
    calls = {"n": 0}

    def _fn(**_kwargs):
        calls["n"] += 1
        raise facts_llm.LlmExtractionError("deterministic")

    with pytest.raises(facts_llm.LlmExtractionError):
        facts_llm._call_with_retries(_fn, retries=2)
    assert calls["n"] == 1


def test_d7_transient_error_still_retried():
    """守卫：瞬时错误维持重试（前后同绿）。"""
    calls = {"n": 0}

    def _fn(**_kwargs):
        calls["n"] += 1
        raise OSError("transient")

    with pytest.raises(facts_llm.LlmExtractionError):
        facts_llm._call_with_retries(_fn, retries=2)
    assert calls["n"] == 3


# ---------- D8（探针后补入）：pred_start==0 退化窗 → 空窗走谓词后兜底 ----------

def _d8_call_fn(predicate: str, subject: str):
    def _call_fn(**_kwargs):
        return ('{"facts": [{"predicate": "' + predicate + '", '
                '"subject": "' + subject + '", '
                '"polarity": null, "modality": null, "key_object": null, '
                '"time_expression": null, "time_stage": null, '
                '"attribution": null, "numerics": []}]}', 0.01)
    return _call_fn


def test_d8_predicate_at_text_start_no_fulltext_tail_hit():
    """谓词居文首（pred_start==0）：修复前 hi=None 退化全文窗取**末**命中
    （谓词之后亦可）；修复后空窗 → missing → 谓词后窗兜底 hits[0]=首命中。"""
    #                0123456789...
    text = "回购股份。甲公司称甲公司将继续。"
    facts = facts_llm.extract_facts_llm(
        RID_H, text, call_fn=_d8_call_fn("回购", "甲公司"))
    subject = facts[0]["subject"]
    assert subject["status"] == "present"          # 倒装兜底路径仍产出主体
    first = text.index("甲公司")
    assert subject["evidence"][0]["start"] == first     # 首命中（非全文末命中）
    assert subject["evidence"][0]["start"] != text.rindex("甲公司")


def test_d8_predicate_not_at_start_nearest_preceding_hit_unchanged():
    """守卫（前后同绿）：谓词非文首时窗内末命中=谓词前最近命中语义不变。"""
    text = "甲公司称将回购。甲公司继续。"
    facts = facts_llm.extract_facts_llm(
        RID_H, text, call_fn=_d8_call_fn("回购", "甲公司"))
    subject = facts[0]["subject"]
    assert subject["status"] == "present"
    assert subject["evidence"][0]["start"] == 0     # 谓词前窗内唯一命中


def test_d8_cross_clause_subject_preserved_per_probe_verdict():
    """守卫（探针裁定钉死）：主体仅见于前序子句时**保留**（子句局域修法
    已被探针证伪不实施——177/2634 金标主体依赖跨子句解析）。"""
    text = "甲公司上涨。公告称，业绩将持续向好。"
    facts = facts_llm.extract_facts_llm(
        RID_H, text, call_fn=_d8_call_fn("向好", "甲公司"))
    subjects = [f["subject"] for f in facts if f["subject"]["status"] == "present"]
    assert any(s["raw_value"] == "甲公司" for s in subjects), \
        "跨子句主体解析必须保留（探针实证其承重）"
