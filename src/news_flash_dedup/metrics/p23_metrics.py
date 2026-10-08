"""P23 金标回放指标计算（单元层，固定候选集回放）。

按 05:31 主审核方批复 + P23-设计-WIP §4.2：Clopper-Pearson 公式钉死为
`scipy.stats.beta.ppf(0.05, x, n-x+1)` 无 x=n 白送约定。

按 05:31 修订 2：固定候选集回放（不连真 BM25/Embedding），Recall@30/10 挂账至
凭据修复后 UAT。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class GoldPair:
    """单条金标对（按 P05 manifest 加载）。"""
    history_record_id: str
    current_record_id: str
    text: str
    expected_decision: str               # "重复" | "不重复" | "边界"
    duplicate_ids: tuple[str, ...]      # 期望 duplicate_ids（无则空）
    clear_or_hold: str = "clear"         # "clear" | "hold"
    is_covered: bool = True             # 是否在金标覆盖集内
    family_id: str = ""


@dataclass(frozen=True)
class ReplayResult:
    """单条回放结果。"""
    history_record_id: str
    current_record_id: str
    predicted_decision: str
    predicted_duplicate_ids: tuple[str, ...]
    expected_decision: str
    expected_duplicate_ids: tuple[str, ...]


def _safe_lower_bound(x: int, n: int) -> float:
    """Clopper-Pearson 单侧 95% 下界（05:31 主审核方修订 1 钉死）。

    实现：scipy.stats.beta.ppf(0.05, x, n-x+1)；无 x=n 白送约定。
    x=0 → 0（保留原值，不上调）。
    """
    try:
        from scipy.stats import beta
    except ImportError as exc:
        raise ImportError(
            "scipy is required for P23 Clopper-Pearson calculation"
        ) from exc
    if n <= 0 or x <= 0:
        return 0.0
    return float(beta.ppf(0.05, x, n - x + 1))


@dataclass
class MetricsAggregator:
    """P23 指标聚合器（按 12 §6.3/§6.4/§6.5 定义）。"""
    replay_results: list[ReplayResult] = field(default_factory=list)

    def add(self, result: ReplayResult) -> None:
        self.replay_results.append(result)

    # ---------- 12 §6.3：成对 Precision/Recall ----------
    def pair_metrics(self) -> Mapping[str, Any]:
        tp = fp = fn = tn = 0
        for r in self.replay_results:
            if r.expected_decision == "重复":
                if r.predicted_decision == "重复":
                    tp += 1
                else:
                    fn += 1
            elif r.expected_decision == "不重复":
                if r.predicted_decision == "重复":
                    fp += 1
                else:
                    tn += 1
            # 边界/hold 不计入 tp/fp/fn
        precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
        recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
        return {
            "tp": tp, "fp": fp, "fn": fn, "tn": tn,
            "precision": precision,
            "recall": recall,
        }

    # ---------- 12 §6.4：查询级严格结果 S + E2E 检测（13:20 第三方审核案一整改）----------
    def strict_metrics(self) -> Mapping[str, Any]:
        # 冻结规格（12 §6.4 L291）：
        #   P = {i∈M: 输出 decision == "重复"}（按输出计，不论金标）
        #   S = {i∈P: D_i 非空 + D_i ⊆ g_i + 五字段等合格}
        #   A = {i∈P: D_i ∩ g_i 非空}
        #   q_plus = {i∈M: 金标有正成员}（不动）
        # "错误输出'重复但空列表'仍留在 P 分母且不能成功"——D_i 空时 P+1 但 S/A 不变。
        P = 0
        S = 0
        A = 0
        q_plus = 0
        for r in self.replay_results:
            if r.expected_decision == "重复":
                q_plus += 1
            # P 按输出计（predicted == "重复" 全部计入，不受金标约束）
            if r.predicted_decision == "重复":
                P += 1
                d_i = set(r.predicted_duplicate_ids)
                g_i = set(r.expected_duplicate_ids)
                # A：D_i ∩ g_i 非空（命中）
                if d_i & g_i:
                    A += 1
                # S：实码仅检 D_i 非空 + D_i ⊆ g_i。
                # 【D30/M-02 注释勘误】原注"按 P23-设计-WIP §4.3 等价条件…子集
                # 已涵盖结构条件"失实——P23-设计-WIP §4.3 无该等价声明；12 §6.4
                # 的五字段/域日/去重排序/逐成员直接证据资格校验未接线（差距已
                # 披露于挂账#9/R9 F7a，随 P24 批处置）。
                if d_i and d_i.issubset(g_i):
                    S += 1
        precision_strict = S / P if P > 0 else 0.0
        recall_strict = S / q_plus if q_plus > 0 else 0.0
        precision_e2e = A / P if P > 0 else 0.0
        recall_e2e = A / q_plus if q_plus > 0 else 0.0
        clopper_lower = _safe_lower_bound(S, P)
        return {
            "P": P, "S": S, "A": A, "q_plus": q_plus,
            "precision_strict": precision_strict,
            "recall_strict": recall_strict,
            "precision_e2e": precision_e2e,
            "recall_e2e": recall_e2e,
            "clopper_pearson_lower_95": clopper_lower,
        }

    # ---------- 12 §6.5：工程成功率 ----------
    def engineering_metrics(self, *,
                              recovered: int = 0,
                              total_accepted: int = 0,
                              succeeded: int = 0,
                              delivered: int = 0,
                              total_to_deliver: int = 0) -> Mapping[str, Any]:
        recovery_rate = recovered / total_accepted if total_accepted > 0 else 0.0
        result_rate = succeeded / total_accepted if total_accepted > 0 else 0.0
        callback_rate = delivered / total_to_deliver if total_to_deliver > 0 else 0.0
        return {
            "recovery_rate": recovery_rate,
            "result_rate": result_rate,
            "callback_rate": callback_rate,
        }


def build_replay_pair(text: str = "甲公司完成回购。",
                       history_record_id: str = "a" * 64,
                       current_record_id: str = "b" * 64,
                       expected_decision: str = "重复",
                       duplicate_ids: tuple[str, ...] = ("a" * 64,),
                       clear_or_hold: str = "clear",
                       family_id: str = "f1") -> GoldPair:
    return GoldPair(
        history_record_id=history_record_id,
        current_record_id=current_record_id,
        text=text,
        expected_decision=expected_decision,
        duplicate_ids=duplicate_ids,
        clear_or_hold=clear_or_hold,
        is_covered=True,
        family_id=family_id,
    )


def load_pairs_from_manifest(manifest: Mapping[str, Any]) -> list[GoldPair]:
    """从**合成/测试专用** manifest 加载 GoldPair 列表（fixture 冻结供给）。

    W2 批76（WA5 L-1 勘正）：本函数消费的是单元测试合成 manifest
    （history_record_id/current_record_id/text/expected_decision 形）——
    **不是**真实 P05 manifest（log/P05-历史金标初版manifest.json 的 pairs 为
    pair_id/gold_label/sources/status/split 形，无上述字段）。真实金标回放
    不经此函数（经 tests/integration 固定供给路径）；勿以本函数解析真实 P05
    manifest。
    """
    pairs_raw = manifest.get("pairs", [])
    pairs: list[GoldPair] = []
    for entry in pairs_raw:
        pairs.append(GoldPair(
            history_record_id=entry["history_record_id"],
            current_record_id=entry["current_record_id"],
            text=entry["text"],
            expected_decision=entry.get("expected_decision", "重复"),
            duplicate_ids=tuple(entry.get("duplicate_ids", [])),
            clear_or_hold=entry.get("clear_or_hold", "clear"),
            is_covered=entry.get("is_covered", True),
            family_id=entry.get("family_id", ""),
        ))
    return pairs


def compute_payload_hash(metrics_dict: Mapping[str, Any]) -> str:
    """指标快照 payload_hash（重放一致性合同）。"""
    canonical = json.dumps(metrics_dict, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


__all__ = [
    "GoldPair",
    "MetricsAggregator",
    "ReplayResult",
    "_safe_lower_bound",
    "build_replay_pair",
    "compute_payload_hash",
    "load_pairs_from_manifest",
]