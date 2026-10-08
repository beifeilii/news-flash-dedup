"""N24（D28-3 谨慎版）言语包裹填充豁免单元测试（红能力测试先行）。

裁定范围（用户 D28-3 + 窗口I 一致性裁决 N24=一致，双锚：09 §7.6-7"已确认
背景/附属关系" + L846"附属/独立关系"分界）：B3 签发壁
`_fact_equivalence_provable` 条件 c（双侧 Fact 全对齐无 leftover）的拒签
路径上增设**白名单填充豁免**分支——leftover fact 全部命中豁免类 → 不计入
覆盖闸（事实不删、审计字段照记）；独立事件（independent relation）不在
豁免范围。豁免类为闭表枚举（谓词集合 + 结构特征），无开放匹配：

类① 宣布/公告/披露/发布 言语包裹（唯一实施类，逐类实测见
log/N24套话豁免实施报告.md）：
  g1 谓词 present 且 raw ∈ {宣布, 公告, 披露, 发布}（闭表）；
  g2 numerics 为零（包裹不得携带新数值——d4107222/447226bd 型实质数据拦截）；
  g3 modality 非 present（包裹不得携带模态承诺）；
  g4 D21 锚规则同纪：谓词属 norm_dict_v1 pred-0002（发布/宣布）时，对侧
     不得存在未对齐的同义类 fact（谓词-词典条目不豁免，防抢占 N27 词典
     对齐通道）；
  g5 已确认附属关系双锚（09 §7.6-7）：主 evidence span 等于同侧某已对齐
     fact 的主 evidence span（同句双重抽取实证）；
  g6 主体同值：subject 与该已对齐 fact 均 present 且逐字相等。

红绿纪律（逐条经旧码复跑验证，证据见实施报告 §五）：
- 【红能力】test_n24_wrapper_filler_exemption_signs：旧码红（覆盖闸拒签
  FACT_INCOMPLETE→聚合 RULE_UNCOVERED 边界）、新码绿（豁免后签发
  FACT_EQUIVALENT→重复）——本裁定唯一行为变更点；审计字段
  exempted_filler_facts 入账且 extra_event uncovered 审计保留双断言。
- 【红能力】test_n24_d21_dict_synonym_competition_not_exempted：旧码红
  （断言豁免不签发在旧码恒真无红能力——改断言为"豁免名单为空且仍拒签"，
  新码下豁免尝试发生但被 g4 拦回）……注：保留面用例的真实红能力由
  主用例承担；保留面断言钉死豁免边界不外溢（g2/g3/g5/g6/g4/闭表外谓词/
  独立事件逐守卫拒绝），新旧码均绿（fp 保护面零松动）。
测量依据：log/temp/winQ-n24/{probe-*-pairs,classify-summary,span-verify}.json
（v2 唯一可翻转对 78c71d5e；tn 51 集三腿零覆盖闸唯一阻断 → fp 结构面为零）。
"""

from __future__ import annotations

import hashlib

from news_flash_dedup.compare import (
    aggregate as aggregate_module,
    p15_integration,
    pair_alignment,
    pair_compare,
)
from news_flash_dedup.compare.aggregate import CoverageStatus, FrozenRecallPlan
from news_flash_dedup.decide.service import decide_once
from news_flash_dedup.facts import FactValidationReport


RECORD_ID_H = "a" * 64
RECORD_ID_C = "b" * 64


# ---------- 夹具（姿势同 test_n27_gate_g_normalize.py / test_p15_text_certificate.py） ----------

def _evidence(text, quote, field, *, record_id):
    start = text.index(quote)
    return {"record_id": record_id, "field": field, "quote": quote,
            "start": start, "end": start + len(quote)}


def _missing():
    return {"status": "missing", "raw_value": None, "evidence": []}


def _present(text, quote, field, *, record_id):
    return {"status": "present", "raw_value": quote,
            "evidence": [_evidence(text, quote, field, record_id=record_id)]}


