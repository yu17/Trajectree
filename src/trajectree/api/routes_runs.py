from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from trajectree.api.deps import actor_from_request, get_ledger
from trajectree.db.ledger import Ledger
from trajectree.models import Caps, Run, RunKind, RunStatus, UsageDay, UsageSlice
from trajectree.repo import NotFound
from trajectree.repo.caps import get_caps, set_caps
from trajectree.repo.routines import resolve_routine_ref, routine_names
from trajectree.repo.runs import get_run, list_runs, spent_usd, txn_ids_for_run, usage_by, usage_days
from trajectree.runner.credentials import RunnerStatus, runner_status, store_oauth_token, sync_backends
from trajectree.runner.kinds import (
    AskRequest,
    QuickAddRequest,
    RunRequest,
    SummarizeRequest,
    SummarizeSessionRequest,
)
from trajectree.runner.service import RunService

router = APIRouter()


class RunDetail(BaseModel):
    run: Run
    txn_ids: list[str]


class UsageReport(BaseModel):
    days: list[UsageDay]
    by_kind: list[UsageSlice]
    by_runner: list[UsageSlice]
    by_routine: list[UsageSlice]  # keyed by routine name, `-` for runs of no routine
    spent_today: float
    caps: Caps


class CredentialsBody(BaseModel):
    backend: Literal["claude"] = "claude"
    oauth_token: str


class SummarizeBody(BaseModel):
    runner: str | None = None
    model: str | None = None


def _enqueue(request: Request, req: RunRequest) -> Run:
    runs: RunService = request.app.state.runs
    try:
        return runs.enqueue(req)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/projects/{project_id}/summarize", response_model=Run, status_code=202)
def summarize(project_id: str, request: Request, body: SummarizeBody | None = None):
    body = body or SummarizeBody()
    return _enqueue(request, SummarizeRequest(project=project_id, runner=body.runner, model=body.model))


@router.post("/sessions/{session_id}/summarize", response_model=Run, status_code=202)
def summarize_session(session_id: str, request: Request, body: SummarizeBody | None = None):
    body = body or SummarizeBody()
    return _enqueue(request, SummarizeSessionRequest(session_id=session_id, runner=body.runner, model=body.model))


@router.post("/ask", response_model=Run, status_code=202)
def ask(body: AskRequest, request: Request):
    return _enqueue(request, body)


@router.post("/quick-add", response_model=Run, status_code=202)
def quick_add(body: QuickAddRequest, request: Request):
    return _enqueue(request, body)


@router.get("/runs", response_model=list[Run])
def runs_list(
    limit: int = Query(50, ge=1, le=500),
    kind: RunKind | None = None,
    status: RunStatus | None = None,
    open_proposals: bool = False,
    routine: str | None = None,
    ledger: Ledger = Depends(get_ledger),
):
    with ledger.locked() as conn:
        routine_id = resolve_routine_ref(conn, routine).id if routine else None
        return list_runs(
            conn, limit=limit, kind=kind, status=status, with_open_proposals=open_proposals, routine_id=routine_id
        )


@router.get("/runs/{run_id}", response_model=RunDetail)
def run_detail(run_id: str, ledger: Ledger = Depends(get_ledger)):
    with ledger.locked() as conn:
        run = get_run(conn, run_id)
        if run is None:
            raise NotFound(f"run {run_id} not found")
        return RunDetail(run=run, txn_ids=txn_ids_for_run(conn, run_id))


@router.post("/runs/{run_id}/proposals/{index}/accept", response_model=Run)
def accept_proposal(run_id: str, index: int, request: Request):
    runs: RunService = request.app.state.runs
    try:
        return runs.apply_proposal(run_id, index, actor=actor_from_request(request))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/runs/{run_id}/proposals/{index}/dismiss", response_model=Run)
def dismiss_proposal(run_id: str, index: int, request: Request):
    runs: RunService = request.app.state.runs
    return runs.dismiss_proposal(run_id, index)


@router.get("/usage", response_model=UsageReport)
def usage(days: int = Query(30, ge=1, le=365), ledger: Ledger = Depends(get_ledger)):
    now = datetime.now(timezone.utc)
    with ledger.locked() as conn:
        by_day = usage_days(conn, days=days, now=now)
        since = datetime.strptime(by_day[0].day, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        names = routine_names(conn)
        return UsageReport(
            days=by_day,
            by_kind=usage_by(conn, field="kind", since=since),
            by_runner=usage_by(conn, field="runner", since=since),
            by_routine=[
                s.model_copy(update={"key": names.get(s.key, s.key)})
                for s in usage_by(conn, field="routine_id", since=since)
            ],
            spent_today=spent_usd(conn, day=now.strftime("%Y-%m-%d")),
            caps=get_caps(conn),
        )


@router.get("/caps", response_model=Caps)
def caps_get(ledger: Ledger = Depends(get_ledger)):
    with ledger.locked() as conn:
        return get_caps(conn)


@router.put("/caps", response_model=Caps)
def caps_put(body: Caps, ledger: Ledger = Depends(get_ledger)):
    with ledger.locked() as conn:
        set_caps(conn, body)
        return get_caps(conn)


@router.get("/runner/status", response_model=RunnerStatus)
def runner_status_get(request: Request):
    return runner_status(request.app.state.settings)


@router.post("/runner/credentials")
def runner_credentials(body: CredentialsBody, request: Request) -> dict[str, bool]:
    try:
        store_oauth_token(request.app.state.settings, body.oauth_token)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"stored": True}


@router.post("/backends/sync")
def backends_sync(request: Request) -> dict[str, list[str]]:
    return {"synced": sync_backends(request.app.state.settings)}
