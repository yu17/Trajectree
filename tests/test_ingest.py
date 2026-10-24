import asyncio, json
from datetime import datetime, timezone
import pytest
from tests.test_claude_sessions import CWD, SID, rec_assistant, rec_user, write_transcript
from trajectree.ingest.exports import render_summary_md, write_exports
from trajectree.ingest.job import run_ingest
from trajectree.ingest.scheduler import ingest_loop
from trajectree.models import ProjectIn, ProjectPatch, ResourceIn
from trajectree.repo.projects import create_project, get_project, update_project
from trajectree.repo.resources import upsert_resource
from trajectree.repo.state import get_state

UTC = timezone.utc

def test_run_ingest_scans_exports_and_flags_stale(ledger, human, settings, tmp_path):
    home = tmp_path / "claude"
    s = settings.model_copy(update={"claude_home": home})
    p = create_project(ledger, human, ProjectIn(slug="proj", title="Proj", kind="research"))
    upsert_resource(ledger, human, ResourceIn(project_id=p.id, kind="folder", locator=CWD))
    write_transcript(home, CWD, SID, [rec_user("2026-01-05T10:00:00.000Z"), rec_assistant("2026-01-05T10:02:00.000Z")])
    report = run_ingest(ledger, s, now=datetime.now(UTC))
    assert report.scan.signals_written == 1 and report.git.repos == 0
    assert report.stale == [p.id] and report.exports_written == 1
    summary = (s.exports_dir / "projects" / "proj" / "SUMMARY.md").read_text()
    assert summary.startswith("# Proj\n") and "_No summary yet._" in summary
    assert get_state(ledger.conn, "stale_projects") == [p.id] and get_state(ledger.conn, "live_sessions")[0]["session_id"] == SID
    assert get_state(ledger.conn, "ingest:last")["exports_written"] == 1

def test_exports_rewrite_only_on_change(ledger, human, settings):
    p = create_project(ledger, human, ProjectIn(slug="a", title="A", kind="research"))
    now = datetime.now(UTC)
    assert write_exports(ledger.conn, settings, now=now) == (1, [])
    assert write_exports(ledger.conn, settings, now=now) == (0, [])
    update_project(ledger, human, p.id, ProjectPatch(summary_md="Now summarised"))
    assert write_exports(ledger.conn, settings, now=now) == (1, [])
    md = render_summary_md(ledger.conn, get_project(ledger.conn, p.id), now=now)
    assert "## Summary\nNow summarised" in md and "status: active · slug: a · summary updated: " in md

def test_run_ingest_without_roots_still_exports(ledger, human, settings):
    create_project(ledger, human, ProjectIn(slug="a", title="A", kind="research"))
    r = run_ingest(ledger, settings)
    assert r.scan is None and r.exports_written == 1 and r.duration_ms >= 0

@pytest.mark.anyio
async def test_ingest_loop_runs_until_stopped(ledger, settings):
    calls = []
    def fake(ledger_, settings_):
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("boom")   # must not kill the loop
    stop = asyncio.Event()
    task = asyncio.create_task(ingest_loop(ledger, settings, interval_s=0.02, stop=stop, run=fake))
    await asyncio.sleep(0.15)
    stop.set()
    await asyncio.wait_for(task, 1)
    assert len(calls) >= 3

from trajectree.ingest.exports import write_files
from trajectree.ingest.job import IngestBusy
import trajectree.ingest.job as job

def test_ingest_busy_raises(ledger, settings):
    assert job._RUNNING.acquire(blocking=False)
    try:
        with pytest.raises(IngestBusy):
            run_ingest(ledger, settings)
    finally:
        job._RUNNING.release()

def test_ingest_stores_last_even_when_a_phase_fails(ledger, human, settings, monkeypatch):
    create_project(ledger, human, ProjectIn(slug="a", title="A", kind="research"))
    def boom(*a, **k):
        raise RuntimeError("git exploded")
    monkeypatch.setattr(job, "collect_git", boom)
    with pytest.raises(RuntimeError):
        run_ingest(ledger, settings)
    assert "git exploded" in " ".join(get_state(ledger.conn, "ingest:last")["git"]["errors"])

def test_write_files_is_atomic_and_isolates_errors(tmp_path):
    good = tmp_path / "ok" / "SUMMARY.md"
    bad = tmp_path / "blocked" / "SUMMARY.md"
    bad.parent.mkdir()
    bad.parent.chmod(0o500)
    try:
        written, errors = write_files([(good, "hello\n"), (bad, "x\n")])
    finally:
        bad.parent.chmod(0o700)
    assert written == 1 and good.read_text() == "hello\n" and len(errors) == 1 and str(bad) in errors[0]
    assert not list(good.parent.glob("*.tmp"))

