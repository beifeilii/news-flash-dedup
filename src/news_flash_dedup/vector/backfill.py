"""B5/E2 S6 真轨建档回填程序（设计 §5.2 B-0..B-3，程序照原文）。

§5.2 原文四步（执行主体 = tests/integration/test_emb_shadow.py 真轨 harness，
本模块承载可注入程序本体；真跑待主窗口放行令）：

- **B-0 建档**：`p19_<run>_<space_id(1024)>` 集合 + 当日分区；出：schema describe
  对拍（八字段 + dim=1024 + 隔离尾）+ ES 双索引在场。
- **B-1 金标回填**：工作簿正文 → chunk → embedding → upsert（嵌入缓存使回填
  与 replay 零边际成本）；出：记录数 / chunk 覆盖率 / 抽样向量有限值核对
  （validate_vector）+ usage 台账。
- **B-2 对拍**：§4.3 双腿对拍；出：验收闸全过（闸 2 锚 recall@30=1.0000
  263/263 N23）。本模块提供证据独立复算入口（b2_verify_gates）——shadow
  证据 JSON 的四闸复算，不依赖 harness 自报。
- **B-3 签收（R191 冻结版）**：R191 用户令删除冻结——清场环节整体取消，
  一切删除禁止、旧半成品原地封存、最终处置归用户亲决。B-3 改为**只读
  签收**（b3_signoff_inventory）：本 run 对象在场核验 + 「新 run 对象之外
  零变化」diff（封存物两版名单恒等）。原删除式 b3_signoff_cleanup 已按令
  移除，永不再供。

INV-1：回填失败 record 诚实 failed + 台账；不跳过不掩盖（reports 逐条在案）。
预算：影子小闸 200k tok/日（§6.3；高沿 38,628 tok 实测 N23）。
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from news_flash_dedup.recall.vector_space import validate_vector
from news_flash_dedup.recall.vector_store import VECTOR_FIELDS


# ---------- B-0 建档 ----------

def b0_establish(milvus: Any, es: Any, config: Any, *,
                 store_factory: Callable[..., Any]) -> dict:
    """B-0 建档：真 space 集合 + 当日分区 + ES 双索引；schema describe 对拍。

    store_factory = RealMilvusP19Store（注入以便单元层 fake 对拍）。
    返回 {"store", "schema"}：schema 对拍报告（八字段/dim/隔离尾/分区/双索引）。
    """
    store = store_factory(milvus, es, config)
    description = milvus.describe_collection(collection_name=config.collection_name)
    fields = description.get("fields") if isinstance(description, Mapping) else None
    if not isinstance(fields, list):
        raise ValueError("collection schema description is unknown")
    names = {f["name"] for f in fields}
    dims = {f.get("params", {}).get("dim") for f in fields
            if f.get("name") == "embedding"}
    partition = "bd_" + config.business_date.isoformat().replace("-", "")
    schema = {
        "collection": config.collection_name,
        "fields_exact": names == set(VECTOR_FIELDS),
        "dimension": next(iter(dims)) if len(dims) == 1 else None,
        "dimension_matches_space": dims == {config.space.dimension},
        "endswith_space_id": config.collection_name.endswith(config.space.space_id),
        "partition_present": partition in milvus.list_partitions(
            collection_name=config.collection_name),
        "items_index_present": bool(es.indices.exists(index=config.items_index)),
        "control_index_present": bool(es.indices.exists(index=config.control_index)),
    }
    schema["pass"] = all([
        schema["fields_exact"], schema["dimension_matches_space"],
        schema["endswith_space_id"], schema["partition_present"],
        schema["items_index_present"], schema["control_index_present"],
    ])
    return {"store": store, "schema": schema}


# ---------- B-1 金标回填 ----------

def b1_backfill(pipeline: Any, store: Any,
                records: Iterable[Mapping[str, Any]], *,
                write_main_record: Callable[[Mapping[str, Any]], None],
                sample_size: int = 20) -> dict:
    """B-1 回填：逐记录 主记录(pending) → pipeline.ingest_record → ready。

    records: {"record_id","text","scope_id","arrival_seq"} 映射迭代；
    write_main_record 回调由调用方承载 ES 直写（创建者职责，INV-6）。
    返回台账：记录数 / record 覆盖率（records_coverage，W-R3c 散项 b：原
    chunk_coverage 单位混算退役）/ chunk 写入原始计数 / 抽样向量有限值核对 /
    usage 增量 / 失败清单（诚实 failed，不跳过不掩盖；失败词表见码内注释）。
    """
    reports = []
    failed = []
    chunks_written = 0
    for record in records:
        rid = record["record_id"]
        try:
            write_main_record(record)
            report = pipeline.ingest_record(
                record_id=rid, text=record["text"],
                scope_id=record["scope_id"], arrival_seq=record["arrival_seq"])
        except (RuntimeError, ValueError, OSError) as error:
            # 诚实 failed + 台账（INV-1 不跳过不掩盖）。
            # W-R3c 散项 a（外审低危）：宽 except 收窄——词表 =
            # RuntimeError/ValueError/OSError，覆盖领域失败全族
            # （P19VectorError/EmbeddingApiError/VectorWriteUnknown 皆
            # RuntimeError；VectorStateTransitionError/VectorIdentityConflict 皆
            # ValueError）+ IO 失败；write_main_record 回调（INV-6 创建者职责）
            # 的运行期失败须以该词表面呈。编程缺陷（TypeError/AttributeError/
            # KeyError 等）与记录契约违规不入台账——传播不掩盖。
            failed.append({"record_id": rid,
                           "error": f"{type(error).__name__}: {error}"})
            continue
        reports.append(report)
        chunks_written += len(report.outcomes)
    # 抽样向量有限值核对（validate_vector 全件校验）
    samples = reports[:sample_size]
    finite_ok = 0
    for report in samples:
        for vector_id, _outcome in report.outcomes:
            row = store.get_vector_row(vector_id)
            if row is None:
                continue
            validate_vector(row["embedding"], pipeline._space)
            if all(math.isfinite(v) for v in row["embedding"]):
                finite_ok += 1
    sampled = sum(len(r.outcomes) for r in samples)
    return {
        "records_total": len(reports) + len(failed),
        "records_ready": len(reports),
        "records_failed": len(failed),
        "failed": failed,
        "chunks_written": chunks_written,
        # W-R3c 散项 b（外审低危）：coverage 单位混算修正——原 chunk_coverage
        # 分母 chunks + len(failed) 以 chunk 数加记录数（单位混算，且失败记录
        # 的 chunk 期望数在 ingest 前不可得，chunk 分母不可诚实构造）；改
        # record 口径 records_coverage（名实相符），混算键退役，
        # chunks_written 原始计数保留。
        "records_coverage": len(reports) / max(1, len(reports) + len(failed)),
        "sample_vectors_checked": sampled,
        "sample_vectors_finite_ok": finite_ok,
        "sample_finite_all_pass": finite_ok == sampled,
        "reports": reports,
    }


# ---------- B-2 对拍（证据独立复算） ----------

def b2_verify_gates(evidence: Mapping[str, Any] | str | Path) -> dict:
    """B-2 对拍：shadow 证据 JSON 四闸独立复算（不依赖 harness 自报结论）。

    闸 1 逐字 recall@10=100%（自报字段汇总核验，逐对工件不在证据契约内——
    名实相符挂 _summary 尾，N3-05）；闸 2 释义 recall@30=1.0000（263/263
    N23 锚；从 missed_paraphrase+语料总数重算并交叉核验自报，矛盾即拒）；
    闸 3 fp=0（候选层不判重）实算化（N3-05）：硬负例登记工件重算 fp 口径——
    硬负例在场（fp 验证非真空）+ 候选命中占比合法且与总数自洽
    （hits=ratio×total 必为整数计数）+ 候选层判重结论零在场；
    闸 4 孤儿/孔洞登记面在案且形态合法。
    """
    if isinstance(evidence, (str, Path)):
        evidence = json.loads(Path(evidence).read_text(encoding="utf-8"))
    leg_b = evidence["leg_b_real"]
    corpus = evidence["corpus"]
    missed = evidence["missed_paraphrase"]
    paraphrase_total = corpus["paraphrase_pairs"]
    recomputed_recall30 = ((paraphrase_total - len(missed)) / paraphrase_total
                           if paraphrase_total else 0.0)
    # 闸 3 fp 口径实算（工件 = 硬负例登记面）：占比自报与总数重算咬合，
    # 缺工件/占比越界/计数不自洽/判重结论在场皆 fail-closed（旧常量 True
    # 零咬合退役）。
    hard_total = corpus.get("hard_negative_pairs")
    hn_ratio = evidence.get("hard_negative_candidate_ratio")
    hn_verdicts = evidence.get("candidate_layer_duplicate_verdicts")
    gate3_fp0 = (
        type(hard_total) is int and hard_total > 0
        and type(hn_ratio) in (int, float) and math.isfinite(hn_ratio)
        and 0.0 <= hn_ratio <= 1.0
        and abs(hn_ratio * hard_total - round(hn_ratio * hard_total)) < 1e-9
        and not hn_verdicts
    )
    checks = {
        "gate1_verbatim_recall@10_summary": (
            leg_b["recall@10_verbatim"] == 1.0 and corpus["verbatim_pairs"] > 0),
        "gate2_paraphrase_recall@30": (
            paraphrase_total == 263
            and not missed
            and recomputed_recall30 == 1.0
            and leg_b["recall@30_paraphrase"] == recomputed_recall30),
        "gate3_fp0_candidate_layer": gate3_fp0,
        "gate4_holes_registered": isinstance(evidence.get("holes"), list),
        "warm_run_consistent_summary": (
            evidence["warm_run"]["recall_equal"] is True
            and evidence["warm_run"]["api_calls_delta"] == 0),
    }
    return {"pass": all(checks.values()), "checks": checks}


# ---------- B-3 签收（R191 冻结版：只读，零删除） ----------

def b3_signoff_inventory(store: Any, milvus: Any, es: Any, *,
                         baseline_collections: Sequence[str],
                         baseline_indices: Sequence[str]) -> dict:
    """B-3 只读签收（R191 删除冻结：本 run 对象原地封存，删除归用户亲决）。

    核验：本 run 对象在场 + 「新 run 对象之外零变化」——跑前快照（含封存物
    新基线）与跑后名单 diff：任何 missing = 封存物/保留对象被删改（违规）；
    新增仅限本 run 白名单前缀。全程只读（list/cat），零写零删。
    """
    prefix_collection = f"p19_{store.config.run_uuid}_"
    prefix_index = f"p19-batch-{store.config.run_uuid}-"
    cols_after = sorted(milvus.list_collections())
    idx_after = sorted(item.get("index", "")
                       for item in es.cat.indices(format="json", h="index"))
    own_collections = sorted(c for c in cols_after
                             if c.startswith(prefix_collection))
    own_indices = sorted(i for i in idx_after if i.startswith(prefix_index))
    added_c = sorted(set(cols_after) - set(baseline_collections))
    added_i = sorted(set(idx_after) - set(baseline_indices))
    diff = {
        "collections_missing": sorted(set(baseline_collections) - set(cols_after)),
        "indices_missing": sorted(set(baseline_indices) - set(idx_after)),
        "collections_added_foreign": sorted(set(added_c) - set(own_collections)),
        "indices_added_foreign": sorted(set(added_i) - set(own_indices)),
    }
    return {"sealed": {"collections": own_collections, "indices": own_indices},
            "diff": diff,
            "own_present": bool(own_collections) and len(own_indices) == 2,
            "drift_free": not any(diff.values())}


__all__ = [
    "b0_establish",
    "b1_backfill",
    "b2_verify_gates",
    "b3_signoff_inventory",
]
