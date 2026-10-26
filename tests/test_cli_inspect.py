import json

import pytest

from tests.test_catalog import MODEL
from tests.test_cli import env_for
from tests.test_run_service import _at, _seg, _render_size, _stretches
from trajectree.cli import main as cli
from trajectree.models import ProjectIn, ResourceIn
from trajectree.repo.projects import create_project
from trajectree.repo.resources import upsert_resource
from trajectree.repo.signals import upsert_signals
from trajectree.runner.credentials import store_oauth_token

# One answer that satisfies both the boundary pass (episodes) and an episode run (proposals).
SERIES_ANSWER = json.dumps({
    "answer": "done",
    "proposals": [],
    "episodes": [
        {"start": "2026-01-05", "end": "2026-01-10", "label": "first"},
        {"start": "2026-01-20", "end": "2026-01-20", "label": "second"},
    ],
})
SIMPLE_ANSWER = json.dumps({"answer": "done", "proposals": []})


@pytest.fixture(autouse=True)
def fast(monkeypatch, settings):
    monkeypatch.setattr(cli, "_sleep", lambda s: None)
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ok")
    monkeypatch.setenv("FAKE_CLAUDE_STRUCTURED", SIMPLE_ANSWER)
    store_oauth_token(settings, "fake")


def _project(ledger, human, tmp_path, slug, signals):
    p = create_project(ledger, human, ProjectIn(slug=slug, title=slug.upper(), kind="research"))
    folder = tmp_path / f"proj-{slug}"
    folder.mkdir()
    upsert_resource(ledger, human, ResourceIn(project_id=p.id, kind="folder", locator=str(folder)))
    with ledger.locked() as conn:
        upsert_signals(conn, [_seg(p.id, *s) for s in signals])
    return p


def test_cli_inspections_lists_due_projects(app, ledger, human, tmp_path, anon, capsys):
    assert cli.main(["inspections"], client=anon, env=env_for(app)) == 0
    assert capsys.readouterr().out.strip() == "nothing to inspect"
    _project(ledger, human, tmp_path, "a", [(5, 10, 11, "s1")])
    assert cli.main(["inspections"], client=anon, env=env_for(app)) == 0
    out = capsys.readouterr().out
    assert out.startswith("a  1 stretches  2026-01-05..2026-01-05  1 segments, 0 commits, 0 file days")
    assert "running" not in out


def test_cli_inspections_names_a_boundary_pass(capsys):
    row = {"slug": "a", "stretches": 3, "first_day": "2026-01-05", "last_day": "2026-01-20", "sessions": 4,
           "commits": 0, "file_days": 0, "active_run": "r9", "episode": None, "episodes": None}
    cli._cmd_inspections(None, _FakeApi(None, [row]))
    assert capsys.readouterr().out.strip().endswith("running boundary pass (r9)")
    cli._cmd_inspections(None, _FakeApi(None, [{**row, "episode": 2, "episodes": 3}]))
    assert capsys.readouterr().out.strip().endswith("running episode 2/3 (r9)")


def test_cli_follows_a_series_while_the_project_is_live(app, ledger, human, settings, tmp_path, monkeypatch, anon, capsys):
    from trajectree.repo.sessions import start_session
    from trajectree.timeutil import utcnow
    p = _project(ledger, human, tmp_path, "a", [(5, 10, 11, "s1"), (5, 12, 13, "s2"), (10, 9, 10, "s3"), (20, 14, 15, "s4")])
    st1, st2, st3 = _stretches(ledger, settings, p)
    settings.inspect_budget_chars = max(_render_size(ledger, settings, [st1, st2]), _render_size(ledger, settings, [st3]))
    monkeypatch.setenv("FAKE_CLAUDE_STRUCTURED", SERIES_ANSWER)
    real_inspect = app.state.runs.inspect
    def inspect_then_go_live(request):
        run = real_inspect(request)
        # The owner opens a session in the project right after starting the inspection.
        with ledger.locked() as conn:
            start_session(conn, session_id="x", cwd="/w/a", transcript_path=None, project_id=p.id, source="hook", now=utcnow())
        return run
    monkeypatch.setattr(app.state.runs, "inspect", inspect_then_go_live)
    assert cli.main(["inspect", "a", "--model", MODEL], client=anon, env=env_for(app)) == 0
    out = capsys.readouterr().out
    assert "episode 1/2" in out and "episode 2/2" in out and out.count(" ok ") == 3


def test_cli_inspect_posts_get_a_long_http_timeout(monkeypatch):
    seen = []

    class Recording:
        def __init__(self, timeout):
            self.default = timeout

        def get(self, url, **kw):
            body = [{"id": "p1", "slug": "a"}]
            return type("R", (), {"status_code": 200, "json": lambda self: body})()

        def post(self, url, **kw):
            seen.append((url.rsplit("/api", 1)[1], kw.get("timeout", self.default)))
            body = {"runs": [], "skipped": []} if url.endswith("/api/inspect") else {"id": "r1", "kind": "inspect", "props": {}}
            return type("R", (), {"status_code": 200, "json": lambda self: body})()

        def close(self):
            pass

    monkeypatch.setattr(cli.httpx, "Client", Recording)
    env = {"TRAJECTREE_TOKEN": "t", "TRAJECTREE_URL": "http://127.0.0.1:1"}
    assert cli.main(["inspect", "a", "--model", MODEL, "--no-wait"], env=env) == 0
    assert cli.main(["inspect", "--all", "--model", MODEL], env=env) == 0
    assert [path for path, _ in seen] == ["/projects/p1/inspect", "/inspect"]
    assert all(timeout >= 120 for _, timeout in seen)


