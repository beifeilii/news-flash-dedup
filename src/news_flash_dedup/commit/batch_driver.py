"""B+ 批量落锤·编排层驱动器（2026-10-08 施工窗）。

设计合同 = log/temp/batch-commit-impl-contract.md（四段式语义、不变量、
commit_one 写序契约逐条镜像）；存储层 = persist/es_store.py「批量落锤」段
（bulk_create_shells / bulk_cas_flip / refresh_indices）。

commit_batch = K 件四段式编排，镜像 commit_one 写相位（commit/coordinator.py）
的批量版——decide/审计批构造/终态主记录体构造由上游逐件完成（内存，廉价），
本层只驱动写序：

  段1 bulk_create_shells（K 个 not_ready 壳一次 bulk，refresh=False）；
      version_conflict 件走 A5 孤儿壳协议——实时 GET 取壳→核 raw_hash
      身份→真实版本留段 3 走更新路径补翻；壳身份不符 CommitOneError
      （fail-closed，不自动覆盖/回收孤儿壳）。GET 命中已终态（pending/
      held）文档 = 半批崩溃后整批重放形状——按 M-3 对拍：内容一致 =
      幂等跳过（段 3 不重写），分歧 = CommitOneError。
  段2 审计合并一批落库（复用现役 store.write_audit_documents——审计
      先存于终态；其 N36 冲突逐文档对拍通路天然承载审计段重放幂等）。
  段3 bulk_cas_flip（逐动作 if_seq_no/if_primary_term，版本取自段 1 真实
      返回 / A5 GET）；"conflict" 件按 A5/幂等对拍处置：GET 对拍内容
      一致 = 幂等跳过，不一致 = CommitOneError。
  段4 refresh_indices() 一次（批量写 refresh=False 的可见性收口，投递
      扫描最长延迟 = 一个批量窗）+ 水位推进到批内最大连续
      arrival_seq：seq <= 现水位 = 已覆盖（重放形状）幂等跳过；断号
      即止——断裂件照样落库但水位不覆盖，留下批补推进；target <=
      prev 零推进（不重复推进）。

不变量（对合同逐条）：
- INV-2：终态只能由 CAS 从壳翻入——段 1 只建 not_ready 壳（非终态，
  壳先于审计不违 INV-2），段 3 是唯一翻态点；壳闸/终态闸由存储层
  逐件 fail-closed 执法（ValueError 整批不发），本层不复制闸逻辑。
- 审计先存于终态：段 2 严格先于段 3；段 1 身份不符/段 3 对拍分歧
  CommitOneError 时审计尚未落库或已落库但终态未翻——两方向都不掩盖。
- M-3 幂等由「段重放 = 全 conflict + 身份对拍」承载：任一环节
  RuntimeError（AuditPersistError/CASConflictError/存储层段失败）原样
  上抛 = 整段失败（调用侧重放——create 确定性 ID 幂等），本层不捕获
  不包装；CommitOneError = 不可恢复身份/内容分歧，响亮死。
- 水位单调：只进不退。批量版与 commit_one 的差异——commit_one 对
  target < prev 逐件报错，批量版允许 item.arrival_seq <= prev 静默幂等
  （整批重放到水位之后的形状是合同设计内形态：「段内任一失败整段
  重放」+「全 conflict 对拍」）；单调闸由「target > prev 才 advance」
  承载，水位读 = realtime GET 不吃 refresh。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from news_flash_dedup.commit.coordinator import CommitOneError
# W2Fγ（A11 同款消费点声明）：跨模块私有复用 es_store 的
# _WRITE_RETRY_TIME_DERIVED_KEYS 为有意单源合同——M-3 重试幂等对拍的
# 时间派生排除面（completed_at/delivery_deadline_at/next_delivery_at：
# 重试时刻不同恒差，身份语义由 raw_hash/event_id/payload_hash 承载，
# 不参拍）与 persist/es_store.py write_main_record N36 同集同源；
# 导出方公共化/声明归 δ 窗。
from news_flash_dedup.persist.es_store import _WRITE_RETRY_TIME_DERIVED_KEYS


@dataclass(frozen=True)
class BatchCommitItem:
    """commit_batch 单件输入：已 decide + 已构造审计批 + 终态主记录体。

    镜像 commit_one 写相位输入（审计落库前的内存形态）：
    - record_id：确定性主记录 ID（create 幂等的锚）；
    - arrival_seq：水位推进序（段 4 连续前缀计算的输入）；
    - audit_docs：本件审计文档序列（audit record to_doc() 形态，
      段 2 全批合并一次落库；零候选件可为空元组）；
    - body：终态主记录体（P18 §3.1 形态——delivery_state∈
      {pending,held} ∧ task_state=succeeded ∧ audit_complete=True ∧
      raw_hash/event_id/audit_ids 全显式；段 1 壳体由本层派生
      dict(body, delivery_state="not_ready", task_state="accepted")，
      与 cas_main_record 约束 b 内化两阶段同款）。

    字段一致性（record_id == body["record_id"]、body 闸键合法）由
    调用侧保证 + 存储层闸 fail-closed 执法，本层不重复校验。
    """
    record_id: str
    arrival_seq: int
    audit_docs: tuple[Mapping, ...]
    body: Mapping


@dataclass(frozen=True)
class BatchCommitOutcome:
    """commit_batch 输出。

    - flipped_ids：本批新翻终态（段 3 CAS 成功，含 A5 补翻），输入序；
    - idempotent_ids：幂等跳过件（段 1 命中已终态对拍一致 / 段 3
      conflict 对拍一致），零重写，输入序；
    - audit_doc_count：段 2 合并落库的审计文档数（重放时为对拍件数）；
    - watermark_prev：段 4 读到的现水位（realtime GET；空批未读=None）；
    - watermark_advanced_to：实际推进目标；None = 未推进（target <=
      prev，重放形状）；
    - deferred_ids：断裂件（已落库但 arrival_seq 超出连续前缀，水位
      未覆盖，留下批补推进），输入序。
    """
    flipped_ids: tuple[str, ...]
    idempotent_ids: tuple[str, ...]
    audit_doc_count: int
    watermark_prev: int | None
    watermark_advanced_to: int | None
    deferred_ids: tuple[str, ...]


def commit_batch(
    items: Sequence[BatchCommitItem],
    store: Any,
    *,
    scope_id: str,
    business_date: str,
) -> BatchCommitOutcome:
    """K 件四段式批量落锤（store = RealESP18Store 协议件，鸭子类型）。

    依赖 store 协议面：bulk_create_shells / write_audit_documents /
    bulk_cas_flip / refresh_indices / get_main_record / get_watermark /
    advance_watermark。

    scope_id/business_date = 批级水位分区——调用侧按分区收集连续
    前缀成批（合同实施切分 3），批内混分区不在本层合同内。
    """
    items = list(items)
    if not items:
        # 空批 = 零调用（连水位读都不发），全空输出
        return BatchCommitOutcome(
            flipped_ids=(), idempotent_ids=(), audit_doc_count=0,
            watermark_prev=None, watermark_advanced_to=None, deferred_ids=(),
        )

    # ---- 段 1：K 个 not_ready 壳一次 bulk（RuntimeError = 整段失败上抛）----
    shell_result = store.bulk_create_shells([
        (item.record_id,
         dict(item.body, delivery_state="not_ready", task_state="accepted"))
        for item in items
    ])
    # versions：record_id -> 段 3 待用的真实 (seq_no, primary_term)；
    # 段 1 命中已终态且对拍一致的件不进 versions（段 3 不重写）。
    versions: dict[str, tuple[int, int]] = {}
    idempotent_ids: list[str] = []
    for item in items:
        mark = shell_result[item.record_id]
        if mark == "conflict":
            resolved = _resolve_create_conflict(store, item)
            if resolved is None:
                idempotent_ids.append(item.record_id)
            else:
                versions[item.record_id] = resolved
        else:
            versions[item.record_id] = mark

    # ---- 段 2：审计合并一批落库（审计先存于终态；AuditPersistError =
    # RuntimeError 整段失败上抛；其 N36 冲突对拍通路承载重放幂等）----
    audit_docs = [doc for item in items for doc in item.audit_docs]
    store.write_audit_documents(audit_docs)

    # ---- 段 3：K 件 bulk CAS 翻终态（版本取自段 1 / A5 真实版本）----
    flip_result = store.bulk_cas_flip([
        (item.record_id, dict(item.body), *versions[item.record_id])
        for item in items
        if item.record_id in versions
    ])
    flipped_ids: list[str] = []
    for item in items:
        if item.record_id not in versions:
            continue
        mark = flip_result[item.record_id]
        if mark == "conflict":
            _assert_flip_conflict_idempotent(store, item)
            idempotent_ids.append(item.record_id)
        else:
            flipped_ids.append(item.record_id)

    # ---- 段 4：一次显式刷新（可见性收口）+ 水位推进到连续前缀 ----
    store.refresh_indices()
    prev = store.get_watermark(scope_id, business_date) or 0
    target = _contiguous_prefix_target(
        prev, [item.arrival_seq for item in items])
    advanced_to: int | None = None
    if target > prev:
        # advance_watermark 内部 realtime GET + CAS（版本竞争 →
        # CASConflictError = RuntimeError 上抛，调用侧重放幂等收口）
        store.advance_watermark(scope_id, business_date, target)
        advanced_to = target
    deferred_ids = tuple(
        item.record_id for item in items if item.arrival_seq > target)
    return BatchCommitOutcome(
        flipped_ids=tuple(flipped_ids),
        idempotent_ids=tuple(idempotent_ids),
        audit_doc_count=len(audit_docs),
        watermark_prev=prev,
        watermark_advanced_to=advanced_to,
        deferred_ids=deferred_ids,
    )


def _resolve_create_conflict(
        store: Any, item: BatchCommitItem) -> tuple[int, int] | None:
    """段 1 conflict 处置（A5 孤儿壳协议 + 已终态 M-3 对拍）。

    返回 (seq_no, primary_term) = not_ready 孤儿壳的真实版本（段 3
    更新路径补翻）；返回 None = 已终态且内容对拍一致（幂等跳过，
    段 3 不重写）。壳身份不符 / 终态内容分歧 / 意外状态 =
    CommitOneError（fail-closed 不掩盖）。
    """
    existing = store.get_main_record(item.record_id)
    if existing is None:
        # bulk create 报冲突而实时 GET 取不到 = 无法证实身份——按段
        # 失败处置（RuntimeError 上抛，调用侧重放；重放时壳缺席则
        # create 天然成功，自愈）
        raise RuntimeError(
            f"bulk shell conflict for {item.record_id!r} but realtime GET "
            f"misses the doc (cannot verify identity; segment replay)"
        )
    source = existing["source"]
    delivery_state = source.get("delivery_state")
    if delivery_state == "not_ready":
        # A5 ②：核验壳内容身份（raw_hash）——不符按损坏数据处置
        # （写侧 fail-closed，不自动覆盖/回收孤儿壳）
        if source.get("raw_hash") != item.body.get("raw_hash"):
            raise CommitOneError(
                f"shell identity mismatch for {item.record_id!r}: stored "
                f"raw_hash={source.get('raw_hash')!r} vs incoming "
                f"{item.body.get('raw_hash')!r} (A5 fail-closed)"
            )
        # A5 ①③：取真实版本，段 3 更新路径补翻
        return (int(existing["seq_no"]), int(existing["primary_term"]))
    if delivery_state in ("pending", "held"):
        # 已终态 = 半批崩溃（翻态后）整批重放形状——M-3 对拍：
        # 一致 = 幂等跳过零重写；分歧 = 冲突不掩盖
        divergent = _retry_divergent_keys(source, item.body)
        if divergent:
            raise CommitOneError(
                f"existing terminal record {item.record_id!r} diverges on "
                f"{divergent} (batch M-3 idempotency refused)"
            )
        return None
    raise CommitOneError(
        f"existing record {item.record_id!r} has unexpected "
        f"delivery_state={delivery_state!r} on shell conflict "
        f"(A5 fail-closed)"
    )


def _assert_flip_conflict_idempotent(store: Any, item: BatchCommitItem) -> None:
    """段 3 conflict 处置：实时 GET 对拍——内容一致 = 幂等跳过（并发
    翻态者与本批同内容，零重写收口）；不一致 = CommitOneError。"""
    existing = store.get_main_record(item.record_id)
    if existing is None:
        raise RuntimeError(
            f"bulk CAS conflict for {item.record_id!r} but realtime GET "
            f"misses the doc (segment replay)"
        )
    divergent = _retry_divergent_keys(existing["source"], item.body)
    if divergent:
        raise CommitOneError(
            f"existing record {item.record_id!r} diverges on {divergent} "
            f"(batch M-3 idempotency refused)"
        )


def _retry_divergent_keys(source: Mapping, body: Mapping) -> list[str]:
    """M-3 重试幂等对拍：attempted body 键集对已存 _source 逐值比较，
    返回分歧键名列表（空 = 一致可幂等）。时间派生三键不参拍（与
    persist/es_store.py write_main_record N36 同集同源单源）。"""
    return [key for key, value in body.items()
            if key not in _WRITE_RETRY_TIME_DERIVED_KEYS
            and source.get(key) != value]


def _contiguous_prefix_target(prev: int, seqs: Sequence[int]) -> int:
    """批内最大连续 arrival_seq（水位推进目标）。

    自 prev + 1 起逐件连号推进：seq <= prev = 水位已覆盖（重放形状）
    或批内重复，跳过；断号即止——其后各件落库照成但水位不覆盖
    （断裂件留下批）。target == prev = 无可推进（零推进幂等）。
    """
    covered = prev
    for seq in sorted(seqs):
        if seq <= covered:
            continue
        if seq != covered + 1:
            break
        covered = seq
    return covered


__all__ = [
    "BatchCommitItem",
    "BatchCommitOutcome",
    "commit_batch",
]
