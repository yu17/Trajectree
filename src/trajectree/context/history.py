"""History before a project's import start: what the `starting_point` run condenses.

`collect_history` runs `git log` and walks folders; `session_history` reads transcripts from disk.
Both are file and subprocess work only, so callers run them with no lock held.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import BaseModel

from trajectree.config import Settings
from trajectree.connectors.claude_sessions import SessionSummary, MAX_TRANSCRIPT_FILES, project_transcripts
from trajectree.connectors.codex_sessions import digest_session, project_codex_sessions
from trajectree.connectors.folders import DEFAULT_EXCLUDES, load_gitignore
from trajectree.importing import SESSION_SOURCE_OF_BACKEND, local_midnight

MAX_WALK_ENTRIES = 50_000  # directory entries visited by one whole folder snapshot
MAX_COMMITS = 5000  # newest commits read per repository
_SUBJECT_CHARS = 100


class HistoryInput(BaseModel):
    git_md: str = ""
    folder_md: str = ""
    truncated: bool = False


def block_date(starts: Mapping[str, date]) -> date:
    """The starting-point block sits at the latest of the chosen starts."""
    return max(starts.values())


# -- git and folders (no lock) -------------------------------------------------------------


def _git_commits(folder: Path, before: datetime) -> tuple[list[tuple[datetime, str, str]], str | None]:
    """`(committed, sha, subject)` of the newest `MAX_COMMITS` commits in `folder` older than
    `before`, and a one-line note when git could not answer (never a silent empty result)."""
    if not (folder / ".git").exists():
        return [], None
    cmd = [
        "git", "-c", "safe.directory=*", "-C", str(folder), "log", "--all", f"--max-count={MAX_COMMITS}",
        f"--before={before.isoformat()}", "--format=%H%x1f%cI%x1f%s",
    ]  # fmt: skip
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
        )
    except subprocess.TimeoutExpired:
        return [], f"git history unavailable for {folder.name}: git log timed out"
    except OSError as exc:
        return [], f"git history unavailable for {folder.name}: {exc.strerror or type(exc).__name__}"
    if proc.returncode != 0:
        reason = (proc.stderr.strip().splitlines() or [f"git exited {proc.returncode}"])[0][:200]
        return [], f"git history unavailable for {folder.name}: {reason}"
    commits: list[tuple[datetime, str, str]] = []
    for line in proc.stdout.splitlines():
        parts = line.split("\x1f")
        if len(parts) != 3:
            continue
        try:
            committed = datetime.fromisoformat(parts[1])
        except ValueError:
            continue
        commits.append((committed, parts[0], parts[2]))
    return commits, None


def _git_markdown(commits: list[tuple[datetime, str, str]], tz: str, budget: int) -> tuple[str, bool]:
    """One line per commit when that fits `budget`; otherwise one line per month."""
    zone = ZoneInfo(tz)
    commits = sorted(commits, key=lambda c: c[0])
    lines = [f"- {c[0].astimezone(zone).date().isoformat()} {c[1][:8]} {c[2][:_SUBJECT_CHARS]}" for c in commits]
    full = "\n".join(lines)
    if len(full) <= budget:
        return full, False
    months: dict[str, list[str]] = {}
    for committed, _, subject in commits:
        months.setdefault(committed.astimezone(zone).strftime("%Y-%m"), []).append(subject)
    counts = [f"- {month}: {len(subjects)} commits" for month, subjects in months.items()]
    # Spend what is left on a few subjects per month, never past the budget.
    for per_month in (3, 1):
        rows = [
            f"{count}: " + "; ".join(s[:60] for s in months[month][-per_month:])
            for count, month in zip(counts, months)
        ]
        if len("\n".join(rows)) <= budget:
            return "\n".join(rows), True
    out, used = [], 0
    for row in counts:
        if used + len(row) + 1 > budget:
            break
        out.append(row)
        used += len(row) + 1
    return "\n".join(out), True


class _Walk:
    """The directory-entry budget shared by a whole folder snapshot."""

    def __init__(self, limit: int) -> None:
        self.left = limit
        self.cut = False

    def spend(self) -> bool:
        if self.left <= 0:
            self.cut = True
            return False
        self.left -= 1
        return True


def _scan_entry(path: Path, cutoff: float, excludes: tuple[str, ...], walk: _Walk) -> tuple[int, int, float]:
    """`(files, bytes, newest mtime)` under `path` among files last modified before `cutoff`."""
    count = total = 0
    newest = 0.0
    stack = [path]
    while stack and not walk.cut:
        directory = stack.pop()
        try:
            with os.scandir(directory) as it:
                entries = list(it)
        except OSError:
            continue
        for entry in entries:
            if not walk.spend():
                break
            try:
                if entry.is_dir(follow_symlinks=False):
                    if entry.name not in excludes:
                        stack.append(Path(entry.path))
                elif entry.is_file(follow_symlinks=False):
                    st = entry.stat(follow_symlinks=False)
                    if st.st_mtime < cutoff:
                        count += 1
                        total += st.st_size
                        newest = max(newest, st.st_mtime)
            except OSError:
                continue
    return count, total, newest


def _size(n: int) -> str:
    value = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{n} B"


def _folder_markdown(folders: list[Path], before: datetime, tz: str, budget: int) -> tuple[str, bool]:
    """Top-level entries of each folder with their size and newest modification before `before`."""
    cutoff = before.timestamp()
    zone = ZoneInfo(tz)
    walk = _Walk(MAX_WALK_ENTRIES)
    lines: list[str] = []
    for root in folders:
        if walk.cut:
            break
        try:
            spec = load_gitignore(root)
        except ValueError:
            spec = None
        try:
            with os.scandir(root) as it:
                entries = sorted(it, key=lambda e: e.name)
        except OSError:
            continue
        rows: list[str] = []
        for entry in entries:
            if not walk.spend():
                break
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
                if not is_dir and not entry.is_file(follow_symlinks=False):
                    continue
                if (is_dir and entry.name in DEFAULT_EXCLUDES) or (
                    spec and spec.match_file(entry.name + ("/" if is_dir else ""))
                ):
                    continue
                if is_dir:
                    count, total, newest = _scan_entry(Path(entry.path), cutoff, DEFAULT_EXCLUDES, walk)
                else:
                    st = entry.stat(follow_symlinks=False)
                    count, total, newest = (1, st.st_size, st.st_mtime) if st.st_mtime < cutoff else (0, 0, 0.0)
            except OSError:
                continue
            if count == 0:
                continue
            when = datetime.fromtimestamp(newest, tz=timezone.utc).astimezone(zone).date().isoformat()
            label = f"{entry.name}/ ({count} files)" if is_dir else entry.name
            rows.append(f"- {label}: {_size(total)}, newest {when}")
        if rows:
            lines.append(f"### {root.name}")
            lines.extend(rows)
    note = f"(folder walk stopped after {MAX_WALK_ENTRIES} entries; sizes and dates are partial)"
    out: list[str] = [note] if walk.cut else []
    used = len(note) + 1 if walk.cut else 0
    for line in lines:
        if used + len(line) + 1 > budget:
            return "\n".join(out), True
        out.append(line)
        used += len(line) + 1
    return "\n".join(out), walk.cut


def collect_history(
    settings: Settings,
    *,
    folders: list[Path],
    starts: Mapping[str, date],
    tz: str,
    budget_chars: int,
) -> HistoryInput:
    """Git commits and the folder snapshot from before the chosen starts; never touches the DB.

    `folders` are the project's folders already mapped to local paths and existing. A source
    that did not skip history (no entry in `starts`) contributes nothing.
    """
    truncated = False
    folder_md = ""
    if "folder" in starts:
        folder_md, truncated = _folder_markdown(
            folders, local_midnight(starts["folder"], tz), tz, budget_chars // 4
        )
    git_md = ""
    if "git" in starts:
        before = local_midnight(starts["git"], tz)
        commits: dict[str, tuple[datetime, str, str]] = {}
        notes: list[str] = []
        for folder in folders:
            found, note = _git_commits(folder, before)
            if note:
                notes.append(note)
            if len(found) >= MAX_COMMITS:
                notes.append(f"(only the newest {MAX_COMMITS} commits before the start were read for {folder.name})")
                truncated = True
            for commit in found:
                commits.setdefault(commit[1], commit)
        body = ""
        if commits:
            room = budget_chars - len(folder_md) - sum(len(n) + 1 for n in notes)
            body, git_truncated = _git_markdown(list(commits.values()), tz, max(room, 0))
            truncated = truncated or git_truncated
        git_md = "\n".join([*notes, body] if body else notes)
    return HistoryInput(git_md=git_md, folder_md=folder_md, truncated=truncated)


# -- agent sessions (file I/O, no lock) -----------------------------------------------------


def session_history(
    settings: Settings,
    folders: list[str],
    registered: list[str],
    *,
    starts: Mapping[str, date],
    budget_chars: int,
) -> str:
    """Digests of agent sessions in the project's `folders` (host paths) that ended before their
    source's start, newest first, until `budget_chars` is used.

    Reads the transcripts directly, so it does not depend on what the ingest scanner kept; a
    source that did not skip history is left out. `registered` is every registered folder host path.
    """
    cutoffs = {source: local_midnight(day, settings.tz) for source, day in starts.items()}
    found: dict[str, tuple[datetime, SessionSummary]] = {}
    capped = codex_capped = False
    for folder in folders:
        scan = project_transcripts(settings, folder, registered=registered, before_by_source=cutoffs)
        codex = project_codex_sessions(settings, folder, registered=registered, before_by_source=cutoffs)
        capped = capped or scan.capped
        codex_capped = codex_capped or codex.capped
        for summary in [*scan.sessions, *codex.sessions]:
            if summary.t_end is not None:
                found[summary.transcript_path] = (summary.t_end, summary)
    zone = ZoneInfo(settings.tz)
    sections: list[str] = []
    used = 0
    for ended, summary in sorted(found.values(), key=lambda item: item[0], reverse=True):
        title = summary.title or (summary.session_id or Path(summary.transcript_path).stem)[:8]
        head = f"### {ended.astimezone(zone).date().isoformat()} {title} ({summary.prompts} prompts)"
        room = budget_chars - used - len(head) - 2
        if room < 200:
            break
        try:
            digest = digest_session(
                summary.backend, Path(summary.transcript_path), max_chars=min(settings.digest_max_chars, room)
            )
        except OSError:
            digest = ""
        section = f"{head}\n{digest}" if digest else head
        sections.append(section)
        used += len(section) + 2
    if capped:
        sections.append(f"(only the newest {MAX_TRANSCRIPT_FILES} transcripts per folder were read)")
    if codex_capped:
        sections.append(f"(only the newest {MAX_TRANSCRIPT_FILES} Codex sessions per folder were read)")
    return "\n\n".join(sections)
