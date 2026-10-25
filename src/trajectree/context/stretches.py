"""Group a project's activity signals into stretches (pure)."""

from datetime import datetime

from trajectree.models import Signal, Stretch

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
