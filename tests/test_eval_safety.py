import json

from eta_agent.eval.generate import generate
from eta_agent.eval.run_eval import run


def test_no_wrong_auto_on_eval_set(tmp_path):
    assert generate(tmp_path / "emails") == 50
    report = run(tmp_path / "emails", tmp_path / "reports")
    rows = json.loads(report.with_suffix(".json").read_text(encoding="utf-8"))
    wrong = [r["id"] for r in rows if r["expected"] != "auto" and r["auto_applied"]]
    assert wrong == []
