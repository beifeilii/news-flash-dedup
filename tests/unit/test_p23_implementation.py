"""P23 金标回放单元测试：固定候选集（manifest/fixture）+ 指标 + Clopper-Pearson。

按 05:31 主审核方批复 + P23-设计-WIP：
1. 候选源 = manifest/fixture 冻结供给（不连真 BM25/Embedding）；
2. Clopper-Pearson 公式钉死 scipy.stats.beta.ppf（无 x=n 白送约定）；
3. Recall@30/10 挂账至凭据修复后 UAT（本轮不测）。
"""

from __future__ import annotations

import pytest

from news_flash_dedup.metrics import (
    GoldPair,
    MetricsAggregator,
    ReplayResult,
    _safe_lower_bound,
    build_replay_pair,
    compute_payload_hash,
    load_pairs_from_manifest,
)


# ---------- 标尺 1：成对 Precision/Recall ----------

def test_pair_metrics_tp_fp_fn_tn():
    """tp=2（重复预测重复）, fp=1（不重复预测重复）, fn=1（重复预测不重复）。"""
    agg = MetricsAggregator()
    agg.add(ReplayResult("a", "b", "重复", ("a",), "重复", ("a",)))
    agg.add(ReplayResult("c", "d", "重复", ("c",), "重复", ("c",)))
    agg.add(ReplayResult("e", "f", "重复", (), "不重复", ()))         # fp（不重复预测重复）
    agg.add(ReplayResult("g", "h", "不重复", (), "重复", ("g",)))   # fn（重复预测不重复）
    m = agg.pair_metrics()
    assert m["tp"] == 2 and m["fp"] == 1 and m["fn"] == 1
    assert m["precision"] == 2 / 3
    assert m["recall"] == 2 / 3


def test_pair_metrics_boundary_and_hold_not_counted():
    """边界 + hold 不计入 tp/fp/fn（按 12 §6.3）。"""
    agg = MetricsAggregator()
    agg.add(ReplayResult("a", "b", "重复", ("a",), "边界case/疑难case", ()))
    agg.add(ReplayResult("c", "d", "边界case/疑难case", (), "不重复", ()))
    agg.add(ReplayResult("e", "f", "重复", ("e",), "重复", ()))   # tp=1
    m = agg.pair_metrics()
    assert m["tp"] == 1 and m["fp"] == 0 and m["fn"] == 0
    assert m["precision"] == 1.0
    assert m["recall"] == 1.0


# ---------- 标尺 2：查询级严格 S + E2E 检测 + Clopper-Pearson ----------

def test_strict_metrics_S_requires_D_i_subset_of_g_i():
    """S 要求 D_i ⊆ g_i；D_i 含 g_i 之外的成员则不计 S。"""
    agg = MetricsAggregator()
    # tp+strict（S）：重复预测重复，D_i ⊆ g_i
    agg.add(ReplayResult("a", "b", "重复", ("a",), "重复", ("a",)))
    # tp 但 D_i 含 g_i 之外的成员（FP）：不算 S
    agg.add(ReplayResult("c", "d", "重复", ("c", "x"), "重复", ("c",)))
    m = agg.strict_metrics()
    assert m["P"] == 2
    assert m["S"] == 1            # 仅第一条
    assert m["A"] == 2            # 两条都检测命中
    assert m["precision_strict"] == 0.5
    # q_plus=2（两条都是真重复）, S=1, recall_strict = 1/2 = 0.5
    assert m["recall_strict"] == 0.5


def test_clopper_pearson_no_x_n_free_pass():
    """05:31 修订：x=n 时 lower ≠ 1（无白送约定）。"""
    # n=389, x=389 → lower ≈ 99.23%（非 100%）
    lb = _safe_lower_bound(389, 389)
    assert lb < 1.0
    assert lb > 0.99


def test_clopper_pearson_x_zero_lower_zero():
    """x=0 → lower=0（保留原值）。"""
    assert _safe_lower_bound(0, 389) == 0.0


def test_clopper_pearson_falls_back_to_zero_on_empty_input():
    """n=0 → lower=0（避免除零）。"""
    assert _safe_lower_bound(0, 0) == 0.0


