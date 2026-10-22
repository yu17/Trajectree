from trajectree.db.fts import search, rebuild
from tests.test_ledger import insert_project

def add_event(h, eid, title, pid="p1"):
    h.conn.execute("insert into event(id, project_id, kind, title, t_start, created_at, updated_at) values (?,?,?,?,?,?,?)",
                   (eid, pid, "decision", title, "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"))

def test_search_finds_event_title(ledger, human):
    with ledger.transaction(human, "add") as h:
        insert_project(h); add_event(h, "e1", "Encoder design frozen")
    hits = search(ledger.conn, "encoder")
    assert [(x.kind, x.id) for x in hits] == [("event", "e1")]

def test_reverted_event_disappears_from_search(ledger, human):
    with ledger.transaction(human, "add") as h:
        insert_project(h); add_event(h, "e1", "Encoder design frozen")
    with ledger.transaction(human, "add e2") as h2:
        add_event(h2, "e2", "Dataset pipeline done")
    ledger.revert(h2.id, human)
    assert [x.id for x in search(ledger.conn, "dataset")] == []
    assert [x.id for x in search(ledger.conn, "encoder")] == ["e1"]

def test_rebuild_matches_triggers(ledger, human):
    with ledger.transaction(human, "add") as h:
        insert_project(h); add_event(h, "e1", "Encoder design frozen")
    ledger.conn.execute("delete from search_fts")
    rebuild(ledger.conn)
    assert [x.id for x in search(ledger.conn, "encoder")] == ["e1"]

def test_changeset_excludes_fts_shadow_tables(ledger, human):
    with ledger.transaction(human, "add") as h:
        insert_project(h); add_event(h, "e1", "Encoder design frozen")
    assert {c.table_name for c in ledger.changes_for(h.id)} == {"project", "event"}

def test_punctuated_and_operator_queries_do_not_raise(ledger, human):
    with ledger.transaction(human, "add") as h:
        insert_project(h); add_event(h, "e1", "Don't ship v1.2 yet")
    for q in ("don't", "v1.2", "C++", "a,b", "AND", 'say "hi'):
        search(ledger.conn, q)
    assert [x.id for x in search(ledger.conn, "don't")] == ["e1"]
    assert [x.id for x in search(ledger.conn, "v1.2")] == ["e1"]
    assert [x.id for x in search(ledger.conn, "ship v1.2")] == ["e1"]

def test_empty_query_returns_no_hits(ledger):
    assert search(ledger.conn, "") == []
    assert search(ledger.conn, "   ") == []
