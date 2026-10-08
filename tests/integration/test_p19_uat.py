"""P19 真 Milvus UAT（05:28 批复标尺 + 公共标尺 · T3 站）。

起草本稿的纪律背景：
- 本稿由 T3 子窗口起草；src 产品代码（vector/milvus_store.py 等）由主窗口亲笔
  （决策日志 D7 同例）。P19 真 Milvus 适配器未落 src 前（挂账 N10/N12），依赖接口
  的用例一律 pytest.skip 并注明"待主窗口接线批复"；草案全文见
  `log/temp/p19-uat-wiring-proposal.md`。
- Milvus 侧已核验事实（18:5x 主窗口 A1 预检 / 挂账 N11 已闭）：
  pymilvus 2.6.13 可用；`MilvusClient(uri='http://<TEST_MILVUS_HOST>:19530',
  token='<TEST_MILVUS_USER>:<TEST_MILVUS_PASS>')` 实测可列出集合；基线已拍
  `log/temp/milvus-collections-baseline-pre-uat.json` =
  ["deerflow_rag_knowledge_base", "rag_knowledge_base"]（2 个保留集合）。
- T1/T2 真机教训的 Milvus 侧类比（本稿全量内化）：
  a) ES auto_create_index=-* 须显式建索引 → Milvus 集合显式创建（schema+index_params
     +日分区，P12 UAT 先例）；
  b) ES "不存在才创建"=op_type=create → Milvus Stable ID（<record_id>_<chunk_id>）
     upsert 幂等：同身份重放=reused 零改写，异身份同主键=VectorIdentityConflict（INV-2）；
  c) ES _seq_no 0 基 → Milvus 写确认以 upsert_count==1 + Strong get 回读逐字段相等为准，
     不做任何版本号算术假设；
  d) watermark 返回新值 → vector_state 翻转以 ES 实时 GET 回读为准；
  e) .env 值带单引号，去引号由运行环境导出保证（此处 strip 仅兜底）；
  f) ES 纯 HTTP → Milvus uri 同走 http://<host>:19530（A1 预检同款）。
- INV-7（自律级）：真实 embedding 模型准入未过不写真实向量——本稿全部使用确定性
  mock 向量（sha256 派生 4 维 float32，维度由 mock EmbeddingSpace 固定，P12 UAT 同型）。

公共标尺（目标文档 §三.5 + 05:28 批复 §8）：
1. 闸门 `P19_CONFIRM_UAT=1` 缺失即 skip/拒启；仅 TEST_* 凭据；PROD_* 出现即拒启；
2. 隔离集合 `p19_<run_uuid>_<space_id>`（Milvus 集合名仅允许 [A-Za-z0-9_]，连字符
   非法——P12 `news_dedup_replay_...` 先例；与指令字面 `p19-<run_uuid>-` 的偏差及
   裁定见建议书矛盾点 #1）+ 隔离 ES 索引 `p19-batch-<run_uuid>-news-dedup-
   (items|control)-v1-YYYY.MM.DD`；
3. 跑前 list_collections + `_cat/indices` 双快照、跑后严格 diff 证明 2 保留集合与
   全部保留索引零触碰、无 p19 stray；
4. dry-run 先行 + 清场（仅 drop/delete 本 run 前缀）；UAT 日志全文落盘；
5. 发现越界立即全停（fixture 快照 diff 即硬停断言）。

跑站命令（主窗口执行；footer 分钟级向下取整）：

    $env:PYTHONPATH='src'
    $env:PYTHONIOENCODING='utf-8'
    $env:PYTHONDONTWRITEBYTECODE='1'
    $env:DEPLOY_ENV='test'
    $env:P19_CONFIRM_UAT='1'
    $env:TEST_MILVUS_HOST=<去引号主机>; $env:TEST_MILVUS_PORT='19530'
    $env:TEST_MILVUS_USER=<去引号用户>; $env:TEST_MILVUS_PASS=<去引号密码>
    $env:TEST_ES_PASSWORD=<去引号密码>   # 同时导出 TEST_ES_PASS 同值
    & '.venv-v1\\Scripts\\python.exe' -m pytest -p no:cacheprovider `
        --basetemp '.\\tmp\\p19-uat-run' -v tests/integration/test_p19_uat.py `
        > ..\\log\\temp\\p19-uat-run-<HHMM>.txt
"""

from __future__ import annotations

import hashlib
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
from news_flash_dedup.recall.vector_space import EmbeddingSpace

# ---------- 受守卫的产品接口导入（挂账 N10/N12：vector/milvus_store.py 未落 src 时整组 skip） ----------

try:
    from news_flash_dedup.vector import milvus_store as p19_ms

    _P19_MS_IMPORT_ERROR = None
except ImportError as exc:  # vector/milvus_store.py 未落（T3 主窗口亲笔）
    p19_ms = None
    _P19_MS_IMPORT_ERROR = exc


