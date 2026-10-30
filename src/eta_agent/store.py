from __future__ import annotations

import json
from datetime import date, datetime, timezone
from typing import Any

from pathlib import Path

from sqlalchemy import (JSON, Column, DateTime, Integer, LargeBinary, MetaData, String, Table, Text,
                        create_engine, desc, insert, select, update)
from sqlalchemy.exc import IntegrityError

from .authz import redact_for_log

meta = MetaData()
messages = Table(
    "processed_messages", meta,
    Column("message_id", String(512), primary_key=True),
    Column("thread_id", String(128), nullable=False),
    Column("status", String(32), nullable=False),
    Column("route", String(16)),
    Column("case_id", String(64)),
    Column("updated_at", DateTime(timezone=True)),
)
emails = Table(
    "email_contents", meta,
    Column("message_id", String(512), primary_key=True),
    Column("sender", String(320)),
    Column("subject", Text),
    Column("sent_at", DateTime(timezone=True)),
    Column("body", Text),
    Column("raw", LargeBinary),
    Column("received_at", DateTime(timezone=True)),
)
audit = Table(
    "audit_events", meta,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("at", DateTime(timezone=True), nullable=False),
    Column("message_id", String(512)),
    Column("event", String(64), nullable=False),
    Column("actor", String(128)),
    Column("detail", JSON),
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(v: datetime | None) -> datetime | None:
    if v is None or v.tzinfo is not None:
        return v
    return v.replace(tzinfo=timezone.utc)


class Store:
    def __init__(self, url: str):
        if url.startswith("postgresql://"):
            url = url.replace("postgresql://", "postgresql+psycopg://", 1)
        if url in ("sqlite://", "sqlite:///:memory:"):
            from sqlalchemy.pool import StaticPool
            self.engine = create_engine(url, connect_args={"check_same_thread": False}, poolclass=StaticPool)
        else:
            if url.startswith("sqlite:///"):
                Path(url[len("sqlite:///"):]).parent.mkdir(parents=True, exist_ok=True)
            self.engine = create_engine(url, future=True)
        meta.create_all(self.engine)

    def claim_message(self, message_id: str, thread_id: str) -> bool:
        try:
            with self.engine.begin() as c:
                c.execute(insert(messages).values(message_id=message_id, thread_id=thread_id,
                                                  status="processing", updated_at=_now()))
            return True
        except IntegrityError:
            return False

    def get_message(self, message_id: str) -> dict[str, Any] | None:
        with self.engine.connect() as c:
            r = c.execute(select(messages).where(messages.c.message_id == message_id)).mappings().first()
            return dict(r) if r else None

    def by_thread(self, thread_id: str) -> dict[str, Any] | None:
        with self.engine.connect() as c:
            r = c.execute(select(messages).where(messages.c.thread_id == thread_id)).mappings().first()
            return dict(r) if r else None

    def set_status(self, message_id: str, status: str, **kw: Any) -> None:
        with self.engine.begin() as c:
            c.execute(update(messages).where(messages.c.message_id == message_id)
                      .values(status=status, updated_at=_now(), **kw))

    def list_messages(self, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        q = select(messages).order_by(desc(messages.c.updated_at)).limit(limit)
        if status:
            q = q.where(messages.c.status == status)
        with self.engine.connect() as c:
            return [dict(r) for r in c.execute(q).mappings()]

    def save_email(self, message_id: str, raw: bytes, sender: str = "", subject: str = "",
                   sent_at: datetime | None = None, body: str = "") -> None:
        try:
            with self.engine.begin() as c:
                c.execute(insert(emails).values(message_id=message_id, sender=sender, subject=subject,
                                                sent_at=sent_at, body=body, raw=raw, received_at=_now()))
        except IntegrityError:
            pass

    def get_email(self, message_id: str) -> dict[str, Any] | None:
        with self.engine.connect() as c:
            r = c.execute(select(emails).where(emails.c.message_id == message_id)).mappings().first()
            return dict(r) if r else None

    def schedule_baseline(self, line: str, current: date) -> date | None:
        baseline, last_after = None, None
        with self.engine.connect() as c:
            rows = c.execute(select(audit.c.detail).where(audit.c.event == "applied").order_by(audit.c.id)).all()
        for (detail,) in rows:
            for ch in (detail or {}).get("changes") or []:
                if ch.get("name") != line or "schedule_date" not in (ch.get("after") or {}):
                    continue
                before = (ch.get("before") or {}).get("schedule_date")
                after = ch["after"]["schedule_date"]
                if baseline is None and before:
                    baseline = before
                if detail.get("mode") == "approved":
                    baseline = after
                last_after = after
        if baseline is None or last_after != current.isoformat():
            return None
        return date.fromisoformat(str(baseline)[:10])

    def audit(self, message_id: str | None, event: str, detail: Any, actor: str = "eta-agent") -> None:
        safe = json.loads(json.dumps(redact_for_log(detail), default=str, ensure_ascii=False))
        with self.engine.begin() as c:
            c.execute(insert(audit).values(at=_now(), message_id=message_id, event=event, actor=actor,
                                           detail=safe))

    def audit_events(self, message_id: str | None = None) -> list[dict[str, Any]]:
        q = select(audit).order_by(audit.c.id)
        if message_id:
            q = q.where(audit.c.message_id == message_id)
        with self.engine.connect() as c:
            return [dict(r) for r in c.execute(q).mappings()]
