import json, threading
from trajectree.db.ledger import Ledger, review_for
from trajectree.models import Actor

def insert_project(h, pid="p1", slug="p1"):
    h.conn.execute("insert into project(id, slug, title, kind, created_at, updated_at) values (?,?,?,?,?,?)",
                   (pid, slug, "P", "research", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"))

def test_transaction_records_changeset_and_changes(ledger, human):
    with ledger.transaction(human, "add project") as h:
        insert_project(h)
    txn = ledger.get_txn(h.id)
    assert txn is not None and txn.intent == "add project" and txn.review == "accepted" and len(txn.changeset) > 0
    changes = ledger.changes_for(h.id)
    assert len(changes) == 1 and changes[0].table_name == "project" and changes[0].op == "INSERT"
    assert changes[0].pk == ["p1"] and changes[0].new["slug"] == "p1" and changes[0].old is None

def test_update_change_has_old_and_new_for_changed_columns_only(ledger, human):
    with ledger.transaction(human, "add") as h:
        insert_project(h)
    with ledger.transaction(human, "rename") as h2:
        h2.conn.execute("update project set title='Q' where id='p1'")
    c = ledger.changes_for(h2.id)[0]
    assert c.op == "UPDATE" and c.old == {"id": "p1", "title": "P"} and c.new == {"title": "Q"}

def test_agent_transaction_is_pending(ledger, agent):
    with ledger.transaction(agent, "agent add") as h:
        insert_project(h)
    assert ledger.get_txn(h.id).review == "pending"

def test_auto_accept_overrides(ledger, agent):
    with ledger.transaction(agent, "materialize", auto_accept=True) as h:
        insert_project(h)
    assert ledger.get_txn(h.id).review == "accepted"

def test_empty_transaction_writes_no_txn_row(ledger, human):
    with ledger.transaction(human, "noop") as h:
        pass
    assert ledger.get_txn(h.id) is None

def test_exception_rolls_back(ledger, human):
    try:
        with ledger.transaction(human, "boom") as h:
            insert_project(h)
            raise RuntimeError("x")
    except RuntimeError:
        pass
    assert ledger.conn.execute("select count(*) from project").fetchone()[0] == 0
    assert ledger.get_txn(h.id) is None

def test_concurrent_transactions_all_recorded(ledger, human):
    def work(n):
        for i in range(25):
            with ledger.transaction(human, f"t{n}-{i}") as h:
                insert_project(h, pid=f"p{n}-{i}", slug=f"p{n}-{i}")
    threads = [threading.Thread(target=work, args=(n,)) for n in range(4)]
    [t.start() for t in threads]; [t.join() for t in threads]
    assert len(ledger.list_txns(limit=1000)) == 100

def test_list_txns_pages_newest_first(ledger, human):
    ids = []
    for i in range(3):
        with ledger.transaction(human, f"t{i}") as h:
            insert_project(h, pid=f"p{i}", slug=f"p{i}")
        ids.append(h.id)
    page = ledger.list_txns(limit=2)
    assert [t.id for t in page] == [ids[2], ids[1]]
    assert [t.id for t in ledger.list_txns(limit=2, before=ids[1])] == [ids[0]]
