"""/debug 只读日志查看端点（设计=log\\debug-endpoint\\方案设计-v1.md §D1-D5，
评审修订=log\\debug-endpoint\\reviews\\评审甲/乙/丙）。

形态：工厂函数 ``create_debug_router`` 返回 APIRouter，全部依赖 kw-only
显式注入，缺失/不可调即 ValueError 拒装（fail-closed，同 query.py 纪律）；
宿主组装根经 ``create_host(debug_router=...)`` 挂载到 query_app，host.py
只接线不复制闸逻辑。

与参考项目（mabc_service routers/debug.py、industry-insight api/v1/debug.py）
的有意差异：
- 只读日志查看——不做 exec/write/env/ps（攻击面与 /env 泄密面归零）；
- token 比较用 hmac.compare_digest 恒定时间（参考项目为普通 !=）；
- 全部路由 include_in_schema=False（不进无鉴权公开的 /openapi.json）；
- 日志源走显式注入的 LogSource 协议，不 glob 自动发现；
- 行数 clamp + 响应字符上限（deque 限行不限字节的缺口修补）；
- 纯 Python 读文件（无 subprocess/shell 拼接，Windows 可测、无注入面）；
- 读文件显式 encoding="utf-8", errors="replace"（坏字节不 500）。

鉴权：请求头 X-Debug-Token，不匹配→403 {"error":"debug token required"}，
鉴权先于一切参数处理（R-Q11 同款）；token 为 None/空串/非 str → 工厂拒装。
token 轮换 = 宿主重启后注入新值（import 期不缓存环境变量，库不读 env）。
"""

from __future__ import annotations

import hmac
import logging
import os
import re
from collections import deque
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Header
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.concurrency import run_in_threadpool

from .debug_page import DEBUG_PAGE_HTML

_LOGGER = logging.getLogger(__name__)

#: 日志文件名白名单（顶层单文件；覆盖 RotatingFileHandler 的 .1-.3 轮转；
#: ``..``、路径分隔符、隐藏后缀天然被 fullmatch 挡掉——评审丙核验）。
_LOG_NAME_PATTERN = re.compile(r"^[\w.\-]+\.log(\.\d+)?$")

#: FileLogSource 单文件拒读上限（评审丙-用例③：超限拒读而非截断读入内存）。
_DEFAULT_MAX_FILE_BYTES = 50 * 1024 * 1024

#: tail 行数钳制上限（评审甲-2：限行）。
_DEFAULT_MAX_LINES = 1000

#: 响应字符上限（评审甲-3：deque 限行不限字节，长 traceback 行仍可打爆响应）。
_DEFAULT_MAX_RESPONSE_CHARS = 200_000

#: 单行截断上限（评审乙-6：单行截断 + 总上限双闸）。
_MAX_LINE_CHARS = 4000

#: token 最小长度闸（评审乙-4：短 token 拒装，防弱口令上线）。
_MIN_TOKEN_LENGTH = 16


class LogSourceError(Exception):
    """日志源通用失败（宿主实现可抛本品或自有异常——router 统一兜 500）。"""


class LogFileNotFound(LogSourceError):
    """白名单内、但磁盘上已不存在（如轮转间隙）。"""


class LogFileTooLarge(LogSourceError):
    """单文件超过 max_file_bytes，拒读。"""


