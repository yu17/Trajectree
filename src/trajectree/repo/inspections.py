"""Inspection watermark and due projects.

The watermark is machine-written and lives in the untracked `state` table (key `inspect:<project id>`),
so advancing it creates no ledger transaction.
"""

from datetime import datetime, timedelta

import apsw

from trajectree.config import Settings
from trajectree.context.stretches import merge_stretches
from trajectree.models import InspectionDue, Project, Signal, Stretch, Watermark
from trajectree.repo import projects
from trajectree.repo.sessions import running_sessions
from trajectree.repo.signals import list_signals
from trajectree.repo.state import get_state, set_state

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


def uninspected_signals(conn: apsw.Connection, project_id: str, wm: Watermark) -> list[Signal]:
    """Activity signals that end after the inspected mark or changed since the last inspection began."""
    rows = list_signals(conn, kinds=_KINDS, project_ids=[project_id], limit=5000)
    if wm.inspected_at is None:
        return rows
    return [
        s
        for s in rows
        if (s.t_end or s.t_start) > wm.inspected_at
        or (wm.started_at is not None and s.updated_at > wm.started_at)
    ]


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
