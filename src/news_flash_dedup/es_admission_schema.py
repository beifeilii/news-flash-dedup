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
    # P1a-T2：头文档游标化——+last_terminal/last_decision_seq/config_version
    # 三长字段（蓝图 1.2）。last_materialized_seq/pending 保留映射槽位：
    # 前者=过渡别名（P1a-T2 起与 last_terminal 同值平移，P1a-T4 摘除）
    # 兼容现役读面（prepare/probe/recovery_metrics），后者=G0 单槽形态
    # （admission.py 域共用本映射——strict 映射删槽即 G0 写 400，禁删）。
    properties = _fields("kind scope_id business_date pipeline_version owner_id", "keyword")
    properties.update(_fields(
        "last_allocated_seq last_materialized_seq last_terminal "
        "last_decision_seq config_version lexical_watermark decision_watermark", "long"
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


def tombstone_mapping() -> dict:
    """P0-T3 单条墓碑索引映射（news-dedup-tombstones-v1-*，按业务日）。

    纪律（设计稿 §5.3）：正文不进墓碑——只存 raw_hash 与身份/证据字段；
    error_summary 为有界人读摘要（不索引）。命名复用 ``day_index`` 的
    kind 形态（``day_index(business_date, "tombstones")``）；**不进
    required_indices**（fresh-namespace 闸形态不动——按需 ensure）。
    """
    properties = _fields(
        "scope_id business_date item_id record_id failed_stage error_class "
        "error_code raw_hash pipeline_version",
        "keyword",
    )
    properties.update(_fields("arrival_seq attempt_count", "long"))
    properties.update(_fields(
        "first_failed_at last_failed_at recorded_at late_success_at", "date"))
    properties.update(_fields("retryable", "boolean"))
    properties["error_summary"] = {"type": "text", "index": False}
    return {"mappings": {"dynamic": "strict", "properties": properties}}


def work_mapping() -> dict:
    """P1a-T1 任务文档索引映射（news-dedup-work-v1-*，按业务日）。

    蓝图=log\\P1a-预备设计-2026-10-11.md §1.1（33 字段逐位镜像
    ``work_queue/schema.WorkItemV1``——字段增删必须两处同步+
    ``test_work_queue`` 形状钉）。纪律：
    - ``text`` 为队列载荷正本（**不入检索面**：index=False，无
      analyzer——work 索引是任务队列不是召回目标；物化成功后 items
      主记录持检索正本）；
    - ``result`` 终态快照=对账面（enabled=False：按 doc ID 读取，
      不进查询）；
    - ``last_error_summary`` 有界人读摘要（不索引，T3 同款）；
    - 裁定（蓝图 §七-2，施工窗定）：work **进 required_indices**（受理
      主路径索引必须在场）；tombstones 维持 T3 按需 ensure 口径。
    """
    properties = _fields(
        "scope_id request_id item_id record_id business_fingerprint "
        "schema_version pipeline_version embedding_space_id business_date "
        "delivery_route_ref trace_id raw_hash task_state materialize_state "
        "terminal_reason lease_owner last_failure_class last_error_code",
        "keyword",
    )
    properties.update(_fields(
        "arrival_seq retry_count lease_generation", "long"))
    properties.update(_fields(
        "expires_at received_at accepted_at enqueued_at updated_at "
        "next_retry_at lease_expires_at first_failed_at last_failed_at",
        "date"))
    properties["text"] = {"type": "text", "index": False}
    properties["last_error_summary"] = {"type": "text", "index": False}
    properties["result"] = {"type": "object", "enabled": False}
    return {"mappings": {"dynamic": "strict", "properties": properties}}


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
