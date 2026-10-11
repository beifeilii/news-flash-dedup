"""P07-C 产品化 AdmissionPort：包装 BatchAdmissionCoordinator/Collector。

不连真实网络；只把 `AdmissionPort.accept()` 协议请求转给批次受理器并把异常分类映射到
合同层的 `Backpressure`/`Unavailable`/`IdentityConflict`/原样 `AdmissionUnknown`/`AdmissionConflict`。
错误分类规则与 `scripts/bench_batch_admission.py:classify_error` 一致；本模块是产品化版本，
bench 脚本可继续使用原 classifier，但 P07-C 起的 HTTP 入口统一走 `CollectorAdmissionPort`。
"""

from __future__ import annotations

from news_flash_dedup.admission import (
    AdmissionConflict, AdmissionReceipt, AdmissionRequest, AdmissionUnknown, IdentityConflict,
)
from news_flash_dedup.api.contract import Backpressure, Unavailable
from news_flash_dedup.batch_admission import AdmissionCapacityExceeded
from news_flash_dedup.batch_collector import BatchAdmissionCollector, CollectorRejected


def suggest_retry_after_seconds(depth: int, capacity: int) -> int:
    """429 Retry-After 建议秒数（P1a-T4；蓝图 §1.4「可重试语义」）。

    口径=快重查偏置：claim 循环 32 件/轮批量排空，过载窗口通常短于
    重试节流代价；硬档满（depth≥capacity）翻倍到 2 秒（排空需多轮，
    快速重试只会加剧受理锁竞争）。确定性纯函数（可复验可对拍）。
    """
    if type(depth) is not int or type(capacity) is not int or depth < 0 or capacity < 1:
        raise ValueError("depth and capacity must be ints with capacity >= 1")
    return 2 if depth >= capacity else 1


class DurableQueueBackpressure(Backpressure):
    """durable 队列容量 429 面（P1a-T4）。

    ``Backpressure`` 合同类子类（合同层 429 映射零改动——``except
    Backpressure`` 自然捕获）；携带 ``depth``/``capacity``/``retry_after_
    seconds`` 观测属性（真实入口适配器由此填响应体的可重试语义与
    Retry-After 建议；快照错误外壳 ``_error`` 冻结不动）。
    """

    def __init__(self, depth: int, capacity: int) -> None:
        self.depth = depth
        self.capacity = capacity
        self.retry_after_seconds = suggest_retry_after_seconds(
            depth, capacity)
        super().__init__(
            f"durable queue capacity exceeded (depth={depth}, "
            f"capacity={capacity})")


class CollectorAdmissionPort:
    """把 `BatchAdmissionCollector.accept()` 包成产品化 `AdmissionPort`。

    错误映射：
    - `AdmissionCapacityExceeded("durable_queue_capacity")` →
      `DurableQueueBackpressure`（合同层映射 429+Retry-After 建议）
    - `AdmissionCapacityExceeded("log_item_limit")` → `Backpressure("durable log capacity")`
    - `AdmissionCapacityExceeded("batch_byte_limit")` → 透传（合同层映射 413）
    - `CollectorRejected("request_too_large")` → 透传（合同层映射 413）
    - `CollectorRejected("queue_full"|"queue_timeout")` → `Backpressure("collector capacity")`
    - `CollectorRejected("closed"|"worker_failed")` → `Unavailable("collector not accepting")`
    - `IdentityConflict` → 透传（合同层映射 409）
    - `AdmissionUnknown` → 透传（合同层映射 500）
    - `AdmissionConflict`（owner/坏状态） → `Unavailable("durable state differs")`
    """

    def __init__(self, collector: BatchAdmissionCollector) -> None:
        if not isinstance(collector, BatchAdmissionCollector):
            raise TypeError("collector must be a BatchAdmissionCollector")
        self.collector = collector

    def accept(self, request: AdmissionRequest) -> AdmissionReceipt:
        # 窗口W2δ（条38，窗口V/Z2 保链裁定同尺）：四处替换型 raise 由 from None
        # 改为 from error——原异常即失败根因，入 __cause__（batch_es_store/milvus_client 同款）。
        try:
            return self.collector.accept(request)
        except AdmissionCapacityExceeded as error:
            if error.reason == "durable_queue_capacity":
                raise DurableQueueBackpressure(
                    error.depth, error.capacity) from error
            if error.reason == "log_item_limit":
                raise Backpressure("durable log capacity") from error
            # batch_byte_limit / log_byte_limit 在合同层映射 413
            raise
        except CollectorRejected as error:
            if error.reason == "request_too_large":
                raise  # 合同层映射 413
            if error.reason in {"queue_full", "queue_timeout"}:
                raise Backpressure("collector capacity") from error
            if error.reason in {"closed", "worker_failed"}:
                raise Unavailable("collector not accepting") from error
            raise
        except IdentityConflict:
            raise  # 合同层映射 409
        except AdmissionConflict as error:
            raise Unavailable("durable state differs") from error
        # AdmissionUnknown 透传 → 合同层映射 500


__all__ = [
    "CollectorAdmissionPort",
    "DurableQueueBackpressure",
    "suggest_retry_after_seconds",
]