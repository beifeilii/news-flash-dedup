"""P12 不可混用的 Embedding 空间与全原文覆盖分块。"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from typing import Protocol, Sequence


_IDENTIFIER = re.compile(r"[a-z0-9_]+\Z")
_RECORD_ID = re.compile(r"[0-9a-f]{64}\Z")
_SENTENCE_END = frozenset("。！？!?；;\n")


@dataclass(frozen=True)
class EmbeddingSpace:
    model: str
    revision: str
    tokenizer: str
    pooling: str
    metric: str
    dimension: int
    document_encoding: str
    query_encoding: str
    chunking: str
    preprocessing: str
    normalization: str = "none"

    def __post_init__(self) -> None:
        for value in (self.model, self.revision, self.tokenizer, self.pooling,
                      self.document_encoding, self.query_encoding, self.chunking,
                      self.preprocessing, self.normalization):
            if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
                raise ValueError("space components must be lowercase identifiers")
        if self.metric != "COSINE":
            raise ValueError("P12 V1 metric must be COSINE")
        if type(self.dimension) is not int or self.dimension < 1:
            raise ValueError("embedding dimension must be positive")

    def configuration(self) -> dict:
        return {
            "model": self.model, "revision": self.revision,
            "tokenizer": self.tokenizer, "pooling": self.pooling,
            "metric": self.metric, "dimension": self.dimension,
            "document_encoding": self.document_encoding,
            "query_encoding": self.query_encoding,
            "chunking": self.chunking, "preprocessing": self.preprocessing,
            "normalization": self.normalization,
        }

    @property
    def space_id(self) -> str:
        encoded = json.dumps(self.configuration(), sort_keys=True,
                             separators=(",", ":")).encode("utf-8")
        return "s_" + hashlib.sha256(encoded).hexdigest()[:40]


def stable_vector_id(record_id: str, chunk_id: int) -> str:
    """稳定主键公式单源（窗口W2Fβ，WA3b-M2 条64）：<record_id>_<chunk_id>。

    身份校验（64-hex record_id / 非负 int chunk_id）在 vector_id()；本函数
    仅承载公式写法，供 recall 严格路径与 P19 fake 编排层
    （vector/coordinator._vector_id）共用——双实现已单源化，公式改动只落此处。
    """
    result = f"{record_id}_{chunk_id}"
    if len(result) > 96:
        raise ValueError("vector_id exceeds VARCHAR(96)")
    return result


def vector_id(record_id: str, chunk_id: int) -> str:
    if not isinstance(record_id, str) or not _RECORD_ID.fullmatch(record_id):
        raise ValueError("record_id must be 64 lowercase hexadecimal characters")
    if type(chunk_id) is not int or chunk_id < 0:
        raise ValueError("chunk_id must be nonnegative")
    return stable_vector_id(record_id, chunk_id)


def validate_vector(vector: Sequence[float], space: EmbeddingSpace) -> tuple[float, ...]:
    if not isinstance(vector, (list, tuple)) or len(vector) != space.dimension:
        raise ValueError("embedding dimension differs from space")
    if any(type(value) not in (int, float) or not math.isfinite(value)
           for value in vector):
        raise ValueError("embedding contains a nonfinite or nonnumeric value")
    result = tuple(float(value) for value in vector)
    if not any(value != 0 for value in result):
        raise ValueError("embedding must not be a zero vector")
    return result


class Tokenizer(Protocol):
    def token_spans(self, text: str) -> Sequence[tuple[int, int]]: ...


class CodepointTokenizer:
    """只供 Mock 合同验证；不是托管模型的实际 tokenizer。"""

    def token_spans(self, text: str) -> tuple[tuple[int, int], ...]:
        return tuple((index, index + 1) for index in range(len(text)))


@dataclass(frozen=True)
class TextChunk:
    chunk_id: int
    start: int
    end: int
    token_count: int
    text: str = field(repr=False)
    chunking_version: str = "tokens512_overlap64_v1"


def chunk_text(text: str, tokenizer: Tokenizer) -> tuple[TextChunk, ...]:
    if not isinstance(text, str):
        raise TypeError("text must be str")
    if not text.strip():
        return ()
    spans = tuple(tokenizer.token_spans(text))
    if not spans or spans[0][0] != 0 or spans[-1][1] != len(text):
        raise ValueError("tokenizer did not cover full original text")
    for previous, current in zip(spans, spans[1:]):
        if previous[1] != current[0] or previous[0] >= previous[1]:
            raise ValueError("tokenizer spans are not contiguous")
    if spans[-1][0] >= spans[-1][1]:
        raise ValueError("last token span is empty")
    chunks: list[TextChunk] = []
    start_token = 0
    while start_token < len(spans):
        end_token = min(start_token + 512, len(spans))
        if end_token < len(spans):
            boundary_start = max(start_token + 1, end_token - 64)
            for position in range(end_token - 1, boundary_start - 1, -1):
                if text[spans[position][0]:spans[position][1]].endswith(tuple(_SENTENCE_END)):
                    end_token = position + 1
                    break
        start = spans[start_token][0]
        end = spans[end_token - 1][1]
        chunks.append(TextChunk(len(chunks), start, end, end_token - start_token,
                                text[start:end]))
        if end_token == len(spans):
            break
        # 窗口W2Fβ（WA3b 条66，原 L135-136 死分支已删；W3F W3c-F-4 首句
        # 措辞勘正，结论不动）：到达此处时 end_token ∈ [start_token + 449,
        # start_token + 512]——min 初值 start_token + 512（end_token <
        # len(spans) 前提），句界回扫仅减不增、下界 boundary_start =
        # start_token + 448（回扫成功时 end_token 已被改小，故原首句
        # "必有 end_token == start_token + 512"与回扫成功情形矛盾，
        # 措辞失准），故 next_start = end_token - 64 >=
        # start_token + 385 > start_token 恒成立——原「chunk boundary
        # cannot make progress」分支结构性不可达。
        start_token = end_token - 64
    return tuple(chunks)
