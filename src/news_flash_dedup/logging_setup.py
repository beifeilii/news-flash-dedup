"""可选的文件日志镜像辅助（设计=log\\debug-endpoint\\方案设计-v1.md §D6）。

用途：为 /debug 端点提供可 tail 的日志文件（FileLogSource 的消费对象），
移植自 mabc_service backend/app/gateway/app.py::_attach_file_logging（调研B §7）。

纪律：
- 本模块不被库自动调用——由宿主入口显式调用（解耦；库零行为变化）；
- stdout 主通道绝不动（阿里云容器平台采集约定，参考项目同纪律）；
- 幂等：按 baseFilename 判重，重复调用不叠加 handler；
- 目录创建/文件打开失败仅 warning 降级为仅 stdout，不影响服务启动。
"""

from __future__ import annotations

import logging
import logging.handlers
import os
from pathlib import Path
from typing import Any

_LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
_LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"

#: 镜像目标：root + uvicorn 三 logger（uvicorn 自带 handler，这里只加文件镜像）。
_TARGET_LOGGERS = ("", "uvicorn", "uvicorn.error", "uvicorn.access")


def attach_file_logging(*, log_file: Any,
                        max_bytes: int = 10 * 1024 * 1024,
                        backup_count: int = 3) -> Path | None:
    """把 root/uvicorn logger 额外镜像到 RotatingFileHandler（幂等）。

    返回实际落盘路径；失败（目录不可建/文件不可开）返回 None 并 warning。
    ``log_file`` 非路径类型 / ``max_bytes``/``backup_count`` 非正整数 →
    ValueError（fail-closed，同项目显式注入纪律）。
    """
    if not isinstance(log_file, (str, os.PathLike)):
        raise ValueError("log_file must be explicitly injected")
    if type(max_bytes) is not int or max_bytes <= 0:
        raise ValueError("max_bytes must be a positive configured integer")
    if type(backup_count) is not int or backup_count <= 0:
        raise ValueError("backup_count must be a positive configured integer")

    path = Path(log_file)
    logger = logging.getLogger(__name__)
    try:
        if path.parent and str(path.parent) not in ("", "."):
            os.makedirs(path.parent, exist_ok=True)
    except OSError as exc:
        logger.warning("无法创建日志目录 %s，跳过文件日志: %s", path.parent, exc)
        return None

    absolute = os.path.abspath(path)
    formatter = logging.Formatter(_LOG_FORMAT, datefmt=_LOG_DATEFMT)
    for name in _TARGET_LOGGERS:
        target = logging.getLogger(name)
        already = any(
            isinstance(handler, logging.handlers.RotatingFileHandler)
            and getattr(handler, "baseFilename", None) == absolute
            for handler in target.handlers
        )
        if already:
            continue
        try:
            handler = logging.handlers.RotatingFileHandler(
                absolute, maxBytes=max_bytes, backupCount=backup_count,
                encoding="utf-8")
        except OSError as exc:
            logger.warning("无法打开日志文件 %s，跳过文件日志: %s", absolute, exc)
            return None
        handler.setFormatter(formatter)
        target.addHandler(handler)
    return path


__all__ = ["attach_file_logging"]
