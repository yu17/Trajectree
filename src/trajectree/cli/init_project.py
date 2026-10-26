from __future__ import annotations

import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, TextIO
from zoneinfo import ZoneInfo

from trajectree.cli.guidance import ADD_HINT, EXCLUDE_LINE, _ensure_mcp, write_rules  # noqa: F401  (re-exported)
from trajectree.importing import local_midnight
from trajectree.models import SLUG_PATTERN

def slug_from_path(path: Path) -> str:
    return re.sub(r"[^a-z0-9]+", "-", path.name.lower()).strip("-")[:64]


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
    remote: list[str] | None = None,
    no_remotes: bool = False,
    start_block_only: bool = False,
    guidance: bool = False,
) -> int:
    path = path.resolve()
    if start_block_only:
        clash = [flag for flag, given in (
            ("--sources", sources is not None), ("--since", bool(since)), ("--remote", bool(remote)),
            ("--no-remotes", no_remotes), ("--no-start-block", no_start_block),
        ) if given]
        if clash:
            print(f"--start-block-only excludes {', '.join(clash)}", file=sys.stderr)
            return 2
    flags = _parse_flags(sources, since)
    if isinstance(flags, str):
        print(flags, file=sys.stderr)
        return 2
    if remote and no_remotes:
        print("--remote and --no-remotes exclude each other", file=sys.stderr)
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
    if start_block_only:
        return _start_block_only(
            api, path, slug, project, out=out, ask=ask, interactive=interactive,
            start_model=start_model, start_runner=start_runner,
        )
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

    # Probed first, before any host write. A folder the daemon cannot see stops here: no rules
    # file, exclude or MCP setup. Any other probe failure propagates, also before those writes.
    report: dict[str, Any] | None = None
    if flags[0] != ["none"]:
        from trajectree.cli.main import _Fail

        try:
            report = api.post("/api/import/probe", {"path": str(path)})
        except _Fail as exc:
            print(f"project registered, but the daemon cannot see this folder: {exc}", file=sys.stderr)
            return 2

    print(f"project: {slug} ({pid})", file=out)
    print(f"resources: {', '.join(kinds)}", file=out)
    if guidance:
        if write_rules(path, slug, shown_title, no_exclude=no_exclude, out=out):
            return 1
        print(_ensure_mcp(runner), file=out)
    elif not interactive:
        print(NOT_INSTALLED, file=out)

    if report is None:  # --sources none
        code = 0
    else:
        code = _import_dialogue(
            api, pid, path, report, flags, out=out, ask=ask, interactive=interactive,
            start_model=start_model, start_runner=start_runner, no_start_block=no_start_block,
            remote=remote or [], no_remotes=no_remotes,
        )
    if code == 0 and interactive and not guidance:
        # Asked last, so it never changes the import; the end of the input counts as no.
        try:
            yes = _prompt(ask, out, GUIDANCE_QUESTION, _yes_no_default)
        except _Abort:
            yes = False
        if yes:
            if write_rules(path, slug, shown_title, no_exclude=no_exclude, out=out):
                return 1
            print(_ensure_mcp(runner), file=out)
        else:
            print(NOT_INSTALLED, file=out)
    return code


NOT_INSTALLED = "guidance: not installed (trajectree guidance install <path> to add it)"
GUIDANCE_QUESTION = (
    "Install session guidance so Claude Code sessions in this folder record milestones through Trajectree? [y/N]: "
)


def _yes_no_default(answer: str) -> bool:
    """Like _yes_no, but an empty answer is no."""
    return _yes_no(answer) if answer else False


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


def _stamp(stamp: str | None) -> datetime | None:
    return datetime.fromisoformat(stamp) if stamp else None


def _local_day(stamp: str | None, tz: str) -> date | None:
    parsed = _stamp(stamp)
    return parsed.astimezone(ZoneInfo(tz)).date() if parsed else None


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


def _check_model(answer: str, choices: list[tuple[str, str | None]]) -> tuple[str, str | None] | None:
    """`(runner, model)` for a list number, a `runner/model` line or a model name; `runner/default`
    is the runner with no model. None for none."""
    if not answer:
        raise ValueError("give a number or a model name, or none to skip")
    if answer == "none":
        return None
    if answer.isdigit():
        n = int(answer)
        if not 1 <= n <= len(choices):
            raise ValueError(f"no model with number {n}")
        return choices[n - 1]
    for runner, model in choices:
        if f"{runner}/{model or 'default'}" == answer:
            return runner, model
    named = [c for c in choices if c[1] == answer]
    if len(named) > 1:
        raise ValueError(f"model {answer} is offered by {', '.join(r for r, _ in named)}; answer with its number")
    if not named:
        raise ValueError(f"unknown model {answer}; answer with a number from the list")
    return named[0]


def _ask_model(api: Any, ask: Callable[[str], str], out: TextIO, sources: list[str]) -> tuple[str, str | None] | None:
    """The owner's pick from the catalog's available runners and their models, or None."""
    catalog = api.get("/api/models")
    choices: list[tuple[str, str | None]] = []
    for r in catalog["runners"]:
        if r["available"]:  # each runner's own default first, then its models
            choices.append((r["runner"], None))
            choices.extend((r["runner"], m["model"]) for m in r["models"])
    if choices:
        print("models:", file=out)
        for n, (runner, model) in enumerate(choices, 1):
            print(f"  {n}. {runner}/{model or 'default'}", file=out)
    else:
        print("no models in the catalog; run: trajectree models --refresh", file=out)
    return _prompt(
        ask, out,
        f"Model to summarise the skipped history of {', '.join(sources)} (number or name, or none to skip): ",
        lambda a: _check_model(a, choices),
    )


