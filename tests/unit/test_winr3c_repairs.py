# -*- coding: utf-8 -*-
"""W-R3c 红测（外部审核低危散项批，先红后绿；R3-M8 取证为只读判定不在本件）：

a. backfill.py:95 宽 except 收窄：编程缺陷（TypeError/KeyError/AttributeError
   等）传播不掩盖；领域/运行期失败词表（RuntimeError/ValueError/OSError——
   覆盖 P19VectorError/EmbeddingApiError/VectorWriteUnknown/
   VectorStateTransitionError/VectorIdentityConflict 全族）仍诚实 failed +
   台账（INV-1 不跳过不掩盖）。
b. backfill.py:119-121 chunk_coverage 单位混算（chunks + failed 记录数）修正：
   record 口径 records_coverage（失败记录的 chunk 期望数不可得，chunk 分母
   不可诚实构造），混算键 chunk_coverage 退役、chunks_written 原始计数保留。
d. embedding_client 响应体无界读 → 对齐 facts/llm.py:105-112 1MB 硬上限
   （读上限+1 判溢出，超长 VectorWriteUnknown 不静默截断不无界 read）。
e. 429 不读 Retry-After → 尊重头：urllib_transport 解析 Retry-After 秒数
   随 EmbeddingRateLimited 携带（畸形/缺/负值 → None 回退内建公式），
   _call_batch 有则照退避；无头时既有 min(60,2·2^a)+jitter 公式不动。
f. audit.py:146 evidence field 键可取空 → 守卫：实胜证据在场时 field 从证据
   自携 field 实填（证据自带权威），证据 field 退化空时回退 field_path，
   无证据诚实空壳形态不动。
g. service.py:70 None fact_id 口径与 extra_event.py:38-41 None 前置拦截不一
   → 对齐：None/缺键拦截不入册、非 None 一律 str()（含空串保留口径）。
h. embedding_query.py docstring 漂移锚勘误：vector_store.py 三处锚
   （192-202 错误面 / 180-183 EMPTY_BODY / 311 prepared_seq 直传）勘正；
   fusion.py:123-126 锚实测仍准（钉守）。
"""

from __future__ import annotations

import re
import urllib.error
from pathlib import Path

import pytest

from news_flash_dedup.compare.pair_alignment import AlignedPair, FactPointer
from news_flash_dedup.compare.pair_compare import PairResult, VerifiedConflict
from news_flash_dedup.compare.value_time import EvidenceRef
from news_flash_dedup.decide import audit as audit_module
from news_flash_dedup.decide.extra_event import _fact_ids
from news_flash_dedup.decide.service import _wrap_facts_as_report
from news_flash_dedup.recall.vector_space import EmbeddingSpace
from news_flash_dedup.recall.vector_store import (
    VectorIdentityConflict,
    VectorWriteUnknown,
)
from news_flash_dedup.vector import backfill as bf
from news_flash_dedup.vector import embedding_client as ec
from news_flash_dedup.vector.coordinator import P19VectorError
from news_flash_dedup.vector.embedding_client import EmbeddingApiError
from news_flash_dedup.vector.milvus_store import VectorStateTransitionError


# ================================ a/b：backfill ================================

def _space():
    return EmbeddingSpace("mock_embedding", "fixture_v1", "codepoint_v1", "none",
                          "COSINE", 4, "plain_v1", "plain_v1",
                          "tokens512_overlap64_bpe_v1", "p09_v1")


class _Pipe:
    """可编程 pipeline：errors 命中 record_id 即抛；否则产 outcomes_per_record chunk。"""

    def __init__(self, space, errors=None, outcomes_per_record=1):
        self._space = space
        self._errors = dict(errors or {})
        self._n = outcomes_per_record

    def ingest_record(self, *, record_id, text, scope_id, arrival_seq):
        from news_flash_dedup.vector.pipeline import IngestReport

        error = self._errors.get(record_id)
        if error is not None:
            raise error
        return IngestReport(
            record_id=record_id,
            chunk_ids=tuple(range(self._n)),
            outcomes=tuple((f"{record_id}_{i}", "created") for i in range(self._n)),
            state="ready", truncated_chunk_ids=(), tokens_used=9, cache_hits=0,
            chunking_version="tokens512_overlap64_bpe_v1")


