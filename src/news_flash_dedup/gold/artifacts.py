"""只输出材料摘要及待人工填写的无正文模板。"""

from __future__ import annotations

import csv
from collections import Counter
from hashlib import sha256
from io import StringIO
from pathlib import Path
from typing import Iterable

from .families import FamilyGraph, assign_splits
from .materials import MaterialCatalog


def build_manifest(
    catalog: MaterialCatalog,
    graph: FamilyGraph,
    splits: dict[str, str],
    *,
    split_seed: str,
) -> dict:
    """生成可对账摘要；正文、原始 ID、旧标签和人工标签均不落入输出。"""
    if set(splits) != set(graph.members):
        raise ValueError("split assignment must cover every family exactly once")
    if set(graph.family_by_row) != {row.row_key for row in catalog.rows}:
        raise ValueError("family graph must cover every material row")
    if not split_seed or splits != assign_splits(graph, seed=split_seed):
        raise ValueError("split assignment must match its recorded seed")

    family_counts = Counter(splits.values())
    edge_counts = Counter(edge.reason for edge in graph.edges)
    row_counts = Counter(
        splits[graph.family_by_row[row.row_key]] for row in catalog.rows
    )
    return {
        "schema_version": "1",
        "split_seed": split_seed,
        "materials": [
            {
                "material_id": material.material_id,
                "byte_size": material.byte_size,
                "aliases": sorted({Path(path).name for path in material.aliases}),
                "sheet_counts": dict(material.sheet_counts),
            }
            for material in catalog.materials
        ],
        "rows": [
            {"row_key": row.row_key, "raw_hash": row.raw_hash, "hidden": row.hidden}
            for row in sorted(catalog.rows, key=lambda item: item.row_key)
        ],
        "totals": {
            "material_count": len(catalog.materials),
            "row_records": len(catalog.rows),
            "negative_pair_rows": len(catalog.negative_pairs),
            "hidden_row_nodes": sum(row.hidden for row in catalog.rows),
            "families": len(graph.members),
            "isolation_risks": len(graph.risks),
            "split_families": dict(sorted(family_counts.items())),
            "split_rows": dict(sorted(row_counts.items())),
        },
        "edge_counts": dict(sorted(edge_counts.items())),
        "edges": [
            {
                "left_row_key": edge.left_row_key,
                "right_row_key": edge.right_row_key,
                "reason": edge.reason,
            }
            for edge in graph.edges
        ],
        "families": [
            {"family_id": family_id, "split": splits[family_id], "member_row_keys": list(members)}
            for family_id, members in graph.members.items()
        ],
        "isolation_risks": [
            {
                "kind": risk.kind,
                "negative_row_key": risk.negative_row_key,
                "counterpart_rows": list(risk.counterpart_rows),
            }
            for risk in graph.risks
        ],
    }


def blank_annotation_csv(
    catalog: MaterialCatalog,
    pairs: Iterable[tuple[str, str]],
    *,
    rule_version: str,
) -> str:
    """显式文本对的双标空表；调用方决定是否在授权位置保存。"""
    if not rule_version.strip():
        raise ValueError("rule_version is required")
    row_keys = {row.row_key for row in catalog.rows}
    fieldnames = (
        "pair_id", "annotator_slot", "a_row_key", "b_row_key",
        "rule_version", "annotator_id", "decision", "reason",
        "aligned_fact_ids", "verified_conflicts", "unresolved_fields",
        "coverage_complete", "evidence_a", "evidence_b", "annotation_status",
    )
    buffer = StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=fieldnames, lineterminator="\n")
    writer.writeheader()
    seen: set[tuple[str, str]] = set()
    for a_row_key, b_row_key in pairs:
        if a_row_key not in row_keys or b_row_key not in row_keys or a_row_key == b_row_key:
            raise ValueError("pair must reference two distinct material rows")
        pair = tuple(sorted((a_row_key, b_row_key)))
        if pair in seen:
            raise ValueError("pair appears more than once in annotation queue")
        seen.add(pair)
        pair_id = sha256("\n".join(pair).encode("utf-8")).hexdigest()
        for slot in ("1", "2"):
            writer.writerow({
                "pair_id": pair_id,
                "annotator_slot": slot,
                "a_row_key": a_row_key,
                "b_row_key": b_row_key,
                "rule_version": rule_version,
                "annotator_id": "",
                "decision": "",
                "reason": "",
                "aligned_fact_ids": "",
                "verified_conflicts": "",
                "unresolved_fields": "",
                "coverage_complete": "",
                "evidence_a": "",
                "evidence_b": "",
                "annotation_status": "pending",
            })
    return buffer.getvalue()
