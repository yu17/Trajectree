from __future__ import annotations

import secrets

from collections.abc import Callable

from fastapi import HTTPException, Request

from trajectree.auth.scopes import Principal, owner_principal
from trajectree.auth.tokens import resolve_token
from trajectree.config import Settings
from trajectree.db.ledger import Ledger
from trajectree.models import Actor

COOKIE_NAME = "trajectree_token"


def token_matches(candidate: str | None, expected: str) -> bool:
    if not candidate:
        return False
    return secrets.compare_digest(candidate.encode(), expected.encode())


def require_token(request: Request) -> None:
    expected: str = request.app.state.token
    auth = request.headers.get("Authorization", "")
    scheme, _, value = auth.partition(" ")
    if scheme.lower() == "bearer" and token_matches(value.strip(), expected):
        return
    if token_matches(request.cookies.get(COOKIE_NAME), expected):
        return
    raise HTTPException(status_code=401, detail="Unauthorized")


def scoped_or_owner(tool: str) -> Callable[[Request], Principal]:
    """Dependency factory: the owner's bearer token, or a live scoped token allowing `tool`. No cookie auth."""

    def dependency(request: Request) -> Principal:
        scheme, _, value = request.headers.get("Authorization", "").partition(" ")
        secret = value.strip()
        if scheme.lower() == "bearer" and secret:
            if token_matches(secret, request.app.state.token):
                return owner_principal()
            with request.app.state.ledger.locked() as conn:
                principal = resolve_token(conn, secret)
            if principal is not None and principal.scope.allows(tool, None, write=False):
                return principal
        raise HTTPException(status_code=401, detail="Unauthorized")

    return dependency


def get_ledger(request: Request) -> Ledger:
    return request.app.state.ledger


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def actor_from_request(request: Request) -> Actor:
    return Actor(
        kind="human",
        ref="owner",
        tool=request.headers.get("X-Trajectree-Client", "web"),
    )
