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
    """禁缓存令违例/台账对账失败/预算起步即尽——红报即停。"""


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
                    progress=None) -> JudgeBatchReport:
    """并发判官批跑主入口。

    requests: JudgePairRequest 可迭代（逐对独立，顺序无关——结论只随对）。
    token_budget：run 域 token 预算（双臂预估账）；0/负=不限（仍记账）。
    proof=True 时批次内开 DEDUP_JUDGE_PROOF=1（P1-a 证明层），批次末恢复。
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

    # usage 逐笔台账（golde2e-assess _logged_call_fn 同款；线程写锁自供）
    ledger_path = Path(usage_ledger_path) if usage_ledger_path else None
    usage_lock = threading.Lock()
    usage_seq = {"n": 0}
    if ledger_path and ledger_path.exists() and run_id == "judge-batch":
        ledger_path.unlink()

    def _usage_row(row: dict) -> None:
        if ledger_path is None:
            return
        with usage_lock:
            usage_seq["n"] += 1
            payload = {"seq": usage_seq["n"],
                       "ts": datetime.now(timezone.utc).isoformat(),
                       "run_id": run_id, **row}
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

    # 决策 #29 台账对账：api+hits == 实判臂数；禁缓存令 hits 恒 0。
    arms = 2 * sum(1 for r in results if r.cell != "budget_skip")
    ledger = judge.ledger.snapshot() if hasattr(judge.ledger, "snapshot") else {
        "api_calls": judge.ledger.api_calls, "cache_hits": judge.ledger.cache_hits}
    if ledger["cache_hits"] > 0:
        raise JudgeExecutorError(
            f"禁缓存令违例：cache_hits={ledger['cache_hits']} > 0（cache_dir=None 下不得有 hits）")
    if ledger["api_calls"] + ledger["cache_hits"] != arms:
        raise JudgeExecutorError(
            f"臂账不合：api={ledger['api_calls']}+hits={ledger['cache_hits']} "
            f"!= arms={arms}")

    counters: dict = {}
    for r in results:
        counters[f"cell.{r.cell}"] = counters.get(f"cell.{r.cell}", 0) + 1
        counters[f"residual.{r.verdict_residual}"] = counters.get(
            f"residual.{r.verdict_residual}", 0) + 1
        counters[f"proof.{r.verdict_proof}"] = counters.get(
            f"proof.{r.verdict_proof}", 0) + 1
    usage = {"api_calls": ledger["api_calls"], "cache_hits": ledger["cache_hits"],
             "tokens_est_total": sum(r.tokens_est for r in results),
             "usage_rows": usage_seq["n"]}
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
        proof=not args.no_proof,
        progress=lambda d, t: print(f"[JUDGE-EXEC] {d}/{t}", flush=True))
    Path(args.out).write_text(json.dumps(_report_to_dict(report),
                                         ensure_ascii=False, indent=2),
                              encoding="utf-8")
    print(f"[JUDGE-EXEC-DONE] run={report.run_id} pairs={len(report.results)} "
          f"arms={report.arms} stopped={report.stopped_reason} "
          f"usage={report.usage} wall={report.wall_s}s")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except JudgeExecutorError as error:
        print(f"[JUDGE-EXEC-STOP] {error}")
        sys.exit(4)
