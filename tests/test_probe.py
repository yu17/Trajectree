import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from trajectree.connectors import probe as probe_mod
from trajectree.connectors.paths import encode_project_dir
from trajectree.connectors.probe import ProbeState, probe_folder, probe_state_reader
from trajectree.importing import ImportRecord, SourceChoice
from trajectree.models import ProjectIn, ProjectPatch, ResourceIn
from trajectree.repo.projects import create_project, update_project
from trajectree.repo.resources import upsert_resource
from trajectree.repo.state import set_state

UTC = timezone.utc
NOW = datetime(2026, 10, 24, 12, tzinfo=UTC)


def nothing(path):
    return ProbeState()


def by_source(report):
    return {s.source: s for s in report.sources}


def git(repo, *args, when=None):
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x"}
    if when:
        env["GIT_COMMITTER_DATE"] = env["GIT_AUTHOR_DATE"] = when
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, env=env)


def make_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    git(repo, "commit", "--allow-empty", "-m", "one", when="2025-03-01T10:00:00+00:00")
    git(repo, "commit", "--allow-empty", "-m", "two", when="2026-02-01T10:00:00+00:00")
    return repo


def write_transcript(home: Path, dir_for: str, sid: str, cwd: str, ts: str):
    d = home / "projects" / encode_project_dir(dir_for)
    d.mkdir(parents=True, exist_ok=True)
    rec = {"type": "user", "message": {"role": "user", "content": "hi"}, "timestamp": ts, "cwd": cwd, "sessionId": sid}
    (d / f"{sid}.jsonl").write_text(json.dumps(rec) + "\n")


def test_git_counts_and_oldest(settings, tmp_path):
    repo = make_repo(tmp_path)
    git_probe = by_source(probe_folder(nothing, settings, str(repo), now=NOW))["git"]
    assert git_probe.count == 2 and git_probe.oldest == datetime(2025, 3, 1, 10, tzinfo=UTC)
    assert by_source(probe_folder(nothing, settings, str(tmp_path), now=NOW))["git"].detail == "not a git repository"


def test_sources_order_default_since_and_codex(settings, tmp_path):
    report = probe_folder(nothing, settings, str(tmp_path), now=NOW)
    assert [s.source for s in report.sources] == ["git", "claude", "codex", "folder"]
    codex = by_source(report)["codex"]
    assert (codex.count, codex.detail) == (0, "not connected")
    assert report.default_since.isoformat() == "2025-10-24" and report.path == str(tmp_path)


def test_claude_sessions_by_cwd(settings, tmp_path):
    home = tmp_path / "claude"
    folder = "/home/u/app"
    write_transcript(home, folder, "s1", folder, "2026-01-05T10:00:00Z")
    write_transcript(home, folder, "s2", folder + "/sub", "2026-01-03T10:00:00Z")
    write_transcript(home, folder, "s3", "/elsewhere", "2026-01-02T10:00:00Z")
    s = settings.model_copy(update={"claude_home": home})
    claude = by_source(probe_folder(nothing, s, folder, now=NOW))["claude"]
    assert claude.count == 2 and claude.oldest == datetime(2026, 1, 3, 10, tzinfo=UTC) and claude.detail == ""


def test_claude_count_is_a_lower_bound_when_capped(settings, tmp_path, monkeypatch):
    home = tmp_path / "claude"
    for i in range(3):
        write_transcript(home, "/home/u/app", f"s{i}", "/home/u/app", f"2026-01-0{i + 1}T10:00:00Z")
    real = probe_mod.project_transcripts
    monkeypatch.setattr(probe_mod, "project_transcripts", lambda *a, **k: real(*a, **k, max_files=2))
    s = settings.model_copy(update={"claude_home": home})
    claude = by_source(probe_folder(nothing, s, "/home/u/app", now=NOW))["claude"]
    assert claude.count == 2 and claude.detail == "at least 2 sessions"


