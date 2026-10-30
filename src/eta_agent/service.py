from __future__ import annotations

import base64
import hashlib
import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import Any

from langgraph.types import Command

from .authz import AccessDenied
from .config import policy, scheduling, settings
from .email_ingest import InvalidEmail, parse_eml
from .graph import Deps, build_graph
from .llm.extractor import make_extractor
from .models import CaseStatus
from .store import Store, as_utc

log = logging.getLogger(__name__)
IN_PROGRESS = ("processing", "reprocessing")
STALE_AFTER = timedelta(minutes=15)


def _retry_delays() -> list[timedelta]:
    import os
    raw = os.environ.get("AUTO_RETRY_MINUTES", "1,5,15").strip()
    return [timedelta(minutes=float(x)) for x in raw.split(",") if x.strip()] if raw else []

def thread_for(message_id: str, attempt: int = 0) -> str:
    h = hashlib.sha256(message_id.encode()).hexdigest()[:20]
    return f"t-{h}" if attempt == 0 else f"t-{h}-r{attempt}"


class Agent:
    def __init__(self, erp: Any, store: Store, extractor: Any, checkpointer: Any):
        self.erp, self.store = erp, store
        self.graph = build_graph(Deps(erp=erp, store=store, extractor=extractor, policy=policy(),
                                      scheduling=scheduling()), checkpointer=checkpointer)
        self.started_at = datetime.now(timezone.utc)
        self.retry_delays = _retry_delays()
        self._running: set[str] = set()
        self._running_lock = threading.Lock()

    def is_running(self, message_id: str) -> bool:
        with self._running_lock:
            return message_id in self._running

    def is_interrupted(self, rec: dict[str, Any] | None) -> bool:
        if not rec or rec["status"] not in IN_PROGRESS or self.is_running(rec["message_id"]):
            return False
        updated = as_utc(rec.get("updated_at"))
        if updated is None:
            return True
        return updated < self.started_at or datetime.now(timezone.utc) - updated > STALE_AFTER

    def retry_info(self, rec: dict[str, Any] | None) -> dict[str, Any] | None:
        if not rec or self.is_running(rec["message_id"]) or rec["status"] not in ("failed", "manual"):
            return None
        events = self.store.audit_events(rec["message_id"])
        starts = [i for i, e in enumerate(events) if e["event"] in ("ingested", "reprocess")]
        last_run = events[starts[-1]:] if starts else events
        reason = None
        if rec["status"] == "failed":
            err = next((e for e in reversed(last_run) if e["event"] == "error"), None)
            d = (err or {}).get("detail") or {}
            reason = "處理時程式發生錯誤" + (f"（{d.get('error')}" + (f"：{d['message']}" if d.get("message") else "") + "）"
                                       if d.get("error") else "")
        else:
            for e in last_run:
                d = e.get("detail") or {}
                if e["event"] == "extract_failed" or (e["event"] == "manual_case" and any(
                        str(r).startswith("LLM 擷取失敗") for r in d.get("reasons", []))):
                    reason = "LLM 讀信失敗" + (f"：{d['error']}" if d.get("error") else "")
                    break
            if reason is None:
                return None
        attempts = sum(1 for e in events if e["event"] == "auto_retry")
        last_at = as_utc(last_run[-1]["at"]) if last_run else as_utc(rec.get("updated_at"))
        next_at = None
        if attempts < len(self.retry_delays) and last_at is not None:
            next_at = last_at + self.retry_delays[attempts]
        return {"attempts": attempts, "max": len(self.retry_delays), "next_at": next_at, "reason": reason}

    def retry_failed(self, now: datetime | None = None) -> list[dict[str, Any]]:
        now = now or datetime.now(timezone.utc)
        out = []
        recs = self.store.list_messages(status="failed", limit=200) + self.store.list_messages(status="manual", limit=200)
        for rec in recs:
            info = self.retry_info(rec)
            if not info or info["next_at"] is None or info["next_at"] > now:
                continue
            email = self.store.get_email(rec["message_id"])
            if not email or not email.get("raw"):
                continue
            self.store.audit(rec["message_id"], "auto_retry",
                             {"attempt": info["attempts"] + 1, "of": info["max"], "reason": info["reason"]})
            try:
                out.append(self.reprocess(email["raw"]))
            except Exception as e:  # noqa: BLE001
                log.warning("自動重試 %s 失敗：%s", rec["message_id"], e)
                out.append({"status": "failed", "message_id": rec["message_id"]})
        return out

    def recover_interrupted(self) -> list[dict[str, Any]]:
        out = []
        for st in IN_PROGRESS:
            for rec in self.store.list_messages(status=st, limit=200):
                if not self.is_interrupted(rec):
                    continue
                email = self.store.get_email(rec["message_id"])
                if not email or not email.get("raw"):
                    continue
                self.store.audit(rec["message_id"], "interrupted", {"previous_status": rec["status"]})
                try:
                    out.append(self.reprocess(email["raw"]))
                except Exception as e:  # noqa: BLE001
                    log.warning("重新分析中斷的信件 %s 失敗：%s", rec["message_id"], e)
        return out

    def process_eml(self, raw: bytes) -> dict[str, Any]:
        em = None
        try:
            em = parse_eml(raw)
            mid = em.message_id
        except InvalidEmail:
            mid = "invalid-" + hashlib.sha256(raw).hexdigest()[:24]
        thread = thread_for(mid)
        if not self.store.claim_message(mid, thread):
            prev = self.store.get_message(mid)
            if em is not None and self.is_interrupted(prev):
                self.store.audit(mid, "interrupted", {"previous_status": prev["status"]})
                self.store.save_email(mid, raw, sender=em.sender if em else "", subject=em.subject if em else "",
                                      sent_at=em.sent_at if em else None, body=em.body if em else "")
                return self.reprocess(raw)
            self.store.audit(mid, "duplicate_ignored", {"previous_status": prev["status"] if prev else None})
            return {"status": "duplicate", "message_id": mid, "thread_id": thread,
                    "previous": prev and {k: prev[k] for k in ("status", "route", "case_id")}}
        self.store.save_email(mid, raw, sender=em.sender if em else "", subject=em.subject if em else "",
                              sent_at=em.sent_at if em else None, body=em.body if em else "")
        return self._run(mid, thread, {"raw_eml_b64": base64.b64encode(raw).decode(), "message_id": mid})

    def _run(self, mid: str, thread: str, inp: Any) -> dict[str, Any]:
        cfg = {"configurable": {"thread_id": thread}}
        with self._running_lock:
            if mid in self._running:
                raise RuntimeError("這封信正在處理中，請稍後再試")
            self._running.add(mid)
        try:
            self.graph.invoke(inp, cfg)
        except Exception as e:  # noqa: BLE001
            self.store.set_status(mid, "failed")
            self.store.audit(mid, "error", {"error": type(e).__name__, "message": str(e)[:200]})
            raise
        finally:
            with self._running_lock:
                self._running.discard(mid)
        snap = self.graph.get_state(cfg)
        st = snap.values
        route = (st.get("decision") or {}).get("route") or ("stop" if st.get("stop_reason") else None)
        if snap.next:
            self.store.set_status(mid, "waiting_review", route=route, case_id=st.get("case_id")
                                  or _interrupt_case(snap))
            return {"status": "waiting_review", "message_id": mid, "thread_id": thread,
                    "case_id": _interrupt_case(snap), "route": route,
                    "reasons": (st.get("decision") or {}).get("reasons", []), "state": st}
        result = st.get("result") or {"status": "ended", "review_status": st.get("review_status")}
        status = {"applied": "done", "already_applied": "done", "nothing_to_apply": "done", "manual": "manual",
                  "failed": "failed"}.get(result.get("status"), result.get("status", "ended"))
        if st.get("review_status") == CaseStatus.REJECTED.value:
            status = "rejected"
        self.store.set_status(mid, status, route=route, case_id=st.get("case_id"))
        return {"status": status, "message_id": mid, "thread_id": thread, "route": route,
                "case_id": st.get("case_id"), "result": result, "state": st}

    def resume(self, thread_id: str, user: str) -> dict[str, Any]:
        rec = self.store.by_thread(thread_id)
        if rec is None:
            raise LookupError("找不到流程")
        if "Purchase Manager" not in self.erp.user_roles(user):
            self.store.audit(rec["message_id"], "resume_denied", {"user": user}, actor=user)
            raise AccessDenied(f"{user} 無權恢復此流程")
        cfg = {"configurable": {"thread_id": thread_id}}
        snap = self.graph.get_state(cfg)
        if not snap.next:
            raise ValueError("流程未在等待審核")
        case_id = _interrupt_case(snap)
        case = self.erp.get_case(case_id)
        if case is None or case.status == CaseStatus.PENDING:
            raise ValueError("案件尚未核准或拒絕，不得恢復")
        return self._run(rec["message_id"], thread_id, Command(resume={"user": user}))

    def view(self, message_id: str) -> dict[str, Any] | None:
        rec = self.store.get_message(message_id)
        if rec is None:
            return None
        snap = self.graph.get_state({"configurable": {"thread_id": rec["thread_id"]}})
        case = _interrupt_case(snap) if snap.next else None
        return {"record": rec, "state": snap.values or {}, "waiting": case is not None,
                "interrupt_case": case,
                "interrupted": self.is_interrupted(rec), "retry": self.retry_info(rec)}

    def reprocess(self, raw: bytes) -> dict[str, Any]:
        mid = parse_eml(raw).message_id
        if self.store.get_message(mid) is None:
            raise LookupError("此信件從未處理過，請用 process_eml")
        if self.is_running(mid):
            raise RuntimeError("這封信正在處理中，請稍後再試")
        n = 1 + sum(1 for e in self.store.audit_events(mid) if e["event"] == "reprocess")
        thread = thread_for(mid, n)
        self.store.audit(mid, "reprocess", {"attempt": n})
        self.store.set_status(mid, "reprocessing", thread_id=thread)
        return self._run(mid, thread, {"raw_eml_b64": base64.b64encode(raw).decode(), "message_id": mid})


