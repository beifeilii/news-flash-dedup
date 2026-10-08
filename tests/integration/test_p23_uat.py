"""P23 真金标回放 UAT（05:31 批复 + 修订①② + 13:20 案一 + 公共标尺 · T5 末站）。

起草本稿的纪律背景：
- 本稿由 T5 子窗口起草；src 产品代码零改动（决策日志 D7 同例：src 由主窗口亲笔）。
  固定候选路径 (a) 所需全部接口已在册且单元层封讫（decide/service.decide_for_task、
  commit/coordinator.commit_one、commit/fake_store.FakeCommitStore、
  metrics/p23_metrics、gold.load_workbooks）——**本稿无需任何 src 接线**，
  接线建议书全文见 `log/temp/p23-uat-wiring-proposal.md`。
- 单元层已封（tests/unit/test_p23_implementation.py，14:55 重新封层）：fake 回放 +
  指标聚合 + CP 公式钉死。**真金标（P05 manifest 393 对 + 仓外工作簿正文）经
  commit_one 全链回放在本稿是首次**。
- 路径口径（05:31 修订②，D5 追认）：固定候选集回放 (a)——候选由金标直接给定，
  **不走召回、不连 ES/Milvus**（设计 §8：manifest 文件读取；dry-run；不触发
  P13-P19 任何真写；192 保留索引全程不涉）。真召回路径 (b) 属 T5 后可选扩展站
  （前置：真 Embedding 准入评估），本稿不做。
- 冻结合同（05:31 批复定稿，一字不可动）：
  ① 输出恰五字段 item_id/text/decision/duplicate_ids/reason（07 §5/11 §1）；
  ② decision 词表闭合 {重复, 不重复, 边界case/疑难case}；
  ③ P-set 按 OUTPUT 口径（12 §6.4 L291 + 13:20 案一：predicted==重复 全集入 P，
     空 D_i 留 P 不算 S/A）；
  ④ Clopper-Pearson 单侧 95% 下界 = scipy.stats.beta.ppf(0.05, x, n-x+1)，
     无 x=n 白送约定（x=0 → 0 保留）；
  ⑤ 闸门（设计 §5.2 / 12 §6.6）：precision_strict（=S/P）≥ 99.5% 且
     CP 下界 ≥ 99%；未达标 → 红（INV-6：CP 未闭合不宣称达标；
     任务纪律：不许调阈值凑绿）。
- 等价通路状态（22:2x B1 实装，主窗口；红绿证据 b1-unit-run-2220.txt /
  p23-replay-b1-run-2222.txt / 指标 JSON uat183345）：**文本证书路径已开通**
  ——p15_integration 真调 compare_time（时间双侧合法缺失 → compatible，
  09 §11.3"允许合法时间缺失"）+ 经 P09 certify_text_equality 颁发
  EXACT/LOSSLESS_TEXT_MATCH 证书（equivalence_ready=True + text_proof）。
  实测：77 对逐字全同金标重复对判 重复（tp=77/fp=0，precision_strict=1.0，
  S=P=77），CP 下界 0.9618 < 0.99 → **闸门仍诚实红（样本量界）**。
  释义级 FACT_EQUIVALENT 仍不颁发（需真 P14 覆盖证明，B2/B3 建设项）；
  263 对改写级金标重复对仍判 边界（recall=0.2265 如实测量）。
  本稿阈值断言按批复原文保留——红即红（INV-6），禁止调阈值或改断言。
  （历史存档：B1 前 L170 equivalence_ready 恒 False + 时间恒占位，P=0，
  见 p23-uat-run-2152.txt。）
- Fact 供给口径（建议书矛盾点 #1 附属 + 21:4x 主窗口裁定，红证据
  p23-uat-run-2138.txt）：真 P14 模型挂账 N3（B2 规则基线建设中，
  设计书 log/temp/b2-rule-p14-design.md）。初稿"零虚构 facts"
  模式经真机证伪——decide 对零 Fact 报告结构性拒收（PairAlignmentError，
  395/395 全灭，合同验证退化空转）。裁定改用**恒等投影脚手架**：
  每条报告注入恰一个机械投影 Fact（subject=全文原文、predicate 固定常量、
  evidence=真实偏移），零语义主张、非 P14 事实模型，全披露于指标 JSON；
  该脚手架仅覆盖文本证书路径可判之对（逐字全同），改写级对的语义判定
  待 B2 规则基线/B3 FACT_EQUIVALENT 签发逻辑，不以恒等投影冒充语义抽取。

公共标尺（目标文档 §三.5 + 设计 §8）：
1. 闸门 `P23_CONFIRM_UAT=1` 缺失即 skip；PROD_* 出现即拒启（本路径不需 TEST_*
   集群凭据——不连集群，故不以 TEST_ES_* 为闸件，差异已在建议书 §四登记）；
2. 金标不可改（INV-1）：manifest dataset_version 逐字节复算 + 工作簿 SHA-256/字节数
   与 manifest materials 钉死项对拍，不符即红（不 skip）；
3. 指标输出落 `log/temp/p23-replay-<run>.json`（测试内 print + 阈值断言）；
4. 失败不冒充边界（INV-4/§4.5）：回放异常单列 failures，不计入边界数；
5. 未标注不当负例（INV-5）：72 多行组/245 行未覆盖仅登记，不进指标分母。

跑站命令（主窗口执行；footer 分钟级向下取整）：

    $env:PYTHONPATH='src'
    $env:PYTHONIOENCODING='utf-8'
    $env:PYTHONDONTWRITEBYTECODE='1'
    $env:DEPLOY_ENV='test'
    $env:P23_CONFIRM_UAT='1'
    # 可选（默认取仓外钉死路径 C:\\Users\\ASUS\\Desktop\\文本去重）：
    # $env:P23_GOLD_WORKBOOK_DIR='<金标工作簿目录>'
    & '.venv-v1\\Scripts\\python.exe' -m pytest -p no:cacheprovider `
        --basetemp '.\\tmp\\p23-uat-run' -v tests/integration/test_p23_uat.py `
        > ..\\log\\temp\\p23-uat-run-<HHMM>.txt

残判层（窗口T · LLM 裁判层管线化一级，2026-09-28）：
- 开关 P23_RESIDUAL_JUDGE=llm_bidi_v1 启用；缺省关闭=现役行为逐字节不变
  （指标 JSON 不出现 residual_judge 段，场景 6a 钉守）。
- 启用后：规则链判"边界case/疑难case"的 clear 对（hold 对不判）→
  decide/llm_residual.SyncResidualJudge 双向裁判（hc+ch 两顺序均"重复"才签）
  + decide/machine_verify R0-R6（默认 audit 不降级；P23_RESIDUAL_JUDGE_MV=gate
  可切闸门语义）→ 残判结果仅入 item 扩展槽与指标 JSON residual_judge 段
  （合并口径 merged_*、cells、疑似队列、台账、residual_payload_hash）——
  五字段 public / DecideOutcome / 决策词汇全程不动（场景 6c 钉守）。
- 缓存：默认原位复用 workspace log/temp/d32-llm-judge/cache（pair_id 命名空间
  与金标逻辑对同一、prompt_sha256 逐字节对拍 D32 judge_v1 终版）→ 合并回放
  零 API 可复跑；P23_RESIDUAL_JUDGE_CACHE_DIR 可覆盖；P23_RESIDUAL_JUDGE_MODEL
  默认 qwen-turbo。双腿制：连跑两遍，residual_payload_hash 须逐字节同一。
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

# ---------- 常量（冻结合同 + 资产钉死值，全部可溯源） ----------

P23_UAT_ENV = "P23_CONFIRM_UAT"
P23_WORKBOOK_DIR_ENV = "P23_GOLD_WORKBOOK_DIR"

_WORKSPACE_ROOT = Path(__file__).resolve().parents[3]      # workspace-dedup
WORKSPACE_LOG = _WORKSPACE_ROOT / "log"
GOLD_MANIFEST_PATH = WORKSPACE_LOG / "P05-历史金标初版manifest.json"
# 仓外金标工作簿钉死路径（takeover_gold_20260924_145418_check.py L12-17 在案）
_DEFAULT_WORKBOOK_DIR = _WORKSPACE_ROOT.parent             # 文本去重
WORKBOOK_NAMES = (
    "v2_重复case_Fact特征分类标注版_与用户标注一致版.xlsx",
    "v2_重复case_第二批_会议口径标注版.xlsx",
)

EXPECTED_DATASET_VERSION = (
    "ds-legacy-v1-18d30c537665c577be080b1e56cd2f888d206ace37ae70aef08cffe574ff3f6a"
)
# manifest materials 钉死项（本窗口 2026-09-26 实测 Get-FileHash 逐字节吻合）
EXPECTED_MATERIALS = {
    "dbfda9677e96ab99a11f5259c2f9f231d23efed5460a8164e38ab22c1a33d26b": 132595,
    "83e635beb4209909087f577c2717c7977017dea22204cce764bbbf32954fac6b": 48003,
}
FROZEN_COUNTS = {
    "unique_explicit_pairs": 393,
    "clear_pairs": 389,
    "clear_duplicate_pairs": 340,
    "clear_nonduplicate_pairs": 49,
    "held_pairs": 4,
    "uncovered_multiline_groups": 72,
    "uncovered_group_rows": 245,
}

PUBLIC_KEYS = {"item_id", "text", "decision", "duplicate_ids", "reason"}
DECISION_VOCAB = {"重复", "不重复", "边界case/疑难case"}

# 闸门阈值（设计 §5.2 / 12 §6.6 + 05:31 修订①：作用于 S/P；未达标=红，不调阈凑绿）
PRECISION_STRICT_GATE = 0.995   # 建议 Precision≥99.5%（点估计）
CP_LOWER_GATE = 0.99            # 单侧 95% 下界 ≥99%

_SYNTHETIC_BUSINESS_DATE = "2000-01-01"   # P05 synthetic_plan 统一业务日（在案）


# ---------- 闸门（公共标尺 1；本路径不连集群，TEST_ES_* 非闸件——建议书 §四） ----------

def _gate_open(env) -> bool:
    """harness 级闸门：P23_CONFIRM_UAT=1 + 无 PROD_* 泄漏。"""
    if env.get(P23_UAT_ENV) != "1":
        return False
    if any(k.startswith("PROD_") for k in env):
        return False
    return True


pytestmark = pytest.mark.skipif(
    not _gate_open(os.environ),
    reason=f"P23 UAT gate not open (need {P23_UAT_ENV}=1, no PROD_*)",
)


# ---------- 规范哈希助手（与 takeover_gold_20260924_145418_check.py L27-32 同文） ----------

def _canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _record_id(row_key: str) -> str:
    """确定性 64-hex record_id（金标 row_key 派生；manifest 不含原始 ID，在案）。"""
    return _digest("p23-rid:" + row_key)


def _item_id(row_key: str) -> str:
    return "item-" + _digest("p23-item:" + row_key)[:16]


# ---------- 金标资产 fixture（INV-1：哈希不符=红；资产缺位=诚实 skip） ----------

def _load_manifest() -> dict:
    if not GOLD_MANIFEST_PATH.exists():
        pytest.skip(f"金标 manifest 不在仓内预期位置：{GOLD_MANIFEST_PATH}")
    return json.loads(GOLD_MANIFEST_PATH.read_text(encoding="utf-8"))


def _verify_manifest_pinned(manifest: dict) -> None:
    """INV-1：dataset_version 逐字节复算 + 冻结计数 + materials 钉死项对拍。"""
    recomputed = "ds-legacy-v1-" + _digest(_canonical(
        {key: value for key, value in manifest.items() if key != "dataset_version"}
    ))
    assert manifest.get("dataset_version") == recomputed, (
        "manifest dataset_version 复算不符——金标被改动（INV-1 违规，红）"
    )
    assert recomputed == EXPECTED_DATASET_VERSION, (
        f"manifest dataset_version {recomputed!r} 与冻结值 {EXPECTED_DATASET_VERSION!r} 不符"
    )
    counts = manifest["counts"]
    for key, expected in FROZEN_COUNTS.items():
        assert counts.get(key) == expected, (
            f"manifest counts[{key!r}]={counts.get(key)!r} 与冻结值 {expected!r} 不符"
        )
    materials = {m["material_id"]: m for m in manifest["materials"]}
    assert set(materials) == set(EXPECTED_MATERIALS), (
        f"manifest materials 钉死集 {sorted(materials)!r} 与在案集不符"
    )
    for material_id, byte_size in EXPECTED_MATERIALS.items():
        assert materials[material_id]["byte_size"] == byte_size


def _build_replay_plan(manifest: dict, rows: dict, plan_by_row: dict):
    """金标对 → 回放计划（角色/到达序/域日）。

    口径声明（建议书矛盾点 #3/#6）：
    - 正表行取 synthetic_plan 官方到达序（history = 低 seq，INV-2）；
    - 负表（不重复 sheet）40 对被 order_policy 排除在 plan 外 → 以
      sources 的 excel_row + A/B 端点合成确定序（100000+excel_row*2[+1]），
      合成事实写入指标 JSON，不冒充官方顺序；
    - 业务日统一 synthetic 2000-01-01（P05 在案）；scope 取 plan 值，
      负表对回退为 synthetic:<material_id[:16]>（与工作簿一一对应）；
    - **合并条目展开**（21:2x 主窗口修复，红证据 p23-uat-run-2125.txt）：
      gold/legacy.py L216-242 将同 family 多源对合并为一 manifest 条目
      （endpoint_row_keys=并集，sources 逐条保留原始子对 row_key/other_row_key）。
      393 manifest 条目 = 391 简单 + 2 合并（4 端点，皆 不重复/clear）
      → 展开为 395 逻辑回放对（391 clear + 4 hold）；manifest 层冻结计数
      （393/389/340/49）不动，逻辑对口径在指标 JSON 显式派生登记。
    """
    clear, hold = [], []
    for pair in manifest["pairs"]:
        endpoints_all = list(pair["endpoint_row_keys"])
        if len(endpoints_all) == 2:
            logical_pairs = [(pair["pair_id"], endpoints_all,
                              pair["sources"][0])]
        else:
            # 合并条目：sources 逐条 = 逻辑子对；并集必须等于 endpoint_row_keys
            logical_pairs = []
            union: set = set()
            for idx, source in enumerate(pair["sources"]):
                sub = [source["row_key"], source["other_row_key"]]
                union.update(sub)
                logical_pairs.append(
                    (f"{pair['pair_id']}#{idx + 1}", sub, source)
                )
            assert union == set(endpoints_all), (
                f"合并金标对 {pair['pair_id']!r} sources 并集 "
                f"!= endpoint_row_keys（资产形态异常）"
            )
        for logical_id, endpoints, neg_source in logical_pairs:
            for key in endpoints:
                assert key in rows, f"金标对端点 {key!r} 不在工作簿目录内"
                assert rows[key].raw_text, f"金标对端点 {key!r} 正文为空"
            seqs = {key: plan_by_row[key]["arrival_seq"]
                    for key in endpoints if key in plan_by_row}
            if len(seqs) == 1:
                pytest.fail(
                    f"金标对 {logical_id!r} 端点仅一侧在 synthetic_plan——"
                    "与 order_policy（负表对整对排除）矛盾，资产形态异常"
                )
            if not seqs:
                a_key, b_key = neg_source["row_key"], neg_source["other_row_key"]
                base = 100000 + neg_source["excel_row"] * 2
                seqs = {a_key: base, b_key: base + 1}
            history_key = min(endpoints, key=lambda k: (seqs[k], k))
            current_key = max(endpoints, key=lambda k: (seqs[k], k))
            plan_row = plan_by_row.get(current_key)
            scope_id = (plan_row["scope_id"] if plan_row
                        else f"synthetic:{rows[current_key].material_id[:16]}")
            business_date = (plan_row["business_date"] if plan_row
                             else _SYNTHETIC_BUSINESS_DATE)
            entry = SimpleNamespace(
                pair_id=logical_id,
                gold_label=pair["gold_label"],
                hold_reason=pair["hold_reason"],
                family_id=pair["family_id"],
                history_key=history_key,
                current_key=current_key,
                history_seq=seqs[history_key],
                current_seq=seqs[current_key],
                scope_id=scope_id,
                business_date=business_date,
            )
            (clear if pair["status"] == "clear" else hold).append(entry)
    return clear, hold


@pytest.fixture(scope="module")
def gold_assets():
    """金标资产装载：manifest（钉死复算）+ 工作簿（哈希对拍）+ 回放计划。"""
    manifest = _load_manifest()
    _verify_manifest_pinned(manifest)

    workbook_dir = Path(os.environ.get(P23_WORKBOOK_DIR_ENV,
                                       str(_DEFAULT_WORKBOOK_DIR)))
    books = tuple(workbook_dir / name for name in WORKBOOK_NAMES)
    missing = [b.name for b in books if not b.exists()]
    if missing:
        pytest.skip(
            f"金标工作簿不在本机（缺 {missing}；降级预案见 "
            "log/temp/p23-uat-wiring-proposal.md §五）——金标正文不可恢复，"
            "回放类用例诚实 skip（非红非绿）"
        )
    materials = {m["material_id"]: m for m in manifest["materials"]}
    for book in books:
        digest = _file_sha256(book)
        assert digest in materials, (
            f"工作簿 {book.name} SHA-256={digest} 不在 manifest materials 钉死清单"
            "——金标材料被替换（INV-1 违规，红）"
        )
        assert book.stat().st_size == materials[digest]["byte_size"]

    from news_flash_dedup.gold import load_workbooks   # 延迟导入：闸门用例保持轻依赖

    catalog = load_workbooks(books)
    rows = {row.row_key: row for row in catalog.rows}
    plan_by_row = {r["row_key"]: r for r in manifest["synthetic_plan"]["rows"]}
    for row_key in plan_by_row:
        assert row_key in rows, f"synthetic_plan 行 {row_key!r} 不在工作簿目录内"

    clear, hold = _build_replay_plan(manifest, rows, plan_by_row)
    # 逻辑对口径（21:2x 主窗口）：manifest 条目冻结计数不动（393/389/4），
    # 合并条目（>2 端点）按 sources 展开，每条 +len(sources)-1。
    merged_extra = sum(
        len(p["sources"]) - 1
        for p in manifest["pairs"] if len(p["endpoint_row_keys"]) > 2
    )
    assert len(clear) == FROZEN_COUNTS["clear_pairs"] + merged_extra
    assert len(hold) == FROZEN_COUNTS["held_pairs"]
    return SimpleNamespace(
        manifest=manifest, rows=rows, plan_by_row=plan_by_row,
        clear=clear, hold=hold, books=books,
        logical_clear=len(clear), merged_extra=merged_extra,
    )


def _identity_facts(row_key: str, row) -> list[dict]:
    """恒等投影 Fact（21:4x 主窗口裁定；字段形态照抄单元层 test_p17_2 L35-69 已封模板）。

    零语义主张：subject=全文原文（evidence 偏移真实可核）、predicate 固定常量、
    time/key_object/numerics 全 missing/空——不抽取、不推断、不编造任何语义；
    仅使 decide 的"报告须含 ≥1 Fact"结构性前置得到满足。非 P14 事实模型
    （N3 挂账终末期）；等价通路占位下该投影不产生任何"重复"输出。
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


