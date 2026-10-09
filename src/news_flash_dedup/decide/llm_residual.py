"""P23 残判层（LLM 双向裁判 + 机器验 R0-R6）生产形态一级（窗口T）。

定位：规则链判"边界case/疑难case"的对 → 双向 LLM 判 → 机器验 → 签"重复"/
留"边界"。decide/service.py 判定核心语义冻结——本模块为独立一级，不修改
五字段输出契约、决策词汇与任何规则链组件；残判签发只在回放/审计/指标层
参与"合并口径（规则签发+残判签发）"。

判定逻辑 = D32 终版（证据 log/D32-大模型裁判三臂实验报告.md + 产物
log/temp/d32-llm-judge/）：
- 双向判：同一对按 hc（文本A=history 先）与 ch（文本位置互换）两顺序各判
  一次，**两顺序 final 均="重复"才签**；两顺序均"不重复"→不重复；其余混合
  →存疑；任一顺序 invalid→invalid 单列；任一顺序 failure→failure 单列。
- 机器验 R0-R6（decide/machine_verify.py）：默认 **audit**（全量计算不降级，
  触发规则入审计槽+疑似度排序因子）；`mv_mode="gate"` 恢复 D32 §10 闸门
  字面语义（拦截→该顺序 final=存疑）。主窗口 2026-09-28 裁定默认 audit：
  双向判自身 fp=0/51，闸门边际 fp 收益为零、tp 成本 15（净负收益），
  影子期先实测"闸门会拦什么"，证据驱动再定终态。
- LLM 工艺与 facts/llm.py 同款：DashScope 兼容端点、temperature=0、seed=42、
  response_format=json_object、超时 60s、首次+最多 3 次重试（线性退避
  1.5s×(attempt+1)）、仍败 → LlmResidualError（failure 单列，INV 式不折算
  边界）。提示词 judge_v1 终版内联为权威版（PROMPT_SHA256=add21915… 属
  内联版，单元测试钉死；落盘 judge_v1.md 原始字节为早期草稿 dcaa8085…，
  与内联版非逐字节同一——其正文经 d32_lib.load_prompt 语义（去首部注释行）
  后与内联版同一，集成钉值按此口径对拍；同 :74-77 R2-H1 勘误口径）。
- 磁盘缓存：键 = sha256(model|prompt_version|arm=A|[order=ch]|pair_id|prompt_sha)
  ——与 D32 裁判缓存同构（hc 不书 order 维度），可原位复用 D32 cache 目录
  实现复跑零 API；原子写（tmp+replace），损坏条目按 miss 处理（fail-closed）。
- judge_v2（C5R 口径加固，2026-09-30）：v1 + "统计指标/经济指标口径定义身份
  差异=关键对象差异→不重复"单句；模式闸参数 ResidualJudgeConfig.prompt_version，
  缺省 judge_v1（v1 保留可切回；版本切换经 prompt_sha 维度使缓存自然隔离）。
- 疑似度（人审分诊排序字段，入审计/扩展槽，不动五字段契约）：
  score = 0.5×(w(hc)+w(ch)) − 0.05×|两顺序触发规则并集|，
  w(重复)=1.0 / w(存疑)=0.5 / w(不重复)=0.0；任一顺序非 ok → None（单列）。

P1-a 判官可签发证明（2026-10-09 增量，decide/judge_proof.py；出处
log\判定链改造最终方案-Codex-20261008.md §4 P1-a + 终裁令-正典-1009 §五-2/3）：
- 生效面：仅 DEDUP_JUDGE_PROOF=1 开关开启的证明路（judge_proof.
  judge_proof_enabled，逐请求读 env）；开关关=本模块逐字节现役（旧缓存键、
  旧 R0-R6 audit/gate、无证明产物）。开关开时 audit 只观测不降级、gate
  按 P_* 触发降级存疑（含"不重复无机器可证伪轴"降级，任务书⑥⑦）。
- 缓存键加固（①）：证明路用 residual_cache_key_hardened——D5 裁定
  （主窗口 D1-D10 逐条 2026-10-09）按合同 §一 公式 sha256(model|
  prompt_sha|policy|order|text_a_sha|text_b_sha)，双序恒书 order 维度
  （内部词表 hc/ch）、双文 sha256 按 文本A/文本B 角色绑位、policy
  版本闭隔；pair_id/arm/prompt_version 三维出键（内容寻址）。现役
  residual_cache_key 布局一字不动供旧路原位复用。同 pair_id 换文本→
  键必异→miss 重判；旧键缓存证明路天然 miss=旧缓存不升级为签发
  凭证；缓存命中只省 LLM 调用、不省证明核验。
- 证明装配（⑤⑥）：双序原判均"重复"→VerifiedJudgeProof；均"不重复"
  →NotDuplicateProof（双侧证伪轴+失败状态枚举，证据不足→未决）。
  证明对象挂在 ResidualOutcome.proof / .not_duplicate_proof（开关关恒
  None，to_audit_dict 不含对应键=旧审计负载逐字节）。

生产异步形态（09 §3.3.4，本批只落接口骨架+设计注记；同步形态回放可测）：
- 提交在临界区外：boundary 条目先按现役链路提交（五字段契约不动），残判
  候选以 ResidualTask 入队——提交点位于 commit 临界区之外，绝不阻塞
  decide_for_task / commit_one；
- 判定异步化：worker 池按 ResidualJudgePort.submit/collect 票据式消费，
  LLM 调用预算/重试/缓存与同步形态同款；
- 合并在审计/指标层：残判签发不回写五字段主记录，合并口径（规则签发+
  残判签发）在审计/指标层物化（与金标回放 merged_metrics 同口径）；
- 失败纪律不变：连续失败 → failure 计数入指标（INV 式，不折算边界）；
  异步形态下 ticket 超时/丢失同样单列，不冒充存疑。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from . import judge_proof
from . import machine_verify

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------- 常量（D32 终版）

JUDGE_PROMPT_VERSION = "judge_v1"
JUDGE_ARM = "A"
DEFAULT_MODEL = "qwen-turbo"
DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
TIMEOUT_S = 60.0
MAX_RETRIES = 3                 # 首次 + 最多 3 次重试 = 至多 4 次调用
# W-A 族⑤(a)（A3-F1）：响应体硬上限（facts/llm.py:105-112 同族成对移植；
# embedding_client.py:66 同族命名对齐）——不静默截断、不无界读。
MAX_RESPONSE_BYTES = 1024 * 1024
MV_AUDIT = "audit"
MV_GATE = "gate"
_MV_MODES = (MV_AUDIT, MV_GATE)

DECISIONS = ("重复", "不重复", "存疑")

# judge_v1 终版（窗口 Z3 注释勘误 R2-H1：内联版为权威版，PROMPT_SHA256 钉值
# add21915… 属内联版，单元测试守卫钉死；落盘
# log/temp/d32-llm-judge/prompts/judge_v1.md 为早期草稿（hash DCAA8085…，
# 与内联版正文不同），留存取证——旧注释"与落盘文件逐字节同一"失实已更正）
JUDGE_PROMPT_V1 = """你是快讯文本去重判定裁判。给定同一新闻域内先后到达的两条快讯正文（文本A先、文本B后），判定它们是否报道同一现实事实。只以正文为输入，不假设任何标题、发布时间、来源渠道等外部信息。

