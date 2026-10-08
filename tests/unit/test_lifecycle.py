from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from news_flash_dedup.lifecycle import (
    BUSINESS_ZONE,
    CleanupAuditLog,
    _CLEANUP_TARGET_PATTERN,
    candidate_indices_for_cleanup,
    dry_run_cleanup,
    execute_cleanup,
    is_valid_cleanup_target,
    probe_targets_present,
    today_in_business_zone,
)


class FakeIndices:
    def __init__(self, present=None):
        self.present = set(present or [])
        self.deleted = []
        self.calls = []

    def exists(self, *, index):
        self.calls.append(("exists", index))
        return index in self.present

    def delete(self, *, index):
        self.calls.append(("delete", index))
        if index not in self.present:
            raise RuntimeError("delete failed")
        self.deleted.append(index)
        self.present.discard(index)


class FakeClient:
    def __init__(self, present=None):
        self.indices = FakeIndices(present)
        self.calls = []


# ===== 硬正则闸门 =====


@pytest.mark.parametrize("name", [
    "news-dedup-items-v1-2026.09.18",
    "news-dedup-audits-v1-2026.09.18",
    "news-dedup-items-v1-2025.01.01",
])
def test_valid_cleanup_target_names_pass(name):
    assert is_valid_cleanup_target(name) is True


@pytest.mark.parametrize("name,reason", [
    # 他人索引名（共享集群上的）
    ("infb_kq_company_uat", "other_team"),
    (".monitoring-es-2026.09.18", "internal_monitoring"),
    ("kibana_sample_data_logs", "kibana_sample"),
    ("metricbeat-7.17.0-2026.09.18", "metricbeat"),
    ("news-dedup-control-v1", "control_index_out_of_scope"),
    ("news-dedup-requests-v1", "requests_index_out_of_scope"),
    # 畸形日期
    ("news-dedup-items-v1-2026.13.99", "invalid_month_day"),
    ("news-dedup-items-v1-2026.9.18", "unpadded_month"),
    ("news-dedup-items-v1-26.09.18", "unpadded_year"),
    # 业务字段被改
    ("news-dedup-itams-v1-2026.09.18", "typo_kind"),
    ("news-dedup-items-v2-2026.09.18", "wrong_version"),
    # 通配符形式（绝对禁止）
    ("news-dedup-items-v1-*", "wildcard"),
    ("news-dedup-*", "wildcard_prefix"),
    ("*", "full_wildcard"),
])
def test_invalid_cleanup_target_names_rejected(name, reason):
    assert is_valid_cleanup_target(name) is False, f"{name} ({reason}) must be rejected"


# ===== 日期推算 =====


def test_candidate_indices_uses_today_minus_7_in_business_zone():
    today = date(2026, 9, 25)
    candidates = candidate_indices_for_cleanup(today)
    assert len(candidates) == 2  # items + audits
    target_date = date(2026, 9, 18)
    assert candidates[0].index_name == "news-dedup-items-v1-2026.09.18"
    assert candidates[0].business_date == target_date
    assert candidates[0].kind == "items"
    assert candidates[1].kind == "audits"


def test_candidate_indices_rejects_invalid_retention_days():
    with pytest.raises(ValueError):
        candidate_indices_for_cleanup(date(2026, 9, 25), retention_days=0)
    with pytest.raises(ValueError):
        candidate_indices_for_cleanup(date(2026, 9, 25), retention_days=-1)


def test_today_in_business_zone_uses_shanghai_clock():
    # 给定 UTC 时间，验证转换到 Asia/Shanghai
    moment = datetime(2026, 9, 24, 16, 30, tzinfo=timezone.utc)  # UTC 16:30 = SHA 00:30
    assert today_in_business_zone(moment) == date(2026, 9, 25)


def test_today_in_business_zone_handles_sha_dst_boundary():
    # Asia/Shanghai 不实行夏令时；明确无 DST
    moment = datetime(2026, 7, 1, 23, 59, tzinfo=timezone.utc)
    assert today_in_business_zone(moment) == date(2026, 7, 2)


# ===== 候选枚举 + 硬正则：拒绝越界传入 =====


