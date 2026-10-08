from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from . import NORMALIZER_VERSION, NormalizationResult, normalize_text


HashKind = Literal["raw", "normalized"]


@dataclass(frozen=True)
class TextHashes:
    """同一 text 的双重 Hash（桶索引值；不含 record_id）。"""

    raw_hash: str
    normalized_hash: str
    normalizer_version: str
    became_empty: bool


def compute_text_hashes(normalized: NormalizationResult) -> TextHashes:
    """从老 NormalizationResult 提双重 Hash（桶索引值）。"""
    return TextHashes(
        raw_hash=normalized.raw_hash,
        normalized_hash=normalized.normalized_hash,
        normalizer_version=normalized.normalizer_version,
        became_empty=(normalized.normalized_text.strip() == ""),
    )


def _assert_normalizer_version(normalizer_version: str) -> None:
    """版本号钉死：normalizer_version 必须 == NORMALIZER_VERSION；传错拒绝。

    09 §4.4：桶键含 normalizer_version；不允许任意字符串（如 v999-fake）混入。
    """
    if normalizer_version != NORMALIZER_VERSION:
        raise ValueError(
            f"normalizer_version must be {NORMALIZER_VERSION!r}; "
            f"got {normalizer_version!r}"
        )


def is_indexable(text: str, *, outer_trim_approved: bool = False,
                 normalizer_version: str = NORMALIZER_VERSION) -> bool:
    """P09 空正文入桶闸门：归一后非空且非纯空白的正文才可入桶。

    09 §4.3 + P09 收尾用户裁决：归一后为空/纯空白的正文不得产生 normalized 桶条目。
    P10+ 写桶必须经此闸门；调用 bucket_key_if_indexable() 而非直接 bucket_key()。

    入桶条件：
    - 原文非空（None/空串拒绝）
    - 归一后非空（normalize_text 不变空）
    - 归一后非纯空白（strip 后非空）

    outer_trim_approved 默认 False（必须显式 True 才裁外缘；按 09 §4.2 启用条件）。
    normalizer_version 必须等于 NORMALIZER_VERSION（钉死）。
    """
    _assert_normalizer_version(normalizer_version)
    if not text:
        return False
    n = normalize_text(text, outer_trim_approved=outer_trim_approved)
    if not n.normalized_text:
        return False
    if not n.normalized_text.strip():
        return False
    return True


def bucket_key(scope_id: str, business_date: str, hash_kind: HashKind,
               hash_value: str, normalizer_version: str = NORMALIZER_VERSION) -> str:
    """09 §4.4 桶键：scope_id/business_date/normalizer_version/hash_kind/hash_value。

    ⚠️ 不调用 is_indexable 检查；调用方须先确认 text 可入桶再构造桶键（P10 写桶闸门）。
    normalizer_version 必须 == NORMALIZER_VERSION（钉死）。
    """
    _assert_normalizer_version(normalizer_version)
    return "/".join([scope_id, business_date, normalizer_version, hash_kind, hash_value])


def bucket_key_if_indexable(text: str, scope_id: str, business_date: str,
                              hash_kind: HashKind, *,
                              outer_trim_approved: bool = False,
                              normalizer_version: str = NORMALIZER_VERSION,
                              hash_value: str | None = None) -> str | None:
    """P10 写桶闸门入口：text 可入桶 → 返回 bucket_key；不可入桶 → 返回 None。

    P10+ 写桶必须用此入口，禁止直接调用 bucket_key()。
    hash_value 缺省：自动从 text 计算（如 raw_hash / normalized_hash）。
    outer_trim_approved 默认 False（必须显式 True 才裁外缘；按 09 §4.2 启用条件）。
    normalizer_version 必须等于 NORMALIZER_VERSION（钉死）。

    闸门与获准裁剪对齐：默认 outer_trim_approved=False → normalized_text 保留外缘空白；
    显式 outer_trim_approved=True → normalized_text 裁掉外缘。
    三种调用方式同文同键：
    - bucket_key_if_indexable(text, ..., outer_trim_approved=True) 自动算
    - normalize_text(text, outer_trim_approved=True) + compute_text_hashes + bucket_key(...)
    - TextHashes(raw/normalized_hash) + bucket_key(...)
    """
    # W2 批41（F7 性能）：双 normalize 合并——旧实现 is_indexable() 内部归一一次、
    # hash_value 缺省又归一一次；现归一一次并以同一结果过闸+取哈希（判定等价：
    # 闸门条件与 is_indexable 逐条同序）。
    _assert_normalizer_version(normalizer_version)
    if not text:
        return None
    normalized = normalize_text(text, outer_trim_approved=outer_trim_approved)
    if not normalized.normalized_text:
        return None
    if not normalized.normalized_text.strip():
        return None
    if hash_value is None:
        h = compute_text_hashes(normalized)
        if hash_kind == "raw":
            hash_value = h.raw_hash
        elif hash_kind == "normalized":
            hash_value = h.normalized_hash
        else:
            raise ValueError(f"unknown hash_kind: {hash_kind!r}")
    return bucket_key(scope_id, business_date, hash_kind, hash_value,
                       normalizer_version=normalizer_version)


def parse_bucket_key(key: str) -> dict:
    """反解析桶键为 dict（仅校验 5 段）。"""
    parts = key.split("/")
    if len(parts) != 5:
        raise ValueError(f"bucket key must have 5 segments; got {len(parts)}: {key!r}")
    scope_id, business_date, normalizer_version, hash_kind, hash_value = parts
    if hash_kind not in {"raw", "normalized"}:
        raise ValueError(f"hash_kind must be 'raw' or 'normalized'; got {hash_kind!r}")
    return {
        "scope_id": scope_id,
        "business_date": business_date,
        "normalizer_version": normalizer_version,
        "hash_kind": hash_kind,
        "hash_value": hash_value,
    }


def text_hashes_for_text(text: str) -> TextHashes:
    """便捷入口：text → normalize_text → compute_text_hashes。"""
    return compute_text_hashes(normalize_text(text))


__all__ = [
    "HashKind",
    "TextHashes",
    "bucket_key",
    "bucket_key_if_indexable",
    "compute_text_hashes",
    "is_indexable",
    "parse_bucket_key",
    "text_hashes_for_text",
]