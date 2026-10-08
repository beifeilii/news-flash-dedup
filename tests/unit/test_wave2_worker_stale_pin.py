# -*- coding: utf-8 -*-
"""修复波 2·A2-09 卫测：stale 语义产出自重试环内（删尾行零行为差；误删环内行即红）。"""
from __future__ import annotations

from test_recall_worker import (_CountingStore, _doc, _MutableWatermark,
                                _worker, DAY, SCOPE)


def test_stale_exhaustion_outcome_from_retry_loop():
    doc = _doc(2, "正文二。")
    watermark = _MutableWatermark(visible=0, prepared=99)  # lexical 恒过期
    store = _CountingStore()
    worker, _ = _worker("live", docs=[doc], store=store, watermark=watermark,
                        max_stale_retries=1)
    report = worker.drive_once(SCOPE, DAY)
    assert report.stale == (doc["record_id"],)
    assert report.committed == ()
    assert store.protocol_calls["write_main_record"] == 0
