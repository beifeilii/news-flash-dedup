"""B4/E1 红测（D1 §五 T1）：F1 召回服务 recall/service.py。

红测清单映射（log\设计-真链路接线包.md §五-T1）：
- R-1  编排器存在性+五路齐递：fake 五通道→build_plan 返回 RecallPlan 且 channel_audits=5
- R-2  coverage 派生 fail-closed：任一通道 unavailable/truncated/缺口→False；全 complete+水位齐→True
- R-3  双侧映射键集：build_commit_inputs 产出 current 与候选两侧恰含 decide 消费键
       （record_id/item_id/text/raw_hash/arrival_seq/scope_id/business_date/facts）
       且 facts 非空、候选 arrival_seq 严格小于 current、expires_at 透传值=物化文档原值
- R-4  模式闸：缺省=fixed；fixed/shadow/live 三态解析；未知值（含大小写变体）fail-closed 拒启
- R-5  NullVectorSearcher：embedding unavailable+SPACE_UNCONFIRMED→recall_gaps 含 embedding、
       recall_incomplete=True、缺口入工件
另钉：F3 FactSupplyPort 默认恒等投影形态（test_p23_uat.py:346-380 同形）与可选 rule 供给。
"""

from __future__ import annotations

import hashlib

import pytest

from news_flash_dedup.recall.fusion import RecallPlan
from news_flash_dedup.recall.models import ChannelResult, RecallCandidate, RecallRequest
from news_flash_dedup.recall.service import (
    IdentityFactSupply,
    NullVectorSearcher,
    RecallModeInvalid,
    RecallService,
    RuleFactSupply,
    mode_from_environment,
)

from b4_fake_es import FakeESClient

DAY = "2026-09-29"
PREFIX = "p01-batch-b4svc-"
EXPECTED_KEYS = {
    "record_id", "item_id", "text", "raw_hash", "arrival_seq",
    "scope_id", "business_date", "facts",
}


def _rid(seed: str) -> str:
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def _request(**overrides) -> RecallRequest:
    base = dict(
        scope_id="default", business_date=DAY, record_id=_rid("current"),
        item_id="9-1", arrival_seq=5, text="美国能源信息署公布原油库存增加。",
        visible_seq=4, prepared_seq=4,
    )
    base.update(overrides)
    return RecallRequest(**base)


class _FakeChannel:
    """鸭式通道：固定 ChannelResult 回放。"""

    def __init__(self, result: ChannelResult) -> None:
        self.result = result
        self.calls: list[RecallRequest] = []

    def search(self, request: RecallRequest,
               query_vector=None) -> ChannelResult:
        self.calls.append(request)
        return self.result


def _complete(channel: str, query_version: str, *,
              candidates: tuple = (), visible_seq: int = 4,
              prepared_seq: int = 4) -> ChannelResult:
    return ChannelResult(
        channel, "complete", candidates, query_version,
        visible_seq=visible_seq, prepared_seq=prepared_seq,
        coverage_complete=True,
    )


def _service_with(results: dict[str, ChannelResult],
                  vector_result: ChannelResult | None = None,
                  **kwargs) -> tuple[RecallService, dict[str, _FakeChannel]]:
    channels = {name: _FakeChannel(result) for name, result in results.items()}
    vector = _FakeChannel(vector_result) if vector_result is not None else None
    service = RecallService(
        None, PREFIX,
        channels=channels,
        vector_searcher=vector if vector is not None else NullVectorSearcher(),
        **kwargs,
    )
    if vector is not None:
        channels["embedding"] = vector
    return service, channels


def _five_complete() -> dict[str, ChannelResult]:
    return {
        "hash": _complete("hash", "hash_v1"),
        "near": _complete("near", "near_v1"),
        "bm25": _complete("bm25", "bm25_v1"),
        "entity": _complete("entity", "entity_v1"),
    }


# ---------- R-1 编排器存在性+五路齐递 ----------

