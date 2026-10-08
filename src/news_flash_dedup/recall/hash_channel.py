"""P10 原文/归一 Hash 共一路召回；命中后实时回取全文。"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from typing import Any

from news_flash_dedup.es_admission_schema import day_index
from news_flash_dedup.text import NORMALIZER_VERSION, normalize_text
from news_flash_dedup.text.hash import bucket_key_if_indexable

from .fingerprints import MINHASH_VERSION, SIMHASH_VERSION, build_fingerprints
from .es_gateway import (
    candidate_eligible,
    elapsed_ms_since,
    position_map_payload,
    response_body,
)
from .models import ChannelResult, RecallCandidate, RecallRequest


HASH_QUERY_VERSION = "hash_v1"


def prepare_recall_fields(text: str, scope_id: str, business_date: str,
                          *, outer_trim_approved: bool = False) -> dict | None:
    """返回可 CAS 并入主记录的派生字段；受理 raw_hash 不代表桶就绪。"""
    raw_key = bucket_key_if_indexable(
        text, scope_id, business_date, "raw",
        outer_trim_approved=outer_trim_approved,
    )
    normalized_key = bucket_key_if_indexable(
        text, scope_id, business_date, "normalized",
        outer_trim_approved=outer_trim_approved,
    )
    if raw_key is None or normalized_key is None:
        return None
    normalized = normalize_text(text, outer_trim_approved=outer_trim_approved)
    fingerprints = build_fingerprints(text, outer_trim_approved=outer_trim_approved)
    if fingerprints is None:
        raise RuntimeError("indexable text produced no fingerprints")
    if not raw_key.endswith("/raw/" + normalized.raw_hash):
        raise RuntimeError("raw bucket differs from normalized artifact")
    if not normalized_key.endswith("/normalized/" + normalized.normalized_hash):
        raise RuntimeError("normalized bucket differs from normalized artifact")
    return {
        "raw_hash": normalized.raw_hash,
        "normalized_hash": normalized.normalized_hash,
        "normalizer_version": normalized.normalizer_version,
        "normalized_text": normalized.normalized_text,
        "position_map": position_map_payload(normalized),
        "simhash": f"{fingerprints.simhash:016x}",
        "simhash_bands": list(fingerprints.simhash_bands),
        "minhash_bands": list(fingerprints.minhash_bands),
        "fingerprint_payload": {
            "simhash_version": SIMHASH_VERSION,
            "minhash_version": MINHASH_VERSION,
            "minhash_signature": list(fingerprints.minhash_signature),
        },
    }


class HashChannel:
    def __init__(self, client: Any, index_prefix: str, *,
                 clock: Callable[[], datetime] | None = None,
                 page_size: int = 50, max_pages: int | None = None,
                 allowed_namespaces: tuple[str, ...] = ("p01-batch-",)) -> None:
        # W-A 族④(a)（A2-06）：命名空间参数化闭集闸——默认 ("p01-batch-",)
        # 接受集与现役单态 regex 逐字节等价；配置面 fail-closed（四通道同型）。
        if (not allowed_namespaces
                or any(not re.fullmatch(r"[a-z0-9][a-z0-9-]*-", ns)
                       for ns in allowed_namespaces)):
            raise ValueError("allowed_namespaces must be lowercase kebab prefixes")
        if not any(re.fullmatch(re.escape(ns) + r"[A-Za-z0-9-]+-", index_prefix)
                   for ns in allowed_namespaces):
            raise ValueError(
                f"index prefix {index_prefix!r} outside allowed namespaces "
                f"{allowed_namespaces!r}")
        if not 1 <= page_size <= 50:
            raise ValueError("Hash page size must be 1..50")
        if max_pages is not None and max_pages < 1:
            raise ValueError("max_pages must be positive")
        self.client = client
        self.index_prefix = index_prefix
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.page_size = page_size
        self.max_pages = max_pages

    def search(self, request: RecallRequest, *,
               timeout_s: float | None = None) -> ChannelResult:
        # W2 ⑩①-5（N43 挂账清偿）：timeout_s 仅在场时透传 ES request_timeout
        # （None=零传参，现役逐字节）。
        started = self.clock()
        raw_key = bucket_key_if_indexable(request.text, request.scope_id,
                                           request.business_date, "raw",
                                           outer_trim_approved=request.outer_trim_approved)
        normalized_key = bucket_key_if_indexable(request.text, request.scope_id,
                                                  request.business_date, "normalized",
                                                  outer_trim_approved=request.outer_trim_approved)
        if raw_key is None or normalized_key is None:
            return ChannelResult("hash", "not_applicable", (), HASH_QUERY_VERSION,
                                 visible_seq=request.visible_seq, error_code="EMPTY_BODY",
                                 elapsed_ms=elapsed_ms_since(started, self.clock()))
        normalized = normalize_text(request.text,
                                    outer_trim_approved=request.outer_trim_approved)
        index = self.index_prefix + day_index(request.business_date)
        now = self.clock()
        if now.tzinfo is None:
            raise ValueError("service clock must be timezone aware")
        candidates: list[RecallCandidate] = []
        pages = 0
        after: list | None = None
        seen: set[str] = set()
        while True:
            body = {
                "size": self.page_size,
                "track_total_hits": False,
                "_source": ["record_id", "item_id", "arrival_seq"],
                "query": {"bool": {
                    "filter": [
                        {"term": {"scope_id": request.scope_id}},
                        {"term": {"business_date": request.business_date}},
                        {"term": {"normalizer_version": NORMALIZER_VERSION}},
                        {"term": {"preparation_state": "ready"}},
                        {"range": {"arrival_seq": {"lt": request.arrival_seq}}},
                        {"range": {"expires_at": {"gt": now.isoformat()}}},
                    ],
                    "must_not": [
                        {"term": {"record_id": request.record_id}},
                        {"term": {"item_id": request.item_id}},
                    ],
                    "should": [
                        {"term": {"raw_hash": normalized.raw_hash}},
                        {"term": {"normalized_hash": normalized.normalized_hash}},
                    ],
                    "minimum_should_match": 1,
                }},
                "sort": [{"arrival_seq": "asc"}, {"record_id": "asc"}],
            }
            if after is not None:
                body["search_after"] = after
            try:
                response = response_body(self.client.search(
                    index=index, body=body,
                    **({"request_timeout": timeout_s}
                       if timeout_s is not None else {})))
                if response.get("timed_out") is not False:
                    raise ValueError("ES search timed out or omitted status")
                shards = response.get("_shards")
                if not isinstance(shards, Mapping) or shards.get("failed") != 0:
                    raise ValueError("ES shard read failed")
                hits = response.get("hits", {}).get("hits")
                if not isinstance(hits, list) or len(hits) > self.page_size:
                    raise ValueError("ES hits are incomplete")
            except Exception:
                return ChannelResult("hash", "unavailable", tuple(candidates),
                                     HASH_QUERY_VERSION, pages, request.visible_seq,
                                     "ES_SEARCH_FAILED",
                                     elapsed_ms=elapsed_ms_since(started, self.clock()))
            pages += 1
            for hit in hits:
                try:
                    hit = response_body(hit)
                    record_id = hit["_id"]
                    if hit["_index"] != index or not isinstance(record_id, str):
                        raise ValueError("search identity differs")
                    fetched = response_body(self.client.get(index=index, id=record_id, realtime=True))
                    source = fetched["_source"]
                    if (fetched.get("found") is not True or
                            fetched.get("_index") != index or fetched.get("_id") != record_id or
                            not isinstance(source, Mapping) or source.get("record_id") != record_id):
                        raise ValueError("readback identity differs")
                    if not candidate_eligible(source, request, self.clock()):
                        continue
                    if source.get("normalizer_version") != NORMALIZER_VERSION or source.get("preparation_state") != "ready":
                        raise ValueError("candidate preparation version differs")
                    text = source.get("text")
                    if not isinstance(text, str) or not text.strip():
                        raise ValueError("candidate text is not indexable")
                    position_map = source.get("position_map")
                    if (not isinstance(position_map, Mapping) or
                            type(position_map.get("outer_trim_approved")) is not bool):
                        raise ValueError("candidate trim approval is missing")
                    rebuilt = normalize_text(
                        text, outer_trim_approved=position_map["outer_trim_approved"])
                    if (source.get("raw_hash") != rebuilt.raw_hash or
                            source.get("normalized_hash") != rebuilt.normalized_hash or
                            source.get("normalized_text") != rebuilt.normalized_text or
                            position_map != position_map_payload(rebuilt)):
                        raise ValueError("candidate indexed Hash differs from text")
                    subpaths = tuple(kind for kind, matches in (
                        ("raw", rebuilt.raw_hash == normalized.raw_hash and text == request.text),
                        ("normalized", rebuilt.normalized_hash == normalized.normalized_hash and
                         rebuilt.normalized_text == normalized.normalized_text),
                    ) if matches)
                    if subpaths and record_id not in seen:
                        seen.add(record_id)
                        candidates.append(RecallCandidate(
                            record_id, source["item_id"], source["arrival_seq"],
                            text, subpaths, 1.0, HASH_QUERY_VERSION,
                        ))
                except Exception:
                    return ChannelResult("hash", "unavailable", tuple(candidates),
                                         HASH_QUERY_VERSION, pages, request.visible_seq,
                                         "HASH_INDEX_MISMATCH",
                                         elapsed_ms=elapsed_ms_since(started, self.clock()))
            if len(hits) < self.page_size:
                break
            next_after = hits[-1].get("sort")
            if not isinstance(next_after, list) or len(next_after) != 2 or next_after == after:
                return ChannelResult("hash", "unavailable", tuple(candidates),
                                     HASH_QUERY_VERSION, pages, request.visible_seq,
                                     "ES_CURSOR_INVALID",
                                     elapsed_ms=elapsed_ms_since(started, self.clock()))
            after = next_after
            if self.max_pages is not None and pages >= self.max_pages:
                return ChannelResult("hash", "truncated", tuple(candidates),
                                     HASH_QUERY_VERSION, pages, request.visible_seq,
                                     "HASH_PAGE_LIMIT",
                                     elapsed_ms=elapsed_ms_since(started, self.clock()))
        return ChannelResult("hash", "complete", tuple(candidates),
                             HASH_QUERY_VERSION, pages, request.visible_seq,
                             coverage_complete=(request.visible_seq is not None and
                                                request.visible_seq >= request.arrival_seq - 1 and
                                                request.prepared_seq is not None and
                                                request.prepared_seq >= request.arrival_seq - 1),
                             prepared_seq=request.prepared_seq,
                             elapsed_ms=elapsed_ms_since(started, self.clock()))