P19_UAT_ENV = "P19_CONFIRM_UAT"
BUSINESS_DATE_STR = "2026-09-26"
BUSINESS_DATE = date(2026, 9, 26)
PRESERVED_COLLECTIONS = ("deerflow_rag_knowledge_base", "rag_knowledge_base")
BASELINE_JSON = (
    Path(__file__).resolve().parents[3]
    / "log" / "temp" / "milvus-collections-baseline-pre-uat.json"
)


# ---------- 闸门（公共标尺 1） ----------

def _gate_open(env) -> bool:
    """harness 级闸门：P19_CONFIRM_UAT=1 + TEST_MILVUS_*/TEST_ES_* 就位 + 无 PROD 泄漏。"""
    if env.get(P19_UAT_ENV) != "1":
        return False
    if not env.get("TEST_MILVUS_HOST"):
        return False
    if not env.get("TEST_ES_HOST"):
        return False
    if any(k.startswith("PROD_MILVUS_") or k.startswith("PROD_ES_") for k in env):
        return False
    return True


def _uat_available() -> bool:
    return _gate_open(os.environ)


pytestmark = pytest.mark.skipif(
    not _uat_available(),
    reason=f"P19 UAT gate not open (need {P19_UAT_ENV}=1 + TEST_MILVUS_*/TEST_ES_*, no PROD_*)",
)


def _require_p19_ms() -> None:
    """P19 真 Milvus 适配器未落 src 时 skip（挂账 N10/N12；待主窗口接线批复）。"""
    if p19_ms is None:
        pytest.skip(
            "P19 真 Milvus 适配器 vector/milvus_store.py 未落 src（挂账 N10/N12；"
            "草案全文见 log/temp/p19-uat-wiring-proposal.md）——待主窗口接线批复"
        )


# ---------- 数据构造助手（确定性 mock，INV-7） ----------

def _space() -> EmbeddingSpace:
    """mock EmbeddingSpace（P12 UAT 同型；空间标识=不可变配置摘要，§2.1）。

    全部组件为小写标识符；dimension=4 固定 mock 维度；metric=COSINE。
    """
    return EmbeddingSpace(
        "mock_embedding", "p19_uat_fixture_v1", "codepoint_v1", "none", "COSINE", 4,
        "plain_v1", "plain_v1", "tokens512_overlap64_v1", "p09_v1",
    )


def _mock_embedding(seed: str) -> list[float]:
    """确定性 mock 向量：sha256 派生 4 维 float32（逐字节 (b+1)/256 ∈ (0,1]）。

    非零、有限、float32 可表示（prepare_vector_row 打包不溢出）；同一种子恒同向量，
    可重入恢复场景据此做逐字段身份对拍（INV-2）。
    """
    digest = hashlib.sha256(f"p19uat-emb-{seed}".encode("utf-8")).digest()
    return [(byte + 1) / 256.0 for byte in digest[: _space().dimension]]


def _rid(suffix: str) -> str:
    """确定性 64-hex record_id（vector_id 主键合法性要求 64 小写 hex）。"""
    return hashlib.sha256(f"p19uat-{suffix}".encode("utf-8")).hexdigest()


def _scope(suffix: str) -> str:
    """场景级 scope_id（[a-z0-9_-]；场景间在共享隔离集合内互不串扰）。"""
    return f"p19uat{suffix}"


def _main_record_shell(record_id: str, *, scope_id: str, arrival_seq: int,
                       vector_state: str = "pending") -> dict:
    """创建者侧主记录壳（05:28 修订 1：首次创建者写 vector_state=pending）。

    UAT 以原始 ES client 直写模拟创建者（P17/P18 落库路径）；P19 仓储只做
    pending→ready/failed 翻转（INV-6 独占权），绝不写 pending。
    """
    return {
        "record_id": record_id,
        "item_id": f"item-{record_id[:8]}",
        "scope_id": scope_id,
        "business_date": BUSINESS_DATE_STR,
        "arrival_seq": arrival_seq,
        "raw_hash": hashlib.sha256(f"raw-{record_id}".encode("utf-8")).hexdigest(),
        "pipeline_version": "dedup_v1",
        "vector_state": vector_state,
        "task_state": "succeeded",
        "audit_complete": True,
    }


# ---------- 客户端构造与快照 ----------

def _strip_quotes(value: str) -> str:
    """.env 单引号 strip（约束 e：去引号由导出侧保证，此处仅防 .env 直读兜底）。"""
    if len(value) >= 2 and value.startswith("'") and value.endswith("'"):
        return value[1:-1]
    return value


def _build_milvus_client():
    """从 TEST_MILVUS_* 构造客户端（A1 预检验证同款：uri=http://host:19530 + token）。

    双命名约定不适用（Milvus 单侧 PASS）；.env 单引号 strip 仅兜底（约束 e）。
    """
    from pymilvus import MilvusClient

    host = os.environ.get("TEST_MILVUS_HOST", "")
    port = int(os.environ.get("TEST_MILVUS_PORT", "19530"))
    user = _strip_quotes(os.environ.get("TEST_MILVUS_USER", ""))
    password = _strip_quotes(os.environ.get("TEST_MILVUS_PASS", ""))
    return MilvusClient(
        uri=f"http://{host}:{port}", token=f"{user}:{password}", timeout=30,
    )


