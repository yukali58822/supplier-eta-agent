from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable

from ..config import ROOT
from .fake import SEED_PATH
from .frappe_rest import FrappeREST, FrappeValidationError

MAP_PATH = ROOT / "data" / "erpnext_map.json"
MODULE = "Buying"
Log = Callable[[str], None]


def _f(fieldname: str, fieldtype: str, label: str, **kw: Any) -> dict:
    return {"fieldname": fieldname, "fieldtype": fieldtype, "label": label, **kw}


def _perm(role: str, **kw: Any) -> dict:
    return {"role": role, "read": 1, "write": 0, "create": 0, "delete": 0, "report": 1, **kw}


DOCTYPES: list[dict] = [
    {"name": "ETA Operation Log", "autoname": "field:op_id", "fields": [
        _f("op_id", "Data", "Operation ID", reqd=1, unique=1, in_list_view=1),
        _f("status", "Select", "Status", options="Pending\nDone\nRejected", default="Pending", in_list_view=1),
        _f("mode", "Data", "Mode"),
        _f("message_id", "Data", "Message-ID", in_list_view=1),
        _f("case_id", "Data", "Case"),
        _f("payload", "Code", "Payload", options="JSON"),
        _f("changes", "Code", "Changes (before/after)", options="JSON")],
     "permissions": [_perm("System Manager", write=1, create=1, delete=1), _perm("Purchase Manager"),
                     _perm("ETA Agent", write=1, create=1)]},
    {"name": "Supplier ETA Case", "autoname": "format:ETA-CASE-{####}", "fields": [
        _f("workflow_state", "Link", "Workflow State", options="Workflow State", read_only=1, in_list_view=1,
           in_standard_filter=1),
        _f("supplier", "Link", "Supplier", options="Supplier", read_only=1, in_list_view=1),
        _f("message_id", "Data", "Message-ID", read_only=1),
        _f("summary", "Long Text", "判斷摘要", read_only=1),
        _f("reasons", "Code", "判斷理由 (JSON)", options="JSON", read_only=1),
        _f("proposed_changes", "Code", "提議異動 (JSON)", options="JSON", read_only=1),
        _f("base_versions", "Code", "資料版本 (JSON)", options="JSON", read_only=1)],
     "permissions": [_perm("System Manager", write=1, create=1, delete=1), _perm("Purchase Manager", write=1),
                     _perm("Purchase User"), _perm("ETA Agent", write=1, create=1)],
     "track_changes": 1},
    {"name": "Shortage Task", "autoname": "format:ST-{####}", "fields": [
        _f("status", "Select", "Status", options="Open\nClosed\nCancelled", default="Open", in_list_view=1,
           in_standard_filter=1),
        _f("supplier", "Link", "Supplier", options="Supplier", reqd=1, in_list_view=1),
        _f("purchase_order", "Link", "Purchase Order", options="Purchase Order", reqd=1),
        _f("po_item", "Data", "Purchase Order Item", reqd=1),
        _f("item_code", "Link", "Item", options="Item", reqd=1, in_list_view=1),
        _f("qty", "Float", "缺口數量", reqd=1, in_list_view=1),
        _f("required_date", "Date", "需求日", reqd=1, in_list_view=1),
        _f("assigned_to", "Link", "負責採購", options="User"),
        _f("committed_date", "Date", "供應商承諾日", read_only=1),
        _f("source_case", "Data", "來源案件", read_only=1),
        _f("source_op", "Data", "來源操作", read_only=1),
        _f("closed_by_message", "Data", "結案信件", read_only=1)],
     "permissions": [_perm("System Manager", write=1, create=1, delete=1), _perm("Purchase Manager", write=1, create=1),
                     _perm("Purchase User", write=1), _perm("ETA Agent", write=1, create=1)],
     "track_changes": 1},
]

CUSTOM_FIELDS = [
    ("Supplier", "custom_eta_email", "Data", "交期回信信箱", "supplier_name", {"options": "Email"}),
    ("Supplier", "custom_transit_days", "Int", "運輸天數（日曆天）", "custom_eta_email", {}),
    ("Item", "custom_inspection_category", "Data", "進料檢驗分類", "item_group", {}),
    ("Item", "custom_production_days", "Int", "生產天數（工作天）", "custom_inspection_category",
     {"description": "成品從開工到完工要幾個工作天；空白或 0 = 未設定，影響分析會採預設值並轉人工"}),
    ("Item", "custom_test_pack_ship_days", "Int", "測試包裝出貨天數（工作天）", "custom_production_days",
     {"description": "完工後功能測試、包裝、安排出貨要幾個工作天；空白或 0 = 未設定"}),
    ("Purchase Order", "custom_eta_seed_key", "Data", "ETA 測試資料代號", "supplier", {"read_only": 1}),
    ("Sales Order", "custom_eta_seed_key", "Data", "ETA 測試資料代號", "customer", {"read_only": 1}),
    ("Work Order", "custom_eta_seed_key", "Data", "ETA 測試資料代號", "production_item", {"read_only": 1}),
]

