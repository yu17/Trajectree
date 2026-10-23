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
from urllib.parse import urlencode

import httpx

from trajectree.timeutil import to_db, utcnow

DEFAULT_URL = "http://127.0.0.1:8742"
POLL_S = 2.0
_sleep = time.sleep


class _Client(Protocol):
    def get(self, url: str, **kwargs: Any) -> Any: ...

    def post(self, url: str, **kwargs: Any) -> Any: ...

    def put(self, url: str, **kwargs: Any) -> Any: ...

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

    init = sub.add_parser("init-project", help="register a folder as a project and install guidance")
    init.add_argument("path")
    init.add_argument("--slug")
    init.add_argument("--title")
    init.add_argument("--kind", choices=["research", "tooling", "routine", "admin"], default="research")
    init.add_argument("--no-exclude", action="store_true")

    sub.add_parser("mcp", help="run the stdio MCP shim that forwards to the daemon")

    def run_opts(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--runner")
        sp.add_argument("--model")
        sp.add_argument("--no-wait", action="store_true")

    summ = sub.add_parser("summarize", help="summarize a project with the runner")
    summ.add_argument("project")
    run_opts(summ)

    ask = sub.add_parser("ask", help="ask a question over project context")
    ask.add_argument("question")
    ask.add_argument("--projects", help="comma-separated slugs")
    ask.add_argument("--days", type=int, default=90)
    ask.add_argument("--budget", type=int, default=4000)
    run_opts(ask)

    qadd = sub.add_parser("add", help="quick-add: turn free text into proposals")
    qadd.add_argument("text")
    qadd.add_argument("--project")
    run_opts(qadd)

    acc = sub.add_parser("accept", help="accept a run proposal (1-based)")
    acc.add_argument("run_id")
    acc.add_argument("n", type=int)

    runs = sub.add_parser("runs", help="list recent runs")
    runs.add_argument("--limit", type=int, default=20)

    usage = sub.add_parser("usage", help="show runner spend")
    usage.add_argument("--days", type=int, default=30)

    caps = sub.add_parser("caps", help="show or change spend caps")
    caps.add_argument("--usd-per-run", type=float)
    caps.add_argument("--usd-per-day", type=float)
    caps.add_argument("--timeout", type=int)
    pause = caps.add_mutually_exclusive_group()
    pause.add_argument("--pause", action="store_true")
    pause.add_argument("--resume", action="store_true")

    sub.add_parser("login", help="store a Claude OAuth token for the runner")

    back = sub.add_parser("backends", help="backend commands")
    back_sub = back.add_subparsers(dest="sub", required=True)
    back_sub.add_parser("sync", help="sync backend credentials")

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
    return p


def _token(env: Mapping[str, str]) -> str | None:
    token = env.get("TRAJECTREE_TOKEN")
    if token:
        return token.strip()
    home = env.get("HOME") if env is not os.environ else None
    base = Path(home) if home else Path.home()
    path = base / ".config" / "trajectree" / "token"
    try:
        return path.read_text().strip() or None
    except OSError:
        return None


class _Api:
    def __init__(self, client: _Client, base: str, token: str) -> None:
        self.client = client
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

    def post(self, path: str, body: dict[str, Any]) -> Any:
        return self._handle(self.client.post(self.base + path, json=body, headers=self.headers))

    def put(self, path: str, body: dict[str, Any]) -> Any:
        return self._handle(self.client.put(self.base + path, json=body, headers=self.headers))

    def delete(self, path: str) -> None:
        self._handle(self.client.delete(self.base + path, headers=self.headers))

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
        print(f"  [{n}] {p['kind']} · {p['project']} · {p['title']} ({p['reason']})", file=out)
        print(f"accept with: trajectree accept {run['id']} {n}", file=out)
    if run.get("error"):
        print(f"error: {run['error']}", file=sys.stderr)


def _launch(api: _Api, args: argparse.Namespace, path: str, body: dict[str, Any]) -> None:
    for key in ("runner", "model"):
        if getattr(args, key):
            body[key] = getattr(args, key)
    run = api.post(path, body)
    print(f"run {run['id']} queued")
    if args.no_wait:
        return
    detail = _wait_run(api, run["id"])
    _print_run(detail, sys.stdout)
    if detail["run"]["status"] in ("failed", "capped"):
        raise _Fail("", 1)


def _cmd_summarize(args: argparse.Namespace, api: _Api) -> None:
    pid = api.project_id(args.project)
    _launch(api, args, f"/api/projects/{pid}/summarize", {})


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


def _cmd_accept(args: argparse.Namespace, api: _Api) -> None:
    if args.n < 1:
        raise _Fail("invalid proposal number", 2)
    run = api.post(f"/api/runs/{args.run_id}/proposals/{args.n - 1}/accept", {})
    p = run["proposals"][args.n - 1]
    print(f"applied: {p['kind']} {p['applied_id']} (txn {p['applied_txn']})")


def _cmd_runs(args: argparse.Namespace, api: _Api) -> None:
    slugs = {p["id"]: p["slug"] for p in api.get("/api/projects")}
    for r in api.get(f"/api/runs?limit={args.limit}"):
        slug = slugs.get(r["project_id"], "-") if r["project_id"] else "-"
        print(f"{r['id']}  {r['created_at']}  {r['kind']}  {r['status']}  ${r['est_usd']:.4f}  {slug}")


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


def _cmd_backends_sync(api: _Api) -> None:
    synced = api.post("/api/backends/sync", {})["synced"]
    print("synced: " + (", ".join(synced) if synced else "none"))


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
) -> None:
    cmd = args.cmd
    simple: dict[str, Callable[[argparse.Namespace, _Api], None]] = {
        "summarize": _cmd_summarize, "ask": _cmd_ask, "add": _cmd_add, "accept": _cmd_accept,
        "runs": _cmd_runs, "usage": _cmd_usage, "caps": _cmd_caps,
    }
    if cmd in simple:
        simple[cmd](args, api)
        return
    if cmd == "login":
        _cmd_login(api, stdin if stdin is not None else sys.stdin)
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
    if cmd == "init-project":
        from trajectree.cli.init_project import init_project

        run = runner or (lambda a: subprocess.run(a, capture_output=True, text=True))
        code = init_project(
            api, Path(args.path), slug=args.slug, title=args.title, kind=args.kind,
            runner=run, out=sys.stdout, no_exclude=args.no_exclude,
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
) -> int:
    env = os.environ if env is None else env
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2
    if args.cmd == "mcp":
        from trajectree.cli.shim import run_shim

        return run_shim(env)
    token = _token(env)
    if token is None:
        print(
            "no token: set TRAJECTREE_TOKEN or write it to ~/.config/trajectree/token",
            file=sys.stderr,
        )
        return 2
    base = env.get("TRAJECTREE_URL", DEFAULT_URL)
    owned = client is None
    http: Any = httpx.Client(timeout=30) if owned else client
    try:
        _run(args, _Api(http, base, token), runner, stdin, env)
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
