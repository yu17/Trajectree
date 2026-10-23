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


def test_chatmaid_message_rejects_anonymous_cookie_and_missing_inbox(anon, client, ledger, app):
    msg = {"ref": "m1", "summary": "x", "channel": "c"}
    assert anon.post("/api/connectors/chatmaid/messages", json=msg).status_code == 401
    cookie = {"Cookie": f"trajectree_token={app.state.token}"}
    assert anon.post("/api/connectors/chatmaid/messages", json=msg, headers=cookie).status_code == 401
    ledger.conn.execute("DELETE FROM project WHERE slug = 'inbox'")
    r = client.post("/api/connectors/chatmaid/messages", json=msg)
    assert r.status_code == 422 and r.json()["detail"] == "no inbox project"
