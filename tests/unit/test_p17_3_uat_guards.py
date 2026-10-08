"""窗口K（D30 H-07-a）配套单测：94 索引守护对拍 helper 的红/绿能力。

集成文件 tests/integration/test_p17_3_uat.py 受 P17_CONFIRM_UAT 门控
（默认 skip，禁连真 ES）；其 94 保留索引对拍 helper
`_assert_preserved_indices_untouched` 的语义在此以纯数据驱动钉死：
- 绿：快照一致 / 仅 P17 隔离前缀差异 → 通过；
- 红：注入外来索引 / 缺失保留索引 → AssertionError（旧断言 X==X 永无此能力）。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_MODULE_PATH = (Path(__file__).resolve().parents[1]
                / "integration" / "test_p17_3_uat.py")
_spec = importlib.util.spec_from_file_location("test_p17_3_uat", _MODULE_PATH)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

_assert_untouched = _mod._assert_preserved_indices_untouched

_P17_PREFIX = "p17-batch-smoke-news-dedup-"


def test_identical_snapshots_pass():
    before = ["idx-a", "idx-b", "idx-c"]
    _assert_untouched(before, list(before), _P17_PREFIX)


def test_p17_isolation_indices_excluded():
    """P17 隔离前缀索引的出现/消失不计入 94 保留清单。"""
    before = ["idx-a", "idx-b"]
    after = ["idx-a", "idx-b", "p17-batch-smoke-news-dedup-items-v1-2026.09.26"]
    _assert_untouched(before, after, _P17_PREFIX)


def test_injected_foreign_index_goes_red():
    """红：快照多出一个非 P17 外来索引 → 必须打红。"""
    before = ["idx-a", "idx-b"]
    after = ["idx-a", "idx-b", "injected-foreign-index"]
    with pytest.raises(AssertionError, match="94 保留索引被改动"):
        _assert_untouched(before, after, _P17_PREFIX)


def test_removed_preserved_index_goes_red():
    """红：保留索引被删（快照缺失）→ 必须打红。"""
    before = ["idx-a", "idx-b"]
    after = ["idx-a"]
    with pytest.raises(AssertionError, match="94 保留索引被改动"):
        _assert_untouched(before, after, _P17_PREFIX)


@pytest.mark.parametrize("corrupted", [
    [],
    ["completely-different"],
    ["idx-a", "idx-b", "extra"],
])
def test_old_assertion_form_is_tautological(corrupted):
    """旧断言形态实证：`snapshot == snapshot` 对任意（含损坏）输入恒真。

    此即 H-07-a 清算动机——旧形态永不接触独立重取的 after，无红能力；
    本参数化钉死"自比恒真"这一事实，防未来回退为该形态。

    # tautology-monument（W2Fε 标注，WC2 裁定取一）：下方自比断言系有意
    # 恒真纪念碑——它证明的是"旧形态无红能力"这一元事实，自身不承担红能力；
    # 现役红能力由本文件 test_injected_foreign/removed_preserved 等独立重取
    # 对拍钉承担。勿以"恒真"为由删除本钉；删除=纪念碑灭失、回退防线开口。
    """
    assert corrupted == corrupted  # 恒真：旧形态对任何损坏均无红能力（tautology-monument）
