"""W2Fδ 修复窗口红测：条36（provision create 异常包装）/条75（G0 store get 对齐）/条77（L-3/L-4）。

只新建不改既有套件。每项先证红（旧行为失败）后修复转绿。
"""

from __future__ import annotations

from datetime import date

import pytest


# ---------- 条36：es_admission_index create 异常包装 ProvisionError 形 ----------

class _CreateBoomIndices:
    def get(self, *, index, allow_no_indices, ignore_unavailable):
        return {}

    def create(self, *, index, body):
        raise ConnectionError("synthetic transport failure")

    def get_settings(self, *, index):  # pragma: no cover - 不应到达
        raise AssertionError("must not reach settings after create failure")


class _Client:
    def __init__(self, indices):
        self.indices = indices


def test_create_exception_wrapped_as_provision_error_with_chain():
    """条36：create 裸穿 → ProvisionError("index_create_failed")，__cause__ 保链。"""
    from news_flash_dedup.es_admission_index import (
        ProvisionError, provision_isolated_indices,
    )
    with pytest.raises(ProvisionError) as captured:
        provision_isolated_indices(
            _Client(_CreateBoomIndices()), "p01-batch-w2fd36-",
            business_day=date(2026, 9, 28))
    assert captured.value.reason == "index_create_failed"
    assert isinstance(captured.value.__cause__, ConnectionError)


def test_provision_error_reasons_vocabulary_matches_original():
    """条36：与 scripts/bench_batch_admission.py 原件同形——固定 reason 码表。"""
    from news_flash_dedup.es_admission_index import ProvisionError
    assert ProvisionError.REASONS == frozenset({
        "prefix_exists", "index_listing_failed", "index_listing_invalid",
        "index_create_failed", "index_settings_failed",
    })
    with pytest.raises(ValueError):
        ProvisionError("not-a-reason")


def test_provision_docstring_marks_batch_store_unpairable():
    """条36：防误接线标注——provision×ElasticsearchBatchStore 当前不可配对。"""
    import news_flash_dedup.es_admission_index as module
    assert "不可配对" in (module.__doc__ or "")
    assert "不可配对" in (module.provision_isolated_indices.__doc__ or "")


def test_batch_store_docstring_marks_provision_unpairable():
    import news_flash_dedup.batch_es_store as module
    assert "不可配对" in (module.ElasticsearchBatchStore.__doc__ or "") or \
           "不可配对" in (module.__doc__ or "")


# ---------- 条77（L-3）：prefix startswith → fullmatch ----------

def test_prefix_requires_run_id_and_trailing_dash():
    """L-3：startswith 时代 'p01-batch-'（空 run-id）/缺尾横杠均被放过 → fullmatch 拒绝。"""
    from news_flash_dedup.es_admission_index import provision_isolated_indices
    for bad in ("p01-batch-", "p01-batch-run1", "p01-batch-", "p01-batch-有-"):
        with pytest.raises(ValueError, match="isolated prefix"):
            provision_isolated_indices(
                _Client(_CreateBoomIndices()), bad, business_day=date(2026, 9, 28))


def test_prefix_fullmatch_accepts_canonical_run_prefix():
    """L-3：合法 p01-batch-<runid>- 不因收紧误拒（过闸后在 create 处失败为证）。"""
    from news_flash_dedup.es_admission_index import provision_isolated_indices
    with pytest.raises(Exception, match="index_create_failed"):
        provision_isolated_indices(
            _Client(_CreateBoomIndices()), "p01-batch-run42-x-",
            business_day=date(2026, 9, 28))


# ---------- 条77（L-4）：settings 裸下标 fail-closed 包装 ----------

class _SettingsBrokenIndices:
    def __init__(self, settings_response):
        self._settings_response = settings_response
        self.created = []

    def get(self, *, index, allow_no_indices, ignore_unavailable):
        return {}

    def create(self, *, index, body):
        self.created.append(index)
        return {"acknowledged": True, "shards_acknowledged": True}

    def get_settings(self, *, index):
        return self._settings_response


@pytest.mark.parametrize("settings_response", [
    {},                                                   # 缺索引键
    {"news-dedup-control-v1": {}},                        # 缺 settings 层
    {"news-dedup-control-v1": {"settings": {"index": {
        "number_of_shards": "not-an-int",
        "number_of_replicas": "0"}}}},                    # 非整数
])
def test_settings_malformed_fails_closed_not_raw_keyerror(settings_response):
    """L-4：settings 响应畸形 → ProvisionError('index_settings_failed') 保链，非裸 KeyError。"""
    from news_flash_dedup.es_admission_index import (
        ProvisionError, provision_isolated_indices,
    )
    indices = _SettingsBrokenIndices(settings_response)
    with pytest.raises(ProvisionError) as captured:
        provision_isolated_indices(
            _Client(indices), "p01-batch-w2fd77-", business_day=date(2026, 9, 28))
    assert captured.value.reason == "index_settings_failed"
    assert captured.value.__cause__ is not None


