"""P1 合流树考试工装③：金标+T0909 双轨同考 runner 脚手架（2026-10-09，
主窗口派单；用户令：轻、快）。

双轨（独立 run_id/目录/注册表防撞名，vfy-dual-run.py 撞名闸同款纪律）：
- gold 轨：scripts\\vfy_layered_harness.py（②）全套——smoke（默认，50 件
  分层+折算锚）/full（697 件=355 对全量+296/0/10/49 锚）；fp=0 硬闸
  任一 fp 即停（exit 3，后续轨不启动）；
- t0909 轨：T 数据 2026-09-09 冻结件抽样 ~1,000 件代表样本（种子钉死，
  默认决策行 tdata-exec-tdata0909-decisions.jsonl 实跑产出面抽——
  candidate_count>0 的 committed 行=真实召回对；正文经
  tdata-stream-2026-09-09.json item_id 回指）→ 判官并发执行器（①）+
  链原生三态（decide_for_task，候选=冻结决策行的真实候选集）。
  主窗口裁定（2026-10-09）：冻结目录=主树 log\\temp\\tdata-frozen-1009\\
  tdata0909\\（22:38 接管停跑亲冻，MANIFEST-sha256.txt 钉 decisions
  sha256=1C90AB61…，对拍不符 fail-closed exit 4）；stream 不入冻结包，
  正文源=主树 tdata-stream-2026-09-09.json 原件（--t-frozen-dir/--t-stream/
  --t-decisions 可覆盖；原散装件回退作废备案，仅冻结目录缺席时兜底）。

评分 = 判官合并口径（verdict_residual，锚口径）+ 证明口径（verdict_proof）
+ 链原生三态（decide_for_task 三态）**分列**：
- gold 轨有标注 → 三口径四格两率 + 锚断言（②全权）；
- t0909 轨无标注 → 分布面（签发率 signed/pairs、cell 分布、链原生三态
  分布、边界率），不编造四格；判官 failure/invalid 臂率 >
  --t-max-failure-rate（默认 5%）= 基建红 exit 5。

防撞名：--run-id 为基，两轨 run_id={base}-gold/{base}-t0909；输出根
{out-root}\\exam-{base}\\ 存在且非 --resume → exit 6；注册表
exam-{base}-registry.json 逐轨登记（run_id/目录/产物/索引·集合命名
规约：未来 HTTP 在环升级时 ES 索引/Milvus 集合一律 {run_id}-*-v1 前缀，
本离线相位零集群对象，规约入册备守）。

CLI（p1int 仓根；venv 主树 .venv-v1）：
    python scripts\\exam_dual_track_runner.py --run-id e1009a \
        --gold-mode smoke --t-sample 1000 --workers 6
产物：{out-root}\\exam-{base}\\{gold,t0909}\\… + summary.json + registry。

exit：0=pass；2=用法错；3=fp 硬闸停；4=锚/抽样红；5=判官/基建红；6=撞名。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent
_WORKSPACE_ROOT = _REPO_ROOT.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))
sys.path.insert(0, str(_HERE))

from judge_concurrent_executor import (  # noqa: E402  工装①复用
    JudgeExecutorError, JudgePairRequest, run_judge_batch,
    VERDICT_DUP, VERDICT_NONDUP, VERDICT_UNSURE)
from vfy_layered_harness import (  # noqa: E402  工装②复用
    HarnessStop, run_harness)

MAIN_TREE_TEMP = (_WORKSPACE_ROOT / "news-flash-dedup" / "log" / "temp")
DEFAULT_T_STREAM = MAIN_TREE_TEMP / "tdata-stream-2026-09-09.json"
DEFAULT_T_DECISIONS = MAIN_TREE_TEMP / "tdata-exec-tdata0909-decisions.jsonl"
# 主窗口裁定（2026-10-09）：冻结目录=主树 log\temp\tdata-frozen-1009\tdata0909\
# （22:38 接管停跑亲冻，MANIFEST 钉 decisions sha256=1C90AB61…）；原回退路径
# （主树 log\temp 散装件）作废备案——仅冻结目录不在场时才作最后兜底。
DEFAULT_T_FROZEN_DIR = MAIN_TREE_TEMP / "tdata-frozen-1009" / "tdata0909"
DEFAULT_OUT_ROOT = _WORKSPACE_ROOT / "log" / "temp"
T_BUSINESS_DATE = "2026-09-09"
T_SCOPE = "tdata0909"


class RunnerStop(RuntimeError):
    def __init__(self, exit_code: int, reason: str, payload: dict | None = None):
        super().__init__(reason)
        self.exit_code = exit_code
        self.payload = payload or {}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------- T 轨数据面

def _sha256_file(path: Path) -> str:
    import hashlib as _hashlib
    digest = _hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _verify_manifest(frozen_dir: Path, decisions_path: Path) -> str | None:
    """冻结完整性对拍：MANIFEST-sha256.txt（冻结根，即 frozen_dir.parent）钉
    decisions 行 sha256；在场必对拍，不符 → RunnerStop(4)（冻结件被改动=
    考试基准失效，fail-closed）。返实算 sha（对拍过或未钉则 None）。"""
    manifest = frozen_dir.parent / "MANIFEST-sha256.txt"
    if not manifest.exists():
        return None
    pinned = None
    # utf-8-sig：MANIFEST 亲冻件带 BOM（EF BB BF 实测），不剥则首行键名失配
    for line in manifest.read_text(encoding="utf-8-sig").splitlines():
        parts = [p.strip() for p in line.split("\t")]
        if len(parts) == 3 and parts[0] == frozen_dir.name:
            pinned = parts[2].upper()
    if pinned is None:
        return None
    actual = _sha256_file(decisions_path)
    if actual != pinned:
        raise RunnerStop(
            4, f"冻结对拍失败：{decisions_path.name} sha256={actual[:16]}… "
               f"!= MANIFEST 钉值 {pinned[:16]}…（冻结件已变，考试基准失效）",
            {"decisions": str(decisions_path), "actual": actual,
             "pinned": pinned})
    return actual


def _resolve_t_paths(frozen_dir: Path | None, stream: Path | None,
                     decisions: Path | None) -> tuple[Path, Path, str | None]:
    """主窗口裁定形：冻结目录在场 → decisions 必取冻结件（MANIFEST 对拍），
    stream 不入冻结包（MANIFEST 只钉 decisions；0909 流件主树原件=唯一权威
    正文源）；冻结目录不在场才回退主树散装件（作废备案，仅兜底）。
    返 (stream, decisions, manifest_sha|None)。两侧缺一 → RunnerStop(4)。"""
    frozen_dir = frozen_dir or DEFAULT_T_FROZEN_DIR
    frozen_live = frozen_dir.exists()
    if frozen_live:
        decisions = decisions or (frozen_dir
                                  / "tdata-exec-tdata0909-decisions.jsonl")
    stream = stream or DEFAULT_T_STREAM
    decisions = decisions or DEFAULT_T_DECISIONS
    for path in (stream, decisions):
        if not path.exists():
            raise RunnerStop(4, f"T 冻结件缺失：{path}")
    manifest_sha = None
    if frozen_live and decisions.resolve().parent == frozen_dir.resolve():
        manifest_sha = _verify_manifest(frozen_dir, decisions)
    return stream, decisions, manifest_sha


def _load_stream_texts(path: Path) -> dict:
    stream = json.loads(path.read_text(encoding="utf-8"))
    return {item["item_id"]: item["text"] for item in stream["items"]}


def _sample_t_rows(decisions_path: Path, n: int, seed: int) -> list[dict]:
    """决策行分层抽样：candidate_count>0 且 committed 的考核行（真实召回对
    承载行），按 arrival_seq 排序后种子抽 n（不足全取）。"""
    rows: list[dict] = []
    with decisions_path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if row.get("committed") and row.get("candidate_count", 0) > 0:
                rows.append(row)
    rows.sort(key=lambda r: r["arrival_seq"])
    rng = random.Random(seed)
    if len(rows) > n:
        rows = sorted(rng.sample(rows, n), key=lambda r: r["arrival_seq"])
    return rows


def _t_record(item_id: str, text: str, seq: int, fact_supply) -> dict:
    rid = hashlib.sha256(f"{T_SCOPE}|{item_id}".encode("utf-8")).hexdigest()
    return {"record_id": rid, "item_id": item_id, "text": text,
            "raw_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "scope_id": T_SCOPE, "business_date": T_BUSINESS_DATE,
            "arrival_seq": seq, "facts": fact_supply(rid, text)}


def _build_t_requests(rows: list, texts: dict,
                      max_pairs_per_item: int) -> tuple[list, dict, list]:
    """T 轨判官对集装配 + 缺失点名（2026-10-09 主窗口令："1,000 抽 999"
    类缺失不许静默——逐行点名原因）。

    返 (requests, pair_backref, skips)。skips 逐条 {item_id, reason, detail}：
    - candidate_text_missing   候选 item_id 在流件正文表缺席（正文缺）；
    - candidate_text_identical 候选正文与当前条逐字同（自对，判了无义）；
    - candidates_list_empty    行 candidate_count>0 但 candidates 表空（数据缺）。
    """
    requests: list[JudgePairRequest] = []
    pair_backref: dict = {}
    skips: list[dict] = []
    for row in rows:
        candidates = sorted(row.get("candidates") or [],
                            key=lambda c: -c.get("score", 0.0))
        if not candidates:
            skips.append({"item_id": row["item_id"],
                          "reason": "candidates_list_empty",
                          "detail": f"candidate_count={row.get('candidate_count')}"
                                    f" 但 candidates 表空"})
            continue
        for cand in candidates[:max_pairs_per_item]:
            cand_text = texts.get(cand["item_id"])
            if cand_text is None:
                skips.append({"item_id": row["item_id"],
                              "candidate_item_id": cand["item_id"],
                              "reason": "candidate_text_missing",
                              "detail": "候选 item_id 流件正文表缺席"})
                continue
            if cand_text == row["text"]:
                skips.append({"item_id": row["item_id"],
                              "candidate_item_id": cand["item_id"],
                              "reason": "candidate_text_identical",
                              "detail": "候选正文与当前条逐字同（自对）"})
                continue
            pid = hashlib.sha256(
                f"{row['record_id']}|{cand['item_id']}".encode("utf-8")).hexdigest()
            requests.append(JudgePairRequest(
                pair_id=pid, text_a=cand_text, text_b=row["text"],
                meta={"current_item_id": row["item_id"],
                      "candidate_item_id": cand["item_id"],
                      "candidate_score": cand.get("score")}))
            pair_backref[pid] = (row["item_id"], cand["item_id"])
    return requests, pair_backref, skips


def run_t0909_track(*, run_id: str, sample: int, max_pairs_per_item: int,
                    workers: int, token_budget: int, seed: int,
                    out_dir: Path, stream_path: Path, decisions_path: Path,
                    max_failure_rate: float, progress=None) -> dict:
    """T0909 轨：冻结决策行抽样 → 判官（①）+ 链原生三态 → 分布评分。
    证据先行（2026-10-09 主窗口令）：判官批毕先落 verdicts.json（执行器
    evidence_dir 内落 + 本层显式落，双保险），再进链原生相位与一切闸。"""
    from news_flash_dedup.recall.service import RuleFactSupply
    from news_flash_dedup.decide import service as decide_service

    texts = _load_stream_texts(stream_path)
    rows = _sample_t_rows(decisions_path, sample, seed)
    if not rows:
        raise RunnerStop(4, "T 轨抽样为空（candidate_count>0 行零）")

    requests, pair_backref, skips = _build_t_requests(
        rows, texts, max_pairs_per_item)
    if not requests:
        raise RunnerStop(4, "T 轨判官对集为空（候选正文回指全失败）",
                         {"skips": skips[:20]})

    report = run_judge_batch(
        requests, run_id=f"{run_id}-judge", workers=workers,
        token_budget=token_budget,
        usage_ledger_path=str(out_dir / "judge-usage.jsonl"),
        evidence_dir=str(out_dir),
        progress=progress)

    # 证据先行：判毕即落 verdicts.json（链原生相位/失败率闸/任何后续之前）
    judge_results = [
        {"pair_id": r.pair_id, "cell": r.cell,
         "verdict_residual": r.verdict_residual,
         "verdict_proof": r.verdict_proof,
         "backref": pair_backref.get(r.pair_id)}
        for r in report.results]
    (out_dir / "verdicts.json").write_text(json.dumps(
        {"schema": "exam-t0909-verdicts-v1", "run_id": run_id,
         "note": "证据先行：判毕即落（一切后续闸之前）",
         "arms": report.arms, "usage": report.usage,
         "stopped_reason": report.stopped_reason,
         "results": judge_results},
        ensure_ascii=False, indent=2), encoding="utf-8")

    # 链原生三态：每行一次 decide_for_task（真实候选集进 required）
    supply = RuleFactSupply()
    chain_rows: list[dict] = []
    for i, row in enumerate(rows, 1):
        current = _t_record(row["item_id"], row["text"],
                            int(row["arrival_seq"]), supply)
        cand_records = []
        for k, cand in enumerate(sorted(row["candidates"],
                                        key=lambda c: -c.get("score", 0.0))):
            cand_text = texts.get(cand["item_id"])
            if cand_text is None:
                continue
            cand_records.append(_t_record(
                cand["item_id"], cand_text,
                int(row["arrival_seq"]) - (len(row["candidates"]) - k), supply))
        if not cand_records:
            chain_rows.append({"item_id": row["item_id"],
                               "decision": None, "note": "候选正文缺失"})
            continue
        outcome = decide_service.decide_for_task(
            cand_records[0], cand_records[1:], current=current,
            coverage_complete=bool(row.get("coverage_complete", False)))
        chain_rows.append({"item_id": row["item_id"],
                           "decision": outcome.decision,
                           "internal_code": outcome.internal_code,
                           "duplicate_ids": list(outcome.duplicate_ids)})
        if progress is not None:
            progress(f"t-chain {i}/{len(rows)}")

    # 分布评分（无标注→不编造四格；判官合并/证明/链原生三列分列）
    judge_dist = Counter(r.verdict_residual for r in report.results)
    proof_dist = Counter(r.verdict_proof for r in report.results)
    cell_dist = Counter(r.cell for r in report.results)
    chain_dist = Counter(r["decision"] for r in chain_rows)
    arms = report.arms
    fail_arms = sum(1 for r in report.results
                    if r.cell in ("failure", "invalid")) * 2
    failure_rate = (fail_arms / arms) if arms else 0.0
    if failure_rate > max_failure_rate:
        raise RunnerStop(5, f"T 轨判官失败/invalid 臂率 {failure_rate:.2%} > "
                            f"{max_failure_rate:.2%}（基建红）",
                         {"arms": arms, "fail_arms": fail_arms})
    assessment = {
        "schema": "exam-t0909-assessment-v1", "run_id": run_id,
        "partition": T_BUSINESS_DATE, "seed": seed,
        "sample_rows": len(rows), "judge_pairs": len(requests),
        "max_pairs_per_item": max_pairs_per_item,
        # 缺失点名（主窗口令："1,000 抽 999"类缺口逐因列名，不静默）：
        # 采样边界（池<请求）+ 对集缺口逐条原因 + 预算截停对数。
        "sample_gap": {
            "requested": sample, "sampled": len(rows),
            "pool_shortfall": max(0, sample - len(rows)),
            "pairs_built": len(requests),
            "pair_gap": len(rows) * max_pairs_per_item - len(requests),
            "skips": skips,
            "budget_skipped": sum(1 for r in report.results
                                  if r.cell == "budget_skip"),
        },
        "stopped_reason": report.stopped_reason,
        "judge_usage": report.usage, "arms": arms,
        "failure_rate": failure_rate,
        "judge_merged_dist": dict(judge_dist),     # 判官合并口径（锚口径）
        "judge_proof_dist": dict(proof_dist),      # 证明口径
        "chain_native_dist": dict(chain_dist),     # 链原生三态
        "cell_dist": dict(cell_dist),
        "signed_rate": (cell_dist.get("signed", 0) / len(requests))
        if requests else None,
        "chain_rows": chain_rows,
        "judge_results": judge_results,
    }
    (out_dir / "assessment.json").write_text(json.dumps(
        assessment, ensure_ascii=False, indent=2), encoding="utf-8")
    return assessment


# ---------------------------------------------------------------- 主流程

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--tracks", default="gold,t0909",
                        help="逗号分隔：gold / t0909（缺省双轨，gold 先考）")
    parser.add_argument("--gold-mode", choices=("smoke", "full"),
                        default="smoke")
    parser.add_argument("--chain-with-judge", action="store_true",
                        help="gold 轨加跑链上回炉锚口径（harness 包②同款）")
    parser.add_argument("--gold-sample-size", type=int, default=50)
    parser.add_argument("--t-sample", type=int, default=1000)
    parser.add_argument("--t-max-pairs-per-item", type=int, default=1)
    parser.add_argument("--t-max-failure-rate", type=float, default=0.05)
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--gold-token-budget", type=int, default=300_000)
    parser.add_argument("--t-token-budget", type=int, default=2_000_000)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--t-frozen-dir", default=None)
    parser.add_argument("--t-stream", default=None)
    parser.add_argument("--t-decisions", default=None)
    parser.add_argument("--out-root", default=str(DEFAULT_OUT_ROOT))
    parser.add_argument("--resume", action="store_true",
                        help="撞名闸转在位：复用既有目录续跑")
    args = parser.parse_args()
    t0 = time.perf_counter()

    tracks = [t.strip() for t in args.tracks.split(",") if t.strip()]
    unknown = set(tracks) - {"gold", "t0909"}
    if unknown:
        raise RunnerStop(2, f"未知轨：{sorted(unknown)}")
    run_root = Path(args.out_root) / f"exam-{args.run_id}"
    # 撞名闸（vfy-dual-run 同款：存在且非 resume 即拒）
    if run_root.exists() and not args.resume:
        raise RunnerStop(6, f"撞名：{run_root} 已在场（--resume 续跑）")
    run_root.mkdir(parents=True, exist_ok=True)
    registry_path = Path(args.out_root) / f"exam-{args.run_id}-registry.json"
    registry = {"schema": "exam-registry-v1", "base_run_id": args.run_id,
                "born": _now(), "tracks": [],
                "naming_convention": {
                    "es_index_prefix": "{run_id}-*-v1",
                    "milvus_collection_prefix": "{run_id}-*-v1",
                    "note": "离线相位零集群对象；HTTP 在环升级时强制前缀防撞"}}
    summary: dict = {"schema": "exam-dual-summary-v1",
                     "base_run_id": args.run_id, "born": _now(),
                     "tracks": {}, "fp_gate": "armed"}

    if "gold" in tracks:
        gold_dir = run_root / "gold"
        gold_run_id = f"{args.run_id}-gold"
        if gold_dir.exists() and not args.resume:
            raise RunnerStop(6, f"撞名：{gold_dir} 已在场")
        assessment = run_harness(
            mode=args.gold_mode, run_id=gold_run_id,
            workers=args.workers, token_budget=args.gold_token_budget,
            seed=args.seed, sample_size=args.gold_sample_size,
            out_dir=gold_dir, out_dir_exist_ok=args.resume,
            chain_with_judge=args.chain_with_judge,
            progress=lambda *a: print(f"[EXAM-GOLD] {a}", flush=True))
        summary["tracks"]["gold"] = {
            "run_id": gold_run_id, "mode": args.gold_mode,
            "anchor_verdicts": assessment["anchor_verdicts"],
            "scores": {k: {kk: vv for kk, vv in v.items() if kk != "detail"}
                       for k, v in assessment["scores"].items()},
            "stopped_reason": assessment["stopped_reason"]}
        registry["tracks"].append({
            "run_id": gold_run_id, "kind": "gold",
            "dir": str(gold_dir),
            "artifacts": ["verdicts.json", "assessment.json",
                          "judge-usage.jsonl"]})
        fp_total = sum(s["fp"] for s in summary["tracks"]["gold"]
                       ["scores"].values())
        if fp_total > 0:   # 双保险（②内已硬闸；此处 runner 级兜底）
            raise RunnerStop(3, f"fp 硬闸（runner 兜底）：gold 轨 fp={fp_total}")

    if "t0909" in tracks:
        t_dir = run_root / "t0909"
        t_run_id = f"{args.run_id}-t0909"
        if t_dir.exists() and not args.resume:
            raise RunnerStop(6, f"撞名：{t_dir} 已在场")
        t_dir.mkdir(parents=True, exist_ok=True)
        stream_path, decisions_path, manifest_sha = _resolve_t_paths(
            Path(args.t_frozen_dir) if args.t_frozen_dir else None,
            Path(args.t_stream) if args.t_stream else None,
            Path(args.t_decisions) if args.t_decisions else None)
        try:
            assessment = run_t0909_track(
                run_id=t_run_id, sample=args.t_sample,
                max_pairs_per_item=args.t_max_pairs_per_item,
                workers=args.workers, token_budget=args.t_token_budget,
                seed=args.seed, out_dir=t_dir,
                stream_path=stream_path, decisions_path=decisions_path,
                max_failure_rate=args.t_max_failure_rate,
                progress=lambda *a: print(f"[EXAM-T0909] {a}", flush=True))
        except JudgeExecutorError as error:
            # 证据先行（主窗口令）：执行器红停也落 summary stub（verdicts 已
            # 由执行器 evidence_dir 先落 t_dir，红停不丢判）
            (run_root / "summary.json").write_text(json.dumps(
                {**summary, "stopped": f"t0909 judge_executor_red: {error}",
                 "executor_payload": error.payload},
                ensure_ascii=False, indent=2), encoding="utf-8")
            raise
        summary["tracks"]["t0909"] = {
            "run_id": t_run_id, "sample_rows": assessment["sample_rows"],
            "judge_pairs": assessment["judge_pairs"],
            "sample_gap": assessment["sample_gap"],
            "judge_merged_dist": assessment["judge_merged_dist"],
            "judge_proof_dist": assessment["judge_proof_dist"],
            "chain_native_dist": assessment["chain_native_dist"],
            "signed_rate": assessment["signed_rate"],
            "failure_rate": assessment["failure_rate"],
            "stopped_reason": assessment["stopped_reason"],
            "frozen_sources": {"stream": str(stream_path),
                               "decisions": str(decisions_path),
                               "manifest_sha256": manifest_sha}}
        registry["tracks"].append({
            "run_id": t_run_id, "kind": "t0909", "dir": str(t_dir),
            "artifacts": ["assessment.json", "judge-usage.jsonl"]})

    summary["wall_s"] = round(time.perf_counter() - t0, 1)
    (run_root / "summary.json").write_text(json.dumps(
        summary, ensure_ascii=False, indent=2), encoding="utf-8")
    registry["wall_s"] = summary["wall_s"]
    registry_path.write_text(json.dumps(registry, ensure_ascii=False, indent=2),
                             encoding="utf-8")
    print(f"[EXAM-DONE] base={args.run_id} tracks={list(summary['tracks'])} "
          f"wall={summary['wall_s']}s out={run_root}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RunnerStop as error:
        print(f"[EXAM-STOP] {error} "
              f"{json.dumps(error.payload, ensure_ascii=False)[:400]}")
        sys.exit(error.exit_code)
    except HarnessStop as error:      # gold 轨 fp/锚闸透传
        print(f"[EXAM-STOP] gold 轨闸：{error}")
        sys.exit(error.exit_code)
    except JudgeExecutorError as error:
        print(f"[EXAM-STOP] 判官执行器红：{error}")
        sys.exit(5)
