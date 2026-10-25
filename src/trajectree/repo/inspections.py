"""Inspection watermark and due projects.

The watermark is machine-written and lives in the untracked `state` table (key `inspect:<project id>`),
so advancing it creates no ledger transaction.
"""

import json
from datetime import datetime, timedelta
from typing import Any

import apsw

from trajectree.config import Settings
from trajectree.context.stretches import merge_stretches
from trajectree.db.schema import table_columns
from trajectree.models import InspectionDue, Project, Signal, Stretch, Watermark
from trajectree.repo import projects
from trajectree.repo.sessions import running_sessions
from trajectree.repo.state import get_state, set_state
from trajectree.timeutil import parse_iso, to_db

STATE_PREFIX = "inspect:"
_KINDS = ["session", "commits", "files_changed"]


def get_watermark(conn: apsw.Connection, project_id: str) -> Watermark:
    raw = get_state(conn, STATE_PREFIX + project_id)
    if not isinstance(raw, dict):
        return Watermark()
    try:
        return Watermark.model_validate(raw)
    except ValueError:
        return Watermark()


def set_watermark(conn: apsw.Connection, project_id: str, wm: Watermark) -> None:
    with conn:
        set_state(conn, STATE_PREFIX + project_id, wm.model_dump(mode="json"))


def _end_text(value: str | datetime) -> str:
    """A signal end in the stored text form, so ISO spellings of one instant compare equal."""
    try:
        return to_db(value if isinstance(value, datetime) else parse_iso(value))
    except ValueError:
        return str(value)


def uninspected_signals(conn: apsw.Connection, project_id: str, wm: Watermark) -> list[Signal]:
    """Activity signals the last inspection did not cover, oldest first; never truncated.

    With an empty watermark every signal counts. Otherwise a signal counts when it ends after
    `inspected_at`, when its key is not in `covered`, or when `covered` holds a different end for it.
    """
    cols = table_columns(conn, "signal")
    marks = ", ".join("?" * len(_KINDS))
    base = f"FROM signal WHERE project_id = ? AND kind IN ({marks})"
    params: list[Any] = [project_id, *_KINDS]
    if wm.inspected_at is None:
        sql = f"SELECT {', '.join(cols)} {base} ORDER BY t_start, id"
        rows = list(conn.execute(sql, params))
    else:
        mark = to_db(wm.inspected_at)
        end = "coalesce(t_end, t_start)"
        covered = {k: _end_text(v) for k, v in wm.covered.items()}
        ids = [
            r[0]
            for r in conn.execute(f"SELECT id, dedupe_key, {end} {base} AND {end} <= ?", [*params, mark])
            if covered.get(r[1]) != _end_text(r[2])
        ]
        rows = list(
            conn.execute(
                f"SELECT {', '.join(cols)} {base} AND {end} > ? ORDER BY t_start, id", [*params, mark]
            )
        )
        for i in range(0, len(ids), 500):
            chunk = ids[i : i + 500]
            rows += conn.execute(
                f"SELECT {', '.join(cols)} FROM signal WHERE id IN ({', '.join('?' * len(chunk))})", chunk
            ).fetchall()
        rows.sort(key=lambda r: (r[cols.index("t_start")], r[cols.index("id")]))
    out = []
    for row in rows:
        data = dict(zip(cols, row))
        data["payload"] = json.loads(data["payload"])
        out.append(Signal.model_validate(data))
    return out


def project_is_live(conn: apsw.Connection, settings: Settings, project_id: str, *, now: datetime) -> bool:
    since = now - timedelta(seconds=settings.live_window_s)
    if any(s.project_id == project_id for s in running_sessions(conn, since=since)):
        return True
    scanned = get_state(conn, "live_sessions")
    return isinstance(scanned, list) and any(
        isinstance(e, dict) and e.get("project_id") == project_id for e in scanned
    )


def uninspected_stretches(
    conn: apsw.Connection, settings: Settings, project: Project, *, now: datetime
) -> list[Stretch]:
    """Settled stretches (quiet for a full gap) not yet inspected; none while a session is live."""
    if project_is_live(conn, settings, project.id, now=now):
        return []
    wm = get_watermark(conn, project.id)
    stretches = merge_stretches(
        uninspected_signals(conn, project.id, wm), gap_s=settings.segment_gap_s, inspected_at=wm.inspected_at
    )
    settled = now - timedelta(seconds=settings.segment_gap_s)
    return [s for s in stretches if s.t_end <= settled]


def due_projects(conn: apsw.Connection, settings: Settings, *, now: datetime) -> list[InspectionDue]:
    out: list[InspectionDue] = []
    for p in projects.list_projects(conn, status="active"):
        st = uninspected_stretches(conn, settings, p, now=now)
        if not st:
            continue
        out.append(
            InspectionDue(
                project_id=p.id,
                slug=p.slug,
                title=p.title,
                stretches=len(st),
                first_day=st[0].t_start.strftime("%Y-%m-%d"),
                last_day=st[-1].t_end.strftime("%Y-%m-%d"),
                sessions=sum(s.sessions for s in st),
                commits=sum(s.commits for s in st),
                file_days=sum(s.file_days for s in st),
            )
        )
    return out