【判定口径】
- 重复：两条文本指向同一现实事实，且主体对齐、事件/状态一致（或互为同义改写）、可识别的时间/阶段无差异、关键对象无差异、所有已出现的对应数值完全一致；差异仅属于格式、同义改写、信息补充或信息缺失。以下也算重复：逐字全同；仅发布/转载时间不同而内容相同；一方数值缺失另一方有数值但其余核心要素一致；一方时间缺失另一方有时间但其余核心要素一致。
- 不重复：主体、事件或事件状态、可识别时间或阶段（如开盘/收盘、初值/终值、同比/环比、当年/次年、当日/次日、不同观测时刻）、关键对象、任一对应数值（无论差异多小）、方向、事实性质（预测 vs 实际发生）中任一项存在现实上不能同时成立的差异。同一事件链中后发快讯体现时间、阶段、状态、核心数值或事实性质的有效变化，属事件更新，判不重复。
- 存疑：主体缺失无法对齐、证据不足、或无法确定关键要素是否一致。拿不准不得强判重复。

【输出契约】只输出一个 JSON 对象，禁止任何其他文字、解释或 markdown 围栏：
{
  "decision": "重复" | "不重复" | "存疑",
  "evidence_a": ["从文本A逐字摘抄的关键证据片段", "..."],
  "evidence_b": ["从文本B逐字摘抄的关键证据片段", "..."],
  "numeric_check": {
    "numbers_a": ["文本A中关键数值的原文写法"],
    "numbers_b": ["文本B中关键数值的原文写法"],
    "conclusion": "一致" | "不一致" | "无关键数值"
  },
  "time_check": {
    "times_a": ["文本A中关键时间表述的原文写法"],
    "times_b": ["文本B中关键时间表述的原文写法"],
    "conclusion": "一致" | "不一致" | "一方缺失" | "均无时间"
  },
  "reason": "判定理由（200字以内）"
}

【硬性规则】
1. evidence_a、evidence_b 均不得为空，每条至少 1 条；每条引文必须是对应原文中的连续逐字片段（至少 4 个字符），禁止改写、翻译、拼接、补全。
2. numbers_a/numbers_b/times_a/times_b 中每个元素也必须是对应原文的逐字片段；没有则给空数组 []。
3. decision 只能取 "重复"/"不重复"/"存疑" 之一。
4. 关键判断必须能从你摘抄的引文中找到依据。

【用户消息格式】
【文本A】
<文本A全文>

【文本B】
<文本B全文>"""

PROMPT_SHA256 = hashlib.sha256(JUDGE_PROMPT_V1.encode("utf-8")).hexdigest()

# judge_v2（C5R 判官口径加固窗，2026-09-30 主窗口裁定 R207）：v1 正文 + 最小
# 增量条款——不重复条款末增一句"统计指标/经济指标的口径定义身份差异属关键
# 对象差异"（C5 fp=62f74308 德国 CPI vs 调和 CPI/HICP：数值/时间全同、指标
# 口径定义身份不同，被 v1 双向签"重复"；根因=v1"关键对象差异"条款未覆盖
# 指标/口径定义身份这一差异类型，与金标 P03 登记口径②同向）。v1 保留为
# 缺省可切回（模式闸参数 ResidualJudgeConfig.prompt_version）。
JUDGE_PROMPT_VERSION_V2 = "judge_v2"
JUDGE_PROMPT_V2 = """你是快讯文本去重判定裁判。给定同一新闻域内先后到达的两条快讯正文（文本A先、文本B后），判定它们是否报道同一现实事实。只以正文为输入，不假设任何标题、发布时间、来源渠道等外部信息。

【判定口径】
- 重复：两条文本指向同一现实事实，且主体对齐、事件/状态一致（或互为同义改写）、可识别的时间/阶段无差异、关键对象无差异、所有已出现的对应数值完全一致；差异仅属于格式、同义改写、信息补充或信息缺失。以下也算重复：逐字全同；仅发布/转载时间不同而内容相同；一方数值缺失另一方有数值但其余核心要素一致；一方时间缺失另一方有时间但其余核心要素一致。
- 不重复：主体、事件或事件状态、可识别时间或阶段（如开盘/收盘、初值/终值、同比/环比、当年/次年、当日/次日、不同观测时刻）、关键对象、任一对应数值（无论差异多小）、方向、事实性质（预测 vs 实际发生）中任一项存在现实上不能同时成立的差异。同一事件链中后发快讯体现时间、阶段、状态、核心数值或事实性质的有效变化，属事件更新，判不重复。统计指标/经济指标的口径定义身份差异属关键对象差异（如 CPI 与调和 CPI/HICP、同比与环比、初值与终值/修正值、名义与实际、总量与人均）：对应指标口径定义不同即关键对象不同，即使数值、时间完全一致也不救，判不重复。
- 存疑：主体缺失无法对齐、证据不足、或无法确定关键要素是否一致。拿不准不得强判重复。

【输出契约】只输出一个 JSON 对象，禁止任何其他文字、解释或 markdown 围栏：
{
  "decision": "重复" | "不重复" | "存疑",
  "evidence_a": ["从文本A逐字摘抄的关键证据片段", "..."],
  "evidence_b": ["从文本B逐字摘抄的关键证据片段", "..."],
  "numeric_check": {
    "numbers_a": ["文本A中关键数值的原文写法"],
    "numbers_b": ["文本B中关键数值的原文写法"],
    "conclusion": "一致" | "不一致" | "无关键数值"
  },
  "time_check": {
    "times_a": ["文本A中关键时间表述的原文写法"],
    "times_b": ["文本B中关键时间表述的原文写法"],
    "conclusion": "一致" | "不一致" | "一方缺失" | "均无时间"
  },
  "reason": "判定理由（200字以内）"
}

【硬性规则】
1. evidence_a、evidence_b 均不得为空，每条至少 1 条；每条引文必须是对应原文中的连续逐字片段（至少 4 个字符），禁止改写、翻译、拼接、补全。
2. numbers_a/numbers_b/times_a/times_b 中每个元素也必须是对应原文的逐字片段；没有则给空数组 []。
3. decision 只能取 "重复"/"不重复"/"存疑" 之一。
4. 关键判断必须能从你摘抄的引文中找到依据。

【用户消息格式】
【文本A】
<文本A全文>

