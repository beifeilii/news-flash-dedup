"""P11 正文实体候选与识别缺口的状态语义。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from news_flash_dedup.recall.entities import EntityExtraction, extract_body_entities
from news_flash_dedup.recall.entity_channel import EntityChannel
from news_flash_dedup.recall.models import RecallRequest


NOW = datetime(2026, 9, 26, 1, tzinfo=timezone.utc)
DAY = "2026-09-26"
PREFIX = "p01-batch-p11-entity-unit-"


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
        self.queries = []
        self.fail_search = False

    def search(self, *, index, body):
        assert index == PREFIX + "news-dedup-items-v1-2026.09.26"
        self.queries.append(body)
        if self.fail_search:
            raise RuntimeError("injected outage")
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
        assert realtime is True
        return {"_index": index, "_id": id, "found": True, "_source": self.docs[id]}


def _request(text="EIA公布石油库存", *, visible_seq=None, prepared_seq=None):
    return RecallRequest("default", DAY, "current", "current-item", 100,
                         text, visible_seq=visible_seq, prepared_seq=prepared_seq)


def test_entity_alias_recall_keeps_hit_but_marks_extraction_uncertain():
    es = EntityES([_item("prior", "p", 1, "美国能源信息署公布石油库存")])
    result = EntityChannel(es, PREFIX, clock=lambda: NOW).search(_request())
    assert result.status == "unavailable"
    assert result.error_code == "ENTITY_EXTRACTION_INCOMPLETE"
    assert [candidate.record_id for candidate in result.candidates] == ["prior"]
    assert result.coverage_complete is False
    query = es.queries[0]["query"]["bool"]
    assert {"terms": {"entity_ids": ["entity_v1|org:美国能源信息署"]}} in query["filter"]
    assert "delivery_state" not in str(query)


def test_entity_unrecognized_body_cannot_claim_no_entities():
    es = EntityES([])
    result = EntityChannel(es, PREFIX, clock=lambda: NOW).search(_request("今日有新进展"))
    assert result.status == "unavailable"
    assert result.error_code == "ENTITY_UNRESOLVED"
    assert not es.queries


def test_only_verified_complete_empty_extraction_is_not_applicable():
    es = EntityES([])
    result = EntityChannel(es, PREFIX, clock=lambda: NOW).search(
        _request("今日有新进展"), extraction=EntityExtraction((), complete=True))
    assert result.status == "not_applicable"
    assert not es.queries


def test_verified_complete_extraction_and_both_frontiers_enable_coverage():
    es = EntityES([_item("prior", "p", 1, "美国能源信息署公布石油库存")])
    extraction = extract_body_entities("EIA公布石油库存")
    verified = EntityExtraction(extraction.mentions, complete=True)
    result = EntityChannel(es, PREFIX, clock=lambda: NOW).search(
        _request(visible_seq=99, prepared_seq=99), extraction=verified)
    assert result.status == "complete"
    assert result.coverage_complete is True
    assert [candidate.record_id for candidate in result.candidates] == ["prior"]


def test_group_and_subsidiary_do_not_alias_in_query():
    es = EntityES([_item("group", "g", 1, "甲集团发布公告")])
    result = EntityChannel(es, PREFIX, clock=lambda: NOW).search(_request("甲公司发布公告"))
    assert result.candidates == ()


def test_corrupted_entity_index_is_unavailable_not_empty_success():
    es = EntityES([_item("bad", "b", 1, "另一条正文",
                         entity_ids=["entity_v1|org:美国能源信息署"])])
    result = EntityChannel(es, PREFIX, clock=lambda: NOW).search(_request())
    assert result.status == "unavailable"
    assert result.error_code == "ENTITY_INDEX_MISMATCH"


def test_entity_search_failure_is_unavailable():
    es = EntityES([])
    es.fail_search = True
    result = EntityChannel(es, PREFIX, clock=lambda: NOW).search(_request())
    assert result.status == "unavailable"
    assert result.error_code == "ES_SEARCH_FAILED"
