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
    assert "- 2026-01-02 [note/happened/block] Event 0 (" in md and "- [ ] Write up (no due) (" in md
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


def test_budget_cut_keeps_newest_events(ledger, human):
    p = create_project(ledger, human, ProjectIn(slug="a", title="A", kind="research"))
    for i in range(200):
        create_event(ledger, human, EventIn(project_id=p.id, kind="note", title=f"Event {i}", t_start=datetime(2025, 8, 1, tzinfo=UTC) + timedelta(days=i)))
    pack = build_context_pack(ledger.conn, project_ids=[p.id], t_from=datetime(2025, 7, 1, tzinfo=UTC), t_to=datetime(2026, 12, 1, tzinfo=UTC), token_budget=600)
    md = pack.markdown
    assert "Event 199 (" in md and "Event 0 (" not in md
    section = md.split("### Events\n")[1].split("\n\n")[0]
    lines = section.splitlines()
    kept = [l for l in lines if l.startswith("- ")]
    dates = [l.split()[1] for l in kept]
    assert dates == sorted(dates) and len(set(dates)) == len(dates)
    assert kept[-1].startswith("- 2026-02-16") and lines.index(next(l for l in lines if l.startswith("(dropped"))) < lines.index(kept[0])


def _signals(ledger, p, specs):
    from trajectree.repo.signals import upsert_signals
    from trajectree.models import SignalIn
    upsert_signals(ledger.conn, [SignalIn(project_id=p.id, kind=k, t_start=t, dedupe_key=f"k{i}", payload=pl) for i, (k, t, pl) in enumerate(specs)])


def test_activity_shows_newest_signals_for_busy_project(ledger, human):
    p = seed(ledger, human)
    base = datetime(2026, 1, 1, tzinfo=UTC)
    _signals(ledger, p, [("commits", base + timedelta(hours=i), {"count": i}) for i in range(600)])
    pack = build_context_pack(ledger.conn, project_ids=[p.id], t_from=FROM, t_to=TO)
    activity = pack.markdown.split("### Activity\n")[1]
    assert "599 commits" in activity and "0 commits" not in activity.replace("590 commits", "")


def test_activity_section_lines(ledger, human):
    p = seed(ledger, human)
    base = datetime(2026, 1, 3, tzinfo=UTC)
    _signals(ledger, p, [("session", base + timedelta(hours=i), pl) for i, pl in enumerate([{"title": "Fix build"}, {"count": 3}, {}])])
    activity = build_context_pack(ledger.conn, project_ids=[p.id], t_from=FROM, t_to=TO).markdown.split("### Activity\n")[1]
    assert "session Fix build" in activity and "session 3 commits" in activity and "- 2026-01-03 session session" in activity


def test_summary_cut_reports_drop(ledger, human):
    p = create_project(ledger, human, ProjectIn(slug="a", title="A", kind="research", summary_md="x" * 5000))
    pack = build_context_pack(ledger.conn, project_ids=[p.id], t_from=FROM, t_to=TO, token_budget=600)
    assert any(d.startswith("a: summary dropped") for d in pack.dropped)
    assert "(dropped" in pack.markdown.split("### Summary\n")[1].split("### Events")[0]


def test_pack_rejects_tiny_budget(ledger, human):
    p = seed(ledger, human)
    with pytest.raises(ValueError):
        build_context_pack(ledger.conn, project_ids=[p.id], t_from=FROM, t_to=TO, token_budget=-5)


@pytest.mark.anyio
async def test_tool_rejects_unknown_slug_and_bad_date(ledger, settings, human):
    seed(ledger, human)
    mcp = build_mcp(ledger, settings, resolve=lambda ctx: owner_principal(None))
    async with Client(mcp) as c:
        r = await c.call_tool("get_context_pack", {"projects": ["zzz"]})
        assert r.is_error and "known projects" in r.content[0].text
        r = await c.call_tool("get_context_pack", {"start": "yesterday"})
        assert r.is_error and "ISO 8601" in r.content[0].text


def test_context_pack_event_line_shows_tier(ledger, human):
    p = seed(ledger, human, n_events=1)
    create_event(ledger, human, EventIn(project_id=p.id, kind="note", title="Moment", tier="pill", t_start=datetime(2026, 1, 3, tzinfo=UTC)))
    md = build_context_pack(ledger.conn, project_ids=[p.id], t_from=FROM, t_to=TO).markdown
    assert "[note/happened/pill] Moment (" in md and "[note/happened/block] Event 0 (" in md
