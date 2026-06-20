"""应收账款 催收 (dunning) — deterministic tier logic + no-double-dun + no-auto-send.

Pure tier/outcome logic (no LLM), plus orchestrator-level integration proving:
  - a re-run NEVER re-sends an account dunned within the cadence window (LOCKED), and
  - the orchestrator NEVER marks a message 'sent' (only the human send route does).
"""
import csv
import os
import sys
from datetime import datetime, timedelta

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from agent_team import (ar_orchestrator, ar_templates, config, db,  # noqa: E402
                        dunning as d, metrics_store as ms)
from agent_team.memory import SharedMemory  # noqa: E402

PACK = ar_templates.load_pack("通用")
NOW = datetime(2026, 6, 30)


# ---------------------------------------------------------------------------
# Pure tier logic
# ---------------------------------------------------------------------------
def test_new_account_reflects_severity():
    # never dunned, 60 days overdue, clean -> first touch is 最终通知 (not a reminder)
    dec = d.assign_tier(60, 5000, None, "确定", PACK)
    assert dec["target_tier"] == d.TIER_FINAL and dec["route"] == "draft"
    out = d.classify_outcome(dec, None, PACK, now=NOW)
    assert out["outcome"] == d.OUTCOME_NEW and out["should_draft"] is True


def test_large_amount_escalates_sooner():
    dec = d.assign_tier(10, 20000, None, "确定", PACK)   # >= amount_large, dpd 10
    assert dec["eligible_tier"] == d.TIER_URGE
    assert d.assign_tier(200000, 1, None, "确定", PACK)["route"] == "handoff"  # >90d
    assert d.assign_tier(5, 100000, None, "确定", PACK)["route"] == "handoff"  # legal amt


def test_suspect_caps_to_reminder_or_routes_to_handoff():
    capped = d.assign_tier(20, 500, None, "疑似", PACK)   # would be urge -> capped reminder
    assert capped["target_tier"] == d.TIER_REMINDER and capped["capped_by_grade"]
    routed = d.assign_tier(60, 5000, None, "疑似", PACK)  # would be final -> handoff
    assert routed["route"] == "handoff"


def test_escalation_one_rung_never_drops():
    # warrants final, but only reminder sent -> climb ONE rung to urge
    dec = d.assign_tier(60, 5000, d.TIER_REMINDER, "确定", PACK)
    assert dec["target_tier"] == d.TIER_URGE and dec["is_escalation"] is True
    # warrants urge, urge already sent -> nothing new (no drop, no re-send)
    dec2 = d.assign_tier(30, 500, d.TIER_URGE, "确定", PACK)
    assert dec2["route"] == "skip" and dec2["target_tier"] is None


def test_classify_outcome_locked_escalated_awaiting():
    dec = d.assign_tier(20, 500, d.TIER_REMINDER, "确定", PACK)   # warrants urge
    locked = {"tier": "reminder", "sent_at": (NOW - timedelta(days=2)).isoformat()}
    assert d.classify_outcome(dec, locked, PACK, now=NOW)["outcome"] == d.OUTCOME_LOCKED
    past = {"tier": "reminder", "sent_at": (NOW - timedelta(days=10)).isoformat()}
    esc = d.classify_outcome(dec, past, PACK, now=NOW)
    assert esc["outcome"] == d.OUTCOME_ESCALATED and esc["tier"] == "urge"
    same = {"tier": "urge", "sent_at": (NOW - timedelta(days=10)).isoformat()}
    assert d.classify_outcome(dec, same, PACK, now=NOW)["outcome"] == d.OUTCOME_AWAITING


def test_build_account_facts_injects_engine_numbers():
    cust = {"customer_id": "C1", "customer_name": "甲公司", "outstanding": 5000.0}
    invs = [{"invoice_no": "INV1", "open_balance": 5000.0, "due_date": "2026-05-01",
             "dpd": 60, "overdue": True}]
    facts = d.build_account_facts(cust, invs, d.TIER_FINAL)
    assert any("INV1" in f and "【" in f for f in facts["facts_block"])
    assert "5000.00" in facts["known_numbers"] and "60" in facts["known_numbers"]
    assert facts["tier_cn"] == "最终通知"


