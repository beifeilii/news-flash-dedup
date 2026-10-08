# -*- coding: utf-8 -*-
"""W3F 二轮红测（W3c-F-2）：milvus_store._ensure_collection 分区创建 TOCTOU。

缺陷面（vector/milvus_store.py:198-201 现役）：条66② 已为
_ensure_collection（:162-170 create 抛错复检 has_collection）与
_ensure_indices（:208-214 同形）补竞态复检，同函数尾部
`if partition not in list_partitions(...): create_partition(...)` 未覆盖
——检查→创建间并发双窗同键撞分区时 create_partition 裸异常逃逸
（不包 VectorWriteUnknown、不并入已存在语义）。

修法（任务书对码区 W3c-2"直接尝试+已存在并入语义"向，对准 66②
"集合已存在→复用"先例）：create 抛错 → 复检 list_partitions——在场
即并入复用；仍不在场 = 创建确认未知（fail-closed VectorWriteUnknown）。
"""

from __future__ import annotations

import inspect

import pytest

from news_flash_dedup.recall.vector_store import VectorWriteUnknown
from news_flash_dedup.vector import milvus_store as milvus_store_module
from news_flash_dedup.vector.milvus_store import RealMilvusP19Store

from tests.unit.test_winw2fb_wa3b_hygiene import (
    _bare_store,
    _wellformed_description,
)

PARTITION = "bd_20260926"     # partition_name(config.business_date.isoformat())


class _AdoptedCollectionBase:
    """集合侧走 adopt 快线（has_collection=True + 良构 describe），
    焦点隔离到分区段。"""

    def has_collection(self, *, collection_name):
        return True

    def describe_collection(self, *, collection_name):
        return _wellformed_description()

    # E 批 M-09 采用闸配套：良构索引证据（FLAT/COSINE embedding）。
    def list_indexes(self, *, collection_name):
        return ["embedding"]

    def describe_index(self, *, collection_name, index_name):
        return {"index_type": "FLAT", "metric_type": "COSINE",
                "field_name": "embedding"}


class _RacePartitionWinMilvus(_AdoptedCollectionBase):
    """分区竞态并发者胜：预检缺席（[]）→ create 撞键抛错，但复检已在场
    （并发实例在检查→创建窗口内先建同键分区）。"""

    def __init__(self):
        self.list_calls = 0
        self.create_calls = 0

    def list_partitions(self, *, collection_name):
        self.list_calls += 1
        return [] if self.list_calls == 1 else [PARTITION]

    def create_partition(self, *, collection_name, partition_name):
        self.create_calls += 1
        raise RuntimeError("partition already exists")


class _RacePartitionLostMilvus(_AdoptedCollectionBase):
    """create 抛错且复检仍缺席（创建确认未知面）。"""

    def __init__(self):
        self.create_calls = 0

    def list_partitions(self, *, collection_name):
        return []

    def create_partition(self, *, collection_name, partition_name):
        self.create_calls += 1
        raise RuntimeError("boom-create")


class _PartitionNormalMilvus(_AdoptedCollectionBase):
    """常态两路记录仪：已建分区复用 / 缺席新建成功。"""

    def __init__(self, existing=()):
        self._existing = list(existing)
        self.create_calls = []

    def list_partitions(self, *, collection_name):
        return list(self._existing)

    def create_partition(self, *, collection_name, partition_name):
        self.create_calls.append(partition_name)
        self._existing.append(partition_name)


# ---------- 红测（修复前竞态裸异常逃逸） ----------

def test_f2_partition_race_merge_not_bare_escape():
    """红能力：检查→创建窗口内并发者先建同键分区——修复前 create 裸
    RuntimeError 逃逸（__init__ 链白崩）；修复后复检在场并入复用
    （"集合已存在→复用"先例同向），_ensure_collection 正常返回。"""
    milvus = _RacePartitionWinMilvus()
    _bare_store(milvus=milvus)._ensure_collection()        # 不抛即并入成功
    assert milvus.create_calls == 1                        # 直接尝试已发生
    assert milvus.list_calls >= 2                          # 抛错后复检在场


def test_f2_partition_create_failure_still_absent_is_unknown():
    """红能力：create 抛错且复检仍缺席——修复前裸 RuntimeError 逃逸
    （确认未知被错误类型掩盖）；修复后 VectorWriteUnknown（fail-closed，
    from error 保链，与 collection/indices 错误分类口径一致）。"""
    milvus = _RacePartitionLostMilvus()
    with pytest.raises(VectorWriteUnknown,
                       match="partition creation confirmation is unknown"):
        _bare_store(milvus=milvus)._ensure_collection()
    assert milvus.create_calls == 1


def test_f2_partition_recheck_wrapped_in_source():
    """机制钉（源码面）：_ensure_collection 分区段携带
    "partition creation confirmation is unknown" 复检包装（66② 同形）。"""
    src = inspect.getsource(RealMilvusP19Store._ensure_collection)
    assert "partition creation confirmation is unknown" in src, \
        "分区 create 竞态复检包装未落地（66② 同族残余仍在）"
    assert milvus_store_module is not None


# ---------- 前后均绿守卫（常态路径逐字节不变） ----------

def test_f2_partition_already_present_skips_create_unchanged():
    """守卫（前后同绿）：分区预检在场 → 不再 create（adopt 快线不动）。"""
    milvus = _PartitionNormalMilvus(existing=[PARTITION])
    _bare_store(milvus=milvus)._ensure_collection()
    assert milvus.create_calls == []


def test_f2_partition_absent_create_success_unchanged():
    """守卫（前后同绿）：分区缺席 + create 成功 → 新建落位（常态路径不动）。"""
    milvus = _PartitionNormalMilvus(existing=[])
    _bare_store(milvus=milvus)._ensure_collection()
    assert milvus.create_calls == [PARTITION]
    assert PARTITION in milvus.list_partitions(collection_name="c")
