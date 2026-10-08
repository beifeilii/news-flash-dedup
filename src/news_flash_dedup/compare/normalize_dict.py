"""对齐层词典归一化：版本化 NormalizationDictionary 装载与查询（D19/S2 §四）。

词典为纯数据 JSON（`data/dictionaries/norm_dict_v*.json`），词条结构
`canonical ← variants[]`、逐条稳定 `id`。装载期五项 fail-closed 校验
（歧义/链式/空串重复/directional/NFC），校验不过即
`NormalizationDictionaryError`（ValueError 子类），绝不退化为无词典硬跑。

设计依据：`log/temp/s2-normalize-hook-proposal.md` §四；`log/D19-词典归一化设计冻结.md`。
"""

from __future__ import annotations

import json
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType
from typing import Mapping


class NormalizationDictionaryError(ValueError):
    """词典文件结构/一致性校验失败（fail-closed）。"""


@dataclass(frozen=True)
class NormalizationHit:
    """单槽归一化命中留痕（审计链引用 entry_id）。"""

    slot: str            # "subject" | "predicate"（key_object 一期不参与归一）
    history_raw: str
    current_raw: str
    canonical: str
    entry_id: str


def _nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


@dataclass(frozen=True)
class NormalizationDictionary:
    """冻结词典对象：版本/日期 + 分 section 的 variant→(canonical, entry_id) 反向索引。"""

    version: str
    date: str | None
    _index: Mapping[str, Mapping[str, tuple[str, str]]]  # section -> variant -> (canonical, entry_id)

    def normalize(self, slot_kind: str, raw: str) -> tuple[str, str] | None:
        """查 slot_kind 对应 section 的反向索引；命中返回 (canonical, entry_id)，未命中 None。

        raw 先经 NFC 归一（与装载期索引口径一致）；raw 本身即 canonical 时同样命中
        （调用方以"双侧 canonical 相等且至少一侧实际改写"判定 Tier-2 命中）。
        """
        if not isinstance(raw, str) or not raw:
            return None
        bucket = self._index.get(slot_kind)
        if bucket is None:
            return None
        return bucket.get(_nfc(raw))

    # S3 测试契约别名（与 normalize 同义）。
    def canonicalize(self, slot_kind: str, raw: str) -> tuple[str, str] | None:
        return self.normalize(slot_kind, raw)


def _validate_section(name: str, section: object) -> Mapping[str, tuple[str, str]]:
    if not isinstance(section, dict):
        raise NormalizationDictionaryError(f"sections.{name} must be an object")
    if section.get("directional") not in (False, None):
        raise NormalizationDictionaryError(
            f"sections.{name}.directional must be false (一期无方向性词条)"
        )
    entries = section.get("entries")
    if not isinstance(entries, list):
        raise NormalizationDictionaryError(f"sections.{name}.entries must be a list")
    index: dict[str, tuple[str, str]] = {}
    canonicals: set[str] = set()
    seen_ids: set[str] = set()
    for position, entry in enumerate(entries):
        where = f"sections.{name}.entries[{position}]"
        if not isinstance(entry, dict):
            raise NormalizationDictionaryError(f"{where} must be an object")
        entry_id = entry.get("id")
        canonical = entry.get("canonical")
        variants = entry.get("variants")
        if not isinstance(entry_id, str) or not entry_id:
            raise NormalizationDictionaryError(f"{where}.id must be a nonempty string")
        if entry_id in seen_ids:
            raise NormalizationDictionaryError(f"{where}.id {entry_id!r} duplicated")
        seen_ids.add(entry_id)
        if not isinstance(canonical, str) or not canonical:
            raise NormalizationDictionaryError(f"{where}.canonical must be a nonempty string")
        if not isinstance(variants, list) or not variants:
            raise NormalizationDictionaryError(f"{where}.variants must be a nonempty list")
        canonical = _nfc(canonical)
        norm_variants: list[str] = []
        for variant in variants:
            if not isinstance(variant, str) or not variant:
                raise NormalizationDictionaryError(f"{where}.variants contains empty variant")
            norm_variants.append(_nfc(variant))
        if len(set(norm_variants)) != len(norm_variants):
            raise NormalizationDictionaryError(f"{where}.variants duplicated after NFC")
        if canonical in norm_variants:
            raise NormalizationDictionaryError(
                f"{where}.canonical {canonical!r} also listed as its own variant"
            )
        for variant in norm_variants:
            if variant in index:
                raise NormalizationDictionaryError(
                    f"{where}.variant {variant!r} ambiguous: already maps to "
                    f"{index[variant][0]!r}"
                )
            index[variant] = (canonical, entry_id)
        canonicals.add(canonical)
    # 链式/环：任何 canonical 不得本身是别的 entry 的 variant。
    for variant, (canonical, _entry_id) in index.items():
        if variant in canonicals:
            raise NormalizationDictionaryError(
                f"sections.{name}: chained normalization rejected — {variant!r} is both "
                f"a variant and another entry's canonical"
            )
    return MappingProxyType(index)


def _load_uncached(path: str) -> NormalizationDictionary:
    try:
        raw_text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise NormalizationDictionaryError(
            f"dictionary file unreadable: {path!r} ({exc})"
        ) from exc
    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise NormalizationDictionaryError(f"dictionary JSON invalid: {exc}") from exc
    if not isinstance(data, dict):
        raise NormalizationDictionaryError("dictionary root must be an object")
    version = data.get("version")
    if not isinstance(version, str) or not version:
        raise NormalizationDictionaryError("dictionary.version must be a nonempty string")
    date = data.get("date")
    if date is not None and (not isinstance(date, str) or not date):
        raise NormalizationDictionaryError("dictionary.date must be a nonempty string or null")
    sections = data.get("sections")
    if not isinstance(sections, dict):
        raise NormalizationDictionaryError("dictionary.sections must be an object")
    index: dict[str, Mapping[str, tuple[str, str]]] = {}
    for name, section in sections.items():
        if not isinstance(name, str) or not name:
            raise NormalizationDictionaryError("section name must be a nonempty string")
        index[name] = _validate_section(name, section)
    return NormalizationDictionary(
        version=version,
        date=date,
        _index=MappingProxyType(index),
    )


@lru_cache(maxsize=None)
def _load_cached(abs_path: str) -> NormalizationDictionary:
    return _load_uncached(abs_path)


def load_dictionary(path: str | Path) -> NormalizationDictionary:
    """按绝对路径装载并缓存冻结词典；文件缺失/损坏/校验失败即异常（fail-closed）。"""
    abs_path = str(Path(path).resolve())
    return _load_cached(abs_path)


def clear_cache() -> None:
    """清空装载缓存（测试隔离用）。"""
    _load_cached.cache_clear()


__all__ = [
    "NormalizationDictionary",
    "NormalizationDictionaryError",
    "NormalizationHit",
    "clear_cache",
    "load_dictionary",
]