def _time_verified_missing():
    """真 P14"原文确无"时间槽（维持签发路径不被诚实时间未知阻断，同 B3 专项）。"""
    return {"status": "missing", "raw_value": None, "evidence": [],
            "verified_missing": True}


def _numeric_present(text, quote, nid, *, record_id, fact_base="facts.f1"):
    base = f"{fact_base}.numerics.{nid}"
    return {
        "numeric_id": nid,
        "evidence": [_evidence(text, quote, base + ".value", record_id=record_id)],
        "metric": _missing(),
        "value": _present(text, quote, base + ".value", record_id=record_id),
        "range_end": _missing(),
        "magnitude": _missing(),
        "unit": _missing(),
        "currency": _missing(),
        "dimension": _missing(),
        "scope": _missing(),
        "role": _missing(),
        "comparator": _missing(),
    }


def _make_fact(text, *, fact_id="f1", subject, predicate, polarity=None,
               modality=None, quote=None, record_id=RECORD_ID_H, numerics=()):
    """quote 缺省=全文（与 test_n27 同款：同文双 fact 自然共 span）；
    quote 显式给子串 → span 不共指（独立事件/异句包裹场景）。"""
    base = f"facts.{fact_id}"
    if polarity is None:
        polarity = predicate
    modality_slot = (_present(text, modality, base + ".event_state.modality",
                              record_id=record_id) if modality else _missing())
    ev_quote = quote if quote is not None else text
    numeric_entries = [
        _numeric_present(text, q, f"n{i}", record_id=record_id, fact_base=base)
        for i, q in enumerate(numerics, 1)
    ]
    return {
        "fact_id": fact_id,
        "evidence": [_evidence(text, ev_quote, base, record_id=record_id)],
        "fact_type": _present(text, predicate, base + ".fact_type",
                              record_id=record_id),
        "subject": (_present(text, subject, base + ".subject", record_id=record_id)
                    if subject else _missing()),
        "event_state": {
            "predicate": _present(text, predicate, base + ".event_state.predicate",
                                  record_id=record_id),
            "polarity": _present(text, polarity, base + ".event_state.polarity",
                                 record_id=record_id),
            "modality": modality_slot,
            "attribution": _missing(),
        },
        "time": {"expression": _time_verified_missing(),
                 "stage": _missing(), "anchor": _missing()},
        "key_object": _missing(),
        "numerics": numeric_entries,
    }


def _make_report(text, facts, *, record_id):
    return FactValidationReport(
        record_id=record_id,
        validated_complete=bool(facts),
        extraction_status="complete",
        valid_fact_ids=tuple(f["fact_id"] for f in facts),
        numeric_inventory=(),
        issues=(),
        artifact={
            "schema_version": "1.0", "record_id": record_id,
            "offset_unit": "unicode_code_point",
            "extraction_status": "complete",
            "facts": facts, "unparsed_spans": [], "uncertainties": [],
        },
    )


def _ctx(*, record_id, item_id, text, arrival_seq):
    return {"record_id": record_id, "item_id": item_id, "text": text,
            "raw_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "scope_id": "default", "business_date": "2026-09-26",
            "arrival_seq": arrival_seq, "pipeline_version": "dedup_v1"}


def _run(history_text, history_facts, current_text, current_facts):
    """build_aligned → extract_p15 → compare_pair → aggregate（单对）。"""
    history = _make_report(history_text, history_facts, record_id=RECORD_ID_H)
    current = _make_report(current_text, current_facts, record_id=RECORD_ID_C)
    alignment = pair_alignment.build_aligned(
        history, current, history_text, current_text,
        dictionary_version="dict_v1", alignment_version="alignment_v1",
    )
    p15_report = p15_integration.extract_p15_results(
        history, current, history_text, current_text, alignment,
    )
    history_ctx = _ctx(record_id=RECORD_ID_H, item_id="item-H",
                       text=history_text, arrival_seq=1)
    current_ctx = _ctx(record_id=RECORD_ID_C, item_id="item-C",
                       text=current_text, arrival_seq=2)
    pair = pair_compare.compare_pair(
        history_ctx, current_ctx,
        history_artifact=history, current_artifact=current,
        alignment=alignment, p15_results=p15_report.p15_results,
    )
    plan = FrozenRecallPlan(version="rrf_v1", required={RECORD_ID_H: history_ctx})
    out = aggregate_module.aggregate(
        current_ctx, plan, [pair],
        coverage=CoverageStatus(visible_seq=10, prepared_seq=10, complete=True),
    )
    return pair, out, p15_report


