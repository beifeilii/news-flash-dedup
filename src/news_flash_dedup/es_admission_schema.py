"""P01 隔离测试索引的 strict 映射，与 10 §4 字段合同一致。"""

from __future__ import annotations

from datetime import date


def _fields(names: str, kind: str) -> dict:
    return {name: {"type": kind} for name in names.split()}


def _disabled(names: str) -> dict:
    return {name: {"type": "object", "enabled": False} for name in names.split()}


def day_index(business_date: str, kind: str = "items") -> str:
    try:
        if date.fromisoformat(business_date).isoformat() != business_date:
            raise ValueError("not canonical date")
    except (TypeError, ValueError) as error:
        raise ValueError("invalid business date") from error
    return f"news-dedup-{kind}-v1-" + business_date.replace("-", ".")


def request_mapping() -> dict:
    properties = _fields(
        "kind scope_id request_id item_id record_id business_fingerprint target_index business_date",
        "keyword",
    )
    properties.update(_fields("arrival_seq", "long"))
    properties.update(_fields("expires_at", "date"))
    return {"mappings": {"dynamic": "strict", "properties": properties}}


def control_mapping() -> dict:
    properties = _fields("kind scope_id business_date pipeline_version owner_id", "keyword")
    properties.update(_fields(
        "last_allocated_seq last_materialized_seq lexical_watermark decision_watermark", "long"
    ))
    properties.update(_fields("updated_at expires_at", "date"))
    properties.update(_disabled("pending checkpoint"))
    return {"mappings": {"dynamic": "strict", "properties": properties}}


def audit_mapping() -> dict:
    """13 个 strict 顶层字段，与 `probe_g0_recovery.py` 中原脚本版一致；P07-B 集中维护。"""
    fields = {name: {"type": "keyword"} for name in (
        "audit_id audit_type scope_id record_id candidate_record_id request_id business_date "
        "pipeline_version payload_hash").split()}
    fields.update({"arrival_seq": {"type": "long"}, "created_at": {"type": "date"},
                   "expires_at": {"type": "date"}, "payload": {"type": "object", "enabled": False}})
    return {"mappings": {"dynamic": "strict", "properties": fields}}


def item_mapping() -> dict:
    properties = _fields(
        "scope_id request_id item_id record_id schema_version pipeline_version embedding_space_id "
        "business_date raw_hash normalized_hash normalizer_version simhash simhash_bands "
        "minhash_bands entity_ids fact_type preparation_state vector_state task_state delivery_state "
        "audit_ids event_id",
        "keyword",
    )
    properties.update(_fields("arrival_seq", "long"))
    properties.update(_fields(
        "received_at accepted_at started_at completed_at expires_at next_delivery_at "
        "delivery_deadline_at", "date"
    ))
    properties.update(_fields("result_version callback_attempts", "integer"))
    properties.update(_fields("audit_complete", "boolean"))
    properties.update(_disabled(
        "position_map fingerprint_payload facts error task_lease delivery_lease diagnostics"
    ))
    properties["text"] = {"type": "text", "analyzer": "dedup_cjk"}
    properties["normalized_text"] = {"type": "text", "index": False}
    properties["callback_body"] = {"type": "text", "index": False}
    properties["result"] = {
        "dynamic": "strict",
        "properties": {
            "decision": {"type": "keyword"},
            "duplicate_ids": {"type": "keyword"},
            "reason": {"type": "text", "index": False},
        },
    }
    return {
        "settings": {
            "number_of_shards": 1,
            "number_of_replicas": 0,
            "refresh_interval": "1s",
            "analysis": {
                "analyzer": {
                    "dedup_cjk": {
                        "type": "custom", "tokenizer": "standard",
                        "filter": ["cjk_width", "lowercase", "cjk_bigram"],
                    }
                }
            },
        },
        "mappings": {"dynamic": "strict", "properties": properties},
    }
