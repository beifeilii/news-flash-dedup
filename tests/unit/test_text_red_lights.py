"""P09-B position_map 真红灯 + 全量覆盖不变量 + 外缘门控。

按用户裁决（2026-09-26 04:30）：
- "A\\r\\nB" 必须 3 个码点 3 个映射（每个 output 码点一 entry）；
- 全量覆盖不变量：len(position_map) == len(normalized_text)；
- 外缘裁剪必须显式 outer_trim_approved=True 才裁，默认 False 不裁；
- 修正 test_position_map_round_trip_for_crlf（输入必须真含 CRLF）；
- 参数化覆盖混合 CRLF/外缘空白/中文。

Span 语义（按 09 §4.1 与 老实现 `original_span`）：
- position_map[i] 是 output 码点 i 对应的 (source_start, source_end)
- CRLF 对应 1 个 output 但 entry 的 source_end - source_start = 2（覆盖 CR+LF 2 字节）
- 任何 entry 的 (source_start, source_end) 与其代表的 output 码点之间一一对应
"""

from __future__ import annotations

import pytest

from news_flash_dedup.text import (
    NORMALIZER_VERSION,
    certify_text_equality,
    normalize_text,
)


# ===== 1. 真红灯："A\r\nB" 必须 3 个码点 3 个映射 =====


def test_text_a_crlf_b_yields_three_codepoints_three_spans():
    """A\\r\\nB：3 个 output 码点；3 个 position_map entry；每个 entry 对应 1 个 output。

    关键不变量：len(position_map) == len(normalized_text)。
    """
    text = "A\r\nB"
    result = normalize_text(text)
    assert result.normalized_text == "A\nB"
    assert len(result.position_map) == 3
    # entry 0（A）: source [0, 1)
    assert result.position_map[0] == (0, 1)
    # entry 1（LF）: source [1, 3) — CRLF 占 2 个 source 字节
    assert result.position_map[1] == (1, 3)
    # entry 2（B）: source [3, 4)
    assert result.position_map[2] == (3, 4)


def test_text_full_width_crlf_yields_three_spans():
    text = "甲\r\n乙"
    result = normalize_text(text)
    assert result.normalized_text == "甲\n乙"
    assert len(result.position_map) == 3
    assert result.position_map[0] == (0, 1)
    assert result.position_map[1] == (1, 3)
    assert result.position_map[2] == (3, 4)


# ===== 2. 全量覆盖不变量：len(position_map) == len(normalized_text) =====


@pytest.mark.parametrize("text,approved", [
    ("A", False),
    ("ABC", False),
    ("A\r\nB", False),
    ("A\r\nB\r\nC", False),
    ("A\r\nB\r\nC", True),
    ("  \r\n  ", True),
    ("甲\r\n乙\r\n丙", False),
    ("甲\r\n乙\r\n丙", True),
    ("\nA\nB\n", False),
    ("\nA\nB\n", True),
    (" ", False),
    ("   \t\n   ", False),
    ("   \t\n   ", True),
])
def test_position_map_length_equals_normalized_length(text, approved):
    """任意输入：len(position_map) == len(normalized_text)。

    关键不变量：每 output 码点一个 entry。
    """
    result = normalize_text(text, outer_trim_approved=approved)
    assert len(result.position_map) == len(result.normalized_text), (
        f"text={text!r}: position_map len {len(result.position_map)} != "
        f"normalized_text len {len(result.normalized_text)}"
    )


def test_original_span_equals_full_text_for_no_trim():
    """无外缘裁剪：original_span(0, n) = (0, len(text))（全文 1:1 覆盖）。"""
    text = "A\r\nB\r\nC"
    result = normalize_text(text)  # 默认 outer_trim_approved=False
    start, end = result.original_span(0, len(result.normalized_text))
    assert start == 0
    assert end == len(text)


def test_original_span_with_outer_trim_starts_at_first_non_whitespace():
    """外缘裁��后 original_span(0, n) 起点为第一个非空白 source 位置。"""
    text = "  \t甲公司\n"
    result = normalize_text(text, outer_trim_approved=True)
    start, end = result.original_span(0, len(result.normalized_text))
    assert start == 3  # 跳过 "  \t" 三个空白
    assert end == 6    # "甲公司" 结束


# ===== 3. 外缘裁剪门控：默认 False 不裁 =====


def test_default_outer_trim_does_not_strip_leading_whitespace():
    """默认 outer_trim_approved=False：前导空白保留。"""
    text = "  \t甲公司\n"
    result = normalize_text(text)  # 默认 approved=False
    assert result.normalized_text == text  # 完全保留
    assert result.outer_trim_approved is False


def test_explicit_outer_trim_strips_whitespace():
    """显式 outer_trim_approved=True：前导后随空白被裁。"""
    text = "  \t甲公司\n"
    result = normalize_text(text, outer_trim_approved=True)
    assert result.normalized_text == "甲公司"
    assert result.outer_trim_approved is True


def test_default_outer_trim_does_not_strip_full_width_space():
    """默认 outer_trim_approved=False：全角空格保留。"""
    text = "　甲公司　"
    result = normalize_text(text)
    assert result.normalized_text == text  # 完全保留


def test_outer_trim_keeps_outer_when_text_is_only_whitespace():
    """变空串防护：原文仅空白时即使 outer_trim_approved=True 也保留（不裁）。

    09 §4.3：空文/纯空白不能获得共同空 Hash 判重。
    """
    result = normalize_text("   \t\n   ", outer_trim_approved=True)
    assert result.normalized_text == "   \t\n   "
    assert result.normalized_text != ""  # 关键不变量


