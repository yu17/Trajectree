from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, Query
from pydantic import AwareDatetime

from trajectree.api.deps import get_ledger
from trajectree.api.routes_entities import _csv
from trajectree.context.pack import ContextPack, build_context_pack
from trajectree.db.ledger import Ledger
from trajectree.repo import NotFound, projects
from trajectree.timeutil import utcnow

router = APIRouter()

WINDOW_BEFORE = timedelta(days=90)
WINDOW_AFTER = timedelta(days=30)


@router.get("/context", response_model=ContextPack)
def context(
    project_filter: str | None = Query(default=None, alias="projects"),
    t_from: AwareDatetime | None = Query(default=None, alias="from"),
    t_to: AwareDatetime | None = Query(default=None, alias="to"),
    budget: int = Query(4000, ge=200, le=100000),
    q: str | None = None,
    ledger: Ledger = Depends(get_ledger),
):
    now = utcnow()
    t_from = t_from or now - WINDOW_BEFORE
    t_to = t_to or now + WINDOW_AFTER
    names = _csv(project_filter)
    with ledger.locked() as conn:
        if names is None:
            ids = [p.id for p in projects.list_projects(conn, status="active")]
        else:
            ids = []
            for name in names:
                found = projects.get_project_by_slug(conn, name) or projects.get_project(conn, name)
                if found is None:
                    raise NotFound(f"unknown project '{name}'")
                ids.append(found.id)
        return build_context_pack(
            conn, project_ids=ids, t_from=t_from, t_to=t_to, token_budget=budget, question=q
        )
