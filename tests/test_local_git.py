import os
import subprocess
from datetime import datetime, timezone

from trajectree.connectors.local_git import poll_git
from trajectree.models import ProjectIn, ResourceIn
from trajectree.repo.projects import create_project
from trajectree.repo.resources import upsert_resource
from trajectree.repo.signals import list_signals

UTC = timezone.utc


def commit(repo, name, when):
    (repo / name).write_text(name)
    env = {
        **os.environ,
        "GIT_AUTHOR_DATE": when,
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