def test_r1_build_plan_fuses_five_channel_results():
    service, channels = _service_with(
        _five_complete(),
        _complete("embedding", "embedding_v1"),
    )
    request = _request()
    plan = service.build_plan(request)
    assert isinstance(plan, RecallPlan)
    assert len(plan.channel_audits) == 5
    assert [audit.channel for audit in plan.channel_audits] == [
        "hash", "near", "bm25", "embedding", "entity"]
    # 五路齐递：四个 ES 通道与向量端口各被调用恰好一次，请求原样透传
    for name in ("hash", "near", "bm25", "entity", "embedding"):
        assert channels[name].calls == [request]
    assert plan.recall_incomplete is False


def test_r1_default_assembly_builds_real_channels_without_network():
    # 默认装配（不注入 channels/vector_searcher）：真四通道构造 + NullVectorSearcher，
    # 构造期零网络（FakeESClient 仅内存），通道构造器前缀闸认可 p01-batch- 隔离前缀。
    service = RecallService(FakeESClient(), PREFIX)
    assert isinstance(service.vector_searcher, NullVectorSearcher)


# ---------- R-2 coverage 派生 fail-closed ----------

def _current_doc(**overrides) -> dict:
    text = "美国能源信息署公布原油库存增加。"
    base = {
        "record_id": _rid("current"), "item_id": "9-1", "text": text,
        "raw_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "scope_id": "default", "business_date": DAY, "arrival_seq": 5,
        "expires_at": "2026-10-06T00:00:00.000000Z",
    }
    base.update(overrides)
    return base


def _coverage_with(channel_results, vector_result=None):
    service, _ = _service_with(channel_results, vector_result)
    plan = service.build_plan(_request())
    _, _, coverage_complete, _ = service.build_commit_inputs(plan, _current_doc())
    return coverage_complete


def test_r2_coverage_true_only_when_all_channels_complete_and_frontiers_met():
    assert _coverage_with(_five_complete(), _complete("embedding", "embedding_v1")) is True


@pytest.mark.parametrize("bad_status", ["unavailable", "truncated"])
def test_r2_coverage_false_when_any_channel_unavailable_or_truncated(bad_status):
    results = _five_complete()
    results["bm25"] = ChannelResult(
        "bm25", bad_status, (), "bm25_v1", visible_seq=4, prepared_seq=4,
        error_code="SOME_ERROR", coverage_complete=False)
    assert _coverage_with(results, _complete("embedding", "embedding_v1")) is False


def test_r2_coverage_false_when_coverage_unproven_or_frontier_gap():
    # complete 但 coverage_complete=False → COVERAGE_UNPROVEN 缺口
    results = _five_complete()
    results["near"] = ChannelResult(
        "near", "complete", (), "near_v1", visible_seq=4, prepared_seq=4,
        coverage_complete=False)
    assert _coverage_with(results, _complete("embedding", "embedding_v1")) is False
    # 水位孔洞：visible_seq 低于 arrival_seq-1 → VISIBLE_FRONTIER_UNPROVEN 缺口
    results = _five_complete()
    results["hash"] = _complete("hash", "hash_v1", visible_seq=3)
    assert _coverage_with(results, _complete("embedding", "embedding_v1")) is False


# ---------- R-3 双侧映射键集 ----------

def test_r3_commit_inputs_both_sides_exact_decide_key_set():
    text = "美国能源信息署公布原油库存增加。"
    cand_text = "美国能源信息署公布原油库存增加（先前报道）。"
    candidate = RecallCandidate(
        record_id=_rid("cand-1"), item_id="9-0", arrival_seq=4, text=cand_text,
        subpaths=("raw",), score=1.0, query_version="hash_v1")
    results = _five_complete()
    results["hash"] = _complete("hash", "hash_v1", candidates=(candidate,))
    service, _ = _service_with(results, _complete("embedding", "embedding_v1"))
    plan = service.build_plan(_request())
    assert [item.candidate.record_id for item in plan.required] == [candidate.record_id]

    doc = _current_doc()
    current, candidates, coverage_complete, expires_at = service.build_commit_inputs(
        plan, doc)

    assert set(current) == EXPECTED_KEYS
    assert len(candidates) == 1
    assert set(candidates[0]) == EXPECTED_KEYS
    # facts 双侧非空（pair_alignment.py:377 前置）
    assert current["facts"] and candidates[0]["facts"]
    # 值透传：current 侧逐键=物化文档原值；候选 raw_hash=text 派生
    assert current["record_id"] == doc["record_id"]
    assert current["raw_hash"] == doc["raw_hash"]
    assert current["arrival_seq"] == 5
    assert candidates[0]["record_id"] == candidate.record_id
    assert candidates[0]["raw_hash"] == hashlib.sha256(
        cand_text.encode("utf-8")).hexdigest()
    # 候选 arrival_seq 严格小于 current（fusion.py:89-98 同型校验）
    assert candidates[0]["arrival_seq"] < current["arrival_seq"]
    # 域日同分区透传
    assert candidates[0]["scope_id"] == current["scope_id"] == "default"
    assert candidates[0]["business_date"] == current["business_date"] == DAY
    # expires_at 透传值=物化文档原值（逐字节）
    assert expires_at == doc["expires_at"]
    assert coverage_complete is True