def _build_es_client():
    """从 TEST_ES_* 构造客户端（T2 `_build_es_client` 同型，读 TEST_ES_PASSWORD）。

    双命名约定：TEST_ES_PASSWORD 优先、TEST_ES_PASS 兜底（运行环境同值导出）。
    """
    from elasticsearch import Elasticsearch

    host = os.environ.get("TEST_ES_HOST", APPROVED_UAT_HOST)
    port = int(os.environ.get("TEST_ES_PORT", str(APPROVED_UAT_PORT)))
    scheme = os.environ.get("TEST_ES_SCHEME", APPROVED_UAT_SCHEME)  # 纯 HTTP（约束 f）
    user = os.environ.get("TEST_ES_USER", "")
    password = _strip_quotes(
        os.environ.get("TEST_ES_PASSWORD") or os.environ.get("TEST_ES_PASS", "")
    )
    return Elasticsearch(
        f"{scheme}://{host}:{port}",
        basic_auth=(user, password),
        verify_certs=False,
        request_timeout=30,
    )


def _snapshot_indices(client) -> list[str]:
    """`_cat/indices` 快照（保留索引清单，T2 同型）。"""
    response = client.cat.indices(format="json", h="index")
    return sorted(item.get("index", "") for item in response)


# ---------- fixtures：双客户端 + 双快照/清场/前缀隔离 ----------

@pytest.fixture(scope="module")
def uat_cluster():
    """公共标尺 3：Milvus/ES 双客户端 + 跑前双快照；跑后严格 diff（零触碰/无 stray 硬停）。"""
    milvus = _build_milvus_client()
    es = _build_es_client()
    cols_before = sorted(milvus.list_collections())
    idx_before = _snapshot_indices(es)
    print(f"[P19-UAT] baseline collections: {cols_before}")
    print(f"[P19-UAT] baseline indices: {len(idx_before)}")
    yield {
        "milvus": milvus, "es": es,
        "cols_before": cols_before, "idx_before": idx_before,
    }
    cols_after = sorted(milvus.list_collections())
    idx_after = _snapshot_indices(es)
    added_c = sorted(set(cols_after) - set(cols_before))
    missing_c = sorted(set(cols_before) - set(cols_after))
    added_i = sorted(set(idx_after) - set(idx_before))
    missing_i = sorted(set(idx_before) - set(idx_after))
    print(f"[P19-UAT] collections diff added={added_c} missing={missing_c}")
    print(f"[P19-UAT] indices diff added={added_i} missing={missing_i}")
    assert not added_c and not missing_c and not added_i and not missing_i, (
        f"保留集合/索引被改动或 stray 残留（越界硬停）！"
        f"collections added={added_c} missing={missing_c}；"
        f"indices added={added_i} missing={missing_i}"
    )
    milvus.close()
    es.close()


@pytest.fixture(scope="module")
def uat_env(uat_cluster):
    """P19 隔离环境：run_uuid + RealMilvusP19Config + 真双写仓储；跑后 dry-run 先行清场。

    清场范围 = 本 run 前缀 `p19_<run_uuid>_`（Milvus 集合）与
    `p19-batch-<run_uuid>-`（ES 索引）（与 T1/T2 同款的更窄 run 作用域，等效且更
    保守）；fixture 析构顺序保证：先清 P19 隔离对象，后做双侧严格 diff。
    """
    _require_p19_ms()
    run_uuid = "uat" + datetime.now(timezone.utc).strftime("%H%M%S%f")  # W-R3b 秒级→微秒
    config = p19_ms.RealMilvusP19Config(
        run_uuid=run_uuid, business_date=BUSINESS_DATE, space=_space(),
    )
    # RealMilvusP19Store 构造即：闸门断言 + 显式建集合（八字段 schema + FLAT 索引
    # + 日分区 bd_20260926，约束 a 类比）+ 显式幂等建 ES 双索引
    store = p19_ms.RealMilvusP19Store(
        uat_cluster["milvus"], uat_cluster["es"], config,
    )
    print(f"[P19-UAT] isolated collection: {config.collection_name}")
    print(
        f"[P19-UAT] isolated indices: {config.items_index} / {config.control_index}"
    )
    yield {
        **uat_cluster,
        "config": config,
        "store": store,
        "run_uuid": run_uuid,
    }
    # 清场（公共标尺 4）：dry-run 先行，仅 drop/delete 本 run 前缀
    plan = store.cleanup_plan()
    print(f"[P19-UAT cleanup dry-run] will delete: {plan}")
    executed = store.cleanup()
    print(f"[P19-UAT cleanup] deleted: {executed}")
    assert executed == plan, (
        f"清场清单与 dry-run 不一致：dry-run={plan} executed={executed}"
    )


# ---------- 公共标尺 1：闸门 ----------

