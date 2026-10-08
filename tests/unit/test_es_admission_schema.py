from datetime import date

import pytest

from news_flash_dedup.es_admission_schema import (
    audit_mapping, control_mapping, day_index, item_mapping, request_mapping,
)


def test_p01_mappings_keep_strict_field_contract():
    item = item_mapping()
    assert item["mappings"]["dynamic"] == "strict"
    assert len(item["mappings"]["properties"]) == 44
    assert set(item["mappings"]["properties"]["result"]["properties"]) == {
        "decision", "duplicate_ids", "reason"
    }
    assert item["mappings"]["properties"]["diagnostics"]["enabled"] is False
    request = request_mapping()
    assert request["mappings"]["dynamic"] == "strict"
    assert len(request["mappings"]["properties"]) == 10
    control = control_mapping()
    assert control["mappings"]["dynamic"] == "strict"
    assert len(control["mappings"]["properties"]) == 13


def test_audit_mapping_has_13_strict_fields():
    audit = audit_mapping()
    assert audit["mappings"]["dynamic"] == "strict"
    properties = audit["mappings"]["properties"]
    assert set(properties) == {
        "audit_id", "audit_type", "scope_id", "record_id", "candidate_record_id",
        "request_id", "business_date", "pipeline_version", "payload_hash",
        "arrival_seq", "created_at", "expires_at", "payload",
    }
    assert properties["payload"]["enabled"] is False


def test_day_index_formats_business_date_to_dotted_index():
    assert day_index("2026-09-24") == "news-dedup-items-v1-2026.09.24"
    assert day_index("2026-09-24", "audits") == "news-dedup-audits-v1-2026.09.24"


@pytest.mark.parametrize("bad", ["2026/09/24", "2026-9-24", "not-a-date", "", None])
def test_day_index_rejects_non_canonical_date(bad):
    with pytest.raises(ValueError):
        day_index(bad)
