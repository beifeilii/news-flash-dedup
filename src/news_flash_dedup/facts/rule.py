"""B2 规则式 P14 事实抽取基线（主窗口 23:0x 亲笔，按 `log/temp/b2-rule-p14-design.md` 实装）。

性质声明（诚实标注，不可删除）：本模块是 **deterministic baseline**（纯 Python
正则/词典，零第三方依赖，无 jieba、无模型调用）。它不声称达到 09 §7 模型抽取
的关系正确性；它的价值是：① 给 decide 链路一个永远可用、永远可复现的 facts
来源；② 作为模型抽取（P14 正式版）落地前的对照基线与回归地板。所有"规则未
命中即 missing"的槽位，语义是**"规则未命中"**，不是 09 §7.2 的"正文确实未
表达"——审计与回放报告必须显式携带此声明。

已知弱项 W1-W8 见设计书 §6.1（同义改写/主体别名对齐不了、复杂句式可能抽错、
中文数词 value=null、anchor 恒 missing、词表外比较符漏抽、引号内标点误切、
role 恒 missing）——W3/W6 残余假对齐/假等价风险以金标回放实测为诚实仲裁。

消费方契约（主窗口已核）：
- pair_alignment 三元组：subject/predicate raw_value 双侧逐字等才配对，
  key_object 双 None 或同串可配对（pair_alignment.py L186-206）；
- p15_integration._spec_from_slot 从 numeric 的 **value 槽字典**内联读
  unit/magnitude/currency/role/metric/comparator/approximate/range_end
  （普通值，非槽位 dict）——故 value 槽同时携带内联键供 P15 比较；
- normalize_time 要求 time evidence field 以 ".time.expression" 结尾且
  raw_value ∈ quote（value_time.py L342-346）；
- build_aligned 对零 Fact 侧抛 PairAlignmentError → 非空无命中返回 fallback
  minimal fact（设计书 §4 裁定），空文本返回 []（T024 边界域由调用方判空）。
"""

from __future__ import annotations

import re
from typing import Any, Mapping

RULE_DICT_VERSION = "rule_dict_v1"
# D19 杠杆 a：v2 供给版本（抽取覆盖扩展）。v1 常量与行为逐字节保持；
# v2 仅在 extract_facts(dict_version=RULE_DICT_VERSION_V2) 显式传入时生效。
RULE_DICT_VERSION_V2 = "rule_dict_v2"

_RECORD_ID_RE = re.compile(r"[0-9a-f]{64}\Z")

# 子句切分（与 facts/core.py L20 `_CLAUSE` 同构；引号内标点误切属 W7 已知弱项）
_CLAUSE = re.compile(r"[^。！？!?；;\n]+[。！？!?；;]?")
# W2Fα1（C3）：子句实义判定的分隔符剥离集——" 。"型子句 strip 空白后仅剩
# 分隔符=无实义（fallback 声明路径判空用，不影响 _clauses 现役切分语义）。
_CLAUSE_SEPARATORS = "。！？!?；;"

# 事件动词词典（设计书 §2.3 四组；列表顺序即"同长度命中取词典序号小者"的序号）
_EVENT_VERBS: tuple[str, ...] = (
    # 公司行动
    "收购", "回购", "增持", "减持", "中标", "签署", "获批", "退市", "上市",
    "停牌", "复牌", "分红", "派息", "配股", "增发", "重组", "并购", "破产",
    "清算", "转让", "质押",
    # 信息披露
    "发布", "披露", "公告", "公布", "预告",
    # 价格产量
    "涨价", "降价", "上调", "下调", "提价", "减产", "增产", "停产", "复工",
    "限价",
    # 金融事件
    "违约", "兑付", "展期", "降息", "加息", "降准",
)

# D19 D 包（v2）：标识符形态数值判据——证券代码/公告文号/标准号/型号数字/年份
# 不是数值事实（S1 实测：300808/[2026]14号/IEC 63203/Z9GT/2027 混入 numerics 撞
# 对齐门）。判据只看码点邻接上下文，确定性、可复跑。
def _is_identifier_numeric_v2(text: str, start: int, end: int) -> bool:
    raw = text[start:end]
    before = text[start - 1] if start > 0 else ""
    after = text[end] if end < len(text) else ""
    if re.fullmatch(r"[036][0-9]{5}", raw):            # 6 位证券代码
        return True
    if before.isascii() and before.isalpha():          # 字母紧邻（Z9GT、IEC 63203）
        return True
    if after.isascii() and after.isalpha():
        return True
    if (before and before in "[-.") or (after and after in "]-:：."):
        return True             # 文号 [2026]14号 / 标准号链 / 点分段税号链
                                # （1b93a701 实证："7005.21.03"尾段"03"以 before="."
                                #  漏网入槽——前界补"."）
        # （S4 G 行实证：before/after 为空串时 "" in "[-" 恒真致文首/文尾
        #  数值误排——5f891f4c "2026中国联通" 双侧不对称根因；空串守卫修正）
    if re.fullmatch(r"(?:19|20)[0-9]{2}", raw) and after and after in "年）)":
        return True                                     # 年份
        # （W2Fα1-E3：补 after 空串守卫——与 L72 同纪律；修复前
        #  "" in "年）)" 恒真，文尾裸年份（"市值2025"型）被静默误排）
    if text[end:end + 2] == "年期":                     # 合约期限（30年期主力合约）
        return True
    if text[end:end + 2] == "指数":                     # 指数名成分（价格200指数）
        return True
    if (re.fullmatch(r"[0-9]{1,2}", raw) and int(raw) <= 31
            and after == "日" and text[end + 1:end + 2] != "元"):
        return True          # 裸日号日期残片（"10日，"/"10日下午"；"100日元"不误伤）
    if (re.fullmatch(r"[0-9]{1,2}", raw) and 1 <= int(raw) <= 12
            and after == "月"):
        return True          # 裸月号日期残片（"7月，南非黄金产量…"，f98a36fe 实证）
    return False


# D19 杠杆 a：v2 扩词（纯追加——_verb_hits 同位置最长优先/同长取序号小者，
# 既有 48 词序号不动是 v1 行为保持的机制保证；词条清单由 S1 金标 fn 闭合表
# 驱动策展：195 个 F_event 文本实到扫描（log/temp/mw-verb-scan.py），只收
# 实到≥1 对的事件动词；R8 纪律排除说类/心理/体貌词（表示/指出/强调/回应/
# 考虑/讨论/评估/呼吁/提出/重申/持续等——防"同主体不同内容"假对齐）。
_EVENT_VERBS_V2_EXTRA: tuple[str, ...] = (
    # 市场行情
    "上涨", "下跌", "上升", "升至", "增长", "下降", "回落", "收于", "收报",
    "收盘", "开盘", "涨停", "跌停", "持平", "突破", "走低", "走高", "下挫",
    "走强", "走弱", "上行", "拉升", "震荡", "放缓", "缓和", "改善", "延续",
    "扩大", "暴跌", "攀升", "下滑", "加速", "创新高",
    # 宏观政策
    "宣布", "调整", "实施", "印发", "通过", "审议", "维持", "补贴", "部署",
    "预计", "预测", "决定", "征收", "取消", "生效", "暂停", "停止", "重启",
    "到期",
    # 人事组织
    "任命", "选举", "退休", "接替",
    # 一般事件
    "举行", "成立", "启动", "达成", "恢复", "结束", "投产", "交付", "出席",
    "调查", "采用", "致辞", "会晤", "事故", "受伤", "失联", "上线", "成交",
    "组建", "拘留", "死亡", "谈判", "量产",
    # 续行轮二轮（10:4x，18 词；mw-verb-scan2.py 位置扫描+68 对逐对目击策展，
    # 表内缺席逐一核实、R8 纪律维持：表示类/评估讨论类言论动词仍排除，
    # 该类残余登记 D19 §五 R8-residual）：
    # 市场行情（跌超/涨超复合与裸单字 跌/涨——最长匹配先消费 下跌/跌停/上涨/
    # 涨停，裸词仅在前者未覆盖处触发，fp 以回放硬闸仲裁）
    "跌超", "涨超", "收跌", "收涨", "低开", "高开", "推高", "压低", "跌", "涨",
    # 宏观数据（库存增减/证券买卖流/票据发行/数据修正）
    "减少", "增加", "买进", "卖出", "发行", "修正",
    # 一般事件（政策永久化/评级授予）
    "永久化", "给予",
    # 三轮（10:9x，2 词；S4 RU-B 行逐对实证）：降——7f9b64cf C'降至'单侧缺 fact
    # （表内升至有/降至无不对称；最长匹配先消费 下降/降低，裸'降'补'降至/降息'型）；
    # 获任——b7873ee5 H'获任'vs C'任命'（人事任命类同族）
    "降", "获任",
)
_EVENT_VERBS_V2: tuple[str, ...] = _EVENT_VERBS + _EVENT_VERBS_V2_EXTRA

# 极性前缀（设计书 §2.4：否定词表 + 完成/将来词表，动词紧邻左侧最长匹配）
_POLARITY_PREFIXES: tuple[str, ...] = (
    "未曾", "不会", "不再", "并未", "已经",   # 二字优先（最长匹配）
    "未", "不", "无", "已", "将", "拟", "正", "取消",
)
# D19 续行（v2）：纯体标记（已经/已/将/拟/正）非极性——modality 槽已独立
# 承载；v2 极性前缀仅保留否定类+取消（0db05a98"已升至"vs"升至"实证：
# 体标记折入 polarity raw 致签发门 raw 比较双侧异串）。将/拟的计划义由
# modality 槽守护（壁若比较 modality 则不误判等价；实测仲裁 tn/fp）。
_POLARITY_PREFIXES_V2: tuple[str, ...] = (
    "未曾", "不会", "不再", "并未",           # 二字优先（最长匹配）
    "未", "不", "无", "取消",
)

