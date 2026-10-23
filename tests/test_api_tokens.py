from datetime import timedelta
from trajectree.auth.tokens import mint_token, resolve_token
from trajectree.models import Actor
from trajectree.auth.scopes import Scope

def test_token_create_list_delete(client, app):
    r = client.post("/api/tokens", json={"label": "hooks", "scope": {"projects": [], "tools": ["hooks"], "write": False}})
    assert r.status_code == 200 and r.json()["secret"].startswith("trj_") and r.json()["expires_at"] is None
    listed = client.get("/api/tokens").json()
    assert listed[0]["label"] == "hooks" and "secret" not in listed[0] and listed[0]["scope"]["tools"] == ["hooks"]
    assert client.delete(f"/api/tokens/{r.json()['id']}").status_code == 204
    assert client.get("/api/tokens").json() == [] and client.delete(f"/api/tokens/{r.json()['id']}").status_code == 404
    assert client.post("/api/tokens", json={"label": "", "scope": {}}).status_code == 422

def test_token_actor_kind_connector(client, app):
    body = {"label": "chatmaid", "scope": {"projects": [], "tools": ["chatmaid"], "write": False}}
    r = client.post("/api/tokens", json={**body, "actor_kind": "connector"})
    assert r.status_code == 200
    principal = resolve_token(app.state.ledger.conn, r.json()["secret"])
    assert principal.actor.kind == "connector" and principal.actor.ref == "chatmaid"
    assert client.post("/api/tokens", json={**body, "actor_kind": "human"}).status_code == 422

def test_scope_allows_mcp():
    assert Scope().allows_mcp() and Scope(tools=["record_event"]).allows_mcp()
    assert not Scope(tools=["hooks"]).allows_mcp() and not Scope(tools=[]).allows_mcp()
    assert not Scope(tools=["chatmaid"]).allows_mcp() and not Scope(tools=["hooks", "chatmaid"]).allows_mcp()

def test_expired_token_is_not_listed_as_live(client, app):
    _, secret = mint_token(app.state.ledger.conn, label="old", scope=Scope(), actor=Actor(kind="agent", ref="x"), ttl=timedelta(seconds=-1))
    assert resolve_token(app.state.ledger.conn, secret) is None
    assert client.get("/api/tokens").json()[0]["label"] == "old"
