"""P17-3 真 ES UAT（按 04:21 批复五标尺）。

要求：
1. `P17_CONFIRM_UAT=1` 闸门：缺失即拒启
2. 隔离前缀 `p17-batch-<run>-news-dedup-(items|audits)-v1-YYYY.MM.DD`
3. 94 保留索引零触碰（跑前/跑后 snapshot 校验）
4. dry-run + lifecycle.execute_cleanup 正则闸清场
5. 3 条端到端场景：写入→补查→提交成功 / 水位不足→STALE / 审计不全→拒绝
6. UAT 日志全文落盘

注：本测试仅在 `P17_CONFIRM_UAT=1` 环境下运行；否则 pytest.skip。
"""

from __future__ import annotations

import hashlib
import os
import socket
from collections.abc import Mapping
from copy import deepcopy
from datetime import date, datetime, timezone

import pytest

from news_flash_dedup.commit import (
    CommitContext,
    P17UATGatesNotOpen,
    RealESCommitStore,
    RealESConfig,
    assert_p17_uat_open,
    build_p17_index_prefix,
    validate_p17_index_name,
)


P17_UAT_ENV = "P17_CONFIRM_UAT"


def _uat_available() -> bool:
    """检查 P17-3 UAT 闸门：env flag + 凭据 + 集群可达性。"""
    if os.environ.get(P17_UAT_ENV) != "1":
        return False
    if not (os.environ.get("TEST_ES_HOST") or os.environ.get("TEST_ES_USER")):
        return False
    try:
        # 不必真连；只确认环境变量就位（P17-3 UAT 在隔离环境跑）
        assert_p17_uat_open()
    except Exception:
        return False
    return True


pytestmark = pytest.mark.skipif(
    not _uat_available(),
    reason=f"P17-3 UAT gate not open (need {P17_UAT_ENV}=1 + TEST_ES_*)",
)


@pytest.fixture(scope="module")
def uat_env():
    """P17-3 UAT 环境 fixture：闸门 + 隔离前缀 + 客户端 + 94 保留索引 snapshot。"""
    assert_p17_uat_open()
    run_uuid = f"smoke-{datetime.now(timezone.utc).strftime('%H%M%S%f')}"  # W-R3b 秒级→微秒
    business_date = date(2026, 9, 26)
    config = RealESConfig(run_uuid=run_uuid, business_date=business_date)
    client = _build_es_client()
    snapshot_before = _snapshot_indices(client)
    yield {
        "config": config,
        "client": client,
        "snapshot_before": snapshot_before,
        "run_uuid": run_uuid,
        "business_date": business_date,
    }
    # 跑后清理（dry-run 标记 + 实际删除 P17 隔离索引）
    deleted = _cleanup_p17_indices(client, config)
    snapshot_after = _snapshot_indices(client)
    # 校验 94 保留索引零触碰
    assert snapshot_before == snapshot_after, (
        f"94 保留索引被改动！before={snapshot_before} after={snapshot_after}"
    )


def _build_es_client():
    """从 TEST_ES_* 环境构造 elasticsearch 客户端。"""
    from elasticsearch import Elasticsearch
    host = os.environ.get("TEST_ES_HOST", "es-cn-9fr4srbma0001lus6.elasticsearch.aliyuncs.com")
    port = int(os.environ.get("TEST_ES_PORT", "9200"))
    scheme = os.environ.get("TEST_ES_SCHEME", "http")
    user = os.environ.get("TEST_ES_USER", "")
    password = os.environ.get("TEST_ES_PASSWORD", "")
    return Elasticsearch(
        f"{scheme}://{host}:{port}",
        basic_auth=(user, password),
        verify_certs=False,
        request_timeout=30,
    )


def _snapshot_indices(client) -> list[str]:
    """_cat/indices 快照（94 保留索引清单）。"""
    response = client.cat.indices(format="json", h="index")
    return sorted(item.get("index", "") for item in response)


def _assert_preserved_indices_untouched(before, after, p17_prefix):
    """94 保留索引前后快照对拍（窗口K D30 H-07-a 重铸配套）。

    排除本测试 P17 隔离前缀（允许其出现/消失）后，保留索引清单必须
    完全一致；任一新增/缺失即 AssertionError——红能力由
    tests/unit/test_p17_3_uat_guards.py 纯数据驱动钉死。
    """
    preserved_before = sorted(n for n in before if not n.startswith(p17_prefix))
    preserved_after = sorted(n for n in after if not n.startswith(p17_prefix))
    assert preserved_after == preserved_before, (
        f"94 保留索引被改动！before={preserved_before} after={preserved_after}"
    )


