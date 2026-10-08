# -*- coding: utf-8 -*-
"""窗口V 审计新发现修复：红测先行（改前红 / 改后绿）+ 守卫钉。

覆盖（外部六路增量复核新增发现处置）：
- N-01【置顶】api/cleanup.py execute 端 fail-open → fail-closed：损坏 JSON
  打 execute 端 → 400 且 cleanup_port.execute 零调用（间谍钉）；dry_run 端
  parity 钉（两端同口径）；非 JSON content-type 空体路径——窗口Z2（N2-01，
  主窗口追裁）起 dry_run 默认翻 True 走演习路径（原"行为保持"守卫同步更正）。
- audit_complete 默认 ×3（M-01 同标准）：persist/coordinator.persist_main_record、
  persist/es_store.persist_main_record_es、decide/audit.build_audit_batch
  去默认改强制关键字（inspect.signature 钉，缺失即 TypeError 的编译级闸）。
  第 4 处 commit/coordinator.commit_one：窗口V 时点上曾登记驳回（禁碰
  test_p23_uat.py:655 回放 harness 依赖默认，窗口T 占用，"无法显式化"系该时点
  旧述）——现已经窗口T 解锁拆除：调用点全部显式化后同标准去默认，与本文件
  :139-144 parametrize 实含 commit_one 一致（W2Fε 锚点勘正：原写 :136-141；
  09-29 W3e F2 回锚：实物 :139/:143/:144——勘正行自插一行自致漂移病在案）。
- N-05：pair_alignment build_aligned 尾部 if/else 两分支赋值逐字相同（死分支）
  → 坍缩；三形态 code 选取守卫钉（改前改后皆绿，行为零漂移）+ v2 腿对锚。
- milvus_client.py:32 from None → from error：__cause__ 保链钉（P 批 15 处纪律）。
"""

from __future__ import annotations

import inspect

import pytest


# ---------------------------------------------------------------------------
# N-01：cleanup execute 端 fail-closed
# ---------------------------------------------------------------------------

class _SpyCleanupPort:
    """cleanup_port 间谍：记录 dry_run/execute 调用，供零调用断言。"""

    def __init__(self):
        self.dry_run_calls: list = []
        self.execute_calls: list = []

    def dry_run(self, today, operator):
        self.dry_run_calls.append((today.isoformat(), operator))
        return {"today": today.isoformat(), "operator": operator,
                "would_delete": [], "absent": [], "dry_run": True}

    def execute(self, today, operator, dry_run):
        self.execute_calls.append((today.isoformat(), operator, dry_run))
        return {"today": today.isoformat(), "operator": operator,
                "dry_run": dry_run, "deleted": [], "skipped_absent": []}


def _cleanup_client(port):
    from fastapi.testclient import TestClient
    from news_flash_dedup.api.cleanup import create_cleanup_app
    return TestClient(create_cleanup_app(
        cleanup_port=port, admin_authenticated=lambda _request: True))


_BROKEN_JSON = b'{"operator": "admin", "dry_run": tr'   # 截断损坏 JSON


def test_n01_execute_malformed_json_is_400_and_execute_never_called():
    """N-01 红测：损坏 JSON 打 execute 端 → 400 + cleanup_port.execute 零调用。

    改前（fail-open）：except Exception 吞错 → body={} → dry_run=False 默认
    → 真删路径放行（200 且 execute 被调）——本断言改前必红。
    """
    port = _SpyCleanupPort()
    client = _cleanup_client(port)
    response = client.post("/v1/api/admin/cleanup/execute",
                            content=_BROKEN_JSON,
                            headers={"content-type": "application/json"})
    assert response.status_code == 400
    assert response.json()["error"] == "invalid JSON body"
    assert port.execute_calls == [], (
        "损坏 JSON 绝不允许放行到 cleanup_port.execute（fail-open 即真删）"
    )


def test_n01_dry_run_malformed_json_is_400_parity_pin():
    """dry_run 端 parity 钉：M-12/L 窗口P 已 fail-closed，前后皆绿。"""
    port = _SpyCleanupPort()
    client = _cleanup_client(port)
    response = client.post("/v1/api/admin/cleanup/dry_run",
                            content=_BROKEN_JSON,
                            headers={"content-type": "application/json"})
    assert response.status_code == 400
    assert response.json()["error"] == "invalid JSON body"
    assert port.dry_run_calls == []


def test_n01_execute_and_dry_run_malformed_json_same_outcome_parity():
    """两端同口径 parity：损坏 JSON → 同 400 同 error 文案（改前 execute 端红）。"""
    port = _SpyCleanupPort()
    client = _cleanup_client(port)
    execute_response = client.post("/v1/api/admin/cleanup/execute",
                                    content=_BROKEN_JSON,
                                    headers={"content-type": "application/json"})
    dry_run_response = client.post("/v1/api/admin/cleanup/dry_run",
                                    content=_BROKEN_JSON,
                                    headers={"content-type": "application/json"})
    assert execute_response.status_code == dry_run_response.status_code == 400
    assert execute_response.json() == dry_run_response.json()


