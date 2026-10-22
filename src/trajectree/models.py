"""Pydantic domain models shared by the ledger, repositories and API."""

from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, Field, model_validator

ProjectKind = Literal["research", "tooling", "routine", "admin", "inbox"]
ProjectStatus = Literal["active", "paused", "done", "archived"]
EventStatus = Literal["happened", "planned", "suggested", "dropped"]
Source = Literal["human", "agent", "connector"]
TodoState = Literal["open", "doing", "done", "dropped"]
Review = Literal["none", "pending", "accepted", "rejected"]
ActorKind = Literal["human", "agent", "session", "connector"]
EntityType = Literal["project", "event", "todo", "resource"]
SignalKind = Literal["commits", "session", "files_changed", "job_state", "message"]
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
