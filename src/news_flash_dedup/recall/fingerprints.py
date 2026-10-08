"""P10 确定性近重复指纹；分数仅用于候选顺序。"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Iterable

from news_flash_dedup.text import normalize_text


SIMHASH_VERSION = "simhash_v1"
MINHASH_VERSION = "minhash_v1"
MINHASH_SEED = "news_flash_dedup_v1"
MINHASH_PERMUTATIONS = 128
MINHASH_BANDS = 32
_TOKEN_PATTERN = re.compile(r"\w+|[^\w\s]", re.UNICODE)


@dataclass(frozen=True)
class Fingerprints:
    # 窗口W2Fβ（WA3a-F2 条10）：normalizer_version 死字段已清理（只写不读；
    # 索引侧 normalizer_version 由 hash_channel.prepare_recall_fields 从
    # NormalizationResult 单源写入并经桶查询/回取校验消费，与本工件无涉）。
    simhash: int
    simhash_bands: tuple[str, ...]
    minhash_signature: tuple[int, ...]
    minhash_bands: tuple[str, ...]


def character_shingles(text: str) -> frozenset[str]:
    """保留码点与文内空白；短文用整段，不产生空 shingle。"""
    if not text:
        return frozenset()
    if len(text) < 3:
        return frozenset({text})
    return frozenset(text[index:index + 3] for index in range(len(text) - 2))


def _features(text: str) -> frozenset[tuple[str, str]]:
    shingles = {("char", shingle) for shingle in character_shingles(text)}
    tokens = {("token", match.group()) for match in _TOKEN_PATTERN.finditer(text)}
    return frozenset(shingles | tokens)


def simhash_from_features(features: Iterable[tuple[str, str]]) -> int:
    weights = [0] * 64
    # 特征去重后各权重为 1；不用 Python 进程随机 hash。
    for kind, feature in set(features):
        digest = hashlib.sha256(
            f"{SIMHASH_VERSION}|{kind}|{feature}".encode("utf-8")
        ).digest()
        bits = int.from_bytes(digest[:8], "big")
        for position in range(64):
            weights[position] += 1 if bits & (1 << position) else -1
    return sum(1 << position for position, weight in enumerate(weights) if weight > 0)


def simhash_bands(value: int) -> tuple[str, ...]:
    if type(value) is not int or not 0 <= value < 1 << 64:
        raise ValueError("simhash must be an unsigned 64-bit integer")
    return tuple(
        f"{SIMHASH_VERSION}:{index}:{(value >> (48 - 16 * index)) & 0xffff:04x}"
        for index in range(4)
    )


def hamming_distance(left: int, right: int) -> int:
    if any(type(value) is not int or not 0 <= value < 1 << 64
           for value in (left, right)):
        raise ValueError("simhash must be an unsigned 64-bit integer")
    return (left ^ right).bit_count()


def _minhash_signature(shingles: frozenset[str]) -> tuple[int, ...]:
    if not shingles:
        return ()
    signature = []
    for permutation in range(MINHASH_PERMUTATIONS):
        minimum = min(
            int.from_bytes(hashlib.sha256(
                f"{MINHASH_VERSION}|{MINHASH_SEED}|{permutation}|{shingle}".encode("utf-8")
            ).digest()[:8], "big")
            for shingle in shingles
        )
        signature.append(minimum)
    return tuple(signature)


def _minhash_bands(signature: tuple[int, ...]) -> tuple[str, ...]:
    if len(signature) != MINHASH_PERMUTATIONS:
        raise ValueError("minhash signature must have 128 values")
    bands = []
    for index in range(MINHASH_BANDS):
        values = signature[index * 4:(index + 1) * 4]
        packed = b"".join(value.to_bytes(8, "big") for value in values)
        digest = hashlib.sha256(packed).hexdigest()
        bands.append(f"{MINHASH_VERSION}:{index}:{digest}")
    return tuple(bands)


def minhash_similarity(left: tuple[int, ...], right: tuple[int, ...]) -> float:
    if len(left) != MINHASH_PERMUTATIONS or len(right) != MINHASH_PERMUTATIONS:
        raise ValueError("minhash signatures must have 128 values")
    return sum(a == b for a, b in zip(left, right)) / MINHASH_PERMUTATIONS


def build_fingerprints(text: str, *, outer_trim_approved: bool = False) -> Fingerprints | None:
    normalized = normalize_text(text, outer_trim_approved=outer_trim_approved)
    if not normalized.normalized_text or not normalized.normalized_text.strip():
        return None
    shingles = character_shingles(normalized.normalized_text)
    signature = _minhash_signature(shingles)
    value = simhash_from_features(_features(normalized.normalized_text))
    return Fingerprints(
        simhash=value,
        simhash_bands=simhash_bands(value),
        minhash_signature=signature,
        minhash_bands=_minhash_bands(signature),
    )
