"""Write MCP tools. Each write is one ledger transaction attributed to the caller's actor.

Preliminary reads (resolving the project, loading the row) run under the ledger lock, which is
released before the repository call; the repositories take the lock themselves. Each tool's
body is blocking (it waits for the ledger lock, which the ingest worker may hold), so it runs in
a worker thread and the event loop stays free.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Annotated

import anyio.to_thread
import apsw
from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from trajectree.auth.scopes import Principal
from trajectree.mcpserver.server import ToolDeps, parse_when, resolve_project
from trajectree.models import (
    Actor,
    Edge,
    EdgeIn,
    EntityType,
    Event,
    EventIn,
    EventPatch,
    EventStatus,
    Project,
    ProjectPatch,
    ProjectStatus,
    Report,
    ReportIn,
    ReportKind,
    ReportPatch,
    ReportScope,
    Resource,
    ResourceIn,
    ResourceKind,
    Source,
    Tier,
    Todo,
    TodoIn,
    TodoPatch,
    TodoState,
)
from trajectree.repo import DuplicateEdge, NotFound, edges, events, projects, reports, resources, runs, todos
from trajectree.timeutil import utcnow

NO_PROJECT = "inbox"

Intent = Annotated[str, Field(min_length=1)]


def source_for(actor: Actor) -> Source:
    if actor.kind == "human":
        return "human"
    if actor.kind == "connector":
        return "connector"
    return "agent"


def _require(principal: Principal, tool: str, project_id: str | None, slug: str) -> None:
    if not principal.scope.allows(tool, project_id, write=True):
        where = NO_PROJECT if project_id is None else slug
        raise ToolError(f"token not permitted: {tool} on {where}")


def _require_projects(conn: apsw.Connection, principal: Principal, tool: str, project_ids: list[str]) -> None:
    """`_require` for each project; with none, only a token not limited to projects may write."""
    if not project_ids:
        if principal.scope.projects is not None:
            raise ToolError(f"token not permitted: {tool} without a project")
        _require(principal, tool, None, NO_PROJECT)
    for project_id in project_ids:
        _require(principal, tool, project_id, _slug_of(conn, project_id))


@contextmanager
def _repo_errors() -> Iterator[None]:
    """Turn repository and validation errors into ToolErrors the model can read."""
    try:
        yield
    except NotFound as exc:
        raise ToolError(f"not found: {exc}") from exc
    except (DuplicateEdge, ValueError) as exc:
        raise ToolError(str(exc)) from exc


def _slug_of(conn: apsw.Connection, project_id: str | None) -> str:
    if project_id is None:
        return NO_PROJECT
    project = projects.get_project(conn, project_id)
    return project.slug if project is not None else project_id


def _endpoint_project(conn: apsw.Connection, kind: EntityType, ref: str) -> str | None:
    """The project an edge endpoint belongs to (a project is its own); ToolError if missing."""
    if kind == "project":
        if projects.get_project(conn, ref) is None:
            raise ToolError(f"link: {kind} '{ref}' not found")
        return ref
    getter = {"event": events.get_event, "todo": todos.get_todo, "resource": resources.get_resource}
    entity = getter[kind](conn, ref)
    if entity is None:
        raise ToolError(f"link: {kind} '{ref}' not found")
    return entity.project_id


def register_write_tools(mcp: MCPServer, deps: ToolDeps) -> None:
    def _record(
        ctx: Context,
        tool: str,
        *,
        project: str | None,
        title: str,
        intent: str,
        kind: str,
        when: str | None,
        end: str | None,
        body_md: str,
        status: EventStatus,
        tier: Tier = "block",
    ) -> Event:
        principal = deps.resolve(ctx)
        with deps.ledger.locked() as conn:
            p = resolve_project(conn, project, deps.cwd_of(ctx))
        _require(principal, tool, p.id, p.slug)
        t_start = parse_when(when, utcnow())
        t_end = parse_when(end, t_start) if end is not None else None
        with _repo_errors():
            data = EventIn(
                project_id=p.id,
                kind=kind,
                title=title,
                body_md=body_md,
                t_start=t_start,
                t_end=t_end,
                status=status,
                tier=tier,
                source=source_for(principal.actor),
            )
            return events.create_event(deps.ledger, principal.actor, data, intent=intent)

    @mcp.tool()
    async def record_event(
        title: str,
        intent: Intent,
        ctx: Context,
        kind: str = "note",
        when: str | None = None,
        end: str | None = None,
        body_md: str = "",
        status: EventStatus = "happened",
        project: str | None = None,
        tier: Tier = "block",
    ) -> Event:
        """Record an event (decision, milestone, note, ...) on a project's timeline; `when`/`end`
        are ISO 8601 (default now). `tier` is "block" for a checkpoint or "pill" for a moment on
        the way to one. `intent` is a one-line reason recorded in the ledger, and the write lands
        pending the owner's review."""

        def body() -> Event:
            return _record(
                ctx, "record_event", project=project, title=title, intent=intent, kind=kind,
                when=when, end=end, body_md=body_md, status=status, tier=tier,
            )

        return await anyio.to_thread.run_sync(body)

    @mcp.tool()
    async def add_note(
        text: str, intent: Intent, ctx: Context, project: str | None = None
    ) -> Event:
        """Add a quick note event, dated now, to a project. `intent` is a one-line reason recorded
        in the ledger, and the write lands pending the owner's review."""

        def body() -> Event:
            return _record(
                ctx, "add_note", project=project, title=text, intent=intent, kind="note",
                when=None, end=None, body_md="", status="happened",
            )

        return await anyio.to_thread.run_sync(body)

    @mcp.tool()
    async def update_event(
        event_id: str,
        intent: Intent,
        ctx: Context,
        title: str | None = None,
        body_md: str | None = None,
        kind: str | None = None,
        status: EventStatus | None = None,
        when: str | None = None,
        end: str | None = None,
        tier: Tier | None = None,
    ) -> Event:
        """Change the given fields of an event; omitted fields stay as they are. `tier` is "block"
        (a checkpoint) or "pill" (a moment on the way to one). `intent` is a one-line reason
        recorded in the ledger, and the write lands pending the owner's review."""

        def body() -> Event:
            principal = deps.resolve(ctx)
            fields: dict[str, object] = {
                k: v
                for k, v in {
                    "title": title, "body_md": body_md, "kind": kind, "status": status,
                    "tier": tier,
                }.items()
                if v is not None
            }
            if when is not None:
                fields["t_start"] = parse_when(when, utcnow())
            if end is not None:
                fields["t_end"] = parse_when(end, utcnow())
            # one lock across check and write, so the row cannot change project in between
            with deps.ledger.locked() as conn:
                current = events.get_event(conn, event_id)
                if current is None:
                    raise ToolError(f"event '{event_id}' not found")
                slug = _slug_of(conn, current.project_id)
                _require(principal, "update_event", current.project_id, slug)
                with _repo_errors():
                    patch = EventPatch(**fields)
                    return events.update_event(
                        deps.ledger, principal.actor, event_id, patch, intent=intent
                    )

        return await anyio.to_thread.run_sync(body)

    @mcp.tool()
    async def add_todo(
        title: str,
        intent: Intent,
        ctx: Context,
        project: str | None = None,
        due: str | None = None,
        body_md: str = "",
        priority: int = 0,
    ) -> Todo:
        """Add an open todo to a project; `due` is ISO 8601. `intent` is a one-line reason
        recorded in the ledger, and the write lands pending the owner's review."""

        def body() -> Todo:
            principal = deps.resolve(ctx)
            with deps.ledger.locked() as conn:
                p = resolve_project(conn, project, deps.cwd_of(ctx))
            _require(principal, "add_todo", p.id, p.slug)
            due_at = parse_when(due, utcnow()) if due is not None else None
            with _repo_errors():
                data = TodoIn(
                    project_id=p.id, title=title, body_md=body_md, due=due_at, priority=priority
                )
                return todos.create_todo(deps.ledger, principal.actor, data, intent=intent)

        return await anyio.to_thread.run_sync(body)

    @mcp.tool()
    async def update_todo(
        todo_id: str,
        intent: Intent,
        ctx: Context,
        title: str | None = None,
        state: TodoState | None = None,
        due: str | None = None,
        body_md: str | None = None,
        priority: int | None = None,
    ) -> Todo:
        """Change the given fields of a todo (e.g. `state` done); omitted fields stay as they are.
        `intent` is a one-line reason recorded in the ledger, and the write lands pending the
        owner's review."""

        def body() -> Todo:
            principal = deps.resolve(ctx)
            fields: dict[str, object] = {
                k: v
                for k, v in {
                    "title": title, "state": state, "body_md": body_md, "priority": priority
                }.items()
                if v is not None
            }
            if due is not None:
                fields["due"] = parse_when(due, utcnow())
            # one lock across check and write, so the row cannot change project in between
            with deps.ledger.locked() as conn:
                current = todos.get_todo(conn, todo_id)
                if current is None:
                    raise ToolError(f"todo '{todo_id}' not found")
                slug = _slug_of(conn, current.project_id)
                _require(principal, "update_todo", current.project_id, slug)
                with _repo_errors():
                    patch = TodoPatch(**fields)
                    return todos.update_todo(
                        deps.ledger, principal.actor, todo_id, patch, intent=intent
                    )

        return await anyio.to_thread.run_sync(body)

    @mcp.tool()
    async def link(
        src_type: EntityType,
        src_id: str,
        dst_type: EntityType,
        dst_id: str,
        rel: str,
        intent: Intent,
        ctx: Context,
    ) -> Edge:
        """Link two entities with a relation: `depends_on`, `derived_from`, `caused`, `relates_to`
        or `part_of` (a pill is `part_of` the block it leads to). `intent` is a one-line reason
        recorded in the ledger, and the write lands pending the owner's review."""

        def body() -> Edge:
            principal = deps.resolve(ctx)
            # one lock across check and write, so an endpoint cannot change project in between
            with deps.ledger.locked() as conn:
                for kind, ref in ((src_type, src_id), (dst_type, dst_id)):
                    project_id = _endpoint_project(conn, kind, ref)
                    _require(principal, "link", project_id, _slug_of(conn, project_id))
                with _repo_errors():
                    data = EdgeIn(
                        src_type=src_type, src_id=src_id, dst_type=dst_type, dst_id=dst_id, rel=rel
                    )
                    return edges.create_edge(deps.ledger, principal.actor, data, intent=intent)

        return await anyio.to_thread.run_sync(body)

    @mcp.tool()
    async def unlink(edge_id: str, intent: Intent, ctx: Context) -> str:
        """Delete an edge (owner sessions only). `intent` is a one-line reason recorded in the
        ledger, and the write lands pending the owner's review."""

        def body() -> str:
            principal = deps.resolve(ctx)
            with deps.ledger.locked():
                if principal.token_id is not None:
                    raise ToolError("unlink is owner-only")
                with _repo_errors():
                    edges.delete_edge(deps.ledger, principal.actor, edge_id, intent=intent)
            return f"unlinked {edge_id}"

        return await anyio.to_thread.run_sync(body)

    def _patch_project(
        ctx: Context, tool: str, project: str, patch: ProjectPatch, intent: str
    ) -> Project:
        principal = deps.resolve(ctx)
        with deps.ledger.locked() as conn:
            p = resolve_project(conn, project, None)
        _require(principal, tool, p.id, p.slug)
        with _repo_errors():
            return projects.update_project(deps.ledger, principal.actor, p.id, patch, intent=intent)

    @mcp.tool()
    async def set_project_status(
        project: str, status: ProjectStatus, intent: Intent, ctx: Context
    ) -> Project:
        """Set a project's status (active, paused, done or archived). `intent` is a one-line reason
        recorded in the ledger, and the write lands pending the owner's review."""

        def body() -> Project:
            return _patch_project(
                ctx, "set_project_status", project, ProjectPatch(status=status), intent
            )

        return await anyio.to_thread.run_sync(body)

    @mcp.tool()
    async def set_project_summary(
        project: str, summary_md: str, intent: Intent, ctx: Context
    ) -> Project:
        """Replace a project's Markdown summary of where it stands. `intent` is a one-line reason
        recorded in the ledger, and the write lands pending the owner's review."""

        def body() -> Project:
            return _patch_project(
                ctx, "set_project_summary", project, ProjectPatch(summary_md=summary_md), intent
            )

        return await anyio.to_thread.run_sync(body)

    @mcp.tool()
    async def upsert_resource(
        project: str,
        kind: ResourceKind,
        locator: str,
        intent: Intent,
        ctx: Context,
        title: str = "",
    ) -> Resource:
        """Attach a resource (folder, repo, URL, doc, session, job) to a project, or update its
        title if the same kind and locator already exist. `intent` is a one-line reason recorded in
        the ledger, and the write lands pending the owner's review."""

        def body() -> Resource:
            principal = deps.resolve(ctx)
            with deps.ledger.locked() as conn:
                p = resolve_project(conn, project, None)
            _require(principal, "upsert_resource", p.id, p.slug)
            with _repo_errors():
                data = ResourceIn(project_id=p.id, kind=kind, locator=locator, title=title)
                return resources.upsert_resource(deps.ledger, principal.actor, data, intent=intent)

        return await anyio.to_thread.run_sync(body)

    @mcp.tool()
    async def save_report_draft(
        title: str,
        kind: ReportKind,
        source_md: str,
        intent: Intent,
        ctx: Context,
        projects: list[str] = [],
        report_id: str | None = None,
    ) -> Report:
        """Save a report (a Marp-flavoured Markdown deck or memo) about `projects` (slugs); with
        `report_id`, replace that report's title, kind, projects and source instead. `intent` is a
        one-line reason recorded in the ledger, and the write lands pending the owner's review."""

        def body() -> Report:
            principal = deps.resolve(ctx)
            # one lock across check and write, so the report cannot change scope in between
            with deps.ledger.locked() as conn:
                ids = list(dict.fromkeys(resolve_project(conn, slug, None).id for slug in projects))
                _require_projects(conn, principal, "save_report_draft", ids)
                current = reports.get_report(conn, report_id) if report_id is not None else None
                if current is not None:  # the report's existing projects must be in scope too
                    _require_projects(conn, principal, "save_report_draft", current.scope.projects)
                scope = ReportScope(projects=ids)
                with _repo_errors():
                    if report_id is None:
                        data = ReportIn(title=title, kind=kind, scope=scope, source_md=source_md)
                        saved = reports.create_report(deps.ledger, principal.actor, data, intent=intent)
                    else:
                        patch = ReportPatch(title=title, kind=kind, scope=scope, source_md=source_md)
                        saved = reports.update_report(
                            deps.ledger, principal.actor, report_id, patch, intent=intent
                        )
                # a drafting run remembers its report, so the web can open the right one
                run_id = principal.actor.run_id
                run = runs.get_run(conn, run_id) if run_id else None
                if run is not None:
                    runs.update_run(conn, run.id, props={**run.props, "report_id": saved.id})
                return saved

        return await anyio.to_thread.run_sync(body)
