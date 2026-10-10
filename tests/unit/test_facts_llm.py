"""LLM 式 P14 抽取器单元测试（主窗口 04:3x；全部 mock call_fn，零网络零费用）。"""

from __future__ import annotations

import json

import pytest

from news_flash_dedup.decide.service import _wrap_facts_as_report
from news_flash_dedup.compare import pair_alignment, p15_integration
from news_flash_dedup.facts import llm as facts_llm
from news_flash_dedup.facts.llm import LlmExtractionError

from tests.unit.test_facts_rule import assert_g1_evidence_real, assert_g2_shape

RECORD_A = "a" * 64
RECORD_B = "b" * 64


def _mock_fn(payload: dict, *, latency: float = 0.001):
    def fn(**_kwargs):
        return json.dumps(payload, ensure_ascii=False), latency
    return fn


def _extract(text: str, payload: dict, record_id: str = RECORD_A, **kwargs):
    facts = facts_llm.extract_facts_llm(
        record_id, text, call_fn=_mock_fn(payload), **kwargs)
    assert_g1_evidence_real(text, record_id, facts)
    assert_g2_shape(facts)
    return facts


BASIC_TEXT = "甲公司今日宣布回购股份100万股。"
BASIC_PAYLOAD = {"facts": [{
    "subject": "甲公司", "predicate": "回购", "polarity": "回购",
    "modality": None, "key_object": "股份", "time_expression": "今日",
    "time_stage": None, "attribution": None,
    "numerics": [{"value_raw": "100万", "metric": None, "unit": "股",
                  "magnitude": "万", "comparator": None,
                  "range_end_raw": None, "direction": None}],
}]}


def test_basic_extraction_shape():
    facts = _extract(BASIC_TEXT, BASIC_PAYLOAD)
    fact = facts[0]
    assert fact["subject"]["raw_value"] == "甲公司"
    assert fact["event_state"]["predicate"]["raw_value"] == "回购"
    assert fact["event_state"]["polarity"]["raw_value"] == "回购"
    assert fact["time"]["expression"]["raw_value"] == "今日"
    numeric = fact["numerics"][0]
    # P15 密封契约：槽 raw_value=裸数值核（"100"），量级/单位独立键携带
    assert numeric["value"]["raw_value"] == "100"
    assert numeric["value"]["value"] == "100"
    assert numeric["value"]["unit"] == "股"
    assert numeric["value"]["magnitude"] == "万"
    assert numeric["unit"]["status"] == "present"
    assert numeric["magnitude"]["raw_value"] == "万"


def test_hallucinated_quote_becomes_missing_with_issue():
    text = "甲公司完成回购。"
    payload = {"facts": [{
        "subject": "乙公司",                      # 幻觉引词：原文不存在
        "predicate": "回购", "polarity": None, "modality": None,
        "key_object": None, "time_expression": "今日",   # 今日也是幻觉
        "time_stage": None, "attribution": None, "numerics": []}]}
    issues: list[dict] = []
    facts = facts_llm.extract_facts_llm(
        RECORD_A, text, call_fn=_mock_fn(payload),
        report_hook=issues.append)
    assert facts[0]["subject"]["status"] == "missing"
    assert facts[0]["time"]["expression"]["status"] == "missing"
    hooked = issues[0]["issues"]
    assert any("乙公司" in i for i in hooked)
    assert any("今日" in i for i in hooked)
    assert_g1_evidence_real(text, RECORD_A, facts)   # 留下的 evidence 全真


def test_malformed_json_raises():
    with pytest.raises(LlmExtractionError):
        facts_llm.extract_facts_llm(
            RECORD_A, "甲公司完成回购。",
            call_fn=lambda **_k: ("这不是JSON", 0.001))


def test_json_fence_tolerated():
    def fn(**_k):
        return "```json\n" + json.dumps(BASIC_PAYLOAD, ensure_ascii=False) + "\n```", 0.001
    facts = facts_llm.extract_facts_llm(RECORD_A, BASIC_TEXT, call_fn=fn)
    assert facts[0]["event_state"]["predicate"]["raw_value"] == "回购"


def test_predicate_missing_or_unlocatable_drops_fact():
    text = "甲公司完成回购。"
    payload = {"facts": [
        {"subject": None, "predicate": None, "numerics": []},
        {"subject": None, "predicate": "增发", "numerics": []},   # 原文无"增发"
        {"subject": "甲公司", "predicate": "回购", "polarity": None,
         "modality": None, "key_object": None, "time_expression": None,
         "time_stage": None, "attribution": None, "numerics": []},
    ]}
    facts = _extract(text, payload)
    assert len(facts) == 1
    assert facts[0]["fact_id"] == "f1"           # 丢弃后重编号
    assert facts[0]["event_state"]["predicate"]["raw_value"] == "回购"


