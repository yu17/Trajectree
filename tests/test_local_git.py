import os
import subprocess
from datetime import datetime, timezone

from trajectree.connectors.local_git import poll_git
from trajectree.models import ProjectIn, ResourceIn
from trajectree.repo.projects import create_project
from trajectree.repo.resources import upsert_resource
from trajectree.repo.resources import list_resources
from trajectree.repo.signals import list_signals
from trajectree.repo.state import get_state

UTC = timezone.utc


def commit(repo, name, when, author_when=None):
    (repo / name).write_text(name)
    env = {
        **os.environ,
        "GIT_AUTHOR_DATE": author_when or when,
        "GIT_COMMITTER_DATE": when,
        "GIT_AUTHOR_NAME": "T",
        "GIT_AUTHOR_EMAIL": "t@x",
        "GIT_COMMITTER_NAME": "T",
        "GIT_COMMITTER_EMAIL": "t@x",
    }
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-q", "-m", f"add {name}"],
        check=True,
        capture_output=True,
        env=env,
    )


def make_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    commit(repo, "a", "2026-01-05T10:00:00+00:00")
    commit(repo, "b", "2026-01-06T09:00:00+00:00")
    return repo


def test_poll_groups_commits_per_day(ledger, human, settings, tmp_path):
    repo = make_repo(tmp_path)
    p = create_project(ledger, human, ProjectIn(slug="a", title="A", kind="research"))
    upsert_resource(ledger, human, ResourceIn(project_id=p.id, kind="git_local", locator=str(repo)))
    r = poll_git(ledger.conn, settings, now=datetime(2026, 1, 7, tzinfo=UTC), since_days=30)
    assert r.repos == 1 and r.signals_written == 2 and r.errors == []
    sigs = list_signals(ledger.conn, project_ids=[p.id], kinds=["commits"])
    assert [s.payload["count"] for s in sigs] == [1, 1] and sigs[0].payload["subjects"] == ["add a"]
    assert sigs[0].dedupe_key.startswith("git:") and sigs[0].dedupe_key.endswith(":2026-01-05")


def test_second_poll_recomputes_whole_day(ledger, human, settings, tmp_path):
    repo = make_repo(tmp_path)
    p = create_project(ledger, human, ProjectIn(slug="a", title="A", kind="research"))
    upsert_resource(ledger, human, ResourceIn(project_id=p.id, kind="git_local", locator=str(repo)))
    poll_git(ledger.conn, settings, now=datetime(2026, 1, 6, 12, tzinfo=UTC), since_days=30)
    commit(repo, "c", "2026-01-06T11:00:00+00:00")
    poll_git(ledger.conn, settings, now=datetime(2026, 1, 6, 13, tzinfo=UTC), since_days=30)
    sigs = list_signals(ledger.conn, project_ids=[p.id], kinds=["commits"])
    assert [s.payload["count"] for s in sigs] == [1, 2]


def test_path_map_and_non_repo_error(ledger, human, settings, tmp_path):
    repo = make_repo(tmp_path)
    p = create_project(ledger, human, ProjectIn(slug="a", title="A", kind="research"))
    upsert_resource(ledger, human, ResourceIn(project_id=p.id, kind="git_local", locator="/host/repo"))
    upsert_resource(ledger, human, ResourceIn(project_id=p.id, kind="git_local", locator=str(tmp_path / "plain")))
    (tmp_path / "plain").mkdir()
    s = settings.model_copy(update={"path_map": [("/host/repo", str(repo))]})
    r = poll_git(ledger.conn, s, now=datetime(2026, 1, 7, tzinfo=UTC), since_days=30)
    assert r.repos == 2 and r.signals_written == 2 and r.errors == [f"{tmp_path / 'plain'}: not a git repository"]


def _single_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    return repo


def _day_keys(ledger, human, settings, repo):
    p = create_project(ledger, human, ProjectIn(slug="a", title="A", kind="research"))
    upsert_resource(ledger, human, ResourceIn(project_id=p.id, kind="git_local", locator=str(repo)))
    poll_git(ledger.conn, settings, now=datetime(2026, 1, 7, tzinfo=UTC), since_days=30)
    sigs = list_signals(ledger.conn, project_ids=[p.id], kinds=["commits"])
    return [s.dedupe_key.rsplit(":", 1)[1] for s in sigs]


def test_grouping_uses_committer_date(ledger, human, settings, tmp_path):
    repo = _single_repo(tmp_path)
    commit(repo, "a", "2026-01-06T10:00:00+00:00", author_when="2026-01-01T10:00:00+00:00")
    assert _day_keys(ledger, human, settings, repo) == ["2026-01-06"]


def test_commit_at_non_utc_offset_lands_on_utc_day(ledger, human, settings, tmp_path):
    repo = _single_repo(tmp_path)
    commit(repo, "a", "2026-01-05T23:30:00-05:00")
    assert _day_keys(ledger, human, settings, repo) == ["2026-01-06"]


def test_failed_repo_is_reported_and_others_still_polled(ledger, human, settings, tmp_path):
    repo = make_repo(tmp_path)
    bad = tmp_path / "bad"
    (bad / ".git").mkdir(parents=True)
    p = create_project(ledger, human, ProjectIn(slug="a", title="A", kind="research"))
    upsert_resource(ledger, human, ResourceIn(project_id=p.id, kind="git_local", locator=str(bad)))
    upsert_resource(ledger, human, ResourceIn(project_id=p.id, kind="git_local", locator=str(repo)))
    r = poll_git(ledger.conn, settings, now=datetime(2026, 1, 7, tzinfo=UTC), since_days=30)
    assert len(r.errors) == 1 and r.errors[0].startswith(str(bad))
    assert r.repos == 2 and r.signals_written == 2
    by_locator = {x.locator: x for x in list_resources(ledger.conn, kinds=["git_local"])}
    assert get_state(ledger.conn, f"git:{by_locator[str(bad)].id}") is None
    assert "last_poll" in get_state(ledger.conn, f"git:{by_locator[str(repo)].id}")


def test_collect_git_runs_without_a_connection(ledger, human, settings, tmp_path):
    from trajectree.connectors.local_git import collect_git
    repo = make_repo(tmp_path)
    p = create_project(ledger, human, ProjectIn(slug="a", title="A", kind="research"))
    upsert_resource(ledger, human, ResourceIn(project_id=p.id, kind="git_local", locator=str(repo)))
    resources = list_resources(ledger.conn, kinds=["git_local"])
    c = collect_git(settings, resources, {resources[0].id: None}, now=datetime(2026, 1, 7, tzinfo=UTC), since_days=30)
    assert len(c.signals) == 2 and c.errors == [] and c.polled == [resources[0].id]
    # nothing was written: the caller writes later, under the lock
    assert list_signals(ledger.conn, project_ids=[p.id]) == [] and get_state(ledger.conn, f"git:{resources[0].id}") is None
