"""Routine-specific steps of a run: the per-routine daily cap, retry backoff, result delivery and
routine state. Helpers for `RunService`; `routine_cap` and `after_routine` are called with the
ledger lock held, `finish_routine_run` with it released.
"""

from __future__ import annotations

import logging
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import apsw

from trajectree.config import Settings
from trajectree.db.ledger import Ledger
from trajectree.delivery import DeliveryMessage, DeliveryResult, deliver
from trajectree.models import Actor, Run
from trajectree.repo.routines import get_routine, set_run_state
from trajectree.repo.runs import get_run, spent_usd, update_run
from trajectree.runner.kinds import RunPlan
from trajectree.timeutil import to_db

log = logging.getLogger("trajectree.runner")

# Infrastructure failures of routine runs are retried after these delays, up to MAX_ATTEMPTS runs.
RETRY_BACKOFF_S = (60, 300, 900)
MAX_ATTEMPTS = 3


def next_utc_midnight(now: datetime) -> datetime:
    day = now.astimezone(timezone.utc).date() + timedelta(days=1)
    return datetime.combine(day, time(0), tzinfo=timezone.utc)


def routine_cap(conn: apsw.Connection, run: Run, *, today: str, now: datetime) -> str | None:
    """The cap error when the run's routine has spent its daily budget (pausing it until the next
    UTC midnight), else None. A missing routine is left for planning to report."""
    routine = get_routine(conn, run.routine_id) if run.routine_id else None
    if routine is None:
        return None
    caps = routine.caps
    if spent_usd(conn, day=today, routine_id=routine.id) + caps.usd_per_run <= caps.usd_per_day:
        return None
    set_run_state(conn, routine.id, paused_until=next_utc_midnight(now))
    return f"routine daily cap {caps.usd_per_day} USD reached"


def retry_props(run: Run, *, now: datetime) -> dict:
    """`props` additions for an infrastructure failure: when to retry a routine run, if ever."""
    if run.kind != "routine" or run.attempt >= MAX_ATTEMPTS:
        return {}
    return {"retry_at": to_db(now + timedelta(seconds=RETRY_BACKOFF_S[run.attempt - 1]))}


def deliver_result(
    ledger: Ledger, settings: Settings, run: Run, plan: RunPlan, *, actor: Actor, now: datetime
) -> DeliveryResult:
    day = now.astimezone(ZoneInfo(plan.routine_tz or "UTC")).strftime("%Y-%m-%d")
    message = DeliveryMessage(
        title=f"{plan.routine_name} · {day}",
        body_md=run.result_md,
        project_id=plan.delivery_project_id,
        run_id=run.id,
        routine=plan.routine_name or "",
    )
    try:
        return deliver(ledger, settings, plan.delivery, message, actor=actor, now=now)
    except Exception as exc:  # a failed delivery never fails the run
        log.warning("run %s delivery failed: %s", run.id, type(exc).__name__)
        return DeliveryResult(channel=plan.delivery, ok=False, detail=f"delivery failed: {type(exc).__name__}")


def after_routine(conn: apsw.Connection, run: Run) -> None:
    """Record a finished routine run on its routine (`last_run_id`, `last_error`, `last_fired`)."""
    if run.routine_id is None or get_routine(conn, run.routine_id) is None:
        return
    fields: dict = {"last_run_id": run.id, "last_error": run.error}
    if run.started is not None:
        fields["last_fired"] = run.started
    set_run_state(conn, run.routine_id, **fields)


def finish_routine_run(
    ledger: Ledger, settings: Settings, run: Run, plan: RunPlan, *, actor: Actor, now: datetime
) -> Run:
    """Deliver an ok routine run's result, then record the run on its routine.

    Called without the ledger lock: delivery may be a network call, and an inbox delivery takes
    the lock itself for its write.
    """
    delivery = (
        deliver_result(ledger, settings, run, plan, actor=actor, now=now)
        if run.status == "ok" and run.result_md
        else None
    )
    with ledger.locked() as conn:
        if delivery is not None:
            current = get_run(conn, run.id) or run
            run = update_run(conn, run.id, props={**current.props, "delivery": delivery.model_dump(mode="json")})
        after_routine(conn, run)
    return run