【文本B】
<文本B全文>"""

PROMPT_SHA256_V2 = hashlib.sha256(JUDGE_PROMPT_V2.encode("utf-8")).hexdigest()

# judge_v3（C5R 主窗口裁定：v3 授权 B+C 轻量版，2026-09-30）：v2 正文同构，
# 唯"口径定义身份"条款收窄——①口径身份差异【仅指】统计指标定义身份（闭合
# 枚举：CPI 与调和 CPI/HICP、同比与环比、月环比与年率、初值与终值/修正值、
# 名义与实际、总量与人均）→不重复；②明确排除实体载体表述（同一公司的股票/
# ADR/债券等载体形式差异、同一主体全称/简称不算口径差异——ADR priming 过严
# 病灶切除；现象驱动非追分：v2 在 P05 对 1ea81b26 SK海力士 ADRs 对 signed→
# doubtful 失 tp，根因=条款"身份"措辞过宽被泛化到实体载体身份）。v1/v2 均
# 保留可切回（模式闸参数 ResidualJudgeConfig.prompt_version 三态）。
JUDGE_PROMPT_VERSION_V3 = "judge_v3"
JUDGE_PROMPT_V3 = """你是快讯文本去重判定裁判。给定同一新闻域内先后到达的两条快讯正文（文本A先、文本B后），判定它们是否报道同一现实事实。只以正文为输入，不假设任何标题、发布时间、来源渠道等外部信息。

【判定口径】
- 重复：两条文本指向同一现实事实，且主体对齐、事件/状态一致（或互为同义改写）、可识别的时间/阶段无差异、关键对象无差异、所有已出现的对应数值完全一致；差异仅属于格式、同义改写、信息补充或信息缺失。以下也算重复：逐字全同；仅发布/转载时间不同而内容相同；一方数值缺失另一方有数值但其余核心要素一致；一方时间缺失另一方有时间但其余核心要素一致。
- 不重复：主体、事件或事件状态、可识别时间或阶段（如开盘/收盘、初值/终值、同比/环比、当年/次年、当日/次日、不同观测时刻）、关键对象、任一对应数值（无论差异多小）、方向、事实性质（预测 vs 实际发生）中任一项存在现实上不能同时成立的差异。同一事件链中后发快讯体现时间、阶段、状态、核心数值或事实性质的有效变化，属事件更新，判不重复。统计指标口径定义身份差异（闭合枚举：CPI 与调和 CPI/HICP、同比与环比、月环比与年率、初值与终值/修正值、名义与实际、总量与人均）属关键对象差异：对应指标口径定义不同即关键对象不同，即使数值、时间完全一致也不救，判不重复。实体载体与名称表述差异不构成口径差异：同一公司/主体的股票、ADR/ADRs、债券等载体形式差异，同一主体的全称/简称/译名表述差异，均不属关键对象差异，其余核心要素一致时按信息缺失或同义改写处理，判重复。
- 存疑：主体缺失无法对齐、证据不足、或无法确定关键要素是否一致。拿不准不得强判重复。

【输出契约】只输出一个 JSON 对象，禁止任何其他文字、解释或 markdown 围栏：
{
  "decision": "重复" | "不重复" | "存疑",
  "evidence_a": ["从文本A逐字摘抄的关键证据片段", "..."],
  "evidence_b": ["从文本B逐字摘抄的关键证据片段", "..."],
  "numeric_check": {
    "numbers_a": ["文本A中关键数值的原文写法"],
    "numbers_b": ["文本B中关键数值的原文写法"],
    "conclusion": "一致" | "不一致" | "无关键数值"
  },
  "time_check": {
    "times_a": ["文本A中关键时间表述的原文写法"],
    "times_b": ["文本B中关键时间表述的原文写法"],
    "conclusion": "一致" | "不一致" | "一方缺失" | "均无时间"
  },
  "reason": "判定理由（200字以内）"
}

【硬性规则】
1. evidence_a、evidence_b 均不得为空，每条至少 1 条；每条引文必须是对应原文中的连续逐字片段（至少 4 个字符），禁止改写、翻译、拼接、补全。
2. numbers_a/numbers_b/times_a/times_b 中每个元素也必须是对应原文的逐字片段；没有则给空数组 []。
3. decision 只能取 "重复"/"不重复"/"存疑" 之一。
4. 关键判断必须能从你摘抄的引文中找到依据。

【用户消息格式】
【文本A】
<文本A全文>

