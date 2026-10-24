"""Import probe: what each history source holds for a folder, before the owner chooses.

`probe_folder` does the heavy work (git, the folder walk, transcript reads) with no ledger lock;
the only database access is `conn_reader`, which takes the lock briefly to read what is
registered. Nothing here writes.
"""

import os
import subprocess
from collections.abc import Callable
from datetime import date, datetime, timezone
from urllib.parse import urlparse

from pydantic import AwareDatetime, BaseModel

from trajectree.config import Settings
from trajectree.connectors.claude_sessions import project_transcripts
from trajectree.connectors.folders import scan_folder_capped
from trajectree.connectors.local_git import discover_remotes
from trajectree.connectors.paths import to_local
from trajectree.db.ledger import Ledger
from trajectree.importing import IMPORT_SOURCES, ImportRecord, default_since, import_record
from trajectree.models import Project, Resource
from trajectree.repo.resources import list_resources, project_for_path
from trajectree.repo.state import get_state

FOLDER_PROBE_MAX_FILES = 200_000
GIT_TIMEOUT_S = 30
# Not history: the rules file init-project writes, and a worktree's `.git` pointer file.
PROBE_SKIP_FILES = (".claude/rules/trajectree.md", ".git")
_EPOCH =datetime(1970, 1, 1, tzinfo=timezone.utc)


class SourceProbe(BaseModel):
    source: str
    count: int
    oldest: AwareDatetime | None
    detail: str = ""
    imported: bool = False


class RemoteProbe(BaseModel):
    url: str
    title: str
    declined: bool = False


class ProbeReport(BaseModel):
    path: str
    default_since: date
    tz: str  # the daemon's zone: a chosen date means local midnight there
    sources: list[SourceProbe]
    remotes: list[RemoteProbe]


class ProbeState(BaseModel):
    """What the probe needs from the ledger."""

    registered: list[str] = []  # host paths of every folder and git_local resource
    project: Project | None = None  # the project the path already belongs to, if any
    declined: list[str] = []  # remote URLs suggested before and not accepted


ConnReader = Callable[[str], ProbeState]


def probe_state_reader(ledger: Ledger) -> ConnReader:
    """A `conn_reader` that reads `ProbeState` from `ledger` under its lock."""

    def read(host_path: str) -> ProbeState:
        with ledger.locked() as conn:
            registered = [r.locator for r in list_resources(conn, kinds=["folder", "git_local"])]
            project = project_for_path(conn, host_path)
            declined: list[str] = []
            if project is not None:
                suggested = get_state(conn, "remote:suggested")
                urls = suggested.get(project.id) if isinstance(suggested, dict) else None
                have = {r.locator for r in list_resources(conn, project_ids=[project.id], kinds=["git_remote"])}
                declined = [u for u in urls if u not in have] if isinstance(urls, list) else []
        return ProbeState(registered=registered, project=project, declined=declined)

    return read


def _git(local, *args: str) -> str | None:
    """stdout of `git <args>` in `local`; None when git fails, times out or is missing."""
    try:
        proc = subprocess.run(
            ["git", "-c", "safe.directory=*", "-C", str(local), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=GIT_TIMEOUT_S,
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    return proc.stdout if proc.returncode == 0 else None


def _probe_git(settings: Settings, host_path: str) -> SourceProbe:
    local = to_local(settings, host_path)
    if not (local / ".git").exists():
        return SourceProbe(source="git", count=0, oldest=None, detail="not a git repository")
    counted = _git(local, "rev-list", "--count", "--all")
    try:
        count = int((counted or "").strip())
    except ValueError:
        return SourceProbe(source="git", count=0, oldest=None, detail="git could not read the repository")
    # `log --reverse --max-count=1` would give the newest commit (the limit applies first),
    # so take the earliest of the root commits instead.
    roots = _git(local, "log", "--all", "--max-parents=0", "--format=%cI") or ""
    dates: list[datetime] = []
    for line in roots.split():
        try:
            dates.append(datetime.fromisoformat(line).astimezone(timezone.utc))
        except ValueError:
            continue
    return SourceProbe(source="git", count=count, oldest=min(dates, default=None))


def _probe_claude(settings: Settings, host_path: str, registered: list[str]) -> SourceProbe:
    found = project_transcripts(settings, host_path, registered=[*registered, host_path])
    starts = [s.t_start for s in found.sessions if s.t_start is not None]
    detail = f"at least {len(found.sessions)} sessions" if found.capped else ""
    return SourceProbe(source="claude", count=len(found.sessions), oldest=min(starts, default=None), detail=detail)


def _probe_folder(settings: Settings, host_path: str) -> SourceProbe:
    local = to_local(settings, host_path)
    if not local.is_dir():
        return SourceProbe(source="folder", count=0, oldest=None, detail="not a directory")
    try:
        days, capped = scan_folder_capped(
            local, since=_EPOCH, big_dir_mb=settings.folders_big_dir_mb, max_entries=FOLDER_PROBE_MAX_FILES,
            skip_files=PROBE_SKIP_FILES,
        )
    except OSError as exc:
        return SourceProbe(source="folder", count=0, oldest=None, detail=str(exc))
    stamps = [d.t_min for d in days.values() if d.count and d.t_min > 0]
    oldest = datetime.fromtimestamp(min(stamps), timezone.utc) if stamps else None
    detail = f"at least {FOLDER_PROBE_MAX_FILES} files" if capped else ""
    return SourceProbe(source="folder", count=sum(d.count for d in days.values()), oldest=oldest, detail=detail)


def probe_folder(
    conn_reader: ConnReader, settings: Settings, host_path: str, *, now: datetime
) -> ProbeReport:
    """Report what each import source holds for `host_path` (a host path), without writing anything."""
    state = conn_reader(host_path)
    probes = {
        "git": _probe_git(settings, host_path),
        "claude": _probe_claude(settings, host_path, state.registered),
        "codex": SourceProbe(source="codex", count=0, oldest=None, detail="not connected"),
        "folder": _probe_folder(settings, host_path),
    }
    record: ImportRecord | None = import_record(state.project) if state.project else None
    sources = []
    for name in IMPORT_SOURCES:
        if name == "remote":
            continue
        choice = record.sources.get(name) if record else None
        sources.append(probes[name].model_copy(update={"imported": choice is not None and choice.picked}))
    anchor = Resource(id="probe", project_id="probe", kind="git_local", locator=host_path)
    remotes = [
        RemoteProbe(url=url, title=urlparse(url).path.strip("/"), declined=url in state.declined)
        for _, url in discover_remotes(settings, [anchor])
    ]
    return ProbeReport(
        path=host_path,
        default_since=default_since(now, settings.scan_since_days, settings.tz),
        tz=settings.tz,
        sources=sources,
        remotes=remotes,
    )
