"""P18 真 ES UAT（05:25 批复标尺 + 公共标尺 · T2 站）。

起草本稿的纪律背景：
- 本稿由 T2 子窗口起草；src 产品代码（persist/es_store.py 等）由主窗口亲笔（决策日志 D7）。
- P18 真 ES 接口未落 src 前（挂账 N9），依赖接口的用例一律 pytest.skip 并注明
  "待主窗口接线批复"；草案全文见 `log/temp/p18-uat-wiring-proposal.md`。
- 真 ES 现实约束（T1 已踩出，本稿全量内化）：
  a) 集群 action.auto_create_index=-* 禁止自动建索引 → 仓储构造时显式幂等建索引
     （commit/es_store.py 19:16 修复先例）；
  b) "不存在才创建" = op_type=create；(if_seq_no=0, if_primary_term=0) 被 ES 8 拒（19:12）；
  c) ES _seq_no 是 0 基（19:17）；
  d) watermark 语义 = arrival_seq 水位值，推进返回新水位（05:34 裁定，19:28）；
  e) 环境变量双命名 TEST_ES_PASS/TEST_ES_PASSWORD 同值导出；.env 值带单引号，
     去引号由运行环境导出保证（此处仅兜底）；
  f) ES 走纯 HTTP（TEST_ES_SCHEME=http）。

公共标尺（目标文档 §三.5 + 05:25 批复 §8）：
1. 闸门 `P18_CONFIRM_UAT=1` 缺失即 skip/拒启；仅 TEST_* 凭据；PROD_* 出现即拒启；
2. 隔离前缀 `p18-batch-<run_uuid>-news-dedup-(items|audits|control)-v1-YYYY.MM.DD`；
3. 跑前 `_cat/indices` 快照、跑后 diff 证明 192 保留索引零触碰、无 stray；
4. dry-run 先行 + 清场（仅删本 run 前缀）；UAT 日志全文落盘（stdout 重定向）；
5. 发现越界立即全停（fixture 快照 diff 即硬停断言）。

跑站命令（主窗口执行；footer 分钟级向下取整）：

    $env:PYTHONPATH='src'
    $env:PYTHONIOENCODING='utf-8'
    $env:PYTHONDONTWRITEBYTECODE='1'
    $env:DEPLOY_ENV='test'
    $env:P18_CONFIRM_UAT='1'
    $env:TEST_ES_PASSWORD=<去引号密码>   # 同时导出 TEST_ES_PASS 同值
    & '.venv-v1\\Scripts\\python.exe' -m pytest -p no:cacheprovider `
        --basetemp '.\\tmp\\p18-uat-run' -v tests/integration/test_p18_uat.py `
        > ..\\log\\temp\\p18-uat-run-<HHMM>.txt
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import date, datetime, timedelta, timezone

import pytest

from news_flash_dedup.decide.types import DecideOutcome
from news_flash_dedup.es_client import (
    APPROVED_UAT_HOST,
    APPROVED_UAT_PORT,
    APPROVED_UAT_SCHEME,
)
from news_flash_dedup.persist import P18PersistError

# ---------- 受守卫的产品接口导入（挂账 N9：persist/es_store.py 未落 src 时整组 skip） ----------

try:
    from news_flash_dedup.persist import es_store as p18_es

    _P18_ES_IMPORT_ERROR = None
except ImportError as exc:  # persist/es_store.py 未落（T2 主窗口亲笔）
    p18_es = None
    _P18_ES_IMPORT_ERROR = exc


P18_UAT_ENV = "P18_CONFIRM_UAT"
BUSINESS_DATE_STR = "2026-09-26"
BUSINESS_DATE = date(2026, 9, 26)


# ---------- 闸门（公共标尺 1） ----------

def _gate_open(env) -> bool:
    """harness 级闸门：P18_CONFIRM_UAT=1 + TEST_ES_HOST 就位 + 无 PROD_ES_* 泄漏。"""
    if env.get(P18_UAT_ENV) != "1":
        return False
    if not env.get("TEST_ES_HOST"):
        return False
    if env.get("PROD_ES_HOST"):
        return False
    return True


def _uat_available() -> bool:
    return _gate_open(os.environ)


pytestmark = pytest.mark.skipif(
    not _uat_available(),
    reason=f"P18 UAT gate not open (need {P18_UAT_ENV}=1 + TEST_ES_*, no PROD_ES_*)",
)


def _require_p18_es() -> None:
    """P18 真 ES 接口未落 src 时 skip（挂账 N9；待主窗口接线批复）。"""
    if p18_es is None:
        pytest.skip(
            "P18 真 ES 接口 persist/es_store.py 未落 src（挂账 N9；草案全文见 "
            "log/temp/p18-uat-wiring-proposal.md）——待主窗口接线批复"
        )


# ---------- fixtures：客户端 + 快照/清场/前缀隔离 ----------

@pytest.fixture(scope="module")
def uat_client():
    """公共标尺 3：客户端 + 192 保留索引跑前快照；跑后严格 diff（零触碰/无 stray 硬停）。"""
    client = _build_es_client()
    snapshot_before = _snapshot_indices(client)
    print(f"[P18-UAT] baseline indices: {len(snapshot_before)}")
    yield {"client": client, "snapshot_before": snapshot_before}
    snapshot_after = _snapshot_indices(client)
    added = sorted(set(snapshot_after) - set(snapshot_before))
    missing = sorted(set(snapshot_before) - set(snapshot_after))
    print(f"[P18-UAT] snapshot diff added={added} missing={missing}")
    assert not added and not missing, (
        f"192 保留索引被改动或 stray 残留（越界硬停）！added={added} missing={missing}"
    )


@pytest.fixture(scope="module")
def uat_env(uat_client):
    """P18 隔离环境：run_uuid + RealESP18Config + 真 ES 仓储；跑后 dry-run 先行清场。

    清场范围 = 本 run 前缀 `p18-batch-<run_uuid>-`（与 T1 同款的更窄 run 作用域，
    等效且更保守）；fixture 析构顺序保证：先清 P18 隔离索引，后做 192 严格 diff。
    """
    _require_p18_es()
    run_uuid = "uat" + datetime.now(timezone.utc).strftime("%H%M%S%f")  # W-R3b 秒级→微秒
    config = p18_es.RealESP18Config(run_uuid=run_uuid, business_date=BUSINESS_DATE)
    client = uat_client["client"]
    # RealESP18Store 构造即：闸门断言 + 显式幂等建索引（auto_create_index=-* 集群约束 a）
    store = p18_es.RealESP18Store(client, config)
    print(
        "[P18-UAT] isolated indices: "
        f"{config.items_index} / {config.audits_index} / {config.control_index}"
    )
    yield {
        "config": config,
        "client": client,
        "store": store,
        "run_uuid": run_uuid,
    }
    # 清场（公共标尺 4）：dry-run 先行，仅删本 run 前缀
    run_prefix = f"p18-batch-{run_uuid}-"
    targets = sorted(n for n in _snapshot_indices(client) if n.startswith(run_prefix))
    if targets:
        print(f"[P18-UAT cleanup dry-run] will delete: {targets}")
    deleted = store.cleanup()
    print(f"[P18-UAT cleanup] deleted: {sorted(deleted)}")
    assert sorted(deleted) == targets, (
        f"清场清单与 dry-run 不一致：dry-run={targets} deleted={sorted(deleted)}"
    )


def _build_es_client():
    """从 TEST_ES_* 构造客户端（T1 `_build_es_client` 同型，读 TEST_ES_PASSWORD）。

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
    """`_cat/indices` 快照（192 保留索引清单）。"""
    response = client.cat.indices(format="json", h="index")
    return sorted(item.get("index", "") for item in response)


