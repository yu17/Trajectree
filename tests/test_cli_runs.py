import io, json
import pytest
from tests.test_catalog import MODEL
from tests.test_cli import env_for
from trajectree.cli import main as cli
from trajectree.runner.credentials import store_oauth_token

STRUCTURED = json.dumps({"answer": "Parsed", "proposals": [{"kind": "todo", "project": "clmi", "title": "Email Bob", "reason": "owed"}]})

@pytest.fixture(autouse=True)
def fast(monkeypatch, settings):
    monkeypatch.setattr(cli, "_sleep", lambda s: None)
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ok")
    monkeypatch.setenv("FAKE_CLAUDE_STRUCTURED", STRUCTURED)
    store_oauth_token(settings, "fake")

def test_cli_add_waits_prints_proposals_and_accept(app, client, anon, capsys):
    client.post("/api/projects", json={"slug": "clmi", "title": "CLMI", "kind": "research"})
    assert cli.main(["add", "email Bob about slides", "--model", MODEL], client=anon, env=env_for(app)) == 0
    out = capsys.readouterr().out
    assert "run " in out and " ok " in out and "[1] todo · clmi · Email Bob (owed)" in out and "accept with: trajectree accept" in out
    run_id = out.split("run ")[1].split()[0]
    assert cli.main(["accept", run_id, "1"], client=anon, env=env_for(app)) == 0
    assert capsys.readouterr().out.startswith("applied: todo ")
    assert client.get("/api/todos").json()[0]["title"] == "Email Bob"
    assert cli.main(["accept", run_id, "1"], client=anon, env=env_for(app)) == 1
    assert cli.main(["accept", run_id, "0"], client=anon, env=env_for(app)) == 2

def test_cli_summarize_no_wait_and_runs_listing(app, client, anon, capsys):
    client.post("/api/projects", json={"slug": "clmi", "title": "CLMI", "kind": "research"})
    assert cli.main(["summarize", "clmi", "--no-wait", "--model", MODEL], client=anon, env=env_for(app)) == 0
    assert capsys.readouterr().out.endswith(" queued\n")
    assert cli.main(["runs", "--limit", "5"], client=anon, env=env_for(app)) == 0
    line = capsys.readouterr().out.splitlines()[0]
    assert "summarize" in line and line.endswith("clmi")

def test_cli_summarize_session_posts_and_validates_args(app, client, anon, capsys):
    assert cli.main(["summarize", "--session", "nope", "--no-wait", "--model", MODEL], client=anon, env=env_for(app)) == 1   # 422 unknown session
    assert "unknown session 'nope'" in capsys.readouterr().err
    assert cli.main(["summarize", "clmi", "--session", "x", "--model", MODEL], client=anon, env=env_for(app)) == 2
    assert cli.main(["summarize"], client=anon, env=env_for(app)) == 2

def test_cli_caps_usage_login_and_status(app, anon, capsys):
    assert cli.main(["caps", "--usd-per-day", "2", "--pause"], client=anon, env=env_for(app)) == 0
    assert capsys.readouterr().out.strip() == "caps: run $1.0 · day $2.0 · timeout 600s · paused yes"
    assert cli.main(["ask", "q", "--model", MODEL], client=anon, env=env_for(app)) == 1   # paused → 409
    assert cli.main(["caps", "--resume"], client=anon, env=env_for(app)) == 0
    capsys.readouterr()
    assert cli.main(["usage"], client=anon, env=env_for(app)) == 0
    assert capsys.readouterr().out.startswith("today: $0.0000 of $2.0 · paused: no")
    assert cli.main(["login"], client=anon, env=env_for(app), stdin=io.StringIO("sk-ant-oat01-x\n")) == 0
    assert capsys.readouterr().out.strip() == "stored"
    assert cli.main(["login"], client=anon, env=env_for(app), stdin=io.StringIO("\n")) == 2
    assert cli.main(["runner", "status"], client=anon, env=env_for(app)) == 0
    assert "claude:" in capsys.readouterr().out
    assert cli.main(["backends", "sync"], client=anon, env=env_for(app)) == 0
    assert capsys.readouterr().out.strip() == "synced: none"

