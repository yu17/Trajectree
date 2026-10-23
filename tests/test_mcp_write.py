from datetime import datetime, timezone
import pytest
from mcp import Client
from trajectree.auth.scopes import Principal, Scope, owner_principal
from trajectree.mcpserver.server import build_mcp
from trajectree.models import Actor, EventIn, ProjectIn, ResourceIn, TodoIn
from trajectree.repo.edges import list_edges
from trajectree.repo.events import create_event, get_event
from trajectree.repo.projects import create_project, get_project
from trajectree.repo.resources import upsert_resource
from trajectree.repo.todos import create_todo, get_todo

UTC = timezone.utc
pytestmark = pytest.mark.anyio

def two_projects(ledger, human):
    a = create_project(ledger, human, ProjectIn(slug="a", title="A", kind="research"))
    b = create_project(ledger, human, ProjectIn(slug="b", title="B", kind="tooling"))
    return a, b

def owner_mcp(ledger, settings, cwd=None):
    return build_mcp(ledger, settings, resolve=lambda ctx: owner_principal("sess-1"), cwd_of=lambda ctx: cwd)

async def test_record_event_lands_pending_with_session_actor(ledger, settings, human):
    a, _ = two_projects(ledger, human)
    async with Client(owner_mcp(ledger, settings)) as c:
        r = await c.call_tool("record_event", {"project": "a", "title": "Decided", "kind": "decision", "intent": "log decision"})
        assert not r.is_error and r.structured_content["source"] == "agent"
        assert (await c.call_tool("record_event", {"project": "a", "title": "x", "intent": ""})).is_error
    t = ledger.list_txns()[0]   # newest; the empty-intent call wrote nothing
    assert (t.actor_kind, t.actor_ref, t.tool, t.review, t.intent) == ("session", "sess-1", "mcp", "pending", "log decision")

async def test_cwd_defaults_project(ledger, settings, human):
    a, _ = two_projects(ledger, human)
    upsert_resource(ledger, human, ResourceIn(project_id=a.id, kind="folder", locator="/mnt/x"))
    async with Client(owner_mcp(ledger, settings, cwd="/mnt/x/sub")) as c:
        r = await c.call_tool("add_note", {"text": "hello", "intent": "note"})
        assert r.structured_content["project_id"] == a.id and r.structured_content["kind"] == "note"

async def test_write_unknown_project_lists_slugs(ledger, settings, human):
    two_projects(ledger, human)
    n = len(ledger.list_txns())
    async with Client(owner_mcp(ledger, settings)) as c:
        r = await c.call_tool("record_event", {"project": "zzz", "title": "x", "intent": "i"})
        assert r.is_error and "known projects: a, b" in r.content[0].text
        r2 = await c.call_tool("add_note", {"text": "x", "intent": "i"})
        assert r2.is_error and "working directory" in r2.content[0].text
    assert len(ledger.list_txns()) == n

async def test_scope_denied_writes_nothing(ledger, settings, human):
    a, b = two_projects(ledger, human)
    run = Principal(actor=Actor(kind="agent", ref="run_1", model="m", backend="claude", tool="mcp"),
                    scope=Scope(projects=[b.id], tools=["record_event"]), token_id="tok")
    mcp = build_mcp(ledger, settings, resolve=lambda ctx: run)
    n = len(ledger.list_txns())
    async with Client(mcp) as c:
        denied = await c.call_tool("record_event", {"project": "a", "title": "x", "intent": "i"})
        assert denied.is_error and "not permitted" in denied.content[0].text
        denied2 = await c.call_tool("add_todo", {"project": "b", "title": "x", "intent": "i"})
        assert denied2.is_error and "not permitted" in denied2.content[0].text
        ok = await c.call_tool("record_event", {"project": "b", "title": "x", "intent": "i"})
        assert not ok.is_error
    txns = ledger.list_txns()
    assert len(txns) == n + 1 and txns[0].actor_kind == "agent" and txns[0].actor_ref == "run_1"

async def test_set_project_summary_and_status(ledger, settings, human):
    a, _ = two_projects(ledger, human)
    async with Client(owner_mcp(ledger, settings)) as c:
        r = await c.call_tool("set_project_summary", {"project": "a", "summary_md": "# A", "intent": "summarise"})
        assert r.structured_content["summary_at"] is not None
        r2 = await c.call_tool("set_project_status", {"project": "a", "status": "paused", "intent": "pause"})
        assert r2.structured_content["status"] == "paused"
    assert get_project(ledger.conn, a.id).summary_md == "# A"
    assert all(t.review == "pending" for t in ledger.list_txns()[:2])

