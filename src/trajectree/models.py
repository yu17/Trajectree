"""Pydantic domain models shared by the ledger, repositories and API."""

from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter
from pydantic import AwareDatetime, BaseModel, Field, field_validator, model_validator

ProjectKind = Literal["research", "tooling", "routine", "admin", "inbox"]
ProjectStatus = Literal["active", "paused", "done", "archived"]
EventStatus = Literal["happened", "planned", "suggested", "dropped"]
Source = Literal["human", "agent", "connector"]
TodoState = Literal["open", "doing", "done", "dropped"]
Review = Literal["none", "pending", "accepted", "rejected"]
ActorKind = Literal["human", "agent", "session", "connector"]
EntityType = Literal["project", "event", "todo", "resource"]
SignalKind = Literal["commits", "session", "files_changed", "job_state", "message", "remote"]
ResourceKind = Literal[
    "folder", "git_local", "git_remote", "claude_session", "slurm_job", "url", "doc"
]

SLUG_PATTERN = r"^[a-z0-9][a-z0-9-]{0,63}$"


def _check_span(t_start: AwareDatetime | None, t_end: AwareDatetime | None) -> None:
    if t_start is not None and t_end is not None and t_end < t_start:
        raise ValueError("t_end must not be before t_start")


class Actor(BaseModel):
    kind: ActorKind
    ref: str
    model: str | None = None
    backend: str | None = None
    tool: str | None = None
    tool_version: str | None = None
    session_id: str | None = None
    run_id: str | None = None
    proposed_by_run: str | None = None


# --- input models -----------------------------------------------------------


class ProjectIn(BaseModel):
    slug: str = Field(pattern=SLUG_PATTERN)
    title: str
    kind: ProjectKind
    status: ProjectStatus = "active"
    lane_order: int = 0
    summary_md: str = ""
    recurrence: str | None = None
    props: dict[str, Any] = Field(default_factory=dict)


class EventIn(BaseModel):
    project_id: str
    kind: str
    title: str
    body_md: str = ""
    t_start: AwareDatetime
    t_end: AwareDatetime | None = None
    status: EventStatus = "happened"
    source: Source = "human"
    props: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _end_not_before_start(self) -> "EventIn":
        _check_span(self.t_start, self.t_end)
        return self


class TodoIn(BaseModel):
    project_id: str | None = None
    title: str
    body_md: str = ""
    due: AwareDatetime | None = None
    state: TodoState = "open"
    priority: int = 0
    props: dict[str, Any] = Field(default_factory=dict)


class ResourceIn(BaseModel):
    project_id: str
    kind: ResourceKind
    locator: str
    title: str = ""
    props: dict[str, Any] = Field(default_factory=dict)


class SignalIn(BaseModel):
    project_id: str
    kind: SignalKind
    t_start: AwareDatetime
    dedupe_key: str
    t_end: AwareDatetime | None = None
    resource_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class EdgeIn(BaseModel):
    src_type: EntityType
    src_id: str
    dst_type: EntityType
    dst_id: str
    rel: str
    props: dict[str, Any] = Field(default_factory=dict)
    valid_from: AwareDatetime | None = None
    valid_to: AwareDatetime | None = None


# --- patch models -----------------------------------------------------------


class ProjectPatch(BaseModel):
    slug: str | None = Field(default=None, pattern=SLUG_PATTERN)
    title: str | None = None
    kind: ProjectKind | None = None
    status: ProjectStatus | None = None
    lane_order: int | None = None
    summary_md: str | None = None
    recurrence: str | None = None
    props: dict[str, Any] | None = None


class EventPatch(BaseModel):
    project_id: str | None = None
    kind: str | None = None
    title: str | None = None
    body_md: str | None = None
    t_start: AwareDatetime | None = None
    t_end: AwareDatetime | None = None
    status: EventStatus | None = None
    source: Source | None = None
    props: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _end_not_before_start(self) -> "EventPatch":
        _check_span(self.t_start, self.t_end)
        return self


class TodoPatch(BaseModel):
    project_id: str | None = None
    title: str | None = None
    body_md: str | None = None
    due: AwareDatetime | None = None
    state: TodoState | None = None
    priority: int | None = None
    props: dict[str, Any] | None = None


# --- stored entities --------------------------------------------------------


class Project(BaseModel):
    id: str
    slug: str
    title: str
    kind: ProjectKind
    status: ProjectStatus
    lane_order: int
    summary_md: str
    recurrence: str | None = None
    props: dict[str, Any] = Field(default_factory=dict)
    summary_at: AwareDatetime | None = None
    created_at: AwareDatetime
    updated_at: AwareDatetime


