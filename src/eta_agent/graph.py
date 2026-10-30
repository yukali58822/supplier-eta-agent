from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import date
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from .authz import AccessDenied, llm_context_guard
from .config import Policy, Scheduling
from .diff import compute_diffs, shortage_scenario, supply_scenario
from .email_ingest import InvalidEmail, parse_eml
from .impact import assess_impact as _assess, gather_snapshot
from .llm.context import build_llm_context
from .llm.extractor import Extractor
from .matching import match_lines
from .models import (CaseStatus, Extraction, ImpactResult, LineDiff, LineMatch, NormalizedLine,
                     ReviewCase, RouteDecision, Supplier)
from .normalize import align_date_text, align_item_alias, clean_nulls, evidence_consistent, normalize_line
from .policy import build_actions, route
from .store import Store
from .writeback import apply_actions


class AgentState(TypedDict, total=False):
    raw_eml_b64: str
    message_id: str
    sender: str
    subject: str
    email_date: str
    body: str
    attachments: list[str]
    supplier: dict
    extractor: str
    extraction: dict
    evidence_problems: list[str]
    normalized: list[dict]
    assumptions: list[str]
    matches: list[dict]
    diffs: list[dict]
    versions: dict[str, str]
    impact: dict
    decision: dict
    actions: list[dict]
    case_id: str
    review_status: str
    result: dict
    stop_reason: str


@dataclass
class Deps:
    erp: Any
    store: Store
    extractor: Extractor
    policy: Policy
    scheduling: Scheduling


def _dump(models: list[Any]) -> list[dict]:
    return [m.model_dump(mode="json") for m in models]