WF_STATES = ["Pending", "Approved", "Rejected", "Expired", "Applied"]
WF_ACTIONS = ["Approve", "Reject", "Apply", "Expire"]


def setup(rest: FrappeREST, log: Log = print) -> None:
    me = rest.logged_user()
    log(f"連線成功：{rest.base_url}（API 使用者：{me}）")

    for role in ("ETA Agent", "Purchase Manager", "Purchase User"):
        if not rest.exists("Role", role):
            rest.insert({"doctype": "Role", "role_name": role, "desk_access": 1})
            log(f"  + 角色 {role}")
    if me != "Administrator":
        user = rest.get_doc("User", me)
        have = {r["role"] for r in user.get("roles", [])}
        need = {"ETA Agent"} - have
        if need:
            rest.update("User", me, {"roles": user["roles"] + [{"role": r} for r in need]})
            log(f"  + 讓 {me} 擁有角色 {', '.join(need)}")

    for dt, fieldname, ftype, label, after, extra in CUSTOM_FIELDS:
        if not rest.exists("Custom Field", f"{dt}-{fieldname}"):
            rest.insert({"doctype": "Custom Field", "dt": dt, "fieldname": fieldname, "fieldtype": ftype,
                         "label": label, "insert_after": after, **extra})
            log(f"  + 自訂欄位 {dt}.{fieldname}")

    for spec in DOCTYPES:
        if not rest.exists("DocType", spec["name"]):
            rest.insert({"doctype": "DocType", "name": spec["name"], "module": MODULE, "custom": 1,
                         "autoname": spec["autoname"], "naming_rule": "Expression" if spec["autoname"].startswith(
                             "format") else "By fieldname", "track_changes": spec.get("track_changes", 0),
                         "fields": spec["fields"], "permissions": spec["permissions"]})
            log(f"  + DocType {spec['name']}")

    for st in WF_STATES:
        if not rest.exists("Workflow State", st):
            rest.insert({"doctype": "Workflow State", "workflow_state_name": st})
    for act in WF_ACTIONS:
        if not rest.exists("Workflow Action Master", act):
            rest.insert({"doctype": "Workflow Action Master", "workflow_action_name": act})
    wf = "Supplier ETA Case Approval"
    if not rest.exists("Workflow", wf):
        rest.insert({
            "doctype": "Workflow", "workflow_name": wf, "document_type": "Supplier ETA Case",
            "workflow_state_field": "workflow_state", "is_active": 1, "send_email_alert": 0,
            "override_status": 1,
            "states": [{"state": s, "doc_status": "0", "allow_edit": "ETA Agent" if s != "Pending" else "Purchase Manager"}
                       for s in WF_STATES],
            "transitions": [
                {"state": "Pending", "action": "Approve", "next_state": "Approved", "allowed": "Purchase Manager",
                 "allow_self_approval": 1},
                {"state": "Pending", "action": "Reject", "next_state": "Rejected", "allowed": "Purchase Manager",
                 "allow_self_approval": 1},
                {"state": "Approved", "action": "Apply", "next_state": "Applied", "allowed": "ETA Agent",
                 "allow_self_approval": 1},
                {"state": "Approved", "action": "Expire", "next_state": "Expired", "allowed": "ETA Agent",
                 "allow_self_approval": 1}]})
        log(f"  + Workflow {wf}（只有 Purchase Manager 能核准 / 拒絕）")

    if not rest.exists("Notification", "Shortage Task Overdue"):
        try:
            rest.insert({"doctype": "Notification", "name": "Shortage Task Overdue", "enabled": 1,
                         "subject": "補料任務逾期：{{ doc.name }} {{ doc.item_code }}", "document_type": "Shortage Task",
                         "event": "Days After", "date_changed": "required_date", "days_in_advance": 1,
                         "channel": "System Notification", "condition": "doc.status == 'Open'",
                         "message": "補料任務 {{ doc.name }}（{{ doc.item_code }} × {{ doc.qty }}）已超過需求日 "
                                    "{{ doc.required_date }} 仍未結案。",
                         "recipients": [{"receiver_by_document_field": "assigned_to"},
                                        {"receiver_by_role": "Purchase Manager"}]})
            log("  + 逾期通知 Shortage Task Overdue")
        except FrappeValidationError as e:
            log(f"  ! 逾期通知建立失敗（不影響主流程）：{e}")

    seed = json.loads(SEED_PATH.read_text(encoding="utf-8"))
    hl = seed["holiday_list"]
    if not rest.exists("Holiday List", hl["name"]):
        start, end = date.fromisoformat(hl["from_date"]), date.fromisoformat(hl["to_date"])
        days = {date.fromisoformat(x): "國定假日" for x in hl["holidays"]}
        d = start
        while d <= end:
            if d.strftime("%A") in hl["weekly_off"]:
                days.setdefault(d, "週休")
            d += timedelta(days=1)
        rest.insert({"doctype": "Holiday List", "holiday_list_name": hl["name"], "from_date": hl["from_date"],
                     "to_date": hl["to_date"],
                     "holidays": [{"holiday_date": str(k), "description": v, "weekly_off": int(v == "週休")}
                                  for k, v in sorted(days.items())]})
        log(f"  + 工作日曆 {hl['name']}")
    log("setup 完成")


