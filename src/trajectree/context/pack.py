"""Token-budgeted markdown digest of one or more projects."""

from __future__ import annotations

from datetime import datetime, timezone

import apsw
from pydantic import BaseModel

from trajectree.db import fts
from trajectree.models import Event, Project, Resource, Signal, Todo
from trajectree.repo import events, projects, resources, signals, todos

CHARS_PER_TOKEN = 4
SHARES = {"summary": 40, "events": 30, "todos": 10, "resources": 10, "activity": 10}
HEADINGS = {
    "summary": "Summary",
    "events": "Events",
    "todos": "Open todos",
    "resources": "Resources",
    "activity": "Activity",
}
OPEN_STATES = ["open", "doing"]
ACTIVITY_LINES = 10
HIT_LINES = 20


class ContextPack(BaseModel):
    markdown: str
    tokens_est: int
    dropped: list[str]
    projects: list[str]


def _day(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d")


def cut_lines(lines: list[str], cap: int) -> tuple[list[str], int]:
    """Whole lines while the newline-joined length stays within `cap`; and how many were cut."""
    kept: list[str] = []
    size = 0
    for line in lines:
        size += len(line) + (1 if kept else 0)
        if size > cap:
            break
        kept.append(line)
    return kept, len(lines) - len(kept)


def summary_body(text: str, cap: int) -> tuple[str, int]:
    """The summary cut at `cap` characters; and how many characters were cut."""
    n = max(0, len(text) - cap)
    return (f"{text[:cap]}\n(dropped {n} more)" if n else text), n


def list_body(lines: list[str], cap: int) -> tuple[str, int]:
    kept, n = cut_lines(lines, cap)
    if n:
        kept.append(f"(dropped {n} more)")
    return "\n".join(kept), n


def newest_body(lines: list[str], cap: int) -> tuple[str, int]:
    """Oldest-first `lines`: the newest that fit stay, in order, after a leading drop marker."""
    kept, n = cut_lines(lines[::-1], cap)
    kept.reverse()
    if n:
        kept.insert(0, f"(dropped {n} more)")
    return "\n".join(kept), n


def event_line(e: Event) -> str:
    return f"- {_day(e.t_start)} [{e.kind}/{e.status}] {e.title} ({e.id})"


def todo_line(t: Todo) -> str:
    due = f"due {_day(t.due)}" if t.due else "no due"
    return f"- [ ] {t.title} ({due}) ({t.id})"


def resource_line(r: Resource) -> str:
    return f"- {r.kind} {r.locator}"


def activity_line(s: Signal) -> str:
    p = s.payload
    text = p["title"] if p.get("title") else f"{p['count']} commits" if "count" in p else s.kind
    return f"- {_day(s.t_start)} {s.kind} {text}"


def project_section(
    conn: apsw.Connection, p: Project, share: int, t_from: datetime, t_to: datetime
) -> tuple[str, list[str]]:
    caps = {name: share * pct // 100 for name, pct in SHARES.items()}
    recent = signals.list_signals(conn, project_ids=[p.id], t_from=t_from, t_to=t_to)
    bodies = {
        "summary": summary_body(p.summary_md, caps["summary"]),
        "events": newest_body(
            [event_line(e) for e in events.list_events(conn, project_ids=[p.id], t_from=t_from, t_to=t_to)],
            caps["events"],
        ),
        "todos": list_body(
            [todo_line(t) for t in todos.list_todos(conn, project_ids=[p.id], states=OPEN_STATES)],
            caps["todos"],
        ),
        "resources": list_body(
            [resource_line(r) for r in resources.list_resources(conn, project_ids=[p.id])],
            caps["resources"],
        ),
        "activity": newest_body([activity_line(s) for s in recent[-ACTIVITY_LINES:]], caps["activity"]),
    }
    parts = [f"## {p.title} ({p.slug}) — {p.status}"]
    dropped: list[str] = []
    for name, (body, n) in bodies.items():
        parts.append(f"### {HEADINGS[name]}" + (f"\n{body}" if body else ""))
        if n:
            dropped.append(f"{p.slug}: {name} dropped {n}")
    return "\n\n".join(parts), dropped


def search_section(conn: apsw.Connection, question: str) -> str:
    lines = []
    for hit in fts.search(conn, question, limit=HIT_LINES):
        owner = projects.get_project(conn, hit.project_id) if hit.project_id else None
        lines.append(f"- [{hit.kind}] {hit.title} ({owner.slug if owner else '-'}) ({hit.id})")
    return "\n".join([f'## Search hits for "{question}"', *lines])


def build_context_pack(
    conn: apsw.Connection,
    *,
    project_ids: list[str],
    t_from: datetime,
    t_to: datetime,
    token_budget: int = 4000,
    question: str | None = None,
) -> ContextPack:
    found = [p for p in (projects.get_project(conn, pid) for pid in project_ids) if p is not None]
    share = token_budget * CHARS_PER_TOKEN // max(1, len(project_ids))
    blocks = [f"# Trajectree context ({_day(t_from)} to {_day(t_to)})"]
    dropped: list[str] = []
    for p in found:
        text, cuts = project_section(conn, p, share, t_from, t_to)
        blocks.append(text)
        dropped += cuts
    if question:
        blocks.append(search_section(conn, question))
    markdown = "\n\n".join(blocks) + "\n"
    return ContextPack(
        markdown=markdown,
        tokens_est=len(markdown) // CHARS_PER_TOKEN,
        dropped=dropped,
        projects=[p.slug for p in found],
    )
