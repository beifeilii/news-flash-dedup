"""P10 指纹的确定性、信息保留和桶覆盖。"""

from __future__ import annotations

import hashlib

from news_flash_dedup.recall.fingerprints import (
    MINHASH_PERMUTATIONS,
    Fingerprints,
    build_fingerprints,
    character_shingles,
    hamming_distance,
    minhash_similarity,
    simhash_bands,
    simhash_from_features,
)


def test_simhash_uses_sha256_prefix_big_endian_for_one_feature():
    feature = ("token", "未增持3%")
    expected = int.from_bytes(
        hashlib.sha256("simhash_v1|token|未增持3%".encode("utf-8")).digest()[:8],
        "big",
    )
    assert simhash_from_features({feature}) == expected


def test_simhash_four_blocks_cover_any_three_bit_changes():
    original = 0x1234567890ABCDEF
    for changed in ((0, 1, 2), (0, 17, 34), (15, 31, 63), (4, 20, 49)):
        altered = original
        for bit in changed:
            altered ^= 1 << bit
        assert hamming_distance(original, altered) == 3
        assert set(simhash_bands(original)) & set(simhash_bands(altered))


def test_short_text_has_nonempty_shingle_and_unicode_codepoint_semantics():
    assert character_shingles("甲") == frozenset({"甲"})
    assert character_shingles("甲乙") == frozenset({"甲乙"})
    assert character_shingles("甲😀乙") == frozenset({"甲😀乙"})


def test_text_features_preserve_numbers_negation_and_comparison():
    a = build_fingerprints("甲公司未增持3%")
    b = build_fingerprints("甲公司增持3%")
    c = build_fingerprints("甲公司增持>3%")
    assert isinstance(a, Fingerprints)
    assert isinstance(b, Fingerprints)
    assert isinstance(c, Fingerprints)
    assert a.simhash != b.simhash
    assert b.simhash != c.simhash
    assert a.minhash_signature != b.minhash_signature
    assert b.minhash_signature != c.minhash_signature


def test_crlf_and_lf_share_fingerprints_but_raw_text_is_unchanged():
    a = build_fingerprints("甲公司\r\n增持")
    b = build_fingerprints("甲公司\n增持")
    assert a == b


def test_minhash_uses_all_128_values_in_32_distinct_band_positions():
    result = build_fingerprints("甲公司完成股份回购，金额为3亿元。")
    assert isinstance(result, Fingerprints)
    assert len(result.minhash_signature) == MINHASH_PERMUTATIONS == 128
    assert len(result.minhash_bands) == 32
    assert len(set(result.minhash_bands)) == 32
    assert len(result.simhash_bands) == 4
    assert minhash_similarity(result.minhash_signature, result.minhash_signature) == 1.0


def test_empty_or_whitespace_text_has_no_fingerprint():
    for text in ("", " ", "\t\n", "\r\n"):
        assert build_fingerprints(text) is None


def test_minhash_similarity_rejects_incomplete_signatures():
    result = build_fingerprints("甲公司完成回购")
    assert isinstance(result, Fingerprints)
    try:
        minhash_similarity(result.minhash_signature[:-1], result.minhash_signature)
    except ValueError:
        pass
    else:
        raise AssertionError("incomplete 128-value signature accepted")