【文本B】
<文本B全文>"""

PROMPT_SHA256_V3 = hashlib.sha256(JUDGE_PROMPT_V3.encode("utf-8")).hexdigest()

# judge_v5（2026-10-10 提交二，p3-semantic-authority，方案 §5.2 文件 F/G）：
# policy_v3 业务口径版，正文与 SHA 单源在 decide/judge_prompt_v5.py（独立
# 文件，不改写 v1/v2/v3/v4 原文、不沿用旧 SHA）；此处仅增量注册——v1 仍
# 为全局隐式默认（可切回），新链由 judge_adapter 显式指定 v5，不依赖本
# 默认值；v4 维持不注册（其测试守卫在案）。
from news_flash_dedup.decide.judge_prompt_v5 import (
    JUDGE_PROMPT_V5,
    JUDGE_PROMPT_VERSION_V5,
    PROMPT_SHA256_V5,
)

# judge_v6（2026-10-11 一期 v6-lite，分支 p3-v6-phase1）：policy_v4 冻结
# 口径版（R8 修订改值→不重复/同值省略过程→重复/只补背景→重复；R7 主体
# 单方缺失受约束回填六条件+原因码"主体单方缺失高置信对齐"），正文与
# SHA 单源在 decide/judge_prompt_v6.py（独立文件，不改写 v1/v2/v3/v5
# 原文、不沿用旧 SHA）；此处仅增量注册——v1 仍为全局隐式默认，v5 保留
# 注册供回放对比（prompt 直解通道可达），v6 经 semantic_authority 模式
# 映射生效（judge_version_config._MODE_TO_VERSION）。默认模式仍
# legacy_proof_gate=judge_v1，生产行为零变化。
from news_flash_dedup.decide.judge_prompt_v6 import (
    JUDGE_PROMPT_V6,
    JUDGE_PROMPT_VERSION_V6,
    PROMPT_SHA256_V6,
)

_JUDGE_PROMPTS = {
    JUDGE_PROMPT_VERSION: (JUDGE_PROMPT_V1, PROMPT_SHA256),
    JUDGE_PROMPT_VERSION_V2: (JUDGE_PROMPT_V2, PROMPT_SHA256_V2),
    JUDGE_PROMPT_VERSION_V3: (JUDGE_PROMPT_V3, PROMPT_SHA256_V3),
    JUDGE_PROMPT_VERSION_V5: (JUDGE_PROMPT_V5, PROMPT_SHA256_V5),
    JUDGE_PROMPT_VERSION_V6: (JUDGE_PROMPT_V6, PROMPT_SHA256_V6),
}


def judge_prompt_for_version(version: str) -> tuple[str, str]:
    """按版本取 (提示词正文, sha256)；未知版本 fail-closed（ValueError）。"""
    try:
        return _JUDGE_PROMPTS[version]
    except KeyError:
        raise ValueError(f"未知 judge prompt 版本：{version!r}"
                         f"（合法：{sorted(_JUDGE_PROMPTS)}）") from None

_DECISION_W = {"重复": 1.0, "存疑": 0.5, "不重复": 0.0}


def judge_prompt_sha256() -> str:
    """judge_v1 内联提示词 sha256（缓存键维度；与 D32 prompts/judge_v1.md 对拍）。"""
    return PROMPT_SHA256


def residual_cache_key(model: str, prompt_version: str, pair_id: str,
                       prompt_sha: str, order: str = "hc",
                       arm: str = JUDGE_ARM) -> str:
    """缓存键 = sha256(model|prompt_version|arm|[order]|pair_id|prompt_sha)。

    与 D32 d32_lib.cache_key 同构：order=hc 不书 order 维度（hc 键与 D32 既有
    条目逐字节同键，原位复用）；order=ch 在 arm 后插入 order 维度。
    """
    if order not in ("hc", "ch"):
        raise ValueError(f"order 非法：{order!r}")
    parts = [model, prompt_version, arm]
    if order != "hc":
        parts.append(order)
    parts += [pair_id, prompt_sha]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def residual_cache_key_hardened(model: str, prompt_sha: str, order: str,
                                text_a_sha256: str, text_b_sha256: str,
                                policy_version: str = judge_proof.PROOF_POLICY_VERSION) -> str:
    """P1-a ① 证明路加固缓存键（2026-10-09 D5 裁定：按合同 §一 公式改）。

    = sha256(model|prompt_sha|policy|order|text_a_sha|text_b_sha)
    （合同 §一 cache_key 逐字公式；内部 order 词表 hc/ch 维度位同构，
    证明体内嵌 cache_key=适配层同公式+合同词表 ab/ba）

    D5 裁定（主窗口 D1-D10 逐条 2026-10-09，合同 v2 追认实现）：
    - pair_id 出键：LLM 只见双文不见 pair_id——同文同序同判=内容寻址
      合法命中；同 pair_id 换文本→双文 hash 必异→miss 重判（病灶切除
      纪律不变）；
    - prompt_version 出键：prompt_sha 内容寻址覆盖提示词版本演进；
    - arm 出键：恒 A 占位维度不再书；
    - policy 版本闭隔证明策略演进（judge_proof.PROOF_POLICY_VERSION=
      "policy_v2" 宪章版本）。
    与现役 residual_cache_key 并存不替（旧键 hc 省 order、无文本 hash——
    audit 旧路原位复用 D32 缓存的兼容纪律一字不动）；本键仅
    DEDUP_JUDGE_PROOF 开关开启的证明路消费；旧键缓存在证明路天然
    miss=旧缓存不升级为签发凭证；文本 hash 非法（非 64 位小写 hex）
    fail-closed。
    """
    if order not in ("hc", "ch"):
        raise ValueError(f"order 非法：{order!r}")
    for name, sha in (("text_a_sha256", text_a_sha256),
                      ("text_b_sha256", text_b_sha256)):
        if not (isinstance(sha, str) and len(sha) == 64
                and all(c in "0123456789abcdef" for c in sha)):
            raise ValueError(
                f"{name} 非法（须 64 位小写 hex sha256）：{sha!r}")
    parts = [model, prompt_sha, policy_version, order,
             text_a_sha256, text_b_sha256]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- 台账 / 异常

class ResidualJudgeLedger:
    """线程安全台账（api_calls/cache_hits/retries/failures/latency）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.api_calls = 0
        self.cache_hits = 0
        self.retries = 0
        self.failures = 0
        self.llm_latency_s = 0.0

    def bump(self, *, api_calls: int = 0, cache_hits: int = 0, retries: int = 0,
             failures: int = 0, latency: float = 0.0) -> None:
        with self._lock:
            self.api_calls += api_calls
            self.cache_hits += cache_hits
            self.retries += retries
            self.failures += failures
            self.llm_latency_s += latency

    def snapshot(self) -> dict:
        with self._lock:
            return {"api_calls": self.api_calls, "cache_hits": self.cache_hits,
                    "retries": self.retries, "failures": self.failures,
                    "llm_latency_s": round(self.llm_latency_s, 3)}


class LlmResidualError(RuntimeError):
    """LLM 调用不可恢复失败（重试 max_retries 次后仍败）——failure 单列。"""


# ---------------------------------------------------------------- 配置 / 结果形态

@dataclass(frozen=True)
class ResidualJudgeConfig:
    """残判层配置。mv_mode：audit（默认，机验全量计算不降级）| gate（拦截→存疑）。

    prompt_version（C5R 模式闸参数，三态）：judge_v1（默认，v1 保留可切回）|
    judge_v2（口径加固版）| judge_v3（口径身份条款收窄版）；未知版本
    fail-closed（ValueError）。
    """
    model: str = DEFAULT_MODEL
    base_url: str = DEFAULT_BASE_URL
    cache_dir: str | None = None
    mv_mode: str = MV_AUDIT
    timeout_s: float = TIMEOUT_S
    max_retries: int = MAX_RETRIES
    prompt_version: str = JUDGE_PROMPT_VERSION

    def __post_init__(self) -> None:
        if self.mv_mode not in _MV_MODES:
            raise ValueError(f"mv_mode {self.mv_mode!r} 非法（{_MV_MODES}）")
        if not self.model:
            raise ValueError("model 不能为空")
        judge_prompt_for_version(self.prompt_version)   # 未知版本 fail-closed


@dataclass(frozen=True)
class ResidualOrderOutcome:
    """单顺序判定结果。fired_rules=机验触发规则名（audit 旧路仅
    decision=="重复" 时计算，R0；证明路 重复/不重复 均核，P_* 规则 id）。

    proof_verification（2026-10-09 P1-a）：judge_proof.OrderVerification，
    仅 DEDUP_JUDGE_PROOF 开且 decision ∈ {重复,不重复} 时非 None；开关关
    恒 None（旧行为逐字节）。"""
    order: str                          # "hc" | "ch"
    status: str                         # "ok" | "invalid" | "failure"
    decision: str | None = None         # 裁判原判（结构合法时）
    final_decision: str | None = None   # mv_mode 处置后（audit=原判；gate+触发=存疑）
    fired_rules: tuple[str, ...] = ()
    interceptions: tuple[dict, ...] = ()
    cache_hit: bool = False
    latency_s: float = 0.0
    attempts: int = 0
    struct_error: str | None = None
    error: str | None = None
    proof_verification: "judge_proof.OrderVerification | None" = None


