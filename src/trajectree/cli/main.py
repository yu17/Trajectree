from __future__ import annotations

import argparse
import getpass
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, TextIO
from urllib.parse import quote, urlencode

import httpx

from trajectree.models import NO_MODEL_CHOSEN
from trajectree.runner.credentials import CONNECTOR_NAMES
from trajectree.timeutil import to_db, utcnow

DEFAULT_URL = "http://127.0.0.1:8742"
POLL_S = 2.0
SLOW_TIMEOUT_S = 120.0  # a catalog refresh asks every runner and backend in turn
_sleep = time.sleep


class _Client(Protocol):
    def get(self, url: str, **kwargs: Any) -> Any: ...

    def post(self, url: str, **kwargs: Any) -> Any: ...

    def put(self, url: str, **kwargs: Any) -> Any: ...

    def patch(self, url: str, **kwargs: Any) -> Any: ...

    def delete(self, url: str, **kwargs: Any) -> Any: ...


class _Fail(Exception):
    def __init__(self, message: str, code: int = 1) -> None:
        super().__init__(message)
        self.code = code


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="trajectree")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("projects", help="list projects")

    proj = sub.add_parser("project", help="project commands")
    proj_sub = proj.add_subparsers(dest="sub", required=True)
    add = proj_sub.add_parser("add", help="create a project")
    add.add_argument("slug")
    add.add_argument("title")
    add.add_argument("--kind", required=True)

    ev = sub.add_parser("event", help="create an event")
    ev.add_argument("project")
    ev.add_argument("title")
    ev.add_argument("--kind", default="note")
    ev.add_argument("--at", default="now")
    ev.add_argument("--end")
    ev.add_argument("--status", default="happened")
    ev.add_argument("--tier", choices=["block", "pill"], default="block")
    ev.add_argument("--body", default="")
    ev.add_argument("--intent")

    todo = sub.add_parser("todo", help="create a todo ('-' for no project)")
    todo.add_argument("project")
    todo.add_argument("title")
    todo.add_argument("--due")

    note = sub.add_parser("note", help="create a note event at now")
    note.add_argument("project")
    note.add_argument("text")

    rev = sub.add_parser("revert", help="revert a transaction")
    rev.add_argument("txn_id")

    txns = sub.add_parser("txns", help="list recent transactions")
    txns.add_argument("--limit", type=int, default=20)

    ctx = sub.add_parser("context", help="print a token-budgeted context pack")
    ctx.add_argument("projects", nargs="+")
    ctx.add_argument("--days", type=int, default=90)
    ctx.add_argument("--budget", type=int, default=4000)
    ctx.add_argument("--q")

    sub.add_parser("ingest", help="run one ingest pass now")

    init = sub.add_parser("init-project", help="register a folder as a project (guidance is opt-in)")
    init.add_argument("path")
    init.add_argument("--slug")
    init.add_argument("--title")
    init.add_argument("--kind", choices=["research", "tooling", "routine", "admin"], default="research")
    init.add_argument("--no-exclude", action="store_true")
    init.add_argument("--guidance", action="store_true",
                      help="also write the session rules file and register the MCP server")
    init.add_argument("--sources", help="history sources to import: a comma list (git,claude,folder) or all")
    init.add_argument("--since", action="append", default=[], metavar="SOURCE=DATE",
                      help="import SOURCE from YYYY-MM-DD, all or default (repeatable)")
    init.add_argument("--start-model", help="model that summarises the history older than the cutoff")
    init.add_argument("--start-runner", help="runner for --start-model")
    init.add_argument("--no-start-block", action="store_true", help="do not summarise skipped history")
    init.add_argument("--remote", action="append", default=[], metavar="URL", help="follow this git remote (repeatable)")
    init.add_argument("--no-remotes", action="store_true", help="follow no git remote")
    init.add_argument("--yes", action="store_true", help="never prompt; a missing choice is an error")
    init.add_argument("--start-block-only", action="store_true",
                      help="queue the starting-point summary of the skipped history again (needs a model)")

    gd = sub.add_parser("guidance", help="add, remove or show session guidance for a project folder")
    gd.add_argument("sub", choices=["install", "remove", "show"])
    gd.add_argument("path")
    gd.add_argument("--no-exclude", action="store_true", help="install: leave .git/info/exclude alone")

    sub.add_parser("mcp", help="run the stdio MCP shim that forwards to the daemon")

    def run_opts(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--runner", help="needed only when the model name alone is ambiguous; alone, it runs on its default model")
        sp.add_argument("--model", help="the model to run on (see: trajectree models)")
        sp.add_argument("--no-wait", action="store_true")

    summ = sub.add_parser("summarize", help="summarize a project, or one session, with the runner")
    summ.add_argument("project", nargs="?")
    summ.add_argument("--session", help="summarize this Claude Code session id instead of a project")
    run_opts(summ)

    sub.add_parser("inspections", help="list the projects with history waiting to be inspected")

    insp = sub.add_parser("inspect", help="inspect a project's uninspected history with the runner")
    insp.add_argument("project", nargs="?")
    insp.add_argument("--all", action="store_true", help="inspect every active project that has something due")
    insp.add_argument("--since", metavar="YYYY-MM-DD", help="re-inspect from this local date")
    run_opts(insp)

    ask = sub.add_parser("ask", help="ask a question over project context")
    ask.add_argument("question")
    ask.add_argument("--projects", help="comma-separated slugs")
    ask.add_argument("--days", type=int, default=90)
    ask.add_argument("--budget", type=int, default=4000)
    run_opts(ask)

    rep = sub.add_parser("report", help="report commands")
    rep_sub = rep.add_subparsers(dest="sub", required=True)
    rep_sub.add_parser("list", help="list reports")
    draft = rep_sub.add_parser("draft", help="draft a deck or memo with the runner")
    draft.add_argument("--kind", choices=["deck", "memo"], required=True)
    draft.add_argument("--title", required=True)
    draft.add_argument("--projects", required=True, help="comma-separated slugs")
    draft.add_argument("--days", type=int, default=90)
    run_opts(draft)
    exp = rep_sub.add_parser("export", help="export a report")
    exp.add_argument("report_id")
    exp.add_argument("--format", choices=["md", "html", "pdf", "pptx"], default="html")
    exp.add_argument("--out", help="write the file here (default: md/html to stdout, pdf/pptx to ./<id>.<format>)")

    qadd = sub.add_parser("add", help="quick-add: turn free text into proposals")
    qadd.add_argument("text")
    qadd.add_argument("--project")
    run_opts(qadd)

    acc = sub.add_parser("accept", help="accept a run proposal (1-based)")
    acc.add_argument("run_id")
    acc.add_argument("n", type=int)

    runs = sub.add_parser("runs", help="list recent runs, or show one run")
    runs.add_argument("--limit", type=int, default=20)
    runs.add_argument("id", nargs="?", help="show this run in full: error, log path and result")

    usage = sub.add_parser("usage", help="show runner spend")
    usage.add_argument("--days", type=int, default=30)

    caps = sub.add_parser("caps", help="show or change spend caps")
    caps.add_argument("--usd-per-run", type=float)
    caps.add_argument("--usd-per-day", type=float)
    caps.add_argument("--timeout", type=int)
    pause = caps.add_mutually_exclusive_group()
    pause.add_argument("--pause", action="store_true")
    pause.add_argument("--resume", action="store_true")

    sub.add_parser("routines", help="list routines")

    rt = sub.add_parser("routine", help="routine commands")
    rt_sub = rt.add_subparsers(dest="sub", required=True)
    for name in ("enable", "disable"):
        rt_sub.add_parser(name, help=f"{name} a routine").add_argument("name")
    rt_set = rt_sub.add_parser("set", help="set the runner and model a routine runs on")
    rt_set.add_argument("name")
    rt_set.add_argument("--model", help="omit to use the runner's own default model")
    rt_set.add_argument("--runner", help="needed only when the model name alone is ambiguous; alone, it runs on its default model")

    def routine_opts(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--projects", help="comma-separated slugs")
        run_opts(sp)

    run_routine = sub.add_parser("run", help="run a routine now")
    run_routine.add_argument("routine")
    routine_opts(run_routine)
    routine_opts(sub.add_parser("review", help="run cross-project-review now"))
    routine_opts(sub.add_parser("prep", help="run meeting-prep now"))

    sched = sub.add_parser("scheduler", help="show the scheduler, or tick it now")
    sched.add_argument("action", nargs="?", choices=["tick"])

    sub.add_parser("login", help="store a Claude OAuth token for the runner")

    conn = sub.add_parser("connectors", help="connector commands")
    conn_sub = conn.add_subparsers(dest="sub", required=True)
    conn_login = conn_sub.add_parser("login", help="store a connector API token (read from stdin)")
    conn_login.add_argument("name", choices=CONNECTOR_NAMES)

    back = sub.add_parser("backends", help="backend commands")
    back_sub = back.add_subparsers(dest="sub", required=True)
    back_sub.add_parser("sync", help="sync backend credentials")

    mdl = sub.add_parser("models", help="list the model catalog")
    mdl.add_argument("--refresh", action="store_true", help="refresh the catalog first")
    mdl.add_argument("--runner", help="the runner whose list --add/--delete edit")
    mdl.add_argument("--add", action="append", default=[], metavar="MODEL", help="add a model to the runner's list (repeatable)")
    mdl.add_argument("--delete", action="append", default=[], metavar="MODEL", help="remove a model from the runner's list (repeatable)")

    rn = sub.add_parser("runner", help="runner commands")
    rn_sub = rn.add_subparsers(dest="sub", required=True)
    rn_sub.add_parser("status", help="runner availability")

    hk = sub.add_parser("hooks", help="Claude Code hook commands")
    hk_sub = hk.add_subparsers(dest="sub", required=True)
    hk_install = hk_sub.add_parser("install", help="merge Trajectree hooks into Claude Code settings")
    hk_install.add_argument("--yes", action="store_true")
    for name, help_ in (("uninstall", "remove Trajectree hooks"), ("show", "show hook status")):
        hk_sub.add_parser(name, help=help_)
    for sp in hk_sub.choices.values():
        sp.add_argument("--settings-file")
    hk_post = hk_sub.add_parser("post", help=argparse.SUPPRESS)  # run by Codex, not by people
    hk_post.add_argument("--token-file", required=True)
    return p


def _config_file(env: Mapping[str, str], name: str) -> Path:
    home = env.get("HOME") if env is not os.environ else None
    base = Path(home) if home else Path.home()
    return base / ".config" / "trajectree" / name


def _token(env: Mapping[str, str]) -> str | None:
    token = env.get("TRAJECTREE_TOKEN")
    if token:
        return token.strip()
    try:
        return _config_file(env, "token").read_text().strip() or None
    except OSError:
        return None


def base_url(env: Mapping[str, str]) -> str:
    """The daemon's URL: TRAJECTREE_URL, else the first line of ~/.config/trajectree/url, else the default."""
    url = (env.get("TRAJECTREE_URL") or "").strip()
    if not url:
        try:
            lines = _config_file(env, "url").read_text().splitlines()
            url = lines[0].strip() if lines else ""
        except OSError:
            url = ""
    if url and not url.startswith(("http://", "https://")):
        where = "TRAJECTREE_URL" if (env.get("TRAJECTREE_URL") or "").strip() else str(_config_file(env, "url"))
        raise _Fail(f"{where} is {url!r}; the daemon URL must start with http:// or https://, like http://127.0.0.1:8742", 2)
    return (url or DEFAULT_URL).rstrip("/")


class _Api:
    def __init__(self, client: _Client, base: str, token: str, *, slow_timeout: float | None = None) -> None:
        self.client = client
        self.slow_timeout = slow_timeout  # for requests that may take minutes; None: the client's default
        self.base = base.rstrip("/")
        self.headers = {"Authorization": f"Bearer {token}", "X-Trajectree-Client": "cli"}

    def _handle(self, resp: Any) -> Any:
        if resp.status_code >= 400:
            try:
                detail = resp.json().get("detail", resp.text)
            except Exception:
                detail = getattr(resp, "text", "") or f"HTTP {resp.status_code}"
            raise _Fail(str(detail))
        return None if resp.status_code == 204 else resp.json()

    def get(self, path: str) -> Any:
        return self._handle(self.client.get(self.base + path, headers=self.headers))

    def post(self, path: str, body: dict[str, Any], *, slow: bool = False) -> Any:
        """`slow`: the request may take minutes, so it gets `slow_timeout`."""
        extra = {"timeout": self.slow_timeout} if slow and self.slow_timeout is not None else {}
        return self._handle(self.client.post(self.base + path, json=body, headers=self.headers, **extra))

    def put(self, path: str, body: dict[str, Any]) -> Any:
        return self._handle(self.client.put(self.base + path, json=body, headers=self.headers))

    def patch(self, path: str, body: dict[str, Any]) -> Any:
        return self._handle(self.client.patch(self.base + path, json=body, headers=self.headers))

    def delete(self, path: str) -> None:
        self._handle(self.client.delete(self.base + path, headers=self.headers))

    def get_bytes(self, path: str) -> bytes:
        resp = self.client.get(self.base + path, headers=self.headers)
        if resp.status_code >= 400:
            self._handle(resp)  # raises with the server's detail
        return resp.content

    def project_id(self, slug: str) -> str:
        for p in self.get("/api/projects"):
            if p["slug"] == slug:
                return p["id"]
        raise _Fail(f"unknown project: {slug}")


def _when(value: str) -> str:
    if value == "now":
        return to_db(utcnow())
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise _Fail(f"invalid datetime: {value}", 2)
    return value


def _wait_run(api: _Api, run_id: str) -> dict[str, Any]:
    while True:
        detail = api.get(f"/api/runs/{run_id}")
        if detail["run"]["status"] not in ("queued", "running"):
            return detail
        _sleep(POLL_S)


def _print_run(detail: dict[str, Any], out: TextIO) -> None:
    run = detail["run"]
    print(
        f"run {run['id']} {run['status']} · ${run['est_usd']:.4f} · {run['turns']} turns"
        f" · {run['duration_ms']} ms",
        file=out,
    )
    if detail["txn_ids"]:
        print(f"pending: {len(detail['txn_ids'])} transaction(s) in the inbox", file=out)
    if run.get("result_md"):
        print(file=out)
        print(run["result_md"], file=out)
    for n, p in enumerate(run.get("proposals") or [], 1):
        state = " [applied]" if p.get("applied_txn") else " [dismissed]" if p.get("dismissed") else ""
        print(f"  [{n}] {p['kind']} · {p['project']} · {p['title']} ({p['reason']}){state}", file=out)
        if not state:
            print(f"accept with: trajectree accept {run['id']} {n}", file=out)
    _print_warning(run)
    if run.get("error"):
        print(f"error: {run['error']}", file=sys.stderr)


def _print_warning(run: dict[str, Any]) -> None:
    warning = (run.get("props") or {}).get("model_warning")
    if warning:
        print(f"warning: {warning}", file=sys.stderr)


def _launch(api: _Api, args: argparse.Namespace, path: str, body: dict[str, Any]) -> None:
    for key in ("runner", "model"):
        if getattr(args, key, None):
            body[key] = getattr(args, key)
    run = api.post(path, body)
    print(f"run {run['id']} queued")
    if args.no_wait:
        _print_warning(run)
        return
    detail = _wait_run(api, run["id"])
    _print_run(detail, sys.stdout)
    if detail["run"]["status"] in ("failed", "capped"):
        raise _Fail("", 1)


def _cmd_summarize(args: argparse.Namespace, api: _Api) -> None:
    if bool(args.session) == bool(args.project):
        raise _Fail("give either a project or --session <id>", 2)
    if args.session:
        _launch(api, args, f"/api/sessions/{quote(args.session, safe='')}/summarize", {})
        return
    pid = api.project_id(args.project)
    _launch(api, args, f"/api/projects/{pid}/summarize", {})


def _cmd_inspections(args: argparse.Namespace, api: _Api) -> None:
    due = api.get("/api/inspections")
    if not due:
        print("nothing to inspect")
        return
    for d in due:
        line = (
            f"{d['slug']}  {d['stretches']} stretches  {d['first_day']}..{d['last_day']}"
            # `sessions` counts session segments
            f"  {d['sessions']} segments, {d['commits']} commits, {d['file_days']} file days"
        )
        if d.get("active_run"):
            if d.get("episode") is None:  # a boundary pass comes before episode 1
                line += f"  running boundary pass ({d['active_run']})"
            else:
                line += f"  running episode {d['episode']}/{d['episodes'] or 0} ({d['active_run']})"
        print(line)


SERIES_RETRIES = 50  # a finished run's successor is queued just after the run is stored


def _series_successor(api: _Api, finished: dict[str, Any]) -> str | None:
    """The run of `finished`'s series that comes right after it (the boundary pass is episode 0)."""
    props = finished.get("props") or {}
    series = props.get("series_id")
    if not series:
        return None
    following = (props.get("episode_no") or 0) + 1
    for run in api.get(f"/api/runs?series_id={quote(str(series), safe='')}&limit=500"):
        if run["id"] != finished["id"] and ((run.get("props") or {}).get("episode_no") or 0) == following:
            return run["id"]
    return None


def _next_in_series(api: _Api, slug: str, finished: dict[str, Any]) -> str | None:
    """The id of the series' next run once it is queued (found by series id, so a project gone live
    meanwhile does not hide it); None when `finished` was the last. Raises `_Fail` when the series stopped."""
    props = finished.get("props") or {}
    more = finished["kind"] == "inspect_boundaries" or (props.get("episode_no") or 0) < (props.get("episodes") or 0)
    for _ in range(SERIES_RETRIES if more else 1):
        if more:
            # The run was stored `ok` before the series carried on: it may have failed validation
            # or recorded why it could not continue since.
            detail = api.get(f"/api/runs/{finished['id']}")
            now = detail["run"]
            if now["status"] in ("failed", "capped"):
                _print_run(detail, sys.stdout)
                raise _Fail("", 1)
            stopped = (now.get("props") or {}).get("series_stopped")
            if stopped:
                raise _Fail(f"series stopped: {stopped}", 1)
        nxt = _series_successor(api, finished)
        if nxt is not None:
            return nxt
        if more:
            _sleep(POLL_S)
    if more:
        raise _Fail(f"series stopped: no next run was queued for {slug}", 1)
    return None


def _cmd_inspect(args: argparse.Namespace, api: _Api) -> None:
    body: dict[str, Any] = {}
    for key in ("runner", "model"):
        if getattr(args, key, None):
            body[key] = getattr(args, key)
    if args.all:
        if args.project or args.since:
            raise _Fail("--all takes no project and no --since", 2)
        res = api.post("/api/inspect", {"projects": "all", **body}, slow=True)  # sizes every due project's transcripts
        slugs = {p["id"]: p["slug"] for p in api.get("/api/projects")}
        for run in res["runs"]:
            print(f"run {run['id']} queued ({slugs.get(run['project_id'], run['project_id'])})")
            _print_warning(run)
        if res["skipped"]:
            print("skipped: " + ", ".join(res["skipped"]))
        return
    if not args.project:
        raise _Fail("give a project or --all", 2)
    if args.since:
        try:
            datetime.strptime(args.since, "%Y-%m-%d")
        except ValueError:
            raise _Fail(f"invalid --since {args.since!r}; use YYYY-MM-DD", 2)
        body["since"] = args.since
    run = api.post(f"/api/projects/{api.project_id(args.project)}/inspect", body, slow=True)  # reads the transcripts
    print(f"run {run['id']} queued")
    if args.no_wait:
        _print_warning(run)
        return
    while True:
        props = run.get("props") or {}
        if run["kind"] == "inspect":
            print(f"episode {props.get('episode_no') or 1}/{props.get('episodes') or 1}")
        detail = _wait_run(api, run["id"])
        _print_run(detail, sys.stdout)
        if detail["run"]["status"] in ("failed", "capped"):
            raise _Fail("", 1)
        if detail["run"]["status"] != "ok":
            return
        nxt = _next_in_series(api, args.project, detail["run"])
        if nxt is None:
            return
        run = api.get(f"/api/runs/{nxt}")["run"]


def _cmd_ask(args: argparse.Namespace, api: _Api) -> None:
    now = utcnow()
    body: dict[str, Any] = {
        "question": args.question,
        "start": to_db(now - timedelta(days=args.days)),
        "end": to_db(now + timedelta(days=30)),
        "token_budget": args.budget,
    }
    if args.projects:
        body["projects"] = [s for s in args.projects.split(",") if s]
    _launch(api, args, "/api/ask", body)


def _cmd_add(args: argparse.Namespace, api: _Api) -> None:
    body: dict[str, Any] = {"text": args.text}
    if args.project:
        body["project"] = args.project
    _launch(api, args, "/api/quick-add", body)


def _cmd_report(args: argparse.Namespace, api: _Api) -> None:
    if args.sub == "list":
        for r in api.get("/api/reports"):
            pending = "  [pending]" if r["pending_txn"] else ""
            print(f"{r['id']}  {r['kind']}  {r['updated_at']}  {r['title']}{pending}")
    elif args.sub == "draft":
        now = utcnow()
        body: dict[str, Any] = {
            "kind": args.kind,
            "title": args.title,
            "projects": [s for s in args.projects.split(",") if s],
            "start": to_db(now - timedelta(days=args.days)),
            "end": to_db(now + timedelta(days=30)),
        }
        _launch(api, args, "/api/reports/draft", body)
    else:
        query = urlencode({"format": args.format})
        data = api.get_bytes(f"/api/reports/{quote(args.report_id, safe='')}/export?{query}")
        out = args.out
        if out is None and args.format in ("md", "html"):
            sys.stdout.write(data.decode("utf-8"))  # text formats go to stdout
            return
        out = out or f"{args.report_id}.{args.format}"  # binary formats default to ./<id>.<fmt>
        Path(out).write_bytes(data)
        print(f"wrote {out}")


def _cmd_accept(args: argparse.Namespace, api: _Api) -> None:
    if args.n < 1:
        raise _Fail("invalid proposal number", 2)
    run = api.post(f"/api/runs/{args.run_id}/proposals/{args.n - 1}/accept", {})
    p = run["proposals"][args.n - 1]
    print(f"applied: {p['kind']} {p['applied_id']} (txn {p['applied_txn']})")


def _cmd_runs(args: argparse.Namespace, api: _Api) -> None:
    if args.id is not None:
        if not args.id.strip():
            raise _Fail("run id required", 2)
        _show_run(args.id, api)
        return
    slugs = {p["id"]: p["slug"] for p in api.get("/api/projects")}
    for r in api.get(f"/api/runs?limit={args.limit}"):
        error = (r.get("error") or "-")[:60]  # a Codex failure carries Codex's own, possibly long, text
        slug = slugs.get(r["project_id"], "-") if r["project_id"] else "-"
        print(f"{r['id']}  {r['created_at']}  {r['kind']}  {r['status']}  {slug}  {r['runner']}  {r['model'] or 'default'}  ${r['est_usd']:.4f}  {error}")


def _show_run(run_id: str, api: _Api) -> None:
    resp = api.client.get(f"{api.base}/api/runs/{quote(run_id, safe='')}", headers=api.headers)
    if resp.status_code == 404:
        raise _Fail(f"no run {run_id}")
    run = api._handle(resp)["run"]
    warning = (run.get("props") or {}).get("model_warning") or "-"
    print(f"status: {run['status']}")
    print(f"kind: {run['kind']}")
    print(f"runner: {run['runner']}")
    print(f"model: {run['model'] or 'default'}")
    print(f"model warning: {warning}")
    print(f"created: {run['created_at']}")
    print(f"finished: {run.get('finished') or '-'}")
    print(f"cost: ${run['est_usd']:.4f}")
    print(f"error: {run.get('error') or '-'}")
    print(f"log: {run.get('log_path') or '-'}")
    print()
    print(run["result_md"] or "")


def _cmd_usage(args: argparse.Namespace, api: _Api) -> None:
    u = api.get(f"/api/usage?days={args.days}")
    paused = "yes" if u["caps"]["paused"] else "no"
    print(f"today: ${u['spent_today']:.4f} of ${u['caps']['usd_per_day']} · paused: {paused}")
    for d in u["days"]:
        if d["runs"]:
            print(f"{d['day']}  ${d['est_usd']:.4f}  {d['runs']} runs")
    print("by kind:")
    for s in u["by_kind"]:
        print(f"{s['key']}  ${s['est_usd']:.4f}  {s['runs']}")


def _cmd_caps(args: argparse.Namespace, api: _Api) -> None:
    caps = api.get("/api/caps")
    if args.usd_per_run is not None:
        caps["usd_per_run"] = args.usd_per_run
    if args.usd_per_day is not None:
        caps["usd_per_day"] = args.usd_per_day
    if args.timeout is not None:
        caps["timeout_s"] = args.timeout
    if args.pause:
        caps["paused"] = True
    if args.resume:
        caps["paused"] = False
    caps = api.put("/api/caps", caps)
    print(
        f"caps: run ${caps['usd_per_run']} · day ${caps['usd_per_day']}"
        f" · timeout {caps['timeout_s']}s · paused {'yes' if caps['paused'] else 'no'}"
    )


ROUTINE_ALIASES = {"review": "cross-project-review", "prep": "meeting-prep"}
MODEL_REQUIRED = ("summarize", "ask", "add", "inspect")  # and `report draft` and `routine set`; routine runs may use the stored choice


def _model_label(runner: str | None, model: str | None) -> str:
    """How a routine's choice reads in a listing: runner/model, a runner's default, or nothing chosen."""
    if runner:
        return f"{runner}/{model or 'default'}"
    return model or "no model"


def _cmd_routines(args: argparse.Namespace, api: _Api) -> None:
    for r in api.get("/api/routines"):
        last = api.get(f"/api/runs/{r['last_run_id']}")["run"]["status"] if r["last_run_id"] else "-"
        enabled = "on" if r["enabled"] else "off"
        model = _model_label(r["runner"], r["model"])
        print(f"{r['name']}  {enabled}  {model}  {r['trigger']}  {r['tz']}  {r['next_fire'] or '-'}  {last}")


def _cmd_routine(args: argparse.Namespace, api: _Api) -> None:
    path = f"/api/routines/{quote(args.name, safe='')}"
    if args.sub == "set":
        # Both are set: without --runner the catalog picks the runner at each run.
        r = api.patch(path, {"model": args.model, "runner": args.runner})
        print(f"{r['name']}: model {r['model'] or 'default'}" + (f" via {r['runner']}" if r["runner"] else ""))
        return
    enable = args.sub == "enable"
    r = api.patch(path, {"enabled": enable})
    if enable:
        print(f"{r['name']} enabled · next fire {r['next_fire'] or '-'}")
    else:
        print(f"{r['name']} disabled")


def _cmd_run_routine(args: argparse.Namespace, api: _Api) -> None:
    name = ROUTINE_ALIASES.get(args.cmd) or args.routine.strip()
    if not name:
        raise _Fail("routine name required", 2)
    body: dict[str, Any] = {}
    if args.projects:
        body["projects"] = [s for s in args.projects.split(",") if s]
    _launch(api, args, f"/api/routines/{quote(name, safe='')}/run", body)


def _cmd_scheduler(args: argparse.Namespace, api: _Api) -> None:
    if args.action == "tick":
        rep = api.post("/api/scheduler/tick", {})
        print(
            f"tick: {len(rep['fired'])} fired, {len(rep['skipped'])} skipped,"
            f" {len(rep['retried'])} retried, {len(rep['errors'])} errors"
        )
        for error in rep["errors"]:
            print(f"error: {error}", file=sys.stderr)
        return
    st = api.get("/api/scheduler")
    print(f"scheduler: every {st['interval_s']}s · last tick {st['last_tick'] or 'never'}")
    for nf in st["next_fires"]:
        print(f"  {nf['routine']}  {nf['at']}")


def _cmd_login(api: _Api, stdin: TextIO) -> None:
    print(
        "Run 'claude setup-token' on this machine, then paste the token (input is not echoed):",
        file=sys.stderr,
    )
    token = (getpass.getpass("token: ") if stdin.isatty() else stdin.readline()).strip()
    if not token:
        raise _Fail("no token given", 2)
    api.post("/api/runner/credentials", {"backend": "claude", "oauth_token": token})
    print("stored")


def _cmd_connectors_login(api: _Api, name: str, stdin: TextIO) -> None:
    print(f"Paste the {name} API token (input is not echoed):", file=sys.stderr)
    token = (getpass.getpass("token: ") if stdin.isatty() else stdin.readline()).strip()
    if not token:
        raise _Fail("no token given", 2)
    api.post(f"/api/connectors/{name}/credentials", {"token": token})
    print("stored")


def _cmd_backends_sync(api: _Api) -> None:
    synced = api.post("/api/backends/sync", {}, slow=True)["synced"]
    print("synced: " + (", ".join(synced) if synced else "none"))


def _cmd_models(args: argparse.Namespace, api: _Api) -> None:
    editing = bool(args.add or args.delete)
    if editing and (args.refresh or not args.runner):
        raise _Fail("--add and --delete need --runner and cannot be combined with --refresh", 2)
    if args.runner and not editing:
        raise _Fail("--runner is for --add or --delete", 2)
    if editing:
        catalog = api.post("/api/models/edit", {"runner": args.runner, "add": args.add, "delete": args.delete})
    else:
        catalog = api.post("/api/models/refresh", {}, slow=True) if args.refresh else api.get("/api/models")
    if not catalog["runners"]:
        print("no catalog yet; run: trajectree models --refresh")
    for r in catalog["runners"]:
        head = r["runner"]
        if r["refreshed_at"]:
            when = datetime.fromisoformat(r["refreshed_at"].replace("Z", "+00:00")).astimezone()
            head += f"  refreshed {when:%Y-%m-%d %H:%M}"
        if r["error"]:
            head += f", {r['error']}"
        print(head)
        for m in r["models"]:
            print(f"  {m['model']}  {m['display'] or ''}".rstrip())


def _cmd_runner_status(api: _Api) -> None:
    st = api.get("/api/runner/status")
    for name in ("claude", "codex"):
        r = st[name]
        avail = "available" if r["available"] else "missing"
        auth = "logged in" if r["authenticated"] else "not logged in"
        print(f"{name}: {avail} {r['version'] or ''} {auth}")
    print(
        "backends: "
        + ", ".join(f"{b['name']} ({'ok' if b['credentials'] else 'no key'})" for b in st["backends"])
    )


def _run(
    args: argparse.Namespace,
    api: _Api,
    runner: Callable[[list[str]], Any] | None = None,
    stdin: TextIO | None = None,
    env: Mapping[str, str] | None = None,
    ask: Callable[[str], str] | None = None,
    interactive: bool | None = None,
) -> None:
    cmd = args.cmd
    # Checked before any request; a routine run without --model uses the routine's stored model.
    if cmd in MODEL_REQUIRED or (cmd == "report" and args.sub == "draft") or (cmd == "routine" and args.sub == "set"):
        if not (args.model or args.runner):
            raise _Fail(f"{NO_MODEL_CHOSEN}; pass --runner or --model (see: trajectree models)", 2)
    simple: dict[str, Callable[[argparse.Namespace, _Api], None]] = {
        "inspections": _cmd_inspections, "inspect": _cmd_inspect,
        "summarize": _cmd_summarize, "ask": _cmd_ask, "add": _cmd_add, "accept": _cmd_accept,
        "runs": _cmd_runs, "usage": _cmd_usage, "caps": _cmd_caps,
        "routines": _cmd_routines, "routine": _cmd_routine, "run": _cmd_run_routine,
        "review": _cmd_run_routine, "prep": _cmd_run_routine, "scheduler": _cmd_scheduler,
        "report": _cmd_report, "models": _cmd_models,
    }
    if cmd in simple:
        simple[cmd](args, api)
        return
    if cmd == "login":
        _cmd_login(api, stdin if stdin is not None else sys.stdin)
        return
    if cmd == "connectors":
        _cmd_connectors_login(api, args.name, stdin if stdin is not None else sys.stdin)
        return
    if cmd == "backends":
        _cmd_backends_sync(api)
        return
    if cmd == "runner":
        _cmd_runner_status(api)
        return
    if cmd == "hooks":
        from trajectree.cli import hooks

        env = os.environ if env is None else env
        settings_file = Path(args.settings_file) if args.settings_file else None
        if args.sub == "install":
            code = hooks.install(api, env, out=sys.stdout, yes=args.yes, settings_file=settings_file)
        elif args.sub == "uninstall":
            code = hooks.uninstall(api, env, out=sys.stdout, settings_file=settings_file)
        else:
            code = hooks.show(api, env, out=sys.stdout, settings_file=settings_file)
        if code:
            raise _Fail("", code)
        return
    if cmd == "guidance":
        from trajectree.cli import guidance as gmod

        run = runner or (lambda a: subprocess.run(a, capture_output=True, text=True))
        if args.sub == "install":
            code = gmod.install(api, Path(args.path), out=sys.stdout, runner=run, no_exclude=args.no_exclude)
        elif args.sub == "remove":
            code = gmod.remove(Path(args.path), out=sys.stdout)
        else:
            code = gmod.show(Path(args.path), out=sys.stdout, runner=run)
        if code:
            raise _Fail("", code)
        return
    if cmd == "init-project":
        from trajectree.cli.init_project import init_project

        run = runner or (lambda a: subprocess.run(a, capture_output=True, text=True))
        code = init_project(
            api, Path(args.path), slug=args.slug, title=args.title, kind=args.kind,
            runner=run, out=sys.stdout, no_exclude=args.no_exclude,
            ask=ask or input,
            interactive=(sys.stdin.isatty() if interactive is None else interactive) and not args.yes,
            sources=args.sources, since=args.since, start_model=args.start_model,
            start_runner=args.start_runner, no_start_block=args.no_start_block,
            remote=args.remote, no_remotes=args.no_remotes, start_block_only=args.start_block_only,
            guidance=args.guidance,
        )
        if code:
            raise _Fail("", code)
        return
    if cmd == "projects":
        for p in api.get("/api/projects"):
            print(f"{p['slug']}  {p['title']}  {p['status']}")
    elif cmd == "project":
        created = api.post(
            "/api/projects", {"slug": args.slug, "title": args.title, "kind": args.kind}
        )
        print(created["id"])
    elif cmd in ("event", "note"):
        pid = api.project_id(args.project)
        if cmd == "note":
            body: dict[str, Any] = {
                "project_id": pid, "kind": "note", "title": args.text, "t_start": _when("now"),
            }
        else:
            body = {
                "project_id": pid,
                "kind": args.kind,
                "title": args.title,
                "body_md": args.body,
                "t_start": _when(args.at),
                "status": args.status,
                "tier": args.tier,
            }
            if args.end:
                body["t_end"] = _when(args.end)
            if args.intent:
                body["intent"] = args.intent
        print(api.post("/api/events", body)["id"])
    elif cmd == "todo":
        body = {"title": args.title}
        if args.project != "-":
            body["project_id"] = api.project_id(args.project)
        if args.due:
            body["due"] = _when(args.due)
        print(api.post("/api/todos", body)["id"])
    elif cmd == "revert":
        print(api.post(f"/api/ledger/txns/{args.txn_id}/revert", {})["id"])
    elif cmd == "context":
        if not 200 <= args.budget <= 100000:
            raise _Fail("invalid budget: must be between 200 and 100000", 2)
        now = utcnow()
        query = {
            "projects": ",".join(args.projects),
            "from": to_db(now - timedelta(days=args.days)),
            "to": to_db(now + timedelta(days=30)),
            "budget": args.budget,
        }
        if args.q:
            query["q"] = args.q
        print(api.get("/api/context?" + urlencode(query))["markdown"], end="")
    elif cmd == "ingest":
        r = api.post("/api/ingest/run", {})
        scan = r["scan"] or {}
        signals = (scan.get("signals_written") or 0) + r["git"]["signals_written"]
        print(
            f"ingest: {scan.get('files_read') or 0} transcripts read, {signals} signals,"
            f" {len(r['stale'])} stale, {r['duration_ms']} ms"
        )
    elif cmd == "txns":
        for t in api.get(f"/api/ledger/txns?limit={args.limit}"):
            print(
                f"{t['id']}  {t['ts']}  {t['actor_kind']}/{t['actor_ref']}  {t['review']}  {t['intent']}"
            )


def main(
    argv: list[str],
    *,
    client: _Client | None = None,
    env: Mapping[str, str] | None = None,
    runner: Callable[[list[str]], Any] | None = None,
    stdin: TextIO | None = None,
    ask: Callable[[str], str] | None = None,
    interactive: bool | None = None,
) -> int:
    env = os.environ if env is None else env
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2
    if args.cmd == "mcp":
        from trajectree.cli.shim import run_shim

        return run_shim(env)
    if args.cmd == "hooks" and args.sub == "post":
        from trajectree.cli import hooks

        return hooks.post(env, token_file=Path(args.token_file), stdin=stdin if stdin is not None else sys.stdin)
    token = _token(env)
    if token is None:
        print(
            "no token: set TRAJECTREE_TOKEN or write it to ~/.config/trajectree/token",
            file=sys.stderr,
        )
        return 2
    try:
        base = base_url(env)
    except _Fail as exc:
        print(str(exc), file=sys.stderr)
        return exc.code
    owned = client is None
    http: Any = httpx.Client(timeout=30) if owned else client
    try:
        api = _Api(http, base, token, slow_timeout=SLOW_TIMEOUT_S if owned else None)
        _run(args, api, runner, stdin, env, ask, interactive)
    except _Fail as exc:
        if str(exc):
            print(str(exc), file=sys.stderr)
        return exc.code
    except httpx.HTTPError as exc:
        print(f"request failed: {exc}", file=sys.stderr)
        return 1
    finally:
        if owned:
            http.close()
    return 0


def entry() -> None:
    sys.exit(main(sys.argv[1:]))
