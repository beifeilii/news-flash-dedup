"""P10 近重复两子路径合一与桶覆盖边界。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from news_flash_dedup.recall.hash_channel import prepare_recall_fields
from news_flash_dedup.recall.models import RecallRequest
from news_flash_dedup.recall.near_channel import NearChannel


NOW = datetime(2026, 9, 26, 1, tzinfo=timezone.utc)
DAY = "2026-09-26"
PREFIX = "p01-batch-p10-near-unit-"


def _item(record_id, item_id, seq, text, **overrides):
    result = {
        "scope_id": "default", "business_date": DAY,
        "record_id": record_id, "item_id": item_id, "arrival_seq": seq,
        "text": text, "expires_at": (NOW + timedelta(days=1)).isoformat(),
        "preparation_state": "ready",
    }
    result.update(prepare_recall_fields(text, "default", DAY) or {})
    result.update(overrides)
    return result


class NearES:
    def __init__(self, docs):
        self.docs = {doc["record_id"]: doc for doc in docs}
        self.queries = []
        self.gets = []
        self.fail_at = None
        self.inflate_first_bucket = False

    def search(self, *, index, body):
        assert index == PREFIX + "news-dedup-items-v1-2026.09.26"
        self.queries.append(body)
        if self.fail_at == len(self.queries):
            raise RuntimeError("injected outage")
        clause = body["query"]["bool"]
        terms = {key: value for part in clause["filter"] if "term" in part
                 for key, value in part["term"].items()}
        band = next(part["term"] for part in clause["filter"]
                    if "term" in part and
                    ("simhash_bands" in part["term"] or "minhash_bands" in part["term"]))
        band_field, band_value = next(iter(band.items()))
        lt = next(part["range"]["arrival_seq"]["lt"] for part in clause["filter"]
                  if "range" in part and "arrival_seq" in part["range"])
        excluded = {key: value for part in clause["must_not"]
                    for key, value in part["term"].items()}
        found = [doc for doc in self.docs.values()
                 if all(doc.get(key) == value for key, value in terms.items()
                        if key not in ("simhash_bands", "minhash_bands"))
                 and band_value in doc.get(band_field, [])
                 and doc["arrival_seq"] < lt
                 and all(doc.get(key) != value for key, value in excluded.items())
                 and datetime.fromisoformat(doc["expires_at"]) > NOW]
        found.sort(key=lambda doc: (doc["arrival_seq"], doc["record_id"]))
        hits = [{"_index": index, "_id": doc["record_id"],
                 "_source": {"record_id": doc["record_id"]}}
                for doc in found[:body["size"]]]
        if self.inflate_first_bucket and len(self.queries) == 1 and hits:
            hits = hits * body["size"]
        return {"timed_out": False, "_shards": {"failed": 0}, "hits": {"hits": hits}}

    def get(self, *, index, id, realtime):
        self.gets.append(id)
        assert realtime is True
        return {"_index": index, "_id": id, "found": True, "_source": self.docs[id]}


def _request(text="甲公司完成3亿元回购", *, visible_seq=None):
    return RecallRequest("default", DAY, "current", "current-item", 100,
                         text, visible_seq)


def test_near_merges_simhash_and_minhash_hits_into_one_main_channel():
    es = NearES([_item("prior", "p", 1, "甲公司完成3亿元回购")])
    result = NearChannel(es, PREFIX, clock=lambda: NOW).search(RecallRequest(
        "default", DAY, "current", "current-item", 100,
        "甲公司完成3亿元回购", visible_seq=99, prepared_seq=99))
    assert result.status == "complete"
    assert result.coverage_complete is True
    assert result.channel == "near"
    assert len(result.candidates) == 1
    assert result.candidates[0].subpaths == ("simhash", "minhash")
    assert len(es.gets) == 1
    assert len(es.queries) == 36


def test_near_filters_domain_day_order_ids_and_expiry():
    body = "甲公司完成3亿元回购"
    es = NearES([
        _item("valid", "v", 1, body, delivery_state="exhausted"),
        _item("other-domain", "d", 2, body, scope_id="other"),
        _item("other-day", "x", 3, body, business_date="2026-09-25"),
        _item("later", "l", 101, body),
        _item("same-item", "current-item", 4, body),
        _item("current", "c", 5, body),
        _item("expired", "e", 6, body,
              expires_at=(NOW - timedelta(seconds=1)).isoformat()),
    ])
    result = NearChannel(es, PREFIX, clock=lambda: NOW).search(_request())
    assert result.status == "complete"
    assert [candidate.record_id for candidate in result.candidates] == ["valid"]


def test_near_empty_text_does_not_query_es():
    es = NearES([])
    result = NearChannel(es, PREFIX, clock=lambda: NOW).search(_request(" \t\n"))
    assert result.status == "not_applicable"
    assert not es.queries


def test_near_bucket_over_500_is_explicitly_truncated():
    es = NearES([_item("prior", "p", 1, "甲公司完成3亿元回购")])
    es.inflate_first_bucket = True
    result = NearChannel(es, PREFIX, clock=lambda: NOW).search(_request())
    assert result.status == "truncated"
    assert result.coverage_complete is False
    assert len(result.truncated_buckets) == 1
    assert [candidate.record_id for candidate in result.candidates] == ["prior"]


def test_near_bucket_query_failure_is_not_empty_success():
    es = NearES([_item("prior", "p", 1, "甲公司完成3亿元回购")])
    es.fail_at = 2
    result = NearChannel(es, PREFIX, clock=lambda: NOW).search(_request())
    assert result.status == "unavailable"
    assert result.error_code == "ES_SEARCH_FAILED"
    assert [candidate.record_id for candidate in result.candidates] == ["prior"]


def test_near_top40_exclusion_is_counted_without_claiming_bucket_failure():
    es = NearES([_item(f"r{seq:03d}", f"i{seq:03d}", seq,
                       "甲公司完成3亿元回购") for seq in range(1, 42)])
    result = NearChannel(es, PREFIX, clock=lambda: NOW).search(RecallRequest(
        "default", DAY, "current", "current-item", 100,
        "甲公司完成3亿元回购", visible_seq=99, prepared_seq=99))
    assert result.status == "complete"
    assert result.coverage_complete is True
    assert len(result.candidates) == 40
    assert result.topk_excluded == 1
    assert [candidate.arrival_seq for candidate in result.candidates] == list(range(1, 41))


def test_near_corrupt_signature_fails_closed_after_readback():
    es = NearES([_item("bad", "b", 1, "甲公司完成3亿元回购",
                       fingerprint_payload={"minhash_signature": [0] * 128,
                                            "simhash_version": "simhash_v1",
                                            "minhash_version": "minhash_v1"})])
    result = NearChannel(es, PREFIX, clock=lambda: NOW).search(_request())
    assert result.status == "unavailable"
    assert result.error_code == "FINGERPRINT_INDEX_MISMATCH"


def test_near_respects_candidate_approved_outer_trim():
    candidate = _item("trimmed", "t", 1, " 甲公司完成3亿元回购 ")
    candidate.update(prepare_recall_fields(
        " 甲公司完成3亿元回购 ", "default", DAY, outer_trim_approved=True))
    es = NearES([candidate])
    result = NearChannel(es, PREFIX, clock=lambda: NOW).search(_request())
    assert result.status == "complete"
    assert [item.record_id for item in result.candidates] == ["trimmed"]


def test_near_rejects_corrupted_p09_transform_log():
    doc = _item("bad-map", "b", 1, "甲公司\r\n完成3亿元回购")
    doc["position_map"]["transforms"] = []
    es = NearES([doc])
    result = NearChannel(es, PREFIX, clock=lambda: NOW).search(
        _request("甲公司\r\n完成3亿元回购"))
    assert result.status == "unavailable"
    assert result.error_code == "FINGERPRINT_INDEX_MISMATCH"


def test_near_lexical_visibility_without_preparation_does_not_claim_coverage():
    es = NearES([])
    result = NearChannel(es, PREFIX, clock=lambda: NOW).search(
        _request(visible_seq=99))
    assert result.status == "complete"
    assert result.coverage_complete is False
