"""P21 Q1 查询端点层（B4 施工窗；设计=log\设计-P21接线包.md §三-Q1）。

GET /v1/api/task/{task_id}：
- 鉴权先于路径参数校验（R-Q11）：未授权→403 {"error":"admin required"}；
- task_id 形态 `<request_id>-<item_id>`（^[1-9]\\d*-[1-9]\\d*$，长度 1..64）→
  422 {"accepted":false,"message":"请求字段无效","details":[]}；
- 端口 None→404 壳恰两键 {"accepted":false,"message":"task not found: <id>"}
  （无 details，R-Q2）；QueryReadError→503 {"accepted":false,"message":
  "服务暂时不可用","details":[]}（服务端状态不可判，绝不伪装 404）；
- 命中→200 投影（外层恰 12 字段，§4.2 表，R-Q3）；端口同步阻塞→
  run_in_threadpool（不占事件循环）。

GET /ready：鉴权→ReadinessPort.issues()→{"ready","issues","mode"}；
fatal→503，degraded/全绿→200（§七-7.2 三态）。

trace 中间件（R-Q10）：X-Trace-ID 回显（缺省生成 uuid4 hex）；访问日志
（/ready 降噪 DEBUG，其余 INFO）。
"""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Callable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from news_flash_dedup.product.task_query import QueryReadError

_TASK_ID_PATTERN = re.compile(r"^[1-9]\d*-[1-9]\d*$")
_TASK_ID_MAX_LENGTH = 64
_LOGGER = logging.getLogger("news_flash_dedup.api.query")


def _shell(status: int, message: str, *, details: bool = True) -> JSONResponse:
    content = {"accepted": False, "message": message}
    if details:
        content["details"] = []
    return JSONResponse(status_code=status, content=content)


def create_query_app(*, query_port, readiness_port,
                     status_authenticated: Callable[[Request], bool]) -> FastAPI:
    """依赖缺失/不可调即拒装（R-Q1 fail-closed）；不内置鉴权与默认值。"""
    if query_port is None or not callable(getattr(query_port, "lookup", None)):
        raise ValueError("query_port with .lookup must be explicitly injected")
    if readiness_port is None or not callable(
            getattr(readiness_port, "issues", None)):
        raise ValueError(
            "readiness_port with .issues must be explicitly injected")
    if status_authenticated is None or not callable(status_authenticated):
        raise ValueError("status_authenticated must be explicitly injected")

    app = FastAPI()

    @app.middleware("http")
    async def trace_middleware(request: Request, call_next):
        trace_id = request.headers.get("x-trace-id") or uuid.uuid4().hex
        response = await call_next(request)
        response.headers["X-Trace-ID"] = trace_id
        line = "%s %s -> %s trace=%s"
        args = (request.method, request.url.path, response.status_code,
                trace_id)
        if request.url.path == "/ready":
            _LOGGER.debug(line, *args)
        else:
            _LOGGER.info(line, *args)
        return response

    @app.get("/v1/api/task/{task_id}")
    async def query_task(task_id: str, request: Request) -> JSONResponse:
        if not status_authenticated(request):          # 鉴权先于路径校验
            return JSONResponse(status_code=403,
                                content={"error": "admin required"})
        if (not task_id or len(task_id) > _TASK_ID_MAX_LENGTH
                or _TASK_ID_PATTERN.fullmatch(task_id) is None):
            return _shell(422, "请求字段无效")
        try:
            view = await run_in_threadpool(query_port.lookup, task_id)
        except QueryReadError:
            return _shell(503, "服务暂时不可用")
        if view is None:
            return _shell(404, f"task not found: {task_id}", details=False)
        return JSONResponse(status_code=200, content=view.to_dict())

    @app.get("/ready")
    async def ready(request: Request) -> JSONResponse:
        if not status_authenticated(request):
            return JSONResponse(status_code=403,
                                content={"error": "admin required"})
        ok, codes = await run_in_threadpool(readiness_port.issues)
        return JSONResponse(status_code=200 if ok else 503, content={
            "ready": ok, "issues": codes, "mode": readiness_port.mode,
        })

    return app


__all__ = ["create_query_app"]
