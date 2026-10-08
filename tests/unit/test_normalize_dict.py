"""D19 杠杆 b · 词典装载（compare/normalize_dict.py）契约单元测试。

D19 契约测试，src 实现主窗口进行中，当前红属预期（本文件只新增，不改 src/既有测试）。
契约来源：`log/temp/s2-normalize-hook-proposal.md` §4（词典契约）+ `log/D19-词典归一化
设计冻结.md` §二-杠杆 b-3，逐条对应任务条款 5：

- 合法 JSON → frozen NormalizationDictionary；
- 五类 fail-closed 各一例 → ValueError：
  ① 歧义 variant 映射两 canonical；② 链式 A→B,B→C；③ 空串；
  ④ directional 反向已存在；⑤ NFC 不同形（归并后撞车）；
- 缓存：重复加载返回缓存同对象；clear_cache() 后为新对象。

公开面（任务给定口径，S2 §3.1）：load_dictionary / clear_cache /
NormalizationDictionary（.version / .date / .canonicalize）。本文件不 import
上述公开面以外的私有名；模块尚未存在时显式 pytest.fail（收集可过、测试红），
不做静默 skip。
"""

from __future__ import annotations

import json

import pytest

try:  # D19 src 实现未落地前模块不存在；显式 fail 保收集可过、测试红
    from news_flash_dedup.compare import normalize_dict
except ImportError:  # pragma: no cover - 实现落地前的预期分支
    normalize_dict = None


@pytest.fixture()
def nd():
    """normalize_dict 模块句柄；未实现时显式红（非收集错误、非 skip）。"""
    if normalize_dict is None:
        pytest.fail(
            "D19 契约：news_flash_dedup.compare.normalize_dict 尚未实现，"
            "本测试当前红属预期"
        )
    return normalize_dict


def _valid_payload() -> dict:
    """合法词典（S2 §4.1 格式）：subject alias 一条 + predicate synonym_class 一条。"""
    return {
        "version": "norm_dict_v1",
        "date": "2026-09-28",
        "sections": {
            "subject": {
                "kind": "alias",
                "directional": False,
                "entries": [
                    {"id": "subj-0001", "canonical": "贵州茅台",
                     "variants": ["茅台", "贵州茅台酒股份有限公司"]},
                ],
            },
            "predicate": {
                "kind": "synonym_class",
                "directional": False,
                "entries": [
                    {"id": "pred-0001", "canonical": "放缓",
                     "variants": ["持续放缓", "增速放缓"]},
                ],
            },
        },
    }


def _write_payload(tmp_path, payload: dict, name: str = "norm_dict.json") -> str:
    path = tmp_path / name
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return str(path)


# ---------- 条款 5：合法 JSON → frozen NormalizationDictionary ----------

def test_load_valid_dictionary_returns_frozen_object(nd, tmp_path):
    """合法 JSON 装载：version/date 可读，对象 frozen（不可变、缓存安全）。"""
    path = _write_payload(tmp_path, _valid_payload())
    dictionary = nd.load_dictionary(path)
    assert dictionary.version == "norm_dict_v1"
    assert dictionary.date == "2026-09-28"
    # frozen：dataclasses.FrozenInstanceError 是 AttributeError 子类；
    # 其他不可变实现（MappingProxyType/namedtuple 等）抛 AttributeError/TypeError。
    with pytest.raises((AttributeError, TypeError)):
        dictionary.version = "hacked"


# ---------- 条款 5 附：canonicalize 公开面（slot 分桶反向索引） ----------

def test_canonicalize_hit_returns_canonical_and_entry_id(nd, tmp_path):
    """命中：variant → (canonical, entry_id)，供 normalization_hits 留痕取用。

    返回形态按 S2 §7.1 `normalize(slot_kind, raw) -> (canonical, entry_id) | None`
    口径（任务条款将公开面命名为 canonicalize）；若实现改返 dict/对象，本断言红，
    届时按裁定口径翻转。
    """
    dictionary = nd.load_dictionary(_write_payload(tmp_path, _valid_payload()))
    result = dictionary.canonicalize("predicate", "持续放缓")
    assert result is not None, "variant 命中须返回 (canonical, entry_id)"
    canonical, entry_id = result
    assert canonical == "放缓"
    assert entry_id == "pred-0001"


def test_canonicalize_subject_alias_hit(nd, tmp_path):
    """subject 段同样命中（别名字典与谓词同义典分桶共存）。"""
    dictionary = nd.load_dictionary(_write_payload(tmp_path, _valid_payload()))
    canonical, entry_id = dictionary.canonicalize("subject", "茅台")
    assert canonical == "贵州茅台"
    assert entry_id == "subj-0001"


def test_canonicalize_miss_returns_none(nd, tmp_path):
    """未命中=恒等（None），调用方据此保持 Tier-1 逐字语义。"""
    dictionary = nd.load_dictionary(_write_payload(tmp_path, _valid_payload()))
    assert dictionary.canonicalize("predicate", "回购") is None


def test_canonicalize_slot_scoping(nd, tmp_path):
    """slot 分桶：predicate 段 variant 不得从 subject 槽查到（防跨槽串扰）。"""
    dictionary = nd.load_dictionary(_write_payload(tmp_path, _valid_payload()))
    assert dictionary.canonicalize("subject", "持续放缓") is None


# ---------- 条款 5：五类 fail-closed，各一例 → ValueError ----------