def test_cli_models_prints_and_refreshes(app, client, anon, capsys, settings):
    settings.codex_bin = settings.claude_bin  # any binary that exists
    settings.codex_home.mkdir(parents=True, exist_ok=True)
    (settings.codex_home / "models_cache.json").write_text(json.dumps({"models": [{"slug": "gpt-6.1-sol", "display_name": "GPT-6.1-Sol", "visibility": "list"}]}))
    calls = []
    real_post = anon.post
    anon.post = lambda *a, **k: (calls.append(a[0]), real_post(*a, **k))[1]
    assert cli.main(["models"], client=anon, env=env_for(app)) == 0
    assert not any("refresh" in c for c in calls)
    capsys.readouterr()
    assert cli.main(["models", "--refresh"], client=anon, env=env_for(app)) == 0
    assert calls[0].endswith("/api/models/refresh")
    out = capsys.readouterr().out
    assert "codex  refreshed " in out and "\n  gpt-6.1-sol  GPT-6.1-Sol\n" in out
    assert "claude  refreshed " in out and "  claude-opus-5-5" in out


def test_slow_commands_get_a_long_http_timeout(monkeypatch, capsys):
    seen = []

    class Recording:
        def __init__(self, timeout):
            self.default = timeout

        def post(self, url, **kw):
            seen.append((url.rsplit("/api", 1)[1], kw.get("timeout", self.default)))
            body = {"synced": ["kimi"]} if url.endswith("/backends/sync") else {"runners": []}
            return type("R", (), {"status_code": 200, "json": lambda self: body})()

        def close(self):
            pass

    monkeypatch.setattr(cli.httpx, "Client", Recording)
    env = {"TRAJECTREE_TOKEN": "t", "TRAJECTREE_URL": "http://127.0.0.1:1"}
    assert cli.main(["models", "--refresh"], env=env) == 0
    assert cli.main(["backends", "sync"], env=env) == 0
    assert [path for path, _ in seen] == ["/models/refresh", "/backends/sync"]
    assert all(timeout >= 120 for _, timeout in seen)


def test_print_run_hides_applied_and_dismissed_hints(capsys):
    import sys
    detail = {"run": {"id": "r1", "status": "ok", "result_md": "", "est_usd": 0, "turns": 1, "duration_ms": 1, "error": None,
              "proposals": [{"kind": "todo", "project": "a", "title": "t1", "reason": "", "applied_txn": "x", "dismissed": False, "when": None, "due": None},
                            {"kind": "todo", "project": "a", "title": "t2", "reason": "", "applied_txn": None, "dismissed": True, "when": None, "due": None},
                            {"kind": "todo", "project": "a", "title": "t3", "reason": "", "applied_txn": None, "dismissed": False, "when": None, "due": None}]}, "txn_ids": []}
    cli._print_run(detail, sys.stdout)
    out = capsys.readouterr().out
    assert "[applied]" in out and "[dismissed]" in out and out.count("trajectree accept r1") == 1


