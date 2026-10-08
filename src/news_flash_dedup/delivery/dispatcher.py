"""P20 调度器：扫 pending + 调度领取 CAS。

按 10 §6.5 第 1 项 + P20-设计-WIP §2：扫 `delivery_state=pending AND
now >= next_delivery_at AND now < expires_at`；fake dict 插入序遍历
（无分页无排序——:40 `store.main_records.items()`；真层
`_list_record_ids` match_all size=500 封顶后客户端排序，
es_store.py:257-264；UAT 规模封顶已自承 es_store.py:158——W2 修复波 2
(a)-11 A4-D4 锚勘正：旧述"分页按 record_id"失实）。
"""

from __future__ import annotations

import logging

from news_flash_dedup.deadline import parse_utc_iso

from .coordinator import parse_iso_utc
from .fake_store import FakeDeliveryStore

_log = logging.getLogger(__name__)


def scan_pending(store: FakeDeliveryStore, *, now: str,
                 skip_stats: dict[str, int] | None = None) -> list[str]:
    """扫可调度的主记录 record_id 列表。

    单元测试传 now 字符串；生产传 datetime.now(timezone.utc).isoformat()。

    窗口W2Fγ（A1）：now 与记录字段共用 coordinator.parse_iso_utc 归一
    （naive 按 UTC 解释、aware 归一 UTC——旧裸 fromisoformat 对 naive/aware
    混比抛 TypeError 逃逸）；解析移入 now_ts 分支——now="" 零校验形态
    不再触碰记录字段（旧实现先解析后短路，畸形记录字段在零校验形态下
    同样裸 ValueError 白崩）。

    窗口W3F（W3d-F-NEW-1，A15 口径补齐调度扫描路径）：非零校验形态下
    next_delivery_at/delivery_deadline_at 空串=存量历史数据不拦（对应闸
    跳过，对齐 receive/update_lease 跳闸同纪律）；畸形串=该记录跳过出
    扫描批 + skip_stats["malformed_record_fields"] 计数（传入时）+ 日志
    留痕——旧裸 ValueError 炸整批收口（expires_at 同族裸解析一并跳闸）；
    合法记录扫描语义逐字节不变。
    """
    now_ts = parse_iso_utc(now) if now else None
    out: list[str] = []
    for rid, rec in store.main_records.items():
        if rec.delivery_state != "pending":
            continue
        if now_ts is None:
            # 零校验形态（现状行为保持）：不解析记录字段，直接收录
            out.append(rid)
            continue
        try:
            next_at = (parse_iso_utc(rec.next_delivery_at)
                       if rec.next_delivery_at else None)
            # D24 补截止闸；D25 注记校正（三轮审计 C-04 证伪原"涵摄恒成立"
            # 断言）：min(+24h, expires_at) 钳制仅 g0_persistence.py:442/478
            # 两处写入路径有，persist/es_store.py、persist/coordinator.py、
            # commit/coordinator.py 三处均无钳制（该三层当前不透传
            # expires_at——结构性缺口已登记 C-08/呈裁批）。本闸保证
            # now < delivery_deadline_at；对无钳制路径，expires_at 执法
            # 依赖受理/持久层 _live 逐点把守，本层不声称涵摄。
            deadline = (parse_iso_utc(rec.delivery_deadline_at)
                        if rec.delivery_deadline_at else None)
            # P2（C-08/10 §6.340，窗口X 并线移植）：字段非空时同时校验
            # now < expires_at——空串=现状行为（受理/持久层 _live 把守，本层
            # 不声称涵摄，D25 注记）
            expires = (parse_iso_utc(rec.expires_at)
                       if rec.expires_at else None)
        except (ValueError, TypeError, AttributeError):
            # W3F（W3d-F-NEW-1）：畸形时间字段记录 fail-closed 跳过不炸批
            # （A15 跳闸同族：批量场景的记录级拒绝）+ 计数 + 日志留痕
            if skip_stats is not None:
                skip_stats["malformed_record_fields"] = (
                    skip_stats.get("malformed_record_fields", 0) + 1)
            _log.warning("scan_pending 跳过畸形时间字段记录 %r"
                         "（next_delivery_at=%r delivery_deadline_at=%r"
                         " expires_at=%r）",
                         rid, rec.next_delivery_at,
                         rec.delivery_deadline_at, rec.expires_at)
            continue
        if expires is not None and now_ts >= expires:
            continue
        if ((next_at is None or now_ts >= next_at)
                and (deadline is None or now_ts < deadline)):
            out.append(rid)
    return out


def scan_expired_delivering(store, *, now: str,
                            skip_stats: dict[str, int] | None = None) -> list[str]:
    """W-A 族③(f)（B3/11§4.3 L166）：到期 delivering 租约重扫。

    返回 delivery_state=="delivering" 且租约 lease_until <= now 的
    record_id 列表（恢复语义=新代次接管，旧工作者不能凭新 CAS 覆盖——
    11 L166/L167；③变更 N23 F6-d 挂账随本件销账，B3 条款 46）。

    - now 必填且经 parse_utc_iso 强校验（空串/naive→ValueError）——恢复
      扫描必须显式，与 scan_pending 的 now_ts 宽松口径差异化（调度可宽松、
      恢复必须显式：零校验形态下"到期"判定无从成立，静默放行=滞留回潮）。
    - 租约来源=仓储协议 lease_for(record_id)（fake 按 record_id 匹配
      lease_index；真层读主记录内嵌 delivery_lease）。
    - delivering 无租约=不一致状态：跳闸 + skip_stats
      ["delivering_without_lease"]+1 + log.warning（可观测不静默）；
      lease_until 解析失败 → skip_stats["malformed_record_fields"]+1
      （复用 scan_pending 跳闸词表）。
    - 返回按 record_id 排序（确定性契约）；截止/到期再核验由重新领取
      路径（receive 五前置）执法，本函数只出清单（单一职责）。
    """
    now_ts = parse_utc_iso(now)          # 空串/naive/畸形 → ValueError 显式拒
    out: list[str] = []
    for rid, rec in store.main_records.items():
        if rec.delivery_state != "delivering":
            continue
        lease = store.lease_for(rid)
        if lease is None:
            if skip_stats is not None:
                skip_stats["delivering_without_lease"] = (
                    skip_stats.get("delivering_without_lease", 0) + 1)
            _log.warning("scan_expired_delivering 跳过无租约 delivering 记录 %r",
                         rid)
            continue
        try:
            lease_until = parse_utc_iso(lease.lease_until)
        except (ValueError, TypeError, AttributeError):
            if skip_stats is not None:
                skip_stats["malformed_record_fields"] = (
                    skip_stats.get("malformed_record_fields", 0) + 1)
            _log.warning("scan_expired_delivering 跳过畸形租约字段记录 %r"
                         "（lease_until=%r）", rid, lease.lease_until)
            continue
        if lease_until <= now_ts:
            out.append(rid)
    return sorted(out)


__all__ = ["scan_expired_delivering", "scan_pending"]
