"""RunService: queue, caps, per-run token lifecycle, execution and proposal application.

Lock discipline: every DB access happens inside `ledger.locked()`; `launch` and a routine run's
delivery run with no lock held.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Mapping
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from trajectree.auth.tokens import mint_token, revoke_token
from trajectree.config import Settings
from trajectree.connectors.codex_sessions import session_signal
from trajectree.context.history import collect_history, session_history
from trajectree.context.stretches import estimate_render_size, estimate_sizes, render_blocks, render_stretches
from trajectree.db.ledger import Ledger
from trajectree.ids import new_id
from trajectree.importing import local_midnight
from trajectree.models import SLOT_OF, Actor, EventIn, Proposal, Routine, Run, RunSlot, Stretch, TodoIn
from trajectree.repo import NotFound, UnknownProject, UnknownSession, events, todos
from trajectree.repo.caps import get_caps
from trajectree.repo.edges import list_edges
from trajectree.repo.inspections import (
    INSPECT_KINDS,
    active_inspection,
    advance_watermark,
    covered_map,
    episode_material,
    get_watermark,
    project_is_live,
    reset_watermark_since,
    set_watermark,
    uninspected_material,
    uninspected_stretches,
)
from trajectree.repo.projects import get_project_by_slug, resolve_project_ref
from trajectree.repo.routines import resolve_routine_ref
from trajectree.repo.runs import (
    create_run,
    find_active_summarize,
    get_run,
    oldest_queued,
    running_runs,
    spent_usd,
    update_run,
)
from trajectree.runner.argv import claude_argv, codex_argv, mask_text, mcp_config_json, run_env, run_secrets
from trajectree.runner.catalog import Resolved, load_catalog, resolve_model
from trajectree.runner.confine import CONFINE_FAILED_EXIT, LandlockUnavailable, confine_argv, landlock_abi
from trajectree.runner.credentials import cli_version, ensure_cfg_dirs, load_oauth_token
from trajectree.runner.kinds import (
    KIND_OF,
    AskRequest,
    InspectBoundariesRequest,
    InspectRequest,
    ReportRequest,
    RoutineRequest,
    RunPlan,
    RunRequest,
    SummarizeRequest,
    StartingPointRequest,
    SummarizeSessionRequest,
    ensure_prompts,
    plan_run,
    project_folder_hosts,
    project_folders,
    HISTORY_BUDGET_CHARS,
    request_from_props,
    runner_parts,
)
from trajectree.runner.launch import launch
from trajectree.runner.parse import ParsedResult, parse_claude_json, parse_codex
from trajectree.runner.routines import after_routine, finish_routine_run, retry_props, routine_cap
from trajectree.runner.safe_io import MiB, read_regular
from trajectree.runner.sandbox import registered_folders, run_paths, runner_cfg_dir
from trajectree.runner.spec import (
    AlreadyApplied,
    InspectionActive,
    MissingCredentials,
    NothingToInspect,
    RunsPaused,
    RunSpec,
)
from trajectree.timeutil import parse_iso, parse_when_lenient, to_db, utcnow

log = logging.getLogger("trajectree.runner")

_TOKEN_RE = re.compile(r"trj_[A-Za-z0-9_-]+")  # our run tokens, wherever an error message echoes one
LAST_MESSAGE_MAX_BYTES = MiB  # a larger Codex final message is refused, not truncated
RECENT_BLOCKS = 20  # the project's most recent blocks an inspect run is shown besides each stretch's own


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

    def validate(self, conn, request: RunRequest, *, routine: Routine | None = None) -> tuple[Resolved, str]:
        """The checks `enqueue` makes before it creates a run, so a caller can run them before
        writing anything: runs not paused, runner well formed, the model resolved against the
        stored catalog. A routine run that names neither runner nor model takes the routine's
        choice; naming either replaces it wholesale (a runner alone means its default model). Returns the resolved choice and the backend. Raises `RunsPaused` or `ValueError`
        (a `ModelChoiceError` for the model). The caller holds the ledger lock."""
        if get_caps(conn).paused:
            raise RunsPaused("runs are paused")
        runner, model = request.runner, request.model
        if routine is not None and not (request.runner or request.model):
            runner, model = routine.runner, routine.model
        if runner is not None:
            runner_parts(runner)  # refuses a malformed runner before it is looked up
        resolved = resolve_model(load_catalog(conn), model, runner)
        return resolved, runner_parts(resolved.runner)[1]

    def check(self, request: RunRequest) -> None:
        with self.ledger.locked() as conn:
            self.validate(conn, request)

    def enqueue(self, request: RunRequest, *, props: Mapping[str, Any] | None = None) -> Run:
        """Create a queued run for `request`; `props` adds entries to the run's props (an
        inspection series passes the boundary pass's `sizes` and the episode list this way)."""
        with self.ledger.locked() as conn:
            kind = KIND_OF[type(request)]
            routine, routine_id, attempt, project_id = None, None, 1, None
            if isinstance(request, RoutineRequest):
                routine = resolve_routine_ref(conn, request.routine)
                routine_id, attempt = routine.id, request.attempt
            resolved, backend = self.validate(conn, request, routine=routine)
            if isinstance(request, StartingPointRequest):
                project_id = resolve_project_ref(conn, request.project).id
            elif isinstance(request, SummarizeRequest):
                project_id = resolve_project_ref(conn, request.project).id
                active = find_active_summarize(conn, project_id)
                if active is not None:
                    return active  # one summary per project in flight
            elif isinstance(request, SummarizeSessionRequest):
                session = session_signal(conn, request.session_id)
                if session is None:
                    raise UnknownSession(f"unknown session '{request.session_id}'")
                project_id = session.project_id
            elif isinstance(request, (InspectRequest, InspectBoundariesRequest)):
                project = resolve_project_ref(conn, request.project)
                project_id = project.id
                if active_inspection(conn, project_id) is not None:
                    raise InspectionActive(project.slug)  # one inspection series per project at a time
            elif isinstance(request, (AskRequest, RoutineRequest, ReportRequest)):
                for ref in request.projects or []:
                    resolve_project_ref(conn, ref)
            elif request.project:
                resolve_project_ref(conn, request.project)
            run_props: dict[str, Any] = {"request": request.model_dump(mode="json")}
            if resolved.warning:
                run_props["model_warning"] = resolved.warning
            if isinstance(request, InspectRequest):
                run_props.update(
                    series_id=request.series_id, episode_no=request.episode_no, episodes=request.episodes
                )
            elif isinstance(request, InspectBoundariesRequest):
                # The boundary pass precedes episode 1; `episodes` is set once its answer validates.
                run_props.update(series_id=request.series_id, episode_no=0, episodes=0)
            run_props.update(props or {})
            return create_run(
                conn,
                kind=kind,
                runner=resolved.runner,
                backend=backend,
                model=resolved.model,
                project_id=project_id,
                props=run_props,
                routine_id=routine_id,
                slot=SLOT_OF[kind],
                attempt=attempt,
            )

    # -- inspections ------------------------------------------------------------------------

    def inspect(self, request: InspectRequest) -> Run:
        """Start an inspection series for the project: one `inspect` run when its uninspected
        material fits `inspect_budget_chars`, else an `inspect_boundaries` run that splits it into
        episodes. `since` re-inspects from that local midnight. The DB is read under the lock;
        the material is sized (transcripts read) with the lock released, then the run is enqueued.
        Raises `InspectionActive`, `NothingToInspect`, `RunsPaused` or `ValueError`."""
        now = self.now()
        with self.ledger.locked() as conn:
            project = resolve_project_ref(conn, request.project)
            if active_inspection(conn, project.id) is not None:
                raise InspectionActive(project.slug)
            self.validate(conn, request)  # a refused model or paused runs move no watermark
            before = get_watermark(conn, project.id)
            if request.since is not None:
                reset_watermark_since(conn, project.id, local_midnight(request.since, self.settings.tz))
            stretches, signals = uninspected_material(conn, self.settings, project, now=now)
            if not stretches:
                if request.since is not None:
                    set_watermark(conn, project.id, before)  # nothing started: leave it as it was
                if project_is_live(conn, self.settings, project.id, now=now):
                    minutes = max(1, self.settings.segment_gap_s // 60)
                    raise NothingToInspect(
                        project.slug,
                        f"a session is live in {project.slug}; inspect once it has been quiet for {minutes} minutes",
                    )
                raise NothingToInspect(project.slug)
            project_events = events.list_events(conn, project_ids=[project.id])  # blocks and pills
            edges = list_edges(conn, event_ids=[e.id for e in project_events])
        # Each transcript is read once, here; the full size follows from the digest sizes.
        sizes = estimate_sizes(stretches, signals, self.settings)
        size = estimate_render_size(
            stretches, signals, sizes, blocks=project_events, edges=edges, settings=self.settings
        )
        budget = self.settings.inspect_budget_chars
        choice = {"project": project.id, "runner": request.runner, "model": request.model}
        if size <= budget:
            return self.enqueue(
                InspectRequest(
                    **choice,
                    episode_start=stretches[0].t_start,
                    episode_end=stretches[-1].t_end,
                    series_id=new_id(),
                    episode_no=1,
                    episodes=1,
                )
            )
        return self.enqueue(
            InspectBoundariesRequest(**choice, series_id=new_id(), budget_chars=budget), props={"sizes": sizes}
        )

    def _collect_inspection(self, run: Run) -> dict[str, Any] | None:
        """For a queued `inspect` run: the stretches inside its episode, rendered with no lock held.
        What the material covers (signal key -> end as read now, and the last stretch's end) is then
        stored on the run, so the watermark later advances by exactly what the run was shown: a
        segment cut from an over-budget render, or ingested after this read, is not covered."""
        request = request_from_props(run.kind, run.props["request"])
        assert isinstance(request, InspectRequest)
        if run.project_id is None:
            return None
        with self.ledger.locked() as conn:
            stretches, signals = episode_material(
                conn, self.settings, run.project_id, start=request.episode_start, end=request.episode_end
            )
            project_events = events.list_events(conn, project_ids=[run.project_id])
            edges = list_edges(conn, event_ids=[e.id for e in project_events])
        if not stretches:
            return None  # the planner fails the run: inspection material missing
        stretches_md, _, rendered = render_stretches(
            stretches,
            signals,
            blocks=project_events,
            edges=edges,
            settings=self.settings,
            budget_chars=self.settings.inspect_budget_chars,  # an episode over the budget is cut
        )
        blocks = sorted((e for e in project_events if e.tier == "block"), key=lambda e: (e.t_end or e.t_start, e.id))
        blocks_md = render_blocks(
            blocks[-RECENT_BLOCKS:], edges, {e.id: e for e in project_events}, tz=self.settings.tz
        )
        with self.ledger.locked() as conn:
            current = get_run(conn, run.id) or run
            update_run(
                conn,
                run.id,
                props={
                    **current.props,
                    "covered": covered_map([s for sid, s in signals.items() if sid in rendered]),
                    "material_end": to_db(stretches[-1].t_end),
                },
            )
        return {"stretches_md": stretches_md, "blocks_md": blocks_md or "- (none)"}

    def _after_inspect(self, done: Run) -> Run:
        """Carry an inspection series on after one of its runs finished (no lock held on entry).

        An ok boundary pass has its episodes validated (else it fails with `boundaries invalid: …`)
        and enqueues episode 1; an ok `inspect` run advances the watermark and enqueues the next
        episode, if any. Any other status stops the series. Returns the run as now stored."""
        if done.status != "ok" or done.project_id is None:
            return done
        request = request_from_props(done.kind, done.props["request"])
        with self.ledger.locked() as conn:
            current = get_run(conn, done.id) or done
            project = resolve_project_ref(conn, done.project_id)
            if isinstance(request, InspectBoundariesRequest):
                stretches = uninspected_stretches(conn, self.settings, project, now=self.now(), live_check=False)
                try:
                    episodes = _validate_episodes(current.props.get("episodes_raw"), stretches, self.settings.tz)
                except ValueError as exc:
                    return self._fail(conn, done.id, f"boundaries invalid: {exc}"[:500])
                current = update_run(
                    conn, done.id, props={**current.props, "episodes": len(episodes), "episodes_json": episodes}
                )
                series, number, total = episodes, 1, len(episodes)
            else:
                assert isinstance(request, InspectRequest)
                material_end = current.props.get("material_end")
                end = request.episode_end or (parse_iso(material_end) if material_end else None)
                if end is not None:
                    advance_watermark(
                        conn,
                        project.id,
                        inspected_at=end,
                        started_at=done.started,
                        series_id=request.series_id,
                        covered=current.props.get("covered") or {},
                    )
                series = current.props.get("series_episodes") or []
                number, total = request.episode_no + 1, request.episodes
                if number > total or len(series) < number:
                    return current  # the series is complete
        episode = series[number - 1]
        nxt = InspectRequest(
            project=project.id,
            runner=request.runner,
            model=request.model,
            episode_start=datetime.fromisoformat(episode["start"]),
            episode_end=datetime.fromisoformat(episode["end"]),
            series_id=request.series_id,
            episode_no=number,
            episodes=total,
        )
        try:
            self.enqueue(nxt, props={"series_episodes": series, "episode_label": episode.get("label", "")})
        except (InspectionActive, RunsPaused, UnknownProject, ValueError) as exc:
            # The series stops here; the watermark keeps what the finished episodes covered.
            log.warning("inspection series %s stopped before episode %d: %s", request.series_id, number, type(exc).__name__)
            with self.ledger.locked() as conn:
                current = get_run(conn, done.id) or current
                current = update_run(conn, done.id, props={**current.props, "series_stopped": str(exc)[:500]})
        return current

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
        prelocked = self._collect_history(current)  # None unless a queued starting_point or inspect run
        with self.ledger.locked() as conn:
            prepared = self._prepare(conn, run_id, version, prelocked)  # revokes its own token if it fails
            if isinstance(prepared, Run):
                return prepared
            run, caps, plan, spec, env, token_id, oauth_token, actor = prepared
        secrets = [spec.mcp_token, oauth_token or "", *run_secrets(env)]
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
            with contextlib.suppress(OSError):  # the run may have left a directory in its place
                mcp_path.unlink(missing_ok=True)
            if tmp_dir is not None:
                shutil.rmtree(tmp_dir, ignore_errors=True)
        if done.kind in INSPECT_KINDS:
            return self._after_inspect(done)  # the token is revoked; the next episode is only queued
        if done.kind != "routine":
            return done
        # Delivery runs with the lock released; the token is already revoked.
        return finish_routine_run(self.ledger, self.settings, done, plan, actor=actor, now=self.now())

    def _collect_history(self, run: Run | None) -> dict[str, Any] | None:
        """For a queued starting_point run, its git, folder and session history; `git log` and the folder
        walk run with no lock held, only the folder lookup takes it. For a queued inspect run, its
        episode's material (see `_collect_inspection`); a boundary pass needs nothing (sizes are in props)."""
        if run is None or run.status != "queued":
            return None
        if run.kind == "inspect":
            return self._collect_inspection(run)
        if run.kind != "starting_point":
            return None
        request = request_from_props(run.kind, run.props["request"])
        assert isinstance(request, StartingPointRequest)
        with self.ledger.locked() as conn:
            folders = project_folders(conn, self.settings, run.project_id) if run.project_id else []
            hosts, registered = project_folder_hosts(conn, run.project_id) if run.project_id else ([], [])
        history = collect_history(
            self.settings,
            folders=folders,
            starts=request.starts,
            tz=self.settings.tz,
            budget_chars=HISTORY_BUDGET_CHARS,
        )
        sessions = session_history(
            self.settings, hosts, registered, starts=request.starts, budget_chars=HISTORY_BUDGET_CHARS
        )
        return {"history": history, "sessions": sessions}

    def _abort(self, run_id: str, token_id: str, error: str) -> Run:
        with self.ledger.locked() as conn:
            try:
                return self._fail(conn, run_id, error, infra=True)
            finally:
                revoke_token(conn, token_id)

    def _prepare(self, conn, run_id: str, version: str | None, prelocked: dict[str, Any] | None = None):
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
            plan = plan_run(conn, self.settings, run=run, request=request, now=now, prelocked=prelocked)
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
                parsed = parse_codex(outcome.stdout, _last_message(last_path))
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
        extra: dict[str, Any] = {}
        if run.kind == "inspect_boundaries":
            # Stored raw; `_after_inspect` validates the episodes once the run is finished.
            current = get_run(conn, run.id) or run
            raw = structured.get("episodes") if isinstance(structured, dict) else None
            extra["props"] = {**current.props, "episodes_raw": raw}
        done = update_run(
            conn,
            run.id,
            status=status,
            error=None if status == "ok" else parsed.error,
            tokens_in=parsed.usage.tokens_in,
            tokens_out=parsed.usage.tokens_out,
            tokens_cache=parsed.usage.tokens_cache,
            est_usd=parsed.est_usd,
            **({"model": parsed.model} if parsed.model and not run.model else {}),  # a runner-only run learns its model
            turns=parsed.turns,
            duration_ms=outcome.duration_ms,
            session_id=parsed.session_id,
            result_md=answer if isinstance(answer, str) else parsed.result_text,
            proposals=_proposals(parsed),
            log_path=outcome.log_path,
            finished=self.now(),
            **extra,
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
                        tier=proposal.tier or "block",
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


def _last_message(path: Path) -> str | None:
    """Codex's final message file, read without following a symlink or blocking on a special
    file the run left in its place; a refused file counts as no message (the run fails)."""
    try:
        data = read_regular(path, max_bytes=LAST_MESSAGE_MAX_BYTES)
    except OSError as exc:
        log.warning("ignoring %s: %s", path.name, exc.strerror or type(exc).__name__)
        return None
    return None if data is None else data.decode("utf-8", errors="replace")


def _write_private(path, text: str) -> None:
    """Write `text` to a file created 0600 (never world-readable, not even briefly)."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(path, 0o600)


def _episode_day(value: object, zone: ZoneInfo, n: int, field: str) -> date:
    """An episode boundary as a date in `zone`: `YYYY-MM-DD`, or an ISO datetime taken at its local date."""
    if isinstance(value, str):
        text = value.strip()
        try:
            return date.fromisoformat(text)
        except ValueError:
            pass
        try:
            moment = datetime.fromisoformat(text)
            return (moment.astimezone(zone) if moment.tzinfo else moment).date()
        except ValueError:
            pass
    shown = repr(value)
    raise ValueError(f"episode {n} has an unparsable {field} date {shown[:40]}")


def _validate_episodes(raw: object, stretches: list[Stretch], tz: str) -> list[dict[str, str]]:
    """A boundary pass's episodes checked against the settled uninspected stretches and snapped to
    their edges: `[{"start", "end", "label"}]` with ISO instants. Raises `ValueError(reason)` when the
    list is empty, a date cannot be read, the episodes are out of order or overlap, one lies outside
    the uninspected span or covers no stretch, or two would split one stretch. An episode over the
    budget is accepted: its render is cut."""
    if not stretches:
        raise ValueError("no uninspected stretches left")
    if not isinstance(raw, list) or not raw:
        raise ValueError("no episodes")
    zone = ZoneInfo(tz)
    first_day = stretches[0].t_start.astimezone(zone).date()
    last_day = stretches[-1].t_end.astimezone(zone).date()
    days: list[tuple[date, date, str]] = []
    for n, item in enumerate(raw, 1):
        if not isinstance(item, dict):
            raise ValueError(f"episode {n} is not an object")
        start = _episode_day(item.get("start"), zone, n, "start")
        end = _episode_day(item.get("end"), zone, n, "end")
        if end < start:
            raise ValueError(f"episode {n} ends before it starts")
        if start < first_day or end > last_day:
            raise ValueError(f"episode {n} ({start}..{end}) is outside the uninspected span {first_day}..{last_day}")
        if days and start < days[-1][0]:
            raise ValueError(f"episodes out of order at episode {n}")
        if days and start <= days[-1][1]:
            raise ValueError(f"episodes {n - 1} and {n} overlap")
        days.append((start, end, " ".join(str(item.get("label") or "").split())[:200]))
    out: list[dict[str, str]] = []
    previous_end: datetime | None = None
    for n, (start, end, label) in enumerate(days, 1):
        lo, hi = local_midnight(start, tz), local_midnight(end + timedelta(days=1), tz)
        # A start inside a stretch snaps to that stretch's start, an end inside one to its end.
        inside = [st for st in stretches if st.t_start < hi and st.t_end >= lo]
        if not inside:
            raise ValueError(f"episode {n} ({start}..{end}) covers no stretch")
        if previous_end is not None and inside[0].t_start <= previous_end:
            raise ValueError(f"episodes {n - 1} and {n} split a stretch")
        previous_end = inside[-1].t_end
        out.append({"start": inside[0].t_start.isoformat(), "end": inside[-1].t_end.isoformat(), "label": label})
    return out


def _proposals(parsed: ParsedResult) -> list[Proposal]:
    items = parsed.structured.get("proposals") if isinstance(parsed.structured, dict) else None
    out: list[Proposal] = []
    for item in items if isinstance(items, list) else []:
        try:
            # The schema makes optional fields nullable; null means "use the default".
            if isinstance(item, dict):
                item = {k: v for k, v in item.items() if v is not None}
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