def test_probe_targets_present_only_checks_named_indices():
    present = {
        "news-dedup-items-v1-2026.09.18",
        "news-dedup-audits-v1-2026.09.18",
        "infb_kq_company_uat",  # 他人索引
        ".monitoring-es-2026.09.18",  # 系统索引
    }
    client = FakeClient(present=present)
    candidates = candidate_indices_for_cleanup(date(2026, 9, 25))
    probed = probe_targets_present(client, candidates)
    # 只检查 candidates 列表里的两个（按今天-7 推算的）
    assert len(probed) == 2
    assert all(t.index_name in present for t in probed)
    # 不应枚举其他索引（call 计数：2 次 exists，没有索引列举）
    exists_calls = [c for c in client.indices.calls if c[0] == "exists"]
    assert len(exists_calls) == 2
    assert all(c[0] == "exists" for c in client.indices.calls)


def test_dry_run_records_audit_and_lists_candidates():
    today = date(2026, 9, 25)
    client = FakeClient(present={
        "news-dedup-items-v1-2026.09.18",
        "news-dedup-audits-v1-2026.09.18",
    })
    audit = CleanupAuditLog(operator="admin-test")
    report = dry_run_cleanup(client, today, audit)
    assert report["dry_run"] is True
    assert report["would_delete"] == [
        "news-dedup-items-v1-2026.09.18",
        "news-dedup-audits-v1-2026.09.18",
    ]
    assert report["absent"] == []
    assert len(audit.dry_run_runs) == 1
    assert audit.dry_run_runs[0]["operator"] == "admin-test"


def test_dry_run_marks_absent_indices_separately():
    today = date(2026, 9, 25)
    client = FakeClient(present=set())  # 全部不存在
    audit = CleanupAuditLog(operator="admin-test")
    report = dry_run_cleanup(client, today, audit)
    assert report["would_delete"] == []
    assert sorted(report["absent"]) == [
        "news-dedup-audits-v1-2026.09.18",
        "news-dedup-items-v1-2026.09.18",
    ]


# ===== execute_cleanup：删除成功 + 审计 =====


def test_execute_cleanup_dry_run_does_not_delete():
    today = date(2026, 9, 25)
    client = FakeClient(present={
        "news-dedup-items-v1-2026.09.18",
        "news-dedup-audits-v1-2026.09.18",
    })
    audit = CleanupAuditLog(operator="admin-test")
    report = execute_cleanup(client, today, audit, dry_run=True)
    assert report["dry_run"] is True
    # 09-29 条 32 F-8③ 修复后语义（W2Fδ/WA5-L11，旧钉所钉为误导语义）：
    # dry_run 将删清单入 report["would_delete"]（未真删不得冒充 deleted），
    # report["deleted"] 恒空；execute 真删路径维持 deleted 记账（见下一用例）。
    assert sorted(report["would_delete"]) == [
        "news-dedup-audits-v1-2026.09.18",
        "news-dedup-items-v1-2026.09.18",
    ]
    assert report["deleted"] == []
    assert client.indices.deleted == []  # dry-run 不真删（零真删守卫保留）
    assert len(audit.deletion_runs) == 2
    for run in audit.deletion_runs:
        assert run["ok"] is True


def test_execute_cleanup_deletes_present_indices_and_records_audit():
    today = date(2026, 9, 25)
    client = FakeClient(present={
        "news-dedup-items-v1-2026.09.18",
        "news-dedup-audits-v1-2026.09.18",
    })
    audit = CleanupAuditLog(operator="admin-1")
    report = execute_cleanup(client, today, audit)
    assert sorted(report["deleted"]) == [
        "news-dedup-audits-v1-2026.09.18",
        "news-dedup-items-v1-2026.09.18",
    ]
    assert report["skipped_absent"] == []
    assert sorted(client.indices.deleted) == [
        "news-dedup-audits-v1-2026.09.18",
        "news-dedup-items-v1-2026.09.18",
    ]
    assert len(audit.deletion_runs) == 2
    assert all(r["operator"] == "admin-1" and r["ok"] is True for r in audit.deletion_runs)


