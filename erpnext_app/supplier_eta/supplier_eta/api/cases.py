import hashlib
import hmac
import json

import frappe
from frappe import _
from frappe.desk.form.assign_to import add as assign_to


def _require_agent():
    if "ETA Agent" not in frappe.get_roles():
        frappe.throw(_("Not permitted"), frappe.PermissionError)


@frappe.whitelist(methods=["POST"])
def create_case(case):
    _require_agent()
    case = frappe.parse_json(case)
    versions = json.dumps(case.get("base_versions") or {}, sort_keys=True)
    for name in frappe.get_all("Supplier ETA Case", filters={"message_id": case["message_id"],
                                                             "status": ["!=", "Expired"]}, pluck="name"):
        doc = frappe.get_doc("Supplier ETA Case", name)
        if json.dumps(json.loads(doc.base_versions or "{}"), sort_keys=True) == versions:
            return _out(doc)
    doc = frappe.get_doc({
        "doctype": "Supplier ETA Case", "message_id": case["message_id"],
        "supplier": case.get("supplier") or None, "summary": case.get("summary", ""),
        "reasons": json.dumps(case.get("reasons", []), ensure_ascii=False),
        "proposed_changes": json.dumps(case.get("proposed_changes", []), ensure_ascii=False),
        "base_versions": versions,
    }).insert()
    for user in _managers():
        assign_to({"assign_to": [user], "doctype": doc.doctype, "name": doc.name,
                   "description": _("供應商交期變更待審核")})
    return _out(doc)


def _managers():
    return frappe.get_all("Has Role", filters={"role": "Purchase Manager", "parenttype": "User",
                                               "parent": ["not in", ["Administrator"]]}, pluck="parent")


def _out(doc):
    return {k: doc.get(k) for k in ("name", "message_id", "supplier", "status", "reasons", "proposed_changes",
                                    "base_versions", "approver", "summary")}


@frappe.whitelist(methods=["POST"])
def get_case(name):
    _require_agent()
    if not frappe.db.exists("Supplier ETA Case", name):
        return None
    return _out(frappe.get_doc("Supplier ETA Case", name))


@frappe.whitelist(methods=["POST"])
def user_roles(user):
    if user != frappe.session.user:
        _require_agent()
    if not frappe.db.exists("User", user) or not frappe.db.get_value("User", user, "enabled"):
        return []
    return frappe.get_roles(user)


def on_case_update(doc, method=None):
    if not doc.has_value_changed("status") or doc.status not in ("Approved", "Rejected"):
        return
    url, secret = frappe.conf.get("eta_agent_url"), frappe.conf.get("eta_webhook_secret")
    if not url or not secret:
        return
    frappe.enqueue("supplier_eta.api.cases._notify_agent", queue="short", enqueue_after_commit=True,
                   url=url, secret=secret, case=doc.name, message_id=doc.message_id)


def _notify_agent(url, secret, case, message_id):
    import requests
    body = json.dumps({"case_id": case, "message_id": message_id}).encode()
    sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    requests.post(f"{url.rstrip('/')}/hooks/case-decided", data=body, timeout=15,
                  headers={"Content-Type": "application/json", "X-ETA-Signature": sig})