def _interrupt_case(snap: Any) -> str | None:
    for t in snap.tasks or []:
        for it in getattr(t, "interrupts", []) or []:
            v = getattr(it, "value", None)
            if isinstance(v, dict) and "case_id" in v:
                return v["case_id"]
    return None


def checkpoint_path(database_url: str):
    from pathlib import Path
    db = Path(database_url[len("sqlite:///"):])
    return db.with_name(db.stem + "_checkpoints.db")


def make_agent(erp: Any = None, store: Store | None = None, extractor: Any = None, checkpointer: Any = None) -> Agent:
    s = settings()
    if erp is None:
        from .erp import make_erp
        erp = make_erp()
    store = store or Store(s.database_url)
    if extractor is None:
        extractor = make_extractor(s.extractor, s.llm_model, base_url=s.ollama_base_url)
    if checkpointer is None:
        if s.database_url.startswith("sqlite:///") and s.database_url != "sqlite:///:memory:":
            import sqlite3

            from langgraph.checkpoint.sqlite import SqliteSaver
            path = checkpoint_path(s.database_url)
            path.parent.mkdir(parents=True, exist_ok=True)
            checkpointer = SqliteSaver(sqlite3.connect(str(path), check_same_thread=False))
        elif s.database_url.startswith("postgresql"):
            from langgraph.checkpoint.postgres import PostgresSaver
            from psycopg import Connection
            conn = Connection.connect(s.database_url, autocommit=True, prepare_threshold=0)
            checkpointer = PostgresSaver(conn)
            checkpointer.setup()
        else:
            from langgraph.checkpoint.memory import InMemorySaver
            checkpointer = InMemorySaver()
    return Agent(erp, store, extractor, checkpointer)
