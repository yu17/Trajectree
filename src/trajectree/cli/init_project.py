from __future__ import annotations

import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, TextIO

from trajectree.guidance import render_rules
from trajectree.models import SLUG_PATTERN

EXCLUDE_LINE = ".claude/rules/trajectree.md"
ADD_HINT = "claude mcp add --scope user trajectree -- trajectree mcp"


def slug_from_path(path: Path) -> str:
    return re.sub(r"[^a-z0-9]+", "-", path.name.lower()).strip("-")[:64]


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
    ask: Callable[[str], str] = input,
    interactive: bool = False,
    sources: str | None = None,
    since: list[str] | None = None,
    start_model: str | None = None,
    start_runner: str | None = None,
    no_start_block: bool = False,
) -> int:
    path = path.resolve()
    flags = _parse_flags(sources, since)
    if isinstance(flags, str):
        print(flags, file=sys.stderr)
        return 2
    if not path.is_dir():
        print(f"not a directory: {path}", file=sys.stderr)
        return 2
    derived = not slug
    slug = slug or slug_from_path(path)
    if not re.match(SLUG_PATTERN, slug):
        if derived and not slug:
            print(f"invalid slug: '' (derived from '{path.name}'); pass --slug", file=sys.stderr)
        else:
            print(f"invalid slug: {slug}", file=sys.stderr)
        return 2
    project = next((p for p in api.get("/api/projects") if p["slug"] == slug), None)
    if project is None:
        # An empty import record means nothing is picked, so the background ingest imports
        # nothing between this call and the owner's confirmed choices.
        record = {"sources": {}, "recorded_at": datetime.now(timezone.utc).isoformat()}
        project = api.post(
            "/api/projects",
            {"slug": slug, "title": title or path.name, "kind": kind, "props": {"import": record}},
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

    # Probed before the rules file exists, so the file this command writes is not counted as history.
    report: dict[str, Any] | None = None
    probe_error = ""
    if flags[0] != ["none"]:
        from trajectree.cli.main import _Fail

        try:
            report = api.post("/api/import/probe", {"path": str(path)})
        except _Fail as exc:
            probe_error = str(exc)

    rules = path / ".claude" / "rules" / "trajectree.md"
    note = ""
    target = rules
    try:
        rules.parent.mkdir(parents=True, exist_ok=True)
        rules.write_text(render_rules(slug, shown_title), encoding="utf-8")
        if has_git and not no_exclude:
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

    print(f"project: {slug} ({pid})", file=out)
    print(f"resources: {', '.join(kinds)}", file=out)
    print(f"rules: {rules}{note}", file=out)
    print(_ensure_mcp(runner), file=out)
    if probe_error:
        print(probe_error, file=sys.stderr)
        return 2
    if report is None:  # --sources none
        return 0
    return _import_dialogue(
        api, pid, report, flags, out=out, ask=ask, interactive=interactive,
        start_model=start_model, start_runner=start_runner, no_start_block=no_start_block,
    )


class _Abort(Exception):
    """The input ended before the dialogue did."""


_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _check_since(text: str) -> str:
    """`text` when it is `all`, `default` or a date up to today; ValueError(reason) otherwise."""
    if text in ("all", "default"):
        return text
    if not _DATE_RE.fullmatch(text):
        raise ValueError(f"'{text}' is not a date: use YYYY-MM-DD, all or default")
    try:
        parsed = date.fromisoformat(text)
    except ValueError:
        raise ValueError(f"'{text}' is not a real date") from None
    if parsed > date.today():
        raise ValueError(f"{text} is in the future")
    return text


def _parse_flags(sources: str | None, since: list[str] | None) -> tuple[list[str] | None, dict[str, str]] | str:
    """(--sources names or None, {source: since text}) or an error message."""
    names = None
    if sources is not None:
        names = [n.strip() for n in sources.split(",")]
        if "" in names or ("none" in names and len(names) > 1):
            return f"invalid --sources: '{sources}'"
    given: dict[str, str] = {}
    for item in since or []:
        name, eq, value = item.partition("=")
        if not eq:
            return f"invalid --since '{item}': expected <source>=<YYYY-MM-DD|all|default>"
        try:
            given[name] = _check_since(value)
        except ValueError as exc:
            return f"invalid --since '{item}': {exc}"
    return names, given


def _day(stamp: str | None) -> date | None:
    return date.fromisoformat(stamp[:10]) if stamp else None


def _prompt(ask: Callable[[str], str], out: TextIO, text: str, check: Callable[[str], Any]) -> Any:
    """Ask until `check(answer)` returns; its ValueError reason is printed and the question repeats."""
    while True:
        try:
            answer = ask(text).strip()
        except EOFError:
            raise _Abort from None
        try:
            return check(answer)
        except ValueError as exc:
            print(exc, file=out)


def _yes_no(answer: str) -> bool:
    if answer.lower() in ("y", "yes"):
        return True
    if answer.lower() in ("n", "no"):
        return False
    raise ValueError("answer y or n")


def _check_model(answer: str) -> str:
    if not answer:
        raise ValueError("give a model name, or none to skip")
    return answer


def _list_sources(probes: list[dict[str, Any]], out: TextIO) -> None:
    print("history found:", file=out)
    for n, p in enumerate(probes, 1):
        if p["imported"]:
            tail = "(imported)"
        elif p["count"] == 0:
            tail = "(nothing found)" + (f" {p['detail']}" if p["detail"] else "")
        else:
            oldest = _day(p["oldest"])
            tail = f"{p['count']} found" + (f", oldest {oldest}" if oldest else "")
            tail += f" ({p['detail']})" if p["detail"] else ""
        print(f"  {n}. {p['source']}  {tail}", file=out)


def _check_pick(answer: str, probes: list[dict[str, Any]], pickable: list[str]) -> list[str]:
    if answer == "all":
        return list(pickable)
    chosen: list[str] = []
    for token in answer.split(","):
        token = token.strip()
        if not token.isdigit():
            raise ValueError(f"'{token}' is not a source number; use numbers like 1,3 or all")
        n = int(token)
        if not 1 <= n <= len(probes):
            raise ValueError(f"{n} is not between 1 and {len(probes)}")
        probe = probes[n - 1]
        if probe["imported"]:
            raise ValueError(f"{probe['source']} is already imported")
        if probe["source"] not in pickable:
            raise ValueError(f"{probe['source']} has nothing to import")
        chosen.append(probe["source"])
    return list(dict.fromkeys(chosen))


def _import_dialogue(
    api: Any,
    pid: str,
    report: dict[str, Any],
    flags: tuple[list[str] | None, dict[str, str]],
    *,
    out: TextIO,
    ask: Callable[[str], str],
    interactive: bool,
    start_model: str | None,
    start_runner: str | None,
    no_start_block: bool,
) -> int:
    flag_sources, flag_since = flags
    probes = report["sources"]
    by_name = {p["source"]: p for p in probes}
    pickable = [p["source"] for p in probes if p["count"] > 0 and not p["imported"]]
    if not pickable:
        if any(p["imported"] for p in probes):
            _list_sources(probes, out)
            print("nothing new to import", file=out)
        return 0
    default_since = date.fromisoformat(report["default_since"])
    _list_sources(probes, out)

    named = [*(n for n in flag_sources or [] if n != "all"), *flag_since]
    for name in named:
        if name not in by_name:
            print(f"unknown source: {name}", file=sys.stderr)
            return 2
    for name in flag_sources or []:
        if name != "all" and name not in pickable:
            why = "already imported" if by_name[name]["imported"] else "nothing found"
            print(f"cannot import {name}: {why}", file=sys.stderr)
            return 2

    def older(name: str, start: date | None) -> bool:
        oldest = _day(by_name[name]["oldest"])
        return start is not None and oldest is not None and oldest < start

    try:
        if flag_sources is not None:
            picked = pickable if "all" in flag_sources else list(dict.fromkeys(flag_sources))
        elif interactive:
            picked = _prompt(ask, out, "Sources to import (numbers like 1,3 or all): ",
                             lambda a: _check_pick(a, probes, pickable))
        else:
            picked = pickable  # only to work out which other choices are missing
        for name in flag_since:
            if name not in picked:
                print(f"--since names {name}, which is not picked", file=sys.stderr)
                return 2

        missing = ["--sources"] if flag_sources is None and not interactive else []
        since_text: dict[str, str] = {}
        for name in picked:
            if name in flag_since:
                since_text[name] = flag_since[name]
            elif not older(name, default_since):
                since_text[name] = "default"  # the cutoff skips nothing, so there is nothing to ask
            elif interactive:
                since_text[name] = _prompt(
                    ask, out,
                    f"{name} reaches back to {_day(by_name[name]['oldest'])}; the default cutoff is "
                    f"{default_since}. Import from (YYYY-MM-DD, all or default): ",
                    _check_since,
                )
            else:
                missing.append(f"--since {name}=<YYYY-MM-DD|all|default>")
                since_text[name] = "default"  # assumed, to report a missing start model as well
        starts: dict[str, str] = {}
        for name, text in since_text.items():
            effective = None if text == "all" else default_since if text == "default" else date.fromisoformat(text)
            if effective is not None and older(name, effective):
                starts[name] = effective.isoformat()
        if starts and not interactive and not start_model and not no_start_block:
            missing.append("--start-model or --no-start-block")
        if missing:
            print("missing choices: " + ", ".join(missing), file=sys.stderr)
            return 2

        remotes: list[str] = []
        if interactive:
            for remote in report["remotes"]:
                if remote["declined"]:
                    print(f"remote {remote['url']} (declined before)", file=out)
                elif _prompt(ask, out, f"Also follow remote {remote['url']}? [y/n]: ", _yes_no):
                    remotes.append(remote["url"])

        start_block: dict[str, Any] | None = None
        if starts and not no_start_block:
            model = start_model
            if not model and interactive:
                model = _prompt(
                    ask, out,
                    f"Model to summarise the skipped history of {', '.join(starts)} (name, or none to skip): ",
                    _check_model,
                )
                model = None if model == "none" else model
            if model:
                runner_name = start_runner
                if interactive and not start_runner:
                    runner_name = ask("Runner (empty: let the daemon decide): ").strip() or None
                start_block = {"runner": runner_name, "model": model}
        if start_block is None:
            starts = {}  # no block, no summary run

        print("import summary:", file=out)
        for name in picked:
            text = since_text[name]
            what = {"all": "all history", "default": f"from {default_since} (default)"}.get(text, f"from {text}")
            print(f"  {name}: {what}", file=out)
        for url in remotes:
            print(f"  remote: {url}", file=out)
        if start_block:
            via = f" via {start_block['runner']}" if start_block["runner"] else ""
            print(f"  starting point: {start_block['model']}{via} summarises the skipped history of {', '.join(starts)}", file=out)
        if interactive and not _prompt(ask, out, "Import now? [y/n]: ", _yes_no):
            print("nothing imported", file=out)
            return 0
    except _Abort:
        print("input ended; nothing imported", file=sys.stderr)
        return 2

    body = {
        "sources": {n: {"picked": True, "since": None if t == "default" else t} for n, t in since_text.items()},
        "remotes": remotes,
        "start_block": start_block,
        "starts": starts,
    }
    result = api.post(f"/api/projects/{pid}/import", body)
    run = result.get("starting_point_run")
    if run:
        print(f"starting-point run {run['id']} queued", file=out)
    print("next ingest pass imports the history (trajectree ingest to run it now)", file=out)
    return 0
