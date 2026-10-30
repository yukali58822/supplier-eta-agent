import json

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from eta_agent.demo import make_eml
from eta_agent.erp import setup_erpnext as se
from eta_agent.erp.erpnext import ERPNextClient
from eta_agent.erp.frappe_rest import FrappeREST
from eta_agent.llm.extractor import RuleBasedExtractor
from eta_agent.service import make_agent
from eta_agent.store import Store
from frappe_mock import FrappeMock

URL = "https://demo.example.frappe.cloud"
ACME, MCU = "sales@acme-passive.com.tw", "delivery@formosa-mcu.com.tw"


@pytest.fixture
def site(tmp_path, monkeypatch):
    monkeypatch.setattr(se, "MAP_PATH", tmp_path / "erpnext_map.json")
    mock = FrappeMock()
    rest = FrappeREST(URL, "k", "s", transport=mock.transport())
    logs = []
    se.setup(rest, logs.append)
    mapping = se.seed(rest, "示範製造股份有限公司", logs.append)
    client = ERPNextClient(URL, "k", "s", transport=mock.transport())
    return mock, rest, client, mapping


def _loc(mapping, text):
    for k, v in mapping["purchase_orders"].items():
        text = text.replace(k, v)
    return text


def _agent(client):
    return make_agent(client, Store("sqlite://"), RuleBasedExtractor(), InMemorySaver())


def test_setup_and_seed_are_idempotent(site, tmp_path):
    mock, rest, client, mapping = site
    n_po = len(mock.db["Purchase Order"])
    se.setup(rest, lambda m: None)
    m2 = se.seed(rest, "示範製造股份有限公司", lambda m: None)
    assert len(mock.db["Purchase Order"]) == n_po == 6 and m2 == mapping
    assert mapping["purchase_orders"]["PUR-ORD-2026-00001"].startswith("PO-REAL-")
    assert {"Supplier ETA Case", "Shortage Task", "ETA Operation Log"} <= set(mock.db["DocType"])


def test_agent_reads_everything_from_erpnext(site):
    mock, rest, client, mapping = site
    assert se.check(client, lambda m: None)
    sup = client.find_supplier_by_email(MCU)
    assert sup.name == "Formosa MCU Supply" and sup.transit_days == 3
    lines = client.open_po_lines(sup.name)
    assert {l.supplier_part_no for l in lines} == {"FM-32F4"} and len(lines) == 2
    wos = {w.name: w for w in client.open_work_orders()}
    assert wos[mapping["work_orders"]["WO-0001"]].required["MCU-STM32"] == 500
    assert client.stock(["MCU-STM32"])["MCU-STM32"] == (50, 0)
    assert any(r.startswith("/api/resource/Bin") for _, r in mock.calls)


def test_scenario_a_auto_writes_to_erpnext(site):
    mock, rest, client, mapping = site
    body = _loc(mapping, "PUR-ORD-2026-00001 AC-C100 2,000 pcs 到貨日延後至 11/7。")
    r = _agent(client).process_eml(make_eml(ACME, "RE", body, "erp-a"))
    assert r["route"] == "auto" and r["result"]["status"] == "applied"
    po = mock.db["Purchase Order"][mapping["purchase_orders"]["PUR-ORD-2026-00001"]]
    assert [i["schedule_date"] for i in po["items"]] == ["2026-11-07", "2026-11-05"]
    [log] = mock.db["ETA Operation Log"].values()
    assert log["status"] == "Done"


def test_scenario_b_review_approve_apply(site):
    mock, rest, client, mapping = site
    ag = _agent(client)
    body = _loc(mapping, "PUR-ORD-2026-00002 FM-32F4 800 pcs 只能於 115/11/09 出貨。")
    r = ag.process_eml(make_eml(MCU, "RE", body, "erp-b"))
    so = mapping["sales_orders"]["SO-0042"]
    assert r["status"] == "waiting_review" and any(so in x and "3 個工作天" in x for x in r["reasons"])
    case = r["case_id"]
    assert mock.db["Supplier ETA Case"][case]["workflow_state"] == "Pending"
    with pytest.raises(ValueError):
        ag.resume(r["thread_id"], "manager@demo.tw")
    mock.user = "manager@demo.tw"
    client.workflow_action(case, "Approve")
    mock.user = "Administrator"
    assert client.get_case(case).approver == "manager@demo.tw"
    r2 = ag.resume(r["thread_id"], "manager@demo.tw")
    assert r2["status"] == "done"
    po = mock.db["Purchase Order"][mapping["purchase_orders"]["PUR-ORD-2026-00002"]]
    assert po["items"][0]["schedule_date"] == "2026-11-12"
    assert mock.db["Supplier ETA Case"][case]["workflow_state"] == "Applied"


