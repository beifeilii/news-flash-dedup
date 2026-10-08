# -*- coding: utf-8 -*-
"""窗口W2Fβ 条63（WA3b-M1）：search_strong 入参正则闸镜像钉。

vector/milvus_store.py search_strong 的 scope_id/business_date 原未校验直拼
Milvus filter 表达式（注入面）；recall/vector_store.py:181-183 同款正则闸此处
镜像——闸必须在表达式构造之前触发（输入缺陷=ValueError，不混入读确认未知的
VectorWriteUnknown），非法入参时 milvus.search 不得被调用。
"""
from __future__ import annotations

from datetime import date

import pytest

from news_flash_dedup.recall.vector_space import EmbeddingSpace
from news_flash_dedup.recall.vector_store import VectorWriteUnknown
from news_flash_dedup.vector.milvus_store import (
    RealMilvusP19Config,
    RealMilvusP19Store,
)


def _space():
    return EmbeddingSpace("mock_embedding", "fixture_v1", "codepoint_v1", "none",
                          "COSINE", 4, "plain_v1", "plain_v1",
                          "tokens512_overlap64_v1", "p09_v1")


def _bare_store(milvus):
    """绕开 __init__ 的 UAT 闸门/真建集合（沿用 winz2/w1fc 先例）。"""
    store = RealMilvusP19Store.__new__(RealMilvusP19Store)
    store.milvus = milvus
    store.config = RealMilvusP19Config(run_uuid="winw2fb",
                                       business_date=date(2026, 9, 26),
                                       space=_space())
    return store


class _SearchStubMilvus:
    def __init__(self):
        self.search_calls = []

    def search(self, **kwargs):
        self.search_calls.append(kwargs)
        return [[{
            "entity": {"record_id": "r-1", "chunk_id": 0, "arrival_seq": 1,
                       "scope_id": "default", "business_date": "2026-09-26",
                       "embedding_space_id": _space().space_id},
            "distance": 0.9, "vector_id": "v-1",
        }]]


@pytest.mark.parametrize("bad_scope", [
    'default" or scope_id == "x',      # filter 注入
    "Default",                          # 大写越域
    "scope with space",
    'quote"inside',
    "",
])
def test_search_strong_rejects_untrusted_scope_before_search(bad_scope):
    """红能力：scope_id 不过 recall 同款正则 → ValueError 且 search 零调用。"""
    milvus = _SearchStubMilvus()
    store = _bare_store(milvus)
    with pytest.raises(ValueError):
        store.search_strong(query_vector=[1.0, 0.0, 0.0, 0.0],
                            scope_id=bad_scope, business_date="2026-09-26")
    assert milvus.search_calls == []


@pytest.mark.parametrize("bad_date", ["2026-13-99", "2026-09-26T00:00:00", ""])
def test_search_strong_rejects_noncanonical_date_before_search(bad_date):
    """红能力：business_date 非规范日 → ValueError（不被兜底包成 Unknown）。"""
    milvus = _SearchStubMilvus()
    store = _bare_store(milvus)
    with pytest.raises(ValueError):
        store.search_strong(query_vector=[1.0, 0.0, 0.0, 0.0],
                            scope_id="default", business_date=bad_date)
    assert milvus.search_calls == []


def test_search_strong_wellformed_inputs_still_pass():
    """绿守卫：合法入参照常检索；过滤表达式逐字节同前。"""
    milvus = _SearchStubMilvus()
    store = _bare_store(milvus)
    hits = store.search_strong(query_vector=[1.0, 0.0, 0.0, 0.0],
                               scope_id="default", business_date="2026-09-26",
                               arrival_seq_before=9)
    assert [hit["record_id"] for hit in hits] == ["r-1"]
    expression = milvus.search_calls[0]["filter"]
    assert expression == (
        'scope_id == "default" and business_date == "2026-09-26" and '
        f'embedding_space_id == "{_space().space_id}" and arrival_seq < 9')


def test_search_strong_rejection_is_not_wrapped_as_unknown():
    """同钉：输入闸异常不落入 VectorWriteUnknown（两类故障域分立）。"""
    store = _bare_store(_SearchStubMilvus())
    try:
        store.search_strong(query_vector=[1.0, 0.0, 0.0, 0.0],
                            scope_id='x" or 1 == 1', business_date="2026-09-26")
    except VectorWriteUnknown:
        raise AssertionError("input gate must not be wrapped as write-unknown")
    except ValueError:
        pass
    else:
        raise AssertionError("injectable scope_id accepted")
