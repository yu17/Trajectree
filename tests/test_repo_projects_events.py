from datetime import datetime, timedelta, timezone
import pytest
from trajectree.repo import DuplicateSlug, NotFound
from trajectree.repo.projects import create_project, update_project, list_projects, get_project
from trajectree.repo.events import create_event, update_event, list_events
from trajectree.models import ProjectIn, ProjectPatch, EventIn, EventPatch

UTC = timezone.utc
def ts(d): return datetime(2026, 1, d, tzinfo=UTC)

def test_create_and_list_project(ledger, human):
    p = create_project(ledger, human, ProjectIn(slug="clmi", title="CLMI", kind="research"))
    assert get_project(ledger.conn, p.id).slug == "clmi" and list_projects(ledger.conn)[0].id == p.id
    assert ledger.list_txns()[0].intent == "create project clmi"

def test_duplicate_slug(ledger, human):
    create_project(ledger, human, ProjectIn(slug="clmi", title="CLMI", kind="research"))
    with pytest.raises(DuplicateSlug):
        create_project(ledger, human, ProjectIn(slug="clmi", title="Again", kind="tooling"))

def test_update_project_records_diff(ledger, human, monkeypatch):
    p = create_project(ledger, human, ProjectIn(slug="clmi", title="CLMI", kind="research"))
    # timestamps have second precision; force a later clock so updated_at differs from created_at
    later = datetime.now(UTC) + timedelta(seconds=5)
    monkeypatch.setattr("trajectree.repo.projects.utcnow", lambda: later)
    update_project(ledger, human, p.id, ProjectPatch(status="paused"))
    c = ledger.changes_for(ledger.list_txns()[0].id)[0]
    assert c.new["status"] == "paused" and "updated_at" in c.new

def test_update_missing_project(ledger, human):
    with pytest.raises(NotFound):
        update_project(ledger, human, "nope", ProjectPatch(title="x"))

def test_event_window_query(ledger, human):
    p = create_project(ledger, human, ProjectIn(slug="clmi", title="CLMI", kind="research"))
    e1 = create_event(ledger, human, EventIn(project_id=p.id, kind="decision", title="A", t_start=ts(5)))
    e2 = create_event(ledger, human, EventIn(project_id=p.id, kind="meeting", title="B", t_start=ts(10), t_end=ts(12)))
    create_event(ledger, human, EventIn(project_id=p.id, kind="note", title="C", t_start=ts(20)))
    got = list_events(ledger.conn, project_ids=[p.id], t_from=ts(6), t_to=ts(11))
    assert [e.id for e in got] == [e2.id]
    assert [e.id for e in list_events(ledger.conn, kinds=["decision"])] == [e1.id]
    assert e1.created_txn == ledger.list_txns(limit=100)[-2].id

def test_update_event_status(ledger, human):
    p = create_project(ledger, human, ProjectIn(slug="clmi", title="CLMI", kind="research"))
    e = create_event(ledger, human, EventIn(project_id=p.id, kind="milestone", title="A", t_start=ts(5), status="planned"))
    assert update_event(ledger, human, e.id, EventPatch(status="happened")).status == "happened"

def test_create_event_missing_project(ledger, human):
    with pytest.raises(NotFound):
        create_event(ledger, human, EventIn(project_id="nope", kind="note", title="A", t_start=ts(5)))
    assert list_events(ledger.conn) == []

def test_update_event_rejects_merged_inverted_span(ledger, human):
    p = create_project(ledger, human, ProjectIn(slug="clmi", title="CLMI", kind="research"))
    e = create_event(ledger, human, EventIn(project_id=p.id, kind="meeting", title="B", t_start=ts(10), t_end=ts(12)))
    with pytest.raises(ValueError, match="t_end before t_start"):
        update_event(ledger, human, e.id, EventPatch(t_start=ts(15)))
    from trajectree.repo.events import get_event
    assert get_event(ledger.conn, e.id).t_start == ts(10)

def test_update_missing_event(ledger, human):
    with pytest.raises(NotFound):
        update_event(ledger, human, "nope", EventPatch(title="x"))

def test_event_tier_round_trips_and_defaults_to_block(ledger, human):
    p = create_project(ledger, human, ProjectIn(slug="t", title="T", kind="research"))
    e = create_event(ledger, human, EventIn(project_id=p.id, kind="note", title="x", t_start=ts(2)))
    assert e.tier == "block"
    assert update_event(ledger, human, e.id, EventPatch(tier="pill")).tier == "pill"
    assert [x.tier for x in list_events(ledger.conn, project_ids=[p.id])] == ["pill"]
    assert create_event(ledger, human, EventIn(project_id=p.id, kind="note", title="y", t_start=ts(3), tier="pill")).tier == "pill"