def test_fail_closed_ambiguous_variant_two_canonicals(nd, tmp_path):
    """① 歧义：同一 variant 出现在两个 canonical 下 → 拒。"""
    payload = _valid_payload()
    payload["sections"]["predicate"]["entries"] = [
        {"id": "pred-0001", "canonical": "放缓", "variants": ["持续放缓"]},
        {"id": "pred-0002", "canonical": "减速", "variants": ["持续放缓"]},
    ]
    with pytest.raises(ValueError):
        nd.load_dictionary(_write_payload(tmp_path, payload))


def test_fail_closed_chained_entries(nd, tmp_path):
    """② 链式：canonical 本身是别的 entry 的 variant（A→B, B→C）→ 拒。"""
    payload = _valid_payload()
    payload["sections"]["predicate"]["entries"] = [
        {"id": "pred-0001", "canonical": "放缓", "variants": ["持续放缓"]},
        {"id": "pred-0002", "canonical": "持续放缓", "variants": ["增长持续放缓"]},
    ]
    with pytest.raises(ValueError):
        nd.load_dictionary(_write_payload(tmp_path, payload))


@pytest.mark.parametrize(
    "entry",
    [
        {"id": "pred-0001", "canonical": "放缓", "variants": [""]},   # 空串 variant
        {"id": "pred-0001", "canonical": "", "variants": ["持续放缓"]},  # 空串 canonical
    ],
    ids=["empty_variant", "empty_canonical"],
)
def test_fail_closed_empty_string(nd, tmp_path, entry):
    """③ 空串（variant 或 canonical）→ 拒。"""
    payload = _valid_payload()
    payload["sections"]["predicate"]["entries"] = [entry]
    with pytest.raises(ValueError):
        nd.load_dictionary(_write_payload(tmp_path, payload))


def test_fail_closed_directional_reverse_exists(nd, tmp_path):
    """④ directional 反向已存在 → 拒。

    契约措辞差异注意：任务条款 5 口径为"directional 反向已存在"拒收；
    S2 §4.2④ 口径为"directional != false 一律拒（fail-closed 防误用）"。
    本例同向构造（directional=true 且正反向词条并存），两种口径下均须
    ValueError，fail-closed 方向一致；措辞差异已在回报中列出。
    """
    payload = _valid_payload()
    payload["sections"]["predicate"] = {
        "kind": "synonym_class",
        "directional": True,
        "entries": [
            {"id": "pred-0001", "canonical": "收购", "variants": ["并购"]},
            {"id": "pred-0002", "canonical": "并购", "variants": ["收购"]},  # 反向已存在
        ],
    }
    with pytest.raises(ValueError):
        nd.load_dictionary(_write_payload(tmp_path, payload))


def test_fail_closed_nfc_conflicting_variants(nd, tmp_path):
    """⑤ NFC 不同形：两 variant 仅 Unicode 形态不同、NFC 归并后同串且分属两
    canonical → 拒（装载期统一 NFC 归一后建索引，与 P09 归一纪律同构）。"""
    payload = _valid_payload()
    payload["sections"]["predicate"]["entries"] = [
        {"id": "pred-0001", "canonical": "甲", "variants": ["Aé"]},      # 组合形 U+00E9
        {"id": "pred-0002", "canonical": "乙", "variants": ["Aé"]},   # 分解形 e+U+0301
    ]
    # 防御性自检：两形源码层不同串、NFC 归并后同串（否则本用例不构契约类）
    import unicodedata
    _v0, _v1 = (e["variants"][0] for e in payload["sections"]["predicate"]["entries"])
    assert _v0 != _v1, "NFC 两形须在源码层不同码点序列"
    assert unicodedata.normalize("NFC", "Aé") == unicodedata.normalize("NFC", "Aé")
    with pytest.raises(ValueError):
        nd.load_dictionary(_write_payload(tmp_path, payload))


# ---------- 条款 5：缓存语义 ----------

def test_load_cache_same_object_and_clear_cache(nd, tmp_path):
    """重复加载返回缓存同对象；clear_cache() 后重建为新对象。"""
    path = _write_payload(tmp_path, _valid_payload())
    nd.clear_cache()
    try:
        first = nd.load_dictionary(path)
        second = nd.load_dictionary(path)
        assert first is second, "同路径重复加载须返回缓存同对象"
        nd.clear_cache()
        third = nd.load_dictionary(path)
        assert third is not first, "clear_cache 后须重建新对象"
        assert third.version == first.version
    finally:
        nd.clear_cache()


# ---------- S2 §4.2/§7.1 附加 fail-closed（装载期技术失败，绝不退化为无词典硬跑） ----------

def test_fail_closed_missing_version_field(nd, tmp_path):
    """版本字段缺失 → 拒（S2 §7.1"版本字段缺失拒"；版本钉死机制的前提）。"""
    payload = _valid_payload()
    del payload["version"]
    with pytest.raises(ValueError):
        nd.load_dictionary(_write_payload(tmp_path, payload))


def test_fail_closed_malformed_json(nd, tmp_path):
    """损坏 JSON → ValueError（PairAlignmentError 为 ValueError 子类）。"""
    path = tmp_path / "broken.json"
    path.write_text("{not a json", encoding="utf-8")
    with pytest.raises(ValueError):
        nd.load_dictionary(str(path))


def test_fail_closed_missing_file(nd, tmp_path):
    """词典文件缺失 → ValueError（S2 §4.2：绝不退化为"无词典硬跑"）。

    若实现让 FileNotFoundError 原样逃逸，本断言红——契约要求装载期失败
    统一归入 ValueError 族（PairAlignmentError）。
    """
    with pytest.raises(ValueError):
        nd.load_dictionary(str(tmp_path / "nonexistent.json"))