def test_buyer_cannot_approve_in_erpnext(site):
    mock, rest, client, mapping = site
    r = _agent(client).process_eml(make_eml(MCU, "RE", _loc(mapping, "PUR-ORD-2026-00002 FM-32F4 只能於 115/11/09 出貨。"), "erp-c"))
    mock.user = "buyer@demo.tw"
    with pytest.raises(PermissionError):
        client.workflow_action(r["case_id"], "Approve")


def test_po_edited_after_approval_expires_case(site):
    mock, rest, client, mapping = site
    ag = _agent(client)
    r = ag.process_eml(make_eml(MCU, "RE", _loc(mapping, "PUR-ORD-2026-00002 FM-32F4 800 pcs 只能於 115/11/09 出貨。"), "erp-d"))
    mock.user = "manager@demo.tw"
    client.workflow_action(r["case_id"], "Approve")
    mock.user = "Administrator"
    mock.db["Purchase Order"][mapping["purchase_orders"]["PUR-ORD-2026-00002"]]["items"][0]["qty"] = 700
    r2 = ag.resume(r["thread_id"], "manager@demo.tw")
    assert r2["result"]["status"] == "version_conflict"
    assert mock.db["Supplier ETA Case"][r["case_id"]]["workflow_state"] == "Expired"


def test_writeback_failure_is_recovered_without_duplicates(site):
    mock, rest, client, mapping = site
    mock.fail_update_items_once = True
    body = _loc(mapping, "PUR-ORD-2026-00001 AC-C100 2,000 pcs 到貨日延後至 11/7。")
    r = _agent(client).process_eml(make_eml(ACME, "RE", body, "erp-e"))
    assert r["result"]["status"] == "applied" and r["result"]["attempts"] == 2
    assert len(mock.db["ETA Operation Log"]) == 1


def test_same_op_applied_once(site):
    mock, rest, client, mapping = site
    line = mapping["po_lines"]["POI-0001-1"]
    p = {"op_id": "op-x", "message_id": "m", "mode": "auto", "case_id": None, "actions": [
        {"type": "update_po_line", "po": mapping["purchase_orders"]["PUR-ORD-2026-00001"], "line": line,
         "expected_version": client.po_line_version(line), "set": {"schedule_date": "2026-11-06"}}]}
    assert client.apply_change(p)["status"] == "applied"
    assert client.apply_change(p)["status"] == "already_applied"


def test_auto_mode_cannot_change_price(site):
    mock, rest, client, mapping = site
    line = mapping["po_lines"]["POI-0001-1"]
    res = client.apply_change({"op_id": "op-y", "message_id": "m", "mode": "auto", "case_id": None, "actions": [
        {"type": "update_po_line", "po": mapping["purchase_orders"]["PUR-ORD-2026-00001"], "line": line,
         "expected_version": client.po_line_version(line), "set": {"rate": 0.01}}]})
    assert res["status"] == "rejected"
    assert mock.db["ETA Operation Log"]["op-y"]["status"] == "Rejected"


def test_shortage_loop_in_erpnext(site):
    mock, rest, client, mapping = site
    ag = _agent(client)
    po5 = mapping["purchase_orders"]["PUR-ORD-2026-00005"]
    r = ag.process_eml(make_eml(ACME, "RE", f"{po5} AC-BOX1 共 1,000 pcs，11/16 先交 600 pcs 到貨，剩餘 400 pcs 待定。", "erp-s1"))
    client.workflow_action(r["case_id"], "Approve")
    ag.resume(r["thread_id"], "Administrator")
    [task] = client.open_shortage_tasks()
    assert task.qty == 400 and task.po_item == mapping["po_lines"]["POI-0005-1"]
    assert mock.db["Shortage Task"][task.name]["assigned_to"] is None
    r2 = ag.process_eml(make_eml(ACME, "RE", f"{po5} AC-BOX1 剩餘 400 pcs 確認將於 11/13 到貨。", "erp-s2",
                                 date="Thu, 29 Oct 2026 09:00:00 +0800"))
    assert r2["route"] == "auto" and client.open_shortage_tasks() == []


