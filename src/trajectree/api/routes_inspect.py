from __future__ import annotations

from datetime import date
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from trajectree.api.deps import get_ledger
from trajectree.db.ledger import Ledger
from trajectree.models import InspectionDue, Run
from trajectree.repo.inspections import due_projects
from trajectree.repo.projects import list_projects, resolve_project_ref
from trajectree.runner.kinds import InspectRequest
from trajectree.runner.service import RunService
from trajectree.runner.spec import InspectionActive, NothingToInspect
from trajectree.timeutil import utcnow

# create_app mounts every protected router under /api with the token check.
router = APIRouter()


class InspectBody(BaseModel):
    runner: str | None = None
    model: str | None = None
    since: date | None = None


class InspectAllBody(BaseModel):
    projects: list[str] | Literal["all"]
    runner: str | None = None
    model: str | None = None


class InspectAllResult(BaseModel):
    runs: list[Run]
    skipped: list[str]  # slugs with nothing to inspect, or an inspection already running


@router.get("/inspections", response_model=list[InspectionDue])
def inspections_due(request: Request, ledger: Ledger = Depends(get_ledger)):
    with ledger.locked() as conn:
        return due_projects(conn, request.app.state.settings, now=utcnow())


def _inspect(request: Request, req: InspectRequest) -> Run:
    runs: RunService = request.app.state.runs
    try:
        return runs.inspect(req)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/projects/{project_id}/inspect", response_model=Run, status_code=202)
def inspect_project(project_id: str, request: Request, body: InspectBody | None = None):
    body = body or InspectBody()
    return _inspect(
        request, InspectRequest(project=project_id, runner=body.runner, model=body.model, since=body.since)
    )


@router.post("/inspect", response_model=InspectAllResult)
def inspect_all(body: InspectAllBody, request: Request, ledger: Ledger = Depends(get_ledger)):
    with ledger.locked() as conn:
        if body.projects == "all":
            refs = [p.id for p in list_projects(conn, status="active")]
        else:
            # An unknown project refuses the whole request before any run is queued.
            refs = [resolve_project_ref(conn, ref).id for ref in body.projects]
    runs: list[Run] = []
    skipped: list[str] = []
    for ref in refs:
        try:
            runs.append(_inspect(request, InspectRequest(project=ref, runner=body.runner, model=body.model)))
        except (NothingToInspect, InspectionActive) as exc:
            skipped.append(exc.slug)
    return InspectAllResult(runs=runs, skipped=skipped)
