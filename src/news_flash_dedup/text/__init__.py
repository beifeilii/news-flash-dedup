"""Lossless full-text normalization and text-equality evidence.

This module does not decide whether two news items describe the same fact. A
caller must validate both articles' facts and evidence before requesting a
text-equality certificate, and must still validate the pair's time relation.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from hashlib import sha256


NORMALIZER_VERSION = "v1-crlf-outer-trim-1"
Span = tuple[int, int]

# 2026-10-09（P0-b，log\temp\判定链修复方案-P0施工单-呈外部评审.md §一-P0-b）：
# 证书解耦开关 DEDUP_CERT_DECOUPLE（默认关=现役逐字节）——开启时
# EXACT/LOSSLESS 证书通道不再要求双侧事实工件 validated_complete（文本
# 相等本身是零语义风险的判定依据，不需要事实抽取背书）；最小正文长度闸
# DEDUP_EXACT_MIN_LEN（默认 50 字符，可调）防模板化超短文本误签，不足长度
# 仍走老路（合格门照常）。FACT_EQUIVALENT 通道的完备性要求一字不动。
CERT_DECOUPLE_ENV = "DEDUP_CERT_DECOUPLE"
EXACT_MIN_LEN_ENV = "DEDUP_EXACT_MIN_LEN"
EXACT_MIN_LEN_DEFAULT = 50


def cert_decouple_enabled(env: Mapping[str, str] | None = None) -> bool:
    """2026-10-09（P0-b）开关解析：精确 "1"=开；缺省/空串/其余一律关
    （fail-closed 默认关，"true"/大小写变体等不放大——mode_from_environment
    同型纪律，recall/service.py:45-55）。"""
    source = os.environ if env is None else env
    return source.get(CERT_DECOUPLE_ENV, "") == "1"


def exact_min_len(env: Mapping[str, str] | None = None) -> int:
    """2026-10-09（P0-b）长度闸解析：缺省/空串=50；非 ASCII 十进制数字串
    一律 ValueError（配置畸形显式拒识，不猜默认值；开关关时本函数不被
    调用，畸形值零效应）。"""
    source = os.environ if env is None else env
    raw = source.get(EXACT_MIN_LEN_ENV, "")
    if not raw:
        return EXACT_MIN_LEN_DEFAULT
    if not raw.isascii() or not raw.isdigit():
        raise ValueError(
            f"{EXACT_MIN_LEN_ENV} must be a nonnegative decimal integer; "
            f"got {raw!r}")
    return int(raw)


@dataclass(frozen=True)
class NormalizationTransform:
    rule_id: str
    input_span: Span
    output_span: Span


@dataclass(frozen=True)
class NormalizationResult:
    text: str = field(repr=False)
    normalized_text: str = field(repr=False)
    position_map: tuple[Span, ...]
    removed_spans: tuple[Span, ...]
    transforms: tuple[NormalizationTransform, ...]
    normalizer_version: str
    raw_hash: str
    normalized_hash: str
    outer_trim_approved: bool

    def original_span(self, start: int, end: int) -> Span:
        """Map a nonempty normalized code-point span to original code points."""
        if type(start) is not int or type(end) is not int:
            raise TypeError("span offsets must be integers")
        if not 0 <= start < end <= len(self.position_map):
            raise ValueError("normalized span is empty or out of bounds")
        return self.position_map[start][0], self.position_map[end - 1][1]

    def indexable_hashes(self) -> tuple[tuple[str, str], ...]:
        """Return candidate bucket values, excluding empty-content fingerprints."""
        if not self.text.strip() or not self.normalized_text.strip():
            return ()
        return (("raw", self.raw_hash), ("normalized", self.normalized_hash))


class TextMatchKind(str, Enum):
    EXACT_TEXT_MATCH = "EXACT_TEXT_MATCH"
    LOSSLESS_TEXT_MATCH = "LOSSLESS_TEXT_MATCH"


@dataclass(frozen=True)
class TextEqualityCertificate:
    kind: TextMatchKind
    normalizer_version: str
    left_raw_hash: str
    right_raw_hash: str
    normalized_hash: str


def _digest(text: str) -> str:
    try:
        encoded = text.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ValueError("text contains invalid Unicode surrogate") from exc
    return sha256(encoded).hexdigest()


def normalize_text(text: str, *, outer_trim_approved: bool = False) -> NormalizationResult:
    """Preserve original code points and apply only approved layout changes.

    ``outer_trim_approved`` must be set only after the caller has proved that
    the outer whitespace is ordinary flash layout rather than code, tables,
    quoted text, or aligned data. The default safely leaves it untouched.
    """
    if not isinstance(text, str):
        raise TypeError("text must be str")
    if not isinstance(outer_trim_approved, bool):
        raise TypeError("outer_trim_approved must be bool")
    raw_hash = _digest(text)

    chars: list[str] = []
    position_map: list[Span] = []
    crlf_events: list[tuple[Span, int]] = []
    index = 0
    while index < len(text):
        if text.startswith("\r\n", index):
            chars.append("\n")
            position_map.append((index, index + 2))
            crlf_events.append(((index, index + 2), len(chars) - 1))
            index += 2
        else:
            chars.append(text[index])
            position_map.append((index, index + 1))
            index += 1

    start = 0
    end = len(chars)
    if outer_trim_approved:
        while start < end and chars[start] in " \t\n":
            start += 1
        while end > start and chars[end - 1] in " \t\n":
            end -= 1
        # A whitespace-only body must never acquire the common empty fingerprint.
        if start == end:
            start = 0
            end = len(chars)

    removed_spans: list[Span] = []
    if start:
        removed_spans.append((0, position_map[start - 1][1]))
    if end < len(chars):
        removed_spans.append((position_map[end][0], len(text)))

    transforms: list[NormalizationTransform] = []
    for input_span, output_index in crlf_events:
        clipped = min(max(output_index - start, 0), end - start)
        output_span = (clipped, clipped + 1) if start <= output_index < end else (clipped, clipped)
        transforms.append(NormalizationTransform("CRLF_TO_LF", input_span, output_span))
    if start:
        transforms.append(NormalizationTransform("TRIM_OUTER_WHITESPACE", removed_spans[0], (0, 0)))
    if end < len(chars):
        transforms.append(NormalizationTransform("TRIM_OUTER_WHITESPACE", removed_spans[-1], (end - start, end - start)))

    normalized_text = "".join(chars[start:end])
    return NormalizationResult(
        text=text,
        normalized_text=normalized_text,
        position_map=tuple(position_map[start:end]),
        removed_spans=tuple(removed_spans),
        transforms=tuple(transforms),
        normalizer_version=NORMALIZER_VERSION,
        raw_hash=raw_hash,
        normalized_hash=_digest(normalized_text),
        outer_trim_approved=outer_trim_approved,
    )


def certify_text_equality(
    left: NormalizationResult,
    right: NormalizationResult,
    *,
    left_qualified: bool = False,
    right_qualified: bool = False,
    decouple_qualification: bool = False,
    min_body_length: int = 0,
) -> TextEqualityCertificate | None:
    """Verify a Hash hit against complete text after both quality gates pass.

    Qualification must cover subject, event, fact/numeric completeness,
    modality, internal consistency, and original-text Evidence. This result is
    only text evidence; pairwise Fact/time validation and five-field result
    aggregation remain downstream.

    2026-10-09（P0-b，log\temp\判定链修复方案-P0施工单-呈外部评审.md
    §一-P0-b）：decouple_qualification=True 时 EXACT/LOSSLESS 证书通道与
    完备性闸解耦——不再要求双侧 qualified（文本相等本身是判定依据），改挂
    min_body_length 最小正文长度闸（按归一后等值内容长度计，CRLF 不计
    双份）；不足长度回落旧路（qualified 双闸照常执法）。默认 False 且
    min_body_length=0 = 旧路逐字节（合格门先行，长度闸零效应）。
    FACT_EQUIVALENT 释义级证书不在此颁发，其完备性要求由消费点保持
    一字不动。
    """
    if not isinstance(decouple_qualification, bool):
        raise TypeError("decouple_qualification must be bool")
    if type(min_body_length) is not int or min_body_length < 0:
        raise ValueError("min_body_length must be a nonnegative int")
    if decouple_qualification:
        # P0-b 解耦路：判型前置（长度闸需触 .normalized_text）；不足最小
        # 长度时合格门回落执法（"不足长度仍走老路"，施工单 §一-P0-b 防护闸）。
        if not isinstance(left, NormalizationResult) or not isinstance(right, NormalizationResult):
            return None
        if min(len(left.normalized_text),
               len(right.normalized_text)) < min_body_length:
            if not (left_qualified is True and right_qualified is True):
                return None
    else:
        if not (left_qualified is True and right_qualified is True):
            return None
    if not isinstance(left, NormalizationResult) or not isinstance(right, NormalizationResult):
        return None
    if left.normalizer_version != right.normalizer_version:
        return None
    # Rebuild the full certificate so a persisted or mutated Hash/mapping cannot
    # stand in for actual original-text equality. Corrupt persisted values fail
    # closed, including invalid Unicode that cannot be encoded as UTF-8.
    try:
        if not left.indexable_hashes() or not right.indexable_hashes():
            return None
        if left != normalize_text(left.text, outer_trim_approved=left.outer_trim_approved):
            return None
        if right != normalize_text(right.text, outer_trim_approved=right.outer_trim_approved):
            return None
    except (TypeError, ValueError, AttributeError):
        return None

    if left.raw_hash == right.raw_hash and left.text == right.text:
        kind = TextMatchKind.EXACT_TEXT_MATCH
    elif left.normalized_hash == right.normalized_hash and left.normalized_text == right.normalized_text:
        kind = TextMatchKind.LOSSLESS_TEXT_MATCH
    else:
        return None
    return TextEqualityCertificate(
        kind=kind,
        normalizer_version=left.normalizer_version,
        left_raw_hash=left.raw_hash,
        right_raw_hash=right.raw_hash,
        normalized_hash=left.normalized_hash,
    )


__all__ = [
    "CERT_DECOUPLE_ENV",
    "EXACT_MIN_LEN_DEFAULT",
    "EXACT_MIN_LEN_ENV",
    "NORMALIZER_VERSION",
    "NormalizationResult",
    "NormalizationTransform",
    "TextEqualityCertificate",
    "TextMatchKind",
    "cert_decouple_enabled",
    "certify_text_equality",
    "exact_min_len",
    "normalize_text",
]
