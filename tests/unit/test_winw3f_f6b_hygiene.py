# -*- coding: utf-8 -*-
"""W3F 红测（W3a-F6②）：llm._numeric_slot 不可达分支卫生（E2 同款）。

缺陷面（facts/llm.py 现役）：
1. L248-251 `try: re_dec = _decimal_text(range_core.group(0)) except
   ValueError: re_dec = None`——_decimal_text（rule.py:863-865）仅
   `replace(",", "")`，**不抛 ValueError**（机制前提见
   test_f6b_decimal_text_never_raises_premise），except 腿结构性不可达；
   re_dec 恒非 None（中文核原样通过）。
2. L275-277 `range_core.group(0) if range_core is not None else None`
   回退腿——re_dec is None ⟺ range_core is None，回退腿同样结构性
   不可达（"中文核回退 range_core 原串"注记机制描述失准：中文核实由
   re_dec 路径产出，见 test_f6b_chinese_core_flows_through_decimal_text）。

修法（E2 同款拆除）：except 腿拆除（re_dec 直赋）；range_end 表达式
收口为 re_dec 单源（status==present 前提）；注记机制描述勘正。
既有钉 test_winw2fa1_c2_range_end.py 断言面不受影响（结果同形）。
"""

from __future__ import annotations

import inspect
import json

from news_flash_dedup.facts import llm as facts_llm
from news_flash_dedup.facts import rule as facts_rule

RID = "a" * 64


def _range_call_fn(range_end_raw: str, value_raw: str = "100"):
    def _call_fn(**_kwargs):
        return json.dumps({"facts": [{
            "predicate": "升至", "subject": "指数", "polarity": None,
            "modality": None, "key_object": None, "time_expression": None,
            "time_stage": None, "attribution": None,
            "numerics": [{"value_raw": value_raw, "metric": None,
                          "unit": "点", "magnitude": None, "comparator": None,
                          "range_end_raw": range_end_raw, "direction": None}]}]},
            ensure_ascii=False), 0.01
    return _call_fn


# ---------- 红测（源码面机制钉：不可达腿存在即红） ----------

def test_f6b_dead_except_valueerror_leg_removed():
    """机制钉：_numeric_slot 不再含裸 `except ValueError:` 防御腿
    （_decimal_text 不抛错，该腿结构性不可达——与 E2 拆除同款）。"""
    src = inspect.getsource(facts_llm._numeric_slot)
    hits = [line.strip() for line in src.splitlines()
            if line.strip() == "except ValueError:"]
    assert hits == [], f"不可达 except ValueError 防御腿仍在：{hits}"


def test_f6b_dead_range_core_fallback_leg_removed():
    """机制钉：range_core.group(0) 在 _numeric_slot 代码行中仅余 1 处
    （re_dec 直赋活路）——L275-277 不可达回退腿已拆除。"""
    src = inspect.getsource(facts_llm._numeric_slot)
    hits = [line.strip() for line in src.splitlines()
            if "range_core.group(0)" in line
            and not line.strip().startswith("#")]
    assert len(hits) == 1, f"range_core.group(0) 代码行应恰 1 处，实测：{hits}"


# ---------- 机制前提钉（前后同绿）：不可达性构造证明 ----------

def test_f6b_decimal_text_never_raises_premise():
    """机制前提：_decimal_text 源码无 raise（仅 replace 去千分位逗号）——
    except ValueError 腿不可达的前提构造性证明。"""
    src = inspect.getsource(facts_rule._decimal_text)
    assert "raise" not in src
    # 行为面：任意核串（含中文核/千分位）均返回不抛错
    assert facts_rule._decimal_text("两百") == "两百"
    assert facts_rule._decimal_text("1,200") == "1200"


def test_f6b_chinese_core_flows_through_decimal_text(monkeypatch):
    """构造调用证不可达路径不触：中文核"两百"的 range_end 值实由
    _decimal_text（re_dec）路径产出（间谍逐值对拍），非回退腿。"""
    calls: list[tuple[str, str]] = []
    real = facts_llm._decimal_text

    def spy(raw_core: str) -> str:
        out = real(raw_core)
        calls.append((raw_core, out))
        return out

    monkeypatch.setattr(facts_llm, "_decimal_text", spy)
    facts = facts_llm.extract_facts_llm(
        RID, "指数升至一百点至两百点。",
        call_fn=_range_call_fn("两百点", value_raw="一百"))
    numeric = facts[0]["numerics"][0]
    assert ("两百", "两百") in calls, \
        f"range 核未经 _decimal_text 路径产出（调用流水：{calls}）"
    assert numeric["value"]["range_end"] == "两百"     # re_dec 路径产物
    assert numeric["range_end"]["status"] == "present"


def test_f6b_no_core_range_slot_present_inline_none():
    """构造调用证 else-None 出路由 re_dec 单源承载：range_end_raw 无数值核
    （range_core=None → re_dec=None）但引词命中 → 槽 present、inline None
    （中间回退腿对该出路零贡献）。"""
    facts = facts_llm.extract_facts_llm(
        RID, "指数升至100点至abc。", call_fn=_range_call_fn("abc"))
    numeric = facts[0]["numerics"][0]
    assert numeric["range_end"]["status"] == "present"
    assert numeric["value"]["range_end"] is None


# ---------- 前后均绿守卫（消费合同不变） ----------

def test_f6b_decimal_core_with_thousands_comma_unchanged():
    """守卫（前后同绿）：千分位核 "1,200" → inline "1200"（去逗号核串）。"""
    facts = facts_llm.extract_facts_llm(
        RID, "指数升至800点至1,200点。", call_fn=_range_call_fn("1,200点", value_raw="800"))
    numeric = facts[0]["numerics"][0]
    assert numeric["value"]["range_end"] == "1200"


def test_f6b_ascii_core_unchanged():
    """守卫（前后同绿）："200点" → inline "200"（C2 合同不动）。"""
    facts = facts_llm.extract_facts_llm(
        RID, "指数升至100点至200点。", call_fn=_range_call_fn("200点"))
    numeric = facts[0]["numerics"][0]
    assert numeric["value"]["range_end"] == "200"
    assert numeric["range_end"]["raw_value"] == "200点"
