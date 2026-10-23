import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from trajectree.models import ProjectIn, RoutineIn, RoutineScope
from trajectree.repo.projects import create_project
from trajectree.repo.routines import create_routine, get_routine
from trajectree.repo.runs import get_run, list_runs, update_run
from trajectree.repo.sessions import end_session, start_session
from trajectree.repo.state import get_state
from trajectree.runner.kinds import AskRequest, RoutineRequest
from trajectree.runner.launch import RunOutcome
from trajectree.runner.service import RunService
from trajectree.scheduler import MAX_ATTEMPTS, RETRY_BACKOFF_S, Scheduler
from trajectree.timeutil import to_db

UTC = timezone.utc
T0 = datetime(2026, 1, 10, 12, tzinfo=UTC)


def _never(argv, *, log_path, **kw):
    """A launch that records nothing and returns an ok outcome (scheduler tests never execute)."""
    return RunOutcome(exit_code=0, timed_out=False, stdout="{}", stderr_tail="", log_path=str(log_path), duration_ms=1)


def _svc(ledger, settings, clock=None):
    return RunService(ledger, settings, launch=_never, now=clock or (lambda: T0))


def _routine(ledger, name="rev", trigger="* * * * *", **over):
    data = {"name": name, "trigger": trigger, "prompt_ref": "cross_project_review.md", "enabled": True, **over}
    return create_routine(ledger.conn, RoutineIn(**data), now=T0)


def test_constants():
    assert RETRY_BACKOFF_S == (60, 300, 900) and MAX_ATTEMPTS == 3


def test_tick_fires_due_routine_and_recomputes(ledger, settings, logged_in):
    r = create_routine(ledger.conn, RoutineIn(name="rev", trigger="* * * * *", prompt_ref="cross_project_review.md", enabled=True), now=T0)
    now = T0 + timedelta(minutes=2)
    sched = Scheduler(ledger, settings, _svc(ledger, settings), now=lambda: now)
    rep = sched.tick()
    assert rep.fired == ["rev"] and list_runs(ledger.conn, routine_id=r.id)[0].slot == "scheduled"
    assert get_routine(ledger.conn, r.id).next_fire > T0 + timedelta(minutes=2)
    run = list_runs(ledger.conn, routine_id=r.id)[0]
    assert run.props["request"]["reason"] == "cron" and rep.ticked == now and rep.errors == []
    assert sched.tick().fired == []   # next_fire moved past now
    status = sched.status(ledger.conn)
    assert status.last_tick == now and status.interval_s == 30
    assert [(f.routine, f.at) for f in status.next_fires] == [("rev", get_routine(ledger.conn, r.id).next_fire)]
    assert get_state(ledger.conn, "scheduler:last")["last_tick"] == to_db(now)


def test_tick_skips_missed_fire_beyond_grace(ledger, settings, logged_in):
    r = _routine(ledger)
    now = T0 + timedelta(hours=2)
    rep = Scheduler(ledger, settings, _svc(ledger, settings), now=lambda: now).tick()
    after = get_routine(ledger.conn, r.id)
    assert rep.skipped == ["rev"] and rep.fired == []
    assert after.last_error.startswith("missed fire") and after.last_error.endswith("skipped")
    assert list_runs(ledger.conn, routine_id=r.id) == [] and after.next_fire > now


def test_tick_skips_broken_routine_and_continues(ledger, settings, logged_in):
    a = _routine(ledger, name="a")
    b = _routine(ledger, name="b")
    ledger.conn.execute("UPDATE routine SET tz = 'Nowhere/X' WHERE id = ?", (a.id,))
    rep = Scheduler(ledger, settings, _svc(ledger, settings), now=lambda: T0 + timedelta(minutes=2)).tick()
    assert len(rep.errors) == 1 and rep.errors[0].startswith("a: ")
    assert rep.fired == ["b"] and len(list_runs(ledger.conn, routine_id=b.id)) == 1
    stored = ledger.conn.execute("SELECT last_error FROM routine WHERE id = ?", (a.id,)).fetchone()[0]
    assert stored and list_runs(ledger.conn, routine_id=a.id) == []


def test_tick_is_not_reentrant(ledger, settings):
    sched = Scheduler(ledger, settings, _svc(ledger, settings), now=lambda: T0)
    assert sched._busy.acquire(blocking=False)
    try:
        rep = sched.tick()
    finally:
        sched._busy.release()
    assert rep.errors == ["tick already running"] and rep.fired == [] and rep.ticked == T0
    assert get_state(ledger.conn, "scheduler:last") is None


def _fail_infra(svc, ledger, run_id):
    with ledger.locked() as conn:
        svc._fail(conn, run_id, "timeout after 1s", infra=True)
    return get_run(ledger.conn, run_id)