def test_p19_uat_harness_gate_requires_confirm_flag():
    """公共标尺 1（闸门）：`P19_CONFIRM_UAT=1` 缺失即 skip/拒启；仅 TEST_*；PROD_* 拒启。

    harness 级恒可跑（不依赖未落 src 的接口）；产品闸 `assert_p19_uat_open`
    在 vector/milvus_store.py 落地后由本用例顺带覆盖。
    """
    good = {P19_UAT_ENV: "1", "TEST_MILVUS_HOST": "milvus.example",
            "TEST_ES_HOST": "es.example"}
    assert _gate_open(good) is True
    assert _gate_open({"TEST_MILVUS_HOST": "milvus.example",
                       "TEST_ES_HOST": "es.example"}) is False          # 缺闸门
    assert _gate_open({P19_UAT_ENV: "0", "TEST_MILVUS_HOST": "milvus.example",
                       "TEST_ES_HOST": "es.example"}) is False
    assert _gate_open({P19_UAT_ENV: "1",
                       "TEST_ES_HOST": "es.example"}) is False          # 缺 Milvus 凭据
    assert _gate_open({P19_UAT_ENV: "1",
                       "TEST_MILVUS_HOST": "milvus.example"}) is False  # 缺 ES 凭据
    assert _gate_open({**good, "PROD_MILVUS_HOST": "prod.example"}) is False  # PROD 拒启
    assert _gate_open({**good, "PROD_ES_HOST": "prod.example"}) is False      # PROD 拒启
    if p19_ms is not None:
        with pytest.raises(p19_ms.P19UATGatesNotOpen):
            p19_ms.assert_p19_uat_open(env={"DEPLOY_ENV": "test"})


# ---------- 批复标尺 §2.1：空间标识由不可变配置摘要定 ----------

def test_p19_space_id_from_immutable_config_digest():
    """§2.1：embedding_space_id = 不可变配置摘要（s_ + sha256(config)[:40]）。

    harness 级恒可跑（recall/vector_space.py 已落 src）：同配置恒同 id（确定性）；
    任一组件（dimension/model/revision）变更即新 id（防混空间地基，INV-4）；
    EmbeddingSpace frozen——运行期不可改写配置。
    """
    space = _space()
    assert space.space_id == _space().space_id                    # 确定性
    assert space.space_id.startswith("s_") and len(space.space_id) == 42
    other_dim = EmbeddingSpace(
        "mock_embedding", "p19_uat_fixture_v1", "codepoint_v1", "none", "COSINE", 8,
        "plain_v1", "plain_v1", "tokens512_overlap64_v1", "p09_v1",
    )
    assert other_dim.space_id != space.space_id                   # 维度变更→新空间
    other_model = EmbeddingSpace(
        "mock_embedding_v2", "p19_uat_fixture_v1", "codepoint_v1", "none", "COSINE", 4,
        "plain_v1", "plain_v1", "tokens512_overlap64_v1", "p09_v1",
    )
    assert other_model.space_id != space.space_id                 # 模型变更→新空间
    with pytest.raises(AttributeError):                           # frozen 不可变
        space.dimension = 8  # type: ignore[misc]


# ---------- 公共标尺 2：隔离命名硬正则（集合 + ES 索引） ----------

def test_p19_isolation_naming_pattern_enforced():
    """隔离命名硬正则：集合 `p19_<run>_<space_id>` + ES 索引 `p19-batch-<run>-...`。

    Milvus 集合名仅允许 [A-Za-z0-9_]（连字符非法，P12 先例；建议书矛盾点 #1）；
    跨代前缀（P12 `news_dedup_replay_...`）与保留集合名一律拒收（跨代/越界防线）。
    """
    _require_p19_ms()
    space = _space()
    name = p19_ms.build_p19_collection_name("uat123456", space.space_id)
    assert name == f"p19_uat123456_{space.space_id}"
    p19_ms.validate_p19_collection_name(name)
    for kind in ("items", "control"):
        p19_ms.validate_p19_index_name(
            f"p19-batch-uat123456-news-dedup-{kind}-v1-2026.09.26"
        )
    # W2Fε 收紧（W1Fδ p20 模式跟进）：裸 Exception → P19CollectionNameInvalid /
    # P19IndexNameInvalid（milvus_store 两验证器各自唯一拒名异常类型；判别实证见下钉）
    with pytest.raises(p19_ms.P19CollectionNameInvalid, match="(?i)pattern|match|invalid"):
        p19_ms.validate_p19_collection_name(
            f"p19-uat123456-{space.space_id}"                     # 连字符形态（Milvus 非法）
        )
    with pytest.raises(p19_ms.P19CollectionNameInvalid, match="(?i)pattern|match|invalid"):
        p19_ms.validate_p19_collection_name(
            f"news_dedup_replay_uat123456_{space.space_id}"       # P12 跨代前缀
        )
    for preserved in PRESERVED_COLLECTIONS:
        with pytest.raises(p19_ms.P19CollectionNameInvalid, match="(?i)pattern|match|invalid"):
            p19_ms.validate_p19_collection_name(preserved)        # 保留集合名拒收
    with pytest.raises(p19_ms.P19IndexNameInvalid, match="(?i)pattern|match|invalid"):
        p19_ms.validate_p19_index_name(
            "p18-batch-uat123456-news-dedup-items-v1-2026.09.26"  # P18 跨代前缀
        )


