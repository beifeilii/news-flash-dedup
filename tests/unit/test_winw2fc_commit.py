# -*- coding: utf-8 -*-
"""W2Fγ 红测（commit 族）：条目 28（M-1 control 索引）/33（M-3 重试幂等）/
35（WA4b 低危族 commit 侧）。

红能力（修复前原貌）：
- 条目28 RealESCommitStore.cleanup 的 fullmatch 过滤把 "-control" 后缀名排除
  → 本 run control 索引残留；且 control 名从未过硬正则闸（缺独立校验）；
- 条目33 commit_one 同 record_id 重试：watermark 回退闸先拒（CommitOneError
  "cannot regress: N -> N"）——冲突而非幂等成功；
- 条目35③ commit_one 以 type() 鸭子对象绕 WatermarkSnapshot（源码实述钉）；
- 条目35④ decision_watermark_seq or 塌缩（显式 0 被吞）；
- 条目35⑥ ESCommitResult 死代码（零消费）留场。
"""
from __future__ import annotations

import datetime
import hashlib

import pytest

from news_flash_dedup.commit import CommitContext, CommitOneError, commit_one
from news_flash_dedup.commit.fake_store import FakeCommitStore


# ---------- 条目28（M-1）：cleanup 含本 run control + control 名独立校验 ----------

class _StubIndicesApi:
    def __init__(self, names):
        self._names = set(names)
        self.deleted: list[str] = []

    def exists(self, *, index):
        return index in self._names

    def create(self, *, index):
        self._names.add(index)
        return {}

    def delete(self, *, index):
        self.deleted.append(index)
        self._names.discard(index)
        return {}


class _StubCatClient:
    def __init__(self, names):
        self.indices = _StubIndicesApi(names)
        self._names = names

    class _Cat:
        def __init__(self, names):
            self._names = names

        def indices(self, format, h):
            return [{"index": n} for n in self._names]

    @property
    def cat(self):
        return self._Cat(self._names)


def _p17_store(monkeypatch, existing_names, *, control_index=None):
    from news_flash_dedup.commit import es_store as p17_es
    from news_flash_dedup.commit.es_store import RealESCommitStore, RealESConfig
    monkeypatch.setattr(p17_es, "assert_p17_uat_open", lambda: None)
    config = RealESConfig(run_uuid="runX",
                          business_date=datetime.date(2026, 9, 26))
    store = RealESCommitStore(_StubCatClient(existing_names), config,
                              control_index=control_index)
    return store


def _this_run_names():
    base = "p17-batch-runX-news-dedup"
    return [
        f"{base}-items-v1-2026.09.26",
        f"{base}-audits-v1-2026.09.26",
        f"{base}-audits-v1-2026.09.26-control",
    ]


def test_cleanup_includes_this_run_control_index(monkeypatch):
    """红能力：cleanup 的 P17_INDEX_PREFIX_PATTERN.fullmatch 把 "-control"
    后缀名过滤掉——本 run control 索引残留（每 UAT run 漏删 1 索引）。"""
    names = _this_run_names() + [
        "p17-batch-otherRun-news-dedup-items-v1-2026.09.26",   # 他 run 零触碰
        "news-dedup-items-v1-2026.09.26",                      # 保留索引零触碰
    ]
    store = _p17_store(monkeypatch, names)
    deleted = store.cleanup()
    assert set(deleted) == set(_this_run_names())              # control 不得残留
    assert "p17-batch-otherRun-news-dedup-items-v1-2026.09.26" not in deleted
    assert "news-dedup-items-v1-2026.09.26" not in deleted