def test_strict_metrics_clopper_lower_for_sample():
    """样本量 n=10 x=9 → lower 计算（实测形态）。"""
    lb = _safe_lower_bound(9, 10)
    # W2Fε 收窄（原 0<lb<1.0 宽区间无守底/守顶）：现算值 float(beta.ppf
    # (0.05, 9, 2))=0.6058366975634952（.venv-v1 scipy 实测），钉 ±1e-9——
    # 冻结 CP 公式（scipy beta.ppf(0.05, x, n-x+1)，无 x=n 白送）被改动即红。
    assert abs(lb - 0.6058366975634952) <= 1e-9


# ---------- 13:20 第三方审核案一整改：P 集合严格按 12 §6.4 ----------

def test_strict_metrics_P_includes_false_positive_gold_not_repeat():
    """金标=不重复 + 预测=重复 → P+1 且 S/A 不变 + precision_strict 下降。"""
    agg = MetricsAggregator()
    # S=1：真重复预测重复，D_i ⊆ g_i
    agg.add(ReplayResult("a", "b", "重复", ("a",), "重复", ("a",)))
    # gold=不重复, predicted=重复 → FP：仅 P+1，S/A 不增
    agg.add(ReplayResult("c", "d", "重复", ("c",), "不重复", ()))
    m = agg.strict_metrics()
    assert m["P"] == 2          # P=2（predicted==重复全集）
    assert m["S"] == 1          # 仅第 1 条满足 D_i ⊆ g_i
    assert m["A"] == 1          # 仅第 1 条 D_i ∩ g_i 非空
    assert m["precision_strict"] == 0.5  # S/P = 1/2
    assert m["q_plus"] == 1     # 仅第 1 条金标有正成员


def test_strict_metrics_P_includes_boundary_gold():
    """金标=边界 + 预测=重复 → 入 P 但 q_plus 不增。"""
    agg = MetricsAggregator()
    agg.add(ReplayResult("a", "b", "重复", ("a",), "边界case/疑难case", ()))
    m = agg.strict_metrics()
    assert m["P"] == 1          # predicted=重复 入 P
    assert m["q_plus"] == 0     # gold=边界 不计入 q_plus
    assert m["S"] == 0          # D_i 非空 + D_i ⊆ g_i（边界无）不满足
    assert m["A"] == 0          # D_i ∩ g_i 空（边界无 g_i）


def test_strict_metrics_empty_D_i_increases_P_not_S():
    """predicted=重复 但 D_i 空 → P+1，S/A 不增（错误输出不能成功）。"""
    agg = MetricsAggregator()
    agg.add(ReplayResult("a", "b", "重复", (), "重复", ("a",)))  # 空 D_i
    agg.add(ReplayResult("c", "d", "重复", ("c",), "重复", ("c",)))  # 正常
    m = agg.strict_metrics()
    assert m["P"] == 2          # 两条 predicted=重复 全入 P
    assert m["S"] == 1          # 仅第 2 条 D_i 非空
    assert m["A"] == 1          # 仅第 2 条 D_i ∩ g_i 非空


def test_pair_metrics_fp_fn_docstrings_swap_note():
    """13:20 案一附记：成对测试 fp/fn 注释语义互换说明。"""
    # 实码：gold=重复∧predicted=不重复 → fn；gold=不重复∧predicted=重复 → fp
    # 实测语义正确：line 31 fp 注释对应 gold=不重复（gold=不重复∧predicted=重复）
    # line 32 fn 注释对应 gold=重复（gold=重复∧predicted=不重复）
    # 注释与断言一致即可，案一附记 fp/fn 互换实指此前预告，已落正
    agg = MetricsAggregator()
    agg.add(ReplayResult("a", "b", "重复", (), "不重复", ()))     # gold=重复, predicted=不重复 → fn
    agg.add(ReplayResult("c", "d", "不重复", ("x",), "重复", ()))  # gold=不重复, predicted=重复 → fp
    m = agg.pair_metrics()
    assert m["tp"] == 0 and m["fp"] == 1 and m["fn"] == 1
    assert m["precision"] == 0.0
    assert m["recall"] == 0.0


# ---------- 标尺 3：工程成功率 ----------

def test_engineering_metrics_three_rates():
    """工程成功率三率：recovery / result / callback。"""
    agg = MetricsAggregator()
    m = agg.engineering_metrics(
        recovered=99, total_accepted=100,
        succeeded=95, total_to_deliver=100,
        delivered=90,
    )
    assert m["recovery_rate"] == 0.99
    assert m["result_rate"] == 0.95
    assert m["callback_rate"] == 0.90


# ---------- manifest/fixture 加载 ----------

