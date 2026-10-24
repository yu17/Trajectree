import json
import os
import subprocess
from datetime import date

from trajectree.connectors.claude_sessions import SessionSummary
from trajectree.context.history import block_date, collect_history, session_history
from trajectree.models import ProjectIn, ResourceIn
from trajectree.repo.projects import create_project
from trajectree.repo.resources import upsert_resource
from trajectree.repo.state import set_state


def _git(repo, *args, when=None):
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t"}
    if when:
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = when
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, env=env)


def _repo(tmp_path, dates):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    for i, when in enumerate(dates):
        _git(repo, "commit", "-q", "--allow-empty", "-m", f"commit {i} at {when[:7]}", when=when)
    return repo


def test_block_date_is_latest():
    assert block_date({"git": date(2025, 1, 1), "claude": date(2026, 1, 1)}) == date(2026, 1, 1)


def test_git_before_start_only(tmp_path):
    repo = _repo(tmp_path, ["2024-06-10T12:00:00+00:00", "2025-03-10T12:00:00+00:00"])
    got = collect_history(None, folders=[repo], starts={"git": date(2025, 1, 1)}, tz="UTC", budget_chars=5000)
    assert "commit 0 at 2024-06" in got.git_md and "2025-03" not in got.git_md
    assert got.truncated is False and got.folder_md == ""


def test_large_history_is_condensed_by_month(tmp_path):
    dates = [f"{2023 + (i // 12) % 2}-{i % 12 + 1:02d}-{(i % 27) + 1:02d}T12:00:00+00:00" for i in range(600)]
    repo = _repo(tmp_path, dates)
    got = collect_history(None, folders=[repo], starts={"git": date(2026, 1, 1)}, tz="UTC", budget_chars=3000)
    assert got.truncated is True and len(got.git_md) <= 3000
    assert "2023-01: 25 commits" in got.git_md and "2024-12: 25 commits" in got.git_md
    assert len(got.git_md.splitlines()) == 24


def test_folder_snapshot_lists_top_level(tmp_path):
    root = tmp_path / "proj"
    (root / "src").mkdir(parents=True)
    (root / "node_modules").mkdir()
    (root / "node_modules" / "junk.js").write_text("x" * 10)
    (root / "src" / "a.py").write_text("x" * 2048)
    (root / "README.md").write_text("hello")
    old = date(2024, 5, 1)
    stamp = 1714521600  # 2024-05-01 UTC
    for p in (root / "src" / "a.py", root / "README.md", root / "node_modules" / "junk.js"):
        os.utime(p, (stamp, stamp))
    new = root / "src" / "new.py"
    new.write_text("fresh")  # modified after the start: not part of the starting point
    got = collect_history(None, folders=[root], starts={"folder": date(2025, 1, 1)}, tz="UTC", budget_chars=4000)
    assert "src/ (1 files): 2.0 KB, newest 2024-05-01" in got.folder_md
    assert "README.md: 5 B, newest 2024-05-01" in got.folder_md
    assert "node_modules" not in got.folder_md
    assert old.isoformat() in got.folder_md


def _scan(conn, path, *, session_id, cwd, t_end, backend="claude", title=None):
    summary = SessionSummary(session_id=session_id, cwd=cwd, t_end=t_end, title=title, transcript_path=str(path),
                             backend=backend, prompts=2)
    set_state(conn, f"scan:{path}", {"offset": 0, "size": 0, "mtime": 0, "summary": summary.model_dump(mode="json")})


def _transcript(path, text):
    path.write_text(json.dumps({"type": "user", "message": {"role": "user", "content": text}}) + "\n")


def test_session_history_before_start(ledger, human, settings, tmp_path):
    proj = create_project(ledger, human, ProjectIn(slug="a", title="A", kind="research"))
    other = create_project(ledger, human, ProjectIn(slug="b", title="B", kind="research"))
    upsert_resource(ledger, human, ResourceIn(project_id=proj.id, kind="folder", locator="/work/a"))
    upsert_resource(ledger, human, ResourceIn(project_id=other.id, kind="folder", locator="/work/b"))
    old, new, foreign, codex = (tmp_path / f"{n}.jsonl" for n in ("old", "new", "foreign", "codex"))
    _transcript(old, "design the parser")
    _transcript(new, "later work")
    _transcript(foreign, "other project")
    _transcript(codex, "codex work")
    _scan(ledger.conn, old, session_id="old", cwd="/work/a", t_end="2024-06-01T10:00:00Z", title="Old session")
    _scan(ledger.conn, new, session_id="new", cwd="/work/a", t_end="2025-03-01T10:00:00Z", title="New session")
    _scan(ledger.conn, foreign, session_id="f", cwd="/work/b", t_end="2024-06-01T10:00:00Z", title="Foreign")
    _scan(ledger.conn, codex, session_id="c", cwd="/work/a", t_end="2024-06-02T10:00:00Z", backend="codex",
          title="Codex one")
    got = session_history(ledger.conn, settings, proj, starts={"claude": date(2025, 1, 1)}, budget_chars=5000)
    assert "Old session" in got and "design the parser" in got
    assert "New session" not in got and "Foreign" not in got and "Codex one" not in got  # codex did not skip