def test_cli_routines_review_and_prep(app, client, anon, capsys):
    env = env_for(app)
    assert cli.main(["routines"], client=anon, env=env) == 0
    rows = [line.split("  ") for line in capsys.readouterr().out.splitlines()]
    assert rows == [["cross-project-review", "off", "no model", "0 7 * * 1", "UTC", "-", "-"],
                    ["meeting-prep", "off", "no model", "0 7 * * 1-5", "UTC", "-", "-"]]
    # No --model: the request is sent and the daemon refuses a routine that stores none.
    assert cli.main(["review", "--no-wait"], client=anon, env=env) == 1
    assert capsys.readouterr().err.strip() == "no model was chosen"
    assert client.get("/api/routines/cross-project-review/runs").json() == []
    client.patch("/api/routines/cross-project-review", json={"model": MODEL, "runner": "claude"})
    client.patch("/api/routines/meeting-prep", json={"model": "k2"})
    assert cli.main(["routines"], client=anon, env=env) == 0
    assert [line.split("  ")[2] for line in capsys.readouterr().out.splitlines()] == [f"claude/{MODEL}", "k2"]
    assert cli.main(["review", "--no-wait"], client=anon, env=env) == 0   # the stored model
    assert capsys.readouterr().out.endswith(" queued\n")
    [review] = client.get("/api/routines/cross-project-review/runs").json()
    assert (review["runner"], review["model"]) == ("claude", MODEL)
    client.post("/api/projects", json={"slug": "clmi", "title": "CLMI", "kind": "research"})
    # --model overrides meeting-prep's stored k2 for this run only
    assert cli.main(["prep", "--projects", "clmi", "--model", MODEL], client=anon, env=env) == 0   # waits for the run
    out = capsys.readouterr().out
    assert " ok " in out and "Parsed" in out
    [prep] = client.get("/api/routines/meeting-prep/runs").json()
    assert prep["props"]["request"]["projects"] == ["clmi"] and prep["status"] == "ok"
    assert (prep["runner"], prep["model"]) == ("claude", MODEL)
    assert client.get("/api/routines/meeting-prep").json()["model"] == "k2"
    assert cli.main(["run", "nope", "--no-wait"], client=anon, env=env) == 1


def test_cli_routine_enable_disable_and_scheduler(app, client, anon, capsys):
    env = env_for(app)
    assert cli.main(["routine", "enable", "meeting-prep"], client=anon, env=env) == 1   # seeded without a model
    assert capsys.readouterr().err.strip() == "no model was chosen"
    client.patch("/api/routines/meeting-prep", json={"model": MODEL})
    assert cli.main(["routine", "enable", "meeting-prep"], client=anon, env=env) == 0
    out = capsys.readouterr().out
    assert out.startswith("meeting-prep enabled · next fire ")
    assert client.get("/api/routines/meeting-prep").json()["enabled"] is True
    assert cli.main(["scheduler"], client=anon, env=env) == 0
    out = capsys.readouterr().out
    assert "every 30s" in out and "meeting-prep" in out
    assert cli.main(["routine", "disable", "meeting-prep"], client=anon, env=env) == 0
    assert capsys.readouterr().out.strip() == "meeting-prep disabled"
    assert client.get("/api/routines/meeting-prep").json()["next_fire"] is None
    assert cli.main(["scheduler", "tick"], client=anon, env=env) == 0
    assert capsys.readouterr().out.startswith("tick: 0 fired, 0 skipped, 0 retried")
    assert cli.main(["routine", "enable", "nope"], client=anon, env=env) == 1


def test_cli_routine_set(app, client, anon, capsys):
    env = env_for(app)
    assert cli.main(["routine", "set", "meeting-prep", "--model", "k2", "--runner", "ccx:kimi"], client=anon, env=env) == 0
    assert capsys.readouterr().out.strip() == "meeting-prep: model k2 via ccx:kimi"
    r = client.get("/api/routines/meeting-prep").json()
    assert (r["runner"], r["model"], r["enabled"]) == ("ccx:kimi", "k2", False)
    assert cli.main(["routine", "set", "meeting-prep", "--model", "gpt-6.1-sol"], client=anon, env=env) == 0
    assert capsys.readouterr().out.strip() == "meeting-prep: model gpt-6.1-sol"
    r = client.get("/api/routines/meeting-prep").json()
    assert (r["runner"], r["model"]) == (None, "gpt-6.1-sol")   # no --runner: the catalog decides at run time
    assert cli.main(["routine", "enable", "meeting-prep"], client=anon, env=env) == 0
    assert cli.main(["routine", "set", "meeting-prep"], client=anon, env=env) == 2   # --model is required
    assert cli.main(["routine", "set", "nope", "--model", "k2"], client=anon, env=env) == 1


