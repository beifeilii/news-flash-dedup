"""P20 真 ES 投递仓储（UAT 接入层）。

20:52 主窗口亲笔落位：草案审定自 `log/temp/p20-uat-wiring-proposal.md` §三
（T4 子窗口起草，主窗口审 diff 全读后采纳；★1/2/3/4/5/8 裁定见 目标文档.md
决策日志 D10；persist/es_store.py 19:58 / vector/milvus_store.py 20:3x 同例）。

按 05:29 批复 + 修订 1 + T1/T2/T3 真机教训：
- 隔离前缀 p20-batch-<run_uuid>-news-dedup-(items|control)-v1-YYYY.MM.DD（事实清单；
  设计 §8 "callbacks" 字面偏差见建议书矛盾点 #6）；
- action.auto_create_index=-*：构造时显式幂等建索引（commit/es_store.py 19:16 先例）；
- (0,0) 首创建映射 op_type=create（19:12）；ES _seq_no 0 基（19:17）；
- 鸭式兼容 FakeDeliveryStore 表面（main_records / update_lease / mark_state /
  advance_round）：已封 dispatcher/coordinator/sender 零改动直跑真 ES（§四 方案乙）；
- 五前置补齐 / 代次互斥 / 领取计数自增 / §4.1 哈希再校的执法点在本仓储
  （已封函数只校子集——建议书矛盾点 #3/#4/#5/#8），已封文件零改动。
"""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any

# 窗口W1Fβ（W1a-F4）：parse_iso_utc 复用 coordinator 同款助手（单一事实源，
# 读宽写严纪律：naive 按 UTC 解释、aware 归一 UTC；coordinator 不反向依赖
# 本模块，无环）。窗口W2Fγ（A11）：公共名消费（复用合同见其 docstring 声明）。
from news_flash_dedup.delivery.coordinator import P20ReceiveError, parse_iso_utc
from news_flash_dedup.delivery.fake_store import (
    FakeDeliveryLease,
    FakeDeliveryRecord,
)
from news_flash_dedup.es_client import assert_test_environment


P20_UAT_ENV_FLAG = "P20_CONFIRM_UAT"
P20_INDEX_PREFIX_PATTERN = re.compile(
    r"^p20-batch-[A-Za-z0-9_-]+-news-dedup-(items|control)-v1-\d{4}\.\d{2}\.\d{2}$"
)


class P20UATGatesNotOpen(RuntimeError):
    """`P20_CONFIRM_UAT=1` 缺失或环境门未满足。"""


class P20IndexPrefixInvalid(ValueError):
    """隔离索引前缀不匹配 `p20-batch-...` 硬正则闸门。"""


class P20CASConflictError(RuntimeError):
    """主记录 CAS 版本竞争冲突——写入未发生，投递状态不动（INV-2/INV-3 同款语义）。"""


class P20PayloadIntegrityError(P20ReceiveError):
    """§4.1/INV-1：callback_body 与 callback_body_hash 对拍不符——冻结载荷被改，
    拒绝读取/发送（仓储读取转换层执法，建议书矛盾点 #5）。"""


class P20ClaimPreconditionError(P20ReceiveError):
    """仓储侧领取五前置 / 代次互斥拒绝（sealed 子集之外的执法点，矛盾点 #3/#4）。"""


def assert_p20_uat_open(env: Mapping[str, str] | None = None) -> None:
    """P20 闸门：`P20_CONFIRM_UAT=1` 必须设置；`assert_test_environment` 通过。"""
    env = env if env is not None else os.environ
    if env.get(P20_UAT_ENV_FLAG) != "1":
        raise P20UATGatesNotOpen(
            f"P20 requires {P20_UAT_ENV_FLAG}=1; got {env.get(P20_UAT_ENV_FLAG)!r}"
        )
    assert_test_environment(env=dict(env))


def build_p20_index_prefix(run_uuid: str, kind: str, business_date: date) -> str:
    """P20 隔离索引名：p20-batch-<run>-news-dedup-<kind>-v1-YYYY.MM.DD（事实清单）。"""
    if kind not in {"items", "control"}:
        raise ValueError("kind must be 'items' or 'control'")
    if not run_uuid or not re.fullmatch(r"[A-Za-z0-9_-]+", run_uuid):
        raise ValueError("run_uuid must match [A-Za-z0-9_-]+")
    dotted = business_date.strftime("%Y.%m.%d")
    return f"p20-batch-{run_uuid}-news-dedup-{kind}-v1-{dotted}"


