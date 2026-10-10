"""P09-B 真实 UAT ES T024 验证：归一桶命中 + 原文逐码点核验。

按 12 §5 T024 与 09 §4.4：CRLF/LF/外缘空白无损归一；归一 Hash 相等 + 原文逐码点一致才形成 EXACT_TEXT_MATCH 证书。
Hash 相等永不短路判重（必须回取原文）。

合并后单一入口 = 老 normalize_text（v1-crlf-outer-trim-1）；本测试调用老 API。

跑法：`P08_CONFIRM_UAT=1 PYTHONPATH=src pytest tests/integration/test_text_normalize_uat.py -v`
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

# TFIX-p3c（2026-10-11 测试债修复）：原两行 ROOT=硬编码外树
# （workspace-dedup/news-flash-dedup 主干 2d0d418）+ sys.path.insert(0,
# ROOT+"/src")——模块导入副作用把**另一棵产品树**的旧包前插 sys.path；
# 全套件收集序（integration 先于 unit）下，news_flash_dedup.runtime_budget
# 首导窗口命中旧树并被 sys.modules 永久钉死（无 counter_snapshot/
# bump_counter），后续 tests/unit/test_p3c_decision_mode::test_budget_
# counter_hook 顺序污染红（单跑绿、套件红——探针实测 runtime_budget.
# __file__ 指向旧树）。死重清理照 test_cleanup_uat.py W3e F1 先例：导入
# 由 PYTHONPATH=src 跑法保证（本文件 docstring 跑法本就如此）；ROOT 改
# Path(__file__) 本树回锚 + 导入期钉防回归（失锚即收集红）。
ROOT = Path(__file__).resolve().parents[2]
assert (ROOT / "src" / "news_flash_dedup").is_dir(), (
    f"ROOT 派生失锚：{ROOT} 下无 src/news_flash_dedup（parents 索引漂移）")


def _uat_env_ready() -> bool:
    return os.environ.get("P08_CONFIRM_UAT") == "1"


pytestmark = pytest.mark.skipif(
    not _uat_env_ready(),
    reason="P09-B T024 真实 UAT ES 测试需要 P08_CONFIRM_UAT=1 显式授权；默认跳过",
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
        pytest.fail(f"UAT env check failed: {error}")
    client = client_from_environment(load_dotenv_first=False)
    info = client.info()
    assert str(info["version"]["number"]).startswith("8.")
    yield client
    client.close()


@pytest.fixture(scope="module")
def ns(uat_client):
    """独立 p09-t024-* prefix 隔离。

    用 item_mapping 但把 dynamic 设为 true（默认），允许写入测试字段
    bucket_key/raw_hash/t024_marker，便于按 term 查询真实命中。
    """
    import re
    raw = f"p09-t024-{datetime.now(timezone.utc).strftime('%H%M%S%f')}-"
    if not re.fullmatch(r"p01-batch-[A-Za-z0-9-]+-", raw):
        prefix = f"p01-batch-{raw}"
    else:
        prefix = raw
    from news_flash_dedup.es_admission_schema import item_mapping
    mapping = item_mapping()
    mapping["mappings"]["dynamic"] = True
    # bucket_key / raw_hash 必须 keyword（term 查询用）
    mapping["mappings"]["properties"]["bucket_key"] = {"type": "keyword"}
    mapping["mappings"]["properties"]["raw_hash"] = {"type": "keyword"}
    bucket_index = f"{prefix}news-dedup-items-v1-2026.09.27"
    uat_client.indices.create(index=bucket_index, body=mapping)
    yield {"prefix": prefix, "bucket_index": bucket_index}
    # 清理 prefix 下所有
    listed = uat_client.indices.get(index=f"{prefix}*", allow_no_indices=True,
                                    ignore_unavailable=True)
    for name in listed.keys():
        try:
            uat_client.indices.delete(index=name)
        except Exception:
            pass


def _search_by_term(uat_client, index, field, value):
    """term 查询按字段精确命中；用于真实桶检索而非按 ID 回取。"""
    body = {"query": {"term": {field: value}}}
    return uat_client.search(index=index, body=body, size=20)


def test_crlf_normalized_hash_matches_lf_form_on_uat(uat_client, ns):
    """T024 子项 1：CRLF 与 LF 的归一 Hash 在 UAT ES 桶中相同。

    同一原文，CRLF 与 LF 写法 → 同一 normalized_text → 同一 normalized_hash。
    raw_hash 不同（CRLF 比 LF 多一个 CR 字节）。

    term 查询：按 normalized 桶键命中两 marker（不在桶里的 hash 不命中）。
    """
    from news_flash_dedup.text import normalize_text
    from news_flash_dedup.text.hash import bucket_key, text_hashes_for_text
    text_lf = "甲公司\n乙公司"
    text_crlf = "甲公司\r\n乙公司"
    n_lf = text_hashes_for_text(text_lf)
    n_crlf = text_hashes_for_text(text_crlf)
    # 两者 normalized_text 相同 → normalized_hash 相同
    assert normalize_text(text_lf).normalized_text == normalize_text(text_crlf).normalized_text
    assert n_lf.normalized_hash == n_crlf.normalized_hash
    # raw_hash 不同
    assert n_lf.raw_hash != n_crlf.raw_hash
    # 桶键相同
    k_norm = bucket_key("default", "2026-09-27", "normalized", n_lf.normalized_hash)
    k_lf_raw = bucket_key("default", "2026-09-27", "raw", n_lf.raw_hash)
    k_crlf_raw = bucket_key("default", "2026-09-27", "raw", n_crlf.raw_hash)
    # 写入 4 个 marker：normalized 桶 1 个（LF/CRLF 同一桶）+ raw 桶 2 个（LF/CRLF 各自）
    # refresh=true 立即可见（term 查询不需等默认 1s 刷新）
    uat_client.index(index=ns["bucket_index"], id="lf-norm", refresh=True,
                      document={"bucket_key": k_norm, "kind": "lf-norm"})
    uat_client.index(index=ns["bucket_index"], id="crlf-norm", refresh=True,
                      document={"bucket_key": k_norm, "kind": "crlf-norm"})
    uat_client.index(index=ns["bucket_index"], id="lf-raw", refresh=True,
                      document={"bucket_key": k_lf_raw, "kind": "lf-raw"})
    uat_client.index(index=ns["bucket_index"], id="crlf-raw", refresh=True,
                      document={"bucket_key": k_crlf_raw, "kind": "crlf-raw"})
    # term 查询：normalized 桶命中 2 条（LF + CRLF）
    norm_hits = _search_by_term(uat_client, ns["bucket_index"], "bucket_key", k_norm)
    assert norm_hits["hits"]["total"]["value"] == 2
    norm_hit_ids = sorted(h["_id"] for h in norm_hits["hits"]["hits"])
    assert norm_hit_ids == ["crlf-norm", "lf-norm"]
    # raw 桶：LF 1 条
    lf_raw_hits = _search_by_term(uat_client, ns["bucket_index"], "bucket_key", k_lf_raw)
    assert lf_raw_hits["hits"]["total"]["value"] == 1
    assert lf_raw_hits["hits"]["hits"][0]["_id"] == "lf-raw"
    # raw 桶：CRLF 1 条
    crlf_raw_hits = _search_by_term(uat_client, ns["bucket_index"], "bucket_key", k_crlf_raw)
    assert crlf_raw_hits["hits"]["total"]["value"] == 1
    assert crlf_raw_hits["hits"]["hits"][0]["_id"] == "crlf-raw"
    # 关键不变量：normalized 桶同时命中 LF 与 CRLF（Hash 相等），但 raw 桶分桶
    assert lf_raw_hits["hits"]["hits"][0]["_id"] != crlf_raw_hits["hits"]["hits"][0]["_id"]


def test_outer_trim_approved_only_with_explicit_approval(uat_client, ns):
    """T024 子项 2：默认 outer_trim_approved=False 不裁；显式 True 才裁。

    防 09 §4.2 启用条件："普通快讯且不位于代码/表格/引用/对齐数据"——
    必须由调用方显式 outer_trim_approved=True 才能裁。
    """
    from news_flash_dedup.text import normalize_text
    text = "  \t甲公司\n"
    n_default = normalize_text(text)  # 默认 False
    n_explicit = normalize_text(text, outer_trim_approved=True)
    # 默认：normalized_text == text（不裁）
    assert n_default.normalized_text == text
    # 显式：normalized_text = "甲公司"
    assert n_explicit.normalized_text == "甲公司"
    # original_span(0, n) 不同
    assert n_default.original_span(0, len(n_default.normalized_text)) == (0, len(text))
    assert n_explicit.original_span(0, len(n_explicit.normalized_text)) == (3, 6)  # 跳过 "  \t"


def test_whitespace_only_texts_have_distinct_raw_hashes_and_no_bucket_records(uat_client, ns):
    """纯空白正文保留各自原文 Hash，但两个 Hash 通道均不得写桶。"""
    from news_flash_dedup.text.hash import bucket_key_if_indexable, text_hashes_for_text
    a = text_hashes_for_text("   ")
    b = text_hashes_for_text("\t\n  ")
    assert a.became_empty is True
    assert b.became_empty is True
    # raw_hash 必不同（两文本不同）
    assert a.raw_hash != b.raw_hash
    assert a.normalized_hash != b.normalized_hash
    for text, raw_hash in (("   ", a.raw_hash), ("\t\n  ", b.raw_hash)):
        for kind in ("raw", "normalized"):
            assert bucket_key_if_indexable(
                text, "default", "2026-09-27", kind
            ) is None
        assert _search_by_term(
            uat_client, ns["bucket_index"], "raw_hash", raw_hash
        )["hits"]["total"]["value"] == 0


def test_bucket_key_if_indexable_returns_none_for_whitespace_via_real_es():
    """空正文 UAT 改道：用 bucket_key_if_indexable() + 断言空白 None + ES 查无桶条目。

    按用户裁决（2026-09-26）：当前 UAT T026 用 bucket_key() 写 ES（不检查 indexable）—
    改为断言 bucket_key_if_indexable(空白) is None 且经闸门写入路径在 ES 中查无文档。
    """
    from news_flash_dedup.text.hash import bucket_key_if_indexable
    # 各种空白输入：所有都应返回 None（闸门闭合）
    empty_inputs = ["", " ", "\t", "\n", "\r\n", "   ", "\r\n\r\n", "　"]
    for text in empty_inputs:
        result = bucket_key_if_indexable(
            text, "default", "2026-09-26", "normalized",
            outer_trim_approved=False,
        )
        assert result is None, (
            f"text={text!r}: bucket_key_if_indexable must return None"
        )
        # raw 桶也应 None
        result_raw = bucket_key_if_indexable(
            text, "default", "2026-09-26", "raw",
        )
        assert result_raw is None, (
            f"text={text!r}: raw bucket must also be None"
        )


def test_empty_text_via_gate_writes_no_documents_to_es(uat_client, ns):
    """空正文 UAT 改道（晚于代码改动时间）：用 bucket_key_if_indexable() 走闸门路径。

    模拟 P10 写桶调用：先调 bucket_key_if_indexable（=None）→ 不写桶 → ES 中无文档。
    对比非空文本：用 bucket_key_if_indexable（!=None）→ 走闸门写入 → ES 中查得。
    """
    from news_flash_dedup.text.hash import bucket_key_if_indexable
    # 空文本：闸门闭合（返回 None），跳过写入
    empty_texts = ["", " ", "\r\n", "   "]
    for text in empty_texts:
        gate_key = bucket_key_if_indexable(
            text, "default", "2026-09-26", "normalized",
        )
        assert gate_key is None
        # 不写桶（按 P10 写桶闸门调用模式）

    # 非空文本：闸门开启（!=None），写入后 ES 应能查到
    non_empty = "甲公司今日实施回购方案"
    gate_key = bucket_key_if_indexable(
        non_empty, "default", "2026-09-26", "normalized",
    )
    assert gate_key is not None
    # 写入桶键文档
    uat_client.index(
        index=ns["bucket_index"], id="non-empty-marker", refresh=True,
        document={"bucket_key": gate_key, "text_preview": non_empty[:20]},
    )
    # 经闸门写入路径在 ES 中查得（按 term 查询真实命中）
    hits = _search_by_term(uat_client, ns["bucket_index"], "bucket_key", gate_key)
    assert hits["hits"]["total"]["value"] == 1
    assert hits["hits"]["hits"][0]["_id"] == "non-empty-marker"


def test_uat_bucket_key_lookup_by_normalized_hash(uat_client, ns):
    """T024 真实 UAT 桶命中：用 normalized_hash 查 bucket_key。"""
    from news_flash_dedup.text import normalize_text
    from news_flash_dedup.text.hash import bucket_key, text_hashes_for_text
    # 三个不同文本：裸 + 外缘空白 + CRLF+外缘空白
    text_a = "甲公司今日实施回购方案"
    text_b = "  甲公司今日实施回购方案  "  # 外缘空白
    text_c = "\r\n甲公司今日实施回购方案\r\n"  # CRLF + 外缘空白
    # 默认 outer_trim_approved=False：normalized_text == text（保留）
    h_a = text_hashes_for_text(text_a)
    h_b = text_hashes_for_text(text_b)
    h_c = text_hashes_for_text(text_c)
    # 三个 normalized_text 不同（外缘空白保留 + CRLF→LF）→ 三个 normalized_hash 不同
    assert normalize_text(text_a).normalized_text == "甲公司今日实施回购方案"
    assert normalize_text(text_b).normalized_text == "  甲公司今日实施回购方案  "
    # text_c 的 CRLF 在 R1 默认启用下会被替换为 LF
    assert normalize_text(text_c).normalized_text == "\n甲公司今日实施回购方案\n"
    # 但显式 outer_trim_approved=True 时：normalized_text 相同
    n_a = normalize_text(text_a)
    n_b = normalize_text(text_b, outer_trim_approved=True)
    n_c = normalize_text(text_c, outer_trim_approved=True)
    assert n_a.normalized_text == n_b.normalized_text == n_c.normalized_text == "甲公司今日实施回购方案"
    # 显式 trim 时 normalized_hash 相同
    assert n_a.normalized_hash == n_b.normalized_hash == n_c.normalized_hash
    # raw_hash 不同（防短路）
    assert h_a.raw_hash != h_b.raw_hash
    assert h_b.raw_hash != h_c.raw_hash
    # 桶键：三个 raw_hash 独立 raw 桶 + 一个 normalized 桶
    k_a = bucket_key("default", "2026-09-27", "normalized", n_a.normalized_hash)
    k_b = bucket_key("default", "2026-09-27", "normalized", n_b.normalized_hash)
    k_c = bucket_key("default", "2026-09-27", "normalized", n_c.normalized_hash)
    assert k_a == k_b == k_c
    # 写入 3 个 marker（按 raw_hash 区分）
    for i, (t, h) in enumerate([(text_a, h_a), (text_b, h_b), (text_c, h_c)]):
        uat_client.index(
            index=ns["bucket_index"], id=f"bucket-marker-{i}",
            document={"raw_hash": h.raw_hash, "kind": t[:8] + "...",
                      "ts": datetime.now(timezone.utc).isoformat()})
    founds = [uat_client.get(index=ns["bucket_index"], id=f"bucket-marker-{i}",
                              realtime=True)
              for i in range(3)]
    raw_hashes_returned = {f["_source"]["raw_hash"] for f in founds}
    # 3 个不同 raw_hash（无短路）
    assert len(raw_hashes_returned) == 3
