"""The routine scheduler: cron fires within a grace window, retries of infrastructure failures and
`session_end` triggers. A routine that names no model is never fired.

One `tick` runs at a time (the daemon's loop and `POST /api/scheduler/tick` share it). Every DB
access holds the ledger lock; `RunService.enqueue` takes the lock itself, so it is called with the
lock released. A failure while handling one routine is written to that routine's `last_error` and
never stops the others.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable
from datetime import datetime, timedelta

import apsw
from pydantic import AwareDatetime, BaseModel

from trajectree.config import Settings
from trajectree.db.ledger import Ledger
from trajectree.models import NO_MODEL_CHOSEN, Routine
from trajectree.repo.projects import list_projects, resolve_project_ref
from trajectree.repo.routines import (
    compute_next_fire,
    due_routine_refs,
    get_routine,
    next_fires,
    record_error,
    session_end_routine_refs,
    set_run_state,
)
from trajectree.repo.runs import failed_retry_candidates, get_run, update_run
from trajectree.repo.sessions import ended_sessions_since
from trajectree.repo.state import get_state, set_state
from trajectree.runner.kinds import RoutineRequest
from trajectree.runner.routines import MAX_ATTEMPTS, RETRY_BACKOFF_S
from trajectree.runner.service import RunService
from trajectree.runner.spec import RunsPaused
from trajectree.timeutil import to_db, utcnow

__all__ = [
    "MAX_ATTEMPTS",
    "RETRY_BACKOFF_S",
    "NextFire",
    "Scheduler",
    "SchedulerReport",
    "SchedulerStatus",
]

log = logging.getLogger("trajectree.scheduler")

LAST_KEY = "scheduler:last"
HANDLED_CAP = 500  # session ids remembered per session_end routine
SESSION_LOOKBACK = timedelta(days=1)
DEFAULT_DEBOUNCE_S = 300


class NextFire(BaseModel):
    routine: str
    at: AwareDatetime


class SchedulerStatus(BaseModel):
    last_tick: AwareDatetime | None
    interval_s: int
    next_fires: list[NextFire]


class SchedulerReport(BaseModel):
    ticked: AwareDatetime
    fired: list[str] = []
    skipped: list[str] = []
    retried: list[str] = []
    errors: list[str] = []


class Scheduler:
    def __init__(
        self,
        ledger: Ledger,
        settings: Settings,
        runs: RunService,
        *,
        now: Callable[[], datetime] = utcnow,
    ) -> None:
        self.ledger = ledger
        self.settings = settings
        self.runs = runs
        self.now = now
        self._busy = threading.Lock()

    # -- status ----------------------------------------------------------------------------

    def status(self, conn: apsw.Connection) -> SchedulerStatus:
        stored = get_state(conn, LAST_KEY)
        last_tick = SchedulerStatus.model_validate(stored).last_tick if stored else None
        return self._status(conn, last_tick)

    def _status(self, conn: apsw.Connection, last_tick: datetime | None) -> SchedulerStatus:
        return SchedulerStatus(
            last_tick=last_tick,
            interval_s=self.settings.scheduler_interval_s,
            next_fires=[NextFire(routine=name, at=at) for name, at in next_fires(conn)],
        )

    # -- tick ------------------------------------------------------------------------------

    def tick(self) -> SchedulerReport:
        now = self.now()
        if not self._busy.acquire(blocking=False):
            return SchedulerReport(ticked=now, errors=["tick already running"])
        try:
            report = SchedulerReport(ticked=now)
            self._fire_due(now, report)
            self._retry_failed(now, report)
            self._fire_session_end(now, report)
            with self.ledger.locked() as conn:
                set_state(conn, LAST_KEY, self._status(conn, now).model_dump(mode="json"))
            return report
        finally:
            self._busy.release()

    def _error(self, report: SchedulerReport, routine_id: str, name: str, exc: Exception) -> None:
        message = f"{type(exc).__name__}: {exc}"[:500]
        log.warning("routine %s: %s", name, message)
        report.errors.append(f"{name}: {message}")
        try:
            with self.ledger.locked() as conn:
                record_error(conn, routine_id, message)
        except Exception:
            log.exception("could not record the error of routine %s", name)

    def _fire_due(self, now: datetime, report: SchedulerReport) -> None:
        grace = timedelta(seconds=self.settings.scheduler_grace_s)
        with self.ledger.locked() as conn:
            refs = due_routine_refs(conn, now=now)
        for routine_id, name in refs:
            try:
                with self.ledger.locked() as conn:
                    routine = get_routine(conn, routine_id)
                    if routine is None or routine.next_fire is None:
                        continue
                    missed = routine.next_fire < now - grace
                    # Move next_fire first: a failing enqueue never fires the same slot twice.
                    fields: dict = {"next_fire": compute_next_fire(routine.trigger, routine.tz, after=now)}
                    if not routine.model:
                        fields["last_error"] = NO_MODEL_CHOSEN  # never fired on a model nobody picked
                    elif missed:
                        fields["last_error"] = f"missed fire at {to_db(routine.next_fire)} skipped"
                    set_run_state(conn, routine_id, **fields)
                if missed or not routine.model:
                    report.skipped.append(name)
                    continue
                self.runs.enqueue(RoutineRequest(routine=routine_id, reason="cron"))
                report.fired.append(name)
            except Exception as exc:
                self._error(report, routine_id, name, exc)

    def _retry_failed(self, now: datetime, report: SchedulerReport) -> None:
        with self.ledger.locked() as conn:
            candidates = failed_retry_candidates(conn, now=now)
        for run in candidates:
            if run.routine_id is None or run.attempt >= MAX_ATTEMPTS:
                continue
            name = run.routine_id
            try:
                with self.ledger.locked() as conn:
                    routine = get_routine(conn, run.routine_id)
                if routine is not None:
                    name = routine.name
                    if not routine.enabled:
                        self._abandon(run.id, "routine disabled")
                        continue
                try:
                    # The failed attempt's request again. A cron fire names no model, so its retry
                    # resolves the routine's current stored runner and model, not the failed run's;
                    # a model named in the request (a manual run) is kept.
                    request = RoutineRequest.model_validate(
                        {**run.props.get("request", {}), "routine": run.routine_id,
                         "attempt": run.attempt + 1, "reason": "retry"}
                    )
                    retry = self.runs.enqueue(request)
                except RunsPaused:
                    raise  # paused: try again on a later tick
                except Exception as exc:
                    # deleted routine (NotFound), removed project (UnknownProject), invalid request:
                    # retrying every tick would never succeed
                    self._abandon(run.id, f"{type(exc).__name__}: {exc}"[:200])
                    raise
                with self.ledger.locked() as conn:
                    current = get_run(conn, run.id) or run
                    update_run(conn, run.id, props={**current.props, "retried": retry.id})
                report.retried.append(name)
            except Exception as exc:
                self._error(report, run.routine_id, name, exc)

    def _abandon(self, run_id: str, note: str) -> None:
        """Take a failed run off the retry list for good, saying why in `props.retry_note`."""
        with self.ledger.locked() as conn:
            current = get_run(conn, run_id)
            if current is not None:
                update_run(conn, run_id, props={**current.props, "retried": "abandoned", "retry_note": note})

    def _fire_session_end(self, now: datetime, report: SchedulerReport) -> None:
        with self.ledger.locked() as conn:
            refs = session_end_routine_refs(conn)
        for routine_id, name in refs:
            key = f"scheduler:session_end:{routine_id}"
            try:
                with self.ledger.locked() as conn:
                    routine = get_routine(conn, routine_id)
                    if routine is None or (routine.paused_until is not None and routine.paused_until > now):
                        continue
                    if not routine.model:
                        record_error(conn, routine_id, NO_MODEL_CHOSEN)
                        report.skipped.append(name)
                        continue
                    handled: list[str] = list(get_state(conn, key) or [])
                    pending = _ended_in_scope(conn, routine, now=now, handled=set(handled))
                for session_id, slug in pending:
                    self.runs.enqueue(RoutineRequest(routine=routine_id, projects=[slug], reason="session_end"))
                    handled.append(session_id)
                    with self.ledger.locked() as conn:
                        set_state(conn, key, handled[-HANDLED_CAP:])
                    report.fired.append(name)
            except Exception as exc:
                self._error(report, routine_id, name, exc)

    # -- loop ------------------------------------------------------------------------------

    async def loop(self, stop: asyncio.Event, *, interval_s: float) -> None:
        """Tick now, then every `interval_s` seconds until `stop` is set."""
        while not stop.is_set():
            try:
                await asyncio.to_thread(self.tick)
            except Exception:
                log.exception("scheduler tick failed")
            try:
                await asyncio.wait_for(stop.wait(), interval_s)
            except TimeoutError:
                pass


def _ended_in_scope(
    conn: apsw.Connection, routine: Routine, *, now: datetime, handled: set[str]
) -> list[tuple[str, str]]:
    """`(session id, project slug)` of each ended, debounced, not yet handled session in scope.

    The window is the last day, not "since the routine last fired": a session that ended just
    before a run started (inside the debounce) would otherwise never be seen; `handled` dedupes.
    """
    debounce = timedelta(seconds=float(routine.props.get("debounce_s", DEFAULT_DEBOUNCE_S)))
    if routine.scope.projects is None:
        in_scope = {p.id: p.slug for p in list_projects(conn, status="active")}
    else:
        in_scope = {p.id: p.slug for p in (resolve_project_ref(conn, ref) for ref in routine.scope.projects)}
    return [
        (s.id, in_scope[s.project_id])
        for s in ended_sessions_since(conn, since=now - SESSION_LOOKBACK)
        if s.id not in handled
        and s.project_id in in_scope
        and s.ended_at is not None
        and s.ended_at <= now - debounce
    ]