# ---------- 数据构造助手 ----------

def _rid(suffix: str) -> str:
    """确定性 64-hex record_id（按场景后缀隔离，避免同 run 内 create 冲突）。"""
    return hashlib.sha256(f"p18uat-{suffix}".encode("utf-8")).hexdigest()


def _audit_record(suffix: str, basis: str = "FACT_EQUIVALENT") -> dict:
    """审计文档（persist/audit_persist.py 同构）。"""
    rid_history = _rid(suffix + "-history")
    rid_current = _rid(suffix + "-current")
    return {
        "comparison_id": f"{rid_history}:{rid_current}",
        "history_record_id": rid_history,
        "current_record_id": rid_current,
        "history_raw_hash": "h-" + suffix,
        "current_raw_hash": "c-" + suffix,
        "basis": basis,
        "field_path": "facts.f1.subject",
        "detail": "主体事件一致",
        "history_evidence": {"record_id": rid_history, "field": "x",
                             "quote": "甲", "start": 0, "end": 1},
        "current_evidence": {"record_id": rid_current, "field": "x",
                             "quote": "甲", "start": 0, "end": 1},
        "pipeline_version": "dedup_v1",
    }


def _decide(decision: str = "重复", item_id: str = "item-C") -> DecideOutcome:
    return DecideOutcome(
        item_id=item_id, text="甲公司完成回购。",
        decision=decision,
        duplicate_ids=("item-A",) if decision == "重复" else (),
        reason="主体与事件一致",
        internal_code="FACT_EQUIVALENT",
        pair_codes={"a" * 64: "FACT_EQUIVALENT"},
        used_evidence=(),
        raw_hash="h",
        pipeline_version="dedup_v1",
    )


