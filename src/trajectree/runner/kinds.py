"""Run kinds: request models, editable prompts, proposal schema and run planning."""

from __future__ import annotations

import re
import string
from collections.abc import Mapping
from datetime import date, datetime, timedelta, timezone
from importlib import resources
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import apsw
from pydantic import AwareDatetime, BaseModel, Field, field_validator

from trajectree.auth.scopes import Scope
from trajectree.config import Settings
from trajectree.connectors.codex_sessions import digest_session, session_signal
from trajectree.connectors.paths import to_local
from trajectree.context.digest import activity_digest
from trajectree.context.history import HistoryInput, block_date, local_midnight
from trajectree.context.pack import build_context_pack
from trajectree.importing import IMPORT_SOURCES
from trajectree.models import RUNNER_PATTERN, Delivery, Project, ReportKind, Run, RunKind
from trajectree.repo import NotFound
from trajectree.repo.projects import get_project_by_slug, list_projects, resolve_project_ref
from trajectree.repo.resources import list_resources
from trajectree.repo.review import pending_txns
from trajectree.repo.routines import resolve_routine_ref
from trajectree.runner.spec import ToolPolicy


class SummarizeRequest(BaseModel):
    project: str
    runner: str | None = None
    model: str | None = None


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    projects: list[str] | None = None
    start: AwareDatetime | None = None
    end: AwareDatetime | None = None
    token_budget: int = Field(4000, ge=200, le=100_000)
    runner: str | None = None
    model: str | None = None


class QuickAddRequest(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    project: str | None = None
    runner: str | None = None
    model: str | None = None


class RoutineRequest(BaseModel):
    routine: str
    projects: list[str] | None = None
    reason: str = "manual"
    attempt: int = Field(1, ge=1, le=3)
    runner: str | None = None
    model: str | None = None


class SummarizeSessionRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=128)
    runner: str | None = None
    model: str | None = None


class ReportRequest(BaseModel):
    kind: ReportKind
    title: str = Field(min_length=1, max_length=200)
    projects: list[str] = Field(min_length=1)
    start: AwareDatetime | None = None
    end: AwareDatetime | None = None
    token_budget: int = Field(6000, ge=200, le=100_000)
    runner: str | None = None
    model: str | None = None


class StartingPointRequest(BaseModel):
    project: str
    starts: dict[str, date]  # each source that skipped history -> where its import starts
    runner: str | None = None
    model: str | None = None

    @field_validator("starts")
    @classmethod
    def _known_sources(cls, starts: dict[str, date]) -> dict[str, date]:
        if not starts:
            raise ValueError("starts needs at least one source")
        for source in starts:
            if source not in IMPORT_SOURCES or source == "remote":
                raise ValueError(f"unknown source '{source}'")
        return starts


RunRequest = (
    SummarizeRequest
    | SummarizeSessionRequest
    | AskRequest
    | QuickAddRequest
    | RoutineRequest
    | ReportRequest
    | StartingPointRequest
)

KIND_OF: dict[type[BaseModel], RunKind] = {
    SummarizeRequest: "summarize",
    SummarizeSessionRequest: "summarize_session",
    AskRequest: "ask",
    QuickAddRequest: "quick_add",
    RoutineRequest: "routine",
    ReportRequest: "report",
    StartingPointRequest: "starting_point",
}
_CLASS_OF = {kind: cls for cls, kind in KIND_OF.items()}


def request_from_props(kind: RunKind, data: dict[str, Any]) -> RunRequest:
    return _CLASS_OF[kind].model_validate(data)  # type: ignore[return-value]


PROPOSAL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["answer", "proposals"],
    "properties": {
        "answer": {"type": "string"},
        "proposals": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                # OpenAI strict mode (codex): every property is required, optional ones are nullable.
                "required": ["kind", "project", "title", "reason", "event_kind", "when", "due", "body_md", "status"],
                "properties": {
                    "kind": {"enum": ["event", "todo"]},
                    "project": {"type": "string"},
                    "title": {"type": "string"},
                    "reason": {"type": "string"},
                    "event_kind": {"type": ["string", "null"]},
                    "when": {"type": ["string", "null"]},
                    "due": {"type": ["string", "null"]},
                    "body_md": {"type": ["string", "null"]},
                    "status": {"enum": ["happened", "planned", "suggested", None]},
                },
            },
        },
    },
}

