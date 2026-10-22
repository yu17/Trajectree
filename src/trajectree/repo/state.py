"""Key/value state repository (connector cursors and the like); not a ledger table."""

import json
from typing import Any

import apsw

from trajectree.timeutil import to_db, utcnow


def get_state(conn: apsw.Connection, key: str) -> Any | None:
    row = conn.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
    return json.loads(row[0]) if row else None


def set_state(conn: apsw.Connection, key: str, value: Any) -> None:
    conn.execute(
        "INSERT INTO state (key, value, updated_at) VALUES (?, ?, ?)"
        " ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (key, json.dumps(value), to_db(utcnow())),
    )
