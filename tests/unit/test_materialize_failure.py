"""P0-T1 单测：物化故障五分类器（fail-closed 钉）。

钉面（施工简报 P0-T1）：
- 瞬断族（网络/429/5xx/超时/DNS——重试+参与熔断，绝不产墓碑）；
- 未知写入族（读回对拍；重试耗尽≠永久失败）；
- 单条永久族（唯一允许进墓碑流程）；
- 身份损坏族（停止自动推进+报警，不重试不墓碑）；
- 系统性故障族（开熔断器，不产墓碑）；
- 0910 事故根因钉：包装类不致盲瞬断判定（cause 链穿透）；
- 默认 UNKNOWN_WRITE（不可证明永久即不定永久——fail-closed）。
"""

from __future__ import annotations

import pytest

from news_flash_dedup.admission import IdentityConflict
from news_flash_dedup.batch_es_store import PermanentBulkError
from news_flash_dedup.materialize_failure import (
    BulkDocumentOutcome,
    FailureClass,
    MaterializeFailure,
    SystemicOutageError,
    bulk_document_outcome,
    classify_exception,
    classify_http_status,
    failure_from_outcome,
    is_systemic_outage,
)


class _StatusError(Exception):
    """带 status_code 的假 HTTP 异常（elasticsearch 异常族鸭子形态）。"""

    def __init__(self, status_code: int, message: str = "http error") -> None:
        super().__init__(f"{message} [{status_code}]")
        self.status_code = status_code


# ---------- classify_exception：瞬断族 ----------

@pytest.mark.parametrize("error", [
    TimeoutError("connect timed out"),
    OSError("connection refused"),
    OSError("read timed out"),
    ConnectionError("fail connecting to host"),
    OSError("server unavailable"),
])
def test_transient_infra_from_transport_shapes(error):
    failure = classify_exception(error)
    assert failure.failure_class is FailureClass.TRANSIENT_INFRA
    assert failure.retryable is True
    assert failure.participates_in_breaker is True
    assert failure.may_tombstone is False


@pytest.mark.parametrize("status", [408, 429, 500, 502, 503, 504])
def test_transient_infra_from_retryable_http_status(status):
    failure = classify_exception(_StatusError(status))
    assert failure.failure_class is FailureClass.TRANSIENT_INFRA
    assert failure.error_code == f"http_{status}"


def test_transient_infra_from_dns_shapes():
    failure = classify_exception(OSError(
        "getaddrinfo failed: nodename nor servname provided"))
    assert failure.failure_class is FailureClass.TRANSIENT_INFRA


# ---------- 0910 事故根因钉：包装类不致盲（cause 链穿透） ----------

def test_wrapped_transport_error_is_transient_through_cause_chain():
    # 形态=batch_es_store/协调器现役包装（from error 保链纪律）：
    # RuntimeError 包装层本体不带传输特征，根因在 __cause__。
    wrapped = RuntimeError("bulk confirmation is unknown")
    wrapped.__cause__ = ConnectionError("connection refused by peer")
    failure = classify_exception(wrapped)
    assert failure.failure_class is FailureClass.TRANSIENT_INFRA
    assert failure.error_code == "connectionerror"


def test_deep_cause_chain_is_pierced_within_limit():
    node = OSError("connection timed out")
    for _ in range(5):
        parent = RuntimeError("outer wrapper")
        parent.__cause__ = node
        node = parent
    assert classify_exception(node).failure_class is FailureClass.TRANSIENT_INFRA


def test_cause_chain_beyond_limit_is_not_pierced():
    # >8 层：穿透有界（防护环）；深度外瞬态不越权定性——fail-closed 落
    # UNKNOWN_WRITE（包装层消息 "unconfirmed" 亦同类）。
    node = OSError("connection timed out")
    for _ in range(10):
        parent = RuntimeError("opaque wrapper")
        parent.__cause__ = node
        node = parent
    assert classify_exception(node).failure_class is FailureClass.UNKNOWN_WRITE


def test_wrapper_without_transport_cause_is_not_transient():
    wrapped = RuntimeError("bulk confirmation is unknown")
    wrapped.__cause__ = ValueError("payload shape is wrong")
    failure = classify_exception(wrapped)
    assert failure.failure_class is FailureClass.UNKNOWN_WRITE
    assert failure.participates_in_breaker is False   # 不稀释熔断样本


