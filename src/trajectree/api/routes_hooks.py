"""Receiver for Claude Code http hooks; authenticated by its own dependency, not the owner-only /api router."""

from __future__ import annotations

import json
import logging
from typing import Any

import anyio.to_thread
from fastapi import APIRouter, Depends, Request

from trajectree.api.deps import scoped_or_owner
from trajectree.connectors.hooks import HOOK_TOOL, handle_hook
from trajectree.timeutil import utcnow

log = logging.getLogger(__name__)

MAX_BODY_BYTES = 64 * 1024


require_hook_token = scoped_or_owner(HOOK_TOOL)


router = APIRouter(prefix="/api/hooks", dependencies=[Depends(require_hook_token)])


async def _receive(request: Request, backend: str) -> dict:
    body = await request.body()
    payload: Any = None
    if len(body) <= MAX_BODY_BYTES:
        try:
            payload = json.loads(body)
        except (ValueError, RecursionError):
            payload = None
    ledger = request.app.state.ledger

    def work():
        with ledger.locked() as conn:
            return handle_hook(conn, payload, now=utcnow(), backend=backend)

    try:
        handled = await anyio.to_thread.run_sync(work)
    except Exception:
        log.exception("%s hook failed", backend)
        return {}
    if handled is None:
        log.warning("ignored %s hook payload", backend)
    return {}


@router.post("/claude")
async def claude_hook(request: Request) -> dict:
    return await _receive(request, "claude")


@router.post("/codex")
async def codex_hook(request: Request) -> dict:
    return await _receive(request, "codex")
