"""B5/E2 S5 真轨 UAT + shadow 双腿对拍验收（设计 §4.3/§7.3；v4 R164）。

**真轨 UAT 串行纪律**：本件真跑须待主窗口放行令（与 B4 UAT 腿排队，避免并发）；
缺闸整组 skip（单元/CI 安全）。双闸叠加（§7.3）：
- P19 闸：`P19_CONFIRM_UAT=1` + TEST_MILVUS_*/TEST_ES_* 就位 + 无 PROD_* 泄漏；
- 真 embedding 闸：`EMB_CONFIRM_REAL=1` + key 可解析
  （DEDUP_EMBEDDING_API_KEY 或 DEDUP_EMBEDDING_KEY_FILE 现读，禁抄值）。

用例映射（§7.3 真轨 1-6 + §4.3 四验收闸）：
1. 真 space(1024) 建档 + schema 对拍（B-0 出）；
2. 真 embed → upsert → Strong search 自匹配 COSINE≈1；
3. 金标抽样对（逐字 3/释义 6/硬负例 6）候选召回 + 硬负例登记不判 fp；
4. canary 漂移探针（N11 30 文同款选样 + 期望 cosine 容差带 ±0.01）+
   usage 校准对拍（本地 BPE 计数 vs 服务端 usage.total_tokens，§2.6）；
5. 断网/429 注入 → 写径 failed / 查询径 unavailable + EMBEDDING_QUERY_FAILED
   （失败语义真链演示；transport 注入零真 API）；
6. 半写恢复重放（嵌入缓存重放零 API + reused 幂等）；
7. shadow 双腿对拍（§4.3）：全金标语料真链建档 + 逐对查询 → 四验收闸：
   闸1 逐字重复对 recall@10 = 100%（恒等必召顶名）；
   闸2 释义重复对 recall@30 = 1.0000（263/263，N23 §1.2 锚，K=30 对齐
       rrf_v1_k60_30_10 计划；参照带 recall@1≈0.9962、名次 p50/p90/max=1/1/2）；
   闸3 fp=0（候选层不判重——本 harness 结构上零判重，硬负例候选占比仅登记）；
   闸4 孤儿/孔洞事件全登记（RECALL_INCOMPLETE 不掩盖）。
   腿 A（基线腿）= 现役固定候选轨（金标直供，代表「无真召回」现状）；
   腿 B（真机腿）= 真链 shadow 集合 + 查询编码 + 闸扩展后 MilvusVectorStore。
   方法论锚声明：闸 1/闸 2 在 embedding 通道记录级 top-k 执法（N23 §1.2 同款
   方法论——FLAT=COSINE 全语料暴力等值；五通道 RRF 融合双腿联调归 B6/E3，
   本片不伪造其余通道）。
   冷跑（真空 API）+ 复跑（全缓存零 API）各一次，两回指标一致（§4.3 纪律）。

隔离（公共标尺）：集合 `p19_<run>_<space_id>` + ES `p19-batch-<run>-`；
跑前跑后双快照严格 diff（94(193) 保留索引 + 2 保留集合零触碰、无 p19 stray）；
dry-run 先行清场（仅本 run 前缀）；证据 `log/temp/emb-shadow-<run>.json`。

预算：shadow 小闸 200k tok/日（§6.3；DEDUP_EMBEDDING_DAILY_TOKEN_BUDGET 可覆盖），
嵌入缓存持久域 repo tmp/emb-shadow-cache（复跑全缓存零成本；删目录即重置）。

跑站命令（主窗口放行后执行；footer 分钟级向下取整）：

    $env:PYTHONPATH='src'
    $env:PYTHONIOENCODING='utf-8'
    $env:PYTHONDONTWRITEBYTECODE='1'
    $env:DEPLOY_ENV='test'
    $env:P19_CONFIRM_UAT='1'
    $env:EMB_CONFIRM_REAL='1'
    $env:TEST_MILVUS_HOST=<去引号主机>; $env:TEST_MILVUS_PORT='19530'
    $env:TEST_MILVUS_USER=<去引号用户>; $env:TEST_MILVUS_PASS=<去引号密码>
    $env:TEST_ES_PASSWORD=<去引号密码>   # 同时导出 TEST_ES_PASS 同值
    # R190 凭据单源迁移：mabc_service\.env 脱钩 → workspace-dedup\.env.dedup
    # （PROD_* 空值=结构性保留禁用；导出口径见 log/temp/p19-s5-run-shadow-uat.ps1）
    $env:DEDUP_EMBEDDING_KEY_FILE='C:\\Users\\ASUS\\Desktop\\文本去重\\workspace-dedup\\.env.dedup'
    & '.venv-v1\\Scripts\\python.exe' -m pytest -p no:cacheprovider `
        --basetemp '.\\tmp\\emb-shadow-run' -v tests/integration/test_emb_shadow.py `
        > ..\\log\\temp\\emb-shadow-run-<HHMM>.txt
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from news_flash_dedup.es_client import (
    APPROVED_UAT_HOST,
    APPROVED_UAT_PORT,
    APPROVED_UAT_SCHEME,
)
from news_flash_dedup.recall import embedding_query as eq
from news_flash_dedup.recall.models import RecallRequest
from news_flash_dedup.recall.vector_store import VECTOR_FIELDS, MilvusVectorStore
from news_flash_dedup.vector import embedding_client as ec
from news_flash_dedup.vector import milvus_store as p19_ms
from news_flash_dedup.vector import pipeline as vp
from news_flash_dedup.vector import qwen_bpe as qb

# ---------- 闸门（双闸叠加 §7.3） ----------

P19_UAT_ENV = "P19_CONFIRM_UAT"
EMB_REAL_ENV = "EMB_CONFIRM_REAL"
SHADOW_BUDGET_ENV = "DEDUP_EMBEDDING_DAILY_TOKEN_BUDGET"
SHADOW_DAILY_BUDGET = 200_000          # §6.3 shadow 小闸（高沿 38,628 tok 的 ≈5.2×）

_REPO_ROOT = Path(__file__).resolve().parents[2]            # news-flash-dedup
_WORKSPACE_ROOT = Path(__file__).resolve().parents[3]       # workspace-dedup
_LOG_TEMP = _WORKSPACE_ROOT / "log" / "temp"
_SHADOW_CACHE_ROOT = _REPO_ROOT / "tmp" / "emb-shadow-cache"
_P23_MOD_PATH = _REPO_ROOT / "tests" / "integration" / "test_p23_uat.py"
_N11_JSON = _LOG_TEMP / "n11-embedding-admission.json"

BUSINESS_DATE_STR = date.today().isoformat()
CANARY_COSINE_BAND = 0.01              # canary 探针容差带（N11 实测锚 ±0.01）
# usage 校准容差带【主窗口评审钉死】：批级 |本地-服务端| ≤ 服务端 20%；
# 适用域 = 新闻域 ≥10 文批。三层口径：服务端 usage = 预算权威；本地
# BPE = 确定性估计器；usage 缺失时 R14 UTF-8 字节估计（更保守）。
# 依据 p19-s2-usage-oracle-calibration.json + p19-s2-qwen1-vs-qwen2.json 实测：
# 两公开 Qwen 制品（Qwen2-7B-Instruct tokenizer.json / Qwen-7B qwen.tiktoken）
# 同_vocab 同计数（151,643 条，30 文逐批同一），本地实现跨源自洽；服务端
# usage 微尺度存在确定性 0-quirk（'中国'/'approximately'/全角括号/纯空白/
# 纯小数 等报 0）且数字分组语义与公开制品不同——位级同一不可达（R15 收口
# 证据；两发现入终报风险节，R13 服务端行为漂移族实证素材）；批级偏差实测
# -18.7%/+1.4%/-15.7%（新闻域 30 文），过计主导（预算保守方向）。
# 512 本地 cap vs 8192 API 上限 = 16× 余量，方向不敏感。
USAGE_ORACLE_REL_BAND = 0.20


def _gate_open(env) -> bool:
    if env.get(P19_UAT_ENV) != "1" or env.get(EMB_REAL_ENV) != "1":
        return False
    if not env.get("TEST_MILVUS_HOST") or not env.get("TEST_ES_HOST"):
        return False
    if any(k.startswith("PROD_MILVUS_") or k.startswith("PROD_ES_") for k in env):
        return False
    return ec.resolve_api_key(env) is not None


pytestmark = pytest.mark.skipif(
    not _gate_open(os.environ),
    reason=f"真轨双闸未开（需 {P19_UAT_ENV}=1 + {EMB_REAL_ENV}=1 + TEST_* + key 可解析；"
           "真跑待主窗口放行令）",
)


# ---------- 客户端/快照（P19 UAT 同型） ----------

def _strip_quotes(value: str) -> str:
    if len(value) >= 2 and value.startswith("'") and value.endswith("'"):
        return value[1:-1]
    return value


def _build_milvus_client():
    from pymilvus import MilvusClient

    host = os.environ.get("TEST_MILVUS_HOST", "")
    port = int(os.environ.get("TEST_MILVUS_PORT", "19530"))
    user = _strip_quotes(os.environ.get("TEST_MILVUS_USER", ""))
    password = _strip_quotes(os.environ.get("TEST_MILVUS_PASS", ""))
    return MilvusClient(uri=f"http://{host}:{port}", token=f"{user}:{password}",
                        timeout=30)


def _build_es_client():
    from elasticsearch import Elasticsearch

    host = os.environ.get("TEST_ES_HOST", APPROVED_UAT_HOST)
    port = int(os.environ.get("TEST_ES_PORT", str(APPROVED_UAT_PORT)))
    scheme = os.environ.get("TEST_ES_SCHEME", APPROVED_UAT_SCHEME)
    user = os.environ.get("TEST_ES_USER", "")
    password = _strip_quotes(
        os.environ.get("TEST_ES_PASSWORD") or os.environ.get("TEST_ES_PASS", ""))
    return Elasticsearch(f"{scheme}://{host}:{port}", basic_auth=(user, password),
                         verify_certs=False, request_timeout=30)


def _snapshot_indices(client) -> list[str]:
    response = client.cat.indices(format="json", h="index")
    return sorted(item.get("index", "") for item in response)


# ---------- 金标加载（P23 manifest 同款加载链，importlib 单源不复制） ----------

def _load_gold_plan():
    """n11/n23 诊断件同款：importlib 加载 test_p23_uat 模块取 _load_manifest/
    _build_replay_plan（单源不复制；模块级 skip 标记在本进程无害）。"""
    spec = importlib.util.spec_from_file_location("p23_mod", _P23_MOD_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    manifest = mod._load_manifest()
    books = tuple(mod._DEFAULT_WORKBOOK_DIR / name for name in mod.WORKBOOK_NAMES)
    from news_flash_dedup.gold import load_workbooks

    catalog = load_workbooks(books)
    rows = {row.row_key: row for row in catalog.rows}
    plan_by_row = {r["row_key"]: r for r in manifest["synthetic_plan"]["rows"]}
    clear, _hold = mod._build_replay_plan(manifest, rows, plan_by_row)
    return rows, clear


def _gold_samples(rows, clear):
    """N11 同款选样：释义重复前 6 + 逐字重复前 3 + 硬负例前 6（pair_id[:8] 钉）。"""
    dup_para, dup_verbatim, neg = [], [], []
    for e in clear:
        h, c = rows[e.history_key].raw_text, rows[e.current_key].raw_text
        if e.gold_label == "重复":
            (dup_verbatim if h == c else dup_para).append((e.pair_id[:8], h, c))
        elif e.gold_label == "不重复" and len(neg) < 6:
            neg.append((e.pair_id[:8], h, c))
    return dup_para[:6], dup_verbatim[:3], neg


# ---------- fixtures ----------

_RUN_UUID_CELL: dict = {"value": None}   # shadow_env → cluster teardown 白名单传递


@pytest.fixture(scope="module")
def shadow_cluster():
    """公共标尺 3（R191 删除冻结版）：双客户端 + 跑前双快照（含封存物新基线）；
    跑后 diff 纪律 = 「新 run 对象之外零变化」——封存物两版名单恒等（任何
    missing 即硬停）、新增仅限本 run 白名单前缀、本 run 对象原地封存不删
    （删除冻结，最终处置归用户亲决）。"""
    milvus = _build_milvus_client()
    es = _build_es_client()
    cols_before = sorted(milvus.list_collections())
    idx_before = _snapshot_indices(es)
    print(f"[EMB-SHADOW] baseline collections (含封存物): {cols_before}")
    print(f"[EMB-SHADOW] baseline indices: {len(idx_before)}")
    yield {"milvus": milvus, "es": es,
           "cols_before": cols_before, "idx_before": idx_before,
           "run_uuid": None}
    cols_after = sorted(milvus.list_collections())
    idx_after = _snapshot_indices(es)
    # 本 run 白名单（shadow_env 在依赖 fixture 中先行 teardown 并写入）
    # ——依赖序保证 shadow_env teardown 先于本 fixture teardown 执行。
    run_uuid = _RUN_UUID_CELL["value"]
    added_c = sorted(set(cols_after) - set(cols_before))
    missing_c = sorted(set(cols_before) - set(cols_after))
    added_i = sorted(set(idx_after) - set(idx_before))
    missing_i = sorted(set(idx_before) - set(idx_after))
    own_c = [c for c in added_c
             if run_uuid and c.startswith(f"p19_{run_uuid}_")]
    own_i = [i for i in added_i
             if run_uuid and i.startswith(f"p19-batch-{run_uuid}-")]
    foreign_c = sorted(set(added_c) - set(own_c))
    foreign_i = sorted(set(added_i) - set(own_i))
    print(f"[EMB-SHADOW] sealed own: collections={own_c} indices={own_i}")
    print(f"[EMB-SHADOW] diff foreign added: c={foreign_c} i={foreign_i}；"
          f"missing: c={missing_c} i={missing_i}")
    assert not missing_c and not missing_i, (
        "封存物/保留对象被删改（R191 冻结硬停）！"
        f"collections missing={missing_c}；indices missing={missing_i}")
    assert not foreign_c and not foreign_i, (
        "新 run 白名单之外出现新增对象（越界硬停）！"
        f"collections added={foreign_c}；indices added={foreign_i}")
    milvus.close()
    es.close()


@pytest.fixture(scope="module")
def shadow_env(shadow_cluster):
    """真链装配：真 space(1024) + 真客户端 + 真仓储 + 真编排 + 真查询器。"""
    run_uuid = "emb" + datetime.now(timezone.utc).strftime("%H%M%S%f")  # W-R3b 秒级→微秒
    space = qb.real_embedding_space()
    config = p19_ms.RealMilvusP19Config(
        run_uuid=run_uuid, business_date=date.today(), space=space)
    store = p19_ms.RealMilvusP19Store(
        shadow_cluster["milvus"], shadow_cluster["es"], config)
    budget = int(os.environ.get(SHADOW_BUDGET_ENV, str(SHADOW_DAILY_BUDGET)))
    client = ec.EmbeddingClient(
        ec.EmbeddingClientConfig(
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            model="text-embedding-v3", dimension=1024,
            cache_root=_SHADOW_CACHE_ROOT, daily_token_budget=budget,
            query_disk_cache_enabled=True),       # shadow 期间查询磁盘层开
        env=dict(os.environ))
    tokenizer = qb.QwenBpeTokenizer()
    pipeline = vp.RealVectorPipeline(store, client, space, tokenizer=tokenizer)
    recall_store = MilvusVectorStore(
        shadow_cluster["milvus"], shadow_cluster["es"], config.collection_name,
        f"p19-batch-{run_uuid}-", space)
    searcher = eq.RealVectorSearcher(recall_store, eq.QueryEmbedder(client, space))
    print(f"[EMB-SHADOW] isolated collection: {config.collection_name}")
    print(f"[EMB-SHADOW] isolated indices: {config.items_index} / "
          f"{config.control_index}")
    yield {**shadow_cluster, "run_uuid": run_uuid, "space": space,
           "config": config, "store": store, "client": client,
           "tokenizer": tokenizer, "pipeline": pipeline,
           "recall_store": recall_store, "searcher": searcher}
    # R191 删除冻结：清场环节整体取消——本 run 对象原地封存（只读登记，
    # 最终处置归用户亲决，清单=log/temp/b5-leftover-inventory.json）。
    _RUN_UUID_CELL["value"] = run_uuid       # 供 cluster teardown 白名单
    own = {"collection": config.collection_name,
           "indices": [config.items_index, config.control_index]}
    print(f"[EMB-SHADOW seal] R191 删除冻结：本 run 对象原地封存（不删）: {own}")


def _rid(seed: str) -> str:
    return hashlib.sha256(f"p19shadow-{seed}".encode("utf-8")).hexdigest()


def _write_main_record(env, record_id: str, *, scope_id: str, arrival_seq: int,
                       text: str) -> None:
    """创建者侧主记录（vector_state=pending，INV-6：P19 不写 pending）。"""
    env["es"].index(
        index=env["config"].items_index, id=record_id,
        document={
            "record_id": record_id, "item_id": f"item-{record_id[:8]}",
            "scope_id": scope_id, "business_date": BUSINESS_DATE_STR,
            "arrival_seq": arrival_seq,
            "raw_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "text": text,
            "embedding_space_id": env["space"].space_id,
            "pipeline_version": "dedup_v1", "vector_state": "pending",
            "task_state": "succeeded", "audit_complete": True,
            "expires_at": "2999-01-01T00:00:00+00:00",
        },
        op_type="create")


def _request(env, record_id: str, text: str, arrival_seq: int,
             frontier: int) -> RecallRequest:
    return RecallRequest(
        scope_id="gold", business_date=BUSINESS_DATE_STR, record_id=record_id,
        item_id=f"item-{record_id[:8]}", arrival_seq=arrival_seq, text=text,
        visible_seq=frontier, prepared_seq=frontier,   # prepared_seq 正确透传
        embedding_space_id=env["space"].space_id)


def _cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return dot / (na * nb)


# ---------- §7.3-1 真 space(1024) 建档 + schema 对拍（B-0 出） ----------

def test_b0_real_space_collection_schema(shadow_env):
    milvus = shadow_env["milvus"]
    config = shadow_env["config"]
    description = milvus.describe_collection(collection_name=config.collection_name)
    names = {f["name"] for f in description["fields"]}
    assert names == set(VECTOR_FIELDS)
    dims = {f["params"]["dim"] for f in description["fields"]
            if f["name"] == "embedding"}
    assert dims == {1024}                                # 维度=真空间维度（INV-4）
    assert config.collection_name.endswith(shadow_env["space"].space_id)
    partition = "bd_" + BUSINESS_DATE_STR.replace("-", "")
    assert partition in milvus.list_partitions(collection_name=config.collection_name)
    assert shadow_env["es"].indices.exists(index=config.items_index)
    assert shadow_env["es"].indices.exists(index=config.control_index)


# ---------- §7.3-2 真 embed → upsert → Strong search 自匹配 ----------

def test_real_embed_upsert_strong_self_match(shadow_env):
    env = shadow_env
    rid = _rid("selfmatch")
    _write_main_record(env, rid, scope_id="gold", arrival_seq=900001,
                       text="中国人民银行宣布降准零点五个百分点。")
    report = env["pipeline"].ingest_record(
        record_id=rid, text="中国人民银行宣布降准零点五个百分点。",
        scope_id="gold", arrival_seq=900001)
    assert report.state == "ready"
    hits = env["store"].search_strong(
        query_vector=env["client"].embed_query("中国人民银行宣布降准零点五个百分点。"),
        scope_id="gold", business_date=BUSINESS_DATE_STR)
    own = [hit for hit in hits if hit["record_id"] == rid]
    assert own and max(hit["score"] for hit in own) > 0.99   # 自匹配 COSINE≈1


# ---------- §7.3-3 金标抽样对候选召回 + 硬负例登记不判 fp ----------

def test_gold_sample_recall_real_chain(shadow_env):
    env = shadow_env
    rows, clear = _load_gold_plan()
    dup_para, dup_verbatim, neg = _gold_samples(rows, clear)
    base_seq = 910000
    written = {}
    triples = dup_para + dup_verbatim + neg
    # 建档：每对 (H, C) 两记录（同文本各一记录，逐字对亦两记录两身份）
    for index, (pid, h, c) in enumerate(triples):
        for side, text in (("H", h), ("C", c)):
            rid = _rid(f"sample-{index}-{side}")
            written[(index, side)] = rid
            _write_main_record(env, rid, scope_id="gold",
                               arrival_seq=base_seq + index * 2 + (0 if side == "H" else 1),
                               text=text)
            env["pipeline"].ingest_record(record_id=rid, text=text, scope_id="gold",
                                          arrival_seq=base_seq + index * 2
                                          + (0 if side == "H" else 1))
    # 逐字对：partner 顶名（恒等必召）
    for index in range(len(dup_para), len(dup_para) + len(dup_verbatim)):
        pid, h, c = triples[index]
        result = env["searcher"].search(
            _request(env, written[(index, "C")], c, base_seq + index * 2 + 1,
                     base_seq + 100), None)
        assert result.status == "complete"
        assert result.candidates[0].record_id == written[(index, "H")], (
            f"逐字对 {pid} partner 未顶名")
    # 释义对：partner 入候选；硬负例：登记不判 fp
    hard_negative_hits = 0
    for index, (pid, h, c) in enumerate(triples):
        result = env["searcher"].search(
            _request(env, written[(index, "C")], c, base_seq + index * 2 + 1,
                     base_seq + 100), None)
        assert result.status == "complete"
        ids = [cand.record_id for cand in result.candidates]
        if index < len(dup_para):
            assert written[(index, "H")] in ids, f"释义对 {pid} partner 漏召"
        elif index >= len(dup_para) + len(dup_verbatim):
            hard_negative_hits += int(written[(index, "H")] in ids)  # 登记不判
    print(f"[EMB-SHADOW] 硬负例候选命中登记：{hard_negative_hits}/6（不判 fp）")


# ---------- §7.3-4 canary 漂移探针 + usage 校准对拍 ----------

def test_canary_probe_and_usage_oracle(shadow_env, tmp_path):
    env = shadow_env
    rows, clear = _load_gold_plan()
    dup_para, dup_verbatim, neg = _gold_samples(rows, clear)
    reference = json.loads(_N11_JSON.read_text(encoding="utf-8"))
    ref_samples = {tag: dict(pairs) for tag, pairs in reference["samples"].items()}
    evidence = {"dimension": None, "pairs": [], "usage_oracle": {}}
    # canary 专用冷域客户端（成本 ≈1.2k tok，shadow 小闸内）。
    # usage 对拍口径（三连败根因链钉死，客户端路径复刻证据
    # log/temp/p19-s5-canary-client-replica.json）：
    # - 客户端按 cache_key 对同文本幂等去重——3 对逐字对的 current 与 history
    #   同文，第二次 embed_query 走进程内 LRU（正确产品行为）→ 零 API；
    # - 故本地侧须按 **API 实发集合（唯一文本）** 计数，与服务端 usage 同分母：
    #   30 文 → 27 唯一（实发 27 调用、服务端 1074 tok；三双胞服务端合计
    #   101 tok 从未发出，1074+101=1175 与 raw-urllib 全发口径吻合）；
    # - 2220 轮 127% 假偏差（共享客户端多层缓存）与 r190/r191 轮 22.5%
    #   （LRU 双胞去重）同根：对拍分母含未实发文本。
    canary_client = ec.EmbeddingClient(
        ec.EmbeddingClientConfig(
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            model="text-embedding-v3", dimension=1024,
            cache_root=tmp_path / "canary-cache",
            daily_token_budget=int(os.environ.get(SHADOW_BUDGET_ENV,
                                                  str(SHADOW_DAILY_BUDGET)))),
        env=dict(os.environ))
    usage_before = canary_client.usage_snapshot()
    local_unique: dict = {}                    # 唯一文本 → 本地 token 计数
    for tag, pairs in (("dup_verbatim", dup_verbatim), ("dup_para", dup_para),
                       ("negative", neg)):
        for pid, h, c in pairs:
            assert pid in ref_samples[tag], f"canary 对 {tag}/{pid} 不在 N11 钉死集"
            vh = canary_client.embed_query(h)
            vc = canary_client.embed_query(c)
            assert len(vh) == len(vc) == 1024        # 维度 1024（INV-4）
            evidence["dimension"] = 1024
            sim = _cosine(vh, vc)
            expected = ref_samples[tag][pid]
            assert abs(sim - expected) <= CANARY_COSINE_BAND, (
                f"canary 漂移：{tag}/{pid} cosine={sim:.4f} "
                f"vs N11 锚 {expected}（带 ±{CANARY_COSINE_BAND}）")
            evidence["pairs"].append({"tag": tag, "pair": pid,
                                      "cosine": round(sim, 4), "anchor": expected})
            local_unique.setdefault(h, qb.count_tokens(env["tokenizer"], h))
            local_unique.setdefault(c, qb.count_tokens(env["tokenizer"], c))
    total_local_tokens = sum(local_unique.values())
    # usage 校准对拍（§2.6）：本地 BPE 计数 vs 服务端 usage.total_tokens
    # （30 文 canary 语料批级对拍；带 = 批级相对 20%【主窗口评审钉死】，
    #  依据见模块头 USAGE_ORACLE_REL_BAND 注释）
    usage_after = canary_client.usage_snapshot()
    server_tokens = usage_after["total_tokens"] - usage_before["total_tokens"]
    deviation = server_tokens - total_local_tokens
    rel = abs(deviation) / max(1, server_tokens)
    evidence["usage_oracle"] = {
        "server_total_tokens": server_tokens,
        "local_bpe_tokens": total_local_tokens,
        "unique_texts": len(local_unique),
        "deviation": deviation,
        "rel_deviation": round(rel, 4),
        "rel_band": USAGE_ORACLE_REL_BAND,
        "estimated_usage_events": (usage_after["estimated_usage_events"]
                                   - usage_before["estimated_usage_events"]),
    }
    assert rel <= USAGE_ORACLE_REL_BAND, (
        f"usage 校准相对偏差 {rel:.4f} 超带 {USAGE_ORACLE_REL_BAND}（评审钉死）")
    print(f"[EMB-SHADOW canary] {json.dumps(evidence['usage_oracle'], ensure_ascii=False)}")
    out = _LOG_TEMP / f"emb-canary-{env['run_uuid']}.json"
    out.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------- §7.3-5 断网/429 注入 → 失败语义真链演示 ----------

class _FaultTransport:
    def __init__(self, fault):
        self.fault = fault
        self.calls = 0

    def __call__(self, **kwargs):
        self.calls += 1
        raise self.fault


def test_outage_injection_failure_semantics(shadow_env, tmp_path):
    env = shadow_env
    fault_client = ec.EmbeddingClient(
        ec.EmbeddingClientConfig(
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            model="text-embedding-v3", dimension=1024,
            cache_root=tmp_path / "fault-cache", daily_token_budget=SHADOW_DAILY_BUDGET),
        transport_fn=_FaultTransport(ec.EmbeddingRateLimited("injected 429")),
        env=dict(os.environ), sleep=lambda s: None, rng=lambda: 0.0)
    # 写径：429 超退避 → failed + EmbeddingApiError（不假成功）
    pipe = vp.RealVectorPipeline(env["store"], fault_client, env["space"],
                                 tokenizer=env["tokenizer"])
    rid = _rid("outage")
    _write_main_record(env, rid, scope_id="gold", arrival_seq=920001,
                       text="断网注入用例正文。")
    with pytest.raises(ec.EmbeddingApiError):
        pipe.ingest_record(record_id=rid, text="断网注入用例正文。",
                           scope_id="gold", arrival_seq=920001)
    assert env["store"].get_vector_state(rid) == "failed"
    assert env["store"].get_vector_row(f"{rid}_0") is None      # 零写入
    # 查询径：→ unavailable + EMBEDDING_QUERY_FAILED（不裸抛）
    searcher = eq.RealVectorSearcher(
        env["recall_store"], eq.QueryEmbedder(fault_client, env["space"]))
    result = searcher.search(
        _request(env, _rid("selfmatch"), "中国人民银行宣布降准。", 1, 100), None)
    assert result.status == "unavailable"
    assert result.error_code == "EMBEDDING_QUERY_FAILED"


# ---------- §7.3-6 半写恢复重放 ----------

def test_half_write_replay_zero_api(shadow_env):
    env = shadow_env
    rid = _rid("halfwrite")
    text = "半写恢复重放用例：先嵌入后中断，重放幂等。"
    _write_main_record(env, rid, scope_id="gold", arrival_seq=930001, text=text)
    chunks = qb.real_chunk_text(text, env["tokenizer"])
    vectors = env["client"].embed_documents([c.text for c in chunks])   # 嵌入成功
    env["store"].upsert_chunks(                                          # 写入成功
        record_id=rid,
        chunks=[(c.chunk_id, v) for c, v in zip(chunks, vectors)],
        scope_id="gold", arrival_seq=930001)
    assert env["store"].get_vector_state(rid) == "pending"               # CAS 前中断
    calls_before = env["client"].usage_snapshot()["api_calls"]
    report = env["pipeline"].ingest_record(                              # 重放
        record_id=rid, text=text, scope_id="gold", arrival_seq=930001)
    assert report.state == "ready"
    assert all(outcome == "reused" for _, outcome in report.outcomes)    # 幂等零改写
    assert env["client"].usage_snapshot()["api_calls"] == calls_before   # 重放零 API


# ---------- §4.3 shadow 双腿对拍 + 四验收闸 ----------

def test_shadow_two_leg_recall_gates(shadow_env):
    env = shadow_env
    rows, clear = _load_gold_plan()
    # 语料：clear 对全部端点行（N23 同款加载域）；同日到达序模拟
    row_seq: dict[str, int] = {}
    for e in clear:
        row_seq.setdefault(e.history_key, e.history_seq)
        row_seq.setdefault(e.current_key, e.current_seq)
    ordered = sorted(row_seq, key=lambda k: (row_seq[k], k))
    rank_of = {key: index + 1 for index, key in enumerate(ordered)}
    frontier = len(ordered) + 1
    record_of = {key: _rid(f"corpus-{key}") for key in ordered}
    text_of = {key: rows[key].raw_text for key in ordered}

    # 建档（写入真链；嵌入缓存经 tmp/emb-shadow-cache 持久域复跑零成本）
    for key in ordered:
        rid = record_of[key]
        _write_main_record(env, rid, scope_id="gold", arrival_seq=rank_of[key],
                           text=text_of[key])
        env["pipeline"].ingest_record(record_id=rid, text=text_of[key],
                                      scope_id="gold", arrival_seq=rank_of[key])

    def run_queries():
        """双腿测量：腿 B = 真链通道候选；腿 A = 固定候选轨（金标直供）。"""
        metrics = {"verbatim": [], "paraphrase": [], "hard_negative_hits": 0,
                   "hard_negative_total": 0, "holes": [], "queries": 0}
        api_before = env["client"].usage_snapshot()["api_calls"]
        for e in clear:
            h_key, c_key = e.history_key, e.current_key
            h_text, c_text = text_of[h_key], text_of[c_key]
            result = env["searcher"].search(
                _request(env, record_of[c_key], c_text, rank_of[c_key], frontier),
                None)
            metrics["queries"] += 1
            if result.status != "complete":
                metrics["holes"].append({                         # 闸 4 登记
                    "pair_id": e.pair_id[:8], "status": result.status,
                    "error_code": result.error_code})
                continue
            ids = [cand.record_id for cand in result.candidates]
            partner = record_of[h_key]
            rank = ids.index(partner) + 1 if partner in ids else None
            if e.gold_label == "重复":
                (metrics["verbatim"] if h_text == c_text
                 else metrics["paraphrase"]).append(
                    {"pair_id": e.pair_id[:8], "rank": rank})
            else:
                metrics["hard_negative_total"] += 1
                metrics["hard_negative_hits"] += int(rank is not None)  # 登记不判
        metrics["api_calls_delta"] = (env["client"].usage_snapshot()["api_calls"]
                                      - api_before)
        return metrics

    cold = run_queries()
    warm = run_queries()      # 复跑：全缓存零 API，指标须一致

    # ---- 差异分析（腿 B 视角；腿 A 固定候选=金标直供，恒 rank 1） ----
    missed = [p for p in cold["paraphrase"] if p["rank"] is None]
    verbatim_missed = [p for p in cold["verbatim"]
                       if p["rank"] is None or p["rank"] > 10]
    ranks = sorted(p["rank"] for p in cold["paraphrase"] if p["rank"] is not None)

    evidence = {
        "run_uuid": env["run_uuid"],
        "collection": env["config"].collection_name,
        "space_id": env["space"].space_id,
        "space_configuration": env["space"].configuration(),
        "key_masked": ec.mask_api_key(ec.resolve_api_key(os.environ)),
        "corpus": {"rows": len(ordered), "pairs": len(clear),
                   "verbatim_pairs": len(cold["verbatim"]),
                   "paraphrase_pairs": len(cold["paraphrase"]),
                   "hard_negative_pairs": cold["hard_negative_total"]},
        "leg_a_fixed_candidate": {"note": "现役固定候选轨：金标直供 partner，恒 rank 1"},
        "leg_b_real": {
            "recall@10_verbatim": (
                sum(1 for p in cold["verbatim"] if p["rank"] is not None
                    and p["rank"] <= 10) / max(1, len(cold["verbatim"]))),
            "recall@30_paraphrase": (
                sum(1 for p in cold["paraphrase"] if p["rank"] is not None
                    and p["rank"] <= 30) / max(1, len(cold["paraphrase"]))),
            "recall@1_paraphrase": (
                sum(1 for p in cold["paraphrase"] if p["rank"] == 1)
                / max(1, len(cold["paraphrase"]))),
            "rank_p50": ranks[len(ranks) // 2] if ranks else None,
            "rank_p90": ranks[int(len(ranks) * 0.9)] if ranks else None,
            "rank_max": ranks[-1] if ranks else None,
        },
        "missed_paraphrase": missed,
        "hard_negative_candidate_ratio": (
            cold["hard_negative_hits"] / max(1, cold["hard_negative_total"])),
        "holes": cold["holes"],
        "warm_run": {"recall_equal": None, "api_calls_delta": warm["api_calls_delta"]},
        "usage": env["client"].usage_snapshot(),
        "alarms": env["client"].alarms,
        "gates": {},
    }

    # ---- 四验收闸 ----
    gates = evidence["gates"]
    gates["gate1_verbatim_recall@10"] = {
        "pass": not verbatim_missed and len(cold["verbatim"]) > 0,
        "detail": f"{len(cold['verbatim']) - len(verbatim_missed)}"
                  f"/{len(cold['verbatim'])}", "missed": verbatim_missed}
    gates["gate2_paraphrase_recall@30"] = {
        "pass": (not missed and len(cold["paraphrase"]) == 263),
        "detail": f"{263 - len(missed)}/263 (N23 锚)",
        "reference_band": {"recall@1": 0.9962, "rank_p50/p90/max": "1/1/2"}}
    gates["gate3_fp0_candidate_layer"] = {
        "pass": True,
        "detail": "候选层不判重（本 harness 结构上零判重）；硬负例候选占比 "
                  f"{evidence['hard_negative_candidate_ratio']:.4f} 仅登记"}
    gates["gate4_holes_registered"] = {
        "pass": True,
        "detail": f"孤儿/孔洞事件 {len(cold['holes'])} 件全登记（RECALL_INCOMPLETE 不掩盖）"}
    evidence["warm_run"]["recall_equal"] = (
        cold["verbatim"] == warm["verbatim"]
        and cold["paraphrase"] == warm["paraphrase"])

    out = _LOG_TEMP / f"emb-shadow-{env['run_uuid']}.json"
    out.write_text(json.dumps(evidence, ensure_ascii=False, indent=2, default=str),
                   encoding="utf-8")
    print(f"[EMB-SHADOW] evidence -> {out}")

    # 闸断言（不过闸不进入 B6，§4.3）
    assert not verbatim_missed, f"闸 1 未过：逐字对漏召 {verbatim_missed}"
    assert not missed and len(cold["paraphrase"]) == 263, (
        f"闸 2 未过：释义对 recall@30 != 1.0000（missed={missed}，"
        f"n={len(cold['paraphrase'])}，锚 263/263 N23）")
    assert warm["api_calls_delta"] == 0, "复跑须全缓存零 API"
    assert evidence["warm_run"]["recall_equal"], "冷跑/复跑指标须一致"
