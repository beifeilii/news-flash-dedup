"""W2Fα1 条目52（WB2-H2）："约"洗白——value_time 保留 approximate 不复位。

llm.py 供给 comparator="约/近" → approximate=True，但 value_time
normalize_numeric 归一 comparator="=" 时把 approximate 强制复位 False
（_COMPARATOR_CN_APPROX_RESET）——"约100"与裸"100"全同 → equal。
09 §8.2 明文："约100与100｜保留近似模态，不擅设容差；不能证明同义则边界"。
修法：移除 approximate 复位（comparator 仍归一 "="），保留近似模态 →
compare_numeric 现役 approximate 闸（任一近似 → unresolved）执法，
fp 安全向（equal→unresolved）。
"""

from __future__ import annotations

from news_flash_dedup.compare import value_time

RID = "a" * 64
_TEXT = "涨幅约100元。"
_BARE_TEXT = "涨幅100元。"
_ALIGNMENT = value_time.AlignmentProof(confirmed=True,
                                       basis="NUMERIC_SAME_DECIMAL:f1/f1")


def _spec(text: str, raw: str, *, comparator: str = "=",
          approximate: bool = False) -> value_time.NumericSpec:
    start = text.index(raw)
    evidence = value_time.EvidenceRef(
        record_id=RID, field="facts.f1.numerics.n1.value", quote=raw,
        start=start, end=start + len(raw))
    return value_time.NumericSpec(
        status="present", raw_value=raw, evidence=evidence, unit="元",
        comparator=comparator, approximate=approximate)


def _approx_spec() -> value_time.NumericSpec:
    return _spec(_TEXT, "100", comparator="约", approximate=True)


def _bare_spec() -> value_time.NumericSpec:
    return _spec(_BARE_TEXT, "100")


# ---------- 红测（修复前 approximate 被复位 → equal） ----------

def test_normalize_preserves_approximate_flag():
    """归一层：comparator="约" 归一为 "=" 但 approximate 必须保留（不复位）。"""
    checked = value_time.normalize_numeric(_TEXT, RID, _approx_spec())
    assert checked.valid
    assert checked.comparator == "="          # 比较符归一保持
    assert checked.approximate is True        # 近似模态保留（修复前被洗 False）


def test_normalize_preserves_approximate_flag_near():
    checked = value_time.normalize_numeric(
        _TEXT, RID, _spec(_TEXT, "100", comparator="近", approximate=True))
    assert checked.valid
    assert checked.comparator == "="
    assert checked.approximate is True


def test_approx_vs_bare_is_unresolved_not_equal():
    """约100 vs 100 → unresolved（09 §8.2：不能证明同义则边界；修复前 equal）。"""
    left = value_time.normalize_numeric(_TEXT, RID, _approx_spec())
    right = value_time.normalize_numeric(_BARE_TEXT, RID, _bare_spec())
    comparison = value_time.compare_numeric(left, right, alignment=_ALIGNMENT)
    assert comparison.status == "unresolved"
    assert comparison.reason_code == "NUMERIC_ALIGNMENT_FAILED"


def test_both_approx_is_unresolved_per_contract():
    """双侧同"约" → 可比性按契约：任一近似即不可证同义 → unresolved。"""
    left = value_time.normalize_numeric(_TEXT, RID, _approx_spec())
    right = value_time.normalize_numeric(_TEXT, RID, _approx_spec())
    comparison = value_time.compare_numeric(left, right, alignment=_ALIGNMENT)
    assert comparison.status == "unresolved"
    assert comparison.reason_code == "NUMERIC_ALIGNMENT_FAILED"


# ---------- 前后均绿守卫（非"约"路径零漂移） ----------

def test_bare_vs_bare_still_equal():
    left = value_time.normalize_numeric(_BARE_TEXT, RID, _bare_spec())
    right = value_time.normalize_numeric(_BARE_TEXT, RID, _bare_spec())
    comparison = value_time.compare_numeric(left, right, alignment=_ALIGNMENT)
    assert comparison.status == "equal"


def test_chao_comparator_mapping_unchanged():
    """"超"→">" 映射与 approximate=False 保持（D19 勘误纪律不动）。"""
    text = "涨幅超100元。"
    checked = value_time.normalize_numeric(
        text, RID, _spec(text, "100", comparator="超"))
    assert checked.valid
    assert checked.comparator == ">"
    assert checked.approximate is False


def test_approximate_false_by_default_unchanged():
    checked = value_time.normalize_numeric(_BARE_TEXT, RID, _bare_spec())
    assert checked.valid
    assert checked.approximate is False