# ---------- 场景构造 ----------

def _wrapper_pair(*, wrapper_predicate="发布", wrapper_modality=None,
                  wrapper_numerics=(), wrapper_quote=None,
                  wrapper_subject="中银航空租赁", twin_subject="中银航空租赁"):
    """78c71d5e 型：C 侧同句双重抽取——言语包裹 f1 + 实质事件 f2（已对齐）。

    H 单 fact（公告，全文 quote）；C 双 fact（包裹 f1 + 孪生 f2，均全文 quote）。
    包裹谓词与孪生谓词默认异词（发布 vs 公告），孪生与 H 谓词逐字锚（公告）。
    """
    history_text = "2026年9月9日，中银航空租赁在港交所公告，公司与空中客车公司订立协议将购机。"
    current_text = "2026年9月9日，中银航空租赁发布公告，公司与空中客车公司订立协议将购机。"
    history_facts = [
        _make_fact(history_text, fact_id="f1", subject="中银航空租赁",
                   predicate="公告", record_id=RECORD_ID_H),
    ]
    current_facts = [
        _make_fact(current_text, fact_id="f1", subject=wrapper_subject,
                   predicate=wrapper_predicate, modality=wrapper_modality,
                   quote=wrapper_quote, record_id=RECORD_ID_C,
                   numerics=wrapper_numerics),
        _make_fact(current_text, fact_id="f2", subject=twin_subject,
                   predicate="公告", record_id=RECORD_ID_C),
    ]
    return history_text, history_facts, current_text, current_facts


# ---------- 主用例（本裁定唯一行为变更点） ----------

def test_n24_wrapper_filler_exemption_signs():
    """【红能力】言语包裹 leftover（78c71d5e 型）→ 豁免后签发 FACT_EQUIVALENT。

    旧码：条件 c 拒签 → equivalence_ready=False → pair unresolved
    FACT_INCOMPLETE → extra_event RULE_UNCOVERED → 聚合边界（红）。
    新码：豁免 → 签发 → pair equivalent → 聚合重复（绿）；且
    exempted_filler_facts 审计入账（不删事实）。"""
    args = _wrapper_pair()
    pair, out, p15_report = _run(*args)
    assert len(pair.aligned_facts) == 1
    assert pair.outcome == "equivalent", (
        "言语包裹填充豁免后条件 c 应通过（签发 FACT_EQUIVALENT）；"
        f"实测 {pair.outcome}/{pair.code}")
    assert pair.code == "FACT_EQUIVALENT"
    assert out.decision == "重复"
    assert out.duplicate_ids == ("item-H",)
    # 审计字段：豁免名单如实入账（事实不删、豁免留痕）
    exempted = getattr(p15_report, "exempted_filler_facts", ())
    assert any(e.get("side") == "current" and e.get("fact_id") == "f1"
               and e.get("predicate") == "发布" and e.get("twin_fact_id") == "f2"
               for e in exempted), f"豁免审计字段缺位：{exempted!r}"


