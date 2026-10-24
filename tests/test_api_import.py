import subprocess
from datetime import datetime, timezone

from trajectree.auth.scopes import Scope
from trajectree.auth.tokens import mint_token
from trajectree.ingest.job import run_ingest
from trajectree.models import Actor
from trajectree.repo.resources import find_resource
from trajectree.repo.state import get_state
from tests.test_api_entities import mk_project


def setup(client, tmp_path, remotes=()):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    for name, url in remotes:
        subprocess.run(["git", "-C", str(repo), "remote", "add", name, url], check=True)
    p = mk_project(client)
    client.post("/api/resources", json={"project_id": p["id"], "kind": "folder", "locator": str(repo)})
    return p, repo


def props_of(client, p):
    return next(x for x in client.get("/api/projects").json() if x["id"] == p["id"])["props"]


def pick(*names, **since):
    return {n: {"picked": True, **({"since": since[n]} if n in since else {})} for n in names}


def test_import_saves_record_and_resources(client, tmp_path):
    p, repo = setup(client, tmp_path)
    body = {"sources": {**pick("git", "folder"), "claude": {"picked": False}}}
    r = client.post(f"/api/projects/{p['id']}/import", json=body)
    assert r.status_code == 200, r.text
    out = r.json()
    assert sorted(x["kind"] for x in out["resources"]) == ["folder", "git_local"]
    assert out["starting_point_run"] is None
    record = out["project"]["props"]["import"]
    assert record["sources"]["git"]["picked"] is True and record["sources"]["claude"]["picked"] is False
    assert record["start_run_id"] is None and record["recorded_at"]
    assert props_of(client, p)["import"] == record


def test_picked_remote_is_accepted_without_inbox(client, app, tmp_path):
    p, repo = setup(client, tmp_path, remotes=[("origin", "git@github.com:o/r.git")])
    body = {"sources": pick("git", "remote"), "remotes": ["https://github.com/o/r"]}
    assert client.post(f"/api/projects/{p['id']}/import", json=body).status_code == 200
    ledger = app.state.ledger
    res = find_resource(ledger.conn, p["id"], "git_remote", "https://github.com/o/r")
    assert res is not None and res.title == "o/r"
    row = ledger.conn.execute(
        "select t.review from change c join txn t on t.id = c.txn_id where c.table_name = 'resource'"
        " and json_extract(c.pk, '$[0]') = ?", (res.id,)).fetchall()
    assert row and all(review != "pending" for (review,) in row)
    assert ledger.list_txns(review="pending") == []


def test_unpicked_remote_recorded_declined(client, app, settings, tmp_path):
    p, repo = setup(client, tmp_path, remotes=[("origin", "git@github.com:o/r.git"), ("up", "https://github.com/o/s.git")])
    body = {"sources": pick("git", "remote"), "remotes": ["https://github.com/o/r"]}
    assert client.post(f"/api/projects/{p['id']}/import", json=body).status_code == 200
    suggested = get_state(app.state.ledger.conn, "remote:suggested")
    assert set(suggested[p["id"]]) == {"https://github.com/o/r", "https://github.com/o/s"}
    settings.remote_every_m = 0
    run_ingest(app.state.ledger, settings, now=datetime(2026, 10, 24, 12, tzinfo=timezone.utc))
    assert find_resource(app.state.ledger.conn, p["id"], "git_remote", "https://github.com/o/s") is None
    assert app.state.ledger.list_txns(review="pending") == []


def test_reimport_of_picked_source_is_409(client, tmp_path):
    p, repo = setup(client, tmp_path)
    url = f"/api/projects/{p['id']}/import"
    assert client.post(url, json={"sources": pick("git")}).status_code == 200
    again = client.post(url, json={"sources": pick("git", "folder")})
    assert again.status_code == 409 and again.json()["detail"] == "already imported: git"
    # a source not picked before may still be picked, and the earlier pick stays
    ok = client.post(url, json={"sources": pick("folder")})
    assert ok.status_code == 200
    sources = ok.json()["project"]["props"]["import"]["sources"]
    assert sources["git"]["picked"] and sources["folder"]["picked"]


def test_start_block_enqueues_starting_point(client, tmp_path):
    p, repo = setup(client, tmp_path)
    body = {
        "sources": {**pick("git", "claude"), "folder": {"picked": False}},
        "starts": {"git": "2025-01-01", "claude": "2025-06-01"},
        "start_block": {"model": "claude-opus-5-5"},
    }
    r = client.post(f"/api/projects/{p['id']}/import", json=body)
    assert r.status_code == 200, r.text
    out = r.json()
    run = out["starting_point_run"]
    assert run["kind"] == "starting_point" and run["model"] == "claude-opus-5-5"
    request = run["props"]["request"]
    assert request["starts"] == {"git": "2025-01-01", "claude": "2025-06-01"} and request["model"] == "claude-opus-5-5"
    assert out["project"]["props"]["import"]["start_run_id"] == run["id"]