class _Rows:
    def __init__(self):
        self.rows = {}

    def get_vector_row(self, vector_id):
        return self.rows.get(vector_id)


def _records(*rids):
    return [{"record_id": rid, "text": f"正文{rid}", "scope_id": "gold",
             "arrival_seq": index + 1} for index, rid in enumerate(rids)]


def _seed_rows(store, rid, n):
    for i in range(n):
        store.rows[f"{rid}_{i}"] = {"embedding": [1.0, 0.5, 0.25, 0.125]}


# ---------- a：宽 except 收窄 ----------

def test_r3c_a_programming_defect_propagates_not_masked():
    """a 主红测：pipeline 编程缺陷（TypeError）不再被宽 except 吞入台账。"""
    pipe = _Pipe(_space(), errors={"r1": TypeError("programming defect")})
    with pytest.raises(TypeError):
        bf.b1_backfill(pipe, _Rows(), _records("r1"),
                       write_main_record=lambda record: None)


def test_r3c_a_malformed_record_key_error_propagates():
    """a 同族红测：记录缺必需键（契约违规）传播，不伪装成嵌入失败入册。"""
    bad = [{"record_id": "r1", "scope_id": "gold", "arrival_seq": 1}]  # 缺 text
    with pytest.raises(KeyError):
        bf.b1_backfill(_Pipe(_space()), _Rows(), bad,
                       write_main_record=lambda record: None)


def test_r3c_a_domain_failures_stay_honest_failed():
    """a 绿守卫（前后均绿）：领域/运行期失败词表仍诚实 failed + 台账。"""
    for error in (EmbeddingApiError("api terminal"),
                  VectorWriteUnknown("embedding response is unknown"),
                  P19VectorError("chunks must be non-empty"),
                  VectorStateTransitionError("source not pending"),
                  VectorIdentityConflict("same vector_id has a different artifact"),
                  OSError("io fault"),
                  RuntimeError("operational runtime failure"),
                  ValueError("domain validation failure")):
        pipe = _Pipe(_space(), errors={"r1": error})
        out = bf.b1_backfill(pipe, _Rows(), _records("r1"),
                             write_main_record=lambda record: None)
        assert out["records_failed"] == 1, error
        assert out["failed"][0]["record_id"] == "r1"


# ---------- b：coverage 单位混算 ----------

def test_r3c_b_coverage_is_record_unit_not_mixed():
    """b 主红测：record 口径覆盖率 2/3；混算键 chunk_coverage 退役。"""
    pipe = _Pipe(_space(), errors={"r2": EmbeddingApiError("injected outage")},
                 outcomes_per_record=2)
    store = _Rows()
    _seed_rows(store, "r1", 2)
    _seed_rows(store, "r3", 2)
    out = bf.b1_backfill(pipe, store, _records("r1", "r2", "r3"),
                         write_main_record=lambda record: None)
    assert out["records_total"] == 3 and out["records_ready"] == 2
    assert out["records_coverage"] == pytest.approx(2 / 3)
    assert "chunk_coverage" not in out        # chunks + failed 记录数混算键退役
    assert out["chunks_written"] == 4          # chunk 原始计数保留（名实相符）


def test_r3c_b_all_success_full_coverage():
    """b 同族钉：全成功 → records_coverage=1.0。"""
    pipe = _Pipe(_space(), outcomes_per_record=2)
    store = _Rows()
    _seed_rows(store, "r1", 2)
    _seed_rows(store, "r2", 2)
    out = bf.b1_backfill(pipe, store, _records("r1", "r2"),
                         write_main_record=lambda record: None)
    assert out["records_coverage"] == 1.0


