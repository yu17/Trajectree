"""Serve the MCP server over streamable HTTP at exactly `/mcp`, behind a bearer gate."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import anyio.to_thread
from fastapi import FastAPI
from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.types import Receive, Scope, Send

from trajectree.api.deps import token_matches
from trajectree.auth.scopes import Principal
from trajectree.auth.tokens import resolve_token
from trajectree.db.ledger import Ledger
from trajectree.mcpserver.server import principal_from_headers

ASGIHandler = Callable[[Scope, Receive, Send], Awaitable[None]]


class BearerGate:
    """ASGI wrapper admitting only the owner's bearer token or a live scoped token (no cookie)."""

    def __init__(self, handler: ASGIHandler, *, owner_token: str, ledger: Ledger) -> None:
        self.handler = handler
        self.owner_token = owner_token
        self.ledger = ledger

    @staticmethod
    def _bearer(scope: Scope) -> str | None:
        auth = ""
        for name, value in scope.get("headers", []):
            if name == b"authorization":
                auth = value.decode("latin-1")
                break
        scheme, _, value = auth.partition(" ")
        token = value.strip()
        if scheme.lower() != "bearer" or not token:
            return None
        return token

    def _authorized(self, token: str | None) -> bool:
        if token is None:
            return False
        if token_matches(token, self.owner_token):
            return True
        with self.ledger.locked() as conn:
            return resolve_token(conn, token) is not None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # the ledger lock may be held by the ingest worker: wait for it off the event loop
        if scope["type"] == "http" and not await anyio.to_thread.run_sync(
            self._authorized, self._bearer(scope)
        ):
            response = JSONResponse({"detail": "Unauthorized"}, status_code=401)
            await response(scope, receive, send)
            return
        await self.handler(scope, receive, send)


def owner_or_token_resolver(ledger: Ledger, owner_token: str) -> Callable[[Context], Principal]:
    """The production resolver: authenticate each tool call by the request's bearer header."""

    def resolve(ctx: Context) -> Principal:
        with ledger.locked() as conn:
            return principal_from_headers(ctx.headers, owner_token=owner_token, conn=conn)

    return resolve


def mount_mcp(app: FastAPI, mcp: MCPServer, *, ledger: Ledger, owner_token: str) -> None:
    # Called only to initialise mcp.session_manager; the returned app would serve
    # /mcp/ under a Mount, so a Route on the exact path is used instead.
    mcp.streamable_http_app(
        streamable_http_path="/",
        json_response=True,
        stateless_http=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    gate = BearerGate(mcp.session_manager.handle_request, owner_token=owner_token, ledger=ledger)
    app.router.routes.append(Route("/mcp", endpoint=gate, methods=["GET", "POST", "DELETE"]))
