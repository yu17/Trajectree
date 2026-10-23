from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from trajectree.models import RoutineIn, RoutinePatch
from trajectree.repo import DuplicateRoutine, NotFound
from trajectree.repo.routines import (
    compute_next_fire,
    create_routine,
    due_routines,
    get_routine,
    get_routine_by_name,
    list_routines,
    resolve_routine_ref,
    set_run_state,
    update_routine,
)

UTC = timezone.utc
NOW = datetime(2026, 1, 10, 12, tzinfo=UTC)


def test_create_and_resolve_routine(conn):
    r = create_routine(conn, RoutineIn(name="review", trigger="0 7 * * 1", prompt_ref="cross_project_review.md", enabled=True), now=NOW)
    assert r.next_fire is not None and r.next_fire > NOW and r.next_fire.tzinfo is not None
    assert resolve_routine_ref(conn, "review").id == r.id == resolve_routine_ref(conn, r.id).id
    assert get_routine_by_name(conn, "review").id == r.id and get_routine(conn, "nope") is None
    with pytest.raises(NotFound):
        resolve_routine_ref(conn, "missing")
    with pytest.raises(DuplicateRoutine):
        create_routine(conn, RoutineIn(name="review", trigger="@daily", prompt_ref="x.md"), now=NOW)


def test_roundtrips_nested_fields(conn):
    r = create_routine(
        conn,
        RoutineIn(name="a", trigger="@daily", prompt_ref="a.md", scope={"projects": ["p"], "lookback_days": 7},
                  tool_policy=["Read"], caps={"usd_per_run": 0.5, "usd_per_day": 1.0}, props={"k": 1}),
        now=NOW,
    )
    got = get_routine(conn, r.id)
    assert got.scope.projects == ["p"] and got.scope.lookback_days == 7
    assert got.tool_policy == ["Read"] and got.caps.usd_per_run == 0.5 and got.props == {"k": 1}
    assert got.enabled is False and got.next_fire is None
    assert [x.name for x in list_routines(conn)] == ["a"]


def test_next_fire_respects_tz():
    nxt = compute_next_fire("0 7 * * *", "Europe/Berlin", after=datetime(2026, 1, 10, 12, tzinfo=UTC))
    assert nxt == datetime(2026, 1, 11, 6, tzinfo=UTC)  # 07:00 Berlin is 06:00 UTC in winter
    assert nxt.utcoffset() == timedelta(0)
    assert compute_next_fire("session_end", "UTC", after=NOW) is None


def test_due_routines_and_pause(conn):
    due = create_routine(conn, RoutineIn(name="due", trigger="@hourly", prompt_ref="a.md", enabled=True), now=NOW)
    off = create_routine(conn, RoutineIn(name="off", trigger="@hourly", prompt_ref="a.md"), now=NOW)
    paused = create_routine(conn, RoutineIn(name="paused", trigger="@hourly", prompt_ref="a.md", enabled=True), now=NOW)
    ended = create_routine(conn, RoutineIn(name="ended", trigger="session_end", prompt_ref="a.md", enabled=True), now=NOW)
    later = NOW + timedelta(hours=2)
    set_run_state(conn, paused.id, paused_until=later + timedelta(hours=1))
    set_run_state(conn, off.id, next_fire=NOW)
    set_run_state(conn, ended.id, next_fire=NOW)
    assert [r.id for r in due_routines(conn, now=NOW)] == []
    assert [r.id for r in due_routines(conn, now=later)] == [due.id]
    assert {r.id for r in due_routines(conn, now=later + timedelta(hours=2))} == {due.id, paused.id}


def test_update_routine_recomputes_next_fire(conn):
    r = create_routine(conn, RoutineIn(name="a", trigger="0 7 * * *", prompt_ref="a.md", enabled=True), now=NOW)
    off = update_routine(conn, r.id, RoutinePatch(enabled=False), now=NOW)
    assert off.next_fire is None
    on = update_routine(conn, r.id, RoutinePatch(enabled=True), now=NOW)
    assert on.next_fire == r.next_fire
    moved = update_routine(conn, r.id, RoutinePatch(trigger="30 9 * * *"), now=NOW)
    assert moved.next_fire == datetime(2026, 1, 11, 9, 30, tzinfo=UTC)
    kept = update_routine(conn, r.id, RoutinePatch(model="m"), now=NOW + timedelta(days=3))
    assert kept.model == "m" and kept.next_fire == moved.next_fire
    with pytest.raises(NotFound):
        update_routine(conn, "nope", RoutinePatch(model="m"), now=NOW)
    with pytest.raises(DuplicateRoutine):
        create_routine(conn, RoutineIn(name="b", trigger="@daily", prompt_ref="a.md"), now=NOW)
        update_routine(conn, r.id, RoutinePatch(name="b"), now=NOW)


def test_set_run_state(conn):
    r = create_routine(conn, RoutineIn(name="a", trigger="@daily", prompt_ref="a.md"), now=NOW)
    got = set_run_state(conn, r.id, last_fired=NOW, last_run_id="run1", last_error="boom", paused_until=None)
    assert got.last_fired == NOW and got.last_run_id == "run1" and got.last_error == "boom"
    with pytest.raises(ValueError):
        set_run_state(conn, r.id, name="x")


def test_routine_in_rejects_bad_trigger_and_tz():
    with pytest.raises(ValidationError, match="cron expression"):
        RoutineIn(name="a", trigger="not a cron", prompt_ref="a.md")
    with pytest.raises(ValidationError, match="unknown timezone"):
        RoutineIn(name="a", trigger="@daily", tz="Mars/Olympus", prompt_ref="a.md")
    with pytest.raises(ValidationError):
        RoutineIn(name="a", trigger="@daily", prompt_ref="../etc/passwd")
    with pytest.raises(ValidationError, match="cron expression"):
        RoutinePatch(trigger="nope")
    with pytest.raises(ValidationError, match="unknown timezone"):
        RoutinePatch(tz="Mars/Olympus")


def test_routine_patch_rejects_explicit_null_for_required_fields():
    for name in ("name", "trigger", "tz", "prompt_ref", "enabled", "scope", "cwd_mode", "tool_policy", "caps",
                 "write_mode", "delivery", "props"):
        with pytest.raises(ValidationError, match="cannot be null"):
            RoutinePatch.model_validate({name: None})
    assert RoutinePatch(runner=None, model=None).model_fields_set == {"runner", "model"}
    assert RoutinePatch().model_dump(exclude_unset=True) == {}