# ================================ d/e：embedding_client ================================

DIM = 4


class _FakeHttpResponse:
    def __init__(self, body: bytes):
        self._body = body
        self.read_calls = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, amt=None):
        self.read_calls.append(amt)
        return self._body if amt is None else self._body[:amt]


def _urlopen_stub(monkeypatch, response=None, error=None):
    def _stub(request, timeout=None):
        if error is not None:
            raise error
        return response

    monkeypatch.setattr(ec.urllib.request, "urlopen", _stub)


def _transport_kwargs():
    return dict(base_url="https://dashscope.example/v1", model="text-embedding-v3",
                api_key="sk-testkey1234567890abcd", texts=["正文"], timeout_s=30)


# ---------- d：响应体 1MB 硬上限 ----------

def test_r3c_d_response_body_over_1mb_rejected_bounded_read(monkeypatch):
    """d 主红测：超 1MB 响应 → VectorWriteUnknown；read 带上限+1 不无界读。"""
    body = b"x" * (1024 * 1024 + 100)   # 超上限且非合法 JSON：cap 必须先于解析拦截
    response = _FakeHttpResponse(body)
    _urlopen_stub(monkeypatch, response=response)
    with pytest.raises(VectorWriteUnknown):
        ec.urllib_transport(**_transport_kwargs())
    assert response.read_calls == [1024 * 1024 + 1]


def test_r3c_d_exactly_1mb_body_passes(monkeypatch):
    """d 边界钉（前后均绿）：恰 1MB 合法响应放行（上限含本数）。"""
    payload = b'{"data": null}'
    body = payload + b" " * (1024 * 1024 - len(payload))
    assert len(body) == 1024 * 1024
    response = _FakeHttpResponse(body)
    _urlopen_stub(monkeypatch, response=response)
    assert ec.urllib_transport(**_transport_kwargs()) == {"data": None}


def test_r3c_d_max_response_bytes_constant():
    """d 常数钉：MAX_RESPONSE_BYTES=1MiB（对齐 facts/llm.py:109 口径）。"""
    assert ec.MAX_RESPONSE_BYTES == 1024 * 1024


# ---------- e：429 Retry-After ----------

def _http_error_429(headers):
    return urllib.error.HTTPError("https://dashscope.example/v1/embeddings",
                                  429, "Too Many Requests", headers, None)


def test_r3c_e_retry_after_header_carried(monkeypatch):
    """e 主红测：429 响应头 Retry-After 秒数随 EmbeddingRateLimited 携带。"""
    _urlopen_stub(monkeypatch, error=_http_error_429({"Retry-After": "7.5"}))
    with pytest.raises(ec.EmbeddingRateLimited) as captured:
        ec.urllib_transport(**_transport_kwargs())
    assert captured.value.retry_after == 7.5


def test_r3c_e_retry_after_absent_or_malformed_falls_back(monkeypatch):
    """e 同族红测：缺头/畸形/负值 → retry_after=None（回退内建公式）。"""
    for headers in ({"Retry-After": "soon"}, {"Retry-After": "-3"}, {}, None):
        _urlopen_stub(monkeypatch, error=_http_error_429(headers))
        with pytest.raises(ec.EmbeddingRateLimited) as captured:
            ec.urllib_transport(**_transport_kwargs())
        assert captured.value.retry_after is None, headers


class _ScriptedTransport:
    """签名同真实调用的可编程 transport（payload dict 或 Exception 实例逐个弹出）。"""

    def __init__(self):
        self.script = []

    def push(self, item):
        self.script.append(item)
        return self

    def __call__(self, *, base_url, model, api_key, texts, timeout_s):
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _ok_payload():
    return {"data": [{"embedding": [1.0, 0.5, 0.25, 0.125], "index": 0}],
            "usage": {"total_tokens": 11}}