def test_start_block_validation(client, tmp_path):
    p, repo = setup(client, tmp_path)
    url = f"/api/projects/{p['id']}/import"
    block = {"model": "m"}
    assert client.post(url, json={"sources": pick("git"), "start_block": block}).status_code == 422
    assert client.post(url, json={"sources": pick("git"), "starts": {"claude": "2025-01-01"}, "start_block": block}).status_code == 422
    assert client.post(url, json={"sources": {"nope": {"picked": True}}}).status_code == 422
    # nothing was saved by the rejected requests
    assert "import" not in props_of(client, p)


def test_import_needs_project_and_folder(client):
    assert client.post("/api/projects/nope/import", json={"sources": pick("git")}).status_code == 404
    p = mk_project(client)
    assert client.post(f"/api/projects/{p['id']}/import", json={"sources": pick("git")}).status_code == 422


def test_probe_endpoint(client, tmp_path):
    (tmp_path / "a.txt").write_text("x")
    r = client.post("/api/import/probe", json={"path": str(tmp_path)})
    assert r.status_code == 200
    body = r.json()
    assert [s["source"] for s in body["sources"]] == ["git", "claude", "codex", "folder"] and body["default_since"]
    assert client.post("/api/import/probe", json={"path": "relative"}).status_code == 422


def test_probe_and_import_are_owner_only(client, anon, app, tmp_path):
    p, repo = setup(client, tmp_path)
    _, secret = mint_token(app.state.ledger.conn, label="run", scope=Scope(), actor=Actor(kind="agent", ref="x"), ttl=None)
    auth = {"Authorization": f"Bearer {secret}"}
    probe = anon.post("/api/import/probe", json={"path": str(repo)}, headers=auth)
    imp = anon.post(f"/api/projects/{p['id']}/import", json={"sources": pick("git")}, headers=auth)
    # The same owner-only /api router as every other route: a scoped token is refused outright.
    assert probe.status_code in (401, 403) and imp.status_code in (401, 403)
    assert "import" not in props_of(client, p)


def test_invalid_runner_leaves_nothing_and_retry_succeeds(client, app, tmp_path):
    p, repo = setup(client, tmp_path, remotes=[("origin", "git@github.com:o/r.git")])
    url = f"/api/projects/{p['id']}/import"
    body = {
        "sources": pick("git", "remote"),
        "remotes": ["https://github.com/o/r"],
        "starts": {"git": "2025-01-01"},
        "start_block": {"runner": "bad runner", "model": "m"},
    }
    r = client.post(url, json=body)
    assert r.status_code == 422 and "invalid runner" in r.json()["detail"]
    conn = app.state.ledger.conn
    assert "import" not in props_of(client, p)
    assert [x["kind"] for x in client.get("/api/resources", params={"projects": p["id"]}).json()] == ["folder"]
    assert get_state(conn, "remote:suggested") is None
    assert conn.execute("select count(*) from run").fetchone()[0] == 0
    body["start_block"]["runner"] = "claude"
    ok = client.post(url, json=body)
    assert ok.status_code == 200 and ok.json()["starting_point_run"]["id"]


def test_start_block_without_model_is_422_and_leaves_nothing(client, app, tmp_path):
    p, repo = setup(client, tmp_path)
    url = f"/api/projects/{p['id']}/import"
    for block in ({}, {"runner": "claude"}, {"model": None}):
        body = {"sources": pick("git"), "starts": {"git": "2025-01-01"}, "start_block": block}
        r = client.post(url, json=body)
        assert (r.status_code, r.json()["detail"]) == (422, "no model was chosen"), block
    body["start_block"] = {"model": "nope"}
    assert client.post(url, json=body).json()["detail"] == "unknown model nope; run: trajectree models --refresh"
    assert "import" not in props_of(client, p)
    assert [x["kind"] for x in client.get("/api/resources", params={"projects": p["id"]}).json()] == ["folder"]
    assert app.state.ledger.conn.execute("select count(*) from run").fetchone()[0] == 0


def test_paused_runs_leave_nothing(client, app, tmp_path):
    p, repo = setup(client, tmp_path)
    assert client.put("/api/caps", json={"paused": True}).status_code == 200
    body = {"sources": pick("git"), "starts": {"git": "2025-01-01"}, "start_block": {"model": "m"}}
    assert client.post(f"/api/projects/{p['id']}/import", json=body).status_code == 409
    assert "import" not in props_of(client, p)
    assert [x["kind"] for x in client.get("/api/resources", params={"projects": p["id"]}).json()] == ["folder"]


def test_probe_path_outside_mapped_roots(client, settings, tmp_path):
    settings.path_map = [(str(tmp_path / "host"), str(tmp_path / "local"))]
    (tmp_path / "host" / "a").mkdir(parents=True)
    other = client.post("/api/import/probe", json={"path": "/etc"})
    assert other.status_code == 422 and other.json()["detail"] == "path /etc is outside every mapped folder"
    escape = client.post("/api/import/probe", json={"path": f"{tmp_path}/host/../../etc"})
    assert escape.status_code == 422 and "outside every mapped folder" in escape.json()["detail"]
    assert client.post("/api/import/probe", json={"path": f"{tmp_path}/host/a/../a"}).status_code == 200