SUMMARIZE_SYSTEM = (
    "You are updating the owner's project tracker. Write through the trajectree MCP tools only: "
    "exactly one set_project_summary call for project `$project_slug`, nothing else; "
    "anything else you notice goes into `proposals`."
)
READ_ONLY_SYSTEM = (
    "You are answering from the owner's project tracker. You have read-only access; "
    "put anything worth recording into `proposals` instead of writing."
)
ROUTINE_SYSTEM = (
    "You are the routine `$routine_name` maintaining the owner's project tracker. "
    "Write only through the trajectree MCP tools, each with a one-line intent; writes land pending review. "
    "Anything uncertain goes into `proposals`."
)
REPORT_SYSTEM = (
    "You draft one $kind for the owner. Call save_report_draft exactly once with Marp-flavoured Markdown "
    "(front matter `marp: true`, slides separated by `---`, speaker notes as HTML comments) and the intent "
    "`draft $kind`; write nothing else."
)

STARTING_POINT_SYSTEM = (
    "You condense a project's history for the owner's tracker. Write through the trajectree MCP tools only: "
    'exactly one record_event call for project `$project_slug` with kind "starting_point", status "happened" '
    "and when `$when`, nothing else; anything else you notice goes into `proposals`."
)
# Characters of history each starting-point input may use.
HISTORY_BUDGET_CHARS = 20_000


def ensure_prompts(settings: Settings) -> None:
    """Create `prompts_dir` and copy each packaged prompt that is missing; never overwrite edits."""
    settings.prompts_dir.mkdir(parents=True, exist_ok=True)
    for source in resources.files("trajectree.runner.prompts").iterdir():
        if not source.name.endswith(".md"):
            continue
        target = settings.prompts_dir / source.name
        if not target.exists():
            target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")


def load_prompt(settings: Settings, kind: RunKind) -> str:
    return (settings.prompts_dir / f"{kind}.md").read_text(encoding="utf-8")


def render_prompt(template: str, vars: Mapping[str, str]) -> str:
    return string.Template(template).safe_substitute(vars)


def runner_parts(runner: str) -> tuple[str, str]:
    """Return `(runner, backend)`; accepts exactly what `RUNNER_PATTERN` accepts."""
    if not re.fullmatch(RUNNER_PATTERN, runner):
        raise ValueError(f"invalid runner '{runner}'")
    return runner, runner.removeprefix("ccx:")


class RunPlan(BaseModel):
    prompt: str
    system_prompt: str
    cwd: Path
    tools: ToolPolicy
    json_schema: dict[str, Any]
    scope: Scope
    usd_cap: float | None = None
    delivery: Delivery = "none"
    routine_id: str | None = None
    delivery_project_id: str | None = None
    routine_name: str | None = None
    routine_tz: str | None = None


def _day(moment: datetime, tz: str | None = None) -> str:
    return moment.astimezone(ZoneInfo(tz) if tz else timezone.utc).strftime("%Y-%m-%d")


def plan_run(
    conn: apsw.Connection,
    settings: Settings,
    *,
    run: Run,
    request: RunRequest,
    now: datetime,
    prelocked: dict[str, Any] | None = None,
) -> RunPlan:
    """Turn a run request into a plan. The caller holds the lock and has seeded the prompts.

    `prelocked` carries what the caller collected with no lock held (a starting_point run's
    `{"history": HistoryInput}`)."""
    if isinstance(request, StartingPointRequest):
        return _plan_starting_point(conn, settings, run, request, now, prelocked or {})
    if isinstance(request, SummarizeRequest):
        return _plan_summarize(conn, settings, run, request, now)
    if isinstance(request, SummarizeSessionRequest):
        return _plan_summarize_session(conn, settings, run, request, now)
    if isinstance(request, AskRequest):
        return _plan_ask(conn, settings, run, request, now)
    if isinstance(request, RoutineRequest):
        return _plan_routine(conn, settings, run, request, now)
    if isinstance(request, ReportRequest):
        return _plan_report(conn, settings, run, request, now)
    return _plan_quick_add(conn, settings, run, request, now)


