import os
import re
import time

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver

from eta_agent.demo import make_eml
from eta_agent.erp import setup_erpnext as se
from eta_agent.erp.erpnext import ERPNextClient
from eta_agent.erp.fake import FakeERP
from eta_agent.erp.frappe_rest import FrappeREST
from eta_agent.llm.extractor import RuleBasedExtractor
from eta_agent.service import make_agent
from eta_agent.store import Store
from eta_agent.web.app import create_app
from eta_agent.web.auth import ERPNextLogin
from frappe_mock import FrappeMock

ACME, MCU = "sales@acme-passive.com.tw", "delivery@formosa-mcu.com.tw"
B_BODY = "PUR-ORD-2026-00002 FM-32F4 800 pcs 只能於 115/11/09 出貨，請見諒。"


def login(client, user):
    r = client.post("/login/demo", data={"user": user})
    assert r.status_code == 200 and r.url.path == "/"
    return re.search(r'name="csrf" (?:value|content)="([^"]+)"', r.text).group(1)


def deliver(client, raw, name=None):
    scanner = client.app.state.inbox
    scanner.min_age_seconds = 0
    scanner.ensure_folders()
    name = name or f"mail-{len(list(scanner.folder.rglob('*.eml')))}.eml"
    (scanner.folder / name).write_bytes(raw)
    return scanner.scan_once()


@pytest.fixture
def erp():
    return FakeERP()


@pytest.fixture
def agent(erp, tmp_path):
    return make_agent(erp, Store(f"sqlite:///{tmp_path}/agent.db"), RuleBasedExtractor(), InMemorySaver())


@pytest.fixture
def web(agent, tmp_path):
    return TestClient(create_app(agent_factory=lambda: agent, backend="fake", inbox=tmp_path / "inbox", watch=False))


def test_requires_login(web):
    assert web.get("/").url.path == "/login"
    assert web.get("/emails/x").url.path == "/login"


def test_demo_login_can_be_disabled(web, monkeypatch):
    monkeypatch.setenv("ALLOW_DEMO_AUTH", "0")
    assert web.post("/login/demo", data={"user": "manager@example.com"}).status_code == 403


def test_user_without_purchasing_role_is_refused(web, erp):
    erp.users["intern@example.com"] = {"Employee"}
    r = web.post("/login/demo", data={"user": "intern@example.com"})
    assert r.url.path == "/login" and "沒有採購相關角色" in r.text


def test_pending_list_puts_customer_delay_first(web):
    csrf = login(web, "manager@example.com")
    deliver(web, make_eml(MCU, "RE: PO6", "PUR-ORD-2026-00006 FM-32F4 到貨日延後至 11/30。", "w-c"))
    deliver(web, make_eml(MCU, "RE: PO2 交期", B_BODY, "w-b"))
    home = web.get("/").text
    pending = home.split('id="recent"')[0]
    assert pending.index("RE: PO2 交期") < pending.index("RE: PO6")
    assert "3 個工作天" in pending and "SO-0042" in pending


def test_duplicate_file_is_not_processed_twice(web, erp):
    raw = make_eml(ACME, "RE: 重複", "PUR-ORD-2026-00001 AC-C100 2,000 pcs 到貨日延後至 11/7。", "w-dup")
    assert [r.status for r in deliver(web, raw, "a.eml")] == ["done"]
    assert [r.status for r in deliver(web, raw, "a.eml")] == ["duplicate"]
    processed = list((web.app.state.inbox.processed_dir).rglob("*.eml"))
    assert sorted(p.name for p in processed) == ["a.eml", "a_1.eml"]
    login(web, "manager@example.com")
    assert web.get("/").text.count("RE: 重複") == 1


def test_detail_shows_highlighted_email_and_timeline(web):
    csrf = login(web, "manager@example.com")
    body = B_BODY + "\n<script>alert('x')</script>"
    deliver(web, make_eml(MCU, "RE: PO2", body, "w-detail"))
    page = web.get("/emails/w-detail").text
    assert "<mark>" in page and "115/11/09" in page
    assert "11/26（四）" in page and "客戶要 11/23（一）" in page and "預估延誤 3 個工作天" in page
    assert "<script>alert" not in page and "&lt;script&gt;" in page
    assert "核准並寫回 ERPNext" in page


