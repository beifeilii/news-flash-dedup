"""P1 合流树考试工装②：分层验证工装（2026-10-09，主窗口派单；用户令：轻、快）。

分层：smoke 模式 = 金标抽 50 件（分层按比例抽断，种子钉死）+ 等效锚断言；
full 模式 = 697 件全量（=355 考核对全量：306 重复 + 49 不重复；挂起 4 对
随流不考核列示，gold-交付 ③/指标计算器口径同一）。

锚断言 296/0/10/49 等效性论证（派单授权"读 gold-交付\\指标计算器.py 后定"）：
- 四格定义逐行同构指标计算器.py:70-117（正例口径：仅"重复"判正；边界
  "拿不准（转人工）"严格口径计入 fn/tn；挂起 考核=false 不入格单列）；
- fp=0 硬闸 = 原语义同一（标注"不重复"而判"重复"=误杀，任一即停 exit 3）；
- 锚口径挂 verdict_residual（残判合并口径——296/0/10/49 锚产出面同一）；
  verdict_proof（P1 证明口径）与链原生三态只分列报告，但任一口径出 fp
  同样即停（派单"任一 fp 即停"）；
- full 模式：精确锚 tp=296/fp=0/fn=10/tn=49；判官边界不确定性带
  （工作进度 R315 铁案：药明康德对 f4df3e77 跨会话位级不确定性 ±1 对，
  两次全真同装跑 296 与 295 均实测）→ tp∈{295,296}∧fn∈{10,11}∧tn=49
  记 pass_r315_band（报告如实标带，不静默）；
- smoke 模式（50 件分层抽样）：绝对数不可比 → 按比例抽断——fp==0 硬闸
  同语义 + precision==1.0 + fn ≤ ⌈n_dup × 10/306⌉（锚 fn 率 3.27% 按样本
  重复对数上取整折算；n=50→43 重复对→fn≤2），超出即 exit 4。

链原生三态：每对经 decide_for_task（RuleFactSupply 规则抽取，判官进链
开关关=链原生）产出 重复/不重复/边界case/疑难case，按指标计算器正例
口径折算四格（重复→判正；不重复/边界→未判正）分列。

复用：判官相位=scripts\\judge_concurrent_executor.py（①，决策 #29 预算/
禁缓存同款）；官方 CSV=gold-交付\\指标计算器.py 子进程复算（复用优先，
本件四格算法与其逐行同构，双路径互证）。

CLI（p1int 仓根；venv 主树 .venv-v1）：
    python scripts\\vfy_layered_harness.py --mode smoke --run-id h1009s \
        --workers 6 --token-budget 200000
    python scripts\\vfy_layered_harness.py --mode full --run-id h1009f \
        --workers 8 --token-budget 2000000
产物：--out-dir（缺省 workspace log\\temp\\vfy-layered-{run_id}\\）下
verdicts.json / assessment.json / 输出数据-②格式-{口径}.jsonl /
指标明细-{口径}.csv（计算器复算）。

exit：0=pass（exact/r315_band/smoke 折算均过）；2=用法错；3=fp 硬闸停；
4=锚断言失败；5=判官执行器红（预算/禁缓存/对账）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import subprocess
import sys
from collections import Counter
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent
_WORKSPACE_ROOT = _REPO_ROOT.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))
sys.path.insert(0, str(_HERE))

from judge_concurrent_executor import (  # noqa: E402  工装①复用
    JudgeExecutorError, JudgePairRequest, run_judge_batch,
    VERDICT_DUP, VERDICT_NONDUP, VERDICT_UNSURE)

GOLD_DIR = _WORKSPACE_ROOT / "gold-交付"
GOLDMAP_JSON = _WORKSPACE_ROOT / "news-flash-dedup" / "log" / "temp" / "golde2e-goldmap.json"
LABELS_JSONL = GOLD_DIR / "③原始标注-Fact标签集-359.jsonl"
INPUTS_JSONL = GOLD_DIR / "①输入数据-系统输入报文-355.jsonl"
CALCULATOR_PY = GOLD_DIR / "指标计算器.py"

ANCHOR = {"tp": 296, "fp": 0, "fn": 10, "tn": 49}     # golde2e-assess.py:61 同一
ANCHOR_DUP_TOTAL = 306                                # 296+10（锚重复对基数）
R315_TP = {295, 296}                                  # R315 判官边界不确定性带
R315_FN = {10, 11}


class HarnessStop(RuntimeError):
    """fp 硬闸/锚失败——落证即停。"""

    def __init__(self, exit_code: int, reason: str, payload: dict | None = None):
        super().__init__(reason)
        self.exit_code = exit_code
        self.payload = payload or {}


# ---------------------------------------------------------------- 金标载入 / 抽样

def load_labels(path: Path = LABELS_JSONL) -> list[dict]:
    """③原始标注：pair_id/标注/考核 三面（指标计算器 read_jsonl 同型）。"""
    rows = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def load_pair_texts(goldmap_path: Path = GOLDMAP_JSON,
                    inputs_path: Path = INPUTS_JSONL) -> dict:
    """pair_id → (text_a, text_b)。主源 goldmap（e2e 钉死面）；缺对回退 ①。"""
    texts: dict = {}
    if goldmap_path.exists():
        goldmap = json.loads(goldmap_path.read_text(encoding="utf-8"))
        for pair in goldmap["pairs"]:
            texts[pair["pair_id"]] = (pair["text_a"], pair["text_b"])
    if inputs_path.exists():
        with inputs_path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                row = json.loads(line)
                texts.setdefault(row["pair_id"],
                                 (row["A篇原文"], row["B篇原文"]))
    return texts


def stratified_sample(assessed: list[dict], n: int, seed: int) -> list[dict]:
    """分层按比例抽断（确定性）：按标注分层、层内按 pair_id 排序后种子抽样，
    层额 round 比例分配（dup:nondup = 306:49 → n=50 ⇒ 43+7）。"""
    dup = sorted((r for r in assessed if r["标注"] == "重复"),
                 key=lambda r: r["pair_id"])
    nondup = sorted((r for r in assessed if r["标注"] != "重复"),
                    key=lambda r: r["pair_id"])
    total = len(dup) + len(nondup)
    n_dup = min(len(dup), int(round(n * len(dup) / total)))
    n_nondup = min(len(nondup), n - n_dup)
    rng = random.Random(seed)
    picked = rng.sample(dup, n_dup) + rng.sample(nondup, n_nondup)
    return sorted(picked, key=lambda r: r["pair_id"])


# ---------------------------------------------------------------- 链原生三态

def _chain_native_verdict(pair_id: str, text_a: str, text_b: str,
                          fact_supply) -> str:
    """对级链原生三态：decide_for_task（判官进链开关关=链原生规则链）。
    重复/不重复直返；边界case/疑难case → 拿不准（转人工）（计算器词汇）。"""
    from news_flash_dedup.decide import service as decide_service

    def _record(side: str, text: str, seq: int) -> dict:
        rid = hashlib.sha256(f"{pair_id}|{side}".encode("utf-8")).hexdigest()
        return {"record_id": rid, "item_id": f"{pair_id[:12]}-{side}",
                "text": text,
                "raw_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "scope_id": "exam", "business_date": "2026-09-09",
                "arrival_seq": seq, "facts": fact_supply(rid, text)}

    outcome = decide_service.decide_for_task(
        _record("a", text_a, 1), [], current=_record("b", text_b, 2),
        coverage_complete=True)
    if outcome.decision == "重复":
        return VERDICT_DUP
    if outcome.decision == "不重复":
        return VERDICT_NONDUP
    return VERDICT_UNSURE


# ---------------------------------------------------------------- 四格（指标计算器逐行同构）

def compute_cells(rows: list[dict], preds: dict) -> dict:
    """指标计算器.py:75-117 算法逐行同构：正例口径仅"重复"判正；挂起
    （考核=false）单列不入格；缺判定按未判重复计入 fn/tn 并列示。
    rows=[{pair_id, 标注, 考核}], preds={pair_id: 判定}。"""
    tp = fp = fn = tn = 0
    boundary, suspended, missing = [], [], []
    detail = []
    for row in rows:
        pid, gold = row["pair_id"], row["标注"]
        if not row.get("考核", gold in ("重复", "不重复")):
            suspended.append((pid, gold))
            continue
        pred = preds.get(pid)
        if pred is None:
            missing.append(pid)
            pred = "（缺判定）"
        positive = (pred == VERDICT_DUP)
        cell = ("tp" if positive else "fn") if gold == "重复" \
            else ("fp" if positive else "tn")
        if cell == "tp":
            tp += 1
        elif cell == "fp":
            fp += 1
        elif cell == "fn":
            fn += 1
        else:
            tn += 1
        if pred == VERDICT_UNSURE:
            boundary.append((pid, gold, cell))
        detail.append({"pair_id": pid, "gold": gold, "pred": pred, "cell": cell})
    precision = tp / (tp + fp) if tp + fp else None
    recall = tp / (tp + fn) if tp + fn else None
    return {"tp": tp, "fp": fp, "fn": fn, "tn": tn,
            "precision": precision, "recall": recall,
            "assessed": tp + fp + fn + tn,
            "boundary": boundary, "suspended": suspended,
            "missing_pred": missing, "detail": detail}


# ---------------------------------------------------------------- 锚断言

def assert_anchors(cells: dict, *, mode: str, caliber: str) -> str:
    """返锚判定结论串；fp>0 → HarnessStop(3)；锚破 → HarnessStop(4)。"""
    if cells["fp"] > 0:
        raise HarnessStop(3, f"fp 硬闸：{caliber} 口径 fp={cells['fp']}",
                          {"caliber": caliber,
                           "fp_pairs": [d for d in cells["detail"]
                                        if d["cell"] == "fp"]})
    if mode == "full":
        exact = all(cells[k] == v for k, v in ANCHOR.items())
        if exact:
            return "pass_exact"
        in_band = (cells["tp"] in R315_TP and cells["fn"] in R315_FN
                   and cells["tn"] == ANCHOR["tn"])
        if in_band:
            return "pass_r315_band"
        raise HarnessStop(
            4, f"full 锚断言失败（{caliber}）："
               f"{ {k: cells[k] for k in ANCHOR} } vs 锚 {ANCHOR}（R315 带外）",
            {"caliber": caliber, "cells": {k: cells[k] for k in ANCHOR}})
    # smoke：按比例抽断折算（docstring 等效性论证）
    n_dup = sum(1 for d in cells["detail"] if d["gold"] == "重复")
    fn_cap = math.ceil(n_dup * ANCHOR["fn"] / ANCHOR_DUP_TOTAL)
    if cells["precision"] != 1.0 or cells["fn"] > fn_cap:
        raise HarnessStop(
            4, f"smoke 折算锚失败（{caliber}）：precision={cells['precision']} "
               f"fn={cells['fn']} > cap={fn_cap}（n_dup={n_dup}）",
            {"caliber": caliber, "cells": {k: cells[k] for k in ANCHOR},
             "fn_cap": fn_cap, "n_dup": n_dup})
    return f"pass_smoke(fn_cap={fn_cap},n_dup={n_dup})"


# ---------------------------------------------------------------- 计算器复算（复用优先）

def run_calculator(out_jsonl: Path, labels_jsonl: Path, csv_path: Path) -> int:
    """gold-交付\\指标计算器.py 子进程官方复算（双路径互证）。
    PYTHONIOENCODING=utf-8 钉死子进程 stdout 编码（本机 cp936 默认会混码）。"""
    import os
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.run(
        [sys.executable, str(CALCULATOR_PY), str(out_jsonl),
         str(labels_jsonl), str(csv_path)],
        capture_output=True, text=True, encoding="utf-8", env=env)
    return proc.returncode


def write_pred_jsonl(rows: list[dict], preds: dict, path: Path) -> None:
    """②输出数据同型：{pair_id, 判定}（计算器输入契约）。"""
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            pid = row["pair_id"]
            fh.write(json.dumps({"pair_id": pid,
                                 "判定": preds.get(pid, "（缺判定）")},
                                ensure_ascii=False) + "\n")


# ---------------------------------------------------------------- 主流程

def run_harness(*, mode: str, run_id: str, workers: int = 6,
                token_budget: int = 2_000_000, seed: int = 42,
                sample_size: int = 50, out_dir: Path | None = None,
                goldmap_path: Path = GOLDMAP_JSON,
                labels_path: Path = LABELS_JSONL,
                chain_native: bool = True,
                verdicts_in: Path | None = None,
                out_dir_exist_ok: bool = False,
                progress=None) -> dict:
    """分层验证主入口（③ runner 复用本函数；CLI=薄壳）。"""
    out_dir = out_dir or (_WORKSPACE_ROOT / "log" / "temp"
                          / f"vfy-layered-{run_id}")
    out_dir.mkdir(parents=True, exist_ok=out_dir_exist_ok)
    labels = load_labels(labels_path)
    texts = load_pair_texts(goldmap_path)
    assessed = [r for r in labels
                if r.get("考核", r["标注"] in ("重复", "不重复"))]
    missing_texts = [r["pair_id"] for r in assessed if r["pair_id"] not in texts]
    if missing_texts:
        raise HarnessStop(4, f"金标对缺正文 {len(missing_texts)} 对",
                          {"pair_ids": missing_texts[:10]})
    exam_rows = (assessed if mode == "full"
                 else stratified_sample(assessed, sample_size, seed))
    requests = [JudgePairRequest(pair_id=r["pair_id"],
                                 text_a=texts[r["pair_id"]][0],
                                 text_b=texts[r["pair_id"]][1],
                                 meta={"gold": r["标注"]})
                for r in exam_rows]

    # 判官相位（①复用；verdicts_in=断点复跑零 API）
    if verdicts_in is not None:
        verdicts = json.loads(verdicts_in.read_text(encoding="utf-8"))["results"]
        usage = {"resume_from": str(verdicts_in)}
        stopped_reason = None
    else:
        report = run_judge_batch(
            requests, run_id=f"{run_id}-judge", workers=workers,
            token_budget=token_budget,
            usage_ledger_path=str(out_dir / "judge-usage.jsonl"),
            progress=progress)
        verdicts = [
            {"pair_id": r.pair_id, "cell": r.cell, "signed": r.signed,
             "verdict_residual": r.verdict_residual,
             "verdict_proof": r.verdict_proof,
             "proof_dup_issued": r.proof_dup_issued,
             "proof_nd_issued": r.proof_nd_issued,
             "proof_failures": list(r.proof_failures),
             "suspicion_score": r.suspicion_score, "error": r.error}
            for r in report.results]
        usage = report.usage
        stopped_reason = report.stopped_reason
    (out_dir / "verdicts.json").write_text(json.dumps(
        {"run_id": run_id, "mode": mode, "results": verdicts},
        ensure_ascii=False, indent=2), encoding="utf-8")
    by_pair = {v["pair_id"]: v for v in verdicts}

    # 链原生三态相位（规则链，判官零 API）
    chain_preds: dict = {}
    if chain_native:
        from news_flash_dedup.recall.service import RuleFactSupply
        supply = RuleFactSupply()
        for i, row in enumerate(exam_rows, 1):
            pid = row["pair_id"]
            chain_preds[pid] = _chain_native_verdict(
                pid, texts[pid][0], texts[pid][1], supply)
            if progress is not None:
                progress(f"chain-native {i}/{len(exam_rows)}")

    # 三口径评分（判官合并=锚口径 residual；证明口径 proof；链原生）分列
    calibers = {
        "judge_residual": {pid: v["verdict_residual"]
                           for pid, v in by_pair.items()},
        "judge_proof": {pid: v["verdict_proof"] for pid, v in by_pair.items()},
    }
    if chain_native:
        calibers["chain_native"] = chain_preds
    scores: dict = {}
    anchor_verdicts: dict = {}
    pending_stop: HarnessStop | None = None   # 先落证后停（assessment 必写）
    for name, preds in calibers.items():
        cells = compute_cells(exam_rows, preds)
        scores[name] = {k: v for k, v in cells.items() if k != "detail"}
        # fp 硬闸任一口径（派单"任一 fp 即停"）；锚断言只挂锚口径
        if cells["fp"] > 0 and pending_stop is None:
            pending_stop = HarnessStop(
                3, f"fp 硬闸：{name} 口径 fp={cells['fp']}",
                {"caliber": name,
                 "fp_pairs": [d for d in cells["detail"] if d["cell"] == "fp"]})
        try:
            anchor_verdicts[name] = (
                assert_anchors(cells, mode=mode, caliber=name)
                if name == "judge_residual" else "report_only")
        except HarnessStop as error:
            anchor_verdicts[name] = f"FAIL: {error}"
            if pending_stop is None:
                pending_stop = error
        # ②格式输出 + 计算器官方复算（双路径互证）
        pred_jsonl = out_dir / f"输出数据-②格式-{name}.jsonl"
        write_pred_jsonl(exam_rows, preds, pred_jsonl)
        rc = run_calculator(pred_jsonl, labels_path,
                            out_dir / f"指标明细-{name}.csv")
        scores[name]["calculator_rc"] = rc
        scores[name]["detail"] = cells["detail"]

    assessment = {
        "schema": "vfy-layered-assessment-v1", "run_id": run_id, "mode": mode,
        "seed": seed, "sample_size": len(exam_rows) if mode == "smoke" else None,
        "exam_pairs": len(exam_rows),
        "anchor": ANCHOR, "anchor_verdicts": anchor_verdicts,
        "stopped_reason": (pending_stop and f"stopped:{pending_stop}") or
                          stopped_reason,
        "judge_usage": usage,
        "scores": scores,
        "counters": dict(Counter(v["verdict_residual"]
                                 for v in by_pair.values())),
    }
    (out_dir / "assessment.json").write_text(json.dumps(
        assessment, ensure_ascii=False, indent=2), encoding="utf-8")
    if pending_stop is not None:
        raise pending_stop
    return assessment


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--mode", choices=("smoke", "full"), required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--token-budget", type=int, default=2_000_000)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--sample-size", type=int, default=50)
    parser.add_argument("--goldmap", default=str(GOLDMAP_JSON))
    parser.add_argument("--labels", default=str(LABELS_JSONL))
    parser.add_argument("--out-dir", default=None)
    parser.add_argument("--no-chain-native", action="store_true")
    parser.add_argument("--verdicts-in", default=None,
                        help="断点复跑：复用既有 verdicts.json（零 API）")
    args = parser.parse_args()
    assessment = run_harness(
        mode=args.mode, run_id=args.run_id, workers=args.workers,
        token_budget=args.token_budget, seed=args.seed,
        sample_size=args.sample_size,
        out_dir=Path(args.out_dir) if args.out_dir else None,
        goldmap_path=Path(args.goldmap), labels_path=Path(args.labels),
        chain_native=not args.no_chain_native,
        verdicts_in=Path(args.verdicts_in) if args.verdicts_in else None,
        progress=lambda *a: print(f"[VFY] {a}", flush=True))
    av = assessment["anchor_verdicts"]
    print(f"[VFY-DONE] run={assessment['run_id']} mode={assessment['mode']} "
          f"anchors={av} stopped={assessment['stopped_reason']}")
    for name, score in assessment["scores"].items():
        print(f"[VFY-DONE] {name}: tp={score['tp']} fp={score['fp']} "
              f"fn={score['fn']} tn={score['tn']} "
              f"P={score['precision']} R={score['recall']}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except HarnessStop as error:
        print(f"[VFY-STOP] {error} "
              f"{json.dumps(error.payload, ensure_ascii=False)[:400]}")
        sys.exit(error.exit_code)
    except JudgeExecutorError as error:
        print(f"[VFY-STOP] 判官执行器红：{error}")
        sys.exit(5)
