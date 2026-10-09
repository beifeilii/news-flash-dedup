"""P16-E DecideOutcome DTO + 公共 dict 序列化。

按 07 §5 / 11 §1：公共结果恰为 `item_id/text/decision/duplicate_ids/reason`
五字段封闭。`to_public_dict()` 不暴露 internal_code / pair_codes /
used_evidence / raw_hash 等内部字段。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any


_DECISION = "重复", "不重复", "边界case/疑难case"
# 提交一（2026-10-10，p3-semantic-authority，§5.1）：判官语义权威三码
# 入白名单（JUDGE_EQUIVALENT/JUDGE_NON_DUPLICATE/JUDGE_UNCERTAIN）——
# 与 compare/pair_compare.py 三码集合同源同步；reason 泄漏扫描闸同步覆盖。
_EQUIVALENT_CODES = {"EXACT_TEXT_MATCH", "LOSSLESS_TEXT_MATCH", "FACT_EQUIVALENT",
                     "JUDGE_EQUIVALENT"}
_CONFLICT_CODES = {"VERIFIED_CONFLICT", "JUDGE_NON_DUPLICATE"}
_UNRESOLVED_CODES = {
    "SUBJECT_UNRESOLVED", "DEPENDENCY_TIMEOUT", "RECALL_INCOMPLETE",
    "CANDIDATE_BUDGET_EXHAUSTED", "EXTRACTION_FAILED", "EVIDENCE_INVALID",
    "NUMERIC_ALIGNMENT_FAILED", "TIME_RELATION_UNCERTAIN",
    "RULE_UNCOVERED", "FACT_INCOMPLETE", "NO_DUPLICATE_FOUND",
    "JUDGE_UNCERTAIN",
}


@dataclass(frozen=True)
class DecideOutcome:
    item_id: str
    text: str
    decision: str
    duplicate_ids: tuple[str, ...]
    reason: str
    internal_code: str
    pair_codes: Mapping[str, str] = field(default_factory=dict)
    used_evidence: tuple[Any, ...] = field(default_factory=tuple)
    unresolved_fields: tuple[str, ...] = field(default_factory=tuple)
    raw_hash: str = ""
    pipeline_version: str = "dedup_v1"
    # R9 外部审核 F4 修复（主窗口 07:0x）：内部字段——集合级决策的全部对级
    # PairResult，供 commit_one 构造真实审计批次（build_audit_batch）；不进入
    # to_public_dict（五字段封闭不动）。
    pair_results: tuple[Any, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if self.decision not in _DECISION:
            raise ValueError(f"decision {self.decision!r} not in {_DECISION}")
        if not self.item_id or not self.text:
            raise ValueError("item_id/text must be nonempty")
        if not self.reason:
            raise ValueError("reason must be nonempty")
        if (self.decision == "重复") != bool(self.duplicate_ids):
            raise ValueError(
                f"decision={self.decision} but duplicate_ids is {self.duplicate_ids!r}"
            )
        # W2Fα2-8①：原 L55-59 第二基数分支（decision∈{不重复,边界} 且
        # duplicate_ids 非空 → raise）为不可达死分支——该形态必先触发上一
        # 基数门（False != True → raise），死分支拆除不松闸（红测
        # test_winw2fa2_hygiene.py 行为守卫在案）。
        if self.internal_code not in (_EQUIVALENT_CODES | _CONFLICT_CODES
                                       | _UNRESOLVED_CODES):
            raise ValueError(
                f"internal_code {self.internal_code!r} not in whitelist"
            )
        for internal in (_EQUIVALENT_CODES | _CONFLICT_CODES
                         | _UNRESOLVED_CODES - {"NO_DUPLICATE_FOUND"}):
            if internal in self.reason:
                raise ValueError(
                    f"public reason must not contain internal code {internal!r}"
                )

    def to_public_dict(self) -> dict:
        """按 07 §5 / 11 §1 输出恰五字段封闭；不暴露内部字段。"""
        return {
            "item_id": self.item_id,
            "text": self.text,
            "decision": self.decision,
            "duplicate_ids": list(self.duplicate_ids),
            "reason": self.reason,
        }


__all__ = ["DecideOutcome"]