def build_graph(deps: Deps, checkpointer: Any = None):
    erp, store, pol, sched = deps.erp, deps.store, deps.policy, deps.scheduling

    def ingest_email(s: AgentState) -> dict:
        try:
            em = parse_eml(base64.b64decode(s["raw_eml_b64"]))
        except InvalidEmail as e:
            return {"stop_reason": f"信件無效：{e}", "message_id": s.get("message_id", "invalid")}
        out: dict[str, Any] = {"message_id": em.message_id, "sender": em.sender, "subject": em.subject,
                               "email_date": em.local_date.isoformat(), "body": em.body,
                               "attachments": list(em.attachments)}
        sup = erp.find_supplier_by_email(em.sender)
        if sup is None:
            out["stop_reason"] = f"寄件者 {em.sender} 不是 ERPNext 已登記的供應商信箱，停止自動處理"
        else:
            out["supplier"] = sup.model_dump(mode="json")
            if not em.body:
                out["stop_reason"] = "信裡只有引用的舊信，沒有供應商新寫的內容，停止自動處理"
        store.audit(em.message_id, "ingested", {"sender": em.sender, "subject": em.subject,
                                                 "supplier": sup.name if sup else None})
        return out

    def read_email(s: AgentState) -> dict:
        sup = Supplier(**s["supplier"])
        try:
            lines, tasks, body, ctx, dropped = build_llm_context(erp, sup.name, s["body"])
        except AccessDenied as e:
            return {"stop_reason": f"授權失敗：{e}"}
        if dropped:
            store.audit(s["message_id"], "context_dropped", {"dropped": dropped})
        ext = deps.extractor.extract(s["subject"], body, ctx)
        if ext is None:
            why = getattr(deps.extractor, "last_error", None) or "原因不明"
            store.audit(s["message_id"], "extract_failed", {"extractor": deps.extractor.name, "error": why})
            return {"stop_reason": f"LLM 擷取失敗（已重試一次）：{why}", "extractor": deps.extractor.name}
        full = f"{s['subject']}\n{body}"
        aliases = [(l.item_code, l.supplier_part_no) for l in lines]
        fixed, fixes = [], []
        for x in ext.lines:
            x = clean_nulls(x)
            x, note = align_item_alias(x, full, aliases)
            if note:
                fixes.append(note)
            x, note = align_date_text(x, full, date.fromisoformat(s["email_date"]))
            if note:
                fixes.append(note)
            fixed.append(x)
        ext = ext.model_copy(update={"lines": fixed})
        problems = []
        for x in ext.lines:
            problems += evidence_consistent(x, full)
        store.audit(s["message_id"], "extracted", {"extractor": deps.extractor.name,
                                                   "extraction": ext.model_dump(mode="json"), "fixes": fixes})
        return {"extraction": ext.model_dump(mode="json"), "evidence_problems": problems,
                "extractor": deps.extractor.name}

    def normalize(s: AgentState) -> dict:
        ext = Extraction(**s["extraction"])
        sup = Supplier(**s["supplier"])
        ref = date.fromisoformat(s["email_date"])
        out, assumptions = [], []
        for i, x in enumerate(ext.lines):
            n, a = normalize_line(i, x, ref, sup.transit_days, sched.default_transit_days)
            out.append(n)
            assumptions += a
        return {"normalized": _dump(out), "assumptions": list(dict.fromkeys(assumptions))}

    def match_po_lines(s: AgentState) -> dict:
        sup = Supplier(**s["supplier"])
        norm = [NormalizedLine(**n) for n in s["normalized"]]
        matches = match_lines(norm, erp.open_po_lines(sup.name), erp.open_shortage_tasks(sup.name))
        return {"matches": _dump(matches)}

    def compute_diff(s: AgentState) -> dict:
        ext = Extraction(**s["extraction"])
        norm = [NormalizedLine(**n) for n in s["normalized"]]
        matches = [LineMatch(**m) for m in s["matches"]]
        diffs = compute_diffs(norm, matches, ext.intent)
        for i, d in enumerate(diffs):
            if d.shortage_task or d.new_eta is None:
                continue
            base = store.schedule_baseline(d.line, d.original_date)
            if base and base != d.original_date:
                diffs[i] = d.model_copy(update={"baseline_date": base,
                                                "cumulative_diff_days": (d.new_eta - base).days})
        versions = {m.po_line.line: m.po_line.po_modified for m in matches if m.po_line}
        return {"diffs": _dump(diffs), "versions": versions}

    def assess_impact(s: AgentState) -> dict:
        ext = Extraction(**s["extraction"])
        diffs = [LineDiff(**d) for d in s["diffs"]]
        if ext.intent == "other" or not diffs:
            return {}
        items = sorted({d.item_code for d in diffs})
        try:
            snap = gather_snapshot(erp, items, sched)
            res = _assess(snap, sched, supply_scenario(diffs, ext.intent), shortage_scenario(diffs), items,
                          s.get("assumptions", []))
        except Exception as e:  # noqa: BLE001
            res = ImpactResult(result="unknown", confidence="low", reasons=[f"讀取資料失敗：{type(e).__name__}"])
        store.audit(s["message_id"], "impact", res.model_dump(mode="json"))
        return {"impact": res.model_dump(mode="json")}

    def route_by_policy(s: AgentState) -> dict:
        ext = Extraction(**s["extraction"])
        norm = [NormalizedLine(**n) for n in s["normalized"]]
        matches = [LineMatch(**m) for m in s["matches"]]
        diffs = [LineDiff(**d) for d in s["diffs"]]
        impact = ImpactResult(**s["impact"]) if s.get("impact") else None
        decision = route(ext, norm, s.get("evidence_problems", []), matches, diffs, impact, pol)
        if s.get("attachments") and decision.route in ("auto", "review"):
            note = f"信件附有檔案（{'、'.join(s['attachments'])}），系統沒有讀附件內容，請確認附件沒有其他變更"
            decision = RouteDecision(route="review", reasons=decision.reasons + [note])
        actions = [] if decision.route in ("manual", "stop") else \
            build_actions(ext.intent, diffs, matches, norm, s["supplier"]["name"], s.get("versions", {}))
        store.audit(s["message_id"], "routed", {"route": decision.route, "reasons": decision.reasons,
                                                "actions": actions})
        return {"decision": decision.model_dump(mode="json"), "actions": actions}

    def human_review(s: AgentState) -> dict:
        if s.get("stop_reason"):
            decision = {"route": "stop", "reasons": [s["stop_reason"]]}
        else:
            decision = s["decision"]
        summary = _summary(s, decision)
        case = erp.create_case(ReviewCase(
            name="new", message_id=s.get("message_id", "unknown"),
            supplier=(s.get("supplier") or {}).get("name", ""), reasons=decision["reasons"],
            proposed_changes=s.get("actions", []), base_versions=s.get("versions", {}), summary=summary))
        if decision["route"] in ("manual", "stop") or not s.get("actions"):
            store.audit(s.get("message_id"), "manual_case", {"case": case.name, "reasons": decision["reasons"]})
            return {"case_id": case.name, "decision": decision, "review_status": "manual",
                    "result": {"status": "manual", "case_id": case.name, "reasons": decision["reasons"]}}
        store.audit(s["message_id"], "review_requested", {"case": case.name})
        resume = interrupt({"case_id": case.name, "reasons": decision["reasons"], "summary": summary})
        current = erp.get_case(case.name)
        store.audit(s["message_id"], "review_resumed", {"case": case.name, "status": current.status.value,
                                                        "by": (resume or {}).get("user")},
                    actor=(resume or {}).get("user", "unknown"))
        return {"case_id": case.name, "review_status": current.status.value}

    def apply_to_erpnext(s: AgentState) -> dict:
        mode = "auto" if s["decision"]["route"] == "auto" else "approved"
        res = apply_actions(erp, message_id=s["message_id"], mode=mode, actions=s.get("actions", []),
                            case_id=s.get("case_id"))
        store.audit(s["message_id"], "applied", {**res, "mode": mode})
        return {"result": {**res, "route": s["decision"]["route"], "case_id": s.get("case_id")}}

    def stop_or(next_node: str):
        def f(s: AgentState) -> str:
            return "human_review" if s.get("stop_reason") else next_node
        return f

    def after_route(s: AgentState) -> str:
        return "apply_to_erpnext" if s["decision"]["route"] == "auto" else "human_review"

    def after_review(s: AgentState) -> str:
        st = s.get("review_status")
        if st == CaseStatus.APPROVED.value:
            return "apply_to_erpnext"
        if st == CaseStatus.PENDING.value:
            return "human_review"
        return END

    g = StateGraph(AgentState)
    for name, fn in [("ingest_email", ingest_email), ("read_email", read_email), ("normalize", normalize),
                     ("match_po_lines", match_po_lines), ("compute_diff", compute_diff),
                     ("assess_impact", assess_impact), ("route_by_policy", route_by_policy),
                     ("human_review", human_review), ("apply_to_erpnext", apply_to_erpnext)]:
        g.add_node(name, fn)
    g.add_edge(START, "ingest_email")
    g.add_conditional_edges("ingest_email", stop_or("read_email"), ["read_email", "human_review"])
    g.add_conditional_edges("read_email", stop_or("normalize"), ["normalize", "human_review"])
    g.add_edge("normalize", "match_po_lines")
    g.add_edge("match_po_lines", "compute_diff")
    g.add_edge("compute_diff", "assess_impact")
    g.add_edge("assess_impact", "route_by_policy")
    g.add_conditional_edges("route_by_policy", after_route, ["apply_to_erpnext", "human_review"])
    g.add_conditional_edges("human_review", after_review, ["apply_to_erpnext", "human_review", END])
    g.add_edge("apply_to_erpnext", END)
    return g.compile(checkpointer=checkpointer)


def _summary(s: AgentState, decision: dict) -> str:
    parts = [f"信件：{s.get('subject', '')}（{s.get('sender', '')}）"]
    if s.get("extraction"):
        parts.append(f"意圖：{s['extraction']['intent']}，擷取信心 {s['extraction']['confidence']:.2f}")
    for d in s.get("diffs", []):
        parts.append(f"{d['po']} {d['item_code']}：原交期 {d['original_date']} → 新 ETA {d['new_eta']}"
                     f"（{d['date_diff_days']} 天）")
    imp = s.get("impact")
    if imp:
        parts.append(f"影響分析：{imp['result']}（信心 {imp['confidence']}）")
        parts += [f"  - {r}" for r in imp.get("reasons", [])]
    parts.append("判斷理由：")
    parts += [f"  - {r}" for r in decision["reasons"]]
    return "\n".join(parts)
