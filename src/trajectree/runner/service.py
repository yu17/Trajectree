"""RunService: queue, caps, per-run token lifecycle, execution and proposal application.

Lock discipline: every DB access happens inside `ledger.locked()`; `launch` runs with no lock held.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone

from pydantic import ValidationError

from trajectree.auth.tokens import mint_token, revoke_token
from trajectree.config import Settings
from trajectree.db.ledger import Ledger
from trajectree.models import Actor, EventIn, Proposal, Run, TodoIn
from trajectree.repo import NotFound, UnknownProject, events, todos
from trajectree.repo.caps import get_caps
from trajectree.repo.projects import get_project_by_slug, resolve_project_ref
from trajectree.repo.runs import create_run, get_run, oldest_queued, spent_usd, update_run
from trajectree.runner.argv import claude_argv, codex_argv, run_env
from trajectree.runner.credentials import cli_version, ensure_cfg_dirs, load_oauth_token
from trajectree.runner.kinds import (
    KIND_OF,
    AskRequest,
    RunRequest,
    SummarizeRequest,
    ensure_prompts,
    plan_run,
    request_from_props,
    runner_parts,
)
from trajectree.runner.launch import launch
from trajectree.runner.parse import ParsedResult, parse_claude_json, parse_codex
from trajectree.runner.spec import (
    AlreadyApplied,
    MissingCredentials,
    RunsPaused,
    RunSpec,
)
from trajectree.timeutil import parse_iso, utcnow

log = logging.getLogger("trajectree.runner")


class RunService:
    def __init__(
        self,
        ledger: Ledger,
        settings: Settings,
        *,
        launch=launch,
        popen=subprocess.Popen,
        now: Callable[[], datetime] = utcnow,
        base_env: Mapping[str, str] | None = None,
        version=cli_version,
    ) -> None:
        self.ledger = ledger
        self.settings = settings
        self.launch = launch
        self.popen = popen
        self.now = now
        self.base_env = base_env
        self.version = version

    def today(self) -> str:
        return self.now().astimezone(timezone.utc).strftime("%Y-%m-%d")

    def _binary(self, runner: str) -> str:
        return self.settings.codex_bin if runner == "codex" else self.settings.claude_bin

    # -- enqueue ---------------------------------------------------------------------------

    def enqueue(self, request: RunRequest) -> Run:
        with self.ledger.locked() as conn:
            if get_caps(conn).paused:
                raise RunsPaused("runs are paused")
            runner = request.runner or self.settings.default_runner
            runner, backend = runner_parts(runner)
            project_id = None
            if isinstance(request, SummarizeRequest):
                project_id = resolve_project_ref(conn, request.project).id
            elif isinstance(request, AskRequest):
                for ref in request.projects or []:
                    resolve_project_ref(conn, ref)
            elif request.project:
                resolve_project_ref(conn, request.project)
            return create_run(
                conn,
                kind=KIND_OF[type(request)],
                runner=runner,
                backend=backend,
                model=request.model,
                project_id=project_id,
                props={"request": request.model_dump(mode="json")},
            )

    # -- execute ---------------------------------------------------------------------------

    def _fail(self, conn, run_id: str, error: str, status: str = "failed") -> Run:
        return update_run(conn, run_id, status=status, error=error, finished=self.now())

    def execute(self, run_id: str) -> Run:
        with self.ledger.locked() as conn:
            current = get_run(conn, run_id)
        # The version probe spawns a subprocess, so it runs before the lock is taken.
        version = (
            self.version(self._binary(current.runner))
            if current is not None and current.status == "queued"
            else None
        )
        with self.ledger.locked() as conn:
            prepared = self._prepare(conn, run_id, version)  # revokes its own token if it fails
            if isinstance(prepared, Run):
                return prepared
            run, caps, plan, spec, env, token_id, oauth_token = prepared
        secret = spec.mcp_token
        try:
            ensure_cfg_dirs(self.settings)
            run_dir = self.settings.runs_dir / run_id
            run_dir.mkdir(parents=True, exist_ok=True)
            last_path = run_dir / "last.md"
            if run.runner == "codex":
                schema_path = run_dir / "schema.json"
                schema_path.write_text(json.dumps(plan.json_schema))
                argv = codex_argv(
                    spec, binary=self.settings.codex_bin, schema_path=schema_path, last_message_path=last_path
                )
            else:
                argv = claude_argv(spec, binary=self.settings.claude_bin)
            outcome = self.launch(
                argv,
                env=env,
                cwd=plan.cwd,
                timeout_s=caps.timeout_s,
                log_path=self.settings.runs_dir / f"{run_id}.log",
                secrets=[secret, oauth_token or ""],
                popen=self.popen,
            )
        except BaseException as exc:
            self._abort(run_id, token_id, str(exc)[:500])
            raise
        with self.ledger.locked() as conn:
            try:
                return self._finalize(conn, run, caps.timeout_s, outcome, last_path)
            finally:
                revoke_token(conn, token_id)

    def _abort(self, run_id: str, token_id: str, error: str) -> None:
        with self.ledger.locked() as conn:
            try:
                self._fail(conn, run_id, error)
            finally:
                revoke_token(conn, token_id)

    def _prepare(self, conn, run_id: str, version: str | None):
        run = get_run(conn, run_id)
        if run is None:
            raise NotFound(f"run {run_id} not found")
        if run.status != "queued":
            return run  # already claimed or finished: never launch twice
        caps = get_caps(conn)
        now = self.now()
        if spent_usd(conn, day=self.today()) + caps.usd_per_run > caps.usd_per_day:
            return self._fail(conn, run_id, f"daily cap {caps.usd_per_day} USD reached", "capped")
        update_run(conn, run_id, status="running", started=now)
        ensure_prompts(self.settings)
        request = request_from_props(run.kind, run.props["request"])
        try:
            plan = plan_run(conn, self.settings, run=run, request=request, now=now)
        except (UnknownProject, NotFound) as exc:
            return self._fail(conn, run_id, str(exc))
        actor = Actor(
            kind="agent",
            ref=run_id,
            model=request.model,
            backend=run.backend,
            tool=run.runner,
            tool_version=version,
            run_id=run_id,
        )
        token_id, secret = mint_token(
            conn,
            label=f"run:{run_id}",
            scope=plan.scope,
            actor=actor,
            ttl=timedelta(seconds=caps.timeout_s + 60),
        )
        try:
            update_run(conn, run_id, token_id=token_id)
            spec = RunSpec(
                run_id=run_id,
                kind=run.kind,
                runner=run.runner,
                backend=run.backend,
                model=run.model,
                prompt=plan.prompt,
                system_prompt=plan.system_prompt,
                cwd=plan.cwd,
                tools=plan.tools,
                usd_cap=caps.usd_per_run,
                timeout_s=caps.timeout_s,
                mcp_url=f"http://127.0.0.1:{self.settings.port}/mcp",
                mcp_token=secret,
                json_schema=plan.json_schema,
            )
            oauth_token = load_oauth_token(self.settings)
            try:
                env = run_env(
                    spec,
                    self.settings,
                    base=self.base_env if self.base_env is not None else os.environ,
                    oauth_token=oauth_token,
                )
            except MissingCredentials as exc:
                revoke_token(conn, token_id)
                return self._fail(conn, run_id, str(exc))
        except BaseException:
            revoke_token(conn, token_id)
            token_id = None
            raise
        return run, caps, plan, spec, env, token_id, oauth_token

    def _finalize(self, conn, run: Run, timeout_s: int, outcome, last_path) -> Run:
        if outcome.timed_out:
            return self._fail(conn, run.id, f"timeout after {timeout_s}s")
        if outcome.exit_code is None:
            return self._fail(conn, run.id, outcome.stderr_tail)
        try:
            if run.runner == "codex":
                last = last_path.read_text(encoding="utf-8") if last_path.exists() else None
                parsed = parse_codex(outcome.stdout, last)
            else:
                parsed = parse_claude_json(outcome.stdout)
        except ValueError:
            return self._fail(conn, run.id, (outcome.stderr_tail or outcome.stdout)[:500])
        if parsed.ok:
            status = "ok"
        elif parsed.subtype == "error_max_budget_usd":
            status = "capped"
        else:
            status = "failed"
        structured = parsed.structured
        answer = structured.get("answer") if isinstance(structured, dict) else None
        return update_run(
            conn,
            run.id,
            status=status,
            error=None if status == "ok" else parsed.error,
            tokens_in=parsed.usage.tokens_in,
            tokens_out=parsed.usage.tokens_out,
            tokens_cache=parsed.usage.tokens_cache,
            est_usd=parsed.est_usd,
            turns=parsed.turns,
            duration_ms=outcome.duration_ms,
            session_id=parsed.session_id,
            result_md=answer if isinstance(answer, str) else parsed.result_text,
            proposals=_proposals(parsed),
            log_path=outcome.log_path,
            finished=self.now(),
        )

    # -- worker ----------------------------------------------------------------------------

    def _next_queued(self) -> Run | None:
        with self.ledger.locked() as conn:
            return oldest_queued(conn)

    def _mark_failed(self, run_id: str, error: str) -> None:
        with self.ledger.locked() as conn:
            self._fail(conn, run_id, error)

    async def worker(self, stop: asyncio.Event, *, poll_s: float = 0.5) -> None:
        while not stop.is_set():
            try:
                run = await asyncio.to_thread(self._next_queued)
                if run is not None:
                    try:
                        await asyncio.to_thread(self.execute, run.id)
                    except Exception as exc:
                        log.exception("run %s crashed", run.id)
                        await asyncio.to_thread(self._mark_failed, run.id, str(exc)[:500])
                    continue
            except Exception:
                log.exception("worker iteration failed")
            try:
                await asyncio.wait_for(stop.wait(), poll_s)
            except TimeoutError:
                pass

    # -- proposals -------------------------------------------------------------------------

    def apply_proposal(self, run_id: str, index: int, *, actor: Actor) -> Run:
        with self.ledger.locked() as conn:
            run = get_run(conn, run_id)
            if run is None:
                raise NotFound(f"run {run_id} not found")
            if not 0 <= index < len(run.proposals):
                raise NotFound(f"proposal {index} not found")
            proposal = run.proposals[index]
            if proposal.applied_txn:
                raise AlreadyApplied(f"proposal {index} already applied")
            project_id: str | None
            if proposal.kind == "todo" and proposal.project == "inbox" and get_project_by_slug(conn, "inbox") is None:
                project_id = None
            else:
                project_id = resolve_project_ref(conn, proposal.project).id
            who = actor.model_copy(
                update={"proposed_by_run": run.id, "model": run.model, "backend": run.backend}
            )
            if proposal.kind == "event":
                assert project_id is not None
                created = events.create_event(
                    self.ledger,
                    who,
                    EventIn(
                        project_id=project_id,
                        kind=proposal.event_kind,
                        title=proposal.title,
                        body_md=proposal.body_md,
                        t_start=parse_iso(proposal.when) if proposal.when else self.now(),
                        status=proposal.status,
                        source="agent",
                    ),
                    intent=proposal.reason,
                )
                txn_id, applied_id = created.created_txn, created.id
            else:
                todo = todos.create_todo(
                    self.ledger,
                    who,
                    TodoIn(
                        project_id=project_id,
                        title=proposal.title,
                        body_md=proposal.body_md,
                        due=parse_iso(proposal.due) if proposal.due else None,
                    ),
                    intent=proposal.reason,
                )
                txn_id, applied_id = self.ledger.list_txns(limit=1)[0].id, todo.id
            updated = [
                p.model_copy(update={"applied_txn": txn_id, "applied_id": applied_id}) if i == index else p
                for i, p in enumerate(run.proposals)
            ]
            return update_run(conn, run_id, proposals=updated)


def _proposals(parsed: ParsedResult) -> list[Proposal]:
    items = parsed.structured.get("proposals") if isinstance(parsed.structured, dict) else None
    out: list[Proposal] = []
    for item in items if isinstance(items, list) else []:
        try:
            out.append(Proposal.model_validate(item))
        except ValidationError:
            continue
    return out