def test_n01_execute_valid_json_still_reaches_port():
    """守卫：合法 JSON 体照常放行（dry_run=False 真删语义不变）。"""
    port = _SpyCleanupPort()
    client = _cleanup_client(port)
    response = client.post("/v1/api/admin/cleanup/execute",
                            json={"today": "2026-09-25", "operator": "ops",
                                  "dry_run": False})
    assert response.status_code == 200
    assert port.execute_calls == [("2026-09-25", "ops", False)]


def test_n01_execute_non_json_content_type_takes_drill_path():
    """窗口Z2（N2-01 cleanup 空白指令，主窗口追裁）：非 JSON content-type
    （空体）不解析 body → dry_run 默认翻 True——空 body 一律演习路径
    （本测试原钉 dry_run is False 系"空白指令落真删"旧形态，同步更正）。
    """
    port = _SpyCleanupPort()
    client = _cleanup_client(port)
    response = client.post("/v1/api/admin/cleanup/execute",
                            content=b"", headers={"content-type": "text/plain"})
    assert response.status_code == 200
    assert len(port.execute_calls) == 1
    _today, operator, dry_run = port.execute_calls[0]
    assert operator == "admin" and dry_run is True


# ---------------------------------------------------------------------------
# audit_complete 默认 ×3（M-01 同标准：去默认改强制关键字 + 编译级闸）
# ---------------------------------------------------------------------------

def _audit_complete_param(func):
    return inspect.signature(func).parameters["audit_complete"]


@pytest.mark.parametrize("qualname", [
    "news_flash_dedup.persist.coordinator.persist_main_record",
    "news_flash_dedup.persist.es_store.persist_main_record_es",
    "news_flash_dedup.decide.audit.build_audit_batch",
    "news_flash_dedup.commit.coordinator.commit_one",
])
def test_audit_complete_no_silent_default_keyword_only(qualname):
    """四处 audit_complete 必须无默认（强制关键字显式表态）。

    改前默认 True → 调用方不显式表态即获"审计完备"语义（fail-open 闸门），
    本断言改前必红；改后 param.default is empty，缺省调用即 TypeError（编译级闸）。
    第 4 处 commit_one（coordinator.py）由窗口T 经窗口V 登记解锁拆除
    （2026-09-28，调用点全部显式化后同标准去默认）。
    """
    module_name, func_name = qualname.rsplit(".", 1)
    import importlib
    func = getattr(importlib.import_module(module_name), func_name)
    param = _audit_complete_param(func)
    assert param.kind is inspect.Parameter.KEYWORD_ONLY
    assert param.default is inspect.Parameter.empty, (
        f"{qualname} 的 audit_complete 不得带默认值（M-01 同标准，窗口V）"
    )


def test_audit_complete_missing_kwarg_is_type_error_persist_main_record():
    """编译级闸实证：persist_main_record 缺 audit_complete 调用即 TypeError（改后绿）。"""
    from news_flash_dedup.persist import coordinator as persist_coordinator
    if _audit_complete_param(persist_coordinator.persist_main_record).default \
            is not inspect.Parameter.empty:
        pytest.skip("默认值未拆除前本闸不存在（红测由 signature 钉承担）")
    with pytest.raises(TypeError):
        persist_coordinator.persist_main_record(
            None, None, arrival_seq=1, raw_hash="h", audit_ids=(),
        )


# ---------------------------------------------------------------------------
# N-05：pair_alignment 死分支坍缩——code 选取三形态守卫钉（前后皆绿）
# ---------------------------------------------------------------------------

_RECORD_H = "a" * 64
_RECORD_C = "b" * 64


def _ev(text, quote, field, record_id):
    start = text.index(quote)
    return {"record_id": record_id, "field": field, "quote": quote,
            "start": start, "end": start + len(quote)}


def _slot(text, quote, field, record_id):
    return {"status": "present", "raw_value": quote,
            "evidence": [_ev(text, quote, field, record_id)]}


def _missing_slot():
    return {"status": "missing", "raw_value": None, "evidence": []}


def _fact(text, fact_id, subject, predicate, record_id):
    base = f"facts.{fact_id}"
    return {
        "fact_id": fact_id,
        "evidence": [_ev(text, text, base, record_id)],
        "fact_type": _slot(text, predicate, base + ".fact_type", record_id),
        "subject": _slot(text, subject, base + ".subject", record_id),
        "event_state": {
            "predicate": _slot(text, predicate, base + ".event_state.predicate", record_id),
            "polarity": _slot(text, predicate, base + ".event_state.polarity", record_id),
            "modality": _missing_slot(),
            "attribution": _missing_slot(),
        },
        "time": {"expression": _missing_slot(), "stage": _missing_slot(),
                 "anchor": _missing_slot()},
        "key_object": _missing_slot(),
        "numerics": [],
    }


def _report(text, facts, record_id):
    from news_flash_dedup.facts import FactValidationReport
    artifact = {"schema_version": "1.0", "record_id": record_id,
                "offset_unit": "unicode_code_point",
                "extraction_status": "complete", "facts": facts,
                "unparsed_spans": [], "uncertainties": []}
    return FactValidationReport(
        record_id=record_id, validated_complete=True,
        extraction_status="complete",
        valid_fact_ids=tuple(f["fact_id"] for f in facts),
        numeric_inventory=(), issues=(), artifact=artifact,
    )


