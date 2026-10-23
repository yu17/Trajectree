"""Local git connector: one `commits` signal per (repository, UTC day) from `git log`.

Commits are placed by committer date (the date `--since` filters on), so a day is always
computed whole and a rebased or amended commit cannot leave a day partial.

Polling has three phases so the `git log` subprocesses never run while the ledger lock is
held: read the resources and their cursors (`read_git_states`), run git with no connection
(`collect_git`), then write the signals and cursors (`write_git`).
"""

import os
import subprocess
from datetime import datetime, timedelta, timezone
from typing import Any

import apsw
from pydantic import BaseModel

from trajectree.config import Settings
from trajectree.connectors.paths import to_local
from trajectree.models import Resource, SignalIn
from trajectree.repo.resources import list_resources
from trajectree.repo.signals import upsert_signals
from trajectree.repo.state import get_state, set_state
from trajectree.timeutil import from_db, to_db


class GitReport(BaseModel):
    repos: int
    signals_written: int
    errors: list[str]


class GitCollection(BaseModel):
    signals: list[SignalIn]
    polled: list[str]  # resource ids whose cursor advances
    errors: list[str]


def _state_key(resource_id: str) -> str:
    return f"git:{resource_id}"


def _since(state: Any, now: datetime, since_days: int) -> datetime:
    if isinstance(state, dict) and isinstance(state.get("last_poll"), str):
        try:
            back = from_db(state["last_poll"]) - timedelta(days=1)
            return back.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        except ValueError:
            pass
    first = (now - timedelta(days=since_days)).astimezone(timezone.utc)
    return first.replace(hour=0, minute=0, second=0, microsecond=0)


def _parse(stdout: str) -> dict[str, list[tuple[datetime, str, str, str]]]:
    days: dict[str, list[tuple[datetime, str, str, str]]] = {}
    for line in stdout.splitlines():
        parts = line.split("\x1f")
        if len(parts) != 4:
            continue
        sha, commit_iso, author, subject = parts
        try:
            when = datetime.fromisoformat(commit_iso).astimezone(timezone.utc)
        except ValueError:
            continue
        days.setdefault(when.date().isoformat(), []).append((when, sha, author, subject))
    return days


def read_git_states(conn: apsw.Connection, resources: list[Resource]) -> dict[str, dict | None]:
    """Each resource's stored poll cursor, keyed by resource id."""
    return {r.id: get_state(conn, _state_key(r.id)) for r in resources}


def collect_git(
    settings: Settings,
    resources: list[Resource],
    states: dict[str, dict | None],
    *,
    now: datetime,
    since_days: int,
) -> GitCollection:
    """Run `git log` for each resource and build its per-day `commits` signals; no DB access.

    A day is always recomputed whole, so its signal is replaced, never merged. One bad
    repository is reported in `errors` and never stops the others.
    """
    errors: list[str] = []
    signals: list[SignalIn] = []
    polled: list[str] = []
    for resource in resources:
        try:
            local = to_local(settings, resource.locator)
            if not (local / ".git").exists():
                errors.append(f"{resource.locator}: not a git repository")
                continue
            since = _since(states.get(resource.id), now, since_days)
            cmd = [
                "git", "-c", "safe.directory=*", "-C", str(local), "log", "--all",
                f"--since={since.isoformat()}", "--date=iso-strict",
                "--format=%H%x1f%cI%x1f%an%x1f%s",
            ]  # fmt: skip
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=60,
                env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
            )
        except (subprocess.TimeoutExpired, OSError, ValueError) as exc:
            errors.append(f"{resource.locator}: {exc}")
            continue
        if proc.returncode != 0:
            errors.append(f"{resource.locator}: {proc.stderr.strip()}")
            continue
        for day, commits in _parse(proc.stdout).items():
            commits.sort(key=lambda c: c[0])
            signals.append(
                SignalIn(
                    project_id=resource.project_id,
                    resource_id=resource.id,
                    kind="commits",
                    t_start=commits[0][0],
                    t_end=commits[-1][0],
                    dedupe_key=f"git:{resource.id}:{day}",
                    payload={
                        "count": len(commits),
                        "subjects": [c[3] for c in commits][:20],
                        "authors": sorted({c[2] for c in commits}),
                        "shas": [c[1] for c in commits][:50],
                    },
                )
            )
        polled.append(resource.id)
    return GitCollection(signals=signals, polled=polled, errors=errors)


def discover_remotes(
    settings: Settings, resources: list[Resource], *, run=subprocess.run
) -> list[tuple[str, str]]:
    """Normalized fetch-remote URLs of each git_local resource as (project_id, url), unique per
    project. A repository that cannot be read is skipped. No DB access."""
    from trajectree.connectors.remote import normalize_remote

    found: list[tuple[str, str]] = []
    for resource in resources:
        try:
            local = to_local(settings, resource.locator)
            if not (local / ".git").exists():
                continue
            proc = run(
                ["git", "-c", "safe.directory=*", "-C", str(local), "remote", "-v"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=60,
                env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
            )
        except (subprocess.TimeoutExpired, OSError, ValueError):
            continue
        if proc.returncode != 0:
            continue
        for line in proc.stdout.splitlines():
            parts = line.split()
            if len(parts) != 3 or parts[2] != "(fetch)":
                continue
            url = normalize_remote(parts[1])
            if url is not None and (resource.project_id, url) not in found:
                found.append((resource.project_id, url))
    return found


def write_git(conn: apsw.Connection, collection: GitCollection, *, now: datetime) -> int:
    """Upsert the collected signals and advance the polled cursors in one transaction."""
    with conn:
        written = upsert_signals(conn, collection.signals)
        for rid in collection.polled:
            set_state(conn, _state_key(rid), {"last_poll": to_db(now)})
    return written


def poll_git(
    conn: apsw.Connection, settings: Settings, *, now: datetime, since_days: int = 90
) -> GitReport:
    """Turn each git_local resource's recent commits into per-day `commits` signals.

    All three phases run on `conn`; the ingest job calls them separately so git runs
    outside the ledger lock.
    """
    resources = list_resources(conn, kinds=["git_local"])
    states = read_git_states(conn, resources)
    collection = collect_git(settings, resources, states, now=now, since_days=since_days)
    written = write_git(conn, collection, now=now)
    return GitReport(repos=len(resources), signals_written=written, errors=collection.errors)
