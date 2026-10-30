import frappe
from frappe import _
from frappe.model.document import Document


class ShortageTask(Document):
    def validate(self):
        if not self.is_new() and self.has_value_changed("status"):
            if self.get_doc_before_save().status != "Open":
                frappe.throw(_("補料任務只能結案一次"))
