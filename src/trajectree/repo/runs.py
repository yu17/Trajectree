"""Run repository: LLM runs and their accounting, kept outside the ledger."""

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

import apsw

from trajectree.ids import new_id
from trajectree.models import Proposal, Run, RunKind, UsageDay, UsageSlice
from trajectree.repo import NotFound
from trajectree.repo._rows import select_row, select_rows, update_fields
from trajectree.db.schema import table_columns
from trajectree.timeutil import to_db, utcnow

_DATETIME_FIELDS = ("started", "finished", "created_at")


def _to_run(conn: apsw.Connection, row: tuple[Any, ...]) -> Run:
    data = dict(zip(table_columns(conn, "run"), row))
    data["props"] = json.loads(data["props"])
    data["proposals"] = json.loads(data["proposals"])
    return Run.model_validate(data)


def create_run(
    conn: apsw.Connection,
    *,
    kind: RunKind,
    runner: str,
    backend: str,
    model: str | None,
    project_id: str | None,
    props: dict | None = None,
) -> Run:
    run_id = new_id()
    conn.execute(
        "INSERT INTO run (id, kind, project_id, status, runner, backend, model, props, created_at)"
        " VALUES (?, ?, ?, 'queued', ?, ?, ?, ?, ?)",
        (run_id, kind, project_id, runner, backend, model, json.dumps(props or {}), to_db(utcnow())),
    )
    run = get_run(conn, run_id)
    assert run is not None
    return run


def get_run(conn: apsw.Connection, run_id: str) -> Run | None:
    row = select_row(conn, "run", run_id)
    return None if row is None else _to_run(conn, row)


def update_run(conn: apsw.Connection, run_id: str, **fields: Any) -> Run:
    if select_row(conn, "run", run_id) is None:
        raise NotFound(f"run {run_id} not found")
    stored: dict[str, Any] = {}
    for name, value in fields.items():
        if name in _DATETIME_FIELDS and value is not None:
            value = to_db(value)
        elif name == "proposals":
            value = json.dumps([Proposal.model_validate(p).model_dump(mode="json") for p in value])
        elif name == "props":
            value = json.dumps(value)
        stored[name] = value
    update_fields(conn, "run", run_id, stored)
    run = get_run(conn, run_id)
    assert run is not None
    return run


def list_runs(
    conn: apsw.Connection,
    *,
    limit: int = 50,
    kind: str | None = None,
    status: str | None = None,
    with_open_proposals: bool = False,
) -> list[Run]:
    where: list[str] = []
    params: list[Any] = []
    if kind is not None:
        where.append("kind = ?")
        params.append(kind)
    if status is not None:
        where.append("status = ?")
        params.append(status)
    if with_open_proposals:
        where.append(
            "EXISTS (SELECT 1 FROM json_each(run.proposals)"
            " WHERE json_extract(value, '$.applied_txn') IS NULL)"
        )
    rows = select_rows(conn, "run", where, params, f"created_at DESC, id DESC LIMIT {int(limit)}")
    return [_to_run(conn, r) for r in rows]


def oldest_queued(conn: apsw.Connection) -> Run | None:
    rows = select_rows(conn, "run", ["status = 'queued'"], [], "created_at, id LIMIT 1")
    return _to_run(conn, rows[0]) if rows else None


_DAY = "substr(coalesce(started, created_at), 1, 10)"


def spent_usd(conn: apsw.Connection, *, day: str) -> float:
    row = conn.execute(f"SELECT COALESCE(SUM(est_usd), 0) FROM run WHERE {_DAY} = ?", (day,)).fetchone()
    return float(row[0])


def usage_days(conn: apsw.Connection, *, days: int, now: datetime) -> list[UsageDay]:
    """One row per UTC day ending today, zero-filled, ascending."""
    today = now.astimezone(timezone.utc).date()
    first = today - timedelta(days=days - 1)
    found = {
        r[0]: r
        for r in conn.execute(
            f"SELECT {_DAY} AS day, SUM(est_usd), COUNT(*), SUM(tokens_in), SUM(tokens_out)"
            f" FROM run WHERE {_DAY} >= ? AND {_DAY} <= ? GROUP BY day",
            (first.isoformat(), today.isoformat()),
        )
    }
    out = []
    for i in range(days):
        day = (first + timedelta(days=i)).isoformat()
        r = found.get(day)
        out.append(
            UsageDay(day=day, est_usd=r[1], runs=r[2], tokens_in=r[3], tokens_out=r[4])
            if r
            else UsageDay(day=day, est_usd=0.0, runs=0, tokens_in=0, tokens_out=0)
        )
    return out


def usage_by(
    conn: apsw.Connection, *, field: Literal["kind", "runner"], since: datetime
) -> list[UsageSlice]:
    if field not in ("kind", "runner"):
        raise ValueError(f"cannot group usage by {field!r}")
    rows = conn.execute(
        f"SELECT {field}, SUM(est_usd), COUNT(*) FROM run"
        " WHERE coalesce(started, created_at) >= ? GROUP BY 1 ORDER BY 2 DESC, 1",
        (to_db(since),),
    )
    return [UsageSlice(key=k, est_usd=usd, runs=n) for k, usd, n in rows]


def txn_ids_for_run(conn: apsw.Connection, run_id: str) -> list[str]:
    return [r[0] for r in conn.execute("SELECT id FROM txn WHERE run_id = ? ORDER BY id", (run_id,))]