def validate_p20_index_name(index_name: str) -> None:
    """P20 索引名必须严格匹配硬正则；192 保留索引零触碰。"""
    if not P20_INDEX_PREFIX_PATTERN.fullmatch(index_name):
        raise P20IndexPrefixInvalid(
            f"index {index_name!r} does not match "
            f"p20-batch-<run>-news-dedup-(items|control)-v1-YYYY.MM.DD"
        )


@dataclass(frozen=True)
class RealESP20Config:
    """P20 真 ES 接入配置。"""
    run_uuid: str
    business_date: date
    items_index: str = field(init=False)
    control_index: str = field(init=False)

    def __post_init__(self) -> None:
        items = build_p20_index_prefix(self.run_uuid, "items", self.business_date)
        control = build_p20_index_prefix(self.run_uuid, "control", self.business_date)
        for name in (items, control):
            validate_p20_index_name(name)
        object.__setattr__(self, "items_index", items)
        object.__setattr__(self, "control_index", control)


def _doc_to_record(source: Mapping) -> FakeDeliveryRecord:
    """ES 主记录 → FakeDeliveryRecord；§4.1/INV-1 哈希再校在读取转换层执法（矛盾点 #5）。

    payload_hash（投递侧名）= callback_body_hash（P18 字段名）——命名映射登记 #9。
    降级记录（callback_body="" 而哈希非空串哈希）在此结构性被拒——INV-1 预期行为。
    """
    callback_body = source.get("callback_body") or ""
    stored_hash = source.get("callback_body_hash") or ""
    recomputed = hashlib.sha256(callback_body.encode("utf-8")).hexdigest()
    if stored_hash != recomputed:
        raise P20PayloadIntegrityError(
            f"callback_body_hash mismatch: stored={stored_hash!r} "
            f"recomputed={recomputed!r}"
        )
    return FakeDeliveryRecord(
        record_id=source["record_id"],
        item_id=source.get("item_id", ""),
        scope_id=source.get("scope_id", ""),
        business_date=source.get("business_date", ""),
        arrival_seq=int(source.get("arrival_seq", 0)),
        delivery_state=source.get("delivery_state", ""),
        delivery_deadline_at=source.get("delivery_deadline_at", ""),
        next_delivery_at=source.get("next_delivery_at", ""),
        callback_attempts=int(source.get("callback_attempts", 0)),
        round_attempts=int(source.get("round_attempts", 0)),
        round=int(source.get("round", 1)),
        callback_body=callback_body,
        payload_hash=stored_hash,
        route_ref=source.get("route_ref", ""),
        vector_state=source.get("vector_state", ""),
        audit_complete=bool(source.get("audit_complete", False)),
        # P2（C-08，窗口X 并线移植）：expires_at 透传（缺键=空串现状；
        # 10 §6.340 读取链）
        expires_at=source.get("expires_at", "") or "",
    )


class _MainRecordsView:
    """FakeDeliveryStore.main_records 的真 ES 映射视图（鸭式兼容，§四 方案乙）。

    __getitem__ = 实时 GET + §4.1 哈希再校 + 版本缓存刷新（窗口M 起为纯读：
    不再写领取上下文——旧实现任何读都改写 _last_claim_read，并发下 send/
    persist/scan 的普通读会污染 update_lease 的落点推断，docstring 原自承
    "单线程 UAT 内确定"）；claim_read = 领取路径专用读，唯一写领取上下文
    的入口（矛盾点 #8 显式化）；__setitem__ = 版本缓存 CAS 全字段合并写
    （persist_next_delivery 的落点）；__iter__/__len__/items = match_all
    （UAT 规模 size=500 封顶，scan_pending 的迭代源）。
    """

    def __init__(self, store: "RealESP20Store") -> None:
        self._store = store

    def __getitem__(self, record_id: str) -> FakeDeliveryRecord:
        # 窗口M（窗口X 并线移植）：普通读不写领取上下文（并发安全）——
        # 领取落点推断只认 claim_read（见下）。send_callback/
        # persist_next_delivery/scan_pending 的读全部经本方法，一律不触碰
        # _last_claim_read。
        doc = self._store.get_main_record(record_id)
        if doc is None:
            raise KeyError(record_id)
        return _doc_to_record(doc["source"])

    def claim_read(self, record_id: str) -> FakeDeliveryRecord:
        """领取路径专用读（矛盾点 #8 显式化）：唯一会写领取上下文的读取。

        sealed receive 经本方法读记录后调用 update_lease——领取上下文只在
        claim 路径写；普通读（__getitem__/get/items）一律不写，并发下不再
        错写租约落点。
        """
        record = self[record_id]
        self._store._last_claim_read = record_id      # 矛盾点 #8：领取落点推断
        return record

    def get(self, record_id: str, default: Any = None) -> Any:
        try:
            return self[record_id]
        except KeyError:
            return default

    def __setitem__(self, record_id: str, record: FakeDeliveryRecord) -> None:
        self._store._cas_merge_record(record_id, record)

    def __iter__(self):
        return iter(self._store._list_record_ids())

    def __len__(self) -> int:
        return len(self._store._list_record_ids())

    def items(self):
        for record_id in self:
            yield record_id, self[record_id]