def _client(tmp_path, transport, *, sleep):
    config = ec.EmbeddingClientConfig(
        base_url="https://dashscope.example/v1", model="text-embedding-v3",
        dimension=DIM, cache_root=tmp_path / "cache")
    return ec.EmbeddingClient(
        config, transport_fn=transport,
        env={"DEDUP_EMBEDDING_API_KEY": "sk-testkey1234567890abcd"},
        sleep=sleep, rng=lambda: 0.5)


def test_r3c_e_client_sleeps_per_retry_after_header(tmp_path):
    """e 装配红测：头在场 → 照头退避（非内建公式 min(60,2·2^0)+0.5=2.5）。"""
    sleeps = []
    transport = _ScriptedTransport()
    transport.push(ec.EmbeddingRateLimited("429", retry_after=7.5)).push(_ok_payload())
    client = _client(tmp_path, transport, sleep=sleeps.append)
    vectors = client.embed_documents(["正文"])
    assert len(vectors) == 1
    assert sleeps == [7.5]


def test_r3c_e_absent_header_builtin_backoff_preserved(tmp_path):
    """e 绿守卫（前后均绿）：无头 → 既有 min(60,2·2^a)+jitter 公式逐秒不动。"""
    sleeps = []
    transport = _ScriptedTransport()
    transport.push(ec.EmbeddingRateLimited("429")).push(_ok_payload())
    client = _client(tmp_path, transport, sleep=sleeps.append)
    client.embed_documents(["正文"])
    assert sleeps == [2.0 * (2 ** 0) + 0.5]


# ================================ f：audit evidence field 守卫 ================================

H_ID = "h" * 64
C_ID = "c" * 64
PV = "dedup_v1"

H_EV = EvidenceRef(record_id=H_ID, field="facts.f1.numerics.n1.value",
                   quote="100元", start=10, end=14)
C_EV = EvidenceRef(record_id=C_ID, field="facts.f1.numerics.n1.value",
                   quote="101元", start=10, end=14)
H_EV_ALIGNED = EvidenceRef(record_id=H_ID, field="facts.f1.subject",
                           quote="甲公司", start=0, end=3)
C_EV_ALIGNED = EvidenceRef(record_id=C_ID, field="facts.f1.subject",
                           quote="甲公司", start=0, end=3)


def _pair(*, outcome="conflict", code="VERIFIED_CONFLICT",
          aligned_facts=(), verified_conflicts=(), used_evidence=()):
    return PairResult(
        pair_id=f"{H_ID}|{C_ID}",
        history_record_id=H_ID, current_record_id=C_ID,
        history_item_id="item-h", current_item_id="item-c",
        history_arrival_seq=1, current_arrival_seq=2,
        history_raw_hash="hh", current_raw_hash="cc",
        pipeline_version=PV, outcome=outcome, code=code, detail="probe",
        aligned_facts=aligned_facts, verified_conflicts=verified_conflicts,
        unresolved_fields=(), used_evidence=used_evidence, budget_at=0)


def _conflict_pair():
    conflict = VerifiedConflict(field_path="facts.f1.numerics.n1.value",
                                basis="NUMERIC_SAME_DECIMAL",
                                history_evidence=H_EV, current_evidence=C_EV,
                                detail="100元 vs 101元")
    return _pair(verified_conflicts=(conflict,), used_evidence=(H_EV, C_EV))


def _aligned_pair():
    aligned = AlignedPair(
        history=FactPointer(record_id=H_ID, fact_id="f1",
                            slot_path="facts.f1", text="t", evidence=H_EV_ALIGNED),
        current=FactPointer(record_id=C_ID, fact_id="f1",
                            slot_path="facts.f1", text="t", evidence=C_EV_ALIGNED),
        basis="EXACT_TEXT_MATCH")
    return _pair(outcome="equivalent", code="FACT_EQUIVALENT",
                 aligned_facts=(aligned,),
                 used_evidence=(H_EV_ALIGNED, C_EV_ALIGNED))