def test_polarity_without_predicate_falls_back():
    text = "甲公司完成回购。"
    payload = {"facts": [{
        "subject": "甲公司", "predicate": "回购",
        "polarity": "已经完成",                   # 不含谓词原词 → 回退谓词极性
        "modality": None, "key_object": None, "time_expression": None,
        "time_stage": None, "attribution": None, "numerics": []}]}
    facts = _extract(text, payload)
    polarity = facts[0]["event_state"]["polarity"]
    assert polarity["raw_value"] == "回购"


def test_negation_polarity_kept():
    text = "甲公司并未回购股份。"
    payload = {"facts": [{
        "subject": "甲公司", "predicate": "回购", "polarity": "并未回购",
        "modality": None, "key_object": "股份", "time_expression": None,
        "time_stage": None, "attribution": None, "numerics": []}]}
    facts = _extract(text, payload)
    assert facts[0]["event_state"]["polarity"]["raw_value"] == "并未回购"


def test_numeric_range_and_comparator():
    text = "甲公司减持100-200万股。"
    payload = {"facts": [{
        "subject": "甲公司", "predicate": "减持", "polarity": "减持",
        "modality": None, "key_object": None, "time_expression": None,
        "time_stage": None, "attribution": None,
        "numerics": [{"value_raw": "100", "metric": None, "unit": "股",
                      "magnitude": "万", "comparator": None,
                      "range_end_raw": "200", "direction": None}]}]}
    facts = _extract(text, payload)
    numeric = facts[0]["numerics"][0]
    assert numeric["value"]["value"] == "100"
    assert numeric["range_end"]["status"] == "present"
    assert numeric["range_end"]["value"] == "200"
    assert numeric["value"]["comparator"] == "range"


def test_hallucinated_unit_rejected_by_near_window_guard():
    """D24 三方审计回归：near_str 守卫曾逻辑反转——`or text.find(val) == -1`
    让全文不存在的幻觉串（如"万元"）反而通过入槽；单位/量级必须锚定
    数值核后窗，幻觉串归 None，真实近窗串保留。"""
    text = "甲公司减持100股。"
    base = {"subject": "甲公司", "predicate": "减持", "polarity": "减持",
            "modality": None, "key_object": None, "time_expression": None,
            "time_stage": None, "attribution": None}

    def _payload(unit):
        return {"facts": [dict(base, numerics=[
            {"value_raw": "100", "metric": None, "unit": unit,
             "magnitude": None, "comparator": None,
             "range_end_raw": None, "direction": None}])]}

    ghost = _extract(text, _payload("万元"))
    assert ghost[0]["numerics"][0]["value"]["unit"] is None  # 旧实现误收
    real = _extract(text, _payload("股"))
    assert real[0]["numerics"][0]["value"]["unit"] == "股"


def test_chinese_numeral_value_null():
    text = "甲公司回购一百万股。"
    payload = {"facts": [{
        "subject": "甲公司", "predicate": "回购", "polarity": None,
        "modality": None, "key_object": None, "time_expression": None,
        "time_stage": None, "attribution": None,
        "numerics": [{"value_raw": "一百万", "metric": None, "unit": "股",
                      "magnitude": None, "comparator": None,
                      "range_end_raw": None, "direction": None}]}]}
    facts = _extract(text, payload)
    numeric = facts[0]["numerics"][0]
    assert numeric["value"]["raw_value"] == "一百万"
    assert numeric["value"]["value"] is None     # 09 §7.2 授权：归一后置


def test_cache_hit_skips_call(tmp_path):
    calls: list[int] = []

    def counting_fn(**_k):
        calls.append(1)
        return json.dumps(BASIC_PAYLOAD, ensure_ascii=False), 0.001

    kwargs = dict(call_fn=counting_fn, cache_dir=str(tmp_path))
    facts1 = facts_llm.extract_facts_llm(RECORD_A, BASIC_TEXT, **kwargs)
    facts2 = facts_llm.extract_facts_llm(RECORD_A, BASIC_TEXT, **kwargs)
    assert len(calls) == 1                       # 第二次走缓存零调用
    assert json.dumps(facts1, sort_keys=True) == json.dumps(facts2, sort_keys=True)