def test_n24_decide_level_audit_preserved():
    """【红能力配套】decide 层端到端：翻转后 extra_event uncovered 审计保留。

    豁免只松覆盖闸，不动 extra_event 检测（decide 层领地零触碰）——
    翻转对 decision=重复 的同时，unresolved_fields 仍携带包裹 fact 的
    uncovered 记录（"仍记录审计字段，不删事实"）。"""
    history_text, history_facts, current_text, current_facts = _wrapper_pair()
    # E 批 H-04 F3 夹具升级（e-batch-design §3.3 F3 行 :235-260——本测试即
    # 该行，设计稿引旧名 +§3.4 第 4 条）：decide 路径经真校验器，夹具须
    # 校验可过——时间槽 present 认领日期串（"2026"/"9"/"9" 三数值词经
    # time 类指派全认领，探针实证双侧 validated_complete=True、issues=空）；
    # 断言语义（重复+uncovered 审计保留）零改动。
    def _claim_date(text, facts, record_id):
        upgraded = []
        for fact in facts:
            g = dict(fact)
            g["time"] = dict(fact["time"])
            g["time"]["expression"] = _present(
                text, "2026年9月9日",
                f"facts.{fact['fact_id']}.time.expression", record_id=record_id)
            upgraded.append(g)
        return upgraded
    history_facts = _claim_date(history_text, history_facts, RECORD_ID_H)
    current_facts = _claim_date(current_text, current_facts, RECORD_ID_C)
    history = {"record_id": RECORD_ID_H, "item_id": "item-H",
               "text": history_text, "arrival_seq": 1,
               "raw_hash": hashlib.sha256(history_text.encode("utf-8")).hexdigest(),
               "facts": history_facts, "extraction_status": "complete"}
    current = {"record_id": RECORD_ID_C, "item_id": "item-C",
               "text": current_text, "arrival_seq": 2,
               "raw_hash": hashlib.sha256(current_text.encode("utf-8")).hexdigest(),
               "facts": current_facts, "extraction_status": "complete"}
    outcome = decide_once(history, current, coverage_complete=True,
                          visible_seq=2, prepared_seq=2)
    assert outcome.decision == "重复", (
        f"decide 层翻转后应为重复，实测 {outcome.decision}/{outcome.internal_code}")
    assert outcome.duplicate_ids == ("item-H",)
    # extra_event 按 fact_id 序号集差判 uncovered（挂账 N24 已注"名单指错、
    # 布尔方向恒对"）：H ids={f1} vs C ids={f1,f2} → uncovered=current:f2；
    # 审计断言盯"有 current: 侧 uncovered 记录保留"，不钉具体序号。
    assert any(str(fid).startswith("current:") for fid in outcome.unresolved_fields), (
        f"extra_event uncovered 审计必须保留：{outcome.unresolved_fields!r}")


# ---------- 保留面：逐守卫拒绝（fp 保护面零松动，新旧码均绿） ----------

def test_n24_wrapper_with_numerics_not_exempted():
    """【保留面 g2】包裹携带新数值 → 不豁免（d4107222 型实质数据拦截）。"""
    args = _wrapper_pair(wrapper_numerics=("9月9日",))
    pair, out, p15_report = _run(*args)
    assert pair.outcome == "unresolved"
    assert out.decision == "边界case/疑难case"
    assert not getattr(p15_report, "exempted_filler_facts", ())


def test_n24_wrapper_with_modality_not_exempted():
    """【保留面 g3】包裹携带模态承诺 → 不豁免。"""
    args = _wrapper_pair(wrapper_modality="将")
    pair, out, p15_report = _run(*args)
    assert pair.outcome == "unresolved"
    assert out.decision == "边界case/疑难case"
    assert not getattr(p15_report, "exempted_filler_facts", ())


def test_n24_wrapper_span_not_coincident_not_exempted():
    """【保留面 g5】包裹 span 不共指已对齐 fact → 独立事件（09 L846 分界），不豁免。"""
    current_text = ("2026年9月9日，中银航空租赁发布公告，公司与空中客车公司订立协议购机。"
                    "公司昨日发布年度招聘计划。")
    history_text = "2026年9月9日，中银航空租赁在港交所公告，公司与空中客车公司订立协议购机。"
    history_facts = [
        _make_fact(history_text, fact_id="f1", subject="中银航空租赁",
                   predicate="公告", record_id=RECORD_ID_H),
    ]
    current_facts = [
        _make_fact(current_text, fact_id="f2", subject="中银航空租赁",
                   predicate="公告",
                   quote="2026年9月9日，中银航空租赁发布公告，公司与空中客车公司订立协议购机。",
                   record_id=RECORD_ID_C),
        # 包裹谓词但 span 在另一句（异句独立事件，不共指已对齐 f2）
        _make_fact(current_text, fact_id="f3", subject="中银航空租赁",
                   predicate="发布", quote="公司昨日发布年度招聘计划。",
                   record_id=RECORD_ID_C),
    ]
    pair, out, p15_report = _run(history_text, history_facts,
                                 current_text, current_facts)
    assert pair.outcome == "unresolved"
    assert out.decision == "边界case/疑难case"
    assert not getattr(p15_report, "exempted_filler_facts", ())


