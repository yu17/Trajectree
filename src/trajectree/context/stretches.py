"""Group a project's activity signals into stretches (pure)."""

from datetime import datetime, timedelta
from pathlib import Path

from pydantic import BaseModel

from trajectree.config import Settings
from trajectree.connectors.codex_sessions import digest_session
from trajectree.context.digest import _line, _one_line
from trajectree.models import Edge, Event, Signal, Stretch

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


def _day(t: datetime) -> str:
    return t.strftime("%Y-%m-%d")


def _span(s: Signal) -> str:
    return f"{s.t_start.strftime('%H:%M')}–{(s.t_end or s.t_start).strftime('%H:%M')}"


def _count(payload: dict, key: str) -> int:
    n = payload.get(key)
    return n if isinstance(n, int) and not isinstance(n, bool) else 0


def _session_digest(s: Signal, settings: Settings) -> str:
    """The segment's windowed digest; empty when the transcript is missing or unreadable."""
    path = s.payload.get("transcript_path")
    if not path:
        return ""
    try:
        return digest_session(
            str(s.payload.get("backend") or "claude"), Path(path),
            max_chars=settings.digest_max_chars, window=(s.t_start, s.t_end or s.t_start),
        )
    except (OSError, ValueError):
        return ""


def estimate_sizes(
    stretches: list[Stretch], signals_by_id: dict[str, Signal], settings: Settings
) -> dict[str, int]:
    """Signal id -> length of its windowed digest, for every session segment (reads transcripts)."""
    out: dict[str, int] = {}
    for st in stretches:
        for sid in st.signal_ids:
            s = signals_by_id.get(sid)
            if s is not None and s.kind == "session":
                out[sid] = len(_session_digest(s, settings))
    return out


def render_segment_lines(
    stretches: list[Stretch], signals_by_id: dict[str, Signal], sizes: dict[str, int]
) -> str:
    """One compact line per segment, in stretch order; reads no transcripts."""
    lines: list[str] = []
    for st in stretches:
        for sid in st.signal_ids:
            s = signals_by_id.get(sid)
            if s is None:
                continue
            if s.kind != "session":
                lines.append(_line(s))
                continue
            p = s.payload
            name = _one_line(p.get("title") or str(p.get("session_id") or "")[:8])
            lines.append(
                f"- {_day(s.t_start)} {_span(s)} {p.get('backend') or 'claude'} session \"{name}\""
                f" {_count(p, 'prompts')} prompts (~{sizes.get(sid, 0)} chars): \"{_one_line(p.get('first_prompt') or '')}\""
            )
    return "\n".join(lines)


def overlapping_blocks(stretch: Stretch, blocks: list[Event], *, gap_s: int) -> list[Event]:
    """Blocks of the stretch's project near it in time, plus the three most recent blocks; oldest first."""
    gap = timedelta(seconds=gap_s)
    mine = [b for b in blocks if b.tier == "block" and b.project_id == stretch.project_id]
    near = [b for b in mine if (b.t_end or b.t_start) >= stretch.t_start - gap and b.t_start <= stretch.t_end + gap]
    recent = sorted(mine, key=lambda b: (b.t_end or b.t_start, b.id))[-3:]
    seen = {b.id for b in near}
    merged = near + [b for b in recent if b.id not in seen]
    return sorted(merged, key=lambda b: (b.t_end or b.t_start, b.id))


def render_blocks(blocks: list[Event], edges: list[Edge], events_by_id: dict[str, Event]) -> str:
    """One line per block: end day, title, id, kind/tier, and its pills (`part_of` edges into the block)."""
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
        lines.append(f"- {_day(b.t_end or b.t_start)} {_one_line(b.title)} ({b.id}) [{b.kind}/{b.tier}]{tail}")
    return "\n".join(lines)


def render_stretches(
    stretches: list[Stretch],
    signals_by_id: dict[str, Signal],
    *,
    blocks: list[Event],
    edges: list[Edge],
    settings: Settings,
    budget_chars: int,
) -> tuple[str, int]:
    """Markdown for the stretches, cut at `budget_chars` with a marker; also the un-truncated size.

    `blocks` is the project's events (blocks and pills; pills are looked up for their blocks).
    Reads transcripts: call outside the ledger lock.
    """
    events_by_id = {e.id: e for e in blocks}
    chunks: list[tuple[str, bool]] = []  # (text, is a segment)
    for k, st in enumerate(stretches, 1):
        head = [f"### Stretch {k}: {_day(st.t_start)} → {_day(st.t_end)}"]
        if st.inspected_up_to is not None:
            head.append(
                f"Inspected up to {_day(st.inspected_up_to)}: the block that covers this part already exists."
            )
        near = overlapping_blocks(st, blocks, gap_s=settings.segment_gap_s)
        head.append("Overlapping blocks:")
        head.append(render_blocks(near, edges, events_by_id) if near else "- (none)")
        head.append("Segments:")
        chunks.append(("\n".join(head), False))
        for sid in st.signal_ids:
            s = signals_by_id.get(sid)
            if s is None:
                continue
            if s.kind != "session":
                chunks.append((_line(s), True))
                continue
            p = s.payload
            name = _one_line(p.get("title") or str(p.get("session_id") or "")[:8])
            line = (
                f"- {_day(s.t_start)} {_span(s)} {p.get('backend') or 'claude'} session \"{name}\""
                f" segment {_count(p, 'segment')}/{_count(p, 'segments')}, {_count(p, 'prompts')} prompts,"
                f" {_one_line(p.get('model') or 'unknown model')}"
            )
            chunks.append((f'{line}\n<data name="digest {s.id}">\n{_session_digest(s, settings)}\n</data>', True))
    full = "\n".join(c for c, _ in chunks)
    if len(full) <= budget_chars:
        return full, len(full)
    kept: list[str] = []
    used = 0
    skipped = 0
    cut = False
    for text, is_segment in chunks:
        if not cut and used + len(text) + 1 <= budget_chars:
            kept.append(text)
            used += len(text) + 1
            continue
        cut = True
        skipped += is_segment
    kept.append(f"(… {skipped} more segments not shown)")
    return "\n".join(kept), len(full)
