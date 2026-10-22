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