# ---------- 公共标尺 2 补钉：收紧类型判别（W2Fε，W1Fδ p20 同款，逐收紧点各 1） ----------

@pytest.mark.parametrize("bad_name", [
    "p19-uat123456-s_hyphen",                                # 收紧点 1：连字符形态
    "news_dedup_replay_uat123456_s_cross",                   # 收紧点 2：P12 跨代前缀
    "deerflow_rag_knowledge_base",                           # 收紧点 3：保留集合名
])
def test_p19_collection_name_tightened_type_discriminates(bad_name):
    """W2Fε 新红钉（W1Fδ p20 模式跟进）：裸 Exception 收紧为
    P19CollectionNameInvalid 的判别实证（语义保持 + 异种异常放行）。"""
    _require_p19_ms()
    assert issubclass(p19_ms.P19CollectionNameInvalid, ValueError)
    with pytest.raises(p19_ms.P19CollectionNameInvalid, match="(?i)pattern|match|invalid|does not match"):
        p19_ms.validate_p19_collection_name(bad_name)
    with pytest.raises(TypeError):
        p19_ms.validate_p19_collection_name(None)   # 异种异常：不被收紧类型捕获


def test_p19_index_name_tightened_type_discriminates():
    """W2Fε 新红钉：validate_p19_index_name 收紧为 P19IndexNameInvalid 同款判别。"""
    _require_p19_ms()
    assert issubclass(p19_ms.P19IndexNameInvalid, ValueError)
    with pytest.raises(p19_ms.P19IndexNameInvalid, match="(?i)pattern|match|invalid|does not match"):
        p19_ms.validate_p19_index_name(
            "p18-batch-uat123456-news-dedup-items-v1-2026.09.26")
    with pytest.raises(TypeError):
        p19_ms.validate_p19_index_name(None)


# ---------- 批复标尺 §3：隔离集合八字段 Schema + 保留集合零触碰 ----------

def test_uat_collection_eight_field_schema_and_preserved_untouched(uat_env):
    """批复 §3 + 公共标尺 2/3：八字段 Schema（无 dynamic/正文/secret）+ 保留集合零触碰。

    真机演示：describe_collection 字段集恰为八字段（含主键 vector_id、dim=空间维度）；
    enable_dynamic_field=False、auto_id=False；集合名以 space_id 收尾（空间标识）；
    日分区 bd_20260926 就位；跑中 list_collections 非 p19 部分与基线恒等（保留集合
    零触碰的跑中执法，跑后严格 diff 由 fixture 析构兜底）。
    """
    milvus = uat_env["milvus"]
    config = uat_env["config"]
    description = milvus.describe_collection(collection_name=config.collection_name)
    fields = {field["name"] for field in description["fields"]}
    assert fields == {
        "vector_id", "record_id", "scope_id", "business_date", "arrival_seq",
        "embedding_space_id", "chunk_id", "embedding",
    }                                                            # 恰八字段
    assert "text" not in fields and "secret" not in fields       # 无正文/secret
    assert description["enable_dynamic_field"] is False          # 无 dynamic
    assert description["auto_id"] is False                       # 主键 P19 构造
    dims = {field["params"].get("dim") for field in description["fields"]
            if field["name"] == "embedding"}
    assert dims == {config.space.dimension}                      # 维度=空间维度（INV-4）
    assert config.collection_name.endswith(config.space.space_id)  # §2.1 空间标识
    partition = "bd_" + BUSINESS_DATE_STR.replace("-", "")
    assert partition in milvus.list_partitions(collection_name=config.collection_name)

    # 跑中保留集合零触碰（非 p19_ 前缀部分与基线恒等）
    now = sorted(milvus.list_collections())
    baseline = uat_env["cols_before"]
    assert [c for c in now if not c.startswith("p19_")] == [
        c for c in baseline if not c.startswith("p19_")
    ], "跑中非 p19 集合与基线不一致（越界硬停）"


# ---------- 批复标尺 §5/INV-1：双写顺序 + Strong 可见性 + 写确认后才 CAS ready ----------

