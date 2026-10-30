from __future__ import annotations

from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from markupsafe import Markup, escape

from ..authz import redact_for_log
from ..store import as_utc

INTENT = {"confirm": "確認原訂內容", "date_change": "變更交期", "price_change": "變更單價",
          "cancel_balance": "取消剩餘數量", "split_delivery": "分批交貨", "other": "其他 / 無法分類"}
DATE_KIND = {"arrival": "到貨日", "ship": "出貨日", "unknown": "無法判斷"}
STATUS = {"waiting_review": ("待審核", "warn"), "done": ("已完成", "ok"), "manual": ("轉人工處理", "muted"),
          "rejected": ("已拒絕", "muted"), "failed": ("處理失敗", "bad"), "processing": ("處理中", "muted"),
          "reprocessing": ("重新分析中", "muted"), "duplicate": ("重複信件", "muted"),
          "interrupted": ("處理中斷", "bad")}
ROUTE = {"auto": "自動處理", "review": "送主管審核", "manual": "轉人工處理", "stop": "停止自動處理"}
IMPACT = {"no_impact": ("沒有影響", "ok"), "impact_detected": ("有影響", "bad"), "unknown": ("無法判斷", "warn")}
CASE = {"Pending": ("待審核", "warn"), "Approved": ("已核准", "ok"), "Rejected": ("已拒絕", "muted"),
        "Expired": ("已失效", "bad"), "Applied": ("已寫回", "ok")}
FIELD = {"schedule_date": "交期", "qty": "數量", "rate": "單價"}
EVENT = {"ingested": "收到信件", "context_dropped": "丟棄不屬於此供應商的資料", "extracted": "讀信完成",
         "impact": "影響分析完成", "routed": "分流判斷", "review_requested": "送主管審核",
         "manual_case": "轉人工處理", "review_resumed": "主管決定後繼續", "resume_denied": "無權限的恢復請求",
         "applied": "寫回 ERPNext", "duplicate_ignored": "重複信件，略過", "reprocess": "重新分析",
         "review_comment": "主管留言", "inbox_file": "從收件資料夾讀取", "extract_failed": "讀信失敗", "error": "處理錯誤",
         "interrupted": "上次處理到一半中斷",
         "auto_retry": "自動重試"}
WEEKDAY = "一二三四五六日"


def local_dt(v: datetime | None, tz: str = "Asia/Taipei") -> str:
    if not v:
        return "—"
    return as_utc(v).astimezone(ZoneInfo(tz)).strftime("%m/%d %H:%M")


def md(v: Any) -> str:
    if not v:
        return "—"
    try:
        d = date.fromisoformat(str(v)[:10])
    except ValueError:
        return str(v)
    return f"{d.month}/{d.day}（{WEEKDAY[d.weekday()]}）"


def impact_label(impact: dict | None) -> tuple[str, str]:
    impact = impact or {}
    if impact.get("result") != "impact_detected":
        return label(IMPACT, impact.get("result"))
    affected = impact.get("affected") or []
    if any((o.get("delay_workdays") or 0) > 0 for o in affected):
        return ("客戶訂單會延誤", "bad")
    if any(o.get("projected_ship") is None for o in affected):
        return ("影響生產，出貨日無法估算", "bad")
    return ("影響生產排程（客戶交期趕得上）", "warn")


def label(table: dict, key: Any) -> tuple[str, str]:
    v = table.get(key)
    if v is None:
        return (str(key) if key else "—", "muted")
    return v if isinstance(v, tuple) else (v, "muted")


def highlight(body: str, evidences: list[str]) -> Markup:
    out = str(escape(body or ""))
    for ev in sorted({e for e in evidences if e and e.strip()}, key=len, reverse=True):
        esc = str(escape(ev.strip()))
        if esc in out:
            out = out.replace(esc, f"<mark>{esc}</mark>", 1)
    return Markup(out)


