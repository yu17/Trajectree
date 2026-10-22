"""Per-project SUMMARY.md files under `<data_dir>/exports/projects/<slug>/`."""

from __future__ import annotations

from pathlib import Path

import apsw

from trajectree.config import Settings
from trajectree.context.pack import OPEN_STATES, _day, event_line, resource_line, todo_line
from trajectree.models import Project
from trajectree.repo import events, projects, resources, todos

RECENT_EVENTS = 10


def render_summary_md(conn: apsw.Connection, project: Project) -> str:
    p = project
    updated = _day(p.summary_at) if p.summary_at else "never"
    open_todos = todos.list_todos(conn, project_ids=[p.id], states=OPEN_STATES)
    # list_events is ordered by t_start, so the tail is the newest; kept oldest first.
    recent = events.list_events(conn, project_ids=[p.id])[-RECENT_EVENTS:]
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


def render_exports(conn: apsw.Connection, settings: Settings) -> list[tuple[Path, str]]:
    """`(path, content)` for every project's SUMMARY.md; reads only, no file access."""
    root = settings.exports_dir / "projects"
    return [(root / p.slug / "SUMMARY.md", render_summary_md(conn, p)) for p in projects.list_projects(conn)]


def write_files(files: list[tuple[Path, str]]) -> int:
    """Write each file whose content changed (or that is missing); returns how many were written."""
    written = 0
    for path, content in files:
        try:
            if path.read_text(encoding="utf-8") == content:
                continue
        except FileNotFoundError:
            pass
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        written += 1
    return written


def write_exports(conn: apsw.Connection, settings: Settings) -> int:
    return write_files(render_exports(conn, settings))
