from datetime import datetime, timedelta, timezone

from tests.test_catalog import MODEL
from trajectree.models import ProjectIn, SignalIn
from trajectree.repo.projects import create_project
from trajectree.repo.signals import upsert_signals

UTC = timezone.utc


def test_inspections_endpoint_lists_due_projects(client, anon, ledger, human):
    assert client.get("/api/inspections").json() == []
    p = create_project(ledger, human, ProjectIn(slug="a", title="A", kind="research"))
    t = datetime.now(UTC) - timedelta(days=3)
    with ledger.locked() as conn:
        upsert_signals(conn, [SignalIn(project_id=p.id, kind="session", t_start=t, t_end=t + timedelta(minutes=30), dedupe_key="s")])
    got = client.get("/api/inspections").json()
    assert len(got) == 1 and got[0]["slug"] == "a" and got[0]["stretches"] == 1
    assert anon.get("/api/inspections").status_code == 401


def _hold_queue(monkeypatch, app):
    """Keep the app's worker from picking up queued runs, so a queued inspection stays active."""
    monkeypatch.setattr(app.state.runs, "_next_queued", lambda slot="interactive": None)


def _due(ledger, human, slug):
    p = create_project(ledger, human, ProjectIn(slug=slug, title=slug.upper(), kind="research"))
    t = datetime.now(UTC) - timedelta(days=3)
    with ledger.locked() as conn:
        upsert_signals(conn, [SignalIn(project_id=p.id, kind="session", t_start=t, t_end=t + timedelta(minutes=30), dedupe_key=f"s-{slug}")])
    return p


def test_post_inspect_returns_202_and_409_when_active(client, app, ledger, human, monkeypatch):
    _hold_queue(monkeypatch, app)
    p = _due(ledger, human, "a")
    r = client.post(f"/api/projects/{p.id}/inspect", json={"model": MODEL})
    assert r.status_code == 202 and r.json()["kind"] == "inspect" and r.json()["status"] == "queued"
    again = client.post("/api/projects/a/inspect", json={"model": MODEL})
    assert again.status_code == 409 and again.json()["detail"] == "an inspection is already running for a"
    create_project(ledger, human, ProjectIn(slug="b", title="B", kind="research"))
    empty = client.post("/api/projects/b/inspect", json={"model": MODEL})
    assert empty.status_code == 409 and empty.json()["detail"] == "nothing to inspect for b"
    assert client.post("/api/projects/b/inspect", json={"model": "no-such-model"}).status_code == 422
    since = client.post("/api/projects/b/inspect", json={"model": MODEL, "since": "2026-01-01"})
    assert since.status_code == 409


def test_post_inspect_all_skips_projects_with_nothing_due(client, app, ledger, human, monkeypatch):
    _hold_queue(monkeypatch, app)
    a = _due(ledger, human, "a")
    create_project(ledger, human, ProjectIn(slug="b", title="B", kind="research"))
    r = client.post("/api/inspect", json={"projects": ["a", "b"], "model": MODEL})
    assert r.status_code == 200
    body = r.json()
    assert [run["project_id"] for run in body["runs"]] == [a.id] and body["skipped"] == ["b"]
    assert client.post("/api/inspect", json={"projects": ["b", "nope"], "model": MODEL}).status_code == 422
    # "all" covers every active project; the one already running is skipped too.
    c = _due(ledger, human, "c")
    body = client.post("/api/inspect", json={"projects": "all", "model": MODEL}).json()
    assert [run["project_id"] for run in body["runs"]] == [c.id]
    assert "a" in body["skipped"] and "b" in body["skipped"]


def test_inspections_lists_active_run_and_episode(client, app, ledger, human, monkeypatch):
    _hold_queue(monkeypatch, app)
    p = _due(ledger, human, "a")
    run = client.post(f"/api/projects/{p.id}/inspect", json={"model": MODEL}).json()
    [due] = client.get("/api/inspections").json()
    assert (due["active_run"], due["episode"], due["episodes"]) == (run["id"], 1, 1)
