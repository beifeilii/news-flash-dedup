"""W2Fδ 修复窗口红测：条81（θ4 采纳——P05 manifest 预防型封存，只读属性置位）。

只读属性置位由修复窗执行（文件系统属性，文件本体内容禁改）；本测试钉住该属性防回退。
内容同一性由 test_p23_uat.py INV-1（dataset_version 逐字节复算钉）在案，本文件不重复钉。
只新建不改既有套件。
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

MANIFEST = (Path(__file__).resolve().parents[3] / "log"
            / "P05-历史金标初版manifest.json")


def test_p05_manifest_exists_at_frozen_location():
    assert MANIFEST.is_file(), f"金标 manifest 缺位：{MANIFEST}"


@pytest.mark.skipif(not hasattr(os.stat_result, "st_file_attributes"),
                    reason="st_file_attributes 仅 Windows 提供")
def test_p05_manifest_is_marked_read_only():
    """θ4：冻结资产加固——文件只读属性必须置位（防误改冻结工件）。"""
    attributes = os.stat(MANIFEST).st_file_attributes
    assert attributes & stat.FILE_ATTRIBUTE_READONLY, (
        "P05 manifest 只读属性未置位（W2 批81 要求置位并钉测）")