def describe_action(a: dict, diffs_by_line: dict[str, dict]) -> str:
    t = a.get("type")
    if t == "update_po_line":
        d = diffs_by_line.get(a.get("line"), {})
        before = {"schedule_date": md(d.get("original_date")), "qty": f"{d.get('original_qty', '—'):g}"
                  if isinstance(d.get("original_qty"), (int, float)) else "—", "rate": "原單價"}
        parts = [f"{FIELD.get(k, k)}：{before.get(k, '—')} → {md(v) if k == 'schedule_date' else v}"
                 for k, v in (a.get("set") or {}).items()]
        item = d.get("item_code", "")
        return f"修改採購單 {a.get('po')}（{item}）：" + "、".join(parts)
    if t == "create_shortage_task":
        q = a.get("qty")
        return (f"建立補料任務：{a.get('item_code')} 還差 {q:g} 個，需求日 {md(a.get('required_date'))}，"
                f"指派給 {a.get('assigned_to') or '（未指派）'}" if isinstance(q, (int, float)) else "建立補料任務")
    if t == "close_shortage_task":
        return f"補料任務 {a.get('task')} 結案（供應商承諾 {md(a.get('committed_date'))} 到貨）"
    if t == "record_confirmation":
        return f"記錄供應商已確認 {a.get('po')}（不修改任何資料）"
    return str(a)


def timeline(order: dict, eta: str | None) -> list[dict]:
    late = (order.get("delay_workdays") or 0) > 0
    return [
        {"label": "零件到貨", "value": md(eta), "note": ""},
        {"label": "工單開工", "value": md(order.get("projected_start")),
         "note": f"原定 {md(order.get('planned_start'))}"},
        {"label": "預計完工", "value": md(order.get("projected_finish")), "note": ""},
        {"label": "可出貨", "value": md(order.get("projected_ship")),
         "note": f"客戶要 {md(order.get('customer_date'))}", "bad": late},
    ]


def _extras(email: dict | None) -> dict:
    raw = (email or {}).get("raw")
    if not raw:
        return {"quoted": "", "attachments": []}
    from ..email_ingest import InvalidEmail, parse_eml
    try:
        em = parse_eml(raw)
    except InvalidEmail:
        return {"quoted": "", "attachments": []}
    return {"quoted": em.quoted, "attachments": list(em.attachments)}


