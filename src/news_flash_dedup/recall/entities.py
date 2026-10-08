"""P11 正文实体显式线索；未识别不代表正文没有实体。"""

from __future__ import annotations

import re
from dataclasses import dataclass


ENTITY_VERSION = "entity_v1"
_EIA = re.compile(r"(?<![A-Za-z0-9])EIA(?![A-Za-z0-9])", re.IGNORECASE)
_EIA_FULL = re.compile("美国能源信息署")
_CODE = re.compile(r"(?:证券|股票)?代码[\s:：]*([0-9]{6}(?:\.(?:SH|SZ))?)(?![0-9A-Za-z])",
                   re.IGNORECASE)
_ORGANIZATION = re.compile(
    r"[\u4e00-\u9fffA-Za-z]{1,20}?(?:股份有限公司|有限公司|公司|集团|银行|证券)"
)


@dataclass(frozen=True)
class EntityMention:
    entity_id: str
    raw: str
    start: int
    end: int


@dataclass(frozen=True)
class EntityExtraction:
    mentions: tuple[EntityMention, ...]
    # 只有 P14 经原文 Evidence 验证后才可填 True；词典无命中始终是未知。
    complete: bool = False

    @property
    def entity_ids(self) -> tuple[str, ...]:
        return tuple(sorted({mention.entity_id for mention in self.mentions}))


def extract_body_entities(text: str) -> EntityExtraction:
    if not isinstance(text, str):
        raise TypeError("text must be str")
    mentions: set[EntityMention] = set()

    def append(entity_id: str, start: int, end: int) -> None:
        mentions.add(EntityMention(f"{ENTITY_VERSION}|{entity_id}",
                                   text[start:end], start, end))

    for pattern in (_EIA, _EIA_FULL):
        for match in pattern.finditer(text):
            append("org:美国能源信息署", *match.span())
    for match in _CODE.finditer(text):
        code = match.group(1).upper()
        append("code:" + code, *match.span(1))
    for match in _ORGANIZATION.finditer(text):
        append("org:" + match.group(), *match.span())
    return EntityExtraction(tuple(sorted(mentions,
                                         key=lambda item: (item.start, item.end,
                                                           item.entity_id))))
