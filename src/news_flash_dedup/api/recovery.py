"""P07-F 产品化恢复 HTTP 端点。

把 `probe_g0_recovery.py` 的 13 场景逻辑并入产品化 HTTP 骨架：
- `POST /v1/api/admin/takeover` 触发新 owner 接管并恢复未完成 pending；
- `GET /v1/api/admin/health` 暴露隔离状态与 owner 代次（运维侧）。

仅运维触发；不暴露给业务客户端；不接收业务 trace。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Protocol

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

_log = logging.getLogger(__name__)


class RecoveryPort(Protocol):
    """产品化恢复端口；由宿主显式注入 coordinator 与 owner_id 提供者。"""

    def takeover(self) -> dict:
        """执行 takeover；返回恢复状态（详见 ProbeRecovery 报告）。"""

    def snapshot(self) -> dict:
        """返回当前 head 摘要（owner_id、allocated、materialized、pending 数量）。"""


def create_recovery_app(*, recovery_port: RecoveryPort,
                       admin_authenticated: Callable[[Request], bool]) -> FastAPI:
    """依赖缺失即拒绝启动；admin 鉴权由宿主显式提供（生产应接入现网鉴权）。

    W2Fγ（F-8②）：启动校验补 snapshot——旧只查 takeover，snapshot 缺席
    时 GET /health 请求期才 503；协议两方法一并启动期 fail-fast。
    """
    if (recovery_port is None
            or not callable(getattr(recovery_port, "takeover", None))
            or not callable(getattr(recovery_port, "snapshot", None))):
        raise ValueError(
            "recovery_port must be explicitly injected "
            "(with callable takeover and snapshot)")
    if admin_authenticated is None or not callable(admin_authenticated):
        raise ValueError("admin_authenticated must be explicitly injected")

    app = FastAPI()

    @app.post("/v1/api/admin/takeover")
    async def admin_takeover(request: Request) -> JSONResponse:
        if not await run_in_threadpool(admin_authenticated, request):
            return JSONResponse(status_code=403, content={"error": "admin required"})
        try:
            report = await run_in_threadpool(recovery_port.takeover)
        except Exception as error:
            # W2Fγ（WA4b-35②）：宽 except 吞错补日志（旧零日志）。
            _log.warning("recovery takeover failed: %s: %s",
                         type(error).__name__, error)
            return JSONResponse(status_code=503, content={"error": type(error).__name__})
        return JSONResponse(status_code=200, content=report)

    @app.get("/v1/api/admin/health")
    async def admin_health(request: Request) -> JSONResponse:
        if not await run_in_threadpool(admin_authenticated, request):
            return JSONResponse(status_code=403, content={"error": "admin required"})
        try:
            snap = await run_in_threadpool(recovery_port.snapshot)
        except Exception as error:
            # W2Fγ（WA4b-35②）：宽 except 吞错补日志（同 takeover 端）。
            _log.warning("recovery snapshot failed: %s: %s",
                         type(error).__name__, error)
            return JSONResponse(status_code=503, content={"error": type(error).__name__})
        return JSONResponse(status_code=200, content=snap)

    return app