def _list_sources(probes: list[dict[str, Any]], out: TextIO, tz: str) -> None:
    print("history found:", file=out)
    for n, p in enumerate(probes, 1):
        if p["imported"]:
            tail = "(imported)"
        elif p["count"] == 0:
            tail = "(nothing found)" + (f" {p['detail']}" if p["detail"] else "")
        else:
            oldest = _local_day(p["oldest"], tz)
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


def _start_block_only(
    api: Any,
    path: Path,
    slug: str,
    project: dict[str, Any] | None,
    *,
    out: TextIO,
    ask: Callable[[str], str],
    interactive: bool,
    start_model: str | None,
    start_runner: str | None,
) -> int:
    """Queue the starting-point block again from the starts the import recorded; no host writes."""
    if project is None:
        print(f"no project {slug} for {path}; run: trajectree init-project {path}", file=sys.stderr)
        return 2
    starts = ((project.get("props") or {}).get("import") or {}).get("starts") or {}
    if not starts:
        print(f"{slug}: no skipped history recorded, nothing to summarise", file=sys.stderr)
        return 2
    print("skipped history: " + ", ".join(f"{name} before {day}" for name, day in sorted(starts.items())), file=out)
    choice = (start_runner, start_model) if (start_model or start_runner) else None
    if choice is None:
        if not interactive:
            print("missing choices: --start-runner or --start-model", file=sys.stderr)
            return 2
        try:
            choice = _ask_model(api, ask, out, sorted(starts))
        except _Abort:
            print("input ended; nothing queued", file=sys.stderr)
            return 2
        if choice is None:
            print("no starting-point run queued", file=out)
            return 0
    run = api.post(f"/api/projects/{project['id']}/import/start-block", {"runner": choice[0], "model": choice[1]})
    print(f"starting-point run {run['id']} queued", file=out)
    return 0


def _skipped_line(starts: dict[str, str], path: Path) -> str:
    days = sorted(set(starts.values()))
    when = days[0] if len(days) == 1 else ", ".join(f"{day} ({name})" for name, day in sorted(starts.items()))
    return (
        f"history before {when} will be neither imported nor summarised"
        f" (later: trajectree init-project {path} --start-block-only)"
    )


def _import_dialogue(
    api: Any,
    pid: str,
    path: Path,
    report: dict[str, Any],
    flags: tuple[list[str] | None, dict[str, str]],
    *,
    out: TextIO,
    ask: Callable[[str], str],
    interactive: bool,
    start_model: str | None,
    start_runner: str | None,
    no_start_block: bool,
    remote: list[str],
    no_remotes: bool,
) -> int:
    flag_sources, flag_since = flags
    tz = report["tz"]
    probes = report["sources"]
    by_name = {p["source"]: p for p in probes}
    pickable = [p["source"] for p in probes if p["count"] > 0 and not p["imported"]]
    if not pickable:
        if any(p["imported"] for p in probes):
            _list_sources(probes, out, tz)
            print("nothing new to import", file=out)
        return 0
    default_since = date.fromisoformat(report["default_since"])
    _list_sources(probes, out, tz)

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
        """True when `name` has history before local midnight of `start` (a day in the daemon's zone)."""
        oldest = _stamp(by_name[name]["oldest"])
        return start is not None and oldest is not None and oldest < local_midnight(start, tz)

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
                    f"{name} reaches back to {_local_day(by_name[name]['oldest'], tz)}; the default cutoff is "
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
        if starts and not interactive and not (start_model or start_runner) and not no_start_block:
            missing.append("--start-runner, --start-model or --no-start-block")
        undecided = [r for r in report["remotes"] if not r["declined"] and not r.get("followed")]
        if not interactive and not remote and not no_remotes and undecided:
            missing.append("--remote <url> or --no-remotes")
        if missing:
            print("missing choices: " + ", ".join(missing), file=sys.stderr)
            return 2

        # The daemon records every remote not sent as declined, so it is never sent by default.
        remotes: list[str] = list(dict.fromkeys(remote))
        for found in report["remotes"]:
            if found.get("followed"):
                print(f"remote {found['url']} (following)", file=out)
            elif found["declined"]:
                print(f"remote {found['url']} (declined before)", file=out)
            elif interactive and not remote and not no_remotes:
                if _prompt(ask, out, f"Also follow remote {found['url']}? [y/n]: ", _yes_no):
                    remotes.append(found["url"])

        start_block: dict[str, Any] | None = None
        if starts and not no_start_block:
            # The flags pass through as given; the daemon resolves and checks them.
            choice = (start_runner, start_model) if (start_model or start_runner) else None
            if choice is None and interactive:
                choice = _ask_model(api, ask, out, list(starts))
            if choice is not None:
                start_block = {"runner": choice[0], "model": choice[1]}
        # `starts` is sent even without a block: the daemon keeps it for --start-block-only.

        print("import summary:", file=out)
        for name in picked:
            text = since_text[name]
            what = {"all": "all history", "default": f"from {default_since} (default)"}.get(text, f"from {text}")
            print(f"  {name}: {what}", file=out)
        for url in remotes:
            print(f"  remote: {url}", file=out)
        if start_block:
            via = f" via {start_block['runner']}" if start_block["runner"] else ""
            print(f"  starting point: {start_block['model'] or 'default'}{via} summarises the skipped history of {', '.join(starts)}", file=out)
        elif starts:
            print("  " + _skipped_line(starts, path), file=out)
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
    print("history imports on the next ingest pass, then the project appears in the Inbox as due for inspection (trajectree ingest to run the pass now)", file=out)
    return 0