def test_load_pairs_from_manifest():
    """从 manifest dict 加载 GoldPair 列表。"""
    manifest = {
        "pairs": [
            {"history_record_id": "a" * 64, "current_record_id": "b" * 64,
              "text": "甲公司完成回购。", "expected_decision": "重复",
              "duplicate_ids": ["a" * 64], "family_id": "f1"},
            {"history_record_id": "c" * 64, "current_record_id": "d" * 64,
              "text": "乙公司不重复。", "expected_decision": "不重复",
              "duplicate_ids": [], "family_id": "f2"},
        ],
    }
    pairs = load_pairs_from_manifest(manifest)
    assert len(pairs) == 2
    assert pairs[0].expected_decision == "重复"
    assert pairs[0].duplicate_ids == ("a" * 64,)
    assert pairs[1].expected_decision == "不重复"


def test_build_replay_pair_defaults():
    """build_replay_pair 默认值（重复 + clear）。"""
    pair = build_replay_pair()
    assert pair.expected_decision == "重复"
    assert pair.clear_or_hold == "clear"
    assert pair.family_id == "f1"


# ---------- 标尺 4：payload_hash 一致性 ----------

def test_payload_hash_stable_across_calls():
    """同输入 compute_payload_hash → 冻结字面（值锚定即含跨调用确定）。

    W-R3b / R3-M7-c5：原"双算相等"（h1==h2）对任意纯函数恒真（常数
    函数亦过），不锚值；冻结字面钉（w-r3b-probes.json 实算）对序列化
    形态任何漂移（分隔符/键序/编码）咬。"""
    m = {"tp": 10, "fp": 1, "fn": 2, "precision": 10 / 11}
    assert compute_payload_hash(m) == (
        "3d7b41f08992745f1ea145dce3d015dfc1b3920e9bfba92cfaebcb9bb9582800")


# ---------- 标尺 5：fake/metrics 不连真 ES/Milvus ----------

def test_metrics_module_does_not_import_es_milvus_clients():
    """P23 metrics 包不引入真 ES/Milvus 客户端。"""
    import news_flash_dedup.metrics.p23_metrics as pm
    module_attrs = dir(pm)
    for attr in ("ESClient", "MilvusClient", "elasticsearch", "pymilvus"):
        assert attr not in module_attrs, (
            f"P23 metrics 不应引入真 ES/Milvus 客户端 {attr!r}"
        )


# ---------- 端到端 smoke ----------

def test_p23_end_to_end_smoke():
    """完整路径：manifest 加载 → 配 replay_result → 聚合指标。"""
    manifest = {"pairs": [
        {"history_record_id": "a" * 64, "current_record_id": "b" * 64,
          "text": "甲公司完成回购。", "expected_decision": "重复",
          "duplicate_ids": ["a" * 64]},
        {"history_record_id": "c" * 64, "current_record_id": "d" * 64,
          "text": "乙公司不重复。", "expected_decision": "不重复",
          "duplicate_ids": []},
    ]}
    pairs = load_pairs_from_manifest(manifest)
    agg = MetricsAggregator()
    # Pair 1：完全命中（重复预测重复 + D_i ⊆ g_i）
    p = pairs[0]
    agg.add(ReplayResult(p.history_record_id, p.current_record_id,
                              "重复", p.duplicate_ids,
                              p.expected_decision, p.duplicate_ids))
    # Pair 2：完全命中（不重复预测不重复 + D_i 空）
    p = pairs[1]
    agg.add(ReplayResult(p.history_record_id, p.current_record_id,
                              "不重复", (),
                              p.expected_decision, p.duplicate_ids))
    pair_m = agg.pair_metrics()
    assert pair_m["tp"] == 1 and pair_m["fp"] == 0 and pair_m["fn"] == 0
    strict_m = agg.strict_metrics()
    assert strict_m["P"] == 1 and strict_m["S"] == 1
    assert strict_m["precision_strict"] == 1.0


# ---------- 14:29 主审三件套·跨日回归测试（R2 证据）----------