_FACT_SUPPLY_ENV = "P23_FACT_SUPPLY"
_FACT_SUPPLY_DEFAULT = "identity"
_FACT_SUPPLY_RULE = "rule_baseline_v1"
_FACT_SUPPLY_RULE_V2 = "rule_baseline_v2"
_FACT_SUPPLY_LLM = "llm_qwen_turbo_v1"
_LLM_CACHE_DIR = WORKSPACE_LOG / "temp" / "llm-fact-cache"
_LLM_KEY_ENV_FILE = WORKSPACE_LOG.parent / "mabc_service" / ".env"
_LLM_REPORTS: list[dict] = []          # 旁路上报收集（本 fixture 生命周期内）


def _llm_api_key() -> str:
    """用户 2026-09-27 04:0x 授权明文使用 mabc_service/.env 的 QWEN_API_KEY。"""
    for line in _LLM_KEY_ENV_FILE.read_text(encoding="utf-8").splitlines():
        if line.startswith("QWEN_API_KEY=") and not line.startswith("#"):
            return line.split("=", 1)[1].strip()
    raise RuntimeError(f"mabc_service/.env 未找到 QWEN_API_KEY（{_LLM_KEY_ENV_FILE}）")


def _facts_for(row_key: str, row) -> list[dict]:
    """Fact 供给选择（主窗口 22:5x 接线；懒导入，identity 默认路径零新依赖）。

    - identity（默认）：恒等投影脚手架（21:4x 裁定，T5 封盘基线可复跑）；
    - rule_baseline_v1：B2 规则式 P14 基线（facts/rule.py，诚实标注
      deterministic baseline，设计书 log/temp/b2-rule-p14-design.md）——
      B2+B3 全链实测模式：释义级等价（FACT_EQUIVALENT 八条件签发壁）只在
      真抽取 facts 下才可能触发；实测指标（含 fp 诚实仲裁）落独立 JSON。
    - llm_qwen_turbo_v1：P14-LLM 决策包 A 原型（facts/llm.py，qwen-turbo
      DashScope 兼容端点；磁盘缓存 log/temp/llm-fact-cache 复跑零调用；
      旁路上报入指标 JSON llm_supply_stats 段）。
    """
    mode = os.environ.get(_FACT_SUPPLY_ENV, _FACT_SUPPLY_DEFAULT)
    if mode == _FACT_SUPPLY_RULE:
        from news_flash_dedup.facts import rule as facts_rule
        return facts_rule.extract_facts(_record_id(row_key), row.raw_text)
    if mode == _FACT_SUPPLY_RULE_V2:
        from news_flash_dedup.facts import rule as facts_rule
        return facts_rule.extract_facts(
            _record_id(row_key), row.raw_text,
            dict_version=facts_rule.RULE_DICT_VERSION_V2)
    if mode == _FACT_SUPPLY_LLM:
        from news_flash_dedup.facts import llm as facts_llm
        # 07:4x A/B 接线：模型名可经 P23_LLM_MODEL 覆盖（默认 qwen-turbo）；
        # 缓存键含 model（D15），不同模型条目天然隔离、turbo 缓存不受污染。
        model = os.environ.get("P23_LLM_MODEL", "qwen-turbo")
        return facts_llm.extract_facts_llm(
            _record_id(row_key), row.raw_text,
            api_key=_llm_api_key(), model=model,
            cache_dir=str(_LLM_CACHE_DIR),
            report_hook=_LLM_REPORTS.append)
    return _identity_facts(row_key, row)


