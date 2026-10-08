"""P20 真 ES 回调投递 UAT（05:29 批复标尺 + 修订 1 + 公共标尺 · T4 站）。

起草本稿的纪律背景：
- 本稿由 T4 子窗口起草；src 产品代码（delivery/es_store.py 等）由主窗口亲笔
  （决策日志 D7 同例）。P20 真 ES 投递仓储未落 src 前（挂账 N13，T4 新立），依赖
  接口的用例一律 pytest.skip 并注明"待主窗口接线批复"；草案全文见
  `log/temp/p20-uat-wiring-proposal.md`。
- 单元层已封（tests/unit/test_p20_implementation.py）：fake 路径 + route_ref 默认
  127.0.0.1:8080；**真 ES delivery store + 真 loopback HTTP 接收在本稿是首次**。
- 修订 1（05:29 批复，已落稿）：UAT route_ref 必须指向 pytest 内建 127.0.0.1
  ephemeral HTTP 接收器；断言接收载荷与冻结 callback_body 逐字节一致 +
  payload_hash 吻合 + 仅 POST；**禁止**任何真实业务回调 URL 出现在测试配置
  （含注释示例一律用 example.invalid 占位）。
  ★ 偏差注记：设计 §8.1 接收器草稿假定 aiohttp（web.TCPSite）；实测 .venv-v1
  **无 aiohttp** 且全仓 tests/src 无 TCPSite 用例——本稿接收器改以 stdlib
  `http.server.ThreadingHTTPServer`（127.0.0.1:0 ephemeral）实现，语义等效、
  零新依赖（见建议书矛盾点 #1，待主窗口裁定追认）。
- 真 ES 现实约束（T1/T2/T3 已踩出，本稿全量内化）：
  a) 集群 action.auto_create_index=-* → 仓储构造时显式幂等建索引（19:16 先例）；
  b) "不存在才创建" = op_type=create；(if_seq_no=0, if_primary_term=0) 被 ES 8 拒；
  c) ES _seq_no 0 基；
  d) 状态翻转以实时 GET 回读为准；
  e) .env 值带单引号；TEST_ES_PASS/TEST_ES_PASSWORD 双命名同值导出（strip 仅兜底）；
  f) ES 纯 HTTP（TEST_ES_SCHEME=http）。

公共标尺（目标文档 §三.5 + 05:29 批复 §8）：
1. 闸门 `P20_CONFIRM_UAT=1` 缺失即 skip/拒启；仅 TEST_* 凭据；PROD_* 出现即拒启；
2. 隔离前缀 `p20-batch-<run_uuid>-news-dedup-(items|control)-v1-YYYY.MM.DD`
   （与设计 §8 "callbacks" 单索引字面偏差见建议书矛盾点 #6）；
3. 跑前 `_cat/indices` 快照、跑后严格 diff 证明 192 保留索引零触碰、无 stray；
4. dry-run 先行 + 清场（仅删本 run 前缀；仓储自带 cleanup——lifecycle.execute_cleanup
   硬正则只收 p01-batch 前缀 + (items|audits)，结构性不适用于 p20-batch，见建议书
   矛盾点 #6）；UAT 日志全文落盘；
5. 发现越界立即全停（fixture 快照 diff 即硬停断言）。

跑站命令（主窗口执行；footer 分钟级向下取整）：

    $env:PYTHONPATH='src'
    $env:PYTHONIOENCODING='utf-8'
    $env:PYTHONDONTWRITEBYTECODE='1'
    $env:DEPLOY_ENV='test'
    $env:P20_CONFIRM_UAT='1'
    $env:TEST_ES_PASSWORD=<去引号密码>   # 同时导出 TEST_ES_PASS 同值
    & '.venv-v1\\Scripts\\python.exe' -m pytest -p no:cacheprovider `
        --basetemp '.\\tmp\\p20-uat-run' -v tests/integration/test_p20_uat.py `
        > ..\\log\\temp\\p20-uat-run-<HHMM>.txt
"""

from __future__ import annotations

import hashlib
import http.server
import json
import os
import socket
import threading
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from news_flash_dedup.delivery import (
    P20ReceiveError,
    advance_backoff,
    compute_attempt_id,
    exhaust_round,
    expire_record,
    persist_next_delivery,
    receive,
    scan_pending,
    send_callback,
)
from news_flash_dedup.es_client import (
    APPROVED_UAT_HOST,
    APPROVED_UAT_PORT,
    APPROVED_UAT_SCHEME,
)

# ---------- 受守卫的产品接口导入（挂账 N13：delivery/es_store.py 未落 src 时整组 skip） ----------

try:
    from news_flash_dedup.delivery import es_store as p20_ds

    _P20_DS_IMPORT_ERROR = None
except ImportError as exc:  # delivery/es_store.py 未落（T4 主窗口亲笔）
    p20_ds = None
    _P20_DS_IMPORT_ERROR = exc


P20_UAT_ENV = "P20_CONFIRM_UAT"
BUSINESS_DATE_STR = "2026-09-26"
BUSINESS_DATE = date(2026, 9, 26)

# 修订 1「不发明头」白名单：urllib/http.client 协议默认头 + 发送器自设的
# Content-Type（sender.py 唯一自设头）。逐请求小写比对；任何 X-* 自定义头即越界。
_ALLOWED_REQUEST_HEADERS = frozenset({
    "host", "accept-encoding", "content-length", "content-type",
    "user-agent", "connection",
})


# ---------- 闸门（公共标尺 1） ----------

def _gate_open(env) -> bool:
    """harness 级闸门：P20_CONFIRM_UAT=1 + TEST_ES_HOST 就位 + 无 PROD_* 泄漏。"""
    if env.get(P20_UAT_ENV) != "1":
        return False
    if not env.get("TEST_ES_HOST"):
        return False
    if any(k.startswith("PROD_") for k in env):
        return False
    return True


def _uat_available() -> bool:
    return _gate_open(os.environ)


pytestmark = pytest.mark.skipif(
    not _uat_available(),
    reason=f"P20 UAT gate not open (need {P20_UAT_ENV}=1 + TEST_ES_*, no PROD_*)",
)


def _require_p20_ds() -> None:
    """P20 真 ES 投递仓储未落 src 时 skip（挂账 N13；待主窗口接线批复）。"""
    if p20_ds is None:
        pytest.skip(
            "P20 真 ES 投递仓储 delivery/es_store.py 未落 src（挂账 N13；草案全文见 "
            "log/temp/p20-uat-wiring-proposal.md）——待主窗口接线批复"
        )


