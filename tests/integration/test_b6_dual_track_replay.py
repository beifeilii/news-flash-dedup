r"""B6/E3 ③：双轨对照——(a) 固定候选轨 vs (b) 真召回轨（真 embedding 五通道）
同金标语料对拍 UAT。

底本：B4 F5（test_p23b_recall_replay.py，NullVectorSearcher 过渡态 (b)-null
轨，run=b4125430）+B4′ F-2 勘误（77 例 coverage 分歧真根因=候选集差异）+
B5 交付面（build_real_vector_searcher+闸扩展 store+真空间
s_0dbdb6fe，1024/COSINE/tokens512_overlap64_bpe_v1）。本件把 (b) 轨的
embedding 通道从 Null 过渡态换为真通道（(b)-real），同语料同口径重跑并对拍。

闸：`P23B_CONFIRM_UAT=1`+`P19_CONFIRM_UAT=1`+`EMB_CONFIRM_REAL=1`+
`DEPLOY_ENV=test`+`TEST_ES_*`/`TEST_MILVUS_*` 就位+key 可解析+无 `PROD_*` 泄漏。

对照口径（任务书 ③ 逐项）：
- (a) 轨=腿 A 固定候选决策（commit_one+FakeCommitStore，coverage_complete=True
  显式占位同文，与 B4 逐行同构）——重跑以自证确定性并与两 (b) 轨三方对照；
- (b)-null 轨=B4 工件只读装载（log\temp\shadow-decision-b4125430.json /
  p23b-replay-b4125430.json，workspace-dedup 侧，零写）；
- (b)-real 轨=真召回计划（真 embedding 通道编入 RRF 融合）+影子决策；
- 决策五字段一致率（(a) vs (b)-real，逐对+逐字段+签发码）+候选集差异登记
  （(b)-real vs (b)-null 逐 current required/pair_top10 差集与通道归因）+
  77 例 coverage 分歧已勘误形态照实呈（先自证复算=77，再呈 (b)-real 下该
  77 例的决策/签发码对照）；
- fp=0：金标"不重复"对任一腿签发重复即红；
- R191 删除冻结：零删除零清场；本 run 对象（p01 双索引+p19 集合）原地封存
  只读登记 log\temp\b6-created-objects.json；保留族快照 diff 硬停；
- ④ 配套：merged_top30/pair_top10/required ID 全量入本件证据 JSON
  （log\temp\b6-e3-dual-track-<run>.json），Recall@30/全量合法性可由工件
  复算（merged_top30 键增补=④ 呈裁候选，本件不改 worker.py）。

物化口径承 B4（呈裁登记在案）：synthetic 业务日 2000-01-01、expires_at 置
2100-01-01（回放留账需要，非留存语义主张）；改动=①embedding_space_id
由 "gold-synthetic" 换为真空间 space_id（闸扩展 store ES 权威对拍需要，
B5′ 建议面落实）；②scope_id 冒号→连字符可信标量适配（补丁⑫ 增补：
vector_store.py:76/:206 词表 [a-z0-9_-]，金标 synthetic:<hex16> 冒号越表，
run-dualtrack-1450 实证 ValueError——前窗物化口径漏配；双射改名，分区
结构/逐对语义不变，B4 对拍键=record_id 不涉 scope 名）。

跑站命令（log\temp\ 落日志；<HHMM>=footer 分钟级）：

    $env:PYTHONPATH='src'
    $env:DEPLOY_ENV='test'
    $env:P23B_CONFIRM_UAT='1'; $env:P19_CONFIRM_UAT='1'; $env:EMB_CONFIRM_REAL='1'
    & '.venv-v1\\Scripts\\python.exe' -m pytest -p no:cacheprovider `
        --basetemp '.\\tmp\\b6-dual-run' -v tests/integration/test_b6_dual_track_replay.py `
        > ..\\log\\temp\\b6-e3-dual-track-run-<HHMM>.txt
"""

from __future__ import annotations

import json
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

P23B_ENV = "P23B_CONFIRM_UAT"
P19_ENV = "P19_CONFIRM_UAT"
EMB_ENV = "EMB_CONFIRM_REAL"
BUSINESS_DATE = "2000-01-01"                    # P05 synthetic 冻结业务日
EXPIRY_REPLAY = "2100-01-01T00:00:00+08:00"
SEEDED_AT = "2000-01-01T00:00:00+08:00"
SHADOW_BUDGET = 200_000
B4_RUN = "b4125430"                              # B4 (b)-null 轨工件钉（只读）

_REPO_ROOT = Path(__file__).resolve().parents[2]            # news-flash-dedup
_WORKSPACE_ROOT = Path(__file__).resolve().parents[3]       # workspace-dedup
_LOG_TEMP = _REPO_ROOT / "log" / "temp"
_WORKSPACE_LOG_TEMP = _WORKSPACE_ROOT / "log" / "temp"
_CREATED_OBJECTS = _LOG_TEMP / "b6-created-objects.json"
_SHADOW_CACHE_ROOT = _REPO_ROOT / "tmp" / "emb-shadow-cache"
_B4_BUNDLE = _WORKSPACE_LOG_TEMP / f"shadow-decision-{B4_RUN}.json"
_B4_METRICS = _WORKSPACE_LOG_TEMP / f"p23b-replay-{B4_RUN}.json"