def _leaf_or_create(rest: FrappeREST, doctype: str, name_field: str, parent_field: str, name: str) -> str:
    if rest.exists(doctype, name):
        return name
    roots = rest.get_list(doctype, ["name"], [["is_group", "=", 1]], order_by="lft asc", limit=1)
    rest.insert({"doctype": doctype, name_field: name, parent_field: roots[0]["name"] if roots else None,
                 "is_group": 0})
    return name


def seed(rest: FrappeREST, company: str, log: Log = print) -> dict:
    d = json.loads(SEED_PATH.read_text(encoding="utf-8"))
    comp = rest.get_doc("Company", company)
    if not comp:
        raise SystemExit(f"ERPNext 找不到公司「{company}」，請檢查 .env 的 ERPNEXT_COMPANY")
    abbr = comp["abbr"]
    wh = {k: f"{k} - {abbr}" for k in ("Stores", "Work In Progress", "Finished Goods")}
    for w in wh.values():
        if not rest.exists("Warehouse", w):
            raise SystemExit(f"找不到倉庫「{w}」。請在 ERPNext 建立，或確認公司縮寫 {abbr}")
    log(f"公司：{company}（{abbr}），倉庫：{wh['Stores']}")
    mapping: dict[str, dict[str, str]] = {"suppliers": {}, "purchase_orders": {}, "po_lines": {},
                                          "sales_orders": {}, "work_orders": {}, "company": {"name": company}}

    item_group = _leaf_or_create(rest, "Item Group", "item_group_name", "parent_item_group", "ETA Demo")
    sup_group = _leaf_or_create(rest, "Supplier Group", "supplier_group_name", "parent_supplier_group", "ETA Demo")
    cust_group = _leaf_or_create(rest, "Customer Group", "customer_group_name", "parent_customer_group", "ETA Demo")

    for s in d["suppliers"]:
        rows = rest.get_list("Supplier", ["name"], [["custom_eta_email", "=", s["email"]]])
        if rows:
            name = rows[0]["name"]
        else:
            name = rest.insert({"doctype": "Supplier", "supplier_name": s["supplier_name"],
                                "supplier_group": sup_group, "custom_eta_email": s["email"],
                                "custom_transit_days": s["transit_days"] or 0})["name"]
            log(f"  + 供應商 {name}")
        mapping["suppliers"][s["name"]] = name
    sup = mapping["suppliers"]

    for c in d["customers"]:
        if not rest.get_list("Customer", ["name"], [["customer_name", "=", c]]):
            rest.insert({"doctype": "Customer", "customer_name": c, "customer_type": "Company",
                         "customer_group": cust_group})
            log(f"  + 客戶 {c}")
    customers = {c: rest.get_list("Customer", ["name"], [["customer_name", "=", c]])[0]["name"] for c in d["customers"]}

    day_fields = {"custom_production_days": "production_days", "custom_test_pack_ship_days": "test_pack_ship_days"}
    for i in d["items"]:
        if rest.exists("Item", i["item_code"]):
            cur = rest.get_list("Item", list(day_fields), [["name", "=", i["item_code"]]])[0]
            fill = {f: i[k] for f, k in day_fields.items() if i.get(k) and not cur.get(f)}
            if fill:
                rest.update("Item", i["item_code"], fill)
                log(f"  ↻ 料件 {i['item_code']} 補上 {fill}")
            continue
        doc = {"doctype": "Item", "item_code": i["item_code"], "item_name": i["item_name"], "item_group": item_group,
               "stock_uom": "Nos", "is_stock_item": 1, "include_item_in_manufacturing": 1, "valuation_rate": 10,
               "custom_inspection_category": i.get("category"),
               **{f: i[k] for f, k in day_fields.items() if i.get(k)}}
        if i.get("supplier"):
            doc["supplier_items"] = [{"supplier": sup[i["supplier"]], "supplier_part_no": i["supplier_part_no"]}]
        rest.insert(doc)
        log(f"  + 料件 {i['item_code']}")

    b = d["bom"]
    boms = rest.get_list("BOM", ["name"], [["item", "=", b["item"]], ["is_default", "=", 1], ["docstatus", "=", 1]])
    if boms:
        bom = boms[0]["name"]
    else:
        bom = rest.insert({"doctype": "BOM", "item": b["item"], "quantity": b["quantity"], "company": company,
                           "rm_cost_as_per": "Valuation Rate", "is_default": 1, "is_active": 1, "docstatus": 1,
                           "items": [{"item_code": k, "qty": v} for k, v in b["items"].items()]})["name"]
        log(f"  + BOM {bom}")

    have = {r["item_code"] for r in rest.get_list("Bin", ["item_code"], [["item_code", "in", list(d["stock"])],
                                                                        ["actual_qty", ">", 0]])}
    rows = [{"item_code": k, "warehouse": wh["Stores"], "qty": v, "valuation_rate": 10}
            for k, v in d["stock"].items() if v > 0 and k not in have]
    if rows:
        rest.insert({"doctype": "Stock Reconciliation", "company": company, "purpose": "Stock Reconciliation",
                     "posting_date": str(date.today()), "items": rows, "docstatus": 1})
        log(f"  + 期初庫存 {len(rows)} 項")

    for po in d["purchase_orders"]:
        rows = rest.get_list("Purchase Order", ["name"], [["custom_eta_seed_key", "=", po["name"]],
                                                         ["docstatus", "=", 1]])
        if rows:
            doc = rest.get_doc("Purchase Order", rows[0]["name"])
        else:
            doc = rest.insert({
                "doctype": "Purchase Order", "supplier": sup[po["supplier"]], "company": company,
                "transaction_date": po["transaction_date"], "set_warehouse": wh["Stores"],
                "schedule_date": min(i["schedule_date"] for i in po["items"]), "custom_eta_seed_key": po["name"],
                "items": [{"item_code": i["item_code"], "qty": i["qty"], "rate": i["rate"],
                           "schedule_date": i["schedule_date"], "warehouse": wh["Stores"]} for i in po["items"]],
                "docstatus": 1})
            log(f"  + 採購單 {doc['name']}（種子 {po['name']}）")
        mapping["purchase_orders"][po["name"]] = doc["name"]
        rows_by_item = {r["item_code"]: r["name"] for r in doc["items"]}
        for i in po["items"]:
            mapping["po_lines"][i["line"]] = rows_by_item[i["item_code"]]

    for so in d["sales_orders"]:
        rows = rest.get_list("Sales Order", ["name"], [["custom_eta_seed_key", "=", so["name"]], ["docstatus", "=", 1]])
        if rows:
            name = rows[0]["name"]
        else:
            name = rest.insert({
                "doctype": "Sales Order", "customer": customers[so["customer"]], "company": company,
                "transaction_date": "2026-10-15", "delivery_date": so["delivery_date"],
                "custom_eta_seed_key": so["name"], "set_warehouse": wh["Finished Goods"],
                "items": [{"item_code": so["item_code"], "qty": so["qty"], "rate": 1000,
                           "delivery_date": so["delivery_date"], "warehouse": wh["Finished Goods"]}],
                "docstatus": 1})["name"]
            log(f"  + 客戶訂單 {name}（種子 {so['name']}，交期 {so['delivery_date']}）")
        mapping["sales_orders"][so["name"]] = name

    for wo in d["work_orders"]:
        rows = rest.get_list("Work Order", ["name"], [["custom_eta_seed_key", "=", wo["name"]], ["docstatus", "=", 1]])
        if rows:
            name = rows[0]["name"]
        else:
            name = rest.insert({
                "doctype": "Work Order", "production_item": wo["production_item"], "bom_no": bom, "qty": wo["qty"],
                "company": company, "sales_order": mapping["sales_orders"][wo["sales_order"]],
                "planned_start_date": f"{wo['planned_start_date']} 08:00:00", "source_warehouse": wh["Stores"],
                "wip_warehouse": wh["Work In Progress"], "fg_warehouse": wh["Finished Goods"],
                "custom_eta_seed_key": wo["name"], "docstatus": 1})["name"]
            log(f"  + 工單 {name}（種子 {wo['name']}，計畫開工 {wo['planned_start_date']}）")
        mapping["work_orders"][wo["name"]] = name

    MAP_PATH.parent.mkdir(parents=True, exist_ok=True)
    MAP_PATH.write_text(json.dumps(mapping, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"seed 完成；編號對照表：{MAP_PATH}")
    return mapping


def reset(rest: FrappeREST, log: Log = print) -> None:
    d = json.loads(SEED_PATH.read_text(encoding="utf-8"))
    for po in d["purchase_orders"]:
        rows = rest.get_list("Purchase Order", ["name"], [["custom_eta_seed_key", "=", po["name"]], ["docstatus", "=", 1]])
        if not rows:
            continue
        name = rows[0]["name"]
        cur = rest.get_list("Purchase Order Item", ["name", "item_code", "qty", "rate", "schedule_date", "uom",
                                                    "conversion_factor"], [["parent", "=", name]], parent="Purchase Order")
        seed_by_item = {i["item_code"]: i for i in po["items"]}
        trans, changed = [], False
        for r in cur:
            s = seed_by_item.get(r["item_code"], {})
            row = {"docname": r["name"], "item_code": r["item_code"], "qty": float(s.get("qty", r["qty"])),
                   "rate": float(s.get("rate", r["rate"])), "schedule_date": s.get("schedule_date", str(r["schedule_date"])[:10]),
                   "conversion_factor": float(r.get("conversion_factor") or 1), "uom": r.get("uom")}
            changed |= (row["qty"] != float(r["qty"]) or row["rate"] != float(r["rate"])
                        or row["schedule_date"] != str(r["schedule_date"])[:10])
            trans.append(row)
        if changed:
            rest.call("erpnext.controllers.accounts_controller.update_child_qty_rate", parent_doctype="Purchase Order",
                      trans_items=trans, parent_doctype_name=name, child_docname="items")
            log(f"  ↺ {name} 恢復種子值")
    for t in rest.get_list("Shortage Task", ["name"], [["status", "=", "Open"]]):
        rest.update("Shortage Task", t["name"], {"status": "Cancelled"})
        log(f"  ↺ 補料任務 {t['name']} → Cancelled")
    log("reset 完成")


def check(client: Any, log: Log = print) -> bool:
    ok = True
    mp = json.loads(MAP_PATH.read_text(encoding="utf-8")) if MAP_PATH.exists() else {}
    seed_d = json.loads(SEED_PATH.read_text(encoding="utf-8"))
    for s in seed_d["suppliers"]:
        sup = client.find_supplier_by_email(s["email"])
        lines = client.open_po_lines(sup.name) if sup else []
        log(f"  {'✅' if sup else '❌'} 供應商 {s['email']} → {sup.name if sup else '找不到'}；未結採購明細 {len(lines)} 筆"
            + (f"（運輸 {sup.transit_days} 天）" if sup and sup.transit_days else ""))
        ok &= sup is not None
    wos = client.open_work_orders()
    seeded = set(mp.get("work_orders", {}).values())
    mine = [w for w in wos if w.name in seeded]
    log(f"  {'✅' if len(mine) == len(seed_d['work_orders']) else '❌'} 測試工單 {len(mine)}/{len(seed_d['work_orders'])}："
        + "；".join(f"{w.name} 開工 {w.planned_start_date} 需求 {len(w.required)} 項" for w in mine))
    ok &= len(mine) == len(seed_d["work_orders"])
    for code in sorted({w.production_item for w in mine}):
        item = client.get_item(code)
        if item and item.production_days and item.test_pack_ship_days:
            log(f"  ✅ 成品 {code}：生產 {item.production_days} 天、測試包裝出貨 {item.test_pack_ship_days} 天（ERPNext 料件主檔）")
        else:
            log(f"  ❌ 成品 {code}：料件主檔沒有填生產天數 / 測試包裝出貨天數（請執行 erpnext seed，或在 ERPNext 料件上填寫）")
            ok = False
    st = client.stock(["MCU-STM32", "CAP-100UF"])
    log(f"  {'✅' if st else '❌'} 庫存 {st}")
    try:
        cal = client.holiday_calendar(seed_d["holiday_list"]["name"])
        log(f"  ✅ 工作日曆 {cal.name}（{len(cal.holidays)} 個休假日）")
    except Exception as e:  # noqa: BLE001
        log(f"  ❌ 工作日曆：{e}")
        ok = False
    return ok
