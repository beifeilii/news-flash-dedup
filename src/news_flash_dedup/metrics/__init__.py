"""P23 指标聚合包（按 12 §6 + 05:31 主审核方修订）。

05:31 修订：
1. Clopper-Pearson 公式钉死 `scipy.stats.beta.ppf(0.05, x, n-x+1)` 无 x=n 白送约定；
2. 候选源 = 固定候选集回放（manifest/fixture 冻结供给），不连真 BM25/Embedding。
"""

from .p23_metrics import (
    GoldPair,
    MetricsAggregator,
    ReplayResult,
    _safe_lower_bound,
    build_replay_pair,
    compute_payload_hash,
    load_pairs_from_manifest,
)

__all__ = [
    "GoldPair",
    "MetricsAggregator",
    "ReplayResult",
    "build_replay_pair",
    "compute_payload_hash",
    "load_pairs_from_manifest",
    "_safe_lower_bound",
]