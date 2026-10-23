from datetime import timedelta
from trajectree.auth.scopes import Scope
from trajectree.auth.tokens import mint_token
from trajectree.models import Actor
from tests.test_api_entities import mk_project

HDR = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}}
LIST = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}

def call(name, args, id=3):
    return {"jsonrpc": "2.0", "id": id, "method": "tools/call", "params": {"name": name, "arguments": args}}

def test_mcp_requires_bearer(anon, app):
    assert anon.post("/mcp", json=INIT, headers=HDR).status_code == 401
    anon.cookies.set("trajectree_token", app.state.token)
    assert anon.post("/mcp", json=INIT, headers=HDR).status_code == 401

def test_mcp_initialize_and_tools_list(client):
    r = client.post("/mcp", json=INIT, headers=HDR)
    assert r.status_code == 200 and r.json()["result"]["serverInfo"]["name"] == "trajectree"
    names = {t["name"] for t in client.post("/mcp", json=LIST, headers=HDR).json()["result"]["tools"]}
    assert {"record_event", "get_context_pack", "unlink"} <= names

def test_mcp_scoped_token_accepted_and_attributed(client, app):
    p = mk_project(client)
    _, secret = mint_token(app.state.ledger.conn, label="run", scope=Scope(projects=[p["id"]], tools=["record_event"]),
                           actor=Actor(kind="agent", ref="run_9", model="m", backend="claude", tool="mcp"), ttl=timedelta(hours=1))
    h = {**HDR, "Authorization": f"Bearer {secret}"}
    r = client.post("/mcp", json=call("record_event", {"project": p["slug"], "title": "T", "intent": "i"}), headers=h)
    assert r.status_code == 200 and not r.json()["result"].get("isError")
    pending = client.get("/api/ledger/txns", params={"review": "pending"}).json()
    assert pending[0]["actor_kind"] == "agent" and pending[0]["actor_ref"] == "run_9"
    assert client.post("/mcp", json=INIT, headers={**HDR, "Authorization": "Bearer trj_bogus"}).status_code == 401

def test_mcp_owner_write_over_http_uses_cwd_header(client, app):
    p = mk_project(client)
    client.post("/api/resources", json={"project_id": p["id"], "kind": "folder", "locator": "/home/u/proj"})
    r = client.post("/mcp", json=call("add_note", {"text": "hi", "intent": "i"}), headers={**HDR, "X-Trajectree-Cwd": "/home/u/proj/src"})
    assert r.json()["result"]["structuredContent"]["project_id"] == p["id"]
    assert client.get("/api/ledger/txns", params={"review": "pending"}).json()[0]["actor_kind"] == "session"

def test_spa_fallback_does_not_shadow_mcp(client):
    assert client.get("/mcp/anything").status_code in (404, 405)


def test_owner_mcp_write_carries_session_id_from_header_or_cwd(client):
    p = mk_project(client)
    client.post("/api/resources", json={"project_id": p["id"], "kind": "folder", "locator": "/home/u/proj"})
    client.post("/mcp", json=call("add_note", {"project": p["slug"], "text": "a", "intent": "i"}), headers={**HDR, "X-Trajectree-Session": "sess-9"})
    t = client.get("/api/ledger/txns", params={"review": "pending"}).json()[0]
    assert t["session_id"] == "sess-9" and t["actor_ref"] == "sess-9" and t["actor_kind"] == "session"
    client.post("/api/hooks/claude", json={"session_id": "live-1", "cwd": "/home/u/proj", "hook_event_name": "SessionStart", "transcript_path": "/t"})
    client.post("/mcp", json=call("add_note", {"text": "b", "intent": "i"}), headers={**HDR, "X-Trajectree-Cwd": "/home/u/proj"})
    t = client.get("/api/ledger/txns", params={"review": "pending"}).json()[0]
    assert t["session_id"] == "live-1" and t["actor_ref"] == "live-1"
