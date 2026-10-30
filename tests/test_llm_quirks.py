from eta_agent.models import ExtractedLine, Extraction
from datetime import date

from eta_agent.normalize import align_date_text, align_item_alias, clean_nulls

ACME, MCU, MANAGER = "sales@acme-passive.com.tw", "delivery@formosa-mcu.com.tw", "manager@example.com"


class Scripted:
    name = "scripted"

    def __init__(self, ext):
        self.ext = ext

    def extract(self, *a):
        return self.ext


def _agent(erp, ext):
    from langgraph.checkpoint.memory import InMemorySaver
    from eta_agent.service import make_agent
    from eta_agent.store import Store
    return make_agent(erp, Store("sqlite://"), Scripted(ext), InMemorySaver())


def test_model_translated_part_no_is_aligned_back(erp, eml):
    ext = Extraction(intent="date_change", confidence=1.0, lines=[ExtractedLine(
        po_number="PUR-ORD-2026-00002", item_code="MCU-STM32", qty_text="800 pcs", date_text="115/11/09",
        date_kind="ship", evidence="PUR-ORD-2026-00002 FM-32F4 800 pcs 只能於 115/11/09 出貨")])
    r = _agent(erp, ext).process_eml(eml(MCU, "RE", "PUR-ORD-2026-00002 FM-32F4 800 pcs 只能於 115/11/09 出貨。", "q1"))
    assert not any("不在信件原文" in x for x in r["reasons"])
    assert any("SO-0042" in x for x in r["reasons"])


def test_unrelated_item_code_is_not_aligned():
    x = ExtractedLine(item_code="CAP-100UF", evidence="x")
    y, note = align_item_alias(x, "PUR-ORD-2026-00002 FM-32F4", [("MCU-STM32", "FM-32F4"), ("CAP-100UF", "AC-C100")])
    assert y.item_code == "CAP-100UF" and note is None


def test_null_string_becomes_none():
    x = clean_nulls(ExtractedLine(item_code="null", date_text="null", date_kind="arrival", qty_text="400 pcs",
                                  evidence="剩餘 400 pcs 待定"))
    assert x.item_code is None and x.date_text is None and x.date_kind == "unknown" and x.qty_text == "400 pcs"


def test_split_with_null_strings_reaches_review_and_creates_task(erp, eml):
    body = "PUR-ORD-2026-00005 AC-BOX1 共 1,000 pcs，11/16 先交 600 pcs 到貨，剩餘 400 pcs 待定。"
    ext = Extraction(intent="split_delivery", confidence=1.0, lines=[
        ExtractedLine(po_number="PUR-ORD-2026-00005", item_code="ENC-BOX", qty_text="600 pcs", date_text="11/16",
                      date_kind="arrival", evidence="11/16 先交 600 pcs 到貨"),
        ExtractedLine(po_number="PUR-ORD-2026-00005", item_code="ENC-BOX", qty_text="400 pcs", date_text="null",
                      date_kind="unknown", evidence="剩餘 400 pcs 待定")])
    ag = _agent(erp, ext)
    r = ag.process_eml(eml(ACME, "RE", body, "q2"))
    assert r["status"] == "waiting_review"
    assert r["reasons"] == ["分批交貨一律人工審核"]
    erp.workflow_action(r["case_id"], "Approve", MANAGER)
    ag.resume(r["thread_id"], MANAGER)
    [t] = erp.open_shortage_tasks()
    assert t.qty == 400


def test_total_qty_listed_as_batch_is_still_blocked(erp, eml):
    body = "PUR-ORD-2026-00005 AC-BOX1 共 1,000 pcs，11/16 先交 600 pcs 到貨，剩餘 400 pcs 待定。"
    ext = Extraction(intent="split_delivery", confidence=1.0, lines=[
        ExtractedLine(po_number="PUR-ORD-2026-00005", item_code="AC-BOX1", qty_text="1,000 pcs", date_text="11/16",
                      date_kind="arrival", evidence="共 1,000 pcs，11/16 先交 600 pcs 到貨"),
        ExtractedLine(po_number="PUR-ORD-2026-00005", item_code="AC-BOX1", qty_text="400 pcs", date_text=None,
                      date_kind="unknown", evidence="剩餘 400 pcs 待定")])
    r = _agent(erp, ext).process_eml(eml(ACME, "RE", body, "q3"))
    assert r["route"] != "auto" and any("超過剩餘未交量" in x for x in r.get("reasons", []) + r.get("result", {}).get("reasons", []))
    assert erp.shortage_tasks == {}


def test_rewritten_date_is_aligned_back(erp, eml):
    body = "PUR-ORD-2026-00001 AC-R10K delivery is pushed out to Nov 6, sorry for the delay."
    ext = Extraction(intent="date_change", confidence=0.9, lines=[ExtractedLine(
        po_number="PUR-ORD-2026-00001", item_code="AC-R10K", date_text="11/06", date_kind="arrival",
        evidence="delivery is pushed out to Nov 6")])
    r = _agent(erp, ext).process_eml(eml(ACME, "RE", body, "q4"))
    assert r["route"] == "auto" and erp.po_lines["POI-0001-2"]["schedule_date"] == "2026-11-06"


def test_rewritten_date_with_different_day_is_not_aligned():
    x = ExtractedLine(date_text="11/07", date_kind="arrival", evidence="pushed out to Nov 6")
    y, note = align_date_text(x, "pushed out to Nov 6", date(2026, 10, 26))
    assert y.date_text == "11/07" and note is None


def test_ambiguous_rewritten_date_is_not_aligned():
    body = "原訂 11/6 到貨，改為 Nov 6 出貨"
    x = ExtractedLine(date_text="2026-11-06", date_kind="ship", evidence=body)
    y, note = align_date_text(x, body, date(2026, 10, 26))
    assert note is None
