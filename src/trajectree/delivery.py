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
from trajectree.repo.projects import get_project, get_project_by_slug
from trajectree.runner.credentials import load_connector_token

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
        sent = _chatmaid(ledger, settings, message, post=post)
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


def _chatmaid(ledger: Ledger, settings: Settings, message: DeliveryMessage, *, post) -> DeliveryResult:
    """POST the result to chatmaid; a failed result tells the caller to fall back to the inbox."""
    token = load_connector_token(settings, "chatmaid")
    if not settings.chatmaid_url or token is None:
        return DeliveryResult(channel="chatmaid", ok=False, detail="chatmaid delivery not configured")
    slug = None
    if message.project_id is not None:
        with ledger.locked() as conn:
            project = get_project(conn, message.project_id)
        slug = project.slug if project is not None else None
    payload = {
        "title": message.title,
        "body_md": message.body_md,
        "project": slug,
        "run_id": message.run_id,
        "routine": message.routine,
    }
    try:
        response = post(
            f"{settings.chatmaid_url.rstrip('/')}/deliver",
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
            timeout=20,
        )
    except (httpx.HTTPError, OSError) as exc:
        return DeliveryResult(channel="chatmaid", ok=False, detail=f"chatmaid: {type(exc).__name__}: {exc}")
    if not 200 <= response.status_code < 300:
        return DeliveryResult(channel="chatmaid", ok=False, detail=f"chatmaid: {response.status_code}")
    return DeliveryResult(channel="chatmaid", ok=True)
