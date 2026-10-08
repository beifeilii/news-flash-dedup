# -*- coding: utf-8 -*-
"""W2Fα2 红测（条目 6，WA1b-L2）：extra_event "None" 伪 id。

decide/extra_event.py:27,32 `str(f.get("fact_id"))` 把缺 fact_id 的 Fact 映射成
伪 id "None"——双侧各一个无 id Fact 即 "None"=="None" 伪匹配，真实额外事件被吞
（假阴，探针 winw2fa2-probe-extra-event.json 合成实证确认；金标 rule v1/v2
全量抽取 0/1197、0/2071 零触发）。修法=None 前置拦截：缺 fact_id 的 Fact 不进
集合匹配（不伪造 id），fail-closed 方向。
"""
from __future__ import annotations

from news_flash_dedup.decide.extra_event import _fact_ids, detect_extra_event


def _artifact(facts: list) -> dict:
    return {"facts": facts}


class _Ctx:
    """对象形态（artifact 属性路径，覆盖 :32 分支）。"""

    def __init__(self, facts: list):
        self.artifact = {"facts": facts}


# ---------------------------------------------------------------- 红测（先红）

def test_l2_missing_fact_id_not_forged_into_none_string():
    """条目6 主红测：缺 fact_id → 提取结果为空，不得出现伪 id 'None'。"""
    ids = _fact_ids(_artifact([{"evidence": []}]))
    assert ids == ()
    assert "None" not in ids


def test_l2_object_artifact_branch_same_intercept():
    """条目6 同族钉：对象.artifact 路径（:32）同款 None 前置拦截。"""
    ids = _fact_ids(_Ctx([{"evidence": []}, {"fact_id": None}]))
    assert ids == ()
    assert "None" not in ids


def test_l2_two_idless_facts_not_pseudo_matched():
    """条目6 假阴钉：双侧各一个无 id Fact → 不得伪匹配入 matched。"""
    report = detect_extra_event(_artifact([{"note": "甲"}]),
                                _artifact([{"note": "乙"}]))
    assert "None" not in report.matched_fact_ids
    assert all("None" not in fid for fid in report.uncovered_fact_ids)


def test_l2_uncovered_not_polluted_by_pseudo_none():
    """条目6 污染钉：一侧真 id、一侧无 id → uncovered 只含真 id 分账。"""
    report = detect_extra_event(_artifact([{"fact_id": "f1"}]),
                                _artifact([{"note": "无 id"}]))
    assert report.has_extra_event is True
    assert report.uncovered_fact_ids == ("history:f1",)
    assert all("None" not in fid for fid in report.uncovered_fact_ids)


def test_l2_literal_none_id_no_longer_collides_with_missing():
    """条目6 碰撞钉：真字面值 'None' fact_id 不再与缺失 id 伪匹配。"""
    report = detect_extra_event(_artifact([{"fact_id": None}]),
                                _artifact([{"fact_id": "None"}]))
    # 缺失侧被拦截 → 真 'None' id 成 uncovered（fail-closed，不再吞）
    assert report.has_extra_event is True
    assert report.uncovered_fact_ids == ("current:None",)
    assert report.matched_fact_ids == ()


def test_l2_fact_count_excludes_idless_facts():
    """条目6 计数口径钉：fact_count 计有效 id 数（无 id Fact 不计）。"""
    report = detect_extra_event(
        _artifact([{"fact_id": "f1"}, {"note": "无 id"}]),
        _artifact([{"fact_id": "f1"}]))
    assert report.history_fact_count == 1
    assert report.current_fact_count == 1


# ---------------------------------------------------------------- 守卫（前后均绿）

def test_l2_normal_matching_semantics_unchanged():
    """条目6 守卫：正常 id 集合匹配语义不变（交集/差集分账逐字节）。"""
    report = detect_extra_event(
        _artifact([{"fact_id": "f1"}, {"fact_id": "f2"}]),
        _artifact([{"fact_id": "f2"}, {"fact_id": "f3"}]))
    assert report.has_extra_event is True
    assert report.uncovered_fact_ids == ("history:f1", "current:f3")
    assert report.matched_fact_ids == ("f2",)
    assert report.history_fact_count == 2
    assert report.current_fact_count == 2


def test_l2_non_dict_facts_still_skipped():
    """条目6 守卫：非 dict 元素仍跳过（现役纪律不变）。"""
    ids = _fact_ids(_artifact(["junk", 42, {"fact_id": "f1"}]))
    assert ids == ("f1",)
