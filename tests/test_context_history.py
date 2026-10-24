import json
import os
import subprocess
from datetime import date, datetime, timezone

from trajectree.connectors.claude_sessions import project_transcripts
from trajectree.connectors.paths import encode_project_dir
from trajectree.context.history import block_date, collect_history, session_history


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




def _transcript(home, cwd, name, text, *, when, project_dir=None, backend_dir="projects"):
    d = home / backend_dir / (project_dir or encode_project_dir(cwd))
    d.mkdir(parents=True, exist_ok=True)
    rec = {"type": "user", "cwd": cwd, "sessionId": name, "timestamp": when,
           "message": {"role": "user", "content": text}}
    (d / f"{name}.jsonl").write_text(json.dumps(rec) + "\n")


def _with_home(settings, tmp_path):
    home = tmp_path / "claude"
    return home, settings.model_copy(update={"claude_home": home})


def test_session_history_before_start(settings, tmp_path):
    home, st = _with_home(settings, tmp_path)
    # Transcripts far older than the ingest cutoff: no scanner state exists or is needed.
    _transcript(home, "/work/a", "old", "design the parser", when="2020-06-01T10:00:00Z")
    _transcript(home, "/work/a/sub", "oldsub", "fix the lexer", when="2020-07-01T10:00:00Z")
    _transcript(home, "/work/a", "new", "later work", when="2025-03-01T10:00:00Z")
    _transcript(home, "/work/b", "other", "other project", when="2020-06-01T10:00:00Z")
    got = session_history(st, ["/work/a"], ["/work/a", "/work/b"], starts={"claude": date(2025, 1, 1)}, budget_chars=5000)
    assert "design the parser" in got and "fix the lexer" in got
    assert "later work" not in got and "other project" not in got
    assert got.index("fix the lexer") < got.index("design the parser")  # newest first
    # codex did not skip history, so nothing from a codex-less start map leaks in
    assert session_history(st, ["/work/a"], ["/work/a"], starts={"git": date(2025, 1, 1)}, budget_chars=5000) == ""


def test_project_transcripts_prefix_collision(settings, tmp_path):
    home, st = _with_home(settings, tmp_path)
    _transcript(home, "/x/transcriptor", "mine", "in transcriptor", when="2024-01-01T00:00:00Z")
    _transcript(home, "/x/transcriptor-android", "sibling", "in android", when="2024-01-01T00:00:00Z")
    _transcript(home, "/x/transcriptor/android", "nested", "in nested", when="2024-01-01T00:00:00Z")
    registered = ["/x/transcriptor", "/x/transcriptor-android", "/x/transcriptor/android"]
    mine = project_transcripts(st, "/x/transcriptor", registered=registered)
    assert [s.session_id for s in mine] == ["mine"]  # the sibling and the deeper registered folder are not ours
    assert [s.session_id for s in project_transcripts(st, "/x/transcriptor-android", registered=registered)] == ["sibling"]
    # an unregistered sub-folder still belongs to its deepest registered ancestor
    assert [s.session_id for s in project_transcripts(st, "/x/transcriptor", registered=["/x/transcriptor"])
            if s.cwd == "/x/transcriptor/android"] == ["nested"]
    cut = project_transcripts(st, "/x/transcriptor", registered=["/x/transcriptor"],
                              before=datetime(2023, 1, 1, tzinfo=timezone.utc))
    assert cut == []
