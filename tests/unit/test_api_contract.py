import hashlib
import json
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from news_flash_dedup.admission import AdmissionReceipt, IdentityConflict, AdmissionUnknown
from news_flash_dedup.api.contract import (
    ContractError, IngressContext, MemberAudit, ResultContext,
    parse_submission, submit, validate_result, success_callback,
    failure_callback, ack_succeeded, safe_log_fields, Backpressure, Unavailable,
)


def payload(text="  甲公司\r\n回购。  "):
    return {
        "traceId": "trace-1", "requestId": "100-2", "articleId": None,
        "rewrittenId": 2,
        "source": {"title": "", "body": "  原稿\r\n  ", "pageDate": "2026-09-23"},
        "rewrite": {"title": "", "body": text}, "integrationId": None,
    }


def context():
    return IngressContext("trusted-scope", datetime(2026, 9, 23, tzinfo=timezone.utc),
                          "schema-v1", "pipeline-v1", "embedding-v1", "route-revision")


def receipt(reused=False):
    return AdmissionReceipt("trusted-scope", "100-2", "100-2", "record-q", 12,
                            "2026-09-23", "2026-09-23T00:00:00Z",
                            "2026-09-30T00:00:00Z", "schema-v1", "pipeline-v1",
                            "embedding-v1", "route-revision", "trace-1", reused)


def test_parse_preserves_raw_body_and_rejects_client_scope():
    dto = parse_submission(payload())
    assert dto.text == "  甲公司\r\n回购。  "
    assert dto.source_body == "  原稿\r\n  "
    bad = payload()
    bad["scope_id"] = "attacker"
    with pytest.raises(ContractError):
        parse_submission(bad)


def test_absent_page_date_defaults_empty_and_invalid_types_are_422():
    # 窗口Z2（13 §1①）：pageDate 可选复原——缺省 ""（旧规格"缺省 422"
    # 与 13 号文档 §1① "可选" 不符，同步更正）；rewrittenId 布尔仍 422。
    absent = payload()
    del absent["source"]["pageDate"]
    assert parse_submission(absent).page_date == ""
    bad = payload()
    bad["rewrittenId"] = True
    with pytest.raises(ContractError):
        parse_submission(bad)


def test_submit_uses_trusted_scope_and_atomic_reused_flag():
    seen = []
    class Port:
        def accept(self, request):
            seen.append(request)
            return receipt(reused=len(seen) > 1)
    port = Port()
    first = submit(payload(), context(), port)
    second = submit(payload(), context(), port)
    assert first.status == second.status == 202
    assert first.body["duplicate"] is False
    assert second.body["duplicate"] is True
    assert set(first.body) == {"accepted", "message", "taskId", "traceId", "requestId", "articleId", "duplicate", "state"}
    assert seen[0].scope_id == "trusted-scope"
    assert seen[0].text == payload()["rewrite"]["body"]
    assert seen[0].trace_id == "trace-1"


def test_submit_maps_known_errors_and_does_not_claim_unknown_rejection():
    class ConflictPort:
        def accept(self, request):
            raise IdentityConflict("raw secret should not leak")
    assert submit(payload(), context(), ConflictPort()).status == 409
    assert "raw secret" not in str(submit(payload(), context(), ConflictPort()).body)
    assert submit({"bad": "input"}, context(), ConflictPort()).status == 422
    class UnknownPort:
        def accept(self, request):
            raise AdmissionUnknown("write uncertain")
    with pytest.raises(AdmissionUnknown):
        submit(payload(), context(), UnknownPort())


def test_submit_preserves_body_identity_across_whitespace_and_line_endings():
    seen = []
    class IdentityPort:
        def accept(self, request):
            seen.append(request.text)
            if len(seen) > 1 and request.text != seen[0]:
                raise IdentityConflict("different original")
            return receipt(reused=len(seen) > 1)
    port = IdentityPort()
    assert submit(payload("甲\r\n乙"), context(), port).status == 202
    assert submit(payload("甲\n乙"), context(), port).status == 409
    assert submit(payload("甲\r\n乙 "), context(), port).status == 409
    assert seen == ["甲\r\n乙", "甲\n乙", "甲\r\n乙 "]


def test_configured_limits_and_known_capacity_errors():
    class Port:
        def accept(self, request):
            raise Backpressure()
    limited = replace(context(), max_text_codepoints=2)
    assert submit(payload("三个字"), limited, Port()).status == 413
    assert submit(payload("短文"), context(), Port()).status == 429
    class Down:
        def accept(self, request):
            raise Unavailable()
    assert submit(payload("短文"), context(), Down()).status == 503


def result(ids=None):
    return {"item_id": "100-2", "text": payload()["rewrite"]["body"],
            "decision": "重复", "duplicate_ids": ids or ["90-1", "91-1"],
            "reason": "主体与事件一致。"}


