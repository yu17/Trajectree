"""Run caps, stored as one JSON row in the `setting` table; not a ledger table."""

import json

import apsw
from pydantic import ValidationError

from trajectree.models import Caps


def get_caps(conn: apsw.Connection) -> Caps:
    """The stored caps, or the defaults when absent or unreadable."""
    row = conn.execute("SELECT value FROM setting WHERE key = 'caps'").fetchone()
    if row is None:
        return Caps()
    try:
        return Caps.model_validate(json.loads(row[0]))
    except (ValueError, ValidationError):
        return Caps()


def set_caps(conn: apsw.Connection, caps: Caps) -> None:
    conn.execute(
        "INSERT INTO setting (key, value) VALUES ('caps', ?)"
        " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (caps.model_dump_json(),),
    )
