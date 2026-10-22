import apsw
from trajectree.db.connection import open_db
from trajectree.db.schema import LEDGER_TABLES, MIGRATIONS, SCHEMA_VERSION, table_columns

def test_v2_tables_and_column(conn):
    names = {r[0] for r in conn.execute("select name from sqlite_master where type='table'")}
    assert {"signal", "state", "token"} <= names
    assert "summary_at" in table_columns(conn, "project")
    assert conn.execute("pragma user_version").fetchone()[0] == SCHEMA_VERSION

def test_migrates_v1_database(tmp_path):
    path = tmp_path / "old.db"
    c = apsw.Connection(str(path))
    for stmt in MIGRATIONS[0][1]:
        c.execute(stmt)
    c.execute("pragma user_version = 1")
    c.close()
    conn = open_db(path)
    assert conn.execute("pragma user_version").fetchone()[0] == SCHEMA_VERSION
    assert "summary_at" in table_columns(conn, "project")

def test_signal_dedupe_key_is_unique(conn):
    import pytest
    conn.execute("insert into signal(id, project_id, kind, t_start, payload, dedupe_key, updated_at) values ('a','p','session','2026-01-01T00:00:00Z','{}','k','2026-01-01T00:00:00Z')")
    with pytest.raises(apsw.ConstraintError):
        conn.execute("insert into signal(id, project_id, kind, t_start, payload, dedupe_key, updated_at) values ('b','p','session','2026-01-01T00:00:00Z','{}','k','2026-01-01T00:00:00Z')")

def test_new_tables_are_not_ledger_tables():
    assert not {"signal", "state", "token"} & set(LEDGER_TABLES)