def test_summary_md_excludes_future_events(ledger, human, settings):
    from trajectree.models import EventIn
    from trajectree.repo.events import create_event
    p = create_project(ledger, human, ProjectIn(slug="a", title="A", kind="research"))
    create_event(ledger, human, EventIn(project_id=p.id, kind="note", title="Past", t_start=datetime(2026, 1, 1, tzinfo=UTC)))
    create_event(ledger, human, EventIn(project_id=p.id, kind="milestone", title="Future", t_start=datetime(2027, 1, 1, tzinfo=UTC), status="planned"))
    md = render_summary_md(ledger.conn, get_project(ledger.conn, p.id), now=datetime(2026, 6, 1, tzinfo=UTC))
    assert "Past" in md and "Future" not in md


def test_ingest_runs_folders_and_recurrence(ledger, human, settings, tmp_path):
    from trajectree.repo.events import list_events
    root = tmp_path / "work"
    root.mkdir()
    (root / "a.txt").write_text("x")
    p = create_project(ledger, human, ProjectIn(slug="gm", title="GM", kind="routine", recurrence="FREQ=WEEKLY;BYDAY=TU;BYHOUR=10;BYMINUTE=0"))
    upsert_resource(ledger, human, ResourceIn(project_id=p.id, kind="folder", locator=str(root)))
    now = datetime(2026, 10, 21, 12, tzinfo=UTC)
    import os
    os.utime(root / "a.txt", (now.timestamp(), now.timestamp()))
    report = run_ingest(ledger, settings, now=now)
    assert report.folders.folders == 1 and report.folders.signals_written == 1 and report.folders.errors == []
    assert report.recurrence.created == 8 and len(list_events(ledger.conn, project_ids=[p.id])) == 8
    second = run_ingest(ledger, settings, now=now)
    assert second.folders is None and second.recurrence.created == 0


def test_ingest_discovers_remote_as_pending_resource(ledger, human, settings, tmp_path):
    import subprocess
    from trajectree.repo.resources import find_resource

    repo = tmp_path / "r"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", "git@github.com:o/r.git"], check=True)
    p = create_project(ledger, human, ProjectIn(slug="a", title="A", kind="research"))
    upsert_resource(ledger, human, ResourceIn(project_id=p.id, kind="git_local", locator=str(repo)))
    settings.remote_every_m = 0
    report = run_ingest(ledger, settings, now=datetime(2026, 10, 22, 12, tzinfo=UTC))
    assert report.remote is None
    found = find_resource(ledger.conn, p.id, "git_remote", "https://github.com/o/r")
    assert found is not None and found.title == "o/r"
    pending = [t for t in ledger.list_txns(review="pending") if t.intent.startswith("discovered git remote")]
    assert len(pending) == 1 and pending[0].intent == "discovered git remote https://github.com/o/r"
    run_ingest(ledger, settings, now=datetime(2026, 10, 22, 13, tzinfo=UTC))
    assert len([t for t in ledger.list_txns(review="pending") if t.intent.startswith("discovered git remote")]) == 1


def _discover_setup(ledger, human, tmp_path):
    import subprocess

    repo = tmp_path / "r"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", "https://evil.example/a/b"], check=True)
    p = create_project(ledger, human, ProjectIn(slug="a", title="A", kind="research"))
    upsert_resource(ledger, human, ResourceIn(project_id=p.id, kind="git_local", locator=str(repo)))
    return p


def test_ingest_polls_discovered_remote_only_after_accept(ledger, human, settings, tmp_path, monkeypatch):
    import httpx
    import trajectree.connectors.remote as remote
    from trajectree.runner.credentials import store_connector_token

    _discover_setup(ledger, human, tmp_path)
    store_connector_token(settings, "gitlab", "glpat-secret")
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=[])

    real = remote.collect_remote
    monkeypatch.setattr(
        job, "collect_remote",
        lambda *a, **k: real(*a, **k, client=httpx.Client(transport=httpx.MockTransport(handler))),
    )
    settings.remote_every_m = 1
    first = run_ingest(ledger, settings, now=datetime(2026, 10, 22, 12, tzinfo=UTC))
    assert first.remote.repos == 0 and requests == []
    txn = [t for t in ledger.list_txns(review="pending") if t.intent.startswith("discovered git remote")][0]
    ledger.set_review(txn.id, "accepted", human)
    second = run_ingest(ledger, settings, now=datetime(2026, 10, 22, 13, tzinfo=UTC))
    # accepted, but its host is not TRAJECTREE_GITLAB_HOST: listed, never polled, no token sent
    assert second.remote.repos == 1 and requests == []
    assert second.remote.errors == ["https://evil.example/a/b: unknown host, not polled"]
    settings.gitlab_host = "evil.example"
    third = run_ingest(ledger, settings, now=datetime(2026, 10, 22, 14, tzinfo=UTC))
    assert third.remote.errors == [] and requests and requests[0].url.host == "evil.example"
    assert requests[0].headers["PRIVATE-TOKEN"] == "glpat-secret"