class RealESP20Store:
    """P20 真 ES 投递仓储（UAT 接入）。

    与 FakeDeliveryStore 协议对齐（鸭式）+ UAT 播种/审计/运维原语：
    main_records / update_lease / mark_state / advance_round（被已封函数消费）；
    seed_delivery_record / write_audit_docs / get_audit_doc / get_main_record /
    cleanup_plan / cleanup（测试稿消费）。版本缓存：每次读写刷新 (seq_no, primary_term)，
    CAS 一律 if_seq_no/if_primary_term（杜绝 (0,0)，约束 b/c）。
    """

    _DELIVERY_STATES = frozenset(
        {"pending", "delivering", "delivered", "exhausted", "expired"}
    )

    def __init__(self, client: Any, config: RealESP20Config) -> None:
        assert_p20_uat_open()
        self.client = client
        self.config = config
        self._versions: dict[str, tuple[int, int]] = {}
        # 领取上下文（矛盾点 #8）：仅 _MainRecordsView.claim_read 写
        # （窗口M 显式化，窗口X 并线——普通读不写，并发不再污染
        # update_lease 落点）。
        self._last_claim_read: str | None = None
        # 约束 a：本集群 action.auto_create_index=-*，写入前显式幂等建索引（19:16 先例）
        self._ensure_indices()

    def _ensure_indices(self) -> None:
        for name in (self.config.items_index, self.config.control_index):
            if not self.client.indices.exists(index=name):
                self.client.indices.create(index=name)

    @property
    def main_records(self) -> _MainRecordsView:
        return _MainRecordsView(self)

    # ---------- 读取 ----------

    def get_main_record(self, record_id: str) -> dict | None:
        """实时 GET 主记录；None = 不存在。副作用：刷新版本缓存。"""
        from elasticsearch import NotFoundError

        try:
            response = self.client.get(index=self.config.items_index,
                                       id=record_id, realtime=True)
        except NotFoundError:
            return None
        seq_no = int(response["_seq_no"])
        primary_term = int(response["_primary_term"])
        self._versions[record_id] = (seq_no, primary_term)
        return {"source": response["_source"], "seq_no": seq_no,
                "primary_term": primary_term}

    def _list_record_ids(self) -> list[str]:
        response = self.client.search(
            index=self.config.items_index, query={"match_all": {}},
            size=500, _source=False,
        )
        return sorted(
            hit["_id"] for hit in response.get("hits", {}).get("hits", [])
        )

    # ---------- CAS 写入核心 ----------

    def _cas_write(self, record_id: str, body: Mapping) -> int:
        """版本缓存前置的 CAS 写；返回真实新 _seq_no（0 基，约束 c）。"""
        from elasticsearch import ConflictError

        version = self._versions.get(record_id)
        if version is None:
            current = self.get_main_record(record_id)
            if current is None:
                raise P20CASConflictError(
                    f"main record {record_id!r} not found for CAS write"
                )
            version = (current["seq_no"], current["primary_term"])
        try:
            response = self.client.index(
                index=self.config.items_index, id=record_id, document=dict(body),
                if_seq_no=version[0], if_primary_term=version[1],
                refresh="wait_for",
            )
        except ConflictError as exc:
            raise P20CASConflictError(
                f"main record {record_id!r} CAS conflict at seq_no={version[0]} "
                f"(delivery state untouched)"
            ) from exc
        new_seq = int(response.get("_seq_no", 0))
        self._versions[record_id] = (
            new_seq, int(response.get("_primary_term", version[1])),
        )
        return new_seq

    # ---------- 领取（sealed receive 的仓储落点；矛盾点 #3/#4/#8） ----------

    def update_lease(self, lease: FakeDeliveryLease) -> str:
        """领取 CAS：五前置补齐 + 冻结对拍 + 代次互斥 + 计数自增，单文档原子落写。

        ★矛盾点 #8：FakeDeliveryLease 无 record_id——落点 = 最近一次
        main_records.claim_read 读取的记录（窗口M 显式化，窗口X 并线：
        sealed receive 的 claim 路径经 claim_read 先读后领；普通读不再写
        上下文，并发下 send/persist/scan 的读不会把落点拐跑——旧实现自承
        "单线程 UAT 内确定"的隐式假设随之解除）。
        ★矛盾点 #3：task_state=succeeded / audit_complete=True / deadline 未过
        由本层补齐（sealed receive 不校）。★矛盾点 #4：round_attempts /
        callback_attempts 自增由本 CAS 携带（设计 §3.1；sealed 不自增）。
        窗口Z2（N2-02，外部审计三轮确认）：expires_at 同查——10 §6.340
        "调度、领取和实际发送前都检查 now < delivery_deadline_at 且
        now < expires_at"，与写入侧 deadline_iso 钳制同语义；缺失/空串
        = 存量历史数据不拦（现状行为）。
        代次互斥（INV-3）：已存租约 owner_generation >= 新代次 → 拒（同代次重入
        与旧代次回放同拒）；新代次接管放行。
        """
        # W-A 族③(e)-3（A4-D1，M-09 rebase 后施工）：落点=租约自证
        # record_id 优先（交错 claim_read(A)→claim_read(B)→update_lease
        # (leaseA) 落 A 不错写 B）；空串回退单例属性=legacy 过渡（现役
        # 构造点零回归）；不做交叉互斥拒绝（携带形态下交错已安全，
        # 误拒会伤合法交错——红测 R3E3 绿守卫钉）。
        record_id = lease.record_id or self._last_claim_read
        if record_id is None:
            raise P20ClaimPreconditionError(
                "claim context absent: main_records.claim_read must precede "
                "update_lease (plain reads no longer set claim context)"
            )
        current = self.get_main_record(record_id)
        if current is None:
            raise P20ClaimPreconditionError(
                f"main record {record_id!r} absent for claim"
            )
        source = current["source"]
        if source.get("delivery_state") not in {"pending", "delivering"}:
            raise P20ClaimPreconditionError(
                f"delivery_state must be pending|delivering; "
                f"got {source.get('delivery_state')!r}"
            )
        if source.get("task_state") != "succeeded":
            raise P20ClaimPreconditionError(
                f"task_state must be succeeded; got {source.get('task_state')!r}"
            )
        if not source.get("audit_complete"):
            raise P20ClaimPreconditionError(
                "audit_complete=False; claim refused"
            )
        if int(source.get("round_attempts", 0)) >= 12:
            raise P20ClaimPreconditionError(
                f"round_attempts {source.get('round_attempts')} >= 12; claim refused"
            )
        # 窗口W2Fγ（A9+A10 时区分类学收口）：deadline 闸与下方 expires 闸
        # 同形硬化——parse_iso_utc 归一 + 畸形串 fail-closed 包
        # P20ClaimPreconditionError（旧裸 fromisoformat：畸形串裸 ValueError、
        # naive/aware 混比 TypeError 逃逸——W1Fβ 申报外残余）；缺失/空串
        # = 存量历史数据不拦（现状行为）。
        deadline = source.get("delivery_deadline_at") or ""
        if deadline:
            try:
                deadline_ts = parse_iso_utc(deadline)
            except (ValueError, TypeError, AttributeError) as err:
                raise P20ClaimPreconditionError(
                    f"delivery_deadline_at unparsable: {deadline!r}; "
                    f"claim refused"
                ) from err
            if deadline_ts <= datetime.now(timezone.utc):
                raise P20ClaimPreconditionError(
                    "delivery_deadline_at passed; claim refused"
                )
        # 窗口Z2（N2-02）：领取同查 now < expires_at（10 §6.340；与上方
        # deadline 闸同形同语义）——缺失/空串 = 存量历史数据不拦。
        # 窗口W1Fβ（W1a-F4，W1a 确认/主窗口对码）：解析加固 fail-closed——
        # 畸形串不再裸 ValueError/TypeError 逃逸，一律
        # P20ClaimPreconditionError 拒领；naive 按 UTC 解释（读宽写严，
        # 同款 parse_iso_utc 纪律）；aware 串比较语义逐字节不变。
        expires_at = source.get("expires_at") or ""
        if expires_at:
            try:
                expires_ts = parse_iso_utc(expires_at)
            except (ValueError, TypeError, AttributeError) as err:
                raise P20ClaimPreconditionError(
                    f"expires_at unparsable: {expires_at!r}; claim refused"
                ) from err
            if expires_ts <= datetime.now(timezone.utc):
                raise P20ClaimPreconditionError(
                    "expires_at passed; claim refused"
                )
        if (source.get("callback_body_hash") or "") != lease.payload_hash:
            raise P20ClaimPreconditionError("payload_hash mismatch at store")
        if (source.get("route_ref") or "") != lease.route_ref:
            raise P20ClaimPreconditionError("route_ref mismatch at store")
        existing = source.get("delivery_lease") or {}
        if int(existing.get("owner_generation", 0)) >= lease.owner_generation:
            raise P20ClaimPreconditionError(
                f"generation regression/reentry: "
                f"stored={existing.get('owner_generation')} "
                f"given={lease.owner_generation}"
            )
        body = dict(source)
        body["delivery_lease"] = {
            "owner_id": lease.owner_id,
            "owner_generation": lease.owner_generation,
            "lease_until": lease.lease_until,
            "attempt_id": lease.attempt_id,
            "round": lease.round,
            "result_version": lease.result_version,
            "event_id": lease.event_id,
            "payload_hash": lease.payload_hash,
            "route_ref": lease.route_ref,
        }
        body["round_attempts"] = int(source.get("round_attempts", 0)) + 1
        body["callback_attempts"] = int(source.get("callback_attempts", 0)) + 1
        self._cas_write(record_id, body)
        return lease.owner_id

    def lease_for(self, record_id: str):
        """W-A 族③(f)-2：真层租约来源（scan_expired_delivering 协议件）——
        读主记录内嵌 source["delivery_lease"] 构造 FakeDeliveryLease
        （record_id=记录自身键自证归属）；记录缺席/无租约/非映射 → None。
        注：设计稿 R9 锚 `_lease_from_source`（es_store.py:362-380）现役
        不存在（零命中在案）——按规范语义以本读径实现。"""
        current = self.get_main_record(record_id)
        if current is None:
            return None
        embedded = current["source"].get("delivery_lease")
        if not isinstance(embedded, Mapping):
            return None
        return FakeDeliveryLease(
            owner_id=embedded.get("owner_id", ""),
            owner_generation=int(embedded.get("owner_generation", 0) or 0),
            lease_until=embedded.get("lease_until", ""),
            attempt_id=embedded.get("attempt_id", ""),
            round=int(embedded.get("round", 1) or 1),
            result_version=int(embedded.get("result_version", 1) or 1),
            event_id=embedded.get("event_id", ""),
            payload_hash=embedded.get("payload_hash", ""),
            route_ref=embedded.get("route_ref", ""),
            record_id=record_id,
        )

    # ---------- 状态翻转 / 轮次（sealed sender 的仓储落点） ----------

    def mark_state(self, record_id: str, state: str) -> None:
        """CAS 翻 delivery_state（词表与 fake 对齐）；INV-2：仅该字段变动。"""
        if state not in self._DELIVERY_STATES:
            raise ValueError(f"invalid delivery_state {state!r}")
        current = self.get_main_record(record_id)
        if current is None:
            raise P20CASConflictError(
                f"main record {record_id!r} not found for mark_state"
            )
        self._cas_write(record_id, dict(current["source"], delivery_state=state))

    def advance_round(self, record_id: str, next_round: int) -> None:
        """round+1 重开（§7）：pending + round=next + round_attempts=0；累计不清零。"""
        current = self.get_main_record(record_id)
        if current is None:
            raise P20CASConflictError(
                f"main record {record_id!r} not found for advance_round"
            )
        body = dict(
            current["source"], delivery_state="pending",
            round=int(next_round), round_attempts=0,
        )
        self._cas_write(record_id, body)

    def _cas_merge_record(self, record_id: str,
                          record: FakeDeliveryRecord) -> None:
        """`main_records[rid] = rec` 的落点（persist_next_delivery）：投递字段组
        合并写（语义字段不动，INV-2）；callback_body_hash 以 rec.payload_hash 回填
        （命名映射登记 #9）。"""
        current = self.get_main_record(record_id)
        if current is None:
            raise P20CASConflictError(
                f"main record {record_id!r} not found for merge"
            )
        body = dict(
            current["source"],
            delivery_state=record.delivery_state,
            delivery_deadline_at=record.delivery_deadline_at,
            next_delivery_at=record.next_delivery_at,
            callback_attempts=record.callback_attempts,
            round_attempts=record.round_attempts,
            round=record.round,
            callback_body=record.callback_body,
            callback_body_hash=record.payload_hash,
            route_ref=record.route_ref,
            vector_state=record.vector_state,
            audit_complete=record.audit_complete,
        )
        self._cas_write(record_id, body)

    # ---------- UAT 播种 / 审计 sidecar（非领取路径） ----------

    def seed_delivery_record(self, body: Mapping) -> tuple[int, int]:
        """UAT 播种（两阶段，约束 b）：create(not_ready) 壳 → CAS 到 body 指定态。

        route_ref 由播种侧写入（P18 落库路径无此字段——矛盾点 #2，本仓储不要求
        P18 回灌）。返回 CAS 后的 (seq_no, primary_term)。
        """
        required = (
            "record_id", "callback_body", "callback_body_hash", "route_ref",
            "event_id", "delivery_state", "task_state", "audit_complete",
        )
        missing = [key for key in required if key not in body]
        if missing:
            raise ValueError(f"seed body missing fields: {missing}")
        record_id = body["record_id"]
        from elasticsearch import ConflictError

        shell = dict(body, delivery_state="not_ready")
        try:
            created = self.client.index(
                index=self.config.items_index, id=record_id, document=shell,
                op_type="create", refresh="wait_for",
            )
        except ConflictError as exc:
            raise P20CASConflictError(
                f"seed {record_id!r} already exists (create conflict)"
            ) from exc
        self._versions[record_id] = (
            int(created.get("_seq_no", 0)),
            int(created.get("_primary_term", 1)),
        )
        new_seq = self._cas_write(record_id, dict(body))
        return new_seq, self._versions[record_id][1]

    def write_audit_docs(self, audit_docs: Iterable[Mapping]) -> list[str]:
        """挂账 #7：审计对照文档落 control 索引（op_type=create 幂等；重放不重复）。"""
        from elasticsearch import ConflictError

        ids: list[str] = []
        for doc in audit_docs:
            doc_id = doc["comparison_id"]
            try:
                self.client.create(index=self.config.control_index, id=doc_id,
                                   document=dict(doc), refresh="wait_for")
            except ConflictError:
                pass                            # 幂等：已记
            ids.append(doc_id)
        return ids

    def get_audit_doc(self, comparison_id: str) -> dict | None:
        """实时 GET 审计对照文档（control 索引）；None = 不存在。"""
        from elasticsearch import NotFoundError

        try:
            response = self.client.get(index=self.config.control_index,
                                       id=comparison_id, realtime=True)
        except NotFoundError:
            return None
        return response["_source"]

    # ---------- UAT 运维（公共标尺 3/4） ----------

    def snapshot_index_names(self) -> list[str]:
        response = self.client.cat.indices(format="json", h="index")
        return sorted(item.get("index", "") for item in response)

    def cleanup_plan(self) -> list[str]:
        """dry-run：仅本 run 前缀 `p20-batch-<run_uuid>-`（T1/T2/T3 同款更窄作用域）。"""
        run_prefix = f"p20-batch-{self.config.run_uuid}-"
        return sorted(n for n in self.snapshot_index_names()
                      if n.startswith(run_prefix))

    def cleanup(self) -> list[str]:
        """执行清场：仅删 cleanup_plan 列出的本 run 前缀索引（不经 lifecycle——
        其硬正则只收 p01-batch 前缀 + (items|audits)，矛盾点 #6）。"""
        targets = self.cleanup_plan()
        for name in targets:
            self.client.indices.delete(index=name)
        return targets


__all__ = [
    "P20CASConflictError",
    "P20ClaimPreconditionError",
    "P20IndexPrefixInvalid",
    "P20PayloadIntegrityError",
    "P20UATGatesNotOpen",
    "P20_INDEX_PREFIX_PATTERN",
    "P20_UAT_ENV_FLAG",
    "RealESP20Config",
    "RealESP20Store",
    "assert_p20_uat_open",
    "build_p20_index_prefix",
    "validate_p20_index_name",
]