def test_n24_wrapper_subject_mismatch_not_exempted():
    """【保留面 g6】包裹与已对齐孪生主体不同值 → 不豁免。"""
    args = _wrapper_pair(wrapper_subject="空中客车公司")
    pair, out, p15_report = _run(*args)
    assert pair.outcome == "unresolved"
    assert out.decision == "边界case/疑难case"
    assert not getattr(p15_report, "exempted_filler_facts", ())


def test_n24_wrapper_subject_missing_not_exempted():
    """【保留面 g6】包裹主体 missing → 不豁免（主体不明不属"已确认附属"）。"""
    args = _wrapper_pair(wrapper_subject=None)
    pair, out, p15_report = _run(*args)
    assert pair.outcome == "unresolved"
    assert out.decision == "边界case/疑难case"
    assert not getattr(p15_report, "exempted_filler_facts", ())


def test_n24_d21_dict_synonym_competition_not_exempted():
    """【保留面 g4 / D21 锚规则同纪】谓词-词典条目不豁免。

    C 侧包裹=发布（pred-0002 成员），H 侧存在未对齐 fact 谓词=宣布（同义类）
    → 覆盖缺口归 N27 词典对齐通道（anchored-not-yet-fired 领地），本豁免
    一律不签发；1b93a701 型双侧公告/发布重影即此家族。"""
    history_text = "墨西哥经济部宣布对涉案产品征收临时反倾销税，措施六个月。"
    current_text = "墨西哥经济部发布公告，对涉案产品征收临时反倾销税，措施六个月。"
    history_facts = [
        # H:f1(征收) ↔ C:f2(征收) 逐字锚对齐（双侧 quote 均为全文 → span 全等）
        _make_fact(history_text, fact_id="f1", subject="墨西哥经济部",
                   predicate="征收", record_id=RECORD_ID_H),
        # H 侧未对齐 leftover：宣布（pred-0002 成员；span/主体与已对齐孪生
        # 全等——g1/g2/g3/g5/g6 全过，唯 g4 拦回：对侧 c:f3 发布同义类未对齐）
        _make_fact(history_text, fact_id="f2", subject="墨西哥经济部",
                   predicate="宣布", record_id=RECORD_ID_H),
    ]
    current_facts = [
        _make_fact(current_text, fact_id="f2", subject="墨西哥经济部",
                   predicate="征收", record_id=RECORD_ID_C),
        # C 侧未对齐 leftover：发布（pred-0002 成员；同唯 g4 拦回——对侧
        # h:f2 宣布同义类未对齐）
        _make_fact(current_text, fact_id="f3", subject="墨西哥经济部",
                   predicate="发布", record_id=RECORD_ID_C),
    ]
    pair, out, p15_report = _run(history_text, history_facts,
                                 current_text, current_facts)
    assert pair.outcome == "unresolved", (
        "D21 守卫：对侧未对齐同义类 fact 存在时豁免不得签发（N27 领地）；"
        f"实测 {pair.outcome}/{pair.code}")
    assert out.decision == "边界case/疑难case"
    assert not getattr(p15_report, "exempted_filler_facts", ())


def test_n24_predicate_outside_closed_set_not_exempted():
    """【保留面 g1】闭表外谓词（出席/上市等）即便结构特征全中也不豁免。"""
    args = _wrapper_pair(wrapper_predicate="订立")
    pair, out, p15_report = _run(*args)
    assert pair.outcome == "unresolved"
    assert out.decision == "边界case/疑难case"
    assert not getattr(p15_report, "exempted_filler_facts", ())


