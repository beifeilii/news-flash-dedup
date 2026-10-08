"""D19 杠杆 b · 对齐门 Tier-2 词典归一化（pair_alignment.py）契约单元测试。

D19 契约测试，src 实现主窗口进行中，当前红属预期（本文件只新增，不改 src/既有测试）。
契约来源：`log/temp/s2-normalize-hook-proposal.md` §3（两层匹配/锚定规则/版本钉死）、
§5（provenance 留痕）+ `log/D19-词典归一化设计冻结.md` §二-杠杆 b，逐条对应任务条款 6-9：

- 条款 6：`build_aligned(..., dictionary=None)` 默认路径与现状逐字节一致；
  dictionary 非 None 且 dictionary.version != dictionary_version → PairAlignmentError。
- 条款 7：Tier-2 命中——subject 逐字锚定 + predicate 经词典同义 → 对齐，
  basis=="SUBJECT_DETERMINISTIC_ALIAS"，normalization_hits 含 slot/前后值/canonical/
  entry_id；未命中词典的 raw 差异对仍不对齐（Tier-1 行为不破）。
- 条款 8：锚定规则——subject 与 predicate 同时仅词典可等（双槽无锚）→ 一期不对齐；
  key_object 不参与归一（词典含 key_object 同义词条也不生效）。
- 条款 9：provenance——outcome.provenance 含 dictionary_version（词典非 None 时从
  "占位"变"真实绑定"）；命中对归一明细由 AlignedPair.normalization_hits 承载。

构造 report 的姿势复用 tests/unit/test_pair_alignment.py 既有助手形态。
normalize_dict 模块尚未存在时显式 pytest.fail（收集可过、测试红），不 import
公开面（load_dictionary/clear_cache/NormalizationDictionary/version/date/
canonicalize）以外的私有名。
"""

from __future__ import annotations

import json

import pytest

from news_flash_dedup.compare import pair_alignment
from news_flash_dedup.facts import FactValidationReport

try:  # D19 src 实现未落地前模块不存在；显式 fail 保收集可过、测试红
    from news_flash_dedup.compare import normalize_dict
except ImportError:  # pragma: no cover - 实现落地前的预期分支
    normalize_dict = None


RECORD_ID_H = "a" * 64
RECORD_ID_C = "b" * 64
ALIGNMENT_VERSION = "alignment_v1"
NORM_DICT_VERSION = "norm_dict_v1"
NORM_DICT_DATE = "2026-09-28"


@pytest.fixture()
def nd():
    """normalize_dict 模块句柄；未实现时显式红（非收集错误、非 skip）。"""
    if normalize_dict is None:
        pytest.fail(
            "D19 契约：news_flash_dedup.compare.normalize_dict 尚未实现，"
            "本测试当前红属预期"
        )
    return normalize_dict


@pytest.fixture(autouse=True)
def _clean_dict_cache():
    """词典缓存跨用例隔离（缓存为模块级 lru_cache 语义）。"""
    if normalize_dict is not None:
        normalize_dict.clear_cache()
    yield
    if normalize_dict is not None:
        normalize_dict.clear_cache()


# ---------- report 构造助手（姿势复用 test_pair_alignment.py） ----------

def _evidence(text, quote, field, *, record_id=RECORD_ID_H, occurrence=0):
    start = -1
    for _ in range(occurrence + 1):
        start = text.index(quote, start + 1)
    return {
        "record_id": record_id,
        "field": field,
        "quote": quote,
        "start": start,
        "end": start + len(quote),
    }


def _missing():
    return {"status": "missing", "raw_value": None, "evidence": []}


def _present(text, quote, field, *, record_id=RECORD_ID_H):
    return {
        "status": "present",
        "raw_value": quote,
        "evidence": [_evidence(text, quote, field, record_id=record_id)],
    }


