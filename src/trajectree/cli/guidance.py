from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Callable, TextIO

from trajectree.guidance import render_rules

EXCLUDE_LINE = ".claude/rules/trajectree.md"
ADD_HINT = "claude mcp add --scope user trajectree -- trajectree mcp"
MANAGED_HEADER = "<!-- managed by trajectree init-project; edits are overwritten -->"


def rules_path(path: Path) -> Path:
    return path / ".claude" / "rules" / "trajectree.md"


def _git_dir(path: Path) -> Path | None:
    dot_git = path / ".git"
    if dot_git.is_dir():
        return dot_git
    try:
        first = dot_git.read_text(encoding="utf-8").splitlines()[0]
    except (OSError, UnicodeDecodeError, IndexError):
        return None
    if not first.startswith("gitdir:") or not first[7:].strip():
        return None
    return path / first[7:].strip()  # an absolute pointer replaces the base


def _exclude_file(git_dir: Path) -> Path:
    """`info/exclude` of the common dir: a linked worktree's gitdir names it in `commondir`."""
    commondir = git_dir / "commondir"
    if commondir.is_file():
        try:
            first = commondir.read_text(encoding="utf-8").splitlines()[0].strip()
        except (UnicodeDecodeError, IndexError):
            first = ""
        if first:
            return git_dir / first / "info" / "exclude"  # an absolute path replaces the base
    return git_dir / "info" / "exclude"


def _add_exclude(exclude: Path) -> None:
    exclude.parent.mkdir(parents=True, exist_ok=True)
    existing = exclude.read_text() if exclude.exists() else ""
    if EXCLUDE_LINE in existing.splitlines():
        return
    sep = "" if not existing or existing.endswith("\n") else "\n"
    exclude.write_text(existing + sep + EXCLUDE_LINE + "\n")


def _remove_exclude(exclude: Path) -> None:
    if not exclude.exists():
        return
    lines = exclude.read_text().splitlines(keepends=True)
    kept = [ln for ln in lines if ln.rstrip("\r\n") != EXCLUDE_LINE]
    if len(kept) != len(lines):
        exclude.write_text("".join(kept))


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


def write_rules(path: Path, slug: str, title: str, *, no_exclude: bool, out: TextIO) -> int:
    """Write the rules file and its exclude line; print the `rules:` line. 1 when a write fails."""
    rules = rules_path(path)
    note = ""
    target = rules
    try:
        rules.parent.mkdir(parents=True, exist_ok=True)
        rules.write_text(render_rules(slug, title), encoding="utf-8")
        if (path / ".git").exists() and not no_exclude:
            git_dir = _git_dir(path)
            if git_dir is None:
                note = " (exclude skipped: unreadable .git pointer)"
            else:
                target = git_dir / "commondir"  # named in the error if reading it fails
                target = _exclude_file(git_dir)
                _add_exclude(target)
    except OSError as exc:
        print(
            f"cannot write {target}: {exc} (project and resources were registered)",
            file=sys.stderr,
        )
        return 1
    print(f"rules: {rules}{note}", file=out)
    return 0


def install(
    api: Any,
    path: Path,
    *,
    out: TextIO,
    runner: Callable[[list[str]], Any],
    no_exclude: bool = False,
) -> int:
    from trajectree.cli.main import _Fail

    path = path.resolve()
    folder = next(
        (r for r in api.get("/api/resources") if r["kind"] == "folder" and r["locator"] == str(path)),
        None,
    )
    project = None
    if folder is not None:
        project = next((p for p in api.get("/api/projects") if p["id"] == folder["project_id"]), None)
    if project is None:
        raise _Fail("folder is not a registered project: run trajectree init-project <path>")
    code = write_rules(path, project["slug"], project.get("title") or path.name, no_exclude=no_exclude, out=out)
    if code:
        return code
    print(_ensure_mcp(runner), file=out)
    return 0


def remove(path: Path, *, out: TextIO) -> int:
    path = path.resolve()
    rules = rules_path(path)
    try:
        if rules.exists():
            if not rules.read_text(encoding="utf-8").startswith(MANAGED_HEADER):
                print(f"{rules} is not managed by trajectree; left alone", file=sys.stderr)
                return 1
            rules.unlink()
        git_dir = _git_dir(path) if (path / ".git").exists() else None
        if git_dir is not None:
            _remove_exclude(_exclude_file(git_dir))
    except (OSError, UnicodeDecodeError) as exc:
        print(f"cannot remove guidance: {exc}", file=sys.stderr)
        return 1
    print("guidance: removed", file=out)
    return 0


def show(path: Path, *, out: TextIO, runner: Callable[[list[str]], Any]) -> int:
    path = path.resolve()
    print(f"rules: {'present' if rules_path(path).is_file() else 'absent'}", file=out)
    try:
        configured = runner(["claude", "mcp", "get", "trajectree"]).returncode == 0
    except FileNotFoundError:
        configured = False
    print(f"mcp: {'configured' if configured else 'absent'}", file=out)
    return 0
