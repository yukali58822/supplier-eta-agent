from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ..authz import AccessDenied
from ..config import settings
from ..inbox import InboxScanner, InboxWatcher, inbox_dir
from .auth import ERPNextActionError, ERPNextLogin, LoginFailed, Sessions, WebUser, demo_users
from .views import EVENT, case_view, local_dt, md, pending_row, recent_row

HERE = Path(__file__).parent
COOKIE = "eta_session"
CASE_DT = "Supplier ETA Case"


class LoginRequired(Exception):
    pass


def create_app(agent_factory: Callable[[], Any] | None = None, erp_login: ERPNextLogin | None = None,
               backend: str | None = None, inbox: Path | None = None, watch: bool | None = None,
               poll_seconds: float | None = None) -> FastAPI:
    state: dict[str, Any] = {}
    scanner = InboxScanner(lambda: agent(), folder=inbox or inbox_dir())
    interval = poll_seconds or float(os.environ.get("INBOX_POLL_SECONDS", "10"))
    watcher = InboxWatcher(scanner, interval)
    if watch is None:
        watch = os.environ.get("INBOX_WATCH", "1") == "1"

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        scanner.ensure_folders()
        if watch:
            watcher.start()
        yield
        watcher.stop()

    app = FastAPI(title="供應商交期確認工作台", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.filters["md"] = md
    tz = settings().company_timezone
    templates.env.filters["dt"] = lambda v: local_dt(v, tz)
    sessions = Sessions()

    def mode() -> str:
        return backend or settings().erp_backend

    def agent():
        if "agent" not in state:
            if agent_factory:
                state["agent"] = agent_factory()
            else:
                from ..service import make_agent
                state["agent"] = make_agent()
        return state["agent"]

    def login_client() -> ERPNextLogin:
        return erp_login or ERPNextLogin(settings().erpnext_url)

    def demo_allowed() -> bool:
        return mode() == "fake" and os.environ.get("ALLOW_DEMO_AUTH", "1") == "1"

    def current(request: Request) -> WebUser:
        u = sessions.get(request.cookies.get(COOKIE))
        if u is None:
            raise LoginRequired()
        return u

    def check_csrf(user: WebUser, token: str) -> None:
        if not token or token != user.csrf:
            raise HTTPException(403, "表單已過期，請重新整理頁面再試一次")

    def back(mid: str, msg: str, level: str = "ok") -> RedirectResponse:
        return RedirectResponse(f"/emails/{quote(mid, safe='')}?msg={quote(msg)}&level={level}", status_code=303)

    def page(request: Request, name: str, **ctx: Any) -> HTMLResponse:
        return templates.TemplateResponse(request, name, {"msg": request.query_params.get("msg"),
                                                          "level": request.query_params.get("level", "ok"), **ctx})

    @app.exception_handler(LoginRequired)
    async def _to_login(request: Request, exc: LoginRequired):
        return RedirectResponse("/login", status_code=303)

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request, error: str | None = None):
        demos = demo_users(agent().erp) if demo_allowed() else []
        return page(request, "login.html", error=error, demos=demos, erpnext=mode() == "erpnext",
                    erpnext_url=settings().erpnext_url)

    def start_session(user: WebUser) -> RedirectResponse:
        if not user.is_staff:
            return RedirectResponse(f"/login?error={quote('這個帳號沒有採購相關角色，不能使用工作台')}", status_code=303)
        r = RedirectResponse("/", status_code=303)
        r.set_cookie(COOKIE, sessions.create(user), httponly=True, samesite="lax")
        return r

    @app.post("/login")
    def login(usr: str = Form(...), pwd: str = Form(...)):
        if mode() != "erpnext":
            raise HTTPException(400, "示範模式請選擇示範帳號")
        try:
            sid, me, full_name = login_client().login(usr, pwd)
        except LoginFailed as e:
            return RedirectResponse(f"/login?error={quote(str(e))}", status_code=303)
        return start_session(WebUser(me, full_name, agent().erp.user_roles(me), sid=sid))

    @app.post("/login/demo")
    def login_demo(user: str = Form(...)):
        if not demo_allowed():
            raise HTTPException(403, "示範登入已關閉")
        roles = agent().erp.user_roles(user)
        return start_session(WebUser(user, user.split("@")[0], roles))

    @app.post("/logout")
    def logout(request: Request):
        sessions.drop(request.cookies.get(COOKIE))
        r = RedirectResponse("/login", status_code=303)
        r.delete_cookie(COOKIE)
        return r

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request):
        user = current(request)
        ag = agent()
        pending = []
        for rec in ag.store.list_messages(status="waiting_review", limit=200):
            v = ag.view(rec["message_id"])
            if v and v["waiting"]:
                pending.append(pending_row(v, ag.store.get_email(rec["message_id"])))
        pending.sort(key=lambda r: r["_sort"])
        recent = [recent_row(r, ag.store.get_email(r["message_id"]), ag.is_interrupted(r)) for r in ag.store.list_messages(limit=30)]
        inbox_info = {"folder": str(scanner.folder), "interval": int(interval), "watching": watch,
                      "last_scan_at": scanner.last_scan_at, "last_error": scanner.last_error,
                      "waiting": len(scanner.waiting_files()), "failed": scanner.failed_count(),
                      "failed_folder": str(scanner.failed_dir)}
        return page(request, "home.html", user=user, pending=pending, recent=recent, inbox=inbox_info)

    @app.get("/emails/{mid:path}", response_class=HTMLResponse)
    def detail(request: Request, mid: str):
        user = current(request)
        ag = agent()
        v = ag.view(mid)
        if v is None:
            raise HTTPException(404, "找不到這封信")
        case_id = v["record"].get("case_id") or v.get("interrupt_case")
        case = ag.erp.get_case(case_id) if case_id else None
        c = case_view(v, ag.store.get_email(mid), case, user, ag.store.audit_events(mid))
        return page(request, "email.html", user=user, c=c, erpnext_case_url=(
            f"{settings().erpnext_url.rstrip('/')}/app/supplier-eta-case/{case_id}"
            if mode() == "erpnext" and case_id else None))

    def decide(request: Request, mid: str, action: str, csrf: str, reason: str = "") -> RedirectResponse:
        user = current(request)
        check_csrf(user, csrf)
        if not user.is_manager:
            raise HTTPException(403, "只有採購主管可以核准或拒絕")
        ag = agent()
        v = ag.view(mid)
        if v is None or not v["waiting"]:
            return back(mid, "這個案件目前不在等待審核", "bad")
        case_id = v["interrupt_case"] or v["record"].get("case_id")
        case = ag.erp.get_case(case_id)
        if action == "Reject" and not reason.strip():
            return back(mid, "拒絕時請填寫原因", "bad")
        if case and case.status.value == "Pending":
            try:
                if mode() == "erpnext":
                    login_client().apply_workflow(user.sid, CASE_DT, case_id, action)
                else:
                    ag.erp.workflow_action(case_id, action, user.email)
            except (ERPNextActionError, AccessDenied) as e:
                return back(mid, f"ERPNext 沒有接受這個動作：{e}。也可以直接到 ERPNext 的案件頁面按 "
                                 f"Approve / Reject，再回來按「套用主管決定」", "bad")
        if action == "Reject":
            ag.store.audit(mid, "review_comment", {"case": case_id, "action": "Reject", "reason": reason.strip()},
                           actor=user.email)
            if mode() == "erpnext" and user.sid:
                login_client().add_comment(user.sid, CASE_DT, case_id, f"拒絕原因：{reason.strip()}")
        return resume_and_report(mid, v, user)

    def resume_and_report(mid: str, v: dict, user: WebUser) -> RedirectResponse:
        try:
            res = agent().resume(v["record"]["thread_id"], user.email)
        except AccessDenied as e:
            return back(mid, str(e), "bad")
        except ValueError as e:
            return back(mid, f"還不能繼續：{e}", "bad")
        except RuntimeError as e:
            return back(mid, f"{e}。重新整理頁面就能看到結果", "warn")
        result = res.get("result") or {}
        if res["status"] == "rejected":
            return back(mid, "已拒絕，採購單沒有任何修改")
        if result.get("status") == "version_conflict":
            return back(mid, "核准已失效：分析之後採購單被修改過，原本的判斷可能不再正確。請按「重新分析」", "bad")
        if result.get("status") in ("applied", "already_applied", "nothing_to_apply"):
            return back(mid, "已核准，並安全寫回 ERPNext")
        return back(mid, f"寫回沒有完成：{result.get('reason') or res['status']}", "bad")

    @app.post("/emails/{mid:path}/approve")
    def approve(request: Request, mid: str, csrf: str = Form(...)):
        return decide(request, mid, "Approve", csrf)

    @app.post("/emails/{mid:path}/reject")
    def reject(request: Request, mid: str, csrf: str = Form(...), reason: str = Form("")):
        return decide(request, mid, "Reject", csrf, reason)

    @app.post("/emails/{mid:path}/apply-decision")
    def apply_decision(request: Request, mid: str, csrf: str = Form(...)):
        user = current(request)
        check_csrf(user, csrf)
        if not user.is_manager:
            raise HTTPException(403, "只有採購主管可以操作")
        v = agent().view(mid)
        if v is None or not v["waiting"]:
            return back(mid, "這個案件目前不在等待審核", "bad")
        return resume_and_report(mid, v, user)

    @app.post("/emails/{mid:path}/reprocess")
    def reprocess(request: Request, mid: str, csrf: str = Form(...)):
        user = current(request)
        check_csrf(user, csrf)
        if not user.is_manager:
            raise HTTPException(403, "只有採購主管可以操作")
        ag = agent()
        email = ag.store.get_email(mid)
        if not email or not email.get("raw"):
            return back(mid, "找不到原始信件，無法重新分析", "bad")
        try:
            res = ag.reprocess(email["raw"])
        except RuntimeError as e:
            return back(mid, str(e), "warn")
        msg = {"waiting_review": "已重新分析，並送主管審核", "done": "已重新分析，並自動處理完成",
               "manual": "已重新分析，仍需要人工處理（原因見下方）"}.get(res["status"], f"已重新分析：{res['status']}")
        return back(mid, msg, "warn" if res["status"] != "done" else "ok")

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    app.state.sessions = sessions
    app.state.inbox = scanner
    app.state.events = EVENT
    return app


app = create_app()