def test_r3_expires_at_passthrough_none_when_materialized_doc_lacks_it():
    service, _ = _service_with(_five_complete(), _complete("embedding", "embedding_v1"))
    plan = service.build_plan(_request())
    doc = _current_doc()
    del doc["expires_at"]
    _, _, _, expires_at = service.build_commit_inputs(plan, doc)
    assert expires_at is None


# ---------- R-4 模式闸 ----------

def test_r4_mode_defaults_to_fixed_when_unset_or_empty():
    assert mode_from_environment({}) == "fixed"
    assert mode_from_environment({"DEDUP_RECALL_MODE": ""}) == "fixed"


@pytest.mark.parametrize("mode", ["fixed", "shadow", "live"])
def test_r4_mode_parses_three_states(mode):
    assert mode_from_environment({"DEDUP_RECALL_MODE": mode}) == mode


@pytest.mark.parametrize("bad", ["FIXED", "Fixed", "Shadow", "LIVE", "on", "1",
                                 "shadow ", " shadow", "pre-live"])
def test_r4_unknown_mode_fails_closed_including_case_variants(bad):
    with pytest.raises(RecallModeInvalid):
        mode_from_environment({"DEDUP_RECALL_MODE": bad})


def test_r4_mode_reads_process_environment_by_default(monkeypatch):
    monkeypatch.delenv("DEDUP_RECALL_MODE", raising=False)
    assert mode_from_environment() == "fixed"
    monkeypatch.setenv("DEDUP_RECALL_MODE", "shadow")
    assert mode_from_environment() == "shadow"


# ---------- R-5 NullVectorSearcher 过渡态缺口 ----------

def test_r5_null_vector_searcher_reports_space_unconfirmed_gap():
    request = _request()
    result = NullVectorSearcher().search(request, None)
    assert result.channel == "embedding"
    assert result.status == "unavailable"
    assert result.candidates == ()
    assert result.query_version == "embedding_v1"
    assert result.error_code == "SPACE_UNCONFIRMED"

    service, _ = _service_with(_five_complete())  # 默认 NullVectorSearcher
    plan = service.build_plan(request)
    assert plan.recall_incomplete is True
    assert ("embedding", "SPACE_UNCONFIRMED") in {
        (gap.channel, gap.reason) for gap in plan.recall_gaps}
    # 缺口入工件：channel_audits 承载 unavailable+SPACE_UNCONFIRMED
    audit = {item.channel: item for item in plan.channel_audits}["embedding"]
    assert audit.status == "unavailable"
    assert audit.error_code == "SPACE_UNCONFIRMED"
    # coverage 派生 fail-closed：embedding 缺口 → False
    _, _, coverage_complete, _ = service.build_commit_inputs(plan, _current_doc())
    assert coverage_complete is False


# ---------- F3 FactSupplyPort ----------

