from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from trajectree.runner.credentials import CONNECTOR_NAMES, store_connector_token

router = APIRouter()


class ConnectorCredentials(BaseModel):
    token: str = Field(min_length=1)


@router.post("/connectors/{name}/credentials")
def connector_credentials(name: str, body: ConnectorCredentials, request: Request) -> dict[str, bool]:
    if name not in CONNECTOR_NAMES:
        raise HTTPException(status_code=404, detail=f"unknown connector {name!r}")
    try:
        store_connector_token(request.app.state.settings, name, body.token)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"stored": True}
