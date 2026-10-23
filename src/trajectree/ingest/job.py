"""One ingest pass: transcripts, git, staleness, exports. No LLM calls."""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta

from pydantic import AwareDatetime, BaseModel

from trajectree.config import Settings
from trajectree.connectors.claude_sessions import ScanReport, scan_transcripts, transcript_roots
from trajectree.connectors.folders import (
    FoldersReport,
    collect_folders,
    folders_due,
    read_folder_states,
    write_folders,
)
from trajectree.connectors.local_git import GitReport, collect_git, read_git_states, write_git
from trajectree.connectors.recurrence import MaterializeReport, materialize
from trajectree.db.ledger import Ledger
from trajectree.ingest.exports import render_exports, write_files
from trajectree.repo.resources import list_resources
from trajectree.repo.signals import stale_projects
from trajectree.repo.state import set_state
from trajectree.timeutil import utcnow


class IngestBusy(Exception):
    """Another ingest pass is already running."""


_RUNNING = threading.Lock()


class IngestReport(BaseModel):
    started: AwareDatetime
    duration_ms: int
    scan: ScanReport | None
    git: GitReport
    folders: FoldersReport | None = None
    recurrence: MaterializeReport | None = None
    stale: list[str]
    exports_written: int
    exports_errors: list[str] = []


def run_ingest(ledger: Ledger, settings: Settings, *, now: datetime | None = None) -> IngestReport:
    """Run every connector, then record staleness, live sessions and the report in state.

    Each phase holds the ledger lock on its own, so API reads can interleave between them.
    Only one pass runs at a time (`IngestBusy` otherwise). `ingest:last` is stored even when a
    phase raises, with defaults for the phases that did not finish; the exception is re-raised.
    """
    if not _RUNNING.acquire(blocking=False):
        raise IngestBusy("ingest already running")
    try:
        now = now or utcnow()
        clock = time.monotonic()
        scan: ScanReport | None = None
        git: GitReport | None = None
        folders: FoldersReport | None = None
        recurrence: MaterializeReport | None = None
        stale: list[str] = []
        exports_written = 0
        exports_errors: list[str] = []
        failure: Exception | None = None
        try:
            if transcript_roots(settings):
                with ledger.locked() as conn:
                    scan = scan_transcripts(conn, settings, now=now)
            # git runs between two locked phases, never while the lock is held
            with ledger.locked() as conn:
                git_resources = list_resources(conn, kinds=["git_local"])
                git_states = read_git_states(conn, git_resources)
            collection = collect_git(
                settings, git_resources, git_states, now=now, since_days=settings.scan_since_days
            )
            with ledger.locked() as conn:
                git_written = write_git(conn, collection, now=now)
            git = GitReport(
                repos=len(git_resources), signals_written=git_written, errors=collection.errors
            )
            # folders: same three phases, the walk runs with no lock; skipped until due
            with ledger.locked() as conn:
                due = folders_due(conn, now=now, every_h=settings.folders_scan_every_h)
                folder_resources = list_resources(conn, kinds=["folder"]) if due else []
                folder_states = read_folder_states(conn, folder_resources)
            if due:
                folder_collection = collect_folders(
                    settings, folder_resources, folder_states, now=now, since_days=settings.scan_since_days
                )
                with ledger.locked() as conn:
                    folders_written = write_folders(conn, folder_collection, now=now)
                folders = FoldersReport(
                    folders=len(folder_resources),
                    signals_written=folders_written,
                    errors=folder_collection.errors,
                )
            recurrence = materialize(ledger, now=now)
            with ledger.locked() as conn:
                stale = stale_projects(conn, settled_before=now - timedelta(hours=settings.stale_settle_h))
                with conn:
                    set_state(conn, "live_sessions", [s.model_dump(mode="json") for s in (scan.live if scan else [])])
                    set_state(conn, "stale_projects", stale)
            with ledger.locked() as conn:
                files = render_exports(conn, settings, now=now)
            exports_written, exports_errors = write_files(files)
        except Exception as exc:
            failure = exc
            raise
        finally:
            if git is None:
                git = GitReport(repos=0, signals_written=0, errors=[str(failure)] if failure else [])
            report = IngestReport(
                started=now,
                duration_ms=int((time.monotonic() - clock) * 1000),
                scan=scan,
                git=git,
                folders=folders,
                recurrence=recurrence,
                stale=stale,
                exports_written=exports_written,
                exports_errors=exports_errors,
            )
            with ledger.locked() as conn:
                set_state(conn, "ingest:last", report.model_dump(mode="json"))
        return report
    finally:
        _RUNNING.release()