def test_manager_approves_and_po_is_written_back(web, erp):
    csrf = login(web, "manager@example.com")
    deliver(web, make_eml(MCU, "RE", B_BODY, "w-ok"))
    r = web.post("/emails/w-ok/approve", data={"csrf": csrf})
    assert "已核准，並安全寫回 ERPNext" in r.text
    assert erp.po_lines["POI-0002-1"]["schedule_date"] == "2026-11-12"
    assert "已寫回 ERPNext 的修改" in r.text and "核准並寫回" not in r.text


def test_buyer_can_view_but_not_approve(web, erp, agent):
    agent.process_eml(make_eml(MCU, "RE", B_BODY, "w-buyer"))
    csrf = login(web, "buyer@example.com")
    page = web.get("/emails/w-buyer").text
    assert "等待採購主管決定" in page and "核准並寫回" not in page
    assert web.post("/emails/w-buyer/approve", data={"csrf": csrf}).status_code == 403
    assert erp.po_lines["POI-0002-1"]["schedule_date"] == "2026-11-04"


def test_csrf_token_required(web, erp):
    csrf = login(web, "manager@example.com")
    deliver(web, make_eml(MCU, "RE", B_BODY, "w-csrf"))
    assert web.post("/emails/w-csrf/approve", data={"csrf": "forged"}).status_code == 403
    assert erp.po_lines["POI-0002-1"]["schedule_date"] == "2026-11-04"


def test_reject_requires_reason_and_records_it(web, erp, agent):
    csrf = login(web, "manager@example.com")
    deliver(web, make_eml(MCU, "RE", B_BODY, "w-rej"))
    assert "拒絕時請填寫原因" in web.post("/emails/w-rej/reject", data={"csrf": csrf, "reason": " "}).text
    r = web.post("/emails/w-rej/reject", data={"csrf": csrf, "reason": "已協調改為 11/6"})
    assert "已拒絕，採購單沒有任何修改" in r.text
    assert erp.po_lines["POI-0002-1"]["schedule_date"] == "2026-11-04"
    ev = [e for e in agent.store.audit_events("w-rej") if e["event"] == "review_comment"]
    assert ev and ev[0]["actor"] == "manager@example.com" and ev[0]["detail"]["reason"] == "已協調改為 11/6"


def test_po_changed_before_approval_shows_expired_and_reprocess(web, erp):
    csrf = login(web, "manager@example.com")
    deliver(web, make_eml(MCU, "RE", B_BODY, "w-exp"))
    erp.external_edit_line("POI-0002-1", qty=700.0)
    r = web.post("/emails/w-exp/approve", data={"csrf": csrf})
    assert "核准已失效" in r.text and "重新分析" in r.text
    assert erp.po_lines["POI-0002-1"]["schedule_date"] == "2026-11-04"
    r = web.post("/emails/w-exp/reprocess", data={"csrf": csrf})
    assert "送主管審核" in r.text and "核准並寫回 ERPNext" in r.text


def test_buyer_does_not_see_unit_price_in_proposed_change(web, agent):
    agent.process_eml(make_eml(ACME, "RE", "PUR-ORD-2026-00001 AC-C100 因原物料上漲，單價調整為 NT$1.35，交期不變。", "w-price"))
    login(web, "buyer@example.com")
    buyer_page = web.get("/emails/w-price").text
    actions = lambda html: html.split("系統讀到的內容")[0]  # noqa: E731
    assert "1.35" not in actions(buyer_page).replace("NT$1.35", "")
    web2 = TestClient(web.app)
    login(web2, "manager@example.com")
    assert "單價：原單價 → 1.35" in web2.get("/emails/w-price").text


def test_pending_review_survives_restart(tmp_path, monkeypatch, erp):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/data/agent.db")
    box = tmp_path / "inbox"
    first = TestClient(create_app(agent_factory=lambda: make_agent(erp, extractor=RuleBasedExtractor()), backend="fake",
                                  inbox=box, watch=False))
    deliver(first, make_eml(MCU, "RE", B_BODY, "w-restart"))
    second = TestClient(create_app(agent_factory=lambda: make_agent(erp, extractor=RuleBasedExtractor()), backend="fake",
                                   inbox=box, watch=False))
    csrf = login(second, "manager@example.com")
    assert "RE" in second.get("/").text.split('id="recent"')[0]
    assert "已核准，並安全寫回" in second.post("/emails/w-restart/approve", data={"csrf": csrf}).text
    assert erp.po_lines["POI-0002-1"]["schedule_date"] == "2026-11-12"


