"""P1a-T1 ES 队列存取（任务文档 enqueue/claim/complete/defer/terminalize/租约接管）。

工艺锚（P0 复用三族）：

- **确定性 ID + create→读回对拍**（T3 ``persist_tombstone`` 同族）：
  ``work_item_id = sha256(scope_id, business_date, arrival_seq)``（与墓碑
  ID 同族工艺——不同索引，天然幂等防双建）；create 成功→GET 读回确认
  （confirmed 才算数）；冲突→读回对拍（身份冻结字段 ``FROZEN_FIELDS``
  一致=幂等重放，首证为准不更新；分歧=IdentityConflict→T1 分类
  IDENTITY_CORRUPTION 停推进+报警）；读回缺失/传输未知=
  ``WorkPersistUnknown``（可重试暂时态，绝不当作已确认）；
- **租约 CAS fencing**（resilience-design §8.2 / 蓝图 1.3）：claim=
  CAS 置 ``leased`` + generation+1 + ``lease_expires_at``；终态翻写
  （ready/tombstoned/expired）与 retry_wait 落账的 CAS 前置
  ``lease_owner``+``lease_generation`` 比对——复活旧 worker 的写入因
  generation 不匹配失败（``WorkLeaseLost``，防僵尸写；处理全程幂等，
  接管后终态输出恰一份由确定性 ID+对拍承载）；
- **CAS 冲突有界重试 8 次**（batch_admission ``_quarantine`` 同先例）；
  非冲突异常=写未知（``WorkPersistUnknown``，fail-closed 不猜）。

层界：本模块只提供原子原语；编排（受理闸序/物化 claim 循环/容量闸）在
batch_admission 域（P1a-T2/T3/T4）。存储协议=``TombstoneStore`` 同型
（get/create/replace/is_conflict——BatchStore/MemoryStore/
ElasticsearchBatchStore 结构满足）；检索面=只读 ``search`` 端口
（``scan_terminal_tombstone_seqs`` 同型：物理前缀由调用方包装进端口）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Protocol

from pydantic import ValidationError

from ..admission import AdmissionConflict, IdentityConflict, _digest, _utc
from ..es_admission_schema import day_index
from ..materialize_failure import MaterializeFailure
from .schema import (
    FROZEN_FIELDS, MATERIALIZE_READY, MATERIALIZE_RETRY_WAIT,
    MATERIALIZE_TOMBSTONE, MATERIALIZE_EXPIRED, MATERIALIZE_MATERIALIZING,
    TERMINAL_TASK_STATES, WORK_TASK_LEASED, WORK_TASK_PENDING,
    WORK_TASK_READY, WORK_TASK_RETRY_WAIT, WORK_TASK_TOMBSTONED,
    WORK_TASK_EXPIRED, WorkItemV1, WorkResultV1,
    validate_work_item_invariants,
)


#: 租约时长初值（蓝图 §七-1 裁定：60s 起步，与 T2 熔断开路 30s/退避上限
#: 60s 协调；终值按 24h shadow 校准）。config_version 治下参数。
WORK_LEASE_DEFAULT_SECONDS = 60.0

_CAS_RETRY_LIMIT = 8


class WorkPersistUnknown(RuntimeError):
    """任务文档持久化确认未知（create/读回/CAS 传输未证）——可重试暂时态。

    **绝不**当作已确认：终态/翻态未证时水位不得推进（T3
    TombstonePersistUnknown 同族纪律）。
    """


class WorkLeaseLost(RuntimeError):
    """fencing 失败：租约代次已被后继接管者推进（僵尸 worker 检出）。

    携带方的任何翻态/落账写入必须放弃本任务（不重试不覆盖——
    「终态输出恰一份」的写侧执法面）。携带字段：``expected_generation``/
    ``observed_generation``（对账面，消息有界）。
    """

    def __init__(self, message: str, *, expected_generation: int,
                 observed_generation: int) -> None:
        super().__init__(message)
        self.expected_generation = expected_generation
        self.observed_generation = observed_generation


class _Conflict(Exception):
    """内部信号：CAS 版本竞争（调用方重读再试，不上抛出域）。"""


class WorkQueueStore(Protocol):
    """任务文档持久化协议（TombstoneStore 同型——结构满足件复用）。"""

    def get(self, index: str, key: str) -> dict | None: ...
    def create(self, index: str, key: str, body: dict) -> None: ...
    def replace(self, index: str, key: str, body: dict,
                seq_no: int, primary_term: int) -> None: ...
    def is_conflict(self, error: Exception) -> bool: ...


@dataclass(frozen=True)
class WorkEnqueueResult:
    """入队持久化结果（confirmed=True 才可推进受理游标）。"""

    work_id: str
    index: str
    confirmed: bool
    created: bool              # True=本次新建；False=幂等重放对拍一致
    existing_source: dict | None = None   # 重放读回证据（冻结字段快照）


@dataclass(frozen=True)
class WorkClaimResult:
    """claim/租约接管结果（outcome 稳定码表）。

    - ``claimed``：CAS 成功持约（lease_generation/expires_at 为新值）；
    - ``recovered``：过期租约接管成功（generation 再+1）；
    - ``busy``：租约被他方持有且未过期，或补偿中不可领（本方不得触碰）；
    - ``waiting``：retry_wait 未到 ``next_retry_at``（退避未到期）；
    - ``terminal``：任务已终态（claim 幂等 no-op，item 快照随附）；
    - ``absent``：任务文档缺席（调用方按未建处理，不猜不存在）。
    """

    outcome: str
    task_state: str = ""
    lease_generation: int = 0
    lease_expires_at: str | None = None
    lease_owner: str | None = None
    item: WorkItemV1 | None = None


def work_index(business_date: str) -> str:
    """任务文档逻辑索引名（``day_index`` kind 形态：work）。"""
    return day_index(business_date, "work")


#: 任务文档族通配索引形态（跨业务日检索面——受理复用/深度计数/孤儿探针/
#: claim 扫描消费；P1a-T2）。与 ``work_index`` 前缀一致性由
#: ``test_work_queue`` 钉（``work_index("…").startswith`` 断言）。
WORK_INDEX_PATTERN = "news-dedup-work-v1-*"


def work_item_id(scope_id: str, business_date: str, arrival_seq: int) -> str:
    """确定性任务 ID：``sha256(scope_id, business_date, arrival_seq)``。

    与墓碑 ID 同族工艺（``tombstone_id`` 同参 digest——不同索引，不冲突）；
    重放对拍，不重复创建第二个任务文档。
    """
    if not isinstance(scope_id, str) or not scope_id:
        raise ValueError("scope_id must be a non-empty str")
    if not isinstance(business_date, str) or not business_date:
        raise ValueError("business_date must be a non-empty str")
    if type(arrival_seq) is not int or arrival_seq < 1:
        raise ValueError("arrival_seq must be a positive int")
    return _digest([scope_id, business_date, arrival_seq])


def work_documents(items: list[WorkItemV1]) -> list[tuple[str, str, dict]]:
    """批量入队文档元组（``bulk_create_classified`` 直接消费的形态）。

    纯函数：``(work_index(business_date), work_item_id(...), body)``——
    T4 存储面零改动复用的对接面（逐条 outcome 解析在协调器收口，
    P1a-T3）。
    """
    return [(work_index(item.business_date),
             work_item_id(item.scope_id, item.business_date,
                          item.arrival_seq),
             item.model_dump(mode="json"))
            for item in items]


# ---------- 读/校验/CAS 基元 ----------


def load_work_item(store: WorkQueueStore, scope_id: str, business_date: str,
                   arrival_seq: int) -> WorkItemV1 | None:
    """读回某序号的任务文档（读侧查询面；读未知 fail-closed 上抛）。"""
    document = _read(store, *work_coords(scope_id, business_date,
                                         arrival_seq))
    if document is None:
        return None
    return _validated(document)


def work_coords(scope_id: str, business_date: str,
                arrival_seq: int) -> tuple[str, str]:
    """(index, key) 坐标对（各原语共用派生面）。"""
    return (work_index(business_date),
            work_item_id(scope_id, business_date, arrival_seq))


def _read(store: WorkQueueStore, index: str, key: str) -> dict | None:
    try:
        return store.get(index, key)
    except Exception as error:
        raise WorkPersistUnknown("work item read is unknown") from error


def _validated(document: dict) -> WorkItemV1:
    """读回文档 → strict+不变量校验（schema 漂移=结构损坏，fail-closed 拒）。"""
    source = document.get("source") if isinstance(document, dict) else None
    if not isinstance(source, dict):
        raise AdmissionConflict("work item document is malformed")
    try:
        item = WorkItemV1.model_validate(source)
    except ValidationError as error:
        raise AdmissionConflict(
            "work item schema validation failed") from error
    try:
        validate_work_item_invariants(item)
    except ValueError as error:
        raise AdmissionConflict(
            "work item invariants validation failed") from error
    return item


def _frozen_divergences(existing: dict | None, body: dict) -> list[str]:
    """冻结字段逐名对拍（生命周期/租约/失败证据/审计不参拍——首证为准）。"""
    if not isinstance(existing, dict):
        return ["<source>"]
    return [name for name in FROZEN_FIELDS
            if existing.get(name) != body.get(name)]


def _parse(timestamp: str) -> datetime:
    value = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    if value.tzinfo is None:
        raise AdmissionConflict("work item timestamp is not timezone aware")
    return value


def _lease_live(item: WorkItemV1, now: datetime) -> bool:
    if item.task_state != WORK_TASK_LEASED or item.lease_expires_at is None:
        return False
    return _parse(item.lease_expires_at) > now


def _write_validated(store: WorkQueueStore, index: str, key: str,
                     document: dict, updated: dict) -> WorkItemV1:
    """写边界执法：新体 strict+不变量校验后才许落库（写侧不产脏文档）。

    CAS 版本竞争以 ``_Conflict`` 内部信号上抛（调用方重读再试）；非冲突
    异常包装为写未知 fail-closed。
    """
    try:
        item = WorkItemV1.model_validate(updated)
    except ValidationError as error:
        raise AdmissionConflict(
            "work item update is schema invalid") from error
    try:
        validate_work_item_invariants(item)
    except ValueError as error:
        raise AdmissionConflict(
            "work item update violates invariants") from error
    try:
        store.replace(index, key, updated,
                      document["seq_no"], document["primary_term"])
    except Exception as error:
        if store.is_conflict(error):
            raise _Conflict() from error
        raise WorkPersistUnknown("work item CAS is unknown") from error
    return item


def _fenced_current(document: dict, *, owner_id: str,
                    lease_generation: int) -> WorkItemV1:
    """读当前文档并做 fencing 前置判定（不写入）。

    - ``leased`` + owner/generation 匹配 → 返回快照（调用方构造翻态体）；
    - ``leased`` + 代次不匹配 → ``WorkLeaseLost``（僵尸检出——本方租约
      已被后继接管）；
    - 终态 → 返回快照（调用方按各自幂等/违例语义收口）；
    - 其余活态（pending/retry_wait/compensating）→ AdmissionConflict
      （未持约翻态=状态机违例——fenced 写要求在案 claim）。
    """
    item = _validated(document)
    if item.task_state == WORK_TASK_LEASED:
        if (item.lease_generation != lease_generation
                or item.lease_owner != owner_id):
            raise WorkLeaseLost(
                "work lease generation moved on: expected "
                f"{lease_generation}, observed {item.lease_generation}",
                expected_generation=lease_generation,
                observed_generation=item.lease_generation)
        return item
    if item.task_state not in TERMINAL_TASK_STATES:
        raise AdmissionConflict(
            "work item is not leased; fenced write requires an active claim")
    return item


# ---------- enqueue（确定性 ID create→读回对拍） ----------


def enqueue_work_item(store: WorkQueueStore,
                      item: WorkItemV1) -> WorkEnqueueResult:
    """持久化单条任务文档（T3 ``persist_tombstone`` 同族工艺）。

    - create 成功 → GET 读回：冻结字段一致 → confirmed/created；读回
      缺失/读失败 → ``WorkPersistUnknown``（重试；不得据 create 即确认）；
    - create 冲突 → GET 对拍：冻结字段一致 → confirmed/created=False
      （幂等，首证为准不更新——生命周期字段不参拍）；分歧 →
      IdentityConflict（IDENTITY_CORRUPTION）；读回未知 →
      ``WorkPersistUnknown``；
    - create 其他异常 → ``WorkPersistUnknown``（from error 保链）。

    入队体不变量执法（``validate_work_item_invariants``）：入队即校验；
    终态体入队=拒绝（入队不是终态通道——enqueue 只建非终态任务）。
    """
    try:
        validate_work_item_invariants(item)
    except ValueError as error:
        raise AdmissionConflict(
            "work item is invalid for enqueue") from error
    if item.task_state in TERMINAL_TASK_STATES:
        raise AdmissionConflict(
            "enqueue cannot create a terminal work item")
    index = work_index(item.business_date)
    key = work_item_id(item.scope_id, item.business_date, item.arrival_seq)
    body = item.model_dump(mode="json")
    try:
        store.create(index, key, body)
    except Exception as error:
        if not store.is_conflict(error):
            raise WorkPersistUnknown(
                "work item create is unknown") from error
        # 冲突：重放形态——实时读回冻结字段对拍（首证为准）。
        try:
            existing = store.get(index, key)
        except Exception as read_error:
            raise WorkPersistUnknown(
                "work item conflict readback is unknown") from read_error
        source = existing["source"] if existing else None
        diverged = _frozen_divergences(source, body)
        if diverged:
            raise IdentityConflict(
                "work item replay diverges on frozen fields: "
                + ",".join(diverged))
        return WorkEnqueueResult(
            work_id=key, index=index, confirmed=True, created=False,
            existing_source=source)
    # create 成功后读回确认（confirmed 才是推进凭据）。
    try:
        readback = store.get(index, key)
    except Exception as read_error:
        raise WorkPersistUnknown(
            "work item readback confirmation is unknown") from read_error
    if readback is None:
        raise WorkPersistUnknown(
            "work item readback did not confirm persistence")
    diverged = _frozen_divergences(readback.get("source"), body)
    if diverged:
        raise IdentityConflict(
            "work item readback diverges on frozen fields: "
            + ",".join(diverged))
    return WorkEnqueueResult(
        work_id=key, index=index, confirmed=True, created=True)


# ---------- claim（租约 CAS：pending/retry_wait 到期/过期租约接管） ----------


def claim_work_item(store: WorkQueueStore, scope_id: str, business_date: str,
                    arrival_seq: int, *, owner_id: str, now: datetime,
                    lease_seconds: float = WORK_LEASE_DEFAULT_SECONDS,
                    ) -> WorkClaimResult:
    """领取任务：CAS 置 ``leased``（generation+1/``lease_expires_at``）。

    领取资格（其余=幂等观察面，零写入）：
    - ``pending`` → 领取；
    - ``retry_wait`` 且 ``next_retry_at`` 已到 → 领取（退避到期）；
    - ``leased`` 且租约已过期 → 接管（claim 顺带 recover，蓝图 1.3；
      generation 再+1）；
    - ``leased`` 且租约未过期（含本方同名持有）→ ``busy``；
    - ``retry_wait`` 未到期 → ``waiting``；``compensating`` → ``busy``；
    - 终态 → ``terminal``（含快照）；文档缺席 → ``absent``。

    CAS 版本竞争（他方并发翻态）：重读重判有界 8 次；超界=
    AdmissionConflict（contention，调用方编排层重试）。
    """
    index, key = work_coords(scope_id, business_date, arrival_seq)
    for _attempt in range(_CAS_RETRY_LIMIT):
        document = _read(store, index, key)
        if document is None:
            return WorkClaimResult(outcome="absent")
        item = _validated(document)
        if item.task_state in TERMINAL_TASK_STATES:
            return WorkClaimResult(
                outcome="terminal", task_state=item.task_state, item=item)
        if _lease_live(item, now):
            return WorkClaimResult(
                outcome="busy", task_state=item.task_state,
                lease_generation=item.lease_generation,
                lease_expires_at=item.lease_expires_at,
                lease_owner=item.lease_owner, item=item)
        if (item.task_state == WORK_TASK_RETRY_WAIT
                and item.next_retry_at is not None
                and _parse(item.next_retry_at) > now):
            return WorkClaimResult(
                outcome="waiting", task_state=item.task_state, item=item)
        if item.task_state not in (WORK_TASK_PENDING, WORK_TASK_RETRY_WAIT,
                                   WORK_TASK_LEASED):
            # compensating 由补正域驱动，不进 claim 通道。
            return WorkClaimResult(
                outcome="busy", task_state=item.task_state, item=item)
        new_generation = item.lease_generation + 1
        lease_expires_at = _utc(now + timedelta(seconds=lease_seconds))
        updated = {
            **document["source"],
            "task_state": WORK_TASK_LEASED,
            "materialize_state": MATERIALIZE_MATERIALIZING,
            "lease_owner": owner_id,
            "lease_generation": new_generation,
            "lease_expires_at": lease_expires_at,
            "updated_at": _utc(now),
        }
        try:
            written = _write_validated(store, index, key, document, updated)
        except _Conflict:
            continue
        return WorkClaimResult(
            outcome="claimed", task_state=WORK_TASK_LEASED,
            lease_generation=new_generation,
            lease_expires_at=lease_expires_at, lease_owner=owner_id,
            item=written)
    raise AdmissionConflict(
        "work item claim CAS contention exceeded retry bound")


# ---------- 过期租约接管（recover_expired_leases 的单条原语） ----------


def recover_expired_lease(store: WorkQueueStore, scope_id: str,
                          business_date: str, arrival_seq: int, *,
                          owner_id: str, now: datetime,
                          lease_seconds: float = WORK_LEASE_DEFAULT_SECONDS,
                          ) -> WorkClaimResult:
    """接管已过期租约（fencing 链关键环：generation 再+1）。

    仅对 ``leased`` 且 ``lease_expires_at <= now`` 的任务生效；接管后
    ``task_state`` 保持 ``leased``（物化中续驱），owner/generation/
    expires_at 换新。未过期（被他方持有）→ ``busy``；终态 → ``terminal``
    （接管无对象——终态已收口）；非 leased 活态 → ``busy``（claim 通道
    处理 pending/retry_wait）；缺席 → ``absent``。
    """
    index, key = work_coords(scope_id, business_date, arrival_seq)
    for _attempt in range(_CAS_RETRY_LIMIT):
        document = _read(store, index, key)
        if document is None:
            return WorkClaimResult(outcome="absent")
        item = _validated(document)
        if item.task_state in TERMINAL_TASK_STATES:
            return WorkClaimResult(
                outcome="terminal", task_state=item.task_state, item=item)
        if item.task_state != WORK_TASK_LEASED or _lease_live(item, now):
            return WorkClaimResult(
                outcome="busy", task_state=item.task_state,
                lease_generation=item.lease_generation,
                lease_expires_at=item.lease_expires_at,
                lease_owner=item.lease_owner, item=item)
        new_generation = item.lease_generation + 1
        lease_expires_at = _utc(now + timedelta(seconds=lease_seconds))
        updated = {
            **document["source"],
            "lease_owner": owner_id,
            "lease_generation": new_generation,
            "lease_expires_at": lease_expires_at,
            "updated_at": _utc(now),
        }
        try:
            written = _write_validated(store, index, key, document, updated)
        except _Conflict:
            continue
        return WorkClaimResult(
            outcome="recovered", task_state=WORK_TASK_LEASED,
            lease_generation=new_generation,
            lease_expires_at=lease_expires_at, lease_owner=owner_id,
            item=written)
    raise AdmissionConflict(
        "work lease recovery CAS contention exceeded retry bound")


# ---------- complete（READY 终态+result 快照） ----------


def complete_work_item(store: WorkQueueStore, scope_id: str,
                       business_date: str, arrival_seq: int, *,
                       result: WorkResultV1 | None, owner_id: str,
                       lease_generation: int, now: datetime) -> str:
    """物化完成收口：CAS 置 ``ready``（fencing 执法）。

    ``result`` 可空（P1a-T2 勘误：物化收口早于决策产出——``None`` 即
    占位态，结论镜像由 commit 域后补；本原语只管状态机收口）。
    返回 ``"completed"``（本次翻态）或 ``"already_completed"``（幂等重放：
    已 ready、代次一致、快照一致——含双方皆 ``None`` 的占位形态）。
    快照分歧=IdentityConflict（确定性输出被破坏）；代次被顶替=
    ``WorkLeaseLost``（僵尸放弃）；他终态在案=AdmissionConflict
    （状态机违例，不得复活）。
    """
    index, key = work_coords(scope_id, business_date, arrival_seq)
    for _attempt in range(_CAS_RETRY_LIMIT):
        document = _read(store, index, key)
        if document is None:
            raise AdmissionConflict("work item is absent at completion")
        item = _fenced_current(document, owner_id=owner_id,
                               lease_generation=lease_generation)
        if item.task_state == WORK_TASK_READY:
            if item.lease_generation != lease_generation:
                raise WorkLeaseLost(
                    "completion superseded: expected generation "
                    f"{lease_generation}, observed {item.lease_generation}",
                    expected_generation=lease_generation,
                    observed_generation=item.lease_generation)
            if item.result != result:
                raise IdentityConflict(
                    "work item completion snapshot diverges on replay")
            return "already_completed"
        if item.task_state in TERMINAL_TASK_STATES:
            raise AdmissionConflict(
                "work item already reached a different terminal state")
        updated = {
            **document["source"],
            "task_state": WORK_TASK_READY,
            "materialize_state": MATERIALIZE_READY,
            "terminal_reason": "",
            "result": (None if result is None
                       else result.model_dump(mode="json")),
            "lease_owner": None,
            "lease_expires_at": None,
            "next_retry_at": None,
            "updated_at": _utc(now),
        }
        try:
            _write_validated(store, index, key, document, updated)
        except _Conflict:
            continue
        return "completed"
    raise AdmissionConflict(
        "work item completion CAS contention exceeded retry bound")


# ---------- defer（RETRY_WAIT 落账：T2 条目账持久化） ----------


def defer_work_item(store: WorkQueueStore, scope_id: str, business_date: str,
                    arrival_seq: int, *, failure: MaterializeFailure,
                    delay_seconds: float, definite: bool, owner_id: str,
                    lease_generation: int, now: datetime) -> str:
    """失败落账：CAS 置 ``retry_wait``（retry_count/next_retry_at/证据）。

    - ``definite=True``（确定失败）：``retry_count``+1，
      ``next_retry_at = now + delay``（全抖动退避由调用方算好注入）；
    - ``definite=False``（UNKNOWN_WRITE）：零计数，``next_retry_at``
      已置则保持（T2 ``record_unknown`` 同型：仅空槽补当前时刻=立即读回）；
    - 失败证据五件（first/last_failed_at、last_failure_class/
      last_error_code/last_error_summary）镜像 T1 ``MaterializeFailure``；
    - 重放幂等：已 ``retry_wait`` 且代次一致 → ``"already_deferred"``
      （退避期间不得重复落账）；代次被顶替 → ``WorkLeaseLost``；
      他终态在案=AdmissionConflict。
    """
    index, key = work_coords(scope_id, business_date, arrival_seq)
    for _attempt in range(_CAS_RETRY_LIMIT):
        document = _read(store, index, key)
        if document is None:
            raise AdmissionConflict("work item is absent at defer")
        item = _validated(document)
        if item.task_state == WORK_TASK_RETRY_WAIT:
            # 幂等重放判定先行（退避期文档不持约——代次=落账者证据）：
            # 同代次=本方先前已落账（already）；异代次=已被后继顶替（僵尸）。
            if item.lease_generation != lease_generation:
                raise WorkLeaseLost(
                    "defer superseded: expected generation "
                    f"{lease_generation}, observed {item.lease_generation}",
                    expected_generation=lease_generation,
                    observed_generation=item.lease_generation)
            return "already_deferred"
        if item.task_state in TERMINAL_TASK_STATES:
            raise AdmissionConflict(
                "work item already reached a terminal state; defer refused")
        item = _fenced_current(document, owner_id=owner_id,
                               lease_generation=lease_generation)
        retry_count = item.retry_count + 1 if definite else item.retry_count
        if definite:
            next_retry_at = _utc(now + timedelta(seconds=delay_seconds))
        else:
            next_retry_at = (item.next_retry_at if item.next_retry_at
                             is not None else _utc(now))
        updated = {
            **document["source"],
            "task_state": WORK_TASK_RETRY_WAIT,
            "materialize_state": MATERIALIZE_RETRY_WAIT,
            "retry_count": retry_count,
            "next_retry_at": next_retry_at,
            "first_failed_at": (item.first_failed_at
                                if item.first_failed_at is not None
                                else _utc(now)),
            "last_failed_at": _utc(now),
            "last_failure_class": failure.failure_class.value,
            "last_error_code": failure.error_code,
            "last_error_summary": failure.detail,
            "lease_owner": None,
            "lease_expires_at": None,
            "updated_at": _utc(now),
        }
        try:
            _write_validated(store, index, key, document, updated)
        except _Conflict:
            continue
        return "deferred"
    raise AdmissionConflict(
        "work item defer CAS contention exceeded retry bound")


# ---------- terminalize（TOMBSTONED/EXPIRED 终态） ----------


def terminalize_work_item(store: WorkQueueStore, scope_id: str,
                          business_date: str, arrival_seq: int, *,
                          task_state: str, terminal_reason: str,
                          owner_id: str | None, lease_generation: int,
                          now: datetime) -> str:
    """终态收口：CAS 置 ``tombstoned``/``expired`` + ``terminal_reason``。

    - 持约翻态（``lease_generation >= 1``）：fencing 判定（僵尸=
      ``WorkLeaseLost``）；未持约活态=AdmissionConflict（状态机违例）；
    - 无约翻态（``lease_generation == 0``，owner=None）：仅对未持约活态
      合法——逻辑到期闸（``expired``）对 ``pending``/``retry_wait``/
      ``compensating`` 直接收口（受理/claim 侧到期执法面）；对
      ``leased`` 拒绝（活租约不可无约终结——先接管再收口）；
    - 重放幂等：同态同理由+（持约方）代次一致 →
      ``"already_terminalized"``（无约方只看同态同理由——代次对无约
      观察者无意义）；理由分歧 → IdentityConflict；持约方代次被顶替 →
      ``WorkLeaseLost``。
    """
    if task_state not in (WORK_TASK_TOMBSTONED, WORK_TASK_EXPIRED):
        raise ValueError(
            "terminal task_state must be 'tombstoned' or 'expired'")
    if not isinstance(terminal_reason, str) or not terminal_reason:
        raise ValueError("terminal_reason must be a non-empty str")
    if lease_generation >= 1 and (not isinstance(owner_id, str)
                                  or not owner_id):
        raise ValueError(
            "owner_id must be a non-empty str when lease_generation >= 1")
    materialize_state = (MATERIALIZE_TOMBSTONE
                         if task_state == WORK_TASK_TOMBSTONED
                         else MATERIALIZE_EXPIRED)
    index, key = work_coords(scope_id, business_date, arrival_seq)
    for _attempt in range(_CAS_RETRY_LIMIT):
        document = _read(store, index, key)
        if document is None:
            raise AdmissionConflict("work item is absent at terminalize")
        if lease_generation >= 1:
            item = _fenced_current(document, owner_id=owner_id,
                                   lease_generation=lease_generation)
        else:
            item = _validated(document)
            if item.task_state == WORK_TASK_LEASED:
                raise AdmissionConflict(
                    "work item holds a live lease; unleased terminalize "
                    "must recover the lease first")
        if item.task_state in TERMINAL_TASK_STATES:
            if (lease_generation >= 1
                    and item.lease_generation != lease_generation):
                raise WorkLeaseLost(
                    "terminalize superseded: expected generation "
                    f"{lease_generation}, observed {item.lease_generation}",
                    expected_generation=lease_generation,
                    observed_generation=item.lease_generation)
            if (item.task_state != task_state
                    or item.terminal_reason != terminal_reason):
                raise IdentityConflict(
                    "work item terminal state or reason diverges on replay")
            return "already_terminalized"
        updated = {
            **document["source"],
            "task_state": task_state,
            "materialize_state": materialize_state,
            "terminal_reason": terminal_reason,
            "result": None,
            "lease_owner": None,
            "lease_expires_at": None,
            "next_retry_at": None,
            "updated_at": _utc(now),
        }
        try:
            _write_validated(store, index, key, document, updated)
        except _Conflict:
            continue
        return "terminalized"
    raise AdmissionConflict(
        "work item terminalize CAS contention exceeded retry bound")


# ---------- 检索面（search 端口，scan_terminal_tombstone_seqs 同型） ----------


def scan_expired_leases(search: "Callable[[str, dict], dict]",
                        scope_id: str, business_date: str, *,
                        now: datetime, scan_size: int = 10000) -> list[int]:
    """枚举已过期租约的 ``arrival_seq``（升序；recover 的扫描面）。

    与 ``scan_terminal_tombstone_seqs`` 同纪律：
    - ``search``=只读检索端口（物理前缀由调用方包装进端口——
      ``lambda index, body: client.search(index=prefix + index, body=body)``）；
    - 查询=``task_state=leased`` ∧ ``lease_expires_at < now``（注入
      时钟——扫描侧不取墙钟）；
    - 索引缺席（404 形态）=当日无队列 → 空表（fail-closed 不猜）；
      检索端口其他异常上抛；回包/条目形态不识别 → 不计入（保守）。
    """
    index = work_index(business_date)
    body = {
        "size": scan_size,
        "track_total_hits": True,
        "query": {"bool": {"filter": [
            {"term": {"scope_id": scope_id}},
            {"term": {"task_state": WORK_TASK_LEASED}},
            {"range": {"lease_expires_at": {"lt": _utc(now)}}},
        ]}},
        "sort": [{"arrival_seq": "asc"}],
        "_source": ["arrival_seq"],
    }
    try:
        result = search(index=index, body=body)
    except Exception as error:
        status = getattr(error, "status_code", None)
        if type(status) is int and status == 404:
            return []          # 无队列索引=无过期租约（fail-closed 不猜）
        raise
    hits = (result.get("hits", {}) if isinstance(result, dict) else {})
    found = hits.get("hits", []) if isinstance(hits, dict) else None
    if not isinstance(found, list):
        return []
    seqs: list[int] = []
    for hit in found:
        source = hit.get("_source") if isinstance(hit, dict) else None
        seq = source.get("arrival_seq") if isinstance(source, dict) else None
        if type(seq) is int and seq >= 1:
            seqs.append(seq)
    seqs.sort()
    return seqs


__all__ = [
    "WORK_INDEX_PATTERN",
    "WORK_LEASE_DEFAULT_SECONDS",
    "WorkClaimResult",
    "WorkEnqueueResult",
    "WorkLeaseLost",
    "WorkPersistUnknown",
    "WorkQueueStore",
    "claim_work_item",
    "complete_work_item",
    "defer_work_item",
    "enqueue_work_item",
    "load_work_item",
    "recover_expired_lease",
    "scan_expired_leases",
    "terminalize_work_item",
    "work_coords",
    "work_documents",
    "work_index",
    "work_item_id",
]
