"""Run caps, stored as one JSON row in the `setting` table; not a ledger table."""

import json
import logging

import apsw
from pydantic import ValidationError

from trajectree.models import Caps

log = logging.getLogger("trajectree.caps")


def get_caps(conn: apsw.Connection) -> Caps:
    """The stored caps, or the defaults when absent or unreadable."""
    row = conn.execute("SELECT value FROM setting WHERE key = 'caps'").fetchone()
    if row is None:
        return Caps()
    try:
        raw = json.loads(row[0])
    except ValueError:
        log.warning("stored caps are not valid JSON; using defaults")
        return Caps()
    try:
        return Caps.model_validate(raw)
    except ValidationError:
        log.warning("stored caps are invalid; using defaults (keeping `paused`)")
        # A bad number must never un-pause the runner.
        return Caps(paused=bool(raw.get("paused", False))) if isinstance(raw, dict) else Caps()


def set_caps(conn: apsw.Connection, caps: Caps) -> None:
    conn.execute(
        "INSERT INTO setting (key, value) VALUES ('caps', ?)"
        " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (caps.model_dump_json(),),
    )
