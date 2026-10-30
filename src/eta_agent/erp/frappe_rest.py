from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import quote

import httpx

from .base import ERPError


class FrappeValidationError(RuntimeError):
    pass


class FrappeDuplicate(FrappeValidationError):
    pass


class FrappeNotFound(FrappeValidationError):
    pass


def _server_message(r: httpx.Response) -> str:
    try:
        body = r.json()
    except ValueError:
        return r.text[:300]
    msgs = []
    if body.get("_server_messages"):
        try:
            for m in json.loads(body["_server_messages"]):
                m = json.loads(m) if isinstance(m, str) else m
                msgs.append(re.sub(r"<[^>]+>", "", str(m.get("message", m))))
        except (ValueError, TypeError):
            msgs.append(str(body["_server_messages"])[:300])
    if not msgs and body.get("exception"):
        msgs.append(str(body["exception"]).splitlines()[-1][:300])
    if not msgs and body.get("message"):
        msgs.append(str(body["message"])[:300])
    return "；".join(msgs) or f"HTTP {r.status_code}"


class FrappeREST:
    def __init__(self, base_url: str, api_key: str, api_secret: str, timeout: float = 30.0,
                 transport: httpx.BaseTransport | None = None):
        if not base_url or not api_key or not api_secret:
            raise ValueError("缺少 ERPNEXT_URL / ERPNEXT_API_KEY / ERPNEXT_API_SECRET")
        self.base_url = base_url.strip().rstrip("/")
        self._http = httpx.Client(base_url=self.base_url, timeout=timeout, transport=transport,
                                  headers={"Authorization": f"token {api_key.strip()}:{api_secret.strip()}",
                                           "Accept": "application/json"})

    def _send(self, method: str, path: str, **kw: Any) -> Any:
        try:
            r = self._http.request(method, path, **kw)
        except httpx.HTTPError as e:
            raise ERPError(f"{method} {path} 連線失敗：{type(e).__name__}") from e
        if r.status_code >= 500 and r.status_code != 503:
            msg = _server_message(r)
            if "ValidationError" in r.text or "LinkValidationError" in r.text:
                raise FrappeValidationError(msg)
            raise ERPError(f"{method} {path} 伺服器錯誤 {r.status_code}：{msg}")
        if r.status_code == 503:
            raise ERPError(f"{method} {path} 服務暫時無法使用")
        if r.status_code in (401, 403):
            raise PermissionError(f"{method} {path} 權限不足：{_server_message(r)}")
        if r.status_code == 404:
            raise FrappeNotFound(f"{path} 不存在")
        if r.status_code == 409 or "DuplicateEntryError" in r.text and r.status_code >= 400:
            raise FrappeDuplicate(_server_message(r))
        if r.status_code >= 400:
            raise FrappeValidationError(_server_message(r))
        return r.json()

    @staticmethod
    def _p(doctype: str) -> str:
        return "/api/resource/" + quote(doctype)

    def get_list(self, doctype: str, fields: list[str] | None = None, filters: Any = None,
                 order_by: str | None = None, limit: int = 0, parent: str | None = None) -> list[dict]:
        if parent:
            return self.call("frappe.client.get_list", doctype=doctype, fields=fields or ["name"],
                             filters=filters or [], order_by=order_by, limit_page_length=limit or 0,
                             parent=parent) or []
        params = {"fields": json.dumps(fields or ["name"]), "limit_page_length": limit or 0}
        if filters:
            params["filters"] = json.dumps(filters)
        if order_by:
            params["order_by"] = order_by
        return self._send("GET", self._p(doctype), params=params)["data"]

    def get_doc(self, doctype: str, name: str) -> dict | None:
        try:
            return self._send("GET", f"{self._p(doctype)}/{quote(name, safe='')}")["data"]
        except FrappeNotFound:
            return None

    def exists(self, doctype: str, name: str) -> bool:
        return bool(self.get_list(doctype, ["name"], [["name", "=", name]], limit=1))

    def insert(self, doc: dict) -> dict:
        doctype = doc["doctype"]
        return self._send("POST", self._p(doctype), json=doc)["data"]

    def update(self, doctype: str, name: str, values: dict) -> dict:
        return self._send("PUT", f"{self._p(doctype)}/{quote(name, safe='')}", json=values)["data"]

    def call(self, method: str, **kwargs: Any) -> Any:
        body = {k: (json.dumps(v, default=str) if isinstance(v, (dict, list)) else v)
                for k, v in kwargs.items() if v is not None}
        return self._send("POST", f"/api/method/{method}", data=body).get("message")

    def logged_user(self) -> str:
        return self._send("GET", "/api/method/frappe.auth.get_logged_user")["message"]
