"""RunService: queue, caps, per-run token lifecycle, execution and proposal application.

Lock discipline: every DB access happens inside `ledger.locked()`; `launch` and a routine run's
delivery run with no lock held.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pydantic import ValidationError

from trajectree.auth.tokens import mint_token, revoke_token
from trajectree.config import Settings
from trajectree.db.ledger import Ledger
from trajectree.models import SLOT_OF, Actor, EventIn, Proposal, Run, RunSlot, TodoIn
from trajectree.repo import NotFound, UnknownProject, UnknownSession, events, todos
from trajectree.repo.caps import get_caps
from trajectree.repo.projects import get_project_by_slug, resolve_project_ref
from trajectree.repo.routines import resolve_routine_ref
from trajectree.repo.signals import get_signal_by_key
from trajectree.repo.runs import (
    create_run,
    find_active_summarize,
    get_run,
    oldest_queued,
    running_runs,
    spent_usd,
    update_run,
)
from trajectree.runner.argv import claude_argv, codex_argv, mask_text, mcp_config_json, run_env
from trajectree.runner.confine import CONFINE_FAILED_EXIT, LandlockUnavailable, confine_argv, landlock_abi
from trajectree.runner.credentials import cli_version, ensure_cfg_dirs, load_oauth_token
from trajectree.runner.kinds import (
    KIND_OF,
    AskRequest,
    ReportRequest,
    RoutineRequest,
    RunPlan,
    RunRequest,
    SummarizeRequest,
    SummarizeSessionRequest,
    ensure_prompts,
    plan_run,
    request_from_props,
    runner_parts,
)
from trajectree.runner.launch import launch
from trajectree.runner.parse import ParsedResult, parse_claude_json, parse_codex
from trajectree.runner.routines import after_routine, finish_routine_run, retry_props, routine_cap
from trajectree.runner.sandbox import registered_folders, run_paths, runner_cfg_dir
from trajectree.runner.spec import (
    AlreadyApplied,
    MissingCredentials,
    RunsPaused,
    RunSpec,
)
from trajectree.timeutil import parse_when_lenient, to_db, utcnow

log = logging.getLogger("trajectree.runner")

_TOKEN_RE = re.compile(r"trj_[A-Za-z0-9_-]+")  # our run tokens, wherever an error message echoes one


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
        landlock_abi: Callable[[], int] = landlock_abi,
    ) -> None:
        self.ledger = ledger
        self.settings = settings
        self.launch = launch
        self.popen = popen
        self.now = now
        self.base_env = base_env
        self.version = version
        self.landlock_abi = landlock_abi

    def today(self) -> str:
        return self.now().astimezone(timezone.utc).strftime("%Y-%m-%d")

    def _binary(self, runner: str) -> str:
        return self.settings.codex_bin if runner == "codex" else self.settings.claude_bin

    # -- enqueue ---------------------------------------------------------------------------

    def enqueue(self, request: RunRequest) -> Run:
        with self.ledger.locked() as conn:
            if get_caps(conn).paused:
                raise RunsPaused("runs are paused")
            kind = KIND_OF[type(request)]
            default_runner, model = self.settings.default_runner, request.model
            routine_id, attempt, project_id = None, 1, None
            if isinstance(request, RoutineRequest):
                routine = resolve_routine_ref(conn, request.routine)
                default_runner = routine.runner or default_runner
                model = model or routine.model
                routine_id, attempt = routine.id, request.attempt
            runner, backend = runner_parts(request.runner or default_runner)
            if isinstance(request, SummarizeRequest):
                project_id = resolve_project_ref(conn, request.project).id
                active = find_active_summarize(conn, project_id)
                if active is not None:
                    return active  # one summary per project in flight
            elif isinstance(request, SummarizeSessionRequest):
                session = get_signal_by_key(conn, f"claude_session:{request.session_id}")
                if session is None:
                    raise UnknownSession(f"unknown session '{request.session_id}'")
                project_id = session.project_id
            elif isinstance(request, (AskRequest, RoutineRequest, ReportRequest)):
                for ref in request.projects or []:
                    resolve_project_ref(conn, ref)
            elif request.project:
                resolve_project_ref(conn, request.project)
            return create_run(
                conn,
                kind=kind,
                runner=runner,
                backend=backend,
                model=model,
                project_id=project_id,
                props={"request": request.model_dump(mode="json")},
                routine_id=routine_id,
                slot=SLOT_OF[kind],
                attempt=attempt,
            )

    # -- execute ---------------------------------------------------------------------------

    def _fail(self, conn, run_id: str, error: str, status: str = "failed", *, infra: bool = False) -> Run:
        """Finish a run unsuccessfully. `infra` marks a failure of ours (not the agent's) in `props`,
        with the time of the next attempt for a routine run that has attempts left."""
        fields: dict = {}
        if infra:
            run = get_run(conn, run_id)
            if run is None:
                raise NotFound(f"run {run_id} not found")
            fields["props"] = {**run.props, "failure": "infra", **retry_props(run, now=self.now())}
        failed = update_run(conn, run_id, status=status, error=error, finished=self.now(), **fields)
        after_routine(conn, failed)
        return failed

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
            run, caps, plan, spec, env, token_id, oauth_token, actor = prepared
        secrets = [spec.mcp_token, oauth_token or ""]
        run_dir = self.settings.runs_dir / run_id
        mcp_path = run_dir / "mcp.json"
        tmp_dir: Path | None = None
        try:
            try:
                try:
                    abi = self.landlock_abi()
                except LandlockUnavailable as exc:
                    # Fail closed: a run never starts unconfined.
                    return self._abort(run_id, token_id, f"landlock unavailable: {exc.strerror or exc}")
                ensure_cfg_dirs(self.settings)
                run_dir.mkdir(parents=True, exist_ok=True)
                cfg_dir = runner_cfg_dir(self.settings, run.runner, run.backend)
                cfg_dir.mkdir(mode=0o700, parents=True, exist_ok=True)  # a Landlock rule needs it to exist
                tmp_dir = Path(tempfile.mkdtemp(prefix=f"trajectree-run-{run_id}-"))  # created 0700
                home = run_dir / "home"
                home.mkdir(mode=0o700, exist_ok=True)
                env = {**env, "TMPDIR": str(tmp_dir), "HOME": str(home)}
                with self.ledger.locked() as conn:
                    folders = registered_folders(conn)
                    current = get_run(conn, run_id) or run
                    update_run(conn, run_id, props={**current.props, "landlock_abi": abi})
                paths = run_paths(  # resolves and stats paths, so it runs unlocked
                    self.settings, folders=folders, runner=run.runner, backend=run.backend,
                    run_dir=run_dir, tmp_dir=tmp_dir, cwd=plan.cwd,
                )
                last_path = run_dir / "last.md"
                stdin_text: str | None = None
                if run.runner == "codex":
                    schema_path = run_dir / "schema.json"
                    schema_path.write_text(json.dumps(plan.json_schema))
                    argv = codex_argv(
                        spec, binary=self.settings.codex_bin, schema_path=schema_path, last_message_path=last_path
                    )
                else:
                    # The run token lives in a private file and the prompt goes on stdin, so
                    # neither shows up in the process table.
                    _write_private(mcp_path, mcp_config_json(spec))
                    argv = claude_argv(spec, binary=self.settings.claude_bin, mcp_config_path=mcp_path.absolute())
                    stdin_text = spec.prompt
                outcome = self.launch(
                    confine_argv(paths.ro, paths.rw, argv),
                    env=env,
                    cwd=plan.cwd,
                    timeout_s=caps.timeout_s,
                    log_path=self.settings.runs_dir / f"{run_id}.log",
                    secrets=secrets,
                    popen=self.popen,
                    stdin_text=stdin_text,
                )
            except BaseException as exc:
                self._abort(run_id, token_id, mask_text(str(exc), secrets)[:500])
                raise
            with self.ledger.locked() as conn:
                try:
                    done = self._finalize(conn, run, plan, actor, caps.timeout_s, outcome, last_path, secrets)
                finally:
                    revoke_token(conn, token_id)
        finally:
            mcp_path.unlink(missing_ok=True)
            if tmp_dir is not None:
                shutil.rmtree(tmp_dir, ignore_errors=True)
        if done.kind != "routine":
            return done
        # Delivery runs with the lock released; the token is already revoked.
        return finish_routine_run(self.ledger, self.settings, done, plan, actor=actor, now=self.now())

    def _abort(self, run_id: str, token_id: str, error: str) -> Run:
        with self.ledger.locked() as conn:
            try:
                return self._fail(conn, run_id, error, infra=True)
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
        if run.kind == "routine" and (capped := routine_cap(conn, run, today=self.today(), now=now)):
            return self._fail(conn, run_id, capped, "capped")
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
            model=run.model,
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
                usd_cap=caps.usd_per_run if plan.usd_cap is None else min(caps.usd_per_run, plan.usd_cap),
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
                return self._fail(conn, run_id, str(exc), infra=True)
        except BaseException:
            revoke_token(conn, token_id)
            token_id = None
            raise
        return run, caps, plan, spec, env, token_id, oauth_token, actor

    def _finalize(
        self, conn, run: Run, plan: RunPlan, actor: Actor, timeout_s: int, outcome, last_path, secrets: list[str]
    ) -> Run:
        if outcome.timed_out:
            return self._fail(conn, run.id, f"timeout after {timeout_s}s", infra=True)
        if outcome.exit_code is None:
            return self._fail(conn, run.id, outcome.stderr_tail, infra=True)
        if wrapper_error := _confine_error(outcome):
            return self._fail(conn, run.id, wrapper_error, infra=True)
        try:
            if run.runner == "codex":
                last = last_path.read_text(encoding="utf-8") if last_path.exists() else None
                parsed = parse_codex(outcome.stdout, last)
            else:
                parsed = parse_claude_json(outcome.stdout)
        except ValueError:
            # stderr_tail is masked by launch; stdout is raw, so mask it before storing.
            return self._fail(
                conn, run.id, (outcome.stderr_tail or mask_text(outcome.stdout, secrets))[:500], infra=True
            )
        if parsed.ok:
            status = "ok"
        elif parsed.subtype == "error_max_budget_usd":
            status = "capped"
        else:
            status = "failed"
        structured = parsed.structured
        answer = structured.get("answer") if isinstance(structured, dict) else None
        done = update_run(
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
        return done  # a routine run's delivery and routine state follow in `execute`, unlocked

    # -- worker ----------------------------------------------------------------------------

    def _paused(self) -> bool:
        with self.ledger.locked() as conn:
            return get_caps(conn).paused

    def _next_queued(self, slot: RunSlot = "interactive") -> Run | None:
        with self.ledger.locked() as conn:
            return oldest_queued(conn, slot=slot)

    def _mark_failed(self, run_id: str, error: str) -> None:
        with self.ledger.locked() as conn:
            run = get_run(conn, run_id)
            # `execute` already finished (and masked) a run it aborted: never overwrite that.
            if run is not None and run.status in ("queued", "running"):
                self._fail(conn, run_id, error, infra=True)  # our code crashed, not the model

    def recover(self) -> int:
        """Fail every run left `running` by a previous daemon and revoke its token."""
        with self.ledger.locked() as conn:
            orphans = running_runs(conn)
            for run in orphans:
                self._fail(conn, run.id, "daemon restarted", infra=True)
                if run.token_id:
                    revoke_token(conn, run.token_id)  # already gone is fine
                (self.settings.runs_dir / run.id / "mcp.json").unlink(missing_ok=True)
        return len(orphans)

    async def worker(self, stop: asyncio.Event, *, slot: RunSlot = "interactive", poll_s: float = 0.5) -> None:
        """Drain queued runs of one slot, one at a time."""
        while not stop.is_set():
            try:
                # Paused: queued runs stay queued until the owner resumes.
                paused = await asyncio.to_thread(self._paused)
                run = None if paused else await asyncio.to_thread(self._next_queued, slot)
                if run is not None:
                    try:
                        await asyncio.to_thread(self.execute, run.id)
                    except Exception as exc:
                        # No traceback or exception text: a launch error can carry the run token.
                        log.error("run %s crashed: %s", run.id, type(exc).__name__)
                        masked = _TOKEN_RE.sub("***", str(exc))
                        await asyncio.to_thread(
                            self._mark_failed, run.id, f"{type(exc).__name__}: {masked}"[:500]
                        )
                    continue
            except Exception:
                log.exception("worker iteration failed")
            try:
                await asyncio.wait_for(stop.wait(), poll_s)
            except TimeoutError:
                pass

    # -- proposals -------------------------------------------------------------------------

    @staticmethod
    def _open_proposal(conn, run_id: str, index: int) -> tuple[Run, Proposal]:
        run = get_run(conn, run_id)
        if run is None:
            raise NotFound(f"run {run_id} not found")
        if not 0 <= index < len(run.proposals):
            raise NotFound(f"proposal {index} not found")
        proposal = run.proposals[index]
        if proposal.applied_txn:
            raise AlreadyApplied(f"proposal {index} already applied")
        if proposal.dismissed:
            raise AlreadyApplied(f"proposal {index} was dismissed")
        return run, proposal

    def dismiss_proposal(self, run_id: str, index: int) -> Run:
        with self.ledger.locked() as conn:
            run, _ = self._open_proposal(conn, run_id, index)
            updated = [
                p.model_copy(update={"dismissed": True}) if i == index else p for i, p in enumerate(run.proposals)
            ]
            return update_run(conn, run_id, proposals=updated)

    def apply_proposal(self, run_id: str, index: int, *, actor: Actor) -> Run:
        with self.ledger.locked() as conn:
            run, proposal = self._open_proposal(conn, run_id, index)
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
                        t_start=parse_when_lenient(proposal.when) if proposal.when else self.now(),
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
                        due=parse_when_lenient(proposal.due) if proposal.due else None,
                    ),
                    intent=proposal.reason,
                )
                # create_todo returns no txn id: find the txn that inserted this todo
                # (`pk` is stored as `json.dumps` of the key list, see Ledger._record).
                row = conn.execute(
                    "SELECT txn_id FROM change WHERE table_name = 'todo' AND op = 'INSERT' AND pk = ?"
                    " ORDER BY txn_id DESC LIMIT 1",
                    (json.dumps([todo.id]),),
                ).fetchone()
                assert row is not None
                txn_id, applied_id = row[0], todo.id
            updated = [
                p.model_copy(update={"applied_txn": txn_id, "applied_id": applied_id}) if i == index else p
                for i, p in enumerate(run.proposals)
            ]
            return update_run(conn, run_id, proposals=updated)


def _confine_error(outcome) -> str | None:
    """The wrapper's own error line when it, not the runner, ended the run: Landlock could not be
    applied (exit 97) or the runner binary could not be executed (exit 127)."""
    if outcome.exit_code == CONFINE_FAILED_EXIT:
        prefix = "trajectree-confine:"
    elif outcome.exit_code == 127:
        prefix = "trajectree-confine: cannot run"
    else:
        return None
    return next((line for line in outcome.stderr_tail.splitlines() if line.startswith(prefix)), None)


def _write_private(path, text: str) -> None:
    """Write `text` to a file created 0600 (never world-readable, not even briefly)."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(path, 0o600)


def _proposals(parsed: ParsedResult) -> list[Proposal]:
    items = parsed.structured.get("proposals") if isinstance(parsed.structured, dict) else None
    out: list[Proposal] = []
    for item in items if isinstance(items, list) else []:
        try:
            proposal = Proposal.model_validate(item)
            # Normalise agent-written dates now; a date that cannot be read drops the proposal.
            dates = {
                name: to_db(parse_when_lenient(value))
                for name in ("when", "due")
                if (value := getattr(proposal, name))
            }
        except (ValidationError, ValueError):
            continue
        # The bookkeeping fields are ours, never the agent's.
        fresh = {"applied_txn": None, "applied_id": None, "dismissed": False}
        out.append(proposal.model_copy(update={**dates, **fresh}))
    return out
