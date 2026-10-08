"""P1 合流树考试工装①：判官并发执行器（2026-10-09，主窗口派单；用户令：轻、快）。

定位：4–8 对并发调判官（SyncResidualJudge 双序 hc+ch 现场真判），尊重
预算闸与禁缓存令（决策 #29 形，vfy-dual-run.py:11-12/144-147 同款纪律）：
- 禁缓存令：ResidualJudgeConfig(cache_dir=None) 钉死+预飞断言；跑后
  台账对账 api_calls + cache_hits == 实判臂数 且 cache_hits == 0，
  任何 hits>0 = 红报（JudgeExecutorError，exit 5）；
- 预算闸：run 域 token 预算账——qwen_bpe 钉值 tokenizer 逐臂预估，
  提交前闸门（reserved + 本对预估 > budget → 停发新对，已发收尾，
  未发对按 budget_skip 记"拿不准（转人工）"，绝不超支硬跑）；
- usage 逐笔台账 JSONL（golde2e-assess.py:155-179 _logged_call_fn 同款：
  tokens_in/out 经 qwen_bpe 现算逐笔落盘）。

判官口径：mv_mode=audit（锚轨同一，golde2e-assess.py:10 注）；
DEDUP_JUDGE_PROOF=1 证明层（P1-a judge_proof）在本执行器内开启
（批次首尾保存/恢复 env 原值，不外溢）。产出双口径逐对结论：
- verdict_residual：锚口径（残判合并 signed→重复 / not_duplicate→不重复 /
  其余→拿不准（转人工））——296/0/10/49 锚断言挂本口径；
- verdict_proof：P1 新口径（双序重复且证明签发→重复 / 双序不重复且
  证伪证明签发→不重复 / 其余→拿不准（转人工））——合同 v1 §四同构。

并发：ThreadPoolExecutor（workers 钳制 [4,8]）；SyncResidualJudge 台账/
票据自带锁（llm_residual.py:590-593 W2Fα2-7b），cache_dir=None 零磁盘
共享态，线程安全面=usage 台账写锁（本件自供）。

复用：被 vfy_layered_harness.py（②）与 exam_dual_track_runner.py（③）
import；亦可独立 CLI 跑任意 pairs JSON：
    python scripts\\judge_concurrent_executor.py --pairs-json pairs.json \
        --run-id demo --workers 6 --token-budget 200000 \
        --usage-ledger usage.jsonl --out verdicts.json
pairs.json = [{"pair_id": "...", "text_a": "...", "text_b": "..."}, ...]

exit：0=跑完（含 budget 截停，stopped_reason 记因）；2=用法错；
3=api key 缺失；4=禁缓存令/台账对账红；5=预算为零起步即停。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent                 # news-flash-dedup-p1int\
_WORKSPACE_ROOT = _REPO_ROOT.parent           # workspace-dedup\
sys.path.insert(0, str(_REPO_ROOT / "src"))

from news_flash_dedup.decide.llm_residual import (                # noqa: E402
    ResidualJudgeConfig, SyncResidualJudge, _chat_once,
    judge_prompt_for_version)
from news_flash_dedup.vector import qwen_bpe as qb                # noqa: E402

ENV_DEDUP = _WORKSPACE_ROOT / ".env.dedup"
DEFAULT_TOKEN_BUDGET = 2_000_000
VERDICT_DUP = "重复"
VERDICT_NONDUP = "不重复"
VERDICT_UNSURE = "拿不准（转人工）"          # gold-交付 ②/指标计算器 词汇同一


class JudgeExecutorError(RuntimeError):
    """禁缓存令违例/台账对账红（api<arms 或 hits>0）——红报即停。
    payload：证据路径等对拍明细（2026-10-09 证据先行令：红前 verdicts 已落盘，
    payload["evidence"] 指其位）。"""

    def __init__(self, reason: str, payload: dict | None = None):
        super().__init__(reason)
        self.payload = payload or {}


def reconcile_arms(*, api_calls: int, cache_hits: int, judged_pairs: int,
                   usage_rows: list) -> list:
    """对账分层（2026-10-09 主窗口令：T 轨 999/1000 判毕撞 api=2001≠arms=1998
    全丢事故的修法）。

    分层：
    - hits > 0            → red（禁缓存令，维持即停）；
    - api < 2×judged      → red（臂账缺失：真判臂数都盖不住，台账/判官不一致）；
    - api > 2×judged      → warning「重试痕」（判官臂内重试每次 _chat_once 都
      计 api_calls——成功路径才入 usage 台账，失败尝试只进 ledger 计数；
      真判臂齐=每判对均有结局（构造性成立）且 api≥臂数下限 → 放行记 warning，
      台账按对分组对拍明细附上：extra>2 的对=重试痕承载对，short<2 的对=
      失败臂对（成功行为零））。
    返 warnings 列表（空=全齐）。
    """
    expected = 2 * judged_pairs
    if cache_hits > 0:
        raise JudgeExecutorError(
            f"禁缓存令违例：cache_hits={cache_hits} > 0（cache_dir=None 下不得有 hits）",
            {"cache_hits": cache_hits})
    if api_calls < expected:
        raise JudgeExecutorError(
            f"臂账缺失红：api={api_calls} < arms={expected}"
            f"（judged={judged_pairs} 对×2；真判臂数盖不住，台账/判官不一致）",
            {"api_calls": api_calls, "expected_arms": expected,
             "judged_pairs": judged_pairs})
    if api_calls == expected:
        return []
    by_pair: dict = {}
    for row in usage_rows:
        if row.get("kind") != "judge_arm":
            continue
        pid = row.get("pair_id") or "（未归属）"
        by_pair[pid] = by_pair.get(pid, 0) + 1
    extra = {p: c for p, c in sorted(by_pair.items()) if c > 2}
    short = {p: c for p, c in sorted(by_pair.items()) if c < 2}
    return [{
        "kind": "retry_trace",
        "detail": f"api={api_calls} > arms={expected}（重试痕：判官臂内重试"
                  f"逐次计账；超差 {api_calls - expected} 次）——真判臂齐放行",
        "api_calls": api_calls, "expected_arms": expected,
        "excess": api_calls - expected,
        "judged_pairs": judged_pairs,
        "pairs_with_retry": extra,          # 成功行>2=重试后成的对
        "pairs_arm_failure": short,         # 成功行<2=臂失败对（结局 failure/invalid）
        "ledger_rows": len(usage_rows),
    }]


def _load_env_key(key: str) -> str:
    """golde2e-assess.py:72-79 同款：workspace .env.dedup 懒取 key。"""
    if not ENV_DEDUP.exists():
        return ""
    for raw in ENV_DEDUP.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            if k.strip() == key:
                return v.strip().strip("'").strip('"')
    return ""


@dataclass(frozen=True)
class JudgePairRequest:
    """单对判官考题。text_a=history 侧（先到），text_b=current 侧（后到）。"""
    pair_id: str
    text_a: str
    text_b: str
    meta: dict = field(default_factory=dict)


@dataclass(frozen=True)
class JudgePairVerdict:
    """单对双口径结论 + 台账摘要。"""
    pair_id: str
    cell: str                       # signed/not_duplicate/doubtful/failure/invalid/budget_skip
    signed: bool
    verdict_residual: str           # 锚口径三态（锚断言挂本列）
    verdict_proof: str              # P1 证明口径三态（分列）
    proof_dup_issued: bool
    proof_nd_issued: bool
    proof_failures: tuple           # 证明未签发原因（P_*/failure 枚举，观测用）
    suspicion_score: float | None
    tokens_est: int                 # 双臂 token 预估（预算账）
    error: str | None = None


@dataclass(frozen=True)
class JudgeBatchReport:
    run_id: str
    results: tuple                  # JudgePairVerdict ...
    counters: dict                  # cell 分布 + verdict 分布
    usage: dict                     # api_calls/cache_hits/tokens_in/out/est
    stopped_reason: str | None      # None | "token_budget"
    arms: int                       # 实判臂数（成功进入判官的对×2）
    wall_s: float


def _scrub_proxy_env() -> None:
    """决策 #29 起跑闸③-2 同款（vfy-dual-run.py:96-99）：代理洗清。"""
    for key in list(os.environ):
        if key.upper() in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
            del os.environ[key]
    os.environ["NO_PROXY"] = "*"