def test_identity_fact_supply_matches_replay_identity_projection_shape():
    text = "美联储宣布维持利率不变。"
    record_id = _rid("identity")
    supply = IdentityFactSupply()
    facts = supply(record_id, text)
    assert isinstance(facts, list) and len(facts) == 1
    fact = facts[0]
    raw_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    # 与 test_p23_uat.py:346-380 同形：fact_id/span/present/missing 槽位
    assert fact["fact_id"] == f"idem-{raw_hash[:16]}"
    span = {"record_id": record_id, "field": "text",
            "quote": text, "start": 0, "end": len(text)}
    assert fact["evidence"] == [span]
    assert fact["subject"] == {"status": "present", "raw_value": text,
                               "evidence": [span]}
    assert fact["event_state"]["predicate"]["raw_value"] == "verbatim_identity_projection"
    assert fact["event_state"]["modality"] == {"status": "missing",
                                               "raw_value": None, "evidence": []}
    assert fact["time"] == {"expression": {"status": "missing", "raw_value": None,
                                           "evidence": []},
                            "stage": {"status": "missing", "raw_value": None,
                                      "evidence": []},
                            "anchor": {"status": "missing", "raw_value": None,
                                       "evidence": []}}
    assert fact["key_object"] == {"status": "missing", "raw_value": None,
                                  "evidence": []}
    assert fact["numerics"] == []
    # 同文同版本确定性幂等：两次供给逐字节同值
    assert supply(record_id, text) == facts


