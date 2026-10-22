# tests/test_revert.py
import pytest
from trajectree.db.ledger import RevertConflict, AlreadyReverted
from tests.test_ledger import insert_project

def title(conn, pid="p1"):
    row = conn.execute("select title from project where id=?", (pid,)).fetchone()
    return row[0] if row else None

def test_revert_restores_previous_state(ledger, human):
    with ledger.transaction(human, "add") as h:
        insert_project(h)
    r = ledger.revert(h.id, human)
    assert title(ledger.conn) is None
    assert r.reverts_txn == h.id and ledger.get_txn(h.id).reverted_by == r.id
    assert r.intent == f"revert {h.id}"

def test_revert_keeps_later_unrelated_changes(ledger, human):
    with ledger.transaction(human, "add p1") as h1:
        insert_project(h1, "p1", "p1")
    with ledger.transaction(human, "add p2") as h2:
        insert_project(h2, "p2", "p2")
    ledger.revert(h1.id, human)
    assert title(ledger.conn, "p1") is None and title(ledger.conn, "p2") == "P"

def test_revert_conflict_aborts_and_reports(ledger, human):
    with ledger.transaction(human, "add") as h1:
        insert_project(h1)
    with ledger.transaction(human, "rename") as h2:
        h2.conn.execute("update project set title='Q' where id='p1'")
    with pytest.raises(RevertConflict) as exc:
        ledger.revert(h1.id, human)
    assert exc.value.conflicts[0].table_name == "project" and exc.value.conflicts[0].pk == ["p1"]
    assert title(ledger.conn) == "Q"
    assert ledger.get_txn(h1.id).reverted_by is None

def test_revert_of_revert_reapplies(ledger, human):
    with ledger.transaction(human, "add") as h:
        insert_project(h)
    r = ledger.revert(h.id, human)
    ledger.revert(r.id, human)
    assert title(ledger.conn) == "P"

def test_double_revert_rejected(ledger, human):
    with ledger.transaction(human, "add") as h:
        insert_project(h)
    ledger.revert(h.id, human)
    with pytest.raises(AlreadyReverted):
        ledger.revert(h.id, human)

def test_reject_reverts_and_marks(ledger, human, agent):
    with ledger.transaction(agent, "agent add") as h:
        insert_project(h)
    t = ledger.set_review(h.id, "rejected", human)
    assert t.review == "rejected" and t.reverted_by is not None and title(ledger.conn) is None

def test_accept_sets_review_without_new_txn(ledger, human, agent):
    with ledger.transaction(agent, "agent add") as h:
        insert_project(h)
    before = len(ledger.list_txns())
    t = ledger.set_review(h.id, "accepted", human)
    assert t.review == "accepted" and t.review_ts is not None and len(ledger.list_txns()) == before

def test_revert_foreign_key_violation_reports_conflict(ledger, human):
    with ledger.transaction(human, "add") as h1:
        insert_project(h1)
    with ledger.transaction(human, "event") as h2:
        h2.conn.execute("insert into event(id, project_id, kind, title, t_start, created_at, updated_at)"
                        " values ('e1','p1','note','E','2026-01-01T00:00:00Z','2026-01-01T00:00:00Z','2026-01-01T00:00:00Z')")
    with pytest.raises(RevertConflict) as exc:
        ledger.revert(h1.id, human)
    assert [c.reason for c in exc.value.conflicts] == ["SQLITE_CHANGESET_FOREIGN_KEY"]
    assert title(ledger.conn) == "P" and ledger.get_txn(h1.id).reverted_by is None

import pytest
from datetime import datetime, timezone
from trajectree.db.ledger import AlreadyReverted, RevertConflict
from trajectree.models import EventIn, EventPatch, ProjectIn
from trajectree.repo.events import create_event, get_event, update_event
from trajectree.repo.projects import create_project
from trajectree.repo.review import pending_txns

UTC = timezone.utc

def _pending_event(ledger, human, agent):
    p = create_project(ledger, human, ProjectIn(slug="a", title="A", kind="research"))
    ev = create_event(ledger, agent, EventIn(project_id=p.id, kind="note", title="n", t_start=datetime(2026, 1, 1, tzinfo=UTC), source="agent"))
    return ev, ledger.list_txns()[0].id

def test_reject_conflict_keeps_review_pending(ledger, human, agent):
    ev, tid = _pending_event(ledger, human, agent)
    update_event(ledger, human, ev.id, EventPatch(title="edited"))
    with pytest.raises(RevertConflict):
        ledger.set_review(tid, "rejected", human)
    t = ledger.get_txn(tid)
    assert t.review == "pending" and t.reverted_by is None

def test_reject_is_one_transaction_and_idempotent(ledger, human, agent):
    ev, tid = _pending_event(ledger, human, agent)
    before = len(ledger.list_txns())
    rejected = ledger.set_review(tid, "rejected", human)
    assert rejected.review == "rejected" and rejected.reverted_by is not None
    assert len(ledger.list_txns()) == before + 1
    assert ledger.get_txn(rejected.reverted_by).reverts_txn == tid
    assert ledger.set_review(tid, "rejected", human).reverted_by == rejected.reverted_by
    assert len(ledger.list_txns()) == before + 1
    assert get_event(ledger.conn, ev.id) is None

def test_accept_reverted_txn_refused(ledger, human, agent):
    _, tid = _pending_event(ledger, human, agent)
    ledger.revert(tid, human)
    with pytest.raises(AlreadyReverted):
        ledger.set_review(tid, "accepted", human)

def test_accept_twice_keeps_first_review_ts(ledger, human, agent, monkeypatch):
    _, tid = _pending_event(ledger, human, agent)
    first = ledger.set_review(tid, "accepted", human)
    monkeypatch.setattr("trajectree.db.ledger.utcnow", lambda: datetime(2030, 1, 1, tzinfo=UTC))
    assert ledger.set_review(tid, "accepted", human).review_ts == first.review_ts

def test_list_txns_filters(ledger, human, agent):
    _pending_event(ledger, human, agent)
    assert [t.actor_kind for t in ledger.list_txns(review="pending")] == ["agent"]
    assert len(ledger.list_txns(actor_kinds=["human"])) == 1
    assert len(ledger.list_txns(actor_kinds=["human", "agent"])) == 2

def test_pending_txns_maps_row_to_latest_pending_txn(ledger, human, agent):
    ev, tid = _pending_event(ledger, human, agent)
    assert pending_txns(ledger.conn, "event") == {ev.id: tid}
    update_event(ledger, agent, ev.id, EventPatch(title="again"))
    newer = ledger.list_txns()[0].id
    assert pending_txns(ledger.conn, "event") == {ev.id: newer}
    ledger.set_review(newer, "accepted", human)
    assert pending_txns(ledger.conn, "event") == {}

def test_locked_is_reentrant_inside_transaction(ledger, human):
    with ledger.transaction(human, "x"):
        with ledger.locked() as conn:
            assert conn is ledger.conn

def test_revert_of_pending_txn_marks_it_rejected(ledger, human, agent):
    _, tid = _pending_event(ledger, human, agent)
    reverted = ledger.revert(tid, human)
    original = ledger.get_txn(tid)
    assert original.review == "rejected" and original.review_ts is not None and original.reverted_by == reverted.id
    assert ledger.list_txns(review="pending") == []

def test_revert_of_accepted_txn_keeps_accepted(ledger, human):
    p = create_project(ledger, human, ProjectIn(slug="a", title="A", kind="research"))
    tid = ledger.list_txns()[0].id
    ledger.revert(tid, human)
    assert ledger.get_txn(tid).review == "accepted"
