from tests.test_api_entities import mk_project
from trajectree.repo.state import set_state

def hook(event, sid="s1", cwd="/home/u/proj", **extra):
    return {"session_id": sid, "transcript_path": "/t.jsonl", "cwd": cwd, "hook_event_name": event, **extra}

def test_hook_lifecycle_updates_registry_and_timeline_live(client):
    p = mk_project(client)
    client.post("/api/resources", json={"project_id": p["id"], "kind": "folder", "locator": "/home/u/proj"})
    assert client.post("/api/hooks/claude", json=hook("SessionStart", source="startup")).json() == {}
    rows = client.get("/api/sessions").json()
    assert rows[0]["id"] == "s1" and rows[0]["project_id"] == p["id"] and rows[0]["source"] == "startup"
    tl = client.get("/api/timeline", params={"from": "2020-01-01T00:00:00Z", "to": "2030-01-01T00:00:00Z"}).json()
    assert [l["session_id"] for l in tl["live"]] == ["s1"]
    client.post("/api/hooks/claude", json=hook("Stop", stop_hook_active=False))
    assert client.get("/api/sessions").json()[0]["ended_at"] is None
    client.post("/api/hooks/claude", json=hook("SessionEnd", reason="other"))
    assert client.get("/api/sessions").json() == []
    tl = client.get("/api/timeline", params={"from": "2020-01-01T00:00:00Z", "to": "2030-01-01T00:00:00Z"}).json()
    assert tl["live"] == []

def test_hook_token_scope(client, anon):
    good = client.post("/api/tokens", json={"label": "hooks", "scope": {"projects": [], "tools": ["hooks"], "write": False}}).json()["secret"]
    bad = client.post("/api/tokens", json={"label": "run", "scope": {"tools": ["record_event"]}}).json()["secret"]
    assert anon.post("/api/hooks/claude", json=hook("SessionStart"), headers={"Authorization": f"Bearer {good}"}).status_code == 200
    assert anon.get("/api/projects", headers={"Authorization": f"Bearer {good}"}).status_code == 401
    assert anon.post("/api/hooks/claude", json=hook("SessionStart"), headers={"Authorization": f"Bearer {bad}"}).status_code == 401

def test_hook_unknown_cwd_missing_fields_bad_token(client, anon):
    assert client.post("/api/hooks/claude", json=hook("SessionStart", cwd="/nowhere")).status_code == 200
    rows = client.get("/api/sessions").json()
    assert rows[0]["project_id"] is None
    tl = client.get("/api/timeline", params={"from": "2020-01-01T00:00:00Z", "to": "2030-01-01T00:00:00Z"}).json()
    assert tl["live"] == []
    assert client.post("/api/hooks/claude", json={}).status_code == 200 and client.post("/api/hooks/claude", content=b"not json").status_code == 200
    assert client.post("/api/hooks/claude", json={"hook_event_name": "Stop"}).status_code == 200
    assert len(client.get("/api/sessions").json()) == 1
    assert anon.post("/api/hooks/claude", json=hook("Stop")).status_code == 401
    assert anon.post("/api/hooks/claude", json=hook("Stop"), headers={"Authorization": "Bearer trj_nope"}).status_code == 401

def test_hook_bad_sizes_and_deep_json_are_ignored(client):
    assert client.post("/api/hooks/claude", json=hook("SessionStart", cwd="x" * 5000)).json() == {}
    assert client.post("/api/hooks/claude", content=b"[" * 100000).json() == {}
    assert client.post("/api/hooks/claude", json=hook("SessionStart", transcript_path="y" * 70000)).json() == {}
    assert client.get("/api/sessions").json() == []

def test_registry_wins_over_scan_and_ended_hides_scan(client, app):
    p = mk_project(client)
    client.post("/api/resources", json={"project_id": p["id"], "kind": "folder", "locator": "/home/u/proj"})
    live = [{"project_id": p["id"], "session_id": sid, "cwd": "/home/u/proj", "title": "T", "started": None, "last_seen": "2026-01-05T12:00:00Z"} for sid in ("s1", "s2")]
    set_state(app.state.ledger.conn, "live_sessions", live)
    client.post("/api/hooks/claude", json=hook("SessionStart", sid="s1"))
    client.post("/api/hooks/claude", json=hook("SessionEnd", sid="s1", reason="other"))
    client.post("/api/hooks/claude", json=hook("SessionStart", sid="s3"))
    tl = client.get("/api/timeline", params={"from": "2020-01-01T00:00:00Z", "to": "2030-01-01T00:00:00Z"}).json()
    assert sorted(l["session_id"] for l in tl["live"]) == ["s2", "s3"]