# ---------------------------------------------------------------------------
# Orchestrator integration: no-auto-send + no-double-dun on re-run
# ---------------------------------------------------------------------------
class _FakeRouter:
    def __init__(self, token_manager=None, cost_listener=None, **k):
        self.cost_listener = cost_listener
        self.token_manager = token_manager

    def complete(self, **kw):
        if self.cost_listener:
            self.cost_listener(config.HAIKU, 3000, 300)
        return "# 应收账款账龄报告\n\n## 核心结论\n- 应收 ¥500.00【1】。\n"

    def complete_with_tools(self, **kw):
        return self.complete(**kw)


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    db.init_db(str(tmp_path / "brain.db"))
    monkeypatch.setattr(ms, "REPORTS_DIR", str(tmp_path / "reports"))
    yield


def _csv(tmp, name, header, rows):
    p = tmp / name
    with open(p, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        for r in rows:
            w.writerow(r)
    return str(p)


def _run_ar(tmp, cid, rid, job):
    inv = _csv(tmp, f"inv{job}.csv", ["发票号", "客户编号", "客户名称", "到期日", "金额"],
               [["INV1", "C1", "甲公司", "2026-06-20", "500"]])
    pay = _csv(tmp, f"pay{job}.csv", ["流水号", "客户编号", "收款日期", "收款金额"],
               [["P9", "C2", "2026-06-30", "100"]])   # orphan -> pushes as_of to 2026-06-30
    cus = _csv(tmp, f"cus{job}.csv", ["客户编号", "客户名称", "联系方式"],
               [["C1", "甲公司", "boss@c1.com"]])
    mem = SharedMemory(job, store_dir=str(tmp / "mem"), goal="应收")
    ar_orchestrator.AROrchestrator(
        mem, {"invoices": (inv, "inv"), "payments": (pay, "pay"),
              "customers": (cus, "cus")},
        format_key="通用", period_label="2026-06", cost_listener=lambda m, i, o: None,
        llm=_FakeRouter(), company_id=cid, run_id=rid, account_id="acct1").run()
    return mem


def test_orchestrator_drafts_are_never_sent_and_rerun_does_not_double_dun(fresh_db, tmp_path):
    cid = ms.get_or_create_company("boss@x.com", company_name="Acme")
    rid1 = ms.create_run(cid, "应收 1", kind="ar")
    _run_ar(tmp_path, cid, rid1, "arA")

    msgs = ms.list_dunning_messages(cid)
    drafts = [m for m in msgs if m["customer_key"] == "C1"]
    assert len(drafts) == 1 and drafts[0]["tier"] == "reminder"  # 10 days overdue
    # NO-AUTO-SEND: the orchestrator never marks anything 'sent'.
    assert all(m["state"] != "sent" for m in msgs)
    assert drafts[0]["draft_body"]                                # numbers-injected draft

    # Simulate the human approve-then-send (the ONLY path to 'sent').
    ms.set_dunning_state(cid, drafts[0]["id"], "approved")
    ms.set_dunning_state(cid, drafts[0]["id"], "sent", channel="email", delivery_id=1)
    assert ms.last_dunning(cid, "C1")["tier"] == "reminder"

    # RE-RUN: same data, within the 7-day cadence lock -> LOCKED, NO new draft.
    rid2 = ms.create_run(cid, "应收 2", kind="ar")
    _run_ar(tmp_path, cid, rid2, "arB")
    after = ms.list_dunning_messages(cid)
    c1_msgs = [m for m in after if m["customer_key"] == "C1"]
    assert len(c1_msgs) == 1                       # no second draft was created
    assert c1_msgs[0]["state"] == "sent"           # the original, untouched
