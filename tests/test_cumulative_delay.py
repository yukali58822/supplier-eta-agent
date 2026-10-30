ACME = "sales@acme-passive.com.tw"
MANAGER = "manager@example.com"
LINE = "POI-0001-1"


def delay_to(agent, eml, day, n):
    return agent.process_eml(eml(ACME, "RE: PUR-ORD-2026-00001", f"PUR-ORD-2026-00001 AC-C100 到貨日延後至 {day}。",
                                 f"creep-{n}@x"))


def test_small_delays_add_up_and_go_to_review(agent, erp, eml):
    r1 = delay_to(agent, eml, "11/7", 1)
    assert r1["status"] == "done" and erp.po_lines[LINE]["schedule_date"] == "2026-11-07"
    r2 = delay_to(agent, eml, "11/9", 2)
    assert r2["status"] == "waiting_review"
    assert any("這次延後 2 天，累計延後 4 天（基準交期 2026-11-05）" in x for x in r2["reasons"])
    assert erp.po_lines[LINE]["schedule_date"] == "2026-11-07"
    d = r2["state"]["diffs"][0]
    assert d["date_diff_days"] == 2 and d["cumulative_diff_days"] == 4 and d["baseline_date"] == "2026-11-05"


def test_first_change_has_no_cumulative_note(agent, erp, eml):
    r = delay_to(agent, eml, "11/7", 1)
    assert r["state"]["diffs"][0]["cumulative_diff_days"] is None
    assert any("延後 2 天（門檻內）" in x for x in r["state"]["decision"]["reasons"])


def test_delay_then_pull_back_is_judged_on_the_total(agent, erp, eml):
    delay_to(agent, eml, "11/7", 1)
    r = delay_to(agent, eml, "11/6", 2)
    assert r["status"] == "done"
    assert any("累計 +1 天，門檻內" in x for x in r["state"]["decision"]["reasons"])


def test_approved_date_becomes_the_new_baseline(agent, erp, eml):
    r = delay_to(agent, eml, "11/10", 1)
    assert r["status"] == "waiting_review"
    erp.workflow_action(r["case_id"], "Approve", MANAGER)
    assert agent.resume(r["thread_id"], MANAGER)["status"] == "done"
    assert erp.po_lines[LINE]["schedule_date"] == "2026-11-10"
    r2 = delay_to(agent, eml, "11/11", 2)
    assert r2["status"] == "done"
    assert r2["state"]["diffs"][0]["cumulative_diff_days"] is None


def test_manual_change_in_erpnext_restarts_the_count(agent, erp, eml):
    delay_to(agent, eml, "11/7", 1)
    erp.po_lines[LINE]["schedule_date"] = "2026-11-20"
    r = delay_to(agent, eml, "11/22", 2)
    assert r["status"] == "done"
    assert r["state"]["diffs"][0]["cumulative_diff_days"] is None


def test_web_shows_cumulative_delay(agent, erp, eml, tmp_path):
    from fastapi.testclient import TestClient

    from eta_agent.web.app import create_app
    delay_to(agent, eml, "11/7", 1)
    delay_to(agent, eml, "11/9", 2)
    web = TestClient(create_app(agent_factory=lambda: agent, backend="fake", inbox=tmp_path / "inbox", watch=False))
    web.post("/login/demo", data={"user": MANAGER})
    page = web.get("/emails/creep-2@x").text
    assert "累計 +4 天（基準 11/5（四））" in page and "累計延後 4 天" in page
