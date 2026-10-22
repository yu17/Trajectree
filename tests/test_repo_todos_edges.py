from datetime import datetime, timezone
import pytest
from trajectree.repo import DuplicateEdge, NotFound
from trajectree.repo.projects import create_project
from trajectree.repo.events import create_event
from trajectree.repo.todos import create_todo, update_todo, list_todos
from trajectree.repo.edges import create_edge, delete_edge, list_edges
from trajectree.repo.review import pending_txns
from trajectree.models import ProjectIn, EventIn, TodoIn, TodoPatch, EdgeIn

UTC = timezone.utc
def ts(d): return datetime(2026, 1, d, tzinfo=UTC)

@pytest.fixture
def two_events(ledger, human):
    p = create_project(ledger, human, ProjectIn(slug="a", title="A", kind="research"))
    q = create_project(ledger, human, ProjectIn(slug="b", title="B", kind="tooling"))
    e1 = create_event(ledger, human, EventIn(project_id=p.id, kind="idea", title="x", t_start=ts(1)))
    e2 = create_event(ledger, human, EventIn(project_id=q.id, kind="milestone", title="y", t_start=ts(2)))
    return p, q, e1, e2

def test_todo_lifecycle(ledger, human, two_events):
    p = two_events[0]
    t = create_todo(ledger, human, TodoIn(project_id=p.id, title="Reply to reviewer", due=ts(9)))
    assert list_todos(ledger.conn, states=["open"])[0].id == t.id
    assert update_todo(ledger, human, t.id, TodoPatch(state="done")).state == "done"
    assert list_todos(ledger.conn, states=["open"]) == []

def test_edge_create_list_delete(ledger, human, two_events):
    _, _, e1, e2 = two_events
    e = create_edge(ledger, human, EdgeIn(src_type="event", src_id=e1.id, dst_type="event", dst_id=e2.id, rel="shares_idea"))
    assert [x.id for x in list_edges(ledger.conn, event_ids=[e1.id])] == [e.id]
    with pytest.raises(DuplicateEdge):
        create_edge(ledger, human, EdgeIn(src_type="event", src_id=e1.id, dst_type="event", dst_id=e2.id, rel="shares_idea"))
    delete_edge(ledger, human, e.id)
    assert list_edges(ledger.conn) == []

def test_edge_requires_existing_endpoints(ledger, human, two_events):
    e1 = two_events[2]
    with pytest.raises(NotFound):
        create_edge(ledger, human, EdgeIn(src_type="event", src_id=e1.id, dst_type="todo", dst_id="nope", rel="blocked_by"))

def test_pending_txns_follow_latest_txn(ledger, human, agent, two_events):
    p = two_events[0]
    e = create_event(ledger, agent, EventIn(project_id=p.id, kind="milestone", title="draft", t_start=ts(3), source="agent"))
    tid = ledger.list_txns()[0].id
    assert pending_txns(ledger.conn, "event") == {e.id: tid}
    ledger.set_review(tid, "accepted", human)
    assert pending_txns(ledger.conn, "event") == {}
