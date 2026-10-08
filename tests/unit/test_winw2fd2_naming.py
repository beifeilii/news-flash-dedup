"""W2Fδ2 红测：条36 D25 命名四位一体裁定实施（09-28 用户授权主窗口候选）。

裁定形态：建/闸/访/清全走 index_prefix+逻辑名——
- 建：provision/required_indices 产 index_prefix+逻辑名（对齐 bench/probe 两原件）；
- 闸：fresh-namespace 闸与创建名同前缀维度（旧码闸查前缀、建用 canonical=脱节）；
- 访：与 ElasticsearchBatchStore._physical 强制前缀逐字相等（配对连通，旧码必 404）；
- 清：所建日索引名过 lifecycle 双兼容硬正则闸；
+ 半创建清理（条目36③解禁）：create 失败（传输异常或未全 ack）时清理本批
  已建索引及当事索引；补偿失败不掩盖原异常（10 §9.3/L458：delete 404 视为已清理）。

先证红（旧无前缀形/无清理失败）后修复转绿。只新建不改既有套件。
"""

from __future__ import annotations

from datetime import date

import pytest

from news_flash_dedup.es_admission_index import (
    ProvisionError,
    provision_isolated_indices,
    required_indices,
)


# ---------- fake 客户端 ----------

class _Indices:
    """dict-fake：listing 恒空（fresh namespace）；可注入 create 失败点/ack 形态/delete 失败集。"""

    def __init__(self, *, fail_create_at=None, ack=True, delete_failures=(), delete_404=()):
        self.created: list[str] = []
        self.deleted: list[str] = []
        self._fail_create_at = fail_create_at
        self._ack = ack
        self._delete_failures = set(delete_failures)
        self._delete_404 = set(delete_404)

    def get(self, *, index, allow_no_indices, ignore_unavailable):
        return {}

    def create(self, *, index, body):
        if self._fail_create_at is not None and len(self.created) == self._fail_create_at:
            raise ConnectionError("synthetic transport failure")
        self.created.append(index)
        return {"acknowledged": self._ack, "shards_acknowledged": self._ack}

    def delete(self, *, index):
        self.deleted.append(index)
        if index in self._delete_404:
            error = RuntimeError("synthetic not found")
            error.status_code = 404
            raise error
        if index in self._delete_failures:
            raise TimeoutError("synthetic delete timeout")

    def get_settings(self, *, index):
        return {
            name: {"settings": {"index": {"number_of_shards": "1",
                                           "number_of_replicas": "0"}}}
            for name in index.split(",")
        }


class _SettingsBoomIndices(_Indices):
    def get_settings(self, *, index):
        raise TimeoutError("synthetic settings timeout")


class _Client:
    def __init__(self, indices):
        self.indices = indices


DAY = date(2026, 9, 28)
PREFIX = "p01-batch-w2fd2-"


# ---------- required_indices 带前缀形 ----------

def test_required_indices_with_prefix_returns_prefixed_names():
    """四位一体（建）：required_indices 接受 index_prefix，六名全带前缀且逻辑尾部不变。"""
    prefixed = required_indices(DAY, index_prefix=PREFIX)
    canonical = required_indices(DAY)
    assert len(prefixed) == 6
    for name in prefixed:
        assert name.startswith(PREFIX)
    assert {name[len(PREFIX):] for name in prefixed} == set(canonical)


def test_required_indices_canonical_default_unchanged():
    """守卫钉：缺省仍产 10 §2 canonical 生产形（无前缀），四位一体不污染缺省口径。"""
    names = required_indices(DAY)
    assert "news-dedup-control-v1" in names
    assert "news-dedup-requests-v1" in names
    assert "news-dedup-items-v1-2026.09.28" in names
    assert "news-dedup-audits-v1-2026.09.29" in names
    assert all(not name.startswith("p01-batch-") for name in names)


def test_required_indices_rejects_invalid_prefix():
    """fail-closed：非 p01-batch-<run-id>- 前缀即拒（空串=canonical 合法除外）。"""
    for bad in ("news-dedup-", "p01-batch-", "p01-batch-run1", "p01-batch-有-", "p02-x-"):
        with pytest.raises(ValueError, match="isolated prefix"):
            required_indices(DAY, index_prefix=bad)


# ---------- provision 建/闸/访/清同口径 ----------

def test_provision_creates_prefixed_names_pairable_with_batch_store():
    """四位一体（建+访）：create 物理名 = prefix+逻辑名，且与
    ElasticsearchBatchStore._physical 强制前缀逐字相等（旧码 canonical 名必 404）。"""
    from news_flash_dedup.batch_es_store import ElasticsearchBatchStore

    indices = _Indices()
    result = provision_isolated_indices(_Client(indices), PREFIX, business_day=DAY)
    expected = set(required_indices(DAY, index_prefix=PREFIX).keys())
    assert set(indices.created) == expected
    assert len(indices.created) == 6
    store = ElasticsearchBatchStore(_Client(_Indices()), index_prefix=PREFIX)
    for logical in required_indices(DAY):
        assert store._physical(logical) in expected  # 访：配对连通，不再 404
    assert set(result["topology"]) == expected


