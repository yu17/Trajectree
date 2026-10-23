from trajectree.repo.projects import get_project_by_slug
from trajectree.repo.signals import get_signal_by_key


def test_store_connector_credentials(client, settings):
    assert client.post("/api/connectors/github/credentials", json={"token": "ghp_x"}).json() == {"stored": True}
    path = settings.connectors_dir / "github_token"
    assert path.stat().st_mode & 0o777 == 0o600
    assert settings.connectors_dir.stat().st_mode & 0o777 == 0o700
    assert path.read_text() == "ghp_x"
    assert client.post("/api/connectors/nope/credentials", json={"token": "x"}).status_code == 404
    assert client.post("/api/connectors/gitlab/credentials", json={"token": ""}).status_code == 422
    assert client.post("/api/connectors/gitlab/credentials", json={"token": "   "}).status_code == 422


def test_chatmaid_message_edge_cases(client, ledger, human, app):
    # the app lifespan has already seeded the inbox project
    inbox = get_project_by_slug(ledger.conn, "inbox")
    assert inbox is not None
    tok = client.post("/api/tokens", json={"label": "chatmaid", "scope": {"projects": [], "tools": ["chatmaid"], "write": False}, "actor_kind": "connector"}).json()["secret"]
    h = {"Authorization": f"Bearer {tok}"}
    ok = client.post("/api/connectors/chatmaid/messages", json={"ref": "m1", "summary": "Advisor asks for slides", "channel": "email", "participants": ["Ada"]}, headers=h)
    assert ok.status_code == 200
    sig = get_signal_by_key(ledger.conn, "chatmaid:m1")
    assert sig.payload == {"ref": "m1", "summary": "Advisor asks for slides", "channel": "email", "participants": ["Ada"]}
    assert sig.kind == "message" and sig.project_id == inbox.id
    assert ok.json() == {"signal_id": sig.id, "project_id": inbox.id}
    assert client.post("/api/connectors/chatmaid/messages", json={"ref": "m2", "summary": "x", "channel": "email", "project": "nope"}, headers=h).status_code == 422
    assert client.post("/api/connectors/chatmaid/messages", json={"ref": "m3", "summary": "x", "channel": "email", "body": "secret"}, headers=h).status_code == 422
    assert client.post("/api/connectors/chatmaid/messages", content=b"x" * 70_000, headers={**h, "Content-Type": "application/json"}).status_code == 413
    hook_tok = client.post("/api/tokens", json={"label": "hooks:x", "scope": {"projects": [], "tools": ["hooks"], "write": False}}).json()["secret"]
    assert client.post("/api/connectors/chatmaid/messages", json={"ref": "m4", "summary": "x", "channel": "c"}, headers={"Authorization": f"Bearer {hook_tok}"}).status_code == 401
    assert client.post("/mcp", headers=h, json={}).status_code == 401  # a chatmaid token cannot reach MCP


def test_chatmaid_message_dedupes_on_ref_and_honours_project_and_when(client, ledger, human):
    from trajectree.models import ProjectIn
    from trajectree.repo.projects import create_project

    a = create_project(ledger, human, ProjectIn(slug="thesis", title="Thesis", kind="research"))
    body = {"ref": "m1", "summary": "first", "channel": "slack", "project": "thesis", "when": "2026-03-01T09:00:00Z"}
    first = client.post("/api/connectors/chatmaid/messages", json=body)  # the owner token is accepted
    second = client.post("/api/connectors/chatmaid/messages", json={**body, "summary": "second"})
    assert first.status_code == second.status_code == 200
    assert first.json()["signal_id"] == second.json()["signal_id"] and first.json()["project_id"] == a.id
    sig = get_signal_by_key(ledger.conn, "chatmaid:m1")
    assert sig.payload["summary"] == "second" and sig.t_start.isoformat().startswith("2026-03-01T09:00:00")
    assert ledger.conn.execute("SELECT count(*) FROM signal WHERE dedupe_key = 'chatmaid:m1'").fetchone()[0] == 1


