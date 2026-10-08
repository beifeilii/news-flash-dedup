"""既有提交路径的可注入 HTTP 入口；鉴权与持久受理由宿主显式提供。"""

from __future__ import annotations

import json
from collections.abc import Callable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from news_flash_dedup.admission import AdmissionUnknown

from .contract import AdmissionPort, IngressContext, HttpResponse, submit


def _error(status: int, message: str) -> JSONResponse:
    # 现网错误外壳仍须联调核对；这里沿源码快照的最小字段。
    return JSONResponse(status_code=status, content={
        "accepted": False, "message": message, "details": [],
    })


def _unique_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def create_app(*, admission_port: AdmissionPort,
               context_provider: Callable[[Request], IngressContext],
               max_http_bytes: int) -> FastAPI:
    """依赖缺失即拒绝启动；不提供匿名 scope 或默认回调路由。"""
    if admission_port is None or not callable(getattr(admission_port, "accept", None)):
        raise ValueError("admission_port must be explicitly injected")
    if context_provider is None or not callable(context_provider):
        raise ValueError("context_provider must be explicitly injected")
    if type(max_http_bytes) is not int or max_http_bytes <= 0:
        raise ValueError("max_http_bytes must be a positive configured integer")

    app = FastAPI()

    @app.exception_handler(AdmissionUnknown)
    async def _on_admission_unknown(_request: Request, _exc: AdmissionUnknown) -> JSONResponse:
        # 受理未确定：保持"未确定"语义到日志，但 HTTP 层返回 500 不伪装业务标签。
        return JSONResponse(status_code=500, content={
            "accepted": False, "message": "受理状态未确认", "details": [],
        })

    @app.post("/v1/api/task/run")
    async def run_task(request: Request) -> JSONResponse:
        chunks: list[bytes] = []
        total = 0
        async for chunk in request.stream():
            total += len(chunk)
            if total > max_http_bytes:
                return _error(413, "请求正文超出限制")
            chunks.append(chunk)
        raw_body = b"".join(chunks)
        try:
            payload = json.loads(raw_body, object_pairs_hook=_unique_pairs)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            return _error(422, "请求 JSON 无效")
        try:
            context = await run_in_threadpool(context_provider, request)
        except Exception:
            return _error(503, "受信接入上下文不可用")
        if not isinstance(context, IngressContext):
            return _error(503, "受信接入上下文不可用")
        outcome: HttpResponse = await run_in_threadpool(
            submit, payload, context, admission_port, raw_http_body=raw_body,
        )
        return JSONResponse(status_code=outcome.status, content=outcome.body)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "alive"}

    return app
