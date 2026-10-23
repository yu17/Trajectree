"""Claude Code http hook events -> the session registry."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import apsw

from trajectree.models import SessionRow
from trajectree.repo.resources import project_for_path
from trajectree.repo.sessions import end_session, start_session, touch_session

HOOK_TOOL = "hooks"
MAX_FIELD_CHARS = 4096


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and 0 < len(value) <= MAX_FIELD_CHARS else None


def handle_hook(conn: apsw.Connection, payload: Any, *, now: datetime) -> SessionRow | None:
    """Apply one hook event; None when the payload is malformed or the event is not tracked."""
    if not isinstance(payload, dict):
        return None
    event = _text(payload.get("hook_event_name"))
    session_id = _text(payload.get("session_id"))
    if event is None or session_id is None:
        return None
    if event == "SessionEnd":
        reason = payload.get("reason")
        return end_session(
            conn, session_id=session_id, reason=reason if isinstance(reason, str) else None, now=now
        )
    if event not in ("SessionStart", "Stop"):
        return None
    cwd = _text(payload.get("cwd"))
    if cwd is None:
        return None
    project = project_for_path(conn, cwd)
    project_id = project.id if project is not None else None
    if event == "Stop":
        return touch_session(conn, session_id=session_id, cwd=cwd, project_id=project_id, now=now)
    transcript, source = payload.get("transcript_path"), payload.get("source")
    return start_session(
        conn,
        session_id=session_id,
        cwd=cwd,
        transcript_path=transcript if isinstance(transcript, str) else None,
        project_id=project_id,
        source=source if isinstance(source, str) else None,
        now=now,
    )
