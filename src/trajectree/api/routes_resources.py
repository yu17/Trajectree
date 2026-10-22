from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from trajectree.api.deps import actor_from_request, get_ledger
from trajectree.api.routes_entities import _csv, _strip
from trajectree.db.ledger import Ledger
from trajectree.models import Resource, ResourceIn
from trajectree.repo import resources

router = APIRouter()


class ResourceCreate(ResourceIn):
    intent: str | None = None


@router.get("/resources", response_model=list[Resource])
def list_resources(
    projects: str | None = None,
    kinds: str | None = None,
    ledger: Ledger = Depends(get_ledger),
):
    with ledger.locked() as conn:
        return resources.list_resources(conn, project_ids=_csv(projects), kinds=_csv(kinds))


@router.post("/resources", response_model=Resource)
def upsert_resource(body: ResourceCreate, request: Request, ledger: Ledger = Depends(get_ledger)):
    return resources.upsert_resource(
        ledger, actor_from_request(request), _strip(body, ResourceIn), intent=body.intent
    )