def test_cache_rebuilds_facts_per_record_id(tmp_path):
    """同一文本不同 record_id（金标集文本复用场景）→ 缓存命中按请求方
    record_id 重建 facts（R2 84 条 PairAlignmentError 根因修复回归）。"""
    calls: list[int] = []

    def counting_fn(**_k):
        calls.append(1)
        return json.dumps(BASIC_PAYLOAD, ensure_ascii=False), 0.001

    kwargs = dict(call_fn=counting_fn, cache_dir=str(tmp_path))
    facts_a = facts_llm.extract_facts_llm(RECORD_A, BASIC_TEXT, **kwargs)
    facts_b = facts_llm.extract_facts_llm(RECORD_B, BASIC_TEXT, **kwargs)
    assert len(calls) == 1
    assert facts_a[0]["evidence"][0]["record_id"] == RECORD_A
    assert facts_b[0]["evidence"][0]["record_id"] == RECORD_B
    assert facts_b[0]["numerics"][0]["evidence"][0]["record_id"] == RECORD_B


def test_transient_retry_then_success():
    attempts: list[int] = []

    def flaky_fn(**_k):
        attempts.append(1)
        if len(attempts) < 3:
            raise TimeoutError("simulated transient")
        return json.dumps(BASIC_PAYLOAD, ensure_ascii=False), 0.001

    facts = facts_llm.extract_facts_llm(RECORD_A, BASIC_TEXT, call_fn=flaky_fn)
    assert len(attempts) == 3
    assert facts[0]["event_state"]["predicate"]["raw_value"] == "回购"


def test_persistent_failure_raises():
    def always_fail(**_k):
        raise ConnectionError("down")
    with pytest.raises(LlmExtractionError):
        facts_llm.extract_facts_llm(RECORD_A, BASIC_TEXT, call_fn=always_fail)


def test_empty_text_and_record_id():
    assert facts_llm.extract_facts_llm(RECORD_A, "", call_fn=_mock_fn({})) == []
    with pytest.raises(ValueError):
        facts_llm.extract_facts_llm("bad", "甲公司完成回购。", call_fn=_mock_fn({}))


def test_all_predicates_dropped_yields_fallback_fact():
    """LLM 谓词全部未命中 → B2 同款 fallback minimal fact（首子句 evidence +
    全 missing），杜绝 PairAlignmentError 技术失败（首轮回放 153 failures 根因）。"""
    text = "公司公告重大事项。"
    payload = {"facts": [
        {"subject": None, "predicate": "不会产生影响", "numerics": []},   # 未命中
        {"subject": None, "predicate": None, "numerics": []},
    ]}
    issues: list[dict] = []
    facts = facts_llm.extract_facts_llm(RECORD_A, text,
                                        call_fn=_mock_fn(payload),
                                        report_hook=issues.append)
    assert len(facts) == 1
    fact = facts[0]
    assert fact["fact_type"]["status"] == "missing"
    assert fact["event_state"]["predicate"]["status"] == "missing"
    assert fact["evidence"][0]["quote"] in text      # 首子句真实 span
    assert_g1_evidence_real(text, RECORD_A, facts)
    assert any("llm_empty_facts_fallback" in i for i in issues[0]["issues"])