def test_rule_fact_supply_matches_facts_rule_extract_facts():
    from news_flash_dedup.facts import rule as facts_rule

    text = "美国能源信息署公布原油库存增加。"
    record_id = _rid("rule")
    supply = RuleFactSupply()
    # 冻结字面钉（W-R3b / R3-M7-c2，w-r3b-probes.json 实算）：原断言
    # supply(...) == facts_rule.extract_facts(...) 系自比——RuleFactSupply
    # 纯委托同一调用链（service.py:133-139），抽取链任何漂移双侧同步
    # 漂移永不红；冻结字面对漂移咬。
    assert supply(record_id, text) == [{
        "fact_id": "f1",
        "evidence": [{"record_id": record_id, "field": "facts.f1",
                      "quote": "美国能源信息署公布原油库存增加。",
                      "start": 0, "end": 16}],
        "fact_type": {"status": "present", "raw_value": "公布",
                      "evidence": [{"record_id": record_id,
                                    "field": "facts.f1.fact_type",
                                    "quote": "公布", "start": 7, "end": 9}]},
        "subject": {"status": "present", "raw_value": "美国能源信息署",
                    "evidence": [{"record_id": record_id,
                                  "field": "facts.f1.subject",
                                  "quote": "美国能源信息署",
                                  "start": 0, "end": 7}]},
        "event_state": {
            "predicate": {"status": "present", "raw_value": "公布",
                          "evidence": [{"record_id": record_id,
                                        "field": "facts.f1.event_state.predicate",
                                        "quote": "公布", "start": 7, "end": 9}]},
            "polarity": {"status": "present", "raw_value": "公布",
                         "evidence": [{"record_id": record_id,
                                       "field": "facts.f1.event_state.polarity",
                                       "quote": "公布", "start": 7, "end": 9}]},
            "modality": {"status": "missing", "raw_value": None, "evidence": []},
            "attribution": {"status": "missing", "raw_value": None,
                            "evidence": []}},
        "time": {"expression": {"status": "missing", "raw_value": None,
                                "evidence": []},
                 "stage": {"status": "missing", "raw_value": None,
                           "evidence": []},
                 "anchor": {"status": "missing", "raw_value": None,
                            "evidence": []}},
        "key_object": {"status": "missing", "raw_value": None, "evidence": []},
        "numerics": [],
    }]
    # 冻结字面钉（W-R5 销项 W-R3b 登记留下批：原 v2 断言 supply_v2(...) ==
    # extract_facts(...) 系同形自比——RuleFactSupply 纯委托同一调用链
    # （service.py:133-139），v2 抽取链任何漂移双侧同步漂移永不红；下值=
    # w-r5-probe-v2facts.json 实算自探针干净链真值，变异体红证
    # w-r5-red-probes.json：_present 偏移漂移下旧绿新红）。v2 扩词生效面：
    # f2「增加」系 v2 动词表扩词命中（v1 同文仅 f1 一条）。
    supply_v2 = RuleFactSupply(dict_version=facts_rule.RULE_DICT_VERSION_V2)
    assert supply_v2(record_id, text) == [{
        "fact_id": "f1",
        "evidence": [{"record_id": record_id, "field": "facts.f1",
                      "quote": "美国能源信息署公布原油库存增加。",
                      "start": 0, "end": 16}],
        "fact_type": {"status": "present", "raw_value": "公布",
                      "evidence": [{"record_id": record_id,
                                    "field": "facts.f1.fact_type",
                                    "quote": "公布", "start": 7, "end": 9}]},
        "subject": {"status": "present", "raw_value": "美国能源信息署",
                    "evidence": [{"record_id": record_id,
                                  "field": "facts.f1.subject",
                                  "quote": "美国能源信息署",
                                  "start": 0, "end": 7}]},
        "event_state": {
            "predicate": {"status": "present", "raw_value": "公布",
                          "evidence": [{"record_id": record_id,
                                        "field": "facts.f1.event_state.predicate",
                                        "quote": "公布", "start": 7, "end": 9}]},
            "polarity": {"status": "present", "raw_value": "公布",
                         "evidence": [{"record_id": record_id,
                                       "field": "facts.f1.event_state.polarity",
                                       "quote": "公布", "start": 7, "end": 9}]},
            "modality": {"status": "missing", "raw_value": None,
                         "evidence": []},
            "attribution": {"status": "missing", "raw_value": None,
                            "evidence": []}},
        "time": {"expression": {"status": "missing", "raw_value": None,
                                "evidence": []},
                 "stage": {"status": "missing", "raw_value": None,
                           "evidence": []},
                 "anchor": {"status": "missing", "raw_value": None,
                            "evidence": []}},
        "key_object": {"status": "missing", "raw_value": None,
                       "evidence": []},
        "numerics": [],
    }, {
        "fact_id": "f2",
        "evidence": [{"record_id": record_id, "field": "facts.f2",
                      "quote": "美国能源信息署公布原油库存增加。",
                      "start": 0, "end": 16}],
        "fact_type": {"status": "present", "raw_value": "增加",
                      "evidence": [{"record_id": record_id,
                                    "field": "facts.f2.fact_type",
                                    "quote": "增加", "start": 13, "end": 15}]},
        "subject": {"status": "present", "raw_value": "美国能源信息署",
                    "evidence": [{"record_id": record_id,
                                  "field": "facts.f2.subject",
                                  "quote": "美国能源信息署",
                                  "start": 0, "end": 7}]},
        "event_state": {
            "predicate": {"status": "present", "raw_value": "增加",
                          "evidence": [{"record_id": record_id,
                                        "field": "facts.f2.event_state.predicate",
                                        "quote": "增加", "start": 13, "end": 15}]},
            "polarity": {"status": "present", "raw_value": "增加",
                         "evidence": [{"record_id": record_id,
                                       "field": "facts.f2.event_state.polarity",
                                       "quote": "增加", "start": 13, "end": 15}]},
            "modality": {"status": "missing", "raw_value": None,
                         "evidence": []},
            "attribution": {"status": "missing", "raw_value": None,
                            "evidence": []}},
        "time": {"expression": {"status": "missing", "raw_value": None,
                                "evidence": []},
                 "stage": {"status": "missing", "raw_value": None,
                           "evidence": []},
                 "anchor": {"status": "missing", "raw_value": None,
                            "evidence": []}},
        "key_object": {"status": "missing", "raw_value": None,
                       "evidence": []},
        "numerics": [],
    }]


def test_build_commit_inputs_uses_injected_supply_for_both_sides():
    candidate = RecallCandidate(
        record_id=_rid("cand-2"), item_id="9-0", arrival_seq=2, text="正文。",
        subpaths=("raw",), score=1.0, query_version="hash_v1")
    results = _five_complete()
    results["hash"] = _complete("hash", "hash_v1", candidates=(candidate,))
    seen: list[tuple[str, str]] = []

    def supply(record_id: str, text: str) -> list[dict]:
        seen.append((record_id, text))
        return [{"fact_id": "pinned"}]

    service, _ = _service_with(results, _complete("embedding", "embedding_v1"),
                               fact_supply=supply)
    plan = service.build_plan(_request())
    current, candidates, _, _ = service.build_commit_inputs(plan, _current_doc())
    assert current["facts"] == [{"fact_id": "pinned"}]
    assert candidates[0]["facts"] == [{"fact_id": "pinned"}]
    # 双侧同端口供给：current 与候选各一次
    assert seen == [(current["record_id"], current["text"]),
                    (candidate.record_id, candidate.text)]
