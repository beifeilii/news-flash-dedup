r"""B6/E3 ①：真链路 E2E 真集群协奏（真 embedding 编入 RRF 融合双腿首跑）。

底本：D1 过闸稿 §五-T6（B4 F7 同型标尺）+B5 交付面（build_real_vector_searcher
+闸扩展 MilvusVectorStore+真空间 s_0dbdb6fe，1024/COSINE/tokens512_overlap64_bpe_v1）。

闸（六闸协奏之五环；P23B 环在双轨件）：
`RELINK_CONFIRM_UAT=1`+`P18_CONFIRM_UAT=1`+`P19_CONFIRM_UAT=1`+
`EMB_CONFIRM_REAL=1`+`P21Q_CONFIRM_UAT=1`+`DEPLOY_ENV=test`+
`TEST_ES_*`/`TEST_MILVUS_*` 就位+key 可解析+无 `PROD_*` 泄漏。

链路：受理→物化→准备→向量播种（真分块/真嵌入/闸扩展 store upsert）→
五通道召回（真 embedding 编入 RRF 融合）→判定→提交（RealESP18Store 经
build_commit_one_store 自检）→shadow 双工件→查询/就绪端点全程；
模式闸 fixed→shadow 阶梯（fixed 零判重循环实证→shadow 双腿→live 提交）。

R191 删除冻结：零删除零清场；本 run 对象（p01 三索引+p18 三索引+p19 集合）
原地封存，只读登记 log\temp\b6-created-objects.json 呈主窗口补登白名单。
保留索引 94+mabc 集合零触碰双快照 diff 硬停。证据 log\temp\b6-e3-truelink-<run>.json。

跑站命令（log\temp\ 落日志；<HHMM>=footer 分钟级）：

    $env:PYTHONPATH='src'
    $env:DEPLOY_ENV='test'
    $env:RELINK_CONFIRM_UAT='1'; $env:P18_CONFIRM_UAT='1'
    $env:P19_CONFIRM_UAT='1'; $env:EMB_CONFIRM_REAL='1'; $env:P21Q_CONFIRM_UAT='1'
    & '.venv-v1\\Scripts\\python.exe' -m pytest -p no:cacheprovider `
        --basetemp '.\\tmp\\b6-e3-run' -v tests/integration/test_b6_e3_truelink_uat.py `
        > ..\\log\\temp\\b6-e3-truelink-run-<HHMM>.txt
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

RELINK_ENV = "RELINK_CONFIRM_UAT"
P18_ENV = "P18_CONFIRM_UAT"
P19_ENV = "P19_CONFIRM_UAT"
EMB_ENV = "EMB_CONFIRM_REAL"
P21Q_ENV = "P21Q_CONFIRM_UAT"
SIX_GATES = (RELINK_ENV, "P23B_CONFIRM_UAT", P18_ENV, P19_ENV, EMB_ENV, P21Q_ENV)
SCOPE = "default"
SHADOW_BUDGET = 200_000          # §6.3 shadow 小闸（B5 同口径）

_REPO_ROOT = Path(__file__).resolve().parents[2]            # news-flash-dedup
_LOG_TEMP = _REPO_ROOT / "log" / "temp"
_CREATED_OBJECTS = _LOG_TEMP / "b6-created-objects.json"
_SHADOW_CACHE_ROOT = _REPO_ROOT / "tmp" / "emb-shadow-cache"   # B5 持久缓存域复用


def _gate_open() -> bool:
    from news_flash_dedup.vector import embedding_client as ec

    env = os.environ
    if any(env.get(flag) != "1" for flag in
           (RELINK_ENV, P18_ENV, P19_ENV, EMB_ENV, P21Q_ENV)):
        return False
    if not env.get("TEST_ES_HOST") or not env.get("TEST_MILVUS_HOST"):
        return False
    if any(k.startswith("PROD_ES_") or k.startswith("PROD_MILVUS_") for k in env):
        return False
    return ec.resolve_api_key(env) is not None


pytestmark = pytest.mark.skipif(
    not _gate_open(),
    reason="B6 E3 真链路协奏闸未开（需 RELINK/P18/P19/EMB_CONFIRM_REAL/P21Q=1 "
           "+ TEST_ES_*/TEST_MILVUS_* + key 可解析，无 PROD_*）",
)


# ---------- 公共助手 ----------

def _es_snapshot(client) -> dict[str, int]:
    rows = client.cat.indices(format="json", h="index,docs.count")
    return {item["index"]: int(item.get("docs.count") or 0)
            for item in rows if not item.get("index", "").startswith(".")}


def _is_dedup_family(name: str) -> bool:
    return name.startswith(("p01-batch-", "p17-batch-", "p18-batch-",
                            "p19-batch-", "p20-batch-", "news-dedup"))


def _register_objects(entries: list[dict]) -> None:
    _LOG_TEMP.mkdir(parents=True, exist_ok=True)
    registry = {"window": "B6/E3", "objects": []}
    if _CREATED_OBJECTS.exists():
        registry = json.loads(_CREATED_OBJECTS.read_text(encoding="utf-8"))
    known = {(obj["kind"], obj["name"]) for obj in registry["objects"]}
    for entry in entries:
        key = (entry["kind"], entry["name"])
        if key not in known:
            registry["objects"].append(entry)
            known.add(key)
    _CREATED_OBJECTS.write_text(
        json.dumps(registry, ensure_ascii=False, indent=2), encoding="utf-8")


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------- fixtures ----------

@pytest.fixture(scope="module")
def b6_cluster():
    """双客户端+双快照（R191 版）：跑后 diff=新 run 白名单之外零变化——
    94 保留索引/mabc 集合任何 missing/drift 即硬停；本 run 对象原地封存。"""
    from news_flash_dedup.es_client import (
        ProductionAccessDenied,
        assert_test_environment,
        client_from_environment,
        load_environment,
    )
    from news_flash_dedup.milvus_client import (
        ProductionMilvusAccessDenied,
        assert_test_milvus_environment,
        build_test_milvus_client,
    )

    load_environment()
    try:
        assert_test_environment()
        assert_test_milvus_environment()
    except (ProductionAccessDenied, ProductionMilvusAccessDenied) as error:
        pytest.fail(f"UAT environment check failed: {error}")  # pragma: no cover
    es = client_from_environment(load_dotenv_first=False)
    milvus = build_test_milvus_client()
    info = es.info()
    assert str(info["version"]["number"]).startswith("8.")
    idx_before = _es_snapshot(es)
    cols_before = sorted(milvus.list_collections())
    mabc_before = {name: milvus.get_collection_stats(collection_name=name)
                   for name in ("rag_knowledge_base",
                                "deerflow_rag_knowledge_base")
                   if name in cols_before}
    print(f"[B6-E3] baseline indices={len(idx_before)} "
          f"collections={len(cols_before)} mabc={sorted(mabc_before)}")
    yield {"es": es, "milvus": milvus, "idx_before": idx_before,
           "cols_before": cols_before, "mabc_before": mabc_before,
           "own_es_prefixes": [], "own_col_prefixes": []}
    idx_after = _es_snapshot(es)
    cols_after = sorted(milvus.list_collections())
    added_i = sorted(set(idx_after) - set(idx_before))
    missing_i = sorted(set(idx_before) - set(idx_after))
    drifted = sorted(n for n in set(idx_before) & set(idx_after)
                     if _is_dedup_family(n) and idx_before[n] != idx_after[n])
    added_c = sorted(set(cols_after) - set(cols_before))
    missing_c = sorted(set(cols_before) - set(cols_after))
    mabc_after = {name: milvus.get_collection_stats(collection_name=name)
                  for name in mabc_before}
    mabc_drift = sorted(name for name in mabc_before
                        if mabc_after.get(name) != mabc_before[name])
    own_prefixes = ("p01-batch-b6-", "p18-batch-b6relink-")
    own_i = [n for n in added_i if n.startswith(own_prefixes)]
    dedup_foreign_i = sorted(n for n in added_i
                             if _is_dedup_family(n) and n not in own_i)
    external_i = sorted(set(added_i) - set(own_i) - set(dedup_foreign_i))
    own_c = [c for c in added_c if c.startswith("p19_b6")]
    dedup_foreign_c = sorted(c for c in added_c
                             if c.startswith(("news_dedup_replay_", "p19_"))
                             and c not in own_c)
    external_c = sorted(set(added_c) - set(own_c) - set(dedup_foreign_c))
    diff = {"added_i": added_i, "missing_i": missing_i, "drifted": drifted,
            "added_c": added_c, "missing_c": missing_c,
            "mabc_drift": mabc_drift,
            "dedup_foreign_i": dedup_foreign_i,
            "dedup_foreign_c": dedup_foreign_c,
            "external_report_only": {"i": external_i, "c": external_c}}
    print(f"[B6-E3] snapshot diff: {json.dumps(diff, ensure_ascii=False)}")
    assert not missing_i and not missing_c, (
        f"保留对象被删（R191 硬停）：i={missing_i} c={missing_c}")
    assert not drifted, f"dedup 保留族文档数被触碰（硬停）：{drifted}"
    assert not mabc_drift, f"mabc 集合被触碰（硬停）：{mabc_drift}"
    assert not dedup_foreign_i and not dedup_foreign_c, (
        f"白名单外新增 dedup 族对象（越界硬停）："
        f"i={dedup_foreign_i} c={dedup_foreign_c}")
    milvus.close()
    es.close()


@pytest.fixture(scope="module")
def b6_env(b6_cluster):
    """真链装配：p01 三索引+p18 三索引+p19 集合+真嵌入客户端+闸扩展 store+
    build_real_vector_searcher（B5 交付面）。"""
    from news_flash_dedup.admission import BUSINESS_ZONE
    from news_flash_dedup.es_admission_schema import (
        control_mapping,
        item_mapping,
        request_mapping,
    )
    from news_flash_dedup.milvus_client import partition_name
    from news_flash_dedup.persist import es_store as p18_es
    from news_flash_dedup.recall.embedding_query import build_real_vector_searcher
    from news_flash_dedup.recall.vector_store import (
        MilvusVectorStore,
        vector_index_params,
        vector_schema,
    )
    from news_flash_dedup.vector import embedding_client as ec
    from news_flash_dedup.vector import qwen_bpe as qb

    es = b6_cluster["es"]
    milvus = b6_cluster["milvus"]
    run = "b6" + datetime.now(timezone.utc).strftime("%H%M%S%f")   # W-R3b 秒级→微秒
    day = datetime.now(BUSINESS_ZONE).date().isoformat()
    dotted = day.replace("-", ".")
    p01_prefix = f"p01-batch-b6-{run}-"
    p01_indices = {
        f"{p01_prefix}news-dedup-control-v1": control_mapping(),
        f"{p01_prefix}news-dedup-requests-v1": request_mapping(),
        f"{p01_prefix}news-dedup-items-v1-{dotted}": item_mapping(),
    }
    for name, schema in p01_indices.items():
        assert not bool(es.indices.exists(index=name))
        es.indices.create(index=name, body=schema)
    config = p18_es.RealESP18Config(
        run_uuid=f"b6relink-{run}", business_date=datetime.now(BUSINESS_ZONE).date())
    store18 = p18_es.RealESP18Store(es, config)      # 构造即幂等建三索引
    space = qb.real_embedding_space()
    collection = f"p19_{run}_{space.space_id}"
    partition = partition_name(day)
    assert milvus.has_collection(collection_name=collection) is False
    milvus.create_collection(collection_name=collection,
                             schema=vector_schema(space),
                             index_params=vector_index_params())
    milvus.create_partition(collection_name=collection,
                            partition_name=partition)
    emb_client = ec.EmbeddingClient(
        ec.EmbeddingClientConfig(
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            model="text-embedding-v3", dimension=1024,
            cache_root=_SHADOW_CACHE_ROOT,
            daily_token_budget=int(os.environ.get(
                "DEDUP_EMBEDDING_DAILY_TOKEN_BUDGET", str(SHADOW_BUDGET))),
            query_disk_cache_enabled=True),
        env=dict(os.environ))
    tokenizer = qb.QwenBpeTokenizer()
    store_vec = MilvusVectorStore(milvus, es, collection, p01_prefix, space)
    searcher = build_real_vector_searcher(
        client=emb_client, space=space, store=store_vec)
    print(f"[B6-E3] isolated: p01={sorted(p01_indices)}")
    print(f"[B6-E3] p18 family: {config.items_index} / {config.audits_index} / "
          f"{config.control_index}")
    print(f"[B6-E3] collection: {collection} partition={partition} "
          f"space={space.space_id}")
    _register_objects(
        [{"kind": "es_index", "name": name, "run": run,
          "created_by": "tests/integration/test_b6_e3_truelink_uat.py",
          "born": datetime.now(timezone.utc).isoformat(timespec="seconds")}
         for name in sorted(p01_indices)]
        + [{"kind": "es_index", "name": name, "run": run,
            "created_by": "tests/integration/test_b6_e3_truelink_uat.py",
            "born": datetime.now(timezone.utc).isoformat(timespec="seconds")}
           for name in (config.items_index, config.audits_index,
                        config.control_index)]
        + [{"kind": "milvus_collection", "name": collection, "run": run,
            "created_by": "tests/integration/test_b6_e3_truelink_uat.py",
            "born": datetime.now(timezone.utc).isoformat(timespec="seconds")}])
    yield {"run": run, "day": day, "p01_prefix": p01_prefix, "config": config,
           "store18": store18, "space": space, "collection": collection,
           "emb_client": emb_client, "tokenizer": tokenizer,
           "store_vec": store_vec, "searcher": searcher,
           "es": es, "milvus": milvus}
    print(f"[B6-E3 seal] R191 删除冻结：p01/p18/p19 三族对象原地封存（不删，"
          "已登记白名单补充档）")


# ---------- 模式闸 fixed→shadow 阶梯 ----------

def test_mode_ladder_fixed_to_shadow():
    from news_flash_dedup.recall.service import (
        RecallModeInvalid,
        mode_from_environment,
    )

    assert mode_from_environment({}) == "fixed"            # 缺省=fixed（现役锚）
    assert mode_from_environment({"DEDUP_RECALL_MODE": ""}) == "fixed"
    assert mode_from_environment({"DEDUP_RECALL_MODE": "fixed"}) == "fixed"
    assert mode_from_environment({"DEDUP_RECALL_MODE": "shadow"}) == "shadow"
    assert mode_from_environment({"DEDUP_RECALL_MODE": "live"}) == "live"
    for bad in ("FIXED", "Shadow", "bogus", "shadow "):
        with pytest.raises(RecallModeInvalid):
            mode_from_environment({"DEDUP_RECALL_MODE": bad})
    print("[B6-E3] 模式闸阶梯：fixed 缺省→shadow/live 三态+大小写变体 fail-closed")


# ---------- 六闸语义（协奏面） ----------

def test_gate_semantics_sixfold(b6_env):
    from news_flash_dedup.es_client import (
        ProductionAccessDenied,
        assert_test_environment,
    )
    from news_flash_dedup.milvus_client import (
        ProductionMilvusAccessDenied,
        assert_test_milvus_environment,
    )
    from news_flash_dedup.persist.es_store import (
        P18UATGatesNotOpen,
        assert_p18_uat_open,
    )
    from news_flash_dedup.vector import embedding_client as ec
    from news_flash_dedup.vector.milvus_store import (
        P19UATGatesNotOpen,
        assert_p19_uat_open,
    )

    env = dict(os.environ)
    present = {flag: env.get(flag) for flag in SIX_GATES}
    assert all(value == "1" for value in present.values()), present
    # 逐闸 fail-closed（env 副本注入，不扰进程环境）
    with pytest.raises(P18UATGatesNotOpen):
        assert_p18_uat_open(env={**env, P18_ENV: "0"})
    with pytest.raises(P19UATGatesNotOpen):
        assert_p19_uat_open(env={**env, P19_ENV: "0"})
    with pytest.raises(ProductionMilvusAccessDenied):
        assert_test_milvus_environment(env={**env, "TEST_MILVUS_HOST": ""})
    with pytest.raises(ProductionAccessDenied):
        assert_test_environment(env={**env, "PROD_ES_HOST": "prod.example"})
    assert ec.resolve_api_key({}) is None
    evidence = {"run": b6_env["run"], "six_gates_present": present,
                "fail_closed_probes": {
                    "P18_without_flag": "P18UATGatesNotOpen",
                    "P19_without_flag": "P19UATGatesNotOpen",
                    "milvus_without_host": "ProductionMilvusAccessDenied",
                    "es_with_prod_leak": "ProductionAccessDenied",
                    "emb_key_absent": "None（EmbeddingKeyMissing 前置拦截面）"}}
    out = _LOG_TEMP / f"b6-e3-gates-{b6_env['run']}.json"
    out.write_text(json.dumps(evidence, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    print(f"[B6-E3] 六闸在场+逐闸 fail-closed 实证 -> {out}")


# ---------- E2E 主链 ----------

def test_e3_truelink_end_to_end(b6_env):
    from fastapi.testclient import TestClient

    from news_flash_dedup.admission import AdmissionRequest
    from news_flash_dedup.api.host import create_host
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
    from news_flash_dedup.recall.service import NullVectorSearcher, RecallService
    from news_flash_dedup.recall.vector_store import prepare_vector_row
    from news_flash_dedup.recall.worker import (
        DedupWorker,
        ElasticsearchRegistrationScanner,
    )
    from news_flash_dedup.vector.qwen_bpe import real_chunk_text

    env = b6_env
    es = env["es"]
    day = env["day"]
    run = env["run"]
    p01_prefix = env["p01_prefix"]
    config = env["config"]
    store18 = env["store18"]
    space = env["space"]
    searcher = env["searcher"]
    usage_0 = env["emb_client"].usage_snapshot()

    # ---------- 受理→物化（p01 族；真空间身份随行） ----------
    store = ElasticsearchBatchStore(es, index_prefix=p01_prefix)
    coordinator = BatchAdmissionCoordinator(
        store, owner_id=f"b6-{run}", owner_isolated=lambda: True,
        limits=BatchLimits(), clock=_now)
    texts = [
        "美国能源信息署公布原油库存增加。",
        "美国能源信息署公布原油库存增加。",       # 与 seq1 同文：hash 命中对
        "美国能源信息署（EIA）公布原油库存录得增加。",  # seq1 释义对
        "央行开展中期借贷便利操作维护流动性合理充裕。",
        "某市发布新一轮新能源汽车购置补贴细则。",
        "国际油价小幅收涨市场关注供应端变化。",
    ]
    requests = [
        AdmissionRequest(
            scope_id=SCOPE, request_id=f"9-{i}", item_id=f"9-{i}", text=text,
            received_at=_now(), schema_version="v1", pipeline_version="dedup_v1",
            embedding_space_id=space.space_id,       # 真空间身份（非 space-x 占位）
            delivery_route_ref="route-b6", trace_id=f"trace-b6-{i}")
        for i, text in enumerate(texts, start=1)
    ]
    receipts = []
    for offset in (0, 3):
        receipts.extend(coordinator.accept_batch(requests[offset:offset + 3]))
    assert [receipt.arrival_seq for receipt in receipts] == [1, 2, 3, 4, 5, 6]
    while coordinator.materialize_oldest() < 6:
        pass
    es.indices.refresh(index=f"{p01_prefix}news-dedup-items-v1-{day.replace('-', '.')}")
    record_ids = [receipt.record_id for receipt in receipts]
    print(f"[B6-E3] materialized 6 docs, record_ids={[r[:8] for r in record_ids]}")
    # 供 N38 探针用：物化即登记（补丁⑫ 自尾部前移——前件后续断言若红，
    # N38 件仍读到键，失败形态由 KeyError 级联转为真实首红点）
    b6_env["record_ids"] = record_ids
    b6_env["texts"] = texts

    # ---------- 准备（F2 真驱动） ----------
    prepare_worker = PrepareWorker(es, p01_prefix, clock=_now)
    report = prepare_worker.prepare(SCOPE, day)
    assert report.conflicts == ()
    assert sorted(report.prepared) == sorted(record_ids)
    assert report.prepared_seq == 6
    provider = ElasticsearchWatermarkProvider(es, p01_prefix)
    assert provider.visible_seq(SCOPE, day) == 6
    assert provider.prepared_seq(SCOPE, day) == 6
    print(f"[B6-E3] prepare done: prepared_seq={report.prepared_seq}")

    # ---------- 向量播种（真分块/真嵌入/闸扩展 store 双写对拍） ----------
    seeded = []
    for receipt, text in zip(receipts, texts):
        chunks = real_chunk_text(text, env["tokenizer"])
        assert len(chunks) == 1                     # 快讯短文单 chunk 域
        vectors = env["emb_client"].embed_documents([chunks[0].text])
        row = prepare_vector_row(receipt.record_id, SCOPE, day,
                                 receipt.arrival_seq, space, 0,
                                 list(vectors[0]))
        outcome = env["store_vec"].upsert(row)
        seeded.append((receipt.record_id[:8], outcome))
    assert all(outcome == "created" for _rid, outcome in seeded)
    print(f"[B6-E3] vector seeded: {seeded}")

    # ---------- 召回服务（真五通道：真 embedding 编入 RRF 融合） ----------
    service = RecallService(es, p01_prefix, vector_searcher=searcher, clock=_now)
    scanner = ElasticsearchRegistrationScanner(es, p01_prefix)

    # ---------- fixed 腿（阶梯第一阶：零判重循环） ----------
    sink: list[tuple[str, dict]] = []

    class _Sink:
        def emit(self, kind: str, payload: dict) -> None:
            sink.append((kind, payload))

    fixed_worker = DedupWorker(
        mode="fixed", recall_service=service, prepare_worker=prepare_worker,
        watermark_provider=provider, scanner=scanner, clock=_now)
    fixed_report = fixed_worker.drive_once(SCOPE, day)
    assert fixed_report.committed == fixed_report.shadowed == ()
    assert fixed_report.failed == fixed_report.stale == ()
    assert sink == []
    for index in (config.items_index, config.audits_index):
        assert es.count(index=index)["count"] == 0
    print("[B6-E3] fixed 腿：零工件零提交（不启动判重循环）")

    # ---------- shadow 腿（真计划+影子决策双工件，零仓储调用） ----------
    shadow_worker = DedupWorker(
        mode="shadow", recall_service=service, prepare_worker=prepare_worker,
        watermark_provider=provider, scanner=scanner,
        artifact_sink=_Sink(), clock=_now)
    shadow_report = shadow_worker.drive_once(SCOPE, day)
    assert sorted(shadow_report.shadowed) == sorted(record_ids)
    assert shadow_report.committed == shadow_report.failed == ()
    kinds = [kind for kind, _ in sink]
    assert kinds.count("recall_plan") == 6
    assert kinds.count("shadow_decision") == 6
    shadow_by_record = {}
    plan_by_record = {}
    for kind, payload in sink:
        if kind == "shadow_decision":
            shadow_by_record[payload["record_id"]] = payload["outcome"]
        else:
            plan_by_record[payload["record_id"]] = payload
    # 真 embedding 通道编入断言：embedding 审计 status=complete（非过渡态
    # unavailable）；seq2 的 embedding 候选含 seq1（cos≈1 恒等召回）
    for rid, payload in plan_by_record.items():
        audit = {a["channel"]: a for a in payload["channel_audits"]}
        assert audit["embedding"]["status"] == "complete", (
            f"embedding 通道未真编入：{audit['embedding']}")
        assert audit["embedding"]["query_version"] == "embedding_v1"
    seq2_plan = plan_by_record[record_ids[1]]
    emb_candidates = [c["record_id"] for c in seq2_plan["required"]
                      if "embedding" in c["channels"]]
    assert record_ids[0] in emb_candidates, (
        f"真 embedding 未召回恒等前序：required={seq2_plan['required']}")
    # coverage 机械派生（恒 False→COVERAGE_UNPROVEN，缺口明示不掩盖）
    for rid, payload in plan_by_record.items():
        assert payload["recall_incomplete"] is True
        assert any(gap["channel"] == "embedding"
                   and gap["reason"] == "COVERAGE_UNPROVEN"
                   for gap in payload["recall_gaps"])
    # 影子路径零仓储调用（真集群断言）
    for index in (config.items_index, config.audits_index, config.control_index):
        assert es.count(index=index)["count"] == 0, f"shadow leg wrote {index}"
    print("[B6-E3] shadow 腿：12 工件、真 embedding 通道 complete 编入、"
          "p18 三索引恒 0（零仓储调用）")

    # ---------- live 腿（真 commit E2E） ----------
    commit_store = build_commit_one_store(store18)
    live_worker = DedupWorker(
        mode="live", recall_service=service, prepare_worker=prepare_worker,
        watermark_provider=provider, scanner=scanner,
        commit_store=commit_store, clock=_now)
    live_report = live_worker.drive_once(SCOPE, day)
    assert live_report.stale == live_report.failed == live_report.awaiting == ()
    assert list(live_report.committed) == record_ids       # 有序提交升序单飞
    assert store18.get_watermark(SCOPE, day) == 6

    # ---------- live 结果回读（五字段+payload_hash+审计 ids+pending 可见） ----------
    live_outcomes = {}
    audit_total = 0
    mismatches = []
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
        assert set(result) == {"item_id", "decision", "duplicate_ids", "reason"}
        assert result["item_id"] == receipt.item_id
        assert source["text"] == texts[receipt.arrival_seq - 1]
        # payload_hash 复算（五字段 JCS→sha256，commit/coordinator.py:61-68）
        public = {"decision": result["decision"],
                  "duplicate_ids": result["duplicate_ids"],
                  "item_id": result["item_id"],
                  "reason": result["reason"],
                  "text": source["text"]}
        callback_body = json.dumps(public, sort_keys=True, ensure_ascii=False)
        assert source["callback_body_hash"] == hashlib.sha256(
            callback_body.encode("utf-8")).hexdigest(), "payload_hash 复算不一致"
        assert source["audit_complete"] is True
        assert source["event_id"]
        live_outcomes[receipt.record_id] = result
        # 双跑对拍（腿 A=live commit vs 腿 B=shadow 决策：五字段+签发码）
        shadow = shadow_by_record[receipt.record_id]
        for key in ("item_id", "text", "decision", "duplicate_ids", "reason"):
            live_value = (source["text"] if key == "text" else result[key])
            if live_value != shadow[key]:
                mismatches.append((receipt.record_id[:8], key))
        if shadow["internal_code"] != source["reason_code"]:
            mismatches.append((receipt.record_id[:8], "internal_code"))
        audit_total += len(source["audit_ids"])
    assert not mismatches, f"双跑不一致：{mismatches[:3]}"
    # 审计可解析+同文对被精判（hash 命中不进 shortcut）
    audit_count = es.count(index=config.audits_index)["count"]
    assert audit_total == audit_count and audit_count >= 1
    judged_pairs = set()
    for rid in record_ids:
        for audit_id in store18.get_main_record(rid)["source"]["audit_ids"]:
            audit = es.get(index=config.audits_index, id=audit_id,
                           realtime=True)["_source"]
            judged_pairs.add((audit["history_record_id"],
                              audit["current_record_id"]))
    assert (record_ids[0], record_ids[1]) in judged_pairs
    # 同文对必判（上方钉）下的现役派生（补丁⑫ 对齐 decide 现役语义，B4 窗
    # F7 20:01 修测试先例——对真实语义对齐，非削断言凑绿）：本链 facts 由
    # 缺省 IdentityFactSupply 恒等投影供给（21:4x 主窗口裁定零语义占位），
    # time 槽=unverified missing → N29 honest-missing 未决（FACT_INCOMPLETE）
    # 优先于 EXACT_TEXT_MATCH 证书 → coverage=False 派生边界。该投影按裁定
    # 不产生任何"重复"输出（test_p23_uat.py:352 钉；test_recall_worker.py
    # :210/:438 同文两路同判疑难钉）；"重复"签发能力（EXACT_TEXT_MATCH 合格
    # 工件路径）由 tests/unit/test_p15_text_certificate.py B1-1 单元面钉死。
    # decide_for_task+恒等投影本地复算同文：pair=(unresolved,FACT_INCOMPLETE)。
    second = store18.get_main_record(record_ids[1])["source"]
    assert live_outcomes[record_ids[1]]["decision"] == "边界case/疑难case"
    assert second["reason_code"] == "FACT_INCOMPLETE"
    assert not live_outcomes[record_ids[1]]["duplicate_ids"]
    # 首条零候选+coverage=False→边界/RECALL_INCOMPLETE
    first = store18.get_main_record(record_ids[0])["source"]
    assert first["result"]["decision"] == "边界case/疑难case"
    assert first["reason_code"] == "RECALL_INCOMPLETE"
    print("[B6-E3] live 腿：6/6 committed；五字段/payload_hash/审计/pending 全过；"
          "双跑 6/6 一致；同文对必判+占位投影边界派生（FACT_INCOMPLETE）；"
          "首条 RECALL_INCOMPLETE 派生演示")

    # ---------- 查询/就绪端点全程 ----------
    # 受理未物化（pending 投影）
    receipt_q = coordinator.accept_batch([AdmissionRequest(
        scope_id=SCOPE, request_id="9-7", item_id="9-7",
        text="证监会就程序化交易新规公开征求意见。",
        received_at=_now(), schema_version="v1", pipeline_version="dedup_v1",
        embedding_space_id=space.space_id, delivery_route_ref="route-b6",
        trace_id="trace-b6-7")])[0]
    # 补丁⑫：登记挂账受理件（seq7 受理未物化占位）——N38 探针驱动序=
    # 先生效腿补提交本件、再触探针分歧；孔洞纪律（L175）下不补物化前沿即停
    b6_env["pending_record_id"] = receipt_q.record_id
    b6_env["pending_seq"] = receipt_q.arrival_seq
    host = create_host(
        es_client=es, index_prefix=p01_prefix, scope_id=SCOPE,
        status_authenticated=lambda request: True,
        env={"DEDUP_RECALL_MODE": "shadow"}, clock=_now,
        vector_searcher=searcher, artifact_sink=_Sink(),
        collector_alive=True, delivery_route_configured=True)
    ready, codes = host.readiness_port.issues()
    assert (ready, codes) == (True, []), (
        f"真 searcher 装配面下 shadow 就绪应全绿（无 degraded 码）：{codes}")
    with TestClient(host.query_app) as http:
        ready_resp = http.get("/ready")
        pending_resp = http.get("/v1/api/task/9-7")
        shell_resp = http.get("/v1/api/task/9-1")
        missing_resp = http.get("/v1/api/task/8-8")
    assert ready_resp.status_code == 200
    assert ready_resp.json() == {"ready": True, "issues": [], "mode": "shadow"}
    assert pending_resp.status_code == 200
    assert pending_resp.json()["taskState"] == "accepted"     # pending 投影
    assert shell_resp.status_code == 200
    shell_body = shell_resp.json()
    assert shell_body["taskState"] == "accepted"   # 受理壳真形（V1 偏差登记：
    # 终态在 p18 主记录，查询读 p01 受理壳——B4 终报偏差登记在案，不冒充终态）
    assert shell_body["result"] is None
    assert missing_resp.status_code == 404
    print("[B6-E3] 查询/就绪端点全程：/ready 全绿（真 searcher 零 degraded 码）"
          "+pending/受理壳/404 三态过")

    # ---------- 证据落盘 ----------
    usage_1 = env["emb_client"].usage_snapshot()
    usage_delta = {key: usage_1.get(key, 0) - usage_0.get(key, 0)
                   for key in ("total_tokens", "api_calls", "cache_hits",
                               "estimated_usage_events")}
    evidence = {
        "run": run, "business_date": day,
        "space_id": space.space_id, "collection": env["collection"],
        "p01_prefix": p01_prefix, "p18_run_uuid": config.run_uuid,
        "mode_ladder": "fixed(零工件)→shadow(12 工件)→live(6/6 committed)",
        "embedding_channel": {"status": "complete", "编入": "RRF 融合双腿",
                              "恒等召回": f"{record_ids[0][:8]}∈seq2 required"},
        "coverage_mechanics": "store 恒 coverage_complete=False→gap "
                              "COVERAGE_UNPROVEN（fusion.py:111-112 机械形态）",
        "dual_run": {"compared": len(record_ids),
                     "mismatches": len(mismatches)},
        "dedup_signing_semantics": (
            "同文对必判在案；缺省恒等投影（21:4x 裁定零语义占位）下现役派生="
            "边界/FACT_INCOMPLETE（N29 honest-missing；test_p23_uat.py:352 钉）"
            "；重复签发能力=EXACT_TEXT_MATCH 合格工件路径，unit B1-1 钉"),
        "embedding_usage_delta": usage_delta,
        "query_ready": {"ready": [ready, codes], "pending_projection": True,
                        "accepted_shell": True, "missing_404": True},
    }
    out = _LOG_TEMP / f"b6-e3-truelink-{run}.json"
    out.write_text(json.dumps(evidence, ensure_ascii=False, indent=2,
                              default=str), encoding="utf-8")
    print(f"[B6-E3] evidence -> {out}")
    # N38 探针登记已于物化段前移（补丁⑫），此处不再尾部登记


# ---------- N38 挂账项真链评估（worker 捕获面 vs 真层分歧响亮死） ----------

def test_n38_divergence_controlled_vs_loud(b6_env):
    from news_flash_dedup.admission import AdmissionRequest
    from news_flash_dedup.batch_admission import BatchLimits, BatchAdmissionCoordinator
    from news_flash_dedup.batch_es_store import ElasticsearchBatchStore
    from news_flash_dedup.commit.coordinator import CommitOneError
    from news_flash_dedup.persist.divergence import (
        AuditDivergenceError,
        MainRecordDivergenceError,
    )
    from news_flash_dedup.persist.es_store import (
        CASConflictError,
        build_commit_one_store,
    )
    from news_flash_dedup.recall.prepare import (
        ElasticsearchWatermarkProvider,
        PrepareWorker,
    )
    from news_flash_dedup.recall.service import RecallService
    from news_flash_dedup.recall.vector_store import prepare_vector_row
    from news_flash_dedup.recall.worker import (
        DedupWorker,
        ElasticsearchRegistrationScanner,
    )
    from news_flash_dedup.vector.qwen_bpe import real_chunk_text

    env = b6_env
    es = env["es"]
    day = env["day"]
    p01_prefix = env["p01_prefix"]
    space = env["space"]
    store18 = env["store18"]
    if "record_ids" not in env:
        # 补丁⑫：前件（E2E 主链）未跑至物化即红时显式依赖信号，不再 KeyError
        pytest.skip("E2E 主链未物化登记（前件红）——N38 探针依赖其 6 条主记录")
    record_ids = env["record_ids"]

    # 双契约子型探针（静态）
    assert issubclass(MainRecordDivergenceError, CommitOneError)
    assert issubclass(MainRecordDivergenceError, CASConflictError)
    assert issubclass(AuditDivergenceError, CommitOneError)

    # ---------- 分歧→受控 failed（worker 捕获面实证） ----------
    store = ElasticsearchBatchStore(es, index_prefix=p01_prefix)
    # 补丁⑫：与 E2E 主链同 run 同 owner 续收（seq7 同域）——异 owner 触
    # batch_admission.py:201-202 所有权篱 AdmissionConflict（篱语义本职执法，
    # 同 run 跨测试续收不属越界；原 b6-n38- 异名=把同域续收误建模成跨 owner）
    coordinator = BatchAdmissionCoordinator(
        store, owner_id=f"b6-{env['run']}", owner_isolated=lambda: True,
        limits=BatchLimits(), clock=_now)
    text7 = "国家发展改革委部署下一阶段稳增长政策举措。"
    receipt7 = coordinator.accept_batch([AdmissionRequest(
        scope_id=SCOPE, request_id="9-9", item_id="9-9", text=text7,
        received_at=_now(), schema_version="v1", pipeline_version="dedup_v1",
        embedding_space_id=space.space_id, delivery_route_ref="route-b6",
        trace_id="trace-b6-9")])[0]
    # 补丁⑫：探针序派生不自硬编码——E2E 查询演示件 9-7 受理占位 seq7
    # （pending_seq 登记在案），本探针=其下一序；孔洞纪律（L175 缺号不越过）
    # 下 9-7 须一并补物化，准备前沿才连续推进至 probe_seq
    probe_seq = receipt7.arrival_seq
    assert probe_seq == env["pending_seq"] + 1
    while coordinator.materialize_oldest() < probe_seq:
        pass
    es.indices.refresh(
        index=f"{p01_prefix}news-dedup-items-v1-{day.replace('-', '.')}")
    prepare_worker = PrepareWorker(es, p01_prefix, clock=_now)
    report = prepare_worker.prepare(SCOPE, day)
    assert receipt7.record_id in report.prepared
    provider = ElasticsearchWatermarkProvider(es, p01_prefix)
    assert provider.prepared_seq(SCOPE, day) == probe_seq
    chunks = real_chunk_text(text7, env["tokenizer"])
    vectors = env["emb_client"].embed_documents([chunks[0].text])
    row = prepare_vector_row(receipt7.record_id, SCOPE, day, probe_seq, space, 0,
                             list(vectors[0]))
    assert env["store_vec"].upsert(row) == "created"
    # 篡改预埋：p18 主记录先占（内容与真决策恒异）→ 重试分歧真复现
    tampered_body = {
        "record_id": receipt7.record_id, "item_id": "9-9",
        "text": "篡改变体（N38 分歧探针预埋，非真决策产物）",
        "result": {"item_id": "9-9", "decision": "不重复",
                   "duplicate_ids": [], "reason": "预埋分歧体。"},
        "task_state": "succeeded", "delivery_state": "pending",
    }
    es.index(index=env["config"].items_index, id=receipt7.record_id,
             document=tampered_body, op_type="create", refresh="wait_for")
    service = RecallService(es, p01_prefix, vector_searcher=env["searcher"],
                            clock=_now)
    scanner = ElasticsearchRegistrationScanner(es, p01_prefix)
    live_worker = DedupWorker(
        mode="live", recall_service=service, prepare_worker=prepare_worker,
        watermark_provider=provider, scanner=scanner,
        commit_store=build_commit_one_store(store18), clock=_now)
    drive = live_worker.drive_once(SCOPE, day)
    # 补丁⑫：生效腿自水位 6 升序单飞——先补提交挂账件 9-7（seq7 常态提交，
    # 水印续推实证），再触探针预埋分歧（N38 核心断言：重试分歧→受控 failed，
    # 不冒泡崩溃=非响亮死）
    assert drive.committed == (env["pending_record_id"],), drive
    assert drive.failed == (receipt7.record_id,), drive
    # 预埋体未被覆盖（fail-closed 零写入）
    still = store18.get_main_record(receipt7.record_id)["source"]
    assert still["text"] == tampered_body["text"]
    print("[B6-E3 N38] 挂账件补提交+重试分歧→受控 failed（worker 捕获面真链"
          "实证）；预埋体零覆盖")

    # ---------- 破隔离真并发→保持响亮死（不静默单飞破口） ----------
    victim = store18.get_main_record(record_ids[0])
    assert victim is not None
    body = dict(victim["source"])
    body.update({"delivery_state": "pending", "task_state": "succeeded",
                 "audit_complete": True})
    with pytest.raises(CASConflictError):
        store18.cas_main_record(
            record_ids[0], body,
            expected_seq_no=victim["seq_no"] + 999,      # 版本竞争注入
            expected_primary_term=victim["primary_term"])
    print("[B6-E3 N38] 真并发 CAS 冲突→裸 CASConflictError 响亮死保持"
          "（修法①边界：不静默）")
