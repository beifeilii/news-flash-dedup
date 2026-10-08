"""/debug 只读日志查看端点红测（设计=log\\debug-endpoint\\方案设计-v2-三方一致.md §四）。

红测清单映射：
- R-D1  工厂 fail-closed：token None/空/非 str/<16 字符→ValueError；
        log_source None/缺 list_files/缺 tail→ValueError；max_lines/
        max_response_chars 非正→ValueError
- R-D2  403 先于一切：无头/错 token/非 ASCII 头值→同文 403
        {"error":"debug token required"}；且源不被触达
- R-D3  页面：200 text/html；CSP/nosniff/no-store 头；源码含 textContent
        渲染、无 innerHTML 拼接日志行（评审甲-1/乙-1 XSS 防线）
- R-D4  logs 列表：200 files；源抛错→500 固定文案不回显异常
- R-D5  log tail：lines 默认 100、clamp [1,1000]；q 窗口后过滤语义钉死
        （先取末尾 N 行再子串过滤）；file 缺省→源收到 None（默认最新）
- R-D6  状态码矩阵：非法文件名（../a.b/x.txt/子目录）→400；合法不存在→404；
        超大→413；源抛错→500；均固定文案
- R-D7  截断双闸：单行>4000 截断带标记；总字符上限 truncated=true 且保留末尾
- R-D8  同步 IO 不占事件循环（run_in_threadpool，评审乙-2）
- R-D9  FileLogSource 实体：目录不存在→ValueError；白名单列表排除
        .txt/子目录、覆盖 .1 轮转；坏编码字节→200 含 U+FFFD；符号链接
        逃逸→400（无权限建符号链接时跳过）；realpath 父目录校验
- R-D10 host 接线：debug_router=None→query_app 无 /debug；注入→/debug 可达
- R-D11 全路由不进 OpenAPI schema（评审甲-2/乙-7）
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from news_flash_dedup.api.debug import (
    FileLogSource,
    LogFileNotFound,
    LogFileTooLarge,
    create_debug_router,
)
from news_flash_dedup.api.host import create_host

TOKEN = "0123456789abcdef"          # 恰 16 位（长度闸边界）
AUTH = {"X-Debug-Token": TOKEN}


class _FakeLogSource:
    """记录调用与事件循环占用（R-D8）；行为可编程。"""

    def __init__(self, files=None, window=None, error=None) -> None:
        self.files = files if files is not None else [
            {"name": "app.log", "size": 12, "mtime": 200},
            {"name": "app.log.1", "size": 34, "mtime": 100},
        ]
        self.window = window if window is not None else [
            "INFO boot", "WARNING slow", "ERROR boom",
        ]
        self.error = error
        self.tail_calls: list[tuple[object, int]] = []
        self.in_event_loop: list[bool] = []

    def _track(self) -> None:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            self.in_event_loop.append(False)
        else:
            self.in_event_loop.append(True)

    def list_files(self):
        self._track()
        if self.error is not None:
            raise self.error
        return self.files

    def tail(self, name, lines):
        self._track()
        self.tail_calls.append((name, lines))
        if self.error is not None:
            raise self.error
        return self.window[-lines:]


def _router(source=None, **overrides):
    kwargs = {"token": TOKEN, "log_source": source or _FakeLogSource()}
    kwargs.update(overrides)
    return create_debug_router(**kwargs)


def _client(source=None, **overrides):
    app = FastAPI()
    app.include_router(_router(source, **overrides))
    return TestClient(app)


# ── R-D1 工厂 fail-closed ────────────────────────────────────────────────

@pytest.mark.parametrize("bad", [None, "", 123, b"x" * 16, "short",
                                 "x" * 15])
def test_factory_rejects_bad_token(bad):
    with pytest.raises(ValueError):
        create_debug_router(token=bad, log_source=_FakeLogSource())


def test_factory_rejects_missing_source():
    with pytest.raises(ValueError):
        create_debug_router(token=TOKEN, log_source=None)


class _NoList:
    def tail(self, name, lines):
        return []


class _NoTail:
    def list_files(self):
        return []


@pytest.mark.parametrize("bad", [_NoList(), _NoTail(), object()])
def test_factory_rejects_incomplete_source(bad):
    with pytest.raises(ValueError):
        create_debug_router(token=TOKEN, log_source=bad)


@pytest.mark.parametrize("kwargs", [{"max_lines": 0}, {"max_lines": -1},
                                    {"max_lines": "100"},
                                    {"max_response_chars": 0},
                                    {"max_response_chars": None}])
def test_factory_rejects_bad_limits(kwargs):
    with pytest.raises(ValueError):
        create_debug_router(token=TOKEN, log_source=_FakeLogSource(),
                            **kwargs)


# ── R-D2 403 先于一切 ────────────────────────────────────────────────────

@pytest.mark.parametrize("path", ["/debug/api/logs", "/debug/api/log",
                                  "/debug/api/log?file=../../etc/passwd",
                                  "/debug/api/log?lines=abc"])
def test_forbidden_before_everything(path):
    source = _FakeLogSource()
    # httpx 头值仅收 ASCII；非 ASCII 头经字节组形式发送（模拟真 UTF-8 客户端）。
    bad_headers = [{}, {"X-Debug-Token": "wrong-token-value"},
                   [(b"x-debug-token", "token-带非ASCII".encode("utf-8"))]]
    with _client(source) as client:
        for headers in bad_headers:
            resp = client.get(path, headers=headers)
            assert resp.status_code == 403, (path, headers)
            assert resp.json() == {"error": "debug token required"}
    assert source.tail_calls == []          # 鉴权先于参数处理与源触达


def test_non_ascii_wrong_token_is_403_not_500():
    # 服务端 str 比较遇非 ASCII 会 TypeError→500（评审乙-3）；两端 utf-8
    # encode 后必须同文 403。
    with _client() as client:
        resp = client.get("/debug/api/logs",
                          headers=[(b"x-debug-token", ("é" * 16).encode("utf-8"))])
        assert resp.status_code == 403
        assert resp.json() == {"error": "debug token required"}


# ── R-D3 页面 ────────────────────────────────────────────────────────────

def test_page_ok_with_security_headers():
    with _client() as client:
        resp = client.get("/debug")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert resp.headers["Cache-Control"] == "no-store"
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert "default-src 'none'" in resp.headers["Content-Security-Policy"]
    body = resp.text
    assert "textContent" in body                       # XSS 防线渲染原语
    assert 'div.textContent = line' in body
    # 日志行不得以 innerHTML 拼接（允许 innerHTML="" 清空容器）
    assert "innerHTML +" not in body and "+ innerHTML" not in body


def test_page_is_public_but_apis_are_not():
    with _client() as client:
        assert client.get("/debug").status_code == 200
        assert client.get("/debug/api/logs").status_code == 403


# ── R-D4 logs 列表 ───────────────────────────────────────────────────────

def test_logs_lists_files():
    with _client() as client:
        resp = client.get("/debug/api/logs", headers=AUTH)
    assert resp.status_code == 200
    assert resp.headers["Cache-Control"] == "no-store"
    names = [f["name"] for f in resp.json()["files"]]
    assert names == ["app.log", "app.log.1"]


def test_logs_source_error_is_plain_500():
    with _client(_FakeLogSource(error=RuntimeError("disk on fire"))) as c:
        resp = c.get("/debug/api/logs", headers=AUTH)
    assert resp.status_code == 500
    assert resp.json() == {"error": "log source unavailable"}
    assert "disk on fire" not in resp.text             # 不回显异常（乙-8）


# ── R-D5 tail 与过滤语义 ─────────────────────────────────────────────────

def test_tail_defaults_lines_100_and_file_none():
    source = _FakeLogSource()
    with _client(source) as client:
        resp = client.get("/debug/api/log", headers=AUTH)
    assert resp.status_code == 200
    assert source.tail_calls == [(None, 100)]
    body = resp.json()
    assert body["lines"] == ["INFO boot", "WARNING slow", "ERROR boom"]
    assert body["total_in_window"] == 3
    assert body["truncated"] is False


@pytest.mark.parametrize("given,clamped", [("1", 1), ("5", 5), ("0", 1),
                                           ("-3", 1), ("99999", 1000)])
def test_lines_clamped(given, clamped):
    source = _FakeLogSource(window=[f"line-{i}" for i in range(2000)])
    with _client(source) as client:
        resp = client.get(f"/debug/api/log?lines={given}", headers=AUTH)
    assert resp.status_code == 200
    assert source.tail_calls == [(None, clamped)]
    assert len(resp.json()["lines"]) == min(clamped, 2000)


def test_lines_invalid_defaults_100():
    # 非法 lines 回退默认 100（lines 收 str 手工解析——int 声明会让 FastAPI
    # 在 handler 前抛 422，破坏鉴权先于一切）。
    source = _FakeLogSource(window=[f"line-{i}" for i in range(2000)])
    with _client(source) as client:
        resp = client.get("/debug/api/log?lines=abc", headers=AUTH)
    assert resp.status_code == 200
    assert source.tail_calls == [(None, 100)]


def test_q_filters_within_window_case_insensitive():
    source = _FakeLogSource(window=["INFO boot", "error soft", "ERROR boom",
                                    "INFO done"])
    with _client(source) as client:
        resp = client.get("/debug/api/log?lines=4&q=ERROR", headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert body["lines"] == ["error soft", "ERROR boom"]   # 窗口后过滤
    assert body["total_in_window"] == 4                    # 过滤前窗口行数


def test_q_no_match_returns_empty():
    with _client() as client:
        resp = client.get("/debug/api/log?q=no-such-needle", headers=AUTH)
    assert resp.status_code == 200
    assert resp.json()["lines"] == []


# ── R-D6 状态码矩阵（FileLogSource 实体驱动真异常类型）──────────────────

@pytest.fixture()
def log_dir(tmp_path):
    (tmp_path / "app.log").write_text("INFO one\nERROR two\n", encoding="utf-8")
    (tmp_path / "app.log.1").write_text("INFO old\n", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("not a log\n", encoding="utf-8")
    (tmp_path / "subdir").mkdir()
    return tmp_path


def _file_client(directory, **overrides):
    return _client(FileLogSource(directory=directory), **overrides)


@pytest.mark.parametrize("bad", ["../secret.log", "a/b.log", "notes.txt",
                                 "..", "app.log/../../x.log", "subdir"])
def test_invalid_file_name_is_400(log_dir, bad):
    with _file_client(log_dir) as client:
        resp = client.get(f"/debug/api/log?file={bad}", headers=AUTH)
    assert resp.status_code == 400
    assert resp.json() == {"error": "invalid log file name"}


def test_valid_but_missing_file_is_404(log_dir):
    with _file_client(log_dir) as client:
        resp = client.get("/debug/api/log?file=gone.log", headers=AUTH)
    assert resp.status_code == 404
    assert resp.json() == {"error": "log file not found"}


def test_too_large_file_is_413(log_dir):
    # 直接构造 max_file_bytes=3 的源（app.log 超 3 字节）
    with _client(FileLogSource(directory=log_dir, max_file_bytes=3)) as c:
        resp = c.get("/debug/api/log?file=app.log", headers=AUTH)
    assert resp.status_code == 413
    assert resp.json() == {"error": "log file too large"}


def test_source_error_is_plain_500(log_dir):
    class _Boom(FileLogSource):
        def tail(self, name, lines):
            raise OSError("permission denied /secret/path")

    with _client(_Boom(directory=log_dir)) as client:
        resp = client.get("/debug/api/log?file=app.log", headers=AUTH)
    assert resp.status_code == 500
    assert resp.json() == {"error": "log source unavailable"}
    assert "/secret/path" not in resp.text


# ── R-D7 截断双闸 ────────────────────────────────────────────────────────

def test_single_line_truncated():
    source = _FakeLogSource(window=["x" * 5000])
    with _client(source) as client:
        resp = client.get("/debug/api/log", headers=AUTH)
    line = resp.json()["lines"][0]
    assert line.endswith("…[line truncated]")
    assert len(line) < 4100


def test_response_char_budget_keeps_tail():
    source = _FakeLogSource(window=[f"line-{i:03d}" for i in range(100)])
    with _client(source, max_response_chars=50) as client:
        resp = client.get("/debug/api/log", headers=AUTH)
    body = resp.json()
    assert body["truncated"] is True
    assert body["lines"][-1] == "line-099"              # 保留末尾
    assert sum(len(x) + 1 for x in body["lines"]) <= 50


# ── R-D8 同步 IO 不占事件循环 ────────────────────────────────────────────

def test_source_calls_run_off_event_loop():
    source = _FakeLogSource()
    with _client(source) as client:
        client.get("/debug/api/logs", headers=AUTH)
        client.get("/debug/api/log", headers=AUTH)
    assert source.in_event_loop
    assert all(flag is False for flag in source.in_event_loop)


# ── R-D9 FileLogSource 实体 ─────────────────────────────────────────────

def test_file_source_rejects_missing_directory(tmp_path):
    with pytest.raises(ValueError):
        FileLogSource(directory=tmp_path / "nope")


@pytest.mark.parametrize("bad", [None, 123])
def test_file_source_rejects_bad_directory(bad):
    with pytest.raises(ValueError):
        FileLogSource(directory=bad)


def test_file_source_lists_only_whitelist(log_dir):
    files = FileLogSource(directory=log_dir).list_files()
    names = [f["name"] for f in files]
    assert "app.log" in names and "app.log.1" in names
    assert "notes.txt" not in names and "subdir" not in names
    assert names[0] == "app.log"                        # mtime 倒序


def test_file_source_tail_reads_utf8_replace(log_dir):
    (log_dir / "bad.log").write_bytes(b"ok\n\xff\xfe bad bytes\nINFO fine\n")
    with _file_client(log_dir) as client:
        resp = client.get("/debug/api/log?file=bad.log", headers=AUTH)
    assert resp.status_code == 200                     # 坏编码不 500
    lines = resp.json()["lines"]
    assert lines[-1] == "INFO fine"
    assert any("�" in line for line in lines)


def test_file_source_tail_default_newest(log_dir):
    source = FileLogSource(directory=log_dir)
    assert source.tail(None, 10)[-1] == "ERROR two"


def test_file_source_missing_default_is_404(tmp_path):
    with _file_client(tmp_path) as client:
        resp = client.get("/debug/api/log", headers=AUTH)
    assert resp.status_code == 404


@pytest.mark.skipif(os.name != "nt" and os.geteuid() != 0,
                    reason="符号链接创建在普通 POSIX 用户下可行；"
                           "Windows 需管理员/Developer Mode，失败则跳过")
def test_symlink_escape_rejected(log_dir, tmp_path):
    outside = tmp_path / "outside.log"
    outside.write_text("SECRET\n", encoding="utf-8")
    link = log_dir / "link.log"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("无权限创建符号链接")
    with _file_client(log_dir) as client:
        resp = client.get("/debug/api/log?file=link.log", headers=AUTH)
    assert resp.status_code == 400


# ── R-D10 host 接线 ─────────────────────────────────────────────────────

def _host(**overrides):
    # 现役 test_api_host.py _host() 同款最小组装（fixed 模式、空探针组）。
    from datetime import datetime, timezone

    from b4_fake_es import FakeESClient

    base = dict(
        es_client=FakeESClient(),
        index_prefix="p01-batch-debug-",
        status_authenticated=lambda request: True,
        env={},
        clock=lambda: datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc),
        probes={},
    )
    base.update(overrides)
    return create_host(**base)


def test_create_host_mounts_debug_router():
    host = _host(debug_router=_router())
    with TestClient(host.query_app) as client:
        assert client.get("/debug").status_code == 200
        assert client.get("/debug/api/logs", headers=AUTH).status_code == 200


def test_create_host_default_no_debug():
    host = _host()
    with TestClient(host.query_app) as client:
        assert client.get("/debug").status_code == 404
        assert client.get("/debug/api/logs").status_code == 404


# ── R-D11 OpenAPI schema ────────────────────────────────────────────────

def test_debug_routes_absent_from_openapi():
    with _client() as client:
        schema = client.get("/openapi.json").json()
    paths = schema["paths"]
    assert "/debug" not in paths
    assert "/debug/api/logs" not in paths
    assert "/debug/api/log" not in paths
