import pytest
from pydantic import ValidationError

from news_flash_dedup.admission_schema import DeliveryAdmissionV1, PendingAdmissionV1


def test_versioned_pending_schema_rejects_unlisted_keys_and_type_coercion():
    pending = {
        "scope_id": "default", "request_id": "100-1", "item_id": "100-1",
        "record_id": "a" * 64, "business_fingerprint": "b" * 64,
        "text": "正文 ", "delivery_route_ref": "route-v1", "trace_id": "trace-first",
        "received_at": "2026-09-23T15:59:00.000000Z",
        "accepted_at": "2026-09-23T15:59:00.000000Z",
        "business_date": "2026-09-23", "expires_at": "2026-09-29T16:00:00.000000Z",
        "schema_version": "1", "pipeline_version": "v1", "embedding_space_id": "v3",
        "arrival_seq": 1,
    }
    assert PendingAdmissionV1.model_validate(pending).text == "正文 "
    with pytest.raises(ValidationError):
        PendingAdmissionV1.model_validate({**pending, "extra": "not allowed"})
    with pytest.raises(ValidationError):
        PendingAdmissionV1.model_validate({**pending, "arrival_seq": "1"})


def test_delivery_admission_schema_freezes_first_trace_without_extra_fields():
    value = {"route_ref": "route-v1", "trace_id": "trace-first"}
    assert DeliveryAdmissionV1.model_validate(value).model_dump() == value
    with pytest.raises(ValidationError):
        DeliveryAdmissionV1.model_validate({**value, "credential": "secret"})
