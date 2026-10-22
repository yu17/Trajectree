from trajectree.cli.main import main


def env_for(app):
    return {"TRAJECTREE_URL": "http://testserver", "TRAJECTREE_TOKEN": app.state.token}


def test_cli_creates_event_by_slug(app, client, anon, capsys):
    client.post("/api/projects", json={"slug": "clmi", "title": "CLMI", "kind": "research"})
    assert main(["event", "clmi", "Encoder frozen", "--kind", "decision", "--at", "2026-01-05T10:00:00Z"], client=anon, env=env_for(app)) == 0
    pid = client.get("/api/projects").json()[0]["id"]
    events = client.get("/api/events", params={"projects": pid}).json()
    assert events[0]["title"] == "Encoder frozen" and events[0]["kind"] == "decision"
    assert client.get("/api/ledger/txns").json()[0]["tool"] == "cli"


def test_cli_unknown_slug_exits_1(app, anon, capsys):
    assert main(["note", "ghost", "hello"], client=anon, env=env_for(app)) == 1
    assert "ghost" in capsys.readouterr().err


def test_cli_revert_and_txns(app, client, anon, capsys):
    client.post("/api/projects", json={"slug": "clmi", "title": "CLMI", "kind": "research"})
    main(["note", "clmi", "hello"], client=anon, env=env_for(app))
    tid = client.get("/api/ledger/txns").json()[0]["id"]
    assert main(["revert", tid], client=anon, env=env_for(app)) == 0
    assert main(["txns", "--limit", "3"], client=anon, env=env_for(app)) == 0
    assert f"revert {tid}" in capsys.readouterr().out


def test_cli_missing_token_exits_2(anon, tmp_path, capsys):
    env = {"TRAJECTREE_URL": "http://testserver", "HOME": str(tmp_path)}
    assert main(["projects"], client=anon, env=env) == 2
    assert "token" in capsys.readouterr().err


def test_cli_token_file_and_projects_listing(app, client, anon, tmp_path, capsys):
    client.post("/api/projects", json={"slug": "clmi", "title": "CLMI", "kind": "research"})
    tok = tmp_path / ".config" / "trajectree" / "token"
    tok.parent.mkdir(parents=True)
    tok.write_text(app.state.token + "\n")
    env = {"TRAJECTREE_URL": "http://testserver", "HOME": str(tmp_path)}
    assert main(["projects"], client=anon, env=env) == 0
    assert capsys.readouterr().out.split() == ["clmi", "CLMI", "active"]


def test_cli_usage_error_exits_2(app, anon):
    assert main(["bogus"], client=anon, env=env_for(app)) == 2


def test_cli_project_add_and_todo(app, client, anon, capsys):
    assert main(["project", "add", "p1", "P One", "--kind", "research"], client=anon, env=env_for(app)) == 0
    assert main(["todo", "p1", "Do it", "--due", "2026-02-01T00:00:00Z"], client=anon, env=env_for(app)) == 0
    assert main(["todo", "-", "Loose"], client=anon, env=env_for(app)) == 0
    todos = client.get("/api/todos").json()
    assert {t["title"] for t in todos} == {"Do it", "Loose"}
    assert [t["project_id"] for t in todos if t["title"] == "Loose"] == [None]


def test_cli_context_prints_pack(app, client, anon, capsys):
    client.post("/api/projects", json={"slug": "clmi", "title": "CLMI", "kind": "research"})
    main(["note", "clmi", "hello"], client=anon, env=env_for(app))
    capsys.readouterr()  # discard the note id printed above
    assert main(["context", "clmi", "--days", "30"], client=anon, env=env_for(app)) == 0
    out = capsys.readouterr().out
    assert out.startswith("# Trajectree context") and "hello" in out


def test_cli_context_rejects_bad_budget(app, anon, capsys):
    assert main(["context", "clmi", "--budget", "5"], client=anon, env=env_for(app)) == 2
    assert "invalid budget" in capsys.readouterr().err


def test_mcp_subcommand_requires_token(tmp_path, capsys):
    assert main(["mcp"], env={"HOME": str(tmp_path)}) == 2
    assert "no token" in capsys.readouterr().err


def test_cli_ingest(app, anon, capsys):
    assert main(["ingest"], client=anon, env=env_for(app)) == 0
    assert capsys.readouterr().out.startswith("ingest: ")
