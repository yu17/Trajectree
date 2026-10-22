"""Read-only MCP tools. Every read runs under the ledger lock."""

from __future__ import annotations

from datetime import timedelta
from typing import Annotated

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from pydantic import BaseModel, Field

from trajectree.context.pack import ContextPack, build_context_pack
from trajectree.db import fts
from trajectree.db.fts import SearchHit
from trajectree.mcpserver.server import ToolDeps, parse_when, resolve_project
from trajectree.models import Edge, Event, Project, Resource, Signal, Todo
from trajectree.repo import edges, events, projects, resources, signals, todos
from trajectree.repo import projects as projects_repo
from trajectree.timeutil import utcnow

OPEN_STATES = ["open", "doing"]
RECENT_EVENTS = 20
WINDOW_BEFORE = timedelta(days=90)
WINDOW_AFTER = timedelta(days=30)


class ProjectDetail(BaseModel):
    project: Project
    open_todos: list[Todo]
    resources: list[Resource]
    recent_events: list[Event]


class TimelineOut(BaseModel):
    events: list[Event]
    todos: list[Todo]
    edges: list[Edge]


def register_read_tools(mcp: MCPServer, deps: ToolDeps) -> None:
    # deps.resolve(ctx) authenticates the caller; reads are not limited by scope.

    @mcp.tool()
    async def list_projects(ctx: Context, status: str | None = None) -> list[Project]:
        """List all projects (lanes) in lane order, optionally only those with `status`
        (active, paused, done or archived)."""
        deps.resolve(ctx)
        with deps.ledger.locked() as conn:
            return projects.list_projects(conn, status=status)

    @mcp.tool()
    async def get_project(project: str, ctx: Context) -> ProjectDetail:
        """Return one project by slug with its open todos, its resources and its 20 most recent
        events, newest first."""
        deps.resolve(ctx)
        with deps.ledger.locked() as conn:
            p = resolve_project(conn, project, None)
            recent = sorted(
                events.list_events(conn, project_ids=[p.id]), key=lambda e: e.t_start, reverse=True
            )[:RECENT_EVENTS]
            return ProjectDetail(
                project=p,
                open_todos=todos.list_todos(conn, project_ids=[p.id], states=OPEN_STATES),
                resources=resources.list_resources(conn, project_ids=[p.id]),
                recent_events=recent,
            )

    @mcp.tool()
    async def timeline(
        ctx: Context,
        projects: list[str] | None = None,
        start: str | None = None,
        end: str | None = None,
        kinds: list[str] | None = None,
    ) -> TimelineOut:
        """Return events overlapping the window from `start` to `end` (ISO 8601; default 90 days ago
        to 30 days ahead), the todos due in it or open without a due date, and the edges between
        those events; `projects` are slugs and `kinds` filters event kinds."""
        deps.resolve(ctx)
        now = utcnow()
        t_from = parse_when(start, now - WINDOW_BEFORE)
        t_to = parse_when(end, now + WINDOW_AFTER)
        with deps.ledger.locked() as conn:
            ids = None if projects is None else [resolve_project(conn, s, None).id for s in projects]
            event_rows = events.list_events(
                conn, project_ids=ids, t_from=t_from, t_to=t_to, kinds=kinds
            )
            todo_rows = [
                t
                for t in todos.list_todos(conn, project_ids=ids)
                if (t.due is None and t.state in OPEN_STATES)
                or (t.due is not None and t_from <= t.due <= t_to)
            ]
            edge_rows = edges.list_edges(conn, event_ids=[e.id for e in event_rows])
            return TimelineOut(events=event_rows, todos=todo_rows, edges=edge_rows)

    @mcp.tool()
    async def search(q: str, ctx: Context, limit: int = 20) -> list[SearchHit]:
        """Full-text search over event, todo and project titles and bodies; returns ranked hits
        with a snippet. Every word in `q` must match."""
        deps.resolve(ctx)
        with deps.ledger.locked() as conn:
            return fts.search(conn, q, limit=limit)

    @mcp.tool()
    async def list_todos(
        ctx: Context, project: str | None = None, states: list[str] | None = None
    ) -> list[Todo]:
        """List todos, soonest due first, for one project (slug) or all projects when omitted;
        `states` defaults to open and doing."""
        deps.resolve(ctx)
        with deps.ledger.locked() as conn:
            ids = None if project is None else [resolve_project(conn, project, None).id]
            return todos.list_todos(conn, project_ids=ids, states=states or OPEN_STATES)

    @mcp.tool()
    async def get_context_pack(
        ctx: Context,
        projects: list[str] | None = None,
        start: str | None = None,
        end: str | None = None,
        token_budget: Annotated[int, Field(ge=200, le=100_000)] = 4000,
        question: str | None = None,
    ) -> ContextPack:
        """Return a token-budgeted markdown digest (summary, events, open todos, resources, recent
        activity) of the given project slugs, default all active projects, for the window from
        `start` to `end` (ISO 8601; default 90 days ago to 30 days ahead). `question` adds
        full-text search hits; `dropped` lists what the budget cut."""
        deps.resolve(ctx)
        now = utcnow()
        t_from = parse_when(start, now - WINDOW_BEFORE)
        t_to = parse_when(end, now + WINDOW_AFTER)
        with deps.ledger.locked() as conn:
            if projects is None:
                ids = [p.id for p in projects_repo.list_projects(conn, status="active")]
            else:
                ids = [resolve_project(conn, s, None).id for s in projects]
            return build_context_pack(
                conn,
                project_ids=ids,
                t_from=t_from,
                t_to=t_to,
                token_budget=token_budget,
                question=question,
            )

    @mcp.tool()
    async def get_signals(
        project: str,
        ctx: Context,
        start: str | None = None,
        end: str | None = None,
        kinds: list[str] | None = None,
        limit: int = 200,
    ) -> list[Signal]:
        """Return a project's (slug) activity signals, oldest first, in the window from `start` to
        `end` (ISO 8601; default 90 days ago to 30 days ahead), optionally only the given kinds."""
        deps.resolve(ctx)
        now = utcnow()
        t_from = parse_when(start, now - WINDOW_BEFORE)
        t_to = parse_when(end, now + WINDOW_AFTER)
        with deps.ledger.locked() as conn:
            p = resolve_project(conn, project, None)
            return signals.list_signals(
                conn, project_ids=[p.id], t_from=t_from, t_to=t_to, kinds=kinds, limit=limit
            )
