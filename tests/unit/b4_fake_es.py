"""B4 施工窗共享测试件：内存版 fake ES 客户端（禁集群，单元层专用）。

覆盖新测试面三件消费者：
- recall/prepare.py（F2 准备层）：search（未准备扫描/ready 证据扫描）、
  get/index（经 ElasticsearchBatchStore 实时 GET+CAS）、indices.refresh；
- recall/worker.py（F0 组合根）：search（未完成登记扫描）；
- persist/es_store.py（N36 真层幂等钉）：bulk create（409 冲突形态）、
  index(op_type=create / if_seq_no+if_primary_term)、create、get、indices.*。

仅实现本批测试用到的查询形态（bool.filter term/terms/range +
must_not term/terms + arrival_seq 升序 + size + _source 投影 +
track_total_hits），非通用 ES 模拟器；查询形态超出即 RuntimeError
（fail-closed，防测试静默绿）。
"""

from __future__ import annotations

import copy


def _not_found(index: str, doc_id: str):
    """与 test_batch_es_store.py:140-152 同式的 404 NotFoundError。"""
    import elasticsearch as es
    from elastic_transport import ApiResponseMeta, NodeConfig

    body = {"_index": index, "_id": doc_id, "found": False}
    return es.NotFoundError(
        "document not found",
        ApiResponseMeta(404, "1.1", {}, 0, NodeConfig("http", "localhost", 9200)),
        body,
    )


def _conflict(reason: str = "version conflict"):
    from elasticsearch import ConflictError

    return ConflictError(reason, None, None)


class _FakeIndicesApi:
    def __init__(self, client: "FakeESClient") -> None:
        self._client = client
        self.refreshed: list[str] = []
        self.created: list[tuple[str, object]] = []

    def exists(self, *, index: str):
        # elasticsearch-py 的 exists 返回 HeadApiResponse（bool 语义）；
        # 现役代码只用 bool(...)（persist/es_store.py:123）。
        return any(idx == index for idx, _ in self._client.docs) or \
            index in self._client.known_indices

    def create(self, *, index: str, body=None):
        self._client.known_indices.add(index)
        self.created.append((index, body))
        return {"acknowledged": True, "index": index}

    def refresh(self, *, index: str):
        self.refreshed.append(index)
        return {"_shards": {"total": 1, "successful": 1, "failed": 0}}


