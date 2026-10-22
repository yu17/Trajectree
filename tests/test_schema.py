import apsw
import pytest

from trajectree.db.schema import LEDGER_TABLES, SCHEMA_VERSION, table_columns


def test_all_tables_exist(conn):
    names = {r[0] for r in conn.execute("select name from sqlite_master where type='table'")}
    assert {"project", "event", "todo", "resource", "edge", "txn", "change", "setting"} <= names


def test_user_version_set(conn):
    assert conn.execute("pragma user_version").fetchone()[0] == SCHEMA_VERSION


def test_wal_and_foreign_keys(conn):
    assert conn.execute("pragma journal_mode").fetchone()[0] == "wal"
    assert conn.execute("pragma foreign_keys").fetchone()[0] == 1


def test_event_requires_existing_project(conn):
    with pytest.raises(apsw.ConstraintError):
        conn.execute("insert into event(id, project_id, kind, title, t_start, created_at, updated_at) values ('e', 'missing', 'note', 't', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')")


def test_table_columns_lists_pk_first(conn):
    assert table_columns(conn, "event")[0] == "id"
    assert set(LEDGER_TABLES) <= {r[0] for r in conn.execute("select name from sqlite_master where type='table'")}