def case_view(view: dict, email: dict | None, case: Any, user: Any, audit: list[dict]) -> dict:
    rec, st = view["record"], view["state"]
    if not user.is_manager:
        st = {**st, **redact_for_log({k: st.get(k) for k in ("extraction", "actions", "result")})}
    ext = st.get("extraction") or {}
    diffs = st.get("diffs") or []
    by_line = {d["line"]: d for d in diffs}
    impact = st.get("impact") or {}
    decision = st.get("decision") or {}
    evid = [ln.get("evidence", "") for ln in ext.get("lines", [])]
    body = (email or {}).get("body") or st.get("body") or ""
    eta = next((d.get("new_eta") for d in diffs if d.get("new_eta")), None)
    affected = []
    for o in impact.get("affected") or []:
        affected.append({**o, "steps": timeline(o, eta),
                         "no_date": o.get("projected_ship") is None})
    result = st.get("result") or {}
    case_status = getattr(getattr(case, "status", None), "value", None)
    waiting = view["waiting"]
    interrupted = view.get("interrupted", False)
    return {
        "message_id": rec["message_id"],
        "status": label(STATUS, "interrupted" if interrupted else rec["status"]),
        "route": "轉人工處理" if rec["status"] == "manual" else "—" if rec["status"] == "failed"
        else ROUTE.get(decision.get("route") or rec.get("route"), "—"),
        "case_id": rec.get("case_id") or view.get("interrupt_case"),
        "case_status": label(CASE, case_status) if case_status else None,
        "sender": (email or {}).get("sender") or st.get("sender", ""),
        "subject": (email or {}).get("subject") or st.get("subject", ""),
        "email_date": md(st.get("email_date") or (email or {}).get("sent_at")),
        "body_html": highlight(body, evid),
        **_extras(email),
        "has_body": bool(body),
        "supplier": (st.get("supplier") or {}).get("supplier_name") or (st.get("supplier") or {}).get("name"),
        "stop_reason": st.get("stop_reason"),
        "intent": INTENT.get(ext.get("intent"), ext.get("intent") or "—"),
        "confidence": ext.get("confidence"),
        "lines": [{**ln, "kind": DATE_KIND.get(ln.get("date_kind"), "—")} for ln in ext.get("lines", [])],
        "assumptions": st.get("assumptions") or [],
        "diffs": [{**d, "original_md": md(d.get("original_date")), "new_md": md(d.get("new_eta")),
                   "cumulative_diff_days": d.get("cumulative_diff_days"),
                   "baseline_md": md(d.get("baseline_date"))} for d in diffs],
        "impact": {"label": impact_label(impact), "confidence": impact.get("confidence"),
                   "affected": affected, "reasons": impact.get("reasons") or [],
                   "basis": impact.get("basis") or [], "assumptions": impact.get("assumptions") or []}
        if impact else None,
        "reasons": ([view["retry"]["reason"]] if rec["status"] == "failed" and view.get("retry") else [])
        or decision.get("reasons") or ([st["stop_reason"]] if st.get("stop_reason") else []),
        "actions": [describe_action(a, by_line) for a in st.get("actions") or []],
        "changes": result.get("changes") or [],
        "result_status": result.get("status"),
        "result_reason": result.get("reason"),
        "can_decide": waiting and user.is_manager and case_status == "Pending",
        "can_apply_decision": waiting and user.is_manager and case_status in ("Approved", "Rejected"),
        "can_reprocess": user.is_manager and not waiting and (
            case_status == "Expired" or result.get("status") == "version_conflict"
            or rec["status"] in ("manual", "failed") or interrupted),
        "interrupted": interrupted,
        "retry": view.get("retry"),
        "failed": rec["status"] == "failed",
        "expired": case_status == "Expired" or result.get("status") == "version_conflict",
        "waiting": waiting,
        "audit": [{"at": e["at"], "event": EVENT.get(e["event"], e["event"]), "actor": e.get("actor") or ""}
                  for e in audit],
    }


def pending_row(view: dict, email: dict | None) -> dict:
    st, rec = view["state"], view["record"]
    affected = (st.get("impact") or {}).get("affected") or []
    delays = [o.get("delay_workdays") for o in affected if o.get("delay_workdays") is not None]
    cust = sorted(o.get("customer_date") for o in affected if o.get("customer_date"))
    orders = sorted({o.get("sales_order") for o in affected if o.get("sales_order")})
    max_delay = max(delays) if delays else None
    reasons = (st.get("decision") or {}).get("reasons") or []
    return {
        "message_id": rec["message_id"], "case_id": rec.get("case_id"),
        "supplier": (st.get("supplier") or {}).get("supplier_name") or (email or {}).get("sender", ""),
        "subject": (email or {}).get("subject") or st.get("subject", ""),
        "intent": INTENT.get((st.get("extraction") or {}).get("intent"), "—"),
        "impact": impact_label(st.get("impact")),
        "max_delay": max_delay, "orders": orders, "customer_date": md(cust[0]) if cust else "—",
        "first_reason": reasons[0] if reasons else "", "more_reasons": max(len(reasons) - 1, 0),
        "updated_at": rec.get("updated_at"),
        "_sort": (-(max_delay if max_delay is not None else -1), cust[0] if cust else "9999"),
    }


def recent_row(rec: dict, email: dict | None, interrupted: bool = False) -> dict:
    return {"message_id": rec["message_id"], "status": label(STATUS, "interrupted" if interrupted else rec["status"]),
            "route": "轉人工處理" if rec["status"] == "manual" else ROUTE.get(rec.get("route"), "—"),
            "case_id": rec.get("case_id"),
            "subject": (email or {}).get("subject") or "（無主旨）", "sender": (email or {}).get("sender") or "",
            "updated_at": rec.get("updated_at")}