_NORM_DICT_ENV = "P23_NORM_DICT"

# ---------- 残判层开关（窗口T · LLM 裁判层管线化一级；缺省关闭=零漂移） ----------
_RESIDUAL_ENV = "P23_RESIDUAL_JUDGE"
_RESIDUAL_ENABLED_VALUE = "llm_bidi_v1"
_RESIDUAL_CACHE_ENV = "P23_RESIDUAL_JUDGE_CACHE_DIR"
_RESIDUAL_MODEL_ENV = "P23_RESIDUAL_JUDGE_MODEL"
_RESIDUAL_MV_ENV = "P23_RESIDUAL_JUDGE_MV"
# 缓存缺省原位复用 D32 裁判缓存（pair_id 命名空间与金标逻辑对同一、prompt_sha
# 逐字节对拍——329 对×2 顺序裁判条目全在场，金标合并回放零 API 可复跑）
_RESIDUAL_DEFAULT_CACHE = WORKSPACE_LOG / "temp" / "d32-llm-judge" / "cache"


def _residual_config():
    """残判层开关与装配（窗口T）。缺省/空=关闭（返 None，现役行为逐字节不变）；
    llm_bidi_v1=启用；其他非空值 fail-closed（防拼写漂移冒充关闭）。"""
    raw = os.environ.get(_RESIDUAL_ENV, "")
    if not raw:
        return None
    if raw != _RESIDUAL_ENABLED_VALUE:
        pytest.fail(f"{_RESIDUAL_ENV}={raw!r} 未知（fail-closed；合法值 "
                    f"{_RESIDUAL_ENABLED_VALUE!r} 或缺省关闭）")
    from news_flash_dedup.decide import llm_residual
    cfg = llm_residual.ResidualJudgeConfig(
        model=os.environ.get(_RESIDUAL_MODEL_ENV, llm_residual.DEFAULT_MODEL),
        cache_dir=os.environ.get(_RESIDUAL_CACHE_ENV,
                                 str(_RESIDUAL_DEFAULT_CACHE)),
        mv_mode=os.environ.get(_RESIDUAL_MV_ENV, llm_residual.MV_AUDIT))
    # api_key_fn 懒取：全缓存命中回放零 API，key 文件无需在场
    judge = llm_residual.SyncResidualJudge(cfg, api_key_fn=_llm_api_key)
    return SimpleNamespace(config=cfg, judge=judge, switch=raw)


