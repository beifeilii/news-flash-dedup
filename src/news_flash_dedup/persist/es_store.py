"""P18 真 ES 仓储（UAT 接入层）。

19:58 主窗口亲笔落位：草案审定自 `log/temp/p18-uat-wiring-proposal.md` §三
（T2 子窗口起草，主窗口审 diff 全读后采纳；★4/5/7/8 裁定见 目标文档.md 决策日志）。

按 05:25 批复 + 修订 1/2 + T1（P17-3）真集群经验：
- 隔离前缀 p18-batch-<run_uuid>-news-dedup-(items|audits|control)-v1-YYYY.MM.DD（修订 1）；
- action.auto_create_index=-*：构造时显式幂等建索引（commit/es_store.py 19:16 先例）；
- (0,0) 首创建映射 op_type=create（19:12）；ES _seq_no 0 基（19:17）；
- watermark 返回新水位值 arrival_seq（05:34 裁定，19:28）；
- delivery_state 首建 not_ready → CAS 成功翻 pending；CAS 失败不动（修订 2 / INV-2）；
  10-07 令（甲-i）decision != "不重复" → CAS 落 held（系统内扣留不投递）；
- 审计先存 bulk create，失败即停（§2.3，P18-INV-1/INV-6）。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any

from news_flash_dedup import admission as _admission
from news_flash_dedup.deadline import deadline_iso as _deadline_iso
from news_flash_dedup.es_client import assert_test_environment
from news_flash_dedup.persist.coordinator import P18PersistError


P18_UAT_ENV_FLAG = "P18_CONFIRM_UAT"
P18_INDEX_PREFIX_PATTERN = re.compile(
    r"^p18-batch-[A-Za-z0-9_-]+-news-dedup-(items|audits|control)-v1-\d{4}\.\d{2}\.\d{2}$"
)


class P18UATGatesNotOpen(RuntimeError):
    """`P18_CONFIRM_UAT=1` 缺失或环境门未满足。"""


class P18IndexPrefixInvalid(ValueError):
    """隔离索引前缀不匹配 `p18-batch-...` 硬正则闸门。"""


class AuditPersistError(RuntimeError):
    """审计先存失败（bulk create 任一冲突/错误）——CAS 前置，绝不掩盖。"""


class CASConflictError(RuntimeError):
    """主记录/水位 CAS 版本竞争冲突——写入未发生，delivery_state 不动（INV-2）。"""


# W2Fγ（A3）：受理壳 task_state 词表（对拍 admission.py 首建词汇——
# accepted/running/failed；pending/succeeded 禁直建，pending 只能由
# cas_main_record 翻入）。
_CREATE_TASK_STATES = frozenset({"accepted", "running", "failed"})


def assert_p18_uat_open(env: Mapping[str, str] | None = None) -> None:
    """P18 闸门：`P18_CONFIRM_UAT=1` 必须设置；`assert_test_environment` 通过。"""
    env = env if env is not None else os.environ
    if env.get(P18_UAT_ENV_FLAG) != "1":
        raise P18UATGatesNotOpen(
            f"P18 requires {P18_UAT_ENV_FLAG}=1; got {env.get(P18_UAT_ENV_FLAG)!r}"
        )
    assert_test_environment(env=dict(env))


def build_p18_index_prefix(run_uuid: str, kind: str, business_date: date) -> str:
    """P18 隔离索引名：p18-batch-<run>-news-dedup-<kind>-v1-YYYY.MM.DD（修订 1）。"""
    if kind not in {"items", "audits", "control"}:
        raise ValueError("kind must be 'items', 'audits' or 'control'")
    if not run_uuid or not re.fullmatch(r"[A-Za-z0-9_-]+", run_uuid):
        raise ValueError("run_uuid must match [A-Za-z0-9_-]+")
    dotted = business_date.strftime("%Y.%m.%d")
    return f"p18-batch-{run_uuid}-news-dedup-{kind}-v1-{dotted}"


def validate_p18_index_name(index_name: str) -> None:
    """P18 索引名必须严格匹配硬正则；192 保留索引零触碰。"""
    if not P18_INDEX_PREFIX_PATTERN.fullmatch(index_name):
        raise P18IndexPrefixInvalid(
            f"index {index_name!r} does not match "
            f"p18-batch-<run>-news-dedup-(items|audits|control)-v1-YYYY.MM.DD"
        )


@dataclass(frozen=True)
class RealESP18Config:
    """P18 真 ES 接入配置。"""
    run_uuid: str
    business_date: date
    items_index: str = field(init=False)
    audits_index: str = field(init=False)
    control_index: str = field(init=False)

    def __post_init__(self) -> None:
        items = build_p18_index_prefix(self.run_uuid, "items", self.business_date)
        audits = build_p18_index_prefix(self.run_uuid, "audits", self.business_date)
        control = build_p18_index_prefix(self.run_uuid, "control", self.business_date)
        for name in (items, audits, control):
            validate_p18_index_name(name)
        object.__setattr__(self, "items_index", items)
        object.__setattr__(self, "audits_index", audits)
        object.__setattr__(self, "control_index", control)


class RealESP18Store:
    """真 ES P18 仓储（UAT 接入）。

    与 FakeP18Store 协议对齐 + commit_one 接线协议（方案 B）：
    write_audit_batch / write_audit_documents / create_main_record /
    cas_main_record / get_main_record / get_audit_doc / write_main_record /
    get_watermark / advance_watermark / snapshot_index_names / cleanup。
    N36（挂账分流核对表 L109）：main_records 重试幂等对拍视图 +
    write_audit_documents 冲突捕获面 + write_main_record 冲突语义映射
    （三路对齐 commit_one M-3 真层幂等重试语义）。
    """

    def __init__(self, client: Any, config: RealESP18Config) -> None:
        assert_p18_uat_open()
        self.client = client
        self.config = config
        # 约束 a：本集群 action.auto_create_index=-*，写入前显式幂等建索引（19:16 先例）
        self._ensure_indices()

    def _ensure_indices(self) -> None:
        for name in (self.config.items_index, self.config.audits_index,
                     self.config.control_index):
            if not self.client.indices.exists(index=name):
                self.client.indices.create(index=name)

    # ---------- 审计先存（§2.3，P18-INV-1/INV-6） ----------

    def write_audit_batch(self, audit_records: Iterable[Mapping],
                          *, created_at: str | None = None) -> list[str]:
        """bulk `create` 写审计文档；任一冲突/错误 → AuditPersistError（失败即停）。

        约束 b：create 语义 = 不存在才创建；重放同 comparison_id 必
        version_conflict（恢复路径据此判"已存"，见 UAT 场景 5a）。
        """
        actions: list[dict] = []
        for record in audit_records:
            doc = dict(record)
            doc.setdefault(
                "created_at",
                created_at or datetime.now(timezone.utc).isoformat(),
            )
            doc.setdefault("decision_artifact_version", "1.0")
            actions.append({"create": {"_index": self.config.audits_index,
                                       "_id": record["comparison_id"]}})
            actions.append(doc)
        if not actions:
            return []
        response = self.client.bulk(operations=actions, refresh="wait_for")
        ids: list[str] = []
        for item in response.get("items", []):
            outcome = item.get("create", {})
            if outcome.get("error"):
                raise AuditPersistError(
                    f"audit bulk create failed: {outcome['error']}"
                )
            ids.append(outcome.get("_id", ""))
        return ids

    def get_audit_doc(self, comparison_id: str) -> dict | None:
        """实时 GET 审计文档；None = 不存在。"""
        from elasticsearch import NotFoundError

        try:
            response = self.client.get(index=self.config.audits_index,
                                       id=comparison_id, realtime=True)
        except NotFoundError:
            return None
        return {
            "source": response["_source"],
            "seq_no": int(response["_seq_no"]),
            "primary_term": int(response["_primary_term"]),
        }

    # ---------- 主记录（§3，修订 2 / INV-2） ----------

    def create_main_record(self, record_id: str, body: Mapping) -> tuple[int, int]:
        """首建主记录（op_type=create）；返回 (seq_no, primary_term)（0 基，约束 c）。

        修订 2：首建必须 delivery_state=not_ready（受理壳 task_state 词汇对拍
        admission.py：accepted/running/failed 均可，禁 pending/succeeded 直建——
        pending 只能由 cas_main_record 翻入）。重复 ID → CASConflictError。

        W2Fγ（A3）：task_state 词表闸补码（docstring 声称已久、码无）——
        缺失/词表外一律 ValueError fail-closed。
        """
        if body.get("delivery_state") != "not_ready":
            raise ValueError(
                f"create main record requires delivery_state=not_ready; "
                f"got {body.get('delivery_state')!r}"
            )
        if body.get("task_state") not in _CREATE_TASK_STATES:
            raise ValueError(
                f"create main record requires task_state in "
                f"accepted|running|failed (受理壳词汇; pending/succeeded "
                f"禁直建); got {body.get('task_state')!r}"
            )
        if "audit_complete" not in body:
            raise ValueError("create main record requires explicit audit_complete")
        from elasticsearch import ConflictError

        try:
            response = self.client.index(
                index=self.config.items_index, id=record_id,
                document=dict(body), op_type="create", refresh="wait_for",
            )
        except ConflictError as exc:
            raise CASConflictError(
                f"main record {record_id!r} already exists (create conflict)"
            ) from exc
        return int(response.get("_seq_no", 0)), int(response.get("_primary_term", 1))

    def cas_main_record(self, record_id: str, body: Mapping, *,
                        expected_seq_no: int, expected_primary_term: int) -> int:
        """CAS 写主记录；返回真实新 _seq_no（0 基，约束 c）。

        前置四校验（fake 语义等效执法 + 真机强化）：
        ① delivery_state ∈ {pending, held}（10-07 令甲-i 白名单）；
        ② {pending, held} ⇒ task_state=succeeded；
        ③ audit_complete=True；④ 更新路径 raw_hash 与已存文档实时 GET 对拍
        （fake 层仅构造侧保证；真 ES 可真实执法，见建议书 §六-7，主窗口裁定纳入）。
        约束 b：(0,0) = 首创建——内部两阶段 create(not_ready) + CAS(pending)
        （修订 2 生命周期内化，INV-2 结构性满足，★4 主窗口裁定认可）。
        版本竞争 → CASConflictError，delivery_state 不动（INV-2）。

        W2Fγ（A5 (0,0) 孤儿壳恢复指引）：约束 b 两阶段（create_main_record
        落 not_ready 壳 → 本函数更新路径 CAS 翻 pending）之间崩溃会残留
        not_ready 孤儿壳；此时重试 (0,0) 必撞 create 冲突（壳已存在）——
        恢复方不得改版本强撞，应：① get_main_record 实时 GET 取壳的真实
        (seq_no, primary_term)；② 核验壳内容身份（raw_hash/事件身份一致）；
        ③ 以真实版本走更新路径 CAS 翻 pending（与 T2 演练"create 冲突挡下
        即已存、仅补标"同族处置）。壳内容身份不符按损坏数据处置（人工
        介入，本层不自动覆盖/回收孤儿壳——写侧 fail-closed）。
        """
        # 10-07 用户令（甲-i）：扣留件 held 亦经 CAS 创建落点写入——闸①
        # 单值 delivery_state=pending 扩 {pending, held} 双值白名单；闸②
        # 对称扩为 {pending,held} ⇒ task_state=succeeded（扣留件同为终态
        # succeeded）。held 无运行时翻入路径（delivery 侧 mark_state/
        # _DELIVERY_STATES 词表闸不扩——防误翻守卫，红测 G6-3/G6-4 双钉）。
        if body.get("delivery_state") not in ("pending", "held"):
            raise ValueError(
                f"CAS main record requires delivery_state=pending|held "
                f"(放行/扣留白名单); got {body.get('delivery_state')!r}"
            )
        if body.get("task_state") != "succeeded":
            raise ValueError(
                f"CAS main record with delivery_state=pending|held requires "
                f"task_state=succeeded; got {body.get('task_state')!r}"
            )
        if not body.get("audit_complete"):
            raise ValueError("audit_complete=False; CAS refused")
        if expected_seq_no == 0 and expected_primary_term == 0:
            # 约束 b + 修订 2：(0,0) 首创建 → op_type=create 受理壳 → 立即翻 pending
            shell = dict(body, delivery_state="not_ready", task_state="accepted")
            expected_seq_no, expected_primary_term = self.create_main_record(
                record_id, shell,
            )
        else:
            existing = self.get_main_record(record_id)
            if existing is None:
                raise CASConflictError(
                    f"main record {record_id!r} not found for CAS update"
                )
            stored_hash = existing["source"].get("raw_hash")
            if stored_hash != body.get("raw_hash"):
                raise ValueError(
                    f"raw_hash mismatch on CAS: stored {stored_hash!r} "
                    f"vs incoming {body.get('raw_hash')!r}"
                )
        from elasticsearch import ConflictError

        try:
            response = self.client.index(
                index=self.config.items_index, id=record_id, document=dict(body),
                if_seq_no=expected_seq_no, if_primary_term=expected_primary_term,
                refresh="wait_for",
            )
        except ConflictError as exc:
            raise CASConflictError(
                f"main record {record_id!r} CAS conflict at "
                f"seq_no={expected_seq_no} (delivery_state untouched)"
            ) from exc
        return int(response.get("_seq_no", 0))

    def get_main_record(self, record_id: str) -> dict | None:
        """实时 GET 主记录；None = 不存在。"""
        from elasticsearch import NotFoundError

        try:
            response = self.client.get(index=self.config.items_index,
                                       id=record_id, realtime=True)
        except NotFoundError:
            return None
        return {
            "source": response["_source"],
            "seq_no": int(response["_seq_no"]),
            "primary_term": int(response["_primary_term"]),
        }

    # ---------- 批量落锤（B+ 第二步，2026-10-08 主窗口） ----------
    #
    # 设计=log\temp\batch-commit-impl-contract.md：四段式（bulk 壳 → bulk 审计
    # →bulk CAS 翻终态 → 整段确认后一次显式刷新+水位推进），INV-2 两阶段
    # 壳语义不绕过——终态仍只能由 CAS 从壳翻入（段 1 建壳/段 3 翻壳），前置
    # 闸与单件路径逐件同款。批量函数 refresh=False，可见性由段 4 的
    # refresh_indices() 统一承担（投递扫描最长延迟=一个批量窗；水位读全走
    # realtime GET 不吃 refresh）。串行单件路径（create_main_record /
    # cas_main_record / write_audit_batch）语义一字不改。

    def bulk_create_shells(
            self, shells: Iterable[tuple[str, Mapping]]) -> dict[str, object]:
        """段 1：一次 bulk create 批量 not_ready 壳（refresh=False）。

        入参=(record_id, body) 序列；body 闸与 create_main_record 同款
        （delivery_state=not_ready ∧ task_state∈受理壳词汇 ∧
        显式 audit_complete 键），任一不符 ValueError fail-closed（整批
        不发）。返回 {record_id: (seq_no, primary_term)}；
        version_conflict → 值="conflict"（调用侧走 A5 孤儿壳协议：
        实时 GET 取壳→核身份→更新路径补翻）；其余错误 → RuntimeError
        整段失败（调用侧按现役幂等语义整段重放——create 确定性 ID
        天然幂等）。
        """
        actions: list[dict] = []
        ids: list[str] = []
        for record_id, body in shells:
            if body.get("delivery_state") != "not_ready":
                raise ValueError(
                    f"bulk shell requires delivery_state=not_ready; "
                    f"got {body.get('delivery_state')!r}")
            if body.get("task_state") not in _CREATE_TASK_STATES:
                raise ValueError(
                    f"bulk shell requires task_state in accepted|running|"
                    f"failed; got {body.get('task_state')!r}")
            if "audit_complete" not in body:
                raise ValueError("bulk shell requires explicit audit_complete")
            actions.append({"create": {"_index": self.config.items_index,
                                       "_id": record_id}})
            actions.append(dict(body))
            ids.append(record_id)
        if not actions:
            return {}
        response = self.client.bulk(operations=actions, refresh=False)
        result: dict[str, object] = {}
        for record_id, item in zip(ids, response.get("items", [])):
            outcome = item.get("create", {})
            error = outcome.get("error")
            if error:
                if outcome.get("status") == 409:
                    result[record_id] = "conflict"
                    continue
                raise RuntimeError(
                    f"bulk shell create failed for {record_id!r}: {error}")
            result[record_id] = (int(outcome.get("_seq_no", 0)),
                                 int(outcome.get("_primary_term", 1)))
        return result

    def bulk_cas_flip(
            self, updates: Iterable[tuple[str, Mapping, int, int]],
    ) -> dict[str, object]:
        """段 3：一次 bulk index 批量 CAS 翻终态（refresh=False）。

        入参=(record_id, body, expected_seq_no, expected_primary_term)；
        前置闸逐件同 cas_main_record（delivery_state∈{pending,held} ⇒
        task_state=succeeded ∧ audit_complete=True）——任一不符 ValueError
        fail-closed（整批不发）。仅更新路径：expected 版本必须来自段 1
        真实返回值（批量版 (0,0) 首创建语义由段 1 承担，本函数不做）。
        返回 {record_id: new_seq_no}；"conflict"=版本竞争（调用侧按
        A5/现役重放语义处置）；其余错误 → RuntimeError 整段失败。
        """
        actions: list[dict] = []
        ids: list[str] = []
        for record_id, body, expected_seq_no, expected_primary_term in updates:
            if body.get("delivery_state") not in ("pending", "held"):
                raise ValueError(
                    f"bulk CAS requires delivery_state=pending|held; "
                    f"got {body.get('delivery_state')!r}")
            if body.get("task_state") != "succeeded":
                raise ValueError(
                    f"bulk CAS with delivery_state=pending|held requires "
                    f"task_state=succeeded; got {body.get('task_state')!r}")
            if not body.get("audit_complete"):
                raise ValueError("audit_complete=False; bulk CAS refused")
            actions.append({
                "index": {"_index": self.config.items_index, "_id": record_id,
                          "if_seq_no": expected_seq_no,
                          "if_primary_term": expected_primary_term}})
            actions.append(dict(body))
            ids.append(record_id)
        if not actions:
            return {}
        response = self.client.bulk(operations=actions, refresh=False)
        result: dict[str, object] = {}
        for record_id, item in zip(ids, response.get("items", [])):
            outcome = item.get("index", {})
            error = outcome.get("error")
            if error:
                if outcome.get("status") == 409:
                    result[record_id] = "conflict"
                    continue
                raise RuntimeError(
                    f"bulk CAS flip failed for {record_id!r}: {error}")
            result[record_id] = int(outcome.get("_seq_no", 0))
        return result

    def refresh_indices(self) -> None:
        """段 4 前置：批量末显式刷新三索引（items/audits/control）——
        批量写均 refresh=False，搜索可见性由本调用统一承担（水位/重试
        读全走 realtime GET，不经刷新）。"""
        self.client.indices.refresh(index=",".join(
            (self.config.items_index, self.config.audits_index,
             self.config.control_index)))

    # ---------- commit_one 接线协议（方案 B） ----------

    def write_audit_documents(self, audit_records: Iterable[Mapping]) -> list[str]:
        """commit_one 现役审计落库契约（批量主分支）的真 ES 实现。

        窗口Y（R-W-01）接线回归修复：commit/coordinator.py L191-199 审计落库
        双态契约优先 `write_audit_documents(docs)`（R9 外部审核 F4 修复激活真
        审计批后该路径实质生效）；本仓储旧名 write_audit_batch 两态均不匹配，
        hasattr 探针落空、回退分支 write_audit 不存在 → AttributeError
        （P18 `test_uat_commit_one_real_es_wiring` 红，窗口W 五站复测实证）。

        最小语义保真：委托现役 write_audit_batch 同一体——bulk `create`
        （审计不可覆盖）、任一冲突/错误 → AuditPersistError（失败即停）、
        文档 enrichment（created_at/decision_artifact_version）与返回 id
        列表语义逐字节不变；write_audit_batch 旧名保留（persist_main_record_es
        与 P18 UAT 直调路径零影响）。

        N36（挂账分流核对表 L109，E 批 B4 施工面并入）：bulk create 冲突
        （AuditPersistError）入捕获面——同内容重试逐文档实时 GET 对拍：
        全部已存且 attempted 键集逐值一致 → 幂等返回 comparison_ids
        （顺序同输入，与成功路径同形）；任一缺失 → 原 AuditPersistError
        重抛不掩盖；任一分歧 → persist.divergence.AuditDivergenceError
        带分歧键明细（fail-closed，对齐 W2Fγ A5 分歧处置纪律；N38=
        CommitOneError∧AuditPersistError 双契约子型，worker 捕获面受控
        failed 停不冒泡响亮死）。
        """
        records = list(audit_records)
        try:
            return self.write_audit_batch(records)
        except AuditPersistError as original:
            ids: list[str] = []
            for record in records:
                comparison_id = record["comparison_id"]
                try:
                    existing = self.get_audit_doc(comparison_id)
                except Exception:
                    # 核验读自身不可用（瘦 spy 仓储/读故障）→无法证实幂等，
                    # 原错误重抛不掩盖（"失败即停"语义保持）
                    raise original
                if existing is None:
                    raise original
                source = existing["source"]
                divergent = [key for key in record
                             if source.get(key) != record[key]]
                if divergent:
                    # N38：分歧=CommitOneError 语义子型（worker 捕获面受控
                    # failed）∧ AuditPersistError 契约面；惰性导入——
                    # persist↔commit 导入环规避见 persist/divergence.py 注
                    from news_flash_dedup.persist.divergence import (
                        AuditDivergenceError,
                    )
                    raise AuditDivergenceError(
                        f"audit doc {comparison_id!r} diverges on retry: "
                        f"{divergent}"
                    ) from original
                ids.append(comparison_id)
            return ids

    def write_main_record(self, record: Any, *, scope_id: str,
                          business_date: str, arrival_seq: int) -> str:
        """FakeCommitStore.write_main_record 协议的真 ES 实现（P17 瘦记录映射 + 两阶段内化）。

        N36（挂账分流核对表 L109，E 批 B4 施工面并入）：create 冲突
        （CASConflictError）语义映射 commit_one 通路③重试幂等——冲突后实时
        GET 对拍稳定键集（completed_at/delivery_deadline_at/next_delivery_at
        时间派生三键除外，与 commit/coordinator.py:71-80 _RETRY_STABLE_FIELDS
        排除面同源）：一致 → 幂等返回 record_id 零写入；分歧（含孤儿壳
        not_ready/accepted——内容与终态体恒异）→ persist.divergence
        .MainRecordDivergenceError 带分歧键明细（fail-closed，不掩盖；
        N38=CommitOneError∧CASConflictError 双契约子型，worker 捕获面受控
        failed 停不冒泡响亮死；真并发 CAS 冲突保持裸 CASConflictError
        冒泡）。
        """
        body = _record_to_body(record, scope_id=scope_id,
                               business_date=business_date, arrival_seq=arrival_seq)
        try:
            self.cas_main_record(record.record_id, body,
                                 expected_seq_no=0, expected_primary_term=0)
        except CASConflictError as original:
            try:
                existing = self.get_main_record(record.record_id)
            except Exception:
                # 核验读自身不可用→无法证实幂等，原错误重抛不掩盖
                raise original
            if existing is None:
                raise original
            source = existing["source"]
            divergent = [key for key, value in body.items()
                         if key not in _WRITE_RETRY_TIME_DERIVED_KEYS
                         and source.get(key) != value]
            if divergent:
                # N38：分歧=CommitOneError 语义子型（worker 捕获面受控
                # failed）∧ CASConflictError 契约面；惰性导入——persist↔commit
                # 导入环规避见 persist/divergence.py 注；真并发冲突仍走
                # cas_main_record 裸 CASConflictError 冒泡
                from news_flash_dedup.persist.divergence import (
                    MainRecordDivergenceError,
                )
                raise MainRecordDivergenceError(
                    f"main record {record.record_id!r} diverges on retry: "
                    f"{divergent}"
                ) from original
            return record.record_id
        return record.record_id

    @property
    def main_records(self) -> Any:
        """commit_one 通路③重试幂等对拍视图（N36，挂账分流核对表 L109）。

        对拍 FakeCommitStore.main_records 协议（.get(record_id) → 快照|None）：
        实时 GET 主记录 → _MainRecordSnapshot 适配——_RETRY_STABLE_FIELDS
        十七属性同义（decision=result.decision、duplicate_ids/reason 取
        result 子键、payload_hash=callback_body_hash、internal_code=
        reason_code、expires_at 缺省空串、audit_ids/duplicate_ids 元组化）。
        只读视图；零写语义。
        """
        return _MainRecordsView(self)

    # ---------- decision_watermark（§4，05:34 裁定） ----------

    def get_watermark(self, scope_id: str, business_date: str) -> int | None:
        """实时 GET 水位；None = 未推进过。"""
        from elasticsearch import NotFoundError

        key = f"{scope_id}|{business_date}"
        try:
            response = self.client.get(index=self.config.control_index,
                                       id=key, realtime=True)
        except NotFoundError:
            return None
        return int(response["_source"].get("decision_watermark_seq", 0))

    def advance_watermark(self, scope_id: str, business_date: str,
                          arrival_seq: int) -> int:
        """推进 decision_watermark_seq；返回新水位值 arrival_seq（约束 d）。

        回退（arrival_seq <= prev）→ ValueError（对拍 fake；P17 真仓储为
        RuntimeError——★8 主窗口裁定：统一登记卫生项归 T7，本站取 ValueError）。
        首推进 op_type=create（约束 b）。
        """
        from elasticsearch import ConflictError, NotFoundError

        key = f"{scope_id}|{business_date}"
        doc = {
            "scope_id": scope_id, "business_date": business_date,
            "decision_watermark_seq": arrival_seq,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            current = self.client.get(index=self.config.control_index,
                                      id=key, realtime=True)
        except NotFoundError:
            try:
                self.client.create(index=self.config.control_index, id=key,
                                   document=doc, refresh="wait_for")
            except ConflictError as exc:
                raise CASConflictError(
                    f"watermark {key!r} create conflict"
                ) from exc
            return arrival_seq
        prev = int(current["_source"].get("decision_watermark_seq", 0))
        if arrival_seq <= prev:
            raise ValueError(
                f"decision_watermark cannot regress: {prev} -> {arrival_seq}"
            )
        try:
            self.client.index(
                index=self.config.control_index, id=key, document=doc,
                if_seq_no=int(current["_seq_no"]),
                if_primary_term=int(current["_primary_term"]),
                refresh="wait_for",
            )
        except ConflictError as exc:
            raise CASConflictError(f"watermark {key!r} CAS conflict") from exc
        return arrival_seq

    # ---------- UAT 运维（公共标尺 3/4） ----------

    def snapshot_index_names(self) -> list[str]:
        response = self.client.cat.indices(format="json", h="index")
        return sorted(item.get("index", "") for item in response)

    def cleanup(self) -> list[str]:
        """仅删本 run 前缀 `p18-batch-<run_uuid>-`（与 T1 同款更窄 run 作用域）。"""
        run_prefix = f"p18-batch-{self.config.run_uuid}-"
        targets = sorted(n for n in self.snapshot_index_names()
                         if n.startswith(run_prefix))
        for name in targets:
            self.client.indices.delete(index=name)
        return targets


def _record_to_body(record: Any, *, scope_id: str, business_date: str,
                    arrival_seq: int) -> dict:
    """FakeMainRecord（P17 瘦记录）→ P18 §3.1 body 映射。

    已知降级（建议书 §六-6）：P17 骨架 FakeMainRecord 无 callback_body（仅
    payload_hash），映射为 callback_body="" + callback_body_hash=payload_hash；
    五字段冻结真值由 P18 persist 路径保证，接线演示不主张 INV-4 现场复算
    （字段补全与挂账 #7 audit_ids 同族，归 T4 核对）。
    """
    callback_body = getattr(record, "callback_body", "") or ""
    callback_body_hash = (
        getattr(record, "callback_body_hash", "")
        or getattr(record, "payload_hash", "")
    )
    completed_at = (getattr(record, "completed_at", "")
                    or datetime.now(timezone.utc).isoformat())
    result = {
        "item_id": record.item_id,
        "decision": record.decision,
        "duplicate_ids": list(record.duplicate_ids),
        "reason": record.reason,
    }
    return {
        "record_id": record.record_id,
        "item_id": record.item_id,
        "text": record.text,
        "scope_id": scope_id,
        "business_date": business_date,
        "arrival_seq": arrival_seq,
        "raw_hash": record.raw_hash,
        "pipeline_version": record.pipeline_version,
        "fact_artifact_hash": "",
        "vector_state": "pending",                  # 05:28 裁定：创建者写入
        "result": result,
        "result_item_id": record.item_id,
        "result_decision": record.decision,
        "result_duplicate_ids": list(record.duplicate_ids),
        "result_reason": record.reason,
        "task_state": "succeeded",
        "completed_at": completed_at,
        "result_version": record.result_version,
        "event_id": record.event_id,
        "audit_ids": list(record.audit_ids),
        "audit_complete": record.audit_complete,
        "reason_code": getattr(record, "internal_code", ""),
        "callback_body": callback_body,
        "callback_body_hash": callback_body_hash,
        "delivery_state": record.delivery_state,
        "delivery_deadline_at": record.delivery_deadline_at,
        "next_delivery_at": completed_at,
        "callback_attempts": record.callback_attempts,
        "round_attempts": 0,
        "round": 1,
        # P2（C-08，窗口X 并线）：expires_at 仅非空时落 key——默认空串不改
        # 现状 body 形状
        **({"expires_at": record.expires_at}
           if getattr(record, "expires_at", "") else {}),
    }


# N36（挂账分流核对表 L109）：write_main_record 重试幂等对拍的时间派生三键
# （与 commit/coordinator.py:71-80 _RETRY_STABLE_FIELDS 排除面同源——
# completed_at/delivery_deadline_at 为时刻派生，next_delivery_at 由
# completed_at 派生，重试时刻不同恒差，身份语义已由 payload_hash/event_id
# 承载，不参拍）。
_WRITE_RETRY_TIME_DERIVED_KEYS = frozenset({
    "completed_at", "delivery_deadline_at", "next_delivery_at"})


class _MainRecordSnapshot:
    """实时 GET 主记录 _source → FakeMainRecord 稳定字段协议适配（只读）。"""

    __slots__ = (
        "record_id", "item_id", "text", "decision", "duplicate_ids",
        "reason", "payload_hash", "delivery_state", "callback_attempts",
        "audit_ids", "audit_complete", "raw_hash", "pipeline_version",
        "result_version", "event_id", "internal_code", "expires_at",
    )

    def __init__(self, source: Mapping) -> None:
        result = source.get("result")
        result = result if isinstance(result, Mapping) else {}
        self.record_id = source.get("record_id")
        self.item_id = source.get("item_id")
        self.text = source.get("text")
        self.decision = result.get("decision")
        self.duplicate_ids = tuple(result.get("duplicate_ids") or ())
        self.reason = result.get("reason")
        self.payload_hash = source.get("callback_body_hash", "")
        self.delivery_state = source.get("delivery_state")
        self.callback_attempts = source.get("callback_attempts", 0)
        self.audit_ids = tuple(source.get("audit_ids") or ())
        self.audit_complete = source.get("audit_complete", False)
        self.raw_hash = source.get("raw_hash")
        self.pipeline_version = source.get("pipeline_version")
        self.result_version = source.get("result_version")
        self.event_id = source.get("event_id")
        self.internal_code = source.get("reason_code", "")
        self.expires_at = source.get("expires_at", "")


class _MainRecordsView:
    """RealESP18Store.main_records 视图（N36）：实时 GET 适配，只读零写。"""

    __slots__ = ("_store",)

    def __init__(self, store: "RealESP18Store") -> None:
        self._store = store

    def get(self, record_id: str, default: Any = None) -> Any:
        doc = self._store.get_main_record(record_id)
        if doc is None:
            return default
        return _MainRecordSnapshot(doc["source"])


def persist_main_record_es(
    decide: Any,
    store: RealESP18Store,
    *,
    record_id: str,
    audit_records: Iterable[Mapping],
    scope_id: str,
    business_date: str,
    arrival_seq: int,
    raw_hash: str,
    audit_complete: bool,  # 窗口V（M-01 同标准）：去默认强制关键字显式表态
    fact_artifact_hash: str = "",
    pipeline_version: str = "dedup_v1",
    expires_at: str | None = None,   # P2 C-04/C-08（窗口X 并线）：None=现状（不钳制不落 key）
    request_id: str | None = None,   # P2 P17-3 前置（窗口X 并线）：None=record_id 回退不变
) -> Mapping:
    """P18 真 ES 提交协调（对拍 persist.coordinator.persist_main_record 流水线）。

    1. audit_complete=False → P18PersistError（CAS 前置，绝不掩盖）；
    2. 审计先存 write_audit_batch（失败即停 → AuditPersistError 上抛，CAS 不发起）；
    3. 主记录两阶段（修订 2）：cas_main_record(0,0) 内化 create(not_ready)→翻 pending；
    4. 推进 decision_watermark（CAS 之后、回调之前；§4.2）。
    """
    if not audit_complete:
        raise P18PersistError("audit_complete=False; CAS refused (no write action)")
    audit_ids = store.write_audit_batch(audit_records)

    callback_body = json.dumps(decide.to_public_dict(), sort_keys=True,
                               ensure_ascii=False)
    # W2Fγ（WA4b-35 双 now 收口）：completed_at 与 delivery_deadline_at
    # 同源单一 now（与 persist/coordinator 同款收口）。
    now = datetime.now(timezone.utc)
    completed_at = now.isoformat()
    body = {
        "record_id": record_id,
        "item_id": decide.item_id,
        "text": decide.text,
        "scope_id": scope_id,
        "business_date": business_date,
        "arrival_seq": arrival_seq,
        "raw_hash": raw_hash,
        "pipeline_version": pipeline_version,
        "fact_artifact_hash": fact_artifact_hash,
        "vector_state": "pending",
        "result": decide.to_public_dict(),
        "result_item_id": decide.item_id,
        "result_decision": decide.decision,
        "result_duplicate_ids": list(decide.duplicate_ids),
        "result_reason": decide.reason,
        "task_state": "succeeded",
        "completed_at": completed_at,
        "result_version": 1,
        # 三轮审计 C-01 修复（D25）：10 §5 冻结 event_id =
        # SHA256(JCS([scope_id, request_id, result_version]))——此处原
        # 沿用旧拼接公式（D24 只修了 fake P17 链路，真 P18 存留）。
        # request_id 真链路透传随 P17-3 挂账，当前以 record_id 同源替代
        # （与 commit/coordinator D24 注记同款纪律）。
        # P2（P17-3 前置，窗口X 并线）：request_id 参数透传优先，缺省
        # record_id 回退。
        # W2Fγ（A11 消费点声明）：跨模块私有复用 admission._digest 为有意
        # 单源合同（仓内 JCS 单一事实源）；导出方公共化/声明归 δ 窗。
        "event_id": _admission._digest([scope_id, request_id or record_id, 1]),
        "audit_ids": list(audit_ids),
        "audit_complete": True,
        "reason_code": decide.internal_code,
        "callback_body": callback_body,
        "callback_body_hash": hashlib.sha256(
            callback_body.encode("utf-8")
        ).hexdigest(),                              # INV-4
        # 10-07 用户令（甲-i，held-delivery-design.md）：decision != "不重复"
        # → held（系统内扣留不投递）；放行件仍 pending。
        "delivery_state": ("pending" if decide.decision == "不重复" else "held"),
        # P2（C-04/C-08，窗口X 并线）：10 §6.337 钳制单源 deadline_iso——
        # expires_at 传入时 deadline=min(+24h, expires_at)；None=现状 now+24h
        "delivery_deadline_at": _deadline_iso(now, expires_at=expires_at),
        "next_delivery_at": completed_at,
        "callback_attempts": 0,
        "round_attempts": 0,
        "round": 1,
        # P2（C-08，窗口X 并线）：expires_at 仅传入时落 key——缺省不改现状
        # body 形状
        **({"expires_at": expires_at} if expires_at else {}),
    }
    store.cas_main_record(record_id, body,
                          expected_seq_no=0, expected_primary_term=0)
    store.advance_watermark(scope_id, business_date, arrival_seq)
    return body


def build_commit_one_store(store: Any, *, scope_id: str | None = None,
                           business_date: str | None = None,
                           arrival_seq: int | None = None) -> Any:
    """commit_one 真 ES 接线入口（挂账 #6）。

    方案 B（主窗口 19:58 裁定采纳）：协议自检后恒等返回——commit_one 直接消费
    RealESP18Store（coordinator.py 两行方案 B 改动已落）；ctx 三参忽略。
    方案 A（备选适配器）全文留档 log/temp/p18-uat-wiring-proposal.md §四。
    """
    for name in ("get_watermark", "write_main_record", "advance_watermark"):
        if not hasattr(store, name):
            raise TypeError(f"store missing commit_one protocol method {name!r}")
    return store


__all__ = [
    "AuditPersistError",
    "CASConflictError",
    "P18IndexPrefixInvalid",
    "P18UATGatesNotOpen",
    "P18_INDEX_PREFIX_PATTERN",
    "P18_UAT_ENV_FLAG",
    "RealESP18Config",
    "RealESP18Store",
    "assert_p18_uat_open",
    "build_commit_one_store",
    "build_p18_index_prefix",
    "persist_main_record_es",
    "validate_p18_index_name",
]
