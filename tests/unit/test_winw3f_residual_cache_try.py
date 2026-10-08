# -*- coding: utf-8 -*-
"""W3F 三轮红测（W3b-F6）：llm_residual._call_cached 缓存写与 LLM 调用
同 try 块 → 缓存写失败被同一 except 吞→LLM 误重试。

缺陷面（decide/llm_residual.py:422-456 现役）：缓存写
（mkdir/tmp.write_text/os.replace :432-452）与 LLM 调用共处同一 try，
写失败（共享冲突/磁盘满等）落入 :456 `except Exception` 瞬时错误环
→ 误重试 LLM（一写失败=max_retries 次无谓调用，fail-closed 成本放大），
重试耗尽后 LlmResidualError → failure 误单列（LLM 实已成功）。

修法（try 分离，对齐读侧 :408-418 同款）：LLM 调用 try 语义逐字节不变；
缓存写独立 try 自吞自登记（_log.warning 留痕）后照常返回 LLM 结果
——读侧"损坏按 miss"纪律使写失条目无害。台账/快照形状不加字段
（metrics JSON 三腿锚字节面不动）。
"""

from __future__ import annotations

import inspect
import logging

import pytest

from news_flash_dedup.decide import llm_residual as lr

from tests.unit.test_decide_llm_residual import (
    _MockLLM,
    _SIGNED_RESP,
    _judge,
    C,
    H,
)

_SNAPSHOT_KEYS = {"api_calls", "cache_hits", "retries", "failures",
                  "llm_latency_s"}


def _blocking_cache_file(tmp_path):
    """cache_dir 指向已存在文件 → path.parent.mkdir 必 FileExistsError
    （缓存写确定性失败驱动面，跨平台）。"""
    blocker = tmp_path / "cache_dir_is_file"
    blocker.write_text("x", encoding="utf-8")
    return str(blocker)


# ---------- 红测（修复前写失败误重试 LLM / 结果变 failure） ----------

def test_f6_cache_write_failure_no_llm_retry_result_unchanged(
        tmp_path, monkeypatch, caplog):
    """红能力：缓存写确定性失败——修复前落入瞬时错误环误重试
    （mock.calls=2×(max_retries+1)=8、failure 误单列）；修复后写失败
    自吞自登记、LLM 零重试（2 调用）、结果与无缓存基线逐字节同一。"""
    monkeypatch.setattr(lr.time, "sleep", lambda _s: None)
    mock = _MockLLM(_SIGNED_RESP)
    ledger = lr.ResidualJudgeLedger()
    with caplog.at_level(logging.WARNING):
        out = _judge(mock, cache_dir=_blocking_cache_file(tmp_path),
                     ledger=ledger).judge_pair("p1", H, C)
    assert mock.calls == 2                        # hc+ch 各一次，零误重试
    assert out.signed is True                     # 结果不翻转（修前 failure）
    baseline = _judge(_MockLLM(_SIGNED_RESP),
                      cache_dir=None).judge_pair("p1", H, C)
    assert out.to_audit_dict() == baseline.to_audit_dict()   # 结果逐字节同一
    snap = ledger.snapshot()
    assert snap["api_calls"] == 2 and snap["retries"] == 0
    assert snap["failures"] == 0
    assert any("cache write failed" in rec.message
               for rec in caplog.records)         # 自登记留痕（hc+ch 各一）


def test_f6_cache_write_try_separated_in_source():
    """机制钉（源码面）：_call_cached 缓存写段独立 try 自吞（W3b-F6 标记
    在场 + 写失败警告在瞬时错误环之后）；LLM 调用/重试注释语义原位不动。"""
    src = inspect.getsource(lr.SyncResidualJudge._call_cached)
    assert "W3b-F6" in src, \
        "缓存写独立 try 分离未落地（同 try 误重试面仍在）"
    assert src.index("cache write failed") > src.index("网络/HTTP/超时等瞬时错误")
    assert "网络/HTTP/超时等瞬时错误" in src     # 重试环语义注释原位（守卫）
    code_lines = [ln for ln in src.splitlines()
                  if not ln.strip().startswith("#")]
    # W2 ⑩①-6（修复波 2 设计稿明示"预算件分支写块复制可接受"）：预算
    # 分支自携同形写块一份——legacy 面写语义单点不动，总数 1→2（分支
    # 级单点语义不变；行为级"写失败不驱动 LLM 误重试"由上方行为钉守）。
    assert sum(ln.count("os.replace(") for ln in code_lines) == 2


# ---------- 前后均绿守卫 ----------

def test_f6_cache_write_success_path_unchanged(tmp_path):
    """守卫（前后同绿）：写成功路径逐字节不动——首跑 2 调用落 2 条目，
    二跑零 API 全命中、审计负载同一。"""
    mock1 = _MockLLM(_SIGNED_RESP)
    out1 = _judge(mock1, cache_dir=str(tmp_path)).judge_pair("p1", H, C)
    assert mock1.calls == 2
    assert len(list(tmp_path.glob("*.json"))) == 2   # hc+ch 条目落盘
    mock2 = _MockLLM(_SIGNED_RESP)
    ledger2 = lr.ResidualJudgeLedger()
    out2 = _judge(mock2, cache_dir=str(tmp_path),
                  ledger=ledger2).judge_pair("p1", H, C)
    assert mock2.calls == 0
    assert ledger2.snapshot()["cache_hits"] == 2
    assert out1.to_audit_dict() == out2.to_audit_dict()


def test_f6_llm_failure_retry_semantics_unchanged(monkeypatch):
    """守卫（前后同绿）：LLM 真失败路径语义不动——重试环/failure 单列/
    台账计数与既有钉值同一（api_calls=2×4、retries=2×3、failures=2）。"""
    monkeypatch.setattr(lr.time, "sleep", lambda _s: None)
    mock = _MockLLM({k: ConnectionError("mock 网络中断") for k in _SIGNED_RESP})
    ledger = lr.ResidualJudgeLedger()
    out = _judge(mock, ledger=ledger).judge_pair("p1", H, C)
    assert out.signed is False and out.cell == "failure"
    assert out.hc.status == "failure" and out.ch.status == "failure"
    snap = ledger.snapshot()
    assert snap["failures"] == 2
    assert snap["api_calls"] == 2 * 4
    assert snap["retries"] == 2 * 3


def test_f6_llm_residual_error_passthrough_no_retry_unchanged():
    """守卫（前后同绿）：call_fn 抛 LlmResidualError → except LlmResidualError
    直通不重试（failure 单列、每顺序恰 1 调用）。"""
    mock = _MockLLM({k: lr.LlmResidualError("响应形态异常")
                     for k in _SIGNED_RESP})
    out = _judge(mock).judge_pair("p1", H, C)
    assert out.hc.status == "failure" and out.ch.status == "failure"
    assert mock.calls == 2


def test_f6_ledger_snapshot_shape_unchanged(tmp_path):
    """守卫（前后同绿）：台账快照键集五键钉死——修不加字段（metrics
    JSON residual_judge.ledger 段三腿锚字节面不动的前提）。"""
    _judge(_MockLLM(_SIGNED_RESP), cache_dir=str(tmp_path)).judge_pair(
        "p1", H, C)
    snap = lr.ResidualJudgeLedger().snapshot()
    assert set(snap) == _SNAPSHOT_KEYS
    judge = _judge(_MockLLM(_SIGNED_RESP), cache_dir=str(tmp_path))
    assert set(judge.ledger.snapshot()) == _SNAPSHOT_KEYS
