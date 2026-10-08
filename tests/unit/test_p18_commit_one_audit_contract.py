"""R-W-01 契约钉（窗口Y）：commit_one 审计落库双态契约 × RealESP18Store 接线。

背景（窗口W 五站复测 R-W-01）：P18 真 ES 接线用例
`test_uat_commit_one_real_es_wiring` 红——
`AttributeError: 'RealESP18Store' object has no attribute 'write_audit'`
（commit/coordinator.py L197 回退分支调用点）。

调用点契约（commit/coordinator.py L191-199，R9 外部审核 F4 修复后现役）：
    if audit_batch.records:
        if hasattr(store, "write_audit_documents"):      # 批量主分支
            audit_ids = tuple(store.write_audit_documents(
                [record.to_doc() for record in audit_batch.records]))
        else:                                            # 逐条回退分支
            audit_ids = tuple(
                store.write_audit(record.comparison_id, record.to_doc())
                for record in audit_batch.records)

三态仓储方法名表（复测核实）：
- commit/es_store.py  RealESCommitStore: write_audit_documents（L126，批量 create）
- commit/fake_store.py FakeCommitStore:  write_audit（L61，逐条）
- persist/es_store.py RealESP18Store:    仅 write_audit_batch 旧名（L127）
  → hasattr 探针落空、回退分支 write_audit 不存在 → AttributeError。

修复（最小语义保真）：RealESP18Store 增 write_audit_documents 薄适配，
委托现役 write_audit_batch 同一体（bulk create / 失败即停 AuditPersistError /
文档 enrichment 零改动）；write_audit_batch 旧名保留（persist_main_record_es
与 P18 UAT 直调零影响）。

本稿纪律：真 ES 不可用于单元层——以方法存在性 + 签名 + 行为间谍钉契约；
不修先红（缺方法即 AttributeError/断言红），修后全绿。禁连集群。
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest

from news_flash_dedup.commit import commit_one
from news_flash_dedup.commit.fake_store import FakeCommitStore
from news_flash_dedup.decide.audit import AuditRecord
from news_flash_dedup.persist import es_store as p18_es


# ---------- 测试数据 ----------

def _audit_record(suffix: str = "spy") -> AuditRecord:
    rid_history = "h" * 63 + "1"
    rid_current = "c" * 63 + "2"
    return AuditRecord(
        comparison_id=f"{rid_history}:{rid_current}",
        history_record_id=rid_history,
        current_record_id=rid_current,
        history_raw_hash="h-" + suffix,
        current_raw_hash="c-" + suffix,
        basis="FACT_EQUIVALENT",
        field_path="facts.f1.subject",
        detail="主体事件一致",
        history_evidence={"record_id": rid_history, "field": "x",
                          "quote": "甲", "start": 0, "end": 1},
        current_evidence={"record_id": rid_current, "field": "x",
                          "quote": "甲", "start": 0, "end": 1},
        pipeline_version="dedup_v1",
    )


class _SpyClient:
    """bulk 行为间谍（单元层替代真 ES）。"""

    def __init__(self, bulk_response: dict | None = None) -> None:
        self.bulk_calls: list[dict] = []
        self._bulk_response = bulk_response or {"items": []}

    def bulk(self, *, operations, refresh):  # noqa: ANN001 - 间谍同形签名
        self.bulk_calls.append({"operations": list(operations),
                                "refresh": refresh})
        return self._bulk_response


def _spy_store(bulk_response: dict | None = None) -> p18_es.RealESP18Store:
    """绕开 __init__（UAT 闸门 + 真建索引）装配间谍仓储——纯单元层对象。"""
    store = p18_es.RealESP18Store.__new__(p18_es.RealESP18Store)
    store.client = _SpyClient(bulk_response)
    store.config = SimpleNamespace(
        audits_index="p18-batch-spy-news-dedup-audits-v1-2026.09.28",
    )
    return store


class _DispatchSpyStore(FakeCommitStore):
    """双态分派间谍（W2Fε 钉 3 改钉）：兼备批量 write_audit_documents 与
    逐条 write_audit 两方法并各自记录调用——生产 commit_one 的 hasattr
    探针必须命中批量主分支、逐条回退分支零调用。"""

    def __init__(self) -> None:
        super().__init__()
        self.batch_calls: list[list[dict]] = []
        self.single_calls: list[tuple] = []

    def write_audit_documents(self, audit_records):
        docs = list(audit_records)
        self.batch_calls.append(docs)
        return [doc["comparison_id"] for doc in docs]

    def write_audit(self, comparison_id, doc):     # 逐条回退分支间谍
        self.single_calls.append((comparison_id, doc))
        return comparison_id


# ---------- 钉 1：方法存在性（缺方法 = 本红原貌） ----------

def test_real_es_p18_store_exposes_write_audit_documents():
    """commit_one 批量主分支探针：RealESP18Store 必须有 write_audit_documents。

    修复前：hasattr=False → AttributeError/断言红（R-W-01 原貌复现）。"""
    assert hasattr(p18_es.RealESP18Store, "write_audit_documents"), (
        "RealESP18Store 缺 write_audit_documents——commit_one hasattr 探针落空，"
        "将回退到 write_audit 逐条分支并 AttributeError（R-W-01 红原貌）"
    )
    assert callable(p18_es.RealESP18Store.write_audit_documents)
    # 旧名保留（persist_main_record_es 与 P18 UAT 直调路径零影响）
    assert hasattr(p18_es.RealESP18Store, "write_audit_batch")


# ---------- 钉 2：签名对拍现役契约（RealESCommitStore 同形） ----------

def test_write_audit_documents_signature_matches_commit_one_contract():
    """现役契约形：write_audit_documents(audit_records) -> list[str]。

    对拍 commit/es_store.py RealESCommitStore.write_audit_documents（L126）：
    单必需位置参数，无额外必需参数（commit_one 调用点只传 docs 列表）。"""
    sig = inspect.signature(p18_es.RealESP18Store.write_audit_documents)
    params = list(sig.parameters.values())
    assert [p.name for p in params] == ["self", "audit_records"]
    required = [p for p in params[1:] if p.default is inspect.Parameter.empty
                and p.kind in (inspect.Parameter.POSITIONAL_ONLY,
                               inspect.Parameter.POSITIONAL_OR_KEYWORD,
                               inspect.Parameter.KEYWORD_ONLY)]
    assert [p.name for p in required] == ["audit_records"]
    # 对拍 RealESCommitStore 现役实现签名同形
    from news_flash_dedup.commit import es_store as commit_es
    ref = inspect.signature(commit_es.RealESCommitStore.write_audit_documents)
    assert [p.name for p in sig.parameters.values()] == [
        p.name for p in ref.parameters.values()]


# ---------- 钉 3：双态分派行为（R-W-01 AttributeError 原貌 → 修后走主分支） ----------

def test_commit_one_audit_dispatch_prefers_batch_contract():
    """生产直调钉（W2Fε 改钉）：commit_one 双态分派走批量主分支。

    原钉对测试内同构复刻件 `_commit_one_audit_dispatch` 逐行对拍——复刻件
    与生产 commit/coordinator.py L191-199（现 :227-245，09-29 W3e F5① 回锚：
    原写 :204-214 系登记时点锚漂移）无物理绑定：生产
    改派/顺序漂移复刻不红，钉的是影子非实物。改钉：直调生产 commit_one +
    双态间谍仓储（FakeCommitStore 子类增 write_audit_documents）：
    hasattr 探针命中批量主分支 → write_audit_documents 恰一次、收到
    to_doc() 文档列表（含 comparison_id）、返回 id 元组回填主记录
    audit_ids；逐条回退 write_audit 零调用。"""
    from test_p17_2_commit_one import _commit_ctx   # 跨测试模块夹具复用
    # （同仓先例：test_batch_collector 引 test_admission；pytest rootdir 无
    # __init__.py 时按文件目录入 sys.path）
    store = _DispatchSpyStore()
    ctx = _commit_ctx(arrival_seq=2, prepared_seq=2, visible_seq=2)
    outcome = commit_one(ctx, store, audit_complete=True)
    assert outcome.state == "committed"
    # 批量主分支：恰一次批量调用；逐条回退分支零调用（R-W-01 原貌的反面）
    assert len(store.batch_calls) == 1, (
        "hasattr 探针未命中批量主分支或重复落库——分派漂移")
    assert store.single_calls == [], (
        "逐条回退 write_audit 被调——批量主分支未被优先")
    docs = store.batch_calls[0]
    assert docs, "审计批次空——F4 修复点退化（build_audit_batch 恒空）"
    assert all("comparison_id" in doc for doc in docs)
    expected_ids = tuple(doc["comparison_id"] for doc in docs)
    # 主记录 audit_ids 回填 = 批量分支返回 id 元组（生产行为钉）
    record = store.main_records[ctx.current["record_id"]]
    assert tuple(record.audit_ids) == expected_ids


# ---------- 钉 4：批量主分支语义 = write_audit_batch 同体（create/enrichment/id 序） ----------

def test_write_audit_documents_bulk_create_semantics():
    """语义保真钉：委托同一体——bulk create + 文档 enrichment + id 顺序返回。"""
    rec1, rec2 = _audit_record("s1"), _audit_record("s2")
    # comparison_id 唯一化（s1/s2 文档共用模板 rid，此处显式区分）
    object.__setattr__(rec2, "comparison_id", rec2.comparison_id + "#2")
    docs = [rec1.to_doc(), rec2.to_doc()]
    response = {"items": [
        {"create": {"_id": rec1.comparison_id, "status": 201}},
        {"create": {"_id": rec2.comparison_id, "status": 201}},
    ]}
    store = _spy_store(response)
    ids = store.write_audit_documents(docs)
    assert ids == [rec1.comparison_id, rec2.comparison_id]

    ops = store.client.bulk_calls[0]["operations"]
    assert len(ops) == 4                                  # 2 文档 × (action+doc)
    assert ops[0]["create"]["_id"] == rec1.comparison_id
    assert ops[2]["create"]["_id"] == rec2.comparison_id
    # create 语义（审计不可覆盖，P18-INV-1/INV-6）+ 目标索引
    for action in (ops[0], ops[2]):
        assert set(action.keys()) == {"create"}
        assert action["create"]["_index"] == (
            "p18-batch-spy-news-dedup-audits-v1-2026.09.28")
    # 文档 enrichment 与 write_audit_batch 同体（created_at/版本缺省补）
    for doc in (ops[1], ops[3]):
        assert doc["comparison_id"] in (rec1.comparison_id, rec2.comparison_id)
        assert doc["created_at"]                            # setdefault 补位
        assert doc["decision_artifact_version"] == "1.0"
    # refresh 口径与 write_audit_batch 一致（wait_for）
    assert store.client.bulk_calls[0]["refresh"] == "wait_for"


# ---------- 钉 5：失败即停 + 空批零调用 ----------

def test_write_audit_documents_failure_stops_and_empty_noop():
    """冲突/错误 → AuditPersistError（失败即停，CAS 前置绝不掩盖）；空批零 bulk。"""
    rec = _audit_record()
    conflict = {"items": [{"create": {
        "_id": rec.comparison_id,
        "error": {"type": "version_conflict_engine_exception",
                  "reason": "document already exists"},
        "status": 409}}]}
    store = _spy_store(conflict)
    with pytest.raises(p18_es.AuditPersistError, match="(?i)bulk create failed"):
        store.write_audit_documents([rec.to_doc()])

    empty_store = _spy_store()
    assert empty_store.write_audit_documents([]) == []
    assert empty_store.client.bulk_calls == []              # 空批零网络动作