def _main_record_body(*, record_id: str, decide: DecideOutcome, scope_id: str,
                      arrival_seq: int, raw_hash: str, delivery_state: str,
                      task_state: str, audit_ids=(), audit_complete: bool = True,
                      error: str | None = None, result_null: bool = False) -> dict:
    """P18 §3.1 字段集主记录 body（对拍 FakeMainRecordV1 + 05:25/05:28 修订）。

    delivery_state 首建 not_ready（task_state=accepted 受理壳，对拍 admission.py
    L340 真实首建词汇）；CAS 成功翻 pending（task_state=succeeded）。
    """
    callback_body = json.dumps(decide.to_public_dict(), sort_keys=True,
                               ensure_ascii=False)
    completed_at = datetime.now(timezone.utc).isoformat()
    body = {
        "record_id": record_id,
        "item_id": decide.item_id,
        "text": decide.text,
        "scope_id": scope_id,
        "business_date": BUSINESS_DATE_STR,
        "arrival_seq": arrival_seq,
        "raw_hash": raw_hash,
        "pipeline_version": "dedup_v1",
        "fact_artifact_hash": "",
        "vector_state": "pending",               # 05:28 裁定：首建写入
        "result": None if result_null else decide.to_public_dict(),
        "result_item_id": decide.item_id,
        "result_decision": decide.decision,
        "result_duplicate_ids": list(decide.duplicate_ids),
        "result_reason": decide.reason,
        "task_state": task_state,
        "completed_at": completed_at,
        "result_version": 1,
        "event_id": hashlib.sha256(
            (decide.item_id + completed_at).encode("utf-8")
        ).hexdigest()[:32],
        "audit_ids": list(audit_ids),
        "audit_complete": audit_complete,
        "reason_code": decide.internal_code,
        "callback_body": callback_body,
        "callback_body_hash": hashlib.sha256(
            callback_body.encode("utf-8")
        ).hexdigest(),                            # INV-4
        "delivery_state": delivery_state,
        "delivery_deadline_at": (
            datetime.now(timezone.utc) + timedelta(hours=24)
        ).isoformat(),
        "next_delivery_at": completed_at,
        "callback_attempts": 0,
        "round_attempts": 0,
        "round": 1,
    }
    if error is not None:
        body["error"] = error
    return body


def _text_with_facts(text="甲公司完成回购。", subject="甲公司",
                     predicate="回购", record_id="a" * 64):
    """T1 _text_with_facts 同型（commit_one 接线演示用）。"""
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


def _ctx_for(text, record_id, item_id, arrival_seq,
             business_date=BUSINESS_DATE_STR, subject="甲公司", predicate="回购"):
    """T1 _ctx_for 同型（19:05 修复：转发 subject/predicate 保持引文一致）。"""
    return {
        "record_id": record_id, "item_id": item_id, "text": text,
        "raw_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "scope_id": "default", "business_date": business_date,
        "arrival_seq": arrival_seq, "pipeline_version": "dedup_v1",
        "facts": _text_with_facts(text=text, subject=subject, predicate=predicate,
                                  record_id=record_id),
    }


# ---------- 公共标尺 1：闸门 ----------

def test_p18_uat_harness_gate_requires_confirm_flag():
    """公共标尺 1（闸门）：`P18_CONFIRM_UAT=1` 缺失即 skip/拒启。

    harness 级恒可跑（不依赖未落 src 的接口）；产品闸 `assert_p18_uat_open`
    在 persist/es_store.py 落地后由本用例顺带覆盖。
    """
    assert _gate_open({P18_UAT_ENV: "1", "TEST_ES_HOST": "es.example"}) is True
    assert _gate_open({"TEST_ES_HOST": "es.example"}) is False            # 缺闸门
    assert _gate_open({P18_UAT_ENV: "0", "TEST_ES_HOST": "es.example"}) is False
    assert _gate_open({P18_UAT_ENV: "1"}) is False                        # 缺凭据
    assert _gate_open({P18_UAT_ENV: "1", "TEST_ES_HOST": "es.example",
                       "PROD_ES_HOST": "prod.example"}) is False          # PROD 泄漏拒启
    if p18_es is not None:
        with pytest.raises(p18_es.P18UATGatesNotOpen):
            p18_es.assert_p18_uat_open(env={"DEPLOY_ENV": "test"})


