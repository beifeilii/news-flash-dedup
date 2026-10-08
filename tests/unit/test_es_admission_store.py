from __future__ import annotations

from news_flash_dedup.es_admission_store import ElasticsearchAdmissionStore


class FakeClient:
    def __init__(self):
        self.calls = []

    def get(self, **kwargs):
        self.calls.append(("get", kwargs))
        # 09-28 条目 75 语义对齐后补桩（W2Fδ/WA5-M1）：新 get 语义 fail-closed
        # 校验响应身份（_index/_id 须等于请求的物理索引/键）与 found 字段——
        # 旧桩缺三字段系钉住对齐前缺陷语义（新语义下 AdmissionReadError:
        # document response identity differs）。按用例意图补命中形：回显请求
        # 身份 + found=True + _source/_seq_no/_primary_term 完整版本元数据。
        return {
            "_index": kwargs["index"], "_id": kwargs["id"], "found": True,
            "_source": {"value": "v"}, "_seq_no": 7, "_primary_term": 3,
        }

    def index(self, **kwargs):
        self.calls.append(("index", kwargs))


def test_get_is_realtime_and_returns_cas_metadata():
    client = FakeClient()
    store = ElasticsearchAdmissionStore(client)
    assert store.get("idx", "id") == {
        "source": {"value": "v"}, "seq_no": 7, "primary_term": 3
    }
    assert client.calls == [("get", {"index": "idx", "id": "id", "realtime": True})]


def test_create_and_replace_use_es_native_guards():
    client = FakeClient()
    store = ElasticsearchAdmissionStore(client)
    store.create("idx", "id", {"value": "v"})
    store.replace("idx", "id", {"value": "w"}, 7, 3)
    assert client.calls == [
        ("index", {"index": "idx", "id": "id", "document": {"value": "v"},
                   "op_type": "create", "refresh": False}),
        ("index", {"index": "idx", "id": "id", "document": {"value": "w"},
                   "if_seq_no": 7, "if_primary_term": 3, "refresh": False}),
    ]


def test_isolated_prefix_routes_all_physical_indices():
    client = FakeClient()
    store = ElasticsearchAdmissionStore(client, index_prefix="p01-g0-run42-")
    store.get("news-dedup-control-v1", "admission_head")
    store.create("news-dedup-items-v1-2026.09.23", "rid", {"x": 1})
    assert client.calls[0][1]["index"] == "p01-g0-run42-news-dedup-control-v1"
    assert client.calls[1][1]["index"] == "p01-g0-run42-news-dedup-items-v1-2026.09.23"
