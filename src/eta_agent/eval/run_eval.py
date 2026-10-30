from __future__ import annotations

import json
import os
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver

from ..config import settings
from ..erp.fake import FakeERP
from ..llm.extractor import make_extractor
from ..service import make_agent
from ..store import Store

TARGETS = {"intent": 0.95, "date": 0.95, "qty": 0.95, "price": 0.95, "match": 0.97, "impact": 1.0,
           "shortage": 0.95}


def _ratio(ok: int, n: int) -> float | None:
    return ok / n if n else None


def _fmt(v: float | None) -> str:
    return "—" if v is None else f"{v * 100:.1f}%"


def evaluate_case(truth: dict[str, Any], raw: bytes, extractor: Any) -> dict[str, Any]:
    erp = FakeERP()
    for t in truth.get("setup", {}).get("shortage_tasks", []):
        erp.shortage_tasks[t["name"]] = dict(t)
    agent = make_agent(erp, Store("sqlite://"), extractor, InMemorySaver())
    try:
        res = agent.process_eml(raw)
    except Exception as e:  # noqa: BLE001
        return {"id": truth["id"], "error": f"{type(e).__name__}: {e}", "route": "error", "status": "error",
                "auto_applied": False}
    st = res.get("state", {})
    route = res.get("route")
    applied = (res.get("result") or {}).get("status") in ("applied", "already_applied") and route == "auto"
    diffs = st.get("diffs", [])
    norm = st.get("normalized", [])
    impact = (st.get("impact") or {}).get("result")
    closed = [t for t, v in erp.shortage_tasks.items() if v["status"] == "Closed"]
    return {
        "id": truth["id"], "status": res["status"], "route": route, "auto_applied": applied,
        "intent": (st.get("extraction") or {}).get("intent"), "diffs": diffs, "normalized": norm,
        "matches": st.get("matches", []), "impact": impact,
        "reasons": (st.get("decision") or {}).get("reasons", []), "shortage_closed": bool(closed),
    }


def score(truth: dict[str, Any], got: dict[str, Any]) -> dict[str, tuple[int, int]]:
    s: dict[str, tuple[int, int]] = {}
    if truth["intent"] is not None:
        s["intent"] = (int(got.get("intent") == truth["intent"]), 1)
    tl = truth["lines"]
    if tl:
        matched = [m["po_line"]["line"] if m.get("po_line") else None for m in got.get("matches", [])]
        exp = [x["line"] for x in tl]
        s["match"] = (int(matched == exp), 1)
        by_idx = {d["index"]: d for d in got.get("diffs", [])}
        norm = got.get("normalized", [])
        for metric, key in (("date", "eta"), ("qty", "qty"), ("price", "price")):
            ok = n = 0
            for i, x in enumerate(tl):
                if x[key] is None and not (metric == "date" and truth["route"] != "auto"):
                    continue
                n += 1
                if i >= len(norm):
                    continue
                if metric == "date":
                    d = by_idx.get(i)
                    actual = (d or {}).get("new_eta") if d else norm[i].get("eta")
                    ok += int(actual == x["eta"])
                elif metric == "qty":
                    ok += int(norm[i].get("qty") == x["qty"])
                else:
                    ok += int(norm[i].get("unit_price") == x["price"])
            if n:
                s[metric] = (ok, n)
    if truth["impact"] is not None:
        s["impact"] = (int(got.get("impact") == truth["impact"]), 1)
    if truth["shortage_close"] is not None:
        s["shortage"] = (int(got.get("shortage_closed") == truth["shortage_close"]), 1)
    s["route"] = (int(got.get("route") == truth["route"] or
                      (truth["route"] == "review" and got.get("route") in ("review", "manual") and not got["auto_applied"])), 1)
    return s


