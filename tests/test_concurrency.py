import re
import threading

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver

from eta_agent.demo import make_eml
from eta_agent.erp.fake import FakeERP
from eta_agent.inbox import InboxScanner
from eta_agent.llm.extractor import RuleBasedExtractor
from eta_agent.service import make_agent
from eta_agent.store import Store
from eta_agent.web.app import create_app

ACME, MCU, MANAGER = "sales@acme-passive.com.tw", "delivery@formosa-mcu.com.tw", "manager@example.com"
AUTO = "PUR-ORD-2026-00001 AC-C100 2,000 pcs 延後至 11/7 到貨。"
REVIEW = "PUR-ORD-2026-00002 FM-32F4 800 pcs 只能於 115/11/09 出貨，請見諒。"


def two_programs(tmp_path, erp, checkpointer=None):
    db = f"sqlite:///{tmp_path}/agent.db"
    cp = checkpointer or InMemorySaver()
    return [make_agent(erp, Store(db), RuleBasedExtractor(), cp) for _ in range(2)]


def run_together(*fns):
    start = threading.Barrier(len(fns))
    out = [None] * len(fns)

    def wrap(i, fn):
        start.wait()
        try:
            out[i] = fn()
        except Exception as e:  # noqa: BLE001
            out[i] = e
    ts = [threading.Thread(target=wrap, args=(i, f)) for i, f in enumerate(fns)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=30)
    return out


def applied_count(store, mid):
    return sum(1 for e in store.audit_events(mid) if e["event"] == "applied"
               and (e["detail"] or {}).get("status") == "applied")


def test_same_email_processed_by_two_programs_at_once_is_written_once(tmp_path):
    erp = FakeERP()
    a, b = two_programs(tmp_path, erp)
    raw = make_eml(ACME, "RE", AUTO, "race-1@x")
    for _ in range(5):
        res = run_together(lambda: a.process_eml(raw), lambda: b.process_eml(raw))
        assert not [r for r in res if isinstance(r, Exception)], res
    statuses = sorted(r["status"] for r in res)
    assert "done" not in statuses or statuses.count("done") <= 1
    assert applied_count(a.store, "race-1@x") == 1
    assert erp.po_lines["POI-0001-1"]["schedule_date"] == "2026-11-07"
    assert len(erp.op_log) == 1


def test_two_inbox_scanners_on_the_same_folder(tmp_path):
    erp = FakeERP()
    a, b = two_programs(tmp_path, erp)
    box = tmp_path / "inbox"
    s1 = InboxScanner(lambda: a, folder=box, min_age_seconds=0)
    s2 = InboxScanner(lambda: b, folder=box, min_age_seconds=0)
    s1.ensure_folders()
    bodies = [(ACME, AUTO), (MCU, REVIEW), ("pcb@hc-pcb.com.tw", "PUR-ORD-2026-00003 HP-GW200-R2 到貨日延後至 11/5。"),
              (ACME, "PUR-ORD-2026-00005 AC-BOX1 1,000 pcs 延後至 11/17 到貨。"),
              ("cs@delta-conn.com", "PUR-ORD-2026-00004 DC-USBC 500 pcs 延後至 11/13 到貨。")]
    for i, (sender, body) in enumerate(bodies):
        (box / f"m{i}.eml").write_bytes(make_eml(sender, "RE", body, f"scan-{i}@x"))
    for _ in range(3):
        run_together(s1.scan_once, s2.scan_once)
    assert s1.last_error is None and s2.last_error is None
    assert list(box.glob("*.eml")) == []
    assert list((box / "failed").glob("*")) == []
    for i in range(len(bodies)):
        rec = a.store.get_message(f"scan-{i}@x")
        assert rec and rec["status"] in ("done", "waiting_review"), (i, rec)
        assert applied_count(a.store, f"scan-{i}@x") <= 1
    assert len(erp.op_log) == 4


