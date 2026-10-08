"""P17 真 ES 提交仓储（UAT 接入层）。

按 04:21 批复 P17-3：
- 仅连 UAT ES（`TEST_*` 凭据 + `assert_test_environment`）；
- 隔离前缀 `p17-batch-<run_uuid>-news-dedup-(items|audits)-v1-YYYY.MM.DD`；
- 94 保留索引零触碰（跑前/跑后 snapshot 校验）；
- `P17_CONFIRM_UAT=1` 闸门：缺失即拒启；
- 真实 CAS 走 `_seq_no/_primary_term` 校验（与 P17 设计 §4.1 一致）；
- 写入失败抛异常而非返回 stale（与 P17-2 fake 行为对称）。
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any

from news_flash_dedup.es_client import (
    APPROVED_UAT_HOST,
    APPROVED_UAT_PORT,
    APPROVED_UAT_SCHEME,
    ProductionAccessDenied,
    assert_test_environment,
)


P17_UAT_ENV_FLAG = "P17_CONFIRM_UAT"
P17_INDEX_PREFIX_PATTERN = re.compile(
    r"^p17-batch-[A-Za-z0-9_-]+-news-dedup-(items|audits)-v1-\d{4}\.\d{2}\.\d{2}$"
)
# W2Fγ（M-1）：本 run control 索引名后缀（不满足上方硬正则——cleanup
# fullmatch 过滤曾把 control 排除致每 UAT run 残留 1 索引）。
P17_CONTROL_SUFFIX = "-control"


class P17UATGatesNotOpen(RuntimeError):
    """`P17_CONFIRM_UAT=1` 缺失或环境门未满足。"""


class P17IndexPrefixInvalid(ValueError):
    """隔离索引前缀不匹配 `p17-batch-...` 硬正则闸门。"""


def assert_p17_uat_open(env: Mapping[str, str] | None = None) -> None:
    """P17-3 闸门：`P17_CONFIRM_UAT=1` 必须设置；`assert_test_environment` 通过。"""
    env = env if env is not None else os.environ
    if env.get(P17_UAT_ENV_FLAG) != "1":
        raise P17UATGatesNotOpen(
            f"P17-3 requires {P17_UAT_ENV_FLAG}=1; got {env.get(P17_UAT_ENV_FLAG)!r}"
        )
    assert_test_environment(env=dict(env))


def build_p17_index_prefix(run_uuid: str, kind: str, business_date: date) -> str:
    """P17 隔离索引前缀：p17-batch-<run>-news-dedup-<kind>-v1-YYYY.MM.DD"""
    if kind not in {"items", "audits"}:
        raise ValueError("kind must be 'items' or 'audits'")
    if not run_uuid or not re.fullmatch(r"[A-Za-z0-9_-]+", run_uuid):
        raise ValueError("run_uuid must match [A-Za-z0-9_-]+")
    dotted = business_date.strftime("%Y.%m.%d")
    return f"p17-batch-{run_uuid}-news-dedup-{kind}-v1-{dotted}"


def validate_p17_index_name(index_name: str) -> None:
    """P17 索引前缀必须严格匹配硬正则；94 保留索引零触碰。"""
    if not P17_INDEX_PREFIX_PATTERN.fullmatch(index_name):
        raise P17IndexPrefixInvalid(
            f"index {index_name!r} does not match "
            f"p17-batch-<run>-news-dedup-(items|audits)-v1-YYYY.MM.DD"
        )


def validate_p17_control_index_name(index_name: str) -> None:
    """W2Fγ（M-1）：本 run control 索引名独立校验——control 名带
    "-control" 后缀不满足 P17_INDEX_PREFIX_PATTERN.fullmatch，从未过硬
    正则闸（构造注入/拼写漂移无防线）；剥后缀后须过硬闸且后缀恰为
    "-control" 结尾。"""
    if (not index_name.endswith(P17_CONTROL_SUFFIX)
            or not P17_INDEX_PREFIX_PATTERN.fullmatch(
                index_name[: -len(P17_CONTROL_SUFFIX)])):
        raise P17IndexPrefixInvalid(
            f"control index {index_name!r} does not match "
            f"p17-batch-<run>-news-dedup-(items|audits)-v1-YYYY.MM.DD-control"
        )


@dataclass(frozen=True)
class RealESConfig:
    """P17 真 ES 接入配置。"""
    run_uuid: str
    business_date: date
    items_index: str = field(init=False)
    audits_index: str = field(init=False)

    def __post_init__(self) -> None:
        items = build_p17_index_prefix(self.run_uuid, "items", self.business_date)
        audits = build_p17_index_prefix(self.run_uuid, "audits", self.business_date)
        validate_p17_index_name(items)
        validate_p17_index_name(audits)
        object.__setattr__(self, "items_index", items)
        object.__setattr__(self, "audits_index", audits)


class RealESCommitStore:
    """真 ES 提交仓储（UAT 接入）。

    使用 `elasticsearch` Python client；CAS 走 `if_seq_no/if_primary_term`。
    """

    def __init__(self, client: Any, config: RealESConfig,
                 *, control_index: str | None = None) -> None:
        assert_p17_uat_open()
        self.client = client
        self.config = config
        # W2Fγ（M-1 + WA4b-35④）：control_index 显式传入 is-None 判别
        # （空串不再被 `or` 塌缩静默重算），且一律过独立硬闸（构造期
        # fail-fast——control 名此前从未过硬正则闸）。
        self.control_index = control_index if control_index is not None else (
            build_p17_index_prefix(config.run_uuid, "audits", config.business_date)
            + "-control"
        )
        validate_p17_control_index_name(self.control_index)
        # 19:16 主窗口修复（UAT 红盘 run-1913）：本集群 action.auto_create_index=-*
        # 禁止自动建索引，写入前必须显式创建（P01 es_admission_index 同款先例）。
        self._ensure_indices()

    def _ensure_indices(self) -> None:
        """幂等创建本 run 的隔离索引（items/audits/control），已存在则跳过。"""
        for name in (self.config.items_index, self.config.audits_index,
                     self.control_index):
            if not self.client.indices.exists(index=name):
                self.client.indices.create(index=name)

    def write_audit_documents(self, audit_records: Iterable[Mapping]) -> list[str]:
        """批量写入审计文档（op_type=create，重复 ID 报 conflict）。

        R9 外部审核 F8c 修复（主窗口 06:3x）：bulk 动作原为 `index`（重复
        comparison_id 静默覆盖审计证据），与 docstring 声称的 create 不符；
        现按 docstring 口径用 `create`（重复 ID → version_conflict 报错，
        审计不可被覆盖）。
        """
        records = list(audit_records)     # 物化：幂等对拍需二次遍历
        actions: list[dict] = []
        for record in records:
            actions.append({"create": {"_index": self.config.audits_index,
                                          "_id": record["comparison_id"]}})
            actions.append(dict(record))
        if not actions:
            return []
        response = self.client.bulk(operations=actions, refresh="wait_for")
        items = response.get("items", [])
        ids: list[str] = []
        errors: list[dict] = []
        for item in items:
            outcome = item.get("create", {})
            if outcome.get("error"):
                errors.append(outcome["error"])
            else:
                ids.append(outcome.get("_id", ""))
        if not errors:
            return ids
        # W-A 族③(d)（A4-C1，N36 式幂等对拍——persist/es_store.py:303-357
        # 成对移植）：bulk error 不再裸 RuntimeError 一抛了之——先构造
        # original（异常类型保持 RuntimeError 零回归），随即逐文档实时 GET
        # 对拍：核验读不可用/任一缺失→原错重抛不掩盖（失败即停）；任一
        # 分歧→AuditDivergenceError 带键明细（fail-closed；惰性导入同
        # persist:347-351 先例）；全部一致→幂等返回（顺序同输入，与成功
        # 路径同形；commit 侧无 enrichment，对拍键集=record 自身键）。
        original = RuntimeError(f"audit bulk write failed: {errors}")
        verified: list[str] = []
        for record in records:
            comparison_id = record["comparison_id"]
            try:
                existing = self.get_audit_doc(comparison_id)
            except Exception:
                raise original
            if existing is None:
                raise original
            source = existing["source"]
            divergent = [key for key in record
                         if source.get(key) != record[key]]
            if divergent:
                from news_flash_dedup.persist.divergence import (
                    AuditDivergenceError,
                )
                raise AuditDivergenceError(
                    f"audit doc {comparison_id!r} diverges on retry: "
                    f"{divergent}"
                ) from original
            verified.append(comparison_id)
        return verified

    def get_audit_doc(self, comparison_id: str) -> dict | None:
        """W-A 族③(d)-1：实时 GET 审计文档；None = 不存在。

        （persist/es_store.py:167-180 镜像——幂等对拍的核验读单源。）
        """
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

    def cas_main_record(self, record_id: str, body: Mapping, *,
                          expected_seq_no: int, expected_primary_term: int) -> int:
        """CAS 写主记录；返回新 seq_no。

        19:12 主窗口修复（UAT 红盘 run-1906）：ES 8 校验拒绝
        (if_seq_no=0, if_primary_term=0)（'ifSeqNo is set, but primary term is [0]'）。
        封口约定 (0,0)="不存在才创建" 在真 ES 上的合法表达是 op_type=create；
        存量 CAS 仍走 if_seq_no/if_primary_term（primary_term>=1）。
        """
        if expected_seq_no == 0 and expected_primary_term == 0:
            response = self.client.index(
                index=self.config.items_index,
                id=record_id,
                document=dict(body),
                op_type="create",
                refresh="wait_for",
            )
        else:
            response = self.client.index(
                index=self.config.items_index,
                id=record_id,
                document=dict(body),
                if_seq_no=expected_seq_no,
                if_primary_term=expected_primary_term,
                refresh="wait_for",
            )
        return int(response.get("_seq_no", 0))

    def get_main_record(self, record_id: str) -> dict | None:
        """实时 GET 主记录；返回 None 表示不存在。"""
        from elasticsearch import NotFoundError

        try:
            response = self.client.get(
                index=self.config.items_index, id=record_id, realtime=True,
            )
        except NotFoundError:
            return None
        return {
            "source": response["_source"],
            "seq_no": int(response["_seq_no"]),
            "primary_term": int(response["_primary_term"]),
        }

    def advance_decision_watermark(self, scope_id: str, business_date: str,
                                       arrival_seq: int) -> int:
        """推进 `decision_watermark_seq`（CAS）。"""
        # 用 audit-index 的 _control/<scope|date> 文档作为水位登记
        key = f"{scope_id}|{business_date}"
        try:
            current = self.client.get(
                index=self.control_index, id=key, realtime=True,
            )
            prev_seq = int(current["_source"].get("decision_watermark_seq", 0))
            seq_no = int(current["_seq_no"])
            primary_term = int(current["_primary_term"])
            if arrival_seq <= prev_seq:
                raise RuntimeError(
                    f"decision_watermark cannot regress: {prev_seq} -> {arrival_seq}"
                )
            # 19:28 主窗口修复（UAT 红盘 run-1923）：返回值必须是新水位值 arrival_seq
            # （05:34 裁定：watermark 语义=arrival_seq 水位值），原 prev_seq+1 系"更新计数"赝品。
            self.client.index(
                index=self.control_index, id=key,
                document={
                    "scope_id": scope_id, "business_date": business_date,
                    "decision_watermark_seq": arrival_seq,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                },
                if_seq_no=seq_no,
                if_primary_term=primary_term,
                refresh="wait_for",
            )
            return arrival_seq
        except Exception as exc:
            from elasticsearch import NotFoundError
            if isinstance(exc, NotFoundError):
                # 首次推进
                self.client.create(
                    index=self.control_index, id=key,
                    document={
                        "scope_id": scope_id, "business_date": business_date,
                        "decision_watermark_seq": arrival_seq,
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                    },
                    refresh="wait_for",
                )
                # 19:28 主窗口修复：首次推进同样返回新水位值（原 return 1 同为赝品计数）
                return arrival_seq
            raise

    def snapshot_index_names(self) -> list[str]:
        """跑前/跑后索引清单快照（`_cat/indices`）。"""
        response = self.client.cat.indices(format="json", h="index")
        return [item.get("index", "") for item in response]

    def cleanup(self) -> list[str]:
        """dry-run + 实际删除 **本 run** 的 P17 隔离索引；返回删除清单。

        R9 外部审核 F8c 修复（主窗口 06:3x）：原先删除所有匹配
        P17_INDEX_PREFIX_PATTERN 的索引——同 UAT 集群**其他 run** 的
        items/audits 证据索引会被跨 run 全删。现限定本 run_uuid 前缀。

        W2Fγ（M-1）：显式含本 run control 索引——control 名带 "-control"
        后缀不满足硬正则 fullmatch，旧过滤把它排除（每 UAT run 残留 1
        索引）；control 名构造期已过 validate_p17_control_index_name
        独立硬闸，此处按恒等匹配精确含入（94 保留索引纪律不受影响——
        control 为本 run 创建，run_prefix 限定不变）。
        """
        snapshot = self.snapshot_index_names()
        run_prefix = f"p17-batch-{self.config.run_uuid}-"
        targets = [
            name for name in snapshot
            if name.startswith(run_prefix)
            and (P17_INDEX_PREFIX_PATTERN.fullmatch(name)
                 or name == self.control_index)
        ]
        for name in targets:
            self.client.indices.delete(index=name)
        return targets


def build_es_client_from_env() -> Any:
    """从 TEST_ES_* 环境变量构造 elasticsearch.Elasticsearch 客户端（UAT only）。

    verify_certs=False（W2Fγ WA4b-35⑥ 登记，非缺陷静默遗留）：UAT 集群
    自签证书既定事实（T1/T2/T3 真机接入沿用）；本函数与客户端仅供 UAT
    （assert_p17_uat_open 闸门 + APPROVED_UAT_* 白名单），生产接管时
    必须翻转并核对 CA 链（接入缺口登记候选，不静默沿用）。
    """
    from elasticsearch import Elasticsearch
    assert_p17_uat_open()
    return Elasticsearch(
        f"{APPROVED_UAT_SCHEME}://{APPROVED_UAT_HOST}:{APPROVED_UAT_PORT}",
        basic_auth=(os.environ.get("TEST_ES_USER", ""),
                    os.environ.get("TEST_ES_PASSWORD", "")),
        verify_certs=False,
        request_timeout=30,
    )


__all__ = [
    "P17_CONTROL_SUFFIX",
    "P17_INDEX_PREFIX_PATTERN",
    "P17UATGatesNotOpen",
    "P17IndexPrefixInvalid",
    "P17_UAT_ENV_FLAG",
    "ProductionAccessDenied",
    "RealESCommitStore",
    "RealESConfig",
    "assert_p17_uat_open",
    "build_es_client_from_env",
    "build_p17_index_prefix",
    "validate_p17_control_index_name",
    "validate_p17_index_name",
]