"""Codex session scanner: one `session` signal per rollout file, read incrementally like Claude's.

Rollouts live at `<root>/YYYY/MM/DD/rollout-<timestamp>-<session-id>.jsonl`. A subagent's rollout
is named `rollout-<timestamp>-<parent-id>_<child-id>.jsonl` but its `session_meta` carries the
parent's id, so a rollout is a session only when the last id in its name is its `session_meta`
id; child rollouts are skipped everywhere, as the Claude scanner skips subagent transcripts. Each line is
`{timestamp, type, payload}`: `session_meta` (first; `id`, `cwd`), `turn_context` (`model`, `cwd`),
`event_msg` (a `UserMessage` item is a prompt), `response_item` (assistant messages and tool calls)
and `token_usage_record` (per-response `usage`). The format is Codex-internal and changes between
versions, so parsing is defensive: an unrecognized or malformed line is skipped. Files are only read.
"""

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

import apsw
from trajectree.config import Settings
from trajectree.connectors.claude_sessions import (
    DEFAULT_SEGMENT_GAP_S,
    MAX_TRANSCRIPT_FILES,
    ProjectTranscripts,
    DigestParts,
    ScanReport,
    SessionSummary,
    _dict,
    _int,
    _str,
    _timestamp,
    add_to_segment,
    cwd_in_folder,
    digest_transcript_windows,
    digest_windows,
    refresh_file,
    prune_session_signals,
    reparse_continuations,
    select_in_folder,
    session_signals,
    transcript_cutoff,
)
from trajectree.models import Signal
from trajectree.repo.signals import session_signals_for, upsert_signals

TITLE_CHARS = 80
_ID_IN_NAME = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def codex_files(settings: Settings) -> list[Path]:
    """Every `rollout-*.jsonl` under the Codex sessions root (none when it is unset or missing)."""
    root = settings.codex_sessions_root
    if root is None or not root.is_dir():
        return []
    try:
        return sorted(f for f in root.rglob("rollout-*.jsonl") if f.is_file())
    except OSError:
        return []


def _name_id(path: str | Path) -> str | None:
    """The last session id in a rollout's file name (a child rollout's own id), if any."""
    found = _ID_IN_NAME.findall(Path(path).stem)
    return found[-1] if found else None


def is_own_session(summary: SessionSummary) -> bool:
    """Whether the rollout is a session of its own: its `session_meta` id is the id in its name."""
    return summary.session_id is not None and summary.session_id == _name_id(summary.transcript_path)


def _shorten(text: str) -> str:
    one = " ".join(text.split())
    return one if len(one) <= TITLE_CHARS else one[: TITLE_CHARS - 1] + "…"


def _blocks(content: Any) -> list[str]:
    """The non-empty `text` of each block in a message or item `content` list."""
    blocks = content if isinstance(content, list) else []
    texts = [_str(_dict(b).get("text")) for b in blocks]
    return [t for t in texts if t is not None and t.strip()]


def _user_prompts(rec: dict[str, Any]) -> list[str]:
    """The prompt text of an `event_msg` user message; the `response_item` copies of the user's
    turn also carry injected context (instructions, environment), so they are not used."""
    payload = _dict(rec.get("payload"))
    item = _dict(payload.get("item"))
    if rec.get("type") == "event_msg" and payload.get("type") == "item_completed" and item.get("type") == "UserMessage":
        return _blocks(item.get("content"))
    return []


