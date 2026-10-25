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
    assert out.startswith("a  1 stretches  2026-01-05..2026-01-05  1 sessions, 0 commits, 0 file days")
    assert "running" not in out


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
    assert " queued" in out and " ok " in out and "episode" not in out
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
    assert " queued (a)" in out and out.startswith("run ") and "skipped: " in out and "b" in out.split("skipped: ")[1].strip().split(", ")
    assert "ok" not in out