class Event(BaseModel):
    id: str
    project_id: str
    kind: str
    title: str
    body_md: str
    t_start: AwareDatetime
    t_end: AwareDatetime | None = None
    status: EventStatus
    source: Source
    props: dict[str, Any] = Field(default_factory=dict)
    created_txn: str | None = None
    created_at: AwareDatetime
    updated_at: AwareDatetime

    @model_validator(mode="after")
    def _end_not_before_start(self) -> "Event":
        _check_span(self.t_start, self.t_end)
        return self


class Todo(BaseModel):
    id: str
    project_id: str | None = None
    title: str
    body_md: str
    due: AwareDatetime | None = None
    state: TodoState
    priority: int
    props: dict[str, Any] = Field(default_factory=dict)
    created_at: AwareDatetime | None = None
    updated_at: AwareDatetime | None = None


class Resource(BaseModel):
    id: str
    project_id: str
    kind: str
    locator: str
    title: str = ""
    props: dict[str, Any] = Field(default_factory=dict)
    created_at: AwareDatetime | None = None
    updated_at: AwareDatetime | None = None


class Signal(SignalIn):
    id: str
    updated_at: AwareDatetime


class StripBin(BaseModel):
    project_id: str
    day: str
    kind: SignalKind
    count: int = Field(
        description="Commits for the commits kind (summed from payload counts); signals otherwise"
    )


class Edge(BaseModel):
    id: str
    src_type: EntityType
    src_id: str
    dst_type: EntityType
    dst_id: str
    rel: str
    props: dict[str, Any] = Field(default_factory=dict)
    valid_from: AwareDatetime | None = None
    valid_to: AwareDatetime | None = None
    created_at: AwareDatetime


class Txn(BaseModel):
    id: str
    ts: AwareDatetime
    actor_kind: ActorKind
    actor_ref: str
    model: str | None = None
    backend: str | None = None
    tool: str | None = None
    tool_version: str | None = None
    session_id: str | None = None
    run_id: str | None = None
    proposed_by_run: str | None = None
    intent: str
    changeset: bytes
    review: Review
    review_ts: AwareDatetime | None = None
    reverts_txn: str | None = None
    reverted_by: str | None = None


class Change(BaseModel):
    txn_id: str
    seq: int
    table_name: str
    op: str
    pk: list[Any]
    old: dict[str, Any] | None = None
    new: dict[str, Any] | None = None


class Conflict(BaseModel):
    reason: str
    table_name: str
    op: str
    pk: list[Any]
    old: dict[str, Any] | None = None
    new: dict[str, Any] | None = None
    conflict_row: dict[str, Any] | None = None


# --- phase 3: runs, sessions, caps ------------------------------------------

RunKind = Literal[
    "summarize", "ask", "quick_add", "summarize_session", "routine", "report", "starting_point"
]
RunSlot = Literal["interactive", "scheduled"]
SLOT_OF: dict[str, RunSlot] = {
    "summarize": "interactive",
    "ask": "interactive",
    "quick_add": "interactive",
    "summarize_session": "interactive",
    "routine": "scheduled",
    "report": "interactive",
    "starting_point": "interactive",
}
RunStatus = Literal["queued", "running", "ok", "failed", "capped", "cancelled"]
RUNNER_PATTERN = r"^(claude|codex|ccx:[A-Za-z0-9][A-Za-z0-9._-]*)$"
ProposalKind = Literal["event", "todo"]


class Proposal(BaseModel):
    kind: ProposalKind
    project: str
    title: str
    reason: str
    event_kind: str = "note"
    when: str | None = None
    due: str | None = None
    body_md: str = ""
    status: Literal["happened", "planned", "suggested"] = "happened"
    applied_txn: str | None = None
    applied_id: str | None = None
    dismissed: bool = False


class Run(BaseModel):
    id: str
    kind: RunKind
    project_id: str | None
    status: RunStatus
    runner: str = Field(pattern=RUNNER_PATTERN)
    backend: str
    model: str | None
    session_id: str | None
    started: AwareDatetime | None
    finished: AwareDatetime | None
    tokens_in: int = 0
    tokens_out: int = 0
    tokens_cache: int = 0
    est_usd: float = 0.0
    turns: int = 0
    duration_ms: int = 0
    result_md: str = ""
    proposals: list[Proposal] = Field(default_factory=list)
    error: str | None = None
    log_path: str | None = None
    token_id: str | None = None
    props: dict[str, Any] = Field(default_factory=dict)
    routine_id: str | None = None
    slot: RunSlot = "interactive"
    attempt: int = 1
    created_at: AwareDatetime


class SessionRow(BaseModel):
    id: str
    cwd: str
    project_id: str | None
    transcript_path: str | None
    backend: str = "claude"
    started_at: AwareDatetime
    last_seen: AwareDatetime
    ended_at: AwareDatetime | None = None
    end_reason: str | None = None
    source: str | None = None