def test_r3c_f_conflict_evidence_field_filled_from_winning():
    """f 主红测：冲突对双侧 evidence field 从实胜证据自携 field 实填。"""
    record = audit_module.build_audit_record(_conflict_pair())
    assert record.history_evidence["field"] == "facts.f1.numerics.n1.value"
    assert record.current_evidence["field"] == "facts.f1.numerics.n1.value"


def test_r3c_f_aligned_evidence_field_filled():
    """f 同族红测：equivalent 对（对齐事实证据）field 同样实填。"""
    record = audit_module.build_audit_record(_aligned_pair())
    assert record.history_evidence["field"] == "facts.f1.subject"
    assert record.current_evidence["field"] == "facts.f1.subject"


def test_r3c_f_hollow_shell_field_stays_honest():
    """f 绿守卫（前后均绿）：无证据对诚实空壳形态不动（field/quote 空）。"""
    record = audit_module.build_audit_record(
        _pair(outcome="unresolved", code="FACT_INCOMPLETE", used_evidence=()))
    assert record.history_evidence["field"] == ""
    assert record.history_evidence["quote"] == ""
    assert record.current_evidence["field"] == ""


def test_r3c_f_explicit_field_path_fallback_when_winning_field_empty():
    """f 绿守卫：证据 field 退化空串 → 回退显式 field_path（回退语义钉）。"""
    ev = EvidenceRef(record_id=H_ID, field="", quote="q", start=0, end=1)
    record = audit_module.build_audit_record(
        _pair(outcome="unresolved", code="FACT_INCOMPLETE", used_evidence=(ev,)),
        field_path="facts.fallback")
    assert record.history_evidence["field"] == "facts.fallback"


# ================================ g：None fact_id 口径对齐 ================================
#
# E 批 H-04 修法 A 后改断（EW 实现窗裁定书 B2 有条件准·条件三执行）：
# ①逐件纪律→校验器承载点 1:1 映射（写入各件 docstring）；②改断前后断言
# 强度对照表入 log\temp\ew-impl-notes.md ⑨-B 段（逐件等强/更强实证）；
# ③无承载点 2 件（str() 归一 / 任意输入类对拍）曾停线再报——主窗口
# 二裁采 (ii) 分歧如实钉（附条件：不可达论证须可执行承载），已改断落地
# （见两件 docstring；ew-impl-notes ⑫ 段终版在案）。

def test_r3c_g_none_fact_id_intercepted():
    """g 主红测（H-04 改断）：fact_id=None 拦截不入 valid_fact_ids。

    条件①纪律→承载点映射：「None fact_id 拦截」→ facts/schema.py:111
    fact_id 型式闸（{"type":"string","pattern":"^f[1-9][0-9]*$"}——None
    非 string 即 Schema 拒）+ facts/core.py:309-311（Schema 阶段 issue
    即早退、valid_fact_ids=() 全件不注册）+:356-357（唯零 issue fact
    入册）。改断后钉面：非法 id 全件拒（更强）+EXTRACTION_FAILED 响亮
    归因路径点名肇事 fact。"""
    report = _wrap_facts_as_report("r1", "正文", [{"fact_id": None}, {"fact_id": "f1"}])
    assert report.valid_fact_ids == ()
    assert report.validated_complete is False
    assert any(issue.code == "EXTRACTION_FAILED"
               and issue.path == "$.facts[0].fact_id"
               for issue in report.issues)


def test_r3c_g_missing_fact_id_intercepted():
    """g 同族红测（H-04 改断）：缺 fact_id 键同样拦截（修复前洗成空串入册）。

    条件①纪律→承载点映射：「缺键拦截」→ facts/schema.py:109 required
    集（fact_id 首键必填，缺键即 Schema 拒）+ facts/core.py:309-311
    （同上早退不注册）+:356-357（唯零 issue fact 入册）。钉面同主红测：
    全件拒+响亮归因（$.facts[0] 缺键路径）。"""
    report = _wrap_facts_as_report("r1", "正文", [{"predicate": "p"}, {"fact_id": "f1"}])
    assert report.valid_fact_ids == ()
    assert report.validated_complete is False
    assert any(issue.code == "EXTRACTION_FAILED"
               and issue.path == "$.facts[0]"
               for issue in report.issues)


