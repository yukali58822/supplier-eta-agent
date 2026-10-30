import io
from datetime import date

import pytest
from fastapi.testclient import TestClient

from eta_agent.config import policy
from eta_agent.models import (Extraction, ImpactKind, ImpactResult, LineDiff, LineMatch, NormalizedLine,
                              POLine)
from eta_agent.policy import route

PL = POLine(po="P1", line="L1", supplier="S", item_code="X", qty=100, schedule_date=date(2026, 11, 10), rate=2.0)


def _route(intent="date_change", days=2, conf=0.9, impact=ImpactKind.NO_IMPACT, impact_conf="high",
           qty=None, price=None, issues=(), inferred=False):
    ext = Extraction(intent=intent, confidence=conf, lines=[])
    n = NormalizedLine(index=0, po_number="P1", item_code="X", qty=qty, unit_price=price, date_text="t",
                       date_kind="arrival", issues=list(issues))
    m = LineMatch(index=0, po_line=PL, inferred_po=inferred)
    eta = date.fromordinal(PL.schedule_date.toordinal() + days)
    d = LineDiff(index=0, po="P1", line="L1", item_code="X", original_date=PL.schedule_date, new_eta=eta,
                 date_diff_days=days, original_qty=100, remaining_qty=100, stated_qty=qty,
                 qty_diff=None if qty is None else qty - 100,
                 price_changed=None if price is None else price != 2.0)
    imp = ImpactResult(result=impact, confidence=impact_conf)
    return route(ext, [n], [], [m], [d], imp, policy())


def test_all_conditions_met_is_auto():
    assert _route().route == "auto"


@pytest.mark.parametrize("kw,needle", [
    ({"days": 3}, "延後 3 天，超過 2 天門檻"),
    ({"days": -3}, "提前 3 天"),
    ({"conf": 0.84}, "低於門檻"),
    ({"impact": ImpactKind.IMPACT_DETECTED}, ""),
    ({"impact": ImpactKind.UNKNOWN}, "unknown"),
    ({"impact_conf": "low"}, "信心為 low"),
    ({"qty": 90}, "數量 90"),
    ({"price": 2.5}, "單價與採購單不同"),
    ({"issues": ["日期不明確：月底"]}, "日期不明確"),
    ({"inferred": True}, "未寫採購單號"),
    ({"intent": "price_change"}, "單價變更一律人工審核"),
    ({"intent": "cancel_balance"}, "取消剩餘"),
    ({"intent": "split_delivery"}, "分批"),
    ({"intent": "confirm", "days": 1}, "確認信的日期"),
])
def test_any_failed_condition_goes_to_review(kw, needle):
    d = _route(**kw)
    assert d.route == "review"
    assert any(needle in r for r in d.reasons)


def test_other_intent_is_manual():
    assert _route(intent="other").route == "manual"


@pytest.fixture
def client(monkeypatch, agent):
    import eta_agent.api as api
    monkeypatch.setattr(api, "_agent", agent)
    monkeypatch.setenv("ALLOW_DEMO_AUTH", "1")
    return TestClient(api.app)


def test_resume_endpoint_authz(client, erp, eml):
    raw = eml("delivery@formosa-mcu.com.tw", "RE", "PUR-ORD-2026-00002 FM-32F4 800 pcs 只能於 115/11/09 出貨。", "api")
    r = client.post("/emails", files={"file": ("a.eml", io.BytesIO(raw))},
                    headers={"X-Demo-User": "buyer@example.com"})
    body = r.json()
    assert body["status"] == "waiting_review"
    tid = body["thread_id"]
    assert client.post("/resume", json={"thread_id": tid}).status_code == 401
    assert client.post("/resume", json={"thread_id": tid}, headers={"X-Demo-User": "manager@example.com"}).status_code == 409
    erp.workflow_action(body["case_id"], "Approve", "manager@example.com")
    assert client.post("/resume", json={"thread_id": tid}, headers={"X-Demo-User": "buyer@example.com"}).status_code == 403
    ok = client.post("/resume", json={"thread_id": tid}, headers={"X-Demo-User": "manager@example.com"})
    assert ok.status_code == 200 and ok.json()["status"] == "done"


def test_policy_rejects_unknown_settings(tmp_path, monkeypatch):
    import shutil

    from eta_agent import config
    for f in ("policy.yaml", "scheduling.yaml", "data_policy.yaml"):
        shutil.copy(config.CONFIG_DIR / f, tmp_path / f)
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    assert config.Policy.load().max_delay_days == 2
    p = tmp_path / "policy.yaml"
    original = p.read_text(encoding="utf-8")
    for bad in ("review_whole_email_if_any_line: true\n", "max_delay_day: 5\n",
                "shortage:\n  auto_close_when_ontime: true\n"):
        p.write_text(original.replace("shortage:\n  auto_close_when_on_time: true\n", "") + bad, encoding="utf-8")
        with pytest.raises(config.ConfigError, match="不認得"):
            config.Policy.load()


def test_scheduling_rejects_old_products_section_and_unknown_settings(tmp_path, monkeypatch):
    import shutil

    from eta_agent import config
    for f in ("policy.yaml", "scheduling.yaml", "data_policy.yaml"):
        shutil.copy(config.CONFIG_DIR / f, tmp_path / f)
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    assert config.Scheduling.load().default_product == {"production_days": 5, "test_pack_ship_days": 2}
    p = tmp_path / "scheduling.yaml"
    original = p.read_text(encoding="utf-8")
    p.write_text(original + "products:\n  GW-200:\n    production_days: 5\n", encoding="utf-8")
    with pytest.raises(config.ConfigError, match="ERPNext 料件主檔"):
        config.Scheduling.load()
    p.write_text(original + "prep_day: 2\n", encoding="utf-8")
    with pytest.raises(config.ConfigError, match="不認得"):
        config.Scheduling.load()
    p.write_text(original.replace("  test_pack_ship_days: 2\n", ""), encoding="utf-8")
    with pytest.raises(config.ConfigError, match="default_product"):
        config.Scheduling.load()