class FileLogSource:
    """固定目录顶层 ``*.log[.N]`` 日志源（kw-only 显式注入目录，fail-closed）。

    - 构造时目录不存在/非目录 → ValueError（不猜测、不自动发现）；
    - 读取：realpath 双重校验（白名单正则 + 解析后父目录必须等于注入目录，
      防符号链接逃逸——评审乙关注点），encoding="utf-8", errors="replace"；
    - tail 用 deque(maxlen=lines) 内存封顶；半行（并发写入中）原样返回，
      由调用方容忍（评审丙核验可接受）。
    """

    def __init__(self, *, directory: Any,
                 max_file_bytes: int = _DEFAULT_MAX_FILE_BYTES) -> None:
        if not isinstance(directory, (str, os.PathLike)):
            raise ValueError("directory must be explicitly injected")
        root = Path(directory)
        if not root.is_dir():
            raise ValueError("directory must be an existing directory")
        if type(max_file_bytes) is not int or max_file_bytes <= 0:
            raise ValueError("max_file_bytes must be a positive configured integer")
        self._root = root
        self._real_root = os.path.realpath(root)
        self._max_file_bytes = max_file_bytes

    def _resolve(self, name: str) -> Path:
        """白名单 + realpath 父目录校验；非法名→ValueError（router 映射 400）。"""
        if _LOG_NAME_PATTERN.fullmatch(name) is None:
            raise ValueError("invalid log file name")
        candidate = Path(os.path.realpath(self._root / name))
        if candidate.parent != Path(self._real_root):
            raise ValueError("invalid log file name")
        return candidate

    def list_files(self) -> list[dict[str, Any]]:
        """顶层白名单文件（mtime 倒序）；非白名单（.txt/子目录）不列出。"""
        found: list[dict[str, Any]] = []
        for entry in self._root.iterdir():
            if not entry.is_file():
                continue
            if _LOG_NAME_PATTERN.fullmatch(entry.name) is None:
                continue
            stat = entry.stat()
            found.append({
                "name": entry.name,
                "size": stat.st_size,
                "mtime": int(stat.st_mtime),
            })
        found.sort(key=lambda item: item["mtime"], reverse=True)
        return found

    def tail(self, name: str | None, lines: int) -> list[str]:
        """取末尾 ``lines`` 行；name=None → 最近修改的白名单文件。"""
        if name is None:
            files = self.list_files()
            if not files:
                raise LogFileNotFound("no log files")
            name = files[0]["name"]
        candidate = self._resolve(name)
        if not candidate.is_file():
            raise LogFileNotFound(name)
        if candidate.stat().st_size > self._max_file_bytes:
            raise LogFileTooLarge(name)
        window: deque[str] = deque(maxlen=lines)
        with candidate.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                window.append(line.rstrip("\n"))
        return list(window)


