"""Per-project SUMMARY.md files under `<data_dir>/exports/projects/<slug>/`."""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

import apsw

from trajectree.config import Settings
from trajectree.context.pack import OPEN_STATES, _day, event_line, resource_line, todo_line
from trajectree.models import Project
from trajectree.repo import events, projects, resources, todos

RECENT_EVENTS = 10


def render_summary_md(conn: apsw.Connection, project: Project, *, now: datetime) -> str:
    p = project
    updated = _day(p.summary_at) if p.summary_at else "never"
    open_todos = todos.list_todos(conn, project_ids=[p.id], states=OPEN_STATES)
    # list_events is ordered by t_start, so the tail is the newest; kept oldest first.
    # Planned future events are not "recent".
    past = [e for e in events.list_events(conn, project_ids=[p.id]) if e.t_start <= now]
    recent = past[-RECENT_EVENTS:]
    sections = [
        ("Summary", [p.summary_md or "_No summary yet._"]),
        ("Open todos", [todo_line(t) for t in open_todos]),
        ("Recent events", [event_line(e) for e in recent]),
        ("Resources", [resource_line(r) for r in resources.list_resources(conn, project_ids=[p.id])]),
    ]
    blocks = [
        f"# {p.title}",
        f"status: {p.status} · slug: {p.slug} · summary updated: {updated}",
        *("\n".join([f"## {heading}", *lines]) for heading, lines in sections),
    ]
    return "\n\n".join(blocks) + "\n"


def render_exports(conn: apsw.Connection, settings: Settings, *, now: datetime) -> list[tuple[Path, str]]:
    """`(path, content)` for every project's SUMMARY.md; reads only, no file access."""
    root = settings.exports_dir / "projects"
    return [(root / p.slug / "SUMMARY.md", render_summary_md(conn, p, now=now)) for p in projects.list_projects(conn)]


def write_files(files: list[tuple[Path, str]]) -> tuple[int, list[str]]:
    """Write each file whose content changed (or that is missing), atomically.

    Returns `(written, errors)`; an OSError on one file is recorded and the rest continue.
    """
    written = 0
    errors: list[str] = []
    for path, content in files:
        tmp = path.with_name(path.name + ".tmp")
        try:
            try:
                if path.read_text(encoding="utf-8") == content:
                    continue
            except FileNotFoundError:
                pass
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(content, encoding="utf-8")
            os.replace(tmp, path)
            written += 1
        except OSError as exc:
            errors.append(f"{path}: {exc}")
            tmp.unlink(missing_ok=True)
    return written, errors


def write_exports(conn: apsw.Connection, settings: Settings, *, now: datetime) -> tuple[int, list[str]]:
    return write_files(render_exports(conn, settings, now=now))