# ---------- 公共标尺 2 + 05:25 修订 1：隔离前缀硬正则 ----------

def test_p18_index_prefix_pattern_enforced():
    """隔离前缀必须严格匹配 p18-batch-<run>-news-dedup-(items|audits|control)-v1-YYYY.MM.DD。

    05:25 修订 1：P18 运行期写入一律 p18-batch-<run_uuid>- 前缀；p17- 前缀仅指
    P17-3 历史 run，P18 用例拒收（跨代污染防线）。
    """
    _require_p18_es()
    for kind in ("items", "audits", "control"):
        p18_es.validate_p18_index_name(
            f"p18-batch-uat123-news-dedup-{kind}-v1-2026.09.26"
        )
    # W2Fε 收紧（W1Fδ p20 模式跟进）：裸 Exception → P18IndexPrefixInvalid
    # （persist/es_store.validate_p18_index_name 唯一拒名异常类型；判别实证见下钉）
    with pytest.raises(p18_es.P18IndexPrefixInvalid, match="(?i)prefix|pattern|match"):
        p18_es.validate_p18_index_name("news-dedup-items-v1-2026.09.26")   # 缺前缀
    with pytest.raises(p18_es.P18IndexPrefixInvalid, match="(?i)prefix|pattern|match"):
        p18_es.validate_p18_index_name(
            "p17-batch-uat123-news-dedup-items-v1-2026.09.26"               # 跨代前缀
        )


# ---------- 公共标尺 2 补钉：收紧类型判别（W2Fε，W1Fδ p20 同款，逐收紧点各 1） ----------

@pytest.mark.parametrize("bad_name", [
    "news-dedup-items-v1-2026.09.26",                        # 收紧点 1：缺前缀
    "p17-batch-uat123-news-dedup-items-v1-2026.09.26",       # 收紧点 2：跨代前缀
])
def test_p18_index_prefix_tightened_type_discriminates(bad_name):
    """W2Fε 新红钉（W1Fδ p20 模式跟进）：裸 Exception 收紧为
    P18IndexPrefixInvalid 的判别实证。

    ① 收紧后原异常仍被捕（语义保持）：拒名仍抛 P18IndexPrefixInvalid
    （ValueError 子类，persist/es_store.validate_p18_index_name 唯一拒名异常类型）；
    ② 放行其他异常（新红钉）：异种异常（非 str 入参 → re.fullmatch 抛
    TypeError）不被 P18IndexPrefixInvalid 兜底，照旧外抛——泛 Exception
    收口已拆除、收紧类型具备判别力的直接证据。
    """
    _require_p18_es()
    assert issubclass(p18_es.P18IndexPrefixInvalid, ValueError)
    with pytest.raises(p18_es.P18IndexPrefixInvalid, match="(?i)prefix|pattern|match"):
        p18_es.validate_p18_index_name(bad_name)
    with pytest.raises(TypeError):
        p18_es.validate_p18_index_name(None)   # 异种异常：不被收紧类型捕获


# ---------- 批复标尺 1：审计先存、失败即停 ----------

def test_uat_audit_persist_failure_stops_before_cas(uat_env):
    """批复标尺 1：audit 写入失败 → 主记录 CAS 不发起（P18-INV-1/INV-6）。

    真 ES 演示：预置同 comparison_id 审计文档（op_type=create 语义下重放必
    version_conflict），persist_main_record_es 在审计阶段抛 AuditPersistError；
    回读证明主记录零写入、watermark 零推进——CAS 从未发起。
    """
    store = uat_env["store"]
    rec = _audit_record("s1")
    first_ids = store.write_audit_batch([rec])          # 先存语义本身演示
    assert first_ids == [rec["comparison_id"]]
    assert store.get_audit_doc(rec["comparison_id"]) is not None

    decide = _decide(item_id="item-S1")
    with pytest.raises(p18_es.AuditPersistError):
        p18_es.persist_main_record_es(
            decide, store,
            record_id=_rid("s1"),
            audit_records=[rec],                        # 同 ID 重放 → bulk 冲突即停
            scope_id="p18uat-s1", business_date=BUSINESS_DATE_STR,
            arrival_seq=11, raw_hash="c-s1", audit_complete=True,
        )
    # 失败即停：CAS 未发起（主记录不存在）、watermark 未推进
    assert store.get_main_record(_rid("s1")) is None
    assert store.get_watermark("p18uat-s1", BUSINESS_DATE_STR) is None


# ---------- 批复标尺 2：CAS 前置四校验真 ES 等效执法 ----------

