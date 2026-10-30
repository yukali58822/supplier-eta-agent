import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

ROLE = "ETA Agent"


def after_install():
    _roles()
    _custom_fields()
    _permissions()
    _workflow()
    _notification()
    frappe.db.commit()


def _roles():
    for r in (ROLE, "Purchase Manager", "Purchase User"):
        if not frappe.db.exists("Role", r):
            frappe.get_doc({"doctype": "Role", "role_name": r, "desk_access": 1}).insert(ignore_permissions=True)


def _custom_fields():
    create_custom_fields({
        "Supplier": [
            {"fieldname": "custom_eta_email", "label": "交期回信信箱", "fieldtype": "Data", "options": "Email",
             "insert_after": "supplier_name", "unique": 0},
            {"fieldname": "custom_transit_days", "label": "運輸天數（日曆天）", "fieldtype": "Int",
             "insert_after": "custom_eta_email"},
        ],
        "Item": [
            {"fieldname": "custom_inspection_category", "label": "進料檢驗分類", "fieldtype": "Data",
             "insert_after": "item_group", "description": "例如 MCU；對應 scheduling.yaml 的 inspection_days"},
            {"fieldname": "custom_production_days", "label": "生產天數（工作天）", "fieldtype": "Int",
             "insert_after": "custom_inspection_category",
             "description": "成品從開工到完工要幾個工作天；空白或 0 = 未設定"},
            {"fieldname": "custom_test_pack_ship_days", "label": "測試包裝出貨天數（工作天）", "fieldtype": "Int",
             "insert_after": "custom_production_days",
             "description": "完工後功能測試、包裝、安排出貨要幾個工作天；空白或 0 = 未設定"},
        ],
    }, update=True)


def _permissions():
    from frappe.permissions import add_permission, update_permission_property
    read_only = ["Supplier", "Item", "Work Order", "Sales Order", "Bin", "Holiday List", "Purchase Order",
                 "Customer", "BOM"]
    for dt in read_only:
        add_permission(dt, ROLE, 0)
        update_permission_property(dt, ROLE, 0, "read", 1)
    update_permission_property("Purchase Order", ROLE, 0, "write", 1)
    update_permission_property("Purchase Order", ROLE, 0, "submit", 1)


def _workflow():
    name = "Supplier ETA Case Approval"
    for st in ("Pending", "Approved", "Rejected", "Expired", "Applied"):
        if not frappe.db.exists("Workflow State", st):
            frappe.get_doc({"doctype": "Workflow State", "workflow_state_name": st}).insert(ignore_permissions=True)
    for act in ("Approve", "Reject"):
        if not frappe.db.exists("Workflow Action Master", act):
            frappe.get_doc({"doctype": "Workflow Action Master", "workflow_action_name": act}).insert(
                ignore_permissions=True)
    if frappe.db.exists("Workflow", name):
        return
    frappe.get_doc({
        "doctype": "Workflow", "workflow_name": name, "document_type": "Supplier ETA Case",
        "workflow_state_field": "status", "is_active": 1, "send_email_alert": 0,
        "override_status": 1,
        "states": [
            {"state": "Pending", "doc_status": "0", "allow_edit": ROLE},
            {"state": "Approved", "doc_status": "0", "allow_edit": "Purchase Manager"},
            {"state": "Rejected", "doc_status": "0", "allow_edit": "Purchase Manager"},
            {"state": "Expired", "doc_status": "0", "allow_edit": ROLE},
            {"state": "Applied", "doc_status": "0", "allow_edit": ROLE},
        ],
        "transitions": [
            {"state": "Pending", "action": "Approve", "next_state": "Approved", "allowed": "Purchase Manager",
             "allow_self_approval": 1},
            {"state": "Pending", "action": "Reject", "next_state": "Rejected", "allowed": "Purchase Manager",
             "allow_self_approval": 1},
        ],
    }).insert(ignore_permissions=True)


def _notification():
    name = "Shortage Task Overdue"
    if frappe.db.exists("Notification", name):
        return
    frappe.get_doc({
        "doctype": "Notification", "name": name, "subject": "補料任務逾期：{{ doc.name }} {{ doc.item_code }}",
        "document_type": "Shortage Task", "event": "Days After", "date_changed": "required_date",
        "days_in_advance": 1, "channel": "System Notification", "enabled": 1,
        "condition": "doc.status == 'Open'",
        "message": "補料任務 {{ doc.name }}（{{ doc.item_code }} × {{ doc.qty }}）已超過需求日 {{ doc.required_date }} 仍未結案。",
        "recipients": [{"receiver_by_document_field": "assigned_to"},
                       {"receiver_by_role": "Purchase Manager"}],
    }).insert(ignore_permissions=True)
