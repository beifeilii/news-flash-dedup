r"""B6/E3 F6：N20 跨模块输入契约真集群 UAT（B4 移交件，D1 过闸稿 §三-F6/§五-T4）。

闸（§五-T4）：`P08_CONFIRM_UAT=1`（ES 通道，test_recall_uat.py:23-25 同型）
+`P12_CONFIRM_UAT=1`（向量，test_vector_p12_uat.py:28-30 同型）+`DEPLOY_ENV=test`
+`TEST_ES_*`/`TEST_MILVUS_*` 就位+无 `PROD_*` 泄漏。

标尺（D1 §三-F6 + B6 纪律）：
- N20-a：真 ES 四通道（P10/P11 适配器：Hash/Near/BM25/Entity）+真 Milvus
  （P12 适配器 MilvusVectorStore mock 空间，test_vector_p12_uat.py 同形）真跑产
  ChannelResult 直接喂 freeze_recall_plan：融合接受真工件（各通道
  query_version 一致、candidate_count≤CHANNEL_LIMITS（fusion.py:15/:144-145）、
  status 枚举封闭（models.py:10 经 fusion.py:141-143）、跨通道同记录身份一致
  （fusion.py:178-180 区，生存即证））。
- N20-b：①P12 mock 空间 embedding 实迹 coverage 形态——store 恒
  coverage_complete=False（vector_store.py:325）+prepared_seq 直传（:327-329），
  融合机械派生 gap reason=COVERAGE_UNPROVEN（fusion.py:111-112 实测码面；
  D1 §三-F6 括注"VECTOR_FRONTIER_UNPROVEN"（fusion.py:123-126）在
  coverage_complete=False 恒真下结构性不可达——如实登记，不硬断言 D1 词面）；
  ②NullVectorSearcher 装配态 recall_gaps 含 embedding/SPACE_UNCONFIRMED 且
  recall_incomplete=True，plan_artifact 工件不省略缺口（09 §5.7 L386）；
  ③多记录小语料（4 条≥3）跨通道同 ID 去重、near 双子路径按一路计
  （P13 设计 L5/L9）。
- R191 删除冻结：全程零删除零清场；本 run 对象（ES 日索引+Milvus 集合）原地
  封存，只读登记 log\temp\b6-created-objects.json 呈主窗口补登白名单。
- 保留索引/保留集合双快照 diff 零触碰硬停；工件 JSON 落盘
  log\temp\b6-e3-n20-<run>.json。

跑站命令（log\temp\ 落日志；<HHMM>=footer 分钟级）：

    $env:PYTHONPATH='src'
    $env:DEPLOY_ENV='test'
    $env:P08_CONFIRM_UAT='1'
    $env:P12_CONFIRM_UAT='1'
    & '.venv-v1\\Scripts\\python.exe' -m pytest -p no:cacheprovider `
        --basetemp '.\\tmp\\b6-n20-run' -v tests/integration/test_recall_n20_contract.py `
        > ..\\log\\temp\\b6-e3-n20-run-<HHMM>.txt
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

import pytest

P08_ENV = "P08_CONFIRM_UAT"
P12_ENV = "P12_CONFIRM_UAT"

_REPO_ROOT = Path(__file__).resolve().parents[2]            # news-flash-dedup
_LOG_TEMP = _REPO_ROOT / "log" / "temp"
_CREATED_OBJECTS = _LOG_TEMP / "b6-created-objects.json"


def _gate_open() -> bool:
    env = os.environ
    return (env.get(P08_ENV) == "1" and env.get(P12_ENV) == "1"
            and bool(env.get("TEST_ES_HOST")) and bool(env.get("TEST_MILVUS_HOST"))
            and not any(k.startswith("PROD_ES_") or k.startswith("PROD_MILVUS_")
                        for k in env))


pytestmark = pytest.mark.skipif(
    not _gate_open(),
    reason=f"N20 跨模块契约 UAT 闸未开（需 {P08_ENV}=1 + {P12_ENV}=1 + "
           "TEST_ES_*/TEST_MILVUS_*，无 PROD_*）",
)


# ---------- 公共助手（B6 自限：快照/登记/封存） ----------

def _es_snapshot(client) -> dict[str, int]:
    rows = client.cat.indices(format="json", h="index,docs.count")
    return {item["index"]: int(item.get("docs.count") or 0)
            for item in rows if not item.get("index", "").startswith(".")}


def _is_dedup_family(name: str) -> bool:
    return name.startswith(("p01-batch-", "p17-batch-", "p18-batch-",
                            "p19-batch-", "p20-batch-", "news-dedup"))


def _register_objects(entries: list[dict]) -> None:
    """R191 只读登记（零删除零清场）：本 run 新对象入 b6-created-objects.json，
    呈主窗口补登 可删除白名单-本项目创建物登记.md §一（现役对象）。"""
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


# ---------- fixtures ----------

@pytest.fixture(scope="module")
def n20_cluster():
    """双客户端+双快照（R191 版）：跑后 diff=新 run 对象之外零变化；封存物
    missing 即硬停；本 run 对象原地封存只读登记（不删）。"""
    from news_flash_dedup.es_client import (
        ProductionAccessDenied,
        assert_test_environment,
        client_from_environment,
        load_environment,
    )
    from news_flash_dedup.milvus_client import (
        assert_test_milvus_environment,
        build_test_milvus_client,
    )

    load_environment()
    try:
        assert_test_environment()
        assert_test_milvus_environment()
    except ProductionAccessDenied as error:  # pragma: no cover - 闸外事故
        pytest.fail(f"UAT environment check failed: {error}")
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
    print(f"[B6-N20] baseline indices={len(idx_before)} "
          f"collections={len(cols_before)}")
    yield {"es": es, "milvus": milvus, "idx_before": idx_before,
           "cols_before": cols_before}
    idx_after = _es_snapshot(es)
    cols_after = sorted(milvus.list_collections())
    added_i = sorted(set(idx_after) - set(idx_before))
    missing_i = sorted(set(idx_before) - set(idx_after))
    drifted = sorted(n for n in set(idx_before) & set(idx_after)
                     if _is_dedup_family(n) and idx_before[n] != idx_after[n])
    own_i = [n for n in added_i if n.startswith("p01-batch-n20-")]
    dedup_foreign_i = sorted(n for n in added_i
                             if _is_dedup_family(n) and n not in own_i)
    external_i = sorted(set(added_i) - set(own_i) - set(dedup_foreign_i))
    added_c = sorted(set(cols_after) - set(cols_before))
    missing_c = sorted(set(cols_before) - set(cols_after))
    own_c = [c for c in added_c if c.startswith("news_dedup_replay_n20b6")]
    dedup_foreign_c = sorted(c for c in added_c
                             if c.startswith(("news_dedup_replay_", "p19_"))
                             and c not in own_c)
    external_c = sorted(set(added_c) - set(own_c) - set(dedup_foreign_c))
    mabc_after = {name: milvus.get_collection_stats(collection_name=name)
                  for name in mabc_before}
    mabc_drift = sorted(name for name in mabc_before
                        if mabc_after.get(name) != mabc_before[name])
    print(f"[B6-N20] snapshot diff: added_i={added_i} missing_i={missing_i} "
          f"drifted_preserved={drifted}")
    print(f"[B6-N20] external tenant drift (report only): "
          f"i={external_i} c={external_c}")
    print(f"[B6-N20] collections added={added_c} missing={missing_c} "
          f"mabc_drift={mabc_drift}")
    assert not missing_i and not missing_c, (
        f"保留对象被删改（R191 硬停）：i={missing_i} c={missing_c}")
    assert not drifted, f"dedup 保留族文档数被触碰（硬停）：{drifted}"
    assert not mabc_drift, f"mabc 集合被触碰（硬停）：{mabc_drift}"
    assert not dedup_foreign_i and not dedup_foreign_c, (
        f"白名单外新增 dedup 族对象（越界硬停）："
        f"i={dedup_foreign_i} c={dedup_foreign_c}")
    milvus.close()
    es.close()


@pytest.fixture(scope="module")
def n20_env(n20_cluster):
    """隔离环境：p01-batch-n20-<run>- 日索引 + news_dedup_replay_<run>_s_<mock>
    集合（replay 形态，闸扩展二态闭集合法形）+4 条语料向量。"""
    from news_flash_dedup.admission import BUSINESS_ZONE
    from news_flash_dedup.es_admission_schema import day_index, item_mapping
    from news_flash_dedup.milvus_client import (
        collection_name_for_test,
        partition_name,
    )
    from news_flash_dedup.recall.entities import extract_body_entities
    from news_flash_dedup.recall.hash_channel import prepare_recall_fields
    from news_flash_dedup.recall.vector_space import EmbeddingSpace
    from news_flash_dedup.recall.vector_store import (
        MilvusVectorStore,
        prepare_vector_row,
        vector_index_params,
        vector_schema,
    )

    es = n20_cluster["es"]
    milvus = n20_cluster["milvus"]
    # W-R3b 秒级→uuid4：p19 集合名 [a-z0-9]{6,16} 长度墙（"n20b6"+%f=17
    # 越界），uuid4 hex[:8]=13 字符合法且跨进程唯一（清单 w-r3b-runuuid-inventory）。
    run = "n20b6" + uuid.uuid4().hex[:8]
    day = datetime.now(BUSINESS_ZONE).date()
    prefix = f"p01-batch-n20-{run}-"
    index = prefix + day_index(day.isoformat())
    space = EmbeddingSpace(
        "mock_embedding", "uat_fixture_v1", "codepoint_v1", "none", "COSINE", 4,
        "plain_v1", "plain_v1", "tokens512_overlap64_v1", "p09_v1")
    collection = collection_name_for_test(run, space.space_id)
    partition = partition_name(day.isoformat())
    assert not bool(es.indices.exists(index=index))
    assert milvus.has_collection(collection_name=collection) is False
    es.indices.create(index=index, body=item_mapping())
    milvus.create_collection(collection_name=collection,
                             schema=vector_schema(space),
                             index_params=vector_index_params())
    milvus.create_partition(collection_name=collection,
                            partition_name=partition)
    # ---- 语料（4 条≥3，N20-b③）：r1/r2 同文（全通道命中对）、r3 近义、r4 无关
    expiry = datetime.combine(day + timedelta(days=7), time.min,
                              tzinfo=BUSINESS_ZONE).astimezone(timezone.utc)
    corpus = [
        ("a" * 64, "n20-i1", 1, "美国能源信息署公布原油库存增加。", [1.0, 0.0, 0.0, 0.0]),
        ("b" * 64, "n20-i2", 2, "美国能源信息署公布原油库存增加。", [1.0, 0.0, 0.0, 0.0]),
        ("c" * 64, "n20-i3", 3, "美国能源信息署（EIA）公布原油库存录得增加。",
         [0.99, 0.141067, 0.0, 0.0]),
        ("d" * 64, "n20-i4", 4, "某市发布新一轮新能源汽车购置补贴细则。",
         [0.0, 1.0, 0.0, 0.0]),
    ]
    for record_id, item_id, seq, text, _vec in corpus:
        source = {
            "scope_id": "default", "business_date": day.isoformat(),
            "record_id": record_id, "item_id": item_id, "arrival_seq": seq,
            "text": text, "entity_ids": list(
                extract_body_entities(text).entity_ids),
            "expires_at": expiry.isoformat(), "preparation_state": "ready",
            "embedding_space_id": space.space_id,
        }
        source.update(prepare_recall_fields(text, "default", day.isoformat()))
        es.index(index=index, id=record_id, document=source,
                 op_type="create", refresh=True)
    store = MilvusVectorStore(milvus, es, collection, prefix, space)
    upserts = []
    for record_id, _item_id, seq, _text, vec in corpus:
        row = prepare_vector_row(record_id, "default", day.isoformat(), seq,
                                 space, 0, vec)
        upserts.append((record_id[:4], store.upsert(row)))
    print(f"[B6-N20] isolated: index={index} collection={collection}")
    print(f"[B6-N20] vector upserts={upserts} partition={partition}")
    _register_objects([
        {"kind": "es_index", "name": index, "run": run,
         "created_by": "tests/integration/test_recall_n20_contract.py",
         "born": datetime.now(timezone.utc).isoformat(timespec="seconds")},
        {"kind": "milvus_collection", "name": collection, "run": run,
         "created_by": "tests/integration/test_recall_n20_contract.py",
         "born": datetime.now(timezone.utc).isoformat(timespec="seconds")},
    ])
    yield {"run": run, "day": day, "prefix": prefix, "index": index,
           "space": space, "collection": collection, "store": store,
           "corpus": corpus}
    print(f"[B6-N20 seal] R191 删除冻结：index={index} 与 "
          f"collection={collection} 原地封存（不删，已登记白名单补充档）")


# ---------- N20-a：真工件喂融合 ----------

def test_n20a_real_channel_artifacts_feed_fusion(n20_cluster, n20_env):
    from news_flash_dedup.recall.bm25_channel import BM25Channel
    from news_flash_dedup.recall.entity_channel import EntityChannel
    from news_flash_dedup.recall.fusion import (
        CHANNEL_LIMITS,
        MAIN_CHANNELS,
        freeze_recall_plan,
    )
    from news_flash_dedup.recall.hash_channel import HashChannel
    from news_flash_dedup.recall.models import RecallRequest
    from news_flash_dedup.recall.near_channel import NearChannel

    es = n20_cluster["es"]
    prefix = n20_env["prefix"]
    space = n20_env["space"]
    day = n20_env["day"]
    store = n20_env["store"]
    request = RecallRequest(
        "default", day.isoformat(), "e" * 64, "n20-cur", 5,
        "美国能源信息署公布原油库存增加。",
        visible_seq=4, prepared_seq=4, embedding_space_id=space.space_id)
    results = (
        HashChannel(es, prefix).search(request),
        NearChannel(es, prefix).search(request),
        BM25Channel(es, prefix).search(request),
        store.search(request, [1.0, 0.0, 0.0, 0.0]),
        EntityChannel(es, prefix).search(request),
    )
    # 通道名闭集 + status 枚举封闭 + candidate_count 上限（逐通道工件级）
    assert [r.channel for r in results] == list(
        ("hash", "near", "bm25", "embedding", "entity"))
    evidence = {"run": n20_env["run"], "channels": []}
    for result in results:
        assert result.status in ("complete", "truncated", "unavailable",
                                 "not_applicable"), result
        limit = CHANNEL_LIMITS.get(result.channel)
        if limit is not None:
            assert len(result.candidates) <= limit, (result.channel, limit)
        for cand in result.candidates:
            assert cand.query_version == result.query_version, (
                f"{result.channel} 候选 query_version 与通道工件不一致")
        evidence["channels"].append({
            "channel": result.channel, "status": result.status,
            "query_version": result.query_version,
            "candidate_count": len(result.candidates),
            "coverage_complete": result.coverage_complete,
            "prepared_seq": result.prepared_seq,
            "error_code": result.error_code,
            "candidates": [{"record_id": c.record_id[:4], "score": c.score,
                            "subpaths": list(c.subpaths)}
                           for c in result.candidates],
        })
    # 融合接受真工件（不抛即过闸①）
    plan = freeze_recall_plan(request, results)
    assert len(plan.channel_audits) == len(MAIN_CHANNELS) == 5
    # 跨通道同 ID 去重：r1/r2 同文对各自单条目+多通道命中
    merged = {f.candidate.record_id: f for f in plan.merged_top30}
    assert "a" * 64 in merged and "b" * 64 in merged
    hit_channels_r1 = {hit.channel for hit in merged["a" * 64].channel_hits}
    assert hit_channels_r1 >= {"hash", "near", "bm25", "embedding"}, (
        f"r1 跨通道命中集不足：{hit_channels_r1}")
    # near 双子路径按一路计（P13 设计 L5/L9）：同一候选 near 命中恰 1 条
    near_hits = [hit for hit in merged["a" * 64].channel_hits
                 if hit.channel == "near"]
    assert len(near_hits) == 1 and len(near_hits[0].subpaths) >= 1
    # embedding mock 空间实迹：cos≈1 自匹配命中（r1/r2），score 降序
    emb = results[3]
    assert emb.status == "complete"
    emb_ids = [c.record_id for c in emb.candidates]
    assert "a" * 64 in emb_ids and "b" * 64 in emb_ids
    assert emb.candidates[0].score == pytest.approx(1.0, abs=1e-4)
    # 多记录小语料：跨通道去重后 merged 覆盖 ≥3 条不同记录（N20-b③）
    assert len(merged) >= 3
    evidence["plan"] = {
        "version": plan.version,
        "merged": {rid[:4]: sorted({h.channel for h in f.channel_hits})
                   for rid, f in merged.items()},
        "recall_gaps": [{"channel": g.channel, "reason": g.reason}
                        for g in plan.recall_gaps],
        "recall_incomplete": plan.recall_incomplete,
    }
    n20_env["evidence_a"] = evidence
    print(f"[B6-N20] N20-a 融合受納：5 通道工件齐、r1 命中={sorted(hit_channels_r1)}")


# ---------- N20-b：缺口明示两形态 + 工件不省略 ----------

def test_n20b_gap_explicitness_two_forms(n20_cluster, n20_env):
    from news_flash_dedup.recall.bm25_channel import BM25Channel
    from news_flash_dedup.recall.entity_channel import EntityChannel
    from news_flash_dedup.recall.fusion import freeze_recall_plan
    from news_flash_dedup.recall.hash_channel import HashChannel
    from news_flash_dedup.recall.models import RecallRequest
    from news_flash_dedup.recall.near_channel import NearChannel
    from news_flash_dedup.recall.service import NullVectorSearcher
    from news_flash_dedup.recall.worker import plan_artifact

    es = n20_cluster["es"]
    prefix = n20_env["prefix"]
    space = n20_env["space"]
    day = n20_env["day"]
    store = n20_env["store"]
    request = RecallRequest(
        "default", day.isoformat(), "e" * 64, "n20-cur", 5,
        "美国能源信息署公布原油库存增加。",
        visible_seq=4, prepared_seq=4, embedding_space_id=space.space_id)
    es_results = (
        HashChannel(es, prefix).search(request),
        NearChannel(es, prefix).search(request),
        BM25Channel(es, prefix).search(request),
        EntityChannel(es, prefix).search(request),
    )
    # 形态①：mock 空间 embedding 实迹——coverage_complete 恒 False +
    # prepared_seq 直传（vector_store.py:325/:327-329）
    emb = store.search(request, [1.0, 0.0, 0.0, 0.0])
    assert emb.coverage_complete is False
    assert emb.prepared_seq == 4           # prepared_seq 直传（None 不捏造）
    plan_real = freeze_recall_plan(
        request, (es_results[0], es_results[1], es_results[2], emb, es_results[3]))
    gaps_real = {(g.channel, g.reason) for g in plan_real.recall_gaps}
    # 机械形态（fusion.py:111-112 实测码面）：coverage 恒 False →
    # COVERAGE_UNPROVEN。D1 §三-F6 括注 VECTOR_FRONTIER_UNPROVEN
    # （fusion.py:123-126）须 coverage_complete=True 方可达——mock 空间下
    # 结构性不可达，如实登记（呈 B6′ 勘误候选，不硬断言 D1 词面）。
    assert ("embedding", "COVERAGE_UNPROVEN") in gaps_real, gaps_real
    assert plan_real.recall_incomplete is True
    # 形态②：NullVectorSearcher 装配态（N20-b 缺口明示）
    null_result = NullVectorSearcher().search(request, None)
    assert null_result.error_code == "SPACE_UNCONFIRMED"
    plan_null = freeze_recall_plan(
        request, (es_results[0], es_results[1], es_results[2], null_result,
                  es_results[3]))
    gaps_null = {(g.channel, g.reason) for g in plan_null.recall_gaps}
    assert ("embedding", "SPACE_UNCONFIRMED") in gaps_null, gaps_null
    assert plan_null.recall_incomplete is True
    # 工件不省略缺口（09 §5.7 L386）：plan_artifact 全形带 gaps
    artifact = plan_artifact(plan_null, request)
    assert any(g["channel"] == "embedding" and g["reason"] == "SPACE_UNCONFIRMED"
               for g in artifact["recall_gaps"])
    assert artifact["recall_incomplete"] is True
    evidence = {
        "run": n20_env["run"],
        "form_real_mock_space": {
            "embedding_coverage_complete": False,
            "embedding_prepared_seq_passthrough": 4,
            "gap": ["embedding", "COVERAGE_UNPROVEN"],
            "d1_wording_registration": (
                "D1 §三-F6 括注 VECTOR_FRONTIER_UNPROVEN 在 store 恒 "
                "coverage_complete=False（vector_store.py:325）下结构性不可达"
                "（fusion.py:111-112 先序派生 COVERAGE_UNPROVEN）；机械形态已钉，"
                "词面差异呈 B6′ 勘误候选"),
        },
        "form_null_assembly": {
            "gap": ["embedding", "SPACE_UNCONFIRMED"],
            "recall_incomplete": True,
            "artifact_gaps_not_omitted": True,
        },
    }
    n20_env["evidence_b"] = evidence
    print("[B6-N20] N20-b 两形态：real=COVERAGE_UNPROVEN（恒 False 派生）/"
          "null=SPACE_UNCONFIRMED；工件缺口不省略")
    # ---- 工件落盘（A+B 合并） ----
    out = _LOG_TEMP / f"b6-e3-n20-{n20_env['run']}.json"
    payload = {"n20a": n20_env.get("evidence_a"), "n20b": evidence}
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    print(f"[B6-N20] evidence -> {out}")
