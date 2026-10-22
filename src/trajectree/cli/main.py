from __future__ import annotations

import argparse
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol
from urllib.parse import urlencode

import httpx

from trajectree.timeutil import to_db, utcnow

DEFAULT_URL = "http://127.0.0.1:8742"


class _Client(Protocol):
    def get(self, url: str, **kwargs: Any) -> Any: ...

    def post(self, url: str, **kwargs: Any) -> Any: ...


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
        return resp.json()

    def get(self, path: str) -> Any:
        return self._handle(self.client.get(self.base + path, headers=self.headers))

    def post(self, path: str, body: dict[str, Any]) -> Any:
        return self._handle(self.client.post(self.base + path, json=body, headers=self.headers))

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


def _run(
    args: argparse.Namespace, api: _Api, runner: Callable[[list[str]], Any] | None = None
) -> None:
    cmd = args.cmd
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
        _run(args, _Api(http, base, token), runner)
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