# ---------- 修订 1：127.0.0.1 ephemeral 接收器（stdlib 形态，建议书矛盾点 #1） ----------

class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    """UAT 接收器 handler：全方法记录（仅 POST 校的反面锚），响应由 server 属性配置。"""

    def _record_and_respond(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        self.server.received_requests.append({
            "method": self.command,
            "path": self.path,
            "headers": dict(self.headers.items()),
            "body": body,
        })
        payload = self.server.response_body.encode("utf-8")
        self.send_response(self.server.response_status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_POST = _record_and_respond
    do_GET = _record_and_respond
    do_PUT = _record_and_respond
    do_DELETE = _record_and_respond

    def log_message(self, format, *args):  # noqa: A002 — 静默访问日志，UAT 日志只留断言
        pass


@pytest.fixture
def callback_receiver():
    """修订 1 接收器：ThreadingHTTPServer 绑 127.0.0.1:0（构造即 ephemeral bind）。

    yield SimpleNamespace(url / received / set_response)。route_ref 唯一合法来源；
    任何真实业务回调 URL（example.invalid 占位除外）禁止出现在本稿。
    """
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _CallbackHandler)
    server.received_requests = []
    server.response_status = 200
    server.response_body = '{"succeed": true}'
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    yield SimpleNamespace(
        url=f"http://127.0.0.1:{port}/callback",
        received=server.received_requests,
        set_response=lambda status, body: (
            setattr(server, "response_status", status),
            setattr(server, "response_body", body),
        ),
    )
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def _closed_port_url() -> str:
    """取一个「曾 ephemeral、当前无监听」的 127.0.0.1 端口（未知结果场景用）。

    bind 后立即 close：连接必被拒（OSError/URLError）。存在理论竞态（端口被
    旁人抢走），UAT 单机环境下可接受并在此注明。
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return f"http://127.0.0.1:{port}/callback"


# ---------- fixtures：客户端 + 快照/清场/前缀隔离 ----------

@pytest.fixture(scope="module")
def uat_client():
    """公共标尺 3：客户端 + 192 保留索引跑前快照；跑后严格 diff（零触碰/无 stray 硬停）。"""
    client = _build_es_client()
    snapshot_before = _snapshot_indices(client)
    print(f"[P20-UAT] baseline indices: {len(snapshot_before)}")
    yield {"client": client, "snapshot_before": snapshot_before}
    snapshot_after = _snapshot_indices(client)
    added = sorted(set(snapshot_after) - set(snapshot_before))
    missing = sorted(set(snapshot_before) - set(snapshot_after))
    print(f"[P20-UAT] snapshot diff added={added} missing={missing}")
    assert not added and not missing, (
        f"192 保留索引被改动或 stray 残留（越界硬停）！added={added} missing={missing}"
    )


@pytest.fixture(scope="module")
def uat_env(uat_client):
    """P20 隔离环境：run_uuid + RealESP20Config + 真 ES 投递仓储；跑后 dry-run 先行清场。

    清场范围 = 本 run 前缀 `p20-batch-<run_uuid>-`（与 T1/T2/T3 同款的更窄 run
    作用域，等效且更保守）；fixture 析构顺序保证：先清 P20 隔离索引，后做 192
    严格 diff。
    """
    _require_p20_ds()
    run_uuid = "uat" + datetime.now(timezone.utc).strftime("%H%M%S%f")  # W-R3b 秒级→微秒
    config = p20_ds.RealESP20Config(run_uuid=run_uuid, business_date=BUSINESS_DATE)
    client = uat_client["client"]
    # RealESP20Store 构造即：闸门断言 + 显式幂等建索引（auto_create_index=-* 集群约束 a）
    store = p20_ds.RealESP20Store(client, config)
    print(
        "[P20-UAT] isolated indices: "
        f"{config.items_index} / {config.control_index}"
    )
    yield {
        "config": config,
        "client": client,
        "store": store,
        "run_uuid": run_uuid,
    }
    # 清场（公共标尺 4）：dry-run 先行，仅删本 run 前缀
    plan = store.cleanup_plan()
    if plan:
        print(f"[P20-UAT cleanup dry-run] will delete: {plan}")
    deleted = store.cleanup()
    print(f"[P20-UAT cleanup] deleted: {sorted(deleted)}")
    assert sorted(deleted) == sorted(plan), (
        f"清场清单与 dry-run 不一致：dry-run={plan} deleted={sorted(deleted)}"
    )


def _build_es_client():
    """从 TEST_ES_* 构造客户端（T1/T2 `_build_es_client` 同型，读 TEST_ES_PASSWORD）。

    双命名约定：TEST_ES_PASSWORD 优先、TEST_ES_PASS 兜底（运行环境同值导出）；
    .env 单引号由导出侧剥除（约束 e），此处 strip 仅防 .env 直读场景，不替代导出约定。
    """
    from elasticsearch import Elasticsearch

    host = os.environ.get("TEST_ES_HOST", APPROVED_UAT_HOST)
    port = int(os.environ.get("TEST_ES_PORT", str(APPROVED_UAT_PORT)))
    scheme = os.environ.get("TEST_ES_SCHEME", APPROVED_UAT_SCHEME)  # 纯 HTTP（约束 f）
    user = os.environ.get("TEST_ES_USER", "")
    password = os.environ.get("TEST_ES_PASSWORD") or os.environ.get("TEST_ES_PASS", "")
    if len(password) >= 2 and password.startswith("'") and password.endswith("'"):
        password = password[1:-1]
    return Elasticsearch(
        f"{scheme}://{host}:{port}",
        basic_auth=(user, password),
        verify_certs=False,
        request_timeout=30,
    )


def _snapshot_indices(client) -> list[str]:
    """`_cat/indices` 快照（192 保留索引清单，T2 同型）。"""
    response = client.cat.indices(format="json", h="index")
    return sorted(item.get("index", "") for item in response)


# ---------- 数据构造助手 ----------

def _rid(suffix: str) -> str:
    """确定性 64-hex record_id（按场景后缀隔离，避免同 run 内 create 冲突）。"""
    return hashlib.sha256(f"p20uat-{suffix}".encode("utf-8")).hexdigest()


def _callback_body(item_id: str) -> str:
    """冻结载荷真值（sort_keys 确定性序列化；与 P18 persist 路径同构）。"""
    return json.dumps({
        "item_id": item_id,
        "text": "甲公司完成回购。",
        # 10-07 令甲-i（I-11）：播种翻"不重复"放行件——投递链测试与判定值无耦合
        "decision": "不重复",
        "duplicate_ids": [],
        "reason": "主体与事件一致",
    }, sort_keys=True, ensure_ascii=False)


def _delivery_body(*, record_id: str, route_ref: str, scope_id: str,
                   arrival_seq: int, event_id: str,
                   delivery_state: str = "pending", task_state: str = "succeeded",
                   audit_complete: bool = True, audit_ids=(),
                   round: int = 1, round_attempts: int = 0,
                   callback_attempts: int = 0,
                   deadline_in_hours: float = 24.0,
                   next_delivery_at: str | None = None) -> dict:
    """P20 §3.1 字段集主记录 body（P18 §3.1 字段 + route_ref——矛盾点 #2）。

    P18 落库路径（persist_main_record_es / _record_to_body）均不写 route_ref；
    UAT 播种侧补写（投递侧 CAS 前置依赖此字段，是否回灌 P18 创建路径请主窗口
    裁定——本 UAT 不依赖该回灌）。
    """
    callback_body = _callback_body(f"item-{record_id[:8]}")
    now = datetime.now(timezone.utc)
    completed_at = now.isoformat()
    return {
        "record_id": record_id,
        "item_id": f"item-{record_id[:8]}",
        "text": "甲公司完成回购。",
        "scope_id": scope_id,
        "business_date": BUSINESS_DATE_STR,
        "arrival_seq": arrival_seq,
        "raw_hash": hashlib.sha256(
            f"raw-{record_id}".encode("utf-8")
        ).hexdigest(),
        "pipeline_version": "dedup_v1",
        "fact_artifact_hash": "",
        "vector_state": "pending",                   # P20 不消费，创建者默认值
        "result": json.loads(callback_body),
        "result_item_id": f"item-{record_id[:8]}",
        # 10-07 令甲-i（I-11）：播种翻"不重复"（与 _callback_body 同源一致）
        "result_decision": "不重复",
        "result_duplicate_ids": [],
        "result_reason": "主体与事件一致",
        "task_state": task_state,
        "completed_at": completed_at,
        "result_version": 1,
        "event_id": event_id,
        "audit_ids": list(audit_ids),
        "audit_complete": audit_complete,
        "reason_code": "FACT_EQUIVALENT",
        "callback_body": callback_body,
        "callback_body_hash": hashlib.sha256(
            callback_body.encode("utf-8")
        ).hexdigest(),                               # INV-4（投递侧名 payload_hash）
        "route_ref": route_ref,                      # ★ P18 字段集无此字段（矛盾点 #2）
        "delivery_state": delivery_state,
        "delivery_deadline_at": (
            now + timedelta(hours=deadline_in_hours)
        ).isoformat(),
        "next_delivery_at": next_delivery_at or completed_at,
        "callback_attempts": callback_attempts,
        "round_attempts": round_attempts,
        "round": round,
    }


def _audit_doc(suffix: str, *, current_record_id: str) -> dict:
    """挂账 #7 审计对照文档（落 p20 control 索引 sidecar，P19 孔洞台账先例）。"""
    rid_history = _rid(suffix + "-history")
    return {
        "comparison_id": f"{rid_history}:{current_record_id}",
        "history_record_id": rid_history,
        "current_record_id": current_record_id,
        "history_raw_hash": "h-" + suffix,
        "current_raw_hash": "c-" + suffix,
        "basis": "FACT_EQUIVALENT",
        "field_path": "facts.f1.subject",
        "detail": "主体事件一致",
        "pipeline_version": "dedup_v1",
    }


def _seed(uat_env, body: dict) -> tuple[int, int]:
    """播种主记录（仓储两阶段原语：create(not_ready) 壳 → CAS 到 body 指定态）。"""
    return uat_env["store"].seed_delivery_record(body)


# ---------- 场景 1 · 公共标尺 1：闸门 ----------

def test_p20_uat_harness_gate_requires_confirm_flag():
    """场景 1（闸门）：`P20_CONFIRM_UAT=1` 缺失即 skip/拒启；仅 TEST_*；PROD_* 拒启。

    harness 级恒可跑（不依赖未落 src 的接口）；产品闸 `assert_p20_uat_open`
    在 delivery/es_store.py 落地后由本用例顺带覆盖。
    """
    assert _gate_open({P20_UAT_ENV: "1", "TEST_ES_HOST": "es.example"}) is True
    assert _gate_open({"TEST_ES_HOST": "es.example"}) is False            # 缺闸门
    assert _gate_open({P20_UAT_ENV: "0", "TEST_ES_HOST": "es.example"}) is False
    assert _gate_open({P20_UAT_ENV: "1"}) is False                        # 缺凭据
    assert _gate_open({P20_UAT_ENV: "1", "TEST_ES_HOST": "es.example",
                       "PROD_ES_HOST": "prod.example"}) is False          # PROD 泄漏拒启
    assert _gate_open({P20_UAT_ENV: "1", "TEST_ES_HOST": "es.example",
                       "PROD_CALLBACK_URL": "https://prod.example.invalid/cb"}) is False
    if p20_ds is not None:
        with pytest.raises(p20_ds.P20UATGatesNotOpen):
            p20_ds.assert_p20_uat_open(env={"DEPLOY_ENV": "test"})


# ---------- 公共标尺 2：隔离前缀硬正则 ----------

def test_p20_index_prefix_pattern_enforced():
    """隔离前缀必须严格匹配 p20-batch-<run>-news-dedup-(items|control)-v1-YYYY.MM.DD。

    跨代前缀（p18-/p19-）与无前缀名一律拒收；设计 §8 字面 "callbacks" 索引不在
    硬正则词表内（矛盾点 #6：事实清单/T2-T3 先例取 (items|control) 双索引）。
    """
    _require_p20_ds()
    for kind in ("items", "control"):
        p20_ds.validate_p20_index_name(
            f"p20-batch-uat123-news-dedup-{kind}-v1-2026.09.26"
        )
    with pytest.raises(p20_ds.P20IndexPrefixInvalid, match="(?i)prefix|pattern|match"):
        p20_ds.validate_p20_index_name("news-dedup-items-v1-2026.09.26")   # 缺前缀
    with pytest.raises(p20_ds.P20IndexPrefixInvalid, match="(?i)prefix|pattern|match"):
        p20_ds.validate_p20_index_name(
            "p18-batch-uat123-news-dedup-items-v1-2026.09.26"               # 跨代前缀
        )
    with pytest.raises(p20_ds.P20IndexPrefixInvalid, match="(?i)prefix|pattern|match"):
        p20_ds.validate_p20_index_name(
            "p20-batch-uat123-news-dedup-callbacks-v1-2026.09.26"           # §8 字面（拒）
        )


# ---------- 公共标尺 2 补钉：收紧类型判别（W1Fδ 项2，逐收紧点各 1） ----------

@pytest.mark.parametrize("bad_name", [
    "news-dedup-items-v1-2026.09.26",                        # 收紧点 1：缺前缀
    "p18-batch-uat123-news-dedup-items-v1-2026.09.26",       # 收紧点 2：跨代前缀
    "p20-batch-uat123-news-dedup-callbacks-v1-2026.09.26",   # 收紧点 3：§8 字面
])
def test_p20_index_prefix_tightened_type_discriminates(bad_name):
    """W1Fδ 项2 新红钉：裸 Exception 收紧为 P20IndexPrefixInvalid 的判别实证。

    ① 收紧后原异常仍被捕（语义保持）：拒名仍抛 P20IndexPrefixInvalid
    （ValueError 子类，es_store.validate_p20_index_name 唯一拒名异常类型）；
    ② 放行其他异常（新红钉）：异种异常（非 str 入参 → re.fullmatch 抛
    TypeError）不被 P20IndexPrefixInvalid 兜底，照旧外抛——泛 Exception
    收口已拆除、收紧类型具备判别力的直接证据。
    """
    _require_p20_ds()
    assert issubclass(p20_ds.P20IndexPrefixInvalid, ValueError)
    with pytest.raises(p20_ds.P20IndexPrefixInvalid, match="(?i)prefix|pattern|match"):
        p20_ds.validate_p20_index_name(bad_name)
    with pytest.raises(TypeError):
        p20_ds.validate_p20_index_name(None)   # 异种异常：不被收紧类型捕获


# ---------- 场景 5 锚 · attempt_id 派生（harness 级恒可跑） ----------

def test_p20_attempt_id_derivation_sha256_jcs():
    """attempt_id = SHA256(JCS([event_id, generation]))（§3.2 / 10 §6.5 第 2 步，全 64hex）。

    harness 级恒可跑（已封 compute_attempt_id 直测）；真 ES 侧的租约 attempt_id
    持久化核对见场景 5 用例。

    D25/C-07 行为修正钉值（P2 工程债批次，窗口X 并线移植）：实现改复用
    admission._digest（JCS 单一事实源，separators=(",", ":") 最简分隔符），
    旧 json.dumps 默认分隔符派生值作废；主窗口追裁：10 文规格无截断，
    [:32] 系 delivery 层自造已去除（与 g0_persistence.py:603 全 64hex
    对齐）。下值为现算 admission._digest(["evt-p20uat-anchor", 3])。
    """
    eid, gen = "evt-p20uat-anchor", 3
    assert compute_attempt_id(eid, gen) == (
        "60b35739a38282c2ad50a275e375074f"
        "ba6592b9270478f62abeab03df7624e4")
    assert len(compute_attempt_id(eid, gen)) == 64
    assert compute_attempt_id(eid, 1) != compute_attempt_id(eid, 2)      # 代次隔离


# ---------- 场景 6 锚 · 退避公式边界（harness 级恒可跑） ----------

def test_p20_backoff_formula_bounds():
    """退避公式 min(1s×2^(n-1), 30min) + ≤20% 抖动（§5 / 10 §6.5 第 6 步）。

    harness 级恒可跑；真 ES 侧的持久化 + 重启不重抽见场景 6a 用例。
    """
    first = advance_backoff(round_attempts=1)          # base = 1s × 2^0 = 1s
    assert 0.8 <= first <= 1.2
    capped = advance_backoff(round_attempts=20)        # base 远超 30min → cap 1800s
    # W2Fε 补下界守底：封顶 1800s 叠加 ≤20% 抖动 ⇒ 机制值域 [1440, 2160]；
    # 原单上界钉对"封顶丢失/抖动放大"无红能力（如 base 未封顶取小值仍绿）。
    assert 1800 * 0.8 <= capped <= 1800 * 1.2


# ---------- 场景 2：接收五前置执法（缺一不领取/不发送） ----------

def test_uat_receive_precondition_quintet_enforced(uat_env):
    """场景 2（批复 §3.1 + 挂账核对）：接收五前置缺一即拒，且零写入（seq_no 不动）。

    五前置 = ① delivery_state ∈ {pending, delivering}（sealed receive 执法）；
    ② task_state=succeeded（★仓储侧 update_lease CAS 补齐——sealed 不校，矛盾点 #3）；
    ③ audit_complete=True（★同②）；④ round_attempts < 12（sealed 执法）；
    ⑤ now < delivery_deadline_at（★同②；sealed 无截止检查，矛盾点 #3）。
    每次拒绝后回读：seq_no 未动、delivery_state 未动——领取 CAS 从未生效，
    发送自然无从谈起（send_callback 非 delivering 即拒，单元层已封）。
    """
    store = uat_env["store"]
    scope = "p20uat-s2"

    # ① delivery_state=not_ready（sealed receive 拒：状态词表外）
    rid1 = _rid("s2-not-ready")
    body1 = _delivery_body(record_id=rid1, route_ref="http://127.0.0.1:1/callback",
                           scope_id=scope, arrival_seq=11, event_id="evt-s2-1",
                           delivery_state="not_ready", task_state="accepted")
    seq1, _ = _seed(uat_env, body1)
    with pytest.raises(P20ReceiveError, match="(?i)pending|delivering"):
        receive(store, rid1, payload_hash=body1["callback_body_hash"],
                route_ref=body1["route_ref"], expected_generation=0,
                event_id="evt-s2-1")
    assert store.get_main_record(rid1)["seq_no"] == seq1                 # 零写入

    # ② task_state=failed（sealed 通过 → ★仓储侧拒）
    rid2 = _rid("s2-task-failed")
    body2 = _delivery_body(record_id=rid2, route_ref="http://127.0.0.1:1/callback",
                           scope_id=scope, arrival_seq=12, event_id="evt-s2-2",
                           task_state="failed")
    seq2, _ = _seed(uat_env, body2)
    with pytest.raises(p20_ds.P20ClaimPreconditionError, match="(?i)task_state"):
        receive(store, rid2, payload_hash=body2["callback_body_hash"],
                route_ref=body2["route_ref"], expected_generation=0,
                event_id="evt-s2-2")
    after2 = store.get_main_record(rid2)
    assert after2["seq_no"] == seq2
    assert after2["source"]["delivery_state"] == "pending"               # 未翻 delivering

    # ③ audit_complete=False（★仓储侧拒）
    rid3 = _rid("s2-audit-incomplete")
    body3 = _delivery_body(record_id=rid3, route_ref="http://127.0.0.1:1/callback",
                           scope_id=scope, arrival_seq=13, event_id="evt-s2-3",
                           audit_complete=False)
    seq3, _ = _seed(uat_env, body3)
    with pytest.raises(p20_ds.P20ClaimPreconditionError, match="(?i)audit_complete"):
        receive(store, rid3, payload_hash=body3["callback_body_hash"],
                route_ref=body3["route_ref"], expected_generation=0,
                event_id="evt-s2-3")
    assert store.get_main_record(rid3)["seq_no"] == seq3

    # ④ round_attempts=12（sealed receive 拒：本轮预算耗尽）
    rid4 = _rid("s2-round-12")
    body4 = _delivery_body(record_id=rid4, route_ref="http://127.0.0.1:1/callback",
                           scope_id=scope, arrival_seq=14, event_id="evt-s2-4",
                           round_attempts=12)
    seq4, _ = _seed(uat_env, body4)
    with pytest.raises(P20ReceiveError, match="(?i)round_attempts"):
        receive(store, rid4, payload_hash=body4["callback_body_hash"],
                route_ref=body4["route_ref"], expected_generation=0,
                event_id="evt-s2-4")
    assert store.get_main_record(rid4)["seq_no"] == seq4

    # ⑤ delivery_deadline_at 已过（★H-05 后协调器 sealed 闸先触发——
    #   三闸永远执法（now 缺省取当前时），旧"仓储侧拒/sealed 无截止检查"
    #   钉值随用户批准的 H-05 行为变更同步为协调器异常类型；
    #   仓储侧 update_lease 截止闸仍为第二道防线，另场景覆盖）
    rid5 = _rid("s2-deadline-past")
    body5 = _delivery_body(record_id=rid5, route_ref="http://127.0.0.1:1/callback",
                           scope_id=scope, arrival_seq=15, event_id="evt-s2-5",
                           deadline_in_hours=-1.0)
    seq5, _ = _seed(uat_env, body5)
    with pytest.raises(P20ReceiveError, match="(?i)deadline|截止"):
        receive(store, rid5, payload_hash=body5["callback_body_hash"],
                route_ref=body5["route_ref"], expected_generation=0,
                event_id="evt-s2-5")
    assert store.get_main_record(rid5)["seq_no"] == seq5


def test_uat_held_record_not_scanned_not_claimable(uat_env):
    """疑似确认扣留真层钉（10-07 令甲-i，I-11 补钉）：

    held 播种件（重复判定创建落点扣留，与三写入点分派口径一致）——
    扫描不收录（dispatcher.py:45 单态闸）、领取拒绝（receive 双态闸
    {pending, delivering} 词表外），拒绝零写入（seq_no/态均不动）。
    held 永不进 delivering 链，scan_expired_delivering 恢复面结构性
    不可达（单元层 G6-2 已钉，本件不重复钉）。
    """
    store = uat_env["store"]
    scope = "p20uat-held"
    rid = _rid("held-duplicate")
    body = _delivery_body(record_id=rid, route_ref="http://127.0.0.1:1/callback",
                          scope_id=scope, arrival_seq=21, event_id="evt-held-1",
                          delivery_state="held")
    # 扣留件真实形状：decision=重复（与 held 创建落点分派口径一致）
    body["result"]["decision"] = "重复"
    body["result"]["duplicate_ids"] = ["item-A"]
    body["result_decision"] = "重复"
    body["result_duplicate_ids"] = ["item-A"]
    seq0, _ = _seed(uat_env, body)
    now = datetime.now(timezone.utc).isoformat()
    assert rid not in scan_pending(store, now=now)                # 扫描排除
    with pytest.raises(P20ReceiveError, match="(?i)pending|delivering"):
        receive(store, rid, payload_hash=body["callback_body_hash"],
                route_ref=body["route_ref"], expected_generation=0,
                event_id="evt-held-1")
    after = store.get_main_record(rid)
    assert after["seq_no"] == seq0                                # 拒领零写入
    assert after["source"]["delivery_state"] == "held"            # 态未动


# ---------- 场景 3：领取 CAS payload_hash + route_ref 未变前置 + 代次互斥 ----------

def test_uat_claim_cas_hash_route_lock_and_generation_mutex(uat_env):
    """场景 3（批复 §3.1「最有价值的一行」+ INV-3）：冻结载荷投递侧执法点。

    (i) payload_hash 异变 → 拒领，零写入；(ii) route_ref 异变 → 拒领，零写入；
    (iii) 真 ES 篡改 route_ref 后按原值领取 → 存储现值对拍不符 → 拒领（篡改即拒）；
    (iv) 代次互斥：gen1 领取成功后同代次重入（expected_generation=0 → gen1）被拒，
    新代次（expected_generation=1 → gen2）接管成功（INV-3：旧 owner 立即丢弃旧结果，
    本用例以顺序确定性演示代次闸门；真并发竞态由仓储 CAS 版本冲突兜底）。
    """
    store = uat_env["store"]
    client = uat_env["client"]
    config = uat_env["config"]
    rid = _rid("s3")
    body = _delivery_body(record_id=rid, route_ref="http://127.0.0.1:1/callback",
                          scope_id="p20uat-s3", arrival_seq=21, event_id="evt-s3")
    seq0, _ = _seed(uat_env, body)

    # (i) payload_hash 异变 → 拒
    with pytest.raises(P20ReceiveError, match="(?i)payload_hash"):
        receive(store, rid, payload_hash="tampered-hash",
                route_ref=body["route_ref"], expected_generation=0,
                event_id="evt-s3")
    assert store.get_main_record(rid)["seq_no"] == seq0                  # 零写入

    # (ii) route_ref 异变 → 拒（占位 URL 一律 example.invalid，修订 1）
    with pytest.raises(P20ReceiveError, match="(?i)route_ref"):
        receive(store, rid, payload_hash=body["callback_body_hash"],
                route_ref="http://wrong.example.invalid/callback",
                expected_generation=0, event_id="evt-s3")
    assert store.get_main_record(rid)["seq_no"] == seq0

    # (iii) 真 ES 侧篡改 route_ref → 按原冻结值领取被拒；事后还原现场
    tampered = dict(store.get_main_record(rid)["source"],
                    route_ref="http://tampered.example.invalid/callback")
    client.index(index=config.items_index, id=rid, document=tampered,
                 refresh="wait_for")
    try:
        with pytest.raises(P20ReceiveError, match="(?i)route_ref"):
            receive(store, rid, payload_hash=body["callback_body_hash"],
                    route_ref=body["route_ref"], expected_generation=0,
                    event_id="evt-s3")
    finally:
        client.index(index=config.items_index, id=rid,
                     document=body, refresh="wait_for")

    # (iv) 代次互斥与接管
    lease1 = receive(store, rid, payload_hash=body["callback_body_hash"],
                     route_ref=body["route_ref"], expected_generation=0,
                     event_id="evt-s3")
    assert lease1.owner_generation == 1
    doc1 = store.get_main_record(rid)["source"]
    assert doc1["delivery_state"] == "delivering"
    assert doc1["delivery_lease"]["owner_generation"] == 1
    assert doc1["delivery_lease"]["attempt_id"] == compute_attempt_id("evt-s3", 1)
    assert doc1["round_attempts"] == 1               # ★领取计数自增（矛盾点 #4）
    assert doc1["callback_attempts"] == 1

    with pytest.raises(p20_ds.P20ClaimPreconditionError, match="(?i)generation|代次"):
        receive(store, rid, payload_hash=body["callback_body_hash"],
                route_ref=body["route_ref"], expected_generation=0,      # 同代次重入
                event_id="evt-s3")
    assert store.get_main_record(rid)["source"]["delivery_lease"]["owner_generation"] == 1

    lease2 = receive(store, rid, payload_hash=body["callback_body_hash"],
                     route_ref=body["route_ref"], expected_generation=1,  # 新代次接管
                     event_id="evt-s3")
    assert lease2.owner_generation == 2
    doc2 = store.get_main_record(rid)["source"]
    assert doc2["delivery_lease"]["owner_generation"] == 2
    assert doc2["delivery_lease"]["attempt_id"] == compute_attempt_id("evt-s3", 2)
    assert doc2["round_attempts"] == 2


# ---------- 场景 4：真 loopback 发送 → delivered（修订 1 三校 + INV-1/INV-2） ----------

def test_uat_loopback_post_byte_identical_ack_delivered(uat_env, callback_receiver):
    """场景 4（修订 1 + §4/INV-1/§4.3）：真 loopback POST → 2xx+succeed=true → delivered。

    接收器三校：① 接收载荷逐字节 == 冻结 callback_body；② 接收体 sha256 ==
    callback_body_hash（payload_hash 吻合）；③ 仅 POST 且路径 /callback。
    「不发明头」校：请求头 ⊆ 协议默认头 + Content-Type 白名单，无任何 X-* 自定义头。
    INV-2 校：投递仅改 delivery_* 字段组，decision/result/audit_ids 等语义字段
    与播种值逐字段恒等。
    """
    store = uat_env["store"]
    rid = _rid("s4")
    body = _delivery_body(record_id=rid, route_ref=callback_receiver.url,
                          scope_id="p20uat-s4", arrival_seq=31, event_id="evt-s4")
    _seed(uat_env, body)

    now = datetime.now(timezone.utc).isoformat()
    assert rid in scan_pending(store, now=now)                           # 调度可见
    lease = receive(store, rid, payload_hash=body["callback_body_hash"],
                    route_ref=callback_receiver.url, expected_generation=0,
                    event_id="evt-s4")
    assert lease.attempt_id == compute_attempt_id("evt-s4", 1)

    result = send_callback(store, rid)                                   # 真 loopback POST
    assert result.success is True
    assert result.response_code == 200

    # 修订 1 三校
    assert len(callback_receiver.received) == 1
    req = callback_receiver.received[0]
    assert req["method"] == "POST"                                       # 仅 POST
    assert req["path"] == "/callback"
    assert req["body"] == body["callback_body"].encode("utf-8")          # 逐字节一致
    assert hashlib.sha256(req["body"]).hexdigest() == body["callback_body_hash"]
    header_names = {name.lower() for name in req["headers"]}
    assert header_names <= _ALLOWED_REQUEST_HEADERS, (
        f"发明头越界：{sorted(header_names - _ALLOWED_REQUEST_HEADERS)}"
    )
    assert not any(name.startswith("x-") for name in header_names)

    # 终态 + INV-2（语义字段冻结）
    doc = store.get_main_record(rid)["source"]
    assert doc["delivery_state"] == "delivered"
    assert doc["delivery_lease"]["attempt_id"] == lease.attempt_id
    for frozen in ("result", "result_decision", "result_duplicate_ids",
                   "result_reason", "audit_ids", "callback_body",
                   "callback_body_hash", "route_ref", "event_id"):
        assert doc[frozen] == body[frozen], f"INV-2 语义字段被改写：{frozen}"


# ---------- 场景 5：ACK 否定路径 + attempt_id 核对 + 未知结果不重发不重计 ----------

def test_uat_ack_negative_paths_and_unknown_no_resend_no_recount(uat_env,
                                                                 callback_receiver):
    """场景 5（§4.3/§4.4/INV-6）：ACK 否定 → 不记 delivered 进退避；未知结果不重发不重计。

    (i) 非 2xx（500）→ success=False，状态回 pending 而非 delivered，接收器恰收
    1 请求（不自动重发），round_attempts 维持领取计入的 1（发送失败不重计）；
    (ii) 2xx 但 succeed=false → 同上（ACK 单一口径：2xx 且 succeed=true 才算）；
    (iii) attempt_id 派生核对：gen2 领取后文档租约 attempt_id ==
    SHA256(JCS([event_id, 2]))（全 64hex，主窗口追裁去 [:32] 截断）；
    (iv) 未知结果（连接拒绝）→ success=False 且状态维持 delivering（续期路径），
    计数不动；新代次（gen2）接管成功（INV-3）。
    """
    store = uat_env["store"]
    rid = _rid("s5")
    body = _delivery_body(record_id=rid, route_ref=callback_receiver.url,
                          scope_id="p20uat-s5", arrival_seq=41, event_id="evt-s5")
    _seed(uat_env, body)

    # (i) 非 2xx ACK
    callback_receiver.set_response(500, '{"succeed": false}')
    receive(store, rid, payload_hash=body["callback_body_hash"],
            route_ref=callback_receiver.url, expected_generation=0,
            event_id="evt-s5")
    r1 = send_callback(store, rid)
    assert r1.success is False
    doc = store.get_main_record(rid)["source"]
    assert doc["delivery_state"] == "pending"                            # 未记 delivered
    assert doc["round_attempts"] == 1                                    # 领取计 1，不重计
    assert doc["callback_attempts"] == 1
    assert len(callback_receiver.received) == 1                          # 不自动重发

    # (ii) 2xx + succeed=false（ACK 单一口径的另一失败面）
    callback_receiver.set_response(200, '{"succeed": false}')
    receive(store, rid, payload_hash=body["callback_body_hash"],
            route_ref=callback_receiver.url, expected_generation=1,
            event_id="evt-s5")
    r2 = send_callback(store, rid)
    assert r2.success is False
    doc = store.get_main_record(rid)["source"]
    assert doc["delivery_state"] == "pending"
    assert doc["round_attempts"] == 2                                    # 仅领取递增
    assert len(callback_receiver.received) == 2

    # (iii) attempt_id 派生核对（§3.2，真 ES 租约持久化值）
    assert doc["delivery_lease"]["attempt_id"] == compute_attempt_id("evt-s5", 2)

    # (iv) 未知结果：无监听端口 → 连接拒绝；不重发不重计，状态维持 delivering
    rid2 = _rid("s5-unknown")
    closed_url = _closed_port_url()
    body2 = _delivery_body(record_id=rid2, route_ref=closed_url,
                           scope_id="p20uat-s5", arrival_seq=42,
                           event_id="evt-s5u")
    _seed(uat_env, body2)
    receive(store, rid2, payload_hash=body2["callback_body_hash"],
            route_ref=closed_url, expected_generation=0, event_id="evt-s5u")
    r3 = send_callback(store, rid2, timeout_connect=0.5, timeout_total=1.0)
    assert r3.success is False
    assert r3.error is not None                                          # URLError/OSError
    doc2 = store.get_main_record(rid2)["source"]
    assert doc2["delivery_state"] == "delivering"        # 未知结果维持（INV-6 续期路径）
    assert doc2["round_attempts"] == 1                                   # 不重计
    assert doc2["callback_attempts"] == 1
    lease2 = receive(store, rid2, payload_hash=body2["callback_body_hash"],
                     route_ref=closed_url, expected_generation=1,        # 新代次接管
                     event_id="evt-s5u")
    assert lease2.owner_generation == 2
    assert store.get_main_record(rid2)["source"]["round_attempts"] == 2


# ---------- 批复 §4.1 / INV-1 增补：哈希再校——篡改冻结载荷即拒发 ----------

def test_uat_frozen_payload_tamper_refused_before_send(uat_env, callback_receiver):
    """批复 §4.1/INV-1：发送前「payload_hash 与 callback_body_hash 仍一致」再校。

    ★执法点在仓储读取转换层（_doc_to_record 复算 sha256(callback_body) 对拍
    callback_body_hash，矛盾点 #5）——已封 sender 不再校。真 ES 侧直接篡改
    callback_body（不动哈希字段）→ 领取路径读取即抛 P20PayloadIntegrityError，
    接收器零请求（篡改载荷绝不发出）。事后还原现场，不污染同 run 后续用例。
    """
    store = uat_env["store"]
    client = uat_env["client"]
    config = uat_env["config"]
    rid = _rid("s5b")
    body = _delivery_body(record_id=rid, route_ref=callback_receiver.url,
                          scope_id="p20uat-s5b", arrival_seq=43, event_id="evt-s5b")
    _seed(uat_env, body)

    tampered = dict(store.get_main_record(rid)["source"],
                    callback_body='{"item_id":"tampered"}')
    client.index(index=config.items_index, id=rid, document=tampered,
                 refresh="wait_for")
    try:
        with pytest.raises(p20_ds.P20PayloadIntegrityError,
                           match="(?i)hash|payload"):
            receive(store, rid, payload_hash=body["callback_body_hash"],
                    route_ref=callback_receiver.url, expected_generation=0,
                    event_id="evt-s5b")
    finally:
        client.index(index=config.items_index, id=rid,
                     document=body, refresh="wait_for")
    assert callback_receiver.received == []                              # 零发送


# ---------- 场景 6a：退避持久化 + 重启不重抽 ----------

def test_uat_backoff_persisted_restart_no_redraw(uat_env, callback_receiver):
    """场景 6a（§5「重启不重抽抖动；每次退避 CAS 一次持久化」）。

    ACK 失败 → advance_backoff 抽取一次 → persist_next_delivery 持久化；
    「重启」= 同 config 新建仓储实例（版本缓存清零，模拟新进程）→ 回读
    next_delivery_at 与持久化值逐字符相等（重启不重抽）、round_attempts 保留；
    重启后实例可直接接管领取（恢复路径）。
    """
    store = uat_env["store"]
    rid = _rid("s6a")
    body = _delivery_body(record_id=rid, route_ref=callback_receiver.url,
                          scope_id="p20uat-s6a", arrival_seq=51, event_id="evt-s6a")
    _seed(uat_env, body)
    callback_receiver.set_response(500, '{"succeed": false}')

    receive(store, rid, payload_hash=body["callback_body_hash"],
            route_ref=callback_receiver.url, expected_generation=0,
            event_id="evt-s6a")
    assert send_callback(store, rid).success is False
    next_at = persist_next_delivery(store, rid,
                                    next_seconds=advance_backoff(round_attempts=1))
    assert "T" in next_at

    # 「重启」：新仓储实例（同 config），回读持久化退避现场
    store2 = p20_ds.RealESP20Store(uat_env["client"], uat_env["config"])
    doc = store2.get_main_record(rid)["source"]
    assert doc["next_delivery_at"] == next_at                            # 不重抽
    assert doc["round_attempts"] == 1
    assert doc["callback_attempts"] == 1
    assert doc["delivery_state"] == "pending"
    lease = receive(store2, rid, payload_hash=body["callback_body_hash"],
                    route_ref=callback_receiver.url, expected_generation=1,
                    event_id="evt-s6a")
    assert lease.owner_generation == 2                                   # 重启后可接管


# ---------- 场景 6b：12 次尝试穷尽 → exhausted（加速，注明） ----------

def test_uat_exhaust_12_attempts_accelerated(uat_env, callback_receiver):
    """场景 6b（§6/INV-4）：round_attempts 达 12 → exhausted，当前轮不再发送。

    ★加速注明：播种 round_attempts=11（而非真跑 11 次失败循环，批复允许加速
    参数）；第 12 次 = 领取（计数自增至 12）+ ACK 失败 → exhaust_round 翻
    exhausted；此后 send_callback 因非 delivering 态即拒（当前轮不再发送）。
    """
    store = uat_env["store"]
    rid = _rid("s6b")
    body = _delivery_body(record_id=rid, route_ref=callback_receiver.url,
                          scope_id="p20uat-s6b", arrival_seq=52, event_id="evt-s6b",
                          round_attempts=11, callback_attempts=11)       # 加速播种
    _seed(uat_env, body)
    callback_receiver.set_response(500, '{"succeed": false}')

    receive(store, rid, payload_hash=body["callback_body_hash"],
            route_ref=callback_receiver.url, expected_generation=0,
            event_id="evt-s6b")
    assert store.get_main_record(rid)["source"]["round_attempts"] == 12  # 第 12 次
    assert send_callback(store, rid).success is False
    assert len(callback_receiver.received) == 1

    exhaust_round(store, rid)
    doc = store.get_main_record(rid)["source"]
    assert doc["delivery_state"] == "exhausted"                          # INV-4
    assert doc["callback_attempts"] == 12
    with pytest.raises(P20ReceiveError, match="(?i)delivering"):
        send_callback(store, rid)                                        # 当前轮不再发送
    assert len(callback_receiver.received) == 1


# ---------- 场景 6c：24h 截止 → expired（加速，注明） ----------

def test_uat_expire_24h_deadline_accelerated(uat_env):
    """场景 6c（§6/INV-5）：24h 截止到期 → expired；expired 记录退出调度扫描。

    ★加速注明：播种 delivery_deadline_at = now-1h（mock 时钟等效，批复允许）。
    截止后领取被仓储侧五前置拒绝（场景 2-⑤ 已校），本用例演示翻 expired 及
    调度扫描词表驱逐（scan_pending 仅收 pending）；已封 scan 不按截止过滤
    （截止执法在领取 CAS，矛盾点 #7 登记）。
    """
    store = uat_env["store"]
    rid = _rid("s6c")
    body = _delivery_body(record_id=rid, route_ref="http://127.0.0.1:1/callback",
                          scope_id="p20uat-s6c", arrival_seq=53, event_id="evt-s6c",
                          deadline_in_hours=-1.0)                        # 加速播种
    _seed(uat_env, body)

    expire_record(store, rid)
    doc = store.get_main_record(rid)["source"]
    assert doc["delivery_state"] == "expired"                            # INV-5
    now = datetime.now(timezone.utc).isoformat()
    assert rid not in scan_pending(store, now=now)                       # 退出调度


# ---------- 场景 7：audit_ids 映射核对（挂账 #7） ----------

def test_uat_audit_ids_mapping_crosscheck(uat_env):
    """场景 7（挂账 #7）：主记录 audit_ids 与审计文档逐一对照。

    审计对照文档落 p20 control 索引 sidecar（op_type=create 幂等，P19 孔洞台账
    先例；落位取舍见建议书矛盾点 #7 登记）：主记录 audit_ids 全量可解析、
    每份审计文档 current_record_id 回指本主记录、集合恒等无缺漏。
    """
    store = uat_env["store"]
    rid = _rid("s7")
    audits = [_audit_doc("s7-a", current_record_id=rid),
              _audit_doc("s7-b", current_record_id=rid)]
    audit_ids = [doc["comparison_id"] for doc in audits]
    store.write_audit_docs(audits)

    body = _delivery_body(record_id=rid, route_ref="http://127.0.0.1:1/callback",
                          scope_id="p20uat-s7", arrival_seq=61, event_id="evt-s7",
                          audit_ids=audit_ids, audit_complete=True)
    _seed(uat_env, body)

    doc = store.get_main_record(rid)["source"]
    assert sorted(doc["audit_ids"]) == sorted(audit_ids)                 # 映射无缺漏
    for audit_id, seeded in zip(audit_ids, audits):
        fetched = store.get_audit_doc(audit_id)
        assert fetched is not None, f"audit_id {audit_id!r} 无对照审计文档"
        assert fetched["current_record_id"] == rid                       # 回指主记录
        assert fetched["history_record_id"] == seeded["history_record_id"]
        assert fetched["basis"] == "FACT_EQUIVALENT"
    # 重放幂等（create 语义）：同 comparison_id 重记不重复
    store.write_audit_docs(audits)
    assert store.get_audit_doc(audit_ids[0]) is not None


# ---------- 场景 8a：清场 dry-run 仅含本 run 前缀 ----------

def test_uat_cleanup_plan_dry_run_run_prefix_only(uat_env):
    """场景 8a（公共标尺 4）：cleanup_plan dry-run 清单全部落本 run 前缀内。

    dry-run 先行 + 仅删本 run 前缀的执行对号由 `uat_env` fixture 析构执法
    （plan == deleted 硬断言）；此处跑中显式证明 dry-run 作用域不越界
    （192 保留索引永不入场）。
    """
    store = uat_env["store"]
    run_prefix = f"p20-batch-{uat_env['run_uuid']}-"
    plan = store.cleanup_plan()
    assert plan, "本 run 隔离索引应出现在 dry-run 清单"
    assert all(name.startswith(run_prefix) for name in plan)
    assert uat_env["config"].items_index in plan
    assert uat_env["config"].control_index in plan


# ---------- 场景 8b：192 保留索引零触碰 ----------

def test_p20_does_not_touch_192_preserved_indices(uat_client):
    """场景 8b（公共标尺 3）：192 保留索引零触碰。

    跑前/跑后严格 diff 由 `uat_client` fixture 析构执法（在 uat_env 清场之后）；
    此处跑中显式比对非 p20-batch 集合恒等。
    """
    client = uat_client["client"]
    now = _snapshot_indices(client)
    baseline = uat_client["snapshot_before"]
    assert sorted(n for n in now if not n.startswith("p20-batch-")) == sorted(
        n for n in baseline if not n.startswith("p20-batch-")
    ), "跑中非 p20 索引集合与基线不一致（越界硬停）"