def test_rejected_discovery_is_not_suggested_again(ledger, human, settings, tmp_path):
    p = _discover_setup(ledger, human, tmp_path)
    settings.remote_every_m = 0
    run_ingest(ledger, settings, now=datetime(2026, 10, 22, 12, tzinfo=UTC))
    txn = [t for t in ledger.list_txns(review="pending") if t.intent.startswith("discovered git remote")][0]
    ledger.set_review(txn.id, "rejected", human)
    run_ingest(ledger, settings, now=datetime(2026, 10, 22, 13, tzinfo=UTC))
    assert [t for t in ledger.list_txns(review="pending") if t.intent.startswith("discovered git remote")] == []
    assert get_state(ledger.conn, "remote:suggested") == {p.id: ["https://evil.example/a/b"]}


def test_ingest_remote_phase_runs_when_due(ledger, human, settings, monkeypatch):
    import trajectree.connectors.remote as remote

    p = create_project(ledger, human, ProjectIn(slug="a", title="A", kind="research"))
    upsert_resource(ledger, human, ResourceIn(project_id=p.id, kind="git_remote", locator="https://github.com/o/r"))
    calls = []

    def fake_collect(settings, resources, states, *, now, starts, client=None):
        calls.append([r.locator for r in resources])
        return remote.RemoteCollection(signals=[], releases=[], polled=[resources[0].id], errors=["boom"])

    monkeypatch.setattr(job, "collect_remote", fake_collect)
    now = datetime(2026, 10, 22, 12, tzinfo=UTC)
    first = run_ingest(ledger, settings, now=now)
    assert first.remote.repos == 1 and first.remote.errors == ["boom"] and calls == [["https://github.com/o/r"]]
    assert run_ingest(ledger, settings, now=now).remote is None and len(calls) == 1


def test_ingest_slurm_phase_when_configured(ledger, human, settings, monkeypatch):
    from pathlib import Path

    fixtures = Path(__file__).parent / "fixtures"
    p = create_project(ledger, human, ProjectIn(slug="proj", title="Proj", kind="research"))
    upsert_resource(ledger, human, ResourceIn(project_id=p.id, kind="folder", locator="/home/u/proj"))
    monkeypatch.setenv("FAKE_SSH_SQUEUE", str(fixtures / "squeue.json"))
    monkeypatch.setenv("FAKE_SSH_SACCT", str(fixtures / "sacct.json"))
    now = datetime(2026, 1, 6, 12, tzinfo=UTC)
    assert run_ingest(ledger, settings, now=now).slurm is None  # not configured
    s = settings.model_copy(
        update={"slurm_host": "login.hpc", "ssh_bin": str(Path(__file__).parent / "bin" / "fake-ssh")}
    )
    first = run_ingest(ledger, s, now=now)
    assert first.slurm.jobs == 3 and first.slurm.resources == 2 and first.slurm.errors == []
    assert get_state(ledger.conn, "ingest:last")["slurm"]["jobs"] == 3
    assert run_ingest(ledger, s, now=now).slurm is None  # not due yet


def test_ingest_slurm_window_is_incremental(ledger, human, settings, tmp_path, monkeypatch):
    from datetime import timedelta
    from pathlib import Path

    argv_file = tmp_path / "argv"
    monkeypatch.setenv("FAKE_SSH_ARGV_FILE", str(argv_file))
    s = settings.model_copy(
        update={"slurm_host": "login.hpc", "ssh_bin": str(Path(__file__).parent / "bin" / "fake-ssh")}
    )
    now = datetime(2026, 1, 6, 12, tzinfo=UTC)

    def since_arg():
        argv = json.loads(argv_file.read_text())  # the last command run is sacct
        return argv[argv.index("-S") + 1]

    monkeypatch.setenv("FAKE_SSH_EXIT", "255")  # sacct fails: window must not advance
    assert run_ingest(ledger, s, now=now).slurm.errors
    assert get_state(ledger.conn, "slurm:last") is not None and get_state(ledger.conn, "slurm:last_ok") is None
    assert since_arg() == (now - timedelta(days=s.scan_since_days)).strftime("%Y-%m-%dT%H:%M:%S")
    monkeypatch.delenv("FAKE_SSH_EXIT")
    later = now + timedelta(minutes=20)
    assert run_ingest(ledger, s, now=later).slurm.errors == []
    assert since_arg() == (now - timedelta(days=s.scan_since_days) + timedelta(minutes=20)).strftime("%Y-%m-%dT%H:%M:%S")
    assert get_state(ledger.conn, "slurm:last_ok")["at"]
    third = later + timedelta(minutes=20)
    run_ingest(ledger, s, now=third)
    assert since_arg() == (later - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%S")
