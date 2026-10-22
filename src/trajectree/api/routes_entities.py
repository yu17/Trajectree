from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import AwareDatetime

from trajectree.api.deps import actor_from_request, get_ledger
from trajectree.db.ledger import Ledger
from trajectree.models import (
    Edge,
    EdgeIn,
    Event,
    EventIn,
    EventPatch,
    Project,
    ProjectIn,
    ProjectPatch,
    Todo,
    TodoIn,
    TodoPatch,
)
from trajectree.repo import edges, events, projects, todos

router = APIRouter()


class ProjectCreate(ProjectIn):
    intent: str | None = None


class ProjectUpdate(ProjectPatch):
    intent: str | None = None


class EventCreate(EventIn):
    intent: str | None = None


class EventUpdate(EventPatch):
    intent: str | None = None


class TodoCreate(TodoIn):
    intent: str | None = None


class TodoUpdate(TodoPatch):
    intent: str | None = None


class EdgeCreate(EdgeIn):
    intent: str | None = None


def _csv(value: str | None) -> list[str] | None:
    if value is None:
        return None
    return [part.strip() for part in value.split(",") if part.strip()]


def _strip(body, model):
    """Rebuild the plain model from a request wrapper, keeping unset fields unset."""
    return model(**body.model_dump(exclude_unset=True, exclude={"intent"}))


@router.get("/projects", response_model=list[Project])
def list_projects(ledger: Ledger = Depends(get_ledger)):
    return projects.list_projects(ledger.conn)


@router.post("/projects", response_model=Project)
def create_project(body: ProjectCreate, request: Request, ledger: Ledger = Depends(get_ledger)):
    return projects.create_project(
        ledger, actor_from_request(request), _strip(body, ProjectIn), intent=body.intent
    )


@router.patch("/projects/{project_id}", response_model=Project)
def update_project(
    project_id: str, body: ProjectUpdate, request: Request, ledger: Ledger = Depends(get_ledger)
):
    return projects.update_project(
        ledger, actor_from_request(request), project_id, _strip(body, ProjectPatch), intent=body.intent
    )


@router.get("/events", response_model=list[Event])
def list_events(
    projects: str | None = None,
    kinds: str | None = None,
    t_from: AwareDatetime | None = Query(default=None, alias="from"),
    t_to: AwareDatetime | None = Query(default=None, alias="to"),
    ledger: Ledger = Depends(get_ledger),
):
    return events.list_events(
        ledger.conn, project_ids=_csv(projects), t_from=t_from, t_to=t_to, kinds=_csv(kinds)
    )


@router.post("/events", response_model=Event)
def create_event(body: EventCreate, request: Request, ledger: Ledger = Depends(get_ledger)):
    return events.create_event(
        ledger, actor_from_request(request), _strip(body, EventIn), intent=body.intent
    )


@router.patch("/events/{event_id}", response_model=Event)
def update_event(
    event_id: str, body: EventUpdate, request: Request, ledger: Ledger = Depends(get_ledger)
):
    return events.update_event(
        ledger, actor_from_request(request), event_id, _strip(body, EventPatch), intent=body.intent
    )


@router.get("/todos", response_model=list[Todo])
def list_todos(
    projects: str | None = None,
    states: str | None = None,
    ledger: Ledger = Depends(get_ledger),
):
    return todos.list_todos(ledger.conn, project_ids=_csv(projects), states=_csv(states))


@router.post("/todos", response_model=Todo)
def create_todo(body: TodoCreate, request: Request, ledger: Ledger = Depends(get_ledger)):
    return todos.create_todo(
        ledger, actor_from_request(request), _strip(body, TodoIn), intent=body.intent
    )


@router.patch("/todos/{todo_id}", response_model=Todo)
def update_todo(
    todo_id: str, body: TodoUpdate, request: Request, ledger: Ledger = Depends(get_ledger)
):
    return todos.update_todo(
        ledger, actor_from_request(request), todo_id, _strip(body, TodoPatch), intent=body.intent
    )


@router.post("/edges", response_model=Edge)
def create_edge(body: EdgeCreate, request: Request, ledger: Ledger = Depends(get_ledger)):
    return edges.create_edge(
        ledger, actor_from_request(request), _strip(body, EdgeIn), intent=body.intent
    )


@router.delete("/edges/{edge_id}", status_code=204)
def delete_edge(
    edge_id: str, request: Request, intent: str | None = None, ledger: Ledger = Depends(get_ledger)
):
    edges.delete_edge(ledger, actor_from_request(request), edge_id, intent=intent)
    return Response(status_code=204)
