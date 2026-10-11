"""F2 准备层（B4/E1 真链路实施 J9 片；设计=log\设计-真链路接线包.md §三-F2）。

六职责（§三-F2①~⑥）：
① 扫描日索引物化文档（task_state∈{accepted,running} 且 preparation_state 非终态）；
② 逐文档 prepare_recall_fields（hash_channel.py:27-62 复用，outer_trim_approved
   默认 False=N15 保守口径）+ extract_body_entities→entity_ids；
③ ES CAS 合并派生字段+preparation_state="ready"（冲突重读重算——派生字段对同
   文本确定性幂等；text/raw_hash 变即放弃该轮并报冲突）；
④ refresh 屏障后推进日控制文档水位——lexical_watermark 写既有 long 槽位；
   prepared 前沿快照内嵌 disabled `checkpoint` 对象（先例 g0_persistence.py:330-334）；
   日控制文档缺则建（create-if-absent，N-4；先例 g0_persistence.py:182-198）；
⑤ WatermarkProvider 读侧端口：visible_seq（=lexical_watermark，09 §3.2 L174）
   与 prepared_seq（=checkpoint 内嵌准备前沿；孔洞纪律 L175：逐文档 ready 证据
   推进，缺号不越过——W-R4 起按 09 §3.4 原文口径补跳过凭证明语义：
   arrival_seq 为全局序号域，分区证据集是其稀疏子集；前沿遇全局序号孔洞时
   向 admission 域查证登记归属（seq 登记映射/pending 受理日志），登记证明
   属他域/日 → 凭证明跳过续推，无法证明（登记缺席/属本分区未就绪）→
   维持停摆 fail-closed，「缺号未知须恢复，不能猜不存在」；
   P0-T5 全前沿令②（主窗口扩充令）：孔洞另查**墓簿**——本域本日
   item_permanent 终态序号（物化域确定性已处理）与外国证明同为跳洞
   凭据，两源分立、账务分计，未证序号仍停摆）；
⑥ 空体/不可索引文本按 hash_channel.py:38-39 None 路径记 not_applicable 不硬拦
   （终态准备证据，前沿可越过——否则空文档恒成孔洞、链路恒 STALE）。

数据兼容（§四-4）：准备写字段全部落既有 item mapping 字段位
（es_admission_schema.py:56-74）；item/control 两侧 mapping 零变更。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from news_flash_dedup.admission import CONTROL_INDEX, REQUEST_INDEX
from news_flash_dedup.batch_admission import HEAD_ID, pending_registration
from news_flash_dedup.batch_es_store import ElasticsearchBatchStore
from news_flash_dedup.es_admission_schema import day_index
from news_flash_dedup.materialize_terminal import scan_terminal_tombstone_seqs

from .entities import extract_body_entities
from .hash_channel import prepare_recall_fields

#: 准备终态词表（通道查询仅认 "ready"；两态同为前沿推进证据——F2⑥）
PREPARED_TERMINAL_STATES = frozenset({"ready", "not_applicable"})
_TASK_STATES_PREPARE = ("accepted", "running")
_SNAPSHOT_KEY = "recall_prepared"


class PrepareConflict(RuntimeError):
    """准备 CAS 冲突：text/raw_hash 已换代或冲突超界——放弃该轮不掩盖。"""


def advance_prepared_frontier(stored: int, ready_seqs: Iterable[int],
                              foreign: Iterable[int] | None = None,
                              terminal: Iterable[int] | None = None) -> int:
    """孔洞纪律（09 §3.2 L175 + §3.4 跳过凭证明语义）：从已存前沿起，逐文档
    ready 证据连续推进；遇全局序号孔洞时，仅当该序号凭登记证明属他域/日
    （foreign=已证外国全局序号集合，由 admission 域登记查证供给）**或**凭
    墓簿证明为本域本日终态（terminal=已证墓碑序号集合，P0-T5 全前沿令②：
    物化域 item_permanent 入墓=确定性已处理，墓证与外国证明同为跳洞凭据，
    两源分立账务分计）才可跳过续推；无证明（两集合均缺席/未涵盖）→
    缺号不越过（fail-closed 不猜）；单调不回退（已覆盖序号跳过，不回撤
    已认证前沿）。

    foreign=None 且 terminal=None（无凭证明输入）与空集行为一致：退化为
    L175 原口径「缺号不越过」——首域占有全局前缀或无证明场景行为逐字节
    不变（既有钉 test_recall_prepare.py::test_r7_frontier_never_skips_holes
    钉的正是该无凭证明场景，语义兼容零改动）。
    """
    if type(stored) is not int or stored < 0:
        raise ValueError("stored frontier must be a nonnegative int")
    proven = None if foreign is None else {int(s) for s in foreign}
    terminal_proven = None if terminal is None else {int(s) for s in terminal}
    frontier = stored
    for seq in sorted({int(s) for s in ready_seqs}):
        if seq <= frontier:
            continue
        if proven is None and terminal_proven is None:
            if seq == frontier + 1:
                frontier = seq
            else:
                break
            continue
        blocked = False
        for gap in range(frontier + 1, seq):
            if ((terminal_proven is not None and gap in terminal_proven)
                    or (proven is not None and gap in proven)):
                frontier = gap   # 凭证明跳过他域/日或墓碑终态序号
                continue
            blocked = True       # 首个未证序号：停于其前，不猜不越过
            break
        if blocked:
            break
        frontier = seq
    return frontier


def _utc_iso(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("clock must be timezone aware")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class PrepareReport:
    """一轮准备驱动报告。"""
    scope_id: str
    business_date: str
    prepared: tuple[str, ...] = ()
    not_applicable: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()
    lexical_watermark: int = 0
    prepared_seq: int = 0


class ElasticsearchWatermarkProvider:
    """F2⑤ 生产读侧：经 ES 控制文档（CONTROL_INDEX 日文档）实时 GET 读水位。

    日文档缺=无任何准备证据→(0,0)（fail-closed，不捏造前沿）。
    """

    def __init__(self, client: Any, index_prefix: str) -> None:
        self._store = ElasticsearchBatchStore(client, index_prefix=index_prefix)

    @staticmethod
    def day_key(scope_id: str, business_date: str) -> str:
        # g0_persistence.py:82-83 同款键式
        return f"day:{scope_id}:{business_date}"

    def _day_source(self, scope_id: str, business_date: str) -> dict:
        doc = self._store.get(CONTROL_INDEX, self.day_key(scope_id, business_date))
        return {} if doc is None else doc["source"]

    def visible_seq(self, scope_id: str, business_date: str) -> int:
        source = self._day_source(scope_id, business_date)
        return int(source.get("lexical_watermark", 0))

    def prepared_seq(self, scope_id: str, business_date: str) -> int:
        source = self._day_source(scope_id, business_date)
        checkpoint = source.get("checkpoint")
        if not isinstance(checkpoint, dict):
            return 0
        snapshot = checkpoint.get(_SNAPSHOT_KEY)
        if not isinstance(snapshot, dict):
            return 0
        return int(snapshot.get("prepared_seq", 0))


class PrepareWorker:
    """F2 准备驱动器。构造注入 ES client+index_prefix（显式注入，五通道风格）；
    内部经 ElasticsearchBatchStore 做实时 GET/CAS（p01-batch- 前缀闸现役执法）。

    配置项（实施窗显式配置，非造数）：page_size（扫描页）、ready_scan_size
    （ready 证据枚举上限，超出保守低认前沿——孔洞纪律安全向）、
    max_cas_attempts（单文档/日文档 CAS 有界重试）、max_scan_rounds（扫描轮次
    上限，防活锁）、proof_page_size / max_proof_pages（登记证明枚举页与页数
    上限——证明工作量有界，超出即无法证明=维持停摆，fail-closed 安全向）。
    """

    def __init__(self, client: Any, index_prefix: str, *,
                 clock: Callable[[], datetime] | None = None,
                 page_size: int = 200, ready_scan_size: int = 10000,
                 max_cas_attempts: int = 3, max_scan_rounds: int = 100,
                 proof_page_size: int = 1000, max_proof_pages: int = 100) -> None:
        if min(page_size, ready_scan_size, max_cas_attempts, max_scan_rounds,
               proof_page_size, max_proof_pages) < 1:
            raise ValueError("prepare worker limits must be positive")
        self.client = client
        self.index_prefix = index_prefix
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.page_size = page_size
        self.ready_scan_size = ready_scan_size
        self.max_cas_attempts = max_cas_attempts
        self.max_scan_rounds = max_scan_rounds
        self.proof_page_size = proof_page_size
        self.max_proof_pages = max_proof_pages
        self._store = ElasticsearchBatchStore(client, index_prefix=index_prefix)

    # ---------- 扫描 ----------

    def _scan_unprepared(self, scope_id: str, physical_index: str) -> list[str]:
        body = {
            "size": self.page_size,
            "track_total_hits": True,
            "query": {"bool": {
                "filter": [
                    {"term": {"scope_id": scope_id}},
                    {"terms": {"task_state": list(_TASK_STATES_PREPARE)}},
                ],
                "must_not": [
                    {"terms": {"preparation_state": list(PREPARED_TERMINAL_STATES)}},
                ],
            }},
            "sort": [{"arrival_seq": "asc"}, {"record_id": "asc"}],
            "_source": ["record_id"],
        }
        result = self.client.search(index=physical_index, body=body)
        hits = result.get("hits", {})
        return [hit["_id"] for hit in hits.get("hits", [])]

    def _scan_ready_seqs(self, scope_id: str, physical_index: str) -> set[int]:
        body = {
            "size": self.ready_scan_size,
            "track_total_hits": True,
            "query": {"bool": {"filter": [
                {"term": {"scope_id": scope_id}},
                {"terms": {"preparation_state": list(PREPARED_TERMINAL_STATES)}},
            ]}},
            "sort": [{"arrival_seq": "asc"}, {"record_id": "asc"}],
            "_source": ["arrival_seq"],
        }
        result = self.client.search(index=physical_index, body=body)
        hits = result.get("hits", {})
        # 超出枚举上限→保守低认（孔洞纪律安全向：只按已枚举证据推进）
        return {int(hit["_source"]["arrival_seq"])
                for hit in hits.get("hits", [])
                if type(hit.get("_source", {}).get("arrival_seq")) is int}

    # ---------- 登记证明枚举（09 §3.4 跳过凭据） ----------

    def _scan_foreign_proofs(self, scope_id: str, business_date: str,
                             holes: list[int], head_source: dict | None,
                             ) -> set[int]:
        """逐序号查证 holes 内全局序号的登记归属，返回已证属他域/日子集。

        证明源（与受理登记同构，只读）：
        ① ES seq 登记映射（REQUEST_INDEX 物化登记，batch_admission.py:467
           形态）——按 arrival_seq 升序有界分页（游标式，proof_page_size×
           max_proof_pages 上限内）；
        ② 头文档 pending 受理日志（未物化段登记，batch_admission.py
           pending_registration 查询面）——①未涵盖序号的最末兜底。

        登记缺席、形态不识别、登记属本分区（同 scope 同 business_date）→
        不证（调用方遇未证孔洞即停，fail-closed 不猜——「缺号未知须恢复，
        不能猜不存在」）。分页预算耗尽同样保守不证。
        """
        if not holes:
            return set()
        proven: set[int] = set()
        remaining = set(holes)
        hi = max(holes)
        cursor = min(holes) - 1
        physical = self.index_prefix + REQUEST_INDEX
        for _page in range(self.max_proof_pages):
            body = {
                "size": self.proof_page_size,
                "track_total_hits": True,
                "query": {"bool": {"filter": [
                    {"term": {"kind": "seq"}},
                    {"range": {"arrival_seq": {"gt": cursor, "lte": hi}}},
                ]}},
                "sort": [{"arrival_seq": "asc"}],
                "_source": ["arrival_seq", "scope_id", "business_date"],
            }
            result = self.client.search(index=physical, body=body)
            hits = result.get("hits", {}).get("hits", [])
            if not hits:
                break
            for hit in hits:
                source = hit.get("_source", {})
                seq = source.get("arrival_seq")
                if type(seq) is not int:
                    continue
                cursor = max(cursor, seq)
                if seq not in remaining:
                    continue
                remaining.discard(seq)
                registered_scope = source.get("scope_id")
                registered_day = source.get("business_date")
                if (isinstance(registered_scope, str) and
                        isinstance(registered_day, str) and
                        (registered_scope, registered_day)
                        != (scope_id, business_date)):
                    proven.add(seq)
                # 登记属本分区/身份残缺：不证（真孔洞或存疑，均停摆不猜）
            if len(hits) < self.proof_page_size or cursor >= hi:
                break
        if remaining and head_source is not None:
            for seq in sorted(remaining):
                registration = pending_registration(head_source, seq)
                if (registration is not None
                        and registration != (scope_id, business_date)):
                    proven.add(seq)
        return proven

    # ---------- 单文档准备（③ CAS 合并） ----------

    def _prepare_one(self, logical_index: str, scope_id: str,
                     business_date: str, record_id: str) -> str:
        """返回 ready / not_applicable / conflict / skipped。"""
        baseline: tuple[Any, Any] | None = None   # (text, raw_hash) 首读基线
        for _attempt in range(self.max_cas_attempts):
            doc = self._store.get(logical_index, record_id)
            if doc is None:
                return "skipped"
            source = doc["source"]
            if (source.get("task_state") not in _TASK_STATES_PREPARE
                    or source.get("preparation_state") in PREPARED_TERMINAL_STATES):
                return "skipped"
            text = source.get("text", "")
            raw_hash = source.get("raw_hash")
            if baseline is not None and (text, raw_hash) != baseline:
                # 冲突重读后：text/raw_hash 已换代→放弃该轮并报冲突
                return "conflict"
            derived = prepare_recall_fields(
                text, scope_id, business_date, outer_trim_approved=False)
            if derived is None:
                # ⑥ 空体/不可索引：not_applicable 终态，不硬拦
                updated = {**source, "preparation_state": "not_applicable"}
                outcome = "not_applicable"
            else:
                updated = {
                    **source, **derived,
                    "entity_ids": list(extract_body_entities(text).entity_ids),
                    "preparation_state": "ready",
                }
                outcome = "ready"
            try:
                self._store.replace(logical_index, record_id, updated,
                                    doc["seq_no"], doc["primary_term"])
            except Exception as error:
                if not self._store.is_conflict(error):
                    raise
                if baseline is None:
                    baseline = (text, raw_hash)
                continue
            return outcome
        return "conflict"

    # ---------- 日控制文档（④ 水位推进） ----------

    def _day_body(self, scope_id: str, business_date: str, now: str) -> dict:
        # create-if-absent 形态对齐 g0_persistence.py:184-190（F2 不持 owner/
        # 决策水位职责：decision_watermark 恒 0 不推进——决策水位属 commit 域）
        return {
            "kind": "day", "scope_id": scope_id, "business_date": business_date,
            "pipeline_version": "dedup_v1",
            "decision_watermark": 0, "lexical_watermark": 0,
            "updated_at": now,
            "checkpoint": {_SNAPSHOT_KEY: {
                "prepared_seq": 0, "prepared_at": now,
                "scope_id": scope_id, "business_date": business_date,
                "evidence": "per_doc_ready", "foreign_proof_count": 0,
            }},
        }

    def _advance_watermarks(self, scope_id: str, business_date: str,
                            physical_index: str) -> tuple[int, int]:
        # refresh 屏障（L174：可检索边界须经 refresh 确认）
        self.client.indices.refresh(index=physical_index)
        head = self._store.get(CONTROL_INDEX, HEAD_ID)
        materialized = 0
        if head is not None:
            value = head["source"].get("last_materialized_seq", 0)
            materialized = value if type(value) is int and value > 0 else 0
        ready_seqs = self._scan_ready_seqs(scope_id, physical_index)
        now = _utc_iso(self.clock())
        day_key = ElasticsearchWatermarkProvider.day_key(scope_id, business_date)
        # 09 §3.4 跳过凭证明（W-R4）：arrival_seq 为全局序号域，分区 ready
        # 证据集是其稀疏子集；推进区间内的孔洞逐序号向 admission 域查证登记
        # 归属，登记证明属他域/日才可跳过。证明枚举一次完成：前沿单调不回退，
        # CAS 重试见到的 stored 只会 ≥ 首读提示值，孔洞集合恒为已证集子集。
        # P0-T5 全前沿令②（主窗口扩充令）：孔洞另查**墓簿**（本域本日
        # item_permanent 终态——物化域确定性已处理，非外国序号）：墓证与
        # 外国证明两源分立、账务分计（tombstone_proof_count 审计位）；
        # 未墓未证序号仍停摆（fail-closed 不猜——「缺号未知须恢复」）。
        foreign: set[int] = set()
        terminal: set[int] | None = None
        if ready_seqs:
            max_ready = max(ready_seqs)
            hint = self._store.get(CONTROL_INDEX, day_key)
            hint_snapshot = None
            if hint is not None:
                hint_checkpoint = hint["source"].get("checkpoint")
                if isinstance(hint_checkpoint, dict):
                    candidate = hint_checkpoint.get(_SNAPSHOT_KEY)
                    if isinstance(candidate, dict):
                        hint_snapshot = candidate
            stored_hint = (int(hint_snapshot.get("prepared_seq", 0))
                           if hint_snapshot is not None else 0)
            if max_ready > stored_hint:
                budget = self.proof_page_size * self.max_proof_pages
                holes: list[int] = []
                for seq in range(stored_hint + 1, max_ready + 1):
                    if seq not in ready_seqs:
                        holes.append(seq)
                        if len(holes) >= budget:
                            # 证明预算耗尽：预算外序号不证（推进停于首个未证
                            # 孔洞——fail-closed，工作量有界）
                            break
                if holes:
                    # 墓簿共读面（⑧同一终态证据源）：物理前缀经端口包装，
                    # ready_scan_size 为单页枚举上界（超出保守不证）。
                    terminal = scan_terminal_tombstone_seqs(
                        lambda index, body: self.client.search(
                            index=self.index_prefix + index, body=body),
                        scope_id, business_date,
                        scan_size=self.ready_scan_size)
                foreign = self._scan_foreign_proofs(
                    scope_id, business_date, holes,
                    head["source"] if head is not None else None)
        for _attempt in range(self.max_cas_attempts):
            day = self._store.get(CONTROL_INDEX, day_key)
            if day is None:
                try:
                    self._store.create(CONTROL_INDEX, day_key,
                                       self._day_body(scope_id, business_date, now))
                except Exception as error:
                    if not self._store.is_conflict(error):
                        raise
                day = self._store.get(CONTROL_INDEX, day_key)
                if day is None:
                    raise PrepareConflict("day control creation is unconfirmed")
            source = day["source"]
            checkpoint = source.get("checkpoint")
            snapshot = (checkpoint.get(_SNAPSHOT_KEY)
                        if isinstance(checkpoint, dict) else None)
            stored = (int(snapshot.get("prepared_seq", 0))
                      if isinstance(snapshot, dict) else 0)
            frontier = advance_prepared_frontier(stored, ready_seqs,
                                                 foreign=foreign,
                                                 terminal=terminal)
            lexical_stored = source.get("lexical_watermark", 0)
            lexical_stored = (lexical_stored
                              if type(lexical_stored) is int and lexical_stored > 0
                              else 0)
            lexical = max(lexical_stored, materialized)
            new_checkpoint = {
                **(checkpoint if isinstance(checkpoint, dict) else {}),
                _SNAPSHOT_KEY: {
                    "prepared_seq": frontier, "prepared_at": now,
                    "scope_id": scope_id, "business_date": business_date,
                    "evidence": "per_doc_ready",
                    # 本轮推进凭登记证明跳过的他域/日序号计数（有界审计位；
                    # 证明本体可由 seq 登记映射/pending 日志确定性重导）
                    "foreign_proof_count": sum(
                        1 for seq in foreign if stored < seq <= frontier),
                    # P0-T5 全前沿令②：凭墓簿跳过的本域日终态序号计数
                    # （有界审计位；证明本体=墓簿索引，确定性重导）。
                    "tombstone_proof_count": sum(
                        1 for seq in (terminal or ())
                        if stored < seq <= frontier),
                },
            }
            if (frontier == stored and lexical == lexical_stored
                    and isinstance(snapshot, dict)):
                return lexical, frontier
            updated = {**source, "lexical_watermark": lexical,
                       "checkpoint": new_checkpoint, "updated_at": now}
            try:
                self._store.replace(CONTROL_INDEX, day_key, updated,
                                    day["seq_no"], day["primary_term"])
            except Exception as error:
                if not self._store.is_conflict(error):
                    raise
                continue
            return lexical, frontier
        raise PrepareConflict("day control watermark CAS contention exceeded")

    # ---------- 一轮驱动（①②③④） ----------

    def prepare(self, scope_id: str, business_date: str, *,
                budget: "ProcessingBudget | None" = None) -> PrepareReport:
        # W2 ⑩①-3（N43 挂账清偿）：budget=传播随行件，本方法不消费——
        # 扫描/水位推进非模型调用，②软预算执法面在模型调用入口闸
        # （facts/llm、llm_residual、embedding_client 三处新逻辑调用拒启闸）。
        logical_index = day_index(business_date)
        physical_index = self.index_prefix + logical_index
        prepared: list[str] = []
        not_applicable: list[str] = []
        conflicts: list[str] = []
        for _round in range(self.max_scan_rounds):
            hit_ids = self._scan_unprepared(scope_id, physical_index)
            if not hit_ids:
                break
            progressed = False
            for record_id in hit_ids:
                outcome = self._prepare_one(logical_index, scope_id,
                                            business_date, record_id)
                if outcome == "ready":
                    prepared.append(record_id)
                    progressed = True
                elif outcome == "not_applicable":
                    not_applicable.append(record_id)
                    progressed = True
                elif outcome == "conflict":
                    conflicts.append(record_id)
            if not progressed:
                break
        lexical, frontier = self._advance_watermarks(
            scope_id, business_date, physical_index)
        return PrepareReport(
            scope_id=scope_id, business_date=business_date,
            prepared=tuple(prepared), not_applicable=tuple(not_applicable),
            conflicts=tuple(conflicts),
            lexical_watermark=lexical, prepared_seq=frontier)


__all__ = [
    "PREPARED_TERMINAL_STATES",
    "ElasticsearchWatermarkProvider",
    "PrepareConflict",
    "PrepareReport",
    "PrepareWorker",
    "advance_prepared_frontier",
]
