"""P09-B 老 normalize_text + 薄壳 hash 端到端验证（合并后单一入口）。

按用户裁决（2026-09-26 04:30）：
- 单一入口 = 老 normalize_text；单一 normalizer_version = "v1-crlf-outer-trim-1"
- hash 是从 normalize_text 结果派生的薄壳

核心性质已在 test_text_red_lights.py 覆盖（34 项）。本文件聚焦：
- 端到端 normalize + compute_text_hashes 一致性
- 桶键 + 解析往返
- 跨版本桶键区分
"""
from __future__ import annotations

import hashlib

import pytest

from news_flash_dedup.text import NORMALIZER_VERSION, normalize_text
from news_flash_dedup.text.hash import (
    HashKind, TextHashes, bucket_key, compute_text_hashes, parse_bucket_key,
    text_hashes_for_text,
)


def _expected_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_normalize_then_compute_hashes_consistency():
    """normalize_text + compute_text_hashes 端到端：raw_hash 来自 text，normalized_hash 来自 normalized_text。"""
    text = "甲公司今日实施回购方案"
    n = normalize_text(text)
    h = compute_text_hashes(n)
    assert h.raw_hash == _expected_sha256(text)
    assert h.normalized_hash == _expected_sha256(n.normalized_text)
    assert h.normalizer_version == NORMALIZER_VERSION
    assert h.became_empty is False


def test_text_hashes_for_text_convenience():
    """便捷入口等价于 normalize + compute。"""
    text = "甲公司"
    h = text_hashes_for_text(text)
    assert h.raw_hash == _expected_sha256(text)
    assert h.normalized_hash == _expected_sha256(text)  # no-op（无变换）


def test_crlf_to_lf_changes_normalized_hash_but_keeps_raw():
    """CRLF→LF：normalized_hash 改变（去 1 字节），raw_hash 不变（来自原文）。"""
    text_crlf = "甲公司\r\n乙公司"
    text_lf = "甲公司\n乙公司"
    h_crlf = text_hashes_for_text(text_crlf)
    h_lf = text_hashes_for_text(text_lf)
    # normalized_text 相同 → normalized_hash 相同
    assert h_crlf.normalized_hash == h_lf.normalized_hash
    # raw_hash 不同（CRLF 多 1 字节）
    assert h_crlf.raw_hash != h_lf.raw_hash
    assert h_crlf.raw_hash == _expected_sha256(text_crlf)
    assert h_lf.raw_hash == _expected_sha256(text_lf)


def test_outer_trim_changes_hashes_only_when_approved():
    """默认 outer_trim_approved=False：normalized_text == text（无变换）。"""
    text = "  甲公司  "
    h = text_hashes_for_text(text)
    assert h.normalized_hash == _expected_sha256(text)  # 不裁
    # 显式 outer_trim_approved=True 时 normalize_text 内部裁，桶索引计算来自 normalized_text
    n_trimmed = normalize_text(text, outer_trim_approved=True)
    h_trimmed = compute_text_hashes(n_trimmed)
    assert h_trimmed.normalized_hash == _expected_sha256("甲公司")


def test_became_empty_flag_for_whitespace_only_input():
    """仅空白输入：became_empty=True（防 T026 短路到空 Hash 桶）。"""
    h = text_hashes_for_text("   ")
    assert h.became_empty is True
    # 即便 normalized_text.strip() 为空也不参与桶索引（indexable_hashes 排除）


def test_bucket_key_format_matches_section_4_4():
    """桶键 5 段用 '/' 分隔。"""
    key = bucket_key("default", "2026-09-25", "raw",
                      _expected_sha256("甲公司"))
    assert key == f"default/2026-09-25/{NORMALIZER_VERSION}/raw/{_expected_sha256('甲公司')}"


def test_bucket_key_distinguishes_raw_vs_normalized():
    """同一 hash_value 在 raw 与 normalized 桶中分别独立。"""
    raw_key = bucket_key("default", "2026-09-25", "raw", "a" * 64)
    norm_key = bucket_key("default", "2026-09-25", "normalized", "a" * 64)
    assert raw_key != norm_key