@dataclass(frozen=True)
class ResidualOutcome:
    """双向装配结果。cell：signed/not_duplicate/doubtful/invalid/failure。

    proof / not_duplicate_proof（2026-10-09 P1-a）：judge_proof 证明件，
    仅 DEDUP_JUDGE_PROOF 开时构造（双序原判均"重复"→proof；均"不重复"
    →not_duplicate_proof）；开关关恒 None，to_audit_dict 不含对应键
    （旧审计负载逐字节锚不动）。"""
    pair_id: str
    cell: str
    signed: bool
    suspicion_score: float | None
    fired_rules_union: tuple[str, ...]
    hc: ResidualOrderOutcome
    ch: ResidualOrderOutcome
    order_flip_signed: bool = False
    proof: "judge_proof.VerifiedJudgeProof | None" = None
    not_duplicate_proof: "judge_proof.NotDuplicateProof | None" = None

    def to_audit_dict(self) -> dict:
        """确定性审计负载（不含 cache_hit/latency/attempts 运行元数据——
        双腿制逐字节对拍锚；运行元数据只进 ledger）。证明键仅开关开构造
        出证明件时出现（旧路恒无=逐字节）。"""
        def _order(o: ResidualOrderOutcome) -> dict:
            return {"status": o.status, "decision": o.decision,
                    "final_decision": o.final_decision,
                    "fired_rules": list(o.fired_rules),
                    "interceptions": [json.loads(json.dumps(i, ensure_ascii=False))
                                      for i in o.interceptions],
                    "struct_error": o.struct_error, "error": o.error}
        d = {"pair_id": self.pair_id, "cell": self.cell, "signed": self.signed,
             "suspicion_score": self.suspicion_score,
             "fired_rules_union": list(self.fired_rules_union),
             "order_flip_signed": self.order_flip_signed,
             "hc": _order(self.hc), "ch": _order(self.ch)}
        if self.proof is not None:
            d["proof"] = self.proof.to_dict()
        if self.not_duplicate_proof is not None:
            d["not_duplicate_proof"] = self.not_duplicate_proof.to_dict()
        return d


@dataclass(frozen=True)
class ResidualTask:
    """异步形态提交单元（09 §3.3.4：临界区外入队）。"""
    pair_id: str
    text_history: str
    text_current: str


class ResidualJudgePort(Protocol):
    """残判层异步接口骨架（票据式；同步实现见 SyncResidualJudge）。

    生产异步形态（09 §3.3.4 设计注记，见模块 docstring）：submit 于临界区外
    入队即返票据；collect 取回判定结果。票据丢失/超时单列 failure，不冒充存疑。
    """

    def submit(self, task: ResidualTask) -> str: ...
    def collect(self, ticket: str) -> ResidualOutcome: ...


# ---------------------------------------------------------------- JSON 解析 / 结构校验（d32_lib 同款）

def parse_json_loose(content: str) -> dict:
    """容忍 ```json 围栏与首尾噪声，不容忍结构非法；返 dict 或抛 ValueError。"""
    cleaned = content.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", cleaned, re.DOTALL)
    if fence:
        cleaned = fence.group(1)
    elif not cleaned.startswith("{"):
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start == -1 or end <= start:
            raise ValueError(f"输出无 JSON 对象：{cleaned[:120]!r}")
        cleaned = cleaned[start:end + 1]
    payload = json.loads(cleaned)
    if not isinstance(payload, dict):
        raise ValueError("JSON 顶层非对象")
    return payload


def _str_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(x, str) and x.strip()
                                           for x in value)


def _opt_str_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(x, str) for x in value)


def validate_judge(payload: Mapping[str, Any]) -> tuple[dict | None, str | None]:
    """裁判输出结构校验（d32_lib.validate_judge 同款）。引文缺失/结构非法 → error。

    提交二注记（§5.2 文件 G-4）：证据字段保持必填（本层只验结构）；引文
    绑定失败自提交一起只进证据诊断（judge_proof/judge_pair），不造成语义
    invalid；真正 JSON 缺字段或 decision 非法仍由本层判 invalid。v5 输出
    契约沿用 v1 族，本函数零改动消费。
    """
    decision = payload.get("decision")
    if decision not in DECISIONS:
        return None, f"decision 非法：{decision!r}"
    ea, eb = payload.get("evidence_a"), payload.get("evidence_b")
    if not _str_list(ea) or not ea:
        return None, "evidence_a 缺失或为空或非字符串数组"
    if not _str_list(eb) or not eb:
        return None, "evidence_b 缺失或为空或非字符串数组"
    nc = payload.get("numeric_check")
    if not isinstance(nc, dict):
        return None, "numeric_check 缺失或非对象"
    if not _opt_str_list(nc.get("numbers_a")):
        return None, "numeric_check.numbers_a 非法"
    if not _opt_str_list(nc.get("numbers_b")):
        return None, "numeric_check.numbers_b 非法"
    if not isinstance(nc.get("conclusion"), str) or not nc["conclusion"].strip():
        return None, "numeric_check.conclusion 缺失"
    tc = payload.get("time_check")
    if not isinstance(tc, dict):
        return None, "time_check 缺失或非对象"
    if not _opt_str_list(tc.get("times_a")):
        return None, "time_check.times_a 非法"
    if not _opt_str_list(tc.get("times_b")):
        return None, "time_check.times_b 非法"
    if not isinstance(tc.get("conclusion"), str) or not tc["conclusion"].strip():
        return None, "time_check.conclusion 缺失"
    reason = payload.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        return None, "reason 缺失"
    return {
        "decision": decision,
        "evidence_a": [x.strip() for x in ea],
        "evidence_b": [x.strip() for x in eb],
        "numeric_check": {"numbers_a": [x.strip() for x in nc["numbers_a"] if x.strip()],
                           "numbers_b": [x.strip() for x in nc["numbers_b"] if x.strip()],
                           "conclusion": nc["conclusion"].strip()},
        "time_check": {"times_a": [x.strip() for x in tc["times_a"] if x.strip()],
                        "times_b": [x.strip() for x in tc["times_b"] if x.strip()],
                        "conclusion": tc["conclusion"].strip()},
        "reason": reason.strip(),
    }, None


# ---------------------------------------------------------------- 同步残判器