# 否定词松散窗词表（R9 外部审核 F1 修复，主窗口 06:5x）：紧邻前缀未命中时，
# 动词左侧 0~4 码点窗内补抓否定词——"未完成回购"中"未"被"完成"隔开，
# 紧邻实现双侧 polarity 同为"回购"，正反事件被 B3 壁 g 放过判等价（阻断级）。
# 仅否定词享受松散窗（误抓代价=边界，安全方向）；肯定/完成前缀仍紧邻。
_POLARITY_NEGATION_LOOSE: tuple[str, ...] = (
    "未曾", "不会", "不再", "并未", "不曾", "无非",
    "未", "不", "无", "非",
)

# 模态词表（设计书 §2.5：动词左侧 0~4 码点窗口）
_MODALITY_WORDS: tuple[str, ...] = (
    "计划", "预计", "可能", "有望",
    "已", "将", "拟", "或",
)
# D19 续行（v2）：已完成体"已"非模态差异——"已升至"与"升至"同一事件
# （0db05a98 实证：polarity 对称化后 modality 已-vs-missing 挂签发门）；
# 将/拟/计划等未来/计划义保留（时态阶段真差异，tn 守护实测）。
_MODALITY_WORDS_V2: tuple[str, ...] = (
    "计划", "预计", "可能", "有望",
    "将", "拟", "或",
)

# 引述归属（设计书 §2.6）
_ATTRIBUTION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"据([^，。；：]{2,20}?)(?:报道|透露|消息)"),
    re.compile(r"([一-龥A-Za-z]{2,15}?)(?:表示|称|宣布|公告称)"),
)

# 时间 expression 三档（设计书 §2.7；长形态在前防前缀截断）
_TIME_EXPRESSION = re.compile(
    r"[0-9]{4}年[0-9]{1,2}月[0-9]{1,2}日"
    r"|[0-9]{4}-[0-9]{1,2}-[0-9]{1,2}"
    r"|[0-9]{1,2}月[0-9]{1,2}日"
    r"|(?:[0-9]{4}年)?(?:上半年|下半年|第[一二三四]季度|[一二三四]季度)"
    r"|(?:本|上|下)季度"
    r"|今日|昨日|前天|明天|本周[一二三四五六日天]?|上周[一二三四五六日天]?|本月|上月"
)
_TIME_STAGE_WORDS: tuple[str, ...] = (
    "开盘", "收盘", "盘中", "早盘", "尾盘", "盘前", "盘后", "午间",
)
# 阶段词表扩充（用户 2026-09-28 裁定批准；09 §8.3 阶段冲突规格+外部审计 R2-H2 后续）：
# 比较层 _STAGES/_STAGE_CONFLICTS 早已认识 同比/环比/初值/终值（value_time L73-81），
# 抽取侧此前不产出致规格不可达。v2 门控追加（D19 杠杆先例：v1 腿逐字节不变）。
_TIME_STAGE_WORDS_V2_EXTRA: tuple[str, ...] = (
    "同比", "环比", "初值", "终值", "修正值", "月初", "月末",
)

# 主体（设计书 §2.1：P1 引号/书名号 → P2 机构后缀 → P3 小词典；窗口=动词前同子句）
# 主窗口实现注：设计书 P2 字面 {2,24}? 非贪婪有两处不符意图——①下限 2 使"甲公司"
# 类单字前缀机构失配；②非贪婪在"甲集团股份有限公司"止于"甲集团"短匹配。实装改为
# 消费型左界 + 贪婪 {1,24}（贪婪回退停在最后一个合法后缀=最长机构名），意图不变。
_SUBJECT_QUOTED = re.compile(r"“([^”]{2,30})”|《([^》]{2,30})》")
# P-E 货币对主体（S5 候补，54b54410/e1140ee8/ec96308c 实证）：X/Y 与 X兑Y
# 两类构式；**置于 P2 之前**（"欧元/美元在欧洲央行公布政策后…"窗内 P2 会
# 抢欧洲央行而真主体是货币对——54b54410 双侧同改实证；v2-only 不破 v1 零漂移，
# 优先级破例登记 D19 §5.11）
_SUBJECT_FX_V2 = re.compile(r"([一-龥A-Za-z]{2,8}(?:/|兑)[一-龥A-Za-z]{2,8})")
_SUBJECT_ORG = re.compile(
    r"(?:^|[，,、：:；;。！？!?\s“”\"'（）()])"
    r"((?:发改委|工信部|财政部|商务部|央行|证监会|银保监会)"  # 部委全名单独立（无前缀形态）
    r"|[一-龥A-Za-z0-9（）()·]{1,24}"
    r"(?:委员会|交易所|发改委|工信部|财政部|商务部|证监会|银保监会"
    r"|协会|公司|集团|股份|银行|基金|证券|保险|信托|央行|局|署|部))"
)
_SUBJECT_SMALL_DICT: tuple[str, ...] = (
    "美联储", "证监会", "中国", "美国", "欧盟", "日本", "俄罗斯", "英国",
)

# D19 杠杆 a（v2 主体扩展，仅在 dict_version=RULE_DICT_VERSION_V2 时咨询，
# 且一律排在 v1 三优先级之后——v1 已命中的窗口行为逐字节不变）：
# P2a 机构后缀扩展（研究所/高校/医院/媒体/政体等通用机构后缀）。
_SUBJECT_ORG_V2 = re.compile(
    r"(?:^|[，,、：:；;。！？!?\s“”\"'（）()])"
    r"([一-龥A-Za-z0-9（）()·]{1,24}"
    r"(?:研究所|研究院|科学院|社科院|研究中心|大学|学院|医院"
    r"|电视台|广播电台|报社|通讯社|出版社|政府|议会|国会"
    r"|法院|检察院|商会|工会|联合会|总会|学会|事务所"
    r"|株式会社|基金会|理事会))"
)
# P2a-v2c 机构后缀二扩（S5 候补 P-D，~12 对命中 ~7 高置信）：重工/科技/信息/
# 家居/租赁/资本/百货/联盟/企业/合伙企业/项目/工程/厂/业务；该/此/其前缀
# 命中作废（"该企业"类泛指回指非实体，S5 fp 方向注记）
_SUBJECT_ORG_V2C = re.compile(
    r"(?:^|[，,、：:；;。！？!?\s“”\"'（）()])"
    r"([一-龥A-Za-z0-9（）()·]{1,24}"
    r"(?:合伙企业|重工|科技|信息|家居|租赁|资本|百货|联盟|企业"
    r"|项目|工程|厂|业务|平台))")
    # 356b6f84"数据跨境综合服务平台"实证补 平台（该/此/其前缀守卫
    # 防"该平台"类回指误取，S5 fp 注记纪律）
# P2b 职务+人名（职务词强制在场——裸人名匹配噪音面过大，裸名走 P3a 名单）。
# S3 B-2 修复：人名段懒惰 + 说类动词/介词/标点/窗尾右界——"主席陈某表示，…放缓"
# 型（说类词在窗内）不再吞"表示"；右界不含汉字时按窗尾收（行长拉加德✓）。
_SUBJECT_PERSON_V2 = re.compile(
    r"(?:^|[，,、：:；;。！？!?\s“”\"'（）()])"
    r"((?:首席执行官|行长|主席|总统|总理|首相|部长|主任|总裁|董事长"
    r"|秘书长|发言人|大臣|司令|将军|院士|教授|副行长|副主席|副总统"
    r"|副部长|副主任)"
    r"[一-龥]{2,4}?(?:·[一-龥]{1,4})?"
    r"(?=表示|说道|指出|宣布|强调|回应|呼吁|警告|称"
    r"|[在于到从就对为与和跟同向朝将已曾正拟不未曾]"
    r"|[，,、：:；;。！？!?\s“”\"'（）()]|$))"
)
# P2c 金融工具/标的名词（S_NULL=97 主攻：美元指数/国债收益率/股价型；
# 双侧须独立抽出同串才对齐——泛化主体的失败方向是边界，fp 安全）。
_SUBJECT_INSTRUMENT_V2 = re.compile(
    r"(?:^|[，,、：:；;。！？!?\s“”\"'（）()])"
    r"([一-龥A-Za-z0-9（）()·]{1,24}"
    r"(?:指数|收益率|汇率|股价|金价|油价|国债|美债|期货|现货"
    r"|基金|股票|债券|利率|原油))"
)
# P2c 后处理：贪婪窗吞入前导期限数字（"5年期国债收益率"）会与时间/数值
# evidence span 重叠（回放实测 EVIDENCE_INVALID×3，uat073524）——确定性剥除
# 前导日期/期限段；剥空则该命中作废（宁 missing 不重叠）。
# f98a36fe 实证扩展：中文数字（"南非七月份黄金产量"七月份为前导）+月份
_INSTRUMENT_LEADING_SPAN = re.compile(
    r"^[0-9一二三四五六七八九十]{1,4}"
    r"(?:年|个月|月份|月|日|周|天|季度)?期?")
