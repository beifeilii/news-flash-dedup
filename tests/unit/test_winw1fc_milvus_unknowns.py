# -*- coding: utf-8 -*-
r"""窗口W1Fγ vector 侧未知异常语义化钉（24 钉：18 能力 + 6 绿守卫）。

出处与迁移：红测证据先行落于 log\temp\winW1Fc_redtests.py（18 红 → 修复后
24 绿，见 log\窗口W1Fc-vector侧修复报告.md）；W1Fδ 收尾窗经主窗口裁定迁入
套件（本文件即原红测逐字搬迁，仅本 docstring 去除"禁碰 tests\"结构约束
旧述，测试体零改动）。fake 构造模式逐字沿用
tests\unit\test_winz2_medium_repairs.py _bare_store/_space/_p19_config 先例，
绕开 __init__ 的 UAT 闸门/真建集合。

四项能力钉 + 绿守卫：

1. search_strong 兜底 L313 `from None` 断链 → 修后 `__cause__` 保原异常。
2. es_authority_source（L209-211 一带）：ES GET 回包畸形（缺 _source /
   缺 _seq_no / _seq_no 非数值 / 缺 _primary_term / _source 非 Mapping）
   → VectorWriteUnknown，非裸 KeyError/TypeError/ValueError。
3. filter_hits_by_es_authority（L385 一带）：hit 畸形（非 Mapping /
   record_id 缺失 / record_id 非 str）→ VectorWriteUnknown，非裸
   KeyError/TypeError 或静默错记孔洞。
4. holes（L429 一带）：ES search 回包畸形（response 非 Mapping / hits
   容器非 Mapping / 内层非 list / hit 非 Mapping / 缺 _source / _source
   非 Mapping）→ VectorWriteUnknown，非裸 AttributeError/TypeError/
   KeyError 或静默放行垃圾。

绿守卫（修复前后均须绿）：NotFoundError→None（两态分立）；良构 GET 回包
逐字段原样；良构 filter 流程（kept/holes）逐字节不变；良构 holes 回读；
search_strong 兜底异常类型/消息不变、良构检索流程不变。
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


def _p19_config():
    return RealMilvusP19Config(run_uuid="winw1fc",
                               business_date=date(2026, 9, 28),
                               space=_space())


def _bare_store(milvus=None, es=None):
    """绕开 __init__ 的 UAT 闸门/真建集合，仅注入依赖（单元层）。"""
    store = RealMilvusP19Store.__new__(RealMilvusP19Store)
    store.milvus = milvus
    store.es = es
    store.config = _p19_config()
    return store


def _not_found():
    from elasticsearch import NotFoundError
    return NotFoundError("missing", None, None)


# =====================================================================
# 1. search_strong 兜底 from None → from error（P 批保链纪律）
# =====================================================================

class _SearchRaisingMilvus:
    def __init__(self, error):
        self._error = error

    def search(self, **kwargs):
        raise self._error


class _SearchStubMilvus:
    def __init__(self, response):
        self._response = response

    def search(self, **kwargs):
        return self._response


def _search_kwargs():
    return dict(query_vector=[1.0, 0.0, 0.0, 0.0],
                scope_id="default", business_date="2026-09-28")


def test_search_wrap_preserves_cause_chain():
    """项1：L313 旧 from None 断链——修后 __cause__ 保原异常。"""
    error = RuntimeError("boom-search")
    store = _bare_store(milvus=_SearchRaisingMilvus(error))
    with pytest.raises(VectorWriteUnknown, match="(?i)search") as caught:
        store.search_strong(**_search_kwargs())
    assert caught.value.__cause__ is error


def test_search_malformed_shape_preserves_cause_chain():
    """项1（同兜底）：回包形状畸形 → 内层 ValueError 入链。"""
    store = _bare_store(milvus=_SearchStubMilvus(response=[[], []]))
    with pytest.raises(VectorWriteUnknown, match="(?i)search") as caught:
        store.search_strong(**_search_kwargs())
    assert isinstance(caught.value.__cause__, ValueError)


def test_search_wrap_type_and_message_unchanged():
    """绿守卫：兜底异常类型/消息逐字节不变（仅 __cause__ 修复）。"""
    store = _bare_store(milvus=_SearchRaisingMilvus(RuntimeError("x")))
    with pytest.raises(VectorWriteUnknown,
                       match="Milvus search confirmation is unknown"):
        store.search_strong(**_search_kwargs())


def test_search_strong_wellformed_flow_unchanged():
    """绿守卫：良构回包检索流程不变（Z2 同形钉）。"""
    hit = {
        "entity": {
            "record_id": "r-1", "chunk_id": 0, "arrival_seq": 1,
            "scope_id": "default", "business_date": "2026-09-28",
            "embedding_space_id": _p19_config().space.space_id,
        },
        "distance": 0.95,
        "vector_id": "v-1",
    }
    store = _bare_store(milvus=_SearchStubMilvus(response=[[hit]]))
    hits = store.search_strong(**_search_kwargs())
    assert hits == [{"record_id": "r-1", "chunk_id": 0, "arrival_seq": 1,
                     "score": 0.95, "vector_id": "v-1"}]


# =====================================================================
# 2. es_authority_source：ES GET 回包畸形 → 语义化（判定①：外部回包）
# =====================================================================

class _GetStubES:
    def __init__(self, response=None, error=None):
        self._response = response
        self._error = error

    def get(self, **kwargs):
        if self._error is not None:
            raise self._error
        return self._response


@pytest.mark.parametrize("response", [
    {},                                  # 缺 _source → 旧裸 KeyError
    {"_source": {"record_id": "r"}},     # 缺 _seq_no → 旧裸 KeyError
    {"_source": {"record_id": "r"}, "_seq_no": 3},
    # ↑ 缺 _primary_term → 旧裸 KeyError
    {"_source": {"record_id": "r"}, "_seq_no": None, "_primary_term": 1},
    # ↑ int(None) → 旧裸 TypeError
    {"_source": {"record_id": "r"}, "_seq_no": "x", "_primary_term": 1},
    # ↑ int("x") → 旧裸 ValueError
    {"_source": "not-a-mapping", "_seq_no": 3, "_primary_term": 1},
    # ↑ _source 非 Mapping → 下游 .get 裸 AttributeError
])
def test_es_authority_malformed_reply_is_semantic(response):
    """项2-①：ES GET 回包畸形 → VectorWriteUnknown（非裸下标错）。"""
    store = _bare_store(es=_GetStubES(response=response))
    with pytest.raises(VectorWriteUnknown, match="(?i)authority"):
        store.es_authority_source("r-1")


def test_es_authority_not_found_still_none():
    """绿守卫：确认不存在 → None（两态分立，语义不动）。"""
    store = _bare_store(es=_GetStubES(error=_not_found()))
    assert store.es_authority_source("r-1") is None


def test_es_authority_wellformed_reply_unchanged():
    """绿守卫：良构回包逐字段原样返回。"""
    reply = {"_source": {"record_id": "r-1", "vector_state": "pending"},
             "_seq_no": 5, "_primary_term": 2}
    store = _bare_store(es=_GetStubES(response=reply))
    assert store.es_authority_source("r-1") == {
        "source": {"record_id": "r-1", "vector_state": "pending"},
        "seq_no": 5,
        "primary_term": 2,
    }


# =====================================================================
# 3. filter_hits_by_es_authority：hit 畸形 → 语义化（判定①：Milvus 回包衍生）
# =====================================================================

class _FilterStubES:
    """良构权威在库；缺 id → NotFoundError；create 记录孔洞调用。"""

    def __init__(self, docs):
        self._docs = docs
        self.created = []

    def get(self, *, index, id, realtime):
        if id not in self._docs:
            raise _not_found()
        return self._docs[id]

    def create(self, **kwargs):
        self.created.append(kwargs)
        return {"_id": kwargs.get("id")}


_WELLFORMED_REPLY = {"_source": {"record_id": "r-ok", "scope_id": "default",
                                 "business_date": "2026-09-28", "arrival_seq": 3,
                                 "vector_state": "ready"},
                     "_seq_no": 1, "_primary_term": 1}


@pytest.mark.parametrize("hit", [
    {},                  # 缺 record_id → 旧裸 KeyError
    None,                # 非 Mapping → 旧裸 TypeError
    [],                  # 非 Mapping → 旧裸 TypeError
    {"record_id": 7},    # record_id 非 str → 旧静默按权威不匹配错记孔洞
])
def test_filter_malformed_hit_is_semantic(hit):
    """项2-②：hit 畸形 → VectorWriteUnknown（非裸下标错/静默错记）。"""
    store = _bare_store(es=_FilterStubES({"r-ok": _WELLFORMED_REPLY}))
    with pytest.raises(VectorWriteUnknown, match="(?i)hit identity"):
        store.filter_hits_by_es_authority([hit], scope_id="default",
                                          business_date="2026-09-28")


def test_filter_wellformed_hits_flow_unchanged():
    """绿守卫：良构 hits 流程逐字节不变（匹配→kept；缺失→孔洞幂等）。"""
    es = _FilterStubES({"r-ok": _WELLFORMED_REPLY})
    store = _bare_store(es=es)
    hits = [{"record_id": "r-ok", "arrival_seq": 3},
            {"record_id": "r-miss", "arrival_seq": 9}]
    kept, holes = store.filter_hits_by_es_authority(
        hits, scope_id="default", business_date="2026-09-28")
    assert kept == [{"record_id": "r-ok", "arrival_seq": 3}]
    assert holes == [{"record_id": "r-miss", "reason": "ES_MISSING"}]
    assert len(es.created) == 1  # 孔洞落 control 索引一次


# =====================================================================
# 4. holes：ES search 回包畸形 → 语义化（判定①：外部回包）
# =====================================================================

class _SearchStubES:
    def __init__(self, response):
        self._response = response

    def search(self, **kwargs):
        return self._response


@pytest.mark.parametrize("response", [
    None,                                        # response 非 Mapping → 旧裸 AttributeError
    {"hits": "not-a-mapping"},                   # hits 容器非 Mapping → 旧裸 AttributeError
    {"hits": {"hits": "not-a-list"}},            # 内层非 list → 旧逐条裸 TypeError
    {"hits": {"hits": [None]}},                  # hit 非 Mapping → 旧裸 TypeError
    {"hits": {"hits": [{"no_source": 1}]}},      # 缺 _source → 旧裸 KeyError
    {"hits": {"hits": [{"_source": "not-a-mapping"}]}},
    # ↑ _source 非 Mapping → 旧静默放行垃圾进台账
])
def test_holes_malformed_reply_is_semantic(response):
    """项2-③：ES search 回包畸形 → VectorWriteUnknown（非裸下标错）。"""
    store = _bare_store(es=_SearchStubES(response))
    with pytest.raises(VectorWriteUnknown, match="(?i)holes"):
        store.holes()


def test_holes_wellformed_reply_unchanged():
    """绿守卫：良构回包回读 _source 列表；缺 hits 键 → 空（旧兼容面不动）。"""
    store = _bare_store(es=_SearchStubES({"hits": {"hits": [
        {"_source": {"hole_id": "h1"}}, {"_source": {"hole_id": "h2"}}]}}))
    assert store.holes() == [{"hole_id": "h1"}, {"hole_id": "h2"}]
    store_empty = _bare_store(es=_SearchStubES({}))
    assert store_empty.holes() == []