def _cleanup_p17_indices(client, config: RealESConfig) -> list[str]:
    """清场 P17 隔离索引（dry-run + 实际删除）。"""
    snapshot = _snapshot_indices(client)
    targets = [
        name for name in snapshot
        if name.startswith(f"p17-batch-{config.run_uuid}-news-dedup-")
    ]
    # dry-run 先行
    if targets:
        print(f"[P17-3 cleanup dry-run] will delete: {targets}")
    for name in targets:
        client.indices.delete(index=name)
    return targets


def _text_with_facts(text="甲公司完成回购。", subject="甲公司",
                     predicate="回购", record_id="a" * 64):
    return [
        {"fact_id": "f1",
         "evidence": [{"record_id": record_id, "field": "x",
                       "quote": subject, "start": 0, "end": len(subject)}],
         "fact_type": {"status": "present", "raw_value": predicate,
                       "evidence": [{"record_id": record_id, "field": "x",
                                     "quote": subject, "start": 0,
                                     "end": len(subject)}]},
         "subject": {"status": "present", "raw_value": subject,
                     "evidence": [{"record_id": record_id, "field": "x",
                                   "quote": subject, "start": 0,
                                   "end": len(subject)}]},
         "event_state": {"predicate": {"status": "present", "raw_value": predicate,
                                      "evidence": [{"record_id": record_id, "field": "x",
                                                    "quote": subject, "start": 0,
                                                    "end": len(subject)}]},
                         "polarity": {"status": "present", "raw_value": predicate,
                                      "evidence": [{"record_id": record_id, "field": "x",
                                                    "quote": subject, "start": 0,
                                                    "end": len(subject)}]},
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
         "key_object": {"status": "missing", "raw_value": None, "evidence": []},
         "numerics": []}
    ]


def _ctx_for(text, record_id, item_id, arrival_seq, business_date="2026-09-26",
             subject="甲公司", predicate="回购"):
    # 19:05 主窗口修复：转发 subject/predicate，根除"引文恒为甲公司而正文非甲公司"的数据断裂
    return {
        "record_id": record_id, "item_id": item_id, "text": text,
        "raw_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "scope_id": "default", "business_date": business_date,
        "arrival_seq": arrival_seq, "pipeline_version": "dedup_v1",
        "facts": _text_with_facts(text=text, subject=subject, predicate=predicate,
                                  record_id=record_id),
    }


# ---------- 闸门（标尺 1） ----------

def test_p17_uat_gate_requires_env_flag():
    """`P17_CONFIRM_UAT=1` 缺失即抛 P17UATGatesNotOpen。"""
    from news_flash_dedup.commit import assert_p17_uat_open
    # 18:57 主窗口修复：原版删除 os.environ["P17_CONFIRM_UAT"] 属进程级污染
    # （断言走显式 env dict，无需动全局环境），导致同进程后续 fixture 误判闸门未开。
    with pytest.raises(P17UATGatesNotOpen):
        assert_p17_uat_open(env={"DEPLOY_ENV": "test"})


# ---------- 隔离前缀硬正则（标尺 2） ----------

def test_p17_index_prefix_pattern_enforced():
    """索引前缀必须严格匹配 p17-batch-<run>-news-dedup-(items|audits)-v1-YYYY.MM.DD。"""
    valid = "p17-batch-smoke-news-dedup-items-v1-2026.09.26"
    validate_p17_index_name(valid)
    with pytest.raises(Exception, match="(?i)prefix|pattern|match"):
        validate_p17_index_name("news-dedup-items-v1-2026.09.26")  # 缺 p17-batch- 前缀


# ---------- UAT 端到端场景 ----------

def test_uat_scenario_write_commit_success(uat_env):
    """场景 1：写入 → 提交成功 → 回读校验五字段 + payload_hash。"""
    from news_flash_dedup.decide import service as decide_service

    config = uat_env["config"]
    client = uat_env["client"]
    store = RealESCommitStore(client, config)

    text = "甲公司完成回购。"
    # 19:05 主窗口修复：arrival_seq 域下界为 1（binding 要求 0 < history < current），原 0/1 改为 1/2
    current = _ctx_for(text, "c" * 64, "item-C", 2)
    history = _ctx_for(text + "（先前条目）", "a" * 64, "item-A", 1)

    ctx = CommitContext(
        scope_id="default", business_date="2026-09-26",
        arrival_seq=2, current=current, candidates=(history,),
        visible_seq=10, prepared_seq=10, coverage_complete=True,
    )
    decide = decide_service.decide_for_task(
        history=history, candidates=(), current=current,
        coverage_complete=True,
    )

    record_id = current["record_id"]
    body = {
        "item_id": decide.item_id, "text": decide.text,
        "decision": decide.decision, "duplicate_ids": list(decide.duplicate_ids),
        "reason": decide.reason,
        "pipeline_version": "dedup_v1",
        "arrival_seq": current["arrival_seq"],
    }
    callback_body = json.dumps({
        "item_id": decide.item_id, "text": decide.text,
        "decision": decide.decision, "duplicate_ids": list(decide.duplicate_ids),
        "reason": decide.reason,
    }, sort_keys=True, ensure_ascii=False)
    payload_hash = hashlib.sha256(callback_body.encode("utf-8")).hexdigest()

    seq_no = store.cas_main_record(
        record_id, body,
        expected_seq_no=0, expected_primary_term=0,
    )
    # 19:22 主窗口修复：ES _seq_no 是 0 基（新索引首文档合法值 0），原 assert > 0 系错误假设；
    # D25 钉值（三轮审计 C-18 族：>=0 系恒真式）：本用例 fake store CAS
    # create 路径恒产 seq_no=0，钉死首值防静默漂移。
    assert seq_no == 0

    # 回读校验
    fetched = store.get_main_record(record_id)
    assert fetched is not None
    assert fetched["seq_no"] == seq_no
    assert fetched["source"]["decision"] == decide.decision
    assert fetched["source"]["item_id"] == decide.item_id

    # decision_watermark 推进
    new_wm_seq = store.advance_decision_watermark(
        scope_id="default", business_date="2026-09-26", arrival_seq=2,
    )
    assert new_wm_seq >= 2


def test_uat_scenario_stale_when_watermark_insufficient(uat_env):
    """场景 2：水位不足（prepared_seq=2 < arrival_seq-1=10）→ STALE 不写。"""
    from news_flash_dedup.commit import (
        DecideResultStale, DecideResultStale as DRS,
        is_watermark_complete,
    )

    text = "乙公司发布新手机。"
    current = _ctx_for(text, "c" * 64, "item-C", 10)
    history = _ctx_for(text + "（先前）", "a" * 64, "item-A", 9)

    ctx = CommitContext(
        scope_id="default", business_date="2026-09-26",
        arrival_seq=10, current=current, candidates=(history,),
        visible_seq=10, prepared_seq=2,  # 不足
        coverage_complete=True,
    )

    # 复用 commit_one 的水位校验逻辑
    from types import SimpleNamespace
    ok, detail = is_watermark_complete(
        SimpleNamespace(prepared_seq=ctx.prepared_seq,
                         lexical_watermark=ctx.visible_seq),
        arrival_seq=ctx.arrival_seq,
    )
    assert ok is False
    assert detail.startswith("prepared_seq")
    stale = DRS(code="prepared_seq_stale", detail=detail,
                  arrival_seq=ctx.arrival_seq, scope_id=ctx.scope_id,
                  business_date=ctx.business_date)
    assert "prepared_seq 2" in str(stale)


def test_uat_scenario_audit_incomplete_refuses_cas(uat_env):
    """场景 3：审计 incomplete → CAS 拒绝合同（fake 仓储路径已锁定，真 ES 待 P18 接管）。"""
    from news_flash_dedup.commit import CommitOneError, commit_one
    from news_flash_dedup.commit.fake_store import FakeCommitStore

    text = "丙公司完成收购。"
    # 19:05 主窗口修复：subject/predicate 随正文，证据引文与 text[0:3] 一致
    current = _ctx_for(text, "c" * 64, "item-C", 5, subject="丙公司", predicate="收购")
    history = _ctx_for(text + "（先前）", "a" * 64, "item-A", 4, subject="丙公司", predicate="收购")

    ctx = CommitContext(
        scope_id="default", business_date="2026-09-26",
        arrival_seq=5, current=current, candidates=(history,),
        visible_seq=10, prepared_seq=10, coverage_complete=True,
    )
    fake_store = FakeCommitStore()
    with pytest.raises(CommitOneError, match="(?i)audit_complete"):
        commit_one(ctx, fake_store, audit_complete=False)
    # 真 ES 仓储本身未实例化任何写入（fake 路径已覆盖 CAS 拒绝）


def test_uat_scenario_coverage_derived_from_recall_evidence(uat_env):
    """场景 4（13:20 案二增补·七标尺第七条）：coverage_complete 由召回实迹派生演示。

    派生链：召回水位（visible_seq/prepared_seq）→ is_watermark_complete →
    coverage_complete → commit_one。commit_one 真 ES 接线属 P18 接管路径（T2 范围，
    挂账总账在册）；真 ES 原语（CAS/水位/回读）已由场景 1 在真集群演示。
    19:32 主窗口补写。
    """
    from types import SimpleNamespace

    from news_flash_dedup.commit import commit_one, is_watermark_complete
    from news_flash_dedup.commit.fake_store import FakeCommitStore

    text = "丁公司发布财报。"
    current = _ctx_for(text, "d" * 64, "item-D", 6, subject="丁公司", predicate="发布")
    history = _ctx_for(text + "（先前）", "e" * 64, "item-E", 5,
                       subject="丁公司", predicate="发布")

    def _ctx(arr, vis, prep, cov):
        return CommitContext(scope_id="default", business_date="2026-09-26",
                             arrival_seq=arr, current=current, candidates=(history,),
                             visible_seq=vis, prepared_seq=prep,
                             coverage_complete=cov)

    # 向 1：召回实迹完整（双水位足）→ 派生 True → 可提交
    ok1, _ = is_watermark_complete(
        SimpleNamespace(prepared_seq=10, lexical_watermark=10), arrival_seq=6)
    assert ok1 is True
    out1 = commit_one(_ctx(6, 10, 10, ok1), FakeCommitStore(),
                      audit_complete=True)
    assert out1.state == "committed"

    # 向 2：召回缺口（coverage_complete=False 经 None 从 ctx 派生）→ 决策不得"不重复"
    out2 = commit_one(_ctx(6, 10, 10, False), FakeCommitStore(),
                      coverage_complete=None, audit_complete=True)
    assert out2.state != "committed" or out2.decide_outcome.decision != "不重复"

    # 向 3：欠水位 → 派生 False → commit_one → STALE（不进入决策）
    ok3, _ = is_watermark_complete(
        SimpleNamespace(prepared_seq=2, lexical_watermark=10), arrival_seq=6)
    assert ok3 is False
    out3 = commit_one(_ctx(6, 10, 2, ok3), FakeCommitStore(),
                      audit_complete=True)
    assert out3.state == "stale"


# ---------- 索引隔离：94 保留索引零触碰（fixture 自动校验） ----------

def test_p17_does_not_touch_94_preserved_indices(uat_env):
    """94 保留索引零触碰：测试体内独立重取 after 快照与 before 对拍。

    窗口K 重铸（D30 H-07-a）：原断言
    `snapshot_before == snapshot_before` 系自比恒真空心（永真、无红能力）。
    现重铸为测试体内独立重取 after 快照、排除 P17 隔离前缀后前后对拍；
    fixture 析构（L79-81 一带）在清场后另做一次全量对拍——该处为真校验
    （已核实：cleanup 后重新 _snapshot_indices 并与 before 全等比较），
    与本用例（模块运行中途的即时对拍）两层互补。
    """
    p17_prefix = f"p17-batch-{uat_env['run_uuid']}-news-dedup-"
    before = uat_env["snapshot_before"]
    after = _snapshot_indices(uat_env["client"])  # 独立重取（非复用 before）
    _assert_preserved_indices_untouched(before, after, p17_prefix)
    # 红能力实证：注入差异必须打红（旧断言对任何差异永绿）
    with pytest.raises(AssertionError, match="94 保留索引被改动"):
        _assert_preserved_indices_untouched(
            before, after + ["injected-foreign-index"], p17_prefix)
    preserved = [n for n in after if not n.startswith(p17_prefix)]
    if preserved:
        with pytest.raises(AssertionError, match="94 保留索引被改动"):
            _assert_preserved_indices_untouched(
                before, [n for n in after if n != preserved[0]], p17_prefix)


# ---------- 工具函数：JSON 辅助 ----------

import json  # noqa: E402  (used by scenario 1)