import apsw
from trajectree.db.connection import open_db
from trajectree.db.schema import LEDGER_TABLES, MIGRATIONS, table_columns

def test_v3_tables(conn):
    names = {r[0] for r in conn.execute("select name from sqlite_master where type='table'")}
    assert {"run", "session"} <= names
    assert "proposals" in table_columns(conn, "run") and "ended_at" in table_columns(conn, "session")
    assert conn.execute("pragma user_version").fetchone()[0] == 3
    assert "run" not in LEDGER_TABLES and "session" not in LEDGER_TABLES

def test_migrates_v2_database(tmp_path):
    path = tmp_path / "old.db"
    c = apsw.Connection(str(path))
    for _, stmts in MIGRATIONS[:2]:
        for s in stmts:
            c.execute(s)
    c.execute("pragma user_version = 2")
    c.close()
    conn = open_db(path)
    assert conn.execute("pragma user_version").fetchone()[0] == 3
