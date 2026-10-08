"""P16-A 双侧 Evidence 对齐门。

按主体 / 事件谓词 / 关键对象三元组做确定性一对一指派；多对一仅限 09 §9.3 明确
"附属/独立"关系（basis=SUPPLEMENTARY_RELATION）。**禁止**任何相似度打分
（BM25 / 向量 / Jaccard / Hash 重叠率等）参与对齐——执法点=candidate_basis
黑白名单闸（_FORBIDDEN_BASIS / _ALIGNMENT_BASIS_WHITELIST；W2Fα1-E1 对码：
本门无相似度入口参数，旧述相似度入参拒绝声称无兑现对象已勘正，死常量
_SIMILARITY_INPUT_KEYS 退役）。

设计依据：`log/P14-16-Fact精判集成设计.md` §3.6（02:43 批复修订版）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from news_flash_dedup.facts import FactValidationReport

from .normalize_dict import NormalizationDictionary, NormalizationHit
from .value_time import EvidenceRef


_ALIGNMENT_VERSION = "alignment_v1"
_ALIGNMENT_BASIS_WHITELIST = frozenset({
    "EXACT_TEXT_MATCH",
    "LOSSLESS_TEXT_MATCH",
    "FACT_EQUIVALENT",
    "SUBJECT_DETERMINISTIC_ALIAS",
    "FACT_TYPE_SAME",
    "KEY_OBJECT_SAME",
    "EVENT_STATE_SAME",
    "TIME_SAME_ABSOLUTE",
    "TIME_SAME_RELATIVE",
    "NUMERIC_SAME_DECIMAL",
    "SUPPLEMENTARY_RELATION",
})
_FORBIDDEN_BASIS = frozenset({
    "MODEL_DECLARED",
    "HASH_MATCH",
    "BUCKET_HIT",
    "BM25_SCORE",
    "VECTOR_SCORE",
    "JACCARD",
    "OVERLAP_RATE",
})
_UNSET = object()


@dataclass(frozen=True)
class FactPointer:
    record_id: str
    fact_id: str
    slot_path: str
    text: str = field(repr=False)
    evidence: EvidenceRef = field(repr=False)
    subject_raw: str | None = None
    predicate_raw: str | None = None
    key_object_raw: str | None = None


@dataclass(frozen=True)
class AlignedPair:
    history: FactPointer
    current: FactPointer
    basis: str
    supplementary: bool = False
    normalization_hits: tuple[NormalizationHit, ...] = ()


@dataclass(frozen=True)
class PairAlignmentOutcome:
    aligned_facts: tuple[AlignedPair, ...]
    unresolved_fields: tuple[str, ...]
    code: str
    provenance: Mapping[str, str]


class PairAlignmentError(ValueError):
    """对齐门拒绝的非业务失败。"""


def _evidence_ref_from_dict(raw: Mapping[str, Any]) -> EvidenceRef:
    return EvidenceRef(
        record_id=str(raw["record_id"]),
        field=str(raw["field"]),
        quote=str(raw["quote"]),
        start=int(raw["start"]),
        end=int(raw["end"]),
    )


def _verify_evidence_against_text(article: Any, text: str, record_id: str) -> None:
    """工件中所有 evidence 必须回原文 record_id、字段路径、Unicode 码点半开区间及 quote。

    F-K1（窗口K 登记观察，主窗口裁定实施，窗口Z1 落地）：引文↔槽位
    raw_value 一致性——present 槽（status="present" 且 raw_value 为串）内
    **声称锚定本槽**的 evidence 条目（field 以槽路径末段结尾，如
    facts.f1.subject 之于 $.facts[0].subject），其 quote 必须含槽
    raw_value（与 value_time normalize_* 现役 raw∈quote 纪律同一口径），
    不一致即 PairAlignmentError（fail-closed）——H-06 型伪造锚点
    （raw="甲公司" 配 text[:2] 假引文）由此封死。不声称本槽的条目
    （恒等投影 field="text"、占位 field="x" 等）维持既有切片校验不追加
    ——全套件探针实证该形态 quote≠raw 全部 field="x"、canonical 字段零
    不一致（log/temp/winz1-fk1-probe.json），本作用域对既有套件零翻转。
    """
    artifact = getattr(article, "artifact", None) or {}
    if not isinstance(artifact, Mapping):
        raise PairAlignmentError("artifact is not a mapping")

    def walk(node: Any, path: str) -> None:
        if isinstance(node, Mapping):
            if "evidence" in node and isinstance(node["evidence"], list):
                slot_raw = node.get("raw_value") \
                    if node.get("status") == "present" else None
                slot_field_suffix = (
                    "." + path.rsplit(".", 1)[-1]
                    if isinstance(slot_raw, str) and slot_raw else None
                )
                for entry in node["evidence"]:
                    if not isinstance(entry, Mapping):
                        raise PairAlignmentError(f"{path}.evidence entry is not a mapping")
                    if entry.get("record_id") != record_id:
                        raise PairAlignmentError(
                            f"{path}.evidence record_id {entry.get('record_id')!r} "
                            f"does not match artifact record_id {record_id!r}"
                        )
                    quote = entry.get("quote")
                    start = entry.get("start")
                    end = entry.get("end")
                    field = entry.get("field")
                    if not isinstance(quote, str) or not isinstance(start, int) \
                            or not isinstance(end, int) or not isinstance(field, str):
                        raise PairAlignmentError(f"{path}.evidence has invalid types")
                    if not (0 <= start < end <= len(text)):
                        raise PairAlignmentError(
                            f"{path}.evidence code-point span [{start},{end}) "
                            f"outside text length {len(text)}"
                        )
                    if text[start:end] != quote:
                        raise PairAlignmentError(
                            f"{path}.evidence quote {quote!r} does not match "
                            f"text[{start}:{end}]={text[start:end]!r}"
                        )
                    # F-K1：声称锚定本槽的条目，引文必须含槽 raw_value（见上部
                    # docstring）；不声称本槽（field 不以槽路径末段结尾）不追加。
                    if slot_field_suffix is not None \
                            and field.endswith(slot_field_suffix) \
                            and slot_raw not in quote:
                        raise PairAlignmentError(
                            f"{path}.evidence quote {quote!r} does not contain "
                            f"slot raw_value {slot_raw!r} for claimed field "
                            f"{field!r}"
                        )
            for key, child in node.items():
                walk(child, f"{path}.{key}")
        elif isinstance(node, list):
            for index, child in enumerate(node):
                walk(child, f"{path}[{index}]")

    walk(artifact, "$")


def _slot_raw(slot: Mapping[str, Any]) -> str | None:
    if slot.get("status") != "present":
        return None
    value = slot.get("raw_value")
    return value if isinstance(value, str) and value else None


def _fact_pointers(report: FactValidationReport, text: str,
                   record_id: str) -> list[FactPointer]:
    artifact = report.artifact or {}
    facts = artifact.get("facts") or []
    pointers: list[FactPointer] = []
    for fact in facts:
        fact_id = str(fact.get("fact_id"))
        base = f"facts.{fact_id}"
        evidence_list = fact.get("evidence") or []
        if not evidence_list:
            raise PairAlignmentError(f"{base}.evidence is empty")
        head = evidence_list[0]
        pointer = FactPointer(
            record_id=record_id,
            fact_id=fact_id,
            slot_path=base,
            text=text,
            evidence=_evidence_ref_from_dict(head),
            subject_raw=_slot_raw(fact.get("subject", {})),
            predicate_raw=_slot_raw(fact.get("event_state", {}).get("predicate", {})),
            key_object_raw=_slot_raw(fact.get("key_object", {})),
        )
        pointers.append(pointer)
    pointers.sort(key=lambda item: (item.evidence.start, item.fact_id))
    return pointers


def _core_triple(pointer: FactPointer) -> tuple[str | None, str | None, str | None]:
    """主体/谓词/关键对象三元组；None 表示槽 missing。

    关键对象 None 不视为 disqualifier（与主体/谓词不同：后两者缺失会破坏核心
    事件，关键对象缺失仅是该 Fact 不指定对象）。
    """
    return (
        pointer.subject_raw,
        pointer.predicate_raw,
        pointer.key_object_raw,
    )


def _pointer_text_match(history: FactPointer, current: FactPointer) -> bool:
    """核心三元组原文码点定位且完全一致。

    主体/谓词任一为 None（即 P14 报 missing/uncertain）→ 不配对；
    关键对象两侧都 None 或同字符串 → 配对；任一侧是 "" 而另一侧非空 → 不配对。
    """
    h_triple = _core_triple(history)
    c_triple = _core_triple(current)
    if h_triple[0] is None or c_triple[0] is None:
        return False
    if h_triple[1] is None or c_triple[1] is None:
        return False
    if h_triple[0] != c_triple[0] or h_triple[1] != c_triple[1]:
        return False
    h_key = h_triple[2]
    c_key = c_triple[2]
    if h_key is None and c_key is None:
        return True
    if h_key is None or c_key is None:
        return False
    return h_key == c_key


def _dict_canonical(dictionary: NormalizationDictionary, slot: str,
                    raw: str) -> tuple[str, str | None]:
    """raw 经词典映射为 canonical；未命中词条时返回 (raw, None)。"""
    found = dictionary.normalize(slot, raw)
    if found is None:
        return raw, None
    return found[0], found[1]


def _pointer_dict_match(
    history: FactPointer,
    current: FactPointer,
    dictionary: NormalizationDictionary,
) -> tuple[NormalizationHit, ...] | None:
    """Tier-2 词典匹配（D19/S2 §3.1-3.2）。

    subject/predicate 分别经词典映射 canonical 后比较；key_object 维持
    双 None 或逐字等（一期不参与归一）。命中条件：
      - 双侧 canonical 相等，且至少一侧实际改写（raw 为词条 variant）；
      - **锚定规则**：subject/predicate 至少一个槽仍为逐字等（词典一期
        只允许补一个槽，双槽同归一属二期新裁定）。
    命中返回逐槽 NormalizationHit 元组，否则 None。
    """
    h_triple = _core_triple(history)
    c_triple = _core_triple(current)
    if h_triple[0] is None or c_triple[0] is None:
        return None
    if h_triple[1] is None or c_triple[1] is None:
        return None
    h_key = h_triple[2]
    c_key = c_triple[2]
    if (h_key is None) != (c_key is None):
        return None
    if h_key is not None and h_key != c_key:
        return None

    hits: list[NormalizationHit] = []
    anchored = False
    for slot, h_raw, c_raw in (
        ("subject", h_triple[0], c_triple[0]),
        ("predicate", h_triple[1], c_triple[1]),
    ):
        if h_raw == c_raw:
            anchored = True
            continue
        h_canonical, h_entry = _dict_canonical(dictionary, slot, h_raw)
        c_canonical, c_entry = _dict_canonical(dictionary, slot, c_raw)
        if h_canonical != c_canonical:
            return None
        if h_entry is None and c_entry is None:
            return None  # 双侧均未命中词条却 raw 不等——不可能（raw 不等已分支），防御
        hits.append(NormalizationHit(
            slot=slot,
            history_raw=h_raw,
            current_raw=c_raw,
            canonical=h_canonical,
            entry_id=h_entry if h_entry is not None else c_entry,
        ))
    if not hits:
        return None  # 全槽逐字等属 Tier-1，不应到达
    if not anchored:
        return None  # 锚定规则：双槽同归一，一期拒
    return tuple(hits)


def build_aligned(
    history_report: FactValidationReport,
    current_report: FactValidationReport,
    history_text: str,
    current_text: str,
    *,
    dictionary_version: str,
    dictionary_date: str | None = None,
    alignment_version: str = _ALIGNMENT_VERSION,
    candidate_basis: str | None = None,
    supplementary_pairs: tuple[tuple[str, str], ...] = (),
    dictionary: NormalizationDictionary | None = None,
) -> PairAlignmentOutcome:
    """按主体/谓词/关键对象确定性三元组配对，输出 AlignedPair 列表。

    参数：
        history_report / current_report：双侧 P14 `FactValidationReport`。
        history_text / current_text：双方原文（与 Evidence 码点核对）。
        dictionary_version / dictionary_date：版本化词典信息；写入 provenance。
        alignment_version：本门版本号，写入 provenance。
        candidate_basis：可选；用于覆盖默认 basis 选择（必须来自白名单）。
        supplementary_pairs：可选；指定 `(history_fact_id, current_fact_id)`
            元组列表，启用 09 §9.3 多对一的"附属/独立"关系，必须与
            `candidate_basis="SUPPLEMENTARY_RELATION"` 联用。
        dictionary：可选；`NormalizationDictionary`（D19/S2）。非 None 时启用
            Tier-2 词典匹配，且强制 `dictionary.version == dictionary_version`
            （及 date 核对）——声明版本与实际词典钉死，防静默漂移；None（默认）
            路径与既有行为逐字节一致。
    """
    if not isinstance(history_text, str) or not history_text:
        raise PairAlignmentError("history_text must be a nonempty string")
    if not isinstance(current_text, str) or not current_text:
        raise PairAlignmentError("current_text must be a nonempty string")
    if not isinstance(dictionary_version, str) or not dictionary_version:
        raise PairAlignmentError("dictionary_version must be nonempty")
    if not isinstance(alignment_version, str) or not alignment_version:
        raise PairAlignmentError("alignment_version must be nonempty")
    if candidate_basis is not None and candidate_basis in _FORBIDDEN_BASIS:
        raise PairAlignmentError(
            f"basis {candidate_basis!r} is forbidden (model/hash/similarity)"
        )
    if candidate_basis is not None and candidate_basis not in _ALIGNMENT_BASIS_WHITELIST:
        raise PairAlignmentError(
            f"basis {candidate_basis!r} is not in whitelist "
            f"{sorted(_ALIGNMENT_BASIS_WHITELIST)}"
        )
    supplementary_set = {tuple(pair) for pair in supplementary_pairs}
    if supplementary_set and candidate_basis != "SUPPLEMENTARY_RELATION":
        raise PairAlignmentError(
            "supplementary_pairs requires candidate_basis='SUPPLEMENTARY_RELATION'"
        )
    if dictionary is not None:
        if not isinstance(dictionary, NormalizationDictionary):
            raise PairAlignmentError("dictionary must be a NormalizationDictionary")
        if dictionary.version != dictionary_version:
            raise PairAlignmentError(
                f"dictionary version mismatch: declared {dictionary_version!r} "
                f"but loaded dictionary is {dictionary.version!r}"
            )
        if dictionary_date is not None and dictionary.date != dictionary_date:
            raise PairAlignmentError(
                f"dictionary date mismatch: declared {dictionary_date!r} "
                f"but loaded dictionary is {dictionary.date!r}"
            )

    history_id = getattr(history_report, "record_id", None)
    current_id = getattr(current_report, "record_id", None)
    if not isinstance(history_id, str) or not isinstance(current_id, str):
        raise PairAlignmentError("history/current record_id must be a string")
    if history_id == current_id:
        raise PairAlignmentError("history and current record_id must differ")

    _verify_evidence_against_text(history_report, history_text, history_id)
    _verify_evidence_against_text(current_report, current_text, current_id)

    history_pointers = _fact_pointers(history_report, history_text, history_id)
    current_pointers = _fact_pointers(current_report, current_text, current_id)
    if not history_pointers or not current_pointers:
        raise PairAlignmentError("history/current reports must contain at least one Fact")

    aligned: list[AlignedPair] = []
    unmatched_history = set(history_pointers)
    unmatched_current = set(current_pointers)
    supplementary_used = False
    # M-04(a)（外审+四轮，窗口J）：两遍扫描——先全量 Tier-1 逐字三元组配对，
    # 再对剩余未匹配 history 跑 Tier-2 词典。原交错结构中先处理 history 的
    # Tier-2 命中会提前 discard current，抢占后序 history 的 Tier-1 逐字
    # 配对（红实证：tests/unit/test_winj_decision_repairs.py
    # test_m04_tier1_verbatim_not_preempted_by_tier2）。dictionary=None
    # 路径第二遍不执行，行为与旧结构逐字节一致。
    for history in history_pointers:
        for current in current_pointers:
            if current not in unmatched_current:
                continue
            if not _pointer_text_match(history, current):
                continue
            pair_id = (history.fact_id, current.fact_id)
            if pair_id in supplementary_set:
                basis = "SUPPLEMENTARY_RELATION"
                supplementary_used = True
            elif candidate_basis == "SUPPLEMENTARY_RELATION":
                basis = "SUPPLEMENTARY_RELATION"
                supplementary_used = True
            else:
                basis = "FACT_EQUIVALENT"
            # W2Fα1（WA1a-E7）：supplementary 逐对标志化——修复前粘滞
            # （supplementary_used 一旦置 True，后序普通 FACT_EQUIVALENT 对
            # 也携带 supplementary=True；潜在缺陷被 L351 守卫掩盖：清单非空
            # 强制全局 basis → 可达配置下全对恒 SUPPLEMENTARY，公共 API
            # 不可达；行为中性卫生修，可达配置语义不变）。
            aligned.append(AlignedPair(
                history, current, basis=basis,
                supplementary=(basis == "SUPPLEMENTARY_RELATION")))
            unmatched_history.discard(history)
            unmatched_current.discard(current)
            break
    if dictionary is not None:
        # Tier-2 词典匹配（D19/S2 §3）：逐字未配对的候选经词典 canonical 再比，
        # 锚定规则约束单槽归一；命中 basis=SUBJECT_DETERMINISTIC_ALIAS。
        for history in history_pointers:
            if history not in unmatched_history:
                continue
            for current in current_pointers:
                if current not in unmatched_current:
                    continue
                hits = _pointer_dict_match(history, current, dictionary)
                if hits is None:
                    continue
                aligned.append(AlignedPair(
                    history, current,
                    basis="SUBJECT_DETERMINISTIC_ALIAS",
                    supplementary=False,
                    normalization_hits=hits,
                ))
                unmatched_history.discard(history)
                unmatched_current.discard(current)
                break

    if supplementary_set and not supplementary_used:
        raise PairAlignmentError(
            "supplementary_pairs provided but no matching Fact pair was found"
        )

    unresolved: list[str] = []
    for pointer in sorted(unmatched_history, key=lambda item: item.fact_id):
        unresolved.append(f"history:{pointer.fact_id}")
    for pointer in sorted(unmatched_current, key=lambda item: item.fact_id):
        unresolved.append(f"current:{pointer.fact_id}")

    if not aligned:
        code = "RULE_UNCOVERED"
    else:
        # N-05（外审六路增量复核，窗口V）：原 if/else 两分支赋值逐字相同
        # （死分支，改前 L422-427 程序化比对 byte-identical=True），坍缩为
        # 单一赋值。来源：窗口J t0 备份（log/temp/winJ-backup/
        # pair_alignment.py.t0 L416-421）已同形态——死分支先于窗口J M-04(a)
        # 改写存在，非该改写引入。行为零漂移由三形态守卫钉
        # （tests/unit/test_winv_audit_repairs.py）+ v2 腿对锚 uat065250 实测。
        code = candidate_basis if candidate_basis in _ALIGNMENT_BASIS_WHITELIST \
            else "FACT_EQUIVALENT"

    provenance = {
        "dictionary_version": dictionary_version,
        "alignment_version": alignment_version,
    }
    if dictionary_date is not None:
        provenance["dictionary_date"] = dictionary_date

    return PairAlignmentOutcome(
        aligned_facts=tuple(aligned),
        unresolved_fields=tuple(unresolved),
        code=code,
        provenance=provenance,
    )


__all__ = [
    "AlignedPair",
    "FactPointer",
    "PairAlignmentError",
    "PairAlignmentOutcome",
    "build_aligned",
]