def test_uat_cas_precondition_four_checks_enforced_on_real_es(uat_env):
    """批复标尺 2：CAS 前置四校验在真 ES 路径等效执法（persist/main_record_persist.py
    fake 语义对拍 + 真 ES 可执法的 raw_hash 对拍）。

    四校验：① delivery_state=pending（更新路径）；② pending ⇒ task_state=succeeded；
    ③ audit_complete=True；④ raw_hash 与已存文档一致（真 ES 实时 GET 对拍，
    fake 层仅构造侧保证——见建议书矛盾点 #7）。每次拒绝零写入（seq_no 不动）。
    """
    store = uat_env["store"]
    decide = _decide(item_id="item-S2")
    rid = _rid("s2")
    shell = _main_record_body(
        record_id=rid, decide=decide, scope_id="p18uat-s2", arrival_seq=21,
        raw_hash="c-s2", delivery_state="not_ready", task_state="accepted",
        audit_ids=(_audit_record("s2")["comparison_id"],), audit_complete=True,
    )
    seq0, term0 = store.create_main_record(rid, shell)

    # ① 更新路径 delivery_state != pending → 拒
    with pytest.raises(ValueError, match="(?i)delivery_state=pending"):
        store.cas_main_record(rid, dict(shell), expected_seq_no=seq0,
                              expected_primary_term=term0)
    # ② delivery_state=pending 但 task_state != succeeded → 拒
    bad_task = dict(shell, delivery_state="pending", task_state="failed")
    with pytest.raises(ValueError, match="(?i)task_state=succeeded"):
        store.cas_main_record(rid, bad_task, expected_seq_no=seq0,
                              expected_primary_term=term0)
    # ③ audit_complete=False → 拒
    bad_audit = dict(shell, delivery_state="pending", task_state="succeeded",
                     audit_complete=False)
    with pytest.raises(ValueError, match="(?i)audit_complete"):
        store.cas_main_record(rid, bad_audit, expected_seq_no=seq0,
                              expected_primary_term=term0)
    # ④ raw_hash 与已存文档不一致 → 拒（真 ES 对拍）
    bad_hash = dict(shell, delivery_state="pending", task_state="succeeded",
                    raw_hash="WRONG-HASH")
    with pytest.raises(ValueError, match="(?i)raw_hash"):
        store.cas_main_record(rid, bad_hash, expected_seq_no=seq0,
                              expected_primary_term=term0)

    # 四次拒绝均零写入：文档维持 shell 原态、版本号未动
    fetched = store.get_main_record(rid)
    assert fetched["seq_no"] == seq0
    assert fetched["source"]["delivery_state"] == "not_ready"
    assert fetched["source"]["task_state"] == "accepted"


# ---------- 批复标尺 3a：CAS 冲突（版本竞争）→ 不写且 delivery_state 不动 ----------

def test_uat_cas_conflict_leaves_delivery_state_untouched(uat_env):
    """批复标尺 3a + INV-2：CAS 冲突 → 不写且 delivery_state 不动。

    真 ES 演示：(i) 竞争方推进版本后，过期版本翻 pending → CASConflictError，
    回读仍 not_ready（投递意图绝不下发）；(ii) 成功翻 pending 后旧版本重放 →
    冲突，回读维持 pending 且 callback_body_hash 不变（INV-5 终态冻结）。
    """
    store = uat_env["store"]
    client = uat_env["client"]
    decide = _decide(item_id="item-S3")
    rid = _rid("s3")
    shell = _main_record_body(
        record_id=rid, decide=decide, scope_id="p18uat-s3", arrival_seq=22,
        raw_hash="c-s3", delivery_state="not_ready", task_state="accepted",
        audit_complete=True,
    )
    seq0, term0 = store.create_main_record(rid, shell)
    pending_body = dict(shell, delivery_state="pending", task_state="succeeded")

    # 竞争方（非本仓储路径）推进文档版本（隔离索引内的合法模拟）
    client.index(index=store.config.items_index, id=rid,
                 document=dict(shell, scratch="competitor"), refresh="wait_for")

    # (i) 过期 (seq0, term0) 翻 pending → 版本竞争冲突；delivery_state 维持 not_ready
    with pytest.raises(p18_es.CASConflictError):
        store.cas_main_record(rid, pending_body, expected_seq_no=seq0,
                              expected_primary_term=term0)
    fetched = store.get_main_record(rid)
    assert fetched["source"]["delivery_state"] == "not_ready"           # INV-2
    assert fetched["source"]["task_state"] == "accepted"

    # 正确版本翻 pending → 成功
    cur = store.get_main_record(rid)
    seq_ok = store.cas_main_record(
        rid, pending_body, expected_seq_no=cur["seq_no"],
        expected_primary_term=cur["primary_term"],
    )
    assert seq_ok > cur["seq_no"]

    # (ii) 旧版本重放 → 冲突；pending 与冻结字段不动
    with pytest.raises(p18_es.CASConflictError):
        store.cas_main_record(rid, pending_body, expected_seq_no=cur["seq_no"],
                              expected_primary_term=cur["primary_term"])
    after = store.get_main_record(rid)
    assert after["seq_no"] == seq_ok
    assert after["source"]["delivery_state"] == "pending"
    assert after["source"]["callback_body_hash"] == pending_body["callback_body_hash"]


