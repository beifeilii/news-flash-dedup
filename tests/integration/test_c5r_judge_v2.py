# -*- coding: utf-8 -*-
"""C5R 判官口径加固窗 · fp 对红测钉 + v2 装配钉（集成级新文件；既有件零改动）。

定位（任务书②红测先行）：
- **fp 钉（场景 3）**：fp=62f74308（德国 8月 CPI vs 调和 CPI/HICP，金标=不重复）
  在 judge_v2 下必须【禁重复】——cell ∈ {not_duplicate, doubtful}（doubtful=边界
  允许格）；signed 即红（fp=0 硬闸条款同窗适用）。v1 下确定性 signed 的红证据=
  log/temp/c5r-fp-pin-v1-red.txt（c5r-fp-pair-pin.py judge_v1，缓存零 API 复现）。
- v2 装配钉（场景 2）：src 注册面（版本/sha/条款/闸参数 fail-closed）集成级复钉。
- 预算闸（决策 #14，任务书④）：C5R 一次性闸 150 万 tok/日（UTC+8 业务日），
  计数器 log/temp/c5r-budget-counter.json 全臂共享落盘（重启不可绕闸；
  不可读/落盘失败 fail-closed）；逐次 usage.total_tokens 实扣（缺 usage→字节
  上界估值入账）；逐次计量 JSONL=c5r-api-<run>.jsonl。本钉首跑 2 活调
  （fp 对 hc+ch），结果写 臂A 同一缓存目录（c5r-oos-residual-cache）——臂A
  回放对该对零 API 复用（不重判）。
- 模式闸（任务书⑤）：P23_RESIDUAL_JUDGE=llm_bidi_v2 → ResidualJudgeConfig
  (prompt_version="judge_v2")；v1 切回=既有 harness（llm_bidi_v1）不动。

跑站命令（仓根 news-flash-dedup）：

    $env:PYTHONPATH='src'
    $env:PYTHONIOENCODING='utf-8'
    $env:PYTHONDONTWRITEBYTECODE='1'
    $env:DEPLOY_ENV='test'
    $env:P23_CONFIRM_UAT='1'
    $env:P23_RESIDUAL_JUDGE='llm_bidi_v2'
    $env:DEDUP_EMBEDDING_KEY_FILE='C:\\Users\\ASUS\\Desktop\\文本去重\\workspace-dedup\\.env.dedup'
    & '.venv-v1\\Scripts\\python.exe' -m pytest -p no:cacheprovider `
        --basetemp '.\\tmp\\c5r-fp-pytest' -v tests/integration/test_c5r_judge_v2.py `
        > .\\log\\temp\\c5r-fp-pin-v2-run.txt
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

# 同目录测试模块复用（pytest prepend 导入模式；C5 harness 机械件单源不转写）
import test_c5_oos_replay as c5

REPO_LOG_TEMP = c5.REPO_LOG_TEMP

# ---------- C5R 常量（决策 #14 闸；全臂共享） ----------

RESIDUAL_SWITCH_ENV = c5.RESIDUAL_SWITCH_ENV      # P23_RESIDUAL_JUDGE
RESIDUAL_ENABLED_VALUE_V2 = "llm_bidi_v2"
DAILY_TOKEN_BUDGET = int(os.environ.get("C5R_DAILY_BUDGET", "1500000"))
BUDGET_PATH = Path(os.environ.get(
    "C5R_BUDGET_PATH", str(REPO_LOG_TEMP / "c5r-budget-counter.json")))
MAX_LIVE_CALLS_ENV = "C5R_MAX_LIVE_CALLS"          # 烟测帽（正式跑勿设）
ARM_A_CACHE_DIR = Path(os.environ.get(
    "C5R_OOS_CACHE_DIR", str(REPO_LOG_TEMP / "c5r-oos-residual-cache")))
MODEL = os.environ.get("C5R_MODEL", "qwen-turbo")

FP_PAIR_ID = ("62f74308a419c79a9828efe077bcca4d1a333e"
              "0b6f0633f56b989b5f9cee50e9")
FP_TEXT_H = "德国8月CPI环比终值为0.2%，与预期及初值一致。（产联社）"
FP_TEXT_C = "德国8月调和CPI环比终值为0.2%，与预期及初值一致。（产联社）"

pytestmark = pytest.mark.skipif(
    not c5._gate_open(os.environ),
    reason="C5R gate not open (need DEPLOY_ENV=test, P23_CONFIRM_UAT=1, no PROD_*)",
)


# ---------- v2 残判装配（预算闸包装= C5 harness _BudgetedJudgeCall 复用） ----------

def _residual_config_v2(run_id: str, metering_path: Path, *,
                        cache_dir: Path = ARM_A_CACHE_DIR):
    """v2 残判层装配。开关必须显式 =llm_bidi_v2；其他值 fail-closed。"""
    raw = os.environ.get(RESIDUAL_SWITCH_ENV, "")
    if raw != RESIDUAL_ENABLED_VALUE_V2:
        pytest.fail(f"{RESIDUAL_SWITCH_ENV}={raw!r} 未知或缺省（fail-closed；"
                    f"C5R 口径要求显式 {RESIDUAL_ENABLED_VALUE_V2!r}；"
                    f"v1 切回走既有 harness=llm_bidi_v1）")
    from news_flash_dedup.decide import llm_residual

    max_live_raw = os.environ.get(MAX_LIVE_CALLS_ENV, "").strip()
    max_live = int(max_live_raw) if max_live_raw else None
    cfg = llm_residual.ResidualJudgeConfig(
        model=MODEL,
        cache_dir=str(cache_dir),
        mv_mode=llm_residual.MV_AUDIT,
        prompt_version=llm_residual.JUDGE_PROMPT_VERSION_V2)
    call = c5._BudgetedJudgeCall(
        api_key_fn=c5._llm_api_key, run_id=run_id, metering_path=metering_path,
        budget_path=BUDGET_PATH, daily_budget=DAILY_TOKEN_BUDGET,
        max_live_calls=max_live, base_url=llm_residual.DEFAULT_BASE_URL,
        timeout_s=llm_residual.TIMEOUT_S)
    # 预算错误经 LlmResidualError 直通道传播（不进瞬时重试环）：C5 harness
    # 同型动态子类化（幂等；C5 模块装配与本装配谁先执行皆正确）。
    if not issubclass(c5._C5BudgetError, llm_residual.LlmResidualError):
        class _C5RBudgetError(llm_residual.LlmResidualError):
            pass
        c5._C5BudgetError = _C5RBudgetError
    judge = llm_residual.SyncResidualJudge(cfg, call_fn=call)
    return SimpleNamespace(config=cfg, judge=judge, call=call, switch=raw)


@pytest.fixture(scope="module")
def fp_judgment():
    """fp 对 v2 双向判定（首跑 2 活调；缓存落 臂A 同目录供回放复用）。"""
    run_id = "c5rpin" + datetime.now(timezone.utc).strftime("%H%M%S")
    metering_path = REPO_LOG_TEMP / f"c5r-api-{run_id}.jsonl"
    residual = _residual_config_v2(run_id, metering_path)
    call = residual.call
    call.context = {"pair_id": FP_PAIR_ID, "order": None}
    try:
        outcome = residual.judge.judge_pair(FP_PAIR_ID, FP_TEXT_H, FP_TEXT_C)
    finally:
        call.context = {"pair_id": None, "order": None}
    body = outcome.to_audit_dict()
    from news_flash_dedup.decide import llm_residual as _lr
    body["prompt_version"] = _lr.JUDGE_PROMPT_VERSION_V2
    body["prompt_sha256"] = _lr.PROMPT_SHA256_V2
    body["budget"] = call.snapshot()
    body["ledger"] = residual.judge.ledger.snapshot()
    out_path = REPO_LOG_TEMP / f"c5r-fp-pin-v2-{run_id}.json"
    out_path.write_text(json.dumps(body, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    print(f"\n[C5R-FP] judgment json: {out_path}")
    print(f"[C5R-FP] cell={outcome.cell} signed={outcome.signed} "
          f"hc={outcome.hc.final_decision} ch={outcome.ch.final_decision} "
          f"budget={call.snapshot()}")
    return SimpleNamespace(outcome=outcome, body=body, run_id=run_id,
                           out_path=out_path, metering_path=metering_path,
                           call=call)


# ---------- 场景 1 · 闸门（族闸照 winw2fz 档 + v2 开关 fail-closed） ----------

def test_c5r_harness_gate_requires_confirm_flag():
    assert c5._gate_open({c5.DEPLOY_ENV_VAR: "test", c5.P23_UAT_ENV: "1"}) is True
    assert c5._gate_open({c5.P23_UAT_ENV: "1"}) is False
    assert c5._gate_open({c5.DEPLOY_ENV_VAR: "test"}) is False
    assert c5._gate_open({c5.DEPLOY_ENV_VAR: "test", c5.P23_UAT_ENV: "1",
                          "PROD_ES_HOST": "x"}) is False
    assert c5._gate_open({c5.DEPLOY_ENV_VAR: "test", c5.P23_UAT_ENV: "1",
                          "PROD_MILVUS_ENDPOINT": ""}) is False     # 空值也拒


def test_c5r_switch_fail_closed():
    """v2 harness 只认 llm_bidi_v2；v1 值/拼写漂移/缺省均 fail-closed。"""
    from news_flash_dedup.decide import llm_residual as _lr
    assert RESIDUAL_ENABLED_VALUE_V2 == "llm_bidi_v2"
    assert _lr.JUDGE_PROMPT_VERSION_V2 == "judge_v2"
    with pytest.raises(ValueError):
        _lr.ResidualJudgeConfig(prompt_version="llm_bidi_v9")
    # v1 切回面不动（既有 harness 的合法值与 src 缺省一致）
    assert c5.RESIDUAL_ENABLED_VALUE == "llm_bidi_v1"
    assert _lr.ResidualJudgeConfig().prompt_version == "judge_v1"


# ---------- 场景 2 · v2 装配钉（集成级复钉 src 注册面） ----------

def test_c5r_v2_registry_pins():
    from news_flash_dedup.decide import llm_residual as _lr
    assert _lr.PROMPT_SHA256_V2 == (
        "b5214c206ef085b08851f6728926af3905fcc6424314920894a175c58b6c37bf")
    prompt, sha = _lr.judge_prompt_for_version("judge_v2")
    assert prompt == _lr.JUDGE_PROMPT_V2 and sha == _lr.PROMPT_SHA256_V2
    assert "口径定义身份" in prompt and "调和 CPI/HICP" in prompt
    # prompt 变更→缓存键隔离（全量重判口径）
    k1 = _lr.residual_cache_key(MODEL, "judge_v1", FP_PAIR_ID,
                                _lr.PROMPT_SHA256, "hc")
    k2 = _lr.residual_cache_key(MODEL, "judge_v2", FP_PAIR_ID,
                                _lr.PROMPT_SHA256_V2, "hc")
    assert k1 != k2


# ---------- 场景 3 · fp 对红测钉（任务书②：v2 下禁重复） ----------

def test_c5r_fp_pair_not_signed_under_v2(fp_judgment):
    """fp=62f74308（CPI vs 调和 CPI/HICP）v2 下必须 不重复 或 边界——禁重复。

    红证据（v1 确定性 signed）：log/temp/c5r-fp-pin-v1-red.txt（EXIT=1）。
    """
    out = fp_judgment.outcome
    assert out.cell in ("not_duplicate", "doubtful"), (
        f"fp 对 v2 下 cell={out.cell!r}——禁重复条款被破（fp=0 硬闸，停报）"
    )
    assert out.signed is False, "fp 对 v2 下被签'重复'——硬闸破，立即停报"
    assert out.hc.status == "ok" and out.ch.status == "ok", (
        f"fp 对判定非正常完成：hc={out.hc.status} ch={out.ch.status}"
    )


# ---------- 场景 4 · 预算/计量落盘（决策 #14 纪律自证） ----------

def test_c5r_budget_metering_written(fp_judgment):
    body = fp_judgment.body
    assert body["budget"]["daily_token_budget"] == DAILY_TOKEN_BUDGET
    live = body["ledger"]["api_calls"]
    hits = body["ledger"]["cache_hits"]
    assert live + hits == 2, "fp 对恰两顺序各一次判定"
    if live > 0:
        assert BUDGET_PATH.exists(), "预算计数器未落盘（决策 #14 违例）"
        counter = json.loads(BUDGET_PATH.read_text(encoding="utf-8"))
        assert counter["used_tokens"] == body["budget"]["budget_used_tokens"]
        assert fp_judgment.metering_path.exists(), "逐次计量 JSONL 未落盘"
        lines = fp_judgment.metering_path.read_text(
            encoding="utf-8").splitlines()
        assert len(lines) == live
        for line in lines:
            rec = json.loads(line)
            assert rec["ok"] is True and rec["tokens"] > 0
            assert rec.get("estimated") is False, (
                "usage.total_tokens 缺失走字节估值——实扣纪律例外须入账复核"
            )
