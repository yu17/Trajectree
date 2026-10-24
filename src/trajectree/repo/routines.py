"""Routine repository: scheduled agent routines, kept outside the ledger like `run`."""

import json
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

import apsw
from croniter import croniter

from trajectree.db.schema import table_columns
from trajectree.ids import new_id
from trajectree.models import NO_MODEL_CHOSEN, Routine, RoutineIn, RoutinePatch
from trajectree.repo import DuplicateRoutine, NotFound
from trajectree.repo._rows import select_row, select_rows, update_fields
from trajectree.timeutil import from_db, to_db

_DATETIME_FIELDS = ("next_fire", "last_fired", "paused_until")
_STATE_FIELDS = ("next_fire", "last_fired", "last_run_id", "last_error", "paused_until")
_JSON_FIELDS = ("scope", "tool_policy", "caps", "props")


def _to_routine(conn: apsw.Connection, row: tuple[Any, ...]) -> Routine:
    data = dict(zip(table_columns(conn, "routine"), row))
    for name in _JSON_FIELDS:
        data[name] = json.loads(data[name])
    data["enabled"] = bool(data["enabled"])
    return Routine.model_validate(data)


def compute_next_fire(trigger: str, tz: str, *, after: datetime) -> datetime | None:
    """The next UTC fire time of `trigger` (evaluated in `tz`) after `after`; None for session_end."""
    if trigger == "session_end":
        return None
    nxt = croniter(trigger, after.astimezone(ZoneInfo(tz))).get_next(datetime)
    return nxt.astimezone(timezone.utc)


def get_routine(conn: apsw.Connection, routine_id: str) -> Routine | None:
    row = select_row(conn, "routine", routine_id)
    return None if row is None else _to_routine(conn, row)


def get_routine_by_name(conn: apsw.Connection, name: str) -> Routine | None:
    rows = select_rows(conn, "routine", ["name = ?"], [name], "id LIMIT 1")
    return _to_routine(conn, rows[0]) if rows else None


def resolve_routine_ref(conn: apsw.Connection, ref: str) -> Routine:
    """Look a routine up by id, then by name."""
    routine = get_routine(conn, ref) or get_routine_by_name(conn, ref)
    if routine is None:
        raise NotFound(f"routine {ref} not found")
    return routine


def list_routines(conn: apsw.Connection) -> list[Routine]:
    return [_to_routine(conn, r) for r in select_rows(conn, "routine", [], [], "name")]


def create_routine(conn: apsw.Connection, data: RoutineIn, *, now: datetime) -> Routine:
    routine_id = new_id()
    next_fire = compute_next_fire(data.trigger, data.tz, after=now) if data.enabled else None
    stamp = to_db(now)
    try:
        conn.execute(
            "INSERT INTO routine (id, name, trigger, tz, enabled, runner, model, prompt_ref, scope,"
            " cwd_mode, tool_policy, caps, write_mode, delivery, next_fire, props, created_at,"
            " updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                routine_id, data.name, data.trigger, data.tz, int(data.enabled), data.runner,
                data.model, data.prompt_ref, data.scope.model_dump_json(), data.cwd_mode,
                json.dumps(data.tool_policy), data.caps.model_dump_json(), data.write_mode,
                data.delivery, to_db(next_fire) if next_fire else None, json.dumps(data.props),
                stamp, stamp,
            ),
        )
    except apsw.ConstraintError:
        raise DuplicateRoutine(data.name) from None
    routine = get_routine(conn, routine_id)
    assert routine is not None
    return routine


def update_routine(
    conn: apsw.Connection, routine_id: str, patch: RoutinePatch, *, now: datetime
) -> Routine:
    current = get_routine(conn, routine_id)
    if current is None:
        raise NotFound(f"routine {routine_id} not found")
    changes = patch.model_dump(exclude_unset=True, mode="json")
    fields: dict[str, Any] = {}
    for name, value in changes.items():
        if name in _JSON_FIELDS:
            value = json.dumps(value)
        elif name == "enabled":
            value = int(value)
        fields[name] = value
    if {"trigger", "tz", "enabled"} & changes.keys():
        merged = current.model_copy(update={k: v for k, v in changes.items() if k in ("trigger", "tz", "enabled")})
        nxt = compute_next_fire(merged.trigger, merged.tz, after=now) if merged.enabled else None
        fields["next_fire"] = to_db(nxt) if nxt else None
    fields["updated_at"] = to_db(now)
    try:
        update_fields(conn, "routine", routine_id, fields)
    except apsw.ConstraintError:
        raise DuplicateRoutine(changes.get("name", current.name)) from None
    routine = get_routine(conn, routine_id)
    assert routine is not None
    return routine