# ---------- 批复标尺 3b：前置校验失败 → 端到端拒绝 ----------

def test_uat_precondition_failure_refuses_end_to_end(uat_env):
    """批复标尺 3b：audit_complete=False 经 persist_main_record_es 端到端拒绝——
    零审计写入、零主记录、零水位（对拍 fake test_cas_failure_due_to_missing_audit_keeps_state_clean）。
    """
    store = uat_env["store"]
    decide = _decide(item_id="item-S3B")
    rec = _audit_record("s3b")
    with pytest.raises(P18PersistError, match="(?i)audit_complete"):
        p18_es.persist_main_record_es(
            decide, store,
            record_id=_rid("s3b"),
            audit_records=[rec],
            scope_id="p18uat-s3b", business_date=BUSINESS_DATE_STR,
            arrival_seq=31, raw_hash="c-s3b", audit_complete=False,
        )
    assert store.get_main_record(_rid("s3b")) is None
    assert store.get_watermark("p18uat-s3b", BUSINESS_DATE_STR) is None
    assert store.get_audit_doc(rec["comparison_id"]) is None              # 先存未发生


# ---------- 批复标尺 4：watermark 单调 ----------

def test_uat_watermark_monotonic_and_regression_rejected(uat_env):
    """批复标尺 4：推进返回新水位值（05:34 裁定 arrival_seq 语义）；回退被拒（raise）。

    对拍 fake `advance_watermark`（ValueError regress）；真 ES 路径经 control
    索引 CAS/首建 create（约束 b/c/d 全量内化）。
    """
    store = uat_env["store"]
    scope = "p18uat-s4"
    first = store.advance_watermark(scope, BUSINESS_DATE_STR, 5)
    assert first == 5                                    # 新水位值而非更新计数
    second = store.advance_watermark(scope, BUSINESS_DATE_STR, 9)
    assert second == 9
    assert store.get_watermark(scope, BUSINESS_DATE_STR) == 9
    with pytest.raises(ValueError, match="(?i)regress"):
        store.advance_watermark(scope, BUSINESS_DATE_STR, 9)   # 等值亦拒（严格单调）
    with pytest.raises(ValueError, match="(?i)regress"):
        store.advance_watermark(scope, BUSINESS_DATE_STR, 3)
    assert store.get_watermark(scope, BUSINESS_DATE_STR) == 9   # 拒绝后水位未动


# ---------- 批复标尺 5：崩溃恢复三场景（可重入写入 + 状态回读） ----------

def test_uat_crash_recovery_audit_persisted_only(uat_env):
    """批复标尺 5a（§5.1：仅审计落库，主记录未 CAS）。

    真 ES 演示：审计先存后"崩溃"（无主记录）→ 恢复不重跑比对（审计重放被
    create 冲突挡下即"已存"），仅补 task_state=failed + error 标；补标可重入
    （重复补标冲突，状态不变，投递意图仍 not_ready）。
    """
    store = uat_env["store"]
    rec = _audit_record("s5a")
    assert store.write_audit_batch([rec]) == [rec["comparison_id"]]
    rid = _rid("s5a")
    assert store.get_main_record(rid) is None                         # 崩溃点

    # 恢复：仅补 failed 标（§5.1 不重跑比对；result=null 不投递业务结论）
    stub = _main_record_body(
        record_id=rid, decide=_decide(item_id="item-S5A"),
        scope_id="p18uat-s5a", arrival_seq=41, raw_hash="c-s5a",
        delivery_state="not_ready", task_state="failed",
        audit_ids=(rec["comparison_id"],), audit_complete=True,
        error="audit_only_persisted", result_null=True,
    )
    store.create_main_record(rid, stub)
    marked = store.get_main_record(rid)
    assert marked["source"]["task_state"] == "failed"
    assert marked["source"]["error"] == "audit_only_persisted"
    assert marked["source"]["result"] is None
    assert marked["source"]["delivery_state"] == "not_ready"          # 投递意图未下发

    # 重入幂等：重复补标 → create 冲突；状态不变
    with pytest.raises(p18_es.CASConflictError):
        store.create_main_record(rid, stub)
    assert store.get_main_record(rid)["seq_no"] == marked["seq_no"]

    # 不重跑比对：审计重放 → 冲突即"已存"（先存语义的另一面）
    with pytest.raises(p18_es.AuditPersistError):
        store.write_audit_batch([rec])


