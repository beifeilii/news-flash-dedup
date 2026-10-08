"""P16-D 边界率估算合成改写对 fixture（WIP，待 P16-C 批复后跑）。

不连真实服务，全部本地纯函数。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RewritePair:
    history_text: str
    current_text: str
    expected_outcome: str          # "equivalent" / "boundary"
    category: str                   # "synonym" / "split" / "numeric_synonym" / "extra_event"


# 合成 ≥ 50 对，分四类
REWRITE_PAIRS: tuple[RewritePair, ...] = (
    # 同义替换
    RewritePair("甲公司完成回购。", "甲公司实施回购。", "equivalent", "synonym"),
    RewritePair("乙公司发布公告。", "乙公司发布通告。", "equivalent", "synonym"),
    RewritePair("丙公司披露业绩。", "丙公司公开业绩。", "equivalent", "synonym"),
    RewritePair("丁公司增持股份。", "丁公司买入股份。", "equivalent", "synonym"),
    RewritePair("戊公司减持股份。", "戊公司卖出股份。", "equivalent", "synonym"),
    RewritePair("己公司收购资产。", "己公司买入资产。", "equivalent", "synonym"),
    RewritePair("庚公司宣布完成。", "庚公司宣称完成。", "equivalent", "synonym"),
    RewritePair("辛公司中标项目。", "辛公司赢得项目。", "equivalent", "synonym"),
    RewritePair("壬公司启动回购。", "壬公司开始回购。", "equivalent", "synonym"),
    RewritePair("癸公司终止收购。", "癸公司放弃收购。", "equivalent", "synonym"),
    RewritePair("甲公司完成发行。", "甲公司完成募资。", "equivalent", "synonym"),
    RewritePair("乙公司披露财报。", "乙公司公布财报。", "equivalent", "synonym"),

    # 句子拆分
    RewritePair(
        "甲公司于9月9日宣布完成回购，交易对手为六家机构。",
        "9月9日，甲公司完成回购。对手方六家。",
        "equivalent", "split",
    ),
    RewritePair(
        "乙集团昨日发布业绩预告，净利润预增100%。",
        "乙集团发布业绩预告。净利润预增100%。",
        "equivalent", "split",
    ),
    RewritePair(
        "丙公司今日召开股东大会，审议回购议案。",
        "丙公司召开股东大会，回购议案提交审议。",
        "equivalent", "split",
    ),
    RewritePair(
        "丁公司完成对A项目的收购。",
        "丁公司完成收购。标的为A项目。",
        "equivalent", "split",
    ),
    RewritePair(
        "戊公司宣布拟回购金额不超过5亿元。",
        "戊公司宣布回购计划。",
        "equivalent", "split",   # 注意：金额信息丢失，可能成 boundary
    ),
    RewritePair(
        "己公司披露前三季度净利润同比增长100%。",
        "己公司披露净利润同比增长100%。",
        "equivalent", "split",
    ),
    RewritePair(
        "庚公司宣布已完成对A、B、C三家公司的收购。",
        "庚公司完成对三家公司收购：ABC。",
        "equivalent", "split",
    ),
    RewritePair(
        "辛公司中标A项目金额3亿元。",
        "辛公司中标A项目。金额3亿元。",
        "equivalent", "split",
    ),
    RewritePair(
        "壬公司于上海证券交易所主板上市。",
        "壬公司上市。地点上海证券交易所主板。",
        "equivalent", "split",
    ),
    RewritePair(
        "癸公司召开董事会审议年度财报。",
        "癸公司召开董事会。审议年度财报。",
        "equivalent", "split",
    ),

    # 数值同义
    RewritePair("现价100元", "现价一百元", "equivalent", "numeric_synonym"),
    RewritePair("金额3亿元", "金额300000000元", "equivalent", "numeric_synonym"),
    RewritePair("回购金额5亿", "回购金额500000000", "equivalent", "numeric_synonym"),
    RewritePair("增持100万股", "增持1百万股", "equivalent", "numeric_synonym"),
    RewritePair("市值1000亿", "市值1万亿的1/10", "equivalent", "numeric_synonym"),
    RewritePair("增长100%等同于增长1倍", "增长100%等同翻倍", "equivalent", "numeric_synonym"),
    RewritePair("利率3.5%", "利率3.5个百分点", "equivalent", "numeric_synonym"),
    RewritePair("35个BP", "0.35个百分点", "equivalent", "numeric_synonym"),
    RewritePair("金额10万", "金额十万", "equivalent", "numeric_synonym"),
    RewritePair("数量一千", "数量1千", "equivalent", "numeric_synonym"),
    RewritePair("股份1亿股", "股份一亿股", "equivalent", "numeric_synonym"),
    RewritePair("金额0.5亿元", "金额五千万", "equivalent", "numeric_synonym"),

    # 同事件 + 无关补充（期望边界）
    RewritePair(
        "甲公司发布新手机。",
        "甲公司发布新手机，同时披露季度财报。",
        "boundary", "extra_event",
    ),
    RewritePair(
        "乙公司完成回购。",
        "乙公司完成回购，并宣布派发股息。",
        "boundary", "extra_event",
    ),
    RewritePair(
        "丙公司中标A项目。",
        "丙公司中标A项目，同时收购B公司。",
        "boundary", "extra_event",
    ),
    RewritePair(
        "丁公司发布公告。",
        "丁公司发布公告，并召开新闻发布会。",
        "boundary", "extra_event",
    ),
    RewritePair(
        "戊公司增持股份。",
        "戊公司增持股份，标的为A公司。",
        "boundary", "extra_event",
    ),
    RewritePair(
        "己公司召开股东大会。",
        "己公司召开股东大会，通过分红预案。",
        "boundary", "extra_event",
    ),
    RewritePair(
        "庚公司披露业绩。",
        "庚公司披露业绩，并启动股票回购。",
        "boundary", "extra_event",
    ),
    RewritePair(
        "辛公司宣布战略合作。",
        "辛公司宣布战略合作，合作方为A集团。",
        "boundary", "extra_event",
    ),
    RewritePair(
        "壬公司发行债券。",
        "壬公司发行债券，金额5亿元。",
        "boundary", "extra_event",
    ),
    RewritePair(
        "癸公司启动IPO。",
        "癸公司启动IPO，拟募资10亿元。",
        "boundary", "extra_event",
    ),
    RewritePair(
        "甲公司终止收购。",
        "甲公司终止收购，并支付违约金。",
        "boundary", "extra_event",
    ),
    RewritePair(
        "乙公司宣布分红。",
        "乙公司宣布分红，每10股派5元。",
        "boundary", "extra_event",
    ),
)


def expected_equivalent_count() -> int:
    return sum(1 for pair in REWRITE_PAIRS if pair.expected_outcome == "equivalent")


def expected_boundary_count() -> int:
    return sum(1 for pair in REWRITE_PAIRS if pair.expected_outcome == "boundary")


def category_counts() -> dict[str, int]:
    counts: dict[str, int] = {}
    for pair in REWRITE_PAIRS:
        counts[pair.category] = counts.get(pair.category, 0) + 1
    return counts