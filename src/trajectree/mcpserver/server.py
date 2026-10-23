"""MCP server factory and the helpers its tools share."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta

import apsw
from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError

from trajectree.api.deps import token_matches
from trajectree.auth.scopes import Principal, owner_principal
from trajectree.auth.tokens import resolve_token
from trajectree.config import Settings
from trajectree.db.ledger import Ledger
from trajectree.models import Project
from trajectree.repo.projects import get_project_by_slug, list_projects
from trajectree.repo.resources import project_for_path
from trajectree.repo.sessions import session_for_cwd
from trajectree.timeutil import utcnow

CWD_HEADER = "x-trajectree-cwd"
SESSION_HEADER = "x-trajectree-session"

INSTRUCTIONS = (
    "Trajectree is the owner's project tracker: projects are lanes on a timeline holding events, "
    "todos, resources and activity signals. Read with list_projects, get_project, timeline, search, "
    "list_todos and get_signals. Record outcomes with the write tools record_event, add_note, "
    "update_event, add_todo, update_todo, link, unlink, set_project_status, set_project_summary and "
    "upsert_resource; every write tool requires an `intent`, one short sentence saying why the change "
    "is made, which becomes the transaction's intent the owner reviews. Projects are named by slug; "
    "when a write omits the project it defaults to the project mapped to the working directory. "
    "Datetimes are ISO 8601 with a timezone, e.g. 2026-01-05T10:00:00Z."
)


@dataclass(frozen=True)
class ToolDeps:
    ledger: Ledger
    settings: Settings
    resolve: Callable[[Context], Principal]
    cwd_of: Callable[[Context], str | None]


def _header(headers: Mapping[str, str], name: str) -> str | None:
    """Case-insensitive header lookup; `name` is lower-case."""
    value = headers.get(name)
    if value is not None:
        return value
    for key, val in headers.items():
        if key.lower() == name:
            return val
    return None


def _cwd_from_headers(ctx: Context) -> str | None:
    headers = ctx.headers
    return _header(headers, CWD_HEADER) if headers is not None else None


def principal_from_headers(
    headers: Mapping[str, str] | None,
    *,
    owner_token: str,
    conn: apsw.Connection,
    now: datetime | None = None,
    live_window_s: int = 900,
) -> Principal:
    """Authenticate a request by its bearer token: the owner's token or a live scoped token."""
    if headers is None:
        raise ToolError("unauthorized")
    scheme, _, value = (_header(headers, "authorization") or "").partition(" ")
    token = value.strip()
    if scheme.lower() != "bearer" or not token:
        raise ToolError("unauthorized")
    if token_matches(token, owner_token):
        session = _header(headers, SESSION_HEADER)
        cwd = _header(headers, CWD_HEADER)
        if session is None and cwd:
            since = (now or utcnow()) - timedelta(seconds=live_window_s)
            running = session_for_cwd(conn, cwd=cwd, since=since)
            session = running.id if running is not None else None
        return owner_principal(session)
    principal = resolve_token(conn, token)
    if principal is None or not principal.scope.allows_mcp():
        raise ToolError("unauthorized")
    return principal


def _known_slugs(conn: apsw.Connection) -> str:
    return ", ".join(sorted(p.slug for p in list_projects(conn)))


def resolve_project(conn: apsw.Connection, slug: str | None, cwd: str | None) -> Project:
    """The project named by `slug`, else the one mapped to `cwd`; ToolError listing slugs otherwise."""
    if slug is not None:
        project = get_project_by_slug(conn, slug)
        if project is None:
            raise ToolError(f"unknown project '{slug}'; known projects: {_known_slugs(conn)}")
        return project
    project = project_for_path(conn, cwd) if cwd is not None else None
    if project is None:
        raise ToolError(
            "no project given and the working directory maps to none; "
            f"known projects: {_known_slugs(conn)}"
        )
    return project


def parse_when(value: str | None, default: datetime) -> datetime:
    """Parse an ISO 8601 datetime with `Z` or an offset; None gives `default`."""
    if value is None:
        return default
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        parsed = None
    if parsed is None or parsed.tzinfo is None:
        raise ToolError(f"'{value}' is not an ISO 8601 datetime")
    return parsed


def build_mcp(
    ledger: Ledger,
    settings: Settings,
    *,
    resolve: Callable[[Context], Principal],
    cwd_of: Callable[[Context], str | None] | None = None,
) -> MCPServer:
    """A fresh MCP server with Trajectree's tools bound to this ledger."""
    from trajectree.mcpserver.read_tools import register_read_tools
    from trajectree.mcpserver.write_tools import register_write_tools

    mcp = MCPServer(name="trajectree", instructions=INSTRUCTIONS)
    deps = ToolDeps(
        ledger=ledger, settings=settings, resolve=resolve, cwd_of=cwd_of or _cwd_from_headers
    )
    register_read_tools(mcp, deps)
    register_write_tools(mcp, deps)
    return mcp
