"""提交时候选集重构内核钉（recall/reconstruct.py，B+ 第三步核心件①）：

- 差集语义：missing=fresh−precheck（须补算）/dropped=precheck−fresh
  （退出聚合）/identical=双空；
- 确定性序：差集输出按 (arrival_seq, record_id) 可复现排序；
- 身份闸：提交请求与预检请求身份字段不符 → ValueError（接线错误
  fail-closed，不得静默流入差集语义）；
- 重构走同一查询路径：build_plan 恰调一次、透传 budget。
"""

from __future__ import annotations

import pytest

from news_flash_dedup.recall.fusion import (
    FusedCandidate,
    RecallPlan,
)
from news_flash_dedup.recall.models import RecallCandidate, RecallRequest
from news_flash_dedup.recall.reconstruct import (
    diff_candidate_sets,
    reconstruct,
    required_id_set,
)


def _cand(rid: str, seq: int) -> RecallCandidate:
    return RecallCandidate(
        record_id=rid, item_id=f"it-{rid}", arrival_seq=seq,
        text=f"text-{rid}", subpaths=(), score=1.0, query_version="v1")


def _fused(rid: str, seq: int) -> FusedCandidate:
    return FusedCandidate(
        candidate=_cand(rid, seq), channel_hits=(),
        rrf_score=0, hash_protected=False,
        selected_for_pair_check=True, incremental_required=False)


def _plan(ids_seqs: tuple[tuple[str, int], ...], tag: str = "p") -> RecallPlan:
    return RecallPlan(
        version="v1", merged_top30=(), pair_top10=(), hash_protected=(),
        incremental_required=(),
        required=tuple(_fused(rid, seq) for rid, seq in ids_seqs),
        recall_gaps=(), planning_excluded=0, channel_audits=())


def _request(rid: str = "cur", seq: int = 10, visible: int = 99) -> RecallRequest:
    return RecallRequest(
        scope_id="default", business_date="2026-10-08", record_id=rid,
        item_id=f"it-{rid}", arrival_seq=seq, text="当前文本",
        visible_seq=visible, prepared_seq=visible)


# ---------- 差集语义 ----------

def test_identical_sets():
    pre = _plan((("a", 1), ("b", 2)))
    fresh = _plan((("b", 2), ("a", 1)))   # 顺序无关——集合语义
    diff = diff_candidate_sets(pre, fresh)
    assert diff.identical and diff.missing == () and diff.dropped == ()
    assert diff.fresh_plan is fresh


def test_missing_and_dropped_deterministic_order():
    pre = _plan((("a", 1), ("b", 2), ("c", 3)))
    fresh = _plan((("a", 1), ("d", 5), ("e", 4)))
    diff = diff_candidate_sets(pre, fresh)
    assert not diff.identical
    # missing 按 (arrival_seq, record_id)：e(4) 先于 d(5)
    assert diff.missing == ("e", "d")
    assert diff.dropped == ("b", "c")   # seq 2 先于 3


def test_required_id_set_reads_required_only():
    plan = _plan((("a", 1), ("b", 2)))
    assert required_id_set(plan) == frozenset({"a", "b"})


# ---------- 重构（同一查询路径 + 身份闸） ----------

class _StubRecallService:
    def __init__(self, plan):
        self.plan = plan
        self.calls = []

    def build_plan(self, request, *, budget=None):
        self.calls.append((request, budget))
        return self.plan


def test_reconstruct_calls_same_path_once_with_budget():
    fresh = _plan((("a", 1), ("x", 7)))
    service = _StubRecallService(fresh)
    pre = _plan((("a", 1),))
    budget = object()
    diff = reconstruct(service, _request(visible=123), pre, budget=budget)
    assert len(service.calls) == 1
    req, used_budget = service.calls[0]
    assert req.visible_seq == 123 and used_budget is budget
    assert diff.missing == ("x",) and diff.fresh_plan is fresh


def test_reconstruct_identity_guard():
    service = _StubRecallService(_plan(()))
    pre_request = _request(rid="cur", seq=10)
    bad_commit_request = _request(rid="cur", seq=11)   # arrival_seq 漂移
    with pytest.raises(ValueError):
        reconstruct(service, bad_commit_request, _plan(()),
                    precheck_request=pre_request)
    assert service.calls == []   # 未发查询即拒


def test_reconstruct_identity_ok_passes_through():
    service = _StubRecallService(_plan((("a", 1),)))
    req = _request()
    diff = reconstruct(service, req, _plan((("a", 1),)),
                       precheck_request=_request())
    assert diff.identical
