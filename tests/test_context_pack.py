from datetime import datetime, timedelta, timezone
import pytest
from mcp import Client
from trajectree.auth.scopes import owner_principal
from trajectree.context.pack import build_context_pack
from trajectree.mcpserver.server import build_mcp
from trajectree.models import EventIn, ProjectIn, TodoIn
from trajectree.repo.events import create_event
from trajectree.repo.projects import create_project
from trajectree.repo.todos import create_todo

UTC = timezone.utc
FROM, TO = datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 2, 1, tzinfo=UTC)

def seed(ledger, human, n_events=2):
    p = create_project(ledger, human, ProjectIn(slug="a", title="A", kind="research", summary_md="The summary."))
    for i in range(n_events):
        create_event(ledger, human, EventIn(project_id=p.id, kind="note", title=f"Event {i}", t_start=datetime(2026, 1, 2, tzinfo=UTC) + timedelta(minutes=i)))
    create_todo(ledger, human, TodoIn(project_id=p.id, title="Write up"))
    return p

def test_pack_sections_and_ids(ledger, human):
    p = seed(ledger, human)
    pack = build_context_pack(ledger.conn, project_ids=[p.id], t_from=FROM, t_to=TO)
    md = pack.markdown
    assert md.startswith("# Trajectree context (2026-01-01 to 2026-02-01)")
    assert "## A (a) — active" in md and "### Summary\nThe summary." in md
    assert "- 2026-01-02 [note/happened] Event 0 (" in md and "- [ ] Write up (no due) (" in md
    assert pack.dropped == [] and pack.projects == ["a"] and pack.tokens_est == len(md) // 4

def test_pack_respects_budget_and_reports_drops(ledger, human):
    p = seed(ledger, human, n_events=200)
    pack = build_context_pack(ledger.conn, project_ids=[p.id], t_from=FROM, t_to=TO, token_budget=600)
    assert pack.tokens_est <= 700
    assert any(d.startswith("a: events dropped") for d in pack.dropped)
    assert "(dropped" in pack.markdown

def test_pack_question_adds_search_hits(ledger, human):
    p = seed(ledger, human)
    pack = build_context_pack(ledger.conn, project_ids=[p.id], t_from=FROM, t_to=TO, question="write")
    assert '## Search hits for "write"' in pack.markdown and "[todo] Write up (a)" in pack.markdown

@pytest.mark.anyio
async def test_get_context_pack_tool(ledger, settings, human):
    seed(ledger, human)
    mcp = build_mcp(ledger, settings, resolve=lambda ctx: owner_principal(None))
    async with Client(mcp) as c:
        r = await c.call_tool("get_context_pack", {"projects": ["a"], "start": "2026-01-01T00:00:00Z", "end": "2026-02-01T00:00:00Z"})
        assert r.structured_content["markdown"].startswith("# Trajectree context") and r.structured_content["projects"] == ["a"]
