"""Folders connector: one `files_changed` signal per (folder, UTC day) from file mtimes.

Like the git connector it has three phases so the filesystem walk never runs while the ledger
lock is held: read cursors (`read_folder_states`), walk with no connection (`collect_folders`),
then write signals and cursors (`write_folders`). The walk never follows symlinks, skips
excluded directory names and anything the root `.gitignore` matches (nested ones are ignored),
and does not descend into a directory whose own files are very large.
"""

import os
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import apsw
from pathspec import GitIgnoreSpec
from pydantic import BaseModel

from trajectree.config import Settings
from trajectree.connectors.paths import to_local
from trajectree.models import Resource, SignalIn
from trajectree.repo.signals import upsert_signals
from trajectree.repo.state import get_state, set_state
from trajectree.timeutil import from_db, to_db

DEFAULT_EXCLUDES = (
    ".git", "node_modules", ".venv", "venv", "__pycache__", ".cache",
    ".mypy_cache", ".pytest_cache", "dist", "build", ".tox",
)  # fmt: skip
MAX_PATHS = 20


class DayChange(BaseModel):
    count: int = 0
    bytes: int = 0
    paths: list[str] = []
    big_dirs: list[str] = []
    t_min: float = 0.0  # earliest and latest mtime counted on the day (epoch seconds)
    t_max: float = 0.0


class FolderCollection(BaseModel):
    signals: list[SignalIn]
    polled: list[str]  # resource ids whose cursor advances
    errors: list[str]


class FoldersReport(BaseModel):
    folders: int
    signals_written: int
    errors: list[str]


def load_gitignore(root: Path) -> GitIgnoreSpec | None:
    """The root `.gitignore` as a spec, or None when absent or unreadable.

    Raises ValueError (GitIgnorePatternError) for a malformed pattern; the collector reports it.
    """
    try:
        lines = (root / ".gitignore").read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    return GitIgnoreSpec.from_lines(lines)


def _day(mtime: float) -> str:
    return datetime.fromtimestamp(mtime, tz=timezone.utc).date().isoformat()


def scan_folder(
    root: Path,
    *,
    since: datetime,
    excludes: tuple[str, ...] | list[str] = DEFAULT_EXCLUDES,
    big_dir_mb: int = 500,
    max_entries: int = 200_000,
) -> dict[str, DayChange]:
    """Per UTC day, the files under `root` modified at or after `since`.

    Raises OSError when `root` itself cannot be listed; unreadable subdirectories are skipped.
    """
    return scan_folder_capped(
        root, since=since, excludes=excludes, big_dir_mb=big_dir_mb, max_entries=max_entries
    )[0]


def scan_folder_capped(
    root: Path,
    *,
    since: datetime,
    excludes: tuple[str, ...] | list[str] = DEFAULT_EXCLUDES,
    big_dir_mb: int = 500,
    max_entries: int = 200_000,
) -> tuple[dict[str, DayChange], bool]:
    """`scan_folder`, plus whether the walk stopped at `max_entries` with entries left unseen."""
    spec = load_gitignore(root)
    cutoff = since.timestamp()
    big_bytes = big_dir_mb * 1024 * 1024
    per_day: dict[str, list[tuple[float, int, str]]] = {}
    big: dict[str, list[str]] = {}
    seen = 0
    stack: list[tuple[Path, str]] = [(root, "")]
    while stack and seen < max_entries:
        directory, rel_dir = stack.pop()
        try:
            with os.scandir(directory) as it:
                entries = list(it)
        except OSError:
            if directory == root:
                raise
            continue
        files: list[tuple[str, float, int]] = []
        subdirs: list[tuple[Path, str]] = []
        for entry in entries:
            seen += 1
            if seen > max_entries:
                break
            rel = f"{rel_dir}{entry.name}"
            try:
                if entry.is_dir(follow_symlinks=False):
                    if entry.name in excludes or (spec and spec.match_file(rel + "/")):
                        continue
                    subdirs.append((Path(entry.path), rel + "/"))
                elif entry.is_file(follow_symlinks=False):
                    if spec and spec.match_file(rel):
                        continue
                    st = entry.stat(follow_symlinks=False)
                    files.append((rel, st.st_mtime, st.st_size))
            except OSError:
                continue
        if rel_dir and sum(size for _, _, size in files) > big_bytes:
            newest = max((m for _, m, _ in files), default=0.0)
            if newest >= cutoff:
                big.setdefault(_day(newest), []).append(rel_dir.rstrip("/"))
            continue
        for rel, mtime, size in files:
            if mtime >= cutoff:
                per_day.setdefault(_day(mtime), []).append((mtime, size, rel))
        stack.extend(subdirs)
    result: dict[str, DayChange] = {}
    for day in sorted(set(per_day) | set(big)):
        items = sorted(per_day.get(day, []))
        result[day] = DayChange(
            count=len(items),
            bytes=sum(size for _, size, _ in items),
            paths=[rel for _, _, rel in items[-MAX_PATHS:]],
            big_dirs=sorted(big.get(day, [])),
            t_min=items[0][0] if items else 0.0,
            t_max=items[-1][0] if items else 0.0,
        )
    return result, seen > max_entries or bool(stack)


