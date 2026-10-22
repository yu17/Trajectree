"""One ingest pass: transcripts, git, staleness, exports. No LLM calls."""

from __future__ import annotations

import time
from datetime import datetime, timedelta

from pydantic import AwareDatetime, BaseModel

from trajectree.config import Settings
from trajectree.connectors.claude_sessions import ScanReport, scan_transcripts, transcript_roots
from trajectree.connectors.local_git import GitReport, poll_git
from trajectree.db.ledger import Ledger
from trajectree.ingest.exports import render_exports, write_files
from trajectree.repo.signals import stale_projects
from trajectree.repo.state import set_state
from trajectree.timeutil import utcnow


class IngestReport(BaseModel):
    started: AwareDatetime
    duration_ms: int
    scan: ScanReport | None
    git: GitReport
    stale: list[str]
    exports_written: int


def run_ingest(ledger: Ledger, settings: Settings, *, now: datetime | None = None) -> IngestReport:
    """Run every connector, then record staleness, live sessions and the report in state.

    Each phase holds the ledger lock on its own, so API reads can interleave between them.
    """
    now = now or utcnow()
    clock = time.monotonic()
    scan: ScanReport | None = None
    if transcript_roots(settings):
        with ledger.locked() as conn:
            scan = scan_transcripts(conn, settings, now=now)
    with ledger.locked() as conn:
        git = poll_git(conn, settings, now=now, since_days=settings.scan_since_days)
    with ledger.locked() as conn:
        stale = stale_projects(conn, settled_before=now - timedelta(hours=settings.stale_settle_h))
        with conn:
            set_state(conn, "live_sessions", [s.model_dump(mode="json") for s in (scan.live if scan else [])])
            set_state(conn, "stale_projects", stale)
    with ledger.locked() as conn:
        files = render_exports(conn, settings)
    exports_written = write_files(files)
    report = IngestReport(
        started=now,
        duration_ms=int((time.monotonic() - clock) * 1000),
        scan=scan,
        git=git,
        stale=stale,
        exports_written=exports_written,
    )
    with ledger.locked() as conn:
        set_state(conn, "ingest:last", report.model_dump(mode="json"))
    return report