class Caps(BaseModel):
    usd_per_run: float = Field(1.0, gt=0, le=100)
    usd_per_day: float = Field(5.0, gt=0, le=1000)
    timeout_s: int = Field(600, ge=1, le=7200)
    paused: bool = False

    @model_validator(mode="after")
    def _run_within_day(self) -> "Caps":
        if self.usd_per_run > self.usd_per_day:
            raise ValueError("usd_per_run must not exceed usd_per_day")
        return self


class UsageDay(BaseModel):
    day: str
    est_usd: float
    runs: int
    tokens_in: int
    tokens_out: int


class UsageSlice(BaseModel):
    key: str
    est_usd: float
    runs: int


# --- phase 4: routines and reports ------------------------------------------

WriteMode = Literal["direct", "propose"]
Delivery = Literal["none", "inbox", "log", "chatmaid"]
CwdMode = Literal["project_folder", "data_dir"]
ReportKind = Literal["deck", "memo"]

ROUTINE_NAME_PATTERN = r"^[a-z0-9][a-z0-9-]{0,63}$"
PROMPT_REF_PATTERN = r"^[a-z0-9_-]+\.md$"


def _check_trigger(value: str | None) -> str | None:
    if value is not None and value != "session_end" and not croniter.is_valid(value):
        raise ValueError("trigger must be a cron expression or 'session_end'")
    return value


def _check_tz(value: str | None) -> str | None:
    if value is not None:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("unknown timezone") from None
    return value


class RoutineScope(BaseModel):
    projects: list[str] | None = None
    lookback_days: int = Field(14, ge=1, le=365)


class RoutineCaps(BaseModel):
    usd_per_run: float = Field(1.0, gt=0, le=100)
    usd_per_day: float = Field(2.0, gt=0, le=1000)

    @model_validator(mode="after")
    def _run_within_day(self) -> "RoutineCaps":
        if self.usd_per_run > self.usd_per_day:
            raise ValueError("usd_per_run must not exceed usd_per_day")
        return self


class RoutineIn(BaseModel):
    name: str = Field(pattern=ROUTINE_NAME_PATTERN)
    trigger: str
    tz: str = "UTC"
    enabled: bool = False
    runner: str | None = Field(None, pattern=RUNNER_PATTERN)
    model: str | None = None
    prompt_ref: str = Field(pattern=PROMPT_REF_PATTERN)
    scope: RoutineScope = RoutineScope()
    cwd_mode: CwdMode = "data_dir"
    tool_policy: list[str] = []
    caps: RoutineCaps = RoutineCaps()
    write_mode: WriteMode = "propose"
    delivery: Delivery = "none"
    props: dict[str, Any] = {}

    _valid_trigger = field_validator("trigger")(_check_trigger)
    _valid_tz = field_validator("tz")(_check_tz)


class RoutinePatch(BaseModel):
    name: str | None = Field(None, pattern=ROUTINE_NAME_PATTERN)
    trigger: str | None = None
    tz: str | None = None
    enabled: bool | None = None
    runner: str | None = Field(None, pattern=RUNNER_PATTERN)
    model: str | None = None
    prompt_ref: str | None = Field(None, pattern=PROMPT_REF_PATTERN)
    scope: RoutineScope | None = None
    cwd_mode: CwdMode | None = None
    tool_policy: list[str] | None = None
    caps: RoutineCaps | None = None
    write_mode: WriteMode | None = None
    delivery: Delivery | None = None
    props: dict[str, Any] | None = None

    _valid_trigger = field_validator("trigger")(_check_trigger)
    _valid_tz = field_validator("tz")(_check_tz)

    @model_validator(mode="after")
    def _required_not_null(self) -> "RoutinePatch":
        # Only `runner` and `model` may be cleared; an omitted field stays unset.
        for name in sorted(self.model_fields_set - {"runner", "model"}):
            if getattr(self, name) is None:
                raise ValueError(f"{name} cannot be null")
        return self


class Routine(RoutineIn):
    id: str
    next_fire: AwareDatetime | None = None
    last_fired: AwareDatetime | None = None
    last_run_id: str | None = None
    last_error: str | None = None
    paused_until: AwareDatetime | None = None
    created_at: AwareDatetime
    updated_at: AwareDatetime


class ReportScope(BaseModel):
    projects: list[str] = []
    start: AwareDatetime | None = None
    end: AwareDatetime | None = None


class ReportIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    kind: ReportKind
    scope: ReportScope = ReportScope()
    source_md: str = ""
    props: dict[str, Any] = {}


class ReportPatch(BaseModel):
    title: str | None = Field(None, min_length=1, max_length=200)
    kind: ReportKind | None = None
    scope: ReportScope | None = None
    source_md: str | None = None
    props: dict[str, Any] | None = None


class Report(ReportIn):
    id: str
    created_at: AwareDatetime
    updated_at: AwareDatetime