def test_control_index_name_independent_validation():
    """红能力：control 名独立校验函数缺席——坏名（无 -control 后缀/剥后缀不过
    硬正则/他 run 前缀均可构造注入）从未过硬闸。"""
    from news_flash_dedup.commit.es_store import (
        P17IndexPrefixInvalid,
        validate_p17_control_index_name,
    )
    validate_p17_control_index_name(
        "p17-batch-runX-news-dedup-audits-v1-2026.09.26-control")
    with pytest.raises(P17IndexPrefixInvalid):
        validate_p17_control_index_name(
            "p17-batch-runX-news-dedup-audits-v1-2026.09.26")          # 缺后缀
    with pytest.raises(P17IndexPrefixInvalid):
        validate_p17_control_index_name("evil-control")                # 剥后缀不过闸
    with pytest.raises(P17IndexPrefixInvalid):
        validate_p17_control_index_name(
            "p17-batch-runX-news-dedup-audits-v1-2026.09.26-control-extra")


def test_explicit_empty_control_index_fails_closed(monkeypatch):
    """红能力（并 35④）：control_index="" 显式空串被 `or` 塌缩静默重算；
    修复后 is-None 判别 + 独立校验 fail-closed。"""
    from news_flash_dedup.commit.es_store import P17IndexPrefixInvalid
    with pytest.raises(P17IndexPrefixInvalid):
        _p17_store(monkeypatch, [], control_index="")


# ---------- 条目33（M-3）：commit_one 同 record_id 重试幂等 ----------

def _text_with_facts(text="甲公司完成回购。", subject="甲公司",
                     predicate="回购", record_id="a" * 64):
    return [
        {"fact_id": "f1",
         "evidence": [{"record_id": record_id, "field": "x",
                       "quote": subject, "start": 0, "end": len(subject)}],
         "fact_type": {"status": "present", "raw_value": predicate,
                       "evidence": [{"record_id": record_id, "field": "x",
                                     "quote": subject, "start": 0,
                                     "end": len(subject)}]},
         "subject": {"status": "present", "raw_value": subject,
                     "evidence": [{"record_id": record_id, "field": "x",
                                   "quote": subject, "start": 0,
                                   "end": len(subject)}]},
         "event_state": {"predicate": {"status": "present", "raw_value": predicate,
                                       "evidence": [{"record_id": record_id, "field": "x",
                                                     "quote": subject, "start": 0,
                                                     "end": len(subject)}]},
                         "polarity": {"status": "present", "raw_value": predicate,
                                      "evidence": [{"record_id": record_id, "field": "x",
                                                    "quote": subject, "start": 0,
                                                    "end": len(subject)}]},
                         "modality": {"status": "missing", "raw_value": None,
                                      "evidence": []},
                         "attribution": {"status": "missing", "raw_value": None,
                                         "evidence": []}},
         "time": {"expression": {"status": "missing", "raw_value": None,
                                  "evidence": []},
                  "stage": {"status": "missing", "raw_value": None,
                            "evidence": []},
                  "anchor": {"status": "missing", "raw_value": None,
                             "evidence": []}},
         "key_object": {"status": "missing", "raw_value": None, "evidence": []},
         "numerics": []}
    ]


def _ctx_for(text, record_id, item_id, arrival_seq):
    return {
        "record_id": record_id, "item_id": item_id, "text": text,
        "raw_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "scope_id": "default", "business_date": "2026-09-26",
        "arrival_seq": arrival_seq, "pipeline_version": "dedup_v1",
        "facts": _text_with_facts(text=text, record_id=record_id),
    }


def _commit_ctx(*, arrival_seq=2, record_id="c" * 64, item_id="item-C",
                text="甲公司完成回购。"):
    current = _ctx_for(text, record_id, item_id, arrival_seq)
    history = _ctx_for("甲公司完成回购。（同日先到条）", "a" * 64, "item-A",
                       max(1, arrival_seq - 1))
    return CommitContext(
        scope_id="default", business_date="2026-09-26",
        arrival_seq=arrival_seq, current=current, candidates=(history,),
        visible_seq=arrival_seq, prepared_seq=arrival_seq,
        coverage_complete=True,
    )


