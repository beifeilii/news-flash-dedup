# -*- coding: utf-8 -*-
"""C5R 判官口径加固窗 · v3 臂A：dev-319 全量重放（judge_v3）。

定位（主窗口 v3 授权 B+C 轻量版）：
- 口径=C5 同族逐字（固定候选轨 (a)+恒等投影+残判 LLM 双向判+机验 audit），
  唯一变量=judge prompt v2→v3（模式闸 llm_bidi_v3 → ResidualJudgeConfig
  (prompt_version="judge_v3")；条款收窄：口径身份=统计指标定义身份闭合枚举，
  实体载体/名称表述排除）。harness 机械件全部 import 复用 test_c5_oos_replay
  （单源不转写）；本文件=v2 臂A runner 机械变换适配（v2 文件零改动）。
- 验收（主窗口 ② C 轻量版 dev 面）：①dev fp=0 硬闸（三口径任一 fp → 红，
  立即停报）；②fp 对 62f74308 保持不签（v2 修复不回退；禁重复方言
  cell∈{not_duplicate,doubtful}）；③边界族签发率不劣于 v2（v3 signed ≤
  30/34，硬断言）；④顺序翻案格零新增（对 v1 基 25 对族外零新增）；
  ⑤tp/fn 全量登记对拍（v1=248/24、v2=247/25）不折算成败。
- 预算闸（决策 #16）：闸 1.6M tok/日全臂共享计数器（c5r-budget-counter.json）
  续算；逐次 usage 实扣+计量 JSONL；超闸 budget_blocked 单列（不折算边界、
  不冒充签发），次日同令续跑（缓存落盘即状态）。
- 缓存：v3 键落 臂A 同目录 log/temp/c5r-oos-residual-cache（缓存键含
  prompt_version+sha 跨版本隔离；v1/v2 键原位只读零写入）。
- 红测纪律：v1 基线=c5-oos-replay-c5oos045513.json（终腿指标，只读对拍）。

跑站命令（仓根 news-flash-dedup）：

    $env:PYTHONPATH='src'
    $env:PYTHONIOENCODING='utf-8'
    $env:PYTHONDONTWRITEBYTECODE='1'
    $env:DEPLOY_ENV='test'
    $env:P23_CONFIRM_UAT='1'
    $env:P23_RESIDUAL_JUDGE='llm_bidi_v3'
    $env:DEDUP_EMBEDDING_KEY_FILE='C:\\Users\\ASUS\\Desktop\\文本去重\\workspace-dedup\\.env.dedup'
    & '.venv-v1\\Scripts\\python.exe' -m pytest -p no:cacheprovider `
        --basetemp '.\\tmp\\c5r-oosv3-pytest' -v tests/integration/test_c5r_oos_replay_v3.py `
        > .\\log\\temp\\c5r-oosv3-run-<HHMM>.txt
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
from test_c5r_judge_v2 import (
    ARM_A_CACHE_DIR,
    BUDGET_PATH,
    DAILY_TOKEN_BUDGET,
    FP_PAIR_ID,
    RESIDUAL_SWITCH_ENV,
)
from test_c5r_judge_v3 import (
    RESIDUAL_ENABLED_VALUE_V3,
    _residual_config_v3,
)

REPO_LOG_TEMP = c5.REPO_LOG_TEMP
PUBLIC_KEYS = c5.PUBLIC_KEYS
DECISION_VOCAB = c5.DECISION_VOCAB
DEV_FROZEN = c5.DEV_FROZEN
ROLE_AMBIGUOUS_TAG = c5.ROLE_AMBIGUOUS_TAG
ANCHOR_MERGED = c5.ANCHOR_MERGED
ANCHOR_RECALL = c5.ANCHOR_RECALL
RECALL_BAND_HOLD = c5.RECALL_BAND_HOLD
RECALL_BAND_FLOOR = c5.RECALL_BAND_FLOOR

# v1 基线（C5 终腿；只读对拍，不符=红不 skip）
C5_V1_FINAL_METRICS = REPO_LOG_TEMP / "c5-oos-replay-c5oos045513.json"
C5_V1_MERGED = {"tp": 248, "fp": 1, "fn": 24, "tn": 12}
C5_V1_BOUNDARY_SIGNED = 29
C5_V1_BOUNDARY_TOTAL = 34

pytestmark = pytest.mark.skipif(
    not c5._gate_open(os.environ),
    reason="C5R gate not open (need DEPLOY_ENV=test, P23_CONFIRM_UAT=1, no PROD_*)",
)


# ---------- 金标资产（复用 C5 装载/钉死函数；v2 manifest 钉值单源） ----------

@pytest.fixture(scope="module")
def gold_assets():
    manifest = c5._load_manifest()
    c5._verify_manifest_pinned(manifest)
    workbook_dir = Path(os.environ.get(c5.WORKBOOK_DIR_ENV,
                                       str(c5._DEFAULT_WORKBOOK_DIR)))
    books = tuple(workbook_dir / name for name in c5.WORKBOOK_NAMES)
    missing = [b.name for b in books if not b.exists()]
    if missing:
        pytest.skip(f"金标工作簿不在本机（缺 {missing}）——诚实 skip（非红非绿）")
    materials = {m["material_id"]: m for m in manifest["materials"]}
    for book in books:
        digest = c5._file_sha256(book)
        assert digest in materials, "工作簿哈希不在 materials 钉死清单（INV-1 红）"
        assert book.stat().st_size == materials[digest]["byte_size"]
    from news_flash_dedup.gold import load_workbooks
    catalog = load_workbooks(books)
    rows = {row.row_key: row for row in catalog.rows}
    clear = c5._build_replay_plan(manifest, rows)
    assert len(clear) == DEV_FROZEN["pairs"]
    return SimpleNamespace(manifest=manifest, rows=rows, clear=clear, books=books)


# ---------- 回放执行 fixture（C5 replay_run 同构；差异=v2 装配+C5R 落盘路径） ----------

@pytest.fixture(scope="module")
def replay_run(gold_assets):
    from news_flash_dedup.commit.coordinator import CommitContext, commit_one
    from news_flash_dedup.commit.fake_store import FakeCommitStore
    from news_flash_dedup.decide import llm_residual as _lr

    run_id = "c5rv3d" + datetime.now(timezone.utc).strftime("%H%M%S")
    metering_path = REPO_LOG_TEMP / f"c5r-oos-api-{run_id}.jsonl"
    residual = _residual_config_v3(run_id, metering_path,
                                   cache_dir=ARM_A_CACHE_DIR)

    items: list[dict] = []
    boundary_items: list[dict] = []
    failures: list[dict] = []

    def _drive(entry) -> dict:
        rows = gold_assets.rows
        history = c5._ctx_mapping(entry.history_key, rows[entry.history_key],
                                  entry.history_seq, entry.scope_id,
                                  entry.business_date)
        current = c5._ctx_mapping(entry.current_key, rows[entry.current_key],
                                  entry.current_seq, entry.scope_id,
                                  entry.business_date)
        # 每对独立 FakeCommitStore+watermark=1（C5 回放适配注记 #2 同口径）
        pair_store = FakeCommitStore()
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
        outcome = commit_one(ctx, pair_store, decision_watermark_seq=1,
                             audit_complete=True,
                             dictionary=None,
                             dictionary_version="dict_v1")
        if outcome.state != "committed" or outcome.decide_outcome is None:
            raise RuntimeError(f"commit_one 未提交：state={outcome.state!r}")
        decide = outcome.decide_outcome
        pub = decide.to_public_dict()
        record = pair_store.main_records[current["record_id"]]
        item = {
            "pair_id": entry.pair_id,
            "gold_label": entry.gold_label,
            "stratum": entry.stratum,
            "strata": entry.strata,
            "sampling_rate": entry.sampling_rate,
            "family_id": entry.family_id,
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
        if pub["decision"] == "边界case/疑难case":
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
            driven = _drive(entry)
            (boundary_items if entry.gold_label == "边界case/疑难case"
             else items).append(driven)
        except Exception as exc:
            failures.append({
                "pair_id": entry.pair_id,
                "gold_label": entry.gold_label,
                "stratum": entry.stratum,
                "error_type": type(exc).__name__,
                "error": str(exc)[:200],
            })

    def _aggregate(item_list):
        agg = MetricsAggregator()
        for item in item_list:
            agg.add(item["replay_result"])
        return agg

    def _merged_of(item_list):
        m_agg = MetricsAggregator()
        for item in item_list:
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
            m_agg.add(rr)
        return m_agg

    agg = _aggregate(items)
    pair_m = dict(agg.pair_metrics())
    strict_m = dict(agg.strict_metrics())
    merged_agg = _merged_of(items)
    merged_pair = dict(merged_agg.pair_metrics())
    merged_strict = dict(merged_agg.strict_metrics())

    cp = {
        "baseline_precision_strict": strict_m["clopper_pearson_lower_95"],
        "baseline_pair_precision": _safe_lower_bound(
            pair_m["tp"], pair_m["tp"] + pair_m["fp"]),
        "baseline_pair_recall": _safe_lower_bound(
            pair_m["tp"], pair_m["tp"] + pair_m["fn"]),
        "merged_precision_strict": merged_strict["clopper_pearson_lower_95"],
        "merged_pair_precision": _safe_lower_bound(
            merged_pair["tp"], merged_pair["tp"] + merged_pair["fp"]),
        "merged_pair_recall": _safe_lower_bound(
            merged_pair["tp"], merged_pair["tp"] + merged_pair["fn"]),
    }

    def _label_dist(item_list) -> dict:
        dist: dict[str, int] = {}
        for item in item_list:
            dist[item["gold_label"]] = dist.get(item["gold_label"], 0) + 1
        return dist

    def _stratified(item_list):
        out = {}
        for item in item_list:
            out.setdefault(item["stratum"], []).append(item)
        section = {}
        for st in sorted(out):
            sub = out[st]
            bp = dict(_aggregate(sub).pair_metrics())
            mp = dict(_merged_of(sub).pair_metrics())
            section[st] = {
                "pairs": len(sub),
                "gold_dist": _label_dist(sub),
                "baseline_pair": bp,
                "merged_pair": mp,
                "merged_recall_cp95_lower": _safe_lower_bound(
                    mp["tp"], mp["tp"] + mp["fn"]),
                "merged_precision_cp95_lower": _safe_lower_bound(
                    mp["tp"], mp["tp"] + mp["fp"]),
            }
        return section

    stratified = _stratified(items)
    role_amb_items = [i for i in items + boundary_items
                      if ROLE_AMBIGUOUS_TAG in i["strata"]]
    role_ambiguous = [{
        "pair_id": i["pair_id"], "stratum": i["stratum"],
        "gold_label": i["gold_label"],
        "public_decision": i["public"]["decision"],
        "internal_code": i["internal_code"],
        "residual_cell": (i.get("residual") or {}).get("cell"),
        "residual_signed": (i.get("residual") or {}).get("signed", False),
    } for i in role_amb_items]

    boundary_dist: dict[str, int] = {}
    boundary_residual_cells: dict[str, int] = {}
    for i in boundary_items:
        boundary_dist[i["public"]["decision"]] = (
            boundary_dist.get(i["public"]["decision"], 0) + 1)
        cell = (i.get("residual") or {}).get("cell")
        if cell:
            boundary_residual_cells[cell] = (
                boundary_residual_cells.get(cell, 0) + 1)

    all_items = items + boundary_items
    judged = [i for i in all_items
              if i.get("residual") is not None
              and i["residual"].get("cell") not in (
                  "budget_blocked", "live_cap_blocked", None)]
    blocked = [i for i in all_items
               if (i.get("residual") or {}).get("cell") in (
                   "budget_blocked", "live_cap_blocked")]
    cells: dict[str, dict[str, int]] = {}
    for item in judged:
        side = ("gold_duplicate" if item["gold_label"] == "重复"
                else "gold_boundary" if item["gold_label"] == "边界case/疑难case"
                else "gold_nonduplicate")
        cell = item["residual"].get("cell", "failure")
        cells.setdefault(side, {}).setdefault(cell, 0)
        cells[side][cell] += 1
    rule_signed = [i for i in all_items if i["public"]["decision"] == "重复"]
    res_signed = [i for i in judged if i["residual"].get("signed")]
    auto_nd = [i for i in judged if i["residual"].get("cell") == "not_duplicate"]
    remainder = [i for i in judged
                 if not i["residual"].get("signed")
                 and i["residual"].get("cell") not in (
                     "not_duplicate", "invalid", "failure")]
    suspect_queue = sorted(
        ({"pair_id": i["pair_id"], "gold_label": i["gold_label"],
          "stratum": i["stratum"],
          "suspicion_score": i["residual"]["suspicion_score"],
          "cell": i["residual"]["cell"],
          "hc_decision": i["residual"]["hc"]["decision"],
          "ch_decision": i["residual"]["ch"]["decision"],
          "fired_rules_union": i["residual"]["fired_rules_union"]}
         for i in remainder),
        key=lambda e: (-(e["suspicion_score"] or 0.0), e["pair_id"]))
    per_pair_residual = sorted((i["residual"] for i in judged),
                               key=lambda r: r["pair_id"])
    residual_section = {
        "switch": f"{RESIDUAL_SWITCH_ENV}={residual.switch}",
        "layer": ("LLM 双向裁判（hc+ch 两顺序均'重复'才签）+机器验 R0-R6 "
                  "（decide/llm_residual.py + decide/machine_verify.py）；"
                  "判定逻辑=D32 终版+ judge_v3 口径收窄（C5R v3）；"
                  "call_fn=C5 预算闸计量包装（src 零改动）"),
        "mv_mode": residual.config.mv_mode,
        "model": residual.config.model,
        "prompt_version": residual.config.prompt_version,
        "prompt_sha256": _lr.judge_prompt_for_version(
            residual.config.prompt_version)[1],
        "cache_dir": str(residual.config.cache_dir),
        "cache_note": ("C5R v2 自目录（D32/C5 v1 缓存原位只读零写入；prompt "
                       "变更→v1 键全失效=全量重判，单元钉守）；fp 对 hc/ch 条目"
                       "由 c5r-fp-pin 先行写入，本回放对该对零 API 复用"),
        "judged_pairs": len(judged),
        "signed": sum(1 for i in judged if i["residual"].get("signed")),
        "cells": cells,
        "residual_tp": sum(1 for i in judged
                           if i["residual"].get("signed")
                           and i["gold_label"] == "重复"),
        "residual_fp": sum(1 for i in judged
                           if i["residual"].get("signed")
                           and i["gold_label"] == "不重复"),
        "blocked_pairs": {
            "budget_blocked": sorted(
                i["pair_id"] for i in blocked
                if i["residual"]["cell"] == "budget_blocked"),
            "live_cap_blocked": sorted(
                i["pair_id"] for i in blocked
                if i["residual"]["cell"] == "live_cap_blocked"),
        },
        "merged_three_way": {
            "signed_duplicate": {
                "total": len(rule_signed) + len(res_signed),
                "rule_signed": len(rule_signed),
                "residual_signed": len(res_signed)},
            "auto_not_duplicate": {
                "total": len(auto_nd),
                "gold_duplicate": sorted(
                    i["pair_id"] for i in auto_nd
                    if i["gold_label"] == "重复"),
                "gold_nonduplicate": sum(
                    1 for i in auto_nd if i["gold_label"] == "不重复"),
                "gold_boundary": sum(
                    1 for i in auto_nd
                    if i["gold_label"] == "边界case/疑难case")},
            "boundary_remainder": {
                "total": len(remainder),
                "gold_duplicate": sum(
                    1 for i in remainder if i["gold_label"] == "重复"),
                "gold_nonduplicate": sum(
                    1 for i in remainder if i["gold_label"] == "不重复"),
                "gold_boundary": sum(
                    1 for i in remainder
                    if i["gold_label"] == "边界case/疑难case")},
        },
        "ledger": residual.judge.ledger.snapshot(),
        "budget": residual.call.snapshot(),
        "suspect_queue": suspect_queue,
        "invalid_pairs": sorted(i["pair_id"] for i in judged
                                if i["residual"].get("cell") == "invalid"),
        "failure_pairs": sorted(i["pair_id"] for i in judged
                                if i["residual"].get("cell") == "failure"),
        "per_pair": per_pair_residual,
        "residual_payload_hash": c5._digest(json.dumps(
            per_pair_residual, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"))),
    }

    # ---- 逐对归因（C5 同构） ----
    attribution = []
    for item in all_items:
        res = item.get("residual")
        if res is not None and res.get("signed"):
            merged_pred = "重复"
        elif res is not None and res.get("cell") == "not_duplicate":
            merged_pred = "不重复"
        elif res is not None and res.get("cell") in (
                "budget_blocked", "live_cap_blocked"):
            merged_pred = "未判定(闸阻断)"
        else:
            merged_pred = item["public"]["decision"]
        gold = item["gold_label"]
        if gold == "重复":
            klass = "tp" if merged_pred == "重复" else "fn"
        elif gold == "不重复":
            klass = "fp" if merged_pred == "重复" else "tn"
        else:
            klass = "gold_boundary"
        attribution.append({
            "pair_id": item["pair_id"],
            "gold_label": gold,
            "stratum": item["stratum"],
            "strata": item["strata"],
            "sampling_rate": item["sampling_rate"],
            "family_id": item["family_id"],
            "public_decision": item["public"]["decision"],
            "internal_code": item["internal_code"],
            "residual_cell": (res or {}).get("cell"),
            "residual_signed": bool((res or {}).get("signed")),
            "merged_predicted": merged_pred,
            "class": klass,
            "flip": (klass in ("fn", "fp")),
        })
    attribution_path = REPO_LOG_TEMP / f"c5r-oos-pairs-{run_id}.jsonl"
    with attribution_path.open("w", encoding="utf-8") as sink:
        for record in sorted(attribution, key=lambda r: r["pair_id"]):
            sink.write(json.dumps(record, ensure_ascii=False) + "\n")
    flips = [r for r in attribution if r["flip"]]

    # ---- v1 基线对读（C5 终腿只读）+ fp 翻转登记 + 锚对读 ----
    v1 = json.loads(C5_V1_FINAL_METRICS.read_text(encoding="utf-8")) \
        if C5_V1_FINAL_METRICS.exists() else None
    v1_per_pair = {}
    if v1 is not None:
        for p in v1["residual_judge"]["per_pair"]:
            v1_per_pair[p["pair_id"]] = p
    v3_per_pair = {p["pair_id"]: p for p in per_pair_residual}
    fp_v1 = v1_per_pair.get(FP_PAIR_ID)
    fp_v3 = v3_per_pair.get(FP_PAIR_ID)
    fp_flip = {
        "pair_id": FP_PAIR_ID,
        "v1_cell": (fp_v1 or {}).get("cell"),
        "v1_signed": (fp_v1 or {}).get("signed"),
        "v3_cell": (fp_v3 or {}).get("cell"),
        "v3_signed": (fp_v3 or {}).get("signed"),
        "flipped": bool(fp_v1 and fp_v3 and fp_v1.get("signed")
                        and not fp_v3.get("signed")),
    }
    # 边界族签发率（v1 29/34 → v3 实测）
    v1_boundary_signed = C5_V1_BOUNDARY_SIGNED
    v3_boundary_signed = sum(
        1 for i in boundary_items if (i.get("residual") or {}).get("signed"))
    boundary_family = {
        "v1_signed": v1_boundary_signed, "total": C5_V1_BOUNDARY_TOTAL,
        "v3_signed": v3_boundary_signed,
        "v1_signed_rate": v1_boundary_signed / C5_V1_BOUNDARY_TOTAL,
        "v3_signed_rate": v3_boundary_signed / C5_V1_BOUNDARY_TOTAL,
        "delta": v3_boundary_signed - v1_boundary_signed,
        "v3_newly_unsigned": sorted(
            i["pair_id"] for i in boundary_items
            if not (i.get("residual") or {}).get("signed")
            and (v1_per_pair.get(i["pair_id"]) or {}).get("signed")),
        "v3_newly_signed": sorted(
            i["pair_id"] for i in boundary_items
            if (i.get("residual") or {}).get("signed")
            and not (v1_per_pair.get(i["pair_id"]) or {}).get("signed")),
    }
    # 顺序翻案格 diff（v1 25 对族 → v3 实测；臂C 输入之一）
    v1_flip_set = sorted(p for p, v in v1_per_pair.items()
                         if v.get("order_flip_signed"))
    v3_flip_set = sorted(p for p, v in v3_per_pair.items()
                         if v.get("order_flip_signed"))
    order_flip_diff = {
        "v1_flip_count": len(v1_flip_set),
        "v3_flip_count": len(v3_flip_set),
        "v1_flip_pairs": v1_flip_set,
        "v3_flip_pairs": v3_flip_set,
        "new_flips_in_v3": sorted(set(v3_flip_set) - set(v1_flip_set)),
        "resolved_in_v3": sorted(set(v1_flip_set) - set(v3_flip_set)),
    }

    coverage_complete = not failures and not blocked
    merged_recall = merged_pair["recall"]
    if merged_pair["fp"] > 0 or pair_m["fp"] > 0 or residual_section[
            "residual_fp"] > 0:
        verdict = "FP_HARD_GATE_BREACH"
    elif not coverage_complete:
        verdict = "PARTIAL_COVERAGE_BUDGET_GATE"
    elif merged_recall >= RECALL_BAND_HOLD:
        verdict = "GENERALIZATION_HELD"
    elif merged_recall >= RECALL_BAND_FLOOR:
        verdict = "WITHIN_EXPECTED_BAND"
    else:
        verdict = "ANOMALY_ROOT_CAUSE_REQUIRED"
    anchor_read = {
        "anchor": {**ANCHOR_MERGED, "recall": ANCHOR_RECALL,
                   "source": "④终验注册接管锚（log/④号任务金标评测报告.md §⑧）"},
        "c5r_dev_v3": {**merged_pair, "recall": merged_recall},
        "recall_delta": merged_recall - ANCHOR_RECALL,
        "bands": {"held": ">=0.96 泛化守住",
                  "expected": "0.90–0.96 预期带内",
                  "anomaly": "<0.90 异常需根因"},
        "verdict": verdict,
        "c5_v1_baseline": {**C5_V1_MERGED,
                           "recall": v1["merged_pair_metrics"]["recall"]
                           if v1 else None,
                           "source": "c5-oos-replay-c5oos045513.json（只读）"},
        "fp_flip": fp_flip,
        "boundary_family": boundary_family,
        "order_flip_diff": order_flip_diff,
        "note": ("判读不调阈（INV-6 同纪律）；dev 非总体代表性抽样——"
                 "点估计为 dev 集口径；v1 基线对拍=C5 终腿指标（只读）"),
    }

    def _stratum_dist(item_list) -> dict:
        dist: dict[str, int] = {}
        for item in item_list:
            dist[item["stratum"]] = dist.get(item["stratum"], 0) + 1
        return dist

    metrics = {
        "run_id": run_id,
        "task": "C5R 判官口径加固窗 v3 臂A dev split 全量重放（judge_v3）",
        "dataset_version": c5.EXPECTED_DATASET_VERSION,
        "manifest_file_sha256": c5.EXPECTED_FILE_SHA256,
        "candidate_path": "fixed_candidate_(a)_gold_supplied_no_recall",
        "fact_supply": ("mechanical_identity_projection_no_semantics（21:4x "
                        "主窗口裁定恒等投影脚手架，与 C5/P23 UAT 同文）"),
        "norm_dictionary": "none",
        "arrival_order": ("synthetic_no_plan_v2：seq=material_rank×1e6+"
                          "excel_row×4+side_rank（C5 同文）"),
        "coverage_complete": "explicit_true_placeholder_fixed_candidate",
        "replay_adapter_note_2": ("每对独立 FakeCommitStore+watermark=1"
            "（C5 回放适配注记 #2 同口径）"),
        "pipeline_version": "dedup_v1",
        "counts": {
            "M": len(ordered),
            "dev_binary_items": len(items),
            "dev_gold_boundary": len(boundary_items),
            "replayed": len(items) + len(boundary_items),
            "replay_failures": len(failures),
            "residual_judged": len(judged),
            "residual_blocked": len(blocked),
            "dev_label_dist": _label_dist(items + boundary_items),
            "dev_stratum_dist": _stratum_dist(items + boundary_items),
        },
        "pair_metrics": pair_m,
        "strict_metrics": strict_m,
        "merged_pair_metrics": merged_pair,
        "merged_strict_metrics": merged_strict,
        "cp95_lower_bounds": cp,
        "anchor_read": anchor_read,
        "stratified": stratified,
        "role_ambiguous": {
            "count": len(role_ambiguous),
            "note": ("strata 副层 role_ambiguous（R183 口径⑪）；dev 内 2 对"
                     "均 S_CROSS/金标边界；v3 表现入归因批"),
            "items": role_ambiguous,
        },
        "gold_boundary_set": {
            "distribution": boundary_dist,
            "residual_cells": boundary_residual_cells,
            "note": ("gold 边界 34 对只报分布不入二元指标；残判签发率 v1=29/34 "
                     "系统性宽松（C5 §六），v3 实测入 anchor_read.boundary_family "
                     "与归因批 c5r-boundary-attribution.json"),
        },
        "flips": {
            "count": len(flips),
            "note": ("翻对全登记（合并口径 predicted vs gold）：fn=金标重复 "
                     "系统未签；fp=金标不重复系统签重复（硬闸对象）"),
            "pairs": sorted(flips, key=lambda r: (r["class"], r["pair_id"])),
        },
        "residual_judge": residual_section,
        "p_set_audit": [
            {
                "pair_id": item["pair_id"],
                "gold_label": item["gold_label"],
                "stratum": item["stratum"],
                "expected_duplicate_ids": sorted(
                    item["replay_result"].expected_duplicate_ids),
                "predicted_duplicate_ids": sorted(
                    item["replay_result"].predicted_duplicate_ids),
                "s_qualified": set(item["replay_result"].predicted_duplicate_ids)
                <= set(item["replay_result"].expected_duplicate_ids),
                "source": "rule",
            }
            for item in items
            if item["replay_result"].predicted_decision == "重复"
        ] + [
            {
                "pair_id": item["pair_id"],
                "gold_label": item["gold_label"],
                "stratum": item["stratum"],
                "expected_duplicate_ids": sorted(
                    item["replay_result"].expected_duplicate_ids),
                "predicted_duplicate_ids": [item["history_item_id"]],
                "s_qualified": item["gold_label"] == "重复",
                "source": "residual",
            }
            for item in items
            if (item.get("residual") or {}).get("signed")
        ],
        "failures": failures,
        "attribution_jsonl": str(attribution_path),
        "api_metering_jsonl": str(metering_path),
    }

    metrics["payload_hash"] = compute_payload_hash(
        {k: v for k, v in metrics.items() if k != "payload_hash"}
    )

    out_path = REPO_LOG_TEMP / f"c5r-oos-replay-{run_id}.json"
    out_path.write_text(json.dumps(metrics, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    print(f"\n[C5R-OOSV3] metrics json: {out_path}")
    print(f"[C5R-OOSV3] attribution jsonl: {attribution_path}")
    print(f"[C5R-OOSV3] merged pair="
          f"{json.dumps(merged_pair, ensure_ascii=False, sort_keys=True)}")
    print(f"[C5R-OOSV3] cp95={json.dumps(cp, ensure_ascii=False, sort_keys=True)}")
    print(f"[C5R-OOSV3] anchor verdict={verdict} "
          f"(recall={merged_recall:.6f} vs anchor {ANCHOR_RECALL:.6f})")
    print(f"[C5R-OOSV3] fp_flip={json.dumps(fp_flip, ensure_ascii=False)}")
    print(f"[C5R-OOSV3] boundary_family v1={v1_boundary_signed}/34 → "
          f"v3={v3_boundary_signed}/34")
    print(f"[C5R-OOSV3] order_flip v1={len(v1_flip_set)} → v3={len(v3_flip_set)} "
          f"new={len(order_flip_diff['new_flips_in_v3'])}")
    print(f"[C5R-OOSV3] residual judged={len(judged)} blocked={len(blocked)} "
          f"ledger={residual_section['ledger']}")
    print(f"[C5R-OOSV3] budget={residual_section['budget']}")
    print(f"[C5R-OOSV3] flips={len(flips)} failures={len(failures)}")
    return SimpleNamespace(
        run_id=run_id, items=items, boundary_items=boundary_items,
        failures=failures, blocked=blocked, judged=judged,
        pair=pair_m, strict=strict_m, merged_pair=merged_pair,
        merged_strict=merged_strict, cp=cp, anchor_read=anchor_read,
        stratified=stratified, metrics=metrics, metrics_path=out_path,
        attribution_path=attribution_path, residual=residual,
        fp_flip=fp_flip, boundary_family=boundary_family,
        order_flip_diff=order_flip_diff,
    )


# ---------- 场景 1 · 闸门 ----------

def test_c5r_oos_harness_gate_requires_confirm_flag():
    assert c5._gate_open({c5.DEPLOY_ENV_VAR: "test", c5.P23_UAT_ENV: "1"}) is True
    assert c5._gate_open({c5.P23_UAT_ENV: "1"}) is False
    assert c5._gate_open({c5.DEPLOY_ENV_VAR: "test"}) is False
    assert c5._gate_open({c5.DEPLOY_ENV_VAR: "prod", c5.P23_UAT_ENV: "1"}) is False
    assert c5._gate_open({c5.DEPLOY_ENV_VAR: "test", c5.P23_UAT_ENV: "1",
                          "PROD_ES_HOST": "x"}) is False
    assert c5._gate_open({c5.DEPLOY_ENV_VAR: "test", c5.P23_UAT_ENV: "1",
                          "PROD_MILVUS_ENDPOINT": ""}) is False
    assert RESIDUAL_ENABLED_VALUE_V3 == "llm_bidi_v3"


# ---------- 场景 2 · manifest 钉死（INV-1） ----------

def test_c5r_oos_manifest_integrity_hash_pinned():
    manifest = c5._load_manifest()
    c5._verify_manifest_pinned(manifest)
    forbidden = {"text", "raw_text", "raw_id", "quote"}
    for pair in manifest["pairs"]:
        assert not (set(pair) & forbidden)


# ---------- 场景 3 · dev 资产可恢复 + 冻结分布 ----------

def test_c5r_oos_dev_assets_recoverable(gold_assets):
    clear = gold_assets.clear
    labels: dict[str, int] = {}
    strata: dict[str, int] = {}
    role_amb = 0
    for entry in clear:
        labels[entry.gold_label] = labels.get(entry.gold_label, 0) + 1
        strata[entry.stratum] = strata.get(entry.stratum, 0) + 1
        if ROLE_AMBIGUOUS_TAG in entry.strata:
            role_amb += 1
        assert entry.history_seq < entry.current_seq
    assert labels["重复"] == DEV_FROZEN["重复"]
    assert labels["不重复"] == DEV_FROZEN["不重复"]
    assert labels["边界case/疑难case"] == DEV_FROZEN["边界case/疑难case"]
    for st in ("S_MULTI", "S_UPG", "S_CROSS", "S_HOLD"):
        assert strata.get(st) == DEV_FROZEN[st]
    assert role_amb == DEV_FROZEN["role_ambiguous"]


# ---------- 场景 4 · 输出合同：全量逐条五字段 + 闭合词表 ----------

def test_c5r_oos_output_contract_five_fields_closed_vocab_full_replay(replay_run):
    all_items = replay_run.items + replay_run.boundary_items
    assert all_items, "回放成功数为 0——合同验证退化空转（全灭即红）"
    assert len(all_items) + len(replay_run.failures) == 319
    for item in all_items:
        pub = item["public"]
        assert set(pub.keys()) == PUBLIC_KEYS
        assert pub["decision"] in DECISION_VOCAB
        assert isinstance(pub["duplicate_ids"], list)
        assert (pub["decision"] == "重复") == bool(pub["duplicate_ids"])
        assert isinstance(pub["item_id"], str) and pub["item_id"]
        assert isinstance(pub["text"], str) and pub["text"]
        assert isinstance(pub["reason"], str) and pub["reason"]
        expected_hash = hashlib.sha256(
            json.dumps(pub, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        assert item["payload_hash"] == expected_hash


# ---------- 场景 5 · 指标正确性：CP 对拍 scipy + P-set 复算 ----------

def test_c5r_oos_metrics_correctness_cp_scipy_and_output_p_set(replay_run):
    from scipy.stats import beta

    for x, n in ((319, 319), (272, 272), (9, 10), (0, 319), (0, 0)):
        expected = 0.0 if (n <= 0 or x <= 0) else float(beta.ppf(0.05, x, n - x + 1))
        assert _safe_lower_bound(x, n) == expected
    assert 0.99 < _safe_lower_bound(319, 319) < 1.0

    results = [i["replay_result"] for i in replay_run.items]
    p_recount = sum(1 for r in results if r.predicted_decision == "重复")
    q_plus_recount = sum(1 for r in results if r.expected_decision == "重复")
    s_recount = sum(
        1 for r in results
        if r.predicted_decision == "重复"
        and set(r.predicted_duplicate_ids)
        and set(r.predicted_duplicate_ids) <= set(r.expected_duplicate_ids)
    )
    strict = replay_run.strict
    assert strict["P"] == p_recount
    assert strict["q_plus"] == q_plus_recount
    assert strict["S"] == s_recount
    if strict["P"] > 0:
        assert strict["precision_strict"] == strict["S"] / strict["P"]
        assert strict["clopper_pearson_lower_95"] == float(
            beta.ppf(0.05, strict["S"], strict["P"] - strict["S"] + 1))


# ---------- 场景 6 · 端到端：覆盖完整 + fp=0 硬闸 + fp 翻转 + tp/fn 零恶化 ----------

def test_c5r_oos_e2e_coverage_fp_hard_gate_and_v1_comparison(replay_run):
    """场景 6（v3 验收 dev 面）：
    ① 覆盖完整（失败=0、闸阻断=0）；② fp=0 硬闸（三口径任一 fp → 红停报）；
    ③ fp 保持：62f74308 v3 下不签（v2 修复不回退；禁重复方言）；
    ④ tp/fn 登记对拍（v1=248/24、v2=247/25）不折算成败；
    ⑤ recall 分级判读只落盘不断言（INV-6 同纪律）。
    """
    assert replay_run.metrics_path.exists(), "指标 JSON 未落盘"
    assert not replay_run.failures, (
        f"回放失败 {len(replay_run.failures)} 对（失败不冒充边界，INV-4）"
    )
    assert not replay_run.blocked, (
        f"残判阻断 {len(replay_run.blocked)} 对——部分覆盖不宣称完成；"
        "次日同令续跑（缓存落盘即续跑状态）"
    )
    pair = replay_run.pair
    merged = replay_run.merged_pair
    residual_fp = replay_run.metrics["residual_judge"]["residual_fp"]
    assert pair["fp"] == 0, f"基线 fp={pair['fp']} != 0——硬闸破，立即停报"
    assert merged["fp"] == 0, f"合并 fp={merged['fp']} != 0——硬闸破，立即停报"
    assert residual_fp == 0, f"残判 fp={residual_fp} != 0——硬闸破，立即停报"
    # fp 保持钉（v3 验收：v2 修复不回退；禁重复方言 cell∈{not_duplicate,doubtful}）
    flip = replay_run.fp_flip
    assert flip["v1_signed"] is True, "v1 基线 fp 对非 signed——基线漂移（对拍红）"
    assert flip["v3_signed"] is False, (
        f"fp 对 v3 下 signed=True——fp 回潮（cell={flip['v3_cell']!r}），停报"
    )
    assert flip["v3_cell"] in ("not_duplicate", "doubtful")
    # tp/fn 登记（v3 dev 面无硬闸；v1=248/24、v2=247/25 对拍落盘，不折算成败）
    print(f"[C5R-OOSV3] tp/fn 登记 v3={merged['tp']}/{merged['fn']} "
          f"（v1=248/24、v2=247/25 对拍）")
    print(f"[C5R-OOSV3] 锚对读 verdict={replay_run.anchor_read['verdict']} "
          f"merged recall={merged['recall']:.6f} "
          f"delta={replay_run.anchor_read['recall_delta']:+.6f}")


# ---------- 场景 7 · 残判层：五字段输出契约不动 ----------

def test_c5r_oos_residual_public_contract_untouched(replay_run):
    signed_n = 0
    auto_n = 0
    for item in replay_run.items + replay_run.boundary_items:
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


# ---------- 场景 8 · 分层/归因/计量落盘 + 内部一致性 ----------

def test_c5r_oos_stratified_attribution_metering_written(replay_run):
    metrics = json.loads(replay_run.metrics_path.read_text(encoding="utf-8"))
    for st in ("S_MULTI", "S_UPG", "S_CROSS", "S_HOLD"):
        assert st in metrics["stratified"], f"分层缺 {st}"
    assert metrics["role_ambiguous"]["count"] == DEV_FROZEN["role_ambiguous"]
    assert len(metrics["role_ambiguous"]["items"]) == 2

    lines = replay_run.attribution_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == len(replay_run.items) + len(replay_run.boundary_items)
    records = [json.loads(line) for line in lines]
    cls: dict[str, int] = {}
    for r in records:
        cls[r["class"]] = cls.get(r["class"], 0) + 1
    if len(replay_run.blocked) == 0:
        assert cls.get("tp", 0) == replay_run.merged_pair["tp"]
        assert cls.get("fp", 0) == replay_run.merged_pair["fp"]
        assert cls.get("fn", 0) == replay_run.merged_pair["fn"]
        assert cls.get("tn", 0) == replay_run.merged_pair["tn"]
    assert cls.get("gold_boundary", 0) == DEV_FROZEN["边界case/疑难case"]
    budget = metrics["residual_judge"]["budget"]
    assert budget["daily_token_budget"] == DAILY_TOKEN_BUDGET
    if budget["live_calls"] > 0:
        assert BUDGET_PATH.exists(), "预算计数器未落盘（决策 #14 违例）"
        counter = json.loads(BUDGET_PATH.read_text(encoding="utf-8"))
        assert counter["used_tokens"] == budget["budget_used_tokens"]
        metering = replay_run.metrics_path.parent / (
            f"c5r-oos-api-{replay_run.run_id}.jsonl")
        assert metering.exists(), "API 计量 JSONL 未落盘"


# ---------- 场景 9 · 边界族签发率 + 顺序翻案格 登记（归因批/臂C 输入） ----------

def test_c5r_oos_boundary_family_and_flip_registration(replay_run):
    """场景 9（v3 验收 dev 面登记+硬闸）：
    ① 边界族签发率不劣于 v2 硬断言：v3 signed ≤ 30/34（v1=29、v2=30 对拍
       落盘；新增签发对全量登记不折算成败）；
    ② 顺序翻案格零新增（对 v1 基 25 对族外零新增，臂C 同口径）；
    ③ role_ambiguous 对（7e7704f5 v1 签发边界对）方向登记。
    """
    bf = replay_run.boundary_family
    assert bf["v3_signed"] <= 30, (
        f"边界族签发 v3={bf['v3_signed']} > 30——劣于 v2（30/34），停报"
    )
    od = replay_run.order_flip_diff
    assert od["new_flips_in_v3"] == [], (
        f"v2 新增顺序翻案格 {len(od['new_flips_in_v3'])} 对"
        f"（{[p[:16] for p in od['new_flips_in_v3'][:5]]}…）——翻案族回潮，停报"
    )
    # 登记打印（报告引用）
    print(f"[C5R-OOSV3] 边界族签发率 v1={bf['v1_signed']}/{bf['total']} → "
          f"v3={bf['v3_signed']}/{bf['total']}（Δ={bf['delta']}）")
    print(f"[C5R-OOSV3] 顺序翻案格 v1={od['v1_flip_count']} → "
          f"v3={od['v3_flip_count']}（新增={len(od['new_flips_in_v3'])}、"
          f"解消={len(od['resolved_in_v3'])}）")
    ra = replay_run.metrics["role_ambiguous"]["items"]
    for item in ra:
        print(f"[C5R-OOSV3] role_ambiguous {item['pair_id'][:16]} "
              f"cell={item['residual_cell']} signed={item['residual_signed']}")