def _residual_metrics_section(residual, items) -> dict:
    """残判层指标段（窗口T）：合并口径（规则签发+残判签发+auto 不重复章）+ 审计槽 + 台账。

    - merged_*：另起一只 MetricsAggregator（冻结聚合器/CP 公式复用零改动），
      残判签发对按 predicted=重复 + duplicate_ids=(history item_id,) 构造；
      双向均"不重复"对按 predicted=不重复 构造（用户 2026-09-28 裁定发放的
      "不重复章"：auto 结案不再滞留疑难队列；安全向——合并 tp/fp 不变，金标
      重复侧中章者转为可见 fn 如实登记）；基线 pair/strict 段不动，本段为
      叠加后的合并视图（五字段契约不变——残判结论不回写 public decision，
      仅在指标层物化）；
    - merged_three_way：合并口径三向分列（signed 重复/auto 不重复/余疑难）；
    - suspect_queue：残判后仍疑难对（双向不重复已 auto 结案不入队）按疑似度
      降序（同分 pair_id 升序，确定性）——人审分诊排序字段，入审计/扩展槽；
    - residual_payload_hash：逐对审计负载（cache_hit/latency 等运行元数据已
      剔除）canonical JSON 的 sha256——双腿制对拍锚（run_id 无关，两遍须
      逐字节同一）。
    """
    from news_flash_dedup.decide import llm_residual as _lr

    judged = [i for i in items if i.get("residual") is not None]
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
            # "不重复章"合并结算：双向均"不重复"→predicted=不重复（安全向，
            # tp/fp 不变；金标重复侧若中章则转为可见 fn 如实登记）
            rr = ReplayResult(
                history_record_id=rr.history_record_id,
                current_record_id=rr.current_record_id,
                predicted_decision="不重复",
                predicted_duplicate_ids=(),
                expected_decision=rr.expected_decision,
                expected_duplicate_ids=rr.expected_duplicate_ids)
        merged_agg.add(rr)
    cells: dict[str, dict[str, int]] = {}
    for item in judged:
        side = ("gold_duplicate" if item["gold_label"] == "重复"
                else "gold_nonduplicate")
        cell = item["residual"].get("cell", "failure")
        cells.setdefault(side, {}).setdefault(cell, 0)
        cells[side][cell] += 1
    # 合并口径三向分列（用户裁定"不重复章"发放后）：signed 重复 / auto 不重复 / 余疑难
    rule_signed = [i for i in items if i["public"]["decision"] == "重复"]
    res_signed = [i for i in judged if i["residual"].get("signed")]
    auto_nd = [i for i in judged if i["residual"].get("cell") == "not_duplicate"]
    remainder = [i for i in judged
                 if not i["residual"].get("signed")
                 and i["residual"].get("cell") not in (
                     "not_duplicate", "invalid", "failure")]
    three_way = {
        "signed_duplicate": {
            "total": len(rule_signed) + len(res_signed),
            "rule_signed": len(rule_signed),
            "residual_signed": len(res_signed)},
        "auto_not_duplicate": {
            "total": len(auto_nd),
            "gold_duplicate": sorted(
                i["pair_id"] for i in auto_nd if i["gold_label"] == "重复"),
            "gold_nonduplicate": sum(
                1 for i in auto_nd if i["gold_label"] != "重复")},
        "boundary_remainder": {
            "total": len(remainder),
            "gold_duplicate": sum(
                1 for i in remainder if i["gold_label"] == "重复"),
            "gold_nonduplicate": sum(
                1 for i in remainder if i["gold_label"] != "重复")},
    }
    suspect_queue = sorted(
        ({"pair_id": i["pair_id"], "gold_label": i["gold_label"],
          "suspicion_score": i["residual"]["suspicion_score"],
          "cell": i["residual"]["cell"],
          "hc_decision": i["residual"]["hc"]["decision"],
          "ch_decision": i["residual"]["ch"]["decision"],
          "fired_rules_union": i["residual"]["fired_rules_union"]}
         for i in judged
         if not i["residual"].get("signed")
         and i["residual"].get("cell") not in (
             "not_duplicate", "invalid", "failure")),
        key=lambda e: (-(e["suspicion_score"] or 0.0), e["pair_id"]))
    per_pair = sorted((i["residual"] for i in judged), key=lambda r: r["pair_id"])
    cfg = residual.config
    return {
        "switch": f"{_RESIDUAL_ENV}={residual.switch}",
        "layer": ("LLM 双向裁判（hc+ch 两顺序均'重复'才签）+机器验 R0-R6 "
                  "（decide/llm_residual.py + decide/machine_verify.py）；"
                  "判定逻辑=D32 终版（log/D32-大模型裁判三臂实验报告.md）"),
        "mv_mode": cfg.mv_mode,
        "mv_mode_note": ("audit=机验全量计算不降级（默认，主窗口 2026-09-28 裁定："
                         "双向判自身 fp=0/51，闸门边际 fp 收益为零、tp 成本 15 净负收益；"
                         "影子期实测后再定 A/B 终态）；gate=拦截→存疑（D32 §10 字面）"),
        "model": cfg.model,
        "prompt_version": _lr.JUDGE_PROMPT_VERSION,
        "prompt_sha256": _lr.PROMPT_SHA256,
        "cache_dir": str(cfg.cache_dir),
        "judged_pairs": len(judged),
        "signed": sum(1 for i in judged if i["residual"].get("signed")),
        "cells": cells,
        "residual_tp": sum(1 for i in judged
                            if i["residual"].get("signed")
                            and i["gold_label"] == "重复"),
        "residual_fp": sum(1 for i in judged
                            if i["residual"].get("signed")
                            and i["gold_label"] != "重复"),
        "merged_pair_metrics": dict(merged_agg.pair_metrics()),
        "merged_strict_metrics": dict(merged_agg.strict_metrics()),
        "merged_note": ("合并口径=规则签发+残判签发+auto 不重复章结算；基线 "
                        "pair_metrics/strict_metrics 段不动（残判结论不回写五字段 "
                        "public decision，仅在指标层物化）"),
        "merged_three_way": three_way,
        "merged_three_way_note": ("合并口径三向分列：signed 重复（规则签发+残判签发）/ "
                                  "auto 不重复（双向均'不重复'章结案——金标重复侧中章者"
                                  "转为可见 fn 如实登记于 gold_duplicate 列表）/ "
                                  "余疑难（单向不重复+存疑等混合，入 suspect_queue）"),
        "ledger": residual.judge.ledger.snapshot(),
        "suspect_queue": suspect_queue,
        "suspect_queue_note": ("残判后仍疑难对（双向混合/存疑格；双向'不重复'已 auto "
                               "结案不入队），按疑似度降序（score=0.5×(w(hc)+w(ch))"
                               "−0.05×|两顺序触发规则并集|，w(重复)=1/存疑=0.5/不重复=0）"
                               "——人审分诊用，排序字段入审计/扩展槽，五字段契约不动"),
        "invalid_pairs": sorted(i["pair_id"] for i in judged
                                 if i["residual"].get("cell") == "invalid"),
        "failure_pairs": sorted(i["pair_id"] for i in judged
                                 if i["residual"].get("cell") == "failure"),
        "failure_note": ("API 连续失败（重试 3 次后）/技术失败单列（INV 式，"
                         "不折算边界、不冒充签发）"),
        "per_pair": per_pair,
        "residual_payload_hash": _digest(json.dumps(
            per_pair, ensure_ascii=False, sort_keys=True, separators=(",", ":"))),
    }


def _load_norm_dictionary():
    """D19 杠杆 b 回放接线：P23_NORM_DICT=绝对路径 → 载入归一化词典。

    返回 (dictionary|None, version_label|None)。未设置环境变量 → (None, None)，
    回放全链 None 路径逐字节不变。词典版本/日期由文件内 pinning 字段与
    build_aligned 校验（错配 → PairAlignmentError 冒泡为技术失败，诚实红）。
    """
    path = os.environ.get(_NORM_DICT_ENV)
    if not path:
        return None, "dict_v1"  # 无词典路径默认版本串（decide 层默认值口径）
    from news_flash_dedup.compare import normalize_dict
    dictionary = normalize_dict.load_dictionary(path)
    return dictionary, dictionary.version


def _fact_supply_label() -> str:
    mode = os.environ.get(_FACT_SUPPLY_ENV, _FACT_SUPPLY_DEFAULT)
    if mode == _FACT_SUPPLY_RULE:
        return ("rule_baseline_v1（22:5x B2 规则式 P14 基线全链实测；deterministic "
                "baseline 非模型，W1-W8 已知弱项见设计书；fp 以本轮回放实测为诚实仲裁）")
    if mode == _FACT_SUPPLY_RULE_V2:
        return ("rule_baseline_v2（09:1x D19 杠杆 a 抽取覆盖扩展：_EVENT_VERBS "
                "v2 纯追加 79 词（行情/宏观/人事/事件，S1 闭合表实到驱动、R8 说类"
                "排除）+主体扩展 P2a 机构后缀/P2b 职务人名/P3a 公众人名小词典；"
                "v1 路径逐字节不变可复跑；fp 以本轮回放实测为诚实仲裁）")
    if mode == _FACT_SUPPLY_LLM:
        model = os.environ.get("P23_LLM_MODEL", "qwen-turbo")
        return (f"llm_qwen_turbo_v1（04:5x P14-LLM 决策包 A 原型全链实测；model={model} "
                "（P23_LLM_MODEL 可覆盖，07:4x A/B 接线）"
                "识别+本仓定位自证，幻觉引词降级 missing 纪律；缓存复跑零调用；"
                "fp 以本轮回放实测为诚实仲裁；当前提示词 p14_llm_prompt_v2"
                "（报道内容独立成事实+连续子串纪律，fp 6e02c930 根因定向修复））")
    return ("mechanical_identity_projection_no_semantics（21:4x 主窗口裁定：恒等投影"
            "脚手架，subject=全文原文/predicate=常量，零语义主张、非 P14 事实模型）")


def _ctx_mapping(row_key: str, row, seq: int, scope_id: str,
                 business_date: str) -> dict:
    """decide/commit 输入映射（facts 供给见 _facts_for；默认恒等投影 21:4x 裁定）。"""
    return {
        "record_id": _record_id(row_key),
        "item_id": _item_id(row_key),
        "text": row.raw_text,
        "raw_hash": row.raw_hash,          # = sha256(raw_text)（materials.py L88 同文）
        "scope_id": scope_id,
        "business_date": business_date,
        "arrival_seq": seq,
        "facts": _facts_for(row_key, row),
    }


# ---------- 回放执行 fixture（模块级一次跑完；指标 JSON 落盘一次） ----------

