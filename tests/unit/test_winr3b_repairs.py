# -*- coding: utf-8 -*-
"""W-R3b 红测：R3-M2 LLM 供给缓存零保护（facts/llm.py）。

红能力（修复前原貌，逐件对应断言）：
- :536-546 缓存写位于零事实兜底（:547-549）之前——零可定位事实的抽取结果
  （全幻觉 raw / 诚实空 facts）被钉入磁盘缓存永久复用：二次调用零成本命中，
  幻觉 raw 再无重审机会（R3-M2 主诉）；
- :518-521 report_hook 命中分支缺 n_facts 键，与未命中分支（:550-553）不对称。
修复契约：
- 零可定位事实（兜底前 facts 为空）→ 不落缓存 + 告警计数
  （zero_facts_cache_skip_count 单调只增）+ 侧车 issue 登记 + 日志告警；
- 命中/未命中 report_hook 键集对称（命中分支补 n_facts）。
"""
from __future__ import annotations

import json

from news_flash_dedup.facts import llm as facts_llm

RECORD_A = "a" * 64

# 全幻觉 payload：两事实谓词均不可定位（同 test_facts_llm 既有全灭形态）→
# 零可定位事实 → B2 同款兜底。
HALLUCINATED_TEXT = "公司公告重大事项。"
HALLUCINATED_PAYLOAD = {"facts": [
    {"subject": None, "predicate": "不会产生影响", "numerics": []},
    {"subject": None, "predicate": None, "numerics": []},
]}
# 诚实空 payload：模型按提示词规则 5 判"无事件动词"——同样零可定位事实。
HONEST_EMPTY_PAYLOAD = {"facts": []}
# 可定位正路径（缓存应正常生效的绿守卫）。
LOCATABLE_TEXT = "甲公司今日宣布回购股份。"
LOCATABLE_PAYLOAD = {"facts": [{
    "subject": "甲公司", "predicate": "回购", "polarity": "回购",
    "modality": None, "key_object": None, "time_expression": "今日",
    "time_stage": None, "attribution": None, "numerics": []}]}


def _counting_fn(payload: dict, calls: list):
    def fn(**_kwargs):
        calls.append(1)
        return json.dumps(payload, ensure_ascii=False), 0.001
    return fn


def test_r3m2_hallucinated_zero_facts_not_cached_and_alarmed(tmp_path):
    """R3-M2 主诉：全幻觉 raw（零可定位事实）不落缓存 + 告警计数 + 重抽不钉死。"""
    calls: list[int] = []
    reports: list[dict] = []
    before = facts_llm.zero_facts_cache_skip_count()
    kwargs = dict(call_fn=_counting_fn(HALLUCINATED_PAYLOAD, calls),
                  cache_dir=str(tmp_path), report_hook=reports.append)
    facts1 = facts_llm.extract_facts_llm(RECORD_A, HALLUCINATED_TEXT, **kwargs)
    assert len(facts1) == 1                       # B2 同款兜底仍在（返回契约不动）
    assert facts1[0]["event_state"]["predicate"]["status"] == "missing"
    # 零保护：缓存文件不生成——幻觉 raw 不得钉入永久复用
    assert list(tmp_path.glob("*.json")) == [], (
        "零可定位事实抽取结果被钉入缓存（R3-M2 零保护缺失）")
    # 告警计数 +1，侧车 issue 同行登记
    assert facts_llm.zero_facts_cache_skip_count() - before == 1
    assert any("llm_zero_facts_cache_skip" in i for i in reports[0]["issues"])
    assert any("llm_empty_facts_fallback" in i for i in reports[0]["issues"])
    # 二次调用重抽（不钉死）：幻觉无永久复用面
    facts2 = facts_llm.extract_facts_llm(RECORD_A, HALLUCINATED_TEXT, **kwargs)
    assert len(calls) == 2
    assert json.dumps(facts1, sort_keys=True) == json.dumps(facts2, sort_keys=True)
    assert facts_llm.zero_facts_cache_skip_count() - before == 2


def test_r3m2_honest_empty_zero_facts_also_not_cached(tmp_path):
    """统一口径：诚实空 facts（规则 5 判无事件）同属零可定位事实，不落缓存。"""
    calls: list[int] = []
    before = facts_llm.zero_facts_cache_skip_count()
    kwargs = dict(call_fn=_counting_fn(HONEST_EMPTY_PAYLOAD, calls),
                  cache_dir=str(tmp_path))
    facts_llm.extract_facts_llm(RECORD_A, HALLUCINATED_TEXT, **kwargs)
    assert list(tmp_path.glob("*.json")) == []
    assert facts_llm.zero_facts_cache_skip_count() - before == 1
    facts_llm.extract_facts_llm(RECORD_A, HALLUCINATED_TEXT, **kwargs)
    assert len(calls) == 2                        # 诚实空同样不钉死


def test_r3m2_report_hook_hit_miss_keys_symmetric(tmp_path):
    """report_hook 命中/未命中分支键集对称（命中分支补 n_facts）。"""
    reports: list[dict] = []
    kwargs = dict(call_fn=_counting_fn(LOCATABLE_PAYLOAD, []),
                  cache_dir=str(tmp_path), report_hook=reports.append)
    facts_llm.extract_facts_llm(RECORD_A, LOCATABLE_TEXT, **kwargs)   # miss
    facts_llm.extract_facts_llm(RECORD_A, LOCATABLE_TEXT, **kwargs)   # hit
    assert len(reports) == 2
    miss_report, hit_report = reports
    assert miss_report["cache_hit"] is False
    assert hit_report["cache_hit"] is True
    assert set(hit_report) == set(miss_report), (
        f"命中/未命中键集不对称：hit-only={set(hit_report) - set(miss_report)} "
        f"miss-only={set(miss_report) - set(hit_report)}")
    assert hit_report["n_facts"] == miss_report["n_facts"] == 1
    assert hit_report["latency_s"] == 0.0         # 命中零延迟钉保持
    assert hit_report["text_sha256"] == miss_report["text_sha256"]


def test_r3m2_locatable_extraction_cached_without_alarm(tmp_path):
    """绿守卫：可定位正路径缓存照常（零保护不误伤）+ 告警计数不动。"""
    calls: list[int] = []
    reports: list[dict] = []
    before = facts_llm.zero_facts_cache_skip_count()
    kwargs = dict(call_fn=_counting_fn(LOCATABLE_PAYLOAD, calls),
                  cache_dir=str(tmp_path), report_hook=reports.append)
    facts_llm.extract_facts_llm(RECORD_A, LOCATABLE_TEXT, **kwargs)
    facts_llm.extract_facts_llm(RECORD_A, LOCATABLE_TEXT, **kwargs)
    assert len(calls) == 1                        # 第二次走缓存零调用（既有契约）
    assert len(list(tmp_path.glob("*.json"))) == 1
    assert facts_llm.zero_facts_cache_skip_count() == before   # 正路径零告警
    assert not any("llm_zero_facts_cache_skip" in i
                   for r in reports for i in r["issues"])