# P3a 知名公众人名/无后缀单实体名小词典（F 类文本裸名扫描策展，
# log/temp/mw-name-scan.py；只收确定单实体、排除歧义名如"福特"）。
_SUBJECT_SMALL_DICT_V2_EXTRA: tuple[str, ...] = (
    "拉加德", "特朗普", "拉平斯基", "阿吉翁", "蒋松荣", "卡什卡利",
    "刘伟平", "阿勒卡比", "卢拉", "董昕", "郭嘉昆", "哈塞特", "李开复",
    "摩根士丹利", "伊朗",
    # S5 P-C 扩展（11:0x，s5-snull-attribution.md §5 Top1，金标 F 类裸名逐对
    # 目击策展，命中 ~21 对）：顺序即优先——"标普"须在"蒙牛"前（cf03eea0
    # "标普将蒙牛展望调整"窗内先中标普=施事正确；反序则取客体蒙牛）。
    "标普", "苹果", "摩根大通", "英伟达", "月之暗面", "诺和诺德",
    "麦克莫兰铜金", "山河药辅", "金牌家居", "安恒信息", "长鑫科技",
    "蒙牛", "日产汽车", "梅西百货", "欧佩克", "港交所", "贝森特",
    "台积电", "碧桂园",
    # S5 P-G 国家裸名（d914bf18 巴西/edf227b9 韩国，P3 原有 伊朗 同族）
    "巴西", "韩国",
    # S5 Top3 期货裸名（b69be43c"沪银跌超5%"等目击策展）
    "沪银", "沪铜", "国际铜", "沪锌", "纯苯", "SC原油",
)

# P-B 板块/概念+个股列举主体（S5 §5 Top3，~10 对命中 9 高置信）：右界
# 行情词硬约束（非列举语境不抽），尾段内取最后命中
_SUBJECT_ENUM_V2 = re.compile(
    r"([一-龥A-Za-z0-9]{2,10}(?:板块|概念(?:板块)?)"
    r"|(?:[一-龥A-Za-z]{2,8}、)+[一-龥A-Za-z]{2,8}(?:等个股|等)?)"
    r"(?=均|纷纷|跟涨|跟跌|涨停|跌停|涨超|跌超|涨幅|跌幅|持续)")
    # 6e300679"港股大模型概念股持续走弱"实证补 持续
# 单个股构式说明：S5 原案 lookahead（涨停等在窗外动词上）于尾段内永假，
# 实现改以"窗外动词∈行情四词"为闸（见 _subject_slot P-B 段）

# P3b ASCII 专名（S5 §5 Top1②）：左界=子句首/标点/空白，右界须接汉字
# （防普通英文词；[一-龥] 已涵盖 宣布/预计/正/据 等），窗内取最后命中。
_SUBJECT_ASCII_V2 = re.compile(
    r"(?:^|[，,、：:；;。!?\s（(])"
    r"([A-Z][A-Za-z0-9-]{1,29}(?:[.-][A-Za-z0-9]+)*)"
    r"(?=[一-龥])")
    # 068cad23"D-Matrix"实证：连字符专名（首段纳入 -；右界汉字约束不变）

# P-A 指标/度量名词主体（S5 §5 Top2，~16 对命中 ~8-9 高置信）：消费型贪婪
# 骨架同 P2c；长后缀先排（持仓比例先于持仓）；前导时间/期限段剥除复用
# _INSTRUMENT_LEADING_SPAN。04244d96"核心通胀"实证补 通胀。
_SUBJECT_METRIC_V2 = re.compile(
    r"[一-龥A-Za-z0-9（）()·]{0,24}"
    r"(?:持仓比例|市占率|客流量|销售额|成交额|交易量|搜索量|净利润|"
    r"价格|产量|销量|运力|票房|均价|占比|成本|通胀|预期|持仓)")
    # 前缀 {0,24}：07877ce8"客流量"裸后缀（零前缀）实证；尾段约束+最低
    # 优先级降噪，零前缀不误升（命中仅限尾段窗内）

# P3a/P3b 尾段副词回退词表（腿⑱回归修复）：尾段以此类词开头=新子句
# 无主体（承前省略），允许回搜全窗继承主语；实词开头（新主体在场）不回退。
_ADVERBIAL_TAIL_STARTS: tuple[str, ...] = (
    "下一步", "同时", "此外", "另外", "目前", "近日", "近期", "此前",
    "早前", "稍早", "早盘", "午后", "尾盘", "盘前", "盘后", "隔夜",
    "今日", "昨日", "明天", "当天", "当日", "随后", "计划", "预计",
    "将", "正", "已", "拟", "也", "还", "并", "且", "而", "但",
    "不过", "然而", "因此", "所以", "如果", "若", "一旦", "尽管",
    "虽然", "即使", "随着", "在", "据",
    # 腿⑲→⑳ 逐对实证追加：较（"较2026年的预测…"比较状语无主体，
    # e812df30）；我们/我方/我（言语转述内第一人称=说话主体自身承前，
    # a3431f33"特朗普表示，我们将…"）
    "较", "我们", "我方", "我",
)

# 关键对象（设计书 §2.8：书名号/引号 → 名词短语 → 证券代码；窗口=动词后同子句）
_KEY_OBJECT_QUOTED = re.compile(r"《([^》]{2,30})》|“([^”]{2,30})”")
_KEY_OBJECT_NOUN = re.compile(
    r"(?:本公司|公司)?"
    r"(?:可转债|股票|股份|债券|合同|协议|项目|订单|产品|产能|牌照|股权|资产|批文)"
)
_KEY_OBJECT_CODE = re.compile(r"(?<![0-9])[036][0-9]{5}(?![0-9])")

# 数值五模式（设计书 §2.9；区间先行，重叠取最长）
_NUM_CORE = r"[+-]?[0-9]+(?:,[0-9]{3})*(?:\.[0-9]+)?"
_NUM_AMOUNT = re.compile(
    r"(?:人民币|美元|港元|欧元)?" + _NUM_CORE + r"(?:亿|万|千|百)?"
    r"(?:元|美元|港元|欧元)?"
)
_NUM_PERCENT = re.compile(r"[+-]?[0-9]+(?:\.[0-9]+)?\s?[%％]")
_NUM_QUANTITY = re.compile(
    _NUM_CORE + r"(?:亿|万|千|百)?"
    r"(?:千瓦时|平方米|股|手|吨|桶|台|辆|家|笔|单|人)"
)
# D19 E5（v2）：补"点"单位（指数点位 279.90点 型，1ff90985 实证）。
_NUM_QUANTITY_V2 = re.compile(
    _NUM_CORE + r"(?:亿|万|千|百)?"
    r"(?:千瓦时|平方米|股|手|吨|桶|台|辆|家|笔|单|人|点)"
)
_NUM_CHINESE = re.compile(r"[零〇一二两三四五六七八九十百千万亿]+(?=元|股|吨|手|%|％|家|笔)")
_NUM_RANGE_JOIN = re.compile(r"[-~至到]")

_MAGNITUDE_WORDS: tuple[str, ...] = ("亿", "万", "千", "百")
_CURRENCY_WORDS: tuple[str, ...] = ("人民币", "美元", "港元", "欧元")
_METRIC_WORDS: tuple[str, ...] = (
    "营业收入", "净利润", "成交额", "成交量", "营收", "市值", "股价", "价格",
    "产量", "销量", "产能", "利率", "股息", "分红",
)
_COMPARATOR_WORDS: tuple[str, ...] = (
    "不低于", "不少于", "超过", "高于", "低于", "不足",
    ">=", "<=", "≥", "≤", "约", "近", "超", ">", "<",
)
_APPROXIMATE_WORDS: frozenset[str] = frozenset({"约", "近"})
_DIRECTION_WORDS: tuple[str, ...] = (
    "上涨", "下跌", "增长", "下降", "减少", "增加", "上调", "下调",
)


class RuleExtractionError(ValueError):
    """规则内部自检失败（evidence 与原文不符等），属实现 bug，提前暴露。"""


def _missing() -> dict:
    return {"status": "missing", "raw_value": None, "evidence": []}


def _present(record_id: str, field: str, raw: str, start: int, end: int,
             text: str, *, extra: Mapping[str, Any] | None = None) -> dict:
    """present 槽 + 构造期硬自检（设计书 §1.0 硬纪律 1）。"""
    if not raw or not (0 <= start < end <= len(text)) or text[start:end] != raw:
        raise RuleExtractionError(
            f"evidence self-check failed for {field}: "
            f"text[{start}:{end}]={text[start:end]!r} != {raw!r}"
        )
    slot: dict[str, Any] = {
        "status": "present",
        "raw_value": raw,
        "evidence": [{"record_id": record_id, "field": field,
                      "quote": raw, "start": start, "end": end}],
    }
    if extra:
        slot.update(extra)
    return slot


def _ev(record_id: str, field: str, quote: str, start: int, end: int,
        text: str) -> dict:
    """裸 evidence 条目 + 自检。"""
    if not (0 <= start < end <= len(text)) or text[start:end] != quote:
        raise RuleExtractionError(
            f"evidence self-check failed for {field}: "
            f"text[{start}:{end}]={text[start:end]!r} != {quote!r}"
        )
    return {"record_id": record_id, "field": field,
            "quote": quote, "start": start, "end": end}


def _clauses(text: str) -> list[tuple[int, int]]:
    """子句 span 列表（跳过全空白子句）。"""
    spans: list[tuple[int, int]] = []
    for match in _CLAUSE.finditer(text):
        if text[match.start():match.end()].strip():
            spans.append((match.start(), match.end()))
    return spans