def _plan_summarize(
    conn: apsw.Connection, settings: Settings, run: Run, request: SummarizeRequest, now: datetime
) -> RunPlan:
    project = resolve_project_ref(conn, run.project_id or request.project)
    since = project.summary_at or now - timedelta(days=90)
    pack = build_context_pack(
        conn, project_ids=[project.id], t_from=since, t_to=now + timedelta(days=30), token_budget=6000
    )
    prompt = render_prompt(
        load_prompt(settings, "summarize"),
        {
            "project_title": project.title,
            "project_slug": project.slug,
            "previous_summary": project.summary_md or "_none yet_",
            "context_pack": pack.markdown,
            "activity_digest": activity_digest(conn, project_ids=[project.id], since=since, until=now),
            "since": _day(since),
            "today": _day(now),
        },
    )
    return RunPlan(
        prompt=prompt,
        system_prompt=render_prompt(SUMMARIZE_SYSTEM, {"project_slug": project.slug}),
        cwd=_folder_cwd(conn, settings, project.id) or settings.runs_dir / run.id,
        tools=ToolPolicy(
            tools=["Read", "Grep", "Glob", "Bash"],
            allowed=["Read", "Grep", "Glob", "Bash(git log *)", "mcp__trajectree__*"],
            # Keep the daemon's secrets (OAuth token, run MCP configs, the DB) out of reach.
            # Read deny rules also govern Grep and Glob.
            disallowed=_secret_denies(settings),
        ),
        json_schema=PROPOSAL_SCHEMA,
        scope=Scope(projects=[project.id], tools=["set_project_summary"], write=True),
    )


def project_folders(conn: apsw.Connection, settings: Settings, project_id: str) -> list[Path]:
    """The project's accepted folder and local-repo resources that exist locally, as local paths."""
    unreviewed = pending_txns(conn, "resource")  # an agent-proposed folder is not trusted yet
    folders: list[Path] = []
    for resource in list_resources(conn, project_ids=[project_id], kinds=["folder", "git_local"]):
        if resource.id in unreviewed:
            continue
        local = to_local(settings, resource.locator)
        if local.is_dir() and local not in folders:
            folders.append(local)
    return folders


def project_folder_hosts(conn: apsw.Connection, project_id: str) -> tuple[list[str], list[str]]:
    """Host paths of the project's accepted folder and local-repo resources, and of every
    registered one (all projects), the set transcripts are attached by."""
    unreviewed = pending_txns(conn, "resource")
    mine = [
        r.locator
        for r in list_resources(conn, project_ids=[project_id], kinds=["folder", "git_local"])
        if r.id not in unreviewed
    ]
    registered = [r.locator for r in list_resources(conn, kinds=["folder", "git_local"])]
    return mine, registered


def _folder_cwd(conn: apsw.Connection, settings: Settings, project_id: str) -> Path | None:
    """The project's first accepted `folder` resource that exists locally, if any."""
    unreviewed = pending_txns(conn, "resource")  # an agent-proposed folder is not trusted yet
    for resource in list_resources(conn, project_ids=[project_id], kinds=["folder"]):
        if resource.id in unreviewed:
            continue
        local = to_local(settings, resource.locator)
        if local.is_dir():
            return local
    return None


def _deny_read(path: Path) -> str:
    """A Read deny rule for everything under `path`; `//` anchors at the filesystem root."""
    return f"Read(//{str(path.absolute()).lstrip('/')}/**)"


def _secret_denies(settings: Settings) -> list[str]:
    """Read deny rules for the daemon's secrets and the host's backend profiles (API keys)."""
    roots = [settings.cfg_root, settings.data_dir]
    if settings.backends_root is not None:
        roots.append(settings.backends_root)
    return [_deny_read(root) for root in roots]


def _read_only_plan(settings: Settings, run: Run, prompt: str) -> RunPlan:
    return RunPlan(
        prompt=prompt,
        system_prompt=READ_ONLY_SYSTEM,
        cwd=settings.runs_dir / run.id,
        tools=ToolPolicy(tools=[], allowed=["mcp__trajectree__*"]),
        json_schema=PROPOSAL_SCHEMA,
        scope=Scope(write=False),
    )