async def test_link_and_unlink_owner_only(ledger, settings, human):
    a, b = two_projects(ledger, human)
    e1 = create_event(ledger, human, EventIn(project_id=a.id, kind="note", title="1", t_start=datetime(2026, 1, 1, tzinfo=UTC)))
    e2 = create_event(ledger, human, EventIn(project_id=b.id, kind="note", title="2", t_start=datetime(2026, 1, 2, tzinfo=UTC)))
    async with Client(owner_mcp(ledger, settings)) as c:
        edge = (await c.call_tool("link", {"src_type": "event", "src_id": e1.id, "dst_type": "event", "dst_id": e2.id, "rel": "caused", "intent": "l"})).structured_content
        assert edge["rel"] == "caused"
    scoped = Principal(actor=Actor(kind="agent", ref="r", tool="mcp"), scope=Scope(), token_id="tok")
    async with Client(build_mcp(ledger, settings, resolve=lambda ctx: scoped)) as c:
        r = await c.call_tool("unlink", {"edge_id": edge["id"], "intent": "u"})
        assert r.is_error and "owner-only" in r.content[0].text
    async with Client(owner_mcp(ledger, settings)) as c:
        r = await c.call_tool("unlink", {"edge_id": edge["id"], "intent": "u"})
        assert not r.is_error
    assert list_edges(ledger.conn) == []

async def test_todo_round_trip_and_bad_range(ledger, settings, human):
    a, _ = two_projects(ledger, human)
    async with Client(owner_mcp(ledger, settings)) as c:
        todo = (await c.call_tool("add_todo", {"project": "a", "title": "t", "due": "2026-03-01T00:00:00Z", "intent": "i"})).structured_content
        done = (await c.call_tool("update_todo", {"todo_id": todo["id"], "state": "done", "intent": "i"})).structured_content
        assert done["state"] == "done" and done["due"].startswith("2026-03-01")
        ev = (await c.call_tool("record_event", {"project": "a", "title": "x", "when": "2026-01-05T10:00:00Z", "intent": "i"})).structured_content
        bad = await c.call_tool("update_event", {"event_id": ev["id"], "end": "2026-01-01T00:00:00Z", "intent": "i"})
        assert bad.is_error
        missing = await c.call_tool("update_event", {"event_id": "nope", "title": "x", "intent": "i"})
        assert missing.is_error

def scoped(project_ids):
    return Principal(actor=Actor(kind="agent", ref="run_s", tool="mcp"),
                     scope=Scope(projects=project_ids), token_id="tok")

async def test_scoped_token_cannot_touch_inbox_todo(ledger, settings, human):
    a, _ = two_projects(ledger, human)
    todo = create_todo(ledger, human, TodoIn(project_id=None, title="loose"))
    n = len(ledger.list_txns())
    async with Client(build_mcp(ledger, settings, resolve=lambda ctx: scoped([a.id]))) as c:
        r = await c.call_tool("update_todo", {"todo_id": todo.id, "state": "done", "intent": "i"})
        assert r.is_error and "not permitted: update_todo on inbox" in r.content[0].text
    after = get_todo(ledger.conn, todo.id)
    assert (after.state, after.title) == ("open", "loose") and len(ledger.list_txns()) == n

async def test_link_denied_when_one_endpoint_outside_scope(ledger, settings, human):
    a, b = two_projects(ledger, human)
    e1 = create_event(ledger, human, EventIn(project_id=a.id, kind="note", title="1", t_start=datetime(2026, 1, 1, tzinfo=UTC)))
    e2 = create_event(ledger, human, EventIn(project_id=b.id, kind="note", title="2", t_start=datetime(2026, 1, 2, tzinfo=UTC)))
    async with Client(build_mcp(ledger, settings, resolve=lambda ctx: scoped([a.id]))) as c:
        r = await c.call_tool("link", {"src_type": "event", "src_id": e1.id, "dst_type": "event", "dst_id": e2.id, "rel": "caused", "intent": "l"})
        assert r.is_error and "not permitted: link on b" in r.content[0].text
    assert list_edges(ledger.conn) == []

async def test_update_event_denied_by_row_project(ledger, settings, human):
    a, b = two_projects(ledger, human)
    e = create_event(ledger, human, EventIn(project_id=a.id, kind="note", title="orig", t_start=datetime(2026, 1, 1, tzinfo=UTC)))
    async with Client(build_mcp(ledger, settings, resolve=lambda ctx: scoped([b.id]))) as c:
        r = await c.call_tool("update_event", {"event_id": e.id, "title": "changed", "intent": "i"})
        assert r.is_error and "not permitted: update_event on a" in r.content[0].text
    assert get_event(ledger.conn, e.id).title == "orig"

