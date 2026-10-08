"""窗口T · fp 认证腿（签发权证据）：312 对硬不重复对照集 × 两级管线。

（W2Fε 文件头勘正：任务下达时为 317 对，U 侧 C1 加严返工后 v2 终版=312 对——
本文件 :63/:304 版本钉已在案；头部 317→312 按冻结实物对齐，含本行共 6 处。）

任务（主窗口 2026-09-28 追加）：把 U 交付的 312 对硬不重复对照集
（manifest：workspace log/temp/tn-expansion/tn_expansion_v1.json，P05 兼容字段
+全文+双侧 sha256）作为候选对喂入管线——**规则链与残判层两级都跑**，两级
各自 signed 数=fp。预期两级 fp 均=0；任何 signed 对=认证事故：逐对取证
（哪层签的/什么码/双向记录/机验触发）落 anomalies 段，测试诚实红。

设计纪律：
- **金标 P05/391 对路径零改动**——本文件独立 fixture；驱动件经 importlib
  只读复用 test_p23_uat.py 内部函数（_ctx_mapping/_facts_for/_load_norm_dictionary/
  _residual_config/_item_id/_record_id；d32-prepare-pairs.py 同款工艺），
  CommitContext/commit_one 调用形态与 P23 金标回放逐行同源。
- env 开关化：`P23_TN_MANIFEST`=manifest 路径（缺省→全文件 skip，不影响
  现役套件）；残判层沿用 `P23_RESIDUAL_JUDGE=llm_bidi_v1` +
  `P23_RESIDUAL_JUDGE_CACHE_DIR`（本腿指向 log/temp/tn-expansion/residual-cache，
  与 D32 缓存物理分开）；Fact 供给/词典沿用 P23_FACT_SUPPLY/P23_NORM_DICT
  （认证跑法=生产镜像 rule_baseline_v2+norm_dict_v1）。
- 同文对闸（先报再跑条款）：装载期断言无 text_a_sha256==text_b_sha256 对
  （EXACT_TEXT_MATCH 风险；v1 装载期实测 0/317 已核，v2=312 对由场景 1
  装载期同闸逐对强校验）——命中即红并提示先报主窗口。
- 预算闸：残判层 API 调用上限 `P23_TN_CERT_BUDGET`（默认 700；312×2=624 基线
  +重试余量）——超限即停止残判判新对、记 budget_aborted，诚实红不硬撑。
- 失败纪律：驱动异常单列 failures（INV-4，不折算边界）；残判 failure 单列。
- 确定性双腿制：指标 JSON 含 tn_payload_hash（逐对两级结果 canonical JSON 的
  sha256，run_id 无关）——连跑两遍须逐字节同一；第二遍残判台账 api_calls=0。
  （W2Fε：同一性对拍已由 pytest 外手工 diff 移入测试内锚——见场景 4
  test_tn_cert_two_leg_payload_hash_identity，首腿如实申报、第二腿自动对拍。）
- 指标 JSON 落 `log/temp/p23-tn-cert-<run>.json`（run=UTC HHMMSS）。

跑站命令（仓根；双腿制连跑两遍）：

    $env:PYTHONPATH='src'; $env:PYTHONIOENCODING='utf-8'
    $env:PYTHONDONTWRITEBYTECODE='1'; $env:DEPLOY_ENV='test'
    $env:P23_FACT_SUPPLY='rule_baseline_v2'
    $env:P23_NORM_DICT='<仓>\\data\\dictionaries\\norm_dict_v1.json'
    $env:P23_RESIDUAL_JUDGE='llm_bidi_v1'
    $env:P23_RESIDUAL_JUDGE_CACHE_DIR='<workspace>\\log\\temp\\tn-expansion\\residual-cache'
    $env:P23_TN_MANIFEST='<workspace>\\log\\temp\\tn-expansion\\tn_expansion_v1.json'
    & '.venv-v1\\Scripts\\python.exe' -m pytest -p no:cacheprovider `
        --basetemp '.\\tmp\\p23-tn-cert' -v tests/integration/test_p23_tn_cert.py
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from news_flash_dedup.commit.coordinator import CommitContext, commit_one
from news_flash_dedup.commit.fake_store import FakeCommitStore
from news_flash_dedup.decide import llm_residual as _lr

# ---------- 开关与路径 ----------
_TN_MANIFEST_ENV = "P23_TN_MANIFEST"
_TN_BUDGET_ENV = "P23_TN_CERT_BUDGET"
_TN_BUDGET_DEFAULT = 700
# 认证集版本钉（主窗口 steering：fp 分母必须是 v2 终版 312 对，对拍后再跑/对账）
_TN_EXPECT_VERSION_ENV = "P23_TN_EXPECT_DATASET_VERSION"

_REPO_ROOT = Path(__file__).resolve().parents[2]
_WORKSPACE_ROOT = Path(__file__).resolve().parents[3]
_WORKSPACE_LOG = _WORKSPACE_ROOT / "log"

pytestmark = pytest.mark.skipif(
    not os.environ.get(_TN_MANIFEST_ENV),
    reason=f"{_TN_MANIFEST_ENV} 未设置——fp 认证腿不挂现役套件（诚实 skip）",
)


def _load_p23_uat_module():
    """只读复用 test_p23_uat.py 内部驱动件（d32-prepare-pairs.py 同款工艺）。"""
    spec = importlib.util.spec_from_file_location(
        "p23_uat_for_tn", _REPO_ROOT / "tests" / "integration" / "test_p23_uat.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["p23_uat_for_tn"] = mod
    spec.loader.exec_module(mod)
    return mod


def _dup_text_groups(pairs: list[dict]) -> dict:
    groups: dict[tuple, list[str]] = defaultdict(list)
    for p in pairs:
        groups[tuple(sorted([p["text_a_sha256"], p["text_b_sha256"]]))].append(
            p["pair_id"])
    dups = {k: v for k, v in groups.items() if len(v) > 1}
    return {"group_count": len(dups),
            "involved_pairs": sum(len(v) for v in dups.values()),
            "note": ("manifest 内部重复文本对（不同 pair_id 同文本对，U 侧组批属性，"
                     "认证计数按 manifest 条目级；预期 fp=0 不受影响）"),
            "groups": {k[0][:12] + "|" + k[1][:12]: v for k, v in
                       sorted(dups.items())}}


@pytest.fixture(scope="module")
def tn_replay():
    """312 对两级回放 fixture（规则链→残判层；模块级跑一遍供多场景断言）。

    驱动形态逐行镜像 test_p23_uat.replay_run._drive：_ctx_mapping 上下文 →
    CommitContext（固定候选=history，coverage_complete=True 占位登记）→
    commit_one → public 五字段。

    09-29 夹具适配 W2Fγ 幂等语义（W2Fζ 异常①主窗口裁定方案 a）：逐对独立
    FakeCommitStore（决策层与 store 层解耦，等价旧跑静默覆盖时代各对独立
    决策的测量语义）——W2Fγ 条 33 fail-closed 幂等语义下，manifest 仅 142
    唯一 current 端点（77 键复用覆盖 247 对）×单 store 复用会致 170 对被
    CommitOneError 诚实单列（非 fp 非漂移）。构造模式参照 W2Fζ 探针
    log/temp/winw2fz-tn-probe.py（逐对独立 store 全量 312 零漂移 fp=0、
    probe_payload_hash=93fc1818… 与旧锚逐字节同一）；逐对独立 store 下
    decision_watermark_seq 恒 1。
    """
    p23 = _load_p23_uat_module()
    manifest_path = Path(os.environ[_TN_MANIFEST_ENV])
    manifest_raw = manifest_path.read_bytes()
    manifest = json.loads(manifest_raw.decode("utf-8"))
    manifest_sha = hashlib.sha256(manifest_raw).hexdigest()
    expect_version = os.environ.get(_TN_EXPECT_VERSION_ENV)
    if expect_version:
        actual_version = manifest.get("dataset_version")
        if actual_version != expect_version:
            raise RuntimeError(
                f"认证集 dataset_version 对拍失败：实物={actual_version} "
                f"期望={expect_version}——拒绝对非裁定版本签发（先报主窗口）")
    pairs = manifest["pairs"]

    residual = p23._residual_config()          # None=残判层关闭（本腿要求开启）
    budget = int(os.environ.get(_TN_BUDGET_ENV, str(_TN_BUDGET_DEFAULT)))
    _norm_dictionary, _norm_dictionary_version = p23._load_norm_dictionary()

    items: list[dict] = []
    failures: list[dict] = []
    budget_aborted = 0

    scope_id = "tn_expansion_v1"
    business_date = "2000-01-01"
    ordered = sorted(pairs, key=lambda p: p["pair_id"])
    for idx, pair in enumerate(ordered):
        pid = pair["pair_id"]
        text_a, text_b = pair["text_a"], pair["text_b"]
        history_key, current_key = pair["endpoint_row_keys"]
        row_a = SimpleNamespace(
            raw_text=text_a,
            raw_hash=hashlib.sha256(text_a.encode("utf-8")).hexdigest())
        row_b = SimpleNamespace(
            raw_text=text_b,
            raw_hash=hashlib.sha256(text_b.encode("utf-8")).hexdigest())
        try:
            # arrival_seq 严格正整数（pair_compare L186：0 < history < current）
            history = p23._ctx_mapping(history_key, row_a, idx * 2 + 1,
                                       scope_id, business_date)
            current = p23._ctx_mapping(current_key, row_b, idx * 2 + 2,
                                       scope_id, business_date)
            store = FakeCommitStore()     # 逐对独立（见 fixture docstring 裁定注记）
            ctx = CommitContext(
                scope_id=scope_id,
                business_date=business_date,
                arrival_seq=idx * 2 + 2,
                current=current,
                candidates=(history,),
                visible_seq=idx * 2 + 2,
                prepared_seq=idx * 2 + 2,
                pipeline_version="dedup_v1",
                coverage_complete=True,   # 显式占位（固定候选路径；同 P23 登记口径）
            )
            outcome = commit_one(ctx, store,
                                 decision_watermark_seq=1,
                                 audit_complete=True,   # M-01 显式表态（窗口T 第 4 处去默认）
                                 dictionary=_norm_dictionary,
                                 dictionary_version=_norm_dictionary_version)
            if outcome.state != "committed" or outcome.decide_outcome is None:
                raise RuntimeError(f"commit_one 未提交：state={outcome.state!r}")
        except Exception as exc:                         # 失败不冒充边界（INV-4）
            failures.append({"pair_id": pid,
                             "error_type": type(exc).__name__,
                             "error": str(exc)[:200]})
            continue

        decide = outcome.decide_outcome
        pub = decide.to_public_dict()
        item = {
            "pair_id": pid,
            "category": pair.get("category"),
            "batch": pair.get("batch"),
            "public": pub,
            "internal_code": decide.internal_code,
            "pair_codes": dict(decide.pair_codes),
            "history_item_id": history["item_id"],
        }
        # 残判层（第二级）：规则链判"边界"才进入；预算闸 fail-closed
        if residual is not None and pub["decision"] == "边界case/疑难case":
            if residual.judge.ledger.api_calls >= budget:
                budget_aborted += 1
                item["residual"] = {"pair_id": pid, "cell": "budget_aborted",
                                    "signed": False, "suspicion_score": None}
            else:
                try:
                    res_out = residual.judge.judge_pair(pid, text_a, text_b)
                    item["residual"] = res_out.to_audit_dict()
                except Exception as exc:                 # 残判技术失败单列
                    item["residual"] = {"pair_id": pid, "cell": "failure",
                                        "signed": False, "suspicion_score": None,
                                        "error_type": type(exc).__name__,
                                        "error": str(exc)[:200]}
        items.append(item)

    # ---------- 两级指标 + 事故取证 ----------
    rule_signed = [i for i in items if i["public"]["decision"] == "重复"]
    res_judged = [i for i in items
                  if i.get("residual") is not None
                  and i["residual"].get("cell") != "budget_aborted"]
    res_signed = [i for i in res_judged if i["residual"].get("signed")]
    anomalies = []
    for i in rule_signed:
        anomalies.append({
            "pair_id": i["pair_id"], "layer": "rule_chain",
            "internal_code": i["internal_code"], "pair_codes": i["pair_codes"],
            "public": i["public"], "category": i["category"], "batch": i["batch"]})
    for i in res_signed:
        anomalies.append({
            "pair_id": i["pair_id"], "layer": "residual_llm",
            "category": i["category"], "batch": i["batch"],
            "rule_decision": i["public"]["decision"],
            "rule_internal_code": i["internal_code"],
            "residual": i["residual"]})
    decision_dist: dict[str, int] = {}
    code_dist: dict[str, int] = {}
    for i in items:
        decision_dist[i["public"]["decision"]] = (
            decision_dist.get(i["public"]["decision"], 0) + 1)
        code_dist[i["internal_code"]] = code_dist.get(i["internal_code"], 0) + 1
    res_cells: dict[str, int] = {}
    for i in res_judged:
        cell = i["residual"].get("cell", "failure")
        res_cells[cell] = res_cells.get(cell, 0) + 1

    run_id = "tncert" + datetime.now(timezone.utc).strftime("%H%M%S%f")  # W-R3b 秒级→微秒
    per_pair = sorted(
        ({"pair_id": i["pair_id"],
          "rule": {"decision": i["public"]["decision"],
                    "internal_code": i["internal_code"]},
          "residual": i.get("residual")}
         for i in items),
        key=lambda r: r["pair_id"])
    payload_hash = hashlib.sha256(json.dumps(
        per_pair, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode("utf-8")).hexdigest()

    metrics = {
        "run_id": run_id,
        "leg": "窗口T-fp认证腿（签发权证据）：312 硬不重复对照×两级管线",
        "manifest": {"path": str(manifest_path),
                      "sha256": manifest_sha,
                      "dataset_version": manifest.get("dataset_version"),
                      "provenance_type": manifest.get("provenance_type"),
                      "counts": manifest.get("counts")},
        "fact_supply": os.environ.get("P23_FACT_SUPPLY", "identity"),
        "norm_dictionary_version": _norm_dictionary_version,
        "counts": {"pairs": len(pairs), "replayed": len(items),
                    "failures": len(failures)},
        "rule_level": {"signed_fp": len(rule_signed),
                        "decision_distribution": decision_dist,
                        "internal_code_distribution": code_dist},
        "residual_level": ({
            "enabled": True,
            "switch": f"{p23._RESIDUAL_ENV}={residual.switch}",
            "model": residual.config.model,
            "prompt_version": _lr.JUDGE_PROMPT_VERSION,
            "prompt_sha256": _lr.PROMPT_SHA256,
            "mv_mode": residual.config.mv_mode,
            "cache_dir": str(residual.config.cache_dir),
            "judged_pairs": len(res_judged),
            "signed_fp": len(res_signed),
            "cells": res_cells,
            "ledger": residual.judge.ledger.snapshot(),
            "budget": budget, "budget_aborted": budget_aborted,
        } if residual is not None else {"enabled": False}),
        "anomalies": anomalies,
        "anomalies_note": ("任何 signed 对=认证事故：layer=rule_chain（internal_code/"
                           "pair_codes）| residual_llm（双向记录+机验触发全量在 "
                           "residual 字段）——预期空"),
        "failures": failures,
        "duplicate_text_pair_groups": _dup_text_groups(pairs),
        "tn_payload_hash": payload_hash,
    }
    out_path = _WORKSPACE_LOG / "temp" / f"p23-tn-cert-{run_id}.json"
    out_path.write_text(json.dumps(metrics, ensure_ascii=False, indent=2,
                                   sort_keys=True), encoding="utf-8")
    print(f"\n[P23-TN-CERT] metrics json: {out_path}")
    print(f"[P23-TN-CERT] rule_fp={len(rule_signed)} "
          f"residual_fp={len(res_signed) if residual is not None else '<off>'} "
          f"judged={len(res_judged)} anomalies={len(anomalies)}")
    if residual is not None:
        print(f"[P23-TN-CERT] ledger={metrics['residual_level']['ledger']} "
              f"payload_hash={payload_hash}")
    return SimpleNamespace(items=items, failures=failures, metrics=metrics,
                           metrics_path=out_path, anomalies=anomalies,
                           residual=residual, manifest=manifest)


# ---------- 场景 ----------

def test_tn_manifest_integrity(tn_replay):
    """场景 1 · manifest 完整性 + 同文对闸（先报再跑条款）。

    对数与 manifest.counts.total_pairs 自洽（版本无关：任务下达时 317，U 侧
    C1 加严返工后 312——以冻结实物为准，不硬编码）。"""
    pairs = tn_replay.manifest["pairs"]
    expected = tn_replay.manifest.get("counts", {}).get("total_pairs")
    if expected is not None:
        assert len(pairs) == expected, (
            f"pairs({len(pairs)}) != counts.total_pairs({expected})——manifest 自洽性破裂")
    assert all(p["gold_label"] == "不重复" and p["status"] == "clear" for p in pairs)
    ids = [p["pair_id"] for p in pairs]
    assert len(set(ids)) == len(ids), "pair_id 不唯一"
    for p in pairs:
        assert hashlib.sha256(
            p["text_a"].encode("utf-8")).hexdigest() == p["text_a_sha256"]
        assert hashlib.sha256(
            p["text_b"].encode("utf-8")).hexdigest() == p["text_b_sha256"]
    same_text = [p["pair_id"] for p in pairs
                 if p["text_a_sha256"] == p["text_b_sha256"]]
    assert not same_text, (
        f"同文对（EXACT_TEXT_MATCH 风险）{len(same_text)} 对：{same_text[:3]}"
        "——按任务条款须先报主窗口再跑")


def test_tn_cert_two_level_fp_zero(tn_replay):
    """场景 2 · 两级 fp 均=0（签发权证据）；任何 signed=认证事故（取证在 anomalies）。"""
    assert not tn_replay.failures, f"回放驱动失败 {len(tn_replay.failures)} 对"
    m = tn_replay.metrics
    assert m["counts"]["replayed"] == m["counts"]["pairs"]
    assert m["rule_level"]["signed_fp"] == 0, (
        f"规则链签发 fp={m['rule_level']['signed_fp']}——认证事故，"
        "逐对取证见 anomalies")
    res = m["residual_level"]
    assert res.get("enabled") is True, "残判层未启用（需 P23_RESIDUAL_JUDGE）"
    assert res["budget_aborted"] == 0, "预算闸触发——本腿证据不完整"
    assert res["signed_fp"] == 0, (
        f"残判层签发 fp={res['signed_fp']}——认证事故，逐对取证见 anomalies")
    assert m["anomalies"] == []


def test_tn_cert_determinism_and_budget(tn_replay):
    """场景 3 · 确定性锚 + 预算闸 + 台账自洽（跨跑同一性对拍见场景 4 测试内锚）。"""
    res = tn_replay.metrics["residual_level"]
    assert res["ledger"]["failures"] == 0, "残判层存在 failure（INV 式单列被触发）"
    assert res["ledger"]["api_calls"] <= res["budget"], (
        f"API 调用 {res['ledger']['api_calls']} 超预算 {res['budget']}")
    assert (res["ledger"]["api_calls"] + res["ledger"]["cache_hits"]
            == 2 * res["judged_pairs"]), "台账与判定数不自洽（每对 hc+ch 两次）"
    assert len(tn_replay.metrics["tn_payload_hash"]) == 64


def test_tn_cert_two_leg_payload_hash_identity(tn_replay):
    """场景 4 · 双腿同一性测试内锚（W2Fε 自 pytest 外对拍移入）。

    原纪律："连跑两遍，tn_payload_hash 须逐字节同一"由跑手在 pytest 外手工
    diff 两份指标 JSON（文件头 :24 已声明）。现移入测试内：扫描 log/temp 既
    往 p23-tn-cert-*.json，取与本跑同一 manifest（sha256 逐字节同）的腿逐一对
    拍 tn_payload_hash；任一不一致即红（确定性被破坏/缓存污染/供给漂移）。
    首腿（无同 manifest 既往腿）如实打印申报——本跑不产生对拍证据，不冒充绿。
    """
    current = tn_replay.metrics["tn_payload_hash"]
    manifest_sha = tn_replay.metrics["manifest"]["sha256"]
    temp_dir = tn_replay.metrics_path.parent
    prior_legs = []
    for path in sorted(temp_dir.glob("p23-tn-cert-*.json")):
        if path.name == tn_replay.metrics_path.name:
            continue                                          # 本跑自身除外
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue                                          # 非 JSON/不可读=非腿档
        if data.get("manifest", {}).get("sha256") == manifest_sha:
            prior_legs.append((path.name, data.get("tn_payload_hash")))
    if not prior_legs:
        print("\n[tn-cert 双腿制] 首腿：log/temp 无同 manifest 既往腿——"
              "本跑仅落盘，同一性对拍由紧随的第二腿执行")
        return
    for name, prior_hash in prior_legs:
        assert prior_hash == current, (
            f"双腿 tn_payload_hash 不一：{name}={prior_hash} vs "
            f"本跑={current}——确定性破坏（残判缓存/供给/码漂移必查）")
