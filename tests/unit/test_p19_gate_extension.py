"""B5/E2 S3 红测六钉（设计 §4.5 闸扩展）：recall/vector_store.py 二态闭集词表。

红测清单映射（log\设计-真Embedding接线包.md v4 R164 §4.5「红测钉面」）：
- ① p19 集合名接受 + endswith(space_id) 执法（p19 名+非该 space 尾拒收）；
- ② news_dedup_replay_ 名回归接受（旧轨不破）；
- ③ 异族错配拒收（p19 名+非该 space 尾 / replay 名+p19 尾但非该 space 尾）；
- ④ 保留集合名（deerflow_rag_knowledge_base/rag_knowledge_base）/第三形态前缀拒收
  （fail-closed 保持：同类型 ValueError 同风格，词表为闭集扩展非通配放开）；
- ⑤ ES 前缀二态（接纳 p01-batch- 与 p19-batch-<run>-，拒第三形态）；
- ⑥ 镜像对拍钉：同一组名串在 vector/milvus_store.py 的 validate_p19_* 与
  recall 侧闸的接受/拒收逐条一致（防循环 import 的镜像常量等价性执法——
  recall/vector_store.py 模块级镜像常量与 vector/milvus_store.py:46-49 同源，
  反向 import 结构性禁止，milvus_store.py:34-41 已 import 本模块）。

既有面零回归附带钉：闸扩展不改 MilvusVectorStore 检索本体（mock 轨既有
用例 tests/unit/test_vector_store.py 全绿为证，本文件不重复其面）。
"""

from __future__ import annotations

import pytest

from news_flash_dedup.recall.vector_space import EmbeddingSpace
from news_flash_dedup.recall.vector_store import MilvusVectorStore
from news_flash_dedup.vector import milvus_store as p19_ms


def _space(tag="fixture_v1"):
    return EmbeddingSpace("mock_embedding", tag, "codepoint_v1", "none",
                          "COSINE", 4, "plain_v1", "plain_v1",
                          "tokens512_overlap64_v1", "p09_v1")


def _construct(collection, prefix, space):
    """仅构造（fake 双倍体不触网）；闸扩展只动 __init__ 两闸。"""
    return MilvusVectorStore(None, None, collection, prefix, space)


# ---------- ① p19 名接受 + endswith(space_id) 执法 ----------

def test_pin1_p19_collection_accepted_with_matching_space_tail():
    space = _space()
    name = p19_ms.build_p19_collection_name("b5uat123", space.space_id)
    store = _construct(name, "p01-batch-b5gate-", space)
    assert store.collection_name == name
    assert name.endswith(space.space_id)


def test_pin1_p19_collection_rejected_without_space_tail():
    space = _space()
    other = _space("other_v1")
    name = p19_ms.build_p19_collection_name("b5uat123", other.space_id)
    with pytest.raises(ValueError, match="(?i)isolated replay"):
        _construct(name, "p01-batch-b5gate-", space)


# ---------- ② news_dedup_replay_ 名回归接受（旧轨不破） ----------

def test_pin2_replay_collection_regression_accepted():
    space = _space()
    name = "news_dedup_replay_abc123_" + space.space_id
    store = _construct(name, "p01-batch-p12-unit-", space)
    assert store.collection_name == name


# ---------- ③ 异族错配拒收 ----------

def test_pin3_replay_name_rejected_without_space_tail():
    space = _space()
    other = _space("other_v1")
    with pytest.raises(ValueError, match="(?i)isolated replay"):
        _construct("news_dedup_replay_abc123_" + other.space_id,
                   "p01-batch-b5gate-", space)


def test_pin3_p19_name_with_replay_style_or_short_run_rejected():
    space = _space()
    # run 段过短（<6）/ 含大写 / 含连字符：非 p19 形态一律拒
    for bad in (f"p19_abc_{space.space_id}",
                f"p19_ABC123_{space.space_id}",
                f"p19-abc123_{space.space_id}"):
        with pytest.raises(ValueError, match="(?i)isolated replay"):
            _construct(bad, "p01-batch-b5gate-", space)


# ---------- ④ 保留集合名 / 第三形态前缀拒收（fail-closed 保持） ----------