def result_context():
    return ResultContext("100-2", payload()["rewrite"]["body"], "record-q",
                         "trusted-scope", "2026-09-23", 12, "pipeline-v1")


def audits():
    raw = hashlib.sha256(payload()["rewrite"]["body"].encode()).hexdigest()
    def comparison_id(record):
        value = json.dumps(["record-q", record, "pipeline-v1"], ensure_ascii=False,
                           separators=(",", ":")).encode()
        return hashlib.sha256(value).hexdigest()
    return {
        "90-1": MemberAudit("90-1", "record-a", "trusted-scope", "2026-09-23", 4,
                            "record-q", "pipeline-v1", raw, comparison_id("record-a"), True, "重复"),
        "91-1": MemberAudit("91-1", "record-b", "trusted-scope", "2026-09-23", 7,
                            "record-q", "pipeline-v1", raw, comparison_id("record-b"), True, "重复"),
    }


def test_result_requires_every_direct_persisted_member_and_order():
    checked = validate_result(result(), result_context(), audits().__getitem__)
    assert checked["duplicate_ids"] == ["90-1", "91-1"]
    bad = audits()
    bad["91-1"] = replace(bad["91-1"], evidence_valid=False)
    with pytest.raises(ContractError):
        validate_result(result(), result_context(), bad.__getitem__)
    with pytest.raises(ContractError):
        validate_result(result(["91-1", "90-1"]), result_context(), audits().__getitem__)
    with pytest.raises(ContractError):
        validate_result(result(["90-1", "missing"]), result_context(), audits().get)


def test_result_rejects_audit_id_that_does_not_bind_the_pair():
    bad = audits()
    bad["91-1"] = replace(bad["91-1"], audit_id="another-pair")
    with pytest.raises(ContractError):
        validate_result(result(), result_context(), bad.__getitem__)


def test_result_rejects_extra_fields_wrong_text_and_nonduplicate_array():
    bad = result()
    bad["errors"] = []
    with pytest.raises(ContractError):
        validate_result(bad, result_context(), audits().get)
    bad = result()
    bad["text"] += " "
    with pytest.raises(ContractError):
        validate_result(bad, result_context(), audits().get)
    bad = result()
    bad["decision"] = "边界case/疑难case"
    with pytest.raises(ContractError):
        validate_result(bad, result_context(), audits().get)


def test_success_callback_has_full_five_fields_and_mirrored_outer_values():
    wire = success_callback(result(), result_context(), audits().__getitem__,
                            request_id="100-2", trace_id="trace-1",
                            completed_time="2026-09-23T12:00:00Z")
    obj = json.loads(wire.body)
    assert obj["resultJson"] == result()
    assert set(obj) == {"requestId", "traceId", "success", "auditDecision", "reason", "completedTime", "resultJson"}
    assert obj["auditDecision"] == obj["resultJson"]["decision"]
    assert obj["reason"] == obj["resultJson"]["reason"]
    assert hashlib.sha256(wire.body).hexdigest() == wire.sha256
    assert b"\\r\\n" in wire.body


def test_failure_callback_is_engineering_channel_only():
    wire = failure_callback(request_id="100-2", trace_id="trace-1",
                            completed_time="2026-09-23T12:00:00Z", code="KNOWN_FAIL",
                            allowed_codes=frozenset({"KNOWN_FAIL"}),
                            message="处理失败", detail="retry exhausted")
    obj = json.loads(wire.body)
    assert obj["auditDecision"] == "FAIL" and obj["resultJson"] is None
    with pytest.raises(ContractError):
        failure_callback(request_id="100-2", trace_id="trace-1",
                         completed_time="2026-09-23T12:00:00Z", code="NEW_CODE",
                         allowed_codes=frozenset({"KNOWN_FAIL"}), message="x", detail="x")


@pytest.mark.parametrize("status,body,expected", [
    (200, b'{"succeed":true}', True), (204, b'{"succeed":true}', True),
    (200, b'{"succeed":1}', False), (200, b'{"succeed":"true"}', False),
    (200, b'[true]', False), (200, b'not-json', False),
    (500, b'{"succeed":true}', False),
])
def test_ack_requires_boolean_true_json_object_and_2xx(status, body, expected):
    assert ack_succeeded(status, body) is expected


def test_log_whitelist_cannot_echo_body_or_callback():
    event = {"stage": "accepted", "status": "202", "trace_id": "trace-1",
             "record_id": "record-q", "text": "秘密原文", "resultJson": result(),
             "callback_body": "秘密载荷", "credentials": "secret"}
    safe = safe_log_fields(event)
    assert safe == {"stage": "accepted", "status": "202"}
    assert "秘密" not in str(safe)


def test_log_whitelist_drops_free_text_in_allowed_field():
    safe = safe_log_fields({"stage": "秘密原文", "trace_id": "甲公司回购。",
                            "status": "202", "record_id": "record-q"})
    assert safe == {"status": "202"}
