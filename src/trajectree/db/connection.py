"""Open the Trajectree SQLite database with the standard pragmas."""

from pathlib import Path

import apsw

from trajectree.db.schema import migrate


def open_db(path: Path | str) -> apsw.Connection:
    conn = apsw.Connection(str(path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    migrate(conn)
    return conn