def _decide_signing_unreachable_for(facts):
    """不可达守卫（可执行，EW 二裁 (ii) 附条件承载件——非注释）：经
    DecideService 真调用链的行为级断言——同文双侧、双侧同注分歧输入类
    facts → 签发门（p15_integration.py:327-328 _artifact_qualified =
    validated_complete ∧ extraction_status=="complete"）不可达，双径
    皆证：①链式 fail-closed——域型三选（PairAlignmentError/
    PairBindingError/DecideInputError）响亮死于签发门上游（域型收窄
    是钉面：意外异常照常冒泡转红）；②链式放行——decision≠"重复"
    且 duplicate_ids 为空。生产径不变式=wrap 层对一切供给
    （identity/rule/llm/夹具）一律先经 validate_fact_artifact——
    分歧只可能活在 validated_complete=False 的报告上；供给路径后续任何
    改动使校验未过工件可达签发时，本守卫即红（响亮）。"""
    import hashlib as _hashlib

    from news_flash_dedup.compare.pair_alignment import PairAlignmentError
    from news_flash_dedup.compare.pair_compare import PairBindingError
    from news_flash_dedup.decide.service import DecideInputError, decide_once
    text = "甲公司完成回购。"

    def _ctx(record_id, item_id, seq):
        return {"record_id": record_id, "item_id": item_id, "text": text,
                "raw_hash": _hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "scope_id": "default", "business_date": "2026-09-26",
                "arrival_seq": seq, "facts": facts,
                "extraction_status": "complete"}

    try:
        outcome = decide_once(_ctx("a" * 64, "item-H", 1),
                              _ctx("c" * 64, "item-C", 2),
                              coverage_complete=True, visible_seq=2,
                              prepared_seq=2)
    except (PairAlignmentError, PairBindingError, DecideInputError):
        # 受 sanction 的 fail-closed 响亮死域型——链式拒于签发门上游
        # （pair_alignment._fact_pointers 证据闸先触）：签发不可达亦证。
        # 域型收窄是钉面一部分：非此三型的意外异常照常冒泡转红。
        return
    assert outcome.decision != "重复"
    assert outcome.duplicate_ids == ()


def test_r3c_g_non_string_fact_id_stringified():
    """g 同族（EW 二裁 (ii) 改断=分歧如实钉+不可达钉，附条件全承载）。

    原纪律（修法 A 前）：非 None 非串 fact_id 一律 str() 归一入册
    （对齐 extra_event._fact_ids :41 口径——同一事实输入两侧同一输出）。
    结构性分歧（修法 A 校验器口径下）：wrap 注册面=validator 准入集——
    fact_id 型式闸（facts/schema.py:111 {"type":"string","pattern":
    "^f[1-9][0-9]*$"}）对 7 只拒不归一，Schema 阶段早退
    （facts/core.py:309-311）全件 valid_fact_ids=()；匹配侧
    extra_event 仍 str() 归一得 ("7",)——非法输入类两侧结构性分歧
    客观存在，如实钉不洗。
    不可达论证（可执行承载，非注释）：wrap 层对一切供给先校验——
    分歧输入类 ⇒ validated_complete=False ⇒ _artifact_qualified 签发门
    不可达；DecideService 行为级守卫=_decide_signing_unreachable_for
    （同文真链决策断言——供给路径改动使分歧可达签发即红）。
    登记引用：原纪律→本钉的转换=④遗漏类形态→①书面接受语义，经
    EW 二裁落地——log\\09-注记-B2-01-贪心匹配.md ①节"④→①转换性质
    成文"先例同构（双锚+挂账联动）；ew-impl-notes ⑨-B/⑫ 段在案。"""
    facts = [{"fact_id": 7}]
    report = _wrap_facts_as_report("r1", "正文", facts)
    # 分歧如实钉①：校验口径——非串 id 全件拒，归册 ()，响亮归因
    assert report.valid_fact_ids == ()
    assert report.validated_complete is False
    assert any(issue.code == "EXTRACTION_FAILED" for issue in report.issues)
    # 分歧如实钉②：匹配侧 extra_event 仍 str() 归一——分歧存在
    assert _fact_ids({"facts": facts}) == ("7",)
    # 不可达钉（可执行）：分歧输入类 ⇒ 签发门行为级不可达
    _decide_signing_unreachable_for(facts)


