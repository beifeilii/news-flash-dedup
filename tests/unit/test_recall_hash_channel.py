"""P10 Hash 通道与 P01/P09/P08 边界的契约测试。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from news_flash_dedup.batch_admission import BatchAdmissionCoordinator
from news_flash_dedup.es_admission_schema import item_mapping
from news_flash_dedup.recall.hash_channel import HashChannel, prepare_recall_fields
from news_flash_dedup.recall.models import RecallRequest
from news_flash_dedup.text import normalize_text


NOW = datetime(2026, 9, 26, 1, 0, tzinfo=timezone.utc)
DAY = "2026-09-26"
PREFIX = "p01-batch-p10-unit-"


def _item(record_id: str, item_id: str, seq: int, text: str, **overrides) -> dict:
    source = {
        "scope_id": "default", "business_date": DAY,
        "record_id": record_id, "item_id": item_id, "arrival_seq": seq,
        "text": text, "expires_at": (NOW + timedelta(days=1)).isoformat(),
        "preparation_state": "ready",
    }
    source.update(prepare_recall_fields(text, "default", DAY) or {})
    source.update(overrides)
    return source


class FakeES:
    def __init__(self, docs):
        self.docs = {doc["record_id"]: doc for doc in docs}
        self.queries = []
        self.gets = []
        self.fail_search = False

    def search(self, *, index, body):
        assert index == PREFIX + "news-dedup-items-v1-2026.09.26"
        self.queries.append(body)
        if self.fail_search:
            raise RuntimeError("injected outage")
        clause = body["query"]["bool"]
        terms = [part["term"] for part in clause["filter"] if "term" in part]
        filters = {key: value for term in terms for key, value in term.items()}
        lt = next(part["range"]["arrival_seq"]["lt"] for part in clause["filter"]
                  if "range" in part and "arrival_seq" in part["range"])
        excluded = {key: value for part in clause["must_not"]
                    for key, value in part["term"].items()}
        hashes = {key: value for part in clause["should"]
                  for key, value in part["term"].items()}
        found = [doc for doc in self.docs.values()
                 if all(doc.get(key) == value for key, value in filters.items())
                 and doc["arrival_seq"] < lt
                 and all(doc.get(key) != value for key, value in excluded.items())
                 and any(doc.get(key) == value for key, value in hashes.items())
                 and datetime.fromisoformat(doc["expires_at"]) > NOW]
        found.sort(key=lambda doc: (doc["arrival_seq"], doc["record_id"]))
        after = body.get("search_after")
        if after is not None:
            found = [doc for doc in found
                     if (doc["arrival_seq"], doc["record_id"]) > tuple(after)]
        hits = [{"_index": index, "_id": doc["record_id"],
                 "_source": doc, "sort": [doc["arrival_seq"], doc["record_id"]]}
                for doc in found[:body["size"]]]
        return {"timed_out": False, "_shards": {"failed": 0}, "hits": {"hits": hits}}

    def get(self, *, index, id, realtime):
        self.gets.append(id)
        assert realtime is True
        return {"_index": index, "_id": id, "found": True, "_source": self.docs[id]}


def _request(text="甲公司完成回购", *, seq=100):
    return RecallRequest("default", DAY, "current", "current-item", seq, text)


def test_prepare_fields_uses_p09_gate_and_preserves_position_map():
    assert prepare_recall_fields(" \t\n", "default", DAY) is None
    fields = prepare_recall_fields("甲\r\n乙", "default", DAY)
    expected = normalize_text("甲\r\n乙")
    assert fields["raw_hash"] == expected.raw_hash
    assert fields["normalized_hash"] == expected.normalized_hash
    assert fields["normalizer_version"] == expected.normalizer_version
    assert fields["position_map"]["spans"] == [[0, 1], [1, 3], [3, 4]]
    assert fields["position_map"]["transforms"] == [{
        "rule_id": "CRLF_TO_LF", "input_span": [1, 3], "output_span": [1, 2],
    }]
    assert fields["simhash_bands"] and fields["minhash_bands"]


def test_hash_recalls_raw_and_normalized_matches_as_one_channel():
    docs = [_item("raw", "a", 1, "甲公司\r\n完成回购"),
            _item("normalized", "b", 2, "甲公司\n完成回购")]
    es = FakeES(docs)
    result = HashChannel(es, PREFIX, clock=lambda: NOW).search(
        _request("甲公司\r\n完成回购")
    )
    assert result.status == "complete"
    assert [item.record_id for item in result.candidates] == ["raw", "normalized"]
    assert result.candidates[0].subpaths == ("raw", "normalized")
    assert result.candidates[1].subpaths == ("normalized",)
    assert es.gets == ["raw", "normalized"]
    query = es.queries[0]["query"]["bool"]
    assert {"term": {"scope_id": "default"}} in query["filter"]
    assert {"term": {"business_date": DAY}} in query["filter"]
    assert {"range": {"arrival_seq": {"lt": 100}}} in query["filter"]
    assert {"term": {"item_id": "current-item"}} in query["must_not"]


def test_hash_filters_domain_day_seq_ids_and_expiry_without_delivery_filter():
    docs = [_item("valid", "v", 1, "甲公司完成回购", delivery_state="expired"),
            _item("other-domain", "d", 1, "甲公司完成回购", scope_id="other"),
            _item("other-day", "x", 1, "甲公司完成回购", business_date="2026-09-25"),
            _item("later", "l", 101, "甲公司完成回购"),
            _item("same-item", "current-item", 2, "甲公司完成回购"),
            _item("current", "c", 3, "甲公司完成回购"),
            _item("expired", "e", 4, "甲公司完成回购",
                  expires_at=(NOW - timedelta(seconds=1)).isoformat())]
    result = HashChannel(FakeES(docs), PREFIX, clock=lambda: NOW).search(_request())
    assert result.status == "complete"
    assert [item.record_id for item in result.candidates] == ["valid"]


def test_hash_paginates_all_protected_hits_beyond_first_50():
    es = FakeES([_item(f"r{seq:03d}", f"item-{seq}", seq, "甲公司完成回购")
                 for seq in range(1, 53)])
    result = HashChannel(es, PREFIX, clock=lambda: NOW).search(_request())
    assert result.status == "complete"
    assert len(result.candidates) == 52
    assert result.pages == 2
    assert len(es.queries) == 2
    assert es.queries[1]["search_after"] == [50, "r050"]


def test_hash_does_not_query_blank_text_even_if_admission_has_raw_hash():
    es = FakeES([_item("blank", "b", 1, "   ")])
    result = HashChannel(es, PREFIX, clock=lambda: NOW).search(_request("   "))
    assert result.status == "not_applicable"
    assert not result.candidates
    assert not es.queries


def test_hash_search_failure_is_not_reported_as_complete_empty():
    es = FakeES([])
    es.fail_search = True
    result = HashChannel(es, PREFIX, clock=lambda: NOW).search(_request())
    assert result.status == "unavailable"
    assert result.error_code == "ES_SEARCH_FAILED"


def test_hash_query_completion_is_separate_from_verified_visibility_frontier():
    es = FakeES([])
    channel = HashChannel(es, PREFIX, clock=lambda: NOW)
    unknown = channel.search(_request())
    assert unknown.status == "complete"
    assert unknown.coverage_complete is False
    certified = channel.search(RecallRequest(
        "default", DAY, "current", "current-item", 100, "甲公司完成回购",
        visible_seq=99, prepared_seq=99,
    ))
    assert certified.status == "complete"
    assert certified.coverage_complete is True


def test_hash_lexical_visibility_without_preparation_does_not_claim_coverage():
    es = FakeES([])
    result = HashChannel(es, PREFIX, clock=lambda: NOW).search(RecallRequest(
        "default", DAY, "current", "current-item", 100,
        "甲公司完成回购", visible_seq=99,
    ))
    assert result.status == "complete"
    assert result.coverage_complete is False


def test_hash_rejects_corrupt_indexed_hash_after_realtime_readback():
    es = FakeES([_item("corrupt", "c", 1, "另一条正文",
                       raw_hash=normalize_text("甲公司完成回购").raw_hash)])
    result = HashChannel(es, PREFIX, clock=lambda: NOW).search(_request())
    assert result.status == "unavailable"
    assert result.error_code == "HASH_INDEX_MISMATCH"


def test_hash_respects_each_side_approved_outer_trim_artifact():
    candidate = _item("trimmed", "t", 1, " 甲公司完成回购 ")
    candidate.update(prepare_recall_fields(
        " 甲公司完成回购 ", "default", DAY, outer_trim_approved=True))
    es = FakeES([candidate])
    result = HashChannel(es, PREFIX, clock=lambda: NOW).search(_request())
    assert result.status == "complete"
    assert [item.record_id for item in result.candidates] == ["trimmed"]
    assert result.candidates[0].subpaths == ("normalized",)


def test_hash_query_can_use_approved_outer_trim_from_internal_request():
    es = FakeES([_item("plain", "p", 1, "甲公司完成回购")])
    request = RecallRequest("default", DAY, "current", "current-item", 100,
                            " 甲公司完成回购 ", outer_trim_approved=True)
    result = HashChannel(es, PREFIX, clock=lambda: NOW).search(request)
    assert result.status == "complete"
    assert [item.record_id for item in result.candidates] == ["plain"]


def test_p01_materialized_item_requires_p10_preparation_before_recall():
    entry = {
        "scope_id": "default", "request_id": "request-1", "item_id": "item-1",
        "record_id": "record-1", "schema_version": "v1", "pipeline_version": "v1",
        "embedding_space_id": "test-space", "business_date": DAY,
        "arrival_seq": 1, "received_at": NOW.isoformat(),
        "accepted_at": NOW.isoformat(),
        "expires_at": (NOW + timedelta(days=1)).isoformat(),
        "text": "甲公司完成回购", "business_fingerprint": "f" * 64,
        "delivery_route_ref": "test-route", "trace_id": None,
    }
    index, record_id, item = BatchAdmissionCoordinator._documents([entry])[0]
    assert index == "news-dedup-items-v1-2026.09.26"
    assert record_id == "record-1"
    assert item["raw_hash"] == normalize_text(entry["text"]).raw_hash
    es = FakeES([item])
    channel = HashChannel(es, PREFIX, clock=lambda: NOW)
    assert channel.search(_request()).candidates == ()
    prepared = prepare_recall_fields(item["text"], item["scope_id"], item["business_date"])
    assert prepared is not None
    assert set(prepared).issubset(item_mapping()["mappings"]["properties"])
    item.update(prepared)
    item["preparation_state"] = "ready"
    assert [candidate.record_id for candidate in channel.search(_request()).candidates] == [
        "record-1"]


def test_hash_rejects_corrupted_p09_transform_log():
    doc = _item("bad-map", "b", 1, "甲公司\r\n完成回购")
    doc["position_map"]["transforms"] = []
    es = FakeES([doc])
    result = HashChannel(es, PREFIX, clock=lambda: NOW).search(
        _request("甲公司\r\n完成回购"))
    assert result.status == "unavailable"
    assert result.error_code == "HASH_INDEX_MISMATCH"