def test_prefix_collision(settings, tmp_path):
    home = tmp_path / "claude"
    write_transcript(home, "/x/transcriptor-android", "a1", "/x/transcriptor-android", "2026-01-05T10:00:00Z")
    s = settings.model_copy(update={"claude_home": home})
    registered = ProbeState(registered=["/x/transcriptor", "/x/transcriptor-android"])
    first = by_source(probe_folder(lambda p: registered, s, "/x/transcriptor", now=NOW))["claude"]
    second = by_source(probe_folder(lambda p: registered, s, "/x/transcriptor-android", now=NOW))["claude"]
    assert first.count == 0 and second.count == 1


def test_folder_count_respects_skips(settings, tmp_path):
    root = tmp_path / "work"
    (root / "node_modules").mkdir(parents=True)
    (root / "node_modules" / "junk.js").write_text("x")
    (root / "ignored").mkdir()
    (root / "ignored" / "i.txt").write_text("x")
    (root / ".gitignore").write_text("ignored/\n")
    (root / "a.txt").write_text("x")
    (root / "sub").mkdir()
    (root / "sub" / "b.txt").write_text("x")
    old = datetime(2025, 5, 5, 8, tzinfo=UTC).timestamp()
    os.utime(root / "a.txt", (old, old))
    folder = by_source(probe_folder(nothing, settings, str(root), now=NOW))["folder"]
    assert folder.count == 3  # a.txt, sub/b.txt, .gitignore
    assert folder.oldest == datetime(2025, 5, 5, 8, tzinfo=UTC) and folder.detail == ""


def test_folder_walk_stops_at_the_cap(settings, tmp_path, monkeypatch):
    root = tmp_path / "work"
    root.mkdir()
    for i in range(10):
        (root / f"f{i}.txt").write_text("x")
    monkeypatch.setattr(probe_mod, "FOLDER_PROBE_MAX_FILES", 4)
    folder = by_source(probe_folder(nothing, settings, str(root), now=NOW))["folder"]
    assert folder.detail == "at least 4 files" and folder.count <= 4


def test_remotes_listed_with_declined_flag(settings, tmp_path):
    repo = make_repo(tmp_path)
    git(repo, "remote", "add", "origin", "git@github.com:o/r.git")
    git(repo, "remote", "add", "other", "https://github.com/o/s.git")
    state = ProbeState(declined=["https://github.com/o/s"])
    remotes = probe_folder(lambda p: state, settings, str(repo), now=NOW).remotes
    assert {(r.url, r.title, r.declined) for r in remotes} == {
        ("https://github.com/o/r", "o/r", False),
        ("https://github.com/o/s", "o/s", True),
    }


def test_imported_flag_from_record(ledger, human, settings, tmp_path):
    p = create_project(ledger, human, ProjectIn(slug="a", title="A", kind="research"))
    upsert_resource(ledger, human, ResourceIn(project_id=p.id, kind="folder", locator=str(tmp_path)))
    record = ImportRecord(
        sources={"git": SourceChoice(picked=True), "claude": SourceChoice(picked=False)}, recorded_at=NOW
    )
    update_project(ledger, human, p.id, ProjectPatch(props={"import": record.model_dump(mode="json")}))
    report = probe_folder(probe_state_reader(ledger), settings, str(tmp_path), now=NOW)
    assert {s.source: s.imported for s in report.sources} == {
        "git": True, "claude": False, "codex": False, "folder": False,
    }


def test_state_reader_reports_declined_remotes(ledger, human, tmp_path):
    p = create_project(ledger, human, ProjectIn(slug="a", title="A", kind="research"))
    upsert_resource(ledger, human, ResourceIn(project_id=p.id, kind="folder", locator=str(tmp_path)))
    upsert_resource(ledger, human, ResourceIn(project_id=p.id, kind="git_remote", locator="https://github.com/o/a"))
    with ledger.locked() as conn:
        set_state(conn, "remote:suggested", {p.id: ["https://github.com/o/a", "https://github.com/o/b"]})
    state = probe_state_reader(ledger)(str(tmp_path))
    assert state.declined == ["https://github.com/o/b"] and state.project.id == p.id
    assert state.registered == [str(tmp_path)]
