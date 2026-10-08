# -*- coding: utf-8 -*-
"""修复波 2·注释锚漂移族卫测（A5-01/A5-02/A5-03 + A2-18/A4-D4/EWΔ-F5 合一）。

锚钉=源码行窗关键词断言（锚漂移即红——防"勘误后再漂移"）；
行为钉=注释所述行为现役为真（防"修锚顺手改行为"）。

实现窗登记（语义钉不动）：
1. 行窗全部按修复波 2 终态文件回填（§1 预算接线 +§2 F-4 使 vector_store.py
   自设计稿基线 +1~+6 行漂移——设计稿窗口号系波前基线，本文件钉终态）。
2. (a)-11 播种面按 test_winw3f_scan_pending_trip.py 现役先例
   （store.upsert_main(_record(...))——FakeDeliveryStore 无 seed* 方法，
   设计稿 seed 探测回退断言结构性不成立）。
"""
from __future__ import annotations

from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "news_flash_dedup"


def _lines(rel):
    return (SRC / rel).read_text(encoding="utf-8").splitlines()


def _window(lines, start_1based, end_1based):
    return "\n".join(lines[start_1based - 1:end_1based])


# ---- 行为钉（注释所述行为为真）----

def test_null_searcher_space_unconfirmed_shape():
    """A5-01 行为钉：NullVectorSearcher 拒收形态=unavailable+SPACE_UNCONFIRMED。"""
    from news_flash_dedup.recall.models import RecallRequest
    from news_flash_dedup.recall.service import NullVectorSearcher
    request = RecallRequest("default", "2026-09-28", "f" * 64, "item-q", 2,
                            "查询正文", embedding_space_id="space-x")
    result = NullVectorSearcher().search(request)
    assert result.status == "unavailable"
    assert result.error_code == "SPACE_UNCONFIRMED"
    assert result.coverage_complete is False


def test_real_store_none_query_vector_degrades_not_typeerror():
    """A5-01 行为钉②：query_vector=None→unavailable+EMBEDDING_INVALID（非 TypeError）。"""
    from news_flash_dedup.recall.vector_store import MilvusVectorStore
    from test_e_h02_vector_frontier import SPACE, _FakeES, _FakeMilvus, _hit_entity
    store = MilvusVectorStore(_FakeMilvus(_hit_entity()), _FakeES({}),
                              f"news_dedup_replay_ab12cd_{SPACE.space_id}",
                              "p01-batch-x1-", SPACE)
    from news_flash_dedup.recall.models import RecallRequest
    request = RecallRequest("default", "2026-09-28", "f" * 64, "item-q", 2,
                            "查询正文", visible_seq=1, prepared_seq=1,
                            embedding_space_id=SPACE.space_id)
    result = store.search(request, None)
    assert result.status == "unavailable"
    assert result.error_code == "EMBEDDING_INVALID"


# ---- 锚钉（行窗含镜像构造；窗号=修复波 2 终态亲读回填）----

@pytest.mark.parametrize(("rel", "start", "end", "needles"), [
    # A5-02① milvus_store.py:571 注释→vector_store.py:287-293 get 包装段
    # （P2 确定性破序增件致 +15 漂移，B1 机械锚先例勘正）
    ("recall/vector_store.py", 287, 293, ["milvus.get", "VectorWriteUnknown"]),
    # A5-02② :645→vector_store.py:300-306 upsert 包装段
    ("recall/vector_store.py", 300, 306, ["milvus.upsert", "VectorWriteUnknown"]),
    # A5-02③ :672→vector_store.py:188-189 prepare_vector_row scope 正则
    ("recall/vector_store.py", 188, 189, ["fullmatch", "scope_id"]),
    # A5-02③′ :672→vector_store.py:325-328 search SCOPE_INVALID
    ("recall/vector_store.py", 325, 328, ["fullmatch", "SCOPE_INVALID"]),
    # A5-02④ :714→vector_store.py:391 score type+isfinite 判型
    ("recall/vector_store.py", 391, 391, ["isfinite", "(int, float)"]),
    # A5-03 es_gateway.py:41→vector_store.py:281 replace("Z") 定点化（注释 :278-280）
    ("recall/vector_store.py", 278, 281, ['endswith("Z")', "+00:00"]),
    # A5-01① service.py:61→vector_store.py:321-324 SPACE_UNCONFIRMED 拒收段
    ("recall/vector_store.py", 321, 324, ["SPACE_UNCONFIRMED", "unavailable"]),
    # A5-01② service.py:65-66→vector_store.py:329-339 EMBEDDING_INVALID 捕获段
    ("recall/vector_store.py", 329, 339, ["ValueError", "EMBEDDING_INVALID"]),
])
def test_comment_anchor_windows(rel, start, end, needles):
    """注释引用行窗必须含镜像构造关键词（锚漂移→红）。"""
    window = _window(_lines(rel), start, end)
    for needle in needles:
        assert needle in window, f"{rel}:{start}-{end} 缺 {needle!r}（锚已漂移）"


def test_milvus_store_comment_anchors_point_inside_file():
    """反向钉：milvus_store.py 注释引用的 vector_store.py 行号不越界。"""
    milvus_lines = _lines("vector/milvus_store.py")
    vs_lines = _lines("recall/vector_store.py")
    import re
    for lineno, line in enumerate(milvus_lines, 1):
        for match in re.finditer(r"vector_store\.py:?(\d+)", line):
            assert int(match.group(1)) <= len(vs_lines), (
                f"milvus_store.py:{lineno} 引用越界")


# ---- (a)-9 A2-18：qwen_bpe 注释面零反引号 ----

def test_qwen_bpe_comment_no_markdown_backtick():
    """A2-18：qwen_bpe.py 注释面零反引号（排版残留防回归）。"""
    for lineno, line in enumerate(
            _lines("vector/qwen_bpe.py"), 1):
        assert "`" not in line, f"qwen_bpe.py:{lineno} 反引号残留"


# ---- (a)-11 A4-D4：scan_pending 收录语义=插入序全集（行为刻画钉）----

def test_scan_pending_zero_check_form_collects_insertion_order():
    """A4-D4 行为钉：now="" 零校验形态→全收录+dict 插入序（docstring 新文
    所述行为为真；防"修注释顺手改行为"）。"""
    from news_flash_dedup.delivery.dispatcher import scan_pending
    from news_flash_dedup.delivery.fake_store import FakeDeliveryStore

    from tests.unit.test_winw2fc_tz_gates import _record
    store = FakeDeliveryStore()
    for rid in ("r-3", "r-1", "r-2"):
        store.upsert_main(_record(record_id=rid))
    assert scan_pending(store, now="") == ["r-3", "r-1", "r-2"]


# ---- (a)-12 EWΔ F-5：公开函数入 __all__ ----

def test_expired_pending_cleanup_in_all():
    """EWΔ F-5：公开函数入 __all__（包装契约一致；17→18 计数钉）。"""
    from news_flash_dedup import lifecycle
    assert "execute_expired_pending_cleanup" in lifecycle.__all__
    assert len(lifecycle.__all__) == 18
    namespace = {}
    exec("from news_flash_dedup.lifecycle import *", namespace)
    assert callable(namespace["execute_expired_pending_cleanup"])
