from __future__ import annotations

from datetime import timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import AwareDatetime, BaseModel, Field

from trajectree.api.deps import get_ledger, get_settings
from trajectree.auth.scopes import Scope
from trajectree.auth.tokens import list_tokens, mint_token, revoke_token
from trajectree.config import Settings
from trajectree.db.ledger import Ledger
from trajectree.models import Actor, SessionRow
from trajectree.repo.sessions import running_sessions
from trajectree.timeutil import utcnow

router = APIRouter()


class TokenCreate(BaseModel):
    label: str = Field(min_length=1, max_length=64)
    scope: Scope
    ttl_s: int | None = Field(None, ge=60)
    actor_kind: Literal["agent", "session"] = "session"


class TokenOut(BaseModel):
    id: str
    label: str
    scope: Scope
    expires_at: AwareDatetime | None
    created_at: AwareDatetime


class TokenSecret(TokenOut):
    secret: str


@router.post("/tokens", response_model=TokenSecret)
def create_token(body: TokenCreate, ledger: Ledger = Depends(get_ledger)):
    ttl = timedelta(seconds=body.ttl_s) if body.ttl_s is not None else None
    actor = Actor(kind=body.actor_kind, ref=body.label, tool="token")
    with ledger.locked() as conn:
        token_id, secret = mint_token(conn, label=body.label, scope=body.scope, actor=actor, ttl=ttl)
        row = next(t for t in list_tokens(conn) if t["id"] == token_id)
    return TokenSecret(**row, secret=secret)


@router.get("/tokens", response_model=list[TokenOut])
def get_tokens(ledger: Ledger = Depends(get_ledger)):
    with ledger.locked() as conn:
        rows = list_tokens(conn)
    return [TokenOut(**r) for r in rows]


@router.delete("/tokens/{token_id}", status_code=204)
def delete_token(token_id: str, ledger: Ledger = Depends(get_ledger)) -> Response:
    with ledger.locked() as conn:
        removed = revoke_token(conn, token_id)
    if not removed:
        raise HTTPException(status_code=404, detail="Not Found")
    return Response(status_code=204)


@router.get("/sessions", response_model=list[SessionRow])
def get_sessions(ledger: Ledger = Depends(get_ledger), settings: Settings = Depends(get_settings)):
    since = utcnow() - timedelta(seconds=settings.live_window_s)
    with ledger.locked() as conn:
        return running_sessions(conn, since=since)
