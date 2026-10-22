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
