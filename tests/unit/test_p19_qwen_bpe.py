"""B5/E2 S2 红测（设计 §2.6 案 A + §2.7 + §7.2 ⑤⑥）：vector/qwen_bpe.py。

红测清单映射（log\设计-真Embedding接线包.md v4 R164）：
- 案 A 制品钉：vendored Qwen 系 BPE 制品（vocab.json/merges.txt）sha256 钉入；
  加载校验失败 fail-closed（QwenBpeTokenizerLoadError）；tokenizer 组件 =
  qwen_bpe_<hash8>（制品 sha256 派生，EmbeddingSpace 组件正则兼容）。
- 分块合同：token_spans 全覆盖（首 span 起于 0、末 span 止于 len(text)、逐段
  相邻非空——recall/vector_space.py:120-127 既有校验面）；确定性；已知 token 钉
  （'中国'/'美联储'/'。'/' hello' 等单词元形）；byte-level 兜底（任意 unicode
  串均可切分，无 OOV）。
- 分块新身份：real_chunk_text 复用 chunk_text 512/64 句界逻辑，产出
  TextChunk.chunking_version == "tokens512_overlap64_bpe_v1"（与存量码点轨
  tokens512_overlap64_v1 结构性区分）；空文本 → ()。
- ⑤ 真空间登记（§2.7）：real_embedding_space() 组件逐字段钉（model 净化
  text_embedding_v3 / revision=n11_20260927 / tokenizer=qwen_bpe_<hash8> /
  pooling=none / COSINE / dimension=1024 / 对称 plain_v1 / chunking 新身份 /
  preprocessing=p09_v1）；space_id 确定性 + frozen + 组件变更即新空间（INV-4）。
- 6000 字护栏属 S1 客户端（§3.2：截断仅作用嵌入输入，chunk 原文区间不动）。
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from news_flash_dedup.recall.vector_space import (
    CodepointTokenizer,
    EmbeddingSpace,
    TextChunk,
    chunk_text,
)
from news_flash_dedup.vector import qwen_bpe as qb


VENDOR = Path(qb.__file__).resolve().parent / "vendor"


# ---------- 制品钉（sha256 钉死 + fail-closed 加载） ----------

def test_vendored_artifacts_exist_and_match_pinned_sha256():
    vocab = VENDOR / "qwen_bpe_vocab.json"
    merges = VENDOR / "qwen_bpe_merges.txt"
    assert vocab.exists() and merges.exists()
    vocab_sha = hashlib.sha256(vocab.read_bytes()).hexdigest()
    merges_sha = hashlib.sha256(merges.read_bytes()).hexdigest()
    assert vocab_sha == qb.QWEN_BPE_VOCAB_SHA256
    assert merges_sha == qb.QWEN_BPE_MERGES_SHA256
    # 组件串 = qwen_bpe_<hash8>（制品 sha256 派生，§2.7 实施期填实）
    # 冻结字面钉（W-R3b / R3-M7-c1，w-r3b-probes.json 实算）：原断言
    # QWEN_BPE_COMPONENT == f"qwen_bpe_{qwen_bpe_component_hash()}" 系
    # 模块常量 vs 其自身定义式（qwen_bpe.py:74，X==X）——制品指纹换钉
    # 重载时双侧同步漂移永不红；冻结字面对漂移咬。
    assert qb.QWEN_BPE_COMPONENT == "qwen_bpe_5dfeb309"
    assert qb.QWEN_BPE_COMPONENT.startswith("qwen_bpe_")
    tail = qb.QWEN_BPE_COMPONENT.removeprefix("qwen_bpe_")
    assert len(tail) == 8 and int(tail, 16) >= 0
    EmbeddingSpace(  # 组件正则兼容（[a-z0-9_]+）——构造不抛即证
        "text_embedding_v3", "n11_20260927", qb.QWEN_BPE_COMPONENT, "none",
        "COSINE", 1024, "plain_v1", "plain_v1", "tokens512_overlap64_bpe_v1",
        "p09_v1")


def test_tokenizer_load_fail_closed_on_artifact_tamper(tmp_path):
    # 制品篡改 → 加载 fail-closed（案 A：加载失败 fail-closed）
    shutil.copy(VENDOR / "qwen_bpe_vocab.json", tmp_path / "qwen_bpe_vocab.json")
    (tmp_path / "qwen_bpe_merges.txt").write_text("tampered\n", encoding="utf-8")
    with pytest.raises(qb.QwenBpeTokenizerLoadError):
        qb.QwenBpeTokenizer(artifact_dir=tmp_path)
    shutil.copy(VENDOR / "qwen_bpe_merges.txt", tmp_path / "qwen_bpe_merges.txt")
    (tmp_path / "qwen_bpe_vocab.json").write_text("{}", encoding="utf-8")
    with pytest.raises(qb.QwenBpeTokenizerLoadError):
        qb.QwenBpeTokenizer(artifact_dir=tmp_path)


def test_tokenizer_load_fail_closed_on_missing_artifact(tmp_path):
    with pytest.raises(qb.QwenBpeTokenizerLoadError):
        qb.QwenBpeTokenizer(artifact_dir=tmp_path)


# ---------- token_spans 分块合同（chunk_text 校验面全覆盖） ----------

@pytest.fixture(scope="module")
def tokenizer():
    return qb.QwenBpeTokenizer()


def _assert_full_cover(text, spans):
    assert spans, "spans must be non-empty for non-empty text"
    assert spans[0][0] == 0 and spans[-1][1] == len(text)
    for prev, cur in zip(spans, spans[1:]):
        assert prev[1] == cur[0] and prev[0] < prev[1]
    assert spans[-1][0] < spans[-1][1]


@pytest.mark.parametrize("text", [
    "中国人民银行宣布降准零点五个百分点。",
    "美联储宣布维持利率不变，市场反应平淡。",
    "The Federal Reserve held rates steady. 市场反应平淡。",
    "混合 half-width 标点, English words, 与数字 2026 年 09 月 29 日。",
    "多\n行\r\n文本\t与  连续  空白 \n\n",
    "尾部空白   ",
    "   头部空白",
    "emoji 😀 与符号 €￥",
    "a",
    "。",
])
def test_token_spans_full_cover_and_deterministic(tokenizer, text):
    spans = tokenizer.token_spans(text)
    _assert_full_cover(text, spans)
    assert tokenizer.token_spans(text) == spans  # 确定性
    # chunk_text 既有校验面不抛（spans 合法性 = 分块合同成立）
    chunks = chunk_text(text, tokenizer)
    assert chunks and all(isinstance(c, TextChunk) for c in chunks)


def test_known_single_token_pins(tokenizer):
    # Qwen2 vocab 已核单词元形（byte-level 编码后在册）
    for text in ("中国", "人民", "银行", "宣布", "美联储", "的", "了", "。"):
        spans = tokenizer.token_spans(text)
        assert spans == ((0, len(text)),), f"{text!r} 应为单 token：{spans}"


def test_known_multi_token_pin(tokenizer):
    assert tokenizer.token_spans("中国。") == ((0, 2), (2, 3))
    assert tokenizer.token_spans(" hello") == ((0, 6),)


def test_byte_level_no_oov(tokenizer):
    # byte-level BPE：任意 unicode 串均可切分（无 OOV），spans 全覆盖
    text = "𠮷野家\u200d🚀\x00\x01"
    spans = tokenizer.token_spans(text)
    _assert_full_cover(text, spans)


def test_count_tokens_matches_spans(tokenizer):
    text = "中国人民银行宣布降准零点五个百分点，释放长期资金约一万亿元。"
    assert qb.count_tokens(tokenizer, text) == len(tokenizer.token_spans(text))
    assert qb.count_tokens(tokenizer, "") == 0


# ---------- 分块新身份（chunking_version = tokens512_overlap64_bpe_v1） ----------

def test_real_chunk_text_new_identity_short_text(tokenizer):
    text = "中国人民银行宣布降准。"
    chunks = qb.real_chunk_text(text, tokenizer)
    assert len(chunks) == 1
    assert chunks[0].chunking_version == "tokens512_overlap64_bpe_v1"
    assert chunks[0].text == text and chunks[0].start == 0 and chunks[0].end == len(text)
    assert chunks[0].token_count == qb.count_tokens(tokenizer, text)


def test_real_chunk_text_empty_text_returns_empty(tokenizer):
    assert qb.real_chunk_text("", tokenizer) == ()
    assert qb.real_chunk_text("   \n\t ", tokenizer) == ()


def test_real_chunk_text_long_text_overlap_and_version(tokenizer):
    # 构造 >512 token 长文（重复一句 ~20 token 40 次 ≈ 800 token）
    sentence = "中国人民银行宣布降准零点五个百分点，释放长期资金约一万亿元。"
    text = sentence * 40
    chunks = qb.real_chunk_text(text, tokenizer)
    assert len(chunks) >= 2
    assert all(c.chunking_version == "tokens512_overlap64_bpe_v1" for c in chunks)
    assert all(c.token_count <= 512 for c in chunks)
    # 全文覆盖（逐 chunk 区间相邻）+ 重叠结构（后 chunk 起点 < 前 chunk 终点，末段除外）
    assert chunks[0].start == 0 and chunks[-1].end == len(text)
    for prev, cur in zip(chunks, chunks[1:]):
        assert cur.start < prev.end  # 64 token 重叠
        assert cur.chunk_id == prev.chunk_id + 1
    # 与码点存量轨结构性区分：同文 chunk_text(Codepoint) 版本串不同
    legacy = chunk_text(text, CodepointTokenizer())
    assert all(c.chunking_version == "tokens512_overlap64_v1" for c in legacy)


def test_real_chunking_version_constant():
    assert qb.REAL_CHUNKING_VERSION == "tokens512_overlap64_bpe_v1"


# ---------- ⑤ 真空间登记（§2.7 逐字段钉 + INV-4） ----------

def test_real_space_components_pinned():
    space = qb.real_embedding_space()
    assert space.model == "text_embedding_v3"          # 净化：排斥连字符
    assert space.revision == "n11_20260927"            # 钉准入批次（R1 锚）
    assert space.tokenizer == qb.QWEN_BPE_COMPONENT    # qwen_bpe_<hash8>
    assert space.pooling == "none"
    assert space.metric == "COSINE"
    assert space.dimension == 1024
    assert space.document_encoding == "plain_v1"       # 对称方案（root §6.4 L424）
    assert space.query_encoding == "plain_v1"
    assert space.chunking == "tokens512_overlap64_bpe_v1"
    assert space.preprocessing == "p09_v1"


def test_real_space_id_deterministic_and_frozen():
    assert qb.real_embedding_space().space_id == qb.real_embedding_space().space_id
    space = qb.real_embedding_space()
    assert space.space_id.startswith("s_") and len(space.space_id) == 42
    with pytest.raises(AttributeError):
        space.dimension = 512  # type: ignore[misc]
    other = EmbeddingSpace(
        "text_embedding_v4", space.revision, space.tokenizer, space.pooling,
        "COSINE", 1024, space.document_encoding, space.query_encoding,
        space.chunking, space.preprocessing)
    assert other.space_id != space.space_id  # 组件变更即新空间（INV-4 防混）


def test_real_space_differs_from_mock_space():
    mock = EmbeddingSpace("mock_embedding", "fixture_v1", "codepoint_v1", "none",
                          "COSINE", 4, "plain_v1", "plain_v1",
                          "tokens512_overlap64_v1", "p09_v1")
    assert qb.real_embedding_space().space_id != mock.space_id