def _false_verb_hit_v2(clause_text: str, index: int, verb: str) -> bool:
    """S4 Top3（v2）：复合名词/名词用法伪命中抑制（实证驱动，逐条登记）。

    - 发布+会：新闻发布会/发布会 中的"发布"是构词成分非事件动词
      （7d5b3215/a445c599/e1e50cfa 实证：伪命中把窗口切进"会上，证监会副主席
      李超表示"致 C 侧主体丢失）；
    - 公告+显示："公告显示"为名词用法（0ef2f947/3a45ae4d 实证）。
    注意："公告称"未抑制（公告称，… 的后续内容即公告事件本身，语义可通）。
    """
    end = index + len(verb)
    if verb == "发布" and clause_text[end:end + 1] == "会":
        return True
    if verb == "公告" and clause_text[end:end + 2] == "显示":
        return True
    if (verb == "公告"
            and re.match(r"[0-9]{4}年", clause_text[end:end + 5])):
        # 公告+文号 = 文件题名名词用法（"商务部公告2026年第38号"=
        # 公告这份文件本身，db623984 双侧对称化实证）
        return True
    if verb == "降" and clause_text[end:end + 1] == "噪":
        return True          # 降噪=复合构词（13b66903"主动降噪"实证）
    # 涨幅/跌幅抑制已撤销（ef943f64 承重实证第四次回归拦截）："跌幅超5%"
    # 在行情文本中承载真实事件（下跌>5%）——名词化但非伪命中；dd94bb27
    # （CPO概念）与 044281ff（单位守卫后 None 主体对称）经实测不依赖该抑制。
    if verb == "成交" and clause_text[end:end + 1] == "额":
        return True          # 成交额=名词构词（37c8b881 S5 ④池实证）
    return False


def _verb_hits(clause_text: str, clause_start: int,
               verbs: tuple[str, ...] = _EVENT_VERBS,
               dict_version: str = RULE_DICT_VERSION) -> list[tuple[int, int, str]]:
    """子句内事件动词命中（同位置最长匹配，同长取词典序号小者），按偏移升序。"""
    hits: list[tuple[int, int, str]] = []
    index = 0
    while index < len(clause_text):
        best: tuple[int, str] | None = None  # (dict_order, verb)
        for order, verb in enumerate(verbs):
            if clause_text.startswith(verb, index):
                if best is None or len(verb) > len(best[1]):
                    best = (order, verb)
        if best is not None:
            if (dict_version == RULE_DICT_VERSION_V2
                    and _false_verb_hit_v2(clause_text, index, best[1])):
                index += len(best[1])        # 消费但不出命中（伪命中抑制）
                continue
            hits.append((clause_start + index, clause_start + index + len(best[1]),
                         best[1]))
            index += len(best[1])
        else:
            index += 1
    return hits


def _subject_slot(record_id: str, field: str, text: str,
                  window_start: int, window_end: int,
                  dict_version: str = RULE_DICT_VERSION) -> dict:
    """P1 → P2 → (v2: P2a/P2b) → P3 → (v2: P3a)，窗口内最后一个命中；
    失败 missing（不编造通用主体）。v2 扩展一律排在 v1 优先级之后。"""
    window = text[window_start:window_end]
    hit: tuple[str, int, int] | None = None
    if dict_version == RULE_DICT_VERSION_V2:
        # P-A 优先道（04244d96 实证）：机构后缀与指标同窗时 P2 抢机构致
        # 双侧异串（"欧洲央行行长拉加德表示，核心通胀将…"C 侧得欧洲央行、
        # H 侧得核心通胀）——指标谓词（缓和/升至/维持…）语境下指标才是真
        # 主体；破例置于 P1 前，尾段约束+剥除规则与后置 P-A 一致，
        # 实测仲裁（D19 §5.11 登记）
        tail_start_p = max(window.rfind(m) for m in "，。：；,")
        tail_p = window[tail_start_p + 1:]
        tail_off_p = window_start + tail_start_p + 1
        # 工具名守卫（b7ee377c 回归拦截）：尾段含期货类工具名时让位 P2c
        # ——"NYMEX柴油期货价格"的工具名是词典锚实体，比"价格"指标更具体
        if not _SUBJECT_INSTRUMENT_V2.search(tail_p):
            for match in _SUBJECT_METRIC_V2.finditer(tail_p):
                raw = match.group(0)
                trimmed = _INSTRUMENT_LEADING_SPAN.sub("", raw)
                trimmed = re.sub(r"^(?:目前|当前|最新|全天|日内|将|正|拟)",
                                 "", trimmed)
                if len(trimmed) < 2:
                    continue
                hit = (trimmed,
                       tail_off_p + match.start() + (len(raw) - len(trimmed)),
                       tail_off_p + match.end())
    if hit is None and dict_version == RULE_DICT_VERSION_V2:
        for match in _SUBJECT_FX_V2.finditer(window):
            # 计量单位对拒绝（"美元/盎司"是计价单位非货币对——
            # 044281ff/ea2bd891 实证；真货币对右操作数必为币种）
            if re.search(r"/(?:盎司|桶|克|吨|手|台|辆|立方米|磅)$",
                         match.group(1)):
                continue
            hit = (match.group(1), window_start + match.start(1),
                   window_start + match.end(1))
    if hit is None:
        for match in _SUBJECT_QUOTED.finditer(window):
            raw = match.group(1) or match.group(2)
            if raw:
                hit = (raw,
                       window_start + match.start(1 if match.group(1) else 2),
                       window_start + match.end(1 if match.group(1) else 2))
    if hit is None:
        for match in _SUBJECT_ORG.finditer(window):
            hit = (match.group(1), window_start + match.start(1),
                   window_start + match.end(1))
    if hit is None and dict_version == RULE_DICT_VERSION_V2:
        for match in _SUBJECT_ORG_V2.finditer(window):
            hit = (match.group(1), window_start + match.start(1),
                   window_start + match.end(1))
    if hit is None and dict_version == RULE_DICT_VERSION_V2:
        for match in _SUBJECT_ORG_V2C.finditer(window):
            if match.group(1).startswith(("该", "此", "其")):
                continue                     # 泛指回指非实体（S5 fp 注记）
            hit = (match.group(1), window_start + match.start(1),
                   window_start + match.end(1))
    if hit is None and dict_version == RULE_DICT_VERSION_V2:
        for match in _SUBJECT_PERSON_V2.finditer(window):
            hit = (match.group(1), window_start + match.start(1),
                   window_start + match.end(1))
    if hit is None and dict_version == RULE_DICT_VERSION_V2:
        for match in _SUBJECT_INSTRUMENT_V2.finditer(window):
            raw = match.group(1)
            trimmed = _INSTRUMENT_LEADING_SPAN.sub("", raw)
            # 连词前缀剥除（2564d3cc"且日本养老基金"实证——并列连词从
            # 窗头漏入贪婪前缀；真名以且/而/也/还起头者不存在于实测域）
            trimmed = re.sub(r"^(?:且|而|也|还)", "", trimmed)
            if len(trimmed) < 2:
                continue  # 剥除后为空/过短——跳过该命中，不取
            hit = (trimmed,
                   window_start + match.start(1) + (len(raw) - len(trimmed)),
                   window_start + match.end(1))
    if hit is None:
        for word in _SUBJECT_SMALL_DICT:
            pos = window.rfind(word)
            if pos >= 0:
                hit = (word, window_start + pos, window_start + pos + len(word))
                break
    if hit is None and dict_version == RULE_DICT_VERSION_V2:
        # S5 P-C/P3b 尾段约束（2a2ca73d 实证）：主体窗跨逗号列举时，裸名/ASCII
        # 只搜最后一个标点之后的尾段——"英伟达下跌2.52%，博通下跌0.90%"中
        # 博通-fact 的窗口若全搜会把"英伟达"错挂给博通（宁 missing 不错挂）。
        # 副词回退（腿⑱ 实证 -7 回归）：尾段以副词/情态/时间/承接词开头=
        # 新子句无主体（承前省略），回搜全窗继承主语（04244d96/0501dffd
        # "表示，计划将…"构式）；尾段自带候选或实词开头（博通/就业）不回退。
        # 尾段切分符不含"、":列举项间顿号切开会把"群核科技、智谱、云知声"
        # 切成"云知声"（09db5f15 实证 P-B 全灭根因）；列举共享同一动词，
        # 顿号右切无防错挂价值，逗号已承担分句切分
        tail_start = max(window.rfind(mark) for mark in "，。：；,")
        tail = window[tail_start + 1:] if tail_start >= 0 else window
        tail_offset = window_start + (tail_start + 1 if tail_start >= 0 else 0)
        for word in _SUBJECT_SMALL_DICT_V2_EXTRA:
            pos = tail.rfind(word)
            if pos >= 0:
                hit = (word, tail_offset + pos, tail_offset + pos + len(word))
                break
        if hit is None:
            for match in _SUBJECT_ASCII_V2.finditer(tail):
                hit = (match.group(1), tail_offset + match.start(1),
                       tail_offset + match.end(1))
        if hit is None:
            # 回退判定：①空尾段（动词句首构式）——仅当窗尾（逗号前）是言语
            #   动词（"…卢拉表示，取消X…"，7197be1a 实证）才继承说话者；
            #   列举断项（"…高通下跌2.79%，跌2.40%"窗尾是数值）不继承
            #   （2a2ca73d 实证，错挂英伟达诱发 VERIFIED_CONFLICT）；
            # ②尾段副词/承接词开头=新子句无主体（承前省略），回搜全窗。
            if not tail:
                # 报=报价动词（"FDR001报1.4100%，上涨4.00个基点"——上涨主体
                # 承前被报价实体，77f9d696 实证），与言语动词同闸
                fallback = bool(re.search(r"(?:表示|称|说|指出|强调|透露|报)$",
                                          window.rstrip("，、。：；, ")))
            else:
                fallback = tail.startswith(_ADVERBIAL_TAIL_STARTS)
            if fallback:
                for word in _SUBJECT_SMALL_DICT_V2_EXTRA:
                    pos = window.rfind(word)
                    if pos >= 0:
                        hit = (word, window_start + pos,
                               window_start + pos + len(word))
                        break
                if hit is None:
                    for match in _SUBJECT_ASCII_V2.finditer(window):
                        hit = (match.group(1), window_start + match.start(1),
                               window_start + match.end(1))
    if hit is None and dict_version == RULE_DICT_VERSION_V2:
        # P-A 指标名词（最低优先级——实体性最弱，仅在前七级全 miss 后启用）；
        # 同样尾段约束（列举窗内指标串也在尾段）
        for match in _SUBJECT_METRIC_V2.finditer(tail):
            raw = match.group(0)
            trimmed = _INSTRUMENT_LEADING_SPAN.sub("", raw)
            # 时间状语前缀剥除（07877ce8 实证：H"目前运力"vs C"当前运力"，
            # 状语入主体致双侧异串——剥后同串"运力"）；
            # 0865a01e"将2026年经济增长预期"实证补 将/正/拟（模态前缀漏入）
            trimmed = re.sub(r"^(?:目前|当前|最新|全天|日内|将|正|拟)", "",
                             trimmed)
            # 中缀剥除不可行（f98a36fe/2f2aab6a 实证 crash）：中缀删字后
            # raw≠text[start:end]，违 evidence 单段 span 契约——中缀异写
            # 走词典别名通道（subj-0009/0010，D21）
            if len(trimmed) < 2:
                continue
            hit = (trimmed,
                   tail_offset + match.start() + (len(raw) - len(trimmed)),
                   tail_offset + match.end())
    if hit is None and dict_version == RULE_DICT_VERSION_V2:
        # P-B 板块/个股列举（右界行情词硬约束）→ 单个股紧邻构式，尾段最后命中
        for match in _SUBJECT_ENUM_V2.finditer(tail):
            raw_e = match.group(1)
            # 时段前缀剥除（946dabee"早盘折叠屏概念"实证——前导剥除同
            # _INSTRUMENT_LEADING_SPAN 模式，start 前移保持 evidence 契约）
            trim_e = re.sub(r"^(?:早盘|午后|尾盘|盘前|盘后|隔夜)", "", raw_e)
            if len(trim_e) < 2:
                continue
            hit = (trim_e,
                   tail_offset + match.start(1) + (len(raw_e) - len(trim_e)),
                   tail_offset + match.end(1))
        if (hit is None
                and text[window_end:window_end + 2] in ("涨停", "跌停", "涨超", "跌超")):
            # 单个股构式：动词在窗外（window_end=verb_start）， lookahead 在
            # 尾段内永假——改以"窗外动词∈行情四词"为闸（水发燃气+涨停实证）；
            # 尾部时间状语剥除（59871c8c"南京港连续两日"实证）
            # 59871c8c 实证扩展："实现连续两个交易日涨停"——实现+两+个交易日
            stripped = re.sub(r"(?:(?:实现)?连续(?:两|[一三四五六七八九十0-9]+)"
                              r"个?(?:交易日|日|天)|再度|再次|盘中)$",
                              "", tail)
            # 连词前缀剥除（2564d3cc"且日本养老基金"实证——"且/而/也/还"
            # 漏入纯名构式）；start 右移前导长、end 锚尾部剥除后位置，
            # 保持 raw==text[start:end] evidence 契约
            lead_stripped = re.sub(r"^(?:且|而|也|还)", "", stripped)
            if re.fullmatch(r"[一-龥A-Za-z]{2,8}", lead_stripped):
                hit = (lead_stripped,
                       tail_offset + (len(stripped) - len(lead_stripped)),
                       tail_offset + len(stripped))
        if (hit is None
                and text[window_end:window_end + 2] in (
                        "震荡", "走弱", "走强", "拉升", "下挫", "走高", "走低")):
            # 板块/概念+行情动词紧邻构式（946dabee"折叠屏概念震荡"实证——
            # 概念后缀在尾段末尾、动词在窗外）；28bf2309"存储芯片概念盘中
            # 走强"实证：先剥尾部时段词再锚尾
            tail_v = re.sub(r"(?:盘中|早盘|午后|尾盘|盘前|盘后|隔夜)$",
                            "", tail)
            tail_m = re.search(r"([一-龥A-Za-z0-9]{2,10}"
                               r"(?:板块|概念(?:板块)?))$", tail_v)
            if tail_m:
                hit = (tail_m.group(1), tail_offset + tail_m.start(1),
                       tail_offset + tail_m.end(1))
    if hit is None:
        return _missing()
    raw, start, end = hit
    if dict_version == RULE_DICT_VERSION_V2:
        # D19 S_DIFF 清理（v2，确定性 span 重选，新 span 仍是原文真实子串）：
        # ① 证券代码前缀+"旗下"（"苏宁环球（000718）旗下苏亚医美…"）→ 取旗下后段；
        # ② 尾部未闭合括号段（"迪拜金融服务管理局（迪拜金管局"）→ 截去括号残段。
        code_prefix = re.match(r"^.+（[036][0-9]{5}）旗下(.+)$", raw)
        doc_prefix = re.match(r"^[0-9]{4}年第[0-9]+号(.+)$", raw)
        if code_prefix and len(code_prefix.group(1)) >= 2:
            start = end - len(code_prefix.group(1))
            raw = code_prefix.group(1)
        elif doc_prefix and len(doc_prefix.group(1)) >= 2:
            # ③ 文号前缀（"2026年第38号商务部"→商务部，db623984 实测）
            start = end - len(doc_prefix.group(1))
            raw = doc_prefix.group(1)
        else:
            open_paren = raw.rfind("（")
            if open_paren >= 2 and "）" not in raw[open_paren:]:
                end = start + open_paren
                raw = raw[:open_paren]
        # ④ 事件动词+单字后缀尾贪婪截除（e722c443 实证：P2a 贪婪尾把
        #    前一动词吃进主体——"美国食品药品监督管理局（FDA）发布部"，截自
        #    动词起；守卫：动词后仅余单字且属主体后缀字，"国家开发银行"
        #    （开发后余两字）不误切）。
        for verb in _EVENT_VERBS_V2:
            pos = raw.find(verb)
            if (pos >= 2 and len(raw) - (pos + len(verb)) == 1
                    and raw[-1] in "部局署司厅处院所会行"):
                raw = raw[:pos]
                end = start + pos
                break
        # ⑤ 尾部"比例"剥除（a7b19826 实证：H"…多头持仓比例"vs C"…多头持仓"
        #    金标异写，'比例'为可省度量后缀，剥后双侧同串对齐）
        if raw.endswith("比例") and len(raw) > 4:
            raw = raw[:-2]
            end = start + len(raw)
    return _present(record_id, field, raw, start, end, text)


