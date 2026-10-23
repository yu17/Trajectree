from __future__ import annotations

import secrets

from fastapi import HTTPException, Request

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