def _apply(acc: SessionSummary, rec: dict[str, Any], *, gap_s: int = DEFAULT_SEGMENT_GAP_S) -> None:
    kind = rec.get("type")
    payload = _dict(rec.get("payload"))
    ts = _timestamp(rec.get("timestamp"))
    counted = not (ts is not None and acc.skip_until is not None and ts <= acc.skip_until)
    if kind == "session_meta":
        acc.session_id = _str(payload.get("id")) or acc.session_id
        acc.cwd = _str(payload.get("cwd")) or acc.cwd
        acc.version = _str(payload.get("cli_version")) or acc.version
    elif kind == "turn_context":
        acc.model = _str(payload.get("model")) or acc.model
        acc.cwd = _str(payload.get("cwd")) or acc.cwd
    elif kind == "response_item":
        if counted and payload.get("type") == "message" and payload.get("role") == "assistant" and _blocks(payload.get("content")):
            acc.assistant_msgs += 1
    elif kind == "token_usage_record" and counted:
        usage = _dict(payload.get("usage"))
        acc.tokens_in += _int(usage.get("input_tokens"))
        acc.tokens_out += _int(usage.get("output_tokens"))
    prompts = _user_prompts(rec)
    if prompts and counted:
        acc.prompts += len(prompts)
        acc.title = acc.title or _shorten(prompts[0])
    if ts is not None:
        acc.t_start = ts if acc.t_start is None else min(acc.t_start, ts)
        acc.t_end = ts if acc.t_end is None else max(acc.t_end, ts)
        if kind in ("response_item", "event_msg") and counted:
            add_to_segment(
                acc, ts, gap_s=gap_s, prompt=bool(prompts), first_prompt=" ".join(prompts[0].split())[:200] if prompts else None
            )


def parse_codex_lines(lines: Iterable[bytes], acc: SessionSummary, *, gap_s: int = DEFAULT_SEGMENT_GAP_S) -> SessionSummary:
    """Fold rollout lines into a copy of `acc`; malformed or unrecognized lines are skipped."""
    out = acc.model_copy(deep=True)
    for line in lines:
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if isinstance(rec, dict):
            _apply(out, rec, gap_s=gap_s)
    return out  # no session_id until the session_meta record is read: not a session (yet)


def digest_codex(path: Path, *, max_chars: int = 12000, window: tuple[datetime, datetime] | None = None) -> str:
    """A compact Markdown digest of a rollout: prompts, tool calls, assistant messages, final message.

    Streams the file once and skips lines it cannot read; `OSError` from opening or reading
    the file propagates. The raw JSONL never leaves this function.
    """
    return digest_codex_windows(path, [window], max_chars=max_chars)[0]


def _codex_digest_record(parts: DigestParts, rec: dict[str, Any]) -> None:
    payload = _dict(rec.get("payload"))
    parts.prompts.extend(" ".join(t.split())[:300] for t in _user_prompts(rec))
    if rec.get("type") == "response_item":
        if payload.get("type") in ("function_call", "custom_tool_call"):
            parts.tools[_str(payload.get("name")) or "unknown"] += 1
        elif payload.get("type") == "message" and payload.get("role") == "assistant":
            for text in _blocks(payload.get("content")):
                parts.assistant_text(text)
    elif rec.get("type") == "event_msg" and payload.get("type") == "task_complete":
        parts.final = (_str(payload.get("last_agent_message")) or "").strip() or parts.final


def digest_codex_windows(
    path: Path, windows: Sequence[tuple[datetime, datetime] | None], *, max_chars: int = 12000
) -> list[str]:
    """`digest_codex` for several windows of one rollout, reading the file once."""
    return digest_windows(path, windows, max_chars=max_chars, add=_codex_digest_record)


def digest_session(
    backend: str, path: Path, *, max_chars: int, window: tuple[datetime, datetime] | None = None
) -> str:
    """The digest for a session of `backend`: a rollout for codex, a Claude-format transcript otherwise."""
    return digest_session_windows(backend, path, [window], max_chars=max_chars)[0]


def digest_session_windows(
    backend: str, path: Path, windows: Sequence[tuple[datetime, datetime] | None], *, max_chars: int
) -> list[str]:
    """One digest per window of a session of `backend`, from a single read of its file."""
    many = digest_codex_windows if backend == "codex" else digest_transcript_windows
    return many(path, windows, max_chars=max_chars)


def session_signal(conn: apsw.Connection, session_id: str) -> Signal | None:
    """The earliest-segment `session` signal for `session_id` (Claude- or Codex-format), or None."""
    found = session_signals_for(conn, session_id)
    return found[0] if found else None


