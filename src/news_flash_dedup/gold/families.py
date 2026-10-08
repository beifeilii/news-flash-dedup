"""按材料线索建立防泄漏家族，关系不具备正标签含义。"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from hashlib import sha256

from .materials import MaterialCatalog, MaterialRow


# 12 §4.3 指名的第一批跨组待审线索；仅连接隔离家族，不产生标签。
FIRST_BATCH_CROSS_GROUP_LEADS = (
    ("25", "27"),
    ("301", "303"),
    ("375", "376"),
    ("292", "294"),
    ("306", "307"),
)


@dataclass(frozen=True)
class IsolationRisk:
    kind: str
    negative_row: int
    negative_row_key: str
    counterpart_rows: tuple[str, ...]


@dataclass(frozen=True)
class IsolationEdge:
    left_row_key: str
    right_row_key: str
    reason: str


@dataclass(frozen=True)
class FamilyGraph:
    family_by_row: dict[str, str]
    members: dict[str, tuple[str, ...]]
    edges: tuple[IsolationEdge, ...]
    risks: tuple[IsolationRisk, ...]


def build_families(catalog: MaterialCatalog) -> FamilyGraph:
    """同组、同 ID、同正文及负对两端只用于同 split 隔离。"""
    parent = {row.row_key: row.row_key for row in catalog.rows}
    edges: set[IsolationEdge] = set()

    def find(key: str) -> str:
        while parent[key] != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key

    def connect(left: str, right: str, reason: str) -> None:
        if left != right:
            edges.add(IsolationEdge(*sorted((left, right)), reason))
        a, b = find(left), find(right)
        if a != b:
            parent[max(a, b)] = min(a, b)

    grouped: dict[tuple[str, str, str], str] = {}
    by_id: dict[str, str] = {}
    by_body: dict[str, str] = {}
    for row in catalog.rows:
        if row.legacy_group is not None and row.side == "single":
            group_key = (row.material_id, row.sheet_name, row.legacy_group)
            if group_key in grouped:
                connect(row.row_key, grouped[group_key], "historical_group")
            else:
                grouped[group_key] = row.row_key
        if row.raw_id in by_id:
            connect(row.row_key, by_id[row.raw_id], "same_id")
        else:
            by_id[row.raw_id] = row.row_key
        if row.raw_text in by_body:
            connect(row.row_key, by_body[row.raw_text], "exact_body")
        else:
            by_body[row.raw_text] = row.row_key

    for material in catalog.materials:
        for left_group, right_group in FIRST_BATCH_CROSS_GROUP_LEADS:
            left = grouped.get((material.material_id, "重复", left_group))
            right = grouped.get((material.material_id, "重复", right_group))
            if left is not None and right is not None:
                connect(left, right, "documented_cross_group_lead")

    for pair in catalog.negative_pairs:
        connect(pair.a_row_key, pair.b_row_key, "negative_pair")

    positives_by_id: dict[str, list[MaterialRow]] = defaultdict(list)
    positives_by_body: dict[str, list[MaterialRow]] = defaultdict(list)
    for row in catalog.rows:
        if row.side == "single":
            positives_by_id[row.raw_id].append(row)
            positives_by_body[row.raw_text].append(row)

    row_by_key = {row.row_key: row for row in catalog.rows}
    risks: list[IsolationRisk] = []
    for pair in catalog.negative_pairs:
        for key in (pair.a_row_key, pair.b_row_key):
            endpoint = row_by_key[key]
            id_conflicts = sorted(
                row.row_key for row in positives_by_id[endpoint.raw_id]
                if row.raw_text != endpoint.raw_text
            )
            if id_conflicts:
                risks.append(IsolationRisk(
                    "id_body_conflict", pair.excel_row, key, tuple(id_conflicts),
                ))
            body_aliases = sorted(
                row.row_key for row in positives_by_body[endpoint.raw_text]
                if row.raw_id != endpoint.raw_id
            )
            if body_aliases:
                risks.append(IsolationRisk(
                    "body_id_alias", pair.excel_row, key, tuple(body_aliases),
                ))

    components: dict[str, list[str]] = defaultdict(list)
    for key in parent:
        components[find(key)].append(key)
    members: dict[str, tuple[str, ...]] = {}
    family_by_row: dict[str, str] = {}
    for component in components.values():
        keys = tuple(sorted(component))
        family_id = sha256("\n".join(keys).encode("utf-8")).hexdigest()
        members[family_id] = keys
        family_by_row.update((key, family_id) for key in keys)
    return FamilyGraph(
        family_by_row=family_by_row,
        members=dict(sorted(members.items())),
        edges=tuple(sorted(edges, key=lambda edge: (edge.left_row_key, edge.right_row_key, edge.reason))),
        risks=tuple(sorted(risks, key=lambda risk: (risk.negative_row_key, risk.kind))),
    )


def assign_splits(graph: FamilyGraph, *, seed: str) -> dict[str, str]:
    """家族级稳定哈希桶；60/20/20 是目标概率而非强制数量。"""
    assignment: dict[str, str] = {}
    for family_id in graph.members:
        digest = sha256(f"{seed}:{family_id}".encode("utf-8")).digest()
        bucket = int.from_bytes(digest[:8], "big") / (1 << 64)
        assignment[family_id] = (
            "dev" if bucket < 0.6 else "validation" if bucket < 0.8 else "test"
        )
    return assignment