def test_file_taken_by_the_other_program_while_processing_is_not_an_error(tmp_path):
    erp = FakeERP()
    a, _ = two_programs(tmp_path, erp)
    box = tmp_path / "inbox"
    scanner = InboxScanner(lambda: a, folder=box, min_age_seconds=0)
    scanner.ensure_folders()
    f = box / "x.eml"
    f.write_bytes(make_eml(ACME, "RE", AUTO, "taken@x"))
    real = a.process_eml

    def process_and_lose_file(raw):
        res = real(raw)
        f.rename(box / "processed" / "x-by-other.eml")
        return res
    a.process_eml = process_and_lose_file
    results = scanner.scan_once()
    assert scanner.last_error is None
    assert [r.status for r in results] == ["done"]
    assert list((box / "failed").glob("*")) == []


def test_file_gone_before_reading_is_skipped(tmp_path):
    erp = FakeERP()
    a, _ = two_programs(tmp_path, erp)
    box = tmp_path / "inbox"
    scanner = InboxScanner(lambda: a, folder=box, min_age_seconds=0)
    scanner.ensure_folders()
    f = box / "gone.eml"
    f.write_bytes(make_eml(ACME, "RE", AUTO, "gone@x"))
    files = scanner.waiting_files()
    f.unlink()
    scanner.waiting_files = lambda: files
    assert scanner.scan_once() == []
    assert scanner.last_error is None and list((box / "failed").glob("*")) == []


def _waiting(agent, erp, mid="appr@x"):
    r = agent.process_eml(make_eml(MCU, "RE", REVIEW, mid))
    assert r["status"] == "waiting_review"
    erp.workflow_action(r["case_id"], "Approve", MANAGER)
    return r


def test_double_click_approve_in_one_program_writes_once(tmp_path):
    erp = FakeERP()
    a, _ = two_programs(tmp_path, erp)
    r = _waiting(a, erp)
    res = run_together(lambda: a.resume(r["thread_id"], MANAGER), lambda: a.resume(r["thread_id"], MANAGER))
    done = [x for x in res if isinstance(x, dict) and x["status"] == "done"]
    assert len(done) >= 1
    for x in res:
        if isinstance(x, Exception):
            assert isinstance(x, (RuntimeError, ValueError)), x
    assert erp.po_lines["POI-0002-1"]["schedule_date"] == "2026-11-12"
    assert len(erp.op_log) == 1


def test_two_programs_resuming_the_same_approval_write_once(tmp_path):
    erp = FakeERP()
    a, b = two_programs(tmp_path, erp)
    r = _waiting(a, erp)
    res = run_together(lambda: a.resume(r["thread_id"], MANAGER), lambda: b.resume(r["thread_id"], MANAGER))
    assert any(isinstance(x, dict) and x["status"] == "done" for x in res), res
    assert erp.po_lines["POI-0002-1"]["schedule_date"] == "2026-11-12"
    assert len(erp.op_log) == 1
    assert erp.cases[r["case_id"]]["status"] == "Applied"


def _web(tmp_path, agent):
    web = TestClient(create_app(agent_factory=lambda: agent, backend="fake", inbox=tmp_path / "box", watch=False))
    page = web.post("/login/demo", data={"user": MANAGER})
    return web, re.search(r'name="csrf" (?:value|content)="([^"]+)"', page.text).group(1)


def test_web_second_click_on_approve_shows_a_message_not_an_error(tmp_path):
    erp = FakeERP()
    a, _ = two_programs(tmp_path, erp)
    a.process_eml(make_eml(MCU, "RE", REVIEW, "web2@x"))
    web, csrf = _web(tmp_path, a)
    first = web.post("/emails/web2@x/approve", data={"csrf": csrf})
    second = web.post("/emails/web2@x/approve", data={"csrf": csrf})
    assert first.status_code == 200 and second.status_code == 200
    assert "不在等待審核" in second.text
    assert len(erp.op_log) == 1


def test_web_approve_while_the_same_email_is_being_processed(tmp_path):
    erp = FakeERP()
    a, _ = two_programs(tmp_path, erp)
    r = _waiting(a, erp, "busy@x")
    web, csrf = _web(tmp_path, a)
    a._running.add("busy@x")
    resp = web.post("/emails/busy@x/apply-decision", data={"csrf": csrf})
    assert resp.status_code == 200 and "正在處理中" in resp.text
    a._running.discard("busy@x")
    assert erp.po_lines["POI-0002-1"]["schedule_date"] == "2026-11-04"
    assert r["case_id"]