def create_debug_router(*, token: Any, log_source: Any,
                        max_lines: int = _DEFAULT_MAX_LINES,
                        max_response_chars: int = _DEFAULT_MAX_RESPONSE_CHARS,
                        ) -> APIRouter:
    """依赖缺失/不可调即拒装（fail-closed）；不读环境变量、不内置默认值。

    - ``token``：X-Debug-Token 期望值；None/空串/非 str/长度<16 → ValueError
      （长度闸=评审乙-4，防弱口令上线）；
    - ``log_source``：鸭式协议，须具可调 ``list_files()`` 与
      ``tail(name, lines)``（FileLogSource 为现成实现，测试可注入假源）。
    """
    if not isinstance(token, str) or not token:
        raise ValueError("token must be explicitly injected")
    if len(token) < _MIN_TOKEN_LENGTH:
        raise ValueError("token must be at least 16 characters")
    if log_source is None or not callable(getattr(log_source, "list_files", None)):
        raise ValueError("log_source with .list_files must be explicitly injected")
    if not callable(getattr(log_source, "tail", None)):
        raise ValueError("log_source with .tail must be explicitly injected")
    if type(max_lines) is not int or max_lines <= 0:
        raise ValueError("max_lines must be a positive configured integer")
    if type(max_response_chars) is not int or max_response_chars <= 0:
        raise ValueError("max_response_chars must be a positive configured integer")

    router = APIRouter(include_in_schema=False)
    token_bytes = token.encode("utf-8")

    # 评审乙-9：日志响应禁缓存（带 query 的 GET 会被中间层/浏览器缓存）。
    _NO_STORE = {"Cache-Control": "no-store"}

    def _forbidden() -> JSONResponse:
        # 固定文案、同文 403 无 oracle；不回显任何内部细节（评审乙-8）。
        return JSONResponse(status_code=403,
                            content={"error": "debug token required"})

    def _authorized(x_debug_token: str | None) -> bool:
        # 缺头先判 None；两端 utf-8 encode 后恒定时间比较——非 ASCII 头值
        # 在 str 比较下会 TypeError→500（评审乙-3）；修参考项目普通 != 缺陷。
        if x_debug_token is None:
            return False
        return hmac.compare_digest(x_debug_token.encode("utf-8"), token_bytes)

    @router.get("/debug", response_class=HTMLResponse)
    async def debug_page() -> HTMLResponse:
        # 页面本身无数据（公开）；数据接口全部 fail-closed——industry 参考形态。
        # 评审乙-1：CSP 收紧 + nosniff（页面渲染侧已 textContent 化）。
        return HTMLResponse(content=DEBUG_PAGE_HTML, headers={
            "Cache-Control": "no-store",
            "Content-Security-Policy": ("default-src 'none'; style-src "
                                        "'unsafe-inline'; script-src "
                                        "'unsafe-inline'"),
            "X-Content-Type-Options": "nosniff",
        })

    @router.get("/debug/api/logs")
    async def debug_logs(x_debug_token: str | None = Header(default=None)
                         ) -> JSONResponse:
        if not _authorized(x_debug_token):
            return _forbidden()
        try:
            # 评审乙-2：同步阻塞 IO 走 threadpool（query.py:82 同款纪律）。
            files = await run_in_threadpool(log_source.list_files)
        except Exception:
            _LOGGER.warning("debug list_files failed", exc_info=True)
            return JSONResponse(status_code=500,
                                content={"error": "log source unavailable"})
        return JSONResponse(status_code=200, content={"files": files},
                            headers=_NO_STORE)

    @router.get("/debug/api/log")
    async def debug_log(file: str | None = None, lines: str = "100",
                        q: str | None = None,
                        x_debug_token: str | None = Header(default=None)
                        ) -> JSONResponse:
        if not _authorized(x_debug_token):
            return _forbidden()
        # lines 收 str 手工解析：int 声明会让 FastAPI 在 handler 前抛 422，
        # 破坏"鉴权先于一切"（红测 R-D2）。非法值回退默认 100（调试工具宽容
        # 取向，钉于 test_lines_invalid_defaults_100）。
        try:
            parsed = int(lines)
        except (TypeError, ValueError):
            parsed = 100
        # 行数钳制 [1, max_lines]（评审甲-2）。
        lines = max(1, min(parsed, max_lines))
        try:
            window = await run_in_threadpool(log_source.tail, file, lines)
        except ValueError:
            return JSONResponse(status_code=400,
                                content={"error": "invalid log file name"})
        except LogFileNotFound:
            return JSONResponse(status_code=404,
                                content={"error": "log file not found"})
        except LogFileTooLarge:
            return JSONResponse(status_code=413,
                                content={"error": "log file too large"})
        except Exception:
            _LOGGER.warning("debug tail failed", exc_info=True)
            return JSONResponse(status_code=500,
                                content={"error": "log source unavailable"})
        if not isinstance(window, list):
            _LOGGER.warning("debug tail returned non-list: %r", type(window))
            return JSONResponse(status_code=500,
                                content={"error": "log source unavailable"})

        total_in_window = len(window)
        # 过滤语义（评审丙-1 钉死）：窗口后过滤——先取末尾 N 行，
        # 再在窗口内做大小写不敏感纯子串匹配（评审乙：q 不得为正则）。
        if q:
            needle = q.lower()
            window = [line for line in window if needle in line.lower()]

        # 单行截断（评审乙-6 第一闸）：长 traceback 单行也受控。
        window = [(line if len(line) <= _MAX_LINE_CHARS
                   else line[:_MAX_LINE_CHARS] + "…[line truncated]")
                  for line in window]

        # 响应字符上限（评审甲-3/乙-6 第二闸）：从末尾向前累加，
        # 超限丢弃更早的行。
        kept: list[str] = []
        budget = max_response_chars
        truncated = False
        for line in reversed(window):
            cost = len(line) + 1
            if cost > budget:
                truncated = True
                break
            kept.append(line)
            budget -= cost
        kept.reverse()

        return JSONResponse(status_code=200, content={
            "file": file,
            "lines": kept,
            "total_in_window": total_in_window,
            "truncated": truncated,
        }, headers=_NO_STORE)

    return router


__all__ = [
    "FileLogSource",
    "LogFileNotFound",
    "LogFileTooLarge",
    "LogSourceError",
    "create_debug_router",
]
