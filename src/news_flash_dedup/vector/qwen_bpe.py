"""B5/E2 S2 案 A：vendored Qwen 系 BPE tokenizer + 真空间登记（设计 §2.6/§2.7）。

落位说明（施工纪律记录）：设计 §3.3-T3 建议「recall/vector_space.py 追加」；
施工令钉死既有文件改动特许面 = recall/vector_store.py 闸扩展一处、其余既有面
零触——两约束的兼容解 = 案 A 全件落本新件（vector/* 本片文件面内），
recall/vector_space.py 的 CodepointTokenizer/chunk_text 一字未动（案 B 存量轨
原样保留，errata 登记不变）。

制品（vendored，sha256 钉死；来源 ModelScope Qwen/Qwen2-7B-Instruct 仓，
与 huggingface 同名件逐字节同族——tokenizer.json 内含 vocab 与本件 vocab.json
全等实测在案）：
- vector/vendor/qwen_bpe_vocab.json   151,643 词元（byte-level 编码形）
- vector/vendor/qwen_bpe_merges.txt   151,387 合并规则（无 #version 头行）
加载期重算 sha256 对拍钉值，不符/缺失 → QwenBpeTokenizerLoadError（fail-closed，
案 A：加载失败 fail-closed；禁退案 B 上新链路）。

分词管线（与 Qwen2 tokenizer.json 逐项对拍）：
1. pre-tokenizer Split Regex（Isolated）：
   (?i:'s|'t|'re|'ve|'m|'ll|'d)|[^\\r\\n\\p{L}\\p{N}]?\\p{L}+|\\p{N}|
    ?[^\\s\\p{L}\\p{N}]+[\\r\\n]*|\\s*[\\r\\n]+|\\s+(?!\\S)|\\s+
   纯 Python 扫描器复刻（stdlib re 无 \\p{L}/\\p{N}；\\p{L}/\\p{N} 以
   unicodedata.category 精确类别执法——'四' 等 CJK 数字类属 Lo 非 N，
   isnumeric() 会误判，禁用）；
2. ByteLevel：GPT-2 式 bytes→unicode 映射（add_prefix_space=false）；
3. BPE 合并按 rank 序；byte-level 无 OOV；
4. token 边界回填为原文字符区间（spans）；跨码点边界（理论罕见）按
   「共享码点并入前段」确定性重对齐并计数（split_codepoint_realignments
   证据钩），保证 chunk_text 校验面（vector_space.py:120-127）成立。

校准 oracle（§2.6）：count_tokens 本地计数 vs 服务端 usage.total_tokens 逐批
对拍——harness = log/temp/p19-s2-usage-oracle-calibration.py；容差带评审钉死。

真空间（§2.7）：real_embedding_space() 组件逐字段钉死；chunking 新身份 =
tokens512_overlap64_bpe_v1（与存量码点轨 tokens512_overlap64_v1 结构性区分）。
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import unicodedata
from pathlib import Path
from typing import Sequence

from news_flash_dedup.recall.vector_space import (
    EmbeddingSpace,
    TextChunk,
    chunk_text,
)


_ARTIFACT_DIR = Path(__file__).resolve().parent / "vendor"

# 制品 sha256 钉（2026-09-29 Get-FileHash 实测；篡改即 fail-closed）
QWEN_BPE_VOCAB_SHA256 = (
    "ca10d7e9fb3ed18575dd1e277a2579c16d108e32f27439684afa0e10b1440910")
QWEN_BPE_MERGES_SHA256 = (
    "599bab54075088774b1733fde865d5bd747cbcc7a547c5bc12610e874e26f5e3")

REAL_CHUNKING_VERSION = "tokens512_overlap64_bpe_v1"

# 英文缩约尾（pre-tokenizer 第一备选，(?i) 大小写不敏感）
_CONTRACTIONS_2 = frozenset({"'s", "'t", "'m", "'d"})
_CONTRACTIONS_3 = frozenset({"'re", "'ve", "'ll"})


def qwen_bpe_component_hash() -> str:
    """制品组合指纹 hash8 = sha256(vocab_sha256 + ":" + merges_sha256)[:8]。"""
    material = f"{QWEN_BPE_VOCAB_SHA256}:{QWEN_BPE_MERGES_SHA256}"
    return hashlib.sha256(material.encode("ascii")).hexdigest()[:8]


QWEN_BPE_COMPONENT = f"qwen_bpe_{qwen_bpe_component_hash()}"


class QwenBpeTokenizerLoadError(RuntimeError):
    """vendored 制品缺失 / sha256 与钉值不符 / 形态畸形——加载 fail-closed（案 A）。"""


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _bytes_to_unicode() -> dict[int, str]:
    """GPT-2 式 byte→unicode 双射（Qwen2 tokenizer.json ByteLevel 同款）。"""
    printable = (list(range(ord("!"), ord("~") + 1))
                 + list(range(0xA1, 0xAC + 1)) + list(range(0xAE, 0xFF + 1)))
    codes = list(printable)
    extra = 0
    for byte in range(256):
        if byte not in printable:
            printable.append(byte)
            codes.append(256 + extra)
            extra += 1
    return dict(zip(printable, map(chr, codes)))


_BYTE_TO_UNICODE = _bytes_to_unicode()


def _is_letter(ch: str) -> bool:
    return unicodedata.category(ch)[0] == "L"      # \p{L} 精确类别（含 CJK Lo）


def _is_number(ch: str) -> bool:
    return unicodedata.category(ch)[0] == "N"      # \p{N}（'四' 属 Lo 不在此）


def _is_whitespace(ch: str) -> bool:
    # Unicode White_Space 属性：Zs/Zl/Zp + \t\n\v\f\r + U+0085
    return (ch in "\t\n\v\f\r\x85"
            or unicodedata.category(ch) in ("Zs", "Zl", "Zp"))


def _is_symbol(ch: str) -> bool:
    return not (_is_whitespace(ch) or _is_letter(ch) or _is_number(ch))


def _pre_tokenize(text: str) -> list[tuple[int, int]]:
    """Qwen2 Split Regex 逐备选复刻（有序、左倾、内部贪婪）。"""
    spans: list[tuple[int, int]] = []
    n = len(text)
    i = 0
    while i < n:
        ch = text[i]
        # 1. (?i:'s|'t|'re|'ve|'m|'ll|'d)
        if ch == "'":
            two = text[i:i + 2].lower()
            three = text[i:i + 3].lower()
            if three in _CONTRACTIONS_3:
                spans.append((i, i + 3))
                i += 3
                continue
            if two in _CONTRACTIONS_2:
                spans.append((i, i + 2))
                i += 2
                continue
        # 2. [^\r\n\p{L}\p{N}]?\p{L}+
        if _is_letter(ch):
            j = i + 1
            while j < n and _is_letter(text[j]):
                j += 1
            spans.append((i, j))
            i = j
            continue
        if (ch not in "\r\n" and not _is_number(ch)
                and i + 1 < n and _is_letter(text[i + 1])):
            j = i + 1
            while j < n and _is_letter(text[j]):
                j += 1
            spans.append((i, j))
            i = j
            continue
        # 3. \p{N}（单字符）
        if _is_number(ch):
            spans.append((i, i + 1))
            i += 1
            continue
        # 4. [空格]?[^\s\p{L}\p{N}]+[\r\n]*
        j = i + 1 if ch == " " else i
        if j < n and _is_symbol(text[j]):
            k = j
            while k < n and _is_symbol(text[k]):
                k += 1
            while k < n and text[k] in "\r\n":
                k += 1
            spans.append((i, k))
            i = k
            continue
        # 5./6./7. 空白族（run 先行）
        if _is_whitespace(ch):
            k = i
            while k < n and _is_whitespace(text[k]):
                k += 1
            # 5. \s*[\r\n]+：run 内含 \r\n → 止于 run 内最后一个 \r\n 之后
            last_crlf = -1
            for p in range(i, k):
                if text[p] in "\r\n":
                    last_crlf = p
            if last_crlf >= 0:
                spans.append((i, last_crlf + 1))
                i = last_crlf + 1
                continue
            # 6. \s+(?!\S)：run 后非 \S（此处 run 极大 ⇒ 后随 \S 或文末）
            if k == n:
                spans.append((i, k))
                i = k
                continue
            if k - i >= 2:
                spans.append((i, k - 1))
                i = k - 1
                continue
            # 7. \s+
            spans.append((i, k))
            i = k
            continue
        # 结构性不可达：每字符必属 L/N/空白/符号其一
        raise AssertionError(f"pretokenizer stuck at {i}: {text[i]!r}")
    return spans


class QwenBpeTokenizer:
    """vendored Qwen 系 BPE tokenizer（案 A；Tokenizer 协议 token_spans）。

    加载期 sha256 对拍钉值，不符即 QwenBpeTokenizerLoadError（fail-closed）。
    """

    def __init__(self, artifact_dir: Path | None = None) -> None:
        directory = Path(artifact_dir) if artifact_dir is not None else _ARTIFACT_DIR
        vocab_path = directory / "qwen_bpe_vocab.json"
        merges_path = directory / "qwen_bpe_merges.txt"
        try:
            vocab_sha = _sha256_file(vocab_path)
            merges_sha = _sha256_file(merges_path)
        except OSError as error:
            raise QwenBpeTokenizerLoadError(
                f"vendored artifact unreadable: {error}") from error
        if vocab_sha != QWEN_BPE_VOCAB_SHA256:
            raise QwenBpeTokenizerLoadError(
                f"vocab sha256 differs from pinned value: {vocab_sha}")
        if merges_sha != QWEN_BPE_MERGES_SHA256:
            raise QwenBpeTokenizerLoadError(
                f"merges sha256 differs from pinned value: {merges_sha}")
        try:
            raw_merges = merges_path.read_text(encoding="utf-8").splitlines()
            ranks: dict[tuple[str, str], int] = {}
            for line in raw_merges:
                if not line or line.startswith("#"):
                    continue
                left, sep, right = line.partition(" ")
                if not sep or not left or not right:
                    raise ValueError(f"malformed merge line: {line!r}")
                ranks[(left, right)] = len(ranks)
            vocab = json.loads(vocab_path.read_text(encoding="utf-8"))
            if not isinstance(vocab, dict) or len(vocab) != 151643:
                raise ValueError("vocab shape differs from Qwen2 BPE")
        except (ValueError, UnicodeDecodeError) as error:
            raise QwenBpeTokenizerLoadError(
                f"vendored artifact malformed: {error}") from error
        self._merge_ranks = ranks
        self._vocab = vocab
        # 跨码点重对齐证据钩（理论罕见；校准报告随附）
        self.split_codepoint_realignments = 0

    # ---------- BPE ----------

    def _bpe_pieces(self, word: str) -> list[str]:
        encoded = "".join(_BYTE_TO_UNICODE[b] for b in word.encode("utf-8"))
        symbols = list(encoded)
        while len(symbols) >= 2:
            best_rank: int | None = None
            best_pair: tuple[str, str] | None = None
            for index in range(len(symbols) - 1):
                rank = self._merge_ranks.get((symbols[index], symbols[index + 1]))
                if rank is not None and (best_rank is None or rank < best_rank):
                    best_rank = rank
                    best_pair = (symbols[index], symbols[index + 1])
            if best_pair is None:
                break
            merged = best_pair[0] + best_pair[1]
            out: list[str] = []
            index = 0
            while index < len(symbols):
                if (index < len(symbols) - 1
                        and (symbols[index], symbols[index + 1]) == best_pair):
                    out.append(merged)
                    index += 2
                else:
                    out.append(symbols[index])
                    index += 1
            symbols = out
        return symbols

    # ---------- Tokenizer 协议 ----------

    def token_spans(self, text: str) -> tuple[tuple[int, int], ...]:
        """原文字符区间序列（全覆盖/相邻/非空——chunk_text 校验面合同）。"""
        if not text:
            return ()
        utf8 = text.encode("utf-8")
        # 字节偏移 → 字符索引 + 码点边界旗标
        char_of_byte = [0] * (len(utf8) + 1)
        boundary = [True] * (len(utf8) + 1)
        position = 0
        for char_index, char in enumerate(text):
            width = len(char.encode("utf-8"))
            for offset in range(position, position + width):
                char_of_byte[offset] = char_index
            for offset in range(position + 1, position + width):
                boundary[offset] = False
            position += width
        char_of_byte[len(utf8)] = len(text)

        spans: list[tuple[int, int]] = []
        open_start = -1
        open_end = -1
        for word_start, word_end in _pre_tokenize(text):
            byte_base = len(text[:word_start].encode("utf-8"))
            cursor = byte_base
            for piece in self._bpe_pieces(text[word_start:word_end]):
                byte_start, byte_end = cursor, cursor + len(piece)
                cursor = byte_end
                span_start = char_of_byte[byte_start]
                span_end = (char_of_byte[byte_end] if boundary[byte_end]
                            else char_of_byte[byte_end] + 1)
                if open_start >= 0 and span_start < open_end:
                    # 跨码点：共享码点并入前段（确定性重对齐）
                    open_end = max(open_end, span_end)
                    self.split_codepoint_realignments += 1
                else:
                    if open_start >= 0:
                        spans.append((open_start, open_end))
                    open_start, open_end = span_start, span_end
        if open_start >= 0:
            spans.append((open_start, open_end))
        return tuple(spans)


def count_tokens(tokenizer: QwenBpeTokenizer, text: str) -> int:
    """本地 token 计数（§2.6 校准 oracle 本地侧）。"""
    return len(tokenizer.token_spans(text))


def real_chunk_text(text: str, tokenizer: QwenBpeTokenizer) -> tuple[TextChunk, ...]:
    """真轨分块：复用 chunk_text 512/64 句界逻辑，chunking_version 新身份。

    与存量码点轨 tokens512_overlap64_v1 结构性区分（TextChunk.chunking_version
    字段承载，vector_space.py:112）；chunk 原文区间不动，6000 字护栏在客户端
    （§3.2：截断仅作用嵌入输入）。
    """
    return tuple(
        dataclasses.replace(chunk, chunking_version=REAL_CHUNKING_VERSION)
        for chunk in chunk_text(text, tokenizer)
    )


def real_embedding_space() -> EmbeddingSpace:
    """真空间登记（§2.7 逐字段钉；space_id 由配置摘要派生，INV-4 地基）。"""
    return EmbeddingSpace(
        model="text_embedding_v3",            # 净化：组件正则排斥连字符
        revision="n11_20260927",              # 钉准入批次（R1 锚）
        tokenizer=QWEN_BPE_COMPONENT,         # qwen_bpe_<hash8>（制品 sha256 钉）
        pooling="none",                       # 托管端内建，无本地 pooling
        metric="COSINE",
        dimension=1024,
        document_encoding="plain_v1",         # 对称方案（root 09 §6.4 L424）
        query_encoding="plain_v1",
        chunking=REAL_CHUNKING_VERSION,       # 案 A 新身份（R105）
        preprocessing="p09_v1",
    )


__all__ = [
    "QWEN_BPE_COMPONENT",
    "QWEN_BPE_MERGES_SHA256",
    "QWEN_BPE_VOCAB_SHA256",
    "QwenBpeTokenizer",
    "QwenBpeTokenizerLoadError",
    "REAL_CHUNKING_VERSION",
    "count_tokens",
    "qwen_bpe_component_hash",
    "real_chunk_text",
    "real_embedding_space",
]
