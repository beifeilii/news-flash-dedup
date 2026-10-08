# -*- coding: utf-8 -*-
"""W-A 族④红测（建议落 tests/unit/test_wina_family4_ungate.py）。

红态锚：④(a) 四通道 p01-batch- 硬闸（bm25:44/entity:28/hash:69/near:39）；
④(b) embedding_client.py:236 200-非-JSON 裸逃逸三态；④(c) worker.py:233-241
扫描无三闸+静默默认。
"""
from __future__ import annotations

import json

import pytest

from news_flash_dedup.recall.bm25_channel import BM25Channel
from news_flash_dedup.recall.entity_channel import EntityChannel
from news_flash_dedup.recall.hash_channel import HashChannel
from news_flash_dedup.recall.near_channel import NearChannel
from news_flash_dedup.recall.vector_store import VectorWriteUnknown
from news_flash_dedup.recall.worker import ElasticsearchRegistrationScanner
from news_flash_dedup.vector import embedding_client
from news_flash_dedup.vector.embedding_client import (
    EmbeddingClient,
    EmbeddingClientConfig,
)


# ==================== ④(a) 命名空间参数化 ====================

_CHANNELS = (BM25Channel, EntityChannel, HashChannel, NearChannel)


@pytest.mark.parametrize("cls", _CHANNELS)
def test_r4a1_non_p01_namespace_accepted_when_configured(cls):
    """配置允许 p17/p18 命名空间后，p17-batch-/p18-batch- 前缀可构造（解闸）。"""
    channel = cls(None, "p17-batch-wa1-", allowed_namespaces=(
        "p01-batch-", "p17-batch-", "p18-batch-"))
    assert channel.index_prefix == "p17-batch-wa1-"


@pytest.mark.parametrize("cls", _CHANNELS)
def test_r4a2_default_namespace_zero_regression(cls):
    """绿守卫：默认（不传 allowed_namespaces）接受集与现役逐字节等价——
    p01-batch- 可构，p17-batch- 仍拒。"""
    channel = cls(None, "p01-batch-p11-unit-")
    assert channel.index_prefix == "p01-batch-p11-unit-"
    with pytest.raises(ValueError):
        cls(None, "p17-batch-wa1-")
    with pytest.raises(ValueError):
        cls(None, "prod-news-dedup-")


@pytest.mark.parametrize("cls", _CHANNELS)
def test_r4a3_namespace_config_fail_closed(cls):
    """配置面 fail-closed：空元组/空串/大写/无尾横线命名空间一律 ValueError
    （防"空串=全放行"与大小写漂移）。"""
    for bad in ((), ("",), ("P01-BATCH-",), ("p01-batch",), ("p01 batch-",)):
        with pytest.raises(ValueError):
            cls(None, "p01-batch-x-", allowed_namespaces=bad)


@pytest.mark.parametrize("cls", _CHANNELS)
def test_r4a4_production_namespace_injection(cls):
    """生产形态：注入生产命名空间（如 prod-batch-）后可构，隔离语义不失。"""
    channel = cls(None, "prod-batch-main-", allowed_namespaces=("prod-batch-",))
    assert channel.index_prefix == "prod-batch-main-"
    with pytest.raises(ValueError):   # 白名单外仍拒
        cls(None, "p01-batch-p11-unit-", allowed_namespaces=("prod-batch-",))


# ==================== ④(b) 200-非-JSON 三态映射 ====================

class _FakeResp:
    def __init__(self, data: bytes):
        self._data = data

    def read(self, n=-1):
        if n is None or n < 0:
            return self._data
        return self._data[:n]

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def _client() -> EmbeddingClient:
    config = EmbeddingClientConfig(
        base_url="http://localhost/v1", model="text-embedding-v3",
        dimension=4, cache_root=None, daily_token_budget=10 ** 9)
    return EmbeddingClient(config, env={"DEDUP_EMBEDDING_API_KEY": "sk-test"},
                           persist_budget=False)


def _patch_transport(monkeypatch, data: bytes):
    monkeypatch.setattr(embedding_client.urllib.request, "urlopen",
                        lambda req, timeout=None: _FakeResp(data))


def test_r4b1_200_non_json_maps_vector_write_unknown(monkeypatch):
    """HTTP 200 + 坏 JSON 响应体 → VectorWriteUnknown（非裸 JSONDecodeError）。"""
    _patch_transport(monkeypatch, b"<html>gateway error page</html>")
    with pytest.raises(VectorWriteUnknown, match="embedding response is unknown"):
        _client().embed_query("测试文本")


def test_r4b2_200_bad_utf8_maps_vector_write_unknown(monkeypatch):
    """HTTP 200 + 坏 UTF-8 响应体 → VectorWriteUnknown（非裸 UnicodeDecodeError）。"""
    _patch_transport(monkeypatch, b"\xff\xfe\x00\x01")
    with pytest.raises(VectorWriteUnknown, match="embedding response is unknown"):
        _client().embed_query("测试文本")


def test_r4b3_valid_payload_green_guard(monkeypatch):
    """绿守卫：合法响应体解析/校验/返回逐字节不变。"""
    payload = json.dumps({
        "data": [{"index": 0, "embedding": [0.5, -0.25, 0.125, 1.0]}],
        "usage": {"total_tokens": 3},
    }).encode("utf-8")
    _patch_transport(monkeypatch, payload)
    vector = _client().embed_query("测试文本")
    assert vector == [0.5, -0.25, 0.125, 1.0]


