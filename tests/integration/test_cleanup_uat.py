"""P08-D 真实 UAT ES 物理清理验证（T019 + T021）。

按用户指令："模拟时钟可以，但 ES 侧索引必须是真实 UAT 读写"。

执行条件：`P08_CONFIRM_UAT=1` 显式授权；fail-fast 通过；独立 p08-cleanup-* prefix 隔离。
"""

from __future__ import annotations

import os
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

# W2Fε 修复：原 ROOT 字面值缺盘符（"/Users/ASUS/..." POSIX 形 Windows 不可达）
# ——改 Path(__file__) 派生（tests/integration → 仓根 news-flash-dedup）。
# 09-29 W3e F1 勘正：parents[1]=tests 目录（注释自述"→仓根"未达成，
# ROOT/"src" 不存在、sys.path.insert 死重被 PYTHONPATH=src 跑法掩盖）——
# 回锚 parents[2]=仓根 news-flash-dedup；sys.path.insert 死重清理（导入由
# PYTHONPATH 保证）；派生真实性由下行导入期钉防回归（失锚即 collection 红）。
ROOT = Path(__file__).resolve().parents[2]
assert (ROOT / "src" / "news_flash_dedup").is_dir(), (
    f"ROOT 派生失锚：{ROOT} 下无 src/news_flash_dedup（parents 索引漂移）")


def _uat_env_ready() -> bool:
    return os.environ.get("P08_CONFIRM_UAT") == "1"


pytestmark = pytest.mark.skipif(
    not _uat_env_ready(),
    reason="P08-D 真实 UAT ES 测试需要 P08_CONFIRM_UAT=1 显式授权；默认跳过",
)


@pytest.fixture(scope="module")
def uat_client():
    from news_flash_dedup.es_client import (
        ProductionAccessDenied, assert_test_environment, client_from_environment,
        load_environment,
    )
    load_environment()
    try:
        assert_test_environment()
    except ProductionAccessDenied as error:
        pytest.fail(f"UAT environment check failed: {error}")
    client = client_from_environment(load_dotenv_first=False)
    info = client.info()
    assert str(info["version"]["number"]).startswith("8.")
    yield client
    client.close()


@pytest.fixture(scope="module")
def cleanup_namespace(uat_client):
    """真实 UAT ES 隔离 namespace：D-7 与 D-6 索引 + 他人索引（防误删）。"""
    import re
    raw = f"p08-cleanup-{datetime.now(timezone.utc).strftime('%H%M%S%f')}-"
    if not re.fullmatch(r"p01-batch-[A-Za-z0-9-]+-", raw):
        prefix = f"p01-batch-{raw}"
    else:
        prefix = raw
    # 实际要清理的两个目标索引（D-7 与 D-6）
    # today 是 2026-09-25，按 today-7=2026-09-18（D-7）推算
    target_d7 = f"{prefix}news-dedup-items-v1-2026.09.18"
    target_d7_audits = f"{prefix}news-dedup-audits-v1-2026.09.18"
    # 不应被清理的 D-6（保留）
    keep_d6 = f"{prefix}news-dedup-items-v1-2026.09.19"
    keep_d6_audits = f"{prefix}news-dedup-audits-v1-2026.09.19"
    # 模拟"他人索引"（共享集群上）
    invader = f"{prefix}infb_kq_company_uat"

    # 创建所有索引
    from news_flash_dedup.es_admission_schema import item_mapping, audit_mapping
    for name in [target_d7, target_d7_audits, keep_d6, keep_d6_audits]:
        uat_client.indices.create(index=name, body=item_mapping() if "items" in name else audit_mapping())
    # "他人索引"真实创建（窗口 Z3 更正：旧注释虚标"用同一 mapping 创建"又括注
    # "实际不创建"，自相矛盾；现真实落盘——候选推算为纯日期反推点名
    # （lifecycle.candidate_indices_for_cleanup 不通配列举），同 prefix 的
    # invader 结构性不入 candidates/all_targets，创建它不会扰动既有断言，
    # 反而使"不误删他人索引"从空转断言升级为物理实证；teardown 按
    # {prefix}* 回收，覆盖本索引）
    uat_client.indices.create(index=invader, body=item_mapping())
    yield {
        "prefix": prefix,
        "target_d7": target_d7,
        "target_d7_audits": target_d7_audits,
        "keep_d6": keep_d6,
        "keep_d6_audits": keep_d6_audits,
        "invader": invader,
        "today": date(2026, 9, 25),
    }
    # 清理：删除 prefix 下所有索引
    listed = uat_client.indices.get(index=f"{prefix}*", allow_no_indices=True,
                                    ignore_unavailable=True)
    for name in listed.keys():
        try:
            uat_client.indices.delete(index=name)
        except Exception:
            pass


