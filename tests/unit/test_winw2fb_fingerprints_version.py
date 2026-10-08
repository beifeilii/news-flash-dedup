# -*- coding: utf-8 -*-
"""窗口W2Fβ 条10+条11：指纹死字段清理钉 + ENTITY_VERSION 双源关系钉。

条10（WA3a-F2）：Fingerprints.normalizer_version 只写不读（索引侧
normalizer_version 由 prepare_recall_fields 从 NormalizationResult 单源写入），
清理后实例不再携带该死字段。
条11（WA3a-F4）：entities.ENTITY_VERSION（词典/抽取版本，entity_ids 前缀）与
entity_channel.ENTITY_QUERY_VERSION（通道 query_version）同值双定义——注释钉死
双源关系，本钉强制两源恒等（任一升版必须同步评估另一源）。
"""
from __future__ import annotations

import dataclasses

from news_flash_dedup.recall.entities import ENTITY_VERSION, extract_body_entities
from news_flash_dedup.recall.entity_channel import ENTITY_QUERY_VERSION
from news_flash_dedup.recall.fingerprints import build_fingerprints


def test_fingerprints_carries_no_dead_normalizer_version():
    """红能力（条10）：死字段已清理——实例与字段表均无 normalizer_version。"""
    result = build_fingerprints("甲公司完成回购")
    assert result is not None
    assert not hasattr(result, "normalizer_version")
    assert "normalizer_version" not in {
        field.name for field in dataclasses.fields(result)}


def test_fingerprints_live_fields_intact_after_cleanup():
    """绿守卫（条10）：清理不动活字段——确定性相等与指纹面不变。"""
    assert build_fingerprints("甲公司\r\n增持") == build_fingerprints("甲公司\n增持")
    result = build_fingerprints("甲公司完成股份回购，金额为3亿元。")
    assert len(result.minhash_signature) == 128
    assert len(result.simhash_bands) == 4
    assert len(result.minhash_bands) == 32


def test_entity_version_dual_sources_are_pinned_equal():
    """钉（条11）：两源同值受测钉强制；entity_ids 前缀与词典版本一致。"""
    assert ENTITY_QUERY_VERSION == ENTITY_VERSION
    extraction = extract_body_entities("EIA公布石油库存")
    assert extraction.mentions
    assert all(mention.entity_id.startswith(ENTITY_VERSION + "|")
               for mention in extraction.mentions)
