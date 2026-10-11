"""P07-B 真实 ES 适配：隔离 UAT 索引集创建与检查。

产品化入口替代 `probe_g0_recovery.py:resources()` 与 `bench_batch_admission.py:provision()`。
只连 UAT ES；不连生产（详见 P07-接入实施设计.md §10）。

W2 批36（D25 增量·裁定前安全动作）：
- create 异常以 `ProvisionError` 形包装（固定 reason 码表与 bench 原件同形；
  传输细节留 `__cause__`，保链纪律）。

W2Fδ2（D25 命名四位一体裁定落地——09-28 用户授权主窗口候选）：
- 建/闸/访/清全走 `index_prefix`+逻辑名：`required_indices` 增 `index_prefix`
  参数（缺省 "" = 10 §2 canonical 生产形），provision 以验过闸的前缀创建
  物理名（建/闸同前缀维度），与 `ElasticsearchBatchStore._physical` 强制
  前缀（访）、bench/probe 两原件（建）、`lifecycle` 双兼容硬正则（清）四位一体；
- W2 批36 登记的"provision 无前缀 canonical 名 vs store 强制前缀**不可配对**
  （配对必 404）"情形自此消除——store 读 provision 产物名逐字相等；
- 半创建清理（条目36③解禁）：create 失败（传输异常或未全 ack）时对本批
  已建索引及当事索引逐个点名删除补偿；补偿失败不掩盖原异常，记入异常
  `cleanup_deleted`/`cleanup_failed` 属性（delete 404 视为已清理=10 §9.3/L458 口径）。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import date, datetime, timedelta
from typing import Any

from .admission import BUSINESS_ZONE, CONTROL_INDEX, REQUEST_INDEX
from .es_admission_schema import (
    audit_mapping,
    control_mapping,
    day_index,
    item_mapping,
    request_mapping,
    work_mapping,
)


class ProvisionError(RuntimeError):
    """与 `scripts/bench_batch_admission.py:ProvisionError` 原件同形（W2 批36）：
    只放固定 reason 码入消息；传输异常细节留 `__cause__`（保链纪律）。"""

    REASONS = frozenset({"prefix_exists", "index_listing_failed", "index_listing_invalid",
                         "index_create_failed", "index_settings_failed"})

    def __init__(self, reason: str):
        if reason not in self.REASONS:
            raise ValueError("invalid provision reason")
        self.reason = reason
        # W2Fδ2（条目36③）：半创建清理补偿结果挂点；未走补偿路径时为空——
        # 不冒充已清理。
        self.cleanup_deleted: tuple[str, ...] = ()
        self.cleanup_failed: tuple[tuple[str, str], ...] = ()
        super().__init__(reason)


def _cleanup_half_created(client: Any,
                          names: list[str]) -> tuple[tuple[str, ...], tuple[tuple[str, str], ...]]:
    """W2Fδ2（条目36③解禁）：create 失败时对本批已建索引及当事索引逐个点名
    删除补偿（不用宽通配符；本批命名空间已经 fresh-namespace 闸验明归本 run）。

    补偿失败绝不掩盖原异常：逐名记入返回的 (deleted, failed)，由调用方挂到
    待抛异常属性上（不静默、可审计）。delete 404 视为已清理（10 §9.3/L458 口径）。
    """
    deleted: list[str] = []
    failed: list[tuple[str, str]] = []
    for name in names:
        try:
            client.indices.delete(index=name)
        except Exception as error:
            if getattr(error, "status_code", None) == 404:
                deleted.append(name)
            else:
                failed.append((name, type(error).__name__))
        else:
            deleted.append(name)
    return tuple(deleted), tuple(failed)


def required_indices(business_day: date, index_prefix: str = "") -> dict[str, dict]:
    """返回某业务日需要的全部索引名 → mapping（含 control/request/两日 items/两日 audits/两日 work）。

    W2Fδ2（09-28 用户授权四位一体裁定）：`index_prefix` 缺省 "" = 10 §2
    canonical 生产形；非空必须全匹配 `p01-batch-[A-Za-z0-9-]+-`（与
    `ElasticsearchBatchStore`/bench/probe 两原件/lifecycle 同口径，fail-closed）。

    P1a-T1（蓝图 §七-2 裁定）：每日增 work 任务索引（`day_index(iso,
    "work")`——受理主路径，队列写前必须在场；当/次日两窗与 items 同梯）。
    tombstones 维持按需 ensure 口径（T3 纪律），不在本清单。
    """
    if index_prefix and not re.fullmatch(r"p01-batch-[A-Za-z0-9-]+-", index_prefix):
        raise ValueError("isolated prefix required (p01-batch-*)")
    result: dict[str, dict] = {
        index_prefix + CONTROL_INDEX: control_mapping(),
        index_prefix + REQUEST_INDEX: request_mapping(),
    }
    for current in (business_day, business_day + timedelta(days=1)):
        iso = current.isoformat()
        result[index_prefix + day_index(iso)] = item_mapping()
        result[index_prefix + day_index(iso, "audits")] = audit_mapping()
        result[index_prefix + day_index(iso, "work")] = work_mapping()
    for mapping in result.values():
        mapping.setdefault("settings", {}).update(number_of_shards=1, number_of_replicas=0)
    return result


def provision_isolated_indices(client: Any, index_prefix: str,
                                business_day: date | None = None,
                                allow_no_indices: bool = True,
                                ignore_unavailable: bool = True) -> dict[str, dict]:
    """在 `index_prefix` 下创建隔离索引集；若已存在则拒绝（fresh namespace）。

    W2Fδ2（09-28 用户授权四位一体裁定）：本函数创建 `index_prefix`+逻辑名
    带前缀物理名（见 required_indices），与强制前缀的 `ElasticsearchBatchStore`
    配对连通——W2 批36 登记的"无前缀 canonical 名**不可配对**（配对必 404）"
    情形已随裁定落地消除；建/闸/访/清同前缀维度，调用方仍不得自行拼接改名。
    半创建清理（条目36③解禁）：create 失败时清理本批已建索引及当事索引，
    补偿结果挂异常 `cleanup_deleted`/`cleanup_failed` 属性。
    """
    business_day = business_day or datetime.now(BUSINESS_ZONE).date()
    # 窗口W2δ（L-3）：startswith→fullmatch，对齐 batch 侧强度（fail-closed 向；
    # 空 run-id/缺尾横杠/前缀外字符在 startswith 时代均被放过）。
    if not re.fullmatch(r"p01-batch-[A-Za-z0-9-]+-", index_prefix):
        raise ValueError("isolated prefix required (p01-batch-*)")
    existing = client.indices.get(
        index=index_prefix + "*",
        allow_no_indices=allow_no_indices,
        ignore_unavailable=ignore_unavailable,
    )
    # 三轮审计 C-03 修复（D25）：elastic_transport 8.x 的
    # ObjectApiResponse 未注册 collections.abc.Mapping（实测
    # issubclass=False）——仅认 Mapping 会让 fresh-namespace 闸对真
    # ES8 客户端静默失效（恒判空）。.body 回退取原始 dict。
    existing_map = (existing if isinstance(existing, Mapping)
                    else getattr(existing, "body", None))
    # 窗口M（F4-5，窗口X 并线移植）：异型响应 fail-closed——既非 Mapping
    # 又无 Mapping .body 时旧实现静默判空（fresh-namespace 闸形同虚设并
    # 继续建索引 = fail-open），改为即拒。
    if not isinstance(existing_map, Mapping):
        raise RuntimeError(
            "provisioning: index listing response has unexpected shape "
            f"{type(existing).__name__} (neither Mapping nor Mapping .body; "
            "F4-5 fail-closed)"
        )
    existing_names = tuple(existing_map.keys())
    if any(not isinstance(name, str) or not name.startswith(index_prefix) for name in existing_names):
        raise RuntimeError("provisioning: existing index listing is invalid")
    if existing_names:
        raise RuntimeError("recovery namespace already exists; choose a fresh run-id")
    mappings = required_indices(business_day, index_prefix=index_prefix)
    created: list[str] = []
    for name, schema in mappings.items():
        # W2Fδ2（条目36③解禁）：create 失败（传输异常或未全 ack）→ 先清理
        # 本批已建索引及当事索引再抛错；补偿失败记录于异常属性，不掩盖原异常。
        try:
            response = client.indices.create(index=name, body=schema)
        except Exception as error:
            wrapped = ProvisionError("index_create_failed")
            wrapped.cleanup_deleted, wrapped.cleanup_failed = _cleanup_half_created(
                client, [*created, name])
            raise wrapped from error
        body = getattr(response, "body", response)
        if (not isinstance(body, Mapping) or body.get("acknowledged") is not True
                or body.get("shards_acknowledged") is not True):
            failure = RuntimeError(f"isolated index creation is not fully acknowledged: {name}")
            failure.cleanup_deleted, failure.cleanup_failed = _cleanup_half_created(
                client, [*created, name])
            raise failure
        created.append(name)
    # 窗口W2δ（L-4）：get_settings 异常与 settings 裸下标统一 fail-closed 包装
    # （旧码 KeyError/TypeError 裸穿，拓扑证据不足时无固定语义）。
    try:
        settings = client.indices.get_settings(index=",".join(created))
    except Exception as error:
        raise ProvisionError("index_settings_failed") from error
    settings_map = getattr(settings, "body", settings)
    try:
        topology = {
            name: {
                "shards": int(settings_map[name]["settings"]["index"]["number_of_shards"]),
                "replicas": int(settings_map[name]["settings"]["index"]["number_of_replicas"]),
            }
            for name in created
        }
    except (KeyError, TypeError, ValueError, AttributeError) as error:
        raise ProvisionError("index_settings_failed") from error
    return {"topology": topology, "index_prefix": index_prefix, "business_day": business_day.isoformat()}


def assert_environment(env: dict[str, str] | None = None) -> None:
    """从 `es_client` 复用 fail-fast 检查。"""
    from .es_client import assert_test_environment as _assert_test_environment
    _assert_test_environment(env)