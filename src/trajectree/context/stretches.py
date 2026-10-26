"""Group a project's activity signals into stretches (pure), and render them for inspection prompts.

Every day and time shown to an agent is in the owner's time zone (`settings.tz`), the zone the
episode answers are read in.
"""

import re
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import BaseModel

from trajectree.config import Settings
from trajectree.connectors.codex_sessions import digest_session_windows
from trajectree.context.digest import _line as _raw_line
from trajectree.context.digest import _one_line as _raw_one_line
from trajectree.models import Edge, Event, Signal, Stretch

_DATA_TAG = re.compile(r"<(/?data)", re.IGNORECASE)
BODY_CHARS = 1200  # of a block's body shown under its line
FIRST_PROMPTS = 3  # sessions whose first prompt a compact stretch line quotes


def fence_safe(text: str) -> str:
    """Neutralise any `<data` / `</data` in transcript- or event-derived text so it cannot close a data fence."""
    return _DATA_TAG.sub(r"&lt;\1", text)


def _one_line(value: object, limit: int = 120) -> str:
    return fence_safe(_raw_one_line(value, limit))


def _line(s: Signal, tz: str = "UTC") -> str:
    return fence_safe(_raw_line(s, tz))

_COUNTED = {"session": "sessions", "commits": "commits", "files_changed": "file_days"}


def merge_stretches(
    signals: list[Signal], *, gap_s: int, inspected_at: datetime | None = None
) -> list[Stretch]:
    """Merge signals whose start lies within `gap_s` of the running end into stretches.

    Each signal is one segment; `session`, `commits` and `files_changed` also feed their own count.
    `inspected_up_to` is `inspected_at` when it falls strictly inside the stretch.
    """
    out: list[Stretch] = []
    for s in sorted(signals, key=lambda s: (s.t_start, s.id)):
        end = s.t_end or s.t_start
        cur = out[-1] if out else None
        if cur is None or s.project_id != cur.project_id or (s.t_start - cur.t_end).total_seconds() > gap_s:
            cur = Stretch(
                project_id=s.project_id, t_start=s.t_start, t_end=end,
                segments=0, sessions=0, commits=0, file_days=0, signal_ids=[],
            )
            out.append(cur)
        cur.t_end = max(cur.t_end, end)
        cur.segments += 1
        counter = _COUNTED.get(s.kind)
        if counter:
            setattr(cur, counter, getattr(cur, counter) + 1)
        cur.signal_ids.append(s.id)
    if inspected_at is not None:
        for st in out:
            if st.t_start < inspected_at < st.t_end:
                st.inspected_up_to = inspected_at
    return out


class SegmentRef(BaseModel):
    """A segment signal with the size of its windowed digest, for the boundary pass."""

    signal: Signal
    digest_chars: int


def _local(t: datetime, tz: str) -> datetime:
    return t.astimezone(ZoneInfo(tz))


def _day(t: datetime, tz: str = "UTC") -> str:
    return _local(t, tz).strftime("%Y-%m-%d")


def _span(s: Signal, tz: str = "UTC") -> str:
    return f"{_local(s.t_start, tz).strftime('%H:%M')}–{_local(s.t_end or s.t_start, tz).strftime('%H:%M')}"


def _count(payload: dict, key: str) -> int:
    n = payload.get(key)
    return n if isinstance(n, int) and not isinstance(n, bool) else 0


def _digests(signals: list[Signal], settings: Settings) -> dict[str, str]:
    """Signal id -> its segment's windowed digest (fence-safe), reading each transcript once; empty
    when the transcript is missing or unreadable."""
    groups: dict[tuple[str, str], list[Signal]] = {}
    for s in signals:
        path = s.payload.get("transcript_path")
        if s.kind == "session" and isinstance(path, str) and path:
            groups.setdefault((str(s.payload.get("backend") or "claude"), path), []).append(s)
    out = {s.id: "" for s in signals if s.kind == "session"}
    for (backend, path), group in groups.items():
        try:
            texts = digest_session_windows(
                backend, Path(path), [(s.t_start, s.t_end or s.t_start) for s in group], max_chars=settings.digest_max_chars
            )
        except (OSError, ValueError):
            continue
        for s, text in zip(group, texts):
            out[s.id] = fence_safe(text)
    return out


def _stretch_signals(stretches: list[Stretch], signals_by_id: dict[str, Signal]) -> list[Signal]:
    return [signals_by_id[sid] for st in stretches for sid in st.signal_ids if sid in signals_by_id]


