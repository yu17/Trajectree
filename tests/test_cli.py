from trajectree.cli.main import main


def env_for(app):
    return {"TRAJECTREE_URL": "http://testserver", "TRAJECTREE_TOKEN": app.state.token}


def test_cli_creates_event_by_slug(app, client, anon, capsys):
    client.post("/api/projects", json={"slug": "clmi", "title": "CLMI", "kind": "research"})
    assert main(["event", "clmi", "Encoder frozen", "--kind", "decision", "--at", "2026-01-05T10:00:00Z"], client=anon, env=env_for(app)) == 0
    pid = next(p["id"] for p in client.get("/api/projects").json() if p["slug"] == "clmi")
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
    assert capsys.readouterr().out.splitlines() == ["inbox  Inbox  active", "clmi  CLMI  active"]   # inbox is seeded


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


def test_cli_connectors_login(app, anon, capsys):
    import io

    from trajectree.runner.credentials import load_connector_token

    assert main(["connectors", "login", "github"], client=anon, env=env_for(app), stdin=io.StringIO("ghp_abc\n")) == 0
    assert capsys.readouterr().out.strip() == "stored"
    assert load_connector_token(app.state.settings, "github") == "ghp_abc"
    assert main(["connectors", "login", "gitlab"], client=anon, env=env_for(app), stdin=io.StringIO("\n")) == 2
    assert load_connector_token(app.state.settings, "gitlab") is None
    assert main(["connectors", "login", "nope"], client=anon, env=env_for(app), stdin=io.StringIO("x\n")) == 2


def test_base_url_precedence(tmp_path):
    from trajectree.cli.main import DEFAULT_URL, base_url

    home = {"HOME": str(tmp_path)}
    assert base_url(home) == DEFAULT_URL
    (tmp_path / ".config" / "trajectree").mkdir(parents=True)
    (tmp_path / ".config" / "trajectree" / "url").write_text("  http://127.0.0.1:40210/ \n")
    assert base_url(home) == "http://127.0.0.1:40210"
    assert base_url({**home, "TRAJECTREE_URL": ""}) == "http://127.0.0.1:40210"
    assert base_url({**home, "TRAJECTREE_URL": "http://other:1/"}) == "http://other:1"


def test_base_url_without_a_scheme_names_where_it_came_from(tmp_path, capsys):
    import pytest
    from trajectree.cli.main import _Fail, base_url

    home = {"HOME": str(tmp_path)}
    with pytest.raises(_Fail) as e:
        base_url({**home, "TRAJECTREE_URL": "127.0.0.1:40210"})
    assert e.value.code == 2 and "TRAJECTREE_URL" in str(e.value) and "http://" in str(e.value)
    (tmp_path / ".config" / "trajectree").mkdir(parents=True)
    (tmp_path / ".config" / "trajectree" / "url").write_text("127.0.0.1:40210\n")
    with pytest.raises(_Fail) as e:
        base_url(home)
    assert str(tmp_path / ".config" / "trajectree" / "url") in str(e.value)
    assert main(["runs"], env={**home, "TRAJECTREE_TOKEN": "t"}) == 2   # a usage error, not a traceback
    assert "must start with http://" in capsys.readouterr().err