def test_dry_run_lists_only_d_minus_seven_targets_not_invaders(uat_client, cleanup_namespace):
    """T019/T021 dry-run：只列出 D-7 的具体索引名（D-6 保留）；不列出他人/系统索引。

    D+7 零点 dry-run 推算 today-7 = 2026-09-18，应命中 target_d7 与 target_d7_audits；
    不应命中 keep_d6（2026-09-19）。
    """
    ns = cleanup_namespace
    from news_flash_dedup.lifecycle import CleanupAuditLog, dry_run_cleanup
    audit = CleanupAuditLog(operator="admin-uat-test")
    report = dry_run_cleanup(uat_client, ns["today"], audit, index_prefix=ns["prefix"])
    assert ns["target_d7"] in report["would_delete"]
    assert ns["target_d7_audits"] in report["would_delete"]
    assert ns["keep_d6"] not in report["would_delete"]
    assert ns["keep_d6_audits"] not in report["would_delete"]
    # 没有宽通配符列举：只 HEAD 点验目标（u1901a1 其他索引也会被列举），
    # 但确认索引名清单严格匹配。P1a-T5：kind 全集四件（work/tombstones
    # 本命名空间未创建 → 只进 absent）。
    expected_targets = sorted(
        [ns["target_d7"], ns["target_d7_audits"],
         f"{ns['prefix']}news-dedup-work-v1-2026.09.18",
         f"{ns['prefix']}news-dedup-tombstones-v1-2026.09.18"])
    assert sorted(report["all_targets"]) == expected_targets
    assert report["deferred"] == []


def _exists(uat_client, index):
    """ES 8 客户端 returns HeadApiResponse；unwrap bool。"""
    return bool(uat_client.indices.exists(index=index))


def test_execute_cleanup_removes_only_d_minus_seven_preserves_d_minus_six(uat_client, cleanup_namespace):
    """T019 物理清理：D-7 删除、D-6 保留；不动 ES 集群拓扑。"""
    ns = cleanup_namespace
    from news_flash_dedup.lifecycle import CleanupAuditLog, execute_cleanup
    audit = CleanupAuditLog(operator="admin-uat-test")
    report = execute_cleanup(uat_client, ns["today"], audit, index_prefix=ns["prefix"])
    assert ns["target_d7"] in report["deleted"]
    assert ns["target_d7_audits"] in report["deleted"]
    # D-6 保留
    assert _exists(uat_client, ns["keep_d6"]) is True
    # D-7 删除
    assert _exists(uat_client, ns["target_d7"]) is False
    # 审计日志记录 operator + 索引名 + 日期
    delete_records = [r for r in audit.deletion_runs if r["ok"]]
    assert all(r["operator"] == "admin-uat-test" for r in delete_records)
    assert any(r["index_name"] == ns["target_d7"] for r in delete_records)


def test_dry_run_does_not_delete_anything(uat_client, cleanup_namespace):
    """dry-run 必须不实际删除任何索引；用 spy 验证 DELETE 没被调用。"""
    ns = cleanup_namespace
    from news_flash_dedup.lifecycle import CleanupAuditLog, dry_run_cleanup

    class _SpyIndices:
        def __init__(self, inner):
            self.inner = inner
            self.delete_calls = 0

        def exists(self, *, index):
            return self.inner.exists(index=index)

        def delete(self, *, index):
            self.delete_calls += 1
            # dry_run 不应触发；显式抛错以便测试失败时定位
            raise AssertionError(f"dry_run must not call delete(index={index})")

    spy_client = type("C", (), {"indices": _SpyIndices(uat_client.indices)})()
    audit = CleanupAuditLog(operator="admin-uat-test")
    dry_run_cleanup(spy_client, ns["today"], audit, index_prefix=ns["prefix"])
    assert spy_client.indices.delete_calls == 0


