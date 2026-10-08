# -*- coding: utf-8 -*-
"""W3F 红测（W3c-F-4）：vector_space.py:145 死分支证明注释首句措辞勘正。

缺陷面（纯注释，码不动）：chunk_text 进度证明注释首句"到达此处必有
end_token == start_token + 512（end_token < len(spans) 前提）"与句界
回扫成功情形矛盾——回扫成功时 end_token 已被改小（∈ [start+449,
start+512]），"必有 == start+512"不成立；证明结论（next_start =
end_token - 64 >= start_token + 385 > start_token 恒成立，原
「cannot make progress」分支结构性不可达）独立推导不受影响，仅措辞修。
注释钉钉死勘正后措辞；行为钉证回扫成功路径进度与覆盖不变（结论不动）。
"""

from __future__ import annotations

import inspect

from news_flash_dedup.recall.vector_space import CodepointTokenizer, chunk_text


# ---------- 红测（注释面：失准首句存在即红） ----------

def test_f4_false_first_sentence_removed():
    """注释钉：失准首句"必有 end_token == start_token + 512（end_token <
    len(spans) 前提）"已退役——该断言与句界回扫成功情形矛盾。"""
    src = inspect.getsource(chunk_text)
    assert "end_token == start_token + 512（end_token < len(spans) 前提）" \
        not in src, "与回扫成功情形矛盾的首句措辞仍在"


def test_f4_corrected_interval_wording_present():
    """注释钉：勘正后区间措辞在码——到达此处时 end_token ∈
    [start_token + 449, start_token + 512]（下界 start_token + 449 =
    boundary_start(start+448) + 1）。"""
    src = inspect.getsource(chunk_text)
    assert "start_token + 449" in src, \
        "勘正后区间下界（start_token + 449）措辞未落地"


# ---------- 前后均绿守卫（行为面：结论不动） ----------

def test_f4_rescan_success_still_progresses_and_covers():
    """守卫（前后同绿）：句界回扫成功路径（end_token 被改小到 501 ≠
    start+512）仍保证进度与全原文覆盖——证明结论在勘正前后题下成立。"""
    text = "甲" * 500 + "。" + "乙" * 200
    chunks = chunk_text(text, CodepointTokenizer())
    assert chunks[0].start == 0
    assert chunks[0].end == 501                    # 回扫命中 "。"(500) → end_token=501
    assert len(chunks) >= 2                        # 进度成立（next_start=437>0 未死循环）
    assert chunks[-1].end == len(text)
    assert all(chunk.text == text[chunk.start:chunk.end] for chunk in chunks)
    assert all(any(chunk.start <= point < chunk.end for chunk in chunks)
               for point in range(len(text)))
