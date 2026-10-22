from datetime import datetime, timezone
from tests.test_api_entities import mk_project
from trajectree.models import SignalIn
from trajectree.repo.signals import upsert_signals
from trajectree.repo.state import set_state

UTC = timezone.utc

def test_timeline_includes_strip_live_and_stale(client, app):
    p = mk_project(client)
    conn = app.state.ledger.conn
    upsert_signals(conn, [SignalIn(project_id=p["id"], kind="session", t_start=datetime(2026, 1, 5, 10, tzinfo=UTC), dedupe_key="s1"),
                          SignalIn(project_id=p["id"], kind="commits", t_start=datetime(2026, 1, 5, 11, tzinfo=UTC), dedupe_key="g1", payload={"count": 3})])
    set_state(conn, "stale_projects", [p["id"], "other"])
    set_state(conn, "live_sessions", [{"project_id": p["id"], "session_id": "x", "cwd": "/c", "title": None, "started": None, "last_seen": "2026-01-05T12:00:00Z"},
                                      {"project_id": "other", "session_id": "y", "cwd": "/d", "title": None, "started": None, "last_seen": "2026-01-05T12:00:00Z"}])
    tl = client.get("/api/timeline", params={"from": "2026-01-01T00:00:00Z", "to": "2026-02-01T00:00:00Z"}).json()
    assert sorted((b["kind"], b["count"]) for b in tl["strip"]) == [("commits", 3), ("session", 1)]  # commits bins sum payload counts
    assert tl["stale"] == [p["id"]] and [l["session_id"] for l in tl["live"]] == ["x"]

def test_signals_route_filters_and_limits(client, app):
    p = mk_project(client)
    upsert_signals(app.state.ledger.conn, [SignalIn(project_id=p["id"], kind="session", t_start=datetime(2026, 1, 5, tzinfo=UTC), dedupe_key="s1"),
                                           SignalIn(project_id=p["id"], kind="commits", t_start=datetime(2026, 1, 6, tzinfo=UTC), dedupe_key="g1")])
    rows = client.get("/api/signals", params={"projects": p["id"], "kinds": "commits"}).json()
    assert [r["dedupe_key"] for r in rows] == ["g1"]
    assert client.get("/api/signals", params={"limit": 0}).status_code == 422

def test_ingest_run_and_status(client):
    assert client.get("/api/ingest/status").json() is None
    r = client.post("/api/ingest/run")
    assert r.status_code == 200 and "duration_ms" in r.json()
    assert client.get("/api/ingest/status").json()["duration_ms"] == r.json()["duration_ms"]

def test_ingest_run_409_when_busy(client):
    import trajectree.ingest.job as job
    assert job._RUNNING.acquire(blocking=False)
    try:
        assert client.post("/api/ingest/run").status_code == 409
    finally:
        job._RUNNING.release()

def test_timeline_skips_malformed_live_entries(client, app):
    p = mk_project(client)
    set_state(app.state.ledger.conn, "live_sessions", [{"project_id": p["id"]}, {"project_id": p["id"], "session_id": "ok", "cwd": "/c", "title": None, "started": None, "last_seen": "2026-01-05T12:00:00Z"}])
    tl = client.get("/api/timeline", params={"from": "2026-01-01T00:00:00Z", "to": "2026-02-01T00:00:00Z"}).json()
    assert [l["session_id"] for l in tl["live"]] == ["ok"]
