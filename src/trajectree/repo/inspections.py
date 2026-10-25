"""Inspection watermark and due projects.

The watermark is machine-written and lives in the untracked `state` table (key `inspect:<project id>`),
so advancing it creates no ledger transaction.
"""

import json
from datetime import datetime, timedelta
from typing import Any

import apsw

from trajectree.config import Settings
from trajectree.context.stretches import merge_stretches
from trajectree.db.schema import table_columns
from trajectree.models import InspectionDue, Project, Run, Signal, Stretch, Watermark
from trajectree.repo import projects
from trajectree.repo.runs import find_active
from trajectree.repo.sessions import running_sessions
from trajectree.repo.state import get_state, set_state
from trajectree.timeutil import parse_iso, to_db

STATE_PREFIX = "inspect:"
_KINDS = ["session", "commits", "files_changed"]


def get_watermark(conn: apsw.Connection, project_id: str) -> Watermark:
    raw = get_state(conn, STATE_PREFIX + project_id)
    if not isinstance(raw, dict):
        return Watermark()
    try:
        return Watermark.model_validate(raw)
    except ValueError:
        return Watermark()


def set_watermark(conn: apsw.Connection, project_id: str, wm: Watermark) -> None:
    with conn:
        set_state(conn, STATE_PREFIX + project_id, wm.model_dump(mode="json"))


def _end_text(value: str | datetime) -> str:
    """A signal end in the stored text form, so ISO spellings of one instant compare equal."""
    try:
        return to_db(value if isinstance(value, datetime) else parse_iso(value))
    except ValueError:
        return str(value)


def uninspected_signals(conn: apsw.Connection, project_id: str, wm: Watermark) -> list[Signal]:
    """Activity signals the last inspection did not cover, oldest first; never truncated.

    With an empty watermark every signal counts. Otherwise a signal counts when it ends after
    `inspected_at`, when its key is not in `covered`, or when `covered` holds a different end for it.
    """
    cols = table_columns(conn, "signal")
    marks = ", ".join("?" * len(_KINDS))
    base = f"FROM signal WHERE project_id = ? AND kind IN ({marks})"
    params: list[Any] = [project_id, *_KINDS]
    if wm.inspected_at is None:
        sql = f"SELECT {', '.join(cols)} {base} ORDER BY t_start, id"
        rows = list(conn.execute(sql, params))
    else:
        mark = to_db(wm.inspected_at)
        end = "coalesce(t_end, t_start)"
        covered = {k: _end_text(v) for k, v in wm.covered.items()}
        ids = [
            r[0]
            for r in conn.execute(f"SELECT id, dedupe_key, {end} {base} AND {end} <= ?", [*params, mark])
            if covered.get(r[1]) != _end_text(r[2])
        ]
        rows = list(
            conn.execute(
                f"SELECT {', '.join(cols)} {base} AND {end} > ? ORDER BY t_start, id", [*params, mark]
            )
        )
        for i in range(0, len(ids), 500):
            chunk = ids[i : i + 500]
            rows += conn.execute(
                f"SELECT {', '.join(cols)} FROM signal WHERE id IN ({', '.join('?' * len(chunk))})", chunk
            ).fetchall()
        rows.sort(key=lambda r: (r[cols.index("t_start")], r[cols.index("id")]))
    out = []
    for row in rows:
        data = dict(zip(cols, row))
        data["payload"] = json.loads(data["payload"])
        out.append(Signal.model_validate(data))
    return out


def project_is_live(conn: apsw.Connection, settings: Settings, project_id: str, *, now: datetime) -> bool:
    since = now - timedelta(seconds=settings.live_window_s)
    if any(s.project_id == project_id for s in running_sessions(conn, since=since)):
        return True
    scanned = get_state(conn, "live_sessions")
    return isinstance(scanned, list) and any(
        isinstance(e, dict) and e.get("project_id") == project_id for e in scanned
    )


def uninspected_stretches(
    conn: apsw.Connection, settings: Settings, project: Project, *, now: datetime, live_check: bool = True
) -> list[Stretch]:
    """Settled stretches (quiet for a full gap) not yet inspected; none while a session is live
    (unless `live_check` is False: a running series checks its episodes against the settled ones)."""
    if live_check and project_is_live(conn, settings, project.id, now=now):
        return []
    wm = get_watermark(conn, project.id)
    stretches = merge_stretches(
        uninspected_signals(conn, project.id, wm), gap_s=settings.segment_gap_s, inspected_at=wm.inspected_at
    )
    settled = now - timedelta(seconds=settings.segment_gap_s)
    return [s for s in stretches if s.t_end <= settled]