def test_uat_upsert_strong_visibility_then_cas_ready(uat_env):
    """批复 §5/INV-1：主记录先落 → upsert 写确认 → Strong search 可见 → 才 CAS ready。

    真机演示（确定性 mock 向量，INV-7）：
    (i) 顺序执法——ES 主记录缺失时 upsert_chunks 拒绝（VectorWriteUnknown），
        Milvus 侧零写入（双写顺序：主记录先落 ES）；
    (ii) 创建者写 vector_state=pending（05:28 修订 1）后 upsert 两 chunk，
        逐行 upsert_count==1 + Strong get 回读逐字段相等（写确认，约束 c 类比）；
    (iii) upsert 确认后、CAS 前：Strong 一致 search 立即可见（不等 flush），
        且 ES vector_state 仍是 pending——证明"写确认后才 CAS ready"的次序；
    (iv) CAS ready 后 ES 实时 GET 回读 vector_state=ready（约束 d 类比）。
    """
    store = uat_env["store"]
    es = uat_env["es"]
    config = uat_env["config"]
    rid = _rid("s3")
    scope = _scope("s3")
    chunks = [(0, _mock_embedding("s3-a")), (1, _mock_embedding("s3-b"))]

    # (i) 顺序执法：主记录未落 → 拒绝且零写入
    with pytest.raises(p19_ms.VectorWriteUnknown, match="(?i)absent|authority"):
        store.upsert_chunks(record_id=rid, chunks=chunks, scope_id=scope,
                            arrival_seq=71)
    assert store.get_vector_row(f"{rid}_0") is None              # Milvus 零写入

    # (ii) 创建者落主记录（vector_state=pending）→ upsert 写确认
    es.index(index=config.items_index, id=rid,
             document=_main_record_shell(rid, scope_id=scope, arrival_seq=71),
             op_type="create", refresh="wait_for")
    report = store.upsert_chunks(record_id=rid, chunks=chunks, scope_id=scope,
                                 arrival_seq=71)
    assert report == [(f"{rid}_0", "created"), (f"{rid}_1", "created")]
    row0 = store.get_vector_row(f"{rid}_0")
    assert row0 is not None and row0["record_id"] == rid
    assert row0["embedding_space_id"] == config.space.space_id   # 防混空间随行进集合

    # (iii) Strong 一致立即可见 + CAS 前 vector_state 仍 pending（次序断言）
    hits = store.search_strong(query_vector=chunks[0][1], scope_id=scope,
                               business_date=BUSINESS_DATE_STR)
    own = [hit for hit in hits if hit["record_id"] == rid]
    assert {hit["chunk_id"] for hit in own} == {0, 1}            # 双 chunk 皆可见
    assert max(hit["score"] for hit in own) > 0.99               # 自匹配 COSINE≈1
    assert store.get_vector_state(rid) == "pending"              # INV-1：未确认前不 ready

    # (iv) 写确认后才 CAS ready；ES 实时回读
    store.cas_vector_state(rid, to="ready")
    assert store.get_vector_state(rid) == "ready"


# ---------- 批复标尺 INV-6：vector_state 翻转独占权 + 非法翻转拒绝 ----------

def test_uat_vector_state_transitions_pending_ready_failed(uat_env):
    """批复 INV-6：P19 仅做 pending→ready/failed 翻转；非法翻转拒绝且零写入。

    真机演示：
    - 记录 A：pending→ready 成功；随后 ready→pending（目标态词表外）与
      ready→failed（源态非 pending）均拒，回读仍 ready、_seq_no 未动（拒绝零写入）；
    - 记录 B：pending→failed 成功；failed→ready 拒（一旦失败不可自愈为 ready，
      对拍单元层 test_mark_vector_state_failed_to_ready_rejected）；
    - to 词表外（如 "unknown"）一律拒；
    - INV-6 独占权：本用例中 pending 仅由"创建者"直写（_main_record_shell 模拟），
      P19 仓储无任何写 pending 的路径（词表即结构性防线）。
    """
    store = uat_env["store"]
    es = uat_env["es"]
    config = uat_env["config"]
    rid_a = _rid("s4a")
    rid_b = _rid("s4b")
    scope = _scope("s4")
    for rid, seq in ((rid_a, 81), (rid_b, 82)):
        es.index(index=config.items_index, id=rid,
                 document=_main_record_shell(rid, scope_id=scope, arrival_seq=seq),
                 op_type="create", refresh="wait_for")
        assert store.get_vector_state(rid) == "pending"          # 创建者写入

    # 记录 A：pending→ready
    store.cas_vector_state(rid_a, to="ready")
    assert store.get_vector_state(rid_a) == "ready"
    seq_ready = store.es_authority_source(rid_a)["seq_no"]
    with pytest.raises(p19_ms.VectorStateTransitionError):
        store.cas_vector_state(rid_a, to="pending")              # ready→pending 拒
    with pytest.raises(p19_ms.VectorStateTransitionError):
        store.cas_vector_state(rid_a, to="failed")               # 源态非 pending 拒
    with pytest.raises(p19_ms.VectorStateTransitionError):
        store.cas_vector_state(rid_a, to="unknown")              # 词表外拒
    after = store.es_authority_source(rid_a)
    assert after["source"]["vector_state"] == "ready"            # 拒绝后状态不动
    assert after["seq_no"] == seq_ready                          # 拒绝零写入

    # 记录 B：pending→failed；failed→ready 拒
    store.cas_vector_state(rid_b, to="failed")
    assert store.get_vector_state(rid_b) == "failed"
    with pytest.raises(p19_ms.VectorStateTransitionError,
                       match="(?i)pending|transition"):
        store.cas_vector_state(rid_b, to="ready")
    assert store.get_vector_state(rid_b) == "failed"


# ---------- 批复标尺 §6/INV-5：孤儿向量丢弃 + 记孔洞 + 不声称完整 ----------

