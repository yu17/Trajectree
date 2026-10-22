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
    """Insert or refresh signals keyed by `dedupe_key`; returns the number of rows given."""
    now = to_db(utcnow())
    with conn:
        for r in rows:
            conn.execute(
                "INSERT INTO signal (id, project_id, resource_id, kind, t_start, t_end, payload,"
                " dedupe_key, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(dedupe_key) DO UPDATE SET project_id = excluded.project_id,"
                " resource_id = excluded.resource_id, t_start = excluded.t_start,"
                " t_end = excluded.t_end, payload = excluded.payload,"
                " updated_at = excluded.updated_at",
                (
                    new_id(), r.project_id, r.resource_id, r.kind, to_db(r.t_start),
                    to_db(r.t_end) if r.t_end else None, json.dumps(r.payload),
                    r.dedupe_key, now,
                ),
            )
    return len(rows)


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


def list_signals(
    conn: apsw.Connection,
    *,
    project_ids: list[str] | None = None,
    t_from: datetime | None = None,
    t_to: datetime | None = None,
    kinds: list[str] | None = None,
    limit: int = 500,
) -> list[Signal]:
    where, params = _where(project_ids, t_from, t_to, kinds)
    sql = f"SELECT {', '.join(table_columns(conn, 'signal'))} FROM signal"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY t_start, id LIMIT ?"
    out = []
    for row in conn.execute(sql, [*params, limit]):
        data = dict(zip(table_columns(conn, "signal"), row))
        data["payload"] = json.loads(data["payload"])
        out.append(Signal.model_validate(data))
    return out


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


def stale_projects(conn: apsw.Connection, *, settled_before: datetime) -> list[str]:
    """Active projects whose earliest activity since the last summary has settled."""
    return [
        r[0]
        for r in conn.execute(
            "SELECT p.id FROM project p WHERE p.status = 'active' AND ("
            " SELECT MIN(ts) FROM ("
            "  SELECT t_start AS ts FROM signal WHERE project_id = p.id"
            "  UNION ALL SELECT created_at FROM event WHERE project_id = p.id"
            " ) WHERE ts > coalesce(p.summary_at, '')) <= ? ORDER BY p.id",
            (to_db(settled_before),),
        )
    ]
