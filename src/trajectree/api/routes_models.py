from __future__ import annotations

import httpx
from fastapi import APIRouter, Request

from trajectree.runner.catalog import Catalog, load_catalog, refresh

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
