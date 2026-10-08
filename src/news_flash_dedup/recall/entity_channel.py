"""P11 仅用正文实体 ID 的补充召回通道。"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from typing import Any

from news_flash_dedup.es_admission_schema import day_index

from .entities import ENTITY_VERSION, EntityExtraction, extract_body_entities
from .es_gateway import candidate_eligible, elapsed_ms_since, response_body
from .models import ChannelResult, RecallCandidate, RecallRequest


# 窗口W2Fβ（WA3a-F4 条11）双源钉死：ENTITY_QUERY_VERSION（本通道
# query_version）与 entities.ENTITY_VERSION（词典/抽取版本，entity_ids 前缀）
# 为同值双定义——语义角色不同但 V1 必须恒等；任一升版须同步评估另一源
# （tests/unit/test_winw2fb_fingerprints_version.py 钉恒等）。
ENTITY_QUERY_VERSION = "entity_v1"


class EntityChannel:
    def __init__(self, client: Any, index_prefix: str, *,
                 clock: Callable[[], datetime] | None = None,
                 top_k: int = 30,
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
        if top_k != 30:
            raise ValueError("P11 V1 entity top_k is fixed at 30")
        self.client = client
        self.index_prefix = index_prefix
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.top_k = top_k

    def search(self, request: RecallRequest,
               *, extraction: EntityExtraction | None = None,
               timeout_s: float | None = None) -> ChannelResult:
        # W2 ⑩①-5（N43 挂账清偿）：timeout_s 仅在场时透传 ES request_timeout
        # （None=零传参，现役逐字节）。
        started = self.clock()
        extraction = extraction if extraction is not None else extract_body_entities(request.text)
        if any(not 0 <= mention.start < mention.end <= len(request.text) or
               request.text[mention.start:mention.end] != mention.raw or
               not mention.entity_id.startswith(ENTITY_VERSION + "|")
               for mention in extraction.mentions):
            return ChannelResult("entity", "unavailable", (), ENTITY_QUERY_VERSION,
                                 visible_seq=request.visible_seq,
                                 error_code="ENTITY_EVIDENCE_INVALID",
                                 prepared_seq=request.prepared_seq,
                                 elapsed_ms=elapsed_ms_since(started, self.clock()))
        if not extraction.mentions:
            status = "not_applicable" if extraction.complete else "unavailable"
            return ChannelResult("entity", status, (), ENTITY_QUERY_VERSION,
                                 visible_seq=request.visible_seq,
                                 error_code=None if extraction.complete else "ENTITY_UNRESOLVED",
                                 prepared_seq=request.prepared_seq,
                                 elapsed_ms=elapsed_ms_since(started, self.clock()))
        index = self.index_prefix + day_index(request.business_date)
        now = self.clock()
        if now.tzinfo is None:
            raise ValueError("service clock must be timezone aware")
        entity_ids = list(extraction.entity_ids)
        found: list[RecallCandidate] = []
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
                    {"terms": {"entity_ids": entity_ids}},
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
            if not isinstance(hits, list) or len(hits) > self.top_k:
                raise ValueError("ES hits are incomplete")
        except Exception:
            return ChannelResult("entity", "unavailable", (), ENTITY_QUERY_VERSION,
                                 visible_seq=request.visible_seq,
                                 error_code="ES_SEARCH_FAILED",
                                 prepared_seq=request.prepared_seq,
                                 elapsed_ms=elapsed_ms_since(started, self.clock()))
        for hit in hits:
            try:
                hit = response_body(hit)
                record_id = hit["_id"]
                if hit["_index"] != index or not isinstance(record_id, str):
                    raise ValueError("search identity differs")
                fetched = response_body(self.client.get(index=index, id=record_id,
                                                        realtime=True))
                source = fetched["_source"]
                if (fetched.get("found") is not True or
                        fetched.get("_index") != index or fetched.get("_id") != record_id or
                        not isinstance(source, Mapping) or source.get("record_id") != record_id):
                    raise ValueError("readback identity differs")
                if not candidate_eligible(source, request, self.clock()):
                    continue
                candidate_text = source.get("text")
                stored_ids = source.get("entity_ids")
                if not isinstance(candidate_text, str) or not isinstance(stored_ids, list):
                    raise ValueError("candidate entity fields are invalid")
                rebuilt = extract_body_entities(candidate_text)
                shared = set(entity_ids) & set(stored_ids) & set(rebuilt.entity_ids)
                if not shared:
                    raise ValueError("indexed entity lacks original-body evidence")
                found.append(RecallCandidate(
                    record_id, source["item_id"], source["arrival_seq"], candidate_text,
                    ("entity",), float(len(shared)), ENTITY_QUERY_VERSION,
                ))
            except Exception:
                return ChannelResult("entity", "unavailable", tuple(found),
                                     ENTITY_QUERY_VERSION, pages=1,
                                     visible_seq=request.visible_seq,
                                     error_code="ENTITY_INDEX_MISMATCH",
                                     prepared_seq=request.prepared_seq,
                                     elapsed_ms=elapsed_ms_since(started, self.clock()))
        found.sort(key=lambda item: (-item.score, item.arrival_seq, item.record_id))
        status = "complete" if extraction.complete else "unavailable"
        # 窗口W2Fβ（WA3a-F1 条9）：hits==top_k 即页可能截断（size=top_k、
        # track_total_hits=False，页外命中不可知）——P11 L19「无桶/页截断」
        # 要件，截断条件并入覆盖布尔式记账（与 BM25 申报口径同形），
        # 不冒充完整覆盖。
        return ChannelResult(
            "entity", status, tuple(found), ENTITY_QUERY_VERSION, pages=1,
            visible_seq=request.visible_seq,
            error_code=None if extraction.complete else "ENTITY_EXTRACTION_INCOMPLETE",
            coverage_complete=(extraction.complete and request.visible_seq is not None
                               and request.visible_seq >= request.arrival_seq - 1
                               and request.prepared_seq is not None
                               and request.prepared_seq >= request.arrival_seq - 1
                               and len(hits) < self.top_k),
            prepared_seq=request.prepared_seq,
            elapsed_ms=elapsed_ms_since(started, self.clock()),
        )