def run(data: Path, out: Path, limit: int | None = None) -> Path:
    s = settings()
    truth_all = json.loads((data / "ground_truth.json").read_text(encoding="utf-8"))[:limit]
    extractor = make_extractor(s.extractor, s.llm_model, base_url=s.ollama_base_url)
    print(f"擷取器：{extractor.name}" + ("（規則式離線基準；要用 Ollama 請在 .env 設 EXTRACTOR=llm 或加 --model）"
                                       if extractor.name.startswith("rule") else ""), flush=True)
    rows = []
    totals: dict[str, list[int]] = {}
    for t in truth_all:
        got = evaluate_case(t, (data / f"{t['id']}.eml").read_bytes(), extractor)
        print(f"  {t['id']}: 預期 {t['route']:<6} 實際 {got.get('route')}", flush=True)
        sc = score(t, got) if "error" not in got else {"route": (0, 1)}
        for k, (ok, n) in sc.items():
            totals.setdefault(k, [0, 0])
            totals[k][0] += ok
            totals[k][1] += n
        rows.append((t, got, sc))

    must_review = [r for r in rows if r[0]["route"] != "auto"]
    wrong_auto = [r for r in must_review if r[1]["auto_applied"]]
    expected_auto = [r for r in rows if r[0]["route"] == "auto"]
    auto_ok = [r for r in rows if r[1]["auto_applied"]]
    missed_auto = [r for r in expected_auto if not r[1]["auto_applied"]]
    stopped = [r for r in rows if r[1]["route"] in ("stop",) or r[1]["status"] in ("error", "failed")]

    lines = [f"# 評估報告（指定測試集實測結果）", "",
             f"- 產生時間：{datetime.now():%Y-%m-%d %H:%M}",
             f"- 擷取器：`{extractor.name}`" + ("（規則式離線基準，不代表 LLM 實力）" if extractor.name.startswith("rule") else ""),
             f"- 測試信：{len(rows)} 封（{dict(Counter(r[0]['category'] for r in rows))}）",
             "- 說明：50 封信只能證明指定案例的結果，不能保證真實環境永不出錯。", "",
             "## 安全指標", "",
             "| 項目 | 數值 |", "| --- | --- |",
             f"| **錯誤自動處理率**（必須人工審核卻被自動套用） | **{len(wrong_auto)} / {len(must_review)}"
             f" = {_fmt(_ratio(len(wrong_auto), len(must_review)))}**（目標 0%） |",
             f"| 必須人工審核件數 | {len(must_review)} |",
             f"| 成功自動處理件數 | {len(auto_ok)} |",
             f"| 自動處理比例（成功自動 ÷ 全部） | {_fmt(_ratio(len(auto_ok), len(rows)))} |",
             f"| 應自動卻轉人工（過度保守） | {len(missed_auto)} / {len(expected_auto)} |",
             f"| 因資料不足 / 來源不明 / 系統錯誤而停止 | {len(stopped)} |", "",
             "## 準確率", "", "| 指標 | 結果 | 目標 | 達成 |", "| --- | --- | --- | --- |"]
    names = {"intent": "意圖", "date": "日期欄位", "qty": "數量欄位", "price": "單價欄位", "match": "明細對應",
             "impact": "影響分析", "shortage": "補料結案", "route": "分流（參考）"}
    for k in ("intent", "date", "qty", "price", "match", "impact", "shortage", "route"):
        if k in totals:
            ok, n = totals[k]
            r = _ratio(ok, n)
            tgt = TARGETS.get(k)
            lines.append(f"| {names[k]} | {ok}/{n} = {_fmt(r)} | {_fmt(tgt) if tgt else '—'} | "
                         f"{'—' if tgt is None else ('✅' if r is not None and r >= tgt else '❌')} |")
    lines += ["", "## 分流分布", "", "| 預期 \\ 實際 | " + " | ".join(["auto", "review", "manual", "stop"]) + " |",
              "| --- | --- | --- | --- | --- |"]
    for exp in ("auto", "review", "manual", "stop"):
        c = Counter(r[1]["route"] for r in rows if r[0]["route"] == exp)
        lines.append(f"| {exp} | " + " | ".join(str(c.get(x, 0)) for x in ("auto", "review", "manual", "stop")) + " |")

    failures = [r for r in rows if any(ok < n for ok, n in r[2].values())]
    lines += ["", f"## 錯誤案例（{len(failures)}）", ""]
    if not failures:
        lines.append("無。")
    for t, g, sc in failures:
        bad = [k for k, (ok, n) in sc.items() if ok < n]
        lines += [f"### {t['id']}（{t['category']}）— 不符：{', '.join(bad)}", "",
                  "```", t["body"].strip(), "```", "",
                  f"- 預期：intent=`{t['intent']}` route=`{t['route']}` impact=`{t['impact']}` "
                  f"lines={[ (x['line'], x['eta'], x['qty'], x['price']) for x in t['lines']]}",
                  f"- 實際：intent=`{g.get('intent')}` route=`{g.get('route')}` impact=`{g.get('impact')}` "
                  f"matched={[m['po_line']['line'] if m.get('po_line') else None for m in g.get('matches', [])]} "
                  f"eta={[n.get('eta') for n in g.get('normalized', [])]} qty={[n.get('qty') for n in g.get('normalized', [])]}",
                  f"- 標準答案推導：{t.get('why') or '—'}",
                  f"- 系統判斷理由：{'；'.join(g.get('reasons', [])) or g.get('error', '—')}", ""]
    lines += ["## 全部案例", "", "| ID | 類別 | 預期 | 實際 | 自動套用 | 影響(預期/實際) |", "| --- | --- | --- | --- | --- | --- |"]
    for t, g, _ in rows:
        lines.append(f"| {t['id']} | {t['category']} | {t['route']} | {g.get('route')} | "
                     f"{'是' if g['auto_applied'] else ''} | {t['impact'] or '—'} / {g.get('impact') or '—'} |")
    lines += ["", "> 人工修正過的案件可加入回歸測試集；本報告不會自動修改 Prompt 或分流規則。"]

    out.mkdir(parents=True, exist_ok=True)
    path = out / f"eval_{datetime.now():%Y%m%d}.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    (out / f"eval_{datetime.now():%Y%m%d}.json").write_text(json.dumps(
        [{"id": t["id"], "expected": t["route"], "got": g.get("route"), "auto_applied": g["auto_applied"],
          "score": sc} for t, g, sc in rows], ensure_ascii=False, indent=1), encoding="utf-8")
    return path
