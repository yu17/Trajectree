from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import AwareDatetime, BaseModel, ValidationError

from trajectree.api.deps import get_ledger
from trajectree.api.routes_entities import _csv
from trajectree.connectors.claude_sessions import LiveSession
from trajectree.db.ledger import Ledger
from trajectree.models import Edge, Event, Project, StripBin, Todo
from trajectree.repo import edges, events, projects, todos
from trajectree.repo.review import pending_txns
from trajectree.repo.signals import strip_bins
from trajectree.repo.state import get_state

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


def _live_from_state(raw: Any, shown: set[str]) -> list[LiveSession]:
    """Stop-gap: validate stored live sessions, skipping malformed entries."""
    live: list[LiveSession] = []
    for entry in raw or []:
        try:
            session = LiveSession.model_validate(entry)
        except ValidationError:
            continue
        if session.project_id in shown:
            live.append(session)
    return live


@router.get("/timeline",response_model=TimelineResponse)
def timeline(
    t_from: AwareDatetime = Query(alias="from"),
    t_to: AwareDatetime = Query(alias="to"),
    project_filter: str | None = Query(default=None, alias="projects"),
    ledger: Ledger = Depends(get_ledger),
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
        strip = strip_bins(conn, project_ids=ids, t_from=t_from, t_to=t_to)
        live = _live_from_state(get_state(conn, "live_sessions"), shown)
        stale = [pid for pid in (get_state(conn, "stale_projects") or []) if pid in shown]
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
