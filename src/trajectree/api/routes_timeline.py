from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, Query
from pydantic import AwareDatetime, BaseModel

from trajectree.api.deps import get_ledger, get_settings
from trajectree.api.routes_entities import _csv
from trajectree.config import Settings
from trajectree.connectors.claude_sessions import LiveSession
from trajectree.db.ledger import Ledger
from trajectree.models import Edge, Event, Project, StripBin, Todo
from trajectree.repo import edges, events, projects, todos
from trajectree.repo.review import pending_txns
from trajectree.repo.sessions import live_sessions
from trajectree.repo.signals import stale_projects, strip_bins
from trajectree.repo.state import get_state
from trajectree.timeutil import utcnow

router = APIRouter()

_OPEN_STATES = ("open", "doing")


class TimelineResponse(BaseModel):
    projects: list[Project]
    events: list[Event]
    todos: list[Todo]
    edges: list[Edge]
    pending: dict[str, dict[str, str]]
    strip: list[StripBin]
    live: list[LiveSession]
    stale: list[str]


@router.get("/timeline",response_model=TimelineResponse)
def timeline(
    t_from: AwareDatetime = Query(alias="from"),
    t_to: AwareDatetime = Query(alias="to"),
    project_filter: str | None = Query(default=None, alias="projects"),
    ledger: Ledger = Depends(get_ledger),
    settings: Settings = Depends(get_settings),
):
    ids = _csv(project_filter)
    with ledger.locked() as conn:
        project_rows = [p for p in projects.list_projects(conn) if ids is None or p.id in ids]
        event_rows = events.list_events(conn, project_ids=ids, t_from=t_from, t_to=t_to)
        todo_rows = [
            t
            for t in todos.list_todos(conn, project_ids=ids)
            if (t.due is None and t.state in _OPEN_STATES)
            or (t.due is not None and t_from <= t.due <= t_to)
        ]
        event_ids = [e.id for e in event_rows]
        edge_rows = edges.list_edges(conn, event_ids=event_ids) if event_ids else []
        returned = {
            "event": set(event_ids),
            "todo": {t.id for t in todo_rows},
            "edge": {e.id for e in edge_rows},
        }
        pending = {
            kind: {row: txn for row, txn in sorted(pending_txns(conn, kind).items()) if row in got}
            for kind, got in returned.items()
        }
        shown = {p.id for p in project_rows}
        now = utcnow()
        strip = strip_bins(conn, project_ids=ids, t_from=t_from, t_to=t_to)
        live = live_sessions(
            conn,
            scan_live=get_state(conn, "live_sessions"),
            shown=shown,
            now=now,
            window_s=settings.live_window_s,
        )
        # Computed live so a new summary clears the flag at once; the ingest job's
        # `stale_projects` state only feeds its report.
        settled_before = now - timedelta(hours=settings.stale_settle_h)
        stale = [pid for pid in stale_projects(conn, settled_before=settled_before) if pid in shown]
    return TimelineResponse(
        projects=project_rows,
        events=event_rows,
        todos=todo_rows,
        edges=edge_rows,
        pending=pending,
        strip=strip,
        live=live,
        stale=stale,
    )