def _polarity_slot(record_id: str, field: str, text: str,
                   verb_start: int, verb_end: int, verb: str,
                   dict_version: str = RULE_DICT_VERSION) -> dict:
    """紧邻左侧最长前缀；无前缀 → 否定词松散窗补抓；再无 → 动词本身。"""
    prefixes = (_POLARITY_PREFIXES_V2
                if dict_version == RULE_DICT_VERSION_V2
                else _POLARITY_PREFIXES)
    for prefix in prefixes:
        start = verb_start - len(prefix)
        if start >= 0 and text[start:verb_start] == prefix:
            raw = text[start:verb_end]
            return _present(record_id, field, raw, start, verb_end, text)
    # R9 F1：否定词松散窗（0~4 码点，取窗内最左命中使极性片段最大化）——
    # "未完成回购"型（否定词被完成态助词隔开）必须产出异于肯定的 raw。
    window_start = max(0, verb_start - 4)
    best: int | None = None
    for word in _POLARITY_NEGATION_LOOSE:
        pos = text.find(word, window_start, verb_start)
        while pos >= 0:
            if best is None or pos < best:
                best = pos
            pos = text.find(word, pos + 1, verb_start)
    if best is not None:
        raw = text[best:verb_end]
        return _present(record_id, field, raw, best, verb_end, text)
    return _present(record_id, field, verb, verb_start, verb_end, text)


def _modality_slot(record_id: str, field: str, text: str, verb_start: int,
                   dict_version: str = RULE_DICT_VERSION) -> dict:
    """动词左侧 0~4 码点窗口：紧邻（end==verb_start）最长优先，否则窗内末命中。"""
    words = (_MODALITY_WORDS_V2 if dict_version == RULE_DICT_VERSION_V2
             else _MODALITY_WORDS)
    window_start = max(0, verb_start - 4)
    best_adjacent: tuple[int, str] | None = None
    best_loose: tuple[int, str] | None = None
    for word in words:
        search = window_start
        while True:
            pos = text.find(word, search, verb_start)
            if pos < 0:
                break
            if pos + len(word) == verb_start:
                if best_adjacent is None or len(word) > len(best_adjacent[1]):
                    best_adjacent = (pos, word)
            elif best_loose is None or pos > best_loose[0]:
                best_loose = (pos, word)
            search = pos + 1
    chosen = best_adjacent or best_loose
    if chosen is None:
        return _missing()
    pos, word = chosen
    return _present(record_id, field, word, pos, pos + len(word), text)


_ATTR_GENERIC_SOURCES_V2: tuple[str, ...] = (
    "业界", "市场", "市场消息", "媒体", "外媒", "知情人士",
    "消息", "据悉", "公开资料", "业内",
)