_FIRST_LINE_BYTES = 64 * 1024
_FIRST_LINE_MAX_BYTES = 1024 * 1024  # a session_meta carrying long instructions


def _own_session_cwd(path: Path) -> str | None:
    """The `cwd` of the file's first record when that is the `session_meta` of the rollout's own
    session (not a child rollout's); a bounded read: 64 KiB, extended to 1 MiB for a longer line."""
    try:
        with path.open("rb") as f:
            line = f.readline(_FIRST_LINE_BYTES)
            if len(line) == _FIRST_LINE_BYTES and not line.endswith(b"\n"):
                line += f.readline(_FIRST_LINE_MAX_BYTES - _FIRST_LINE_BYTES)
                if len(line) == _FIRST_LINE_MAX_BYTES and not line.endswith(b"\n"):
                    return None
    except OSError:
        return None
    try:
        rec = json.loads(line)
    except Exception:  # anything a hostile first line can raise (RecursionError included)
        return None
    if not isinstance(rec, dict) or rec.get("type") != "session_meta":
        return None
    payload = _dict(rec.get("payload"))
    if _str(payload.get("id")) is None or _str(payload.get("id")) != _name_id(path):
        return None
    return _str(payload.get("cwd"))


def project_codex_sessions(
    settings: Settings,
    folder: str,
    *,
    registered: Sequence[str],
    before: datetime | None = None,
    before_by_source: Mapping[str, datetime] | None = None,
    max_files: int = MAX_TRANSCRIPT_FILES,
) -> ProjectTranscripts:
    """Codex counterpart of `project_transcripts`: summaries of the rollouts whose cwd belongs to
    `folder` (a host path) by the deepest-registered-folder rule, newest first. Same `before`,
    `before_by_source` (needs a `codex` entry, else nothing is read) and `max_files` meaning.
    Rollouts are not grouped by folder on disk, so candidates are first narrowed by the `cwd` in each
    file's first record (`session_meta`, one bounded read); a file without a parsable one, or a child
    rollout, is skipped, and a later `turn_context` cwd change is not seen by this pre-filter. The cap then counts only
    the rollouts in this folder. No DB, no lock."""
    if before_by_source is not None and "codex" not in before_by_source:
        return ProjectTranscripts(sessions=[])
    candidates: list[tuple[float, str, Path]] = []
    for path in codex_files(settings):
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        cwd = _own_session_cwd(path)
        if cwd is not None and cwd_in_folder(cwd, folder, registered):
            candidates.append((mtime, "codex", path))
    return select_in_folder(
        candidates,
        folder,
        registered,
        before=before,
        before_by_source=before_by_source,
        max_files=max_files,
        parse=parse_codex_lines,
    )


def scan_codex(conn: apsw.Connection, settings: Settings, *, now: datetime) -> ScanReport:
    """Bring every recent rollout's summary up to date and upsert its session signal.

    Like `scan_transcripts`: all DB work on `conn` in one transaction; callers take the ledger lock.
    """
    cutoff = transcript_cutoff(conn, settings, now, "codex")
    files_seen = files_read = 0
    summaries: list[tuple[SessionSummary, float]] = []
    with conn:
        for path in codex_files(settings):
            try:
                st = path.stat()
            except OSError:
                continue
            if cutoff is not None and st.st_mtime < cutoff:
                continue
            files_seen += 1
            refreshed = refresh_file(conn, path, st, "codex", parse_codex_lines, gap_s=settings.segment_gap_s)
            if refreshed is None:
                continue
            files_read += refreshed[1]
            if not is_own_session(refreshed[0]):
                continue  # a child rollout (or one whose session_meta is not written yet)
            summaries.append((refreshed[0], st.st_mtime))
        summaries = reparse_continuations(conn, settings, summaries, parse=parse_codex_lines)
        emitted: dict[str, set[str]] = {}
        signals, live = session_signals(conn, settings, summaries, now, emitted)
        written = upsert_signals(conn, signals)
        prune_session_signals(conn, emitted)  # only the rollouts processed in this pass
    return ScanReport(files_seen=files_seen, files_read=files_read, signals_written=written, live=live)

