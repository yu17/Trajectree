"""Routine-specific steps of a run: the per-routine daily cap, result delivery and routine state.

Helpers for `RunService`; each is called with the ledger lock held.
"""

from __future__ import annotations

import logging
from datetime import datetime, time, timedelta, timezone

import apsw

from trajectree.config import Settings
from trajectree.db.ledger import Ledger
from trajectree.delivery import DeliveryMessage, DeliveryResult, deliver
from trajectree.models import Actor, Run
from trajectree.repo.routines import get_routine, set_run_state
from trajectree.repo.runs import spent_usd
from trajectree.runner.kinds import RunPlan

log = logging.getLogger("trajectree.runner")


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


def deliver_result(
    ledger: Ledger, settings: Settings, run: Run, plan: RunPlan, *, actor: Actor, now: datetime
) -> DeliveryResult:
    message = DeliveryMessage(
        title=f"{plan.routine_name} · {now.astimezone(timezone.utc):%Y-%m-%d}",
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