URL = "https://demo.example.frappe.cloud"


@pytest.fixture
def site(tmp_path, monkeypatch):
    monkeypatch.setattr(se, "MAP_PATH", tmp_path / "erpnext_map.json")
    mock = FrappeMock()
    rest = FrappeREST(URL, "k", "s", transport=mock.transport())
    se.setup(rest, lambda m: None)
    mapping = se.seed(rest, "示範製造股份有限公司", lambda m: None)
    client = ERPNextClient(URL, "k", "s", transport=mock.transport())
    ag = make_agent(client, Store(f"sqlite:///{tmp_path}/agent.db"), RuleBasedExtractor(), InMemorySaver())
    web = TestClient(create_app(agent_factory=lambda: ag, backend="erpnext", inbox=tmp_path / "inbox", watch=False,
                                erp_login=ERPNextLogin(URL, transport=mock.transport())))
    po2 = mapping["purchase_orders"]["PUR-ORD-2026-00002"]
    return mock, web, ag, po2


def erp_login(web, usr, pwd):
    r = web.post("/login", data={"usr": usr, "pwd": pwd})
    m = re.search(r'name="csrf" (?:value|content)="([^"]+)"', r.text)
    return r, (m.group(1) if m else None)


def test_erpnext_login_checks_password(site):
    mock, web, ag, po2 = site
    r, _ = erp_login(web, "manager@demo.tw", "wrong")
    assert r.url.path == "/login" and "帳號或密碼錯誤" in r.text


def test_erpnext_approval_is_recorded_as_the_manager(site):
    mock, web, ag, po2 = site
    _, csrf = erp_login(web, "manager@demo.tw", "pw-manager")
    deliver(web, make_eml(MCU, "RE", B_BODY.replace("PUR-ORD-2026-00002", po2), "e-ok"))
    r = web.post("/emails/e-ok/approve", data={"csrf": csrf})
    assert "已核准，並安全寫回 ERPNext" in r.text
    [case] = mock.db["Supplier ETA Case"].values()
    assert case["workflow_state"] == "Applied"
    assert [v["owner"] for v in mock.db["Version"].values() if v["docname"] == case["name"]][0] == "manager@demo.tw"
    assert mock.db["Purchase Order"][po2]["items"][0]["schedule_date"] == "2026-11-12"


def test_erpnext_reject_adds_comment_on_case(site):
    mock, web, ag, po2 = site
    _, csrf = erp_login(web, "manager@demo.tw", "pw-manager")
    deliver(web, make_eml(MCU, "RE", B_BODY.replace("PUR-ORD-2026-00002", po2), "e-rej"))
    web.post("/emails/e-rej/reject", data={"csrf": csrf, "reason": "請先跟客戶確認"})
    [c] = mock.db["Comment"].values()
    assert c["content"] == "拒絕原因：請先跟客戶確認" and c["owner"] == "manager@demo.tw"


def test_erpnext_buyer_cannot_approve(site):
    mock, web, ag, po2 = site
    ag.process_eml(make_eml(MCU, "RE", B_BODY.replace("PUR-ORD-2026-00002", po2), "e-buyer"))
    _, csrf = erp_login(web, "buyer@demo.tw", "pw-buyer")
    assert web.post("/emails/e-buyer/approve", data={"csrf": csrf}).status_code == 403


def test_decision_made_in_erpnext_can_be_applied_from_web(site):
    mock, web, ag, po2 = site
    r = ag.process_eml(make_eml(MCU, "RE", B_BODY.replace("PUR-ORD-2026-00002", po2), "e-native"))
    mock.user = "manager@demo.tw"
    ag.erp.workflow_action(r["case_id"], "Approve")
    mock.user = "Administrator"
    _, csrf = erp_login(web, "manager@demo.tw", "pw-manager")
    assert "套用主管決定" in web.get("/emails/e-native").text
    assert "已核准，並安全寫回" in web.post("/emails/e-native/apply-decision", data={"csrf": csrf}).text


def test_web_has_no_manual_upload(web):
    login(web, "manager@example.com")
    assert web.post("/upload").status_code in (404, 405)
    home = web.get("/").text
    assert "收件資料夾" in home and "匯入並處理" not in home


