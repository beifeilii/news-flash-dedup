"""P11 正文 BM25 召回边界与长文尾段覆盖。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from news_flash_dedup.recall.bm25_channel import BM25Channel
from news_flash_dedup.recall.models import RecallRequest


NOW = datetime(2026, 9, 26, 1, tzinfo=timezone.utc)
DAY = "2026-09-26"
PREFIX = "p01-batch-p11-unit-"


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
        self.queries = []
        self.gets = []
        self.fail_at = None

    def search(self, *, index, body):
        assert index == PREFIX + "news-dedup-items-v1-2026.09.26"
        self.queries.append(body)
        if self.fail_at == len(self.queries):
            raise RuntimeError("injected outage")
        clause = body["query"]["bool"]
        terms = {key: value for part in clause["filter"] if "term" in part
                 for key, value in part["term"].items()}
        lt = next(part["range"]["arrival_seq"]["lt"] for part in clause["filter"]
                  if "range" in part and "arrival_seq" in part["range"])
        excluded = {key: value for part in clause["must_not"]
                    for key, value in part["term"].items()}
        match = clause["must"][0]["match"]["text"]
        query_text = match["query"]
        assert match["operator"] == "or"
        found = [doc for doc in self.docs.values()
                 if all(doc.get(key) == value for key, value in terms.items())
                 and doc["arrival_seq"] < lt
                 and all(doc.get(key) != value for key, value in excluded.items())
                 and datetime.fromisoformat(doc["expires_at"]) > NOW
                 and (doc["text"] in query_text or query_text in doc["text"])]
        found.sort(key=lambda doc: (doc["arrival_seq"], doc["record_id"]))
        return {"timed_out": False, "_shards": {"failed": 0}, "hits": {"hits": [
            {"_index": index, "_id": doc["record_id"], "_score": 8.0,
             "_source": {"record_id": doc["record_id"]}}
            for doc in found[:body["size"]]
        ]}}

    def get(self, *, index, id, realtime):
        assert realtime is True
        self.gets.append(id)
        return {"_index": index, "_id": id, "found": True, "_source": self.docs[id]}


def _request(text="甲公司完成回购", *, visible_seq=None):
    return RecallRequest("default", DAY, "current", "current-item", 100,
                         text, visible_seq)


def test_bm25_queries_only_body_with_common_filter_and_rechecks_full_record():
    es = BM25ES([
        _item("valid", "v", 1, "甲公司完成回购", delivery_state="expired"),
        _item("domain", "d", 2, "甲公司完成回购", scope_id="other"),
        _item("day", "x", 3, "甲公司完成回购", business_date="2026-09-25"),
        _item("late", "l", 101, "甲公司完成回购"),
        _item("same-item", "current-item", 4, "甲公司完成回购"),
        _item("current", "c", 5, "甲公司完成回购"),
    ])
    result = BM25Channel(es, PREFIX, clock=lambda: NOW).search(_request(visible_seq=99))
    assert result.status == "complete"
    assert result.coverage_complete is True
    assert [candidate.record_id for candidate in result.candidates] == ["valid"]
    assert es.gets == ["valid"]
    query = es.queries[0]["query"]["bool"]
    assert query["must"] == [{"match": {"text": {"query": "甲公司完成回购",
                                           "operator": "or"}}}]
    assert {"term": {"business_date": DAY}} in query["filter"]
    assert {"term": {"item_id": "current-item"}} in query["must_not"]
    assert "delivery_state" not in str(query)


def test_bm25_long_text_queries_tail_instead_of_silently_dropping_it():
    text = "前段文字" * 150 + "尾段标识"
    es = BM25ES([_item("tail", "t", 1, "尾段标识")])
    result = BM25Channel(es, PREFIX, clock=lambda: NOW).search(_request(text))
    assert result.status == "complete"
    assert len(es.queries) > 1
    assert "尾段标识" in es.queries[-1]["query"]["bool"]["must"][0]["match"]["text"]["query"]
    assert [candidate.record_id for candidate in result.candidates] == ["tail"]


def test_bm25_second_segment_failure_retains_first_hit_but_marks_unavailable():
    text = "首段标识" + "甲" * 600
    es = BM25ES([_item("first", "f", 1, "首段标识")])
    es.fail_at = 2
    result = BM25Channel(es, PREFIX, clock=lambda: NOW).search(_request(text))
    assert result.status == "unavailable"
    assert result.coverage_complete is False
    assert [candidate.record_id for candidate in result.candidates] == ["first"]


def test_bm25_no_visibility_proof_and_empty_text_are_distinct_states():
    es = BM25ES([])
    unknown = BM25Channel(es, PREFIX, clock=lambda: NOW).search(_request())
    assert unknown.status == "complete"
    assert unknown.coverage_complete is False
    blank = BM25Channel(es, PREFIX, clock=lambda: NOW).search(_request(" \t\n"))
    assert blank.status == "not_applicable"
    assert len(es.queries) == 1
