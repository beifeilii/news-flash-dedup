"""P08-D 产品化清理 HTTP 端点：dry-run + 真实删除 + 审计。

严格按 P07 §10 与 P08-A 设计 §6：
- 仅 UAT ES；fail-fast 保留到上线前不得移除；
- 候选枚举 = today-7 推算具体索引名清单，逐个 HEAD 确认存在后点名 DELETE；
- 禁止宽通配符；硬正则闸门 `^news-dedup-(items|audits)-v1-\\d{4}\\.\\d{2}\\.\\d{2}$`；
- dry-run + 审计日志：删除前输出"将删除清单"，每次删除记录索引名+日期+触发者。

窗口Z2（N2-01，外审升级，主窗口追裁）：execute 端 dry_run 缺省翻 True——
空 body/缺键一律演习路径，仅显式 dry_run=false（严格布尔）才真删。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import date
from typing import Protocol

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from ..lifecycle import CleanupAuditLog, today_in_business_zone

_log = logging.getLogger(__name__)

# W2Fγ（F-8①）：execute 端演习路径响应标注文案（真删意图响亮化——form
# 体不解析 dry_run、空 body/缺键翻 True，真删意图曾静默降级零标注）。
_DRILL_REASON = (
    "dry_run defaulted/coerced to True: 仅 JSON 体 {\"dry_run\": false} "
    "（严格布尔）触发真删；form 体不解析 dry_run，空 body/缺键/非 false 值"
    "一律演习（窗口Z2 N2-01 缺省翻转 + W2Fγ F-8① 响亮化标注）"
)


class CleanupPort(Protocol):
    """产品化清理端口；由宿主显式注入 ES 客户端与审计日志。"""

    def dry_run(self, today: date, operator: str) -> dict: ...
    def execute(self, today: date, operator: str, dry_run: bool) -> dict: ...


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": message})


def create_cleanup_app(*, cleanup_port: CleanupPort,
                       admin_authenticated: Callable[[Request], bool]) -> FastAPI:
    # W2Fγ（F-8②）：启动校验补 execute——旧只查 dry_run，execute 缺席时
    # 请求期才 503；协议两方法一并启动期 fail-fast。
    if (cleanup_port is None
            or not callable(getattr(cleanup_port, "dry_run", None))
            or not callable(getattr(cleanup_port, "execute", None))):
        raise ValueError(
            "cleanup_port must be explicitly injected "
            "(with callable dry_run and execute)")
    if admin_authenticated is None or not callable(admin_authenticated):
        raise ValueError("admin_authenticated must be explicitly injected")

    app = FastAPI()

    @app.post("/v1/api/admin/cleanup/dry_run")
    async def admin_cleanup_dry_run(request: Request) -> JSONResponse:
        if not await run_in_threadpool(admin_authenticated, request):
            return _error(403, "admin required")
        form = await request.form() if request.headers.get("content-type", "").startswith(
            "application/x-www-form-urlencoded") else {}
        today_raw = form.get("today") if hasattr(form, "get") else None
        # fail-closed（M-12/L 窗口P）：JSON 体损坏按 400 拒绝，
        # 不再吞错退化为空体默认值。
        if request.headers.get("content-type", "").startswith("application/json"):
            try:
                body = await request.json()
            except ValueError:
                return _error(400, "invalid JSON body")
        else:
            body = {}
        today_str = (today_raw or (body.get("today") if isinstance(body, dict) else None))
        operator = (body.get("operator") if isinstance(body, dict) else None) or "admin"
        try:
            today = date.fromisoformat(today_str) if today_str else None
        except (TypeError, ValueError):
            return _error(400, "invalid today date")
        if today is None:
            today = today_in_business_zone()
        try:
            report = await run_in_threadpool(cleanup_port.dry_run, today, operator)
        except Exception as error:
            # W2Fγ（WA4b-35②）：宽 except 吞错补日志（旧零日志——503 只回
            # 类型名，运维不可见根因）。
            _log.warning("cleanup dry_run failed: %s: %s",
                         type(error).__name__, error)
            return _error(503, type(error).__name__)
        return JSONResponse(status_code=200, content=report)

    @app.post("/v1/api/admin/cleanup/execute")
    async def admin_cleanup_execute(request: Request) -> JSONResponse:
        if not await run_in_threadpool(admin_authenticated, request):
            return _error(403, "admin required")
        # fail-closed（N-01/窗口V，与 dry_run 端口径对齐）：JSON 体损坏按 400
        # 拒绝，不再吞错退化为空体默认值——旧 fail-open 下损坏 JSON 落
        # dry_run=False 默认 = 真删路径放行（窗口P 项4 登记残留，外审升级置顶）。
        # except 口径 ValueError（json.JSONDecodeError 父类）同 dry_run 端，
        # 不再 except Exception 通吞。
        if request.headers.get("content-type", "").startswith("application/json"):
            try:
                body = await request.json()
            except ValueError:
                return _error(400, "invalid JSON body")
        else:
            body = {}
        operator = (body.get("operator") if isinstance(body, dict) else None) or "admin"
        # 窗口Z2（N2-01 cleanup 空白指令，外审升级，主窗口追裁）：dry_run
        # 默认翻 True——空 body/缺键一律演习路径；仅显式 dry_run=false
        # （严格布尔）才真删（fail-closed：0/null/"" 等非 false 值经
        # `is not False` 判为演习，不落真删路径）。
        dry_run_raw = body.get("dry_run") if isinstance(body, dict) else None
        dry_run_flag = dry_run_raw is not False
        today_str = body.get("today") if isinstance(body, dict) else None
        try:
            today = date.fromisoformat(today_str) if today_str else None
        except (TypeError, ValueError):
            return _error(400, "invalid today date")
        if today is None:
            today = today_in_business_zone()
        try:
            report = await run_in_threadpool(cleanup_port.execute, today, operator,
                                              dry_run_flag)
        except ValueError as error:
            return _error(400, str(error))
        except Exception as error:
            # W2Fγ（WA4b-35②）：宽 except 吞错补日志（同 dry_run 端）。
            _log.warning("cleanup execute failed: %s: %s",
                         type(error).__name__, error)
            return _error(503, type(error).__name__)
        # W2Fγ（F-8①）：演习路径响应显式标注 drill 原因——form 体
        # dry_run=false/空 body 的真删意图曾静默降级零标注；响亮化
        # （保兼容：不取 400、不改端口调用形态，仅响应增补标注字段）。
        if dry_run_flag and isinstance(report, dict):
            report = {**report, "drill_reason": _DRILL_REASON}
        return JSONResponse(status_code=200, content=report)

    @app.get("/v1/api/admin/cleanup/health")
    async def admin_cleanup_health(request: Request) -> JSONResponse:
        if not await run_in_threadpool(admin_authenticated, request):
            return _error(403, "admin required")
        return JSONResponse(status_code=200, content={"status": "ready"})

    return app


__all__ = ["CleanupPort", "CleanupAuditLog", "create_cleanup_app"]