"""P17 最小审计写入器（P17 03:57 修订 §9 方案 ①）。

P17 自带 `decide_pair_audit` 写入器，P18 实施时接管。交接点写明在
`log/P18-执行证据.md`。本模块不连真实 ES——纯数据结构 + 序列化。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable  # W2Fα2-8③：Mapping 死导入清扫
from dataclasses import asdict, dataclass, field
from typing import Any

from news_flash_dedup.compare.pair_compare import PairResult


@dataclass(frozen=True)
class AuditRecord:
    """单条对级审计文档（P17 自带，待 P18 接管）。"""
    comparison_id: str
    history_record_id: str
    current_record_id: str
    history_raw_hash: str
    current_raw_hash: str
    basis: str
    field_path: str
    detail: str
    history_evidence: dict
    current_evidence: dict
    pipeline_version: str
    # 提交一修复（2026-10-10 整改令四）：判官证据诊断结构化持久化——
    # 必须进持久化审计文档（普通日志可保留但不是唯一载体）；绝不进公共
    # 五字段；不并入 payload_hash 规范化集（既有审计哈希稳定性不动）。
    # full_text_fallback_used=True=走了完整原文回退证据，显式降质标记，
    # 不与精确引文同质量级。判官未参与的对保持默认（pass/空/False/""）。
    evidence_status: str = "pass"
    evidence_warnings: tuple = ()
    machine_findings: tuple = ()
    full_text_fallback_used: bool = False
    judge_decision_mode: str = ""
    payload_hash: str = field(default="")
    # 终审 P1-2（2026-10-10）：diagnostics_hash——判官证据诊断五字段
    # （evidence_status/evidence_warnings/machine_findings/
    # full_text_fallback_used/judge_decision_mode）的规范化哈希（定序
    # 规范化：warnings/findings 排序 + JSON canonical 序列化）。诊断
    # 面内容指纹：五字段任一变化即变。payload_hash 原样保留不动（历史
    # 兼容——诊断字段不进 payload 规范化集，既有审计重放一致性不破）。
    diagnostics_hash: str = field(default="")

    def __post_init__(self) -> None:
        if not self.comparison_id:
            raise ValueError("comparison_id must be nonempty")
        if not self.history_record_id or not self.current_record_id:
            raise ValueError("history/current record_id must be nonempty")
        if self.history_record_id == self.current_record_id:
            raise ValueError("self-audit forbidden")
        if not self.payload_hash:
            object.__setattr__(self, "payload_hash", _compute_payload_hash(self))
        if not self.diagnostics_hash:
            object.__setattr__(self, "diagnostics_hash",
                               _compute_diagnostics_hash(self))

    def to_doc(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class AuditBatch:
    """一批审计文档（一次 decide_for_task 的全部对级结果）。

    W2Fα2-8②：created_at 死字段拆除（全 src/tests 零消费方，恒值默认
    "2026-09-26" 从不透传——真 created_at 由 persist/ 侧各自 enrichment）。
    W2Fα2-5（WA1b-L1，探针 winw2fa2-probe-index-prefix.json 裁定=接线）：
    index_prefix 入字段——修复前 build_audit_batch 实传值被静默丢弃
    （coordinator 实传 "p17-fake-audits-v1" 从不成为任何真实索引名；
    审计批实际落点由仓储配置侧决定，不落错索引，故不升中危）。
    """
    records: tuple[AuditRecord, ...]
    audit_complete: bool
    index_prefix: str = "p17-batch-decide-audits-v1"

    def to_index_actions(self, index_prefix: str | None = None) -> list[dict]:
        """生成 ES bulk 写入 actions（不连真 ES；上层负责执行）。

        W2Fα2-5 接线：缺省用批次自带 index_prefix；显式实参仍优先
        （现役测试契约不破）。
        """
        prefix = self.index_prefix if index_prefix is None else index_prefix
        actions: list[dict] = []
        for record in self.records:
            actions.append({
                "index": {
                    "_index": f"{prefix}",
                    "_id": record.comparison_id,
                }
            })
            actions.append(record.to_doc())
        return actions


def _compute_payload_hash(record: AuditRecord) -> str:
    canonical = {
        "history_record_id": record.history_record_id,
        "current_record_id": record.current_record_id,
        "history_raw_hash": record.history_raw_hash,
        "current_raw_hash": record.current_raw_hash,
        "basis": record.basis,
        "field_path": record.field_path,
        "detail": record.detail,
        "history_evidence": record.history_evidence,
        "current_evidence": record.current_evidence,
        "pipeline_version": record.pipeline_version,
    }
    encoded = json.dumps(canonical, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _compute_diagnostics_hash(record: AuditRecord) -> str:
    """终审 P1-2：判官证据诊断五字段规范化哈希。

    规范化口径：evidence_warnings/machine_findings 排序（集合语义，
    与双序并表顺序解耦）+ JSON canonical 序列化（sort_keys +
    ensure_ascii=False，仓内现役习语）。五字段任一变化 → hash 变；
    与 payload_hash 完全独立（诊断不进 payload 规范化集）。
    """
    canonical = {
        "evidence_status": record.evidence_status,
        "evidence_warnings": sorted(record.evidence_warnings),
        "machine_findings": sorted(record.machine_findings),
        "full_text_fallback_used": record.full_text_fallback_used,
        "judge_decision_mode": record.judge_decision_mode,
    }
    encoded = json.dumps(canonical, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _comparison_id(pair: PairResult) -> str:
    """W2Fα2-46（WB4 F-2，高危契约硬冲突）：comparison_id 对齐规格公式。

    契约 10 §3 L48 逐字（W3F W3b-F1 锚勘正：原注"L43"系四级载体连锁
    错锚，10 L43 实为 record_id 公式行）：
        comparison_id = SHA256(JCS([query_record_id, candidate_record_id,
                                    pipeline_version]))
    方向（10 §3 L55 明文义，述不引：comparison_id 有方向，query 为当前
    新稿、candidate 为历史候选——W3F W3b-F1 勘正：原注"L48'A为历史
    候选、B为当前新稿'"系字面编造引语，10 号文无此文；与
    api/contract.py validate_result L258-261/267 的 expected_audit_id
    计算逐字同式）：
        query_record_id = 当前新稿（pair.current_record_id）
        candidate_record_id = 历史候选（pair.history_record_id）
    JCS 采用仓内现役习语（json.dumps ensure_ascii=False + separators
    (",", ":") 后 UTF-8——字符串数组场景与 RFC8785 逐字节一致，同
    api/contract.py/admission._digest 口径）。修复前为
    "history:current" 裸拼接，过不了 validate_result 对拍。
    """
    return hashlib.sha256(json.dumps(
        [pair.current_record_id, pair.history_record_id, pair.pipeline_version],
        ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


def _winning_evidence(pair: PairResult, record_id: str):
    """W2Fα2-4（WA1b-M1）：从 PairResult 实胜证据选该侧代表证据。

    实胜优先：verified_conflicts 双侧证据是 conflict 对的签发基底（detail
    明文"已验证至少一条充分冲突：{conflicts[0].field_path}"），先于对齐事实
    证据；无冲突时回退 used_evidence 顺序首条（equivalent/unresolved 对）。
    同侧多条取首条（确定性）。无证据 → None（诚实空壳，不伪造）。
    """
    for conflict in pair.verified_conflicts:
        for ev in (conflict.history_evidence, conflict.current_evidence):
            if ev.record_id == record_id:
                return ev
    for ev in pair.used_evidence:
        if ev.record_id == record_id:
            return ev
    return None


def _evidence_doc(record_id: str, field_path: str, winning) -> dict:
    """组装审计证据段：quote/start/end 从实胜证据实填（W2Fα2-4）。

    W-R3c 散项 f（外审低危）：field 键守卫——修复前恒取对级 field_path
    （build_audit_batch 路径恒为默认空串），实胜证据在场时 field 亦可空，
    证据段自相矛盾（quote/start/end 实填而 field 空）。守卫：winning 在场
    取证据自携 field（证据自带权威，value_time.py:126-131 EvidenceRef）；
    证据 field 退化空串回退 field_path；无证据诚实空壳形态不动。
    """
    doc = {
        "record_id": record_id,
        "field": field_path,
        "quote": "",
        "start": 0,
        "end": 0,
    }
    if winning is not None:
        doc["field"] = winning.field or field_path
        doc["quote"] = winning.quote
        doc["start"] = winning.start
        doc["end"] = winning.end
    return doc


def build_audit_record(
    pair: PairResult,
    *,
    field_path: str = "",
    basis: str | None = None,
    detail: str = "",
) -> AuditRecord:
    """从 PairResult 构造一条审计文档。

    比较 ID（W2Fα2-46）：契约公式 SHA256(JCS([query=current,
    candidate=history, pipeline_version]))，见 _comparison_id；payload_hash
    自计算保证重放一致性（同 ID 异内容 → 一致性错误）。

    证据段（W2Fα2-4，WA1b-M1）：quote/start/end 从 PairResult 实胜证据
    实填（第三方 L44 族恒空壳残余修复），来源选择与无证据兜底见
    _winning_evidence / _evidence_doc。

    basis 缺省（None）按对级结果实传 `pair.code`（R2-H4 外部三审，窗口Z1
    规格还原：conflict → VERIFIED_CONFLICT、unresolved → 该对实际未决码、
    equivalent → 证书码；修复前恒取默认值 FACT_EQUIVALENT——任意结果洗成
    等价签发基底，审计失真且 payload_hash 按伪 basis 固化）；显式传入仍
    优先（现役覆盖契约保留）。payload_hash 由 AuditRecord.__post_init__
    按真 basis 自计算固化。
    """
    history_evidence = _evidence_doc(
        pair.history_record_id, field_path,
        _winning_evidence(pair, pair.history_record_id))
    current_evidence = _evidence_doc(
        pair.current_record_id, field_path,
        _winning_evidence(pair, pair.current_record_id))
    return AuditRecord(
        comparison_id=_comparison_id(pair),
        history_record_id=pair.history_record_id,
        current_record_id=pair.current_record_id,
        history_raw_hash=pair.history_raw_hash,
        current_raw_hash=pair.current_raw_hash,
        basis=pair.code if basis is None else basis,  # R2-H4：见 docstring
        field_path=field_path,
        detail=detail or pair.detail,
        history_evidence=history_evidence,
        current_evidence=current_evidence,
        pipeline_version=pair.pipeline_version,
        # 提交一修复（整改令四）：对级判官诊断结构化入审计文档
        # （getattr 兜底：手工构造的对级夹具无新字段时保持默认）。
        evidence_status=getattr(pair, "evidence_status", "pass"),
        evidence_warnings=tuple(getattr(pair, "evidence_warnings", ())),
        machine_findings=tuple(getattr(pair, "machine_findings", ())),
        full_text_fallback_used=bool(
            getattr(pair, "full_text_fallback_used", False)),
        judge_decision_mode=getattr(pair, "judge_decision_mode", ""),
    )


def build_audit_batch(pairs: Iterable[PairResult], *,
                        index_prefix: str = "p17-batch-decide-audits-v1",
                        audit_complete: bool,  # 窗口V（M-01 同标准）：去默认强制关键字显式表态
                        ) -> AuditBatch:
    """从一批 PairResult 构造 AuditBatch（决定层一次性审计）。

    W2Fα2-5：index_prefix 接线入 AuditBatch（修复前静默丢弃，探针
    winw2fa2-probe-index-prefix.json 在案）。W2Fα2-8②：created_at
    恒值默认死形参拆除（零消费方）。
    """
    records = tuple(build_audit_record(pair) for pair in pairs)
    return AuditBatch(records=records, audit_complete=audit_complete,
                      index_prefix=index_prefix)


__all__ = [
    "AuditBatch",
    "AuditRecord",
    "build_audit_batch",
    "build_audit_record",
]