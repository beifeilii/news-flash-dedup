"""P21 Q3 就绪聚合（B4 施工窗；设计=log\设计-P21接线包.md §三-Q3/§七-7.2）。

三态语义：fatal 码任一在→ready=False（端点 503）；仅 degraded 码→ready=True
带码上报（端点 200）；全绿→(True, [])。

码词表闭合（§七-7.2 分册，R-Q9 逐一枚举钉死）：
- fixed：fatal={es_control_read_failed, collector_not_running,
  delivery_route_unconfigured}；degraded=∅（无召回链可宣告）；
- shadow：fatal=fixed 三码 + watermark_unreadable + 五通道
  recall_chain_unavailable:<channel>；degraded={embedding_space_unconfirmed}
  （N20-b 缺口明示——影子腿带码上岗）；
- live：fatal=shadow fatal + vector_channel_unavailable；degraded=∅
  （向量缺口退役为 fatal——live 真链不允许缺口上岗）。

探针协议：Mapping[str, Callable[[], str|None]]——返回 None=绿，返回码=报码；
词表外码/非串返回值→ReadinessVocabularyError（拒收不降级，R-Q8 末句纪律）；
探针异常上抛（调用方端点 500 域——探针自身崩溃不等于"被探测件故障"）。
mode 单一真源=recall.service.RECALL_MODES。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

from news_flash_dedup.recall.service import RECALL_MODES

_CHANNELS = ("hash", "near", "bm25", "embedding", "entity")

_FIXED_FATAL = frozenset({
    "es_control_read_failed",
    "collector_not_running",
    "delivery_route_unconfigured",
})
_CHAIN_FATAL = frozenset(
    {f"recall_chain_unavailable:{channel}" for channel in _CHANNELS}
    | {"watermark_unreadable"})


class ReadinessVocabularyError(ValueError):
    """探针返回词表外码/非法形态——拒收（不降级、不静默上报未知码）。"""


class ReadinessPort:
    """就绪聚合端口：probes 声明序执行，首个非 None 返回即记码（每探针一码）。"""

    FATAL_CODES: dict[str, frozenset] = {
        "fixed": _FIXED_FATAL,
        "shadow": _FIXED_FATAL | _CHAIN_FATAL,
        "live": _FIXED_FATAL | _CHAIN_FATAL | {"vector_channel_unavailable"},
    }
    DEGRADED_CODES: dict[str, frozenset] = {
        "fixed": frozenset(),
        "shadow": frozenset({"embedding_space_unconfirmed"}),
        "live": frozenset(),
    }

    def __init__(self, probes: Mapping[str, Callable[[], str | None]], *,
                 mode: str) -> None:
        if mode not in RECALL_MODES:
            raise ValueError(f"unknown recall mode: {mode!r}")
        if probes is None or not isinstance(probes, Mapping):
            raise ValueError("probes must be a Mapping[str, Callable]")
        for name, probe in probes.items():
            if not callable(probe):
                raise ValueError(f"probe {name!r} is not callable")
        self._probes = dict(probes)
        self.mode = mode

    def issues(self) -> tuple[bool, list[str]]:
        """返回 (ready, codes)：fatal 任一→(False, ...)；仅 degraded→(True, ...)。"""
        fatal = self.FATAL_CODES[self.mode]
        degraded = self.DEGRADED_CODES[self.mode]
        codes: list[str] = []
        ready = True
        for name, probe in self._probes.items():
            code = probe()
            if code is None:
                continue
            if not isinstance(code, str) or not code:
                raise ReadinessVocabularyError(
                    f"probe {name!r} returned malformed code {code!r}")
            if code in fatal:
                ready = False
                codes.append(code)
            elif code in degraded:
                codes.append(code)
            else:
                raise ReadinessVocabularyError(
                    f"probe {name!r} returned code {code!r} outside the "
                    f"{self.mode} vocabulary")
        return ready, codes


__all__ = ["ReadinessPort", "ReadinessVocabularyError"]