def test_execute_cleanup_distinguishes_real_delete_from_absent_pseudo():
    """窗口K（D30 H-07-b）配套钉值：ok=True 双形态——真删 error None vs
    absent 伪删 error "absent"（lifecycle.py:173-176 设计语义）。

    tests/integration/test_cleanup_uat.py 审计断言强化所依赖的源码语义
    在此单元层钉死；语义漂移（absent 改 ok=False / error None，或真删
    改记 error 非 None）本测即红。
    """
    today = date(2026, 9, 25)
    client = FakeClient(present={
        "news-dedup-items-v1-2026.09.18",
        # audits 目标缺失 → absent 伪删路径
    })
    audit = CleanupAuditLog(operator="admin-test")
    report = execute_cleanup(client, today, audit)
    assert report["deleted"] == ["news-dedup-items-v1-2026.09.18"]
    assert report["skipped_absent"] == ["news-dedup-audits-v1-2026.09.18"]
    assert len(audit.deletion_runs) == 2
    by_name = {r["index_name"]: r for r in audit.deletion_runs}
    real = by_name["news-dedup-items-v1-2026.09.18"]
    pseudo = by_name["news-dedup-audits-v1-2026.09.18"]
    assert real["ok"] is True and real["error"] is None          # 真删
    assert pseudo["ok"] is True and pseudo["error"] == "absent"  # absent 伪删


# ===== 红灯测试：他人索引名 + 畸形名传入清理路径必须拒绝 =====


@pytest.mark.parametrize("invader_prefix", [
    "infb_kq_company_uat-",           # 他人业务命名空间
    ".monitoring-es-",                # 系统监控
    "kibana_sample_data_logs-",       # Kibana 样例
    "metricbeat-7.17.0-",             # Metricbeat
])
def test_execute_cleanup_rejects_invader_prefix_injected(invader_prefix):
    """真实驱动 execute_cleanup（D24 重写）：恶意 index_prefix 使生成的
    候选名失配硬正则——执行路径自身必须拒绝+记审计+零删除。

    原版为零驱动（测试内联重实现闸门后自抛 ValueError，从未调用
    execute_cleanup——execute_cleanup 闸门失效时本测试仍绿）；候选名
    只能经 index_prefix 通道注入非法名（日期由真实日期推算必合法），
    故以 prefix 注入达成真实红能力。
    """
    today = date(2026, 9, 25)
    client = FakeClient(present=set())
    audit = CleanupAuditLog(operator="admin-test")
    with pytest.raises(ValueError, match="refused invalid cleanup target"):
        execute_cleanup(client, today, audit, index_prefix=invader_prefix)
    assert client.indices.deleted == []  # 未执行任何删除
    assert len(audit.deletion_runs) == 1
    assert audit.deletion_runs[0]["ok"] is False
    assert audit.deletion_runs[0]["error"] == "invalid_target_name"


def test_execute_cleanup_accepts_isolated_batch_prefix():
    """D24 正向对照：合法 p01-batch-* 前缀候选过闸门并正常删除。"""
    today = date(2026, 9, 25)
    client = FakeClient(present={
        "p01-batch-r1-news-dedup-items-v1-2026.09.18",
        "p01-batch-r1-news-dedup-audits-v1-2026.09.18",
    })
    audit = CleanupAuditLog(operator="admin-test")
    report = execute_cleanup(client, today, audit, index_prefix="p01-batch-r1-")
    assert sorted(report["deleted"]) == [
        "p01-batch-r1-news-dedup-audits-v1-2026.09.18",
        "p01-batch-r1-news-dedup-items-v1-2026.09.18",
    ]
    assert len(audit.deletion_runs) == 2
    assert all(r["ok"] is True for r in audit.deletion_runs)


def test_wildcard_delete_is_not_a_supported_call_path():
    """明确断言：lifecycle 模块不提供宽通配符删除路径。"""
    from news_flash_dedup import lifecycle
    assert not hasattr(lifecycle, "delete_by_wildcard")
    assert not hasattr(lifecycle, "delete_matching")
    candidates = candidate_indices_for_cleanup(date(2026, 9, 25))
    for target in candidates:
        assert is_valid_cleanup_target(target.index_name)


def test_pattern_regex_matches_only_known_good_shape():
    """防止正则被无意改宽。"""
    assert _CLEANUP_TARGET_PATTERN.fullmatch("news-dedup-items-v1-2026.09.18")
    assert _CLEANUP_TARGET_PATTERN.fullmatch("news-dedup-audits-v1-2026.09.18")
    assert not _CLEANUP_TARGET_PATTERN.fullmatch("news-dedup-items-v1-2026.9.18")
    assert not _CLEANUP_TARGET_PATTERN.fullmatch("news-dedup-items-v1-26.09.18")
    assert not _CLEANUP_TARGET_PATTERN.fullmatch("infb_kq_company_uat")
    assert not _CLEANUP_TARGET_PATTERN.fullmatch("news-dedup-*")