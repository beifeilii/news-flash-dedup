"""P11 正文实体词项只从原文显式片段提取。"""

from __future__ import annotations

from news_flash_dedup.recall.entities import extract_body_entities


def test_eia_aliases_share_one_versioned_entity_id_and_original_spans():
    text = "EIA公布数据；美国能源信息署随后修正。"
    extraction = extract_body_entities(text)
    ids = {mention.entity_id for mention in extraction.mentions}
    assert ids == {"entity_v1|org:美国能源信息署"}
    assert {text[mention.start:mention.end] for mention in extraction.mentions} == {
        "EIA", "美国能源信息署"}
    assert extraction.complete is False


def test_stock_code_preserves_leading_and_trailing_zero_and_exchange():
    text = "证券代码：000010.SZ 今日上涨；代码 000001 未变。"
    extraction = extract_body_entities(text)
    ids = {mention.entity_id for mention in extraction.mentions}
    assert "entity_v1|code:000010.SZ" in ids
    assert "entity_v1|code:000001" in ids
    assert "entity_v1|code:00001" not in ids
    assert all(text[mention.start:mention.end] == mention.raw
               for mention in extraction.mentions)


def test_group_and_subsidiary_are_not_collapsed():
    parent = extract_body_entities("甲集团发布公告")
    child = extract_body_entities("甲公司发布公告")
    parent_ids = {mention.entity_id for mention in parent.mentions}
    child_ids = {mention.entity_id for mention in child.mentions}
    assert "entity_v1|org:甲集团" in parent_ids
    assert "entity_v1|org:甲公司" in child_ids
    assert parent_ids.isdisjoint(child_ids)


def test_no_recognized_entity_is_uncertain_not_proven_absent():
    extraction = extract_body_entities("今日有新进展")
    assert extraction.mentions == ()
    assert extraction.complete is False
