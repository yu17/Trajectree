"""Local git connector: one `commits` signal per (repository, UTC day) from `git log`.

Commits are placed by committer date (the date `--since` filters on), so a day is always
computed whole and a rebased or amended commit cannot leave a day partial.
"""

import os
import subprocess
from datetime import datetime, timedelta, timezone

import apsw
from pydantic import BaseModel

from trajectree.config import Settings
from trajectree.connectors.paths import to_local
from trajectree.models import SignalIn
from trajectree.repo.resources import list_resources
from trajectree.repo.signals import upsert_signals
from trajectree.repo.state import get_state, set_state
from trajectree.timeutil import from_db, to_db


class GitReport(BaseModel):
    repos: int
    signals_written: int
    errors: list[str]


def _since(conn: apsw.Connection, resource_id: str, now: datetime, since_days: int) -> datetime:
    state = get_state(conn, f"git:{resource_id}")
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


def poll_git(
    conn: apsw.Connection, settings: Settings, *, now: datetime, since_days: int = 90
) -> GitReport:
    """Turn each git_local resource's recent commits into per-day `commits` signals.

    A day is always recomputed whole, so its signal is replaced, never merged. One bad
    repository is reported in `errors` and never stops the others.
    """
    resources = list_resources(conn, kinds=["git_local"])
    errors: list[str] = []
    signals: list[SignalIn] = []
    polled: list[str] = []
    with conn:
        for resource in resources:
            try:
                local = to_local(settings, resource.locator)
                if not (local / ".git").exists():
                    errors.append(f"{resource.locator}: not a git repository")
                    continue
                since = _since(conn, resource.id, now, since_days)
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
        written = upsert_signals(conn, signals)
        for rid in polled:
            set_state(conn, f"git:{rid}", {"last_poll": to_db(now)})
    return GitReport(repos=len(resources), signals_written=written, errors=errors)
