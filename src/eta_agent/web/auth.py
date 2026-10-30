from __future__ import annotations

import json
import secrets
import threading
from dataclasses import dataclass, field
from typing import Any

import httpx

from ..erp.frappe_rest import _server_message

STAFF_ROLES = {"Purchase User", "Purchase Manager", "System Manager", "Administrator"}


class LoginFailed(Exception):
    pass


class ERPNextActionError(Exception):
    pass


@dataclass
class WebUser:
    email: str
    full_name: str
    roles: set[str]
    sid: str | None = None
    csrf: str = field(default_factory=lambda: secrets.token_urlsafe(24))

    @property
    def is_manager(self) -> bool:
        return "Purchase Manager" in self.roles

    @property
    def is_staff(self) -> bool:
        return bool(self.roles & STAFF_ROLES)

    @property
    def role_label(self) -> str:
        return "採購主管" if self.is_manager else ("採購人員" if "Purchase User" in self.roles else "管理員")


class Sessions:
    def __init__(self) -> None:
        self._data: dict[str, WebUser] = {}
        self._lock = threading.Lock()

    def create(self, user: WebUser) -> str:
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._data[token] = user
        return token

    def get(self, token: str | None) -> WebUser | None:
        if not token:
            return None
        with self._lock:
            return self._data.get(token)

    def drop(self, token: str | None) -> None:
        with self._lock:
            self._data.pop(token or "", None)


class ERPNextLogin:
    def __init__(self, base_url: str, transport: httpx.BaseTransport | None = None, timeout: float = 30.0):
        self.base_url = base_url.strip().rstrip("/")
        self._transport = transport
        self._timeout = timeout

    def _client(self, sid: str | None = None) -> httpx.Client:
        return httpx.Client(base_url=self.base_url, transport=self._transport, timeout=self._timeout,
                            cookies={"sid": sid} if sid else None, headers={"Accept": "application/json"})

    def login(self, usr: str, pwd: str) -> tuple[str, str, str]:
        try:
            with self._client() as c:
                r = c.post("/api/method/login", data={"usr": usr, "pwd": pwd})
                if r.status_code != 200:
                    raise LoginFailed("帳號或密碼錯誤")
                sid = r.cookies.get("sid") or c.cookies.get("sid")
                if not sid or sid == "Guest":
                    raise LoginFailed("ERPNext 沒有發給登入憑證")
                full_name = (r.json() or {}).get("full_name") or usr
            with self._client(sid) as c:
                me = c.get("/api/method/frappe.auth.get_logged_user").json()["message"]
        except httpx.HTTPError as e:
            raise LoginFailed(f"連不到 ERPNext：{type(e).__name__}") from e
        return sid, me, full_name

    def apply_workflow(self, sid: str, doctype: str, name: str, action: str) -> None:
        try:
            with self._client(sid) as c:
                r = c.post("/api/method/frappe.model.workflow.apply_workflow",
                           data={"doc": json.dumps({"doctype": doctype, "name": name}), "action": action})
        except httpx.HTTPError as e:
            raise ERPNextActionError(f"連不到 ERPNext：{type(e).__name__}") from e
        if r.status_code != 200:
            raise ERPNextActionError(_server_message(r))

    def add_comment(self, sid: str, doctype: str, name: str, text: str) -> None:
        try:
            with self._client(sid) as c:
                c.post("/api/resource/Comment", json={"comment_type": "Comment", "reference_doctype": doctype,
                                                      "reference_name": name, "content": text})
        except httpx.HTTPError:
            pass


def demo_users(erp: Any) -> list[dict[str, Any]]:
    users = getattr(erp, "users", {}) or {}
    return [{"email": u, "roles": sorted(r)} for u, r in users.items()
            if r & {"Purchase User", "Purchase Manager"}]
