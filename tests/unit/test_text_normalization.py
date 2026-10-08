import hashlib

import pytest

from news_flash_dedup.text import (
    NormalizationResult,
    TextMatchKind,
    certify_text_equality,
    normalize_text,
)


def test_original_is_preserved_and_crlf_maps_to_code_point_spans():
    original = "甲𠮷\r\n公司\r\n完成回购。"

    result = normalize_text(original)

    assert result.text == original
    assert result.normalized_text == "甲𠮷\n公司\n完成回购。"
    assert result.position_map[:5] == ((0, 1), (1, 2), (2, 4), (4, 5), (5, 6))
    assert result.position_map[5] == (6, 8)
    assert result.original_span(1, 4) == (1, 5)
    assert original[slice(*result.original_span(1, 4))] == "𠮷\r\n公"
    assert [(t.rule_id, t.input_span, t.output_span) for t in result.transforms] == [
        ("CRLF_TO_LF", (2, 4), (2, 3)),
        ("CRLF_TO_LF", (6, 8), (5, 6)),
    ]
    assert result.removed_spans == ()
    assert result.raw_hash == hashlib.sha256(original.encode("utf-8")).hexdigest()
    assert result.normalized_hash == hashlib.sha256(result.normalized_text.encode("utf-8")).hexdigest()


def test_outer_trim_requires_explicit_approval_and_remains_traceable():
    original = " \t甲公司完成回购。\r\n\t "
    default = normalize_text(original)
    approved = normalize_text(original, outer_trim_approved=True)

    assert default.normalized_text == " \t甲公司完成回购。\n\t "
    assert approved.normalized_text == "甲公司完成回购。"
    assert approved.removed_spans == ((0, 2), (10, 14))
    assert approved.position_map == tuple((i, i + 1) for i in range(2, 10))
    assert {t.rule_id for t in approved.transforms} == {"CRLF_TO_LF", "TRIM_OUTER_WHITESPACE"}
    assert normalize_text(" \t\r\n ", outer_trim_approved=True).normalized_text != ""


@pytest.mark.parametrize(
    "text",
    [
        "代码600000、010、v1.10、12/31/36；100不等于1。",
        "利率3.75%、-1%、1-2、C++；>3%、>=3%、<3%。",
        "未取消招标；预计、若、可能；（初值）和\"终值\"。",
        "Boring Company 完成30 亿美元；美元美元。",
        "𠮷𠮷、①、Ａ、é、e\u0301；中文1 English",
    ],
)
def test_unapproved_semantic_rewrites_never_enter_full_text_normalization(text):
    result = normalize_text(text)
    assert result.text == result.normalized_text == text
    assert result.position_map == tuple((i, i + 1) for i in range(len(text)))
    assert result.transforms == ()


def test_only_exact_or_authorized_lossless_full_text_equality_yields_text_certificate():
    exact = normalize_text("甲公司完成回购。")
    crlf = normalize_text("甲公司\r\n完成回购。")
    lf = normalize_text("甲公司\n完成回购。")
    punctuated = normalize_text("甲公司，完成回购。")

    exact_certificate = certify_text_equality(
        exact, normalize_text(exact.text), left_qualified=True, right_qualified=True
    )
    lossless_certificate = certify_text_equality(
        crlf, lf, left_qualified=True, right_qualified=True
    )

    assert exact_certificate is not None and exact_certificate.kind is TextMatchKind.EXACT_TEXT_MATCH
    assert lossless_certificate is not None and lossless_certificate.kind is TextMatchKind.LOSSLESS_TEXT_MATCH
    assert certify_text_equality(exact, punctuated, left_qualified=True, right_qualified=True) is None
    assert not hasattr(lossless_certificate, "is_duplicate")


def test_outer_whitespace_matches_only_with_explicitly_approved_transform():
    plain = normalize_text("甲公司完成回购。")
    spaced = " \t甲公司完成回购。\n"

    assert certify_text_equality(
        plain, normalize_text(spaced), left_qualified=True, right_qualified=True
    ) is None
    approved = certify_text_equality(
        plain,
        normalize_text(spaced, outer_trim_approved=True),
        left_qualified=True,
        right_qualified=True,
    )
    assert approved is not None and approved.kind is TextMatchKind.LOSSLESS_TEXT_MATCH


def test_same_hash_value_without_same_full_text_cannot_certify_match():
    first = normalize_text("甲公司完成回购。")
    second = normalize_text("乙公司完成回购。")
    forged = NormalizationResult(
        text=second.text,
        normalized_text=second.normalized_text,
        position_map=second.position_map,
        removed_spans=second.removed_spans,
        transforms=second.transforms,
        normalizer_version=second.normalizer_version,
        raw_hash=first.raw_hash,
        normalized_hash=first.normalized_hash,
        outer_trim_approved=second.outer_trim_approved,
    )

    assert certify_text_equality(first, forged, left_qualified=True, right_qualified=True) is None


def test_corrupt_persisted_text_artifact_cannot_certify_match():
    valid = normalize_text("甲公司完成回购。")
    corrupt = NormalizationResult(
        text="\ud800",
        normalized_text=valid.normalized_text,
        position_map=valid.position_map,
        removed_spans=valid.removed_spans,
        transforms=valid.transforms,
        normalizer_version=valid.normalizer_version,
        raw_hash=valid.raw_hash,
        normalized_hash=valid.normalized_hash,
        outer_trim_approved=False,
    )
    assert certify_text_equality(valid, corrupt, left_qualified=True, right_qualified=True) is None


def test_empty_normalized_fingerprint_is_not_a_hash_candidate_or_certificate():
    empty = normalize_text("")
    spaces = normalize_text("  ", outer_trim_approved=True)
    other_noise = normalize_text("\t\r\n", outer_trim_approved=True)

    assert empty.indexable_hashes() == ()
    assert spaces.normalized_text == "  "
    assert spaces.indexable_hashes() == other_noise.indexable_hashes() == ()
    assert certify_text_equality(empty, normalize_text("")) is None
    assert certify_text_equality(spaces, normalize_text("  ", outer_trim_approved=True), left_qualified=True, right_qualified=True) is None


@pytest.mark.parametrize("incomplete", ["上涨1%", "（产联社）", "甲公司本次发行总额为100亿元，本次发行总额为101亿元。"])
def test_hash_match_without_double_sided_quality_gate_is_never_certified(incomplete):
    artifact = normalize_text(incomplete)
    assert certify_text_equality(artifact, normalize_text(incomplete)) is None
    assert certify_text_equality(artifact, normalize_text(incomplete), left_qualified=True) is None


def test_invalid_unicode_surrogates_fail_before_hashing():
    with pytest.raises(ValueError, match="Unicode"):
        normalize_text("\ud800")


@pytest.mark.parametrize("start,end", [(-1, 1), (0, 0), (0, 99), (2, 1)])
def test_normalized_evidence_span_rejects_invalid_offsets(start, end):
    artifact = normalize_text("𠮷\r\n甲")
    with pytest.raises(ValueError, match="span"):
        artifact.original_span(start, end)