def set_run_state(conn: apsw.Connection, routine_id: str, **fields: Any) -> Routine:
    """Record scheduler state (`next_fire`, `last_fired`, `last_run_id`, `last_error`, `paused_until`)."""
    unknown = set(fields) - set(_STATE_FIELDS)
    if unknown:
        raise ValueError(f"not a run-state field: {sorted(unknown)}")
    if select_row(conn, "routine", routine_id) is None:
        raise NotFound(f"routine {routine_id} not found")
    stored = {
        name: to_db(value) if name in _DATETIME_FIELDS and value is not None else value
        for name, value in fields.items()
    }
    update_fields(conn, "routine", routine_id, stored)
    routine = get_routine(conn, routine_id)
    assert routine is not None
    return routine


_DUE = (
    "enabled = 1 AND trigger != 'session_end' AND next_fire IS NOT NULL AND next_fire <= ?"
    " AND (paused_until IS NULL OR paused_until <= ?)"
)


def due_routines(conn: apsw.Connection, *, now: datetime) -> list[Routine]:
    """Enabled, time-triggered routines whose `next_fire` has passed and that are not paused."""
    stamp = to_db(now)
    rows = select_rows(conn, "routine", [_DUE], [stamp, stamp], "next_fire, name")
    return [_to_routine(conn, r) for r in rows]


# The scheduler lists routines by (id, name) without validating rows, so one corrupt routine is
# reported on its own instead of failing the listing for every other routine.


def due_routine_refs(conn: apsw.Connection, *, now: datetime) -> list[tuple[str, str]]:
    """`(id, name)` of each routine `due_routines` would return."""
    stamp = to_db(now)
    return [
        (r[0], r[1])
        for r in conn.execute(f"SELECT id, name FROM routine WHERE {_DUE} ORDER BY next_fire, name", (stamp, stamp))
    ]


def session_end_routine_refs(conn: apsw.Connection) -> list[tuple[str, str]]:
    """`(id, name)` of each enabled routine triggered by `session_end`."""
    return [
        (r[0], r[1])
        for r in conn.execute(
            "SELECT id, name FROM routine WHERE enabled = 1 AND trigger = 'session_end' ORDER BY name"
        )
    ]


def routine_names(conn: apsw.Connection) -> dict[str, str]:
    return {r[0]: r[1] for r in conn.execute("SELECT id, name FROM routine")}


def next_fires(conn: apsw.Connection) -> list[tuple[str, datetime]]:
    """`(name, next_fire)` of each enabled routine with a next fire time, soonest first."""
    rows = conn.execute(
        "SELECT name, next_fire FROM routine WHERE enabled = 1 AND next_fire IS NOT NULL"
        " ORDER BY next_fire, name"
    )
    return [(name, from_db(at)) for name, at in rows]


def disable_modelless_routines(conn: apsw.Connection, *, now: datetime) -> list[str]:
    """Disable every enabled routine that names neither a runner nor a model, recording why; return their names."""
    names = [
        r[0] for r in conn.execute(
            "SELECT name FROM routine WHERE enabled = 1 AND (model IS NULL OR model = '') AND (runner IS NULL OR runner = '') ORDER BY name"
        )
    ]
    if names:
        conn.execute(
            "UPDATE routine SET enabled = 0, next_fire = NULL, last_error = ?, updated_at = ?"
            " WHERE enabled = 1 AND (model IS NULL OR model = '') AND (runner IS NULL OR runner = '')",
            (f"disabled: {NO_MODEL_CHOSEN}", to_db(now)),
        )
    return names


def record_error(conn: apsw.Connection, routine_id: str, error: str) -> None:
    """Store `last_error` without reading the routine back (it may not validate)."""
    conn.execute("UPDATE routine SET last_error = ? WHERE id = ?", (error, routine_id))