def test_failed_email_moves_to_failed_folder_with_reason(web, agent):
    def boom(raw):
        raise RuntimeError("ERPNext 連不上")
    agent.process_eml = boom
    [r] = deliver(web, make_eml(MCU, "RE", B_BODY, "w-fail"), "bad.eml")
    box = web.app.state.inbox
    assert r.status == "failed" and (box.failed_dir / "bad.eml").exists()
    assert "ERPNext 連不上" in (box.failed_dir / "bad.eml.error.txt").read_text(encoding="utf-8")
    login(web, "manager@example.com")
    assert "1 封" in web.get("/").text.split('id="recent"')[0]


def test_file_still_being_copied_is_left_for_next_round(web):
    box = web.app.state.inbox
    box.ensure_folders()
    (box.folder / "copying.eml").write_bytes(make_eml(MCU, "RE", B_BODY, "w-copy"))
    box.min_age_seconds = 60
    assert box.scan_once() == [] and (box.folder / "copying.eml").exists()
    box.min_age_seconds = 0
    assert [r.status for r in box.scan_once()] == ["waiting_review"]


def test_web_server_checks_inbox_automatically(agent, tmp_path):
    box = tmp_path / "inbox"
    app = create_app(agent_factory=lambda: agent, backend="fake", inbox=box, watch=True, poll_seconds=0.2)
    app.state.inbox.min_age_seconds = 0
    with TestClient(app) as client:
        (box / "b.tmp").write_bytes(make_eml(MCU, "RE", B_BODY, "w-auto"))
        (box / "b.tmp").rename(box / "b.eml")
        for _ in range(150):
            rec = agent.store.get_message("w-auto")
            if rec and rec["status"] != "processing":
                break
            time.sleep(0.1)
        assert agent.store.get_message("w-auto")["status"] == "waiting_review"
        assert not (box / "b.eml").exists()
        login(client, "manager@example.com")
        assert "從收件資料夾讀取" in client.get("/emails/w-auto").text


def test_cli_inbox_command(tmp_path, monkeypatch, capsys):
    from eta_agent.cli import main
    monkeypatch.setenv("INBOX_DIR", str(tmp_path / "inbox"))
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/data/agent.db")
    monkeypatch.setenv("ERP_BACKEND", "fake")
    (tmp_path / "inbox").mkdir()
    (tmp_path / "inbox" / "b.eml").write_bytes(make_eml(MCU, "RE", B_BODY, "w-cli"))
    assert main(["inbox"]) == 0
    assert "b.eml：已送主管審核" in capsys.readouterr().out
    assert list((tmp_path / "inbox" / "processed").rglob("b.eml"))


def test_llm_failure_shows_reason_and_can_be_reprocessed(erp, tmp_path):
    class Flaky(RuleBasedExtractor):
        name = "flaky"
        broken = True
        last_error = None

        def extract(self, subject, body, candidates):
            if Flaky.broken:
                self.last_error = "ConnectError: 連不上 Ollama（Connection refused）"
                return None
            return super().extract(subject, body, candidates)
    ag = make_agent(erp, Store(f"sqlite:///{tmp_path}/agent.db"), Flaky(), InMemorySaver())
    client = TestClient(create_app(agent_factory=lambda: ag, backend="fake", inbox=tmp_path / "inbox", watch=False))
    deliver(client, make_eml(MCU, "RE", B_BODY, "w-flaky"))
    csrf = login(client, "manager@example.com")
    page = client.get("/emails/w-flaky").text
    assert "連不上 Ollama" in page and "重新分析這封信" in page and "讀信失敗" in page
    Flaky.broken = False
    r = client.post("/emails/w-flaky/reprocess", data={"csrf": csrf})
    assert "已重新分析，並送主管審核" in r.text and "核准並寫回 ERPNext" in r.text


def _interrupt(store, raw):
    from datetime import datetime, timezone

    from eta_agent.email_ingest import parse_eml
    from eta_agent.service import thread_for
    em = parse_eml(raw)
    store.claim_message(em.message_id, thread_for(em.message_id))
    store.save_email(em.message_id, raw, sender=em.sender, subject=em.subject, sent_at=em.sent_at, body=em.body)
    with store.engine.begin() as c:
        from sqlalchemy import update

        from eta_agent.store import messages
        c.execute(update(messages).where(messages.c.message_id == em.message_id)
                  .values(updated_at=datetime(2026, 10, 29, 7, 10, 22, tzinfo=timezone.utc)))
    return em.message_id