def _attr_generic_suppress_v2(slot: dict, dict_version: str) -> dict:
    """D19 续行（v2）：泛来源引述归 missing——"据业界消息"类来源不携带
    实体信息，其显隐是文体差异（3beee066 H"据业界消息"vs C 无 实证）；
    具体具名来源（财联社/道琼斯/公司公告…）保留。"""
    if (dict_version != RULE_DICT_VERSION_V2
            or slot.get("status") != "present"):
        return slot
    if (slot.get("raw_value") or "") in _ATTR_GENERIC_SOURCES_V2:
        return _missing()
    return slot


def _attribution_slot(record_id: str, field: str, text: str,
                      clause_start: int, clause_end: int) -> dict:
    """两模式子句内第一处命中；未命中 missing。"""
    clause_text = text[clause_start:clause_end]
    for pattern in _ATTRIBUTION_PATTERNS:
        match = pattern.search(clause_text)
        if match and match.group(1):
            raw = match.group(1)
            start = clause_start + match.start(1)
            return _present(record_id, field, raw, start, start + len(raw), text)
    return _missing()


def _time_slots(record_id: str, base: str, text: str,
                clause_start: int, clause_end: int,
                dict_version: str = RULE_DICT_VERSION
                ) -> tuple[dict, dict, dict, list[tuple[int, int]]]:
    """expression 第一处命中 / stage 词表 / anchor 恒 missing；返回占用的数值排除窗。

    D19 E4（v2）：排除窗覆盖**全部**时间表达式命中（v1 仅第一处）——"9月10日…
    比9月9日上升"型第二日期成分不再漏进 numerics（71be651f 实证）。
    """
    clause_text = text[clause_start:clause_end]
    expression = _missing()
    exclusion: list[tuple[int, int]] = []
    match = _TIME_EXPRESSION.search(clause_text)
    if match:
        raw = match.group(0)
        start = clause_start + match.start()
        end = clause_start + match.end()
        expression = _present(record_id, base + ".time.expression",
                              raw, start, end, text)
        exclusion.append((start, end))
        if dict_version == RULE_DICT_VERSION_V2:
            for extra in _TIME_EXPRESSION.finditer(clause_text, match.end()):
                exclusion.append((clause_start + extra.start(),
                                  clause_start + extra.end()))
    stage = _missing()
    stage_words = _TIME_STAGE_WORDS
    if dict_version == RULE_DICT_VERSION_V2:
        # v2 扩充词表并入扫描（同一定顺序语义：按词表序首个命中造槽；
        # v1 腿词表不变=行为逐字节保持）
        stage_words = _TIME_STAGE_WORDS + _TIME_STAGE_WORDS_V2_EXTRA
    for word in stage_words:
        pos = clause_text.find(word)
        if pos >= 0:
            start = clause_start + pos
            stage = _present(record_id, base + ".time.stage",
                             word, start, start + len(word), text)
            break
    return expression, stage, _missing(), exclusion


def _rewrap_slot_base(slot: dict, record_id: str, field: str,
                      text: str) -> dict:
    """present 槽字段路径重卷（W2Fα1-E5 双算消重配套）：同 raw/span 换
    field 名，经 _present 构造期自检（零正则重扫）；missing 槽原样返回。"""
    if slot.get("status") != "present":
        return slot
    evidence = slot["evidence"][0]
    return _present(record_id, field, slot["raw_value"],
                    evidence["start"], evidence["end"], text)


def _key_object_slot(record_id: str, field: str, text: str,
                     window_start: int, window_end: int
                     ) -> tuple[dict, tuple[int, int] | None]:
    """动词后同子句窗口：书名号/引号 → 名词短语 → 证券代码；返回占用 span。"""
    window = text[window_start:window_end]
    match = _KEY_OBJECT_QUOTED.search(window)
    if match:
        raw = match.group(1) or match.group(2)
        group_index = 1 if match.group(1) else 2
        start = window_start + match.start(group_index)
        end = window_start + match.end(group_index)
        return (_present(record_id, field, raw, start, end, text), (start, end))
    match = _KEY_OBJECT_NOUN.search(window)
    if match:
        raw = match.group(0)
        start = window_start + match.start()
        end = window_start + match.end()
        return (_present(record_id, field, raw, start, end, text), (start, end))
    match = _KEY_OBJECT_CODE.search(window)
    if match:
        raw = match.group(0)
        start = window_start + match.start()
        end = window_start + match.end()
        return (_present(record_id, field, raw, start, end, text), (start, end))
    return _missing(), None


def _decimal_text(raw_core: str) -> str:
    """去千分位逗号的十进制字符串（设计书 §2.9 value 规则）。"""
    return raw_core.replace(",", "")


def _word_left(text: str, words: tuple[str, ...], end: int,
               window: int) -> tuple[str, int, int] | None:
    """左窗 [end-window, end] 内命中：间隙 0 优先，同间隙取最长（设计书 0~4 窗）。"""
    start_floor = max(0, end - window)
    for gap in range(0, window + 1):
        anchor = end - gap
        best: tuple[str, int, int] | None = None
        for word in words:
            pos = anchor - len(word)
            if pos >= start_floor and text[pos:anchor] == word:
                if best is None or len(word) > len(best[0]):
                    best = (word, pos, anchor)
        if best is not None:
            return best
    return None


def _metric_near(text: str, num_start: int, num_end: int
                 ) -> tuple[str, int, int] | None:
    """数值前后 0~6 码点内指标名词典第一处（先左后右、先近后远）。"""
    left_start = max(0, num_start - 6)
    best: tuple[str, int, int] | None = None
    for word in _METRIC_WORDS:
        pos = text.rfind(word, left_start, num_start)
        if pos >= 0 and (best is None or pos > best[1]):
            best = (word, pos, pos + len(word))
    if best is not None:
        return best
    right_end = min(len(text), num_end + 6)
    for word in _METRIC_WORDS:
        pos = text.find(word, num_end, right_end)
        if pos >= 0:
            return (word, pos, pos + len(word))
    return None


class _NumericCandidate:
    """数值候选（五模式归一后的中间形态）。"""

    def __init__(self, *, start: int, end: int, core: str, core_start: int,
                 core_end: int, chinese: bool, magnitude: str | None,
                 unit: str | None, unit_start: int | None, unit_end: int | None,
                 currency: str | None, currency_span: tuple[int, int] | None,
                 range_end_core: str | None,
                 range_span: tuple[int, int] | None) -> None:
        self.start = start
        self.end = end
        self.core = core
        self.core_start = core_start
        self.core_end = core_end
        self.chinese = chinese
        self.magnitude = magnitude
        self.unit = unit
        self.unit_span = (unit_start, unit_end) if unit_start is not None else None
        self.currency = currency
        self.currency_span = currency_span
        self.range_end_core = range_end_core
        self.range_span = range_span

    def overlaps(self, other: "_NumericCandidate") -> bool:
        return self.start < other.end and other.start < self.end


def _numeric_candidates(clause_text: str, clause_start: int,
                        dict_version: str = RULE_DICT_VERSION
                        ) -> list[_NumericCandidate]:
    """五模式候选（区间先行；同起点取最长）；相对坐标转全文坐标。

    D19 E1/E5（v2）：区间连接加单位相容检查（"1%至279.90点"跨单位不连接）；
    数量模式换用 _NUM_QUANTITY_V2（补"点"单位）。v1 路径逐字节不变。
    """
    candidates: list[_NumericCandidate] = []

    def add_amount_like(match: re.Match[str],
                        pattern_kind: str) -> "_NumericCandidate | None":
        # W2Fα1（WA1a-E6）：实返追加的候选引用（core 不可识别时 None）——
        # 调用方原以 candidates[-1] 脆弱假设挂载区间右端（本函数早返时
        # candidates[-1] 是陈旧候选/空列表 IndexError）。
        raw = match.group(0)
        start = clause_start + match.start()
        end = clause_start + match.end()
        core_match = re.search(_NUM_CORE if pattern_kind != "chinese" else
                               r"[零〇一二两三四五六七八九十百千万亿]+", raw)
        if core_match is None:
            return None
        core = core_match.group(0)
        core_start = start + core_match.start()
        core_end = start + core_match.end()
        tail = raw[core_match.end():]
        magnitude = None
        unit = None
        unit_span: tuple[int, int] | None = None
        if pattern_kind == "percent":
            unit = tail.strip()[-1:] if tail.strip() else None
        elif tail[:3] in ("千瓦时", "平方米"):
            unit = tail[:3]               # 复合单位整体，"千" 不拆作量级
        elif tail:
            if tail[0] in _MAGNITUDE_WORDS:
                magnitude = tail[0]
            rest = tail[1:] if magnitude else tail
            unit = rest or None
        if unit:
            unit_start = end - len(unit)
            unit_span = (unit_start, end)
        currency = None
        currency_span: tuple[int, int] | None = None
        head = raw[:core_match.start()]
        for word in _CURRENCY_WORDS:
            if head.endswith(word):
                currency = word
                currency_span = (start, start + len(word))
                break
        candidate = _NumericCandidate(
            start=start, end=end, core=core, core_start=core_start,
            core_end=core_end, chinese=(pattern_kind == "chinese"),
            magnitude=magnitude, unit=unit,
            unit_start=unit_span[0] if unit_span else None,
            unit_end=unit_span[1] if unit_span else None,
            currency=currency, currency_span=currency_span,
            range_end_core=None, range_span=None)
        candidates.append(candidate)
        return candidate

    # 区间先行：单端点匹配后探测连接符+第二端点
    quantity_pattern = (_NUM_QUANTITY_V2 if dict_version == RULE_DICT_VERSION_V2
                        else _NUM_QUANTITY)
    occupied: list[tuple[int, int]] = []
    singles: list[re.Match[str] | tuple[str, re.Match[str]]] = []
    for pattern, kind in ((_NUM_PERCENT, "percent"), (quantity_pattern, "quantity"),
                          (_NUM_AMOUNT, "amount"), (_NUM_CHINESE, "chinese")):
        for match in pattern.finditer(clause_text):
            singles.append((kind, match))
    singles.sort(key=lambda item: (item[1].start(), -(item[1].end() - item[1].start())))

    accepted: list[tuple[str, re.Match[str]]] = []
    for kind, match in singles:
        span = (match.start(), match.end())
        if any(span[0] < occ[1] and occ[0] < span[1] for occ in occupied):
            continue
        # 区间探测：连接符 + 紧随的第二端点（同模式族近似）
        tail = clause_text[match.end():match.end() + 24]
        join = _NUM_RANGE_JOIN.match(tail)
        range_core = None
        range_span = None
        if join and kind != "chinese":
            second = re.match(_NUM_CORE, tail[join.end():])
            if second:
                range_core = second.group(0)
                range_span = (clause_start + match.end() + join.end(),
                              clause_start + match.end() + join.end() + len(range_core))
        candidate = add_amount_like(match, kind)
        if candidate is None:
            # 核不可识别（E6 实返 None）：不产生候选，区间右端无处可挂——
            # 按未连接记账占用（修复前 candidates[-1] 会挂到陈旧候选上）。
            occupied.append((match.start(), match.end()))
            accepted.append((kind, match))
            continue
        if range_core is not None and dict_version == RULE_DICT_VERSION_V2:
            # D19 E1：单位相容检查——右端单位与左端不同且非空 → 放弃连接
            # （"下跌1%至279.90点" % 与 点 不相容；右端无单位则继承允许）。
            right_after = tail[join.end() + len(range_core):]
            right_unit_match = re.match(r"[%％]|[一-龥]{1,3}", right_after)
            right_unit = right_unit_match.group(0) if right_unit_match else ""
            left_unit = candidate.unit or ""
            if right_unit not in ("", left_unit):
                range_core = None
                range_span = None
        if range_core is not None:
            candidate.range_end_core = range_core
            candidate.range_span = range_span
            candidate.end = range_span[1]
            occupied.append((match.start(),
                             match.end() + join.end() + len(range_core)))  # type: ignore[name-defined]
        else:
            occupied.append((match.start(), match.end()))
        accepted.append((kind, match))
    return candidates