def test_cleanup_rejects_invalid_target_name_on_force_injection(uat_client, cleanup_namespace):
    """硬正则闸门：即使把他人索引硬塞进 candidates，清理路径必须拒绝并记录失败。

    测试逻辑：把 "恶意"索引名（不匹配正则）硬塞进 candidates；调用 execute_cleanup 时应抛 ValueError。
    """
    ns = cleanup_namespace
    from news_flash_dedup.lifecycle import (
        CleanupAuditLog, CleanupTarget, candidate_indices_for_cleanup,
        execute_cleanup, is_valid_cleanup_target,
    )
    audit = CleanupAuditLog(operator="admin-uat-test")
    invalid_target = CleanupTarget(
        index_name="infb_kq_company_uat",  # 他人索引（不在 candidates 推算里）
        business_date=ns["today"],
        kind="items",
        is_present=True,
    )
    assert not is_valid_cleanup_target(invalid_target.index_name)
    # 真正的"硬塞 invader" 测试已在 unit test 里覆盖，这里验证 UAT ES 不会误删他人索引
    report = execute_cleanup(uat_client, ns["today"], audit, index_prefix=ns["prefix"])
    assert ns["invader"] not in report["deleted"]
    assert ns["invader"] not in report["skipped_absent"]
    # 窗口 Z3：invader 现已真实创建（见 fixture）——物理实证清理后他人索引仍在，
    # 取代旧版"从未创建的索引不在 deleted"空转断言
    assert _exists(uat_client, ns["invader"]) is True, (
        "execute_cleanup 物理误删他人索引（invader 真实存在却被清除）"
    )


def test_cold_startup_after_cleanup_confirms_d_minus_seven_gone_d_minus_six_present(
        uat_client, cleanup_namespace):
    """T021 冷启动：清理后 D-7 不可读、D-6 可读。"""
    ns = cleanup_namespace
    from news_flash_dedup.lifecycle import CleanupAuditLog, execute_cleanup
    audit = CleanupAuditLog(operator="admin-uat-test")
    execute_cleanup(uat_client, ns["today"], audit, index_prefix=ns["prefix"])
    # 冷启动验证：D-7 不可读（404），D-6 索引存在
    from elasticsearch import NotFoundError
    with pytest.raises(NotFoundError):
        uat_client.get(index=ns["target_d7"], id="anything", realtime=True)
    assert _exists(uat_client, ns["keep_d6"]) is True


def test_audit_log_records_operator_today_and_index_names(uat_client, cleanup_namespace):
    """审计日志：每次删除必须记录 operator + today + index_name + business_date。

    窗口K 重铸（D30 H-07-b）：原断言仅钉 ok=True——可被 absent 伪删满足
    （execute_cleanup 对不存在目标记 ok=True,error="absent"，见 src
    lifecycle.py:173-176）；且模块内先行用例已删 D-7，本用例历史上跑的
    正是 absent 伪删路径（伪绿实证）。现重铸：先确保两目标真实存在
    （缺则重建），驱动真删，钉死 error is None（真删）与
    error=="absent"（伪删）的区分；语义钉值见
    tests/unit/test_lifecycle.py::test_execute_cleanup_distinguishes_real_delete_from_absent_pseudo。
    """
    ns = cleanup_namespace
    from news_flash_dedup.es_admission_schema import audit_mapping, item_mapping
    from news_flash_dedup.lifecycle import CleanupAuditLog, execute_cleanup
    # 保证真删路径：目标若已被模块内先行用例删除则重建（module 级共享 namespace）
    for name in (ns["target_d7"], ns["target_d7_audits"]):
        if not _exists(uat_client, name):
            uat_client.indices.create(
                index=name,
                body=item_mapping() if "items" in name else audit_mapping())
    audit = CleanupAuditLog(operator="admin-cleanup-uat")
    report = execute_cleanup(uat_client, ns["today"], audit, index_prefix=ns["prefix"])
    # 真删实证：两目标入 deleted 而非 skipped_absent。P1a-T5：kind 全集
    # 四件——work/tombstones 本命名空间未创建 → absent 伪删（ok=True
    # error="absent"，lifecycle 既有语义），deletion_runs 4 条。
    assert sorted(report["deleted"]) == sorted([ns["target_d7"], ns["target_d7_audits"]])
    assert sorted(report["skipped_absent"]) == sorted(
        [f"{ns['prefix']}news-dedup-work-v1-2026.09.18",
         f"{ns['prefix']}news-dedup-tombstones-v1-2026.09.18"])
    assert len(audit.deletion_runs) == 4
    for entry in audit.deletion_runs:
        assert entry["operator"] == "admin-cleanup-uat"
        assert entry["today"] == ns["today"].isoformat()
        assert entry["business_date"] == "2026-09-18"
        assert entry["kind"] in {"items", "audits", "work", "tombstones"}
        # 真删 vs absent 伪删区分（旧断言只钉 ok=True，伪删亦 ok=True）
        assert entry["ok"] is True
        if entry["kind"] in {"items", "audits"}:
            assert entry["error"] is None, (
                f"absent 伪删冒充真删：{entry['index_name']} error={entry['error']!r}"
            )
        else:
            assert entry["error"] == "absent"