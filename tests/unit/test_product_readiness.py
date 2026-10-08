"""B4/P21 红测（log\设计-P21接线包.md §八-8.1）：Q3 就绪聚合 product/readiness.py。

红测清单映射：
- R-Q8  /ready 三态（端口半）：fatal→ready=False；degraded→ready=True 带码上报；
        码词表闭合——未知码注入→拒收；fixed/shadow/live 三态码表分册
        （§七-7.2：fixed 三 fatal；shadow +五通道/水位 fatal+embedding degraded；
        live +vector fatal、degraded 空）
- R-Q9  码词表完整闭合：三态fatal/degraded集合逐一枚举钉死；recall_chain_unavailable
        后缀词表=五通道；探针乱返回值→拒收
"""

from __future__ import annotations

import pytest

from news_flash_dedup.product.readiness import (
    ReadinessPort,
    ReadinessVocabularyError,
)

def _probes(**name_to_code):
    return {name: (lambda code=code: code) for name, code in name_to_code.items()}


# ---------- R-Q9 码词表闭合 ----------

def test_rq9_vocabulary_closed_per_mode():
    assert ReadinessPort.FATAL_CODES["fixed"] == frozenset({
        "es_control_read_failed", "collector_not_running",
        "delivery_route_unconfigured"})
    assert ReadinessPort.DEGRADED_CODES["fixed"] == frozenset()
    assert ReadinessPort.FATAL_CODES["shadow"] == frozenset({
        "es_control_read_failed", "collector_not_running",
        "delivery_route_unconfigured", "watermark_unreadable",
        "recall_chain_unavailable:hash", "recall_chain_unavailable:near",
        "recall_chain_unavailable:bm25", "recall_chain_unavailable:embedding",
        "recall_chain_unavailable:entity"})
    assert ReadinessPort.DEGRADED_CODES["shadow"] == frozenset({
        "embedding_space_unconfirmed"})
    assert ReadinessPort.FATAL_CODES["live"] == frozenset({
        "es_control_read_failed", "collector_not_running",
        "delivery_route_unconfigured", "watermark_unreadable",
        "recall_chain_unavailable:hash", "recall_chain_unavailable:near",
        "recall_chain_unavailable:bm25", "recall_chain_unavailable:embedding",
        "recall_chain_unavailable:entity", "vector_channel_unavailable"})
    assert ReadinessPort.DEGRADED_CODES["live"] == frozenset()


def test_rq9_mode_validation():
    with pytest.raises(ValueError):
        ReadinessPort({}, mode="bogus")
    with pytest.raises(ValueError):
        ReadinessPort(None, mode="fixed")


# ---------- R-Q8 三态（端口半） ----------

def test_rq8_all_probes_green():
    port = ReadinessPort(_probes(es=None, collector=None), mode="fixed")
    assert port.issues() == (True, [])


def test_rq8_fatal_code_flips_not_ready():
    port = ReadinessPort(
        _probes(es="es_control_read_failed", collector=None), mode="fixed")
    ready, codes = port.issues()
    assert ready is False
    assert codes == ["es_control_read_failed"]


def test_rq8_degraded_reports_but_stays_ready():
    port = ReadinessPort(
        _probes(embedding="embedding_space_unconfirmed"), mode="shadow")
    ready, codes = port.issues()
    assert ready is True
    assert codes == ["embedding_space_unconfirmed"]


def test_rq8_codes_preserve_probe_declaration_order():
    port = ReadinessPort(
        {"b_probe": lambda: "collector_not_running",
         "a_probe": lambda: "es_control_read_failed"},
        mode="fixed")
    ready, codes = port.issues()
    assert ready is False
    assert codes == ["collector_not_running", "es_control_read_failed"]


@pytest.mark.parametrize("mode,code", [
    ("fixed", "embedding_space_unconfirmed"),   # shadow 专属 degraded 码在 fixed 拒收
    ("fixed", "watermark_unreadable"),          # shadow/live 专属 fatal 在 fixed 拒收
    ("fixed", "recall_chain_unavailable:hash"),
    ("shadow", "vector_channel_unavailable"),   # live 专属 fatal 在 shadow 拒收
    ("live", "embedding_space_unconfirmed"),    # degraded 退役后（live）拒收
    ("shadow", "recall_chain_unavailable:bogus"),
    ("fixed", "made_up_code"),
])
def test_rq8_unknown_code_rejected_vocabulary_closed(mode, code):
    port = ReadinessPort({"probe": lambda: code}, mode=mode)
    with pytest.raises(ReadinessVocabularyError):
        port.issues()


@pytest.mark.parametrize("mode,code", [
    ("shadow", "recall_chain_unavailable:hash"),
    ("shadow", "recall_chain_unavailable:embedding"),
    ("shadow", "watermark_unreadable"),
    ("live", "vector_channel_unavailable"),
    ("live", "recall_chain_unavailable:entity"),
])
def test_rq8_mode_specific_fatal_codes_accepted(mode, code):
    port = ReadinessPort({"probe": lambda: code}, mode=mode)
    ready, codes = port.issues()
    assert ready is False
    assert codes == [code]


def test_rq8_malformed_probe_return_rejected():
    port = ReadinessPort({"probe": lambda: 123}, mode="fixed")
    with pytest.raises(ReadinessVocabularyError):
        port.issues()
    port = ReadinessPort({"probe": lambda: ""}, mode="fixed")
    with pytest.raises(ReadinessVocabularyError):
        port.issues()