def test_pin4_preserved_and_third_form_collections_rejected():
    space = _space()
    for bad in ("deerflow_rag_knowledge_base",
                "rag_knowledge_base",
                f"p20_b5uat123_{space.space_id}",
                f"p19_b5uat123_{space.space_id}_extra",
                f"news_dedup_replay_b5_{space.space_id}".replace("_b5_", "_b5x_")[:-42]
                + space.space_id):
        with pytest.raises(ValueError, match="(?i)isolated replay"):
            _construct(bad, "p01-batch-b5gate-", space)


# ---------- ⑤ ES 前缀二态 ----------

def test_pin5_es_prefix_two_state_vocabulary():
    space = _space()
    name = p19_ms.build_p19_collection_name("b5uat123", space.space_id)
    # 接纳：现役 p01-batch- 与 P19 形态 p19-batch-<run>-
    assert _construct(name, "p01-batch-b5gate-", space).es_index_prefix == "p01-batch-b5gate-"
    assert (_construct(name, "p19-batch-b5uat123-", space).es_index_prefix
            == "p19-batch-b5uat123-")
    # 拒：第三形态（含 p19-batch 缺尾横杠 / run 段非法 / 跨代前缀）
    for bad in ("p02-batch-b5gate-", "p19-batch-b5uat123",
                "p19-batch-B5UAT123-", "p19-batch-abc-", "p19-foo-",
                "prod-p19-batch-b5uat123-"):
        with pytest.raises(ValueError, match="(?i)ES index prefix must be isolated"):
            _construct(name, bad, space)


# ---------- ⑥ 镜像对拍钉（validate_p19_* ⇔ recall 侧闸逐条一致） ----------

_COLLECTION_BATTERY = [
    "p19_b5uat123_{sid}",            # 合法 p19 形
    "p19_abcdef0123456789_{sid}",    # run 16 字符上沿
    "p19_abcde_{sid}",               # run 过短
    "p19_abcdefghijklmnopq_{sid}",   # run 过长（17）
    "p19_B5UAT123_{sid}",            # 大写
    "news_dedup_replay_b5uat123_{sid}",  # replay 形（recall 收 / p19 验证器拒）
    "deerflow_rag_knowledge_base",
    "rag_knowledge_base",
    "p20_b5uat123_{sid}",
]

_RUN_TOKEN_BATTERY = ["b5uat123", "abcdef0123456789", "abcde",
                      "abcdefghijklmnopq", "B5UAT123", "abc-def"]


def test_pin6_mirror_parity_collection_names():
    space = _space()
    for template in _COLLECTION_BATTERY:
        name = template.format(sid=space.space_id)
        try:
            p19_ms.validate_p19_collection_name(name)
            p19_side = True
        except p19_ms.P19CollectionNameInvalid:
            p19_side = False
        try:
            _construct(name, "p01-batch-b5gate-", space)
            recall_side = True
        except ValueError:
            recall_side = False
        if template.startswith("news_dedup_replay_"):
            # replay 形属 recall 闸另一态（p19 验证器拒、recall 收）——非镜像面
            assert p19_side is False and recall_side is True
            continue
        # 镜像面 = p19 形态：两侧接受/拒收逐条一致（闭集外第三形态双拒）
        assert p19_side is recall_side, (
            f"镜像失配：{name!r} p19 验证器={p19_side} recall 闸={recall_side}")


def test_pin6_mirror_parity_es_run_tokens():
    space = _space()
    name = p19_ms.build_p19_collection_name("b5uat123", space.space_id)
    for token in _RUN_TOKEN_BATTERY:
        prefix = f"p19-batch-{token}-"
        full_index = f"{prefix}news-dedup-items-v1-2026.09.29"
        try:
            p19_ms.validate_p19_index_name(full_index)
            p19_side = True
        except p19_ms.P19IndexNameInvalid:
            p19_side = False
        try:
            _construct(name, prefix, space)
            recall_side = True
        except ValueError:
            recall_side = False
        assert p19_side is recall_side, (
            f"镜像失配：run={token!r} p19 索引验证器={p19_side} recall 前缀闸={recall_side}")