def _plan_summarize_session(
    conn: apsw.Connection, settings: Settings, run: Run, request: SummarizeSessionRequest, now: datetime
) -> RunPlan:
    signal = session_signal(conn, request.session_id)
    if signal is None:
        raise NotFound(f"unknown session {request.session_id}")
    transcript_path = signal.payload.get("transcript_path")
    if not isinstance(transcript_path, str) or not transcript_path:
        raise NotFound("transcript unreadable")
    try:
        digest = digest_session(
            str(signal.payload.get("backend") or "claude"), Path(transcript_path), max_chars=settings.digest_max_chars
        )
    except OSError:
        raise NotFound("transcript unreadable") from None
    project = resolve_project_ref(conn, signal.project_id)
    pack = build_context_pack(
        conn,
        project_ids=[project.id],
        t_from=now - timedelta(days=30),
        t_to=now + timedelta(days=30),
        token_budget=2000,
    )
    prompt = render_prompt(
        load_prompt(settings, "summarize_session"),
        {
            "session_id": request.session_id,
            "session_title": str(signal.payload.get("title") or request.session_id[:8]),
            "project_slug": project.slug,
            "project_title": project.title,
            "transcript_digest": digest or "_empty transcript_",
            "context_pack": pack.markdown,
            "today": _day(now),
        },
    )
    return _read_only_plan(settings, run, prompt)


def _plan_ask(
    conn: apsw.Connection, settings: Settings, run: Run, request: AskRequest, now: datetime
) -> RunPlan:
    if request.projects:
        projects = [resolve_project_ref(conn, ref) for ref in request.projects]
    else:
        projects = list_projects(conn, status="active")
    pack = build_context_pack(
        conn,
        project_ids=[p.id for p in projects],
        t_from=request.start or now - timedelta(days=90),
        t_to=request.end or now + timedelta(days=30),
        token_budget=request.token_budget,
        question=request.question,
    )
    prompt = render_prompt(
        load_prompt(settings, "ask"),
        {
            "question": request.question,
            "context_pack": pack.markdown,
            "projects": ", ".join(p.slug for p in projects),
            "today": _day(now),
        },
    )
    return _read_only_plan(settings, run, prompt)


def _plan_quick_add(
    conn: apsw.Connection, settings: Settings, run: Run, request: QuickAddRequest, now: datetime
) -> RunPlan:
    active = list_projects(conn, status="active")
    if get_project_by_slug(conn, "inbox") is not None:
        default = "inbox"
    elif request.project:
        default = resolve_project_ref(conn, request.project).slug
    else:
        default = active[0].slug if active else "inbox"
    prompt = render_prompt(
        load_prompt(settings, "quick_add"),
        {
            "text": request.text,
            "today": _day(now),
            "projects": "\n".join(f"- {p.slug}: {p.title}" for p in active),
            "default_project": default,
        },
    )
    return _read_only_plan(settings, run, prompt)


def _plan_report(
    conn: apsw.Connection, settings: Settings, run: Run, request: ReportRequest, now: datetime
) -> RunPlan:
    projects = [resolve_project_ref(conn, ref) for ref in request.projects]
    ids = [p.id for p in projects]
    pack = build_context_pack(
        conn,
        project_ids=ids,
        t_from=request.start or now - timedelta(days=90),
        t_to=request.end or now + timedelta(days=30),
        token_budget=request.token_budget,
    )
    try:
        template = (settings.prompts_dir / f"report_{request.kind}.md").read_text(encoding="utf-8")
    except FileNotFoundError:
        raise NotFound(f"prompt report_{request.kind}.md missing") from None
    prompt = render_prompt(
        template,
        {
            "title": request.title,
            "kind": request.kind,
            "projects": ", ".join(p.slug for p in projects),
            "context_pack": pack.markdown,
            "today": _day(now),
        },
    )
    return RunPlan(
        prompt=prompt,
        system_prompt=render_prompt(REPORT_SYSTEM, {"kind": request.kind}),
        cwd=settings.runs_dir / run.id,
        tools=ToolPolicy(tools=[], allowed=["mcp__trajectree__*"]),
        json_schema=PROPOSAL_SCHEMA,
        scope=Scope(projects=ids, tools=["save_report_draft"], write=True),
    )


