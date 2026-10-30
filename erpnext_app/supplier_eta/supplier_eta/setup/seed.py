import json
import os
from datetime import date, timedelta

import frappe

HERE = os.path.dirname(__file__)


def _data():
    with open(os.path.join(HERE, "seed_data.json"), encoding="utf-8") as f:
        return json.load(f)


def run(keys_path=None):
    d = _data()
    frappe.flags.in_test = False
    _setup_wizard(d)
    from supplier_eta.setup.install import after_install
    after_install()
    company = d["company"]["name"]
    abbr = d["company"]["abbr"]
    _holiday_list(d, company)
    keys = _users(d)
    _suppliers(d)
    _customers(d)
    _items(d)
    bom = _bom(d, company)
    _stock(d, company)
    _purchase_orders(d, company)
    _sales_orders(d, company)
    _work_orders(d, company, abbr, bom)
    frappe.db.commit()
    path = keys_path or os.environ.get("ETA_KEYS_PATH") or frappe.get_site_path("..", "eta_agent_keys.json")
    with open(path, "w") as f:
        json.dump(keys, f)
    os.chmod(path, 0o600)
    print(f"seed 完成；API 金鑰寫入 {path}")


def _setup_wizard(d):
    if frappe.db.get_single_value("System Settings", "setup_complete"):
        return
    from frappe.desk.page.setup_wizard.setup_wizard import setup_complete
    c = d["company"]
    setup_complete({
        "currency": c["currency"], "full_name": "Administrator", "company_name": c["name"],
        "timezone": "Asia/Taipei", "company_abbr": c["abbr"], "domains": ["Manufacturing"],
        "country": c["country"], "fy_start_date": "2026-01-01", "fy_end_date": "2026-12-31",
        "language": "english", "company_tagline": "", "email": "admin@example.com",
        "password": "admin", "chart_of_accounts": "Standard",
    })


def _holiday_list(d, company):
    hl = d["holiday_list"]
    if not frappe.db.exists("Holiday List", hl["name"]):
        start, end = date.fromisoformat(hl["from_date"]), date.fromisoformat(hl["to_date"])
        days = {date.fromisoformat(x): "國定假日" for x in hl["holidays"]}
        cur = start
        while cur <= end:
            if cur.strftime("%A") in hl["weekly_off"]:
                days.setdefault(cur, "週休")
            cur += timedelta(days=1)
        frappe.get_doc({"doctype": "Holiday List", "holiday_list_name": hl["name"], "from_date": hl["from_date"],
                        "to_date": hl["to_date"],
                        "holidays": [{"holiday_date": str(k), "description": v,
                                      "weekly_off": 1 if v == "週休" else 0} for k, v in sorted(days.items())]}
                       ).insert(ignore_permissions=True)
    frappe.db.set_value("Company", company, "default_holiday_list", hl["name"])


def _users(d):
    from frappe.core.doctype.user.user import generate_keys
    keys = {}
    for u in d["users"]:
        if not frappe.db.exists("User", u["email"]):
            doc = frappe.get_doc({"doctype": "User", "email": u["email"], "first_name": u["first_name"],
                                  "send_welcome_email": 0, "user_type": "System User",
                                  "new_password": frappe.generate_hash(length=16)})
            doc.insert(ignore_permissions=True)
            doc.add_roles(*u["roles"])
        secret = generate_keys(u["email"])["api_secret"]
        keys[u["email"]] = {"api_key": frappe.db.get_value("User", u["email"], "api_key"), "api_secret": secret}
    return keys


def _suppliers(d):
    if not frappe.db.exists("Supplier Group", "Raw Material"):
        frappe.get_doc({"doctype": "Supplier Group", "supplier_group_name": "Raw Material"}).insert()
    for s in d["suppliers"]:
        if frappe.db.exists("Supplier", s["name"]):
            continue
        frappe.get_doc({"doctype": "Supplier", "supplier_name": s["supplier_name"],
                        "supplier_group": "Raw Material", "custom_eta_email": s["email"],
                        "custom_transit_days": s["transit_days"] or 0}).insert(set_name=s["name"])


