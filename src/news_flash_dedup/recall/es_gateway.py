"""召回通道共用的 ES 响应与候选资格校验。"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from news_flash_dedup.text import NormalizationResult

from .models import RecallRequest


def response_body(result: Any) -> Mapping:
    body = getattr(result, "body", result)
    if not isinstance(body, Mapping):
        raise ValueError("ES response is not an object")
    return body


def elapsed_ms_since(started: datetime, now: datetime) -> float:
    """通道自报耗时（毫秒）= service clock 两次读数之差（窗口W2Fβ 条58）。

    固定钟测试下为 0.0；不新增 tzinfo 校验面（各通道既有时钟闸位置不变）。
    """
    return (now - started).total_seconds() * 1000


def candidate_eligible(source: Mapping, request: RecallRequest, now: datetime) -> bool:
    if (source.get("scope_id") != request.scope_id or
            source.get("business_date") != request.business_date or
            source.get("record_id") == request.record_id or
            source.get("item_id") == request.item_id):
        return False
    seq = source.get("arrival_seq")
    if type(seq) is not int or seq >= request.arrival_seq or seq < 1:
        return False
    expiry = source.get("expires_at")
    if not isinstance(expiry, str):
        raise ValueError("candidate expiry is missing")
    # W3F（W3c-F-1，同类不同步收口，对齐 vector_store.py:266 修法——
    # 变换行；定点注释 :263-265；W2 修复波 2 (a)-3 锚勘正）：
    # replace("Z") 定点化——仅尾 Z 是时区指示符，替换为 +00:00；串内其他
    # 位置的 Z 不再被误改写，畸形串交由 fromisoformat 拒绝（fail-closed
    # 不变；新旧变换式 12 候选串 outcome 对拍全同，探针
    # log\temp\winw3f-f1-z-probe.txt 在案）。
    parsed = datetime.fromisoformat(
        expiry[:-1] + "+00:00" if expiry.endswith("Z") else expiry)
    if parsed.tzinfo is None:
        raise ValueError("candidate expiry has no timezone")
    return now < parsed


def position_map_payload(normalized: NormalizationResult) -> dict:
    return {
        "spans": [list(span) for span in normalized.position_map],
        "removed_spans": [list(span) for span in normalized.removed_spans],
        "transforms": [{
            "rule_id": transform.rule_id,
            "input_span": list(transform.input_span),
            "output_span": list(transform.output_span),
        } for transform in normalized.transforms],
        "outer_trim_approved": normalized.outer_trim_approved,
    }
