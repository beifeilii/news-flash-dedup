# -*- coding: utf-8 -*-
"""窗口W2Fβ 条9（WA3a-F1）：实体通道页截断记账钉。

设计 P11 L19：实体通道 coverage_complete 需要双水位且「无抽取不确定或桶/页
截断」。ES 单页 size=top_k(30)、track_total_hits=False——hits==top_k 时页外
是否还有命中不可知，必须按页截断记账 coverage_complete=False（与 BM25 申报
口径同形：截断条件并入覆盖布尔式），不得静默 True。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from news_flash_dedup.recall.entities import EntityExtraction, extract_body_entities
from news_flash_dedup.recall.entity_channel import EntityChannel
from news_flash_dedup.recall.models import RecallRequest


NOW = datetime(2026, 9, 26, 1, tzinfo=timezone.utc)
DAY = "2026-09-26"
PREFIX = "p01-batch-w2fb-entity-"


def _item(record_id, item_id, seq, text, **overrides):
    source = {"scope_id": "default", "business_date": DAY,
              "record_id": record_id, "item_id": item_id, "arrival_seq": seq,
              "text": text,
              "expires_at": (NOW + timedelta(days=1)).isoformat(),
              "entity_ids": list(extract_body_entities(text).entity_ids)}
    source.update(overrides)
    return source


class EntityES:
    def __init__(self, docs):
        self.docs = {doc["record_id"]: doc for doc in docs}

    def search(self, *, index, body):
        clause = body["query"]["bool"]
        terms = {key: value for part in clause["filter"] if "term" in part
                 for key, value in part["term"].items()}
        ids = next(part["terms"]["entity_ids"] for part in clause["filter"]
                   if "terms" in part)
        lt = next(part["range"]["arrival_seq"]["lt"] for part in clause["filter"]
                  if "range" in part and "arrival_seq" in part["range"])
        excluded = {key: value for part in clause["must_not"]
                    for key, value in part["term"].items()}
        found = [doc for doc in self.docs.values()
                 if all(doc.get(key) == value for key, value in terms.items())
                 and any(entity_id in doc["entity_ids"] for entity_id in ids)
                 and doc["arrival_seq"] < lt
                 and all(doc.get(key) != value for key, value in excluded.items())
                 and datetime.fromisoformat(doc["expires_at"]) > NOW]
        found.sort(key=lambda doc: (doc["arrival_seq"], doc["record_id"]))
        return {"timed_out": False, "_shards": {"failed": 0}, "hits": {"hits": [
            {"_index": index, "_id": doc["record_id"],
             "_source": {"record_id": doc["record_id"]}}
            for doc in found[:body["size"]]
        ]}}

    def get(self, *, index, id, realtime):
        return {"_index": index, "_id": id, "found": True, "_source": self.docs[id]}


def _docs(count):
    return [_item(f"r{seq:03d}", f"i{seq:03d}", seq, "美国能源信息署公布石油库存")
            for seq in range(1, count + 1)]


def _verified_search(doc_count):
    extraction = extract_body_entities("EIA公布石油库存")
    verified = EntityExtraction(extraction.mentions, complete=True)
    request = RecallRequest("default", DAY, "current", "current-item", 100,
                            "EIA公布石油库存", visible_seq=99, prepared_seq=99)
    return EntityChannel(EntityES(_docs(doc_count)), PREFIX,
                         clock=lambda: NOW).search(request, extraction=verified)


def test_full_page_hits_marks_coverage_incomplete():
    """红能力：hits==top_k(30)=页可能截断 → coverage_complete=False。"""
    result = _verified_search(30)
    assert result.status == "complete"
    assert len(result.candidates) == 30
    assert result.coverage_complete is False


def test_full_page_keeps_status_candidates_and_no_error():
    """同钉：截断记账只动覆盖布尔——status/候选/error_code 形态不变。"""
    result = _verified_search(30)
    assert result.status == "complete"
    assert result.error_code is None
    assert [candidate.arrival_seq for candidate in result.candidates] == list(
        range(1, 31))


def test_partial_page_keeps_coverage_complete():
    """绿守卫：hits<top_k(29)=页确定穷尽 → coverage_complete 仍为 True。"""
    result = _verified_search(29)
    assert result.status == "complete"
    assert len(result.candidates) == 29
    assert result.coverage_complete is True
