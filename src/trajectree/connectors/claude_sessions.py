"""Claude Code transcript scanner: one `session` signal per transcript, read incrementally.

Transcripts live at `<root>/<encoded cwd>/<session-uuid>.jsonl`. Their format is internal
to Claude Code and changes between versions, so parsing is defensive: a line that does not
look as expected is skipped, never raised on. Files are only ever opened for reading.
"""

import json
import os
from collections import Counter, deque
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO

import apsw
from pydantic import AwareDatetime, BaseModel, ValidationError

from trajectree.config import Settings
from trajectree.connectors.paths import encode_project_dir
from trajectree.importing import SESSION_SOURCE_OF_BACKEND, source_picked, source_start
from trajectree.models import Project, SignalIn
from trajectree.repo.projects import list_projects
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
    # One API response is written as several assistant lines (one per content block) that
    # share `message.id` and repeat its usage; these let a later line, even one read in a
    # later incremental scan, replace that message's contribution instead of adding to it.
    last_msg_id: str | None = None
    last_msg_usage: tuple[int, int] = (0, 0)


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
        acc.model = _str(message.get("model")) or acc.model
        tokens_in = (
            _int(usage.get("input_tokens"))
            + _int(usage.get("cache_creation_input_tokens"))
            + _int(usage.get("cache_read_input_tokens"))
        )
        tokens_out = _int(usage.get("output_tokens"))
        msg_id = _str(message.get("id"))
        if msg_id is not None and msg_id == acc.last_msg_id:
            # Same response as the previous line: its last line's usage is the one that counts.
            acc.tokens_in -= acc.last_msg_usage[0]
            acc.tokens_out -= acc.last_msg_usage[1]
        else:  # a new message (a line without an id is its own message)
            acc.assistant_msgs += 1
            acc.last_msg_id = msg_id
        acc.tokens_in += tokens_in
        acc.tokens_out += tokens_out
        acc.last_msg_usage = (tokens_in, tokens_out)
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


def _text_blocks(content: Any) -> list[str]:
    """The non-empty text of a message `content` (a string, or a list of typed blocks)."""
    if isinstance(content, str):
        return [content] if content.strip() else []
    blocks = content if isinstance(content, list) else []
    texts = [_str(_dict(b).get("text")) for b in blocks if _dict(b).get("type") == "text"]
    return [t for t in texts if t is not None and t.strip()]


def digest_transcript(path: Path, *, max_chars: int = 12000) -> str:
    """A compact Markdown digest of a transcript: prompts, tools used, assistant text, last message.

    Streams the file once and skips lines it cannot read; `OSError` from opening or reading
    the file propagates. The raw JSONL never leaves this function.
    """
    prompts: list[str] = []
    tools: Counter[str] = Counter()
    head: list[str] = []
    tail: deque[str] = deque(maxlen=10)
    texts = 0
    last = ""
    with path.open("rb") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if not isinstance(rec, dict):
                continue
            content = _dict(rec.get("message")).get("content")
            if rec.get("type") == "user" and _is_prompt(rec):
                prompts.extend(" ".join(t.split())[:300] for t in _text_blocks(content))
            elif rec.get("type") == "assistant":
                for block in content if isinstance(content, list) else []:
                    block = _dict(block)
                    if block.get("type") == "tool_use":
                        tools[_str(block.get("name")) or "unknown"] += 1
                for text in _text_blocks(content):
                    texts += 1
                    last = text.strip()
                    short = " ".join(text.split())[:300]
                    if len(head) < 10:
                        head.append(short)
                    else:
                        tail.append(short)
    return render_digest(prompts, tools, head, list(tail), texts, last, max_chars)


def render_digest(
    prompts: list[str],
    tools: Counter[str],
    head: list[str],
    tail: list[str],
    texts: int,
    last: str,
    max_chars: int,
) -> str:
    """The Markdown digest of a session: `head`/`tail` are the first and last assistant texts of `texts`."""
    sections: list[str] = []
    if prompts:
        sections.append("## Prompts\n" + "\n".join(f"{i}. {p}" for i, p in enumerate(prompts, 1)))
    if tools:
        ranked = sorted(tools.items(), key=lambda kv: (-kv[1], kv[0]))
        sections.append("## Tools used\n" + "\n".join(f"- {name} × {n}" for name, n in ranked))
    if head:
        skipped = texts - len(head) - len(tail)
        lines = [f"- {t}" for t in head]
        if skipped > 0:
            lines.append(f"(… {skipped} more)")
        lines.extend(f"- {t}" for t in tail)
        sections.append("## Assistant\n" + "\n".join(lines))
    if last:
        sections.append("## Final message\n" + last[:1500])
    digest = "\n\n".join(sections)
    if len(digest) <= max_chars:
        return digest
    marker = "(digest truncated)"
    return digest[: max(0, max_chars - len(marker) - 1)] + "\n" + marker