NO_MODEL = "no model was chosen; pass --model (see: trajectree models)"


def test_cli_requires_model(capsys):
    class NoHttp:
        def __getattr__(self, name):
            raise AssertionError(f"HTTP {name} sent")

    for argv in (["summarize", "clmi"], ["summarize", "--session", "s1"], ["ask", "q"], ["ask", "q", "--runner", "claude"],
                 ["add", "t"], ["report", "draft", "--kind", "memo", "--title", "T", "--projects", "clmi"]):
        assert cli.main(argv, client=NoHttp(), env={"TRAJECTREE_TOKEN": "t"}) == 2, argv
        assert capsys.readouterr().err.strip() == NO_MODEL, argv


def test_cli_prints_model_warning(capsys):
    import sys
    detail = {"run": {"id": "r1", "status": "ok", "result_md": "", "est_usd": 0, "turns": 1, "duration_ms": 1, "error": None,
                      "proposals": [], "props": {"model_warning": "model gpt-7 is not in the catalog for codex"}}, "txn_ids": []}
    cli._print_run(detail, sys.stdout)
    assert capsys.readouterr().err.strip() == "warning: model gpt-7 is not in the catalog for codex"
    del detail["run"]["props"]
    cli._print_run(detail, sys.stdout)
    assert "warning" not in capsys.readouterr().err


def test_cli_report_draft_list_and_export(app, client, anon, capsys, tmp_path, monkeypatch):
    from pathlib import Path
    env = env_for(app)
    client.post("/api/projects", json={"slug": "clmi", "title": "CLMI", "kind": "research"})
    assert cli.main(["report", "draft", "--kind", "deck", "--title", "Q1", "--projects", "clmi", "--days", "30", "--no-wait", "--model", MODEL],
                    client=anon, env=env) == 0
    assert capsys.readouterr().out.endswith(" queued\n")
    [run] = client.get("/api/runs", params={"kind": "report"}).json()
    req = run["props"]["request"]
    assert (req["kind"], req["title"], req["projects"]) == ("deck", "Q1", ["clmi"]) and req["start"] and req["end"]
    r = client.post("/api/reports", json={"title": "Memo", "kind": "memo", "source_md": "# Hi"}).json()
    assert cli.main(["report", "list"], client=anon, env=env) == 0
    assert capsys.readouterr().out.splitlines() == [f"{r['id']}  memo  {r['updated_at']}  Memo"]
    out = tmp_path / "memo.html"
    assert cli.main(["report", "export", r["id"], "--format", "html", "--out", str(out)], client=anon, env=env) == 0
    assert "<h1>Hi</h1>" in out.read_text() and capsys.readouterr().out.strip() == f"wrote {out}"
    assert cli.main(["report", "export", r["id"], "--format", "md"], client=anon, env=env) == 0
    assert capsys.readouterr().out == "# Hi"   # md and html go to stdout
    assert cli.main(["report", "export", r["id"], "--format", "pdf"], client=anon, env=env) == 1
    assert "marp-cli not available" in capsys.readouterr().err
    monkeypatch.chdir(tmp_path)
    fake = str(Path(__file__).parent / "bin" / "fake-marp")
    app.state.which = lambda name: fake if name == "marp" else None
    assert cli.main(["report", "export", r["id"], "--format", "pdf"], client=anon, env=env) == 0
    assert capsys.readouterr().out.strip() == f"wrote {r['id']}.pdf"
    assert (tmp_path / f"{r['id']}.pdf").read_text().startswith("FAKE MARP .pdf")
    assert cli.main(["report", "draft", "--kind", "deck", "--title", "Q1", "--projects", "zzz", "--no-wait", "--model", MODEL], client=anon, env=env) == 1
