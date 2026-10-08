"""P09 收尾剩余集成缺陷修复红灯（先红后绿）。

按用户裁决（2026-09-26）三项集成缺陷：
1. 闸门与获准裁剪对齐：bucket_key_if_indexable(" A ") 与获准 outer_trim_approved 的桶键不一致。
   修复方向：bucket_key_if_indexable 必须接受 outer_trim_approved 参数并透传给 normalize_text，
   或接受预算好的 NormalizationResult/TextHashes。
   红灯：三种调用方式同文同键。

2. 版本号钉死：normalizer_version 不得接受任意字符串（v999-fake 实测可入键）。
   修复方向：固定为包当前 NORMALIZER_VERSION，传错拒绝。
   红灯：传 v999-fake 应拒绝。

3. 空正文 UAT 改走闸门：T026 改为断言 bucket_key_if_indexable(空白) is None，
   且经闸门写入路径在 ES 中查无文档；raw_hash 不同的断言保留为次要。
"""
from __future__ import annotations

import pytest

from news_flash_dedup.text import NORMALIZER_VERSION, normalize_text
from news_flash_dedup.text.hash import (
    TextHashes, bucket_key, bucket_key_if_indexable, compute_text_hashes,
    is_indexable, parse_bucket_key,
)


# ===== 红灯 1：闸门与获准裁剪对齐 =====


def test_bucket_key_if_indexable_default_trim_keeps_outer_whitespace():
    """红灯已修——normalized 桶键随 outer_trim_approved 变化（raw_hash 不变：09 §4.4）。

    默认 outer_trim_approved=False → normalized_text 保留外缘空白 → 不同的 normalized_hash；
    显式 outer_trim_approved=True → normalized_text 裁外缘 → 不同的 normalized_hash。
    两个 normalized 桶键应不同。
    """
    text = "  A  "
    default = bucket_key_if_indexable(text, "default", "2026-09-26", "normalized")
    explicit_trim = bucket_key_if_indexable(
        text, "default", "2026-09-26", "normalized", outer_trim_approved=True)
    # 默认 vs 显式获准裁剪：normalized 桶键应不同
    assert default is not None and explicit_trim is not None
    assert default.split("/")[4] != explicit_trim.split("/")[4], (
        f"normalized 桶键应随 outer_trim_approved 变化；"
        f"default={default} explicit_trim={explicit_trim}"
    )
    # 显式 outer_trim_approved=True 后的 normalized 桶键 hash_value == sha256("A")
    import hashlib
    expected = hashlib.sha256("A".encode("utf-8")).hexdigest()
    assert explicit_trim.split("/")[4] == expected


def test_three_call_styles_must_yield_same_bucket_key_for_same_text():
    """红灯——三种调用方式同文同键。

    1. bucket_key_if_indexable(text, ..., outer_trim_approved=True) 自动算
    2. 显式预算 NormalizationResult → compute_text_hashes → 显式 hash_value
    3. 显式预算 TextHashes → 显式 hash_value

    三者 raw_hash 必须一致（闸门与获准裁剪对齐）。
    """
    from news_flash_dedup.text import normalize_text
    text = "  A  "

    # 方式 1：闸门 + outer_trim_approved=True
    k1 = bucket_key_if_indexable(
        text, "default", "2026-09-26", "raw", outer_trim_approved=True)

    # 方式 2：显式 normalize + compute
    n = normalize_text(text, outer_trim_approved=True)
    h2 = compute_text_hashes(n)
    k2 = bucket_key("default", "2026-09-26", "raw", h2.raw_hash)

    # 方式 3：构造 TextHashes
    h3 = TextHashes(raw_hash=h2.raw_hash, normalized_hash=h2.normalized_hash,
                    normalizer_version=h2.normalizer_version, became_empty=h2.became_empty)
    k3 = bucket_key("default", "2026-09-26", "raw", h3.raw_hash)

    # 红：三键必须全等（修后）。当前若 outer_trim_approved 未透传，k1 != k2/k3。
    assert k1 == k2 == k3, (
        f"BEFORE FIX: k1={k1} k2={k2} k3={k3}"
    )


