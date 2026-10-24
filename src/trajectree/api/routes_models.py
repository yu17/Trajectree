from __future__ import annotations

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from trajectree.runner.catalog import Catalog, edit_models, load_catalog, refresh, store_catalog

router = APIRouter()


def refresh_catalog(app) -> Catalog:
    """One refresh with its own HTTP client; tests set `app.state.model_transport` (no real network)."""
    with httpx.Client(transport=getattr(app.state, "model_transport", None)) as http:
        return refresh(app.state.ledger, app.state.settings, http)


@router.get("/models", response_model=Catalog)
def models(request: Request) -> Catalog:
    with request.app.state.ledger.locked() as conn:
        return load_catalog(conn)


@router.post("/models/refresh", response_model=Catalog)
def models_refresh(request: Request) -> Catalog:
    return refresh_catalog(request.app)


class EditBody(BaseModel):
    runner: str
    add: list[str] = []
    delete: list[str] = []


@router.post("/models/edit", response_model=Catalog)
def models_edit(body: EditBody, request: Request) -> Catalog:
    add = [m.strip() for m in body.add]
    delete = [m.strip() for m in body.delete]
    if not (add or delete) or not all(add + delete):
        raise HTTPException(status_code=422, detail="name at least one model to add or delete; names must not be blank")
    with request.app.state.ledger.locked() as conn:
        try:
            catalog = edit_models(load_catalog(conn), body.runner, add, delete)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"unknown runner {body.runner}") from None
        store_catalog(conn, catalog)
    return catalog