def test_explicit_context_chain_is_not_followed():
    # 只走 raise X from Y 显式链：隐式 __context__ 不作为瞬态证据。
    try:
        raise ConnectionError("connection reset by peer")
    except ConnectionError as cause:
        wrapper = RuntimeError("bulk confirmation is unknown")
        wrapper.__context__ = cause                    # 隐式链，非 __cause__
    assert classify_exception(wrapper).failure_class is FailureClass.UNKNOWN_WRITE


# ---------- classify_exception：未知写入族 ----------

def test_unknown_write_from_409_conflict():
    # 409=已存在：需 realtime GET 身份对拍收口（不可直接成功也不可定永久）。
    failure = classify_exception(_StatusError(409, "version conflict"))
    assert failure.failure_class is FailureClass.UNKNOWN_WRITE
    assert failure.retryable is True
    assert failure.participates_in_breaker is False
    assert failure.may_tombstone is False


def test_unknown_write_from_read_side_404_not_permanent():
    # 读侧未命中绝不定永久（fail-closed：404 永不墓碑）。
    failure = classify_exception(_StatusError(404, "document not found"))
    assert failure.failure_class is FailureClass.UNKNOWN_WRITE


@pytest.mark.parametrize("message", [
    "bulk item confirmation is unknown",
    "mget returned an incomplete result",
    "document response is invalid",
    "bulk item outcome is inconsistent",
])
def test_unknown_write_from_unconfirmed_markers(message):
    failure = classify_exception(RuntimeError(message))
    assert failure.failure_class is FailureClass.UNKNOWN_WRITE
    assert failure.error_code == "unconfirmed_outcome"


def test_unclassifiable_defaults_to_unknown_write():
    failure = classify_exception(RuntimeError("weird machine state"))
    assert failure.failure_class is FailureClass.UNKNOWN_WRITE
    assert failure.error_code == "unclassified"
    assert failure.may_tombstone is False              # 重试耗尽≠永久失败


# ---------- classify_exception：单条永久族 ----------

@pytest.mark.parametrize("status", [400, 401, 403, 412, 413, 422])
def test_item_permanent_from_deterministic_4xx(status):
    failure = classify_exception(_StatusError(status, "mapper rejected"))
    assert failure.failure_class is FailureClass.ITEM_PERMANENT
    assert failure.retryable is False
    assert failure.participates_in_breaker is False
    assert failure.may_tombstone is True               # 唯一允许进墓碑流程


def test_item_permanent_from_permanent_bulk_error():
    error = PermanentBulkError(
        "bulk item permanently rejected", failed_document_positions=(3,))
    failure = classify_exception(error)
    assert failure.failure_class is FailureClass.ITEM_PERMANENT
    assert failure.may_tombstone is True


def test_item_permanent_wrapped_in_cause_chain():
    wrapped = RuntimeError("materialization attempt failed")
    wrapped.__cause__ = PermanentBulkError("bulk item permanently rejected")
    assert classify_exception(wrapped).failure_class is FailureClass.ITEM_PERMANENT


# ---------- classify_exception：身份损坏/系统性故障 ----------

def test_identity_corruption_from_identity_conflict():
    failure = classify_exception(
        IdentityConflict("request or item identity already binds other content"))
    assert failure.failure_class is FailureClass.IDENTITY_CORRUPTION
    assert failure.retryable is False                  # 不重试
    assert failure.may_tombstone is False              # 不墓碑


def test_systemic_outage_from_dedicated_error():
    failure = classify_exception(SystemicOutageError("breaker opened"))
    assert failure.failure_class is FailureClass.SYSTEMIC_OUTAGE
    assert failure.retryable is True                   # 恢复后可续
    assert failure.participates_in_breaker is True
    assert failure.may_tombstone is False              # 不产墓碑
    assert is_systemic_outage(SystemicOutageError("x")) is True
    assert is_systemic_outage(RuntimeError("x")) is False


# ---------- classify_http_status ----------

