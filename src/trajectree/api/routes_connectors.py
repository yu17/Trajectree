from __future__ import annotations

from typing import Annotated

import anyio.to_thread
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from trajectree.api.deps import scoped_or_owner
from trajectree.models import SignalIn
from trajectree.repo.projects import get_project_by_slug, resolve_project_ref
from trajectree.repo.signals import get_signal_by_key, upsert_signals
from trajectree.runner.credentials import CONNECTOR_NAMES, store_connector_token
from trajectree.timeutil import utcnow

CHATMAID_TOOL = "chatmaid"
MAX_BODY_BYTES = 64 * 1024

router = APIRouter()
# Mounted outside the owner-only /api router: it takes the owner token or a `chatmaid`-scoped token.
chatmaid_router = APIRouter(prefix="/api/connectors/chatmaid", dependencies=[Depends(scoped_or_owner(CHATMAID_TOOL))])


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
async def chatmaid_message(request: Request) -> dict[str, str]:
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
