import frappe
from frappe import _
from frappe.model.document import Document

AGENT_STATUSES = {"Expired", "Applied"}
MANAGER_STATUSES = {"Approved", "Rejected"}


class SupplierETACase(Document):
    def validate(self):
        if self.is_new():
            self.status = "Pending"
            return
        if not self.has_value_changed("status"):
            return
        before = self.get_doc_before_save().status
        roles = set(frappe.get_roles())
        if self.status in MANAGER_STATUSES:
            if before != "Pending":
                frappe.throw(_("只有待審核案件可以核准或拒絕"))
            if "Purchase Manager" not in roles:
                frappe.throw(_("只有採購主管可以核准或拒絕"), frappe.PermissionError)
            self.approver = frappe.session.user
        elif self.status in AGENT_STATUSES:
            if not frappe.flags.eta_writeback:
                frappe.throw(_("Expired / Applied 只能由寫回服務設定"), frappe.PermissionError)
        else:
            frappe.throw(_("不允許的狀態變更"))