def test_chatmaid_token_restricted_to_projects(client, ledger, human):
    from trajectree.models import ProjectIn
    from trajectree.repo.projects import create_project

    thesis = create_project(ledger, human, ProjectIn(slug="thesis", title="Thesis", kind="research"))

    def mint(projects):
        scope = {"projects": projects, "tools": ["chatmaid"], "write": False}
        secret = client.post("/api/tokens", json={"label": "chatmaid", "scope": scope, "actor_kind": "connector"}).json()["secret"]
        return {"Authorization": f"Bearer {secret}"}

    restricted = mint([thesis.id])
    to_inbox = client.post("/api/connectors/chatmaid/messages", json={"ref": "r1", "summary": "x", "channel": "email"}, headers=restricted)
    assert to_inbox.status_code == 403 and to_inbox.json()["detail"] == "token not permitted for project inbox"
    assert get_signal_by_key(ledger.conn, "chatmaid:r1") is None
    to_thesis = client.post("/api/connectors/chatmaid/messages", json={"ref": "r2", "summary": "x", "channel": "email", "project": "thesis"}, headers=restricted)
    assert to_thesis.status_code == 200 and to_thesis.json()["project_id"] == thesis.id
    # the documented recipe (`projects: []`) reaches every project
    anywhere = mint([])
    for ref, project in (("r3", None), ("r4", "thesis")):
        body = {"ref": ref, "summary": "x", "channel": "email", **({"project": project} if project else {})}
        assert client.post("/api/connectors/chatmaid/messages", json=body, headers=anywhere).status_code == 200


def test_chatmaid_message_rejects_anonymous_cookie_and_missing_inbox(anon, client, ledger, app):
    msg = {"ref": "m1", "summary": "x", "channel": "c"}
    assert anon.post("/api/connectors/chatmaid/messages", json=msg).status_code == 401
    cookie = {"Cookie": f"trajectree_token={app.state.token}"}
    assert anon.post("/api/connectors/chatmaid/messages", json=msg, headers=cookie).status_code == 401
    ledger.conn.execute("DELETE FROM project WHERE slug = 'inbox'")
    r = client.post("/api/connectors/chatmaid/messages", json=msg)
    assert r.status_code == 422 and r.json()["detail"] == "no inbox project"


def test_connector_status_reports_configuration_without_secrets(client, settings):
    import json

    client.post("/api/connectors/github/credentials", json={"token": "ghp_secret"})
    resp = client.get("/api/connectors/status")
    assert resp.status_code == 200
    rows = {r["name"]: r for r in resp.json()}
    assert list(rows) == ["claude-sessions", "local-git", "folders", "github", "gitlab", "slurm", "chatmaid", "recurrence"]
    assert rows["github"]["configured"] is True and rows["slurm"]["configured"] is False
    assert rows["gitlab"]["configured"] is False and rows["local-git"]["configured"] is True
    assert rows["gitlab"]["detail"] == "set TRAJECTREE_GITLAB_HOST"
    assert "ghp_secret" not in json.dumps(rows)


def test_gitlab_status_needs_token_and_host(client, settings):
    def gitlab():
        return {r["name"]: r for r in client.get("/api/connectors/status").json()}["gitlab"]

    client.post("/api/connectors/gitlab/credentials", json={"token": "glpat-secret"})
    assert gitlab()["configured"] is False and gitlab()["detail"] == "set TRAJECTREE_GITLAB_HOST"
    settings.gitlab_host = "gitlab.uni.edu"
    assert gitlab() == {**gitlab(), "configured": True, "detail": "host gitlab.uni.edu"}


def test_connector_status_surfaces_errors_and_last_run(client, ledger):
    from trajectree.repo.state import set_state

    with ledger.locked() as conn:
        set_state(conn, "ingest:last", {"started": "2026-01-05T10:00:00Z", "git": {"errors": [f"e{i}" for i in range(7)]}, "folders": {"errors": ["boom"]}, "recurrence": {"errors": []}})
        set_state(conn, "folders:last", {"at": "2026-01-05T09:00:00Z"})
    rows = {r["name"]: r for r in client.get("/api/connectors/status").json()}
    assert rows["local-git"]["errors"] == ["e0", "e1", "e2", "e3", "e4"]
    assert rows["local-git"]["last_run"] == "2026-01-05T10:00:00Z"
    assert rows["folders"]["errors"] == ["boom"] and rows["folders"]["last_run"] == "2026-01-05T09:00:00Z"
    assert rows["claude-sessions"]["last_run"] is None and rows["github"]["errors"] == []


def test_connector_status_tolerates_malformed_state(client, ledger):
    from trajectree.repo.state import set_state

    with ledger.locked() as conn:
        set_state(conn, "ingest:last", ["not", "a", "dict"])
        set_state(conn, "slurm:last", 5)
    assert client.get("/api/connectors/status").status_code == 200


def test_mounts(client, settings):
    body = client.get("/api/mounts").json()
    assert body["data_dir"] == str(settings.data_dir) and body["cfg_dir"] == str(settings.cfg_root)
    assert body["exports_dir"] == str(settings.exports_dir) and body["claude_home"] is None
    assert body["path_map"] == []