def _plan_routine(
    conn: apsw.Connection, settings: Settings, run: Run, request: RoutineRequest, now: datetime
) -> RunPlan:
    routine = resolve_routine_ref(conn, run.routine_id or request.routine)
    refs = request.projects or routine.scope.projects
    projects: list[Project] = (
        [resolve_project_ref(conn, ref) for ref in refs] if refs else list_projects(conn, status="active")
    )
    if not projects:
        raise NotFound("no projects in scope")  # never mint an unrestricted token
    ids = [p.id for p in projects]
    pack = build_context_pack(
        conn,
        project_ids=ids,
        t_from=now - timedelta(days=routine.scope.lookback_days),
        t_to=now + timedelta(days=30),
        token_budget=8000,
    )
    try:
        template = (settings.prompts_dir / routine.prompt_ref).read_text(encoding="utf-8")
    except FileNotFoundError:
        raise NotFound(f"prompt {routine.prompt_ref} missing") from None
    prompt = render_prompt(
        template,
        {
            "routine_name": routine.name,
            "projects": ", ".join(p.slug for p in projects),
            "context_pack": pack.markdown,
            "today": _day(now, routine.tz),
            "lookback_days": str(routine.scope.lookback_days),
        },
    )
    cwd = settings.runs_dir / run.id
    if routine.cwd_mode == "project_folder":
        cwd = _folder_cwd(conn, settings, ids[0]) or cwd
    direct = routine.write_mode == "direct"
    return RunPlan(
        prompt=prompt,
        system_prompt=render_prompt(ROUTINE_SYSTEM, {"routine_name": routine.name}) if direct else READ_ONLY_SYSTEM,
        cwd=cwd,
        tools=ToolPolicy(
            tools=routine.tool_policy,
            allowed=[*routine.tool_policy, "mcp__trajectree__*"],
            disallowed=(
                _secret_denies(settings) if routine.tool_policy else []
            ),
        ),
        json_schema=PROPOSAL_SCHEMA,
        scope=Scope(projects=ids, write=direct),
        usd_cap=routine.caps.usd_per_run,
        delivery=routine.delivery,
        routine_id=routine.id,
        # Inbox delivery goes to the first explicitly scoped project, else the `inbox` project.
        delivery_project_id=ids[0] if refs else None,
        routine_name=routine.name,
        routine_tz=routine.tz,
    )


def _plan_starting_point(
    conn: apsw.Connection,
    settings: Settings,
    run: Run,
    request: StartingPointRequest,
    now: datetime,
    prelocked: dict[str, Any],
) -> RunPlan:
    project = resolve_project_ref(conn, run.project_id or request.project)
    history: HistoryInput = prelocked.get("history") or HistoryInput()
    sessions: str = prelocked.get("sessions") or ""
    if not (history.git_md or history.folder_md or sessions):
        raise NotFound("no history before the chosen start")
    day = block_date(request.starts)
    sources = "\n".join(f"- {source}: before {start.isoformat()}" for source, start in sorted(request.starts.items()))
    if history.truncated:
        sources += "\n- Some history was condensed to fit; say so rather than guessing."
    prompt = render_prompt(
        load_prompt(settings, "starting_point"),
        {
            "project_title": project.title,
            "project_slug": project.slug,
            "block_date": day.isoformat(),
            "git_history": history.git_md or "_none_",
            "session_history": sessions or "_none_",
            "folder_snapshot": history.folder_md or "_none_",
            "sources": sources,
        },
    )
    when = local_midnight(day, settings.tz).isoformat()
    return RunPlan(
        prompt=prompt,
        system_prompt=render_prompt(STARTING_POINT_SYSTEM, {"project_slug": project.slug, "when": when}),
        cwd=_folder_cwd(conn, settings, project.id) or settings.runs_dir / run.id,
        tools=ToolPolicy(
            tools=["Read", "Grep", "Glob", "Bash"],
            allowed=["Read", "Grep", "Glob", "Bash(git log *)", "mcp__trajectree__*"],
            disallowed=_secret_denies(settings),
        ),
        json_schema=PROPOSAL_SCHEMA,
        scope=Scope(projects=[project.id], tools=["record_event"], write=True),
    )
