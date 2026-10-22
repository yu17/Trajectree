"""Claude Code transcript scanner: one `session` signal per transcript, read incrementally.

Transcripts live at `<root>/<encoded cwd>/<session-uuid>.jsonl`. Their format is internal
to Claude Code and changes between versions, so parsing is defensive: a line that does not
look as expected is skipped, never raised on. Files are only ever opened for reading.
"""

import json
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import apsw
from pydantic import AwareDatetime, BaseModel, ValidationError

from trajectree.config import Settings
from trajectree.models import Project, SignalIn
from trajectree.repo.resources import project_for_path
from trajectree.repo.signals import upsert_signals
from trajectree.repo.state import get_state, set_state


class SessionSummary(BaseModel):
    session_id: str | None = None
    cwd: str | None = None
    title: str | None = None
    git_branch: str | None = None
    model: str | None = None
    version: str | None = None
    t_start: AwareDatetime | None = None
    t_end: AwareDatetime | None = None
    prompts: int = 0
    assistant_msgs: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    transcript_path: str
    backend: str


class LiveSession(BaseModel):
    project_id: str
    session_id: str
    cwd: str
    title: str | None
    started: AwareDatetime | None
    last_seen: AwareDatetime


class ScanReport(BaseModel):
    files_seen: int
    files_read: int
    signals_written: int
    live: list[LiveSession]


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        ts = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError:
        return None
    return ts if ts.utcoffset() is not None else None


def _is_prompt(rec: dict[str, Any]) -> bool:
    message = _dict(rec.get("message"))
    if message.get("role") != "user" or rec.get("isMeta") is True:
        return False
    content = message.get("content")
    if isinstance(content, str):
        return True
    return isinstance(content, list) and any(
        isinstance(block, dict) and block.get("type") == "text" for block in content
    )


def _apply(acc: SessionSummary, rec: dict[str, Any]) -> None:
    kind = rec.get("type")
    if kind == "user" and _is_prompt(rec):
        acc.prompts += 1
    elif kind == "assistant":
        message = _dict(rec.get("message"))
        usage = _dict(message.get("usage"))
        acc.assistant_msgs += 1
        acc.model = _str(message.get("model")) or acc.model
        acc.tokens_in += (
            _int(usage.get("input_tokens"))
            + _int(usage.get("cache_creation_input_tokens"))
            + _int(usage.get("cache_read_input_tokens"))
        )
        acc.tokens_out += _int(usage.get("output_tokens"))
    elif kind == "ai-title":
        acc.title = _str(rec.get("aiTitle")) or acc.title
    elif kind == "summary" and acc.title is None:
        acc.title = _str(rec.get("summary"))
    acc.cwd = acc.cwd or _str(rec.get("cwd"))
    acc.session_id = acc.session_id or _str(rec.get("sessionId"))
    acc.git_branch = acc.git_branch or _str(rec.get("gitBranch"))
    acc.version = acc.version or _str(rec.get("version"))
    ts = _timestamp(rec.get("timestamp"))
    if ts is not None:
        acc.t_start = ts if acc.t_start is None else min(acc.t_start, ts)
        acc.t_end = ts if acc.t_end is None else max(acc.t_end, ts)


def parse_lines(lines: Iterable[bytes], acc: SessionSummary) -> SessionSummary:
    """Fold transcript lines into a copy of `acc`; malformed or unexpected lines are skipped."""
    out = acc.model_copy(deep=True)
    for line in lines:
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if isinstance(rec, dict):
            _apply(out, rec)
    return out


def transcript_roots(settings: Settings) -> list[tuple[str, Path]]:
    """`(backend, projects dir)` for the Claude home and each ccx backend profile that exist."""
    roots: list[tuple[str, Path]] = []
    if settings.claude_home is not None and (settings.claude_home / "projects").is_dir():
        roots.append(("claude", settings.claude_home / "projects"))
    if settings.backends_root is not None and settings.backends_root.is_dir():
        for profile in sorted(settings.backends_root.iterdir()):
            if (profile / "projects").is_dir():
                roots.append((profile.name, profile / "projects"))
    return roots


