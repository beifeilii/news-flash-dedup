# -*- coding: utf-8 -*-
"""W-A 族⑤红测（建议落 tests/unit/test_wina_family5_alignment.py）。

红态锚（未修复码面必红）：
- ⑤(a) decide/llm_residual.py:473-474 无 1MB 上限、坏 UTF-8/JSON 裸逃逸进瞬时重试环；
- ⑤(b) decide/service.py:191(193)/212/340 raw_hash 直取缺键裸 KeyError；
- ⑤(c) decide/llm_residual.py:554-555 LlmResidualError 短路绕 failures 台账；
- ⑤(d) admission.py:417 lookup 读路径经 _head() 创建控制文档+owner 闸。

绿守卫（前后均绿）：恰 1MB 合法响应放行；raw_hash 错配仍绑定门 fail-closed；
重试耗尽 failures 无双计；同 owner lookup 协议不变。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

import pytest

from news_flash_dedup.admission import (
    AdmissionConflict,
    AdmissionCoordinator,
    AdmissionRequest,
)
from news_flash_dedup.decide import llm_residual
from news_flash_dedup.decide.llm_residual import (
    LlmResidualError,
    ResidualJudgeConfig,
    ResidualJudgeLedger,
    SyncResidualJudge,
)
from news_flash_dedup.decide.service import DecideInputError, decide_for_task


# ==================== ⑤(a) _chat_once 双加固移植 ====================

class _FakeResp:
    """urllib 响应双倍体（read(n) 语义对齐 HTTPResponse；ctx 管理器）。"""

    def __init__(self, data: bytes):
        self._data = data
        self.read_sizes = []

    def read(self, n=-1):
        self.read_sizes.append(n)
        if n is None or n < 0:
            return self._data
        return self._data[:n]

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


_ONE_MB = 1024 * 1024


def _patch_urlopen(monkeypatch, data: bytes) -> _FakeResp:
    resp = _FakeResp(data)
    monkeypatch.setattr(llm_residual.urllib.request, "urlopen",
                        lambda req, timeout=None: resp)
    return resp


def _chat_kwargs() -> dict:
    return dict(model="qwen-turbo", base_url="http://localhost/v1",
                api_key="k", system="s", user="u", timeout_s=1.0)


def test_r5a1_response_over_1mb_hard_limit(monkeypatch):
    """超 1MB 响应体 → LlmResidualError（不静默截断、不无界读）。"""
    payload = json.dumps({"choices": [{"message": {"content": "x"}}]},
                         ensure_ascii=False).encode("utf-8")
    oversized = payload + b" " * (_ONE_MB - len(payload) + 1)
    _patch_urlopen(monkeypatch, oversized)
    with pytest.raises(LlmResidualError, match="1MB"):
        llm_residual._chat_once(**_chat_kwargs())


def test_r5a2_bad_utf8_wrapped_deterministic(monkeypatch):
    """坏 UTF-8 → LlmResidualError 确定性失败（非裸 UnicodeDecodeError）。"""
    _patch_urlopen(monkeypatch, b"\xff\xfe\x00\x01not-utf8")
    with pytest.raises(LlmResidualError, match="UTF-8|确定性"):
        llm_residual._chat_once(**_chat_kwargs())


def test_r5a3_bad_json_wrapped_deterministic(monkeypatch):
    """坏 JSON → LlmResidualError 确定性失败（非裸 JSONDecodeError）。"""
    _patch_urlopen(monkeypatch, b"this is not json")
    with pytest.raises(LlmResidualError, match="JSON|确定性"):
        llm_residual._chat_once(**_chat_kwargs())


def test_r5a4_exactly_1mb_valid_json_green_guard(monkeypatch):
    """绿守卫：恰 1MB 合法响应放行（溢出判据是 >1MB）。"""
    stub = json.dumps({"choices": [{"message": {"content": ""}}]},
                      ensure_ascii=False).encode("utf-8")
    pad = _ONE_MB - len(stub)       # ""→" N空格 "净增量恰为 pad 字符
    payload = json.dumps({"choices": [{"message": {"content": " " * pad}}]},
                         ensure_ascii=False).encode("utf-8")
    assert len(payload) == _ONE_MB
    _patch_urlopen(monkeypatch, payload)
    content, latency = llm_residual._chat_once(**_chat_kwargs())
    assert isinstance(content, str) and latency >= 0.0


def test_r5a5_bad_json_no_deterministic_replay(monkeypatch):
    """坏 JSON 属确定性失败：judge_pair 每顺序恰 1 次 HTTP 调用（不重试重放）。

    红态（现役）：JSONDecodeError 落瞬时重试环 → 每顺序 4 次调用（api_calls=8）。"""
    monkeypatch.setattr(llm_residual.time, "sleep", lambda _s: None)
    calls = []

    def _fake_urlopen(req, timeout=None):
        calls.append(1)
        return _FakeResp(b"definitely not json")

    monkeypatch.setattr(llm_residual.urllib.request, "urlopen", _fake_urlopen)
    ledger = ResidualJudgeLedger()
    judge = SyncResidualJudge(config=ResidualJudgeConfig(cache_dir=None),
                              api_key="k", ledger=ledger)
    outcome = judge.judge_pair("pair-r5a5", "文本甲", "文本乙")
    assert outcome.cell == "failure"
    assert outcome.hc.status == "failure" and outcome.ch.status == "failure"
    assert len(calls) == 2, f"确定性失败不得重试：实调 {len(calls)} 次"
    snap = ledger.snapshot()
    assert snap["api_calls"] == 2
    assert snap["retries"] == 0
    assert snap["failures"] == 2     # 与⑤(c) 联动：每顺序恰 +1


# ==================== ⑤(b) raw_hash 缺键容错拉齐 ====================

_HISTORY_TEXT = "历史快讯正文：甲公司上调目标价。"
_CURRENT_TEXT = "当前快讯正文：甲公司下调目标价。"


def _fact(text, *, fact_id, subject, predicate, sentence, record_id):
    """单 Fact 夹具（非空 facts——pair_alignment.py:377 空指针即拒，
    缺键路径必须活着走到 raw_hash 行/绑定门）。形对齐
    test_pair_alignment_normalize.py:93-114。"""
    base = f"facts.{fact_id}"

    def ev(quote):
        start = text.index(quote)
        return {"record_id": record_id, "field": base, "quote": quote,
                "start": start, "end": start + len(quote)}

    def present(quote):
        return {"status": "present", "raw_value": quote, "evidence": [ev(quote)]}

    missing = {"status": "missing", "raw_value": None, "evidence": []}
    return {
        "fact_id": fact_id, "evidence": [ev(sentence)],
        "fact_type": present(predicate), "subject": present(subject),
        "event_state": {"predicate": present(predicate),
                        "polarity": present(predicate),
                        "modality": missing, "attribution": missing},
        "time": {"expression": missing, "stage": missing, "anchor": missing},
        "key_object": missing, "numerics": [],
    }


def _history_full() -> dict:
    return {"record_id": "a" * 64, "item_id": "item-h", "text": _HISTORY_TEXT,
            "raw_hash": hashlib.sha256(_HISTORY_TEXT.encode("utf-8")).hexdigest(),
            "arrival_seq": 1,
            "facts": [_fact(_HISTORY_TEXT, fact_id="f1", subject="甲公司",
                            predicate="上调", sentence="甲公司上调目标价。",
                            record_id="a" * 64)]}


def _current_full() -> dict:
    return {"record_id": "b" * 64, "item_id": "item-c", "text": _CURRENT_TEXT,
            "raw_hash": hashlib.sha256(_CURRENT_TEXT.encode("utf-8")).hexdigest(),
            "arrival_seq": 2,
            "facts": [_fact(_CURRENT_TEXT, fact_id="g1", subject="甲公司",
                            predicate="下调", sentence="甲公司下调目标价。",
                            record_id="b" * 64)]}


def _decide(history, current):
    return decide_for_task(
        history, [], current=current,
        dictionary_version="dict_v1", alignment_version="alignment_v1",
        pipeline_version="dedup_v1", coverage_complete=True,
        visible_seq=2, prepared_seq=2)


def test_r5b1_current_missing_raw_hash_recomputes():
    """current 缺 raw_hash 键 → 按 text 重算（候选侧 M-13 同口径），不裸 KeyError。

    红态锚：decide/service.py:212 直取 → KeyError('raw_hash')（冒烟实证）。"""
    current = _current_full()
    del current["raw_hash"]
    outcome = _decide(_history_full(), current)
    assert outcome.raw_hash == hashlib.sha256(
        _CURRENT_TEXT.encode("utf-8")).hexdigest()


def test_r5b2_history_missing_raw_hash_recomputes():
    """history 缺 raw_hash 键 → 重算（re_freeze 装配侧同口径），判定链完整跑完。

    红态锚：decide/service.py:193 直取 → KeyError('raw_hash')。"""
    history = _history_full()
    del history["raw_hash"]
    outcome = _decide(history, _current_full())
    assert outcome.raw_hash == hashlib.sha256(
        _CURRENT_TEXT.encode("utf-8")).hexdigest()


def test_r5b3_missing_identity_key_decide_input_error():
    """identity 键（text）缺失 → DecideInputError 声明通道（非裸 KeyError）。

    红态锚：decide/service.py:172 直取 → KeyError('text')（冒烟实证）。"""
    current = _current_full()
    del current["text"]
    with pytest.raises(DecideInputError, match="missing required keys"):
        _decide(_history_full(), current)


def test_r5b4_wrong_upstream_raw_hash_still_binding_gate():
    """绿守卫（前后均绿）：上游 raw_hash 与 text 不符（键在但错配）→
    pair_compare 对级绑定门 PairBindingError fail-closed（pair_compare.py:222
    既有钉面）——M-13 口径不变：缺键/空串才重算，错配绝不在本层重算洗值。"""
    from news_flash_dedup.compare.pair_compare import PairBindingError
    current = _current_full()
    current["raw_hash"] = "0" * 64      # 与 text 不符的错配键
    with pytest.raises(PairBindingError, match="raw_hash"):
        _decide(_history_full(), current)


# ==================== ⑤(c) LlmResidualError 纳入 failures 台账 ====================

def _raise_deterministic(**_kw):
    raise LlmResidualError("deterministic bad response shape")


def test_r5c1_deterministic_error_bumps_failures():
    """确定性失败短路（:554-555）必须 failures+1/顺序——状态面/计数面拉齐。"""
    ledger = ResidualJudgeLedger()
    judge = SyncResidualJudge(config=ResidualJudgeConfig(cache_dir=None),
                              api_key="k", call_fn=_raise_deterministic,
                              ledger=ledger)
    outcome = judge.judge_pair("pair-r5c1", "文本甲", "文本乙")
    assert outcome.cell == "failure"
    assert outcome.hc.status == "failure" and outcome.ch.status == "failure"
    snap = ledger.snapshot()
    assert snap["api_calls"] == 2, "确定性失败每顺序恰一次调用"
    assert snap["retries"] == 0
    assert snap["failures"] == 2, "状态面 failure 必须计数面 failures 同步"


def _raise_transient(**_kw):
    raise ConnectionError("simulated transient network fault")


def test_r5c2_transient_exhaustion_no_double_count(monkeypatch):
    """绿守卫：瞬时错误重试耗尽路径（:595）failures 每顺序恰 +1，不被⑤(c) 双计。"""
    monkeypatch.setattr(llm_residual.time, "sleep", lambda _s: None)
    ledger = ResidualJudgeLedger()
    judge = SyncResidualJudge(config=ResidualJudgeConfig(cache_dir=None),
                              api_key="k", call_fn=_raise_transient,
                              ledger=ledger)
    outcome = judge.judge_pair("pair-r5c2", "文本甲", "文本乙")
    assert outcome.cell == "failure"
    snap = ledger.snapshot()
    assert snap["api_calls"] == 8       # (1+max_retries=4) × 2 顺序
    assert snap["retries"] == 6
    assert snap["failures"] == 2, "重试耗尽恰 +1/顺序，短路修复不得波及本路径"


# ==================== ⑤(d) lookup 读路径写副作用拆除 ====================

class _Conflict(Exception):
    pass


class _MemoryStore:
    """tests/unit/test_admission.py MemoryStore 同形（get/create/replace/is_conflict）。"""

    def __init__(self):
        from copy import deepcopy
        from threading import Lock
        self._deepcopy = deepcopy
        self.docs = {}
        self.lock = Lock()

    def get(self, index, key):
        with self.lock:
            value = self._deepcopy(self.docs.get((index, key)))
        return value

    def create(self, index, key, body):
        with self.lock:
            if (index, key) in self.docs:
                raise _Conflict()
            self.docs[index, key] = {"source": self._deepcopy(body),
                                     "seq_no": 0, "primary_term": 1}

    def replace(self, index, key, body, seq_no, primary_term):
        with self.lock:
            old = self.docs[index, key]
            if (old["seq_no"], old["primary_term"]) != (seq_no, primary_term):
                raise _Conflict()
            self.docs[index, key] = {"source": self._deepcopy(body),
                                     "seq_no": seq_no + 1,
                                     "primary_term": primary_term}

    def is_conflict(self, error):
        return isinstance(error, _Conflict)


_CLOCK = lambda: datetime(2026, 9, 23, 15, 59, tzinfo=timezone.utc)


def _request(request_id="100-1", item_id="100-1", text="甲公司发布新产品。"):
    return AdmissionRequest(
        scope_id="default", request_id=request_id, item_id=item_id, text=text,
        received_at=datetime(2026, 9, 23, 15, 58, tzinfo=timezone.utc),
        schema_version="1", pipeline_version="v1", embedding_space_id="v3",
        delivery_route_ref="audit-route-v1", trace_id="trace-first")


def _coordinator(store, owner_id="writer-1"):
    return AdmissionCoordinator(store, owner_id=owner_id,
                                owner_isolated=lambda: True, clock=_CLOCK)


def test_r5d1_lookup_never_creates_control_doc():
    """lookup 纯读：head 缺场返回 None 且**不得创建**控制文档。"""
    store = _MemoryStore()
    service = _coordinator(store)
    assert service.lookup("default", "100-1") is None
    assert store.get("news-dedup-control-v1", "admission_head") is None, (
        "读路径产生写副作用（head 被 lookup 创建）")


def test_r5d2_lookup_across_owner_boundary_is_readonly():
    """异 owner 协调器 lookup 不撞 owner 闸：读到 pending 凭据（10 L262 三读协议）。"""
    store = _MemoryStore()
    writer = _coordinator(store, owner_id="writer-1")
    accepted = writer.accept(_request(), materialize=False)
    assert accepted.arrival_seq == 1
    other = _coordinator(store, owner_id="writer-2")     # 非属主实例
    receipt = other.lookup("default", "100-1")
    assert receipt is not None and receipt.arrival_seq == 1


def test_r5d3_same_owner_protocol_green_guard():
    """绿守卫：同 owner accept→lookup→recover 协议逐字节不变。"""
    store = _MemoryStore()
    service = _coordinator(store)
    accepted = service.accept(_request(), materialize=False)
    assert service.lookup("default", "100-1").arrival_seq == 1
    restarted = _coordinator(store)
    restarted.recover()
    assert restarted.lookup("default", "100-1") == accepted
    head = store.get("news-dedup-control-v1", "admission_head")
    assert head["source"]["pending"] is None