def _customers(d):
    for c in d["customers"]:
        if not frappe.db.exists("Customer", c):
            frappe.get_doc({"doctype": "Customer", "customer_name": c, "customer_type": "Company"}).insert()


def _items(d):
    for i in d["items"]:
        if frappe.db.exists("Item", i["item_code"]):
            continue
        doc = {"doctype": "Item", "item_code": i["item_code"], "item_name": i["item_name"],
               "item_group": "Products" if i["item_code"] == d["bom"]["item"] else "Raw Material",
               "stock_uom": "Nos", "is_stock_item": 1, "include_item_in_manufacturing": 1,
               "valuation_rate": 10, "custom_inspection_category": i.get("category"),
               "custom_production_days": i.get("production_days") or 0,
               "custom_test_pack_ship_days": i.get("test_pack_ship_days") or 0}
        if i.get("supplier"):
            doc["supplier_items"] = [{"supplier": i["supplier"], "supplier_part_no": i["supplier_part_no"]}]
        frappe.get_doc(doc).insert()


def _bom(d, company):
    b = d["bom"]
    existing = frappe.db.get_value("BOM", {"item": b["item"], "is_default": 1, "docstatus": 1})
    if existing:
        return existing
    bom = frappe.get_doc({"doctype": "BOM", "item": b["item"], "quantity": b["quantity"], "company": company,
                          "rm_cost_as_per": "Valuation Rate", "is_default": 1, "is_active": 1,
                          "items": [{"item_code": k, "qty": v} for k, v in b["items"].items()]})
    bom.insert()
    bom.submit()
    return bom.name


def _stock(d, company):
    if frappe.db.exists("Stock Reconciliation", {"company": company, "docstatus": 1}):
        return
    items = [{"item_code": k, "warehouse": d["warehouse"], "qty": v, "valuation_rate": 10}
             for k, v in d["stock"].items() if v > 0]
    sr = frappe.get_doc({"doctype": "Stock Reconciliation", "company": company, "purpose": "Opening Stock",
                         "posting_date": "2026-10-01", "set_posting_time": 1,
                         "expense_account": frappe.db.get_value("Company", company, "stock_adjustment_account"),
                         "items": items})
    sr.insert()
    sr.submit()


def _purchase_orders(d, company):
    for po in d["purchase_orders"]:
        if frappe.db.exists("Purchase Order", po["name"]):
            continue
        doc = frappe.get_doc({
            "doctype": "Purchase Order", "supplier": po["supplier"], "company": company,
            "transaction_date": po["transaction_date"], "set_warehouse": d["warehouse"],
            "schedule_date": min(i["schedule_date"] for i in po["items"]),
            "items": [{"name": i["line"], "item_code": i["item_code"], "qty": i["qty"], "rate": i["rate"],
                       "schedule_date": i["schedule_date"], "warehouse": d["warehouse"]} for i in po["items"]]})
        doc.insert(set_name=po["name"])
        doc.submit()


def _sales_orders(d, company):
    for so in d["sales_orders"]:
        if frappe.db.exists("Sales Order", so["name"]):
            continue
        doc = frappe.get_doc({
            "doctype": "Sales Order", "customer": so["customer"], "company": company,
            "transaction_date": "2026-10-15", "delivery_date": so["delivery_date"],
            "items": [{"item_code": so["item_code"], "qty": so["qty"], "rate": 1000,
                       "delivery_date": so["delivery_date"], "warehouse": d["warehouse"]}]})
        doc.insert(set_name=so["name"])
        doc.submit()


def _work_orders(d, company, abbr, bom):
    for wo in d["work_orders"]:
        if frappe.db.exists("Work Order", wo["name"]):
            continue
        doc = frappe.get_doc({
            "doctype": "Work Order", "production_item": wo["production_item"], "bom_no": bom,
            "qty": wo["qty"], "company": company, "sales_order": wo["sales_order"],
            "planned_start_date": f"{wo['planned_start_date']} 08:00:00",
            "source_warehouse": d["warehouse"], "wip_warehouse": f"Work In Progress - {abbr}",
            "fg_warehouse": f"Finished Goods - {abbr}", "skip_transfer": 0})
        doc.insert(set_name=wo["name"])
        doc.submit()
