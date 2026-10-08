"""P10 SimHash/MinHash 双子路径合并为近重复主通道。"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from typing import Any

from news_flash_dedup.es_admission_schema import day_index
from news_flash_dedup.text import NORMALIZER_VERSION, normalize_text

from .fingerprints import (
    MINHASH_VERSION,
    SIMHASH_VERSION,
    Fingerprints,
    build_fingerprints,
    hamming_distance,
    minhash_similarity,
)
from .es_gateway import (
    candidate_eligible,
    elapsed_ms_since,
    position_map_payload,
    response_body,
)
from .models import ChannelResult, RecallCandidate, RecallRequest


NEAR_QUERY_VERSION = "near_v1"
_log = logging.getLogger(__name__)


class NearChannel:
    def __init__(self, client: Any, index_prefix: str, *,
                 clock: Callable[[], datetime] | None = None,
                 bucket_limit: int = 500, top_k: int = 40,
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
        if bucket_limit != 500:
            raise ValueError("P10 V1 bucket limit is fixed at 500")
        if top_k != 40:
            raise ValueError("P10 V1 near top_k is fixed at 40")
        self.client = client
        self.index_prefix = index_prefix
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.bucket_limit = bucket_limit
        self.top_k = top_k

    def _candidate_list(self, loaded: dict[str, tuple[Mapping, Fingerprints] | None],
                        paths: dict[str, set[str]],
                        query: Fingerprints) -> tuple[tuple[RecallCandidate, ...], int]:
        candidates: list[RecallCandidate] = []
        for record_id, subpaths in paths.items():
            if not subpaths:
                continue
            item = loaded[record_id]
            if item is None:
                continue
            source, fingerprint = item
            scores = []
            if "simhash" in subpaths:
                scores.append(1 - hamming_distance(query.simhash, fingerprint.simhash) / 64)
            if "minhash" in subpaths:
                scores.append(minhash_similarity(query.minhash_signature,
                                                 fingerprint.minhash_signature))
            candidates.append(RecallCandidate(
                record_id, source["item_id"], source["arrival_seq"], source["text"],
                tuple(kind for kind in ("simhash", "minhash") if kind in subpaths),
                max(scores), NEAR_QUERY_VERSION,
            ))
        candidates.sort(key=lambda candidate: (-candidate.score, candidate.arrival_seq,
                                               candidate.record_id))
        # M-12/L 窗口P：top_k 单源化——原硬编码 [:40] 与构造器 top_k=40 重复。
        return tuple(candidates[:self.top_k]), max(0, len(candidates) - self.top_k)

    def search(self, request: RecallRequest, *,
               timeout_s: float | None = None) -> ChannelResult:
        # W2 ⑩①-5（N43 挂账清偿）：timeout_s 仅在场时透传 ES request_timeout
        # （None=零传参，现役逐字节）。
        started = self.clock()
        query = build_fingerprints(request.text,
                                   outer_trim_approved=request.outer_trim_approved)
        if query is None:
            return ChannelResult("near", "not_applicable", (), NEAR_QUERY_VERSION,
                                 visible_seq=request.visible_seq, error_code="EMPTY_BODY",
                                 elapsed_ms=elapsed_ms_since(started, self.clock()))
        now = self.clock()
        if now.tzinfo is None:
            raise ValueError("service clock must be timezone aware")
        index = self.index_prefix + day_index(request.business_date)
        loaded: dict[str, tuple[Mapping, Fingerprints] | None] = {}
        paths: dict[str, set[str]] = {}
        truncated: list[str] = []
        pages = 0

        def result(status: str, error_code: str | None = None) -> ChannelResult:
            candidates, excluded = self._candidate_list(loaded, paths, query)
            return ChannelResult(
                "near", status, candidates, NEAR_QUERY_VERSION, pages,
                request.visible_seq, error_code, tuple(truncated),
                coverage_complete=(status == "complete" and request.visible_seq is not None
                                   and request.visible_seq >= request.arrival_seq - 1
                                   and request.prepared_seq is not None
                                   and request.prepared_seq >= request.arrival_seq - 1),
                topk_excluded=excluded,
                prepared_seq=request.prepared_seq,
                elapsed_ms=elapsed_ms_since(started, self.clock()),
            )

        for kind, field, keys in (
            ("simhash", "simhash_bands", query.simhash_bands),
            ("minhash", "minhash_bands", query.minhash_bands),
        ):
            for key in keys:
                body = {
                    "size": self.bucket_limit + 1,
                    "track_total_hits": False,
                    "_source": ["record_id"],
                    "query": {"bool": {
                        "filter": [
                            {"term": {"scope_id": request.scope_id}},
                            {"term": {"business_date": request.business_date}},
                            {"term": {"normalizer_version": NORMALIZER_VERSION}},
                            {"term": {"preparation_state": "ready"}},
                            {"term": {field: key}},
                            {"range": {"arrival_seq": {"lt": request.arrival_seq}}},
                            {"range": {"expires_at": {"gt": now.isoformat()}}},
                        ],
                        "must_not": [
                            {"term": {"record_id": request.record_id}},
                            {"term": {"item_id": request.item_id}},
                        ],
                    }},
                    "sort": [{"arrival_seq": "asc"}, {"record_id": "asc"}],
                }
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
                    if not isinstance(hits, list) or len(hits) > self.bucket_limit + 1:
                        raise ValueError("ES bucket result is incomplete")
                except Exception as error:
                    # 窗口W2Fβ（WA3b 条66）：吞异常前留证——返回面（status/
                    # error_code/部分候选）不变，根因类型入 warning 日志。
                    _log.warning(
                        "near channel bucket query failed, channel unavailable: "
                        "%s", type(error).__name__)
                    return result("unavailable", "ES_SEARCH_FAILED")
                pages += 1
                if len(hits) > self.bucket_limit:
                    truncated.append(key)
                for hit in hits[:self.bucket_limit]:
                    try:
                        hit = response_body(hit)
                        record_id = hit["_id"]
                        if hit["_index"] != index or not isinstance(record_id, str):
                            raise ValueError("search identity differs")
                        if record_id not in loaded:
                            fetched = response_body(self.client.get(index=index, id=record_id,
                                                            realtime=True))
                            source = fetched["_source"]
                            if (fetched.get("found") is not True or
                                    fetched.get("_index") != index or
                                    fetched.get("_id") != record_id or
                                    not isinstance(source, Mapping) or
                                    source.get("record_id") != record_id):
                                raise ValueError("readback identity differs")
                            if not candidate_eligible(source, request, self.clock()):
                                loaded[record_id] = None
                                continue
                            if (source.get("normalizer_version") != NORMALIZER_VERSION or
                                    source.get("preparation_state") != "ready"):
                                raise ValueError("candidate preparation version differs")
                            text = source.get("text")
                            if not isinstance(text, str):
                                raise ValueError("candidate text is invalid")
                            position_map = source.get("position_map")
                            if (not isinstance(position_map, Mapping) or
                                    type(position_map.get("outer_trim_approved")) is not bool):
                                raise ValueError("candidate trim approval is missing")
                            rebuilt = normalize_text(
                                text, outer_trim_approved=position_map["outer_trim_approved"])
                            fingerprint = build_fingerprints(
                                text, outer_trim_approved=position_map["outer_trim_approved"])
                            payload = source.get("fingerprint_payload")
                            if (fingerprint is None or not isinstance(payload, Mapping) or
                                    payload.get("simhash_version") != SIMHASH_VERSION or
                                    payload.get("minhash_version") != MINHASH_VERSION or
                                    source.get("normalized_text") != rebuilt.normalized_text or
                                    position_map != position_map_payload(rebuilt) or
                                    source.get("simhash") != f"{fingerprint.simhash:016x}" or
                                    source.get("simhash_bands") != list(fingerprint.simhash_bands) or
                                    source.get("minhash_bands") != list(fingerprint.minhash_bands) or
                                    payload.get("minhash_signature") != list(fingerprint.minhash_signature)):
                                raise ValueError("indexed fingerprint differs from text")
                            loaded[record_id] = (source, fingerprint)
                        if loaded[record_id] is None:
                            continue
                        _, fingerprint = loaded[record_id]
                        if kind == "simhash":
                            if hamming_distance(query.simhash, fingerprint.simhash) <= 3:
                                paths.setdefault(record_id, set()).add("simhash")
                        elif key in fingerprint.minhash_bands:
                            paths.setdefault(record_id, set()).add("minhash")
                    except Exception as error:
                        # 窗口W2Fβ（WA3b 条66）：同上证——候选对拍失败留根因类型。
                        _log.warning(
                            "near channel candidate verification failed, channel "
                            "unavailable: %s", type(error).__name__)
                        return result("unavailable", "FINGERPRINT_INDEX_MISMATCH")
        return result("truncated" if truncated else "complete",
                      "BUCKET_TRUNCATED" if truncated else None)
