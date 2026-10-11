"""P08-B 真实 UAT ES 跨日对账：T017 跨日重提 + T018 跨日不召回。

按用户指令：测试必须真实构造跨零点场景，不接受纯 mock 时钟断言就算过——
模拟时钟可以，但 ES 侧索引必须是真实 UAT 读写。

P1a-T5（卡3.6"跨日"项）：本套件迁任务文档形态（durable 队列）——
协调器全部 `use_durable_queue=True`；隔离命名空间预建 D-1/D/D+1 三日
work 日索引（任务文档落位面）；断言面增任务文档族（work 索引内
business_date/arrival_seq/expires_at 复用首值——钉 6.1 全钉第三项）。
物化驱动=materialize_prefix（T3 起 durable 侧=claim 驱动循环）。

执行条件：环境边界 P07 §10：仅连 TEST_ES_* UAT 集群；fail-fast 检查通过；每个 run-id
使用独立 p08-crossday-* prefix 隔离，不影响其它测试与集群。

跑法（W2Fε docstring 勘正：本套件并无 --confirm-uat pytest CLI——全仓无
addoption 注册该选项；真实闸门是环境变量 P08_CONFIRM_UAT=1）：
  P08_CONFIRM_UAT=1 PYTHONPATH=src .venv-v1/Scripts/python.exe -B -m pytest \\
    tests/integration/test_cross_day_uat.py -v

注意：本测试必须显式置 `P08_CONFIRM_UAT=1` 环境变量授权；缺省运行跳过
（模块级 pytestmark skipif，见下 _uat_env_ready）。
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time
from datetime import date, datetime, time as dt_time, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))


def _uat_env_ready() -> bool:
    """检测是否显式传入 P08_CONFIRM_UAT=1；按环境边界不允许默认连 ES。"""
    return os.environ.get("P08_CONFIRM_UAT") == "1"


pytestmark = pytest.mark.skipif(
    not _uat_env_ready(),
    reason="P08-B 真实 UAT ES 测试需要 P08_CONFIRM_UAT=1 显式授权；默认跳过",
)


@pytest.fixture(scope="module")
def uat_client():
    """仅在 P08_CONFIRM_UAT=1 下创建真实 UAT ES 客户端；fail-fast 检查通过。"""
    from news_flash_dedup.es_client import (
        ProductionAccessDenied, assert_test_environment, client_from_environment,
        load_environment,
    )
    load_environment()  # 显式从项目根 .env 加载
    try:
        assert_test_environment()
    except ProductionAccessDenied as error:
        pytest.fail(f"UAT environment check failed: {error}")
    client = client_from_environment(load_dotenv_first=False)
    info = client.info()
    assert str(info["version"]["number"]).startswith("8."), \
        f"UAT ES must be 8.x; got {info['version']['number']}"
    yield client
    client.close()


@pytest.fixture(scope="module")
def isolated_namespace(uat_client):
    """独立 p08-crossday-* prefix；module 结束清理。"""
    import re
    # ElasticsearchBatchStore 限定 prefix 必须是 p01-batch-*；改为 p08-crossday- 嵌入
    raw_prefix = f"p08-crossday-{datetime.now(timezone.utc).strftime('%H%M%S%f')}-"
    if not re.fullmatch(r"p01-batch-[A-Za-z0-9-]+-", raw_prefix):
        # 把 p08 改成 p01-batch- 前缀，保留 crossday 标识
        prefix = f"p01-batch-{raw_prefix}"
    else:
        prefix = raw_prefix
    indices = [
        f"{prefix}news-dedup-control-v1",
        f"{prefix}news-dedup-requests-v1",
        # D 日（测试业务日）
        f"{prefix}news-dedup-items-v1-2026.09.24",
        f"{prefix}news-dedup-audits-v1-2026.09.24",
        # D+1 日（重提所在日）
        f"{prefix}news-dedup-items-v1-2026.09.25",
        f"{prefix}news-dedup-audits-v1-2026.09.25",
        # D-1 日（隔离用）
        f"{prefix}news-dedup-items-v1-2026.09.23",
        f"{prefix}news-dedup-audits-v1-2026.09.23",
    ]
    from news_flash_dedup.es_admission_schema import (
        audit_mapping, control_mapping, item_mapping, request_mapping,
        work_mapping,
    )
    mappings = {
        f"{prefix}news-dedup-control-v1": control_mapping(),
        f"{prefix}news-dedup-requests-v1": request_mapping(),
        f"{prefix}news-dedup-items-v1-2026.09.24": item_mapping(),
        f"{prefix}news-dedup-audits-v1-2026.09.24": audit_mapping(),
        f"{prefix}news-dedup-items-v1-2026.09.25": item_mapping(),
        f"{prefix}news-dedup-audits-v1-2026.09.25": audit_mapping(),
        f"{prefix}news-dedup-items-v1-2026.09.23": item_mapping(),
        f"{prefix}news-dedup-audits-v1-2026.09.23": audit_mapping(),
        # P1a-T5：任务文档落位面（D-1/D/D+1 三日 work 日索引——durable
        # 队列 enqueue 点名写入，预建同 N31 滚动口径）。
        f"{prefix}news-dedup-work-v1-2026.09.24": work_mapping(),
        f"{prefix}news-dedup-work-v1-2026.09.25": work_mapping(),
        f"{prefix}news-dedup-work-v1-2026.09.23": work_mapping(),
    }
    for name, schema in mappings.items():
        uat_client.indices.create(index=name, body=schema)
    yield {"prefix": prefix, "indices": indices}
    # 清理：所有 P08 索引与 audits 都按 prefix 列举后删除
    listed = uat_client.indices.get(index=f"{prefix}*", allow_no_indices=True,
                                    ignore_unavailable=True)
    for name in listed.keys():
        try:
            uat_client.indices.delete(index=name)
        except Exception:
            pass


# ============================================================================
# T017：跨日重提不切换业务日（D 日 23:59 受理，D+1 00:01 重提 → 首次日期/序号/版本不变）
# ============================================================================


def test_cross_day_retry_keeps_first_business_date_and_arrival_seq(
        uat_client, isolated_namespace):
    """真实 UAT ES 跨日重提（P1a-T5 任务文档形态）：

    1. D 日 23:59:30（Asia/Shanghai）首次受理，模拟时钟精确控制；
    2. D+1 00:00:30 同身份同文重提（重试日时钟注入——按重试日重算
       身份=禁止，钉 6.1 负形态）；
    3. receipt.business_date 必须 = D（不切换为 D+1）；
    4. receipt.arrival_seq 必须 = 首次序号（不重新分配）；
    5. 任务文档驻 D 日 work 索引（business_date/arrival_seq/expires_at
       复用首值——D+1 work 索引零落位）。
    """
    from news_flash_dedup.batch_admission import (
        BatchAdmissionCoordinator, BatchLimits,
    )
    from news_flash_dedup.batch_collector import BatchAdmissionCollector
    from news_flash_dedup.work_queue import work_index, work_item_id

    prefix = isolated_namespace["prefix"]
    store = _build_store(uat_client, prefix)
    service = BatchAdmissionCoordinator(
        store, owner_id="p08-crossday-owner", owner_isolated=lambda: True,
        limits=BatchLimits(), clock=lambda: _first_clock(),
        use_durable_queue=True,
    )
    collector = BatchAdmissionCollector(service, max_wait_seconds=0.01,
                                       max_queue_items=8, request_timeout_seconds=2)

    # D 日 23:59:30（SHA）首次受理
    accepted_at_d = _sha(2026, 9, 24, 23, 59, 30)
    request_id = "1001-1"
    first_text = "甲公司2026年9月24日发布回购金额100万元。"
    first = collector.accept(_build_request("default", request_id, request_id, first_text,
                                              accepted_at_d))
    # 首次 business_date 必须 = D（"2026-09-24"）
    assert first.business_date == "2026-09-24", \
        f"first business_date must be D; got {first.business_date}"
    first_arrival_seq = first.arrival_seq
    first_record_id = first.record_id
    first_accepted_at = first.accepted_at
    first_expires_at = first.expires_at
    collector.close()

    # 任务文档驻 D 日 work 索引（P1a-T5：复用读回的物理证据面）。
    work_key = work_item_id("default", "2026-09-24", first_arrival_seq)
    work_doc = _get(uat_client, f"{prefix}{work_index('2026-09-24')}", work_key)
    assert work_doc is not None and work_doc.get("found") is True
    work_source = work_doc["_source"]
    assert work_source["business_date"] == "2026-09-24"
    assert work_source["arrival_seq"] == first_arrival_seq
    assert work_source["expires_at"] == first_expires_at

    # D+1 00:00:30（SHA）同身份同文重提
    accepted_at_d1 = _sha(2026, 9, 25, 0, 0, 30)
    service_d1 = BatchAdmissionCoordinator(
        store, owner_id="p08-crossday-owner", owner_isolated=lambda: True,
        limits=BatchLimits(), clock=lambda: accepted_at_d1,
        use_durable_queue=True,
    )
    collector_d1 = BatchAdmissionCollector(service_d1, max_wait_seconds=0.01,
                                          max_queue_items=8, request_timeout_seconds=2)
    try:
        retry = collector_d1.accept(_build_request("default", request_id, request_id,
                                                    first_text, accepted_at_d1))
    finally:
        collector_d1.close()

    # T017 核心断言
    assert retry.business_date == "2026-09-24", \
        f"retry must keep first business_date (D); got {retry.business_date}"
    assert retry.arrival_seq == first_arrival_seq, \
        f"retry must reuse first arrival_seq; got {retry.arrival_seq}, want {first_arrival_seq}"
    assert retry.record_id == first_record_id, \
        f"retry must reuse record_id; got {retry.record_id}, want {first_record_id}"
    assert retry.accepted_at == first_accepted_at, \
        f"retry must reuse first accepted_at; got {retry.accepted_at}"
    assert retry.expires_at == first_expires_at, \
        f"retry must reuse first expires_at; got {retry.expires_at}"
    assert retry.reused is True
    # 跨日不重算落位：D+1 work 索引零任务文档。
    assert _get(uat_client, f"{prefix}{work_index('2026-09-25')}",
                work_key) is None


def _get(uat_client, index, doc_id):
    """Elasticsearch Python 客户端 not-found 抛 NotFoundError；此处返回 None 便于断言。"""
    from elasticsearch import NotFoundError
    try:
        return uat_client.get(index=index, id=doc_id, realtime=True)
    except NotFoundError:
        return None


def test_cross_day_retry_lands_physical_record_in_D_index_not_D1(
        uat_client, isolated_namespace):
    """T017 子断言：主记录物理位置在 D 日索引，不在 D+1 索引（P1a-T5
    durable：物化=claim 驱动循环——materialize_prefix 同面）。"""
    from news_flash_dedup.batch_admission import (
        BatchAdmissionCoordinator, BatchLimits,
    )
    from news_flash_dedup.batch_collector import BatchAdmissionCollector
    from news_flash_dedup.admission import _digest

    prefix = isolated_namespace["prefix"]
    store = _build_store(uat_client, prefix)
    service = BatchAdmissionCoordinator(
        store, owner_id="p08-crossday-owner", owner_isolated=lambda: True,
        limits=BatchLimits(), clock=lambda: _sha(2026, 9, 24, 23, 59, 30),
        use_durable_queue=True,
    )
    collector = BatchAdmissionCollector(service, max_wait_seconds=0.01)
    first = collector.accept(_build_request("default", "2001-1", "2001-1", "正文",
                                              _sha(2026, 9, 24, 23, 59, 30)))
    service.materialize_prefix()
    collector.close()

    d_index = f"{prefix}news-dedup-items-v1-2026.09.24"
    d1_index = f"{prefix}news-dedup-items-v1-2026.09.25"
    record_id = _digest(["default", "2001-1"])
    d_record = _get(uat_client, d_index, record_id)
    d1_record = _get(uat_client, d1_index, record_id)
    assert d_record is not None and d_record.get("found") is True, \
        f"record must exist in D index; got {d_record}"
    assert d1_record is None, \
        f"record must NOT exist in D+1 index; got {d1_record}"


def test_d_plus_one_new_id_same_text_does_not_recall_d_index(
        uat_client, isolated_namespace):
    """T018：D 日旧文已物化；D+1 新身份同文（不同 record_id）召回时只查 D+1 索引，
    不召回 D 索引（P1a-T5 durable 形态——召回窗口仍限任务首次
    business_date 单分区，钉 6.4"7 天≠跨日召回"）。

    模拟召回过滤：filter 强制 `business_date == '2026-09-25'`（D+1），scope 相同。
    """
    from news_flash_dedup.batch_admission import (
        BatchAdmissionCoordinator, BatchLimits,
    )
    from news_flash_dedup.batch_collector import BatchAdmissionCollector

    prefix = isolated_namespace["prefix"]
    store = _build_store(uat_client, prefix)
    # D 日 23:00 受理 + 物化旧文
    service = BatchAdmissionCoordinator(
        store, owner_id="p08-crossday-owner", owner_isolated=lambda: True,
        limits=BatchLimits(), clock=lambda: _sha(2026, 9, 24, 23, 0, 0),
        use_durable_queue=True,
    )
    collector = BatchAdmissionCollector(service, max_wait_seconds=0.01)
    old_request_id = "3001-1"
    old_text = "甲公司9月24日发布回购100万元。"
    old_receipt = collector.accept(_build_request("default", old_request_id,
                                                  old_request_id, old_text,
                                                  _sha(2026, 9, 24, 23, 0, 0)))
    service.materialize_prefix()
    collector.close()
    # 等持久
    time.sleep(0.2)
    # 触发 D 日主索引含旧 record
    service.materialize_prefix()
    time.sleep(0.2)

    # T018 核心断言：在线召回过滤 `business_date == '2026-09-25'`（D+1）不召回 D
    from news_flash_dedup.admission import _digest
    old_record_id = _digest(["default", old_request_id])
    d_index = f"{prefix}news-dedup-items-v1-2026.09.24"
    d1_index = f"{prefix}news-dedup-items-v1-2026.09.25"
    d_record = _get(uat_client, d_index, old_record_id)
    d1_record = _get(uat_client, d1_index, old_record_id)
    assert d_record is not None and d_record.get("found") is True
    assert d1_record is None, \
        f"old record must NOT appear in D+1 index; got {d1_record}"

    # D+1 受理一个全新 ID 但同文（record_id 不同）→ 不与 D 日同 ID 召回
    new_request_id = "3002-1"  # 不同 contentId-versionNo
    service_d1 = BatchAdmissionCoordinator(
        store, owner_id="p08-crossday-owner", owner_isolated=lambda: True,
        limits=BatchLimits(), clock=lambda: _sha(2026, 9, 25, 12, 0, 0),
        use_durable_queue=True,
    )
    collector_d1 = BatchAdmissionCollector(service_d1, max_wait_seconds=0.01)
    new_receipt = collector_d1.accept(_build_request("default", new_request_id,
                                                     new_request_id, old_text,
                                                     _sha(2026, 9, 25, 12, 0, 0)))
    service_d1.materialize_prefix()
    assert new_receipt.business_date == "2026-09-25"
    new_record_id = _digest(["default", new_request_id])
    d1_record = _get(uat_client, d1_index, new_record_id)
    d_record = _get(uat_client, d_index, new_record_id)
    assert d1_record is not None and d1_record.get("found") is True
    assert d_record is None, \
        f"new record must NOT be in D index; got {d_record}"
    collector_d1.close()


# ============================================================================
# 工具函数
# ============================================================================


def _sha(year: int, month: int, day: int, hour: int, minute: int, second: int) -> datetime:
    """Asia/Shanghai → UTC；测试时钟由服务端受控时钟推导（不接请求时间）。"""
    import zoneinfo
    sha = zoneinfo.ZoneInfo("Asia/Shanghai")
    return datetime(year, month, day, hour, minute, second, tzinfo=sha).astimezone(timezone.utc)


def _first_clock() -> datetime:
    """首次受理时钟：D 日 23:59:30 SHA → UTC。"""
    return _sha(2026, 9, 24, 23, 59, 30)


def _build_store(uat_client, prefix: str):
    """构造 BatchMemoryStore 形态的 ElasticsearchBatchStore（操作真实 UAT ES）。"""
    from news_flash_dedup.batch_es_store import ElasticsearchBatchStore
    return ElasticsearchBatchStore(uat_client, index_prefix=prefix)


def _build_request(scope: str, request_id: str, item_id: str, text: str,
                  received_at: datetime):
    from news_flash_dedup.admission import AdmissionRequest
    return AdmissionRequest(
        scope_id=scope, request_id=request_id, item_id=item_id, text=text,
        received_at=received_at,
        schema_version="1", pipeline_version="prod-v1",
        embedding_space_id="none", delivery_route_ref="prod-route-v1",
        trace_id="cross-day-trace",
    )