def estimate_sizes(
    stretches: list[Stretch], signals_by_id: dict[str, Signal], settings: Settings
) -> dict[str, int]:
    """Signal id -> length of its windowed digest, for every session segment (reads each transcript once)."""
    return {sid: len(text) for sid, text in _digests(_stretch_signals(stretches, signals_by_id), settings).items()}


def _session_name(p: dict) -> str:
    return _one_line(p.get("title") or str(p.get("session_id") or "")[:8])


def _segment_lines(stretches: list[Stretch], signals_by_id: dict[str, Signal], sizes: dict[str, int], tz: str) -> list[str]:
    lines: list[str] = []
    for st in stretches:
        for sid in st.signal_ids:
            s = signals_by_id.get(sid)
            if s is None:
                continue
            if s.kind != "session":
                lines.append(_line(s, tz))
                continue
            p = s.payload
            lines.append(
                f"- {_day(s.t_start, tz)} {_span(s, tz)} {_one_line(p.get('backend') or 'claude')} session \"{_session_name(p)}\""
                f" {_count(p, 'prompts')} prompts (~{sizes.get(sid, 0)} chars): \"{_one_line(p.get('first_prompt') or '')}\""
            )
    return lines


def _stretch_line(st: Stretch, signals_by_id: dict[str, Signal], sizes: dict[str, int], tz: str) -> str:
    sessions = [s for sid in st.signal_ids if (s := signals_by_id.get(sid)) is not None and s.kind == "session"]
    chars = sum(sizes.get(s.id, 0) for s in sessions)
    prompts = [_one_line(s.payload.get("first_prompt")) for s in sessions[:FIRST_PROMPTS] if s.payload.get("first_prompt")]
    return (
        f"- {_day(st.t_start, tz)}..{_day(st.t_end, tz)} {st.segments} segments, {st.sessions} sessions,"
        f" {st.commits} commits, {st.file_days} file days (~{chars} chars): \"{' / '.join(prompts)}\""
    )


def render_segment_lines(
    stretches: list[Stretch],
    signals_by_id: dict[str, Signal],
    sizes: dict[str, int],
    *,
    tz: str = "UTC",
    budget_chars: int | None = None,
) -> str:
    """One compact line per segment, in stretch order; reads no transcripts.

    Over `budget_chars` the timeline is compacted to one line per stretch, and when that is still
    over, cut with a `(… N more stretches not shown)` marker."""
    full = "\n".join(_segment_lines(stretches, signals_by_id, sizes, tz))
    if budget_chars is None or len(full) <= budget_chars:
        return full
    compact = [_stretch_line(st, signals_by_id, sizes, tz) for st in stretches]
    text = "\n".join(compact)
    if len(text) <= budget_chars:
        return text
    for kept in range(len(compact) - 1, -1, -1):
        text = "\n".join([*compact[:kept], f"(… {len(compact) - kept} more stretches not shown)"])
        if len(text) <= budget_chars:
            return text
    return text  # even the marker alone is over a tiny budget


def overlapping_blocks(stretch: Stretch, blocks: list[Event], *, gap_s: int) -> list[Event]:
    """Blocks of the stretch's project near it in time, plus the three most recent blocks; oldest first."""
    gap = timedelta(seconds=gap_s)
    mine = [b for b in blocks if b.tier == "block" and b.project_id == stretch.project_id]
    near = [b for b in mine if (b.t_end or b.t_start) >= stretch.t_start - gap and b.t_start <= stretch.t_end + gap]
    recent = sorted(mine, key=lambda b: (b.t_end or b.t_start, b.id))[-3:]
    seen = {b.id for b in near}
    merged = near + [b for b in recent if b.id not in seen]
    return sorted(merged, key=lambda b: (b.t_end or b.t_start, b.id))


def _body(b: Event) -> str:
    """The block's body, fence-safe, cut at `BODY_CHARS` with `…`, each line indented two spaces."""
    body = (b.body_md or "").strip()
    if not body:
        return ""
    if len(body) > BODY_CHARS:
        body = body[:BODY_CHARS].rstrip() + "…"
    return "\n".join(f"  {line}".rstrip() for line in fence_safe(body).splitlines())


