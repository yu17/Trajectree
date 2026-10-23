from tests.test_api_entities import mk_project


def test_routine_crud_and_run(client):
    r = client.post("/api/routines", json={"name": "x", "trigger": "@daily", "prompt_ref": "cross_project_review.md"})
    assert r.status_code == 201 and r.json()["enabled"] is False and r.json()["next_fire"] is None
    assert client.post("/api/routines", json={"name": "x", "trigger": "@daily", "prompt_ref": "a.md"}).status_code == 409
    assert client.post("/api/routines", json={"name": "y", "trigger": "bad", "prompt_ref": "a.md"}).status_code == 422
    assert client.patch("/api/routines/x", json={"enabled": True}).json()["next_fire"]
    assert client.get(f"/api/routines/{r.json()['id']}").json()["name"] == "x"
    assert client.get("/api/routines/nope").status_code == 404
    run = client.post("/api/routines/x/run", json={}).json()
    assert run["slot"] == "scheduled" and client.get("/api/routines/x/runs").json()[0]["id"] == run["id"]
    assert [x["id"] for x in client.get("/api/runs", params={"routine": "x"}).json()] == [run["id"]]
    assert client.get("/api/runs", params={"routine": "nope"}).status_code == 404
    assert client.get("/api/scheduler").json()["interval_s"] == 30
    assert client.post("/api/scheduler/tick").status_code == 200
    assert "by_routine" in client.get("/api/usage").json()


def test_routines_list_includes_seeds(client):
    names = [r["name"] for r in client.get("/api/routines").json()]
    assert names == ["cross-project-review", "meeting-prep"]
    assert client.get("/api/projects").json()[0]["slug"] == "inbox"   # seed routines deliver there


def test_patch_rejects_null_for_required_fields(client):
    client.post("/api/routines", json={"name": "x", "trigger": "@daily", "prompt_ref": "a.md"})
    for field in ("name", "trigger", "tz", "prompt_ref", "enabled", "scope", "cwd_mode", "tool_policy", "caps",
                  "write_mode", "delivery"):
        assert client.patch("/api/routines/x", json={field: None}).status_code == 422, field
    ok = client.patch("/api/routines/x", json={"runner": None, "model": None})
    assert ok.status_code == 200 and ok.json()["runner"] is None
    assert client.post("/api/routines", json={"name": "y", "trigger": "@daily", "prompt_ref": "b.md"}).status_code == 201
    assert client.patch("/api/routines/y", json={"name": "x"}).status_code == 409


def test_run_routine_with_projects_and_unknown_project(client):
    mk_project(client)
    client.post("/api/routines", json={"name": "x", "trigger": "@daily", "prompt_ref": "a.md"})
    run = client.post("/api/routines/x/run", json={"projects": ["clmi"]})
    assert run.status_code == 202 and run.json()["props"]["request"]["projects"] == ["clmi"]
    assert client.post("/api/routines/x/run", json={"projects": ["zzz"]}).status_code == 422
    assert client.post("/api/routines/nope/run", json={}).status_code == 404


def test_run_routine_paused_is_409(client):
    client.post("/api/routines", json={"name": "x", "trigger": "@daily", "prompt_ref": "a.md"})
    caps = client.get("/api/caps").json()
    client.put("/api/caps", json={**caps, "paused": True})
    assert client.post("/api/routines/x/run", json={}).status_code == 409


def test_usage_by_routine_uses_names(client, ledger):
    from datetime import datetime, timezone
    from trajectree.repo.runs import create_run, update_run
    r = client.post("/api/routines", json={"name": "x", "trigger": "@daily", "prompt_ref": "a.md"}).json()
    now = datetime.now(timezone.utc)
    with ledger.locked() as conn:
        a = create_run(conn, kind="routine", runner="claude", backend="claude", model=None, project_id=None,
                       routine_id=r["id"], slot="scheduled")
        update_run(conn, a.id, status="ok", est_usd=0.5, started=now)
        b = create_run(conn, kind="ask", runner="claude", backend="claude", model=None, project_id=None)
        update_run(conn, b.id, status="ok", est_usd=0.25, started=now)
    by = {s["key"]: s["est_usd"] for s in client.get("/api/usage").json()["by_routine"]}
    assert by == {"x": 0.5, "-": 0.25}


def test_scheduler_status_and_tick_report(client):
    rep = client.post("/api/scheduler/tick").json()
    assert set(rep) == {"ticked", "fired", "skipped", "retried", "errors"}
    status = client.get("/api/scheduler").json()
    assert set(status) == {"last_tick", "interval_s", "next_fires"} and status["next_fires"] == []
