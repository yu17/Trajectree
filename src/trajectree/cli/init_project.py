from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any, Callable, TextIO

from trajectree.guidance import render_rules
from trajectree.models import SLUG_PATTERN

EXCLUDE_LINE = ".claude/rules/trajectree.md"
ADD_HINT = "claude mcp add --scope user trajectree -- trajectree mcp"


def slug_from_path(path: Path) -> str:
    return re.sub(r"[^a-z0-9]+", "-", path.name.lower()).strip("-")[:64]


def _add_exclude(git_dir: Path) -> None:
    exclude = git_dir / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    existing = exclude.read_text() if exclude.exists() else ""
    if EXCLUDE_LINE in existing.splitlines():
        return
    sep = "" if not existing or existing.endswith("\n") else "\n"
    exclude.write_text(existing + sep + EXCLUDE_LINE + "\n")


def _ensure_mcp(runner: Callable[[list[str]], Any]) -> str:
    try:
        if runner(["claude", "mcp", "get", "trajectree"]).returncode == 0:
            return "mcp: already configured"
        added = runner(["claude", "mcp", "add", "--scope", "user", "trajectree", "--", "trajectree", "mcp"])
    except FileNotFoundError:
        return f"mcp: claude not found; run: {ADD_HINT}"
    if added.returncode != 0:
        return f"mcp: failed (claude mcp add exited {added.returncode}); run: {ADD_HINT}"
    return "mcp: configured"


def init_project(
    api: Any,
    path: Path,
    *,
    slug: str | None,
    title: str | None,
    kind: str,
    runner: Callable[[list[str]], Any],
    out: TextIO,
    no_exclude: bool = False,
) -> int:
    path = path.resolve()
    slug = slug or slug_from_path(path)
    if not re.match(SLUG_PATTERN, slug):
        print(f"invalid slug: {slug}", file=sys.stderr)
        return 2
    project = next((p for p in api.get("/api/projects") if p["slug"] == slug), None)
    if project is None:
        project = api.post(
            "/api/projects", {"slug": slug, "title": title or path.name, "kind": kind}
        )
    pid = project["id"]
    shown_title = project.get("title") or title or path.name

    has_git = (path / ".git").exists()
    kinds = ["folder"] + (["git_local"] if has_git else [])
    for rkind in kinds:
        api.post(
            "/api/resources",
            {"project_id": pid, "kind": rkind, "locator": str(path), "title": path.name},
        )

    rules = path / ".claude" / "rules" / "trajectree.md"
    rules.parent.mkdir(parents=True, exist_ok=True)
    rules.write_text(render_rules(slug, shown_title), encoding="utf-8")
    if has_git and not no_exclude:
        _add_exclude(path / ".git")

    print(f"project: {slug} ({pid})", file=out)
    print(f"resources: {', '.join(kinds)}", file=out)
    print(f"rules: {rules}", file=out)
    print(_ensure_mcp(runner), file=out)
    return 0
