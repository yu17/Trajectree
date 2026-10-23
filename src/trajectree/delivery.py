"""Delivery of a routine's result: nowhere, as a pending inbox note, to the log, or to chatmaid."""

from __future__ import annotations

import logging
from datetime import datetime

import httpx
from pydantic import BaseModel

from trajectree.config import Settings
from trajectree.db.ledger import Ledger
from trajectree.models import Actor, Delivery, EventIn
from trajectree.repo.events import create_event
from trajectree.repo.projects import get_project_by_slug

log = logging.getLogger("trajectree.delivery")


class DeliveryMessage(BaseModel):
    title: str
    body_md: str
    project_id: str | None
    run_id: str
    routine: str


class DeliveryResult(BaseModel):
    channel: Delivery
    ok: bool
    detail: str | None = None
    event_id: str | None = None


def deliver(
    ledger: Ledger,
    settings: Settings,
    channel: Delivery,
    message: DeliveryMessage,
    *,
    actor: Actor,
    now: datetime,
    post=httpx.post,
) -> DeliveryResult:
    if channel == "none":
        return DeliveryResult(channel="none", ok=True)
    if channel == "log":
        return _log(message)
    if channel == "chatmaid":
        sent = _chatmaid(settings, message, post=post)
        if sent.ok:
            return sent
        fallback = _inbox(ledger, message, actor=actor, now=now)
        detail = "; ".join(d for d in (sent.detail, fallback.detail) if d)
        return fallback.model_copy(update={"detail": detail})
    return _inbox(ledger, message, actor=actor, now=now)


def _log(message: DeliveryMessage, detail: str | None = None) -> DeliveryResult:
    log.info("routine %s run %s: %s", message.routine, message.run_id, message.title)
    return DeliveryResult(channel="log", ok=True, detail=detail)


def _inbox(ledger: Ledger, message: DeliveryMessage, *, actor: Actor, now: datetime) -> DeliveryResult:
    project_id = message.project_id
    if project_id is None:
        with ledger.locked() as conn:
            inbox = get_project_by_slug(conn, "inbox")
        project_id = inbox.id if inbox is not None else None
    if project_id is None:
        return _log(message, "no project for inbox delivery")
    event = create_event(
        ledger,
        actor,
        EventIn(
            project_id=project_id,
            kind="note",
            title=message.title,
            body_md=message.body_md,
            t_start=now,
            status="happened",
            source="agent",
        ),
        intent=f"deliver {message.routine} result",
    )
    return DeliveryResult(channel="inbox", ok=True, event_id=event.id)


def _chatmaid(settings: Settings, message: DeliveryMessage, *, post) -> DeliveryResult:
    """Placeholder until the chatmaid contract lands; callers fall back to the inbox."""
    return DeliveryResult(channel="chatmaid", ok=False, detail="chatmaid delivery not configured")
