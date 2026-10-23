import apsw

from trajectree.db.connection import open_db
from trajectree.db.schema import LEDGER_TABLES, MIGRATIONS, migrate, table_columns
from trajectree.repo.runs import get_run


def test_v4_tables_and_columns(conn):
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 4
    assert {"routine_id", "slot", "attempt"} <= set(table_columns(conn, "run"))
    assert "report" in LEDGER_TABLES and "routine" not in LEDGER_TABLES
    assert table_columns(conn, "routine")[:3] == ["id", "name", "trigger"]
    assert table_columns(conn, "report")[:3] == ["id", "title", "kind"]


def test_v3_database_migrates_to_v4(tmp_path):
    conn = apsw.Connection(str(tmp_path / "old.db"))
    for _, stmts in MIGRATIONS[:3]:
        for s in stmts:
            conn.execute(s)
    conn.execute("PRAGMA user_version = 3")
    conn.execute(
        "INSERT INTO run (id, kind, status, runner, backend, created_at)"
        " VALUES ('r1', 'ask', 'ok', 'claude', 'claude', '2026-01-05T10:00:00Z')"
    )
    migrate(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 4
    run = get_run(conn, "r1")
    assert run.slot == "interactive" and run.attempt == 1 and run.routine_id is None
    conn.close()
    assert open_db(tmp_path / "old.db").execute("PRAGMA user_version").fetchone()[0] == 4
