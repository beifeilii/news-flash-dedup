# -*- coding: utf-8 -*-
"""W2Fα2 红测（decide/llm_residual.py 条目 7 两处 + 条目 59 硬约束钉）：

条目 7a（WA1b-L3，:533-534）：sync collect 未知票据裸 KeyError——与模块
docstring"票据丢失/超时同样单列 failure，不冒充存疑"（:41、:254）直接相悖。
修法=未知/丢失票据 → cell="failure" 单列（hc/ch 双顺序 status="failure"，
台账 failures+1），不裸抛、不冒充存疑。

条目 7b（WA1b-L3，:379,431-444）：_tickets dict 无锁（submit/collect 并发
可丢票据/竞态弹票）+ 缓存 tmp 按内容键共享（同键并发写互踩可产损坏条目）。
修法=_tickets 加锁 + tmp 名带 pid/tid 唯一化（os.replace 原子语义不变）。

条目 59（WB2 L5，测试面硬约束）：残判层不回写五字段主记录（09 §1.3-4
"代码负责公共 decision"、§1.3-9"边界不改写"；模块 docstring"残判签发不回写
五字段主记录，合并口径在审计/指标层物化"在案）——钉死防未来违反。
"""
from __future__ import annotations

import dataclasses
import json
import re
import threading

from news_flash_dedup.decide import llm_residual as lr


# ---------------------------------------------------------------- 测试工装

def _jjson(decision, ea, eb):
    return json.dumps({
        "decision": decision,
        "evidence_a": list(ea), "evidence_b": list(eb),
        "numeric_check": {"numbers_a": [], "numbers_b": [], "conclusion": "无关键数值"},
        "time_check": {"times_a": [], "times_b": [], "conclusion": "均无时间"},
        "reason": "测试判定",
    }, ensure_ascii=False)


H, C = "甲公司营收100万", "甲公司营收100万元整"
_RESP = {
    (H, C): _jjson("重复", ("甲公司营收100万",), ("甲公司营收100万",)),
    (C, H): _jjson("重复", ("甲公司营收100万",), ("甲公司营收100万",)),
}


class _MockLLM:
    def __init__(self, responses):
        self.responses = responses

    def __call__(self, *, model, system, user):
        m = re.match(r"【文本A】\n(.*)\n\n【文本B】\n(.*)\Z", user, re.DOTALL)
        assert m, f"用户消息形态异常：{user[:60]!r}"
        return self.responses[(m.group(1), m.group(2))], 0.01


def _judge(*, cache_dir=None, ledger=None):
    return lr.SyncResidualJudge(
        lr.ResidualJudgeConfig(cache_dir=cache_dir),
        call_fn=_MockLLM(_RESP), ledger=ledger)


# ---------------------------------------------------------------- 条目 7a：未知票据单列 failure

def test_l3_collect_unknown_ticket_returns_failure_cell_not_keyerror():
    """条目7a 主红测：未知票据 collect → failure 单列（不裸 KeyError）。"""
    judge = _judge()
    out = judge.collect("ticket-never-submitted")
    assert out.cell == "failure"
    assert out.signed is False
    assert out.suspicion_score is None
    assert out.pair_id == "ticket-never-submitted"
    assert out.hc.status == "failure" and out.ch.status == "failure"
    assert "ticket-never-submitted" in (out.hc.error or "")


def test_l3_collect_unknown_ticket_bumps_ledger_failures():
    """条目7a 台账钉：票据丢失 failures+1（INV 式单列，不折算边界）。"""
    ledger = lr.ResidualJudgeLedger()
    judge = _judge(ledger=ledger)
    judge.collect("ghost-1")
    judge.collect("ghost-2")
    assert ledger.snapshot()["failures"] == 2


def test_l3_double_collect_second_is_failure_not_keyerror():
    """条目7a 同族钉：同一票据二次 collect（已被弹票=丢失）→ failure 单列。"""
    judge = _judge()
    ticket = judge.submit(lr.ResidualTask(pair_id="p1", text_history=H,
                                          text_current=C))
    first = judge.collect(ticket)
    assert first.signed is True
    second = judge.collect(ticket)
    assert second.cell == "failure" and second.signed is False