def _numeric_entry(record_id: str, base: str, numeric_id: str,
                   candidate: _NumericCandidate, text: str,
                   clause_time_expression: dict,
                   clause_time_span: tuple[int, int] | None,
                   clause_text: str, clause_start: int,
                   dict_version: str = RULE_DICT_VERSION) -> dict:
    """单条 numeric 12 字段 + value 槽内联 P15 消费键（设计书 §2.9 + 主窗口契约注）。"""
    nbase = f"{base}.numerics.{numeric_id}"
    core = candidate.core
    decimal = None if candidate.chinese else _decimal_text(core)

    comparator_word = None
    comparator_span: tuple[int, int] | None = None
    left_hit = _word_left(text, _COMPARATOR_WORDS, candidate.start, 4)
    if left_hit is not None:
        comparator_word, comp_start, comp_end = left_hit
        # D19 E3（v2）："约"前字为构词字（合约/条约/契约/盟约/公约/签约/续约/
        # 毁约/违约）时是否定——"主力合约下跌0.08%"的 约 不是比较符。
        if (dict_version == RULE_DICT_VERSION_V2 and comparator_word == "约"
                and comp_start > 0
                and text[comp_start - 1] in "合条契盟公签续毁违"):
            comparator_word = None
        else:
            comparator_span = (comp_start, comp_end)

    metric_hit = _metric_near(text, candidate.core_start, candidate.core_end)

    direction_hit: tuple[str, int, int] | None = None
    for word in _DIRECTION_WORDS:
        pos = clause_text.find(word)
        if pos >= 0:
            direction_hit = (word, clause_start + pos, clause_start + pos + len(word))
            break

    # numeric 级 evidence = 覆盖 比较符+数字+量级+单位 的最小连续真实 span
    span_start = candidate.start
    if comparator_span is not None and comparator_span[1] == candidate.start:
        span_start = comparator_span[0]
    span_end = candidate.end
    numeric_quote = text[span_start:span_end]
    numeric_evidence = _ev(record_id, nbase, numeric_quote, span_start, span_end, text)

    if candidate.chinese:
        inline_comparator = "="
        inline_range = None
    elif candidate.range_end_core is not None:
        inline_comparator = "range"
        inline_range = _decimal_text(candidate.range_end_core)
    elif comparator_word is not None:
        inline_comparator = comparator_word
        inline_range = None
    else:
        inline_comparator = "="
        inline_range = None

    # D19 续行（v2）：量级在场时"元"为记账默认单位——"1000亿元"与"1000亿"
    # 等价（37c8b881 实证：双侧同一数值因 元 显隐不对称挂 NUMERIC 闸）；
    # 元-only（无量级，"1000元"）保留。归一在抽取侧、v2 门控，v1 不动。
    eff_unit = candidate.unit
    if (dict_version == RULE_DICT_VERSION_V2 and eff_unit == "元"
            and candidate.magnitude):
        eff_unit = None

    value_slot = _present(
        record_id, nbase + ".value", core, candidate.core_start,
        candidate.core_end, text,
        extra={
            "value": decimal,
            "unit": eff_unit or "",
            "magnitude": candidate.magnitude or "",
            "currency": candidate.currency,
            "role": None,                      # 设计书 §2.9：role 一律 missing
            "metric": metric_hit[0] if metric_hit else None,
            "comparator": inline_comparator,
            "approximate": comparator_word in _APPROXIMATE_WORDS
                           if comparator_word else False,
            "range_end": inline_range,
        },
    )

    if candidate.range_end_core is not None and candidate.range_span is not None:
        range_slot = _present(
            record_id, nbase + ".range_end", candidate.range_end_core,
            candidate.range_span[0], candidate.range_span[1], text,
            extra={"value": _decimal_text(candidate.range_end_core)},
        )
    else:
        range_slot = _missing()

    magnitude_slot = _missing()
    if candidate.magnitude:
        magnitude_slot = _present(
            record_id, nbase + ".magnitude", candidate.magnitude,
            candidate.core_end, candidate.core_end + len(candidate.magnitude), text)

    unit_slot = _missing()
    if eff_unit and candidate.unit_span is not None:
        unit_slot = _present(record_id, nbase + ".unit", eff_unit,
                             candidate.unit_span[0], candidate.unit_span[1], text)

    currency_slot = _missing()
    if candidate.currency and candidate.currency_span is not None:
        currency_slot = _present(record_id, nbase + ".currency", candidate.currency,
                                 candidate.currency_span[0], candidate.currency_span[1],
                                 text)

    metric_slot = _missing()
    if metric_hit is not None:
        metric_slot = _present(record_id, nbase + ".metric", metric_hit[0],
                               metric_hit[1], metric_hit[2], text)

    comparator_slot = _missing()
    if comparator_word is not None and comparator_span is not None:
        comparator_slot = _present(record_id, nbase + ".comparator", comparator_word,
                                   comparator_span[0], comparator_span[1], text)

    direction_slot = _missing()
    if direction_hit is not None:
        direction_slot = _present(record_id, nbase + ".direction", direction_hit[0],
                                  direction_hit[1], direction_hit[2], text)

    time_slot = _missing()
    if clause_time_span is not None and clause_time_expression.get("status") == "present":
        raw = clause_time_expression["raw_value"]
        time_slot = _present(record_id, nbase + ".time", raw,
                             clause_time_span[0], clause_time_span[1], text)

    return {
        "numeric_id": numeric_id,
        "evidence": [numeric_evidence],
        "metric": metric_slot,
        "value": value_slot,
        "range_end": range_slot,
        "magnitude": magnitude_slot,
        "unit": unit_slot,
        "currency": currency_slot,
        "role": _missing(),                  # 设计书 §2.9：role 一律 missing（W8）
        "comparator": comparator_slot,
        "direction": direction_slot,
        "time": time_slot,
    }


