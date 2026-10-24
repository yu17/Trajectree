"""One ingest pass: transcripts, git, staleness, exports. No LLM calls."""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta

from urllib.parse import urlparse

import apsw
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
from trajectree.connectors.local_git import (
    GitReport,
    collect_git,
    discover_remotes,
    read_git_states,
    write_git,
)
from trajectree.connectors.recurrence import MaterializeReport, materialize
from trajectree.connectors.remote import (
    RemoteReport,
    collect_remote,
    read_remote_states,
    remote_due,
    write_remote,
)
from trajectree.connectors.slurm import SlurmReport, collect_slurm, slurm_due, slurm_since, write_slurm
from trajectree.db.ledger import Ledger
from trajectree.importing import source_picked, source_start
from trajectree.ingest.exports import render_exports, write_files
from trajectree.models import Actor, Resource, ResourceIn
from trajectree.repo.projects import list_projects
from trajectree.repo.resources import find_resource, list_resources, upsert_resource
from trajectree.repo.signals import stale_projects
from trajectree.repo.review import pending_txns
from trajectree.repo.state import get_state, set_state
from trajectree.timeutil import utcnow


class IngestBusy(Exception):
    """Another ingest pass is already running."""


_RUNNING = threading.Lock()
DISCOVERY_ACTOR = Actor(kind="connector", ref="local-git", tool="git")


def _chosen(
    conn: apsw.Connection, settings: Settings, resources: list[Resource], source: str, now: datetime
) -> tuple[list[Resource], dict[str, datetime | None]]:
    """The resources whose project picked `source`, and where each one's first poll starts."""
    projects = {p.id: p for p in list_projects(conn)}
    kept = [r for r in resources if r.project_id in projects and source_picked(projects[r.project_id], source)]
    starts = {
        r.id: source_start(
            projects[r.project_id], source, now=now, default_days=settings.scan_since_days, tz=settings.tz
        )
        for r in kept
    }
    return kept, starts


class IngestReport(BaseModel):
    started: AwareDatetime
    duration_ms: int
    scan: ScanReport | None
    git: GitReport
    folders: FoldersReport | None = None
    remote: RemoteReport | None = None
    slurm: SlurmReport | None = None
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
        remote: RemoteReport | None = None
        slurm: SlurmReport | None = None
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
                git_resources, git_starts = _chosen(
                    conn, settings, list_resources(conn, kinds=["git_local"]), "git", now
                )
                git_states = read_git_states(conn, git_resources)
            collection = collect_git(settings, git_resources, git_states, now=now, starts=git_starts)
            with ledger.locked() as conn:
                git_written = write_git(conn, collection, now=now)
            git = GitReport(
                repos=len(git_resources), signals_written=git_written, errors=collection.errors
            )
            # remotes seen in local checkouts become pending git_remote resources (suggestions)
            discovered = discover_remotes(settings, git_resources)
            with ledger.locked() as conn:
                # every URL ever suggested is remembered, so a rejected suggestion stays gone
                suggested = get_state(conn, "remote:suggested")
                suggested = suggested if isinstance(suggested, dict) else {}
                for project_id, url in discovered:
                    seen = suggested.get(project_id)
                    seen = seen if isinstance(seen, list) else []
                    if url in seen:
                        continue
                    suggested[project_id] = [*seen, url]
                    with conn:
                        set_state(conn, "remote:suggested", suggested)
                    if find_resource(conn, project_id, "git_remote", url) is None:
                        upsert_resource(
                            ledger,
                            DISCOVERY_ACTOR,
                            ResourceIn(
                                project_id=project_id,
                                kind="git_remote",
                                locator=url,
                                title=urlparse(url).path.strip("/"),
                            ),
                            intent=f"discovered git remote {url}",
                        )
            # remote: same three phases, the HTTP calls run with no lock; skipped until due
            with ledger.locked() as conn:
                remote_run = remote_due(conn, now=now, every_m=settings.remote_every_m)
                # only accepted remotes are polled: a pending (discovered) one must not receive tokens
                unreviewed = pending_txns(conn, "resource") if remote_run else {}
                remote_resources, remote_starts = _chosen(
                    conn,
                    settings,
                    [r for r in list_resources(conn, kinds=["git_remote"]) if r.id not in unreviewed]
                    if remote_run
                    else [],
                    "remote",
                    now,
                )
                remote_states = read_remote_states(conn, remote_resources)
            if remote_run:
                remote_collection = collect_remote(
                    settings, remote_resources, remote_states, now=now, starts=remote_starts
                )
                remote_signals, remote_milestones = write_remote(ledger, remote_collection, now=now)
                remote = RemoteReport(
                    repos=len(remote_resources),
                    signals_written=remote_signals,
                    milestones_created=remote_milestones,
                    errors=remote_collection.errors,
                )
            # slurm: ssh runs with no lock; only when a login host is configured and due
            if settings.slurm_host:
                with ledger.locked() as conn:
                    slurm_run = slurm_due(conn, now=now, every_m=settings.slurm_every_m)
                    slurm_from = slurm_since(conn, now=now, full_days=settings.scan_since_days)
                if slurm_run:
                    slurm_collection = collect_slurm(settings, since=slurm_from)
                    slurm = write_slurm(ledger, settings, slurm_collection, now=now)
            # folders: same three phases, the walk runs with no lock; skipped until due
            with ledger.locked() as conn:
                due = folders_due(conn, now=now, every_h=settings.folders_scan_every_h)
                folder_resources, folder_starts = _chosen(
                    conn, settings, list_resources(conn, kinds=["folder"]) if due else [], "folder", now
                )
                folder_states = read_folder_states(conn, folder_resources)
            if due:
                folder_collection = collect_folders(
                    settings, folder_resources, folder_states, now=now, starts=folder_starts
                )
                with ledger.locked() as conn:
                    folders_written = write_folders(conn, folder_collection, now=now)
                folders = FoldersReport(
                    folders=len(folder_resources),
                    signals_written=folders_written,
                    errors=folder_collection.errors,
                )
            recurrence = materialize(ledger, now=now, tz=settings.tz)
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
                remote=remote,
                slurm=slurm,
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
