# -*- coding: utf-8 -*-
"""C5 扩样外样本初测（过拟合检验首轮）· dev split 319 对 · 固定候选轨+残判 LLM。

定位（附七 C 批 C5 窗；任务书=主窗口 prompt 2026-09-30）：
- 底本 = gold-expansion/manifest-v2.json（C1 工程窗产物，R184 闭环；
  文件 SHA256 钉死 88100fc4…b73，dataset_version 逐字节复算，材料=双工作簿
  SHA/字节数对拍——INV-1 金标只读，不符即红不 skip）。
- 口径 = ④终验同族（log/④号任务金标评测报告.md）：固定候选轨 (a)（候选由
  金标端点直接给定，不走召回、不连 ES/Milvus）+ 恒等投影 fact supply
  （21:4x 主窗口裁定脚手架，零语义主张）+ 残判 LLM（llm_bidi_v1 =
  decide/llm_residual.SyncResidualJudge 双向裁判+机验 audit，judge_v1
  提示词/model=qwen-turbo 与 D32 终版逐字节同）。
- 与 P23 UAT 的关系：v2 manifest 为 P05 键超集（pairs 增 split/stratum/
  strata/sampling_rate 等键；无 synthetic_plan）。本稿为**读取适配新文件**
  ——tests/integration/test_p23_uat.py 既有字节零改动；本文件不复用其
  P05 专属常量（GOLD_MANIFEST_PATH/FROZEN_COUNTS/EXPECTED_MATERIALS 等），
  仅同文复写与 P05 无关的机械件（恒等投影模板/五字段合同/CP 对拍），src
  冻结件（metrics 聚合器/CP 公式/commit_one/FakeCommitStore/llm_residual）
  一律 import 复用零改动。
- 到达序适配（v2 无 synthetic_plan 的诚实合成，不冒充官方顺序）：
  seq = material_rank×1,000,000 + excel_row×4 + side_rank（side 序
  single<A<B；material_rank=sorted(material_id) 序）——同材料内 excel_row
  自然序、跨材料 deterministic；合成事实落指标 JSON arrival_order 段。
  scope=synthetic:<current 端点 material_id[:16]>（P05 负表回退同口径），
  business_date=2000-01-01 synthetic。
- 回放适配注记 #2（2026-09-30 烟测红证据 c5-oos-run-smoke.txt：144/319
  CommitOneError M-3 幂等拒收）：v2 S_MULTI 多行组 C(n,2) 对使同一行
  以 current 端点出现在多对中；单一 FakeCommitStore 下该行二次提交与
  既有主记录 audit_ids 分歧（M-3 幂等拒收）。P05 全集端点互斥（多行组
  72/245 行在 P05 未覆盖），从未触发。**决策输入全在 ctx**（coordinator
  L147-152：decide_for_task(history=candidates[-1])），store 纯落库槽
  不喂决策——故改为**每对独立新 FakeCommitStore + watermark=1**：固定
  候选轨 (a)"候选由金标直接给定"口径的忠实实现（每对=独立两报告决策
  世界），零口径削减；跨对 fake 持久层干扰本非口径成分（P05 零失败
  实证决策从未依赖跨对 store 态）。
- 预算闸（照 D2 影子小闸精神，vector/backfill.py §6.3 同型）：默认 200k
  tok/日（UTC+8 业务日），计数器落盘 log/temp/c5-oos-budget-counter.json
  （重启不可绕闸；不可读/落盘失败 fail-closed 拒一切活调）；实际扣账取
  API 响应 usage.total_tokens，缺 usage → UTF-8 字节数保守上界估值
  （1 token ≥ 1 byte 恒成立，估值事件入账）。超闸对=budget_blocked 单列
  （不折算边界、不冒充签发），次日同令续跑（残判缓存落盘即续跑状态）。
  **本窗一次性裁定（主窗口 2026-09-30 决策记录 #13）**：C5 专用闸经
  C5_OOS_DAILY_BUDGET 提至 600k tok/日（本窗有效，不改 D2 shadow 小闸；
  逐次实扣/落盘/超闸自停纪律不变），目标=当日全覆盖收官；闸值随指标
  JSON residual_judge.budget.daily_token_budget 逐腿落盘可溯。
- 残判缓存：C5 自目录 log/temp/c5-oos-residual-cache（**D32 缓存目录原位
  只读，不写入**——D32 为冻结证据资产）；36 个 dev 对（S_UPG/S_HOLD 遗留
  对，pair_id 与 P05/D32 同一）的 72 条双向条目以**副本播种**（种子清单
  c5-oos-cache-seed-manifest.json 逐件 SHA256 登记来源），新对活调结果
  写入 C5 自目录。缓存命中=零 API 如实入账。
- 硬闸与判读：fp=0 硬闸（pair/merged/residual 三口径任一 fp → 红，立即
  停报主窗口）；recall 跌幅分级判读（合并 recall ≥0.96=泛化守住 /
  0.90–0.96=预期带内 / <0.90=异常需根因——对读常驻轨锚 328/0/12/51
  recall=0.9647058823529412）；锚对读只判读不调阈（INV-6 同纪律）。
- 金标边界对口径：gold_label=边界case/疑难case 34 对**只报分布不入二元
  指标**（P23 hold 集同口径：无二元真值可判正误；aggregators 原生排除
  非重复/不重复标签）；stratum/role_ambiguous 分层单列。
- 覆盖诚实：回放失败/预算阻断/活调帽阻断一律单列且**使覆盖断言红**
  （不空转冒充验证）；部分覆盖态的指标 JSON 仍落盘（judged 子集如实），
  但本套件不宣称绿。

跑站命令（仓根 news-flash-dedup；footer 分钟级向下取整）：

    $env:PYTHONPATH='src'
    $env:PYTHONIOENCODING='utf-8'
    $env:PYTHONDONTWRITEBYTECODE='1'
    $env:DEPLOY_ENV='test'
    $env:P23_CONFIRM_UAT='1'
    $env:P23_RESIDUAL_JUDGE='llm_bidi_v1'
    $env:DEDUP_EMBEDDING_KEY_FILE='C:\\Users\\ASUS\\Desktop\\文本去重\\workspace-dedup\\.env.dedup'
    # 可选：$env:C5_OOS_MAX_LIVE_CALLS='4'   # 烟测活调帽（正式跑勿设）
    & '.venv-v1\\Scripts\\python.exe' -m pytest -p no:cacheprovider `
        --basetemp '.\\tmp\\c5-oos-pytest' -v tests/integration/test_c5_oos_replay.py `
        > .\\log\\temp\\c5-oos-run-<HHMM>.txt
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from news_flash_dedup.metrics import (
    MetricsAggregator,
    ReplayResult,
    _safe_lower_bound,
    compute_payload_hash,
)

# ---------- 常量（钉死值全部可溯源；v2 专属，与 P05 常量零共享） ----------

P23_UAT_ENV = "P23_CONFIRM_UAT"               # 族闸照 winw2fz 档（④证据 L6/L12）
RESIDUAL_SWITCH_ENV = "P23_RESIDUAL_JUDGE"    # 残判层开关（窗口T 同族）
RESIDUAL_ENABLED_VALUE = "llm_bidi_v1"
DEPLOY_ENV_VAR = "DEPLOY_ENV"
KEY_FILE_ENV = "DEDUP_EMBEDDING_KEY_FILE"     # 任务书：API key 现读 .env.dedup

_REPO_ROOT = Path(__file__).resolve().parents[2]          # news-flash-dedup
_WORKSPACE_ROOT = _REPO_ROOT.parent                       # workspace-dedup
REPO_LOG_TEMP = _REPO_ROOT / "log" / "temp"
GOLD_V2_MANIFEST_PATH = _WORKSPACE_ROOT / "gold-expansion" / "manifest-v2.json"
_DEFAULT_WORKBOOK_DIR = _WORKSPACE_ROOT.parent            # 文本去重
WORKBOOK_NAMES = (
    "v2_重复case_Fact特征分类标注版_与用户标注一致版.xlsx",
    "v2_重复case_第二批_会议口径标注版.xlsx",
)
WORKBOOK_DIR_ENV = "C5_OOS_WORKBOOK_DIR"

# 任务书钉死：manifest-v2 文件 SHA256（2026-09-30 C5 窗 Get-FileHash 实测吻合）
EXPECTED_FILE_SHA256 = (
    "88100fc4cc982d21597ed90ff3121038e852b35fa1d10710daf920bc05421b73"
)
EXPECTED_DATASET_VERSION = (
    "ds-c1-expansion-v2-125e856db8326e20cddffffb8f96308035b5ab4777d2551af06e066dbca4fd55"
)
# manifest materials 钉死项（与 P05 同双工作簿；C5 窗 Get-FileHash 实测吻合）
EXPECTED_MATERIALS = {
    "dbfda9677e96ab99a11f5259c2f9f231d23efed5460a8164e38ab22c1a33d26b": 132595,
    "83e635beb4209909087f577c2717c7977017dea22204cce764bbbf32954fac6b": 48003,
}
# v2 冻结计数（manifest counts 段钉死；C5 窗 2026-09-30 实测吻合）
FROZEN_COUNTS = {
    "unique_pairs": 418,
    "label_重复": 358,
    "label_不重复": 24,
    "label_边界case/疑难case": 36,
    "split_dev": 319,
    "split_validation": 31,
    "split_test": 68,
    "stratum_S_MULTI": 330,
    "stratum_S_UPG": 54,
    "stratum_S_CROSS": 30,
    "stratum_S_HOLD": 4,
}
# dev split 冻结分布（C5 窗实测；场景 3 钉守）
DEV_FROZEN = {
    "pairs": 319,
    "重复": 272,
    "不重复": 13,
    "边界case/疑难case": 34,
    "S_MULTI": 261,
    "S_UPG": 34,
    "S_CROSS": 22,
    "S_HOLD": 2,
    "role_ambiguous": 2,      # strata 副层（R183 口径⑪ 登记对）
}
ROLE_AMBIGUOUS_TAG = "role_ambiguous"

PUBLIC_KEYS = {"item_id", "text", "decision", "duplicate_ids", "reason"}
DECISION_VOCAB = {"重复", "不重复", "边界case/疑难case"}

# 常驻轨锚（④终验注册接管值；对读用，不调阈）
ANCHOR_MERGED = {"tp": 328, "fp": 0, "fn": 12, "tn": 51}
ANCHOR_RECALL = 328 / 340                     # 0.9647058823529412
RECALL_BAND_HOLD = 0.96                       # ≥0.96 泛化守住
RECALL_BAND_FLOOR = 0.90                      # 0.90–0.96 预期带内；<0.90 异常

# 预算闸（照 D2 小闸精神自设；任务书 200k tok/日+落盘计数）
DAILY_TOKEN_BUDGET = int(os.environ.get("C5_OOS_DAILY_BUDGET", "200000"))
BUDGET_PATH = Path(os.environ.get(
    "C5_OOS_BUDGET_PATH", str(REPO_LOG_TEMP / "c5-oos-budget-counter.json")))
MAX_LIVE_CALLS_ENV = "C5_OOS_MAX_LIVE_CALLS"  # 烟测/纯回放腿用活调帽（缺省无帽）

CACHE_DIR = Path(os.environ.get(
    "C5_OOS_CACHE_DIR", str(REPO_LOG_TEMP / "c5-oos-residual-cache")))
MODEL = os.environ.get("C5_OOS_MODEL", "qwen-turbo")

_SYNTHETIC_BUSINESS_DATE = "2000-01-01"
_BUSINESS_TZ = timezone(timedelta(hours=8))   # 业务日 UTC+8（embedding_client 同型）
_SIDE_RANK = {"single": 0, "A": 1, "B": 2}


# ---------- 闸门（族闸照 winw2fz 档：DEPLOY_ENV=test + CONFIRM + 无 PROD_*） ----------

def _gate_open(env) -> bool:
    """harness 级闸门：DEPLOY_ENV=test + P23_CONFIRM_UAT=1 + 无 PROD_* 泄漏。"""
    if env.get(DEPLOY_ENV_VAR) != "test":
        return False
    if env.get(P23_UAT_ENV) != "1":
        return False
    if any(k.startswith("PROD_") for k in env):
        return False
    return True


pytestmark = pytest.mark.skipif(
    not _gate_open(os.environ),
    reason=(f"C5 OOS gate not open (need {DEPLOY_ENV_VAR}=test, "
            f"{P23_UAT_ENV}=1, no PROD_*)"),
)


# ---------- 规范哈希助手（与 P23 UAT 同文；takeover 对账脚本同文） ----------

def _canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _record_id(row_key: str) -> str:
    """确定性 record_id（P23 同 namespace：行派生身份跨回放轨可比——
    S_UPG/S_HOLD 遗留对与 winw2fz dump 逐对可 diff；FakeCommitStore 内存态，
    无跨 run 持久碰撞面）。"""
    return _digest("p23-rid:" + row_key)


def _item_id(row_key: str) -> str:
    return "item-" + _digest("p23-item:" + row_key)[:16]


# ---------- 金标资产装载（INV-1：哈希不符=红；工作簿缺位=诚实 skip） ----------

def _load_manifest() -> dict:
    if not GOLD_V2_MANIFEST_PATH.exists():
        pytest.skip(f"v2 manifest 不在预期位置：{GOLD_V2_MANIFEST_PATH}")
    return json.loads(GOLD_V2_MANIFEST_PATH.read_text(encoding="utf-8"))


def _verify_manifest_pinned(manifest: dict) -> None:
    """INV-1：文件 SHA256（任务书钉值）+ dataset_version 逐字节复算 +
    冻结计数 + materials 钉死项对拍。不符即红（不 skip）。"""
    digest = _file_sha256(GOLD_V2_MANIFEST_PATH)
    assert digest == EXPECTED_FILE_SHA256, (
        f"manifest-v2 文件 SHA256={digest} != 任务书钉值 "
        f"{EXPECTED_FILE_SHA256}——金标被改动（INV-1 违规，红）"
    )
    recomputed = "ds-c1-expansion-v2-" + _digest(_canonical(
        {key: value for key, value in manifest.items() if key != "dataset_version"}
    ))
    assert manifest.get("dataset_version") == recomputed, (
        "manifest-v2 dataset_version 复算不符——金标被改动（INV-1 违规，红）"
    )
    assert recomputed == EXPECTED_DATASET_VERSION, (
        f"dataset_version {recomputed!r} 与钉值不符"
    )
    counts = manifest["counts"]
    assert counts.get("unique_pairs") == FROZEN_COUNTS["unique_pairs"]
    labels = counts["label_counts"]
    for label in ("重复", "不重复", "边界case/疑难case"):
        assert labels.get(label) == FROZEN_COUNTS[f"label_{label}"], (
            f"label_counts[{label!r}]={labels.get(label)!r} 与冻结值不符"
        )
    splits = counts["split_counts"]
    for split, key in (("dev", "split_dev"), ("validation", "split_validation"),
                       ("test", "split_test")):
        assert splits.get(split) == FROZEN_COUNTS[key]
    strata = counts["stratum_counts"]
    for stratum in ("S_MULTI", "S_UPG", "S_CROSS", "S_HOLD"):
        assert strata.get(stratum) == FROZEN_COUNTS[f"stratum_{stratum}"]
    materials = {m["material_id"]: m for m in manifest["materials"]}
    assert set(materials) == set(EXPECTED_MATERIALS), (
        "manifest-v2 materials 钉死集与在案集（双工作簿）不符"
    )
    for material_id, byte_size in EXPECTED_MATERIALS.items():
        assert materials[material_id]["byte_size"] == byte_size


def _build_replay_plan(manifest: dict, rows: dict):
    """v2 dev 对 → 回放计划（到达序诚实合成，不冒充官方 synthetic_plan）。

    - dev split 全集（split=='dev'）；status 必须全 clear（钉守，hold 形态
      出现即红——v2 实测 319/319 clear）；
    - 端点必须恰 2 个（v2 dev 无合并条目，实测 0）；
    - seq 合成：material_rank×1e6 + excel_row×4 + side_rank（deterministic，
      同材料 excel_row 自然序；不重复 sheet A/B 侧 +1/+2 错开——P05 负表
      100000+excel_row*2[+1] 同精神）；history=低 seq（INV-2）；
    - scope=synthetic:<current material_id[:16]>；business_date=2000-01-01。
    """
    material_rank = {mid: idx for idx, mid in enumerate(sorted(
        {key.split(":", 1)[0] for key in rows}))}
    clear = []
    for pair in manifest["pairs"]:
        if pair.get("split") != "dev":
            continue
        assert pair.get("status") == "clear", (
            f"dev 对 {pair['pair_id'][:16]} status={pair.get('status')!r} "
            "!= clear——v2 hold 形态超出本适配假设（结构性障碍，红）"
        )
        endpoints = list(pair["endpoint_row_keys"])
        assert len(endpoints) == 2, (
            f"dev 对 {pair['pair_id'][:16]} 端点数={len(endpoints)} != 2——"
            "合并条目形态超出本适配假设（v2 dev 实测为 0；结构性障碍，红）"
        )
        for key in endpoints:
            assert key in rows, f"金标对端点 {key!r} 不在工作簿目录内"
            assert rows[key].raw_text, f"金标对端点 {key!r} 正文为空"
        seqs = {}
        for key in endpoints:
            row = rows[key]
            seqs[key] = (material_rank[row.material_id] * 1_000_000
                         + row.excel_row * 4 + _SIDE_RANK[row.side])
        history_key = min(endpoints, key=lambda k: (seqs[k], k))
        current_key = max(endpoints, key=lambda k: (seqs[k], k))
        entry = SimpleNamespace(
            pair_id=pair["pair_id"],
            gold_label=pair["gold_label"],
            stratum=pair["stratum"],
            strata=list(pair.get("strata") or []),
            sampling_rate=pair.get("sampling_rate"),
            family_id=pair["family_id"],
            history_key=history_key,
            current_key=current_key,
            history_seq=seqs[history_key],
            current_seq=seqs[current_key],
            scope_id=f"synthetic:{rows[current_key].material_id[:16]}",
            business_date=_SYNTHETIC_BUSINESS_DATE,
        )
        clear.append(entry)
    return clear


@pytest.fixture(scope="module")
def gold_assets():
    """v2 金标资产装载：manifest（钉死复算）+ 工作簿（哈希对拍）+ dev 回放计划。"""
    manifest = _load_manifest()
    _verify_manifest_pinned(manifest)

    workbook_dir = Path(os.environ.get(WORKBOOK_DIR_ENV,
                                       str(_DEFAULT_WORKBOOK_DIR)))
    books = tuple(workbook_dir / name for name in WORKBOOK_NAMES)
    missing = [b.name for b in books if not b.exists()]
    if missing:
        pytest.skip(
            f"金标工作簿不在本机（缺 {missing}）——金标正文不可恢复，"
            "回放类用例诚实 skip（非红非绿）"
        )
    materials = {m["material_id"]: m for m in manifest["materials"]}
    for book in books:
        digest = _file_sha256(book)
        assert digest in materials, (
            f"工作簿 {book.name} SHA-256={digest} 不在 manifest materials "
            "钉死清单——金标材料被替换（INV-1 违规，红）"
        )
        assert book.stat().st_size == materials[digest]["byte_size"]

    from news_flash_dedup.gold import load_workbooks   # 延迟导入：闸门用例轻依赖

    catalog = load_workbooks(books)
    rows = {row.row_key: row for row in catalog.rows}
    clear = _build_replay_plan(manifest, rows)
    assert len(clear) == DEV_FROZEN["pairs"], (
        f"dev 回放计划 {len(clear)} != 319（split 过滤口径漂移）"
    )
    return SimpleNamespace(manifest=manifest, rows=rows, clear=clear, books=books)


def _identity_facts(row_key: str, row) -> list[dict]:
    """恒等投影 Fact（21:4x 主窗口裁定；与 P23 UAT 同文复写——模板与 P05 无关）。

    零语义主张：subject=全文原文（evidence 偏移真实可核）、predicate 固定常量、
    time/key_object/numerics 全 missing/空——不抽取、不推断、不编造任何语义；
    仅使 decide 的"报告须含 ≥1 Fact"结构性前置得到满足。非 P14 事实模型。
    """
    text = row.raw_text
    record_id = _record_id(row_key)
    span = {"record_id": record_id, "field": "text",
            "quote": text, "start": 0, "end": len(text)}
    present = {"status": "present", "raw_value": text, "evidence": [span]}
    missing = {"status": "missing", "raw_value": None, "evidence": []}
    return [{
        "fact_id": f"idem-{row.raw_hash[:16]}",
        "evidence": [span],
        "fact_type": {"status": "present",
                      "raw_value": "verbatim_identity_projection",
                      "evidence": [span]},
        "subject": present,
        "event_state": {
            "predicate": {"status": "present",
                          "raw_value": "verbatim_identity_projection",
                          "evidence": [span]},
            "polarity": {"status": "present",
                         "raw_value": "verbatim_identity_projection",
                         "evidence": [span]},
            "modality": missing,
            "attribution": missing,
        },
        "time": {"expression": missing, "stage": missing, "anchor": missing},
        "key_object": missing,
        "numerics": [],
    }]


def _ctx_mapping(row_key: str, row, seq: int, scope_id: str,
                 business_date: str) -> dict:
    """decide/commit 输入映射（恒等投影供给；P23 _ctx_mapping 同文）。"""
    return {
        "record_id": _record_id(row_key),
        "item_id": _item_id(row_key),
        "text": row.raw_text,
        "raw_hash": row.raw_hash,
        "scope_id": scope_id,
        "business_date": business_date,
        "arrival_seq": seq,
        "facts": _identity_facts(row_key, row),
    }


# ---------- API key 现读（resolve_api_key 同逻辑内联：DEDUP_EMBEDDING_KEY_FILE
# 指向 workspace-dedup/.env.dedup 的 QWEN_API_KEY 行；禁抄值、不注入环境） ----------

def _llm_api_key() -> str:
    key_file = (os.environ.get(KEY_FILE_ENV) or "").strip()
    assert key_file, f"{KEY_FILE_ENV} 未设置（任务书：指向 workspace-dedup/.env.dedup）"
    text = Path(key_file).read_text(encoding="utf-8")
    value = None
    for line in text.splitlines():
        if line.startswith("QWEN_API_KEY=") and not line.startswith("#"):
            value = line.split("=", 1)[1].strip()
            break
    if value is None:
        candidate = text.strip()
        value = candidate if candidate and "\n" not in candidate else None
    assert value, f"{key_file} 未找到 QWEN_API_KEY 行"
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        value = value[1:-1]
    return value


# ---------- 预算闸 + 计量（D2 小闸同型：落盘/日翻转/fail-closed/实际 usage 扣账） ----------

class _C5BudgetError(RuntimeError):
    """预算闸超闸 / 计数器落盘失败——fail-closed，不静默降级（R14 同纪律）。

    注：本类在装配时动态注册为 llm_residual.LlmResidualError 子类语义——
    经 _judge_order 的 LlmResidualError 直通道传播（不进瞬时错误重试环），
    由本 harness 按 budget_exhausted 旗标再分类为 budget_blocked 单列。
    """


class _BudgetedJudgeCall:
    """残判 LLM 调用包装（SyncResidualJudge call_fn 注入点，src 零改动）。

    - 预算闸：used >= budget 于调用前拒（D2 _check_budget_open 同语义；
      实际扣账=响应 usage.total_tokens，落闸前最后一次调用可微越——
      D2 同口径）；业务日 UTC+8 翻转重置；计数器 tmp+replace 原子落盘；
      不可读/落盘失败 fail-closed（此后一切活调拒，alarms 入账）。
    - 计量：逐次（含重试/失败）JSONL 落 c5-oos-api-<run>.jsonl——
      pair_id/order 经 harness 同步单线程上下文注记（回放为同步串行）。
    - 活调帽（C5_OOS_MAX_LIVE_CALLS）：烟测/纯回放验证腿用；到帽即拒，
      harness 按 live_cap_blocked 单列（与 budget_blocked 区分）。
    """

    def __init__(self, *, api_key_fn, run_id: str, metering_path: Path,
                 budget_path: Path, daily_budget: int,
                 max_live_calls: int | None, base_url: str, timeout_s: float):
        self._api_key_fn = api_key_fn
        self._api_key = None
        self.run_id = run_id
        self._metering_path = metering_path
        self._budget_path = budget_path
        self._daily_budget = daily_budget
        self._max_live_calls = max_live_calls
        self._base_url = base_url
        self._timeout_s = timeout_s
        self._lock = threading.Lock()
        self._seq = 0
        self.live_calls = 0
        self.budget_exhausted = False
        self.cap_exhausted = False
        self.alarms: list[dict] = []
        self._broken = False
        self._budget_day, self._budget_used = self._load_counter()
        # 同步上下文（harness 逐对判定时注记；串行无竞态）
        self.context: dict = {"pair_id": None, "order": None}

    # ---- 预算计数器（R14：落盘 + 启动加载 + 日翻转 + 落盘失败 fail-closed） ----

    def _business_day(self) -> str:
        return datetime.now(timezone.utc).astimezone(
            _BUSINESS_TZ).date().isoformat()

    def _load_counter(self) -> tuple[str, int]:
        today = self._business_day()
        if not self._budget_path.exists():
            return today, 0
        try:
            body = json.loads(self._budget_path.read_text(encoding="utf-8"))
            day = body.get("business_date")
            used = body.get("used_tokens")
        except (OSError, ValueError):
            self._broken = True
            self.alarms.append({"type": "budget_counter_unreadable"})
            return today, 0
        if day != today or type(used) is not int or used < 0:
            return today, 0
        return day, used

    def _persist_counter(self) -> None:
        body = {"business_date": self._budget_day,
                "used_tokens": self._budget_used,
                "calls": self.live_calls,
                "updated_at": datetime.now(timezone.utc).isoformat()}
        temp = self._budget_path.with_suffix(".json.tmp")
        self._budget_path.parent.mkdir(parents=True, exist_ok=True)
        temp.write_text(json.dumps(body, ensure_ascii=False, indent=2),
                        encoding="utf-8")
        os.replace(temp, self._budget_path)

    def _charge(self, tokens: int) -> None:
        with self._lock:
            self._budget_used += tokens
            try:
                self._persist_counter()
            except OSError:
                self._broken = True
                self.alarms.append({"type": "budget_counter_persist_failed"})
                raise _C5BudgetError(
                    "budget counter persistence failed (fail-closed)") from None

    def _check_open(self) -> None:
        with self._lock:
            if self._broken:
                raise _C5BudgetError(
                    "budget counter broken (fail-closed, R14 同纪律)")
            today = self._business_day()
            if today != self._budget_day:
                self._budget_day = today
                self._budget_used = 0
                try:
                    self._persist_counter()
                except OSError:
                    self._broken = True
                    raise _C5BudgetError(
                        "budget counter persistence failed (fail-closed)")
            if self._max_live_calls is not None and (
                    self.live_calls >= self._max_live_calls):
                self.cap_exhausted = True
                raise _C5BudgetError(
                    f"live-call cap reached: {self.live_calls} >= "
                    f"{self._max_live_calls}（烟测/纯回放腿活调帽）")
            if self._budget_used >= self._daily_budget:
                self.budget_exhausted = True
                self.alarms.append({
                    "type": "budget_exceeded", "used_tokens": self._budget_used,
                    "budget": self._daily_budget})
                raise _C5BudgetError(
                    f"daily token budget exceeded: {self._budget_used} >= "
                    f"{self._daily_budget} (fail-closed, D2 §6.3 同型)")

    # ---- 计量 ----

    def _meter(self, record: dict) -> None:
        with self._lock:
            self._seq += 1
            line = {"run_id": self.run_id, "seq": self._seq,
                    "ts": datetime.now(timezone.utc).isoformat(),
                    **record}
            self._metering_path.parent.mkdir(parents=True, exist_ok=True)
            with self._metering_path.open("a", encoding="utf-8") as sink:
                sink.write(json.dumps(line, ensure_ascii=False) + "\n")

    # ---- call_fn（SyncResidualJudge 注入签名：model/system/user → (content, latency)） ----

    def __call__(self, *, model: str, system: str, user: str):
        self._check_open()                      # 闸前拒（fail-closed）
        if self._api_key is None:
            self._api_key = self._api_key_fn()
        body = json.dumps({
            "model": model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "response_format": {"type": "json_object"},
            "temperature": 0,
            "seed": 42,
        }).encode("utf-8")
        req = urllib.request.Request(
            self._base_url.rstrip("/") + "/chat/completions", data=body,
            headers={"Authorization": f"Bearer {self._api_key}",
                     "Content-Type": "application/json"})
        t0 = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=self._timeout_s) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            self.live_calls += 1               # 失败尝试也是活调（无 usage 不扣账）
            self._meter({**self.context, "ok": False,
                         "error_type": type(exc).__name__,
                         "error": str(exc)[:200], "tokens": 0})
            raise
        latency = time.perf_counter() - t0
        self.live_calls += 1
        try:
            content = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            self._meter({**self.context, "ok": False,
                         "error_type": "BadResponseShape",
                         "error": str(payload)[:200], "tokens": 0})
            raise RuntimeError(
                f"chat 响应形态异常：{str(payload)[:200]!r}") from exc
        usage = payload.get("usage") if isinstance(payload, dict) else None
        total = usage.get("total_tokens") if isinstance(usage, dict) else None
        prompt_t = usage.get("prompt_tokens") if isinstance(usage, dict) else None
        completion_t = (usage.get("completion_tokens")
                        if isinstance(usage, dict) else None)
        estimated = False
        if type(total) is not int or total < 0:
            # 缺 usage → UTF-8 字节数保守上界（1 token ≥ 1 byte 恒成立；R14 同纪律）
            total = len(system.encode("utf-8")) + len(user.encode("utf-8")) \
                + len(content.encode("utf-8"))
            estimated = True
        self._charge(total)
        self._meter({**self.context, "ok": True, "cache_hit": False,
                     "prompt_tokens": prompt_t, "completion_tokens": completion_t,
                     "tokens": total, "estimated": estimated,
                     "latency_s": round(latency, 3),
                     "budget_used_after": self._budget_used})
        return content, latency

    def snapshot(self) -> dict:
        with self._lock:
            return {"budget_business_date": self._budget_day,
                    "budget_used_tokens": self._budget_used,
                    "daily_token_budget": self._daily_budget,
                    "live_calls": self.live_calls,
                    "max_live_calls": self._max_live_calls,
                    "budget_exhausted": self.budget_exhausted,
                    "cap_exhausted": self.cap_exhausted,
                    "alarms": list(self.alarms)}


# ---------- 残判层装配（窗口T 同族；cache=C5 自目录播种副本，D32 原位只读） ----------

def _residual_config(run_id: str, metering_path: Path):
    """残判层装配。开关必须显式 =llm_bidi_v1（族闸之一）；其他值 fail-closed。"""
    raw = os.environ.get(RESIDUAL_SWITCH_ENV, "")
    if raw != RESIDUAL_ENABLED_VALUE:
        pytest.fail(f"{RESIDUAL_SWITCH_ENV}={raw!r} 未知或缺省（fail-closed；"
                    f"C5 口径要求显式 {RESIDUAL_ENABLED_VALUE!r}）")
    from news_flash_dedup.decide import llm_residual

    max_live_raw = os.environ.get(MAX_LIVE_CALLS_ENV, "").strip()
    max_live = int(max_live_raw) if max_live_raw else None
    cfg = llm_residual.ResidualJudgeConfig(
        model=MODEL,
        cache_dir=str(CACHE_DIR),
        mv_mode=llm_residual.MV_AUDIT)
    call = _BudgetedJudgeCall(
        api_key_fn=_llm_api_key, run_id=run_id, metering_path=metering_path,
        budget_path=BUDGET_PATH, daily_budget=DAILY_TOKEN_BUDGET,
        max_live_calls=max_live, base_url=llm_residual.DEFAULT_BASE_URL,
        timeout_s=llm_residual.TIMEOUT_S)
    # 预算错误经 LlmResidualError 直通道传播（不进重试环）：动态子类化
    global _C5BudgetError
    if not issubclass(_C5BudgetError, llm_residual.LlmResidualError):
        class _C5BudgetError(llm_residual.LlmResidualError):  # noqa: F811
            pass
    judge = llm_residual.SyncResidualJudge(cfg, call_fn=call)
    return SimpleNamespace(config=cfg, judge=judge, call=call, switch=raw)


# ---------- 回放执行 fixture（模块级一次跑完；指标/逐对 JSON 落盘一次） ----------

@pytest.fixture(scope="module")
def replay_run(gold_assets):
    """固定候选路径 (a) dev 319 对全链回放 + 残判 LLM（预算闸/计量/播种缓存）。

    与 P23 replay_run 同构；差异（全部已登记）：
    - 金标=v2 dev；record/item id 派生 namespace 同 P23（行派生身份）；
    - 每对独立 FakeCommitStore+watermark=1（回放适配注记 #2，模块 docstring；
      决策输入全在 ctx，store 纯落库槽——口径零削减）；
    - gold 边界对同链回放（五字段合同覆盖）但**不入二元聚合**（hold 同口径）；
    - 残判 universe=全部回放对的规则边界对（含 gold 边界对——其二元指标
      不消费残判结论，仅入边界分布/归因段）；
    - budget_blocked/live_cap_blocked 单列，使覆盖断言红（不冒充完成）。
    """
    from news_flash_dedup.commit.coordinator import CommitContext, commit_one
    from news_flash_dedup.commit.fake_store import FakeCommitStore
    from news_flash_dedup.decide import llm_residual as _lr

    run_id = "c5oos" + datetime.now(timezone.utc).strftime("%H%M%S%f")  # W-R3b 秒级→微秒
    metering_path = REPO_LOG_TEMP / f"c5-oos-api-{run_id}.jsonl"
    residual = _residual_config(run_id, metering_path)

    items: list[dict] = []          # 二元金标对（重复/不重复）
    boundary_items: list[dict] = [] # gold 边界对（只报分布）
    failures: list[dict] = []

    def _drive(entry) -> dict:
        rows = gold_assets.rows
        history = _ctx_mapping(entry.history_key, rows[entry.history_key],
                               entry.history_seq, entry.scope_id, entry.business_date)
        current = _ctx_mapping(entry.current_key, rows[entry.current_key],
                               entry.current_seq, entry.scope_id, entry.business_date)
        # 每对独立 store + watermark=1（回放适配注记 #2，见模块 docstring）：
        # 决策输入全在 ctx，store 纯落库槽；S_MULTI 共享行跨对重提不在
        # 固定候选轨口径内（P05 端点互斥从未触发 M-3 拒收）。
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
            coverage_complete=True,   # 显式占位（固定候选路径；登记于指标 JSON）
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
        # 残判层串接（窗口T 同族）：规则链判边界的对 → 双向 LLM 判+机验；
        # 残判结论不回写五字段 public。预算/帽阻断单列（不冒充存疑/签发）。
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
                        # 单腿完成+另腿被闸拒：如实再分类（hc 缓存已落盘可续跑）
                        item["residual"]["cell"] = (
                            "budget_blocked" if call.budget_exhausted
                            else "live_cap_blocked")
                        item["residual"]["signed"] = False
                        item["residual"]["note"] = (
                            "判定途中闸拒——单腿结果保留，整体按阻断单列")
                    if item["residual"].get("cell") == "not_duplicate":
                        item["residual"]["auto_not_duplicate"] = True
                except Exception as exc:      # 残判技术失败单列（INV 式）
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
        except Exception as exc:                       # 失败不冒充边界（INV-4）
            failures.append({
                "pair_id": entry.pair_id,
                "gold_label": entry.gold_label,
                "stratum": entry.stratum,
                "error_type": type(exc).__name__,
                "error": str(exc)[:200],
            })

    # ---- 聚合（冻结聚合器复用零改动）：基线（规则链 public）+ 合并（+残判） ----
    def _aggregate(item_list):
        agg = MetricsAggregator()
        for item in item_list:
            agg.add(item["replay_result"])
        return agg

    agg = _aggregate(items)
    pair_m = dict(agg.pair_metrics())
    strict_m = dict(agg.strict_metrics())

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

    # ---- CP95 下界（冻结公式复用；precision 与 recall 双指标） ----
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

    # ---- 分层（stratum × 基线/合并；role_ambiguous 副层单列） ----
    def _stratified(item_list):
        out = {}
        for item in item_list:
            st = item["stratum"]
            out.setdefault(st, []).append(item)
        section = {}
        for st in sorted(out):
            sub = out[st]
            base_agg = _aggregate(sub)
            m_agg = MetricsAggregator()
            for item in sub:
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
            bp = dict(base_agg.pair_metrics())
            mp = dict(m_agg.pair_metrics())
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

    def _label_dist(item_list) -> dict:
        dist: dict[str, int] = {}
        for item in item_list:
            dist[item["gold_label"]] = dist.get(item["gold_label"], 0) + 1
        return dist

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

    # ---- 金标边界对分布（P23 hold 集同口径：只报分布不入二元指标） ----
    boundary_dist: dict[str, int] = {}
    boundary_residual_cells: dict[str, int] = {}
    for i in boundary_items:
        boundary_dist[i["public"]["decision"]] = (
            boundary_dist.get(i["public"]["decision"], 0) + 1)
        cell = (i.get("residual") or {}).get("cell")
        if cell:
            boundary_residual_cells[cell] = (
                boundary_residual_cells.get(cell, 0) + 1)

    # ---- 残判段（窗口T 同族 + C5 阻断单列） ----
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
                  "判定逻辑=D32 终版；call_fn=C5 预算闸计量包装（src 零改动）"),
        "mv_mode": residual.config.mv_mode,
        "model": residual.config.model,
        "prompt_version": _lr.JUDGE_PROMPT_VERSION,
        "prompt_sha256": _lr.PROMPT_SHA256,
        "cache_dir": str(residual.config.cache_dir),
        "cache_note": ("C5 自目录（D32 原位只读）；36 dev 对×2 顺序 D32 条目"
                       "副本播种（c5-oos-cache-seed-manifest.json 逐件 SHA256 "
                       "登记）；新对活调结果写本目录"),
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
        "residual_payload_hash": _digest(json.dumps(
            per_pair_residual, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"))),
    }

    # ---- 逐对归因（翻对全登记；合并口径 predicted） ----
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
    attribution_path = REPO_LOG_TEMP / f"c5-oos-pairs-{run_id}.jsonl"
    with attribution_path.open("w", encoding="utf-8") as sink:
        for record in sorted(attribution, key=lambda r: r["pair_id"]):
            sink.write(json.dumps(record, ensure_ascii=False) + "\n")
    flips = [r for r in attribution if r["flip"]]

    # ---- 锚对读（判读记录，不调阈） ----
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
        "c5_dev": {**merged_pair, "recall": merged_recall},
        "recall_delta": merged_recall - ANCHOR_RECALL,
        "bands": {"held": ">=0.96 泛化守住",
                  "expected": "0.90–0.96 预期带内",
                  "anomaly": "<0.90 异常需根因"},
        "verdict": verdict,
        "note": ("判读不调阈（INV-6 同纪律）；dev 非总体代表性抽样"
                 "（S_UPG 不重复对 0.30 富集，manifest non_representative_notice "
                 "在案）——点估计为 dev 集口径，非总体精度/召回估计"),
    }

    def _stratum_dist(item_list) -> dict:
        dist: dict[str, int] = {}
        for item in item_list:
            dist[item["stratum"]] = dist.get(item["stratum"], 0) + 1
        return dist

    metrics = {
        "run_id": run_id,
        "task": "C5 扩样外样本初测（过拟合检验首轮）dev split",
        "dataset_version": EXPECTED_DATASET_VERSION,
        "manifest_file_sha256": EXPECTED_FILE_SHA256,
        "candidate_path": "fixed_candidate_(a)_gold_supplied_no_recall",
        "fact_supply": ("mechanical_identity_projection_no_semantics（21:4x "
                        "主窗口裁定恒等投影脚手架，与 P23 UAT 同文；零语义主张、"
                        "非 P14 事实模型）"),
        "norm_dictionary": "none",
        "arrival_order": ("synthetic_no_plan_v2：seq=material_rank×1e6+"
                          "excel_row×4+side_rank（v2 无 synthetic_plan 的诚实 "
                          "合成，不冒充官方顺序）；scope=synthetic:<current "
                          "material_id[:16]>；business_date=2000-01-01"),
        "coverage_complete": "explicit_true_placeholder_fixed_candidate",
        "replay_adapter_note_2": ("每对独立 FakeCommitStore+watermark=1"
            "（S_MULTI 共享行跨对重提的 M-3 幂等拒收规避；决策输入全在 ctx，"
            "store 纯落库槽——口径零削减，烟测红证据 c5-oos-run-smoke.txt）"),
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
            "note": ("strata 副层 role_ambiguous（R183 口径⑪：数值角色歧义——"
                     "变动量 vs 水平值同数异口径且语境可双解）；C1 仲裁登记对，"
                     "dev 内 2 对均 S_CROSS/金标边界"),
            "items": role_ambiguous,
        },
        "gold_boundary_set": {
            "distribution": boundary_dist,
            "residual_cells": boundary_residual_cells,
            "note": ("gold 边界 34 对只报分布不入二元指标（P23 hold 集同口径："
                     "无二元真值可判正误）；系统判边界且金标边界=口径一致"
                     "（双向疑难收敛信号），系统签重复/不重复=方向性分歧信号"),
        },
        "flips": {
            "count": len(flips),
            "note": ("翻对全登记（合并口径 predicted vs gold）：fn=金标重复 "
                     "系统未签（含 auto 不重复章转可见 fn 与余疑难）；fp=金标 "
                     "不重复系统签重复（硬闸对象）"),
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

    out_path = REPO_LOG_TEMP / f"c5-oos-replay-{run_id}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(metrics, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    print(f"\n[C5-OOS] metrics json: {out_path}")
    print(f"[C5-OOS] attribution jsonl: {attribution_path}")
    print(f"[C5-OOS] api metering jsonl: {metering_path}")
    print(f"[C5-OOS] pair={json.dumps(pair_m, ensure_ascii=False, sort_keys=True)}")
    print(f"[C5-OOS] strict={json.dumps(strict_m, ensure_ascii=False, sort_keys=True)}")
    print(f"[C5-OOS] merged pair="
          f"{json.dumps(merged_pair, ensure_ascii=False, sort_keys=True)}")
    print(f"[C5-OOS] merged strict="
          f"{json.dumps(merged_strict, ensure_ascii=False, sort_keys=True)}")
    print(f"[C5-OOS] cp95={json.dumps(cp, ensure_ascii=False, sort_keys=True)}")
    print(f"[C5-OOS] anchor verdict={verdict} "
          f"(recall={merged_recall:.6f} vs anchor {ANCHOR_RECALL:.6f})")
    print(f"[C5-OOS] residual judged={len(judged)} blocked={len(blocked)} "
          f"ledger={residual_section['ledger']}")
    print(f"[C5-OOS] budget={residual_section['budget']}")
    print(f"[C5-OOS] flips={len(flips)} failures={len(failures)}")
    return SimpleNamespace(
        run_id=run_id, items=items, boundary_items=boundary_items,
        failures=failures, blocked=blocked, judged=judged,
        pair=pair_m, strict=strict_m, merged_pair=merged_pair,
        merged_strict=merged_strict, cp=cp, anchor_read=anchor_read,
        stratified=stratified, metrics=metrics, metrics_path=out_path,
        attribution_path=attribution_path, residual=residual,
    )


# ---------- 场景 1 · 闸门（harness 级恒可跑） ----------

def test_c5_oos_harness_gate_requires_confirm_flag():
    """场景 1（族闸照 winw2fz 档）：DEPLOY_ENV=test + P23_CONFIRM_UAT=1 +
    无 PROD_* 三要件缺一不可。"""
    assert _gate_open({DEPLOY_ENV_VAR: "test", P23_UAT_ENV: "1"}) is True
    assert _gate_open({P23_UAT_ENV: "1"}) is False                     # 缺 DEPLOY_ENV
    assert _gate_open({DEPLOY_ENV_VAR: "test"}) is False               # 缺 CONFIRM
    assert _gate_open({DEPLOY_ENV_VAR: "prod", P23_UAT_ENV: "1"}) is False
    assert _gate_open({DEPLOY_ENV_VAR: "test", P23_UAT_ENV: "0"}) is False
    assert _gate_open({DEPLOY_ENV_VAR: "test", P23_UAT_ENV: "1",
                       "PROD_ES_HOST": "x"}) is False
    assert _gate_open({DEPLOY_ENV_VAR: "test", P23_UAT_ENV: "1",
                       "PROD_MILVUS_ENDPOINT": ""}) is False           # 空值也拒


# ---------- 场景 2 · v2 manifest 钉死（INV-1；不符=红不 skip） ----------

def test_c5_oos_manifest_integrity_hash_pinned():
    """INV-1：文件 SHA256（任务书钉值）+ dataset_version 复算 + 冻结计数 +
    materials 钉死项 + 无正文纪律（pairs 键集不含 text/raw_text/quote）。"""
    manifest = _load_manifest()
    _verify_manifest_pinned(manifest)
    forbidden = {"text", "raw_text", "raw_id", "quote"}
    for pair in manifest["pairs"]:
        assert not (set(pair) & forbidden), (
            f"金标对 {pair['pair_id'][:16]!r} 含正文键——违反无正文纪律"
        )


# ---------- 场景 3 · dev 资产可恢复 + 冻结分布钉守 ----------

def test_c5_oos_dev_assets_recoverable(gold_assets):
    """dev 319 对端点正文全恢复；标签/stratum/role_ambiguous 分布钉守；
    到达序 history<current（INV-2 合成序同守）。"""
    clear = gold_assets.clear
    labels: dict[str, int] = {}
    strata: dict[str, int] = {}
    role_amb = 0
    for entry in clear:
        labels[entry.gold_label] = labels.get(entry.gold_label, 0) + 1
        strata[entry.stratum] = strata.get(entry.stratum, 0) + 1
        if ROLE_AMBIGUOUS_TAG in entry.strata:
            role_amb += 1
        assert entry.history_seq < entry.current_seq, (
            f"金标对 {entry.pair_id[:16]!r} history.arrival_seq >= current"
        )
    assert labels["重复"] == DEV_FROZEN["重复"]
    assert labels["不重复"] == DEV_FROZEN["不重复"]
    assert labels["边界case/疑难case"] == DEV_FROZEN["边界case/疑难case"]
    for st in ("S_MULTI", "S_UPG", "S_CROSS", "S_HOLD"):
        assert strata.get(st) == DEV_FROZEN[st], (
            f"dev stratum {st}={strata.get(st)!r} != {DEV_FROZEN[st]}"
        )
    assert role_amb == DEV_FROZEN["role_ambiguous"]


# ---------- 场景 4 · 输出合同：全量逐条五字段 + 闭合词表（非抽样） ----------

def test_c5_oos_output_contract_five_fields_closed_vocab_full_replay(replay_run):
    """场景 4（冻结合同①②同族）：319 对全量逐条断言（含 gold 边界对）。

    每条回放产出：① public dict 恰五字段；② decision ∈ 闭合词表；
    ③ duplicate_ids 与 decision 严格耦合；④ item_id/text/reason 非空；
    ⑤ commit 层 payload_hash == sha256(sort_keys 五字段 JSON)。
    覆盖完整性：成功 + 失败 == 319（失败单列，不冒充合同产出）；
    防空转闸：回放全灭即红（不空转冒充验证）。
    """
    all_items = replay_run.items + replay_run.boundary_items
    assert all_items, "回放成功数为 0——合同验证退化空转（全灭即红）"
    assert len(all_items) + len(replay_run.failures) == 319, (
        f"回放覆盖不完整：成功 {len(all_items)} + 失败 "
        f"{len(replay_run.failures)} != 319 dev 对"
    )
    for item in all_items:
        pub = item["public"]
        assert set(pub.keys()) == PUBLIC_KEYS, (
            f"pair {item['pair_id'][:16]!r} 输出字段集 != 恰五字段"
        )
        assert pub["decision"] in DECISION_VOCAB
        assert isinstance(pub["duplicate_ids"], list)
        assert (pub["decision"] == "重复") == bool(pub["duplicate_ids"])
        assert isinstance(pub["item_id"], str) and pub["item_id"]
        assert isinstance(pub["text"], str) and pub["text"]
        assert isinstance(pub["reason"], str) and pub["reason"]
        expected_hash = hashlib.sha256(
            json.dumps(pub, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        assert item["payload_hash"] == expected_hash, (
            f"pair {item['pair_id'][:16]!r} commit 层 payload_hash 不符"
        )


# ---------- 场景 5 · 指标正确性：CP 对拍 scipy + P-set 按 OUTPUT 独立复算 ----------

def test_c5_oos_metrics_correctness_cp_scipy_and_output_p_set(replay_run):
    """场景 5（冻结合同③④同族）：CP 下界与 scipy 直调逐字节对拍；
    基线 strict 与合并 strict 的 P/S/q_plus 独立复算（OUTPUT 口径）。"""
    from scipy.stats import beta

    for x, n in ((319, 319), (272, 272), (9, 10), (0, 319), (0, 0)):
        expected = 0.0 if (n <= 0 or x <= 0) else float(beta.ppf(0.05, x, n - x + 1))
        assert _safe_lower_bound(x, n) == expected, (
            f"CP 下界 ({x},{n}) 与 scipy 直调不符——冻结公式被改动"
        )
    assert 0.99 < _safe_lower_bound(319, 319) < 1.0   # x=n 无白送约定

    for results, strict in (
            ([i["replay_result"] for i in replay_run.items], replay_run.strict),
    ):
        p_recount = sum(1 for r in results if r.predicted_decision == "重复")
        q_plus_recount = sum(1 for r in results if r.expected_decision == "重复")
        s_recount = sum(
            1 for r in results
            if r.predicted_decision == "重复"
            and set(r.predicted_duplicate_ids)
            and set(r.predicted_duplicate_ids) <= set(r.expected_duplicate_ids)
        )
        assert strict["P"] == p_recount, "P-set 未按 OUTPUT 口径"
        assert strict["q_plus"] == q_plus_recount
        assert strict["S"] == s_recount
        if strict["P"] > 0:
            assert strict["precision_strict"] == strict["S"] / strict["P"]
            assert strict["clopper_pearson_lower_95"] == float(
                beta.ppf(0.05, strict["S"], strict["P"] - strict["S"] + 1))


# ---------- 场景 6 · 端到端：覆盖完整 + fp=0 硬闸 + 锚对读判读落盘 ----------

def test_c5_oos_e2e_coverage_fp_hard_gate(replay_run):
    """场景 6（任务书硬闸 + 诚实覆盖）。

    ① 覆盖完整：回放失败=0 且 残判阻断（budget/live_cap）=0——部分覆盖
       即红（不空转冒充完成；预算闸阻断=次日同令续跑，残判缓存落盘即状态）；
    ② fp=0 硬闸：基线 pair / 合并 pair / 残判签发 三口径任一 fp → 红，
       立即停报主窗口（任务书：任何 fp 立即停报）；
    ③ recall 分级判读只落盘不断言（≥0.96 守住 / 0.90–0.96 带内 /
       <0.90 异常需根因——判读不调阈，INV-6 同纪律）。
    """
    assert replay_run.metrics_path.exists(), "指标 JSON 未落盘"
    blocked = replay_run.blocked
    assert not replay_run.failures, (
        f"回放失败 {len(replay_run.failures)} 对——"
        f"{replay_run.failures[:3]}（失败不冒充边界，INV-4）"
    )
    assert not blocked, (
        f"残判阻断 {len(blocked)} 对（预算闸/活调帽）——部分覆盖不宣称完成；"
        f"次日同令续跑（残判缓存落盘即续跑状态，已判对零 API）；"
        f"阻断对={[i['pair_id'][:16] for i in blocked[:5]]}…"
    )
    pair = replay_run.pair
    merged = replay_run.merged_pair
    residual_fp = replay_run.metrics["residual_judge"]["residual_fp"]
    assert pair["fp"] == 0, (
        f"基线 fp={pair['fp']} != 0——fp=0 硬闸被破，立即停报主窗口"
    )
    assert merged["fp"] == 0, (
        f"合并 fp={merged['fp']} != 0——fp=0 硬闸被破，立即停报主窗口"
    )
    assert residual_fp == 0, (
        f"残判签发 fp={residual_fp} != 0——fp=0 硬闸被破，立即停报主窗口"
    )
    print(f"[C5-OOS] 锚对读 verdict={replay_run.anchor_read['verdict']} "
          f"merged recall={replay_run.merged_pair['recall']:.6f} "
          f"delta={replay_run.anchor_read['recall_delta']:+.6f}")


# ---------- 场景 7 · 残判层：五字段输出契约不动 ----------

def test_c5_oos_residual_public_contract_untouched(replay_run):
    """场景 7（窗口T 6c 同族）：残判结论不回写 public decision——
    残判签发对/中章对的 public.decision 仍为 边界case/疑难case。"""
    signed_n = 0
    auto_n = 0
    for item in replay_run.items + replay_run.boundary_items:
        res = item.get("residual")
        if res is None:
            continue
        if res.get("signed"):
            signed_n += 1
            assert item["public"]["decision"] == "边界case/疑难case", (
                f"pair {item['pair_id'][:16]!r} 残判签发回写了 public decision"
            )
        if res.get("cell") == "not_duplicate":
            auto_n += 1
            assert res.get("auto_not_duplicate") is True
            assert item["public"]["decision"] == "边界case/疑难case"
    assert signed_n == replay_run.metrics["residual_judge"]["signed"]
    assert auto_n == (replay_run.metrics["residual_judge"]
                      ["merged_three_way"]["auto_not_duplicate"]["total"])


# ---------- 场景 8 · 分层/归因/计量落盘 + 内部一致性 ----------

def test_c5_oos_stratified_attribution_metering_written(replay_run):
    """场景 8：stratum 分层段全四层、role_ambiguous 单列、逐对归因 JSONL、
    API 计量 JSONL 落盘且内部一致（归因 319 行、class 分布与合并口径自洽）。"""
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
    # 合并口径自洽（闸阻断对在覆盖完整腿不存在；部分腿如实不计入）
    blocked_n = len(replay_run.blocked)
    if blocked_n == 0:
        assert cls.get("tp", 0) == replay_run.merged_pair["tp"]
        assert cls.get("fp", 0) == replay_run.merged_pair["fp"]
        assert cls.get("fn", 0) == replay_run.merged_pair["fn"]
        assert cls.get("tn", 0) == replay_run.merged_pair["tn"]
    assert cls.get("gold_boundary", 0) == DEV_FROZEN["边界case/疑难case"]
    # 预算闸台账：计数器落盘文件在场且 used 与 metering 对拍（有活调时）
    budget = metrics["residual_judge"]["budget"]
    assert budget["daily_token_budget"] == DAILY_TOKEN_BUDGET
    if budget["live_calls"] > 0:
        assert BUDGET_PATH.exists(), "预算计数器未落盘（R14 违例）"
        counter = json.loads(BUDGET_PATH.read_text(encoding="utf-8"))
        assert counter["used_tokens"] == budget["budget_used_tokens"]
        metering = replay_run.metrics_path.parent / (
            f"c5-oos-api-{replay_run.run_id}.jsonl")
        assert metering.exists(), "API 计量 JSONL 未落盘"