@pytest.fixture(scope="module")
def replay_run(gold_assets):
    """固定候选路径 (a) 全链回放：commit_one（内嵌 decide_for_task）+ FakeCommitStore。

    - 候选 = 金标对端点直接给定（不走召回，05:31 修订②）；
    - coverage_complete=True 显式传入并登记为占位（固定候选路径无召回实迹可派生；
      13:20 案二"真 UAT 路径禁默认 True"指真召回路径，占位声明见指标 JSON）；
    - decision_watermark 由本 fixture 自维护单调计数（与 arrival_seq 解耦，
      避免负表合成序与同 current 复用造成回退假失败）；
    - 每条回放产出 public dict + payload_hash 对拍值，供合同用例全量逐条断言；
    - 回放异常单列 failures（失败不冒充边界，INV-4）。
    """
    from news_flash_dedup.commit.coordinator import CommitContext, commit_one
    from news_flash_dedup.commit.fake_store import FakeCommitStore

    store = FakeCommitStore()
    watermark: dict[str, int] = {}
    items: list[dict] = []
    failures: list[dict] = []
    hold_items: list[dict] = []
    _norm_dictionary, _norm_dictionary_version = _load_norm_dictionary()
    residual = _residual_config()          # 窗口T：缺省 None=关闭（零漂移）

    def _drive(entry, *, allow_residual: bool) -> dict:
        rows = gold_assets.rows
        history = _ctx_mapping(entry.history_key, rows[entry.history_key],
                               entry.history_seq, entry.scope_id, entry.business_date)
        current = _ctx_mapping(entry.current_key, rows[entry.current_key],
                               entry.current_seq, entry.scope_id, entry.business_date)
        key = f"{entry.scope_id}|{entry.business_date}"
        watermark[key] = watermark.get(key, 0) + 1
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
        outcome = commit_one(ctx, store, decision_watermark_seq=watermark[key],
                             audit_complete=True,   # M-01 显式表态（窗口T 第 4 处去默认）
                             dictionary=_norm_dictionary,
                             dictionary_version=_norm_dictionary_version)
        if outcome.state != "committed" or outcome.decide_outcome is None:
            raise RuntimeError(f"commit_one 未提交：state={outcome.state!r}")
        decide = outcome.decide_outcome
        pub = decide.to_public_dict()
        record = store.main_records[current["record_id"]]
        item = {
            "pair_id": entry.pair_id,
            "gold_label": entry.gold_label,
            "hold_reason": entry.hold_reason,
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
        # 窗口T 残判层串接（P23_RESIDUAL_JUDGE=llm_bidi_v1；缺省关闭零改动）：
        # 规则链判"边界"的对 → 双向 LLM 判+机验 → 残判结果仅入 item 扩展槽
        # 与指标 JSON residual_judge 段——五字段 public / DecideOutcome /
        # 决策词汇不动；残判层技术失败单列（INV 式，不折算边界、不污染基线回放）。
        if (residual is not None and allow_residual
                and pub["decision"] == "边界case/疑难case"):
            rows = gold_assets.rows
            try:
                res_out = residual.judge.judge_pair(
                    entry.pair_id, rows[entry.history_key].raw_text,
                    rows[entry.current_key].raw_text)
                item["residual"] = res_out.to_audit_dict()
                # "不重复章"（用户 2026-09-28 裁定发放）：双向均"不重复"→扩展槽
                # 记 auto_not_duplicate，合并口径按 predicted=不重复 结算结案；
                # 单向不重复+另向存疑→保持疑难入 suspect_queue；机验 R0 纪律
                # 不变（不重复无机验对象）。五字段 public 不回写。
                if item["residual"].get("cell") == "not_duplicate":
                    item["residual"]["auto_not_duplicate"] = True
            except Exception as exc:
                item["residual"] = {
                    "pair_id": entry.pair_id, "cell": "failure",
                    "signed": False, "suspicion_score": None,
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:200]}
        return item

    ordered = sorted(gold_assets.clear,
                     key=lambda e: (e.scope_id, e.current_seq, e.current_key))
    for entry in ordered:
        try:
            items.append(_drive(entry, allow_residual=True))
        except Exception as exc:                       # 失败不冒充边界（INV-4）
            failures.append({
                "pair_id": entry.pair_id,
                "gold_label": entry.gold_label,
                "error_type": type(exc).__name__,
                "error": str(exc)[:200],
            })
    for entry in sorted(gold_assets.hold,
                        key=lambda e: (e.scope_id, e.current_seq, e.current_key)):
        try:
            hold_items.append(_drive(entry, allow_residual=False))
        except Exception as exc:
            failures.append({
                "pair_id": entry.pair_id,
                "gold_label": "hold",
                "error_type": type(exc).__name__,
                "error": str(exc)[:200],
            })

    agg = MetricsAggregator()
    for item in items:
        agg.add(item["replay_result"])
    pair_m = dict(agg.pair_metrics())
    strict_m = dict(agg.strict_metrics())

    m_total = len(ordered)
    boundary_n = sum(1 for i in items
                     if i["public"]["decision"] == "边界case/疑难case")
    time_cost = sum(
        1 for i in items
        if i["public"]["decision"] == "边界case/疑难case"
        and i["internal_code"] == "TIME_RELATION_UNCERTAIN"
    )
    synonym_cost = sum(
        1 for i in items
        if i["gold_label"] == "重复"
        and i["public"]["decision"] == "边界case/疑难case"
        and i["internal_code"] in {"FACT_INCOMPLETE", "SUBJECT_UNRESOLVED"}
    )
    hold_dist: dict[str, int] = {}
    for i in hold_items:
        hold_dist[i["public"]["decision"]] = hold_dist.get(i["public"]["decision"], 0) + 1

    run_id = "uat" + datetime.now(timezone.utc).strftime("%H%M%S%f")  # W-R3b 秒级→微秒
    metrics = {
        "run_id": run_id,
        "dataset_version": EXPECTED_DATASET_VERSION,
        "candidate_path": "fixed_candidate_(a)_gold_supplied_no_recall",
        "fact_supply": _fact_supply_label(),
        "norm_dictionary": (_norm_dictionary_version
                            if _norm_dictionary is not None else "none"),
        "coverage_complete": "explicit_true_placeholder_fixed_candidate",
        "pipeline_version": "dedup_v1",
        "counts": {
            "M": m_total,
            "H": len(gold_assets.hold),
            "replayed": len(items),
            "replay_failures": len(failures),
            "manifest_entries_total": FROZEN_COUNTS["unique_explicit_pairs"],
            "manifest_clear_entries": FROZEN_COUNTS["clear_pairs"],
            "merged_entry_expansion": gold_assets.merged_extra,
            "uncovered_multiline_groups": FROZEN_COUNTS["uncovered_multiline_groups"],
            "uncovered_group_rows": FROZEN_COUNTS["uncovered_group_rows"],
        },
        "pair_metrics": pair_m,
        "strict_metrics": strict_m,
        "boundary_rate_M": boundary_n / m_total if m_total else 0.0,
        # 21:4x 主窗口修复：failures 含 hold 失败，M 口径失败率只计 clear 失败
        # （原口径 len(failures)/M 在 hold 失败时 >1，红证据 p23-uat-run-2138.txt）
        "failure_rate_M": (
            sum(1 for f in failures if f["gold_label"] != "hold") / m_total
            if m_total else 0.0
        ),
        "hold_failures": sum(1 for f in failures if f["gold_label"] == "hold"),
        "time_dictionary_cost": {
            "boundary_with_TIME_RELATION_UNCERTAIN": time_cost,
            "note": (("恒等投影模式（21:4x 裁定）：仅 verbatim 全同文本对可对齐，"
                      "本计数=对齐对中 TIME_RELATION_UNCERTAIN 数（下界口径，77≈"
                      "金标中全同文本重复对规模）；真值待 Fact 供给+时间词典接线"
                      "（挂账 #1，终末期）")
                     if os.environ.get(_FACT_SUPPLY_ENV, _FACT_SUPPLY_DEFAULT)
                     == _FACT_SUPPLY_DEFAULT else
                     "真 Fact 供给模式（R9 F7c 文案修复 06:3x：note 按供给分支——"
                     "本计数=对齐对中 TIME_RELATION_UNCERTAIN 数，含释义对时间槽"
                     "实比较；挂账 #1 时间词典接线仍终末期）"),
        },
        "synonym_dictionary_cost": {
            "gold_duplicate_boundary_fact_incomplete": synonym_cost,
            "note": ("代理口径：金标重复被判边界且主因 FACT_INCOMPLETE/SUBJECT_UNRESOLVED"
                     "（码点级不一致含同义改写）；恒等投影下非全同文本产 no-match "
                     "而非 FACT_INCOMPLETE，故本项实测 0 属口径预期；真 synonym "
                     "命中率待词典升版（挂账 #2）"),
        },
        "pair_metrics_note": ("R9 F7b 口径注记（主窗口 06:3x）：tn 系报告字段、非 "
                              "12 §6.3 冻结口径成员（冻结仅 tp/fp/fn/FPR，边界率另报）；"
                              "金标'不重复'预测'边界'并入 tn，不进 precision/recall/FPR/"
                              "任何闸门公式——阅读时勿作'判对'理解"),
        "hold_set": {
            "distribution": hold_dist,
            "note": "4 hold 对只报分布不入 M 指标（§4.5 H 集；无金标标签可判正误）",
        },
        "deferred": {
            "recall_at_30_10": "挂账 #5：真召回路径 (b) 专属，固定候选 (a) 不测",
            "entity_recall_N2": "挂账 N2：实体误召/漏召需真 P14 供给，本轮不可测",
            "chinese_numerals_N16": "挂账 N16：中文数词/单位词典扩展随金标回放，本轮占位",
            "p13_contract_N20": "挂账 N20：真召回适配器契约，(a) 路径不测",
            "s_qual_domain_date_#9": "S-qualification 域日项：synthetic 单日 2000-01-01，"
                                     "无域日多样性，仅结构性声明（挂账 #9 部分可测）",
        },
        "p_set_audit": [
            {
                "pair_id": item["pair_id"],
                "gold_label": item["gold_label"],
                "expected_duplicate_ids": sorted(
                    item["replay_result"].expected_duplicate_ids),
                "predicted_duplicate_ids": sorted(
                    item["replay_result"].predicted_duplicate_ids),
                "s_qualified": set(item["replay_result"].predicted_duplicate_ids)
                <= set(item["replay_result"].expected_duplicate_ids),
            }
            for item in items
            if item["replay_result"].predicted_decision == "重复"
        ],
        "p_set_audit_note": ("主窗口 23:2x 增设：P 集（predicted==重复）逐条审计——"
                             "s_qualified=false 即 fp（12 §6.4 OUTPUT 口径），"
                             "供 W3/W6 假对齐根因追查与 T8 终审证据"),
        "llm_supply_stats": {
            "calls": sum(1 for r in _LLM_REPORTS if not r.get("cache_hit")),
            "cache_hits": sum(1 for r in _LLM_REPORTS if r.get("cache_hit")),
            "avg_latency_s": (round(sum(r.get("latency_s", 0.0)
                                        for r in _LLM_REPORTS
                                        if not r.get("cache_hit"))
                                    / max(1, sum(1 for r in _LLM_REPORTS
                                                 if not r.get("cache_hit"))), 3)),
            "issue_entries": sum(len(r.get("issues", [])) for r in _LLM_REPORTS),
            "issues_sample": [i for r in _LLM_REPORTS
                              for i in r.get("issues", [])][:20],
            "note": ("仅 llm_qwen_turbo_v1 供给时有意义：幻觉引词降级 missing 的"
                     " issue 流水（全量侧车 log/temp/llm-fact-cache 逐文 JSON）"),
        },
        "failures": failures,
    }
    # 窗口T：残判层指标段仅在开关启用时存在——缺省关闭整段不出现（逐字节零漂移）
    if residual is not None:
        metrics["residual_judge"] = _residual_metrics_section(residual, items)
    metrics["payload_hash"] = compute_payload_hash(
        {k: v for k, v in metrics.items() if k != "payload_hash"}
    )

    out_path = WORKSPACE_LOG / "temp" / f"p23-replay-{run_id}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(metrics, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    print(f"\n[P23-UAT] metrics json: {out_path}")
    print(f"[P23-UAT] pair={json.dumps(pair_m, ensure_ascii=False, sort_keys=True)}")
    print(f"[P23-UAT] strict={json.dumps(strict_m, ensure_ascii=False, sort_keys=True)}")
    print(f"[P23-UAT] boundary_rate_M={metrics['boundary_rate_M']:.6f} "
          f"failures={len(failures)}")
    if residual is not None:
        res = metrics["residual_judge"]
        print(f"[P23-UAT] residual_judge: judged={res['judged_pairs']} "
              f"signed={res['signed']} (tp={res['residual_tp']} "
              f"fp={res['residual_fp']}) ledger={res['ledger']}")
        print(f"[P23-UAT] merged pair="
              f"{json.dumps(res['merged_pair_metrics'], ensure_ascii=False, sort_keys=True)}")
        print(f"[P23-UAT] merged strict="
              f"{json.dumps(res['merged_strict_metrics'], ensure_ascii=False, sort_keys=True)}")
        print(f"[P23-UAT] residual_payload_hash={res['residual_payload_hash']}")
    return SimpleNamespace(
        items=items, failures=failures, hold_items=hold_items,
        pair=pair_m, strict=strict_m, metrics=metrics,
        metrics_path=out_path, store=store, residual=residual,
    )