def test_reset_restores_seed_values(site):
    mock, rest, client, mapping = site
    body = _loc(mapping, "PUR-ORD-2026-00001 AC-C100 2,000 pcs 到貨日延後至 11/7。")
    _agent(client).process_eml(make_eml(ACME, "RE", body, "erp-r"))
    se.reset(rest, lambda m: None)
    po = mock.db["Purchase Order"][mapping["purchase_orders"]["PUR-ORD-2026-00001"]]
    assert [i["schedule_date"] for i in po["items"]] == ["2026-11-05", "2026-11-05"]


def test_rates_never_reach_llm_from_erpnext(site):
    mock, rest, client, mapping = site

    class Spy(RuleBasedExtractor):
        seen = []

        def extract(self, subject, body, candidates):
            Spy.seen.append(json.dumps(candidates, ensure_ascii=False))
            return super().extract(subject, body, candidates)
    make_agent(client, Store("sqlite://"), Spy(), InMemorySaver()).process_eml(
        make_eml(MCU, "RE", _loc(mapping, "PUR-ORD-2026-00002 FM-32F4 確認如期交貨。"), "erp-l"))
    assert Spy.seen and "85" not in Spy.seen[0] and '"rate"' not in Spy.seen[0]


def test_shortage_task_assigned_to_existing_buyer(site, monkeypatch):
    mock, rest, client, mapping = site
    import eta_agent.policy as pol
    monkeypatch.setattr(pol, "DEFAULT_ASSIGNEE", "buyer@demo.tw")
    ag = _agent(client)
    po5 = mapping["purchase_orders"]["PUR-ORD-2026-00005"]
    r = ag.process_eml(make_eml(ACME, "RE", f"{po5} AC-BOX1 共 1,000 pcs，11/16 先交 600 pcs 到貨，剩餘 400 pcs 待定。", "erp-s3"))
    client.workflow_action(r["case_id"], "Approve")
    ag.resume(r["thread_id"], "Administrator")
    [todo] = mock.db["ToDo"].values()
    assert todo["allocated_to"] == "buyer@demo.tw"


def test_product_days_live_in_erpnext_item(site):
    mock, rest, client, mapping = site
    assert {"Item-custom_production_days", "Item-custom_test_pack_ship_days"} <= set(mock.db["Custom Field"])
    item = client.get_item("GW-200")
    assert (item.production_days, item.test_pack_ship_days) == (5, 2)
    mock.db["Item"]["GW-200"]["custom_production_days"] = 7
    r = _agent(client).process_eml(make_eml(MCU, "RE", _loc(mapping, "PUR-ORD-2026-00002 FM-32F4 800 pcs 只能於 115/11/09 出貨。"), "erp-days"))
    assert any("預估延誤 5 個工作天" in x for x in r["reasons"])


def test_reseed_fills_missing_days_without_overwriting_edits(site):
    mock, rest, client, mapping = site
    gw = mock.db["Item"]["GW-200"]
    gw["custom_production_days"] = 7
    gw["custom_test_pack_ship_days"] = 0
    se.seed(rest, "示範製造股份有限公司", lambda m: None)
    assert (gw["custom_production_days"], gw["custom_test_pack_ship_days"]) == (7, 2)


def test_check_flags_product_without_days(site):
    mock, rest, client, mapping = site
    mock.db["Item"]["GW-200"]["custom_production_days"] = 0
    logs = []
    assert se.check(client, logs.append) is False
    assert any("❌ 成品 GW-200" in m for m in logs)


def test_cumulative_delay_on_erpnext(site):
    mock, rest, client, mapping = site
    ag = _agent(client)
    r1 = ag.process_eml(make_eml(ACME, "RE", _loc(mapping, "PUR-ORD-2026-00001 AC-C100 到貨日延後至 11/7。"), "erp-c1"))
    assert r1["result"]["status"] == "applied"
    r2 = ag.process_eml(make_eml(ACME, "RE", _loc(mapping, "PUR-ORD-2026-00001 AC-C100 到貨日延後至 11/9。"), "erp-c2"))
    assert r2["status"] == "waiting_review" and any("累計延後 4 天" in x for x in r2["reasons"])
    po = mock.db["Purchase Order"][mapping["purchase_orders"]["PUR-ORD-2026-00001"]]
    assert po["items"][0]["schedule_date"] == "2026-11-07"