def _user_msg(text_a: str, text_b: str) -> str:
    """llm_residual._judge_order :785 逐字同一形态（预估账口径锚）。"""
    return f"【文本A】\n{text_a}\n\n【文本B】\n{text_b}"


def _clamp_workers(workers: int) -> int:
    return max(4, min(8, int(workers)))


def run_judge_batch(requests, *, run_id: str = "judge-batch",
                    workers: int = 6,
                    token_budget: int = DEFAULT_TOKEN_BUDGET,
                    usage_ledger_path: str | None = None,
                    api_key: str | None = None,
                    model: str | None = None,
                    prompt_version: str | None = None,
                    proof: bool = True,
                    evidence_dir: str | None = None,
                    progress=None) -> JudgeBatchReport:
    """并发判官批跑主入口。

    requests: JudgePairRequest 可迭代（逐对独立，顺序无关——结论只随对）。
    token_budget：run 域 token 预算（双臂预估账）；0/负=不限（仍记账）。
    proof=True 时批次内开 DEDUP_JUDGE_PROOF=1（P1-a 证明层），批次末恢复。
    evidence_dir：证据先行令（2026-10-09 主窗口，T 轨 verdicts 全丢事故
    修法）——在场时批次判毕**先于一切对账/断言闸**把 verdicts 落盘
    （verdicts-{run_id}.json）；对账红 raise 时 payload["evidence"] 指其位。
    progress(done, total)：可选进度回调（心跳用）。
    """
    t0 = time.perf_counter()
    requests = list(requests)
    _scrub_proxy_env()
    key = api_key or os.environ.get("QWEN_API_KEY", "") or _load_env_key("QWEN_API_KEY")
    if not key:
        raise JudgeExecutorError("QWEN_API_KEY 缺失（env/.env.dedup 均无）")

    cfg_kwargs: dict = {"cache_dir": None, "mv_mode": "audit"}     # 禁缓存令钉死
    if model:
        cfg_kwargs["model"] = model
    if prompt_version:
        cfg_kwargs["prompt_version"] = prompt_version
    cfg = ResidualJudgeConfig(**cfg_kwargs)
    # 禁缓存令预飞（决策 #29 形）：cache_dir 恒 None + 无判官缓存 env 注入面。
    assert cfg.cache_dir is None, "禁缓存令：cache_dir 必须 None"
    for env_key in os.environ:
        if "JUDGE_CACHE" in env_key.upper() or "RESIDUAL_CACHE" in env_key.upper():
            raise JudgeExecutorError(f"禁缓存令预飞红：env {env_key} 在场")

    tokenizer = qb.QwenBpeTokenizer()
    system_prompt, _prompt_sha = judge_prompt_for_version(cfg.prompt_version)
    model_name = cfg.model

    # usage 逐笔台账（golde2e-assess _logged_call_fn 同款；线程写锁自供；
    # 2026-10-09：pair_id 线程归属——对账分层按对分组对拍用；内存留痕一份）
    ledger_path = Path(usage_ledger_path) if usage_ledger_path else None
    usage_lock = threading.Lock()
    usage_seq = {"n": 0}
    usage_rows: list = []
    tls = threading.local()
    if ledger_path and ledger_path.exists() and run_id == "judge-batch":
        ledger_path.unlink()

    def _usage_row(row: dict) -> None:
        with usage_lock:
            usage_seq["n"] += 1
            payload = {"seq": usage_seq["n"],
                       "ts": datetime.now(timezone.utc).isoformat(),
                       "run_id": run_id,
                       "pair_id": getattr(tls, "pair_id", None), **row}
            usage_rows.append(payload)
            if ledger_path is not None:
                with ledger_path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(payload, ensure_ascii=False) + "\n")

    def _logged_call_fn(*, model: str, system: str, user: str):
        content, latency = _chat_once(
            model=model, base_url=cfg.base_url, api_key=key,
            system=system, user=user, timeout_s=cfg.timeout_s)
        _usage_row({
            "kind": "judge_arm", "method": "_chat_once", "model": model,
            "latency_ms": round(latency * 1000, 1),
            "tokens_in": qb.count_tokens(tokenizer, system)
            + qb.count_tokens(tokenizer, user),
            "tokens_out": qb.count_tokens(tokenizer, content)})
        return content, latency

    judge = SyncResidualJudge(cfg, api_key=key, call_fn=_logged_call_fn)

    # 预算闸：双臂预估 + reserved 提交门（线程锁）
    def _pair_est(req: JudgePairRequest) -> int:
        base = qb.count_tokens(tokenizer, system_prompt)
        return (base + qb.count_tokens(tokenizer, _user_msg(req.text_a, req.text_b))
                + base + qb.count_tokens(tokenizer, _user_msg(req.text_b, req.text_a)))

    budget_lock = threading.Lock()
    reserved = {"tokens": 0}
    stopped = {"reason": None}

    def _try_reserve(est: int) -> bool:
        if token_budget <= 0:
            return True
        with budget_lock:
            if stopped["reason"] is not None:
                return False
            if reserved["tokens"] + est > token_budget:
                stopped["reason"] = "token_budget"
                return False
            reserved["tokens"] += est
            return True

    def _judge_one(req: JudgePairRequest, est: int) -> JudgePairVerdict:
        tls.pair_id = req.pair_id            # usage 台账按对归属（对账分层用）
        try:
            outcome = judge.judge_pair(req.pair_id, req.text_a, req.text_b)
        except Exception as error:  # noqa: BLE001 — 判官失败单列，不拖垮批次
            return JudgePairVerdict(
                pair_id=req.pair_id, cell="failure", signed=False,
                verdict_residual=VERDICT_UNSURE, verdict_proof=VERDICT_UNSURE,
                proof_dup_issued=False, proof_nd_issued=False,
                proof_failures=(f"executor:{type(error).__name__}",),
                suspicion_score=None, tokens_est=est,
                error=f"{type(error).__name__}: {error}")
        finally:
            tls.pair_id = None
        cell = outcome.cell
        verdict_residual = (VERDICT_DUP if outcome.signed
                            else VERDICT_NONDUP if cell == "not_duplicate"
                            else VERDICT_UNSURE)
        proof_dup = outcome.proof
        proof_nd = outcome.not_duplicate_proof
        dup_issued = bool(proof_dup and proof_dup.issued)
        nd_issued = bool(proof_nd and proof_nd.issued)
        failures: list[str] = []
        if proof_dup is not None and not proof_dup.issued:
            failures.extend(proof_dup.failure_reasons)
        if proof_nd is not None and not proof_nd.issued:
            failures.append(f"nd:{proof_nd.failure}:{proof_nd.failure_detail[:80]}")
        verdict_proof = (VERDICT_DUP if dup_issued
                         else VERDICT_NONDUP if nd_issued
                         else VERDICT_UNSURE)
        return JudgePairVerdict(
            pair_id=req.pair_id, cell=cell, signed=outcome.signed,
            verdict_residual=verdict_residual, verdict_proof=verdict_proof,
            proof_dup_issued=dup_issued, proof_nd_issued=nd_issued,
            proof_failures=tuple(failures),
            suspicion_score=outcome.suspicion_score,
            tokens_est=est)

    # DEDUP_JUDGE_PROOF 批次域开关（保存/恢复，不外溢）
    prior_proof_env = os.environ.get("DEDUP_JUDGE_PROOF")
    if proof:
        os.environ["DEDUP_JUDGE_PROOF"] = "1"
    results: list[JudgePairVerdict] = []
    try:
        pending: list[tuple[JudgePairRequest, int]] = []
        for req in requests:
            est = _pair_est(req)
            if _try_reserve(est):
                pending.append((req, est))
            else:
                results.append(JudgePairVerdict(
                    pair_id=req.pair_id, cell="budget_skip", signed=False,
                    verdict_residual=VERDICT_UNSURE, verdict_proof=VERDICT_UNSURE,
                    proof_dup_issued=False, proof_nd_issued=False,
                    proof_failures=("budget_skip",),
                    suspicion_score=None, tokens_est=est,
                    error="token_budget 闸截停（未发判）"))
        done = {"n": 0}
        with ThreadPoolExecutor(max_workers=_clamp_workers(workers),
                                thread_name_prefix="judge-exec") as pool:
            for verdict in pool.map(lambda pair: _judge_one(*pair), pending):
                results.append(verdict)
                done["n"] += 1
                if progress is not None:
                    progress(done["n"], len(pending))
    finally:
        if proof:
            if prior_proof_env is None:
                os.environ.pop("DEDUP_JUDGE_PROOF", None)
            else:
                os.environ["DEDUP_JUDGE_PROOF"] = prior_proof_env

    # 证据先行（2026-10-09 主窗口令：T 轨 999/1000 判毕撞对账闸 verdicts
    # 未落盘全丢事故修法）——一切对账/断言闸之前先把 verdicts 落盘。
    arms = 2 * sum(1 for r in results if r.cell != "budget_skip")
    ledger = judge.ledger.snapshot()
    evidence_path: str | None = None
    if evidence_dir is not None:
        ev_dir = Path(evidence_dir)
        ev_dir.mkdir(parents=True, exist_ok=True)
        ev_file = ev_dir / f"verdicts-{run_id}.json"
        ev_file.write_text(json.dumps({
            "schema": "judge-batch-evidence-v1", "run_id": run_id,
            "saved": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "note": "证据先行：本件先于对账/断言闸落盘（红停不丢判）",
            "arms": arms, "ledger": ledger,
            "stopped_reason": stopped["reason"],
            "results": [
                {"pair_id": r.pair_id, "cell": r.cell, "signed": r.signed,
                 "verdict_residual": r.verdict_residual,
                 "verdict_proof": r.verdict_proof,
                 "proof_dup_issued": r.proof_dup_issued,
                 "proof_nd_issued": r.proof_nd_issued,
                 "proof_failures": list(r.proof_failures),
                 "suspicion_score": r.suspicion_score,
                 "tokens_est": r.tokens_est, "error": r.error}
                for r in results],
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        evidence_path = str(ev_file)

    # 对账分层（主窗口令）：api>arms=重试痕 warning 放行；api<arms/hits>0=红。
    judged_pairs = arms // 2
    try:
        warnings = reconcile_arms(
            api_calls=ledger["api_calls"], cache_hits=ledger["cache_hits"],
            judged_pairs=judged_pairs, usage_rows=usage_rows)
    except JudgeExecutorError as error:
        error.payload.setdefault("evidence", evidence_path)
        error.payload.setdefault("ledger", ledger)
        error.payload.setdefault("arms", arms)
        raise

    counters: dict = {}
    for r in results:
        counters[f"cell.{r.cell}"] = counters.get(f"cell.{r.cell}", 0) + 1
        counters[f"residual.{r.verdict_residual}"] = counters.get(
            f"residual.{r.verdict_residual}", 0) + 1
        counters[f"proof.{r.verdict_proof}"] = counters.get(
            f"proof.{r.verdict_proof}", 0) + 1
    usage = {"api_calls": ledger["api_calls"], "cache_hits": ledger["cache_hits"],
             "tokens_est_total": sum(r.tokens_est for r in results),
             "usage_rows": usage_seq["n"], "warnings": warnings,
             "evidence": evidence_path}
    return JudgeBatchReport(
        run_id=run_id, results=tuple(results), counters=counters, usage=usage,
        stopped_reason=stopped["reason"], arms=arms,
        wall_s=round(time.perf_counter() - t0, 1))


def _report_to_dict(report: JudgeBatchReport) -> dict:
    return {
        "run_id": report.run_id, "counters": report.counters,
        "usage": report.usage, "stopped_reason": report.stopped_reason,
        "arms": report.arms, "wall_s": report.wall_s,
        "results": [
            {"pair_id": r.pair_id, "cell": r.cell, "signed": r.signed,
             "verdict_residual": r.verdict_residual,
             "verdict_proof": r.verdict_proof,
             "proof_dup_issued": r.proof_dup_issued,
             "proof_nd_issued": r.proof_nd_issued,
             "proof_failures": list(r.proof_failures),
             "suspicion_score": r.suspicion_score,
             "tokens_est": r.tokens_est, "error": r.error}
            for r in report.results],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--pairs-json", required=True)
    parser.add_argument("--run-id", default="judge-batch")
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--token-budget", type=int, default=DEFAULT_TOKEN_BUDGET)
    parser.add_argument("--usage-ledger", default=None)
    parser.add_argument("--model", default=None)
    parser.add_argument("--prompt-version", default=None)
    parser.add_argument("--no-proof", action="store_true",
                        help="关 P1-a 证明层（锚口径纯残判回放用）")
    parser.add_argument("--evidence-dir", default=None,
                        help="证据先行：判毕先于对账闸落 verdicts-{run_id}.json")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    rows = json.loads(Path(args.pairs_json).read_text(encoding="utf-8"))
    requests = [JudgePairRequest(pair_id=r["pair_id"], text_a=r["text_a"],
                                 text_b=r["text_b"],
                                 meta={k: v for k, v in r.items()
                                       if k not in ("pair_id", "text_a", "text_b")})
                for r in rows]
    report = run_judge_batch(
        requests, run_id=args.run_id, workers=args.workers,
        token_budget=args.token_budget, usage_ledger_path=args.usage_ledger,
        model=args.model, prompt_version=args.prompt_version,
        proof=not args.no_proof, evidence_dir=args.evidence_dir,
        progress=lambda d, t: print(f"[JUDGE-EXEC] {d}/{t}", flush=True))
    Path(args.out).write_text(json.dumps(_report_to_dict(report),
                                         ensure_ascii=False, indent=2),
                              encoding="utf-8")
    for warning in report.usage.get("warnings", []):
        print(f"[JUDGE-EXEC-WARN] {warning['kind']}: {warning['detail']}")
    print(f"[JUDGE-EXEC-DONE] run={report.run_id} pairs={len(report.results)} "
          f"arms={report.arms} stopped={report.stopped_reason} "
          f"usage={report.usage} wall={report.wall_s}s")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except JudgeExecutorError as error:
        print(f"[JUDGE-EXEC-STOP] {error} "
              f"{json.dumps(error.payload, ensure_ascii=False)[:400]}")
        sys.exit(4)
