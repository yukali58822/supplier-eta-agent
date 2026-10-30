from __future__ import annotations

import copy
import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from ..authz import AccessDenied, assert_writable, authorize_and_project
from ..models import (CaseStatus, Item, POLine, ReviewCase, SalesOrder, ShortageTask, Supplier,
                      WorkOrder)
from ..workcal import WorkCalendar
from .base import ERPError, line_fingerprint

SEED_PATH = (Path(__file__).resolve().parents[3] / "erpnext_app" / "supplier_eta" / "supplier_eta"
             / "setup" / "seed_data.json")

ROLE = "eta_agent"


def _d(s: str) -> date:
    return date.fromisoformat(s)


class FakeERP:
    def __init__(self, seed: dict[str, Any] | None = None):
        seed = copy.deepcopy(seed or json.loads(SEED_PATH.read_text(encoding="utf-8")))
        self.seed = seed
        self.suppliers = {s["name"]: s for s in seed["suppliers"]}
        self.items = {i["item_code"]: i for i in seed["items"]}
        self.po_lines: dict[str, dict[str, Any]] = {}
        for po in seed["purchase_orders"]:
            for it in po["items"]:
                self.po_lines[it["line"]] = {
                    "po": po["name"], "line": it["line"], "supplier": po["supplier"],
                    "item_code": it["item_code"], "qty": float(it["qty"]),
                    "received_qty": float(it.get("received_qty", 0)), "rate": float(it["rate"]),
                    "schedule_date": it["schedule_date"], "docstatus": 1,
                    "supplier_part_no": self.items[it["item_code"]].get("supplier_part_no"),
                    "discount_amount": 0.0,
                }
        self.stock_qty = {k: float(v) for k, v in seed["stock"].items()}
        self.reserved = {k: float(v) for k, v in seed.get("reserved", {}).items()}
        bom = seed["bom"]
        self.work_orders = {}
        for wo in seed["work_orders"]:
            req = {k: v * wo["qty"] / bom["quantity"] for k, v in bom["items"].items()} \
                if wo["production_item"] == bom["item"] else {}
            self.work_orders[wo["name"]] = {**wo, "required": wo.get("required", req)}
        self.sales_orders = {s["name"]: s for s in seed["sales_orders"]}
        self.shortage_tasks: dict[str, dict[str, Any]] = {t["name"]: dict(t) for t in seed.get("shortage_tasks", [])}
        hl = seed["holiday_list"]
        self._holiday = hl
        self.users = {u["email"]: set(u["roles"]) for u in seed["users"]}
        self.cases: dict[str, dict[str, Any]] = {}
        self.op_log: dict[str, dict[str, Any]] = {}
        self.todos: list[dict[str, Any]] = []
        self.version_log: list[dict[str, Any]] = []
        self.caller = "agent@example.com"
        self.fail_next_apply: str | None = None
        self.read_log: list[tuple[str, str]] = []

    def _proj(self, doctype: str, rec: dict[str, Any], purpose: str) -> dict[str, Any]:
        self.read_log.append((doctype, purpose))
        return authorize_and_project(doctype, rec, role=ROLE, purpose=purpose)

    def _line(self, raw: dict[str, Any], purpose: str) -> POLine:
        p = self._proj("Purchase Order Item", {
            "name": raw["line"], "parent": raw["po"], "item_code": raw["item_code"],
            "supplier_part_no": raw["supplier_part_no"], "qty": raw["qty"],
            "received_qty": raw["received_qty"], "schedule_date": raw["schedule_date"],
            "rate": raw["rate"], "discount_amount": raw["discount_amount"]}, purpose)
        return POLine(po=p["parent"], line=p["name"], supplier=raw["supplier"], item_code=p["item_code"],
                      supplier_part_no=p.get("supplier_part_no"), qty=p["qty"],
                      received_qty=p["received_qty"], schedule_date=_d(p["schedule_date"]),
                      rate=p.get("rate"), po_modified=self._fp(raw))

    @staticmethod
    def _fp(raw: dict[str, Any]) -> str:
        return line_fingerprint(raw["qty"], raw["received_qty"], raw["schedule_date"], raw["rate"],
                                raw["docstatus"])

    def find_supplier_by_email(self, email: str) -> Supplier | None:
        for s in self.suppliers.values():
            if s.get("email") and s["email"].lower() == (email or "").lower():
                return self.get_supplier(s["name"])
        return None

    def get_supplier(self, name: str) -> Supplier | None:
        s = self.suppliers.get(name)
        if not s:
            return None
        p = self._proj("Supplier", {"name": s["name"], "supplier_name": s["supplier_name"],
                                    "custom_eta_email": s.get("email"),
                                    "custom_transit_days": s.get("transit_days")}, "match_po")
        return Supplier(name=p["name"], supplier_name=p["supplier_name"], email=p["custom_eta_email"],
                        transit_days=p["custom_transit_days"])

    def open_po_lines(self, supplier: str) -> list[POLine]:
        return [self._line(r, "match_po") for r in self.po_lines.values()
                if r["supplier"] == supplier and r["docstatus"] == 1 and r["qty"] > r["received_qty"]]

    def open_po_lines_for_items(self, items: list[str]) -> list[POLine]:
        return [self._line(r, "impact_analysis") for r in self.po_lines.values()
                if r["item_code"] in items and r["docstatus"] == 1 and r["qty"] > r["received_qty"]]

    def get_po_line(self, line: str) -> POLine | None:
        r = self.po_lines.get(line)
        return self._line(r, "writeback") if r else None

    def po_line_version(self, line: str) -> str | None:
        r = self.po_lines.get(line)
        return self._fp(r) if r else None

    def get_item(self, item_code: str) -> Item | None:
        i = self.items.get(item_code)
        if not i:
            return None
        p = self._proj("Item", {"name": i["item_code"], "item_name": i["item_name"],
                                "custom_inspection_category": i.get("category"),
                                "custom_production_days": i.get("production_days"),
                                "custom_test_pack_ship_days": i.get("test_pack_ship_days")}, "impact_analysis")
        return Item(item_code=p["name"], item_name=p["item_name"],
                    inspection_category=p["custom_inspection_category"],
                    production_days=p["custom_production_days"] or None,
                    test_pack_ship_days=p["custom_test_pack_ship_days"] or None)

    def open_work_orders(self) -> list[WorkOrder]:
        out = []
        for w in self.work_orders.values():
            p = self._proj("Work Order", {"name": w["name"], "production_item": w["production_item"],
                                          "qty": w["qty"], "planned_start_date": w.get("planned_start_date"),
                                          "sales_order": w.get("sales_order"),
                                          "required_items": w["required"]}, "impact_analysis")
            out.append(WorkOrder(name=p["name"], production_item=p["production_item"], qty=p["qty"],
                                 planned_start_date=_d(p["planned_start_date"]) if p.get("planned_start_date") else None,
                                 sales_order=p.get("sales_order"), required=p["required_items"]))
        return out

    def get_sales_order(self, name: str) -> SalesOrder | None:
        s = self.sales_orders.get(name)
        if not s:
            return None
        p = self._proj("Sales Order", {"name": s["name"], "customer": s["customer"],
                                       "delivery_date": s.get("delivery_date")}, "impact_analysis")
        return SalesOrder(name=p["name"], customer=p["customer"],
                          delivery_date=_d(p["delivery_date"]) if p.get("delivery_date") else None)

    def stock(self, items: list[str]) -> dict[str, tuple[float, float]]:
        out = {}
        for i in items:
            if i in self.stock_qty:
                p = self._proj("Bin", {"item_code": i, "actual_qty": self.stock_qty[i],
                                       "reserved_qty": self.reserved.get(i, 0.0)}, "impact_analysis")
                out[i] = (p["actual_qty"], p["reserved_qty"])
        return out

    def holiday_calendar(self, name: str) -> WorkCalendar:
        hl = self._holiday
        if hl["name"] != name:
            raise KeyError(f"找不到 Holiday List：{name}")
        start, end = _d(hl["from_date"]), _d(hl["to_date"])
        offs = {"Monday": 0, "Tuesday": 1, "Wednesday": 2, "Thursday": 3, "Friday": 4,
                "Saturday": 5, "Sunday": 6}
        weekly = {offs[w] for w in hl["weekly_off"]}
        hol = {_d(x) for x in hl["holidays"]}
        d = start
        while d <= end:
            if d.weekday() in weekly:
                hol.add(d)
            d += timedelta(days=1)
        return WorkCalendar(name, start, end, frozenset(hol))

    def open_shortage_tasks(self, supplier: str | None = None) -> list[ShortageTask]:
        out = []
        for t in self.shortage_tasks.values():
            if t["status"] == "Open" and (supplier is None or t["supplier"] == supplier):
                p = self._proj("Shortage Task", {**t, "required_date": t["required_date"]}, "shortage_tracking")
                out.append(ShortageTask(**{**p, "required_date": _d(p["required_date"])}))
        return out

    def create_case(self, case: ReviewCase) -> ReviewCase:
        for c in self.cases.values():
            if c["message_id"] == case.message_id and c["base_versions"] == case.base_versions \
                    and c["status"] != "Expired":
                return ReviewCase(**c)
        name = f"ETA-CASE-{len(self.cases) + 1:04d}"
        data = case.model_copy(update={"name": name, "status": CaseStatus.PENDING}).model_dump(mode="json")
        self.cases[name] = data
        return ReviewCase(**data)

    def get_case(self, name: str) -> ReviewCase | None:
        c = self.cases.get(name)
        return ReviewCase(**c) if c else None

    def workflow_action(self, case: str, action: str, user: str) -> None:
        if "Purchase Manager" not in self.users.get(user, set()):
            raise AccessDenied(f"{user} 不是採購主管")
        c = self.cases[case]
        if c["status"] != "Pending":
            raise ValueError(f"案件狀態為 {c['status']}，不可 {action}")
        c["status"] = {"Approve": "Approved", "Reject": "Rejected"}[action]
        c["approver"] = user

    def user_roles(self, user: str) -> set[str]:
        return set(self.users.get(user, set()))

    def operation_status(self, op_id: str) -> dict[str, Any] | None:
        return copy.deepcopy(self.op_log.get(op_id))

    def apply_change(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.fail_next_apply == "before":
            self.fail_next_apply = None
            raise ERPError("連線逾時（請求未送達）")
        result = self._apply(payload)
        if self.fail_next_apply == "after":
            self.fail_next_apply = None
            raise ERPError("連線逾時（伺服器已處理，回應遺失）")
        return result

    def _apply(self, payload: dict[str, Any]) -> dict[str, Any]:
        op_id = payload["op_id"]
        if "ETA Agent" not in self.users.get(self.caller, set()):
            return {"status": "rejected", "reason": "呼叫者不是 ETA Agent"}
        done = self.op_log.get(op_id)
        if done and done["status"] == "Done":
            return {"status": "already_applied", "op_id": op_id}

        mode = payload["mode"]
        case = None
        if mode == "approved":
            case = self.cases.get(payload.get("case_id") or "")
            if not case:
                return {"status": "rejected", "reason": "找不到審批案件"}
            if case["status"] != "Approved":
                return {"status": "rejected", "reason": f"案件狀態為 {case['status']}，未核准"}
            if "Purchase Manager" not in self.users.get(case.get("approver") or "", set()):
                return {"status": "rejected", "reason": "核准人無採購主管權限"}
            if json.dumps(case["proposed_changes"], sort_keys=True, default=str) != \
                    json.dumps(payload["actions"], sort_keys=True, default=str):
                return {"status": "rejected", "reason": "寫回內容與核准內容不一致"}
        elif mode != "auto":
            return {"status": "rejected", "reason": f"未知模式 {mode}"}

        staged: list[tuple[str, Any]] = []
        for a in payload["actions"]:
            t = a["type"]
            if t == "update_po_line":
                r = self.po_lines.get(a["line"])
                if not r:
                    return {"status": "rejected", "reason": f"找不到明細 {a['line']}"}
                try:
                    assert_writable("Purchase Order Item", a["set"].keys())
                except AccessDenied as e:
                    return {"status": "rejected", "reason": str(e)}
                if mode == "auto" and set(a["set"]) - {"schedule_date"}:
                    return {"status": "rejected", "reason": "自動模式只允許修改交期"}
                if self._fp(r) != a["expected_version"]:
                    if case:
                        case["status"] = "Expired"
                    return {"status": "version_conflict", "reason": f"{a['line']} 關鍵欄位已變更，原核准作廢"}
                if "qty" in a["set"] and float(a["set"]["qty"]) < r["received_qty"]:
                    return {"status": "rejected", "reason": "數量不可小於已收數量"}
                staged.append((t, a))
            elif t == "create_shortage_task":
                if mode != "approved":
                    return {"status": "rejected", "reason": "建立補料任務須經核准"}
                staged.append((t, a))
            elif t == "close_shortage_task":
                task = self.shortage_tasks.get(a["task"])
                if not task or task["status"] != "Open":
                    return {"status": "rejected", "reason": f"補料任務 {a['task']} 不是未結狀態（只能結案一次）"}
                staged.append((t, a))
            elif t == "record_confirmation":
                staged.append((t, a))
            else:
                return {"status": "rejected", "reason": f"未知操作 {t}"}

        changes = []
        for t, a in staged:
            if t == "update_po_line":
                r = self.po_lines[a["line"]]
                before = {k: r[k] for k in a["set"]}
                for k, v in a["set"].items():
                    r[k] = float(v) if k in ("qty", "rate") else v
                changes.append({"doctype": "Purchase Order Item", "name": a["line"], "before": before,
                                "after": dict(a["set"])})
            elif t == "create_shortage_task":
                name = f"ST-{len(self.shortage_tasks) + 1:04d}"
                self.shortage_tasks[name] = {
                    "name": name, "supplier": a["supplier"], "purchase_order": a["po"], "po_item": a["line"],
                    "item_code": a["item_code"], "qty": a["qty"], "required_date": a["required_date"],
                    "status": "Open", "assigned_to": a.get("assigned_to")}
                self.todos.append({"reference_type": "Shortage Task", "reference_name": name,
                                   "allocated_to": a.get("assigned_to"), "date": a["required_date"]})
                changes.append({"doctype": "Shortage Task", "name": name, "created": True})
            elif t == "close_shortage_task":
                self.shortage_tasks[a["task"]]["status"] = "Closed"
                self.shortage_tasks[a["task"]]["closed_by_message"] = payload["message_id"]
                changes.append({"doctype": "Shortage Task", "name": a["task"], "before": {"status": "Open"},
                                "after": {"status": "Closed"}})
        if case:
            case["status"] = "Applied"
        self.op_log[op_id] = {"op_id": op_id, "status": "Done", "message_id": payload["message_id"],
                              "case_id": payload.get("case_id"), "changes": changes}
        self.version_log.extend(changes)
        return {"status": "applied", "op_id": op_id, "changes": changes}

    def external_edit_line(self, line: str, **fields: Any) -> None:
        self.po_lines[line].update(fields)
