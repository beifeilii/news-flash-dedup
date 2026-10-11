"""隔离批次受理日志的 Elasticsearch 8.x 存储适配。

W2 批36 防误接线标注（历史登记）：本模块 `ElasticsearchBatchStore` 强制以
p01-batch- 前缀拼接物理索引名；`es_admission_index.provision_isolated_indices`
曾创建无前缀 canonical 名——两者**不可配对**（配对必 404），命名四位一体
裁定曾挂用户域（D25）。

批36 登记的不可配对情形已于 09-28 经 W2Fδ2 四位一体裁定落地消除（命名=用户
授权主窗口候选：带前缀建/闸/访/清一体；10 号文无前缀明文规格已核）——配对
已连通（钉实证）；调用方仍不得绕过裁定形态自行拼接对齐。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any


class BatchReadError(RuntimeError):
    """实时读取未能证明存在或明确的文档未命中。"""


class PermanentBulkError(RuntimeError):
    """已确认的不可重试逐项写入错误，须隔离原批次。"""

    def __init__(self, message: str, *, failed_document_positions: tuple[int, ...] = ()) -> None:
        super().__init__(message)
        # 仅当完整响应已逐项核验 index/id/status 后才能定位到文档序号。
        self.failed_document_positions = failed_document_positions


class ElasticsearchBatchStore:
    def __init__(self, client: Any, *, index_prefix: str) -> None:
        if not re.fullmatch(r"p01-batch-[A-Za-z0-9-]+-", index_prefix):
            raise ValueError("batch prototype requires a p01-batch- run prefix")
        self.client = client
        self.index_prefix = index_prefix

    def _physical(self, index: str) -> str:
        return self.index_prefix + index

    @staticmethod
    def _document(result: Mapping) -> dict:
        if (not isinstance(result.get("_source"), dict) or
                type(result.get("_seq_no")) is not int or result["_seq_no"] < 0 or
                type(result.get("_primary_term")) is not int or result["_primary_term"] < 1):
            raise BatchReadError("document source or version is incomplete")
        return {
            "source": result["_source"],
            "seq_no": result["_seq_no"],
            "primary_term": result["_primary_term"],
        }

    def _read_result(self, result: Any, index: str, key: str, operation: str) -> dict | None:
        result = getattr(result, "body", result)
        if not isinstance(result, Mapping):
            raise BatchReadError("document response is invalid")
        if "error" in result:
            raise BatchReadError(operation + " item failed")
        shards = result.get("_shards")
        if shards is not None and (not isinstance(shards, Mapping) or shards.get("failed") != 0):
            raise BatchReadError("document shard read failed")
        if result.get("_index") != self._physical(index) or result.get("_id") != key:
            raise BatchReadError("document response identity differs")
        if result.get("found") is False and "_source" not in result:
            return None
        if result.get("found") is not True:
            raise BatchReadError("document existence is unconfirmed")
        return self._document(result)

    def get(self, index: str, key: str) -> dict | None:
        try:
            result = self.client.get(index=self._physical(index), id=key, realtime=True)
        except Exception as error:
            # 404 也可能是索引或分片故障，只认带正确身份的明确文档未命中。
            if getattr(error, "status_code", None) == 404:
                body = getattr(error, "body", None)
                if isinstance(body, Mapping) and body.get("found") is False:
                    try:
                        return self._read_result(body, index, key, "get")
                    except BatchReadError:
                        pass
            # 窗口Z2（P 批保链纪律）：from None → from error——原异常即失败
            # 根因，入 __cause__（milvus_client.py:32-34 同款注记）。
            raise BatchReadError("get confirmation is unknown") from error
        return self._read_result(result, index, key, "get")

    def mget(self, keys: list[tuple[str, str]]) -> list[dict | None]:
        if not keys:
            return []
        try:
            result = self.client.mget(
                docs=[{"_index": self._physical(index), "_id": key} for index, key in keys],
                realtime=True,
            )
        except Exception as error:
            # 窗口Z2（P 批保链纪律）：from None → from error
            raise BatchReadError("mget confirmation is unknown") from error
        result = getattr(result, "body", result)
        docs = result.get("docs") if isinstance(result, Mapping) else None
        if not isinstance(docs, list) or len(docs) != len(keys):
            raise BatchReadError("mget returned an incomplete result")
        return [self._read_result(doc, index, key, "mget")
                for doc, (index, key) in zip(docs, keys)]

    def create(self, index: str, key: str, body: dict) -> None:
        self.client.index(index=self._physical(index), id=key, document=body,
                          op_type="create", refresh=False)

    def replace(self, index: str, key: str, body: dict, seq_no: int, primary_term: int) -> None:
        self.client.index(index=self._physical(index), id=key, document=body,
                          if_seq_no=seq_no, if_primary_term=primary_term, refresh=False)

    def bulk_create(self, documents: list[tuple[str, str, dict]]) -> None:
        if not documents:
            return
        operations: list[dict] = []
        for index, key, body in documents:
            operations.extend((
                {"create": {"_index": self._physical(index), "_id": key}},
                body,
            ))
        try:
            result = self.client.bulk(operations=operations, refresh=False)
        except Exception as error:
            status = getattr(error, "status_code", None)
            if type(status) is int and 400 <= status < 500 and status not in (408, 409, 429):
                # 窗口Z2（P 批保链纪律）：from None → from error
                raise PermanentBulkError("bulk request permanently rejected") from error
            raise RuntimeError("bulk confirmation is unknown") from error
        result = getattr(result, "body", result)
        items = result.get("items") if isinstance(result, Mapping) else None
        if not isinstance(items, list) or len(items) != len(documents):
            raise RuntimeError("bulk item count is incomplete")
        statuses: list[int] = []
        for item, (index, key, _) in zip(items, documents):
            created = item.get("create") if isinstance(item, Mapping) and len(item) == 1 else None
            if (not isinstance(created, Mapping) or
                    created.get("_index") != self._physical(index) or created.get("_id") != key or
                    type(created.get("status")) is not int):
                raise RuntimeError("bulk item identity or status is incomplete")
            status = created["status"]
            if status == 201 and "error" in created:
                raise RuntimeError("bulk item outcome is inconsistent")
            statuses.append(status)
        permanent = tuple(position for position, status in enumerate(statuses)
                          if 400 <= status < 500 and status not in (408, 409, 429))
        if permanent:
            raise PermanentBulkError("bulk item permanently rejected",
                                     failed_document_positions=permanent)
        if any(status not in (201, 409) for status in statuses):
            raise RuntimeError("bulk item confirmation is unknown")

    @staticmethod
    def is_conflict(error: Exception) -> bool:
        from elasticsearch import ConflictError

        return isinstance(error, ConflictError)

    def bulk_create_classified(self, documents: list[tuple[str, str, dict]]):
        """P0-T4 逐条结果解析 bulk（新方法——``bulk_create`` 与既有调用方零 diff）。

        返回逐文档 ``BulkDocumentOutcome`` 元组（位置=输入序，身份/状态已核验）：
        - 201 → 成功（若响应仍带 error 字段=不一致结构，按不可解析抛出，
          调用方以全量读回对拍兜底）；
        - 409 → UNKNOWN_WRITE（可能已存在：读回身份对拍收口）；
        - 408/429/≥500 → TRANSIENT_INFRA（确定失败：重执收敛——若实际
          已落，重执撞 409 走读回对拍自愈）；
        - 其余 4xx → ITEM_PERMANENT（确定性单条拒绝——唯一墓簿入口）；
        - 请求级异常（传输/请求级 4xx）与 ``bulk_create`` 同形抛出
          （RuntimeError 未知 / PermanentBulkError 请求级拒绝）——调用方
          走既有全量读回/结构性隔离路径。

        注：``bulk_document_outcome`` 为函数内延迟导入（materialize_failure
        顶层反向引用本模块的 PermanentBulkError——顶层互导成环，函数内
        收口即无环）。
        """
        from .materialize_failure import bulk_document_outcome

        if not documents:
            return ()
        operations: list[dict] = []
        for index, key, body in documents:
            operations.extend((
                {"create": {"_index": self._physical(index), "_id": key}},
                body,
            ))
        try:
            result = self.client.bulk(operations=operations, refresh=False)
        except Exception as error:
            status = getattr(error, "status_code", None)
            if type(status) is int and 400 <= status < 500 and status not in (408, 409, 429):
                # 窗口Z2（P 批保链纪律）：from None → from error
                raise PermanentBulkError("bulk request permanently rejected") from error
            raise RuntimeError("bulk confirmation is unknown") from error
        result = getattr(result, "body", result)
        items = result.get("items") if isinstance(result, Mapping) else None
        if not isinstance(items, list) or len(items) != len(documents):
            raise RuntimeError("bulk item count is incomplete")
        outcomes: list = []
        for item, (index, key, _) in zip(items, documents):
            created = item.get("create") if isinstance(item, Mapping) and len(item) == 1 else None
            if (not isinstance(created, Mapping) or
                    created.get("_index") != self._physical(index) or created.get("_id") != key or
                    type(created.get("status")) is not int):
                raise RuntimeError("bulk item identity or status is incomplete")
            status = created["status"]
            if status == 201 and "error" in created:
                raise RuntimeError("bulk item outcome is inconsistent")
            error_field = created.get("error")
            error_type = ""
            error_reason = ""
            if isinstance(error_field, Mapping):
                error_type = str(error_field.get("type", ""))
                error_reason = str(error_field.get("reason", ""))
            outcomes.append(bulk_document_outcome(
                position=len(outcomes), index=index, key=key, status=status,
                error_type=error_type, error_reason=error_reason))
        return tuple(outcomes)