def _build(history_facts, current_facts, *, candidate_basis=None):
    from news_flash_dedup.compare import pair_alignment
    history_text = "甲公司完成回购。乙公司宣布增持。"
    current_text = "甲公司完成回购。丙公司签署协议。"
    return pair_alignment.build_aligned(
        _report(history_text, history_facts, _RECORD_H),
        _report(current_text, current_facts, _RECORD_C),
        history_text, current_text,
        dictionary_version="dict_v1",
        candidate_basis=candidate_basis,
    )


def test_n05_code_rule_uncovered_when_nothing_aligned():
    """形态一：零对齐 → code=RULE_UNCOVERED（不受 candidate_basis 影响）。"""
    history_facts = [_fact("甲公司完成回购。乙公司宣布增持。", "f1", "甲公司", "回购", _RECORD_H)]
    current_facts = [_fact("甲公司完成回购。丙公司签署协议。", "g1", "丙公司", "签署", _RECORD_C)]
    outcome = _build(history_facts, current_facts,
                     candidate_basis="EVENT_STATE_SAME")
    assert outcome.aligned_facts == ()
    assert outcome.code == "RULE_UNCOVERED"


def test_n05_code_default_fact_equivalent_when_aligned_and_all_resolved():
    """形态二：有对齐 + 零未决 → code=FACT_EQUIVALENT（candidate_basis=None 落默认）。"""
    history_facts = [_fact("甲公司完成回购。乙公司宣布增持。", "f1", "甲公司", "回购", _RECORD_H)]
    current_facts = [_fact("甲公司完成回购。丙公司签署协议。", "g1", "甲公司", "回购", _RECORD_C)]
    outcome = _build(history_facts, current_facts)
    assert len(outcome.aligned_facts) == 1
    assert outcome.unresolved_fields == ()
    assert outcome.code == "FACT_EQUIVALENT"


def test_n05_code_same_value_with_and_without_unresolved():
    """形态三（死分支钉）：有对齐时，有/无未决两种形状 code 取值必须一致——

    原 if/else 两分支赋值逐字相同（死分支，先逐字比对确认后坍缩），
    本钉锁死坍缩前后行为零漂移（前后皆绿）。
    """
    history_two = [
        _fact("甲公司完成回购。乙公司宣布增持。", "f1", "甲公司", "回购", _RECORD_H),
        _fact("甲公司完成回购。乙公司宣布增持。", "f2", "乙公司", "增持", _RECORD_H),
    ]
    current_one = [_fact("甲公司完成回购。丙公司签署协议。", "g1", "甲公司", "回购", _RECORD_C)]
    with_unresolved = _build(history_two, current_one,
                             candidate_basis="EVENT_STATE_SAME")
    assert with_unresolved.unresolved_fields == ("history:f2",)
    history_one = [history_two[0]]
    without_unresolved = _build(history_one, current_one,
                                candidate_basis="EVENT_STATE_SAME")
    assert without_unresolved.unresolved_fields == ()
    assert with_unresolved.code == without_unresolved.code == "EVENT_STATE_SAME"
    # candidate_basis=None 落默认 FACT_EQUIVALENT，同样两形状一致
    assert _build(history_two, current_one).code == \
        _build(history_one, current_one).code == "FACT_EQUIVALENT"


# ---------------------------------------------------------------------------
# milvus_client.py:32 from None → from error（P 批 15 处保链纪律）
# ---------------------------------------------------------------------------

_MILVUS_ENV = {
    "DEPLOY_ENV": "test",
    "TEST_MILVUS_HOST": "c-75641abe71c37c6f-internal.milvus.aliyuncs.com",
    "TEST_MILVUS_PORT": "19530",
    "TEST_MILVUS_USER": "u",
    "TEST_MILVUS_PASS": "p",
}


def test_milvus_non_numeric_port_chain_preserved():
    """红测：ValueError 包装路径 __cause__ 必须保链（改前 from None → __cause__=None 红）。"""
    from news_flash_dedup.milvus_client import (
        ProductionMilvusAccessDenied, assert_test_milvus_environment,
    )
    env = {**_MILVUS_ENV, "TEST_MILVUS_PORT": "abc"}
    with pytest.raises(ProductionMilvusAccessDenied) as excinfo:
        assert_test_milvus_environment(env)
    assert isinstance(excinfo.value.__cause__, ValueError), (
        "from None 断链逆 P 批纪律——__cause__ 必须为原 ValueError"
    )


def test_milvus_none_port_chain_preserved():
    """红测：TypeError 包装路径 __cause__ 同款保链（int(None) 原 TypeError）。"""
    from news_flash_dedup.milvus_client import (
        ProductionMilvusAccessDenied, assert_test_milvus_environment,
    )
    env = {**_MILVUS_ENV, "TEST_MILVUS_PORT": None}
    with pytest.raises(ProductionMilvusAccessDenied) as excinfo:
        assert_test_milvus_environment(env)
    assert isinstance(excinfo.value.__cause__, TypeError)
