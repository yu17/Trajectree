import json, socket, threading, time
import httpx
import pytest
import uvicorn
from tests.test_api_entities import mk_project
from trajectree.api.app import create_app
from trajectree.runner.credentials import store_oauth_token

STRUCTURED = json.dumps({"answer": "Parsed", "proposals": [{"kind": "event", "project": "clmi", "title": "Decided X", "reason": "decision taken", "event_kind": "decision"}]})

def wait_run(client, run_id, deadline=15):
    end = time.monotonic() + deadline
    while time.monotonic() < end:
        d = client.get(f"/api/runs/{run_id}").json()
        if d["run"]["status"] not in ("queued", "running"):
            return d
        time.sleep(0.1)
    raise AssertionError("run did not finish")

def test_quick_add_run_and_accept_proposal(client, settings, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ok")
    monkeypatch.setenv("FAKE_CLAUDE_STRUCTURED", STRUCTURED)
    store_oauth_token(settings, "fake")
    p = mk_project(client)   # slug clmi
    r = client.post("/api/quick-add", json={"text": "we decided X"})
    assert r.status_code == 202 and r.json()["status"] == "queued"
    d = wait_run(client, r.json()["id"])
    assert d["run"]["status"] == "ok" and d["run"]["result_md"] == "Parsed" and d["txn_ids"] == []
    a = client.post(f"/api/runs/{d['run']['id']}/proposals/0/accept")
    assert a.status_code == 200 and a.json()["proposals"][0]["applied_txn"]
    assert client.get("/api/events", params={"projects": p["id"]}).json()[0]["title"] == "Decided X"
    assert client.post(f"/api/runs/{d['run']['id']}/proposals/0/accept").status_code == 409
    assert client.post(f"/api/runs/{d['run']['id']}/proposals/9/accept").status_code == 404
    assert [x["id"] for x in client.get("/api/runs", params={"open_proposals": "true"}).json()] == []
    assert client.get("/api/runs").json()[0]["id"] == d["run"]["id"]

def test_dismiss_proposal_api(client, settings, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ok")
    monkeypatch.setenv("FAKE_CLAUDE_STRUCTURED", STRUCTURED)
    store_oauth_token(settings, "fake")
    mk_project(client)
    d = wait_run(client, client.post("/api/quick-add", json={"text": "we decided X"}).json()["id"])
    run_id = d["run"]["id"]
    assert [x["id"] for x in client.get("/api/runs", params={"open_proposals": "true"}).json()] == [run_id]
    r = client.post(f"/api/runs/{run_id}/proposals/0/dismiss")
    assert r.status_code == 200 and r.json()["proposals"][0]["dismissed"] is True and r.json()["proposals"][0]["applied_txn"] is None
    assert client.post(f"/api/runs/{run_id}/proposals/0/dismiss").status_code == 409
    assert client.post(f"/api/runs/{run_id}/proposals/0/accept").json()["detail"] == "proposal 0 was dismissed"
    assert client.post(f"/api/runs/{run_id}/proposals/5/dismiss").status_code == 404
    assert client.post("/api/runs/nope/proposals/0/dismiss").status_code == 404
    assert client.get("/api/runs", params={"open_proposals": "true"}).json() == []

def test_startup_fails_runs_left_running(settings, ledger):
    from fastapi.testclient import TestClient
    from trajectree.repo.runs import create_run, get_run, update_run
    run = create_run(ledger.conn, kind="ask", runner="claude", backend="claude", model=None, project_id=None)
    update_run(ledger.conn, run.id, status="running")
    with TestClient(create_app(settings, ledger=ledger)):
        after = get_run(ledger.conn, run.id)
    assert after.status == "failed" and after.error == "daemon restarted"

def test_caps_per_run_must_not_exceed_per_day(client):
    r = client.put("/api/caps", json={"usd_per_run": 3, "usd_per_day": 2, "timeout_s": 120, "paused": False})
    assert r.status_code == 422 and "usd_per_run must not exceed usd_per_day" in json.dumps(r.json())
    assert client.get("/api/caps").json()["usd_per_run"] == 1.0
    assert client.put("/api/caps", json={"usd_per_run": 2, "usd_per_day": 2, "timeout_s": 120, "paused": False}).status_code == 200

def test_caps_pause_and_usage(client):
    assert client.get("/api/caps").json()["paused"] is False
    assert client.put("/api/caps", json={"usd_per_run": 0.5, "usd_per_day": 2, "timeout_s": 120, "paused": True}).status_code == 200
    assert client.post("/api/ask", json={"question": "q"}).status_code == 409
    assert client.put("/api/caps", json={"usd_per_run": 0.5, "usd_per_day": 2, "timeout_s": 120, "paused": False}).status_code == 200
    u = client.get("/api/usage", params={"days": 7}).json()
    assert len(u["days"]) == 7 and u["caps"]["usd_per_day"] == 2 and u["spent_today"] == 0
    assert client.put("/api/caps", json={"usd_per_run": -1}).status_code == 422
    assert client.post("/api/ask", json={"question": "q", "runner": "evil"}).status_code == 422
    assert client.post("/api/ask", json={"question": "q", "projects": ["zzz"]}).status_code == 422

def test_runner_status_credentials_and_backends_sync(client, settings, tmp_path):
    assert client.get("/api/runner/status").json()["claude"]["authenticated"] is False
    assert client.post("/api/runner/credentials", json={"oauth_token": "  "}).status_code == 422
    assert client.post("/api/runner/credentials", json={"oauth_token": "tok"}).json() == {"stored": True}
    assert client.get("/api/runner/status").json()["claude"]["authenticated"] is True
    assert client.post("/api/backends/sync").json() == {"synced": []}

def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]

def test_summarize_run_end_to_end_over_http(ledger, human, settings, monkeypatch):
    """The fake CLI calls set_project_summary over the real MCP endpoint with its run token."""
    port = free_port()
    s = settings.model_copy(update={"port": port})
    store_oauth_token(s, "fake")
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "mcp")
    monkeypatch.setenv("FAKE_CLAUDE_PROJECT", "clmi")
    app = create_app(s, ledger=ledger)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    h = {"Authorization": f"Bearer {app.state.token}"}
    try:
        for _ in range(100):
            try:
                if httpx.get(base + "/api/health").status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.1)
        p = httpx.post(base + "/api/projects", json={"slug": "clmi", "title": "CLMI", "kind": "research"}, headers=h).json()
        run = httpx.post(base + f"/api/projects/{p['id']}/summarize", json={}, headers=h).json()
        for _ in range(150):
            d = httpx.get(base + f"/api/runs/{run['id']}", headers=h).json()
            if d["run"]["status"] not in ("queued", "running"):
                break
            time.sleep(0.1)
        assert d["run"]["status"] == "ok", d["run"]["error"]
        assert len(d["txn_ids"]) == 1
        t = httpx.get(base + f"/api/ledger/txns/{d['txn_ids'][0]}", headers=h).json()["txn"]
        assert (t["actor_kind"], t["actor_ref"], t["run_id"], t["review"], t["tool"]) == ("agent", run["id"], run["id"], "pending", "claude")
        assert httpx.get(base + "/api/projects", headers=h).json()[0]["summary_md"] == "# Fresh summary"
    finally:
        server.should_exit = True
        thread.join(10)
