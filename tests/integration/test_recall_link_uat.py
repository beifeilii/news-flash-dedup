r"""B4/E1 F7：E3 真链路 UAT（受理→物化→准备→真召回→commit E2E）。

设计底本=log\设计-真链路接线包.md §三-F7/§五-T6（R145/R148 修订全量承接）。
闸（§五-T6）：`RELINK_CONFIRM_UAT=1` + `P18_CONFIRM_UAT=1`（提交段仓储闸，
persist/es_store.py:32）+ `DEPLOY_ENV=test` + `TEST_ES_*`（无 PROD_ES_* 泄漏）。

标尺逐项（§五-T6）：
① 双前缀双 run 族——受理/准备/召回段 `p01-batch-relink-<run>-`、提交段
   `p18-batch-relink-<run>-`（persist/es_store.py:33-35 正则合法形），两族
   各自命名空间独立；
② 保留索引基线快照跑前/跑后 diff 零触碰且快照/清场覆盖两族（硬约束#2）；
③ dry-run 先行+清场审计落盘（print 入日志）；
④ shadow 态双工件（真计划+影子决策五字段+签发码全形）+双跑对拍
   （腿 A=live commit 决策 vs 腿 B=shadow 决策；决策五字段+duplicate_ids+
   internal_code 一致）+影子路径零仓储调用断言（shadow 驱动后 p18 三索引
   docs.count 恒 0）；
⑤ live 态 E2E：受理→物化→准备→真召回→commit（RealESP18Store 经
   build_commit_one_store 自检）→五字段回读+payload_hash 复算+审计 ids+
   `delivery_state="pending"` 字段可见（出货断面=BP9，现闸下不验出货）；
⑥ coverage 派生演示：NullVectorSearcher 过渡态恒缺口（embedding/
   SPACE_UNCONFIRMED）→recall_incomplete=True→coverage=False→零候选
   current=边界/RECALL_INCOMPLETE（禁占位）；
⑦ 日志红绿双盘由跑站命令 stdout 重定向落盘（公共标尺）。

跑站命令（log\temp\ 落日志；<HHMM>=footer 分钟级）：

    $env:PYTHONPATH='src'
    $env:DEPLOY_ENV='test'
    $env:RELINK_CONFIRM_UAT='1'
    $env:P18_CONFIRM_UAT='1'
    & '.venv-v1\\Scripts\\python.exe' -m pytest -p no:cacheprovider `
        --basetemp '.\\tmp\\relink-uat-run' -v tests/integration/test_recall_link_uat.py `
        > ..\\log\\temp\\relink-uat-run-<HHMM>.txt
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone

import pytest

RELINK_ENV = "RELINK_CONFIRM_UAT"
P18_ENV = "P18_CONFIRM_UAT"
SCOPE = "default"
CLOCK_NOW = datetime(2026, 9, 29, 10, 0, tzinfo=timezone.utc)
BUSINESS_DATE = "2026-09-29"          # CLOCK_NOW 的 Asia/Shanghai 业务日


def _gate_open() -> bool:
    env = os.environ
    return (env.get(RELINK_ENV) == "1" and env.get(P18_ENV) == "1"
            and bool(env.get("TEST_ES_HOST")) and not env.get("PROD_ES_HOST"))


pytestmark = pytest.mark.skipif(
    not _gate_open(),
    reason=f"E3 真链路 UAT 闸未开（需 {RELINK_ENV}=1 + {P18_ENV}=1 + TEST_ES_*，"
           "无 PROD_ES_*）",
)


def _clock() -> datetime:
    return CLOCK_NOW


def _snapshot(client) -> dict[str, int]:
    rows = client.cat.indices(format="json", h="index,docs.count")
    return {item["index"]: int(item.get("docs.count") or 0)
            for item in rows if not item.get("index", "").startswith(".")}


# ---------- fixtures ----------

@pytest.fixture(scope="module")
def uat_client():
    """公共标尺②：客户端 + 保留索引跑前快照；跑后两族清场后严格 diff 硬停。"""
    from news_flash_dedup.es_client import (
        ProductionAccessDenied,
        assert_test_environment,
        client_from_environment,
        load_environment,
    )

    load_environment()
    try:
        assert_test_environment()
    except ProductionAccessDenied as error:  # pragma: no cover - 闸外事故
        pytest.fail(f"UAT environment check failed: {error}")
    client = client_from_environment(load_dotenv_first=False)
    info = client.info()
    assert str(info["version"]["number"]).startswith("8."), (
        f"UAT ES must be 8.x; got {info['version']['number']}")
    before = _snapshot(client)
    print(f"[RELINK-UAT] baseline indices={len(before)} "
          f"docs={sum(before.values())}")
    yield client
    after = _snapshot(client)
    added = sorted(set(after) - set(before))
    missing = sorted(set(before) - set(after))
    drifted = sorted(name for name in set(before) & set(after)
                     if before[name] != after[name])
    print(f"[RELINK-UAT] snapshot diff added={added} missing={missing} "
          f"drifted={drifted}")
    assert not added and not missing and not drifted, (
        "保留索引被改动或 stray 残留（越界硬停）！"
        f"added={added} missing={missing} drifted={drifted}")


@pytest.fixture(scope="module")
def relink_env(uat_client):
    """双前缀双 run 族隔离环境（标尺①）；析构=dry-run 先行+两族清场（标尺③）。"""
    from news_flash_dedup.es_admission_schema import (
        control_mapping,
        item_mapping,
        request_mapping,
    )
    from news_flash_dedup.persist import es_store as p18_es

    run = "b4" + datetime.now(timezone.utc).strftime("%H%M%S%f")   # W-R3b 秒级→微秒
    p01_prefix = f"p01-batch-relink-{run}-"
    p01_indices = {
        f"{p01_prefix}news-dedup-control-v1": control_mapping(),
        f"{p01_prefix}news-dedup-requests-v1": request_mapping(),
        f"{p01_prefix}news-dedup-items-v1-2026.09.29": item_mapping(),
    }
    for name, schema in p01_indices.items():
        uat_client.indices.create(index=name, body=schema)
    config = p18_es.RealESP18Config(
        run_uuid=f"relink-{run}", business_date=datetime(2026, 9, 29).date())
    store18 = p18_es.RealESP18Store(uat_client, config)   # 构造即幂等建索引
    print(f"[RELINK-UAT] p01 family: {sorted(p01_indices)}")
    print(f"[RELINK-UAT] p18 family: {config.items_index} / "
          f"{config.audits_index} / {config.control_index}")
    yield {
        "run": run,
        "p01_prefix": p01_prefix,
        "config": config,
        "store18": store18,
    }
    # 清场（标尺③）：dry-run 先行，两族分别 execute
    for family in (p01_prefix, f"p18-batch-relink-{run}-"):
        listed = sorted(uat_client.indices.get(
            index=f"{family}*", allow_no_indices=True,
            ignore_unavailable=True).keys())
        print(f"[RELINK-UAT cleanup dry-run] family={family} "
              f"will delete: {listed}")
        for name in listed:
            uat_client.indices.delete(index=name)
        print(f"[RELINK-UAT cleanup] family={family} deleted: {listed}")


# ---------- E2E ----------

def test_e3_recall_link_end_to_end(uat_client, relink_env):
    from news_flash_dedup.admission import AdmissionRequest
    from news_flash_dedup.batch_admission import (
        BatchAdmissionCoordinator,
        BatchLimits,
    )
    from news_flash_dedup.batch_es_store import ElasticsearchBatchStore
    from news_flash_dedup.persist.es_store import build_commit_one_store
    from news_flash_dedup.recall.prepare import (
        ElasticsearchWatermarkProvider,
        PrepareWorker,
    )
    from news_flash_dedup.recall.service import (
        NullVectorSearcher,
        RecallService,
    )
    from news_flash_dedup.recall.worker import (
        DedupWorker,
        ElasticsearchRegistrationScanner,
    )

    client = uat_client
    p01_prefix = relink_env["p01_prefix"]
    config = relink_env["config"]
    store18 = relink_env["store18"]

    # ---------- 受理→物化（p01 族） ----------
    store = ElasticsearchBatchStore(client, index_prefix=p01_prefix)
    coordinator = BatchAdmissionCoordinator(
        store, owner_id=f"relink-{relink_env['run']}",
        owner_isolated=lambda: True, limits=BatchLimits(), clock=_clock)
    texts = [
        "美国能源信息署公布原油库存增加。",
        "美国能源信息署公布原油库存增加。",   # 与首条同文：hash 命中对
        "央行开展中期借贷便利操作维护流动性合理充裕。",
        "某市发布新一轮新能源汽车购置补贴细则。",
        "国际油价小幅收涨市场关注供应端变化。",
        "证监会就程序化交易新规公开征求意见。",
    ]
    requests = [
        AdmissionRequest(
            scope_id=SCOPE, request_id=f"9-{i}", item_id=f"9-{i}", text=text,
            received_at=CLOCK_NOW, schema_version="v1",
            pipeline_version="dedup_v1", embedding_space_id="space-x",
            delivery_route_ref="route-relink", trace_id=f"trace-{i}")
        for i, text in enumerate(texts, start=1)
    ]
    receipts = []
    for offset in (0, 3):                       # 两批各 3 条（max_batch_items 内）
        receipts.extend(coordinator.accept_batch(requests[offset:offset + 3]))
    assert [receipt.arrival_seq for receipt in receipts] == [1, 2, 3, 4, 5, 6]
    while coordinator.materialize_oldest() < 6:
        pass
    assert coordinator.materialize_oldest() == 6
    client.indices.refresh(index=f"{p01_prefix}news-dedup-items-v1-2026.09.29")
    record_ids = [receipt.record_id for receipt in receipts]
    print(f"[RELINK-UAT] materialized 6 docs; record_ids={[r[:8] for r in record_ids]}")

    # ---------- 准备（F2 真驱动） ----------
    prepare_worker = PrepareWorker(client, p01_prefix, clock=_clock)
    report = prepare_worker.prepare(SCOPE, BUSINESS_DATE)
    assert report.conflicts == ()
    assert sorted(report.prepared) == sorted(record_ids)
    assert report.prepared_seq == 6
    assert report.lexical_watermark == 6
    provider = ElasticsearchWatermarkProvider(client, p01_prefix)
    assert provider.visible_seq(SCOPE, BUSINESS_DATE) == 6
    assert provider.prepared_seq(SCOPE, BUSINESS_DATE) == 6
    print(f"[RELINK-UAT] prepare done: prepared_seq={report.prepared_seq} "
          f"lexical={report.lexical_watermark}")

    # ---------- 召回服务（真四通道 + NullVectorSearcher 过渡态） ----------
    service = RecallService(client, p01_prefix,
                            vector_searcher=NullVectorSearcher(clock=_clock),
                            clock=_clock)
    scanner = ElasticsearchRegistrationScanner(client, p01_prefix)

    # ---------- shadow 腿（标尺④：双工件+零仓储调用） ----------
    sink: list[tuple[str, dict]] = []

    class _Sink:
        def emit(self, kind: str, payload: dict) -> None:
            sink.append((kind, payload))

    shadow_worker = DedupWorker(
        mode="shadow", recall_service=service, prepare_worker=prepare_worker,
        watermark_provider=provider, scanner=scanner,
        artifact_sink=_Sink(), clock=_clock)
    shadow_report = shadow_worker.drive_once(SCOPE, BUSINESS_DATE)
    assert sorted(shadow_report.shadowed) == sorted(record_ids)
    assert shadow_report.committed == shadow_report.failed == ()
    kinds = [kind for kind, _ in sink]
    assert kinds.count("recall_plan") == 6
    assert kinds.count("shadow_decision") == 6
    # coverage 派生演示（标尺⑥）：embedding 过渡态恒缺口→recall_incomplete
    for kind, payload in sink:
        if kind != "recall_plan":
            continue
        assert payload["recall_incomplete"] is True
        assert any(gap["channel"] == "embedding"
                   for gap in payload["recall_gaps"])
    # 影子路径零仓储调用（R-11/R15 真集群断言）：p18 三索引恒 0 文档
    for index in (config.items_index, config.audits_index, config.control_index):
        count = client.count(index=index)["count"]
        assert count == 0, f"shadow leg wrote {index}: count={count}"
    print("[RELINK-UAT] shadow leg: 12 artifacts, p18 family count=0 (零仓储调用)")

    # ---------- live 腿（标尺⑤：真 commit E2E） ----------
    commit_store = build_commit_one_store(store18)   # 协议自检先行
    live_worker = DedupWorker(
        mode="live", recall_service=service, prepare_worker=prepare_worker,
        watermark_provider=provider, scanner=scanner,
        commit_store=commit_store, clock=_clock)
    live_report = live_worker.drive_once(SCOPE, BUSINESS_DATE)
    assert live_report.stale == live_report.failed == live_report.awaiting == ()
    # 有序提交：升序单飞（receipts 采集序=arrival_seq 升序）
    assert list(live_report.committed) == record_ids
    assert store18.get_watermark(SCOPE, BUSINESS_DATE) == 6
    print(f"[RELINK-UAT] live leg committed={len(live_report.committed)} "
          f"watermark=6")

    # ---------- live 结果回读（五字段+payload_hash+审计 ids+pending 可见） ----------
    shadow_by_record = {
        payload["record_id"]: payload for kind, payload in sink
        if kind == "shadow_decision"}
    audit_total = 0
    for receipt in receipts:
        doc = store18.get_main_record(receipt.record_id)
        assert doc is not None, receipt.record_id
        source = doc["source"]
        assert source["task_state"] == "succeeded"
        result = source["result"]
        # 10-07 令甲-i（I-10）：断面钉口径自证——delivery_state ∈ {pending,held}
        # 且与 result.decision 分派一致（不重复↔pending 放行 / 重复·边界↔held 扣留）
        assert source["delivery_state"] == (
            "pending" if result["decision"] == "不重复" else "held")
        # 五字段冻结=result 四键（_record_to_body :491-496 现役形）+text 居顶层（:500）
        assert set(result) == {"item_id", "decision", "duplicate_ids", "reason"}
        assert result["item_id"] == receipt.item_id
        assert source["text"] == texts[receipt.arrival_seq - 1]
        # payload_hash 复算（decide.to_public_dict 五字段 JCS→sha256，
        # commit/coordinator.py:61-68：sort_keys+ensure_ascii=False+默认分隔符）
        public = {"decision": result["decision"],
                  "duplicate_ids": result["duplicate_ids"],
                  "item_id": result["item_id"],
                  "reason": result["reason"],
                  "text": source["text"]}
        callback_body = json.dumps(public, sort_keys=True, ensure_ascii=False)
        assert source["callback_body_hash"] == hashlib.sha256(
            callback_body.encode("utf-8")).hexdigest()
        assert source["audit_complete"] is True
        assert source["event_id"]
        # 双跑对拍（标尺④）：腿 A live 决策 == 腿 B shadow 决策（五字段+签发码）
        shadow = shadow_by_record[receipt.record_id]["outcome"]
        assert shadow["item_id"] == result["item_id"]
        assert shadow["text"] == source["text"]
        assert shadow["decision"] == result["decision"]
        assert shadow["duplicate_ids"] == result["duplicate_ids"]
        assert shadow["reason"] == result["reason"]
        assert shadow["internal_code"] == source["reason_code"]
        audit_total += len(source["audit_ids"])
    # 审计 ids：同文对（seq1→seq2）必入审计；审计文档数=各记录 audit_ids 之和
    seq2_doc = store18.get_main_record(record_ids[1])["source"]
    assert len(seq2_doc["audit_ids"]) >= 1
    audit_count = client.count(index=config.audits_index)["count"]
    assert audit_total == audit_count and audit_count >= 1
    judged_pairs = set()
    for record_id in record_ids:
        for audit_id in store18.get_main_record(record_id)["source"]["audit_ids"]:
            audit = client.get(index=config.audits_index, id=audit_id,
                               realtime=True)["_source"]
            assert audit["comparison_id"] == audit_id
            judged_pairs.add((audit["history_record_id"],
                              audit["current_record_id"]))
    # 同文对（seq1=history, seq2=current）必被精判（hash 命中不进 shortcut）
    assert (record_ids[0], record_ids[1]) in judged_pairs
    # coverage 派生（标尺⑥）：首条零候选+coverage=False→边界/RECALL_INCOMPLETE
    first = store18.get_main_record(record_ids[0])["source"]
    assert first["result"]["decision"] == "边界case/疑难case"
    assert first["reason_code"] == "RECALL_INCOMPLETE"
    print("[RELINK-UAT] 对拍 6/6 一致；payload_hash 复算全过；"
          f"audit docs={audit_count}; coverage 派生边界演示过")
