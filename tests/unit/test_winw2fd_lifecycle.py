"""W2Fδ 修复窗口红测：条48（F-4 §9.3 清理清单 ES 侧子集）。

补：reviews/requests delete_by_query 步骤 + expired 标记 + 超期残留枚举。
fake 红测先行；真集群走 UAT 闸（本文件不触网）。Milvus 侧不动（挂账）。
只新建不改既有套件。
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest


NOW = datetime(2026, 10, 8, 4, 0, 0, tzinfo=timezone.utc)
NOW_ISO = "2026-10-08T04:00:00Z"


class _DocIndices:
    """文档级 fake：exists/受限通配 listing；count/delete_by_query 在 client 层。"""

    def __init__(self, present=(), listing=None):
        self.present = set(present)
        self.listing = dict(listing or {})
        self.exists_calls = []

    def exists(self, *, index):
        self.exists_calls.append(index)
        return index in self.present

    def get(self, *, index, allow_no_indices, ignore_unavailable):
        prefixes = [pat[:-1] for pat in index.split(",") if pat.endswith("*")]
        return {name: {} for name in self.listing
                if any(name.startswith(prefix) for prefix in prefixes)}


class _DocClient:
    def __init__(self, indices, counts=None, deleted=None, fail_delete_for=None):
        self.indices = indices
        self.counts = dict(counts or {})
        self.deleted = dict(deleted or {})
        self.fail_delete_for = fail_delete_for
        self.count_calls = []
        self.delete_by_query_calls = []

    def count(self, *, index, body):
        self.count_calls.append((index, body))
        return {"count": self.counts.get(index, 0),
                "_shards": {"total": 1, "successful": 1, "failed": 0}}

    def delete_by_query(self, *, index, body, refresh):
        self.delete_by_query_calls.append((index, body, refresh))
        if self.fail_delete_for == index:
            raise ConnectionError("synthetic delete_by_query failure")
        # W-A 族③(c)-2 配套：成功语义字面化（timed_out/failures/
        # version_conflicts——新三面核验缺键即拒的既有双倍体补全，断言零改动）。
        return {"timed_out": False, "failures": [], "version_conflicts": 0,
                "deleted": self.deleted.get(index, self.counts.get(index, 0))}


# ---------- 硬正则：delete_by_query 目标名闸（94 保留索引纪律兼容） ----------

def test_expired_document_index_names_pass():
    from news_flash_dedup.lifecycle import is_valid_expired_document_index
    assert is_valid_expired_document_index("news-dedup-requests-v1")
    assert is_valid_expired_document_index("news-dedup-reviews-v1")
    assert is_valid_expired_document_index("p01-batch-run42-news-dedup-requests-v1")


@pytest.mark.parametrize("name", [
    "news-dedup-control-v1",                 # 控制文档不在本路径
    "news-dedup-items-v1-2026.09.18",        # 日索引走 execute_cleanup 点名删除
    "news-dedup-audits-v1-2026.09.18",
    "news-dedup-requests-v2",
    "news-dedup-requests-v1-extra",
    "infb_kq_company_uat",                   # 共享集群他组索引
    ".monitoring-es-2026.09.18",
    "news-dedup-requests-v1*",
    "*",
])
def test_expired_document_index_names_rejected(name):
    from news_flash_dedup.lifecycle import is_valid_expired_document_index
    assert is_valid_expired_document_index(name) is False


def test_document_index_pattern_disjoint_from_day_index_pattern():
    """94 保留索引硬正则兼容核：两族清理目标名互不重叠（双向对照）。"""
    from news_flash_dedup.lifecycle import (
        is_valid_cleanup_target, is_valid_expired_document_index,
    )
    day_names = ["news-dedup-items-v1-2026.09.18", "news-dedup-audits-v1-2026.09.18",
                 "p01-batch-r1-news-dedup-items-v1-2026.09.18"]
    doc_names = ["news-dedup-requests-v1", "news-dedup-reviews-v1",
                 "p01-batch-r1-news-dedup-requests-v1"]
    for name in day_names:
        assert is_valid_cleanup_target(name) and not is_valid_expired_document_index(name)
    for name in doc_names:
        assert is_valid_expired_document_index(name) and not is_valid_cleanup_target(name)
    # 控制文档两条路径都不收
    assert not is_valid_cleanup_target("news-dedup-control-v1")
    assert not is_valid_expired_document_index("news-dedup-control-v1")


# ---------- expired 标记查询体 ----------

def test_expired_document_query_uses_expires_at_range():
    from news_flash_dedup.lifecycle import expired_document_query
    assert expired_document_query(NOW) == {
        "query": {"range": {"expires_at": {"lt": NOW_ISO}}}}


def test_expired_document_query_rejects_naive_now():
    from news_flash_dedup.lifecycle import expired_document_query
    with pytest.raises(ValueError):
        expired_document_query(datetime(2026, 10, 8, 4, 0, 0))


# ---------- 计划：在场/缺场 + 过期计数 ----------

def test_plan_counts_expired_per_present_index():
    from news_flash_dedup.lifecycle import plan_expired_document_cleanup
    client = _DocClient(
        _DocIndices(present={"news-dedup-requests-v1"}),
        counts={"news-dedup-requests-v1": 7})
    targets = plan_expired_document_cleanup(client, NOW)
    by_kind = {t.kind: t for t in targets}
    assert by_kind["requests"].is_present and by_kind["requests"].expired_count == 7
    assert not by_kind["reviews"].is_present and by_kind["reviews"].expired_count is None
    # 只对在场索引发 count
    assert [call[0] for call in client.count_calls] == ["news-dedup-requests-v1"]
    assert client.count_calls[0][1] == {
        "query": {"range": {"expires_at": {"lt": NOW_ISO}}}}


def test_plan_supports_isolated_prefix():
    from news_flash_dedup.lifecycle import plan_expired_document_cleanup
    client = _DocClient(
        _DocIndices(present={"p01-batch-r1-news-dedup-reviews-v1"}),
        counts={"p01-batch-r1-news-dedup-reviews-v1": 3})
    targets = plan_expired_document_cleanup(client, NOW, index_prefix="p01-batch-r1-")
    by_kind = {t.kind: t for t in targets}
    assert by_kind["reviews"].expired_count == 3
    assert not by_kind["requests"].is_present


# ---------- 执行：dry-run / 真删 / 审计 / expired 标记 ----------

def test_execute_dry_run_deletes_nothing_but_reports_counts():
    from news_flash_dedup.lifecycle import (
        CleanupAuditLog, execute_expired_document_cleanup,
    )
    client = _DocClient(
        _DocIndices(present={"news-dedup-requests-v1", "news-dedup-reviews-v1"}),
        counts={"news-dedup-requests-v1": 5, "news-dedup-reviews-v1": 2})
    audit = CleanupAuditLog(operator="w2fd-test")
    report = execute_expired_document_cleanup(client, NOW, audit, dry_run=True)
    assert client.delete_by_query_calls == []
    assert report["would_delete"] == {
        "news-dedup-requests-v1": 5, "news-dedup-reviews-v1": 2}
    assert report["deleted"] == {}
    assert report["expired_before"] == NOW_ISO  # expired 标记入报告
    runs = audit.document_cleanup_runs
    assert len(runs) == 2
    assert all(run["expired_before"] == NOW_ISO for run in runs)  # expired 标记入审计
    assert all(run["error"] == "dry_run" and run["ok"] for run in runs)


def test_execute_real_run_delete_by_query_named_only():
    from news_flash_dedup.lifecycle import (
        CleanupAuditLog, execute_expired_document_cleanup,
    )
    client = _DocClient(
        _DocIndices(present={"news-dedup-requests-v1"}),
        counts={"news-dedup-requests-v1": 4}, deleted={"news-dedup-requests-v1": 4})
    audit = CleanupAuditLog(operator="w2fd-test")
    report = execute_expired_document_cleanup(client, NOW, audit)
    assert client.delete_by_query_calls == [
        ("news-dedup-requests-v1",
         {"query": {"range": {"expires_at": {"lt": NOW_ISO}}}}, True)]
    assert report["deleted"] == {"news-dedup-requests-v1": 4}
    assert report["skipped_absent"] == ["news-dedup-reviews-v1"]
    runs = audit.document_cleanup_runs
    assert runs[0]["deleted"] == 4 and runs[0]["ok"]
    assert runs[1]["error"] == "absent" and runs[1]["ok"]


def test_execute_delete_failure_records_audit_and_raises():
    from news_flash_dedup.lifecycle import (
        CleanupAuditLog, execute_expired_document_cleanup,
    )
    client = _DocClient(
        _DocIndices(present={"news-dedup-requests-v1"}),
        counts={"news-dedup-requests-v1": 1},
        fail_delete_for="news-dedup-requests-v1")
    audit = CleanupAuditLog(operator="w2fd-test")
    with pytest.raises(ConnectionError):
        execute_expired_document_cleanup(client, NOW, audit)
    assert audit.document_cleanup_runs[0]["ok"] is False
    assert audit.document_cleanup_runs[0]["error"] == "ConnectionError"


# ---------- 超期残留枚举 ----------

def test_residue_enumerates_indices_older_than_retention():
    from news_flash_dedup.lifecycle import enumerate_overdue_residue
    listing = {
        "news-dedup-items-v1-2026.09.20": {},   # 早于 cutoff(10-01) → 残留
        "news-dedup-audits-v1-2026.09.20": {},  # 残留
        "news-dedup-items-v1-2026.10.01": {},   # == cutoff → 不残留（本日 T-7 点名）
        "news-dedup-items-v1-2026.10.05": {},   # 新于 cutoff → 不残留
    }
    client = _DocClient(_DocIndices(listing=listing))
    report = enumerate_overdue_residue(client, date(2026, 10, 8))
    assert report["residue"] == [
        "news-dedup-audits-v1-2026.09.20", "news-dedup-items-v1-2026.09.20"]
    assert report["cutoff"] == "2026-10-01"
    assert report["alien_alarms"] == []


def test_residue_alarms_on_malformed_names_in_namespace():
    """本命名空间内异形名：告警不静默跳过（且不进入残留清单）。"""
    from news_flash_dedup.lifecycle import enumerate_overdue_residue
    listing = {
        "news-dedup-items-v1-2026.13.99": {},   # 伪日期
        "news-dedup-items-v1-2026.9.18": {},    # 未补零
        "news-dedup-items-v1-2020.01.01": {},   # 合法且超期 → 残留
    }
    client = _DocClient(_DocIndices(listing=listing))
    report = enumerate_overdue_residue(client, date(2026, 10, 8))
    assert sorted(report["alien_alarms"]) == [
        "news-dedup-items-v1-2026.13.99", "news-dedup-items-v1-2026.9.18"]
    assert report["residue"] == ["news-dedup-items-v1-2020.01.01"]


def test_residue_listing_is_scoped_to_day_index_namespace():
    """读取受限通配仅覆盖本命名空间日索引形（control/requests/reviews 不在通配内）。"""
    from news_flash_dedup.lifecycle import enumerate_overdue_residue
    indices = _DocIndices(listing={})
    client = _DocClient(indices)
    enumerate_overdue_residue(client, date(2026, 10, 8), index_prefix="p01-batch-r1-")
    # fake get 记录的查询串必须只含 items/audits/work/tombstones 四支且带前缀
    # （_DocIndices.get 的入参即通配串；P1a-T5：超期残留枚举=清理域全集）
    queried = []
    # 重新调用并捕获
    class _Cap(_DocIndices):
        def get(self, *, index, allow_no_indices, ignore_unavailable):
            queried.append(index)
            return {}
    client = _DocClient(_Cap())
    enumerate_overdue_residue(client, date(2026, 10, 8), index_prefix="p01-batch-r1-")
    assert queried == ["p01-batch-r1-news-dedup-items-v1-*,"
                       "p01-batch-r1-news-dedup-audits-v1-*,"
                       "p01-batch-r1-news-dedup-work-v1-*,"
                       "p01-batch-r1-news-dedup-tombstones-v1-*"]


def test_residue_rejects_bad_retention():
    from news_flash_dedup.lifecycle import enumerate_overdue_residue
    client = _DocClient(_DocIndices())
    with pytest.raises(ValueError):
        enumerate_overdue_residue(client, date(2026, 10, 8), retention_days=0)


# ---------- 条32（W2Fγ 转入，F-8③/WA5-L11 并案）：dry_run 报告字段名误导 ----------

class _DayIndices:
    def __init__(self, present=()):
        self.present = set(present)
        self.deleted = []

    def exists(self, *, index):
        return index in self.present

    def delete(self, *, index):
        if index not in self.present:
            raise RuntimeError("delete failed")
        self.deleted.append(index)
        self.present.discard(index)


class _DayClient:
    def __init__(self, present=()):
        self.indices = _DayIndices(present)


def test_execute_cleanup_dry_run_reports_would_delete_not_deleted():
    """条32：dry_run 分支写 report["would_delete"]（未真删不得冒充 deleted）。

    P1a-T5：kind 全集四件——在场 items/audits 入 dry_run 留痕；work/
    tombstones 缺场入 absent（旧"全 dry_run"钉随 kind 全集更新）。
    """
    from news_flash_dedup.lifecycle import CleanupAuditLog, execute_cleanup
    client = _DayClient(present={
        "news-dedup-items-v1-2026.09.18", "news-dedup-audits-v1-2026.09.18"})
    audit = CleanupAuditLog(operator="w2fd-test")
    report = execute_cleanup(client, date(2026, 9, 25), audit, dry_run=True)
    assert report["dry_run"] is True
    assert sorted(report["would_delete"]) == [
        "news-dedup-audits-v1-2026.09.18", "news-dedup-items-v1-2026.09.18"]
    assert report["deleted"] == []  # 未真删：deleted 恒空
    assert client.indices.deleted == []
    by_name = {run["index_name"]: run["error"] for run in audit.deletion_runs}
    assert by_name["news-dedup-items-v1-2026.09.18"] == "dry_run"
    assert by_name["news-dedup-audits-v1-2026.09.18"] == "dry_run"
    assert by_name["news-dedup-work-v1-2026.09.18"] == "absent"
    assert by_name["news-dedup-tombstones-v1-2026.09.18"] == "absent"


def test_execute_cleanup_real_run_keeps_deleted_and_empty_would_delete():
    """条32 对照：execute 路径 report["deleted"] 语义不动，would_delete 恒空。"""
    from news_flash_dedup.lifecycle import CleanupAuditLog, execute_cleanup
    client = _DayClient(present={"news-dedup-items-v1-2026.09.18"})
    audit = CleanupAuditLog(operator="w2fd-test")
    report = execute_cleanup(client, date(2026, 9, 25), audit)
    assert report["deleted"] == ["news-dedup-items-v1-2026.09.18"]
    assert report["would_delete"] == []
    assert client.indices.deleted == ["news-dedup-items-v1-2026.09.18"]