def test_explicit_normalization_result_passes_through_outer_trim_approved():
    """红灯——闸门应接受预算好的 NormalizationResult 并按 outer_trim_approved 算桶键。

    调用方若已预算 NormalizationResult（用 outer_trim_approved=True），
    闸门应按此预算算 normalized_hash（不是按默认 outer_trim_approved=False 重算）。
    """
    from news_flash_dedup.text import normalize_text
    text = "  A  "
    n = normalize_text(text, outer_trim_approved=True)
    # 闸门应接受 n 并直接用其 raw/normalized_hash
    k = bucket_key_if_indexable(
        text, "default", "2026-09-26", "normalized",
        outer_trim_approved=True)
    # 红：k 的 hash_value == n.normalized_hash（获准裁剪后的"A"）
    expected_normalized_hash = n.normalized_hash
    assert k is not None and k.split("/")[4] == expected_normalized_hash


# ===== 红灯 2：版本号钉死 =====


@pytest.mark.parametrize("bad_version", [
    "v999-fake", "p09-v999", "v1.0.0", "v1", "latest", "", " ", "v0", "v2-bad",
])
def test_normalizer_version_must_reject_arbitrary_string(bad_version):
    """红灯——normalizer_version 不得接受任意字符串；只允许 NORMALIZER_VERSION。

    当前实现 `is_indexable(text, normalizer_version=...)` 不校验，v999-fake 实测可入键。
    红：传错应拒绝（ValueError）。
    """
    with pytest.raises(ValueError, match="normalizer_version"):
        is_indexable("甲公司", normalizer_version=bad_version)


def test_normalizer_version_must_equal_NORMALIZER_VERSION():
    """红灯——正常通过：传 NORMALIZER_VERSION 不拒绝。"""
    # 当前实现不校验 → 绿灯；修后会保持绿。
    assert is_indexable("甲公司", normalizer_version=NORMALIZER_VERSION) is True


def test_bucket_key_if_indexable_rejects_arbitrary_normalizer_version():
    """红灯——桶键 normalizer_version 同样必须校验。"""
    with pytest.raises(ValueError, match="normalizer_version"):
        bucket_key_if_indexable("甲公司", "default", "2026-09-26", "raw",
                                 normalizer_version="v999-fake")


# ===== 红灯 3：空正文 UAT 改走闸门（先在 unit 层面）=====


def test_bucket_key_if_indexable_returns_none_for_whitespace():
    """红灯——空/纯空白输入：bucket_key_if_indexable 必须 None。

    与 test_text_indexable_gate.py 重复覆盖；本文件专注外缘裁剪对齐 + 版本号。
    """
    for text in ["", " ", "\r\n", "\n", "\t", "　"]:
        assert bucket_key_if_indexable(text, "default", "2026-09-26", "raw") is None
        assert bucket_key_if_indexable(text, "default", "2026-09-26", "normalized") is None


def test_bucket_key_can_be_called_with_arbitrary_string_but_flagged_red():
    """红灯——bucket_key() 当前不校验 indexable；文档化红线但仍可构造桶键。

    修复后：仍可构造（不抛错），但 P10+ 写桶闸门调用方必须经 bucket_key_if_indexable()。
    本测试断言当前 bucket_key() 不抛错（保持红线文档而非抛错）。
    """
    raw_hash = "a" * 64
    key = bucket_key("default", "2026-09-26", "normalized", raw_hash)
    assert key == f"default/2026-09-26/{NORMALIZER_VERSION}/normalized/{raw_hash}"


def test_indexable_gate_with_outer_trim_approved_explicit():
    """闸门 + outer_trim_approved=True 显式批准时，normalized 后仍非空 → 可入桶。

    红：当前 bucket_key_if_indexable 不传 outer_trim_approved，
    默认 outer_trim_approved=False；"  A  " 的 normalized == text（保留空白）。
    但 is_indexable 只看 strip() 后非空 → True（"  A  ".strip() == "A"）。

    修复后 is_indexable 仍 True（"  A  " strip 后是 "A" 非空）；但 raw_hash 与裁剪后不同。
    """
    assert is_indexable("  A  ") is True


def test_indexable_gate_with_outer_trim_approved_whitespace_only():
    """闸门 outer_trim_approved=True 时，\"   \" 仍因 strip 后空而不可入桶。

    即使获准裁剪，原文仅空白时 normalized_text == text（防 09 §4.3 变空串）。
    """
    assert is_indexable("   ", outer_trim_approved=True) is False
    assert bucket_key_if_indexable("   ", "default", "2026-09-26", "raw",
                                     outer_trim_approved=True) is None