def test_uat_orphan_vector_discarded_hole_recorded_incomplete(uat_env):
    """批复 §6/INV-5：Milvus 命中但 ES 主记录缺失/不匹配 → 丢弃并记孔洞，
    不声称完整（RECALL_INCOMPLETE 主原因）。

    真机演示两类孤儿（孔洞台账落 p19 control 索引，create 幂等）：
    - 孤儿甲（ES_MISSING）：创建者落 pending 主记录 → upsert → ES 文档被删
      （模拟主记录缺失）；Strong search 仍命中（Milvus 侧残留），权威回查为 None
      → 候选丢弃 + 记孔洞；
    - 孤儿乙（AUTHORITY_MISMATCH）：落 pending → upsert → ES 文档被改写为
      异 arrival_seq（身份不匹配）→ 同样丢弃 + 记孔洞；
    - 孔洞非空 → recall_completeness() = (False, "RECALL_INCOMPLETE")——
      不缩小 selected 掩盖、不声称完整；孔洞重记幂等（同 hole_id 不重复）。
    """
    store = uat_env["store"]
    es = uat_env["es"]
    config = uat_env["config"]
    scope = _scope("s5")
    rid_a = _rid("s5a")
    rid_b = _rid("s5b")
    es.index(index=config.items_index, id=rid_a,
             document=_main_record_shell(rid_a, scope_id=scope, arrival_seq=91),
             op_type="create", refresh="wait_for")
    es.index(index=config.items_index, id=rid_b,
             document=_main_record_shell(rid_b, scope_id=scope, arrival_seq=92),
             op_type="create", refresh="wait_for")
    store.upsert_chunks(record_id=rid_a, scope_id=scope, arrival_seq=91,
                        chunks=[(0, _mock_embedding("s5-a"))])
    store.upsert_chunks(record_id=rid_b, scope_id=scope, arrival_seq=92,
                        chunks=[(0, _mock_embedding("s5-b"))])

    # 制造孤儿：甲删文档（缺失）；乙改写 arrival_seq（不匹配）
    es.delete(index=config.items_index, id=rid_a, refresh="wait_for")
    es.index(index=config.items_index, id=rid_b,
             document=_main_record_shell(rid_b, scope_id=scope, arrival_seq=999),
             refresh="wait_for")

    hits = store.search_strong(query_vector=_mock_embedding("s5-a"),
                               scope_id=scope, business_date=BUSINESS_DATE_STR)
    assert {hit["record_id"] for hit in hits} >= {rid_a, rid_b}  # Milvus 侧命中仍在
    kept, holes = store.filter_hits_by_es_authority(
        hits, scope_id=scope, business_date=BUSINESS_DATE_STR,
    )
    assert kept == []                                            # 孤儿一律丢弃
    hole_reasons = {hole["record_id"]: hole["reason"] for hole in holes}
    assert hole_reasons[rid_a] == "ES_MISSING"
    assert hole_reasons[rid_b] == "AUTHORITY_MISMATCH"

    complete, reason = store.recall_completeness()
    assert complete is False and reason == "RECALL_INCOMPLETE"   # 不声称完整（INV-5）

    # 孔洞台账幂等：重记同 hole_id 不重复
    before = sorted(h["hole_id"] for h in store.holes())
    store.filter_hits_by_es_authority(hits, scope_id=scope,
                                      business_date=BUSINESS_DATE_STR)
    after = sorted(h["hole_id"] for h in store.holes())
    assert after == before


# ---------- 挂账 N12：半写补偿/重启恢复（幂等 upsert + 状态回读） ----------