def test_n24_independent_event_leftover_still_blocked():
    """【保留面】真实质附加（独立事件，N24B-SUBST 家族）照拒——红线不动摇。

    test_p16_d_matrix.test_p14b 同款形态经 extract 路径复验：leftover 谓词
    收购（闭表外+带实质语义）→ 不豁免、聚合 RULE_UNCOVERED 边界。"""
    history_text = "甲公司发布新手机。"
    current_text = "甲公司发布新手机，同时收购芯片公司。"
    history_facts = [
        _make_fact(history_text, fact_id="f1", subject="甲公司",
                   predicate="发布", record_id=RECORD_ID_H),
    ]
    current_facts = [
        _make_fact(current_text, fact_id="f1", subject="甲公司",
                   predicate="发布", record_id=RECORD_ID_C),
        _make_fact(current_text, fact_id="f2", subject="甲公司",
                   predicate="收购", record_id=RECORD_ID_C),
    ]
    pair, out, p15_report = _run(history_text, history_facts,
                                 current_text, current_facts)
    assert pair.outcome == "unresolved"
    assert out.decision == "边界case/疑难case"
    assert not getattr(p15_report, "exempted_filler_facts", ())


def test_n24_bilateral_wrappers_both_exempted():
    """双侧各一同句包裹（H 公告孪生购买 / C 发布孪生购买）→ 双双豁免签发。

    文本异文（无文本证书路径），走 FACT_EQUIVALENT；双侧包裹谓词不同义类
    （公告 ∉ 词典、发布 ∈ pred-0002 但对侧 leftover=公告非同义类）→
    D21 守卫不拦截。"""
    history_text = "中银航空租赁公告，拟购买飞机。"
    current_text = "中银航空租赁发布公告，拟购买飞机。"
    history_facts = [
        _make_fact(history_text, fact_id="f1", subject="中银航空租赁",
                   predicate="公告", record_id=RECORD_ID_H),
        _make_fact(history_text, fact_id="f2", subject="中银航空租赁",
                   predicate="购买", record_id=RECORD_ID_H),
    ]
    current_facts = [
        _make_fact(current_text, fact_id="f1", subject="中银航空租赁",
                   predicate="发布", record_id=RECORD_ID_C),
        _make_fact(current_text, fact_id="f2", subject="中银航空租赁",
                   predicate="购买", record_id=RECORD_ID_C),
    ]
    pair, out, p15_report = _run(history_text, history_facts,
                                 current_text, current_facts)
    # h:f2(购买)↔c:f2(购买) 逐字锚对齐；leftover h:f1(公告)/c:f1(发布)
    # 均命中类①（span/主体与各自已对齐孪生共指同值）→ 双双豁免
    assert pair.outcome == "equivalent"
    assert pair.code == "FACT_EQUIVALENT"
    assert out.decision == "重复"
    exempted = getattr(p15_report, "exempted_filler_facts", ())
    assert any(e.get("side") == "history" and e.get("predicate") == "公告"
               for e in exempted), f"history 侧豁免未入账：{exempted!r}"
    assert any(e.get("side") == "current" and e.get("predicate") == "发布"
               for e in exempted), f"current 侧豁免未入账：{exempted!r}"


def test_n24_no_leftover_audit_field_empty():
    """【保留面】无 leftover 的全对齐对：豁免名单恒空，签发路径零扰动。"""
    text = "甲公司完成回购。"
    facts_h = [_make_fact(text, fact_id="f1", subject="甲公司",
                          predicate="完成", record_id=RECORD_ID_H)]
    facts_c = [_make_fact(text, fact_id="f1", subject="甲公司",
                          predicate="完成", record_id=RECORD_ID_C)]
    pair, out, p15_report = _run(text, facts_h, text, facts_c)
    assert pair.outcome == "equivalent"
    assert out.decision == "重复"
    assert getattr(p15_report, "exempted_filler_facts", ()) == ()