def test_interrupted_email_put_back_in_inbox_is_reprocessed_not_duplicate(erp, tmp_path):
    store = Store(f"sqlite:///{tmp_path}/agent.db")
    raw = make_eml(MCU, "RE: PUR-ORD-2026-00002", B_BODY, "cut-1@x")
    mid = _interrupt(store, raw)
    ag = make_agent(erp, store, RuleBasedExtractor(), InMemorySaver())
    web = TestClient(create_app(agent_factory=lambda: ag, backend="fake", inbox=tmp_path / "inbox", watch=False))
    res = deliver(web, raw)
    assert [r.status for r in res] == ["waiting_review"]
    assert store.get_message(mid)["status"] == "waiting_review"
    events = [e["event"] for e in store.audit_events(mid)]
    assert "interrupted" in events and "duplicate_ignored" not in events


def test_interrupted_email_already_moved_is_recovered_on_first_scan(erp, tmp_path):
    store = Store(f"sqlite:///{tmp_path}/agent.db")
    mid = _interrupt(store, make_eml(MCU, "RE: PUR-ORD-2026-00002", B_BODY, "cut-2@x"))
    ag = make_agent(erp, store, RuleBasedExtractor(), InMemorySaver())
    web = TestClient(create_app(agent_factory=lambda: ag, backend="fake", inbox=tmp_path / "inbox", watch=False))
    login(web, "manager@example.com")
    page = web.get(f"/emails/{mid}").text
    assert "處理中斷" in page and "處理到一半中斷了" in page and "/reprocess" in page
    home = web.get("/").text
    assert "處理中斷" in home and "10/29 15:10" in home
    web.app.state.inbox.scan_once()
    assert store.get_message(mid)["status"] == "waiting_review"
    web.app.state.inbox.scan_once()
    assert sum(e["event"] == "reprocess" for e in store.audit_events(mid)) == 1


def test_email_being_processed_now_is_not_called_interrupted(agent, tmp_path):
    raw = make_eml(MCU, "RE: PUR-ORD-2026-00002", B_BODY, "busy@x")
    agent.process_eml(raw)
    rec = agent.store.get_message("busy@x")
    agent.store.set_status("busy@x", "processing")
    assert not agent.is_interrupted(agent.store.get_message("busy@x"))
    agent._running.add("busy@x")
    with pytest.raises(RuntimeError, match="正在處理中"):
        agent.reprocess(raw)
    assert rec is not None


class FlakyExtractor(RuleBasedExtractor):
    def __init__(self, fail):
        super().__init__()
        self.fail, self.last_error = fail, None

    def extract(self, *a, **kw):
        if self.fail > 0:
            self.fail -= 1
            self.last_error = "連不上 Ollama"
            return None
        return super().extract(*a, **kw)


