import frappe
from frappe import _

PO_ITEM_FIELDS = ["poi.name", "poi.parent", "po.supplier", "poi.item_code", "poi.supplier_part_no",
                  "poi.qty", "poi.received_qty", "poi.schedule_date", "poi.rate", "po.docstatus"]


def _require_agent():
    if "ETA Agent" not in frappe.get_roles():
        frappe.throw(_("Not permitted"), frappe.PermissionError)


def _supplier(row):
    return {"name": row.name, "supplier_name": row.supplier_name, "custom_eta_email": row.custom_eta_email,
            "custom_transit_days": row.custom_transit_days or None}


@frappe.whitelist(methods=["POST"])
def supplier_by_email(email):
    _require_agent()
    rows = frappe.get_all("Supplier", filters={"custom_eta_email": email, "disabled": 0},
                          fields=["name", "supplier_name", "custom_eta_email", "custom_transit_days"])
    return _supplier(rows[0]) if len(rows) == 1 else None


@frappe.whitelist(methods=["POST"])
def supplier(name):
    _require_agent()
    row = frappe.db.get_value("Supplier", name, ["name", "supplier_name", "custom_eta_email",
                                                 "custom_transit_days"], as_dict=True)
    return _supplier(row) if row else None


@frappe.whitelist(methods=["POST"])
def open_po_lines(supplier=None, items=None, purpose="match_po"):
    _require_agent()
    fields = list(PO_ITEM_FIELDS)
    if purpose != "match_po":
        fields.remove("poi.rate")
    cond, vals = ["po.docstatus = 1", "po.status not in ('Closed', 'Completed', 'On Hold')",
                  "poi.qty > poi.received_qty"], {}
    if supplier:
        cond.append("po.supplier = %(supplier)s")
        vals["supplier"] = supplier
    if items:
        vals["items"] = tuple(frappe.parse_json(items))
        cond.append("poi.item_code in %(items)s")
    if not supplier and not items:
        frappe.throw(_("須指定 supplier 或 items"))
    return frappe.db.sql(f"""select {', '.join(fields)} from `tabPurchase Order Item` poi
        join `tabPurchase Order` po on po.name = poi.parent where {' and '.join(cond)}
        order by poi.schedule_date, poi.idx""", vals, as_dict=True)


@frappe.whitelist(methods=["POST"])
def po_line(line):
    _require_agent()
    rows = frappe.db.sql(f"""select {', '.join(PO_ITEM_FIELDS)} from `tabPurchase Order Item` poi
        join `tabPurchase Order` po on po.name = poi.parent where poi.name = %s""", line, as_dict=True)
    return rows[0] if rows else None


@frappe.whitelist(methods=["POST"])
def item(item_code):
    _require_agent()
    return frappe.db.get_value("Item", item_code, ["name", "item_name", "custom_inspection_category",
                                                 "custom_production_days", "custom_test_pack_ship_days"],
                               as_dict=True)


@frappe.whitelist(methods=["POST"])
def open_work_orders():
    _require_agent()
    wos = frappe.get_all("Work Order", filters={"docstatus": 1, "status": ["in", ["Not Started", "In Process"]]},
                         fields=["name", "production_item", "qty", "planned_start_date", "sales_order"])
    for w in wos:
        req = frappe.get_all("Work Order Item", filters={"parent": w.name},
                             fields=["item_code", "required_qty", "transferred_qty"])
        w["required_items"] = {r.item_code: max(r.required_qty - r.transferred_qty, 0) for r in req}
        w["planned_start_date"] = str(w.planned_start_date)[:10] if w.planned_start_date else None
    return wos


@frappe.whitelist(methods=["POST"])
def sales_order(name):
    _require_agent()
    return frappe.db.get_value("Sales Order", name, ["name", "customer", "delivery_date"], as_dict=True)


@frappe.whitelist(methods=["POST"])
def stock(items):
    _require_agent()
    items = frappe.parse_json(items)
    warehouses = frappe.conf.get("eta_stock_warehouses")
    filters = {"item_code": ["in", items]}
    if warehouses:
        filters["warehouse"] = ["in", warehouses]
    rows = frappe.get_all("Bin", filters=filters, fields=["item_code", "sum(actual_qty) as actual_qty",
                                                         "sum(reserved_qty) as reserved_qty"],
                          group_by="item_code")
    found = {r.item_code for r in rows}
    rows += [{"item_code": i, "actual_qty": 0, "reserved_qty": 0} for i in items
             if i not in found and frappe.db.exists("Item", i)]
    return rows


@frappe.whitelist(methods=["POST"])
def holiday_list(name):
    _require_agent()
    hl = frappe.get_doc("Holiday List", name)
    return {"from_date": str(hl.from_date), "to_date": str(hl.to_date),
            "holidays": [str(h.holiday_date) for h in hl.holidays]}


@frappe.whitelist(methods=["POST"])
def open_shortage_tasks(supplier=None):
    _require_agent()
    filters = {"status": "Open"}
    if supplier:
        filters["supplier"] = supplier
    return frappe.get_all("Shortage Task", filters=filters,
                          fields=["name", "supplier", "purchase_order", "po_item", "item_code", "qty",
                                  "required_date", "status", "assigned_to"])
