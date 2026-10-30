import eta_agent.erp as erp_pkg
from eta_agent import demo
from eta_agent.erp import setup_erpnext as se
from eta_agent.erp.erpnext import ERPNextClient
from eta_agent.erp.frappe_rest import FrappeREST
from eta_agent.llm.extractor import RuleBasedExtractor
from frappe_mock import FrappeMock


def test_demo_runs_end_to_end_on_erpnext(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(se, "MAP_PATH", tmp_path / "map.json")
    mock = FrappeMock(user="admin@demo.tw")
    mock.db["User"]["admin@demo.tw"] = {"name": "admin@demo.tw", "enabled": 1,
                                        "roles": [{"role": "System Manager"}, {"role": "Purchase Manager"}]}
    rest = FrappeREST("https://x", "k", "s", transport=mock.transport())
    se.setup(rest, lambda m: None)
    se.seed(rest, "示範製造股份有限公司", lambda m: None)
    monkeypatch.setattr(erp_pkg, "make_erp", lambda backend=None: ERPNextClient(
        "https://x", "k", "s", transport=mock.transport()))
    assert demo.run_demo(extractor=RuleBasedExtractor(), backend="erpnext") == 0
    out = capsys.readouterr().out
    assert "❌" not in out and "核准人：admin@demo.tw" in out
    assert out.count("狀態：done　分流：auto") == 2
    assert {c["workflow_state"] for c in mock.db["Supplier ETA Case"].values()} == {"Applied"}
    assert [t["status"] for t in mock.db["Shortage Task"].values()] == ["Closed"]
