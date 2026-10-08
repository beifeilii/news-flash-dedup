"""Elasticsearch 8.x 受理持久层。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


class AdmissionReadError(RuntimeError):
    """实时读取未能证明存在或明确的文档未命中（对齐 batch 侧 BatchReadError 语义）。"""


class ElasticsearchAdmissionStore:
    def __init__(self, client: Any, *, index_prefix: str = "") -> None:
        self.client = client
        if index_prefix and (not index_prefix.startswith("p01-g0-") or not index_prefix.endswith("-")):
            raise ValueError("G0 index prefix must be isolated and end with '-'")
        self.index_prefix = index_prefix

    def _physical(self, index: str) -> str:
        return self.index_prefix + index

    @staticmethod
    def _document(result: Mapping) -> dict:
        if (not isinstance(result.get("_source"), dict) or
                type(result.get("_seq_no")) is not int or result["_seq_no"] < 0 or
                type(result.get("_primary_term")) is not int or result["_primary_term"] < 1):
            raise AdmissionReadError("document source or version is incomplete")
        return {
            "source": result["_source"],
            "seq_no": result["_seq_no"],
            "primary_term": result["_primary_term"],
        }

    def _read_result(self, result: Any, index: str, key: str, operation: str) -> dict | None:
        # W2 批75（WA5 M-1，与 WA2-F4 同域并案）：对齐 batch_es_store.py:45-60 修复
        # 语义——身份（_index/_id）+shards 校验，只认带正确身份的明确文档未命中；
        # 旧实现 NotFoundError→None 会把索引/分片故障冒充成文档缺失。
        result = getattr(result, "body", result)
        if not isinstance(result, Mapping):
            raise AdmissionReadError("document response is invalid")
        if "error" in result:
            raise AdmissionReadError(operation + " item failed")
        shards = result.get("_shards")
        if shards is not None and (not isinstance(shards, Mapping) or shards.get("failed") != 0):
            raise AdmissionReadError("document shard read failed")
        if result.get("_index") != self._physical(index) or result.get("_id") != key:
            raise AdmissionReadError("document response identity differs")
        if result.get("found") is False and "_source" not in result:
            return None
        if result.get("found") is not True:
            raise AdmissionReadError("document existence is unconfirmed")
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
                    except AdmissionReadError:
                        pass
            # W2 批75：from error 保链（窗口Z2 P 批纪律同尺）。
            raise AdmissionReadError("get confirmation is unknown") from error
        return self._read_result(result, index, key, "get")

    def create(self, index: str, key: str, body: dict) -> None:
        self.client.index(index=self._physical(index), id=key, document=body, op_type="create", refresh=False)

    def replace(self, index: str, key: str, body: dict, seq_no: int, primary_term: int) -> None:
        self.client.index(
            index=self._physical(index), id=key, document=body, if_seq_no=seq_no,
            if_primary_term=primary_term, refresh=False,
        )

    @staticmethod
    def is_conflict(error: Exception) -> bool:
        from elasticsearch import ConflictError

        return isinstance(error, ConflictError)
