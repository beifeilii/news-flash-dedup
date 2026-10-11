"""P0-T1 物化故障五分类（单源分类器）。

口径（施工简报 P0-T1，2026-10-11；设计稿 §5.2 五分类口径，比第二 AI
施工计划多 SYSTEMIC_OUTAGE 一类——简报为权威）：

- ``TRANSIENT_INFRA``：网络/429/5xx/超时/DNS——重试+参与熔断，绝不产墓碑；
- ``UNKNOWN_WRITE``：写入结果未知——GET/MGET 读回对拍，读回仍不确定保持
  RETRY_WAIT 续重试；**重试耗尽≠永久失败**（永不升级墓碑）；
- ``ITEM_PERMANENT``：已确认且只属于单条的不可恢复永久错——**唯一允许
  进入墓碑流程**的分类；
- ``IDENTITY_CORRUPTION``：身份/顺序/摘要不一致——停止自动推进+报警，
  不重试不墓碑；
- ``SYSTEMIC_OUTAGE``：系统性故障（T2 熔断器判定域抛出
  :class:`SystemicOutageError`）——开熔断器，不产墓碑。

分类器纪律（0910 受理层 41×500 事故根因同型防御，算法参照
c3-bench-harness tdata_exec.py ``_is_transient_conn``——产品域语义独立，
不照抄）：瞬断判定穿透显式 ``raise X from Y`` cause 链（≤8 层，不走
``__context__`` 隐式链）——包装类（BatchReadError/AdmissionUnknown/
RuntimeError）不带传输根因本体时不得误判瞬断；**分类不可证明永久时默认
UNKNOWN_WRITE**（fail-closed：读回决定，不猜——404 读侧未命中亦不墓碑）。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .admission import IdentityConflict
from .batch_es_store import PermanentBulkError


class FailureClass(str, Enum):
    """物化失败五分类（str 枚举：墓碑/指标/日志可直接序列化）。"""

    TRANSIENT_INFRA = "transient_infra"
    UNKNOWN_WRITE = "unknown_write"
    ITEM_PERMANENT = "item_permanent"
    IDENTITY_CORRUPTION = "identity_corruption"
    SYSTEMIC_OUTAGE = "systemic_outage"

    @property
    def retryable(self) -> bool:
        """等待/读回后可重试（TRANSIENT/UNKNOWN/SYSTEMIC 三类）。"""
        return self in _RETRYABLE_CLASSES

    @property
    def participates_in_breaker(self) -> bool:
        """计入熔断器失败样本（网络/系统级；UNKNOWN_WRITE 读回优先不计数）。"""
        return self in (FailureClass.TRANSIENT_INFRA,
                        FailureClass.SYSTEMIC_OUTAGE)

    @property
    def may_tombstone(self) -> bool:
        """唯一允许进入墓碑流程的分类（简报铁律：只有确定单条永久错才产墓碑）。"""
        return self is FailureClass.ITEM_PERMANENT


_RETRYABLE_CLASSES = frozenset({
    FailureClass.TRANSIENT_INFRA,
    FailureClass.UNKNOWN_WRITE,
    FailureClass.SYSTEMIC_OUTAGE,
})


class SystemicOutageError(RuntimeError):
    """系统性故障（T2 熔断器判定域抛出）：开熔断器、不产墓碑、不重试计数。"""


_DETAIL_LIMIT = 300
_CAUSE_CHAIN_LIMIT = 8

# 瞬断节点特征（tdata_exec.py _is_transient_node 同型收敛，产品域独立维护）：
# 名称（连接/超时/DNS 族）、传输层模块、消息关键词三路。
_TRANSIENT_NAME_HINTS = ("connection", "timeout", "timedout", "timed_out",
                         "dns", "resolution")
_TRANSIENT_MODULE_HINTS = ("elastic_transport", "urllib3", "requests",
                           "http.client")
_TRANSIENT_MESSAGE_HINTS = (
    "timed out", "connection", "connect", "unavailable", "connection refused",
    "read timed out", "fail connecting", "server unavailable",
    "shard read failed", "name or service not known",
    "nodename nor servname provided", "temporarily unavailable",
)
# 未知写入/响应形态标记（读回对拍优先，绝不直接定永久）。
_UNKNOWN_MESSAGE_HINTS = (
    "unknown", "unconfirmed", "incomplete", "invalid", "not yet confirmed",
    "inconsistent", "ambiguous",
)


def _bounded_detail(text: object) -> str:
    detail = "" if text is None else str(text)
    if len(detail) > _DETAIL_LIMIT:
        detail = detail[: _DETAIL_LIMIT - 3] + "..."
    return detail


def _iter_cause_chain(error: BaseException):
    """显式 ``raise X from Y`` 链逐节点（≤8 层；不走 __context__ 隐式链）。"""
    node: BaseException | None = error
    for _ in range(_CAUSE_CHAIN_LIMIT):
        if node is None:
            return
        yield node
        node = node.__cause__


def _is_transient_node(node: BaseException) -> bool:
    name = type(node).__name__.lower()
    if any(hint in name for hint in _TRANSIENT_NAME_HINTS):
        return True
    message = str(node).lower()
    if any(hint in message for hint in _TRANSIENT_MESSAGE_HINTS):
        return True
    module = type(node).__module__.lower()
    if any(hint in module for hint in _TRANSIENT_MODULE_HINTS):
        return any(hint in message for hint in _TRANSIENT_MESSAGE_HINTS)
    return False


def _chain_status_code(error: BaseException) -> int | None:
    for node in _iter_cause_chain(error):
        status = getattr(node, "status_code", None)
        if type(status) is int:
            return status
    return None


@dataclass(frozen=True)
class MaterializeFailure:
    """一次物化失败的单源分类结果（不可变；摘要有界，不含正文）。"""

    failure_class: FailureClass
    error_code: str
    detail: str

    def __post_init__(self) -> None:
        if not isinstance(self.failure_class, FailureClass):
            raise TypeError("failure_class must be a FailureClass")
        if not isinstance(self.error_code, str) or not self.error_code:
            raise ValueError("error_code must be a non-empty str")
        if len(self.detail) > _DETAIL_LIMIT:
            raise ValueError(
                f"failure detail must be bounded to {_DETAIL_LIMIT} characters")

    @property
    def retryable(self) -> bool:
        return self.failure_class.retryable

    @property
    def participates_in_breaker(self) -> bool:
        return self.failure_class.participates_in_breaker

    @property
    def may_tombstone(self) -> bool:
        return self.failure_class.may_tombstone


def classify_http_status(status: int) -> FailureClass:
    """物化写入侧 HTTP/bulk item 状态分类（非 2xx 输入；2xx 拒绝输入）。

    - 408/429/≥500 → TRANSIENT_INFRA（重试+熔断样本）；
    - 409 → UNKNOWN_WRITE（已存在，需 realtime GET 身份对拍收口）；
    - 404 → UNKNOWN_WRITE（**读侧未命中不得定永久**——写入语境 404
      形态异常，读回决定，fail-closed）；
    - 其余 4xx → ITEM_PERMANENT（确定性单条拒绝，如映射解析 400）；
    - 其余（1xx/负数等畸形）→ UNKNOWN_WRITE。
    """
    if type(status) is not int:
        raise TypeError("status must be an int")
    if 200 <= status < 300:
        raise ValueError("status is a success outcome, not a failure")
    if status in (408, 429) or status >= 500:
        return FailureClass.TRANSIENT_INFRA
    if status in (404, 409):
        return FailureClass.UNKNOWN_WRITE
    if 400 <= status < 500:
        return FailureClass.ITEM_PERMANENT
    return FailureClass.UNKNOWN_WRITE


def _failure(failure_class: FailureClass, error_code: str,
             detail: object) -> MaterializeFailure:
    return MaterializeFailure(
        failure_class=failure_class, error_code=error_code,
        detail=_bounded_detail(detail))


def classify_exception(error: BaseException) -> MaterializeFailure:
    """异常 → 五分类单源入口（cause 链穿透；默认 UNKNOWN_WRITE fail-closed）。

    判定序（先具体后启发）：
    1. ``SystemicOutageError`` → SYSTEMIC_OUTAGE（熔断器域信号）；
    2. ``IdentityConflict`` → IDENTITY_CORRUPTION（身份绑定分歧）；
    3. ``PermanentBulkError`` → ITEM_PERMANENT（现役 store 已逐项核验的
       确定性 4xx——T4 起改走 bulk_create_classified 逐条面，本映射保留
       兼容收口）；
    4. cause 链上首个 int ``status_code`` → :func:`classify_http_status`；
    5. cause 链瞬态特征（名称/模块/消息）→ TRANSIENT_INFRA；
    6. 消息含未知/不完整/形态异常标记 → UNKNOWN_WRITE；
    7. 兜底 → UNKNOWN_WRITE（不可证明永久即不定永久——重试耗尽≠永久）。
    """
    # 显式类型匹配（真实 import，无域环：admission/batch_es_store 均不
    # 依赖本模块）。
    for node in _iter_cause_chain(error):
        if isinstance(node, SystemicOutageError):
            return _failure(FailureClass.SYSTEMIC_OUTAGE, "systemic_outage",
                            node)
        if isinstance(node, IdentityConflict):
            return _failure(FailureClass.IDENTITY_CORRUPTION,
                            "identity_conflict", node)
        if isinstance(node, PermanentBulkError):
            return _failure(FailureClass.ITEM_PERMANENT,
                            "permanent_bulk_error", node)

    status = _chain_status_code(error)
    if status is not None:
        return _failure(classify_http_status(status), f"http_{status}", error)

    for node in _iter_cause_chain(error):
        if _is_transient_node(node):
            return _failure(FailureClass.TRANSIENT_INFRA,
                            type(node).__name__.lower(), node)

    message = str(error).lower()
    if any(hint in message for hint in _UNKNOWN_MESSAGE_HINTS):
        return _failure(FailureClass.UNKNOWN_WRITE, "unconfirmed_outcome",
                        error)
    return _failure(FailureClass.UNKNOWN_WRITE, "unclassified", error)


@dataclass(frozen=True)
class BulkDocumentOutcome:
    """bulk 单文档结果（位置可还原到 entry 四文档之一——位置即契约）。

    - ``status``：HTTP/bulk item 状态码（int；畸形响应在 store 解析层即拒，
      不进入本结构）；
    - ``failure_class``：None=成功（201）；409=UNKNOWN_WRITE（读回身份
      对拍）；其余按 :func:`classify_http_status`；
    - ``error_code``：成功为空串；失败为稳定短码（http_* / response_*）。
    """

    position: int
    index: str
    key: str
    status: int
    failure_class: FailureClass | None
    error_code: str
    detail: str

    def __post_init__(self) -> None:
        if type(self.position) is not int or self.position < 0:
            raise ValueError("position must be a nonnegative int")
        if type(self.status) is not int:
            raise ValueError("status must be an int")
        if not isinstance(self.index, str) or not self.index:
            raise ValueError("index must be a non-empty str")
        if not isinstance(self.key, str) or not self.key:
            raise ValueError("key must be a non-empty str")
        if len(self.detail) > _DETAIL_LIMIT:
            raise ValueError(
                f"outcome detail must be bounded to {_DETAIL_LIMIT} characters")
        if 200 <= self.status < 300:
            if self.failure_class is not None or self.error_code:
                raise ValueError("success outcome must carry no failure")
        elif self.failure_class is None or not self.error_code:
            raise ValueError("non-success outcome must carry a failure")

    @property
    def succeeded(self) -> bool:
        return 200 <= self.status < 300


def bulk_document_outcome(position: int, index: str, key: str, status: int,
                          *, error_type: str = "", error_reason: str = "",
                          ) -> BulkDocumentOutcome:
    """bulk item 解析结果工厂（纯逻辑；store 层完成位置/身份还原后调用）。

    201 成功（携带 error 字段=响应不一致，由 store 层先拒——本工厂不接
    该形态）；其余状态经 :func:`classify_http_status` 归类，错误摘要取
    ``error_type: error_reason``（有界）。
    """
    if type(status) is not int:
        raise ValueError("status must be an int")
    if 200 <= status < 300:
        return BulkDocumentOutcome(
            position=position, index=index, key=key, status=status,
            failure_class=None, error_code="", detail="")
    failure_class = classify_http_status(status)
    detail = _bounded_detail(
        f"{error_type}: {error_reason}" if error_type or error_reason
        else f"status {status}")
    return BulkDocumentOutcome(
        position=position, index=index, key=key, status=status,
        failure_class=failure_class, error_code=f"http_{status}",
        detail=detail)


def failure_from_outcome(outcome: BulkDocumentOutcome) -> MaterializeFailure:
    """单文档失败结果 → 分类摘要（成功文档输入即 ValueError—— misuse 拒）。"""
    if outcome.succeeded:
        raise ValueError("outcome is a success, not a failure")
    assert outcome.failure_class is not None          # invariant: 非成功必带
    return MaterializeFailure(
        failure_class=outcome.failure_class,
        error_code=outcome.error_code,
        detail=outcome.detail)


def is_systemic_outage(error: BaseException) -> bool:
    """T2 熔断域速查（classify_exception 的轻量子集——不做全分类）。"""
    return isinstance(error, SystemicOutageError)


__all__ = [
    "BulkDocumentOutcome",
    "FailureClass",
    "MaterializeFailure",
    "SystemicOutageError",
    "bulk_document_outcome",
    "classify_exception",
    "classify_http_status",
    "failure_from_outcome",
    "is_systemic_outage",
]
