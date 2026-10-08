r"""B4/E1 F5：(b) 轨金标真召回回放 UAT（J9-b/J9-c）。

设计底本=log\设计-真链路接线包.md §三-F5/§五-T5（R145/R148 修订承接）+
12 号文 §6.2 L256-275（指标口径）/§6.4 L289-296（P/A/S 与四要件）/§6.6 L354（门槛参考）。

闸（§五-T5）：`P23B_CONFIRM_UAT=1`（P23_CONFIRM_UAT 同族新闸，主窗口定名）
+`TEST_ES_HOST` 就位+无 `PROD_ES_*` 泄漏+`DEPLOY_ENV=test`。

纪律逐项：
- INV-1 金标钉死同文复用（test_p23_uat.py:198-219 `_verify_manifest_pinned`
  + :165-178 `_canonical/_digest/_file_sha256` 助手族 import 复用）；
- 隔离前缀 `p01-batch-p23b-<run>-`（单族：回放不提交，无 p18 族）；
- 快照 diff：dedup 保留族（p01/p17/p18/p20-batch-*、news-dedup*）名+文档数
  零触碰硬停；外部租客（rag_kb/flink 等）文档数漂移只报告不冒充；
- (a) 轨资产逐字节不动：p23-replay-*.json / 锚 JSON / manifest 只读；
  本件只写两个新命名空间：log\temp\p23b-replay-<run>.json 与
  log\temp\shadow-decision-<run>.json；
- 指标口径=12 §6.2 L256-275 逐行（c_i_30/c_i_10/r_i/v_i/e_i、Recall@30/10、
  覆盖率@30、30→10 保留率、逐查询数量 P50/P95/P99/max、未完成原因分列）；
- S-qualification 实核（J9-c）=四要件函数真实执行（五字段/域日顺序/去重排序/
  逐成员直接证据）+候选合法性全量实核（同域日/前序/非自身）+融合不变量
  全量钉（merged≤30、pair≤10、required=去重并集、protected⊆merged）；
- 禁 coverage 占位：真路径一律派生（embedding 过渡态恒缺口→coverage=False）；
- 失败单列不冒充边界（INV-4，test_p23_uat.py:54 同纪律）；
- 双跑对拍（§六 R13）：腿 A=(a) 轨固定候选决策（commit_one+FakeCommitStore，
  coverage_complete=True 显式占位同文）vs 腿 B=影子决策（真召回计划）；逐对
  五字段+duplicate_ids+internal_code 一致率全量留账，分歧原因分列（不预断
  一致率数值）。

物化口径（呈裁登记）：金标 business_date 冻结 synthetic 2000-01-01（P05 在案，
test_p23_uat.py:143）；若走受理路径 expires_at=2000-01-08 至今已过期→候选
全灭（candidate_eligible 到期门禁）。故 951 行按 batch_admission._documents
物化形（:441-469 十三键+raw_hash/task_state/delivery_state）直写隔离前缀日
索引，expires_at 置 2100-01-01（回放留账需要，非留存语义主张）；受理→物化
真链路本身的验收=F7（test_recall_link_uat.py），本件验收=召回/融合/决策回放。

跑站命令（log\temp\ 落日志；<HHMM>=footer 分钟级）：

    $env:PYTHONPATH='src'
    $env:DEPLOY_ENV='test'
    $env:P23B_CONFIRM_UAT='1'
    & '.venv-v1\\Scripts\\python.exe' -m pytest -p no:cacheprovider `
        --basetemp '.\\tmp\\p23b-uat-run' -v tests/integration/test_p23b_recall_replay.py `
        > ..\\log\\temp\\p23b-uat-run-<HHMM>.txt
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
    sys.path.insert(0, str(_THIS_DIR))          # prepend importmode 双保险

P23B_ENV = "P23B_CONFIRM_UAT"
BUSINESS_DATE = "2000-01-01"                    # P05 synthetic 冻结业务日
EXPIRY_REPLAY = "2100-01-01T00:00:00+08:00"     # 回放留账置远（见模块 docstring）
SEEDED_AT = "2000-01-01T00:00:00+08:00"
EMBEDDING_SPACE = "gold-synthetic"


def _gate_open() -> bool:
    env = os.environ
    return (env.get(P23B_ENV) == "1" and bool(env.get("TEST_ES_HOST"))
            and not env.get("PROD_ES_HOST"))


pytestmark = pytest.mark.skipif(
    not _gate_open(),
    reason=f"(b) 轨金标真召回回放闸未开（需 {P23B_ENV}=1 + TEST_ES_*，"
           "无 PROD_ES_*）",
)


# ---------- fixtures ----------

@pytest.fixture(scope="module")
def p23b_client():
    """客户端 + 基线快照；跑后 dedup 保留族名/文档数严格 diff（越界硬停）。"""
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
    before = _snapshot(client)
    print(f"[P23B-UAT] baseline indices={len(before)} "
          f"docs={sum(before.values())}")
    yield client
    after = _snapshot(client)
    added = sorted(set(after) - set(before))
    missing = sorted(set(before) - set(after))
    preserved = sorted(name for name in before if _is_dedup_family(name))
    drifted_preserved = sorted(name for name in preserved
                               if name in after and before[name] != after[name])
    drifted_external = sorted(
        name for name in set(before) & set(after)
        if name not in set(preserved) and before[name] != after[name])
    print(f"[P23B-UAT] snapshot diff added={added} missing={missing}")
    print(f"[P23B-UAT] preserved({len(preserved)}) drifted={drifted_preserved}")
    print(f"[P23B-UAT] external tenant drift (report only): {drifted_external}")
    assert not added and not missing, (
        f"索引增删越界（硬停）：added={added} missing={missing}")
    assert not drifted_preserved, (
        f"dedup 保留族文档数被触碰（硬停）：{drifted_preserved}")


@pytest.fixture(scope="module")
def p23b_gold():
    """金标资产：INV-1 钉死复算+工作簿对拍+回放计划（全同文复用 (a) 轨）。"""
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
    print(f"[P23B-UAT] gold: rows={len(rows)} plan_rows={len(plan_by_row)} "
          f"clear_pairs={len(clear)} hold={len(hold)}")
    return {
        "manifest": manifest, "rows": rows, "plan_by_row": plan_by_row,
        "clear": clear, "hold": hold,
    }


@pytest.fixture(scope="module")
def p23b_env(p23b_client, p23b_gold):
    """隔离前缀单族：日索引显式建（item_mapping）+951 行物化形直写+refresh。"""
    from test_p23_uat import _digest, _item_id, _record_id

    from news_flash_dedup.batch_admission import HEAD_ID, LOG_VERSION
    from news_flash_dedup.es_admission_schema import (
        control_mapping,
        item_mapping,
    )

    client = p23b_client
    run = "b4" + datetime.now(timezone.utc).strftime("%H%M%S%f")   # W-R3b 秒级→微秒
    prefix = f"p01-batch-p23b-{run}-"
    items_index = f"{prefix}news-dedup-items-v1-2000.01.01"
    control_index = f"{prefix}news-dedup-control-v1"
    client.indices.create(index=items_index, body=item_mapping())
    client.indices.create(index=control_index, body=control_mapping())
    rows = p23b_gold["rows"]
    plan_by_row = p23b_gold["plan_by_row"]
    operations: list[dict] = []
    for row_key, plan_row in plan_by_row.items():
        row = rows[row_key]
        doc = {
            "scope_id": plan_row["scope_id"],
            "request_id": "req-" + _digest("p23b-req:" + row_key)[:16],
            "item_id": _item_id(row_key),
            "record_id": _record_id(row_key),
            "schema_version": "v1", "pipeline_version": "dedup_v1",
            "embedding_space_id": EMBEDDING_SPACE,
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
    result = client.bulk(operations=operations, refresh="wait_for")
    assert not result["errors"], (
        f"物化写入存在失败：{[i for i in result['items'] if 'error' in i.get('create', {})][:3]}")
    # F2 水位推进需读受理头（prepare._advance_watermarks→head.last_materialized_seq
    # 快照入日控制文档）；直写物化无受理日志→种回放头（值=物化总数，形状
    # =batch_admission._validate_log :122-136 合法形）
    client.create(index=control_index, id=HEAD_ID, refresh="wait_for",
                  document={
                      "kind": "admission", "owner_id": f"p23b-{run}",
                      "last_allocated_seq": len(plan_by_row),
                      "last_materialized_seq": len(plan_by_row),
                      "checkpoint": {},
                      "pending": {"version": LOG_VERSION, "batches": []},
                      "updated_at": SEEDED_AT,
                  })
    print(f"[P23B-UAT] materialized {len(plan_by_row)} docs into {items_index}")
    yield {
        "run": run, "prefix": prefix, "items_index": items_index,
    }
    listed = sorted(client.indices.get(
        index=f"{prefix}*", allow_no_indices=True,
        ignore_unavailable=True).keys())
    print(f"[P23B-UAT cleanup dry-run] will delete: {listed}")
    for name in listed:
        client.indices.delete(index=name)
    print(f"[P23B-UAT cleanup] deleted: {listed}")


# ---------- 助手 ----------

def _snapshot(client) -> dict[str, int]:
    rows = client.cat.indices(format="json", h="index,docs.count")
    return {item["index"]: int(item.get("docs.count") or 0)
            for item in rows if not item.get("index", "").startswith(".")}


def _is_dedup_family(name: str) -> bool:
    return name.startswith(("p01-batch-", "p17-batch-", "p18-batch-",
                            "p20-batch-", "news-dedup"))


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
    """nearest-rank 百分位（确定性；n=0 调用方先拦）。"""
    rank = max(1, math.ceil(q / 100 * len(sorted_counts)))
    return sorted_counts[rank - 1]


def _count_stats(counts: list[int]) -> dict:
    """12 §6.2 L275：逐查询数量的总量/P50/P95/P99/max。"""
    if not counts:
        return {"total": 0, "p50": None, "p95": None, "p99": None,
                "max": None, "n": 0}
    ordered = sorted(counts)
    return {
        "total": sum(counts),
        "p50": _percentile(ordered, 50),
        "p95": _percentile(ordered, 95),
        "p99": _percentile(ordered, 99),
        "max": ordered[-1],
        "n": len(counts),
    }


# ---------- 回放主体 ----------

def test_p23b_recall_replay(p23b_client, p23b_gold, p23b_env):
    from test_p23_uat import (
        EXPECTED_DATASET_VERSION,
        WORKSPACE_LOG,
        _ctx_mapping,
        _item_id,
        _load_norm_dictionary,
        _record_id,
    )

    from news_flash_dedup.commit.coordinator import CommitContext, commit_one
    from news_flash_dedup.commit.fake_store import FakeCommitStore
    from news_flash_dedup.metrics import compute_payload_hash
    from news_flash_dedup.recall.models import RecallRequest
    from news_flash_dedup.recall.prepare import (
        ElasticsearchWatermarkProvider,
        PrepareWorker,
    )
    from news_flash_dedup.recall.service import (
        NullVectorSearcher,
        RecallService,
    )
    from news_flash_dedup.recall.worker import (
        plan_artifact,
        shadow_decide,
        shadow_decision_artifact,
    )

    client = p23b_client
    rows = p23b_gold["rows"]
    clear = p23b_gold["clear"]
    run = p23b_env["run"]
    prefix = p23b_env["prefix"]
    clock = lambda: datetime.now(timezone.utc)
    norm_dictionary, norm_dictionary_version = _load_norm_dictionary()

    # ---------- 准备（F2 真驱动，双 scope） ----------
    scopes = sorted({entry.scope_id for entry in clear})
    prepare_worker = PrepareWorker(client, prefix, clock=clock)
    provider = ElasticsearchWatermarkProvider(client, prefix)
    watermarks: dict[str, tuple[int, int]] = {}
    for scope_id in scopes:
        report = prepare_worker.prepare(scope_id, BUSINESS_DATE)
        assert report.conflicts == (), report.conflicts
        watermarks[scope_id] = (provider.visible_seq(scope_id, BUSINESS_DATE),
                                provider.prepared_seq(scope_id, BUSINESS_DATE))
        print(f"[P23B-UAT] prepare {scope_id}: prepared={len(report.prepared)} "
              f"watermarks={watermarks[scope_id]}")

    # ---------- 查询集：clear 对去重 current；g_i=金标重复前序集 ----------
    id_to_row: dict[str, tuple[str, int, str]] = {}   # record_id→(row_key,seq,scope)
    for row_key, plan_row in p23b_gold["plan_by_row"].items():
        id_to_row[_record_id(row_key)] = (
            row_key, plan_row["arrival_seq"], plan_row["scope_id"])
    currents: dict[str, dict] = {}
    for entry in clear:
        slot = currents.setdefault(entry.current_key, {
            "entry": entry, "g_i": set(), "scope_id": entry.scope_id,
        })
        if entry.gold_label == "重复":
            slot["g_i"].add(_record_id(entry.history_key))
    ordered_currents = sorted(
        currents.values(),
        key=lambda slot: (slot["scope_id"], slot["entry"].current_seq,
                          slot["entry"].current_key))
    q_plus = [slot for slot in ordered_currents if slot["g_i"]]
    print(f"[P23B-UAT] pairs={len(clear)} distinct_currents={len(ordered_currents)} "
          f"q_plus={len(q_plus)}")

    # ---------- 腿 B：真召回+影子决策（F0/F1 现役原语直驱） ----------
    service = RecallService(client, prefix,
                            vector_searcher=NullVectorSearcher(clock=clock),
                            clock=clock)
    failures: list[dict] = []
    legality_violations: list[dict] = []
    fusion_violations: list[dict] = []
    bundle_items: list[dict] = []
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
            embedding_space_id=EMBEDDING_SPACE)
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
            outcome_b = shadow_decide(
                current, candidates, coverage_complete=coverage,
                visible_seq=visible, prepared_seq=prepared,
                dictionary=norm_dictionary,
                dictionary_version=norm_dictionary_version)
        except Exception as exc:                       # INV-4：失败单列
            failures.append({"current_key": entry.current_key, "leg": "B",
                             "error_type": type(exc).__name__,
                             "error": str(exc)[:200]})
            continue
        merged_ids = [f.candidate.record_id for f in plan.merged_top30]
        pair10_ids = [f.candidate.record_id for f in plan.pair_top10]
        protected_ids = [f.candidate.record_id for f in plan.hash_protected]
        incremental_ids = [f.candidate.record_id
                           for f in plan.incremental_required]
        required_ids = [f.candidate.record_id for f in plan.required]
        # 融合不变量全量钉（真计划逐条）
        if not (len(merged_ids) <= 30 and len(pair10_ids) <= 10
                and len(set(required_ids)) == len(required_ids)
                and set(pair10_ids) <= set(merged_ids)
                and set(protected_ids) <= set(merged_ids)):
            fusion_violations.append({"record_id": record_id})
        # 候选合法性全量实核：同域日/前序/非自身（12 §6.2 h_i 合法形）
        for cand_id in set(merged_ids) | set(protected_ids) \
                | set(incremental_ids):
            row_key_c, seq_c, scope_c = id_to_row[cand_id]
            if (scope_c != slot["scope_id"] or seq_c >= entry.current_seq
                    or cand_id == record_id):
                legality_violations.append({
                    "current": record_id, "candidate": cand_id,
                    "candidate_scope": scope_c, "candidate_seq": seq_c})
        compared = {pair.history_record_id
                    for pair in outcome_b.pair_results}
        e_i = sorted(set(required_ids) & compared)
        r_i = sorted(set(merged_ids) | set(protected_ids)
                     | set(incremental_ids))
        per_current[entry.current_key] = {
            "outcome_b": outcome_b, "g_i": slot["g_i"],
            "h_i_count": sum(
                1 for key, plan_row in p23b_gold["plan_by_row"].items()
                if plan_row["scope_id"] == slot["scope_id"]
                and plan_row["arrival_seq"] < entry.current_seq),
            "c_i_30": merged_ids, "c_i_10": pair10_ids,
            "r_i": r_i, "v_i": required_ids, "e_i": e_i,
            "gaps": [{"channel": g.channel, "reason": g.reason}
                     for g in plan.recall_gaps],
        }
        bundle_items.append({
            "plan": plan_artifact(plan, request),
            "shadow_decision": shadow_decision_artifact(
                outcome_b, plan=plan, request=request,
                generated_at=clock().isoformat()),
            "pair_status": {pair.history_record_id: pair.outcome
                            for pair in outcome_b.pair_results},
        })
    print(f"[P23B-UAT] leg B: plans={len(bundle_items)} "
          f"failures={len(failures)} legality_violations={len(legality_violations)}")

    # ---------- 腿 A：(a) 轨固定候选决策（同文驱动） ----------
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
                coverage_complete=True)          # (a) 轨显式占位同文
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
        except Exception as exc:                       # INV-4：失败单列
            failures.append({"pair_id": entry.pair_id, "leg": "A",
                             "error_type": type(exc).__name__,
                             "error": str(exc)[:200]})
    print(f"[P23B-UAT] leg A: pairs={len(leg_a)} failures={len(failures)}")

    # ---------- 双跑对拍（§六 R13：逐对五字段+ids+签发码） ----------
    fields = ("decision", "duplicate_ids", "item_id", "text", "reason")
    agree_full = 0
    agree_field = {name: 0 for name in (*fields, "internal_code")}
    compared_pairs = 0
    for entry in clear:
        pair_a = leg_a.get(entry.pair_id)
        slot_b = per_current.get(entry.current_key)
        if pair_a is None or slot_b is None:
            continue
        public_b = slot_b["outcome_b"].to_public_dict()
        compared_pairs += 1
        full = True
        for name in fields:
            same = pair_a["public"][name] == public_b[name]
            agree_field[name] += 1 if same else 0
            full = full and same
        same_code = pair_a["internal_code"] == slot_b["outcome_b"].internal_code
        agree_field["internal_code"] += 1 if same_code else 0
        agree_full += 1 if (full and same_code) else 0

    # ---------- 12 §6.2 指标（q_plus 限定，L262-273 逐行） ----------
    recall_sets = {name: [] for name in
                   ("h_i", "g_i", "c_i_30", "c_i_10", "r_i", "v_i", "e_i")}
    hits = {name: 0 for name in
            ("c30", "c10", "r", "v", "e")}
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
    c10_minus_e = 0
    v_minus_e = 0
    for slot in q_plus:
        data = per_current.get(slot["entry"].current_key)
        if data is None:
            continue
        c10_minus_e += len(set(data["c_i_10"]) - set(data["e_i"]))
        v_minus_e += len(set(data["v_i"]) - set(data["e_i"]))
    unfinished = {
        "count_c10_minus_e": c10_minus_e,
        "count_v_minus_e": v_minus_e,
        "reasons": ("e_i=实入 pair_results 的 v_i 成员（PairOutcome 三类"
                    " equivalent/conflict/unresolved 均为完成有效比较，"
                    "compare/pair_compare.py:23）；差值=未实际直接比较"),
    }

    # ---------- S-qualification 实核（12 §6.4 L289-296 四要件） ----------
    structure_violations: list[dict] = []
    p_members: list[dict] = []
    for slot in ordered_currents:
        data = per_current.get(slot["entry"].current_key)
        if data is None:
            continue
        outcome_b = data["outcome_b"]
        public_b = outcome_b.to_public_dict()
        # L291 结构审计：不重复/边界的 D_i 必须为空；五字段封闭
        if set(public_b) != {"item_id", "text", "decision",
                             "duplicate_ids", "reason"}:
            structure_violations.append({
                "record_id": slot["entry"].current_key, "issue": "five_fields"})
        if public_b["decision"] != "重复" and public_b["duplicate_ids"]:
            structure_violations.append({
                "record_id": slot["entry"].current_key,
                "issue": "non_duplicate_with_duplicate_ids"})
        if public_b["decision"] != "重复":
            continue
        # P 成员四要件逐条实核
        dup_ids = list(public_b["duplicate_ids"])
        checks = {
            "five_fields": set(public_b) == {"item_id", "text", "decision",
                                             "duplicate_ids", "reason"},
            "domain_date_order": _domain_date_order_ok(
                dup_ids, id_to_row, slot),
            "dedup_order": len(dup_ids) == len(set(dup_ids)),
            "direct_evidence": all(member in set(data["e_i"])
                                   for member in dup_ids),
        }
        p_members.append({
            "record_id": _record_id(slot["entry"].current_key),
            "duplicate_ids": dup_ids, "checks": checks,
            "a_hit": bool(set(dup_ids) & data["g_i"]),
            "s_qualified": bool(dup_ids)
            and set(dup_ids) <= data["g_i"] and all(checks.values()),
        })
    s_qualification = {
        "P": len(p_members),
        "A": sum(1 for m in p_members if m["a_hit"]),
        "S": sum(1 for m in p_members if m["s_qualified"]),
        "members": p_members,
        "note": ("恒等投影占位语义下预期 P=0（test_p23_uat.py:352 同述）；"
                 "四要件函数对 P 成员真实执行；域日多样性=synthetic 单日"
                 "×2 scope 如实登记，不冒充多日覆盖"),
    }

    # ---------- 工件落盘（两新命名空间；(a) 资产零触碰） ----------
    generated_at = clock().isoformat()
    bundle = {
        "kind": "shadow_decision_bundle", "leg": "B", "run_id": run,
        "dataset_version": EXPECTED_DATASET_VERSION,
        "generated_at": generated_at, "items": bundle_items,
    }
    bundle["payload_hash"] = compute_payload_hash(
        {k: v for k, v in bundle.items() if k != "payload_hash"})
    metrics = {
        "run_id": run, "dataset_version": EXPECTED_DATASET_VERSION,
        "candidate_path": "real_recall_(b)_five_channel_fusion",
        "fact_supply": "mechanical_identity_projection_no_semantics（同 (a) 缺省）",
        "norm_dictionary": norm_dictionary_version,
        "coverage_semantics": ("derived_fail_closed：embedding 过渡态恒缺口"
                               "（NullVectorSearcher）→recall_incomplete=True"
                               "→coverage=False，真路径零占位"),
        "pipeline_version": "dedup_v1",
        "counts": {
            "rows": len(p23b_gold["plan_by_row"]),
            "pairs_clear": len(clear),
            "distinct_currents": len(ordered_currents),
            "q_plus": n_plus,
            "replayed_currents": len(per_current),
            "replay_failures": len(failures),
            "domain_days": len({(s, BUSINESS_DATE) for s in scopes}),
            "scopes": {scope: sum(
                1 for r in p23b_gold["plan_by_row"].values()
                if r["scope_id"] == scope) for scope in scopes},
            "watermark_note": ("回放头 last_materialized_seq=951（全局序语义）"
                               "→lexical=951；金标 per-scope 编号下可见性全覆盖"
                               "不扭曲合法性，prepared per-scope（137/814）"
                               "为实际闸值"),
        },
        "recall_at_30_10": recall_metrics,
        "per_query_counts": per_query_counts,
        "unfinished": unfinished,
        "leg_agreement": {
            "compared_pairs": compared_pairs,
            "full_tuple_equal": agree_full,
            "per_field_equal": agree_field,
            "note": ("腿 A=(a) 固定候选+coverage 显式 True 占位；腿 B=真召回"
                     "+派生 coverage=False（embedding 缺口）——reason/"
                     "internal_code 分歧的预期根因=占位 vs 派生（登记非缺陷）；"
                     "decision/duplicate_ids 为语义可比面"),
        },
        "s_qualification": s_qualification,
        "structure_audit": {
            "checked_outcomes": len(per_current),
            "violations": structure_violations,
        },
        "candidate_legality": {
            "checked_currents": len(per_current),
            "violations": legality_violations,
        },
        "fusion_invariants": {"violations": fusion_violations},
        "gate_reference_L354": {
            "recall_at_30_target": 0.98, "strict_e2e_recall_target": 0.90,
            "note": ("12 §6.6 L354 原文：建议门槛仅供 Baseline 后联合冻结，"
                     "不提高原数值门槛，非已达到或生产承诺——本 JSON 只出"
                     "实测值，不作过闸断言"),
        },
        "failures": failures,
    }
    metrics["payload_hash"] = compute_payload_hash(
        {k: v for k, v in metrics.items() if k != "payload_hash"})
    out_dir = WORKSPACE_LOG / "temp"
    out_dir.mkdir(parents=True, exist_ok=True)
    bundle_path = out_dir / f"shadow-decision-{run}.json"
    metrics_path = out_dir / f"p23b-replay-{run}.json"
    bundle_path.write_text(json.dumps(bundle, ensure_ascii=False, indent=2),
                           encoding="utf-8")
    metrics_path.write_text(json.dumps(metrics, ensure_ascii=False, indent=2),
                            encoding="utf-8")
    print(f"[P23B-UAT] artifacts: {bundle_path.name} / {metrics_path.name}")
    print(f"[P23B-UAT] Recall@30={recall_metrics['recall_at_30']} "
          f"Recall@10={recall_metrics['recall_at_10']} "
          f"coverage@30={recall_metrics['pair_coverage_at_30']}")
    print(f"[P23B-UAT] leg agreement full={agree_full}/{compared_pairs} "
          f"per_field={agree_field}")

    # ---------- 诚实闸（INV-4/合法性/结构/一致性） ----------
    assert not failures, f"回放存在失败（INV-4 单列）：{failures[:3]}"
    assert not legality_violations, (
        f"候选合法性违规：{legality_violations[:3]}")
    assert not fusion_violations, f"融合不变量违规：{fusion_violations[:3]}"
    assert not structure_violations, f"结构违规：{structure_violations[:3]}"
    assert n_plus > 0, "q_plus 分母为零——金标形态异常"
    assert compared_pairs == len(clear)
    # payload_hash 自证：落盘内容重算一致
    reloaded = json.loads(metrics_path.read_text(encoding="utf-8"))
    assert reloaded["payload_hash"] == compute_payload_hash(
        {k: v for k, v in reloaded.items() if k != "payload_hash"})


def _domain_date_order_ok(dup_ids: list[str], id_to_row: dict,
                          slot: dict) -> bool:
    """域日顺序要件：成员与 current 同域日且按 arrival_seq 非递减。"""
    seqs: list[int] = []
    for member in dup_ids:
        row_key, seq, scope = id_to_row.get(member, (None, None, None))
        if row_key is None or scope != slot["scope_id"]:
            return False
        seqs.append(seq)
    return seqs == sorted(seqs)