def _state_key(resource_id: str) -> str:
    return f"folders:{resource_id}"


def _since(state: Any, start: datetime | None) -> datetime:
    """The cursor minus a day when the resource was polled; otherwise `start` (None: the epoch)."""
    if isinstance(state, dict) and isinstance(state.get("last_scan"), str):
        try:
            back = from_db(state["last_scan"]) - timedelta(days=1)
            return back.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        except ValueError:
            pass
    return start or datetime(1970, 1, 1, tzinfo=timezone.utc)


def read_folder_states(conn: apsw.Connection, resources: list[Resource]) -> dict[str, dict | None]:
    """Each resource's stored scan cursor, keyed by resource id."""
    return {r.id: get_state(conn, _state_key(r.id)) for r in resources}


def collect_folders(
    settings: Settings,
    resources: list[Resource],
    states: dict[str, dict | None],
    *,
    now: datetime,
    starts: Mapping[str, datetime | None],
) -> FolderCollection:
    """Walk each folder resource and build its per-day `files_changed` signals; no DB access.

    A missing or unreadable folder is reported in `errors` and never stops the others.
    """
    errors: list[str] = []
    signals: list[SignalIn] = []
    polled: list[str] = []
    for resource in resources:
        if resource.kind != "folder":
            continue
        local = to_local(settings, resource.locator)
        try:
            if not local.is_dir():
                errors.append(f"{resource.locator}: not a directory")
                continue
            days = scan_folder(
                local,
                since=_since(states.get(resource.id), starts.get(resource.id)),
                big_dir_mb=settings.folders_big_dir_mb,
            )
        except (OSError, ValueError) as exc:  # ValueError: malformed .gitignore pattern
            errors.append(f"{resource.locator}: {exc}")
            continue
        for day, change in days.items():
            t_min = datetime.fromtimestamp(change.t_min or change.t_max or 0, tz=timezone.utc)
            t_max = datetime.fromtimestamp(change.t_max or change.t_min or 0, tz=timezone.utc)
            if not change.count:  # only big directories changed: anchor on the day itself
                t_min = t_max = datetime.fromisoformat(day).replace(tzinfo=timezone.utc)
            signals.append(
                SignalIn(
                    project_id=resource.project_id,
                    resource_id=resource.id,
                    kind="files_changed",
                    t_start=t_min,
                    t_end=t_max,
                    dedupe_key=f"folder:{resource.id}:{day}",
                    payload={
                        "count": change.count,
                        "bytes": change.bytes,
                        "paths": change.paths,
                        "big_dirs": change.big_dirs,
                    },
                )
            )
        polled.append(resource.id)
    return FolderCollection(signals=signals, polled=polled, errors=errors)


def write_folders(conn: apsw.Connection, collection: FolderCollection, *, now: datetime) -> int:
    """Upsert the signals, advance the scanned cursors and stamp `folders:last` in one transaction."""
    with conn:
        written = upsert_signals(conn, collection.signals)
        for rid in collection.polled:
            set_state(conn, _state_key(rid), {"last_scan": to_db(now)})
        set_state(conn, "folders:last", {"at": to_db(now)})
    return written


def folders_due(conn: apsw.Connection, *, now: datetime, every_h: int) -> bool:
    """True when no scan is recorded or the last one is older than `every_h` hours (0 = never)."""
    if every_h <= 0:
        return False
    state = get_state(conn, "folders:last")
    if not isinstance(state, dict) or not isinstance(state.get("at"), str):
        return True
    try:
        return now - from_db(state["at"]) >= timedelta(hours=every_h)
    except ValueError:
        return True
