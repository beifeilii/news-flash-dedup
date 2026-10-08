# -*- coding: utf-8 -*-
"""C5R 判官口径加固窗 · v3 臂B：P05 常驻轨合并腿（v2 供给+norm_dict+judge_v3 残判）。

定位（主窗口 v3 授权 B+C 轻量版 ②验收口径 C 轻量版）：
- 常驻轨=tests/integration/test_p23_uat.py 同族逐字（固定候选轨 (a)；v2 规则
  供给 rule_baseline_v2 + norm_dict_v1；合并口径=规则签发+残判签发+auto 不
  重复章）。唯一变量=judge prompt v2→v3（llm_bidi_v3；条款收窄：口径身份=
  统计指标定义身份闭合枚举，实体载体/名称表述排除）。harness 机械件 import
  复用 test_p23_uat（单源不转写）；本文件=C5R v3 适配新文件。
- **339 口径验收**（争议标签注记 c5r-disputed-label-62f74308.json 在案，
  manifest 文件不改）：排除 62f74308 后 q_plus=339；v1 参照值=327/339
  ≈0.9646017699115044。v3 须：recall_339 ≥ 327/339（⟺ tp_339 ≥ 327，整数
  等价）且 merged fp=0（全面硬闸）且 judged=313 且 failure/invalid/blocked=0
  且基线腿=63/0/277/51（规则侧零漂移旁证）。全面指标（340 口径）全量登记
  不折算成败。
- 顺序翻案格零新增（对 v1 基；族内转归全量登记）。
- 双腿制：连跑两遍，residual_payload_hash 逐字节同一（第二遍全缓存命中零
  API）。红测 8 判（c5rv3p*）已写 v3 键入同缓存目录——腿1 零 API 复用。
- 预算闸（决策 #16）：1.6M tok/日（UTC+8 业务日）全臂共享计数器
  c5r-budget-counter.json 续算；逐次 usage 实扣+计量 JSONL
  c5r-p05v3-api-<run>.jsonl；超闸 budget_blocked 单列不冒充（部分覆盖即红，
  次日续跑）。
- 锚对读（只读）：v1 锚 winw2fz-metrics-merged1.json（328/0/12/51、judged
  313/signed 265、payload 8f613399…）；v2 双腿 c5r-p05-merged-c5rp05*.json
  （326/0/14/51、payload 53a29745…）——逐对 diff 落 anchor_read。

跑站命令（仓根 news-flash-dedup；双腿制=同令连跑两遍）：

    $env:PYTHONPATH='src'
    $env:PYTHONIOENCODING='utf-8'
    $env:PYTHONDONTWRITEBYTECODE='1'
    $env:DEPLOY_ENV='test'
    $env:P23_CONFIRM_UAT='1'
    $env:P23_FACT_SUPPLY='rule_baseline_v2'
    $env:P23_NORM_DICT='<仓>\\data\\dictionaries\\norm_dict_v1.json'
    $env:P23_RESIDUAL_JUDGE='llm_bidi_v3'
    $env:DEDUP_EMBEDDING_KEY_FILE='C:\\Users\\ASUS\\Desktop\\文本去重\\workspace-dedup\\.env.dedup'
    & '.venv-v1\\Scripts\\python.exe' -m pytest -p no:cacheprovider `
        --basetemp '.\\tmp\\c5r-p05v3-pytest' -v tests/integration/test_c5r_p05_merged_v3.py `
        > .\\log\\temp\\c5r-p05v3-run-merged<N>.txt
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from news_flash_dedup.metrics import (
    MetricsAggregator,
    ReplayResult,
    _safe_lower_bound,
    compute_payload_hash,
)

import test_c5_oos_replay as c5
import test_p23_uat as p23
from test_c5r_judge_v3 import (
    BUDGET_PATH,
    DAILY_TOKEN_BUDGET,
    FP_PAIR_ID,
    RESIDUAL_ENABLED_VALUE_V3,
    RESIDUAL_SWITCH_ENV,
    _residual_config_v3,
)

REPO_LOG_TEMP = c5.REPO_LOG_TEMP
P05_CACHE_DIR = Path(os.environ.get(
    "C5R_P05_CACHE_DIR", str(REPO_LOG_TEMP / "c5r-p05-residual-cache")))
MODEL = os.environ.get("C5R_MODEL", "qwen-turbo")

# v1 锚（只读对拍）
V1_ANCHOR_PATH = REPO_LOG_TEMP / "winw2fz-metrics-merged1.json"
V1_MERGED = {"tp": 328, "fp": 0, "fn": 12, "tn": 51}
V1_JUDGED = 313
V1_SIGNED = 265
V1_BASE_PAIR = {"tp": 63, "fp": 0, "fn": 277, "tn": 51}
V1_PAYLOAD_HASH = ("8f61339953898bd0017511582a03b6f91b31c5785165c87178"
                   "bb3e8e14293f49")
# 339 口径（争议对 62f74308 出基；v1 参照=327/339）
DISPUTED_PAIR_ID = FP_PAIR_ID          # 62f74308…（test_c5r_judge_v3 同源）
DISPUTED_BASIS = 339
V1_REF_TP_339 = 327
RECALL_FLOOR_339 = 327 / 339           # 0.9646017699115044

pytestmark = pytest.mark.skipif(
    not c5._gate_open(os.environ),
    reason="C5R gate not open (need DEPLOY_ENV=test, P23_CONFIRM_UAT=1, no PROD_*)",
)


# ---------- 金标资产（P05 manifest；复用 p23 装载/钉死函数单源） ----------

@pytest.fixture(scope="module")
def gold_assets():
    manifest = p23._load_manifest()
    p23._verify_manifest_pinned(manifest)
    workbook_dir = Path(os.environ.get(p23.P23_WORKBOOK_DIR_ENV,
                                       str(p23._DEFAULT_WORKBOOK_DIR)))
    books = tuple(workbook_dir / name for name in p23.WORKBOOK_NAMES)
    missing = [b.name for b in books if not b.exists()]
    if missing:
        pytest.skip(f"金标工作簿不在本机（缺 {missing}）——诚实 skip（非红非绿）")
    materials = {m["material_id"]: m for m in manifest["materials"]}
    for book in books:
        digest = p23._file_sha256(book)
        assert digest in materials, "工作簿哈希不在 materials 钉死清单（INV-1 红）"
        assert book.stat().st_size == materials[digest]["byte_size"]
    from news_flash_dedup.gold import load_workbooks
    catalog = load_workbooks(books)
    rows = {row.row_key: row for row in catalog.rows}
    plan_by_row = {r["row_key"]: r for r in manifest["synthetic_plan"]["rows"]}
    for row_key in plan_by_row:
        assert row_key in rows
    clear, hold = p23._build_replay_plan(manifest, rows, plan_by_row)
    merged_extra = sum(
        len(p["sources"]) - 1
        for p in manifest["pairs"] if len(p["endpoint_row_keys"]) > 2
    )
    assert len(clear) == p23.FROZEN_COUNTS["clear_pairs"] + merged_extra
    assert len(hold) == p23.FROZEN_COUNTS["held_pairs"]
    return SimpleNamespace(
        manifest=manifest, rows=rows, plan_by_row=plan_by_row,
        clear=clear, hold=hold, books=books,
        logical_clear=len(clear), merged_extra=merged_extra,
    )


# ---------- 回放执行 fixture（P23 replay_run 同构；差异=v3 残判装配+预算闸+C5R 路径） ----------

@pytest.fixture(scope="module")
def replay_run(gold_assets):
    from news_flash_dedup.commit.coordinator import CommitContext, commit_one
    from news_flash_dedup.commit.fake_store import FakeCommitStore
    from news_flash_dedup.decide import llm_residual as _lr

    run_id = "c5rpv3" + datetime.now(timezone.utc).strftime("%H%M%S")
    metering_path = REPO_LOG_TEMP / f"c5r-p05v3-api-{run_id}.jsonl"
    residual = _residual_config_v3(run_id, metering_path,
                                   cache_dir=P05_CACHE_DIR)

    store = FakeCommitStore()
    watermark: dict[str, int] = {}
    items: list[dict] = []
    failures: list[dict] = []
    hold_items: list[dict] = []
    _norm_dictionary, _norm_dictionary_version = p23._load_norm_dictionary()

    def _drive(entry, *, allow_residual: bool) -> dict:
        rows = gold_assets.rows
        history = p23._ctx_mapping(entry.history_key, rows[entry.history_key],
                                   entry.history_seq, entry.scope_id,
                                   entry.business_date)
        current = p23._ctx_mapping(entry.current_key, rows[entry.current_key],
                                   entry.current_seq, entry.scope_id,
                                   entry.business_date)
        key = f"{entry.scope_id}|{entry.business_date}"
        watermark[key] = watermark.get(key, 0) + 1
        ctx = CommitContext(
            scope_id=entry.scope_id,
            business_date=entry.business_date,
            arrival_seq=entry.current_seq,
            current=current,
            candidates=(history,),
            visible_seq=entry.current_seq,
            prepared_seq=entry.current_seq,
            pipeline_version="dedup_v1",
            coverage_complete=True,
        )
        outcome = commit_one(ctx, store, decision_watermark_seq=watermark[key],
                             audit_complete=True,
                             dictionary=_norm_dictionary,
                             dictionary_version=_norm_dictionary_version)
        if outcome.state != "committed" or outcome.decide_outcome is None:
            raise RuntimeError(f"commit_one 未提交：state={outcome.state!r}")
        decide = outcome.decide_outcome
        pub = decide.to_public_dict()
        record = store.main_records[current["record_id"]]
        item = {
            "pair_id": entry.pair_id,
            "gold_label": entry.gold_label,
            "hold_reason": entry.hold_reason,
            "scope_id": entry.scope_id,
            "public": pub,
            "internal_code": decide.internal_code,
            "payload_hash": record.payload_hash,
            "history_item_id": history["item_id"],
            "replay_result": ReplayResult(
                history_record_id=history["record_id"],
                current_record_id=current["record_id"],
                predicted_decision=pub["decision"],
                predicted_duplicate_ids=tuple(pub["duplicate_ids"]),
                expected_decision=entry.gold_label,
                expected_duplicate_ids=(
                    (history["item_id"],) if entry.gold_label == "重复" else ()
                ),
            ),
        }
        if (allow_residual and pub["decision"] == "边界case/疑难case"):
            rows = gold_assets.rows
            call = residual.call
            if call.budget_exhausted or call.cap_exhausted:
                item["residual"] = {
                    "pair_id": entry.pair_id,
                    "cell": ("budget_blocked" if call.budget_exhausted
                             else "live_cap_blocked"),
                    "signed": False, "suspicion_score": None,
                    "note": "预算闸/活调帽阻断——未判定，不折算边界不冒充签发"}
            else:
                call.context = {"pair_id": entry.pair_id, "order": None}
                try:
                    res_out = residual.judge.judge_pair(
                        entry.pair_id, rows[entry.history_key].raw_text,
                        rows[entry.current_key].raw_text)
                    item["residual"] = res_out.to_audit_dict()
                    if (call.budget_exhausted or call.cap_exhausted) and (
                            res_out.cell == "failure"):
                        item["residual"]["cell"] = (
                            "budget_blocked" if call.budget_exhausted
                            else "live_cap_blocked")
                        item["residual"]["signed"] = False
                        item["residual"]["note"] = (
                            "判定途中闸拒——单腿结果保留，整体按阻断单列")
                    if item["residual"].get("cell") == "not_duplicate":
                        item["residual"]["auto_not_duplicate"] = True
                except Exception as exc:
                    item["residual"] = {
                        "pair_id": entry.pair_id, "cell": "failure",
                        "signed": False, "suspicion_score": None,
                        "error_type": type(exc).__name__,
                        "error": str(exc)[:200]}
                finally:
                    call.context = {"pair_id": None, "order": None}
        return item

    ordered = sorted(gold_assets.clear,
                     key=lambda e: (e.scope_id, e.current_seq, e.current_key))
    for entry in ordered:
        try:
            items.append(_drive(entry, allow_residual=True))
        except Exception as exc:
            failures.append({
                "pair_id": entry.pair_id,
                "gold_label": entry.gold_label,
                "error_type": type(exc).__name__,
                "error": str(exc)[:200],
            })
    for entry in sorted(gold_assets.hold,
                        key=lambda e: (e.scope_id, e.current_seq, e.current_key)):
        try:
            hold_items.append(_drive(entry, allow_residual=False))
        except Exception as exc:
            failures.append({
                "pair_id": entry.pair_id,
                "gold_label": "hold",
                "error_type": type(exc).__name__,
                "error": str(exc)[:200],
            })

    agg = MetricsAggregator()
    for item in items:
        agg.add(item["replay_result"])
    pair_m = dict(agg.pair_metrics())
    strict_m = dict(agg.strict_metrics())

    # ---- 残判合并段（p23._residual_metrics_section 同构；prompt_version 取 config） ----
    judged = [i for i in items
              if i.get("residual") is not None
              and i["residual"].get("cell") not in (
                  "budget_blocked", "live_cap_blocked", None)]
    blocked = [i for i in items
               if (i.get("residual") or {}).get("cell") in (
                   "budget_blocked", "live_cap_blocked")]
    merged_agg = MetricsAggregator()
    for item in items:
        rr = item["replay_result"]
        res = item.get("residual")
        if res is not None and res.get("signed"):
            rr = ReplayResult(
                history_record_id=rr.history_record_id,
                current_record_id=rr.current_record_id,
                predicted_decision="重复",
                predicted_duplicate_ids=(item["history_item_id"],),
                expected_decision=rr.expected_decision,
                expected_duplicate_ids=rr.expected_duplicate_ids)
        elif res is not None and res.get("cell") == "not_duplicate":
            rr = ReplayResult(
                history_record_id=rr.history_record_id,
                current_record_id=rr.current_record_id,
                predicted_decision="不重复",
                predicted_duplicate_ids=(),
                expected_decision=rr.expected_decision,
                expected_duplicate_ids=rr.expected_duplicate_ids)
        merged_agg.add(rr)
    merged_pair = dict(merged_agg.pair_metrics())
    merged_strict = dict(merged_agg.strict_metrics())

    cells: dict[str, dict[str, int]] = {}
    for item in judged:
        side = ("gold_duplicate" if item["gold_label"] == "重复"
                else "gold_nonduplicate")
        cell = item["residual"].get("cell", "failure")
        cells.setdefault(side, {}).setdefault(cell, 0)
        cells[side][cell] += 1
    rule_signed = [i for i in items if i["public"]["decision"] == "重复"]
    res_signed = [i for i in judged if i["residual"].get("signed")]
    auto_nd = [i for i in judged if i["residual"].get("cell") == "not_duplicate"]
    remainder = [i for i in judged
                 if not i["residual"].get("signed")
                 and i["residual"].get("cell") not in (
                     "not_duplicate", "invalid", "failure")]
    three_way = {
        "signed_duplicate": {
            "total": len(rule_signed) + len(res_signed),
            "rule_signed": len(rule_signed),
            "residual_signed": len(res_signed)},
        "auto_not_duplicate": {
            "total": len(auto_nd),
            "gold_duplicate": sorted(
                i["pair_id"] for i in auto_nd if i["gold_label"] == "重复"),
            "gold_nonduplicate": sum(
                1 for i in auto_nd if i["gold_label"] != "重复")},
        "boundary_remainder": {
            "total": len(remainder),
            "gold_duplicate": sum(
                1 for i in remainder if i["gold_label"] == "重复"),
            "gold_nonduplicate": sum(
                1 for i in remainder if i["gold_label"] != "重复")},
    }
    suspect_queue = sorted(
        ({"pair_id": i["pair_id"], "gold_label": i["gold_label"],
          "suspicion_score": i["residual"]["suspicion_score"],
          "cell": i["residual"]["cell"],
          "hc_decision": i["residual"]["hc"]["decision"],
          "ch_decision": i["residual"]["ch"]["decision"],
          "fired_rules_union": i["residual"]["fired_rules_union"]}
         for i in remainder),
        key=lambda e: (-(e["suspicion_score"] or 0.0), e["pair_id"]))
    per_pair = sorted((i["residual"] for i in judged), key=lambda r: r["pair_id"])
    residual_section = {
        "switch": f"{RESIDUAL_SWITCH_ENV}={residual.switch}",
        "layer": ("LLM 双向裁判（hc+ch 两顺序均'重复'才签）+机器验 R0-R6 "
                  "（decide/llm_residual.py + decide/machine_verify.py）；"
                  "判定逻辑=D32 终版 + judge_v3 口径收窄（C5R v3）；"
                  "call_fn=C5 预算闸计量包装（src 零改动）"),
        "mv_mode": residual.config.mv_mode,
        "model": residual.config.model,
        "prompt_version": residual.config.prompt_version,
        "prompt_sha256": _lr.judge_prompt_for_version(
            residual.config.prompt_version)[1],
        "cache_dir": str(residual.config.cache_dir),
        "judged_pairs": len(judged),
        "signed": sum(1 for i in judged if i["residual"].get("signed")),
        "cells": cells,
        "residual_tp": sum(1 for i in judged
                           if i["residual"].get("signed")
                           and i["gold_label"] == "重复"),
        "residual_fp": sum(1 for i in judged
                           if i["residual"].get("signed")
                           and i["gold_label"] != "重复"),
        "blocked_pairs": {
            "budget_blocked": sorted(
                i["pair_id"] for i in blocked
                if i["residual"]["cell"] == "budget_blocked"),
            "live_cap_blocked": sorted(
                i["pair_id"] for i in blocked
                if i["residual"]["cell"] == "live_cap_blocked"),
        },
        "merged_pair_metrics": merged_pair,
        "merged_strict_metrics": merged_strict,
        "merged_three_way": three_way,
        "ledger": residual.judge.ledger.snapshot(),
        "budget": residual.call.snapshot(),
        "suspect_queue": suspect_queue,
        "invalid_pairs": sorted(i["pair_id"] for i in judged
                                if i["residual"].get("cell") == "invalid"),
        "failure_pairs": sorted(i["pair_id"] for i in judged
                                if i["residual"].get("cell") == "failure"),
        "per_pair": per_pair,
        "residual_payload_hash": p23._digest(json.dumps(
            per_pair, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"))),
    }

    # ---- 339 口径（争议对出基；v1 参照 327/339） ----
    disputed_signed = any(
        i["pair_id"] == DISPUTED_PAIR_ID and i["residual"].get("signed")
        for i in judged)
    tp_339 = merged_pair["tp"] - (1 if disputed_signed else 0)
    recall_339 = tp_339 / DISPUTED_BASIS
    disputed_cell = next(
        (i["residual"].get("cell") for i in judged
         if i["pair_id"] == DISPUTED_PAIR_ID), None)
    basis_339 = {
        "disputed_pair_id": DISPUTED_PAIR_ID,
        "disputed_cell_v3": disputed_cell,
        "disputed_signed_v3": disputed_signed,
        "q_plus": DISPUTED_BASIS,
        "v1_reference": {"tp": V1_REF_TP_339, "recall": RECALL_FLOOR_339},
        "tp_339": tp_339,
        "recall_339": recall_339,
        "recall_floor_met": recall_339 >= RECALL_FLOOR_339,
        "note": ("争议标签注记 c5r-disputed-label-62f74308.json：P05=重复 vs "
                 "C1 口径⑪族=不重复，manifest 文件不改；排除后 339 重复对口径"),
    }

    # ---- v1 锚逐对 diff（只读；翻案格族/签发翻转登记） ----
    v1_anchor = json.loads(V1_ANCHOR_PATH.read_text(encoding="utf-8")) \
        if V1_ANCHOR_PATH.exists() else None
    v1_per_pair = {}
    if v1_anchor is not None:
        for p in v1_anchor["residual_judge"]["per_pair"]:
            v1_per_pair[p["pair_id"]] = p
    v3_per_pair = {p["pair_id"]: p for p in per_pair}
    v1_flip_set = sorted(p for p, v in v1_per_pair.items()
                         if v.get("order_flip_signed"))
    v3_flip_set = sorted(p for p, v in v3_per_pair.items()
                         if v.get("order_flip_signed"))
    sign_flips = []
    for pid in sorted(set(v1_per_pair) & set(v3_per_pair)):
        s1 = bool(v1_per_pair[pid].get("signed"))
        s2 = bool(v3_per_pair[pid].get("signed"))
        if s1 != s2:
            sign_flips.append({
                "pair_id": pid,
                "v1_cell": v1_per_pair[pid].get("cell"),
                "v3_cell": v3_per_pair[pid].get("cell"),
                "direction": ("signed→unsigned" if s1 else "unsigned→signed"),
            })
    anchor_read = {
        "v1_anchor": {"merged": V1_MERGED, "judged": V1_JUDGED,
                      "signed": V1_SIGNED, "payload_hash": V1_PAYLOAD_HASH,
                      "source": "winw2fz-metrics-merged1.json（只读）"},
        "v3_merged": merged_pair,
        "basis_339": basis_339,
        "sign_flip_pairs": sign_flips,
        "order_flip_diff": {
            "v1_flip_count": len(v1_flip_set),
            "v3_flip_count": len(v3_flip_set),
            "v1_flip_pairs": v1_flip_set,
            "v3_flip_pairs": v3_flip_set,
            "new_flips_in_v3": sorted(set(v3_flip_set) - set(v1_flip_set)),
            "resolved_in_v3": sorted(set(v1_flip_set) - set(v3_flip_set)),
        },
        "note": ("v3 锚验收（C 轻量版）：339 口径 recall≥327/339 + fp=0 硬闸；"
                 "全面 340 口径全量登记不折算成败；判读不调阈（INV-6）"),
    }

    metrics = {
        "run_id": run_id,
        "task": "C5R 判官口径加固窗 v3 臂B P05 合并腿（v2 供给+judge_v3 残判）",
        "dataset_version": p23.EXPECTED_DATASET_VERSION,
        "candidate_path": "fixed_candidate_(a)_gold_supplied_no_recall",
        "fact_supply": p23._fact_supply_label(),
        "norm_dictionary": (_norm_dictionary_version
                            if _norm_dictionary is not None else "none"),
        "coverage_complete": "explicit_true_placeholder_fixed_candidate",
        "pipeline_version": "dedup_v1",
        "counts": {
            "M": len(ordered),
            "H": len(gold_assets.hold),
            "replayed": len(items),
            "replay_failures": len(failures),
            "manifest_entries_total": p23.FROZEN_COUNTS["unique_explicit_pairs"],
            "manifest_clear_entries": p23.FROZEN_COUNTS["clear_pairs"],
            "merged_entry_expansion": gold_assets.merged_extra,
            "residual_judged": len(judged),
            "residual_blocked": len(blocked),
        },
        "pair_metrics": pair_m,
        "strict_metrics": strict_m,
        "residual_judge": residual_section,
        "anchor_read": anchor_read,
        "failures": failures,
        "api_metering_jsonl": str(metering_path),
    }
    metrics["payload_hash"] = compute_payload_hash(
        {k: v for k, v in metrics.items() if k != "payload_hash"}
    )

    out_path = REPO_LOG_TEMP / f"c5r-p05-merged-{run_id}.json"
    out_path.write_text(json.dumps(metrics, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    print(f"\n[C5R-P05V3] metrics json: {out_path}")
    print(f"[C5R-P05V3] base pair={json.dumps(pair_m, ensure_ascii=False, sort_keys=True)}")
    print(f"[C5R-P05V3] merged pair="
          f"{json.dumps(merged_pair, ensure_ascii=False, sort_keys=True)}")
    print(f"[C5R-P05V3] 339口径 tp={tp_339}/339 recall={recall_339:.6f} "
          f"floor=0.964602 met={recall_339 >= RECALL_FLOOR_339} "
          f"disputed_cell={disputed_cell}")
    print(f"[C5R-P05V3] residual judged={len(judged)} signed={residual_section['signed']} "
          f"blocked={len(blocked)} ledger={residual_section['ledger']}")
    print(f"[C5R-P05V3] payload_hash={residual_section['residual_payload_hash']}")
    print(f"[C5R-P05V3] sign_flips={len(sign_flips)} "
          f"order_flip v1={len(v1_flip_set)}→v3={len(v3_flip_set)} "
          f"new={len(anchor_read['order_flip_diff']['new_flips_in_v3'])}")
    print(f"[C5R-P05V3] budget={residual_section['budget']}")
    return SimpleNamespace(
        run_id=run_id, items=items, failures=failures, hold_items=hold_items,
        pair=pair_m, strict=strict_m, merged_pair=merged_pair,
        merged_strict=merged_strict, metrics=metrics, metrics_path=out_path,
        residual=residual, judged=judged, blocked=blocked,
        anchor_read=anchor_read, basis_339=basis_339,
    )


# ---------- 场景 1 · 闸门 + 供给面钉守 ----------

def test_c5r_p05v3_harness_gate_and_supply_face():
    assert c5._gate_open({c5.DEPLOY_ENV_VAR: "test", c5.P23_UAT_ENV: "1"}) is True
    assert c5._gate_open({c5.P23_UAT_ENV: "1"}) is False
    assert c5._gate_open({c5.DEPLOY_ENV_VAR: "test", c5.P23_UAT_ENV: "1",
                          "PROD_ES_HOST": "x"}) is False
    assert RESIDUAL_ENABLED_VALUE_V3 == "llm_bidi_v3"
    # 供给面钉守：合并腿口径=v2 供给+norm_dict_v1（与 v2 臂B 同，唯一变量=prompt）
    assert os.environ.get("P23_FACT_SUPPLY") == "rule_baseline_v2"
    assert os.environ.get("P23_NORM_DICT"), "P23_NORM_DICT 未配（合并腿口径不全）"
    assert "norm_dict_v1.json" in os.environ["P23_NORM_DICT"]


# ---------- 场景 2 · 339 口径验收（v3 锚门槛） ----------

def test_c5r_p05v3_merged_anchor_gates(replay_run):
    """recall_339≥327/339（tp_339≥327）+ fp=0 全面硬闸 + judged=313 +
    failure/invalid=0 + 基线腿=v2 供给锚 63/0/277/51（规则侧零漂移）。"""
    m = replay_run.metrics
    res = m["residual_judge"]
    assert not replay_run.failures, (
        f"回放失败 {len(replay_run.failures)} 对（INV-4）")
    assert not replay_run.blocked, (
        f"残判阻断 {len(replay_run.blocked)} 对——部分覆盖不宣称完成（次日续跑）")
    base = {k: m["pair_metrics"][k] for k in ("tp", "fp", "fn", "tn")}
    assert base == V1_BASE_PAIR, (
        f"基线腿={base} != v2 供给锚 {V1_BASE_PAIR}——规则侧漂移（非 prompt 面），停报"
    )
    # fp=0 硬闸（全面三口径）
    assert m["pair_metrics"]["fp"] == 0, "基线 fp != 0——硬闸破，立即停报"
    assert res["merged_pair_metrics"]["fp"] == 0, (
        "合并 fp != 0——precision 1.0 被破，立即停报")
    assert res["residual_fp"] == 0, "残判签发 fp != 0——硬闸破，立即停报"
    # 339 口径 recall 不降（tp_339 ≥ 327 ⟺ recall_339 ≥ 0.96460177）
    b = replay_run.basis_339
    assert b["q_plus"] == DISPUTED_BASIS == 339
    assert b["tp_339"] >= V1_REF_TP_339, (
        f"339口径 tp={b['tp_339']} < 327——recall_339 {b['recall_339']:.6f} "
        "< 0.964602 降线，锚再立失败，停报呈裁")
    assert b["recall_339"] >= RECALL_FLOOR_339
    # precision 1.0（全面点估计 + strict）
    ms = res["merged_strict_metrics"]
    mp = res["merged_pair_metrics"]
    assert ms["precision_strict"] == 1.0
    assert ms["S"] == mp["tp"] and ms["P"] == mp["tp"] + mp["fp"]
    # 宇宙形态
    assert res["judged_pairs"] == V1_JUDGED, (
        f"judged={res['judged_pairs']} != 313——残判宇宙漂移")
    assert res["failure_pairs"] == [] and res["invalid_pairs"] == []
    assert res["ledger"]["failures"] == 0
    # 台账自洽
    assert res["signed"] == res["residual_tp"] + res["residual_fp"]
    assert (m["pair_metrics"]["tp"] + res["residual_tp"]) == mp["tp"]
    scores = [e["suspicion_score"] for e in res["suspect_queue"]]
    assert scores == sorted(scores, reverse=True), "疑似队列未按疑似度降序"


# ---------- 场景 3 · 残判层：五字段输出契约不动 ----------

def test_c5r_p05v3_residual_public_contract_untouched(replay_run):
    signed_n = 0
    auto_n = 0
    for item in replay_run.items:
        res = item.get("residual")
        if res is None:
            continue
        if res.get("signed"):
            signed_n += 1
            assert item["public"]["decision"] == "边界case/疑难case"
        if res.get("cell") == "not_duplicate":
            auto_n += 1
            assert res.get("auto_not_duplicate") is True
            assert item["public"]["decision"] == "边界case/疑难case"
    assert signed_n == replay_run.metrics["residual_judge"]["signed"]
    assert auto_n == (replay_run.metrics["residual_judge"]
                      ["merged_three_way"]["auto_not_duplicate"]["total"])


# ---------- 场景 4 · 预算/计量落盘 ----------

def test_c5r_p05v3_budget_metering_written(replay_run):
    budget = replay_run.metrics["residual_judge"]["budget"]
    assert budget["daily_token_budget"] == DAILY_TOKEN_BUDGET
    if budget["live_calls"] > 0:
        assert BUDGET_PATH.exists(), "预算计数器未落盘（决策 #16 违例）"
        counter = json.loads(BUDGET_PATH.read_text(encoding="utf-8"))
        assert counter["used_tokens"] == budget["budget_used_tokens"]
        metering = replay_run.metrics_path.parent / (
            f"c5r-p05v3-api-{replay_run.run_id}.jsonl")
        assert metering.exists(), "API 计量 JSONL 未落盘"
        lines = metering.read_text(encoding="utf-8").splitlines()
        assert len(lines) == budget["live_calls"]
        estimated = sum(1 for line in lines
                        if json.loads(line).get("estimated"))
        assert estimated == 0, "usage 缺失估值事件——实扣纪律例外须入账复核"


# ---------- 场景 5 · 顺序翻案格族零新增（对 v1 基） ----------

def test_c5r_p05v3_order_flip_zero_resurgence(replay_run):
    """v3 新增顺序翻案格=∅（对 v1 基；族内转归全量登记不折算成败）。"""
    od = replay_run.anchor_read["order_flip_diff"]
    assert od["new_flips_in_v3"] == [], (
        f"v3 新增顺序翻案格 {len(od['new_flips_in_v3'])} 对"
        f"（{[p[:16] for p in od['new_flips_in_v3'][:5]]}…）——翻案族回潮，停报"
    )
    print(f"[C5R-P05V3] 顺序翻案格 v1={od['v1_flip_count']} → v3={od['v3_flip_count']}"
          f"（新增=0 钉守；解消={len(od['resolved_in_v3'])}）")
    sf = replay_run.anchor_read["sign_flip_pairs"]
    print(f"[C5R-P05V3] 签发翻转对 n={len(sf)}："
          f"{[(p['pair_id'][:16], p['direction']) for p in sf[:10]]}")