def transcript_roots(settings: Settings) -> list[tuple[str, Path]]:
    """`(backend, projects dir)` for the Claude home and each ccx backend profile that exist."""
    roots: list[tuple[str, Path]] = []
    if settings.claude_home is not None and (settings.claude_home / "projects").is_dir():
        roots.append(("claude", settings.claude_home / "projects"))
    if settings.backends_root is not None and settings.backends_root.is_dir():
        try:
            profiles = sorted(settings.backends_root.iterdir())
        except OSError:
            profiles = []
        for profile in profiles:
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


MAX_TRANSCRIPT_FILES = 500  # transcripts parsed per `project_transcripts` call


class ProjectTranscripts(BaseModel):
    sessions: list[SessionSummary]  # newest first
    capped: bool = False  # more matching files existed than `max_files`; the newest were read


def project_transcripts(
    settings: Settings,
    folder: str,
    *,
    registered: Sequence[str],
    before: datetime | None = None,
    before_by_source: Mapping[str, datetime] | None = None,
    max_files: int = MAX_TRANSCRIPT_FILES,
) -> ProjectTranscripts:
    """Summaries of the transcripts whose cwd belongs to `folder` (a host path), newest first.

    A transcript belongs to `folder` when its recorded cwd lies in it and `folder` is the deepest
    of the `registered` folder host paths containing that cwd (the rule `project_for_path` uses),
    so `/x/app-android` never counts for `/x/app`. `before` keeps only sessions that ended before
    it. `before_by_source` (import source -> cutoff) does the same per source and skips, without
    reading, the transcripts of any source it does not list. At most `max_files` transcripts are
    parsed (newest files first); `capped` says the rest were left out. Pure file I/O: no DB, no lock.
    """
    prefix = encode_project_dir(folder)
    candidates: list[tuple[float, str, Path]] = []
    for backend, root in transcript_roots(settings):
        if before_by_source is not None and SESSION_SOURCE_OF_BACKEND[backend] not in before_by_source:
            continue
        try:
            dirs = [d for d in root.iterdir() if d.is_dir() and (d.name == prefix or d.name.startswith(prefix + "-"))]
        except OSError:
            continue
        for d in dirs:
            try:
                for f in d.glob("*.jsonl"):
                    if f.is_file():
                        candidates.append((f.stat().st_mtime, backend, f))
            except OSError:
                continue
    return select_in_folder(
        candidates, folder, registered, before=before, before_by_source=before_by_source, max_files=max_files
    )


def cwd_in_folder(cwd: str, folder: str, registered: Sequence[str]) -> bool:
    """True when `cwd` lies in `folder` and `folder` is the deepest of the `registered` folders containing it."""
    path = PurePosixPath(cwd)
    root = PurePosixPath(folder)
    if not (path == root or path.is_relative_to(root)):
        return False
    deeper = (PurePosixPath(r) for r in registered if len(r) > len(folder))
    return not any(path == r or path.is_relative_to(r) for r in deeper)


