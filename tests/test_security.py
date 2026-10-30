import json

import pytest

from eta_agent import config
from eta_agent.authz import (AccessDenied, assert_writable, authorize_and_project, llm_context_guard,
                             redact_for_log)
from eta_agent.config import ConfigError


def test_price_and_discount_never_reach_llm_context(agent, spy, eml):
    agent.process_eml(eml("delivery@formosa-mcu.com.tw", "RE: PUR-ORD-2026-00002",
                          "PUR-ORD-2026-00002 FM-32F4 800 pcs 確認如期交貨。", "sec-1"))
    sent = json.dumps(spy.calls, ensure_ascii=False)
    for forbidden in ('"rate"', '"qty"', "85.0", "discount", "valuation", "mobile"):
        assert forbidden not in sent


def test_pii_and_secrets_in_email_are_redacted_before_llm(agent, spy, eml):
    body = ("PUR-ORD-2026-00002 FM-32F4 800 pcs 確認如期交貨。\n"
            "聯絡人手機 0912-345-678，身分證 A123456789\npassword: hunter2\napi_key_ABCDEF1234567890")
    agent.process_eml(eml("delivery@formosa-mcu.com.tw", "RE", body, "sec-2"))
    sent = spy.calls[0]["body"]
    for s in ("0912-345-678", "A123456789", "hunter2", "ABCDEF1234567890"):
        assert s not in sent


def test_secrets_not_in_audit_log(agent, eml):
    agent.process_eml(eml("delivery@formosa-mcu.com.tw", "RE",
                          "PUR-ORD-2026-00002 FM-32F4 800 pcs 單價 USD 85 確認，手機 0912-345-678", "sec-3"))
    log = json.dumps(agent.store.audit_events(), ensure_ascii=False, default=str)
    assert "0912-345-678" not in log
    assert '"unit_price_text": "***"' in log


def test_unauthorized_field_request_is_denied():
    with pytest.raises(AccessDenied):
        authorize_and_project("Purchase Order Item", {"discount_amount": 5}, role="eta_agent",
                              purpose="match_po", fields=["discount_amount"])
    with pytest.raises(AccessDenied):
        authorize_and_project("Purchase Order Item", {"rate": 1}, role="eta_agent",
                              purpose="impact_analysis", fields=["rate"])


def test_undefined_field_role_or_doctype_is_denied():
    with pytest.raises(AccessDenied):
        authorize_and_project("Purchase Order Item", {"secret_x": 1}, role="eta_agent", purpose="match_po",
                              fields=["secret_x"])
    with pytest.raises(AccessDenied):
        authorize_and_project("Purchase Order", {"name": "x"}, role="intern", purpose="match_po")
    with pytest.raises(AccessDenied):
        authorize_and_project("Payroll", {"name": "x"}, role="eta_agent", purpose="match_po")
    with pytest.raises(AccessDenied):
        llm_context_guard("hi", {"Payroll": [{"salary": 1}]})


def test_projection_silently_drops_unlisted_fields():
    out = authorize_and_project("Purchase Order Item", {"name": "L1", "rate": 9, "secret": 1},
                                role="eta_agent", purpose="impact_analysis")
    assert out == {"name": "L1"}


def test_missing_policy_file_refuses(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    with pytest.raises(ConfigError):
        config.DataPolicy.load()


def test_writeback_outside_whitelist_rejected_client_and_server(erp):
    with pytest.raises(AccessDenied):
        assert_writable("Purchase Order Item", ["discount_amount"])
    res = erp.apply_change({"op_id": "x1", "message_id": "m", "mode": "auto", "case_id": None, "actions": [
        {"type": "update_po_line", "po": "PUR-ORD-2026-00001", "line": "POI-0001-1",
         "expected_version": erp.po_line_version("POI-0001-1"), "set": {"discount_amount": 0}}]})
    assert res["status"] == "rejected"


def test_auto_mode_cannot_change_price_or_qty(erp):
    res = erp.apply_change({"op_id": "x2", "message_id": "m", "mode": "auto", "case_id": None, "actions": [
        {"type": "update_po_line", "po": "PUR-ORD-2026-00001", "line": "POI-0001-1",
         "expected_version": erp.po_line_version("POI-0001-1"), "set": {"rate": 0.01}}]})
    assert res["status"] == "rejected" and erp.po_lines["POI-0001-1"]["rate"] == 1.2


def test_non_agent_caller_cannot_write(erp):
    erp.caller = "buyer@example.com"
    res = erp.apply_change({"op_id": "x3", "message_id": "m", "mode": "auto", "case_id": None, "actions": [
        {"type": "record_confirmation", "po": "PUR-ORD-2026-00001", "line": "POI-0001-1"}]})
    assert res["status"] == "rejected"


def test_redact_for_log():
    out = redact_for_log({"rate": 1, "nested": [{"api_token": "abc", "ok": "0912-345-678"}]})
    assert out["rate"] == "***" and out["nested"][0]["api_token"] == "***"
    assert "0912" not in out["nested"][0]["ok"]
