"""Signal repository: connector observations kept outside the ledger."""

import json
from datetime import datetime
from typing import Any

import apsw

from trajectree.db.schema import table_columns
from trajectree.ids import new_id
from trajectree.models import Signal, SignalIn, StripBin
from trajectree.timeutil import to_db, utcnow


def upsert_signals(conn: apsw.Connection, rows: list[SignalIn]) -> int:
    """Insert or refresh signals keyed by `dedupe_key`; returns the number of rows changed.

    A conflicting row is rewritten (and its `updated_at` bumped) only when its span, payload or
    project differs, so a repeat pass over unchanged data counts 0 and moves nothing.
    """
    now = to_db(utcnow())
    changed = 0
    with conn:
        for r in rows:
            conn.execute(
                "INSERT INTO signal (id, project_id, resource_id, kind, t_start, t_end, payload,"
                " dedupe_key, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(dedupe_key) DO UPDATE SET project_id = excluded.project_id,"
                " resource_id = excluded.resource_id, t_start = excluded.t_start,"
                " t_end = excluded.t_end, payload = excluded.payload,"
                " updated_at = excluded.updated_at"
                " WHERE signal.t_start IS NOT excluded.t_start OR signal.t_end IS NOT excluded.t_end"
                " OR signal.payload IS NOT excluded.payload OR signal.project_id IS NOT excluded.project_id",
                (
                    new_id(), r.project_id, r.resource_id, r.kind, to_db(r.t_start),
                    to_db(r.t_end) if r.t_end else None, json.dumps(r.payload),
                    r.dedupe_key, now,
                ),
            )
            changed += conn.changes()
    return changed


def _like_prefix(prefix: str) -> str:
    """A LIKE pattern (with `ESCAPE '\\'`) matching every text that starts with `prefix`."""
    return prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def delete_signals_by_prefix_except(conn: apsw.Connection, prefix: str, keep: set[str], *, kind: str) -> int:
    """Delete the `kind` signals whose `dedupe_key` starts with `prefix` and is not in `keep`;
    returns how many were deleted."""
    rows = conn.execute(
        "SELECT id, dedupe_key FROM signal WHERE kind = ? AND dedupe_key LIKE ? ESCAPE '\\'",
        (kind, _like_prefix(prefix)),
    ).fetchall()
    stale = [sid for sid, key in rows if key not in keep]
    with conn:
        for i in range(0, len(stale), 500):
            chunk = stale[i : i + 500]
            conn.execute(f"DELETE FROM signal WHERE id IN ({', '.join('?' * len(chunk))})", chunk)
    return len(stale)


def _where(
    project_ids: list[str] | None,
    t_from: datetime | None,
    t_to: datetime | None,
    kinds: list[str] | None = None,
) -> tuple[list[str], list[Any]]:
    where: list[str] = []
    params: list[Any] = []
    if project_ids is not None:
        where.append(f"project_id IN ({', '.join('?' * len(project_ids))})")
        params.extend(project_ids)
    if kinds is not None:
        where.append(f"kind IN ({', '.join('?' * len(kinds))})")
        params.extend(kinds)
    if t_to is not None:
        where.append("t_start <= ?")
        params.append(to_db(t_to))
    if t_from is not None:
        where.append("coalesce(t_end, t_start) >= ?")
        params.append(to_db(t_from))
    return where, params


def get_signal_by_key(conn: apsw.Connection, dedupe_key: str) -> Signal | None:
    cols = table_columns(conn, "signal")
    row = conn.execute(
        f"SELECT {', '.join(cols)} FROM signal WHERE dedupe_key = ?", (dedupe_key,)
    ).fetchone()
    if row is None:
        return None
    data = dict(zip(cols, row))
    data["payload"] = json.loads(data["payload"])
    return Signal.model_validate(data)


def session_signals_for(conn: apsw.Connection, session_id: str) -> list[Signal]:
    """Every `session` signal of one session id (Claude- or Codex-format), earliest first.

    Matches the pre-segment key `<source>_session:<id>` and the segment keys `<source>_session:<id>:<minute>`.
    """
    cols = table_columns(conn, "signal")
    escaped = session_id.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    keys = [f"{src}_session:{session_id}" for src in ("claude", "codex")]
    likes = [f"{src}_session:{escaped}:%" for src in ("claude", "codex")]
    out = []
    for row in conn.execute(
        f"SELECT {', '.join(cols)} FROM signal WHERE dedupe_key IN (?, ?)"
        " OR dedupe_key LIKE ? ESCAPE '\\' OR dedupe_key LIKE ? ESCAPE '\\'"
        " ORDER BY t_start, id",
        [*keys, *likes],
    ):
        data = dict(zip(cols, row))
        data["payload"] = json.loads(data["payload"])
        out.append(Signal.model_validate(data))
    return out


def list_signals(
    conn: apsw.Connection,
    *,
    project_ids: list[str] | None = None,
    t_from: datetime | None = None,
    t_to: datetime | None = None,
    kinds: list[str] | None = None,
    limit: int = 500,
    newest_first: bool = False,
) -> list[Signal]:
    where, params = _where(project_ids, t_from, t_to, kinds)
    sql = f"SELECT {', '.join(table_columns(conn, 'signal'))} FROM signal"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY t_start DESC, id DESC LIMIT ?" if newest_first else " ORDER BY t_start, id LIMIT ?"
    out = []
    for row in conn.execute(sql, [*params, limit]):
        data = dict(zip(table_columns(conn, "signal"), row))
        data["payload"] = json.loads(data["payload"])
        out.append(Signal.model_validate(data))
    return out


def count_signals(
    conn: apsw.Connection,
    *,
    project_ids: list[str] | None = None,
    t_from: datetime | None = None,
    t_to: datetime | None = None,
) -> int:
    where, params = _where(project_ids, t_from, t_to)
    sql = "SELECT count(*) FROM signal" + (" WHERE " + " AND ".join(where) if where else "")
    return conn.execute(sql, params).fetchone()[0]


def latest_signals(
    conn: apsw.Connection,
    *,
    project_id: str,
    t_from: datetime | None,
    t_to: datetime | None,
    n: int,
) -> list[Signal]:
    """The newest `n` signals of one project in the window, returned oldest first."""
    where, params = _where([project_id], t_from, t_to)
    cols = table_columns(conn, "signal")
    sql = f"SELECT {', '.join(cols)} FROM signal WHERE {' AND '.join(where)}"
    sql += " ORDER BY t_start DESC, id DESC LIMIT ?"
    out = []
    for row in conn.execute(sql, [*params, n]):
        data = dict(zip(cols, row))
        data["payload"] = json.loads(data["payload"])
        out.append(Signal.model_validate(data))
    out.reverse()
    return out


def strip_bins(
    conn: apsw.Connection,
    *,
    project_ids: list[str] | None,
    t_from: datetime,
    t_to: datetime,
) -> list[StripBin]:
    """Per (project, UTC day, kind) counts: commits for `commits` signals, signals otherwise."""
    where, params = _where(project_ids, t_from, t_to)
    sql = (
        "SELECT project_id, substr(t_start, 1, 10) AS day, kind,"
        " CASE WHEN kind = 'commits' THEN SUM(CASE WHEN typeof(json_extract(payload, '$.count')) IN ('integer', 'real')"
        " THEN json_extract(payload, '$.count') ELSE 1 END)"
        " ELSE COUNT(*) END"
        " FROM signal"
    )
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " GROUP BY project_id, day, kind ORDER BY day, project_id, kind"
    return [
        StripBin(project_id=p, day=d, kind=k, count=c) for p, d, k, c in conn.execute(sql, params)
    ]
