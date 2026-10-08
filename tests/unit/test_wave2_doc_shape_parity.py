# -*- coding: utf-8 -*-
"""修复波 2·A1-009 文档形状双份构造对拍卫测。

单槽（admission recover 物化）与批面（batch._documents）主记录/三映射
形状恒等同构——任一侧单独演进即红（漂移温床封盖）。

实现窗登记（设计稿 §3.1 (a)-5 授权先例调整，语义钉"同构恒等"不动）：
1. 过滤缺陷修正：MemoryStore 文档键为 (index, doc_id) 二元组、主记录
   doc_id=record_id——items 须按 INDEX 前缀 "news-dedup-items-v1-" 过滤
   （设计稿按 doc-key 前缀过滤恒空集）；mappings 按 doc-key 前缀
   request:/item:/seq: 过滤（设计稿此半正确，保留）。
2. 时钟对齐：batch coordinator 须 when=request().received_at 与单槽同钟——
   否则 business_date/accepted_at 结构性分叉（2026-09-23 vs 2026-09-24）。
"""
from __future__ import annotations

from test_admission import MemoryStore, request
from test_batch_admission import BatchMemoryStore, coordinator as batch_coordinator

from news_flash_dedup.admission import AdmissionCoordinator

_ITEMS_PREFIX = "news-dedup-items-v1-"
_MAPPING_PREFIXES = ("request:", "item:", "seq:")


def _partition(docs):
    """(index, key)→source 文档面分区：items 按索引前缀，mappings 按键前缀。"""
    items = {key: body["source"] for (index, key), body in docs.items()
             if index.startswith(_ITEMS_PREFIX)}
    mappings = {key: body["source"] for (index, key), body in docs.items()
                if key.startswith(_MAPPING_PREFIXES)}
    return items, mappings


def _single_slot_documents():
    """单槽：accept(materialize=False)→recover() 物化，取主记录+三映射。"""
    store = MemoryStore()
    service = AdmissionCoordinator(
        store, owner_id="writer-1", owner_isolated=lambda: True,
        clock=lambda: request().received_at)
    service.accept(request(), materialize=False)
    service.recover()
    return _partition(store.docs)


def _batch_documents():
    """批面：accept_batch→materialize_oldest，取主记录+三映射。"""
    store = BatchMemoryStore()
    service = batch_coordinator(store, when=request().received_at)
    service.accept_batch([request()])
    service.materialize_oldest()
    return _partition(store.docs)


def test_main_record_shape_parity():
    single, _ = _single_slot_documents()
    batch, _ = _batch_documents()
    assert len(single) == len(batch) == 1
    s_main, b_main = next(iter(single.values())), next(iter(batch.values()))
    assert set(s_main) == set(b_main)                       # 键集恒等（漂移即红）
    for key in ("scope_id", "request_id", "item_id", "record_id", "schema_version",
                "pipeline_version", "embedding_space_id", "business_date",
                "arrival_seq", "received_at", "accepted_at", "expires_at", "text",
                "raw_hash", "task_state", "delivery_state", "result"):
        assert s_main[key] == b_main[key], key
    assert set(s_main["diagnostics"]) == set(b_main["diagnostics"]) == {"delivery"}
    assert (set(s_main["diagnostics"]["delivery"])
            == set(b_main["diagnostics"]["delivery"]))


def test_three_mappings_shape_parity():
    _, s_maps = _single_slot_documents()
    _, b_maps = _batch_documents()
    assert set(s_maps) == set(b_maps)                       # request:/item:/seq: 三键同集
    for key in s_maps:
        assert set(s_maps[key]) == set(b_maps[key]), key    # kind+8 公共键+target_index
        for field, value in s_maps[key].items():
            assert b_maps[key][field] == value, (key, field)
