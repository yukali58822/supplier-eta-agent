import hashlib
import json

import frappe
from frappe import _
from frappe.utils import getdate

WRITABLE = {"Purchase Order Item": {"schedule_date", "qty", "rate"}}


def line_fingerprint(qty, received_qty, schedule_date, rate, docstatus=1):
    payload = json.dumps([float(qty), float(received_qty), str(schedule_date),
                          None if rate is None else round(float(rate), 6), int(docstatus)])
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def _current_line(line):
    rows = frappe.db.sql("""select poi.name, poi.parent, poi.item_code, poi.qty, poi.received_qty,
        poi.schedule_date, poi.rate, poi.uom, poi.conversion_factor, po.docstatus
        from `tabPurchase Order Item` poi join `tabPurchase Order` po on po.name = poi.parent
        where poi.name = %s""", line, as_dict=True)
    return rows[0] if rows else None


@frappe.whitelist(methods=["POST"])
def operation_status(op_id):
    if "ETA Agent" not in frappe.get_roles():
        frappe.throw(_("Not permitted"), frappe.PermissionError)
    if not frappe.db.exists("ETA Operation Log", op_id):
        return None
    log = frappe.get_doc("ETA Operation Log", op_id)
    return {"op_id": log.op_id, "status": log.status, "changes": json.loads(log.changes or "[]")}


@frappe.whitelist(methods=["POST"])
def apply_change(payload):
    if "ETA Agent" not in frappe.get_roles():
        frappe.throw(_("Not permitted"), frappe.PermissionError)
    p = frappe.parse_json(payload)
    op_id, mode = p["op_id"], p["mode"]
    if frappe.db.exists("ETA Operation Log", op_id):
        return {"status": "already_applied", "op_id": op_id}

    case = None
    if mode == "approved":
        if not p.get("case_id") or not frappe.db.exists("Supplier ETA Case", p["case_id"]):
            return {"status": "rejected", "reason": "找不到審批案件"}
        case = frappe.get_doc("Supplier ETA Case", p["case_id"], for_update=True)
        if case.status != "Approved":
            return {"status": "rejected", "reason": f"案件狀態為 {case.status}，未核准"}
        if "Purchase Manager" not in frappe.get_roles(case.approver or "Guest"):
            return {"status": "rejected", "reason": "核准人無採購主管權限"}
        approved = json.loads(case.proposed_changes or "[]")
        if _canon(approved) != _canon(p["actions"]):
            return {"status": "rejected", "reason": "寫回內容與核准內容不一致"}
    elif mode != "auto":
        return {"status": "rejected", "reason": f"未知模式 {mode}"}

    for a in p["actions"]:
        t = a["type"]
        if t == "update_po_line":
            cur = _current_line(a["line"])
            if not cur:
                return {"status": "rejected", "reason": f"找不到明細 {a['line']}"}
            bad = set(a["set"]) - WRITABLE["Purchase Order Item"]
            if bad:
                return {"status": "rejected", "reason": f"寫回欄位超出允許清單：{sorted(bad)}"}
            if mode == "auto" and set(a["set"]) - {"schedule_date"}:
                return {"status": "rejected", "reason": "自動模式只允許修改交期"}
            fp = line_fingerprint(cur.qty, cur.received_qty, cur.schedule_date, cur.rate, cur.docstatus)
            if fp != a["expected_version"]:
                if case:
                    frappe.flags.eta_writeback = True
                    case.db_set("status", "Expired")
                    frappe.db.commit()
                return {"status": "version_conflict", "reason": f"{a['line']} 關鍵欄位已變更，原核准作廢"}
            if "qty" in a["set"] and float(a["set"]["qty"]) < cur.received_qty:
                return {"status": "rejected", "reason": "數量不可小於已收數量"}
        elif t == "create_shortage_task":
            if mode != "approved":
                return {"status": "rejected", "reason": "建立補料任務須經核准"}
        elif t == "close_shortage_task":
            if frappe.db.get_value("Shortage Task", a["task"], "status") != "Open":
                return {"status": "rejected", "reason": f"補料任務 {a['task']} 不是未結狀態（只能結案一次）"}
        elif t != "record_confirmation":
            return {"status": "rejected", "reason": f"未知操作 {t}"}

    frappe.db.savepoint("eta_apply")
    try:
        changes = _write(p, case)
        frappe.get_doc({"doctype": "ETA Operation Log", "op_id": op_id, "status": "Done", "mode": mode,
                        "message_id": p["message_id"], "case_id": p.get("case_id"),
                        "payload": json.dumps(p, ensure_ascii=False, default=str),
                        "changes": json.dumps(changes, ensure_ascii=False, default=str)}).insert()
        if case:
            frappe.flags.eta_writeback = True
            case.status = "Applied"
            case.applied_op_id = op_id
            case.save()
    except Exception:
        frappe.db.rollback(save_point="eta_apply")
        frappe.log_error(title="ETA writeback failed", message=frappe.get_traceback())
        return {"status": "failed", "reason": "ERPNext 寫入失敗，已回滾"}
    finally:
        frappe.flags.eta_writeback = False
    return {"status": "applied", "op_id": op_id, "changes": changes}


def _canon(actions):
    return json.dumps(actions, sort_keys=True, default=str)


def _write(p, case):
    from erpnext.controllers.accounts_controller import update_child_qty_rate

    changes = []
    by_po = {}
    for a in p["actions"]:
        if a["type"] == "update_po_line":
            by_po.setdefault(a["po"], []).append(a)
    for po, acts in by_po.items():
        trans_items = []
        for a in acts:
            cur = _current_line(a["line"])
            row = {"docname": cur.name, "item_code": cur.item_code, "qty": cur.qty, "rate": cur.rate,
                   "schedule_date": str(cur.schedule_date), "uom": cur.uom,
                   "conversion_factor": cur.conversion_factor}
            before = {k: (str(row[k]) if k == "schedule_date" else row[k]) for k in a["set"]}
            row.update({k: (str(getdate(v)) if k == "schedule_date" else float(v)) for k, v in a["set"].items()})
            trans_items.append(row)
            changes.append({"doctype": "Purchase Order Item", "name": cur.name, "before": before,
                            "after": a["set"]})
        update_child_qty_rate("Purchase Order", json.dumps(trans_items), po)

    for a in p["actions"]:
        if a["type"] == "create_shortage_task":
            t = frappe.get_doc({"doctype": "Shortage Task", "supplier": a["supplier"],
                                "purchase_order": a["po"], "po_item": a["line"], "item_code": a["item_code"],
                                "qty": a["qty"], "required_date": a["required_date"], "status": "Open",
                                "assigned_to": a.get("assigned_to"),
                                "source_case": case.name if case else None}).insert()
            if a.get("assigned_to"):
                from frappe.desk.form.assign_to import add as assign_to
                assign_to({"assign_to": [a["assigned_to"]], "doctype": "Shortage Task", "name": t.name,
                           "date": a["required_date"],
                           "description": f"補料 {a['item_code']} {a['qty']}，需求日 {a['required_date']}"})
            changes.append({"doctype": "Shortage Task", "name": t.name, "created": True})
        elif a["type"] == "close_shortage_task":
            t = frappe.get_doc("Shortage Task", a["task"], for_update=True)
            t.status = "Closed"
            t.committed_date = a.get("committed_date")
            t.closed_by_message = p["message_id"]
            t.save()
            changes.append({"doctype": "Shortage Task", "name": t.name, "before": {"status": "Open"},
                            "after": {"status": "Closed"}})
    return changes
