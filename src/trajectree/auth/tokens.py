"""Short-lived scoped tokens, stored only as SHA-256 hashes."""

import hashlib
import json
import secrets
from datetime import datetime, timedelta

from trajectree.auth.scopes import Principal, Scope
from trajectree.ids import new_id
from trajectree.models import Actor
from trajectree.timeutil import from_db, to_db, utcnow


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def mint_token(
    conn, *, label: str, scope: Scope, actor: Actor, ttl: timedelta | None
) -> tuple[str, str]:
    """Create a token; return (token_id, secret). The secret is never stored."""
    token_id = new_id()
    secret = "trj_" + secrets.token_urlsafe(32)
    now = utcnow()
    expires_at = to_db(now + ttl) if ttl is not None else None
    conn.execute(
        "INSERT INTO token (id, hash, label, scope, actor, expires_at, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (token_id, _hash(secret), label, scope.model_dump_json(), actor.model_dump_json(),
         expires_at, to_db(now)),
    )
    return token_id, secret


def resolve_token(conn, secret: str, *, now: datetime | None = None) -> Principal | None:
    """Return the Principal for a live token, or None if unknown, expired or revoked."""
    row = conn.execute(
        "SELECT id, scope, actor, expires_at FROM token WHERE hash = ?", (_hash(secret),)
    ).fetchone()
    if row is None:
        return None
    token_id, scope, actor, expires_at = row
    if expires_at is not None and (now or utcnow()) >= from_db(expires_at):
        return None
    return Principal(
        actor=Actor.model_validate_json(actor),
        scope=Scope.model_validate_json(scope),
        token_id=token_id,
    )


def list_tokens(conn) -> list[dict]:
    """Every token row, expired ones included, newest first; the secret and hash are never returned."""
    rows = conn.execute(
        "SELECT id, label, scope, actor, expires_at, created_at FROM token ORDER BY created_at DESC, id"
    ).fetchall()
    return [
        {
            "id": i, "label": label, "scope": json.loads(scope), "actor_kind": json.loads(actor)["kind"],
            "expires_at": exp, "created_at": created,
        }
        for i, label, scope, actor, exp, created in rows
    ]


def revoke_token(conn, token_id: str) -> bool:
    """Delete a token; False when no such token existed."""
    conn.execute("DELETE FROM token WHERE id = ?", (token_id,))
    return conn.changes() > 0