def test_cli_inspect_runs_and_follows_a_series(app, ledger, human, settings, tmp_path, monkeypatch, anon, capsys):
    p = _project(ledger, human, tmp_path, "a", [(5, 10, 11, "s1"), (5, 12, 13, "s2"), (10, 9, 10, "s3"), (20, 14, 15, "s4")])
    st1, st2, st3 = _stretches(ledger, settings, p)
    settings.inspect_budget_chars = max(_render_size(ledger, settings, [st1, st2]), _render_size(ledger, settings, [st3]))
    # The fake returns the same answer for the boundary pass and the episodes, so it carries both.
    monkeypatch.setenv("FAKE_CLAUDE_STRUCTURED", SERIES_ANSWER)
    assert cli.main(["inspect", "a", "--model", MODEL], client=anon, env=env_for(app)) == 0
    out = capsys.readouterr().out
    assert "episode 1/2" in out and "episode 2/2" in out
    assert out.index("episode 1/2") < out.index("episode 2/2")
    assert out.count(" queued") == 1 and out.count(" ok ") == 3


def test_cli_inspect_single_run_and_refusals(app, ledger, human, tmp_path, anon, capsys):
    _project(ledger, human, tmp_path, "a", [(5, 10, 11, "s1")])
    assert cli.main(["inspect", "a", "--model", MODEL], client=anon, env=env_for(app)) == 0
    out = capsys.readouterr().out
    assert " queued" in out and " ok " in out and "episode 1/1" in out
    # Nothing is due any more: the daemon's 409 comes back as the error.
    assert cli.main(["inspect", "a", "--model", MODEL], client=anon, env=env_for(app)) == 1
    assert "nothing to inspect for a" in capsys.readouterr().err
    assert cli.main(["inspect", "--model", MODEL], client=anon, env=env_for(app)) == 2
    assert cli.main(["inspect", "a", "--all", "--model", MODEL], client=anon, env=env_for(app)) == 2


def test_cli_inspect_requires_a_model(app, anon, capsys):
    assert cli.main(["inspect", "a"], client=anon, env=env_for(app)) == 2
    assert "no model or runner was chosen" in capsys.readouterr().err


def test_cli_inspect_all_prints_queued_and_skipped(app, ledger, human, tmp_path, monkeypatch, anon, capsys):
    monkeypatch.setattr(app.state.runs, "_next_queued", lambda slot="interactive": None)
    _project(ledger, human, tmp_path, "a", [(5, 10, 11, "s1")])
    create_project(ledger, human, ProjectIn(slug="b", title="B", kind="research"))
    assert cli.main(["inspect", "--all", "--model", MODEL], client=anon, env=env_for(app)) == 0
    out = capsys.readouterr().out
    # b has nothing due, so `all` does not try it: nothing is skipped
    assert " queued (a)" in out and out.startswith("run ") and "skipped" not in out
    assert "ok" not in out
    # a second pass finds a running: it is skipped
    assert cli.main(["inspect", "--all", "--model", MODEL], client=anon, env=env_for(app)) == 0
    assert capsys.readouterr().out.strip() == "skipped: a"


class _FakeApi:
    def __init__(self, run, due, series=()):
        self.run, self.due, self.series, self.calls = run, due, list(series), 0

    def get(self, path):
        self.calls += 1
        if path == "/api/inspections":
            return self.due
        if path.startswith("/api/runs?series_id="):
            return self.series
        return {"run": self.run, "txn_ids": []}


def _finished(**props):
    return {"id": "r1", "kind": "inspect", "status": "ok", "props": {"episode_no": 1, "episodes": 2, "series_id": "S", **props}}


def test_cli_finds_the_next_run_by_series_not_by_project():
    nxt = {"id": "r2", "kind": "inspect", "status": "queued", "props": {"series_id": "S", "episode_no": 2}}
    # Nothing is listed as due (the project went live), yet the series' next run is found.
    api = _FakeApi(_finished(), [], series=[nxt, _finished()])
    assert cli._next_in_series(api, "a", _finished()) == "r2"


def test_cli_inspect_reports_a_stopped_series():
    api = _FakeApi(_finished(series_stopped="the model is paused"), [])
    with pytest.raises(cli._Fail) as exc:
        cli._next_in_series(api, "a", _finished())
    assert exc.value.code == 1 and str(exc.value) == "series stopped: the model is paused"
    assert api.calls == 1  # no waiting out the bound


def test_cli_inspect_stops_when_no_next_run_is_queued():
    api = _FakeApi(_finished(), [])
    with pytest.raises(cli._Fail) as exc:
        cli._next_in_series(api, "a", _finished())
    assert exc.value.code == 1 and str(exc.value) == "series stopped: no next run was queued for a"


def test_cli_inspect_stops_when_the_boundary_pass_fails_validation(capsys):
    failed = {"id": "r1", "kind": "inspect_boundaries", "status": "failed", "est_usd": 0, "turns": 1,
              "duration_ms": 1, "error": "boundaries invalid: no episodes", "props": {}}
    api = _FakeApi(failed, [])
    with pytest.raises(cli._Fail) as exc:
        cli._next_in_series(api, "a", {**failed, "status": "ok"})
    assert exc.value.code == 1
    assert "boundaries invalid" in capsys.readouterr().err