# ---------- 场景 1 · 闸门（harness 级恒可跑：不依赖金标资产/src 接线） ----------

def test_p23_uat_harness_gate_requires_confirm_flag():
    """场景 1（公共标尺 1）：`P23_CONFIRM_UAT=1` 缺失即 skip/拒启；PROD_* 拒启。

    harness 级恒可跑：仅校闸门谓词逻辑，不触金标资产、不触集群、不触 src 接线。
    模块级 skipif 即"缺失即 skip"的诚实语义本身（全套件守底时本文件 7 用例全 skip，
    理由字符串含闸门变量名）。
    """
    assert _gate_open({P23_UAT_ENV: "1"}) is True
    assert _gate_open({}) is False                                   # 缺闸门
    assert _gate_open({P23_UAT_ENV: "0"}) is False
    assert _gate_open({P23_UAT_ENV: "1", "PROD_ES_HOST": "x"}) is False
    assert _gate_open({P23_UAT_ENV: "1", "PROD_CALLBACK_URL": "https://prod.example.invalid"}) is False
    # 本路径不连集群：TEST_ES_* 有无不影响闸门（差异登记建议书 §四）
    assert _gate_open({P23_UAT_ENV: "1", "TEST_ES_HOST": "es.example"}) is True


# ---------- 场景 1b · 金标 manifest 钉死（INV-1；不符=红不 skip） ----------

def test_p23_gold_manifest_integrity_hash_pinned():
    """INV-1：manifest 哈希钉死 + 冻结计数 + materials 钉死项 + 无正文纪律。

    dataset_version 按 takeover 对账脚本同文复算（canonical JSON 去版本键
    再 sha256，前缀 ds-legacy-v1-）；任何字节级改动 → 复算不符 → 红。
    附纪律核验：pairs 条目键集不含 text/raw_text/raw_id/quote（P05 证据 L9：
    manifest 只存行键/摘要/标签/计数）。
    """
    manifest = _load_manifest()
    _verify_manifest_pinned(manifest)
    forbidden = {"text", "raw_text", "raw_id", "quote"}
    for pair in manifest["pairs"]:
        assert not (set(pair) & forbidden), (
            f"金标对 {pair['pair_id']!r} 含正文键——违反 P05 无正文纪律"
        )
    assert manifest["counts"]["M"] == 0 and manifest["counts"]["q_plus"] == 0, (
        "legacy manifest 无 M/H/q_plus 官方赋值（synthetic_only）；"
        "回放 q_plus 口径 = 金标重复对数 340（建议书矛盾点 #4 登记）"
    )


# ---------- 场景 1c · 工作簿钉死 + 正文可恢复（资产缺位=诚实 skip） ----------

def test_p23_gold_workbook_integrity_and_text_recovery(gold_assets):
    """INV-1 延伸：两本工作簿 SHA-256/字节数与 manifest materials 对拍；
    393 对全部端点正文可恢复（raw_text 非空）；synthetic_plan 951 行全解析。

    工作簿缺位 → fixture 诚实 skip（非红非绿，降级预案见建议书 §五）；
    哈希不符 → fixture 断言红（金标材料被替换，INV-1 违规）。
    """
    manifest = gold_assets.manifest
    assert len(manifest["synthetic_plan"]["rows"]
               ) == manifest["counts"]["synthetic_replay_rows"] == 951
    clear_dup = sum(1 for e in gold_assets.clear if e.gold_label == "重复")
    clear_neg = sum(1 for e in gold_assets.clear if e.gold_label == "不重复")
    # 逻辑对口径：合并条目（2 条皆 不重复/clear）展开后负例 +merged_extra；
    # manifest 层冻结计数（340/49）由 test_p23_gold_manifest_integrity_hash_pinned 钉守
    assert clear_dup == FROZEN_COUNTS["clear_duplicate_pairs"]
    assert clear_neg == (
        FROZEN_COUNTS["clear_nonduplicate_pairs"] + gold_assets.merged_extra
    )
    for entry in gold_assets.clear + gold_assets.hold:
        assert entry.history_seq < entry.current_seq, (
            f"金标对 {entry.pair_id!r} history.arrival_seq >= current（INV-2 违规）"
        )


# ---------- 场景 2 · 输出合同：全量逐条五字段 + 闭合词表（非抽样） ----------