def test_llm_failure_is_retried_automatically(erp, tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone
    monkeypatch.setenv("AUTO_RETRY_MINUTES", "1,5")
    ag = make_agent(erp, Store(f"sqlite:///{tmp_path}/agent.db"), FlakyExtractor(fail=1), InMemorySaver())
    raw = make_eml(MCU, "RE: PUR-ORD-2026-00002", B_BODY, "flaky@x")
    assert ag.process_eml(raw)["status"] == "manual"
    info = ag.retry_info(ag.store.get_message("flaky@x"))
    assert info["attempts"] == 0 and info["max"] == 2 and "連不上 Ollama" in info["reason"]
    now = datetime.now(timezone.utc)
    assert ag.retry_failed(now) == []
    res = ag.retry_failed(now + timedelta(minutes=2))
    assert [r["status"] for r in res] == ["waiting_review"]
    assert ag.retry_info(ag.store.get_message("flaky@x")) is None


def test_program_error_is_retried_then_gives_up(erp, agent, monkeypatch):
    from datetime import datetime, timedelta, timezone
    agent.retry_delays = [timedelta(minutes=1), timedelta(minutes=5)]
    monkeypatch.setattr(erp, "find_supplier_by_email", lambda *_: (_ for _ in ()).throw(RuntimeError("ERPNext 沒回應")))
    raw = make_eml(MCU, "RE: PUR-ORD-2026-00002", B_BODY, "boom@x")
    with pytest.raises(RuntimeError):
        agent.process_eml(raw)
    later = datetime.now(timezone.utc) + timedelta(hours=1)
    for _ in range(4):
        agent.retry_failed(later)
    events = [e["event"] for e in agent.store.audit_events("boom@x")]
    assert events.count("auto_retry") == 2
    info = agent.retry_info(agent.store.get_message("boom@x"))
    assert info["next_at"] is None and "ERPNext 沒回應" in info["reason"]


def test_failed_page_shows_reason_and_retry_schedule(erp, agent, web, monkeypatch):
    monkeypatch.setattr(erp, "find_supplier_by_email", lambda *_: (_ for _ in ()).throw(RuntimeError("ERPNext 沒回應")))
    deliver(web, make_eml(MCU, "RE: PUR-ORD-2026-00002", B_BODY, "boom2@x"))
    login(web, "manager@example.com")
    page = web.get("/emails/boom2@x").text
    assert "失敗原因" in page and "ERPNext 沒回應" in page and "系統會自動重試" in page
    assert "停止自動處理" not in page and "10/26（一）" in page
    assert "/reprocess" in page and "為什麼需要你審核" not in page


def test_manual_case_that_retrying_cannot_fix_is_not_retried(agent):
    from datetime import datetime, timedelta, timezone
    raw = make_eml(MCU, "RE: 交期", "PUR-ORD-2099-99999 FM-32F4 只能於 115/11/09 出貨。", "nopo@x")
    agent.process_eml(raw)
    assert agent.store.get_message("nopo@x")["status"] in ("manual", "waiting_review")
    assert agent.retry_info(agent.store.get_message("nopo@x")) is None
    assert agent.retry_failed(datetime.now(timezone.utc) + timedelta(days=1)) == []


def test_page_shows_quote_and_attachment_separately_and_escapes_html(web):
    from email.message import EmailMessage
    m = EmailMessage()
    m["From"], m["Subject"], m["Date"], m["Message-ID"] = ACME, "RE: 交期", "Mon, 26 Oct 2026 10:00:00 +0800", "<show@x>"
    m.set_content("PUR-ORD-2026-00001 AC-C100 2,000 pcs 延後至 11/7 到貨。<script>alert(1)</script>\n\n"
                  "-----Original Message-----\nFrom: purchasing@example.com\n\n請問 AC-R10K 能否提前至 11/1？")
    m.add_attachment(b"%PDF", maintype="application", subtype="pdf", filename="交期表.pdf")
    deliver(web, m.as_bytes())
    login(web, "manager@example.com")
    page = web.get("/emails/show@x").text
    assert "<script>alert(1)</script>" not in page and "&lt;script&gt;" in page
    assert "引用的舊信" in page and "AC-R10K" in page
    assert "交期表.pdf" in page and "系統不讀附件內容" in page
    assert "系統沒有讀附件內容" in page


def test_impact_label_separates_production_delay_from_customer_delay(web):
    deliver(web, make_eml(MCU, "RE: PUR-ORD-2026-00002", B_BODY, "imp-cust@x"))
    deliver(web, make_eml("pcb@hc-pcb.com.tw", "RE: PUR-ORD-2026-00003",
                          "PUR-ORD-2026-00003 HP-GW200-R2 到貨日延後至 11/9。", "imp-prod@x"))
    login(web, "manager@example.com")
    cust, prod = web.get("/emails/imp-cust@x").text, web.get("/emails/imp-prod@x").text
    assert "客戶訂單會延誤" in cust and "影響生產排程" not in cust
    assert "影響生產排程（客戶交期趕得上）" in prod and "客戶訂單會延誤" not in prod
    home = web.get("/").text
    assert "客戶訂單會延誤" in home and "影響生產排程（客戶交期趕得上）" in home


def test_page_for_email_processed_by_older_version_still_renders(web, agent):
    deliver(web, make_eml(MCU, "RE: PUR-ORD-2026-00002", B_BODY, "old-ver@x"))
    cfg = {"configurable": {"thread_id": agent.store.get_message("old-ver@x")["thread_id"]}}
    diffs = agent.graph.get_state(cfg).values["diffs"]
    for d in diffs:
        d.pop("cumulative_diff_days", None)
        d.pop("baseline_date", None)
    agent.graph.update_state(cfg, {"diffs": diffs})
    login(web, "manager@example.com")
    r = web.get("/emails/old-ver@x")
    assert r.status_code == 200 and "晚 8 天" in r.text and "累計" not in r.text
