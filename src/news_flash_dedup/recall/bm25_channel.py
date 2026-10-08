"""P11 基于 ES dedup_cjk 分析器的正文 BM25 通道。"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from typing import Any

from news_flash_dedup.es_admission_schema import day_index

from .es_gateway import candidate_eligible, elapsed_ms_since, response_body
from .models import ChannelResult, RecallCandidate, RecallRequest


BM25_QUERY_VERSION = "bm25_v1"
_SEGMENT_CHARS = 512
_SEGMENT_OVERLAP = 64
# 窗口W2Fβ（WB2-L3 条58）：k1=1.2/b=0.75 显式配置声明——值同 09 §5.4 初值
# （亦即 ES 内置 BM25 默认值），防 ES 默认漂移静默改变打分。P11 §4 在案：
# 主索引模板未显式设置 similarity，真固定 k1/b 须按 10 §4.2 升版模板并做
# 兼容回放——本声明不改 mapping、不改查询体，仅钉值防漂移。
BM25_K1 = 1.2
BM25_B = 0.75


def _segments(text: str) -> tuple[str, ...]:
    result = []
    start = 0
    while start < len(text):
        end = min(start + _SEGMENT_CHARS, len(text))
        result.append(text[start:end])
        if end == len(text):
            break
        start = end - _SEGMENT_OVERLAP
    return tuple(result)


class BM25Channel:
    def __init__(self, client: Any, index_prefix: str, *,
                 clock: Callable[[], datetime] | None = None,
                 top_k: int = 40, max_segments: int | None = None,
                 allowed_namespaces: tuple[str, ...] = ("p01-batch-",)) -> None:
        # W-A 族④(a)（A2-06）：命名空间参数化闭集闸——默认 ("p01-batch-",)
        # 接受集与现役单态 regex 逐字节等价（re.escape 无元字符）；配置面
        # fail-closed（空元组/空串/大写/无尾横线一律拒，防"空串=全放行"
        # 与大小写漂移）。
        if (not allowed_namespaces
                or any(not re.fullmatch(r"[a-z0-9][a-z0-9-]*-", ns)
                       for ns in allowed_namespaces)):
            raise ValueError("allowed_namespaces must be lowercase kebab prefixes")
        if not any(re.fullmatch(re.escape(ns) + r"[A-Za-z0-9-]+-", index_prefix)
                   for ns in allowed_namespaces):
            raise ValueError(
                f"index prefix {index_prefix!r} outside allowed namespaces "
                f"{allowed_namespaces!r}")
        if top_k != 40:
            raise ValueError("P11 V1 BM25 top_k is fixed at 40")
        if max_segments is not None and max_segments < 1:
            raise ValueError("max_segments must be positive")
        self.client = client
        self.index_prefix = index_prefix
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.top_k = top_k
        self.max_segments = max_segments

    def search(self, request: RecallRequest, *,
               timeout_s: float | None = None) -> ChannelResult:
        # W2 ⑩①-5（N43 挂账清偿）：timeout_s 仅在场时透传 ES request_timeout
        # （None=零传参，现役逐字节）。
        started = self.clock()
        if not request.text.strip():
            return ChannelResult("bm25", "not_applicable", (), BM25_QUERY_VERSION,
                                 visible_seq=request.visible_seq, error_code="EMPTY_BODY",
                                 elapsed_ms=elapsed_ms_since(started, self.clock()))
        index = self.index_prefix + day_index(request.business_date)
        now = self.clock()
        if now.tzinfo is None:
            raise ValueError("service clock must be timezone aware")
        segments = _segments(request.text)
        found: dict[str, RecallCandidate] = {}
        pages = 0

        def result(status: str, error_code: str | None = None) -> ChannelResult:
            ranked = sorted(found.values(),
                            key=lambda item: (-item.score, item.arrival_seq,
                                              item.record_id))
            return ChannelResult(
                "bm25", status, tuple(ranked[:self.top_k]), BM25_QUERY_VERSION,
                pages, request.visible_seq, error_code,
                coverage_complete=(status == "complete" and request.visible_seq is not None
                                   and request.visible_seq >= request.arrival_seq - 1),
                topk_excluded=max(0, len(ranked) - self.top_k),
                elapsed_ms=elapsed_ms_since(started, self.clock()),
            )

        for segment in segments:
            if self.max_segments is not None and pages >= self.max_segments:
                return result("truncated", "BM25_SEGMENT_LIMIT")
            body = {
                "size": self.top_k,
                "track_total_hits": False,
                "_source": ["record_id"],
                "query": {"bool": {
                    "filter": [
                        {"term": {"scope_id": request.scope_id}},
                        {"term": {"business_date": request.business_date}},
                        {"range": {"arrival_seq": {"lt": request.arrival_seq}}},
                        {"range": {"expires_at": {"gt": now.isoformat()}}},
                    ],
                    "must_not": [
                        {"term": {"record_id": request.record_id}},
                        {"term": {"item_id": request.item_id}},
                    ],
                    "must": [{"match": {"text": {"query": segment,
                                                 "operator": "or"}}}],
                }},
                "sort": [{"_score": "desc"}, {"record_id": "asc"}],
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
                if not isinstance(hits, list) or len(hits) > self.top_k:
                    raise ValueError("ES hits are incomplete")
            except Exception:
                return result("unavailable", "ES_SEARCH_FAILED")
            pages += 1
            for hit in hits:
                try:
                    hit = response_body(hit)
                    record_id = hit["_id"]
                    score = hit["_score"]
                    if (hit["_index"] != index or not isinstance(record_id, str) or
                            type(score) not in (int, float) or not math.isfinite(score)):
                        raise ValueError("search hit identity or score is invalid")
                    fetched = response_body(self.client.get(index=index, id=record_id,
                                                            realtime=True))
                    source = fetched["_source"]
                    if (fetched.get("found") is not True or
                            fetched.get("_index") != index or fetched.get("_id") != record_id or
                            not isinstance(source, Mapping) or source.get("record_id") != record_id):
                        raise ValueError("readback identity differs")
                    if not candidate_eligible(source, request, self.clock()):
                        continue
                    text = source.get("text")
                    if not isinstance(text, str):
                        raise ValueError("candidate text is invalid")
                    candidate = RecallCandidate(
                        record_id, source["item_id"], source["arrival_seq"], text,
                        ("bm25",), float(score), BM25_QUERY_VERSION,
                    )
                    old = found.get(record_id)
                    if old is None or candidate.score > old.score:
                        found[record_id] = candidate
                except Exception:
                    return result("unavailable", "ES_READ_FAILED")
        return result("complete")