def test_run_single_pair_cross_day_each_ctx_holds_own_business_date(monkeypatch):
    """14:29 三件套·1：history=2026-09-25 / current=2026-09-26 →
    `_run_single_pair` 两 ctx 各持其日（history_ctx.business_date=2026-09-25、
    current_ctx.business_date=2026-09-26），同时验证 PairBindingError 路径可达。

    真基线（14:27 主审法证）：L279 history_report（正确）+ L288 history_report（current_ctx 单向错向）。
    红档 v1 出身（主审 14:50 法证）：实采自双交叉中间态（L279 current_report + L288 history_report，
       历史某次 cp/swap 引入），故失败签名为 history_ctx='2026-09-26' 应='2026-09-25'——而真基线下
       应为 history_ctx='2026-09-25' 通过 + current_ctx='2026-09-25' 应='2026-09-26' 失配，且
       with-block 因两 ctx 同日不抛 PairBindingError（DID NOT RAISE）。
    红能力多维成立：双交叉态→history_ctx 失配；真基线→current_ctx 失配+DID NOT RAISE；修复后→双 assert 通过+PairBindingError 抛出。
    绿：service.py L279 history_report / L288 current_report → 两 ctx 各持其日；
       历史 ctx 与当前 ctx 不同日 → PairBindingError 自然抛出。
    """
    from types import SimpleNamespace
    from news_flash_dedup.decide import service as decide_service
    from news_flash_dedup.compare import pair_alignment, p15_integration, pair_compare

    history_text = "甲公司完成回购。"
    current_text = "甲公司完成回购。"
    history_record_id = "a" * 64
    current_record_id = "b" * 64

    history_report = SimpleNamespace(artifact={"business_date": "2026-09-25"})
    current_report = SimpleNamespace(artifact={"business_date": "2026-09-26"})

    captured: dict = {}
    binding_error_seen: list = []

    real_compare_pair = pair_compare.compare_pair

    def spy_compare_pair(history_ctx, current_ctx, *args, **kwargs):
        captured["history_ctx"] = dict(history_ctx)
        captured["current_ctx"] = dict(current_ctx)
        # 模拟真实 PairBindingError 路径（与 pair_compare._check_binding L168-171 同语义）
        if history_ctx["business_date"] != current_ctx["business_date"]:
            binding_error_seen.append(
                (history_ctx["business_date"], current_ctx["business_date"])
            )
            raise pair_compare.PairBindingError(
                f"business_date mismatch: {history_ctx['business_date']!r} "
                f"vs {current_ctx['business_date']!r}"
            )
        return real_compare_pair(history_ctx, current_ctx, *args, **kwargs)

    monkeypatch.setattr(pair_compare, "compare_pair", spy_compare_pair)
    # 跳过 build_aligned / extract_p15_results（仅证 ctx 派生，不走 P16-A/P15 实逻辑）
    monkeypatch.setattr(
        pair_alignment, "build_aligned",
        lambda *a, **kw: SimpleNamespace(aligned_facts=(), unresolved_fields=()),
    )
    monkeypatch.setattr(
        p15_integration, "extract_p15_results",
        lambda *a, **kw: SimpleNamespace(p15_results=None),
    )

    with pytest.raises(pair_compare.PairBindingError):
        decide_service._run_single_pair(
            history_record_id, history_text, history_report,
            current_record_id, current_text, current_report,
            history_arrival_seq=1, current_arrival_seq=2,
            history_item_id="item-a", current_item_id="item-b",
            dictionary_version="dict_v1",
            alignment_version="alignment_v1",
            pipeline_version="dedup_v1",
            # R9 迁移（主窗口 06:5x）：域日经显式参数转发（生产路径
            # decide_for_task 由输入 mapping 实传）——替代旧 artifact 读法
            # （本仓 _wrap_facts_as_report 从不携带 business_date，旧读法
            # 恒落默认）。原断言意图不变：两 ctx 各持其日+跨日必抛。
            history_business_date="2026-09-25",
            current_business_date="2026-09-26",
        )

    # 断言 1：两 ctx 各持其日（修复前 L279/L288 错向 → 两 assert 均失败）
    assert captured["history_ctx"]["business_date"] == "2026-09-25", (
        f"history_ctx.business_date={captured['history_ctx']['business_date']!r} "
        f"应从 history_report 派生为 '2026-09-25'（14:29 R2 修复）"
    )
    assert captured["current_ctx"]["business_date"] == "2026-09-26", (
        f"current_ctx.business_date={captured['current_ctx']['business_date']!r} "
        f"应从 current_report 派生为 '2026-09-26'（14:29 R2 修复）"
    )
    # 断言 2：PairBindingError 路径可达（修复后两 ctx 不同日 → 必抛）
    assert binding_error_seen == [("2026-09-25", "2026-09-26")], (
        f"PairBindingError 应在 history_ctx.bd != current_ctx.bd 时触发，实测 {binding_error_seen!r}"
    )