"""P17 commit_one 编排：fake 仓储注测，STALE 端到端双场景 + CAS 路径。

按 04:06 强制令 P17-2 标尺 1-3：
1. prepared_seq 过期 → 返回 DECIDE_RESULT_STALE（不抛不吞）
2. lexical_watermark 过期 → 返回 DECIDE_RESULT_STALE
3. audit_complete=False → CAS 拒绝（无写入动作）
4. CAS 成功 → 主记录字段集 + decision_watermark 推进
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from news_flash_dedup.commit.fake_store import FakeCommitStore, FakeMainRecord
from news_flash_dedup.deadline import deadline_iso
from news_flash_dedup.decide import audit as audit_module
from news_flash_dedup.decide import judge_adapter as judge_adapter_module
from news_flash_dedup.decide import service as decide_service
from news_flash_dedup.decide.audit import AuditBatch
from news_flash_dedup.decide.types import DecideOutcome
from news_flash_dedup.persist.fake_store import FakeCASConflictError


@dataclass(frozen=True)
class CommitContext:
    """commit_one 的输入上下文。"""
    scope_id: str
    business_date: str
    arrival_seq: int
    current: Mapping
    candidates: tuple[Mapping, ...]
    visible_seq: int
    prepared_seq: int
    pipeline_version: str = "dedup_v1"
    # M-01（13:20 原令执行，窗口J）：coverage_complete 默认 True 拆除——强制
    # 关键字参数无默认，缺省构造即 TypeError，覆盖完整性不得静默供给。
    # field(kw_only=True) 免字段重排（pipeline_version 带默认在前，dataclass
    # "非默认跟默认"禁令由 kw_only 豁免），既有位置传参形态零影响。
    coverage_complete: bool = field(kw_only=True)


@dataclass(frozen=True)
class CommitOutcome:
    """commit_one 的输出。"""
    state: str  # "committed" / "stale"
    decide_outcome: DecideOutcome | None = None
    stale: Any = None  # DecideResultStale
    audit_complete: bool = False
    main_record_id: str | None = None
    decision_watermark_advanced_to: int | None = None


class CommitOneError(ValueError):
    """commit_one 不可恢复错误（如 audit_complete=False 时强行 CAS、候选非法成员）。"""


def _compute_payload_hash(callback_body: str) -> str:
    return hashlib.sha256(callback_body.encode("utf-8")).hexdigest()


def _build_callback_body(decide: DecideOutcome) -> str:
    """构造回调冻结载荷（07 §5/11 §1 五字段 JSON）。"""
    pub = decide.to_public_dict()
    return json.dumps(pub, sort_keys=True, ensure_ascii=False)


# W2Fγ（M-3）：重试幂等对拍的稳定字段集——冻结身份/载荷/决策字段全含；
# completed_at、delivery_deadline_at 为时间派生字段（重试时刻不同恒差），
# 其身份语义已由 payload_hash（五字段冻结载荷哈希）与 event_id（身份三元组
# 摘要）承载，不参拍。
_RETRY_STABLE_FIELDS = (
    "record_id", "item_id", "text", "decision", "duplicate_ids", "reason",
    "payload_hash", "delivery_state", "callback_attempts", "audit_ids",
    "audit_complete", "raw_hash", "pipeline_version", "result_version",
    "event_id", "internal_code", "expires_at",
)


def _record_divergence(existing: Any, attempted: Any) -> list[str]:
    """M-3 重试幂等对拍：返回两记录在稳定字段集上的分歧字段名列表
    （空 = 一致可幂等）。"""
    return [name for name in _RETRY_STABLE_FIELDS
            if getattr(existing, name, None) != getattr(attempted, name, None)]


@dataclass(frozen=True)
class CommitWritePlan:
    """写相位内存构造产物（零 store 交互）——串行 commit_one 与 B+ 批量
    适配层（recall/parallel_worker.py）共用单源，两侧写出的主记录体/
    审计批结构性同源（P6 六面对拍的对象）。"""
    decide: DecideOutcome
    audit_batch: AuditBatch
    main_record: FakeMainRecord
    target_watermark: int
    completed_at: str


def build_commit_write_plan(
    ctx: CommitContext,
    decide: DecideOutcome,
    *,
    audit_complete: bool,                   # 与 commit_one 同款强制显式
    expires_at: str | None = None,
    request_id: str | None = None,
    decision_watermark_seq: int | None = None,
) -> CommitWritePlan:
    """commit_one 写相位内存构造段（2026-10-08 B+ 抽取，行为零改动）。

    抽取范围=原 commit_one L215-327 同段：审计批构造（真实对级
    pair_results→AuditBatch，audit_complete 调用方原样透传）、回调冻结
    载荷/payload_hash、单源 now、event_id（JCS[scope,request_id,1]，
    request_id 回退链同款）、FakeMainRecord 全字段（held/pending 扣留
    放行口径同款）、水位目标缺省=ctx.arrival_seq。audit_complete 有效性
    判定（effective 拒绝/放行）与 store 相关预检（水位单调/幂等对拍）
    不在本段——各写路径（串行 commit_one/批量 commit_batch）各自承载。
    """
    audit_batch: AuditBatch = audit_module.build_audit_batch(
        decide.pair_results,
        audit_complete=audit_complete,
        index_prefix="p17-fake-audits-v1",
    )
    callback_body = _build_callback_body(decide)
    payload_hash = _compute_payload_hash(callback_body)
    now = datetime.now(timezone.utc)
    completed_at = now.isoformat()
    from news_flash_dedup import admission as _admission
    if not isinstance(request_id, str) or not request_id:
        request_id = ctx.current.get("request_id")
    if not isinstance(request_id, str) or not request_id:
        request_id = ctx.current["record_id"]
    event_id = _admission._digest([ctx.scope_id, request_id, 1])
    record_id = ctx.current["record_id"]
    target_watermark = (decision_watermark_seq
                        if decision_watermark_seq is not None
                        else ctx.arrival_seq)
    audit_ids = tuple(record.comparison_id
                      for record in audit_batch.records)
    main_record = FakeMainRecord(
        record_id=record_id,
        item_id=decide.item_id,
        text=decide.text,
        decision=decide.decision,
        duplicate_ids=decide.duplicate_ids,
        reason=decide.reason,
        payload_hash=payload_hash,
        delivery_state=("pending" if decide.decision == "不重复" else "held"),
        delivery_deadline_at=deadline_iso(now, expires_at=expires_at),
        expires_at=expires_at or "",
        callback_attempts=0,
        audit_ids=audit_ids,
        audit_complete=True,
        raw_hash=decide.raw_hash,
        pipeline_version=ctx.pipeline_version,
        completed_at=completed_at,
        result_version=1,
        event_id=event_id,
        internal_code=decide.internal_code,
    )
    return CommitWritePlan(
        decide=decide, audit_batch=audit_batch, main_record=main_record,
        target_watermark=target_watermark, completed_at=completed_at)



def commit_one(
    ctx: CommitContext,
    store: FakeCommitStore,
    *,
    decision_watermark_seq: int | None = None,
    allow_audit_complete_false: bool = False,
    audit_complete: bool,  # 窗口T（M-01 同标准，窗口V 登记待解锁）：第 4 处默认拆除——强制关键字显式表态
    coverage_complete: bool | None = None,  # 13:50 R3：None=从 ctx 派生；显式覆盖用于测试
    dictionary=None,                        # D19 杠杆 b：None 路径行为逐字节不变
    dictionary_version: str = "dict_v1",
    expires_at: str | None = None,          # P2 C-04/C-08（窗口X 并线）：None=现状（不钳制不透传）
    request_id: str | None = None,          # P2 P17-3 前置（窗口X 并线）：None=D24 回退链不变
    budget=None,                            # W2 ⑩①-4（N43 挂账清偿）：ProcessingBudget|None；None=现役逐字节
    judge_callable=None,                    # P1 联调（2026-10-09 P1-a）：None=按 env 装配真件（DEDUP_JUDGE_PROOF 关→None=未注入 fail-closed 未决）
    judge_version_config=None,              # 终审复审补丁：判官版本单源（None=装配/服务各自按 env 分发）
) -> CommitOutcome:
    """commit_one 编排（fake 仓储注测）。

    流水线：
    1. 水位校验（visible_seq + prepared_seq ≥ arrival_seq - 1）→ STALE 端到端；
    2. decide_for_task 集合级决策；
    3. 审计前置：构建 audit_batch（audit_complete=False → 拒绝 CAS 不写入）；
    4. CAS 主记录写入（仅 audit_complete=True 时）；
    5. 推进 decision_watermark（仅 CAS 成功后）。

    终审复审补丁（唯一权威单源传递）：judge_version_config 在场时判官
    预装配（judge_callable 未注入时）与 decide_for_task 同一配置一路
    传入（装配与判定同源同版本，禁止各读环境变量）；缺省维持原 env
    分发口径（judge_callable 显式注入时配置由注入方保证，装配零接触）。
    """
    # 标尺 1：STALE 端到端双场景（prepared_seq / lexical_watermark）
    # W2Fγ（WA4b-35③）：type() 鸭子对象绕型拆除——以真 WatermarkSnapshot
    # 构造（is_watermark_complete 只读 prepared_seq/lexical_watermark 两
    # 字段；owner_id/owner_generation/last_materialized_seq 为占位——
    # commit_one 不持有 owner 身份，值不进判定，逐字节行为不变）。
    from news_flash_dedup.commit import (
        DecideResultStale,
        WatermarkSnapshot,
        is_watermark_complete,
    )
    ok, detail = is_watermark_complete(
        WatermarkSnapshot(
            scope_id=ctx.scope_id,
            business_date=ctx.business_date,
            last_materialized_seq=ctx.visible_seq,   # 占位：校验不读
            lexical_watermark=ctx.visible_seq,
            prepared_seq=ctx.prepared_seq,
            owner_id="",                             # 占位：commit_one 不持有
            owner_generation=0,                      # 占位：校验不读
        ),
        arrival_seq=ctx.arrival_seq,
    )
    if not ok:
        code = ("prepared_seq_stale" if detail.startswith("prepared_seq")
                else "lexical_watermark_stale")
        stale = DecideResultStale(
            code=code, detail=detail,
            arrival_seq=ctx.arrival_seq, scope_id=ctx.scope_id,
            business_date=ctx.business_date,
        )
        return CommitOutcome(state="stale", stale=stale, audit_complete=False)

    # 集合级决策：history = 最近候选；candidates = 其余候选
    if coverage_complete is None:
        coverage_complete = ctx.coverage_complete
    if ctx.candidates:
        history = ctx.candidates[-1]
        remaining_candidates = ctx.candidates[:-1]
        # P1 联调装配（2026-10-09 P1-a）：真件 judge_callable 透传——显式
        # 注入优先（测试罐装 LLM 边界）；None=按 env 装配（DEDUP_JUDGE_PROOF
        # 关→build_judge_callable 返 None=未注入，adjudicate_pair 默认实现
        # fail-closed 未决，绝不冒签）。DEDUP_JUDGE_IN_CHAIN 关=判官段整体
        # 跳过（decide_for_task 内闸，老行为逐字节）。
        # 终审复审补丁：judge_version_config 在场时预装配与 decide_for_task
        # 同一配置一路传入（唯一权威单源——禁止各读环境变量：装配侧
        # version_config=、服务侧 judge_version_config= 同对象）。
        if judge_callable is None:
            judge_callable = judge_adapter_module.build_judge_callable(
                budget=budget, version_config=judge_version_config)
        decide = decide_service.decide_for_task(
            history=history,
            candidates=remaining_candidates,
            current=ctx.current,
            pipeline_version=ctx.pipeline_version,
            coverage_complete=coverage_complete,  # 13:20 案二整改：从 ctx 派生（禁默认 True）
            visible_seq=ctx.visible_seq,
            prepared_seq=ctx.prepared_seq,
            dictionary=dictionary,  # D19 杠杆 b 穿透
            dictionary_version=dictionary_version,
            budget=budget,  # W2 ⑩①-4：T011 归因面透传（None=零 diff）
            judge_callable=judge_callable,  # P1 联调：真件/None（未注入 fail-closed）
            judge_version_config=judge_version_config,  # 终审复审：单源一路传递
        )
    else:
        # R9 外部审核 F3 修复（主窗口 06:5x）：分区首条（零候选）——无历史
        # 可比对，结构性"不重复"；原先 history=ctx.current 自对自身进
        # decide_for_task → pair_alignment "record_id must differ" 冒泡，
        # 每个 scope/date 分区首条 100% 崩溃（r9a-f3-repro.py 实证）。
        # 三方审计 V2 实证修复（D24）：零候选 + coverage_complete=False 时
        # "无候选"可能是召回不全的假象——案二不变量（覆盖不全不得"不重复"）
        # 对此分支同样适用，改判边界（RECALL_INCOMPLETE）。
        from news_flash_dedup.decide.types import DecideOutcome
        current_mapping = ctx.current
        if coverage_complete:
            decide = DecideOutcome(
                item_id=current_mapping["item_id"],
                text=current_mapping["text"],
                decision="不重复",
                duplicate_ids=(),
                reason="本域日本分区首条，无历史候选可比对。",
                internal_code="NO_DUPLICATE_FOUND",
                pair_codes={},
                used_evidence=(),
                unresolved_fields=(),
                raw_hash=current_mapping["raw_hash"],
                pipeline_version=ctx.pipeline_version,
            )
        else:
            decide = DecideOutcome(
                item_id=current_mapping["item_id"],
                text=current_mapping["text"],
                decision="边界case/疑难case",
                duplicate_ids=(),
                reason="召回覆盖不全且零候选，无法确认无重复。",
                internal_code="RECALL_INCOMPLETE",
                pair_codes={},
                used_evidence=(),
                unresolved_fields=("candidates",),
                raw_hash=current_mapping["raw_hash"],
                pipeline_version=ctx.pipeline_version,
            )

    # W2 ⑩①-4（N43 挂账清偿，12 L374）：CAS 临界区启动闸——余量不足
    # commit_target_s 则不假装按时（决策已完成、审计/CAS 未启动即诚实
    # CommitOneError；worker 收容 "failed"，下轮重领重试——T082'重试
    # 不重置预算'由"持原 accepted_at 派生同 deadline"承载）。
    if budget is not None and budget.remaining_s() < budget.config.commit_target_s:
        raise CommitOneError(
            "budget below commit target; CAS not started (honest refusal)")

    # 标尺 4：审计前置（audit_complete=False → CAS 拒绝）
    # R9 外部审核 F4 修复（主窗口 07:0x）：原先 build_audit_batch([]) 恒空批、
    # audit_ids=() 而 audit_complete=True（"骨架阶段暂空"）——外部审核实测
    # 判定属实且 #7 关闭口径超出实证。现以 decide 外露的真实对级 PairResult
    # 构造审计批次并落库，audit_ids 回填主记录。
    # 2026-10-08（B+ 抽取）：内存构造段归 build_commit_write_plan 单源
    # （审计批/载荷/event_id/主记录体/水位目标），行为逐字节不变；
    # effective 判定与审计落库仍在本函数（写路径侧承载）。
    plan = build_commit_write_plan(
        ctx, decide, audit_complete=audit_complete, expires_at=expires_at,
        request_id=request_id, decision_watermark_seq=decision_watermark_seq)
    audit_batch = plan.audit_batch
    effective_audit_complete = audit_batch.audit_complete
    if not effective_audit_complete:
        if not allow_audit_complete_false:
            raise CommitOneError(
                "audit_complete=False; CAS refused (no write action)"
            )
        # 显式允许：CAS 拒绝路径（不写）
        return CommitOutcome(
            state="stale",
            decide_outcome=decide,
            audit_complete=False,
        )

    # 审计落库（CAS 主记录前）：真 ES 仓储批量 create（不可覆盖），fake 逐条。
    # W2Fγ（M-3 幂等通路①）：fake 逐条分支重 comparison_id 冲突（W2Fγ A4
    # create-only）时对拍已存文档——内容一致 = 同批重试幂等已存（跳过，
    # 不重复写入）；不一致 = 审计证据撞车，CommitOneError 冲突（不掩盖）。
    if audit_batch.records:
        if hasattr(store, "write_audit_documents"):
            audit_ids = tuple(store.write_audit_documents(
                [record.to_doc() for record in audit_batch.records]))
        else:
            ids = []
            for record in audit_batch.records:
                doc = record.to_doc()
                try:
                    store.write_audit(record.comparison_id, doc)
                except FakeCASConflictError:
                    existing_doc = getattr(store, "audit_docs", {}).get(
                        record.comparison_id)
                    if existing_doc != doc:
                        raise CommitOneError(
                            f"audit doc {record.comparison_id!r} conflict: "
                            f"existing doc differs (M-3 retry divergence)"
                        )
                    # 一致 → 幂等已存（同批重试），不重复写入
                ids.append(record.comparison_id)
            audit_ids = tuple(ids)
    else:
        audit_ids = ()

    # 标尺 3：CAS 成功路径（前置：watermark 单调性）
    # 2026-10-08（B+ 抽取）：主记录体/水位目标取自 plan（构造单源）；
    # audit_ids 以落库返回为准（原序同款，值=comparison_id 序列）。
    target_watermark = plan.target_watermark
    main_record = plan.main_record
    record_id = main_record.record_id
    if audit_ids != main_record.audit_ids:
        # 真仓储落库返回与构造推导不一致=异常（值本应同源同序）——
        # fail-closed 不猜测（正常路径恒等，本支结构性不可达）
        raise CommitOneError(
            f"audit_ids divergence between write receipt and write plan: "
            f"{audit_ids!r} vs {main_record.audit_ids!r}")

    # 推进 decision_watermark 必须单调（前置校验避免无效 CAS）
    # W2Fγ（WA4b-35④）：`or` falsy 塌缩拆除——显式 0 不再被吞为
    # arrival_seq（0 是合法显式目标值；is-None 判别）。
    # 19:58 主窗口（方案 B）：dict 属性读 → 协议方法调用（同义改写，消灭结构耦合）
    prev_watermark = store.get_watermark(ctx.scope_id, ctx.business_date) or 0
    if target_watermark < prev_watermark:
        raise CommitOneError(
            f"decision_watermark cannot regress: {prev_watermark} -> {target_watermark}"
        )

    # W2Fγ（M-3 幂等通路②，WB2 中危，探针 winw2fc-probe-m3-commit-retry.json
    # 实证旧形=watermark 回退闸先拒）：同 record_id 重试不再裸吃冲突——
    # 冲突/齐平时对拍既有记录稳定字段（时间派生字段 completed_at/
    # delivery_deadline_at 除外），一致 → 幂等成功返回（零重复写入）；
    # 不一致/记录缺席 → CommitOneError 冲突（不掩盖）。
    if target_watermark == prev_watermark:
        existing_records = getattr(store, "main_records", None)
        existing = existing_records.get(record_id) if existing_records is not None else None
        if existing is None:
            raise CommitOneError(
                f"commit conflict: watermark already at {prev_watermark} but "
                f"main record {record_id!r} missing or store not introspectable "
                f"(M-3 idempotency requires fake-style main_records view)"
            )
        diff_fields = _record_divergence(existing, main_record)
        if diff_fields:
            raise CommitOneError(
                f"commit conflict: existing main record {record_id!r} "
                f"diverges on {diff_fields} (M-3 idempotency refused)"
            )
        return CommitOutcome(          # 幂等成功：零重复写入/零重复推进
            state="committed",
            decide_outcome=decide,
            audit_complete=True,
            main_record_id=record_id,
            decision_watermark_advanced_to=prev_watermark,
        )

    # 19:58 主窗口（方案 B）：调用处补 ctx 三参（fake 以 **_ctx 吞掉；真 ES 仓储消费）
    try:
        store.write_main_record(main_record, scope_id=ctx.scope_id,
                                business_date=ctx.business_date,
                                arrival_seq=ctx.arrival_seq)
    except FakeCASConflictError as exc:
        # M-3 幂等通路③：写入撞 create 冲突（首提交 CAS 成功后、watermark
        # 推进前崩溃的补偿形状）——对拍一致 → 补推进水位完成提交；不一致
        # → 冲突不掩盖。
        existing_records = getattr(store, "main_records", None)
        existing = existing_records.get(record_id) if existing_records is not None else None
        if existing is None:
            raise
        diff_fields = _record_divergence(existing, main_record)
        if diff_fields:
            raise CommitOneError(
                f"commit conflict: existing main record {record_id!r} "
                f"diverges on {diff_fields} (M-3 idempotency refused)"
            ) from exc
        store.advance_watermark(ctx.scope_id, ctx.business_date,
                                target_watermark)
        return CommitOutcome(
            state="committed",
            decide_outcome=decide,
            audit_complete=True,
            main_record_id=record_id,
            decision_watermark_advanced_to=target_watermark,
        )

    store.advance_watermark(ctx.scope_id, ctx.business_date, target_watermark)

    return CommitOutcome(
        state="committed",
        decide_outcome=decide,
        audit_complete=True,
        main_record_id=record_id,
        decision_watermark_advanced_to=target_watermark,
    )


__all__ = [
    "CommitContext",
    "CommitOneError",
    "CommitOutcome",
    "CommitWritePlan",
    "build_commit_write_plan",
    "commit_one",
]