def episode_material(
    conn: apsw.Connection,
    settings: Settings,
    project_id: str,
    *,
    start: datetime | None,
    end: datetime | None,
) -> tuple[list[Stretch], dict[str, Signal]]:
    """The uninspected stretches that start inside `start`..`end` (either bound open when None),
    with their signals by id: what one `inspect` run reads."""
    wm = get_watermark(conn, project_id)
    signals = uninspected_signals(conn, project_id, wm)
    stretches = [
        st
        for st in merge_stretches(signals, gap_s=settings.segment_gap_s, inspected_at=wm.inspected_at)
        if (start is None or st.t_start >= start) and (end is None or st.t_start <= end)
    ]
    wanted = {sid for st in stretches for sid in st.signal_ids}
    return stretches, {s.id: s for s in signals if s.id in wanted}


def covered_map(signals: list[Signal]) -> dict[str, str]:
    """Signal dedupe_key -> its end in stored text form, the entries a watermark's `covered` keeps."""
    return {s.dedupe_key: _end_text(s.t_end or s.t_start) for s in signals}


def reset_watermark_since(conn: apsw.Connection, project_id: str, at: datetime) -> Watermark:
    """Re-inspect from `at`: `inspected_at = at`, and `covered` replaced by every activity signal of
    the project ending at or before `at`, so the history before it stays inspected."""
    marks = ", ".join("?" * len(_KINDS))
    end = "coalesce(t_end, t_start)"
    rows = conn.execute(
        f"SELECT dedupe_key, {end} FROM signal WHERE project_id = ? AND kind IN ({marks}) AND {end} <= ?",
        [project_id, *_KINDS, to_db(at)],
    )
    wm = Watermark(inspected_at=at, covered={key: _end_text(value) for key, value in rows})
    set_watermark(conn, project_id, wm)
    return wm


def advance_watermark(
    conn: apsw.Connection,
    project_id: str,
    *,
    inspected_at: datetime,
    started_at: datetime | None,
    series_id: str | None,
    covered: dict[str, str],
) -> Watermark:
    """After an ok `inspect` run: move `inspected_at` forward (never back) and add what the run covered."""
    wm = get_watermark(conn, project_id)
    if wm.inspected_at is not None and wm.inspected_at > inspected_at:
        inspected_at = wm.inspected_at
    new = Watermark(
        inspected_at=inspected_at,
        started_at=started_at,
        series_id=series_id,
        covered={**wm.covered, **{k: _end_text(v) for k, v in covered.items()}},
    )
    set_watermark(conn, project_id, new)
    return new


INSPECT_KINDS = ("inspect", "inspect_boundaries")


def active_inspection(conn: apsw.Connection, project_id: str) -> Run | None:
    """The project's queued or running `inspect` or `inspect_boundaries` run, if any."""
    for kind in INSPECT_KINDS:
        run = find_active(conn, project_id, kind)
        if run is not None:
            return run
    return None


def due_projects(conn: apsw.Connection, settings: Settings, *, now: datetime) -> list[InspectionDue]:
    out: list[InspectionDue] = []
    for p in projects.list_projects(conn, status="active"):
        st = uninspected_stretches(conn, settings, p, now=now)
        if not st:
            continue
        active = active_inspection(conn, p.id)
        out.append(
            InspectionDue(
                project_id=p.id,
                slug=p.slug,
                title=p.title,
                stretches=len(st),
                first_day=st[0].t_start.strftime("%Y-%m-%d"),
                last_day=st[-1].t_end.strftime("%Y-%m-%d"),
                sessions=sum(s.sessions for s in st),
                commits=sum(s.commits for s in st),
                file_days=sum(s.file_days for s in st),
                active_run=active.id if active else None,
                # A boundary pass has no episode yet (0 in its props): reported as None.
                episode=(active.props.get("episode_no") or None) if active else None,
                episodes=(active.props.get("episodes") or None) if active else None,
            )
        )
    return out