def render_blocks(
    blocks: list[Event], edges: list[Edge], events_by_id: dict[str, Event], *, tz: str = "UTC"
) -> str:
    """One line per block: end day, title, id, kind/tier, and its pills (`part_of` edges into the
    block, titles only); its body follows, indented."""
    lines: list[str] = []
    for b in blocks:
        pills = sorted(
            (
                events_by_id[e.src_id]
                for e in edges
                if e.rel == "part_of" and e.dst_type == "event" and e.dst_id == b.id
                and e.src_type == "event" and e.src_id in events_by_id
            ),
            key=lambda x: (x.t_start, x.id),
        )
        tail = f" pills: {'; '.join(f'{_one_line(p.title)} ({p.id})' for p in pills)}" if pills else ""
        lines.append(f"- {_day(b.t_end or b.t_start, tz)} {_one_line(b.title)} ({b.id}) [{fence_safe(b.kind)}/{b.tier}]{tail}")
        if body := _body(b):
            lines.append(body)
    return "\n".join(lines)


def _chunks(
    stretches: list[Stretch],
    signals_by_id: dict[str, Signal],
    *,
    blocks: list[Event],
    edges: list[Edge],
    settings: Settings,
    digest: Callable[[Signal], str],
) -> list[tuple[str, str | None]]:
    """The rendered pieces: (text, the segment's signal id; None for a stretch header)."""
    tz = settings.tz
    events_by_id = {e.id: e for e in blocks}
    chunks: list[tuple[str, str | None]] = []
    for k, st in enumerate(stretches, 1):
        head = [f"### Stretch {k}: {_day(st.t_start, tz)} → {_day(st.t_end, tz)}"]
        if st.inspected_up_to is not None:
            head.append(
                f"Inspected up to {_day(st.inspected_up_to, tz)}: the block that covers this part already exists."
            )
        near = overlapping_blocks(st, blocks, gap_s=settings.segment_gap_s)
        head.append("Overlapping blocks:")
        head.append(render_blocks(near, edges, events_by_id, tz=tz) if near else "- (none)")
        head.append("Segments:")
        chunks.append(("\n".join(head), None))
        for sid in st.signal_ids:
            s = signals_by_id.get(sid)
            if s is None:
                continue
            if s.kind != "session":
                chunks.append((_line(s, tz), sid))
                continue
            p = s.payload
            line = (
                f"- {_day(s.t_start, tz)} {_span(s, tz)} {_one_line(p.get('backend') or 'claude')} session \"{_session_name(p)}\""
                f" segment {_count(p, 'segment')}/{_count(p, 'segments')}, {_count(p, 'prompts')} prompts,"
                f" {_one_line(p.get('model') or 'unknown model')}"
            )
            chunks.append((f'{line}\n<data name="digest {s.id}">\n{digest(s)}\n</data>', sid))
    return chunks


def estimate_render_size(
    stretches: list[Stretch],
    signals_by_id: dict[str, Signal],
    sizes: dict[str, int],
    *,
    blocks: list[Event],
    edges: list[Edge],
    settings: Settings,
) -> int:
    """The un-truncated length `render_stretches` would return, from the digest `sizes` (see
    `estimate_sizes`); renders headers and lines only, so it reads no transcripts."""
    chunks = _chunks(stretches, signals_by_id, blocks=blocks, edges=edges, settings=settings, digest=lambda s: "")
    return len("\n".join(c for c, _ in chunks)) + sum(
        sizes.get(sid, 0) for _, sid in chunks if sid is not None and signals_by_id[sid].kind == "session"
    )


def render_stretches(
    stretches: list[Stretch],
    signals_by_id: dict[str, Signal],
    *,
    blocks: list[Event],
    edges: list[Edge],
    settings: Settings,
    budget_chars: int,
) -> tuple[str, int, set[str]]:
    """Markdown for the stretches, cut at `budget_chars` with a marker; also the un-truncated size
    and the ids of the signals whose segment line made it into the markdown (a cut one did not,
    so an inspection must not count it as covered).

    `blocks` is the project's events (blocks and pills; pills are looked up for their blocks).
    Reads transcripts (each once): call outside the ledger lock.
    """
    digests = _digests(_stretch_signals(stretches, signals_by_id), settings)
    chunks = _chunks(
        stretches, signals_by_id, blocks=blocks, edges=edges, settings=settings, digest=lambda s: digests.get(s.id, "")
    )
    full = "\n".join(c for c, _ in chunks)
    if len(full) <= budget_chars:
        return full, len(full), {sid for _, sid in chunks if sid is not None}
    kept: list[str] = []
    rendered: set[str] = set()
    used = 0
    skipped = 0
    cut = False
    for text, sid in chunks:
        if not cut and used + len(text) + 1 <= budget_chars:
            kept.append(text)
            used += len(text) + 1
            if sid is not None:
                rendered.add(sid)
            continue
        cut = True
        skipped += sid is not None
    kept.append(f"(… {skipped} more segments not shown)")
    return "\n".join(kept), len(full), rendered
