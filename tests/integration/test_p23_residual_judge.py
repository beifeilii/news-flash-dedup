"""窗口T · 残判层 × D32 证据对拍钉值（decide/llm_residual + machine_verify）。

钉值面（全部读 workspace log/temp/d32-llm-judge/ 产物，缺位即诚实 skip——
不触金标工作簿、不触集群、不设 P23_CONFIRM_UAT）：
1. judge_v1 内联提示词 sha256 与 D32 prompts/judge_v1.md 正文逐字节对拍；
2. 缓存键布局与 D32 d32_lib.cache_key 同构——按本模块键公式计算的 hc/ch
   条目文件在 D32 cache 目录真实在场（字节级键兼容凭证）；
3. 误伤数与 D32 模拟一致：以臂A(hc) run1 签发集为底，本模块 R1/R2/R3/R6
   触发对集合与 d32-hardening.json rules_fire_on_signed 逐一对拍
   （R1=0/R2=3/R3=2/R6=4）；
4. fp 三对（6e02c930/d27e0196/f74eaded）规则直评被拦：真实文本+真实裁判
   输出（臂A hc run1），与 d32-hardening-fp.py 实测逐一对拍
   （6e02c930→R2 中；d27e0196→R1 中（R3 同中）；f74eaded→R3 中）；
5. 全宇宙端到端：SyncResidualJudge 跑 329 对（缓存原位复用，零 API）——
   audit 模式 signed=266/278、fp=0/51（D32 双向臂A 对拍）；gate 模式
   signed=251/278、fp=0/51（窗口T 探针实测值，留档备 A/B 终态）；
   两遍复跑审计负载逐字节同一（双腿制层内锚）。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from news_flash_dedup.decide import llm_residual as lr
from news_flash_dedup.decide import machine_verify as mv

_D32_DIR = (Path(__file__).resolve().parents[3] / "log" / "temp"
            / "d32-llm-judge")
_PAIRS329 = _D32_DIR / "pairs329.json"
_ARM_A_HC = _D32_DIR / "results" / "arm-A-order-hc-run1.json"
_HARDENING = _D32_DIR / "d32-hardening.json"
_CACHE_DIR = _D32_DIR / "cache"
_FP_SHORTS = ("6e02c930", "d27e0196", "f74eaded")

pytestmark = pytest.mark.skipif(
    not (_PAIRS329.exists() and _ARM_A_HC.exists() and _HARDENING.exists()
         and _CACHE_DIR.is_dir()),
    reason="D32 产物（pairs329/arm-A run1/hardening/cache）不在本机——诚实 skip",
)


@pytest.fixture(scope="module")
def d32_assets():
    pairs_doc = json.loads(_PAIRS329.read_text(encoding="utf-8"))
    meta = {p["pair_id"]: p for p in pairs_doc["pairs"]}
    arm_a_hc = json.loads(_ARM_A_HC.read_text(encoding="utf-8"))
    hardening = json.loads(_HARDENING.read_text(encoding="utf-8"))
    return meta, arm_a_hc, hardening


def _load_prompt_like_d32(name: str) -> str:
    """d32_lib.load_prompt 同款：首个非注释非空行起为正文，strip。"""
    raw = (_D32_DIR / "prompts" / f"{name}.md").read_text(encoding="utf-8")
    lines = raw.splitlines()
    start = next(i for i, ln in enumerate(lines)
                 if ln.strip() and not ln.lstrip().startswith("#"))
    return "\n".join(lines[start:]).strip()


# ---------------------------------------------------------------- 1 · 提示词对拍

def test_prompt_sha256_matches_d32_file():
    d32_text = _load_prompt_like_d32("judge_v1")
    d32_sha = hashlib.sha256(d32_text.encode("utf-8")).hexdigest()
    assert lr.PROMPT_SHA256 == d32_sha, "内联 judge_v1 提示词与 D32 终版漂移"
    assert d32_sha == (
        "add219156ff72a0b9a8cabffc73eb975aa4ad51ae1a80c98c8a88118ba9cd149")


# ---------------------------------------------------------------- 2 · 缓存键字节级兼容

def test_cache_key_hits_existing_d32_entries(d32_assets):
    meta, _, _ = d32_assets
    sample = sorted(meta)[:5]                              # 抽样 5 对×2 顺序
    for pid in sample:
        for order in ("hc", "ch"):
            key = lr.residual_cache_key(lr.DEFAULT_MODEL, lr.JUDGE_PROMPT_VERSION,
                                        pid, lr.PROMPT_SHA256, order)
            assert (_CACHE_DIR / f"{key}.json").exists(), (
                f"按本模块键公式计算的 {order} 条目不在 D32 cache——键布局漂移")


# ---------------------------------------------------------------- 3 · 误伤数与 D32 模拟一致

def test_rule_collateral_counts_match_d32_sim(d32_assets):
    meta, arm_a_hc, hardening = d32_assets
    fired = {"R1": set(), "R2": set(), "R3": set(), "R6": set()}
    for pid, rec in arm_a_hc["pairs"].items():
        if rec["final_decision"] != "重复":
            continue                                   # 与模拟器同底：签发集
        j = rec["judge"]["judgment"]
        ta, tb = meta[pid]["text_a"], meta[pid]["text_b"]
        if mv.rule_r1_subject_codes(ta, tb):
            fired["R1"].add(pid[:12])
        if mv.rule_r2_time_anchor(j):
            fired["R2"].add(pid[:12])
        if mv.rule_r3_numeric_conflict(ta, tb):
            fired["R3"].add(pid[:12])
        if mv.rule_r6_polarity_conflict(ta, tb):
            fired["R6"].add(pid[:12])
    expected = hardening["rules_fire_on_signed"]
    for rule in ("R1", "R2", "R3", "R6"):
        assert fired[rule] == set(expected[rule]), (
            f"{rule} 触发集合与 d32-hardening.json 不符："
            f"{sorted(fired[rule])} vs {expected[rule]}")
    # 钉死计数（d32-hardening.json 在案值）：R1=0/R2=3/R3=2/R6=4
    assert {k: len(v) for k, v in fired.items()
            } == {"R1": 0, "R2": 3, "R3": 2, "R6": 4}


# ---------------------------------------------------------------- 4 · fp 三对规则直评被拦

def test_fp_trio_intercepted_by_rules(d32_assets):
    meta, arm_a_hc, _ = d32_assets
    pids = {short: next(p for p in meta if p.startswith(short))
            for short in _FP_SHORTS}
    verdict = {}
    for short, pid in pids.items():
        ta, tb = meta[pid]["text_a"], meta[pid]["text_b"]
        j = arm_a_hc["pairs"][pid]["judge"]["judgment"]
        verdict[short] = {
            "R1": mv.rule_r1_subject_codes(ta, tb),
            "R2": mv.rule_r2_time_anchor(j),
            "R3": mv.rule_r3_numeric_conflict(ta, tb),
            "R6": mv.rule_r6_polarity_conflict(ta, tb),
        }
    # 与 d32-hardening-fp.py 实测逐一对拍
    assert verdict["6e02c930"] == {"R1": False, "R2": True, "R3": False,
                                    "R6": False}
    assert verdict["d27e0196"] == {"R1": True, "R2": False, "R3": True,
                                    "R6": False}
    assert verdict["f74eaded"] == {"R1": False, "R2": False, "R3": True,
                                    "R6": False}
    # 每对至少一条规则被拦（3/3 覆盖）
    assert all(any(v.values()) for v in verdict.values())


# ---------------------------------------------------------------- 5 · 全宇宙端到端（零 API + 双腿制）

def test_full_universe_bidirectional_vs_d32(d32_assets):
    meta, _, _ = d32_assets
    # 前置：329 对×2 顺序缓存条目全在场（否则诚实 skip，不触网）
    for pid in meta:
        for order in ("hc", "ch"):
            key = lr.residual_cache_key(lr.DEFAULT_MODEL, lr.JUDGE_PROMPT_VERSION,
                                        pid, lr.PROMPT_SHA256, order)
            if not (_CACHE_DIR / f"{key}.json").exists():
                pytest.skip(f"D32 缓存条目缺失（{pid[:8]}/{order}）——诚实 skip")

    def _run(mode):
        ledger = lr.ResidualJudgeLedger()
        judge = lr.SyncResidualJudge(
            lr.ResidualJudgeConfig(model=lr.DEFAULT_MODEL,
                                    cache_dir=str(_CACHE_DIR), mv_mode=mode),
            ledger=ledger)
        outs = {pid: judge.judge_pair(pid, meta[pid]["text_a"], meta[pid]["text_b"])
                for pid in sorted(meta)}
        return outs, ledger.snapshot()

    for mode, expect_signed in ((lr.MV_AUDIT, 266), (lr.MV_GATE, 251)):
        outs, snap = _run(mode)
        tp = sum(1 for pid, o in outs.items()
                 if meta[pid]["side"] == "fn278" and o.signed)
        fp = sum(1 for pid, o in outs.items()
                 if meta[pid]["side"] == "ctrl51" and o.signed)
        assert (tp, fp) == (expect_signed, 0), (
            f"{mode} 模式 tp={tp}/278 fp={fp}/51，预期 {expect_signed}/0")
        assert snap["api_calls"] == 0 and snap["failures"] == 0
        assert snap["cache_hits"] == 2 * len(meta)

    # 双腿制（层内锚）：audit 两遍审计负载逐字节同一
    outs1, _ = _run(lr.MV_AUDIT)
    outs2, _ = _run(lr.MV_AUDIT)
    canon = lambda outs: json.dumps(
        [outs[pid].to_audit_dict() for pid in sorted(outs)],
        ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    assert canon(outs1) == canon(outs2)