async def test_not_found_messages_name_the_entity(ledger, settings, human):
    a, _ = two_projects(ledger, human)
    async with Client(owner_mcp(ledger, settings)) as c:
        r = await c.call_tool("update_event", {"event_id": "nope", "title": "x", "intent": "i"})
        assert r.is_error and "event 'nope' not found" in r.content[0].text
        r = await c.call_tool("update_todo", {"todo_id": "nope", "title": "x", "intent": "i"})
        assert r.is_error and "todo 'nope' not found" in r.content[0].text
        r = await c.call_tool("link", {"src_type": "event", "src_id": "nope", "dst_type": "project", "dst_id": a.id, "rel": "r", "intent": "i"})
        assert r.is_error and "link: event 'nope' not found" in r.content[0].text
        r = await c.call_tool("unlink", {"edge_id": "nope", "intent": "i"})
        assert r.is_error and "not found: nope" in r.content[0].text
        r = await c.call_tool("record_event", {"project": "a", "title": "x", "end": "", "intent": "i"})
        assert r.is_error and "ISO 8601" in r.content[0].text

async def test_save_report_draft_scope_and_update(ledger, settings, human):
    from trajectree.models import ReportIn, ReportScope
    from trajectree.repo.reports import create_report, get_report
    a, b = two_projects(ledger, human)
    run = Principal(actor=Actor(kind="agent", ref="run_r", model="m", backend="claude", tool="mcp"),
                    scope=Scope(projects=[a.id], tools=["save_report_draft"], write=True), token_id="tok")
    async with Client(build_mcp(ledger, settings, resolve=lambda ctx: run)) as c:
        r = await c.call_tool("save_report_draft", {"title": "Deck", "kind": "deck", "source_md": "# One", "intent": "draft deck", "projects": ["a"]})
        assert not r.is_error and r.structured_content["scope"]["projects"] == [a.id]
        report_id = r.structured_content["id"]
        t = ledger.list_txns()[0]
        assert (t.actor_kind, t.actor_ref, t.review, t.intent) == ("agent", "run_r", "pending", "draft deck")
        r2 = await c.call_tool("save_report_draft", {"title": "Deck", "kind": "deck", "source_md": "# Two", "intent": "draft deck",
                                                     "projects": ["a"], "report_id": report_id})
        assert not r2.is_error and r2.structured_content["id"] == report_id
        n = len(ledger.list_txns())
        denied = await c.call_tool("save_report_draft", {"title": "X", "kind": "memo", "source_md": "x", "intent": "i", "projects": ["a", "b"]})
        assert denied.is_error and "not permitted: save_report_draft on b" in denied.content[0].text
        bare = await c.call_tool("save_report_draft", {"title": "X", "kind": "memo", "source_md": "x", "intent": "i"})
        assert bare.is_error and "not permitted: save_report_draft without a project" in bare.content[0].text
        unknown = await c.call_tool("save_report_draft", {"title": "X", "kind": "memo", "source_md": "x", "intent": "i", "projects": ["zzz"]})
        assert unknown.is_error and "unknown project 'zzz'" in unknown.content[0].text
        missing = await c.call_tool("save_report_draft", {"title": "X", "kind": "memo", "source_md": "x", "intent": "i", "projects": ["a"], "report_id": "nope"})
        assert missing.is_error and "not found" in missing.content[0].text
        other = create_report(ledger, human, ReportIn(title="B only", kind="memo", scope=ReportScope(projects=[b.id])))
        n = len(ledger.list_txns())
        hijack = await c.call_tool("save_report_draft", {"title": "X", "kind": "memo", "source_md": "x", "intent": "i", "projects": ["a"], "report_id": other.id})
        assert hijack.is_error and "not permitted: save_report_draft on b" in hijack.content[0].text
        assert len(ledger.list_txns()) == n and get_report(ledger.conn, other.id).title == "B only"
    assert get_report(ledger.conn, report_id).source_md == "# Two"
    async with Client(owner_mcp(ledger, settings)) as c:
        r = await c.call_tool("save_report_draft", {"title": "Any", "kind": "memo", "source_md": "m", "intent": "draft memo"})
        assert not r.is_error and r.structured_content["scope"]["projects"] == []