def test_uat_crash_recovery_cas_ok_watermark_pending(uat_env):
    """批复标尺 5b（§5.2：主记录 CAS 成功，watermark 未推进）。

    真 ES 演示：create(not_ready)+CAS(pending) 后"崩溃"（水位缺）→ 恢复先读
    结果后补水位（结果已落 → 推进合法，绝不先推进再落结果）；二次扫描水位已
    ≥ arrival_seq → 不再推进（盲目重推必被 regress 拒，证明幂等）。
    """
    store = uat_env["store"]
    scope = "p18uat-s5b"
    rid = _rid("s5b")
    decide = _decide(item_id="item-S5B")
    shell = _main_record_body(
        record_id=rid, decide=decide, scope_id=scope, arrival_seq=42,
        raw_hash="c-s5b", delivery_state="not_ready", task_state="accepted",
        audit_complete=True,
    )
    seq0, term0 = store.create_main_record(rid, shell)
    store.cas_main_record(rid, dict(shell, delivery_state="pending",
                                    task_state="succeeded"),
                          expected_seq_no=seq0, expected_primary_term=term0)
    assert store.get_watermark(scope, BUSINESS_DATE_STR) is None      # 崩溃点

    main = store.get_main_record(rid)
    assert main["source"]["task_state"] == "succeeded"                # 结果已落
    recovered = store.advance_watermark(scope, BUSINESS_DATE_STR,
                                        main["source"]["arrival_seq"])
    assert recovered == 42
    assert store.get_watermark(scope, BUSINESS_DATE_STR) == 42

    # 二次扫描幂等：盲目重推同值必被 regress 拒 → 恢复逻辑先读后跳
    with pytest.raises(ValueError, match="(?i)regress"):
        store.advance_watermark(scope, BUSINESS_DATE_STR, 42)
    assert store.get_watermark(scope, BUSINESS_DATE_STR) == 42


def test_uat_crash_recovery_full_committed_idempotent(uat_env):
    """批复标尺 5c（§5.3：CAS + watermark + delivery_state=pending 全部完成）。

    真 ES 演示：全链路（persist_main_record_es）完成后恢复扫描零动作——
    主记录 seq_no、watermark 值、审计文档三者回读均不变（恢复幂等，P20 接管投递）。
    """
    store = uat_env["store"]
    scope = "p18uat-s5c"
    rid = _rid("s5c")
    rec = _audit_record("s5c")
    p18_es.persist_main_record_es(
        _decide(item_id="item-S5C"), store,
        record_id=rid, audit_records=[rec],
        scope_id=scope, business_date=BUSINESS_DATE_STR,
        arrival_seq=51, raw_hash="c-s5c", audit_complete=True,
    )
    before = store.get_main_record(rid)
    assert before["source"]["task_state"] == "succeeded"
    assert before["source"]["delivery_state"] == "pending"
    assert store.get_watermark(scope, BUSINESS_DATE_STR) == 51
    assert store.get_audit_doc(rec["comparison_id"]) is not None

    # 恢复扫描零动作（无补写）→ 三者不变
    after = store.get_main_record(rid)
    assert after["seq_no"] == before["seq_no"]
    assert after["source"] == before["source"]
    assert store.get_watermark(scope, BUSINESS_DATE_STR) == 51


# ---------- 批复标尺 6 + 05:25 修订 2：delivery_state 生命周期 ----------

