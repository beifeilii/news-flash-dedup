# -*- coding: utf-8 -*-
"""窗口W2Fβ 条54+条58：prepared_seq 回传钉 + 通道审计耗时/截断原因字段钉 + BM25 k1/b 声明钉。

条54（WB2-M3）：recall/vector_store.search 的 ChannelResult 必须回传
request.prepared_seq（融合 _gap 的 VECTOR_FRONTIER_UNPROVEN 报告路径依赖）。
条58（WB2-L1/L3）：ChannelAudit 增 elapsed_ms（通道自报耗时，经 ChannelResult
plumbing）与 truncate_reason（截断原因独立字段，不再只混 error_code）——均为
additive 字段，既有 error_code/状态面不动；BM25 k1=1.2/b=0.75 显式配置声明
（值同 09 §5.4 初值，防 ES 默认漂移；不动 mapping——P11 §4 在案）。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from news_flash_dedup.recall.bm25_channel import BM25Channel
from news_flash_dedup.recall import bm25_channel as bm25_module
from news_flash_dedup.recall.entities import EntityExtraction, extract_body_entities
from news_flash_dedup.recall.entity_channel import EntityChannel
from news_flash_dedup.recall.fusion import freeze_recall_plan
from news_flash_dedup.recall.hash_channel import HashChannel, prepare_recall_fields
from news_flash_dedup.recall.models import ChannelResult, RecallRequest
from news_flash_dedup.recall.near_channel import NearChannel
from news_flash_dedup.recall.vector_space import EmbeddingSpace
from news_flash_dedup.recall.vector_store import MilvusVectorStore


NOW = datetime(2026, 9, 26, 1, tzinfo=timezone.utc)
DAY = "2026-09-26"
PREFIX = "p01-batch-w2fb-audit-"


# ---------- 共享 fake（形态沿用 test_recall_*/test_vector_store 既有钉文件） ----------

def _space():
    return EmbeddingSpace("mock_embedding", "fixture_v1", "codepoint_v1", "none",
                          "COSINE", 4, "plain_v1", "plain_v1",
                          "tokens512_overlap64_v1", "p09_v1")


def _item(record_id, item_id, seq, text, **overrides):
    source = {"scope_id": "default", "business_date": DAY,
              "record_id": record_id, "item_id": item_id, "arrival_seq": seq,
              "text": text,
              "expires_at": (NOW + timedelta(days=1)).isoformat()}
    source.update(overrides)
    return source


class BM25ES:
    def __init__(self, docs):
        self.docs = {doc["record_id"]: doc for doc in docs}

    def search(self, *, index, body):
        clause = body["query"]["bool"]
        lt = next(part["range"]["arrival_seq"]["lt"] for part in clause["filter"]
                  if "range" in part and "arrival_seq" in part["range"])
        match = clause["must"][0]["match"]["text"]
        found = [doc for doc in self.docs.values()
                 if doc.get("scope_id") == "default"
                 and doc.get("business_date") == DAY
                 and doc["arrival_seq"] < lt
                 and datetime.fromisoformat(doc["expires_at"]) > NOW
                 and (doc["text"] in match["query"] or match["query"] in doc["text"])]
        found.sort(key=lambda doc: (doc["arrival_seq"], doc["record_id"]))
        return {"timed_out": False, "_shards": {"failed": 0}, "hits": {"hits": [
            {"_index": index, "_id": doc["record_id"], "_score": 8.0,
             "_source": {"record_id": doc["record_id"]}}
            for doc in found[:body["size"]]
        ]}}

    def get(self, *, index, id, realtime):
        return {"_index": index, "_id": id, "found": True, "_source": self.docs[id]}


class EntityES:
    def __init__(self, docs):
        self.docs = {doc["record_id"]: doc for doc in docs}

    def search(self, *, index, body):
        clause = body["query"]["bool"]
        ids = next(part["terms"]["entity_ids"] for part in clause["filter"]
                   if "terms" in part)
        found = [doc for doc in self.docs.values()
                 if any(entity_id in doc["entity_ids"] for entity_id in ids)]
        found.sort(key=lambda doc: (doc["arrival_seq"], doc["record_id"]))
        return {"timed_out": False, "_shards": {"failed": 0}, "hits": {"hits": [
            {"_index": index, "_id": doc["record_id"],
             "_source": {"record_id": doc["record_id"]}}
            for doc in found[:body["size"]]
        ]}}

    def get(self, *, index, id, realtime):
        return {"_index": index, "_id": id, "found": True, "_source": self.docs[id]}


class NearES:
    def __init__(self, docs):
        self.docs = {doc["record_id"]: doc for doc in docs}

    def search(self, *, index, body):
        clause = body["query"]["bool"]
        band = next(part["term"] for part in clause["filter"]
                    if "term" in part and
                    ("simhash_bands" in part["term"] or "minhash_bands" in part["term"]))
        band_field, band_value = next(iter(band.items()))
        found = [doc for doc in self.docs.values()
                 if band_value in doc.get(band_field, [])]
        found.sort(key=lambda doc: (doc["arrival_seq"], doc["record_id"]))
        return {"timed_out": False, "_shards": {"failed": 0}, "hits": {"hits": [
            {"_index": index, "_id": doc["record_id"],
             "_source": {"record_id": doc["record_id"]}}
            for doc in found[:body["size"]]
        ]}}

    def get(self, *, index, id, realtime):
        return {"_index": index, "_id": id, "found": True, "_source": self.docs[id]}


class HashES:
    def __init__(self, docs):
        self.docs = {doc["record_id"]: doc for doc in docs}

    def search(self, *, index, body):
        clause = body["query"]["bool"]
        hashes = {key: value for part in clause["should"]
                  for key, value in part["term"].items()}
        found = [doc for doc in self.docs.values()
                 if any(doc.get(key) == value for key, value in hashes.items())]
        found.sort(key=lambda doc: (doc["arrival_seq"], doc["record_id"]))
        return {"timed_out": False, "_shards": {"failed": 0}, "hits": {"hits": [
            {"_index": index, "_id": doc["record_id"], "_source": doc,
             "sort": [doc["arrival_seq"], doc["record_id"]]}
            for doc in found[:body["size"]]
        ]}}

    def get(self, *, index, id, realtime):
        return {"_index": index, "_id": id, "found": True, "_source": self.docs[id]}


class FakeMilvus:
    def __init__(self, search_rows):
        self.rows = {}
        self.search_rows = search_rows

    def get(self, *, collection_name, ids, output_fields, consistency_level):
        return [self.rows[key] for key in ids if key in self.rows]

    def upsert(self, *, collection_name, data, partition_name):
        self.rows[data["vector_id"]] = data
        return {"upsert_count": 1}

    def search(self, **kwargs):
        return [self.search_rows[:kwargs["limit"]]]


class VectorES:
    def __init__(self, docs):
        self.docs = {doc["record_id"]: doc for doc in docs}

    def get(self, *, index, id, realtime):
        if id not in self.docs:
            return {"_index": index, "_id": id, "found": False}
        return {"_index": index, "_id": id, "found": True,
                "_source": self.docs[id]}


def _request(text="甲公司完成回购", **overrides):
    values = {"visible_seq": 99, "prepared_seq": 99}
    values.update(overrides)
    return RecallRequest("default", DAY, "c" * 64, "current-item", 100,
                         text, **values)


# ---------- 条54：vector_store ChannelResult 补传 prepared_seq ----------

def _vector_result(prepared_seq):
    record_id = "a" * 64
    doc = _item(record_id, "i1", 1, "甲公司完成回购",
                embedding_space_id=_space().space_id)
    hit = {"id": f"{record_id}_0", "distance": 0.9,
           "entity": {"record_id": record_id, "scope_id": "default",
                      "business_date": DAY, "arrival_seq": 1,
                      "embedding_space_id": _space().space_id, "chunk_id": 0}}
    store = MilvusVectorStore(
        FakeMilvus([hit]), VectorES([doc]),
        "news_dedup_replay_abc123_" + _space().space_id,
        PREFIX, _space(), clock=lambda: NOW)
    request = _request(embedding_space_id=_space().space_id,
                       prepared_seq=prepared_seq)
    return store.search(request, [1.0, 0.0, 0.0, 0.0])


def test_vector_channel_result_carries_prepared_seq():
    """红能力（条54）：embedding 通道结果回传请求 prepared_seq。"""
    result = _vector_result(99)
    assert result.status == "complete"
    assert result.prepared_seq == 99


def test_vector_channel_result_prepared_seq_none_passthrough():
    """同钉（条54）：请求 prepared_seq=None 时结果亦为 None（直传不捏造）。"""
    assert _vector_result(None).prepared_seq is None


# ---------- 条58-L1：ChannelAudit 耗时 + truncate_reason 独立字段 ----------

def _response(channel, *, status="complete", error_code=None, elapsed_ms=None):
    return ChannelResult(channel, status, (), channel + "_v1",
                         visible_seq=99, prepared_seq=99,
                         coverage_complete=(status == "complete"),
                         error_code=error_code, elapsed_ms=elapsed_ms)


def test_channel_audit_carries_elapsed_ms():
    """红能力（条58）：通道自报耗时经 ChannelResult 进入 ChannelAudit。"""
    request = _request()
    plan = freeze_recall_plan(request, tuple(
        _response(channel, elapsed_ms=12.5 if channel == "bm25" else 0.0)
        for channel in ("hash", "near", "bm25", "embedding", "entity")))
    audits = {audit.channel: audit for audit in plan.channel_audits}
    assert audits["bm25"].elapsed_ms == 12.5
    assert audits["hash"].elapsed_ms == 0.0


def test_channel_audit_truncate_reason_is_independent_field():
    """红能力（条58）：截断原因独立成字段；error_code 既有面不动。"""
    request = _request()
    plan = freeze_recall_plan(request, tuple(
        _response(channel,
                  status="truncated" if channel == "near" else "complete",
                  error_code="BUCKET_TRUNCATED" if channel == "near" else None)
        for channel in ("hash", "near", "bm25", "embedding", "entity")))
    audits = {audit.channel: audit for audit in plan.channel_audits}
    assert audits["near"].truncate_reason == "BUCKET_TRUNCATED"
    assert audits["near"].error_code == "BUCKET_TRUNCATED"
    assert audits["bm25"].truncate_reason is None


def test_missing_channel_audit_has_no_elapsed_or_truncate_reason():
    """同钉（条58）：CHANNEL_MISSING 审计两新字段为 None（不冒充已计量）。"""
    plan = freeze_recall_plan(_request(), [_response("hash")])
    audits = {audit.channel: audit for audit in plan.channel_audits}
    assert audits["near"].elapsed_ms is None
    assert audits["near"].truncate_reason is None


def test_main_channels_report_elapsed_ms_on_full_searches():
    """红能力（条58）：五通道真实 search 路径自报耗时（固定钟 → 0.0）。"""
    fp_doc = _item("h" * 64, "i1", 1, "甲公司完成回购",
                   preparation_state="ready")
    fp_doc.update(prepare_recall_fields("甲公司完成回购", "default", DAY))
    entity_doc = _item("e" * 64, "i2", 2, "美国能源信息署公布石油库存",
                       entity_ids=list(extract_body_entities(
                           "美国能源信息署公布石油库存").entity_ids))
    bm25 = BM25Channel(BM25ES([_item("b" * 64, "i3", 3, "甲公司完成回购")]),
                       PREFIX, clock=lambda: NOW).search(_request())
    near = NearChannel(NearES([fp_doc]), PREFIX,
                       clock=lambda: NOW).search(_request())
    hash_result = HashChannel(HashES([fp_doc]), PREFIX,
                              clock=lambda: NOW).search(_request())
    extraction = extract_body_entities("EIA公布石油库存")
    entity = EntityChannel(EntityES([entity_doc]), PREFIX,
                           clock=lambda: NOW).search(
        _request("EIA公布石油库存"),
        extraction=EntityExtraction(extraction.mentions, complete=True))
    vector = _vector_result(99)
    for result in (bm25, near, hash_result, entity, vector):
        assert result.status == "complete"
        assert result.elapsed_ms == 0.0


def test_early_return_results_also_carry_elapsed_ms():
    """同钉（条58）：not_applicable 早退路径同样戳耗时（不混 None/缺失）。"""
    blank = BM25Channel(BM25ES([]), PREFIX, clock=lambda: NOW).search(
        _request(" \t\n"))
    assert blank.status == "not_applicable"
    assert blank.elapsed_ms == 0.0


# ---------- 条58-L3：BM25 k1/b 显式配置声明 ----------

def test_bm25_k1_b_explicitly_declared_at_contract_values():
    """红能力（条58）：k1=1.2/b=0.75 显式声明（09 §5.4 初值，防默认漂移）。"""
    assert bm25_module.BM25_K1 == 1.2
    assert bm25_module.BM25_B == 0.75
