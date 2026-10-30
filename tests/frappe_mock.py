from __future__ import annotations

import json
from collections import defaultdict
from urllib.parse import parse_qs, unquote, urlparse

import httpx

CHILD_TABLES = {"Purchase Order": ("items", "Purchase Order Item"), "Work Order": ("required_items", "Work Order Item"),
                "Holiday List": ("holidays", "Holiday"), "User": ("roles", "Has Role"), "BOM": ("items", "BOM Item")}
SERIES = {"Purchase Order": "PO-REAL-", "Sales Order": "SO-REAL-", "Work Order": "WO-REAL-", "BOM": "BOM-",
          "Stock Reconciliation": "SR-", "Version": "VER-", "Supplier": None}


class FrappeMock:
    def __init__(self, user: str = "Administrator"):
        self.db: dict[str, dict[str, dict]] = defaultdict(dict)
        self.counters: dict[str, int] = defaultdict(int)
        self.user = user
        self.passwords: dict[str, str] = {"manager@demo.tw": "pw-manager", "buyer@demo.tw": "pw-buyer"}
        self.sessions: dict[str, str] = {}
        self.calls: list[tuple[str, str]] = []
        self.fail_update_items_once = False
        self.workflows: dict[str, dict] = {}
        self.db["Company"]["示範製造股份有限公司"] = {"name": "示範製造股份有限公司", "abbr": "DM"}
        for w in ("Stores", "Work In Progress", "Finished Goods"):
            self.db["Warehouse"][f"{w} - DM"] = {"name": f"{w} - DM"}
        for dt, root in (("Item Group", "全部物料組"), ("Supplier Group", "全部供應商組"), ("Customer Group", "全部客戶組")):
            self.db[dt][root] = {"name": root, "is_group": 1, "lft": 1}
        self.db["User"]["manager@demo.tw"] = {"name": "manager@demo.tw", "enabled": 1,
                                             "roles": [{"role": "Purchase Manager"}]}
        self.db["User"]["buyer@demo.tw"] = {"name": "buyer@demo.tw", "enabled": 1, "roles": [{"role": "Purchase User"}]}

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    @staticmethod
    def _ok(data) -> httpx.Response:
        return httpx.Response(200, json=data)

    @staticmethod
    def _err(code: int, exc: str, msg: str) -> httpx.Response:
        return httpx.Response(code, json={"exc_type": exc, "exception": f"frappe.exceptions.{exc}: {msg}",
                                          "_server_messages": json.dumps([json.dumps({"message": msg})])})

    def _name_for(self, dt: str, doc: dict) -> str:
        meta = self.db["DocType"].get(dt)
        if meta:
            auto = meta.get("autoname", "")
            if auto.startswith("field:"):
                return doc[auto.split(":", 1)[1]]
            if auto.startswith("format:"):
                self.counters[dt] += 1
                return auto[7:].replace("{####}", f"{self.counters[dt]:04d}")
        for key in ("name", "item_code", "role_name", "workflow_name", "workflow_state_name", "workflow_action_name",
                    "holiday_list_name", "item_group_name", "supplier_group_name", "customer_group_name",
                    "supplier_name", "customer_name"):
            if dt not in SERIES and doc.get(key):
                return doc[key]
        if dt == "Custom Field":
            return f"{doc['dt']}-{doc['fieldname']}"
        if dt == "Supplier":
            return doc["supplier_name"]
        self.counters[dt] += 1
        return f"{SERIES.get(dt) or dt[:3].upper() + '-'}{self.counters[dt]:05d}"

    def _rows(self, dt: str) -> list[dict]:
        for parent_dt, (field, child_dt) in CHILD_TABLES.items():
            if child_dt == dt:
                out = []
                for p in self.db[parent_dt].values():
                    for r in p.get(field, []):
                        out.append({**r, "parent": p["name"], "docstatus": p.get("docstatus", 0)})
                return out
        return list(self.db[dt].values())

    @staticmethod
    def _match(row: dict, filters) -> bool:
        if isinstance(filters, dict):
            filters = [[k, "=", v] for k, v in filters.items()]
        for f in filters or []:
            field, op, val = f[-3], f[-2], f[-1]
            cur = row.get(field)
            if op == "=" and not (cur == val or (val in ("", None) and cur in ("", None))):
                return False
            if op == "!=" and cur == val:
                return False
            if op == "in" and cur not in val:
                return False
            if op == ">" and not (cur is not None and float(cur) > float(val)):
                return False
        return True

    def _list(self, dt, fields, filters, order_by=None, limit=0):
        rows = [r for r in self._rows(dt) if self._match(r, filters)]
        if order_by and "creation desc" in order_by:
            rows = list(reversed(rows))
        if order_by and "lft" in order_by:
            rows.sort(key=lambda r: r.get("lft", 99))
        if int(limit or 0):
            rows = rows[:int(limit)]
        return [{f: r.get(f) for f in fields} for r in rows]

    def _on_insert(self, dt: str, doc: dict) -> None:
        if dt in ("Supplier", "Customer", "Item"):
            doc.setdefault("disabled", 0)
        if dt in CHILD_TABLES:
            field = CHILD_TABLES[dt][0]
            for i, r in enumerate(doc.get(field, []), 1):
                r.setdefault("name", f"{doc['name']}-row{i}")
                r.setdefault("received_qty", 0)
                r.setdefault("uom", "Nos")
                r.setdefault("conversion_factor", 1)
        if dt == "Purchase Order":
            doc["status"] = "To Receive and Bill" if doc.get("docstatus") == 1 else "Draft"
            sup_items = {}
            for it in self.db["Item"].values():
                for si in it.get("supplier_items", []):
                    sup_items[(it["name"], si["supplier"])] = si["supplier_part_no"]
            for r in doc["items"]:
                r["supplier_part_no"] = sup_items.get((r["item_code"], doc["supplier"]))
        if dt == "Work Order":
            bom = self.db["BOM"][doc["bom_no"]]
            doc["status"] = "Not Started"
            doc["required_items"] = [{"name": f"{doc['name']}-req{i}", "item_code": b["item_code"],
                                      "required_qty": b["qty"] * doc["qty"] / bom["quantity"], "transferred_qty": 0}
                                     for i, b in enumerate(bom["items"], 1)]
        if dt == "Stock Reconciliation" and doc.get("docstatus") == 1:
            for r in doc["items"]:
                key = f"{r['item_code']}-{r['warehouse']}"
                self.db["Bin"][key] = {"name": key, "item_code": r["item_code"], "warehouse": r["warehouse"],
                                       "actual_qty": r["qty"], "reserved_qty": 0}
        if dt == "Workflow":
            self.workflows[doc["document_type"]] = doc
        wf = self.workflows.get(dt)
        if wf and not doc.get(wf["workflow_state_field"]):
            doc[wf["workflow_state_field"]] = wf["states"][0]["state"]

    def _roles(self, user: str) -> set[str]:
        if user == "Administrator":
            return {"Administrator", "System Manager", "Purchase Manager", "ETA Agent"}
        return {r["role"] for r in self.db["User"].get(user, {}).get("roles", [])}

    def apply_workflow(self, doc_ref: dict, action: str):
        dt, name = doc_ref["doctype"], doc_ref["name"]
        wf = self.workflows[dt]
        doc = self.db[dt][name]
        field = wf["workflow_state_field"]
        for t in wf["transitions"]:
            if t["state"] == doc[field] and t["action"] == action:
                if t["allowed"] not in self._roles(self.user):
                    return self._err(403, "PermissionError", "Not permitted")
                old = doc[field]
                doc[field] = t["next_state"]
                vname = self._name_for("Version", {})
                self.db["Version"][vname] = {"name": vname, "ref_doctype": dt, "docname": name, "owner": self.user,
                                             "data": json.dumps({"changed": [[field, old, t["next_state"]]]})}
                return self._ok({"message": doc})
        return self._err(417, "WorkflowTransitionError", f"Not a valid Workflow Action: {action} from {doc[field]}")

    def update_items(self, trans_items, po: str):
        if self.fail_update_items_once:
            self.fail_update_items_once = False
            return httpx.Response(502, text="Bad Gateway")
        doc = self.db["Purchase Order"][po]
        keep = {t["docname"]: t for t in trans_items}
        new_rows = []
        for r in doc["items"]:
            if r["name"] not in keep:
                continue
            t = keep[r["name"]]
            r.update({"qty": t["qty"], "rate": t["rate"], "schedule_date": t["schedule_date"]})
            new_rows.append(r)
        doc["items"] = new_rows
        return self._ok({"message": None})

    def handle(self, req: httpx.Request) -> httpx.Response:
        path = unquote(urlparse(str(req.url)).path)
        if path == "/api/method/login":
            form = {k: v[0] for k, v in parse_qs(req.content.decode()).items()}
            if self.passwords.get(form.get("usr")) != form.get("pwd"):
                return self._err(401, "AuthenticationError", "Invalid login credentials")
            sid = f"sid-{len(self.sessions) + 1}"
            self.sessions[sid] = form["usr"]
            return httpx.Response(200, json={"message": "Logged In", "full_name": form["usr"].split("@")[0]},
                                  headers={"set-cookie": f"sid={sid}; Path=/"})
        sid = None
        for part in req.headers.get("cookie", "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == "sid":
                sid = v
        if sid and sid not in self.sessions:
            return self._err(403, "SessionExpired", "Session expired")
        acting, self.user = self.user, self.sessions.get(sid, self.user)
        try:
            return self._route(req)
        finally:
            self.user = acting

    def _route(self, req: httpx.Request) -> httpx.Response:
        url = urlparse(str(req.url))
        path = unquote(url.path)
        self.calls.append((req.method, path))
        if path.startswith("/api/resource/"):
            parts = path[len("/api/resource/"):].split("/", 1)
            dt = parts[0]
            name = parts[1] if len(parts) > 1 else None
            if req.method == "GET" and name:
                doc = self.db[dt].get(name)
                return self._ok({"data": doc}) if doc else self._err(404, "DoesNotExistError", f"{dt} {name} not found")
            if req.method == "GET":
                q = parse_qs(url.query)
                fields = json.loads(q.get("fields", ['["name"]'])[0])
                filters = json.loads(q["filters"][0]) if "filters" in q else []
                return self._ok({"data": self._list(dt, fields, filters, q.get("order_by", [None])[0],
                                                    q.get("limit_page_length", [0])[0])})
            body = json.loads(req.content)
            if req.method == "POST":
                body.pop("doctype", None)
                doc = dict(body)
                doc["name"] = self._name_for(dt, doc)
                if doc["name"] in self.db[dt]:
                    return self._err(409, "DuplicateEntryError", f"{dt} {doc['name']} already exists")
                doc.setdefault("docstatus", 0)
                doc["owner"] = self.user
                self._on_insert(dt, doc)
                self.db[dt][doc["name"]] = doc
                return self._ok({"data": doc})
            if req.method == "PUT":
                doc = self.db[dt][name]
                wf = self.workflows.get(dt)
                if wf and wf["workflow_state_field"] in body and body[wf["workflow_state_field"]] != doc.get(wf["workflow_state_field"]):
                    return self._err(417, "WorkflowTransitionError", "狀態只能透過 Workflow 變更")
                doc.update(body)
                return self._ok({"data": doc})
        if path.startswith("/api/method/"):
            method = path[len("/api/method/"):]
            form = {k: v[0] for k, v in parse_qs(req.content.decode()).items()} if req.content else {}
            j = lambda k: json.loads(form[k]) if k in form else None  # noqa: E731
            if method == "frappe.auth.get_logged_user":
                return self._ok({"message": self.user})
            if method == "frappe.client.get_list":
                return self._ok({"message": self._list(form["doctype"], j("fields"), j("filters"),
                                                       form.get("order_by"), form.get("limit_page_length") or 0)})
            if method == "frappe.model.workflow.apply_workflow":
                return self.apply_workflow(j("doc"), form["action"])
            if method == "erpnext.controllers.accounts_controller.update_child_qty_rate":
                return self.update_items(j("trans_items"), form["parent_doctype_name"])
            if method == "frappe.desk.form.assign_to.add":
                self.db["ToDo"][f"todo-{len(self.db['ToDo'])}"] = {"reference_name": form["name"],
                                                                  "allocated_to": j("assign_to")[0]}
                return self._ok({"message": []})
        return self._err(404, "DoesNotExistError", f"mock 沒有實作 {req.method} {path}")
