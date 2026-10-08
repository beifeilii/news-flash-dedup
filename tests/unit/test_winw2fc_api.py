# -*- coding: utf-8 -*-
"""W2Fγ 红测（api 族）：条目 30（F-8① drill 响亮化）/31（F-8② 启动校验）/
35（WA4b 低危族 api/deadline 侧）。

红能力（修复前原貌）：
- 条目30 execute 端 form 体 dry_run=false 真删意图静默降级为演习（响应零标注）；
- 条目31 create_cleanup_app 启动校验只查 dry_run（execute 缺席）；
  create_recovery_app 只查 takeover（snapshot 缺席）——请求期 503 而非启动期
  fail-fast；
- 条目35② cleanup/recovery 四处宽 except 吞错零日志（caplog 实证）；
- 条目35⑤ deadline.clamp_deadline/deadline_iso 对 naive now 不 fail-fast
  （潜伏：naive 截止静默产出；与 W1Fβ 族 fail-fast 同向补闸）。
"""
from __future__ import annotations

import datetime
import logging

import pytest
from fastapi.testclient import TestClient

from news_flash_dedup.api.cleanup import create_cleanup_app
from news_flash_dedup.api.recovery import create_recovery_app


class FakeCleanupPort:
    def __init__(self):
        self.calls = []

    def dry_run(self, today, operator):
        self.calls.append(("dry_run", today.isoformat(), operator))
        return {"today": today.isoformat(), "operator": operator,
                "would_delete": [], "absent": [], "dry_run": True}

    def execute(self, today, operator, dry_run):
        self.calls.append(("execute", today.isoformat(), operator, dry_run))
        return {"today": today.isoformat(), "operator": operator,
                "dry_run": dry_run, "deleted": [], "skipped_absent": []}


def _always_admin(_request):
    return True


# ---------- 条目30（F-8①）：execute 真删意图响亮化 ----------

def test_execute_drill_response_annotates_drill_reason():
    """红能力：form 体 dry_run=false（form 不解析）→ 静默降级演习——响应
    零标注；修复后演习路径响应显式标注 drill 原因（保兼容不取 400）。"""
    port = FakeCleanupPort()
    client = TestClient(create_cleanup_app(
        cleanup_port=port, admin_authenticated=_always_admin))
    response = client.post(
        "/v1/api/admin/cleanup/execute",
        data={"dry_run": "false", "today": "2026-09-25"},
        headers={"content-type": "application/x-www-form-urlencoded"})
    assert response.status_code == 200
    assert port.calls[0][3] is True                            # 演习路径（兼容保持）
    body = response.json()
    assert body.get("drill_reason"), f"演习响应缺 drill 原因标注: {body!r}"


def test_execute_explicit_json_false_no_drill_annotation():
    """绿守卫：显式 JSON dry_run=false → 真删路径无 drill 标注。"""
    port = FakeCleanupPort()
    client = TestClient(create_cleanup_app(
        cleanup_port=port, admin_authenticated=_always_admin))
    response = client.post("/v1/api/admin/cleanup/execute",
                           json={"dry_run": False, "today": "2026-09-25"})
    assert response.status_code == 200
    assert port.calls[0][3] is False                           # 真删路径
    assert "drill_reason" not in response.json()


def test_execute_empty_body_drill_annotated():
    """红能力（Z2 缺省翻转同族）：空 body → 演习 + 标注。"""
    port = FakeCleanupPort()
    client = TestClient(create_cleanup_app(
        cleanup_port=port, admin_authenticated=_always_admin))
    response = client.post("/v1/api/admin/cleanup/execute", json={})
    assert response.status_code == 200
    assert port.calls[0][3] is True
    assert response.json().get("drill_reason")


# ---------- 条目31（F-8②）：启动校验补 execute/snapshot ----------

class _CleanupPortNoExecute:
    def dry_run(self, today, operator):
        return {}


class _RecoveryPortTakeoverOnly:
    def takeover(self):
        return {}


