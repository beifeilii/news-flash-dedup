"""P2 先行件①红测（2026-10-09）：时间硬编码清除——无年 'M月D日' 不得静默补默认年。

正典/出处：
- 宪章《判定宪章-草案-v2-1009.md》（policy_v2 已生效）§三-2：相对时间/缺锚
  时间无共同绝对锚点 → 边界并 KEEP，**不得用系统接收时间/发布时间/另一条
  正文替其补日期**；§〇-4：判定唯一输入=快讯正文。
- 终裁令《判定链改造-终裁令-正典-1009.md》§五-3："不重复"证明接口纪律同源
  （不能证明即不得签发）。
- 施工对象：compare/value_time.py `_YEAR_LESS_DEFAULT_YEAR=2026`（D19 C 包
  声明式假设，V1 金标 2026-09 语料专用）——P2 拆除，无年 'M月D日' 解析层
  不命中 → normalize_time 落 TIME_RELATION_UNCERTAIN（时间未决→边界）。

红能力说明：本文件全部用例在拆除前代码下必红（旧行为=静默补 2026 后按
绝对日期比较）；拆除后转绿。带年日期路径为前后均绿守卫。
"""

from __future__ import annotations

from datetime import date

from news_flash_dedup.compare.value_time import (
    AlignmentProof,
    EvidenceRef,
    TimeSpec,
    compare_time,
    normalize_time,
)

RECORD_ID = "b" * 64
ALIGNED = AlignmentProof(confirmed=True, basis="same subject, metric, time, role and original Evidence")


def _ev(text: str, quote: str, field: str = "facts.f1.time.expression") -> EvidenceRef:
    start = text.index(quote)
    return EvidenceRef(RECORD_ID, field, quote, start, start + len(quote))


def _time(text: str, raw: str, *, anchor: str | None = None,
          relation: str | None = None, anchor_proof=None):
    spec = TimeSpec(
        status="present",
        raw_value=raw,
        evidence=_ev(text, raw),
        anchor_evidence=None if anchor is None else _ev(text, anchor, "facts.f1.time.anchor"),
        anchor_relation=relation,
        anchor_proof=anchor_proof,
    )
    return normalize_time(text, RECORD_ID, spec)


def _compare(left, right):
    return compare_time(left, right, alignment=ALIGNED)


# ---------- 主红测：无年 'M月D日' → 时间未决，不得静默补 2026 ----------

def test_yearless_md_is_not_silently_completed_to_default_year():
    """红测核心：'9月20日' 无年 → invalid(TIME_RELATION_UNCERTAIN)、absolute 恒 None。

    拆除前（旧行为）：valid 且 absolute == date(2026, 9, 20)（静默补
    _YEAR_LESS_DEFAULT_YEAR=2026）——本断言在旧码下必红。
    """
    checked = _time("甲9月20日公告。", "9月20日")
    assert checked.absolute is None
    assert checked.valid is False
    assert checked.issue_code == "TIME_RELATION_UNCERTAIN"


def test_yearless_md_same_pair_is_undecided_not_compatible():
    """双侧同 '9月20日'：年不可证 → 时间未决（旧码借默认年判 compatible，必红）。"""
    result = _compare(_time("甲9月20日公告。", "9月20日"),
                      _time("乙9月20日公告。", "9月20日"))
    assert result.relation == "unresolved"
    assert result.reason_code == "TIME_RELATION_UNCERTAIN"


def test_yearless_md_diff_pair_is_undecided_not_signed_conflict():
    """'9月20日' vs '9月21日'：年不可证 → 未决，不得签 VERIFIED_CONFLICT
    （旧码借默认年判 conflict，必红）。"""
    result = _compare(_time("甲9月20日公告。", "9月20日"),
                      _time("乙9月21日公告。", "9月21日"))
    assert result.relation == "unresolved"
    assert result.reason_code == "TIME_RELATION_UNCERTAIN"


def test_yearless_md_vs_full_year_date_is_undecided_not_compatible():
    """'9月20日' vs '2026年9月20日'：无年侧不得借默认年对齐有年侧（旧码
    双侧同落 2026-09-20 → compatible，必红）。"""
    result = _compare(_time("甲9月20日公告。", "9月20日"),
                      _time("乙2026年9月20日公告。", "2026年9月20日"))
    assert result.relation == "unresolved"
    assert result.reason_code == "TIME_RELATION_UNCERTAIN"


def test_yearless_md_anchor_cannot_resolve_relative_time():
    """无年锚点 '9月22日' 不得为 '次日' 补全绝对日期（旧码锚解析同样静默
    补 2026 → absolute=2026-09-23，必红）。"""
    checked = _time("9月22日公告，次日生效", "次日",
                    anchor="9月22日", relation="next_day_of",
                    anchor_proof=ALIGNED)
    assert checked.absolute is None
    assert checked.valid is False
    assert checked.issue_code == "TIME_RELATION_UNCERTAIN"


# ---------- 前后均绿守卫：带年日期路径零漂移 ----------

def test_full_year_date_paths_unchanged_guard():
    """守卫：带年日期解析/比较逐字节保持（本组断言拆除前后均绿）。"""
    chinese = _time("甲2026年9月20日公告。", "2026年9月20日")
    iso = _time("乙2026-09-20公告。", "2026-09-20")
    assert chinese.valid and iso.valid
    assert chinese.absolute == iso.absolute == date(2026, 9, 20)
    assert _compare(chinese, iso).relation == "compatible"
    other = _time("丙2026年9月21日公告。", "2026年9月21日")
    result = _compare(chinese, other)
    assert result.relation == "conflict" and result.reason_code == "VERIFIED_CONFLICT"


def test_full_year_anchor_still_resolves_relative_time_guard():
    """守卫：带年锚点仍可为 '次日' 供锚（宪章不禁正文自供锚；前后均绿）。"""
    checked = _time("2026年9月22日公告，次日生效", "次日",
                    anchor="2026年9月22日", relation="next_day_of",
                    anchor_proof=ALIGNED)
    assert checked.valid and checked.absolute == date(2026, 9, 23)


def test_default_year_constant_removed_from_module():
    """钉死拆除面：模块内不得复活 _YEAR_LESS_DEFAULT_YEAR 常量形态
    （防回退红线；旧码下本断言因常量存在而必红）。"""
    from news_flash_dedup.compare import value_time
    assert not hasattr(value_time, "_YEAR_LESS_DEFAULT_YEAR")
