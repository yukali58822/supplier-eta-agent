from __future__ import annotations

import hashlib
import hmac
import json
import os
from typing import Any

import httpx
from fastapi import Depends, FastAPI, File, Header, HTTPException, Request, UploadFile
from pydantic import BaseModel

from .authz import AccessDenied, redact_for_log
from .config import settings
from .service import Agent, make_agent

app = FastAPI(title="Supplier ETA Agent", version="0.1.0")
_agent: Agent | None = None


def agent() -> Agent:
    global _agent
    if _agent is None:
        _agent = make_agent()
    return _agent


def current_user(authorization: str | None = Header(None), x_demo_user: str | None = Header(None)) -> str:
    s = settings()
    if s.erp_backend == "erpnext":
        if not authorization or not authorization.startswith("token "):
            raise HTTPException(401, "需要 ERPNext API 金鑰")
        try:
            r = httpx.get(f"{s.erpnext_url}/api/method/frappe.auth.get_logged_user",
                          headers={"Authorization": authorization}, timeout=10)
        except httpx.HTTPError:
            raise HTTPException(503, "無法驗證身分")
        if r.status_code != 200:
            raise HTTPException(401, "身分驗證失敗")
        return r.json()["message"]
    if os.environ.get("ALLOW_DEMO_AUTH", "1") != "1" or not x_demo_user:
        raise HTTPException(401, "需要 X-Demo-User（僅限 fake ERP 示範模式）")
    return x_demo_user


class ResumeIn(BaseModel):
    thread_id: str


def _public(res: dict[str, Any]) -> dict[str, Any]:
    out = {k: v for k, v in res.items() if k != "state"}
    st = res.get("state") or {}
    out["impact"] = st.get("impact")
    out["decision"] = st.get("decision")
    out["actions"] = st.get("actions")
    return redact_for_log(out)


@app.post("/emails")
async def upload_email(file: UploadFile = File(...), user: str = Depends(current_user)) -> dict[str, Any]:
    raw = await file.read()
    if len(raw) > 2_000_000:
        raise HTTPException(413, "信件過大")
    return _public(agent().process_eml(raw))


@app.post("/resume")
def resume(body: ResumeIn, user: str = Depends(current_user)) -> dict[str, Any]:
    try:
        return _public(agent().resume(body.thread_id, user))
    except AccessDenied as e:
        raise HTTPException(403, str(e))
    except LookupError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(409, str(e))


@app.get("/messages/{message_id}")
def message(message_id: str, user: str = Depends(current_user)) -> dict[str, Any]:
    rec = agent().store.get_message(message_id)
    if not rec:
        raise HTTPException(404, "not found")
    return {"message": rec, "audit": agent().store.audit_events(message_id)}


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/hooks/case-decided")
async def case_decided(request: Request, x_eta_signature: str = Header("")) -> dict[str, Any]:
    secret = os.environ.get("ETA_WEBHOOK_SECRET", "")
    raw = await request.body()
    expected = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    if not secret or not hmac.compare_digest(expected, x_eta_signature):
        raise HTTPException(401, "簽章錯誤")
    data = json.loads(raw)
    ag = agent()
    case = ag.erp.get_case(data["case_id"])
    if case is None or case.message_id != data.get("message_id"):
        raise HTTPException(404, "找不到案件")
    rec = ag.store.get_message(case.message_id)
    if rec is None or not case.approver:
        raise HTTPException(409, "案件與流程不一致")
    try:
        return _public(ag.resume(rec["thread_id"], case.approver))
    except AccessDenied as e:
        raise HTTPException(403, str(e))
    except ValueError as e:
        raise HTTPException(409, str(e))