def test_p23_output_contract_five_fields_closed_vocab_full_replay(replay_run):
    """场景 2（冻结合同①②）：389 clear + 4 hold 全量逐条断言（非抽样）。

    每条回放产出：① public dict 恰五字段（多一个少一个都违规）；
    ② decision ∈ 闭合词表；③ duplicate_ids 为 list 且与 decision 严格耦合
    （重复 ↔ 非空）；④ item_id/text/reason 非空；⑤ commit 层 payload_hash ==
    sha256(sort_keys 五字段 JSON)（P17 提交链五字段一致性，挂账 N14 校准锚）。
    覆盖完整性：成功条数 + 失败条数 == 金标对总数（失败单列，不冒充合同产出）。
    """
    total = len(replay_run.items) + len(replay_run.hold_items)
    # 防空转闸（21:4x 主窗口）：回放全灭时覆盖等式仍可自洽（0+395==395），
    # 合同验证将退化为零条空转——必须显式判红（红证据 p23-uat-run-2138.txt）
    assert replay_run.items, (
        "回放成功数为 0——输出合同验证将退化空转（全灭即红，不空转冒充验证）"
    )
    expected = (replay_run.metrics["counts"]["M"]
                + replay_run.metrics["counts"]["H"])
    assert total + len(replay_run.failures) == expected, (
        f"回放覆盖不完整：成功 + 失败 != {expected} 逻辑金标对"
        "（393 manifest 条目经合并展开 = 395 逻辑对）"
    )
    for item in replay_run.items + replay_run.hold_items:
        pub = item["public"]
        assert set(pub.keys()) == PUBLIC_KEYS, (
            f"pair {item['pair_id']!r} 输出字段集 {sorted(pub.keys())!r} != 恰五字段"
        )
        assert pub["decision"] in DECISION_VOCAB, (
            f"pair {item['pair_id']!r} decision={pub['decision']!r} 越出闭合词表"
        )
        assert isinstance(pub["duplicate_ids"], list)
        assert (pub["decision"] == "重复") == bool(pub["duplicate_ids"]), (
            f"pair {item['pair_id']!r} decision 与 duplicate_ids 耦合破坏"
        )
        assert isinstance(pub["item_id"], str) and pub["item_id"]
        assert isinstance(pub["text"], str) and pub["text"]
        assert isinstance(pub["reason"], str) and pub["reason"]
        expected_hash = hashlib.sha256(
            json.dumps(pub, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        assert item["payload_hash"] == expected_hash, (
            f"pair {item['pair_id']!r} commit 层 payload_hash 与五字段 JSON 不符"
        )


# ---------- 场景 3 · 指标正确性：CP 对拍 scipy + P-set 按 OUTPUT 独立复算 ----------

def test_p23_metrics_correctness_cp_scipy_and_output_p_set(replay_run):
    """场景 3（冻结合同③④）：CP 下界与 scipy 直调逐字节对拍；P-set 按 OUTPUT 复算。

    ① `_safe_lower_bound(x, n)` 与 `float(scipy.stats.beta.ppf(0.05, x, n-x+1))`
       在 (389,389)/(337,340)/(9,10)/(0,389)/(0,0) 上逐一相等
       （x=0/n=0 → 0.0 保留，无 x=n 白送约定，05:31 修订①）；
    ② 在真实回放结果上独立复算：P = #{predicted==重复}（不论金标，13:20 案一）、
       q_plus = #{gold==重复}、S = #{predicted==重复 ∧ D_i 非空 ∧ D_i ⊆ g_i}，
       与聚合器输出逐一相等（聚合器单元层已封，本步证接线无误）。
    """
    from scipy.stats import beta

    for x, n in ((389, 389), (337, 340), (9, 10), (0, 389), (0, 0)):
        expected = 0.0 if (n <= 0 or x <= 0) else float(beta.ppf(0.05, x, n - x + 1))
        assert _safe_lower_bound(x, n) == expected, (
            f"CP 下界 ({x},{n}) 与 scipy 直调不符——冻结公式被改动"
        )
    # x=n=389 不得白送 1.0（05:31 修订①：lower = 0.05^(1/389) ≈ 0.9923）
    assert 0.99 < _safe_lower_bound(389, 389) < 1.0

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
    assert strict["P"] == p_recount, "P-set 未按 OUTPUT 口径（13:20 案一回潮）"
    assert strict["q_plus"] == q_plus_recount
    assert strict["S"] == s_recount
    if strict["P"] > 0:
        assert strict["precision_strict"] == strict["S"] / strict["P"]
        assert strict["clopper_pearson_lower_95"] == float(
            beta.ppf(0.05, strict["S"], strict["P"] - strict["S"] + 1)
        )
    else:
        # P=0 时 CP 下界按冻结语义为 0.0——闸门必然不闭合（INV-6 不宣称达标）
        assert strict["precision_strict"] == 0.0
        assert strict["clopper_pearson_lower_95"] == 0.0


# ---------- 场景 4 · 金标回放端到端（固定候选 a）+ 批复阈值闸 ----------

def test_p23_gold_replay_e2e_fixed_candidate_threshold_gate(replay_run):
    """场景 4（设计 §5.2 / 12 §6.6 + 05:31 批复）：金标输入 → 全链决策 → 五字段输出
    → 与金标标签比对 → 指标达标断言。

    闸门（一字不可动）：precision_strict（=S/P）≥ 99.5% 且 CP 单侧 95% 下界 ≥ 99%。
    **不达标 = 红，不许调阈值凑绿**（任务纪律 + INV-6：CP 未闭合不宣称达标）。

    预期告知（建议书矛盾点 #1，待主窗口裁定）：当前 src 等价通路占位
    （p15_integration L170 equivalence_ready 恒 False + L161-164 时间恒
    unresolved），decide_for_task 结构性不可能输出"重复" → P=0 → 本闸在当前
    src 态必然不闭合（红）。该红是 UAT 对"输入输出是否符合最初要求"的诚实
    回答，不是测试缺陷；禁止以改断言/调阈值/xfail 任何形式藏红。
    指标 JSON 已落盘（replay_run fixture），主窗口复算以实物为准。
    """
    strict = replay_run.strict
    assert replay_run.metrics_path.exists(), "指标 JSON 未落盘"
    assert strict["precision_strict"] >= PRECISION_STRICT_GATE, (
        f"precision_strict={strict['precision_strict']:.6f} < "
        f"{PRECISION_STRICT_GATE}（S={strict['S']} P={strict['P']}）——"
        "12 §6.6 闸门未闭合（INV-6：不宣称达标；矛盾点 #1 待裁定）"
    )
    assert strict["clopper_pearson_lower_95"] >= CP_LOWER_GATE, (
        f"CP 下界={strict['clopper_pearson_lower_95']:.6f} < {CP_LOWER_GATE}——"
        "Clopper-Pearson 未闭合（INV-6：不宣称达标；矛盾点 #1 待裁定）"
    )


# ---------- 场景 5 · T5 挂账项核对：测量值产出并落 JSON ----------

def test_p23_t5_deferred_items_measurement_written(replay_run):
    """场景 5（挂账分流核对表 T5 行）：挂账测量项实测值产出 + 内部一致性。

    可测项（本路径）：#1 时间词典成本（TIME_RELATION_UNCERTAIN 计数，含上游掩盖
    声明）、#2 同义词词典成本（FACT_INCOMPLETE 代理口径）、#3 金标真边界率
    （predicted 边界 / 389，对照 P16-D 合成 0% 裁定）、#9 S-qualification 可测部
    （S 计数 + 五字段合格由场景 2 全量背书）；
    仍挂账（本路径结构性不可测）：#5 Recall@30/10（真召回 (b) 专属）、N2 实体
    误召/漏召（需真 P14）、N16 中文数词扩展、N20 P13 真适配器契约——
    四项 deferred 标记必须落 JSON（防遗忘，核对表 §三 T5 列）。
    一致性断言：各项计数自洽（0 ≤ 成本 ≤ 边界数 ≤ 389；hold 分布合计 = 4）。
    """
    metrics = json.loads(replay_run.metrics_path.read_text(encoding="utf-8"))
    boundary_n = round(metrics["boundary_rate_M"] * metrics["counts"]["M"])
    assert 0 <= metrics["time_dictionary_cost"][
        "boundary_with_TIME_RELATION_UNCERTAIN"] <= boundary_n
    assert 0 <= metrics["synonym_dictionary_cost"][
        "gold_duplicate_boundary_fact_incomplete"] <= boundary_n
    # W-R3b（R3-M7-d）：原三处恒真弱式已清（0.0≤率≤1.0 ×2 + 分布合计
    # +0≤4 ×1，327-333 窗死已清先例照）——红探针实证（w-r3b-red-probes）：
    # 语义错误变异（率互换/分布错类）下弱式全绿零鉴别力；其唯一可咬域
    # （率>1/合计>4）由生产式结构不变量（:829/:832-835 分子≤分母、hold
    # 合计≤4）与下方精确等值钉覆盖，清除零损失。
    # hold 分布 + hold 失败合计 = 4（hold 失败在 failures 里带 gold_label="hold"）
    hold_failures = sum(1 for f in metrics["failures"] if f["gold_label"] == "hold")
    assert sum(metrics["hold_set"]["distribution"].values()
               ) + hold_failures == FROZEN_COUNTS["held_pairs"]
    for key in ("recall_at_30_10", "entity_recall_N2",
                "chinese_numerals_N16", "p13_contract_N20"):
        assert key in metrics["deferred"], f"挂账测量缺 deferred.{key}（防遗忘违规）"
    assert metrics["counts"]["M"] == (
        FROZEN_COUNTS["clear_pairs"] + metrics["counts"]["merged_entry_expansion"]
    ), "M 应为逻辑对（manifest clear 389 + 合并条目展开各 +1 = 391），口径已登记"
    assert metrics["counts"]["uncovered_multiline_groups"] == 72, (
        "72 多行组未覆盖必须登记（INV-5：未标注不当负例）"
    )
    # 金标真边界率（挂账 #3 实测值，对照 P16-D 合成 0/46 裁定"门太松不予采信"）
    print(f"[P23-UAT] T5 测量：真边界率={metrics['boundary_rate_M']:.6f} "
          f"时间词典成本={metrics['time_dictionary_cost']['boundary_with_TIME_RELATION_UNCERTAIN']} "
          f"同义词成本(proxy)={metrics['synonym_dictionary_cost']['gold_duplicate_boundary_fact_incomplete']}")


# ---------- 场景 6 · 残判层（窗口T：LLM 裁判层管线化一级） ----------

_RESIDUAL_ON = os.environ.get(_RESIDUAL_ENV, "") == _RESIDUAL_ENABLED_VALUE


def test_p23_residual_switch_off_zero_effect(replay_run):
    """场景 6a（窗口T 验收：开关缺省零效应）：缺省关闭=现役行为逐字节不变。

    指标 JSON 不出现 residual_judge 段；基线 pair/strict/boundary/failures 由
    场景 3/4/5 既有断言守底（本用例只钉残判层自身的零效应面）。
    """
    if _RESIDUAL_ON:
        pytest.skip(f"{_RESIDUAL_ENV} 已启用——零效应用例仅在缺省关闭时断言")
    assert "residual_judge" not in replay_run.metrics
    assert replay_run.residual is None


def test_p23_residual_merged_metrics_anchor(replay_run):
    """场景 6b（窗口T 验收：金标合并回放合并口径 + 双腿制锚 + 台账）。

    合并口径（规则签发+残判签发）对拍 D32：规则链 64 + 残判 264 = 328
    （验收窗 64+266±3=[327,333]；残判宇宙 276=278−2 新晋规则签发），fp=0/51。
    台账：残判宇宙 ⊆ D32 329 对缓存宇宙 → api_calls=0、cache_hits=2×judged、
    failures=0；residual_payload_hash 在场（双腿制两遍逐字节同一的对拍锚，
    跨 run 比对见 log/窗口T-LLM裁判层管线化报告.md）。

    （窗口 Z3 死断言清理：原 `assert 327 <= merged_pair["tp"] <= 333`
    验收窗宽断言删除——上一条 ==328 精确钉先于它求值且语义严格更强：
    tp==328 通过则窗断言恒真，tp!=328 则精确钉先红，窗断言永无触发路径
    （死断言）。验收窗口径保留于本 docstring 备查，执法归 ==328 精确钉。）
    """
    if not _RESIDUAL_ON:
        pytest.skip(f"需 {_RESIDUAL_ENV}={_RESIDUAL_ENABLED_VALUE}")
    res = replay_run.metrics["residual_judge"]
    merged_pair = res["merged_pair_metrics"]
    assert merged_pair["tp"] == 328, (
        f"合并 tp={merged_pair['tp']} != 328（规则 64+残判 264，D32 对拍）"
        f"——cells={res['cells']} judged={res['judged_pairs']}")
    assert merged_pair["fp"] == 0, (
        f"合并 fp={merged_pair['fp']} != 0/51（D32 双向判 fp=0 被破）")
    assert res["residual_fp"] == 0
    merged_strict = res["merged_strict_metrics"]
    assert merged_strict["P"] == merged_pair["tp"] + merged_pair["fp"]
    assert merged_strict["S"] == merged_pair["tp"]
    # 台账：全缓存命中零 API（D32 缓存原位复用；残判宇宙 ⊆ 329 对宇宙）
    assert res["ledger"]["api_calls"] == 0, (
        f"残判层发生真实 API 调用 {res['ledger']['api_calls']} 次——"
        "缓存未全命中（缓存目录/prompt_sha/pair_id 命名空间漂移？）")
    assert res["ledger"]["cache_hits"] == 2 * res["judged_pairs"]
    assert res["ledger"]["failures"] == 0
    assert res["failure_pairs"] == [] and res["invalid_pairs"] == []
    # 双腿制对拍锚 + 疑似队列降序
    assert len(res["residual_payload_hash"]) == 64
    scores = [e["suspicion_score"] for e in res["suspect_queue"]]
    assert scores == sorted(scores, reverse=True), "疑似队列未按疑似度降序"
    # 残判签发对数与合并口径自洽
    assert res["signed"] == res["residual_tp"] + res["residual_fp"]
    assert (replay_run.pair["tp"] + res["residual_tp"]) == merged_pair["tp"]


def test_p23_residual_public_contract_untouched(replay_run):
    """场景 6c（窗口T 验收：五字段输出契约不动）：残判结论不回写 public decision。

    残判签"重复"/auto"不重复"章仅在合并指标层物化；每对五字段 public 保持
    规则链原判（残判签发对/中章对的 public.decision 仍为 边界case/疑难case）。
    """
    if not _RESIDUAL_ON:
        pytest.skip(f"需 {_RESIDUAL_ENV}={_RESIDUAL_ENABLED_VALUE}")
    signed_n = 0
    auto_n = 0
    for item in replay_run.items:
        res = item.get("residual")
        if res and res.get("signed"):
            signed_n += 1
            assert item["public"]["decision"] == "边界case/疑难case", (
                f"pair {item['pair_id']!r} 残判签发回写了 public decision——"
                "五字段输出契约被破坏")
        if res and res.get("cell") == "not_duplicate":
            auto_n += 1
            assert res.get("auto_not_duplicate") is True, (
                f"pair {item['pair_id']!r} 双向不重复但扩展槽缺 auto_not_duplicate 章")
            assert item["public"]["decision"] == "边界case/疑难case", (
                f"pair {item['pair_id']!r} auto 不重复章回写了 public decision——"
                "五字段输出契约被破坏")
        elif res is not None:
            assert res.get("auto_not_duplicate") is not True, (
                f"pair {item['pair_id']!r} 非双向不重复格却带 auto_not_duplicate 章")
    assert signed_n == replay_run.metrics["residual_judge"]["signed"]
    assert (auto_n == replay_run.metrics["residual_judge"]
            ["merged_three_way"]["auto_not_duplicate"]["total"])


def test_p23_residual_auto_not_duplicate_three_way(replay_run):
    """场景 6d（窗口T 验收：用户裁定"不重复章"三向分列对拍 D32 双向五格）。

    预期（D32 五格实测）：对照侧 36 对 auto 不重复（正确）；重复侧 1 对稳定
    不重复=a8eec545…（穆哈/摩卡，从疑难转为可见 fn 如实登记）；疑难队列
    48→11（dup 侧 doubtful 11）；合并 tp=328/fp=0/fn=12/tn=51 不变（安全向）。
    """
    if not _RESIDUAL_ON:
        pytest.skip(f"需 {_RESIDUAL_ENV}={_RESIDUAL_ENABLED_VALUE}")
    res = replay_run.metrics["residual_judge"]
    tw = res["merged_three_way"]
    # H2 约修复后分解（09-29 W2Fζ 实测，合并四维不变）：H2 翻转对被残判层
    # 接住——规则签发 64→63、残判签发 264→265；signed 总量 328 与
    # (tp,fp,fn,tn)=(328,0,12,51)、CP 下界均不变（本场景末段合并四维钉守）。
    # 迁移注：H-04 修法 A 供给面迁移 63+265→0+328（rule 签发面全灭、残判全量
    # 承接，signed 总值 328 不动），测量锚=eval-金标回放-20261007，判定团 C 案补丁⑬a 程序。
    assert tw["signed_duplicate"] == {
        "total": 328, "rule_signed": 0, "residual_signed": 328}, (
        f"signed 重复三向={tw['signed_duplicate']} != 328=0+328")
    auto = tw["auto_not_duplicate"]
    assert auto["total"] == 37 and auto["gold_nonduplicate"] == 36, (
        f"auto 不重复={auto} != 37（对照 36+重复侧 1），D32 五格对拍破裂")
    assert len(auto["gold_duplicate"]) == 1, (
        f"重复侧 auto 不重复 {len(auto['gold_duplicate'])} 对 != 1（应仅 a8eec545 穆哈/摩卡）")
    assert auto["gold_duplicate"][0].startswith("a8eec545"), (
        f"重复侧 auto 不重复对={auto['gold_duplicate'][0][:16]} != a8eec545…"
        "（D32 稳定不重复对穆哈/摩卡漂移）")
    assert tw["boundary_remainder"] == {
        "total": 11, "gold_duplicate": 11, "gold_nonduplicate": 0}, (
        f"余疑难={tw['boundary_remainder']} != 11（dup 侧 doubtful）")
    # 疑难队列 48→11：双向不重复已 auto 结案不入队
    assert len(res["suspect_queue"]) == 11, (
        f"疑难队列 {len(res['suspect_queue'])} != 11（48−36 auto 对照−1 auto 重复侧）")
    assert all(e["gold_label"] == "重复" for e in res["suspect_queue"])
    # 合并总量安全向不变（auto 章不动 tp/fp）
    merged_pair = res["merged_pair_metrics"]
    assert (merged_pair["tp"], merged_pair["fp"], merged_pair["fn"],
            merged_pair["tn"]) == (328, 0, 12, 51), (
        f"合并四维={merged_pair} != (328,0,12,51)——auto 章破安全向")
    # D32 五格原样保留（cells 不受三向重列影响）——H2 约修复后分解
    # （09-29 W2Fζ 实测，合并四维不变）：翻转对被残判层接住 signed 264→265。
    # 迁移注：H-04 修法 A 残判承接 signed 265→328（同一供给面迁移，测量锚=
    # eval-金标回放-20261007，判定团 C 案补丁⑬a 程序）。
    assert res["cells"] == {
        "gold_duplicate": {"signed": 328, "doubtful": 11, "not_duplicate": 1},
        "gold_nonduplicate": {"not_duplicate": 36}}, f"cells={res['cells']} 漂移"
