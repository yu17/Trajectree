"""Event repository."""

from datetime import datetime
from typing import Any

import apsw

from trajectree.db.ledger import Ledger
from trajectree.ids import new_id
from trajectree.models import Actor, Event, EventIn, EventPatch
from trajectree.repo import InvalidRange, NotFound
from trajectree.repo._rows import dumps_props, row_to_model, select_row, select_rows, update_fields
from trajectree.timeutil import to_db, utcnow


def get_event(conn: apsw.Connection, event_id: str) -> Event | None:
    row = select_row(conn, "event", event_id)
    return row_to_model(conn, "event", row, Event) if row else None


def list_events(
    conn: apsw.Connection,
    *,
    project_ids: list[str] | None = None,
    t_from: datetime | None = None,
    t_to: datetime | None = None,
    kinds: list[str] | None = None,
) -> list[Event]:
    where: list[str] = []
    params: list[Any] = []
    if project_ids is not None:
        where.append(f"project_id IN ({', '.join('?' * len(project_ids))})")
        params.extend(project_ids)
    if kinds is not None:
        where.append(f"kind IN ({', '.join('?' * len(kinds))})")
        params.extend(kinds)
    if t_to is not None:
        where.append("t_start <= ?")
        params.append(to_db(t_to))
    if t_from is not None:
        where.append("coalesce(t_end, t_start) >= ?")
        params.append(to_db(t_from))
    rows = select_rows(conn, "event", where, params, "t_start")
    return [row_to_model(conn, "event", r, Event) for r in rows]


def create_event(
    ledger: Ledger, actor: Actor, data: EventIn, *, intent: str | None = None
) -> Event:
    event_id = new_id()
    now = to_db(utcnow())
    with ledger.transaction(actor, intent or f"record event: {data.title}") as h:
        if h.conn.execute("SELECT 1 FROM project WHERE id = ?", (data.project_id,)).fetchone() is None:
            raise NotFound(data.project_id)
        h.conn.execute(
            "INSERT INTO event (id, project_id, kind, title, body_md, t_start, t_end, status,"
            " source, props, created_txn, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event_id, data.project_id, data.kind, data.title, data.body_md,
                to_db(data.t_start), to_db(data.t_end) if data.t_end else None,
                data.status, data.source, dumps_props(data.props), h.id, now, now,
            ),
        )
    event = get_event(ledger.conn, event_id)
    assert event is not None
    return event


def update_event(
    ledger: Ledger, actor: Actor, event_id: str, patch: EventPatch, *, intent: str | None = None
) -> Event:
    changes = patch.model_dump(exclude_unset=True)
    fields: dict[str, Any] = dict(changes)
    for key in ("t_start", "t_end"):
        if fields.get(key) is not None:
            fields[key] = to_db(fields[key])
    if "props" in fields:
        fields["props"] = dumps_props(fields["props"])
    fields["updated_at"] = to_db(utcnow())
    with ledger.transaction(actor, intent or f"update event {event_id}") as h:
        row = select_row(h.conn, "event", event_id)
        if row is None:
            raise NotFound(event_id)
        current = row_to_model(h.conn, "event", row, Event)
        t_start = changes.get("t_start", current.t_start)
        t_end = changes["t_end"] if "t_end" in changes else current.t_end
        if t_end is not None and t_end < t_start:
            raise InvalidRange("t_end before t_start")
        update_fields(h.conn, "event", event_id, fields)
    event = get_event(ledger.conn, event_id)
    assert event is not None
    return event