def test_r3c_g_valid_ids_unchanged():
    """g 绿守卫（H-04 改断）：全合法串 id 逐值不动。

    条件①纪律→承载点映射：「合法串 id 逐值入册、顺序不动」→
    facts/core.py:319-357（逐 fact 零 issue 即 append fact_id，遍历序=
    输入序）+:323-324（f1..fn 顺序闸同源钉）。改断夹具升级：最小 dict
    （不过 Schema）→校验可过双 fact（_complete_fact 族，f2 经深拷改号
    +证据 field 路径同步改 facts.f2——field 与 fact 序位错配即
    EVIDENCE_INVALID，EVIDENCE 闸 :328 承载）；断言面（("f1","f2")
    逐值）零改动，且新增 validated_complete/issues 双钉（更强：准入径
    顺带钉死全件洁净）。"""
    from test_e_h04_facts_projection import _complete_fact
    import copy as _copy

    record_id, text = "a" * 64, "甲公司完成回购。"
    fact1 = _complete_fact(record_id, text)
    fact2 = _copy.deepcopy(fact1)
    fact2["fact_id"] = "f2"

    def _retag(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "field" and isinstance(value, str):
                    node[key] = value.replace("facts.f1", "facts.f2", 1)
                else:
                    _retag(value)
        elif isinstance(node, list):
            for item in node:
                _retag(item)
    _retag(fact2)
    report = _wrap_facts_as_report(record_id, text, [fact1, fact2])
    assert report.valid_fact_ids == ("f1", "f2")
    assert report.validated_complete is True
    assert report.issues == ()


def test_r3c_g_aligns_with_extra_event_none_discipline():
    """g 对拍件（EW 二裁 (ii) 改断=分歧如实钉+不可达钉，附条件全承载）。

    原纪律（修法 A 前）：wrap 注册面 ≡ extra_event._fact_ids 匹配面，
    同一事实输入（含 None/缺键/非串/非 dict 任意输入类）同一输出——
    修复前 wrap 侧 None 原样入册/缺键洗空串的口径不齐由此件对拍钉死。
    结构性分歧（修法 A 校验器口径下）：wrap 注册面=validator 准入集——
    混合非法输入类全件 Schema 拒（facts/schema.py:109 required+:111
    型式闸；facts/core.py:309-311 早退）⇒ valid_fact_ids=()；匹配侧
    纪律未动（None/缺键前置拦截 :39-40、非 dict 丢弃 :36-37、非 None
    str() 归一 :41）⇒ ("f1","7")——任意非法输入类两侧结构性分歧客观
    存在，如实钉不洗。
    不可达论证（可执行承载，非注释）：同 g_non_string 件——wrap 层对
    一切供给先校验，分歧输入类 ⇒ validated_complete=False ⇒ 签发门
    不可达；DecideService 行为级守卫=_decide_signing_unreachable_for。
    合法输入类两侧一致性由 g_valid_ids_unchanged 在校验口径下承接
    （("f1","f2") 逐值不动）——对拍纪律的有效域成文收窄，非丢弃。
    登记引用：④→①转换成文，log\\09-注记-B2-01-贪心匹配.md ①节先例
    同构；ew-impl-notes ⑨-B/⑫ 段在案。"""
    facts = [{"fact_id": None}, {"predicate": "p"}, {"fact_id": "f1"},
             {"fact_id": 7}, "not-a-dict"]
    report = _wrap_facts_as_report("r1", "正文", facts)
    # 分歧如实钉①：校验口径——混合非法输入类全件拒，归册 ()
    assert report.valid_fact_ids == ()
    assert report.validated_complete is False
    # 分歧如实钉②：匹配侧口径未动（None/缺键拦截、str() 归一、非 dict
    # 丢弃）——("f1","7")，与注册面 () 的结构性分歧如实入档
    assert _fact_ids({"facts": facts}) == ("f1", "7")
    # 不可达钉（可执行）：分歧输入类 ⇒ 签发门行为级不可达
    _decide_signing_unreachable_for(facts)


# ================================ h：embedding_query 漂移锚勘误 ================================

_SRC = Path(__file__).resolve().parents[2] / "src" / "news_flash_dedup"


def _query_doc() -> str:
    return (_SRC / "recall" / "embedding_query.py").read_text(encoding="utf-8")


def _store_lines() -> list[str]:
    return (_SRC / "recall" / "vector_store.py").read_text(
        encoding="utf-8").splitlines()


def _anchor_of(doc: str, marker: str) -> tuple[int, int]:
    lines = doc.splitlines()
    index = next(i for i, line in enumerate(lines) if marker in line)
    window = "\n".join(lines[max(0, index - 1):index + 1])   # 弹条可折行：锚在上行
    match = re.search(r"vector_store\.py:(\d+)-(\d+)", window)
    assert match, f"{marker} 弹条未锚 vector_store.py 区间"
    return int(match.group(1)), int(match.group(2))


def test_r3c_h_empty_body_anchor_covers_real_branch():
    """h 红测①：「空文本不耗 API」弹条锚须覆盖真实 EMPTY_BODY 分支行。"""
    start, end = _anchor_of(_query_doc(), "空文本不耗 API")
    store = _store_lines()
    empty_body_line = next(i for i, line in enumerate(store, 1)
                           if 'error_code="EMPTY_BODY"' in line)
    assert start <= empty_body_line <= end, (
        f"锚 vector_store.py:{start}-{end} 未覆盖 EMPTY_BODY 分支行 {empty_body_line}")


def test_r3c_h_error_surface_anchor_covers_input_rejection_gates():
    """h 红测②：「错误面」弹条锚须覆盖输入拒收早退面（EMPTY_BODY..EMBEDDING_INVALID）。"""
    start, end = _anchor_of(_query_doc(), "错误面）")
    store = _store_lines()
    for code in ("EMPTY_BODY", "EMBEDDING_INVALID"):
        gate_line = next(i for i, line in enumerate(store, 1)
                         if f'error_code="{code}"' in line)
        assert start <= gate_line <= end, (
            f"锚 vector_store.py:{start}-{end} 未覆盖 {code} 行 {gate_line}")


def test_r3c_h_prepared_seq_anchor_points_at_passthrough():
    """h 红测③：「prepared_seq 直传」锚行须为真实直传行。"""
    bullet = next(line for line in _query_doc().splitlines()
                  if "prepared_seq 直传" in line)
    cited = int(re.search(r"vector_store\.py:(\d+)", bullet).group(1))
    store = _store_lines()
    assert "prepared_seq=request.prepared_seq" in store[cited - 1], (
        f"锚 vector_store.py:{cited} 非 prepared_seq 直传行：{store[cited - 1]!r}")


def test_r3c_h_fusion_anchor_accurate_pin():
    """h 绿守卫：fusion.py:123-126 VECTOR_FRONTIER_UNPROVEN 锚实测仍准。"""
    fusion = (_SRC / "recall" / "fusion.py").read_text(
        encoding="utf-8").splitlines()
    assert any("VECTOR_FRONTIER_UNPROVEN" in line for line in fusion[122:126])