def _transcripts(root: Path) -> list[Path]:
    """`*.jsonl` files directly inside each `<root>/<dir>/` (not subagent folders or memory)."""
    try:
        dirs = sorted(d for d in root.iterdir() if d.is_dir())
    except OSError:
        return []
    files: list[Path] = []
    for d in dirs:
        try:
            files.extend(sorted(f for f in d.glob("*.jsonl") if f.is_file()))
        except OSError:
            continue
    return files


def _stored_summary(state: Any) -> SessionSummary | None:
    if not (
        isinstance(state, dict)
        and isinstance(state.get("offset"), int)
        and isinstance(state.get("size"), int)
        and isinstance(state.get("mtime"), (int, float))
    ):
        return None
    try:
        return SessionSummary.model_validate(state.get("summary"))
    except ValidationError:
        return None


def scan_transcripts(conn: apsw.Connection, settings: Settings, *, now: datetime) -> ScanReport:
    """Bring every recent transcript's summary up to date and upsert its session signal.

    Runs all DB work on `conn`; callers that need the ledger lock take it themselves.
    """
    cutoff = (now - timedelta(days=settings.scan_since_days)).timestamp()
    files_seen = files_read = 0
    summaries: list[tuple[SessionSummary, float]] = []
    for backend, root in transcript_roots(settings):
        for path in _transcripts(root):
            try:
                st = path.stat()
            except OSError:
                continue
            if st.st_mtime < cutoff:
                continue
            files_seen += 1
            abs_path = str(path.absolute())
            key = f"scan:{abs_path}"
            state = get_state(conn, key)
            stored = _stored_summary(state)
            if stored is not None and state["size"] == st.st_size and state["mtime"] == st.st_mtime:
                summaries.append((stored, st.st_mtime))
                continue
            if stored is not None and st.st_size >= state["size"]:
                offset, summary = state["offset"], stored
            else:  # new file, unreadable state, or the file shrank: start over
                offset, summary = 0, SessionSummary(transcript_path=abs_path, backend=backend)
            try:
                with path.open("rb") as f:
                    f.seek(offset)
                    data = f.read()
            except OSError:
                continue
            # Only complete lines; a live session's partial last line is re-read next time.
            complete = data.rfind(b"\n") + 1
            summary = parse_lines(data[:complete].splitlines(), summary)
            files_read += 1
            set_state(
                conn,
                key,
                {
                    "offset": offset + complete,
                    "size": st.st_size,
                    "mtime": st.st_mtime,
                    "summary": summary.model_dump(mode="json"),
                },
            )
            summaries.append((summary, st.st_mtime))

    projects: dict[str, Project | None] = {}
    signals: list[SignalIn] = []
    live: list[LiveSession] = []
    for summary, mtime in summaries:
        if summary.session_id is None or summary.cwd is None:
            continue
        if summary.cwd not in projects:
            projects[summary.cwd] = project_for_path(conn, summary.cwd)
        project = projects[summary.cwd]
        if project is None:
            continue
        seen = datetime.fromtimestamp(mtime, timezone.utc)
        running = (now - seen).total_seconds() < settings.live_window_s
        signals.append(
            SignalIn(
                project_id=project.id,
                kind="session",
                t_start=summary.t_start or seen,
                t_end=summary.t_end or seen,
                dedupe_key=f"claude_session:{summary.session_id}",
                payload={
                    "session_id": summary.session_id,
                    "cwd": summary.cwd,
                    "title": summary.title,
                    "git_branch": summary.git_branch,
                    "model": summary.model,
                    "prompts": summary.prompts,
                    "tokens_in": summary.tokens_in,
                    "tokens_out": summary.tokens_out,
                    "transcript_path": summary.transcript_path,
                    "backend": summary.backend,
                    "running": running,
                },
            )
        )
        if running:
            live.append(
                LiveSession(
                    project_id=project.id,
                    session_id=summary.session_id,
                    cwd=summary.cwd,
                    title=summary.title,
                    started=summary.t_start,
                    last_seen=seen,
                )
            )
    written = upsert_signals(conn, signals)
    return ScanReport(
        files_seen=files_seen, files_read=files_read, signals_written=written, live=live
    )
