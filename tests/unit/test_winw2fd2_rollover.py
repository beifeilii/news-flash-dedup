"""W2Fδ2 红测：条37/N31 日索引滚动创建（owner=部署侧 provisioning，09-28 用户批准）。

scripts/provision_day_rollover.py：为指定 business_date 创建 items/audits 日索引——
- 复用 es_admission_schema 映射 + 10 §4.2 settings（shards=1/refresh 1s/dedup_cjk，
  副本按 UAT 惯例 0——10 §2 L32 明载实际副本由压测决定）；
- 前缀口径与 W2Fδ2 四位一体裁定一致：缺省 "" = 10 §2 canonical 生产形，
  非空必须全匹配 p01-batch-<run-id>-（UAT 隔离形）；
- 硬正则校验：目标名必须过 lifecycle 日索引硬正则闸（不过即拒，fail-closed）；
- 幂等=已存在跳过；--dry-run 默认真（零写）；--execute 才建；UAT 闸 DEPLOY_ENV=test。

先证红（脚本不存在/行为缺失）后修复转绿。全程 fake client，禁触网。
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import provision_day_rollover as rollover  # noqa: E402

DAY = date(2026, 9, 29)
ITEMS = "news-dedup-items-v1-2026.09.29"
AUDITS = "news-dedup-audits-v1-2026.09.29"
WORK = "news-dedup-work-v1-2026.09.29"
PREFIX = "p01-batch-w2fd2-"


class _FakeIndices:
    def __init__(self, existing=(), *, create_error=None, ack=True):
        self._existing = set(existing)
        self._create_error = create_error
        self._ack = ack
        self.created: list[str] = []
        self.exists_calls: list[str] = []

    def exists(self, *, index):
        self.exists_calls.append(index)
        return index in self._existing

    def create(self, *, index, body):
        if self._create_error is not None:
            raise self._create_error
        self.created.append(index)
        self._existing.add(index)
        return {"acknowledged": self._ack, "shards_acknowledged": self._ack}


class _FakeClient:
    def __init__(self, indices):
        self.indices = indices


# ---------- 干跑默认 ----------

def test_dry_run_is_default_and_writes_nothing():
    """--dry-run 默认真：plan 只报将创建/已存在，零 create 调用；
    CLI 缺省 execute=False。"""
    args = rollover.parse_args(["--business-date", "2026-09-29"])
    assert args.execute is False
    indices = _FakeIndices()
    report = rollover.plan_day_rollover(_FakeClient(indices), DAY)
    assert report["dry_run"] is True
    assert report["would_create"] == [ITEMS, AUDITS, WORK]
    assert report["skipped_existing"] == []
    assert indices.created == []  # 零写
    assert set(indices.exists_calls) == {ITEMS, AUDITS, WORK}  # HEAD 点验为只读


# ---------- 幂等=已存在跳过 ----------

def test_execute_creates_missing_and_skips_existing_idempotent():
    """幂等：部分已存在只补缺失；再跑全跳过零创建（重复执行不报错不重建）。"""
    indices = _FakeIndices(existing={ITEMS})
    client = _FakeClient(indices)
    report = rollover.execute_day_rollover(client, DAY)
    assert report["dry_run"] is False
    assert report["created"] == [AUDITS, WORK]
    assert report["skipped_existing"] == [ITEMS]
    again = rollover.execute_day_rollover(client, DAY)
    assert again["created"] == []
    assert again["skipped_existing"] == [ITEMS, AUDITS, WORK]
    assert indices.created == [AUDITS, WORK]  # 两轮合计仅 2 次 create


def test_execute_all_existing_is_full_skip():
    """幂等边界：全部已存在 → 零 create、全 skipped_existing。"""
    indices = _FakeIndices(existing={ITEMS, AUDITS, WORK})
    report = rollover.execute_day_rollover(_FakeClient(indices), DAY)
    assert report["created"] == []
    assert report["skipped_existing"] == [ITEMS, AUDITS, WORK]
    assert indices.created == []


# ---------- 异形名拒（硬正则校验 + 前缀口径） ----------

def test_invalid_prefix_rejected_before_any_client_call():
    """异形名拒：非 p01-batch-<run-id>- 前缀即 ValueError，且在任何
    exists/create 之前（fail-closed，零集群交互）。"""
    for bad in ("news-dedup-", "p01-batch-", "p01-batch-run1", "p01-batch-有-", "x"):
        indices = _FakeIndices()
        with pytest.raises(ValueError, match="isolated prefix"):
            rollover.rollover_indices(DAY, index_prefix=bad)
        with pytest.raises(ValueError, match="isolated prefix"):
            rollover.plan_day_rollover(_FakeClient(indices), DAY, index_prefix=bad)
        with pytest.raises(ValueError, match="isolated prefix"):
            rollover.execute_day_rollover(_FakeClient(indices), DAY, index_prefix=bad)
        assert indices.exists_calls == []
        assert indices.created == []


def test_generated_names_pass_lifecycle_hard_regex():
    """硬正则校验：canonical 与带前缀两形目标名全过 lifecycle 日索引硬正则闸
    （与清口径四位一体；脚本内部构造期即闸，永不产异形名）。"""
    from news_flash_dedup.lifecycle import _CLEANUP_TARGET_PATTERN

    for prefix in ("", PREFIX):
        names = rollover.rollover_indices(DAY, index_prefix=prefix)
        # P1a-T5：滚动集三件（items/audits/work）——tombstones 不进滚动
        assert set(names) == {prefix + ITEMS, prefix + AUDITS, prefix + WORK}
        for name in names:
            assert _CLEANUP_TARGET_PATTERN.fullmatch(name), name


# ---------- 映射与 settings（10 §2/§4.2） ----------

def test_mappings_reuse_schema_and_doc10_settings():
    """复用 es_admission_schema 映射 + 10 §4.2 settings：items 带 dedup_cjk
    分析器/refresh 1s；shards=1、replicas=0（UAT 惯例，10 §2 L32 压测定副本）。"""
    mappings = rollover.rollover_indices(DAY)
    item = mappings[ITEMS]
    assert item["settings"]["number_of_shards"] == 1
    assert item["settings"]["number_of_replicas"] == 0
    assert item["settings"]["refresh_interval"] == "1s"
    assert item["settings"]["analysis"]["analyzer"]["dedup_cjk"]["filter"] == [
        "cjk_width", "lowercase", "cjk_bigram"]
    assert item["mappings"]["properties"]["text"]["analyzer"] == "dedup_cjk"
    audit = mappings[AUDITS]
    assert audit["settings"]["number_of_shards"] == 1
    assert audit["settings"]["number_of_replicas"] == 0
    assert audit["mappings"]["properties"]["payload"]["enabled"] is False


# ---------- 失败路径纪律 ----------

def test_create_exception_wrapped_as_provision_error():
    """create 传输异常 → ProvisionError("index_create_failed") from error（保链）。"""
    from news_flash_dedup.es_admission_index import ProvisionError

    indices = _FakeIndices(create_error=ConnectionError("synthetic"))
    with pytest.raises(ProvisionError) as captured:
        rollover.execute_day_rollover(_FakeClient(indices), DAY)
    assert captured.value.reason == "index_create_failed"
    assert isinstance(captured.value.__cause__, ConnectionError)


def test_unacknowledged_create_raises():
    """create 未 ack → RuntimeError（不冒充成功）。"""
    indices = _FakeIndices(ack=False)
    with pytest.raises(RuntimeError, match="not acknowledged"):
        rollover.execute_day_rollover(_FakeClient(indices), DAY)


# ---------- UAT 环境闸 ----------

def test_main_routes_through_uat_environment_gate():
    """UAT 闸：main 经 client_from_environment（内含 DEPLOY_ENV=test 闸）取客户端；
    非 test 环境即拒，零业务调用。全程注入伪工厂，禁触网。"""
    from news_flash_dedup.es_client import ProductionAccessDenied

    calls = []

    def _deny():
        raise ProductionAccessDenied("DEPLOY_ENV must be 'test' during P07 development")

    def _factory():
        calls.append("factory")
        return _FakeClient(_FakeIndices())

    class _ClosingClient(_FakeClient):
        def close(self):
            calls.append("close")

    import news_flash_dedup.es_client as es_client
    original = es_client.client_from_environment
    try:
        es_client.client_from_environment = _deny
        with pytest.raises(ProductionAccessDenied):
            rollover.main(["--business-date", "2026-09-29"])
        assert calls == []  # 闸拒绝后零业务交互

        indices = _FakeIndices()
        es_client.client_from_environment = lambda: _ClosingClient(indices)
        assert rollover.main(["--business-date", "2026-09-29"]) == 0  # dry-run 默认
        assert indices.created == []  # 默认零写
        assert "close" in calls  # 客户端必关
    finally:
        es_client.client_from_environment = original