def test_uat_delivery_state_lifecycle_not_ready_to_pending(uat_env):
    """批复标尺 6：delivery_state not_ready →（CAS 成功）→ pending 真机演示。

    create 首建 not_ready（含 vector_state=pending 空壳，05:28 裁定）→ 回读；
    CAS 翻 pending（task_state=succeeded + audit_complete=True）→ 回读；
    INV-4 现场复算 callback_body_hash == sha256(callback_body)。
    """
    store = uat_env["store"]
    scope = "p18uat-s6"
    rid = _rid("s6")
    decide = _decide(item_id="item-S6")
    rec = _audit_record("s6")
    store.write_audit_batch([rec])
    shell = _main_record_body(
        record_id=rid, decide=decide, scope_id=scope, arrival_seq=55,
        raw_hash="c-s6", delivery_state="not_ready", task_state="accepted",
        audit_ids=(rec["comparison_id"],), audit_complete=True,
    )
    seq0, term0 = store.create_main_record(rid, shell)

    created = store.get_main_record(rid)
    assert created["source"]["delivery_state"] == "not_ready"         # 首建态
    assert created["source"]["vector_state"] == "pending"
    assert created["source"]["task_state"] == "accepted"

    seq1 = store.cas_main_record(
        rid, dict(shell, delivery_state="pending", task_state="succeeded"),
        expected_seq_no=seq0, expected_primary_term=term0,
    )
    assert seq1 > seq0                                                # 0 基单调（约束 c）
    flipped = store.get_main_record(rid)
    src = flipped["source"]
    assert src["delivery_state"] == "pending"                         # CAS 成功翻 pending
    assert src["task_state"] == "succeeded"
    assert src["audit_ids"] == [rec["comparison_id"]]
    recomputed = hashlib.sha256(src["callback_body"].encode("utf-8")).hexdigest()
    assert recomputed == src["callback_body_hash"]                    # INV-4


# ---------- 挂账 #6：commit_one ↔ 真 ES 接线演示 ----------

def test_uat_commit_one_real_es_wiring(uat_env):
    """挂账 #6：commit_one ↔ 真 ES 接线演示（待主窗口接线批复）。

    候选接线方案（适配器 / commit_one 参数化仓储协议，推荐后者）与草案全文见
    `log/temp/p18-uat-wiring-proposal.md`；`build_commit_one_store` 未落 src 前
    本用例 skip。落地后演示：CommitContext（T1 同型 facts，coverage_complete 由
    召回水位显式派生，禁默认 True）→ commit_one → committed → 真 ES 回读
    主记录 delivery_state=pending + watermark=arrival_seq。
    """
    builder = getattr(p18_es, "build_commit_one_store", None)
    if builder is None:
        pytest.skip(
            "commit_one 真 ES 接线未落（挂账 #6）——待主窗口接线批复"
            "（候选方案与取舍见 log/temp/p18-uat-wiring-proposal.md）"
        )
    from types import SimpleNamespace

    from news_flash_dedup.commit import (
        CommitContext,
        commit_one,
        is_watermark_complete,
    )

    store = uat_env["store"]
    # 带参签名两案兼容：方案 B 恒等返回（忽略 ctx 三参）；方案 A 适配器据此补源
    commit_store = builder(store, scope_id="p18uat-s7",
                           business_date=BUSINESS_DATE_STR, arrival_seq=61)

    text = "戊公司完成增发。"
    current = _ctx_for(text, _rid("s7-current"), "item-S7", 61,
                       subject="戊公司", predicate="增发")
    history = _ctx_for(text + "（先前）", _rid("s7-history"), "item-S6H", 60,
                       subject="戊公司", predicate="增发")
    # coverage_complete 由召回水位显式派生（13:20 案二：真实 UAT 路径禁默认 True）
    coverage_ok, _ = is_watermark_complete(
        SimpleNamespace(prepared_seq=100, lexical_watermark=100), arrival_seq=61,
    )
    assert coverage_ok is True
    ctx = CommitContext(
        scope_id="p18uat-s7", business_date=BUSINESS_DATE_STR,
        arrival_seq=61, current=current, candidates=(history,),
        visible_seq=100, prepared_seq=100, coverage_complete=True,
    )
    out = commit_one(ctx, commit_store, coverage_complete=coverage_ok,
                     audit_complete=True)
    assert out.state == "committed"
    assert out.decision_watermark_advanced_to == 61

    fetched = store.get_main_record(current["record_id"])
    assert fetched is not None
    assert fetched["source"]["delivery_state"] == "pending"
    assert store.get_watermark("p18uat-s7", BUSINESS_DATE_STR) == 61


# ---------- 公共标尺 3：192 保留索引零触碰 ----------

def test_p18_does_not_touch_192_preserved_indices(uat_client):
    """192 保留索引零触碰：跑前/跑后严格 diff 由 `uat_client` fixture 析构执法
    （在 uat_env 清场之后）；此处跑中显式比对非 p18-batch 集合恒等。
    """
    client = uat_client["client"]
    now = _snapshot_indices(client)
    baseline = uat_client["snapshot_before"]
    assert sorted(n for n in now if not n.startswith("p18-batch-")) == sorted(
        n for n in baseline if not n.startswith("p18-batch-")
    ), "跑中非 p18 索引集合与基线不一致（越界硬停）"
