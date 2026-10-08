"""logging_setup.attach_file_logging 红测（设计=log\\debug-endpoint\\方案设计-v2-三方一致.md §四）。

红测清单映射：
- R-L1  参数闸：log_file None/非路径、max_bytes/backup_count 非正整数→ValueError
- R-L2  镜像生效：root logger 写出的行落盘（含格式串 level/name）
- R-L3  幂等：两次调用同路径，handler 数不增（评审乙：重复 attach 不叠加）
- R-L4  坏目录仅 warning 返回 None，不抛、不影响既有 handler
- R-L5  覆盖 root+uvicorn 三 logger（uvicorn.error 写行也落盘）
"""

from __future__ import annotations

import logging

import pytest

from news_flash_dedup.logging_setup import attach_file_logging


@pytest.fixture(autouse=True)
def _clean_handlers(tmp_path):
    """每个用例前后摘除本测试挂上的 RotatingFileHandler，防泄漏污染。"""
    import logging.handlers
    yield tmp_path
    for name in ("", "uvicorn", "uvicorn.error", "uvicorn.access"):
        target = logging.getLogger(name)
        for handler in list(target.handlers):
            if isinstance(handler, logging.handlers.RotatingFileHandler):
                target.removeHandler(handler)
                handler.close()


# ── R-L1 参数闸 ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("bad", [None, 123, b"/tmp/x.log"])
def test_rejects_bad_log_file(bad):
    with pytest.raises(ValueError):
        attach_file_logging(log_file=bad)


@pytest.mark.parametrize("kwargs", [{"max_bytes": 0}, {"max_bytes": -1},
                                    {"max_bytes": "10"}, {"backup_count": 0},
                                    {"backup_count": None}])
def test_rejects_bad_rotation(kwargs, tmp_path):
    with pytest.raises(ValueError):
        attach_file_logging(log_file=tmp_path / "x.log", **kwargs)


# ── R-L2 镜像生效 ────────────────────────────────────────────────────────

def test_root_log_mirrored_to_file(tmp_path):
    log_file = tmp_path / "logs" / "app.log"
    result = attach_file_logging(log_file=log_file)
    assert result == log_file
    logging.getLogger("redtest.mirror").warning("hello-mirror-3241")
    for handler in logging.getLogger().handlers:
        handler.flush()
    content = log_file.read_text(encoding="utf-8")
    assert "hello-mirror-3241" in content
    assert "WARNING" in content and "redtest.mirror" in content


# ── R-L3 幂等 ────────────────────────────────────────────────────────────

def test_attach_is_idempotent(tmp_path):
    log_file = tmp_path / "app.log"
    attach_file_logging(log_file=log_file)
    before = len(logging.getLogger().handlers)
    attach_file_logging(log_file=log_file)
    assert len(logging.getLogger().handlers) == before


# ── R-L4 坏目录降级 ──────────────────────────────────────────────────────

def test_bad_directory_degrades_to_none(tmp_path):
    # 用"文件当目录"制造 OSError（跨平台稳定），仅 warning 不抛。
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    result = attach_file_logging(log_file=blocker / "sub" / "app.log")
    assert result is None


# ── R-L5 uvicorn logger 覆盖 ─────────────────────────────────────────────

def test_uvicorn_loggers_mirrored(tmp_path):
    log_file = tmp_path / "gw.log"
    attach_file_logging(log_file=log_file)
    target = logging.getLogger("uvicorn.error")
    # 全量套件中 bench 用例经 uvicorn.Config(log_level="critical") 可能遗留
    # logger 级别（本用例单跑不复现、套件内复现的教训）——级别是宿主职责，
    # attach 只镜像不改级别；此处显式钉级别，测后还原。
    saved_level = target.level
    target.setLevel(logging.INFO)
    try:
        target.error("uvicorn-boom-7788")
        for handler in target.handlers:
            handler.flush()
        assert "uvicorn-boom-7788" in log_file.read_text(encoding="utf-8")
    finally:
        target.setLevel(saved_level)
