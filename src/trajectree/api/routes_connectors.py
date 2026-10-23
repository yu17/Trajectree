from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

import anyio.to_thread
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from trajectree.api.deps import scoped_or_owner
from trajectree.auth.scopes import Principal, Scope
from trajectree.models import SignalIn
from trajectree.repo.projects import get_project_by_slug, resolve_project_ref
from trajectree.repo.signals import get_signal_by_key, upsert_signals
from trajectree.repo.state import get_state
from trajectree.runner.credentials import CONNECTOR_NAMES, load_connector_token, store_connector_token
from trajectree.timeutil import utcnow

CHATMAID_TOOL = "chatmaid"
MAX_BODY_BYTES = 64 * 1024

router = APIRouter()
# Mounted outside the owner-only /api router: it takes the owner token or a `chatmaid`-scoped token.
# One dependency object, so the route's own `Depends` reuses the router's (cached) principal.
chatmaid_auth = scoped_or_owner(CHATMAID_TOOL)
chatmaid_router = APIRouter(prefix="/api/connectors/chatmaid", dependencies=[Depends(chatmaid_auth)])


def _chatmaid_may_post_to(scope: Scope, project_id: str) -> bool:
    """`projects: []` (the documented recipe) or None means any project; a list restricts."""
    return scope.projects in (None, []) or project_id in scope.projects


class ConnectorCredentials(BaseModel):
    token: str = Field(min_length=1)


@router.post("/connectors/{name}/credentials")
def connector_credentials(name: str, body: ConnectorCredentials, request: Request) -> dict[str, bool]:
    if name not in CONNECTOR_NAMES:
        raise HTTPException(status_code=404, detail=f"unknown connector {name!r}")
    try:
        store_connector_token(request.app.state.settings, name, body.token)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"stored": True}


ConnectorName = Literal["claude-sessions", "local-git", "folders", "github", "gitlab", "slurm", "chatmaid", "recurrence"]
# connector -> (ingest:last section, cursor state key); either may be absent
_SECTIONS: dict[str, tuple[str | None, str | None]] = {
    "claude-sessions": ("scan", None),
    "local-git": ("git", None),
    "folders": ("folders", "folders:last"),
    "github": ("remote", "remote:last"),
    "gitlab": ("remote", "remote:last"),
    "slurm": ("slurm", "slurm:last"),
    "chatmaid": (None, None),
    "recurrence": ("recurrence", None),
}
MAX_ERRORS = 5


class ConnectorStatus(BaseModel):
    name: ConnectorName
    configured: bool
    last_run: AwareDatetime | None
    errors: list[str]
    detail: str | None


def _cursor_at(value: object) -> str | None:
    raw = value.get("at") if isinstance(value, dict) else value
    return raw if isinstance(raw, str) else None


def _parse_when(raw: object) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        when = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return when if when.tzinfo is not None else None


@router.get("/connectors/status", response_model=list[ConnectorStatus])
def connector_status(request: Request) -> list[ConnectorStatus]:
    settings = request.app.state.settings
    with request.app.state.ledger.locked() as conn:
        report = get_state(conn, "ingest:last")
        cursors = {key: get_state(conn, key) for key in ("folders:last", "remote:last", "slurm:last")}
    report = report if isinstance(report, dict) else {}
    github, gitlab = load_connector_token(settings, "github"), load_connector_token(settings, "gitlab")
    chatmaid_token = load_connector_token(settings, "chatmaid")
    config: dict[str, tuple[bool, str | None]] = {
        "claude-sessions": (bool(settings.claude_home or settings.backends_root), None),
        "local-git": (True, None),
        "folders": (settings.folders_scan_every_h > 0, f"every {settings.folders_scan_every_h} h"),
        "github": (github is not None, "token stored" if github else None),
        "gitlab": (
            gitlab is not None and bool(settings.gitlab_host),
            f"host {settings.gitlab_host}" if settings.gitlab_host else "set TRAJECTREE_GITLAB_HOST",
        ),
        "slurm": (bool(settings.slurm_host), f"host {settings.slurm_host}" if settings.slurm_host else None),
        "chatmaid": (
            bool(settings.chatmaid_url) or chatmaid_token is not None,
            f"url {settings.chatmaid_url}" if settings.chatmaid_url else ("token stored" if chatmaid_token else None),
        ),
        "recurrence": (True, None),
    }
    out: list[ConnectorStatus] = []
    for name, (section_key, cursor_key) in _SECTIONS.items():
        section = report.get(section_key) if section_key else None
        errors = section.get("errors") if isinstance(section, dict) else None
        last = _cursor_at(cursors[cursor_key]) if cursor_key else None
        if cursor_key is None and isinstance(section, dict):
            last = report.get("started")
        configured, detail = config[name]
        out.append(ConnectorStatus(
            name=name, configured=configured, last_run=_parse_when(last),
            errors=[str(e) for e in errors[:MAX_ERRORS]] if isinstance(errors, list) else [],
            detail=detail,
        ))
    return out


class ChatmaidMessage(BaseModel):
    """A reference and summary of a message; bodies are never accepted."""

    model_config = ConfigDict(extra="forbid")

    ref: str = Field(min_length=1, max_length=512)
    summary: str = Field(min_length=1, max_length=2000)
    channel: str = Field(min_length=1, max_length=64)
    project: str | None = None
    when: AwareDatetime | None = None
    participants: list[Annotated[str, StringConstraints(max_length=100)]] = Field(default_factory=list, max_length=10)


@chatmaid_router.post("/messages")
async def chatmaid_message(request: Request, principal: Principal = Depends(chatmaid_auth)) -> dict[str, str]:
    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="Payload Too Large")
    try:
        msg = ChatmaidMessage.model_validate_json(raw)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors(include_url=False, include_context=False, include_input=False)) from exc
    ledger = request.app.state.ledger
    now = utcnow()

    def work() -> dict[str, str]:
        with ledger.locked() as conn:
            if msg.project is not None:
                project = resolve_project_ref(conn, msg.project)
            else:
                project = get_project_by_slug(conn, "inbox")
                if project is None:
                    raise HTTPException(status_code=422, detail="no inbox project")
            if not _chatmaid_may_post_to(principal.scope, project.id):
                raise HTTPException(status_code=403, detail=f"token not permitted for project {project.slug}")
            key = f"chatmaid:{msg.ref}"
            upsert_signals(conn, [SignalIn(
                project_id=project.id,
                kind="message",
                t_start=msg.when or now,
                dedupe_key=key,
                payload={
                    "ref": msg.ref,
                    "summary": msg.summary,
                    "channel": msg.channel,
                    "participants": msg.participants,
                },
            )])
            signal = get_signal_by_key(conn, key)
        return {"signal_id": signal.id, "project_id": project.id}

    return await anyio.to_thread.run_sync(work)