def test_uat_half_write_restart_recovery_idempotent(uat_env):
    """挂账 N12 + INV-2：写入中断点的可重入恢复（幂等 upsert + 状态回读）。

    真机演示：
    (i) 半写现场——主记录 pending + chunk 0 已确认、chunk 1 未写时"崩溃"；
    (ii) 恢复第一遍：状态回读（ES 仍 pending + Strong get 逐字段对拍）→
        chunk 0 重放=reused（零改写）、chunk 1=created → 写确认后 CAS ready；
    (iii) 恢复第二遍（重入）：两 chunk 皆 reused；ready→ready 翻转被拒
        （恢复逻辑据此判"已完成"零动作）；Milvus 行内容与 ES _seq_no 均不变；
    (iv) INV-2：同 vector_id 携异向量重放 → VectorIdentityConflict，
        Milvus 侧原行不动（不可覆盖规则）。
    """
    store = uat_env["store"]
    es = uat_env["es"]
    config = uat_env["config"]
    scope = _scope("s6")
    rid = _rid("s6")
    chunks = [(0, _mock_embedding("s6-a")), (1, _mock_embedding("s6-b"))]
    es.index(index=config.items_index, id=rid,
             document=_main_record_shell(rid, scope_id=scope, arrival_seq=95),
             op_type="create", refresh="wait_for")

    # (i) 半写：仅 chunk 0 落库即"崩溃"
    first = store.upsert_chunks(record_id=rid, chunks=chunks[:1], scope_id=scope,
                                arrival_seq=95)
    assert first == [(f"{rid}_0", "created")]
    assert store.get_vector_row(f"{rid}_1") is None              # 中断点
    assert store.get_vector_state(rid) == "pending"

    # (ii) 恢复第一遍：幂等重放 + 补写 + 确认后 CAS ready
    second = store.upsert_chunks(record_id=rid, chunks=chunks, scope_id=scope,
                                 arrival_seq=95)
    assert second == [(f"{rid}_0", "reused"), (f"{rid}_1", "created")]
    store.cas_vector_state(rid, to="ready")
    assert store.get_vector_state(rid) == "ready"

    # (iii) 恢复第二遍：重入零动作
    third = store.upsert_chunks(record_id=rid, chunks=chunks, scope_id=scope,
                                arrival_seq=95)
    assert third == [(f"{rid}_0", "reused"), (f"{rid}_1", "reused")]
    seq_done = store.es_authority_source(rid)["seq_no"]
    with pytest.raises(p19_ms.VectorStateTransitionError):
        store.cas_vector_state(rid, to="ready")                  # 已完成 → 拒（零动作判据）
    assert store.es_authority_source(rid)["seq_no"] == seq_done  # ES 侧零写入
    row0 = store.get_vector_row(f"{rid}_0")
    assert row0["embedding"] == pytest.approx(chunks[0][1])      # Milvus 侧内容不动

    # (iv) INV-2：同主键异内容重放 → 冲突且原行不动
    tampered = [(0, _mock_embedding("s6-WRONG"))]
    with pytest.raises(p19_ms.VectorIdentityConflict):
        store.upsert_chunks(record_id=rid, chunks=tampered, scope_id=scope,
                            arrival_seq=95)
    assert store.get_vector_row(f"{rid}_0")["embedding"] == row0["embedding"]


# ---------- 公共标尺 4：清场 dry-run 先行 + 仅 drop 本 run 前缀 ----------

def test_uat_cleanup_dry_run_then_drop_run_prefix_only(uat_env):
    """公共标尺 4：dry-run 先行 + 仅 drop 本 run 前缀集合 + 保留集合永不在清单。

    真机演示：临时 scratch 集合（同 run 前缀、异 space 摘要占位）→ cleanup_plan()
    dry-run 必含 scratch 与本 run 主集合、必不含任何保留集合 → drop scratch 后
    plan 收缩且主集合仍在；ES 侧 items/control 亦在 plan。全量清场执行与
    dry-run==executed 一致性由 `uat_env` fixture 析构执法。
    """
    milvus = uat_env["milvus"]
    store = uat_env["store"]
    config = uat_env["config"]
    from news_flash_dedup.recall.vector_store import (
        vector_index_params, vector_schema,
    )

    scratch = f"p19_{config.run_uuid}_s_" + "0" * 40             # 合法形态占位摘要
    milvus.create_collection(collection_name=scratch,
                             schema=vector_schema(config.space),
                             index_params=vector_index_params())
    assert milvus.has_collection(collection_name=scratch) is True

    plan_with = store.cleanup_plan()                             # dry-run 先行
    assert scratch in plan_with["collections"]
    assert config.collection_name in plan_with["collections"]
    assert config.items_index in plan_with["indices"]
    assert config.control_index in plan_with["indices"]
    for preserved in PRESERVED_COLLECTIONS:
        assert preserved not in plan_with["collections"]         # 保留集合永不入场

    milvus.drop_collection(collection_name=scratch)              # 仅 drop 本 run 前缀对象
    assert milvus.has_collection(collection_name=scratch) is False
    plan_without = store.cleanup_plan()
    assert scratch not in plan_without["collections"]
    assert config.collection_name in plan_without["collections"]


# ---------- 公共标尺 3：2 保留集合零触碰 + 无 p19 stray ----------

def test_p19_does_not_touch_preserved_collections(uat_env):
    """公共标尺 3：2 保留集合零触碰、无 p19 stray。

    跑前/跑后严格 diff 由 `uat_cluster` fixture 析构执法（在 uat_env 清场之后）；
    此处跑中显式比对：非 p19_ 集合与基线恒等；p19_ 前缀集合恰为本 run 主集合
    （无跨 run stray）；已拍基线 JSON 的两个保留集合在跑前实况中俱在。
    """
    milvus = uat_env["milvus"]
    config = uat_env["config"]
    baseline = uat_env["cols_before"]
    now = sorted(milvus.list_collections())
    assert [c for c in now if not c.startswith("p19_")] == [
        c for c in baseline if not c.startswith("p19_")
    ], "跑中非 p19 集合与基线不一致（越界硬停）"
    assert [c for c in now if c.startswith("p19_")] == [config.collection_name], (
        "存在本 run 之外的 p19 stray 集合"
    )
    if BASELINE_JSON.exists():
        recorded = set(json.loads(BASELINE_JSON.read_text(encoding="utf-8")))
        assert recorded == set(PRESERVED_COLLECTIONS)            # 已拍基线自洽
        assert recorded <= set(baseline)                         # 保留集合实况俱在