def _chat_once(*, model: str, base_url: str, api_key: str, system: str,
               user: str, timeout_s: float) -> tuple[str, float]:
    """DashScope 兼容端点 chat completions（urllib 零新依赖，facts/llm.py 工艺）。"""
    body = json.dumps({
        "model": model,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "response_format": {"type": "json_object"},
        "temperature": 0,
        "seed": 42,
    }).encode("utf-8")
    req = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions", data=body,
        headers={"Authorization": f"Bearer {api_key}",
                 "Content-Type": "application/json"})
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        # W-A 族⑤(a)（A3-F1，facts/llm.py:105-121 成对移植——类型名
        # LlmResidualError↔LlmExtractionError 域各属，工艺逐字符同形）：
        # ①1MB 硬上限（有界读+溢出抛错，不静默截断）；②坏 UTF-8/JSON
        # 包 LlmResidualError 确定性失败（_call_cached LlmResidualError
        # 短路不进瞬时重试环）。
        raw = resp.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise LlmResidualError(
                f"chat 响应体超过 1MB 硬上限（已读 {len(raw)} 字节）")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise LlmResidualError(
                f"chat 响应体非合法 UTF-8/JSON（确定性失败不重试）：{exc}"
            ) from exc
    latency = time.perf_counter() - t0
    try:
        return payload["choices"][0]["message"]["content"], latency
    except (KeyError, IndexError, TypeError) as exc:
        raise LlmResidualError(f"chat 响应形态异常：{str(payload)[:200]!r}") from exc


