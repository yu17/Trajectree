"""Routines and the scheduler: CRUD, manual runs, run history, scheduler status and a manual tick."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from trajectree.api.deps import get_ledger
from trajectree.db.ledger import Ledger
from trajectree.models import Routine, RoutineIn, RoutinePatch, Run
from trajectree.repo.routines import create_routine, list_routines, resolve_routine_ref, update_routine
from trajectree.repo.runs import list_runs
from trajectree.runner.kinds import RoutineRequest
from trajectree.runner.service import RunService
from trajectree.scheduler import Scheduler, SchedulerReport, SchedulerStatus
from trajectree.timeutil import utcnow

router = APIRouter()


class RunRoutineBody(BaseModel):
    projects: list[str] | None = None


@router.get("/routines", response_model=list[Routine])
def routines_list(ledger: Ledger = Depends(get_ledger)):
    with ledger.locked() as conn:
        return list_routines(conn)


@router.post("/routines", response_model=Routine, status_code=201)
def routines_create(body: RoutineIn, ledger: Ledger = Depends(get_ledger)):
    with ledger.locked() as conn:
        return create_routine(conn, body, now=utcnow())


@router.get("/routines/{ref}", response_model=Routine)
def routines_get(ref: str, ledger: Ledger = Depends(get_ledger)):
    with ledger.locked() as conn:
        return resolve_routine_ref(conn, ref)


@router.patch("/routines/{ref}", response_model=Routine)
def routines_patch(ref: str, body: RoutinePatch, ledger: Ledger = Depends(get_ledger)):
    with ledger.locked() as conn:
        routine = resolve_routine_ref(conn, ref)
        return update_routine(conn, routine.id, body, now=utcnow())


@router.post("/routines/{ref}/run", response_model=Run, status_code=202)
def routines_run(ref: str, request: Request, body: RunRoutineBody | None = None):
    runs: RunService = request.app.state.runs
    body = body or RunRoutineBody()
    try:
        return runs.enqueue(RoutineRequest(routine=ref, projects=body.projects))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/routines/{ref}/runs", response_model=list[Run])
def routines_runs(ref: str, limit: int = Query(20, ge=1, le=500), ledger: Ledger = Depends(get_ledger)):
    with ledger.locked() as conn:
        routine = resolve_routine_ref(conn, ref)
        return list_runs(conn, routine_id=routine.id, limit=limit)


@router.get("/scheduler", response_model=SchedulerStatus)
def scheduler_status(request: Request, ledger: Ledger = Depends(get_ledger)):
    scheduler: Scheduler = request.app.state.scheduler
    with ledger.locked() as conn:
        return scheduler.status(conn)


@router.post("/scheduler/tick", response_model=SchedulerReport)
def scheduler_tick(request: Request):
    scheduler: Scheduler = request.app.state.scheduler
    return scheduler.tick()
