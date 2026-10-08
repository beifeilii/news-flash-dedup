r"""B6/E3 Q4：查询/就绪端点真 ES UAT（B4 移交件，P21 定稿 §九-Q4/L291）。

闸（L291，R174 定名）：`P21Q_CONFIRM_UAT=1`+`DEPLOY_ENV=test`+`TEST_ES_*` 就位
+无 `PROD_ES_*` 泄漏+隔离前缀 `p01-batch-p21q-<run>-`+保留索引快照 diff+
R191 删除冻结（零删除零清场；本 run 对象原地封存只读登记
log\temp\b6-created-objects.json 呈主窗口补登白名单——L291"dry-run 清场审计"
为 R191 前口径，冻结令覆盖为只读登记）。

场景（L291 逐项）：
①物化前查询（pending 投影）②物化后未终态③终态五字段透传与回调 resultJson
同形对拍（callback_body 交叉核对，11 §4.1 L148）④假 404 读序真 ES 复现
（清槽窗口注入：映射滞后 pending 投影；真 404 两键壳）⑤读错误注入（断集群：
QueryReadError 形态→503（spec 形，api/query.py:83-85 钉面）；传输层
BatchReadError 裸冒泡形态→实测 500/未包装——P21"断集群→503"在该形态下
不成立，如实登记呈 B6′）⑥/ready 探针拔线（fatal/degraded/全绿分档实证，
经 create_host 组装根+default_probes 真探针）。

跑站命令（log\temp\ 落日志；<HHMM>=footer 分钟级）：

    $env:PYTHONPATH='src'
    $env:DEPLOY_ENV='test'
    $env:P21Q_CONFIRM_UAT='1'
    & '.venv-v1\\Scripts\\python.exe' -m pytest -p no:cacheprovider `
        --basetemp '.\\tmp\\b6-p21q-run' -v tests/integration/test_query_ready_uat.py `
        > ..\\log\\temp\\b6-e3-p21q-run-<HHMM>.txt
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

P21Q_ENV = "P21Q_CONFIRM_UAT"
SCOPE = "default"

_REPO_ROOT = Path(__file__).resolve().parents[2]            # news-flash-dedup
_LOG_TEMP = _REPO_ROOT / "log" / "temp"
_CREATED_OBJECTS = _LOG_TEMP / "b6-created-objects.json"


def _gate_open() -> bool:
    env = os.environ
    return (env.get(P21Q_ENV) == "1" and bool(env.get("TEST_ES_HOST"))
            and not any(k.startswith("PROD_ES_") for k in env))


pytestmark = pytest.mark.skipif(
    not _gate_open(),
    reason=f"查询/就绪端点 UAT 闸未开（需 {P21Q_ENV}=1 + TEST_ES_*，无 PROD_ES_*）",
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
def p21q_client():
    """客户端+基线快照；跑后 diff=本 run 前缀之外零变化（R191 硬停）。"""
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
    assert str(info["version"]["number"]).startswith("8.")
    before = _es_snapshot(client)
    print(f"[B6-P21Q] baseline indices={len(before)}")
    yield client
    after = _es_snapshot(client)
    added = sorted(set(after) - set(before))
    missing = sorted(set(before) - set(after))
    drifted = sorted(n for n in set(before) & set(after)
                     if _is_dedup_family(n) and before[n] != after[n])
    own = [n for n in added if n.startswith("p01-batch-p21q-")]
    dedup_foreign = sorted(n for n in added
                           if _is_dedup_family(n) and n not in own)
    external = sorted(set(added) - set(own) - set(dedup_foreign))
    print(f"[B6-P21Q] snapshot diff added={added} missing={missing} "
          f"drifted={drifted}")
    print(f"[B6-P21Q] external tenant drift (report only): {external}")
    assert not missing, f"保留索引被删（R191 硬停）：{missing}"
    assert not drifted, f"dedup 保留族文档数被触碰（硬停）：{drifted}"
    assert not dedup_foreign, f"白名单外新增 dedup 族索引（越界硬停）：{dedup_foreign}"
    client.close()


@pytest.fixture(scope="module")
def p21q_env(p21q_client):
    """隔离前缀三件套（control/requests/items 日索引）+受理协调器。"""
    from news_flash_dedup.admission import BUSINESS_ZONE
    from news_flash_dedup.batch_admission import BatchAdmissionCoordinator, BatchLimits
    from news_flash_dedup.batch_es_store import ElasticsearchBatchStore
    from news_flash_dedup.es_admission_schema import (
        control_mapping,
        item_mapping,
        request_mapping,
    )
    from news_flash_dedup.product.task_query import TaskQueryPort

    client = p21q_client
    run = "p21q" + datetime.now(timezone.utc).strftime("%H%M%S%f")   # W-R3b 秒级→微秒
    day = datetime.now(BUSINESS_ZONE).date().isoformat()
    prefix = f"p01-batch-p21q-{run}-"
    indices = {
        f"{prefix}news-dedup-control-v1": control_mapping(),
        f"{prefix}news-dedup-requests-v1": request_mapping(),
        f"{prefix}news-dedup-items-v1-{day.replace('-', '.')}": item_mapping(),
    }
    for name, schema in indices.items():
        assert not bool(client.indices.exists(index=name))
        client.indices.create(index=name, body=schema)
    store = ElasticsearchBatchStore(client, index_prefix=prefix)
    coordinator = BatchAdmissionCoordinator(
        store, owner_id=f"b6-p21q-{run}", owner_isolated=lambda: True,
        limits=BatchLimits(), clock=_now)
    query_port = TaskQueryPort(store, scope_id=SCOPE, clock=_now)
    print(f"[B6-P21Q] p01 family: {sorted(indices)}")
    _register_objects([
        {"kind": "es_index", "name": name, "run": run,
         "created_by": "tests/integration/test_query_ready_uat.py",
         "born": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        for name in sorted(indices)
    ])
    yield {"run": run, "day": day, "prefix": prefix, "store": store,
           "coordinator": coordinator, "query_port": query_port,
           "client": client}
    print(f"[B6-P21Q seal] R191 删除冻结：{sorted(indices)} 原地封存（不删，"
          "已登记白名单补充档）")


def _accept(env, task_id: str, text: str):
    from news_flash_dedup.admission import AdmissionRequest

    request = AdmissionRequest(
        scope_id=SCOPE, request_id=task_id, item_id=task_id, text=text,
        received_at=_now(), schema_version="v1", pipeline_version="dedup_v1",
        embedding_space_id="space-p21q", delivery_route_ref="route-p21q",
        trace_id=f"trace-{task_id}")
    return env["coordinator"].accept_batch([request])[0]


# ---------- 场景①：物化前查询（pending 投影） ----------

def test_1_pending_projection_before_materialization(p21q_env):
    receipt = _accept(p21q_env, "9-1", "央行开展中期借贷便利操作维护流动性合理充裕。")
    assert receipt.arrival_seq == 1
    view = p21q_env["query_port"].lookup("9-1")
    assert view is not None, "物化前 pending 窗口查询不得 404（假 404 防线）"
    body = view.to_dict()
    assert body["taskState"] == "accepted"
    assert body["deliveryState"] == "not_ready"
    assert body["result"] is None
    assert body["callbackDelivered"] is None
    assert body["createdAt"] is not None
    assert body["traceId"] == "trace-9-1"
    print("[B6-P21Q] ①物化前 pending 投影过")


# ---------- 场景②：物化后未终态 ----------

def test_2_post_materialization_non_terminal(p21q_env):
    while p21q_env["coordinator"].materialize_oldest() < 1:
        pass
    view = p21q_env["query_port"].lookup("9-1")
    assert view is not None
    body = view.to_dict()
    assert body["taskState"] == "accepted"      # 主记录受理壳（result=None）
    assert body["deliveryState"] == "not_ready"
    assert body["result"] is None
    print("[B6-P21Q] ②物化后未终态投影过（受理壳真形）")


# ---------- 场景③：终态五字段透传 + 回调 resultJson 同形对拍 ----------

def test_3_terminal_projection_and_callback_shape(p21q_env):
    from fastapi.testclient import TestClient

    from news_flash_dedup.api.query import create_query_app
    from news_flash_dedup.batch_admission import _keys
    from news_flash_dedup.admission import REQUEST_INDEX
    from news_flash_dedup.es_admission_schema import day_index
    from news_flash_dedup.product.readiness import ReadinessPort

    env = p21q_env
    store = env["store"]
    request_key = _keys(SCOPE, "9-1", "9-1")[1]
    request_doc = store.get(REQUEST_INDEX, request_key)["source"]
    rid = request_doc["record_id"]
    target = request_doc["target_index"]
    assert target == day_index(env["day"])
    main = store.get(target, rid)
    assert main is not None, "物化后主记录须可见（清槽后 step4 命中）"
    # ---- 终态翻转（测试侧装置：P21 场景③终态读径验证；受理壳 worker 面
    # 终态回写=V1 偏差登记（B4 终报），本件验读径不验回写链） ----
    five_fields = {
        "item_id": "9-1",
        "text": "央行开展中期借贷便利操作维护流动性合理充裕。",
        "decision": "重复",
        "duplicate_ids": ["9-0"],
        "reason": "文本完全一致。",
    }
    callback_body = json.dumps(five_fields, sort_keys=True, ensure_ascii=False)
    terminal = dict(main["source"])
    terminal.update({
        "task_state": "succeeded",
        # 10-07 令甲-i（I-12 转用升级）：重复件创建落点扣留 delivery_state=held
        # ——本件转用为 held 查询道真链 UAT 钉（设计稿 §2 轨①：扣留≠消失，
        # 五字段照投 + callbackDelivered=null + deliveryState=held 直出）
        "delivery_state": "held",
        "callback_attempts": 0,
        "started_at": "2026-09-30T02:00:00.000000Z",
        "completed_at": "2026-09-30T02:00:03.000000Z",
        "result": {"decision": five_fields["decision"],
                   "duplicate_ids": five_fields["duplicate_ids"],
                   "reason": five_fields["reason"]},
        "callback_body": callback_body,     # 同次 CAS 载荷（item_mapping:75 槽位）
    })
    store.replace(target, rid, terminal, main["seq_no"], main["primary_term"])
    env["client"].indices.refresh(index=env["prefix"] + target)
    # ---- 端口层投影 ----
    view = env["query_port"].lookup("9-1")
    assert view is not None
    body = view.to_dict()
    assert body["taskState"] == "succeeded"
    assert body["deliveryState"] == "held"            # 10-07 令甲-i：扣留态透传直出
    assert body["result"] == five_fields, "终态五字段须与 callback_body 同形同值"
    assert body["callbackDelivered"] is None       # held+attempts=0 三态（未投递）
    assert body["startedAt"] == "2026-09-30T02:00:00.000000Z"
    assert body["completedAt"] == "2026-09-30T02:00:03.000000Z"
    # 回调交叉核对：回读 callback_body 与投影五字段逐键同值（11 §4.1 L148）
    reread = store.get(target, rid)["source"]
    assert json.loads(reread["callback_body"]) == body["result"]
    # ---- 端点层（真端口+真 ES） ----
    app = create_query_app(
        query_port=env["query_port"],
        readiness_port=ReadinessPort({}, mode="fixed"),
        status_authenticated=lambda request: True)
    with TestClient(app) as http:
        response = http.get("/v1/api/task/9-1")
    assert response.status_code == 200
    outer = response.json()
    assert set(outer) == {"taskId", "requestId", "articleId", "traceId",
                          "taskState", "deliveryState", "createdAt",
                          "startedAt", "completedAt", "callbackDelivered",
                          "result", "error"}
    assert outer["result"] == five_fields
    for leaked in ("mode", "diagnostics", "record_id", "recordId",
                   "callback_body", "payload_hash"):
        assert leaked not in outer
    print("[B6-P21Q] ③终态五字段透传+回调同形对拍过（端口+端点双层）")


# ---------- 场景④：假 404 读序真 ES 复现 + 真 404 ----------

def test_4_false_404_window_and_true_404(p21q_env):
    from fastapi.testclient import TestClient

    from news_flash_dedup.api.query import create_query_app
    from news_flash_dedup.product.readiness import ReadinessPort

    env = p21q_env
    # 清槽窗口注入：新批受理但不物化——request 映射未建，pending 在槽
    _accept(env, "9-4", "证监会就程序化交易新规公开征求意见。")
    view = env["query_port"].lookup("9-4")
    assert view is not None, "清槽窗口（映射滞后）须 pending 投影，不得假 404"
    assert view.to_dict()["taskState"] == "accepted"
    # 真 404：全宇宙无此任务
    assert env["query_port"].lookup("8-8") is None
    app = create_query_app(
        query_port=env["query_port"],
        readiness_port=ReadinessPort({}, mode="fixed"),
        status_authenticated=lambda request: True)
    with TestClient(app) as http:
        missing = http.get("/v1/api/task/8-8")
        pending = http.get("/v1/api/task/9-4")
    assert missing.status_code == 404
    shell = missing.json()
    assert set(shell) == {"accepted", "message"}      # 两键壳、无 details
    assert shell["accepted"] is False and "8-8" in shell["message"]
    assert pending.status_code == 200
    assert pending.json()["taskState"] == "accepted"
    print("[B6-P21Q] ④假 404 防线（pending 投影）+真 404 两键壳过")


# ---------- 场景⑤：读错误注入（断集群） ----------

def test_5_read_error_injection(p21q_env):
    from elasticsearch import Elasticsearch
    from fastapi.testclient import TestClient

    from news_flash_dedup.api.query import create_query_app
    from news_flash_dedup.batch_es_store import (
        BatchReadError,
        ElasticsearchBatchStore,
    )
    from news_flash_dedup.product.readiness import ReadinessPort
    from news_flash_dedup.product.task_query import QueryReadError, TaskQueryPort

    env = p21q_env
    # 形态 A（spec 形）：端口抛 QueryReadError → 端点 503（api/query.py:83-85 钉面）
    class _BrokenPort:
        def lookup(self, task_id):
            raise QueryReadError("simulated read failure")

    app = create_query_app(
        query_port=_BrokenPort(),
        readiness_port=ReadinessPort({}, mode="fixed"),
        status_authenticated=lambda request: True)
    with TestClient(app) as http:
        response = http.get("/v1/api/task/9-1")
    assert response.status_code == 503
    assert response.json() == {"accepted": False, "message": "服务暂时不可用",
                               "details": []}
    # 形态 B（传输层真断）：真客户端指向死端口 → BatchReadError 裸冒泡
    dead = Elasticsearch("http://127.0.0.1:9", request_timeout=1, max_retries=0)
    broken_store = ElasticsearchBatchStore(dead, index_prefix=env["prefix"])
    broken_port = TaskQueryPort(broken_store, scope_id=SCOPE, clock=_now)
    with pytest.raises(BatchReadError):
        broken_port.lookup("9-1")
    app_b = create_query_app(
        query_port=broken_port,
        readiness_port=ReadinessPort({}, mode="fixed"),
        status_authenticated=lambda request: True)
    with TestClient(app_b, raise_server_exceptions=False) as http:
        response_b = http.get("/v1/api/task/9-1")
    # 实测登记：BatchReadError 未经 QueryReadError 包装（task_query.py 读序无
    # try 面），端点映射不及 → 500 而非 P21 L291 场景⑤词面"断集群→503"。
    # 如实呈 B6′（不冒充 spec 形成立）。
    assert response_b.status_code == 500, response_b.status_code
    print("[B6-P21Q] ⑤读错误：QueryReadError→503（spec 形）过；"
          "BatchReadError 传输层裸冒泡→500（登记呈 B6′：与场景⑤词面 503 不符）")


# ---------- 场景⑥：/ready 探针拔线（分档实证） ----------

def test_6_ready_probe_ladder(p21q_env):
    from fastapi.testclient import TestClient

    from news_flash_dedup.api.host import create_host
    from news_flash_dedup.recall.models import ChannelResult
    from news_flash_dedup.recall.service import NullVectorSearcher, RecallModeInvalid

    env = p21q_env
    client = env["client"]

    class _Sink:
        def emit(self, kind, payload):
            pass

    class _HealthyVectorSearcher:
        """非 Null 装配（真 searcher 同型鸭式）：就绪面 real=True → 向量无缺口。"""

        def search(self, request, query_vector=None):
            return ChannelResult("embedding", "complete", (), "embedding_v1")

    def _host(**overrides):
        kwargs = dict(
            es_client=client, index_prefix=env["prefix"], scope_id=SCOPE,
            status_authenticated=lambda request: True,
            env={"DEDUP_RECALL_MODE": "shadow"}, clock=_now,
            vector_searcher=_HealthyVectorSearcher(),
            artifact_sink=_Sink(), collector_alive=True,
            delivery_route_configured=True)
        kwargs.update(overrides)
        return create_host(**kwargs)

    # 全绿（shadow+真 searcher 装配面）
    green = _host()
    assert green.mode == "shadow"
    assert green.readiness_port.issues() == (True, [])
    # degraded（Null 过渡态→N20-b 缺口明示码）
    degraded = _host(vector_searcher=NullVectorSearcher())
    assert degraded.readiness_port.issues() == (
        True, ["embedding_space_unconfirmed"])
    # fatal（es_control_read 拔线：死端口客户端）
    from elasticsearch import Elasticsearch

    dead = Elasticsearch("http://127.0.0.1:9", request_timeout=1, max_retries=0)
    fatal = _host(es_client=dead)
    ready, codes = fatal.readiness_port.issues()
    assert ready is False and "es_control_read_failed" in codes
    # fatal（日索引缺失→五通道链路码）
    from news_flash_dedup.api.host import default_probes

    probes = default_probes(
        es_client=client, index_prefix=env["prefix"], mode="shadow", clock=_now,
        vector_searcher=_HealthyVectorSearcher(), collector_alive=True,
        delivery_route_configured=True, business_date="2000-01-02")
    from news_flash_dedup.product.readiness import ReadinessPort

    ready2, codes2 = ReadinessPort(probes, mode="shadow").issues()
    assert ready2 is False
    assert any(code.startswith("recall_chain_unavailable:") for code in codes2)
    # fixed 态（无召回链探针面）
    fixed = create_host(
        es_client=client, index_prefix=env["prefix"], scope_id=SCOPE,
        status_authenticated=lambda request: True, env={}, clock=_now,
        collector_alive=True, delivery_route_configured=True)
    assert fixed.mode == "fixed"
    assert fixed.readiness_port.issues() == (True, [])
    # 模式闸 fail-closed
    with pytest.raises(RecallModeInvalid):
        create_host(
            es_client=client, index_prefix=env["prefix"], scope_id=SCOPE,
            status_authenticated=lambda request: True,
            env={"DEDUP_RECALL_MODE": "bogus"}, clock=_now,
            artifact_sink=_Sink())
    # 端点层三态 + 鉴权 403 + 422
    with TestClient(green.query_app) as http:
        ok = http.get("/ready")
        bad = http.get("/v1/api/task/not-a-task")
    assert ok.status_code == 200
    assert ok.json() == {"ready": True, "issues": [], "mode": "shadow"}
    assert bad.status_code == 422
    forbidden = _host(status_authenticated=lambda request: False)
    with TestClient(forbidden.query_app) as http:
        denied = http.get("/ready")
    assert denied.status_code == 403
    assert denied.json() == {"error": "admin required"}
    with TestClient(degraded.query_app) as http:
        deg = http.get("/ready")
    assert deg.status_code == 200
    assert deg.json()["issues"] == ["embedding_space_unconfirmed"]
    with TestClient(fatal.query_app) as http:
        fat = http.get("/ready")
    assert fat.status_code == 503
    assert fat.json()["ready"] is False
    print("[B6-P21Q] ⑥/ready 分档：全绿/degraded/fatal(es 拔线+日索引缺失)/"
          "fixed/模式闸 fail-closed/403/422 全过")
