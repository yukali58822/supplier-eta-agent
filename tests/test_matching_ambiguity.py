ACME = "sales@acme-passive.com.tw"


def add_line(erp, line, po, item_code, part_no, schedule, qty=1000.0, supplier="SUP-ACME"):
    base = dict(erp.po_lines["POI-0001-1"])
    base.update({"po": po, "line": line, "item_code": item_code, "supplier_part_no": part_no,
                 "schedule_date": schedule, "qty": qty, "received_qty": 0.0, "supplier": supplier})
    erp.po_lines[line] = base


def send(agent, body, mid):
    return agent.process_eml(_eml(body, mid))


def _eml(body, mid):
    from eta_agent.demo import make_eml
    return make_eml(ACME, "RE: 交期", body, mid)


def assert_untouched(erp, lines):
    for line, date in lines.items():
        assert erp.po_lines[line]["schedule_date"] == date, line


def reasons_of(r):
    return " ".join(r.get("reasons") or (r.get("result") or {}).get("reasons") or [])


def test_same_po_same_item_on_two_lines_is_not_guessed(agent, erp):
    add_line(erp, "POI-0001-3", "PUR-ORD-2026-00001", "CAP-100UF", "AC-C100", "2026-11-20")
    r = send(agent, "PUR-ORD-2026-00001 AC-C100 延後至 11/7 到貨。", "amb-1@x")
    assert r["status"] != "done"
    assert "找不到唯一明細（符合 2 筆）" in reasons_of(r)
    assert_untouched(erp, {"POI-0001-1": "2026-11-05", "POI-0001-3": "2026-11-20"})


def test_no_po_number_and_item_on_several_pos_is_not_guessed(agent, erp):
    add_line(erp, "POI-0007-1", "PUR-ORD-2026-00007", "CAP-100UF", "AC-C100", "2026-11-12")
    r = send(agent, "AC-C100 延後至 11/7 到貨。", "amb-2@x")
    assert r["status"] != "done"
    assert_untouched(erp, {"POI-0001-1": "2026-11-05", "POI-0007-1": "2026-11-12"})


def test_no_po_number_even_if_only_one_match_needs_a_person(agent, erp):
    r = send(agent, "AC-C100 2,000 pcs 延後至 11/7 到貨。", "amb-3@x")
    assert r["status"] == "waiting_review"
    assert "信中未寫採購單號（推定為 PUR-ORD-2026-00001），須人工確認" in reasons_of(r)
    assert_untouched(erp, {"POI-0001-1": "2026-11-05"})


def test_one_supplier_part_no_mapped_to_two_internal_items_is_not_guessed(agent, erp):
    add_line(erp, "POI-0001-4", "PUR-ORD-2026-00001", "CAP-100UF-T", "AC-C100", "2026-11-05")
    r = send(agent, "PUR-ORD-2026-00001 AC-C100 延後至 11/7 到貨。", "amb-4@x")
    assert r["status"] != "done"
    assert "符合 2 筆" in reasons_of(r)
    assert_untouched(erp, {"POI-0001-1": "2026-11-05", "POI-0001-4": "2026-11-05"})


def test_internal_item_code_picks_the_right_line_when_part_no_is_shared(agent, erp):
    add_line(erp, "POI-0001-4", "PUR-ORD-2026-00001", "CAP-100UF-T", "AC-C100", "2026-11-05")
    r = send(agent, "PUR-ORD-2026-00001 CAP-100UF 延後至 11/7 到貨。", "amb-5@x")
    assert [d["line"] for d in r["state"]["diffs"]] == ["POI-0001-1"]
    assert erp.po_lines["POI-0001-4"]["schedule_date"] == "2026-11-05"


def test_po_number_that_does_not_exist(agent, erp):
    r = send(agent, "PUR-ORD-2026-00099 AC-C100 延後至 11/7 到貨。", "amb-6@x")
    assert r["status"] != "done"
    assert "採購單 PUR-ORD-2026-00099 不存在、已結案或不屬於此供應商" in reasons_of(r)
    assert_untouched(erp, {"POI-0001-1": "2026-11-05"})


def test_item_not_on_that_po(agent, erp):
    r = send(agent, "PUR-ORD-2026-00001 AC-BOX1 延後至 11/7 到貨。", "amb-7@x")
    assert r["status"] != "done"
    assert "符合 0 筆" in reasons_of(r)
    assert_untouched(erp, {"POI-0001-1": "2026-11-05", "POI-0005-1": "2026-11-16"})


def test_fully_received_line_is_not_a_candidate_and_no_other_line_is_changed(agent, erp):
    erp.po_lines["POI-0001-1"]["received_qty"] = erp.po_lines["POI-0001-1"]["qty"]
    r = send(agent, "PUR-ORD-2026-00001 AC-C100 延後至 11/7 到貨。", "amb-8@x")
    assert r["status"] != "done"
    assert_untouched(erp, {"POI-0001-1": "2026-11-05", "POI-0001-2": "2026-11-05"})


def test_po_number_without_item_on_single_line_po_needs_a_person(agent, erp):
    r = agent.process_eml(__import__("eta_agent.demo", fromlist=["make_eml"]).make_eml(
        "delivery@formosa-mcu.com.tw", "RE", "PUR-ORD-2026-00002 延後至 11/5 到貨。", "amb-11@x"))
    assert r["status"] == "waiting_review"
    assert "信中沒有料號，推定為這張單唯一未結的明細 MCU-STM32，須人工確認" in reasons_of(r)
    assert erp.po_lines["POI-0002-1"]["schedule_date"] == "2026-11-04"


def test_one_ambiguous_line_holds_back_the_whole_email(agent, erp):
    add_line(erp, "POI-0001-3", "PUR-ORD-2026-00001", "CAP-100UF", "AC-C100", "2026-11-20")
    r = send(agent, "PUR-ORD-2026-00005 AC-BOX1 延後至 11/17 到貨。\nPUR-ORD-2026-00001 AC-C100 延後至 11/7 到貨。",
             "amb-9@x")
    assert r["status"] != "done"
    assert_untouched(erp, {"POI-0005-1": "2026-11-16", "POI-0001-1": "2026-11-05", "POI-0001-3": "2026-11-20"})


def test_lowercase_part_no_still_matches_exactly_one(agent, erp):
    r = send(agent, "PUR-ORD-2026-00001 ac-c100 延後至 11/7 到貨。", "amb-10@x")
    lines = [d["line"] for d in (r["state"].get("diffs") or [])]
    assert lines in ([], ["POI-0001-1"])
    assert erp.po_lines["POI-0001-2"]["schedule_date"] == "2026-11-05"