def test_get_settings_exception_wrapped():
    """L-4 并案：get_settings 调用本身异常同样包装（裸穿 fail-open 向）。"""
    from news_flash_dedup.es_admission_index import (
        ProvisionError, provision_isolated_indices,
    )

    class _SettingsBoom(_SettingsBrokenIndices):
        def get_settings(self, *, index):
            raise TimeoutError("synthetic settings timeout")

    with pytest.raises(ProvisionError) as captured:
        provision_isolated_indices(
            _Client(_SettingsBoom(None)), "p01-batch-w2fd77b-",
            business_day=date(2026, 9, 28))
    assert captured.value.reason == "index_settings_failed"
    assert isinstance(captured.value.__cause__, TimeoutError)


# ---------- 条75：es_admission_store get 对齐 batch 侧修复语义 ----------

class _FakeNotFound(Exception):
    def __init__(self, status_code=404, body=None):
        super().__init__("synthetic 404")
        self.status_code = status_code
        self.body = body


class _GetClient:
    def __init__(self, *, result=None, error=None):
        self._result = result
        self._error = error
        self.calls = []

    def get(self, **kwargs):
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        return self._result


def _store(client, prefix=""):
    from news_flash_dedup.es_admission_store import ElasticsearchAdmissionStore
    return ElasticsearchAdmissionStore(client, index_prefix=prefix)


def _full_hit(index="idx", key="id"):
    return {"_index": index, "_id": key, "found": True,
            "_source": {"value": "v"}, "_seq_no": 7, "_primary_term": 3,
            "_shards": {"total": 1, "successful": 1, "failed": 0}}


def test_get_confirmed_miss_returns_none():
    """条75：404 + body found:false + 身份正确 = 明确文档未命中 → None。"""
    body = {"_index": "idx", "_id": "id", "found": False}
    store = _store(_GetClient(error=_FakeNotFound(body=body)))
    assert store.get("idx", "id") is None


def test_get_404_without_confirmed_miss_body_raises():
    """条75（核心红）：404 无 body/无 found:false —— 旧码静默 None（索引/分片故障被
    冒充成文档缺失）→ 对齐后 AdmissionReadError 保链。"""
    from news_flash_dedup.es_admission_store import AdmissionReadError
    store = _store(_GetClient(error=_FakeNotFound(body=None)))
    with pytest.raises(AdmissionReadError) as captured:
        store.get("idx", "id")
    assert captured.value.__cause__ is not None


def test_get_404_with_wrong_identity_raises():
    """条75：404 body 的 _index/_id 与本请求不符 → 未命中身份不被承认。"""
    from news_flash_dedup.es_admission_store import AdmissionReadError
    body = {"_index": "other", "_id": "id", "found": False}
    store = _store(_GetClient(error=_FakeNotFound(body=body)))
    with pytest.raises(AdmissionReadError):
        store.get("idx", "id")


def test_get_success_with_shard_failure_raises():
    """条75：成功响应 _shards.failed != 0 → 读取未证实。"""
    from news_flash_dedup.es_admission_store import AdmissionReadError
    hit = _full_hit()
    hit["_shards"] = {"total": 2, "successful": 1, "failed": 1}
    store = _store(_GetClient(result=hit))
    with pytest.raises(AdmissionReadError):
        store.get("idx", "id")


def test_get_success_without_identity_raises():
    """条75：成功响应缺 _index/_id/found（身份未证实）→ AdmissionReadError。"""
    from news_flash_dedup.es_admission_store import AdmissionReadError
    store = _store(_GetClient(result={
        "_source": {"value": "v"}, "_seq_no": 7, "_primary_term": 3}))
    with pytest.raises(AdmissionReadError):
        store.get("idx", "id")


def test_get_success_found_false_inline_returns_none():
    """条75：200 形态但 found:false 且无 _source = 明确未命中 → None。"""
    store = _store(_GetClient(result={"_index": "idx", "_id": "id", "found": False}))
    assert store.get("idx", "id") is None


def test_get_full_valid_hit_returns_document():
    """条75：完整身份+版本的成功响应照返（对齐后行为不退化）。"""
    store = _store(_GetClient(result=_full_hit()))
    assert store.get("idx", "id") == {
        "source": {"value": "v"}, "seq_no": 7, "primary_term": 3}


def test_get_transport_error_keeps_chain():
    """条75：非 404 传输异常 → AdmissionReadError from error（保链纪律）。"""
    from news_flash_dedup.es_admission_store import AdmissionReadError
    store = _store(_GetClient(error=ConnectionError("synthetic")))
    with pytest.raises(AdmissionReadError) as captured:
        store.get("idx", "id")
    assert isinstance(captured.value.__cause__, ConnectionError)
