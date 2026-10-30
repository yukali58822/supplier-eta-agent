from __future__ import annotations

import json
from datetime import date
from typing import Any

import httpx

from ..authz import AccessDenied, assert_writable, authorize_and_project
from ..models import (CaseStatus, Item, POLine, ReviewCase, SalesOrder, ShortageTask, Supplier,
                      WorkOrder)
from ..workcal import WorkCalendar
from .base import ERPError, line_fingerprint
from .frappe_rest import FrappeDuplicate, FrappeREST, FrappeValidationError

ROLE = "eta_agent"
CASE_DT = "Supplier ETA Case"
TASK_DT = "Shortage Task"
OPLOG_DT = "ETA Operation Log"
OPEN_PO_STATUSES = ["To Receive and Bill", "To Receive", "To Bill", "Draft"]


def _d(v: Any) -> date | None:
    return date.fromisoformat(str(v)[:10]) if v else None


def _canon(x: Any) -> str:
    return json.dumps(x, sort_keys=True, default=str, ensure_ascii=False)


class ERPNextClient:
    def __init__(self, base_url: str, api_key: str, api_secret: str, timeout: float = 30.0,
                 warehouses: list[str] | None = None, transport: httpx.BaseTransport | None = None):
        self.rest = FrappeREST(base_url, api_key, api_secret, timeout, transport=transport)
        self.warehouses = warehouses or []

    def _p(self, doctype: str, rec: dict[str, Any], purpose: str) -> dict[str, Any]:
        return authorize_and_project(doctype, rec, role=ROLE, purpose=purpose)

    def _supplier(self, s: dict[str, Any]) -> Supplier:
        p = self._p("Supplier", s, "match_po")
        return Supplier(name=p["name"], supplier_name=p.get("supplier_name") or p["name"],
                        email=p.get("custom_eta_email"), transit_days=p.get("custom_transit_days") or None)

    _SUP_FIELDS = ["name", "supplier_name", "custom_eta_email", "custom_transit_days"]

    def find_supplier_by_email(self, email: str) -> Supplier | None:
        rows = self.rest.get_list("Supplier", self._SUP_FIELDS,
                                  [["custom_eta_email", "=", email], ["disabled", "=", 0]])
        return self._supplier(rows[0]) if len(rows) == 1 else None

    def get_supplier(self, name: str) -> Supplier | None:
        rows = self.rest.get_list("Supplier", self._SUP_FIELDS, [["name", "=", name]])
        return self._supplier(rows[0]) if rows else None

    _POI_FIELDS = ["name", "parent", "item_code", "supplier_part_no", "qty", "received_qty", "schedule_date",
                   "rate", "docstatus"]

    def _open_pos(self, filters: list) -> dict[str, dict]:
        rows = self.rest.get_list("Purchase Order", ["name", "supplier", "status", "docstatus"],
                                  [["docstatus", "=", 1], ["status", "in", OPEN_PO_STATUSES]] + filters)
        return {r["name"]: r for r in rows}

    def _line(self, r: dict[str, Any], supplier: str, purpose: str) -> POLine:
        p = self._p("Purchase Order Item", r, purpose)
        return POLine(po=p["parent"], line=p["name"], supplier=supplier, item_code=p["item_code"],
                      supplier_part_no=p.get("supplier_part_no"), qty=p["qty"],
                      received_qty=p.get("received_qty") or 0, schedule_date=_d(p["schedule_date"]),
                      rate=p.get("rate"),
                      po_modified=line_fingerprint(r["qty"], r.get("received_qty") or 0, r["schedule_date"],
                                                   r.get("rate"), r.get("docstatus", 1)))

    def _lines(self, pos: dict[str, dict], extra: list, purpose: str) -> list[POLine]:
        if not pos:
            return []
        rows = self.rest.get_list("Purchase Order Item", self._POI_FIELDS,
                                  [["parent", "in", list(pos)]] + extra, parent="Purchase Order")
        return [self._line(r, pos[r["parent"]]["supplier"], purpose) for r in rows
                if float(r["qty"]) > float(r.get("received_qty") or 0)]

    def open_po_lines(self, supplier: str) -> list[POLine]:
        return self._lines(self._open_pos([["supplier", "=", supplier]]), [], "match_po")

    def open_po_lines_for_items(self, items: list[str]) -> list[POLine]:
        rows = self.rest.get_list("Purchase Order Item", ["parent"], [["item_code", "in", items],
                                  ["docstatus", "=", 1]], parent="Purchase Order")
        parents = sorted({r["parent"] for r in rows})
        pos = self._open_pos([["name", "in", parents]]) if parents else {}
        return self._lines(pos, [["item_code", "in", items]], "impact_analysis")

    def _raw_line(self, line: str) -> dict | None:
        rows = self.rest.get_list("Purchase Order Item", self._POI_FIELDS + ["uom", "conversion_factor"],
                                  [["name", "=", line]], parent="Purchase Order")
        return rows[0] if rows else None

    def get_po_line(self, line: str) -> POLine | None:
        r = self._raw_line(line)
        if not r:
            return None
        sup = self.rest.get_list("Purchase Order", ["supplier"], [["name", "=", r["parent"]]])
        return self._line(r, sup[0]["supplier"] if sup else "", "writeback")

    def po_line_version(self, line: str) -> str | None:
        r = self._raw_line(line)
        return line_fingerprint(r["qty"], r.get("received_qty") or 0, r["schedule_date"], r.get("rate"),
                                r.get("docstatus", 1)) if r else None

    def get_item(self, item_code: str) -> Item | None:
        rows = self.rest.get_list("Item", ["name", "item_name", "custom_inspection_category",
                                           "custom_production_days", "custom_test_pack_ship_days"],
                                  [["name", "=", item_code]])
        if not rows:
            return None
        p = self._p("Item", rows[0], "impact_analysis")
        return Item(item_code=p["name"], item_name=p.get("item_name") or "",
                    inspection_category=p.get("custom_inspection_category") or None,
                    production_days=int(p.get("custom_production_days") or 0) or None,
                    test_pack_ship_days=int(p.get("custom_test_pack_ship_days") or 0) or None)

    def open_work_orders(self) -> list[WorkOrder]:
        wos = self.rest.get_list("Work Order", ["name", "production_item", "qty", "planned_start_date",
                                                "sales_order"],
                                 [["docstatus", "=", 1], ["status", "in", ["Not Started", "In Process"]]])
        if not wos:
            return []
        req = self.rest.get_list("Work Order Item", ["parent", "item_code", "required_qty", "transferred_qty"],
                                 [["parent", "in", [w["name"] for w in wos]]], parent="Work Order")
        need: dict[str, dict[str, float]] = {}
        for r in req:
            rem = max(float(r["required_qty"]) - float(r.get("transferred_qty") or 0), 0)
            need.setdefault(r["parent"], {})
            need[r["parent"]][r["item_code"]] = need[r["parent"]].get(r["item_code"], 0) + rem
        out = []
        for w in wos:
            p = self._p("Work Order", {**w, "required_items": need.get(w["name"], {})}, "impact_analysis")
            out.append(WorkOrder(name=p["name"], production_item=p["production_item"], qty=p["qty"],
                                 planned_start_date=_d(p.get("planned_start_date")),
                                 sales_order=p.get("sales_order") or None, required=p["required_items"]))
        return out

    def get_sales_order(self, name: str) -> SalesOrder | None:
        rows = self.rest.get_list("Sales Order", ["name", "customer", "delivery_date"], [["name", "=", name]])
        if not rows:
            return None
        p = self._p("Sales Order", rows[0], "impact_analysis")
        return SalesOrder(name=p["name"], customer=p["customer"], delivery_date=_d(p.get("delivery_date")))

    def stock(self, items: list[str]) -> dict[str, tuple[float, float]]:
        filters: list = [["item_code", "in", items]]
        if self.warehouses:
            filters.append(["warehouse", "in", self.warehouses])
        rows = self.rest.get_list("Bin", ["item_code", "actual_qty", "reserved_qty"], filters)
        out: dict[str, list[float]] = {}
        for r in rows:
            p = self._p("Bin", r, "impact_analysis")
            a = out.setdefault(p["item_code"], [0.0, 0.0])
            a[0] += float(p.get("actual_qty") or 0)
            a[1] += float(p.get("reserved_qty") or 0)
        for i in items:
            if i not in out and self.get_item(i):
                out[i] = [0.0, 0.0]
        return {k: (v[0], v[1]) for k, v in out.items()}

    def holiday_calendar(self, name: str) -> WorkCalendar:
        hl = self.rest.get_doc("Holiday List", name)
        if not hl:
            raise KeyError(f"ERPNext 找不到 Holiday List：{name}")
        return WorkCalendar(name, _d(hl["from_date"]), _d(hl["to_date"]),
                            frozenset(_d(h["holiday_date"]) for h in hl.get("holidays", [])))

    _TASK_FIELDS = ["name", "supplier", "purchase_order", "po_item", "item_code", "qty", "required_date",
                    "status", "assigned_to"]

    def open_shortage_tasks(self, supplier: str | None = None) -> list[ShortageTask]:
        f: list = [["status", "=", "Open"]]
        if supplier:
            f.append(["supplier", "=", supplier])
        out = []
        for r in self.rest.get_list(TASK_DT, self._TASK_FIELDS, f):
            p = self._p("Shortage Task", r, "shortage_tracking")
            out.append(ShortageTask(**{**p, "qty": float(p["qty"]), "required_date": _d(p["required_date"])}))
        return out

    _CASE_FIELDS = ["name", "message_id", "supplier", "workflow_state", "reasons", "proposed_changes",
                    "base_versions", "summary"]

    def create_case(self, case: ReviewCase) -> ReviewCase:
        versions = _canon(case.base_versions)
        for r in self.rest.get_list(CASE_DT, self._CASE_FIELDS, [["message_id", "=", case.message_id]]):
            if r.get("workflow_state") != "Expired" and _canon(json.loads(r.get("base_versions") or "{}")) == versions:
                return self._case(r)
        doc = self.rest.insert({
            "doctype": CASE_DT, "message_id": case.message_id, "supplier": case.supplier or None,
            "summary": case.summary, "reasons": json.dumps(case.reasons, ensure_ascii=False),
            "proposed_changes": json.dumps(case.proposed_changes, ensure_ascii=False, default=str),
            "base_versions": versions})
        return self._case(doc)

    def get_case(self, name: str) -> ReviewCase | None:
        rows = self.rest.get_list(CASE_DT, self._CASE_FIELDS, [["name", "=", name]])
        return self._case(rows[0]) if rows else None

    def _approver(self, name: str) -> str | None:
        for v in self.rest.get_list("Version", ["owner", "data"], [["ref_doctype", "=", CASE_DT],
                                    ["docname", "=", name]], order_by="creation desc"):
            try:
                changed = json.loads(v.get("data") or "{}").get("changed", [])
            except ValueError:
                continue
            for field, _old, new in changed:
                if field == "workflow_state" and new in ("Approved", "Rejected"):
                    return v["owner"]
        return None

    def _case(self, r: dict[str, Any]) -> ReviewCase:
        status = CaseStatus(r.get("workflow_state") or "Pending")
        approver = self._approver(r["name"]) if status in (CaseStatus.APPROVED, CaseStatus.REJECTED,
                                                           CaseStatus.APPLIED, CaseStatus.EXPIRED) else None
        return ReviewCase(
            name=r["name"], message_id=r["message_id"], supplier=r.get("supplier") or "", status=status,
            reasons=json.loads(r.get("reasons") or "[]"),
            proposed_changes=json.loads(r.get("proposed_changes") or "[]"),
            base_versions=json.loads(r.get("base_versions") or "{}"), approver=approver,
            summary=r.get("summary") or "")

    def user_roles(self, user: str) -> set[str]:
        if user == "Administrator":
            return {"Administrator", "System Manager", "Purchase Manager", "Purchase User", "ETA Agent"}
        doc = self.rest.get_doc("User", user)
        if not doc or not doc.get("enabled"):
            return set()
        return {r["role"] for r in doc.get("roles", [])}

    def workflow_action(self, case: str, action: str, user: str | None = None) -> None:
        self.rest.call("frappe.model.workflow.apply_workflow", doc={"doctype": CASE_DT, "name": case},
                       action=action)

    def operation_status(self, op_id: str) -> dict[str, Any] | None:
        doc = self.rest.get_doc(OPLOG_DT, op_id)
        if not doc:
            return None
        return {"op_id": op_id, "status": doc.get("status"), "changes": json.loads(doc.get("changes") or "[]")}

    def apply_change(self, payload: dict[str, Any]) -> dict[str, Any]:
        op_id, mode, actions = payload["op_id"], payload["mode"], payload["actions"]
        recovering = False
        try:
            self.rest.insert({"doctype": OPLOG_DT, "op_id": op_id, "status": "Pending", "mode": mode,
                              "message_id": payload["message_id"], "case_id": payload.get("case_id"),
                              "payload": json.dumps(payload, ensure_ascii=False, default=str)})
        except FrappeDuplicate:
            st = self.operation_status(op_id)
            if st and st["status"] == "Done":
                return {"status": "already_applied", "op_id": op_id}
            recovering = True

        try:
            res = self._apply(payload, recovering)
        except FrappeValidationError as e:
            res = {"status": "rejected", "reason": f"ERPNext 拒絕：{e}"}
        if res["status"] == "applied":
            self.rest.update(OPLOG_DT, op_id, {"status": "Done",
                                               "changes": json.dumps(res["changes"], ensure_ascii=False, default=str)})
        elif res["status"] in ("rejected", "version_conflict"):
            self.rest.update(OPLOG_DT, op_id, {"status": "Rejected", "changes": json.dumps(res, ensure_ascii=False)})
        return res

    def _apply(self, p: dict[str, Any], recovering: bool) -> dict[str, Any]:
        mode, actions = p["mode"], p["actions"]
        case = None
        if mode == "approved":
            case = self.get_case(p.get("case_id") or "")
            if case is None:
                return {"status": "rejected", "reason": "找不到審批案件"}
            if case.status != CaseStatus.APPROVED and not (recovering and case.status == CaseStatus.APPLIED):
                return {"status": "rejected", "reason": f"案件狀態為 {case.status.value}，未核准"}
            if not case.approver or "Purchase Manager" not in self.user_roles(case.approver):
                return {"status": "rejected", "reason": "核准人無採購主管權限"}
            if _canon(case.proposed_changes) != _canon(actions):
                return {"status": "rejected", "reason": "寫回內容與核准內容不一致"}
        elif mode != "auto":
            return {"status": "rejected", "reason": f"未知模式 {mode}"}

        current: dict[str, dict] = {}
        for a in actions:
            t = a["type"]
            if t == "update_po_line":
                r = self._raw_line(a["line"])
                if not r:
                    return {"status": "rejected", "reason": f"找不到明細 {a['line']}"}
                try:
                    assert_writable("Purchase Order Item", a["set"].keys())
                except AccessDenied as e:
                    return {"status": "rejected", "reason": str(e)}
                if mode == "auto" and set(a["set"]) - {"schedule_date"}:
                    return {"status": "rejected", "reason": "自動模式只允許修改交期"}
                done = self._line_matches(r, a["set"])
                fp = line_fingerprint(r["qty"], r.get("received_qty") or 0, r["schedule_date"], r.get("rate"),
                                      r.get("docstatus", 1))
                if fp != a["expected_version"] and not (recovering and done):
                    if case:
                        self._case_transition(case.name, "Expire")
                    return {"status": "version_conflict", "reason": f"{a['line']} 關鍵欄位已變更，原核准作廢"}
                if "qty" in a["set"] and float(a["set"]["qty"]) < float(r.get("received_qty") or 0):
                    return {"status": "rejected", "reason": "數量不可小於已收數量"}
                current[a["line"]] = r
            elif t == "create_shortage_task":
                if mode != "approved":
                    return {"status": "rejected", "reason": "建立補料任務須經核准"}
            elif t == "close_shortage_task":
                task = self.rest.get_doc(TASK_DT, a["task"])
                if not task:
                    return {"status": "rejected", "reason": f"找不到補料任務 {a['task']}"}
                if task["status"] != "Open" and not (recovering and task.get("closed_by_message") == p["message_id"]):
                    return {"status": "rejected", "reason": f"補料任務 {a['task']} 不是未結狀態（只能結案一次）"}
            elif t != "record_confirmation":
                return {"status": "rejected", "reason": f"未知操作 {t}"}

        changes: list[dict] = []
        by_po: dict[str, list[dict]] = {}
        for a in actions:
            if a["type"] == "update_po_line":
                by_po.setdefault(a["po"], []).append(a)
        for po, acts in by_po.items():
            trans, pending = [], []
            for a in acts:
                r = current[a["line"]]
                before = {k: (str(r[k])[:10] if k == "schedule_date" else r[k]) for k in a["set"]}
                changes.append({"doctype": "Purchase Order Item", "name": a["line"], "before": before,
                                "after": a["set"]})
                if self._line_matches(r, a["set"]):
                    continue
                row = {"docname": r["name"], "item_code": r["item_code"], "qty": float(r["qty"]),
                       "rate": float(r["rate"]), "schedule_date": str(r["schedule_date"])[:10],
                       "conversion_factor": float(r.get("conversion_factor") or 1), "uom": r.get("uom")}
                row.update({k: (str(v) if k == "schedule_date" else float(v)) for k, v in a["set"].items()})
                pending.append(row)
            if pending:
                others = [o for o in self.rest.get_list("Purchase Order Item", self._POI_FIELDS + ["uom", "conversion_factor"],
                                                        [["parent", "=", po]], parent="Purchase Order")
                          if o["name"] not in {x["docname"] for x in pending}]
                for o in others:
                    trans.append({"docname": o["name"], "item_code": o["item_code"], "qty": float(o["qty"]),
                                  "rate": float(o["rate"]), "schedule_date": str(o["schedule_date"])[:10],
                                  "conversion_factor": float(o.get("conversion_factor") or 1), "uom": o.get("uom")})
                self.rest.call("erpnext.controllers.accounts_controller.update_child_qty_rate",
                               parent_doctype="Purchase Order", trans_items=pending + trans,
                               parent_doctype_name=po, child_docname="items")

        for a in actions:
            if a["type"] == "create_shortage_task":
                existing = self.rest.get_list(TASK_DT, ["name"], [["source_op", "=", p["op_id"]],
                                                                  ["po_item", "=", a["line"]]])
                if existing:
                    name = existing[0]["name"]
                else:
                    assignee = a.get("assigned_to")
                    if assignee and assignee != "Administrator" and not self.rest.exists("User", assignee):
                        assignee = None
                    t = self.rest.insert({"doctype": TASK_DT, "supplier": a["supplier"], "purchase_order": a["po"],
                                          "po_item": a["line"], "item_code": a["item_code"], "qty": a["qty"],
                                          "required_date": a["required_date"], "status": "Open",
                                          "assigned_to": assignee, "source_case": p.get("case_id"),
                                          "source_op": p["op_id"]})
                    name = t["name"]
                    if assignee:
                        try:
                            self.rest.call("frappe.desk.form.assign_to.add", assign_to=[assignee],
                                           doctype=TASK_DT, name=name, date=a["required_date"],
                                           description=f"補料 {a['item_code']} {a['qty']}，需求日 {a['required_date']}")
                        except FrappeValidationError:
                            pass
                changes.append({"doctype": TASK_DT, "name": name, "created": True})
            elif a["type"] == "close_shortage_task":
                task = self.rest.get_doc(TASK_DT, a["task"])
                if task["status"] == "Open":
                    self.rest.update(TASK_DT, a["task"], {"status": "Closed", "committed_date": a.get("committed_date"),
                                                          "closed_by_message": p["message_id"]})
                changes.append({"doctype": TASK_DT, "name": a["task"], "before": {"status": "Open"},
                                "after": {"status": "Closed"}})
        if case and case.status == CaseStatus.APPROVED:
            self._case_transition(case.name, "Apply")
        return {"status": "applied", "op_id": p["op_id"], "changes": changes}

    @staticmethod
    def _line_matches(r: dict, target: dict) -> bool:
        for k, v in target.items():
            cur = r.get(k)
            if k == "schedule_date":
                if str(cur)[:10] != str(v)[:10]:
                    return False
            elif abs(float(cur or 0) - float(v)) > 1e-9:
                return False
        return True

    def _case_transition(self, case: str, action: str) -> None:
        self.rest.call("frappe.model.workflow.apply_workflow", doc={"doctype": CASE_DT, "name": case},
                       action=action)
