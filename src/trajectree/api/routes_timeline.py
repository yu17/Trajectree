from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from pydantic import AwareDatetime, BaseModel

from trajectree.api.deps import get_ledger
from trajectree.api.routes_entities import _csv
from trajectree.db.ledger import Ledger
from trajectree.models import Edge, Event, Project, Todo
from trajectree.repo import edges, events, projects, todos
from trajectree.repo.review import pending_ids

router = APIRouter()

_OPEN_STATES = ("open", "doing")


class TimelineResponse(BaseModel):
    projects: list[Project]
    events: list[Event]
    todos: list[Todo]
    edges: list[Edge]
    pending: dict[str, list[str]]
    live: list = []


@router.get("/timeline", response_model=TimelineResponse)
def timeline(
    t_from: AwareDatetime = Query(alias="from"),
    t_to: AwareDatetime = Query(alias="to"),
    project_filter: str | None = Query(default=None, alias="projects"),
    ledger: Ledger = Depends(get_ledger),
):
    conn = ledger.conn
    ids = _csv(project_filter)
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
    pending = {kind: sorted(pending_ids(conn, kind) & got) for kind, got in returned.items()}
    return TimelineResponse(
        projects=project_rows,
        events=event_rows,
        todos=todo_rows,
        edges=edge_rows,
        pending=pending,
        live=[],
    )