def test_cached_empty_facts_also_fallback(tmp_path):
    """缓存零事实读取路径同样确定性兜底（命中路径语义不动）。

    【W-R3b 呈裁（R3-M2 配套）】原夹具经 miss 路径落盘空 facts 再命中；
    R3-M2 零保护起零可定位事实不再落缓存（新行为由 test_winr3b_repairs
    钉死），改写为直种遗产条目（修复前已落盘的零事实 raw）——命中路径
    兜底语义一字未动，断言全保留（calls==0 命中 / 双读同形 / 兜底）。
    """
    calls: list[int] = []

    def empty_fn(**_k):
        calls.append(1)
        return json.dumps({"facts": []}, ensure_ascii=False), 0.001

    text = "公司公告重大事项。"
    # 直种遗产缓存条目（R3-M2 前落盘形态：raw={"facts": []} + model/版本/摘要）
    # 开工令①b（2026-10-11 令2）：缓存键四元（+extractor_schema_version
    # 维）——直种条目须带 schema 维才命中（遗产三原条目=miss 重抽，
    # "schema 升版不错误复用旧 Fact"）；命中路径兜底语义一字未动。
    import hashlib as _hl
    text_hash = _hl.sha256(text.encode("utf-8")).hexdigest()
    (tmp_path / f"{text_hash}.json").write_text(json.dumps({
        "model": facts_llm.DEFAULT_MODEL,
        "prompt_version": facts_llm.LLM_PROMPT_VERSION,
        "extractor_schema_version": facts_llm.EXTRACTOR_SCHEMA_VERSION,
        "text_sha256": text_hash,
        "raw_llm_content": json.dumps({"facts": []}, ensure_ascii=False),
        "issues": [],
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    kwargs = dict(call_fn=empty_fn, cache_dir=str(tmp_path))
    facts1 = facts_llm.extract_facts_llm(RECORD_A, text, **kwargs)
    facts2 = facts_llm.extract_facts_llm(RECORD_A, text, **kwargs)
    assert len(calls) == 0                        # 遗产条目命中，零调用
    assert len(facts1) == len(facts2) == 1
    assert facts1[0]["event_state"]["predicate"]["status"] == "missing"


def test_llm_error_without_key_raises():
    import os
    saved = os.environ.pop("QWEN_API_KEY", None)
    try:
        with pytest.raises(LlmExtractionError):
            facts_llm.extract_facts_llm(RECORD_A, "甲公司完成回购。")
    finally:
        if saved is not None:
            os.environ["QWEN_API_KEY"] = saved


# ---------- 集成：LLM 语义归一的 recall 增益（B2 W1 弱项消解演示） ----------

def test_integration_paraphrase_synonym_predicate_aligns_and_equivalent():
    """B2 对齐不了的同义改写（宣布回购 vs 公告称回购），LLM 归一到同谓词
    → 对齐（recall 增益的单元级演示）。
    （N29/D28 重钉：c 侧 time_expression=None 系 LLM 未产出=未知而非"确无"
    → 单侧时间 unknown 进 time_pairs → FACT_EQUIVALENT 拒签发落边界；旧预期
    "九条件全过→签发→重复"系 missing 洗白链路，裁定废除。对齐增益本身
    不受影响，仍由下方 aligned_facts 断言钉守。）"""
    history_text = "甲公司今日宣布回购股份。"
    current_text = "甲公司公告称回购股份。"
    payload = {"facts": [{
        "subject": "甲公司", "predicate": "回购", "polarity": "回购",
        "modality": None, "key_object": "股份", "time_expression": None,
        "time_stage": None, "attribution": None, "numerics": []}]}
    h_payload = {"facts": [dict(payload["facts"][0], time_expression="今日")]}

    h_facts = facts_llm.extract_facts_llm(RECORD_A, history_text,
                                          call_fn=_mock_fn(h_payload))
    c_facts = facts_llm.extract_facts_llm(RECORD_B, current_text,
                                          call_fn=_mock_fn(payload))
    report_a = _wrap_facts_as_report(RECORD_A, history_text, h_facts)
    report_b = _wrap_facts_as_report(RECORD_B, current_text, c_facts)
    alignment = pair_alignment.build_aligned(
        report_a, report_b, history_text, current_text,
        dictionary_version="dict_v1", alignment_version="alignment_v1")
    assert alignment.aligned_facts, "LLM 归一同谓词后应对齐（B2 此例对齐不了）"
    p15 = p15_integration.extract_p15_results(
        report_a, report_b, history_text, current_text, alignment)
    assert p15.p15_results.time_pairs, "单侧时间未知必须进 time_pairs（N29）"
    assert p15.p15_results.equivalence_ready is False
    assert p15.p15_results.text_proof is None


def test_integration_conflicting_numbers_still_conflict():
    """LLM 供给下数值冲突仍按 VERIFIED_CONFLICT 拦截（签发壁第 i 条不豁免）。"""
    history_text = "甲公司减持100万股。"
    current_text = "甲公司减持200万股。"
    def payload(n):
        return {"facts": [{
            "subject": "甲公司", "predicate": "减持", "polarity": "减持",
            "modality": None, "key_object": None, "time_expression": None,
            "time_stage": None, "attribution": None,
            "numerics": [{"value_raw": n, "metric": None, "unit": "股",
                          "magnitude": "万", "comparator": None,
                          "range_end_raw": None, "direction": None}]}]}
    h_facts = facts_llm.extract_facts_llm(RECORD_A, history_text,
                                          call_fn=_mock_fn(payload("100万")))
    c_facts = facts_llm.extract_facts_llm(RECORD_B, current_text,
                                          call_fn=_mock_fn(payload("200万")))
    report_a = _wrap_facts_as_report(RECORD_A, history_text, h_facts)
    report_b = _wrap_facts_as_report(RECORD_B, current_text, c_facts)
    alignment = pair_alignment.build_aligned(
        report_a, report_b, history_text, current_text,
        dictionary_version="dict_v1", alignment_version="alignment_v1")
    p15 = p15_integration.extract_p15_results(
        report_a, report_b, history_text, current_text, alignment)
    assert p15.p15_results.verified_conflicts
    assert p15.p15_results.equivalence_ready is False