def test_provision_created_names_pass_lifecycle_gates():
    """四位一体（清）：所建 items/audits 日索引名过 lifecycle 日索引硬正则闸；
    requests 名过文档级硬正则闸（双兼容形带前缀支路）。"""
    from news_flash_dedup.lifecycle import (
        _CLEANUP_TARGET_PATTERN,
        _EXPIRED_DOCUMENT_INDEX_PATTERN,
    )

    indices = _Indices()
    provision_isolated_indices(_Client(indices), PREFIX, business_day=DAY)
    day_names = [n for n in indices.created if "-items-v1-" in n or "-audits-v1-" in n]
    assert len(day_names) == 4
    for name in day_names:
        assert _CLEANUP_TARGET_PATTERN.fullmatch(name), name
    assert _EXPIRED_DOCUMENT_INDEX_PATTERN.fullmatch(PREFIX + "news-dedup-requests-v1")


# ---------- 半创建清理（条目36③解禁） ----------

def test_create_failure_cleans_half_created_indices():
    """③：第 3 个 create 传输失败 → ProvisionError 保链 + 本批已建 2 个及当事
    索引逐个点名删除补偿（补偿清单挂异常属性，不静默）。"""
    indices = _Indices(fail_create_at=2)
    with pytest.raises(ProvisionError) as captured:
        provision_isolated_indices(_Client(indices), PREFIX, business_day=DAY)
    error = captured.value
    assert error.reason == "index_create_failed"
    assert isinstance(error.__cause__, ConnectionError)
    expected_names = list(required_indices(DAY, index_prefix=PREFIX).keys())
    failed_name = expected_names[2]
    assert indices.created == expected_names[:2]
    assert set(indices.deleted) == {*expected_names[:2], failed_name}
    assert set(error.cleanup_deleted) == {*expected_names[:2], failed_name}
    assert error.cleanup_failed == ()


def test_unacknowledged_create_triggers_cleanup():
    """③并案：create 未全 ack（索引极可能已物理建出）→ RuntimeError + 清理
    含当事索引；旧码直接泄漏半创建集。"""
    indices = _Indices(ack=False)
    with pytest.raises(RuntimeError, match="not fully acknowledged"):
        provision_isolated_indices(_Client(indices), PREFIX, business_day=DAY)
    first_name = next(iter(required_indices(DAY, index_prefix=PREFIX)))
    assert indices.created == [first_name]
    assert indices.deleted == [first_name]


def test_cleanup_delete_failure_does_not_mask_primary_error():
    """③纪律：补偿删除失败不掩盖原异常——原 __cause__ 不变，
    失败名+异常类型记 cleanup_failed（不静默、不冒充已清理）。"""
    names = list(required_indices(DAY, index_prefix=PREFIX).keys())
    indices = _Indices(fail_create_at=1, delete_failures={names[0]})
    with pytest.raises(ProvisionError) as captured:
        provision_isolated_indices(_Client(indices), PREFIX, business_day=DAY)
    error = captured.value
    assert isinstance(error.__cause__, ConnectionError)
    assert error.cleanup_deleted == (names[1],)
    assert error.cleanup_failed == ((names[0], "TimeoutError"),)


def test_cleanup_delete_404_counts_as_cleaned():
    """③口径（10 §9.3/L458"404 视为已清理"）：delete 404 → 计入 cleanup_deleted，
    不算补偿失败。"""
    names = list(required_indices(DAY, index_prefix=PREFIX).keys())
    indices = _Indices(fail_create_at=1, delete_404={names[0]})
    with pytest.raises(ProvisionError) as captured:
        provision_isolated_indices(_Client(indices), PREFIX, business_day=DAY)
    error = captured.value
    assert set(error.cleanup_deleted) == {names[0], names[1]}
    assert error.cleanup_failed == ()


def test_settings_phase_failure_does_not_cleanup():
    """③边界：清理仅限 create 失败路径——settings 阶段失败不删本批索引
    （全量已建留待拓扑核查；旧 _SettingsBrokenIndices 形禁碰面对照同语义）。"""
    indices = _SettingsBoomIndices()
    with pytest.raises(ProvisionError) as captured:
        provision_isolated_indices(_Client(indices), PREFIX, business_day=DAY)
    assert captured.value.reason == "index_settings_failed"
    assert indices.deleted == []