@pytest.mark.parametrize("status,expected", [
    (408, FailureClass.TRANSIENT_INFRA),
    (429, FailureClass.TRANSIENT_INFRA),
    (500, FailureClass.TRANSIENT_INFRA),
    (503, FailureClass.TRANSIENT_INFRA),
    (409, FailureClass.UNKNOWN_WRITE),
    (404, FailureClass.UNKNOWN_WRITE),
    (400, FailureClass.ITEM_PERMANENT),
    (422, FailureClass.ITEM_PERMANENT),
    (100, FailureClass.UNKNOWN_WRITE),                # 畸形状态
])
def test_classify_http_status_matrix(status, expected):
    assert classify_http_status(status) is expected


def test_classify_http_status_rejects_success_input():
    with pytest.raises(ValueError):
        classify_http_status(201)
    with pytest.raises(TypeError):
        classify_http_status("201")  # type: ignore[arg-type]


# ---------- 分类矩阵谓词 ----------

def test_five_class_predicate_matrix():
    matrix = {
        FailureClass.TRANSIENT_INFRA: (True, True, False),
        FailureClass.UNKNOWN_WRITE: (True, False, False),
        FailureClass.ITEM_PERMANENT: (False, False, True),
        FailureClass.IDENTITY_CORRUPTION: (False, False, False),
        FailureClass.SYSTEMIC_OUTAGE: (True, True, False),
    }
    for failure_class, (retryable, breaker, tombstone) in matrix.items():
        assert failure_class.retryable is retryable
        assert failure_class.participates_in_breaker is breaker
        assert failure_class.may_tombstone is tombstone


# ---------- MaterializeFailure 结构纪律 ----------

def test_materialize_failure_detail_is_bounded():
    failure = classify_exception(RuntimeError("x" * 10_000))
    assert len(failure.detail) <= 300


def test_materialize_failure_rejects_invalid_shapes():
    with pytest.raises(TypeError):
        MaterializeFailure(failure_class="transient_infra",   # type: ignore[arg-type]
                           error_code="x", detail="y")
    with pytest.raises(ValueError):
        MaterializeFailure(failure_class=FailureClass.TRANSIENT_INFRA,
                           error_code="", detail="y")
    with pytest.raises(ValueError):
        MaterializeFailure(failure_class=FailureClass.TRANSIENT_INFRA,
                           error_code="x", detail="y" * 301)


# ---------- BulkDocumentOutcome 工厂与结构 ----------

def test_bulk_document_outcome_success_shape():
    outcome = bulk_document_outcome(0, "idx", "key-1", 201)
    assert outcome.succeeded is True
    assert outcome.failure_class is None
    assert outcome.error_code == ""
    with pytest.raises(ValueError):
        failure_from_outcome(outcome)                  # 成功不得转失败


@pytest.mark.parametrize("status,expected", [
    (409, FailureClass.UNKNOWN_WRITE),
    (412, FailureClass.ITEM_PERMANENT),
    (503, FailureClass.TRANSIENT_INFRA),
])
def test_bulk_document_outcome_failure_shapes(status, expected):
    outcome = bulk_document_outcome(
        7, "idx", "key-2", status,
        error_type="mapper_parsing_exception", error_reason="field rejected")
    assert outcome.succeeded is False
    assert outcome.failure_class is expected
    assert outcome.error_code == f"http_{status}"
    assert "mapper_parsing_exception" in outcome.detail
    assert failure_from_outcome(outcome).failure_class is expected


def test_bulk_document_outcome_structural_invariants():
    ok = bulk_document_outcome(0, "idx", "k", 201)
    # 成功携带失败字段即拒（响应不一致形态由 store 层先拦）：
    with pytest.raises(ValueError):
        BulkDocumentOutcome(position=0, index="idx", key="k", status=201,
                            failure_class=FailureClass.UNKNOWN_WRITE,
                            error_code="http_409", detail="x")
    # 非成功缺失败字段即拒：
    with pytest.raises(ValueError):
        BulkDocumentOutcome(position=0, index="idx", key="k", status=409,
                            failure_class=None, error_code="", detail="")
    # 位置/身份字段纪律：
    with pytest.raises(ValueError):
        BulkDocumentOutcome(position=-1, index="idx", key="k", status=201,
                            failure_class=None, error_code="", detail="")
    with pytest.raises(ValueError):
        BulkDocumentOutcome(position=0, index="", key="k", status=201,
                            failure_class=None, error_code="", detail="")
    assert ok.position == 0 and ok.status == 201
