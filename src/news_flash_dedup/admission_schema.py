"""受理日志与初始投递诊断的版本化应用层字段约束。"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

# W2 批80（L-10 收紧；探针 log/temp/winw2fd-probe-admission-schema.json 在案）：
# 时间三字段钉现役唯一产出面 admission._utc 的 canonical 形（微秒+Z，
# 探针 1001/1001 产出全匹配；金标零拒收）。
# text min_length **未收**——既有钉（test_batch_admission 空文走通受理全链并期望
# 202）与禁碰面硬冲突，翻转面已呈主窗口裁定；空文入桶仍由 P09 is_indexable 闸把守。
_UTC_TIMESTAMP_PATTERN = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$"


class PendingAdmissionV1(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    scope_id: str = Field(min_length=1)
    request_id: str = Field(min_length=1)
    item_id: str = Field(min_length=1)
    record_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    business_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    text: str
    delivery_route_ref: str = Field(min_length=1)
    trace_id: str | None
    received_at: str = Field(pattern=_UTC_TIMESTAMP_PATTERN)
    accepted_at: str = Field(pattern=_UTC_TIMESTAMP_PATTERN)
    business_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    expires_at: str = Field(pattern=_UTC_TIMESTAMP_PATTERN)
    schema_version: str = Field(min_length=1)
    pipeline_version: str = Field(min_length=1)
    embedding_space_id: str = Field(min_length=1)
    arrival_seq: int = Field(ge=1)


class DeliveryAdmissionV1(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    route_ref: str = Field(min_length=1)
    trace_id: str | None