class SyncResidualJudge(ResidualJudgePort):
    """同步残判器（本批回放可测形态；异步形态按 ResidualJudgePort 骨架扩展）。

    api_key 懒取（api_key_fn 仅在真实调用发生时才求值——全缓存命中回放不需要
    key 文件在场）；call_fn 注入点供单元测试 mock（签名同 _chat_once 关键字
    子集 model/system/user → (content, latency_s)）。
    """

    def __init__(self, config: ResidualJudgeConfig | None = None, *,
                 api_key: str | None = None,
                 api_key_fn: Callable[[], str] | None = None,
                 call_fn: Callable[..., tuple[str, float]] | None = None,
                 ledger: ResidualJudgeLedger | None = None,
                 budget: "ProcessingBudget | None" = None) -> None:
        self.config = config or ResidualJudgeConfig()
        self._api_key = api_key
        self._api_key_fn = api_key_fn
        self._call_fn = call_fn
        self.ledger = ledger or ResidualJudgeLedger()
        # W2 ⑩①-6（N43 挂账清偿）：预算件——None=现役路径逐字节
        # （:560-576 常量 60.0/3 与 mock 三参形态零改动）。
        self._budget = budget
        # W2Fα2-7b（WA1b-L3）：_tickets 配专用锁——修复前 submit/collect
        # 裸读写 dict，并发下可丢票据/竞态弹票（fail-closed 缓解）。
        self._tickets: dict[str, ResidualOutcome] = {}
        self._tickets_lock = threading.Lock()

    # ---------- LLM 调用（缓存优先，失败单列） ----------

    def _resolve_api_key(self) -> str:
        if self._api_key:
            return self._api_key
        if self._api_key_fn is not None:
            self._api_key = self._api_key_fn()
            return self._api_key
        key = os.environ.get("QWEN_API_KEY", "")
        if not key:
            raise LlmResidualError("QWEN_API_KEY 未设置且未显式传 api_key/api_key_fn")
        self._api_key = key
        return key

    def _call_cached(self, *, pair_id: str, order: str, system: str,
                     user: str, proof_mode: bool = False,
                     text_a_sha256: str | None = None,
                     text_b_sha256: str | None = None
                     ) -> tuple[str, float, bool, int]:
        """返 (raw_content, latency_s, cache_hit, attempts)。缓存 miss → 调用+重试+落盘。

        proof_mode（2026-10-09 P1-a ①）：开关开的证明路——加固键（双文
        hash+双序+policy 版本）；读侧对拍条目 key_material 双文 hash（不符
        按 miss，复用"损坏条目 fail-closed"纪律）；写侧 key_material 补双文
        hash+policy_version。proof_mode=False=现役逐字节（旧键、旧读写面）。
        """
        cfg = self.config
        prompt_sha = judge_prompt_for_version(cfg.prompt_version)[1]
        if proof_mode:
            key = residual_cache_key_hardened(
                cfg.model, prompt_sha, order, text_a_sha256, text_b_sha256)
        else:
            key = residual_cache_key(cfg.model, cfg.prompt_version, pair_id,
                                     prompt_sha, order)
        key_material = {"model": cfg.model,
                        "prompt_version": cfg.prompt_version,
                        "arm": JUDGE_ARM, "order": order,
                        "pair_id": pair_id,
                        "prompt_sha256": prompt_sha}
        if proof_mode:
            key_material["text_a_sha256"] = text_a_sha256
            key_material["text_b_sha256"] = text_b_sha256
            key_material["policy_version"] = judge_proof.PROOF_POLICY_VERSION
        path: Path | None = None
        if cfg.cache_dir is not None:
            path = Path(cfg.cache_dir) / f"{key}.json"
            if path.exists():
                try:
                    entry = json.loads(path.read_text(encoding="utf-8"))
                    raw = entry["raw_content"]
                    if not isinstance(raw, str):
                        raise TypeError("raw_content 非 str")
                    if proof_mode:
                        # 旧缓存不升级为签发凭证：条目双文 hash 必须与本对
                        # 绑位一致（键本身已绑定，此为纵深对拍——键外维度
                        # 被手工搬入同键文件时仍按 miss 重判）。
                        km = entry.get("key_material") or {}
                        if (km.get("text_a_sha256") != text_a_sha256
                                or km.get("text_b_sha256") != text_b_sha256):
                            raise ValueError(
                                "key_material 双文 hash 与本对不符")
                    self.ledger.bump(cache_hits=1)
                    return raw, 0.0, True, 0
                except (OSError, UnicodeDecodeError, ValueError, KeyError,
                        TypeError):
                    _log.warning("residual cache entry corrupted, treated as miss: "
                                 "%s", path)      # 损坏条目按 miss（fail-closed），覆盖重写
        fn = self._call_fn
        last: Exception | None = None
        if self._budget is not None:
            # W2 ⑩①-6（N43 挂账清偿）：预算件路径——缓存命中分支恒在
            # 本分支之前（:546-553，结构性不计账/零预算冲击）；软预算执法
            # 面（越 prepare_soft_s→拒启新逻辑调用，charge 未发生）；
            # permit 驱动环（runtime_budget L15：失败或成功均扣模型额度，
            # 重试经共享追加预算授权；截断 timeout_s 覆盖现役 60.0 常量）。
            # 现役路径（下方 legacy 环）零改动，budget=None 即逐字节。
            if self._budget.beyond_prepare_soft():
                self.ledger.bump(failures=1)
                raise LlmResidualError(
                    "预算软边界：不再启动新模型调用（已启动者收尾）")
            permit = self._budget.permit()
            while True:
                ok, timeout_s = permit.next_attempt()
                if not ok:
                    self.ledger.bump(failures=1)
                    raise LlmResidualError(
                        f"LLM 调用预算拒付（共享追加/账户/总闸尽）：{last!r}")
                try:
                    self.ledger.bump(api_calls=1,
                                     retries=1 if permit.attempts > 1 else 0)
                    if fn is not None:
                        raw, latency = fn(model=cfg.model, system=system,
                                          user=user, timeout_s=timeout_s)
                    else:
                        raw, latency = _chat_once(
                            model=cfg.model, base_url=cfg.base_url,
                            api_key=self._resolve_api_key(), system=system,
                            user=user, timeout_s=timeout_s)
                    self.ledger.bump(latency=latency)
                except LlmResidualError:
                    self.ledger.bump(failures=1)
                    raise
                except Exception as exc:      # 网络/HTTP/超时等瞬时错误
                    last = exc
                    time.sleep(1.5 * permit.attempts)
                    continue
                # 缓存写块与下方现役 :590-614 同形（W3F 纪律照承）；
                # legacy 分支整体独立零改动，本分支自携一份（attempts 取
                # permit.attempts）。
                if path is not None:
                    try:
                        path.parent.mkdir(parents=True, exist_ok=True)
                        tmp = path.with_name(
                            f"{path.stem}.{os.getpid()}.{threading.get_ident()}.tmp")
                        tmp.write_text(json.dumps({
                            "key_material": key_material,
                            "request": {"model": cfg.model, "temperature": 0,
                                        "seed": 42,
                                        "system": system, "user": user},
                            "raw_content": raw,
                            "latency_s": round(latency, 3),
                            "attempts": permit.attempts,
                        }, ensure_ascii=False, indent=2), encoding="utf-8")
                        os.replace(tmp, path)
                    except Exception:
                        _log.warning("residual cache write failed, treated as "
                                     "non-cached (LLM result unchanged): %s", path)
                return raw, latency, False, permit.attempts
        for attempt in range(cfg.max_retries + 1):
            try:
                self.ledger.bump(api_calls=1, retries=1 if attempt else 0)
                if fn is not None:
                    raw, latency = fn(model=cfg.model, system=system, user=user)
                else:
                    raw, latency = _chat_once(
                        model=cfg.model, base_url=cfg.base_url,
                        api_key=self._resolve_api_key(), system=system, user=user,
                        timeout_s=cfg.timeout_s)
                self.ledger.bump(latency=latency)
            except LlmResidualError:
                # W-A 族⑤(c)（A3-F3）：确定性失败短路前计入 failures 台账——
                # 状态面 failure（_judge_order 转）/计数面 failures 拉齐；
                # 重试耗尽路径（循环外 failures+1）不经本 except，无双计。
                self.ledger.bump(failures=1)
                raise
            except Exception as exc:      # 网络/HTTP/超时等瞬时错误
                last = exc
                if attempt < cfg.max_retries:
                    time.sleep(1.5 * (attempt + 1))
                continue
            # W3F（W3b-F6，try 分离，对齐上方读侧 :408-418 同款）：缓存写
            # 独立 try 自吞自登记——写失败（共享冲突/磁盘满等）_log.warning
            # 留痕后照常返回 LLM 结果（读侧"损坏按 miss"纪律使写失条目
            # 无害，下次自然重算重写）；修复前写失败与 LLM 调用同 try，
            # 落入瞬时错误环误重试 LLM（一写失败=一次无谓调用，fail-closed
            # 成本放大），重试耗尽后 failure 误单列（LLM 实已成功）。LLM
            # 调用/重试/failure 单列语义逐字节不变；台账/快照形状不加字段
            # （metrics JSON residual_judge.ledger 段三腿锚字节面不动）。
            if path is not None:
                try:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    # W2Fα2-7b（WA1b-L3）：tmp 名带 pid/tid 唯一化——修复前
                    # tmp=path.with_suffix(".tmp") 按内容键共享，同键并发写
                    # 互踩可产损坏条目（fail-closed 缓解）；os.replace 原子
                    # 语义不变，损坏条目按 miss 的读侧纪律不变。
                    tmp = path.with_name(
                        f"{path.stem}.{os.getpid()}.{threading.get_ident()}.tmp")
                    tmp.write_text(json.dumps({
                        "key_material": key_material,
                        "request": {"model": cfg.model, "temperature": 0, "seed": 42,
                                    "system": system, "user": user},
                        "raw_content": raw,
                        "latency_s": round(latency, 3),
                        "attempts": attempt + 1,
                    }, ensure_ascii=False, indent=2), encoding="utf-8")
                    os.replace(tmp, path)
                except Exception:
                    _log.warning("residual cache write failed, treated as "
                                 "non-cached (LLM result unchanged): %s", path)
            return raw, latency, False, attempt + 1
        self.ledger.bump(failures=1)
        raise LlmResidualError(
            f"LLM 调用失败（重试 {cfg.max_retries} 次后）：{last!r}")

    # ---------- 单顺序判定 ----------

    def _judge_order(self, pair_id: str, *, order: str, text_a: str,
                     text_b: str, proof_mode: bool = False
                     ) -> ResidualOrderOutcome:
        user_msg = f"【文本A】\n{text_a}\n\n【文本B】\n{text_b}"
        prompt_text = judge_prompt_for_version(self.config.prompt_version)[0]
        try:
            raw, latency, hit, attempts = self._call_cached(
                pair_id=pair_id, order=order, system=prompt_text,
                user=user_msg, proof_mode=proof_mode,
                text_a_sha256=(judge_proof.text_sha256(text_a)
                               if proof_mode else None),
                text_b_sha256=(judge_proof.text_sha256(text_b)
                               if proof_mode else None))
        except LlmResidualError as exc:
            return ResidualOrderOutcome(order=order, status="failure",
                                        error=str(exc)[:300])
        judgment: dict | None = None
        struct_err: str | None = None
        try:
            payload = parse_json_loose(raw)
            judgment, struct_err = validate_judge(payload)
        except ValueError as exc:
            struct_err = f"JSON 解析失败：{exc}"
        if judgment is None:
            return ResidualOrderOutcome(
                order=order, status="invalid", struct_error=struct_err,
                cache_hit=hit, latency_s=round(latency, 3), attempts=attempts)
        decision = judgment["decision"]
        verification: "judge_proof.OrderVerification | None" = None
        fired: tuple[dict, ...] = ()
        if proof_mode:
            # P1-a 证明路（②③④⑥）：重复/不重复均证明级核验（存疑本已保守
            # 不送核）；触发 P_* 入审计槽，gate 降级与现役同构（:下 final）。
            # 缓存命中只省 LLM 调用——本核验每跑必从判定+原文现算，旧缓存
            # 不升级为签发凭证。
            if decision in ("重复", "不重复"):
                verification = judge_proof.verify_order_judgment(
                    order=order, judgment=judgment,
                    text_a=text_a, text_b=text_b)
                fired = verification.to_interceptions()
        else:
            # R0 闸门纪律：机器验仅闸/验判"重复"的判定（不重复/存疑本已保守）
            if decision == "重复":
                fired = tuple(machine_verify.verify_signed_judgment(
                    judgment, text_a, text_b))
        final = decision
        if self.config.mv_mode == MV_GATE and fired:
            final = "存疑"                # gate 模式：拦截→降级存疑（D32 §10 字面）
        return ResidualOrderOutcome(
            order=order, status="ok", decision=decision, final_decision=final,
            fired_rules=tuple(f["rule"] for f in fired),
            interceptions=fired, cache_hit=hit,
            latency_s=round(latency, 3), attempts=attempts,
            proof_verification=verification)

    # ---------- 双向装配（D32 终版：两顺序 final 均"重复"才签） ----------

    def judge_pair(self, pair_id: str, text_history: str,
                   text_current: str) -> ResidualOutcome:
        """残判主入口：hc（history→current）+ ch（current→history）双向判。

        P1-a（2026-10-09）：proof_mode=judge_proof.judge_proof_enabled()
        逐请求读 env（P0 开关同型纪律）；开关开→加固缓存键+证明级核验+
        证明装配（audit 只观测不降级，gate 按 P_* 降级存疑）；开关关→
        现役逐字节。"""
        proof_mode = judge_proof.judge_proof_enabled()
        hc = self._judge_order(pair_id, order="hc",
                               text_a=text_history, text_b=text_current,
                               proof_mode=proof_mode)
        ch = self._judge_order(pair_id, order="ch",
                               text_a=text_current, text_b=text_history,
                               proof_mode=proof_mode)
        fired_union = tuple(sorted(set(hc.fired_rules) | set(ch.fired_rules)))
        if hc.status == "failure" or ch.status == "failure":
            return ResidualOutcome(pair_id=pair_id, cell="failure", signed=False,
                                   suspicion_score=None,
                                   fired_rules_union=fired_union, hc=hc, ch=ch)
        if hc.status == "invalid" or ch.status == "invalid":
            return ResidualOutcome(pair_id=pair_id, cell="invalid", signed=False,
                                   suspicion_score=None,
                                   fired_rules_union=fired_union, hc=hc, ch=ch)
        f_hc, f_ch = hc.final_decision, ch.final_decision
        signed = (f_hc == "重复" and f_ch == "重复")
        if signed:
            cell = "signed"
        elif f_hc == "不重复" and f_ch == "不重复":
            cell = "not_duplicate"
        else:
            cell = "doubtful"             # 双存疑或混合（含恰一顺序签=顺序翻案）
        suspicion = round(0.5 * (_DECISION_W[hc.decision] + _DECISION_W[ch.decision])
                          - 0.05 * len(fired_union), 4)
        # P1-a ⑤⑥ 证明装配（开关开且双序结构合法）：按裁判原判（demotion
        # 前）选证明型——audit 下证明如实记录核验结果（issued=False 可观测）；
        # gate 下触发 P_* 的顺序已降级存疑（cell 自然非 signed/not_duplicate），
        # 幸存 cell 与 proof.issued 同向不变式成立。
        proof: "judge_proof.VerifiedJudgeProof | None" = None
        nd_proof: "judge_proof.NotDuplicateProof | None" = None
        if proof_mode:
            prompt_sha = judge_prompt_for_version(self.config.prompt_version)[1]
            if hc.decision == "重复" and ch.decision == "重复":
                proof = judge_proof.build_duplicate_proof(
                    pair_id=pair_id, text_history=text_history,
                    text_current=text_current,
                    hc=hc.proof_verification, ch=ch.proof_verification,
                    model=self.config.model,
                    prompt_version=self.config.prompt_version,
                    prompt_sha256=prompt_sha)
            elif hc.decision == "不重复" and ch.decision == "不重复":
                nd_proof = judge_proof.build_not_duplicate_proof(
                    pair_id=pair_id, text_history=text_history,
                    text_current=text_current,
                    hc=hc.proof_verification, ch=ch.proof_verification,
                    model=self.config.model,
                    prompt_version=self.config.prompt_version,
                    prompt_sha256=prompt_sha)
        return ResidualOutcome(
            pair_id=pair_id, cell=cell, signed=signed,
            suspicion_score=suspicion, fired_rules_union=fired_union, hc=hc, ch=ch,
            order_flip_signed=((f_hc == "重复") != (f_ch == "重复")),
            proof=proof, not_duplicate_proof=nd_proof)

    # ---------- ResidualJudgePort 骨架（同步形态：submit 即判，票据=pair_id） ----------

    def submit(self, task: ResidualTask) -> str:
        outcome = self.judge_pair(task.pair_id, task.text_history, task.text_current)
        with self._tickets_lock:
            self._tickets[task.pair_id] = outcome
        return task.pair_id

    def collect(self, ticket: str) -> ResidualOutcome:
        """取回判定结果；票据丢失/未知 → failure 单列（W2Fα2-7a，WA1b-L3）。

        修复前 `self._tickets.pop(ticket)` 对未知票据裸抛 KeyError——与
        模块 docstring"票据丢失/超时同样单列 failure，不冒充存疑"（:41）
        及 ResidualJudgePort 协议 docstring（:254）直接相悖。现按纪律
        返回 cell="failure"（双顺序 status="failure"、台账 failures+1，
        INV 式不折算边界、不冒充存疑）。
        """
        with self._tickets_lock:
            outcome = self._tickets.pop(ticket, None)
        if outcome is None:
            self.ledger.bump(failures=1)
            error = f"票据丢失或未知：{ticket!r}"[:300]
            return ResidualOutcome(
                pair_id=ticket, cell="failure", signed=False,
                suspicion_score=None, fired_rules_union=(),
                hc=ResidualOrderOutcome(order="hc", status="failure",
                                        error=error),
                ch=ResidualOrderOutcome(order="ch", status="failure",
                                        error=error))
        return outcome


__all__ = [
    "JUDGE_PROMPT_VERSION", "JUDGE_PROMPT_V1", "PROMPT_SHA256", "JUDGE_ARM",
    "JUDGE_PROMPT_VERSION_V2", "JUDGE_PROMPT_V2", "PROMPT_SHA256_V2",
    "JUDGE_PROMPT_VERSION_V3", "JUDGE_PROMPT_V3", "PROMPT_SHA256_V3",
    "JUDGE_PROMPT_VERSION_V5", "JUDGE_PROMPT_V5", "PROMPT_SHA256_V5",
    "JUDGE_PROMPT_VERSION_V6", "JUDGE_PROMPT_V6", "PROMPT_SHA256_V6",
    "DEFAULT_MODEL", "DEFAULT_BASE_URL", "DECISIONS", "MV_AUDIT", "MV_GATE",
    "judge_prompt_sha256", "judge_prompt_for_version", "residual_cache_key",
    "residual_cache_key_hardened",
    "ResidualJudgeLedger", "LlmResidualError",
    "ResidualJudgeConfig", "ResidualOrderOutcome", "ResidualOutcome",
    "ResidualTask", "ResidualJudgePort", "SyncResidualJudge",
    "parse_json_loose", "validate_judge",
]
