import json

from eta_agent.erp.fake import FakeERP
from eta_agent.llm.context import build_llm_context

OTHER_SUPPLIERS_POS = ["PUR-ORD-2026-00001", "PUR-ORD-2026-00003", "PUR-ORD-2026-00004", "PUR-ORD-2026-00005"]
OTHER_PARTS = ["AC-C100", "AC-R10K", "HP-GW200-R2", "DC-USBC", "AC-BOX1", "CAP-100UF", "PCB-GW200"]


def test_only_senders_supplier_reaches_llm(agent, spy, eml):
    agent.process_eml(eml("delivery@formosa-mcu.com.tw", "RE", "PUR-ORD-2026-00002 FM-32F4 確認如期交貨。", "scope-1"))
    sent = json.dumps(spy.calls[0]["candidates"], ensure_ascii=False)
    assert {r["parent"] for r in spy.calls[0]["candidates"]["Purchase Order Item"]} == \
        {"PUR-ORD-2026-00002", "PUR-ORD-2026-00006"}
    for x in OTHER_SUPPLIERS_POS + OTHER_PARTS:
        assert x not in sent


def test_only_allowed_fields_reach_llm(agent, spy, eml):
    agent.process_eml(eml("delivery@formosa-mcu.com.tw", "RE", "PUR-ORD-2026-00002 FM-32F4 確認如期交貨。", "scope-2"))
    for row in spy.calls[0]["candidates"]["Purchase Order Item"]:
        assert set(row) == {"parent", "item_code", "supplier_part_no"}


def test_email_naming_another_suppliers_po_does_not_pull_its_data(agent, spy, eml):
    agent.process_eml(eml("delivery@formosa-mcu.com.tw", "RE", "PUR-ORD-2026-00001 AC-C100 延後至 11/7。", "scope-3"))
    parents = {r["parent"] for r in spy.calls[0]["candidates"]["Purchase Order Item"]}
    assert "PUR-ORD-2026-00001" not in parents


def test_foreign_record_returned_by_erp_is_dropped():
    erp = FakeERP()
    real = erp.open_po_lines
    erp.open_po_lines = lambda s: real(s) + real("SUP-ACME")
    lines, _, _, ctx, dropped = build_llm_context(erp, "SUP-MCU", "x")
    assert {l.supplier for l in lines} == {"SUP-MCU"}
    assert len(dropped) == 3 and all("SUP-ACME" in d for d in dropped)
    assert "AC-C100" not in json.dumps(ctx)


def test_unknown_sender_sends_nothing(agent, spy, eml):
    agent.process_eml(eml("someone@gmail.com", "RE", "PUR-ORD-2026-00002 FM-32F4", "scope-4"))
    assert spy.calls == []