def test_cleanup_factory_rejects_port_without_execute():
    """红能力：cleanup_port 缺 execute——旧启动放行、请求期 503；
    修复后启动期 fail-fast。"""
    with pytest.raises(ValueError, match="(?i)execute|cleanup_port"):
        create_cleanup_app(cleanup_port=_CleanupPortNoExecute(),
                           admin_authenticated=_always_admin)


def test_recovery_factory_rejects_port_without_snapshot():
    """红能力：recovery_port 缺 snapshot——旧启动放行、请求期 503；
    修复后启动期 fail-fast。"""
    with pytest.raises(ValueError, match="(?i)snapshot|recovery_port"):
        create_recovery_app(recovery_port=_RecoveryPortTakeoverOnly(),
                            admin_authenticated=_always_admin)


# ---------- 条目35②：宽 except 补日志 ----------

class _ExplodingCleanupPort:
    def dry_run(self, today, operator):
        raise RuntimeError("boom-dry")

    def execute(self, today, operator, dry_run):
        raise RuntimeError("boom-exec")


class _ExplodingRecoveryPort:
    def takeover(self):
        raise RuntimeError("boom-takeover")

    def snapshot(self):
        raise RuntimeError("boom-snapshot")


def test_cleanup_endpoints_log_on_broad_except(caplog):
    """红能力：cleanup 两端 503 宽 except 吞错零日志。"""
    client = TestClient(create_cleanup_app(
        cleanup_port=_ExplodingCleanupPort(),
        admin_authenticated=_always_admin))
    with caplog.at_level(logging.WARNING):
        assert client.post("/v1/api/admin/cleanup/dry_run",
                           json={}).status_code == 503
        assert client.post("/v1/api/admin/cleanup/execute",
                           json={}).status_code == 503
    texts = [r.getMessage() for r in caplog.records]
    assert any("boom-dry" in t for t in texts), f"dry_run 吞错零日志: {texts!r}"
    assert any("boom-exec" in t for t in texts), f"execute 吞错零日志: {texts!r}"


def test_recovery_endpoints_log_on_broad_except(caplog):
    """红能力：recovery 两端 503 宽 except 吞错零日志。"""
    client = TestClient(create_recovery_app(
        recovery_port=_ExplodingRecoveryPort(),
        admin_authenticated=_always_admin))
    with caplog.at_level(logging.WARNING):
        assert client.post("/v1/api/admin/takeover").status_code == 503
        assert client.get("/v1/api/admin/health").status_code == 503
    texts = [r.getMessage() for r in caplog.records]
    assert any("boom-takeover" in t for t in texts), f"takeover 吞错零日志: {texts!r}"
    assert any("boom-snapshot" in t for t in texts), f"snapshot 吞错零日志: {texts!r}"


# ---------- 条目35⑤：deadline 对 naive now fail-fast ----------

def test_deadline_iso_rejects_naive_now():
    """红能力：naive now 静默产 naive 截止（潜伏：仓内纪律 aware-only）；
    修复后 fail-fast ValueError（与 parse_utc_iso naive 拒同向）。"""
    from news_flash_dedup.deadline import clamp_deadline, deadline_iso
    naive = datetime.datetime(2026, 9, 26, 12, 0, 0)           # tzinfo=None
    with pytest.raises(ValueError, match="(?i)aware|naive"):
        deadline_iso(naive)
    with pytest.raises(ValueError, match="(?i)aware|naive"):
        clamp_deadline(naive)


def test_deadline_iso_aware_now_unaffected():
    """绿守卫：aware now 行为逐字节钉（+24h；expires_at 钳制不变）。"""
    from news_flash_dedup.deadline import deadline_iso, parse_utc_iso
    now = datetime.datetime(2026, 9, 26, 12, 0, 0, tzinfo=datetime.timezone.utc)
    out = deadline_iso(now)
    assert parse_utc_iso(out) == now + datetime.timedelta(hours=24)
    clamped = deadline_iso(now, expires_at="2026-09-26T18:00:00+00:00")
    assert parse_utc_iso(clamped) == datetime.datetime(
        2026, 9, 26, 18, 0, 0, tzinfo=datetime.timezone.utc)