def _make_fact(text, *, fact_id, subject, predicate, key_object="",
               sentence=None, record_id=RECORD_ID_H):
    base = f"facts.{fact_id}"
    sentence = sentence or text
    return {
        "fact_id": fact_id,
        "evidence": [_evidence(text, sentence, base, record_id=record_id)],
        "fact_type": _present(text, predicate, base + ".fact_type", record_id=record_id),
        "subject": _present(text, subject, base + ".subject", record_id=record_id),
        "event_state": {
            "predicate": _present(text, predicate, base + ".event_state.predicate",
                                  record_id=record_id),
            "polarity": _present(text, predicate, base + ".event_state.polarity",
                                 record_id=record_id),
            "modality": _missing(),
            "attribution": _missing(),
        },
        "time": {"expression": _missing(), "stage": _missing(), "anchor": _missing()},
        "key_object": (_present(text, key_object, base + ".key_object", record_id=record_id)
                       if key_object else _missing()),
        "numerics": [],
    }


def _make_report(text, facts, *, record_id) -> FactValidationReport:
    artifact = {
        "schema_version": "1.0",
        "record_id": record_id,
        "offset_unit": "unicode_code_point",
        "extraction_status": "complete",
        "facts": facts,
        "unparsed_spans": [],
        "uncertainties": [],
    }
    return FactValidationReport(
        record_id=record_id,
        validated_complete=bool(facts),
        extraction_status="complete",
        valid_fact_ids=tuple(fact["fact_id"] for fact in facts),
        numeric_inventory=(),
        issues=(),
        artifact=artifact,
    )


def _build(history_text, current_text, history_fact, current_fact, **kwargs):
    history = _make_report(history_text, [history_fact], record_id=RECORD_ID_H)
    current = _make_report(current_text, [current_fact], record_id=RECORD_ID_C)
    return pair_alignment.build_aligned(
        history, current, history_text, current_text, **kwargs)


# ---------- 词典构造助手 ----------