def test_commit_one_identical_retry_is_idempotent_success():
    """红能力：同 ctx 重试——旧 watermark 回退闸先拒（CommitOneError
    "cannot regress: 2 -> 2"，探针 winw2fc-probe-m3-commit-retry.json 实证）；
    修复后冲突时对拍既有记录一致 → 幂等成功返回，不重复写入。"""
    store = FakeCommitStore()
    ctx = _commit_ctx()
    first = commit_one(ctx, store, audit_complete=True)
    assert first.state == "committed"
    cas_before = len(store.cas_calls)
    wm_before = len(store.watermark_calls)

    retry = commit_one(ctx, store, audit_complete=True)
    assert retry.state == "committed"                        # 幂等成功而非冲突
    assert retry.main_record_id == first.main_record_id
    assert store.main_records[first.main_record_id].text == "甲公司完成回购。"
    assert len(store.cas_calls) == cas_before                # 零重复写入
    assert len(store.watermark_calls) == wm_before           # 水位不重复推进
    assert store.get_watermark("default", "2026-09-26") == 2


def test_commit_one_divergent_retry_raises_conflict():
    """红能力：同 record_id 不同正文重试（身份撞车）——对拍不一致 → 冲突
    （不得幂等成功掩盖）。"""
    store = FakeCommitStore()
    ctx = _commit_ctx()
    assert commit_one(ctx, store, audit_complete=True).state == "committed"

    tampered = _commit_ctx()
    tampered.current["text"] = "乙公司发布业绩预告。"        # 同 record_id 异正文
    tampered.current["raw_hash"] = hashlib.sha256(
        tampered.current["text"].encode("utf-8")).hexdigest()
    tampered.current["facts"] = _text_with_facts(
        text="乙公司发布业绩预告。", subject="乙公司", predicate="发布",
        record_id="c" * 64)
    with pytest.raises(CommitOneError, match="(?i)conflict|不一致|冲突"):
        commit_one(tampered, store, audit_complete=True)
    assert store.main_records["c" * 64].text == "甲公司完成回购。"   # 已存不动


def test_commit_one_true_regression_still_rejected():
    """绿守卫：真回退（target < prev）照拒——幂等通路不放纵回退。"""
    store = FakeCommitStore()
    ctx = _commit_ctx()
    assert commit_one(ctx, store, audit_complete=True).state == "committed"
    older = _commit_ctx(record_id="d" * 64, item_id="item-D",
                        text="甲公司完成回购。（另条）")
    with pytest.raises(CommitOneError, match="cannot regress"):
        commit_one(older, store, audit_complete=True,
                   decision_watermark_seq=1)                # prev=2 > 1


# ---------- 条目35③：鸭子对象绕 WatermarkSnapshot（源码实述钉） ----------

def test_commit_one_uses_real_watermark_snapshot_not_duck():
    """红能力：commit_one 以 type("W",(),{...})() 鸭子对象喂
    is_watermark_snapshot 校验（绕真类型）——实型构造缺席即红。"""
    from pathlib import Path
    src = Path(
        "src/news_flash_dedup/commit/coordinator.py").read_text(encoding="utf-8")
    assert 'type("W", ()' not in src
    assert "WatermarkSnapshot(" in src


# ---------- 条目35④：decision_watermark_seq 显式 0 不被 or 塌缩 ----------

def test_explicit_zero_watermark_seq_not_collapsed_to_arrival_seq():
    """红能力：decision_watermark_seq=0 显式传入被 `or` 塌缩为 arrival_seq
    （0 是合法显式值：初始水位）——塌缩缺席即红（0 目标在 prev=0 时应走
    幂等/冲突判定而非静默改写为 arrival_seq 提交）。"""
    store = FakeCommitStore()
    ctx = _commit_ctx()
    with pytest.raises(CommitOneError):
        commit_one(ctx, store, audit_complete=True, decision_watermark_seq=0)
    assert store.main_records == {}                          # 未静默塌缩提交


# ---------- 条目35⑥：ESCommitResult 死代码清除 ----------

def test_es_commit_result_dead_code_removed():
    """红能力：ESCommitResult 零消费死代码留场（src/tests 全仓零构造）。"""
    from news_flash_dedup.commit import es_store as p17_es
    assert not hasattr(p17_es, "ESCommitResult")
    assert "ESCommitResult" not in p17_es.__all__
