# -*- coding: utf-8 -*-
"""C5R 判官口径加固窗 · v3 红测钉 + v3 装配钉（集成级新文件；既有件零改动）。

定位（主窗口 v3 授权 B+C 轻量版 ⑤红测先行）：
- **ADR 复签钉（场景 3）**：1ea81b26（SK海力士 vs SK海力士 ADRs，金标=重复）
  v2 下 signed→doubtful 失 tp（"身份"措辞泛化到实体载体——ADR priming 病灶）。
  v3 条款收窄（实体载体形式差异不构成口径差异）后必须【恢复签发】：
  cell=signed 且 signed=True。
- **62f74308 保持钉（场景 3）**：CPI vs 调和 CPI/HICP 为闭合枚举内统计指标
  口径定义身份差异，v3 下必须【禁重复】（signed=False；cell ∈
  {not_duplicate, doubtful}，主窗口呈裁 A 照"禁重复"方言收 doubtful 合规，
  与 v2 钉前例一致；该对已出 339 验收基）——v2 修复不回退。
- 现象登记钉（场景 3，不硬闸）：6d833967（CPI 年率 vs 调和 CPI 年率，枚举内
  →预期维持不签）与 94df0680（中国电研截头对，实体名称面→预期复签）逐对
  落盘，供臂B 批前预测；不折算成败。
- v3 装配钉（场景 2）：src 注册面三态闸（v1/v2/v3；v1 缺省可切回不变；
  v1/v2 sha 钉零漂移；v3 sha 钉死；缓存键跨版本隔离）。
- 预算闸（决策 #16，同 #14/#15 纪律）：闸 1.6M tok/日（UTC+8 业务日），
  计数器 log/temp/c5r-budget-counter.json 全臂共享续算；逐次 usage 实扣+
  计量 JSONL=c5r-v3-api-<run>.jsonl。本钉 4 对×2 顺序=至多 8 活调，结果写
  P05 残判缓存目录（c5r-p05-residual-cache，v3 键）——臂B v3 批零 API 复用。
- 模式闸：P23_RESIDUAL_JUDGE=llm_bidi_v3 → prompt_version="judge_v3"；
  v1 切回=既有 harness（llm_bidi_v1）不动；v2=llm_bidi_v2 不动。

跑站命令（仓根 news-flash-dedup）：

    $env:PYTHONPATH='src'
    $env:PYTHONIOENCODING='utf-8'
    $env:PYTHONDONTWRITEBYTECODE='1'
    $env:DEPLOY_ENV='test'
    $env:P23_CONFIRM_UAT='1'
    $env:P23_RESIDUAL_JUDGE='llm_bidi_v3'
    $env:DEDUP_EMBEDDING_KEY_FILE='C:\\Users\\ASUS\\Desktop\\文本去重\\workspace-dedup\\.env.dedup'
    & '.venv-v1\\Scripts\\python.exe' -m pytest -p no:cacheprovider `
        --basetemp '.\\tmp\\c5r-v3-pytest' -v tests/integration/test_c5r_judge_v3.py `
        > .\\log\\temp\\c5r-v3-pin-run.txt
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import test_c5_oos_replay as c5

REPO_LOG_TEMP = c5.REPO_LOG_TEMP

RESIDUAL_SWITCH_ENV = c5.RESIDUAL_SWITCH_ENV      # P23_RESIDUAL_JUDGE
RESIDUAL_ENABLED_VALUE_V3 = "llm_bidi_v3"
DAILY_TOKEN_BUDGET = int(os.environ.get("C5R_DAILY_BUDGET", "1500000"))
BUDGET_PATH = Path(os.environ.get(
    "C5R_BUDGET_PATH", str(REPO_LOG_TEMP / "c5r-budget-counter.json")))
P05_CACHE_DIR = Path(os.environ.get(
    "C5R_P05_CACHE_DIR", str(REPO_LOG_TEMP / "c5r-p05-residual-cache")))
MODEL = os.environ.get("C5R_MODEL", "qwen-turbo")

# 红测对（P05 manifest 文本，探针 c5r-pair-text-probe.out.json 在案）
ADR_PAIR_ID = ("1ea81b262240f758b1bc8e2e43371a4e794ad90e978e0"
               "36a7a0444f2e844ebdb")
ADR_TEXT_H = "摩根大通给予SK海力士ADRs超配评级，目标价245美元。（产联社）"
ADR_TEXT_C = "摩根大通给予SK海力士超配评级，目标价245美元。（产联社）"

FP_PAIR_ID = ("62f74308a419c79a9828efe077bcca4d1a333e"
              "0b6f0633f56b989b5f9cee50e9")
FP_TEXT_H = "德国8月CPI环比终值为0.2%，与预期及初值一致。（产联社）"
FP_TEXT_C = "德国8月调和CPI环比终值为0.2%，与预期及初值一致。（产联社）"

CPI_YOY_PAIR_ID = ("6d83396785e5a9fc7892b809be46a0b2e014c0"
                   "beea3c506d319ec188e9dc1434")
CPI_YOY_H = "德国8月CPI年率终值为2.9%，与预期值一致，前值为2.90%。（产联社）"
CPI_YOY_C = "德国8月调和CPI年率终值为2.9%，与预期值一致，前值为2.90%。（产联社）"

TRUNC_PAIR_ID = ("94df0680124ba56b42fcca43ca74c842ea982b1b"
                 "59c6387f507a8430ef0d1d86")
TRUNC_H = ("中国电研在互动平台表示，公司现有油墨树脂产品可适配柔性线路板"
           "（FPC）应用场景，目前暂未开发直接应用于硬质PCB的树脂类产品。"
           "（产联社）")
TRUNC_C = ("研在互动平台表示，公司现有油墨树脂产品可适配柔性线路板"
           "（FPC）应用场景，目前暂未开发直接应用于硬质PCB的树脂类产品。"
           "（产联社）")

PROBE_PAIRS = (
    ("adr_restore", ADR_PAIR_ID, ADR_TEXT_H, ADR_TEXT_C),
    ("fp_hold", FP_PAIR_ID, FP_TEXT_H, FP_TEXT_C),
    ("cpi_yoy_register", CPI_YOY_PAIR_ID, CPI_YOY_H, CPI_YOY_C),
    ("trunc_register", TRUNC_PAIR_ID, TRUNC_H, TRUNC_C),
)

pytestmark = pytest.mark.skipif(
    not c5._gate_open(os.environ),
    reason="C5R gate not open (need DEPLOY_ENV=test, P23_CONFIRM_UAT=1, no PROD_*)",
)


# ---------- v3 残判装配（预算闸包装= C5 harness _BudgetedJudgeCall 复用） ----------

def _residual_config_v3(run_id: str, metering_path: Path, *,
                        cache_dir: Path = P05_CACHE_DIR):
    """v3 残判层装配。开关必须显式 =llm_bidi_v3；其他值 fail-closed。"""
    raw = os.environ.get(RESIDUAL_SWITCH_ENV, "")
    if raw != RESIDUAL_ENABLED_VALUE_V3:
        pytest.fail(f"{RESIDUAL_SWITCH_ENV}={raw!r} 未知或缺省（fail-closed；"
                    f"C5R v3 口径要求显式 {RESIDUAL_ENABLED_VALUE_V3!r}；"
                    f"v1 切回走既有 harness=llm_bidi_v1）")
    from news_flash_dedup.decide import llm_residual

    cfg = llm_residual.ResidualJudgeConfig(
        model=MODEL,
        cache_dir=str(cache_dir),
        mv_mode=llm_residual.MV_AUDIT,
        prompt_version=llm_residual.JUDGE_PROMPT_VERSION_V3)
    call = c5._BudgetedJudgeCall(
        api_key_fn=c5._llm_api_key, run_id=run_id, metering_path=metering_path,
        budget_path=BUDGET_PATH, daily_budget=DAILY_TOKEN_BUDGET,
        max_live_calls=None, base_url=llm_residual.DEFAULT_BASE_URL,
        timeout_s=llm_residual.TIMEOUT_S)
    if not issubclass(c5._C5BudgetError, llm_residual.LlmResidualError):
        class _C5RBudgetError(llm_residual.LlmResidualError):
            pass
        c5._C5BudgetError = _C5RBudgetError
    judge = llm_residual.SyncResidualJudge(cfg, call_fn=call)
    return SimpleNamespace(config=cfg, judge=judge, call=call, switch=raw)


@pytest.fixture(scope="module")
def v3_probe():
    """v3 四对双向判定（至多 8 活调；v3 键落 P05 缓存目录供臂B 批复用）。"""
    run_id = "c5rv3p" + datetime.now(timezone.utc).strftime("%H%M%S")
    metering_path = REPO_LOG_TEMP / f"c5r-v3-api-{run_id}.jsonl"
    residual = _residual_config_v3(run_id, metering_path)
    call = residual.call
    from news_flash_dedup.decide import llm_residual as _lr
    results = {}
    try:
        for tag, pid, text_h, text_c in PROBE_PAIRS:
            call.context = {"pair_id": pid, "order": None}
            try:
                outcome = residual.judge.judge_pair(pid, text_h, text_c)
            finally:
                call.context = {"pair_id": None, "order": None}
            body = outcome.to_audit_dict()
            body["probe_tag"] = tag
            body["prompt_version"] = _lr.JUDGE_PROMPT_VERSION_V3
            body["prompt_sha256"] = _lr.PROMPT_SHA256_V3
            results[tag] = SimpleNamespace(
                pair_id=pid, outcome=outcome, body=body)
    finally:
        summary = {
            "run_id": run_id,
            "prompt_version": _lr.JUDGE_PROMPT_VERSION_V3,
            "prompt_sha256": _lr.PROMPT_SHA256_V3,
            "budget": call.snapshot(),
            "ledger": residual.judge.ledger.snapshot(),
            "pairs": {tag: r.body for tag, r in results.items()},
        }
        out_path = REPO_LOG_TEMP / f"c5r-v3-probe-{run_id}.json"
        out_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2),
                            encoding="utf-8")
        print(f"\n[C5R-V3] probe json: {out_path}")
        for tag, r in results.items():
            o = r.outcome
            print(f"[C5R-V3] {tag}: pair={r.pair_id[:16]} cell={o.cell} "
                  f"signed={o.signed} hc={o.hc.final_decision} "
                  f"ch={o.ch.final_decision}")
        print(f"[C5R-V3] budget={call.snapshot()}")
    return SimpleNamespace(results=results, run_id=run_id,
                           metering_path=metering_path, call=call,
                           ledger=residual.judge.ledger.snapshot())


# ---------- 场景 1 · 闸门 + 开关 fail-closed ----------

def test_c5r_v3_harness_gate_and_switch():
    assert c5._gate_open({c5.DEPLOY_ENV_VAR: "test", c5.P23_UAT_ENV: "1"}) is True
    assert c5._gate_open({c5.P23_UAT_ENV: "1"}) is False
    assert c5._gate_open({c5.DEPLOY_ENV_VAR: "test", c5.P23_UAT_ENV: "1",
                          "PROD_ES_HOST": "x"}) is False
    assert c5._gate_open({c5.DEPLOY_ENV_VAR: "test", c5.P23_UAT_ENV: "1",
                          "PROD_MILVUS_ENDPOINT": ""}) is False
    assert RESIDUAL_ENABLED_VALUE_V3 == "llm_bidi_v3"
    from news_flash_dedup.decide import llm_residual as _lr
    assert _lr.JUDGE_PROMPT_VERSION_V3 == "judge_v3"
    with pytest.raises(ValueError):
        _lr.ResidualJudgeConfig(prompt_version="llm_bidi_v9")
    # v1 切回面不动（既有 harness 的合法值与 src 缺省一致）
    assert c5.RESIDUAL_ENABLED_VALUE == "llm_bidi_v1"
    assert _lr.ResidualJudgeConfig().prompt_version == "judge_v1"


# ---------- 场景 2 · v3 装配钉（注册面三态 + 既有钉零翻转） ----------

def test_c5r_v3_registry_pins():
    from news_flash_dedup.decide import llm_residual as _lr
    # 三态注册
    assert sorted(_lr._JUDGE_PROMPTS) == ["judge_v1", "judge_v2", "judge_v3"]
    # v3 sha 钉死
    assert _lr.PROMPT_SHA256_V3 == (
        "8985d905b30c2847509b3e7c6d3fb1b9a2cfd267aa86a567c53d080447cb1494")
    prompt, sha = _lr.judge_prompt_for_version("judge_v3")
    assert prompt == _lr.JUDGE_PROMPT_V3 and sha == _lr.PROMPT_SHA256_V3
    # 条款面：闭合枚举 + 实体载体排除
    assert "月环比与年率" in prompt and "CPI 与调和 CPI/HICP" in prompt
    assert "不构成口径差异" in prompt and "ADR" in prompt
    # 既有钉零翻转：v1/v2 sha 与缺省不漂
    assert _lr.PROMPT_SHA256 == (
        "add219156ff72a0b9a8cabffc73eb975aa4ad51ae1a80c98c8a88118ba9cd149")
    assert _lr.PROMPT_SHA256_V2 == (
        "b5214c206ef085b08851f6728926af3905fcc6424314920894a175c58b6c37bf")
    assert _lr.ResidualJudgeConfig().prompt_version == "judge_v1"
    # 缓存键跨版本隔离（v2/v3 同对不同键）
    k2 = _lr.residual_cache_key(MODEL, "judge_v2", FP_PAIR_ID,
                                _lr.PROMPT_SHA256_V2, "hc")
    k3 = _lr.residual_cache_key(MODEL, "judge_v3", FP_PAIR_ID,
                                _lr.PROMPT_SHA256_V3, "hc")
    assert k2 != k3


# ---------- 场景 3 · 红测钉（任务书⑤） ----------

def test_c5r_v3_adr_pair_restored_signed(v3_probe):
    """ADR 对（1ea81b26，SK海力士 ADRs）v3 下必须恢复签发——病灶切除验收。"""
    out = v3_probe.results["adr_restore"].outcome
    assert out.hc.status == "ok" and out.ch.status == "ok", (
        f"ADR 对判定非正常完成：hc={out.hc.status} ch={out.ch.status}")
    assert out.cell == "signed" and out.signed is True, (
        f"ADR 对 v3 下 cell={out.cell!r} signed={out.signed}——"
        "实体载体排除条款未生效（ADR priming 病灶未切除），停报"
    )


def test_c5r_v3_fp_pair_holds_unsigned(v3_probe):
    """62f74308（CPI vs 调和 CPI/HICP）v3 下必须 禁重复——v2 修复不回退。

    呈裁 A（主窗口 2026-09-30 裁定）：照"禁重复"方言收 doubtful 为 fp-hold
    合规（v2 前例一致；该对已出 339 验收基；为强扭 not_duplicate 再迭代
    prompt=按 dev 追分违硬约束——不迭代）。实测 cell=doubtful（hc=不重复/
    ch=重复），signed=False——合规在案。
    """
    out = v3_probe.results["fp_hold"].outcome
    assert out.hc.status == "ok" and out.ch.status == "ok", (
        f"fp 对判定非正常完成：hc={out.hc.status} ch={out.ch.status}")
    assert out.signed is False, "fp 对 v3 下被签'重复'——fp 回潮，立即停报"
    assert out.cell in ("not_duplicate", "doubtful"), (
        f"fp 对 v3 下 cell={out.cell!r}——禁重复口径外格，停报"
    )


def test_c5r_v3_phenomenon_register(v3_probe):
    """现象登记（不硬闸）：6d833967 预期维持不签（枚举内）；94df0680 预期
    复签（实体名称面）。逐对落盘供批前预测，不折算成败。"""
    cpi = v3_probe.results["cpi_yoy_register"].outcome
    trc = v3_probe.results["trunc_register"].outcome
    print(f"[C5R-V3] 登记 6d833967: cell={cpi.cell} signed={cpi.signed}"
          f"（预期不签：{'符' if not cpi.signed else '逆——停报复核'}）")
    print(f"[C5R-V3] 登记 94df0680: cell={trc.cell} signed={trc.signed}"
          f"（预期复签：{'符' if trc.signed else '逆——批前复核'}）")
    assert cpi.hc.status == "ok" and cpi.ch.status == "ok"
    assert trc.hc.status == "ok" and trc.ch.status == "ok"


# ---------- 场景 4 · 预算/计量落盘（决策 #16 纪律自证） ----------

def test_c5r_v3_budget_metering_written(v3_probe):
    budget = v3_probe.call.snapshot()
    assert budget["daily_token_budget"] == DAILY_TOKEN_BUDGET
    live = v3_probe.ledger["api_calls"]
    hits = v3_probe.ledger["cache_hits"]
    assert live + hits == 8, "四对恰各两顺序判定"
    if live > 0:
        assert BUDGET_PATH.exists(), "预算计数器未落盘（决策 #16 违例）"
        counter = json.loads(BUDGET_PATH.read_text(encoding="utf-8"))
        assert counter["used_tokens"] == budget["budget_used_tokens"]
        assert v3_probe.metering_path.exists(), "逐次计量 JSONL 未落盘"
        lines = v3_probe.metering_path.read_text(
            encoding="utf-8").splitlines()
        assert len(lines) == live
        for line in lines:
            rec = json.loads(line)
            assert rec["ok"] is True and rec["tokens"] > 0
            assert rec.get("estimated") is False, (
                "usage.total_tokens 缺失走字节估值——实扣纪律例外须入账复核"
            )
