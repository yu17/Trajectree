from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from pydantic import AwareDatetime

from trajectree.api.deps import get_ledger
from trajectree.api.routes_entities import _csv
from trajectree.db.ledger import Ledger
from trajectree.ingest.job import IngestReport, run_ingest
from trajectree.models import Signal
from trajectree.repo.signals import list_signals
from trajectree.repo.state import get_state

router = APIRouter()


@router.get("/signals", response_model=list[Signal])
def signals(
    project_filter: str | None = Query(default=None, alias="projects"),
    kind_filter: str | None = Query(default=None, alias="kinds"),
    t_from: AwareDatetime | None = Query(default=None, alias="from"),
    t_to: AwareDatetime | None = Query(default=None, alias="to"),
    limit: int = Query(200, ge=1, le=2000),
    ledger: Ledger = Depends(get_ledger),
):
    with ledger.locked() as conn:
        return list_signals(
            conn,
            project_ids=_csv(project_filter),
            kinds=_csv(kind_filter),
            t_from=t_from,
            t_to=t_to,
            limit=limit,
        )


@router.post("/ingest/run", response_model=IngestReport)
def ingest_run(request: Request, ledger: Ledger = Depends(get_ledger)):
    return run_ingest(ledger, request.app.state.settings)


@router.get("/ingest/status", response_model=IngestReport | None)
def ingest_status(ledger: Ledger = Depends(get_ledger)):
    with ledger.locked() as conn:
        return get_state(conn, "ingest:last")