def test_parse_bucket_key_round_trip():
    key = bucket_key("default", "2026-09-25", "normalized",
                      _expected_sha256("甲公司"))
    parsed = parse_bucket_key(key)
    assert parsed["scope_id"] == "default"
    assert parsed["business_date"] == "2026-09-25"
    assert parsed["hash_kind"] == "normalized"
    assert parsed["hash_value"] == _expected_sha256("甲公司")
    assert parsed["normalizer_version"] == NORMALIZER_VERSION


def test_parse_bucket_key_rejects_invalid_hash_kind():
    bad_key = f"default/2026-09-25/{NORMALIZER_VERSION}/foobar/" + "a" * 64
    with pytest.raises(ValueError, match="hash_kind must be"):
        parse_bucket_key(bad_key)


def test_parse_bucket_key_rejects_wrong_segment_count():
    with pytest.raises(ValueError, match="must have 5 segments"):
        parse_bucket_key("a/b/c")
    with pytest.raises(ValueError, match="must have 5 segments"):
        parse_bucket_key("a/b/c/d/e/f")


def test_different_normalizer_versions_yield_different_bucket_keys():
    """normalizer_version 钉死：版本号不同 → 桶键不同。

    当前只允许 NORMALIZER_VERSION（v1-crlf-outer-trim-1）；任何其他版本必须拒绝。
    红：传错版本应抛 ValueError。
    """
    raw_hash = _expected_sha256("甲公司")
    # 同版本：相等
    k1 = bucket_key("default", "2026-09-25", "raw", raw_hash, normalizer_version=NORMALIZER_VERSION)
    k1_again = bucket_key("default", "2026-09-25", "raw", raw_hash, normalizer_version=NORMALIZER_VERSION)
    assert k1 == k1_again
    # 钉死：传错版本抛 ValueError
    import pytest
    with pytest.raises(ValueError, match="normalizer_version"):
        bucket_key("default", "2026-09-25", "raw", raw_hash, normalizer_version="v0-fake")


def test_text_hash_is_not_record_id():
    """09 §2.1：text_hash 不是 record_id（record_id 来自 sha256(jcs([scope_id, item_id]))）。"""
    text_hash = _expected_sha256("甲公司")
    record_id = hashlib.sha256(
        __import__("json").dumps(
            ["default", "1001-1"], ensure_ascii=False,
            separators=(",", ":"), sort_keys=True).encode("utf-8")
    ).hexdigest()
    assert text_hash != record_id


def test_T026_no_shortcut_raw_hash_distinguishes_same_normalized():
    """T026 核心：normalized_hash 相同时 raw_hash 必不同（防短路）。"""
    # 默认 outer_trim_approved=False：原文保留 → normalized_text == text
    a = text_hashes_for_text("  \t甲公司\n")
    b = text_hashes_for_text("甲公司")
    # 两 normalized_text 不同（前者有空白）→ normalized_hash 不同
    assert a.normalized_hash != b.normalized_hash
    # raw_hash 必不同
    assert a.raw_hash != b.raw_hash
    assert a.raw_hash == _expected_sha256("  \t甲公司\n")
    assert b.raw_hash == _expected_sha256("甲公司")


def test_became_empty_explicit_via_normalize_text():
    """normalize_text 默认 outer_trim_approved=False 不裁；显式 True 才允许裁���

    但 09 §4.3 变空串防护：原文仅空白时即使 approved=True 也保留原文。
    """
    n = normalize_text("   ", outer_trim_approved=True)
    # 09 §4.3 变空串防护：保留原文 "   "，normalized_text == text
    assert n.normalized_text == "   "
    # indexable_hashes 排除空文/纯空白
    assert n.indexable_hashes() == ()
    h = compute_text_hashes(n)
    assert h.became_empty is True  # 防 T026 短路标记