def test_r4b4_oversize_still_unknown_green_guard(monkeypatch):
    """绿守卫：1MB 溢出族（:233-235 现役闸）不受本修复扰动。"""
    _patch_transport(monkeypatch, b" " * (1024 * 1024 + 8))
    with pytest.raises(VectorWriteUnknown, match="embedding response is unknown"):
        _client().embed_query("测试文本")


# ==================== ④(c) worker 扫描三闸 ====================

def _ok_page(docs):
    return {"timed_out": False, "_shards": {"total": 1, "successful": 1, "failed": 0},
            "hits": {"hits": [{"_source": doc} for doc in docs]}}


def _doc(seq):
    return {"record_id": f"r{seq}", "item_id": f"i{seq}", "arrival_seq": seq,
            "text": f"正文{seq}", "scope_id": "default", "task_state": "accepted"}


class _FakeSearchClient:
    def __init__(self, pages):
        self._pages = list(pages)
        self.calls = 0

    def search(self, index, body):
        self.calls += 1
        if self._pages:
            return self._pages.pop(0)
        return _ok_page([])


def _scan(pages, **kw):
    scanner = ElasticsearchRegistrationScanner(_FakeSearchClient(pages),
                                               "p01-batch-x-", page_size=2)
    return scanner.scan("default", "2026-10-07", **kw)


def test_r4c1_non_mapping_response_rejected():
    """result 非 Mapping（裸 list）→ RegistrationScanError（非 AttributeError）。"""
    from news_flash_dedup.recall.worker import RegistrationScanError
    with pytest.raises(RegistrationScanError):
        _scan([["not", "a", "mapping"]])


def test_r4c2_timed_out_true_rejected():
    page = _ok_page([_doc(1)])
    page["timed_out"] = True
    from news_flash_dedup.recall.worker import RegistrationScanError
    with pytest.raises(RegistrationScanError, match="timed_out"):
        _scan([page])


def test_r4c3_timed_out_omitted_rejected():
    """静默默认拆除：timed_out 缺键同拒（bm25:108-109'or omitted status'口径）。"""
    page = _ok_page([_doc(1)])
    del page["timed_out"]
    from news_flash_dedup.recall.worker import RegistrationScanError
    with pytest.raises(RegistrationScanError, match="timed_out"):
        _scan([page])


def test_r4c4_shard_failure_rejected():
    page = _ok_page([_doc(1)])
    page["_shards"]["failed"] = 1
    from news_flash_dedup.recall.worker import RegistrationScanError
    with pytest.raises(RegistrationScanError, match="shard"):
        _scan([page])


def test_r4c5_hits_structure_missing_rejected():
    """静默默认拆除：hits 结构缺场不得当空页提前终止。"""
    from news_flash_dedup.recall.worker import RegistrationScanError
    with pytest.raises(RegistrationScanError, match="hits"):
        _scan([{"timed_out": False, "_shards": {"failed": 0}}])
    with pytest.raises(RegistrationScanError, match="hits"):
        _scan([{"timed_out": False, "_shards": {"failed": 0},
                "hits": {"hits": "not-a-list"}}])


def test_r4c6_hit_source_malformed_rejected():
    page = _ok_page([_doc(1)])
    page["hits"]["hits"] = [{"no_source": True}]
    from news_flash_dedup.recall.worker import RegistrationScanError
    with pytest.raises(RegistrationScanError):
        _scan([page])


def test_r4c7_two_page_scan_green_guard():
    """绿守卫：满页→空页合法翻页语义逐字节不变。"""
    docs = _scan([_ok_page([_doc(1), _doc(2)]), _ok_page([])])
    assert [d["arrival_seq"] for d in docs] == [1, 2]


def test_r4c8_object_api_response_body_unwrap():
    """response_body 解包：带 .body 属性的响应对象按现役公共件口径解包。"""
    class _Apiish:
        def __init__(self, body):
            self.body = body
    docs = _scan([_Apiish(_ok_page([_doc(1)])), _Apiish(_ok_page([]))])
    assert [d["arrival_seq"] for d in docs] == [1]


def test_r4c9_drive_once_scan_failure_contained():
    """轮级收容：scan 抛 RegistrationScanError → drive_once 不炸、当轮零提交、
    DriveReport.scan_error 留痕（游标未推进，下轮自动重扫）。"""
    from news_flash_dedup.recall.worker import DedupWorker, RegistrationScanError

    class _BoomScanner:
        def scan(self, scope_id, business_date, *, after_seq=0):
            raise RegistrationScanError("simulated shard failure")

    class _NullPrepare:
        def prepare(self, scope_id, business_date):
            return None

    worker = DedupWorker(mode="shadow", recall_service=None,
                         prepare_worker=_NullPrepare(),
                         watermark_provider=None, scanner=_BoomScanner(),
                         artifact_sink=type("S", (), {"emit": lambda s, k, p: None})())
    report = worker.drive_once("default", "2026-10-07")
    assert report.committed == () and report.shadowed == ()
    assert report.scan_error is not None and "shard failure" in report.scan_error