class FakeESClient:
    """内存文档仓库版 fake ES 客户端（doc 版本 _seq_no 0 基 / term 恒 1）。"""

    def __init__(self) -> None:
        self.docs: dict[tuple[str, str], dict] = {}
        self.known_indices: set[str] = set()
        self.indices = _FakeIndicesApi(self)
        self.calls: list[tuple] = []

    # ---------- 文档原语 ----------

    def put(self, index: str, doc_id: str, source: dict, *,
            seq_no: int = 0, primary_term: int = 1) -> None:
        """测试直接置文档（模拟物化/历史写入）。"""
        self.known_indices.add(index)
        self.docs[(index, doc_id)] = {
            "_source": copy.deepcopy(source),
            "_seq_no": seq_no,
            "_primary_term": primary_term,
        }

    def source(self, index: str, doc_id: str) -> dict | None:
        doc = self.docs.get((index, doc_id))
        return copy.deepcopy(doc["_source"]) if doc else None

    # ---------- 客户端 API（elasticsearch-py 鸭子形态） ----------

    def get(self, *, index: str, id: str, realtime: bool = True):
        self.calls.append(("get", index, id))
        doc = self.docs.get((index, id))
        if doc is None:
            raise _not_found(index, id)
        return {
            "_index": index, "_id": id, "found": True,
            "_source": copy.deepcopy(doc["_source"]),
            "_seq_no": doc["_seq_no"], "_primary_term": doc["_primary_term"],
        }

    def mget(self, *, docs: list, realtime: bool = True):
        # P0-T7：mget 面（batch_admission accept/逐条收口读回路径消费；
        # 真 ES 鸭子形态——found False=未命中，无 per-doc _shards）。
        self.calls.append(("mget", len(docs)))
        results = []
        for doc in docs:
            index, doc_id = doc["_index"], doc["_id"]
            existing = self.docs.get((index, doc_id))
            if existing is None:
                results.append({"_index": index, "_id": doc_id, "found": False})
            else:
                results.append({
                    "_index": index, "_id": doc_id, "found": True,
                    "_source": copy.deepcopy(existing["_source"]),
                    "_seq_no": existing["_seq_no"],
                    "_primary_term": existing["_primary_term"],
                })
        return {"docs": results}

    def index(self, *, index: str, id: str, document: dict,
              op_type: str | None = None, if_seq_no=None,
              if_primary_term=None, refresh=False):
        self.calls.append(("index", index, id, op_type, if_seq_no, if_primary_term))
        self.known_indices.add(index)
        existing = self.docs.get((index, id))
        if op_type == "create":
            if existing is not None:
                raise _conflict("document already exists")
            self.docs[(index, id)] = {
                "_source": copy.deepcopy(document), "_seq_no": 0, "_primary_term": 1,
            }
            return {"_index": index, "_id": id, "_seq_no": 0, "_primary_term": 1,
                    "result": "created"}
        if existing is None:
            raise _not_found(index, id)
        if if_seq_no is not None and (
                if_seq_no != existing["_seq_no"]
                or if_primary_term != existing["_primary_term"]):
            raise _conflict()
        new_seq = existing["_seq_no"] + 1
        self.docs[(index, id)] = {
            "_source": copy.deepcopy(document),
            "_seq_no": new_seq, "_primary_term": existing["_primary_term"],
        }
        return {"_index": index, "_id": id, "_seq_no": new_seq,
                "_primary_term": existing["_primary_term"], "result": "updated"}

    def create(self, *, index: str, id: str, document: dict, refresh=False):
        return self.index(index=index, id=id, document=document,
                          op_type="create", refresh=refresh)

    def bulk(self, *, operations: list, refresh=False):
        self.calls.append(("bulk", len(operations)))
        items = []
        cursor = 0
        while cursor < len(operations):
            action, body = operations[cursor], operations[cursor + 1]
            cursor += 2
            if len(action) != 1:
                raise RuntimeError("fake bulk: action must be single-key")
            op, meta = next(iter(action.items()))
            idx, doc_id = meta["_index"], meta["_id"]
            self.known_indices.add(idx)
            if op == "create":
                if (idx, doc_id) in self.docs:
                    items.append({"create": {
                        "_index": idx, "_id": doc_id, "status": 409,
                        "error": {"type": "version_conflict_engine_exception",
                                  "reason": "document already exists"},
                    }})
                else:
                    self.docs[(idx, doc_id)] = {
                        "_source": copy.deepcopy(body), "_seq_no": 0, "_primary_term": 1,
                    }
                    items.append({"create": {
                        "_index": idx, "_id": doc_id, "status": 201,
                        "_seq_no": 0, "_primary_term": 1,
                    }})
            elif op == "index":
                # B+ 批量落锤段 3（2026-10-08）：镜像 index() 版本语义——
                # if_seq_no/if_primary_term 与已存不符 → 409 版本竞争；
                # 带版本写不存在文档 → 404（真 ES 同形 version_conflict）。
                existing = self.docs.get((idx, doc_id))
                want_seq = meta.get("if_seq_no")
                want_term = meta.get("if_primary_term")
                if existing is None:
                    items.append({"index": {
                        "_index": idx, "_id": doc_id, "status": 404,
                        "error": {"type": "version_conflict_engine_exception",
                                  "reason": "document does not exist"},
                    }})
                elif want_seq is not None and (
                        want_seq != existing["_seq_no"]
                        or want_term != existing["_primary_term"]):
                    items.append({"index": {
                        "_index": idx, "_id": doc_id, "status": 409,
                        "error": {"type": "version_conflict_engine_exception",
                                  "reason": "version conflict"},
                    }})
                else:
                    new_seq = existing["_seq_no"] + 1
                    self.docs[(idx, doc_id)] = {
                        "_source": copy.deepcopy(body), "_seq_no": new_seq,
                        "_primary_term": existing["_primary_term"],
                    }
                    items.append({"index": {
                        "_index": idx, "_id": doc_id, "status": 200,
                        "_seq_no": new_seq,
                        "_primary_term": existing["_primary_term"],
                    }})
            else:
                raise RuntimeError(f"fake bulk: op {op!r} not implemented")
        return {"errors": any(
            "error" in next(iter(item.values())) for item in items),
            "items": items}

    # ---------- 最小查询求值 ----------

    @staticmethod
    def _match_clause(source: dict, clause: dict) -> bool:
        if len(clause) != 1:
            raise RuntimeError("fake search: clause must be single-key")
        kind, body = next(iter(clause.items()))
        if kind == "term":
            field, value = next(iter(body.items()))
            return source.get(field) == value
        if kind == "terms":
            field, values = next(iter(body.items()))
            return source.get(field) in values
        if kind == "range":
            field, bounds = next(iter(body.items()))
            value = source.get(field)
            if value is None:
                return False
            if "gt" in bounds and not value > bounds["gt"]:
                return False
            if "gte" in bounds and not value >= bounds["gte"]:
                return False
            if "lt" in bounds and not value < bounds["lt"]:
                return False
            if "lte" in bounds and not value <= bounds["lte"]:
                return False
            return True
        raise RuntimeError(f"fake search: unsupported clause {kind!r}")

    def search(self, *, index: str, body: dict):
        self.calls.append(("search", index, copy.deepcopy(body)))
        query = body.get("query", {})
        bool_q = query.get("bool") if isinstance(query, dict) else None
        if bool_q is None and query not in ({}, {"match_all": {}}):
            raise RuntimeError("fake search: only bool/match_all queries")
        bool_q = bool_q or {}
        matched = []
        for (idx, doc_id), doc in self.docs.items():
            if idx != index:
                continue
            source = doc["_source"]
            if any(not self._match_clause(source, clause)
                   for clause in bool_q.get("filter", [])):
                continue
            if any(self._match_clause(source, clause)
                   for clause in bool_q.get("must_not", [])):
                continue
            matched.append((doc_id, source))
        sort = body.get("sort") or [{"arrival_seq": "asc"}]
        sort_keys = []
        for spec in sort:
            field, order = next(iter(spec.items()))
            if order != "asc":
                raise RuntimeError("fake search: only asc sort")
            sort_keys.append(field)
        matched.sort(key=lambda item: tuple(
            item[1].get(field) if field != "record_id" else item[0]
            for field in sort_keys) + (item[0],))
        total = len(matched)
        size = body.get("size", 10)
        page = matched[:size]
        wanted = body.get("_source")
        hits = []
        for doc_id, source in page:
            shown = source if wanted is None else {
                key: source.get(key) for key in wanted if key in source}
            hits.append({"_index": index, "_id": doc_id, "_source": shown})
        # W-A 族④(c)-5（设计 §2.2 ④c 4 配套，additive）：search 成功返回体
        # 补 timed_out/_shards 字面（成功语义字面化，非放宽闸门）——护
        # 全真扫描路径绿件（worker scan G2/G3 缺键即拒的静默默认拆除）。
        return {"timed_out": False,
                "_shards": {"total": 1, "successful": 1, "failed": 0},
                "hits": {"total": {"value": total, "relation": "eq"},
                         "hits": hits}}


__all__ = ["FakeESClient"]
