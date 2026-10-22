from datetime import datetime, timezone

import pytest
from mcp import Client
from mcp.server.mcpserver.exceptions import ToolError

from trajectree.auth.scopes import owner_principal
from trajectree.mcpserver.server import build_mcp, parse_when, principal_from_headers
from trajectree.models import EventIn, ProjectIn, TodoIn
from trajectree.repo.events import create_event
from trajectree.repo.projects import create_project
from trajectree.repo.todos import create_todo

UTC = timezone.utc
pytestmark = pytest.mark.anyio


@pytest.fixture
def mcp(ledger, settings):
    return build_mcp(ledger, settings, resolve=lambda ctx: owner_principal(None))


def seed(ledger, human):
    p = create_project(ledger, human, ProjectIn(slug="a", title="A", kind="research", summary_md="sum"))
    e = create_event(ledger, human, EventIn(project_id=p.id, kind="decision", title="Freeze", t_start=datetime(2026, 1, 5, tzinfo=UTC)))
    t = create_todo(ledger, human, TodoIn(project_id=p.id, title="write up"))
    return p, e, t


async def test_tool_names(mcp):
    async with Client(mcp) as c:
        names = {t.name for t in (await c.list_tools()).tools}
        assert {"list_projects", "get_project", "timeline", "search", "list_todos", "get_signals"} <= names


async def test_get_project_by_slug(mcp, ledger, human):
    p, e, t = seed(ledger, human)
    async with Client(mcp) as c:
        r = await c.call_tool("get_project", {"project": "a"})
        assert not r.is_error
        d = r.structured_content
        assert d["project"]["slug"] == "a" and [x["id"] for x in d["recent_events"]] == [e.id]
        assert [x["id"] for x in d["open_todos"]] == [t.id]


async def test_unknown_project_lists_known_slugs(mcp, ledger, human):
    seed(ledger, human)
    async with Client(mcp) as c:
        r = await c.call_tool("get_project", {"project": "zzz"})
        assert r.is_error and "known projects: a" in r.content[0].text


async def test_timeline_window_and_bad_date(mcp, ledger, human):
    p, e, _ = seed(ledger, human)
    async with Client(mcp) as c:
        r = await c.call_tool("timeline", {"projects": ["a"], "start": "2026-01-01T00:00:00Z", "end": "2026-02-01T00:00:00Z"})
        assert [x["id"] for x in r.structured_content["events"]] == [e.id]
        r2 = await c.call_tool("timeline", {"start": "yesterday"})
        assert r2.is_error and "ISO 8601" in r2.content[0].text


async def test_search_and_todos(mcp, ledger, human):
    p, e, t = seed(ledger, human)
    async with Client(mcp) as c:
        hits = (await c.call_tool("search", {"q": "freeze"})).structured_content["result"]
        assert hits[0]["id"] == e.id
        todos = (await c.call_tool("list_todos", {"project": "a"})).structured_content["result"]
        assert [x["id"] for x in todos] == [t.id]


def test_parse_when():
    assert parse_when("2026-01-05T10:00:00Z", datetime(2000, 1, 1, tzinfo=UTC)) == datetime(2026, 1, 5, 10, tzinfo=UTC)
    assert parse_when(None, datetime(2000, 1, 1, tzinfo=UTC)) == datetime(2000, 1, 1, tzinfo=UTC)
    with pytest.raises(ToolError):
        parse_when("2026-01-05T10:00:00", datetime(2000, 1, 1, tzinfo=UTC))


def test_principal_from_headers_owner_and_unauthorized(ledger):
    with ledger.locked() as conn:
        p = principal_from_headers(
            {"authorization": "Bearer owner-secret", "x-trajectree-session": "sess-9"},
            owner_token="owner-secret",
            conn=conn,
        )
        assert p.actor.kind == "session" and p.actor.ref == "sess-9" and p.token_id is None
        for headers in (None, {}, {"authorization": "Bearer wrong"}, {"authorization": "Basic owner-secret"}):
            with pytest.raises(ToolError, match="unauthorized"):
                principal_from_headers(headers, owner_token="owner-secret", conn=conn)
