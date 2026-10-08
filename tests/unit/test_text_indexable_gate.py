"""P09 空正文入桶闸门红灯：归一后为空/纯空白的正文不得产生 normalized 桶条目。

按用户裁决（2026-09-26 04:30）+ 收尾剩余四项：
- is_indexable(text) 必须 False 当 normalized_text 为空或纯空白
- bucket_key_if_indexable(text, ...) 必须 None
- 直接 bucket_key() 不做 indexable 检查（闸门由调用方经 bucket_key_if_indexable）

红线：
- \"\\r\\n\" 与 \"\\n\" 归一后均为空 → bucket_key_if_indexable 返回 None
- \"\" / \"   \" / \"\\t\\n  \" / \"\\r\\n  \" 均为空/纯空白 → None
- 正常文本 → 返回字符串桶键
"""
from __future__ import annotations

import pytest

from news_flash_dedup.text import NORMALIZER_VERSION
from news_flash_dedup.text.hash import (
    bucket_key, bucket_key_if_indexable, is_indexable, text_hashes_for_text,
)


# ===== 红灯：纯空白类输入均不可入桶 =====


@pytest.mark.parametrize("text", [
    "",
    " ",
    "  ",
    "\t",
    "\n",
    "\r\n",
    "\r\n\r\n",
    " \t\n ",
    "\r\n  \t\n",
    "　",                  # 全角空格
    "　 \t\n　\r\n",
])
def test_is_indexable_false_for_whitespace_or_empty(text):
    """纯空白/空串：is_indexable 必须 False（防 T026 空 Hash 短路）。"""
    assert is_indexable(text) is False, (
        f"text={text!r}: must not be indexable (would create empty Hash bucket)"
    )


@pytest.mark.parametrize("text", [
    "",
    "\r\n",
    "\n",
    "\t",
    "  ",
    "\r\n  ",
    "　\n\r\n　",
])
def test_bucket_key_if_indexable_returns_none_for_whitespace(text):
    """空/纯空白输入：bucket_key_if_indexable 必须返回 None（闸门闭合）。"""
    result = bucket_key_if_indexable(text, "default", "2026-09-27", "normalized")
    assert result is None, (
        f"text={text!r}: bucket_key_if_indexable must return None; got {result!r}"
    )


@pytest.mark.parametrize("text", [
    "甲公司",
    "甲公司\n乙公司",
    "甲公司\r\n乙公司",
    "  甲公司  ",  # 默认 outer_trim_approved=False：保留原���，但 normalized_text 非空
    "甲公司今日实施回购方案。",
])
def test_is_indexable_true_for_normal_text(text):
    """正常文本（即使带空白但有核心内容）：is_indexable True。"""
    assert is_indexable(text) is True


def test_bucket_key_if_indexable_returns_string_for_normal_text():
    """正常文本：bucket_key_if_indexable 返回完整桶键。"""
    key = bucket_key_if_indexable("甲公司", "default", "2026-09-27", "raw")
    assert key is not None
    # 5 段：scope_id/business_date/normalizer_version/hash_kind/hash_value
    parts = key.split("/")
    assert len(parts) == 5
    assert parts[0] == "default"
    assert parts[1] == "2026-09-27"
    assert parts[2] == NORMALIZER_VERSION
    assert parts[3] == "raw"
    assert len(parts[4]) == 64  # sha256 hex


def test_bucket_key_if_indexable_auto_computes_hash_value():
    """bucket_key_if_indexable 不传 hash_value 时自动从 text 计算。

    带变换的文本（CRLF→LF / 外缘空白裁切）：raw 与 normalized 不同。
    无变换文本（"甲公司"）：raw == normalized（V1 仅启用 R1+R2，简单输入不触发）。
    """
    # 带 CRLF 的输入：raw_hash != normalized_hash
    k_raw = bucket_key_if_indexable("甲公司\r\n", "default", "2026-09-27", "raw")
    k_norm = bucket_key_if_indexable("甲公司\r\n", "default", "2026-09-27", "normalized")
    assert k_raw is not None and k_norm is not None
    assert k_raw.split("/")[4] != k_norm.split("/")[4]  # CRLF→LF 改变 normalized_hash
    # 无变换输入：raw == normalized
    k_a = bucket_key_if_indexable("甲公司", "default", "2026-09-27", "raw")
    k_b = bucket_key_if_indexable("甲公司", "default", "2026-09-27", "normalized")
    assert k_a.split("/")[4] == k_b.split("/")[4]  # V1 no-op


def test_bucket_key_if_indexable_accepts_explicit_hash_value():
    """显式传 hash_value 时不走 auto 计算（调用方已算好）。"""
    h = text_hashes_for_text("甲公司")
    k_explicit = bucket_key_if_indexable(
        "甲公司", "default", "2026-09-27", "raw",
        hash_value=h.raw_hash,
    )
    k_auto = bucket_key_if_indexable("甲公司", "default", "2026-09-27", "raw")
    # 不传 hash_value 时 auto 计算应得相同 raw_hash
    assert k_explicit.split("/")[4] == h.raw_hash
    assert k_auto.split("/")[4] == h.raw_hash


def test_bucket_key_by_passes_does_not_check_indexability():
    """⚠️ bucket_key() 本身不做 indexable 检查（闸门由调用方负责）。

    直接调用 bucket_key 仍能构造桶键字符串——这是文档化的红线，
    调用方必须用 bucket_key_if_indexable() 替代。
    """
    raw_hash = "a" * 64
    # 直接 bucket_key 不做 indexable 检查；可构造任意桶键字符串
    key = bucket_key("default", "2026-09-27", "normalized", raw_hash)
    assert key == f"default/2026-09-27/{NORMALIZER_VERSION}/normalized/{raw_hash}"


def test_bucket_key_if_indexable_rejects_unknown_hash_kind():
    """未知 hash_kind 应抛 ValueError（让 bug 显形）。"""
    with pytest.raises(ValueError, match="unknown hash_kind"):
        bucket_key_if_indexable("甲公司", "default", "2026-09-27", "bogus")  # type: ignore[arg-type]


# ===== 闸门与 T026 防短路的链式验证 =====


def test_indexable_false_does_not_create_empty_hash_bucket():
    """T026 核心：空/纯空白输入经 is_indexable 闸门后不得产生任何桶键。

    三个不同空/纯空白输入：所有 bucket_key_if_indexable 都返回 None；
    后续 P10 写桶调用方遇 None 时必须跳过（不构造空桶键）。
    """
    empty_inputs = ["", "\r\n", "\n", "   ", "\t\n  \r\n  ", "　"]
    for text in empty_inputs:
        for kind in ("raw", "normalized"):
            result = bucket_key_if_indexable(text, "default", "2026-09-27", kind)
            assert result is None, (
                f"empty input {text!r} ({kind}) must NOT produce a bucket_key"
            )


def test_indexable_text_with_outer_whitespace_default_not_indexable_but_does_not_collide():
    """默认 outer_trim_approved=False：带前后空白的文本保留（normalized_text == text），可入桶。

    P09 收尾红线：变空串防护是另一回事（09 §4.3）；入桶闸门只看 normalized_text 是否可索引。
    \"  甲公司  \" 可入桶（normalized_text == text == \"  甲公司  \" 非空）。
    """
    text = "  甲公司  "
    assert is_indexable(text) is True
    k = bucket_key_if_indexable(text, "default", "2026-09-27", "normalized")
    assert k is not None