def select_in_folder(
    candidates: list[tuple[float, str, Path]],
    folder: str,
    registered: Sequence[str],
    *,
    before: datetime | None,
    before_by_source: Mapping[str, datetime] | None,
    max_files: int,
    parse: Callable[[Iterable[bytes], SessionSummary], SessionSummary] | None = None,
) -> ProjectTranscripts:
    """Parse the newest `max_files` of `candidates` (`(mtime, backend, path)`) and keep the sessions
    whose cwd belongs to `folder` (the deepest-registered-folder rule), ended before their cutoff."""
    candidates = sorted(candidates, key=lambda c: c[0], reverse=True)
    capped = len(candidates) > max_files
    found: list[SessionSummary] = []
    for mtime, backend, path in candidates[:max_files]:
        cutoff = before_by_source[SESSION_SOURCE_OF_BACKEND[backend]] if before_by_source is not None else before
        try:
            summary, _ = _read_from(
                path, 0, SessionSummary(transcript_path=str(path.absolute()), backend=backend), parse or parse_lines
            )
        except OSError:
            continue
        if summary.cwd is None:
            continue
        if not cwd_in_folder(summary.cwd, folder, registered):
            continue
        ended = summary.t_end or datetime.fromtimestamp(mtime, timezone.utc)
        if cutoff is not None and ended >= cutoff:
            continue
        found.append(summary.model_copy(update={"t_end": ended}))
    found.sort(key=lambda sm: sm.t_end or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return ProjectTranscripts(sessions=found, capped=capped)


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


def _read_from(
    path: Path,
    offset: int,
    acc: SessionSummary,
    parse: Callable[[Iterable[bytes], SessionSummary], SessionSummary] = parse_lines,
) -> tuple[SessionSummary, int]:
    """Fold the complete lines after `offset` into `acc`, streaming one line at a time.

    Returns the new summary and the byte length of the complete lines consumed. Only
    complete lines count; a live session's partial last line (no trailing newline) is
    re-read on the next scan.
    """
    consumed = 0

    def complete_lines(f: BinaryIO) -> Iterator[bytes]:
        nonlocal consumed
        for line in f:
            if not line.endswith(b"\n"):
                return
            consumed += len(line)
            yield line

    with path.open("rb") as f:
        f.seek(offset)
        summary = parse(complete_lines(f), acc)
    return summary, consumed


def transcript_cutoff(
    conn: apsw.Connection, settings: Settings, now: datetime, source: str = "claude"
) -> float | None:
    """Skip transcript files last written before this (POSIX seconds); `None` reads them all.

    The earliest `source` start among the projects that picked it, so a project that asked
    for more history than the default gets it; with no such project, the default.
    """
    default = (now - timedelta(days=settings.scan_since_days)).timestamp()
    starts = [
        source_start(p, source, now=now, default_days=settings.scan_since_days, tz=settings.tz)
        for p in list_projects(conn)
        if source_picked(p, source)
    ]
    if not starts:
        return default
    if None in starts:
        return None
    return min(s.timestamp() for s in starts if s is not None)


def refresh_file(
    conn: apsw.Connection,
    path: Path,
    st: os.stat_result,
    backend: str,
    parse: Callable[[Iterable[bytes], SessionSummary], SessionSummary] = parse_lines,
) -> tuple[SessionSummary, bool] | None:
    """The file's stored summary brought up to date, and whether any of it was read; `None` when
    unreadable. Resumes from the stored byte offset; a new, unreadable-state or shrunk file starts over."""
    abs_path = str(path.absolute())
    key = f"scan:{abs_path}"
    state = get_state(conn, key)
    stored = _stored_summary(state)
    if stored is not None and state["size"] == st.st_size and state["mtime"] == st.st_mtime:
        return stored, False
    if stored is not None and st.st_size >= state["offset"]:
        offset, summary = state["offset"], stored
    else:  # new file, unreadable state, or truncated below what was read: start over
        offset, summary = 0, SessionSummary(transcript_path=abs_path, backend=backend)
    try:
        summary, consumed = _read_from(path, offset, summary, parse)
    except OSError:
        return None
    set_state(
        conn,
        key,
        {
            "offset": offset + consumed,
            "size": st.st_size,
            "mtime": st.st_mtime,
            "summary": summary.model_dump(mode="json"),
        },
    )
    return summary, True


def _refresh_summaries(
    conn: apsw.Connection, settings: Settings, now: datetime
) -> tuple[int, int, list[tuple[SessionSummary, float]]]:
    """Update each recent transcript's stored summary; returns (seen, read, summaries)."""
    cutoff = transcript_cutoff(conn, settings, now)
    files_seen = files_read = 0
    summaries: list[tuple[SessionSummary, float]] = []
    for backend, root in transcript_roots(settings):
        for path in _transcripts(root):
            try:
                st = path.stat()
            except OSError:
                continue
            if cutoff is not None and st.st_mtime < cutoff:
                continue
            files_seen += 1
            refreshed = refresh_file(conn, path, st, backend)
            if refreshed is None:
                continue
            files_read += refreshed[1]
            summaries.append((refreshed[0], st.st_mtime))
    return files_seen, files_read, summaries


def session_signals(
    conn: apsw.Connection,
    settings: Settings,
    summaries: list[tuple[SessionSummary, float]],
    now: datetime,
) -> tuple[list[SignalIn], list[LiveSession]]:
    """One `session` signal per summary whose cwd maps to a project, plus the live ones."""
    projects: dict[str, Project | None] = {}
    signals: list[SignalIn] = []
    live: list[LiveSession] = []
    for summary, mtime in summaries:
        if summary.cwd is None:
            continue
        # The transcript file is named after its session; use that when no record said.
        session_id = summary.session_id or Path(summary.transcript_path).stem
        if summary.cwd not in projects:
            projects[summary.cwd] = project_for_path(conn, summary.cwd)
        project = projects[summary.cwd]
        if project is None:
            continue
        source = SESSION_SOURCE_OF_BACKEND[summary.backend]
        if not source_picked(project, source):
            continue
        seen = datetime.fromtimestamp(mtime, timezone.utc)
        start = source_start(
            project, source, now=now, default_days=settings.scan_since_days, tz=settings.tz
        )
        if start is not None and (summary.t_end or seen) < start:
            continue
        running = (now - seen).total_seconds() < settings.live_window_s
        signals.append(
            SignalIn(
                project_id=project.id,
                kind="session",
                t_start=summary.t_start or seen,
                t_end=summary.t_end or seen,
                dedupe_key=f"{source}_session:{session_id}",
                payload={
                    "session_id": session_id,
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
                    session_id=session_id,
                    cwd=summary.cwd,
                    title=summary.title,
                    started=summary.t_start,
                    last_seen=seen,
                )
            )
    return signals, live


def scan_transcripts(conn: apsw.Connection, settings: Settings, *, now: datetime) -> ScanReport:
    """Bring every recent transcript's summary up to date and upsert its session signal.

    Runs all DB work on `conn` in one transaction, so cursors and signals land together;
    callers that need the ledger lock take it themselves.
    """
    with conn:
        files_seen, files_read, summaries = _refresh_summaries(conn, settings, now)
        signals, live = session_signals(conn, settings, summaries, now)
        written = upsert_signals(conn, signals)
    return ScanReport(
        files_seen=files_seen, files_read=files_read, signals_written=written, live=live
    )
