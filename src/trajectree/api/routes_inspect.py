from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from trajectree.api.deps import get_ledger
from trajectree.db.ledger import Ledger
from trajectree.models import InspectionDue
from trajectree.repo.inspections import due_projects
from trajectree.timeutil import utcnow

# create_app mounts every protected router under /api with the token check.
router = APIRouter()


@router.get("/inspections", response_model=list[InspectionDue])
def inspections_due(request: Request, ledger: Ledger = Depends(get_ledger)):
    with ledger.locked() as conn:
        return due_projects(conn, request.app.state.settings, now=utcnow())