def test_l3_known_ticket_still_popped_verbatim():
    """条目7a 守卫（前后均绿）：正常票据 collect 行为逐字节不变。"""
    judge = _judge()
    ticket = judge.submit(lr.ResidualTask(pair_id="p1", text_history=H,
                                          text_current=C))
    out = judge.collect(ticket)
    assert out.signed is True and out.pair_id == "p1" and out.cell == "signed"


# ---------------------------------------------------------------- 条目 7b：票据锁 + tmp 唯一化

def test_l3_tickets_guarded_by_lock():
    """条目7b 锁钉：_tickets 配专用锁（submit/collect 临界区）。"""
    judge = _judge()
    assert hasattr(judge, "_tickets_lock")
    assert isinstance(judge._tickets_lock, type(threading.Lock()))


def test_l3_cache_tmp_name_unique_per_writer(tmp_path, monkeypatch):
    """条目7b tmp 钉：临时文件名带写入者身份（pid/tid），同键并发不互踩。"""
    seen: list[str] = []
    real_replace = lr.os.replace

    def spy_replace(src, dst):
        seen.append(str(src))
        return real_replace(src, dst)

    monkeypatch.setattr(lr.os, "replace", spy_replace)
    judge = _judge(cache_dir=str(tmp_path))
    judge.judge_pair("p1", H, C)
    assert seen, "缓存写未经 os.replace（原子写纪律被破坏）"
    for tmp_name in seen:
        # 带点片段断言：防 pid/tid 数字子串偶合 sha256 hex 键（前缀形态 "{键}.{pid}.{tid}.tmp"）
        assert f".{lr.os.getpid()}." in tmp_name
        assert f".{threading.get_ident()}." in tmp_name


def test_l3_concurrent_submit_collect_no_ticket_loss():
    """条目7b 并发烟测：多线程 submit/collect 自票自取，零丢失零串票。"""
    judge = _judge()
    errors: list[BaseException] = []
    outcomes: dict[str, object] = {}
    lock = threading.Lock()

    def worker(i: int) -> None:
        try:
            pid = f"p{i}"
            # _MockLLM 键只认 (H,C)/(C,H)——pair_id 不进路由，票据按 pair_id 分账
            ticket = judge.submit(lr.ResidualTask(pair_id=pid, text_history=H,
                                                  text_current=C))
            out = judge.collect(ticket)
            with lock:
                outcomes[pid] = out
        except BaseException as exc:  # noqa: BLE001 - 烟测全捕登记
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert len(outcomes) == 16
    for pid, out in outcomes.items():
        assert out.pair_id == pid


# ---------------------------------------------------------------- 条目 59：五字段不回写硬约束钉

FIVE_FIELDS = frozenset({"item_id", "text", "decision", "duplicate_ids", "reason"})


def test_l5_residual_outcome_fields_disjoint_from_five_fields():
    """条目59 结构钉①：ResidualOutcome 字段名与五字段封闭集零交。"""
    names = {f.name for f in dataclasses.fields(lr.ResidualOutcome)}
    assert names.isdisjoint(FIVE_FIELDS)


def test_l5_audit_payload_top_level_disjoint_from_five_fields():
    """条目59 结构钉②：to_audit_dict() 顶层键与五字段零交（残判只在审计/
    扩展槽参与合并口径，顶层不携五字段主记录语义）。"""
    out = _judge().judge_pair("p1", H, C)
    assert set(out.to_audit_dict()).isdisjoint(FIVE_FIELDS)


def test_l5_module_has_no_decide_outcome_surface():
    """条目59 结构钉③：残判模块不引入/不构造 DecideOutcome（无回写载体）。"""
    assert not hasattr(lr, "DecideOutcome")
    assert "DecideOutcome" not in lr.__all__
    assert not hasattr(lr.SyncResidualJudge, "write_back")
    assert not hasattr(lr.SyncResidualJudge, "update_main_record")


def test_l5_judge_pair_returns_no_five_field_attributes():
    """条目59 结构钉④：judge_pair 产物无任何五字段属性（实例级）。"""
    out = _judge().judge_pair("p1", H, C)
    for name in FIVE_FIELDS:
        assert not hasattr(out, name)


def test_l5_module_docstring_anchors_no_writeback_discipline():
    """条目59 文书锚：模块 docstring 明载"不回写五字段"纪律（防静默漂移）。"""
    assert "不回写五字段" in (lr.__doc__ or "")