def _norm_payload() -> dict:
    return {
        "version": NORM_DICT_VERSION,
        "date": NORM_DICT_DATE,
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


def _load(nd, tmp_path, payload=None) -> object:
    path = tmp_path / "norm_dict.json"
    path.write_text(json.dumps(payload or _norm_payload(), ensure_ascii=False),
                    encoding="utf-8")
    return nd.load_dictionary(str(path))


def _rewritten_pair_kwargs():
    """条款 6 默认路径样本：同事件改写双文（subject/predicate/key_object 逐字等）。"""
    history_text = "甲公司于9月9日宣布完成回购，交易对手为六家机构。"
    current_text = "回购已完成。甲公司是实施方，对手方六家机构。"
    return dict(
        history_text=history_text,
        current_text=current_text,
        history_fact=_make_fact(history_text, fact_id="f1", subject="甲公司",
                                predicate="回购", key_object="六家机构",
                                record_id=RECORD_ID_H),
        current_fact=_make_fact(current_text, fact_id="f1", subject="甲公司",
                                predicate="回购", key_object="六家机构",
                                record_id=RECORD_ID_C),
    )


# ---------- 条款 6：dictionary=None 默认路径逐字节一致 ----------

def test_dictionary_none_path_byte_identical():
    """显式 dictionary=None：对齐结果/basis/provenance 键集合与现状完全一致。"""
    args = _rewritten_pair_kwargs()
    outcome = _build(**args, dictionary_version="dict_v1",
                     alignment_version=ALIGNMENT_VERSION, dictionary=None)
    assert len(outcome.aligned_facts) == 1
    pair = outcome.aligned_facts[0]
    assert pair.basis == "FACT_EQUIVALENT"
    assert outcome.code == "FACT_EQUIVALENT"
    assert not outcome.unresolved_fields
    assert getattr(pair, "normalization_hits", ()) == (), (
        "None 路径对齐对不得携带归一命中记录"
    )
    # 逐字节一致含 provenance 结构：不增删键（S2 §五载体 2）
    assert dict(outcome.provenance) == {
        "dictionary_version": "dict_v1",
        "alignment_version": ALIGNMENT_VERSION,
    }


def test_default_call_without_dictionary_kwarg_unchanged():
    """完全不传 dictionary（今日签名）：绿地板——实现前后都必须通过。"""
    args = _rewritten_pair_kwargs()
    outcome = _build(**args, dictionary_version="dict_v1",
                     alignment_version=ALIGNMENT_VERSION)
    assert len(outcome.aligned_facts) == 1
    assert outcome.aligned_facts[0].basis == "FACT_EQUIVALENT"
    assert outcome.code == "FACT_EQUIVALENT"
    assert not outcome.unresolved_fields
    assert dict(outcome.provenance) == {
        "dictionary_version": "dict_v1",
        "alignment_version": ALIGNMENT_VERSION,
    }


# ---------- 条款 6：版本钉死（fail-closed） ----------

def test_dictionary_version_mismatch_raises(nd, tmp_path):
    """dictionary.version != dictionary_version → PairAlignmentError（防静默漂移）。"""
    dictionary = _load(nd, tmp_path)
    text = "甲公司完成回购。"
    fact_h = _make_fact(text, fact_id="f1", subject="甲公司", predicate="回购",
                        record_id=RECORD_ID_H)
    fact_c = _make_fact(text, fact_id="f1", subject="甲公司", predicate="回购",
                        record_id=RECORD_ID_C)
    with pytest.raises(pair_alignment.PairAlignmentError):
        _build(text, text, fact_h, fact_c,
               dictionary_version="dict_v1",            # 与词典 version 不符
               alignment_version=ALIGNMENT_VERSION,
               dictionary=dictionary)
    # 版本串相符时同一调用必须放行（排除"凡是词典即拒"的假实现）
    outcome = _build(text, text, fact_h, fact_c,
                     dictionary_version=NORM_DICT_VERSION,
                     dictionary_date=NORM_DICT_DATE,
                     alignment_version=ALIGNMENT_VERSION,
                     dictionary=dictionary)
    assert len(outcome.aligned_facts) == 1


def test_dictionary_date_mismatch_raises(nd, tmp_path):
    """dictionary_date 提供且与 dictionary.date 不符 → PairAlignmentError（S2 §3.3 同错）。"""
    dictionary = _load(nd, tmp_path)
    text = "甲公司完成回购。"
    fact_h = _make_fact(text, fact_id="f1", subject="甲公司", predicate="回购",
                        record_id=RECORD_ID_H)
    fact_c = _make_fact(text, fact_id="f1", subject="甲公司", predicate="回购",
                        record_id=RECORD_ID_C)
    with pytest.raises(pair_alignment.PairAlignmentError):
        _build(text, text, fact_h, fact_c,
               dictionary_version=NORM_DICT_VERSION,
               dictionary_date="1999-01-01",            # 与词典 date 不符
               alignment_version=ALIGNMENT_VERSION,
               dictionary=dictionary)


# ---------- 条款 7：Tier-2 命中 ----------

def test_tier2_predicate_synonym_hit_aligns(nd, tmp_path):
    """subject 逐字锚定 + predicate 经词典同义（持续放缓→放缓）→ 对齐。"""
    dictionary = _load(nd, tmp_path)
    history_text = "甲公司表示，就业增长持续放缓。"
    current_text = "甲公司表示，就业增长放缓。"
    outcome = _build(
        history_text, current_text,
        _make_fact(history_text, fact_id="f1", subject="甲公司", predicate="持续放缓",
                   record_id=RECORD_ID_H),
        _make_fact(current_text, fact_id="f1", subject="甲公司", predicate="放缓",
                   record_id=RECORD_ID_C),
        dictionary_version=NORM_DICT_VERSION, dictionary_date=NORM_DICT_DATE,
        alignment_version=ALIGNMENT_VERSION, dictionary=dictionary,
    )
    assert len(outcome.aligned_facts) == 1, "条款 7：单槽词典同义 + 另一槽逐字锚定应对齐"
    pair = outcome.aligned_facts[0]
    assert pair.basis == "SUBJECT_DETERMINISTIC_ALIAS"
    # 对级 code 仍走 FACT_EQUIVALENT（S2 §五/§7.3：白名单零改动，basis 留在对齐层）
    assert outcome.code == "FACT_EQUIVALENT"
    assert not outcome.unresolved_fields
    hits = getattr(pair, "normalization_hits", None)
    assert hits is not None and len(hits) == 1, (
        "条款 7：命中对 normalization_hits 须恰含被归一槽的一条记录"
    )
    hit = hits[0]
    assert hit.slot == "predicate"
    assert {hit.history_raw, hit.current_raw} == {"持续放缓", "放缓"}
    assert hit.canonical == "放缓"
    assert hit.entry_id == "pred-0001"


def test_tier2_subject_alias_hit_aligns(nd, tmp_path):
    """predicate 逐字锚定 + subject 经词典别名（茅台→贵州茅台）→ 对齐。"""
    dictionary = _load(nd, tmp_path)
    history_text = "茅台今日上涨。"
    current_text = "贵州茅台今日上涨。"
    outcome = _build(
        history_text, current_text,
        _make_fact(history_text, fact_id="f1", subject="茅台", predicate="上涨",
                   record_id=RECORD_ID_H),
        _make_fact(current_text, fact_id="f1", subject="贵州茅台", predicate="上涨",
                   record_id=RECORD_ID_C),
        dictionary_version=NORM_DICT_VERSION, dictionary_date=NORM_DICT_DATE,
        alignment_version=ALIGNMENT_VERSION, dictionary=dictionary,
    )
    assert len(outcome.aligned_facts) == 1
    pair = outcome.aligned_facts[0]
    assert pair.basis == "SUBJECT_DETERMINISTIC_ALIAS"
    assert not outcome.unresolved_fields
    hits = getattr(pair, "normalization_hits", None)
    assert hits is not None and len(hits) == 1
    hit = hits[0]
    assert hit.slot == "subject"
    assert {hit.history_raw, hit.current_raw} == {"茅台", "贵州茅台"}
    assert hit.canonical == "贵州茅台"
    assert hit.entry_id == "subj-0001"


def test_raw_difference_not_in_dictionary_stays_unmatched(nd, tmp_path):
    """Tier-1 行为不破：raw 差异未命中词典（回购≠收购）→ 仍不对齐。"""
    dictionary = _load(nd, tmp_path)
    history_text = "甲公司完成回购。"
    current_text = "甲公司完成收购。"
    outcome = _build(
        history_text, current_text,
        _make_fact(history_text, fact_id="f1", subject="甲公司", predicate="回购",
                   record_id=RECORD_ID_H),
        _make_fact(current_text, fact_id="f1", subject="甲公司", predicate="收购",
                   record_id=RECORD_ID_C),
        dictionary_version=NORM_DICT_VERSION, dictionary_date=NORM_DICT_DATE,
        alignment_version=ALIGNMENT_VERSION, dictionary=dictionary,
    )
    assert not outcome.aligned_facts
    assert outcome.code == "RULE_UNCOVERED"
    assert set(outcome.unresolved_fields) == {"history:f1", "current:f1"}


# ---------- 条款 8：锚定规则 ----------

def test_anchor_rule_double_slot_dictionary_only_rejected(nd, tmp_path):
    """双槽无锚：subject 与 predicate 同时仅词典可等 → 一期不对齐（落未匹配）。"""
    dictionary = _load(nd, tmp_path)
    history_text = "茅台方面表示，就业增长持续放缓。"
    current_text = "贵州茅台方面表示，就业增长放缓。"
    outcome = _build(
        history_text, current_text,
        _make_fact(history_text, fact_id="f1", subject="茅台", predicate="持续放缓",
                   record_id=RECORD_ID_H),
        _make_fact(current_text, fact_id="f1", subject="贵州茅台", predicate="放缓",
                   record_id=RECORD_ID_C),
        dictionary_version=NORM_DICT_VERSION, dictionary_date=NORM_DICT_DATE,
        alignment_version=ALIGNMENT_VERSION, dictionary=dictionary,
    )
    assert not outcome.aligned_facts, (
        "条款 8：subject/predicate 同时仅靠词典等价（无原文锚点）一期不得对齐"
    )
    assert outcome.code == "RULE_UNCOVERED"
    assert set(outcome.unresolved_fields) == {"history:f1", "current:f1"}


def test_key_object_not_normalized(nd, tmp_path):
    """key_object 不参与归一：subject/predicate 逐字等、key_object 均 present 且
    不同 → 即便词典已注入也不对齐（Tier-1 key_object 语义保持）。"""
    dictionary = _load(nd, tmp_path)
    history_text = "甲公司完成回购合同。"
    current_text = "甲公司完成回购协议。"
    outcome = _build(
        history_text, current_text,
        _make_fact(history_text, fact_id="f1", subject="甲公司", predicate="回购",
                   key_object="合同", record_id=RECORD_ID_H),
        _make_fact(current_text, fact_id="f1", subject="甲公司", predicate="回购",
                   key_object="协议", record_id=RECORD_ID_C),
        dictionary_version=NORM_DICT_VERSION, dictionary_date=NORM_DICT_DATE,
        alignment_version=ALIGNMENT_VERSION, dictionary=dictionary,
    )
    assert not outcome.aligned_facts
    assert set(outcome.unresolved_fields) == {"history:f1", "current:f1"}


def test_key_object_section_in_dictionary_has_no_effect(nd, tmp_path):
    """条款 8 字面口径：词典即使含 key_object 同义词条也不生效。

    契约未言明 loader 对未知 section（key_object）是否 fail-closed：若拒载，
    本用例以 skip 显式记录该待裁定点（不静默放过），由主窗口裁定后翻转。
    """
    payload = _norm_payload()
    payload["sections"]["key_object"] = {
        "kind": "alias",
        "directional": False,
        "entries": [
            {"id": "keyo-0001", "canonical": "合同", "variants": ["协议"]},
        ],
    }
    try:
        dictionary = _load(nd, tmp_path, payload)
    except ValueError:
        pytest.skip(
            "loader 对 key_object section fail-closed 拒载（契约未言明未知 section "
            "行为，待主窗口裁定）；拒载本身已保证 key_object 词条不生效"
        )
    history_text = "甲公司完成回购合同。"
    current_text = "甲公司完成回购协议。"
    outcome = _build(
        history_text, current_text,
        _make_fact(history_text, fact_id="f1", subject="甲公司", predicate="回购",
                   key_object="合同", record_id=RECORD_ID_H),
        _make_fact(current_text, fact_id="f1", subject="甲公司", predicate="回购",
                   key_object="协议", record_id=RECORD_ID_C),
        dictionary_version=NORM_DICT_VERSION, dictionary_date=NORM_DICT_DATE,
        alignment_version=ALIGNMENT_VERSION, dictionary=dictionary,
    )
    assert not outcome.aligned_facts, (
        "条款 8：词典含 key_object 同义词条（合同←协议）也不得参与归一"
    )
    assert set(outcome.unresolved_fields) == {"history:f1", "current:f1"}


# ---------- 条款 9：provenance 留痕 ----------

def test_provenance_real_binding_on_hit(nd, tmp_path):
    """命中时 outcome.provenance 的 dictionary_version/dictionary_date 从"占位"变
    "真实绑定"（S2 §五载体 2），归一明细由 AlignedPair.normalization_hits 承载。

    契约歧义注意：任务条款 9 字面另有"命中时含 normalization 相关键"表述，与
    S2 §五载体 2"provenance 不增删键"存在张力；本测试按两文档一致部分断言
    （版本真实绑定 + normalization_hits 载体），歧义已在回报中列出待裁定。
    """
    dictionary = _load(nd, tmp_path)
    history_text = "甲公司表示，就业增长持续放缓。"
    current_text = "甲公司表示，就业增长放缓。"
    outcome = _build(
        history_text, current_text,
        _make_fact(history_text, fact_id="f1", subject="甲公司", predicate="持续放缓",
                   record_id=RECORD_ID_H),
        _make_fact(current_text, fact_id="f1", subject="甲公司", predicate="放缓",
                   record_id=RECORD_ID_C),
        dictionary_version=NORM_DICT_VERSION, dictionary_date=NORM_DICT_DATE,
        alignment_version=ALIGNMENT_VERSION, dictionary=dictionary,
    )
    assert outcome.aligned_facts, "前置：本用例需 Tier-2 命中"
    # dictionary_version 真实绑定（非占位串 dict_v1）
    assert outcome.provenance["dictionary_version"] == NORM_DICT_VERSION
    assert outcome.provenance["dictionary_date"] == NORM_DICT_DATE
    assert outcome.provenance["alignment_version"] == ALIGNMENT_VERSION
    # 归一留痕载体 1：命中对携带 normalization_hits（slot/前后值/canonical/entry_id）
    hits = getattr(outcome.aligned_facts[0], "normalization_hits", None)
    assert hits, "命中时归一明细须在 normalization_hits 可查"
    hit = hits[0]
    for field_name in ("slot", "history_raw", "current_raw", "canonical", "entry_id"):
        assert hasattr(hit, field_name), (
            f"normalization_hits 记录缺字段 {field_name!r}（S2 §五 NormalizationHit 契约）"
        )
