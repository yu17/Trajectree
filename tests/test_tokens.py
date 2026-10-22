from datetime import datetime, timedelta, timezone
from trajectree.auth.scopes import Scope, owner_principal
from trajectree.auth.tokens import mint_token, resolve_token, revoke_token
from trajectree.models import Actor

UTC = timezone.utc
RUN = Actor(kind="agent", ref="run_1", model="claude-opus-5-5", backend="claude", tool="mcp")

def test_mint_and_resolve_round_trip(conn):
    scope = Scope(projects=["p1"], tools=["record_event"])
    tid, secret = mint_token(conn, label="run_1", scope=scope, actor=RUN, ttl=timedelta(hours=1))
    assert secret.startswith("trj_")
    principal = resolve_token(conn, secret)
    assert principal.actor == RUN and principal.scope == scope and principal.token_id == tid
    assert conn.execute("select count(*) from token where hash = ?", (secret,)).fetchone()[0] == 0

def test_unknown_and_revoked_tokens_resolve_none(conn):
    tid, secret = mint_token(conn, label="x", scope=Scope(), actor=RUN, ttl=None)
    assert resolve_token(conn, "trj_nope") is None
    revoke_token(conn, tid)
    assert resolve_token(conn, secret) is None

def test_expired_token_resolves_none(conn):
    _, secret = mint_token(conn, label="x", scope=Scope(), actor=RUN, ttl=timedelta(minutes=5))
    assert resolve_token(conn, secret, now=datetime.now(UTC) + timedelta(minutes=6)) is None
    assert resolve_token(conn, secret, now=datetime.now(UTC) + timedelta(minutes=4)) is not None

def test_scope_allows_matrix():
    full = Scope()
    assert full.allows("unlink", "p1", write=True) and full.allows("timeline", None, write=False)
    s = Scope(projects=["p1"], tools=["record_event", "timeline"])
    assert s.allows("record_event", "p1", write=True)
    assert not s.allows("record_event", "p2", write=True)
    assert not s.allows("add_todo", "p1", write=True)
    assert s.allows("timeline", None, write=False)
    assert not Scope(write=False).allows("record_event", "p1", write=True)

def test_owner_principal():
    p = owner_principal("sess-1")
    assert p.actor.kind == "session" and p.actor.ref == "sess-1" and p.token_id is None
    assert owner_principal().actor.ref == "owner"