def test_outer_trim_required_parameter_rejects_non_bool():
    """outer_trim_approved 必须为 bool；非 bool 抛 TypeError。"""
    with pytest.raises(TypeError, match="outer_trim_approved must be bool"):
        normalize_text("甲公司", outer_trim_approved="yes")  # type: ignore[arg-type]


def test_normalize_text_rejects_non_str():
    """text 必须为 str；非 str 抛 TypeError。"""
    with pytest.raises(TypeError, match="text must be str"):
        normalize_text(123)  # type: ignore[arg-type]


# ===== 4. cert/Hash 相等证书与防短路 =====


def test_certify_text_equality_returns_none_without_qualification():
    """cert 必须双方都 qualified=True 才返回证书；否则 None（不判重）。"""
    left = normalize_text("甲公司")
    right = normalize_text("甲公司")
    assert certify_text_equality(left, right) is None
    assert certify_text_equality(left, right, left_qualified=True) is None
    assert certify_text_equality(left, right, right_qualified=True) is None


def test_certify_text_equality_exact_text_match():
    """双方 qualified + 完全相同文本 → EXACT_TEXT_MATCH 证书。"""
    left = normalize_text("甲公司")
    right = normalize_text("甲公司")
    cert = certify_text_equality(left, right, left_qualified=True, right_qualified=True)
    assert cert is not None
    assert cert.kind.value == "EXACT_TEXT_MATCH"
    assert cert.normalized_hash == left.normalized_hash


def test_certify_text_equality_lossless_for_crlf_outer_whitespace():
    """CRLF 与外缘空白差异 → LOSSLESS_TEXT_MATCH（无损归一证书）。"""
    left = normalize_text("甲公司\n", outer_trim_approved=True)
    right = normalize_text("甲公司")
    cert = certify_text_equality(left, right, left_qualified=True, right_qualified=True)
    assert cert is not None
    assert cert.kind.value == "LOSSLESS_TEXT_MATCH"


def test_certify_text_equality_none_for_different_text():
    """不同文本 → 证书 None（不判重；Hash 不短路）。"""
    left = normalize_text("甲公司")
    right = normalize_text("乙公司")
    cert = certify_text_equality(left, right, left_qualified=True, right_qualified=True)
    assert cert is None


def test_certify_text_equality_rejects_different_normalizer_version():
    """不同 normalizer_version → None（不混版本证书）。"""
    left = normalize_text("甲公司")
    from dataclasses import replace
    right = replace(left, normalizer_version="v0-fake")
    cert = certify_text_equality(left, right, left_qualified=True, right_qualified=True)
    assert cert is None


def test_certify_text_equality_rejects_corrupt_persisted_hash():
    """反伪造：持久化 hash 被改 → cert 必须 None（cert 重算原文 hash 校验）。"""
    left = normalize_text("甲公司")
    right = normalize_text("甲公司")
    # 篡改 right 的 raw_hash
    from dataclasses import replace
    right_corrupt = replace(right, raw_hash="0" * 64)
    cert = certify_text_equality(left, right_corrupt, left_qualified=True, right_qualified=True)
    assert cert is None


# ===== 5. 修正原 test_position_map_round_trip_for_crlf（真含 CRLF）=====


def test_position_map_round_trip_for_crlf_real_crlf_input():
    """原 test_position_map_round_trip_for_crlf 修正：输入必须真含 CRLF。"""
    text = "甲公司\r\n乙公司"  # 真 CRLF
    result = normalize_text(text)
    assert result.normalized_text == "甲公司\n乙公司"
    assert len(result.position_map) == len(result.normalized_text)  # 全量覆盖
    # entry 数 == output 码点数
    assert len(result.position_map) == 7  # 3 + 1(\n) + 3


# ===== 6. 防短路：Hash 相等不构成证书 =====


def test_two_different_texts_with_same_normalized_have_different_raw_hashes():
    """防 T026 短路：normalized_hash 相等时 raw_hash 必不同。"""
    # 默认 outer_trim_approved=False：原文保留
    left = normalize_text("  甲公司  ")  # 不裁，normalized == text
    right = normalize_text("甲公司")
    # 两 normalized_text 不同（前者有空白）
    assert left.normalized_text != right.normalized_text
    # raw_hash 也不同
    assert left.raw_hash != right.raw_hash


def test_indexable_hashes_excludes_whitespace_only_input():
    """indexable_hashes 对仅空白输入返回空（防 T026 短路到空 Hash 桶）。"""
    # 默认 outer_trim_approved=False：空白保留，但 indexable 仍排除
    result = normalize_text("   ")
    assert result.indexable_hashes() == ()


def test_indexable_hashes_returns_two_kinds_for_normal_input():
    """正常输入：indexable_hashes 返回 ("raw", raw) + ("normalized", norm)。"""
    result = normalize_text("甲公司")
    pairs = result.indexable_hashes()
    assert ("raw", result.raw_hash) in pairs
    assert ("normalized", result.normalized_hash) in pairs


# ===== 7. normalizer_version 固定 =====


def test_normalizer_version_is_fixed():
    """normalizer_version 固定为老实现的 'v1-crlf-outer-trim-1'。"""
    result = normalize_text("甲公司")
    assert result.normalizer_version == "v1-crlf-outer-trim-1"
    assert NORMALIZER_VERSION == "v1-crlf-outer-trim-1"