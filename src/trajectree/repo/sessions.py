"""Session repository: registry of running Claude Code sessions, kept outside the ledger."""

from datetime import datetime
from typing import Any

import apsw

from trajectree.db.schema import table_columns
from trajectree.models import SessionRow
from trajectree.repo._rows import select_row, select_rows
from trajectree.timeutil import to_db


def _to_session(conn: apsw.Connection, row: tuple[Any, ...]) -> SessionRow:
    return SessionRow.model_validate(dict(zip(table_columns(conn, "session"), row)))


def _get(conn: apsw.Connection, session_id: str) -> SessionRow | None:
    row = select_row(conn, "session", session_id)
    return None if row is None else _to_session(conn, row)


def start_session(
    conn: apsw.Connection,
    *,
    session_id: str,
    cwd: str,
    transcript_path: str | None,
    project_id: str | None,
    source: str | None,
    backend: str = "claude",
    now: datetime,
) -> SessionRow:
    """Register a session, or reopen a known one keeping its original `started_at`."""
    ts = to_db(now)
    conn.execute(
        "INSERT INTO session (id, cwd, project_id, transcript_path, backend, started_at, last_seen, source)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(id) DO UPDATE SET cwd = excluded.cwd, transcript_path = excluded.transcript_path,"
        " project_id = excluded.project_id, source = excluded.source, last_seen = excluded.last_seen,"
        " ended_at = NULL, end_reason = NULL",
        (session_id, cwd, project_id, transcript_path, backend, ts, ts, source),
    )
    session = _get(conn, session_id)
    assert session is not None
    return session


def touch_session(
    conn: apsw.Connection, *, session_id: str, cwd: str, project_id: str | None, now: datetime
) -> SessionRow:
    """Record activity; a session never seen before is created with source 'stop'."""
    ts = to_db(now)
    conn.execute(
        "INSERT INTO session (id, cwd, project_id, started_at, last_seen, source)"
        " VALUES (?, ?, ?, ?, ?, 'stop')"
        " ON CONFLICT(id) DO UPDATE SET last_seen = excluded.last_seen, cwd = excluded.cwd,"
        " project_id = excluded.project_id, ended_at = NULL, end_reason = NULL",
        (session_id, cwd, project_id, ts, ts),
    )
    session = _get(conn, session_id)
    assert session is not None
    return session


def end_session(
    conn: apsw.Connection, *, session_id: str, reason: str, now: datetime
) -> SessionRow | None:
    if select_row(conn, "session", session_id) is None:
        return None
    conn.execute(
        "UPDATE session SET ended_at = ?, end_reason = ? WHERE id = ?",
        (to_db(now), reason, session_id),
    )
    return _get(conn, session_id)


def running_sessions(conn: apsw.Connection, *, since: datetime) -> list[SessionRow]:
    rows = select_rows(
        conn, "session", ["ended_at IS NULL", "last_seen >= ?"], [to_db(since)], "last_seen DESC, id"
    )
    return [_to_session(conn, r) for r in rows]


def session_for_cwd(conn: apsw.Connection, *, cwd: str, since: datetime) -> SessionRow | None:
    rows = select_rows(
        conn,
        "session",
        ["ended_at IS NULL", "last_seen >= ?", "cwd = ?"],
        [to_db(since), cwd],
        "last_seen DESC, id LIMIT 1",
    )
    return _to_session(conn, rows[0]) if rows else None


def ended_session_ids(conn: apsw.Connection, ids: list[str]) -> set[str]:
    if not ids:
        return set()
    marks = ", ".join("?" * len(ids))
    return {
        r[0]
        for r in conn.execute(f"SELECT id FROM session WHERE ended_at IS NOT NULL AND id IN ({marks})", ids)
    }
