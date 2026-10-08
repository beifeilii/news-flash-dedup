"""P12 向量空间、稳定主键与原文全覆盖分块。"""

from __future__ import annotations

import pytest

from news_flash_dedup.recall.vector_space import (
    CodepointTokenizer,
    EmbeddingSpace,
    chunk_text,
    validate_vector,
    vector_id,
)


RECORD_ID = "a" * 64


def _space(dimension=4):
    return EmbeddingSpace(
        model="mock_embedding", revision="fixture_v1",
        tokenizer="codepoint_v1", pooling="none", metric="COSINE",
        dimension=dimension, document_encoding="plain_v1",
        query_encoding="plain_v1", chunking="tokens512_overlap64_v1",
        preprocessing="p09_v1",
    )


def test_space_id_is_stable_and_changes_with_dimension_or_encoding():
    first = _space()
    assert first.space_id == _space().space_id
    assert first.space_id != _space(5).space_id
    different = EmbeddingSpace(**{**first.configuration(), "query_encoding": "other_v1"})
    assert first.space_id != different.space_id
    assert len(first.space_id) <= 64
    assert all(char.islower() or char.isdigit() or char == "_" for char in first.space_id)


def test_vector_id_is_stable_and_rejects_invalid_record_or_chunk():
    assert vector_id(RECORD_ID, 0) == RECORD_ID + "_0"
    assert vector_id(RECORD_ID, 12) == RECORD_ID + "_12"
    with pytest.raises(ValueError):
        vector_id("not-a-record", 0)
    with pytest.raises(ValueError):
        vector_id(RECORD_ID, -1)


def test_vector_validation_rejects_wrong_dimension_nonfinite_and_zero():
    space = _space()
    assert validate_vector([1.0, 0.0, 0.0, 0.0], space) == (1.0, 0.0, 0.0, 0.0)
    for vector in ([1.0, 2.0], [1.0, float("nan"), 0.0, 0.0],
                   [0.0] * 4, [1.0, float("inf"), 0.0, 0.0]):
        with pytest.raises(ValueError):
            validate_vector(vector, space)


def test_chunking_covers_every_original_codepoint_and_last_tail():
    text = "甲" * 1097 + "最后一段"
    chunks = chunk_text(text, CodepointTokenizer())
    assert len(chunks) >= 3
    assert chunks[0].start == 0
    assert chunks[-1].end == len(text)
    assert [chunk.chunk_id for chunk in chunks] == list(range(len(chunks)))
    assert all(chunk.text == text[chunk.start:chunk.end] for chunk in chunks)
    assert all(chunk.token_count <= 512 for chunk in chunks)
    assert all(any(chunk.start <= point < chunk.end for chunk in chunks)
               for point in range(len(text)))


def test_chunking_prefers_sentence_boundary_and_preserves_overlap():
    text = "甲" * 490 + "。" + "乙" * 100
    chunks = chunk_text(text, CodepointTokenizer())
    assert chunks[0].end == 491
    assert chunks[1].start < chunks[0].end
    assert chunks[-1].end == len(text)


def test_empty_body_has_no_vector_chunks():
    assert chunk_text(" \t\n", CodepointTokenizer()) == ()