def _gate_open() -> bool:
    from news_flash_dedup.vector import embedding_client as ec

    env = os.environ
    if any(env.get(flag) != "1" for flag in (P23B_ENV, P19_ENV, EMB_ENV)):
        return False
    if not env.get("TEST_ES_HOST") or not env.get("TEST_MILVUS_HOST"):
        return False
    if any(k.startswith("PROD_ES_") or k.startswith("PROD_MILVUS_") for k in env):
        return False
    return ec.resolve_api_key(env) is not None


pytestmark = pytest.mark.skipif(
    not _gate_open(),
    reason="B6 双轨对照闸未开（需 P23B/P19/EMB_CONFIRM_REAL=1 + TEST_ES_*/"
           "TEST_MILVUS_* + key 可解析，无 PROD_*）",
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


def _workbook_paths() -> tuple[Path, ...]:
    from test_p23_uat import (
        _DEFAULT_WORKBOOK_DIR,
        P23_WORKBOOK_DIR_ENV,
        WORKBOOK_NAMES,
    )

    workbook_dir = Path(os.environ.get(P23_WORKBOOK_DIR_ENV,
                                       str(_DEFAULT_WORKBOOK_DIR)))
    books = tuple(workbook_dir / name for name in WORKBOOK_NAMES)
    missing = [b.name for b in books if not b.exists()]
    if missing:
        pytest.skip(f"金标工作簿不在本机（缺 {missing}）——诚实 skip（非红非绿）")
    return books


def _percentile(sorted_counts: list[int], q: int) -> int:
    rank = max(1, math.ceil(q / 100 * len(sorted_counts)))
    return sorted_counts[rank - 1]


def _count_stats(counts: list[int]) -> dict:
    if not counts:
        return {"total": 0, "p50": None, "p95": None, "p99": None,
                "max": None, "n": 0}
    ordered = sorted(counts)
    return {"total": sum(counts), "p50": _percentile(ordered, 50),
            "p95": _percentile(ordered, 95), "p99": _percentile(ordered, 99),
            "max": ordered[-1], "n": len(counts)}


def _vector_scope(scope_id: str) -> str:
    """补丁⑫：闸扩展 store 可信标量适配——vector_store.py:76（prepare_vector_row
    硬校验）/:206（search 软拒 SCOPE_INVALID）词表 [a-z0-9_-]{1,128}，金标
    `synthetic:<hex16>` 的冒号越表。冒号→连字符：本语料双射无碰撞（scope 仅
    `synthetic:` 前缀两值），分区结构/逐对语义不变（scope 只做等值分界；
    decide 绑定门比等值、B4 (b)-null 对拍键=record_id 不涉 scope 名）。
    物化/播种/召回/水位/合法性检查全域同名；腿 A（FakeCommitStore 不触集群）
    保持金标原形。"""
    return scope_id.replace(":", "-")


# ---------- fixtures ----------

@pytest.fixture(scope="module")
def dt_cluster():
    """双客户端+双快照（R191 版）：94 保留索引/mabc 集合任何 missing/drift
    即硬停；本 run 对象原地封存只读登记。"""
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
    print(f"[B6-DT] baseline indices={len(idx_before)} "
          f"collections={len(cols_before)}")
    yield {"es": es, "milvus": milvus, "idx_before": idx_before,
           "cols_before": cols_before, "mabc_before": mabc_before}
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
    own_prefixes = ("p01-batch-b6dt-",)
    own_i = [n for n in added_i if n.startswith(own_prefixes)]
    dedup_foreign_i = sorted(n for n in added_i
                             if _is_dedup_family(n) and n not in own_i)
    external_i = sorted(set(added_i) - set(own_i) - set(dedup_foreign_i))
    own_c = [c for c in added_c if c.startswith("p19_b6dt")]
    dedup_foreign_c = sorted(c for c in added_c
                             if c.startswith(("news_dedup_replay_", "p19_"))
                             and c not in own_c)
    external_c = sorted(set(added_c) - set(own_c) - set(dedup_foreign_c))
    print(f"[B6-DT] snapshot diff added_i={added_i} missing_i={missing_i} "
          f"drifted={drifted} added_c={added_c} missing_c={missing_c} "
          f"mabc_drift={mabc_drift}")
    print(f"[B6-DT] external tenant drift (report only): "
          f"i={external_i} c={external_c}")
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
def dt_gold():
    """金标资产：INV-1 钉死复算+工作簿对拍+回放计划（与 B4 (b)-null 同文）。"""
    from test_p23_uat import (
        _build_replay_plan,
        _file_sha256,
        _load_manifest,
        _verify_manifest_pinned,
    )

    manifest = _load_manifest()
    _verify_manifest_pinned(manifest)
    books = _workbook_paths()
    materials = {m["material_id"]: m for m in manifest["materials"]}
    for book in books:
        digest = _file_sha256(book)
        assert digest in materials and \
            book.stat().st_size == materials[digest]["byte_size"], (
                f"工作簿 {book.name} 哈希/大小不在 manifest 钉死清单（INV-1 红）")
    from news_flash_dedup.gold import load_workbooks

    catalog = load_workbooks(books)
    rows = {row.row_key: row for row in catalog.rows}
    plan_by_row = {r["row_key"]: r
                   for r in manifest["synthetic_plan"]["rows"]}
    clear, hold = _build_replay_plan(manifest, rows, plan_by_row)
    print(f"[B6-DT] gold: rows={len(rows)} clear_pairs={len(clear)} "
          f"hold={len(hold)}")
    return {"manifest": manifest, "rows": rows, "plan_by_row": plan_by_row,
            "clear": clear, "hold": hold}


@pytest.fixture(scope="module")
def dt_env(dt_cluster, dt_gold):
    """隔离装配：p01-batch-b6dt-<stamp>- 双索引+951 行物化（真空间身份）+
    p19 集合+真嵌入客户端+闸扩展 store+build_real_vector_searcher。"""
    from test_p23_uat import _digest, _item_id, _record_id

    from news_flash_dedup.batch_admission import HEAD_ID, LOG_VERSION
    from news_flash_dedup.es_admission_schema import control_mapping, item_mapping
    from news_flash_dedup.milvus_client import partition_name
    from news_flash_dedup.recall.embedding_query import build_real_vector_searcher
    from news_flash_dedup.recall.vector_store import (
        MilvusVectorStore,
        vector_index_params,
        vector_schema,
    )
    from news_flash_dedup.vector import embedding_client as ec
    from news_flash_dedup.vector import qwen_bpe as qb

    es = dt_cluster["es"]
    milvus = dt_cluster["milvus"]
    stamp = datetime.now(timezone.utc).strftime("%H%M%S%f")   # W-R3b 秒级→微秒
    run = "b6dt" + stamp
    prefix = f"p01-batch-b6dt-{stamp}-"
    items_index = f"{prefix}news-dedup-items-v1-2000.01.01"
    control_index = f"{prefix}news-dedup-control-v1"
    space = qb.real_embedding_space()
    collection = f"p19_{run}_{space.space_id}"
    partition = partition_name(BUSINESS_DATE)
    es.indices.create(index=items_index, body=item_mapping())
    es.indices.create(index=control_index, body=control_mapping())
    milvus.create_collection(collection_name=collection,
                             schema=vector_schema(space),
                             index_params=vector_index_params())
    milvus.create_partition(collection_name=collection,
                            partition_name=partition)
    rows = dt_gold["rows"]
    plan_by_row = dt_gold["plan_by_row"]
    operations: list[dict] = []
    for row_key, plan_row in plan_by_row.items():
        row = rows[row_key]
        doc = {
            "scope_id": _vector_scope(plan_row["scope_id"]),   # 补丁⑫ 可信标量
            "request_id": "req-" + _digest("b6dt-req:" + row_key)[:16],
            "item_id": _item_id(row_key),
            "record_id": _record_id(row_key),
            "schema_version": "v1", "pipeline_version": "dedup_v1",
            "embedding_space_id": space.space_id,   # 真空间身份（B4 为占位串）
            "business_date": BUSINESS_DATE,
            "arrival_seq": plan_row["arrival_seq"],
            "received_at": SEEDED_AT, "accepted_at": SEEDED_AT,
            "expires_at": EXPIRY_REPLAY,
            "text": row.raw_text, "raw_hash": row.raw_hash,
            "task_state": "accepted", "delivery_state": "not_ready",
            "result": None,
            "diagnostics": {"delivery": {
                "route_ref": "gold-replay", "trace_id": None}},
        }
        operations.append({"create": {"_index": items_index,
                                      "_id": doc["record_id"]}})
        operations.append(doc)
    result = es.bulk(operations=operations, refresh="wait_for")
    assert not result["errors"], (
        f"物化写入存在失败：{[i for i in result['items'] if 'error' in i.get('create', {})][:3]}")
    es.create(index=control_index, id=HEAD_ID, refresh="wait_for",
              document={
                  "kind": "admission", "owner_id": f"b6dt-{run}",
                  "last_allocated_seq": len(plan_by_row),
                  "last_materialized_seq": len(plan_by_row),
                  "checkpoint": {},
                  "pending": {"version": LOG_VERSION, "batches": []},
                  "updated_at": SEEDED_AT,
              })
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
    store_vec = MilvusVectorStore(milvus, es, collection, prefix, space)
    searcher = build_real_vector_searcher(
        client=emb_client, space=space, store=store_vec)
    print(f"[B6-DT] materialized {len(plan_by_row)} docs into {items_index}")
    print(f"[B6-DT] collection={collection} space={space.space_id}")
    _register_objects([
        {"kind": "es_index", "name": name, "run": run,
         "created_by": "tests/integration/test_b6_dual_track_replay.py",
         "born": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        for name in (items_index, control_index)
    ] + [{"kind": "milvus_collection", "name": collection, "run": run,
          "created_by": "tests/integration/test_b6_dual_track_replay.py",
          "born": datetime.now(timezone.utc).isoformat(timespec="seconds")}])
    yield {"run": run, "prefix": prefix, "items_index": items_index,
           "control_index": control_index, "space": space,
           "collection": collection, "emb_client": emb_client,
           "tokenizer": tokenizer, "store_vec": store_vec, "searcher": searcher,
           "es": es, "milvus": milvus}
    print(f"[B6-DT seal] R191 删除冻结：{items_index}/{control_index}/"
          f"{collection} 原地封存（不删，已登记白名单补充档）")


# ---------- 双轨对照主体 ----------

def test_dual_track_replay(dt_cluster, dt_gold, dt_env):
    from test_p23_uat import (
        EXPECTED_DATASET_VERSION,
        _ctx_mapping,
        _item_id,
        _load_norm_dictionary,
        _record_id,
    )

    from news_flash_dedup.commit.coordinator import CommitContext, commit_one
    from news_flash_dedup.commit.fake_store import FakeCommitStore
    from news_flash_dedup.recall.models import RecallRequest
    from news_flash_dedup.recall.prepare import (
        ElasticsearchWatermarkProvider,
        PrepareWorker,
    )
    from news_flash_dedup.recall.service import RecallService
    from news_flash_dedup.recall.vector_store import prepare_vector_row
    from news_flash_dedup.recall.worker import shadow_decide
    from news_flash_dedup.vector.qwen_bpe import real_chunk_text

    es = dt_env["es"]
    rows = dt_gold["rows"]
    clear = dt_gold["clear"]
    plan_by_row = dt_gold["plan_by_row"]
    prefix = dt_env["prefix"]
    space = dt_env["space"]
    run = dt_env["run"]
    clock = lambda: datetime.now(timezone.utc)
    norm_dictionary, norm_dictionary_version = _load_norm_dictionary()
    usage_0 = dt_env["emb_client"].usage_snapshot()

    # ---------- B4 (b)-null 工件只读装载 ----------
    assert _B4_BUNDLE.exists() and _B4_METRICS.exists(), (
        f"B4 (b)-null 轨工件缺失（只读依赖）：{_B4_BUNDLE}")
    b4_bundle = json.loads(_B4_BUNDLE.read_text(encoding="utf-8"))
    b4_metrics = json.loads(_B4_METRICS.read_text(encoding="utf-8"))
    assert b4_bundle["run_id"] == B4_RUN
    bnull_by_record = {}
    bnull_plan_by_record = {}
    for item in b4_bundle["items"]:
        rid = item["shadow_decision"]["record_id"]
        bnull_by_record[rid] = item["shadow_decision"]["outcome"]
        bnull_plan_by_record[rid] = item["plan"]
    print(f"[B6-DT] B4 (b)-null 工件装载：items={len(bnull_by_record)}")

    # ---------- 准备（F2 真驱动，双 scope） ----------
    scopes = sorted({_vector_scope(entry.scope_id) for entry in clear})  # 补丁⑫
    prepare_worker = PrepareWorker(es, prefix, clock=clock)
    provider = ElasticsearchWatermarkProvider(es, prefix)
    watermarks: dict[str, tuple[int, int]] = {}
    for scope_id in scopes:
        report = prepare_worker.prepare(scope_id, BUSINESS_DATE)
        assert report.conflicts == (), report.conflicts
        watermarks[scope_id] = (provider.visible_seq(scope_id, BUSINESS_DATE),
                                provider.prepared_seq(scope_id, BUSINESS_DATE))
        print(f"[B6-DT] prepare {scope_id}: prepared={len(report.prepared)} "
              f"watermarks={watermarks[scope_id]}")

    # ---------- 向量播种（真分块/真嵌入/闸扩展 store；B5 持久缓存复用） ----------
    texts_by_record: dict[str, str] = {}
    for row_key, plan_row in plan_by_row.items():
        texts_by_record[_record_id(row_key)] = rows[row_key].raw_text
    unique_texts = sorted(set(texts_by_record.values()))
    vectors_by_text: dict[str, list[list[float]]] = {}
    for text in unique_texts:
        chunks = real_chunk_text(text, dt_env["tokenizer"])
        chunk_texts = [chunk.text for chunk in chunks]
        vectors_by_text[text] = [list(v) for v in
                                 dt_env["emb_client"].embed_documents(
                                     chunk_texts)]
    seeded = 0
    seeded_chunks = 0
    for row_key, plan_row in plan_by_row.items():
        record_id = _record_id(row_key)
        text = texts_by_record[record_id]
        chunks = real_chunk_text(text, dt_env["tokenizer"])
        vectors = vectors_by_text[text]
        assert len(chunks) == len(vectors)
        for chunk, vector in zip(chunks, vectors):
            row = prepare_vector_row(record_id,
                                     _vector_scope(plan_row["scope_id"]),  # 补丁⑫
                                     BUSINESS_DATE, plan_row["arrival_seq"],
                                     space, chunk.chunk_id, vector)
            outcome = dt_env["store_vec"].upsert(row)
            assert outcome == "created", (record_id, outcome)
            seeded_chunks += 1
        seeded += 1
    usage_1 = dt_env["emb_client"].usage_snapshot()
    usage_delta = {key: usage_1.get(key, 0) - usage_0.get(key, 0)
                   for key in ("total_tokens", "api_calls", "cache_hits",
                               "estimated_usage_events")}
    print(f"[B6-DT] vector seeded: records={seeded} chunks={seeded_chunks} "
          f"usage_delta={usage_delta}")

    # ---------- 查询集装配（与 B4 同构） ----------
    id_to_row: dict[str, tuple[str, int, str]] = {}
    for row_key, plan_row in plan_by_row.items():
        id_to_row[_record_id(row_key)] = (
            row_key, plan_row["arrival_seq"],
            _vector_scope(plan_row["scope_id"]))               # 补丁⑫
    currents: dict[str, dict] = {}
    for entry in clear:
        slot = currents.setdefault(entry.current_key, {
            "entry": entry, "g_i": set(),
            "scope_id": _vector_scope(entry.scope_id),         # 补丁⑫
        })
        if entry.gold_label == "重复":
            slot["g_i"].add(_record_id(entry.history_key))
    ordered_currents = sorted(
        currents.values(),
        key=lambda slot: (slot["scope_id"], slot["entry"].current_seq,
                          slot["entry"].current_key))
    q_plus = [slot for slot in ordered_currents if slot["g_i"]]
    print(f"[B6-DT] pairs={len(clear)} distinct_currents={len(ordered_currents)} "
          f"q_plus={len(q_plus)}")

    # ---------- (b)-real 轨：真五通道召回+影子决策 ----------
    service = RecallService(es, prefix, vector_searcher=dt_env["searcher"],
                            clock=clock)
    failures: list[dict] = []
    legality_violations: list[dict] = []
    fusion_violations: list[dict] = []
    per_current: dict[str, dict] = {}
    for slot in ordered_currents:
        entry = slot["entry"]
        row = rows[entry.current_key]
        record_id = _record_id(entry.current_key)
        visible, prepared = watermarks[slot["scope_id"]]
        request = RecallRequest(
            scope_id=slot["scope_id"], business_date=BUSINESS_DATE,
            record_id=record_id, item_id=_item_id(entry.current_key),
            arrival_seq=entry.current_seq, text=row.raw_text,
            visible_seq=visible, prepared_seq=prepared,
            embedding_space_id=space.space_id)
        current_doc = {
            "record_id": record_id, "item_id": _item_id(entry.current_key),
            "text": row.raw_text, "raw_hash": row.raw_hash,
            "scope_id": slot["scope_id"], "business_date": BUSINESS_DATE,
            "arrival_seq": entry.current_seq, "expires_at": EXPIRY_REPLAY,
        }
        try:
            plan = service.build_plan(request)
            current, candidates, coverage, _expires = \
                service.build_commit_inputs(plan, current_doc)
            outcome_real = shadow_decide(
                current, candidates, coverage_complete=coverage,
                visible_seq=visible, prepared_seq=prepared,
                dictionary=norm_dictionary,
                dictionary_version=norm_dictionary_version)
        except Exception as exc:                       # INV-4：失败单列
            failures.append({"current_key": entry.current_key, "leg": "B-real",
                             "error_type": type(exc).__name__,
                             "error": str(exc)[:200]})
            continue
        merged_ids = [f.candidate.record_id for f in plan.merged_top30]
        pair10_ids = [f.candidate.record_id for f in plan.pair_top10]
        protected_ids = [f.candidate.record_id for f in plan.hash_protected]
        incremental_ids = [f.candidate.record_id
                           for f in plan.incremental_required]
        required_ids = [f.candidate.record_id for f in plan.required]
        if not (len(merged_ids) <= 30 and len(pair10_ids) <= 10
                and len(set(required_ids)) == len(required_ids)
                and set(pair10_ids) <= set(merged_ids)
                and set(protected_ids) <= set(merged_ids)):
            fusion_violations.append({"record_id": record_id})
        for cand_id in set(merged_ids) | set(protected_ids) \
                | set(incremental_ids):
            row_key_c, seq_c, scope_c = id_to_row[cand_id]
            if (scope_c != slot["scope_id"] or seq_c >= entry.current_seq
                    or cand_id == record_id):
                legality_violations.append({
                    "current": record_id, "candidate": cand_id,
                    "candidate_scope": scope_c, "candidate_seq": seq_c})
        compared = {pair.history_record_id
                    for pair in outcome_real.pair_results}
        e_i = sorted(set(required_ids) & compared)
        r_i = sorted(set(merged_ids) | set(protected_ids)
                     | set(incremental_ids))
        audit_emb = next(a for a in plan.channel_audits
                         if a.channel == "embedding")
        per_current[entry.current_key] = {
            "outcome_real": outcome_real, "g_i": slot["g_i"],
            "h_i_count": sum(
                1 for key, plan_row in plan_by_row.items()
                if _vector_scope(plan_row["scope_id"]) == slot["scope_id"]
                and plan_row["arrival_seq"] < entry.current_seq),
            "c_i_30": merged_ids, "c_i_10": pair10_ids,
            "r_i": r_i, "v_i": required_ids, "e_i": e_i,
            "gaps": [{"channel": g.channel, "reason": g.reason}
                     for g in plan.recall_gaps],
            "embedding_audit": {"status": audit_emb.status,
                                "candidate_count": audit_emb.candidate_count,
                                "coverage_complete": audit_emb.coverage_complete},
            # ④ 配套：merged_top30 键候选评估原料（本件证据面自带，不改 worker）
            "merged_top30_ids": merged_ids,
        }
    print(f"[B6-DT] (b)-real: plans={len(per_current)} "
          f"failures={len(failures)} legality={len(legality_violations)} "
          f"fusion={len(fusion_violations)}")

    # ---------- (a) 轨=腿 A：固定候选决策（与 B4 逐行同构） ----------
    store_a = FakeCommitStore()
    watermark_a: dict[str, int] = {}
    leg_a: dict[str, dict] = {}
    for entry in sorted(clear, key=lambda e: (e.scope_id, e.current_seq,
                                              e.current_key)):
        key = f"{entry.scope_id}|{entry.business_date}"
        watermark_a[key] = watermark_a.get(key, 0) + 1
        history = _ctx_mapping(entry.history_key, rows[entry.history_key],
                               entry.history_seq, entry.scope_id,
                               entry.business_date)
        current_a = _ctx_mapping(entry.current_key, rows[entry.current_key],
                                 entry.current_seq, entry.scope_id,
                                 entry.business_date)
        try:
            ctx = CommitContext(
                scope_id=entry.scope_id, business_date=entry.business_date,
                arrival_seq=entry.current_seq, current=current_a,
                candidates=(history,), visible_seq=entry.current_seq,
                prepared_seq=entry.current_seq, pipeline_version="dedup_v1",
                coverage_complete=True)
            outcome = commit_one(ctx, store_a,
                                 decision_watermark_seq=watermark_a[key],
                                 audit_complete=True,
                                 dictionary=norm_dictionary,
                                 dictionary_version=norm_dictionary_version)
            if outcome.state != "committed" or outcome.decide_outcome is None:
                raise RuntimeError(f"commit_one 未提交：{outcome.state!r}")
            leg_a[entry.pair_id] = {
                "public": outcome.decide_outcome.to_public_dict(),
                "internal_code": outcome.decide_outcome.internal_code,
            }
        except Exception as exc:
            failures.append({"pair_id": entry.pair_id, "leg": "A",
                             "error_type": type(exc).__name__,
                             "error": str(exc)[:200]})
    print(f"[B6-DT] leg A: pairs={len(leg_a)} failures={len(failures)}")

    # ---------- 三方对拍：五字段一致率+签发码 ----------
    fields = ("decision", "duplicate_ids", "item_id", "text", "reason")
    agree_a_real = {name: 0 for name in (*fields, "internal_code")}
    agree_a_null = {name: 0 for name in ("reason", "internal_code")}
    compared_pairs = 0
    divergent_77: list[dict] = []
    fp_violations: list[dict] = []
    for entry in clear:
        pair_a = leg_a.get(entry.pair_id)
        data = per_current.get(entry.current_key)
        record_id = _record_id(entry.current_key)
        bnull = bnull_by_record.get(record_id)
        if pair_a is None or data is None or bnull is None:
            continue
        public_real = data["outcome_real"].to_public_dict()
        compared_pairs += 1
        # fp=0（金标"不重复"对任一腿签发重复即红；duplicate_ids 为 item_id 域）
        history_item = _item_id(entry.history_key)
        if entry.gold_label == "不重复":
            if history_item in pair_a["public"]["duplicate_ids"]:
                fp_violations.append({"pair": entry.pair_id, "leg": "A"})
            if history_item in public_real["duplicate_ids"]:
                fp_violations.append({"pair": entry.pair_id, "leg": "B-real"})
        for name in fields:
            if pair_a["public"][name] == public_real[name]:
                agree_a_real[name] += 1
        if pair_a["internal_code"] == data["outcome_real"].internal_code:
            agree_a_real["internal_code"] += 1
        # 复算 B4 (a) vs (b)-null 分歧（勘误形态自证：须恰 77 对）
        reason_diff = pair_a["public"]["reason"] != bnull["reason"]
        code_diff = pair_a["internal_code"] != bnull["internal_code"]
        if not reason_diff:
            agree_a_null["reason"] += 1
        if not code_diff:
            agree_a_null["internal_code"] += 1
        if reason_diff or code_diff:
            divergent_77.append({
                "pair_id": entry.pair_id,
                "current": record_id,
                "gold_label": entry.gold_label,
                "a": {"decision": pair_a["public"]["decision"],
                      "internal_code": pair_a["internal_code"],
                      "reason": pair_a["public"]["reason"]},
                "b_null": {"decision": bnull["decision"],
                           "internal_code": bnull["internal_code"],
                           "reason": bnull["reason"]},
                "b_real": {"decision": public_real["decision"],
                           "internal_code": data["outcome_real"].internal_code,
                           "reason": public_real["reason"],
                           "duplicate_ids": public_real["duplicate_ids"]},
                "candidate_sets": {
                    "b_null_required": sorted(
                        c["record_id"]
                        for c in bnull_plan_by_record[record_id]["required"]),
                    "b_real_required": sorted(data["v_i"]),
                },
            })
    assert compared_pairs == len(clear)

    # ---------- 候选集差异登记（(b)-real vs (b)-null 逐 current） ----------
    candidate_delta = []
    embedding_contributions = 0
    for slot in ordered_currents:
        data = per_current.get(slot["entry"].current_key)
        if data is None:
            continue
        record_id = _record_id(slot["entry"].current_key)
        bnull_plan = bnull_plan_by_record.get(record_id)
        if bnull_plan is None:
            continue
        null_required = {c["record_id"] for c in bnull_plan["required"]}
        real_required = set(data["v_i"])
        added = sorted(real_required - null_required)
        dropped = sorted(null_required - real_required)
        if added or dropped:
            candidate_delta.append({
                "current": record_id, "added": added, "dropped": dropped,
                "null_pair_top10": list(bnull_plan["pair_top10"]),
                "real_pair_top10": list(data["c_i_10"])})
        emb_new = [c for c in data["v_i"] if c not in null_required]
        embedding_contributions += len(emb_new)

    # ---------- 12 §6.2 指标（(b)-real 轨，q_plus 限定） ----------
    recall_sets = {name: [] for name in
                   ("h_i", "g_i", "c_i_30", "c_i_10", "r_i", "v_i", "e_i")}
    hits = {name: 0 for name in ("c30", "c10", "r", "v", "e")}
    gold_covered = 0
    gold_total = 0
    for slot in q_plus:
        data = per_current.get(slot["entry"].current_key)
        if data is None:
            continue
        g_i = data["g_i"]
        recall_sets["h_i"].append(data["h_i_count"])
        recall_sets["g_i"].append(len(g_i))
        recall_sets["c_i_30"].append(len(data["c_i_30"]))
        recall_sets["c_i_10"].append(len(data["c_i_10"]))
        recall_sets["r_i"].append(len(data["r_i"]))
        recall_sets["v_i"].append(len(data["v_i"]))
        recall_sets["e_i"].append(len(data["e_i"]))
        gold_total += len(g_i)
        gold_covered += len(g_i & set(data["c_i_30"]))
        if g_i & set(data["c_i_30"]):
            hits["c30"] += 1
        if g_i & set(data["c_i_10"]):
            hits["c10"] += 1
        if g_i & set(data["r_i"]):
            hits["r"] += 1
        if g_i & set(data["v_i"]):
            hits["v"] += 1
        if g_i & set(data["e_i"]):
            hits["e"] += 1
    n_plus = len(q_plus)
    recall_metrics = {
        "recall_at_30": hits["c30"] / n_plus if n_plus else None,
        "pair_coverage_at_30": (gold_covered / gold_total
                                if gold_total else None),
        "recall_at_10": hits["c10"] / n_plus if n_plus else None,
        "retention_30_to_10": (hits["c10"] / hits["c30"]
                               if hits["c30"] else None),
        "extended_candidate_coverage_r": (hits["r"] / n_plus
                                          if n_plus else None),
        "extended_plan_coverage_v": hits["v"] / n_plus if n_plus else None,
        "effective_comparison_coverage_e": (hits["e"] / n_plus
                                            if n_plus else None),
        "denominators": {"q_plus": n_plus, "gold_positives": gold_total,
                         "recall30_hits": hits["c30"]},
        "percentile_method": "nearest-rank",
    }
    per_query_counts = {name: _count_stats(values)
                        for name, values in recall_sets.items()}

    # ---------- 证据落盘（④ merged_top30 复算原料全量随行） ----------
    evidence = {
        "kind": "b6_dual_track_replay", "run_id": run,
        "dataset_version": EXPECTED_DATASET_VERSION,
        "space_id": space.space_id, "collection": dt_env["collection"],
        "b4_null_run": B4_RUN,
        "scope_form": ("补丁⑫ 可信标量适配：synthetic:<hex16>→synthetic-<hex16>"
                       "（vector_store.py:76 词表；双射改名，分区结构/逐对语义"
                       "/B4 对拍口径不变）"),
        "embedding_usage_delta": usage_delta,
        "counts": {
            "rows": len(plan_by_row), "pairs_clear": len(clear),
            "distinct_currents": len(ordered_currents), "q_plus": n_plus,
            "replayed_currents": len(per_current),
            "replay_failures": len(failures),
            "seeded_chunks": seeded_chunks,
        },
        "recall_at_30_10_b_real": recall_metrics,
        "recall_at_30_10_b_null_b4": b4_metrics["recall_at_30_10"],
        "per_query_counts_b_real": per_query_counts,
        "leg_agreement_a_vs_b_real": {
            "compared_pairs": compared_pairs,
            "per_field_equal": agree_a_real,
            "note": ("腿 A=(a) 固定候选+coverage 显式 True 占位；(b)-real="
                     "真召回（真 embedding 编入）+派生 coverage=False——"
                     "decision/duplicate_ids 为语义可比面；reason/internal_code"
                     " 分歧根因口径=B4′ F-2（候选集差异）"),
        },
        "leg_agreement_a_vs_b_null_recomputed": {
            "compared_pairs": compared_pairs,
            "reason_equal": agree_a_null["reason"],
            "internal_code_equal": agree_a_null["internal_code"],
            "b4_reported": b4_metrics["leg_agreement"]["per_field_equal"],
        },
        "divergent_corrected_shape": {
            "count": len(divergent_77),
            "root_cause": "B4′ F-2 勘误：候选集差异（非 coverage 占位词面）",
            "pairs": divergent_77,
        },
        "candidate_set_delta": {
            "currents_with_delta": len(candidate_delta),
            "embedding_new_required_total": embedding_contributions,
            "entries": candidate_delta,
        },
        "fp_violations": fp_violations,
        "merged_top30_by_current": {
            _record_id(slot["entry"].current_key): per_current[
                slot["entry"].current_key]["merged_top30_ids"]
            for slot in ordered_currents
            if slot["entry"].current_key in per_current},
        "fusion_invariant_violations": fusion_violations,
        "candidate_legality_violations": legality_violations,
        "failures": failures,
    }
    out = _LOG_TEMP / f"b6-e3-dual-track-{run}.json"
    out.write_text(json.dumps(evidence, ensure_ascii=False, indent=2,
                              default=str), encoding="utf-8")
    print(f"[B6-DT] evidence -> {out}")
    print(f"[B6-DT] Recall@30 (b)-real={recall_metrics['recall_at_30']} vs "
          f"(b)-null(B4)={b4_metrics['recall_at_30_10']['recall_at_30']}")
    print(f"[B6-DT] (a) vs (b)-real 五字段一致率={agree_a_real}；"
          f"77 例复算={len(divergent_77)}；候选集差异 currents="
          f"{len(candidate_delta)}；fp={len(fp_violations)}")

    # ---------- 诚实闸 ----------
    assert not failures, f"回放存在失败（INV-4 单列）：{failures[:3]}"
    assert not legality_violations, (
        f"候选合法性违规：{legality_violations[:3]}")
    assert not fusion_violations, f"融合不变量违规：{fusion_violations[:3]}"
    assert not fp_violations, f"fp=0 纪律被破：{fp_violations[:3]}"
    assert n_plus > 0, "q_plus 分母为零——金标形态异常"
    # 勘误形态自证：(a) vs (b)-null 复算须恰 77 对（B4′ F-2 在案数）
    assert len(divergent_77) == 77, (
        f"(a) vs (b)-null 分歧复算={len(divergent_77)}，与 B4′ F-2 在案 77 不符"
        "——工件装载或对拍口径异常，硬停呈查")
    # (b)-real 语义面不劣化：decision/duplicate_ids 一致率 ≥ (b)-null 轨
    b4_decision_equal = b4_metrics["leg_agreement"]["per_field_equal"]["decision"]
    b4_dup_equal = b4_metrics["leg_agreement"]["per_field_equal"]["duplicate_ids"]
    assert agree_a_real["decision"] >= b4_decision_equal, (
        f"(b)-real decision 一致率 {agree_a_real['decision']} < "
        f"(b)-null {b4_decision_equal}——真 embedding 编入引入语义劣化，硬停呈查")
    assert agree_a_real["duplicate_ids"] >= b4_dup_equal, (
        f"(b)-real duplicate_ids 一致率 {agree_a_real['duplicate_ids']} < "
        f"(b)-null {b4_dup_equal}——真 embedding 编入引入语义劣化，硬停呈查")
    # 真 embedding 通道全程编入：逐 current embedding 审计 status=complete
    assert all(data["embedding_audit"]["status"] == "complete"
               for data in per_current.values()), (
        "存在 embedding 通道非 complete 的 current——真通道未全程编入")