def test_retry_backoff_stops_after_three(ledger, settings, logged_in):
    rid = _routine(ledger, trigger="@daily", enabled=False).id
    clock = [T0]
    svc = _svc(ledger, settings, clock=lambda: clock[0])
    sched = Scheduler(ledger, settings, svc, now=lambda: clock[0])
    first = svc.enqueue(RoutineRequest(routine="rev", projects=None))
    failed = _fail_infra(svc, ledger, first.id)
    assert failed.props["failure"] == "infra" and failed.props["retry_at"] == to_db(T0 + timedelta(seconds=60))
    assert sched.tick().retried == []   # not yet
    clock[0] = T0 + timedelta(seconds=61)
    assert sched.tick().retried == ["rev"]
    second = list_runs(ledger.conn, routine_id=rid)[0]
    assert (second.attempt, second.props["request"]["reason"]) == (2, "retry")
    assert get_run(ledger.conn, first.id).props["retried"] == second.id
    assert sched.tick().retried == []   # retried once only
    assert _fail_infra(svc, ledger, second.id).props["retry_at"] == to_db(clock[0] + timedelta(seconds=300))
    clock[0] += timedelta(seconds=301)
    assert sched.tick().retried == ["rev"]
    third = list_runs(ledger.conn, routine_id=rid)[0]
    assert third.attempt == 3
    assert "retry_at" not in _fail_infra(svc, ledger, third.id).props   # attempts exhausted
    clock[0] += timedelta(seconds=1000)
    assert sched.tick().retried == []
    assert [r.attempt for r in list_runs(ledger.conn, routine_id=rid)] == [3, 2, 1]


def test_retry_keeps_the_original_projects(ledger, human, settings, logged_in):
    create_project(ledger, human, ProjectIn(slug="a", title="A", kind="research"))
    rid = _routine(ledger, trigger="@daily", enabled=False).id
    svc = _svc(ledger, settings)
    first = svc.enqueue(RoutineRequest(routine="rev", projects=["a"], reason="session_end"))
    _fail_infra(svc, ledger, first.id)
    Scheduler(ledger, settings, svc, now=lambda: T0 + timedelta(hours=1)).tick()
    retry = list_runs(ledger.conn, routine_id=rid)[0]
    assert retry.props["request"]["projects"] == ["a"] and retry.attempt == 2


def test_content_failure_not_retried(ledger, settings, logged_in):
    rid = _routine(ledger, trigger="@daily", enabled=False).id
    svc = _svc(ledger, settings)
    failed = svc.enqueue(RoutineRequest(routine="rev"))
    with ledger.locked() as conn:
        svc._fail(conn, failed.id, "model said no")
    capped = svc.enqueue(RoutineRequest(routine="rev"))
    update_run(ledger.conn, capped.id, status="capped", props={**capped.props, "failure": "infra",
                                                                "retry_at": to_db(T0 - timedelta(hours=1))})
    ask = svc.enqueue(AskRequest(question="q"))
    assert "retry_at" not in _fail_infra(svc, ledger, ask.id).props   # only routine runs retry
    assert "retry_at" not in get_run(ledger.conn, failed.id).props
    rep = Scheduler(ledger, settings, svc, now=lambda: T0 + timedelta(hours=1)).tick()
    assert rep.retried == [] and len(list_runs(ledger.conn, routine_id=rid)) == 2


def test_session_end_trigger_fires_once_per_session(ledger, human, settings, logged_in):
    a = create_project(ledger, human, ProjectIn(slug="a", title="A", kind="research"))
    b = create_project(ledger, human, ProjectIn(slug="b", title="B", kind="research"))
    r = _routine(ledger, name="wrap", trigger="session_end", scope=RoutineScope(projects=["a"]))
    start_session(ledger.conn, session_id="s1", cwd="/a", transcript_path=None, project_id=a.id, source="startup", now=T0)
    start_session(ledger.conn, session_id="s2", cwd="/b", transcript_path=None, project_id=b.id, source="startup", now=T0)
    start_session(ledger.conn, session_id="s3", cwd="/x", transcript_path=None, project_id=None, source="startup", now=T0)
    for sid in ("s1", "s2", "s3"):
        end_session(ledger.conn, session_id=sid, reason="other", now=T0 + timedelta(minutes=1))
    svc = _svc(ledger, settings)
    early = Scheduler(ledger, settings, svc, now=lambda: T0 + timedelta(minutes=2)).tick()
    assert early.fired == []   # still inside the debounce window
    sched = Scheduler(ledger, settings, svc, now=lambda: T0 + timedelta(minutes=10))
    assert sched.tick().fired == ["wrap"]
    assert sched.tick().fired == []
    runs = list_runs(ledger.conn, routine_id=r.id)
    assert len(runs) == 1 and runs[0].props["request"]["projects"] == ["a"]
    assert runs[0].props["request"]["reason"] == "session_end"
    assert get_state(ledger.conn, f"scheduler:session_end:{r.id}") == ["s1"]


def test_session_end_debounce_comes_from_props(ledger, human, settings, logged_in):
    a = create_project(ledger, human, ProjectIn(slug="a", title="A", kind="research"))
    r = _routine(ledger, name="wrap", trigger="session_end", props={"debounce_s": 30})
    start_session(ledger.conn, session_id="s1", cwd="/a", transcript_path=None, project_id=a.id, source="startup", now=T0)
    end_session(ledger.conn, session_id="s1", reason="other", now=T0)
    assert Scheduler(ledger, settings, _svc(ledger, settings), now=lambda: T0 + timedelta(minutes=1)).tick().fired == ["wrap"]
    assert len(list_runs(ledger.conn, routine_id=r.id)) == 1


@pytest.mark.anyio
async def test_loop_ticks_until_stopped(ledger, settings):
    sched = Scheduler(ledger, settings, _svc(ledger, settings), now=lambda: T0)
    stop = asyncio.Event()
    task = asyncio.create_task(sched.loop(stop, interval_s=0.02))
    for _ in range(100):
        await asyncio.sleep(0.02)
        if get_state(ledger.conn, "scheduler:last") is not None:
            break
    stop.set()
    await asyncio.wait_for(task, 5)
    assert get_state(ledger.conn, "scheduler:last")["last_tick"] == to_db(T0)