def extract_facts(record_id: str, text: str, *,
                  dict_version: str = RULE_DICT_VERSION) -> list[dict]:
    """规则式 P14 抽取基线主入口（签名与设计书 §3.2 一致）。

    - record_id 必须匹配 ^[0-9a-f]{64}$，否则抛 ValueError；
    - 空文本/全空白 → []（T024 空正文边界域，由调用方判空接线）；
      W2Fα1（C3）补充：分隔符-only/无实义子句文本（"。"型，零子句）
      归同一声明路径 → []（不足证据，不抛 IndexError）；
    - 非空无命中 → 恰 1 个 fallback minimal fact（设计书 §4 裁定：避免
      build_aligned 零 Fact 侧 PairAlignmentError 冒泡成技术失败；全 missing
      语义="规则未命中"，非"正文确实未表达"）；
    - `dict_version`（D19 杠杆 a）：默认 RULE_DICT_VERSION（v1，行为与历史
      逐字节一致）；RULE_DICT_VERSION_V2 启用动词表扩词与主体扩展模式
      （v2 扩展一律排在 v1 优先级之后）；其他取值 raise ValueError。
    """
    if not isinstance(record_id, str) or not _RECORD_ID_RE.fullmatch(record_id):
        raise ValueError("record_id must match ^[0-9a-f]{64}$")
    if not isinstance(text, str):
        raise TypeError("text must be str")
    if dict_version not in (RULE_DICT_VERSION, RULE_DICT_VERSION_V2):
        raise ValueError(f"dict_version must be one of "
                         f"{(RULE_DICT_VERSION, RULE_DICT_VERSION_V2)!r}")
    if not text.strip():
        return []
    verbs_table = (_EVENT_VERBS_V2 if dict_version == RULE_DICT_VERSION_V2
                   else _EVENT_VERBS)

    facts: list[dict] = []
    last_org_subject: tuple[str, int, int] | None = None
    for clause_start, clause_end in _clauses(text):
        clause_text = text[clause_start:clause_end]
        verbs = _verb_hits(clause_text, clause_start, verbs_table,
                           dict_version=dict_version)
        if not verbs:
            # S5 ② 回填源补种（21dac765 实证）：言语子句无事件动词→无
            # fact→机构主体无从登记——对 表示/称/说 子句独立抽取一次主体
            # 补种（窗口=子句起点→言语动词；机构后缀守卫同上）
            if dict_version == RULE_DICT_VERSION_V2:
                sp = re.search(r"(?:表示|称|说|指出|强调|透露)", clause_text)
                if sp:
                    seed = _subject_slot(record_id, "facts.seed.subject",
                                         text, clause_start,
                                         clause_start + sp.start(),
                                         dict_version=dict_version)
                    if seed.get("status") == "present":
                        raw_s = seed.get("raw_value") or ""
                        if raw_s.endswith(("局", "部", "委", "司", "署",
                                           "厅", "办", "所", "院", "会")):
                            ev_s = seed["evidence"][0]
                            last_org_subject = (raw_s, ev_s["start"],
                                                ev_s["end"])
            continue

        # 数值排除窗只需时间 span（各 fact 时间槽在动词循环内按自身 base 重建）
        # W2Fα1（WA1a-E5）：_time_slots 每子句只算一次——修复前 1+N 次双算
        # （子句级一次取排除窗 + 每动词按 base 重扫同一子句正则）；动词循环
        # 内改为字段路径重卷（_rewrap_slot_base：同 raw/span 换 base，零正则
        # 重扫，产出逐字节同形）。
        _e, _s, _a, time_exclusion = _time_slots(
            record_id, "facts.fX", text, clause_start, clause_end,
            dict_version=dict_version)

        code_spans: list[tuple[int, int]] = []
        key_object_by_verb: dict[int, tuple[dict, tuple[int, int] | None]] = {}
        for verb_start, verb_end, _verb in verbs:
            ko_slot, ko_span = _key_object_slot(
                record_id, "facts.fK.key_object", text, verb_end, clause_end)
            key_object_by_verb[verb_start] = (ko_slot, ko_span)
            if ko_span is not None and _KEY_OBJECT_CODE.fullmatch(
                    text[ko_span[0]:ko_span[1]] or ""):
                code_spans.append(ko_span)

        candidates = _numeric_candidates(clause_text, clause_start,
                                         dict_version=dict_version)
        filtered: list[_NumericCandidate] = []
        for candidate in candidates:
            if any(candidate.core_start < stop and start < candidate.core_end
                   for start, stop in time_exclusion):
                continue                                   # 日期成分排除
            if any(candidate.core_start < stop and start < candidate.core_end
                   for start, stop in code_spans):
                continue                                   # 证券代码排除
            if (dict_version == RULE_DICT_VERSION_V2
                    and _is_identifier_numeric_v2(text, candidate.core_start,
                                                  candidate.core_end)):
                continue                                   # D19 D 包：标识符数值排除
            filtered.append(candidate)

        for index, (verb_start, verb_end, verb) in enumerate(verbs):
            fact_id = f"f{len(facts) + 1}"
            base = f"facts.{fact_id}"
            expr_slot = _rewrap_slot_base(_e, record_id,
                                          base + ".time.expression", text)
            stage_slot = _rewrap_slot_base(_s, record_id,
                                           base + ".time.stage", text)
            ko_slot, _ko_span = key_object_by_verb[verb_start]
            # key_object field 重建为当前 fact 的路径
            if ko_slot.get("status") == "present":
                raw = ko_slot["raw_value"]
                ev = ko_slot["evidence"][0]
                ko_slot = _present(record_id, base + ".key_object", raw,
                                   ev["start"], ev["end"], text)
            subj_slot = _subject_slot(record_id, base + ".subject", text,
                                      clause_start, verb_start,
                                      dict_version=dict_version)
            if dict_version == RULE_DICT_VERSION_V2:
                if subj_slot.get("status") == "present":
                    raw_s = subj_slot.get("raw_value") or ""
                    if raw_s.endswith(("局", "部", "委", "司", "署", "厅",
                                       "办", "所", "院", "会")):
                        ev_s = subj_slot["evidence"][0]
                        last_org_subject = (raw_s, ev_s["start"], ev_s["end"])
                elif (last_org_subject is not None
                        and re.match(r"^(?:同时|此外|并且|另外|下一步"
                                     r"|进一步|加力|而且|另|并|将|在)",
                                     clause_text)):
                    # "在"：介词短语起句承接（21dac765 C 侧"在强化标准硬约束
                    # 方面，将加力推进…"实证）；仅在窗内无候选时触发，自带
                    # 主体的在字句不受影响
                    # S5 ② 跨子句机构主体回填（政务表述主语承前省略，
                    # 21dac765"同时，将加力推进…"实证）：承接词起句+窗内无
                    # 候选+最近在场机构主体（机构后缀守卫——人名/指标不
                    # 跨句扩散）；span 复用源发生处，evidence 单段契约成立
                    raw_s, s0, s1 = last_org_subject
                    subj_slot = _present(record_id, base + ".subject",
                                         raw_s, s0, s1, text)
            fact = {
                "fact_id": fact_id,
                "evidence": [_ev(record_id, base,
                                 text[clause_start:clause_end],
                                 clause_start, clause_end, text)],
                "fact_type": _present(record_id, base + ".fact_type", verb,
                                      verb_start, verb_end, text),
                "subject": subj_slot,
                "event_state": {
                    "predicate": _present(record_id, base + ".event_state.predicate",
                                          verb, verb_start, verb_end, text),
                    "polarity": _polarity_slot(record_id,
                                               base + ".event_state.polarity",
                                               text, verb_start, verb_end, verb,
                                               dict_version=dict_version),
                    "modality": _modality_slot(record_id,
                                               base + ".event_state.modality",
                                               text, verb_start,
                                               dict_version=dict_version),
                    "attribution": _attr_generic_suppress_v2(
                        _attribution_slot(
                            record_id, base + ".event_state.attribution",
                            text, clause_start, clause_end),
                        dict_version),
                },
                "time": {"expression": expr_slot, "stage": stage_slot,
                         "anchor": _missing()},
                "key_object": ko_slot,
                "numerics": [],
            }
            facts.append(fact)

        # 数值归属：归数字左侧最近动词的 fact；**无左动词则归右侧最近动词**
        # （23:4x 主窗口按 fp 3139b94f 根因修正："宣传费1/2"型动词前判别数字
        # 双侧对称丢弃→假等价实测 1 例；设计书 §2.9 孤儿丢弃口径原指"无动词
        # 子句"，动词前数字归右邻动词是设计意图的最小延展）。真无动词子句的
        # 孤立数字仍不抽取（本循环只在 verbs 非空时运行）。
        for candidate in filtered:
            owner = None
            for verb_start, verb_end, _verb in verbs:
                if verb_start <= candidate.core_start:
                    owner = verb_start
                else:
                    break
            if owner is None:
                owner = verbs[0][0]               # 右邻最近动词兜底（见上注）
            owner_index = next(i for i, (vs, _ve, _v) in enumerate(verbs)
                               if vs == owner)
            fact = facts[len(facts) - len(verbs) + owner_index]
            numeric_id = f"n{len(fact['numerics']) + 1}"
            expr = fact["time"]["expression"]
            expr_span = None
            if expr.get("status") == "present":
                ev = expr["evidence"][0]
                expr_span = (ev["start"], ev["end"])
            fact["numerics"].append(_numeric_entry(
                record_id, f"facts.{fact['fact_id']}",
                numeric_id, candidate, text, expr, expr_span,
                clause_text, clause_start, dict_version=dict_version))

    if not facts:
        # fallback minimal fact（设计书 §3.3/§4：真实子句 evidence + 全 missing）
        clauses = _clauses(text)
        if not any(text[s:e].strip().strip(_CLAUSE_SEPARATORS)
                   for s, e in clauses):
            # W2Fα1（C3，WA1a）：分隔符-only/无实义子句文本（"。"/" 。"型）——
            # _CLAUSE 要求子句含非分隔符码点但可携空白前缀（" 。"仍入列），
            # 此类文本零实义子句，裸 [0] 抛 IndexError 逃逸声明通道（入口
            # text.strip() 判空闸不覆盖）。归"空文本/全空白 → []"同一声明
            # 路径（不足证据，调用方判空接线），不伪造 evidence 不抛技术异常。
            return []
        first_start, first_end = clauses[0]
        return [{
            "fact_id": "f1",
            "evidence": [_ev(record_id, "facts.f1",
                             text[first_start:first_end], first_start, first_end,
                             text)],
            "fact_type": _missing(),
            "subject": _missing(),
            "event_state": {key: _missing()
                            for key in ("predicate", "polarity", "modality",
                                        "attribution")},
            "time": {key: _missing()
                     for key in ("expression", "stage", "anchor")},
            "key_object": _missing(),
            "numerics": [],
        }]
    return facts


__all__ = [
    "RULE_DICT_VERSION",
    "RULE_DICT_VERSION_V2",
    "RuleExtractionError",
    "extract_facts",
]
